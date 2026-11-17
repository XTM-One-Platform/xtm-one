"""Tests for agentic execution accounting (``services/agentic_executions.py``).

Covers the two halves of the contract:
  * the hot path (:func:`record_agentic_execution`) does a single INCR + EXPIRE
    and can NEVER raise into the execution it is accounting for;
  * the flush folds the Redis day counters into ``agentic_execution_daily`` with
    an idempotent, monotonic, lock-light upsert, skipping malformed keys and
    deduping so PostgreSQL can never see the same conflict row twice.
"""

from __future__ import annotations

import logging
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.dialects import postgresql

from app.services import agentic_executions as agex


def _redis_mock(keys: list[str], values: list[str | None]):
    """Redis double supporting ``scan_iter`` + a GET pipeline."""

    async def _scan_iter(*_args, **_kwargs):
        for key in keys:
            yield key

    pipe = MagicMock()
    pipe.get = MagicMock()
    pipe.execute = AsyncMock(return_value=values)

    redis = MagicMock()
    redis.scan_iter = _scan_iter
    redis.pipeline = MagicMock(return_value=pipe)
    redis.incr = AsyncMock(return_value=1)
    redis.expire = AsyncMock(return_value=True)
    return redis


class _SessionMock:
    """``async with async_session() as db`` double capturing executed statements."""

    def __init__(self):
        self.statements = []
        self.committed = False

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, stmt):
        self.statements.append(stmt)
        return MagicMock()

    async def commit(self):
        self.committed = True


def _row_counts(session: _SessionMock) -> dict[tuple[date, str], int]:
    """Extract ``{(day, source): count}`` from the captured INSERT statement.

    Asserting on the bound parameters (rather than the SQL text) is what pins
    the aggregation semantics: how many rows the flush emits, and with which
    counts.
    """
    params = session.statements[0].compile(dialect=postgresql.dialect()).params
    indices = sorted(
        int(key[len("day_m"):]) for key in params if key.startswith("day_m")
    )
    suffixes = [f"_m{i}" for i in indices] if indices else [""]
    return {
        (params[f"day{sfx}"], params[f"source{sfx}"]): params[f"count{sfx}"]
        for sfx in suffixes
    }


# ---------------------------------------------------------------------------
# Key format
# ---------------------------------------------------------------------------


def test_day_key_format():
    key = agex._day_key(agex.SOURCE_AGENT_CHAT, date(2026, 7, 30))
    assert key == "agentic:exec:2026-07-30:agent_chat"


def test_day_key_normalizes_oversized_source():
    """A source longer than the DB column must be clamped before it becomes part
    of a key, so a counter can never live under a key the table cannot store.
    """
    long_source = "x" * (agex.SOURCE_MAX_LENGTH + 20)
    key = agex._day_key(long_source, date(2026, 7, 30))
    assert key == f"agentic:exec:2026-07-30:{'x' * agex.SOURCE_MAX_LENGTH}"


async def test_record_warns_on_unregistered_source_but_still_counts(caplog):
    """An entry point wired up without registering its constant must surface in
    the logs — but the execution is still counted, because dropping it would
    silently lose real agentic work.
    """
    redis = _redis_mock([], [])
    with (
        patch.object(agex, "redis_client", AsyncMock(return_value=redis)),
        caplog.at_level(logging.WARNING, logger=agex.__name__),
    ):
        await agex.record_agentic_execution("brand_new_entry_point")

    redis.incr.assert_awaited_once()
    assert "unregistered source" in caplog.text


async def test_record_does_not_warn_for_registered_sources(caplog):
    redis = _redis_mock([], [])
    with (
        patch.object(agex, "redis_client", AsyncMock(return_value=redis)),
        caplog.at_level(logging.WARNING, logger=agex.__name__),
    ):
        for source in agex.KNOWN_SOURCES:
            await agex.record_agentic_execution(source)

    assert caplog.text == ""


# ---------------------------------------------------------------------------
# record_agentic_execution – hot path
# ---------------------------------------------------------------------------


async def test_record_increments_and_sets_ttl():
    redis = _redis_mock([], [])
    with patch.object(agex, "redis_client", AsyncMock(return_value=redis)):
        await agex.record_agentic_execution(agex.SOURCE_ASSIGNMENT)

    redis.incr.assert_awaited_once()
    key = redis.incr.await_args.args[0]
    assert key.startswith(agex._KEY_PREFIX)
    assert key.endswith(f":{agex.SOURCE_ASSIGNMENT}")
    redis.expire.assert_awaited_once_with(key, agex._KEY_TTL)


async def test_record_never_raises_on_redis_failure():
    """Accounting must never be able to fail the execution it measures."""
    with patch.object(
        agex, "redis_client", AsyncMock(side_effect=RuntimeError("redis down"))
    ):
        # Must return normally, not raise.
        assert await agex.record_agentic_execution(agex.SOURCE_AGENT_CHAT) is None


# ---------------------------------------------------------------------------
# flush_agentic_executions
# ---------------------------------------------------------------------------


async def test_flush_returns_zero_when_no_keys():
    redis = _redis_mock([], [])
    with patch.object(agex, "redis_client", AsyncMock(return_value=redis)):
        assert await agex.flush_agentic_executions() == 0
    redis.pipeline.assert_not_called()


async def test_flush_upserts_with_monotonic_on_conflict():
    """The flush must emit ONE idempotent, monotonic upsert: ON CONFLICT on
    (day, source) with GREATEST so a Redis restart can't walk a stored total
    backwards and a re-flush is a no-op.
    """
    keys = [
        "agentic:exec:2026-07-29:agent_chat",
        "agentic:exec:2026-07-29:assignment",
        "agentic:exec:2026-07-30:agent_chat",
    ]
    redis = _redis_mock(keys, ["12", "4", "7"])
    session = _SessionMock()

    with (
        patch.object(agex, "redis_client", AsyncMock(return_value=redis)),
        patch("app.core.database.async_session", session),
    ):
        flushed = await agex.flush_agentic_executions()

    assert flushed == 3
    assert session.committed is True
    assert len(session.statements) == 1

    sql = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": False},
        )
    ).lower()
    assert "on conflict" in sql
    assert "greatest" in sql
    assert "agentic_execution_daily" in sql


async def test_flush_skips_malformed_and_empty_keys():
    """Junk keys and non-positive counters are dropped instead of aborting the
    whole flush (a bad key must not cost the install every other counter).
    """
    keys = [
        "agentic:exec:not-a-date:agent_chat",  # unparseable day
        "agentic:exec:2026-07-30",  # no source segment
        "agentic:exec:2026-07-30:agent_chat",  # good
        "agentic:exec:2026-07-31:assignment",  # value None -> skipped
        "agentic:exec:2026-08-01:assignment",  # value 0 -> skipped
    ]
    redis = _redis_mock(keys, ["5", "9", "3", None, "0"])
    session = _SessionMock()

    with (
        patch.object(agex, "redis_client", AsyncMock(return_value=redis)),
        patch("app.core.database.async_session", session),
    ):
        flushed = await agex.flush_agentic_executions()

    assert flushed == 1


async def test_flush_collapses_a_key_returned_twice_by_scan():
    """Redis SCAN guarantees no misses, not no duplicates. The same key coming
    back twice is ONE counter and must not be double-counted, nor produce two
    rows for the same (day, source) — PostgreSQL rejects an ON CONFLICT DO
    UPDATE that would touch the same row twice in one statement.
    """
    keys = [
        "agentic:exec:2026-07-30:agent_chat",
        "agentic:exec:2026-07-30:agent_chat",
    ]
    redis = _redis_mock(keys, ["11", "11"])
    session = _SessionMock()

    with (
        patch.object(agex, "redis_client", AsyncMock(return_value=redis)),
        patch("app.core.database.async_session", session),
    ):
        flushed = await agex.flush_agentic_executions()

    assert flushed == 1
    assert _row_counts(session) == {(date(2026, 7, 30), "agent_chat"): 11}


async def test_flush_merges_sources_that_collide_once_truncated():
    """Two sources that differ only PAST the column width must land in ONE row.

    Regression test: the dedupe key used to be built from the untruncated
    source while the inserted value was truncated, so two such sources produced
    two rows for the same (day, source) and PostgreSQL aborted the whole flush
    with "ON CONFLICT DO UPDATE command cannot affect row a second time" —
    every cycle, forever. They are distinct counters sharing one storage
    bucket, so their counts SUM.
    """
    prefix = "s" * agex.SOURCE_MAX_LENGTH
    keys = [
        f"agentic:exec:2026-07-30:{prefix}AAAA",
        f"agentic:exec:2026-07-30:{prefix}BBBB",
    ]
    redis = _redis_mock(keys, ["4", "7"])
    session = _SessionMock()

    with (
        patch.object(agex, "redis_client", AsyncMock(return_value=redis)),
        patch("app.core.database.async_session", session),
    ):
        flushed = await agex.flush_agentic_executions()

    assert flushed == 1
    assert _row_counts(session) == {(date(2026, 7, 30), prefix): 11}


async def test_refund_decrements_the_same_day_key_floored_at_zero():
    """A refund reverses exactly the counter the record incremented, through the
    shared floor-at-zero primitive — a refund landing after the UTC day rolled
    over must not push the new day's counter negative.
    """
    redis = _redis_mock([], [])
    decr = AsyncMock()
    with (
        patch.object(agex, "redis_client", AsyncMock(return_value=redis)),
        patch.object(agex, "decr_if_positive", decr),
    ):
        await agex.record_agentic_execution(agex.SOURCE_AGENT_CHAT)
        await agex.refund_agentic_execution(agex.SOURCE_AGENT_CHAT)

    recorded_key = redis.incr.await_args.args[0]
    refunded_key = decr.await_args.args[1]
    assert refunded_key == recorded_key


async def test_refund_never_raises_on_redis_failure():
    """Like the recorder, the refund must never raise into the caller."""
    with patch.object(
        agex, "redis_client", AsyncMock(side_effect=RuntimeError("redis down"))
    ):
        assert await agex.refund_agentic_execution(agex.SOURCE_AGENT_CHAT) is None
