"""Regression tests for the CTEM posture hot-path guards (issue: prod pool
exhaustion after the CTEM rollout).

These cover the pure async concurrency machinery in ``app.api.v1.ctem_metrics``
that keeps a ~20-30s live build off the request hot path and off the DB pool:

* ``_payload_age_seconds`` - stale-while-revalidate freshness math.
* ``_build_and_cache`` - per-(user, vertical) singleflight (concurrent requests
  for the same key build ONCE), a process-wide concurrency cap, eviction of idle
  singleflight locks, and rebuilding a cache entry with an unparseable age.
* ``_schedule_posture_refresh`` - background refresh is deduped per key.

No DB / Redis / network: ``_build_posture_payload`` and the background coroutine
are monkeypatched, and Redis is a tiny in-memory fake.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import ctem_metrics


def _async_return(value):
    """An async function (any args) that resolves to *value* - for stubbing the
    awaited cache/redis helpers."""

    async def _fn(*_a, **_k):
        return value

    return _fn


class _FakeRedis:
    """Minimal async Redis stand-in supporting the get/set the cache uses."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.store[key] = value


def test_payload_age_seconds_parsing():
    fresh = {"generated_at": datetime.now(UTC).isoformat()}
    assert ctem_metrics._payload_age_seconds(fresh) < 5

    old = {"generated_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat()}
    assert ctem_metrics._payload_age_seconds(old) > 3600

    # Missing / malformed / non-dict inputs degrade to None (treated as stale).
    assert ctem_metrics._payload_age_seconds({}) is None
    assert ctem_metrics._payload_age_seconds(None) is None
    assert ctem_metrics._payload_age_seconds({"generated_at": "not-a-date"}) is None

    # A naive timestamp is interpreted as UTC (no tz-offset skew).
    naive_utc_now = datetime.now(UTC).replace(tzinfo=None).isoformat()
    age = ctem_metrics._payload_age_seconds({"generated_at": naive_utc_now})
    assert age is not None and -5 < age < 5


async def test_build_and_cache_is_singleflight(monkeypatch):
    """N concurrent requests for the SAME (user, vertical) trigger ONE build;
    the rest receive the cached result instead of each launching a collection."""
    calls = 0

    async def fake_build(db, user, vertical):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)  # simulate the slow live collection
        return (
            {"generated_at": datetime.now(UTC).isoformat(), "build": calls},
            {"scoping": {"a": 1}},
        )

    monkeypatch.setattr(ctem_metrics, "_build_posture_payload", fake_build)
    ctem_metrics._build_locks.clear()
    ctem_metrics._build_lock_refs.clear()
    client = _FakeRedis()
    user = SimpleNamespace(id="u-singleflight")

    results = await asyncio.gather(
        *[ctem_metrics._build_and_cache(None, user, "cyber", client) for _ in range(8)]
    )

    assert calls == 1, "singleflight must collapse concurrent same-key builds onto one"
    # Every caller gets the single build's payload + the cached facts.
    assert all(payload["build"] == 1 for payload, _ in results)
    assert all(facts == {"scoping": {"a": 1}} for _, facts in results)

    # The display payload AND the raw facts are both cached for reuse by /stages.
    posture_key, facts_key = ctem_metrics._posture_cache_keys("u-singleflight", "cyber")
    assert posture_key in client.store
    assert facts_key in client.store


async def test_build_and_cache_caps_global_concurrency(monkeypatch):
    """A burst of DIFFERENT-key builds never runs more than the cap at once, so a
    surge of dashboard loads can't exhaust the DB pool / hammer upstream."""
    concurrent = 0
    peak = 0

    async def fake_build(db, user, vertical):
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0.05)
        concurrent -= 1
        return ({"generated_at": datetime.now(UTC).isoformat()}, {})

    monkeypatch.setattr(ctem_metrics, "_build_posture_payload", fake_build)
    ctem_metrics._build_locks.clear()
    ctem_metrics._build_lock_refs.clear()
    client = _FakeRedis()

    users = [SimpleNamespace(id=f"u-cap-{i}") for i in range(12)]
    await asyncio.gather(
        *[ctem_metrics._build_and_cache(None, u, "cyber", client) for u in users]
    )

    assert peak <= ctem_metrics._MAX_CONCURRENT_BUILDS
    assert peak > 1, "the fake build should have actually overlapped"


async def test_build_and_cache_evicts_idle_locks(monkeypatch):
    """The singleflight lock registry must not grow without bound: once a build
    settles with no in-flight callers, its lock + refcount entry are evicted so a
    long-lived worker serving many distinct users does not leak ``asyncio.Lock``s."""

    async def fake_build(db, user, vertical):
        return ({"generated_at": datetime.now(UTC).isoformat()}, {})

    monkeypatch.setattr(ctem_metrics, "_build_posture_payload", fake_build)
    ctem_metrics._build_locks.clear()
    ctem_metrics._build_lock_refs.clear()
    client = _FakeRedis()

    users = [SimpleNamespace(id=f"u-evict-{i}") for i in range(5)]
    await asyncio.gather(
        *[ctem_metrics._build_and_cache(None, u, "cyber", client) for u in users]
    )

    assert ctem_metrics._build_locks == {}, "idle singleflight locks must be evicted"
    assert ctem_metrics._build_lock_refs == {}


async def test_build_and_cache_rebuilds_on_unparseable_age(monkeypatch):
    """A cached entry whose ``generated_at`` is missing/unparseable is stale, so the
    fast path inside the lock must rebuild instead of serving it (mirroring
    ``ctem_posture``, where ``age is None`` forces a refresh)."""
    calls = 0

    async def fake_build(db, user, vertical):
        nonlocal calls
        calls += 1
        return (
            {"generated_at": datetime.now(UTC).isoformat(), "build": calls},
            {"scoping": {"a": 1}},
        )

    monkeypatch.setattr(ctem_metrics, "_build_posture_payload", fake_build)
    ctem_metrics._build_locks.clear()
    ctem_metrics._build_lock_refs.clear()
    client = _FakeRedis()
    user = SimpleNamespace(id="u-unparseable")
    posture_key, facts_key = ctem_metrics._posture_cache_keys("u-unparseable", "cyber")
    # Seed a malformed cache entry: present but with an unparseable generated_at.
    client.store[posture_key] = json.dumps({"generated_at": "not-a-date", "build": 0})
    client.store[facts_key] = json.dumps({})

    payload, facts = await ctem_metrics._build_and_cache(None, user, "cyber", client)

    assert calls == 1, "an unparseable cache entry must be rebuilt, not served stale"
    assert payload["build"] == 1
    assert facts == {"scoping": {"a": 1}}


async def test_schedule_posture_refresh_deduped(monkeypatch):
    """A stale-cache hit schedules at most ONE background refresh per key even if
    several requests observe the stale entry at once."""
    started = 0

    async def fake_bg(user_id, vertical, profile_stamp):
        nonlocal started
        started += 1
        await asyncio.sleep(0.02)

    monkeypatch.setattr(ctem_metrics, "_refresh_posture_bg", fake_bg)
    ctem_metrics._bg_refresh_inflight.clear()

    ctem_metrics._schedule_posture_refresh("u-bg", "cyber", None)
    ctem_metrics._schedule_posture_refresh("u-bg", "cyber", None)  # deduped while in flight

    await asyncio.sleep(0.05)  # let the background task finish
    assert started == 1


async def test_build_and_cache_releases_db_before_building(monkeypatch):
    """The pooled DB connection is released BEFORE the live build runs, so a build
    queued behind the semaphore (one per extra user in a 350-user burst) never
    pins a connection - the root cause of the pool exhaustion."""
    order: list[str] = []

    async def fake_release(db):
        order.append("release")

    async def fake_build(db, user, vertical):
        order.append("build")
        return ({"generated_at": datetime.now(UTC).isoformat()}, {})

    monkeypatch.setattr(ctem_metrics, "release_db_connection_before_loop", fake_release)
    monkeypatch.setattr(ctem_metrics, "_build_posture_payload", fake_build)
    ctem_metrics._build_locks.clear()
    ctem_metrics._build_lock_refs.clear()
    client = _FakeRedis()
    user = SimpleNamespace(id="u-release")
    db = SimpleNamespace()  # non-None so the release path is exercised

    await ctem_metrics._build_and_cache(db, user, "cyber", client)

    assert order == ["release", "build"], "must release the DB connection before building"


async def test_schedule_posture_refresh_caps_fanout(monkeypatch):
    """Under a stale burst from many distinct users the background-refresh fan-out
    is bounded: excess keys keep serving stale instead of spawning a task +
    session each."""
    started: list[str] = []

    async def fake_bg(user_id, vertical, profile_stamp):
        started.append(user_id)
        await asyncio.sleep(0.05)

    monkeypatch.setattr(ctem_metrics, "_refresh_posture_bg", fake_bg)
    ctem_metrics._bg_refresh_inflight.clear()

    cap = ctem_metrics._MAX_INFLIGHT_REFRESHES
    for i in range(cap + 10):
        ctem_metrics._schedule_posture_refresh(f"u-fanout-{i}", "cyber", None)

    # Tasks have not run yet (no await), so exactly `cap` are scheduled; the rest
    # are dropped and keep serving the stale cache.
    assert len(ctem_metrics._bg_refresh_inflight) == cap

    await asyncio.sleep(0.1)  # let the capped batch run + clean up
    assert len(started) == cap


# ── /sync force-rebuild: spinner spans the rebuild, never shows the sample ─────


async def test_sync_force_rebuilds_active_vertical(monkeypatch):
    """POST /sync clears the cache AND rebuilds the ACTIVE vertical INLINE, so the
    dashboard's refresh button spins for the whole rebuild (and the existing live
    data stays on screen) instead of only clearing keys and letting a later
    refetch flip the UI. It returns only once the fresh payload is cached."""
    monkeypatch.setattr(ctem_metrics, "_require_xtm_one", lambda: None)

    class _FakeRedisDel:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        async def delete(self, *keys):
            self.deleted.extend(keys)
            return len(keys)

    fake = _FakeRedisDel()
    monkeypatch.setattr(ctem_metrics, "_redis_or_none", _async_return(fake))

    built = {}

    async def _build(_db, _user, vertical, _client, *, force=False):
        built["vertical"] = vertical
        built["force"] = force
        return ({"generated_at": "x"}, {})

    monkeypatch.setattr(ctem_metrics, "_build_and_cache", _build)

    user = SimpleNamespace(id=uuid.uuid4(), ctem_vertical="cyber")
    result = await ctem_metrics.refresh_ctem_posture(
        vertical=None, db=SimpleNamespace(), user=user
    )

    assert result["status"] == "refreshed"
    assert built["vertical"] == "cyber"  # active vertical rebuilt inline
    assert built["force"] is True  # forced so a failed clear can't make it a no-op
    assert len(fake.deleted) > 0  # cache cleared before the rebuild


async def test_sync_rebuild_failure_returns_503(monkeypatch):
    """A genuine inline-rebuild failure surfaces as HTTP 503 instead of a
    misleading ``{"status": "refreshed"}``: the client must be able to treat the
    force-sync as failed (and keep the existing data on screen) rather than act
    on a false success. The cache clear stays best-effort and still runs first."""
    monkeypatch.setattr(ctem_metrics, "_require_xtm_one", lambda: None)

    class _FakeRedisDel:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        async def delete(self, *keys):
            self.deleted.extend(keys)
            return len(keys)

    fake = _FakeRedisDel()
    monkeypatch.setattr(ctem_metrics, "_redis_or_none", _async_return(fake))

    async def _build_boom(_db, _user, _vertical, _client, *, force=False):
        raise RuntimeError("upstream collection is down")

    monkeypatch.setattr(ctem_metrics, "_build_and_cache", _build_boom)

    user = SimpleNamespace(id=uuid.uuid4(), ctem_vertical="cyber")
    with pytest.raises(HTTPException) as exc_info:
        await ctem_metrics.refresh_ctem_posture(
            vertical=None, db=SimpleNamespace(), user=user
        )

    assert exc_info.value.status_code == 503
    assert len(fake.deleted) > 0  # cache still cleared (best-effort) before the failed build


async def test_build_and_cache_force_skips_fresh_cache(monkeypatch):
    """force=True bypasses the stale-while-fresh short-circuit so a forced rebuild
    (POST /sync) actually rebuilds even when a fresh cache entry is present -
    otherwise a force sync whose cache clear failed would be a silent no-op."""
    calls = 0

    async def fake_build(db, user, vertical):
        nonlocal calls
        calls += 1
        return (
            {"generated_at": datetime.now(UTC).isoformat(), "build": calls},
            {"scoping": {"a": 1}},
        )

    monkeypatch.setattr(ctem_metrics, "_build_posture_payload", fake_build)
    ctem_metrics._build_locks.clear()
    ctem_metrics._build_lock_refs.clear()
    client = _FakeRedis()
    user = SimpleNamespace(id="u-force")
    posture_key, facts_key = ctem_metrics._posture_cache_keys("u-force", "cyber")
    # Seed a FRESH cache entry (age ~0) that a non-forced build would serve as-is.
    client.store[posture_key] = json.dumps(
        {"generated_at": datetime.now(UTC).isoformat(), "build": 0}
    )
    client.store[facts_key] = json.dumps({})

    payload, facts = await ctem_metrics._build_and_cache(
        None, user, "cyber", client, force=True
    )

    assert calls == 1, "force=True must rebuild even when a fresh cache entry exists"
    assert payload["build"] == 1
    assert facts == {"scoping": {"a": 1}}
