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

"""Memory Steward consolidation history and undo (#8258).

Two audiences, one rule each, checked on the rows:

- ``/settings/me/memory/consolidations``: the caller's own lanes (their memory
  with each agent). Nobody else sees them, an administrator included.
- ``/agents/{agent_id}/memory/consolidations``: the agent's own lane, for the
  people who manage the agent (``can_manage``).

Each item names the entries the operation touched with their text, which is
the lane owner's own memory. An undo that cannot be applied answers 409 with
a generic message; the reason stays in the logs.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_ai_enabled
from app.api.v1.helpers import can_manage, get_user_agent, get_user_group_ids, parse_path_id
from app.core.database import get_db
from app.models.agent import Agent
from app.models.agent_memory_entry import AgentMemoryEntry
from app.models.memory_consolidation import MemoryConsolidation
from app.models.user import User
from app.services.concierge.memory_history import (
    ConsolidationNotFoundError,
    UndoRefusedError,
    list_consolidations,
    undo_consolidation,
)
from app.services.memory_holds import RETENTION_DAYS

logger = logging.getLogger(__name__)

me_router = APIRouter()
agent_router = APIRouter()

_NOT_FOUND = "Consolidation not found."
_UNDO_REFUSED = "This consolidation can no longer be undone."


async def _serialize(db: AsyncSession, rows: list[MemoryConsolidation]) -> list[dict]:
    entry_ids = {
        uuid.UUID(entry_id) for row in rows for entry_id in row.before_entry_ids
    } | {row.after_entry_id for row in rows if row.after_entry_id is not None}
    entries = (
        {
            entry.id: entry
            for entry in (
                await db.execute(select(AgentMemoryEntry).where(AgentMemoryEntry.id.in_(entry_ids)))
            ).scalars()
        }
        if entry_ids
        else {}
    )
    agent_ids = {row.agent_id for row in rows}
    names = (
        dict((await db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids)))).all())
        if agent_ids
        else {}
    )
    restorable_after = datetime.now(UTC) - timedelta(days=RETENTION_DAYS)

    def entry(entry_id: uuid.UUID, role: str, row: MemoryConsolidation) -> dict:
        found = entries.get(entry_id)
        # Only an entry of the row's own lane is shown: ids come from the row.
        same_lane = found is not None and found.agent_id == row.agent_id and found.user_id == row.user_id
        return {
            "id": str(entry_id),
            "role": role,
            "content": found.content if same_lane else None,
            "memory_type": found.memory_type if same_lane else None,
            "exists": same_lane,
        }

    items = []
    for row in rows:
        before_role = {"merge": "merged", "remove": "removed", "demote": "demoted", "flag": "flagged"}.get(
            row.operation, "removed"
        )
        after_role = "created" if row.operation == "promote" else "kept"
        items.append(
            {
                "id": str(row.id),
                "agent_id": str(row.agent_id),
                "agent_name": names.get(row.agent_id, "(deleted agent)"),
                "operation": row.operation,
                "reason": row.reason,
                "applied_at": row.applied_at.isoformat(),
                "undone_at": row.undone_at.isoformat() if row.undone_at else None,
                "undoable": row.undone_at is None and row.operation != "flag" and row.applied_at > restorable_after,
                "entries": [entry(uuid.UUID(i), before_role, row) for i in row.before_entry_ids]
                + ([entry(row.after_entry_id, after_role, row)] if row.after_entry_id else []),
            }
        )
    return items


async def _undo(db: AsyncSession, consolidation_id: str, *, actor: User, **lane) -> dict:
    cid = parse_path_id(consolidation_id, not_found_detail=_NOT_FOUND)
    try:
        row = await undo_consolidation(db, cid, actor=actor, **lane)
    except ConsolidationNotFoundError:
        raise HTTPException(status_code=404, detail=_NOT_FOUND) from None
    except UndoRefusedError as e:
        await db.rollback()
        logger.info("Consolidation %s undo refused: %s", cid, e)
        raise HTTPException(status_code=409, detail=_UNDO_REFUSED) from None
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return (await _serialize(db, [row]))[0]


@me_router.get("/me/memory/consolidations")
async def list_my_memory_consolidations(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The consolidations of the caller's own memory lanes, newest first."""
    rows = await list_consolidations(db, user_id=user.id)
    return {"consolidations": await _serialize(db, rows), "retention_days": RETENTION_DAYS}


@me_router.post("/me/memory/consolidations/{consolidation_id}/undo")
async def undo_my_memory_consolidation(
    consolidation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Undo one consolidation of the caller's own lanes."""
    return await _undo(db, consolidation_id, actor=user, user_id=user.id)


async def _managed_agent(db: AsyncSession, user: User, agent_id: str) -> Agent:
    group_ids = await get_user_group_ids(db, user.id)
    agent = await get_user_agent(db, user.id, agent_id, group_ids, is_admin=user.is_admin)
    if not can_manage(user, agent, group_ids):
        raise HTTPException(status_code=403, detail="Only a manager of this agent can see its memory history.")
    return agent


@agent_router.get("/{agent_id}/memory/consolidations")
async def list_agent_memory_consolidations(
    agent_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The consolidations of the agent's own memory lane, newest first."""
    agent = await _managed_agent(db, user, agent_id)
    rows = await list_consolidations(db, agent_id=agent.id, agent_lane_only=True)
    return {"consolidations": await _serialize(db, rows), "retention_days": RETENTION_DAYS}


@agent_router.post("/{agent_id}/memory/consolidations/{consolidation_id}/undo")
async def undo_agent_memory_consolidation(
    agent_id: str,
    consolidation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> dict:
    """Undo one consolidation of the agent's own memory lane. An agent's
    memory is shared AI content: like its other memory writes, frozen while
    AI is off."""
    agent = await _managed_agent(db, user, agent_id)
    return await _undo(db, consolidation_id, actor=user, agent_id=agent.id)
