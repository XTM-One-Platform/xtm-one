"""The platform-wide quota counters through a Redis wipe, against a real
PostgreSQL and a real Redis.

Same scratch-database recipe as ``test_agent_failure_pool_postgres.py``: a
throwaway database from ``DATABASE_URL`` (skipped without one), the whole
schema, dropped afterwards. The real Redis test needs ``REDIS_URL`` and skips
otherwise; it writes one counter of a quota type no platform has, and deletes it.

What a double could not honestly exercise, and what is therefore tested here:

* the flush writing ``quota_global_usage`` and the gate reading it back through
  its own session once Redis lost the counters, for a monthly and a yearly
  license ceiling;
* the ``ON CONFLICT`` upsert: a lower Redis count never lowers a saved mark, and
  two writers racing on a new period both land;
* a counter lost while a flush commits a higher mark, read back at that mark,
  and a flush outlasting the read-back's wait, which seeds nothing;
* a per-user flush in flight holding up neither the read-back nor the save of
  the platform-wide counters;
* a quota reset leaving the platform-wide counters and their rows alone;
* Settings > Usage and quotas > Quotas listing none of it under a user;
* racing charges seeding a lost counter once, through the real script.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
import redis.asyncio as redis_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - every model, for ALL_TABLES
from app.core import config
from app.models.quota import QuotaGlobalUsage, QuotaUsage
from app.models.user import User
from app.services import quota
from app.services.quota import (
    QuotaExceededError,
    _current_period_key,
    _global_usage_key,
    _usage_key,
    check_agentic_quota,
    flush_quota_usage,
    get_platform_quota_monitoring,
    increment_agentic_usage,
    reset_user_quota_usage,
)
from tests.frozen_clock import FROZEN_NOW
from tests.scratch_database import ALL_TABLES, scratch_database
from tests.test_quota_global_durability import _Redis

pytestmark = pytest.mark.integration

# The periods of the frozen instant the code under test reads too (conftest's
# ``_frozen_quota_clock``): a run crossing midnight UTC keeps them one.
DAY = _current_period_key("daily", FROZEN_NOW)
MONTH = _current_period_key("monthly", FROZEN_NOW)
YEAR = _current_period_key("yearly", FROZEN_NOW)

# Captured at import, before the suite-wide fixture swaps it for "nothing
# saved yet": these tests read the real table.
_REAL_LOAD_GLOBAL_BASELINE = quota._load_global_baseline


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("quotaglob", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _seed_user(session) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"quota-{uuid.uuid4().hex[:8]}@test.local",
        full_name="Quota User",
        hashed_password="x",
    )
    session.add(user)
    await session.commit()
    return user


async def _saved_marks(session_factory) -> dict[tuple[str, str], int]:
    async with session_factory() as session:
        rows = (await session.execute(select(QuotaGlobalUsage))).scalars().all()
    return {(row.usage_type, row.period): row.count for row in rows}


def _use(monkeypatch, redis: _Redis) -> None:
    monkeypatch.setattr(quota, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(quota, "_load_global_baseline", _REAL_LOAD_GLOBAL_BASELINE)


def _license(monkeypatch, ceilings: dict[str, int]) -> None:
    monkeypatch.setattr(config.settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(quota, "get_xtm_agentic_ceilings", AsyncMock(return_value=ceilings))
    monkeypatch.setattr(quota, "resolve_quota_config", AsyncMock(return_value=None))


async def _upsert(session, count: int) -> None:
    await quota._upsert_global_usage(
        session,
        [{"usage_type": "agentic", "period": MONTH, "count": count}],
        datetime.now(UTC),
    )


async def test_a_flush_then_a_wipe_leaves_every_license_ceiling_where_it_was(monkeypatch):
    redis = _Redis()
    _use(monkeypatch, redis)
    _license(monkeypatch, {"monthly": 3, "yearly": 5})
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        for _ in range(3):
            await increment_agentic_usage(user.id, db=session)

        # Seven counters: the user's monthly one, the user's three quota panel
        # counters (``agentic_view``: today, this month, this year) and the
        # platform-wide pool of every panel period.
        assert await flush_quota_usage() == 7
        assert await _saved_marks(session_factory) == {
            ("agentic", DAY): 3,
            ("agentic", MONTH): 3,
            ("agentic", YEAR): 3,
        }

        redis.store.clear()
        with pytest.raises(QuotaExceededError) as refused:
            await check_agentic_quota(session, user.id)
        assert (refused.value.current, refused.value.period) == (3, "monthly")

        # The monthly ceiling is raised; the yearly one keeps counting from its
        # saved mark, through a second wipe.
        _license(monkeypatch, {"monthly": 100, "yearly": 5})
        for _ in range(2):
            await increment_agentic_usage(user.id, db=session)
        assert redis.store[_global_usage_key("agentic", YEAR)] == 5
        await flush_quota_usage()
        redis.store.clear()
        with pytest.raises(QuotaExceededError) as refused:
            await check_agentic_quota(session, user.id)
        assert (refused.value.current, refused.value.period) == (5, "yearly")
        assert await _saved_marks(session_factory) == {
            ("agentic", DAY): 5,
            ("agentic", MONTH): 5,
            ("agentic", YEAR): 5,
        }

        # The only ``quota_usage`` rows are the user's own.
        async with session_factory() as check:
            owners = (
                await check.execute(select(QuotaUsage.user_id).where(QuotaUsage.usage_type == "agentic"))
            ).scalars().all()
        assert owners == [user.id]


async def test_a_refunded_counter_never_lowers_its_saved_mark(monkeypatch):
    redis = _Redis({_global_usage_key("agentic", MONTH): 9})
    _use(monkeypatch, redis)
    async with _temp_database() as session_factory:
        assert await flush_quota_usage() == 1
        redis.store[_global_usage_key("agentic", MONTH)] = 4
        await flush_quota_usage()
        assert await _saved_marks(session_factory) == {("agentic", MONTH): 9}

        redis.store[_global_usage_key("agentic", MONTH)] = 11
        await flush_quota_usage()
        assert await _saved_marks(session_factory) == {("agentic", MONTH): 11}


@pytest.mark.parametrize(("first_count", "second_count"), [(5, 8), (8, 5)])
async def test_two_writers_racing_on_a_new_period_both_land(first_count, second_count):
    """The second insert waits on the first one's row, then raises it or not:
    no unique violation, and the higher count stays."""

    async def _second_writer(session) -> None:
        await _upsert(session, second_count)
        await session.commit()

    async with _temp_database() as session_factory:
        async with session_factory() as first, session_factory() as second:
            await _upsert(first, first_count)
            racing = asyncio.create_task(_second_writer(second))
            await asyncio.sleep(0.2)
            assert not racing.done(), "the second writer waits for the first one's row"
            await first.commit()
            await racing

        assert await _saved_marks(session_factory) == {("agentic", MONTH): 8}


async def test_a_counter_lost_while_a_flush_commits_is_seeded_from_that_flush(monkeypatch):
    """Redis loses the counter while a flush is saving 8 over the 5 the row
    held: seeded from the 5, the counter would stay below its saved mark for
    the rest of the period, so the read-back waits for the flush to commit."""
    redis = _Redis()
    _use(monkeypatch, redis)
    async with _temp_database() as session_factory:
        async with session_factory() as session:
            await _upsert(session, 5)
            await session.commit()
        async with session_factory() as flush:
            await flush.execute(
                text("SELECT pg_advisory_xact_lock(CAST(:k AS bigint))"),
                {"k": quota._QUOTA_GLOBAL_FLUSH_LOCK_KEY},
            )
            await _upsert(flush, 8)
            reseed = asyncio.create_task(quota._get_global_count(redis, "agentic", MONTH))
            await asyncio.sleep(0.3)
            assert not reseed.done(), "the read-back waits for the flush in flight"
            await flush.commit()

        assert await reseed == 8
        assert redis.store[_global_usage_key("agentic", MONTH)] == 8


async def test_a_read_back_that_outwaits_the_flush_creates_no_counter(monkeypatch):
    """The flush keeps its lock past the read-back's wait: the read-back gives
    up without seeding the counter (seeded at 0, the ceiling would restart for
    the period) and fails like an unreachable store, so a due background run
    is held until it can be read instead of failing."""
    redis = _Redis()
    _use(monkeypatch, redis)
    monkeypatch.setattr(quota, "_GLOBAL_BASELINE_FLUSH_WAIT_SECONDS", 0.3)
    async with _temp_database() as session_factory:
        async with session_factory() as session:
            await _upsert(session, 5)
            await session.commit()
        async with session_factory() as flush:
            await flush.execute(
                text("SELECT pg_advisory_xact_lock(CAST(:k AS bigint))"),
                {"k": quota._QUOTA_GLOBAL_FLUSH_LOCK_KEY},
            )
            with pytest.raises(TimeoutError) as gave_up:
                await quota._get_global_count(redis, "agentic", MONTH)
            await flush.rollback()

    assert _global_usage_key("agentic", MONTH) not in redis.store
    assert quota.is_quota_store_outage(gave_up.value)


async def _hold_the_flush_lock(session) -> None:
    """What another replica's per-user flush holds for as long as its rows take."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(CAST(:k AS bigint))"),
        {"k": quota._QUOTA_FLUSH_LOCK_KEY},
    )


async def test_a_per_user_flush_in_flight_does_not_hold_up_the_read_back(monkeypatch):
    """At a period rollover every platform-wide counter is missing at once, and
    each read-back waiting on the flush lock outwaited the per-user flush: its
    quota check failed. It waits on the platform-wide save only."""
    redis = _Redis()
    _use(monkeypatch, redis)
    monkeypatch.setattr(quota, "_GLOBAL_BASELINE_FLUSH_WAIT_SECONDS", 0.3)
    async with _temp_database() as session_factory:
        async with session_factory() as session:
            await _upsert(session, 5)
            await session.commit()
        async with session_factory() as users_flush:
            await _hold_the_flush_lock(users_flush)
            assert await quota._get_global_count(redis, "agentic", MONTH) == 5
            await users_flush.rollback()

    assert redis.store[_global_usage_key("agentic", MONTH)] == 5


async def test_the_platform_wide_counters_are_saved_past_a_per_user_flush_in_flight(monkeypatch):
    """Another replica's per-user flush holds the flush lock: the users'
    counters wait for the next cycle, the platform-wide ones are saved."""
    async with _temp_database() as session_factory:
        async with session_factory() as session:
            user = await _seed_user(session)
        redis = _Redis(
            {
                _global_usage_key("agentic", MONTH): 9,
                _usage_key(user.id, "agentic", None, MONTH): 4,
            }
        )
        _use(monkeypatch, redis)
        async with session_factory() as users_flush:
            await _hold_the_flush_lock(users_flush)
            assert await flush_quota_usage() == 1
            await users_flush.rollback()

        assert await _saved_marks(session_factory) == {("agentic", MONTH): 9}
        async with session_factory() as check:
            assert (await check.execute(select(QuotaUsage))).scalars().all() == []


async def test_a_quota_reset_leaves_the_platform_wide_counters_alone(monkeypatch):
    """A reset never gave the license pool its capacity back: it clears the
    user's counters and rows, never the platform-wide ones."""
    async with _temp_database() as session_factory:
        async with session_factory() as session:
            user = await _seed_user(session)
            session.add(QuotaGlobalUsage(usage_type="agentic", period=MONTH, count=6))
            session.add(QuotaUsage(user_id=user.id, usage_type="agentic", period=MONTH, count=4))
            await session.commit()
        user_key = _usage_key(user.id, "agentic", None, MONTH)
        global_key = _global_usage_key("agentic", MONTH)
        redis = _Redis({user_key: 4, global_key: 7})
        _use(monkeypatch, redis)

        async with session_factory() as admin_session:
            await reset_user_quota_usage(admin_session, user.id)

        assert user_key not in redis.store
        assert redis.store[global_key] == 7
        assert await _saved_marks(session_factory) == {("agentic", MONTH): 6}


async def test_settings_quotas_lists_the_platform_wide_usage_under_nobody(monkeypatch):
    monkeypatch.setattr(config.settings, "platform_mode", "copilot")
    monkeypatch.setattr(
        quota,
        "_load_global_quota_config",
        AsyncMock(return_value={"agentic": {"default": 2, "period": "monthly", "platform_limit": 1000}}),
    )
    async with _temp_database() as session_factory:
        async with session_factory() as session:
            user = await _seed_user(session)
            session.add(QuotaUsage(user_id=user.id, usage_type="agentic", period=MONTH, count=3))
            session.add(QuotaGlobalUsage(usage_type="agentic", period=MONTH, count=50))
            await session.commit()
        # Redis lost every counter: the platform-wide row reads the saved mark.
        _use(monkeypatch, _Redis())

        async with session_factory() as session:
            monitoring = await get_platform_quota_monitoring(session)

    platform_wide = [row for row in monitoring["quotas"] if row["type"] == "agentic" and row["is_global"]]
    assert [row["current_usage"] for row in platform_wide] == [50]
    per_user = [row for row in monitoring["quotas"] if row["type"] == "agentic" and not row["is_global"]]
    assert [(row["active_users"], row["current_usage"]) for row in per_user] == [(1, 3)]
    assert [(e["email"], e["usage"]) for e in monitoring["over_quota_users"]["agentic"]] == [(user.email, 3)]
    assert [(c["email"], c["count"]) for c in monitoring["top_consumers"]["agentic"]] == [(user.email, 3)]
    (month,) = [entry for entry in monitoring["history"] if entry["period"] == MONTH]
    assert month["agentic"] == 3


# ---------------------------------------------------------------------------
# Racing charges against a real Redis
# ---------------------------------------------------------------------------


@pytest.fixture
async def real_redis():
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        pytest.skip("REDIS_URL is not set")
    client = redis_asyncio.Redis.from_url(url, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:  # pragma: no cover - environment dependent
        await client.aclose()
        pytest.skip(f"Redis unreachable at {url}: {exc}")
    usage_type = f"t4711{uuid.uuid4().hex[:8]}"
    try:
        yield client, usage_type
    finally:
        await client.delete(_global_usage_key(usage_type, MONTH))
        await client.aclose()


async def test_charges_racing_on_a_lost_counter_seed_its_saved_mark_once(real_redis, monkeypatch):
    client, usage_type = real_redis
    key = _global_usage_key(usage_type, MONTH)

    async def _saved(saved_type: str, period_key: str) -> int:
        await asyncio.sleep(0.01)
        return 7 if (saved_type, period_key) == (usage_type, MONTH) else 0

    monkeypatch.setattr(quota, "_load_global_baseline", _saved)

    await asyncio.gather(*(quota._count_global(client, usage_type, ["monthly"]) for _ in range(30)))

    assert int(await client.get(key)) == 37
    assert await client.ttl(key) > 30 * 86_400
