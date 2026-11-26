# SPDX-License-Identifier: Apache-2.0

"""CTEM decisions of the same kind for the same person, asked once (#2451).

A reconcile burst opens hundreds of cases at once, and the loop files one
``ctem:validation:<case>`` decision per case: one person then holds hundreds
of cards that ask the same thing ("measure this asset for the first time").
This module folds them into one grouped decision per recipient and kind:

- :func:`group_kind`: which kind of decision a case's validation gate is, when
  it may be grouped (``first_measure``: a ``control_decay`` case of an asset
  never measured). A P1 case whose SLA ends within :data:`NEAR_SLA` stays
  individual (:func:`stays_individual`), so its card is never buried in a list.
- :func:`join_group`: called where the decision item is written
  (``work_item.create_work_item``): the case's item joins the open group of its
  recipient and kind (created on first use) and waits on it, blocked, without
  its own card; the group's card lists the cases, its due date is the earliest
  due of the group.
- :func:`fan_out`: called where a person answers (``work_item.answer_work_item``):
  "approve all" and "decline all" answer every case's item, "approve selected"
  answers the cases the answer text names and leaves the rest grouped. Each
  case's item reopens with the answer in ``payload.answer``, so the fleet's
  "Gate answered" listener applies it on the case as for an individual card.

The group's key is ``ctem:group:<kind>:<recipient>``: under ``ctem:`` it is a
CTEM key, but it names no case, so no case page counts it as its gate.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

#: The decision keys that may join a group, each followed by the case id.
GROUPABLE_PREFIXES: tuple[str, ...] = ("ctem:validation:", "ctem:f2:validate:")
#: The key prefix of a grouped decision item.
GROUP_PREFIX = "ctem:group:"
#: The first-measure kind: a control_decay case of an asset with no test evidence yet (#2352).
FIRST_MEASURE = "first_measure"
#: A P1 case whose SLA ends sooner than this keeps its own card.
NEAR_SLA = timedelta(hours=72)
#: The blocked reason of a case's item waiting on its group (read by the stall sweep and the case page).
GROUPED_REASON_LEAD = "Grouped into "
#: The answer options of a group card.
APPROVE_ALL = "approve_all"
APPROVE_SELECTED = "approve_selected"
DECLINE_ALL = "decline_all"
LATER = "later"
#: The option each case's item receives.
CASE_APPROVE = "approve"
CASE_DECLINE = "decline"

_CONTEXT_CHARS = 800
_NUMBERS = re.compile(r"\b(\d+)(?:\s*-\s*(\d+))?\b")

_QUESTIONS: dict[str, str] = {
    FIRST_MEASURE: "Launch the first measurement of {n} asset(s) that have no test evidence yet?",
}
_ASKS: dict[str, str] = {
    FIRST_MEASURE: (
        "Each case asks for a first validation run on one asset (no measurement ever booked in OpenCRQ). "
        "The runs are not read-only."
    ),
}


@dataclass(frozen=True)
class CaseFacts:
    """What grouping reads of a case."""

    case_id: str
    title: str
    trigger_path: str | None
    severity: str | None
    sla_due_at: datetime | None


def case_id_of(correlation_key: object) -> str | None:
    """The case id of a groupable decision key, else ``None`` (pure)."""
    if not isinstance(correlation_key, str):
        return None
    for prefix in GROUPABLE_PREFIXES:
        if correlation_key.startswith(prefix):
            raw = correlation_key[len(prefix) :].strip()
            try:
                return str(uuid.UUID(raw))
            except ValueError:
                return None
    return None


def is_group_key(correlation_key: object) -> bool:
    """Whether *correlation_key* names a grouped decision (pure)."""
    return isinstance(correlation_key, str) and correlation_key.startswith(GROUP_PREFIX)


def group_kind(case: CaseFacts) -> str | None:
    """The kind of group a case's validation decision joins, ``None`` when it stays individual (pure)."""
    if case.trigger_path == "control_decay" and "for the first time" in (case.title or "").lower():
        return FIRST_MEASURE
    return None


def stays_individual(case: CaseFacts, now: datetime) -> bool:
    """A P1 case whose SLA ends within :data:`NEAR_SLA` keeps its own card (pure)."""
    if (case.severity or "").upper() != "P1" or case.sla_due_at is None:
        return False
    due = case.sla_due_at if case.sla_due_at.tzinfo else case.sla_due_at.replace(tzinfo=UTC)
    return due - now <= NEAR_SLA


def group_key(kind: str, recipient: str) -> str:
    """``ctem:group:<kind>:<recipient>`` (pure)."""
    return f"{GROUP_PREFIX}{kind}:{recipient}"


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def earliest_due(members: Sequence[Mapping[str, Any]]) -> datetime | None:
    """The earliest ``due_at`` of the members, ``None`` when none has one (pure)."""
    dues = [d for d in (_parse(m.get("due_at")) for m in members) if d is not None]
    return min(dues) if dues else None


def build_ask(kind: str, members: Sequence[Mapping[str, Any]], *, cases_url: str | None = None) -> dict[str, Any]:
    """The card of a group: the count, the priority mix, the list of cases, the four options (pure)."""
    n = len(members)
    mix: dict[str, int] = {}
    for member in members:
        mix[str(member.get("severity") or "unrated")] = mix.get(str(member.get("severity") or "unrated"), 0) + 1
    mix_text = ", ".join(f"{count} {sev}" for sev, count in sorted(mix.items()))
    lead = f"{n} case(s) ({mix_text}). {_ASKS.get(kind, '')}".strip()
    lines: list[str] = []
    used = len(lead) + 2
    for index, member in enumerate(members):
        line = f"{index + 1}. {member.get('title') or 'case'}".rstrip()
        if used + len(line) + 1 > _CONTEXT_CHARS - 40:
            lines.append(f"- and {n - index} more on the cases list")
            break
        lines.append(line)
        used += len(line) + 1
    ask: dict[str, Any] = {
        "question": _QUESTIONS.get(kind, "Approve the {n} grouped decision(s)?").format(n=n),
        "context": "\n".join([lead, *lines]),
        "options": [
            {"key": APPROVE_ALL, "label": "Approve all", "effect": "Every case of the list moves on."},
            {
                "key": APPROVE_SELECTED,
                "label": "Approve selected",
                "effect": "Name the case numbers of the list in the answer (e.g. 1, 4, 7-9): only those move on, "
                "the rest stay in this decision.",
            },
            {"key": DECLINE_ALL, "label": "Decline all", "effect": "Every case is parked with your reason."},
            {
                "key": LATER,
                "label": "Later",
                "effect": "Nothing runs; the decision keeps waiting.",
                "keeps_waiting": True,
            },
        ],
        "recommendation": APPROVE_ALL,
    }
    if cases_url:
        ask["links"] = [{"label": "Open the cases", "url": cases_url}]
    return ask


def selected_members(members: Sequence[Mapping[str, Any]], text: str | None) -> list[Mapping[str, Any]]:
    """The members whose number on the card (1-based, ranges allowed) the answer text names (pure)."""
    named: set[int] = set()
    for start, end in _NUMBERS.findall(text or ""):
        low, high = int(start), int(end or start)
        if high < low or high - low > len(members):
            continue
        named.update(range(low, high + 1))
    return [m for index, m in enumerate(members, start=1) if index in named]


async def _case_facts(db: AsyncSession, case_id: str) -> CaseFacts | None:
    from app.models.ctem_case import CtemCase

    case = await db.get(CtemCase, uuid.UUID(case_id))
    if case is None:
        return None
    return CaseFacts(
        case_id=case_id,
        title=str(case.title or ""),
        trigger_path=getattr(case, "trigger_path", None),
        severity=getattr(case, "severity", None),
        sla_due_at=getattr(case, "sla_due_at", None),
    )


async def _lock(db: AsyncSession, key: str) -> None:
    """Serialise the join of one group (PostgreSQL; other databases skip it)."""
    try:
        dialect = db.get_bind().dialect.name
    except Exception:  # a session double without a bind
        return
    if dialect != "postgresql":
        return
    from sqlalchemy import text as sql_text

    await db.execute(sql_text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})


def _cases_url() -> str | None:
    from app.core.config import settings

    base = (settings.frontend_url or "").rstrip("/")
    return f"{base}/ctem/cases" if base else None


async def join_group(
    db: AsyncSession,
    *,
    member_id: uuid.UUID,
    correlation_key: str,
    title: str,
    priority: str,
    due_at: datetime | None,
    owner_agent_id: uuid.UUID | None,
    created_by_agent_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    workspace_id: uuid.UUID | None,
    is_company_managed: bool,
    waits_for_user_id: uuid.UUID | None,
    waits_for_group_ids: list | None,
    now: datetime,
) -> tuple[Any, bool] | None:
    """The group item the decision *correlation_key* joins, and whether it was created; ``None`` to stay individual.

    The caller writes the case's item blocked on the group, without ``needs_human``.
    Never raises on a case it cannot read: that decision stays individual.
    """
    case_id = case_id_of(correlation_key)
    if case_id is None or owner_agent_id is None:
        return None
    recipient = (
        f"user-{waits_for_user_id}"
        if waits_for_user_id
        else (f"group-{waits_for_group_ids[0]}" if waits_for_group_ids else None)
    )
    if recipient is None:
        return None
    try:
        case = await _case_facts(db, case_id)
    except Exception:
        return None
    if case is None or stays_individual(case, now):
        return None
    kind = group_kind(case)
    if kind is None:
        return None
    from app.models.work_item import WORK_ITEM_TERMINAL_STATUSES, WorkItem

    key = group_key(kind, recipient)
    await _lock(db, key)
    group = (
        await db.execute(
            select(WorkItem)
            .where(
                WorkItem.correlation_key == key,
                WorkItem.owner_agent_id == owner_agent_id,
                WorkItem.needs_human.is_(True),
                WorkItem.status.not_in(WORK_ITEM_TERMINAL_STATUSES),
            )
            .order_by(WorkItem.created_at)
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    member = {
        "item_id": str(member_id),
        "case_id": case_id,
        "title": title[:200],
        "severity": case.severity,
        "due_at": _iso(due_at),
        "priority": priority,
    }
    created = group is None
    if group is None:
        members = [member]
        group = WorkItem(
            user_id=user_id,
            waits_for_user_id=waits_for_user_id,
            waits_for_group_ids=waits_for_group_ids,
            is_company_managed=is_company_managed,
            workspace_id=workspace_id,
            owner_agent_id=owner_agent_id,
            created_by_agent_id=created_by_agent_id,
            title="",
            payload={},
            status="blocked",
            priority=priority,
            correlation_key=key,
            needs_human=True,
            source="agent",
            last_activity_at=now,
        )
        db.add(group)
    else:
        members = [
            m
            for m in (dict(group.payload or {}).get("group") or {}).get("members") or []
            if m.get("case_id") != case_id
        ]
        members.append(member)
    _write_group(group, kind, recipient, members, now)
    # The group's id is what the case's item waits on: give a new group its row now.
    await db.flush()
    return group, created


#: Most open per-case decisions one consolidation pass folds into groups.
CONSOLIDATE_BATCH = 500


async def consolidate(db: AsyncSession, *, now: datetime | None = None, limit: int = CONSOLIDATE_BATCH) -> int:
    """Fold the open per-case decisions of a groupable kind into their recipient's group; how many. Idempotent.

    A case's item that joins a group stops waiting for the person (its own card
    is closed, no notification) and waits on the group, blocked; a re-run finds
    no such item left and changes nothing. The caller commits.
    """
    from sqlalchemy import or_

    from app.models.work_item import WORK_ITEM_TERMINAL_STATUSES, WorkItem

    now = now or datetime.now(UTC)
    rows = (
        (
            await db.execute(
                select(WorkItem)
                .where(
                    WorkItem.needs_human.is_(True),
                    WorkItem.status.not_in(WORK_ITEM_TERMINAL_STATUSES),
                    or_(*(WorkItem.correlation_key.like(f"{p}%") for p in GROUPABLE_PREFIXES)),
                )
                .order_by(WorkItem.created_at)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    folded = 0
    for item in rows:
        joined = await join_group(
            db,
            member_id=item.id,
            correlation_key=str(item.correlation_key),
            title=str(item.title or ""),
            priority=str(item.priority or "medium"),
            due_at=item.due_at,
            owner_agent_id=item.owner_agent_id,
            created_by_agent_id=item.created_by_agent_id,
            user_id=item.user_id,
            workspace_id=item.workspace_id,
            is_company_managed=bool(item.is_company_managed),
            waits_for_user_id=item.waits_for_user_id,
            waits_for_group_ids=item.waits_for_group_ids,
            now=now,
        )
        if joined is None:
            continue
        group = joined[0]
        item.needs_human = None
        item.ask = None
        item.waits_for_user_id = None
        item.waits_for_group_ids = None
        item.status = "blocked"
        item.blocked_on = [str(group.id)]
        item.blocked_reason = f"{GROUPED_REASON_LEAD}{group.id}"
        item.last_activity_at = now
        folded += 1
    return folded


_PRIORITY_RANK = {"urgent": 0, "high": 1, "medium": 2, "low": 3}


def _write_group(group: Any, kind: str, recipient: str, members: list[dict[str, Any]], now: datetime) -> None:
    """Rewrite the group's title, card, due date, priority and member list from *members*."""
    payload = dict(group.payload or {})
    payload["group"] = {"kind": kind, "recipient": recipient, "members": members}
    group.payload = payload
    group.title = f"CTEM: {len(members)} decision(s) grouped ({kind.replace('_', ' ')})"
    group.ask = build_ask(kind, members, cases_url=_cases_url())
    group.due_at = earliest_due(members)
    ranks = [_PRIORITY_RANK.get(str(m.get("priority")), 2) for m in members]
    group.priority = next(p for p, r in _PRIORITY_RANK.items() if r == min(ranks or [2]))
    group.last_activity_at = now


async def fan_out(
    db: AsyncSession,
    group: Any,
    *,
    option: str,
    answer: Mapping[str, Any],
    note: str,
) -> tuple[list[uuid.UUID], bool]:
    """Answer the case items of *group* with *option*: ``(ids answered, group still waiting)``.

    ``approve_selected`` answers the members the answer text names and keeps the
    rest grouped (``ValueError`` when it names none); ``approve_all`` and
    ``decline_all`` answer every member. The caller commits, then publishes each
    answered item's transition (answered) so the fleet's listener applies it.
    """
    from app.models.work_item import WORK_ITEM_TERMINAL_STATUSES, WorkItem

    spec = dict(group.payload or {}).get("group") or {}
    members: list[dict[str, Any]] = list(spec.get("members") or [])
    if option == APPROVE_SELECTED:
        chosen = selected_members(members, answer.get("text"))
        if not chosen:
            raise ValueError(
                "approve_selected needs the cases to approve: name their numbers on the card (e.g. 1, 4, 7-9)"
            )
        case_option = CASE_APPROVE
    elif option == APPROVE_ALL:
        chosen, case_option = members, CASE_APPROVE
    elif option == DECLINE_ALL:
        chosen, case_option = members, CASE_DECLINE
    else:
        raise ValueError(f"option {option!r} is not an answer of a grouped decision")
    ids = [uuid.UUID(str(m["item_id"])) for m in chosen]
    rows = (
        (
            await db.execute(
                select(WorkItem)
                .where(WorkItem.id.in_(ids), WorkItem.status.not_in(WORK_ITEM_TERMINAL_STATUSES))
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    answered: list[uuid.UUID] = []
    label = "Approve" if case_option == CASE_APPROVE else "Decline"
    for item in rows:
        payload = dict(item.payload or {})
        payload["answer"] = {
            **{k: v for k, v in answer.items() if k in ("text", "answered_by", "answered_at", "question")},
            "option": case_option,
            "label": label,
            "via_group": str(group.id),
        }
        item.payload = payload
        item.description = f"{note} (grouped decision {group.id})\n\n{item.description or ''}".rstrip()
        item.status = "open"
        item.blocked_on = None
        item.blocked_reason = None
        item.claimed_by_agent_id = None
        item.claimed_by_run_id = None
        item.claimed_at = None
        item.last_activity_at = now
        answered.append(item.id)
    chosen_ids = {str(m["item_id"]) for m in chosen}
    remaining = [m for m in members if str(m["item_id"]) not in chosen_ids]
    if remaining:
        _write_group(group, str(spec.get("kind") or FIRST_MEASURE), str(spec.get("recipient") or ""), remaining, now)
        return answered, True
    return answered, False
