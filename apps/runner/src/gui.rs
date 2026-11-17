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

//! XTM One Desktop - the native app (feature = "gui").
//!
//! One tray-resident app with two windows and a daemon:
//!
//!   * the **chat window** (`chat`): a full-size, resizable webview that loads
//!     the enrolled instance's web app - chat, agents, history, everything the
//!     browser shows - the way Claude Desktop or Slack host their web UI. The
//!     shell adds what a tab cannot: it stays in the tray, comes back on a
//!     global shortcut, opens external links in the OS browser, saves downloads
//!     to the Downloads folder, and tells the page WHICH enrolled machine it is
//!     running on (the bridge below), so every chat turn typed here can act on
//!     this machine through the runner the app embeds;
//!   * the **connection panel** (`main`): the small static page for
//!     onboarding (instance URL + enrollment key), overview, settings and
//!     about - the runner's face, unchanged in role;
//!   * the **runner daemon**: the same `daemon::serve` loop the headless CLI
//!     runs, pulling commands over the outbound WebSocket / long-poll.
//!
//! The bridge. Before any script of the page runs, the shell injects
//! `window.__XTM_ONE_DESKTOP__` = `{version, platform, arch, runner_id,
//! runner_name, server_url}` (read-only, only on the instance's own origin -
//! never on an identity provider's page reached during sign-in). That object is
//! the whole contract: the web app reads `runner_id` and sends it as
//! `desktop_runner_id` on each turn; the backend pins that machine as the
//! default target of the `computer_use` tools and offers held shell commands
//! inline. In the other direction the page gets a deliberately tiny IPC
//! surface, granted at RUNTIME to the enrolled origin and to the chat window
//! only (`instance_bridge_capability`): the status events, the bridge facts,
//! "open the connection panel", and the opener plugin (so `target=_blank` links
//! reach the OS browser). Enrolling, unenrolling and quitting stay reachable
//! from the local panel alone - web content can never do them.
//!
//! The local side of the chat. The page renders the paths an agent
//! touched on this machine with `Open` / `Show in folder`, through two
//! commands of the shell's own (`desktop_open_local_path`,
//! `desktop_reveal_local_path`) - never the opener plugin's unscoped path
//! commands - validated by `local_paths.rs` (local, existing, canonical, and
//! passive content only for `Open`) behind a focus + rate gate. A turn that
//! ends while the window is elsewhere asks for the person's attention
//! (`desktop_request_attention`). The bridge facts say which of these the
//! shell supports (`features`, `open_extensions`), so a web app newer than the
//! installed shell never offers what it cannot do.
//!
//! The web session. The page hands every change of its session to the
//! shell (`desktop_store_session`), which keeps it in the OS keychain, and
//! takes it back at launch (`desktop_load_session`): the webview's own storage
//! can roll back, and a session lost with it would sign the person out at
//! every restart. See `web_session.rs`.
//!
//! The menus. The tray icon's menu and the application menu (a menu
//! bar on the chat window on Windows and Linux, the app-wide menu bar on
//! macOS) offer the same actions (`MenuAction`), served by one listener.
//!
//! Lifecycle:
//!   * On launch, if enrolled -> spawn the daemon and (unless started at login)
//!     open the chat window; if not -> show the panel's onboarding.
//!   * Closing either window hides it to the tray; the daemon keeps running.
//!   * Quit, from the tray or the application menu, stops the daemon and exits;
//!     during an update install it waits for the install, a minute at most
//!     (`request_quit`).
//!   * An update restarts the app into the new build with the windows that
//!     were open, the chat on the page it showed (`apply_update`).
//!   * Enrollment / unenrollment / settings all flow through IPC commands that
//!     reuse the same `daemon::serve` the CLI uses.

use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::ipc::CapabilityBuilder;
use tauri::menu::{
    Menu, MenuItem, PredefinedMenuItem, Submenu, HELP_SUBMENU_ID, WINDOW_SUBMENU_ID,
};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::webview::{DownloadEvent, NewWindowResponse};
use tauri::{
    AppHandle, Emitter, Manager, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder, Wry,
};
use tauri_plugin_autostart::ManagerExt;
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};
use tauri_plugin_opener::OpenerExt;
use tauri_plugin_updater::UpdaterExt;
use tokio::sync::watch;

use crate::config::RunnerConfig;
use crate::credential;
use crate::local_paths::{self, LocalPathError, ResolvedPath};
use crate::status::{RunnerStatus, StatusReporter};
use crate::version::AGENT_VERSION;
use crate::web_session::{self, WebSession};

/// The product, as every human-facing surface spells it. The crate, the
/// binary, the bundle identifier and the dist artifacts keep `xtm-one-runner`:
/// that is the identity the installed fleet self-updates under.
pub const PRODUCT_NAME: &str = "XTM One Desktop";

/// The name the login autostart entry is registered under - the registry
/// value on Windows, `~/Library/LaunchAgents/<name>.plist` on macOS,
/// `~/.config/autostart/<name>.desktop` on Linux. Kept on the TECHNICAL
/// identity like the crate, the bundle id and the keychain entry, and not on
/// the product name: the entry a pre-rename install wrote stays the one this
/// build reads and rewrites, so `is_enabled()` is truthful across the rename
/// (a person who turned autostart off stays off), and login never starts the
/// app twice from an old-named and a new-named entry.
const AUTOSTART_ENTRY_NAME: &str = "XTM One Runner";

/// The command-line flag the login autostart entry passes, so a launch at
/// login stays quietly in the tray instead of opening the chat window.
const AUTOSTARTED_FLAG: &str = "--autostarted";

/// The connection panel window (declared in `tauri.conf.json`).
const PANEL_WINDOW: &str = "main";
/// The chat window, created at runtime because its URL is the enrolled
/// instance's - unknown until enrollment.
const CHAT_WINDOW: &str = "chat";
/// Where the chat window lands on the instance.
const CHAT_PATH: &str = "/chat";

/// How the app was launched, so login-autostart can stay quietly in the tray
/// while a manual launch opens the chat window.
pub struct GuiLaunch {
    pub autostarted: bool,
}

/// A running transport loop plus the handles to stop it. Status is observed
/// separately by `wire_status`, which holds its own `watch::Receiver`.
struct DaemonHandle {
    shutdown_tx: watch::Sender<bool>,
    join: tauri::async_runtime::JoinHandle<()>,
}

#[derive(Default)]
struct AppState {
    daemon: Mutex<Option<DaemonHandle>>,
    status: Mutex<Option<RunnerStatus>>,
    status_item: Mutex<Option<MenuItem<Wry>>>,
    /// The instance origin the chat window's runtime capability was granted
    /// to, and a counter so a re-enrollment against another instance gets a
    /// capability of its own (identifiers must be unique per process).
    bridge_origin: Mutex<Option<String>>,
    bridge_capability_seq: Mutex<u32>,
    /// The browser sign-in in progress, if any: its id and the poll task.
    browser_login: Mutex<Option<BrowserLogin>>,
    /// When web content last asked to open or show a local path.
    local_path_last: Mutex<Option<std::time::Instant>>,
    /// Held by `apply_update` from the download to the restart.
    /// Shared so the blocking install can own its guard until the restart.
    update_lock: std::sync::Arc<tokio::sync::Mutex<()>>,
    /// Whether that install is running, so a quit waits for it.
    install_gate: std::sync::Arc<InstallGate>,
}

/// A browser sign-in handoff the app is waiting on.
struct BrowserLogin {
    login_id: String,
    /// The client the handoff was opened with - the one that cancels it, so a
    /// cancel never has to reload the config and the keychain.
    api: crate::api::ApiClient,
    started_at: std::time::Instant,
    poll: tauri::async_runtime::JoinHandle<()>,
}

// ---------------------------------------------------------------------------
// Serializable snapshots for the panel and the bridge.
// ---------------------------------------------------------------------------

#[derive(Serialize, Clone)]
struct PublicConfig {
    name: String,
    server_url: String,
    runner_id: String,
    platform: String,
    cdp_url: String,
    auto_update: bool,
}

#[derive(Serialize, Clone)]
struct Snapshot {
    enrolled: bool,
    status: RunnerStatus,
    version: String,
    autostart: bool,
    product_name: String,
    config: Option<PublicConfig>,
}

#[derive(Serialize, Clone)]
struct UpdateMeta {
    version: String,
    notes: Option<String>,
}

/// What the web app learns about the machine it runs on. Mirrors
/// `DesktopBridge` in `frontend/src/types/index.ts` - keep the two in lockstep.
/// Read-only facts: nothing here is a secret (the credential never leaves the
/// keychain), and nothing here grants anything - the backend re-checks the
/// runner id against the calling user's visibility on every turn.
#[derive(Serialize, Clone)]
struct BridgeState {
    version: String,
    platform: String,
    arch: String,
    runner_id: Option<String>,
    runner_name: Option<String>,
    server_url: String,
    /// What this shell can do beyond the original bridge - a web app
    /// newer than the installed shell reads it before offering anything.
    features: Vec<&'static str>,
    /// Extensions `desktop_open_local_path` hands to an application; the page
    /// offers `Open` for these and `Show in folder` for everything else.
    open_extensions: Vec<&'static str>,
}

/// The shell capabilities `BridgeState.features` announces: `local_paths` is
/// `desktop_open_local_path` + `desktop_reveal_local_path`, `attention` is
/// `desktop_request_attention`, `session` is `desktop_load_session` +
/// `desktop_store_session`.
const BRIDGE_FEATURES: &[&str] = &["local_paths", "attention", "session"];

fn bridge_state(config: &RunnerConfig) -> BridgeState {
    BridgeState {
        version: AGENT_VERSION.to_string(),
        platform: config.platform.clone(),
        arch: crate::enroll::detect_arch().to_string(),
        runner_id: Some(config.runner_id.clone()),
        runner_name: config.name.clone(),
        server_url: config.base_url(),
        features: BRIDGE_FEATURES.to_vec(),
        open_extensions: local_paths::OPENABLE_EXTENSIONS.to_vec(),
    }
}

// ---------------------------------------------------------------------------
// App entry point.
// ---------------------------------------------------------------------------

pub fn run(launch: GuiLaunch) {
    crate::init_tracing();

    let mut builder = tauri::Builder::default();
    // macOS has one menu bar for the whole app. On Windows and Linux the menu
    // belongs to the chat window (`open_chat_window`): an app-wide menu would
    // also land on the connection panel, which has its own tabs.
    if cfg!(target_os = "macos") {
        builder = builder.menu(|app| {
            build_app_menu(app).or_else(|e| {
                tracing::warn!(error = %e, "application menu unavailable; keeping the default");
                Menu::default(app)
            })
        });
    }
    builder
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            // A second launch brings the app forward - unless it is a login
            // autostart (a still-registered legacy-named entry can fire next
            // to the new one), which must stay quiet like the first one did.
            if !second_launch_is_autostart(&argv) {
                open_chat_or_panel(app);
            }
        }))
        .plugin(tauri_plugin_opener::init())
        .plugin(
            // Launch agent on macOS (the builder's default), never the
            // AppleScript login item: the agent is keyed by name like the
            // other two platforms' entries.
            tauri_plugin_autostart::Builder::new()
                .app_name(AUTOSTART_ENTRY_NAME)
                .args([AUTOSTARTED_FLAG])
                .build(),
        )
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, shortcut, event| {
                    if event.state == ShortcutState::Pressed && *shortcut == summon_shortcut() {
                        toggle_chat_window(app);
                    }
                })
                .build(),
        )
        // The only menu listener. Tauri hands every menu click, tray and
        // application menu alike, to every global listener: a second one
        // would run each action twice.
        .on_menu_event(|app, event| dispatch_menu_event(app, event.id.as_ref()))
        .manage(AppState::default())
        .invoke_handler(tauri::generate_handler![
            get_state,
            enroll,
            unenroll,
            set_auto_update,
            set_cdp_url,
            set_autostart,
            open_instance,
            open_chat,
            check_for_update,
            install_update,
            quit,
            desktop_bridge_state,
            open_panel,
            desktop_begin_browser_login,
            desktop_cancel_browser_login,
            desktop_open_local_path,
            desktop_reveal_local_path,
            desktop_request_attention,
            desktop_load_session,
            desktop_store_session
        ])
        .setup(move |app| {
            let handle = app.handle().clone();
            build_tray(&handle)?;

            // A dockless menu-bar app on macOS until a window is on screen;
            // `present_window` flips it to a regular app while one is.
            #[cfg(target_os = "macos")]
            let _ = handle.set_activation_policy(tauri::ActivationPolicy::Accessory);

            // Closing the panel hides it to the tray instead of quitting.
            if let Some(window) = handle.get_webview_window(PANEL_WINDOW) {
                hide_on_close(&handle, &window);
            }

            // Summon the chat from anywhere. A taken combination is not fatal:
            // the tray and the panel remain, and the log says why.
            if let Err(e) = handle.global_shortcut().register(summon_shortcut()) {
                tracing::warn!(error = %e, "global shortcut unavailable");
            }

            let autostarted = launch.autostarted;
            let update_handle = handle.clone();
            tauri::async_runtime::spawn(async move {
                bootstrap(handle, autostarted).await;
            });
            // One unattended update supervisor for the process lifetime; it idles
            // until the runner is enrolled with auto-update on.
            spawn_auto_update_loop(update_handle);
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build the XTM One Desktop app")
        .run(|_app, event| {
            // Keep running in the tray when the last window closes (code == None);
            // an explicit `app.exit(code)` carries Some(code) and is allowed.
            if let tauri::RunEvent::ExitRequested { code, api, .. } = event {
                if code.is_none() {
                    api.prevent_exit();
                }
            }
        });
}

/// Whether a second launch of the app is a login autostart rather than a
/// person double-clicking the icon.
fn second_launch_is_autostart(argv: &[String]) -> bool {
    argv.iter().any(|arg| arg == AUTOSTARTED_FLAG)
}

/// Start the daemon if enrolled, otherwise open onboarding.
async fn bootstrap(app: AppHandle, autostarted: bool) {
    // After an update restart, what was on screen (`OpenWindows`) decides, not
    // the launch arguments the restart inherited.
    let restore = open_windows_path().and_then(|path| take_open_windows(&path, unix_now()));
    match RunnerConfig::load() {
        Ok(Some(mut config)) => match credential::load(&config.runner_id) {
            Ok(Some(token)) => {
                // Re-apply the stored preference on every launch, so the OS
                // entry always points at the executable this build runs from
                // (an upgrade moves it on Windows). A config from a build
                // without the stored preference has none yet: the OS entry - read
                // under the same name that build wrote it - IS what the
                // person chose, so it is recorded once and honoured.
                if config.launch_at_login.is_none() {
                    adopt_autostart_choice(&app, &mut config);
                }
                if config.launch_at_login.unwrap_or(true) {
                    let _ = app.autolaunch().enable();
                }
                start_daemon(&app, config, token);
                match restore {
                    Some(windows) => restore_open_windows(&app, &windows),
                    None if !autostarted => {
                        if let Err(e) = open_chat_window(&app) {
                            tracing::warn!(error = %e, "could not open the chat window; showing the panel");
                            show_panel(&app, "overview");
                        }
                    }
                    None => {}
                }
            }
            _ => {
                apply_status(&app, &RunnerStatus::NotEnrolled);
                show_panel(&app, "onboarding");
            }
        },
        _ => {
            apply_status(&app, &RunnerStatus::NotEnrolled);
            show_panel(&app, "onboarding");
        }
    }
}

/// One-time adoption of the launch-at-login choice a build without the stored
/// preference kept only in the OS entry (the old panel's switch created and
/// removed it). The entry is read under the very name that build wrote it
/// (`AUTOSTART_ENTRY_NAME` never followed the product name), so its presence
/// IS the person's choice.
/// Recorded in the config so every later launch re-applies it; when the OS
/// cannot be asked, the historical enrollment default (on) applies - never a
/// silent loss of the autostart a fleet relies on.
fn adopt_autostart_choice(app: &AppHandle, config: &mut RunnerConfig) {
    let enabled = match app.autolaunch().is_enabled() {
        Ok(enabled) => enabled,
        Err(e) => {
            tracing::debug!(error = %e, "autostart entry unreadable; keeping autostart on");
            true
        }
    };
    tracing::info!(
        enabled,
        "adopted the launch-at-login choice from the OS autostart entry"
    );
    config.launch_at_login = Some(enabled);
    if let Err(e) = config.save() {
        tracing::warn!(error = %e, "could not store the launch-at-login choice");
    }
}

// ---------------------------------------------------------------------------
// Daemon lifecycle.
// ---------------------------------------------------------------------------

fn start_daemon(app: &AppHandle, config: RunnerConfig, token: String) {
    let state = app.state::<AppState>();
    stop_daemon(&state);

    let (shutdown_tx, _) = watch::channel(false);
    let (status_tx, status_rx) = watch::channel(RunnerStatus::Connecting);
    let reporter = StatusReporter::new(status_tx);

    let serve_shutdown = shutdown_tx.clone();
    let cfg = config.clone();
    let join = tauri::async_runtime::spawn(async move {
        // The GUI updates through the OS-native bundle updater, so the
        // bare-binary self-update path is disabled here.
        let _ = crate::daemon::serve(cfg, token, serve_shutdown, reporter, false).await;
    });

    *state.daemon.lock().unwrap() = Some(DaemonHandle { shutdown_tx, join });

    wire_status(app.clone(), status_rx);
    apply_status(app, &RunnerStatus::Connecting);
}

fn stop_daemon(state: &AppState) {
    if let Some(handle) = state.daemon.lock().unwrap().take() {
        let _ = handle.shutdown_tx.send(true);
        handle.join.abort();
    }
}

/// Mirror every status transition onto the tray + the panel + the chat window.
fn wire_status(app: AppHandle, mut status_rx: watch::Receiver<RunnerStatus>) {
    tauri::async_runtime::spawn(async move {
        loop {
            let status = status_rx.borrow().clone();
            apply_status(&app, &status);
            if status_rx.changed().await.is_err() {
                break;
            }
        }
    });
}

fn apply_status(app: &AppHandle, status: &RunnerStatus) {
    let state = app.state::<AppState>();
    *state.status.lock().unwrap() = Some(status.clone());

    // Event emission is thread-safe; the panel (and the chat page, which is
    // granted `core:event:allow-listen` for it) update from this.
    let _ = app.emit("status", status.clone());

    // Menu + tray mutations must run on the main thread (a no-op detour when we
    // already are on it). `wire_status` drives this from a Tokio task, so never
    // touch muda handles inline.
    let label = status.label().to_string();
    let item = state.status_item.lock().unwrap().clone();
    let app = app.clone();
    let _ = app.clone().run_on_main_thread(move || {
        if let Some(item) = item {
            let _ = item.set_text(format!("Status: {label}"));
        }
        if let Some(tray) = app.tray_by_id("main") {
            let _ = tray.set_tooltip(Some(format!("{PRODUCT_NAME} - {label}")));
        }
    });
}

// ---------------------------------------------------------------------------
// Tray icon + menu.
// ---------------------------------------------------------------------------

fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let header = MenuItem::with_id(app, "header", PRODUCT_NAME, false, None::<&str>)?;
    let status_item = MenuItem::with_id(app, "status", "Status: starting...", false, None::<&str>)?;
    let item = |action: MenuAction, label: &str| {
        MenuItem::with_id(app, action.id(), label, true, None::<&str>)
    };
    let open_chat = item(MenuAction::OpenChat, &format!("Open {PRODUCT_NAME}"))?;
    let open_instance = item(MenuAction::OpenInBrowser, "Open in browser")?;
    let panel = item(MenuAction::ConnectionPanel, "Connection panel")?;
    let settings = item(MenuAction::Settings, "Settings")?;
    let about = item(MenuAction::About, "About")?;
    let quit_item = item(MenuAction::Quit, &format!("Quit {PRODUCT_NAME}"))?;
    let sep1 = PredefinedMenuItem::separator(app)?;
    let sep2 = PredefinedMenuItem::separator(app)?;
    let sep3 = PredefinedMenuItem::separator(app)?;

    let menu = Menu::with_items(
        app,
        &[
            &header,
            &status_item,
            &sep1,
            &open_chat,
            &open_instance,
            &sep2,
            &panel,
            &settings,
            &about,
            &sep3,
            &quit_item,
        ],
    )?;

    *app.state::<AppState>().status_item.lock().unwrap() = Some(status_item);

    let mut builder = TrayIconBuilder::with_id("main")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .tooltip(PRODUCT_NAME)
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                open_chat_or_panel(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder.build(app)?;
    Ok(())
}

// ---------------------------------------------------------------------------
// The application menu, and the actions every menu shares.
// ---------------------------------------------------------------------------

/// What an item of the tray menu or of the application menu does. Both menus
/// name their items by `MenuAction::id`, so one listener serves them both.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum MenuAction {
    OpenChat,
    OpenInBrowser,
    ConnectionPanel,
    Settings,
    About,
    CheckForUpdates,
    Quit,
}

impl MenuAction {
    const ALL: [MenuAction; 7] = [
        MenuAction::OpenChat,
        MenuAction::OpenInBrowser,
        MenuAction::ConnectionPanel,
        MenuAction::Settings,
        MenuAction::About,
        MenuAction::CheckForUpdates,
        MenuAction::Quit,
    ];

    fn id(self) -> &'static str {
        match self {
            MenuAction::OpenChat => "open_chat",
            MenuAction::OpenInBrowser => "open_in_browser",
            MenuAction::ConnectionPanel => "connection_panel",
            MenuAction::Settings => "settings",
            MenuAction::About => "about",
            MenuAction::CheckForUpdates => "check_for_updates",
            MenuAction::Quit => "quit",
        }
    }

    fn from_id(id: &str) -> Option<MenuAction> {
        MenuAction::ALL.into_iter().find(|action| action.id() == id)
    }
}

/// The menu listener (registered once, in `run`). The tray's header and status
/// lines and the native items name no action and are ignored.
fn dispatch_menu_event(app: &AppHandle, id: &str) {
    let Some(action) = MenuAction::from_id(id) else {
        return;
    };
    match action {
        MenuAction::OpenChat => open_chat_or_panel(app),
        MenuAction::OpenInBrowser => {
            let app = app.clone();
            tauri::async_runtime::spawn(async move {
                let _ = open_instance_inner(&app);
            });
        }
        MenuAction::ConnectionPanel => show_panel(app, "overview"),
        MenuAction::Settings => show_panel(app, "settings"),
        MenuAction::About => show_panel(app, "about"),
        MenuAction::CheckForUpdates => {
            show_panel(app, "overview");
            let _ = app.emit_to(PANEL_WINDOW, CHECK_FOR_UPDATES_EVENT, ());
        }
        MenuAction::Quit => request_quit(app),
    }
}

/// Asks the connection panel to run the check of its "Check for updates"
/// button, so the menu's check reports its progress where the button does.
const CHECK_FOR_UPDATES_EVENT: &str = "check-for-updates";

/// An item the OS (macOS) or muda implements itself; its clicks never reach
/// `dispatch_menu_event`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum NativeItem {
    Services,
    Hide,
    HideOthers,
    ShowAll,
    CloseWindow,
    Undo,
    Redo,
    Cut,
    Copy,
    Paste,
    SelectAll,
    Fullscreen,
    Minimize,
    Zoom,
    BringAllToFront,
}

#[derive(Debug)]
enum MenuEntry {
    /// One of the shell's own items, with its keyboard shortcut if any.
    Action {
        action: MenuAction,
        label: String,
        shortcut: Option<&'static str>,
    },
    Native(NativeItem),
    Separator,
}

#[derive(Debug)]
struct SubmenuLayout {
    /// The id macOS gives a role to: the window list, the help search.
    id: Option<&'static str>,
    title: String,
    entries: Vec<MenuEntry>,
}

/// The application menu as each platform lays it out.
///
/// macOS gets the menu bar of a Mac app. Its Edit menu is not decoration: a
/// WKWebView receives Cmd+C / Cmd+V / Cmd+Z only through those items.
///
/// Windows and Linux get a File / Help menu bar on the chat window, without
/// shortcuts: WebView2 never hands a key typed in the page to the window's
/// menu, so a shortcut would be displayed and never fire, and GTK would fire
/// one ahead of the page. No Edit menu either: the page copies and pastes on
/// its own, and muda's Linux edit items do nothing without its libxdo feature,
/// which the app does not link.
fn app_menu_layout(macos: bool) -> Vec<SubmenuLayout> {
    use MenuEntry::{Native, Separator};
    let action =
        |action: MenuAction, label: &str, shortcut: Option<&'static str>| MenuEntry::Action {
            action,
            label: label.to_string(),
            shortcut,
        };
    let submenu = |id: Option<&'static str>, title: &str, entries: Vec<MenuEntry>| SubmenuLayout {
        id,
        title: title.to_string(),
        entries,
    };
    let about = format!("About {PRODUCT_NAME}");
    let quit = format!("Quit {PRODUCT_NAME}");

    if !macos {
        return vec![
            submenu(
                None,
                "File",
                vec![
                    action(MenuAction::ConnectionPanel, "Connection panel", None),
                    action(MenuAction::Settings, "Settings...", None),
                    action(MenuAction::OpenInBrowser, "Open in browser", None),
                    Separator,
                    action(MenuAction::Quit, &quit, None),
                ],
            ),
            submenu(
                None,
                "Help",
                vec![
                    action(MenuAction::CheckForUpdates, "Check for updates...", None),
                    Separator,
                    action(MenuAction::About, &about, None),
                ],
            ),
        ];
    }
    vec![
        submenu(
            None,
            PRODUCT_NAME,
            vec![
                action(MenuAction::About, &about, None),
                action(MenuAction::CheckForUpdates, "Check for updates...", None),
                Separator,
                action(MenuAction::Settings, "Settings...", Some("CmdOrCtrl+,")),
                action(MenuAction::ConnectionPanel, "Connection panel", None),
                Separator,
                Native(NativeItem::Services),
                Separator,
                Native(NativeItem::Hide),
                Native(NativeItem::HideOthers),
                Native(NativeItem::ShowAll),
                Separator,
                action(MenuAction::Quit, &quit, Some("CmdOrCtrl+Q")),
            ],
        ),
        submenu(
            None,
            "File",
            vec![
                action(MenuAction::OpenInBrowser, "Open in browser", None),
                Separator,
                Native(NativeItem::CloseWindow),
            ],
        ),
        submenu(
            None,
            "Edit",
            vec![
                Native(NativeItem::Undo),
                Native(NativeItem::Redo),
                Separator,
                Native(NativeItem::Cut),
                Native(NativeItem::Copy),
                Native(NativeItem::Paste),
                Native(NativeItem::SelectAll),
            ],
        ),
        submenu(None, "View", vec![Native(NativeItem::Fullscreen)]),
        // The chat window, once closed to the tray, is not in the window list.
        submenu(
            Some(WINDOW_SUBMENU_ID),
            "Window",
            vec![
                Native(NativeItem::Minimize),
                Native(NativeItem::Zoom),
                Separator,
                action(MenuAction::OpenChat, "Chat window", None),
                Separator,
                Native(NativeItem::BringAllToFront),
            ],
        ),
        submenu(Some(HELP_SUBMENU_ID), "Help", Vec::new()),
    ]
}

/// The application menu of this platform (`app_menu_layout`).
fn build_app_menu(app: &AppHandle) -> tauri::Result<Menu<Wry>> {
    let menu = Menu::new(app)?;
    for layout in app_menu_layout(cfg!(target_os = "macos")) {
        let submenu = match layout.id {
            Some(id) => Submenu::with_id(app, id, &layout.title, true)?,
            None => Submenu::new(app, &layout.title, true)?,
        };
        for entry in &layout.entries {
            match entry {
                MenuEntry::Action {
                    action,
                    label,
                    shortcut,
                } => submenu.append(&MenuItem::with_id(
                    app,
                    action.id(),
                    label,
                    true,
                    *shortcut,
                )?)?,
                MenuEntry::Native(item) => submenu.append(&native_menu_item(app, *item)?)?,
                MenuEntry::Separator => submenu.append(&PredefinedMenuItem::separator(app)?)?,
            }
        }
        menu.append(&submenu)?;
    }
    Ok(menu)
}

fn native_menu_item(app: &AppHandle, item: NativeItem) -> tauri::Result<PredefinedMenuItem<Wry>> {
    match item {
        NativeItem::Services => PredefinedMenuItem::services(app, None),
        NativeItem::Hide => PredefinedMenuItem::hide(app, None),
        NativeItem::HideOthers => PredefinedMenuItem::hide_others(app, None),
        NativeItem::ShowAll => PredefinedMenuItem::show_all(app, None),
        NativeItem::CloseWindow => PredefinedMenuItem::close_window(app, None),
        NativeItem::Undo => PredefinedMenuItem::undo(app, None),
        NativeItem::Redo => PredefinedMenuItem::redo(app, None),
        NativeItem::Cut => PredefinedMenuItem::cut(app, None),
        NativeItem::Copy => PredefinedMenuItem::copy(app, None),
        NativeItem::Paste => PredefinedMenuItem::paste(app, None),
        NativeItem::SelectAll => PredefinedMenuItem::select_all(app, None),
        NativeItem::Fullscreen => PredefinedMenuItem::fullscreen(app, None),
        NativeItem::Minimize => PredefinedMenuItem::minimize(app, None),
        NativeItem::Zoom => PredefinedMenuItem::maximize(app, None),
        NativeItem::BringAllToFront => PredefinedMenuItem::bring_all_to_front(app, None),
    }
}

// ---------------------------------------------------------------------------
// Window helpers.
// ---------------------------------------------------------------------------

/// The global "summon the chat" combination. Control+Alt+Space on every OS
/// (Claude Desktop's quick-entry default), a combination no platform claims.
fn summon_shortcut() -> Shortcut {
    Shortcut::new(Some(Modifiers::CONTROL | Modifiers::ALT), Code::Space)
}

fn show_panel(app: &AppHandle, section: &str) {
    if let Some(window) = app.get_webview_window(PANEL_WINDOW) {
        present_window(app, &window);
        // Addressed to the panel, not private to it: Tauri also delivers an
        // `emit_to` to the any-target listeners of every webview (a plain JS
        // `listen`), the remote chat page included.
        let _ = window.emit_to(PANEL_WINDOW, "navigate", section.to_string());
    }
}

/// The chat when enrolled, the panel's onboarding otherwise.
fn open_chat_or_panel(app: &AppHandle) {
    match open_chat_window(app) {
        Ok(()) => {}
        Err(_) => show_panel(app, "onboarding"),
    }
}

/// Global shortcut: bring the chat forward, or tuck it away when it is the
/// window on top - one key to summon and dismiss.
fn toggle_chat_window(app: &AppHandle) {
    match app.get_webview_window(CHAT_WINDOW) {
        Some(window)
            if window.is_visible().unwrap_or(false) && window.is_focused().unwrap_or(false) =>
        {
            let _ = window.hide();
            settle_activation_policy(app);
        }
        _ => open_chat_or_panel(app),
    }
}

/// Show + focus a window, and (macOS) become a regular Dock app while a window
/// is on screen.
fn present_window(app: &AppHandle, window: &WebviewWindow) {
    #[cfg(target_os = "macos")]
    let _ = app.set_activation_policy(tauri::ActivationPolicy::Regular);
    #[cfg(not(target_os = "macos"))]
    let _ = app;
    let _ = window.show();
    let _ = window.unminimize();
    let _ = window.set_focus();
}

/// Back to a menu-bar-only app on macOS once no window is visible.
fn settle_activation_policy(app: &AppHandle) {
    #[cfg(target_os = "macos")]
    {
        let any_visible = [PANEL_WINDOW, CHAT_WINDOW]
            .iter()
            .filter_map(|label| app.get_webview_window(label))
            .any(|w| w.is_visible().unwrap_or(false));
        if !any_visible {
            let _ = app.set_activation_policy(tauri::ActivationPolicy::Accessory);
        }
    }
    #[cfg(not(target_os = "macos"))]
    let _ = app;
}

/// Closing a window hides it to the tray; the daemon keeps running.
fn hide_on_close(app: &AppHandle, window: &WebviewWindow) {
    let hidden = window.clone();
    let app = app.clone();
    window.on_window_event(move |event| {
        if let tauri::WindowEvent::CloseRequested { api, .. } = event {
            api.prevent_close();
            let _ = hidden.hide();
            settle_activation_policy(&app);
        }
    });
}

fn open_instance_inner(app: &AppHandle) -> Result<(), String> {
    let config = RunnerConfig::load()
        .map_err(|e| e.to_string())?
        .ok_or_else(|| "not enrolled".to_string())?;
    app.opener()
        .open_url(config.base_url(), None::<&str>)
        .map_err(|e| e.to_string())
}

// ---------------------------------------------------------------------------
// The chat window - the instance's web app in a native window.
// ---------------------------------------------------------------------------

/// Open (or bring forward) the chat window. Errors when not enrolled - the
/// window has no URL to load then - or when the webview cannot be created.
fn open_chat_window(app: &AppHandle) -> Result<(), String> {
    open_chat_window_at(app, None).map(|_| ())
}

/// `open_chat_window`, landing on `page` when it is on the enrolled instance
/// (the page an update restart brings back) instead of the chat.
fn open_chat_window_at(app: &AppHandle, page: Option<&str>) -> Result<WebviewWindow, String> {
    if let Some(window) = app.get_webview_window(CHAT_WINDOW) {
        present_window(app, &window);
        return Ok(window);
    }
    let config = RunnerConfig::load()
        .map_err(|e| e.to_string())?
        .ok_or_else(|| "not enrolled".to_string())?;
    let url = chat_start_url(&config.base_url(), page)?;
    let origin = url.origin().ascii_serialization();
    ensure_bridge_capability(app, &origin)?;

    let init_script = bridge_init_script(&bridge_state(&config), &origin);

    let opener_app = app.clone();
    let navigation_app = app.clone();
    let navigation_origin = origin.clone();
    let downloads_app = app.clone();
    let mut builder = WebviewWindowBuilder::new(app, CHAT_WINDOW, WebviewUrl::External(url))
        .title(PRODUCT_NAME)
        .inner_size(1280.0, 860.0)
        .min_inner_size(900.0, 600.0)
        .center()
        .background_color(tauri::window::Color(7, 13, 25, 255))
        .initialization_script(init_script)
        .zoom_hotkeys_enabled(true)
        // The page handles its own drops (files dragged onto the chat go
        // through the HTML5 `dataTransfer`); Tauri's native drag-drop handler
        // would swallow them on Windows and hand the page file PATHS instead.
        .disable_drag_drop_handler()
        // The window is the enrolled instance's, and stays on it: this is the
        // webview the bridge and the runtime capability were granted to, so
        // no other site may render in it - not an identity provider (sign-in
        // runs in the system browser, `desktop_begin_browser_login`), not a
        // page a link points at, and not a previously enrolled instance whose
        // capability this process still holds. Any other web page opens in
        // the OS browser instead; file://, javascript:, data: and custom
        // schemes load nowhere. (On WebKitGTK this policy also sees subframe
        // navigations - the web app embeds no iframe today; one from another
        // origin would open in the OS browser rather than render.)
        .on_navigation(
            move |url| match navigation_decision(url, &navigation_origin) {
                Navigation::Allow => true,
                Navigation::OpenExternally => {
                    let _ = navigation_app.opener().open_url(url.as_str(), None::<&str>);
                    false
                }
                Navigation::Deny => false,
            },
        )
        // A page that asks for a new window (window.open, a target=_blank the
        // opener plugin did not catch) gets the OS browser, never a second
        // native window carrying the bridge.
        .on_new_window(move |url, _features| {
            if matches!(url.scheme(), "http" | "https" | "mailto" | "tel") {
                let _ = opener_app.opener().open_url(url.as_str(), None::<&str>);
            }
            NewWindowResponse::Deny
        })
        // Downloads land in the user's Downloads folder under the (sanitised)
        // name the server suggested, never overwriting an existing file. The
        // gap between choosing the name and the webview creating the file is
        // accepted: a second download racing for the same name is a rename
        // conflict, not a security boundary.
        .on_download(move |_webview, event| {
            if let DownloadEvent::Requested { url, destination } = event {
                if let Ok(dir) = downloads_app.path().download_dir() {
                    let name = download_file_name(&url, destination);
                    let target = unique_download_path(&dir, &name);
                    // Belt over the sanitiser: whatever the name, the file
                    // lands IN the Downloads folder or the download is refused.
                    if target.parent() != Some(dir.as_path()) {
                        return false;
                    }
                    *destination = target;
                }
            }
            true
        });
    // The Windows / Linux menu bar (macOS sets its menu app-wide in `run`). A
    // menu that cannot be built costs the menu bar, never the chat.
    if !cfg!(target_os = "macos") {
        match build_app_menu(app) {
            Ok(menu) => builder = builder.menu(menu),
            Err(e) => tracing::warn!(error = %e, "application menu unavailable"),
        }
    }
    let window = builder.build().map_err(|e| e.to_string())?;
    hide_on_close(app, &window);
    present_window(app, &window);
    Ok(window)
}

/// Grant the enrolled instance's origin - and only it, in the chat window
/// only - the read-only bridge surface. Idempotent per origin; a later
/// enrollment against another instance adds a second capability (there is no
/// way to revoke one at runtime, and the chat window is destroyed on
/// unenrollment, so the stale grant reaches no page).
fn ensure_bridge_capability(app: &AppHandle, origin: &str) -> Result<(), String> {
    let state = app.state::<AppState>();
    let mut granted = state.bridge_origin.lock().unwrap();
    if granted.as_deref() == Some(origin) {
        return Ok(());
    }
    let mut seq = state.bridge_capability_seq.lock().unwrap();
    *seq += 1;
    app.add_capability(instance_bridge_capability(*seq, origin))
        .map_err(|e| format!("granting the instance bridge capability: {e}"))?;
    *granted = Some(origin.to_string());
    Ok(())
}

/// Everything web content on the instance origin may ask the shell for - the
/// complete list, one permission per line so a review reads it whole. Absent
/// on purpose: enroll, unenroll, settings, quit, updates (the local panel's),
/// any `:default` SET, which bundles more than the line names - the opener's
/// default set also grants `reveal_item_in_dir`, an unscoped command that
/// opens the OS file manager on any path a page names - and every `opener:`
/// PATH permission (`open_path`, `reveal_item_in_dir`): a local path reaches
/// the OS only through the shell's own validated commands below.
const BRIDGE_PERMISSIONS: &[&str] = &[
    // Runner status pushed by `apply_status`, and the browser sign-in outcome
    // pushed by `poll_browser_login`.
    "core:event:allow-listen",
    "core:event:allow-unlisten",
    // The bridge facts on demand, and a way to reach the connection panel.
    "allow-desktop-bridge-state",
    "allow-open-panel",
    // Single sign-on in the system browser: the login page asks the
    // shell to start the handoff and to give up on it. The shell only ever
    // opens the enrolled instance's own SSO route, and the session comes back
    // to THIS window as an event - never to the page that asked.
    "allow-desktop-begin-browser-login",
    "allow-desktop-cancel-browser-login",
    // The opener plugin's own init script routes `target=_blank` and
    // ctrl-clicked links to the OS browser through `open_url`; without this
    // grant the ACL refuses the call AFTER the click's default was prevented,
    // and every such link silently does nothing. The command plus its default
    // URL scope (http, https, mailto, tel) - never `open_path` or
    // `reveal_item_in_dir`.
    "opener:allow-open-url",
    "opener:allow-default-urls",
    // The local side of the chat: open / show a path the agent
    // touched on this machine. The shell's OWN commands - local, existing,
    // canonical, passive content only for Open (`local_paths.rs`), from the
    // focused chat window, rate-limited - never the plugin's path commands.
    "allow-desktop-open-local-path",
    "allow-desktop-reveal-local-path",
    // Flash the taskbar entry / bounce the Dock icon when a turn ends while
    // the window is elsewhere; a no-op while it is focused.
    "allow-desktop-request-attention",
    // The page's own session, kept in the OS keychain and handed back at
    // launch - to the chat window on the enrolled origin only.
    "allow-desktop-load-session",
    "allow-desktop-store-session",
];

/// The runtime capability behind the bridge: `BRIDGE_PERMISSIONS`, for the
/// enrolled origin, in the chat webview only.
fn instance_bridge_capability(seq: u32, origin: &str) -> CapabilityBuilder {
    BRIDGE_PERMISSIONS.iter().fold(
        CapabilityBuilder::new(format!("instance-bridge-{seq}"))
            .local(false)
            .remote(format!("{origin}/*"))
            .window(CHAT_WINDOW)
            .webview(CHAT_WINDOW),
        |capability, permission| capability.permission(*permission),
    )
}

/// The script every top-level document of the chat window runs before its own
/// scripts. Defines the read-only bridge on the instance's origin only - the
/// origin test inside is what keeps any other document (and, where the
/// platform also runs init scripts in subframes, any foreign iframe) from
/// getting it.
fn bridge_init_script(state: &BridgeState, origin: &str) -> String {
    let facts = serde_json::to_string(state).unwrap_or_else(|_| "null".to_string());
    let origin_literal = serde_json::to_string(origin).unwrap_or_else(|_| "\"\"".to_string());
    format!(
        r#"(function () {{
  try {{
    if (window.location.origin !== {origin_literal}) return;
    Object.defineProperty(window, "__XTM_ONE_DESKTOP__", {{
      value: Object.freeze({facts}),
      writable: false,
      configurable: false,
      enumerable: true
    }});
    var mark = function () {{
      if (document.documentElement) {{
        document.documentElement.setAttribute("data-xtm-one-desktop", "1");
      }}
    }};
    if (document.documentElement) {{ mark(); }} else {{ document.addEventListener("DOMContentLoaded", mark); }}
  }} catch (e) {{
    /* the web app works without the bridge; it just is not "the desktop" then */
  }}
}})();"#
    )
}

/// What the chat window does with a navigation request.
#[derive(Debug, PartialEq, Eq)]
enum Navigation {
    /// Load it in the webview: the enrolled instance's own origin, or the
    /// webview's internal `about:` pages.
    Allow,
    /// Another web page: hand it to the OS browser and stay where we are.
    OpenExternally,
    /// Nothing else loads or opens - `file:`, `javascript:`, `data:`, `blob:`,
    /// custom schemes.
    Deny,
}

/// Where the chat window lands: `CHAT_PATH` appended to the enrolled base URL,
/// keeping any path prefix the instance is served under.
fn chat_url(base_url: &str) -> String {
    format!("{}{CHAT_PATH}", base_url.trim_end_matches('/'))
}

/// The chat window's first page: `page` when it is an http(s) page of the
/// enrolled instance's origin carrying no credentials, else `chat_url`. `page`
/// comes from a file in the config directory, so it is held to the origin the
/// bridge is granted to.
fn chat_start_url(base_url: &str, page: Option<&str>) -> Result<Url, String> {
    let chat = Url::parse(&chat_url(base_url)).map_err(|e| e.to_string())?;
    let origin = chat.origin().ascii_serialization();
    Ok(page
        .and_then(|page| Url::parse(page).ok())
        .filter(|page| {
            matches!(page.scheme(), "http" | "https")
                && page.username().is_empty()
                && page.password().is_none()
                && page.origin().ascii_serialization() == origin
        })
        .unwrap_or(chat))
}

/// The chat window's navigation policy. An EXACT origin comparison - scheme,
/// host and port as the `url` crate serialises them (default ports elided,
/// host lower-cased, IDN in punycode) - so `https://one.example` admits
/// neither `http://one.example`, nor `https://one.example:8443`, nor
/// `https://one.example.evil`, nor `https://evil/one.example`.
fn navigation_decision(url: &Url, instance_origin: &str) -> Navigation {
    match url.scheme() {
        "about" => Navigation::Allow,
        "http" | "https" => {
            if url.origin().ascii_serialization() == instance_origin {
                Navigation::Allow
            } else {
                Navigation::OpenExternally
            }
        }
        _ => Navigation::Deny,
    }
}

/// Device names Windows resolves anywhere in the file system, whatever the
/// directory and whatever the extension: `Downloads\CON` is the console,
/// `Downloads\NUL.txt` the bit bucket.
const WINDOWS_RESERVED_NAMES: &[&str] = &[
    "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8",
    "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
];

/// Longest file name we save under (bytes); the extension survives a cut.
const MAX_DOWNLOAD_NAME_LEN: usize = 150;

const GENERIC_DOWNLOAD_NAME: &str = "download";

/// A file name safe to join onto the Downloads folder, whatever a server
/// suggested. Applied to every candidate the same way on every OS (the name
/// comes from the instance, not from the user): no path separators, so the
/// file lands IN the folder and never beside or above it; none of the
/// characters Windows refuses or reads specially (`:` opens an NTFS stream);
/// no control characters; no trailing dots or spaces (Windows drops them, so
/// two names would alias); no reserved device name; bounded length. `..`,
/// `.` and the empty name become `download`.
fn sanitize_download_file_name(name: &str) -> String {
    let cleaned: String = name
        .chars()
        .map(|c| match c {
            '/' | '\\' | ':' | '*' | '?' | '"' | '<' | '>' | '|' => '_',
            c if c.is_control() => '_',
            c => c,
        })
        .collect();
    let mut cleaned = normalize_download_file_name(&cleaned);
    if cleaned.len() > MAX_DOWNLOAD_NAME_LEN {
        let (stem, ext) = split_extension(&cleaned);
        // An "extension" longer than half the budget is not one worth keeping
        // (`a.` + 300 characters): the whole name is cut instead, so the
        // result is never longer than the budget whatever the server sent.
        let (stem, ext) = if ext.len() > MAX_DOWNLOAD_NAME_LEN / 2 {
            (cleaned.as_str(), String::new())
        } else {
            (stem, ext)
        };
        let keep = (MAX_DOWNLOAD_NAME_LEN - ext.len()).min(stem.len());
        let mut cut = keep;
        while !stem.is_char_boundary(cut) {
            cut -= 1;
        }
        // Normalised AGAIN: the cut can end on a dot or a space the first pass
        // never saw (a stem cut right before its own dot, a run of spaces
        // ahead of a long extension) and can bare a device name that padding
        // had hidden. A reserved stem is at most four characters, so its `_`
        // prefix never pushes the cut result over the budget.
        cleaned = normalize_download_file_name(&format!("{}{ext}", &stem[..cut]));
    }
    cleaned
}

/// The invariants a download name keeps whatever its length: no leading
/// spaces, no trailing dots or spaces (Windows drops them, so two names would
/// alias), never empty (`download`), never a reserved device name (prefixed).
fn normalize_download_file_name(name: &str) -> String {
    let mut cleaned = name.trim_end_matches([' ', '.']).trim_start().to_string();
    if cleaned.is_empty() {
        return GENERIC_DOWNLOAD_NAME.to_string();
    }
    let stem = cleaned.split('.').next().unwrap_or("").to_ascii_uppercase();
    if WINDOWS_RESERVED_NAMES.contains(&stem.as_str()) {
        cleaned.insert(0, '_');
    }
    cleaned
}

/// `("report", ".pdf")` for `report.pdf`; the whole name and `""` when there
/// is no extension to keep (`README`, `.hidden`).
fn split_extension(name: &str) -> (&str, String) {
    match name.rsplit_once('.') {
        Some((stem, ext)) if !stem.is_empty() && !ext.is_empty() => (stem, format!(".{ext}")),
        _ => (name, String::new()),
    }
}

/// The file name a download is saved under: what the webview suggested, else
/// the URL's last path segment, else a generic name - sanitised either way.
fn download_file_name(url: &Url, suggested: &Path) -> String {
    let suggested_name = suggested
        .file_name()
        .and_then(|n| n.to_str())
        .filter(|s| !s.is_empty());
    let raw = suggested_name.map(str::to_string).or_else(|| {
        url.path_segments()
            .and_then(|mut segments| segments.next_back())
            .filter(|s| !s.is_empty())
            .map(|s| s.to_string())
    });
    sanitize_download_file_name(raw.as_deref().unwrap_or(GENERIC_DOWNLOAD_NAME))
}

/// Whether a path is already taken - a file, a directory, or a symlink
/// INCLUDING a dangling one (`exists()` follows links and would report a
/// planted `report.pdf -> /elsewhere` as free, and the webview would then
/// write through it).
fn path_is_taken(path: &Path) -> bool {
    std::fs::symlink_metadata(path).is_ok()
}

/// `dir/name`, or `dir/name (n).ext` when that already exists; past a
/// thousand homonyms the clock breaks the tie, so nothing is ever overwritten.
fn unique_download_path(dir: &Path, name: &str) -> PathBuf {
    let candidate = dir.join(name);
    if !path_is_taken(&candidate) {
        return candidate;
    }
    let (stem, ext) = split_extension(name);
    (1..1000)
        .map(|n| dir.join(format!("{stem} ({n}){ext}")))
        .find(|p| !path_is_taken(p))
        .unwrap_or_else(|| {
            let stamp = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_millis())
                .unwrap_or(0);
            dir.join(format!("{stem} ({stamp}){ext}"))
        })
}

/// Tear the chat window down for good (unenrollment): `close()` would only
/// hide it through `hide_on_close`.
fn destroy_chat_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(CHAT_WINDOW) {
        let _ = window.destroy();
    }
}

// ---------------------------------------------------------------------------
// Snapshot.
// ---------------------------------------------------------------------------

fn build_snapshot(app: &AppHandle) -> Snapshot {
    let config = RunnerConfig::load().ok().flatten();
    let enrolled = config
        .as_ref()
        .map(|c| credential::load(&c.runner_id).ok().flatten().is_some())
        .unwrap_or(false);
    // The stored preference is the truth once enrolled (it is what every
    // launch re-applies); before that, whatever the OS entry says.
    let autostart = match &config {
        Some(c) if enrolled => c.launch_at_login.unwrap_or(true),
        _ => app.autolaunch().is_enabled().unwrap_or(false),
    };

    let last_status = app
        .state::<AppState>()
        .status
        .lock()
        .unwrap()
        .clone()
        .unwrap_or(RunnerStatus::NotEnrolled);
    let status = if enrolled {
        last_status
    } else {
        RunnerStatus::NotEnrolled
    };

    let public = config.map(|c| PublicConfig {
        name: c.name.clone().unwrap_or_else(|| "This machine".to_string()),
        server_url: c.base_url(),
        runner_id: c.runner_id.clone(),
        platform: c.platform.clone(),
        cdp_url: c.cdp_url.clone(),
        auto_update: c.auto_update,
    });

    Snapshot {
        enrolled,
        status,
        version: AGENT_VERSION.to_string(),
        autostart,
        product_name: PRODUCT_NAME.to_string(),
        config: public,
    }
}

// ---------------------------------------------------------------------------
// IPC commands (the panel calls these).
// ---------------------------------------------------------------------------

#[tauri::command]
fn get_state(app: AppHandle) -> Snapshot {
    build_snapshot(&app)
}

#[tauri::command]
async fn enroll(
    app: AppHandle,
    server_url: String,
    enroll_key: String,
    name: Option<String>,
) -> Result<Snapshot, String> {
    let name = name.and_then(|n| {
        let trimmed = n.trim().to_string();
        if trimmed.is_empty() {
            None
        } else {
            Some(trimmed)
        }
    });
    let config = crate::enroll::run(crate::enroll::EnrollOptions {
        server_url: server_url.trim().to_string(),
        enrollment_key: enroll_key.trim().to_string(),
        name,
        cdp_url: None,
    })
    .await
    .map_err(|e| format!("{e:#}"))?;

    let token = credential::load(&config.runner_id)
        .map_err(|e| format!("{e:#}"))?
        .ok_or_else(|| "credential missing after enrollment".to_string())?;

    // Persist across reboots by default; the user can disable it in Settings.
    let _ = app.autolaunch().enable();

    start_daemon(&app, config, token);
    // The whole point of enrolling: the chat, on this machine. A failure here
    // is not an enrollment failure - the panel's overview still offers it.
    if let Err(e) = open_chat_window(&app) {
        tracing::warn!(error = %e, "chat window could not be opened after enrollment");
    }
    Ok(build_snapshot(&app))
}

#[tauri::command]
fn unenroll(app: AppHandle) -> Result<Snapshot, String> {
    stop_daemon(&app.state::<AppState>());
    // A browser sign-in still polling would otherwise keep asking the OLD
    // instance with the OLD credential and, on completion, hand that
    // instance's session to whatever chat window exists by then - after a
    // quick re-enrollment, the NEW instance's page.
    if let Some(login) = app.state::<AppState>().browser_login.lock().unwrap().take() {
        login.poll.abort();
        tauri::async_runtime::spawn(async move {
            let _ = login.api.desktop_login_cancel(&login.login_id).await;
        });
    }
    destroy_chat_window(&app);
    if let Ok(Some(config)) = RunnerConfig::load() {
        let _ = credential::delete(&config.runner_id);
        let _ = RunnerConfig::delete();
    }
    let _ = app.autolaunch().disable();
    apply_status(&app, &RunnerStatus::NotEnrolled);
    show_panel(&app, "onboarding");
    Ok(build_snapshot(&app))
}

#[tauri::command]
fn set_auto_update(enabled: bool) -> Result<(), String> {
    let mut config = RunnerConfig::load()
        .map_err(|e| e.to_string())?
        .ok_or_else(|| "not enrolled".to_string())?;
    config.auto_update = enabled;
    config.save().map_err(|e| e.to_string())
}

#[tauri::command]
fn set_cdp_url(app: AppHandle, url: String) -> Result<(), String> {
    let mut config = RunnerConfig::load()
        .map_err(|e| e.to_string())?
        .ok_or_else(|| "not enrolled".to_string())?;
    config.cdp_url = url.trim().to_string();
    config.save().map_err(|e| e.to_string())?;
    // Restart the loop so the new CDP endpoint takes effect immediately.
    if let Ok(Some(token)) = credential::load(&config.runner_id) {
        start_daemon(&app, config, token);
    }
    Ok(())
}

#[tauri::command]
fn set_autostart(app: AppHandle, enabled: bool) -> Result<(), String> {
    let manager = app.autolaunch();
    let outcome = if enabled {
        manager.enable()
    } else {
        manager.disable()
    };
    outcome.map_err(|e| e.to_string())?;
    // Remember the choice so every launch re-applies it (see `bootstrap`).
    if let Ok(Some(mut config)) = RunnerConfig::load() {
        config.launch_at_login = Some(enabled);
        config.save().map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
fn open_instance(app: AppHandle) -> Result<(), String> {
    open_instance_inner(&app)
}

/// The panel's "Open XTM One Desktop" button.
#[tauri::command]
fn open_chat(app: AppHandle) -> Result<(), String> {
    open_chat_window(&app)
}

/// Bridge (remote-callable): the facts the init script injected, on demand -
/// for a page that (re)loaded and wants to confirm what it runs in.
#[tauri::command]
fn desktop_bridge_state() -> Result<BridgeState, String> {
    let config = RunnerConfig::load()
        .map_err(|e| e.to_string())?
        .ok_or_else(|| "not enrolled".to_string())?;
    Ok(bridge_state(&config))
}

/// The panel sections web content may ask for. Onboarding - the enrollment
/// form - is deliberately absent: a page must not be able to steer the user
/// into re-enrolling.
const REMOTE_PANEL_SECTIONS: &[&str] = &["overview", "settings", "about"];

/// The section `open_panel` lands on for an arbitrary (remote-supplied) value.
fn remote_panel_section(requested: Option<&str>) -> &'static str {
    REMOTE_PANEL_SECTIONS
        .iter()
        .copied()
        .find(|s| Some(*s) == requested)
        .unwrap_or("overview")
}

/// Bridge (remote-callable): show the connection panel on a section.
#[tauri::command]
fn open_panel(app: AppHandle, section: Option<String>) {
    show_panel(&app, remote_panel_section(section.as_deref()));
}

// ---------------------------------------------------------------------------
// Local paths and attention.
// ---------------------------------------------------------------------------

/// Least time between two local-path actions web content may trigger: each
/// one brings another application's window up, and a page looping on the
/// command must not be able to flood the desktop with them.
const LOCAL_PATH_MIN_INTERVAL: Duration = Duration::from_millis(400);

/// Refusal codes of the gate, next to `LocalPathError::code`'s - the page maps
/// every one to its own wording.
const LOCAL_PATH_UNFOCUSED: &str = "unfocused";
const LOCAL_PATH_TOO_FAST: &str = "too_fast";
const LOCAL_PATH_FAILED: &str = "failed";

/// Whether a local-path action may proceed, before its path is even parsed:
/// asked by the chat window while it is the window the person is looking at,
/// and not moments after the previous action.
fn local_path_gate(app: &AppHandle, window: &WebviewWindow) -> Result<(), String> {
    if window.label() != CHAT_WINDOW || !window.is_focused().unwrap_or(false) {
        return Err(LOCAL_PATH_UNFOCUSED.to_string());
    }
    let state = app.state::<AppState>();
    let mut last = state.local_path_last.lock().unwrap();
    if last.is_some_and(|at| at.elapsed() < LOCAL_PATH_MIN_INTERVAL) {
        return Err(LOCAL_PATH_TOO_FAST.to_string());
    }
    *last = Some(std::time::Instant::now());
    Ok(())
}

/// Parse and resolve a page-supplied path off the async runtime
/// (canonicalizing can block on a slow disk or a sleeping drive).
async fn resolve_local_path(
    app: &AppHandle,
    raw: String,
) -> Result<(PathBuf, ResolvedPath), String> {
    let home = app.path().home_dir().ok();
    tauri::async_runtime::spawn_blocking(move || -> Result<_, LocalPathError> {
        let input = local_paths::parse_local_path(&raw, home.as_deref())?;
        let resolved = local_paths::resolve_existing(&input)?;
        Ok((input, resolved))
    })
    .await
    .map_err(|_| LOCAL_PATH_FAILED.to_string())?
    .map_err(|e| e.code().to_string())
}

/// Select `path` (already validated) in the OS file manager: never executes it.
fn reveal_in_file_manager(app: &AppHandle, path: &Path) -> Result<(), String> {
    app.opener().reveal_item_in_dir(path).map_err(|e| {
        tracing::warn!(error = %e, "could not show a local path for the chat");
        LOCAL_PATH_FAILED.to_string()
    })
}

/// Bridge (remote-callable): open a local item with its default application
/// - passive content only, a plain folder in the file manager. Anything else
/// (an executable, a script, a file without an extension, a bundle) is shown
/// in its folder instead, in the same call: the person asked to see it, and a
/// second request would meet the rate gate this one just passed.
#[tauri::command]
async fn desktop_open_local_path(
    app: AppHandle,
    webview_window: WebviewWindow,
    path: String,
) -> Result<(), String> {
    local_path_gate(&app, &webview_window)?;
    let (input, resolved) = resolve_local_path(&app, path).await?;
    let Ok(target) = local_paths::plan_open(&input, &resolved) else {
        return reveal_in_file_manager(&app, &resolved.path);
    };
    let target = target
        .to_str()
        .ok_or_else(|| LocalPathError::Invalid.code().to_string())?
        .to_string();
    app.opener().open_path(target, None::<&str>).map_err(|e| {
        tracing::warn!(error = %e, "could not open a local path for the chat");
        LOCAL_PATH_FAILED.to_string()
    })
}

/// Bridge (remote-callable): show a local item selected in the file manager.
/// Never executes anything, so it serves every item `Open` refuses.
#[tauri::command]
async fn desktop_reveal_local_path(
    app: AppHandle,
    webview_window: WebviewWindow,
    path: String,
) -> Result<(), String> {
    local_path_gate(&app, &webview_window)?;
    let (_input, resolved) = resolve_local_path(&app, path).await?;
    reveal_in_file_manager(&app, &resolved.path)
}

/// Bridge (remote-callable): a turn ended while the person was looking at
/// another window - flash the taskbar entry (Windows, Linux) or bounce the
/// Dock icon (macOS) once. Nothing while the chat window has the focus.
#[tauri::command]
fn desktop_request_attention(app: AppHandle) {
    let Some(window) = app.get_webview_window(CHAT_WINDOW) else {
        return;
    };
    if window.is_focused().unwrap_or(false) {
        return;
    }
    let _ = window.request_user_attention(Some(tauri::UserAttentionType::Informational));
}

// ---------------------------------------------------------------------------
// The web session.
// ---------------------------------------------------------------------------

/// Refusal codes of the session commands. The page reads either as "not
/// kept" and goes on with its own storage.
const SESSION_REFUSED: &str = "refused";
const SESSION_UNAVAILABLE: &str = "unavailable";

/// The enrolled configuration, when `window` is the chat window showing the
/// enrolled instance: the one page whose session the shell keeps. The
/// capability already says so; this is the belt over it, as for the browser
/// sign-in's token.
fn web_session_gate(window: &WebviewWindow) -> Result<RunnerConfig, String> {
    let config = RunnerConfig::load()
        .ok()
        .flatten()
        .ok_or_else(|| SESSION_REFUSED.to_string())?;
    let instance = Url::parse(&config.base_url())
        .map(|u| u.origin().ascii_serialization())
        .map_err(|_| SESSION_REFUSED.to_string())?;
    let on_instance = window
        .url()
        .map(|u| u.origin().ascii_serialization() == instance)
        .unwrap_or(false);
    if window.label() != CHAT_WINDOW || !on_instance {
        return Err(SESSION_REFUSED.to_string());
    }
    Ok(config)
}

/// Bridge (remote-callable): the session the shell keeps for the chat page,
/// `None` when the page never handed one over (a shell updated into this
/// feature: the page hands over the one it holds).
#[tauri::command]
fn desktop_load_session(webview_window: WebviewWindow) -> Result<Option<WebSession>, String> {
    let config = web_session_gate(&webview_window)?;
    match credential::load_web_session(&config.runner_id) {
        Ok(kept) => Ok(kept.as_deref().and_then(web_session::parse)),
        Err(e) => {
            tracing::warn!(error = %e, "could not read the kept web session");
            Err(SESSION_UNAVAILABLE.to_string())
        }
    }
}

/// Bridge (remote-callable): keep the session as the page now holds it,
/// signed out included. A session that cannot be kept is forgotten instead:
/// a stale copy would win over the page's own storage at the next launch.
#[tauri::command]
fn desktop_store_session(webview_window: WebviewWindow, session: WebSession) -> Result<(), String> {
    let config = web_session_gate(&webview_window)?;
    // Removed first, from every backend: a value one backend refuses (too long
    // for the Credential Manager) lands in the other, and the older copy left
    // where `load` looks first would be read instead.
    let _ = credential::delete_web_session(&config.runner_id);
    if session.validate().is_err() {
        return Err(SESSION_REFUSED.to_string());
    }
    credential::store_web_session(&config.runner_id, &web_session::serialize(&session)).map_err(
        |e| {
            tracing::warn!(error = %e, "could not keep the web session");
            SESSION_UNAVAILABLE.to_string()
        },
    )
}

// ---------------------------------------------------------------------------
// Single sign-on in the system browser.
// ---------------------------------------------------------------------------

/// The SSO start routes the shell will open. Anything else from web content is
/// refused: the browser is only ever sent to the enrolled instance's own
/// sign-in, never to a URL the page composed.
const BROWSER_LOGIN_PROVIDERS: &[&str] = &["oidc", "saml"];

/// Event the chat window receives when a browser sign-in ends. Payload:
/// `{ status: "completed", access_token }` or `{ status: "failed", reason }`.
const BROWSER_LOGIN_EVENT: &str = "desktop-browser-login";

/// How often the app asks the platform whether the browser flow finished.
const BROWSER_LOGIN_POLL_INTERVAL: Duration = Duration::from_secs(2);

/// Least time between two handoffs web content may start. The command is
/// remote-callable and each call opens a server record and an OS browser tab:
/// a page looping on it must not be able to spray either.
const BROWSER_LOGIN_MIN_INTERVAL: Duration = Duration::from_secs(2);

#[derive(Serialize, Clone)]
struct BrowserLoginStarted {
    login_id: String,
    expires_in_seconds: u64,
}

#[derive(Serialize, Clone)]
#[serde(tag = "status", rename_all = "snake_case")]
enum BrowserLoginOutcome {
    Completed {
        login_id: String,
        access_token: String,
    },
    Failed {
        login_id: String,
        reason: String,
    },
}

/// The instance's SSO start URL for a browser handoff, or `None` for an
/// unknown provider.
fn browser_login_url(base_url: &str, provider: &str, login_id: &str) -> Option<String> {
    if !BROWSER_LOGIN_PROVIDERS.contains(&provider) {
        return None;
    }
    Some(format!(
        "{}/api/auth/{}/login?desktop_login={}",
        base_url.trim_end_matches('/'),
        provider,
        login_id
    ))
}

/// Bridge (remote-callable): run single sign-on in the system browser.
///
/// Opens a pending login with the runner credential, sends the OS browser to
/// the instance's own SSO route carrying its id, and polls until the person
/// completes (and confirms) the sign-in there. The session then reaches the
/// chat window as a `desktop-browser-login` event - the page signs itself in
/// with it. Only one handoff at a time: starting a new one cancels the old.
#[tauri::command]
async fn desktop_begin_browser_login(
    app: AppHandle,
    provider: String,
) -> Result<BrowserLoginStarted, String> {
    // Refuse before anything is loaded, minted or opened: an unknown provider
    // must not leave an orphan pending login behind, and a page looping on
    // this command must not spray records and browser tabs.
    if !BROWSER_LOGIN_PROVIDERS.contains(&provider.as_str()) {
        return Err(format!("unknown sign-in provider '{provider}'"));
    }
    let started_recently = {
        let state = app.state::<AppState>();
        let slot = state.browser_login.lock().unwrap();
        slot.as_ref()
            .is_some_and(|l| l.started_at.elapsed() < BROWSER_LOGIN_MIN_INTERVAL)
    };
    if started_recently {
        return Err("a browser sign-in was started a moment ago".to_string());
    }
    let config = RunnerConfig::load()
        .map_err(|e| e.to_string())?
        .ok_or_else(|| "not enrolled".to_string())?;
    let token = credential::load(&config.runner_id)
        .map_err(|e| e.to_string())?
        .ok_or_else(|| "not enrolled".to_string())?;
    let api = crate::api::ApiClient::new(&config, token).map_err(|e| e.to_string())?;

    // Only one handoff at a time: the previous one is dropped first, with its
    // own client, so its record does not outlive the app's interest in it.
    cancel_browser_login_task(&app).await;

    let started = api
        .desktop_login_start()
        .await
        .map_err(|e| format!("could not start the browser sign-in: {e:#}"))?;
    let url = browser_login_url(&config.base_url(), &provider, &started.login_id)
        .ok_or_else(|| format!("unknown sign-in provider '{provider}'"))?;

    if let Err(e) = app.opener().open_url(&url, None::<&str>) {
        let _ = api.desktop_login_cancel(&started.login_id).await;
        return Err(format!("could not open the browser: {e}"));
    }

    let login_id = started.login_id.clone();
    let deadline_secs = started.expires_in_seconds.max(30);
    let poll_app = app.clone();
    let poll_api = api.clone();
    let poll_id = login_id.clone();
    let poll_origin = Url::parse(&config.base_url())
        .map(|u| u.origin().ascii_serialization())
        .map_err(|e| e.to_string())?;
    let poll = tauri::async_runtime::spawn(async move {
        poll_browser_login(poll_app, poll_api, poll_origin, poll_id, deadline_secs).await;
    });
    *app.state::<AppState>().browser_login.lock().unwrap() = Some(BrowserLogin {
        login_id,
        api,
        started_at: std::time::Instant::now(),
        poll,
    });

    Ok(BrowserLoginStarted {
        login_id: started.login_id,
        expires_in_seconds: started.expires_in_seconds,
    })
}

/// Bridge (remote-callable): the person gave up on the browser sign-in.
#[tauri::command]
async fn desktop_cancel_browser_login(app: AppHandle, login_id: String) -> Result<(), String> {
    let current = app
        .state::<AppState>()
        .browser_login
        .lock()
        .unwrap()
        .as_ref()
        .map(|l| l.login_id.clone());
    if current.as_deref() != Some(login_id.as_str()) {
        return Ok(());
    }
    cancel_browser_login_task(&app).await;
    Ok(())
}

/// Abort the poll task (if any) and tell the platform to drop the pending
/// login, with the client that opened it.
async fn cancel_browser_login_task(app: &AppHandle) {
    let previous = app.state::<AppState>().browser_login.lock().unwrap().take();
    if let Some(previous) = previous {
        previous.poll.abort();
        let _ = previous.api.desktop_login_cancel(&previous.login_id).await;
    }
}

/// Poll the pending login until the session arrives, the platform reports it
/// gone, or the window closes; then tell the chat window how it ended.
async fn poll_browser_login(
    app: AppHandle,
    api: crate::api::ApiClient,
    instance_origin: String,
    login_id: String,
    deadline_secs: u64,
) {
    use crate::api::DesktopLoginPoll;

    let deadline = tokio::time::Instant::now() + Duration::from_secs(deadline_secs);
    let outcome = loop {
        if tokio::time::Instant::now() >= deadline {
            // Nobody will fill this record any more: drop it server-side too.
            let _ = api.desktop_login_cancel(&login_id).await;
            break BrowserLoginOutcome::Failed {
                login_id: login_id.clone(),
                reason: "timeout".to_string(),
            };
        }
        match api.desktop_login_exchange(&login_id).await {
            Ok(DesktopLoginPoll::Completed { access_token }) => {
                break BrowserLoginOutcome::Completed {
                    login_id: login_id.clone(),
                    access_token,
                };
            }
            Ok(DesktopLoginPoll::Gone) => {
                break BrowserLoginOutcome::Failed {
                    login_id: login_id.clone(),
                    reason: "expired".to_string(),
                };
            }
            Ok(DesktopLoginPoll::Pending) => {}
            Err(e) => tracing::debug!(error = %e, "browser sign-in poll failed; retrying"),
        }
        tokio::time::sleep(BROWSER_LOGIN_POLL_INTERVAL).await;
    };

    // This handoff is over either way: forget it before the page hears the
    // outcome, so a cancel racing the completion finds nothing to cancel.
    {
        let state = app.state::<AppState>();
        let mut slot = state.browser_login.lock().unwrap();
        if slot.as_ref().is_some_and(|l| l.login_id == login_id) {
            *slot = None;
        }
    }
    let Some(window) = app.get_webview_window(CHAT_WINDOW) else {
        return;
    };
    // A session goes to the instance's page and to nothing else. The
    // navigation policy keeps the window on the instance, and this is the
    // belt over it: `emit` reaches whatever document the webview shows and a
    // navigated-away page's listeners stay registered, so an off-origin
    // document would otherwise hear the token.
    if matches!(outcome, BrowserLoginOutcome::Completed { .. }) {
        let on_instance = window
            .url()
            .map(|u| u.origin().ascii_serialization() == instance_origin)
            .unwrap_or(false);
        if !on_instance {
            tracing::warn!(
                "chat window left the instance during the browser sign-in; session dropped"
            );
            return;
        }
        tracing::info!("browser sign-in completed; signing the chat window in");
    }
    // Addressed to the chat window's label; the connection panel has no use
    // for it and nothing else listens.
    let _ = window.emit_to(CHAT_WINDOW, BROWSER_LOGIN_EVENT, outcome);
    present_window(&app, &window);
}

#[tauri::command]
async fn check_for_update(app: AppHandle) -> Result<Option<UpdateMeta>, String> {
    let updater = build_updater(&app)?;
    match updater.check().await.map_err(|e| e.to_string())? {
        Some(update) => {
            let update = installable_update(update)?;
            Ok(Some(UpdateMeta {
                version: update.version.clone(),
                notes: update.body.clone(),
            }))
        }
        None => Ok(None),
    }
}

#[tauri::command]
async fn install_update(app: AppHandle) -> Result<(), String> {
    let updater = build_updater(&app)?;
    if let Some(update) = updater.check().await.map_err(|e| e.to_string())? {
        apply_update(&app, installable_update(update)?).await?;
    }
    Ok(())
}

/// Download `update`, install it and restart into it, recording the windows
/// on screen first for the new build to bring back (`bootstrap`). On Windows
/// `install` hands over to the installer and exits the process - the installer
/// relaunches the app - so it returns only on failure.
async fn apply_update(app: &AppHandle, update: tauri_plugin_updater::Update) -> Result<(), String> {
    // The panel's button and the auto-update loop both land here: one attempt
    // at a time, so two installers never race and a failed attempt never takes
    // back the record another one is about to restart on.
    let state = app.state::<AppState>();
    let updating = state.update_lock.clone().lock_owned().await;
    // Refused before the download too, so nothing is fetched for nothing.
    install_preflight(app)?;
    let bytes = update
        .download(|_downloaded, _total| {}, || {})
        .await
        .map_err(|e| e.to_string())?;
    let path = open_windows_path();
    let windows = current_open_windows(app);
    let gate = state.install_gate.clone();
    let restart_gate = gate.clone();
    let notice = app.clone();
    let quitting = app.clone();
    let handle = app.clone();
    install_then_restart(
        updating,
        move || {
            gated_install(
                &gate,
                || {
                    // Checked again right before the plugin runs: the
                    // permissions can change during the download. Inside the
                    // gate, so a quit during the notice waits too.
                    if install_preflight(&notice)?.needs_administrator {
                        announce_administrator_prompt(&notice);
                    }
                    install_with_record(path.as_deref(), &windows, || {
                        update.install(bytes).map_err(|e| e.to_string())
                    })
                },
                || quit_now(&quitting),
            )
        },
        move || -> std::convert::Infallible {
            restart_or_quit(
                &restart_gate,
                || -> std::convert::Infallible { handle.restart() },
                || -> std::convert::Infallible {
                    quit_now(&handle);
                    park_forever()
                },
            )
        },
    )
    .await
    .map(|restarted| match restarted {})
}

/// A running install, and a quit asked for while it runs (`request_quit`).
#[derive(Default)]
struct InstallGate {
    state: Mutex<InstallGateState>,
}

#[derive(Default)]
struct InstallGateState {
    installing: bool,
    quit_held: bool,
    /// A quit went through: the event loop the macOS install dispatches to
    /// is on its way out, so no install may start any more.
    closing: bool,
}

impl InstallGate {
    /// Start an install; `false` once the app is quitting.
    fn begin(&self) -> bool {
        let mut state = self.state.lock().unwrap();
        state.installing = !state.closing;
        state.installing
    }

    /// The install ended; whether a quit was held meanwhile (and is now due).
    fn end(&self) -> bool {
        let mut state = self.state.lock().unwrap();
        state.installing = false;
        let quit_due = std::mem::take(&mut state.quit_held);
        // The held quit is carried out now: no install may start after it.
        state.closing |= quit_due;
        quit_due
    }

    /// Hold a quit while an install runs; `false` when none does, and the
    /// quit goes through.
    fn hold_quit(&self) -> bool {
        let mut state = self.state.lock().unwrap();
        state.quit_held |= state.installing;
        state.closing |= !state.installing;
        state.installing
    }
}

/// Run `install` inside `gate`. A quit held while it ran is carried out once
/// it fails; a successful install leaves it to `restart_or_quit`.
fn gated_install(
    gate: &InstallGate,
    install: impl FnOnce() -> Result<(), String>,
    quit: impl FnOnce(),
) -> Result<(), String> {
    if !gate.begin() {
        return Err("the app is quitting; the update was not installed".to_string());
    }
    let installed = install();
    if installed.is_err() && gate.end() {
        quit();
    }
    installed
}

/// After a successful install: quit if a quit was held during it, restart
/// into the new build otherwise.
fn restart_or_quit<R>(
    gate: &InstallGate,
    restart: impl FnOnce() -> R,
    quit: impl FnOnce() -> R,
) -> R {
    if gate.end() {
        quit()
    } else {
        restart()
    }
}

fn park_forever() -> ! {
    loop {
        std::thread::park();
    }
}

/// What the install will need that the person should hear about first.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct InstallPlan {
    /// The macOS updater will show an administrator dialog, from the main
    /// thread: the app's windows stop responding until it is answered.
    needs_administrator: bool,
}

/// Refuse, before any download, an install the updater plugin
/// (tauri-plugin-updater 2.10.1) would panic on - release builds abort on a
/// panic, which would end the app halfway through an update.
fn install_preflight(app: &AppHandle) -> Result<InstallPlan, String> {
    #[cfg(windows)]
    check_installer_args(&app.env().args_os)?;
    // The executable the updater derives the bundle it moves from.
    #[cfg(target_os = "macos")]
    if let Some(bundle) = tauri::utils::platform::current_exe()
        .ok()
        .and_then(|exe| app_bundle_of(&exe))
    {
        if macos_move_needs_administrator(&bundle) {
            check_privileged_move(&bundle, &std::env::temp_dir())?;
            return Ok(InstallPlan {
                needs_administrator: true,
            });
        }
    }
    let _ = app;
    Ok(InstallPlan {
        needs_administrator: false,
    })
}

/// Windows: `install` passes the app's own arguments minus the first to the
/// installer (`current_exe_args()[1..]`), which panics on an empty list. The
/// standard library never hands a Windows process one (an empty command line
/// still yields the executable as the first argument), so this only keeps
/// that slice safe whatever builds the app's `Env`.
#[cfg_attr(not(windows), allow(dead_code))]
fn check_installer_args(args: &[std::ffi::OsString]) -> Result<(), String> {
    if args.is_empty() {
        return Err(
            "the updater does not know this app's command line, which the Windows \
             installer needs to restart it; install the update from the Desktop page \
             of the instance"
                .to_string(),
        );
    }
    Ok(())
}

/// Linux: the executable the updater should replace, when the one it would
/// pick up (`APPIMAGE`) has no parent directory - its temp-dir fallback
/// (`extract_path.parent().unwrap()`) would panic on it: `current_exe`, the
/// one it uses without `APPIMAGE`. `None` keeps the updater's own choice; an
/// error when such an `APPIMAGE` has nothing to stand in for it.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
fn updater_executable_override(
    appimage: Option<&std::ffi::OsStr>,
    current_exe: Option<PathBuf>,
) -> Result<Option<PathBuf>, String> {
    match appimage {
        Some(path) if Path::new(path).parent().is_none() => {
            current_exe.map(Some).ok_or_else(|| {
                "the updater cannot tell which executable to replace: APPIMAGE names none \
             and this app's own path is unknown; install the update from the Desktop \
             page of the instance"
                    .to_string()
            })
        }
        _ => Ok(None),
    }
}

/// macOS: the app bundle the updater replaces, found the way it finds it
/// (`extract_path_from_executable`).
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
fn app_bundle_of(executable: &Path) -> Option<PathBuf> {
    let dir = executable.parent()?;
    if dir.display().to_string().contains("Contents/MacOS") {
        return Some(dir.parent()?.parent()?.to_path_buf());
    }
    Some(dir.to_path_buf())
}

/// macOS: a bundle the updater must move with an administrator's rights goes
/// through `do shell script "rm -rf '<bundle>' && mv -f '<temp>' '<bundle>'"`
/// built from the raw paths. A `"` or `\` breaks the AppleScript string and
/// `compile().expect(..)` panics; a `'` splits the shell quoting, so `rm -rf`
/// would be handed another path.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
fn check_privileged_move(bundle: &Path, temp: &Path) -> Result<(), String> {
    let unsafe_char = |path: &Path| path.display().to_string().contains(['\'', '"', '\\']);
    if unsafe_char(bundle) || unsafe_char(temp) {
        return Err(format!(
            "the update needs an administrator to replace {}, and macOS cannot be asked \
             for one safely for a path holding a quote or a backslash; move the app to \
             a folder you can write to, or install the update from the Desktop page of \
             the instance",
            bundle.display()
        ));
    }
    Ok(())
}

/// What decides whether `rename(2)` may move the app bundle out of its folder.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
#[derive(Clone, Copy, Debug)]
struct MoveAccess {
    bundle_writable: bool,
    parent_writable: bool,
    /// The folder has its sticky bit set: only the owner of the entry, the
    /// owner of the folder or the superuser may move an entry out of it.
    parent_sticky: bool,
    bundle_owned: bool,
    parent_owned: bool,
    superuser: bool,
    /// The bundle or its folder carries an immutable or append-only flag.
    immutable: bool,
}

/// Whether the updater's `rename` of the bundle fails with `PermissionDenied`
/// (`EACCES` / `EPERM`), which is exactly when it asks for an administrator.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
fn move_needs_administrator(access: MoveAccess) -> bool {
    !(access.bundle_writable && access.parent_writable)
        || (access.parent_sticky
            && !(access.bundle_owned || access.parent_owned || access.superuser))
        || access.immutable
}

/// macOS: `move_needs_administrator` for `bundle`, read off the file system.
#[cfg(target_os = "macos")]
fn macos_move_needs_administrator(bundle: &Path) -> bool {
    use std::os::macos::fs::MetadataExt as _;
    use std::os::raw::{c_char, c_int, c_uint};
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::MetadataExt;
    extern "C" {
        fn access(path: *const c_char, mode: c_int) -> c_int;
        fn getuid() -> c_uint;
    }
    const W_OK: c_int = 2;
    // UF_IMMUTABLE | UF_APPEND | SF_IMMUTABLE | SF_APPEND (<sys/stat.h>).
    const LOCKED: u32 = 0x0000_0002 | 0x0000_0004 | 0x0002_0000 | 0x0004_0000;
    let writable = |path: &Path| {
        std::ffi::CString::new(path.as_os_str().as_bytes())
            // SAFETY: `access` reads a NUL-terminated path and nothing else.
            .map(|path| unsafe { access(path.as_ptr(), W_OK) } == 0)
            .unwrap_or(false)
    };
    let Some(parent) = bundle.parent() else {
        return true;
    };
    let (Ok(bundle_meta), Ok(parent_meta)) = (bundle.metadata(), parent.metadata()) else {
        return true;
    };
    // SAFETY: `getuid` takes no argument and cannot fail.
    let uid = unsafe { getuid() };
    move_needs_administrator(MoveAccess {
        bundle_writable: writable(bundle),
        parent_writable: writable(parent),
        parent_sticky: parent_meta.mode() & 0o1000 != 0,
        bundle_owned: bundle_meta.uid() == uid,
        parent_owned: parent_meta.uid() == uid,
        superuser: uid == 0,
        immutable: (bundle_meta.st_flags() | parent_meta.st_flags()) & LOCKED != 0,
    })
}

/// Tells the connection panel the macOS administrator dialog is coming.
const UPDATE_APPROVAL_EVENT: &str = "update-needs-approval";

/// Long enough for the panel to show the notice before the dialog, shown
/// from the main thread, stops it from repainting.
const APPROVAL_NOTICE_LEAD: Duration = Duration::from_millis(800);

/// Say, in the panel, that an administrator dialog is about to open and the
/// app's windows wait for it - the updater shows it from the main thread.
fn announce_administrator_prompt(app: &AppHandle) {
    show_panel(app, "overview");
    let _ = app.emit_to(PANEL_WINDOW, UPDATE_APPROVAL_EVENT, ());
    std::thread::sleep(APPROVAL_NOTICE_LEAD);
}

/// Install, then restart into the new build, both on the blocking pool and
/// with `updating` (the update lock) held throughout. The blocking task
/// outlives a cancelled wait, so it owns both: handed back to the caller, the
/// lock would be released while the install still ran, and a successful
/// install would leave the old build running, to install the same update
/// again at its next check. In the app `restart` never returns - the process
/// restarts and the lock goes with it; a failed install releases the lock for
/// the next attempt.
async fn install_then_restart<R: Send + 'static>(
    updating: tokio::sync::OwnedMutexGuard<()>,
    install: impl FnOnce() -> Result<(), String> + Send + 'static,
    restart: impl FnOnce() -> R + Send + 'static,
) -> Result<R, String> {
    install_off_runtime(move || {
        let _updating = updating;
        install()?;
        Ok(restart())
    })
    .await
}

/// Run `install` on the blocking pool and wait for it. An install can wait on
/// a person for as long as a prompt stays open - `pkexec` (then a password
/// dialog, then `sudo`) for a `.deb` / `.rpm` update, the administrator prompt
/// for a macOS app that is not writable - and run inline it would hold a
/// worker of the runtime the daemon (heartbeats, command polling) and the
/// panel's commands share for that long. Its result comes back unchanged; a
/// dropped wait never stops it, so what it owns lives until it ends.
async fn install_off_runtime<T: Send + 'static>(
    install: impl FnOnce() -> Result<T, String> + Send + 'static,
) -> Result<T, String> {
    tauri::async_runtime::spawn_blocking(install)
        .await
        .map_err(|e| format!("the update install did not complete: {e}"))?
}

/// Record `windows` at `path`, then install: the restarted build reopens what
/// the record says, and a failed install takes the record back.
fn install_with_record(
    path: Option<&Path>,
    windows: &OpenWindows,
    install: impl FnOnce() -> Result<(), String>,
) -> Result<(), String> {
    if let Some(path) = path {
        if let Err(e) = save_open_windows(path, windows) {
            tracing::warn!(error = %e, "could not record the open windows for the restart");
        }
    }
    let installed = install();
    if installed.is_err() {
        if let Some(path) = path {
            let _ = std::fs::remove_file(path);
        }
    }
    installed
}

/// The windows on screen when the app went down to install an update. The
/// build it restarts into gets the previous launch's arguments - an app
/// started at login keeps `--autostarted` - so they cannot say what to show.
#[derive(Serialize, Deserialize, Debug, Default, PartialEq, Eq)]
#[serde(default)]
struct OpenWindows {
    /// Unix seconds: a record no restart consumed (an installer that failed
    /// once the app had quit) must not reopen windows at a later launch.
    saved_at: u64,
    chat: bool,
    /// The page the chat window showed.
    chat_page: Option<String>,
    chat_maximized: bool,
    panel: bool,
}

/// How long a record stays good: the installer runs between the save and the
/// restart.
const OPEN_WINDOWS_MAX_AGE: Duration = Duration::from_secs(10 * 60);

/// Bound on the record: a few flags and one URL, in a directory every process
/// of the user can write.
const OPEN_WINDOWS_MAX_BYTES: u64 = 64 * 1024;

impl OpenWindows {
    fn is_fresh(&self, now: u64) -> bool {
        now.checked_sub(self.saved_at)
            .is_some_and(|age| age <= OPEN_WINDOWS_MAX_AGE.as_secs())
    }
}

fn unix_now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn open_windows_path() -> Option<PathBuf> {
    RunnerConfig::config_dir()
        .ok()
        .map(|dir| dir.join("open-windows.json"))
}

/// What is on screen now. A minimized window is open too (on macOS it is not
/// "visible").
fn current_open_windows(app: &AppHandle) -> OpenWindows {
    let is_open = |window: &WebviewWindow| {
        window.is_visible().unwrap_or(false) || window.is_minimized().unwrap_or(false)
    };
    let chat = app.get_webview_window(CHAT_WINDOW).filter(is_open);
    OpenWindows {
        saved_at: unix_now(),
        chat: chat.is_some(),
        chat_page: chat
            .as_ref()
            .and_then(|window| window.url().ok())
            .map(String::from),
        chat_maximized: chat
            .as_ref()
            .is_some_and(|window| window.is_maximized().unwrap_or(false)),
        panel: app
            .get_webview_window(PANEL_WINDOW)
            .is_some_and(|window| is_open(&window)),
    }
}

fn save_open_windows(path: &Path, windows: &OpenWindows) -> std::io::Result<()> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    std::fs::write(path, serde_json::to_vec(windows)?)?;
    crate::config::restrict_permissions(path);
    Ok(())
}

/// Read the record and delete it - it is consumed once, whatever it says.
/// `None` when there is none, or it is not a small plain file, unreadable,
/// malformed, stale, or cannot be deleted.
fn take_open_windows(path: &Path, now: u64) -> Option<OpenWindows> {
    let read = read_open_windows(path);
    if read
        .as_ref()
        .is_err_and(|e| e.kind() == std::io::ErrorKind::NotFound)
    {
        return None;
    }
    accept_open_windows(read, std::fs::remove_file(path), now)
}

/// The record's bytes; `None` for anything but a plain file (a link, a
/// directory, a pipe) and for a file over `OPEN_WINDOWS_MAX_BYTES`.
fn read_open_windows(path: &Path) -> std::io::Result<Option<Vec<u8>>> {
    use std::io::Read;
    if !std::fs::symlink_metadata(path)?.is_file() {
        return Ok(None);
    }
    let mut raw = Vec::new();
    std::fs::File::open(path)?
        .take(OPEN_WINDOWS_MAX_BYTES + 1)
        .read_to_end(&mut raw)?;
    Ok(((raw.len() as u64) <= OPEN_WINDOWS_MAX_BYTES).then_some(raw))
}

/// What a consumed record counts for: nothing until it is gone - one that
/// could not be deleted would reopen the windows at every launch.
fn accept_open_windows(
    read: std::io::Result<Option<Vec<u8>>>,
    removed: std::io::Result<()>,
    now: u64,
) -> Option<OpenWindows> {
    if let Err(e) = removed {
        tracing::warn!(error = %e, "could not delete the open windows record; ignoring it");
        return None;
    }
    let raw = read.ok().flatten()?;
    serde_json::from_slice::<OpenWindows>(&raw)
        .ok()
        .filter(|windows| windows.is_fresh(now))
}

/// Bring back what an update restart took down: the chat on the page it
/// showed, maximized if it was, then the panel on its overview (the new
/// version is there, where a check started from the panel left off).
fn restore_open_windows(app: &AppHandle, windows: &OpenWindows) {
    let mut panel = windows.panel;
    if windows.chat {
        match open_chat_window_at(app, windows.chat_page.as_deref()) {
            Ok(window) => {
                if windows.chat_maximized {
                    let _ = window.maximize();
                }
            }
            Err(e) => {
                tracing::warn!(error = %e, "could not reopen the chat window; showing the panel");
                panel = true;
            }
        }
    }
    if panel {
        show_panel(app, "overview");
    }
}

/// Interval between unattended update checks on the GUI auto-update path.
const AUTO_UPDATE_INTERVAL: Duration = Duration::from_secs(6 * 60 * 60);

/// Tells the connection panel an update exists that the loop will not install
/// itself (a `.deb` / `.rpm` install, see `auto_install_allowed`); the panel's
/// update button then installs it.
const UPDATE_AVAILABLE_EVENT: &str = "update-available";

/// Whether the unattended loop may install an update for this bundle.
///
/// A `.deb` / `.rpm` update runs `dpkg -i` / `rpm -U` through `pkexec` (then a
/// zenity / kdialog password dialog, then `sudo`), so installing one
/// unattended would raise an administrator password prompt out of nowhere on
/// every cycle. Those installs announce the update and install it on the
/// user's click; every other bundle - AppImage, NSIS, the macOS app,
/// or an unpatched dev build (`None`) - keeps installing unattended.
fn auto_install_allowed(bundle: Option<tauri::utils::config::BundleType>) -> bool {
    use tauri::utils::config::BundleType;
    !matches!(bundle, Some(BundleType::Deb | BundleType::Rpm))
}

/// Whether this bundle can install the update the updater resolved.
///
/// A `.deb` / `.rpm` install only takes its own package. When the instance
/// publishes none - a platform image that ships no packages, or a package whose
/// signature is missing - the updater falls back to the `linux-<arch>`
/// AppImage, which `install_deb` / `install_rpm` refuse, and only after the
/// whole download. Every other bundle takes what the updater resolved.
fn update_installs_on(
    bundle: Option<tauri::utils::config::BundleType>,
    download_url: &Url,
) -> bool {
    use tauri::utils::config::BundleType;
    let package = match bundle {
        Some(BundleType::Deb) => ".deb",
        Some(BundleType::Rpm) => ".rpm",
        _ => return true,
    };
    download_url.path().ends_with(package)
}

/// `update`, unless this install cannot take it (`update_installs_on`): the
/// panel's check and install say so before any download.
fn installable_update(
    update: tauri_plugin_updater::Update,
) -> Result<tauri_plugin_updater::Update, String> {
    if update_installs_on(tauri::utils::platform::bundle_type(), &update.download_url) {
        Ok(update)
    } else {
        Err(format!(
            "your instance does not publish version {} as this package; \
             install it from the Desktop page of the instance",
            update.version
        ))
    }
}

/// Surface an update the user installs themselves: bring up the panel on its
/// overview, where the update button now installs the announced version.
fn announce_update(app: &AppHandle, meta: UpdateMeta) {
    show_panel(app, "overview");
    let _ = app.emit_to(PANEL_WINDOW, UPDATE_AVAILABLE_EVENT, meta);
}

/// Background supervisor: while the runner is enrolled with `auto_update` on,
/// periodically ask the enrolled server for a newer signed bundle and apply it
/// (a `.deb` / `.rpm` install announces it instead, `auto_install_allowed`).
/// This keeps a fleet of thousands current with zero operator touch, which is
/// the whole reason `auto_update` defaults to true. Signature verification is
/// enforced by the bundle updater (minisign) regardless of this toggle; the
/// toggle only governs whether we apply automatically or just let the operator
/// click "Check for updates". The loop reloads config each tick so toggling the
/// setting (or enrolling later) takes effect without a restart, and runs for the
/// whole process lifetime as a single instance (never per-daemon-start, so
/// re-enrolling can't stack duplicate loops).
fn spawn_auto_update_loop(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        // Let the daemon connect first on a cold launch before competing for the
        // network; a pending update is not more urgent than coming online.
        tokio::time::sleep(Duration::from_secs(90)).await;
        // A package install announces each new version once per launch, not
        // on every cycle.
        let mut announced: Option<String> = None;
        loop {
            let auto = matches!(RunnerConfig::load(), Ok(Some(c)) if c.auto_update);
            if auto {
                if let Ok(updater) = build_updater(&app) {
                    match updater.check().await {
                        // Neither announced nor installed: the click would
                        // download an update this install then refuses.
                        Ok(Some(update))
                            if !update_installs_on(
                                tauri::utils::platform::bundle_type(),
                                &update.download_url,
                            ) =>
                        {
                            tracing::warn!(
                                version = %update.version,
                                url = %update.download_url,
                                "auto-update: the instance publishes no update in this \
                                 package's format; reinstall from its Desktop page"
                            );
                        }
                        Ok(Some(update))
                            if !auto_install_allowed(tauri::utils::platform::bundle_type()) =>
                        {
                            if announced.as_deref() != Some(update.version.as_str()) {
                                tracing::info!(
                                    version = %update.version,
                                    "auto-update: newer desktop build available; a package \
                                     install updates on the user's click"
                                );
                                announced = Some(update.version.clone());
                                announce_update(
                                    &app,
                                    UpdateMeta {
                                        version: update.version.clone(),
                                        notes: update.body.clone(),
                                    },
                                );
                            }
                        }
                        Ok(Some(update)) => {
                            tracing::info!(
                                version = %update.version,
                                "auto-update: newer desktop build available, installing"
                            );
                            if let Err(e) = apply_update(&app, update).await {
                                tracing::warn!(
                                    error = %e,
                                    "auto-update: install failed; will retry next cycle"
                                );
                            }
                        }
                        Ok(None) => {}
                        Err(e) => tracing::debug!(error = %e, "auto-update: check failed"),
                    }
                }
            }
            tokio::time::sleep(AUTO_UPDATE_INTERVAL).await;
        }
    });
}

/// Build an updater pointed at the enrolled server (the bundled endpoint is only
/// a default; a self-hosted install updates from its own platform).
fn build_updater(app: &AppHandle) -> Result<tauri_plugin_updater::Updater, String> {
    let config = RunnerConfig::load()
        .map_err(|e| e.to_string())?
        .ok_or_else(|| "not enrolled".to_string())?;
    let endpoint = format!(
        "{}/api/v1/runners/updater/{{{{target}}}}/{{{{arch}}}}/{{{{current_version}}}}",
        config.base_url()
    );
    let url = tauri::Url::parse(&endpoint).map_err(|e| e.to_string())?;
    let builder = app
        .updater_builder()
        .endpoints(vec![url])
        .map_err(|e| e.to_string())?;
    #[cfg(target_os = "linux")]
    let builder = match updater_executable_override(
        app.env().appimage.as_deref(),
        tauri::utils::platform::current_exe().ok(),
    )? {
        Some(path) => builder.executable_path(path),
        None => builder,
    };
    builder.build().map_err(|e| e.to_string())
}

#[tauri::command]
fn quit(app: AppHandle) {
    request_quit(&app);
}

/// Quit, unless an update install is running: it may be waiting on a system
/// password prompt (`pkexec`, the macOS administrator dialog), which would
/// outlive the app and install the package with the app gone. The quit is
/// then held - the panel says why, the machine stays connected - and carried
/// out once the install ends (`gated_install`, `restart_or_quit`), or once
/// `QUIT_HOLD_LIMIT` has passed.
fn request_quit(app: &AppHandle) {
    let quitting = app.clone();
    hold_or_quit(
        &app.state::<AppState>().install_gate,
        QUIT_HOLD_LIMIT,
        || {
            show_panel(app, "overview");
            let _ = app.emit_to(PANEL_WINDOW, QUIT_HELD_EVENT, QUIT_HOLD_LIMIT.as_secs());
        },
        move || quit_now(&quitting),
    );
}

/// The longest a quit waits for an update install. Only a password prompt
/// keeps an install running this long, and one nobody answers must not keep
/// the app from quitting: past this the app quits and leaves it open.
const QUIT_HOLD_LIMIT: Duration = Duration::from_secs(60);

/// `quit` at once when no install runs. Otherwise the quit is held: `held`
/// says so, the install carries it out when it ends, and `quit` runs after
/// `limit` whether it ended or not.
fn hold_or_quit(
    gate: &InstallGate,
    limit: Duration,
    held: impl FnOnce(),
    quit: impl FnOnce() + Send + 'static,
) {
    if gate.hold_quit() {
        held();
        tauri::async_runtime::spawn(async move {
            tokio::time::sleep(limit).await;
            quit();
        });
    } else {
        quit();
    }
}

fn quit_now(app: &AppHandle) {
    stop_daemon(&app.state::<AppState>());
    app.exit(0);
}

/// Tells the connection panel a quit waits for an update install, and for how
/// many seconds at most.
const QUIT_HELD_EVENT: &str = "quit-held";

#[cfg(test)]
mod tests {
    use super::*;

    fn state() -> BridgeState {
        BridgeState {
            version: "1.260925.7".into(),
            platform: "windows".into(),
            arch: "x64".into(),
            runner_id: Some("2f1c9d7a-0000-4000-8000-000000000000".into()),
            runner_name: Some("SAMUEL-LAPTOP".into()),
            server_url: "https://one.filigran.io".into(),
            features: BRIDGE_FEATURES.to_vec(),
            open_extensions: local_paths::OPENABLE_EXTENSIONS.to_vec(),
        }
    }

    #[test]
    fn the_bridge_announces_what_the_shell_can_do() {
        let script = bridge_init_script(&state(), "https://one.filigran.io");
        assert!(script.contains(r#""features":["local_paths","attention","session"]"#));
        assert!(script.contains(r#""open_extensions":["txt","#));
        // The page offers Open only for what the shell will open.
        for active in ["exe", "xml", "odt", "svg", "html"] {
            assert!(!script.contains(&format!("\"{active}\"")), "{active}");
        }
    }

    #[test]
    fn init_script_defines_the_bridge_on_the_instance_origin_only() {
        let script = bridge_init_script(&state(), "https://one.filigran.io");
        assert!(script.contains(r#"window.location.origin !== "https://one.filigran.io""#));
        assert!(script.contains(r#""runner_id":"2f1c9d7a-0000-4000-8000-000000000000""#));
        assert!(script.contains(r#""server_url":"https://one.filigran.io""#));
        assert!(script.contains("__XTM_ONE_DESKTOP__"));
        assert!(script.contains("data-xtm-one-desktop"));
        // Frozen and non-writable: the page reads it, it never rewrites it.
        assert!(script.contains("Object.freeze"));
        assert!(script.contains("writable: false"));
    }

    #[test]
    fn init_script_never_carries_a_secret() {
        let script = bridge_init_script(&state(), "https://one.filigran.io");
        assert!(!script.contains("run-"));
        assert!(!script.contains("renr-"));
    }

    #[test]
    fn download_name_prefers_the_suggested_file_name() {
        let url = Url::parse("https://one.filigran.io/api/v1/files/abc/download").unwrap();
        assert_eq!(
            download_file_name(&url, Path::new("C:/Users/x/Downloads/report.pdf")),
            "report.pdf"
        );
        assert_eq!(download_file_name(&url, Path::new("")), "download");
        let named =
            Url::parse("https://one.filigran.io/dist/xtm-one-runner-linux-x64.AppImage").unwrap();
        assert_eq!(
            download_file_name(&named, Path::new("")),
            "xtm-one-runner-linux-x64.AppImage"
        );
    }

    #[test]
    fn download_name_is_sanitised_whatever_the_server_suggested() {
        // Separators and traversal never leave the Downloads folder: a name is
        // ONE path component, so what looked like a path becomes one file name.
        assert_eq!(
            sanitize_download_file_name("../../evil.sh"),
            ".._.._evil.sh"
        );
        assert_eq!(sanitize_download_file_name(".."), "download");
        assert_eq!(sanitize_download_file_name("."), "download");
        assert_eq!(sanitize_download_file_name("a/b\\c.txt"), "a_b_c.txt");
        // Windows device names, any case, with or without an extension.
        assert_eq!(sanitize_download_file_name("CON"), "_CON");
        assert_eq!(sanitize_download_file_name("nul.txt"), "_nul.txt");
        assert_eq!(sanitize_download_file_name("com1.log"), "_com1.log");
        assert_eq!(sanitize_download_file_name("console.txt"), "console.txt");
        // NTFS alternate data streams and the other refused characters.
        assert_eq!(
            sanitize_download_file_name("report:hidden.pdf"),
            "report_hidden.pdf"
        );
        assert_eq!(
            sanitize_download_file_name("a<b>c|d?e*f\"g.txt"),
            "a_b_c_d_e_f_g.txt"
        );
        // Control characters, trailing dots and spaces (Windows drops them).
        assert_eq!(
            sanitize_download_file_name("re\u{0}port\n.pdf"),
            "re_port_.pdf"
        );
        assert_eq!(sanitize_download_file_name("report.pdf. . "), "report.pdf");
        assert_eq!(sanitize_download_file_name("   "), "download");
        // Bounded, extension kept.
        let long = format!("{}.pdf", "x".repeat(400));
        let cut = sanitize_download_file_name(&long);
        assert!(cut.len() <= MAX_DOWNLOAD_NAME_LEN && cut.ends_with(".pdf"));
        // An oversized "extension" cannot smuggle the name past the budget.
        let long_ext = format!("a.{}", "y".repeat(300));
        let cut = sanitize_download_file_name(&long_ext);
        assert!(cut.len() <= MAX_DOWNLOAD_NAME_LEN, "{}", cut.len());
        assert!(cut.starts_with("a.yyy"));
        // Multi-byte text is cut on a character boundary.
        let wide = format!("{}.txt", "\u{00e9}".repeat(200));
        let cut = sanitize_download_file_name(&wide);
        assert!(cut.len() <= MAX_DOWNLOAD_NAME_LEN && cut.ends_with(".txt"));
        // The cut itself never leaves a trailing dot or space, nor a device
        // name that padding had hidden - the invariants hold AFTER the cut.
        let dot_at_the_cut = format!("{}.{}", "x".repeat(149), "y".repeat(300));
        let cut = sanitize_download_file_name(&dot_at_the_cut);
        assert!(
            cut.len() <= MAX_DOWNLOAD_NAME_LEN && !cut.ends_with('.'),
            "{cut}"
        );
        let padded_device = format!("CON{}.{}", " ".repeat(200), "z".repeat(300));
        assert_eq!(sanitize_download_file_name(&padded_device), "_CON");
        // 149 characters then a space: the cut lands exactly on the space,
        // which the second normalisation - and only it - removes.
        let space_at_the_cut = format!("{} .{}", "x".repeat(149), "z".repeat(300));
        assert_eq!(
            sanitize_download_file_name(&space_at_the_cut),
            "x".repeat(149)
        );
        // Ordinary names pass untouched.
        assert_eq!(
            sanitize_download_file_name("Q3 report (final).pdf"),
            "Q3 report (final).pdf"
        );
        assert_eq!(sanitize_download_file_name(".env"), ".env");
        assert_eq!(sanitize_download_file_name("..hidden"), "..hidden");
        // And the URL fallback goes through the same sanitiser.
        let url = Url::parse("https://one.filigran.io/api/v1/files/nul").unwrap();
        assert_eq!(download_file_name(&url, Path::new("")), "_nul");
    }

    #[test]
    fn navigation_stays_on_the_instance_origin_and_nowhere_else() {
        let origin = "https://one.filigran.io";
        let decide = |s: &str| navigation_decision(&Url::parse(s).unwrap(), origin);
        // The instance itself, any path, query or fragment; default port elided.
        assert_eq!(decide("https://one.filigran.io/chat"), Navigation::Allow);
        assert_eq!(
            decide("https://one.filigran.io:443/login#token=x"),
            Navigation::Allow
        );
        assert_eq!(decide("https://ONE.filigran.io/x?y=z"), Navigation::Allow);
        assert_eq!(decide("about:blank"), Navigation::Allow);
        // Another web page - the identity provider included - opens in the OS
        // browser and never renders inside the privileged webview.
        assert_eq!(
            decide("https://idp.example/authorize"),
            Navigation::OpenExternally
        );
        assert_eq!(
            decide("http://one.filigran.io/chat"),
            Navigation::OpenExternally
        );
        assert_eq!(
            decide("https://one.filigran.io:8443/chat"),
            Navigation::OpenExternally
        );
        assert_eq!(
            decide("https://one.filigran.io.evil.com/"),
            Navigation::OpenExternally
        );
        assert_eq!(
            decide("https://evil.com/one.filigran.io"),
            Navigation::OpenExternally
        );
        assert_eq!(
            decide("https://user@one.filigran.io@evil.com/"),
            Navigation::OpenExternally
        );
        // Nothing else loads or opens.
        assert_eq!(decide("file:///etc/passwd"), Navigation::Deny);
        assert_eq!(decide("javascript:alert(1)"), Navigation::Deny);
        assert_eq!(decide("data:text/html,hi"), Navigation::Deny);
        assert_eq!(decide("blob:https://one.filigran.io/abc"), Navigation::Deny);
        assert_eq!(decide("ms-msdt:/id"), Navigation::Deny);
        // A non-default port is part of the origin and must match exactly.
        let dev = "http://localhost:4000";
        let decide_dev = |s: &str| navigation_decision(&Url::parse(s).unwrap(), dev);
        assert_eq!(decide_dev("http://localhost:4000/chat"), Navigation::Allow);
        assert_eq!(
            decide_dev("http://localhost:5173/chat"),
            Navigation::OpenExternally
        );
    }

    #[test]
    fn the_bridge_grants_named_permissions_and_no_default_set() {
        // A `:default` set bundles more than its name says (the opener's also
        // grants `reveal_item_in_dir`, unscoped); the panel-only commands must
        // never be reachable from web content; and a local path reaches the
        // OS only through the shell's own validated commands - never
        // an `opener:` path permission.
        for permission in BRIDGE_PERMISSIONS {
            assert!(!permission.ends_with(":default"), "{permission}");
            for forbidden in ["enroll", "quit", "update", "autostart", "cdp", "get-state"] {
                assert!(!permission.contains(forbidden), "{permission}");
            }
            if let Some(opener) = permission.strip_prefix("opener:") {
                assert!(
                    ["allow-open-url", "allow-default-urls"].contains(&opener),
                    "{permission}"
                );
            }
        }
        assert!(BRIDGE_PERMISSIONS.contains(&"opener:allow-open-url"));
        assert!(BRIDGE_PERMISSIONS.contains(&"opener:allow-default-urls"));
        assert!(BRIDGE_PERMISSIONS.contains(&"allow-desktop-begin-browser-login"));
        for own in [
            "allow-desktop-open-local-path",
            "allow-desktop-reveal-local-path",
            "allow-desktop-request-attention",
            "allow-desktop-load-session",
            "allow-desktop-store-session",
        ] {
            assert!(BRIDGE_PERMISSIONS.contains(&own), "{own}");
        }
    }

    #[test]
    fn the_bridge_grant_is_exactly_this_list() {
        // The complete surface web content reaches, pinned: a new line here
        // is a decision a review must see, not a side effect of a new rule.
        assert_eq!(
            BRIDGE_PERMISSIONS,
            &[
                "core:event:allow-listen",
                "core:event:allow-unlisten",
                "allow-desktop-bridge-state",
                "allow-open-panel",
                "allow-desktop-begin-browser-login",
                "allow-desktop-cancel-browser-login",
                "opener:allow-open-url",
                "opener:allow-default-urls",
                "allow-desktop-open-local-path",
                "allow-desktop-reveal-local-path",
                "allow-desktop-request-attention",
                "allow-desktop-load-session",
                "allow-desktop-store-session",
            ]
        );
        // The local panel never gets the chat's local-path, attention or
        // session commands, nor any opener path command.
        let panel = include_str!("../capabilities/default.json");
        for absent in [
            "local-path",
            "request-attention",
            "-session",
            "open-path",
            "reveal-item",
            "opener:default",
        ] {
            assert!(!panel.contains(absent), "{absent}");
        }
    }

    #[test]
    fn every_bridge_command_is_declared_to_the_build() {
        // An `allow-<command>` permission only exists for a command listed in
        // build.rs `APP_COMMANDS`; a missing line there fails the capability
        // at runtime, long after this compiles.
        let build = include_str!("../build.rs");
        for permission in BRIDGE_PERMISSIONS {
            if let Some(command) = permission.strip_prefix("allow-") {
                let name = command.replace('-', "_");
                assert!(build.contains(&format!("\"{name}\"")), "{permission}");
            }
        }
    }

    #[test]
    fn the_chat_url_keeps_an_instance_path_prefix() {
        assert_eq!(
            chat_url("https://one.filigran.io"),
            "https://one.filigran.io/chat"
        );
        assert_eq!(
            chat_url("https://one.filigran.io/"),
            "https://one.filigran.io/chat"
        );
        assert_eq!(
            chat_url("https://corp.example/xtm"),
            "https://corp.example/xtm/chat"
        );
        assert_eq!(
            chat_url("http://localhost:4000"),
            "http://localhost:4000/chat"
        );
    }

    #[test]
    fn a_second_launch_at_login_stays_quiet() {
        let argv = |a: &[&str]| a.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        assert!(second_launch_is_autostart(&argv(&[
            "xtm-one-runner",
            "--autostarted"
        ])));
        assert!(!second_launch_is_autostart(&argv(&["xtm-one-runner"])));
        assert!(!second_launch_is_autostart(&argv(&[])));
    }

    #[test]
    fn unique_download_path_never_overwrites() {
        let dir = std::env::temp_dir().join(format!("xtm-desktop-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let first = unique_download_path(&dir, "notes.txt");
        assert_eq!(first, dir.join("notes.txt"));
        std::fs::write(&first, b"x").unwrap();
        let second = unique_download_path(&dir, "notes.txt");
        assert_eq!(second, dir.join("notes (1).txt"));
        std::fs::write(&second, b"x").unwrap();
        assert_eq!(
            unique_download_path(&dir, "notes.txt"),
            dir.join("notes (2).txt")
        );
        assert_eq!(unique_download_path(&dir, "README"), dir.join("README"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn browser_login_only_opens_the_instances_own_sso_routes() {
        let id = "d".repeat(43);
        assert_eq!(
            browser_login_url("https://one.filigran.io/", "oidc", &id),
            Some(format!(
                "https://one.filigran.io/api/auth/oidc/login?desktop_login={id}"
            ))
        );
        assert_eq!(
            browser_login_url("https://one.filigran.io", "saml", &id),
            Some(format!(
                "https://one.filigran.io/api/auth/saml/login?desktop_login={id}"
            ))
        );
        // Web content cannot steer the browser anywhere else through the shell.
        assert_eq!(
            browser_login_url("https://one.filigran.io", "google", &id),
            None
        );
        assert_eq!(
            browser_login_url("https://one.filigran.io", "../evil", &id),
            None
        );
    }

    #[test]
    fn panel_sections_are_an_allow_list() {
        // `open_panel` is remote-callable: an arbitrary section string from
        // web content must land on the overview, never on onboarding (which
        // is the enrollment form) or an unknown page.
        for (input, expected) in [
            (Some("overview"), "overview"),
            (Some("settings"), "settings"),
            (Some("about"), "about"),
            (Some("onboarding"), "overview"),
            (Some("<script>"), "overview"),
            (None, "overview"),
        ] {
            assert_eq!(remote_panel_section(input), expected);
        }
    }

    fn menu_actions(layout: &[SubmenuLayout]) -> Vec<MenuAction> {
        layout
            .iter()
            .flat_map(|submenu| &submenu.entries)
            .filter_map(|entry| match entry {
                MenuEntry::Action { action, .. } => Some(*action),
                _ => None,
            })
            .collect()
    }

    #[test]
    fn menu_actions_are_named_by_distinct_ids() {
        for action in MenuAction::ALL {
            assert_eq!(MenuAction::from_id(action.id()), Some(action));
        }
        let mut ids: Vec<&str> = MenuAction::ALL.iter().map(|a| a.id()).collect();
        ids.sort_unstable();
        ids.dedup();
        assert_eq!(ids.len(), MenuAction::ALL.len());
        // The tray's header and status lines run nothing.
        for inert in ["header", "status", ""] {
            assert_eq!(MenuAction::from_id(inert), None, "{inert}");
        }
    }

    #[test]
    fn the_application_menu_reaches_what_the_tray_reaches() {
        for macos in [true, false] {
            let actions = menu_actions(&app_menu_layout(macos));
            for action in [
                MenuAction::ConnectionPanel,
                MenuAction::Settings,
                MenuAction::About,
                MenuAction::OpenInBrowser,
                MenuAction::CheckForUpdates,
                MenuAction::Quit,
            ] {
                let count = actions.iter().filter(|a| **a == action).count();
                assert_eq!(count, 1, "{action:?} (macOS: {macos})");
            }
        }
        // On Windows and Linux the menu is the chat window's own; the macOS
        // menu bar also shows over the panel, with the chat closed to the tray.
        assert!(menu_actions(&app_menu_layout(true)).contains(&MenuAction::OpenChat));
        assert!(!menu_actions(&app_menu_layout(false)).contains(&MenuAction::OpenChat));
    }

    #[test]
    fn shortcuts_and_native_items_are_macos_only() {
        for submenu in app_menu_layout(false) {
            for entry in &submenu.entries {
                assert!(
                    matches!(
                        entry,
                        MenuEntry::Action { shortcut: None, .. } | MenuEntry::Separator
                    ),
                    "{} > {entry:?}",
                    submenu.title
                );
            }
        }
        let shortcut = |wanted: MenuAction| {
            app_menu_layout(true)
                .into_iter()
                .flat_map(|submenu| submenu.entries)
                .find_map(|entry| match entry {
                    MenuEntry::Action {
                        action, shortcut, ..
                    } if action == wanted => Some(shortcut),
                    _ => None,
                })
                .flatten()
        };
        assert_eq!(shortcut(MenuAction::Settings), Some("CmdOrCtrl+,"));
        assert_eq!(shortcut(MenuAction::Quit), Some("CmdOrCtrl+Q"));
    }

    #[test]
    fn the_macos_menu_keeps_what_a_mac_app_relies_on() {
        let layout = app_menu_layout(true);
        let natives: Vec<NativeItem> = layout
            .iter()
            .flat_map(|submenu| &submenu.entries)
            .filter_map(|entry| match entry {
                MenuEntry::Native(item) => Some(*item),
                _ => None,
            })
            .collect();
        // The Edit items carry the chat's clipboard shortcuts; Close Window
        // and Hide carry Cmd+W and Cmd+H.
        for item in [
            NativeItem::Undo,
            NativeItem::Redo,
            NativeItem::Cut,
            NativeItem::Copy,
            NativeItem::Paste,
            NativeItem::SelectAll,
            NativeItem::CloseWindow,
            NativeItem::Hide,
        ] {
            assert!(natives.contains(&item), "{item:?}");
        }
        assert_eq!(layout[0].title, PRODUCT_NAME);
        let roles: Vec<&str> = layout.iter().filter_map(|submenu| submenu.id).collect();
        assert_eq!(roles, [WINDOW_SUBMENU_ID, HELP_SUBMENU_ID]);
    }

    #[test]
    fn a_restored_chat_lands_on_its_page_only_on_the_instance() {
        let start = |page: Option<&str>| {
            chat_start_url("https://one.filigran.io", page)
                .unwrap()
                .to_string()
        };
        assert_eq!(start(None), "https://one.filigran.io/chat");
        assert_eq!(
            start(Some("https://one.filigran.io/chat/3f2a?panel=files#m-12")),
            "https://one.filigran.io/chat/3f2a?panel=files#m-12"
        );
        // The origin as the navigation policy reads it: neither the default
        // port nor the host's case is part of it.
        assert_eq!(
            start(Some("https://ONE.filigran.io:443/agents")),
            "https://one.filigran.io/agents"
        );
        for foreign in [
            "https://one.filigran.io.evil.com/chat",
            "https://one.filigran.io./chat",
            "http://one.filigran.io/chat",
            "https://one.filigran.io:8443/chat",
            "https://one.filigran.io@evil.com/chat",
            r"https:\\evil.com\one.filigran.io",
            // Credentials the webview would send with its first request.
            "https://evil@one.filigran.io/chat",
            "https://evil:secret@one.filigran.io/chat",
            "blob:https://one.filigran.io/3f2a",
            "about:blank",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "data:text/html,hi",
            "/chat/3f2a",
            "not a url",
            "",
        ] {
            assert_eq!(
                start(Some(foreign)),
                "https://one.filigran.io/chat",
                "{foreign}"
            );
        }
        // An instance served under a path prefix keeps it.
        assert_eq!(
            chat_start_url(
                "https://corp.example/xtm",
                Some("https://corp.example/xtm/agents")
            )
            .unwrap()
            .as_str(),
            "https://corp.example/xtm/agents"
        );
        assert_eq!(
            chat_start_url("https://corp.example/xtm/", None)
                .unwrap()
                .as_str(),
            "https://corp.example/xtm/chat"
        );
    }

    #[test]
    fn a_restored_page_is_one_the_navigation_policy_loads() {
        // The record may name only a page the window would load anyway (bar
        // `about:`, which is no page to come back to).
        for base in ["https://one.filigran.io", "https://b\u{fc}cher.example"] {
            let fallback = chat_start_url(base, None).unwrap();
            let origin = fallback.origin().ascii_serialization();
            for page in [
                "https://one.filigran.io/chat/3f2a",
                "https://ONE.filigran.io:443/agents",
                "https://one.filigran.io./agents",
                "https://one.filigran.io:8443/agents",
                "http://one.filigran.io/agents",
                "https://xn--bcher-kva.example/agents",
                "https://B\u{dc}CHER.example/agents",
                "https://b\u{fc}cher.example.evil.com/agents",
                "blob:https://one.filigran.io/3f2a",
                "about:blank",
            ] {
                let url = Url::parse(page).unwrap();
                let loads = url.scheme() != "about"
                    && navigation_decision(&url, &origin) == Navigation::Allow;
                let restored = chat_start_url(base, Some(page)).unwrap() != fallback;
                assert_eq!(restored, loads, "{base} <- {page}");
            }
        }
    }

    #[test]
    fn an_open_windows_record_expires() {
        let record = OpenWindows {
            saved_at: 1_000,
            ..Default::default()
        };
        let max = OPEN_WINDOWS_MAX_AGE.as_secs();
        assert!(record.is_fresh(1_000));
        assert!(record.is_fresh(1_000 + max));
        assert!(!record.is_fresh(1_000 + max + 1));
        // Saved "in the future" (a clock set back since): not trusted.
        assert!(!record.is_fresh(999));
        // A record without a timestamp never counts.
        assert!(!OpenWindows::default().is_fresh(unix_now()));
    }

    #[test]
    fn an_open_windows_record_is_good_for_one_restart() {
        let dir =
            std::env::temp_dir().join(format!("xtm-desktop-restore-test-{}", std::process::id()));
        let path = dir.join("open-windows.json");
        let windows = OpenWindows {
            saved_at: 1_000,
            chat: true,
            chat_page: Some("https://one.filigran.io/chat/3f2a".into()),
            chat_maximized: true,
            panel: false,
        };
        save_open_windows(&path, &windows).unwrap();
        assert_eq!(take_open_windows(&path, 1_030), Some(windows));
        // Consumed: the next launch finds nothing.
        assert!(!path_is_taken(&path));
        assert_eq!(take_open_windows(&path, 1_031), None);
        // A stale or unreadable record is dropped as well.
        let stale = OpenWindows {
            saved_at: 1_000,
            chat: true,
            ..Default::default()
        };
        save_open_windows(&path, &stale).unwrap();
        let late = 1_000 + OPEN_WINDOWS_MAX_AGE.as_secs() + 1;
        assert_eq!(take_open_windows(&path, late), None);
        assert!(!path_is_taken(&path));
        std::fs::write(&path, b"{not json").unwrap();
        assert_eq!(take_open_windows(&path, 1_000), None);
        assert!(!path_is_taken(&path));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn an_open_windows_record_counts_only_once_it_is_gone() {
        let fresh = br#"{"saved_at":1000,"chat":true}"#.to_vec();
        let denied = || std::io::Error::from(std::io::ErrorKind::PermissionDenied);
        assert!(accept_open_windows(Ok(Some(fresh.clone())), Ok(()), 1_000).is_some_and(|w| w.chat));
        // Read but still on disk: it would reopen the windows at every launch.
        assert_eq!(
            accept_open_windows(Ok(Some(fresh)), Err(denied()), 1_000),
            None
        );
        // Deleted, but unreadable, oversized or not a plain file.
        assert_eq!(accept_open_windows(Err(denied()), Ok(()), 1_000), None);
        assert_eq!(accept_open_windows(Ok(None), Ok(()), 1_000), None);
        // A wrong type is refused; a field a later build adds is not.
        let wrong = br#"{"saved_at":1000,"chat":"yes"}"#.to_vec();
        assert_eq!(accept_open_windows(Ok(Some(wrong)), Ok(()), 1_000), None);
        let later = br#"{"saved_at":1000,"chat":true,"tabs":3}"#.to_vec();
        assert!(accept_open_windows(Ok(Some(later)), Ok(()), 1_000).is_some_and(|w| w.chat));
    }

    #[test]
    fn an_open_windows_record_must_be_a_small_plain_file() {
        let dir =
            std::env::temp_dir().join(format!("xtm-desktop-restore-bounds-{}", std::process::id()));
        let path = dir.join("open-windows.json");
        // Oversized: dropped and deleted, however fresh it claims to be.
        let huge = OpenWindows {
            saved_at: 1_000,
            chat: true,
            chat_page: Some(format!(
                "https://one.filigran.io/chat?q={}",
                "x".repeat(OPEN_WINDOWS_MAX_BYTES as usize)
            )),
            ..Default::default()
        };
        save_open_windows(&path, &huge).unwrap();
        assert_eq!(take_open_windows(&path, 1_000), None);
        assert!(!path_is_taken(&path));
        // A directory in its place is never honoured.
        std::fs::create_dir(&path).unwrap();
        assert_eq!(take_open_windows(&path, 1_000), None);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn an_open_windows_record_is_never_read_through_a_link() {
        let dir =
            std::env::temp_dir().join(format!("xtm-desktop-restore-link-{}", std::process::id()));
        let target = dir.join("elsewhere.json");
        let fresh = OpenWindows {
            saved_at: 1_000,
            chat: true,
            ..Default::default()
        };
        save_open_windows(&target, &fresh).unwrap();
        let path = dir.join("open-windows.json");
        std::os::unix::fs::symlink(&target, &path).unwrap();
        assert_eq!(take_open_windows(&path, 1_000), None);
        // The link is consumed; what it pointed at is left alone.
        assert!(!path_is_taken(&path));
        assert!(path_is_taken(&target));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_record_is_written_before_the_install_and_taken_back_on_failure() {
        let dir =
            std::env::temp_dir().join(format!("xtm-desktop-install-test-{}", std::process::id()));
        let path = dir.join("open-windows.json");
        let windows = OpenWindows {
            saved_at: 1_000,
            chat: true,
            ..Default::default()
        };
        // On Windows the installer ends the process: the record is on disk
        // before the install starts, and a successful install leaves it for
        // the restart.
        let installed = install_with_record(Some(&path), &windows, || {
            assert!(path_is_taken(&path));
            Ok(())
        });
        assert_eq!(installed, Ok(()));
        assert!(path_is_taken(&path));
        // A failed install takes it back: no later launch reopens the windows.
        let failed = install_with_record(Some(&path), &windows, || Err("disk full".to_string()));
        assert_eq!(failed, Err("disk full".to_string()));
        assert!(!path_is_taken(&path));
        // Without a config directory nothing is recorded; the install runs.
        let mut ran = false;
        let installed = install_with_record(None, &windows, || {
            ran = true;
            Ok(())
        });
        assert_eq!(installed, Ok(()));
        assert!(ran);
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Bound on a wait a passing test never comes near: only a broken build
    /// (an install run inline, a restart that never comes) reaches it.
    const TEST_WAIT: Duration = Duration::from_secs(30);

    /// The update lock `apply_update` holds, taken.
    fn taken_update_lock() -> (
        std::sync::Arc<tokio::sync::Mutex<()>>,
        tokio::sync::OwnedMutexGuard<()>,
    ) {
        let lock = std::sync::Arc::new(tokio::sync::Mutex::new(()));
        let updating = lock.clone().try_lock_owned().unwrap();
        (lock, updating)
    }

    #[test]
    fn an_install_waiting_on_a_prompt_leaves_the_runtime_free() {
        // One worker: an install run on it would hold it for the whole prompt,
        // and the task that answers the prompt would never get to run.
        let runtime = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        let (answer, prompt) = std::sync::mpsc::channel::<()>();
        let install = move || {
            prompt
                .recv_timeout(TEST_WAIT)
                .map_err(|_| "the runtime was held while the prompt was open".to_string())
        };
        let (installed, ()) = runtime.block_on(async {
            tokio::join!(install_off_runtime(install), async move {
                let _ = answer.send(());
            })
        });
        assert_eq!(installed, Ok(()));
    }

    #[test]
    fn a_cancelled_wait_keeps_the_update_lock_until_the_install_ends() {
        let (lock, updating) = taken_update_lock();
        let (answer, prompt) = std::sync::mpsc::channel::<()>();
        let (restarting, restarted) = std::sync::mpsc::channel::<()>();
        let wait = install_then_restart(
            updating,
            move || {
                // Dismissed: the install fails once the prompt closes.
                let _ = prompt.recv();
                Err("the package installation failed".to_string())
            },
            move || {
                let _ = restarting.send(());
            },
        );
        // The command waiting on it goes away while the prompt is open.
        assert!(futures_util::FutureExt::now_or_never(wait).is_none());
        assert!(
            lock.try_lock().is_err(),
            "a second install could start while the first one still runs"
        );
        answer.send(()).unwrap();
        // The failed install frees the lock for the next attempt, and
        // restarts nothing.
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .unwrap();
        let free =
            runtime.block_on(async { tokio::time::timeout(TEST_WAIT, lock.lock()).await.is_ok() });
        assert!(free, "the update lock outlived the install");
        assert!(restarted.try_recv().is_err());
    }

    #[test]
    fn a_successful_install_restarts_even_when_nobody_waits() {
        let (lock, updating) = taken_update_lock();
        let (answer, prompt) = std::sync::mpsc::channel::<()>();
        let (restarting, restarted) = std::sync::mpsc::channel::<bool>();
        let held = lock.clone();
        let wait = install_then_restart(
            updating,
            move || {
                let _ = prompt.recv();
                Ok(())
            },
            // In the app the restart never returns: the process restarts.
            move || {
                let _ = restarting.send(held.try_lock().is_err());
            },
        );
        assert!(futures_util::FutureExt::now_or_never(wait).is_none());
        answer.send(()).unwrap();
        // The new build is installed: the app restarts into it with the lock
        // still held, rather than keep running the old build, which would
        // install the same update again at its next check.
        assert_eq!(restarted.recv_timeout(TEST_WAIT), Ok(true));
    }

    #[test]
    fn a_dismissed_prompt_still_reports_the_install_error() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        let (_lock, updating) = taken_update_lock();
        let (restarting, restarted) = std::sync::mpsc::channel::<()>();
        let installed = runtime.block_on(install_then_restart(
            updating,
            || Err("the package installation failed".to_string()),
            move || {
                let _ = restarting.send(());
            },
        ));
        assert_eq!(
            installed,
            Err("the package installation failed".to_string())
        );
        assert!(restarted.try_recv().is_err(), "a failed install restarted");
    }

    #[test]
    fn a_quit_during_an_install_waits_for_the_install_to_end() {
        // No install running: the app quits at once.
        assert!(!InstallGate::default().hold_quit());
        let gate = InstallGate::default();
        assert!(gate.begin());
        // A system password prompt may be open: the quit waits.
        assert!(gate.hold_quit());
        assert!(gate.end(), "the held quit is due once the install ends");
        assert!(!gate.end(), "and it is carried out once");
        assert!(!gate.hold_quit());
    }

    #[test]
    fn a_held_quit_goes_through_at_its_limit_when_the_install_never_ends() {
        let held = std::cell::Cell::new(false);
        // No install running: the app quits at once, nothing is held.
        let (quitting, quit) = std::sync::mpsc::channel::<()>();
        hold_or_quit(
            &InstallGate::default(),
            TEST_WAIT,
            || held.set(true),
            move || {
                let _ = quitting.send(());
            },
        );
        assert_eq!(quit.try_recv(), Ok(()));
        assert!(!held.get());
        // A password prompt nobody answers: the install never ends, and the
        // quit waits for it only until the limit.
        let gate = InstallGate::default();
        assert!(gate.begin());
        let (quitting, quit) = std::sync::mpsc::channel::<()>();
        hold_or_quit(
            &gate,
            Duration::from_millis(20),
            || held.set(true),
            move || {
                let _ = quitting.send(());
            },
        );
        assert!(held.get(), "the panel says why the quit waits");
        assert_eq!(
            quit.recv_timeout(TEST_WAIT),
            Ok(()),
            "a quit held by an install that never ends never went through"
        );
    }

    #[test]
    fn an_install_does_not_start_once_a_quit_went_through() {
        let gate = InstallGate::default();
        // A quit with no install running: the app is on its way out.
        assert!(!gate.hold_quit());
        let ran = std::cell::Cell::new(false);
        let installed = gated_install(
            &gate,
            || {
                ran.set(true);
                Ok(())
            },
            || {},
        );
        assert!(installed.is_err());
        assert!(
            !ran.get(),
            "the install would outlive the event loop it dispatches to"
        );
    }

    #[test]
    fn a_quit_held_during_a_failed_install_is_carried_out_after_it() {
        let gate = InstallGate::default();
        let order = std::sync::Mutex::new(Vec::new());
        let installed = gated_install(
            &gate,
            || {
                // Quit while the prompt is open, then dismiss the prompt.
                assert!(gate.hold_quit());
                order.lock().unwrap().push("install ended");
                Err("the package installation failed".to_string())
            },
            || order.lock().unwrap().push("quit"),
        );
        assert_eq!(
            installed,
            Err("the package installation failed".to_string())
        );
        assert_eq!(*order.lock().unwrap(), ["install ended", "quit"]);
        // The app is quitting now: an update queued behind the lock must not
        // start while its event loop goes away.
        let ran = std::cell::Cell::new(false);
        let queued = gated_install(
            &gate,
            || {
                ran.set(true);
                Ok(())
            },
            || {},
        );
        assert!(queued.is_err());
        assert!(!ran.get());
        // Without a quit, a failed install leaves the app running.
        let gate = InstallGate::default();
        let quit = std::cell::Cell::new(false);
        let _ = gated_install(&gate, || Err("dismissed".into()), || quit.set(true));
        assert!(!quit.get());
    }

    #[test]
    fn a_privileged_move_is_predicted_the_way_the_updater_decides_it() {
        let own = MoveAccess {
            bundle_writable: true,
            parent_writable: true,
            parent_sticky: false,
            bundle_owned: true,
            parent_owned: false,
            superuser: false,
            immutable: false,
        };
        assert!(!move_needs_administrator(own));
        // rename(2) moves an entry out of a sticky folder for the owner of the
        // entry, the owner of the folder, or the superuser.
        for allowed in [
            MoveAccess {
                parent_sticky: true,
                ..own
            },
            MoveAccess {
                parent_sticky: true,
                bundle_owned: false,
                parent_owned: true,
                ..own
            },
            MoveAccess {
                parent_sticky: true,
                bundle_owned: false,
                superuser: true,
                ..own
            },
        ] {
            assert!(!move_needs_administrator(allowed), "{allowed:?}");
        }
        for access in [
            MoveAccess {
                bundle_writable: false,
                ..own
            },
            MoveAccess {
                parent_writable: false,
                ..own
            },
            // rename(2) refuses another owner's entry of a sticky folder the
            // user does not own either, and an immutable or append-only entry.
            MoveAccess {
                parent_sticky: true,
                bundle_owned: false,
                ..own
            },
            MoveAccess {
                immutable: true,
                ..own
            },
        ] {
            assert!(move_needs_administrator(access), "{access:?}");
        }
    }

    #[test]
    fn a_quit_held_during_a_successful_install_replaces_the_restart() {
        let gate = InstallGate::default();
        let quit = std::cell::Cell::new(false);
        let installed = gated_install(
            &gate,
            || {
                assert!(gate.hold_quit());
                Ok(())
            },
            || quit.set(true),
        );
        assert_eq!(installed, Ok(()));
        assert!(!quit.get(), "the quit waits for the restart decision");
        assert_eq!(restart_or_quit(&gate, || "restart", || "quit"), "quit");
        // With no quit held, the new build starts.
        let gate = InstallGate::default();
        assert_eq!(gated_install(&gate, || Ok(()), || quit.set(true)), Ok(()));
        assert_eq!(restart_or_quit(&gate, || "restart", || "quit"), "restart");
        assert!(!quit.get());
    }

    #[test]
    fn an_install_the_updater_would_panic_on_is_refused_first() {
        // Windows: the installer gets the app's arguments minus the first.
        assert!(check_installer_args(&[]).is_err());
        assert!(check_installer_args(&["xtm-one-runner.exe".into()]).is_ok());
        // macOS: a privileged move built from a path that breaks its quoting.
        let temp = Path::new("/var/folders/ab/T");
        for bundle in [
            "/Applications/Sam's Apps/XTM One Desktop.app",
            "/Applications/A\"B.app",
            "/Applications/A\\B.app",
        ] {
            assert!(
                check_privileged_move(Path::new(bundle), temp).is_err(),
                "{bundle}"
            );
        }
        let bundle = Path::new("/Applications/XTM One Desktop.app");
        assert!(check_privileged_move(bundle, Path::new("/tmp/it's")).is_err());
        assert!(check_privileged_move(bundle, temp).is_ok());
    }

    #[test]
    fn the_linux_updater_replaces_an_executable_it_can_find_a_folder_for() {
        let exe = || Some(PathBuf::from("/usr/bin/xtm-one-runner"));
        // An APPIMAGE the updater's temp-dir fallback would panic on.
        for broken in ["", "/"] {
            let broken = Some(std::ffi::OsStr::new(broken));
            assert_eq!(
                updater_executable_override(broken, exe()),
                Ok(exe()),
                "{broken:?}"
            );
            // Nothing to stand in for it: no updater, rather than one that
            // keeps the path it would panic on.
            assert!(
                updater_executable_override(broken, None).is_err(),
                "{broken:?}"
            );
        }
        // A real AppImage, or none: the updater's own choice stands.
        let appimage = std::ffi::OsStr::new("/home/sam/XTM One Desktop.AppImage");
        assert_eq!(updater_executable_override(Some(appimage), exe()), Ok(None));
        assert_eq!(updater_executable_override(Some(appimage), None), Ok(None));
        assert_eq!(updater_executable_override(None, exe()), Ok(None));
    }

    #[test]
    fn the_app_bundle_is_found_the_way_the_updater_finds_it() {
        assert_eq!(
            app_bundle_of(Path::new(
                "/Applications/XTM One Desktop.app/Contents/MacOS/xtm-one-runner"
            )),
            Some(PathBuf::from("/Applications/XTM One Desktop.app"))
        );
        assert_eq!(
            app_bundle_of(Path::new("/usr/local/bin/xtm-one-runner")),
            Some(PathBuf::from("/usr/local/bin"))
        );
    }

    #[test]
    fn the_panel_says_why_a_quit_waits_and_that_macos_will_ask() {
        let panel = include_str!("../ui/app.js");
        for event in [QUIT_HELD_EVENT, UPDATE_APPROVAL_EVENT] {
            assert!(panel.contains(&format!("listen(\"{event}\"")), "{event}");
        }
    }

    /// How many times `code` holds `ident` as a whole identifier.
    fn identifier_count(code: &str, ident: &str) -> usize {
        let is_ident = |c: char| c.is_alphanumeric() || c == '_';
        code.match_indices(ident)
            .filter(|(at, _)| {
                !code[..*at].chars().next_back().is_some_and(is_ident)
                    && !code[at + ident.len()..]
                        .chars()
                        .next()
                        .is_some_and(is_ident)
            })
            .count()
    }

    #[test]
    fn one_listener_serves_every_menu() {
        // Tauri hands every menu click to every global menu listener, tray
        // menu included, and to every window's own listener: a second
        // registration anywhere would run each action twice (two browser tabs
        // for one "Open in browser"). Tauri keeps its listener list private,
        // so the registrations are counted in the source: every file, any
        // spelling of the call, comments aside.
        let needle = ["on_menu", "_event"].concat();
        let mut registrations = Vec::new();
        let mut dirs = vec![PathBuf::from(concat!(env!("CARGO_MANIFEST_DIR"), "/src"))];
        while let Some(dir) = dirs.pop() {
            for entry in std::fs::read_dir(&dir).unwrap() {
                let path = entry.unwrap().path();
                if path.is_dir() {
                    dirs.push(path);
                    continue;
                }
                if path.extension().is_none_or(|ext| ext != "rs") {
                    continue;
                }
                let source = std::fs::read_to_string(&path).unwrap();
                for (n, line) in source.lines().enumerate() {
                    let code = line.split("//").next().unwrap_or_default();
                    for _ in 0..identifier_count(code, &needle) {
                        registrations.push(format!("{}:{}", path.display(), n + 1));
                    }
                }
            }
        }
        assert_eq!(registrations.len(), 1, "{registrations:?}");
    }

    #[test]
    fn identifiers_are_counted_whole() {
        assert_eq!(identifier_count("w.emit_to(PANEL, e, ())", "emit_to"), 1);
        assert_eq!(
            identifier_count("Emitter::emit_to (w, t, e, p)", "emit_to"),
            1
        );
        assert_eq!(identifier_count("remit_to(); emit_tos()", "emit_to"), 0);
    }

    #[test]
    fn only_package_installs_wait_for_a_click_to_update() {
        use tauri::utils::config::BundleType;

        // pkexec / a password dialog every cycle: announce, never install.
        assert!(!auto_install_allowed(Some(BundleType::Deb)));
        assert!(!auto_install_allowed(Some(BundleType::Rpm)));
        // Everything else keeps updating unattended, an unpatched build too.
        for bundle in [
            BundleType::AppImage,
            BundleType::Nsis,
            BundleType::Msi,
            BundleType::App,
            BundleType::Dmg,
        ] {
            assert!(auto_install_allowed(Some(bundle)));
        }
        assert!(auto_install_allowed(None));
    }

    #[test]
    fn a_package_install_only_takes_its_own_package() {
        use tauri::utils::config::BundleType;

        let dist = |name: &str| {
            Url::parse(&format!(
                "https://one.filigran.io/api/v1/runners/dist/{name}"
            ))
            .unwrap()
        };
        // The instance publishes the install's own package.
        assert!(update_installs_on(
            Some(BundleType::Deb),
            &dist("xtm-one-runner-linux-x64.deb")
        ));
        assert!(update_installs_on(
            Some(BundleType::Rpm),
            &dist("xtm-one-runner-linux-arm64.rpm")
        ));
        // It publishes none (an older instance): the updater
        // falls back to the AppImage, which install_deb / install_rpm refuse
        // only after the whole download.
        for bundle in [BundleType::Deb, BundleType::Rpm] {
            assert!(!update_installs_on(
                Some(bundle),
                &dist("xtm-one-runner-linux-x64.AppImage")
            ));
        }
        assert!(!update_installs_on(
            Some(BundleType::Rpm),
            &dist("xtm-one-runner-linux-x64.deb")
        ));
        // Every other bundle takes what the updater resolved, as before.
        for bundle in [
            Some(BundleType::AppImage),
            Some(BundleType::Nsis),
            Some(BundleType::App),
            None,
        ] {
            assert!(update_installs_on(
                bundle,
                &dist("xtm-one-runner-linux-x64.AppImage")
            ));
        }
    }
}
