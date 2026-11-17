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

//! Command executor - maps a [`Command`] kind to its capability and produces a
//! [`CommandOutcome`].
//!
//! Screenshot / input / file ops are synchronous OS calls and run in a blocking
//! task so they never stall the async transport. Shell and browser control are
//! async. Every command runs under an outer timeout derived from the server's
//! per-command budget so a hung capability yields a `timeout` result instead of
//! wedging the runner.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use tokio::task;
use tracing::warn;

use crate::capabilities::{files, jobs, shell};
#[cfg(feature = "desktop")]
use crate::capabilities::{input, screenshot};
use crate::host_facts;
use crate::protocol::{Command, CommandOutcome};

pub struct Executor {
    default_timeout: u64,
    /// One command at a time on this machine, whichever connection delivered
    /// it (a platform restart drops the socket while a command runs): OS input
    /// and captures must never overlap. Held for the execution only, not for
    /// the result upload that follows, which may retry for a while.
    turn: Arc<Semaphore>,
    #[cfg(feature = "browser")]
    cdp_url: String,
    #[cfg(feature = "browser")]
    browser: crate::capabilities::browser::BrowserController,
}

impl Executor {
    pub fn new(_cdp_url: String, default_timeout: u64) -> Self {
        Self {
            default_timeout,
            turn: Arc::new(Semaphore::new(1)),
            #[cfg(feature = "browser")]
            cdp_url: _cdp_url,
            #[cfg(feature = "browser")]
            browser: crate::capabilities::browser::BrowserController::new(),
        }
    }

    pub async fn run(&self, cmd: &Command) -> CommandOutcome {
        let budget = cmd.timeout.unwrap_or(self.default_timeout).max(1);
        // Outer ceiling: the command's budget plus a fixed grace for the
        // capability to wind down. The server's redelivery lease MUST exceed
        // this window plus the result-upload budget (see `_command_lease_seconds`
        // in backend/app/services/runner_transport.py, which mirrors this 30s as
        // its execution grace) so a still-running command is never reclaimed and
        // re-dispatched as a concurrent duplicate. The lease starts when the
        // command is delivered, so the ceiling covers the wait for the turn too.
        let ceiling = Duration::from_secs(budget + 30);
        // The end of the budget, from the same start as the ceiling: a blocking
        // capability the ceiling cannot stop (synthetic input) stops on it by
        // itself, so it is done before the command is reported as timed out.
        let deadline = Instant::now().checked_add(Duration::from_secs(budget));
        let started = AtomicBool::new(false);
        let work = async {
            // Never closed, so the wait always ends with the turn.
            let turn = self.turn.clone().acquire_owned().await.ok();
            started.store(true, Ordering::Relaxed);
            self.dispatch(cmd, budget, deadline, turn).await
        };
        match tokio::time::timeout(ceiling, work).await {
            Ok(outcome) => outcome,
            Err(_) if !started.load(Ordering::Relaxed) => CommandOutcome::timeout(format!(
                "command '{}' never started within its {budget}s budget: \
                 another command held this machine the whole time",
                cmd.kind
            )),
            Err(_) => CommandOutcome::timeout(format!(
                "command '{}' exceeded its {budget}s budget",
                cmd.kind
            )),
        }
    }

    /// `turn` is held until the capability is done: a blocking one takes it
    /// along, since the ceiling cannot stop it once it runs.
    async fn dispatch(
        &self,
        cmd: &Command,
        budget: u64,
        deadline: Option<Instant>,
        turn: Turn,
    ) -> CommandOutcome {
        let payload = cmd.payload.clone();
        match cmd.kind.as_str() {
            "screenshot" => self.screenshot(payload, turn).await,
            "computer_action" => self.computer_action(payload, deadline, turn).await,
            "read_file" => blocking(turn, move || files::read_file(&payload)).await,
            "write_file" => blocking(turn, move || files::write_file(&payload)).await,
            "list_dir" => blocking(turn, move || files::list_dir(&payload)).await,
            "run_shell" if is_background(&payload) => jobs::start(&payload, &cmd.id).await,
            "run_shell" => shell::execute(&payload, budget, Some(&cmd.id)).await,
            "shell_job" => jobs::handle(&payload).await,
            "host_facts" => CommandOutcome::completed(host_facts::collect_json().await),
            "browser_control" => self.browser_control(&payload).await,
            other => CommandOutcome::failed(format!("unsupported command kind '{other}'")),
        }
    }

    #[cfg(feature = "desktop")]
    async fn screenshot(&self, payload: serde_json::Value, turn: Turn) -> CommandOutcome {
        blocking(turn, move || screenshot::capture(&payload)).await
    }

    #[cfg(feature = "desktop")]
    async fn computer_action(
        &self,
        payload: serde_json::Value,
        deadline: Option<Instant>,
        turn: Turn,
    ) -> CommandOutcome {
        blocking(turn, move || input::execute(&payload, deadline)).await
    }

    // Headless flavor: these capabilities are not merely unavailable, they were
    // never linked. Say so in the outcome instead of failing obscurely, so the
    // operator learns the runner is the wrong flavor for the assignment rather
    // than chasing a permission or display problem.
    #[cfg(not(feature = "desktop"))]
    async fn screenshot(&self, _payload: serde_json::Value, _turn: Turn) -> CommandOutcome {
        CommandOutcome::failed(HEADLESS_NO_DESKTOP)
    }

    #[cfg(not(feature = "desktop"))]
    async fn computer_action(
        &self,
        _payload: serde_json::Value,
        _deadline: Option<Instant>,
        _turn: Turn,
    ) -> CommandOutcome {
        CommandOutcome::failed(HEADLESS_NO_DESKTOP)
    }

    #[cfg(feature = "browser")]
    async fn browser_control(&self, payload: &serde_json::Value) -> CommandOutcome {
        self.browser.execute(&self.cdp_url, payload).await
    }

    #[cfg(not(feature = "browser"))]
    async fn browser_control(&self, _payload: &serde_json::Value) -> CommandOutcome {
        CommandOutcome::failed(
            "This runner was built without browser control. Use the full-desktop \
             screenshot capability, or install a runner build with the 'browser' feature.",
        )
    }
}

#[cfg(not(feature = "desktop"))]
const HEADLESS_NO_DESKTOP: &str =
    "This runner is a headless build: it has no screen capture and no \
     synthetic input (it links no desktop stack, which is what lets it run on a \
     server). Assign shell / file / browser work here, or install the desktop \
     build on a machine with a graphical session.";

/// The executor's turn, held for as long as a command runs.
type Turn = Option<OwnedSemaphorePermit>;

/// `run_shell` with `background: true`: started as a job, answered at once.
fn is_background(payload: &serde_json::Value) -> bool {
    payload
        .get("background")
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
}

/// Run a synchronous capability on the blocking pool, mapping a panic / join
/// failure to a clean failed outcome. The closure keeps `turn` until it
/// returns: a ceiling that gives up on it only stops waiting, the closure
/// runs on, and the next command must not start beside it.
async fn blocking<F>(turn: Turn, f: F) -> CommandOutcome
where
    F: FnOnce() -> CommandOutcome + Send + 'static,
{
    match task::spawn_blocking(move || {
        let _turn = turn;
        f()
    })
    .await
    {
        Ok(outcome) => outcome,
        Err(err) => {
            warn!("blocking capability task failed: {err}");
            CommandOutcome::failed(format!("capability task failed: {err}"))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::OutcomeStatus;

    /// A command no capability serves: it fails at once and touches nothing.
    fn inert_command() -> Command {
        Command {
            id: "command-1".into(),
            kind: "no_such_capability".into(),
            payload: serde_json::Value::Null,
            timeout: Some(1),
        }
    }

    #[tokio::test]
    async fn a_command_waits_for_the_one_running_and_gives_its_turn_back_when_done() {
        let executor = Executor::new(String::new(), 5);
        // Another command is running on this machine, delivered over another
        // connection maybe: the next one does not start.
        let running = executor.turn.try_acquire().unwrap();
        let command = inert_command();
        let next = executor.run(&command);
        tokio::pin!(next);
        assert!(futures_util::poll!(&mut next).is_pending());
        drop(running);
        assert_eq!(next.await.status, OutcomeStatus::Failed);
        // The turn is back as soon as the command ran, before its result is
        // uploaded: the next command never waits on an upload.
        assert_eq!(executor.turn.available_permits(), 1);
    }

    #[tokio::test]
    async fn a_blocking_capability_given_up_on_keeps_the_turn_until_it_returns() {
        let turn = Arc::new(Semaphore::new(1));
        let held = turn.clone().acquire_owned().await.ok();
        let (release, wait) = std::sync::mpsc::channel::<()>();
        let capability = blocking(held, move || {
            let _ = wait.recv();
            CommandOutcome::failed("done")
        });
        // The ceiling gives up on it; the closure itself runs on.
        assert!(tokio::time::timeout(Duration::from_millis(50), capability)
            .await
            .is_err());
        assert_eq!(turn.available_permits(), 0);
        release.send(()).unwrap();
        let back = tokio::time::timeout(Duration::from_secs(10), turn.acquire()).await;
        assert!(back.is_ok(), "the turn never came back");
    }

    #[tokio::test(start_paused = true)]
    async fn the_wait_for_the_turn_counts_against_the_ceiling() {
        // The platform's lease starts at delivery: a command still queued when
        // its ceiling (budget + 30 s) ends must not run past it.
        let executor = Executor::new(String::new(), 5);
        let _running = executor.turn.try_acquire().unwrap();
        let started = tokio::time::Instant::now();
        let outcome = executor.run(&inert_command()).await;
        assert_eq!(outcome.status, OutcomeStatus::Timeout);
        assert!(outcome.error.unwrap().contains("never started"));
        assert_eq!(started.elapsed(), Duration::from_secs(1 + 30));
    }
}
