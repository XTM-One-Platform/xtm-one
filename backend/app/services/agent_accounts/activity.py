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

"""What an agent account did with every message it heard, kept for its owner.

Each message the account heard is one row of ``agent_account_decisions``: the
engine's decision, its outcome, and the reply it produced. A message parked
for a grace period is the same row, updated when the period ends
(``waited``). What a reply became since - sent after an approval, dropped,
expired - is read from the reply itself (``effective_outcome``), so the log
and the analytics never disagree with the replies.

The account page reads the log paged and filtered (``list_decisions``) and
its analytics over a period (``activity_stats``), every figure computed in
SQL. Rows older than ``RETENTION_DAYS`` are purged by the scheduler
(``purge_old_decisions``).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Select, and_, delete, extract, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session
from app.models.agent_account import AgentAccountDecision, AgentAccountReply
from app.models.annotation import Annotation
from app.services.agent_accounts.events import AccountEvent
from app.services.agent_accounts.rules import Decision

logger = logging.getLogger(__name__)

RETENTION_DAYS = 30
#: The longest message text a row keeps.
PREVIEW_CHARS = 500
#: Rows the scheduler purges per pass.
PURGE_BATCH = 5000
#: Rows of each "top" table of the analytics.
TOP_ROWS = 10

#: What an outcome says the account did: the page's groups. Every other
#: outcome - a decision's reason, ``owner_replied``, ``agent_author`` - is
#: ``left``: the message was not for this account to answer.
OUTCOME_GROUPS = {
    "sent": "answered",
    # The agent handed the message to the account's owner (``triage.py``).
    "flagged": "needs_owner",
    "pending": "awaiting_approval",
    "shadow": "shadow",
    "rejected": "dropped",
    "expired": "dropped",
    "silent": "silent",
    "waiting": "waiting",
    # Queued behind its conversation's turn in flight, then answered within
    # the next turn on a later message of the burst.
    "queued": "waiting",
    "coalesced": "answered",
    "failed": "failed",
    "ai_unavailable": "failed",
    "agent_unavailable": "failed",
    # Another pipeline of the agent - an assignment run, or another of its
    # accounts - took the mail first and answers it; the decision's
    # note names that holder.
    "already_handled": "already_handled",
}
GROUPS = (
    "answered",
    "needs_owner",
    "awaiting_approval",
    "shadow",
    "dropped",
    "silent",
    "waiting",
    "already_handled",
    "left",
    "failed",
)

#: The periods of the analytics, and the width of their buckets.
PERIODS: dict[str, tuple[timedelta, str]] = {
    "24h": (timedelta(hours=24), "hour"),
    "7d": (timedelta(days=7), "day"),
    "30d": (timedelta(days=30), "day"),
}


def group_of(outcome: str) -> str:
    return OUTCOME_GROUPS.get(outcome, "left")


def effective_outcome():
    """The outcome as it is now: the reply's status when the message produced one."""
    return func.coalesce(AgentAccountReply.status, AgentAccountDecision.outcome)


def _in_group(group: str):
    outcome = effective_outcome()
    if group == "left":
        return outcome.not_in(list(OUTCOME_GROUPS))
    return outcome.in_([value for value, name in OUTCOME_GROUPS.items() if name == group])


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


async def record_decision(
    account_id: str,
    event: AccountEvent,
    decision: Decision | None,
    outcome: str,
    *,
    reply_id: uuid.UUID | None = None,
    note: str | None = None,
    holder: str | None = None,
) -> None:
    """Record what the account did with *event*; a later outcome of the same message replaces the earlier one.

    *note* is the agent's own line on its triage (``triage.Triage.note``), and
    only that. *holder* is the claim's holder of a message another pipeline of
    the agent took (``already_handled``), kept as data. On a
    session of its own: the log never decides whether the caller's work
    commits. Never raises.
    """
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "account_id": uuid.UUID(account_id),
        "outcome": outcome[:40],
        "reason": decision.reason[:40] if decision else None,
        "respond": bool(decision and decision.respond),
        "waited": outcome == "waiting",
        "conversation_type": event.conversation_type[:20],
        "conversation_id": event.conversation_id[:500],
        "conversation_name": (event.conversation_name or None) and event.conversation_name[:500],
        "message_id": event.message_id[:255],
        "sender_id": (event.sender_id or None) and event.sender_id[:255],
        "sender_name": (event.sender_name or None) and event.sender_name[:255],
        "preview": event.text[:PREVIEW_CHARS],
        "mentions_account": event.mentions_account,
        "addressed": bool(decision and decision.addressed),
        "thread_continuation": bool(decision and decision.thread_continuation),
        "rule_id": decision.rule_id if decision else None,
        "rule_name": decision.rule_name if decision else None,
        "sender_match": decision.sender if decision else None,
        "note": note or None,
        "holder": (holder or None) and holder[:80],
        "reply_id": reply_id,
        "message_at": event.created_at,
        "created_at": now,
        "updated_at": now,
    }
    insert = pg_insert(AgentAccountDecision).values(**values)
    row = AgentAccountDecision.__table__.c
    statement = insert.on_conflict_do_update(
        constraint="uq_agent_account_decisions_message",
        set_={
            "outcome": insert.excluded.outcome,
            "reason": insert.excluded.reason,
            "respond": insert.excluded.respond,
            "waited": or_(row.waited, insert.excluded.waited),
            "addressed": insert.excluded.addressed,
            "thread_continuation": insert.excluded.thread_continuation,
            "rule_id": insert.excluded.rule_id,
            "rule_name": insert.excluded.rule_name,
            "sender_match": insert.excluded.sender_match,
            "note": func.coalesce(insert.excluded.note, row.note),
            "holder": insert.excluded.holder,
            "reply_id": func.coalesce(insert.excluded.reply_id, row.reply_id),
            "conversation_name": func.coalesce(insert.excluded.conversation_name, row.conversation_name),
            "updated_at": insert.excluded.updated_at,
        },
        # The account hearing a message the agent sent on its own
        # leaves that send's entry as the send logged it.
        where=row.kind.is_distinct_from("proactive"),
    )
    try:
        async with async_session() as db:
            await db.execute(statement)
            await db.commit()
    except Exception:
        logger.warning("Could not record a decision of agent account %s", account_id, exc_info=True)


async def record_turn_trace(
    account_id: str, conversation_id: str, message_ids: list[str], trace_id: str
) -> None:
    """Name on every message of one turn the trace that answered it.

    A message merged into another's turn (``coalesced``) is answered by that
    turn: its entry then says which, so the turn's prompt can be read from the
    message. On a session of its own; never raises.
    """
    ids = [m[:255] for m in message_ids if m]
    if not ids or not trace_id:
        return
    try:
        async with async_session() as db:
            await db.execute(
                AgentAccountDecision.__table__.update()
                .where(
                    AgentAccountDecision.account_id == uuid.UUID(account_id),
                    AgentAccountDecision.conversation_id == conversation_id[:500],
                    AgentAccountDecision.message_id.in_(ids),
                )
                .values(trace_id=trace_id[:64])
            )
            await db.commit()
    except Exception:
        logger.warning("Could not record the trace of a turn of agent account %s", account_id, exc_info=True)


async def purge_old_decisions(db: AsyncSession) -> int:
    """Delete one batch of rows older than ``RETENTION_DAYS``; the caller commits."""
    cutoff = datetime.now(UTC) - timedelta(days=RETENTION_DAYS)
    old = select(AgentAccountDecision.id).where(AgentAccountDecision.created_at < cutoff).limit(PURGE_BATCH)
    result = await db.execute(delete(AgentAccountDecision).where(AgentAccountDecision.id.in_(old)))
    return result.rowcount or 0


# ---------------------------------------------------------------------------
# The log
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionFilters:
    #: One of :data:`GROUPS`.
    group: str | None = None
    conversation_type: str | None = None
    conversation_id: str | None = None
    #: A sender's platform id or, for one the platform named only, their name.
    sender: str | None = None
    #: Words of the message, the sender's or the conversation's name, or the agent's note.
    text: str | None = None
    #: ``reply`` (a message the account heard) or ``proactive`` (a message the
    #: agent sent on its own); None for both.
    kind: str | None = None
    #: Several of :data:`GROUPS` at once (any of them), with ``group``.
    groups: tuple[str, ...] = ()
    #: Several kinds of conversation at once (any of them), with ``conversation_type``.
    conversation_types: tuple[str, ...] = ()


def _logged(account_id: uuid.UUID, filters: DecisionFilters, *columns: Any) -> Select:
    query = (
        select(*columns)
        .select_from(AgentAccountDecision)
        .outerjoin(AgentAccountReply, AgentAccountReply.id == AgentAccountDecision.reply_id)
        .where(AgentAccountDecision.account_id == account_id)
    )
    groups = [name for name in (filters.group, *filters.groups) if name in GROUPS]
    if groups:
        query = query.where(or_(*(_in_group(name) for name in dict.fromkeys(groups))))
    if filters.kind == "proactive":
        query = query.where(AgentAccountDecision.kind == "proactive")
    elif filters.kind == "reply":
        query = query.where(AgentAccountDecision.kind.is_distinct_from("proactive"))
    conversation_types = [value for value in (filters.conversation_type, *filters.conversation_types) if value]
    if conversation_types:
        query = query.where(AgentAccountDecision.conversation_type.in_(list(dict.fromkeys(conversation_types))))
    if filters.conversation_id:
        query = query.where(AgentAccountDecision.conversation_id == filters.conversation_id)
    if filters.sender:
        query = query.where(
            or_(AgentAccountDecision.sender_id == filters.sender, AgentAccountDecision.sender_name == filters.sender)
        )
    words = (filters.text or "").strip()
    if words:
        query = query.where(
            or_(
                AgentAccountDecision.preview.icontains(words, autoescape=True),
                AgentAccountDecision.sender_name.icontains(words, autoescape=True),
                AgentAccountDecision.conversation_name.icontains(words, autoescape=True),
                AgentAccountDecision.note.icontains(words, autoescape=True),
            )
        )
    return query


def _decision_order(sort: str, direction: str) -> tuple[Any, ...]:
    """ORDER BY of the log: the column asked, then newest first so equal values keep a stable order."""
    newest = (AgentAccountDecision.created_at.desc(), AgentAccountDecision.id.desc())
    column = {
        "conversation": AgentAccountDecision.conversation_name,
        "sender": AgentAccountDecision.sender_name,
    }.get(sort)
    if column is None:
        if direction == "asc":
            return (AgentAccountDecision.created_at.asc(), AgentAccountDecision.id.asc())
        return newest
    ordered = column.asc().nulls_last() if direction == "asc" else column.desc().nulls_last()
    return (ordered, *newest)


async def list_decisions(
    db: AsyncSession,
    account_id: uuid.UUID,
    filters: DecisionFilters,
    *,
    limit: int,
    offset: int,
    sort: str = "date",
    direction: str = "desc",
) -> tuple[list[dict[str, Any]], int]:
    """One page of the log with the reply each message produced; and the total.

    Newest first unless *sort* (``date``, ``conversation`` or ``sender``) and
    *direction* say otherwise; equal values keep the newest first.
    """
    total = (await db.execute(_logged(account_id, filters, func.count()))).scalar_one()
    rows = (
        await db.execute(
            _logged(
                account_id,
                filters,
                AgentAccountDecision,
                effective_outcome().label("effective"),
                AgentAccountReply.status,
                AgentAccountReply.sent_text,
                AgentAccountReply.draft_text,
                AgentAccountReply.error,
                AgentAccountReply.origin,
            )
            .order_by(*_decision_order(sort, direction))
            .limit(limit)
            .offset(offset)
        )
    ).all()
    items = []
    for decision, effective, reply_status, sent_text, draft_text, reply_error, origin in rows:
        items.append(
            {
                "decision": decision,
                "kind": decision.kind or "reply",
                "origin": origin if decision.kind == "proactive" else None,
                "outcome": effective,
                "group": group_of(effective),
                "reply_status": reply_status,
                "reply_text": sent_text or draft_text,
                "reply_error": reply_error,
            }
        )
    return items, int(total)


# ---------------------------------------------------------------------------
# The analytics
# ---------------------------------------------------------------------------


def _zone(tz: str | None) -> str:
    try:
        ZoneInfo(tz or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"
    return tz or "UTC"


def _buckets(since: datetime, until: datetime, unit: str, tz: str) -> list[datetime]:
    """Every bucket start of the period, in *tz*, naive - as PostgreSQL truncates them."""
    zone = ZoneInfo(tz)
    start = since.astimezone(zone).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    if unit == "day":
        start = start.replace(hour=0)
    end = until.astimezone(zone).replace(tzinfo=None)
    step = timedelta(hours=1) if unit == "hour" else timedelta(days=1)
    out = []
    while start <= end:
        out.append(start)
        start += step
    return out


def _median(expression):
    return func.percentile_cont(0.5).within_group(expression)


def _in_window(window: Any, *columns: Any) -> Select:
    """*columns* over the decisions *window* selects, each joined to the reply it produced."""
    return (
        select(*columns)
        .select_from(AgentAccountDecision)
        .outerjoin(AgentAccountReply, AgentAccountReply.id == AgentAccountDecision.reply_id)
        .where(window)
    )


async def activity_stats(db: AsyncSession, account_id: uuid.UUID, *, period: str, tz: str | None) -> dict[str, Any]:
    """What the account did over *period* (:data:`PERIODS`), timeline buckets in *tz*."""
    span, unit = PERIODS.get(period, PERIODS["7d"])
    zone = _zone(tz)
    until = datetime.now(UTC)
    since = until - span
    # The analytics are about the messages the account heard: a message the
    # agent sent on its own is in the log, not in these figures.
    window = and_(
        AgentAccountDecision.account_id == account_id,
        AgentAccountDecision.created_at >= since,
        AgentAccountDecision.kind.is_distinct_from("proactive"),
    )
    outcome = effective_outcome()
    answered = func.count().filter(outcome == "sent")
    for_agent = func.count().filter(AgentAccountDecision.respond.is_(True))

    groups = dict.fromkeys(GROUPS, 0)
    outcomes: dict[str, int] = {}
    by_type: dict[str, dict[str, int]] = {}
    heard = for_agent_total = 0
    for conversation_type, value, respond, count in (
        await db.execute(
            _in_window(window, AgentAccountDecision.conversation_type, outcome, AgentAccountDecision.respond, func.count())
            .group_by(AgentAccountDecision.conversation_type, outcome, AgentAccountDecision.respond)
        )
    ).all():
        heard += count
        for_agent_total += count if respond else 0
        group = group_of(value)
        groups[group] += count
        outcomes[value] = outcomes.get(value, 0) + count
        per_type = by_type.setdefault(conversation_type, dict.fromkeys(GROUPS, 0))
        per_type[group] += count

    bucket = func.date_trunc(unit, func.timezone(zone, AgentAccountDecision.created_at))
    counted: dict[datetime, dict[str, int]] = {}
    # By position: the bucket carries bound parameters, which PostgreSQL never
    # matches between the select list and a repeated GROUP BY expression.
    for at, value, count in (
        await db.execute(_in_window(window, bucket, outcome, func.count()).group_by(text("1"), text("2")))
    ).all():
        at = at.replace(tzinfo=None) if isinstance(at, datetime) else at
        per_bucket = counted.setdefault(at, dict.fromkeys(GROUPS, 0))
        per_bucket[group_of(value)] += count
    timeline = [
        {"at": start.isoformat(), "counts": counted.get(start, dict.fromkeys(GROUPS, 0))}
        for start in _buckets(since, until, unit, zone)
    ]

    conversations = [
        {
            "conversation_id": row.conversation_id,
            "conversation_name": row.name,
            "conversation_type": row.conversation_type,
            "with_name": row.with_name,
            "heard": row.heard,
            "for_agent": row.for_agent,
            "answered": row.answered,
        }
        for row in (
            await db.execute(
                _in_window(
                    window,
                    AgentAccountDecision.conversation_id,
                    AgentAccountDecision.conversation_type,
                    func.max(AgentAccountDecision.conversation_name).label("name"),
                    # Who the account talks with there, its own messages aside:
                    # a direct chat has no name of its own.
                    func.max(AgentAccountDecision.sender_name)
                    .filter(AgentAccountDecision.reason.is_distinct_from("own_message"))
                    .label("with_name"),
                    func.count().label("heard"),
                    for_agent.label("for_agent"),
                    answered.label("answered"),
                )
                .group_by(AgentAccountDecision.conversation_id, AgentAccountDecision.conversation_type)
                .order_by(func.count().desc())
                .limit(TOP_ROWS)
            )
        ).all()
    ]

    sender_key = func.coalesce(AgentAccountDecision.sender_id, AgentAccountDecision.sender_name)
    senders = [
        {
            "sender": row.sender,
            "sender_name": row.name,
            "heard": row.heard,
            "for_agent": row.for_agent,
            "answered": row.answered,
        }
        for row in (
            await db.execute(
                _in_window(
                    window,
                    sender_key.label("sender"),
                    func.max(AgentAccountDecision.sender_name).label("name"),
                    func.count().label("heard"),
                    for_agent.label("for_agent"),
                    answered.label("answered"),
                )
                .where(sender_key.is_not(None))
                .group_by(sender_key)
                .order_by(func.count().desc())
                .limit(TOP_ROWS)
            )
        ).all()
    ]

    rules = [
        {
            "rule_id": row.rule_id,
            "rule_name": row.name,
            "matched": row.matched,
            "answered": row.answered,
            "silent": row.silent,
        }
        for row in (
            await db.execute(
                _in_window(
                    window,
                    AgentAccountDecision.rule_id,
                    func.max(AgentAccountDecision.rule_name).label("name"),
                    func.count().label("matched"),
                    answered.label("answered"),
                    func.count().filter(outcome == "silent").label("silent"),
                )
                .where(AgentAccountDecision.rule_id.is_not(None))
                .group_by(AgentAccountDecision.rule_id)
                .order_by(func.count().desc())
                .limit(TOP_ROWS * 2)
            )
        ).all()
    ]

    reply_window = and_(
        AgentAccountReply.account_id == account_id,
        AgentAccountReply.created_at >= since,
        AgentAccountReply.kind.is_distinct_from("proactive"),
    )
    replies = {
        status: count
        for status, count in (
            await db.execute(
                select(AgentAccountReply.status, func.count()).where(reply_window).group_by(AgentAccountReply.status)
            )
        ).all()
    }
    median_answer = (
        await db.execute(
            select(
                _median(extract("epoch", AgentAccountReply.created_at - AgentAccountDecision.message_at))
            )
            .select_from(AgentAccountReply)
            .join(AgentAccountDecision, AgentAccountDecision.reply_id == AgentAccountReply.id)
            .where(
                reply_window,
                AgentAccountReply.status == "sent",
                AgentAccountReply.decided_at.is_(None),
                AgentAccountDecision.message_at.is_not(None),
            )
        )
    ).scalar()
    median_approval = (
        await db.execute(
            select(_median(extract("epoch", AgentAccountReply.decided_at - AgentAccountReply.created_at))).where(
                reply_window, AgentAccountReply.status == "sent", AgentAccountReply.decided_at.is_not(None)
            )
        )
    ).scalar()
    ratings = dict.fromkeys(("positive", "negative"), 0)
    for rating, count in (
        await db.execute(
            select(Annotation.rating, func.count())
            .join(AgentAccountReply, AgentAccountReply.id == Annotation.target_id)
            .where(Annotation.target_type == "agent_account_reply", reply_window)
            .group_by(Annotation.rating)
        )
    ).all():
        if rating in ratings:
            ratings[rating] = int(count)

    return {
        "period": period if period in PERIODS else "7d",
        "bucket": unit,
        "timezone": zone,
        "since": since.isoformat(),
        "until": until.isoformat(),
        "heard": heard,
        "for_agent": for_agent_total,
        "groups": groups,
        "outcomes": outcomes,
        "left_reasons": {value: count for value, count in outcomes.items() if group_of(value) == "left"},
        "by_conversation_type": by_type,
        "timeline": timeline,
        "conversations": conversations,
        "senders": senders,
        "rules": rules,
        "replies": replies,
        "median_answer_seconds": float(median_answer) if median_answer is not None else None,
        "median_approval_seconds": float(median_approval) if median_approval is not None else None,
        "ratings": ratings,
    }
