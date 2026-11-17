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

//! Shell command execution.
//!
//! Windows -> PowerShell (`powershell -NoProfile -Command`); macOS/Linux -> the
//! default shell (`sh -c`). Enforces the per-command timeout the agent set and
//! caps captured output so a runaway command can't produce a multi-gigabyte
//! result payload.
//!
//! The shell runs as the root of its own process tree (a new process group on
//! Unix; on Windows the tree under its pid): a command that ends early is ended
//! with everything it started, and what it had printed until then is reported
//! with the outcome, never dropped.

use std::collections::HashMap;
use std::process::Stdio;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

use anyhow::{anyhow, Result};
use tokio::io::{AsyncRead, AsyncReadExt};
use tokio::process::Command;
use tokio::sync::Notify;

use crate::protocol::{CommandOutcome, OutcomeStatus};

/// Hard cap on captured stdout/stderr (1 MiB each); the tail is dropped with a
/// marker so the agent knows output was truncated.
const MAX_STREAM: usize = 1_048_576;

/// How long the pipes are drained after the tree was killed: a process that
/// escaped the tree (a detached grandchild holding the pipe) must not keep the
/// outcome waiting, so what was read by then is what is reported.
const DRAIN_AFTER_KILL: Duration = Duration::from_secs(2);

/// What one pipe produced so far, shared with the task reading it so a command
/// ended early still reports it.
#[derive(Default)]
struct Captured {
    bytes: Vec<u8>,
    truncated: bool,
}

type Shared = Arc<Mutex<Captured>>;

/// The shell commands running now, by command id, each with the signal that
/// stops it: the server's `stop` frame (the Stop on the chat card) finds the
/// command here. A command not in the map (never started, already exited) has
/// nothing to stop, so a late stop is a no-op.
fn running() -> &'static Mutex<HashMap<String, Arc<Notify>>> {
    static RUNNING: OnceLock<Mutex<HashMap<String, Arc<Notify>>>> = OnceLock::new();
    RUNNING.get_or_init(Default::default)
}

/// Ask the running shell command `command_id` to stop; `false` when no such
/// command is running here (it already exited, or never started).
pub fn request_stop(command_id: &str) -> bool {
    let running = running().lock().unwrap_or_else(|e| e.into_inner());
    match running.get(command_id) {
        Some(stop) => {
            // notify_one keeps a permit: a stop that lands before the command
            // waits on it still ends it.
            stop.notify_one();
            true
        }
        None => false,
    }
}

/// Registers a running command for [`request_stop`] and removes it when the
/// command ends, whichever way it ends.
struct Registration(Option<String>);

impl Registration {
    fn new(command_id: Option<&str>) -> (Self, Arc<Notify>) {
        let stop = Arc::new(Notify::new());
        if let Some(id) = command_id {
            running()
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .insert(id.to_string(), stop.clone());
        }
        (Self(command_id.map(str::to_string)), stop)
    }
}

impl Drop for Registration {
    fn drop(&mut self) {
        if let Some(id) = self.0.take() {
            running()
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .remove(&id);
        }
    }
}

/// How the wait on a running command ended.
enum Ended<T> {
    Exited(T),
    TimedOut,
    Stopped,
}

pub async fn execute(
    payload: &serde_json::Value,
    default_timeout: u64,
    command_id: Option<&str>,
) -> CommandOutcome {
    match execute_inner(payload, default_timeout, command_id).await {
        Ok(outcome) => outcome,
        Err(err) => CommandOutcome::failed(format!("run_shell failed: {err:#}")),
    }
}

async fn execute_inner(
    payload: &serde_json::Value,
    default_timeout: u64,
    command_id: Option<&str>,
) -> Result<CommandOutcome> {
    let command = payload
        .get("command")
        .and_then(|v| v.as_str())
        .ok_or_else(|| anyhow!("missing 'command'"))?;
    let cwd = payload.get("cwd").and_then(|v| v.as_str());
    let timeout_secs = payload
        .get("timeout")
        .and_then(|v| v.as_u64())
        .unwrap_or(default_timeout)
        .max(1);

    let mut cmd = shell_command(command);
    if let Some(dir) = cwd {
        cmd.current_dir(dir);
    }
    cmd.stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    // Windows: run the shell headless so a `run_shell` task never flashes a
    // console window (the runner is a windowless GUI app). No-op elsewhere.
    crate::proc::no_window(&mut cmd);
    own_process_group(&mut cmd);

    let mut child = cmd.spawn().map_err(|e| anyhow!("spawning shell: {e}"))?;
    let pid = child.id();
    let (_registration, stop) = Registration::new(command_id);

    // Read both pipes CONCURRENTLY, keeping at most MAX_STREAM bytes each and
    // draining the overflow, so a noisy command (e.g. `yes`) cannot balloon
    // memory before the timeout - wait_with_output() would have buffered every
    // byte first, defeating the cap. Draining both pipes also prevents a
    // pipe-full deadlock while the child keeps writing. The readers own their
    // pipe and write into a shared buffer, so the bytes read before an early
    // end are still there to report.
    let out = Shared::default();
    let err = Shared::default();
    let mut readers = tokio::task::JoinSet::new();
    if let Some(pipe) = child.stdout.take() {
        readers.spawn(read_capped(pipe, MAX_STREAM, out.clone()));
    }
    if let Some(pipe) = child.stderr.take() {
        readers.spawn(read_capped(pipe, MAX_STREAM, err.clone()));
    }

    let finished = async {
        while readers.join_next().await.is_some() {}
        child.wait().await
    };
    // Bound before the match so the borrows `finished` holds end here, and
    // the timeout and stop arms may use the readers and the child again.
    let waited = tokio::select! {
        result = tokio::time::timeout(Duration::from_secs(timeout_secs), finished) => match result {
            Ok(status) => Ended::Exited(status),
            Err(_) => Ended::TimedOut,
        },
        _ = stop.notified() => Ended::Stopped,
    };
    let status = match waited {
        Ended::Exited(status) => status.map_err(|e| anyhow!("awaiting shell: {e}"))?,
        ended => {
            kill_tree(pid, &mut child).await;
            // The killed tree closes its ends of the pipes: the readers reach
            // EOF with the last bytes written, unless something outside the
            // tree still holds a pipe.
            let _ = tokio::time::timeout(DRAIN_AFTER_KILL, async {
                while readers.join_next().await.is_some() {}
            })
            .await;
            readers.abort_all();
            let _ = child.try_wait();
            let (status, error, stopped) = match ended {
                // Stopped from the chat card: a failure the user
                // chose, told apart from a crash by `stopped: "user"`.
                Ended::Stopped => (
                    OutcomeStatus::Failed,
                    "command stopped by the user".to_string(),
                    "user",
                ),
                _ => (
                    OutcomeStatus::Timeout,
                    format!("command exceeded its {timeout_secs}s timeout"),
                    "timeout",
                ),
            };
            return Ok(stopped_early(status, error, stopped, &out, &err));
        }
    };

    Ok(CommandOutcome::completed(serde_json::json!({
        "exit_code": status.code(),
        "stdout": render_shared(&out),
        "stderr": render_shared(&err),
    })))
}

/// The outcome of a command ended before it exited: its status and why, with
/// what it had printed until then and `stopped` naming who ended it, so the
/// agent and the card both show the partial output.
fn stopped_early(
    status: OutcomeStatus,
    error: String,
    stopped: &str,
    out: &Shared,
    err: &Shared,
) -> CommandOutcome {
    CommandOutcome {
        status,
        result: Some(serde_json::json!({
            "exit_code": null,
            "stopped": stopped,
            "stdout": render_shared(out),
            "stderr": render_shared(err),
        })),
        error: Some(error),
        file: None,
    }
}

/// Unix: the shell leads a new process group, so ending the command signals
/// every process it started rather than the shell alone.
#[cfg(unix)]
pub(crate) fn own_process_group(cmd: &mut Command) {
    cmd.process_group(0);
}

/// Windows: the tree is found from the shell's pid when it is ended.
#[cfg(not(unix))]
pub(crate) fn own_process_group(_cmd: &mut Command) {}

/// End the shell and everything it started. Best-effort: the shell itself is
/// killed whatever becomes of the tree.
#[cfg(unix)]
pub(crate) async fn kill_tree(pid: Option<u32>, child: &mut tokio::process::Child) {
    if let Some(pgid) = pid.and_then(|p| i32::try_from(p).ok()) {
        // SAFETY: kill(2) with a negative pid signals the process group the
        // shell leads (`process_group(0)` at spawn); it reads no memory.
        unsafe {
            libc::kill(-pgid, libc::SIGKILL);
        }
    }
    let _ = child.start_kill();
}

#[cfg(windows)]
pub(crate) async fn kill_tree(pid: Option<u32>, child: &mut tokio::process::Child) {
    if let Some(pid) = pid {
        // taskkill /T ends the process and every process it started.
        let mut taskkill = Command::new("taskkill");
        taskkill
            .args(["/T", "/F", "/PID", &pid.to_string()])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        crate::proc::no_window(&mut taskkill);
        if let Ok(mut killer) = taskkill.spawn() {
            let _ = tokio::time::timeout(Duration::from_secs(5), killer.wait()).await;
        }
    }
    let _ = child.start_kill();
}

#[cfg(not(any(unix, windows)))]
pub(crate) async fn kill_tree(_pid: Option<u32>, child: &mut tokio::process::Child) {
    let _ = child.start_kill();
}

#[cfg(windows)]
pub(crate) fn shell_command(command: &str) -> Command {
    let mut cmd = Command::new("powershell");
    cmd.args([
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        command,
    ]);
    cmd
}

#[cfg(not(windows))]
pub(crate) fn shell_command(command: &str) -> Command {
    let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/sh".to_string());
    let mut cmd = Command::new(shell);
    cmd.args(["-c", command]);
    cmd
}

/// Read a child pipe to EOF into `into`, keeping at most `cap` bytes and
/// draining the rest, so a runaway writer cannot exhaust memory.
async fn read_capped<R: AsyncRead + Unpin>(mut reader: R, cap: usize, into: Shared) {
    let mut buf = [0u8; 8192];
    loop {
        match reader.read(&mut buf).await {
            Ok(0) | Err(_) => break,
            Ok(n) => {
                let mut kept = into.lock().unwrap_or_else(|e| e.into_inner());
                if kept.bytes.len() < cap {
                    let take = (cap - kept.bytes.len()).min(n);
                    kept.bytes.extend_from_slice(&buf[..take]);
                    if take < n {
                        kept.truncated = true;
                    }
                } else {
                    // Past the cap: keep draining so the child never blocks
                    // on a full pipe, but stop retaining bytes.
                    kept.truncated = true;
                }
            }
        }
    }
}

fn render_shared(captured: &Shared) -> String {
    let kept = captured.lock().unwrap_or_else(|e| e.into_inner());
    render_stream(kept.bytes.clone(), kept.truncated)
}

fn render_stream(bytes: Vec<u8>, truncated: bool) -> String {
    let text = String::from_utf8_lossy(&bytes).into_owned();
    if truncated {
        format!("{text}\n...[output truncated at {MAX_STREAM} bytes]")
    } else {
        text
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::{ResultMeta, MAX_RESULT_META_BYTES};

    fn meta_len(stdout: String, stderr: String) -> usize {
        serde_json::to_string(&ResultMeta {
            status: "completed".into(),
            result: Some(serde_json::json!({
                "exit_code": 1,
                "stdout": stdout,
                "stderr": stderr,
            })),
            error: None,
        })
        .unwrap()
        .len()
    }

    #[test]
    fn a_shell_result_at_its_output_caps_fits_a_result_upload() {
        // One ordinary stream cut at its cap is already over the 1 MiB a form
        // field is refused past by default.
        let text = || render_stream(vec![b'a'; MAX_STREAM], true);
        assert!(meta_len(text(), String::new()) > MAX_STREAM);
        // Control bytes are the worst case: each one is written `\u00XX`, six
        // bytes; an invalid byte becomes U+FFFD, three.
        let control = || render_stream(vec![0x01; MAX_STREAM], true);
        let worst = meta_len(control(), control());
        assert!(worst > 12 * MAX_STREAM, "{worst}");
        assert!(worst <= MAX_RESULT_META_BYTES, "{worst}");
        let invalid = || render_stream(vec![0xff; MAX_STREAM], true);
        assert!(meta_len(invalid(), invalid()) < worst);
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn a_timed_out_command_reports_what_it_printed_and_ends_its_tree() {
        let marker = format!("/tmp/xtm-runner-tree-{}", std::process::id());
        let _ = std::fs::remove_file(&marker);
        // The shell starts a background child that would write the marker
        // after the timeout: killing the shell alone would leave it running.
        let script = format!("echo started; (sleep 3; touch {marker}) & sleep 30");
        let payload = serde_json::json!({ "command": script, "timeout": 1 });
        let started = std::time::Instant::now();
        let outcome = execute(&payload, 60, None).await;
        assert!(
            started.elapsed() < Duration::from_secs(5),
            "{:?}",
            started.elapsed()
        );
        assert_eq!(outcome.status, OutcomeStatus::Timeout);
        let result = outcome
            .result
            .expect("a timed-out command keeps its output");
        assert_eq!(result["stopped"], "timeout");
        assert!(result["exit_code"].is_null());
        assert_eq!(result["stdout"], "started\n");
        assert!(outcome.error.unwrap().contains("1s timeout"));
        tokio::time::sleep(Duration::from_secs(4)).await;
        assert!(
            !std::path::Path::new(&marker).exists(),
            "a child of the shell outlived it"
        );
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn a_command_that_exits_reports_its_code_and_output() {
        let payload = serde_json::json!({ "command": "echo out && echo err 1>&2 && exit 3" });
        let outcome = execute(&payload, 30, None).await;
        assert_eq!(outcome.status, OutcomeStatus::Completed);
        let result = outcome.result.unwrap();
        assert_eq!(result["exit_code"], 3);
        assert!(result["stdout"].as_str().unwrap().contains("out"));
        assert!(result["stderr"].as_str().unwrap().contains("err"));
        assert!(result.get("stopped").is_none());
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn a_stopped_command_ends_its_tree_and_keeps_what_it_printed() {
        let id = format!("stop-test-{}", std::process::id());
        let payload = serde_json::json!({ "command": "echo started; sleep 60", "timeout": 120 });
        let started = std::time::Instant::now();
        let run = {
            let id = id.clone();
            tokio::spawn(async move { execute(&payload, 120, Some(&id)).await })
        };
        // The command registers once it is spawned; stop it as soon as it is.
        let mut stopped = false;
        for _ in 0..100 {
            tokio::time::sleep(Duration::from_millis(50)).await;
            if request_stop(&id) {
                stopped = true;
                break;
            }
        }
        assert!(stopped, "the running command was never registered");
        let outcome = run.await.unwrap();
        assert!(
            started.elapsed() < Duration::from_secs(10),
            "{:?}",
            started.elapsed()
        );
        assert_eq!(outcome.status, OutcomeStatus::Failed);
        let result = outcome.result.expect("a stopped command keeps its output");
        assert_eq!(result["stopped"], "user");
        assert!(result["exit_code"].is_null());
        assert!(outcome.error.unwrap().contains("stopped by the user"));
        // Once it ended it is no longer running: a late stop is a no-op.
        assert!(!request_stop(&id));
    }

    #[test]
    fn a_stop_for_a_command_that_is_not_running_is_a_no_op() {
        assert!(!request_stop("no-such-command"));
    }
}
