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
//! The relay converges on the build its platform ships: `GET /update-manifest`
//! with this machine's `os`, `arch` and version advertises the relay build the
//! platform image embeds. When it is newer, the relay downloads the archive,
//! its `.sha256` and its `.minisig`, verifies BOTH (`agent_core::release`:
//! checksum for integrity, minisign against the release keys compiled into
//! this build for authenticity, the archive and the version the signature must
//! name), extracts its binary and swaps the running executable. A build with no
//! release key detects updates but never applies one; a container never applies
//! one (it is updated by pulling its image).

use std::time::Duration;

use serde_json::Value;

use crate::api::PlatformApi;

/// What a check concluded.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum UpdateOutcome {
    /// Nothing newer is offered.
    UpToDate,
    /// A newer build is offered but was not applied, and why.
    Available { version: String, reason: String },
    /// The newer build replaced this executable; restart to run it.
    #[cfg_attr(not(feature = "self-update"), allow(dead_code))]
    Applied(String),
    /// Applying it failed.
    Failed(String),
}

/// The stable name of this machine's release archive (`_RELAY_ARCHIVES` in
/// `backend/app/api/v1/relays.py`); what its signature must name.
#[cfg_attr(not(feature = "self-update"), allow(dead_code))]
pub fn archive_name(os: &str, arch: &str) -> Option<String> {
    match (os, arch) {
        ("windows", "x64" | "arm64") => Some(format!("xtm-one-relay-windows-{arch}.zip")),
        // One universal binary covers both Mac architectures.
        ("macos", "x64" | "arm64") => Some("xtm-one-relay-macos-universal.tar.gz".to_string()),
        ("linux", "x64" | "arm64") => Some(format!("xtm-one-relay-linux-{arch}.tar.gz")),
        _ => None,
    }
}

/// How often the relay asks for a newer build.
pub const CHECK_INTERVAL: Duration = Duration::from_secs(6 * 3600);

pub struct Updater {
    api: PlatformApi,
    auto: bool,
    busy: tokio::sync::Mutex<()>,
}

impl Updater {
    pub fn new(api: PlatformApi, auto: bool) -> Self {
        Self {
            api,
            auto,
            busy: tokio::sync::Mutex::new(()),
        }
    }

    /// Ask the platform for a newer build and apply it when allowed.
    pub async fn check_and_apply(&self) -> UpdateOutcome {
        let _guard = self.busy.lock().await;
        let os = agent_core::platform::detect_platform();
        let arch = agent_core::platform::detect_arch();
        let manifest = match self.api.update_manifest(os, arch).await {
            Ok(manifest) => manifest,
            Err(e) => return UpdateOutcome::Failed(format!("reading the update manifest: {e:#}")),
        };
        let target = manifest.get("target").cloned().unwrap_or(Value::Null);
        let offered = |key: &str| target.get(key).and_then(Value::as_bool).unwrap_or(false);
        if !offered("update_available") {
            return if offered("needs_update") {
                UpdateOutcome::Available {
                    version: "?".into(),
                    reason: "the platform requires a newer relay but serves no signed build \
                             for this machine; install it by hand"
                        .into(),
                }
            } else {
                UpdateOutcome::UpToDate
            };
        }
        let text = |key: &str| target.get(key).and_then(Value::as_str).map(str::to_string);
        let (Some(version), Some(url), Some(sha256_url), Some(signature_url)) = (
            text("version"),
            text("url"),
            text("sha256_url"),
            text("signature_url"),
        ) else {
            return UpdateOutcome::Failed("the manifest offers an incomplete build".into());
        };
        if !self.auto {
            return UpdateOutcome::Available {
                version,
                reason: if crate::config::in_container() {
                    "containers are updated by pulling the new image".into()
                } else {
                    "auto_update is off".into()
                },
            };
        }
        self.apply(os, arch, version, url, sha256_url, signature_url)
            .await
    }

    #[cfg(not(feature = "self-update"))]
    async fn apply(
        &self,
        _os: &str,
        _arch: &str,
        version: String,
        _url: String,
        _sha256_url: String,
        _signature_url: String,
    ) -> UpdateOutcome {
        UpdateOutcome::Available {
            version,
            reason: "this build cannot update itself".into(),
        }
    }

    #[cfg(feature = "self-update")]
    async fn apply(
        &self,
        os: &str,
        arch: &str,
        version: String,
        url: String,
        sha256_url: String,
        signature_url: String,
    ) -> UpdateOutcome {
        use agent_core::release::{
            extract_binary, refuse_rollback, signature_floor, verify_minisign, verify_sha256,
            ArchiveBinary, Requirements,
        };
        use tracing::{info, warn};

        use crate::version::{AGENT_VERSION, MINISIGN_PUBKEY, SIGNATURE_FLOOR, SUBJECT};

        let Some(pubkeys) = MINISIGN_PUBKEY.map(str::trim).filter(|k| !k.is_empty()) else {
            return UpdateOutcome::Available {
                version,
                reason: "this build carries no release key, so it never applies an update".into(),
            };
        };
        let Some(archive) = archive_name(os, arch) else {
            return UpdateOutcome::Failed(format!("no relay release for {os}/{arch}"));
        };
        let result: anyhow::Result<()> = async {
            refuse_rollback(SUBJECT, &version, AGENT_VERSION)?;
            let floor = signature_floor(SIGNATURE_FLOOR).map_err(anyhow::Error::msg)?;
            info!("downloading relay {version}");
            let bytes = self.api.download(&url, 256 * 1024 * 1024).await?;
            let sha = self.api.download(&sha256_url, 4096).await?;
            let signature = self.api.download(&signature_url, 16 * 1024).await?;
            verify_sha256(&bytes, &String::from_utf8_lossy(&sha))?;
            verify_minisign(
                SUBJECT,
                &bytes,
                &String::from_utf8_lossy(&signature),
                pubkeys,
                &Requirements {
                    file: Some(&archive),
                    version: Some(&version),
                    timestamp_floor: floor,
                },
            )?;
            let exe = std::env::current_exe()?;
            let bin_name = exe
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or("xtm-one-relay")
                .to_string();
            let tmp =
                std::env::temp_dir().join(format!("xtm-one-relay-update-{}", std::process::id()));
            std::fs::create_dir_all(&tmp)?;
            let extracted = extract_binary(
                &bytes,
                &url,
                &ArchiveBinary {
                    subject: SUBJECT,
                    bin_name: &bin_name,
                    accepted: &["xtm-one-relay", "xtm-one-relay.exe"],
                },
                &tmp,
            );
            let new_bin = match extracted {
                Ok(path) => path,
                Err(e) => {
                    let _ = std::fs::remove_dir_all(&tmp);
                    return Err(e);
                }
            };
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let _ = std::fs::set_permissions(&new_bin, std::fs::Permissions::from_mode(0o755));
            }
            let swapped = self_replace::self_replace(&new_bin).map_err(|e| {
                anyhow::anyhow!(
                    "replacing {}: {e} (the account running the relay must own its binary)",
                    exe.display()
                )
            });
            let _ = std::fs::remove_dir_all(&tmp);
            swapped
        }
        .await;
        match result {
            Ok(()) => {
                info!("relay updated to {version}; restarting");
                UpdateOutcome::Applied(version)
            }
            Err(e) => {
                warn!("relay update to {version} refused: {e:#}");
                UpdateOutcome::Failed(format!("{e:#}"))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_archive_names_are_the_platform_s() {
        assert_eq!(
            archive_name("windows", "x64").as_deref(),
            Some("xtm-one-relay-windows-x64.zip")
        );
        assert_eq!(
            archive_name("windows", "arm64").as_deref(),
            Some("xtm-one-relay-windows-arm64.zip")
        );
        assert_eq!(
            archive_name("macos", "arm64").as_deref(),
            Some("xtm-one-relay-macos-universal.tar.gz")
        );
        assert_eq!(
            archive_name("linux", "x64").as_deref(),
            Some("xtm-one-relay-linux-x64.tar.gz")
        );
        assert_eq!(archive_name("linux", "riscv64"), None);
    }
}
