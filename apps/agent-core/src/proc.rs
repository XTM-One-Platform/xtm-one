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

//! Cross-platform child-process spawn hardening.
//!
//! On Windows an agent binary may be a GUI (windowless) app, or a service with
//! no console. When such a process launches a *console-subsystem* child -
//! `powershell.exe`, `cmd.exe`, `reg.exe`, `sh` - Windows allocates a brand-new
//! console for that child, which flashes a black terminal window on screen for
//! the child's lifetime. The agents spawn children only for headless plumbing
//! (host-facts probes, the runner's `run_shell` capability), so a visible
//! console is never wanted.
//!
//! [`no_window`] sets `CREATE_NO_WINDOW` so no console is ever allocated for the
//! child. It is a no-op on macOS / Linux, where there is no such behaviour.

/// Suppress the console window Windows would otherwise allocate for a
/// console-subsystem child process. No-op on non-Windows targets.
#[cfg(windows)]
pub fn no_window(cmd: &mut tokio::process::Command) {
    // CREATE_NO_WINDOW (winbase.h): the child runs with no console window.
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    cmd.creation_flags(CREATE_NO_WINDOW);
}

/// No-op on non-Windows targets (no console is allocated for child processes).
#[cfg(not(windows))]
pub fn no_window(_cmd: &mut tokio::process::Command) {}
