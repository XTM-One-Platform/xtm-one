"""A ``platform_quota_reset`` support code against a real PostgreSQL.

The code resets the platform-wide usage counters - the ones the license
ceilings are enforced against - in Redis and in ``quota_global_usage``. What a
fake session could not honestly exercise, and is therefore run end to end on a
scratch database (``tests/scratch_database.py``):

* that the reset deletes the named counters of the CURRENT windows and nothing
  else - not a past window, not a type it does not name, not a user's usage;
* that the gate lets work through again once it has committed, reading the
  counter back from the database like after a Redis loss;
* that the write of the platform-wide counters cannot bring back what it
  deleted;
* that a reset whose commit fails, or that cannot take the locks, changes
  nothing and leaves the code unused;
* that the route commits its audit row with the reset, and writes none for a
  refused code.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy import text as sa_text

import app.services.agent_failure_pool as pool_mod
from app.api.v1 import settings as settings_api
from app.api.v1.schemas.settings import SupportCodeRedeemRequest
from app.models.audit_log import AuditLog
from app.models.quota import QuotaGlobalUsage, QuotaUsage
from app.models.support_code import SupportCodeRedemption
from app.services import quota
from app.services.agent_failure_pool import PLATFORM_QUOTA_RESET_KIND
from app.services.support_codes import redeem_support_code
from tests.test_agent_failure_pool_postgres import (
    _awaitable,
    _FakeRedis,
    _mint,
    _seed_user,
    _signing_key,
    _temp_database,
)

pytestmark = pytest.mark.integration

_PAST_MONTH = "2020-01"

# The unit suite answers every read-back with "nothing saved"
# (``conftest._no_durable_quota_baseline``); these tests are about the rows it
# reads, so they put the real one back.
_REAL_LOAD_GLOBAL_BASELINE = quota._load_global_baseline


@pytest.fixture(autouse=True)
def _real_global_baseline(monkeypatch):
    monkeypatch.setattr(quota, "_load_global_baseline", _REAL_LOAD_GLOBAL_BASELINE)


def _now_keys() -> dict[str, str]:
    return {
        period: quota._current_period_key(period)
        for period in ("daily", "monthly", "yearly")
    }


def _reset_code(key, **claims) -> str:
    return _mint(key, kind=PLATFORM_QUOTA_RESET_KIND, **claims)


async def _commit_fails():
    raise RuntimeError("the commit failed")


async def _global_rows(session) -> dict[tuple[str, str], int]:
    rows = (
        await session.execute(
            select(QuotaGlobalUsage.usage_type, QuotaGlobalUsage.period, QuotaGlobalUsage.count)
        )
    ).all()
    return {(row.usage_type, row.period): row.count for row in rows}


async def _ledger(session_factory) -> list[SupportCodeRedemption]:
    async with session_factory() as session:
        return list((await session.execute(select(SupportCodeRedemption))).scalars())


@pytest.fixture
def redis(monkeypatch) -> _FakeRedis:
    store = _FakeRedis()
    monkeypatch.setattr(quota, "redis_client", lambda: _awaitable(store))
    monkeypatch.setattr(pool_mod, "redis_client", lambda: _awaitable(store))
    return store


async def _seed_platform_usage(session, redis: _FakeRedis, user_id: uuid.UUID) -> None:
    """The license allowance spent: every agentic window full, a sandbox
    counter, an LLM proxy counter, a past month, and one user's own usage.
    Redis runs ahead of the saved marks on two of them (increments not yet
    flushed)."""
    now = _now_keys()
    saved = {
        ("agentic", now["daily"]): 30,
        ("agentic", now["monthly"]): 100,
        ("agentic", now["yearly"]): 300,
        ("sandbox", now["monthly"]): 4,
        ("llm_proxy", now["monthly"]): 50,
        ("agentic", _PAST_MONTH): 90,
    }
    for (usage_type, period), count in saved.items():
        session.add(QuotaGlobalUsage(usage_type=usage_type, period=period, count=count))
    session.add(
        QuotaUsage(
            user_id=user_id,
            usage_type="agentic",
            model=None,
            period=now["monthly"],
            agent_id=None,
            count=12,
        )
    )
    await session.commit()
    live = {
        ("agentic", now["daily"]): 32,
        ("agentic", now["monthly"]): 100,
        ("sandbox", now["monthly"]): 6,
        ("llm_proxy", now["monthly"]): 50,
        ("agentic", _PAST_MONTH): 90,
    }
    for (usage_type, period), count in live.items():
        redis.store[quota._global_usage_key(usage_type, period)] = count
    redis.store[quota._usage_key(user_id, "agentic", None, now["monthly"])] = 12


@pytest.mark.asyncio
async def test_a_reset_clears_the_named_counters_of_the_current_windows_only(
    monkeypatch, redis
):
    key = _signing_key(monkeypatch)
    now = _now_keys()
    async with _temp_database() as session_factory:
        async with session_factory() as session:
            user = await _seed_user(session)
            await _seed_platform_usage(session, redis, user.id)
            result = await redeem_support_code(
                session, _reset_code(key, note="POC-118"), user.id
            )

            assert result["kind"] == PLATFORM_QUOTA_RESET_KIND
            assert result["note"] == "POC-118"
            assert result["quota_types"] == ["agentic", "sandbox"]
            assert result["periods"] == ["daily", "monthly", "yearly"]
            # What each counter held: the larger of the live count and the saved
            # mark, which is what the gate was comparing with the ceiling.
            assert result["counters"] == [
                {"quota_type": "agentic", "period": "daily", "period_key": now["daily"], "previous_count": 32},
                {"quota_type": "agentic", "period": "monthly", "period_key": now["monthly"], "previous_count": 100},
                {"quota_type": "agentic", "period": "yearly", "period_key": now["yearly"], "previous_count": 300},
                {"quota_type": "sandbox", "period": "daily", "period_key": now["daily"], "previous_count": 0},
                {"quota_type": "sandbox", "period": "monthly", "period_key": now["monthly"], "previous_count": 6},
                {"quota_type": "sandbox", "period": "yearly", "period_key": now["yearly"], "previous_count": 0},
            ]

        async with session_factory() as session:
            assert await _global_rows(session) == {
                ("llm_proxy", now["monthly"]): 50,
                ("agentic", _PAST_MONTH): 90,
            }
            user_rows = (await session.execute(select(QuotaUsage.count))).scalars().all()
            assert user_rows == [12]
        assert redis.store == {
            quota._global_usage_key("llm_proxy", now["monthly"]): 50,
            quota._global_usage_key("agentic", _PAST_MONTH): 90,
            quota._usage_key(user.id, "agentic", None, now["monthly"]): 12,
        }
        assert quota._CLEAR_STASH_INDEX not in redis.hashes

        ledger = await _ledger(session_factory)
        assert [(row.kind, row.note, row.redeemed_by_user_id) for row in ledger] == [
            (PLATFORM_QUOTA_RESET_KIND, "POC-118", user.id)
        ]


@pytest.mark.asyncio
async def test_a_narrowed_reset_leaves_the_windows_it_does_not_name(monkeypatch, redis):
    key = _signing_key(monkeypatch)
    now = _now_keys()
    async with _temp_database() as session_factory:
        async with session_factory() as session:
            user = await _seed_user(session)
            await _seed_platform_usage(session, redis, user.id)
            await redeem_support_code(
                session,
                _reset_code(key, quota_types=["agentic"], periods=["daily"]),
                user.id,
            )
        async with session_factory() as session:
            rows = await _global_rows(session)
    assert ("agentic", now["daily"]) not in rows
    assert rows[("agentic", now["monthly"])] == 100
    assert rows[("agentic", now["yearly"])] == 300
    assert rows[("sandbox", now["monthly"])] == 4
    assert quota._global_usage_key("agentic", now["daily"]) not in redis.store
    assert redis.store[quota._global_usage_key("agentic", now["monthly"])] == 100
    assert redis.store[quota._global_usage_key("sandbox", now["monthly"])] == 6


@pytest.mark.asyncio
async def test_the_ceiling_lets_work_through_again_once_the_reset_committed(
    monkeypatch, redis
):
    """The point of the code: the license ceiling refused every run, and after
    the reset it admits them again. The counter is gone from both stores, so
    the gate reads it back from the database - like after a Redis loss - and
    starts from 0, not from the saved mark."""
    key = _signing_key(monkeypatch)
    now = _now_keys()
    monkeypatch.setattr(quota.settings, "platform_mode", "xtm_one")

    async def _ceilings(_db):
        return {"daily": 30}

    monkeypatch.setattr(quota, "get_xtm_agentic_ceilings", _ceilings)
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        await _seed_platform_usage(session, redis, user.id)

        with pytest.raises(quota.QuotaExceededError):
            await quota.check_agentic_quota(session, None)
        # Losing the live counter is no way around the ceiling: it is read
        # back from its saved mark (30 of 30).
        redis.store.pop(quota._global_usage_key("agentic", now["daily"]))
        with pytest.raises(quota.QuotaExceededError):
            await quota.check_agentic_quota(session, None)
        assert redis.store[quota._global_usage_key("agentic", now["daily"])] == 30

        await redeem_support_code(session, _reset_code(key), user.id)

        await quota.check_agentic_quota(session, None)
        assert redis.store[quota._global_usage_key("agentic", now["daily"])] == 0

        # The next charge counts from there.
        await quota._count_global(redis, "agentic", ["daily"])
        assert redis.store[quota._global_usage_key("agentic", now["daily"])] == 1


@pytest.mark.asyncio
async def test_the_write_of_the_platform_counters_brings_nothing_back(monkeypatch, redis):
    """``_flush_global_usage`` only ever raises a row. Run after the reset, it
    re-reads Redis under its lock and finds nothing to write: the deleted mark
    is not resurrected. A later charge is saved from zero."""
    key = _signing_key(monkeypatch)
    now = _now_keys()
    counter = quota._global_usage_key("agentic", now["daily"])
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        await _seed_platform_usage(session, redis, user.id)

        await redeem_support_code(session, _reset_code(key), user.id)

        assert await quota._flush_global_usage(redis, [counter]) == 0
        async with session_factory() as fresh:
            assert ("agentic", now["daily"]) not in await _global_rows(fresh)

        await quota._count_global(redis, "agentic", ["daily"])
        assert await quota._flush_global_usage(redis, [counter]) == 1
        async with session_factory() as fresh:
            assert (await _global_rows(fresh))[("agentic", now["daily"])] == 1


@pytest.mark.asyncio
async def test_a_reset_whose_commit_fails_changes_nothing_and_keeps_the_code(
    monkeypatch, redis
):
    """The reset commits the redemption with the clear. When that commit
    fails, neither lands: the rows roll back, the counters Redis alone held
    come back from the stash, and the code can be redeemed again."""
    key = _signing_key(monkeypatch)
    now = _now_keys()
    code = _reset_code(key)
    async with _temp_database() as session_factory:
        async with session_factory() as session:
            user = await _seed_user(session)
            await _seed_platform_usage(session, redis, user.id)
            before = dict(redis.store)

        async with session_factory() as admin_session:
            admin_session.commit = _commit_fails
            with pytest.raises(RuntimeError, match="the commit failed"):
                await redeem_support_code(admin_session, code, user.id)

        assert redis.store == before
        async with session_factory() as session:
            assert (await _global_rows(session))[("agentic", now["daily"])] == 30
        assert await _ledger(session_factory) == []

        async with session_factory() as session:
            await redeem_support_code(session, code, user.id)
        assert quota._global_usage_key("agentic", now["daily"]) not in redis.store
        assert len(await _ledger(session_factory)) == 1


@pytest.mark.asyncio
async def test_a_reset_behind_a_stuck_write_of_the_platform_counters_changes_nothing(
    monkeypatch, redis
):
    """The write of the platform-wide counters holds its lock for one
    statement. One that keeps it past the bounded wait refuses the reset
    before anything is cleared, and the code stays unused."""
    key = _signing_key(monkeypatch)
    now = _now_keys()
    monkeypatch.setattr(quota, "_PLATFORM_RESET_GLOBAL_LOCK_WAIT_SECONDS", 0.2)
    async with _temp_database() as session_factory:
        async with session_factory() as session:
            user = await _seed_user(session)
            await _seed_platform_usage(session, redis, user.id)
            before = dict(redis.store)

        async with session_factory() as writer:
            await writer.execute(
                sa_text("SELECT pg_advisory_xact_lock(CAST(:k AS bigint))"),
                {"k": quota._QUOTA_GLOBAL_FLUSH_LOCK_KEY},
            )
            async with session_factory() as admin_session:
                with pytest.raises(quota.QuotaClearBusyError):
                    await redeem_support_code(admin_session, _reset_code(key), user.id)
            await writer.rollback()

        assert redis.store == before
        async with session_factory() as session:
            assert (await _global_rows(session))[("agentic", now["daily"])] == 30
        assert await _ledger(session_factory) == []


@pytest.mark.asyncio
async def test_a_read_back_waits_for_a_reset_in_flight_and_reads_what_it_left(
    monkeypatch, redis
):
    """A gate that misses the counter while the reset has not committed yet
    must not read the row the reset is deleting: it waits on the lock the
    reset holds, then reads none (committed) - 0, not the old mark."""
    now = _now_keys()
    async with _temp_database() as session_factory:
        async with session_factory() as session:
            user = await _seed_user(session)
            await _seed_platform_usage(session, redis, user.id)

        released = asyncio.Event()
        committing = asyncio.Event()

        async with session_factory() as admin_session:
            real_commit = admin_session.commit

            async def _held_commit():
                committing.set()
                await released.wait()
                await real_commit()

            admin_session.commit = _held_commit
            reset = asyncio.create_task(
                quota.reset_platform_quota_usage(admin_session, ["agentic"], ["daily"])
            )
            await asyncio.wait_for(committing.wait(), timeout=10)
            # The counter has left Redis; the row is deleted but not committed.
            assert quota._global_usage_key("agentic", now["daily"]) not in redis.store
            read = asyncio.create_task(
                quota._get_global_count(redis, "agentic", now["daily"])
            )
            await asyncio.sleep(0.3)
            assert not read.done(), "the read-back did not wait for the reset"
            released.set()
            await asyncio.wait_for(reset, timeout=10)
            assert await asyncio.wait_for(read, timeout=10) == 0


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


async def _audit_rows(session_factory) -> list[AuditLog]:
    async with session_factory() as session:
        return list((await session.execute(select(AuditLog))).scalars())


@pytest.mark.asyncio
async def test_the_route_commits_its_audit_row_with_the_reset(monkeypatch, redis):
    key = _signing_key(monkeypatch)
    async with _temp_database() as session_factory:
        async with session_factory() as session:
            user = await _seed_user(session)
            await _seed_platform_usage(session, redis, user.id)

        code = _reset_code(key, id="poc-118-reset", periods=["monthly"])
        async with session_factory() as session:
            result = await settings_api.redeem_quota_support_code(
                SupportCodeRedeemRequest(code=code), request=None, user=user, db=session
            )
        assert result["kind"] == PLATFORM_QUOTA_RESET_KIND

        audit = await _audit_rows(session_factory)
        assert [(row.entity_type, row.summary, row.details) for row in audit] == [
            (
                "platform_settings",
                "Reset the platform-wide usage counters (agentic, sandbox; "
                "current monthly windows) with support code poc-118-reset",
                {
                    "support_code_id": "poc-118-reset",
                    "support_code_kind": PLATFORM_QUOTA_RESET_KIND,
                },
            )
        ]

        # The same code again: refused, and neither burned nor audited twice.
        async with session_factory() as session:
            with pytest.raises(HTTPException) as refused:
                await settings_api.redeem_quota_support_code(
                    SupportCodeRedeemRequest(code=code), request=None, user=user, db=session
                )
            await session.rollback()  # what ``get_db`` does on the way out
        assert refused.value.status_code == 400
        assert refused.value.detail.startswith("This support code has already been used")
        assert len(await _audit_rows(session_factory)) == 1
        assert len(await _ledger(session_factory)) == 1


@pytest.mark.asyncio
async def test_the_route_answers_a_busy_flush_with_a_503_and_no_audit_row(
    monkeypatch, redis
):
    key = _signing_key(monkeypatch)
    monkeypatch.setattr(quota, "_PLATFORM_RESET_GLOBAL_LOCK_WAIT_SECONDS", 0.2)
    async with _temp_database() as session_factory:
        async with session_factory() as session:
            user = await _seed_user(session)

        async with session_factory() as writer:
            await writer.execute(
                sa_text("SELECT pg_advisory_xact_lock(CAST(:k AS bigint))"),
                {"k": quota._QUOTA_GLOBAL_FLUSH_LOCK_KEY},
            )
            async with session_factory() as session:
                with pytest.raises(HTTPException) as busy:
                    await settings_api.redeem_quota_support_code(
                        SupportCodeRedeemRequest(code=_reset_code(key)),
                        request=None,
                        user=user,
                        db=session,
                    )
                await session.rollback()
            await writer.rollback()

        assert busy.value.status_code == 503
        assert busy.value.detail["code"] == "quota_flush_busy"
        assert await _audit_rows(session_factory) == []
        assert await _ledger(session_factory) == []


@pytest.mark.asyncio
async def test_the_route_still_recharges_the_failure_pools(monkeypatch, redis):
    """One route for every kind: a recharge code does what the pool route does."""
    key = _signing_key(monkeypatch)
    async with _temp_database() as session_factory:
        async with session_factory() as session:
            user = await _seed_user(session)

        async with session_factory() as session:
            result = await settings_api.redeem_quota_support_code(
                SupportCodeRedeemRequest(code=_mint(key, id="recharge-9")),
                request=None,
                user=user,
                db=session,
            )
        assert result["kind"] == pool_mod.POOL_RECHARGE_KIND
        assert result["code_id"] == "recharge-9"
        assert result["truncated"] is False
        audit = await _audit_rows(session_factory)
        assert [row.summary for row in audit] == [
            "Recharged all agent failure pools with support code recharge-9"
        ]
