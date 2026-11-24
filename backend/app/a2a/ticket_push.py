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

"""Ticket pushes: telling a partner a ticket its task filed changed status.

A partner files a ticket (a work item) from an A2A task, and the task is long
terminal by the time the ticket moves to merged, released or done. The task
push (``push``) has nothing left to announce, and the bulk read
(``partner_runs.partner_work_items_clause``) needs the partner to ask. This
module closes that loop: on ``work_item.status_changed`` of an item created by
a run of a partner task's run tree, every callback the partner registered on
that originating task receives one notification.

The task push's properties hold, on purpose:

* **identity and status only.** The task, the work item id and the two
  statuses; never the title, a note or the result. The partner reads the
  ticket through the bulk read with its own credential, so a forged
  notification discloses nothing;
* **the callback is the partner's.** The registered address is re-checked by
  the destination guard before every send and the registered credential is
  decrypted only to present it (``push._deliver_body``);
* **best-effort, never retried.** One attempt per status change, recorded on
  the delivery metrics, never on the callback's attempt counter: that budget
  belongs to the task's own announcement. A callback whose task budget is
  spent is not called. The bulk read is the floor.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.a2a import A2ATask

logger = logging.getLogger(__name__)

#: The event this module listens to.
STATUS_CHANGED_EVENT = "work_item.status_changed"

#: The notification's discriminator: a task push carries none, so a partner
#: tells the two apart by its presence.
KIND = "work_item.status_changed"

#: The statuses a partner is told about. ``claimed`` is the platform's own
#: dispatch bookkeeping (a claim precedes ``in_progress`` within seconds) and
#: is not a change of the ticket the partner cares about.
NOTIFIED_STATUSES = frozenset({"open", "in_progress", "blocked", "done", "cancelled"})

#: How far up ``parent_run_id`` a ticket's run is looked for its task. A
#: partner task's consults and delegations nest a few levels; the bound stops
#: a corrupted chain from looping.
_MAX_HOPS = 16


def ticket_body(task: A2ATask, *, work_item_id: str, status: str, previous_status: str | None) -> dict[str, Any]:
    """The notification of one ticket status change, without the token. Pure."""
    return {
        "kind": KIND,
        "taskId": str(task.id),
        "contextId": str(task.context_id),
        "workItemId": work_item_id,
        "status": status,
        "previousStatus": previous_status,
    }


async def originating_task(db: AsyncSession, run_id: uuid.UUID | None) -> A2ATask | None:
    """The partner task whose run tree holds *run_id*, else ``None``.

    The inverse of ``partner_runs._partner_run_tree``: walk ``parent_run_id``
    up from the run that created the ticket until a run an A2A task maps to.
    """
    from app.models.a2a import A2ATask
    from app.models.assignment import AssignmentRun

    seen: set[uuid.UUID] = set()
    current = run_id
    while current is not None and current not in seen and len(seen) < _MAX_HOPS:
        seen.add(current)
        task = (await db.execute(select(A2ATask).where(A2ATask.run_id == current).limit(1))).scalar_one_or_none()
        if task is not None:
            return task
        current = (
            await db.execute(select(AssignmentRun.parent_run_id).where(AssignmentRun.id == current))
        ).scalar_one_or_none()
    return None


async def notify_ticket_status(db: AsyncSession, data: dict[str, Any]) -> int:
    """Notify the partner callbacks of one ``work_item.status_changed``; how many were called.

    Never raises: a failure is a partner that reads the bulk read instead.
    """
    from app.a2a import push as push_svc
    from app.core.config import settings
    from app.models.a2a import A2APushConfig
    from app.models.work_item import WorkItem

    status = data.get("status")
    previous = data.get("previous_status")
    if status not in NOTIFIED_STATUSES or status == previous:
        return 0
    work_item_id = data.get("work_item_id")
    try:
        wid = uuid.UUID(str(work_item_id))
    except (TypeError, ValueError):
        return 0
    try:
        origin = (await db.execute(select(WorkItem.origin_run_id).where(WorkItem.id == wid))).scalar_one_or_none()
        if origin is None:
            return 0
        task = await originating_task(db, origin)
        if task is None:
            return 0
        configs = (
            (
                await db.execute(
                    select(A2APushConfig).where(
                        A2APushConfig.task_id == task.id,
                        # A callback is scoped to its registration, like every
                        # read on this surface: the task's own client only.
                        A2APushConfig.client_id == task.client_id,
                        A2APushConfig.attempts < settings.a2a_push_max_attempts,
                    )
                )
            )
            .scalars()
            .all()
        )
    except Exception:
        logger.warning("Ticket push lookup failed for work item %s", work_item_id, exc_info=True)
        return 0

    body = ticket_body(task, work_item_id=str(wid), status=str(status), previous_status=previous)
    for config in configs:
        await push_svc.deliver_body(config, body, label=f"work item {wid}")
    return len(configs)


async def on_status_changed(event: Any) -> None:
    """Event-bus listener: one session per event, never raises."""
    from app.core.database import async_session

    try:
        async with async_session() as db:
            await notify_ticket_status(db, dict(event.data or {}))
    except Exception:
        logger.warning("Ticket push failed", exc_info=True)


_registered = False


def register_ticket_push() -> None:
    """Listen to ``work_item.status_changed`` (idempotent).

    The bus is in-process and the event is raised in the process that wrote
    the item, so every process registers it at startup, like the platform
    event dispatcher.
    """
    global _registered
    if _registered:
        return
    from app.services.event_bus import event_bus

    event_bus.add_listener(STATUS_CHANGED_EVENT, on_status_changed)
    _registered = True
