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

"""Memory Steward history: what a consolidation changed, its undo, its purge (#8258).

A lane is one agent's own memory (``user_id`` NULL) or one user's memory with
an agent. Each change a consolidation pass applies to a lane is one
``MemoryConsolidation`` row naming the entries it flagged superseded; the
entries stay restorable for ``RETENTION_DAYS`` (decision B19), then the hourly
purge deletes them with the row. A user's own deletion of their memory, or
their opt-out (a revocation handler), purges at once.

A change its owner undid, or a promotion whose entry they deleted, leaves a
tombstone (its fingerprint, no text) the lane keeps until its own purge: a
pass never makes that change again.

Who may list or undo is the caller's business (``api/v1/memory_consolidations``):
these functions take the lane the caller may act on and match the row to it.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_memory_entry import AgentMemoryEntry
from app.models.memory_consolidation import (
    MemoryConsolidation,
    MemoryConsolidationTombstone,
    MemoryLaneCheckpoint,
)
from app.models.user import User
from app.services.audit import log_audit
from app.services.health.consent import Revocation, register_revocation_handler
from app.services.memory_holds import HOLDING_OPERATIONS, RETENTION_DAYS

logger = logging.getLogger(__name__)

#: Undone rows stay in the owner's history this long; their tombstone, which
#: keeps a pass from redoing them, stays until the lane is purged.
UNDONE_RETENTION_DAYS = 90


@dataclass(frozen=True)
class Lane:
    agent_id: uuid.UUID
    user_id: uuid.UUID | None

    @property
    def key(self) -> str:
        return f"{self.agent_id}:{self.user_id or 'agent'}"


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def normalized_text(text: str) -> str:
    return " ".join(text.lower().split())


def operation_fingerprint(operation: str, *parts: object) -> str:
    """An operation's identity in its lane: the same entries (or, for a
    promotion, the same observation) give the same fingerprint."""
    return text_digest("|".join([operation, *sorted(str(part) for part in parts)]))


def promotion_fingerprint(text: str) -> str:
    return operation_fingerprint("promote", text_digest(normalized_text(text)))


class ConsolidationNotFoundError(Exception):
    """No consolidation of the caller's lanes has this id."""


class UndoRefusedError(Exception):
    """The consolidation cannot be undone; the reason is for logs only."""


def lane_entries(lane: Lane):
    user = (
        AgentMemoryEntry.user_id.is_(None)
        if lane.user_id is None
        else AgentMemoryEntry.user_id == lane.user_id
    )
    return and_(AgentMemoryEntry.agent_id == lane.agent_id, user)


def lane_rows(lane: Lane):
    user = (
        MemoryConsolidation.user_id.is_(None)
        if lane.user_id is None
        else MemoryConsolidation.user_id == lane.user_id
    )
    return and_(MemoryConsolidation.agent_id == lane.agent_id, user)


async def bury(db: AsyncSession, lane: Lane, fingerprints: Iterable[str]) -> None:
    """Never make these changes again in *lane*."""
    values = [
        {"lane_key": lane.key, "fingerprint": fp, "agent_id": lane.agent_id, "user_id": lane.user_id}
        for fp in set(fingerprints)
    ]
    if values:
        await db.execute(insert(MemoryConsolidationTombstone).values(values).on_conflict_do_nothing())


async def known_fingerprints(db: AsyncSession, lane: Lane) -> frozenset[str]:
    """What a pass must not plan again: the lane's rows and its tombstones."""
    rows = await db.execute(
        select(MemoryConsolidation.fingerprint)
        .where(lane_rows(lane))
        .union(
            select(MemoryConsolidationTombstone.fingerprint).where(MemoryConsolidationTombstone.lane_key == lane.key)
        )
    )
    return frozenset(rows.scalars().all())


# ── History, undo, purge ─────────────────────────────────────────────────────


async def list_consolidations(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    agent_lane_only: bool = False,
    limit: int = 100,
) -> list[MemoryConsolidation]:
    """Newest first: one user's lanes (*user_id*), or one agent's own lane
    (*agent_id* with *agent_lane_only*)."""
    query = select(MemoryConsolidation)
    if user_id is not None:
        query = query.where(MemoryConsolidation.user_id == user_id)
    if agent_id is not None:
        query = query.where(MemoryConsolidation.agent_id == agent_id)
    if agent_lane_only:
        query = query.where(MemoryConsolidation.user_id.is_(None))
    if user_id is None and not agent_lane_only:
        raise ValueError("a consolidation listing names its lanes")
    result = await db.execute(
        query.order_by(MemoryConsolidation.applied_at.desc(), MemoryConsolidation.id.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def consolidation_counts(
    db: AsyncSession, *, since: datetime, agent_id: uuid.UUID | None = None
) -> dict[str, int]:
    """Operations applied since *since* and not undone, per operation: the
    digest's numbers. A ``flag`` changed nothing; it is counted as such."""
    query = select(MemoryConsolidation.operation, func.count()).where(
        MemoryConsolidation.applied_at >= since, MemoryConsolidation.undone_at.is_(None)
    )
    if agent_id is not None:
        query = query.where(MemoryConsolidation.agent_id == agent_id)
    rows = await db.execute(query.group_by(MemoryConsolidation.operation))
    return {operation: int(n) for operation, n in rows.all()}


async def undo_consolidation(
    db: AsyncSession,
    consolidation_id: uuid.UUID,
    *,
    actor: User,
    user_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
) -> MemoryConsolidation:
    """Undo one consolidation of the lane the caller names: *user_id* (their
    own lanes), or *agent_id* without a user (that agent's own lane).

    All or nothing: every entry it changed must still carry the change, else
    :class:`UndoRefusedError` and nothing is restored."""
    query = select(MemoryConsolidation).where(MemoryConsolidation.id == consolidation_id)
    if user_id is not None:
        query = query.where(MemoryConsolidation.user_id == user_id)
    elif agent_id is not None:
        query = query.where(MemoryConsolidation.agent_id == agent_id, MemoryConsolidation.user_id.is_(None))
    else:
        raise ConsolidationNotFoundError
    row = (await db.execute(query.with_for_update())).scalar_one_or_none()
    if row is None:
        raise ConsolidationNotFoundError
    if row.undone_at is not None:
        raise UndoRefusedError("already undone")
    if row.operation == "flag":
        raise UndoRefusedError("a flag changes nothing")
    if row.applied_at <= datetime.now(UTC) - timedelta(days=RETENTION_DAYS):
        raise UndoRefusedError("past the retention")
    lane = Lane(row.agent_id, row.user_id)

    async with db.begin_nested() as savepoint:
        if row.operation == "promote":
            promoted = (
                await db.execute(
                    select(AgentMemoryEntry.content).where(lane_entries(lane), AgentMemoryEntry.id == row.after_entry_id)
                )
            ).scalar_one_or_none()
            if promoted is None:
                await savepoint.rollback()
                raise UndoRefusedError("the promoted entry is gone")
            # An entry edited since holds someone's words: not the Steward's to delete.
            if promotion_fingerprint(promoted) != row.fingerprint:
                await savepoint.rollback()
                raise UndoRefusedError("the promoted entry was edited since")
            await db.execute(
                delete(AgentMemoryEntry)
                .where(lane_entries(lane), AgentMemoryEntry.id == row.after_entry_id)
                .execution_options(synchronize_session=False)
            )
        else:
            before = [uuid.UUID(entry_id) for entry_id in row.before_entry_ids]
            successor = (
                AgentMemoryEntry.superseded_by_id.is_(None)
                if row.after_entry_id is None
                else AgentMemoryEntry.superseded_by_id == row.after_entry_id
            )
            restored = await db.execute(
                update(AgentMemoryEntry)
                .where(
                    lane_entries(lane),
                    AgentMemoryEntry.id.in_(before),
                    AgentMemoryEntry.superseded_at.is_not(None),
                    successor,
                )
                .values(superseded_at=None, superseded_by_id=None)
                .returning(AgentMemoryEntry.id)
                .execution_options(synchronize_session=False)
            )
            if len(restored.scalars().all()) != len(before):
                await savepoint.rollback()
                raise UndoRefusedError("an entry changed since")
        row.undone_at = datetime.now(UTC)
        await bury(db, lane, [row.fingerprint])
        await db.flush()

    await log_audit(
        db,
        user=actor,
        action="undo",
        entity_type="memory_consolidation",
        entity_id=row.id,
        entity_name=f"Memory {row.operation}",
        summary=f"Undid a Memory Steward {row.operation}",
        details={
            "agent_id": str(row.agent_id),
            "lane": "user" if row.user_id else "agent",
            "before_entry_ids": row.before_entry_ids,
            "after_entry_id": str(row.after_entry_id) if row.after_entry_id else None,
        },
    )
    return row


def _held_ids(rows: Iterable[MemoryConsolidation]) -> list[uuid.UUID]:
    return [
        uuid.UUID(entry_id)
        for row in rows
        if row.operation in HOLDING_OPERATIONS and row.undone_at is None
        for entry_id in row.before_entry_ids
    ]


async def _purge_rows(
    db: AsyncSession, rows: Sequence[MemoryConsolidation], *, holding: Sequence[str] = HOLDING_OPERATIONS
) -> int:
    """Delete the entries the *holding* ones of *rows* still hold (still
    superseded), then all the rows."""
    held = _held_ids(row for row in rows if row.operation in holding)
    deleted = 0
    if held:
        result = await db.execute(
            delete(AgentMemoryEntry)
            .where(AgentMemoryEntry.id.in_(held), AgentMemoryEntry.superseded_at.is_not(None))
            .execution_options(synchronize_session=False)
        )
        deleted = int(getattr(result, "rowcount", 0) or 0)
    if rows:
        await db.execute(
            delete(MemoryConsolidation)
            .where(MemoryConsolidation.id.in_([row.id for row in rows]))
            .execution_options(synchronize_session=False)
        )
    await db.flush()
    return deleted


async def purge_lane_consolidations(
    db: AsyncSession, *, user_id: uuid.UUID, agent_id: uuid.UUID | None = None
) -> int:
    """At once, retention included: a user's own deletion of their memory
    (all of it, or with one agent) or their opt-out. Returns the held
    entries deleted."""
    query = select(MemoryConsolidation).where(MemoryConsolidation.user_id == user_id)
    checkpoints = delete(MemoryLaneCheckpoint).where(MemoryLaneCheckpoint.user_id == user_id)
    if agent_id is not None:
        query = query.where(MemoryConsolidation.agent_id == agent_id)
        checkpoints = checkpoints.where(MemoryLaneCheckpoint.agent_id == agent_id)
    tombstones = delete(MemoryConsolidationTombstone).where(MemoryConsolidationTombstone.user_id == user_id)
    if agent_id is not None:
        tombstones = tombstones.where(MemoryConsolidationTombstone.agent_id == agent_id)
    rows = list((await db.execute(query)).scalars().all())
    deleted = await _purge_rows(db, rows)
    await db.execute(checkpoints.execution_options(synchronize_session=False))
    await db.execute(tombstones.execution_options(synchronize_session=False))
    return deleted


async def purge_user_retention(db: AsyncSession, user_id: uuid.UUID) -> int:
    """The user opted out: what the Steward holds of their lanes goes now."""
    return await purge_lane_consolidations(db, user_id=user_id)


async def purge_on_revocation(db: AsyncSession, revocation: Revocation) -> None:
    """A user's opt-out purges at once, in the revoking transaction (B19). A
    revoked agent opt-in only stops the passes: consent is read on each."""
    if revocation.kind == "user_opt_out" and revocation.user_id is not None:
        await purge_user_retention(db, revocation.user_id)


register_revocation_handler(purge_on_revocation)


async def purge_entry_consolidations(db: AsyncSession, entry_id: uuid.UUID) -> int:
    """An entry was deleted by hand: the rows naming it go. Its merged
    duplicates go too; the older side of a contradiction it won comes back;
    its promotion is never made again."""
    text_id = str(entry_id)
    rows = list(
        (
            await db.execute(
                select(MemoryConsolidation).where(
                    or_(
                        MemoryConsolidation.after_entry_id == entry_id,
                        MemoryConsolidation.before_entry_ids.op("@>")(func.jsonb_build_array(text_id)),
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    won = [
        uuid.UUID(older)
        for row in rows
        if row.operation == "remove" and row.after_entry_id == entry_id and row.undone_at is None
        for older in row.before_entry_ids
    ]
    if won:
        await db.execute(
            update(AgentMemoryEntry)
            .where(AgentMemoryEntry.id.in_(won), AgentMemoryEntry.superseded_by_id == entry_id)
            .values(superseded_at=None, superseded_by_id=None)
            .execution_options(synchronize_session=False)
        )
    for row in rows:
        if row.operation == "promote":
            await bury(db, Lane(row.agent_id, row.user_id), [row.fingerprint])
    return await _purge_rows(db, rows, holding=("merge",))


async def purge_expired_consolidations(db: AsyncSession, *, now: datetime | None = None) -> dict[str, int]:
    """Hourly: entries held past ``RETENTION_DAYS`` are deleted with their row;
    undone rows go after ``UNDONE_RETENTION_DAYS``."""
    now = now or datetime.now(UTC)
    expired = list(
        (
            await db.execute(
                select(MemoryConsolidation).where(
                    MemoryConsolidation.undone_at.is_(None),
                    MemoryConsolidation.applied_at < now - timedelta(days=RETENTION_DAYS),
                )
            )
        )
        .scalars()
        .all()
    )
    # A promotion outlives its row: deleted by hand later, it must not be
    # promoted again from a summary that still holds it.
    for row in expired:
        if row.operation == "promote":
            await bury(db, Lane(row.agent_id, row.user_id), [row.fingerprint])
    entries = await _purge_rows(db, expired)
    undone = await db.execute(
        delete(MemoryConsolidation)
        .where(
            MemoryConsolidation.undone_at.is_not(None),
            MemoryConsolidation.undone_at < now - timedelta(days=UNDONE_RETENTION_DAYS),
        )
        .execution_options(synchronize_session=False)
    )
    await db.flush()
    return {
        "rows": len(expired) + int(getattr(undone, "rowcount", 0) or 0),
        "entries": entries,
    }
