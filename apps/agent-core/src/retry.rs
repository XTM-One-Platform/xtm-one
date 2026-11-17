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

//! Which failures a later attempt can succeed on, and how long to wait first.
//!
//! An agent talks to a platform that restarts (deploys, scale events): the
//! startup gate and the graceful drain answer 503 + `Retry-After`, a proxy in
//! front of it answers 502 / 504, and for a moment nothing answers at all.
//! None of that is fatal and none of it is worth a warning; a fleet that failed
//! together must not retry in lock-step either.

use std::future::Future;
use std::time::Duration;

use anyhow::{anyhow, Result};
use reqwest::header::{HeaderMap, RETRY_AFTER};
use reqwest::StatusCode;
use tokio::time::Instant;

/// Longest `Retry-After` honoured; a larger hint is read as this.
pub const MAX_RETRY_AFTER: Duration = Duration::from_secs(30);

/// The platform answered, but with a status worth retrying: it is restarting
/// (the startup gate and the graceful drain both answer 503 + `Retry-After`),
/// a proxy in front of it has no healthy backend (502 / 504), or it asked the
/// agent to slow down (429). Never fatal, and not worth a warning.
#[derive(Debug)]
pub struct Unavailable {
    pub status: StatusCode,
    pub retry_after: Option<Duration>,
}

impl std::fmt::Display for Unavailable {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "platform temporarily unavailable (HTTP {})", self.status)
    }
}

impl std::error::Error for Unavailable {}

/// Statuses a later attempt can succeed on: a timeout, rate limiting, and any
/// server-side failure. Every other 4xx (a revoked credential, an unknown or
/// already-finished command, a refused payload) would fail the same way again.
pub fn is_transient_status(status: StatusCode) -> bool {
    status == StatusCode::REQUEST_TIMEOUT
        || status == StatusCode::TOO_MANY_REQUESTS
        || status.is_server_error()
}

/// A `Retry-After` given in seconds, capped at `MAX_RETRY_AFTER`. The
/// HTTP-date form is ignored (the platform never sends it).
pub fn retry_after(headers: &HeaderMap) -> Option<Duration> {
    let secs: u64 = headers
        .get(RETRY_AFTER)?
        .to_str()
        .ok()?
        .trim()
        .parse()
        .ok()?;
    Some(Duration::from_secs(secs).min(MAX_RETRY_AFTER))
}

/// `Err(Unavailable)` for a transient status, so callers can tell a restart
/// from a real refusal; the response otherwise.
pub fn check_transient(resp: reqwest::Response) -> Result<reqwest::Response> {
    let status = resp.status();
    if is_transient_status(status) {
        return Err(anyhow!(Unavailable {
            status,
            retry_after: retry_after(resp.headers()),
        }));
    }
    Ok(resp)
}

/// Whether `err` is worth retrying: a transient status, or no answer at all
/// (connection refused / reset, timeout) - what a platform restart looks like.
pub fn is_transient(err: &anyhow::Error) -> bool {
    err.chain().any(|cause| {
        cause.downcast_ref::<Unavailable>().is_some()
            || cause
                .downcast_ref::<reqwest::Error>()
                .is_some_and(|e| e.is_connect() || e.is_timeout() || e.is_request())
    })
}

/// Exponential back-off with jitter: `base * 2^attempt`, capped, of which the
/// upper half is spread by `jitter` (in `[0, 1)`), so a fleet that failed
/// together does not retry in lock-step.
pub fn backoff_delay(attempt: u32, base: Duration, cap: Duration, jitter: f64) -> Duration {
    let ceiling = base
        .saturating_mul(2u32.saturating_pow(attempt.min(16)))
        .min(cap);
    let half = ceiling / 2;
    half + half.mul_f64(jitter.clamp(0.0, 1.0))
}

/// A uniform fraction in `[0, 1)` for back-off jitter.
///
/// Drawn from the standard library's randomly keyed hasher, so there is no
/// RNG dependency: every call gets fresh keys (the draws are independent), and
/// each machine of a fleet seeds them from its own OS randomness.
pub fn jitter() -> f64 {
    use std::hash::{BuildHasher, Hasher};
    let bits = std::collections::hash_map::RandomState::new()
        .build_hasher()
        .finish();
    // The top 53 bits are exact in an f64, so the fraction stays below 1.
    (bits >> 11) as f64 / (1u64 << 53) as f64
}

/// How long to wait before retrying after `err`, the `attempt`-th failure in
/// a row (from 0): see [`delay_for`].
pub fn retry_delay(err: &anyhow::Error, attempt: u32, base: Duration, cap: Duration) -> Duration {
    let retry_after = err
        .chain()
        .find_map(|cause| cause.downcast_ref::<Unavailable>())
        .and_then(|u| u.retry_after);
    delay_for(retry_after, attempt, base, cap, jitter(), jitter())
}

/// The jittered back-off for `attempt`, or, when the server's `Retry-After`
/// asks for longer, that floor plus a `spread` share of the back-off: the
/// floor alone would bring every agent the platform turned away at once back
/// at the same instant.
pub fn delay_for(
    retry_after: Option<Duration>,
    attempt: u32,
    base: Duration,
    cap: Duration,
    jitter: f64,
    spread: f64,
) -> Duration {
    let backoff = backoff_delay(attempt, base, cap, jitter);
    match retry_after {
        Some(floor) if floor >= backoff => floor + backoff.mul_f64(spread.clamp(0.0, 1.0)),
        _ => backoff,
    }
}

/// The shortest time an attempt is given before the budget ends.
const MIN_ATTEMPT_TIME: Duration = Duration::from_secs(1);

/// Run `attempt` until it succeeds, fails for good, or `budget` runs out.
///
/// Only a transient failure ([`is_transient`]) is retried, after
/// [`retry_delay`]. Each attempt is handed its timeout: `attempt_timeout` (the
/// client's usual request timeout), cut to what is left of the budget, so no
/// attempt runs past the budget and a hung one still leaves room for another.
/// A back-off that would end past the budget is cut short, so attempts go on
/// until `MIN_ATTEMPT_TIME` before the deadline: a platform back just before
/// the budget ends still gets the result. `what` names the call in the logs.
pub async fn retry_within<F, Fut>(
    what: &str,
    budget: Duration,
    attempt_timeout: Duration,
    base: Duration,
    cap: Duration,
    mut attempt: F,
) -> Result<()>
where
    F: FnMut(Duration) -> Fut,
    Fut: Future<Output = Result<()>>,
{
    let exhausted = || format!("still failing after {}s of retries", budget.as_secs());
    let deadline = Instant::now() + budget;
    let mut failures: u32 = 0;
    let mut last_err: Option<anyhow::Error> = None;
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            let err = last_err.unwrap_or_else(|| anyhow!("{what}: no time left to try"));
            return Err(err.context(exhausted()));
        }
        let err = match attempt(remaining.min(attempt_timeout)).await {
            Ok(()) => return Ok(()),
            Err(e) if !is_transient(&e) => return Err(e),
            Err(e) => e,
        };
        // A back-off that would end past the budget is cut short: retries go
        // on until the deadline, each attempt keeping at least a moment.
        let left = deadline.saturating_duration_since(Instant::now());
        if left <= MIN_ATTEMPT_TIME {
            return Err(err.context(exhausted()));
        }
        let wait = retry_delay(&err, failures, base, cap).min(left - MIN_ATTEMPT_TIME);
        tracing::debug!("{what} failed ({err:#}); retrying in {wait:?}");
        tokio::time::sleep(wait).await;
        failures = failures.saturating_add(1);
        last_err = Some(err);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_back_off_doubles_up_to_its_cap_and_spreads_its_upper_half() {
        let base = Duration::from_millis(500);
        let cap = Duration::from_secs(8);
        assert_eq!(backoff_delay(0, base, cap, 0.0), Duration::from_millis(250));
        assert_eq!(backoff_delay(0, base, cap, 1.0), Duration::from_millis(500));
        assert_eq!(backoff_delay(3, base, cap, 0.0), Duration::from_secs(2));
        assert_eq!(backoff_delay(40, base, cap, 1.0), cap);
    }

    #[test]
    fn a_longer_retry_after_is_a_floor_with_a_spread_on_top() {
        let base = Duration::from_millis(500);
        let cap = Duration::from_secs(8);
        let wait = delay_for(Some(Duration::from_secs(5)), 0, base, cap, 1.0, 1.0);
        assert_eq!(wait, Duration::from_millis(5500));
        let shorter = delay_for(Some(Duration::from_millis(10)), 0, base, cap, 1.0, 1.0);
        assert_eq!(shorter, Duration::from_millis(500));
    }

    #[test]
    fn jitter_stays_in_the_unit_interval() {
        for _ in 0..1000 {
            let j = jitter();
            assert!((0.0..1.0).contains(&j), "{j}");
        }
    }

    #[test]
    fn only_a_timeout_rate_limiting_or_a_server_failure_is_transient() {
        for code in [408, 429, 500, 502, 503, 504] {
            assert!(is_transient_status(StatusCode::from_u16(code).unwrap()));
        }
        for code in [400, 401, 403, 404, 409, 413, 422] {
            assert!(!is_transient_status(StatusCode::from_u16(code).unwrap()));
        }
    }

    #[tokio::test(start_paused = true)]
    async fn a_refusal_is_not_retried() {
        let mut calls = 0;
        let result = retry_within(
            "test",
            Duration::from_secs(10),
            Duration::from_secs(5),
            Duration::from_millis(100),
            Duration::from_secs(1),
            |_| {
                calls += 1;
                async { Err(anyhow!("refused")) }
            },
        )
        .await;
        assert!(result.is_err());
        assert_eq!(calls, 1);
    }

    #[tokio::test(start_paused = true)]
    async fn a_restart_is_ridden_out() {
        let mut calls = 0;
        let result = retry_within(
            "test",
            Duration::from_secs(10),
            Duration::from_secs(5),
            Duration::from_millis(100),
            Duration::from_secs(1),
            |_| {
                calls += 1;
                let attempt = calls;
                async move {
                    if attempt < 3 {
                        Err(anyhow!(Unavailable {
                            status: StatusCode::SERVICE_UNAVAILABLE,
                            retry_after: None,
                        }))
                    } else {
                        Ok(())
                    }
                }
            },
        )
        .await;
        assert!(result.is_ok());
        assert_eq!(calls, 3);
    }
}
