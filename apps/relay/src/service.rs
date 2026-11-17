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

//! Running the relay as an OS service: a systemd unit (Linux), a launchd
//! daemon (macOS) or a Windows service, restarted on failure and after a
//! self-update.

use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};

pub const SERVICE_NAME: &str = "xtm-one-relay";
#[cfg_attr(not(windows), allow(dead_code))]
pub const DISPLAY_NAME: &str = "XTM One Relay";
#[cfg_attr(not(windows), allow(dead_code))]
const DESCRIPTION: &str =
    "Pull-based egress proxy: carries the requests XTM One routes through this relay to the destinations it allows.";

/// What `service install` sets up.
pub struct InstallOptions {
    pub config: PathBuf,
    /// The account the service runs as (Linux / macOS); root otherwise.
    pub user: Option<String>,
}

fn current_exe() -> Result<PathBuf> {
    let exe = std::env::current_exe().context("locating the relay binary")?;
    Ok(std::fs::canonicalize(&exe).unwrap_or(exe))
}

fn absolute(path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .map(|d| d.join(path))
            .unwrap_or_else(|_| path.to_path_buf())
    }
}

#[cfg(unix)]
fn require_root() -> Result<()> {
    // SAFETY: geteuid has no preconditions and cannot fail.
    if unsafe { libc::geteuid() } != 0 {
        bail!("installing a system service needs root: run it with sudo");
    }
    Ok(())
}

/// `--user` ends up in the service definition and in `chown`'s arguments: an
/// account name only, so it can neither add a directive nor read as an option.
#[cfg(unix)]
fn check_account(user: &str) -> Result<()> {
    let mut chars = user.chars();
    let valid = user.len() <= 32
        && chars
            .next()
            .is_some_and(|c| c.is_ascii_alphabetic() || c == '_')
        && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.'));
    if !valid {
        bail!("--user '{user}' is not an account name (letters, digits, '_', '-', '.')");
    }
    Ok(())
}

/// A path systemd reads between double quotes, where a quote, a backslash or
/// a control character would end it, and `%` / `$` would be expanded.
#[cfg(all(unix, not(target_os = "macos")))]
fn check_unit_path(path: &Path) -> Result<()> {
    let text = path.to_string_lossy();
    if text
        .chars()
        .any(|c| matches!(c, '"' | '\\' | '%' | '$') || c.is_control())
    {
        bail!(
            "{} cannot go into a systemd unit: move it to a path without quotes, backslashes, '%' or '$'",
            path.display()
        );
    }
    Ok(())
}

#[cfg(unix)]
fn run(program: &str, args: &[&str]) -> Result<()> {
    let status = std::process::Command::new(program)
        .args(args)
        .status()
        .with_context(|| format!("running {program}"))?;
    if !status.success() {
        bail!("{program} {} failed ({status})", args.join(" "));
    }
    Ok(())
}

// -- Linux: systemd ------------------------------------------------------------

#[cfg(all(unix, not(target_os = "macos")))]
const UNIT_PATH: &str = "/etc/systemd/system/xtm-one-relay.service";

/// The systemd unit: restarted on any exit (a self-update exits to be
/// restarted), the system read-only but for the relay's own directories.
#[cfg(all(unix, not(target_os = "macos")))]
fn systemd_unit(exe: &Path, config: &Path, user: Option<&str>) -> String {
    let state_dir = config.parent().unwrap_or(Path::new("/etc/xtm-one-relay"));
    let exe_dir = exe.parent().unwrap_or(Path::new("/usr/local/bin"));
    let account = match user {
        Some(user) => format!("User={user}\nGroup={user}\n"),
        None => String::new(),
    };
    format!(
        "[Unit]\n\
         Description=XTM One Relay (pull-based egress proxy for XTM One)\n\
         Wants=network-online.target\n\
         After=network-online.target\n\
         \n\
         [Service]\n\
         Type=simple\n\
         ExecStart=\"{exe}\" run --config \"{config}\"\n\
         ExecReload=/bin/kill -HUP $MAINPID\n\
         Restart=always\n\
         RestartSec=5\n\
         {account}\
         NoNewPrivileges=yes\n\
         PrivateTmp=yes\n\
         ProtectSystem=strict\n\
         ReadWritePaths=\"{state}\" \"{bin}\"\n\
         ProtectHome=yes\n\
         ProtectKernelTunables=yes\n\
         ProtectKernelModules=yes\n\
         ProtectControlGroups=yes\n\
         RestrictSUIDSGID=yes\n\
         LockPersonality=yes\n\
         \n\
         [Install]\n\
         WantedBy=multi-user.target\n",
        exe = exe.display(),
        config = config.display(),
        state = state_dir.display(),
        bin = exe_dir.display(),
    )
}

#[cfg(all(unix, not(target_os = "macos")))]
pub fn install(options: &InstallOptions) -> Result<()> {
    require_root()?;
    if let Some(user) = &options.user {
        check_account(user)?;
    }
    let exe = current_exe()?;
    let config = absolute(&options.config);
    check_unit_path(&exe)?;
    check_unit_path(&config)?;
    let dir = config
        .parent()
        .context("the configuration has no directory")?;
    crate::config::prepare_dir(dir)?;
    if let Some(user) = &options.user {
        run(
            "chown",
            &["-R", &format!("{user}:"), &dir.display().to_string()],
        )?;
    }
    std::fs::write(
        UNIT_PATH,
        systemd_unit(&exe, &config, options.user.as_deref()),
    )
    .with_context(|| format!("writing {UNIT_PATH}"))?;
    run("systemctl", &["daemon-reload"])?;
    run("systemctl", &["enable", "--now", SERVICE_NAME])?;
    Ok(())
}

#[cfg(all(unix, not(target_os = "macos")))]
pub fn uninstall() -> Result<()> {
    require_root()?;
    let _ = run("systemctl", &["disable", "--now", SERVICE_NAME]);
    match std::fs::remove_file(UNIT_PATH) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(anyhow::Error::new(e).context(format!("removing {UNIT_PATH}"))),
    }
    run("systemctl", &["daemon-reload"])
}

#[cfg(all(unix, not(target_os = "macos")))]
pub fn start() -> Result<()> {
    run("systemctl", &["start", SERVICE_NAME])
}

#[cfg(all(unix, not(target_os = "macos")))]
pub fn stop() -> Result<()> {
    run("systemctl", &["stop", SERVICE_NAME])
}

// -- macOS: launchd ------------------------------------------------------------

#[cfg(target_os = "macos")]
const LAUNCHD_LABEL: &str = "io.filigran.xtm-one-relay";
#[cfg(target_os = "macos")]
const PLIST_PATH: &str = "/Library/LaunchDaemons/io.filigran.xtm-one-relay.plist";

#[cfg(target_os = "macos")]
fn xml_escape(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

#[cfg(target_os = "macos")]
fn launchd_plist(exe: &Path, config: &Path, user: Option<&str>) -> String {
    let user = match user {
        Some(user) => format!(
            "    <key>UserName</key>\n    <string>{}</string>\n",
            xml_escape(user)
        ),
        None => String::new(),
    };
    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{exe}</string>
        <string>run</string>
        <string>--config</string>
        <string>{config}</string>
    </array>
{user}    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Library/Logs/xtm-one-relay.log</string>
    <key>StandardErrorPath</key>
    <string>/Library/Logs/xtm-one-relay.log</string>
</dict>
</plist>
"#,
        label = LAUNCHD_LABEL,
        exe = xml_escape(&exe.display().to_string()),
        config = xml_escape(&config.display().to_string()),
    )
}

#[cfg(target_os = "macos")]
pub fn install(options: &InstallOptions) -> Result<()> {
    require_root()?;
    if let Some(user) = &options.user {
        check_account(user)?;
    }
    let exe = current_exe()?;
    let config = absolute(&options.config);
    let dir = config
        .parent()
        .context("the configuration has no directory")?;
    crate::config::prepare_dir(dir)?;
    if let Some(user) = &options.user {
        run("chown", &["-R", user, &dir.display().to_string()])?;
    }
    std::fs::write(
        PLIST_PATH,
        launchd_plist(&exe, &config, options.user.as_deref()),
    )
    .with_context(|| format!("writing {PLIST_PATH}"))?;
    let _ = run(
        "launchctl",
        &["bootout", &format!("system/{LAUNCHD_LABEL}")],
    );
    run("launchctl", &["bootstrap", "system", PLIST_PATH])
}

#[cfg(target_os = "macos")]
pub fn uninstall() -> Result<()> {
    require_root()?;
    let _ = run(
        "launchctl",
        &["bootout", &format!("system/{LAUNCHD_LABEL}")],
    );
    match std::fs::remove_file(PLIST_PATH) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(anyhow::Error::new(e).context(format!("removing {PLIST_PATH}"))),
    }
}

#[cfg(target_os = "macos")]
pub fn start() -> Result<()> {
    run("launchctl", &["bootstrap", "system", PLIST_PATH])
}

#[cfg(target_os = "macos")]
pub fn stop() -> Result<()> {
    run(
        "launchctl",
        &["bootout", &format!("system/{LAUNCHD_LABEL}")],
    )
}

// -- Windows: the Service Control Manager -----------------------------------

#[cfg(windows)]
pub fn install(options: &InstallOptions) -> Result<()> {
    use std::ffi::{OsStr, OsString};
    use std::time::Duration;
    use windows_service::service::{
        ServiceAccess, ServiceAction, ServiceActionType, ServiceErrorControl,
        ServiceFailureActions, ServiceFailureResetPeriod, ServiceInfo, ServiceStartType,
        ServiceType,
    };
    use windows_service::service_manager::{ServiceManager, ServiceManagerAccess};

    if options.user.is_some() {
        bail!("--user is not supported on Windows: the service runs as LocalSystem");
    }
    let exe = current_exe()?;
    let config = absolute(&options.config);
    let dir = config
        .parent()
        .context("the configuration has no directory")?;
    crate::config::prepare_dir(dir)?;
    let manager = ServiceManager::local_computer(
        None::<&str>,
        ServiceManagerAccess::CONNECT | ServiceManagerAccess::CREATE_SERVICE,
    )
    .context("opening the Service Control Manager (run from an elevated prompt)")?;
    let info = ServiceInfo {
        name: OsString::from(SERVICE_NAME),
        display_name: OsString::from(DISPLAY_NAME),
        service_type: ServiceType::OWN_PROCESS,
        start_type: ServiceStartType::AutoStart,
        error_control: ServiceErrorControl::Normal,
        executable_path: exe,
        launch_arguments: vec![
            OsString::from("service"),
            OsString::from("run"),
            OsString::from("--config"),
            config.into_os_string(),
        ],
        dependencies: vec![],
        account_name: None,
        account_password: None,
    };
    let service = manager
        .create_service(&info, ServiceAccess::CHANGE_CONFIG | ServiceAccess::START)
        .context("creating the xtm-one-relay service")?;
    service.set_description(DESCRIPTION)?;
    let restart = |seconds| ServiceAction {
        action_type: ServiceActionType::Restart,
        delay: Duration::from_secs(seconds),
    };
    service.update_failure_actions(ServiceFailureActions {
        reset_period: ServiceFailureResetPeriod::After(Duration::from_secs(86400)),
        reboot_msg: None,
        command: None,
        actions: Some(vec![restart(5), restart(5), restart(30)]),
    })?;
    // A self-update stops the service with an error code to be restarted.
    service.set_failure_actions_on_non_crash_failures(true)?;
    service
        .start(&[] as &[&OsStr])
        .context("starting the service")?;
    Ok(())
}

#[cfg(windows)]
fn open_service(
    access: windows_service::service::ServiceAccess,
) -> Result<windows_service::service::Service> {
    use windows_service::service_manager::{ServiceManager, ServiceManagerAccess};
    let manager = ServiceManager::local_computer(None::<&str>, ServiceManagerAccess::CONNECT)
        .context("opening the Service Control Manager (run from an elevated prompt)")?;
    manager
        .open_service(SERVICE_NAME, access)
        .context("opening the xtm-one-relay service (is it installed?)")
}

#[cfg(windows)]
pub fn uninstall() -> Result<()> {
    use windows_service::service::{ServiceAccess, ServiceState};
    let service =
        open_service(ServiceAccess::QUERY_STATUS | ServiceAccess::STOP | ServiceAccess::DELETE)?;
    if service.query_status()?.current_state != ServiceState::Stopped {
        let _ = service.stop();
    }
    service.delete().context("deleting the service")?;
    Ok(())
}

#[cfg(windows)]
pub fn start() -> Result<()> {
    use std::ffi::OsStr;
    use windows_service::service::ServiceAccess;
    open_service(ServiceAccess::START)?
        .start(&[] as &[&OsStr])
        .context("starting the service")
}

#[cfg(windows)]
pub fn stop() -> Result<()> {
    use windows_service::service::ServiceAccess;
    open_service(ServiceAccess::STOP)?
        .stop()
        .context("stopping the service")?;
    Ok(())
}

/// The Windows service entry point: `xtm-one-relay service run`, started by
/// the Service Control Manager.
#[cfg(windows)]
pub mod windows {
    use std::ffi::OsString;
    use std::path::PathBuf;
    use std::sync::OnceLock;
    use std::time::Duration;

    use anyhow::Result;
    use tokio::sync::watch;
    use windows_service::service::{
        ServiceControl, ServiceControlAccept, ServiceExitCode, ServiceState, ServiceStatus,
        ServiceType,
    };
    use windows_service::service_control_handler::{self, ServiceControlHandlerResult};

    use super::SERVICE_NAME;
    use crate::tunnel::Outcome;

    static CONFIG: OnceLock<PathBuf> = OnceLock::new();

    windows_service::define_windows_service!(ffi_service_main, service_main);

    /// Hand the process to the Service Control Manager (blocks until the
    /// service stops).
    pub fn run_dispatcher(config: PathBuf) -> Result<()> {
        let _ = CONFIG.set(config);
        windows_service::service_dispatcher::start(SERVICE_NAME, ffi_service_main)?;
        Ok(())
    }

    fn status(state: ServiceState, exit: ServiceExitCode) -> ServiceStatus {
        ServiceStatus {
            service_type: ServiceType::OWN_PROCESS,
            current_state: state,
            controls_accepted: if state == ServiceState::Running {
                ServiceControlAccept::STOP | ServiceControlAccept::SHUTDOWN
            } else {
                ServiceControlAccept::empty()
            },
            exit_code: exit,
            checkpoint: 0,
            wait_hint: Duration::from_secs(10),
            process_id: None,
        }
    }

    fn service_main(_arguments: Vec<OsString>) {
        let config = CONFIG
            .get()
            .cloned()
            .unwrap_or_else(crate::config::default_config_path);
        let (shutdown_tx, shutdown_rx) = watch::channel(false);
        let handler = move |event| match event {
            ServiceControl::Stop | ServiceControl::Shutdown => {
                let _ = shutdown_tx.send(true);
                ServiceControlHandlerResult::NoError
            }
            ServiceControl::Interrogate => ServiceControlHandlerResult::NoError,
            _ => ServiceControlHandlerResult::NotImplemented,
        };
        let Ok(handle) = service_control_handler::register(SERVICE_NAME, handler) else {
            return;
        };
        let _ = handle.set_service_status(status(ServiceState::Running, ServiceExitCode::Win32(0)));
        let exit = match tokio::runtime::Runtime::new() {
            Ok(runtime) => match runtime.block_on(crate::daemon::run(config, shutdown_rx)) {
                Ok(Outcome::Shutdown) => ServiceExitCode::Win32(0),
                // Stopped with an error code so the failure actions restart
                // the service on the new binary.
                Ok(Outcome::Updated) => ServiceExitCode::ServiceSpecific(75),
                Err(e) => {
                    tracing::error!("{e:#}");
                    ServiceExitCode::ServiceSpecific(1)
                }
            },
            Err(_) => ServiceExitCode::ServiceSpecific(1),
        };
        let _ = handle.set_service_status(status(ServiceState::Stopped, exit));
    }
}

#[cfg(all(test, unix, not(target_os = "macos")))]
mod tests {
    use super::*;

    #[test]
    fn the_unit_keeps_the_system_read_only_but_for_the_relay_s_own_directories() {
        let unit = systemd_unit(
            Path::new("/opt/xtm-one-relay/xtm-one-relay"),
            Path::new("/etc/xtm-one-relay/relay.toml"),
            Some("xtm-one-relay"),
        );
        assert!(unit.contains("ExecStart=\"/opt/xtm-one-relay/xtm-one-relay\" run --config \"/etc/xtm-one-relay/relay.toml\""));
        assert!(unit.contains("ProtectSystem=strict"));
        assert!(unit.contains("ReadWritePaths=\"/etc/xtm-one-relay\" \"/opt/xtm-one-relay\""));
        assert!(unit.contains("User=xtm-one-relay"));
        assert!(unit.contains("Restart=always"));
    }

    #[test]
    fn only_an_account_name_reaches_the_unit_and_chown() {
        for user in ["xtm-one-relay", "_relay", "svc.relay", "Relay2"] {
            assert!(check_account(user).is_ok(), "{user}");
        }
        for user in [
            "",
            "-R",
            "--reference=/etc",
            "relay\nExecStartPre=/bin/sh",
            "relay user",
            "1relay",
            &"a".repeat(33),
        ] {
            assert!(check_account(user).is_err(), "{user:?}");
        }
    }

    #[test]
    fn a_path_that_would_break_out_of_its_quotes_is_refused() {
        assert!(check_unit_path(Path::new("/opt/xtm-one-relay/xtm-one-relay")).is_ok());
        assert!(check_unit_path(Path::new("/etc/xtm one relay/relay.toml")).is_ok());
        for path in [
            "/etc/x\"y/relay.toml",
            "/etc/x\ny/relay.toml",
            "/etc/x\\y/relay.toml",
            "/etc/%h/relay.toml",
            "/etc/$HOME/relay.toml",
        ] {
            assert!(check_unit_path(Path::new(path)).is_err(), "{path:?}");
        }
    }
}
