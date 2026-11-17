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

//! OS permission preflight for capture + synthetic input.
//!
//! macOS gates screen capture behind the **Screen Recording** TCC permission
//! and synthetic mouse/keyboard behind **Accessibility**. Without them the OS
//! does not error - `xcap` returns a black / wallpaper-only frame and `enigo`
//! silently no-ops. For audit evidence that is the worst possible failure: a
//! "successful" capture that proves nothing. We preflight and return a crisp,
//! actionable message instead, and raise the one-time system prompt so the user
//! lands on the right Privacy pane.
//!
//! Windows and Linux have no equivalent per-app consent gate for a process
//! already running in the user's interactive session, so `ensure` is a no-op
//! there (it returns `Ok`). The Linux capture path surfaces its own
//! Wayland/X11 hint on failure (see `screenshot.rs`).

/// A TCC-gated capability the runner needs before it can produce genuine
/// evidence.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Permission {
    ScreenRecording,
    Accessibility,
}

impl Permission {
    // Only consulted by the macOS message builder; on other targets the enum is
    // still constructed at the call sites but the label is never formatted.
    #[cfg_attr(not(target_os = "macos"), allow(dead_code))]
    fn label(self) -> &'static str {
        match self {
            Permission::ScreenRecording => "Screen Recording",
            Permission::Accessibility => "Accessibility",
        }
    }
}

/// `Ok(())` when the permission is granted (or not applicable on this OS), or
/// `Err(message)` with an operator-facing instruction when it must be granted.
pub fn ensure(permission: Permission) -> Result<(), String> {
    ensure_impl(permission)
}

#[cfg(target_os = "macos")]
fn ensure_impl(permission: Permission) -> Result<(), String> {
    let granted = match permission {
        Permission::ScreenRecording => macos::has_screen_recording(),
        Permission::Accessibility => macos::is_trusted(),
    };
    if granted {
        return Ok(());
    }
    // Raise the one-time system prompt for Screen Recording (a no-op if the user
    // has already answered); it opens the right pane so approval is one click.
    // Accessibility's prompt is raised by the OS on the first synthetic-input
    // attempt, so we only need the actionable message there.
    if permission == Permission::ScreenRecording {
        macos::request_screen_recording();
    }
    Err(format!(
        "macOS has not granted '{label}' permission to XTM One Desktop. Approve \
         it in System Settings > Privacy & Security > {label}, then retry. The \
         runner must run in your logged-in graphical session (not an SSH/headless \
         shell) for this to take effect.",
        label = permission.label(),
    ))
}

#[cfg(not(target_os = "macos"))]
fn ensure_impl(_permission: Permission) -> Result<(), String> {
    Ok(())
}

#[cfg(target_os = "macos")]
mod macos {
    // Screen Recording preflight/prompt. Declared `bool` in Apple's CoreGraphics
    // headers (C `_Bool`), which matches Rust's `bool` FFI ABI. Available since
    // macOS 10.15.
    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGPreflightScreenCaptureAccess() -> bool;
        fn CGRequestScreenCaptureAccess() -> bool;
    }

    // Accessibility trust check. `AXIsProcessTrusted` returns a Carbon `Boolean`
    // (`unsigned char`), so we read it as `u8` and compare, rather than assuming
    // the `_Bool` ABI.
    #[link(name = "ApplicationServices", kind = "framework")]
    extern "C" {
        fn AXIsProcessTrusted() -> u8;
    }

    pub fn has_screen_recording() -> bool {
        unsafe { CGPreflightScreenCaptureAccess() }
    }

    pub fn request_screen_recording() -> bool {
        unsafe { CGRequestScreenCaptureAccess() }
    }

    pub fn is_trusted() -> bool {
        unsafe { AXIsProcessTrusted() != 0 }
    }
}
