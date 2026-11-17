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

//! The running relay: configuration, credential, the tunnel to the platform,
//! the configuration watcher, the update check and the health endpoint.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, SystemTime};

use anyhow::{bail, Context, Result};
use tokio::sync::watch;
use tracing::{debug, error, info, warn};

use crate::api::PlatformApi;
use crate::config::{Config, State};
use crate::enroll::{enroll, resolve_token, EnrollOptions};
use crate::forward::Forwarder;
use crate::status::{State as RelayState, StatusReporter};
use crate::tls::TlsConfigs;
use crate::tunnel::{Outcome, Tunnel};
use crate::update::{UpdateOutcome, Updater, CHECK_INTERVAL};
use crate::version::AGENT_VERSION;

/// Run the relay until `shutdown` (or until a self-update needs a restart).
pub async fn run(config_path: PathBuf, shutdown: watch::Receiver<bool>) -> Result<Outcome> {
    crate::tls::install_default_provider();
    let config = Config::load(&config_path)?;
    for warning in config.check()? {
        warn!("{warning}");
    }
    info!(
        "XTM One Relay {AGENT_VERSION} starting (config {})",
        config.path.display()
    );

    let mut state = State::load(&config)?;
    let token = match resolve_token(&config, state.as_ref())? {
        Some(token) => token,
        None => match std::env::var("XTM_RELAY_ENROLLMENT_KEY")
            .ok()
            .map(|k| k.trim().to_string())
            .filter(|k| !k.is_empty())
        {
            // A container enrolls itself on its first start.
            Some(key) => {
                let server_url = config.server_url()?;
                let enrolled = enroll(
                    &config,
                    EnrollOptions {
                        server_url,
                        enrollment_key: key,
                        name: config.name.clone(),
                        force: false,
                    },
                )
                .await
                .context("enrolling with XTM_RELAY_ENROLLMENT_KEY")?;
                state = Some(enrolled);
                resolve_token(&config, state.as_ref())?
                    .context("the credential enrollment stored cannot be read back")?
            }
            None => {
                let status =
                    StatusReporter::new(config.status_path(), None, config.server_url.clone());
                status.set(RelayState::NotEnrolled, None, None);
                bail!(
                    "this relay is not enrolled: run `xtm-one-relay enroll --server-url <url> --key \
                     rlenr-...` (or set XTM_RELAY_KEY / XTM_RELAY_ENROLLMENT_KEY)"
                );
            }
        },
    };
    let server_url = match (&config.server_url, &state) {
        (Some(_), _) => config.server_url()?,
        (None, Some(state)) => {
            crate::config::require_secure_server_url(&state.server_url)?;
            state.server_url.clone()
        }
        (None, None) => config.server_url()?,
    };

    let tls = TlsConfigs::build(config.ca_bundle.as_deref())?;
    crate::tls::warn_unverified(&config.insecure_tls_destinations);
    let forwarder = Arc::new(Forwarder::new(
        config.policy()?,
        config.insecure_tls_policy()?,
        tls.clone(),
    ));
    let status = Arc::new(StatusReporter::new(
        config.status_path(),
        state.as_ref().map(|s| s.relay_id.clone()),
        Some(server_url.clone()),
    ));
    let http = crate::api::http_client(&config, &tls, Duration::from_secs(120))?;
    let api = PlatformApi::new(http, &server_url, token, Duration::from_secs(25));
    let updater = Arc::new(Updater::new(api.clone(), config.auto_update));

    if let Some(listen) = config.health_listen {
        let (status, forwarder) = (status.clone(), forwarder.clone());
        tokio::spawn(async move {
            if let Err(e) = crate::health::serve(listen, status, forwarder).await {
                error!("health endpoint: {e:#}");
            }
        });
    }
    let (rehello_tx, rehello_rx) = watch::channel(0u64);
    let watcher = tokio::spawn(watch_config(
        config.path.clone(),
        config.clone(),
        forwarder.clone(),
        rehello_tx,
    ));
    let (updated_tx, mut updated_rx) = watch::channel(false);
    let update_loop = tokio::spawn(update_loop(updater.clone(), updated_tx));

    let tunnel = Tunnel::new(
        config,
        api,
        tls,
        forwarder,
        status.clone(),
        updater,
        rehello_rx,
    );
    let outcome = tokio::select! {
        outcome = tunnel.run(shutdown) => outcome,
        _ = updated_rx.changed() => Outcome::Updated,
    };
    watcher.abort();
    update_loop.abort();
    if outcome == Outcome::Shutdown {
        status.set(RelayState::Stopped, None, None);
        info!("relay stopped");
    }
    Ok(outcome)
}

/// Ask for a newer build now and then; apply it when allowed.
async fn update_loop(updater: Arc<Updater>, updated: watch::Sender<bool>) {
    tokio::time::sleep(Duration::from_secs(120)).await;
    let mut announced: Option<String> = None;
    loop {
        match updater.check_and_apply().await {
            UpdateOutcome::Applied(_) => {
                let _ = updated.send(true);
                return;
            }
            UpdateOutcome::Available { version, reason } => {
                if announced.as_deref() != Some(version.as_str()) {
                    info!("relay {version} is available, not applied: {reason}");
                    announced = Some(version);
                }
            }
            UpdateOutcome::UpToDate => debug!("relay is up to date"),
            UpdateOutcome::Failed(why) => warn!("relay update check: {why}"),
        }
        tokio::time::sleep(CHECK_INTERVAL).await;
    }
}

fn modified(path: &Path) -> Option<SystemTime> {
    std::fs::metadata(path).and_then(|m| m.modified()).ok()
}

/// Reload the allow-lists when relay.toml changes (or on SIGHUP); everything
/// else needs a restart, which is said.
async fn watch_config(
    path: PathBuf,
    mut current: Config,
    forwarder: Arc<Forwarder>,
    rehello: watch::Sender<u64>,
) {
    let mut last = modified(&path);
    #[cfg(unix)]
    let mut hangup = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::hangup()).ok();
    loop {
        #[cfg(unix)]
        let signalled = {
            let hup = async {
                match hangup.as_mut() {
                    Some(signal) => {
                        signal.recv().await;
                    }
                    None => std::future::pending::<()>().await,
                }
            };
            tokio::select! {
                _ = tokio::time::sleep(Duration::from_secs(5)) => false,
                _ = hup => true,
            }
        };
        #[cfg(not(unix))]
        let signalled = {
            tokio::time::sleep(Duration::from_secs(5)).await;
            false
        };
        let now = modified(&path);
        if !signalled && now == last {
            continue;
        }
        last = now;
        let reloaded = Config::load(&path).and_then(|c| {
            let policy = c.policy()?;
            let unverified = c.insecure_tls_policy()?;
            Ok((c, policy, unverified))
        });
        match reloaded {
            Ok((config, policy, unverified)) => {
                let changed = config.allowed_destinations != current.allowed_destinations
                    || config.insecure_tls_destinations != current.insecure_tls_destinations;
                if changed {
                    info!(
                        "reloaded {}: allowed destinations now {}",
                        path.display(),
                        if policy.is_empty() {
                            "none".to_string()
                        } else {
                            policy.declared().join(", ")
                        }
                    );
                    crate::tls::warn_unverified(&config.insecure_tls_destinations);
                    forwarder.set_policy(policy, unverified);
                    rehello.send_modify(|generation| *generation += 1);
                }
                let restart_needed = config.server_url != current.server_url
                    || config.ca_bundle != current.ca_bundle
                    || config.upstream_proxy != current.upstream_proxy
                    || config.transport != current.transport
                    || config.max_concurrent_requests != current.max_concurrent_requests
                    || config.health_listen != current.health_listen
                    || config.auto_update != current.auto_update;
                if restart_needed {
                    warn!(
                        "{} changed settings that apply at the next start: restart the relay",
                        path.display()
                    );
                }
                current = config;
            }
            Err(e) => error!("{} not reloaded: {e:#}", path.display()),
        }
    }
}
