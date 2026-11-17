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

//! Shared core of the XTM One on-prem agents.
//!
//! Two binaries run on a customer's machines and talk to the platform the same
//! way - outbound only, enrolled once, a durable credential, self-updating
//! under signature: the runner (`apps/runner`) and the relay (`apps/relay`).
//! What they must do identically lives here, so a fix to how a credential is
//! stored or a release is verified reaches both:
//!
//! * [`credential`] - the durable credential in the OS keychain, or a 0600 file
//!   where there is none;
//! * [`retry`] - which failures a later attempt can succeed on, and how long to
//!   wait before it;
//! * [`platform`] - the platform / architecture vocabulary of the update
//!   manifests and the salted machine identity sent at enrollment;
//! * [`proc`] - child processes that never flash a console on Windows;
//! * `release` (feature `self-update`) - verifying a release archive before an
//!   agent replaces itself with it.
//!
//! Anything an agent says in its own name (the runner, the relay) is passed in
//! by the caller, so each keeps its own wording.

pub mod credential;
pub mod platform;
pub mod proc;
#[cfg(feature = "self-update")]
pub mod release;
pub mod retry;
