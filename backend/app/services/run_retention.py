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

"""Retention of assignment runs (Operations > Executions).

``assignment_runs`` is the largest table of the platform and had no automatic
retention: only the administrator's manual purges. The window is the
``assignment_run_days`` key of ``platform_settings.coordination_retention``
(Settings > Observability), else ``ASSIGNMENT_RUN_RETENTION_DAYS`` (90); 0 keeps
every run forever.

A run is deleted when it is finished (``assignment_engine.TERMINAL_RUN_STATUSES``)
and its last activity is older than the window. The table has no ``updated_at``:
the last activity is ``completed_at``, else ``created_at``. A run is never
deleted while it:

- is not finished (pending, delayed, queued, running, awaiting a person);
- holds the claim of a work item that is not finished;
- holds a shared-state entry as a lease;
- has a background task still pending or running under it;
- is the parent of a run that is kept (its link would be nulled). A parent and
  its children all past the window go together, so the ``SET NULL`` of the
  foreign key never reaches a run that stays.

:func:`delete_runs_cascade` is the one cascade every deletion of runs goes
through (A2A task locks, file attachments, orchestration executions, background
tasks), the administrator's purges included. A retention pass never touches an
assignment's execution counters: it is not a manual purge.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import String, cast, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.assignment import AssignmentRun

logger = logging.getLogger(__name__)

#: Runs one retention call deletes; a call that deletes this many may have left more.
RUN_RETENTION_BATCH = 2000
#: The ``system:<actor>`` the audit entry of a pass names.
RUN_RETENTION_AUDIT_ACTOR = "run_retention"
#: Background task statuses that keep the run they were spawned under.
_LIVE_TASK_STATUSES = ("pending", "running")


def _last_activity() -> Any:
    """SQL: a run's last activity, ``completed_at`` else ``created_at``."""
    return func.coalesce(AssignmentRun.completed_at, AssignmentRun.created_at)


def _expired(cutoff: datetime) -> list[Any]:
    """SQL: the run is finished and its last activity is older than *cutoff*."""
    from app.services.assignment_engine import TERMINAL_RUN_STATUSES

    return [
        AssignmentRun.status.in_(TERMINAL_RUN_STATUSES),
        # Implied by the line below; keeps the scan on ix_assignment_runs_created_at.
        AssignmentRun.created_at < cutoff,
        _last_activity() < cutoff,
    ]


def _kept(cutoff: datetime) -> list[Any]:
    """SQL: none of the reasons to keep a finished run past the window holds."""
    from app.models.shared_state import SharedState
    from app.models.sub_agent_task import SubAgentTask
    from app.models.work_item import WORK_ITEM_TERMINAL_STATUSES, WorkItem
    from app.services.assignment_engine import TERMINAL_RUN_STATUSES

    run_id_text = cast(AssignmentRun.id, String)
    child = aliased(AssignmentRun)
    return [
        ~select(WorkItem.id)
        .where(
            WorkItem.claimed_by_run_id == AssignmentRun.id,
            WorkItem.status.not_in(WORK_ITEM_TERMINAL_STATUSES),
        )
        .exists(),
        ~select(SharedState.id).where(SharedState.held_by_run_id == AssignmentRun.id).exists(),
        ~select(SubAgentTask.id)
        .where(
            SubAgentTask.status.in_(_LIVE_TASK_STATUSES),
            or_(
                SubAgentTask.metadata_json["assignment_run_id"].astext == run_id_text,
                SubAgentTask.metadata_json["parent_run_id"].astext == run_id_text,
            ),
        )
        .exists(),
        ~select(child.id)
        .where(
            child.parent_run_id == AssignmentRun.id,
            or_(
                child.status.not_in(TERMINAL_RUN_STATUSES),
                func.coalesce(child.completed_at, child.created_at) >= cutoff,
            ),
        )
        .exists(),
    ]


async def cascade_before_run_delete(db: AsyncSession, run_ids: Sequence[uuid.UUID]) -> None:
    """What every deletion of runs does before its DELETE: files, executions, background tasks.

    The file attachments of the runs and their stored objects are deleted, the
    orchestration executions still pending or running are cancelled, and the
    background tasks spawned under the runs are cancelled. Flushes, never commits.
    """
    from app.services.file_cleanup import delete_file_attachments
    from app.services.orchestration_engine import cancel_executions_for_runs
    from app.services.sub_agent import mark_sub_agent_tasks_cancelled_for_runs

    ids = [rid for rid in run_ids if rid is not None]
    if not ids:
        return
    await delete_file_attachments(db, assignment_run_id=ids)
    await cancel_executions_for_runs(db, ids)
    await mark_sub_agent_tasks_cancelled_for_runs(db, ids)


async def delete_runs_cascade(db: AsyncSession, run_ids: Sequence[uuid.UUID], *guards: Any) -> int:
    """Delete the runs *run_ids* with their cascade; answers the rows deleted.

    The A2A tasks mirroring the runs are locked before the first write (the
    order every other writer of a task and its run takes), then
    :func:`cascade_before_run_delete`, then the DELETE. *guards* are extra
    WHERE clauses the DELETE re-applies, so a run that changed since its id was
    read is left alone. Never commits: the caller owns the transaction.
    """
    from app.a2a.tasks import lock_tasks_before_deletion

    ids = [rid for rid in run_ids if rid is not None]
    if not ids:
        return 0
    await lock_tasks_before_deletion(db, run_ids=ids)
    await cascade_before_run_delete(db, ids)
    result = await db.execute(delete(AssignmentRun).where(AssignmentRun.id.in_(ids), *guards))
    return int(getattr(result, "rowcount", 0) or 0)


async def purge_expired_runs(*, older_than_days: int, limit: int = RUN_RETENTION_BATCH) -> int:
    """Delete up to *limit* finished runs past *older_than_days*, oldest first.

    ``<= 0`` keeps every run and opens no session. The DELETE re-applies the
    status, the window and every keep rule, so a run resumed, claimed or given a
    child between the id SELECT and the DELETE is never removed. Answers the
    rows deleted: a result equal to *limit* means more may be left.
    """
    from app.core.database import async_session

    if not older_than_days or older_than_days <= 0:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    rules = [*_expired(cutoff), *_kept(cutoff)]
    async with async_session() as db:
        run_ids = (
            (
                await db.execute(
                    select(AssignmentRun.id).where(*rules).order_by(AssignmentRun.created_at.asc()).limit(max(1, limit))
                )
            )
            .scalars()
            .all()
        )
        if not run_ids:
            return 0
        purged = await delete_runs_cascade(db, list(run_ids), *rules)
        await db.commit()
    return purged


async def _audit_anchor(db: AsyncSession) -> uuid.UUID | None:
    """The administrator the audit entry of a pass is anchored on: the oldest active one."""
    from app.models.user import User

    return (
        await db.execute(
            select(User.id)
            .where(User.role == "admin", User.is_active.is_(True))
            .order_by(User.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def audit_run_retention_pass(deleted: int, days: int) -> None:
    """One platform audit entry for a pass that deleted runs (count and window), none per run."""
    from app.core.database import async_session
    from app.services.audit import log_system_audit

    if deleted <= 0:
        return
    async with async_session() as db:
        anchor = await _audit_anchor(db)
        if anchor is None:
            logger.warning("Run retention deleted %d run(s) and found no administrator to audit it on", deleted)
            return
        await log_system_audit(
            db,
            actor=RUN_RETENTION_AUDIT_ACTOR,
            anchor_user_id=anchor,
            action="retention_purge",
            entity_type="assignment_run",
            summary=f"Deleted {deleted} finished run(s) past the {days}-day run retention",
            details={"deleted": deleted, "retention_days": days},
        )
        await db.commit()
