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

//! The runner daemon core, shared by the headless CLI (`run`) and the GUI app.
//!
//! [`serve`] owns the whole serve-until-shutdown lifecycle: build the executor,
//! optionally wire the signature-verified self-updater, then run the transport
//! loop (WebSocket fast path, HTTP long-poll fallback) until the shutdown signal
//! fires or a fatal error occurs. It returns `Ok(true)` when a self-update was
//! applied and the caller should re-exec into the new binary.
//!
//! The GUI passes `enable_self_update = false` because installed builds update
//! through the OS-native bundle updater (Tauri updater); the CLI keeps the
//! bare-binary self-update path for tarball / service installs.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use anyhow::Result;
use tokio::sync::watch;
use tracing::info;

use crate::api::ApiClient;
use crate::config::RunnerConfig;
use crate::enroll::{detect_arch, detect_platform};
use crate::executor::Executor;
use crate::status::{RunnerStatus, StatusReporter};

/// Serve commands until `shutdown_tx` is tripped or a fatal error occurs.
///
/// Returns `Ok(true)` if a self-update was applied (the caller re-execs), or
/// `Ok(false)` on a clean stop. `shutdown_tx` is shared so both the caller
/// (Ctrl-C / GUI quit) and the updater can end the loop; the transport derives
/// its own receiver from it.
pub async fn serve(
    config: RunnerConfig,
    token: String,
    shutdown_tx: watch::Sender<bool>,
    status: StatusReporter,
    enable_self_update: bool,
) -> Result<bool> {
    let shutdown_rx = shutdown_tx.subscribe();

    let executor = Arc::new(Executor::new(
        config.cdp_url.clone(),
        // The server stamps its own per-command budget; this is the local
        // default when a command carries none.
        120,
    ));

    // Signature-verified self-update (CLI / bare-binary installs only). `restart`
    // is tripped by the updater once a new binary has been swapped in.
    let restart = Arc::new(AtomicBool::new(false));
    let updater = if enable_self_update {
        ApiClient::new(&config, token.clone()).ok().map(|api| {
            Arc::new(crate::update::UpdateController::new(
                api,
                detect_platform(),
                detect_arch(),
                config.auto_update,
                restart.clone(),
                shutdown_tx.clone(),
            ))
        })
    } else {
        None
    };

    // Startup pre-check: a runner too old to even connect updates HERE, before
    // the transport loop, then re-execs into the compatible build.
    if let Some(u) = &updater {
        u.maybe_apply().await;
        if restart.load(Ordering::SeqCst) {
            return Ok(true);
        }
    }

    // Periodic check (every 6h) for the long-running case.
    if let Some(u) = &updater {
        let uc = u.clone();
        let rx = shutdown_rx.clone();
        tokio::spawn(async move {
            uc.run_periodic(rx).await;
        });
    }

    status.set(RunnerStatus::Connecting);
    let result = crate::transport::run(
        config,
        token,
        executor,
        shutdown_rx,
        updater.clone(),
        status.clone(),
    )
    .await;

    // A self-update applied while running trips `restart`; the caller re-execs.
    if restart.load(Ordering::SeqCst) {
        return Ok(true);
    }

    match result {
        Ok(()) => {
            info!("runner stopped");
            status.set(RunnerStatus::Stopped);
            Ok(false)
        }
        Err(e) => {
            let message = format!("{e:#}");
            status.set(RunnerStatus::Error {
                message: message.clone(),
            });
            Err(e)
        }
    }
}
