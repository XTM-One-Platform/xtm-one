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

"""Long waits of a human gate (#842, S7; human-gates.md section 7, F3).

A run paused for a person stays resumable as it always was, but:

- a run paused on a tool approval never holds its assignment (#1647): the
  gate asks before the tool runs, so later triggers run at once;
- past ``HUMAN_GATE_QUEUE_RELEASE_HOURS`` (default 4 h) of waiting, a run
  that a schedule, an event or a delivery started stops holding its
  assignment: :func:`holds_assignment` leaves it out of the overlap guard and
  the delivery queue's slots, so later triggers run. A chat-started run
  (``CHAT_STARTED_TRIGGER_TYPES``) keeps holding: a person is in the loop.
  The wait is measured from the run's open ``decisions`` row, written in the
  transaction that paused it (section 8, G2), so no column is added;
- a decision past its own deadline expires (:func:`expire_overdue_decisions`,
  on the autonomous tick): the ``decisions`` row becomes ``expired`` with an
  ``expired`` event, and the run resumes with the default option (the ask's
  recommendation) when it names one, else with "no answer" as its tool
  result. A tool approval past its deadline resumes with every call rejected.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, and_, exists, not_, or_, select

from app.core.config import settings
from app.core.utils import format_exception_message
from app.models.decision import Decision, DecisionEvent

logger = logging.getLogger(__name__)

#: ``trigger_context["type"]`` of a run a person started from a chat: its
#: pause never releases the assignment (section 7: "off for chat-started runs").
CHAT_STARTED_TRIGGER_TYPES: frozenset[str] = frozenset({"chat_handover"})
#: Decision kinds whose pause releases its assignment at once (not after
#: ``release_hours``): a tool approval stops the loop before the tool runs.
RELEASED_AT_ONCE_KINDS: frozenset[str] = frozenset({"tool_approval"})
#: What a run reads when its decision expires with no default option.
NO_ANSWER = "No answer before the deadline"
#: The surface an expiry is recorded under.
EXPIRY_SURFACE = "deadline"
#: Decisions expired per tick; the rest follow on the next ticks.
EXPIRY_BATCH = 50


def release_hours() -> float:
    """Hours after which a paused run stops holding its assignment; 0 or less turns it off."""
    try:
        return float(settings.human_gate_queue_release_hours or 0)
    except (TypeError, ValueError):
        return 0.0


def released_wait_clause(model: Any, now: datetime | None = None) -> ColumnElement[bool]:
    """SQL: a run of *model* (``AssignmentRun`` or an alias) whose pause no longer holds its assignment.

    A pause on a tool approval (:data:`RELEASED_AT_ONCE_KINDS`) never holds:
    the gate asks before the tool runs, so nothing is half written, and the
    answer resumes the run as before. Any other pause holds until
    :func:`release_hours`. A chat-started run always holds.
    """
    hours = release_hours()
    run_type = model.trigger_context["type"].as_string()
    open_decision = and_(
        Decision.source_type == "run",
        Decision.source_id == model.id,
        Decision.status == "open",
    )
    released: list[ColumnElement[bool]] = [Decision.kind.in_(sorted(RELEASED_AT_ONCE_KINDS))]
    if hours > 0:
        released.append(Decision.created_at <= (now or datetime.now(UTC)) - timedelta(hours=hours))
    return and_(
        model.status == "awaiting_human",
        or_(
            model.trigger_context.is_(None),
            run_type.is_(None),
            run_type.not_in(sorted(CHAT_STARTED_TRIGGER_TYPES)),
        ),
        exists(select(Decision.id).where(open_decision, or_(*released))),
    )


def holds_assignment(model: Any, statuses: Any, now: datetime | None = None) -> ColumnElement[bool]:
    """SQL: a run of *model* in one of *statuses* that still holds its assignment (F3)."""
    return and_(model.status.in_(list(statuses)), not_(released_wait_clause(model, now)))


def expiry_outcome(ask: Any, *, approval: bool = False) -> dict[str, Any]:
    """What an expired decision answers: the default option when the ask names one, else no answer.

    ``response`` is what the run reads (an option key resumes as that option,
    ``decision_ask.run_answer_text``); the rest is stored on the decision.
    """
    if approval:
        return {"option": "reject", "text": NO_ANSWER, "response": NO_ANSWER}
    ask = ask if isinstance(ask, dict) else {}
    recommendation = str(ask.get("recommendation") or "").strip()
    options = ask.get("options") if isinstance(ask.get("options"), list) else []
    for option in options:
        if not isinstance(option, dict):
            continue
        key = str(option.get("key") or "").strip()
        if key and recommendation and key.casefold() == recommendation.casefold():
            return {"option": key, "label": str(option.get("label") or key), "response": key}
    return {"text": NO_ANSWER, "response": NO_ANSWER}


def _expired_history_entry(run: Any, outcome: dict[str, Any], now: datetime) -> dict[str, Any]:
    return {
        "question": run.human_input_question,
        "response": outcome.get("label") or outcome.get("text"),
        "resolution": "expired",
        "responded_at": now.isoformat(),
        "responded_by": "Deadline",
        "surface": EXPIRY_SURFACE,
    }


async def _expire_one(decision_id: uuid.UUID, now: datetime) -> bool:
    from app.core.database import async_session
    from app.models.assignment import AssignmentRun
    from app.services.assignment_engine import resume_assignment_run

    async with async_session() as db:
        decision = (
            await db.execute(select(Decision).where(Decision.id == decision_id).with_for_update(skip_locked=True))
        ).scalar_one_or_none()
        if decision is None or decision.status != "open" or decision.deadline is None or decision.deadline > now:
            return False
        run = (
            await db.execute(select(AssignmentRun).where(AssignmentRun.id == decision.source_id).with_for_update())
        ).scalar_one_or_none()
        approval = bool(run is not None and run.pending_approvals)
        outcome = expiry_outcome(decision.ask, approval=approval)
        stored = {k: v for k, v in outcome.items() if k != "response"}
        # Closed first: the dual-write hook then finds no open row to answer.
        decision.status = "expired"
        decision.answer = stored
        decision.surface = EXPIRY_SURFACE
        db.add(DecisionEvent(decision_id=decision.id, event="expired", surface=EXPIRY_SURFACE, detail=stored))
        await db.flush()
        if run is None or run.status != "awaiting_human":
            await db.commit()
            return True
        if approval:
            from app.services.chat_approval_resume import record_approval_decision
            from app.tools.approval import DECISION_SOURCE_DEADLINE, DECISION_SOURCE_KEY

            # Named for what refused it: the run reads "nobody answered", never
            # "Rejected by human reviewer", and the audit row is the platform's.
            run.approval_decisions = {
                str(p.get("tool_call_id")): {
                    "decision": "reject",
                    "rejection_reason": NO_ANSWER,
                    DECISION_SOURCE_KEY: DECISION_SOURCE_DEADLINE,
                }
                for p in run.pending_approvals or []
                if isinstance(p, dict) and p.get("tool_call_id")
            }
            await record_approval_decision(
                db, run=run, user=None, surface=EXPIRY_SURFACE, system_actor="decision_deadline"
            )
        else:
            run.human_input_response = outcome["response"]
        entry = _expired_history_entry(run, outcome, now)
        if approval:
            from app.tools.approval import OUTCOME_EXPIRED

            entry["gate_outcomes"] = [
                {"tool_call_id": p.get("tool_call_id"), "tool_name": p.get("tool_name"), "outcome": OUTCOME_EXPIRED}
                for p in run.pending_approvals or []
                if isinstance(p, dict)
            ]
        run.human_input_history = [*(run.human_input_history or []), entry]
        await resume_assignment_run(db, run)
        run_id = run.id
    from app.services.push_action_tokens import void_run_tokens

    await void_run_tokens(run_id)
    return True


async def expire_overdue_decisions(*, now: datetime | None = None, limit: int = EXPIRY_BATCH) -> int:
    """Expire the open run decisions past their deadline and resume their runs; how many expired."""
    from app.core.database import async_session

    now = now or datetime.now(UTC)
    async with async_session() as db:
        ids = (
            (
                await db.execute(
                    select(Decision.id)
                    .where(
                        Decision.status == "open",
                        Decision.source_type == "run",
                        Decision.deadline.is_not(None),
                        Decision.deadline <= now,
                    )
                    .order_by(Decision.deadline)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    expired = 0
    for decision_id in ids:
        try:
            if await _expire_one(decision_id, now):
                expired += 1
        except Exception as exc:
            logger.warning("Decision %s not expired: %s", decision_id, format_exception_message(exc))
    return expired
