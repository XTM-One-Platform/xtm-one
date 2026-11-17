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

"""Per-object timers: a run arms a date, the platform raises ``timer.due`` then.

A run that must act later on one object (a scheduling thread unanswered after
three business days, a calendar entry at T-60) arms a timer with one tool call
(``arm_timer``) instead of keeping a cron that polls every object. At its due
time the scheduler tick (``autonomous``) raises the ``timer.due`` platform
event; every ``platform_event`` assignment whose filter matches (for example
``{"correlation_key": {"starts_with": "schedule:"}}``) runs once with the
timer's payload.

Rules:

* **One armed timer per (agent, correlation key).** Arming the same key again
  replaces the earlier timer (state ``replaced``), in one transaction under
  the partial unique index ``uq_platform_timers_armed_key``.
* **Unless.** A timer armed with ``unless_work_item_id`` is skipped, never
  fired, when that work item is done or cancelled by its due time; with
  ``unless_shared_state_key``, when that shared-state entry is resolved. Both
  are read as the person the timer runs for.
* **At most once.** :func:`fire_due_timers` re-reads each due row under
  ``FOR UPDATE SKIP LOCKED`` and commits its ``fired`` state, with the event
  id, before raising the event: two scheduler processes never both fire it,
  and a crash after the commit loses one event rather than raising two. The
  event id is the timer id, so the dispatcher's dedupe per (assignment, event)
  holds as well.
* **Durable.** The row lives in Postgres: a rollout between arm and due loses
  nothing; a timer due while no scheduler ran fires on the next tick.
* **No model call** at arm time or at fire time: the event wakes whatever
  assignment subscribes to it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from app.core.utils import format_exception_message, normalize_correlation_key, safe_uuid
from app.models.platform_timer import (
    TIMER_ARMED,
    TIMER_CANCELLED,
    TIMER_FIRED,
    TIMER_REPLACED,
    TIMER_SKIPPED,
    TIMER_STATES,
    PlatformTimer,
)

logger = logging.getLogger(__name__)

#: The platform event a due timer raises.
TIMER_DUE_EVENT = "timer.due"

#: How far ahead a timer may be armed: a timer is not a calendar.
MAX_HORIZON_DAYS = 366

#: The largest payload a timer carries, serialised (it travels in every event).
MAX_PAYLOAD_CHARS = 8_000

#: How many armed timers one agent may hold for one person.
MAX_ARMED_PER_AGENT = 2_000

#: The list states :func:`list_timers` accepts.
LIST_STATES: tuple[str, ...] = ("armed", "ended", "all")


class TimerError(ValueError):
    """A timer request refused, with the words the caller is told."""


def parse_due(value: Any, *, now: datetime | None = None) -> datetime:
    """``due_at`` as an aware UTC instant: ISO 8601 with its offset, ahead, within the horizon."""
    text = str(value or "").strip()
    if not text:
        raise TimerError("due_at is required: an ISO 8601 date and time with its offset, e.g. 2026-11-10T09:00:00+01:00.")
    if text[-1] in "Zz":
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise TimerError(f"due_at '{value}' is not an ISO 8601 date and time with its offset.") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TimerError(f"due_at '{value}' has no UTC offset; a time without one is never read as UTC.")
    now = now or datetime.now(UTC)
    at = parsed.astimezone(UTC)
    if at <= now:
        raise TimerError(f"due_at {at.isoformat()} is not in the future (it is now {now.isoformat()}).")
    if at > now + timedelta(days=MAX_HORIZON_DAYS):
        raise TimerError(f"due_at {at.isoformat()} is more than {MAX_HORIZON_DAYS} days ahead.")
    return at


def check_payload(payload: Any) -> dict:
    """The timer's payload: a JSON object within :data:`MAX_PAYLOAD_CHARS`."""
    import json

    if payload in (None, ""):
        return {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            raise TimerError("payload must be a JSON object.") from None
    if not isinstance(payload, dict):
        raise TimerError("payload must be a JSON object.")
    try:
        size = len(json.dumps(payload, default=str))
    except (TypeError, ValueError):
        raise TimerError("payload must be JSON-serialisable.") from None
    if size > MAX_PAYLOAD_CHARS:
        raise TimerError(f"payload is {size} characters; a timer carries at most {MAX_PAYLOAD_CHARS}.")
    return payload


def serialize_timer(timer: PlatformTimer) -> dict[str, Any]:
    """A timer as the tools and the run page read it."""

    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    return {
        "id": str(timer.id),
        "correlation_key": timer.correlation_key,
        "due_at": _iso(timer.due_at),
        "state": timer.state,
        "payload": timer.payload or {},
        "unless_work_item_id": str(timer.unless_work_item_id) if timer.unless_work_item_id else None,
        "unless_shared_state_key": timer.unless_shared_state_key,
        "agent_id": str(timer.agent_id),
        "run_id": str(timer.run_id) if timer.run_id else None,
        "outcome": timer.outcome,
        "event_id": timer.event_id,
        "created_at": _iso(timer.created_at),
        "ended_at": _iso(timer.ended_at),
    }


async def arm_timer(
    *,
    user_id: str,
    agent_id: str,
    correlation_key: str,
    due_at: Any,
    payload: Any = None,
    unless_work_item_id: str | None = None,
    unless_shared_state_key: str | None = None,
    run_id: str | uuid.UUID | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Arm (or re-arm) the timer *correlation_key* of *agent_id*; (timer, replaced timer or None)."""
    from app.core.database import async_session

    uid, aid = safe_uuid(user_id), safe_uuid(agent_id)
    if uid is None or aid is None:
        raise TimerError("a timer needs the person and the agent it runs for.")
    key = normalize_correlation_key(correlation_key)
    if not key:
        raise TimerError("correlation_key is required: the object the timer is about, e.g. schedule:thread-42.")
    at = parse_due(due_at, now=now)
    body = check_payload(payload)
    unless_item = None
    if unless_work_item_id:
        unless_item = safe_uuid(unless_work_item_id)
        if unless_item is None:
            raise TimerError(f"unless_work_item_id '{unless_work_item_id}' is not a work item id.")
    unless_key = normalize_correlation_key(unless_shared_state_key) if unless_shared_state_key else None
    now = now or datetime.now(UTC)
    async with async_session() as db:
        previous = (
            await db.execute(
                select(PlatformTimer)
                .where(
                    PlatformTimer.agent_id == aid,
                    PlatformTimer.correlation_key == key,
                    PlatformTimer.state == TIMER_ARMED,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        replaced = None
        if previous is not None:
            previous.state = TIMER_REPLACED
            previous.ended_at = now
            previous.outcome = "replaced by a new arm_timer on the same correlation_key"
            replaced = serialize_timer(previous)
            # The partial unique index admits the new row only once this one left 'armed'.
            await db.flush()
        else:
            armed = (
                await db.execute(
                    select(func.count(PlatformTimer.id)).where(
                        PlatformTimer.agent_id == aid,
                        PlatformTimer.user_id == uid,
                        PlatformTimer.state == TIMER_ARMED,
                    )
                )
            ).scalar_one()
            if armed >= MAX_ARMED_PER_AGENT:
                await db.rollback()
                raise TimerError(
                    f"this agent already holds {armed} armed timers for this person (at most "
                    f"{MAX_ARMED_PER_AGENT}): cancel the ones no longer needed."
                )
        timer = PlatformTimer(
            id=uuid.uuid4(),
            user_id=uid,
            agent_id=aid,
            run_id=safe_uuid(str(run_id)) if run_id else None,
            correlation_key=key,
            due_at=at,
            payload=body,
            unless_work_item_id=unless_item,
            unless_shared_state_key=unless_key,
            state=TIMER_ARMED,
            created_at=now,
        )
        db.add(timer)
        await db.commit()
        return serialize_timer(timer), replaced


async def cancel_timer(
    *, user_id: str, agent_id: str, correlation_key: str | None = None, timer_id: str | None = None
) -> dict[str, Any] | None:
    """Cancel the armed timer named by key or id, of *agent_id* for *user_id*; ``None`` when there is none."""
    from app.core.database import async_session

    uid, aid = safe_uuid(user_id), safe_uuid(agent_id)
    if uid is None or aid is None:
        return None
    clauses = [PlatformTimer.user_id == uid, PlatformTimer.agent_id == aid, PlatformTimer.state == TIMER_ARMED]
    if timer_id:
        tid = safe_uuid(timer_id)
        if tid is None:
            return None
        clauses.append(PlatformTimer.id == tid)
    else:
        key = normalize_correlation_key(correlation_key)
        if not key:
            raise TimerError("name the timer to cancel: its correlation_key or its timer_id.")
        clauses.append(PlatformTimer.correlation_key == key)
    async with async_session() as db:
        timer = (await db.execute(select(PlatformTimer).where(*clauses).with_for_update())).scalar_one_or_none()
        if timer is None:
            await db.rollback()
            return None
        timer.state = TIMER_CANCELLED
        timer.ended_at = datetime.now(UTC)
        timer.outcome = "cancelled"
        await db.commit()
        return serialize_timer(timer)


async def list_timers(
    *,
    user_id: str,
    agent_id: str | None = None,
    run_id: str | None = None,
    state: str = "armed",
    key_prefix: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """The timers of *user_id* (of one agent, or one run), soonest due first for the armed ones."""
    from app.core.database import async_session

    if state not in LIST_STATES:
        raise TimerError(f"state must be one of {', '.join(LIST_STATES)}.")
    uid = safe_uuid(user_id)
    if uid is None:
        return []
    query = select(PlatformTimer).where(PlatformTimer.user_id == uid)
    if agent_id:
        aid = safe_uuid(agent_id)
        if aid is None:
            return []
        query = query.where(PlatformTimer.agent_id == aid)
    if run_id:
        rid = safe_uuid(run_id)
        if rid is None:
            return []
        query = query.where(PlatformTimer.run_id == rid)
    if state == "armed":
        query = query.where(PlatformTimer.state == TIMER_ARMED).order_by(PlatformTimer.due_at.asc())
    else:
        if state == "ended":
            query = query.where(PlatformTimer.state != TIMER_ARMED)
        query = query.order_by(PlatformTimer.created_at.desc())
    # The prefix is read like the key it matches: arm_timer stores the key stripped and cut
    # at CORRELATION_KEY_MAX_LENGTH, so an unnormalized prefix (a long base64 id, a stray
    # space) would miss a timer the same key re-arms. LIKE wildcards match literally.
    key_prefix = normalize_correlation_key(key_prefix)
    if key_prefix:
        escaped = key_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(PlatformTimer.correlation_key.like(f"{escaped}%", escape="\\"))
    query = query.limit(max(1, min(int(limit or 50), 200)))
    async with async_session() as db:
        rows = (await db.execute(query)).scalars().all()
    return [serialize_timer(row) for row in rows]


async def unless_reason(timer: PlatformTimer) -> str | None:
    """Why *timer* must be skipped (its ``unless`` condition met), ``None`` when it fires."""
    from app.models.shared_state import SHARED_STATE_RESOLVED_STATUS
    from app.models.work_item import WORK_ITEM_TERMINAL_STATUSES

    if timer.unless_work_item_id is not None:
        from app.services.work_item import get_work_item

        item = await get_work_item(str(timer.unless_work_item_id), user_id=str(timer.user_id))
        if item is not None and item.get("status") in WORK_ITEM_TERMINAL_STATUSES:
            return f"unless: work item {timer.unless_work_item_id} is {item.get('status')}"
    if timer.unless_shared_state_key:
        from app.services.shared_state import get_state

        entry = await get_state(user_id=str(timer.user_id), key=timer.unless_shared_state_key)
        if entry is not None and entry.get("status") == SHARED_STATE_RESOLVED_STATUS:
            return f"unless: shared state {timer.unless_shared_state_key} is resolved"
    return None


def timer_due_data(timer: PlatformTimer) -> dict[str, Any]:
    """The payload of the ``timer.due`` event raised by *timer*."""
    return {
        "timer_id": str(timer.id),
        "correlation_key": timer.correlation_key,
        "due_at": timer.due_at.isoformat(),
        "payload": timer.payload or {},
        "armed_by_run_id": str(timer.run_id) if timer.run_id else None,
        "agent_id": str(timer.agent_id),
        "participant_agent_ids": [str(timer.agent_id)],
        "object_type": "timer",
        "object_id": str(timer.id),
        "user_id": str(timer.user_id),
        # A timer is a personal object of the person it runs for.
        "personal_owner_id": str(timer.user_id),
    }


async def fire_due_timers(*, now: datetime | None = None, limit: int = 200) -> int:
    """Fire (or skip) every armed timer due by *now*; how many raised ``timer.due``.

    One transaction per timer: the row is re-read under ``FOR UPDATE SKIP
    LOCKED``, its ``unless`` checked, its end state committed, and only then
    the event raised (at most once).
    """
    from app.core.database import async_session
    from app.services.platform_events import publish

    now = now or datetime.now(UTC)
    fired = 0
    async with async_session() as db:
        ids = (
            await db.execute(
                select(PlatformTimer.id)
                .where(PlatformTimer.state == TIMER_ARMED, PlatformTimer.due_at <= now)
                .order_by(PlatformTimer.due_at.asc())
                .limit(max(int(limit), 1))
            )
        ).scalars().all()
        for timer_id in ids:
            try:
                timer = (
                    await db.execute(
                        select(PlatformTimer)
                        .where(PlatformTimer.id == timer_id, PlatformTimer.state == TIMER_ARMED)
                        .with_for_update(skip_locked=True)
                    )
                ).scalar_one_or_none()
                if timer is None:
                    await db.rollback()
                    continue
                reason = await unless_reason(timer)
                timer.ended_at = now
                if reason is not None:
                    timer.state = TIMER_SKIPPED
                    timer.outcome = reason
                    await db.commit()
                    continue
                timer.state = TIMER_FIRED
                timer.event_id = str(timer.id)
                timer.outcome = f"{TIMER_DUE_EVENT} raised"
                data = timer_due_data(timer)
                await db.commit()
            except Exception as exc:
                logger.warning("Timer %s not fired: %s", timer_id, format_exception_message(exc))
                await db.rollback()
                continue
            await publish(TIMER_DUE_EVENT, data, event_id=str(timer_id))
            fired += 1
    return fired


__all__ = [
    "LIST_STATES",
    "MAX_ARMED_PER_AGENT",
    "MAX_HORIZON_DAYS",
    "MAX_PAYLOAD_CHARS",
    "TIMER_DUE_EVENT",
    "TIMER_STATES",
    "TimerError",
    "arm_timer",
    "cancel_timer",
    "check_payload",
    "fire_due_timers",
    "list_timers",
    "parse_due",
    "serialize_timer",
    "timer_due_data",
    "unless_reason",
]
