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

//! Verifying a release before an agent replaces itself with it.
//!
//! An agent downloads its next build from the platform it is enrolled against:
//! the archive plus its `.sha256` and `.minisig`. BOTH are verified - the
//! checksum for integrity, the minisign signature against the compiled-in
//! release keys for authenticity - so the network is never trusted to hand an
//! agent code.
//!
//! The `.minisig` the pipeline stages is minisign's own four-line text; the
//! verifier also accepts the base64-wrapped form `tauri signer sign` writes to
//! `.sig`, because that is what every older runner release served under
//! the `.minisig` name (see [`decode_signature_forms`]).
//!
//! A signature is checked the way the install scripts check it: the
//! key id it names selects one of the compiled-in release keys (so a key
//! rotation can be staged), a key or an R of small order is refused, and its
//! trusted comment must name the archive this machine takes (`file:`), the
//! version the platform offered (`version:`) and a `timestamp:` no older than
//! the release floor. A version the comment does not carry is not checked, so
//! archives signed before release signatures named their version keep
//! verifying; and an agent never replaces itself with a build that is not newer
//! than itself.
//!
//! `subject` - the agent's own name, `runner` or `relay` - is what the
//! refusals call it.

use std::path::{Path, PathBuf};

use anyhow::{anyhow, Context};

/// Check an archive against its `.sha256` file.
pub fn verify_sha256(bytes: &[u8], sha_file: &str) -> anyhow::Result<()> {
    use sha2::{Digest, Sha256};

    // A `.sha256` file is either a bare hex digest or the `sha256sum` form
    // "<hex>  <filename>"; take the first whitespace-delimited token.
    let expected = sha_file
        .split_whitespace()
        .next()
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase();
    if expected.len() != 64 || !expected.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(anyhow!("malformed .sha256 file"));
    }
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    let actual = hex::encode(hasher.finalize());
    if actual != expected {
        return Err(anyhow!(
            "archive SHA-256 mismatch (expected {expected}, computed {actual})"
        ));
    }
    Ok(())
}

/// Verify an update the way [`check_signature`] does; `Ok` carries the signed
/// trusted comment.
pub fn verify_minisign(
    subject: &str,
    bytes: &[u8],
    signature: &str,
    pubkeys: &str,
    requirements: &Requirements,
) -> anyhow::Result<String> {
    match check_signature(subject, bytes, signature, pubkeys, requirements) {
        SignatureCheck::Verified { trusted_comment } => Ok(trusted_comment),
        SignatureCheck::Rejected(why) => Err(anyhow!("minisign signature did not verify: {why}")),
        SignatureCheck::Unreadable(why) => Err(anyhow!(why)),
    }
}

/// The oldest release-signature `timestamp:` a build accepts, from the value
/// compiled into it (`None` / blank: 0, no floor).
pub fn signature_floor(compiled: Option<&str>) -> Result<u64, String> {
    match compiled.map(str::trim) {
        None | Some("") => Ok(0),
        Some(value) => value
            .parse()
            .map_err(|_| format!("invalid compiled-in signature floor {value:?}")),
    }
}

/// A `MAJOR.YYMMDD.SEQ` (any dotted-integer) version, as the platform parses one.
pub fn parse_version(value: &str) -> Option<Vec<u64>> {
    let parts = value
        .trim()
        .split('.')
        .map(|part| {
            if !part.is_empty() && part.bytes().all(|b| b.is_ascii_digit()) {
                part.parse().ok()
            } else {
                None
            }
        })
        .collect::<Option<Vec<u64>>>()?;
    Some(parts)
}

/// Refuse to replace this agent with a build that is provably not newer: the
/// platform only offers newer ones, so anything else is a rollback. A version
/// that does not parse is left to the signature checks, as before.
pub fn refuse_rollback(subject: &str, offered: &str, current: &str) -> anyhow::Result<()> {
    let (Some(mut offered_parts), Some(mut current_parts)) =
        (parse_version(offered), parse_version(current))
    else {
        return Ok(());
    };
    let width = offered_parts.len().max(current_parts.len());
    offered_parts.resize(width, 0);
    current_parts.resize(width, 0);
    if offered_parts > current_parts {
        return Ok(());
    }
    Err(anyhow!(
        "the platform offers {subject} {offered}, which is not newer than this {subject} \
         ({current}): refusing to roll back"
    ))
}

/// Decode a detached signature, whichever form it was published in; `Ok` also
/// carries the four-line text it was decoded from.
///
/// Two shapes exist and BOTH must parse, for the same reason as the public key
/// below:
///   * the raw minisign text - four lines, `untrusted comment:` first - which
///     is what `minisign_verify::Signature::decode` expects and what the release
///     pipeline stages as `.minisig`;
///   * the Tauri convention - base64 of that ENTIRE text, which is what
///     `tauri signer sign` writes to `.sig` (the form `latest.json` carries and
///     the bundle updater decodes itself) and what every older runner release
///     served as `.minisig`, so an air-gapped mirror populated from one
///     of those still verifies.
///
/// Feeding the second form straight to `Signature::decode` fails with
/// `Invalid encoding in minisign data`, which is exactly what kept every
/// headless / service runner install on its first-installed build: the
/// check found the update, the download and the checksum passed, and
/// verification refused it every 6 hours, forever.
fn decode_signature_forms(
    raw: &str,
) -> Result<(minisign_verify::Signature, String), minisign_verify::Error> {
    use base64::Engine as _;
    use minisign_verify::Signature;

    let trimmed = raw.trim();
    let raw_err = match Signature::decode(trimmed) {
        Ok(sig) => return Ok((sig, trimmed.to_string())),
        Err(e) => e,
    };
    let text = base64::engine::general_purpose::STANDARD
        .decode(trimmed)
        .ok()
        .and_then(|decoded| String::from_utf8(decoded).ok())
        .ok_or(raw_err)?;
    let text = text.trim();
    Signature::decode(text).map(|sig| (sig, text.to_string()))
}

/// What `minisign_verify` keeps to itself about a signature it decoded: the key
/// id it names and the R of both signatures.
struct SignatureParts {
    key_id: [u8; 8],
    file_r: [u8; 32],
    global_r: [u8; 32],
}

/// Read the parts of a signature text `minisign_verify::Signature::decode`
/// accepted, line for line as it read them.
fn signature_parts(text: &str) -> Option<SignatureParts> {
    use base64::Engine as _;
    let b64 = base64::engine::general_purpose::STANDARD;
    let mut lines = text.lines();
    lines.next()?;
    let signature = b64.decode(lines.next()?).ok()?;
    lines.next()?;
    let global = b64.decode(lines.next()?).ok()?;
    if signature.len() != 74 || global.len() != 64 {
        return None;
    }
    Some(SignatureParts {
        key_id: signature[2..10].try_into().ok()?,
        file_r: signature[10..42].try_into().ok()?,
        global_r: global[..32].try_into().ok()?,
    })
}

/// libsodium's encodings of points of small order (`ge25519_has_small_order`),
/// matched with the sign bit ignored, as install.sh and install.ps1 match them.
pub const SMALL_ORDER_POINTS: [&str; 7] = [
    "0000000000000000000000000000000000000000000000000000000000000000",
    "0100000000000000000000000000000000000000000000000000000000000000",
    "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
    "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",
    "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
];

/// A key of small order verifies forged signatures, and `minisign_verify` only
/// refuses two of them; an R of small order is one no genuine signer makes.
pub fn has_small_order(point: &[u8; 32]) -> bool {
    let mut masked = *point;
    masked[31] &= 0x7f;
    SMALL_ORDER_POINTS.contains(&hex::encode(masked).as_str())
}

/// The value of the first `name:` field of a trusted comment
/// (`timestamp:<t>\tfile:<name>\tversion:<v>`), as the install scripts read it.
pub fn comment_field<'a>(comment: &'a str, name: &str) -> Option<&'a str> {
    comment
        .split('\t')
        .find_map(|field| field.strip_prefix(name)?.strip_prefix(':'))
}

/// What a signature must say about itself on top of verifying. A
/// version its trusted comment does not carry is not checked, so an archive
/// signed before release signatures named their version keeps verifying; a
/// missing file, or a missing timestamp while a floor is set, is refused.
#[derive(Debug, Default)]
pub struct Requirements<'a> {
    /// The archive the `file:` field must name.
    pub file: Option<&'a str>,
    /// The agent version the `version:` field must be.
    pub version: Option<&'a str>,
    /// The oldest `timestamp:` (Unix seconds) accepted; 0 for none.
    pub timestamp_floor: u64,
}

/// Why a verified trusted comment does not meet `requirements`, if it does not.
fn requirement_refusal(
    subject: &str,
    comment: &str,
    requirements: &Requirements,
) -> Option<String> {
    if let Some(expected) = requirements.file {
        match comment_field(comment, "file") {
            Some(named) if named == expected => {}
            Some(named) => return Some(format!("it was made for {named}, not {expected}")),
            None => {
                return Some(format!(
                    "it names no file, so nothing ties it to {expected}"
                ))
            }
        }
    }
    if let (Some(expected), Some(signed)) =
        (requirements.version, comment_field(comment, "version"))
    {
        if signed != expected {
            return Some(format!(
                "it was made for {subject} {signed}, not {expected}"
            ));
        }
    }
    let floor = requirements.timestamp_floor;
    if floor > 0 {
        match comment_field(comment, "timestamp") {
            Some(stamp) => match stamp.parse::<u64>() {
                Ok(time) if time >= floor => {}
                Ok(time) => {
                    return Some(format!(
                        "it was made at {time}, before {floor}, the oldest release \
                         signature this build accepts"
                    ))
                }
                Err(_) => {
                    return Some(format!(
                        "its timestamp {stamp:?} is not a time, so nothing says it is \
                         newer than {floor}"
                    ))
                }
            },
            None => {
                return Some(format!(
                    "it names no timestamp, so nothing says it is newer than {floor}"
                ))
            }
        }
    }
    None
}

/// What a check concluded about a file and its detached signature.
#[derive(Debug)]
pub enum SignatureCheck {
    /// Key id, signature and trusted-comment signature all verify, and the
    /// trusted comment meets the requirements.
    Verified { trusted_comment: String },
    /// A minisign signature that does not hold for this file under these keys,
    /// or that was not made for what is required.
    Rejected(String),
    /// Nothing that can be checked: a key or the signature does not parse.
    Unreadable(String),
}

/// Check `bytes` against a detached minisign `signature` under `pubkeys`, with
/// the rules [`verify_minisign`] applies to an update: either published
/// signature form, either key form, prehashed ("ED") signatures only, the key
/// selected by the key id the signature names, no key or R of small order, and
/// a trusted comment that meets `requirements`.
///
/// Where [`verify_minisign`] only says no, this tells a signature that does not
/// verify apart from input that cannot be checked at all: the install scripts
/// refuse the first and warn about the second.
pub fn check_signature(
    subject: &str,
    bytes: &[u8],
    signature: &str,
    pubkeys: &str,
    requirements: &Requirements,
) -> SignatureCheck {
    use minisign_verify::Error;

    let rejected = |why: &str| SignatureCheck::Rejected(why.to_string());
    let keys = match parse_trusted_keys(pubkeys) {
        Ok(keys) => keys,
        Err(e) => return SignatureCheck::Unreadable(format!("{e:#}")),
    };
    let (sig, text) = match decode_signature_forms(signature) {
        Ok(decoded) => decoded,
        // Four well-formed lines naming an algorithm minisign does not define.
        Err(Error::UnsupportedAlgorithm) => {
            return rejected("it names a signature algorithm minisign does not define")
        }
        Err(e) => {
            return SignatureCheck::Unreadable(format!("invalid minisign signature file: {e}"))
        }
    };
    let Some(parts) = signature_parts(&text) else {
        return SignatureCheck::Unreadable(
            "invalid minisign signature file: Invalid encoding in minisign data".to_string(),
        );
    };
    let Some(key) = keys.iter().find(|key| key.key_id == parts.key_id) else {
        return rejected("it was made with another key");
    };
    if has_small_order(&key.key) {
        return rejected("the key it names is of small order, which verifies forged signatures");
    }
    if has_small_order(&parts.file_r) || has_small_order(&parts.global_r) {
        return rejected("it carries an R of small order, which no genuine signature has");
    }
    match key.verifier.verify(bytes, &sig, false) {
        Ok(()) => {}
        Err(Error::UnexpectedKeyId) => return rejected("it was made with another key"),
        Err(Error::UnexpectedAlgorithm) => {
            return SignatureCheck::Rejected(format!(
                "it is a legacy (not prehashed) signature, which the {subject} does not accept"
            ))
        }
        Err(_) => {
            return rejected("the file or its trusted comment was changed after it was signed")
        }
    }
    let trusted_comment = sig.trusted_comment().to_string();
    if let Some(why) = requirement_refusal(subject, &trusted_comment, requirements) {
        return SignatureCheck::Rejected(why);
    }
    SignatureCheck::Verified { trusted_comment }
}

/// A release key: its key id and Ed25519 key, which `minisign_verify` keeps to
/// itself, next to its verifier.
pub struct TrustedKey {
    key_id: [u8; 8],
    key: [u8; 32],
    verifier: minisign_verify::PublicKey,
}

fn trusted_key(line: &str) -> Option<TrustedKey> {
    use base64::Engine as _;

    let verifier = minisign_verify::PublicKey::from_base64(line).ok()?;
    let raw = base64::engine::general_purpose::STANDARD
        .decode(line)
        .ok()?;
    if raw.len() != 42 {
        return None;
    }
    Some(TrustedKey {
        key_id: raw[2..10].try_into().ok()?,
        key: raw[10..42].try_into().ok()?,
        verifier,
    })
}

/// Parse the release keys: comma-separated, each in either form of
/// [`parse_minisign_pubkey`]. The key id a signature names picks the one that
/// checks it (the first with that id), so the list can carry the next key
/// before the release signs with it.
fn parse_trusted_keys(raw: &str) -> anyhow::Result<Vec<TrustedKey>> {
    let keys = raw
        .split(',')
        .map(str::trim)
        .filter(|entry| !entry.is_empty())
        .map(parse_minisign_pubkey)
        .collect::<anyhow::Result<Vec<_>>>()?;
    if keys.is_empty() {
        return Err(anyhow!("invalid minisign public key: none given"));
    }
    Ok(keys)
}

/// Parse one minisign public key, whichever form it was given in.
///
/// Two shapes exist and BOTH must parse, because a key reaches an agent in
/// both (CI bakes the key lines of the agent's `release-signing.json`, and a
/// fork may bake `tauri.conf.json`'s `plugins.updater.pubkey` verbatim):
///   * the bare key data - the second line of a minisign `.pub` file, which is
///     what `minisign_verify::PublicKey::from_base64` natively expects, and
///   * the Tauri convention - base64 of the ENTIRE `.pub` file, untrusted
///     comment line included (what `tauri signer generate` prints and what
///     lives in `tauri.conf.json`).
///
/// Feeding the second form straight to `from_base64` fails, which would brick
/// the bare-binary self-updater on every production build, so fall back to
/// decoding the blob and scanning its lines for the key data.
pub fn parse_minisign_pubkey(raw: &str) -> anyhow::Result<TrustedKey> {
    use base64::Engine as _;

    let trimmed = raw.trim();
    if let Some(key) = trusted_key(trimmed) {
        return Ok(key);
    }
    let decoded = base64::engine::general_purpose::STANDARD
        .decode(trimmed)
        .map_err(|e| anyhow!("invalid minisign public key: {e}"))?;
    let text = String::from_utf8(decoded)
        .map_err(|_| anyhow!("invalid minisign public key: not UTF-8"))?;
    text.lines()
        .find_map(|line| trusted_key(line.trim()))
        .ok_or_else(|| anyhow!("invalid minisign public key: no key data line found"))
}

/// The binary an archive must hold: written out as `bin_name`, found under it
/// or one of the agent's own names (`accepted`).
pub struct ArchiveBinary<'a> {
    /// What the refusals call the agent (`runner`, `relay`).
    pub subject: &'a str,
    /// The name it is written out as (the running executable's).
    pub bin_name: &'a str,
    /// The other entry names that are the agent's binary.
    pub accepted: &'a [&'a str],
}

impl ArchiveBinary<'_> {
    /// True when a tar/zip entry's basename is the binary we want.
    fn matches(&self, entry_name: &str) -> bool {
        let base = entry_name.rsplit(['/', '\\']).next().unwrap_or(entry_name);
        base == self.bin_name || self.accepted.contains(&base)
    }
}

/// Extract the agent binary from a `.zip` (Windows) or `.tar.gz` (unix) archive.
pub fn extract_binary(
    archive: &[u8],
    url: &str,
    binary: &ArchiveBinary,
    out_dir: &Path,
) -> anyhow::Result<PathBuf> {
    if url.ends_with(".zip") {
        extract_from_zip(archive, binary, out_dir)
    } else if url.ends_with(".tar.gz") || url.ends_with(".tgz") {
        extract_from_targz(archive, binary, out_dir)
    } else {
        Err(anyhow!("unsupported archive type for {url}"))
    }
}

fn extract_from_zip(
    bytes: &[u8],
    binary: &ArchiveBinary,
    out_dir: &Path,
) -> anyhow::Result<PathBuf> {
    use std::io::{Cursor, Read, Write};

    let bin_name = binary.bin_name;
    let mut zip = zip::ZipArchive::new(Cursor::new(bytes)).context("opening the zip archive")?;
    for i in 0..zip.len() {
        let mut entry = zip.by_index(i).context("reading a zip entry")?;
        if !entry.is_file() {
            continue;
        }
        let name = entry
            .name()
            .context("reading a zip entry name")?
            .into_owned();
        if binary.matches(&name) {
            let out = out_dir.join(bin_name);
            let mut buf = Vec::with_capacity(entry.size() as usize);
            entry.read_to_end(&mut buf).context("reading zip entry")?;
            let mut file = std::fs::File::create(&out).context("writing extracted binary")?;
            file.write_all(&buf).context("writing extracted binary")?;
            return Ok(out);
        }
    }
    Err(anyhow!(
        "{} binary '{bin_name}' not found in the zip archive",
        binary.subject
    ))
}

fn extract_from_targz(
    bytes: &[u8],
    binary: &ArchiveBinary,
    out_dir: &Path,
) -> anyhow::Result<PathBuf> {
    use std::io::{Cursor, Read, Write};

    let bin_name = binary.bin_name;
    let gz = flate2::read::GzDecoder::new(Cursor::new(bytes));
    let mut tar = tar::Archive::new(gz);
    for entry in tar.entries().context("reading the tar.gz archive")? {
        let mut entry = entry.context("reading a tar entry")?;
        if !entry.header().entry_type().is_file() {
            continue;
        }
        let path = entry
            .path()
            .context("reading a tar entry path")?
            .to_path_buf();
        let name = path.to_string_lossy();
        if binary.matches(&name) {
            let out = out_dir.join(bin_name);
            let mut buf = Vec::new();
            entry.read_to_end(&mut buf).context("reading tar entry")?;
            let mut file = std::fs::File::create(&out).context("writing extracted binary")?;
            file.write_all(&buf).context("writing extracted binary")?;
            return Ok(out);
        }
    }
    Err(anyhow!(
        "{} binary '{bin_name}' not found in the tar.gz archive",
        binary.subject
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    // The runner's throwaway fixture key and signature (apps/runner/src/update.rs
    // carries the full suite; these pin what the subject changes).
    const PAYLOAD: &[u8] = b"xtm-one-runner self-update fixture payload (#3743)\n";
    const KEY: &str = "RWSRNhxpZP3VeNdBcMrZ70t+ks6N46DKBb15Q9yCGtYii+76LXWRTd7T";
    const SIG: &str = "untrusted comment: signature from xtm-one-runner test key\nRUSRNhxpZP3VePJ9Gt9CA/TxR2O4oPCtO+vp0gPyMUuTHLwleciB4JQoQFkyfAEAqKvv/4uCXCk1p6SLNm+S9YkD2H3+J9DKiw8=\ntrusted comment: timestamp:1790000000\tfile:fixture.tar.gz\nTEKrt719BBzN00ESdP6yW2DruNkJT1VDUmEae/USsn3IZgs0fPyo8EMEjIbyfPxzudZZA0pwtIK9UD8mT6wyCA==\n";

    #[test]
    fn a_signature_verifies_whoever_checks_it() {
        let comment = verify_minisign("relay", PAYLOAD, SIG, KEY, &Requirements::default())
            .expect("the fixture verifies");
        assert_eq!(comment_field(&comment, "file"), Some("fixture.tar.gz"));
    }

    #[test]
    fn a_wrong_file_is_refused() {
        let requirements = Requirements {
            file: Some("xtm-one-relay-linux-x64.tar.gz"),
            ..Requirements::default()
        };
        let err = verify_minisign("relay", PAYLOAD, SIG, KEY, &requirements).unwrap_err();
        assert!(
            format!("{err:#}").contains("made for fixture.tar.gz, not xtm-one-relay-linux-x64"),
            "{err:#}"
        );
    }

    #[test]
    fn the_rollback_refusal_names_the_agent() {
        let err = refuse_rollback("relay", "1.2.3", "1.2.3").unwrap_err();
        assert_eq!(
            format!("{err:#}"),
            "the platform offers relay 1.2.3, which is not newer than this relay (1.2.3): \
             refusing to roll back"
        );
        refuse_rollback("relay", "1.2.4", "1.2.3").expect("newer");
    }

    #[test]
    fn the_signature_floor_reads_the_compiled_value() {
        assert_eq!(signature_floor(None), Ok(0));
        assert_eq!(signature_floor(Some(" ")), Ok(0));
        assert_eq!(signature_floor(Some("1790000000")), Ok(1_790_000_000));
        assert!(signature_floor(Some("soon")).is_err());
    }

    fn targz(entries: &[(&str, &[u8])]) -> Vec<u8> {
        let mut builder = tar::Builder::new(flate2::write::GzEncoder::new(
            Vec::new(),
            flate2::Compression::fast(),
        ));
        for (name, data) in entries {
            let mut header = tar::Header::new_gnu();
            header.set_size(data.len() as u64);
            header.set_mode(0o755);
            header.set_cksum();
            builder.append_data(&mut header, name, *data).unwrap();
        }
        builder.into_inner().unwrap().finish().unwrap()
    }

    fn zipped(entries: &[(&str, &[u8])]) -> Vec<u8> {
        use std::io::Write;

        let mut writer = zip::ZipWriter::new(std::io::Cursor::new(Vec::new()));
        for (name, data) in entries {
            writer
                .start_file(*name, zip::write::SimpleFileOptions::default())
                .unwrap();
            writer.write_all(data).unwrap();
        }
        writer.finish().unwrap().into_inner()
    }

    #[test]
    fn the_archive_yields_the_agent_s_own_binary_only() {
        let dir = std::env::temp_dir().join(format!("agent-core-extract-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let binary = ArchiveBinary {
            subject: "relay",
            bin_name: "relay-under-test",
            accepted: &["xtm-one-relay", "xtm-one-relay.exe"],
        };
        let archive = targz(&[
            ("README.txt", b"read me"),
            ("xtm-one-relay-linux-x64/xtm-one-relay", b"the binary"),
        ]);
        let out = extract_binary(&archive, "x.tar.gz", &binary, &dir).unwrap();
        assert_eq!(std::fs::read(&out).unwrap(), b"the binary");

        let runner_only = targz(&[("xtm-one-runner", b"another agent")]);
        let err = extract_binary(&runner_only, "x.tar.gz", &binary, &dir).unwrap_err();
        assert_eq!(
            format!("{err:#}"),
            "relay binary 'relay-under-test' not found in the tar.gz archive"
        );
        assert!(extract_binary(&archive, "x.rar", &binary, &dir).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_windows_archive_yields_the_agent_s_own_binary_only() {
        let dir = std::env::temp_dir().join(format!("agent-core-unzip-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let binary = ArchiveBinary {
            subject: "relay",
            bin_name: "relay-under-test.exe",
            accepted: &["xtm-one-relay", "xtm-one-relay.exe"],
        };
        let archive = zipped(&[
            ("README.txt", b"read me"),
            ("xtm-one-relay-windows-x64/xtm-one-relay.exe", b"the binary"),
        ]);
        let out = extract_binary(&archive, "x.zip", &binary, &dir).unwrap();
        assert_eq!(out, dir.join("relay-under-test.exe"));
        assert_eq!(std::fs::read(&out).unwrap(), b"the binary");

        let runner_only = zipped(&[("xtm-one-runner.exe", b"another agent")]);
        let err = extract_binary(&runner_only, "x.zip", &binary, &dir).unwrap_err();
        assert_eq!(
            format!("{err:#}"),
            "relay binary 'relay-under-test.exe' not found in the zip archive"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }
}
