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

//! Event-driven transport - WebSocket fast path with an HTTP long-poll fallback.
//!
//! Mirrors `backend/app/services/runner_transport.py`:
//!
//! * Fast path: hold an outbound WebSocket, send `hello` (with host facts) on
//!   connect, absorb pushed `command` frames, execute each, upload the result
//!   over HTTP, and send periodic `heartbeat` frames so `last_seen_at` stays
//!   fresh (the authoritative online signal).
//! * Fallback: when the socket cannot be established, drain the same durable
//!   queue over the long-poll endpoint (which itself waits on a Redis wake, so
//!   latency matches the socket) and heartbeat over HTTP.
//! * Reconnect with capped exponential backoff; a rejected credential is fatal.

use std::future::Future;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{anyhow, Result};
use futures_util::{SinkExt, StreamExt};
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::Message;
use tracing::{debug, error, info, warn};

use crate::api::{is_transient, retry_delay, ApiClient};
use crate::config::RunnerConfig;
use crate::executor::Executor;
use crate::host_facts;
use crate::protocol::{ClientFrame, Command, ServerFrame};
use crate::status::{RunnerStatus, StatusReporter};
use crate::version::AGENT_VERSION;

/// A fatal, non-retryable transport error (a revoked / invalid credential).
#[derive(Debug)]
pub struct FatalAuthError(pub String);

impl std::fmt::Display for FatalAuthError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl std::error::Error for FatalAuthError {}

/// Run the transport forever: WS fast path, long-poll fallback, reconnecting
/// with backoff. Returns only on a fatal error (revoked credential) or when the
/// shutdown signal fires.
pub async fn run(
    config: RunnerConfig,
    token: String,
    executor: Arc<Executor>,
    mut shutdown: tokio::sync::watch::Receiver<bool>,
    updater: Option<Arc<crate::update::UpdateController>>,
    status: StatusReporter,
) -> Result<()> {
    let api = ApiClient::new(&config, token.clone())?;
    let mut backoff = Backoff::new();
    // Whether the server's last answer refused this version.
    let mut refused = false;

    loop {
        if *shutdown.borrow() {
            info!("shutdown requested; stopping transport");
            return Ok(());
        }

        let ws_url = config.ws_connect_url(AGENT_VERSION);
        let attempt = tokio::select! {
            r = serve_websocket(&ws_url, &token, &api, &executor, &config, &status, shutdown.clone()) => r,
            _ = shutdown.changed() => {
                if *shutdown.borrow() { return Ok(()); }
                continue;
            }
        };

        match attempt {
            Ok(WsOutcome::CleanClose) => {
                backoff.reset();
                refused = false;
                status.set(RunnerStatus::Reconnecting);
                info!("runner WebSocket closed; reconnecting");
            }
            Ok(WsOutcome::UpdateRequired) => {
                // The server refuses this version. Attempt a self-update now (a
                // mid-session min-version bump); the applier trips shutdown on
                // success so the next loop turn returns and main re-execs.
                refused = true;
                status.set(RunnerStatus::UpdateRequired);
                warn!("server requires a newer runner; attempting self-update");
                if let Some(u) = &updater {
                    u.maybe_apply().await;
                }
            }
            Ok(WsOutcome::NeverConnected(reason)) => {
                warn!("WebSocket unavailable ({reason}); using HTTP long-poll fallback");
                // Long-poll fallback until it is time to retry the WS fast path.
                let stint = backoff.long_poll_stint();
                if let Err(e) = serve_long_poll(
                    &api,
                    &executor,
                    &config,
                    &status,
                    stint,
                    &mut refused,
                    shutdown.clone(),
                )
                .await
                {
                    if let Some(fatal) = e.downcast_ref::<FatalAuthError>() {
                        error!("fatal: {fatal}");
                        return Err(e);
                    }
                    warn!("long-poll fallback error: {e:#}");
                }
            }
            Err(e) => {
                if let Some(fatal) = e.downcast_ref::<FatalAuthError>() {
                    error!("fatal: {fatal}");
                    return Err(e);
                }
                status.set(RunnerStatus::Reconnecting);
                warn!("WebSocket error: {e:#}");
            }
        }

        let delay = backoff.next();
        debug!("reconnecting in {:?}", delay);
        tokio::select! {
            _ = tokio::time::sleep(delay) => {}
            _ = shutdown.changed() => { if *shutdown.borrow() { return Ok(()); } }
        }
    }
}

enum WsOutcome {
    /// The socket connected and later closed cleanly.
    CleanClose,
    /// The socket could never be established (fall back to long-poll).
    NeverConnected(String),
    /// The server refused this runner as below the minimum supported version
    /// (an `update_required` frame or a 4426 close). Trigger a self-update
    /// instead of reconnect-storming.
    UpdateRequired,
}

async fn serve_websocket(
    ws_url: &str,
    token: &str,
    api: &ApiClient,
    executor: &Arc<Executor>,
    config: &RunnerConfig,
    status: &StatusReporter,
    shutdown: tokio::sync::watch::Receiver<bool>,
) -> Result<WsOutcome> {
    let request = match build_ws_request(ws_url, token) {
        Ok(request) => request,
        // A malformed request is a local bug, not a transient network fault; do
        // not treat it as fatal auth (that would stop the runner for good) - fall
        // back to long-poll and retry.
        Err(err) => return Ok(WsOutcome::NeverConnected(format!("ws request: {err}"))),
    };
    let (ws_stream, _resp) = match tokio_tungstenite::connect_async(request).await {
        Ok(ok) => ok,
        Err(err) => {
            if let Some(fatal) = classify_ws_error(&err) {
                return Err(anyhow!(FatalAuthError(fatal)));
            }
            return Ok(WsOutcome::NeverConnected(err.to_string()));
        }
    };
    status.set(RunnerStatus::Online {
        transport: "websocket".into(),
    });
    info!("runner connected (WebSocket fast path)");

    let (mut sink, mut stream) = ws_stream.split();
    let (out_tx, mut out_rx) = mpsc::channel::<Message>(32);

    // Writer task owns the sink; every outgoing frame (hello, heartbeat, pong,
    // ack) flows through the channel so the read loop and heartbeat never race.
    let writer = tokio::spawn(async move {
        while let Some(msg) = out_rx.recv().await {
            if sink.send(msg).await.is_err() {
                break;
            }
        }
        let _ = sink.close().await;
    });

    // Hello: report host facts and ask the server to drain queued commands.
    let facts = host_facts::collect_json_cached().await;
    let hello = ClientFrame::Hello {
        host_facts: facts,
        agent_version: AGENT_VERSION.to_string(),
    };
    if out_tx
        .send(Message::Text(serde_json::to_string(&hello)?))
        .await
        .is_err()
    {
        return Ok(WsOutcome::CleanClose);
    }

    // Heartbeat task.
    let hb_tx = out_tx.clone();
    let hb_interval = config.heartbeat_interval_seconds.max(5);
    let heartbeat = tokio::spawn(async move {
        let mut ticker = tokio::time::interval(Duration::from_secs(hb_interval));
        ticker.tick().await; // fire immediately-then-interval; skip the first
        loop {
            ticker.tick().await;
            let facts = host_facts::collect_json_cached().await;
            let frame = ClientFrame::Heartbeat {
                host_facts: facts,
                agent_version: AGENT_VERSION.to_string(),
            };
            match serde_json::to_string(&frame) {
                Ok(text) => {
                    if hb_tx.send(Message::Text(text)).await.is_err() {
                        break;
                    }
                }
                Err(_) => break,
            }
        }
    });

    let result = read_loop(&mut stream, &out_tx, api, executor, shutdown).await;

    heartbeat.abort();
    drop(out_tx);
    let _ = writer.await;
    result
}

async fn read_loop(
    stream: &mut (impl futures_util::Stream<Item = Result<Message, tokio_tungstenite::tungstenite::Error>>
              + Unpin),
    out_tx: &mpsc::Sender<Message>,
    api: &ApiClient,
    executor: &Arc<Executor>,
    mut shutdown: tokio::sync::watch::Receiver<bool>,
) -> Result<WsOutcome> {
    loop {
        let msg = tokio::select! {
            m = stream.next() => m,
            _ = shutdown.changed() => {
                if *shutdown.borrow() { return Ok(WsOutcome::CleanClose); }
                continue;
            }
        };
        let Some(msg) = msg else {
            return Ok(WsOutcome::CleanClose);
        };
        let msg = match msg {
            Ok(m) => m,
            Err(e) => return Err(anyhow!("socket read: {e}")),
        };
        match msg {
            Message::Text(text) => match parse_frame(&text) {
                Some(ServerFrame::Command { command }) => {
                    spawn_command(command, out_tx.clone(), api.clone(), executor.clone());
                }
                Some(ServerFrame::Stop { command_id }) => {
                    // A late stop (the command already exited) finds nothing.
                    // A background job is stopped by the id of the command
                    // that started it (its job id).
                    if !crate::capabilities::shell::request_stop(&command_id)
                        && !crate::capabilities::jobs::request_stop(&command_id)
                    {
                        debug!("stop for {command_id}: not running here");
                    }
                }
                Some(ServerFrame::UpdateRequired { min_version, .. }) => {
                    warn!(
                        "server requires a newer runner (min {})",
                        min_version.as_deref().unwrap_or("?")
                    );
                    return Ok(WsOutcome::UpdateRequired);
                }
                _ => {}
            },
            Message::Binary(_) => { /* control frames only */ }
            Message::Ping(payload) => {
                let _ = out_tx.send(Message::Pong(payload)).await;
            }
            Message::Pong(_) => {}
            Message::Close(frame) => {
                if let Some(frame) = frame {
                    if matches!(frame.code, CloseCode::Library(4401)) {
                        return Err(anyhow!(FatalAuthError(
                            "runner credential rejected by server (revoked?)".into()
                        )));
                    }
                    if matches!(frame.code, CloseCode::Library(4404)) {
                        return Err(anyhow!(FatalAuthError(
                            "runners are disabled on this server".into()
                        )));
                    }
                    // 4426 (mirrors HTTP 426 Upgrade Required): version too old.
                    if matches!(frame.code, CloseCode::Library(4426)) {
                        return Ok(WsOutcome::UpdateRequired);
                    }
                }
                return Ok(WsOutcome::CleanClose);
            }
            Message::Frame(_) => {}
        }
    }
}

fn parse_frame(text: &str) -> Option<ServerFrame> {
    match serde_json::from_str::<ServerFrame>(text) {
        Ok(frame) => Some(frame),
        Err(e) => {
            debug!("ignoring unparseable server frame: {e}");
            None
        }
    }
}

/// Execute a command off the read loop so long captures never block receiving
/// new commands or sending heartbeats. The executor runs one command at a time
/// (OS input / capture never overlap); the result upload runs after its turn.
fn spawn_command(
    cmd: Command,
    out_tx: mpsc::Sender<Message>,
    api: ApiClient,
    executor: Arc<Executor>,
) {
    tokio::spawn(async move {
        let command_id = cmd.id.clone();
        debug!("executing command {} ({})", command_id, cmd.kind);
        let outcome = executor.run(&cmd).await;
        if let Err(e) = api.submit_result(&command_id, outcome).await {
            warn!("failed to upload result for {command_id}: {e:#}");
        }
        let ack = ClientFrame::Ack { command_id };
        if let Ok(text) = serde_json::to_string(&ack) {
            let _ = out_tx.send(Message::Text(text)).await;
        }
    });
}

/// HTTP long-poll fallback: claim + execute commands and heartbeat, for a
/// bounded stint, then return so the caller retries the WS fast path.
/// `refused` carries a refusal of this version from one stint to the next: a
/// stint started under one reads Online only once a poll is served again.
async fn serve_long_poll(
    api: &ApiClient,
    executor: &Arc<Executor>,
    config: &RunnerConfig,
    status: &StatusReporter,
    stint: Duration,
    refused: &mut bool,
    mut shutdown: tokio::sync::watch::Receiver<bool>,
) -> Result<()> {
    if !*refused {
        status.set(long_poll_online());
    }
    info!("runner connected (HTTP long-poll fallback)");
    let deadline = tokio::time::Instant::now() + stint;

    // Heartbeat task over HTTP. A transient failure (the platform restarting)
    // is retried within the interval instead of waiting a whole tick, so a
    // short outage does not age `last_seen_at` past the offline window.
    let hb_api = api.clone();
    let hb_interval = Duration::from_secs(config.heartbeat_interval_seconds.max(5));
    let heartbeat = tokio::spawn(async move {
        let mut ticker = tokio::time::interval(hb_interval);
        ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            ticker.tick().await;
            let facts = host_facts::collect_json_cached().await;
            if let Err(e) = beat(hb_interval, || hb_api.heartbeat(&facts, AGENT_VERSION)).await {
                debug!("long-poll heartbeat failed: {e:#}");
            }
        }
    });
    // Kick an immediate heartbeat so host facts land before the first poll.
    let facts = host_facts::collect_json_cached().await;
    let _ = api.heartbeat(&facts, AGENT_VERSION).await;

    let mut poll_retry = PollRetry::default();
    let result = loop {
        if *shutdown.borrow() || tokio::time::Instant::now() >= deadline {
            break Ok(());
        }
        let poll = tokio::select! {
            r = api.poll() => r,
            _ = shutdown.changed() => { if *shutdown.borrow() { break Ok(()); } continue; }
            _ = tokio::time::sleep_until(deadline) => break Ok(()),
        };
        match poll {
            Ok(resp) => {
                if let Some(now) =
                    answered_poll_status(resp.update_required, poll_retry.answered(), refused)
                {
                    status.set(now);
                }
                if resp.update_required {
                    // Too old to be served commands: say so, as the WebSocket
                    // path does, rather than keep reading "Online (fallback)".
                    // The updaters apply the update (the headless startup
                    // pre-check and periodic check, the desktop bundle
                    // updater); end the stint so we don't hot-loop the poll
                    // while waiting.
                    warn!("server requires a newer runner; pausing long-poll until update");
                    break Ok(());
                }
                if let Some(cmd) = resp.command {
                    // Execute (the executor runs one command at a time), then
                    // upload the result. Like the WS spawn path, but there is
                    // no socket to ack over.
                    let api2 = api.clone();
                    let exec2 = executor.clone();
                    tokio::spawn(async move {
                        let id = cmd.id.clone();
                        let outcome = exec2.run(&cmd).await;
                        if let Err(e) = api2.submit_result(&id, outcome).await {
                            warn!("failed to upload result for {id}: {e:#}");
                        }
                    });
                }
                // else: timeout with no command; re-poll immediately.
            }
            Err(e) => {
                if e.to_string().contains("revoked") {
                    break Err(anyhow!(FatalAuthError(e.to_string())));
                }
                // A platform restart (503 + Retry-After, a proxy 502 / 504, no
                // answer at all) is expected during a deploy: back off with
                // jitter so a fleet does not re-poll in lock-step, show the
                // machine as reconnecting until a poll is answered again, and
                // do not report it as an error.
                let (wait, first_failure) = poll_retry.failed(&e);
                if first_failure {
                    status.set(RunnerStatus::Reconnecting);
                }
                if is_transient(&e) {
                    debug!("long-poll unavailable ({e:#}); retrying in {wait:?}");
                } else {
                    warn!("long-poll error: {e:#}");
                }
                tokio::select! {
                    _ = tokio::time::sleep(wait) => {}
                    _ = shutdown.changed() => { if *shutdown.borrow() { break Ok(()); } }
                    _ = tokio::time::sleep_until(deadline) => break Ok(()),
                }
            }
        }
    };

    heartbeat.abort();
    result
}

/// Build the WS handshake request carrying the `run-` credential in an
/// `Authorization: Bearer` header rather than the URL query string, so a
/// credential granting OS control never lands in a proxy / access-log URL.
/// `into_client_request` fills in all the required upgrade headers; we only add
/// the bearer on top and mark it sensitive so it is redacted from logs.
fn build_ws_request(
    ws_url: &str,
    token: &str,
) -> Result<tokio_tungstenite::tungstenite::handshake::client::Request> {
    use tokio_tungstenite::tungstenite::client::IntoClientRequest;
    use tokio_tungstenite::tungstenite::http::header::{HeaderValue, AUTHORIZATION};

    let mut request = ws_url.into_client_request()?;
    let mut bearer = HeaderValue::from_str(&format!("Bearer {token}"))?;
    bearer.set_sensitive(true);
    request.headers_mut().insert(AUTHORIZATION, bearer);
    Ok(request)
}

/// Detect a fatal auth failure from a WS connect error (HTTP 401 on upgrade).
fn classify_ws_error(err: &tokio_tungstenite::tungstenite::Error) -> Option<String> {
    if let tokio_tungstenite::tungstenite::Error::Http(resp) = err {
        if resp.status() == 401 {
            return Some("runner credential rejected by server (revoked?)".into());
        }
        if resp.status() == 404 {
            return Some("runners are disabled on this server".into());
        }
    }
    None
}

/// Back-off after a failed long-poll request (the platform restarting).
const POLL_RETRY_BASE: Duration = Duration::from_secs(1);
const POLL_RETRY_CAP: Duration = Duration::from_secs(30);

/// Back-off for a failed HTTP heartbeat, retried within one interval.
const HB_RETRY_BASE: Duration = Duration::from_secs(1);
const HB_RETRY_CAP: Duration = Duration::from_secs(8);

/// Back-off between failed long-polls, reset by the next poll the platform
/// answers.
#[derive(Default)]
struct PollRetry {
    failures: u32,
}

impl PollRetry {
    /// A poll failed: how long to wait before the next one, and whether this
    /// failure starts a run of them (the machine just stopped being served).
    fn failed(&mut self, err: &anyhow::Error) -> (Duration, bool) {
        let wait = retry_delay(err, self.failures, POLL_RETRY_BASE, POLL_RETRY_CAP);
        let first = self.failures == 0;
        self.failures = self.failures.saturating_add(1);
        (wait, first)
    }

    /// The platform answered a poll: whether that ends a run of failures.
    fn answered(&mut self) -> bool {
        std::mem::take(&mut self.failures) > 0
    }
}

/// The one status an answered long-poll publishes: `UpdateRequired` when it
/// refuses this version (kept in `refused` for the next stints), even when it
/// ends a run of failed polls; Online when it serves the version after a
/// refusal or a run of failed polls (`recovered`); nothing otherwise.
fn answered_poll_status(
    update_required: bool,
    recovered: bool,
    refused: &mut bool,
) -> Option<RunnerStatus> {
    if update_required {
        *refused = true;
        return Some(RunnerStatus::UpdateRequired);
    }
    let was_refused = std::mem::take(refused);
    (was_refused || recovered).then(long_poll_online)
}

fn long_poll_online() -> RunnerStatus {
    RunnerStatus::Online {
        transport: "long_poll".into(),
    }
}

/// Send one HTTP heartbeat, retrying a transient failure until `interval` has
/// passed since the beat began; the next beat takes over from there. Each
/// attempt ends with the interval too, so a hung request never pushes the
/// retries, or the next beat, past it.
async fn beat<F, Fut>(interval: Duration, mut send: F) -> Result<()>
where
    F: FnMut() -> Fut,
    Fut: Future<Output = Result<()>>,
{
    let started = tokio::time::Instant::now();
    let mut attempt: u32 = 0;
    loop {
        let left = interval.saturating_sub(started.elapsed());
        let err = match tokio::time::timeout(left, send()).await {
            Ok(Ok(())) => return Ok(()),
            Ok(Err(e)) => e,
            Err(_) => return Err(anyhow!("no answer within the heartbeat interval")),
        };
        let wait = retry_delay(&err, attempt, HB_RETRY_BASE, HB_RETRY_CAP);
        if !is_transient(&err) || started.elapsed() + wait >= interval {
            return Err(err);
        }
        debug!("long-poll heartbeat failed ({err:#}); retrying in {wait:?}");
        tokio::time::sleep(wait).await;
        attempt = attempt.saturating_add(1);
    }
}

/// Capped exponential backoff for reconnects.
struct Backoff {
    attempt: u32,
}

impl Backoff {
    fn new() -> Self {
        Self { attempt: 0 }
    }
    fn reset(&mut self) {
        self.attempt = 0;
    }
    fn next(&mut self) -> Duration {
        let secs = 2u64.saturating_pow(self.attempt.min(5)).min(60);
        self.attempt = self.attempt.saturating_add(1);
        Duration::from_secs(secs.max(1))
    }
    /// How long to stay on the long-poll fallback before retrying WS.
    fn long_poll_stint(&self) -> Duration {
        Duration::from_secs(120)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::api::Unavailable;
    use reqwest::StatusCode;
    use tokio::time::Instant;

    fn restart(retry_after_secs: Option<u64>) -> anyhow::Error {
        anyhow!(Unavailable {
            status: StatusCode::SERVICE_UNAVAILABLE,
            retry_after: retry_after_secs.map(Duration::from_secs),
        })
    }

    const INTERVAL: Duration = Duration::from_secs(30);

    // The heartbeat retry, on tokio's paused clock.

    #[tokio::test(start_paused = true)]
    async fn a_heartbeat_rides_out_a_restart_within_its_interval() {
        let started = Instant::now();
        let mut calls = 0;
        let result = beat(INTERVAL, || {
            calls += 1;
            let answer = if calls <= 2 {
                Err(restart(Some(1)))
            } else {
                Ok(())
            };
            async move { answer }
        })
        .await;
        assert!(result.is_ok(), "{result:?}");
        assert_eq!(calls, 3);
        assert!(started.elapsed() < INTERVAL);
    }

    #[tokio::test(start_paused = true)]
    async fn a_refused_heartbeat_is_not_retried() {
        let mut calls = 0;
        let result = beat(INTERVAL, || {
            calls += 1;
            async { Err::<(), _>(anyhow!("heartbeat status: 403 Forbidden")) }
        })
        .await;
        assert!(result.is_err());
        assert_eq!(calls, 1);
    }

    #[tokio::test(start_paused = true)]
    async fn heartbeat_retries_end_before_the_next_beat() {
        // The platform stays down: the beat gives up inside its interval, so
        // the next tick sends a fresh heartbeat instead of queueing behind it.
        let started = Instant::now();
        let mut calls = 0;
        let result = beat(INTERVAL, || {
            calls += 1;
            async { Err::<(), _>(restart(None)) }
        })
        .await;
        assert!(result.is_err());
        assert!(calls > 1, "{calls}");
        assert!(started.elapsed() < INTERVAL);
    }

    #[tokio::test(start_paused = true)]
    async fn a_hung_heartbeat_ends_with_its_interval() {
        let started = Instant::now();
        let result = beat(INTERVAL, std::future::pending::<Result<()>>).await;
        assert!(result.is_err());
        assert_eq!(started.elapsed(), INTERVAL);
    }

    // The long-poll back-off and what it tells the status.

    #[test]
    fn a_failed_poll_backs_off_and_an_answer_resets_it() {
        let mut poll = PollRetry::default();
        let err = anyhow!("connection refused");
        let (wait, first) = poll.failed(&err);
        assert!(first, "the first failure starts a run");
        assert!(
            (POLL_RETRY_BASE / 2..=POLL_RETRY_BASE).contains(&wait),
            "{wait:?}"
        );
        let mut waits = Vec::new();
        for _ in 0..10 {
            let (wait, first) = poll.failed(&err);
            assert!(!first);
            waits.push(wait);
        }
        assert!(waits.iter().all(|w| *w <= POLL_RETRY_CAP), "{waits:?}");
        assert!(waits[9] >= POLL_RETRY_CAP / 2, "{waits:?}");
        // The next answer ends the run once, and the back-off starts over.
        assert!(poll.answered());
        assert!(!poll.answered());
        let (wait, first) = poll.failed(&err);
        assert!(first);
        assert!(wait <= POLL_RETRY_BASE, "{wait:?}");
    }

    #[test]
    fn a_failed_poll_honours_retry_after() {
        let mut poll = PollRetry::default();
        let (wait, _) = poll.failed(&restart(Some(5)));
        assert!(wait >= Duration::from_secs(5), "{wait:?}");
    }

    #[test]
    fn an_answered_poll_publishes_one_status() {
        let mut refused = false;
        // Served, as the previous answer was: nothing new to say.
        assert_eq!(answered_poll_status(false, false, &mut refused), None);
        // Refused, the answer that ends a run of failed polls (a platform
        // restart) included: Update required alone, never Online first.
        for recovered in [false, true] {
            assert_eq!(
                answered_poll_status(true, recovered, &mut refused),
                Some(RunnerStatus::UpdateRequired)
            );
            assert!(refused);
        }
        // Served again: Online, once.
        assert_eq!(
            answered_poll_status(false, false, &mut refused),
            Some(long_poll_online())
        );
        assert!(!refused);
        assert_eq!(answered_poll_status(false, false, &mut refused), None);
        // Served after a run of failed polls.
        assert_eq!(
            answered_poll_status(false, true, &mut refused),
            Some(long_poll_online())
        );
    }

    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    /// A loopback HTTP server that answers each request with the JSON body of
    /// the first route its path starts with (`{}` for any other path).
    async fn serve_json(routes: &'static [(&'static str, &'static str)]) -> String {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        tokio::spawn(async move {
            while let Ok((mut socket, _)) = listener.accept().await {
                tokio::spawn(async move {
                    let mut request = Vec::new();
                    let mut chunk = [0u8; 4096];
                    let head_len = loop {
                        if let Some(at) = request.windows(4).position(|w| w == b"\r\n\r\n") {
                            break at + 4;
                        }
                        match socket.read(&mut chunk).await {
                            Ok(0) | Err(_) => return,
                            Ok(n) => request.extend_from_slice(&chunk[..n]),
                        }
                    };
                    let head = String::from_utf8_lossy(&request[..head_len]).into_owned();
                    let body_len: usize = head
                        .lines()
                        .find_map(|line| {
                            let (name, value) = line.split_once(':')?;
                            if name.eq_ignore_ascii_case("content-length") {
                                value.trim().parse().ok()
                            } else {
                                None
                            }
                        })
                        .unwrap_or(0);
                    // Read the whole request before answering and closing.
                    while request.len() < head_len + body_len {
                        match socket.read(&mut chunk).await {
                            Ok(0) | Err(_) => return,
                            Ok(n) => request.extend_from_slice(&chunk[..n]),
                        }
                    }
                    let path = head.split_whitespace().nth(1).unwrap_or_default();
                    let body = routes
                        .iter()
                        .find(|(route, _)| path.starts_with(route))
                        .map_or("{}", |(_, body)| body);
                    let response = format!(
                        "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n\
                         content-length: {}\r\nconnection: close\r\n\r\n{body}",
                        body.len()
                    );
                    let _ = socket.write_all(response.as_bytes()).await;
                    let _ = socket.shutdown().await;
                });
            }
        });
        base
    }

    /// One long-poll stint against `server_url`.
    async fn long_poll_stint(
        server_url: &str,
        status: &StatusReporter,
        refused: &mut bool,
        stint: Duration,
    ) {
        let config: RunnerConfig = serde_json::from_value(serde_json::json!({
            "server_url": server_url,
            "runner_id": "3f2a",
            "platform": "linux",
        }))
        .unwrap();
        let api = ApiClient::new(&config, "run-test".into()).unwrap();
        let executor = Arc::new(Executor::new(String::new(), 120));
        let (_stop, shutdown) = tokio::sync::watch::channel(false);
        serve_long_poll(&api, &executor, &config, status, stint, refused, shutdown)
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn a_refused_version_reads_update_required_on_the_long_poll_fallback() {
        crate::host_facts::seed_cache_for_tests(serde_json::json!({})).await;
        let refusing = serve_json(&[
            (
                "/api/v1/runners/poll",
                r#"{"command":null,"update_required":true}"#,
            ),
            (
                "/api/v1/runners/heartbeat",
                r#"{"ok":true,"update_required":true}"#,
            ),
        ])
        .await;
        let (status_tx, mut status) = tokio::sync::watch::channel(RunnerStatus::Connecting);
        let reporter = StatusReporter::new(status_tx);
        let mut refused = false;
        let long = Duration::from_secs(60);
        long_poll_stint(&refusing, &reporter, &mut refused, long).await;
        // No command reaches this version until it updates: the tray and the
        // panel must not read "Online (fallback)".
        assert_eq!(*status.borrow_and_update(), RunnerStatus::UpdateRequired);

        // The next stint, once the WebSocket failed again: still refused, and
        // never "Online (fallback)" on the way.
        let published = tokio::spawn(async move {
            let mut seen = Vec::new();
            while status.changed().await.is_ok() {
                let now = status.borrow_and_update().clone();
                let refused_again = now == RunnerStatus::UpdateRequired;
                seen.push(now);
                if refused_again {
                    break;
                }
            }
            (seen, status)
        });
        long_poll_stint(&refusing, &reporter, &mut refused, long).await;
        let (seen, mut status) = tokio::time::timeout(Duration::from_secs(30), published)
            .await
            .expect("the stint published no status")
            .unwrap();
        assert_eq!(seen, [RunnerStatus::UpdateRequired]);

        // Served again (the minimum version was lowered): online.
        let serving = serve_json(&[("/api/v1/runners/poll", r#"{"command":null}"#)]).await;
        long_poll_stint(
            &serving,
            &reporter,
            &mut refused,
            Duration::from_millis(100),
        )
        .await;
        assert_eq!(
            *status.borrow_and_update(),
            RunnerStatus::Online {
                transport: "long_poll".into()
            }
        );
        assert!(!refused);
    }
}
