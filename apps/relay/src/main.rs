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

//! XTM One Relay - a pull-based egress proxy for XTM One.
//!
//! A customer runs it next to the tools a SaaS platform cannot reach. It
//! connects OUT to the platform (WebSocket, HTTP long-poll fallback), pulls the
//! requests routed through its relay group, sends them to the destinations its
//! local allow-list admits and streams the answers back. Nothing connects to it.
//!
//! ```text
//! xtm-one-relay enroll --server-url https://xtm-one.example.com --key rlenr-...
//! xtm-one-relay check-config
//! sudo xtm-one-relay service install
//! xtm-one-relay status
//! ```

mod api;
mod config;
mod daemon;
mod dispatch;
mod enroll;
mod forward;
mod frame;
mod health;
mod host;
mod policy;
mod service;
mod status;
mod tls;
mod tunnel;
mod update;
mod version;

use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::Arc;

use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand};
use serde_json::json;
use tokio::sync::{mpsc, watch};

use crate::config::{Config, State};
use crate::forward::Forwarder;
use crate::frame::Frame;
use crate::status::StatusReporter;
use crate::tunnel::Outcome;

#[derive(Parser)]
#[command(
    name = "xtm-one-relay",
    version = version::AGENT_VERSION,
    about = "XTM One Relay: a pull-based egress proxy for XTM One"
)]
struct Cli {
    /// The configuration file (relay.toml); its directory holds the relay's state.
    #[arg(long, global = true, env = "XTM_RELAY_CONFIG")]
    config: Option<PathBuf>,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Enroll this machine in a relay group with an enrollment key.
    Enroll {
        /// The XTM One platform (https://...).
        #[arg(long)]
        server_url: Option<String>,
        /// The rlenr- enrollment key minted in XTM One (Relays > a group > Enroll a relay).
        #[arg(long, env = "XTM_RELAY_ENROLLMENT_KEY", hide_env_values = true)]
        key: String,
        /// How the relay shows in XTM One (the hostname otherwise).
        #[arg(long)]
        name: Option<String>,
        /// Enroll again even if this machine is already enrolled.
        #[arg(long)]
        force: bool,
    },
    /// Run the relay in the foreground (what the service runs).
    Run,
    /// Show whether the relay is enrolled and connected (exit code 0 when online).
    Status {
        /// The status as JSON.
        #[arg(long)]
        json: bool,
    },
    /// Validate the configuration and show the effective allow-list.
    CheckConfig,
    /// Send one GET from this machine through the relay's own checks and client.
    TestDestination {
        /// The URL to request (http:// or https://).
        url: String,
    },
    /// Forget the enrollment (credential and state); relay.toml is kept.
    Reset {
        /// Do not ask for confirmation.
        #[arg(long)]
        yes: bool,
    },
    /// Install, remove, start or stop the OS service.
    Service {
        #[command(subcommand)]
        action: ServiceAction,
    },
}

#[derive(Subcommand)]
enum ServiceAction {
    /// Install the relay as a system service and start it (needs root / an elevated prompt).
    Install {
        /// The account the service runs as (Linux and macOS); root otherwise.
        #[arg(long)]
        user: Option<String>,
    },
    /// Stop and remove the system service.
    Uninstall,
    /// Start the installed service.
    Start,
    /// Stop the installed service.
    Stop,
    /// The service entry point (started by the service manager).
    #[command(hide = true)]
    Run,
}

fn init_logging(level: &str, log_file: Option<&Path>) {
    let filter = tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| {
        tracing_subscriber::EnvFilter::new(format!("xtm_one_relay={level},agent_core={level},warn"))
    });
    let builder = tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_target(false);
    match log_file.and_then(|path| {
        // Keep one bounded log: start over past 10 MB.
        if std::fs::metadata(path)
            .map(|m| m.len() > 10 * 1024 * 1024)
            .unwrap_or(false)
        {
            let _ = std::fs::remove_file(path);
        }
        std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
            .ok()
    }) {
        Some(file) => {
            let _ = builder
                .with_ansi(false)
                .with_writer(std::sync::Mutex::new(file))
                .try_init();
        }
        None => {
            let _ = builder.with_writer(std::io::stderr).try_init();
        }
    }
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    let config_path = cli
        .config
        .clone()
        .unwrap_or_else(config::default_config_path);
    let level = Config::load(&config_path)
        .map(|c| c.log_level)
        .unwrap_or_else(|_| "info".into());

    // The Windows service hands the process to the service dispatcher before
    // any runtime exists; it builds its own.
    #[cfg(windows)]
    if let Command::Service {
        action: ServiceAction::Run,
    } = &cli.command
    {
        let log = config_path.parent().map(|d| d.join("relay.log"));
        init_logging(&level, log.as_deref());
        return match service::windows::run_dispatcher(config_path) {
            Ok(()) => ExitCode::SUCCESS,
            Err(e) => {
                tracing::error!("{e:#}");
                ExitCode::FAILURE
            }
        };
    }

    let quiet = !matches!(
        cli.command,
        Command::Run
            | Command::Service {
                action: ServiceAction::Run
            }
    );
    init_logging(if quiet { "warn" } else { &level }, None);

    let runtime = match tokio::runtime::Runtime::new() {
        Ok(runtime) => runtime,
        Err(e) => {
            eprintln!("error: starting the runtime: {e}");
            return ExitCode::FAILURE;
        }
    };
    match runtime.block_on(dispatch_command(cli.command, config_path)) {
        Ok(code) => code,
        Err(e) => {
            eprintln!("error: {e:#}");
            ExitCode::FAILURE
        }
    }
}

async fn dispatch_command(command: Command, config_path: PathBuf) -> Result<ExitCode> {
    match command {
        Command::Enroll {
            server_url,
            key,
            name,
            force,
        } => {
            let config = Config::load(&config_path)?;
            let server_url = server_url
                .or_else(|| config.server_url.clone())
                .context("pass --server-url (the XTM One address)")?;
            let state = enroll::enroll(
                &config,
                enroll::EnrollOptions {
                    server_url,
                    enrollment_key: key,
                    name,
                    force,
                },
            )
            .await?;
            println!(
                "Enrolled as relay {} in group {}.",
                state.relay_id,
                state.group_name.as_deref().unwrap_or("?")
            );
            println!("Configuration: {}", config.path.display());
            let reloaded = Config::load(&config_path)?;
            if reloaded.allowed_destinations.is_empty() {
                println!(
                    "Next: list the destinations this relay may reach in allowed_destinations, \
                     then run `xtm-one-relay service install` (as root / an elevated prompt)."
                );
            } else {
                println!(
                    "Next: run `xtm-one-relay service install` (as root / an elevated prompt)."
                );
            }
            Ok(ExitCode::SUCCESS)
        }
        Command::Run
        | Command::Service {
            action: ServiceAction::Run,
        } => run_foreground(config_path).await,
        Command::Status { json } => status(&config_path, json),
        Command::CheckConfig => check_config(&config_path),
        Command::TestDestination { url } => test_destination(&config_path, &url).await,
        Command::Reset { yes } => {
            let config = Config::load(&config_path)?;
            if !yes {
                eprint!("Forget this relay's enrollment (credential and state)? [y/N] ");
                let mut answer = String::new();
                std::io::stdin().read_line(&mut answer)?;
                if !matches!(answer.trim(), "y" | "Y" | "yes") {
                    println!("Nothing changed.");
                    return Ok(ExitCode::SUCCESS);
                }
            }
            match enroll::reset(&config)? {
                Some(id) => println!(
                    "Relay {id} forgotten. Revoke it in XTM One if it should not come back."
                ),
                None => println!("This machine was not enrolled."),
            }
            Ok(ExitCode::SUCCESS)
        }
        Command::Service { action } => {
            match action {
                ServiceAction::Install { user } => {
                    service::install(&service::InstallOptions {
                        config: config_path.clone(),
                        user,
                    })?;
                    println!("Service {} installed and started.", service::SERVICE_NAME);
                }
                ServiceAction::Uninstall => {
                    service::uninstall()?;
                    println!("Service {} removed.", service::SERVICE_NAME);
                }
                ServiceAction::Start => service::start()?,
                ServiceAction::Stop => service::stop()?,
                ServiceAction::Run => unreachable!("handled above"),
            }
            Ok(ExitCode::SUCCESS)
        }
    }
}

/// Run until Ctrl-C / SIGTERM; restart on a self-update.
async fn run_foreground(config_path: PathBuf) -> Result<ExitCode> {
    let (shutdown_tx, shutdown_rx) = watch::channel(false);
    tokio::spawn(async move {
        wait_for_signal().await;
        let _ = shutdown_tx.send(true);
    });
    match daemon::run(config_path, shutdown_rx).await? {
        Outcome::Shutdown => Ok(ExitCode::SUCCESS),
        Outcome::Updated => restart_self(),
    }
}

#[cfg(unix)]
async fn wait_for_signal() {
    use tokio::signal::unix::{signal, SignalKind};
    let mut term = signal(SignalKind::terminate()).ok();
    tokio::select! {
        _ = tokio::signal::ctrl_c() => {}
        _ = async {
            match term.as_mut() {
                Some(term) => { term.recv().await; }
                None => std::future::pending::<()>().await,
            }
        } => {}
    }
}

#[cfg(not(unix))]
async fn wait_for_signal() {
    let _ = tokio::signal::ctrl_c().await;
}

/// Replace this process with the updated binary.
fn restart_self() -> Result<ExitCode> {
    let exe = std::env::current_exe()?;
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        let err = std::process::Command::new(&exe).args(&args).exec();
        bail!("restarting {} after the update: {err}", exe.display());
    }
    #[cfg(not(unix))]
    {
        std::process::Command::new(&exe)
            .args(&args)
            .spawn()
            .with_context(|| format!("restarting {} after the update", exe.display()))?;
        Ok(ExitCode::SUCCESS)
    }
}

#[cfg(unix)]
fn process_alive(pid: u32) -> bool {
    // SAFETY: signal 0 only checks that the process exists.
    unsafe { libc::kill(pid as libc::pid_t, 0) == 0 }
}

#[cfg(not(unix))]
fn process_alive(_pid: u32) -> bool {
    true
}

fn status(config_path: &Path, as_json: bool) -> Result<ExitCode> {
    let config = Config::load(config_path)?;
    let state = State::load(&config)?;
    let credential = if std::env::var("XTM_RELAY_KEY").is_ok_and(|k| !k.trim().is_empty()) {
        "XTM_RELAY_KEY"
    } else {
        match &state {
            Some(state) => match enroll::credential_store(&config).load(&state.relay_id) {
                Ok(Some(_)) => "stored",
                Ok(None) => "missing",
                Err(_) => "unreadable",
            },
            None => "none",
        }
    };
    let mut running = StatusReporter::read(&config.status_path())?;
    if let Some(status) = &mut running {
        if !process_alive(status.pid) && status.state != status::State::Stopped {
            status.state = status::State::Stopped;
            status.transport = None;
        }
    }
    let online = running
        .as_ref()
        .is_some_and(|s| s.state == status::State::Online);
    if as_json {
        println!(
            "{}",
            serde_json::to_string_pretty(&json!({
                "version": version::AGENT_VERSION,
                "config": config.path,
                "server_url": config.server_url.clone().or(state.as_ref().map(|s| s.server_url.clone())),
                "enrollment": state,
                "credential": credential,
                "status": running,
                "allowed_destinations": config.allowed_destinations,
            }))?
        );
    } else {
        println!("XTM One Relay {}", version::AGENT_VERSION);
        println!("  Configuration : {}", config.path.display());
        let server = config
            .server_url
            .clone()
            .or(state.as_ref().map(|s| s.server_url.clone()));
        println!(
            "  Platform      : {}",
            server.as_deref().unwrap_or("not set")
        );
        match &state {
            Some(state) => println!(
                "  Enrolled      : relay {} in group {} (since {})",
                state.relay_id,
                state.group_name.as_deref().unwrap_or("?"),
                state.enrolled_at
            ),
            None => println!("  Enrolled      : no"),
        }
        println!("  Credential    : {credential}");
        match &running {
            Some(status) => {
                let transport = status
                    .transport
                    .as_deref()
                    .map(|t| format!(" over {t}"))
                    .unwrap_or_default();
                println!(
                    "  State         : {}{transport} since {}",
                    status.state.label(),
                    status.since
                );
                if let Some(error) = &status.last_error {
                    println!("  Last error    : {error}");
                }
            }
            None => println!("  State         : never started"),
        }
        let allowed = if config.allowed_destinations.is_empty() {
            "none (every request is refused)".to_string()
        } else {
            config.allowed_destinations.join(", ")
        };
        println!("  Allowed       : {allowed}");
    }
    Ok(if online {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(3)
    })
}

fn check_config(config_path: &Path) -> Result<ExitCode> {
    let config = Config::load(config_path)?;
    let warnings = config.check()?;
    let policy = config.policy()?;
    println!("{} is valid.", config.path.display());
    println!(
        "  Platform            : {}",
        config.server_url.as_deref().unwrap_or("not set")
    );
    println!(
        "  Allowed destinations: {}",
        if policy.is_empty() {
            "none".to_string()
        } else {
            policy.declared().join(", ")
        }
    );
    println!("  Transport           : {:?}", config.transport);
    println!("  Max concurrent      : {}", config.max_concurrent_requests);
    println!("  Auto-update         : {}", config.auto_update);
    for warning in &warnings {
        println!("  warning: {warning}");
    }
    Ok(ExitCode::SUCCESS)
}

async fn test_destination(config_path: &Path, url: &str) -> Result<ExitCode> {
    tls::install_default_provider();
    let config = Config::load(config_path)?;
    let tls = tls::TlsConfigs::build(config.ca_bundle.as_deref())?;
    let forwarder = Arc::new(Forwarder::new(
        config.policy()?,
        config.insecure_tls_policy()?,
        tls,
    ));
    let sid = "0".repeat(32);
    let open = Frame::new(
        json!({
            "type": "open",
            "sid": sid,
            "kind": "http",
            "method": "GET",
            "url": url,
            "headers": [["User-Agent", version::user_agent()]],
            "timeout_ms": 15_000,
            // No platform is reading: the whole answer may be passed at once.
            "window": u64::MAX / 2,
            "body_len": 0,
        }),
        vec![],
    );
    let (_down_tx, down_rx) = mpsc::unbounded_channel();
    let (out_tx, mut out_rx) = mpsc::channel(1024);
    let started = std::time::Instant::now();
    let serve = tokio::spawn(forwarder.serve(open, down_rx, out_tx));
    let mut status_code = None;
    let mut bytes = 0usize;
    while let Some(frame) = out_rx.recv().await {
        match frame.kind() {
            "response_head" => status_code = frame.u64_field("status"),
            "data" => bytes += frame.payload.len(),
            "end" => break,
            "error" => {
                println!(
                    "Refused or failed: {} - {}",
                    frame.str_field("code").unwrap_or("?"),
                    frame.str_field("message").unwrap_or("")
                );
                return Ok(ExitCode::FAILURE);
            }
            _ => {}
        }
    }
    let _ = serve.await;
    match status_code {
        Some(code) => {
            println!(
                "HTTP {code} from {url} in {} ms ({bytes} bytes), reached from this machine.",
                started.elapsed().as_millis()
            );
            Ok(ExitCode::SUCCESS)
        }
        None => bail!("no answer from {url}"),
    }
}
