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

//! What a relay reports about its machine (enrollment and every hello): enough
//! for an admin to tell relays apart, nothing about the network it serves.

use serde_json::{json, Value};

use crate::config::{in_container, Config};
use crate::version::AGENT_VERSION;

pub fn hostname() -> String {
    hostname::get()
        .ok()
        .and_then(|h| h.into_string().ok())
        .unwrap_or_else(|| "unknown".to_string())
}

pub fn facts(config: &Config) -> Value {
    json!({
        "hostname": hostname(),
        "os": agent_core::platform::detect_platform(),
        "arch": agent_core::platform::detect_arch(),
        "cpus": std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1),
        "relay_version": AGENT_VERSION,
        "container": in_container(),
        "upstream_proxy": config.upstream_proxy.is_some(),
        "transport_mode": config.transport,
        "auto_update": config.auto_update,
    })
}
