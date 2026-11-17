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

//! Storage of the durable `run-` credential.
//!
//! Preferred backend is OS-native: DPAPI (Windows), Keychain (macOS) or the
//! Secret Service / libsecret (Linux) via the `keyring` crate. The credential
//! is written once at enrollment and read on every start; it never lands in the
//! config file or a log line.
//!
//! Fallback: a headless Linux server usually runs no Secret Service at all, and
//! there the keyring call fails outright - which used to make `enroll` die on a
//! box where everything else worked. So when the OS keychain is
//! unavailable we fall back to a 0600 file in the runner's own config dir, and
//! say so loudly.
//!
//! The mechanism is shared with the relay ([`agent_core::credential`]); what is
//! the runner's own is where it files the credential: the `xtm-one-runner`
//! keychain service and the runner's config directory, both unchanged so an
//! installed runner keeps finding the credential it enrolled with.
//!
//! XTM One Desktop keeps the chat window's web session next to it, under an
//! account of its own: the webview's storage is a cache that a torn
//! write rolls back at every launch, the keychain is not.

use agent_core::credential::CredentialStore;
use anyhow::Result;

use crate::config::RunnerConfig;

const SERVICE: &str = "xtm-one-runner";

const STORE: CredentialStore = CredentialStore::new(SERVICE, RunnerConfig::config_dir);

/// Where the fallback credential lives for a runner id.
#[cfg(test)]
fn fallback_path(runner_id: &str) -> Result<std::path::PathBuf> {
    STORE.fallback_path(runner_id)
}

/// Persist the `run-` credential for a runner id.
pub fn store(runner_id: &str, token: &str) -> Result<()> {
    STORE.store(runner_id, token)
}

/// Load the `run-` credential for a runner id, or `None` when absent.
///
/// Both backends are consulted: a runner enrolled before a keychain appeared
/// (or after one disappeared) must keep starting.
pub fn load(runner_id: &str) -> Result<Option<String>> {
    STORE.load(runner_id)
}

/// Delete the stored credential (used by `reset`), and the web session kept
/// with it. Missing is not an error.
///
/// Clears BOTH backends: leaving a stale file behind would silently re-enroll
/// the machine on the next start.
pub fn delete(runner_id: &str) -> Result<()> {
    if let Err(e) = delete_web_session(runner_id) {
        tracing::warn!(error = %e, "could not forget the kept web session");
    }
    STORE.delete(runner_id)
}

/// The account the web session of a runner id is filed under. Never the
/// runner id itself: the fallback file is named after the account, and the
/// machine's credential owns that name.
fn web_session_account(runner_id: &str) -> String {
    format!("web-session-{runner_id}")
}

/// Keep the chat window's web session (a JSON document) for a runner id.
#[cfg_attr(not(feature = "gui"), allow(dead_code))]
pub fn store_web_session(runner_id: &str, session: &str) -> Result<()> {
    STORE.store(&web_session_account(runner_id), session)
}

/// The web session kept for a runner id, or `None` when none was.
#[cfg_attr(not(feature = "gui"), allow(dead_code))]
pub fn load_web_session(runner_id: &str) -> Result<Option<String>> {
    STORE.load(&web_session_account(runner_id))
}

/// Forget the web session kept for a runner id. Missing is not an error.
pub fn delete_web_session(runner_id: &str) -> Result<()> {
    STORE.delete(&web_session_account(runner_id))
}

#[cfg(test)]
mod tests {
    use super::{fallback_path, web_session_account};
    use crate::config::RunnerConfig;

    /// Against this machine's real OS keychain:
    /// `cargo test --bins a_web_session_round_trips -- --ignored`.
    #[test]
    #[ignore]
    fn a_web_session_round_trips_through_the_os_keychain() {
        let runner = format!("keychain-test-{}", std::process::id());
        let short = r#"{"token":"h.p.s","admin_token":null}"#;
        super::store_web_session(&runner, short).unwrap();
        assert_eq!(
            super::load_web_session(&runner).unwrap().as_deref(),
            Some(short)
        );
        // Too long for the Windows Credential Manager: it lands in the file,
        // and only because the older copy was removed first is it the one read.
        let long = format!(
            r#"{{"token":"h.{}.s","admin_token":null}}"#,
            "p".repeat(3000)
        );
        super::delete_web_session(&runner).unwrap();
        super::store_web_session(&runner, &long).unwrap();
        assert_eq!(
            super::load_web_session(&runner).unwrap().as_deref(),
            Some(long.as_str())
        );
        super::delete(&runner).unwrap();
        assert_eq!(super::load_web_session(&runner).unwrap(), None);
    }

    #[test]
    fn the_web_session_never_takes_the_credential_s_place() {
        let runner = "2f1c9d7a-0000-4000-8000-000000000000";
        let session = fallback_path(&web_session_account(runner)).unwrap();
        assert_ne!(session, fallback_path(runner).unwrap());
        assert_eq!(
            session.file_name().unwrap().to_str().unwrap(),
            format!("credential-web-session-{runner}")
        );
    }

    #[test]
    fn fallback_path_stays_inside_the_config_dir() {
        let path = fallback_path("run-abc123").unwrap();
        assert_eq!(
            path.file_name().unwrap().to_str().unwrap(),
            "credential-run-abc123"
        );
        assert_eq!(path.parent().unwrap(), RunnerConfig::config_dir().unwrap());
    }

    #[test]
    fn fallback_path_neutralizes_a_hostile_runner_id() {
        // The id arrives from the server and becomes a path segment. Anything
        // outside [A-Za-z0-9_-] collapses to '_', so separators and dots (and
        // therefore "..") cannot survive into the file name.
        let path = fallback_path("../../etc/cron.d/pwn").unwrap();
        let name = path.file_name().unwrap().to_str().unwrap();
        assert_eq!(name, "credential-______etc_cron_d_pwn");
        assert!(!name.contains('/') && !name.contains(".."));
        assert_eq!(path.parent().unwrap(), RunnerConfig::config_dir().unwrap());
    }
}
