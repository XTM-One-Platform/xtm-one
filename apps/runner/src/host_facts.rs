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

//! Host context the runner reports on connect and every heartbeat.
//!
//! The agent reads this (`list_runners`) to plan without probing: OS build,
//! which shells exist and their versions (Windows PowerShell 5.1 vs pwsh 7),
//! installed browsers, monitor topology, timezone and locale. Shape matches the
//! comment on `Runner.host_facts` in `backend/app/models/runner.py`.

use std::sync::OnceLock;
use std::time::{Duration, Instant};

// A command run with a short timeout, trimmed stdout on success (shared with
// the relay, which probes the same machine id).
use agent_core::platform::probe;
use serde::Serialize;
use tokio::sync::Mutex;

use crate::version::AGENT_VERSION;

/// How long a collected host-facts snapshot is reused before a fresh probe.
///
/// Host facts (OS build, installed shells/browsers, monitor topology) change
/// rarely within a session, yet collecting them spawns several short-lived
/// child processes on Windows. Re-probing on every heartbeat (default 30 s) is
/// needless churn on a fleet of machines, so heartbeats reuse the snapshot and
/// only refresh occasionally. Connect always collects fresh (empty cache).
const FACTS_TTL: Duration = Duration::from_secs(30 * 60);

#[derive(Debug, Serialize)]
pub struct HostFacts {
    pub os: String,
    pub os_version: String,
    pub os_build: String,
    pub arch: String,
    pub hostname: String,
    pub user: String,
    pub shells: serde_json::Map<String, serde_json::Value>,
    pub browsers: Vec<BrowserFact>,
    pub monitors: Vec<MonitorFact>,
    pub timezone: Option<String>,
    pub locale: Option<String>,
    /// Linux display-server session (`wayland` / `x11` / `tty`) from
    /// `XDG_SESSION_TYPE`, so the agent knows whether full-desktop capture goes
    /// through a Wayland portal. `None` on Windows/macOS and when unset.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub session_type: Option<String>,
    pub runner_version: String,
    /// Which build this is: `desktop` or `headless`. Reported so the platform
    /// can tell the two apart from the facts alone - an empty `monitors` list
    /// is ambiguous (a desktop runner reports that too when capture fails).
    pub runner_flavor: String,
}

#[derive(Debug, Serialize)]
pub struct BrowserFact {
    pub name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub version: Option<String>,
    pub path: String,
    /// Whether a user profile directory exists (best-effort "has a session").
    pub has_profile: bool,
}

#[derive(Debug, Serialize)]
pub struct MonitorFact {
    pub index: u32,
    pub width: u32,
    pub height: u32,
    pub primary: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
}

/// Collect the full host-facts snapshot. Best-effort: a probe that fails is
/// simply omitted rather than failing the connect.
pub async fn collect() -> HostFacts {
    let os = std::env::consts::OS.to_string();
    let arch = std::env::consts::ARCH.to_string();
    let hostname = hostname::get()
        .ok()
        .and_then(|h| h.into_string().ok())
        .unwrap_or_else(|| "unknown".to_string());
    let user = current_user();

    let os_version = sysinfo::System::os_version().unwrap_or_default();
    let long_os = sysinfo::System::long_os_version().unwrap_or_default();
    let os_build = sysinfo::System::kernel_version().unwrap_or_default();

    HostFacts {
        os,
        os_version: if long_os.is_empty() {
            os_version
        } else {
            long_os
        },
        os_build,
        arch,
        hostname,
        user,
        shells: detect_shells().await,
        browsers: detect_browsers().await,
        monitors: detect_monitors(),
        timezone: iana_time_zone::get_timezone().ok(),
        locale: sys_locale::get_locale(),
        session_type: session_type(),
        runner_version: AGENT_VERSION.to_string(),
        runner_flavor: crate::version::FLAVOR.to_string(),
    }
}

/// The active display-server session on Linux (`wayland`/`x11`/`tty`), used by
/// the agent to reason about capture. Only meaningful on Linux.
fn session_type() -> Option<String> {
    #[cfg(target_os = "linux")]
    {
        std::env::var("XDG_SESSION_TYPE")
            .ok()
            .filter(|s| !s.is_empty())
            .map(|s| s.to_ascii_lowercase())
    }
    #[cfg(not(target_os = "linux"))]
    {
        None
    }
}

/// The full-facts snapshot serialized as a JSON value for the wire.
pub async fn collect_json() -> serde_json::Value {
    serde_json::to_value(collect().await).unwrap_or(serde_json::Value::Null)
}

fn facts_cache() -> &'static Mutex<Option<(Instant, serde_json::Value)>> {
    static CACHE: OnceLock<Mutex<Option<(Instant, serde_json::Value)>>> = OnceLock::new();
    CACHE.get_or_init(|| Mutex::new(None))
}

/// Host facts as a JSON value, recomputed at most once per [`FACTS_TTL`].
///
/// The first call (connect) collects a fresh snapshot; subsequent calls
/// (heartbeats) reuse it until it ages out. This is what keeps a 30 s heartbeat
/// from spawning `powershell` several times a minute forever. The lock is
/// released during collection, so a rare concurrent double-collect is possible
/// and harmless (both writers store an equivalent snapshot).
pub async fn collect_json_cached() -> serde_json::Value {
    {
        let guard = facts_cache().lock().await;
        if let Some((collected_at, value)) = guard.as_ref() {
            if collected_at.elapsed() < FACTS_TTL {
                return value.clone();
            }
        }
    }
    let value = collect_json().await;
    *facts_cache().lock().await = Some((Instant::now(), value.clone()));
    value
}

/// Stand `value` in for this machine's facts: a test that drives a transport
/// must not probe the host it runs on (shells, browsers, monitors).
#[cfg(test)]
pub async fn seed_cache_for_tests(value: serde_json::Value) {
    *facts_cache().lock().await = Some((Instant::now(), value));
}

fn current_user() -> String {
    #[cfg(windows)]
    {
        std::env::var("USERNAME").unwrap_or_else(|_| "unknown".to_string())
    }
    #[cfg(not(windows))]
    {
        std::env::var("USER")
            .or_else(|_| std::env::var("LOGNAME"))
            .unwrap_or_else(|_| "unknown".to_string())
    }
}

/// Stable, privacy-preserving machine identity reported at enrollment: a
/// salted SHA-256 of the OS machine id. The server uses it to converge a
/// re-enrollment (reinstall, credential loss, install-script re-run) on the
/// machine's existing runner row instead of duplicating the fleet list.
///
/// The raw OS id never leaves the machine — hashing it with an app-specific
/// salt is the practice `machine-id(5)` recommends — and hostname is
/// deliberately not part of the identity (cloned VM fleets share one).
/// Best-effort: `None` simply enrolls without convergence. The salt is the
/// runner's own, so the identity an installed runner reports never changes.
pub async fn machine_id() -> Option<String> {
    agent_core::platform::machine_id(b"xtm-one-runner:").await
}

async fn detect_shells() -> serde_json::Map<String, serde_json::Value> {
    let mut shells = serde_json::Map::new();

    #[cfg(windows)]
    {
        // Windows PowerShell 5.1 (built in) and PowerShell 7+ (pwsh) are
        // distinct; the agent needs to know which is available.
        if let Some(v) = probe(
            "powershell",
            &[
                "-NoProfile",
                "-Command",
                "$PSVersionTable.PSVersion.ToString()",
            ],
        )
        .await
        {
            shells.insert("powershell".into(), v.into());
        }
        if let Some(v) = probe(
            "pwsh",
            &[
                "-NoProfile",
                "-Command",
                "$PSVersionTable.PSVersion.ToString()",
            ],
        )
        .await
        {
            shells.insert("pwsh".into(), v.into());
        }
        // cmd.exe is always present; version tracks the OS.
        shells.insert("cmd".into(), serde_json::Value::Bool(true));
    }

    #[cfg(not(windows))]
    {
        for (name, args) in [
            ("bash", ["--version"].as_slice()),
            ("zsh", ["--version"].as_slice()),
            ("sh", ["--version"].as_slice()),
            (
                "pwsh",
                [
                    "-NoProfile",
                    "-Command",
                    "$PSVersionTable.PSVersion.ToString()",
                ]
                .as_slice(),
            ),
        ] {
            if let Some(v) = probe(name, args).await {
                let first_line = v.lines().next().unwrap_or(&v).to_string();
                shells.insert(name.to_string(), first_line.into());
            }
        }
    }

    shells
}

async fn detect_browsers() -> Vec<BrowserFact> {
    let mut out = Vec::new();
    for candidate in browser_candidates() {
        if std::path::Path::new(&candidate.path).exists() {
            let version = candidate.version().await;
            out.push(BrowserFact {
                name: candidate.name.to_string(),
                version,
                path: candidate.path.clone(),
                has_profile: candidate
                    .profile
                    .as_ref()
                    .map(|p| std::path::Path::new(p).exists())
                    .unwrap_or(false),
            });
        }
    }
    out
}

struct BrowserCandidate {
    name: &'static str,
    path: String,
    profile: Option<String>,
    /// Registry `BLBeacon` version key on Windows (Chrome/Edge), if any.
    /// Only read on Windows; on other targets it is populated but unused.
    #[cfg_attr(not(windows), allow(dead_code))]
    win_beacon: Option<&'static str>,
    /// Whether the binary prints its version to stdout via `--version`.
    cli_version: bool,
}

impl BrowserCandidate {
    async fn version(&self) -> Option<String> {
        #[cfg(windows)]
        if let Some(beacon) = self.win_beacon {
            if let Some(v) = probe(
                "powershell",
                &[
                    "-NoProfile",
                    "-Command",
                    &format!("(Get-ItemProperty '{beacon}').version"),
                ],
            )
            .await
            {
                return Some(v);
            }
        }
        if self.cli_version {
            if let Some(v) = probe(&self.path, &["--version"]).await {
                return Some(v);
            }
        }
        #[cfg(windows)]
        {
            // Fall back to the executable's product version.
            if let Some(v) = probe(
                "powershell",
                &[
                    "-NoProfile",
                    "-Command",
                    &format!("(Get-Item '{}').VersionInfo.ProductVersion", self.path),
                ],
            )
            .await
            {
                return Some(v);
            }
        }
        None
    }
}

#[cfg(windows)]
fn browser_candidates() -> Vec<BrowserCandidate> {
    let local = std::env::var("LOCALAPPDATA").unwrap_or_default();
    let pf = std::env::var("ProgramFiles").unwrap_or_else(|_| "C:\\Program Files".into());
    let pf86 =
        std::env::var("ProgramFiles(x86)").unwrap_or_else(|_| "C:\\Program Files (x86)".into());
    vec![
        BrowserCandidate {
            name: "chrome",
            path: format!("{pf}\\Google\\Chrome\\Application\\chrome.exe"),
            profile: Some(format!("{local}\\Google\\Chrome\\User Data\\Default")),
            win_beacon: Some("HKCU:\\Software\\Google\\Chrome\\BLBeacon"),
            cli_version: false,
        },
        BrowserCandidate {
            name: "edge",
            path: format!("{pf86}\\Microsoft\\Edge\\Application\\msedge.exe"),
            profile: Some(format!("{local}\\Microsoft\\Edge\\User Data\\Default")),
            win_beacon: Some("HKCU:\\Software\\Microsoft\\Edge\\BLBeacon"),
            cli_version: false,
        },
        BrowserCandidate {
            name: "firefox",
            path: format!("{pf}\\Mozilla Firefox\\firefox.exe"),
            profile: std::env::var("APPDATA")
                .ok()
                .map(|a| format!("{a}\\Mozilla\\Firefox\\Profiles")),
            win_beacon: None,
            cli_version: true,
        },
    ]
}

#[cfg(target_os = "macos")]
fn browser_candidates() -> Vec<BrowserCandidate> {
    let home = std::env::var("HOME").unwrap_or_default();
    vec![
        BrowserCandidate {
            name: "chrome",
            path: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome".into(),
            profile: Some(format!(
                "{home}/Library/Application Support/Google/Chrome/Default"
            )),
            win_beacon: None,
            cli_version: true,
        },
        BrowserCandidate {
            name: "edge",
            path: "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge".into(),
            profile: Some(format!(
                "{home}/Library/Application Support/Microsoft Edge/Default"
            )),
            win_beacon: None,
            cli_version: true,
        },
        BrowserCandidate {
            name: "safari",
            path: "/Applications/Safari.app/Contents/MacOS/Safari".into(),
            profile: None,
            win_beacon: None,
            cli_version: false,
        },
        BrowserCandidate {
            name: "firefox",
            path: "/Applications/Firefox.app/Contents/MacOS/firefox".into(),
            profile: Some(format!(
                "{home}/Library/Application Support/Firefox/Profiles"
            )),
            win_beacon: None,
            cli_version: true,
        },
    ]
}

#[cfg(target_os = "linux")]
fn browser_candidates() -> Vec<BrowserCandidate> {
    let home = std::env::var("HOME").unwrap_or_default();
    vec![
        BrowserCandidate {
            name: "chrome",
            path: "/usr/bin/google-chrome".into(),
            profile: Some(format!("{home}/.config/google-chrome/Default")),
            win_beacon: None,
            cli_version: true,
        },
        BrowserCandidate {
            name: "chromium",
            path: "/usr/bin/chromium".into(),
            profile: Some(format!("{home}/.config/chromium/Default")),
            win_beacon: None,
            cli_version: true,
        },
        BrowserCandidate {
            name: "edge",
            path: "/usr/bin/microsoft-edge".into(),
            profile: Some(format!("{home}/.config/microsoft-edge/Default")),
            win_beacon: None,
            cli_version: true,
        },
        BrowserCandidate {
            name: "firefox",
            path: "/usr/bin/firefox".into(),
            profile: Some(format!("{home}/.mozilla/firefox")),
            win_beacon: None,
            cli_version: true,
        },
    ]
}

/// Headless flavor: no capture stack is linked, so there are no monitors to
/// report. An empty list is the honest answer and already the shape callers
/// handle (the desktop build reports it too when capture fails).
#[cfg(not(feature = "desktop"))]
fn detect_monitors() -> Vec<MonitorFact> {
    Vec::new()
}

#[cfg(feature = "desktop")]
fn detect_monitors() -> Vec<MonitorFact> {
    match xcap::Monitor::all() {
        Ok(monitors) => monitors
            .into_iter()
            .enumerate()
            .map(|(index, m)| MonitorFact {
                index: index as u32,
                width: m.width().unwrap_or(0),
                height: m.height().unwrap_or(0),
                primary: m.is_primary().unwrap_or(false),
                name: m.name().ok(),
            })
            .collect(),
        Err(_) => Vec::new(),
    }
}
