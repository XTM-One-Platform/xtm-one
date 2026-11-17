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

//! HTTP client for the runner REST surface.
//!
//! Enrollment (`renr-` -> `run-`), the long-poll fallback, heartbeats, result
//! uploads (multipart) and the self-update manifest. The WebSocket fast path
//! lives in [`crate::transport`]; results always upload here over HTTP so large
//! binaries never traverse the socket.

#[cfg(test)]
use std::future::Future;
use std::time::Duration;

use anyhow::{anyhow, Context, Result};
#[cfg(test)]
use reqwest::header::{HeaderMap, RETRY_AFTER};
use reqwest::redirect::Policy;
use reqwest::{Client, StatusCode};
#[cfg(test)]
use tokio::time::Instant;

use crate::config::RunnerConfig;
use crate::protocol::{
    CommandOutcome, EnrollRequest, EnrollResponse, FilePart, OutcomeStatus, PollResponse,
    ResultMeta, MAX_RESULT_META_BYTES,
};

/// How long `submit_result` keeps retrying a transient failure.
///
/// The platform reclaims a dispatched command once its lease lapses and
/// redelivers it; the lease reserves 60 s for the result upload
/// (`_RUNNER_UPLOAD_GRACE_SECONDS` in `backend/app/services/runner_transport.py`,
/// pinned against this constant by `backend/tests/test_runners_transport.py`).
/// Retrying past it would race a redelivery of the same command, so the budget
/// stays strictly inside the grace. The agent's wait for the result covers it
/// too (`RUNNER_UPLOAD_RETRY_BUDGET_SECONDS`, same file and test), so a result
/// that rides out a restart still reaches the agent.
pub(crate) const UPLOAD_RETRY_BUDGET_SECS: u64 = 50;

/// Back-off between result-upload attempts.
const UPLOAD_BACKOFF_BASE: Duration = Duration::from_millis(500);
const UPLOAD_BACKOFF_CAP: Duration = Duration::from_secs(8);

// The retry policy is shared with the relay (`agent_core::retry`): which
// failures a later attempt can succeed on, and how long to wait before it.
#[cfg(test)]
pub(crate) use agent_core::retry::Unavailable;
#[cfg(test)]
use agent_core::retry::{
    backoff_delay, delay_for, is_transient_status, jitter, retry_after, MAX_RETRY_AFTER,
};
use agent_core::retry::{check_transient, retry_within};
pub(crate) use agent_core::retry::{is_transient, retry_delay};

/// Authenticated REST client bound to one enrolled runner.
#[derive(Clone)]
pub struct ApiClient {
    http: Client,
    api_base: String,
    /// `https://host/api/v1` - the sibling routers the runner credential also
    /// authenticates against (the Desktop browser sign-in handoff).
    #[cfg_attr(not(feature = "gui"), allow(dead_code))]
    v1_base: String,
    token: String,
    long_poll_timeout: u64,
    /// The client-wide request timeout, which also bounds each result-upload
    /// attempt.
    request_timeout: Duration,
}

/// A pending browser sign-in opened for this Desktop app.
#[cfg(feature = "gui")]
#[derive(serde::Deserialize)]
pub struct DesktopLoginStart {
    pub login_id: String,
    pub expires_in_seconds: u64,
}

/// One poll of a pending browser sign-in.
#[cfg(feature = "gui")]
pub enum DesktopLoginPoll {
    /// The person has not finished in the browser yet.
    Pending,
    /// The session, handed out exactly once.
    Completed { access_token: String },
    /// Expired, cancelled, or never this runner's - stop polling.
    Gone,
}

impl ApiClient {
    pub fn new(config: &RunnerConfig, token: String) -> Result<Self> {
        let request_timeout_secs = config.long_poll_timeout_seconds + 15;
        let http = build_http_client(request_timeout_secs)?;
        Ok(Self {
            http,
            api_base: config.api_base(),
            v1_base: format!("{}/api/v1", config.base_url()),
            token,
            long_poll_timeout: config.long_poll_timeout_seconds,
            request_timeout: Duration::from_secs(request_timeout_secs),
        })
    }

    /// Open a pending browser sign-in bound to this runner.
    #[cfg(feature = "gui")]
    pub async fn desktop_login_start(&self) -> Result<DesktopLoginStart> {
        let url = format!("{}/desktop-login/start", self.v1_base);
        let resp = self
            .bearer(self.http.post(&url))
            .send()
            .await
            .context("desktop sign-in start request")?
            .error_for_status()
            .context("desktop sign-in start status")?;
        resp.json().await.context("parsing desktop sign-in start")
    }

    /// Poll a pending browser sign-in; the session comes back once.
    #[cfg(feature = "gui")]
    pub async fn desktop_login_exchange(&self, login_id: &str) -> Result<DesktopLoginPoll> {
        let url = format!("{}/desktop-login/exchange", self.v1_base);
        let resp = self
            .bearer(self.http.post(&url))
            .json(&serde_json::json!({ "login_id": login_id }))
            .send()
            .await
            .context("desktop sign-in exchange request")?;
        match resp.status() {
            reqwest::StatusCode::ACCEPTED => Ok(DesktopLoginPoll::Pending),
            // Expired / cancelled / never ours - or the runner credential
            // itself refused (revoked mid-flow): nothing will fill this
            // record, so stop polling instead of retrying until the deadline.
            reqwest::StatusCode::NOT_FOUND
            | reqwest::StatusCode::GONE
            | reqwest::StatusCode::UNAUTHORIZED
            | reqwest::StatusCode::FORBIDDEN => Ok(DesktopLoginPoll::Gone),
            status if status.is_success() => {
                #[derive(serde::Deserialize)]
                struct Body {
                    access_token: String,
                }
                let body: Body = resp
                    .json()
                    .await
                    .context("parsing desktop sign-in exchange")?;
                Ok(DesktopLoginPoll::Completed {
                    access_token: body.access_token,
                })
            }
            status => Err(anyhow!("desktop sign-in exchange failed ({status})")),
        }
    }

    /// Drop a pending browser sign-in this app no longer waits for.
    #[cfg(feature = "gui")]
    pub async fn desktop_login_cancel(&self, login_id: &str) -> Result<()> {
        let url = format!("{}/desktop-login/{}", self.v1_base, login_id);
        self.bearer(self.http.delete(&url))
            .send()
            .await
            .context("desktop sign-in cancel request")?;
        Ok(())
    }

    fn bearer(&self, builder: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        builder.bearer_auth(&self.token)
    }

    /// Long-poll for the next command. Returns `None` on an empty (timeout)
    /// response so the caller immediately re-polls. The runner version rides in
    /// the query string so the server can gate an out-of-date runner (it answers
    /// `update_required` instead of a command).
    pub async fn poll(&self) -> Result<PollResponse> {
        let url = format!(
            "{}/poll?version={}",
            self.api_base,
            crate::version::AGENT_VERSION
        );
        let resp = self
            .bearer(self.http.get(&url))
            .timeout(Duration::from_secs(self.long_poll_timeout + 10))
            .send()
            .await
            .context("long-poll request")?;
        if resp.status() == reqwest::StatusCode::UNAUTHORIZED {
            return Err(anyhow!("runner credential rejected (revoked?)"));
        }
        let resp = check_transient(resp)?
            .error_for_status()
            .context("long-poll status")?;
        resp.json().await.context("parsing poll response")
    }

    /// Refresh liveness + host facts over HTTP (long-poll transport keepalive).
    pub async fn heartbeat(
        &self,
        host_facts: &serde_json::Value,
        agent_version: &str,
    ) -> Result<()> {
        let url = format!("{}/heartbeat", self.api_base);
        let body = serde_json::json!({
            "host_facts": host_facts,
            "agent_version": agent_version,
        });
        let resp = self
            .bearer(self.http.post(&url))
            .json(&body)
            .send()
            .await
            .context("heartbeat request")?;
        check_transient(resp)?
            .error_for_status()
            .context("heartbeat status")?;
        Ok(())
    }

    /// Upload a command's terminal result (JSON meta + optional binary file).
    ///
    /// Retries a transient failure (no answer, a timeout, 408 / 429 / 5xx) with
    /// jittered back-off, honouring `Retry-After`, for up to
    /// `UPLOAD_RETRY_BUDGET_SECS` - long enough to outlast a platform rollout,
    /// short enough to stay inside the upload grace of the command's lease
    /// ([`retry_within`]). The backend redelivers a command whose result never
    /// lands, so a dropped upload would re-execute a mutating command and leave
    /// the agent's tool call waiting on a result that never comes. Any other
    /// status fails at once: retrying a refusal cannot succeed. The server-side
    /// result write is idempotent (first result wins), so a duplicate is
    /// dropped rather than double-recorded.
    pub async fn submit_result(&self, command_id: &str, outcome: CommandOutcome) -> Result<()> {
        let url = format!("{}/commands/{}/result", self.api_base, command_id);
        let (meta_json, file) = upload_parts(command_id, outcome)?;
        // The multipart form is consumed by send(), so keep the file parts to
        // rebuild it on each retry attempt.
        let file = file.map(|f| (f.filename, f.content_type, f.bytes));
        let (url, meta_json, file) = (&url, &meta_json, &file);
        retry_within(
            &format!("result upload for {command_id}"),
            Duration::from_secs(UPLOAD_RETRY_BUDGET_SECS),
            self.request_timeout,
            UPLOAD_BACKOFF_BASE,
            UPLOAD_BACKOFF_CAP,
            move |timeout| async move {
                let mut form = reqwest::multipart::Form::new().text("meta", meta_json.clone());
                if let Some((name, content_type, bytes)) = file {
                    let part = reqwest::multipart::Part::bytes(bytes.clone())
                        .file_name(name.clone())
                        .mime_str(content_type)
                        .context("building file part")?;
                    form = form.part("file", part);
                }
                let resp = self
                    .bearer(self.http.post(url))
                    .timeout(timeout)
                    .multipart(form)
                    .send()
                    .await
                    .context("result upload request")?;
                check_transient(resp)?
                    .error_for_status()
                    .context("result upload status")?;
                Ok::<(), anyhow::Error>(())
            },
        )
        .await
    }

    /// Fetch the self-update manifest for THIS machine (os + arch + flavor +
    /// current version), so the server can return a `target` block with the
    /// exact version-matched archive + integrity URLs and its update decision.
    ///
    /// The flavor is not decoration: a headless install that updated itself
    /// into a desktop build would link a desktop stack its host does not have
    /// and never start again. It rides every check so the server can
    /// only ever answer with the same flavor.
    pub async fn update_manifest(&self, os_name: &str, arch: &str) -> Result<serde_json::Value> {
        let url = format!(
            "{}/update-manifest?current_version={}&os={}&arch={}&flavor={}",
            self.api_base,
            crate::version::AGENT_VERSION,
            os_name,
            arch,
            crate::version::FLAVOR,
        );
        let resp = self
            .bearer(self.http.get(&url))
            .send()
            .await
            .context("update-manifest request")?
            .error_for_status()
            .context("update-manifest status")?;
        resp.json().await.context("parsing update manifest")
    }

    /// Download an artifact (archive / `.sha256` / `.minisig`) as raw bytes.
    ///
    /// No bearer: these are public release assets. In the embedded-distribution
    /// model the URL is the enrolled platform's own `/download/...` redirect to
    /// a same-origin `/dist` asset, which the shared client follows (its
    /// no-downgrade redirect policy never blocks a same-scheme redirect).
    /// Authenticity does not rest on transport regardless: the caller verifies
    /// the archive's SHA-256 and minisign signature against the compiled-in key.
    #[cfg(feature = "self-update")]
    pub async fn download_bytes(&self, url: &str) -> Result<Vec<u8>> {
        let resp = self
            .http
            .get(url)
            .send()
            .await
            .with_context(|| format!("downloading {url}"))?
            .error_for_status()
            .with_context(|| format!("download status for {url}"))?;
        let bytes = resp
            .bytes()
            .await
            .with_context(|| format!("reading body of {url}"))?;
        Ok(bytes.to_vec())
    }

    /// Download a small text artifact (`.sha256` / `.minisig`).
    #[cfg(feature = "self-update")]
    pub async fn download_text(&self, url: &str) -> Result<String> {
        let bytes = self.download_bytes(url).await?;
        String::from_utf8(bytes).with_context(|| format!("{url} was not valid UTF-8"))
    }
}

/// The `meta` JSON and the file a result upload carries for `outcome`.
///
/// A `meta` past [`MAX_RESULT_META_BYTES`] would be refused whole, so it is not
/// sent: the command is reported as failed with the size in its error, and the
/// file goes with the result it belonged to.
fn upload_parts(command_id: &str, outcome: CommandOutcome) -> Result<(String, Option<FilePart>)> {
    let meta = ResultMeta {
        status: outcome.status.as_str().to_string(),
        result: outcome.result,
        error: outcome.error,
    };
    let meta_json = serde_json::to_string(&meta).context("serializing result meta")?;
    if meta_json.len() <= MAX_RESULT_META_BYTES {
        return Ok((meta_json, outcome.file));
    }
    tracing::warn!(
        "result for {command_id} is {} bytes, over the {MAX_RESULT_META_BYTES} a result may \
         carry; reporting the command as failed",
        meta_json.len()
    );
    let refused = ResultMeta {
        status: OutcomeStatus::Failed.as_str().to_string(),
        result: None,
        error: Some(format!(
            "the command's result is {} bytes once written as JSON, over the {} MiB a result \
             may carry: ask the machine for less at once (a narrower command, one folder, part \
             of a file)",
            meta_json.len(),
            MAX_RESULT_META_BYTES / (1024 * 1024)
        )),
    };
    let refused = serde_json::to_string(&refused).context("serializing result meta")?;
    Ok((refused, None))
}

/// Exchange an enrollment key for a durable runner credential (no auth yet).
pub async fn enroll(server_url: &str, request: &EnrollRequest) -> Result<EnrollResponse> {
    let http = build_http_client(60)?;
    let base = server_url.trim_end_matches('/');
    let url = format!("{base}/api/v1/runners/enroll");
    let resp = http
        .post(&url)
        .json(request)
        .send()
        .await
        .context("enroll request")?;
    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(enroll_error(status, &body));
    }
    resp.json().await.context("parsing enroll response")
}

/// The server's terse refusal of an enrollment whose transport it cannot
/// confirm as HTTPS: the same body as the A2A surface's.
const INSECURE_TRANSPORT_DETAIL: &str = "https required";

/// A failed enrollment, worded for whoever reads it in the panel or the CLI.
///
/// Only a server running with `CREDENTIAL_TRANSPORT_STRICT` answers so. It
/// says only `https required`, even to a runner that used `https://`: behind
/// a TLS-terminating proxy it does not trust, the platform sees plain HTTP.
/// The fix is on the server, so the message says so. Every other failure is
/// reported exactly as before.
fn enroll_error(status: StatusCode, body: &str) -> anyhow::Error {
    let refused_transport = status == StatusCode::FORBIDDEN
        && serde_json::from_str::<serde_json::Value>(body).is_ok_and(|value| {
            value.get("detail").and_then(serde_json::Value::as_str)
                == Some(INSECURE_TRANSPORT_DETAIL)
        });
    if refused_transport {
        return anyhow!(
            "enrollment refused ({status}): the server could not confirm this connection \
             arrived over HTTPS. Behind a TLS-terminating proxy, its administrator must set \
             FORWARDED_ALLOW_IPS to the proxy's address (and have the proxy send \
             X-Forwarded-Proto), or turn CREDENTIAL_TRANSPORT_STRICT (\"Require HTTPS for \
             runner enrollment and MCP tokens\" in Settings > Security) off."
        );
    }
    anyhow!("enrollment failed ({status}): {body}")
}

fn build_http_client(timeout_secs: u64) -> Result<Client> {
    Client::builder()
        .user_agent(format!("xtm-one-runner/{}", crate::version::AGENT_VERSION))
        .timeout(Duration::from_secs(timeout_secs))
        .connect_timeout(Duration::from_secs(20))
        .redirect(no_downgrade_redirect_policy())
        .build()
        .context("building HTTP client")
}

/// Follow redirects (bounded to 10), but NEVER an HTTPS -> HTTP downgrade.
///
/// Every client built here carries secrets that grant OS control: the
/// enrollment key and the durable `run-` credential travel in the enroll
/// request body/response, and the `Authorization: Bearer` credential rides on
/// every authenticated call. reqwest preserves the request body across a
/// 307/308 redirect and does not strip `Authorization` on a same-host,
/// same-explicit-port scheme downgrade, so following an HTTPS -> HTTP redirect
/// could disclose those secrets in cleartext. In the embedded-distribution
/// model every download is a same-origin redirect served by the enrolled
/// platform, so there is no legitimate HTTPS -> HTTP redirect to preserve.
fn no_downgrade_redirect_policy() -> Policy {
    Policy::custom(|attempt| {
        if attempt.previous().len() >= 10 {
            return attempt.error("too many redirects");
        }
        let downgraded = attempt
            .previous()
            .last()
            .is_some_and(|prev| prev.scheme() == "https" && attempt.url().scheme() == "http");
        if downgraded {
            attempt.error("refusing to follow an HTTPS -> HTTP redirect (would expose the runner credential in cleartext)")
        } else {
            attempt.follow()
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use reqwest::header::HeaderValue;
    use std::collections::HashSet;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};

    fn ms(millis: u64) -> Duration {
        Duration::from_millis(millis)
    }

    fn retry_after_of(value: &'static str) -> Option<Duration> {
        let mut headers = HeaderMap::new();
        headers.insert(RETRY_AFTER, HeaderValue::from_static(value));
        retry_after(&headers)
    }

    fn unavailable(retry_after: Option<Duration>) -> anyhow::Error {
        anyhow!(Unavailable {
            status: StatusCode::SERVICE_UNAVAILABLE,
            retry_after,
        })
    }

    #[test]
    fn retry_after_reads_delta_seconds() {
        assert_eq!(retry_after_of("5"), Some(Duration::from_secs(5)));
        assert_eq!(retry_after_of(" 1 "), Some(Duration::from_secs(1)));
        assert_eq!(retry_after_of("0"), Some(Duration::ZERO));
    }

    #[test]
    fn retry_after_ignores_dates_garbage_and_absence() {
        assert_eq!(retry_after_of("Wed, 21 Oct 2015 07:28:00 GMT"), None);
        assert_eq!(retry_after_of("soon"), None);
        assert_eq!(retry_after_of("-3"), None);
        assert_eq!(retry_after(&HeaderMap::new()), None);
    }

    #[test]
    fn retry_after_is_capped() {
        assert_eq!(retry_after_of("3600"), Some(MAX_RETRY_AFTER));
    }

    #[test]
    fn an_unconfirmed_transport_refusal_names_the_server_side_fix() {
        let error =
            enroll_error(StatusCode::FORBIDDEN, r#"{"detail": "https required"}"#).to_string();
        assert!(
            error.contains("could not confirm this connection arrived over HTTPS"),
            "{error}"
        );
        assert!(error.contains("FORWARDED_ALLOW_IPS"), "{error}");
        assert!(error.contains("CREDENTIAL_TRANSPORT_STRICT"), "{error}");
    }

    #[test]
    fn other_enrollment_failures_keep_the_server_s_answer() {
        let expired = enroll_error(
            StatusCode::UNAUTHORIZED,
            r#"{"detail": "Invalid or expired enrollment key"}"#,
        )
        .to_string();
        assert!(
            expired.contains("Invalid or expired enrollment key"),
            "{expired}"
        );
        // A 403 for another reason is not taken for the transport refusal.
        let other = enroll_error(
            StatusCode::FORBIDDEN,
            r#"{"detail": "Runners are disabled"}"#,
        )
        .to_string();
        assert!(
            other.starts_with("enrollment failed (403 Forbidden)"),
            "{other}"
        );
        let html = enroll_error(StatusCode::FORBIDDEN, "<html>https required</html>").to_string();
        assert!(!html.contains("FORWARDED_ALLOW_IPS"), "{html}");
    }

    #[test]
    fn transient_statuses_are_retried_and_refusals_are_not() {
        for code in [408, 429, 500, 502, 503, 504] {
            let status = StatusCode::from_u16(code).unwrap();
            assert!(is_transient_status(status), "{code} should be retried");
        }
        for code in [400, 401, 403, 404, 409, 410, 413, 422] {
            let status = StatusCode::from_u16(code).unwrap();
            assert!(!is_transient_status(status), "{code} should not be retried");
        }
    }

    #[test]
    fn a_restart_is_transient_and_a_refusal_is_not() {
        assert!(is_transient(&unavailable(None).context("long-poll status")));
        assert!(!is_transient(&anyhow!(
            "runner credential rejected (revoked?)"
        )));
    }

    #[test]
    fn backoff_grows_is_capped_and_jittered() {
        let (base, cap) = (ms(500), Duration::from_secs(8));
        assert_eq!(backoff_delay(0, base, cap, 0.0), ms(250));
        assert_eq!(backoff_delay(0, base, cap, 1.0), ms(500));
        assert_eq!(backoff_delay(3, base, cap, 0.0), Duration::from_secs(2));
        assert_eq!(backoff_delay(u32::MAX, base, cap, 1.0), cap);
        for attempt in 0..40 {
            for jitter in [0.0, 0.5, 0.999] {
                let delay = backoff_delay(attempt, base, cap, jitter);
                assert!(
                    (base / 2..=cap).contains(&delay),
                    "{attempt} {jitter} {delay:?}"
                );
            }
        }
    }

    #[test]
    fn retry_after_is_a_floor_on_the_backoff() {
        let (base, cap) = (ms(500), Duration::from_secs(8));
        let five = Duration::from_secs(5);
        assert_eq!(delay_for(Some(five), 0, base, cap, 0.5, 0.0), five);
        let spread = delay_for(Some(five), 0, base, cap, 0.5, 0.999);
        assert!(five < spread && spread < five + base, "{spread:?}");
        // A hint shorter than the back-off leaves the back-off alone.
        assert_eq!(
            delay_for(Some(ms(100)), 4, base, cap, 0.5, 0.9),
            backoff_delay(4, base, cap, 0.5)
        );
        let unhinted = retry_delay(&anyhow!("connection refused"), 0, base, cap);
        assert!(unhinted <= base);
    }

    #[test]
    fn a_retry_after_floor_keeps_the_fleet_spread() {
        // The startup gate answers every runner `Retry-After: 5` at once; if
        // they all waited exactly 5 s they would all come back together.
        let (base, cap) = (ms(500), Duration::from_secs(8));
        let five = Duration::from_secs(5);
        let delays: HashSet<Duration> = (0..32)
            .map(|_| retry_delay(&unavailable(Some(five)), 0, base, cap))
            .collect();
        assert!(delays.iter().all(|d| *d >= five), "{delays:?}");
        assert!(
            delays.len() > 1,
            "every retry lands at the same instant: {delays:?}"
        );
    }

    #[test]
    fn upload_budget_outlasts_a_restart_inside_the_server_grace() {
        let budget = Duration::from_secs(UPLOAD_RETRY_BUDGET_SECS);
        // The lease keeps 60 s for the upload (runner_transport.py).
        assert!(budget < Duration::from_secs(60));
        // The startup gate asks for 5 s at a time; a rollout lasts tens.
        assert!(budget >= Duration::from_secs(30));
    }

    fn content_result(len: usize) -> CommandOutcome {
        CommandOutcome::completed_file(
            serde_json::json!({ "content": "x".repeat(len) }),
            FilePart {
                bytes: vec![1, 2, 3],
                filename: "a.bin".into(),
                content_type: "application/octet-stream".into(),
            },
        )
    }

    #[test]
    fn a_result_up_to_the_meta_limit_is_uploaded_as_it_is() {
        let (empty, _) = upload_parts("command-1", content_result(0)).unwrap();
        let fits = MAX_RESULT_META_BYTES - empty.len();
        let (meta, file) = upload_parts("command-1", content_result(fits)).unwrap();
        assert_eq!(meta.len(), MAX_RESULT_META_BYTES);
        let meta: serde_json::Value = serde_json::from_str(&meta).unwrap();
        assert_eq!(meta["status"], "completed");
        assert_eq!(meta["result"]["content"].as_str().unwrap().len(), fits);
        assert_eq!(file.unwrap().bytes, vec![1, 2, 3]);
    }

    #[test]
    fn a_result_past_the_meta_limit_is_reported_as_failed_and_says_why() {
        let (empty, _) = upload_parts("command-1", content_result(0)).unwrap();
        let over = MAX_RESULT_META_BYTES - empty.len() + 1;
        let (meta, file) = upload_parts("command-1", content_result(over)).unwrap();
        assert!(meta.len() < 1024, "{}", meta.len());
        let meta: serde_json::Value = serde_json::from_str(&meta).unwrap();
        assert_eq!(meta["status"], "failed");
        assert!(meta.get("result").is_none(), "{meta}");
        let error = meta["error"].as_str().unwrap();
        let size = format!("{} bytes once written as JSON", MAX_RESULT_META_BYTES + 1);
        assert!(error.contains(&size), "{error}");
        assert!(error.contains("16 MiB"), "{error}");
        // The file belonged to the result that is not sent.
        assert!(file.is_none());
    }

    #[test]
    fn jitter_draws_are_independent_fractions() {
        let draws: Vec<f64> = (0..64).map(|_| jitter()).collect();
        assert!(draws.iter().all(|j| (0.0..1.0).contains(j)), "{draws:?}");
        let distinct: HashSet<u64> = draws.iter().map(|j| j.to_bits()).collect();
        assert_eq!(distinct.len(), draws.len(), "{draws:?}");
    }

    // The upload retry loop, on tokio's paused clock: sleeps advance it
    // instantly, so these run in no time and every duration is exact.

    const BUDGET: Duration = Duration::from_secs(UPLOAD_RETRY_BUDGET_SECS);
    const ATTEMPT_TIMEOUT: Duration = Duration::from_secs(40);

    async fn upload_with<F, Fut>(attempt: F) -> Result<()>
    where
        F: FnMut(Duration) -> Fut,
        Fut: Future<Output = Result<()>>,
    {
        retry_within(
            "test upload",
            BUDGET,
            ATTEMPT_TIMEOUT,
            UPLOAD_BACKOFF_BASE,
            UPLOAD_BACKOFF_CAP,
            attempt,
        )
        .await
    }

    #[tokio::test(start_paused = true)]
    async fn an_upload_rides_out_a_restart() {
        let started = Instant::now();
        let mut calls = 0;
        let result = upload_with(|_| {
            calls += 1;
            let answer = if calls <= 3 {
                Err(unavailable(Some(Duration::from_secs(5))))
            } else {
                Ok(())
            };
            async move { answer }
        })
        .await;
        assert!(result.is_ok(), "{result:?}");
        assert_eq!(calls, 4);
        // Three `Retry-After: 5` floors, and still well inside the budget.
        let elapsed = started.elapsed();
        assert!(elapsed >= Duration::from_secs(15), "{elapsed:?}");
        assert!(elapsed < BUDGET, "{elapsed:?}");
    }

    #[tokio::test(start_paused = true)]
    async fn a_refused_upload_is_not_retried() {
        let started = Instant::now();
        let mut calls = 0;
        let result = upload_with(|_| {
            calls += 1;
            async { Err::<(), _>(anyhow!("result upload status: 404 Not Found")) }
        })
        .await;
        assert!(result.is_err());
        assert_eq!(calls, 1);
        assert_eq!(started.elapsed(), Duration::ZERO);
    }

    #[tokio::test(start_paused = true)]
    async fn an_upload_gives_up_inside_its_budget() {
        let started = Instant::now();
        let mut attempts = Vec::new();
        let result = upload_with(|timeout| {
            attempts.push((started.elapsed(), timeout));
            async { Err::<(), _>(unavailable(None)) }
        })
        .await;
        assert!(result.is_err());
        assert!(attempts.len() > 3, "{attempts:?}");
        assert!(started.elapsed() < BUDGET);
        for (at, timeout) in attempts {
            // No attempt starts, or may run, past the budget.
            assert!(at + timeout <= BUDGET, "{at:?} + {timeout:?}");
        }
    }

    #[tokio::test(start_paused = true)]
    async fn a_platform_back_before_the_budget_ends_still_gets_the_result() {
        // Turned away with `Retry-After: 30` twice: the second back-off would
        // end past the budget, and the platform is back at 35 s.
        let started = Instant::now();
        let mut calls = 0;
        let result = upload_with(|_| {
            calls += 1;
            let answer = if started.elapsed() < Duration::from_secs(35) {
                Err(unavailable(Some(Duration::from_secs(30))))
            } else {
                Ok(())
            };
            async move { answer }
        })
        .await;
        assert!(result.is_ok(), "{result:?}");
        assert_eq!(calls, 3);
        assert!(started.elapsed() < BUDGET);
    }

    #[tokio::test(start_paused = true)]
    async fn retries_go_on_until_the_deadline() {
        // `Retry-After: 30` puts the attempts at 0 s and 30 s; the platform
        // is still down at 45 s and back at 46 s, inside the budget.
        let started = Instant::now();
        let result = upload_with(|_| {
            let answer = if started.elapsed() < Duration::from_secs(46) {
                Err(unavailable(Some(Duration::from_secs(30))))
            } else {
                Ok(())
            };
            async move { answer }
        })
        .await;
        assert!(result.is_ok(), "{result:?}");
        assert!(started.elapsed() < BUDGET);
    }

    #[tokio::test(start_paused = true)]
    async fn a_hung_upload_attempt_leaves_room_for_another() {
        // The first attempt is never answered (the connection hangs until the
        // client times it out); the next one lands.
        let mut timeouts = Vec::new();
        let result = upload_with(|timeout| {
            timeouts.push(timeout);
            let hangs = timeouts.len() == 1;
            async move {
                if hangs {
                    tokio::time::sleep(timeout).await;
                    return Err(unavailable(None));
                }
                Ok(())
            }
        })
        .await;
        assert!(result.is_ok(), "{result:?}");
        assert_eq!(timeouts.len(), 2);
        // The hung attempt ran for the client's timeout, not the whole budget.
        assert_eq!(timeouts[0], ATTEMPT_TIMEOUT);
    }

    // The real client against a local stand-in for the platform.

    /// Answers each request with the next scripted status (and `Retry-After`)
    /// and closes the connection; counts the requests it read.
    async fn scripted_platform(
        script: Vec<(u16, Option<&'static str>)>,
    ) -> (RunnerConfig, Arc<AtomicUsize>) {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let served = Arc::new(AtomicUsize::new(0));
        let count = served.clone();
        tokio::spawn(async move {
            while let Ok((mut socket, _)) = listener.accept().await {
                let mut request = Vec::new();
                let mut chunk = [0u8; 8192];
                let mut body_end = None;
                while body_end.is_none_or(|end| request.len() < end) {
                    let read = socket.read(&mut chunk).await.unwrap_or(0);
                    if read == 0 {
                        break;
                    }
                    request.extend_from_slice(&chunk[..read]);
                    if body_end.is_none() {
                        if let Some(head) = request.windows(4).position(|w| w == b"\r\n\r\n") {
                            let head_text =
                                String::from_utf8_lossy(&request[..head]).to_lowercase();
                            let length: usize = head_text
                                .lines()
                                .find_map(|line| line.strip_prefix("content-length:"))
                                .and_then(|value| value.trim().parse().ok())
                                .unwrap_or(0);
                            body_end = Some(head + 4 + length);
                        }
                    }
                }
                let n = count.fetch_add(1, Ordering::SeqCst);
                let (status, retry_after) = script[n.min(script.len() - 1)];
                let retry_after = retry_after
                    .map(|value| format!("retry-after: {value}\r\n"))
                    .unwrap_or_default();
                let response = format!(
                    "HTTP/1.1 {status} Scripted\r\ncontent-length: 0\r\nconnection: close\r\n{retry_after}\r\n"
                );
                let _ = socket.write_all(response.as_bytes()).await;
                let _ = socket.shutdown().await;
            }
        });
        let config = serde_json::from_value(serde_json::json!({
            "server_url": format!("http://{addr}"),
            "runner_id": "runner-test",
            "platform": "linux",
        }))
        .unwrap();
        (config, served)
    }

    #[tokio::test]
    async fn a_result_upload_rides_out_a_platform_restart() {
        let (config, served) =
            scripted_platform(vec![(503, Some("1")), (502, None), (200, None)]).await;
        let api = ApiClient::new(&config, "run-test".into()).unwrap();
        let started = std::time::Instant::now();
        let outcome = CommandOutcome::completed(serde_json::json!({ "ok": true }));
        api.submit_result("command-1", outcome).await.unwrap();
        assert_eq!(served.load(Ordering::SeqCst), 3);
        // The platform's `Retry-After: 1` was honoured on the way.
        assert!(started.elapsed() >= Duration::from_secs(1));
    }

    #[tokio::test]
    async fn a_refused_result_upload_is_not_retried() {
        let (config, served) = scripted_platform(vec![(401, None), (200, None)]).await;
        let api = ApiClient::new(&config, "run-test".into()).unwrap();
        let err = api
            .submit_result("command-1", CommandOutcome::failed("boom"))
            .await
            .unwrap_err();
        assert!(!is_transient(&err), "{err:#}");
        assert_eq!(served.load(Ordering::SeqCst), 1);
    }

    /// Reads each request whole and answers it 200, keeping what it read -
    /// except the first, whose connection it drops after `drop_after` bytes,
    /// unanswered.
    async fn recording_platform(drop_after: usize) -> (RunnerConfig, Arc<Mutex<Vec<Vec<u8>>>>) {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let requests = Arc::new(Mutex::new(Vec::new()));
        let kept = requests.clone();
        tokio::spawn(async move {
            let mut connections = 0;
            while let Ok((mut socket, _)) = listener.accept().await {
                connections += 1;
                let drop_at = if connections == 1 {
                    drop_after
                } else {
                    usize::MAX
                };
                let mut request = Vec::new();
                let mut chunk = [0u8; 8192];
                let mut body_end = None;
                while body_end.is_none_or(|end| request.len() < end) && request.len() < drop_at {
                    let read = socket.read(&mut chunk).await.unwrap_or(0);
                    if read == 0 {
                        break;
                    }
                    request.extend_from_slice(&chunk[..read]);
                    if body_end.is_none() {
                        if let Some(head) = request.windows(4).position(|w| w == b"\r\n\r\n") {
                            let head_text =
                                String::from_utf8_lossy(&request[..head]).to_lowercase();
                            let length: usize = head_text
                                .lines()
                                .find_map(|line| line.strip_prefix("content-length:"))
                                .and_then(|value| value.trim().parse().ok())
                                .unwrap_or(0);
                            body_end = Some(head + 4 + length);
                        }
                    }
                }
                if request.len() >= drop_at {
                    drop(socket);
                    continue;
                }
                kept.lock().unwrap().push(request);
                let _ = socket
                    .write_all(b"HTTP/1.1 200 OK\r\ncontent-length: 0\r\nconnection: close\r\n\r\n")
                    .await;
                let _ = socket.shutdown().await;
            }
        });
        let config = serde_json::from_value(serde_json::json!({
            "server_url": format!("http://{addr}"),
            "runner_id": "runner-test",
            "platform": "linux",
        }))
        .unwrap();
        (config, requests)
    }

    #[tokio::test]
    async fn a_result_upload_whose_connection_drops_is_sent_again() {
        // The platform sees the runner go away mid-upload (ClientDisconnect)
        // and records nothing: the runner sends the result again, and that
        // one lands.
        let (config, requests) = recording_platform(4096).await;
        let api = ApiClient::new(&config, "run-test".into()).unwrap();
        let outcome =
            CommandOutcome::completed(serde_json::json!({ "stdout": "x".repeat(1024 * 1024) }));
        api.submit_result("command-1", outcome).await.unwrap();
        let requests = requests.lock().unwrap();
        assert_eq!(requests.len(), 1, "the dropped attempt is not counted");
        assert!(requests[0].len() > 1024 * 1024, "{}", requests[0].len());
    }

    #[tokio::test]
    async fn a_result_upload_is_the_form_every_platform_version_reads() {
        // A `meta` text field holding `status` / `result` / `error` and at
        // most one `file` part: what the route reads.
        let (config, requests) = recording_platform(usize::MAX).await;
        let api = ApiClient::new(&config, "run-test".into()).unwrap();
        let outcome = CommandOutcome::completed_file(
            serde_json::json!({ "path": "/tmp/a.bin", "size": 3 }),
            FilePart {
                bytes: vec![1, 2, 3],
                filename: "a.bin".into(),
                content_type: "application/octet-stream".into(),
            },
        );
        api.submit_result("command-1", outcome).await.unwrap();

        let request = requests.lock().unwrap().pop().unwrap();
        let head_end = request.windows(4).position(|w| w == b"\r\n\r\n").unwrap();
        let head = String::from_utf8_lossy(&request[..head_end]).to_string();
        assert!(
            head.starts_with("POST /api/v1/runners/commands/command-1/result "),
            "{head}"
        );
        let boundary = head
            .lines()
            .find(|line| line.to_lowercase().starts_with("content-type:"))
            .and_then(|line| line.split_once("boundary="))
            .map(|(_, boundary)| boundary.trim().to_string())
            .unwrap();
        let body = String::from_utf8_lossy(&request[head_end + 4..]).to_string();
        let parts: Vec<&str> = body
            .split(&format!("--{boundary}"))
            .filter(|part| !part.is_empty() && !part.starts_with("--"))
            .collect();
        assert_eq!(parts.len(), 2, "{body}");
        let meta = parts[0]
            .strip_prefix("\r\nContent-Disposition: form-data; name=\"meta\"\r\n\r\n")
            .and_then(|rest| rest.strip_suffix("\r\n"))
            .unwrap();
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(meta).unwrap(),
            serde_json::json!({
                "status": "completed",
                "result": { "path": "/tmp/a.bin", "size": 3 },
            })
        );
        assert!(
            parts[1].starts_with(
                "\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.bin\"\r\n\
                 Content-Type: application/octet-stream\r\n\r\n\u{1}\u{2}\u{3}"
            ),
            "{:?}",
            parts[1]
        );
    }

    #[tokio::test]
    async fn a_restart_is_transient_on_poll_and_heartbeat_and_a_revocation_is_not() {
        let (config, _) = scripted_platform(vec![(503, Some("5")), (504, None), (401, None)]).await;
        let api = ApiClient::new(&config, "run-test".into()).unwrap();
        let (base, cap) = (Duration::from_secs(1), Duration::from_secs(30));
        let poll = api.poll().await.unwrap_err();
        assert!(is_transient(&poll), "{poll:#}");
        assert!(retry_delay(&poll, 0, base, cap) >= Duration::from_secs(5));
        let beat = api
            .heartbeat(&serde_json::json!({}), "0.0.0")
            .await
            .unwrap_err();
        assert!(is_transient(&beat), "{beat:#}");
        // A revoked runner still stops: the transport reads "revoked" as fatal.
        let revoked = api.poll().await.unwrap_err();
        assert!(!is_transient(&revoked), "{revoked:#}");
        assert!(revoked.to_string().contains("revoked"), "{revoked:#}");
    }
}
