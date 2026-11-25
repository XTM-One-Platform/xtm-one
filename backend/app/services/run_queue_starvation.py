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

"""Assignment runs waiting for a worker: why a run waits, and one admin alert per starvation episode (#2040).

A run waiting in the queue used to say only ``pending``: nobody could tell a run held by a saturated pool from one
not due yet, and nobody was told when assignment runs waited for an hour and expired before start
(``expired_not_started``, ``deadline_exceeded_before_start``) while chat was served.

* :func:`queue_wait_line` - the reason and the place in the queue of a waiting run, for ``list_runs`` / ``get_run``;
* :func:`check_queue_starvation` - on the scheduler tick: when the oldest due assignment run has waited for a worker
  over :data:`STARVATION_ALERT_SECONDS`, or a run expired before start, every active administrator gets ONE
  notification-bell entry for the episode. The episode is a Redis key held while it lasts and dropped when the
  queue drains, so the next episode alerts again; Redis unreachable sends nothing (never one alert per tick).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: How long the oldest due assignment run may wait for a worker before the administrators are told.
STARVATION_ALERT_SECONDS = 900

#: The window in which a run expired before start opens an episode: a few scheduler ticks.
EXPIRED_LOOKBACK_SECONDS = 300

#: The Redis key held while an episode lasts, and how long it outlives the last tick that saw it.
EPISODE_KEY = "assignment_queue:starvation_episode"
EPISODE_TTL_SECONDS = 3 * 3600

#: The ``notification_source`` of the alert, for the bell's filters.
NOTIFICATION_SOURCE = "assignment_queue_starvation"

_ADAPTER = "assignment"


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _waiting(model: Any, now: datetime) -> list[Any]:
    from app.services.orchestration_engine import STARTED_AT_PAYLOAD_KEY

    return [
        model.adapter == _ADAPTER,
        model.state == "pending",
        model.run_at <= now,
        ~model.payload.has_key(STARTED_AT_PAYLOAD_KEY),
    ]


def describe_wait(*, run_at: datetime | None, now: datetime, ahead: int) -> str:
    """The ``Waiting:`` line of a run whose execution is pending since *run_at*, *ahead* due runs before it."""
    due = _aware(run_at)
    if due is not None and due > now:
        return f"Waiting: not due yet, it starts at {due.isoformat()}"
    waited = int(max(0.0, (now - due).total_seconds())) if due is not None else 0
    return (
        f"Waiting: no free worker for {waited}s; position {ahead + 1} in the assignment queue "
        f"({ahead} due run(s) ahead)"
    )


async def queue_wait_line(db: AsyncSession, run_id: uuid.UUID | str, *, now: datetime | None = None) -> str | None:
    """Why the run *run_id* waits and its place in the assignment queue; ``None`` when it does not wait or on error."""
    from app.models.orchestration import OrchestrationExecution
    from app.services.orchestration_adapters.assignment import execution_dedup_key

    now = now or datetime.now(UTC)
    try:
        execution = (
            await db.execute(
                select(OrchestrationExecution).where(
                    OrchestrationExecution.dedup_key == execution_dedup_key(run_id),
                    OrchestrationExecution.state == "pending",
                )
            )
        ).scalar_one_or_none()
        if execution is None:
            return None
        ahead = 0
        due_at = _aware(execution.run_at)
        if due_at is not None and due_at <= now:
            ahead = int(
                (
                    await db.execute(
                        select(func.count())
                        .select_from(OrchestrationExecution)
                        .where(
                            *_waiting(OrchestrationExecution, now),
                            OrchestrationExecution.run_at < execution.run_at,
                        )
                    )
                ).scalar_one()
                or 0
            )
        return describe_wait(run_at=execution.run_at, now=now, ahead=ahead)
    except Exception as exc:
        logger.debug("run %s queue wait unreadable: %s", run_id, format_exception_message(exc))
        return None


async def _starvation(db: AsyncSession, now: datetime) -> tuple[int, float, int]:
    """(due runs waiting, oldest wait in seconds, runs expired before start in the lookback window)."""
    from app.models.assignment import AssignmentRun
    from app.models.orchestration import OrchestrationExecution
    from app.services.orchestration_adapters.assignment import EXPIRED_NOT_STARTED_STOP_REASON

    waiting, oldest = (
        await db.execute(
            select(func.count(), func.min(OrchestrationExecution.run_at)).where(*_waiting(OrchestrationExecution, now))
        )
    ).one()
    expired = (
        await db.execute(
            select(func.count())
            .select_from(AssignmentRun)
            .where(
                AssignmentRun.stop_reason == EXPIRED_NOT_STARTED_STOP_REASON,
                AssignmentRun.completed_at >= now - timedelta(seconds=EXPIRED_LOOKBACK_SECONDS),
            )
        )
    ).scalar_one()
    oldest_at = _aware(oldest)
    wait = max(0.0, (now - oldest_at).total_seconds()) if oldest_at is not None else 0.0
    return int(waiting or 0), wait, int(expired or 0)


async def _notify_admins(db: AsyncSession, *, title: str, summary: str, extra: dict[str, Any]) -> int:
    from app.models.user import User
    from app.services.notify import notify_web_user

    admin_ids = (
        (await db.execute(select(User.id).where(User.role == "admin", User.is_active.is_(True)))).scalars().all()
    )
    for admin_id in admin_ids:
        await notify_web_user(
            db,
            str(admin_id),
            title=title,
            summary=summary,
            source_label="Assignments",
            extra_data={"notification_source": NOTIFICATION_SOURCE, "path": "/assignments", **extra},
        )
    return len(admin_ids)


async def check_queue_starvation(db: AsyncSession, *, now: datetime | None = None, redis: Any = None) -> int:
    """Alert the administrators once per episode of assignment runs starved of workers; the alerts written.

    Never raises: the scheduler tick goes on whatever happens here. Commits what it writes.
    """
    now = now or datetime.now(UTC)
    try:
        waiting, wait, expired = await _starvation(db, now)
        if redis is None:
            from app.core.redis import redis_client

            redis = await redis_client()
        if wait < STARVATION_ALERT_SECONDS and not expired:
            await redis.delete(EPISODE_KEY)
            return 0
        opened = bool(await redis.set(EPISODE_KEY, now.isoformat(), nx=True, ex=EPISODE_TTL_SECONDS))
        if not opened:
            await redis.expire(EPISODE_KEY, EPISODE_TTL_SECONDS)
            return 0
        title = "Assignment runs are waiting for a worker"
        summary = (
            f"{waiting} due assignment run(s) wait for a free worker, the oldest for {int(wait)}s"
            + (f"; {expired} run(s) expired before any worker started them" if expired else "")
            + ". Check the workers and the queue in the run list: one alert per episode."
        )
        sent = await _notify_admins(
            db,
            title=title,
            summary=summary,
            extra={"waiting": waiting, "oldest_wait_seconds": int(wait), "expired_before_start": expired},
        )
        await db.commit()
        logger.warning("Assignment queue starvation: %s (alert sent to %d administrator(s))", summary, sent)
        return sent
    except Exception as exc:
        logger.warning("Assignment queue starvation not checked: %s", format_exception_message(exc))
        try:
            await db.rollback()
        except Exception:
            logger.debug("rollback after the starvation check failed", exc_info=True)
        return 0
