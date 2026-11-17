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

//! One-time enrollment: exchange a `renr-` key for a durable `run-` credential.
//!
//! Reports host facts up front so the new [`Runner`] row lands fully described
//! (the agent can `list_runners` and plan immediately). The raw `run-` key is
//! returned once and stored in the OS keychain; only the runner id + server URL
//! go to the config file.

use anyhow::{anyhow, Context, Result};

use crate::api;
use crate::config::RunnerConfig;
use crate::credential;
use crate::host_facts;
use crate::protocol::EnrollRequest;
use crate::version::AGENT_VERSION;

/// The OS family and CPU architecture in the vocabulary the update manifest
/// speaks (`windows` | `macos` | `linux`, `x64` | `arm64`), shared with the
/// relay.
pub use agent_core::platform::{detect_arch, detect_platform};

pub struct EnrollOptions {
    pub server_url: String,
    pub enrollment_key: String,
    pub name: Option<String>,
    pub cdp_url: Option<String>,
}

/// Refuse to enroll over an unencrypted transport. Enrollment sends the
/// reusable `renr-` key up and brings the durable `run-` credential back, so
/// plain `http://` must not be used except against a loopback host (local
/// development). Every other host is required to be `https://`.
fn require_secure_server_url(server_url: &str) -> Result<()> {
    let url = url::Url::parse(server_url)
        .with_context(|| format!("invalid server URL '{server_url}'"))?;
    match url.scheme() {
        "https" => Ok(()),
        "http" => {
            let loopback = match url.host() {
                Some(url::Host::Domain(d)) => d.eq_ignore_ascii_case("localhost"),
                Some(url::Host::Ipv4(ip)) => ip.is_loopback(),
                Some(url::Host::Ipv6(ip)) => ip.is_loopback(),
                None => false,
            };
            if loopback {
                Ok(())
            } else {
                Err(anyhow!(
                    "refusing to enroll over plain http:// to a non-loopback host; \
                     use https:// so the enrollment key and runner credential are \
                     never sent in cleartext"
                ))
            }
        }
        other => Err(anyhow!("unsupported server URL scheme '{other}://'")),
    }
}

/// Perform enrollment, persist config + credential, and return the config.
pub async fn run(opts: EnrollOptions) -> Result<RunnerConfig> {
    require_secure_server_url(&opts.server_url)?;
    let platform = detect_platform().to_string();
    let hostname = hostname::get().ok().and_then(|h| h.into_string().ok());
    let facts = host_facts::collect_json().await;

    let request = EnrollRequest {
        enrollment_key: opts.enrollment_key,
        platform: platform.clone(),
        hostname: hostname.clone(),
        name: opts.name.clone(),
        host_facts: Some(facts),
        agent_version: Some(AGENT_VERSION.to_string()),
        // Lets the server fold a re-enrollment of this same machine into its
        // existing runner row instead of listing the machine twice.
        machine_id: host_facts::machine_id().await,
    };

    let resp = api::enroll(&opts.server_url, &request)
        .await
        .context("enrolling with the platform")?;

    let mut config = RunnerConfig {
        server_url: opts.server_url.trim_end_matches('/').to_string(),
        runner_id: resp.runner.id.clone(),
        name: resp
            .runner
            .name
            .clone()
            .or_else(|| opts.name.clone())
            .or_else(|| hostname.clone()),
        platform,
        heartbeat_interval_seconds: resp.heartbeat_interval_seconds.unwrap_or(30),
        long_poll_timeout_seconds: resp.long_poll_timeout_seconds.unwrap_or(25),
        cdp_url: opts
            .cdp_url
            .unwrap_or_else(|| "http://127.0.0.1:9222".to_string()),
        auto_update: true,
        launch_at_login: Some(true),
    };
    if config.heartbeat_interval_seconds == 0 {
        config.heartbeat_interval_seconds = 30;
    }
    if config.long_poll_timeout_seconds == 0 {
        config.long_poll_timeout_seconds = 25;
    }

    config.save().context("saving runner config")?;
    credential::store(&config.runner_id, &resp.key)
        .context("storing runner credential in the OS keychain")?;

    tracing::info!(
        runner_id = %resp.runner.id,
        name = resp.runner.name.as_deref().unwrap_or("(unnamed)"),
        permission_mode = resp.runner.permission_mode.as_deref().unwrap_or("restricted"),
        "enrolled with the platform"
    );

    Ok(config)
}
