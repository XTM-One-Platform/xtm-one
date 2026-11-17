/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

//! Signature-verified self-update.
//!
//! The runner tracks the server it is enrolled against: `GET /update-manifest`
//! (with this machine's `os`, `arch` and `current_version`) advertises the
//! runner build the platform itself ships as the target, so a runner always
//! converges on a build compatible with its server - essential for
//! self-hosted, where the runner must match the build shipped by the
//! customer's own platform.
//!
//! When a newer build is available and auto-update is enabled, [`UpdateController`]
//! downloads the version-matched archive plus its `.sha256` and `.minisig`,
//! verifies BOTH (checksum for integrity, minisign against the compiled-in
//! release keys for authenticity), extracts the binary and swaps the running
//! executable atomically ([`self_replace`]). Verification is mandatory: a build
//! with no compiled-in public key can still detect an update but will refuse to
//! apply it, so the network is never trusted to hand us code.
//!
//! The `.minisig` the pipeline stages is minisign's own four-line text; the
//! verifier also accepts the base64-wrapped form `tauri signer sign` writes to
//! `.sig`, because that is what every older release served under the
//! `.minisig` name (see `agent_core::release`, which the relay verifies with
//! too).
//!
//! A signature is checked the way the install scripts check it: the
//! key id it names selects one of the compiled-in release keys
//! (`apps/runner/release-signing.json`, so a key rotation can be staged), a key
//! or an R of small order is refused, and its trusted comment must name the
//! archive this machine takes (`file:`), the version the platform offered
//! (`version:`) and a `timestamp:` no older than the release floor. A version
//! the comment does not carry is not checked, so archives signed before release
//! signatures named their version keep verifying (every release signature names
//! its file and its time); and the runner never replaces itself with a build
//! that is not newer than itself.
//!
//! The apply machinery is gated behind the `self-update` feature; the detection
//! + logging path is always compiled.

use std::sync::atomic::AtomicBool;
use std::sync::Arc;
use std::time::Duration;

use tracing::{info, warn};

use crate::api::ApiClient;
use crate::version::AGENT_VERSION;

/// A build the server advertises for this runner's os/arch.
// The download/integrity URLs are only consumed by the `self-update` applier; a
// build without that feature still reports availability but never reads them.
#[cfg_attr(not(feature = "self-update"), allow(dead_code))]
pub struct UpdateInfo {
    /// The server's own version (the target we converge on).
    pub version: String,
    pub url: Option<String>,
    pub sha256_url: Option<String>,
    pub signature_url: Option<String>,
    /// A newer build than what this runner reports exists.
    pub update_available: bool,
    /// This runner is below the hard protocol floor (refused until it updates).
    pub needs_update: bool,
}

/// Ask the server what the current build is for this `os`/`arch`.
///
/// Fail-soft: any transport / parse error returns `None` (no update known) so a
/// flaky check never disrupts the runner. Prefers the server's `target` block
/// (authoritative decision), falling back to the `artifacts` table + a version
/// comparison if an older server omits it.
pub async fn check(api: &ApiClient, os_name: &str, arch: &str) -> Option<UpdateInfo> {
    let manifest = api.update_manifest(os_name, arch).await.ok()?;
    // The version we converge on (and show the user) is the RUNNER build the
    // server embeds (`runner_version`, MAJOR.YYMMDD.SEQ) — never the platform's
    // own `version`, which uses a different scheme (1.YYMMDD.0-dev.<sha>) and is
    // meaningless next to AGENT_VERSION. Falling back to `version` only keeps
    // very old servers, which had no `runner_version` field, working.
    let server_version = manifest
        .get("runner_version")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .or_else(|| manifest.get("version").and_then(|v| v.as_str()))
        .unwrap_or_default()
        .to_string();

    let as_str =
        |v: &serde_json::Value, key: &str| v.get(key).and_then(|x| x.as_str()).map(str::to_string);

    if let Some(target) = manifest.get("target") {
        let info = UpdateInfo {
            version: as_str(target, "version")
                .filter(|s| !s.is_empty())
                .unwrap_or(server_version),
            url: as_str(target, "url"),
            sha256_url: as_str(target, "sha256_url"),
            signature_url: as_str(target, "signature_url"),
            update_available: target
                .get("update_available")
                .and_then(|v| v.as_bool())
                .unwrap_or(false),
            needs_update: target
                .get("needs_update")
                .and_then(|v| v.as_bool())
                .unwrap_or(false),
        };
        if !info.update_available && !info.needs_update {
            return None;
        }
        return Some(info);
    }

    // Fallback for an older server without a per-target decision: compare
    // versions and read the artifact URLs straight from the table.
    let update_available = !server_version.is_empty() && server_version != AGENT_VERSION;
    if !update_available {
        return None;
    }
    // Older servers answer without a `target` block. Read the artifacts table
    // directly - and for a headless runner take the nested `headless` entry,
    // never the desktop archive sitting next to it.
    let entry = manifest
        .get("artifacts")
        .and_then(|a| a.get(os_name))
        .and_then(|o| o.get(arch))
        .and_then(|e| {
            if crate::version::FLAVOR == "headless" {
                e.get("headless")
            } else {
                Some(e)
            }
        });
    Some(UpdateInfo {
        version: server_version,
        url: entry.and_then(|e| as_str(e, "url")),
        sha256_url: entry.and_then(|e| as_str(e, "sha256_url")),
        signature_url: entry.and_then(|e| as_str(e, "signature_url")),
        update_available,
        needs_update: false,
    })
}

/// Owns everything the runner needs to decide on and apply a self-update, and
/// to signal the main loop to restart into the new binary.
pub struct UpdateController {
    api: ApiClient,
    os_name: &'static str,
    arch: &'static str,
    auto_update: bool,
    /// Set true once a new binary has been swapped in; the main loop re-execs.
    restart: Arc<AtomicBool>,
    /// Trips graceful shutdown so in-flight work drains before the re-exec.
    shutdown_tx: tokio::sync::watch::Sender<bool>,
}

impl UpdateController {
    pub fn new(
        api: ApiClient,
        os_name: &'static str,
        arch: &'static str,
        auto_update: bool,
        restart: Arc<AtomicBool>,
        shutdown_tx: tokio::sync::watch::Sender<bool>,
    ) -> Self {
        Self {
            api,
            os_name,
            arch,
            auto_update,
            restart,
            shutdown_tx,
        }
    }

    /// Check for a newer build and, when allowed, apply it. On a successful swap
    /// this sets the restart flag and trips shutdown so the main loop re-execs.
    pub async fn maybe_apply(&self) {
        let Some(info) = check(&self.api, self.os_name, self.arch).await else {
            return;
        };

        if !self.auto_update {
            info!(
                "a newer runner ({} vs current {}) is available; auto-update is \
                 disabled on this runner, update it out-of-band",
                info.version, AGENT_VERSION
            );
            return;
        }

        #[cfg(feature = "self-update")]
        {
            match self.apply(&info).await {
                Ok(()) => {
                    info!(
                        "self-updated to runner {} (from {}); restarting",
                        info.version, AGENT_VERSION
                    );
                    self.restart
                        .store(true, std::sync::atomic::Ordering::SeqCst);
                    let _ = self.shutdown_tx.send(true);
                }
                Err(e) => warn!(
                    "self-update to {} failed ({e:#}); staying on {} and will retry",
                    info.version, AGENT_VERSION
                ),
            }
        }
        #[cfg(not(feature = "self-update"))]
        {
            let _ = &self.restart;
            let _ = &self.shutdown_tx;
            warn!(
                "a newer runner ({} vs {}) is available, but this build has no \
                 self-update support compiled in; download the new build manually",
                info.version, AGENT_VERSION
            );
        }
    }

    /// Run the periodic update check until shutdown: once shortly after start,
    /// then every 6 hours.
    pub async fn run_periodic(self: Arc<Self>, mut shutdown: tokio::sync::watch::Receiver<bool>) {
        let mut ticker = tokio::time::interval(Duration::from_secs(6 * 3600));
        ticker.tick().await; // consume the immediate tick; next fires in 6h
                             // Small initial delay so it never races enrollment logging.
        tokio::time::sleep(Duration::from_secs(30)).await;
        loop {
            if *shutdown.borrow() {
                return;
            }
            self.maybe_apply().await;
            tokio::select! {
                _ = ticker.tick() => {}
                _ = shutdown.changed() => { if *shutdown.borrow() { return; } }
            }
        }
    }

    #[cfg(feature = "self-update")]
    async fn apply(&self, info: &UpdateInfo) -> anyhow::Result<()> {
        use anyhow::{anyhow, Context};

        let url = info.url.as_deref().ok_or_else(|| {
            anyhow!(
                "server advertised no downloadable build for this platform \
                 (deployment built without embedded runner artifacts?)"
            )
        })?;
        let sha_url = info
            .sha256_url
            .as_deref()
            .ok_or_else(|| anyhow!("server advertised no SHA-256 for the update"))?;
        let sig_url = info
            .signature_url
            .as_deref()
            .ok_or_else(|| anyhow!("server advertised no signature for the update"))?;
        let pubkeys = crate::version::MINISIGN_PUBKEY.ok_or_else(|| {
            anyhow!("no compiled-in minisign public key; refusing to self-update unsigned code")
        })?;
        let timestamp_floor = compiled_signature_floor().map_err(|e| anyhow!(e))?;
        let flavor = crate::version::FLAVOR;
        let archive_name =
            release_archive_name(self.os_name, self.arch, flavor).ok_or_else(|| {
                anyhow!(
                    "the release ships no {flavor} archive for {}/{}",
                    self.os_name,
                    self.arch
                )
            })?;
        refuse_rollback(&info.version, AGENT_VERSION)?;

        info!("downloading runner update {} ({url}) ...", info.version);
        let archive = self.api.download_bytes(url).await?;
        let sha_txt = self.api.download_text(sha_url).await?;
        let sig_txt = self.api.download_text(sig_url).await?;

        verify_sha256(&archive, &sha_txt).context("verifying archive checksum")?;
        let requirements = Requirements {
            file: Some(archive_name.as_str()),
            version: Some(info.version.as_str()).filter(|v| !v.is_empty()),
            timestamp_floor,
        };
        let trusted_comment = verify_minisign(&archive, &sig_txt, pubkeys, &requirements)
            .context("verifying archive signature")?;
        if comment_field(&trusted_comment, "version").is_none() {
            info!(
                "the signature of runner {} names no version (signed before release \
                 signatures carried one): the version is the platform's word",
                info.version
            );
        }

        let exe = std::env::current_exe().context("resolving the current executable")?;
        let bin_name = exe
            .file_name()
            .and_then(|s| s.to_str())
            .unwrap_or("xtm-one-runner")
            .to_string();

        let tmp_dir =
            std::env::temp_dir().join(format!("xtm-one-runner-update-{}", std::process::id()));
        std::fs::create_dir_all(&tmp_dir).context("creating the update staging dir")?;
        let extracted = extract_binary(&archive, url, &bin_name, &tmp_dir);
        let new_bin = match extracted {
            Ok(p) => p,
            Err(e) => {
                let _ = std::fs::remove_dir_all(&tmp_dir);
                return Err(e);
            }
        };

        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&new_bin, std::fs::Permissions::from_mode(0o755));
        }

        let swapped = self_replace::self_replace(&new_bin)
            .context("swapping the running executable with the new build");
        let _ = std::fs::remove_dir_all(&tmp_dir);
        swapped?;
        Ok(())
    }
}

// Verifying a release is shared with the relay (`agent_core::release`); what is
// the runner's own is the name the refusals call it and the archives it takes.
#[cfg(feature = "self-update")]
use agent_core::release::verify_sha256;
#[cfg(feature = "self-update")]
pub use agent_core::release::{comment_field, Requirements, SignatureCheck};
#[cfg(all(test, feature = "self-update"))]
use agent_core::release::{has_small_order, parse_minisign_pubkey, SMALL_ORDER_POINTS};

/// What the refusals call this agent.
#[cfg(feature = "self-update")]
const SUBJECT: &str = "runner";

/// Verify an update the way [`check_signature`] does; `Ok` carries the signed
/// trusted comment.
#[cfg(feature = "self-update")]
fn verify_minisign(
    bytes: &[u8],
    signature: &str,
    pubkeys: &str,
    requirements: &Requirements,
) -> anyhow::Result<String> {
    agent_core::release::verify_minisign(SUBJECT, bytes, signature, pubkeys, requirements)
}

/// The oldest release-signature `timestamp:` this build accepts
/// (`XTM_RUNNER_SIGNATURE_FLOOR`); 0 when it was built without one.
#[cfg(feature = "self-update")]
pub fn compiled_signature_floor() -> Result<u64, String> {
    agent_core::release::signature_floor(crate::version::SIGNATURE_FLOOR)
}

/// The stable name of the release archive for this machine: what the platform
/// serves it as and what its signed trusted comment names (`_BARE_ARCHIVES` /
/// `_HEADLESS_ARCHIVES` in backend/app/api/v1/runners.py). `None` for a build
/// the release does not ship.
#[cfg(feature = "self-update")]
fn release_archive_name(os_name: &str, arch: &str, flavor: &str) -> Option<String> {
    match (os_name, arch, flavor) {
        ("windows", "x64" | "arm64", "desktop") => {
            Some(format!("xtm-one-runner-windows-{arch}.zip"))
        }
        // One universal binary covers both Mac architectures.
        ("macos", "x64" | "arm64", "desktop") => {
            Some("xtm-one-runner-macos-universal.tar.gz".to_string())
        }
        ("linux", "x64" | "arm64", "desktop") => {
            Some(format!("xtm-one-runner-linux-{arch}.tar.gz"))
        }
        ("linux", "x64" | "arm64", "headless") => {
            Some(format!("xtm-one-runner-linux-{arch}-headless.tar.gz"))
        }
        _ => None,
    }
}

/// Refuse to replace this runner with a build that is provably not newer: the
/// platform only offers newer ones, so anything else is a rollback. A version
/// that does not parse is left to the signature checks, as before.
#[cfg(feature = "self-update")]
fn refuse_rollback(offered: &str, current: &str) -> anyhow::Result<()> {
    agent_core::release::refuse_rollback(SUBJECT, offered, current)
}

/// Check `bytes` against a detached minisign `signature` under `pubkeys`, with
/// the rules [`verify_minisign`] applies to an update: see
/// `agent_core::release::check_signature`.
#[cfg(feature = "self-update")]
pub fn check_signature(
    bytes: &[u8],
    signature: &str,
    pubkeys: &str,
    requirements: &Requirements,
) -> SignatureCheck {
    agent_core::release::check_signature(SUBJECT, bytes, signature, pubkeys, requirements)
}

/// Extract the runner binary from a `.zip` (Windows) or `.tar.gz` (unix) archive.
#[cfg(feature = "self-update")]
fn extract_binary(
    archive: &[u8],
    url: &str,
    bin_name: &str,
    out_dir: &std::path::Path,
) -> anyhow::Result<std::path::PathBuf> {
    let binary = agent_core::release::ArchiveBinary {
        subject: SUBJECT,
        bin_name,
        accepted: &["xtm-one-runner", "xtm-one-runner.exe"],
    };
    agent_core::release::extract_binary(archive, url, &binary, out_dir)
}

#[cfg(all(test, feature = "self-update"))]
pub(crate) mod tests {
    use super::{
        check_signature, comment_field, has_small_order, parse_minisign_pubkey, refuse_rollback,
        release_archive_name, verify_sha256, Requirements, SignatureCheck,
    };

    /// The self-update check with nothing required of the trusted comment.
    fn verify_minisign(bytes: &[u8], signature: &str, pubkeys: &str) -> anyhow::Result<String> {
        super::verify_minisign(bytes, signature, pubkeys, &Requirements::default())
    }

    fn check(bytes: &[u8], signature: &str, pubkeys: &str) -> SignatureCheck {
        check_signature(bytes, signature, pubkeys, &Requirements::default())
    }

    // A throwaway minisign keypair minted for these tests (the private half was
    // never persisted). The payload was signed once with the `minisign` crate,
    // prehashed like `tauri signer sign` signs release archives, and the same
    // signature is carried in BOTH published forms so the tests pin the exact
    // bytes each form contains rather than re-deriving one from the other.
    pub(crate) const PAYLOAD: &[u8] = b"xtm-one-runner self-update fixture payload (#3743)\n";
    pub(crate) const FIXTURE_PUBKEY_BARE: &str =
        "RWSRNhxpZP3VeNdBcMrZ70t+ks6N46DKBb15Q9yCGtYii+76LXWRTd7T";
    const FIXTURE_PUBKEY_TAURI: &str = "dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IDc4RDVGRDY0NjkxQzM2OTEKUldTUk5oeHBaUDNWZU5kQmNNclo3MHQra3M2TjQ2REtCYjE1UTl5Q0d0WWlpKzc2TFhXUlRkN1QK";
    /// The raw minisign text: what the release pipeline stages as `.minisig`.
    pub(crate) const SIG_RAW: &str = "untrusted comment: signature from xtm-one-runner test key\nRUSRNhxpZP3VePJ9Gt9CA/TxR2O4oPCtO+vp0gPyMUuTHLwleciB4JQoQFkyfAEAqKvv/4uCXCk1p6SLNm+S9YkD2H3+J9DKiw8=\ntrusted comment: timestamp:1790000000\tfile:fixture.tar.gz\nTEKrt719BBzN00ESdP6yW2DruNkJT1VDUmEae/USsn3IZgs0fPyo8EMEjIbyfPxzudZZA0pwtIK9UD8mT6wyCA==\n";
    /// The same signature as `tauri signer sign` writes it to `.sig`: base64 of
    /// the whole text, one line. Every older release served this as
    /// `.minisig`, and it must keep verifying so an old mirror still works.
    const SIG_TAURI: &str = "dW50cnVzdGVkIGNvbW1lbnQ6IHNpZ25hdHVyZSBmcm9tIHh0bS1vbmUtcnVubmVyIHRlc3Qga2V5ClJVU1JOaHhwWlAzVmVQSjlHdDlDQS9UeFIyTzRvUEN0Tyt2cDBnUHlNVXVUSEx3bGVjaUI0SlFvUUZreWZBRUFxS3Z2LzR1Q1hDazFwNlNMTm0rUzlZa0QySDMrSjlES2l3OD0KdHJ1c3RlZCBjb21tZW50OiB0aW1lc3RhbXA6MTc5MDAwMDAwMAlmaWxlOmZpeHR1cmUudGFyLmd6ClRFS3J0NzE5QkJ6TjAwRVNkUDZ5VzJEcnVOa0pUMVZEVW1FYWUvVVNzbjNJWmdzMGZQeW84RU1FaklieWZQeHp1ZFpaQTBwd3RJSzlVRDhtVDZ3eUNBPT0K";

    #[test]
    fn verifies_the_raw_minisign_text_the_pipeline_stages() {
        verify_minisign(PAYLOAD, SIG_RAW, FIXTURE_PUBKEY_BARE).expect("raw .minisig must verify");
    }

    #[test]
    fn verifies_the_tauri_form_every_release_before_3743_served() {
        // The production regression: a single base64 line, exactly what
        // `tauri signer sign` writes and what `.minisig` used to contain.
        assert_eq!(SIG_TAURI.lines().count(), 1);
        verify_minisign(PAYLOAD, SIG_TAURI, FIXTURE_PUBKEY_BARE)
            .expect("Tauri-form .minisig must verify");
    }

    #[test]
    fn verifies_with_either_pubkey_form() {
        verify_minisign(PAYLOAD, SIG_RAW, FIXTURE_PUBKEY_TAURI).expect("tauri-form pubkey");
        verify_minisign(PAYLOAD, SIG_TAURI, FIXTURE_PUBKEY_TAURI).expect("both tauri forms");
    }

    #[test]
    fn tolerates_surrounding_whitespace_and_crlf() {
        let padded = format!("\n{}\n\n", SIG_TAURI);
        verify_minisign(PAYLOAD, &padded, FIXTURE_PUBKEY_BARE).expect("padded tauri form");
        let crlf = SIG_RAW.replace('\n', "\r\n");
        verify_minisign(PAYLOAD, &crlf, FIXTURE_PUBKEY_BARE).expect("CRLF raw form");
    }

    #[test]
    fn rejects_a_signature_over_other_bytes() {
        let mut tampered = PAYLOAD.to_vec();
        tampered[0] ^= 0x01;
        for sig in [SIG_RAW, SIG_TAURI] {
            let err = verify_minisign(&tampered, sig, FIXTURE_PUBKEY_BARE).unwrap_err();
            assert!(
                format!("{err:#}").contains("did not verify"),
                "unexpected error: {err:#}"
            );
        }
    }

    #[test]
    fn rejects_garbage_signature_files() {
        for sig in [
            "",
            "definitely-not-a-signature",
            "dGhpcyBpcyBub3QgYSBzaWduYXR1cmU=",
        ] {
            let err = verify_minisign(PAYLOAD, sig, FIXTURE_PUBKEY_BARE).unwrap_err();
            assert!(
                format!("{err:#}").contains("invalid minisign signature file"),
                "unexpected error for {sig:?}: {err:#}"
            );
        }
    }

    #[test]
    fn sha256_accepts_both_file_forms() {
        // sha256 of PAYLOAD, as `sha256sum` writes it and as a bare digest.
        let digest = {
            use sha2::{Digest, Sha256};
            hex::encode(Sha256::digest(PAYLOAD))
        };
        verify_sha256(PAYLOAD, &format!("{digest}  fixture.tar.gz\n")).expect("sha256sum form");
        verify_sha256(PAYLOAD, &digest.to_ascii_uppercase()).expect("bare digest");
        assert!(verify_sha256(b"other", &digest).is_err());
        assert!(verify_sha256(PAYLOAD, "not-a-digest").is_err());
    }

    /// The production trust root EXACTLY as it lives in `tauri.conf.json`
    /// (`plugins.updater.pubkey`): base64 of the whole minisign `.pub` file,
    /// untrusted-comment line included. CI bakes this value verbatim into
    /// `XTM_RUNNER_MINISIGN_PUBKEY`, so if it stops parsing every production
    /// build refuses to self-update.
    const TAURI_FORM: &str = "dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IEI3M0FBNzE5NkM3RUI5MwpSV1NUNjhlV2NhcHpDOExSdEZpN2JPU1BzSllCQmVhdjNmMEdMNUFZdmRyWUtsS2pEUTlzSVNBcQo=";
    /// The same key as bare key data (the `.pub` file's second line).
    const BARE_FORM: &str = "RWST68eWcapzC8LRtFi7bOSPsJYBBeav3f0GL5AYvdrYKlKjDQ9sISAq";

    #[test]
    fn parses_the_tauri_form_production_pubkey() {
        parse_minisign_pubkey(TAURI_FORM).expect("tauri-form pubkey must parse");
    }

    #[test]
    fn parses_bare_key_data() {
        parse_minisign_pubkey(BARE_FORM).expect("bare-form pubkey must parse");
    }

    #[test]
    fn rejects_garbage() {
        assert!(parse_minisign_pubkey("definitely-not-a-key").is_err());
        assert!(parse_minisign_pubkey("").is_err());
    }

    // -- `verify-signature` --------------------------------------------

    /// `SIG_RAW` with the 2-byte algorithm of its signature line replaced.
    fn with_algorithm(algorithm: &[u8; 2]) -> String {
        use base64::Engine as _;
        let b64 = base64::engine::general_purpose::STANDARD;
        let mut lines: Vec<String> = SIG_RAW.lines().map(str::to_string).collect();
        let mut signature = b64.decode(&lines[1]).expect("fixture signature line");
        signature[..2].copy_from_slice(algorithm);
        lines[1] = b64.encode(signature);
        lines.join("\n") + "\n"
    }

    /// The fixture key with one byte of its key id flipped: another key.
    fn other_key_id() -> String {
        use base64::Engine as _;
        let b64 = base64::engine::general_purpose::STANDARD;
        let mut key = b64.decode(FIXTURE_PUBKEY_BARE).expect("fixture key");
        key[2] ^= 0x01;
        b64.encode(key)
    }

    #[test]
    fn check_verifies_every_published_form() {
        let crlf = SIG_RAW.replace('\n', "\r\n");
        for (sig, key) in [
            (SIG_RAW, FIXTURE_PUBKEY_BARE),
            (SIG_TAURI, FIXTURE_PUBKEY_BARE),
            (crlf.as_str(), FIXTURE_PUBKEY_BARE),
            (SIG_RAW, FIXTURE_PUBKEY_TAURI),
        ] {
            match check(PAYLOAD, sig, key) {
                SignatureCheck::Verified { trusted_comment } => {
                    assert_eq!(trusted_comment, "timestamp:1790000000\tfile:fixture.tar.gz")
                }
                other => panic!("expected a verified signature, got {other:?}"),
            }
        }
    }

    #[test]
    fn check_rejects_a_signature_that_does_not_hold() {
        let mut tampered = PAYLOAD.to_vec();
        tampered[0] ^= 0x01;
        let edited_comment = SIG_RAW.replace("file:fixture.tar.gz", "file:other.tar.gz");
        let other_key = other_key_id();
        let legacy = with_algorithm(b"Ed");
        let unknown = with_algorithm(b"XX");
        for (payload, sig, key, reason) in [
            (
                &tampered[..],
                SIG_RAW,
                FIXTURE_PUBKEY_BARE,
                "changed after it was signed",
            ),
            (
                PAYLOAD,
                edited_comment.as_str(),
                FIXTURE_PUBKEY_BARE,
                "changed after it was signed",
            ),
            (PAYLOAD, SIG_RAW, other_key.as_str(), "another key"),
            (PAYLOAD, legacy.as_str(), FIXTURE_PUBKEY_BARE, "legacy"),
            (
                PAYLOAD,
                unknown.as_str(),
                FIXTURE_PUBKEY_BARE,
                "does not define",
            ),
        ] {
            match check(payload, sig, key) {
                SignatureCheck::Rejected(why) => assert!(why.contains(reason), "{why}"),
                other => panic!("expected a rejection ({reason}), got {other:?}"),
            }
        }
    }

    #[test]
    fn check_reports_what_cannot_be_checked() {
        for (sig, key) in [
            ("", FIXTURE_PUBKEY_BARE),
            ("<!DOCTYPE html><html>Sign in</html>", FIXTURE_PUBKEY_BARE),
            (SIG_RAW, "definitely-not-a-key"),
        ] {
            assert!(
                matches!(check(PAYLOAD, sig, key), SignatureCheck::Unreadable(_)),
                "expected unreadable input for {sig:?} / {key:?}"
            );
        }
    }

    // -- the archive and the version signed, a key list, small order ----

    /// A second throwaway key (its private half was never stored) and what it
    /// signed over `PAYLOAD`: the same trusted comment as `SIG_RAW`, one that
    /// also names a runner version, and one of a signer's own that names
    /// nothing (`minisign -t`).
    pub(crate) const SECOND_KEY: &str = "RWSkpNA/D0II3fqTapZXAP3nyOTH4SeZ1fTnSe3ZEEmgpJhaRSfeZulf";
    const SIG_SECOND_KEY: &str = "untrusted comment: signature from a second xtm-one-runner test key\nRUSkpNA/D0II3SfiHN41aYmDuhaTOzEh8XDl9zitxxgBh2+M+hr3Vo2Lj3kvhQy98P88tNG2C9bVLcAwsxECJ0ayX3HZevDsLwI=\ntrusted comment: timestamp:1790000000\tfile:fixture.tar.gz\nZomcPnP/btIjcxHKxhjZcv6+01+cP5EfNd8YfgFnlWHKo3tVpgKTj065nIG90yei/FBEO5M70hHrG4uTkUepBw==\n";
    pub(crate) const SIG_VERSIONED: &str = "untrusted comment: signature from a second xtm-one-runner test key\nRUSkpNA/D0II3SfiHN41aYmDuhaTOzEh8XDl9zitxxgBh2+M+hr3Vo2Lj3kvhQy98P88tNG2C9bVLcAwsxECJ0ayX3HZevDsLwI=\ntrusted comment: timestamp:1790000100\tfile:fixture.tar.gz\tversion:1.260928.57\n5oFTY+D91iqx2oVgoleu8S3uLyxQkL9HUK6YCf5FK1fcJQc9XCWhR8JJRXru+YEkVAI4sfM4C9O9lnDcnKdxBA==\n";
    const SIG_UNBOUND: &str = "untrusted comment: signature from a second xtm-one-runner test key\nRUSkpNA/D0II3SfiHN41aYmDuhaTOzEh8XDl9zitxxgBh2+M+hr3Vo2Lj3kvhQy98P88tNG2C9bVLcAwsxECJ0ayX3HZevDsLwI=\ntrusted comment: release build\n2jmsGYMIHqrib/ioZV2mMh+Q216oqPnkbHvX9Dg05hBIQRbjhHfknpwCFY7Rlz/jwYNyQRaYIJ9PqhvmfIrRCw==\n";

    fn verified(result: SignatureCheck) -> String {
        match result {
            SignatureCheck::Verified { trusted_comment } => trusted_comment,
            other => panic!("expected a verified signature, got {other:?}"),
        }
    }

    fn rejected(result: SignatureCheck) -> String {
        match result {
            SignatureCheck::Rejected(why) => why,
            other => panic!("expected a rejection, got {other:?}"),
        }
    }

    fn for_file(file: &str) -> Requirements<'_> {
        Requirements {
            file: Some(file),
            ..Requirements::default()
        }
    }

    #[test]
    fn a_signature_is_bound_to_the_archive_it_names() {
        // A validly signed build of another platform or flavor served in place
        // of this machine's is refused, as the install scripts refuse it.
        let keys = FIXTURE_PUBKEY_BARE;
        let why = rejected(check_signature(
            PAYLOAD,
            SIG_RAW,
            keys,
            &for_file("xtm-one-runner-linux-x64.tar.gz"),
        ));
        assert_eq!(
            why,
            "it was made for fixture.tar.gz, not xtm-one-runner-linux-x64.tar.gz"
        );
        verified(check_signature(
            PAYLOAD,
            SIG_RAW,
            keys,
            &for_file("fixture.tar.gz"),
        ));
        let err = super::verify_minisign(
            PAYLOAD,
            SIG_TAURI,
            keys,
            &for_file("xtm-one-runner-linux-arm64-headless.tar.gz"),
        )
        .unwrap_err();
        assert!(
            format!("{err:#}").contains("made for fixture.tar.gz"),
            "{err:#}"
        );
    }

    #[test]
    fn a_comment_that_names_no_file_or_time_is_refused_where_one_is_required() {
        // SIG_UNBOUND's comment (a signer's own, "release build") names neither;
        // every release signature names both.
        let why = rejected(check_signature(
            PAYLOAD,
            SIG_UNBOUND,
            SECOND_KEY,
            &for_file("fixture.tar.gz"),
        ));
        assert_eq!(
            why,
            "it names no file, so nothing ties it to fixture.tar.gz"
        );
        let why = rejected(check_signature(
            PAYLOAD,
            SIG_UNBOUND,
            SECOND_KEY,
            &Requirements {
                timestamp_floor: 1_790_000_000,
                ..Requirements::default()
            },
        ));
        assert_eq!(
            why,
            "it names no timestamp, so nothing says it is newer than 1790000000"
        );
        // Nothing required of it (a key given by hand to verify-signature).
        assert_eq!(
            verified(check(PAYLOAD, SIG_UNBOUND, SECOND_KEY)),
            "release build"
        );
    }

    /// Every archive the platform serves, with the machine that takes it:
    /// `_BARE_ARCHIVES` / `_HEADLESS_ARCHIVES` in backend/app/api/v1/runners.py
    /// (backend/tests/test_runner_install_scripts.py keeps the two equal).
    const SERVED_ARCHIVES: &[(&str, &str, &str, &str)] = &[
        (
            "windows",
            "x64",
            "desktop",
            "xtm-one-runner-windows-x64.zip",
        ),
        (
            "windows",
            "arm64",
            "desktop",
            "xtm-one-runner-windows-arm64.zip",
        ),
        (
            "macos",
            "x64",
            "desktop",
            "xtm-one-runner-macos-universal.tar.gz",
        ),
        (
            "macos",
            "arm64",
            "desktop",
            "xtm-one-runner-macos-universal.tar.gz",
        ),
        ("linux", "x64", "desktop", "xtm-one-runner-linux-x64.tar.gz"),
        (
            "linux",
            "arm64",
            "desktop",
            "xtm-one-runner-linux-arm64.tar.gz",
        ),
        (
            "linux",
            "x64",
            "headless",
            "xtm-one-runner-linux-x64-headless.tar.gz",
        ),
        (
            "linux",
            "arm64",
            "headless",
            "xtm-one-runner-linux-arm64-headless.tar.gz",
        ),
    ];

    #[test]
    fn the_self_updater_expects_the_archive_the_platform_serves_this_machine() {
        for (os_name, arch, flavor, archive) in SERVED_ARCHIVES {
            assert_eq!(
                release_archive_name(os_name, arch, flavor).as_deref(),
                Some(*archive),
                "{os_name}/{arch} {flavor}"
            );
        }
        // No headless build ships outside Linux, and nothing for an unknown arch.
        assert_eq!(release_archive_name("windows", "x64", "headless"), None);
        assert_eq!(release_archive_name("macos", "arm64", "headless"), None);
        assert_eq!(release_archive_name("linux", "riscv64", "desktop"), None);
    }

    /// Keys of small order, each with a .minisig "signed" by it over `PAYLOAD`:
    /// `minisign_verify` accepts every one of these forgeries.
    const SMALL_ORDER_FORGERIES: &[(&str, &str)] = &[
        // order 2 (y = p - 1)
        ("RWRzbWFsbG9yZOz///////////////////////////////////////9/", "untrusted comment: a forgery\nRURzbWFsbG9yZN1hoadK/O6otyj6cAw0mepu40aHHo0N2BvqrOQwj678bIr7cGiVN6b5lI/RjPpGbs6OfS8SzHx9Wmy1gd94DwQ=\ntrusted comment: timestamp:1790000000\tfile:fixture.tar.gz\nt9/T0ktWDicb+tZaN7kUSV5sihQuUbSx2d8Zi1LvV0T97BS0ugOqgE/BjXatjgsni+5zDGxj0SMUF8VZ7M5LAg==\n"),
        // order 4 (y = p, the non-canonical 0)
        ("RWRzbWFsbG9yZO3///////////////////////////////////////9/", "untrusted comment: a forgery\nRURzbWFsbG9yZKfOObgC15j0pNEdfqtw+f2kufF6F9p5UBQHuWawLvVhvLUAPnUkBgD26yCOLcpHnrSal8jZa8xQdl8EbaCZsg0=\ntrusted comment: timestamp:1790000000\tfile:fixture.tar.gz\nT2EEB5hy0LJ5SIOVlEbr19iUkXU+cHwoRHsAEuvZnphdlKAgGOC1RWTlXWcTD+8gSek5gUKqcAKEH+IXJ/WtBg==\n"),
        // order 8
        ("RWRzbWFsbG9yZCbolY/CsiewRcP0ifLvmPDV36wF08YzObE4AohtU/wF", "untrusted comment: a forgery\nRURzbWFsbG9yZFFWOadt5U6WqJNESJxLXqkEPZGw3a2qSjFXE57SkQeMFGe7XLJMoEoChj2kfrNq2K29qKEcp/KMLF4sJVbeyAs=\ntrusted comment: timestamp:1790000000\tfile:fixture.tar.gz\nT2EEB5hy0LJ5SIOVlEbr19iUkXU+cHwoRHsAEuvZnphdlKAgGOC1RWTlXWcTD+8gSek5gUKqcAKEH+IXJ/WtBg==\n"),
        // order 8, sign bit set
        ("RWRzbWFsbG9yZMcXanA9TdhPujwLdg0QZw8qIFP6LDnMxk7H/XeSrAP6", "untrusted comment: a forgery\nRURzbWFsbG9yZKLeVoWrvyjOSlBqFXkQvUMf1jCD3OzLR/60CdGnUHsuUricxPuEu59D9KEQOnP2uHjxWF9XMpH2ZGdd5q9UfQU=\ntrusted comment: timestamp:1790000000\tfile:fixture.tar.gz\nU166cNGort6ddcPiZn3aKlrJ6eISGEoH00SPIEPzp0eKLNcVIJe6sYv89n8MzbMvREOT1SA6hafnpAq13iTmAQ==\n"),
        // order 1 (y = p + 1, the non-canonical identity)
        ("RWRzbWFsbG9yZO7///////////////////////////////////////9/", "untrusted comment: a forgery\nRURzbWFsbG9yZE9hBAeYctCyeUiDlZRG69fYlJF1PnB8KER7ABLr2Z6YXZSgIBjgtUVk5V1nEw/vIEnpOYFCqnAChB/iFyf1rQY=\ntrusted comment: timestamp:1790000000\tfile:fixture.tar.gz\nT2EEB5hy0LJ5SIOVlEbr19iUkXU+cHwoRHsAEuvZnphdlKAgGOC1RWTlXWcTD+8gSek5gUKqcAKEH+IXJ/WtBg==\n"),
    ];

    #[test]
    fn a_key_of_small_order_is_refused() {
        for (key, forgery) in SMALL_ORDER_FORGERIES {
            let why = rejected(check(PAYLOAD, forgery, key));
            assert!(why.contains("of small order"), "{key}: {why}");
            assert!(verify_minisign(PAYLOAD, forgery, key).is_err(), "{key}");
        }
    }

    /// `SECOND_KEY` signatures whose R is the identity (a point of small
    /// order): the cofactorless equation holds, so `minisign_verify` accepts
    /// them; libsodium, and the install scripts, refuse them.
    const SIG_IDENTITY_R_FILE: &str = "untrusted comment: an R of small order\nRUSkpNA/D0II3QEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAMW34XAaZSQtl9Icc3xcBl16EP6Yg+NVfwcqj1YOnHws=\ntrusted comment: timestamp:1790000000\tfile:fixture.tar.gz\n95n/W+qDSCslmZD9CVrqUUTQXD6E9BSWnPVf/CJAJGuUb0/n+vxhsVld+0uRNERRGrqNn/Pdya0WtEU5fhnCAw==\n";
    const SIG_IDENTITY_R_GLOBAL: &str = "untrusted comment: an R of small order\nRUSkpNA/D0II3SfiHN41aYmDuhaTOzEh8XDl9zitxxgBh2+M+hr3Vo2Lj3kvhQy98P88tNG2C9bVLcAwsxECJ0ayX3HZevDsLwI=\ntrusted comment: timestamp:1790000000\tfile:fixture.tar.gz\nAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABnZ2b7Bcyn+0DZwC8hqHc5iIzNUgjUQBGV5RMB4rRYBQ==\n";

    #[test]
    fn an_r_of_small_order_is_refused_in_either_signature() {
        for sig in [SIG_IDENTITY_R_FILE, SIG_IDENTITY_R_GLOBAL] {
            let why = rejected(check(PAYLOAD, sig, SECOND_KEY));
            assert!(why.contains("R of small order"), "{why}");
            assert!(verify_minisign(PAYLOAD, sig, SECOND_KEY).is_err());
        }
    }

    #[test]
    fn small_order_is_matched_on_libsodium_encodings_with_the_sign_bit_ignored() {
        let from_hex = |hex_point: &str| -> [u8; 32] {
            hex::decode(hex_point)
                .expect("hex")
                .try_into()
                .expect("32 bytes")
        };
        for point in super::SMALL_ORDER_POINTS {
            let mut signed = from_hex(point);
            assert!(has_small_order(&signed), "{point}");
            signed[31] |= 0x80;
            assert!(has_small_order(&signed), "{point} with the sign bit set");
        }
        let fixture = &base64::Engine::decode(
            &base64::engine::general_purpose::STANDARD,
            FIXTURE_PUBKEY_BARE,
        )
        .expect("fixture key")[10..];
        assert!(!has_small_order(fixture.try_into().expect("32 bytes")));
    }

    #[test]
    fn the_key_id_a_signature_names_selects_a_key_from_the_list() {
        // A rotation is staged: builds trust the next key before the release
        // signs with it, and keep trusting the old one until it is retired.
        let both = format!("{FIXTURE_PUBKEY_BARE},{SECOND_KEY}");
        let reversed = format!(" {SECOND_KEY} , {FIXTURE_PUBKEY_TAURI} ,");
        for keys in [both.as_str(), reversed.as_str()] {
            verified(check(PAYLOAD, SIG_RAW, keys));
            verified(check(PAYLOAD, SIG_SECOND_KEY, keys));
            verify_minisign(PAYLOAD, SIG_SECOND_KEY, keys).expect("the second key verifies");
        }
        let why = rejected(check(PAYLOAD, SIG_SECOND_KEY, FIXTURE_PUBKEY_BARE));
        assert!(why.contains("another key"), "{why}");
        let other = other_key_id();
        let why = rejected(check(PAYLOAD, SIG_RAW, &format!("{SECOND_KEY},{other}")));
        assert!(why.contains("another key"), "{why}");
        // A list is read whole: one entry that is not a key is not skipped.
        assert!(matches!(
            check(
                PAYLOAD,
                SIG_RAW,
                &format!("{FIXTURE_PUBKEY_BARE},not-a-key")
            ),
            SignatureCheck::Unreadable(_)
        ));
        assert!(matches!(
            check(PAYLOAD, SIG_RAW, " , "),
            SignatureCheck::Unreadable(_)
        ));
    }

    #[test]
    fn a_signed_version_must_be_the_one_offered() {
        let offered = |version: &'static str| Requirements {
            file: Some("fixture.tar.gz"),
            version: Some(version),
            timestamp_floor: 0,
        };
        let comment = verified(check_signature(
            PAYLOAD,
            SIG_VERSIONED,
            SECOND_KEY,
            &offered("1.260928.57"),
        ));
        assert_eq!(comment_field(&comment, "version"), Some("1.260928.57"));
        // An older signed build served as the one the platform offers.
        let why = rejected(check_signature(
            PAYLOAD,
            SIG_VERSIONED,
            SECOND_KEY,
            &offered("1.261001.3"),
        ));
        assert_eq!(why, "it was made for runner 1.260928.57, not 1.261001.3");
        // Signed before versions were: accepted as before.
        verified(check_signature(
            PAYLOAD,
            SIG_SECOND_KEY,
            SECOND_KEY,
            &offered("1.261001.3"),
        ));
    }

    #[test]
    fn a_signature_older_than_the_floor_is_refused() {
        let floor = |timestamp_floor: u64| Requirements {
            timestamp_floor,
            ..Requirements::default()
        };
        // SIG_RAW was made at 1790000000.
        verified(check_signature(
            PAYLOAD,
            SIG_RAW,
            FIXTURE_PUBKEY_BARE,
            &floor(0),
        ));
        verified(check_signature(
            PAYLOAD,
            SIG_RAW,
            FIXTURE_PUBKEY_BARE,
            &floor(1_790_000_000),
        ));
        let why = rejected(check_signature(
            PAYLOAD,
            SIG_RAW,
            FIXTURE_PUBKEY_BARE,
            &floor(1_790_000_001),
        ));
        assert!(why.contains("before 1790000001"), "{why}");
        verified(check_signature(
            PAYLOAD,
            SIG_VERSIONED,
            SECOND_KEY,
            &floor(1_790_000_100),
        ));
    }

    #[test]
    fn comment_fields_are_read_as_the_install_scripts_read_them() {
        let comment = "timestamp:1790000100\tfile:a.tar.gz\tversion:1.2.3\tfile:b.tar.gz";
        assert_eq!(comment_field(comment, "timestamp"), Some("1790000100"));
        assert_eq!(comment_field(comment, "file"), Some("a.tar.gz"));
        assert_eq!(comment_field(comment, "version"), Some("1.2.3"));
        assert_eq!(comment_field("release build", "file"), None);
        assert_eq!(comment_field("files:x\tfile", "file"), None);
    }

    #[test]
    fn the_runner_never_rolls_itself_back() {
        refuse_rollback("1.260928.57", "1.260928.56").expect("a newer build");
        refuse_rollback("1.261001.1", "1.260928.57").expect("a newer day");
        refuse_rollback("2", "1.260928.57").expect("a newer major");
        for (offered, current) in [
            ("1.260928.57", "1.260928.57"),
            ("1.260901.3", "1.260928.57"),
            ("1.260928", "1.260928.0"),
        ] {
            let err = refuse_rollback(offered, current).unwrap_err();
            assert!(
                format!("{err:#}").contains("refusing to roll back"),
                "{offered}"
            );
        }
        // Nothing to compare: left to the signature checks, as before.
        refuse_rollback("0.0.0-dev", "1.260928.57").expect("unparsed offer");
        refuse_rollback("1.260901.3", "0.1.0-dev").expect("unparsed current");
    }
}
