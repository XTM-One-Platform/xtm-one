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

A member answers with one of its OWN gate's options, never a key the group
invents (#2801): "approve all" gives each case the option recommended on its
own ask, "decline all" the decline option the members share. Only a gate with a
recommendation joins, and only a group whose members offer the same option keys;
a member whose answer cannot be mapped gets its own card back.

The group's key is ``ctem:group:<kind>:<recipient>``: under ``ctem:`` it is a
CTEM key, but it names no case, so no case page counts it as its gate.
"""

from __future__ import annotations

import hashlib
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
#: The option a legacy member receives when its gate's ask cannot be read (binary gates).
CASE_APPROVE = "approve"
CASE_DECLINE = "decline"
#: The option keys "decline all" maps to, first found in the members' shared option set.
DECLINE_KEYS: tuple[str, ...] = (CASE_DECLINE, "do_not_run", "reject", "park")
_LABEL_CHARS = 80

_CONTEXT_CHARS = 800
#: A card number or range standing alone ("4", "7-9"), never digits inside a word such as "host-12".
_NUMBERS = re.compile(r"(?<![\w-])(\d+)(?:\s*-\s*(\d+))?(?![\w-])")

_QUESTIONS: dict[str, str] = {
    FIRST_MEASURE: "Answer {n} first-measure decision(s), each with the option recommended for its case?",
}
_ASKS: dict[str, str] = {
    FIRST_MEASURE: (
        "Each case asks how to measure one asset that has no test evidence yet. "
        "Approve answers each case with its recommended option, shown after its title."
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


def group_key(kind: str, recipient: str, option_keys: Sequence[str] | None = None) -> str:
    """``ctem:group:<kind>:<recipient>``, then ``:<option set digest>`` for members with an option set (pure).

    Gates offering different option keys never share a group (#2801): one
    answer must map to an option every member offers.
    """
    base = f"{GROUP_PREFIX}{kind}:{recipient}"
    if not option_keys:
        return base
    digest = hashlib.sha256("\n".join(sorted(option_keys)).encode()).hexdigest()[:12]
    return f"{base}:{digest}"


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


def member_choice(ask: object) -> tuple[tuple[str, ...], str, str] | None:
    """``(sorted option keys, recommended key, its label)`` of a gate's ask, ``None`` when it cannot join (pure).

    A gate joins a group only when its ask offers at least two options and
    recommends one of them: the group's "approve" answers it with that option.
    """
    if not isinstance(ask, Mapping):
        return None
    options = [o for o in ask.get("options") or [] if isinstance(o, Mapping) and str(o.get("key") or "").strip()]
    if len(options) < 2:
        return None
    recommended = str(ask.get("recommendation") or "").strip()
    match = next((o for o in options if str(o["key"]).strip().casefold() == recommended.casefold()), None)
    if not recommended or match is None:
        return None
    keys = tuple(sorted({str(o["key"]).strip() for o in options}))
    label = str(match.get("label") or match["key"]).strip()[:_LABEL_CHARS]
    return keys, str(match["key"]).strip(), label


def decline_key(option_keys: Sequence[str] | None) -> str | None:
    """The option "decline all" gives every member: the first of :data:`DECLINE_KEYS` the set offers (pure).

    ``None`` for an option set with none of them. A legacy group, whose set was
    never stored, keeps the binary ``decline``.
    """
    if option_keys is None:
        return CASE_DECLINE
    offered = {k.casefold(): k for k in option_keys}
    return next((offered[k.casefold()] for k in DECLINE_KEYS if k.casefold() in offered), None)


def member_answer(ask: object, *, approving: bool, legacy: bool) -> tuple[str, str] | None:
    """``(option key, label)`` a member receives from its group's answer, ``None`` when none of its options fits (pure).

    Approve gives the option its own ask recommends; decline gives the first
    option of :data:`DECLINE_KEYS` its ask offers. A member whose ask cannot be
    read at all keeps the binary ``approve`` / ``decline`` of a group opened
    before #2801 (*legacy*); in a newer group it maps to nothing.
    """
    choice = member_choice(ask)
    if choice is None:
        if not legacy:
            return None
        return (CASE_APPROVE, "Approve") if approving else (CASE_DECLINE, "Decline")
    option_keys, recommended, recommended_label = choice
    if approving:
        return recommended, recommended_label
    key = decline_key(option_keys)
    if key is None:
        return None
    options = ask.get("options") if isinstance(ask, Mapping) else None
    label = next(
        (str(o.get("label") or key) for o in options or [] if isinstance(o, Mapping) and str(o.get("key")) == key),
        key,
    )
    return key, label[:_LABEL_CHARS]


def earliest_due(members: Sequence[Mapping[str, Any]]) -> datetime | None:
    """The earliest ``due_at`` of the members, ``None`` when none has one (pure)."""
    dues = [d for d in (_parse(m.get("due_at")) for m in members) if d is not None]
    return min(dues) if dues else None


def build_ask(
    kind: str,
    members: Sequence[Mapping[str, Any]],
    *,
    cases_url: str | None = None,
    option_keys: Sequence[str] | None = None,
) -> dict[str, Any]:
    """The card of a group: the count, the priority mix, each case with its recommended option, the options (pure).

    "Decline all" is offered only when the members' option set has a decline
    option (:func:`decline_key`).
    """
    n = len(members)
    mix: dict[str, int] = {}
    for member in members:
        mix[str(member.get("severity") or "unrated")] = mix.get(str(member.get("severity") or "unrated"), 0) + 1
    mix_text = ", ".join(f"{count} {sev}" for sev, count in sorted(mix.items()))
    lead = f"{n} case(s) ({mix_text}). {_ASKS.get(kind, '')}".strip()
    # The card shows the head of the list; the numbers past it follow the same order on the cases list.
    lines: list[str] = []
    used = len(lead) + 2
    for index, member in enumerate(members):
        line = f"{index + 1}. {member.get('title') or 'case'}".rstrip()
        if member.get("recommended_label"):
            line = f"{line}: {member['recommended_label']}"
        if used + len(line) + 1 > _CONTEXT_CHARS - 40:
            lines.append(f"- and {n - index} more on the cases list")
            break
        lines.append(line)
        used += len(line) + 1
    ask: dict[str, Any] = {
        "question": _QUESTIONS.get(kind, "Approve the {n} grouped decision(s)?").format(n=n),
        "context": "\n".join([lead, *lines]),
        "options": [
            {
                "key": APPROVE_ALL,
                "label": "Approve all",
                "effect": "Every case of the list is answered with the option shown after its title.",
            },
            {
                "key": APPROVE_SELECTED,
                "label": "Approve selected",
                "effect": "Name the case numbers shown on this card (e.g. 1, 4, 7-9): only those are answered "
                "with their option; the rest stay in this decision and the card then lists the next ones.",
            },
            *(
                [{"key": DECLINE_ALL, "label": "Decline all", "effect": "Every case is parked with your reason."}]
                if decline_key(option_keys)
                else []
            ),
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
    waits_for_group_ids: list[Any] | None,
    now: datetime,
    ask: object = None,
) -> tuple[Any, bool] | None:
    """The group item the decision *correlation_key* joins, and whether it was created; ``None`` to stay individual.

    The caller writes the case's item blocked on the group, without ``needs_human``,
    and keeps its *ask*. A gate whose ask recommends none of two or more options
    stays individual (:func:`member_choice`). Never raises on a case it cannot
    read: that decision stays individual.
    """
    case_id = case_id_of(correlation_key)
    if case_id is None or owner_agent_id is None:
        return None
    choice = member_choice(ask)
    if choice is None:
        return None
    option_keys, recommended, recommended_label = choice
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

    key = group_key(kind, recipient, option_keys)
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
        "recommended": recommended,
        "recommended_label": recommended_label,
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
    _write_group(group, kind, recipient, members, now, option_keys=list(option_keys))
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
    from sqlalchemy import String, cast, literal, or_

    from app.models.ctem_case import CtemCase
    from app.models.work_item import WORK_ITEM_TERMINAL_STATUSES, WorkItem

    now = now or datetime.now(UTC)
    case_key = cast(CtemCase.id, String)
    # Only the gates of first-measure cases are read, so ungroupable gates never fill the batch.
    groupable_case = (
        select(CtemCase.id)
        .where(
            CtemCase.trigger_path == "control_decay",
            CtemCase.title.ilike("%for the first time%"),
            or_(*(WorkItem.correlation_key == literal(p, String) + case_key for p in GROUPABLE_PREFIXES)),
        )
        .exists()
    )
    rows = (
        (
            await db.execute(
                select(WorkItem)
                .where(
                    WorkItem.needs_human.is_(True),
                    WorkItem.status.not_in(WORK_ITEM_TERMINAL_STATUSES),
                    or_(*(WorkItem.correlation_key.like(f"{p}%") for p in GROUPABLE_PREFIXES)),
                    groupable_case,
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
            ask=item.ask,
        )
        if joined is None:
            continue
        group = joined[0]
        # The member keeps its ask (#2801): its options are what the group's answer maps to.
        item.needs_human = None
        item.waits_for_user_id = None
        item.waits_for_group_ids = None
        item.status = "blocked"
        item.blocked_on = [str(group.id)]
        item.blocked_reason = f"{GROUPED_REASON_LEAD}{group.id}"
        item.last_activity_at = now
        folded += 1
    return folded


_PRIORITY_RANK = {"urgent": 0, "high": 1, "medium": 2, "low": 3}


def _write_group(
    group: Any,
    kind: str,
    recipient: str,
    members: list[dict[str, Any]],
    now: datetime,
    *,
    option_keys: list[str] | None = None,
) -> None:
    """Rewrite the group's title, card, due date, priority and member list from *members*.

    *option_keys* is the option set every member offers; ``None`` on a legacy
    group (opened before #2801), which never stored one.
    """
    payload = dict(group.payload or {})
    spec: dict[str, Any] = {"kind": kind, "recipient": recipient, "members": members}
    if option_keys is not None:
        spec["option_keys"] = option_keys
    payload["group"] = spec
    group.payload = payload
    group.title = f"CTEM: {len(members)} decision(s) grouped ({kind.replace('_', ' ')})"
    group.ask = build_ask(kind, members, cases_url=_cases_url(), option_keys=option_keys)
    group.due_at = earliest_due(members)
    ranks = [_PRIORITY_RANK.get(str(m.get("priority")), 2) for m in members]
    group.priority = next(p for p, r in _PRIORITY_RANK.items() if r == min(ranks or [2]))
    group.last_activity_at = now


def _back_to_own_card(item: Any, group: Any, now: datetime) -> None:
    """Give a member whose gate offers no option for the group's answer its own card back (#2801)."""
    item.needs_human = True
    item.waits_for_user_id = getattr(group, "waits_for_user_id", None)
    item.waits_for_group_ids = getattr(group, "waits_for_group_ids", None)
    item.status = "blocked"
    item.blocked_on = None
    item.blocked_reason = None
    note = f"Left the grouped decision {group.id}: no option of this case fits its answer; asked on its own card."
    item.description = f"{note}\n\n{item.description or ''}".rstrip()
    item.last_activity_at = now


async def _restore_asks(db: AsyncSession, items: Sequence[Any]) -> None:
    """Put back the ask a member folded before #2801 lost, from its closed decision (``consolidate`` nulled it)."""
    missing = {item.id: item for item in items if not isinstance(getattr(item, "ask", None), Mapping)}
    if not missing:
        return
    try:
        from app.models.decision import Decision

        rows = (
            await db.execute(
                select(Decision.source_id, Decision.ask)
                .where(Decision.source_type == "work_item", Decision.source_id.in_(list(missing)))
                .order_by(Decision.created_at)
            )
        ).all()
    except Exception:  # a session double without the decisions table: the member stays legacy
        return
    for source_id, ask in rows:  # the latest decision of each item wins
        if source_id in missing and member_choice(ask) is not None:
            missing[source_id].ask = dict(ask)


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
    ``decline_all`` answer every member. Each member receives an option of its
    own gate (:func:`member_answer`); one whose gate offers none for the answer
    is given its own card back and is not in the ids answered. The caller
    commits, then publishes each answered item's transition (answered) so the
    fleet's listener applies it.
    """
    from app.models.work_item import WORK_ITEM_TERMINAL_STATUSES, WorkItem

    spec = dict(group.payload or {}).get("group") or {}
    members: list[dict[str, Any]] = list(spec.get("members") or [])
    stored_keys = spec.get("option_keys")
    option_keys: list[str] | None = [str(k) for k in stored_keys] if isinstance(stored_keys, list) else None
    chosen: list[Mapping[str, Any]]
    approving = option in (APPROVE_ALL, APPROVE_SELECTED)
    if option == APPROVE_SELECTED:
        chosen = selected_members(members, answer.get("text"))
        if not chosen:
            raise ValueError(
                "approve_selected needs the cases to approve: name their numbers on the card (e.g. 1, 4, 7-9)"
            )
    elif option == APPROVE_ALL:
        chosen = [*members]
    elif option == DECLINE_ALL:
        if decline_key(option_keys) is None:
            raise ValueError("decline_all is not an answer of this grouped decision: its cases offer no decline")
        chosen = [*members]
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
    await _restore_asks(db, rows)
    now = datetime.now(UTC)
    answered: list[uuid.UUID] = []
    returned: list[uuid.UUID] = []
    for item in rows:
        mapped = member_answer(item.ask, approving=approving, legacy=option_keys is None)
        if mapped is None:
            # No option of its own gate fits the group's answer: it gets its own card back (#2801).
            _back_to_own_card(item, group, now)
            returned.append(item.id)
            continue
        case_option, label = mapped
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
        _write_group(
            group,
            str(spec.get("kind") or FIRST_MEASURE),
            str(spec.get("recipient") or ""),
            remaining,
            now,
            option_keys=option_keys,
        )
        return answered, True
    return answered, False
