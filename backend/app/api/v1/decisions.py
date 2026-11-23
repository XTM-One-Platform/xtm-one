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

``GET /decisions/links/{token}`` and ``POST /decisions/links/{token}/answer``
serve the signed answer link of S9b (``/decide/<token>``, human-gates.md I.4):
the page reads the decision with the recipient's session, and the answer
goes through the same router, once, under the decision's row lock.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin, require_agentic_ai
from app.api.v1.schemas.assignment import ApprovalDecision
from app.core.database import get_db
from app.models.decision import Decision, DecisionEvent
from app.models.user import User
from app.services import decision_answer_links as answer_links
from app.services.decision_links import decision_link
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


class LinkAnswer(BaseModel):
    #: The free answer of a ``text`` link; with an option link, what goes with it.
    text: Annotated[str | None, Field(max_length=8000)] = None
    #: An option picked on the ``text`` link's page (the shared decision card shows them all);
    #: an option link answers its signed option only.
    option: Annotated[str | None, Field(max_length=200)] = None


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
    fleet: Annotated[
        str | None,
        Query(
            description=(
                "Only the decisions this fleet raised (runs and work items of its current home members, #1493). "
                "A fleet the caller cannot read is a 404; a manager of the fleet also sees the decisions "
                "addressed to someone else, marked can_answer false."
            )
        ),
    ] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The "Waiting for you" inbox (S5): the caller's open decisions across every source.

    ``enabled`` is false, with its reason, while the read switch is off; the
    readers then keep their per-engine views.
    """
    from app.services.decision_inbox import waiting_for

    if fleet is None:
        return await waiting_for(db, user, limit=limit)
    from app.api.v1.fleets import _visible_fleet
    from app.api.v1.helpers import can_manage_entity

    fleet_row = await _visible_fleet(db, user, fleet)
    manages = await can_manage_entity(db, user, fleet_row)
    return await waiting_for(db, user, limit=limit, fleet_id=fleet_row.id, every_recipient=manages)


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


async def _answered_by_name(db: AsyncSession, row: Decision) -> str | None:
    if row.answered_by is None:
        return None
    person = await db.get(User, row.answered_by)
    return (getattr(person, "full_name", None) or getattr(person, "email", None)) if person else None


async def _link_view(db: AsyncSession, link: answer_links.AnswerLink, row: Decision) -> dict[str, Any]:
    ask = row.ask or {}
    return {
        "decision_id": row.public_id,
        "status": row.status,
        "option": link.option,
        "option_label": None
        if link.option == answer_links.TEXT_OPTION
        else answer_links.option_label(ask, link.option),
        "ask": ask,
        "deadline": row.deadline.isoformat() if row.deadline else None,
        "source_link": decision_link(row.source_type, row.source_id),
        "decided": None if row.status == "open" else answer_links.decided_state(row, await _answered_by_name(db, row)),
    }


async def _open_link(db: AsyncSession, token: str, user: User) -> tuple[Any, Decision]:
    link = answer_links.read_token(token)
    row = (await db.execute(select(Decision).where(Decision.id == link.decision_id))).scalar_one_or_none()
    try:
        answer_links.check_reader(link, user)
    except HTTPException as exc:
        # A forwarded link answers nothing, and the audit trail says who tried (I.4.1).
        if row is not None:
            db.add(
                DecisionEvent(
                    decision_id=row.id,
                    event="refused",
                    actor_id=getattr(user, "id", None),
                    surface=link.surface,
                    detail={"status": exc.status_code, "reason": "answer link opened by someone else"},
                )
            )
            await db.commit()
        raise
    if row is None:
        raise HTTPException(status_code=404, detail="This decision no longer exists.")
    return link, row


@router.get("/links/{token}")
async def read_answer_link(
    token: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """What an answer link opens: the decision, and its decided state once answered. Never answers."""
    link, row = await _open_link(db, token, user)
    return await _link_view(db, link, row)


@router.post("/links/{token}/answer")
async def answer_by_link(
    token: str,
    body: LinkAnswer,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
) -> dict[str, Any]:
    """Answer with the link's option as its recipient; a decision no longer open answers nothing.

    The lock is the engine's own (the run, flow run or work item row the
    answer function locks, then the dual write closes the one open decision
    in that transaction): two clicks race on it, the second finds the gate no
    longer waiting (409) and gets the decided state. Holding a lock on the
    decisions row here would deadlock the engines that answer on their own
    session (``answer_work_item``).
    """
    link, row = await _open_link(db, token, user)
    decision_id = row.id
    if row.status == "open":
        if link.option == answer_links.TEXT_OPTION:
            option = (body.option or "").strip() or None
        elif body.option and body.option.casefold() != link.option.casefold():
            raise HTTPException(status_code=400, detail="This link answers one option only.")
        else:
            option = link.option
        text = (body.text or "").strip() or None
        if option is None and not text:
            raise HTTPException(status_code=400, detail="Write an answer first.")
        try:
            await answer_decision(db, row.public_id, user, option=option, text=text, surface=link.surface)
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
        else:
            # The engines commit their own answer; anything left on this session lands before the response.
            await db.commit()
            db.expire_all()
            fresh = await db.get(Decision, decision_id)
            if fresh is not None and fresh.status != "open":
                return {**await _link_view(db, link, fresh), "answered_now": True}
    # Single use: the second click (or a click after another surface answered) shows the decided state.
    await db.commit()
    db.expire_all()
    fresh = await db.get(Decision, decision_id)
    if fresh is None:
        raise HTTPException(status_code=404, detail="This decision no longer exists.")
    return {**await _link_view(db, link, fresh), "answered_now": False}
