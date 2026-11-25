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

"""A scheduled assignment is never starved in silence by a run of its own.

The scheduler (``autonomous._process_scheduled_assignments``) does not start a
fire while a run of the assignment is active (``has_active_run``): the overlap
guard. A run resumed again and again across rollouts could stay active for
hours, its per-attempt timeout restarting at every resume, and every fire in
between would be lost without a trace.

Four rules close it:

* a fire whose previous fire is still waiting for a worker and never started
  supersedes it (:func:`supersede_never_started`, #2040): the stale fire ends
  ``skipped`` (``stop_reason`` ``superseded``) and the new one is created, so a
  saturated queue costs a monitor one pass, never two;
* a due fire dropped by the overlap guard is recorded as a ``skipped`` run
  whose summary names the run in the way, its status and since when
  (:func:`skipped_fire_run`);
* the assignment's timeout bounds the run's wall time across its resumes: the
  first ``started_at`` is kept by every resume, and a resume past the
  effective timeout (the floored ``timeout_seconds``, as its orchestration
  deadline) fails the run instead (:func:`wall_time_refusal`);
* a run with a fire of its schedule due behind it is given
  :data:`STARVING_GRACEFUL_STREAK` graceful resumes in a row with no tool call
  completed between them, not ``MAX_GRACEFUL_STREAK``
  (:func:`graceful_streak_bound`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.core.utils import next_cron_run, schedule_timezone
from app.services.background_checkpoint import MAX_GRACEFUL_STREAK, RESUME_CAP_REACHED

#: The graceful resumes in a row with no progress a run may make while a fire
#: of its schedule waits behind it: it ends at its second such take-back.
STARVING_GRACEFUL_STREAK = 1

#: Where a skipped fire names the run that was in its way.
SKIPPED_BEHIND_KEY = "skipped_behind_run"

#: The opening words of a run failed for its wall time across resumes.
WALL_TIME_EXCEEDED = "Assignment timed out across its resumes"

#: The ``stop_reason`` of a fire that waited for a worker, never started, and
#: was replaced by the next fire of its schedule (#2040).
SUPERSEDED_STOP_REASON = "superseded"

#: The run statuses of a fire that may still be superseded: waiting, never started.
SUPERSEDABLE_STATUSES = frozenset({"pending", "delayed"})


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def fire_due_behind(assignment: Any, run: Any, *, now: datetime | None = None) -> bool:
    """Whether a fire of *assignment*'s schedule fell due after *run* was created.

    Only a ``schedule`` assignment with a readable cron has one; its first fire
    after the run's ``created_at`` (in the schedule's zone) at or before *now*.
    """
    if getattr(assignment, "trigger_type", None) != "schedule":
        return False
    config = getattr(assignment, "trigger_config", None)
    cron = config.get("cron") if isinstance(config, dict) else None
    created = _aware(getattr(run, "created_at", None))
    if not isinstance(cron, str) or created is None:
        return False
    zone, _ = schedule_timezone(config)
    due = next_cron_run(cron, created, tz=zone)
    return due is not None and due <= (now or datetime.now(UTC))


def graceful_streak_bound(assignment: Any, run: Any, *, now: datetime | None = None) -> int:
    """The graceful resumes in a row with no progress *run* may still make.

    :data:`STARVING_GRACEFUL_STREAK` when a fire of its schedule is due behind
    it, ``MAX_GRACEFUL_STREAK`` otherwise.
    """
    return STARVING_GRACEFUL_STREAK if fire_due_behind(assignment, run, now=now) else MAX_GRACEFUL_STREAK


def starving_note(bound: int) -> str:
    """What a graceful cap text adds when the tighter bound ended the run."""
    return (
        f" A fire of its schedule is due behind it, so it was given {bound} graceful resume(s) in a row "
        f"with no tool call completed, not {MAX_GRACEFUL_STREAK}."
    )


def wall_time_refusal(
    run: Any, *, effective_timeout: float | None, now: datetime | None = None
) -> str | None:
    """Why a resumed *run* is not resumed again: its wall time is past its timeout, else ``None``.

    *effective_timeout* is the run's floored timeout in seconds (``None`` when
    the assignment's timeout is disabled). The wall time runs from the first
    ``started_at``, which every resume keeps, less the time it waited for a
    person (#1645): a HITL pause is not run time.
    """
    from app.services.hitl_resume_clock import awaiting_human_seconds

    started = _aware(getattr(run, "started_at", None))
    if effective_timeout is None or effective_timeout <= 0 or started is None:
        return None
    elapsed = ((now or datetime.now(UTC)) - started).total_seconds() - (awaiting_human_seconds(run) or 0.0)
    if elapsed <= effective_timeout:
        return None
    return (
        f"{RESUME_CAP_REACHED}: {WALL_TIME_EXCEEDED}: it started at {started.isoformat()} and has run "
        f"{int(elapsed)}s across its resumes, past its timeout of {int(effective_timeout)}s. It is failed "
        "rather than resumed once more, and its work item claims are released, so the next fire of its "
        "assignment may run."
    )


def superseded_summary(*, now: datetime) -> str:
    """The summary of a fire replaced by the next one before any worker started it."""
    return (
        f"Scheduled fire superseded: it waited for a free worker and never started; the fire of "
        f"{now.isoformat()} replaces it."
    )


async def supersede_never_started(db: Any, run: Any, *, now: datetime | None = None) -> bool:
    """End *run*, a fire still waiting for a worker, so the next fire of its schedule runs instead (#2040).

    Only a run that never started is superseded: status ``pending`` or ``delayed``,
    no ``started_at``, and its dispatch execution still ``pending`` with no start
    recorded, locked ``FOR UPDATE SKIP LOCKED`` so a worker claiming it at the
    same moment keeps it (``False``, the caller skips the new fire as before).
    A run handed back by a stopping worker carries its first start and is
    never superseded. The execution is cancelled and the run ends ``skipped``
    with ``stop_reason`` :data:`SUPERSEDED_STOP_REASON`; flushed, not committed.
    """
    from sqlalchemy import select

    from app.models.orchestration import OrchestrationExecution
    from app.services.orchestration_adapters.assignment import execution_dedup_key
    from app.services.orchestration_engine import STARTED_AT_PAYLOAD_KEY

    if getattr(run, "status", None) not in SUPERSEDABLE_STATUSES or getattr(run, "started_at", None) is not None:
        return False
    now = now or datetime.now(UTC)
    execution = (
        await db.execute(
            select(OrchestrationExecution)
            .where(
                OrchestrationExecution.dedup_key == execution_dedup_key(run.id),
                OrchestrationExecution.state == "pending",
            )
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if execution is None or (execution.payload or {}).get(STARTED_AT_PAYLOAD_KEY):
        return False
    execution.state = "cancelled"
    execution.lease_owner = None
    execution.lease_expires_at = None
    execution.last_error = "Superseded by the next fire of its schedule before any worker started it."
    execution.updated_at = now
    run.status = "skipped"
    run.stop_reason = SUPERSEDED_STOP_REASON
    run.result_summary = superseded_summary(now=now)
    run.completed_at = now
    await db.flush()
    return True


def skipped_fire_summary(active_run: Any, *, now: datetime | None = None) -> str:
    """The summary of a scheduled fire dropped because *active_run* is in the way."""
    since = _aware(getattr(active_run, "started_at", None) or getattr(active_run, "created_at", None))
    status = getattr(active_run, "status", None) or "active"
    when = f" since {since.isoformat()}" if since is not None else ""
    age = ""
    if since is not None:
        age = f" ({int(((now or datetime.now(UTC)) - since).total_seconds() // 60)} min)"
    return f"Scheduled fire skipped: previous run {getattr(active_run, 'id', '?')} still {status}{when}{age}."


def skipped_fire_run(
    assignment: Any, active_run: Any, *, cron: str, now: datetime | None = None
) -> dict[str, Any]:
    """The columns of the ``skipped`` run recorded for a dropped fire.

    The shape of a schedule run (``trigger_type`` / ``scheduled_at`` in the
    payload, ``type`` / ``cron`` / ``scheduled_at`` / ``triggered_at`` in the
    context) plus :data:`SKIPPED_BEHIND_KEY`, never executed (no orchestration
    execution is created for it).
    """
    now = now or datetime.now(UTC)
    stamp = now.isoformat()
    return {
        "assignment_id": assignment.id,
        "agent_id": assignment.agent_id,
        "user_id": getattr(assignment, "run_as_user_id", None) or assignment.user_id,
        "is_company_managed": assignment.is_company_managed,
        "trigger_payload": {"trigger_type": "schedule", "scheduled_at": stamp},
        "trigger_context": {
            "type": "schedule",
            "cron": cron,
            "scheduled_at": stamp,
            "triggered_at": stamp,
            SKIPPED_BEHIND_KEY: str(getattr(active_run, "id", "")),
        },
        "status": "skipped",
        "result_summary": skipped_fire_summary(active_run, now=now),
        "started_at": now,
        "completed_at": now,
    }
