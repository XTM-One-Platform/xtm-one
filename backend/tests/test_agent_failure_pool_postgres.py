"""Support-code redemption against a real PostgreSQL.

Same scratch-database recipe as ``test_flow_runner_postgres.py``: a throwaway
database is created from ``DATABASE_URL`` (skipped when the user lacks
CREATEDB), the whole schema is created, and the database is dropped afterwards.

What a fake session could not honestly exercise, and what is therefore tested
here end to end:

* the UNIQUE constraint on ``support_code_redemptions.code_id`` — the *only*
  thing that makes an offline-verified code single-use;
* that a recharge deletes the pool counters and **nothing else** — a bug that
  widened the ``DELETE`` to real action-quota usage rows would be invisible
  against a stub;
* that a quota reset reads, off its own ``DELETE ... RETURNING``, the windows
  whose credit-back causes it has to forget;
* that a reset or a recharge whose commit fails, or whose process dies before
  it, puts back what it took out of Redis, decided by ``pg_xact_status``;
* the window anchoring on real ``PlatformSettings`` rows.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.enterprise.xtm_ca as xtm_ca_mod
import app.services.agent_failure_pool as pool_mod
from app.models.platform_settings import PlatformSettings
from app.models.quota import QuotaUsage
from app.models.support_code import SupportCodeRedemption
from app.models.user import User
from app.services.agent_failure_pool import (
    POOL_RECHARGE_KIND,
    SUPPORT_CODE_PREFIX,
    SupportCodeError,
    _window_from_anchor,
    redeem_pool_recharge_code,
    resolve_pool_window,
)
from app.services.quota import _POOL_USAGE_TYPE, _current_period_key, _usage_key
from tests.quota_redis_scripts import RedisHashes, run_quota_script
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


class _GetPipeline:
    def __init__(self, redis):
        self._redis = redis
        self._keys: list[str] = []

    def get(self, key):
        self._keys.append(key)
        return self

    async def execute(self):
        return [await self._redis.get(key) for key in self._keys]


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("failpool", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


class _FakeRedis(RedisHashes):
    """Just enough Redis for the recharge sweep and the counter arithmetic."""

    def __init__(self, keys: dict[str, int] | None = None):
        self.store: dict[str, int] = dict(keys or {})
        self.hashes: dict[str, dict[str, int]] = {}

    async def incr(self, key):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def incrby(self, key, amount):
        self.store[key] = self.store.get(key, 0) + amount
        return self.store[key]

    async def decr(self, key):
        self.store[key] = self.store.get(key, 0) - 1
        return self.store[key]

    async def expire(self, key, ttl):
        return True

    async def eval(self, script, numkeys, *keys_and_args):
        return run_quota_script(self.store, self.hashes, script, keys_and_args)

    def pipeline(self, transaction=True):
        return _GetPipeline(self)

    async def scan_iter(self, match: str, count: int = 100):
        # Only the two wildcard shapes the sweep uses ("prefix*middle*").
        head, _, tail = match.partition("*")
        middle, _, _end = tail.rpartition("*")
        for key in list(self.store):
            if key.startswith(head) and middle in key:
                yield key

    async def delete(self, *keys):
        for key in keys:
            self.store.pop(key, None)
            self.hashes.pop(key, None)
        return len(keys)

    async def get(self, key):
        value = self.store.get(key)
        return None if value is None else str(value).encode()

    async def set(self, key, value, *args, **kwargs):
        self.store[key] = int(value) if str(value).lstrip("-").isdigit() else value
        return True

    async def getdel(self, key):
        value = self.store.pop(key, None)
        return None if value is None else str(value).encode()

    async def delete_many(self, *keys):
        for key in keys:
            self.store.pop(key, None)
        return len(keys)


def _signing_key(monkeypatch):
    """Stand in for the Filigran product key: sign here, trust the public half."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    monkeypatch.setattr(xtm_ca_mod, "XTM_CA_PEM", public_pem)
    return key


def _mint(key, **overrides) -> str:
    payload = {
        "v": 1,
        "kind": POOL_RECHARGE_KIND,
        "id": str(uuid.uuid4()),
        "platform": "global",
        "exp": "2099-01-01T00:00:00Z",
    }
    payload.update(overrides)
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = key.sign(raw, padding.PKCS1v15(), hashes.SHA256())

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode().rstrip("=")

    return f"{SUPPORT_CODE_PREFIX}{b64(raw)}.{b64(signature)}"


async def _seed_user(session) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"pool-{uuid.uuid4().hex[:8]}@test.local",
        full_name="Pool Admin",
        hashed_password="x",
    )
    session.add(user)
    await session.commit()
    return user


@pytest.mark.asyncio
async def test_redeeming_a_code_clears_pool_counters_only(monkeypatch):
    """A recharge zeroes the failure pools, in both stores, and leaves billable
    usage untouched."""
    key = _signing_key(monkeypatch)
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        agent_id = uuid.uuid4()
        window = (await resolve_pool_window(session)).key

        # One spent pool row and one billable usage row for the same user.
        session.add(
            QuotaUsage(
                user_id=user.id,
                usage_type=_POOL_USAGE_TYPE,
                model=None,
                period=window,
                agent_id=None,  # no agents table row needed for this FK-free case
                count=3,
            )
        )
        session.add(
            QuotaUsage(
                user_id=user.id,
                usage_type="agentic",
                model=None,
                period="2026-08",
                agent_id=None,
                count=42,
            )
        )
        await session.commit()

        pool_key = _usage_key(user.id, _POOL_USAGE_TYPE, None, window, agent_id)
        usage_key = _usage_key(user.id, "agentic", None, "2026-08")
        redis = _FakeRedis({pool_key: 3, usage_key: 42})
        monkeypatch.setattr(
            pool_mod, "redis_client", lambda: _awaitable(redis)
        )

        result = await redeem_pool_recharge_code(
            session, _mint(key, note="SUPPORT-77"), user.id
        )

        assert result["note"] == "SUPPORT-77"
        # Both counts, reported apart: the durable rows and the live keys
        # overlap only partially, so neither is a total.
        assert result["rows_deleted"] == 1
        assert result["keys_cleared"] == 1
        assert result["truncated"] is False

        # Durable pool rows gone, billable usage intact.
        remaining = (
            await session.execute(
                select(QuotaUsage.usage_type, QuotaUsage.count)
            )
        ).all()
        assert [(r.usage_type, r.count) for r in remaining] == [("agentic", 42)]
        # Live counters: the pool key swept, the usage key left alone.
        assert pool_key not in redis.store
        assert redis.store[usage_key] == 42

        ledger = (
            await session.execute(select(SupportCodeRedemption))
        ).scalars().all()
        assert len(ledger) == 1
        assert ledger[0].code_id == result["code_id"]
        assert ledger[0].redeemed_by_user_id == user.id
        assert ledger[0].kind == POOL_RECHARGE_KIND


@pytest.mark.asyncio
async def test_a_code_cannot_be_redeemed_twice(monkeypatch):
    """Offline verification makes a code infinitely replayable on its own; the
    ledger is what stops the second use."""
    key = _signing_key(monkeypatch)
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        monkeypatch.setattr(
            pool_mod, "redis_client", lambda: _awaitable(_FakeRedis())
        )
        code = _mint(key)

        await redeem_pool_recharge_code(session, code, user.id)
        with pytest.raises(SupportCodeError, match="already been used"):
            await redeem_pool_recharge_code(session, code, user.id)

        ledger = (
            await session.execute(select(SupportCodeRedemption))
        ).scalars().all()
        assert len(ledger) == 1


@pytest.mark.asyncio
async def test_two_distinct_codes_both_redeem(monkeypatch):
    """The single-use rule is per code, not per platform."""
    key = _signing_key(monkeypatch)
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        monkeypatch.setattr(
            pool_mod, "redis_client", lambda: _awaitable(_FakeRedis())
        )

        await redeem_pool_recharge_code(session, _mint(key), user.id)
        await redeem_pool_recharge_code(session, _mint(key), user.id)

        ledger = (
            await session.execute(select(SupportCodeRedemption))
        ).scalars().all()
        assert len(ledger) == 2


@pytest.mark.asyncio
async def test_an_expired_code_leaves_no_trace(monkeypatch):
    """A refused code must not consume its own id — otherwise a clock-skew
    refusal would burn a code the customer never used."""
    key = _signing_key(monkeypatch)
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        monkeypatch.setattr(
            pool_mod, "redis_client", lambda: _awaitable(_FakeRedis())
        )

        with pytest.raises(SupportCodeError, match="expired"):
            await redeem_pool_recharge_code(
                session, _mint(key, exp="2020-01-01T00:00:00Z"), user.id
            )

        ledger = (
            await session.execute(select(SupportCodeRedemption))
        ).scalars().all()
        assert ledger == []


class _LicenseStub:
    """Only the two fields ``resolve_pool_window`` reads off a licence."""

    def __init__(self, validated: bool, start: datetime | None):
        self.license_validated = validated
        self.license_start_date = start


@pytest.mark.asyncio
async def test_window_anchors_on_the_platform_creation_date(monkeypatch):
    """With no readable licence, the renewal date is this instance's own
    anniversary — not January, and not a moving target."""
    import app.enterprise.licensing as licensing_mod

    monkeypatch.setattr(
        licensing_mod,
        "get_xtm_license_info",
        lambda **_kwargs: _LicenseStub(False, None),
    )
    async with _temp_database() as session_factory, session_factory() as session:
        session.add(
            PlatformSettings(
                id=uuid.uuid4(),
                created_at=datetime(2023, 5, 17, 8, 0, tzinfo=UTC),
            )
        )
        await session.commit()

        window = await resolve_pool_window(session)

        assert window.source == "platform"
        assert (window.start.month, window.start.day) == (5, 17)
        assert window.end == window.start.replace(year=window.start.year + 1)
        assert window.start <= datetime.now(UTC) < window.end


@pytest.mark.asyncio
async def test_a_validated_licence_wins_over_the_platform_date(monkeypatch):
    """The pool follows the commercial cycle when there is one to follow."""
    import app.enterprise.licensing as licensing_mod

    monkeypatch.setattr(
        licensing_mod,
        "get_xtm_license_info",
        lambda **_kwargs: _LicenseStub(True, datetime(2022, 11, 3, tzinfo=UTC)),
    )
    async with _temp_database() as session_factory, session_factory() as session:
        session.add(
            PlatformSettings(
                id=uuid.uuid4(),
                created_at=datetime(2023, 5, 17, tzinfo=UTC),
            )
        )
        await session.commit()

        window = await resolve_pool_window(session)

        assert window.source == "xtm_license"
        assert (window.start.month, window.start.day) == (11, 3)


@pytest.mark.asyncio
async def test_an_unvalidated_licence_never_moves_the_anniversary(monkeypatch):
    """A PEM that did not verify carries attacker-controlled dates: moving the
    renewal date would be worth forging one for, so it is ignored."""
    import app.enterprise.licensing as licensing_mod

    monkeypatch.setattr(
        licensing_mod,
        "get_xtm_license_info",
        lambda **_kwargs: _LicenseStub(False, datetime(2022, 11, 3, tzinfo=UTC)),
    )
    async with _temp_database() as session_factory, session_factory() as session:
        session.add(
            PlatformSettings(
                id=uuid.uuid4(),
                created_at=datetime(2023, 5, 17, tzinfo=UTC),
            )
        )
        await session.commit()

        window = await resolve_pool_window(session)

        assert window.source == "platform"
        assert (window.start.month, window.start.day) == (5, 17)


async def _awaitable(value):
    """``redis_client`` is awaited by its callers; hand them a ready value."""
    return value


# ---------------------------------------------------------------------------
# Quota reset - the user's credit causes go with their counters
# ---------------------------------------------------------------------------


class _CauseRedis(_FakeRedis):
    """Counters plus the credit-back cause hashes a quota reset has to forget."""

    def __init__(self, keys=None, hashes=None):
        super().__init__(keys)
        self.hashes.update({key: dict(value) for key, value in (hashes or {}).items()})

    async def scan_iter(self, match: str, count: int = 100):
        import fnmatch

        for key in [*self.store, *self.hashes]:
            if fnmatch.fnmatch(key, match):
                yield key

    async def delete(self, *keys):
        removed = 0
        for key in keys:
            removed += self.store.pop(key, None) is not None
            removed += self.hashes.pop(key, None) is not None
        return removed


@pytest.mark.asyncio
async def test_a_quota_reset_forgets_the_credit_causes_of_its_user(monkeypatch):
    """The reset deletes the user's pool and free rows and learns from
    ``RETURNING`` which windows it removed credits from. The user's own cause
    tallies go, in every window; so does the platform-wide tally of each window
    the user had credits in, which cannot say whose its causes were. Nothing
    of anyone else's goes."""
    from app.services.quota import (
        _FREE_REFUND_USAGE_TYPE,
        _refund_cause_key,
        reset_user_quota_usage,
    )

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        other = await _seed_user(session)
        window, older = "2026-01-01", "2025-01-01"
        for owner, usage_type, period, count in (
            (user, _POOL_USAGE_TYPE, window, 2),
            (user, "agentic", "2026-08", 7),
            (other, _POOL_USAGE_TYPE, window, 1),
        ):
            session.add(
                QuotaUsage(
                    user_id=owner.id,
                    usage_type=usage_type,
                    model=None,
                    period=period,
                    agent_id=None,
                    count=count,
                )
            )
        await session.commit()

        redis = _CauseRedis(
            hashes={
                _refund_cause_key(_POOL_USAGE_TYPE, window): {"unknown": 3},
                _refund_cause_key(_POOL_USAGE_TYPE, older): {"unknown": 1},
                _refund_cause_key(_FREE_REFUND_USAGE_TYPE, window): {
                    "provider_unreachable": 1
                },
                _refund_cause_key(_POOL_USAGE_TYPE, window, user.id): {"tool_error": 1},
                _refund_cause_key(_POOL_USAGE_TYPE, older, user.id): {"tool_error": 4},
                _refund_cause_key(_POOL_USAGE_TYPE, window, other.id): {"tool_error": 1},
            }
        )
        monkeypatch.setattr(
            "app.services.quota.redis_client", lambda: _awaitable(redis)
        )

        await reset_user_quota_usage(session, user.id)

        remaining = (
            await session.execute(select(QuotaUsage.user_id, QuotaUsage.usage_type))
        ).all()
        assert [(r.user_id, r.usage_type) for r in remaining] == [
            (other.id, _POOL_USAGE_TYPE)
        ]
        assert set(redis.hashes) == {
            # The user had no credit in either of these two.
            _refund_cause_key(_POOL_USAGE_TYPE, older),
            _refund_cause_key(_FREE_REFUND_USAGE_TYPE, window),
            _refund_cause_key(_POOL_USAGE_TYPE, window, other.id),
        }


# ---------------------------------------------------------------------------
# The flush against a reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_flush_never_writes_back_what_a_reset_deleted(monkeypatch):
    """The flush scans its counters, then a reset lands before the flush gets
    its lock: the reset deletes the user's counters and rows, and the flush
    must not bring them back from what it scanned."""
    from app.core import database
    from app.services.quota import flush_quota_usage, reset_user_quota_usage

    async with _temp_database() as session_factory:
        async with session_factory() as session:
            user = await _seed_user(session)
        key = _usage_key(user.id, "agentic", None, "2026-09")
        redis = _CauseRedis({key: 3})
        monkeypatch.setattr("app.services.quota.redis_client", lambda: _awaitable(redis))
        flush_sessions = database.async_session
        reset_done = False

        @asynccontextmanager
        async def _a_reset_lands_first():
            nonlocal reset_done
            if not reset_done:
                reset_done = True
                async with session_factory() as admin_session:
                    await reset_user_quota_usage(admin_session, user.id)
            async with flush_sessions() as flush_session:
                yield flush_session

        # Set and restored by hand, inside the scratch database: monkeypatch
        # would restore the scratch-bound factory after the scratch engine is
        # gone.
        database.async_session = _a_reset_lands_first
        try:
            assert await flush_quota_usage() == 0
        finally:
            database.async_session = flush_sessions

        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(QuotaUsage.count).where(QuotaUsage.user_id == user.id)
                )
            ).all()
        assert rows == [], "the flush wrote back a counter the reset had deleted"
        assert key not in redis.store


@pytest.mark.asyncio
async def test_a_reset_waits_for_a_flush_holding_the_lock(monkeypatch):
    """The reset takes the flush's advisory lock, like the recharge: while a
    flush holds it the reset waits, so the flush cannot upsert after the reset
    deleted the rows."""
    import asyncio

    from sqlalchemy import text

    from app.services.quota import _QUOTA_FLUSH_LOCK_KEY, reset_user_quota_usage

    async with _temp_database() as session_factory:
        async with session_factory() as session:
            user = await _seed_user(session)
        redis = _CauseRedis()
        monkeypatch.setattr("app.services.quota.redis_client", lambda: _awaitable(redis))

        async with session_factory() as flush_session:
            await flush_session.execute(
                text("SELECT pg_advisory_xact_lock(CAST(:k AS bigint))"),
                {"k": _QUOTA_FLUSH_LOCK_KEY},
            )

            async def _reset():
                async with session_factory() as admin_session:
                    await reset_user_quota_usage(admin_session, user.id)

            reset = asyncio.create_task(_reset())
            await asyncio.sleep(0.5)
            assert not reset.done(), "the reset ran while a flush held the lock"
            await flush_session.commit()

        await asyncio.wait_for(reset, timeout=10)


@pytest.mark.asyncio
async def test_a_reset_behind_a_lock_that_stays_taken_resets_nothing(monkeypatch):
    """Whoever holds the flush lock past the bounded wait (a flush that cannot
    stand aside, stuck in one statement) may have read the counters already:
    going ahead without the lock let it write them back. The reset is
    refused before it clears anything, and never waits on the statement
    timeout."""
    import asyncio

    from sqlalchemy import text

    from app.services import quota
    from app.services.quota import (
        _QUOTA_FLUSH_LOCK_KEY,
        QuotaClearBusyError,
        reset_user_quota_usage,
    )

    monkeypatch.setattr(quota, "_RESET_FLUSH_LOCK_WAIT_SECONDS", 0.3)
    async with _temp_database() as session_factory:
        async with session_factory() as session:
            user = await _seed_user(session)
            session.add(
                QuotaUsage(
                    user_id=user.id,
                    usage_type="agentic",
                    model=None,
                    period="2026-09",
                    agent_id=None,
                    count=4,
                )
            )
            await session.commit()
        key = _usage_key(user.id, "agentic", None, "2026-09")
        redis = _CauseRedis({key: 5})
        monkeypatch.setattr("app.services.quota.redis_client", lambda: _awaitable(redis))

        async with session_factory() as flush_session:
            await flush_session.execute(
                text("SELECT pg_advisory_xact_lock(CAST(:k AS bigint))"),
                {"k": _QUOTA_FLUSH_LOCK_KEY},
            )
            async with session_factory() as admin_session:
                with pytest.raises(QuotaClearBusyError):
                    await asyncio.wait_for(
                        reset_user_quota_usage(admin_session, user.id), timeout=10
                    )
            await flush_session.rollback()

        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(QuotaUsage.count).where(QuotaUsage.user_id == user.id)
                )
            ).all()
        assert [row.count for row in rows] == [4]
        assert redis.store[key] == 5
        assert quota._FLUSH_YIELD_KEY in redis.store, "the flush was not asked to yield"


@pytest.mark.asyncio
async def test_a_long_flush_stands_aside_for_a_reset_instead_of_writing_back(
    monkeypatch,
):
    """A flush is writing a user's counters, one row at a time, when an admin
    resets that user. It read the counters before the reset: a reset going
    ahead without its lock after the bounded wait deleted them, and the flush
    then committed them back - the pool spent again until the next reset.
    The flush now stands aside within a row, the reset takes the lock,
    and nothing the reset deleted comes back."""
    import asyncio

    from app.core import database
    from app.services import quota
    from app.services.quota import flush_quota_usage, reset_user_quota_usage

    # The order alone decides it: the flush stands aside, then the reset takes
    # the lock. A wall-clock window here failed on a loaded runner after the
    # flush had stood aside; the bounded refusal has its own test above, with
    # the lock held.
    monkeypatch.setattr(quota, "_RESET_FLUSH_LOCK_WAIT_SECONDS", float("inf"))
    monkeypatch.setattr(quota, "_FLUSH_YIELD_CHECK_ROWS", 1)
    async with _temp_database() as session_factory:
        async with session_factory() as session:
            user = await _seed_user(session)
        redis = _CauseRedis(
            {_usage_key(user.id, "agentic", None, f"2026-{m:02d}"): 3 for m in range(1, 13)}
        )
        monkeypatch.setattr("app.services.quota.redis_client", lambda: _awaitable(redis))
        reset: asyncio.Task | None = None

        async def _reset():
            async with session_factory() as admin_session:
                await reset_user_quota_usage(admin_session, user.id)

        class _SlowRows:
            """The flush's session, each row taking a tenth of a second; the
            first one is where the admin clicks "Reset usage"."""

            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            async def execute(self, stmt, *args, **kwargs):
                nonlocal reset
                if getattr(stmt, "_for_update_arg", None) is not None:
                    if reset is None:
                        reset = asyncio.create_task(_reset())
                        for _ in range(100):
                            if quota._FLUSH_YIELD_KEY in redis.store:
                                break
                            await asyncio.sleep(0.05)
                    await asyncio.sleep(0.1)
                return await self._real.execute(stmt, *args, **kwargs)

        flush_sessions = database.async_session

        @asynccontextmanager
        async def _slow_flush_session():
            async with session_factory() as real:
                yield _SlowRows(real)

        database.async_session = _slow_flush_session
        try:
            assert await flush_quota_usage() == 0, "the flush did not stand aside"
        finally:
            database.async_session = flush_sessions
        assert reset is not None
        await asyncio.wait_for(reset, timeout=60)

        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(QuotaUsage.count).where(QuotaUsage.user_id == user.id)
                )
            ).all()
        assert rows == [], "the flush wrote back counters the reset deleted"
        assert not [key for key in redis.store if key.startswith(f"quota:usage:{user.id}")]


# ---------------------------------------------------------------------------
# A cold claim against a reset or a recharge in flight
# ---------------------------------------------------------------------------


@dataclass
class _PooledPair:
    """One (user, agent) pair whose durable pool row holds *count* slots."""

    sessions: async_sessionmaker
    redis: _FakeRedis
    user_id: uuid.UUID
    agent_id: uuid.UUID
    pool_key: str

    async def claim(self, outcome_out: dict | None = None) -> bool:
        import asyncio

        from app.services import quota

        async with self.sessions() as claim_session:
            return await asyncio.wait_for(
                quota._claim_failure_pool_slot_and_refund(
                    self.redis,
                    self.user_id,
                    self.agent_id,
                    claim_session,
                    "monthly",
                    3,
                    "test",
                    outcome_out=outcome_out,
                ),
                timeout=10,
            )

    async def reset_in_flight(self, admin_session) -> None:
        """What a quota reset has done before it commits: the user's
        pool-clear lock, the key swept, the durable row deleted."""
        from sqlalchemy import delete

        from app.services import quota

        await quota._lock_pool_clear(admin_session, self.user_id)
        self.redis.store.pop(self.pool_key, None)
        await admin_session.execute(
            delete(QuotaUsage).where(QuotaUsage.user_id == self.user_id)
        )


@asynccontextmanager
async def _pooled_pair(monkeypatch, count: int) -> AsyncIterator[_PooledPair]:
    from app.models.agent import Agent
    from app.services import quota

    window = _window_from_anchor(datetime(2026, 1, 1, tzinfo=UTC), datetime.now(UTC), "test")
    async with _temp_database() as session_factory:
        async with session_factory() as session:
            user = await _seed_user(session)
            agent = Agent(
                id=uuid.uuid4(),
                user_id=user.id,
                name="Pooled Agent",
                slug=f"pooled-{uuid.uuid4().hex[:6]}",
            )
            session.add(agent)
            await session.flush()
            session.add(
                QuotaUsage(
                    user_id=user.id,
                    usage_type=_POOL_USAGE_TYPE,
                    model=None,
                    period=window.key,
                    agent_id=agent.id,
                    count=count,
                )
            )
            await session.commit()
            user_id, agent_id = user.id, agent.id

        redis = _FakeRedis()
        monkeypatch.setattr("app.services.quota.redis_client", lambda: _awaitable(redis))
        monkeypatch.setattr(pool_mod, "redis_client", lambda: _awaitable(redis))
        monkeypatch.setattr(pool_mod, "resolve_pool_window", AsyncMock(return_value=window))
        monkeypatch.setattr(
            quota, "_resolve_agentic_periods", AsyncMock(return_value=("monthly", set()))
        )
        yield _PooledPair(
            session_factory,
            redis,
            user_id,
            agent_id,
            quota._failure_pool_key(user_id, agent_id, window.key),
        )


async def _still_waiting(task) -> bool:
    import asyncio

    await asyncio.sleep(0.5)
    return not task.done()


@pytest.mark.asyncio
async def test_a_cold_claim_waits_for_a_reset_and_counts_from_what_it_left(monkeypatch):
    """A reset has swept the user's pool key and deleted the durable row, and
    has not committed: to every other session the row still reads "spent",
    and whether the pool holds nothing or that count depends on the commit.
    A failure claimed in that window waits for the reset, then counts from
    what it left: here the reset commits, so the claim is the first of a
    whole pool. With no admin action in flight the row is still folded in,
    so a lost cache cannot refill a pool."""
    import asyncio

    async with _pooled_pair(monkeypatch, 3) as pair:
        assert await pair.claim() is False, "the durable row says the pool is spent"
        assert pair.redis.store[pair.pool_key] == 3

        async with pair.sessions() as admin_session:
            await pair.reset_in_flight(admin_session)
            claim = asyncio.create_task(pair.claim())
            assert await _still_waiting(claim), "the claim did not wait for the reset"
            await admin_session.commit()

        assert await claim is True
        assert pair.redis.store[pair.pool_key] == 1


@pytest.mark.asyncio
async def test_a_reset_that_rolls_back_leaves_the_pool_as_spent_as_it_was(monkeypatch):
    """The reset fails after sweeping the key: its transaction rolls back and
    the durable row survives. A claim that counted from zero meanwhile would
    have warmed a counter at 1 that the restored row is never read into again,
    handing out refunds past a spent pool. It waits, and
    reads the restored row."""
    import asyncio

    async with _pooled_pair(monkeypatch, 3) as pair:
        async with pair.sessions() as admin_session:
            await pair.reset_in_flight(admin_session)
            claim = asyncio.create_task(pair.claim())
            assert await _still_waiting(claim), "the claim did not wait for the reset"
            await admin_session.rollback()

        assert await claim is False
        assert pair.redis.store[pair.pool_key] == 3
        assert await pair.claim() is False
        assert pair.redis.store[pair.pool_key] == 3


@pytest.mark.asyncio
async def test_a_recharge_that_rolls_back_leaves_every_pool_as_it_was(monkeypatch):
    """The same with the platform-wide clear: the recharge has deleted every
    pool row and swept every pool key, then its commit fails."""
    import asyncio

    async with _pooled_pair(monkeypatch, 3) as pair:
        await pair.claim()
        release = asyncio.Event()

        async def _commit_fails_when_released():
            await release.wait()
            raise RuntimeError("the commit failed")

        async with pair.sessions() as admin_session:
            admin_session.commit = _commit_fails_when_released
            recharge = asyncio.create_task(pool_mod._clear_pool_counters(admin_session))
            await _until(lambda: pair.pool_key not in pair.redis.store)
            claim = asyncio.create_task(pair.claim())
            assert await _still_waiting(claim), "the claim did not wait for the recharge"
            release.set()
            with pytest.raises(RuntimeError, match="the commit failed"):
                await asyncio.wait_for(recharge, timeout=10)

        assert await claim is False
        assert pair.redis.store[pair.pool_key] == 3


async def _until(condition, timeout: float = 5.0) -> None:
    import asyncio

    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.02)


async def _commit_fails():
    raise RuntimeError("the commit failed")


@pytest.mark.asyncio
async def test_a_reset_whose_commit_fails_loses_no_increment(monkeypatch):
    """The durable row says 1, Redis says 3: two refunds granted since the last
    flush, which only Redis holds. The reset's commit fails after its counters
    left Redis - deleted, those two were lost, and the next failure was decided
    against the row and granted on a spent pool. They come back, and
    the pool stays spent."""
    from app.services import quota

    async with _pooled_pair(monkeypatch, 1) as pair:
        pair.redis.store[pair.pool_key] = 3
        async with pair.sessions() as admin_session:
            admin_session.commit = _commit_fails
            with pytest.raises(RuntimeError, match="the commit failed"):
                await quota.reset_user_quota_usage(admin_session, pair.user_id)

        assert pair.redis.store.get(pair.pool_key) == 3
        assert await pair.claim() is False
        assert pair.redis.store[pair.pool_key] == 3
        async with pair.sessions() as session:
            rows = (
                await session.execute(
                    select(QuotaUsage.count).where(QuotaUsage.user_id == pair.user_id)
                )
            ).all()
        assert [row.count for row in rows] == [1], "the rolled-back reset kept the row"


@pytest.mark.asyncio
async def test_a_stash_a_dead_reset_left_behind_is_put_back_by_the_next_flush(
    monkeypatch,
):
    """The process running the reset dies between moving the counters out of
    Redis and its commit: nothing of it runs again and PostgreSQL rolls its
    transaction back. The next flush reads that from ``pg_xact_status`` and
    puts the counters back, even with nothing of its own to write."""
    from app.core import database
    from app.services import quota

    async with _pooled_pair(monkeypatch, 1) as pair:
        pair.redis.store[pair.pool_key] = 3

        async def _process_dies(db, _redis):
            await db.rollback()

        monkeypatch.setattr(quota, "_abandon_clear", _process_dies)
        async with pair.sessions() as admin_session:
            admin_session.commit = _commit_fails
            with pytest.raises(RuntimeError, match="the commit failed"):
                await quota.reset_user_quota_usage(admin_session, pair.user_id)
        assert pair.pool_key not in pair.redis.store

        flush_sessions = database.async_session
        database.async_session = pair.sessions
        try:
            await quota.flush_quota_usage()
        finally:
            database.async_session = flush_sessions

        assert pair.redis.store.get(pair.pool_key) == 3
        assert quota._CLEAR_STASH_INDEX not in pair.redis.hashes
        assert await pair.claim() is False


@pytest.mark.asyncio
async def test_a_stash_of_a_committed_reset_is_dropped_not_put_back(monkeypatch):
    """The reverse: the reset committed and its process died before it could
    drop the stash. Putting it back would undo the reset."""
    from app.core import database
    from app.services import quota

    async with _pooled_pair(monkeypatch, 1) as pair:
        pair.redis.store[pair.pool_key] = 3
        drop = quota._drop_clear_stash

        async def _drop_fails(_redis, _token):
            raise ConnectionError("Redis went away")

        monkeypatch.setattr(quota, "_drop_clear_stash", _drop_fails)
        async with pair.sessions() as admin_session:
            await quota.reset_user_quota_usage(admin_session, pair.user_id)
        monkeypatch.setattr(quota, "_drop_clear_stash", drop)
        assert quota._CLEAR_STASH_INDEX in pair.redis.hashes

        flush_sessions = database.async_session
        database.async_session = pair.sessions
        try:
            await quota.flush_quota_usage()
        finally:
            database.async_session = flush_sessions

        assert pair.pool_key not in pair.redis.store
        assert quota._CLEAR_STASH_INDEX not in pair.redis.hashes


@pytest.mark.asyncio
async def test_a_recharge_whose_commit_fails_loses_no_increment_nor_the_code(
    monkeypatch,
):
    """The recharge commits the redemption with the clear. When that commit
    fails, neither lands: the counters Redis alone held come back, and the
    code can be redeemed again."""
    key = _signing_key(monkeypatch)
    async with _pooled_pair(monkeypatch, 1) as pair:
        pair.redis.store[pair.pool_key] = 3
        code = _mint(key)
        async with pair.sessions() as admin_session:
            admin_session.commit = _commit_fails
            with pytest.raises(RuntimeError, match="the commit failed"):
                await redeem_pool_recharge_code(admin_session, code, pair.user_id)

        assert pair.redis.store.get(pair.pool_key) == 3
        assert await pair.claim() is False
        async with pair.sessions() as admin_session:
            result = await redeem_pool_recharge_code(admin_session, code, pair.user_id)
        assert (result["rows_deleted"], result["keys_cleared"]) == (1, 1)
        assert pair.pool_key not in pair.redis.store


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "count", "granted"),
    [
        # Rolled back: the one slot left by the restored row, and not one more.
        ("rollback", 2, [False, False, False, True]),
        # Committed: a whole pool of three for four failures.
        ("commit", 2, [False, True, True, True]),
    ],
)
async def test_claims_waiting_on_a_reset_share_the_pool_it_leaves(
    monkeypatch, outcome, count, granted
):
    """Four failures of one pair claim while the reset is in flight. However
    it ends, they are decided together against the pool it leaves: never more
    refunds than that pool holds, and none it holds refused."""
    import asyncio

    async with _pooled_pair(monkeypatch, count) as pair:
        async with pair.sessions() as admin_session:
            await pair.reset_in_flight(admin_session)
            claims = [asyncio.create_task(pair.claim()) for _ in range(4)]
            await asyncio.sleep(0.5)
            assert not any(task.done() for task in claims)
            await getattr(admin_session, outcome)()

        assert sorted(await asyncio.gather(*claims)) == granted
        assert pair.redis.store[pair.pool_key] == 3


@pytest.mark.asyncio
async def test_a_claim_does_not_outwait_a_clear_that_never_ends(monkeypatch):
    """A clear still in flight after the bounded wait leaves the pool
    unknown: the claim is not granted, reports why, and seeds nothing - so
    whatever the clear then does, the pool it leaves is the one the next
    claim reads."""
    from app.services import quota

    monkeypatch.setattr(quota, "_POOL_CLEAR_WAIT_SECONDS", 0.3)
    async with _pooled_pair(monkeypatch, 3) as pair:
        async with pair.sessions() as admin_session:
            await pair.reset_in_flight(admin_session)
            report: dict = {}
            assert await pair.claim(report) is False
            assert report["outcome"] == quota.REFUND_OUTCOME_FAILED
            assert pair.pool_key not in pair.redis.store
            await admin_session.rollback()

        assert await pair.claim() is False
        assert pair.redis.store[pair.pool_key] == 3


# ---------------------------------------------------------------------------
# Abandoned run — end to end against the real tables
# ---------------------------------------------------------------------------


def _no_detached_run_endings(monkeypatch) -> None:
    """Keep the run ending's detached tasks off the test's event loop.

    ``AssignmentAdapter.on_dead_letter`` schedules the conversation wake
    (``assignment_run_wake.schedule``: a 15 s sleep, then a claim on the
    configured database) and the sandbox workspace collection
    (``collect_runs_soon``: the object store and the configured database) in
    tasks of their own. Neither is what these tests are about, and neither
    points at the scratch database: left on the loop, pytest-asyncio's teardown
    cancels them and waits, and one that did not end on its cancel held the
    Bootstrap shard until the step's 25-minute timeout (main run 42403246723).
    ``test_assignment_run_resume.py`` stubs the wake the same way.
    """
    monkeypatch.setattr("app.services.assignment_run_wake.schedule", lambda run_id: None)
    monkeypatch.setattr("app.services.assignment_engine._collect_run_workspace", lambda run: None)


@pytest.mark.asyncio
async def test_dead_lettered_run_credits_its_action_back(monkeypatch):
    """A charged run the engine gives up on gets its action back, for free.

    Driven through the real ``on_dead_letter`` against real rows, because the
    whole point of this path is that it reads the charge from the DB rather
    than from any in-memory state.
    """
    from app.models.agent import Agent
    from app.models.assignment import AssignmentRun
    from app.models.orchestration import OrchestrationExecution
    from app.services.orchestration_adapters.assignment import AssignmentAdapter
    from app.services.orchestration_adapters.base import AdapterContext
    from app.services.quota import _free_refund_key, _usage_key

    _no_detached_run_endings(monkeypatch)
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        agent = Agent(id=uuid.uuid4(), user_id=user.id, name="Reaped Agent", slug=f"reaped-{uuid.uuid4().hex[:6]}")
        session.add(agent)
        await session.flush()
        run = AssignmentRun(
            id=uuid.uuid4(),
            agent_id=agent.id,
            user_id=user.id,
            status="running",
            started_at=datetime.now(UTC),
            # Written by the engine at charge time — the only durable trace of
            # the charge this out-of-process path can read.
            trigger_context={
                "quota_metered": True,
                "quota_metered_user_id": str(user.id),
            },
        )
        session.add(run)
        execution = OrchestrationExecution(
            id=uuid.uuid4(),
            adapter="assignment",
            state="dead_letter",
            payload={"run_id": str(run.id)},
            last_error="Deadline exceeded at 2026-08-26T09:00:00+00:00.",
        )
        session.add(execution)
        await session.commit()

        # One action charged, as the engine would have.
        redis = _FakeRedis()
        monkeypatch.setattr(
            "app.services.quota.redis_client", lambda: _awaitable(redis)
        )
        monkeypatch.setattr(
            pool_mod, "redis_client", lambda: _awaitable(redis)
        )
        from app.services.quota import increment_agentic_usage

        await increment_agentic_usage(user.id, agent_id=agent.id)
        # The refund resolves the pool window on a private session (it must
        # never touch the caller's, which is mid-failure), and that session
        # points at the configured database rather than this scratch one. Pin
        # the window so the assertion is about the credit, not about which
        # database answered.
        fixed_window = _window_from_anchor(
            datetime(2026, 1, 1, tzinfo=UTC), datetime.now(UTC), "test"
        )
        monkeypatch.setattr(
            pool_mod, "resolve_pool_window", AsyncMock(return_value=fixed_window)
        )
        window = fixed_window.key
        period_key = _current_period_key("monthly")
        assert redis.store[_usage_key(user.id, "agentic", None, period_key)] == 1

        await AssignmentAdapter().on_dead_letter(
            AdapterContext(db=session, worker_id="test"), execution
        )
        await session.commit()

        await session.refresh(run)
        assert run.status == "failed"
        # The action is back...
        assert redis.store[_usage_key(user.id, "agentic", None, period_key)] == 0
        # ...credited as free, and no pool slot spent.
        assert redis.store[_free_refund_key(user.id, agent.id, window)] == 1
        assert _failure_pool_key_absent(redis, user.id, agent.id, window)


@pytest.mark.asyncio
async def test_dead_lettered_run_that_never_started_is_not_credited(monkeypatch):
    """Nobody ever claimed it, so nothing was ever charged: crediting here would
    take an action from a different run. This is the demo instance's own case —
    six scheduled runs killed by a missing worker, none of them charged."""
    from app.models.agent import Agent
    from app.models.assignment import AssignmentRun
    from app.models.orchestration import OrchestrationExecution
    from app.services.orchestration_adapters.assignment import AssignmentAdapter
    from app.services.orchestration_adapters.base import AdapterContext
    from app.services.quota import _usage_key, increment_agentic_usage

    _no_detached_run_endings(monkeypatch)
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        agent = Agent(id=uuid.uuid4(), user_id=user.id, name="Never Ran", slug=f"never-{uuid.uuid4().hex[:6]}")
        session.add(agent)
        await session.flush()
        run = AssignmentRun(
            id=uuid.uuid4(),
            agent_id=agent.id,
            user_id=user.id,
            status="pending",
            # No quota_metered flag: the run never reached the charge.
            trigger_context={"type": "schedule"},
        )
        session.add(run)
        execution = OrchestrationExecution(
            id=uuid.uuid4(),
            adapter="assignment",
            state="dead_letter",
            payload={"run_id": str(run.id)},
            last_error="Deadline exceeded.",
        )
        session.add(execution)
        await session.commit()

        redis = _FakeRedis()
        monkeypatch.setattr(
            "app.services.quota.redis_client", lambda: _awaitable(redis)
        )
        monkeypatch.setattr(pool_mod, "redis_client", lambda: _awaitable(redis))
        # Another run's legitimate usage, which must survive untouched.
        other_user = await _seed_user(session)
        await increment_agentic_usage(other_user.id, agent_id=agent.id)
        period_key = _current_period_key("monthly")

        await AssignmentAdapter().on_dead_letter(
            AdapterContext(db=session, worker_id="test"), execution
        )
        await session.commit()

        await session.refresh(run)
        assert run.status == "failed"
        assert redis.store[_usage_key(other_user.id, "agentic", None, period_key)] == 1


def _failure_pool_key_absent(redis, user_id, agent_id, window) -> bool:
    from app.services.quota import _failure_pool_key

    return _failure_pool_key(user_id, agent_id, window) not in redis.store


@pytest.mark.asyncio
async def test_abandoned_sub_agent_task_credits_its_action_back(monkeypatch):
    """A background task charged inside its agent turn, then abandoned by the
    engine, gets its action back — driven through the real adapter, because the
    whole point is that this runs where the turn no longer exists."""
    from app.models.agent import Agent
    from app.models.orchestration import OrchestrationExecution
    from app.models.sub_agent_task import SubAgentTask
    from app.services.orchestration_adapters.base import AdapterContext
    from app.services.orchestration_adapters.sub_agent import SubAgentAdapter
    from app.services.quota import (
        _free_refund_key,
        _usage_key,
        increment_agentic_usage,
    )

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        parent = Agent(id=uuid.uuid4(), user_id=user.id, name="Parent", slug=f"p-{uuid.uuid4().hex[:6]}")
        target = Agent(id=uuid.uuid4(), user_id=user.id, name="Worker", slug=f"w-{uuid.uuid4().hex[:6]}")
        session.add_all([parent, target])
        await session.flush()
        task = SubAgentTask(
            id=uuid.uuid4(),
            user_id=user.id,
            parent_agent_id=parent.id,
            target_agent_id=target.id,
            task_description="do the thing",
            status="running",
        )
        session.add(task)
        execution = OrchestrationExecution(
            id=uuid.uuid4(),
            adapter="sub_agent",
            state="dead_letter",
            payload={"task_id": str(task.id)},
            last_error="Deadline exceeded.",
        )
        session.add(execution)
        await session.commit()

        redis = _FakeRedis()
        monkeypatch.setattr(
            "app.services.quota.redis_client", lambda: _awaitable(redis)
        )
        monkeypatch.setattr(pool_mod, "redis_client", lambda: _awaitable(redis))
        fixed_window = _window_from_anchor(
            datetime(2026, 1, 1, tzinfo=UTC), datetime.now(UTC), "test"
        )
        monkeypatch.setattr(
            pool_mod, "resolve_pool_window", AsyncMock(return_value=fixed_window)
        )

        # The charge, exactly as the agent turn makes it.
        await increment_agentic_usage(
            user.id, agent_id=target.id, receipt_token=f"sub_agent_task:{task.id}"
        )
        period_key = _current_period_key("monthly")
        assert redis.store[_usage_key(user.id, "agentic", None, period_key)] == 1

        await SubAgentAdapter().on_dead_letter(
            AdapterContext(db=session, worker_id="test"), execution
        )
        await session.commit()

        await session.refresh(task)
        assert task.status == "failed"
        assert redis.store[_usage_key(user.id, "agentic", None, period_key)] == 0
        assert redis.store[_free_refund_key(user.id, target.id, fixed_window.key)] == 1
        # The receipt is redeemed, so a second sweep credits nothing.
        assert redis.store[f"quota:receipt:sub_agent_task:{task.id}"] == "redeemed"
        await SubAgentAdapter().on_dead_letter(
            AdapterContext(db=session, worker_id="test"), execution
        )
        assert redis.store[_usage_key(user.id, "agentic", None, period_key)] == 0
        assert redis.store[_free_refund_key(user.id, target.id, fixed_window.key)] == 1
