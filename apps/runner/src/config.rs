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

//! Persistent runner configuration.
//!
//! Split deliberately from the credential: non-secret state (which server, this
//! machine's runner id, tuning) lives in a JSON file under the OS config dir;
//! the `run-` credential goes to the OS keychain ([`crate::credential`], with a
//! 0600 file fallback on hosts that have none). That way a config file is safe
//! to inspect and back up, and the secret is never mixed into it.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

const QUALIFIER: &str = "one";
const ORGANIZATION: &str = "Filigran";
const APPLICATION: &str = "xtm-one-runner";

/// The persisted, non-secret runner configuration.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RunnerConfig {
    /// Base URL of the XTM One platform, e.g. `https://one.filigran.io`.
    pub server_url: String,
    /// This machine's runner id (returned at enrollment).
    pub runner_id: String,
    /// Human-friendly runner name (server-assigned or the machine hostname).
    /// Non-secret, display-only - surfaced in the GUI overview and tray tooltip.
    #[serde(default)]
    pub name: Option<String>,
    /// OS family reported at enrollment: `windows` | `macos` | `linux`.
    pub platform: String,
    /// Heartbeat cadence the server told us to use (seconds).
    #[serde(default = "default_heartbeat")]
    pub heartbeat_interval_seconds: u64,
    /// Long-poll hold time the server told us to use (seconds).
    #[serde(default = "default_long_poll")]
    pub long_poll_timeout_seconds: u64,
    /// CDP endpoint for real-browser control (feature = "browser").
    #[serde(default = "default_cdp_url")]
    pub cdp_url: String,
    /// Whether the runner may apply signature-verified self-updates on its own.
    /// Default true: a fleet of thousands stays current without operator touch.
    /// Set false (edit config.json) to only LOG that a newer build exists and
    /// update out-of-band. Signature verification is enforced either way — this
    /// only governs auto-*apply*, never whether an unsigned build is trusted.
    #[serde(default = "default_auto_update")]
    pub auto_update: bool,
    /// Whether the GUI app registers itself to start at login. Stored here
    /// (and not only read back from the OS autostart entry) so the desktop app
    /// can re-apply it on every launch - an upgrade moves the executable on
    /// Windows, and re-applying rewrites the entry's path.
    ///
    /// `None` is a config written by a build without this field, which kept this choice
    /// only in the OS entry: the GUI reads that entry once at launch (it is
    /// registered under the technical name the old build used too) and records
    /// what it says, so a person who had turned autostart OFF is not turned
    /// back on by the upgrade. Enrollment writes `Some(true)` - the historical
    /// default.
    #[serde(default)]
    pub launch_at_login: Option<bool>,
}

fn default_heartbeat() -> u64 {
    30
}

fn default_long_poll() -> u64 {
    25
}

fn default_cdp_url() -> String {
    "http://127.0.0.1:9222".to_string()
}

fn default_auto_update() -> bool {
    true
}

impl RunnerConfig {
    /// The directory holding the config file (created on demand).
    pub fn config_dir() -> Result<PathBuf> {
        let dirs = directories::ProjectDirs::from(QUALIFIER, ORGANIZATION, APPLICATION)
            .context("could not resolve an OS config directory")?;
        Ok(dirs.config_dir().to_path_buf())
    }

    fn config_path() -> Result<PathBuf> {
        Ok(Self::config_dir()?.join("config.json"))
    }

    /// Load the persisted config, or `None` if the runner is not yet enrolled.
    pub fn load() -> Result<Option<Self>> {
        let path = Self::config_path()?;
        if !path.exists() {
            return Ok(None);
        }
        let raw = std::fs::read_to_string(&path)
            .with_context(|| format!("reading config at {}", path.display()))?;
        let cfg: RunnerConfig = serde_json::from_str(&raw)
            .with_context(|| format!("parsing config at {}", path.display()))?;
        Ok(Some(cfg))
    }

    /// Persist the config (pretty JSON, 0600 on unix).
    pub fn save(&self) -> Result<()> {
        let dir = Self::config_dir()?;
        std::fs::create_dir_all(&dir)
            .with_context(|| format!("creating config dir {}", dir.display()))?;
        let path = Self::config_path()?;
        let raw = serde_json::to_string_pretty(self)?;
        std::fs::write(&path, raw)
            .with_context(|| format!("writing config at {}", path.display()))?;
        restrict_permissions(&path);
        Ok(())
    }

    /// Remove the persisted config (used by `reset`).
    pub fn delete() -> Result<()> {
        let path = Self::config_path()?;
        if path.exists() {
            std::fs::remove_file(&path)
                .with_context(|| format!("removing config at {}", path.display()))?;
        }
        Ok(())
    }

    /// Normalized base URL without a trailing slash.
    pub fn base_url(&self) -> String {
        self.server_url.trim_end_matches('/').to_string()
    }

    /// REST base, e.g. `https://host/api/v1/runners`.
    pub fn api_base(&self) -> String {
        format!("{}/api/v1/runners", self.base_url())
    }

    /// WebSocket connect URL.
    ///
    /// The `run-` credential is NOT in the URL: it travels in an
    /// `Authorization: Bearer` header set by the transport, because query
    /// strings are routinely retained by reverse proxies, load balancers and
    /// access logs, and this credential grants OS control. The runner `version`
    /// DOES ride the query string (it is not sensitive) so the server can gate
    /// an out-of-date runner on the WS fast path, exactly as the long-poll
    /// `?version=` parameter does.
    pub fn ws_connect_url(&self, version: &str) -> String {
        let base = self.base_url();
        let ws = if let Some(rest) = base.strip_prefix("https://") {
            format!("wss://{rest}")
        } else if let Some(rest) = base.strip_prefix("http://") {
            format!("ws://{rest}")
        } else {
            base
        };
        let encoded = urlencode(version);
        format!("{ws}/api/v1/runners/connect?version={encoded}")
    }
}

/// Minimal percent-encoding for a query parameter value (belt-and-braces for
/// the `version` string, which is a dotted-int build version).
fn urlencode(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for byte in value.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(byte as char)
            }
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}

#[cfg(unix)]
pub(crate) fn restrict_permissions(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    if let Ok(meta) = std::fs::metadata(path) {
        let mut perms = meta.permissions();
        perms.set_mode(0o600);
        let _ = std::fs::set_permissions(path, perms);
    }
}

#[cfg(not(unix))]
pub(crate) fn restrict_permissions(_path: &Path) {
    // Windows: the file inherits the user profile's ACL, which is per-user.
}
