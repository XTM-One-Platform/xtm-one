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

"""``POST /decisions/{id}/answer``: one answer path for every human gate (#842, S2).

``GET /decisions/waiting``: the "Waiting for you" inbox read (S5).

The id is ``run:<uuid>``, ``flow_run:<uuid>``, ``work_item:<uuid>`` or a
``decisions`` row id; the answer goes to the engine's own answer function
(:mod:`app.services.decision_router`), so the old endpoints and this one apply
the same recipient rule, refusals and audit.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin, require_agentic_ai
from app.api.v1.schemas.assignment import ApprovalDecision
from app.core.database import get_db
from app.models.user import User
from app.services.decision_router import answer_decision

router = APIRouter()


class DecisionAnswer(BaseModel):
    #: The key of one of the ask's options (a tool approval: approve, approve_always, reject).
    option: Annotated[str | None, Field(max_length=40)] = None
    #: Free text: the answer itself, or what goes with the option (a rejection reason).
    text: Annotated[str | None, Field(max_length=8000)] = None
    #: A tool approval decided call by call.
    decisions: Annotated[list[ApprovalDecision] | None, Field(max_length=50)] = None
    #: Where the answer was given (web, inbox, bell, push, teams_card...).
    surface: Annotated[str | None, Field(max_length=40)] = None


@router.get("/backfill")
async def backfill_status(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The health probe of the backfill (S2b): the count check per source and the read switch."""
    from app.services.decision_backfill import read_switch

    conn = await db.connection()
    return await conn.run_sync(lambda sync_conn: read_switch(sync_conn))


@router.get("/waiting")
async def waiting(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The "Waiting for you" inbox (S5): the caller's open decisions across every source.

    ``enabled`` is false, with its reason, while the read switch is off; the
    readers then keep their per-engine views.
    """
    from app.services.decision_inbox import waiting_for

    return await waiting_for(db, user, limit=limit)


@router.post("/{decision_id}/answer")
async def answer(
    decision_id: str,
    body: DecisionAnswer,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
) -> dict[str, Any]:
    """Answer the decision as the caller: refused when the caller is not its recipient."""
    return await answer_decision(
        db,
        decision_id,
        user,
        option=body.option,
        text=body.text,
        decisions=body.decisions,
        surface=body.surface,
    )
