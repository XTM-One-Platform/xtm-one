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

//! Live runner status, shared between the transport loop and the GUI.
//!
//! The transport reports transitions through a [`StatusReporter`]; the GUI holds
//! the matching `watch::Receiver` and mirrors every change onto the tray tooltip,
//! the tray status line, and a `status` event the webview panel listens for. The
//! headless CLI passes [`StatusReporter::noop`] so the exact same transport code
//! runs with zero status plumbing.

use serde::Serialize;
use tokio::sync::watch;

/// A user-facing snapshot of what the runner is doing right now.
///
/// Some variants / helpers are only consumed by the GUI; a headless
/// (`--no-default-features`) build never reads them, hence the feature-gated
/// dead-code allowances below.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum RunnerStatus {
    /// No credential yet - the panel shows onboarding.
    #[cfg_attr(not(feature = "gui"), allow(dead_code))]
    NotEnrolled,
    /// Establishing the outbound connection.
    Connecting,
    /// Connected and serving commands. `transport` is `websocket` or `long_poll`.
    Online { transport: String },
    /// Lost the connection; backing off before the next attempt.
    Reconnecting,
    /// The server refuses this version, over the WebSocket or the long-poll
    /// fallback. The headless daemon's self-updater applies the update; the
    /// desktop app installs nothing on this signal - its bundle updater does,
    /// on its own schedule or on the person's click.
    UpdateRequired,
    /// The loop ended cleanly (shutdown requested).
    Stopped,
    /// A fatal, non-retryable error (revoked credential, runners disabled).
    Error { message: String },
}

impl RunnerStatus {
    /// A short label for the tray tooltip / status line.
    #[cfg_attr(not(feature = "gui"), allow(dead_code))]
    pub fn label(&self) -> String {
        match self {
            RunnerStatus::NotEnrolled => "Not enrolled".into(),
            RunnerStatus::Connecting => "Connecting...".into(),
            RunnerStatus::Online { transport } if transport == "long_poll" => {
                "Online (fallback)".into()
            }
            RunnerStatus::Online { .. } => "Online".into(),
            RunnerStatus::Reconnecting => "Reconnecting...".into(),
            RunnerStatus::UpdateRequired => "Update required".into(),
            RunnerStatus::Stopped => "Stopped".into(),
            RunnerStatus::Error { .. } => "Error".into(),
        }
    }
}

/// A cheap, cloneable handle the transport uses to publish status transitions.
/// Wraps an optional sender so the CLI path can opt out entirely.
#[derive(Clone, Default)]
pub struct StatusReporter {
    tx: Option<watch::Sender<RunnerStatus>>,
}

impl StatusReporter {
    /// A reporter that drops every update (headless CLI path).
    pub fn noop() -> Self {
        Self { tx: None }
    }

    /// A reporter backed by a `watch` channel (GUI path).
    #[cfg_attr(not(feature = "gui"), allow(dead_code))]
    pub fn new(tx: watch::Sender<RunnerStatus>) -> Self {
        Self { tx: Some(tx) }
    }

    /// Publish a new status. A dropped receiver is not an error - the transport
    /// keeps serving whether or not anyone is watching.
    pub fn set(&self, status: RunnerStatus) {
        if let Some(tx) = &self.tx {
            let _ = tx.send(status);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The connection panel, which mirrors `label()` in `friendlyStatusLabel`.
    const PANEL_JS: &str = include_str!("../ui/app.js");

    #[test]
    fn a_refused_version_is_never_described_as_an_install_in_progress() {
        // The desktop app installs nothing on this signal: the version is
        // refused until the person updates.
        let label = RunnerStatus::UpdateRequired.label();
        assert_eq!(label, "Update required");
        assert!(PANEL_JS.contains(&format!("return \"{label}\";")));
        assert!(!PANEL_JS.contains("Installing a required update"));
        assert!(!PANEL_JS.contains("\"Updating"));
    }
}
