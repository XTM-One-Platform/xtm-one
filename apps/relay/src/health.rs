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

//! A minimal local health endpoint for service managers and probes:
//! `GET /healthz` answers 200 while the relay is connected to the platform and
//! 503 otherwise, with the status as JSON. Off unless `health_listen` is set.
//! It reads nothing of the requests the relay serves.

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tracing::{debug, info};

use crate::forward::Forwarder;
use crate::status::{State, StatusReporter};

pub async fn serve(
    listen: SocketAddr,
    status: Arc<StatusReporter>,
    forwarder: Arc<Forwarder>,
) -> Result<()> {
    let listener = TcpListener::bind(listen)
        .await
        .with_context(|| format!("binding the health endpoint on {listen}"))?;
    info!("health endpoint on http://{listen}/healthz");
    loop {
        let (mut socket, _) = match listener.accept().await {
            Ok(accepted) => accepted,
            Err(e) => {
                debug!("health endpoint accept: {e}");
                tokio::time::sleep(Duration::from_millis(200)).await;
                continue;
            }
        };
        let status = status.clone();
        let forwarder = forwarder.clone();
        tokio::spawn(async move {
            let mut buf = [0u8; 1024];
            let read = tokio::time::timeout(Duration::from_secs(2), socket.read(&mut buf)).await;
            let Ok(Ok(n)) = read else { return };
            let request = String::from_utf8_lossy(&buf[..n]);
            let path = request
                .lines()
                .next()
                .and_then(|line| line.split_whitespace().nth(1))
                .unwrap_or("");
            let (code, body) = if path == "/healthz" || path == "/" {
                let snapshot = status.snapshot(forwarder.in_flight());
                let code = if snapshot.state == State::Online {
                    "200 OK"
                } else {
                    "503 Service Unavailable"
                };
                (code, serde_json::to_string(&snapshot).unwrap_or_default())
            } else {
                ("404 Not Found", "{}".to_string())
            };
            let answer = format!(
                "HTTP/1.1 {code}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\
                 Cache-Control: no-store\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            let _ = socket.write_all(answer.as_bytes()).await;
            let _ = socket.shutdown().await;
        });
    }
}
