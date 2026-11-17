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

//! Wire protocol shared with the XTM One backend.
//!
//! The backend contract lives in `backend/app/api/v1/runners.py` and
//! `backend/app/services/runner_transport.py`. These types mirror it exactly:
//!
//! * enrollment (`renr-` -> `run-`): [`EnrollRequest`] / [`EnrollResponse`];
//! * the WebSocket fast path: the runner sends [`ClientFrame`] (`hello` /
//!   `heartbeat`), the server pushes [`ServerFrame::Command`];
//! * the HTTP long-poll fallback returns [`PollResponse`];
//! * results always upload over HTTP multipart as [`ResultMeta`] (+ an optional
//!   binary part), never over the socket.

use serde::{Deserialize, Serialize};

/// Body of `POST /api/v1/runners/enroll`.
#[derive(Debug, Serialize)]
pub struct EnrollRequest {
    pub enrollment_key: String,
    pub platform: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hostname: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub host_facts: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub agent_version: Option<String>,
    /// Salted hash of the OS machine id (`host_facts::machine_id`) so the
    /// server converges a re-enrollment on the machine's existing row.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub machine_id: Option<String>,
}

/// Response of `POST /api/v1/runners/enroll` (raw `run-` key returned once).
#[derive(Debug, Deserialize)]
pub struct EnrollResponse {
    pub runner: RunnerInfo,
    pub key: String,
    #[serde(default)]
    pub heartbeat_interval_seconds: Option<u64>,
    #[serde(default)]
    pub long_poll_timeout_seconds: Option<u64>,
}

/// Subset of the serialized runner we care about after enrollment.
#[derive(Debug, Deserialize)]
pub struct RunnerInfo {
    pub id: String,
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub permission_mode: Option<String>,
}

/// A command pushed by the server (over WS) or drained from the long-poll.
#[derive(Debug, Clone, Deserialize)]
pub struct Command {
    pub id: String,
    pub kind: String,
    #[serde(default)]
    pub payload: serde_json::Value,
    #[serde(default)]
    pub timeout: Option<u64>,
}

/// A frame received on the WebSocket from the server.
#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ServerFrame {
    Command {
        command: Command,
    },
    /// The runner is below `runner_min_supported_version`; the server sends this
    /// then closes with code 4426. Trigger a self-update instead of reconnecting.
    /// (The server also echoes `current_version`, which the runner already knows,
    /// so it is intentionally not deserialized here.)
    UpdateRequired {
        #[serde(default)]
        min_version: Option<String>,
    },
    /// Stop a running shell command (the Stop on its chat card): the
    /// runner ends its process tree and reports `stopped: "user"` with the
    /// output printed until then. A command no longer running ignores it.
    Stop {
        command_id: String,
    },
    #[serde(other)]
    Unknown,
}

/// A frame the runner sends up the WebSocket. Control frames only.
#[derive(Debug, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ClientFrame {
    /// Sent immediately on connect: reports host facts + version, and asks the
    /// server to re-drain any queued commands.
    Hello {
        host_facts: serde_json::Value,
        agent_version: String,
    },
    /// Periodic keepalive that refreshes `last_seen_at` (the authoritative
    /// online signal) and updates host facts.
    Heartbeat {
        host_facts: serde_json::Value,
        agent_version: String,
    },
    /// Best-effort acknowledgement (ignored server-side, kept for symmetry).
    Ack { command_id: String },
}

/// `GET /api/v1/runners/poll` response.
#[derive(Debug, Deserialize)]
pub struct PollResponse {
    #[serde(default)]
    pub command: Option<Command>,
    /// Set when the runner is below the server's minimum supported version. No
    /// command is dispatched until it updates.
    #[serde(default)]
    pub update_required: bool,
}

/// The largest `meta` part a result upload may carry, in bytes. The platform
/// reads that part in memory and refuses a larger one
/// (`RUNNER_MAX_RESULT_META_BYTES` in `backend/app/services/runner_transport.py`,
/// pinned against this constant by `backend/tests/test_runner_result_upload.py`).
/// Every result a capability produces within its own caps fits: two 1 MiB shell
/// streams at the worst JSON escaping (six bytes a byte) are 12 MiB.
pub const MAX_RESULT_META_BYTES: usize = 16 * 1024 * 1024;

/// The JSON `meta` part of a result upload. `result` is the structured payload
/// the agent tool reads; a binary artifact travels as the multipart `file`.
#[derive(Debug, Serialize)]
pub struct ResultMeta {
    /// One of `completed` | `failed` | `timeout`.
    pub status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// Terminal outcome of executing one [`Command`]. `file`, when present, is
/// uploaded as the multipart binary part (a PNG screenshot, a file read).
#[derive(Debug, Default)]
pub struct CommandOutcome {
    pub status: OutcomeStatus,
    pub result: Option<serde_json::Value>,
    pub error: Option<String>,
    pub file: Option<FilePart>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum OutcomeStatus {
    #[default]
    Completed,
    Failed,
    Timeout,
}

impl OutcomeStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            OutcomeStatus::Completed => "completed",
            OutcomeStatus::Failed => "failed",
            OutcomeStatus::Timeout => "timeout",
        }
    }
}

/// A binary artifact to upload alongside a command's result meta.
#[derive(Debug)]
pub struct FilePart {
    pub bytes: Vec<u8>,
    pub filename: String,
    pub content_type: String,
}

impl CommandOutcome {
    pub fn completed(result: serde_json::Value) -> Self {
        Self {
            status: OutcomeStatus::Completed,
            result: Some(result),
            error: None,
            file: None,
        }
    }

    pub fn completed_file(result: serde_json::Value, file: FilePart) -> Self {
        Self {
            status: OutcomeStatus::Completed,
            result: Some(result),
            error: None,
            file: Some(file),
        }
    }

    pub fn failed(error: impl Into<String>) -> Self {
        Self {
            status: OutcomeStatus::Failed,
            result: None,
            error: Some(error.into()),
            file: None,
        }
    }

    pub fn timeout(error: impl Into<String>) -> Self {
        Self {
            status: OutcomeStatus::Timeout,
            result: None,
            error: Some(error.into()),
            file: None,
        }
    }
}
