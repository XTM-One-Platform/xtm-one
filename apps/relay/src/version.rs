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

//! What this build is, compiled in by the release pipeline.

/// The relay version (`XTM_RELAY_VERSION`, set by the release build; the crate
/// version, `0.0.0-dev`, otherwise).
pub const AGENT_VERSION: &str = match option_env!("XTM_RELAY_VERSION") {
    Some(version) => version,
    None => env!("CARGO_PKG_VERSION"),
};

/// The release keys a self-update must be signed with (comma-separated minisign
/// public keys, `XTM_RELAY_MINISIGN_PUBKEY`). A build without them detects an
/// update but never applies one.
#[cfg_attr(not(feature = "self-update"), allow(dead_code))]
pub const MINISIGN_PUBKEY: Option<&str> = option_env!("XTM_RELAY_MINISIGN_PUBKEY");

/// The oldest release-signature `timestamp:` this build accepts
/// (`XTM_RELAY_SIGNATURE_FLOOR`).
#[cfg_attr(not(feature = "self-update"), allow(dead_code))]
pub const SIGNATURE_FLOOR: Option<&str> = option_env!("XTM_RELAY_SIGNATURE_FLOOR");

/// The relay protocol this build speaks (`RELAY_PROTOCOL_VERSION` in
/// `backend/app/models/relay.py`).
pub const PROTOCOL_VERSION: u64 = 1;

/// The capabilities this build serves. `tcp`, `process` and `secrets` are
/// reserved by the platform for later relay kinds.
pub const CAPABILITIES: &[&str] = &["http"];

/// What messages call this agent.
#[cfg_attr(not(feature = "self-update"), allow(dead_code))]
pub const SUBJECT: &str = "relay";

/// The User-Agent of the relay's own calls to the platform (never of the
/// requests it forwards, which carry the caller's headers).
pub fn user_agent() -> String {
    format!("xtm-one-relay/{AGENT_VERSION}")
}
