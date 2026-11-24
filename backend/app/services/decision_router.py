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

"""One answer path for every human gate (#842, S2; human-gates.md section 6, E).

``POST /decisions/{id}/answer`` takes a decision id (``run:<uuid>``,
``flow_run:<uuid>``, ``work_item:<uuid>``, or the ``decisions`` row id),
resolves its source and hands the answer to the engine's own answer function,
the same one the old endpoints call:

- a run paused by ``request_human_input``: ``answer_paused_run`` with the
  option key (or its label) or the text as the response;
- a tool approval: ``answer_paused_run`` with one decision per pending call;
- a flow Ask-user park: ``answer_flow_park``;
- a work item waiting for a person: ``work_item.answer_work_item``.

The recipient rule is the engine's (the run's access rule, the flow's
company rule, the work item's ``recipient_clause``), so a person refused by
the old endpoint is refused here too; every refusal is written to
``decision_events`` (``refused``) when the decision row exists.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.decision import SOURCE_TYPES, Decision, DecisionEvent
from app.models.decision_hooks import answer_surface

#: The surfaces an answer may name; anything else is recorded as ``router``.
SURFACES: frozenset[str] = frozenset(
    {"router", "web", "inbox", "bell", "push", "mail"}
    # The in-place cards of the bots (S9, S10, S11, S12).
    | {"teams_card", "slack_card", "whatsapp_card", "google_chat_card"}
    # The channels an answer link (S9b, ``decision_answer_links``) is sent on.
    | {"teams_chat", "teams_channel", "google_chat", "email"}
)


@dataclass(frozen=True)
class DecisionRef:
    source_type: str
    source_id: uuid.UUID


def parse_decision_id(raw: str) -> DecisionRef | uuid.UUID:
    """``run:<uuid>`` and the like into a source; a bare uuid is a decisions row id."""
    value = (raw or "").strip()
    kind, sep, rest = value.partition(":")
    try:
        if sep:
            if kind not in SOURCE_TYPES:
                raise ValueError(kind)
            return DecisionRef(kind, uuid.UUID(rest))
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=404, detail="Decision not found") from None


async def resolve(db: AsyncSession, raw: str) -> tuple[DecisionRef, Decision | None]:
    """The source a decision id names, and its open (else latest) decisions row."""
    parsed = parse_decision_id(raw)
    if isinstance(parsed, uuid.UUID):
        row = await db.get(Decision, parsed)
        if row is None:
            raise HTTPException(status_code=404, detail="Decision not found")
        return DecisionRef(row.source_type, row.source_id), row
    row = (
        await db.execute(
            select(Decision)
            .where(Decision.source_type == parsed.source_type, Decision.source_id == parsed.source_id)
            .order_by((Decision.status == "open").desc(), Decision.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return parsed, row


def run_response(ask: dict[str, Any] | None, option: str | None, text: str | None) -> str:
    """The free-text response a run reads: the option's key (the resume spells it out), else the text."""
    key = (option or "").strip()
    if key:
        options = (ask or {}).get("options") or []
        chosen = next((o for o in options if str(o.get("key", "")).casefold() == key.casefold()), None)
        if options and chosen is None:
            raise HTTPException(status_code=400, detail=f"option {key!r} is not one of the ask's options")
        key = str(chosen["key"]) if chosen else key
        return f"{key}: {text.strip()}" if text and text.strip() else key
    return (text or "").strip()


async def _record_refusal(db: AsyncSession, row: Decision | None, user: Any, surface: str, exc: HTTPException) -> None:
    if row is None:
        return
    # The rollback expires every instance of the session (the row, and the user
    # when this request loaded it): read their ids first, a lazy refresh on an
    # AsyncSession raises MissingGreenlet.
    decision_id = row.id
    actor_id = getattr(user, "id", None)
    await db.rollback()
    db.add(
        DecisionEvent(
            decision_id=decision_id,
            event="refused",
            actor_id=actor_id,
            surface=surface,
            detail={"status": exc.status_code, "reason": str(exc.detail)[:300]},
        )
    )
    await db.commit()


async def _dispatch(
    db: AsyncSession,
    ref: DecisionRef,
    row: Decision | None,
    user: Any,
    *,
    option: str | None,
    text: str | None,
    decisions: list[Any] | None,
    surface: str,
) -> dict[str, Any]:
    if ref.source_type == "run":
        from app.api.v1.assignments import answer_paused_run
        from app.api.v1.schemas.assignment import ApprovalDecision, RespondToHumanInputRequest
        from app.models.assignment import AssignmentRun

        run = await db.get(AssignmentRun, ref.source_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Decision not found")
        if run.pending_approvals:
            if decisions is None:
                verdicts: dict[str, Literal["approve", "approve_always", "reject"]] = {
                    "approve": "approve",
                    "approve_always": "approve_always",
                    "reject": "reject",
                }
                verdict = verdicts.get((option or "").strip())
                if verdict is None:
                    raise HTTPException(
                        status_code=400,
                        detail="A tool approval is answered with approve, approve_always or reject.",
                    )
                decisions = [
                    ApprovalDecision(
                        tool_call_id=p.get("tool_call_id"),
                        decision=verdict,
                        rejection_reason=(text or None) if verdict == "reject" else None,
                    )
                    for p in run.pending_approvals
                ]
            body = RespondToHumanInputRequest(decisions=decisions)
        else:
            body = RespondToHumanInputRequest(response=run_response(row.ask if row else None, option, text))
        run = await answer_paused_run(db, ref.source_id, user, body, surface="decision_router", answered_from=surface)
        return {"source_type": "run", "source_id": str(run.id), "status": run.status}
    if ref.source_type == "flow_run":
        from app.api.v1.flow_runs import answer_flow_park

        # The option's key, with the note after it when one is typed (#842, S6):
        # answer_flow_park reads the key back as {{ask.option}}.
        response = run_response(row.ask if row else None, option, text)
        flow_run = await answer_flow_park(db, user, str(ref.source_id), response)
        return {"source_type": "flow_run", "source_id": str(flow_run.id), "status": flow_run.status}
    from app.services import work_item as work_item_service
    from app.services.company_managed_writes import CompanyManagedWriteRefusedError, person_writer
    from app.services.scheduled_work_items import ScheduledWorkItemError

    try:
        item = await work_item_service.answer_work_item(
            str(ref.source_id),
            user_id=str(user.id),
            option=option,
            text=text,
            answered_by=getattr(user, "full_name", None) or getattr(user, "email", None),
            writer=person_writer(is_admin=bool(getattr(user, "is_admin", False))),
        )
    except (CompanyManagedWriteRefusedError, ScheduledWorkItemError) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except work_item_service.WorkItemNotWaitingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    return {"source_type": "work_item", "source_id": str(ref.source_id), "status": item.get("status")}


async def answer_decision(
    db: AsyncSession,
    raw_id: str,
    user: Any,
    *,
    option: str | None = None,
    text: str | None = None,
    decisions: list[Any] | None = None,
    surface: str | None = None,
) -> dict[str, Any]:
    """Answer the decision *raw_id* names, as *user*, through its engine."""
    if not (option or (text and text.strip()) or decisions):
        raise HTTPException(status_code=400, detail="An answer needs an option, a text or decisions.")
    where = surface if surface in SURFACES else "router"
    ref, row = await resolve(db, raw_id)
    token = answer_surface.set(where)
    try:
        result = await _dispatch(db, ref, row, user, option=option, text=text, decisions=decisions, surface=where)
    except HTTPException as exc:
        await _record_refusal(db, row, user, where, exc)
        raise
    finally:
        answer_surface.reset(token)
    if row is not None:
        # The link cards a Microsoft account sent for it show the decided state (S9b, I.4 (b), (c)),
        # and so do the cards the Google Chat app posted (S11, I.4 (d)).
        from app.services import google_chat_decision_cards as chat_cards
        from app.services.decision_graph_cards import has_sent_cards

        if await has_sent_cards(db, row.id) or await chat_cards.has_sent_cards(db, row.id):
            schedule_card_refresh(row.id)
    from app.models.decision import public_id

    return {"id": public_id(ref.source_type, ref.source_id), **result}


#: The refreshes in flight: a reference keeps each task alive until it ends.
_REFRESHES: set[asyncio.Task[None]] = set()


def schedule_card_refresh(decision_id: uuid.UUID) -> None:
    """Update the sent link cards of *decision_id* in the background; the answer never waits for Graph."""
    from app.services.decision_graph_cards import refresh_after_answer

    task = asyncio.get_running_loop().create_task(refresh_after_answer(decision_id))
    _REFRESHES.add(task)
    task.add_done_callback(_REFRESHES.discard)
