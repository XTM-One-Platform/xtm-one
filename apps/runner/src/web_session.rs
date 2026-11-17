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

//! The chat window's web session, kept by the shell.
//!
//! The web app keeps its session in `localStorage`, which inside XTM One
//! Desktop lives in the webview's profile. On Windows that is a LevelDB which,
//! once a write is torn (an unclean shutdown), drops every later write at each
//! launch: the page boots with the token it held before the tear and is
//! signed out at every restart. So the shell keeps the session too, in the OS
//! keychain next to the machine's credential (`credential.rs`), and the page
//! takes it back at launch (`restoreDesktopSession()` in the web app).
//!
//! What is kept is the page's session as the page last set it - the token the
//! platform minted, and during an impersonation the administrator's - never a
//! token of the shell's own: the platform still decides when the session
//! ends. A signed-out page is kept too (both empty), which is not the same as
//! nothing kept: the first means "signed out", the second "ask the page".

use serde::{Deserialize, Serialize};

/// The longest token kept. A platform session token is a few hundred
/// characters; anything far longer is not one.
const MAX_TOKEN_LEN: usize = 4096;

/// A web session as the page holds it: its session token and, while an
/// administrator impersonates someone, the administrator's own (the
/// `copilot_token` / `copilot_admin_token` pair of `lib/api.ts`).
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WebSession {
    pub token: Option<String>,
    pub admin_token: Option<String>,
}

/// Why a session is not kept: not something the platform could have handed
/// the page.
#[derive(Debug, PartialEq, Eq)]
pub struct InvalidSession;

impl WebSession {
    /// Only session tokens, and an administrator's only under the token of
    /// the session it impersonates.
    pub fn validate(&self) -> Result<(), InvalidSession> {
        if self.admin_token.is_some() && self.token.is_none() {
            return Err(InvalidSession);
        }
        let tokens = [&self.token, &self.admin_token];
        if tokens.into_iter().flatten().all(|t| is_session_token(t)) {
            Ok(())
        } else {
            Err(InvalidSession)
        }
    }
}

/// A compact JWT: three base64url segments, none empty, of bounded length.
fn is_session_token(token: &str) -> bool {
    token.len() <= MAX_TOKEN_LEN
        && token.split('.').count() == 3
        && token.split('.').all(|part| {
            !part.is_empty()
                && part
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
        })
}

/// The kept session read back. A document that does not parse or validate
/// (written by another build, or by hand) reads as nothing kept.
pub fn parse(raw: &str) -> Option<WebSession> {
    serde_json::from_str::<WebSession>(raw)
        .ok()
        .filter(|session| session.validate().is_ok())
}

/// The document a session is kept as.
pub fn serialize(session: &WebSession) -> String {
    serde_json::to_string(session).expect("a web session always serializes")
}

#[cfg(test)]
mod tests {
    use super::*;

    const TOKEN: &str = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl";
    const ADMIN: &str = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhIn0.YWRtaW4tc2ln";

    fn session(token: Option<&str>, admin_token: Option<&str>) -> WebSession {
        WebSession {
            token: token.map(str::to_string),
            admin_token: admin_token.map(str::to_string),
        }
    }

    #[test]
    fn a_session_round_trips_signed_out_included() {
        for kept in [
            session(Some(TOKEN), None),
            session(Some(TOKEN), Some(ADMIN)),
            session(None, None),
        ] {
            assert_eq!(kept.validate(), Ok(()));
            assert_eq!(parse(&serialize(&kept)), Some(kept));
        }
        assert_eq!(
            serialize(&session(None, None)),
            r#"{"token":null,"admin_token":null}"#
        );
    }

    #[test]
    fn only_session_tokens_are_kept() {
        for refused in [
            "",
            "not-a-jwt",
            "a.b",
            "a.b.c.d",
            "a..c",
            "a.b.",
            "a.b.c d",
            "a.b.c\n",
            "a.b/c.d",
            "a.b+c.d",
        ] {
            assert_eq!(
                session(Some(refused), None).validate(),
                Err(InvalidSession),
                "{refused:?}"
            );
            assert_eq!(
                session(Some(TOKEN), Some(refused)).validate(),
                Err(InvalidSession),
                "{refused:?}"
            );
        }
        let long = format!("{}.{}.sig", "a".repeat(10), "b".repeat(MAX_TOKEN_LEN));
        assert_eq!(session(Some(&long), None).validate(), Err(InvalidSession));
    }

    #[test]
    fn an_administrator_s_token_needs_the_session_it_impersonates() {
        assert_eq!(session(None, Some(ADMIN)).validate(), Err(InvalidSession));
    }

    #[test]
    fn a_document_that_is_not_a_kept_session_reads_as_none() {
        for raw in [
            "",
            "null",
            "{}x",
            r#"{"token":"a.b"}"#,
            r#"{"token":null,"admin_token":"a.b.c"}"#,
            r#"{"token":"a.b.c","extra":1}"#,
            "run-0123456789",
        ] {
            assert_eq!(parse(raw), None, "{raw}");
        }
        assert_eq!(parse("{}"), Some(session(None, None)));
    }
}
