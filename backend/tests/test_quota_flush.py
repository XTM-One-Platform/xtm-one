"""Tests for flush_quota_usage() retry / lock-contention hardening.

Covers:
  * _is_retryable_flush_error() classification of 55P03 / 57014 vs. other errors.
  * flush_quota_usage() retries on transient lock errors with jittered exponential back-off.
  * flush_quota_usage() raises immediately on non-retryable errors.
  * flush_quota_usage() re-raises after all retry attempts are exhausted.
  * SELECT FOR UPDATE NOWAIT is used so lock contention is surfaced immediately.
  * flush_quota_usage() takes a cluster-wide pg_try_advisory_xact_lock and skips
    the cycle when another replica already holds it.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import quota as quota_module
from app.services.quota import (
    _FLUSH_MAX_ATTEMPTS,
    _FLUSH_RETRY_BASE_DELAY,
    _FLUSH_RETRY_JITTER_MAX_FACTOR,
    _FLUSH_RETRY_JITTER_MIN_FACTOR,
    _FLUSH_RETRY_MAX_DELAY,
    _compute_flush_retry_delay,
    _is_retryable_flush_error,
)
from tests.quota_redis_scripts import RedisHashes, run_quota_script

# ---------------------------------------------------------------------------
# _compute_flush_retry_delay – jittered backoff bounds / cap
# ---------------------------------------------------------------------------


def test_compute_flush_retry_delay_bounds_and_cap():
    """The jittered backoff for every attempt stays within
    ``[base * MIN_FACTOR, base * MAX_FACTOR]`` and never exceeds
    ``_FLUSH_RETRY_MAX_DELAY`` — including the capped attempts where the
    exponential base (and ``base * MAX_FACTOR``) would otherwise exceed the
    cap. Sampled repeatedly since the delay is randomised.
    """
    eps = 1e-9
    # Derive the per-attempt exponential base from the constants (don't
    # hard-code) so the test asserts the formula + cap, not specific values.
    for attempt in range(1, _FLUSH_MAX_ATTEMPTS + 1):
        base = min(
            _FLUSH_RETRY_BASE_DELAY * (2 ** (attempt - 1)),
            _FLUSH_RETRY_MAX_DELAY,
        )
        for _ in range(50):
            delay = _compute_flush_retry_delay(attempt)
            assert delay >= base * _FLUSH_RETRY_JITTER_MIN_FACTOR - eps
            assert delay <= base * _FLUSH_RETRY_JITTER_MAX_FACTOR + eps
            assert delay <= _FLUSH_RETRY_MAX_DELAY + eps


# ---------------------------------------------------------------------------
# _is_retryable_flush_error – pure function tests
# ---------------------------------------------------------------------------


class _FakePGError(Exception):
    """Minimal stand-in for asyncpg / SQLAlchemy driver exception that carries a SQLSTATE."""

    def __init__(self, message: str, sqlstate: str | None = None):
        super().__init__(message)
        self.sqlstate = sqlstate
        self.pgcode = sqlstate


def test_is_retryable_flush_error_returns_true_for_55p03_sqlstate():
    exc = _FakePGError("lock not available", sqlstate="55P03")
    assert _is_retryable_flush_error(exc) is True


def test_is_retryable_flush_error_returns_true_for_57014_sqlstate():
    exc = _FakePGError("statement timeout", sqlstate="57014")
    assert _is_retryable_flush_error(exc) is True


def test_is_retryable_flush_error_returns_true_for_55p03_via_cause_chain():
    """SQLSTATE carried on ``__cause__`` is still detected."""
    wrapper = RuntimeError("wrapped")
    wrapper.__cause__ = _FakePGError("lock not available", sqlstate="55P03")
    assert _is_retryable_flush_error(wrapper) is True


def test_is_retryable_flush_error_returns_true_for_statement_timeout_string():
    exc = RuntimeError("canceling statement due to statement timeout")
    assert _is_retryable_flush_error(exc) is True


def test_is_retryable_flush_error_returns_true_for_lock_timeout_string():
    exc = RuntimeError("canceling statement due to lock timeout")
    assert _is_retryable_flush_error(exc) is True


def test_is_retryable_flush_error_returns_true_for_55p03_lowercase_string():
    exc = RuntimeError("sqlstate 55p03 triggered")
    assert _is_retryable_flush_error(exc) is True


def test_is_retryable_flush_error_returns_false_for_unrelated_error():
    exc = ValueError("unexpected column type")
    assert _is_retryable_flush_error(exc) is False


def test_is_retryable_flush_error_returns_false_for_integrity_error():
    exc = Exception("IntegrityError: unique constraint violated")
    assert _is_retryable_flush_error(exc) is False


def test_is_retryable_flush_error_handles_cycle_in_exception_chain():
    """Exception chains with a cycle must not cause infinite loops."""
    exc = RuntimeError("root")
    exc.__context__ = exc  # self-referential cycle
    # Must not hang and must return False (no retryable state detected)
    assert _is_retryable_flush_error(exc) is False


# ---------------------------------------------------------------------------
# flush_quota_usage() retry behavior – helpers
# ---------------------------------------------------------------------------


def _make_fake_redis(user_id: uuid.UUID) -> MagicMock:
    """Return a minimal Redis fake that yields one quota usage key."""
    usage_key = f"quota:usage:{user_id}:llm_proxy:_total:2026-06"
    encoded_count = b"42"

    async def _scan_iter(match, count):
        yield usage_key

    pipeline = MagicMock()
    pipeline.get = MagicMock()
    pipeline.execute = AsyncMock(return_value=[encoded_count])

    redis = MagicMock()
    redis.scan_iter = _scan_iter
    redis.pipeline = MagicMock(return_value=pipeline)
    redis.hgetall = AsyncMock(return_value={})
    redis.exists = AsyncMock(return_value=0)
    return redis


class _FakeResult:
    """Minimal stand-in for SQLAlchemy ``Result``."""

    def __init__(self, rows: list | None = None):
        self._rows = rows if rows is not None else []

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0] if self._rows else None

    def scalar_one(self):
        if not self._rows:
            raise Exception("No row found")
        return self._rows[0]


class _SuccessSession:
    """DB session fake that reports: lock acquired, no duplicates, user exists,
    no existing quota row.

    On the first execute() call (SELECT pg_try_advisory_xact_lock) it returns a
    scalar reflecting ``lock_acquired``.  On the second (SET LOCAL
    statement_timeout) it returns a dummy result.  On the third (dup_query) it
    returns an empty list.  On the fourth (User.id pre-fetch) it returns the
    provided user_id.  On all subsequent calls (SELECT QuotaUsage FOR UPDATE
    NOWAIT) it returns None — meaning the row does not exist, so the upsert loop
    INSERTs instead.

    When ``lock_acquired`` is False the flush short-circuits after the first
    call, so calls 2+ never happen.
    """

    def __init__(self, valid_user_id: uuid.UUID, lock_acquired: bool = True):
        self._valid_user_id = valid_user_id
        self._lock_acquired = lock_acquired
        self._call_idx = 0
        self.added: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, stmt, params=None):
        self._call_idx += 1
        if self._call_idx == 1:
            # SELECT pg_try_advisory_xact_lock(...) — cluster-wide flush lock
            return _FakeResult([self._lock_acquired])
        elif self._call_idx == 2:
            # SET LOCAL statement_timeout
            return _FakeResult()
        elif self._call_idx == 3:
            # dup_query — no duplicates
            return _FakeResult([])
        elif self._call_idx == 4:
            # select User.id — return the valid user
            return _FakeResult([(self._valid_user_id,)])
        else:
            # SELECT QuotaUsage FOR UPDATE NOWAIT — no existing row
            return _FakeResult()

    async def commit(self):
        pass

    def add(self, obj):
        self.added.append(obj)


class _LockErrorSession:
    """DB session fake that always raises a retryable lock-timeout error."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, stmt, params=None):
        raise RuntimeError("canceling statement due to statement timeout")

    async def commit(self):
        pass

    def add(self, obj):
        pass


# ---------------------------------------------------------------------------
# flush_quota_usage() tests
# ---------------------------------------------------------------------------


async def test_flush_quota_usage_retries_on_lock_contention_and_succeeds(monkeypatch):
    """First attempt raises 55P03; second attempt succeeds — two sessions used."""
    user_id = uuid.uuid4()

    sessions_used: list = []
    success_session = _SuccessSession(valid_user_id=user_id)

    def _session_factory():
        if not sessions_used:
            sessions_used.append("error")
            return _LockErrorSession()
        sessions_used.append("success")
        return success_session

    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=_make_fake_redis(user_id)))

    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    with (
        patch("app.core.database.async_session", side_effect=_session_factory),
        patch("app.services.quota.asyncio.sleep", side_effect=_fake_sleep),
    ):
        result = await quota_module.flush_quota_usage()

    assert result == 1  # one counter flushed on the successful retry
    assert len(sessions_used) == 2, f"Expected exactly 2 sessions, got {sessions_used}"
    assert sessions_used[0] == "error"
    assert sessions_used[1] == "success"
    # Back-off must have been applied between attempts
    assert len(sleep_calls) == 1
    assert sleep_calls[0] >= _FLUSH_RETRY_BASE_DELAY * _FLUSH_RETRY_JITTER_MIN_FACTOR
    assert sleep_calls[0] <= _FLUSH_RETRY_BASE_DELAY * _FLUSH_RETRY_JITTER_MAX_FACTOR


async def test_flush_quota_usage_raises_immediately_on_non_retryable_error(monkeypatch):
    """A non-lock error must propagate immediately without any retry."""
    user_id = uuid.uuid4()

    call_count = 0

    class _BrokenSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, stmt, params=None):
            nonlocal call_count
            call_count += 1
            raise ValueError("unexpected schema mismatch")

        async def commit(self):
            pass

    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=_make_fake_redis(user_id)))
    sleep_mock = AsyncMock()

    with (
        patch("app.core.database.async_session", return_value=_BrokenSession()),
        patch("app.services.quota.asyncio.sleep", sleep_mock),
        pytest.raises(ValueError, match="unexpected schema mismatch"),
    ):
        await quota_module.flush_quota_usage()

    # No sleep — non-retryable error must not trigger back-off
    sleep_mock.assert_not_awaited()
    # Called only once — no retry
    assert call_count == 1


async def test_flush_quota_usage_raises_after_all_attempts_exhausted(monkeypatch, caplog):
    """All _FLUSH_MAX_ATTEMPTS lock-contention errors → final exception raised + error logged."""
    user_id = uuid.uuid4()

    attempt_count = 0

    def _always_lock_session():
        nonlocal attempt_count
        attempt_count += 1
        return _LockErrorSession()

    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=_make_fake_redis(user_id)))
    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    with (
        patch("app.core.database.async_session", side_effect=_always_lock_session),
        patch("app.services.quota.asyncio.sleep", side_effect=_fake_sleep),
        caplog.at_level("ERROR"),
        pytest.raises(RuntimeError, match="canceling statement due to statement timeout"),
    ):
        await quota_module.flush_quota_usage()

    # Exactly _FLUSH_MAX_ATTEMPTS sessions were opened
    assert attempt_count == _FLUSH_MAX_ATTEMPTS
    # Back-off between each pair of attempts (one less than total attempts)
    assert len(sleep_calls) == _FLUSH_MAX_ATTEMPTS - 1
    # Jittered exponential back-off, derived from the constants (not
    # hard-coded) so the test tracks the formula + cap if they change.
    expected_bases = [
        min(_FLUSH_RETRY_BASE_DELAY * (2**i), _FLUSH_RETRY_MAX_DELAY)
        for i in range(_FLUSH_MAX_ATTEMPTS - 1)
    ]
    for idx, base in enumerate(expected_bases[: len(sleep_calls)]):
        assert sleep_calls[idx] >= base * _FLUSH_RETRY_JITTER_MIN_FACTOR
        assert sleep_calls[idx] <= min(base * _FLUSH_RETRY_JITTER_MAX_FACTOR, _FLUSH_RETRY_MAX_DELAY)
    # Error must be logged clearly
    assert "flush_quota_usage" in caplog.text
    assert "still failing" in caplog.text


async def test_flush_quota_usage_returns_zero_when_no_redis_keys(monkeypatch):
    """No Redis keys → no DB work, returns 0."""

    async def _empty_scan_iter(match, count):
        # Empty async generator: the loop never iterates so nothing is
        # yielded, but the lexical ``yield`` still makes this a generator -
        # without a bare unreachable statement after an early return.
        for _ in ():
            yield _

    redis = MagicMock()
    redis.scan_iter = _empty_scan_iter
    redis.exists = AsyncMock(return_value=0)

    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))

    # async_session must NOT be called when there are no keys
    session_factory = MagicMock()
    with patch("app.core.database.async_session", session_factory):
        result = await quota_module.flush_quota_usage()

    assert result == 0
    session_factory.assert_not_called()


async def test_flush_quota_usage_does_not_count_stale_keys(monkeypatch):
    """A Redis key whose user no longer exists in the DB is skipped, and must
    NOT be counted in the flushed total — returning len(rows) would over-count
    and contradict the documented "number of counters flushed" return value.
    """
    redis_user = uuid.uuid4()
    other_user = uuid.uuid4()  # the only user the DB pre-fetch reports as valid
    # _SuccessSession reports ``other_user`` as the valid id, so the Redis row
    # for ``redis_user`` falls into the stale-key skip branch.
    success_session = _SuccessSession(valid_user_id=other_user)

    monkeypatch.setattr(
        quota_module,
        "redis_client",
        AsyncMock(return_value=_make_fake_redis(redis_user)),
    )

    with patch("app.core.database.async_session", return_value=success_session):
        result = await quota_module.flush_quota_usage()

    assert result == 0, "stale Redis key must not be counted as flushed"


async def test_flush_quota_usage_flushes_sandbox_usage_type(monkeypatch):
    """A `usage_type="sandbox"` Redis key (spec 002-sandbox-quota-usage-tracking)
    flushes to a `quota_usage` row exactly like any other usage type — this
    function derives `usage_type` generically from the Redis key itself, not
    from a hardcoded list, so no code change was needed here for the new
    usage type (research.md §1)."""
    user_id = uuid.uuid4()
    usage_key = f"quota:usage:{user_id}:sandbox:_total:2026-06"

    async def _scan_iter(match, count):
        yield usage_key

    pipeline = MagicMock()
    pipeline.get = MagicMock()
    pipeline.execute = AsyncMock(return_value=[b"3"])

    redis = MagicMock()
    redis.scan_iter = _scan_iter
    redis.pipeline = MagicMock(return_value=pipeline)
    redis.hgetall = AsyncMock(return_value={})
    redis.exists = AsyncMock(return_value=0)

    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))

    session = _SuccessSession(valid_user_id=user_id)
    with patch("app.core.database.async_session", return_value=session):
        result = await quota_module.flush_quota_usage()

    assert result == 1
    assert len(session.added) == 1
    flushed = session.added[0]
    assert flushed.usage_type == "sandbox"
    assert flushed.user_id == user_id
    assert flushed.count == 3
    assert flushed.model is None
    assert flushed.agent_id is None


async def test_flush_quota_usage_select_uses_for_update_nowait(monkeypatch):
    """The SELECT in the upsert loop must carry FOR UPDATE NOWAIT.

    Scans the SQLAlchemy statements passed to execute() for the upsert
    SELECT (identified by the presence of a FOR UPDATE clause) and inspects
    its ``_for_update_arg`` metadata to confirm ``nowait=True`` is set.
    Selecting by clause presence rather than by call index keeps the test
    robust to reordering of the surrounding statements.
    """
    user_id = uuid.uuid4()

    captured_stmts: list = []
    success_session = _SuccessSession(valid_user_id=user_id)
    original_execute = success_session.execute

    async def _capturing_execute(stmt, params=None):
        captured_stmts.append(stmt)
        return await original_execute(stmt, params)

    success_session.execute = _capturing_execute

    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=_make_fake_redis(user_id)))

    with patch("app.core.database.async_session", return_value=success_session):
        await quota_module.flush_quota_usage()

    # Identify the upsert SELECT by the presence of its FOR UPDATE clause
    # rather than by call index, so reordering the surrounding statements
    # doesn't break this assertion. SQLAlchemy 2.0 stores the clause as a
    # ForUpdateArg on the Select's ``_for_update_arg`` (None when not set).
    for_update_args = [
        fu
        for fu in (getattr(stmt, "_for_update_arg", None) for stmt in captured_stmts)
        if fu is not None
    ]
    assert for_update_args, "the upsert SELECT must carry a FOR UPDATE clause"
    assert all(getattr(fu, "nowait", False) is True for fu in for_update_args), (
        "the upsert SELECT must use NOWAIT to fail fast on locked rows"
    )


async def test_flush_quota_usage_acquires_advisory_xact_lock(monkeypatch):
    """The flush must take a transaction-scoped advisory lock before doing any
    DB work, so concurrent replicas serialize instead of colliding on the
    per-row FOR UPDATE NOWAIT upserts.
    """
    user_id = uuid.uuid4()

    captured_stmts: list = []
    success_session = _SuccessSession(valid_user_id=user_id)
    original_execute = success_session.execute

    async def _capturing_execute(stmt, params=None):
        captured_stmts.append(str(stmt))
        return await original_execute(stmt, params)

    success_session.execute = _capturing_execute

    monkeypatch.setattr(
        quota_module, "redis_client", AsyncMock(return_value=_make_fake_redis(user_id))
    )

    with patch("app.core.database.async_session", return_value=success_session):
        await quota_module.flush_quota_usage()

    # A transaction-scoped advisory lock (auto-released on COMMIT/ROLLBACK so it
    # can never leak into the pool) must be the first statement of the flush.
    assert any("pg_try_advisory_xact_lock" in s for s in captured_stmts), (
        "the flush must acquire a pg_try_advisory_xact_lock"
    )
    assert "pg_try_advisory_xact_lock" in captured_stmts[0], (
        "the advisory lock must be acquired before any other DB work"
    )


async def test_flush_quota_usage_skips_when_lock_held_by_another_replica(monkeypatch):
    """When pg_try_advisory_xact_lock returns False (another replica is already
    flushing), the cycle is skipped: no dedup, no upsert, returns 0 — and no
    error is raised so the autonomous tick stays clean.
    """
    user_id = uuid.uuid4()
    skip_session = _SuccessSession(valid_user_id=user_id, lock_acquired=False)

    monkeypatch.setattr(
        quota_module, "redis_client", AsyncMock(return_value=_make_fake_redis(user_id))
    )

    with patch("app.core.database.async_session", return_value=skip_session):
        result = await quota_module.flush_quota_usage()

    assert result == 0
    # Only the advisory-lock probe ran; the flush short-circuited before the
    # SET LOCAL / dedup / upsert statements.
    assert skip_session._call_idx == 1
    assert skip_session.added == [], "no upsert may run when the lock is not held"


# ---------------------------------------------------------------------------
# What the flush writes is read under its lock
# ---------------------------------------------------------------------------


class _DictRedis(RedisHashes):
    """A counter store the test can change between the scan and the lock."""

    def __init__(self, store: dict[str, bytes]):
        self.store = store
        self.hashes: dict[str, dict] = {}

    async def scan_iter(self, match, count):
        for key in list(self.store):
            if key.startswith(match.rstrip("*")):
                yield key

    def pipeline(self):
        return _DictPipeline(self.store)

    async def delete(self, *keys):
        for key in keys:
            self.store.pop(key, None)
            self.hashes.pop(key, None)
        return len(keys)

    async def eval(self, script, numkeys, *keys_and_args):
        return run_quota_script(self.store, self.hashes, script, keys_and_args)


class _DictPipeline:
    def __init__(self, store: dict[str, bytes]):
        self._store = store
        self._keys: list[str] = []

    def get(self, key):
        self._keys.append(key)

    async def execute(self):
        return [self._store.get(key) for key in self._keys]


class _SessionChangingRedisAtTheLock(_SuccessSession):
    """Runs *change* when the flush asks for its advisory lock - what a quota
    reset or a recharge holding that lock does just before it is released."""

    def __init__(self, valid_user_id: uuid.UUID, change):
        super().__init__(valid_user_id)
        self._change = change

    async def execute(self, stmt, params=None):
        if self._call_idx == 0:
            self._change()
        return await super().execute(stmt, params)


async def test_a_counter_deleted_before_the_lock_is_not_written_back(monkeypatch):
    """A reset (or a recharge) deletes the counter after the flush scanned it
    and before the flush got the lock: the flush must not upsert the value it
    saw before, or the deleted count comes back."""
    user_id = uuid.uuid4()
    key = f"quota:usage:{user_id}:agentic:_total:2026-09:_all"
    redis = _DictRedis({key: b"3"})
    session = _SessionChangingRedisAtTheLock(user_id, lambda: redis.store.pop(key))
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))

    with patch("app.core.database.async_session", return_value=session):
        result = await quota_module.flush_quota_usage()

    assert result == 0
    assert session.added == []


async def test_an_increment_before_the_lock_is_flushed_not_lost(monkeypatch):
    """The value written is the one under the lock, so an increment landing
    between the scan and the lock is part of it."""
    user_id = uuid.uuid4()
    key = f"quota:usage:{user_id}:agentic:_total:2026-09:_all"
    redis = _DictRedis({key: b"3"})
    session = _SessionChangingRedisAtTheLock(
        user_id, lambda: redis.store.update({key: b"4"})
    )
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))

    with patch("app.core.database.async_session", return_value=session):
        result = await quota_module.flush_quota_usage()

    assert result == 1
    (row,) = session.added
    assert row.count == 4


# ---------------------------------------------------------------------------
# The flush stands aside for a waiting reset or recharge
# ---------------------------------------------------------------------------


def _months_of_usage(user_id: uuid.UUID, months: int) -> dict[str, bytes]:
    return {
        f"quota:usage:{user_id}:agentic:_total:2026-{month:02d}:_all": b"3"
        for month in range(1, months + 1)
    }


class _CommitCountingSession(_SuccessSession):
    def __init__(self, valid_user_id: uuid.UUID, on_row=None):
        super().__init__(valid_user_id)
        self.commits = 0
        self._on_row = on_row

    async def execute(self, stmt, params=None):
        if self._on_row and getattr(stmt, "_for_update_arg", None) is not None:
            self._on_row()
        return await super().execute(stmt, params)

    async def commit(self):
        self.commits += 1


async def test_a_flush_stands_aside_for_a_clear_waiting_for_its_lock(monkeypatch):
    """A quota reset or a recharge is waiting for the flush lock: a flush
    that gets it first gives it straight back, and writes nothing."""
    user_id = uuid.uuid4()
    redis = _DictRedis(_months_of_usage(user_id, 1))
    redis.store[quota_module._FLUSH_YIELD_KEY] = b"1"
    session = _CommitCountingSession(user_id)
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))

    with patch("app.core.database.async_session", return_value=session):
        assert await quota_module.flush_quota_usage() == 0

    assert session.added == [] and session.commits == 0
    assert session._call_idx == 1, "only the lock was taken"


async def test_a_long_flush_stands_aside_for_a_clear_that_starts_waiting(monkeypatch):
    """The clear starts waiting while the flush is writing: the flush rolls
    back within a few rows instead of keeping the lock until it is done, so
    the clear gets the lock instead of going ahead without it and seeing
    counters it deletes written back. The counters stay in Redis for
    the next cycle."""
    monkeypatch.setattr(quota_module, "_FLUSH_YIELD_CHECK_ROWS", 2)
    user_id = uuid.uuid4()
    redis = _DictRedis(_months_of_usage(user_id, 6))
    session = _CommitCountingSession(
        user_id,
        on_row=lambda: redis.store.setdefault(quota_module._FLUSH_YIELD_KEY, b"1"),
    )
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))

    with patch("app.core.database.async_session", return_value=session):
        assert await quota_module.flush_quota_usage() == 0

    assert session.commits == 0, "the flush committed after a clear asked for the lock"
    assert len(session.added) == 2, "it stood aside at its next check, two rows in"
    assert sum(key.startswith("quota:usage:") for key in redis.store) == 6


class _SettleSession:
    """The flush lock, then how PostgreSQL answers for a stash's transaction."""

    def __init__(self, status):
        self._status = status
        self.statements: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.statements.append(sql)
        value = self._status if "pg_xact_status" in sql else True
        return _FakeResult([value])

    async def commit(self):
        pass


@pytest.mark.parametrize(("status", "restored"), [("aborted", True), ("committed", False)])
async def test_the_flush_settles_a_stash_a_dead_clear_left_behind(
    monkeypatch, status, restored
):
    """A reset stashed its counters and died before it could put them back or
    drop them. The next flush settles the stash from how its transaction
    ended - even with no counter of its own to write."""
    user_id = uuid.uuid4()
    key = f"quota:usage:{user_id}:agentic:_total:2026-09:_all"
    redis = _DictRedis({})
    redis.hashes[quota_module._CLEAR_STASH_INDEX] = {"dead": "4242"}
    redis.hashes[quota_module._clear_stash_key("dead")] = {key: "3:-1:0"}
    session = _SettleSession(status)
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))

    with patch("app.core.database.async_session", return_value=session):
        assert await quota_module.flush_quota_usage() == 0

    assert ("pg_xact_status" in session.statements[1]) is True
    assert (redis.store.get(key) == 3) is restored
    assert redis.hashes == {}
