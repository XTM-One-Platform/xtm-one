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

//! Background shell jobs.
//!
//! `run_shell` with `background: true` starts the command as the root of its
//! own process tree, exactly like a foreground command, but answers at once:
//! the job id (the command id), the pid and the first lines it printed. The
//! command's stdout and stderr go to a log file on this machine, so a job that
//! prints for an hour costs nothing in memory and its output survives the
//! turn. The `shell_job` command kind reads a job's log from an offset, lists
//! the jobs, and stops one with every process it started.
//!
//! Jobs live as long as the runner: they survive the chat turn that started
//! them, and end when they exit, when they are stopped (the tool, the card's
//! Stop through the server's `stop` frame), or when the runner itself exits.

use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

use anyhow::{anyhow, Result};
use tokio::sync::Notify;

use crate::capabilities::shell::{kill_tree, own_process_group, shell_command};
use crate::protocol::CommandOutcome;

/// Most jobs running at once on this machine: a job is a process tree the
/// user may not see, so an agent looping on `background: true` is refused
/// rather than filling the machine.
pub const MAX_RUNNING_JOBS: usize = 8;

/// Most jobs remembered (running or ended); the oldest ended job is forgotten
/// first, its log file removed with it.
const MAX_REMEMBERED_JOBS: usize = 32;

/// How long the start waits for the first lines before answering.
const FIRST_LINES_WAIT: Duration = Duration::from_millis(1_000);

/// Most bytes of the first lines returned by the start.
const FIRST_LINES_BYTES: u64 = 4 * 1024;

/// Most bytes one `tail` read returns.
pub const MAX_TAIL_BYTES: u64 = 64 * 1024;

/// What a `tail` without an offset returns: the end of the log.
const DEFAULT_TAIL_BYTES: u64 = 16 * 1024;

/// One background job.
struct Job {
    id: String,
    command: String,
    pid: Option<u32>,
    started_at: String,
    log: PathBuf,
    state: Arc<Mutex<JobState>>,
    stop: Arc<Notify>,
}

#[derive(Default, Clone)]
struct JobState {
    /// `None` while the job runs.
    ended_at: Option<String>,
    exit_code: Option<i32>,
    /// `"user"` when it was stopped, `None` when it exited by itself.
    stopped: Option<&'static str>,
}

impl Job {
    fn snapshot(&self) -> serde_json::Value {
        let state = self.state.lock().unwrap_or_else(|e| e.into_inner()).clone();
        let size = std::fs::metadata(&self.log).map(|m| m.len()).unwrap_or(0);
        serde_json::json!({
            "job_id": self.id,
            "command": self.command,
            "pid": self.pid,
            "started_at": self.started_at,
            "status": status_of(&state),
            "ended_at": state.ended_at,
            "exit_code": state.exit_code,
            "stopped": state.stopped,
            "output_bytes": size,
        })
    }

    fn running(&self) -> bool {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .ended_at
            .is_none()
    }
}

fn status_of(state: &JobState) -> &'static str {
    match (&state.ended_at, state.stopped) {
        (None, _) => "running",
        (Some(_), Some(_)) => "stopped",
        (Some(_), None) => "exited",
    }
}

fn jobs() -> &'static Mutex<Vec<Job>> {
    static JOBS: OnceLock<Mutex<Vec<Job>>> = OnceLock::new();
    JOBS.get_or_init(Default::default)
}

fn jobs_dir() -> PathBuf {
    std::env::temp_dir().join("xtm-one-runner-jobs")
}

fn now() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
}

/// Start `payload.command` as a background job named `job_id`.
pub async fn start(payload: &serde_json::Value, job_id: &str) -> CommandOutcome {
    match start_inner(payload, job_id).await {
        Ok(outcome) => outcome,
        Err(err) => CommandOutcome::failed(format!("run_shell (background) failed: {err:#}")),
    }
}

async fn start_inner(payload: &serde_json::Value, job_id: &str) -> Result<CommandOutcome> {
    let command = payload
        .get("command")
        .and_then(|v| v.as_str())
        .ok_or_else(|| anyhow!("missing 'command'"))?;
    let cwd = payload.get("cwd").and_then(|v| v.as_str());
    {
        let list = jobs().lock().unwrap_or_else(|e| e.into_inner());
        if list.iter().filter(|j| j.running()).count() >= MAX_RUNNING_JOBS {
            return Err(anyhow!(
                "{MAX_RUNNING_JOBS} background jobs are already running on this machine; \
                 stop one (shell_jobs action 'stop') before starting another"
            ));
        }
        if list.iter().any(|j| j.id == job_id) {
            return Err(anyhow!("job {job_id} already exists"));
        }
    }

    let dir = jobs_dir();
    std::fs::create_dir_all(&dir).map_err(|e| anyhow!("creating {}: {e}", dir.display()))?;
    let log = dir.join(format!("{}.log", safe_name(job_id)));
    let file = File::create(&log).map_err(|e| anyhow!("creating {}: {e}", log.display()))?;
    let err_file = file
        .try_clone()
        .map_err(|e| anyhow!("opening the job log: {e}"))?;

    let mut cmd = shell_command(command);
    if let Some(dir) = cwd {
        cmd.current_dir(dir);
    }
    cmd.stdin(Stdio::null())
        .stdout(Stdio::from(file))
        .stderr(Stdio::from(err_file))
        // The job outlives this call by design; the watcher below owns it.
        .kill_on_drop(false);
    crate::proc::no_window(&mut cmd);
    own_process_group(&mut cmd);
    let mut child = cmd.spawn().map_err(|e| anyhow!("spawning shell: {e}"))?;
    let pid = child.id();

    let state = Arc::new(Mutex::new(JobState::default()));
    let stop = Arc::new(Notify::new());
    let job = Job {
        id: job_id.to_string(),
        command: command.to_string(),
        pid,
        started_at: now(),
        log: log.clone(),
        state: state.clone(),
        stop: stop.clone(),
    };
    remember(job);

    // The watcher: records the exit, or ends the tree on a stop.
    tokio::spawn(async move {
        let exited = tokio::select! {
            status = child.wait() => Some(status.ok().and_then(|s| s.code())),
            _ = stop.notified() => None,
        };
        match exited {
            Some(code) => {
                let mut s = state.lock().unwrap_or_else(|e| e.into_inner());
                s.exit_code = code;
                s.ended_at = Some(now());
            }
            None => {
                kill_tree(pid, &mut child).await;
                let _ = tokio::time::timeout(Duration::from_secs(2), child.wait()).await;
                let mut s = state.lock().unwrap_or_else(|e| e.into_inner());
                s.stopped = Some("user");
                s.ended_at = Some(now());
            }
        }
    });

    // The first lines: wait a moment for the command to print or exit.
    let waited = tokio::time::Instant::now();
    while waited.elapsed() < FIRST_LINES_WAIT {
        let size = std::fs::metadata(&log).map(|m| m.len()).unwrap_or(0);
        if size >= FIRST_LINES_BYTES || !is_running(job_id) {
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    let (first, _next, _size) = read_window(&log, 0, FIRST_LINES_BYTES)?;
    let mut result = snapshot(job_id).unwrap_or_else(|| serde_json::json!({ "job_id": job_id }));
    if let Some(obj) = result.as_object_mut() {
        obj.insert("background".into(), serde_json::Value::Bool(true));
        obj.insert("first_output".into(), serde_json::Value::String(first));
    }
    Ok(CommandOutcome::completed(result))
}

/// Keep `job`, forgetting the oldest ended jobs past [`MAX_REMEMBERED_JOBS`].
fn remember(job: Job) {
    let mut list = jobs().lock().unwrap_or_else(|e| e.into_inner());
    list.push(job);
    while list.len() > MAX_REMEMBERED_JOBS {
        match list.iter().position(|j| !j.running()) {
            Some(i) => {
                let gone = list.remove(i);
                let _ = std::fs::remove_file(&gone.log);
            }
            None => break,
        }
    }
}

fn safe_name(id: &str) -> String {
    id.chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '-' {
                c
            } else {
                '_'
            }
        })
        .take(80)
        .collect()
}

fn is_running(job_id: &str) -> bool {
    let list = jobs().lock().unwrap_or_else(|e| e.into_inner());
    list.iter().any(|j| j.id == job_id && j.running())
}

fn snapshot(job_id: &str) -> Option<serde_json::Value> {
    let list = jobs().lock().unwrap_or_else(|e| e.into_inner());
    list.iter().find(|j| j.id == job_id).map(Job::snapshot)
}

/// Ask the running job `job_id` to stop; `false` when no such job runs here.
pub fn request_stop(job_id: &str) -> bool {
    let list = jobs().lock().unwrap_or_else(|e| e.into_inner());
    match list.iter().find(|j| j.id == job_id && j.running()) {
        Some(job) => {
            job.stop.notify_one();
            true
        }
        None => false,
    }
}

/// Read at most `max` bytes of `log` from `offset`: the text, the next offset
/// and the log's size.
fn read_window(log: &PathBuf, offset: u64, max: u64) -> Result<(String, u64, u64)> {
    let mut file = File::open(log).map_err(|e| anyhow!("opening the job log: {e}"))?;
    let size = file.metadata().map(|m| m.len()).unwrap_or(0);
    let start = offset.min(size);
    file.seek(SeekFrom::Start(start))
        .map_err(|e| anyhow!("reading the job log: {e}"))?;
    let mut buf = Vec::new();
    file.take(max)
        .read_to_end(&mut buf)
        .map_err(|e| anyhow!("reading the job log: {e}"))?;
    let next = start + buf.len() as u64;
    Ok((String::from_utf8_lossy(&buf).into_owned(), next, size))
}

/// The `shell_job` command: `list`, `tail` (job_id, optional offset and
/// max_bytes) or `stop` (job_id).
pub async fn handle(payload: &serde_json::Value) -> CommandOutcome {
    match handle_inner(payload).await {
        Ok(result) => CommandOutcome::completed(result),
        Err(err) => CommandOutcome::failed(format!("shell_job failed: {err:#}")),
    }
}

async fn handle_inner(payload: &serde_json::Value) -> Result<serde_json::Value> {
    let action = payload
        .get("action")
        .and_then(|v| v.as_str())
        .unwrap_or("list");
    if action == "list" {
        let list = jobs().lock().unwrap_or_else(|e| e.into_inner());
        let entries: Vec<_> = list.iter().rev().map(Job::snapshot).collect();
        return Ok(serde_json::json!({ "jobs": entries }));
    }
    let job_id = payload
        .get("job_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| anyhow!("missing 'job_id'"))?;
    let log = {
        let list = jobs().lock().unwrap_or_else(|e| e.into_inner());
        list.iter()
            .find(|j| j.id == job_id)
            .map(|j| j.log.clone())
            .ok_or_else(|| {
                anyhow!(
                    "no job {job_id} on this machine (the runner restarted, or it was forgotten)"
                )
            })?
    };
    match action {
        "stop" => {
            if request_stop(job_id) {
                // The watcher ends the tree; wait for it to record the end.
                for _ in 0..50 {
                    if !is_running(job_id) {
                        break;
                    }
                    tokio::time::sleep(Duration::from_millis(100)).await;
                }
            }
            tail(job_id, &log, None, DEFAULT_TAIL_BYTES)
        }
        "tail" => {
            let offset = payload.get("offset").and_then(|v| v.as_u64());
            let max = payload
                .get("max_bytes")
                .and_then(|v| v.as_u64())
                .unwrap_or(if offset.is_some() {
                    MAX_TAIL_BYTES
                } else {
                    DEFAULT_TAIL_BYTES
                })
                .clamp(1, MAX_TAIL_BYTES);
            tail(job_id, &log, offset, max)
        }
        other => Err(anyhow!("unknown action '{other}' (list, tail, stop)")),
    }
}

fn tail(job_id: &str, log: &PathBuf, offset: Option<u64>, max: u64) -> Result<serde_json::Value> {
    let size = std::fs::metadata(log).map(|m| m.len()).unwrap_or(0);
    let from = offset.unwrap_or_else(|| size.saturating_sub(max));
    let (text, next, size) = read_window(log, from, max)?;
    let mut out = snapshot(job_id).unwrap_or_else(|| serde_json::json!({ "job_id": job_id }));
    if let Some(obj) = out.as_object_mut() {
        obj.insert("offset".into(), from.min(size).into());
        obj.insert("next_offset".into(), next.into());
        obj.insert("output_bytes".into(), size.into());
        obj.insert("more".into(), (next < size).into());
        obj.insert("output".into(), text.into());
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(unix)]
    #[tokio::test]
    async fn a_background_job_answers_at_once_and_keeps_running() {
        let id = format!("job-test-{}", std::process::id());
        let payload = serde_json::json!({ "command": "echo first; sleep 30" });
        let started = std::time::Instant::now();
        let outcome = start(&payload, &id).await;
        assert!(
            started.elapsed() < Duration::from_secs(2),
            "{:?}",
            started.elapsed()
        );
        let result = outcome.result.expect("a started job answers its id");
        assert_eq!(result["job_id"], id.as_str());
        assert_eq!(result["background"], true);
        assert_eq!(result["status"], "running");
        assert!(result["first_output"].as_str().unwrap().contains("first"));
        assert!(result["pid"].as_u64().is_some());

        let listed = handle(&serde_json::json!({ "action": "list" }))
            .await
            .result
            .unwrap();
        assert!(listed["jobs"]
            .as_array()
            .unwrap()
            .iter()
            .any(|j| j["job_id"] == id.as_str()));

        let stopped = handle(&serde_json::json!({ "action": "stop", "job_id": id })).await;
        let stopped = stopped.result.unwrap();
        assert_eq!(stopped["status"], "stopped");
        assert_eq!(stopped["stopped"], "user");
        assert!(stopped["output"].as_str().unwrap().contains("first"));
        // A second stop finds nothing running.
        assert!(!request_stop(&id));
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn a_tail_reads_from_an_offset_and_reports_the_exit() {
        let id = format!("job-tail-{}", std::process::id());
        let payload = serde_json::json!({ "command": "printf 'abcdef'; exit 4" });
        let _ = start(&payload, &id).await;
        for _ in 0..50 {
            if !is_running(&id) {
                break;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        let read = handle(&serde_json::json!({ "action": "tail", "job_id": id, "offset": 2 }))
            .await
            .result
            .unwrap();
        assert_eq!(read["output"], "cdef");
        assert_eq!(read["next_offset"], 6);
        assert_eq!(read["more"], false);
        assert_eq!(read["status"], "exited");
        assert_eq!(read["exit_code"], 4);
    }

    #[tokio::test]
    async fn an_unknown_job_is_named_in_the_error() {
        let outcome = handle(&serde_json::json!({ "action": "tail", "job_id": "nope" })).await;
        assert!(outcome.error.unwrap().contains("no job nope"));
    }
}
