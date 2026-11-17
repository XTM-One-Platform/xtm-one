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
//! On Windows the runner binary is a GUI (windowless) app. When a windowless
//! process launches a *console-subsystem* child - `powershell.exe`, `cmd.exe`,
//! `sh` - Windows allocates a brand-new console for that child, which flashes a
//! black terminal window on screen for the child's lifetime. The runner spawns
//! children only for headless plumbing (host-facts probes on every heartbeat,
//! the `run_shell` capability), so a visible console is never wanted.
//!
//! [`no_window`] (shared with the relay, `agent_core::proc`) sets
//! `CREATE_NO_WINDOW` so no console is ever allocated for the child. It is a
//! no-op on macOS / Linux, where there is no such behaviour.

pub use agent_core::proc::no_window;
