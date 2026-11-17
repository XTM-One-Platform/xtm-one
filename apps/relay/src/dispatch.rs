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

//! The requests of one connection to the platform.
//!
//! The platform hands this relay `open` frames (a new request) and, for the
//! requests it holds, `data` (more request body), `window` (credit for the
//! answer) and `cancel` frames. Each request is a task of its own; the
//! dispatcher routes every frame to its task and forgets a request once it
//! answered or was cancelled. A dispatcher lives as long as its connection: the
//! platform fails the requests of a dropped connection itself, so they are
//! aborted with it.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use serde_json::json;
use tokio::sync::mpsc;
use tracing::debug;

use crate::forward::Forwarder;
use crate::frame::Frame;

struct Slot {
    down: mpsc::UnboundedSender<Frame>,
    task: tokio::task::AbortHandle,
}

/// The requests in flight on one connection.
pub struct Dispatcher {
    forwarder: Arc<Forwarder>,
    out: mpsc::Sender<Frame>,
    max: usize,
    streams: Arc<Mutex<HashMap<String, Slot>>>,
}

/// A request id as the platform mints them (`uuid4().hex`).
fn valid_sid(sid: &str) -> bool {
    !sid.is_empty() && sid.len() <= 64 && sid.bytes().all(|b| b.is_ascii_hexdigit())
}

impl Dispatcher {
    pub fn new(forwarder: Arc<Forwarder>, out: mpsc::Sender<Frame>, max: usize) -> Self {
        Self {
            forwarder,
            out,
            max: max.max(1),
            streams: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    /// Requests being served.
    pub fn in_flight(&self) -> usize {
        self.streams.lock().expect("streams").len()
    }

    /// How many more requests this relay can take.
    pub fn free(&self) -> usize {
        self.max.saturating_sub(self.in_flight())
    }

    /// Route one frame from the platform.
    pub fn handle(&self, frame: Frame) {
        match frame.kind() {
            "open" => self.open(frame),
            "data" | "window" | "end" => {
                let Some(sid) = frame.sid() else { return };
                let streams = self.streams.lock().expect("streams");
                if let Some(slot) = streams.get(sid) {
                    let _ = slot.down.send(frame);
                }
            }
            "cancel" => {
                let Some(sid) = frame.sid() else { return };
                if let Some(slot) = self.streams.lock().expect("streams").remove(sid) {
                    debug!(sid = %sid, "request cancelled by the platform");
                    slot.task.abort();
                }
            }
            other => debug!("ignoring a '{other}' frame"),
        }
    }

    fn open(&self, frame: Frame) {
        let Some(sid) = frame.sid().map(str::to_string) else {
            return;
        };
        if !valid_sid(&sid) {
            debug!("ignoring an open frame with an invalid request id");
            return;
        }
        let mut streams = self.streams.lock().expect("streams");
        if streams.contains_key(&sid) {
            return;
        }
        if streams.len() >= self.max {
            // The platform only hands over what the relay said it has room
            // for; answer rather than drop if it ever does not.
            let out = self.out.clone();
            tokio::spawn(async move {
                let _ = out
                    .send(Frame::new(
                        json!({
                            "type": "error",
                            "sid": sid,
                            "code": "protocol_error",
                            "message": "the relay is at its max_concurrent_requests",
                        }),
                        vec![],
                    ))
                    .await;
            });
            return;
        }
        let (down_tx, down_rx) = mpsc::unbounded_channel();
        let forwarder = self.forwarder.clone();
        let out = self.out.clone();
        let registry = self.streams.clone();
        let key = sid.clone();
        // Spawned and registered under the lock, so the task cannot finish
        // (and unregister) before it is registered.
        let task = tokio::spawn(async move {
            forwarder.serve(frame, down_rx, out).await;
            registry.lock().expect("streams").remove(&key);
        });
        streams.insert(
            sid,
            Slot {
                down: down_tx,
                task: task.abort_handle(),
            },
        );
    }

    /// Abort every request (the connection is gone).
    pub fn abort_all(&self) {
        for (_, slot) in self.streams.lock().expect("streams").drain() {
            slot.task.abort();
        }
    }
}

impl Drop for Dispatcher {
    fn drop(&mut self) {
        self.abort_all();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::policy::Policy;
    use crate::tls::TlsConfigs;

    fn dispatcher(max: usize) -> (Dispatcher, mpsc::Receiver<Frame>) {
        crate::tls::install_default_provider();
        let forwarder = Arc::new(Forwarder::new(
            Policy::default(),
            Policy::default(),
            TlsConfigs::build(None).unwrap(),
        ));
        let (tx, rx) = mpsc::channel(16);
        (Dispatcher::new(forwarder, tx, max), rx)
    }

    fn open(sid: &str) -> Frame {
        Frame::new(
            json!({"type": "open", "sid": sid, "kind": "http", "method": "GET", "url": "http://10.0.0.1/"}),
            vec![],
        )
    }

    #[tokio::test]
    async fn every_open_is_answered_and_forgotten() {
        let (d, mut rx) = dispatcher(4);
        d.handle(open("aa"));
        let answer = rx.recv().await.unwrap();
        // An empty allow-list refuses: the request still gets its error frame.
        assert_eq!(answer.kind(), "error");
        assert_eq!(answer.str_field("code"), Some("destination_denied"));
        tokio::task::yield_now().await;
        assert_eq!(d.in_flight(), 0);
        assert_eq!(d.free(), 4);
    }

    #[tokio::test]
    async fn invalid_and_duplicate_opens_are_ignored() {
        let (d, _rx) = dispatcher(4);
        d.handle(open("not-hex!"));
        assert_eq!(d.in_flight(), 0);
    }

    #[tokio::test]
    async fn an_open_past_capacity_is_refused() {
        let (d, mut rx) = dispatcher(1);
        // Hold the only slot with a request that waits on its body.
        let mut held = open("bb");
        held.header.insert("body_len".into(), json!(10));
        d.handle(held);
        d.handle(open("cc"));
        let mut codes = Vec::new();
        for _ in 0..2 {
            let frame = rx.recv().await.unwrap();
            codes.push((
                frame.sid().unwrap().to_string(),
                frame.str_field("code").unwrap().to_string(),
            ));
        }
        assert!(
            codes.contains(&("cc".into(), "protocol_error".into())),
            "{codes:?}"
        );
    }

    #[tokio::test]
    async fn a_cancel_aborts_the_request() {
        let (d, _rx) = dispatcher(4);
        let mut held = open("dd");
        held.header
            .insert("url".into(), json!("http://203.0.113.1/"));
        d.handle(held);
        d.handle(Frame::new(json!({"type": "cancel", "sid": "dd"}), vec![]));
        assert_eq!(d.in_flight(), 0);
    }
}
