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

//! The machine an agent runs on, as the platform names it.
//!
//! * [`detect_platform`] / [`detect_arch`] speak the vocabulary of the update
//!   manifests (`windows` | `macos` | `linux`, `x64` | `arm64`);
//! * [`machine_id`] is the salted, privacy-preserving machine identity an agent
//!   reports at enrollment, so a re-enrollment converges on its existing row.

use std::time::Duration;

use sha2::{Digest, Sha256};
use tokio::process::Command as TokioCommand;

/// The OS family in the platform's vocabulary.
pub fn detect_platform() -> &'static str {
    #[cfg(windows)]
    {
        "windows"
    }
    #[cfg(target_os = "macos")]
    {
        "macos"
    }
    #[cfg(all(not(windows), not(target_os = "macos")))]
    {
        "linux"
    }
}

/// CPU architecture in the vocabulary the update manifest speaks (`x64` |
/// `arm64`). Anything else reports the raw `std::env::consts::ARCH` so the
/// server simply finds no matching artifact rather than mis-mapping it.
pub fn detect_arch() -> &'static str {
    match std::env::consts::ARCH {
        "x86_64" => "x64",
        "aarch64" => "arm64",
        other => other,
    }
}

/// Stable, privacy-preserving machine identity reported at enrollment: a
/// salted SHA-256 of the OS machine id. The server uses it to converge a
/// re-enrollment (reinstall, credential loss, install-script re-run) on the
/// machine's existing row instead of duplicating the fleet list.
///
/// `salt` is the agent's own (`b"xtm-one-runner:"`, `b"xtm-one-relay:"`), so
/// two agents on one machine never report the same id, and the raw OS id never
/// leaves the machine - hashing it with an app-specific salt is the practice
/// `machine-id(5)` recommends. The hostname is deliberately not part of the
/// identity (cloned VM fleets share one). Best-effort: `None` simply enrolls
/// without convergence.
pub async fn machine_id(salt: &[u8]) -> Option<String> {
    let raw = raw_machine_id().await?;
    let raw = raw.trim();
    if raw.is_empty() {
        return None;
    }
    let mut hasher = Sha256::new();
    hasher.update(salt);
    hasher.update(raw.as_bytes());
    Some(hex::encode(hasher.finalize()))
}

#[cfg(windows)]
async fn raw_machine_id() -> Option<String> {
    // HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid: set once at OS
    // install. Read via reg.exe (always present, headless through `probe`)
    // to avoid a registry crate for a single value. The value NAME is not
    // localized, so the line match is locale-safe.
    let out = probe(
        "reg",
        &[
            "query",
            r"HKLM\SOFTWARE\Microsoft\Cryptography",
            "/v",
            "MachineGuid",
        ],
    )
    .await?;
    out.lines()
        .find(|l| l.contains("MachineGuid"))
        .and_then(|l| l.split_whitespace().last())
        .map(str::to_string)
}

#[cfg(target_os = "macos")]
async fn raw_machine_id() -> Option<String> {
    // IOPlatformUUID from the IOKit registry: stable for the machine's
    // lifetime, survives reinstalls, renames, and OS updates.
    let out = probe("ioreg", &["-rd1", "-c", "IOPlatformExpertDevice"]).await?;
    out.lines()
        .find(|l| l.contains("IOPlatformUUID"))
        .and_then(|l| l.rsplit('"').nth(1))
        .map(str::to_string)
}

#[cfg(all(not(windows), not(target_os = "macos")))]
async fn raw_machine_id() -> Option<String> {
    // systemd machine-id, with the dbus path as the pre-systemd fallback.
    ["/etc/machine-id", "/var/lib/dbus/machine-id"]
        .iter()
        .find_map(|p| std::fs::read_to_string(p).ok())
}

/// Run a command with a short timeout and return trimmed stdout on success.
pub async fn probe(program: &str, args: &[&str]) -> Option<String> {
    let mut cmd = TokioCommand::new(program);
    cmd.args(args).kill_on_drop(true);
    // Never flash a console window for these background probes on Windows.
    crate::proc::no_window(&mut cmd);
    let out = tokio::time::timeout(Duration::from_secs(5), cmd.output())
        .await
        .ok()?
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if text.is_empty() {
        None
    } else {
        Some(text)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_platform_and_arch_speak_the_manifest_vocabulary() {
        assert!(["windows", "macos", "linux"].contains(&detect_platform()));
        if cfg!(target_arch = "x86_64") {
            assert_eq!(detect_arch(), "x64");
        }
        if cfg!(target_arch = "aarch64") {
            assert_eq!(detect_arch(), "arm64");
        }
    }

    #[tokio::test]
    async fn two_salts_never_report_the_same_machine() {
        let (Some(runner), Some(relay)) = (
            machine_id(b"xtm-one-runner:").await,
            machine_id(b"xtm-one-relay:").await,
        ) else {
            // A host without a machine id (a bare container) enrolls without
            // convergence; nothing to compare.
            return;
        };
        assert_eq!(runner.len(), 64);
        assert_ne!(runner, relay);
    }
}
