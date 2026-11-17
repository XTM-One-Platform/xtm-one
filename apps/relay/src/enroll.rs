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

//! Enrollment: exchange an `rlenr-` key (minted in XTM One, Relays > a group >
//! Enroll a relay) for the durable `rly-` credential this relay authenticates
//! with from then on.
//!
//! The credential is an owner-only file in the relay's state directory - not
//! the OS keychain: the relay runs as a service account, which is rarely the
//! account that enrolled it - or, in a container, the `XTM_RELAY_KEY`
//! environment variable.

use std::time::Duration;

use agent_core::credential::CredentialStore;
use anyhow::{bail, Context, Result};
use serde_json::json;
use tracing::info;

use crate::config::{prepare_dir, require_secure_server_url, write_enrollment, Config, State};
use crate::tls::TlsConfigs;
use crate::version::AGENT_VERSION;

/// The keychain-free store of the relay credential, in the state directory.
pub fn credential_store(config: &Config) -> CredentialStore {
    CredentialStore::files_in("xtm-one-relay", config.dir.clone())
}

/// The salt of this agent's machine identity (the runner uses its own).
pub const MACHINE_ID_SALT: &[u8] = b"xtm-one-relay:";

/// The credential to authenticate with: `XTM_RELAY_KEY`, else the one
/// enrollment stored.
pub fn resolve_token(config: &Config, state: Option<&State>) -> Result<Option<String>> {
    if let Ok(key) = std::env::var("XTM_RELAY_KEY") {
        let key = key.trim().to_string();
        if !key.is_empty() {
            if !key.starts_with("rly-") {
                bail!("XTM_RELAY_KEY must be a relay credential (rly-...)");
            }
            return Ok(Some(key));
        }
    }
    let Some(state) = state else {
        return Ok(None);
    };
    credential_store(config).load(&state.relay_id)
}

pub struct EnrollOptions {
    pub server_url: String,
    pub enrollment_key: String,
    pub name: Option<String>,
    pub force: bool,
}

/// Enroll this machine; returns what was recorded.
pub async fn enroll(config: &Config, options: EnrollOptions) -> Result<State> {
    let server_url = options.server_url.trim().trim_end_matches('/').to_string();
    require_secure_server_url(&server_url)?;
    let key = options.enrollment_key.trim();
    if !key.starts_with("rlenr-") {
        bail!("the enrollment key must start with 'rlenr-' (mint one in XTM One: Relays > a group > Enroll a relay)");
    }
    let previous = State::load(config)?;
    if let Some(previous) = &previous {
        if !options.force {
            bail!(
                "this machine is already enrolled (relay {} on {}); pass --force to enroll again",
                previous.relay_id,
                previous.server_url
            );
        }
    }

    let tls = TlsConfigs::build(config.ca_bundle.as_deref())?;
    let client = crate::api::http_client(config, &tls, Duration::from_secs(60))?;
    let hostname = crate::host::hostname();
    let name = options.name.clone().or_else(|| config.name.clone());
    let body = json!({
        "enrollment_key": key,
        "platform": agent_core::platform::detect_platform(),
        "hostname": hostname,
        "name": name.clone().unwrap_or_else(|| hostname.clone()),
        "host_facts": crate::host::facts(config),
        "agent_version": AGENT_VERSION,
        "machine_id": agent_core::platform::machine_id(MACHINE_ID_SALT).await,
    });
    let answer = crate::api::enroll(&client, &server_url, &body).await?;
    let relay_id = answer
        .relay
        .get("id")
        .and_then(|v| v.as_str())
        .context("the enrollment answer names no relay")?
        .to_string();
    if !answer.key.starts_with("rly-") {
        bail!("the platform answered an unexpected credential");
    }

    prepare_dir(&config.dir)?;
    let store = credential_store(config);
    store.store(&relay_id, &answer.key)?;
    if let Some(previous) = &previous {
        if previous.relay_id != relay_id {
            let _ = store.delete(&previous.relay_id);
        }
    }
    let state = State {
        relay_id,
        server_url: server_url.clone(),
        group_id: answer.group.as_ref().and_then(|g| g.id.clone()),
        group_name: answer.group.as_ref().and_then(|g| g.name.clone()),
        enrolled_at: chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true),
    };
    state.save(config)?;
    write_enrollment(&config.path, &server_url, options.name.as_deref())?;
    info!(
        "enrolled as relay {} in group {}",
        state.relay_id,
        state.group_name.as_deref().unwrap_or("?")
    );
    Ok(state)
}

/// Forget the enrollment: the credential and the state (relay.toml is kept).
pub fn reset(config: &Config) -> Result<Option<String>> {
    let state = State::load(config)?;
    if let Some(state) = &state {
        credential_store(config).delete(&state.relay_id)?;
    }
    State::delete(config)?;
    let _ = std::fs::remove_file(config.status_path());
    Ok(state.map(|s| s.relay_id))
}
