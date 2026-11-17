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

//! The relay's own HTTP calls to the platform (`/api/v1/relays/...`):
//! enrollment, the long-poll transport, heartbeats and the self-update
//! manifest. The forwarded requests never go through here.

use std::time::Duration;

use agent_core::retry::check_transient;
use anyhow::{anyhow, bail, Context, Result};
use reqwest::redirect::Policy as RedirectPolicy;
use reqwest::{Client, StatusCode};
use serde::Deserialize;
use serde_json::Value;

use crate::config::Config;
use crate::frame::{decode_frames, Frame};
use crate::tls::TlsConfigs;
use crate::version::{user_agent, AGENT_VERSION};

/// The platform refused this relay; what to do depends on why.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Refusal {
    /// The credential is unknown or revoked (401).
    Revoked,
    /// Relays are turned off on this platform (404).
    Disabled,
    /// This version is below the platform's minimum (426).
    UpdateRequired,
    /// The hello was refused (400) - a protocol the platform does not speak.
    Refused(String),
}

impl std::fmt::Display for Refusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Revoked => f.write_str("the platform refused the relay credential (revoked?)"),
            Self::Disabled => f.write_str("relays are disabled on this platform"),
            Self::UpdateRequired => f.write_str("the platform requires a newer relay"),
            Self::Refused(why) => write!(f, "the platform refused this relay: {why}"),
        }
    }
}

impl std::error::Error for Refusal {}

/// The platform's answer to an enrollment.
#[derive(Debug, Deserialize)]
pub struct EnrollResponse {
    pub relay: Value,
    pub key: String,
    pub group: Option<EnrolledGroup>,
}

#[derive(Debug, Deserialize)]
pub struct EnrolledGroup {
    pub id: Option<String>,
    pub name: Option<String>,
}

/// Follow redirects (bounded), never from https to http: every call carries
/// the relay credential, which must not travel in clear.
fn no_downgrade() -> RedirectPolicy {
    RedirectPolicy::custom(|attempt| {
        if attempt.previous().len() >= 10 {
            return attempt.error("too many redirects");
        }
        let downgraded = attempt
            .previous()
            .last()
            .is_some_and(|prev| prev.scheme() == "https" && attempt.url().scheme() == "http");
        if downgraded {
            attempt.error(
                "refusing to follow an https -> http redirect (would expose the relay credential)",
            )
        } else {
            attempt.follow()
        }
    })
}

/// An HTTP client to the platform: the verified TLS configuration, the
/// upstream proxy when one is set (the environment's otherwise).
pub fn http_client(config: &Config, tls: &TlsConfigs, timeout: Duration) -> Result<Client> {
    let mut builder = Client::builder()
        .use_preconfigured_tls((*tls.verified).clone())
        .user_agent(user_agent())
        .timeout(timeout)
        .connect_timeout(Duration::from_secs(20))
        .redirect(no_downgrade());
    if let Some(proxy) = &config.upstream_proxy {
        builder = builder.proxy(
            reqwest::Proxy::all(proxy).with_context(|| format!("upstream_proxy '{proxy}'"))?,
        );
    }
    builder.build().context("building the HTTP client")
}

/// The detail of an error answer, for messages.
async fn detail(resp: reqwest::Response) -> String {
    let status = resp.status();
    let body = resp.text().await.unwrap_or_default();
    let detail = serde_json::from_str::<Value>(&body)
        .ok()
        .and_then(|v| v.get("detail").cloned())
        .map(|d| match d {
            Value::String(s) => s,
            other => other.to_string(),
        })
        .unwrap_or(body);
    let detail: String = detail.chars().take(300).collect();
    format!("{status}: {detail}")
}

/// A refusal for the statuses that mean one, the answer otherwise.
async fn checked(resp: reqwest::Response) -> Result<reqwest::Response> {
    match resp.status() {
        StatusCode::UNAUTHORIZED => Err(anyhow!(Refusal::Revoked)),
        StatusCode::NOT_FOUND => Err(anyhow!(Refusal::Disabled)),
        StatusCode::UPGRADE_REQUIRED => Err(anyhow!(Refusal::UpdateRequired)),
        StatusCode::BAD_REQUEST => Err(anyhow!(Refusal::Refused(detail(resp).await))),
        _ => {
            let resp = check_transient(resp)?;
            if !resp.status().is_success() {
                bail!("platform answered {}", detail(resp).await);
            }
            Ok(resp)
        }
    }
}

/// Exchange an enrollment key for the durable credential.
pub async fn enroll(client: &Client, server_url: &str, body: &Value) -> Result<EnrollResponse> {
    let url = format!("{server_url}/api/v1/relays/enroll");
    let resp = client
        .post(&url)
        .json(body)
        .send()
        .await
        .with_context(|| format!("reaching {url}"))?;
    let status = resp.status();
    if !status.is_success() {
        let text = detail(resp).await;
        if status == StatusCode::FORBIDDEN && text.to_ascii_lowercase().contains("https") {
            bail!(
                "enrollment refused ({text}): the platform could not confirm this connection \
                 arrived over HTTPS. Behind a TLS-terminating proxy, its administrator must set \
                 FORWARDED_ALLOW_IPS to the proxy's address (and have the proxy send \
                 X-Forwarded-Proto)."
            );
        }
        bail!("enrollment failed ({text})");
    }
    resp.json().await.context("reading the enrollment answer")
}

/// The relay's authenticated calls.
#[derive(Clone)]
pub struct PlatformApi {
    http: Client,
    base: String,
    token: String,
    poll_timeout: Duration,
}

impl PlatformApi {
    pub fn new(http: Client, server_url: &str, token: String, poll_timeout: Duration) -> Self {
        Self {
            http,
            base: format!("{server_url}/api/v1/relays"),
            token,
            poll_timeout,
        }
    }

    /// `wss://` for the WebSocket fast path.
    pub fn ws_url(&self) -> String {
        let base = if let Some(rest) = self.base.strip_prefix("https://") {
            format!("wss://{rest}")
        } else if let Some(rest) = self.base.strip_prefix("http://") {
            format!("ws://{rest}")
        } else {
            self.base.clone()
        };
        format!("{base}/connect?version={}", urlencode(AGENT_VERSION))
    }

    pub fn token(&self) -> &str {
        &self.token
    }

    pub fn set_poll_timeout(&mut self, seconds: u64) {
        self.poll_timeout = Duration::from_secs(seconds.clamp(5, 300));
    }

    fn version_query() -> String {
        format!("version={}", urlencode(AGENT_VERSION))
    }

    /// The long-poll transport's hello; the answer is the `hello_ack`.
    pub async fn hello(&self, hello: &Value) -> Result<Value> {
        let resp = self
            .http
            .post(format!("{}/hello?{}", self.base, Self::version_query()))
            .bearer_auth(&self.token)
            .json(hello)
            .send()
            .await
            .context("relay hello")?;
        checked(resp)
            .await?
            .json()
            .await
            .context("reading the hello answer")
    }

    /// One long poll: the opens to start and the frames of held requests.
    pub async fn poll(&self, free: usize) -> Result<Vec<Frame>> {
        let resp = self
            .http
            .get(format!(
                "{}/poll?free={free}&wait=true&{}",
                self.base,
                Self::version_query()
            ))
            .bearer_auth(&self.token)
            .timeout(self.poll_timeout + Duration::from_secs(15))
            .send()
            .await
            .context("relay poll")?;
        let bytes = checked(resp)
            .await?
            .bytes()
            .await
            .context("reading the poll answer")?;
        decode_frames(&bytes).context("decoding the poll answer")
    }

    /// Upload response frames (concatenated, each with `len`).
    pub async fn upload(&self, body: Vec<u8>) -> Result<()> {
        let resp = self
            .http
            .post(format!("{}/frames", self.base))
            .bearer_auth(&self.token)
            .header(reqwest::header::CONTENT_TYPE, "application/octet-stream")
            .body(body)
            .send()
            .await
            .context("relay frame upload")?;
        checked(resp).await?;
        Ok(())
    }

    /// A long-poll heartbeat; `true` when the platform requires a newer build.
    pub async fn heartbeat(&self) -> Result<bool> {
        let resp = self
            .http
            .post(format!("{}/heartbeat", self.base))
            .bearer_auth(&self.token)
            .json(&serde_json::json!({ "agent_version": AGENT_VERSION }))
            .send()
            .await
            .context("relay heartbeat")?;
        let body: Value = checked(resp).await?.json().await.unwrap_or(Value::Null);
        Ok(body
            .get("update_required")
            .and_then(Value::as_bool)
            .unwrap_or(false))
    }

    /// The self-update manifest for this machine.
    pub async fn update_manifest(&self, os: &str, arch: &str) -> Result<Value> {
        let resp = self
            .http
            .get(format!(
                "{}/update-manifest?os={os}&arch={arch}&current_version={}",
                self.base,
                urlencode(AGENT_VERSION)
            ))
            .send()
            .await
            .context("update manifest")?;
        checked(resp)
            .await?
            .json()
            .await
            .context("reading the update manifest")
    }

    /// Download a release artifact, refusing one larger than `cap`.
    #[cfg_attr(not(feature = "self-update"), allow(dead_code))]
    pub async fn download(&self, url: &str, cap: usize) -> Result<Vec<u8>> {
        let parsed = url::Url::parse(url).with_context(|| format!("artifact URL '{url}'"))?;
        crate::config::require_secure_server_url(parsed.as_str())?;
        let mut resp = checked(
            self.http
                .get(url)
                .timeout(Duration::from_secs(600))
                .send()
                .await
                .with_context(|| format!("downloading {url}"))?,
        )
        .await?;
        let mut out = Vec::new();
        while let Some(chunk) = resp
            .chunk()
            .await
            .with_context(|| format!("downloading {url}"))?
        {
            out.extend_from_slice(&chunk);
            if out.len() > cap {
                bail!("{url} is larger than {cap} bytes");
            }
        }
        Ok(out)
    }
}

/// Percent-encode a query value (versions are `1.2.3` or `0.0.0-dev`; this
/// keeps them readable and safe).
fn urlencode(text: &str) -> String {
    text.bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                (b as char).to_string()
            }
            other => format!("%{other:02X}"),
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_websocket_url_follows_the_server_scheme() {
        let api = PlatformApi::new(
            Client::new(),
            "https://one.example.com",
            "rly-x".into(),
            Duration::from_secs(25),
        );
        assert!(api
            .ws_url()
            .starts_with("wss://one.example.com/api/v1/relays/connect?version="));
        let local = PlatformApi::new(
            Client::new(),
            "http://localhost:4000",
            "rly-x".into(),
            Duration::from_secs(25),
        );
        assert!(local
            .ws_url()
            .starts_with("ws://localhost:4000/api/v1/relays/connect?"));
    }

    #[test]
    fn query_values_are_encoded() {
        assert_eq!(urlencode("0.0.0-dev"), "0.0.0-dev");
        assert_eq!(urlencode("a b&c"), "a%20b%26c");
    }
}
