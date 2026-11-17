"""Tests for cross-run sustained stale Notion reference detection.

When the same Notion resource (database, page, block) returns 404 across
*separate* agent runs for the same integration, a WARNING should be emitted
after ``_STALE_REF_WARN_THRESHOLD`` occurrences so operators can identify
permanently-deleted or un-shared Notion IDs.

The counter is stored in Redis, keyed by integration ID + normalised resource
ID, with a sliding TTL.  The ``_bump_stale_ref`` coroutine is awaited by the
request; clearing the counter of a recovered resource is still
fire-and-forget (``asyncio.ensure_future``), so tests use
``await asyncio.sleep(0)`` to let that task run before asserting on it.

Pre-call suppression: once the counter reaches ``_STALE_REF_WARN_THRESHOLD``
a Redis GET is issued *before* the HTTP call on subsequent runs so the API call
can be skipped entirely — saving the round-trip and returning a clear "stale"
error to the caller immediately.  Recovery: when the last failing bump is older
than ``_STALE_REF_RECHECK_SECS`` (derived from the key's remaining TTL) the
guard stands down and allows a live probe so a restored resource self-heals.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.integrations.providers.notion._common import (
    _STALE_REF_RECHECK_SECS,
    _STALE_REF_REDIS_KEY_PREFIX,
    _STALE_REF_REDIS_TTL_SECS,
    _STALE_REF_WARN_THRESHOLD,
    NotionContext,
)

# ── helpers ──────────────────────────────────────────────────────────────────


def _make_fake_redis(initial_count: int = 0):
    """Return a minimal fake Redis object whose ``incr`` simulates a counter."""
    counts: dict[str, int] = {}

    async def _incr(key: str) -> int:
        counts[key] = counts.get(key, initial_count) + 1
        return counts[key]

    fake = MagicMock()
    fake.incr = _incr
    fake.expire = AsyncMock()
    fake.delete = AsyncMock(return_value=1)
    fake._counts = counts  # expose for assertions
    return fake


async def _one_run_404(ctx: NotionContext, url: str) -> None:
    """Simulate a single agent run that hits a 404 for *url*."""
    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request:
        mock_request.return_value = (
            None,
            "Error: Notion resource not found while trying to resolve data source.",
        )
        async with ctx.client() as client:
            await ctx.request(client, "get", url, "Notion", "resolve data source")
    # Let any fire-and-forget counter clear complete.
    await asyncio.sleep(0)


# ── tests ─────────────────────────────────────────────────────────────────────


def test_stale_ref_warn_threshold_keeps_fast_detection():
    """Hourly automations should not wait many failing runs before suppression."""
    assert _STALE_REF_WARN_THRESHOLD == 2


@pytest.mark.asyncio
async def test_stale_ref_counter_incremented_on_404():
    """Redis counter is incremented once per run when a resource first 404s."""
    url = "https://api.notion.com/v1/databases/3118fce1-7f2a-803b-acb2-000be1f827c2"
    integration_id = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"
    fake_redis = _make_fake_redis()

    with patch(
        "app.core.redis.redis_client",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        # Each new NotionContext represents a fresh run (in-run cache is empty).
        ctx1 = NotionContext("token", integration_id=integration_id)
        await _one_run_404(ctx1, url)

        ctx2 = NotionContext("token", integration_id=integration_id)
        await _one_run_404(ctx2, url)

    # Two separate runs → counter incremented twice.
    assert any(v == 2 for v in fake_redis._counts.values())


@pytest.mark.asyncio
async def test_stale_ref_counter_not_incremented_within_same_run():
    """Multiple 404s on the same resource within ONE run only count once.

    The second call is short-circuited by the in-run missing-resource cache
    (``_cached_missing_resource_error``), so ``_bump_stale_ref`` is never
    triggered a second time.
    """
    url = "https://api.notion.com/v1/databases/3118fce1-7f2a-803b-acb2-000be1f827c2"
    integration_id = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"
    fake_redis = _make_fake_redis()

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request, patch(
        "app.core.redis.redis_client",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        mock_request.return_value = (
            None,
            "Error: Notion resource not found while trying to resolve data source.",
        )
        ctx = NotionContext("token", integration_id=integration_id)
        async with ctx.client() as client:
            # Two calls within the SAME run / same context instance.
            await ctx.request(client, "get", url, "Notion", "resolve data source")
            await ctx.request(client, "get", url, "Notion", "resolve data source")
        await asyncio.sleep(0)

    # Only one call reached safe_request (second was short-circuited).
    assert mock_request.call_count == 1
    # Counter incremented only once for the run, not twice.
    assert all(v == 1 for v in fake_redis._counts.values())


@pytest.mark.asyncio
async def test_same_id_probed_as_multiple_types_counts_once_per_run():
    """Probing the same dead ID as database, page, AND block in one run bumps once.

    The in-run missing cache is keyed per (resource_type, id), so each probe is
    a distinct cache entry — but they are the same stale reference. Without the
    per-run ``_stale_ref_bumped`` dedup, a single run would bump the shared
    Redis counter three times and reach the warn threshold immediately,
    defeating the cross-run semantics.
    """
    resource_id = "3118fce1-7f2a-803b-acb2-000be1f827c2"
    urls = [
        f"https://api.notion.com/v1/databases/{resource_id}",
        f"https://api.notion.com/v1/pages/{resource_id}",
        f"https://api.notion.com/v1/blocks/{resource_id}/children",
    ]
    integration_id = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"
    fake_redis = _make_fake_redis()

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request, patch(
        "app.core.redis.redis_client",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        mock_request.return_value = (
            None,
            "Error: Notion resource not found while trying to resolve data source.",
        )
        ctx = NotionContext("token", integration_id=integration_id)
        async with ctx.client() as client:
            for url in urls:
                await ctx.request(client, "get", url, "Notion", "resolve data source")
        await asyncio.sleep(0)

    # All three probes reached safe_request (distinct cache keys per type)...
    assert mock_request.call_count == 3
    # ...but the shared cross-run counter was bumped exactly once.
    assert list(fake_redis._counts.values()) == [1]


@pytest.mark.asyncio
async def test_sustained_stale_warning_emitted_at_threshold(caplog):
    """A WARNING is logged when the counter reaches _STALE_REF_WARN_THRESHOLD."""
    url = "https://api.notion.com/v1/databases/3118fce1-7f2a-803b-acb2-000be1f827c2"
    integration_id = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"
    # Start the counter at threshold - 1 so the very next run crosses the line.
    fake_redis = _make_fake_redis(initial_count=_STALE_REF_WARN_THRESHOLD - 1)

    with patch(
        "app.core.redis.redis_client",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        ctx = NotionContext("token", integration_id=integration_id)
        with caplog.at_level(logging.WARNING, logger="app.integrations.providers.notion"):
            await _one_run_404(ctx, url)

    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    stale_warnings = [
        r for r in warning_records if "sustained stale reference" in r.message.lower()
    ]
    assert stale_warnings, "Expected a sustained-stale-reference WARNING at threshold"
    assert "3118fce1" in stale_warnings[0].message
    assert integration_id in stale_warnings[0].message


@pytest.mark.asyncio
async def test_no_warning_below_threshold(caplog):
    """No sustained-stale WARNING before the threshold is reached."""
    url = "https://api.notion.com/v1/databases/aabbcc11-0000-0000-0000-000000000001"
    integration_id = "00000000-0000-0000-0000-000000000001"
    # Counter will go from 0 → 1 (below threshold).
    fake_redis = _make_fake_redis(initial_count=0)

    with patch(
        "app.core.redis.redis_client",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        ctx = NotionContext("token", integration_id=integration_id)
        with caplog.at_level(logging.WARNING, logger="app.integrations.providers.notion"):
            await _one_run_404(ctx, url)

    stale_warnings = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "sustained stale reference" in r.message.lower()
    ]
    assert not stale_warnings, "No sustained-stale WARNING expected below threshold"


@pytest.mark.asyncio
async def test_no_counter_without_integration_id():
    """No Redis call is made when NotionContext has no integration_id."""
    url = "https://api.notion.com/v1/databases/3118fce1-7f2a-803b-acb2-000be1f827c2"
    fake_redis = _make_fake_redis()

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request, patch(
        "app.core.redis.redis_client",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        mock_request.return_value = (
            None,
            "Error: Notion resource not found while trying to resolve data source.",
        )
        # No integration_id — no Redis counter expected.
        ctx = NotionContext("token")
        async with ctx.client() as client:
            await ctx.request(client, "get", url, "Notion", "resolve data source")
        await asyncio.sleep(0)

    assert not fake_redis._counts, "Redis must not be touched when integration_id is absent"


@pytest.mark.asyncio
async def test_redis_failure_does_not_raise():
    """A Redis error in _bump_stale_ref is silently swallowed."""
    url = "https://api.notion.com/v1/databases/3118fce1-7f2a-803b-acb2-000be1f827c2"
    integration_id = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"

    async def _broken_redis():
        raise ConnectionError("Redis unavailable")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request, patch(
        "app.core.redis.redis_client",
        side_effect=_broken_redis,
    ):
        mock_request.return_value = (
            None,
            "Error: Notion resource not found while trying to resolve data source.",
        )
        ctx = NotionContext("token", integration_id=integration_id)
        async with ctx.client() as client:
            # Must not raise even though Redis is broken.
            _resp, err = await ctx.request(client, "get", url, "Notion", "resolve data source")
        await asyncio.sleep(0)

    assert err is not None
    assert "resource not found" in err.lower()


@pytest.mark.asyncio
async def test_redis_ttl_refreshed_on_each_bump():
    """expire() is called on every bump to keep the counter's TTL sliding."""
    url = "https://api.notion.com/v1/pages/aaaabbbb-0000-0000-0000-000000000001"
    integration_id = "00000000-0000-0000-0000-000000000002"
    fake_redis = _make_fake_redis()

    with patch(
        "app.core.redis.redis_client",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        for _ in range(2):
            ctx = NotionContext("token", integration_id=integration_id)
            await _one_run_404(ctx, url)

    assert fake_redis.expire.call_count == 2
    for call in fake_redis.expire.call_args_list:
        assert call.args[1] == _STALE_REF_REDIS_TTL_SECS


@pytest.mark.asyncio
async def test_redis_key_includes_integration_and_resource_id():
    """Redis key encodes both the integration ID and the normalised resource ID."""
    url = "https://api.notion.com/v1/databases/3118fce1-7f2a-803b-acb2-000be1f827c2"
    integration_id = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"
    fake_redis = _make_fake_redis()

    with patch(
        "app.core.redis.redis_client",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        ctx = NotionContext("token", integration_id=integration_id)
        await _one_run_404(ctx, url)

    assert fake_redis._counts, "Expected at least one Redis INCR call"
    key = next(iter(fake_redis._counts))
    assert key.startswith(_STALE_REF_REDIS_KEY_PREFIX)
    assert integration_id in key
    # Normalised UUID (no dashes) in key
    assert "3118fce17f2a803bacb2000be1f827c2" in key


@pytest.mark.asyncio
async def test_request_meta_fires_stale_ref_counter(caplog):
    """request_meta() also increments the cross-run counter on 404."""
    url = "https://api.notion.com/v1/databases/3118fce1-7f2a-803b-acb2-000be1f827c2"
    integration_id = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"
    fake_redis = _make_fake_redis(initial_count=_STALE_REF_WARN_THRESHOLD - 1)

    fake_resp = httpx.Response(
        404,
        json={
            "object": "error",
            "status": 404,
            "code": "object_not_found",
            "message": "Could not find database with ID: 3118fce1-7f2a-803b-acb2-000be1f827c2.",
        },
    )

    with patch(
        "app.core.redis.redis_client",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        ctx = NotionContext("token", integration_id=integration_id)
        with caplog.at_level(logging.WARNING, logger="app.integrations.providers.notion"):
            async with ctx.client() as client:
                with patch.object(client, "get", return_value=fake_resp):
                    _, _, status_code, _ = await ctx.request_meta(
                        client, "get", url, "Notion", "resolve data source"
                    )
            await asyncio.sleep(0)

    assert status_code == 404
    stale_warnings = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "sustained stale reference" in r.message.lower()
    ]
    assert stale_warnings, "request_meta() should trigger the stale-ref WARNING too"


# ── pre-call suppression tests ───────────────────────────────────────


def _make_fake_redis_at_count(count: int, *, ttl: int = _STALE_REF_REDIS_TTL_SECS):
    """Return a fake Redis whose pipelined GET/TTL simulates an existing counter.

    Used to simulate a Redis counter that has already accumulated *count*
    failures in previous runs, so the pre-call stale check can decide whether
    to suppress the upcoming API call.  ``ttl`` is the counter key's remaining
    TTL — the default (full TTL) simulates a bump that just happened, i.e. a
    resource well inside the suppression window.

    The pre-check issues ONE pipelined round-trip (GET + TTL); assert on
    ``fake._pipe.execute.call_count`` to count Redis round-trips.
    """
    fake = MagicMock()
    pipe = MagicMock()
    pipe.get = MagicMock()
    pipe.ttl = MagicMock()
    pipe.execute = AsyncMock(return_value=[str(count).encode() if count else None, ttl])
    fake.pipeline = MagicMock(return_value=pipe)
    fake._pipe = pipe  # expose for assertions
    fake.incr = AsyncMock(return_value=count + 1)
    fake.expire = AsyncMock()
    fake.delete = AsyncMock(return_value=1)
    return fake


@pytest.mark.asyncio
async def test_api_call_suppressed_when_counter_at_threshold():
    """safe_request is NOT called when the Redis counter equals _STALE_REF_WARN_THRESHOLD.

    A resource that has already failed in _STALE_REF_WARN_THRESHOLD separate
    runs should have its next call suppressed without touching the Notion API.
    """
    url = "https://api.notion.com/v1/databases/2468fce1-7f2a-8051-b06a-dd7b6a9c3898"
    integration_id = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"
    fake_redis = _make_fake_redis_at_count(_STALE_REF_WARN_THRESHOLD)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request, patch(
        "app.core.redis.redis_client",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        ctx = NotionContext("token", integration_id=integration_id)
        async with ctx.client() as client:
            _, err = await ctx.request(client, "get", url, "Notion", "resolve data source")

    mock_request.assert_not_called()
    assert err is not None
    assert "stale" in err.lower()
    assert "suppressed" in err.lower()


@pytest.mark.asyncio
async def test_api_call_suppressed_when_counter_above_threshold():
    """safe_request is NOT called when the Redis counter exceeds _STALE_REF_WARN_THRESHOLD."""
    url = "https://api.notion.com/v1/databases/2468fce1-7f2a-8051-b06a-dd7b6a9c3898"
    integration_id = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"
    fake_redis = _make_fake_redis_at_count(_STALE_REF_WARN_THRESHOLD + 5)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request, patch(
        "app.core.redis.redis_client",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        ctx = NotionContext("token", integration_id=integration_id)
        async with ctx.client() as client:
            _, err = await ctx.request(client, "get", url, "Notion", "resolve data source")

    mock_request.assert_not_called()
    assert err is not None


@pytest.mark.asyncio
async def test_api_call_proceeds_when_counter_below_threshold():
    """safe_request IS called when the Redis counter is below _STALE_REF_WARN_THRESHOLD."""
    url = "https://api.notion.com/v1/databases/2468fce1-7f2a-8051-b06a-dd7b6a9c3898"
    integration_id = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"
    fake_redis = _make_fake_redis_at_count(_STALE_REF_WARN_THRESHOLD - 1)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request, patch(
        "app.core.redis.redis_client",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        mock_request.return_value = (MagicMock(is_success=True, json=lambda: {}), None)
        ctx = NotionContext("token", integration_id=integration_id)
        async with ctx.client() as client:
            _, err = await ctx.request(client, "get", url, "Notion", "resolve data source")

    mock_request.assert_called_once()
    assert err is None


@pytest.mark.asyncio
async def test_api_call_proceeds_when_redis_unavailable_during_pre_check():
    """API call proceeds normally when the pre-call Redis check fails.

    The stale pre-check is observability/optimisation, not correctness.  A
    Redis outage must never silently suppress calls to live resources.
    """
    url = "https://api.notion.com/v1/databases/2468fce1-7f2a-8051-b06a-dd7b6a9c3898"
    integration_id = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"

    async def _broken_redis():
        raise ConnectionError("Redis unavailable")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request, patch(
        "app.core.redis.redis_client",
        side_effect=_broken_redis,
    ):
        mock_request.return_value = (MagicMock(is_success=True, json=lambda: {}), None)
        ctx = NotionContext("token", integration_id=integration_id)
        async with ctx.client() as client:
            _, err = await ctx.request(client, "get", url, "Notion", "resolve data source")

    mock_request.assert_called_once()
    assert err is None


@pytest.mark.asyncio
async def test_in_run_cache_populated_on_first_stale_skip():
    """After the first cross-run stale skip the in-run cache is populated.

    The second call to the same URL hits the in-run missing cache (not
    ``_is_cross_run_stale``) so no extra Redis GET is issued.
    """
    url = "https://api.notion.com/v1/databases/2468fce1-7f2a-8051-b06a-dd7b6a9c3898"
    integration_id = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"
    fake_redis = _make_fake_redis_at_count(_STALE_REF_WARN_THRESHOLD)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request, patch(
        "app.core.redis.redis_client",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        ctx = NotionContext("token", integration_id=integration_id)
        async with ctx.client() as client:
            _, err1 = await ctx.request(client, "get", url, "Notion", "resolve data source")
            _, err2 = await ctx.request(client, "get", url, "Notion", "check data source")

    mock_request.assert_not_called()
    # ONE pipelined Redis round-trip (second call used in-run cache).
    assert fake_redis._pipe.execute.call_count == 1
    assert err1 is not None
    assert err2 is not None


@pytest.mark.asyncio
async def test_same_id_different_types_uses_per_run_check_cache():
    """When the same resource ID is stale, probing it as a different type
    reuses the memoised per-run pre-check (no second Redis GET).

    E.g. first GET to ``/v1/databases/{id}`` and then GET to
    ``/v1/data_sources/{id}`` with the same UUID: only ONE Redis round-trip.
    """
    resource_id = "2468fce1-7f2a-8051-b06a-dd7b6a9c3898"
    urls = [
        f"https://api.notion.com/v1/databases/{resource_id}",
        f"https://api.notion.com/v1/data_sources/{resource_id}",
    ]
    integration_id = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"
    fake_redis = _make_fake_redis_at_count(_STALE_REF_WARN_THRESHOLD)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request, patch(
        "app.core.redis.redis_client",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        ctx = NotionContext("token", integration_id=integration_id)
        async with ctx.client() as client:
            for url in urls:
                await ctx.request(client, "get", url, "Notion", "probe")

    mock_request.assert_not_called()
    # Both paths share the same normalised resource ID — only ONE pipelined
    # Redis round-trip.
    assert fake_redis._pipe.execute.call_count == 1


@pytest.mark.asyncio
async def test_concurrent_probes_share_single_redis_get():
    """Concurrent probes to the same stale resource ID share ONE Redis GET.

    Parallel tool flows (e.g. relation-title resolution via asyncio.gather)
    can hit the pre-check for the same resource ID before any of them has
    completed — the in-flight Task memo must dedupe them to a single Redis
    round-trip.
    """
    resource_id = "2468fce1-7f2a-8051-b06a-dd7b6a9c3898"
    url = f"https://api.notion.com/v1/databases/{resource_id}"
    integration_id = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"
    fake_redis = _make_fake_redis_at_count(_STALE_REF_WARN_THRESHOLD)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request, patch(
        "app.core.redis.redis_client",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        ctx = NotionContext("token", integration_id=integration_id)
        async with ctx.client() as client:
            results = await asyncio.gather(
                *[
                    ctx.request(client, "get", url, "Notion", "probe")
                    for _ in range(5)
                ]
            )

    mock_request.assert_not_called()
    assert fake_redis._pipe.execute.call_count == 1
    assert all(err is not None for _, err in results)


@pytest.mark.asyncio
async def test_suppression_honours_expected_statuses_contract():
    """With ``expected_statuses``, suppression returns a synthetic 404 response.

    Callers that opt into safe_request's ``expected_statuses`` contract
    (e.g. ``resolve_data_source_id``) branch on ``resp.status_code`` — the
    suppressed fast path must synthesize the 404 the skipped call would have
    produced, mirroring the in-run missing-cache path.
    """
    url = "https://api.notion.com/v1/databases/2468fce1-7f2a-8051-b06a-dd7b6a9c3898"
    integration_id = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"
    fake_redis = _make_fake_redis_at_count(_STALE_REF_WARN_THRESHOLD)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request, patch(
        "app.core.redis.redis_client",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        ctx = NotionContext("token", integration_id=integration_id)
        async with ctx.client() as client:
            resp, err = await ctx.request(
                client,
                "get",
                url,
                "Notion",
                "resolve data source",
                expected_statuses={400},
            )

    mock_request.assert_not_called()
    assert err is not None
    assert resp is not None
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "object_not_found"
    assert body["message"] == err


@pytest.mark.asyncio
async def test_request_meta_suppressed_when_counter_at_threshold():
    """request_meta() also suppresses the call when the stale counter >= threshold."""
    url = "https://api.notion.com/v1/databases/2468fce1-7f2a-8051-b06a-dd7b6a9c3898"
    integration_id = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"
    fake_redis = _make_fake_redis_at_count(_STALE_REF_WARN_THRESHOLD)

    with patch(
        "app.core.redis.redis_client",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        ctx = NotionContext("token", integration_id=integration_id)
        async with ctx.client() as client:
            with patch.object(client, "get") as mock_client_get:
                _, err, status_code, error_code = await ctx.request_meta(
                    client, "get", url, "Notion", "resolve data source"
                )
                mock_client_get.assert_not_called()

    assert err is not None
    assert "stale" in err.lower()
    assert status_code == 404
    assert error_code == "object_not_found"


@pytest.mark.asyncio
async def test_suppression_warning_logged(caplog):
    """A WARNING is emitted when a cross-run-stale call is suppressed."""
    url = "https://api.notion.com/v1/data_sources/2468fce1-7f2a-8051-b06a-dd7b6a9c3898"
    integration_id = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"
    fake_redis = _make_fake_redis_at_count(_STALE_REF_WARN_THRESHOLD)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ), patch(
        "app.core.redis.redis_client",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        ctx = NotionContext("token", integration_id=integration_id)
        with caplog.at_level(logging.WARNING, logger="app.integrations.providers.notion"):
            async with ctx.client() as client:
                await ctx.request(client, "get", url, "Notion", "retrieve data source")

    suppression_warnings = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "cross-run-stale" in r.message.lower()
    ]
    assert suppression_warnings, "Expected a cross-run-stale suppression WARNING"


@pytest.mark.asyncio
async def test_recovery_probe_allowed_after_recheck_window():
    """The API call proceeds when the bump age has reached the recheck window.

    Suppressed calls never bump the counter, so the key's remaining TTL
    encodes the age of the last real 404.  Once that age reaches
    ``_STALE_REF_RECHECK_SECS`` (>=) the guard stands down and lets a live
    probe through so a re-shared/restored resource can recover without
    waiting the full counter TTL.
    """
    url = "https://api.notion.com/v1/databases/2468fce1-7f2a-8051-b06a-dd7b6a9c3898"
    integration_id = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"
    # Remaining TTL small enough that age >= recheck window.
    stale_age_ttl = _STALE_REF_REDIS_TTL_SECS - _STALE_REF_RECHECK_SECS
    fake_redis = _make_fake_redis_at_count(
        _STALE_REF_WARN_THRESHOLD, ttl=stale_age_ttl
    )

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request, patch(
        "app.core.redis.redis_client",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        mock_request.return_value = (MagicMock(is_success=True, json=lambda: {}), None)
        ctx = NotionContext("token", integration_id=integration_id)
        async with ctx.client() as client:
            _, err = await ctx.request(client, "get", url, "Notion", "resolve data source")

    mock_request.assert_called_once()
    assert err is None


@pytest.mark.asyncio
async def test_suppressed_when_bump_is_fresh_within_recheck_window():
    """Suppression stays active while the last bump is younger than the recheck window."""
    url = "https://api.notion.com/v1/databases/2468fce1-7f2a-8051-b06a-dd7b6a9c3898"
    integration_id = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"
    # Remaining TTL just above the recheck boundary → age just below the window.
    fresh_ttl = _STALE_REF_REDIS_TTL_SECS - _STALE_REF_RECHECK_SECS + 60
    fake_redis = _make_fake_redis_at_count(_STALE_REF_WARN_THRESHOLD, ttl=fresh_ttl)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request, patch(
        "app.core.redis.redis_client",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        ctx = NotionContext("token", integration_id=integration_id)
        async with ctx.client() as client:
            _, err = await ctx.request(client, "get", url, "Notion", "resolve data source")

    mock_request.assert_not_called()
    assert err is not None
    assert "suppressed" in err.lower()


@pytest.mark.asyncio
async def test_fail_open_when_ttl_unreadable():
    """A missing/expired TTL (-2) fails open: the live API call proceeds."""
    url = "https://api.notion.com/v1/databases/2468fce1-7f2a-8051-b06a-dd7b6a9c3898"
    integration_id = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"
    fake_redis = _make_fake_redis_at_count(_STALE_REF_WARN_THRESHOLD, ttl=-2)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request, patch(
        "app.core.redis.redis_client",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        mock_request.return_value = (MagicMock(is_success=True, json=lambda: {}), None)
        ctx = NotionContext("token", integration_id=integration_id)
        async with ctx.client() as client:
            _, err = await ctx.request(client, "get", url, "Notion", "resolve data source")

    mock_request.assert_called_once()
    assert err is None


def test_unambiguous_resource_path_children_is_blocks_only():
    """The ``children`` sub-path qualifies for blocks ONLY.

    ``/v1/blocks/{id}/children`` is the sole children endpoint the Notion API
    exposes; any other ``/v1/{type}/{id}/children`` shape must not be treated
    as an unambiguous resource path (and therefore can never be suppressed or
    cached as a missing resource).
    """
    rid = "2468fce1-7f2a-8051-b06a-dd7b6a9c3898"
    assert NotionContext._is_unambiguous_resource_path(
        f"https://api.notion.com/v1/blocks/{rid}/children"
    )
    assert NotionContext._is_unambiguous_resource_path(
        f"https://api.notion.com/v1/databases/{rid}"
    )
    assert not NotionContext._is_unambiguous_resource_path(
        f"https://api.notion.com/v1/pages/{rid}/children"
    )
    assert not NotionContext._is_unambiguous_resource_path(
        f"https://api.notion.com/v1/data_sources/{rid}/children"
    )
    assert not NotionContext._is_unambiguous_resource_path(
        f"https://api.notion.com/v1/pages/{rid}/properties/some_prop"
    )


# ── self-heal on success ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_success_clears_stale_counter_below_threshold():
    """A successful GET deletes a lingering (below-threshold) stale counter.

    Without the self-heal, the counter only decays via the 14-day TTL and a
    single future transient 404 would ratchet it straight past the threshold,
    re-arming a full suppression window against a healthy resource.
    """
    url = "https://api.notion.com/v1/databases/2468fce1-7f2a-8051-b06a-dd7b6a9c3898"
    integration_id = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"
    fake_redis = _make_fake_redis_at_count(_STALE_REF_WARN_THRESHOLD - 1)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request, patch(
        "app.core.redis.redis_client",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        mock_request.return_value = (MagicMock(is_success=True, json=lambda: {}), None)
        ctx = NotionContext("token", integration_id=integration_id)
        async with ctx.client() as client:
            _, err = await ctx.request(client, "get", url, "Notion", "resolve data source")
        # Let the fire-and-forget _clear_stale_ref task complete.
        await asyncio.sleep(0)

    assert err is None
    fake_redis.delete.assert_awaited_once()
    deleted_key = fake_redis.delete.await_args.args[0]
    assert deleted_key.startswith(_STALE_REF_REDIS_KEY_PREFIX)
    assert "2468fce17f2a8051b06add7b6a9c3898" in deleted_key


@pytest.mark.asyncio
async def test_recovery_probe_success_clears_counter():
    """A successful recovery probe (after the recheck window) deletes the counter."""
    url = "https://api.notion.com/v1/databases/2468fce1-7f2a-8051-b06a-dd7b6a9c3898"
    integration_id = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"
    stale_age_ttl = _STALE_REF_REDIS_TTL_SECS - _STALE_REF_RECHECK_SECS
    fake_redis = _make_fake_redis_at_count(_STALE_REF_WARN_THRESHOLD, ttl=stale_age_ttl)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request, patch(
        "app.core.redis.redis_client",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        mock_request.return_value = (MagicMock(is_success=True, json=lambda: {}), None)
        ctx = NotionContext("token", integration_id=integration_id)
        async with ctx.client() as client:
            _, err = await ctx.request(client, "get", url, "Notion", "resolve data source")
        await asyncio.sleep(0)

    mock_request.assert_called_once()
    assert err is None
    fake_redis.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_probe_does_not_clear_counter():
    """A 404 must never delete the counter (only successes self-heal)."""
    url = "https://api.notion.com/v1/databases/2468fce1-7f2a-8051-b06a-dd7b6a9c3898"
    integration_id = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"
    fake_redis = _make_fake_redis_at_count(_STALE_REF_WARN_THRESHOLD - 1)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request, patch(
        "app.core.redis.redis_client",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        mock_request.return_value = (
            None,
            "Error: Notion resource not found while trying to resolve data source.",
        )
        ctx = NotionContext("token", integration_id=integration_id)
        async with ctx.client() as client:
            _, err = await ctx.request(client, "get", url, "Notion", "resolve data source")
        await asyncio.sleep(0)

    assert err is not None
    fake_redis.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_meta_success_clears_stale_counter():
    """request_meta()'s success path also self-heals the counter."""
    url = "https://api.notion.com/v1/databases/2468fce1-7f2a-8051-b06a-dd7b6a9c3898"
    integration_id = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"
    fake_redis = _make_fake_redis_at_count(_STALE_REF_WARN_THRESHOLD - 1)

    ok_resp = httpx.Response(200, json={"object": "database"})

    with patch(
        "app.core.redis.redis_client",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        ctx = NotionContext("token", integration_id=integration_id)
        async with ctx.client() as client:
            with patch.object(client, "get", return_value=ok_resp):
                resp, err, _, _ = await ctx.request_meta(
                    client, "get", url, "Notion", "retrieve database"
                )
        await asyncio.sleep(0)

    assert err is None
    assert resp is not None
    fake_redis.delete.assert_awaited_once()


def test_suppression_error_is_honest_and_actionable():
    """The synthetic stale error must state the API was NOT called and offer
    a live fallback — the 2026-07-14 incident showed the previous wording
    made the agent report a phantom 404 as a live check."""
    integration_id = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"
    ctx = NotionContext("token", integration_id=integration_id)
    url = "https://api.notion.com/v1/databases/2468fce1-7f2a-8051-b06a-dd7b6a9c3898"
    key = NotionContext._resource_key_from_url(url)
    assert key is not None
    err = ctx._make_cross_run_stale_error(url, key)
    lower = err.lower()
    assert "without contacting the notion api" in lower
    assert "cached verdict" in lower
    assert "search_notion" in lower
    assert "connectivity test" in lower
    # Backwards-compatible markers other code/tests rely on.
    assert "stale" in lower
    assert "suppressed" in lower


# ── clear_stale_ref_counters (operator escape hatch) ─────────────────────────


def _make_fake_redis_with_keys(keys: list[str]):
    """Fake Redis whose scan_iter yields *keys* and whose delete counts them."""
    fake = MagicMock()

    async def _scan_iter(match: str = "*", count: int = 500):
        for k in keys:
            yield k

    fake.scan_iter = _scan_iter
    fake.delete = AsyncMock(side_effect=lambda *args: len(args))
    return fake


@pytest.mark.asyncio
async def test_clear_stale_ref_counters_deletes_matching_keys():
    from app.integrations.providers.notion._common import clear_stale_ref_counters

    integration_id = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"
    keys = [
        f"{_STALE_REF_REDIS_KEY_PREFIX}{integration_id}:aaa",
        f"{_STALE_REF_REDIS_KEY_PREFIX}{integration_id}:bbb",
    ]
    fake_redis = _make_fake_redis_with_keys(keys)

    with patch(
        "app.core.redis.redis_client",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        deleted = await clear_stale_ref_counters(integration_id)

    assert deleted == 2
    fake_redis.delete.assert_awaited_once_with(*keys)


@pytest.mark.asyncio
async def test_clear_stale_ref_counters_best_effort_on_redis_failure():
    from app.integrations.providers.notion._common import clear_stale_ref_counters

    async def _broken_redis():
        raise ConnectionError("Redis unavailable")

    with patch("app.core.redis.redis_client", side_effect=_broken_redis):
        deleted = await clear_stale_ref_counters("some-integration")

    assert deleted == 0


@pytest.mark.asyncio
async def test_clear_stale_ref_counters_noop_without_integration_id():
    from app.integrations.providers.notion._common import clear_stale_ref_counters

    assert await clear_stale_ref_counters("") == 0


@pytest.mark.asyncio
async def test_pre_check_not_applied_to_post_requests():
    """POST requests are never subject to the cross-run stale pre-check.

    The stale guard only covers GET requests on unambiguous resource paths
    (``_is_unambiguous_resource_path``).  A POST to a data source query
    endpoint must always reach the API regardless of the stale counter.
    """
    url = "https://api.notion.com/v1/data_sources/2468fce1-7f2a-8051-b06a-dd7b6a9c3898/query"
    integration_id = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"
    fake_redis = _make_fake_redis_at_count(_STALE_REF_WARN_THRESHOLD + 10)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request, patch(
        "app.core.redis.redis_client",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        mock_request.return_value = (MagicMock(is_success=True, json=lambda: {"results": []}), None)
        ctx = NotionContext("token", integration_id=integration_id)
        async with ctx.client() as client:
            _, err = await ctx.request(client, "post", url, "Notion", "query database")

    mock_request.assert_called_once()
    assert err is None
    # No Redis pre-check round-trip may happen for a POST.
    fake_redis._pipe.execute.assert_not_called()
