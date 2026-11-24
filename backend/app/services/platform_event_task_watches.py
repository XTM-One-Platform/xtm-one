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

"""One-off platform event watches that wake a background task.

An orchestrator that waits for an event (a work item completed, a run failed)
used to create a ``platform_event`` assignment with ``once: true`` and delete
it afterwards. A task watch is the primitive without the assignment: the first
platform event that matches starts ONE background task of the watch's agent,
with the watch's brief, then the watch ends::

    create_watch(db, user_id=..., agent_id=..., task="Re-read the PR and merge it",
                 events=["work_item.completed"], filter={"correlation_key": "dev:x"},
                 expires_at=datetime(...))

**Armed** while ``fired_at`` and ``cancelled_at`` are empty and ``expires_at``
is ahead. The deadline is mandatory and at most :data:`MAX_HORIZON` ahead, so
a forgotten watch ends by itself.

**Scope.** The events a task watch receives are the ones a ``scope: own``
assignment of the same agent and owner receives
(:func:`~app.services.platform_events.mismatch_reason`): objects the agent
created, owns, holds or is the parent of, and a personal object only for its
owner; the owner must read every workspace the event touches. There is no
``scope: all`` task watch.

**At most once.** The delivery locks the row, checks it is still armed, the
owner active and the agent enabled, and commits the fired mark BEFORE it
starts the task: two events raised together start one task. A task that then
fails to start re-arms the watch (best effort), so the next event retries.

**The brief.** The task text is the watch's brief followed by the event: its
name and ids, then the payload inside a quarantine block, since a note or a
title is text another agent or a user wrote.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: The farthest deadline a watch may take.
MAX_HORIZON = timedelta(days=7)
#: Armed watches one user may hold at once.
MAX_ARMED_PER_USER = 50
#: Bound of the brief.
MAX_TASK_CHARS = 8000
#: Bound of the event payload quoted in the task text.
_PAYLOAD_MAX_CHARS = 4000
#: ``extra_metadata.origin`` of the background task a watch starts.
ORIGIN = "platform_event_watch"


class TaskWatchError(ValueError):
    """A watch that cannot be set as asked; the message is the refusal."""


def validate(
    *, task: object, events: object, filter: object, expires_at: object, now: datetime | None = None
) -> tuple[str, list[str], dict[str, Any], datetime]:
    """The watch's (task, events, filter, deadline), normalised; raises :class:`TaskWatchError`."""
    from app.services.platform_events import PLATFORM_EVENTS, parse_deadline

    if not isinstance(task, str) or not task.strip():
        raise TaskWatchError("task is required: the brief of the background task the event starts.")
    if len(task) > MAX_TASK_CHARS:
        raise TaskWatchError(f"task is {len(task)} characters; at most {MAX_TASK_CHARS}.")
    names = [events] if isinstance(events, str) else events if isinstance(events, list) else None
    if not names:
        raise TaskWatchError("event must name a platform event, or a list of them (see list_platform_events).")
    unknown = [n for n in names if not isinstance(n, str) or n not in PLATFORM_EVENTS]
    if unknown:
        raise TaskWatchError(
            f"unknown platform event(s) {unknown!r}; known: {', '.join(sorted(PLATFORM_EVENTS))}."
        )
    if filter is None:
        filter = {}
    if not isinstance(filter, dict):
        raise TaskWatchError("filter must be a JSON object (payload keys, dotted paths).")
    # The operator grammar of the payload filter: a misspelled operator
    # would be read as a payload field and the watch would never fire.
    from app.services.integration_event_filters import filter_expression_error

    if (error := filter_expression_error(filter)) is not None:
        raise TaskWatchError(error)
    deadline = expires_at if isinstance(expires_at, datetime) else parse_deadline(expires_at)
    if deadline is None or deadline.tzinfo is None:
        raise TaskWatchError(
            "expires_at is required: an ISO 8601 date-time with its offset, for example 2026-11-02T09:00:00Z."
        )
    moment = now or datetime.now(UTC)
    if deadline <= moment:
        raise TaskWatchError("expires_at is in the past: a watch needs a deadline ahead.")
    if deadline > moment + MAX_HORIZON:
        raise TaskWatchError(f"expires_at is more than {MAX_HORIZON.days} days ahead.")
    return task.strip(), list(dict.fromkeys(names)), dict(filter), deadline


def armed(watch: Any, now: datetime | None = None) -> bool:
    """Whether *watch* still waits for its event."""
    if watch is None or watch.fired_at is not None or watch.cancelled_at is not None:
        return False
    deadline = watch.expires_at
    if deadline is None:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return deadline > (now or datetime.now(UTC))


def state(watch: Any, now: datetime | None = None) -> str:
    """``armed``, ``fired``, ``cancelled`` or ``expired``."""
    if watch.fired_at is not None:
        return "fired"
    if watch.cancelled_at is not None:
        return "cancelled"
    return "armed" if armed(watch, now) else "expired"


def describe(watch: Any, now: datetime | None = None) -> dict[str, Any]:
    """The API view of a watch (the brief included: its owner set it)."""
    return {
        "id": str(watch.id),
        "agent_id": str(watch.agent_id),
        "label": watch.label,
        "task": watch.task,
        "events": list(watch.events or []),
        "filter": dict(watch.filter or {}),
        "expires_at": watch.expires_at.isoformat() if watch.expires_at else None,
        "created_at": watch.created_at.isoformat() if watch.created_at else None,
        "fired_at": watch.fired_at.isoformat() if watch.fired_at else None,
        "fired_event_id": watch.fired_event_id,
        "task_id": str(watch.task_id) if watch.task_id else None,
        "state": state(watch, now),
    }


async def create_watch(
    db: Any,
    *,
    user_id: uuid.UUID,
    agent_id: uuid.UUID,
    task: object,
    events: object,
    filter: object = None,
    expires_at: object = None,
    label: str | None = None,
    created_by_agent_id: uuid.UUID | None = None,
) -> Any:
    """Set a watch and commit it. The caller has checked the user may write *agent_id*."""
    from sqlalchemy import func, select

    from app.models.platform_event_task_watch import PlatformEventTaskWatch as Watch

    text, names, flt, deadline = validate(task=task, events=events, filter=filter, expires_at=expires_at)
    now = datetime.now(UTC)
    count = (
        await db.execute(
            select(func.count())
            .select_from(Watch)
            .where(
                Watch.user_id == user_id,
                Watch.fired_at.is_(None),
                Watch.cancelled_at.is_(None),
                Watch.expires_at > now,
            )
        )
    ).scalar_one()
    if count >= MAX_ARMED_PER_USER:
        raise TaskWatchError(f"{count} watches are already armed; at most {MAX_ARMED_PER_USER}. Cancel one first.")
    watch = Watch(
        user_id=user_id,
        agent_id=agent_id,
        created_by_agent_id=created_by_agent_id,
        task=text,
        events=names,
        filter=flt,
        label=(label or "").strip()[:200] or None,
        expires_at=deadline,
        created_at=now,
    )
    db.add(watch)
    await db.commit()
    await db.refresh(watch)
    return watch


async def list_watches(db: Any, *, user_id: uuid.UUID, include_ended: bool = False, limit: int = 50) -> list[Any]:
    """The user's watches, newest first; armed only unless *include_ended*."""
    from sqlalchemy import select

    from app.models.platform_event_task_watch import PlatformEventTaskWatch as Watch

    query = select(Watch).where(Watch.user_id == user_id)
    if not include_ended:
        query = query.where(
            Watch.fired_at.is_(None), Watch.cancelled_at.is_(None), Watch.expires_at > datetime.now(UTC)
        )
    rows = (await db.execute(query.order_by(Watch.created_at.desc()).limit(max(1, min(limit, 200))))).scalars()
    return list(rows)


async def cancel_watch(db: Any, *, user_id: uuid.UUID, watch_id: uuid.UUID) -> Any | None:
    """Cancel one of the user's armed watches; the row (whatever its state), ``None`` when not theirs."""
    from sqlalchemy import select

    from app.models.platform_event_task_watch import PlatformEventTaskWatch as Watch

    watch = (
        await db.execute(
            select(Watch).where(Watch.id == watch_id, Watch.user_id == user_id).with_for_update()
        )
    ).scalar_one_or_none()
    if watch is None:
        await db.rollback()
        return None
    if armed(watch):
        watch.cancelled_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(watch)
    else:
        await db.rollback()
    return watch


def task_text(watch: Any, name: str, payload: Mapping[str, Any]) -> str:
    """The background task's brief: the watch's brief, the event's ids, the payload quarantined."""
    from app.services.content_safety import wrap_untrusted

    body = json.dumps(dict(payload), default=str, ensure_ascii=False)
    if len(body) > _PAYLOAD_MAX_CHARS:
        body = body[:_PAYLOAD_MAX_CHARS] + " [truncated]"
    header = (
        f"Platform event watch {watch.id} fired: event {name}, event_id {payload.get('event_id')}, "
        f"object {payload.get('object_type')} {payload.get('object_id')}, "
        f"occurred_at {payload.get('occurred_at')}."
    )
    return f"{watch.task}\n\n{header}\n\n{wrap_untrusted(body, f'platform event {name}')}"


async def _armed_for(db: Any, name: str) -> list[Any]:
    from sqlalchemy import select

    from app.models.platform_event_task_watch import PlatformEventTaskWatch as Watch

    rows = (
        await db.execute(
            select(Watch).where(
                Watch.fired_at.is_(None), Watch.cancelled_at.is_(None), Watch.expires_at > datetime.now(UTC)
            )
        )
    ).scalars()
    return [w for w in rows if name in (w.events or [])]


async def _claim(watch_id: uuid.UUID, name: str, payload: Mapping[str, Any]) -> Any | None:
    """Lock the watch, re-check it, its owner and its agent, and commit the fired mark; the row or ``None``."""
    from sqlalchemy import select

    from app.core.database import async_session
    from app.models.agent import Agent
    from app.models.platform_event_task_watch import PlatformEventTaskWatch as Watch
    from app.models.user import User
    from app.services.integration_event_filters import payload_filter_reason

    async with async_session() as db:
        watch = (
            await db.execute(select(Watch).where(Watch.id == watch_id).with_for_update())
        ).scalar_one_or_none()
        if (
            not armed(watch)
            or name not in (watch.events or [])
            or payload_filter_reason(payload, watch.filter or {}) is not None
        ):
            await db.rollback()
            return None
        user_active = (await db.execute(select(User.is_active).where(User.id == watch.user_id))).scalar_one_or_none()
        agent_enabled = (await db.execute(select(Agent.enabled).where(Agent.id == watch.agent_id))).scalar_one_or_none()
        if user_active is not True or agent_enabled is not True:
            await db.rollback()
            return None
        watch.fired_at = datetime.now(UTC)
        watch.fired_event_id = str(payload.get("event_id") or "")[:64] or None
        await db.commit()
        await db.refresh(watch)
        return watch


async def _set_task(watch_id: uuid.UUID, task_id: uuid.UUID | None) -> None:
    """Record the task the watch started, or re-arm it when none started."""
    from sqlalchemy import update

    from app.core.database import async_session
    from app.models.platform_event_task_watch import PlatformEventTaskWatch as Watch

    values: dict[str, Any] = {"task_id": task_id} if task_id else {"fired_at": None, "fired_event_id": None}
    async with async_session() as db:
        await db.execute(update(Watch).where(Watch.id == watch_id).values(**values))
        await db.commit()


async def deliver(watch: Any, name: str, payload: Mapping[str, Any]) -> str:
    """Start the task of one matching watch; what happened."""
    from app.services.sub_agent import create_sub_agent_task

    claimed = await _claim(watch.id, name, payload)
    if claimed is None:
        return f"watch {watch.id} already fired or changed"
    triggered_at = datetime.now(UTC).isoformat()
    task = None
    try:
        task = await create_sub_agent_task(
            None,
            user_id=str(claimed.user_id),
            parent_agent_id=str(claimed.created_by_agent_id or claimed.agent_id),
            target_agent_id=str(claimed.agent_id),
            task_description=task_text(claimed, name, payload),
            allow_self=True,
            extra_metadata={
                "origin": ORIGIN,
                "platform_event_watch_id": str(claimed.id),
                "event": name,
                "event_id": payload.get("event_id"),
                # With the task's own start, the event-to-task latency is a measure.
                "occurred_at": payload.get("occurred_at"),
                "triggered_at": triggered_at,
            },
        )
    except Exception as exc:
        logger.warning("Platform event watch %s: task not started: %s", claimed.id, format_exception_message(exc))
    await _set_task(claimed.id, task.id if task is not None else None)
    if task is None:
        return f"watch {claimed.id} re-armed: the task did not start"
    return f"watch {claimed.id} started task {task.id}"


def _subscription(watch: Any) -> Any:
    """The ``scope: own`` subscription *watch* stands for, as the dispatcher tests it."""
    from app.services import platform_events as pe

    return pe.Subscription(
        assignment_id=watch.id,
        agent_id=watch.agent_id,
        events=tuple(watch.events or ()),
        filter=dict(watch.filter or {}),
        scope_all=False,
        user_id=watch.user_id,
        once=True,
    )


async def _taking(watches: list[Any], name: str, payload: Mapping[str, Any]) -> list[Any]:
    """The *watches* that take event *name*: filter, scope and readable workspaces."""
    from app.services import platform_events as pe

    readable: dict[str, set[str]] = {}
    object_readers: dict[str, bool] = {}
    taking = []
    for watch in watches:
        sub = _subscription(watch)
        if pe.mismatch_reason(sub, name, payload) is not None:
            continue
        if not await pe.can_read_workspace(sub, payload, readable):
            continue
        if not await pe.can_read_object(sub, payload, object_readers):
            continue
        taking.append(watch)
    return taking


async def watches_taking(name: str, payload: Mapping[str, Any]) -> list[Any]:
    """The armed task watches event *name* would start, as :func:`dispatch_event` tests them; read-only.

    For a publisher that must know whether anything hears an event before it
    raises it (``platform_timers``). Raises when the watches cannot be read.
    """
    from app.core.database import async_session

    async with async_session() as db:
        watches = await _armed_for(db, name)
    return await _taking(watches, name, payload)


async def dispatch_event(name: str, payload: Mapping[str, Any]) -> list[str]:
    """Deliver event *name* to every armed task watch in scope; one outcome per delivery."""
    from app.core.database import async_session

    try:
        async with async_session() as db:
            watches = await _armed_for(db, name)
    except Exception as exc:
        logger.warning("Platform event %s: task watches unreadable: %s", name, format_exception_message(exc))
        return []
    outcomes: list[str] = []
    for watch in await _taking(watches, name, payload):
        try:
            outcomes.append(await deliver(watch, name, payload))
        except Exception as exc:
            logger.warning(
                "Platform event %s not delivered to watch %s: %s", name, watch.id, format_exception_message(exc)
            )
    return outcomes
