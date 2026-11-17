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

"""Conversational grouping for the inbound surface.

A2A gives a client one identifier that spans several tasks — ``contextId``. It
is what makes ``transfer`` implementable at all: the contract is that the target
handles the messages that follow, so there has to be something a follow-up
belongs to.

Three rules live in this module, and each is a refusal as much as a capability:

* the platform mints every identifier, so a partner cannot name one;
* a context resolves only for the client that created it, and an identifier this
  deployment never issued resolves exactly as another client's would;
* who answers next is recorded, moved by a transfer and by nothing else.

The card a context was created under is frozen on it. A transfer changes which
agent answers, never which contract the partner validated its request against —
so a follow-up is still bounded by the exposure the partner originally addressed.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.a2a import A2AContext
from app.models.agent import Agent


async def mint(
    db: AsyncSession,
    *,
    client_id: str,
    agent_id: uuid.UUID,
    exposure_id: uuid.UUID,
) -> A2AContext:
    """Create a context for a submission that carried no identifier.

    Added to the session but deliberately not flushed on its own: the context
    and the first task under it are one unit of work. A submission that turns
    out to be a duplicate has its insert rolled back, and a context left behind
    by it would group nothing.

    The identifier is assigned here rather than left to the column default,
    which SQLAlchemy applies at flush: the caller needs it *now*, to set on the
    task it is about to insert in the same unit of work.
    """
    context = A2AContext(
        id=uuid.uuid4(),
        client_id=client_id,
        responsible_agent_id=agent_id,
        exposure_id=exposure_id,
    )
    db.add(context)
    return context


async def resolve(
    db: AsyncSession, context_id: str | None, client_id: str
) -> A2AContext | None:
    """The client's own context, or ``None``.

    ``None`` covers three cases the caller must not tell apart: a malformed
    identifier, one this deployment never issued, and one belonging to another
    client. They share an answer so the existence of a context cannot be probed,
    which is the same uniform not-found the task surface already returns.

    The client is part of the query rather than a check on the result: a
    post-filter leaks existence through timing, and survives only as long as
    nobody refactors the check away.
    """
    if not context_id:
        return None
    try:
        parsed = uuid.UUID(str(context_id))
    except (ValueError, AttributeError, TypeError):
        # A partner-supplied string arrives here unparsed. An exception would
        # be a 500 where a not-found belongs.
        return None
    return (
        await db.execute(
            select(A2AContext).where(
                A2AContext.id == parsed,
                A2AContext.client_id == client_id,
            )
        )
    ).scalar_one_or_none()


async def set_responsible(
    db: AsyncSession, context: A2AContext, agent_id: uuid.UUID
) -> None:
    """Record that *agent_id* answers this context from now on.

    Called for a transfer only. Consultation returns to the agent that asked,
    and background delegation does not answer the partner at all, so neither
    changes who owns the conversation.
    """
    if context.responsible_agent_id == agent_id:
        return
    context.responsible_agent_id = agent_id
    await db.flush()


#: How many unfinished tasks one context may hold: the one executing and the
#: follow-ups queued behind it. Parallel messages in one conversation are
#: queued rather than refused, so this is the only bound left on a partner
#: stacking work under one context; past it the submission is refused as a
#: rate limit, with ``CONTEXT_QUEUE_RETRY_AFTER_SECONDS`` to wait.
MAX_OPEN_TASKS_PER_CONTEXT = 10
CONTEXT_QUEUE_RETRY_AFTER_SECONDS = 30


def _open_tasks_query(context_id: uuid.UUID, before: Any = None) -> Any:
    """The unfinished tasks under a context, optionally only those ahead of *before*.

    Read from the run rather than stored on the context, so it cannot disagree
    with what ``tasks/get`` reports. Delegated work counts: ``resolve_state``
    keeps a task ``working`` while a child of its run is alive, so reading the
    run alone would call a task the partner is being told is running "not open".

    *before* is a task of the same context: only the tasks submitted ahead of
    it are selected, ordered by creation then id so two submissions in the same
    instant still have one order every replica agrees on.
    """
    from sqlalchemy import String, and_, cast, or_

    from app.models.a2a import A2ATask
    from app.models.assignment import AssignmentRun
    from app.models.sub_agent_task import SubAgentTask

    live_child = (
        select(SubAgentTask.id)
        .where(
            SubAgentTask.status.in_(("pending", "running")),
            or_(
                SubAgentTask.metadata_json["parent_run_id"].astext
                == cast(A2ATask.run_id, String),
                SubAgentTask.metadata_json["assignment_run_id"].astext
                == cast(A2ATask.run_id, String),
            ),
        )
        .exists()
    )
    query = (
        select(A2ATask.id)
        .join(AssignmentRun, AssignmentRun.id == A2ATask.run_id)
        .where(
            A2ATask.context_id == context_id,
            A2ATask.cancel_requested_at.is_(None),
            or_(
                AssignmentRun.status.in_(("pending", "delayed", "running")),
                live_child,
            ),
        )
    )
    if before is not None:
        query = query.where(
            A2ATask.id != before.id,
            or_(
                A2ATask.created_at < before.created_at,
                and_(
                    A2ATask.created_at == before.created_at,
                    A2ATask.id < before.id,
                ),
            ),
        )
    return query


async def open_task_count(db: AsyncSession, context: A2AContext) -> int:
    """How many tasks under *context* have not finished, up to the queue bound.

    Capped one past the bound: the caller only asks whether the bound is
    reached, and an uncapped count is a full scan of a busy context.
    """
    rows = (
        await db.execute(
            _open_tasks_query(context.id).limit(MAX_OPEN_TASKS_PER_CONTEXT + 1)
        )
    ).scalars().all()
    return len(rows)


async def has_open_task_ahead(db: AsyncSession, task: Any) -> bool:
    """Whether a task submitted ahead of *task* in its context has not finished.

    A follow-up sent while its conversation is busy is queued, not refused: it
    runs once every task ahead of it is finished. Two tasks executing under one
    context at once would leave the partner unable to say which answered what,
    and the second would race the first's transfer for who is responsible, so
    the order of submission is the order of execution.
    """
    if task.context_id is None:
        return False
    rows = (
        await db.execute(
            _open_tasks_query(task.context_id, before=task).limit(1)
        )
    ).scalars().all()
    return bool(rows)


async def reachable_agent_ids(db: AsyncSession, client_id: str) -> set[uuid.UUID]:
    """Every agent this partner's registration still binds.

    The one place that answers "may this partner reach that agent", so a
    follow-up's refusal and a transfer's record of who answers next cannot
    disagree about it.
    """
    from app.a2a import providers
    from app.models.integration import Integration

    integration_id = await providers.registration_integration_id(db, client_id)
    if integration_id is None:
        return set()
    integration = (
        await db.execute(
            select(Integration).where(Integration.id == integration_id)
        )
    ).scalar_one_or_none()
    if integration is None:
        return set()
    return {
        a.id
        for a in await providers.agents_binding(
            db, integration_id, integration.provider
        )
    }


async def responsible_agent_if_reachable(
    db: AsyncSession, context: A2AContext, client_id: str
) -> Agent | None:
    """The agent that answers this context, if this partner may still reach it.

    ``None`` when the operator has since unbound that agent from this partner's
    integration, or disabled it. Responsibility recorded once is not consent
    forever: a follow-up is refused rather than answered by an agent the
    partner's own binding no longer covers.
    """
    agent = (
        await db.execute(
            select(Agent).where(Agent.id == context.responsible_agent_id)
        )
    ).scalar_one_or_none()
    if agent is None:
        return None
    reachable = await reachable_agent_ids(db, client_id)
    return agent if agent.id in reachable else None
