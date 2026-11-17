"""Tests for the shared rate limiter that paces HubSpot API calls.

HubSpot's CRM Search API is capped at 5 requests/second per account, shared
across every user and integration in the portal. A burst of parallel agent
tool calls hitting ``POST /crm/v3/objects/{type}/search`` used to fire
simultaneously, breach the cap in the same instant, and each independently
retry -- a self-amplifying 429 storm. ``RateLimiter`` (in
``app.integrations.http``) smooths the burst with a token bucket plus an
optional concurrency cap, and HubSpot's ``build_tools`` funnels every request
for an account through one shared limiter keyed by a secret-free hash of the
token.

These tests cover:
  - the generic ``RateLimiter`` token-bucket pacing (deterministic fake clock),
  - its initial-burst (capacity) behaviour,
  - its optional concurrency cap,
  - ``get_shared_rate_limiter`` registry identity (same key -> same instance),
  - HubSpot ``build_tools`` wiring: concurrent search calls never exceed the
    configured in-flight concurrency.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import httpx
import pytest

from app.integrations.http import RateLimiter, get_shared_rate_limiter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeClock:
    """Deterministic monotonic clock whose ``sleep`` advances time.

    Lets the token-bucket pacing tests assert exact wait durations without any
    real sleeping (and without flakiness).
    """

    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _ok_response(payload: dict) -> httpx.Response:
    """Build a minimal successful httpx.Response stub returning *payload*."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.is_success = True
    resp.headers = httpx.Headers({})
    resp.text = ""
    resp.json = lambda: payload
    return resp


# ---------------------------------------------------------------------------
# RateLimiter -- token-bucket pacing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_bucket_paces_sequential_acquisitions():
    """capacity=1, rate=4/s -> first slot is free, each later slot waits 0.25s."""
    clock = _FakeClock()
    limiter = RateLimiter(
        rate=4.0, capacity=1.0, time_fn=clock.time, sleep_fn=clock.sleep
    )
    for _ in range(5):
        async with limiter.slot():
            pass
    # 1 immediate token + 4 refills at 1/4 s each = 1.0 s of paced waiting.
    assert clock.now == pytest.approx(1.0, abs=1e-6)


@pytest.mark.asyncio
async def test_token_bucket_allows_initial_burst_up_to_capacity():
    """capacity=3 -> the first three slots are free before pacing kicks in."""
    clock = _FakeClock()
    limiter = RateLimiter(
        rate=2.0, capacity=3.0, time_fn=clock.time, sleep_fn=clock.sleep
    )
    for _ in range(3):
        async with limiter.slot():
            pass
    # The initial burst (= capacity) consumes no wait time.
    assert clock.now == pytest.approx(0.0, abs=1e-6)
    async with limiter.slot():
        pass
    # The 4th slot waits 1/rate = 0.5 s for the next token.
    assert clock.now == pytest.approx(0.5, abs=1e-6)


@pytest.mark.asyncio
async def test_token_bucket_caps_burst_at_capacity_not_elapsed_idle():
    """Idle time never lets the bucket accrue beyond ``capacity`` tokens."""
    clock = _FakeClock()
    limiter = RateLimiter(
        rate=5.0, capacity=2.0, time_fn=clock.time, sleep_fn=clock.sleep
    )
    # Simulate a long idle period before any request.
    clock.now = 100.0
    # Only ``capacity`` (=2) slots may fire for free despite 100 s of idle.
    async with limiter.slot():
        pass
    async with limiter.slot():
        pass
    assert clock.now == pytest.approx(100.0, abs=1e-6)
    # The 3rd must wait for a fresh token.
    async with limiter.slot():
        pass
    assert clock.now == pytest.approx(100.0 + 1.0 / 5.0, abs=1e-6)


# ---------------------------------------------------------------------------
# RateLimiter -- concurrency cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrency_cap_limits_in_flight_slots():
    """With a huge rate (no pacing delay) the semaphore caps in-flight slots."""
    limiter = RateLimiter(rate=1e6, capacity=1e6, max_concurrency=3)
    active = 0
    peak = 0

    async def _job() -> None:
        nonlocal active, peak
        async with limiter.slot():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*[_job() for _ in range(12)])
    assert peak <= 3


@pytest.mark.asyncio
async def test_no_concurrency_cap_allows_unbounded_in_flight():
    """Without ``max_concurrency`` the limiter only paces rate, not concurrency."""
    limiter = RateLimiter(rate=1e6, capacity=1e6)
    active = 0
    peak = 0

    async def _job() -> None:
        nonlocal active, peak
        async with limiter.slot():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*[_job() for _ in range(8)])
    # All 8 ran concurrently (no semaphore in play).
    assert peak == 8


@pytest.mark.asyncio
async def test_slot_does_not_consume_tokens_while_blocked_on_concurrency():
    """A caller blocked on the concurrency semaphore must not pre-consume a token.

    Regression for the ``slot()`` acquire order: the token is consumed *after*
    the semaphore, not before. If tokens were taken first, callers queued on a
    full semaphore would burn tokens while waiting and then start in a burst the
    instant a slot frees -- so the token bucket would stop pacing request
    *starts* (the very storm the limiter exists to prevent). Here ``capacity``
    is generous and ``rate`` low (negligible refill), so the assertion isolates
    the acquire ordering rather than the bucket's own pacing.
    """
    limiter = RateLimiter(rate=1.0, capacity=10.0, max_concurrency=1)
    hold = asyncio.Event()
    started = 0

    async def _job() -> None:
        nonlocal started
        async with limiter.slot():
            started += 1
            await hold.wait()

    # One job takes the single concurrency slot and parks in flight; the other
    # three must queue on the semaphore.
    tasks = [asyncio.create_task(_job()) for _ in range(4)]
    # Let every task run up to its blocking await (in-flight hold or semaphore).
    await asyncio.sleep(0.05)
    try:
        # Only the in-flight holder has started, and only its one token has been
        # consumed -- the three queued callers must not have taken tokens while
        # blocked on the semaphore (10 capacity - 1 consumed = 9 remaining).
        assert started == 1
        assert limiter._tokens == pytest.approx(9.0, abs=1e-6)
    finally:
        hold.set()
        await asyncio.gather(*tasks)


class _RefusedError(Exception):
    pass


@pytest.mark.asyncio
async def test_admit_runs_once_the_slot_is_held_and_before_a_token_is_spent():
    """``slot(admit=...)``: a caller whose request became pointless while
    it queued - its target started refusing it - leaves the moment it holds a
    slot, without waiting for the bucket. The refusal releases the slot."""
    clock = _FakeClock()
    limiter = RateLimiter(
        rate=1.0, capacity=1.0, max_concurrency=1, time_fn=clock.time, sleep_fn=clock.sleep
    )
    admitted: list[str] = []
    hold = asyncio.Event()
    holding = asyncio.Event()

    async def _holder() -> None:
        async with limiter.slot():
            holding.set()
            await hold.wait()

    def _refuse() -> None:
        admitted.append("asked")
        raise _RefusedError

    async def _queued() -> None:
        async with limiter.slot(admit=_refuse):
            pytest.fail("a refused caller never runs its block")

    holder = asyncio.create_task(_holder())
    await holding.wait()
    queued = asyncio.create_task(_queued())
    await asyncio.sleep(0.01)
    assert admitted == [], "admit waits for the slot: it is not a pre-check"
    assert limiter.in_use == 2
    hold.set()
    await holder
    with pytest.raises(_RefusedError):
        await queued
    assert admitted == ["asked"]
    assert clock.now == 0.0, "refused before the next token was waited for"
    assert limiter.in_use == 0
    async with limiter.slot(admit=lambda: None):
        pass
    assert clock.now == pytest.approx(1.0), "the slot was released: the next caller only waits for its token"


# ---------------------------------------------------------------------------
# get_shared_rate_limiter -- registry identity
# ---------------------------------------------------------------------------


def test_shared_limiter_same_key_returns_same_instance():
    a = get_shared_rate_limiter("test:key:alpha", rate=1.0, capacity=1.0)
    # Later config is ignored -- the first instance is reused for the key.
    b = get_shared_rate_limiter("test:key:alpha", rate=99.0, capacity=99.0)
    c = get_shared_rate_limiter("test:key:beta", rate=1.0, capacity=1.0)
    assert a is b
    assert a is not c


@pytest.mark.asyncio
async def test_a_full_registry_never_evicts_a_limiter_a_caller_is_inside(monkeypatch):
    """Eviction drops the idlest limiters, never one with a caller inside
    (waiting for a slot or holding one): the next caller for that key would
    build a second limiter beside it and double the key's concurrency. One
    key per image host is what makes the cap reachable."""
    from app.integrations import http as integration_http

    monkeypatch.setattr(integration_http, "_RATE_LIMITER_REGISTRY", {})
    monkeypatch.setattr(integration_http, "_RATE_LIMITER_REGISTRY_MAX", 3)
    busy = get_shared_rate_limiter("test:busy", rate=1e6, capacity=1e6, max_concurrency=1)
    hold = asyncio.Event()
    holding = asyncio.Event()

    async def _holder() -> None:
        async with busy.slot():
            holding.set()
            await hold.wait()

    task = asyncio.create_task(_holder())
    await holding.wait()
    try:
        # ``busy`` refilled first: by ``last_refill`` alone it is the victim.
        get_shared_rate_limiter("test:idle", rate=1.0, capacity=1.0)
        get_shared_rate_limiter("test:recent", rate=1.0, capacity=1.0)
        get_shared_rate_limiter("test:new", rate=1.0, capacity=1.0)
        assert get_shared_rate_limiter("test:busy", rate=1e6, capacity=1e6, max_concurrency=1) is busy
        assert set(integration_http._RATE_LIMITER_REGISTRY) == {"test:busy", "test:recent", "test:new"}
    finally:
        hold.set()
        await task
    assert busy.in_use == 0


# ---------------------------------------------------------------------------
# HubSpot build_tools wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hubspot_search_calls_share_concurrency_cap(monkeypatch):
    """Concurrent HubSpot search tool calls must not exceed the configured
    in-flight concurrency -- the shared per-account limiter throttles them.

    Mirrors ``test_microsoft_concurrency_semaphore_limits_parallel_calls`` but
    targets the HubSpot deal-search endpoint.
    """
    import app.integrations.providers.hubspot as hs
    from app.tools.base import CallableToolExecutor, ToolCall

    # High rate so the token bucket never delays -- isolate the concurrency cap.
    monkeypatch.setattr(hs, "_HUBSPOT_MAX_RPS", 100_000.0)
    monkeypatch.setattr(hs, "_HUBSPOT_BURST", 100_000.0)
    monkeypatch.setattr(hs, "_HUBSPOT_CONCURRENCY", 3)

    active = 0
    peak = 0

    async def _fake_request(client, method, url, provider, action, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        # Simulate latency so the calls overlap in flight.
        await asyncio.sleep(0.01)
        active -= 1
        return _ok_response({"results": [], "total": 0, "paging": {}}), None

    # _throttled wraps safe_request with the limiter slot, so patching
    # safe_request still exercises the limiter (the slot is acquired first).
    monkeypatch.setattr(hs, "safe_request", _fake_request)

    executor = CallableToolExecutor()
    # Unique token -> fresh registry key (no reuse of a cached limiter).
    hs.build_tools(token="rate-limit-test-token-1868", executor=executor)

    tasks = [
        executor.execute_tool(
            ToolCall(
                id=f"call-{i}",
                name="search_hubspot_deals",
                arguments={"query": f"q{i}"},
            )
        )
        for i in range(12)
    ]
    results = await asyncio.gather(*tasks)

    assert len(results) == 12
    # Peak concurrent in-flight calls must not exceed the limiter's cap.
    assert peak <= 3
