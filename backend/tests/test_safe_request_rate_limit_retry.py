"""Tests for the rate-limit retry logic in safe_request (#rate-limit-storm).

Validates that safe_request:
- Retries up to _RATE_LIMIT_MAX_RETRIES times on HTTP 429 responses.
- Applies decorrelated-jitter exponential backoff anchored at the
  ``Retry-After`` floor (so concurrent callers never reconverge to the
  same wake-up instant).
- Honours, clamps, and sanitises the ``Retry-After`` header (numeric,
  HTTP-date, NaN/inf, negative, oversize).
- Returns a rate-limit error string after all retries are exhausted and
  emits the ``"after N retries"`` log label on that path.
- Returns the success response when a retry eventually succeeds.
- Does not retry on other non-success status codes (4xx, 5xx).
- Retries a connection never established (any verb), never a timeout, and
  names the outage (since when, retries made) once the retries are spent.
- Keeps the ``(None, err)`` contract on failure by default, and only returns
  the failed ``httpx.Response`` when the caller opts in via
  ``expected_statuses`` (so it can branch on ``resp.status_code``).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.integrations import http as http_mod
from app.integrations.http import (
    _RATE_LIMIT_MAX_DELAY,
    _RATE_LIMIT_MAX_RETRIES,
    TRANSIENT_RETRY_STATUSES,
    _parse_retry_after,
    safe_request,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(status_code: int, headers: dict | None = None) -> httpx.Response:
    """Build a minimal httpx.Response stub."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.is_success = 200 <= status_code < 300
    resp.headers = httpx.Headers(headers or {})
    resp.text = ""
    resp.json = lambda: {}
    return resp


class _FakeClient:
    """Fake async HTTP client whose ``patch`` method returns pre-baked responses."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self._calls: list[tuple[str, dict]] = []

    async def _call(self, url: str, **kwargs: Any) -> httpx.Response:
        self._calls.append((url, kwargs))
        if not self._responses:
            raise RuntimeError("No more responses configured")
        return self._responses.pop(0)

    # safe_request dispatches through the verb-agnostic ``request()`` entry
    # point (so a body can ride on any verb), mirroring httpx.AsyncClient.
    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        return await self._call(url, **kwargs)

    # Verb helpers kept for any direct callers / parity with httpx.
    async def get(self, url: str, **kwargs) -> httpx.Response:
        return await self._call(url, **kwargs)

    async def post(self, url: str, **kwargs) -> httpx.Response:
        return await self._call(url, **kwargs)

    async def patch(self, url: str, **kwargs) -> httpx.Response:
        return await self._call(url, **kwargs)

    async def put(self, url: str, **kwargs) -> httpx.Response:
        return await self._call(url, **kwargs)

    async def delete(self, url: str, **kwargs) -> httpx.Response:
        return await self._call(url, **kwargs)


# ---------------------------------------------------------------------------
# Tests — _parse_retry_after helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("5", 5.0),
        ("0", 0.0),
        ("12.5", 12.5),
        # HTTP-date form (RFC 7231 §7.1.3) — must not raise, falls back to 0.
        ("Thu, 13 May 2026 12:35:00 GMT", 0.0),
        # Garbage values must also fall back gracefully.
        ("nope", 0.0),
        ("", 0.0),
        (None, 0.0),
        # Non-finite floats — asyncio.sleep raises on NaN and inf would
        # park the worker forever; must collapse to 0 instead.
        ("nan", 0.0),
        ("inf", 0.0),
        ("-inf", 0.0),
        # Negative values normalise to 0.
        ("-5", 0.0),
        # Oversize values clamp to _RATE_LIMIT_MAX_DELAY so a misconfigured
        # provider can't park us for 24 h.
        ("86400", _RATE_LIMIT_MAX_DELAY),
        (str(_RATE_LIMIT_MAX_DELAY + 1), _RATE_LIMIT_MAX_DELAY),
    ],
)
def test_parse_retry_after_handles_seconds_and_http_date(value, expected):
    """``_parse_retry_after`` returns the numeric seconds value when present,
    sanitises NaN / inf / negative values to ``0.0``, clamps oversize values
    to ``_RATE_LIMIT_MAX_DELAY``, and falls back to ``0.0`` for HTTP-date /
    unparseable / empty / missing headers so the retry loop can always rely
    on the helper instead of catching its own ``ValueError``."""
    assert _parse_retry_after(value) == expected


# ---------------------------------------------------------------------------
# Tests — safe_request behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retries_on_429_and_succeeds_on_third_attempt():
    """Two 429s followed by a 200 — should succeed with the third response."""
    responses = [
        _make_response(429),
        _make_response(429),
        _make_response(200),
    ]
    client = _FakeClient(responses)

    with patch("app.integrations.http.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        resp, err = await safe_request(client, "get", "https://example.com/x", "TestProvider", "test action")

    assert err is None
    assert resp is not None
    assert resp.status_code == 200
    # Must have slept exactly twice (once per 429 retry)
    assert mock_sleep.call_count == 2


@pytest.mark.asyncio
async def test_returns_error_after_all_retries_exhausted():
    """All retries return 429 — safe_request returns the rate-limit error string."""
    responses = [_make_response(429)] * (_RATE_LIMIT_MAX_RETRIES + 1)
    client = _FakeClient(responses)

    with patch("app.integrations.http.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        resp, err = await safe_request(client, "patch", "https://example.com/task", "Microsoft", "update task")

    # No expected_statuses opt-in → historical (None, err) contract preserved.
    assert resp is None
    assert err is not None
    assert "429" in err or "rate limit" in err.lower()
    # Slept _RATE_LIMIT_MAX_RETRIES times
    assert mock_sleep.call_count == _RATE_LIMIT_MAX_RETRIES


@pytest.mark.asyncio
async def test_respects_retry_after_header():
    """Retry-After: 5 on the 429 — sleep should be in ``[5, 5 + cap]``.

    The decorrelated-jitter implementation anchors the sleep at the
    Retry-After floor and spreads uniformly above it by up to the
    exponential-backoff cap (``min(_RATE_LIMIT_MAX_DELAY, base * 2^0) = 1``
    on the first retry), so the upper bound is the floor plus that cap.
    Both endpoints are reachable (``random.uniform`` returns ``a <= N <= b``),
    so the assertions below use ``<=`` on the upper bound.
    """
    responses = [
        _make_response(429, headers={"Retry-After": "5"}),
        _make_response(200),
    ]
    client = _FakeClient(responses)

    sleep_args: list[float] = []

    async def _capture_sleep(secs: float) -> None:
        sleep_args.append(secs)

    with patch("app.integrations.http.asyncio.sleep", side_effect=_capture_sleep):
        _resp, err = await safe_request(client, "get", "https://example.com/y", "TestProvider", "get resource")

    assert err is None
    assert len(sleep_args) == 1
    # Must wait at least the Retry-After value (the floor).
    assert sleep_args[0] >= 5.0
    # And never more than floor + cap (= 5 + 1 on the first retry).
    assert sleep_args[0] <= 6.0


@pytest.mark.asyncio
async def test_retry_after_decorrelated_jitter_spreads_concurrent_callers():
    """Eight concurrent callers seeing the same ``Retry-After`` value must
    draw different sleep durations — that's the whole point of the
    decorrelated jitter that replaced ``max(retry_after, jitter)``."""
    sleep_args: list[float] = []

    async def _capture_sleep(secs: float) -> None:
        sleep_args.append(secs)

    async def _run_once() -> None:
        client = _FakeClient([
            _make_response(429, headers={"Retry-After": "5"}),
            _make_response(200),
        ])
        await safe_request(client, "get", "https://example.com/burst", "TestProvider", "burst call")

    with patch("app.integrations.http.asyncio.sleep", side_effect=_capture_sleep):
        await asyncio.gather(*[_run_once() for _ in range(8)])

    # 8 callers × 1 retry each
    assert len(sleep_args) == 8
    # All sleeps respect the Retry-After floor and the cap
    assert all(5.0 <= s <= 6.0 for s in sleep_args)
    # Decorrelated jitter must produce diverse wake-up times — at least
    # half the values should be distinct (the assertion is purposely
    # loose to remain non-flaky on rare random clusterings, while still
    # catching a regression to the old ``max(retry_after, jitter)``
    # pattern which would collapse all 8 sleeps to exactly 5.0).
    assert len(set(sleep_args)) >= 4


@pytest.mark.asyncio
async def test_retry_after_is_clamped_to_max_delay():
    """A pathological ``Retry-After: 86400`` (24 h) must clamp to
    ``_RATE_LIMIT_MAX_DELAY`` so a misconfigured provider or hostile proxy
    can never park a worker for the full advertised delay."""
    responses = [
        _make_response(429, headers={"Retry-After": "86400"}),
        _make_response(200),
    ]
    client = _FakeClient(responses)

    sleep_args: list[float] = []

    async def _capture_sleep(secs: float) -> None:
        sleep_args.append(secs)

    with patch("app.integrations.http.asyncio.sleep", side_effect=_capture_sleep):
        _resp, err = await safe_request(client, "get", "https://example.com/clamp", "TestProvider", "clamp call")

    assert err is None
    assert len(sleep_args) == 1
    # Floor is clamped to MAX_DELAY (=30s), upper bound = MAX_DELAY + cap (= 30 + 1 on first retry).
    assert _RATE_LIMIT_MAX_DELAY <= sleep_args[0] <= _RATE_LIMIT_MAX_DELAY + 1.0


@pytest.mark.asyncio
async def test_retry_after_http_date_does_not_crash():
    """Retry-After as an HTTP-date string (RFC 7231 §7.1.3) must not raise
    ValueError — the retry loop must fall back to jitter-only and succeed."""
    responses = [
        _make_response(429, headers={"Retry-After": "Thu, 13 May 2026 12:35:00 GMT"}),
        _make_response(200),
    ]
    client = _FakeClient(responses)

    sleep_args: list[float] = []

    async def _capture_sleep(secs: float) -> None:
        sleep_args.append(secs)

    with patch("app.integrations.http.asyncio.sleep", side_effect=_capture_sleep):
        resp, err = await safe_request(client, "get", "https://example.com/date-retry", "TestProvider", "get resource")

    # Must not crash and must have retried once
    assert err is None
    assert resp is not None
    assert len(sleep_args) == 1
    # With an unparseable Retry-After the sleep falls back to jitter-only (>= 0)
    assert sleep_args[0] >= 0.0


@pytest.mark.asyncio
async def test_does_not_retry_on_403():
    """Non-429 error (403) — must return immediately without any retry."""
    responses = [_make_response(403)]
    client = _FakeClient(responses)

    with patch("app.integrations.http.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        resp, err = await safe_request(client, "get", "https://example.com/z", "TestProvider", "get resource")

    # No expected_statuses opt-in → historical (None, err) contract preserved.
    assert resp is None
    assert err is not None
    assert "access denied" in err.lower()
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_does_not_retry_on_500():
    """Server error (500) — must return immediately without any retry."""
    responses = [_make_response(500)]
    client = _FakeClient(responses)

    with patch("app.integrations.http.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        resp, err = await safe_request(client, "post", "https://example.com/q", "TestProvider", "create thing")

    # No expected_statuses opt-in → historical (None, err) contract preserved.
    assert resp is None
    assert err is not None
    assert "server error" in err.lower()
    mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — opt-in transient-error retry (retry_statuses)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", sorted(TRANSIENT_RETRY_STATUSES))
async def test_retries_transient_status_when_opted_in(status):
    """With ``retry_statuses=TRANSIENT_RETRY_STATUSES`` a 502/503/504 must be
    retried with backoff and succeed when the upstream recovers."""
    responses = [
        _make_response(status),
        _make_response(status),
        _make_response(200),
    ]
    client = _FakeClient(responses)

    with patch("app.integrations.http.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        resp, err = await safe_request(
            client,
            "get",
            "https://grafana.example.com/api/health",
            "Grafana",
            "health check",
            retry_statuses=TRANSIENT_RETRY_STATUSES,
        )

    assert err is None
    assert resp is not None
    assert resp.status_code == 200
    assert mock_sleep.call_count == 2


@pytest.mark.asyncio
async def test_transient_retry_exhaustion_returns_server_error():
    """A persistently failing upstream (all 503) must exhaust the retries and
    return the standard server-error string — never loop forever."""
    responses = [_make_response(503)] * (_RATE_LIMIT_MAX_RETRIES + 1)
    client = _FakeClient(responses)

    with patch("app.integrations.http.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        resp, err = await safe_request(
            client,
            "get",
            "https://grafana.example.com/api/search",
            "Grafana",
            "search",
            retry_statuses=TRANSIENT_RETRY_STATUSES,
        )

    assert resp is None
    assert err is not None
    assert "server error" in err.lower()
    assert "503" in err
    assert mock_sleep.call_count == _RATE_LIMIT_MAX_RETRIES


@pytest.mark.asyncio
async def test_transient_retry_honours_retry_after_on_503():
    """RFC 9110 permits ``Retry-After`` on 503 — the transient retry must
    anchor its sleep at that floor exactly like the 429 path."""
    responses = [
        _make_response(503, headers={"Retry-After": "5"}),
        _make_response(200),
    ]
    client = _FakeClient(responses)

    sleep_args: list[float] = []

    async def _capture_sleep(secs: float) -> None:
        sleep_args.append(secs)

    with patch("app.integrations.http.asyncio.sleep", side_effect=_capture_sleep):
        resp, err = await safe_request(
            client,
            "get",
            "https://grafana.example.com/api/org",
            "Grafana",
            "get org info",
            retry_statuses=TRANSIENT_RETRY_STATUSES,
        )

    assert err is None
    assert resp is not None
    assert len(sleep_args) == 1
    assert 5.0 <= sleep_args[0] <= 6.0


@pytest.mark.asyncio
async def test_no_transient_retry_without_opt_in():
    """Without ``retry_statuses`` a 502 keeps the historical no-retry
    behaviour — non-idempotent callers must never be retried implicitly."""
    responses = [_make_response(502)]
    client = _FakeClient(responses)

    with patch("app.integrations.http.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        resp, err = await safe_request(
            client,
            "post",
            "https://grafana.example.com/api/annotations",
            "Grafana",
            "create annotation",
        )

    assert resp is None
    assert err is not None
    assert "server error" in err.lower()
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_transient_retry_does_not_cover_unlisted_statuses():
    """``retry_statuses`` is an allow-list — a 500 (not in the transient set)
    must still fail immediately even when the caller opted in for 502/503/504."""
    responses = [_make_response(500)]
    client = _FakeClient(responses)

    with patch("app.integrations.http.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        resp, err = await safe_request(
            client,
            "get",
            "https://grafana.example.com/api/health",
            "Grafana",
            "health check",
            retry_statuses=TRANSIENT_RETRY_STATUSES,
        )

    assert resp is None
    assert err is not None
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_connect_error_is_retried_then_names_the_outage():
    """A connection never established is retried (any verb), then the error names since when."""
    calls = {"n": 0}

    class _ErrorClient:
        async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
            calls["n"] += 1
            raise httpx.ConnectError("connection refused")

    http_mod._UNREACHABLE_SINCE.pop("OutageProvider", None)
    with patch("app.integrations.http.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        resp, err = await safe_request(_ErrorClient(), "post", "https://example.com/net", "OutageProvider", "net call")

    assert resp is None
    assert err is not None
    assert "connect" in err.lower()
    assert calls["n"] == http_mod._CONNECT_MAX_RETRIES + 1
    assert mock_sleep.await_count == http_mod._CONNECT_MAX_RETRIES
    assert "Outage: OutageProvider unreachable from this worker since " in err
    assert f"{http_mod._CONNECT_MAX_RETRIES} retries made" in err
    assert "OutageProvider" in http_mod._UNREACHABLE_SINCE


@pytest.mark.asyncio
async def test_connect_error_then_answer_clears_the_outage():
    """A blip is absorbed by the retry and the first answer ends the outage."""
    answers: list = [httpx.ConnectError("refused"), _make_response(200)]

    class _BlipClient:
        async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

    with patch("app.integrations.http.asyncio.sleep", new_callable=AsyncMock):
        resp, err = await safe_request(_BlipClient(), "get", "https://example.com/net", "BlipProvider", "net call")

    assert err is None and resp is not None
    assert "BlipProvider" not in http_mod._UNREACHABLE_SINCE


@pytest.mark.asyncio
async def test_timeout_is_not_retried():
    """A timeout may have reached the provider: returned at once, no retry."""

    class _TimeoutClient:
        async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

    with patch("app.integrations.http.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        resp, err = await safe_request(_TimeoutClient(), "post", "https://example.com/net", "TestProvider", "net call")

    assert resp is None and err is not None
    assert "Outage" not in err
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_success_on_first_attempt_no_sleep():
    """200 on first try — no sleep, no retry."""
    responses = [_make_response(200)]
    client = _FakeClient(responses)

    with patch("app.integrations.http.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        resp, err = await safe_request(client, "get", "https://example.com/ok", "TestProvider", "get ok")

    assert err is None
    assert resp is not None
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_expected_status_suppresses_warning_but_returns_error():
    """A non-2xx status listed in ``expected_statuses`` (e.g. a benign 404 probe)
    must NOT emit the WARNING log, yet the error string is still returned so the
    caller can branch on it."""
    responses = [_make_response(404)]
    client = _FakeClient(responses)

    with patch("app.integrations.http._log_request_warning") as mock_warn:
        resp, err = await safe_request(
            client,
            "get",
            "https://example.com/probe",
            "Microsoft",
            "probe meeting",
            expected_statuses={404},
        )

    # The 404 response is returned so the caller can branch on the status.
    assert resp is not None
    assert resp.status_code == 404
    assert err is not None  # error string still surfaced to the caller
    mock_warn.assert_not_called()  # but the benign probe is kept out of the logs


@pytest.mark.asyncio
async def test_unexpected_status_still_warns_when_expected_statuses_set():
    """A non-2xx status NOT in ``expected_statuses`` must still emit the WARNING
    log — the suppression is scoped to the flagged statuses only. The failed
    response is still returned because the caller opted in via
    ``expected_statuses``."""
    responses = [_make_response(500)]
    client = _FakeClient(responses)

    with patch("app.integrations.http._log_request_warning") as mock_warn:
        resp, err = await safe_request(
            client,
            "get",
            "https://example.com/probe",
            "Microsoft",
            "probe meeting",
            expected_statuses={404},
        )

    # Opted in via expected_statuses → the failed response is returned even for
    # an out-of-set status, so the caller can inspect resp.status_code.
    assert resp is not None
    assert resp.status_code == 500
    assert err is not None
    mock_warn.assert_called_once()


@pytest.mark.asyncio
async def test_failed_response_only_returned_when_opted_in():
    """Pin the opt-in contract: the same 500 failure returns
    ``(None, err)`` without ``expected_statuses`` (preserving the historical
    contract so ``resp is None`` callers keep working) and ``(resp, err)`` once
    the caller opts in via ``expected_statuses``."""
    # Default: no opt-in → resp is None.
    resp, err = await safe_request(
        _FakeClient([_make_response(500)]),
        "get",
        "https://example.com/x",
        "Microsoft",
        "get resource",
    )
    assert resp is None
    assert err is not None

    # Opted in → the failed response is surfaced for status branching.
    resp, err = await safe_request(
        _FakeClient([_make_response(500)]),
        "get",
        "https://example.com/x",
        "Microsoft",
        "get resource",
        expected_statuses={404},
    )
    assert resp is not None
    assert resp.status_code == 500
    assert err is not None


@pytest.mark.asyncio
async def test_empty_expected_statuses_returns_response_and_still_warns():
    """An **empty** ``expected_statuses`` set opts into receiving the failed
    response (for status branching) without classifying any status as
    "expected" — every non-2xx failure must still emit the WARNING log.
    This is the idiom merge_github_pr uses so 405 merge failures stay
    visible in logs/monitoring."""
    responses = [_make_response(405)]
    client = _FakeClient(responses)

    with patch("app.integrations.http._log_request_warning") as mock_warn:
        resp, err = await safe_request(
            client,
            "put",
            "https://example.com/merge",
            "GitHub",
            "merge PR",
            expected_statuses=set(),
        )

    # The failed response is returned so the caller can branch on 405...
    assert resp is not None
    assert resp.status_code == 405
    assert err is not None
    # ...but the failure is NOT silenced — it still hits the warning log.
    mock_warn.assert_called_once()


@pytest.mark.asyncio
async def test_unexpected_status_warns_when_no_expected_statuses():
    """Without ``expected_statuses`` the default behaviour is unchanged: every
    non-2xx error is logged at WARNING."""
    responses = [_make_response(404)]
    client = _FakeClient(responses)

    with patch("app.integrations.http._log_request_warning") as mock_warn:
        resp, err = await safe_request(
            client,
            "get",
            "https://example.com/x",
            "Microsoft",
            "get resource",
        )

    # No expected_statuses opt-in → historical (None, err) contract preserved.
    assert resp is None
    assert err is not None
    mock_warn.assert_called_once()


@pytest.mark.asyncio
async def test_microsoft_concurrency_semaphore_limits_parallel_calls():
    """Microsoft build_tools uses a semaphore so at most _MICROSOFT_CONCURRENCY
    Graph API calls run simultaneously.  We verify that 10 concurrent tool calls
    never exceed the configured limit in-flight at the same time."""
    from app.integrations.providers.microsoft import build_tools
    from app.integrations.providers.microsoft._common import _MICROSOFT_CONCURRENCY
    from app.tools.base import CallableToolExecutor, ToolCall

    active: list[int] = []
    peak: list[int] = []

    async def _fake_request(client, method, url, provider, action, **kwargs):
        active.append(1)
        peak.append(sum(active))
        # Simulate a small delay so calls overlap
        await asyncio.sleep(0.01)
        active.pop()
        return _make_response(200), None

    from app.integrations.providers import microsoft as ms_mod

    original = ms_mod._common.safe_request
    ms_mod._common.safe_request = _fake_request  # type: ignore[assignment]
    try:
        executor = CallableToolExecutor()
        build_tools(token="dummy", executor=executor)

        # Fire 10 concurrent update_microsoft_task calls
        tasks = [
            executor.execute_tool(
                ToolCall(
                    id=f"call-{i}",
                    name="update_microsoft_task",
                    arguments={"list_id": "list1", "task_id": f"task{i}", "title": f"T{i}"},
                )
            )
            for i in range(10)
        ]
        results = await asyncio.gather(*tasks)
    finally:
        ms_mod._common.safe_request = original  # type: ignore[assignment]

    # All calls complete
    assert len(results) == 10
    # Peak concurrent in-flight calls must not exceed the semaphore limit
    assert max(peak) <= _MICROSOFT_CONCURRENCY
