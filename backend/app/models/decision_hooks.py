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

"""The dual write of the decisions table (#842, S2; human-gates.md section 8).

Every engine opens and closes its pause on its own columns, from many write
paths (a work item parks for a person from the agent tools, the platform's
own parks, the API...). Rather than a call in each of them, one ``before_flush``
hook reads the change each flush carries on the three source models and writes
the ``decisions`` row and its ``decision_events`` line in the SAME transaction:

- an :class:`~app.models.assignment.AssignmentRun` entering ``awaiting_human``
  opens a ``hitl`` (or ``tool_approval``) decision; an answer stored on it
  (``human_input_response`` or ``approval_decisions``) answers it; leaving the
  status unanswered cancels it;
- a :class:`~app.models.flow_run.FlowRun` whose ``state.waiting`` becomes an
  unreleased ``human`` park opens a ``flow_ask`` decision; the release answers
  it; the park gone unreleased cancels it;
- a :class:`~app.models.work_item.WorkItem` whose ``needs_human`` turns on
  opens a ``work_item`` decision; turning off answers it when the flush stores
  ``payload.answer``, else cancels it.

The engines still resume from their own columns. The transition rules are
pure functions (``*_transition``) so each is unit-tested without a database.
"""

from __future__ import annotations

import contextvars
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import event, inspect as sa_inspect, update
from sqlalchemy.orm import Session

from app.models.decision import Decision, DecisionEvent

logger = logging.getLogger(__name__)

#: The surface an answer came from, set by the caller that answers
#: (the router sets ``router``, or the surface its body names); read by the
#: hook when it writes the ``answered`` event.
answer_surface: contextvars.ContextVar[str | None] = contextvars.ContextVar("decision_answer_surface", default=None)

#: Tool approvals are decided with these options (human-gates.md section 2).
APPROVAL_OPTIONS: tuple[dict[str, str], ...] = (
    {"key": "approve", "label": "Approve"},
    {"key": "approve_always", "label": "Approve always"},
    {"key": "reject", "label": "Reject"},
)

# A gate transition: ("open", kind, ask, recipient) | ("answer", answer, actor) | ("cancel", reason) | None.
Transition = tuple[Any, ...] | None


def _old_new(obj: Any, key: str) -> tuple[Any, Any, bool]:
    """(value before this flush, value now, changed) of one column."""
    state = sa_inspect(obj)
    history = state.attrs[key].history
    now = state.dict.get(key)
    if state.pending:
        return None, now, now is not None
    if not history.has_changes():
        return now, now, False
    old = history.deleted[0] if history.deleted else None
    return old, now, True


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


def owner_recipient(user_id: Any, *, company_managed: bool = False, company: str = "admins") -> str:
    """The one recipient rule's default (section 6): the source's owner, else *company*."""
    if company_managed or not user_id:
        return company
    return f"user:{user_id}"


def run_ask(question: str | None, messages: Any, pending_approvals: Any) -> tuple[str, dict[str, Any]]:
    """The kind and the ask of a run's pause, from what the engine stored."""
    text = (question or "").strip() or "A person's input is needed"
    if pending_approvals:
        return "tool_approval", {"question": text, "options": [dict(o) for o in APPROVAL_OPTIONS]}
    for message in reversed(messages if isinstance(messages, list) else []):
        calls = message.get("tool_calls") if isinstance(message, dict) else None
        for call in calls or []:
            if isinstance(call, dict) and call.get("name") == "request_human_input":
                args = call.get("arguments")
                if isinstance(args, dict) and args.get("question"):
                    from app.services.decision_ask import normalize_run_ask

                    try:
                        return "hitl", normalize_run_ask(
                            args.get("question"),
                            args.get("context") or "",
                            args.get("options"),
                            args.get("recommendation"),
                            args.get("deadline"),
                            args.get("links"),
                        )
                    except ValueError:
                        break
        if calls:
            break
    return "hitl", {"question": text}


def run_transition(
    old_status: str | None,
    new_status: str | None,
    *,
    answered: bool,
    answer: dict[str, Any] | None = None,
    actor: Any = None,
) -> Transition:
    """What a run's flush does to its decision (pure)."""
    if new_status == "awaiting_human" and old_status != "awaiting_human":
        return ("open",)
    if new_status == "awaiting_human" and answered:
        return ("answer", answer or {}, actor)
    if old_status == "awaiting_human" and new_status != "awaiting_human":
        return ("answer", answer or {}, actor) if answered else ("cancel", f"run {new_status}")
    return None


def flow_transition(old_state: Any, new_state: Any) -> Transition:
    """What a flow run's flush does to its Ask-user decision (pure)."""
    old = (old_state or {}).get("waiting") if isinstance(old_state, dict) else None
    new = (new_state or {}).get("waiting") if isinstance(new_state, dict) else None
    old_open = isinstance(old, dict) and old.get("kind") == "human" and not old.get("released")
    new_human = isinstance(new, dict) and new.get("kind") == "human"
    if new_human and not new.get("released"):
        same = old_open and (old.get("step_id"), old.get("node")) == (new.get("step_id"), new.get("node"))
        return None if same else ("open",)
    if new_human and new.get("released") and old_open:
        return ("answer", {"text": new.get("response")}, new.get("responded_by"))
    if old_open:
        return ("cancel", "flow run moved on")
    return None


def work_item_transition(old_needs: Any, new_needs: Any, *, answer: dict[str, Any] | None) -> Transition:
    """What a work item's flush does to its decision (pure)."""
    if new_needs and not old_needs:
        return ("open",)
    if old_needs and not new_needs:
        if answer:
            kept = {k: answer[k] for k in ("option", "label", "text") if answer.get(k)}
            return ("answer", kept, None)
        return ("cancel", "no longer waiting for a person")
    return None


_HAS_TABLE: dict[int, bool] = {}


def _store_ready(session: Session) -> bool:
    """The table exists on this connection's database (a scratch test schema may lack it)."""
    bind = session.get_bind()
    key = id(bind)
    if key not in _HAS_TABLE:
        _HAS_TABLE[key] = sa_inspect(session.connection()).has_table(Decision.__tablename__)
    return _HAS_TABLE[key]


def _close_open(session: Session, source_type: str, source_id: uuid.UUID, values: dict[str, Any]) -> list[uuid.UUID]:
    table = Decision.__table__
    result = session.connection().execute(
        update(table)
        .where(table.c.source_type == source_type, table.c.source_id == source_id, table.c.status == "open")
        .values(**values, updated_at=datetime.now(UTC))
        .returning(table.c.id)
    )
    return [row[0] for row in result]


def _apply(session: Session, source_type: str, source_id: Any, transition: Transition, opener: Any) -> None:
    if transition is None or source_id is None or not _store_ready(session):
        return
    sid = source_id if isinstance(source_id, uuid.UUID) else uuid.UUID(str(source_id))
    now = datetime.now(UTC)
    surface = answer_surface.get()
    if transition[0] == "open":
        for closed in _close_open(session, source_type, sid, {"status": "cancelled"}):
            session.add(DecisionEvent(decision_id=closed, event="cancelled", detail={"reason": "asked again"}))
        kind, ask, recipient, deadline = opener()
        decision = Decision(
            id=uuid.uuid4(),
            source_type=source_type,
            source_id=sid,
            kind=kind,
            ask=ask,
            recipient=recipient,
            status="open",
            deadline=deadline,
            created_at=now,
            updated_at=now,
        )
        session.add(decision)
        session.add(DecisionEvent(decision_id=decision.id, event="asked", detail={"kind": kind}))
        return
    if transition[0] == "answer":
        _, answer, actor = transition
        actor_id = _uuid_or_none(actor)
        values: dict[str, Any] = {"status": "answered", "answer": answer, "answered_at": now, "surface": surface}
        if actor_id is not None:
            values["answered_by"] = actor_id
        for closed in _close_open(session, source_type, sid, values):
            session.add(
                DecisionEvent(decision_id=closed, event="answered", actor_id=actor_id, surface=surface, detail=answer)
            )
        return
    for closed in _close_open(session, source_type, sid, {"status": "cancelled"}):
        session.add(DecisionEvent(decision_id=closed, event="cancelled", detail={"reason": transition[1]}))


def _deadline(ask: dict[str, Any]) -> datetime | None:
    raw = ask.get("deadline") if isinstance(ask, dict) else None
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _sync_run(session: Session, run: Any) -> None:
    old_status, new_status, changed = _old_new(run, "status")
    _, response, response_changed = _old_new(run, "human_input_response")
    _, decisions, decisions_changed = _old_new(run, "approval_decisions")
    answered = bool((response_changed and response) or (decisions_changed and decisions))
    if not (changed or answered):
        return
    answer: dict[str, Any] = {}
    actor = None
    history = run.human_input_history or []
    last = history[-1] if history and isinstance(history[-1], dict) else {}
    if answered or (old_status == "awaiting_human" and last.get("resolution") == "answered"):
        answered = True
        actor = last.get("responded_by_id")
        if decisions:
            answer = {"decisions": {k: (v or {}).get("decision") for k, v in dict(decisions).items()}}
        elif response:
            answer = {"text": response}
        elif last.get("response"):
            answer = {"text": last["response"]}
    transition = run_transition(old_status, new_status, answered=answered, answer=answer, actor=actor)

    def opener() -> tuple[str, dict[str, Any], str, datetime | None]:
        kind, ask = run_ask(run.human_input_question, run.messages, run.pending_approvals)
        recipient = owner_recipient(run.user_id, company_managed=bool(run.is_company_managed))
        return kind, ask, recipient, _deadline(ask)

    _apply(session, "run", run.id, transition, opener)


def _sync_flow_run(session: Session, run: Any) -> None:
    old_state, new_state, changed = _old_new(run, "state")
    if not changed:
        return
    transition = flow_transition(old_state, new_state)

    def opener() -> tuple[str, dict[str, Any], str, datetime | None]:
        waiting = (new_state or {}).get("waiting") or {}
        ask = {"question": str(waiting.get("question") or "A person's answer is needed")}
        # A company flow asks the company (section 6: an explicit recipient, not a router exception).
        recipient = owner_recipient(run.user_id, company_managed=bool(run.is_company_managed), company="company")
        return "flow_ask", ask, recipient, None

    _apply(session, "flow_run", run.id, transition, opener)


def _sync_work_item(session: Session, item: Any) -> None:
    old_needs, new_needs, changed = _old_new(item, "needs_human")
    if not changed:
        return
    payload = item.payload if isinstance(item.payload, dict) else {}
    transition = work_item_transition(old_needs, new_needs, answer=payload.get("answer") if not new_needs else None)
    if transition and transition[0] == "answer":
        answer = payload.get("answer") or {}
        user_id = getattr(item, "_decision_answered_by_id", None)
        transition = ("answer", transition[1], user_id or answer.get("answered_by_id"))

    def opener() -> tuple[str, dict[str, Any], str, datetime | None]:
        ask = dict(item.ask) if isinstance(item.ask, dict) and item.ask.get("question") else {}
        if not ask:
            ask = {"question": str(item.blocked_reason or item.title or "A person's answer is needed")}
        recipient = owner_recipient(item.waits_for_user_id or item.user_id)
        return "work_item", ask, recipient, _deadline(ask)

    _apply(session, "work_item", item.id, transition, opener)


@event.listens_for(Session, "before_flush")
def _write_decisions(session: Session, flush_context: Any, instances: Any) -> None:
    from app.models.assignment import AssignmentRun
    from app.models.flow_run import FlowRun
    from app.models.work_item import WorkItem

    for obj in (*session.new, *session.dirty):
        if isinstance(obj, AssignmentRun):
            _sync_run(session, obj)
        elif isinstance(obj, FlowRun):
            _sync_flow_run(session, obj)
        elif isinstance(obj, WorkItem):
            _sync_work_item(session, obj)
