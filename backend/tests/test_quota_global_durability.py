"""The platform-wide quota counters survive a Redis wipe.

The platform-wide agentic limits - the license ceilings in XTM One mode,
``platform_limit`` in Copilot mode - are enforced against the ``_global`` Redis
counters and nothing else. The flush used to skip them (``quota_usage`` needs a
user), so an eviction, a restart without persistence, a failover to a cold
replica or a yearly key lapsing after 35 idle days reopened every platform-wide
ceiling for the rest of its period.

Pinned here against in-memory doubles: the flush saves them to
``quota_global_usage`` with no user, first and under a lock of their own that
their read-back waits on, never the per-user flush's; a counter Redis
lost is read back at its
saved high-water mark before the gate reads it or a charge increments it, once
even under racing charges; a counter Redis still holds is never re-read; a
refund never puts one back; a read-back that fails creates nothing and refuses
in XTM One. ``test_quota_global_postgres.py`` runs the same story against a
real PostgreSQL and Redis.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.dialects import postgresql

from app.core import config
from app.models.quota import QuotaUsage
from app.services import agentic_executions
from app.services import quota as quota_module
from app.services.agent_chat import enforce_agentic_quota_ex
from app.services.quota import (
    QUOTA_UNAVAILABLE_MESSAGE,
    QuotaExceededError,
    _current_period_key,
    _global_usage_key,
    _usage_key,
    check_agentic_quota,
    flush_quota_usage,
    get_user_quota_usage,
    increment_agentic_usage,
    increment_llm_proxy_usage,
    increment_sandbox_usage,
    refund_agentic_usage,
)
from tests.frozen_clock import FROZEN_NOW
from tests.quota_redis_scripts import RedisHashes, run_quota_script

# The periods of the frozen instant the code under test reads too (conftest's
# ``_frozen_quota_clock``): a run crossing midnight UTC keeps them one.
MONTH = _current_period_key("monthly", FROZEN_NOW)
YEAR = _current_period_key("yearly", FROZEN_NOW)

# Captured at import, before the suite-wide fixture swaps it for "nothing saved
# yet": the sharing of one read is tested on the real function.
_REAL_LOAD_GLOBAL_BASELINE = quota_module._load_global_baseline


class _Redis(RedisHashes):
    """Counters in a dict, with ``SET NX`` and the expiries a caller asked for."""

    def __init__(self, store: dict | None = None):
        self.store: dict = dict(store or {})
        self.hashes: dict = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key):
        value = self.store.get(key)
        return None if value is None else str(value).encode()

    async def set(self, key, value, nx=False, ex=None, px=None, **kwargs):
        if nx and key in self.store:
            return None
        self.store[key] = int(value) if str(value).lstrip("-").isdigit() else value
        if ex:
            self.ttls[key] = ex
        return True

    async def incr(self, key):
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    async def incrby(self, key, amount):
        self.store[key] = int(self.store.get(key, 0)) + amount
        return self.store[key]

    async def expire(self, key, ttl):
        self.ttls[key] = ttl
        return True

    async def getdel(self, key):
        value = self.store.pop(key, None)
        return None if value is None else str(value).encode()

    async def delete(self, *keys):
        removed = 0
        for key in keys:
            removed += self.store.pop(key, None) is not None
            removed += self.hashes.pop(key, None) is not None
        return removed

    async def eval(self, script, numkeys, *keys_and_args):
        return run_quota_script(self.store, self.hashes, script, keys_and_args)

    async def scan_iter(self, match=None, count=None):
        for key in list(self.store):
            if match is None or fnmatch.fnmatchcase(key, match):
                yield key

    def pipeline(self):
        return _Pipeline(self.store)


class _Pipeline:
    def __init__(self, store: dict):
        self._store = store
        self._keys: list[str] = []

    def get(self, key):
        self._keys.append(key)

    async def execute(self):
        return [
            None if self._store.get(key) is None else str(self._store[key]).encode()
            for key in self._keys
        ]


class _Saved:
    """``quota_global_usage`` as the read-back sees it; counts its reads."""

    def __init__(self, rows: dict[tuple[str, str], int] | None = None, *, fail: bool = False):
        self.rows = dict(rows or {})
        self.fail = fail
        self.reads: list[tuple[str, str]] = []

    async def __call__(self, usage_type: str, period_key: str) -> int:
        self.reads.append((usage_type, period_key))
        await asyncio.sleep(0)
        if self.fail:
            raise ConnectionError("database unreachable")
        return self.rows.get((usage_type, period_key), 0)


@pytest.fixture
def redis(monkeypatch):
    store = _Redis()
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=store))
    monkeypatch.setattr(agentic_executions, "redis_client", AsyncMock(return_value=store))
    return store


def _saved(monkeypatch, rows=None, **kwargs) -> _Saved:
    saved = _Saved(rows, **kwargs)
    monkeypatch.setattr(quota_module, "_load_global_baseline", saved)
    return saved


def _xtm_one(monkeypatch, ceilings: dict[str, int]) -> None:
    monkeypatch.setattr(config.settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(quota_module, "get_xtm_agentic_ceilings", AsyncMock(return_value=ceilings))
    monkeypatch.setattr(quota_module, "resolve_quota_config", AsyncMock(return_value=None))


# ---------------------------------------------------------------------------
# The flush saves the platform-wide counters, attributed to nobody
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, rows=None):
        self._rows = rows or []

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self.scalar()


_GLOBAL_LOCK = quota_module._QUOTA_GLOBAL_FLUSH_LOCK_KEY
_FLUSH_LOCK = quota_module._QUOTA_FLUSH_LOCK_KEY


class _StatementTimeoutError(Exception):
    """What a statement cut by ``statement_timeout`` raises (SQLSTATE 57014)."""

    sqlstate = "57014"


class _FlushSession:
    """The flush's statements, answered by what they are: the locks are free
    but those in *held*, no duplicate rows, *user* exists, no ``quota_usage``
    row yet; the save of the platform-wide counters raises when *unsaved*, and
    its first *timeouts* attempts hit the statement timeout.
    ``log`` keeps, in order, the locks asked for, those saves and the commits."""

    def __init__(
        self,
        user: uuid.UUID,
        *,
        held: frozenset[int] = frozenset(),
        unsaved=False,
        timeouts: int = 0,
    ):
        self._user = user
        self._held = held
        self._unsaved = unsaved
        self._timeouts = timeouts
        self.statements: list = []
        self.added: list = []
        self.log: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, stmt, params=None):
        self.statements.append(stmt)
        if getattr(getattr(stmt, "table", None), "name", None) == "quota_global_usage":
            self.log.append(("save",))
            if self._unsaved:
                raise ConnectionError("quota_global_usage unreachable")
            if self._timeouts:
                self._timeouts -= 1
                raise _StatementTimeoutError("canceling statement due to statement timeout")
            return _Result()
        sql = str(stmt)
        if "pg_try_advisory_xact_lock" in sql:
            self.log.append(("lock", params["k"]))
            return _Result([params["k"] not in self._held])
        if "FROM users" in sql:
            return _Result([(self._user,)])
        return _Result()

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.log.append(("commit",))


def _global_upserts(session: _FlushSession) -> list:
    return [
        stmt
        for stmt in session.statements
        if getattr(getattr(stmt, "table", None), "name", None) == "quota_global_usage"
    ]


def _upserted_rows(stmt) -> set[tuple[str, str, int]]:
    params = stmt.compile(dialect=postgresql.dialect()).params
    rows, index = set(), 0
    while f"usage_type_m{index}" in params:
        rows.add(
            (params[f"usage_type_m{index}"], params[f"period_m{index}"], params[f"count_m{index}"])
        )
        index += 1
    return rows


async def test_the_flush_saves_the_platform_wide_counters_with_no_user(redis):
    user = uuid.uuid4()
    redis.store.update(
        {
            _global_usage_key("agentic", MONTH): 7,
            _global_usage_key("agentic", YEAR): 40,
            _global_usage_key("sandbox", MONTH): 2,
            _global_usage_key("llm_proxy", MONTH): 9,
            # Seeded from nothing saved: no row to write.
            _global_usage_key("sandbox", YEAR): 0,
            _usage_key(user, "agentic", None, MONTH): 5,
        }
    )
    session = _FlushSession(user)

    with patch("app.core.database.async_session", return_value=session):
        assert await flush_quota_usage() == 5

    (upsert,) = _global_upserts(session)
    assert _upserted_rows(upsert) == {
        ("agentic", MONTH, 7),
        ("agentic", YEAR, 40),
        ("sandbox", MONTH, 2),
        ("llm_proxy", MONTH, 9),
    }
    sql = str(upsert.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (usage_type, period) DO UPDATE" in sql
    assert "WHERE quota_global_usage.count < excluded.count" in sql, "a saved mark only grows"
    # The user's own counter is a ``quota_usage`` row as before; nothing of the
    # platform-wide counters is attributed to anybody.
    assert [(row.user_id, row.usage_type, row.count) for row in session.added] == [
        (user, "agentic", 5)
    ]
    assert all(isinstance(row, QuotaUsage) and row.user_id for row in session.added)
    # Saved first, in a transaction of their own under a lock of their own:
    # their read-back waits on that lock, never on the per-user rows.
    assert session.log == [
        ("lock", _GLOBAL_LOCK),
        ("save",),
        ("commit",),
        ("lock", _FLUSH_LOCK),
        ("commit",),
    ]


async def test_a_counter_scanned_twice_is_saved_once(redis, monkeypatch):
    """SCAN may return a key twice, and one ``INSERT ... ON CONFLICT`` may not
    update the same row twice: PostgreSQL would abort every flush."""
    key = _global_usage_key("agentic", MONTH)
    redis.store[key] = 4

    async def _twice(match=None, count=None):
        yield key
        yield key

    monkeypatch.setattr(redis, "scan_iter", _twice)
    session = _FlushSession(uuid.uuid4())

    with patch("app.core.database.async_session", return_value=session):
        assert await flush_quota_usage() == 1

    (upsert,) = _global_upserts(session)
    assert _upserted_rows(upsert) == {("agentic", MONTH, 4)}


async def test_a_flush_with_no_platform_wide_counter_writes_no_saved_mark(redis):
    user = uuid.uuid4()
    redis.store[_usage_key(user, "agentic", None, MONTH)] = 1
    session = _FlushSession(user)

    with patch("app.core.database.async_session", return_value=session):
        assert await flush_quota_usage() == 1

    assert _global_upserts(session) == []
    assert ("lock", _GLOBAL_LOCK) not in session.log


async def test_platform_wide_counters_alone_take_no_flush_lock(redis):
    redis.store[_global_usage_key("agentic", MONTH)] = 3
    session = _FlushSession(uuid.uuid4())

    with patch("app.core.database.async_session", return_value=session):
        assert await flush_quota_usage() == 1

    assert session.log == [("lock", _GLOBAL_LOCK), ("save",), ("commit",)]


def _user_and_global(redis) -> uuid.UUID:
    user = uuid.uuid4()
    redis.store.update(
        {
            _global_usage_key("agentic", MONTH): 7,
            _usage_key(user, "agentic", None, MONTH): 5,
        }
    )
    return user


async def test_the_platform_wide_counters_are_saved_while_another_replica_flushes_the_users(redis):
    """Another replica's per-user flush holds the flush lock for as long as its
    rows take: the platform-wide counters are saved without waiting for it."""
    user = _user_and_global(redis)
    session = _FlushSession(user, held=frozenset({_FLUSH_LOCK}))

    with patch("app.core.database.async_session", return_value=session):
        assert await flush_quota_usage() == 1

    (upsert,) = _global_upserts(session)
    assert _upserted_rows(upsert) == {("agentic", MONTH, 7)}
    assert session.added == []


async def test_the_platform_wide_save_stands_aside_while_its_lock_is_held(redis):
    """Another replica saving them, or a read-back holding their lock: they
    wait for the next cycle, and the users' counters are saved meanwhile."""
    user = _user_and_global(redis)
    session = _FlushSession(user, held=frozenset({_GLOBAL_LOCK}))

    with patch("app.core.database.async_session", return_value=session):
        assert await flush_quota_usage() == 1

    assert _global_upserts(session) == []
    assert [(row.user_id, row.count) for row in session.added] == [(user, 5)]


async def test_a_platform_wide_save_that_fails_leaves_the_users_flush_to_run(redis, caplog):
    user = _user_and_global(redis)
    session = _FlushSession(user, unsaved=True)

    with (
        caplog.at_level("WARNING", logger="app.services.quota"),
        patch("app.core.database.async_session", return_value=session),
    ):
        assert await flush_quota_usage() == 1

    assert [(row.user_id, row.count) for row in session.added] == [(user, 5)]
    assert "could not save the platform-wide counters" in caplog.text


def _save_warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "platform-wide counters" in r.getMessage()]


async def test_a_platform_wide_save_cut_by_a_statement_timeout_is_retried_in_the_cycle(
    redis, monkeypatch
):
    """The save used to ride the per-user retries; on its own it still gets a
    second try at a statement timeout, not a whole flush cycle later."""
    monkeypatch.setattr(quota_module, "_compute_flush_retry_delay", lambda attempt: 0.0)
    user = _user_and_global(redis)
    session = _FlushSession(user, timeouts=1)

    with patch("app.core.database.async_session", return_value=session):
        assert await flush_quota_usage() == 2

    assert session.log == [
        ("lock", _GLOBAL_LOCK),
        ("save",),
        ("lock", _GLOBAL_LOCK),
        ("save",),
        ("commit",),
        ("lock", _FLUSH_LOCK),
        ("commit",),
    ]
    _timed_out, landed = _global_upserts(session)
    assert _upserted_rows(landed) == {("agentic", MONTH, 7)}


async def test_a_platform_wide_save_still_timing_out_waits_for_the_next_cycle_quietly(
    redis, monkeypatch, caplog
):
    monkeypatch.setattr(quota_module, "_compute_flush_retry_delay", lambda attempt: 0.0)
    monkeypatch.setattr(quota_module, "_global_save_failures", 0)
    user = _user_and_global(redis)
    session = _FlushSession(user, timeouts=2)

    with (
        caplog.at_level("WARNING", logger="app.services.quota"),
        patch("app.core.database.async_session", return_value=session),
    ):
        assert await flush_quota_usage() == 1

    assert session.log.count(("save",)) == quota_module._GLOBAL_FLUSH_MAX_ATTEMPTS
    assert [(row.user_id, row.count) for row in session.added] == [(user, 5)]
    (warning,) = _save_warnings(caplog)
    assert "statement timeout" in warning.getMessage()
    assert not warning.exc_info, "a timeout says what it was without a traceback"


async def test_an_outage_logs_its_traceback_once_until_a_save_lands(redis, monkeypatch, caplog):
    """A database outage fails the save every cycle: the first failure of the
    run carries the traceback, the next ones count the cycles."""
    monkeypatch.setattr(quota_module, "_global_save_failures", 0)
    user = _user_and_global(redis)
    cycles = [
        _FlushSession(user, unsaved=True),
        _FlushSession(user, unsaved=True),
        _FlushSession(user),
        _FlushSession(user, unsaved=True),
    ]

    with caplog.at_level("WARNING", logger="app.services.quota"):
        for session in cycles:
            with patch("app.core.database.async_session", return_value=session):
                await flush_quota_usage()

    warnings = _save_warnings(caplog)
    assert [bool(w.exc_info) for w in warnings] == [True, False, True]
    assert "(2 consecutive cycle(s))" in warnings[1].getMessage()
    assert "(1 consecutive cycle(s))" in warnings[2].getMessage(), "a landed save ends the run"


class _ReadBackSession:
    """The read-back's session: every lock free, the saved mark *count*."""

    def __init__(self, count: int):
        self._count = count
        self.locks: list[int] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, stmt, params=None):
        if "pg_try_advisory_xact_lock_shared" in str(stmt):
            self.locks.append(params["k"])
            return _Result([True])
        return _Result([self._count])


async def test_the_read_back_waits_on_the_platform_wide_save_only():
    """Waiting on the flush lock, every read-back of a period rollover - when
    every platform-wide counter is missing at once - outwaited the per-user
    flush, and its quota check failed."""
    session = _ReadBackSession(4)

    with patch("app.core.database.async_session", return_value=session):
        assert await quota_module._read_global_baseline("agentic", MONTH) == 4

    assert session.locks == [_GLOBAL_LOCK]
    assert _GLOBAL_LOCK != _FLUSH_LOCK


# ---------------------------------------------------------------------------
# The gate reads a lost counter back at its saved mark
# ---------------------------------------------------------------------------


async def test_the_license_ceiling_does_not_restart_from_zero_after_a_wipe(redis, monkeypatch):
    _xtm_one(monkeypatch, {"monthly": 5})
    _saved(monkeypatch, {("agentic", MONTH): 5})

    with pytest.raises(QuotaExceededError) as refused:
        await check_agentic_quota(object(), uuid.uuid4())

    assert (refused.value.current, refused.value.limit) == (5, 5)
    key = _global_usage_key("agentic", MONTH)
    assert redis.store[key] == 5, "the counter is back in Redis"
    assert redis.ttls[key] == quota_module._USAGE_KEY_TTL


async def test_the_platform_limit_does_not_restart_from_zero_after_a_wipe(redis, monkeypatch):
    monkeypatch.setattr(config.settings, "platform_mode", "copilot")
    monkeypatch.setattr(
        quota_module,
        "_load_platform_wide_config",
        AsyncMock(return_value={"platform_limit": 6, "period": "monthly"}),
    )
    _saved(monkeypatch, {("agentic", MONTH): 4, ("sandbox", MONTH): 2})

    with pytest.raises(QuotaExceededError) as refused:
        await check_agentic_quota(object(), None)

    assert (refused.value.current, refused.value.limit) == (6, 6)


async def test_a_yearly_ceiling_survives_its_counter_lapsing(redis, monkeypatch):
    """No agentic run for 35 days: the yearly key expired with the monthly one,
    and a new month starts with no key at all. The yearly ceiling still holds
    what was spent earlier in the year."""
    _xtm_one(monkeypatch, {"monthly": 100, "yearly": 7})
    _saved(monkeypatch, {("agentic", YEAR): 7})

    with pytest.raises(QuotaExceededError) as refused:
        await check_agentic_quota(object(), uuid.uuid4())

    assert (refused.value.current, refused.value.period) == (7, "yearly")


async def test_a_counter_redis_still_holds_is_not_read_again(redis, monkeypatch):
    """A refund lowered the live counter below its saved mark: Redis is right,
    and the database is not asked."""
    _xtm_one(monkeypatch, {"monthly": 5})
    saved = _saved(monkeypatch, {("agentic", MONTH): 5})
    redis.store[_global_usage_key("agentic", MONTH)] = 4
    redis.store[_global_usage_key("sandbox", MONTH)] = 0

    await check_agentic_quota(object(), uuid.uuid4())

    assert saved.reads == []


async def test_a_counter_nothing_saved_is_read_from_the_database_once(redis, monkeypatch):
    """A sandbox never used this month has no counter and no row: it is seeded
    at 0, or every request would ask the database again."""
    _xtm_one(monkeypatch, {"monthly": 5})
    saved = _saved(monkeypatch)

    await check_agentic_quota(object(), uuid.uuid4())
    await check_agentic_quota(object(), uuid.uuid4())

    assert sorted(saved.reads) == [("agentic", MONTH), ("sandbox", MONTH)]
    assert redis.store == {
        _global_usage_key("agentic", MONTH): 0,
        _global_usage_key("sandbox", MONTH): 0,
    }


async def test_racing_reads_of_a_lost_counter_share_one_database_read(monkeypatch):
    """After a Redis loss every gate check of a process misses at once, each
    holding its request's connection: one read per caller would each want a
    second connection from the same pool."""
    reads = 0

    async def _slow_read(usage_type: str, period_key: str) -> int:
        nonlocal reads
        reads += 1
        await asyncio.sleep(0.01)
        return 4

    monkeypatch.setattr(quota_module, "_load_global_baseline", _REAL_LOAD_GLOBAL_BASELINE)
    monkeypatch.setattr(quota_module, "_read_global_baseline", _slow_read)

    counts = await asyncio.gather(
        *(quota_module._load_global_baseline("agentic", MONTH) for _ in range(10))
    )

    assert counts == [4] * 10
    assert reads == 1
    assert await quota_module._load_global_baseline("agentic", MONTH) == 4
    assert reads == 2, "nothing is cached past the read"


async def test_the_platform_wide_gauge_of_a_user_reads_a_lost_counter_back(redis, monkeypatch):
    """The user's gauge under the license pool shows what the gate enforces."""

    async def _config(db, redis_, user_id, quota_type):
        if quota_type == "agentic":
            return {"default": 50, "period": "monthly", "global": True}
        return None

    monkeypatch.setattr(quota_module, "resolve_quota_config", _config)
    _saved(monkeypatch, {("agentic", MONTH): 12, ("sandbox", MONTH): 3})

    entries = await get_user_quota_usage(object(), uuid.uuid4())

    (pool,) = [entry for entry in entries if entry.get("scope") == "global"]
    assert (pool["period"], pool["count"]) == (MONTH, 15)


# ---------------------------------------------------------------------------
# A charge counts on from the saved mark
# ---------------------------------------------------------------------------


async def test_a_charge_counts_on_from_the_saved_mark(redis, monkeypatch):
    _saved(monkeypatch, {("agentic", MONTH): 4})
    user = uuid.uuid4()

    await increment_agentic_usage(user)

    assert redis.store[_global_usage_key("agentic", MONTH)] == 5
    assert redis.store[_usage_key(user, "agentic", None, MONTH)] == 1


async def test_charges_racing_on_a_lost_counter_seed_it_once(redis, monkeypatch):
    saved = _saved(monkeypatch, {("agentic", MONTH): 4})

    await asyncio.gather(*(increment_agentic_usage(uuid.uuid4()) for _ in range(10)))

    assert redis.store[_global_usage_key("agentic", MONTH)] == 14
    assert len(saved.reads) >= 1


async def test_every_platform_wide_period_of_a_charge_is_put_back(redis, monkeypatch):
    """XTM One charges every license period at once; each counts on from its
    own saved mark."""
    monkeypatch.setattr(config.settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(
        quota_module,
        "get_xtm_agentic_ceilings",
        AsyncMock(return_value={"monthly": 100, "yearly": 1000}),
    )
    monkeypatch.setattr(quota_module, "resolve_quota_config", AsyncMock(return_value=None))
    _saved(monkeypatch, {("agentic", MONTH): 4, ("agentic", YEAR): 90})

    await increment_agentic_usage(uuid.uuid4(), db=object())

    assert redis.store[_global_usage_key("agentic", MONTH)] == 5
    assert redis.store[_global_usage_key("agentic", YEAR)] == 91


async def test_sandbox_and_llm_proxy_charges_count_on_from_their_saved_marks(redis, monkeypatch):
    _saved(monkeypatch, {("sandbox", MONTH): 3, ("llm_proxy", MONTH): 8})

    await increment_sandbox_usage(None)
    await increment_llm_proxy_usage(uuid.uuid4(), None)

    assert redis.store[_global_usage_key("sandbox", MONTH)] == 4
    assert redis.store[_global_usage_key("llm_proxy", MONTH)] == 9


class _FailoverRedis(_Redis):
    """Loses *key* right after the first command that reaches it: a failover
    to a cold replica in the middle of a charge."""

    def __init__(self, key: str, store: dict):
        super().__init__(store)
        self._pending: str | None = key

    def _touched(self, keys) -> None:
        if self._pending in keys:
            self.store.pop(self._pending, None)
            self._pending = None

    async def get(self, key):
        value = await super().get(key)
        self._touched({key})
        return value

    async def incr(self, key):
        value = await super().incr(key)
        self._touched({key})
        return value

    async def eval(self, script, numkeys, *keys_and_args):
        value = await super().eval(script, numkeys, *keys_and_args)
        self._touched(set(keys_and_args[:numkeys]))
        return value


async def test_a_counter_lost_in_the_middle_of_a_charge_is_not_restarted_at_one(monkeypatch):
    """Read and incremented by two commands, a counter lost between them came
    back at 1, and the saved mark was never read again for the period."""
    key = _global_usage_key("agentic", MONTH)
    redis = _FailoverRedis(key, {key: 4})
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))
    _saved(monkeypatch, {("agentic", MONTH): 4})

    await increment_agentic_usage(uuid.uuid4())

    assert await quota_module._get_global_count(redis, "agentic", MONTH) >= 4


async def test_a_refund_after_a_reseed_takes_back_its_charge(redis, monkeypatch):
    _saved(monkeypatch, {("agentic", MONTH): 4})

    await increment_agentic_usage(None)
    await refund_agentic_usage(None)

    assert redis.store[_global_usage_key("agentic", MONTH)] == 4


async def test_a_refund_never_puts_a_lost_counter_back(redis, monkeypatch):
    """Redis lost the counter after the charge, which most likely went with
    it: a refund taking one off the saved mark would count less than spent."""
    _saved(monkeypatch, {("agentic", MONTH): 4})

    await refund_agentic_usage(uuid.uuid4())

    assert _global_usage_key("agentic", MONTH) not in redis.store
    assert await quota_module._get_global_count(redis, "agentic", MONTH) == 4


# ---------------------------------------------------------------------------
# A read-back that fails
# ---------------------------------------------------------------------------


async def test_a_charge_whose_read_back_fails_creates_no_counter(redis, monkeypatch):
    """Created at 1, the counter would never be read back for the rest of the
    period."""
    _saved(monkeypatch, fail=True)

    with pytest.raises(ConnectionError):
        await increment_agentic_usage(uuid.uuid4())

    assert _global_usage_key("agentic", MONTH) not in redis.store


@pytest.mark.parametrize("charge", ["agentic", "sandbox", "llm_proxy"])
async def test_a_charge_whose_read_back_fails_counts_nothing(redis, monkeypatch, charge):
    """The platform-wide counters are read back before anything is counted,
    the user's own counters included."""
    _saved(monkeypatch, fail=True)
    user = uuid.uuid4()

    if charge == "sandbox":
        await increment_sandbox_usage(user)
    else:
        with pytest.raises(ConnectionError):
            if charge == "agentic":
                await increment_agentic_usage(user)
            else:
                await increment_llm_proxy_usage(user, "gpt")

    assert [key for key in redis.store if key.startswith("quota:usage:")] == []


async def test_the_periods_of_one_charge_count_together_or_not_at_all(redis, monkeypatch):
    """A lost yearly counter that cannot be read back leaves the monthly one,
    which Redis held, uncounted too."""
    monkeypatch.setattr(config.settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(
        quota_module,
        "get_xtm_agentic_ceilings",
        AsyncMock(return_value={"monthly": 100, "yearly": 1000}),
    )
    monkeypatch.setattr(quota_module, "resolve_quota_config", AsyncMock(return_value=None))
    _saved(monkeypatch, fail=True)
    redis.store[_global_usage_key("agentic", MONTH)] = 4

    with pytest.raises(ConnectionError):
        await increment_agentic_usage(uuid.uuid4(), db=object())

    assert redis.store == {_global_usage_key("agentic", MONTH): 4}


async def test_xtm_one_refuses_a_turn_whose_ceiling_cannot_be_read_back(redis, monkeypatch):
    _xtm_one(monkeypatch, {"monthly": 5})
    monkeypatch.setattr(config.settings, "quota_fail_mode", "open")
    _saved(monkeypatch, fail=True)

    out: dict = {}
    refusal = await enforce_agentic_quota_ex(object(), str(uuid.uuid4()), quota_out=out)

    assert refusal == (QUOTA_UNAVAILABLE_MESSAGE, False)
    assert out["reason"] == "quota_unavailable"
    assert not [key for key in redis.store if key.startswith("quota:usage:")]
