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

//! What the running relay says about itself: `status.json` in its state
//! directory (read by `xtm-one-relay status`) and the health endpoint.

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use tracing::debug;

use crate::version::AGENT_VERSION;

/// The relay's connection state.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum State {
    Starting,
    Connecting,
    Online,
    Reconnecting,
    NotEnrolled,
    Revoked,
    Disabled,
    UpdateRequired,
    Stopped,
}

impl State {
    pub fn label(self) -> &'static str {
        match self {
            Self::Starting => "starting",
            Self::Connecting => "connecting",
            Self::Online => "online",
            Self::Reconnecting => "reconnecting",
            Self::NotEnrolled => "not enrolled",
            Self::Revoked => "credential revoked",
            Self::Disabled => "relays disabled on the platform",
            Self::UpdateRequired => "update required",
            Self::Stopped => "stopped",
        }
    }
}

/// One snapshot of the relay.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Status {
    pub state: State,
    pub transport: Option<String>,
    pub since: String,
    pub last_error: Option<String>,
    pub relay_id: Option<String>,
    pub server_url: Option<String>,
    pub version: String,
    pub pid: u32,
    #[serde(default)]
    pub in_flight: usize,
}

/// Keeps `status.json` in step with the relay.
pub struct StatusReporter {
    path: PathBuf,
    inner: Mutex<Status>,
}

fn now() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
}

impl StatusReporter {
    pub fn new(path: PathBuf, relay_id: Option<String>, server_url: Option<String>) -> Self {
        let reporter = Self {
            path,
            inner: Mutex::new(Status {
                state: State::Starting,
                transport: None,
                since: now(),
                last_error: None,
                relay_id,
                server_url,
                version: AGENT_VERSION.to_string(),
                pid: std::process::id(),
                in_flight: 0,
            }),
        };
        reporter.write();
        reporter
    }

    /// Record a new state; the file is rewritten only when something changed.
    pub fn set(&self, state: State, transport: Option<&str>, error: Option<String>) {
        {
            let mut status = self.inner.lock().expect("status");
            let transport = transport.map(str::to_string);
            if status.state == state && status.transport == transport && status.last_error == error
            {
                return;
            }
            if status.state != state {
                status.since = now();
            }
            status.state = state;
            status.transport = transport;
            if error.is_some() || state == State::Online {
                status.last_error = error;
            }
        }
        self.write();
    }

    pub fn snapshot(&self, in_flight: usize) -> Status {
        let mut status = self.inner.lock().expect("status").clone();
        status.in_flight = in_flight;
        status
    }

    fn write(&self) {
        let status = self.inner.lock().expect("status").clone();
        let Ok(text) = serde_json::to_string_pretty(&status) else {
            return;
        };
        if let Err(e) = std::fs::write(&self.path, text) {
            debug!("could not write {}: {e}", self.path.display());
        }
    }

    /// The last status a relay wrote at `path`.
    pub fn read(path: &Path) -> Result<Option<Status>> {
        match std::fs::read_to_string(path) {
            Ok(text) => Ok(Some(
                serde_json::from_str(&text)
                    .with_context(|| format!("reading {}", path.display()))?,
            )),
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(err) => Err(anyhow::Error::new(err).context(format!("reading {}", path.display()))),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_file_follows_the_state() {
        let path =
            std::env::temp_dir().join(format!("xtm-relay-status-{}.json", std::process::id()));
        let reporter = StatusReporter::new(path.clone(), Some("r1".into()), None);
        reporter.set(State::Online, Some("websocket"), None);
        let read = StatusReporter::read(&path).unwrap().unwrap();
        assert_eq!(read.state, State::Online);
        assert_eq!(read.transport.as_deref(), Some("websocket"));
        reporter.set(State::Reconnecting, None, Some("socket closed".into()));
        let read = StatusReporter::read(&path).unwrap().unwrap();
        assert_eq!(read.last_error.as_deref(), Some("socket closed"));
        let _ = std::fs::remove_file(&path);
    }
}
