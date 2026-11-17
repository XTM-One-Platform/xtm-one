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

"""The sweep that keeps the cross-agent user memory true.

Hourly, install-wide (a Redis lease decides, like the other sweeps), while
agentic AI may run (the scheduler's memory-maintenance step), in the
background so the scheduler tick never waits on a model:

1. An older free-text profile is split by the consolidation
   model (``user_memory.migrate_legacy_profile``), given the agents the user
   worked with: what is about the user becomes the profile, what an agent's
   work left becomes that agent's notes, the rest is dropped. Until then no
   agent reads it. A profile is offered at most once a day and a sweep makes
   at most ``_MAX_MIGRATIONS_PER_SWEEP`` model calls, split or not. A visitor
   of the public widget has no account to read its agents from: its next
   consolidation splits it.
2. An entry past its TTL is dropped (an agent never reads one anyway).

Every write goes through ``user_memory.update_entries``: applied by entry id
onto the entries as stored at write time, never over a purge that ran since.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from app.core.redis import redis_client
from app.core.utils import format_exception_message, safe_uuid
from app.services.user_memory_entries import MemoryEntry

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECS = 3_600
# Outside the ``user_mem:`` family: a memory purge has no reason to drop it.
_SWEEP_LEASE_KEY = "lease:user_memory_sweep"
# The agents a migrated profile can hand notes to: those the user worked with lately.
_ROSTER_WINDOW_DAYS = 180
_ROSTER_MAX_AGENTS = 30
# Model calls per sweep: a profile offered and not split counts as well.
_MAX_MIGRATIONS_PER_SWEEP = 200
# A profile is offered to the model at most once in this window, split or not.
_SPLIT_ATTEMPT_KEY = "lease:user_memory_split:{identity}"
_SPLIT_RETRY_SECS = 86_400

_running: set[asyncio.Task] = set()


async def sweep_user_memories_if_due() -> bool:
    """Start :func:`sweep_user_memories` when this process takes the hourly lease."""
    try:
        redis = await redis_client()
        if not await redis.set(_SWEEP_LEASE_KEY, "1", nx=True, ex=SWEEP_INTERVAL_SECS):
            return False
    except Exception as exc:
        logger.debug("User memory sweep lease not taken: %s", format_exception_message(exc))
        return False
    task = asyncio.create_task(_sweep_logged(), name="user-memory-sweep")
    _running.add(task)
    task.add_done_callback(_running.discard)
    return True


async def _sweep_logged() -> None:
    try:
        await sweep_user_memories()
    except Exception as exc:
        logger.error("User memory sweep error: %s", format_exception_message(exc), exc_info=True)


async def _identities(redis) -> list[str]:
    identities: set[str] = set()
    for tier in ("entries", "medium"):
        prefix = f"user_mem:{tier}:"
        cursor: int = 0
        while True:
            cursor, keys = await redis.scan(cursor, match=f"{prefix}*", count=500)
            cursor = int(cursor)
            for key in keys:
                name = key if isinstance(key, str) else key.decode()
                identities.add(name[len(prefix) :])
            if not cursor:
                break
    return sorted(identities)


async def sweep_user_memories() -> dict[str, int]:
    """One pass over every memory; the counts of what it changed."""
    redis = await redis_client()
    totals = {"offered": 0, "migrated": 0, "expired": 0}
    for identity in await _identities(redis):
        try:
            counts = await sweep_identity(redis, identity, migrate=totals["offered"] < _MAX_MIGRATIONS_PER_SWEEP)
        except Exception as exc:
            logger.warning("User memory sweep failed for %s: %s", identity, format_exception_message(exc))
            continue
        for key, value in counts.items():
            totals[key] += value
    if any(totals.values()):
        logger.info(
            "User memory sweep: %d profile(s) offered to the model, %d split, %d expired note(s) dropped",
            totals["offered"],
            totals["migrated"],
            totals["expired"],
        )
    return totals


async def sweep_identity(redis, identity: str, *, migrate: bool = True) -> dict[str, int]:
    """Split the free-text profile of one user, or drop the expired entries of one memory."""
    from app.services import user_memory

    counts = {"offered": 0, "migrated": 0, "expired": 0}
    generation = await user_memory.memory_generation(redis, identity)
    entries, legacy, stored = await user_memory._read_entries(redis, identity)
    if not stored:
        user_id = safe_uuid(identity)
        if not (migrate and legacy.strip() and user_id is not None):
            return counts
        if not await redis.set(
            _SPLIT_ATTEMPT_KEY.format(identity=identity), "1", nx=True, ex=_SPLIT_RETRY_SECS
        ):
            return counts
        counts["offered"] = 1
        if await _migrate(identity, user_id):
            counts["migrated"] = 1
        return counts

    now = datetime.now(UTC)
    if all(entry.is_live(now) for entry in entries):
        return counts
    removed = 0

    def _unexpired(current: list[MemoryEntry] | None) -> list[MemoryEntry] | None:
        # Judged on the entries as stored at write time: a note restated
        # since the snapshot is live again and stays.
        nonlocal removed
        if current is None:
            return None
        kept = [entry for entry in current if entry.is_live(now)]
        removed = len(current) - len(kept)
        return kept if removed else None

    if await user_memory.update_entries(redis, identity, _unexpired, generation=generation) is not None:
        counts["expired"] = removed
        await user_memory._undo_if_account_deleted(redis, identity)
    return counts


async def _migrate(identity: str, user_id: uuid.UUID) -> bool:
    """Have the consolidation model split one account's free-text profile."""
    from app.core.database import async_session
    from app.llm.router import get_llm_for_user
    from app.services import user_memory

    async with async_session() as db:
        agents = await _recent_agents(db, user_id)
        llm, model = await get_llm_for_user(db, None)
    return await user_memory.migrate_legacy_profile(identity, llm, agents=agents, model=model)


async def _recent_agents(db, user_id: uuid.UUID) -> list[tuple[str, str]]:
    """``(id, name)`` of the agents the user worked with lately, the latest first."""
    from sqlalchemy import func, select, union_all

    from app.models.agent import Agent
    from app.models.assignment import AssignmentRun
    from app.models.chat import Conversation

    since = datetime.now(UTC) - timedelta(days=_ROSTER_WINDOW_DAYS)
    worked = union_all(
        select(Conversation.agent_id.label("agent_id"), Conversation.updated_at.label("at")).where(
            Conversation.user_id == user_id,
            Conversation.agent_id.is_not(None),
            Conversation.updated_at >= since,
        ),
        select(AssignmentRun.agent_id.label("agent_id"), AssignmentRun.created_at.label("at")).where(
            AssignmentRun.user_id == user_id,
            AssignmentRun.created_at >= since,
        ),
    ).subquery()
    latest = (
        select(worked.c.agent_id, func.max(worked.c.at).label("at"))
        .group_by(worked.c.agent_id)
        .subquery()
    )
    rows = await db.execute(
        select(Agent.id, Agent.name)
        .join(latest, latest.c.agent_id == Agent.id)
        .order_by(latest.c.at.desc())
        .limit(_ROSTER_MAX_AGENTS)
    )
    return [(str(agent_id), name or "?") for agent_id, name in rows.all()]
