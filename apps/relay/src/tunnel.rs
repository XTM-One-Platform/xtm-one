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

//! The relay's connection to the platform - always outbound.
//!
//! * Fast path: a WebSocket (`/api/v1/relays/connect`). The relay sends its
//!   `hello`, the platform answers `hello_ack`, then pushes `open` frames while
//!   the relay has room, plus the `data` / `window` / `cancel` frames of the
//!   requests it holds; the relay sends the answers back on the same socket and
//!   a `heartbeat` frame every interval.
//! * Fallback: HTTP long-poll (`/hello`, `/poll`, `/frames`, `/heartbeat`), the
//!   same frames over plain requests - behind an upstream proxy, or wherever
//!   the WebSocket cannot be opened. In `auto` mode the relay goes back to the
//!   WebSocket after each long-poll stint, once its requests are answered.
//! * Reconnects with capped, jittered back-off; a refusal (revoked credential,
//!   relays disabled, version too old) is waited out instead of hammered.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use agent_core::retry::{backoff_delay, jitter, retry_delay, retry_within};
use anyhow::anyhow;
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::sync::{mpsc, watch};
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::Message;
use tracing::{debug, error, info, warn};

use crate::api::{PlatformApi, Refusal};
use crate::config::{Config, TransportMode};
use crate::dispatch::Dispatcher;
use crate::forward::{Forwarder, Limits};
use crate::frame::{decode_frames, encode_frames, Frame, MAX_UPLOAD_BODY_BYTES};
use crate::status::{State, StatusReporter};
use crate::tls::TlsConfigs;
use crate::update::{UpdateOutcome, Updater};
use crate::version::{user_agent, AGENT_VERSION, CAPABILITIES, PROTOCOL_VERSION};

/// How a tunnel run ended.
#[derive(Debug, PartialEq, Eq)]
pub enum Outcome {
    Shutdown,
    /// A newer build replaced this executable; the caller restarts it.
    Updated,
}

/// How one connection ended.
enum End {
    Closed(String),
    NeverConnected(String),
    Refused(Refusal),
    Shutdown,
    /// The allow-list changed: reconnect so the hello reports it.
    Rehello,
    /// A long-poll stint is over: try the WebSocket again.
    Switch,
    Failed(anyhow::Error),
}

/// A connection that stayed up this long resets the back-off.
const STABLE_CONNECTION: Duration = Duration::from_secs(60);

const RECONNECT_BASE: Duration = Duration::from_secs(1);
const RECONNECT_CAP: Duration = Duration::from_secs(60);
/// How long a refusal is waited out before asking again.
const REFUSAL_WAIT: Duration = Duration::from_secs(300);
/// How long a long-poll stint lasts before `auto` tries the WebSocket again.
const POLL_STINT_FIRST: Duration = Duration::from_secs(300);
const POLL_STINT_CAP: Duration = Duration::from_secs(1800);
/// How long requests in flight are given to finish when switching transport.
const DRAIN_LIMIT: Duration = Duration::from_secs(60);
/// The batch of frames sent at once.
const BATCH_BYTES: usize = 4 * 1024 * 1024;

pub struct Tunnel {
    config: Config,
    api: PlatformApi,
    tls: TlsConfigs,
    forwarder: Arc<Forwarder>,
    status: Arc<StatusReporter>,
    updater: Arc<Updater>,
    rehello: watch::Receiver<u64>,
    heartbeat: Duration,
    online_since: Option<Instant>,
}

impl Tunnel {
    pub fn new(
        config: Config,
        api: PlatformApi,
        tls: TlsConfigs,
        forwarder: Arc<Forwarder>,
        status: Arc<StatusReporter>,
        updater: Arc<Updater>,
        rehello: watch::Receiver<u64>,
    ) -> Self {
        Self {
            config,
            api,
            tls,
            forwarder,
            status,
            updater,
            rehello,
            heartbeat: Duration::from_secs(30),
            online_since: None,
        }
    }

    fn online(&mut self, transport: &str) {
        self.online_since = Some(Instant::now());
        self.status.set(State::Online, Some(transport), None);
    }

    fn hello(&self) -> Value {
        let policy = self.forwarder.policy();
        let destinations: Vec<&String> = policy.declared().iter().take(200).collect();
        json!({
            "type": "hello",
            "protocol": PROTOCOL_VERSION,
            "capabilities": CAPABILITIES,
            "destinations": destinations,
            "max_concurrent": self.config.max_concurrent_requests,
            "version": AGENT_VERSION,
            "host_facts": crate::host::facts(&self.config),
        })
    }

    fn apply_ack(&mut self, ack: &Value) {
        self.forwarder.set_limits(Limits::from_ack(ack));
        if let Some(seconds) = ack.get("heartbeat_interval").and_then(Value::as_u64) {
            self.heartbeat = Duration::from_secs(seconds.clamp(5, 300));
        }
        if let Some(seconds) = ack.get("long_poll_timeout").and_then(Value::as_u64) {
            self.api.set_poll_timeout(seconds);
        }
    }

    fn max_concurrent(&self) -> usize {
        self.config.max_concurrent_requests as usize
    }

    /// Serve until shutdown (or a self-update that needs a restart).
    pub async fn run(mut self, mut shutdown: watch::Receiver<bool>) -> Outcome {
        let mut failures: u32 = 0;
        let mut poll_until: Option<Instant> = None;
        let mut stint = POLL_STINT_FIRST;
        loop {
            if *shutdown.borrow() {
                return Outcome::Shutdown;
            }
            let websocket = match self.config.transport {
                TransportMode::LongPoll => false,
                TransportMode::Websocket => true,
                TransportMode::Auto => {
                    self.config.upstream_proxy.is_none()
                        && poll_until.is_none_or(|until| Instant::now() >= until)
                }
            };
            self.status.set(
                if failures == 0 {
                    State::Connecting
                } else {
                    State::Reconnecting
                },
                None,
                None,
            );
            let end = if websocket {
                self.websocket(&mut shutdown).await
            } else {
                let limit = match (self.config.transport, poll_until) {
                    (TransportMode::Auto, Some(until)) if self.config.upstream_proxy.is_none() => {
                        Some(until.saturating_duration_since(Instant::now()))
                    }
                    _ => None,
                };
                self.long_poll(limit, &mut shutdown).await
            };
            if self
                .online_since
                .take()
                .is_some_and(|since| since.elapsed() >= STABLE_CONNECTION)
            {
                failures = 0;
            }
            let wait = match end {
                End::Shutdown => return Outcome::Shutdown,
                End::Rehello => {
                    info!("allowed destinations changed; reconnecting to report them");
                    failures = 0;
                    Duration::ZERO
                }
                End::Switch => {
                    poll_until = None;
                    Duration::ZERO
                }
                End::Closed(reason) => {
                    info!("connection to the platform closed ({reason}); reconnecting");
                    self.status.set(State::Reconnecting, None, Some(reason));
                    failures = failures.saturating_add(1);
                    backoff_delay(failures.min(3), RECONNECT_BASE, RECONNECT_CAP, jitter())
                }
                End::NeverConnected(reason) if self.config.transport == TransportMode::Auto => {
                    warn!(
                        "WebSocket unavailable ({reason}); using HTTP long-poll for {}s",
                        stint.as_secs()
                    );
                    poll_until = Some(Instant::now() + stint);
                    stint = (stint * 2).min(POLL_STINT_CAP);
                    Duration::ZERO
                }
                End::NeverConnected(reason) => {
                    warn!("could not connect to the platform: {reason}");
                    self.status.set(State::Reconnecting, None, Some(reason));
                    failures = failures.saturating_add(1);
                    backoff_delay(failures, RECONNECT_BASE, RECONNECT_CAP, jitter())
                }
                End::Failed(err) => {
                    warn!("connection to the platform failed: {err:#}");
                    self.status
                        .set(State::Reconnecting, None, Some(format!("{err:#}")));
                    failures = failures.saturating_add(1);
                    backoff_delay(failures, RECONNECT_BASE, RECONNECT_CAP, jitter())
                }
                End::Refused(refusal) => match self.refused(refusal).await {
                    Some(outcome) => return outcome,
                    None => REFUSAL_WAIT,
                },
            };
            if poll_until.is_some_and(|until| Instant::now() >= until) {
                poll_until = None;
            }
            if !wait.is_zero() {
                debug!("reconnecting in {wait:?}");
                tokio::select! {
                    _ = tokio::time::sleep(wait) => {}
                    _ = shutdown.changed() => {}
                }
            }
        }
    }

    /// Record a refusal; `Some` when it ends the run.
    async fn refused(&self, refusal: Refusal) -> Option<Outcome> {
        match &refusal {
            Refusal::Revoked => {
                error!(
                    "the platform refused this relay's credential: it was revoked, or the relay \
                     was re-enrolled elsewhere. Run `xtm-one-relay enroll` with a new key."
                );
                self.status
                    .set(State::Revoked, None, Some(refusal.to_string()));
            }
            Refusal::Disabled => {
                warn!("relays are disabled on this platform; asking again in 5 minutes");
                self.status
                    .set(State::Disabled, None, Some(refusal.to_string()));
            }
            Refusal::UpdateRequired => {
                warn!("the platform requires a newer relay; checking for an update");
                self.status
                    .set(State::UpdateRequired, None, Some(refusal.to_string()));
                match self.updater.check_and_apply().await {
                    UpdateOutcome::Applied(_) => return Some(Outcome::Updated),
                    UpdateOutcome::Available { version, reason } => {
                        error!("relay {version} is required but not applied: {reason}");
                    }
                    UpdateOutcome::UpToDate => {}
                    UpdateOutcome::Failed(why) => error!("relay update failed: {why}"),
                }
            }
            Refusal::Refused(why) => {
                error!("the platform refused this relay: {why}");
                self.status
                    .set(State::Reconnecting, None, Some(why.clone()));
            }
        }
        None
    }

    // -- WebSocket ----------------------------------------------------------

    async fn websocket(&mut self, shutdown: &mut watch::Receiver<bool>) -> End {
        use tokio_tungstenite::tungstenite::client::IntoClientRequest;
        use tokio_tungstenite::tungstenite::http::header::{
            HeaderValue, AUTHORIZATION, USER_AGENT,
        };
        use tokio_tungstenite::tungstenite::protocol::WebSocketConfig;

        let mut request = match self.api.ws_url().into_client_request() {
            Ok(request) => request,
            Err(e) => return End::NeverConnected(format!("WebSocket request: {e}")),
        };
        match HeaderValue::from_str(&format!("Bearer {}", self.api.token())) {
            Ok(mut bearer) => {
                bearer.set_sensitive(true);
                request.headers_mut().insert(AUTHORIZATION, bearer);
            }
            Err(_) => return End::Failed(anyhow!("the relay credential is not a valid header")),
        }
        if let Ok(agent) = HeaderValue::from_str(&user_agent()) {
            request.headers_mut().insert(USER_AGENT, agent);
        }
        let ws_config = WebSocketConfig {
            max_message_size: Some(16 * 1024 * 1024),
            max_frame_size: Some(16 * 1024 * 1024),
            ..WebSocketConfig::default()
        };
        let connect = tokio_tungstenite::connect_async_tls_with_config(
            request,
            Some(ws_config),
            false,
            Some(tokio_tungstenite::Connector::Rustls(
                self.tls.verified.clone(),
            )),
        );
        let ws = match tokio::time::timeout(Duration::from_secs(30), connect).await {
            Ok(Ok((ws, _))) => ws,
            Ok(Err(tokio_tungstenite::tungstenite::Error::Http(resp))) => {
                // The platform refuses a bad credential or disabled relays
                // before the upgrade, which reaches us as a bare 403: the
                // long-poll hello says which it is.
                return End::NeverConnected(format!("upgrade refused (HTTP {})", resp.status()));
            }
            Ok(Err(e)) => return End::NeverConnected(e.to_string()),
            Err(_) => return End::NeverConnected("timed out".into()),
        };
        let (mut sink, mut stream) = ws.split();

        let hello = match Frame::new(self.hello(), vec![]).encode() {
            Ok(bytes) => bytes,
            Err(e) => return End::Failed(e),
        };
        if let Err(e) = sink.send(Message::Binary(hello)).await {
            return End::Closed(format!("sending the hello: {e}"));
        }
        let ack = match tokio::time::timeout(Duration::from_secs(20), stream.next()).await {
            Err(_) => return End::Closed("no hello_ack".into()),
            Ok(None) => return End::Closed("closed before the hello_ack".into()),
            Ok(Some(Err(e))) => return End::Closed(format!("reading the hello_ack: {e}")),
            Ok(Some(Ok(Message::Close(frame)))) => return close_end(frame.map(|f| f.code)),
            Ok(Some(Ok(Message::Binary(data)))) => match decode_frames(&data) {
                Ok(frames) => match frames.into_iter().next() {
                    Some(frame) if frame.kind() == "hello_ack" => Value::Object(frame.header),
                    Some(frame) if frame.kind() == "update_required" => {
                        return End::Refused(Refusal::UpdateRequired)
                    }
                    Some(frame) if frame.kind() == "error" => {
                        return End::Refused(Refusal::Refused(
                            frame
                                .str_field("message")
                                .unwrap_or("hello refused")
                                .to_string(),
                        ))
                    }
                    _ => return End::Failed(anyhow!("the platform did not acknowledge the hello")),
                },
                Err(e) => return End::Failed(e.context("reading the hello_ack")),
            },
            Ok(Some(Ok(_))) => return End::Failed(anyhow!("unexpected answer to the hello")),
        };
        self.apply_ack(&ack);
        self.online("websocket");
        info!(
            "relay connected (WebSocket); allowed destinations: {}",
            describe(self.forwarder.policy().declared())
        );

        let (out_tx, out_rx) = mpsc::channel::<Frame>(256);
        let (ctrl_tx, ctrl_rx) = mpsc::channel::<Message>(8);
        let dispatcher = Dispatcher::new(self.forwarder.clone(), out_tx, self.max_concurrent());
        let heartbeat = self.heartbeat;
        let writer = tokio::spawn(websocket_writer(sink, out_rx, ctrl_rx, heartbeat));
        let idle_limit = heartbeat * 3 + Duration::from_secs(15);
        let mut last_seen = Instant::now();
        let mut tick = tokio::time::interval(Duration::from_secs(1));
        let mut rehello_pending = false;
        let mut rehello_alive = true;

        let end = loop {
            tokio::select! {
                message = stream.next() => {
                    last_seen = Instant::now();
                    match message {
                        None => break End::Closed("socket closed".into()),
                        Some(Err(e)) => break End::Closed(format!("socket error: {e}")),
                        Some(Ok(Message::Binary(data))) => match decode_frames(&data) {
                            Ok(frames) => {
                                let mut update = false;
                                for frame in frames {
                                    match frame.kind() {
                                        "update_required" => update = true,
                                        "hello_ack" => {}
                                        _ => dispatcher.handle(frame),
                                    }
                                }
                                if update {
                                    break End::Refused(Refusal::UpdateRequired);
                                }
                            }
                            Err(e) => break End::Failed(e.context("reading a frame")),
                        },
                        Some(Ok(Message::Ping(payload))) => {
                            let _ = ctrl_tx.send(Message::Pong(payload)).await;
                        }
                        Some(Ok(Message::Close(frame))) => break close_end(frame.map(|f| f.code)),
                        Some(Ok(_)) => {}
                    }
                }
                _ = tick.tick() => {
                    if writer.is_finished() {
                        break End::Closed("the socket stopped accepting frames".into());
                    }
                    if last_seen.elapsed() > idle_limit {
                        break End::Closed(format!("nothing heard for {}s", idle_limit.as_secs()));
                    }
                    if rehello_pending && dispatcher.in_flight() == 0 {
                        break End::Rehello;
                    }
                }
                changed = self.rehello.changed(), if rehello_alive => {
                    match changed {
                        Ok(()) => rehello_pending = true,
                        Err(_) => rehello_alive = false,
                    }
                }
                _ = shutdown.changed() => {
                    if *shutdown.borrow() {
                        let _ = ctrl_tx.send(Message::Close(None)).await;
                        break End::Shutdown;
                    }
                }
            }
        };
        dispatcher.abort_all();
        drop(dispatcher);
        drop(ctrl_tx);
        if matches!(end, End::Shutdown) {
            let _ = tokio::time::timeout(Duration::from_secs(2), writer).await;
        } else {
            writer.abort();
        }
        end
    }

    // -- Long-poll ----------------------------------------------------------

    async fn long_poll(
        &mut self,
        limit: Option<Duration>,
        shutdown: &mut watch::Receiver<bool>,
    ) -> End {
        let ack = match self.api.hello(&self.hello()).await {
            Ok(ack) => ack,
            Err(e) => {
                return match e.downcast_ref::<Refusal>() {
                    Some(refusal) => End::Refused(refusal.clone()),
                    None => End::Failed(e),
                }
            }
        };
        self.apply_ack(&ack);
        self.online("long_poll");
        info!(
            "relay connected (HTTP long-poll); allowed destinations: {}",
            describe(self.forwarder.policy().declared())
        );

        let (out_tx, out_rx) = mpsc::channel::<Frame>(256);
        let dispatcher = Dispatcher::new(self.forwarder.clone(), out_tx, self.max_concurrent());
        let uploader = AbortOnDrop(tokio::spawn(upload_loop(self.api.clone(), out_rx)));
        let update_required = Arc::new(AtomicBool::new(false));
        let _heartbeat = AbortOnDrop(tokio::spawn(heartbeat_loop(
            self.api.clone(),
            self.heartbeat,
            update_required.clone(),
        )));

        let started = Instant::now();
        let mut draining_since: Option<Instant> = None;
        let mut rehello_pending = false;
        let mut failures: u32 = 0;
        let end = loop {
            if *shutdown.borrow() {
                break End::Shutdown;
            }
            if update_required.load(Ordering::Relaxed) {
                break End::Refused(Refusal::UpdateRequired);
            }
            if self.rehello.has_changed().unwrap_or(false) {
                let _ = self.rehello.borrow_and_update();
                rehello_pending = true;
            }
            let stint_over = limit.is_some_and(|l| started.elapsed() >= l);
            if (stint_over || rehello_pending) && draining_since.is_none() {
                draining_since = Some(Instant::now());
            }
            if let Some(since) = draining_since {
                if dispatcher.in_flight() == 0 || since.elapsed() >= DRAIN_LIMIT {
                    break if rehello_pending {
                        End::Rehello
                    } else {
                        End::Switch
                    };
                }
            }
            let free = if draining_since.is_some() {
                0
            } else {
                dispatcher.free()
            };
            let polled = tokio::select! {
                polled = self.api.poll(free) => polled,
                _ = shutdown.changed() => continue,
            };
            match polled {
                Ok(frames) => {
                    failures = 0;
                    let mut update = false;
                    for frame in frames {
                        if frame.kind() == "update_required" {
                            update = true;
                        } else {
                            dispatcher.handle(frame);
                        }
                    }
                    if update {
                        break End::Refused(Refusal::UpdateRequired);
                    }
                }
                Err(e) => {
                    if let Some(refusal) = e.downcast_ref::<Refusal>() {
                        break End::Refused(refusal.clone());
                    }
                    let wait = retry_delay(
                        &e,
                        failures,
                        Duration::from_secs(1),
                        Duration::from_secs(30),
                    );
                    failures = failures.saturating_add(1);
                    if failures > 20 {
                        break End::Failed(e);
                    }
                    debug!("long poll failed ({e:#}); retrying in {wait:?}");
                    self.status.set(
                        State::Reconnecting,
                        Some("long_poll"),
                        Some(format!("{e:#}")),
                    );
                    tokio::select! {
                        _ = tokio::time::sleep(wait) => {}
                        _ = shutdown.changed() => {}
                    }
                }
            }
        };
        dispatcher.abort_all();
        drop(dispatcher);
        // Give the frames already produced a moment to reach the platform.
        if !matches!(end, End::Refused(_)) {
            let _ = tokio::time::timeout(Duration::from_secs(5), async {
                while !uploader.0.is_finished() {
                    tokio::time::sleep(Duration::from_millis(50)).await;
                }
            })
            .await;
        }
        end
    }
}

/// Aborts a task when dropped.
struct AbortOnDrop(tokio::task::JoinHandle<()>);

impl Drop for AbortOnDrop {
    fn drop(&mut self) {
        self.0.abort();
    }
}

fn describe(destinations: &[String]) -> String {
    if destinations.is_empty() {
        "none (every request is refused)".into()
    } else {
        destinations.join(", ")
    }
}

/// What a close code from the platform means.
fn close_end(code: Option<CloseCode>) -> End {
    match code {
        Some(CloseCode::Library(4401)) => End::Refused(Refusal::Revoked),
        Some(CloseCode::Library(4404)) => End::Refused(Refusal::Disabled),
        Some(CloseCode::Library(4426)) => End::Refused(Refusal::UpdateRequired),
        Some(CloseCode::Library(4400)) => End::Refused(Refusal::Refused(
            "the platform refused the hello (protocol mismatch)".into(),
        )),
        Some(code) => End::Closed(format!("closed by the platform ({code})")),
        None => End::Closed("closed by the platform".into()),
    }
}

/// Owns the socket's write half: answer frames (batched), control frames and
/// the heartbeat.
async fn websocket_writer<S>(
    mut sink: S,
    mut out_rx: mpsc::Receiver<Frame>,
    mut ctrl_rx: mpsc::Receiver<Message>,
    heartbeat: Duration,
) where
    S: futures_util::Sink<Message> + Unpin,
{
    let mut tick = tokio::time::interval(heartbeat);
    tick.tick().await;
    loop {
        tokio::select! {
            frame = out_rx.recv() => {
                let Some(frame) = frame else { break };
                let mut size = frame.payload.len();
                let mut batch = vec![frame];
                while size < BATCH_BYTES {
                    match out_rx.try_recv() {
                        Ok(next) => {
                            size += next.payload.len();
                            batch.push(next);
                        }
                        Err(_) => break,
                    }
                }
                let bytes = if batch.len() == 1 { batch[0].encode() } else { encode_frames(&batch) };
                let Ok(bytes) = bytes else { break };
                if sink.send(Message::Binary(bytes)).await.is_err() {
                    break;
                }
            }
            control = ctrl_rx.recv() => {
                let Some(control) = control else { continue };
                let close = matches!(control, Message::Close(_));
                if sink.send(control).await.is_err() || close {
                    break;
                }
            }
            _ = tick.tick() => {
                let Ok(beat) = Frame::new(json!({"type": "heartbeat"}), vec![]).encode() else { break };
                if sink.send(Message::Binary(beat)).await.is_err() {
                    break;
                }
                if sink.send(Message::Ping(Vec::new())).await.is_err() {
                    break;
                }
            }
        }
    }
    let _ = sink.close().await;
}

/// Uploads the answers produced on a long-poll connection, in batches.
async fn upload_loop(api: PlatformApi, mut rx: mpsc::Receiver<Frame>) {
    while let Some(first) = rx.recv().await {
        let mut size = first.payload.len();
        let mut batch = vec![first];
        let gather_until = tokio::time::Instant::now() + Duration::from_millis(20);
        while size < BATCH_BYTES {
            match tokio::time::timeout_at(gather_until, rx.recv()).await {
                Ok(Some(next)) => {
                    size += next.payload.len();
                    batch.push(next);
                }
                _ => break,
            }
        }
        let body = match encode_frames(&batch) {
            Ok(body) if body.len() <= MAX_UPLOAD_BODY_BYTES => body,
            Ok(_) | Err(_) => {
                warn!("dropping {} frames that do not fit one upload", batch.len());
                continue;
            }
        };
        let sent = retry_within(
            "frame upload",
            Duration::from_secs(30),
            Duration::from_secs(20),
            Duration::from_millis(500),
            Duration::from_secs(4),
            |_| api.upload(body.clone()),
        )
        .await;
        if let Err(e) = sent {
            warn!("could not upload {} answer frames: {e:#}", batch.len());
        }
    }
}

/// Keeps a long-polling relay marked alive while requests run.
async fn heartbeat_loop(api: PlatformApi, interval: Duration, update_required: Arc<AtomicBool>) {
    let mut tick = tokio::time::interval(interval);
    tick.tick().await;
    loop {
        tick.tick().await;
        match api.heartbeat().await {
            Ok(true) => update_required.store(true, Ordering::Relaxed),
            Ok(false) => {}
            Err(e) => debug!("long-poll heartbeat failed: {e:#}"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::policy::Policy;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;
    use tokio_tungstenite::tungstenite::handshake::server::{Request, Response};
    use tokio_tungstenite::tungstenite::protocol::CloseFrame;

    async fn destination() -> u16 {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut buf = [0u8; 2048];
            let _ = socket.read(&mut buf).await.unwrap();
            socket
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\nConnection: close\r\n\r\npong")
                .await
                .unwrap();
        });
        port
    }

    fn binary(frame: Frame) -> Message {
        Message::Binary(frame.encode().unwrap())
    }

    // The handshake callback's error type is tungstenite's.
    #[allow(clippy::result_large_err)]
    #[tokio::test]
    async fn a_request_travels_the_websocket_and_a_revocation_ends_it() {
        crate::tls::install_default_provider();
        let dest_port = destination().await;
        let platform = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let platform_port = platform.local_addr().unwrap().port();
        let server = tokio::spawn(async move {
            let (socket, _) = platform.accept().await.unwrap();
            let mut auth = None;
            let ws =
                tokio_tungstenite::accept_hdr_async(socket, |req: &Request, resp: Response| {
                    auth = req
                        .headers()
                        .get("authorization")
                        .and_then(|v| v.to_str().ok())
                        .map(str::to_string);
                    Ok(resp)
                })
                .await
                .unwrap();
            let (mut tx, mut rx) = ws.split();
            let Some(Ok(Message::Binary(hello))) = rx.next().await else {
                panic!("no hello");
            };
            let hello = decode_frames(&hello).unwrap().remove(0);
            assert_eq!(hello.kind(), "hello");
            assert_eq!(hello.header["capabilities"], json!(["http"]));
            assert_eq!(hello.header["destinations"], json!(["127.0.0.1"]));
            tx.send(binary(Frame::new(
                json!({"type": "hello_ack", "protocol": 1, "heartbeat_interval": 30, "chunk": 65536}),
                vec![],
            )))
            .await
            .unwrap();
            tx.send(binary(Frame::new(
                json!({
                    "type": "open",
                    "sid": "abcdef0123456789abcdef0123456789",
                    "kind": "http",
                    "method": "GET",
                    "url": format!("http://127.0.0.1:{dest_port}/ping"),
                    "headers": [],
                    "timeout_ms": 5000,
                    "window": 1048576,
                    "body_len": 0,
                }),
                vec![],
            )))
            .await
            .unwrap();
            let mut frames = Vec::new();
            while let Some(Ok(message)) = rx.next().await {
                if let Message::Binary(data) = message {
                    for frame in decode_frames(&data).unwrap() {
                        let done = frame.kind() == "end" || frame.kind() == "error";
                        if frame.kind() != "heartbeat" {
                            frames.push(frame);
                        }
                        if done {
                            break;
                        }
                    }
                    if frames
                        .last()
                        .is_some_and(|f| f.kind() == "end" || f.kind() == "error")
                    {
                        break;
                    }
                }
            }
            tx.send(Message::Close(Some(CloseFrame {
                code: CloseCode::Library(4401),
                reason: "".into(),
            })))
            .await
            .unwrap();
            (auth, frames)
        });

        let dir = std::env::temp_dir().join(format!("xtm-relay-tunnel-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let mut config = Config::load(&dir.join("relay.toml")).unwrap();
        config.allowed_destinations = vec!["127.0.0.1".into()];
        let tls = TlsConfigs::build(None).unwrap();
        let forwarder = Arc::new(Forwarder::new(
            config.policy().unwrap(),
            Policy::default(),
            tls.clone(),
        ));
        let status = Arc::new(StatusReporter::new(dir.join("status.json"), None, None));
        let api = PlatformApi::new(
            reqwest::Client::new(),
            &format!("http://127.0.0.1:{platform_port}"),
            "rly-test".into(),
            Duration::from_secs(25),
        );
        let updater = Arc::new(Updater::new(api.clone(), false));
        let (_rehello_tx, rehello_rx) = watch::channel(0u64);
        let mut tunnel = Tunnel::new(config, api, tls, forwarder, status, updater, rehello_rx);
        let (_shutdown_tx, mut shutdown_rx) = watch::channel(false);
        let end = tokio::time::timeout(Duration::from_secs(20), tunnel.websocket(&mut shutdown_rx))
            .await
            .expect("the tunnel ends");
        assert!(matches!(end, End::Refused(Refusal::Revoked)));

        let (auth, frames) = server.await.unwrap();
        assert_eq!(auth.as_deref(), Some("Bearer rly-test"));
        assert_eq!(frames[0].kind(), "response_head", "{frames:?}");
        assert_eq!(frames[0].u64_field("status"), Some(200));
        let body: Vec<u8> = frames
            .iter()
            .filter(|f| f.kind() == "data")
            .flat_map(|f| f.payload.clone())
            .collect();
        assert_eq!(body, b"pong");
        assert_eq!(frames.last().unwrap().kind(), "end");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
