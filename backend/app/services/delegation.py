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

"""Delegation service – unified tracking for all inter-agent interactions.

Provides a single API for recording consultations, transfers, and background
tasks.  Replaces the narrower ``sub_agent.py`` for new code; the old service
is kept for backward compat with the ``spawn_background_task`` tool flow.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_delegation import AgentDelegation
from app.services.sub_agent_result_text import stored_answer

logger = logging.getLogger(__name__)


# ── Public helpers ────────────────────────────────────────────────────────────


async def log_delegation(
    db: AsyncSession,
    *,
    delegation_type: str,  # "consult" | "transfer" | "background_task"
    user_id: str | None,
    parent_agent_id: str,
    target_agent_id: str,
    description: str,
    status: str = "completed",
    result: str | None = None,
    error: str | None = None,
    conversation_id: str | None = None,
    message_id: str | None = None,
    duration_ms: int | None = None,
    metadata: dict | None = None,
) -> AgentDelegation:
    """Record a delegation in the unified audit trail.

    For synchronous delegations (consult/transfer), this is called *after*
    the delegation completes with status already set.  For background tasks,
    it's called at creation time with status='pending'.
    """
    delegation = AgentDelegation(
        user_id=uuid.UUID(user_id) if user_id else None,
        parent_agent_id=uuid.UUID(parent_agent_id),
        target_agent_id=uuid.UUID(target_agent_id),
        type=delegation_type,
        conversation_id=uuid.UUID(conversation_id) if conversation_id else None,
        message_id=uuid.UUID(message_id) if message_id else None,
        description=description,
        status=status,
        result=(stored_answer(result) if result else None),
        error=(error[:5_000] if error else None),
        duration_ms=duration_ms,
        metadata_json=metadata or {},
        started_at=(
            datetime.now(UTC) if status in ("running", "completed", "failed") else None
        ),
        completed_at=datetime.now(UTC) if status in ("completed", "failed") else None,
    )
    db.add(delegation)
    await db.flush()
    return delegation


async def update_delegation(
    db: AsyncSession,
    *,
    sub_agent_task_id: str,
    status: str,
    result: str | None = None,
    error: str | None = None,
) -> AgentDelegation | None:
    """Update the lifecycle of a background-task delegation row.

    ``create_sub_agent_task`` logs the delegation with ``status='pending'`` and
    stores the ``SubAgentTask`` id in ``metadata_json['sub_agent_task_id']`` —
    that id is the join key used here to move the row through its lifecycle.
    Valid ``status`` values are the delegation vocabulary only — ``running``,
    ``completed``, ``failed``, or ``incomplete`` (a wait-budget death whose
    work was not finished; timeouts and dead-letters are recorded as
    ``status='failed'`` with the cause in ``error`` — there is no separate
    timeout state).

    Only ACTIVE (pending / running) rows are matched, and the row is locked
    with ``FOR UPDATE`` so the status filter is re-checked on the committed
    row: a concurrent writer (e.g. a dead-letter sweep racing a task body
    that just recorded completion) blocks on the lock and then sees the
    terminal status, so it can never downgrade the row.

    Returns the updated row, or *None* when no matching delegation exists
    (e.g. the original ``log_delegation`` call failed and was swallowed) or
    the delegation is already terminal.
    """
    row = await db.execute(
        select(AgentDelegation)
        .where(
            AgentDelegation.type == "background_task",
            AgentDelegation.status.in_(("pending", "running")),
            AgentDelegation.metadata_json["sub_agent_task_id"].astext
            == str(sub_agent_task_id),
        )
        .order_by(AgentDelegation.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    delegation = row.scalar_one_or_none()
    if delegation is None:
        logger.debug(
            "No background_task delegation found for sub-agent task %s",
            sub_agent_task_id,
        )
        return None

    delegation.status = status
    if delegation.started_at is None and status in ("running", "completed", "failed"):
        delegation.started_at = datetime.now(UTC)
    if status in ("completed", "failed"):
        delegation.completed_at = datetime.now(UTC)
    if result is not None:
        delegation.result = stored_answer(result)
    if error is not None:
        delegation.error = error[:5_000]
    # Scope the flush to the delegation row: callers (task body, dead-letter
    # cleanup) invoke this best-effort helper with unrelated pending changes
    # (SubAgentTask / AssignmentRun) in the same session — an unscoped flush
    # would push those too, so their errors would be swallowed as a
    # "delegation update" failure by the try/except in the caller.
    await db.flush([delegation])
    return delegation


async def list_delegations(
    db: AsyncSession,
    user_id: str | None = None,
    *,
    parent_agent_id: str | None = None,
    target_agent_id: str | None = None,
    agent_id: str | None = None,
    delegation_type: str | None = None,
    status: str | None = None,
    conversation_id: str | None = None,
    any_agent_ids: list[uuid.UUID] | None = None,
    limit: int = 50,
) -> list[dict]:
    """List delegations with optional filters.

    When ``agent_id`` is provided, returns delegations where the agent is
    *either* the parent or the target (full picture for an agent's tab);
    ``any_agent_ids`` does the same for a group of agents (a fleet's
    activity).
    """
    from sqlalchemy import or_

    query = select(AgentDelegation)

    if user_id:
        query = query.where(AgentDelegation.user_id == uuid.UUID(user_id))
    if agent_id:
        aid = uuid.UUID(agent_id)
        query = query.where(
            or_(
                AgentDelegation.parent_agent_id == aid,
                AgentDelegation.target_agent_id == aid,
            )
        )
    if any_agent_ids is not None:
        query = query.where(
            or_(
                AgentDelegation.parent_agent_id.in_(any_agent_ids),
                AgentDelegation.target_agent_id.in_(any_agent_ids),
            )
        )
    if parent_agent_id:
        query = query.where(
            AgentDelegation.parent_agent_id == uuid.UUID(parent_agent_id)
        )
    if target_agent_id:
        query = query.where(
            AgentDelegation.target_agent_id == uuid.UUID(target_agent_id)
        )
    if delegation_type:
        query = query.where(AgentDelegation.type == delegation_type)
    if status:
        query = query.where(AgentDelegation.status == status)
    if conversation_id:
        query = query.where(
            AgentDelegation.conversation_id == uuid.UUID(conversation_id)
        )

    query = query.order_by(AgentDelegation.created_at.desc()).limit(limit)
    result = await db.execute(query)
    delegations = result.scalars().all()

    return [_serialize(d) for d in delegations]


async def get_delegation(
    db: AsyncSession,
    delegation_id: str,
    user_id: str | None = None,
) -> dict | None:
    """Get a single delegation by ID."""
    query = select(AgentDelegation).where(
        AgentDelegation.id == uuid.UUID(delegation_id)
    )
    if user_id:
        query = query.where(AgentDelegation.user_id == uuid.UUID(user_id))
    result = await db.execute(query)
    d = result.scalar_one_or_none()
    return _serialize(d) if d else None


def _serialize(d: AgentDelegation) -> dict:
    """Serialize a delegation record. ``outcome`` is
    :func:`app.services.run_outcome.run_outcome`, the rule every surface that
    shows a run status reads: ``interrupted`` for one the platform ended."""
    from app.services.run_outcome import run_outcome

    return {
        "id": str(d.id),
        "user_id": str(d.user_id) if d.user_id else None,
        "parent_agent_id": str(d.parent_agent_id),
        "target_agent_id": str(d.target_agent_id),
        "type": d.type,
        "conversation_id": str(d.conversation_id) if d.conversation_id else None,
        "message_id": str(d.message_id) if d.message_id else None,
        "description": d.description,
        "status": d.status,
        "result": d.result,
        "error": d.error,
        "outcome": run_outcome(d.status, d.error),
        "duration_ms": d.duration_ms,
        "metadata": d.metadata_json,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "started_at": d.started_at.isoformat() if d.started_at else None,
        "completed_at": d.completed_at.isoformat() if d.completed_at else None,
        "has_result": d.result is not None,
    }
