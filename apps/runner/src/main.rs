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

//! XTM One Desktop - the native XTM One client, and the runner inside it.
//!
//! Install with a native installer, enroll once from a small panel (server URL
//! and enrollment key), and the app opens your chat in its own window, lives
//! in the system tray and connects OUTBOUND to XTM One (WebSocket fast path,
//! HTTP long-poll fallback), PULLING commands - the platform never opens an
//! inbound port on the machine. A chat turn typed in the window can act on
//! this machine: agents drive the embedded runner through the `computer_use`
//! built-in tools to run shell commands (approved inline), read/write files,
//! capture genuine full-desktop screenshots and control a real authenticated
//! browser.
//!
//! One binary, two faces. With no subcommand (double-click / autostart /
//! Finder) it runs as the GUI app: chat window, tray icon, connection panel,
//! background daemon. With a subcommand it runs as the headless CLI - the
//! **headless runner** kept for servers, VMs and MDM installs where there is no
//! desktop session: `enroll` (exchange a `renr-` key for a durable credential),
//! `run` (connect and serve commands), `status` (show config + test
//! connectivity), and `reset` (remove the stored credential + config). The
//! headless flavor (`--no-default-features --features browser,self-update`)
//! compiles only this face: no webview, no tray, no X11 / Wayland stack.
//!
//! On Windows the binary is built for the "windows" subsystem so the GUI never
//! flashes a console; CLI mode re-attaches the parent console so `status` /
//! `enroll` still print where you launched them.
#![cfg_attr(all(not(debug_assertions), windows), windows_subsystem = "windows")]

mod api;
mod capabilities;
mod config;
mod credential;
mod daemon;
mod enroll;
mod executor;
#[cfg(feature = "gui")]
mod gui;
mod host_facts;
// Every flavor: `capabilities::files` shape-checks paths with it. Its
// page-facing half (`parse_local_path`, `resolve_existing`, `plan_open`) is
// GUI-only.
#[cfg_attr(not(feature = "gui"), allow(dead_code))]
mod local_paths;
mod proc;
mod protocol;
mod status;
mod transport;
mod update;
mod version;
#[cfg(feature = "gui")]
mod web_session;

use anyhow::{anyhow, Context, Result};
use clap::{Parser, Subcommand};
use tracing::{error, info};
use tracing_subscriber::EnvFilter;

use crate::config::RunnerConfig;
use crate::status::StatusReporter;
use crate::version::AGENT_VERSION;

#[derive(Parser)]
#[command(
    name = "xtm-one-runner",
    version = AGENT_VERSION,
    about = "XTM One Desktop - native XTM One client with the pull-based OS-control runner (headless runner on servers)",
    long_about = None
)]
struct Cli {
    #[command(subcommand)]
    command: Option<Cmd>,
}

#[derive(Subcommand)]
enum Cmd {
    /// Enroll this machine with an XTM One platform.
    Enroll {
        /// Platform base URL, e.g. https://one.filigran.io
        #[arg(long, env = "XTM_ONE_SERVER")]
        server: String,
        /// Enrollment key (renr-...) from the XTM One Desktop page (sidebar > XTM One Desktop).
        #[arg(long, env = "XTM_ONE_ENROLL_KEY")]
        key: String,
        /// Friendly machine name (defaults to the hostname).
        #[arg(long)]
        name: Option<String>,
        /// CDP endpoint for real-browser control.
        #[arg(long)]
        cdp_url: Option<String>,
    },
    /// Connect and serve commands.
    Run,
    /// Show config and test connectivity.
    Status,
    /// Remove the stored credential and config (unenroll locally).
    Reset,
    /// Check a file against its detached minisign signature, with the rules the
    /// self-updater applies to a release archive. Exit status: 0 verified, 1 the
    /// signature does not verify or was not made for what is expected, 3 it
    /// could not be checked. The install scripts use it to verify an upgrade.
    #[cfg(feature = "self-update")]
    VerifySignature {
        /// The file to check, e.g. a downloaded runner archive.
        file: std::path::PathBuf,
        /// Its signature: the release `.minisig` (the base64 form `tauri signer
        /// sign` writes is read too).
        #[arg(long)]
        signature: std::path::PathBuf,
        /// The minisign public key to check against: its base64 key line, or the
        /// base64 of a whole `.pub` file; several, comma-separated, are chosen
        /// between by the key id the signature names. Defaults to the release
        /// keys this build was compiled with, and their signature floor.
        #[arg(long)]
        pubkey: Option<String>,
        /// The archive name the signed trusted comment must name, when it names
        /// one (`file:`).
        #[arg(long)]
        expect_file: Option<String>,
        /// The runner version the signed trusted comment must name, when it
        /// names one (`version:`).
        #[arg(long)]
        expect_version: Option<String>,
    },
}

/// The subcommand names + help/version flags that route to the headless CLI.
/// Anything else (no args, `--autostarted`, a macOS `-psn_...` launch arg) opens
/// the GUI, so double-clicking the app never errors out on an unknown flag.
const CLI_TOKENS: &[&str] = &[
    "enroll",
    "run",
    "status",
    "reset",
    "verify-signature",
    "help",
    "-h",
    "--help",
    "-V",
    "--version",
];

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let is_cli = args
        .get(1)
        .map(|a| CLI_TOKENS.contains(&a.as_str()))
        .unwrap_or(false);

    if is_cli {
        #[cfg(windows)]
        attach_parent_console();
        init_tracing();
        let cli = Cli::parse();
        let Some(command) = cli.command else {
            // `--help` / `--version` already printed and exited inside parse().
            return;
        };
        let runtime = match tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
        {
            Ok(rt) => rt,
            Err(e) => {
                eprintln!("failed to start async runtime: {e}");
                std::process::exit(1);
            }
        };
        if let Err(e) = runtime.block_on(run_cli(command)) {
            error!("{e:#}");
            std::process::exit(1);
        }
        return;
    }

    // Default: launch the GUI app (tray + panel + background daemon).
    #[cfg(feature = "gui")]
    {
        let autostarted = args.iter().any(|a| a == "--autostarted");
        gui::run(gui::GuiLaunch { autostarted });
    }
    #[cfg(not(feature = "gui"))]
    {
        eprintln!(
            "This build has no GUI. Use a subcommand: xtm-one-runner enroll|run|status|reset"
        );
        std::process::exit(2);
    }
}

async fn run_cli(command: Cmd) -> Result<()> {
    match command {
        Cmd::Enroll {
            server,
            key,
            name,
            cdp_url,
        } => cmd_enroll(server, key, name, cdp_url).await,
        Cmd::Run => cmd_run().await,
        Cmd::Status => cmd_status().await,
        Cmd::Reset => cmd_reset().await,
        #[cfg(feature = "self-update")]
        Cmd::VerifySignature {
            file,
            signature,
            pubkey,
            expect_file,
            expect_version,
        } => std::process::exit(cmd_verify_signature(
            &file,
            &signature,
            pubkey.as_deref(),
            expect_file.as_deref(),
            expect_version.as_deref(),
        )),
    }
}

/// `verify-signature` exit statuses; 2 stays clap's usage error.
#[cfg(feature = "self-update")]
const VERIFY_OK: i32 = 0;
#[cfg(feature = "self-update")]
const VERIFY_REJECTED: i32 = 1;
#[cfg(feature = "self-update")]
const VERIFY_UNCHECKED: i32 = 3;

/// Reads nothing but its arguments - no config, no credential - so it answers
/// on a machine that is not enrolled, which is where an install script runs it.
#[cfg(feature = "self-update")]
fn cmd_verify_signature(
    file: &std::path::Path,
    signature: &std::path::Path,
    pubkey: Option<&str>,
    expect_file: Option<&str>,
    expect_version: Option<&str>,
) -> i32 {
    use crate::update::{Requirements, SignatureCheck};

    // The floor belongs to the release keys: a key given by hand has none.
    let (pubkey, timestamp_floor) = match (pubkey, crate::version::MINISIGN_PUBKEY) {
        (Some(pubkey), _) => (pubkey, 0),
        (None, Some(release)) => match crate::update::compiled_signature_floor() {
            Ok(floor) => (release, floor),
            Err(why) => {
                eprintln!("cannot check the signature: {why}");
                return VERIFY_UNCHECKED;
            }
        },
        (None, None) => {
            eprintln!(
                "cannot check the signature: no --pubkey given, and this build has no release key compiled in"
            );
            return VERIFY_UNCHECKED;
        }
    };
    let requirements = Requirements {
        file: expect_file,
        version: expect_version,
        timestamp_floor,
    };
    let signature_text = match std::fs::read_to_string(signature) {
        Ok(text) => text,
        Err(e) => {
            eprintln!(
                "cannot check the signature: reading {}: {e}",
                signature.display()
            );
            return VERIFY_UNCHECKED;
        }
    };
    let bytes = match std::fs::read(file) {
        Ok(bytes) => bytes,
        Err(e) => {
            eprintln!(
                "cannot check the signature: reading {}: {e}",
                file.display()
            );
            return VERIFY_UNCHECKED;
        }
    };
    match crate::update::check_signature(&bytes, &signature_text, pubkey, &requirements) {
        SignatureCheck::Verified { trusted_comment } => {
            println!("signature verified (trusted comment: {trusted_comment})");
            VERIFY_OK
        }
        SignatureCheck::Rejected(why) => {
            eprintln!("the signature is refused: {why}");
            VERIFY_REJECTED
        }
        SignatureCheck::Unreadable(why) => {
            eprintln!("cannot check the signature: {why}");
            VERIFY_UNCHECKED
        }
    }
}

pub(crate) fn init_tracing() {
    let filter = EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| EnvFilter::new("info,xtm_one_runner=info"));
    tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_target(false)
        .init();
}

async fn cmd_enroll(
    server: String,
    key: String,
    name: Option<String>,
    cdp_url: Option<String>,
) -> Result<()> {
    info!("enrolling with {server} ...");
    let config = enroll::run(enroll::EnrollOptions {
        server_url: server,
        enrollment_key: key,
        name,
        cdp_url,
    })
    .await?;
    info!(
        "enrolled: runner id {} ({}). Credential stored in the OS keychain.",
        config.runner_id, config.platform
    );
    info!("start serving with: xtm-one-runner run");
    Ok(())
}

async fn cmd_run() -> Result<()> {
    let config = load_config()?;
    let token = credential::load(&config.runner_id)?
        .ok_or_else(|| anyhow!("no stored credential; run `xtm-one-runner enroll` first"))?;

    info!(
        "XTM One headless runner {} starting (runner {}, {})",
        AGENT_VERSION, config.runner_id, config.platform
    );
    info!("server: {}", config.base_url());

    let (shutdown_tx, _shutdown_rx) = tokio::sync::watch::channel(false);

    // Ctrl-C / SIGTERM -> graceful shutdown.
    let sd = shutdown_tx.clone();
    tokio::spawn(async move {
        wait_for_shutdown_signal().await;
        info!("shutdown signal received");
        let _ = sd.send(true);
    });

    // Headless installs keep the bare-binary self-update path.
    let restart = daemon::serve(config, token, shutdown_tx, StatusReporter::noop(), true).await?;
    if restart {
        return reexec();
    }
    Ok(())
}

/// Re-exec the (freshly swapped) executable with the same arguments, then let
/// this process exit. Used after a self-update so the new binary takes over
/// without needing an external supervisor.
fn reexec() -> Result<()> {
    let exe = std::env::current_exe().context("resolving the current executable for restart")?;
    let args: Vec<String> = std::env::args().skip(1).collect();
    info!("restarting into the updated runner");
    std::process::Command::new(&exe)
        .args(&args)
        .spawn()
        .context("spawning the updated runner")?;
    Ok(())
}

async fn cmd_status() -> Result<()> {
    let config = load_config()?;
    println!(
        "XTM One Desktop / runner {AGENT_VERSION} ({})",
        crate::version::FLAVOR
    );
    println!("  server:     {}", config.base_url());
    println!("  runner id:  {}", config.runner_id);
    println!("  platform:   {}", config.platform);
    println!("  cdp url:    {}", config.cdp_url);
    println!(
        "  config dir: {}",
        RunnerConfig::config_dir()
            .map(|p| p.display().to_string())
            .unwrap_or_else(|_| "<unknown>".into())
    );

    println!("  auto-update: {}", config.auto_update);

    match credential::load(&config.runner_id)? {
        Some(token) => {
            println!("  credential: present (OS keychain)");
            let api = api::ApiClient::new(&config, token)?;
            match update::check(&api, enroll::detect_platform(), enroll::detect_arch()).await {
                Some(info) => println!(
                    "  server:     reachable; runner update available -> {}",
                    info.version
                ),
                None => println!("  server:     reachable; runner is up to date"),
            }
        }
        None => println!("  credential: MISSING - run `xtm-one-runner enroll`"),
    }
    Ok(())
}

async fn cmd_reset() -> Result<()> {
    match RunnerConfig::load()? {
        Some(config) => {
            credential::delete(&config.runner_id)?;
            RunnerConfig::delete()?;
            info!(
                "removed credential and config for runner {}",
                config.runner_id
            );
        }
        None => info!("nothing to reset (not enrolled)"),
    }
    Ok(())
}

fn load_config() -> Result<RunnerConfig> {
    RunnerConfig::load()
        .context("loading runner config")?
        .ok_or_else(|| {
            anyhow!("not enrolled; run `xtm-one-runner enroll --server <url> --key <renr-...>`")
        })
}

/// Re-attach the parent process's console on Windows so CLI subcommands print to
/// the terminal that launched them (the binary is otherwise a windowless GUI
/// app). A no-op failure just means there was no parent console (e.g. launched
/// from Explorer), which is fine.
#[cfg(windows)]
fn attach_parent_console() {
    use windows_sys::Win32::System::Console::{AttachConsole, ATTACH_PARENT_PROCESS};
    unsafe {
        AttachConsole(ATTACH_PARENT_PROCESS);
    }
}

#[cfg(unix)]
async fn wait_for_shutdown_signal() {
    use tokio::signal::unix::{signal, SignalKind};
    let mut term = match signal(SignalKind::terminate()) {
        Ok(s) => s,
        Err(_) => {
            let _ = tokio::signal::ctrl_c().await;
            return;
        }
    };
    tokio::select! {
        _ = tokio::signal::ctrl_c() => {}
        _ = term.recv() => {}
    }
}

#[cfg(not(unix))]
async fn wait_for_shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
}

#[cfg(test)]
mod tests {
    use super::{Cli, CLI_TOKENS};
    use clap::CommandFactory;

    #[test]
    fn every_subcommand_routes_to_the_cli() {
        // A subcommand missing from CLI_TOKENS would open the GUI instead of
        // running - for `verify-signature`, from inside an install script.
        for sub in Cli::command().get_subcommands() {
            assert!(
                CLI_TOKENS.contains(&sub.get_name()),
                "`{}` is not in CLI_TOKENS",
                sub.get_name()
            );
        }
    }

    #[cfg(feature = "self-update")]
    #[test]
    fn verify_signature_answers_with_its_exit_status() {
        use super::{cmd_verify_signature, VERIFY_OK, VERIFY_REJECTED, VERIFY_UNCHECKED};
        use crate::update::tests::{
            FIXTURE_PUBKEY_BARE, PAYLOAD, SECOND_KEY, SIG_RAW, SIG_VERSIONED,
        };

        let dir = std::env::temp_dir().join(format!(
            "xtm-one-runner-verify-signature-test-{}",
            std::process::id()
        ));
        std::fs::create_dir_all(&dir).expect("temp dir");
        let file = dir.join("fixture.tar.gz");
        let tampered = dir.join("tampered.tar.gz");
        let signature = dir.join("fixture.tar.gz.minisig");
        let garbage = dir.join("garbage.minisig");
        let versioned = dir.join("versioned.minisig");
        std::fs::write(&file, PAYLOAD).expect("payload");
        let mut other = PAYLOAD.to_vec();
        other[0] ^= 0x01;
        std::fs::write(&tampered, other).expect("tampered payload");
        std::fs::write(&signature, SIG_RAW).expect("signature");
        std::fs::write(&versioned, SIG_VERSIONED).expect("versioned signature");
        std::fs::write(&garbage, "<!DOCTYPE html>").expect("garbage");

        let key = Some(FIXTURE_PUBKEY_BARE);
        let check = |file: &std::path::Path, sig: &std::path::Path, key: Option<&str>| {
            cmd_verify_signature(file, sig, key, None, None)
        };
        assert_eq!(check(&file, &signature, key), VERIFY_OK);
        assert_eq!(check(&tampered, &signature, key), VERIFY_REJECTED);
        assert_eq!(check(&file, &garbage, key), VERIFY_UNCHECKED);
        assert_eq!(
            check(&file, &dir.join("missing.minisig"), key),
            VERIFY_UNCHECKED
        );
        if crate::version::MINISIGN_PUBKEY.is_none() {
            assert_eq!(check(&file, &signature, None), VERIFY_UNCHECKED);
        }

        // The archive and the version the signature must name.
        let keys = format!("{FIXTURE_PUBKEY_BARE},{SECOND_KEY}");
        let keys = Some(keys.as_str());
        let expect = |sig: &std::path::Path, name: &str, version: Option<&str>| {
            cmd_verify_signature(&file, sig, keys, Some(name), version)
        };
        assert_eq!(expect(&signature, "fixture.tar.gz", None), VERIFY_OK);
        assert_eq!(
            expect(&signature, "xtm-one-runner-linux-x64.tar.gz", None),
            VERIFY_REJECTED
        );
        assert_eq!(
            expect(&versioned, "fixture.tar.gz", Some("1.260928.57")),
            VERIFY_OK
        );
        assert_eq!(
            expect(&versioned, "fixture.tar.gz", Some("1.261001.3")),
            VERIFY_REJECTED
        );
        // Signed before versions were: accepted whatever version is expected.
        assert_eq!(
            expect(&signature, "fixture.tar.gz", Some("1.261001.3")),
            VERIFY_OK
        );
        let _ = std::fs::remove_dir_all(&dir);
    }
}
