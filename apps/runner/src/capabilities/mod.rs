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

//! Capability modules - the OS-level actions a command maps to.
//!
//! Each mirrors a `computer_use` command kind and returns a
//! [`crate::protocol::CommandOutcome`]. Screenshot / input / file ops are
//! synchronous (run in a blocking task); shell is async; browser control is
//! async and feature-gated.
//!
//! Shell and file ops are the always-built core. Screenshot and input sit
//! behind `desktop`: they are what drags in the X11 / Wayland stack, so the
//! headless flavor compiles them out entirely rather than shipping capabilities
//! that would fail at the loader on a server.

pub mod files;
pub mod jobs;
pub mod shell;

#[cfg(feature = "desktop")]
pub mod input;
#[cfg(feature = "desktop")]
pub mod permissions;
#[cfg(feature = "desktop")]
pub mod screenshot;

#[cfg(feature = "browser")]
pub mod browser;
