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

use tauri_build::{AppManifest, Attributes, WindowsAttributes};

/// Every IPC command the app exposes, listed so Tauri generates an
/// `allow-<command>` / `deny-<command>` permission pair for each.
///
/// Without this manifest app commands have no permission identity: they are
/// reachable from ANY local window and from no remote origin. With it, every
/// window - the local connection panel included - is granted its commands
/// explicitly in a capability (`capabilities/default.json`), and the chat
/// window, which loads the enrolled instance's web app (a remote origin known
/// only at enrollment time), can be handed the read-only bridge subset at
/// runtime and nothing else. Keep in lockstep with `generate_handler!` in
/// `gui.rs`.
const APP_COMMANDS: &[&str] = &[
    "get_state",
    "enroll",
    "unenroll",
    "set_auto_update",
    "set_cdp_url",
    "set_autostart",
    "open_instance",
    "open_chat",
    "check_for_update",
    "install_update",
    "quit",
    "desktop_bridge_state",
    "open_panel",
    "desktop_begin_browser_login",
    "desktop_cancel_browser_login",
    "desktop_open_local_path",
    "desktop_reveal_local_path",
    "desktop_request_attention",
    "desktop_load_session",
    "desktop_store_session",
];

fn main() {
    // `tauri_build` embeds the app icon + Windows manifest and prepares
    // `tauri::generate_context!`. It reads the `cargo:dev` link instruction that
    // the `tauri` crate emits, so it MUST only run when the `gui` feature (and
    // therefore the `tauri` dependency) is active - otherwise it panics with
    // "missing `cargo:dev` instruction" on a headless (`--no-default-features`)
    // build. Cargo exposes the active features to the build script as
    // `CARGO_FEATURE_<NAME>` env vars.
    if std::env::var_os("CARGO_FEATURE_GUI").is_some() {
        // The Visual C++ runtime is linked INTO the binary: a Windows machine
        // without the Visual C++ Redistributable cannot start one that imports
        // vcruntime140.dll, nor update itself out of it. Spelled out
        // rather than left to Tauri's default, which moved once already: the
        // CLI used to ask for it, and stopped in 2.12. CI refuses a Windows
        // binary that imports the runtime (windows/assert-static-vcruntime.ps1).
        let attributes = Attributes::new()
            .app_manifest(AppManifest::new().commands(APP_COMMANDS))
            .windows_attributes(WindowsAttributes::new().static_vc_runtime(true));
        tauri_build::try_build(attributes).expect("failed to run tauri-build");
    }
}
