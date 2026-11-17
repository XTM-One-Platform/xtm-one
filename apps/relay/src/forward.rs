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

//! One relayed HTTP request: from the platform's `open` frame to the `end`
//! (or `error`) frame of its answer.
//!
//! * The destination is resolved HERE and the local policy is applied to the
//!   addresses the connection will use; the client is pinned to exactly those
//!   addresses, so the name cannot be re-resolved somewhere else in between.
//! * Redirects are never followed (a 3xx goes back to the caller, whose next
//!   request is checked again) and an answer is never decompressed: its bytes
//!   and its `Content-Encoding` travel as they came.
//! * The request body arrives inline in the `open` frame and, past 1 MiB, in
//!   `data` frames; the answer goes back in `data` frames of at most the
//!   platform's chunk size, never past the credit window the platform grants
//!   (`window` frames), so a slow caller slows the destination down instead of
//!   filling memory.

use std::collections::HashMap;
use std::net::{IpAddr, SocketAddr};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant};

use anyhow::Result;
use reqwest::header::{HeaderMap, HeaderName, HeaderValue};
use serde_json::{json, Value};
use tokio::sync::{mpsc, Notify};
use tracing::debug;

use crate::frame::{
    clip_message, Frame, MAX_FRAME_PAYLOAD_BYTES, MAX_HEADER_VALUE_CHARS, MAX_RESPONSE_HEADERS,
};
use crate::policy::{normalize_host, Policy};
use crate::tls::TlsConfigs;

/// Headers that describe one connection, never forwarded either way.
const HOP_BY_HOP: [&str; 9] = [
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
];

/// What the platform grants before its first `window` frame.
pub const INITIAL_WINDOW_BYTES: u64 = 1024 * 1024;
const DEFAULT_TIMEOUT: Duration = Duration::from_secs(60);
const MAX_TIMEOUT: Duration = Duration::from_secs(30 * 60);
const DEFAULT_CONNECT_TIMEOUT: Duration = Duration::from_secs(20);
/// A pinned client is reused for this long after its last request.
const CLIENT_IDLE: Duration = Duration::from_secs(300);
const MAX_CACHED_CLIENTS: usize = 128;

/// The platform's limits, from its `hello_ack`.
#[derive(Debug, Clone, Copy)]
pub struct Limits {
    pub max_response_bytes: u64,
    pub chunk: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            max_response_bytes: 100 * 1024 * 1024,
            chunk: 64 * 1024,
        }
    }
}

impl Limits {
    /// Read the limits a `hello_ack` announces, keeping the defaults for what
    /// it does not say.
    pub fn from_ack(ack: &Value) -> Self {
        let defaults = Self::default();
        let max_response_bytes = ack
            .get("max_response_bytes")
            .and_then(Value::as_u64)
            .filter(|v| *v > 0)
            .unwrap_or(defaults.max_response_bytes);
        let chunk = ack
            .get("chunk")
            .and_then(Value::as_u64)
            .map(|v| (v as usize).clamp(1024, MAX_FRAME_PAYLOAD_BYTES))
            .unwrap_or(defaults.chunk);
        Self {
            max_response_bytes,
            chunk,
        }
    }
}

/// Why a request failed, in the platform's vocabulary (`RelayError` codes).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Failure {
    pub code: &'static str,
    pub message: String,
}

impl Failure {
    fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }
}

#[derive(Hash, PartialEq, Eq, Clone)]
struct ClientKey {
    host: String,
    addrs: Vec<SocketAddr>,
    unverified: bool,
    connect_ms: u64,
}

/// The credit the platform granted for one answer.
struct Credit {
    left: Mutex<u64>,
    granted: Notify,
}

impl Credit {
    fn new(initial: u64) -> Self {
        Self {
            left: Mutex::new(initial),
            granted: Notify::new(),
        }
    }

    fn grant(&self, increment: u64) {
        *self.left.lock().expect("credit") += increment;
        self.granted.notify_one();
    }

    /// Take up to `want` bytes of credit, waiting until some is granted.
    async fn take(&self, want: usize) -> usize {
        loop {
            let notified = self.granted.notified();
            {
                let mut left = self.left.lock().expect("credit");
                if *left > 0 {
                    let n = (*left).min(want as u64);
                    *left -= n;
                    return n as usize;
                }
            }
            notified.await;
        }
    }
}

/// Aborts a task when dropped.
struct AbortOnDrop(tokio::task::JoinHandle<()>);

impl Drop for AbortOnDrop {
    fn drop(&mut self) {
        self.0.abort();
    }
}

/// Sends relayed requests to their destinations.
pub struct Forwarder {
    policy: RwLock<Arc<Policy>>,
    unverified_tls: RwLock<Arc<Policy>>,
    tls: TlsConfigs,
    limits: RwLock<Limits>,
    clients: Mutex<HashMap<ClientKey, (reqwest::Client, Instant)>>,
    in_flight: AtomicUsize,
}

impl Forwarder {
    pub fn new(policy: Policy, unverified_tls: Policy, tls: TlsConfigs) -> Self {
        Self {
            policy: RwLock::new(Arc::new(policy)),
            unverified_tls: RwLock::new(Arc::new(unverified_tls)),
            tls,
            limits: RwLock::new(Limits::default()),
            clients: Mutex::new(HashMap::new()),
            in_flight: AtomicUsize::new(0),
        }
    }

    /// Apply a reloaded allow-list to every request from now on.
    pub fn set_policy(&self, policy: Policy, unverified_tls: Policy) {
        *self.policy.write().expect("policy") = Arc::new(policy);
        *self.unverified_tls.write().expect("policy") = Arc::new(unverified_tls);
        self.clients.lock().expect("clients").clear();
    }

    pub fn policy(&self) -> Arc<Policy> {
        self.policy.read().expect("policy").clone()
    }

    pub fn set_limits(&self, limits: Limits) {
        *self.limits.write().expect("limits") = limits;
    }

    fn limits(&self) -> Limits {
        *self.limits.read().expect("limits")
    }

    /// Requests being served right now.
    pub fn in_flight(&self) -> usize {
        self.in_flight.load(Ordering::Relaxed)
    }

    /// A client pinned to `addrs` for `host`, reused while it is warm.
    fn client(&self, key: ClientKey) -> Result<reqwest::Client, Failure> {
        let now = Instant::now();
        let mut clients = self.clients.lock().expect("clients");
        clients.retain(|_, (_, used)| now.duration_since(*used) < CLIENT_IDLE);
        if let Some((client, used)) = clients.get_mut(&key) {
            *used = now;
            return Ok(client.clone());
        }
        if clients.len() >= MAX_CACHED_CLIENTS {
            if let Some(oldest) = clients
                .iter()
                .min_by_key(|(_, (_, used))| *used)
                .map(|(k, _)| k.clone())
            {
                clients.remove(&oldest);
            }
        }
        let tls = if key.unverified {
            (*self.tls.unverified).clone()
        } else {
            (*self.tls.verified).clone()
        };
        let client = reqwest::Client::builder()
            .use_preconfigured_tls(tls)
            .resolve_to_addrs(&key.host, &key.addrs)
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(Duration::from_millis(key.connect_ms))
            .pool_idle_timeout(Duration::from_secs(90))
            .build()
            .map_err(|e| {
                Failure::new("protocol_error", format!("building the HTTP client: {e}"))
            })?;
        clients.insert(key, (client.clone(), now));
        Ok(client)
    }

    /// Serve one `open` frame; every outcome ends with an `end` or `error`
    /// frame on `out`.
    pub async fn serve(
        self: Arc<Self>,
        open: Frame,
        down: mpsc::UnboundedReceiver<Frame>,
        out: mpsc::Sender<Frame>,
    ) {
        let sid = open.sid().unwrap_or_default().to_string();
        self.in_flight.fetch_add(1, Ordering::Relaxed);
        let result = self.exchange(&sid, open, down, &out).await;
        self.in_flight.fetch_sub(1, Ordering::Relaxed);
        if let Err(failure) = result {
            debug!(sid = %sid, code = failure.code, "relayed request failed: {}", failure.message);
            let _ = out
                .send(Frame::new(
                    json!({
                        "type": "error",
                        "sid": sid,
                        "code": failure.code,
                        "message": clip_message(&failure.message),
                    }),
                    vec![],
                ))
                .await;
        }
    }

    async fn exchange(
        &self,
        sid: &str,
        open: Frame,
        mut down: mpsc::UnboundedReceiver<Frame>,
        out: &mpsc::Sender<Frame>,
    ) -> Result<(), Failure> {
        let kind = open.str_field("kind").unwrap_or("http");
        if kind != "http" {
            return Err(Failure::new(
                "protocol_error",
                format!("this relay does not serve '{kind}' requests"),
            ));
        }
        let timeout = open
            .u64_field("timeout_ms")
            .map(Duration::from_millis)
            .unwrap_or(DEFAULT_TIMEOUT)
            .clamp(Duration::from_secs(1), MAX_TIMEOUT);
        let connect_timeout = open
            .u64_field("connect_timeout_ms")
            .map(Duration::from_millis)
            .unwrap_or(DEFAULT_CONNECT_TIMEOUT)
            .clamp(Duration::from_secs(1), timeout);
        let method = open
            .str_field("method")
            .and_then(|m| reqwest::Method::from_bytes(m.as_bytes()).ok())
            .ok_or_else(|| Failure::new("protocol_error", "the request has no valid method"))?;
        let url = open
            .str_field("url")
            .and_then(|u| url::Url::parse(u).ok())
            .ok_or_else(|| Failure::new("protocol_error", "the request has no valid URL"))?;
        if !matches!(url.scheme(), "http" | "https") {
            return Err(Failure::new(
                "destination_denied",
                "only http and https URLs are relayed",
            ));
        }
        let host = url
            .host_str()
            .ok_or_else(|| Failure::new("destination_denied", "the URL names no host"))?
            .to_string();
        let port = url.port_or_known_default().unwrap_or(443);

        // Resolve here and admit the addresses the connection will use.
        let policy = self.policy();
        let bare = normalize_host(&host);
        let literal = bare.parse::<IpAddr>().ok();
        if literal.is_none() && !policy.may_resolve(&bare, port) {
            return Err(Failure::new(
                "destination_denied",
                format!("{bare}:{port} is not in this relay's allowed destinations"),
            ));
        }
        let ips: Vec<IpAddr> = match literal {
            Some(ip) => vec![ip],
            None => match tokio::time::timeout(
                connect_timeout,
                tokio::net::lookup_host((bare.as_str(), port)),
            )
            .await
            {
                Ok(Ok(addrs)) => addrs.map(|a| a.ip()).collect(),
                Ok(Err(e)) => {
                    return Err(Failure::new("dns_failed", format!("resolving {bare}: {e}")))
                }
                Err(_) => {
                    return Err(Failure::new(
                        "dns_failed",
                        format!("resolving {bare} timed out"),
                    ))
                }
            },
        };
        let addrs = policy
            .admit(&bare, port, &ips)
            .map_err(|denied| Failure::new("destination_denied", denied.0))?;
        let unverified = url.scheme() == "https"
            && self
                .unverified_tls
                .read()
                .expect("policy")
                .admit(&bare, port, &ips)
                .is_ok();
        let client = self.client(ClientKey {
            host: host.clone(),
            addrs,
            unverified,
            connect_ms: connect_timeout.as_millis() as u64,
        })?;

        let headers = request_headers(&open)?;
        let body_len = open
            .u64_field("body_len")
            .unwrap_or(open.payload.len() as u64);
        let inline = open.payload;
        if inline.len() as u64 > body_len {
            return Err(Failure::new(
                "protocol_error",
                "the inline body is longer than body_len",
            ));
        }

        // The down pump: request body frames, credit grants.
        let credit = Arc::new(Credit::new(
            open.header
                .get("window")
                .and_then(Value::as_u64)
                .unwrap_or(INITIAL_WINDOW_BYTES),
        ));
        let mut request = client.request(method, url).headers(headers);
        let (body_tx, body_rx) = mpsc::channel::<Result<Vec<u8>, std::io::Error>>(8);
        let streamed = (inline.len() as u64) < body_len;
        if streamed {
            request = request
                .header(reqwest::header::CONTENT_LENGTH, body_len)
                .body(reqwest::Body::wrap_stream(receiver_stream(body_rx)));
        } else if body_len > 0 {
            request = request.body(inline.clone());
        }
        let pump_credit = credit.clone();
        let pump = AbortOnDrop(tokio::spawn(async move {
            let mut body_tx = if streamed { Some(body_tx) } else { None };
            let mut remaining = body_len;
            if let Some(tx) = &body_tx {
                remaining -= inline.len() as u64;
                if !inline.is_empty() && tx.send(Ok(inline)).await.is_err() {
                    body_tx = None;
                }
            }
            while let Some(frame) = down.recv().await {
                match frame.kind() {
                    "window" => {
                        if let Some(increment) = frame.u64_field("increment") {
                            pump_credit.grant(increment);
                        }
                    }
                    "data" => {
                        let Some(tx) = &body_tx else { continue };
                        let len = frame.payload.len() as u64;
                        if len > remaining {
                            let _ = tx
                                .send(Err(std::io::Error::other("request body past body_len")))
                                .await;
                            body_tx = None;
                            continue;
                        }
                        remaining -= len;
                        if tx.send(Ok(frame.payload)).await.is_err() {
                            body_tx = None;
                            continue;
                        }
                        if remaining == 0 {
                            body_tx = None;
                        }
                    }
                    _ => {}
                }
            }
        }));

        let response = match tokio::time::timeout(timeout, request.send()).await {
            Ok(Ok(response)) => response,
            Ok(Err(e)) => return Err(classify(&e)),
            Err(_) => {
                return Err(Failure::new(
                    "timeout",
                    format!("no answer from {bare}:{port} within {}s", timeout.as_secs()),
                ))
            }
        };

        let limits = self.limits();
        if let Some(length) = response.content_length() {
            if length > limits.max_response_bytes {
                return Err(Failure::new(
                    "too_large",
                    format!(
                        "the answer is {length} bytes, past the {} bytes allowed",
                        limits.max_response_bytes
                    ),
                ));
            }
        }
        let head = Frame::new(
            json!({
                "type": "response_head",
                "sid": sid,
                "status": response.status().as_u16(),
                "headers": response_headers(response.headers()),
            }),
            vec![],
        );
        if out.send(head).await.is_err() {
            return Ok(());
        }

        let mut response = response;
        let mut total: u64 = 0;
        loop {
            let chunk = match tokio::time::timeout(timeout, response.chunk()).await {
                Ok(Ok(Some(chunk))) => chunk,
                Ok(Ok(None)) => break,
                Ok(Err(e)) => {
                    let failure = classify(&e);
                    return Err(Failure::new(
                        failure.code,
                        format!("the answer broke off: {}", failure.message),
                    ));
                }
                Err(_) => {
                    return Err(Failure::new(
                        "timeout",
                        format!("the answer stalled for {}s", timeout.as_secs()),
                    ))
                }
            };
            total += chunk.len() as u64;
            if total > limits.max_response_bytes {
                return Err(Failure::new(
                    "too_large",
                    format!(
                        "the answer passed the {} bytes allowed",
                        limits.max_response_bytes
                    ),
                ));
            }
            let mut rest: &[u8] = &chunk;
            while !rest.is_empty() {
                let want = rest.len().min(limits.chunk);
                let n = match tokio::time::timeout(timeout, credit.take(want)).await {
                    Ok(n) => n,
                    Err(_) => {
                        return Err(Failure::new(
                            "timeout",
                            "the platform stopped reading the answer",
                        ))
                    }
                };
                let frame = Frame::new(json!({"type": "data", "sid": sid}), rest[..n].to_vec());
                if out.send(frame).await.is_err() {
                    return Ok(());
                }
                rest = &rest[n..];
            }
        }
        drop(pump);
        let _ = out
            .send(Frame::new(json!({"type": "end", "sid": sid}), vec![]))
            .await;
        Ok(())
    }
}

/// A stream over a channel of body chunks, for `reqwest::Body::wrap_stream`.
fn receiver_stream(
    rx: mpsc::Receiver<Result<Vec<u8>, std::io::Error>>,
) -> impl futures_util::Stream<Item = Result<Vec<u8>, std::io::Error>> {
    futures_util::stream::unfold(rx, |mut rx| async move {
        rx.recv().await.map(|item| (item, rx))
    })
}

/// The request headers of an `open` frame, minus what describes the platform's
/// own connection (the client sets `Host` and `Content-Length` itself).
fn request_headers(open: &Frame) -> Result<HeaderMap, Failure> {
    let mut headers = HeaderMap::new();
    let Some(list) = open.header.get("headers") else {
        return Ok(headers);
    };
    let Value::Array(list) = list else {
        return Err(Failure::new("protocol_error", "headers must be a list"));
    };
    for pair in list {
        let (Some(name), Some(value)) = (
            pair.get(0).and_then(Value::as_str),
            pair.get(1).and_then(Value::as_str),
        ) else {
            return Err(Failure::new(
                "protocol_error",
                "a header is not a [name, value] pair",
            ));
        };
        let lower = name.to_ascii_lowercase();
        if HOP_BY_HOP.contains(&lower.as_str()) || lower == "host" || lower == "content-length" {
            continue;
        }
        let name = HeaderName::from_bytes(name.as_bytes())
            .map_err(|_| Failure::new("protocol_error", format!("invalid header name '{name}'")))?;
        let value = HeaderValue::from_str(value).map_err(|_| {
            Failure::new(
                "protocol_error",
                format!("invalid value for header '{name}'"),
            )
        })?;
        headers.append(name, value);
    }
    Ok(headers)
}

/// The answer's headers as the platform takes them: `[name, value]` pairs,
/// hop-by-hop ones dropped, bounded in count and length.
fn response_headers(headers: &HeaderMap) -> Vec<[String; 2]> {
    headers
        .iter()
        .filter(|(name, _)| !HOP_BY_HOP.contains(&name.as_str()))
        .take(MAX_RESPONSE_HEADERS)
        .map(|(name, value)| {
            let text = String::from_utf8_lossy(value.as_bytes());
            [
                name.as_str().to_string(),
                text.chars().take(MAX_HEADER_VALUE_CHARS).collect(),
            ]
        })
        .collect()
}

/// A transport failure in the platform's vocabulary.
fn classify(err: &reqwest::Error) -> Failure {
    let detail = root_cause(err);
    if err.is_timeout() {
        return Failure::new("timeout", detail);
    }
    if is_tls_failure(err) {
        return Failure::new("tls_failed", detail);
    }
    if err.is_connect() {
        return Failure::new("connect_failed", detail);
    }
    if err.is_request() || err.is_body() {
        return Failure::new("connect_failed", detail);
    }
    Failure::new("protocol_error", detail)
}

fn root_cause(err: &(dyn std::error::Error + 'static)) -> String {
    let mut message = err.to_string();
    let mut source = err.source();
    while let Some(cause) = source {
        let text = cause.to_string();
        if !text.is_empty() && !message.contains(&text) {
            message = format!("{message}: {text}");
        }
        source = cause.source();
    }
    message
}

fn is_tls_failure(err: &(dyn std::error::Error + 'static)) -> bool {
    let mut source: Option<&(dyn std::error::Error + 'static)> = Some(err);
    while let Some(cause) = source {
        if cause.downcast_ref::<rustls::Error>().is_some() {
            return true;
        }
        if let Some(io) = cause.downcast_ref::<std::io::Error>() {
            if io
                .get_ref()
                .is_some_and(|inner| inner.downcast_ref::<rustls::Error>().is_some())
            {
                return true;
            }
        }
        source = cause.source();
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    fn forwarder(patterns: &[&str]) -> Arc<Forwarder> {
        crate::tls::install_default_provider();
        Arc::new(Forwarder::new(
            Policy::parse(patterns).unwrap(),
            Policy::default(),
            TlsConfigs::build(None).unwrap(),
        ))
    }

    fn open(url: &str, body: &[u8], body_len: usize) -> Frame {
        Frame::new(
            json!({
                "type": "open",
                "sid": "0123456789abcdef0123456789abcdef",
                "kind": "http",
                "method": "POST",
                "url": url,
                "headers": [["X-Test", "yes"], ["Connection", "close"], ["Host", "spoofed"]],
                "timeout_ms": 5000,
                "window": 1024 * 1024,
                "body_len": body_len,
            }),
            body.to_vec(),
        )
    }

    /// A one-shot HTTP server answering `body` and handing back the request.
    async fn server(body: &'static [u8]) -> (u16, tokio::task::JoinHandle<Vec<u8>>) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let handle = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut request = Vec::new();
            let mut buf = [0u8; 4096];
            loop {
                let n = socket.read(&mut buf).await.unwrap();
                request.extend_from_slice(&buf[..n]);
                let text = String::from_utf8_lossy(&request).to_string();
                if let Some(end) = text.find("\r\n\r\n") {
                    let length = text
                        .lines()
                        .find_map(|l| {
                            l.to_ascii_lowercase()
                                .strip_prefix("content-length: ")
                                .map(|v| v.trim().parse::<usize>().unwrap())
                        })
                        .unwrap_or(0);
                    if request.len() >= end + 4 + length {
                        break;
                    }
                }
                if n == 0 {
                    break;
                }
            }
            let answer = format!(
                "HTTP/1.1 201 Created\r\nContent-Length: {}\r\nContent-Encoding: gzip\r\nX-Answer: ok\r\nConnection: close\r\n\r\n",
                body.len()
            );
            socket.write_all(answer.as_bytes()).await.unwrap();
            socket.write_all(body).await.unwrap();
            request
        });
        (port, handle)
    }

    async fn run(fwd: Arc<Forwarder>, open: Frame, downs: Vec<Frame>) -> Vec<Frame> {
        let (down_tx, down_rx) = mpsc::unbounded_channel();
        for frame in downs {
            down_tx.send(frame).unwrap();
        }
        let (out_tx, mut out_rx) = mpsc::channel(64);
        fwd.serve(open, down_rx, out_tx).await;
        let mut frames = Vec::new();
        while let Ok(frame) = out_rx.try_recv() {
            frames.push(frame);
        }
        drop(down_tx);
        frames
    }

    #[tokio::test]
    async fn a_request_is_relayed_and_its_answer_streamed_back_untouched() {
        let (port, server) = server(b"\x1f\x8bcompressed-bytes").await;
        let fwd = forwarder(&["127.0.0.1"]);
        let frames = run(
            fwd,
            open(&format!("http://127.0.0.1:{port}/api"), b"hello", 5),
            vec![],
        )
        .await;
        assert_eq!(frames[0].kind(), "response_head", "{frames:?}");
        assert_eq!(frames[0].u64_field("status"), Some(201));
        let headers = frames[0].header["headers"].as_array().unwrap();
        assert!(headers
            .iter()
            .any(|h| h[0] == "content-encoding" && h[1] == "gzip"));
        assert!(!headers.iter().any(|h| h[0] == "connection"));
        let body: Vec<u8> = frames
            .iter()
            .filter(|f| f.kind() == "data")
            .flat_map(|f| f.payload.clone())
            .collect();
        assert_eq!(body, b"\x1f\x8bcompressed-bytes");
        assert_eq!(frames.last().unwrap().kind(), "end");
        let request = String::from_utf8_lossy(&server.await.unwrap()).to_string();
        assert!(request.starts_with("POST /api HTTP/1.1"));
        assert!(request.to_ascii_lowercase().contains("x-test: yes"));
        assert!(!request.contains("spoofed"));
        assert!(request.ends_with("hello"));
    }

    #[tokio::test]
    async fn a_body_past_the_inline_part_arrives_in_data_frames() {
        let (port, server) = server(b"ok").await;
        let fwd = forwarder(&["127.0.0.1"]);
        let downs = vec![
            Frame::new(json!({"type": "data", "sid": "x"}), b" world".to_vec()),
            Frame::new(json!({"type": "data", "sid": "x"}), b"!".to_vec()),
        ];
        let frames = run(
            fwd,
            open(&format!("http://127.0.0.1:{port}/"), b"hello", 12),
            downs,
        )
        .await;
        assert_eq!(frames.last().unwrap().kind(), "end", "{frames:?}");
        let request = String::from_utf8_lossy(&server.await.unwrap()).to_string();
        assert!(request.ends_with("hello world!"), "{request}");
    }

    #[tokio::test]
    async fn a_destination_outside_the_local_list_is_refused_before_any_connection() {
        let fwd = forwarder(&["splunk.corp.local"]);
        let frames = run(fwd, open("http://127.0.0.1:9/", b"", 0), vec![]).await;
        assert_eq!(frames.len(), 1);
        assert_eq!(frames[0].kind(), "error");
        assert_eq!(frames[0].str_field("code"), Some("destination_denied"));
    }

    #[tokio::test]
    async fn an_unreachable_destination_is_a_connect_failure() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        let fwd = forwarder(&["127.0.0.1"]);
        let frames = run(
            fwd,
            open(&format!("http://127.0.0.1:{port}/"), b"", 0),
            vec![],
        )
        .await;
        assert_eq!(
            frames[0].str_field("code"),
            Some("connect_failed"),
            "{frames:?}"
        );
    }

    #[tokio::test]
    async fn the_answer_waits_for_credit() {
        let credit = Arc::new(Credit::new(3));
        assert_eq!(credit.take(10).await, 3);
        let waiter = {
            let credit = credit.clone();
            tokio::spawn(async move { credit.take(10).await })
        };
        tokio::task::yield_now().await;
        assert!(!waiter.is_finished());
        credit.grant(4);
        assert_eq!(waiter.await.unwrap(), 4);
    }

    #[test]
    fn limits_follow_the_hello_ack_within_bounds() {
        let limits = Limits::from_ack(&json!({"max_response_bytes": 1000, "chunk": 10_000_000}));
        assert_eq!(limits.max_response_bytes, 1000);
        assert_eq!(limits.chunk, MAX_FRAME_PAYLOAD_BYTES);
        let defaults = Limits::from_ack(&json!({}));
        assert_eq!(defaults.chunk, 64 * 1024);
    }

    #[test]
    fn hop_by_hop_and_platform_headers_are_not_forwarded() {
        let headers = request_headers(&open("http://x/", b"", 0)).unwrap();
        assert!(headers.get("x-test").is_some());
        assert!(headers.get("connection").is_none());
        assert!(headers.get("host").is_none());
    }
}
