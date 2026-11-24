# SPDX-License-Identifier: Apache-2.0

"""CTEM cases kept current with no user action (dev-docs/ctem-cases.md s.4.3)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services import ctem_cases_refresh as refresh


class FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.z: dict[str, dict[str, float]] = {}

    async def get(self, key):
        return self.kv.get(key)

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return False
        self.kv[key] = value
        return True

    async def delete(self, key: str) -> None:
        self.kv.pop(key, None)

    async def zadd(self, key, mapping):
        self.z.setdefault(key, {}).update(mapping)

    async def zremrangebyscore(self, key, low, high):
        self.z[key] = {m: s for m, s in self.z.get(key, {}).items() if not (low <= s <= high)}

    async def zrevrangebyscore(self, key, high, low):
        items = [(m, s) for m, s in self.z.get(key, {}).items() if low <= s <= high]
        return [m for m, _ in sorted(items, key=lambda x: -x[1])]


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()

    async def _client():
        return fake

    monkeypatch.setattr(refresh, "_redis", _client)
    return fake


@pytest.mark.asyncio
async def test_freshness_keeps_the_last_applied_time_through_an_outage(redis):
    t1 = datetime(2026, 11, 22, 13, 30, tzinfo=UTC)
    t2 = datetime(2026, 11, 22, 13, 40, tzinfo=UTC)
    await refresh.record_freshness("cyber", {"applied": True}, t1)
    await refresh.record_freshness("cyber", {"applied": False, "missing": ["openaev"]}, t2)
    got = await refresh.freshness("cyber")
    assert got["updated_at"] == t1.isoformat()
    assert got["missing"] == ["openaev"] and got["missing_at"] == t2.isoformat()
    await refresh.record_freshness("cyber", {"applied": True}, t2)
    assert (await refresh.freshness("cyber"))["missing"] == []


@pytest.mark.asyncio
async def test_freshness_without_redis_is_empty(monkeypatch):
    async def _down():
        raise RuntimeError("no redis")

    monkeypatch.setattr(refresh, "_redis", _down)
    assert await refresh.freshness("cyber") == {"updated_at": None, "missing": [], "missing_at": None}


@pytest.mark.asyncio
async def test_identities_are_the_fleet_run_as_else_recent_viewers(redis, monkeypatch):
    now = 1_800_000_000.0
    await refresh.note_viewer("old", "cyber", now=now - 30 * 86400)
    await refresh.note_viewer("v1", "cyber", now=now - 60)
    await refresh.note_viewer("v2", "cyber", now=now)
    assert await refresh.recent_viewers("cyber", now=now) == ["v2", "v1"]

    async def no_fleet(db):
        return []

    monkeypatch.setattr(refresh, "loop_fleet_run_as", no_fleet)
    monkeypatch.setattr(refresh.time, "time", lambda: now)
    assert await refresh.pass_identities(None, "cyber") == ["v2", "v1"]

    async def fleet(db):
        return ["runner"]

    monkeypatch.setattr(refresh, "loop_fleet_run_as", fleet)
    assert await refresh.pass_identities(None, "cyber") == ["runner"]


def _primary(monkeypatch):
    org = SimpleNamespace(id=uuid.uuid4())

    async def get_primary(db):
        return org

    import app.services.organizations as organizations

    monkeypatch.setattr(organizations, "get_primary", get_primary)
    return org


class FakeDb:
    def __init__(self) -> None:
        self.rollbacks = 0

    async def rollback(self):
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_scheduled_pass_recomputes_as_each_identity_once_per_interval(redis, monkeypatch):
    """A new open vulnerability reaches its case within one interval with nobody on the page."""
    org = _primary(monkeypatch)
    calls: list[tuple[uuid.UUID, str, str]] = []

    async def identities(db, vertical):
        return ["a", "b"]

    async def minutes(db):
        return 15

    async def recompute_for_user(db, *, organization_id, user_id, vertical):
        calls.append((organization_id, user_id, vertical))
        return {"applied": user_id == "a", "missing": [] if user_id == "a" else ["openaev"]}

    import app.services.ctem_cases_source as source

    monkeypatch.setattr(refresh, "pass_identities", identities)
    monkeypatch.setattr(refresh, "interval_minutes", minutes)
    monkeypatch.setattr(source, "recompute_for_user", recompute_for_user)
    first = await refresh.scheduled_pass(FakeDb())
    second = await refresh.scheduled_pass(FakeDb())
    assert first == {"identities": 2, "applied": 1, "skipped": 1}
    assert second is None  # the slot holds for the interval
    assert calls == [(org.id, "a", "cyber"), (org.id, "b", "cyber")]
    # "a" applied first, then "b" saw an outage: the muted line names OpenAEV.
    fresh = await refresh.freshness("cyber")
    assert fresh["updated_at"] and fresh["missing"] == ["openaev"]


@pytest.mark.asyncio
async def test_scheduled_pass_goes_on_when_one_identity_fails(redis, monkeypatch):
    _primary(monkeypatch)

    async def identities(db, vertical):
        return ["boom", "ok"]

    async def minutes(db):
        return 15

    async def recompute_for_user(db, *, organization_id, user_id, vertical):
        if user_id == "boom":
            raise RuntimeError("source down")
        return {"applied": True}

    import app.services.ctem_cases_source as source

    monkeypatch.setattr(refresh, "pass_identities", identities)
    monkeypatch.setattr(refresh, "interval_minutes", minutes)
    monkeypatch.setattr(source, "recompute_for_user", recompute_for_user)
    db = FakeDb()
    assert await refresh.scheduled_pass(db) == {"identities": 2, "applied": 1, "skipped": 1}
    assert db.rollbacks == 1


@pytest.mark.asyncio
async def test_two_concurrent_recomputes_of_one_organization_never_both_write(
    redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1953: the second recompute skips (busy) while the first runs; the lock frees at its end."""
    import asyncio
    from typing import Any, cast

    import app.services.ctem_cases_source as source

    no_db = cast(Any, None)
    org = uuid.uuid4()
    started, release = asyncio.Event(), asyncio.Event()
    writes: list[str] = []

    async def recompute_for_user(db: Any, *, organization_id: Any, user_id: str, vertical: str) -> dict[str, Any]:
        started.set()
        await release.wait()
        writes.append(user_id)
        return {"applied": True}

    monkeypatch.setattr(source, "recompute_for_user", recompute_for_user)
    first = asyncio.create_task(refresh.recompute_as(no_db, organization_id=org, user_id="a", vertical="cyber"))
    await started.wait()
    second = await refresh.recompute_as(no_db, organization_id=org, user_id="b", vertical="cyber")
    # Another organization is never blocked by this one.
    release.set()
    other = await refresh.recompute_as(no_db, organization_id=uuid.uuid4(), user_id="c", vertical="cyber")
    assert (await first)["applied"] is True
    assert second["busy"] is True and second["applied"] is False
    assert other["applied"] is True
    assert sorted(writes) == ["a", "c"]
    # A busy answer moves neither the freshness line nor the viewer's stamp.
    assert "ctem:cases:viewer-recompute:b:cyber" not in redis.kv
    # Released: the next pass runs.
    assert not [k for k in redis.kv if k.startswith("ctem:cases:recompute-lock:")]
    again = await refresh.recompute_as(no_db, organization_id=org, user_id="b", vertical="cyber")
    assert again["applied"] is True


@pytest.mark.asyncio
async def test_a_lock_another_holder_took_is_never_released_by_this_one(redis: FakeRedis) -> None:
    org = uuid.uuid4()
    key = f"ctem:cases:recompute-lock:{org}:cyber"
    async with refresh.recompute_lock(org, "cyber") as held:
        assert held is True
        redis.kv[key] = "someone-else"  # expired and retaken by another pod
    assert redis.kv[key] == "someone-else"


@pytest.mark.asyncio
async def test_the_recompute_runs_when_redis_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _down() -> None:
        raise RuntimeError("no redis")

    monkeypatch.setattr(refresh, "_redis", _down)
    async with refresh.recompute_lock(uuid.uuid4(), "cyber") as held:
        assert held is True


@pytest.mark.asyncio
async def test_viewer_due_after_the_interval_only(redis):
    now = 1_800_000_000.0
    assert await refresh.viewer_due("u", "cyber", 15, now=now) is True
    redis.kv["ctem:cases:viewer-recompute:u:cyber"] = str(now - 60)
    assert await refresh.viewer_due("u", "cyber", 15, now=now) is False
    assert await refresh.viewer_due("u", "cyber", 15, now=now + 15 * 60) is True


def test_profile_interval_defaults_to_15_minutes_and_refuses_under_5():
    from pydantic import ValidationError

    from app.services.organization_context import CasesRecompute, OrganizationProfile

    assert OrganizationProfile().cases_recompute.interval_minutes == 15
    with pytest.raises(ValidationError):
        CasesRecompute(interval_minutes=4)


def test_the_interval_stays_out_of_the_legacy_dump_and_the_agent_block():
    from app.services import organization_context as oc

    assert "cases_recompute" in oc.EXPOSURE_FIELDS
    profile = oc.parse_profile({"name": "Acme", "cases_recompute": {"interval_minutes": 30}})
    assert profile.cases_recompute.interval_minutes == 30
    assert "cases_recompute" not in oc.legacy_dump(profile)
