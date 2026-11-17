# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Agentic execution accounting — how much agentic work an install performs.

An **agentic execution** is one logical request that runs the agentic loop:
a chat turn, a General Assistant turn, an assignment run, a background
sub-agent task, or a browser-extension task.  It is counted exactly once
regardless of how many agents it fans out to — nested consult / transfer
handovers belong to their parent execution, not to a new one (the same
"one logical request" unit the agentic quota meters, OBS-3).

Why this exists
---------------
Adoption analytics needs the *volume of agentic work*, and no existing
counter answers that question: ``assignment_runs`` only covers scheduled /
triggered automations (missing every chat-driven execution), and chat
messages count conversation turns (missing assignments entirely, and
double-counting a single turn's user + assistant rows).  So executions are
counted explicitly, here.

Design
------
* **Hot path**: :func:`record_agentic_execution` does a single Redis ``INCR``
  on a per-(UTC day, source) key.  It never touches the caller's DB session
  and never raises — accounting must not be able to fail an execution, and
  must not hold a row lock for the minutes an agentic loop can run.
* **Durability**: the autonomous scheduler calls
  :func:`flush_agentic_executions` every few minutes to fold the Redis
  counters into ``agentic_execution_daily``.
* **Telemetry**: ``services/telemetry.py`` reads the durable table for the
  ``xtm_one_agentic_executions_count`` (all-time cumulative) and
  ``xtm_one_agentic_executions_by_day`` (rolling window) gauges.

Counts only — no user, agent, conversation, or content identity is recorded
at any point.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.redis import decr_if_positive, redis_client
from app.core.utils import format_exception_message
from app.models.agentic_execution import SOURCE_MAX_LENGTH

logger = logging.getLogger(__name__)

# ── Sources ──────────────────────────────────────────────────────────────────
# The entry point that produced an execution.  Kept coarse and stable: these
# values are persisted, so they are a contract.  Sub-agent background tasks and
# self-fan-out workers run through ``run_agent_chat`` and are therefore counted
# as ``agent_chat``; so are public widget turns, which go through the same gate
# (``agent_chat.enforce_agentic_quota_ex``).
SOURCE_AGENT_CHAT = "agent_chat"
SOURCE_GENERAL_CHAT = "general_chat"
SOURCE_ASSIGNMENT = "assignment"
SOURCE_EXTENSION = "extension"
#: Deep Investigation chat turns. Metered like any other agentic turn
#: even though no model is called: the work is real (a bounded loop of outbound
#: fetches plus persisted state), and leaving it unmetered made the mode a way
#: around the per-user quota entirely.
SOURCE_DEEP_INVESTIGATION = "deep_investigation"
#: An administrator's "Draft with AI" of an organization profile (#9484):
#: one model call per click, metered like a chat turn.
SOURCE_ORGANIZATION_AI_DRAFT = "organization_ai_draft"

KNOWN_SOURCES: frozenset[str] = frozenset(
    {
        SOURCE_AGENT_CHAT,
        SOURCE_GENERAL_CHAT,
        SOURCE_ASSIGNMENT,
        SOURCE_EXTENSION,
        SOURCE_DEEP_INVESTIGATION,
        SOURCE_ORGANIZATION_AI_DRAFT,
    }
)

# Redis key prefix: ``agentic:exec:{YYYY-MM-DD}:{source}``.
_KEY_PREFIX = "agentic:exec:"

# Key TTL (seconds).  Comfortably longer than the telemetry by-day window so a
# day is still flushable well after it closes, while keeping the keyspace
# bounded for installs that never run the flush.
_KEY_TTL = 86_400 * 40


def _normalize_source(source: str) -> str:
    """Clamp *source* to the storage width of the ``source`` column.

    Applied before the source becomes part of any key — Redis or DB — so a
    counter can never be written under a key that the durable table cannot
    represent.  See :data:`SOURCE_MAX_LENGTH`.
    """
    return source[:SOURCE_MAX_LENGTH]


def _day_key(source: str, day: date | None = None) -> str:
    """Return the Redis counter key for *source* on *day* (default: today UTC)."""
    day = day or datetime.now(UTC).date()
    return f"{_KEY_PREFIX}{day.isoformat()}:{_normalize_source(source)}"


async def record_agentic_execution(source: str) -> None:
    """Count one top-level agentic execution from *source*.

    Best-effort and silent: a Redis hiccup must never fail (or slow down) the
    execution it is accounting for, so every error is swallowed at debug level.
    An unregistered source is still recorded — dropping it would silently lose
    executions — but it is logged as a warning, because it means a new entry
    point was wired up without adding its constant to :data:`KNOWN_SOURCES`.
    """
    if source not in KNOWN_SOURCES:
        logger.warning(
            "Recording agentic execution from unregistered source %r — add it "
            "to KNOWN_SOURCES in services/agentic_executions.py",
            source,
        )
    try:
        redis = await redis_client()
        key = _day_key(source)
        await redis.incr(key)
        await redis.expire(key, _KEY_TTL)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "Agentic execution accounting failed for source %s: %s",
            source,
            format_exception_message(exc),
        )


async def refund_agentic_execution(source: str) -> None:
    """Reverse a prior :func:`record_agentic_execution` for one logical request.

    Called when a metered request is ultimately refused *before doing any
    work* — the same refund-safe condition that reverses the agentic quota (see
    ``quota.refund_agentic_usage``), currently a ``skip_llm`` pipeline whose
    PREPARE stage declines, e.g. an extraction denied for lack of a licence.
    Without this the execution metric and the quota would drift precisely on
    licence-gated installs, where *every* refused request would still be
    reported as agentic work performed.

    Floored at zero (shared ``decr_if_positive`` primitive): a refund landing
    just after the UTC day rolled over decrements the new day's counter, which
    must not go negative.  Like every other write here it is best-effort and
    never raises into the caller.

    A refund is only reflected downstream if it lands before the next flush —
    the durable upsert is monotonic, so a same-cycle record + refund nets out,
    while a refund arriving after its record was already flushed is dropped.
    That is the same bounded approximation ``refund_agentic_usage`` accepts, and
    the window is milliseconds against a 5-minute flush cadence.
    """
    try:
        redis = await redis_client()
        await decr_if_positive(redis, _day_key(source))
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "Agentic execution refund failed for source %s: %s",
            source,
            format_exception_message(exc),
        )


async def flush_agentic_executions() -> int:
    """Fold the Redis day counters into ``agentic_execution_daily``.

    Returns the number of (day, source) counters flushed.

    The upsert is a single ``INSERT ... ON CONFLICT DO UPDATE SET count =
    GREATEST(existing, incoming)``, which makes the flush:

    * **idempotent** — re-flushing the same snapshot is a no-op, so the
      scheduler can run it on every replica on every cadence;
    * **monotonic** — a Redis restart / eviction can never walk a stored total
      backwards;
    * **lock-light** — unlike the quota flush (per-row ``SELECT FOR UPDATE
      NOWAIT``, which needed a cluster-wide advisory lock to stop replicas
      colliding with 55P03), concurrent replicas just serialise on the row lock
      for microseconds and converge on the same value.  No advisory lock, no
      retry loop.

    Redis stays the running counter for the current day; the table is the
    durable snapshot, so nothing is lost when a flush is skipped or fails.
    """
    from app.core.database import async_session
    from app.models.agentic_execution import AgenticExecutionDaily

    redis = await redis_client()
    keys: list[str] = []
    async for key in redis.scan_iter(match=f"{_KEY_PREFIX}*", count=200):
        keys.append(key if isinstance(key, str) else key.decode())

    if not keys:
        return 0

    pipe = redis.pipeline()
    for key in keys:
        pipe.get(key)
    values = await pipe.execute()

    # Collapse by Redis key first: SCAN may return the same key more than once
    # (it guarantees no misses, not no duplicates), and counting a key twice
    # would inflate the bucket.
    snapshot: dict[str, str] = {
        key: val
        for key, val in zip(keys, values, strict=True)
        if val is not None
    }

    # Then aggregate into storage buckets, normalizing ``source`` to the DB
    # column width BEFORE it becomes part of the bucket key. Two sources that
    # differ only past the cut MUST land in one bucket: PostgreSQL rejects an
    # ON CONFLICT DO UPDATE that would touch the same row twice, which would
    # abort the whole flush every cycle. Distinct keys sharing a bucket are
    # distinct counters, so they SUM (the per-key collapse above already
    # removed the duplicate-key case, which must not double-count).
    counters: dict[tuple[date, str], int] = {}
    for key, val in snapshot.items():
        day_str, _, source = key.removeprefix(_KEY_PREFIX).partition(":")
        if not source:
            continue
        try:
            day = date.fromisoformat(day_str)
            count = int(val)
        except ValueError:
            logger.debug("Skipping malformed agentic execution key %s", key)
            continue
        if count <= 0:
            continue
        pair = (day, _normalize_source(source))
        counters[pair] = counters.get(pair, 0) + count

    if not counters:
        return 0

    now = datetime.now(UTC)
    rows = [
        {
            "id": uuid.uuid4(),
            "day": day,
            "source": source,
            "count": count,
            "updated_at": now,
        }
        for (day, source), count in counters.items()
    ]

    async with async_session() as db:
        stmt = pg_insert(AgenticExecutionDaily).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["day", "source"],
            set_={
                "count": sa.func.greatest(
                    AgenticExecutionDaily.__table__.c.count, stmt.excluded.count
                ),
                "updated_at": stmt.excluded.updated_at,
            },
        )
        await db.execute(stmt)
        await db.commit()

    logger.debug("Flushed %d agentic execution counters to DB", len(rows))
    return len(rows)
