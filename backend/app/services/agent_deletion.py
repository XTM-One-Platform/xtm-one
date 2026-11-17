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

"""Deleting an agent: the one path of the REST route and the agent-builder tool.

``DELETE /api/v1/agents/{agent_id}`` and the agent builder's ``delete_agent``
tool used to delete an agent through two copies that drifted apart: the tool
left the agent's system-managed knowledge bases, the live executions of its
runs and the audit entry behind. Both call :func:`delete_agent` now. What each
keeps is its own permission gate: the route checks on the request session
before calling it, the tool re-checks in every attempt through *authorize*.

The agent's runs go first, in committed batches: one statement over a
history of tens of thousands of runs, each carrying its conversation and its
trigger payload, outlasted the 30 s ``statement_timeout`` on every attempt and
rolled everything back.

Its conversations and traces stay: the deletion writes the agent's
tombstone, marks the conversations it held, and hands them to a successor
when the caller names one (``conversation_continuity.record_agent_deletion``).
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, NamedTuple

from sqlalchemy import Text, cast, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import safe_uuid
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.embedding import Embedding
from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
from app.services.audit import log_audit
from app.services.kb_helpers import cancel_document_jobs_after_commit

if TYPE_CHECKING:
    from fastapi import Request

    from app.models.user import User

#: Answers the user the deletion is audited as, or a refusal to return as is.
Authorize = Callable[[AsyncSession], Awaitable["User | str"]]

#: Runs deleted per committed transaction ahead of the agent's own.
RUN_BATCH_SIZE = 500


class _RunBatch(NamedTuple):
    #: Runs the batch read: a full batch is followed by another.
    selected: int
    #: Runs its DELETE removed, fewer when a concurrent deletion took some.
    deleted: int


async def delete_agent(
    agent_id: uuid.UUID,
    *,
    authorize: Authorize,
    request: Request | None = None,
    via: str | None = None,
    successor_id: uuid.UUID | None = None,
) -> str | None:
    """Delete agent *agent_id* and everything it owns.

    *successor_id* names the agent its conversations continue with, for the
    owners who can use it (``conversation_continuity.record_agent_deletion``).
    It is checked before anything is deleted, and again in the agent's
    transaction: a successor that cannot take them is a refusal.

    All the destructive work runs in a dedicated, serialized, deadlock-retried
    transaction. Deleting an agent cascades onto child tables that carry TWO
    agent FKs each (``agent_delegations`` / ``sub_agent_tasks``) and SET NULL
    on ``conversations`` / ``work_items``; two agents deleted concurrently can
    cascade onto the same rows in opposite lock order and deadlock (40P01).
    ``acquire_agent_delete_lock`` serializes every agent deletion so they never
    deadlock against each other, and ``run_with_deadlock_retry`` covers the
    residual delete-vs-non-delete-writer class, so an agent can always be
    deleted whatever the concurrency.

    Before that transaction, the agent's runs are deleted in batches of
    ``RUN_BATCH_SIZE``, each its own serialized, deadlock-retried transaction
    (:func:`_delete_run_batch`), so no statement grows with the agent's
    history and a deletion that fails keeps the batches it committed: the next
    attempt resumes where it stopped. The agent's transaction then only meets
    the runs created while the batches ran.

    *authorize* runs first in every transaction, on that transaction's
    session, and answers the user the deletion is audited as, or a refusal,
    which is returned with nothing more deleted. *via* names the surface in the
    audit entry when it is not the REST API. Returns ``None`` once the agent is
    gone - deleted here, or already gone when the lock was granted - and its
    memory cleared; raises what the retries did not absorb, for the caller to
    word.
    """
    from app.api.v1.helpers import run_with_deadlock_retry
    from app.core.database import async_session

    if successor_id is not None:
        refusal = await _successor_check(agent_id, successor_id, authorize)
        if refusal is not None:
            return refusal

    refusal = await _delete_runs_in_batches(agent_id, authorize, request=request, via=via)
    if refusal is not None:
        return refusal

    async def _attempt() -> str | None:
        async with async_session() as db:
            try:
                actor = await authorize(db)
                if isinstance(actor, str):
                    await db.rollback()
                    return actor
                refused = await _delete_agent_rows(
                    db, agent_id, actor=actor, request=request, via=via, successor_id=successor_id
                )
                if isinstance(refused, str):
                    await db.rollback()
                    return refused
                await db.commit()
            except Exception:
                # Roll back before the session returns to the pool so it never
                # carries a FAILED transaction, then re-raise so a transient
                # deadlock / serialization failure is retried.
                await db.rollback()
                raise
        return None

    refusal = await run_with_deadlock_retry(_attempt, label=f"delete_agent({agent_id})")
    if refusal is not None:
        return refusal

    # Only once the agent is really gone: cleared before, a failed delete left
    # the agent with no memory, and a consolidation in flight wrote it back.
    from app.services.agent_memory import clear_deleted_agent_memory

    await clear_deleted_agent_memory(str(agent_id))
    return None


async def _successor_check(agent_id: uuid.UUID, successor_id: uuid.UUID, authorize: Authorize) -> str | None:
    """Why *successor_id* cannot take the agent's conversations, before any run goes."""
    from app.core.database import async_session
    from app.services.conversation_continuity import successor_refusal

    async with async_session() as db:
        try:
            actor = await authorize(db)
            if isinstance(actor, str):
                return actor
            return await successor_refusal(db, actor_id=actor.id, agent_id=agent_id, successor_id=successor_id)
        finally:
            await db.rollback()


async def _delete_runs_in_batches(
    agent_id: uuid.UUID,
    authorize: Authorize,
    *,
    request: Request | None,
    via: str | None,
) -> str | None:
    """Delete the agent's runs a committed batch at a time; a refusal stops it and is returned.

    The first batch that deletes runs writes the ``purge_runs`` audit entry in
    its own transaction, so no run goes unaudited when the deletion fails
    before the agent's ``delete`` entry.
    """
    from app.api.v1.helpers import run_with_deadlock_retry
    from app.core.database import async_session

    audited = False

    async def _batch() -> str | _RunBatch:
        async with async_session() as db:
            try:
                actor = await authorize(db)
                if isinstance(actor, str):
                    await db.rollback()
                    return actor
                batch = await _delete_run_batch(db, agent_id)
                if batch.deleted and not audited:
                    await _audit_run_purge(db, agent_id, actor=actor, request=request, via=via)
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return batch

    while True:
        outcome = await run_with_deadlock_retry(_batch, label=f"delete_agent({agent_id}) runs")
        if isinstance(outcome, str):
            return outcome
        audited = audited or outcome.deleted > 0
        if outcome.selected < RUN_BATCH_SIZE:
            return None


async def _audit_run_purge(
    db: AsyncSession,
    agent_id: uuid.UUID,
    *,
    actor: User,
    request: Request | None,
    via: str | None,
) -> None:
    agent = await db.get(Agent, agent_id)
    if agent is None:
        return
    details: dict = {"slug": agent.slug, "is_company_managed": agent.is_company_managed, "reason": "agent_deletion"}
    if via is not None:
        details["via"] = via
    await log_audit(
        db,
        user=actor,
        action="purge_runs",
        entity_type="agent",
        entity_id=agent.id,
        entity_name=agent.name,
        summary=f"Purged the runs of agent '{agent.name}' to delete it",
        details=details,
        request=request,
    )


async def _delete_agent_rows(
    db: AsyncSession,
    agent_id: uuid.UUID,
    *,
    actor: User,
    request: Request | None,
    via: str | None,
    successor_id: uuid.UUID | None = None,
) -> str | None:
    """Every row an agent deletion removes or settles, in *db*'s transaction.

    DB-persistent agent memories go with the ``agent_memory_entries`` ON
    DELETE CASCADE of the agent row. Returns a refusal when *successor_id*
    can no longer take the agent's conversations, with nothing written.
    """
    from app.a2a.tasks import lock_tasks_before_deletion
    from app.api.v1.helpers import acquire_agent_delete_lock, purge_agent_references
    from app.services.conversation_continuity import record_agent_deletion, successor_refusal
    from app.services.orchestration_engine import cancel_executions_for_runs
    from app.services.sub_agent import mark_sub_agent_tasks_cancelled_for_runs

    # Serialize BEFORE taking any row lock, then read the agent as the lock
    # left it: a deletion that held it before may have removed it.
    await acquire_agent_delete_lock(db)
    agent = await db.get(Agent, agent_id, populate_existing=True)
    if agent is None:
        return None
    successor: Agent | None = None
    if successor_id is not None:
        refusal = await successor_refusal(db, actor_id=actor.id, agent_id=agent_id, successor_id=successor_id)
        if refusal is not None:
            return refusal
        successor = await db.get(Agent, successor_id)

    # Before the first write: the A2A tasks this deletion reaches are the first
    # rows every other writer of a task and its run takes.
    await lock_tasks_before_deletion(db, agent_ids=[agent_id])
    # Then the row lock the agent's DELETE takes anyway, before its runs are
    # read: a run or a background task is inserted under a key-share lock on
    # its agent, so none is created for this agent from here on, and every run
    # the deletion removes is one whose executions it cancels.
    await db.execute(select(Agent.id).where(Agent.id == agent_id).with_for_update())

    await _delete_system_knowledge_bases(db, agent)

    # Cancel the live orchestration executions of the agent's runs before the
    # runs go, so none keeps a reconciler slot or wedges the sub-agent fan-out
    # counter on pending, and cancel the background tasks spawned under them.
    run_ids = (await db.execute(select(AssignmentRun.id).where(AssignmentRun.agent_id == agent_id))).scalars().all()
    await cancel_executions_for_runs(db, run_ids)
    await mark_sub_agent_tasks_cancelled_for_runs(db, run_ids)

    # Again in the statement before each DELETE that reaches a task: a run or
    # task created since the first lock goes too, and must be held first too.
    await lock_tasks_before_deletion(db, agent_ids=[agent_id])
    await db.execute(delete(AssignmentRun).where(AssignmentRun.agent_id == agent_id))
    await db.execute(delete(Assignment).where(Assignment.agent_id == agent_id))

    details: dict = {"slug": agent.slug, "is_company_managed": agent.is_company_managed}
    if successor is not None:
        details["successor_agent_id"] = str(successor.id)
    if via is not None:
        details["via"] = via
    await log_audit(
        db,
        user=actor,
        action="delete",
        entity_type="agent",
        entity_id=agent.id,
        entity_name=agent.name,
        summary=f"Deleted agent '{agent.name}'",
        details=details,
        request=request,
    )

    # The agent-id references no FK cleans (handover edges on other agents and
    # the General Assistant, default / linked / exposed-agent pointers), so no
    # ghost node stays in the topology graph.
    await purge_agent_references(db, agent_id)

    # Its tombstone, and its conversations marked or handed to the successor,
    # where the SET NULL cascade below would lock the same rows.
    await record_agent_deletion(db, agent, actor_id=getattr(actor, "id", None), successor=successor)

    # Its cascade reaches its exposures, contexts and their tasks.
    await lock_tasks_before_deletion(db, agent_ids=[agent_id])
    await db.delete(agent)
    return None


async def _delete_run_batch(db: AsyncSession, agent_id: uuid.UUID) -> _RunBatch:
    """Delete the agent's ``RUN_BATCH_SIZE`` oldest runs in *db*'s transaction.

    The steps and the lock order of :func:`_delete_agent_rows` over a bounded
    set of runs: serialized with every agent deletion, the A2A tasks of the
    batch's runs locked before the first write (only theirs: the agent's
    exposures and contexts go with the agent, and locking every run's tasks
    would make each batch grow with the history), then the agent's row lock (no
    run nor background task is created for the agent while the batch runs),
    then the runs themselves, then the live executions and background tasks of
    the batch cancelled, and its tasks locked again in the statement before the
    DELETE. Each assignment's
    ``execution_count`` loses the runs the DELETE removed, as a single run's
    deletion does: a deletion that fails later leaves assignments counting the
    runs they still have.
    """
    from app.a2a.tasks import lock_tasks_before_deletion
    from app.api.v1.helpers import acquire_agent_delete_lock
    from app.services.orchestration_engine import cancel_executions_for_runs
    from app.services.sub_agent import mark_sub_agent_tasks_cancelled_for_runs

    await acquire_agent_delete_lock(db)
    run_ids = (
        (
            await db.execute(
                select(AssignmentRun.id)
                .where(AssignmentRun.agent_id == agent_id)
                .order_by(AssignmentRun.created_at)
                .limit(RUN_BATCH_SIZE)
            )
        )
        .scalars()
        .all()
    )
    if not run_ids:
        return _RunBatch(selected=0, deleted=0)
    selected = len(run_ids)
    await lock_tasks_before_deletion(db, run_ids=run_ids)
    await db.execute(select(Agent.id).where(Agent.id == agent_id).with_for_update())
    # The runs before their executions, in key order, as a single run's
    # deletion and the cancellation of child runs take them.
    run_ids = (
        (
            await db.execute(
                select(AssignmentRun.id)
                .where(AssignmentRun.id.in_(run_ids))
                .order_by(AssignmentRun.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if not run_ids:
        return _RunBatch(selected=selected, deleted=0)
    await cancel_executions_for_runs(db, run_ids)
    await mark_sub_agent_tasks_cancelled_for_runs(db, run_ids)
    await lock_tasks_before_deletion(db, run_ids=run_ids)
    deleted_for = (
        (
            await db.execute(
                delete(AssignmentRun).where(AssignmentRun.id.in_(run_ids)).returning(AssignmentRun.assignment_id)
            )
        )
        .scalars()
        .all()
    )
    for assignment_id, deleted in Counter(a for a in deleted_for if a is not None).items():
        await db.execute(
            update(Assignment)
            .where(Assignment.id == assignment_id)
            .values(execution_count=func.greatest(Assignment.execution_count - deleted, 0))
        )
    return _RunBatch(selected=selected, deleted=len(deleted_for))


async def _delete_system_knowledge_bases(db: AsyncSession, agent: Agent) -> None:
    """The system-managed knowledge bases bound to *agent* alone, with their documents and vectors.

    One another agent binds too stays: it is theirs as well, and goes with the
    last agent bound to it - the rule the knowledge-base deletions follow, which
    refuse a system-managed knowledge base an agent still binds.
    """
    kb_ids = [kb_id for kb_id in map(safe_uuid, agent.knowledge_base_ids or []) if kb_id is not None]
    if not kb_ids:
        return
    system_kbs = (
        (
            await db.execute(
                select(KnowledgeBase).where(KnowledgeBase.id.in_(kb_ids), KnowledgeBase.is_system_managed.is_(True))
            )
        )
        .scalars()
        .all()
    )
    if not system_kbs:
        return
    bound_elsewhere = await _bound_by_other_agents(db, agent.id, [str(kb.id) for kb in system_kbs])
    system_kbs = [kb for kb in system_kbs if str(kb.id) not in bound_elsewhere]
    if not system_kbs:
        return
    system_kb_ids = [kb.id for kb in system_kbs]
    # Their queued embedding jobs go once this transaction commits, never
    # under the delete lock.
    cancel_document_jobs_after_commit(
        db,
        (
            await db.execute(
                select(KnowledgeDocument.id).where(KnowledgeDocument.knowledge_base_id.in_(system_kb_ids))
            )
        )
        .scalars()
        .all(),
    )
    await db.execute(delete(Embedding).where(Embedding.knowledge_base_id.in_(system_kb_ids)))
    await db.execute(delete(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id.in_(system_kb_ids)))
    for kb in system_kbs:
        await db.delete(kb)


async def _bound_by_other_agents(db: AsyncSession, agent_id: uuid.UUID, kb_ids: list[str]) -> set[str]:
    """The ids among *kb_ids* another agent's ``knowledge_base_ids`` holds."""
    # A text ``contains`` only narrows the scan; the membership test is exact.
    rows = (
        (
            await db.execute(
                select(Agent.knowledge_base_ids).where(
                    Agent.id != agent_id,
                    or_(*(cast(Agent.knowledge_base_ids, Text).contains(kb_id) for kb_id in kb_ids)),
                )
            )
        )
        .scalars()
        .all()
    )
    bound = {str(kb_id) for ids in rows for kb_id in ids or []}
    return {kb_id for kb_id in kb_ids if kb_id in bound}
