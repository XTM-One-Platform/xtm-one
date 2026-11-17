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

//! The runner's own version string.
//!
//! Format `MAJOR.YYMMDD.SEQ` (the protocol/version gate the backend enforces
//! via `runner_min_supported_version`). CI stamps it at build time through the
//! `XTM_RUNNER_VERSION` env var; a plain `cargo build` falls back to the Cargo
//! package version so a dev build still reports something coherent.

pub const AGENT_VERSION: &str = match option_env!("XTM_RUNNER_VERSION") {
    Some(v) => v,
    None => env!("CARGO_PKG_VERSION"),
};

/// Which flavor this binary was built as - `desktop` (screenshots + synthetic
/// input, needs a graphical session) or `headless` (shell / files / browser
/// only, links no desktop stack).
///
/// It rides every self-update check so a headless install can only ever be
/// offered another headless build: swapping a server's runner for the desktop
/// flavor would leave it unable to start at all.
pub const FLAVOR: &str = if cfg!(feature = "desktop") {
    "desktop"
} else {
    "headless"
};

/// The minisign public keys baked in at build time (`XTM_RUNNER_MINISIGN_PUBKEY`,
/// comma-separated: CI passes the `keys` of `apps/runner/release-signing.json`).
///
/// The self-updater REQUIRES them to apply a downloaded build: a release archive
/// is only swapped in after its detached `.minisig` verifies against the key
/// whose id it names. A build without them (a plain `cargo build`) can still
/// *detect* a newer version but will refuse to self-apply — signature
/// verification is not optional, so an unsigned or unkeyed build fails closed
/// rather than trusting the network.
#[cfg_attr(not(feature = "self-update"), allow(dead_code))]
pub const MINISIGN_PUBKEY: Option<&str> = option_env!("XTM_RUNNER_MINISIGN_PUBKEY");

/// The oldest release signature this build accepts, as a Unix time compared
/// with the signed `timestamp:` (`XTM_RUNNER_SIGNATURE_FLOOR`: CI passes the
/// `timestamp_floor` of `apps/runner/release-signing.json`; unset or 0: none).
#[cfg_attr(not(feature = "self-update"), allow(dead_code))]
pub const SIGNATURE_FLOOR: Option<&str> = option_env!("XTM_RUNNER_SIGNATURE_FLOOR");
