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

"""Retire an assignment once its runs have ended (``delete_assignment(after_run=true)``).

``assignment_runs.assignment_id`` cascades on delete, so ``delete_assignment``
refuses while a run of the assignment is in flight: the run would be deleted
under itself and end "no longer recorded". That refusal also made it
impossible for a one-off assignment to retire itself from its own run, the
one moment it knows its work is done.

``after_run=true`` turns the refusal into a deferred deletion:

* **Now**, in the caller's transaction: the assignment is paused with reason
  :data:`RETIRE_AFTER_RUN_REASON` (:func:`mark_retire_after_run`), so no new
  run starts and the queued deliveries are abandoned by the queue drain.
* **On the scheduler tick** (``autonomous``, next to the claim and lease
  releases): :func:`retire_assignments_after_runs` deletes every assignment
  carrying that pause once none of its runs is active or queued.

The runs outlive the assignment: before the delete, every run of the
assignment (all terminal by then) is detached (``assignment_id`` set to NULL,
:func:`detach_runs_clause`), so the ``ON DELETE CASCADE`` reaches none of them
and ``get_run`` / ``list_runs`` keep answering the run that retired it, with
its status and result. The audit row of the deletion names the runs kept.

The mark lives on the row, so it survives a rollout and a restart. Resuming
the assignment (the power toggle, ``update_assignment(enabled=true)``) clears
the pause and with it the retirement.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy import and_, exists, select, update

from app.models.assignment import Assignment, AssignmentRun
from app.services.assignment_pause import pause_assignment

logger = logging.getLogger(__name__)

#: ``Assignment.paused_reason`` of an assignment waiting to be deleted once
#: its runs end. A key of ``assignment_pause.PAUSE_REASONS``.
RETIRE_AFTER_RUN_REASON = "retire_after_run"

#: The system actor of the deletion's audit row.
RETIRE_ACTOR = "assignment-retire-after-run"

#: How many kept run ids the deletion's audit row lists.
_KEPT_RUNS_LISTED = 20


def _blocking_statuses() -> list[str]:
    """The run statuses that keep a retiring assignment: active and queued."""
    from app.services.assignment_engine import ACTIVE_RUN_STATUSES, QUEUED_RUN_STATUS

    return [*ACTIVE_RUN_STATUSES, QUEUED_RUN_STATUS]


def _has_blocking_run_clause():
    """SQL: the assignment has a run still active or queued."""
    return exists(
        select(AssignmentRun.id).where(
            AssignmentRun.assignment_id == Assignment.id,
            AssignmentRun.status.in_(_blocking_statuses()),
        )
    )


def retiring_clause():
    """SQL: the assignment is marked for retirement and none of its runs is left."""
    return and_(
        Assignment.enabled.is_(False),
        Assignment.paused_reason == RETIRE_AFTER_RUN_REASON,
        ~_has_blocking_run_clause(),
    )


def detach_runs_clause(assignment_id: uuid.UUID):
    """SQL: detach the runs of *assignment_id* so its deletion keeps them.

    Answers the ids of the runs detached (``RETURNING``).
    """
    return (
        update(AssignmentRun)
        .where(AssignmentRun.assignment_id == assignment_id)
        .values(assignment_id=None)
        .returning(AssignmentRun.id)
        .execution_options(synchronize_session=False)
    )


def mark_retire_after_run(
    assignment: Assignment,
    *,
    actor_user_id: uuid.UUID | str | None,
    actor_label: str,
    run_ids: list[uuid.UUID] | None = None,
    now: datetime | None = None,
) -> str:
    """Pause *assignment* for deletion once its runs end; the details written.

    Pure in-session mutation, as :func:`pause_assignment`: the caller commits.
    """
    shown = ", ".join(str(rid) for rid in (run_ids or [])[:5])
    details = (
        f"Deletion scheduled by {actor_label} once its run(s) in flight end"
        + (f" ({shown})" if shown else "")
        + ". Resume it to cancel the deletion."
    )
    pause_assignment(
        assignment,
        reason=RETIRE_AFTER_RUN_REASON,
        details=details,
        actor_user_id=actor_user_id,
        actor_label=actor_label,
        now=now,
    )
    return details


async def retire_assignments_after_runs(*, limit: int = 200) -> int:
    """Delete every assignment marked for retirement whose runs have all ended.

    Each row is re-read under ``FOR UPDATE SKIP LOCKED`` with the same
    predicate, so an assignment resumed, or given a new run, between the read
    and the delete is kept. One commit per assignment: a failure on one leaves
    the others deleted. Returns how many were deleted.
    """
    from app.core.database import async_session
    from app.services.audit import log_system_audit

    deleted = 0
    async with async_session() as db:
        ids = (
            await db.execute(
                select(Assignment.id)
                .where(retiring_clause())
                .order_by(Assignment.paused_at.asc())
                .limit(max(int(limit), 1))
            )
        ).scalars().all()
        for assignment_id in ids:
            try:
                assignment = (
                    await db.execute(
                        select(Assignment)
                        .where(Assignment.id == assignment_id, retiring_clause())
                        .with_for_update(skip_locked=True)
                    )
                ).scalar_one_or_none()
                if assignment is None:
                    await db.rollback()
                    continue
                name = assignment.name
                kept_runs = (
                    (await db.execute(detach_runs_clause(assignment.id)))
                    .scalars()
                    .all()
                )
                if assignment.user_id is not None:
                    await log_system_audit(
                        db,
                        actor=RETIRE_ACTOR,
                        anchor_user_id=assignment.user_id,
                        action="delete",
                        entity_type="assignment",
                        entity_id=assignment.id,
                        entity_name=name,
                        summary=f"Deleted assignment '{name}' after its last run ended",
                        details={
                            "agent_id": str(assignment.agent_id),
                            "trigger_type": assignment.trigger_type,
                            "reason": RETIRE_AFTER_RUN_REASON,
                            "scheduled_by": assignment.paused_by_label,
                            "runs_kept": len(kept_runs),
                            "run_ids": [
                                str(rid) for rid in kept_runs[:_KEPT_RUNS_LISTED]
                            ],
                        },
                    )
                # A deleted seeded fleet health check stays deleted: its fleet opts out.
                from app.services.fleets import record_health_check_deleted

                await record_health_check_deleted(db, assignment, actor=assignment.paused_by_label)
                await db.delete(assignment)
                await db.commit()
                deleted += 1
                logger.info(
                    "Assignment '%s' (%s) deleted: retirement after its last run",
                    name,
                    assignment_id,
                )
            except Exception:
                logger.exception("Retiring assignment %s failed", assignment_id)
                await db.rollback()
    return deleted
