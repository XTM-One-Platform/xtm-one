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

"""``work_item.past_due``: raised once when an open work item crosses its due date (#1662).

``get_fleet_health`` reports ``past_due`` but only the weekly check reads it,
so an overdue item waited a week for a reaction. The scheduler tick
(``autonomous``) now calls :func:`raise_past_due_items`: every open item
(not a scheduled one-off, which has its own missed state) whose ``due_at``
has passed and that carries no platform mark is re-read under ``FOR UPDATE
SKIP LOCKED``, marked, committed, and only then the event is raised.

Rules:

* **Once per due date.** The mark (:data:`PAST_DUE_MARK_KEY` in the payload)
  is kept across a payload rewrite (:func:`carry_past_due_mark`) and cleared
  when ``due_at`` changes (:func:`clear_past_due_mark`): a new due date
  crossed raises again, the same one never twice. The event id is the item
  and the due date, so the dispatcher's dedupe holds as well.
* **No model call:** the event wakes whatever ``platform_event`` assignment
  subscribes to it (the fleet orchestrator's "Overdue work items" listener,
  ``services/fleets.py``).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

PAST_DUE_EVENT = "work_item.past_due"
#: Payload key of the platform's mark: the due date the event was raised for.
PAST_DUE_MARK_KEY = "_past_due_raised_for"
#: Items marked per tick at most; the rest are taken at the next tick.
PAST_DUE_SCAN_LIMIT = 200


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def past_due_event_id(item: Any) -> str:
    """The stable event id of *item*'s crossing of its current due date."""
    return f"{item.id}:past_due:{_iso(item.due_at)}"


def past_due_data(item: Any, *, now: datetime) -> dict[str, Any]:
    """The ``work_item.past_due`` payload: the work item keys, ``due_at`` and ``overdue_seconds``."""
    from app.services.platform_events import work_item_data

    data = work_item_data(item, previous_status=getattr(item, "status", None))
    due = item.due_at if item.due_at.tzinfo else item.due_at.replace(tzinfo=UTC)
    data["due_at"] = due.isoformat()
    data["overdue_seconds"] = max(int((now - due).total_seconds()), 0)
    return data


def carry_past_due_mark(payload: Any, previous: Any) -> Any:
    """*payload* (a writer's new payload) with the stored mark kept: the mark is the platform's."""
    if not isinstance(payload, Mapping) or not isinstance(previous, Mapping):
        return payload
    mark = previous.get(PAST_DUE_MARK_KEY)
    if mark is None or PAST_DUE_MARK_KEY in payload:
        return payload
    return {**payload, PAST_DUE_MARK_KEY: mark}


def clear_past_due_mark(item: Any) -> None:
    """Drop the mark of *item* whose due date changed, so the new one raises once when crossed."""
    payload = getattr(item, "payload", None)
    if isinstance(payload, Mapping) and PAST_DUE_MARK_KEY in payload:
        item.payload = {k: v for k, v in payload.items() if k != PAST_DUE_MARK_KEY}


async def raise_past_due_items(*, now: datetime | None = None, limit: int = PAST_DUE_SCAN_LIMIT) -> int:
    """Mark and raise ``work_item.past_due`` for every open item past its due date; how many."""
    from app.core.database import async_session
    from app.models.work_item import WORK_ITEM_OPEN_STATUSES, WorkItem
    from app.services.platform_events import publish

    now = now or datetime.now(UTC)
    raised = 0
    due_clause = (
        WorkItem.status.in_(WORK_ITEM_OPEN_STATUSES),
        WorkItem.due_at.isnot(None),
        WorkItem.due_at <= now,
        # A scheduled one-off has its own missed state and actions.
        WorkItem.action_mode.is_(None),
    )
    async with async_session() as db:
        ids = (
            (
                await db.execute(
                    select(WorkItem.id)
                    .where(*due_clause, WorkItem.payload[PAST_DUE_MARK_KEY].astext.is_(None))
                    .order_by(WorkItem.due_at.asc())
                    .limit(max(int(limit), 1))
                )
            )
            .scalars()
            .all()
        )
        for item_id in ids:
            try:
                item = (
                    await db.execute(
                        select(WorkItem).where(WorkItem.id == item_id, *due_clause).with_for_update(skip_locked=True)
                    )
                ).scalar_one_or_none()
                payload = dict(item.payload or {}) if item is not None else {}
                if item is None or payload.get(PAST_DUE_MARK_KEY) is not None:
                    await db.rollback()
                    continue
                payload[PAST_DUE_MARK_KEY] = _iso(item.due_at)
                item.payload = payload
                data = past_due_data(item, now=now)
                event_id = past_due_event_id(item)
                await db.commit()
            except Exception as exc:
                logger.warning("Work item %s past due not raised: %s", item_id, format_exception_message(exc))
                await db.rollback()
                continue
            await publish(PAST_DUE_EVENT, data, event_id=event_id)
            raised += 1
    return raised


__all__ = [
    "PAST_DUE_EVENT",
    "PAST_DUE_MARK_KEY",
    "PAST_DUE_SCAN_LIMIT",
    "carry_past_due_mark",
    "clear_past_due_mark",
    "past_due_data",
    "past_due_event_id",
    "raise_past_due_items",
]
