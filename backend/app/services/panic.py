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

"""Emergency stop — halt every agentic activity in a scope, at once.

The profile menu's "panic button". Two scopes:

* ``self`` — everything that runs *for* the acting user: their runs (including
  runs a company-managed assignment executes as them), their background
  tasks, their in-flight chat turns, their flow runs, the runner commands they
  dispatched, and the assignments they own or that run as them.
* ``platform`` (administrators only) — the same sweep over every user, plus
  the surfaces that have no owner (evaluation and autonomous pentest runs),
  plus the platform AI kill switch (``ai_state``) so nothing new starts until
  an administrator turns it back on.

Nothing here invents a mechanism. Each arm reuses the primitive the matching
"cancel" endpoint already uses — the A2A cascade for run trees, the engine's
execution cancel, the Redis cross-process chat cancel, the flow-run cancel
recipe, the pause provenance module — so the panic button stops things the
same way, and leaves the same records, as stopping them one by one would.

Three records survive the stop:

* Every paused assignment carries ``paused_reason = "panic_stop"``, which is
  what makes :func:`resume_after_emergency_stop` able to re-enable exactly
  what the stop switched off and nothing a human had paused on purpose.
* An open platform stop's state on ``PlatformSettings`` (when, which pause
  stamps, the AI override to restore) until the platform resume clears it:
  not the audit rows, which cascade with the user who pressed the stop.
* One ``audit_logs`` row per stop / resume with the per-kind counts, on top
  of the per-assignment ``pause`` / ``resume`` rows the provenance module
  writes anyway.

Transaction contract: the ``emergency_stop`` / ``resume_after_emergency_stop``
coroutines flush but never commit — the router commits, then calls
:func:`after_commit` for the work that must only happen once the rows are
durable (cache invalidation, partner notifications, SSE frames). A cancel
told to a remote partner cannot be rolled back, so it must not go out
before the local rows that justify it are committed.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import Request
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import chunked, request_client_ip, safe_uuid
from app.models.assignment import Assignment, AssignmentRun
from app.models.user import User
from app.services.assignment_pause import (
    PANIC_STOP_REASON,
    actor_display_name,
    pause_assignment,
    record_pause_audits,
    resume_assignment,
)

logger = logging.getLogger(__name__)

PanicScope = Literal["self", "platform"]


class PlatformStopActiveError(Exception):
    """A self-scope resume was asked while a platform-wide stop is in effect."""

#: What lands in ``error`` / ``paused_details`` on everything the stop touches.
STOP_MESSAGE = "Stopped by an emergency stop."

#: Audit actions — one vocabulary for the stop, the resume and the log reader.
AUDIT_ACTION_STOP = "panic_stop"
AUDIT_ACTION_RESUME = "panic_resume"

_ID_CHUNK = 1000

# Strong references to detached follow-up tasks (asyncio keeps weak ones).
_background: set[asyncio.Task] = set()


@dataclass
class EmergencyStopOutcome:
    """What one stop did — the counts the UI shows, and the after-commit work."""

    scope: PanicScope
    runs_cancelled: int = 0
    executions_cancelled: int = 0
    sub_agent_tasks_cancelled: int = 0
    chat_turns_cancelled: int = 0
    flow_runs_cancelled: int = 0
    runner_commands_cancelled: int = 0
    evaluation_runs_cancelled: int = 0
    autonomous_runs_cancelled: int = 0
    assignments_paused: int = 0
    ai_disabled: bool = False
    #: ``False`` when cancelling the live work failed after the barrier was
    #: committed (:func:`cancel_live_work`): the counts are what the barrier
    #: and the post-commit sweeps did, not the whole stop.
    completed: bool = True
    #: Runs cancelled by the post-commit sweep — admitted while the stop was
    #: in flight. Already included in ``runs_cancelled``; reported apart so an
    #: operator knows a second wave happened.
    late_admissions_cancelled: int = 0
    #: The AI override as it was BEFORE the stop flipped it (wire tri-state:
    #: ``True`` / ``False`` / ``None`` = follow the deployment mode). Recorded
    #: on the audit row so the resume puts back what the administrator had.
    ai_override_before_stop: bool | None = None
    #: The ``panic_stop`` audit row — what :func:`record_stop_reason` annotates
    #: once the operator has said why, after the stop itself is done.
    audit_id: uuid.UUID | None = None
    stopped_at: datetime | None = None
    #: Who pressed it and whose activity was in scope (``None`` = platform):
    #: what the post-commit admission sweep needs to find late runs.
    actor_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    #: When the first post-commit sweep started (it stamps it). A ``self``
    #: stop does not gate new work, so a later pass bounds itself on it
    #: (:func:`_self_stop_later_pass_filter`).
    first_swept_at: datetime | None = None

    # After-commit work, not part of the wire response.
    partner_cancel_run_ids: list[uuid.UUID] = field(default_factory=list, repr=False)
    cancelled_conversation_ids: list[str] = field(default_factory=list, repr=False)
    cancelled_flow_runs: list[Any] = field(default_factory=list, repr=False)
    finished_runner_command_ids: list[uuid.UUID] = field(
        default_factory=list, repr=False
    )

    def counts(self) -> dict[str, int | bool | str]:
        return {
            "scope": self.scope,
            "runs_cancelled": self.runs_cancelled,
            "executions_cancelled": self.executions_cancelled,
            "sub_agent_tasks_cancelled": self.sub_agent_tasks_cancelled,
            "chat_turns_cancelled": self.chat_turns_cancelled,
            "flow_runs_cancelled": self.flow_runs_cancelled,
            "runner_commands_cancelled": self.runner_commands_cancelled,
            "evaluation_runs_cancelled": self.evaluation_runs_cancelled,
            "autonomous_runs_cancelled": self.autonomous_runs_cancelled,
            "assignments_paused": self.assignments_paused,
            "ai_disabled": self.ai_disabled,
        }

    def response(self) -> dict[str, Any]:
        return {
            **self.counts(),
            "late_admissions_cancelled": self.late_admissions_cancelled,
            "completed": self.completed,
            "audit_id": str(self.audit_id) if self.audit_id else None,
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
        }


@dataclass
class ResumeOutcome:
    scope: PanicScope
    reason: str = ""
    assignments_resumed: int = 0
    ai_enabled: bool = False

    def counts(self) -> dict[str, int | bool | str]:
        return {
            "scope": self.scope,
            "assignments_resumed": self.assignments_resumed,
            "ai_enabled": self.ai_enabled,
        }


def _owner_for(actor: User, scope: PanicScope) -> uuid.UUID | None:
    """``None`` means "every user" — the platform scope."""
    if scope == "platform":
        if not actor.is_admin:
            raise PermissionError("The platform scope requires an administrator")
        return None
    return actor.id


def _assignment_scope_filter(owner: uuid.UUID | None):
    """Assignments in scope: the ones the user owns AND the company-managed ones
    that execute as them (``run_as_user_id``) — both fire work under their
    identity, which is what "everything that concerns me" means here.
    """
    if owner is None:
        return None
    return or_(Assignment.user_id == owner, Assignment.run_as_user_id == owner)


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------


async def arm_admission_barrier(
    db: AsyncSession,
    *,
    actor: User,
    scope: PanicScope,
    request: Request | None = None,
) -> EmergencyStopOutcome:
    """Phase 1 of the stop: close the door before clearing the room.

    Pauses every assignment in scope and, for the platform scope, flips the
    AI kill switch — and RETURNS so the caller commits this on its own,
    before :func:`emergency_stop` selects the work to cancel. Once this is
    committed, a scheduler tick or a trigger on any replica meets a paused
    assignment (and a refused AI gate) and admits nothing new, so what the
    second phase selects is the complete set of live work. Doing both in one
    transaction left a window between the SELECT and the commit where a
    concurrent admission was only caught by the best-effort sweep.

    The outcome carries ``stopped_at`` (the assignments' ``paused_at`` and
    the audit join key) and the counts of this phase; the caller passes it
    into :func:`emergency_stop`.
    """
    owner = _owner_for(actor, scope)
    await serialize_stop_transitions(db)
    now = datetime.now(UTC)
    outcome = EmergencyStopOutcome(
        scope=scope, stopped_at=now, actor_id=actor.id, owner_id=owner
    )
    if owner is None:
        await _disable_platform_ai(db, actor=actor, outcome=outcome, request=request)
        await _open_platform_stop(
            db, stopped_at=now, ai_override_before_stop=outcome.ai_override_before_stop
        )
    actor_label = actor_display_name(actor)
    await _pause_assignments(
        db,
        owner=owner,
        actor=actor,
        actor_label=actor_label,
        scope=scope,
        now=now,
        outcome=outcome,
        request=request,
    )
    # The ``panic_stop`` row is written HERE, in the phase the caller commits
    # first, with the platform stop's state above: it is what
    # ``record_stop_reason`` annotates, what a ``self`` resume keys on and what
    # the banner names. Written only in phase 2, a failure between the two
    # commits would leave AI off, assignments paused and no trail of the stop.
    # Phase 2 completes its counts.
    from app.services.audit import log_audit

    entry = await log_audit(
        db,
        user=actor,
        action=AUDIT_ACTION_STOP,
        entity_type="platform" if owner is None else "user",
        entity_id=None if owner is None else actor.id,
        entity_name=None if owner is None else actor.email,
        summary=(
            "Emergency stop: halted every agentic activity on the platform"
            if owner is None
            else f"Emergency stop: halted every agentic activity of {actor_label}"
        ),
        # ``stopped_at`` is the join key ``record_stop_reason`` uses to find the
        # assignments THIS stop paused (their ``paused_at`` carries it).
        details={
            **outcome.counts(),
            "stopped_at": now.isoformat(),
            "reason": None,
            "phase": "armed",
            # What the resume restores: an administrator who had AI off (or
            # explicitly on) before the stop gets that back, not "deployment
            # default".
            "ai_override_before_stop": outcome.ai_override_before_stop,
        },
        request=request,
    )
    outcome.audit_id = entry.id
    return outcome


async def emergency_stop(
    db: AsyncSession,
    *,
    actor: User,
    scope: PanicScope,
    request: Request | None = None,
    outcome: EmergencyStopOutcome | None = None,
) -> EmergencyStopOutcome:
    """Phase 2: cancel everything active in *scope*.

    Called with the ``outcome`` of a COMMITTED :func:`arm_admission_barrier`
    (the router's two-transaction shape). Without one — tests, callers that
    accept the single-transaction window — it arms the barrier itself in the
    same transaction. Runs go before their dependants because the sub-agent
    and partner arms are scoped by run id.
    """
    owner = _owner_for(actor, scope)
    if outcome is None:
        outcome = await arm_admission_barrier(
            db, actor=actor, scope=scope, request=request
        )
    now = outcome.stopped_at or datetime.now(UTC)

    await _cancel_runs(db, owner=owner, now=now, outcome=outcome)
    await _cancel_background_tasks(db, owner=owner, outcome=outcome)
    await _cancel_chat_turns(db, owner=owner, outcome=outcome)
    # Commands before flow runs: giving one up tells the flow event bus, and a
    # listener run that starts on it is then cancelled with the others.
    await _cancel_runner_commands(db, owner=owner, outcome=outcome)
    await _cancel_flow_runs(db, owner=owner, now=now, outcome=outcome)
    if owner is None:
        await _cancel_platform_only(db, now=now, outcome=outcome)

    # Complete the row phase 1 wrote with what this phase cancelled.
    if outcome.audit_id is not None:
        from app.models.audit_log import AuditLog

        entry = (
            await db.execute(select(AuditLog).where(AuditLog.id == outcome.audit_id))
        ).scalar_one_or_none()
        if entry is not None:
            entry.details = {
                **(entry.details or {}),
                **outcome.counts(),
                "phase": "completed",
            }
            await db.flush()
    logger.warning(
        "EMERGENCY STOP by %s (scope=%s): %s", actor.email, scope, outcome.counts()
    )
    return outcome


async def cancel_live_work(
    db: AsyncSession,
    *,
    actor: User,
    scope: PanicScope,
    outcome: EmergencyStopOutcome,
    request: Request | None = None,
) -> EmergencyStopOutcome:
    """Phase 2 after a COMMITTED barrier, committed here, never lost to an error.

    The barrier is durable by then (assignments paused, AI off, the trail
    open), so a failure here must not skip :func:`after_commit` with it: the
    caller still runs it, and its sweeps cancel the runs, sub-flows, tasks
    and chat turns this phase did not reach. What it did reach is rolled
    back, so the returned outcome keeps the barrier's counts plus the chat
    cancels already asked (Redis, not rolled back), reports
    ``completed = False`` and marks the trail ``incomplete``.
    """
    barrier = _copy_outcome(outcome)
    try:
        outcome = await emergency_stop(
            db, actor=actor, scope=scope, request=request, outcome=outcome
        )
        await db.commit()
        return outcome
    except Exception:
        logger.exception(
            "Emergency stop by %s (scope=%s): cancelling the live work failed after "
            "the barrier was committed; the post-commit sweeps take over",
            actor.email,
            scope,
        )
        await db.rollback()
    barrier.completed = False
    barrier.chat_turns_cancelled = outcome.chat_turns_cancelled
    barrier.cancelled_conversation_ids = list(outcome.cancelled_conversation_ids)
    if barrier.audit_id is not None:
        try:
            from app.models.audit_log import AuditLog

            entry = (
                await db.execute(select(AuditLog).where(AuditLog.id == barrier.audit_id))
            ).scalar_one_or_none()
            if entry is not None:
                entry.details = {**(entry.details or {}), "phase": "incomplete"}
                await db.commit()
        except Exception:
            logger.warning(
                "Emergency stop: could not mark the trail incomplete", exc_info=True
            )
            await db.rollback()
    return barrier


def _copy_outcome(outcome: EmergencyStopOutcome) -> EmergencyStopOutcome:
    """A copy whose lists phase 2 cannot extend in place."""
    from dataclasses import replace

    return replace(
        outcome,
        partner_cancel_run_ids=list(outcome.partner_cancel_run_ids),
        cancelled_conversation_ids=list(outcome.cancelled_conversation_ids),
        cancelled_flow_runs=list(outcome.cancelled_flow_runs),
        finished_runner_command_ids=list(outcome.finished_runner_command_ids),
    )


async def record_stop_reason(
    db: AsyncSession,
    *,
    actor: User,
    audit_id: uuid.UUID,
    reason: str,
) -> int:
    """Attach the operator's stated reason to a stop that already happened.

    An emergency stop must not wait for a form, so the reason is asked right
    after the stop and written here: onto the ``panic_stop`` audit row (its
    ``details.reason`` and, for the list view, its summary) and onto the
    ``paused_details`` of every assignment that stop paused, so the "Paused"
    badge on each of them explains itself. Only the actor of the stop may
    annotate it. Flushes; the caller commits. Returns the number of
    assignments annotated; raises ``LookupError`` when the row is not theirs.
    """
    from app.models.audit_log import AuditLog

    entry = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.id == audit_id,
                AuditLog.action == AUDIT_ACTION_STOP,
                AuditLog.user_id == actor.id,
            )
        )
    ).scalar_one_or_none()
    if entry is None:
        raise LookupError("No emergency stop of yours carries this id")
    reason = reason.strip()
    details = dict(entry.details or {})
    details["reason"] = reason
    details["reason_recorded_at"] = datetime.now(UTC).isoformat()
    entry.details = details  # a new dict, so the JSON column sees the change
    base_summary = entry.summary.split(" — reason: ", 1)[0]
    entry.summary = f"{base_summary} — reason: {reason}"

    annotated = 0
    stopped_at_raw = details.get("stopped_at")
    if stopped_at_raw:
        stopped_at = datetime.fromisoformat(stopped_at_raw)
        rows = (
            await db.execute(
                select(Assignment).where(
                    Assignment.paused_reason == PANIC_STOP_REASON,
                    Assignment.paused_by_user_id == actor.id,
                    Assignment.paused_at == stopped_at,
                )
            )
        ).scalars().all()
        for assignment in rows:
            current = (assignment.paused_details or "").split(" Reason: ", 1)[0]
            assignment.paused_details = f"{current} Reason: {reason}"
            annotated += 1
    await db.flush()
    logger.info(
        "Emergency stop %s annotated by %s (%d assignment(s)): %s",
        audit_id,
        actor.email,
        annotated,
        reason,
    )
    return annotated


async def _disable_platform_ai(
    db: AsyncSession,
    *,
    actor: User,
    outcome: EmergencyStopOutcome,
    request: Request | None,
) -> None:
    """Flip the runtime kill switch — the only thing that stops NEW work.

    Same write and same audit row as the Settings toggle, so the log reader
    sees one story whichever button was pressed. The cache is invalidated by
    :func:`after_commit`: doing it here would let a concurrent request re-cache
    the pre-commit value for a whole TTL.
    """
    from app.services import ai_state
    from app.services.audit import log_audit

    previous = await ai_state.set_ai_enabled(db, False)
    outcome.ai_disabled = True
    outcome.ai_override_before_stop = previous
    await log_audit(
        db,
        user=actor,
        action="toggle",
        entity_type="general_assistant",
        entity_name="Platform Settings",
        summary="AI disabled (emergency stop)",
        details={
            "setting": "ai_enabled",
            "from": previous,
            "to": False,
            "resolved": False,
            "reason": AUDIT_ACTION_STOP,
        },
        request=request,
    )


async def _cancel_runs(
    db: AsyncSession,
    *,
    owner: uuid.UUID | None,
    now: datetime,
    outcome: EmergencyStopOutcome,
    message: str = STOP_MESSAGE,
) -> None:
    """Every non-terminal run in scope, then the tree under each.

    ``queued`` is included on purpose: it is not in ``ACTIVE_RUN_STATUSES``
    (a queued run owns no execution) but the reconciler would promote it to
    ``pending`` as soon as the assignment goes idle — which the stop makes
    happen for every assignment at once.
    """
    from app.a2a.tasks import cascade_cancel
    from app.services.assignment_engine import ACTIVE_RUN_STATUSES, QUEUED_RUN_STATUS
    from app.services.orchestration_engine import cancel_executions_for_runs

    stoppable = (*ACTIVE_RUN_STATUSES, QUEUED_RUN_STATUS)
    query = select(AssignmentRun.id).where(AssignmentRun.status.in_(stoppable))
    if owner is not None:
        query = query.where(AssignmentRun.user_id == owner)
    roots = list((await db.execute(query)).scalars().all())
    if not roots:
        return

    # ``error`` carries the human-readable cause: the run card and the run
    # page show it next to the ``cancelled`` status, so nobody has to guess
    # why a run stopped mid-way.
    for batch in chunked(roots, _ID_CHUNK):
        result = await db.execute(
            update(AssignmentRun)
            .where(
                AssignmentRun.id.in_(batch),
                AssignmentRun.status.in_(stoppable),
            )
            .values(status="cancelled", completed_at=now, error=message)
        )
        outcome.runs_cancelled += result.rowcount or 0

    # Children (transfer runs) + the sub-agent tasks linked to the tree. The
    # cascade cancels ``pending / delayed / running`` children; the second
    # UPDATE catches the ``awaiting_human`` / ``queued`` ones an ordinary
    # cancel leaves alone but an emergency stop must not.
    tree = await cascade_cancel(db, roots)
    root_ids = set(roots)
    descendants = [rid for rid in tree if rid not in root_ids]
    for batch in chunked(descendants, _ID_CHUNK):
        result = await db.execute(
            update(AssignmentRun)
            .where(
                AssignmentRun.id.in_(batch),
                AssignmentRun.status.in_(stoppable),
            )
            .values(status="cancelled", completed_at=now, error=message)
        )
        outcome.runs_cancelled += result.rowcount or 0
        # The cascade already flipped the pending / running descendants; give
        # those the same cause (only rows it cancelled just now, none older).
        await db.execute(
            update(AssignmentRun)
            .where(
                AssignmentRun.id.in_(batch),
                AssignmentRun.status == "cancelled",
                AssignmentRun.error.is_(None),
                AssignmentRun.completed_at >= now,
            )
            .values(error=message)
        )
    # The cascade cancels the DESCENDANTS' executions; the roots' own
    # executions (``assignment_run:<id>`` / ``payload.run_id``) are cancelled
    # here so the reconciler frees their slots and fires their cancel_event.
    outcome.executions_cancelled += await cancel_executions_for_runs(db, tree)
    # ``roots`` is EVERY active run in scope, children included, and the
    # cascade appends each root's descendants — so a child appears twice.
    # One cancel per partner task, never one per path that reached it.
    outcome.partner_cancel_run_ids = list(dict.fromkeys(tree))
    await db.flush()


async def _cancel_background_tasks(
    db: AsyncSession,
    *,
    owner: uuid.UUID | None,
    outcome: EmergencyStopOutcome,
    message: str = STOP_MESSAGE,
) -> None:
    from app.services.sub_agent import cancel_sub_agent_tasks_for_scope

    tasks, executions = await cancel_sub_agent_tasks_for_scope(
        db, user_id=owner, error=message
    )
    outcome.sub_agent_tasks_cancelled += tasks
    outcome.executions_cancelled += executions


async def _cancel_chat_turns(
    db: AsyncSession, *, owner: uuid.UUID | None, outcome: EmergencyStopOutcome
) -> None:
    """Ask every in-flight chat turn in scope to stop, wherever it runs.

    The run row flip in :func:`_cancel_runs` is what the loop's handover
    gates read; the Redis request is what makes the turn's steering bridge
    fire its cancel event within a second. Best-effort: a Redis outage must
    not fail the stop — the rows are already cancelled.
    """
    from app.services.chat_turn_guard import (
        list_processing_conversations,
        request_turn_cancel,
    )

    try:
        conversation_ids = await list_processing_conversations()
    except Exception:
        logger.warning(
            "Emergency stop: could not list in-flight chat turns", exc_info=True
        )
        return
    if not conversation_ids:
        return
    if owner is not None:
        from app.models.chat import Conversation

        wanted = [u for u in (safe_uuid(c) for c in conversation_ids) if u]
        mine: set[str] = set()
        for batch in chunked(wanted, _ID_CHUNK):
            rows = (
                await db.execute(
                    select(Conversation.id).where(
                        Conversation.id.in_(batch), Conversation.user_id == owner
                    )
                )
            ).scalars()
            mine.update(str(cid) for cid in rows)
        conversation_ids = [c for c in conversation_ids if c in mine]
    # A sweep after the stop meets the turns it already cancelled still
    # unwinding: the request is repeated, the turn is counted once.
    counted = set(outcome.cancelled_conversation_ids)
    for conversation_id in conversation_ids:
        try:
            if (
                await request_turn_cancel(conversation_id)
                and conversation_id not in counted
            ):
                outcome.chat_turns_cancelled += 1
                outcome.cancelled_conversation_ids.append(conversation_id)
        except Exception:
            logger.warning(
                "Emergency stop: could not request cancel for conversation %s",
                conversation_id,
                exc_info=True,
            )


async def _cancel_flow_runs(
    db: AsyncSession,
    *,
    owner: uuid.UUID | None,
    now: datetime,
    outcome: EmergencyStopOutcome,
    message: str = STOP_MESSAGE,
) -> None:
    """Same recipe as ``POST /flows/runs/{id}/cancel``, over the scope."""
    from app.models.flow_run import FLOW_RUN_ACTIVE_STATUSES, FlowRun

    query = select(FlowRun).where(FlowRun.status.in_(FLOW_RUN_ACTIVE_STATUSES))
    if owner is not None:
        query = query.where(FlowRun.user_id == owner)
    runs = list((await db.execute(query)).scalars().all())
    outcome.executions_cancelled += await _cancel_flow_run_rows(db, runs, now=now, message=message)
    outcome.flow_runs_cancelled += len(runs)
    outcome.cancelled_flow_runs.extend(runs)


async def _cancel_flow_run_rows(
    db: AsyncSession, runs: list[Any], *, now: datetime, message: str = STOP_MESSAGE
) -> int:
    """Cancel *runs* and their executions. Returns the executions cancelled."""
    if not runs:
        return 0
    from app.models.flow_run import FLOW_RUN_STATUS_CANCELLED
    from app.services.flow_runner import close_running_steps, release_runner_commands
    from app.services.orchestration_engine import cancel_executions_for_runs

    for run in runs:
        run.status = FLOW_RUN_STATUS_CANCELLED
        run.completed_at = now
        # Per run: it takes one run_id and closes that run's journal rows.
        await close_running_steps(db, run.id, error=message)
        # As the flow cancel endpoint does: nobody reads these results now, so
        # a queued command must not run later and a dispatched one is never
        # delivered again.
        await release_runner_commands(db, run, reason=message)
    # One batched statement set for every run's executions, not one per run.
    executions = await cancel_executions_for_runs(db, [run.id for run in runs])
    await db.flush()
    return executions


async def _sweep_orphaned_sub_flows(outcome: EmergencyStopOutcome) -> None:
    """Sub-flows a cancelled flow run started as the stop landed.

    A ``flow.call`` node already running when the stop cancelled its run can
    still insert the child after the stop selected the live flow runs; the
    child of a cancelled run is never live work, in either scope. Follows the
    chain down, since that child may have started its own.
    """
    from sqlalchemy.orm import aliased

    from app.core.database import async_session
    from app.models.flow_run import (
        FLOW_CALL_MAX_DEPTH,
        FLOW_RUN_ACTIVE_STATUSES,
        FLOW_RUN_STATUS_CANCELLED,
        FlowRun,
    )

    parent = aliased(FlowRun)
    cancelled: list[Any] = []
    executions = 0
    try:
        async with async_session() as db:
            now = datetime.now(UTC)
            for _ in range(FLOW_CALL_MAX_DEPTH + 1):
                query = (
                    select(FlowRun)
                    .join(parent, FlowRun.parent_run_id == parent.id)
                    .where(
                        # No ``created_at`` bound, for the reason given in
                        # :func:`sweep_late_admissions`.
                        FlowRun.status.in_(FLOW_RUN_ACTIVE_STATUSES),
                        parent.status == FLOW_RUN_STATUS_CANCELLED,
                    )
                )
                if outcome.owner_id is not None:
                    query = query.where(FlowRun.user_id == outcome.owner_id)
                runs = list((await db.execute(query)).scalars().all())
                if not runs:
                    break
                executions += await _cancel_flow_run_rows(db, runs, now=now)
                cancelled.extend(runs)
            if cancelled:
                await _add_to_audit_counts(
                    db,
                    outcome,
                    flow_runs_cancelled=len(cancelled),
                    executions_cancelled=executions,
                )
                await db.commit()
    except Exception:
        logger.warning(
            "Emergency stop: sub-flow sweep failed (scope=%s)",
            outcome.scope,
            exc_info=True,
        )
        return
    if not cancelled:
        return
    outcome.flow_runs_cancelled += len(cancelled)
    outcome.executions_cancelled += executions
    from app.services.flow_run_stream import publish_flow_run_update

    for run in cancelled:
        with suppress(Exception):
            await publish_flow_run_update(run)
    logger.warning(
        "Emergency stop: cancelled %d sub-flow run(s) a cancelled run started "
        "during the stop (scope=%s)",
        len(cancelled),
        outcome.scope,
    )


async def _cancel_runner_commands(
    db: AsyncSession,
    *,
    owner: uuid.UUID | None,
    outcome: EmergencyStopOutcome,
    message: str = STOP_MESSAGE,
) -> None:
    """Give up every open runner command in scope, through the transport.

    A queued or held command is finished as ``cancelled``. A ``dispatched``
    one may already be running on the host and cannot be recalled, but it is
    marked given up so it is never delivered again. ``give_up_on_command``
    locks each row, so a runner claiming it meanwhile is never overwritten.
    """
    from app.models.runner import RunnerCommand
    from app.services.runner_transport import give_up_on_command

    query = select(RunnerCommand.id).where(
        RunnerCommand.status.in_(("pending", "pending_approval", "dispatched"))
    )
    if owner is not None:
        query = query.where(RunnerCommand.user_id == owner)
    command_ids = list((await db.execute(query)).scalars().all())
    for command_id in command_ids:
        snapshot = await give_up_on_command(
            command_id, message, undelivered_status="cancelled", db=db
        )
        if (
            snapshot is not None
            and snapshot["status"] == "cancelled"
            and snapshot["error"] == message
        ):
            outcome.runner_commands_cancelled += 1
            outcome.finished_runner_command_ids.append(command_id)
    if command_ids:
        await db.flush()


async def _cancel_platform_only(
    db: AsyncSession, *, now: datetime, outcome: EmergencyStopOutcome
) -> None:
    """Evaluation runs and autonomous pentest runs — no owner, admin surfaces."""
    from app.models.evaluation import EvaluationRun
    from app.models.orchestration import OrchestrationExecution
    from app.services.orchestration_adapters.autonomous import (
        ADAPTER_NAME as AUTONOMOUS_ADAPTER,
    )
    from app.services.orchestration_adapters.evaluation import (
        ADAPTER_NAME as EVALUATION_ADAPTER,
    )
    from app.services.orchestration_engine import cancel_execution

    # Executions, then runs, then episodes: the global lock order, the
    # one a worker takes (``_fence_on_lease`` locks its execution, then writes
    # its runs). Writing the runs first, the stop would hold them while waiting
    # on a fenced worker's execution, and deadlock with it. Each level
    # is locked in id order, so two overlapping stops take the same sequence.
    rows = (
        await db.execute(
            select(OrchestrationExecution.id, OrchestrationExecution.adapter).where(
                OrchestrationExecution.adapter.in_(
                    (EVALUATION_ADAPTER, AUTONOMOUS_ADAPTER)
                ),
                OrchestrationExecution.state.in_(("pending", "running")),
            )
            .order_by(OrchestrationExecution.id)
        )
    ).all()
    for execution_id, adapter in rows:
        execution = await cancel_execution(db, execution_id=execution_id)
        if execution is None or execution.state != "cancelled":
            continue
        outcome.executions_cancelled += 1
        if adapter == AUTONOMOUS_ADAPTER:
            outcome.autonomous_runs_cancelled += 1

    # Locked as read: a run a worker completes meanwhile drops out once its lock is
    # granted, as PostgreSQL re-checks the status, instead of being overwritten.
    eval_runs = (
        await db.execute(
            select(EvaluationRun)
            .where(EvaluationRun.status.in_(("pending", "running")))
            .order_by(EvaluationRun.id)
            .with_for_update()
        )
    ).scalars().all()
    for run in eval_runs:
        run.status = "cancelled"
        run.error = STOP_MESSAGE
        run.completed_at = now
        if run.started_at is not None:
            run.duration_seconds = round((now - run.started_at).total_seconds(), 2)
        outcome.evaluation_runs_cancelled += 1
    # Then the stopped runs' open episodes, as cancelling one run does, all
    # runs at once. An open episode would accept a late callback and call the
    # judge on a run the stop ended.
    if eval_runs:
        from app.services.evaluation_continuation import (
            close_episodes_for_cancelled_runs,
        )

        await close_episodes_for_cancelled_runs(
            db, run_ids=[run.id for run in eval_runs], error=STOP_MESSAGE
        )
    if eval_runs or rows:
        await db.flush()


async def _pause_assignments(
    db: AsyncSession,
    *,
    owner: uuid.UUID | None,
    actor: User,
    actor_label: str,
    scope: PanicScope,
    now: datetime,
    outcome: EmergencyStopOutcome,
    request: Request | None,
) -> None:
    query = select(Assignment).where(Assignment.enabled.is_(True))
    scope_filter = _assignment_scope_filter(owner)
    if scope_filter is not None:
        query = query.where(scope_filter)
    assignments = (await db.execute(query)).scalars().all()
    details = (
        f"Emergency stop pressed by {actor_label} "
        f"({'whole platform' if scope == 'platform' else 'own activity'})."
    )
    for assignment in assignments:
        pause_assignment(
            assignment,
            reason=PANIC_STOP_REASON,
            details=details,
            actor_user_id=actor.id,
            actor_label=actor_label,
            now=now,
        )
        outcome.assignments_paused += 1
    # The barrier holds the stop lock (and, platform-wide, the settings row):
    # every pause row in one flush, not a flush and a savepoint per assignment.
    await record_pause_audits(
        db,
        assignments=assignments,
        user=actor,
        reason=PANIC_STOP_REASON,
        details=details,
        request=request,
    )


# ---------------------------------------------------------------------------
# After commit
# ---------------------------------------------------------------------------


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _cancel_conversation_dependants(conversation_id: str) -> None:
    """Partner tasks + local background tasks the conversation left running.

    ``cancel_tasks_for_conversation`` opens its own session, so it runs after
    the stop's commit, detached — a partner that cannot be told must not turn
    the stop into a failure.
    """
    try:
        from app.services.sub_agent import cancel_tasks_for_conversation

        await cancel_tasks_for_conversation(None, conversation_id)
    except Exception:
        logger.warning(
            "Emergency stop: cascade for conversation %s failed",
            conversation_id,
            exc_info=True,
        )


async def sweep_late_admissions(outcome: EmergencyStopOutcome) -> int:
    """Cancel runs admitted while the stop was in flight. Returns the count.

    The stop is one transaction: a scheduler tick or a trigger on another
    replica that read ``enabled = True`` (or an AI-enabled cache) before the
    commit can create a run AFTER ``_cancel_runs`` selected its rows, leaving
    a live run behind a committed ``panic_stop`` assignment. Run once the
    stop is durable, in its own session: every run still active in scope is
    cancelled the same way, and its partners are told; so is a sub-flow a cancelled flow run started
    (:func:`_sweep_orphaned_sub_flows`), and a platform stop also cancels the
    background tasks and chat turns still live
    (:func:`_sweep_platform_live_work`). Idempotent and
    best-effort — the pause and the kill switch, now visible, stop the next
    admission at the source.

    The first call stamps ``first_swept_at``; a later call for a ``self``
    stop only cancels what :func:`_self_stop_later_pass_filter` keeps, so a
    run the user starts after the stop is not taken for a late admission.
    """
    if outcome.stopped_at is None:
        return 0
    later_self_pass = (
        outcome.owner_id is not None and outcome.first_swept_at is not None
    )
    if outcome.first_swept_at is None:
        outcome.first_swept_at = datetime.now(UTC)
    from app.a2a.client.delegation import spawn_run_cancel
    from app.a2a.tasks import cascade_cancel
    from app.core.database import async_session
    from app.services.assignment_engine import ACTIVE_RUN_STATUSES, QUEUED_RUN_STATUS
    from app.services.orchestration_engine import cancel_executions_for_runs

    stoppable = (*ACTIVE_RUN_STATUSES, QUEUED_RUN_STATUS)
    cancelled = 0
    tree: list[uuid.UUID] = []
    try:
        async with async_session() as db:
            # No lower bound on ``created_at``: it is stamped app-side at the
            # flush, so a run flushed just before ``stopped_at`` and committed
            # after phase 2 read the live runs would slip under any cut-off.
            # Nothing in scope is legitimately live after a committed stop
            # that no resume lifted, whenever it was created - except, after
            # the first pass of a ``self`` stop, what the user started since.
            query = select(AssignmentRun.id).where(
                AssignmentRun.status.in_(stoppable),
            )
            if outcome.owner_id is not None:
                query = query.where(AssignmentRun.user_id == outcome.owner_id)
            if later_self_pass:
                query = query.where(_self_stop_later_pass_filter(outcome))
            late = list((await db.execute(query)).scalars().all())
            if late:
                now = datetime.now(UTC)
                result = await db.execute(
                    update(AssignmentRun)
                    .where(
                        AssignmentRun.id.in_(late),
                        AssignmentRun.status.in_(stoppable),
                    )
                    .values(status="cancelled", completed_at=now, error=STOP_MESSAGE)
                )
                cancelled = result.rowcount or 0
                tree = await cascade_cancel(db, late)
                await cancel_executions_for_runs(db, tree)
                if cancelled:
                    # The trail must say what the stop halted in total,
                    # including what this pass caught after the commit.
                    await _add_to_audit_counts(
                        db,
                        outcome,
                        runs_cancelled=cancelled,
                        late_admissions_cancelled=cancelled,
                    )
                await db.commit()
    except Exception:
        logger.warning(
            "Emergency stop: late-admission sweep failed (scope=%s)",
            outcome.scope,
            exc_info=True,
        )
        cancelled = 0
        tree = []
    if tree:
        spawn_run_cancel(list(dict.fromkeys(tree)))
    # Fold into the outcome the route serialises AFTER this runs, so the
    # operator's summary counts what was really halted.
    outcome.runs_cancelled += cancelled
    outcome.late_admissions_cancelled += cancelled
    if cancelled:
        logger.warning(
            "Emergency stop: cancelled %d run(s) admitted during the stop (scope=%s)",
            cancelled,
            outcome.scope,
        )
    await _sweep_orphaned_sub_flows(outcome)
    if outcome.owner_id is None:
        await _sweep_platform_live_work(outcome)
    return cancelled


def _self_stop_later_pass_filter(outcome: EmergencyStopOutcome):
    """The runs a ``self`` stop's later pass may still cancel.

    A ``self`` stop closes its scope's assignments, not the user's door: a run
    the user starts after it (a chat turn that consults or transfers to
    another agent creates one) is theirs and must survive the delayed pass.
    What that pass may still catch is a run that already existed when the
    first pass ran (its ``created_at`` is stamped at the flush, so a run
    committed after that pass read the table still carries an earlier
    stamp), a run of an assignment this stop paused (a scheduler or trigger
    that loaded it before the pause), and a child of a run this stop
    cancelled (a turn or run still winding down).
    """
    from sqlalchemy.orm import aliased

    parent = aliased(AssignmentRun)
    return or_(
        AssignmentRun.created_at <= outcome.first_swept_at,
        AssignmentRun.assignment_id.in_(
            select(Assignment.id).where(
                Assignment.enabled.is_(False),
                Assignment.paused_reason == PANIC_STOP_REASON,
                Assignment.paused_at == outcome.stopped_at,
            )
        ),
        AssignmentRun.parent_run_id.in_(
            select(parent.id).where(
                parent.status == "cancelled",
                parent.error == STOP_MESSAGE,
                parent.completed_at >= outcome.stopped_at,
            )
        ),
    )


async def _sweep_platform_live_work(outcome: EmergencyStopOutcome) -> None:
    """Background tasks and chat turns live after a platform stop.

    With AI off nothing agentic may be live on the platform, so what is found
    here was admitted by a replica whose AI cache was still warm: a chat turn
    would run to its end, and a background task it spawned would be held
    until the resume and then run. Platform scope only: a ``self`` stop does
    not gate new work, so a turn started after it is the user's own.
    """
    from app.core.database import async_session
    from app.services.sub_agent import cancel_sub_agent_tasks_for_scope

    known = set(outcome.cancelled_conversation_ids)
    tasks = executions = chats_before = 0
    try:
        async with async_session() as db:
            tasks, executions = await cancel_sub_agent_tasks_for_scope(
                db, user_id=None, error=STOP_MESSAGE
            )
            chats_before = outcome.chat_turns_cancelled
            await _cancel_chat_turns(db, owner=None, outcome=outcome)
            chats = outcome.chat_turns_cancelled - chats_before
            if tasks or chats:
                await _add_to_audit_counts(
                    db,
                    outcome,
                    sub_agent_tasks_cancelled=tasks,
                    executions_cancelled=executions,
                    chat_turns_cancelled=chats,
                )
            await db.commit()
    except Exception:
        logger.warning(
            "Emergency stop: live-work sweep failed (scope=platform)",
            exc_info=True,
        )
        return
    outcome.sub_agent_tasks_cancelled += tasks
    outcome.executions_cancelled += executions
    for conversation_id in outcome.cancelled_conversation_ids:
        if conversation_id not in known:
            _spawn(_cancel_conversation_dependants(conversation_id))
    if tasks or outcome.chat_turns_cancelled - chats_before:
        logger.warning(
            "Emergency stop: cancelled %d background task(s) and %d chat turn(s) "
            "admitted during the stop (scope=platform)",
            tasks,
            outcome.chat_turns_cancelled - chats_before,
        )


async def _add_to_audit_counts(
    db: AsyncSession, outcome: EmergencyStopOutcome, **increments: int
) -> None:
    """Add a sweep's counts to the stop's ``panic_stop`` audit row."""
    if outcome.audit_id is None:
        return
    from app.models.audit_log import AuditLog

    entry = (
        await db.execute(select(AuditLog).where(AuditLog.id == outcome.audit_id))
    ).scalar_one_or_none()
    if entry is None:
        return
    details = dict(entry.details or {})
    for key, value in increments.items():
        details[key] = int(details.get(key) or 0) + value
    entry.details = details


async def after_commit(outcome: EmergencyStopOutcome) -> None:
    """The work that may only happen once the stop is durable."""
    if outcome.ai_disabled:
        from app.services import ai_state

        ai_state.invalidate_cache()
    # The sweep cascades the conversations it cancels itself.
    stopped_conversations = list(outcome.cancelled_conversation_ids)
    # Now that the pause and the kill switch are visible to every replica,
    # catch what slipped in between the sweep and the commit - unless a
    # resume already landed, whose runs are legitimate.
    if not await _resumed_since(outcome):
        await sweep_late_admissions(outcome)
    # ...and once more after the AI-gate cache can no longer be warm anywhere:
    # the cache is process-local (no cross-replica invalidation, by design), so
    # another replica may admit chat-born work for up to one TTL after the
    # first commit, and a scheduler may reuse an ``Assignment`` it loaded before
    # it. This pass counts into the audit row only — the response is out.
    from app.services import ai_state

    _spawn(_delayed_sweep(outcome, ai_state.CACHE_TTL_SECONDS + 1.0))
    # Everything below runs NOW — partners, conversations and the live UI must
    # not wait one cache TTL, and a detached task would not survive a restart.
    await announce_stopped_work(outcome, conversation_ids=stopped_conversations)


async def stop_owner_live_work(
    db: AsyncSession, *, owner: uuid.UUID, message: str, now: datetime | None = None
) -> EmergencyStopOutcome:
    """Cancel the live work of *owner* alone: no pause, no AI switch, no stop trail.

    The arms of :func:`emergency_stop` for one account, with *message* as the
    cause each cancelled row shows. Used when an account is deactivated
    (``services/user_deactivation.py``): its runs, background tasks, chat
    turns, runner commands and flow runs end at once, the same way and with
    the same records as stopping them one by one. Flushes, never commits;
    the caller commits, then calls :func:`announce_stopped_work`.
    """
    outcome = EmergencyStopOutcome(scope="self", stopped_at=now or datetime.now(UTC), owner_id=owner)
    stopped_at = outcome.stopped_at or datetime.now(UTC)
    await _cancel_runs(db, owner=owner, now=stopped_at, outcome=outcome, message=message)
    await _cancel_background_tasks(db, owner=owner, outcome=outcome, message=message)
    await _cancel_chat_turns(db, owner=owner, outcome=outcome)
    await _cancel_runner_commands(db, owner=owner, outcome=outcome, message=message)
    await _cancel_flow_runs(db, owner=owner, now=stopped_at, outcome=outcome, message=message)
    return outcome


async def announce_stopped_work(outcome: EmergencyStopOutcome, *, conversation_ids: list[str] | None = None) -> None:
    """Tell partners, conversations, flow streams and runners what a committed stop ended.

    *conversation_ids*: the conversations whose dependants to cancel, by
    default every one the stop cancelled (a sweep cascades its own).
    """
    stopped_conversations = list(outcome.cancelled_conversation_ids) if conversation_ids is None else conversation_ids
    if outcome.partner_cancel_run_ids:
        from app.a2a.client.delegation import spawn_run_cancel

        spawn_run_cancel(outcome.partner_cancel_run_ids)
    for conversation_id in stopped_conversations:
        _spawn(_cancel_conversation_dependants(conversation_id))
    if outcome.cancelled_flow_runs:
        from app.services.flow_run_stream import publish_flow_run_update

        for run in outcome.cancelled_flow_runs:
            with suppress(Exception):
                await publish_flow_run_update(run)
    if outcome.finished_runner_command_ids:
        with suppress(Exception):
            from app.services import runner_transport

            # Wakes each command's waiter and the parked flow nodes.
            await runner_transport.announce_finished_commands(
                outcome.finished_runner_command_ids, "cancelled"
            )


async def _resumed_since(outcome: EmergencyStopOutcome) -> bool:
    """Whether a resume in the stop's scope landed after the stop.

    Neither sweep may cancel runs that a resume — pressed inside the window,
    which the reason-first dialog makes easy — legitimately started.
    """
    if outcome.stopped_at is None:
        return False
    from app.core.database import async_session
    from app.models.audit_log import AuditLog

    try:
        async with async_session() as db:
            query = select(AuditLog.id).where(
                AuditLog.action == AUDIT_ACTION_RESUME,
                AuditLog.created_at >= outcome.stopped_at,
            )
            if outcome.owner_id is None:
                query = query.where(AuditLog.entity_type == "platform")
            else:
                # A resume of the user's own scope, or of the whole platform,
                # both re-open the door for their runs.
                query = query.where(
                    or_(
                        AuditLog.entity_type == "platform",
                        AuditLog.user_id == outcome.owner_id,
                    )
                )
            return (await db.execute(query.limit(1))).scalar_one_or_none() is not None
    except Exception:
        logger.debug("Emergency stop: resume probe failed", exc_info=True)
        return True  # when in doubt, do not cancel what may be legitimate


async def _delayed_sweep(outcome: EmergencyStopOutcome, delay: float) -> None:
    """The second late-admission pass, once no replica can hold a warm cache."""
    await asyncio.sleep(delay)
    if await _resumed_since(outcome):
        logger.info(
            "Emergency stop: delayed sweep skipped, the stop was lifted meanwhile "
            "(scope=%s)",
            outcome.scope,
        )
        return
    await sweep_late_admissions(outcome)


async def after_resume_commit(outcome: ResumeOutcome) -> None:
    if outcome.ai_enabled:
        from app.services import ai_state

        ai_state.invalidate_cache()


# ---------------------------------------------------------------------------
# Resume + status
# ---------------------------------------------------------------------------


def _panic_paused_query(
    owner: uuid.UUID | None, *, platform_stop_times: list[datetime] | None = None
):
    """The assignments a resume in *owner*'s scope re-enables.

    The platform scope takes only what a platform stop paused (their
    ``paused_at`` is its ``stopped_at``): a user's own stop is theirs to
    lift, and an administrator lifting the platform must not restart what a
    user deliberately kept halted. A ``self`` scope takes every stop's pause
    on the user's assignments, since it is refused while a platform stop is
    open and a platform resume already took the platform's.
    """
    query = select(Assignment).where(
        Assignment.enabled.is_(False),
        Assignment.paused_reason == PANIC_STOP_REASON,
    )
    scope_filter = _assignment_scope_filter(owner)
    if scope_filter is not None:
        query = query.where(scope_filter)
    else:
        query = query.where(Assignment.paused_at.in_(platform_stop_times or []))
    return query


async def _platform_stop_times(db: AsyncSession) -> list[datetime]:
    """The ``stopped_at`` of every platform stop, as stamped on what it paused.

    The open stop's come from its state on ``PlatformSettings``, so a resume
    still finds what it paused when the audit rows are gone; the
    trail adds the earlier, lifted stops, whose pauses another stop may have
    held through their resume.
    """
    from app.models.audit_log import AuditLog

    rows = (
        await db.execute(
            select(AuditLog.details).where(
                AuditLog.action == AUDIT_ACTION_STOP,
                AuditLog.entity_type == "platform",
            )
        )
    ).scalars()
    times: list[datetime] = list((await _platform_stop_state(db)).times)
    for details in rows:
        raw = (details or {}).get("stopped_at")
        if isinstance(raw, str):
            with suppress(ValueError):
                stamped = datetime.fromisoformat(raw)
                if stamped not in times:
                    times.append(stamped)
    return times


async def _open_self_stops(db: AsyncSession) -> dict[uuid.UUID, datetime]:
    """Users whose own stop is still in effect, with the moment it was pressed.

    A ``self`` stop is open until that user's next ``self`` resume (a
    platform resume does not lift it). The moment is the stop's
    ``stopped_at``: the ``paused_at`` it stamped on what it paused.
    Emergency stops are rare, so the trail is read whole.
    """
    from app.models.audit_log import AuditLog

    resumed = dict(
        (
            await db.execute(
                select(AuditLog.user_id, func.max(AuditLog.created_at))
                .where(
                    AuditLog.action == AUDIT_ACTION_RESUME,
                    AuditLog.entity_type == "user",
                )
                .group_by(AuditLog.user_id)
            )
        ).all()
    )
    stops = (
        await db.execute(
            select(AuditLog.user_id, AuditLog.created_at, AuditLog.details).where(
                AuditLog.action == AUDIT_ACTION_STOP, AuditLog.entity_type == "user"
            )
        )
    ).all()
    open_stops: dict[uuid.UUID, datetime] = {}
    for user_id, created_at, details in stops:
        last_resume = resumed.get(user_id)
        if last_resume is not None and last_resume >= created_at:
            continue
        pressed = created_at
        raw = (details or {}).get("stopped_at")
        if isinstance(raw, str):
            with suppress(ValueError):
                pressed = datetime.fromisoformat(raw)
        if user_id not in open_stops or pressed > open_stops[user_id]:
            open_stops[user_id] = pressed
    return open_stops


def _covered_by_open_stop(
    assignment: Any,
    open_stops: dict[uuid.UUID, datetime],
    *,
    lifting: uuid.UUID | None,
) -> bool:
    """Whether another open stop still covers this paused assignment.

    A stop covers the assignments in its user's scope that were paused when
    it was pressed: the ones it paused, and the ones it found already paused.
    A resume leaves those in place, or the user lifting their own stop would
    restart what someone else's stop still holds. ``lifting`` is the user
    whose own stop the resume lifts (``None`` for a platform resume).
    """
    paused_at = assignment.paused_at
    for user_id in (assignment.user_id, assignment.run_as_user_id):
        if user_id is None or user_id == lifting:
            continue
        pressed = open_stops.get(user_id)
        if pressed is not None and (paused_at is None or paused_at <= pressed):
            return True
    return False


async def resume_after_emergency_stop(
    db: AsyncSession,
    *,
    actor: User,
    scope: PanicScope,
    reason: str,
    request: Request | None = None,
) -> ResumeOutcome:
    """Re-enable what the stop paused — and nothing else.

    Only assignments stamped ``panic_stop`` are touched: one a human paused
    before or after the stop keeps their decision, and one another user's
    open stop still covers stays paused until that stop is lifted
    (:func:`_covered_by_open_stop`). In the platform scope the
    AI override goes back to what the stop's barrier recorded
    (``ai_override_before_stop``), never forced ON: a deployment configured
    without AI stays without AI, and the platform stop's state is cleared.

    ``reason`` is the operator's stated reason for lifting the stop. Unlike
    the stop it is asked BEFORE acting — a resume is not an emergency — and
    it lands on the ``panic_resume`` audit row and on every per-assignment
    ``resume`` row (``details.note``).
    """
    owner = _owner_for(actor, scope)
    reason = reason.strip()
    outcome = ResumeOutcome(scope=scope, reason=reason)

    # Under the same lock the barrier takes: the check below and the
    # re-enables after it cannot straddle a platform stop being armed.
    await serialize_stop_transitions(db)
    if owner is not None and await platform_stop_open(db):
        # A user's assignments paused by a PLATFORM stop are that stop's
        # boundary, not theirs to lift one scope at a time: only lifting the
        # platform stop (an administrator) resumes them.
        raise PlatformStopActiveError(
            "A platform-wide emergency stop is in effect; only an administrator "
            "can lift it"
        )

    platform_times = await _platform_stop_times(db) if owner is None else None
    open_stops = await _open_self_stops(db)
    assignments = [
        assignment
        for assignment in (
            await db.execute(
                _panic_paused_query(owner, platform_stop_times=platform_times)
            )
        ).scalars()
        if not _covered_by_open_stop(assignment, open_stops, lifting=owner)
    ]
    for assignment in assignments:
        await resume_assignment(
            db, assignment=assignment, user=actor, request=request, note=reason
        )
        outcome.assignments_resumed += 1

    if owner is None:
        from app.api.v1.platform_settings_helpers import get_platform_settings
        from app.services import ai_state
        from app.services.audit import log_audit

        ps = await get_platform_settings(db)
        # Only the override the STOP set, and only back to what it WAS: an
        # administrator who had turned AI off before the stop keeps AI off,
        # and one who changed the switch in Settings during the stop keeps
        # that change (the switch no longer reads as the stop left it).
        if ps.ai_disabled is True and await platform_stop_open(db):
            restore_to = await _ai_override_before_stop(db)
            previous = await ai_state.set_ai_enabled(db, restore_to)
            # The EFFECTIVE answer, not "the override is gone".
            outcome.ai_enabled = ai_state.resolve(restore_to)
            await log_audit(
                db,
                user=actor,
                action="toggle",
                entity_type="general_assistant",
                entity_name="Platform Settings",
                summary=(
                    "AI restored to its pre-stop setting (emergency stop lifted): "
                    + (
                        "enabled"
                        if restore_to is True
                        else "disabled"
                        if restore_to is False
                        else "follow the deployment mode"
                    )
                ),
                details={
                    "setting": "ai_enabled",
                    "from": previous,
                    "to": restore_to,
                    "resolved": ai_state.resolve(restore_to),
                    "reason": AUDIT_ACTION_RESUME,
                },
                request=request,
            )
        await _close_platform_stop(db)

    from app.services.audit import log_audit

    await log_audit(
        db,
        user=actor,
        action=AUDIT_ACTION_RESUME,
        entity_type="platform" if owner is None else "user",
        entity_id=None if owner is None else actor.id,
        entity_name=None if owner is None else actor.email,
        summary=(
            "Emergency stop lifted on the platform"
            if owner is None
            else f"Emergency stop lifted for {actor_display_name(actor)}"
        )
        + f" — reason: {reason}",
        details={**outcome.counts(), "reason": reason},
        request=request,
    )
    logger.warning(
        "Emergency stop lifted by %s (scope=%s): %s",
        actor.email,
        scope,
        outcome.counts(),
    )
    return outcome


async def _ai_override_before_stop(db: AsyncSession) -> bool | None:
    """The AI override the open platform stop found, which its resume restores.

    Recorded by the FIRST stop of the open sequence, not the latest: a second
    platform stop pressed while the first is still open (a token stop after a
    UI stop, a second operator) finds the ``False`` the first stop wrote, and
    restoring that would leave AI off after the resume. ``None`` (follow the
    deployment mode) when no platform stop is open.
    """
    return (await _platform_stop_state(db)).ai_override


@dataclass(frozen=True)
class _PlatformStopState:
    """The open platform stop, as stored on ``PlatformSettings``."""

    #: ``stopped_at`` of the first stop of the open sequence; ``None`` = none open.
    at: datetime | None = None
    #: ``stopped_at`` of every platform stop pressed while it is open.
    times: tuple[datetime, ...] = ()
    #: The AI override (wire tri-state) the first stop found.
    ai_override: bool | None = None


async def _platform_stop_state(db: AsyncSession) -> _PlatformStopState:
    """Read the open platform stop off the ``PlatformSettings`` singleton.

    Not from the ``panic_stop`` / ``panic_resume`` audit rows: they cascade
    with the user who pressed the stop (and the token path files them under
    the administrator who minted the token), while what the stop did — paused
    assignments, AI off — stays in place until a resume lifts it. A column
    read, so a session that loaded the settings earlier still sees the
    committed state.
    """
    from app.models.platform_settings import PlatformSettings

    row = (
        await db.execute(
            select(
                PlatformSettings.platform_stop_at,
                PlatformSettings.platform_stop_times,
                PlatformSettings.platform_stop_ai_override,
            )
            .order_by(PlatformSettings.created_at.asc(), PlatformSettings.id.asc())
            .limit(1)
        )
    ).one_or_none()
    if row is None or row.platform_stop_at is None:
        return _PlatformStopState()
    times: list[datetime] = []
    for raw in row.platform_stop_times or []:
        if isinstance(raw, str):
            with suppress(ValueError):
                times.append(datetime.fromisoformat(raw))
    return _PlatformStopState(
        at=row.platform_stop_at,
        times=tuple(times),
        ai_override=(
            row.platform_stop_ai_override
            if isinstance(row.platform_stop_ai_override, bool)
            else None
        ),
    )


async def _open_platform_stop(
    db: AsyncSession, *, stopped_at: datetime, ai_override_before_stop: bool | None
) -> None:
    """Record a platform stop on ``PlatformSettings``, in the barrier's transaction.

    The first stop of a sequence opens it with the AI override it found; a
    further stop pressed while it is open only adds its ``stopped_at``, the
    stamp on the assignments it paused. Called after
    :func:`_disable_platform_ai`, which created and row-locked the singleton.
    """
    from app.models.platform_settings import PlatformSettings

    state = await _platform_stop_state(db)
    if state.at is None:
        values: dict[str, Any] = {
            "platform_stop_at": stopped_at,
            "platform_stop_times": [stopped_at.isoformat()],
            "platform_stop_ai_override": ai_override_before_stop,
        }
    else:
        values = {
            "platform_stop_times": [
                *(t.isoformat() for t in state.times),
                stopped_at.isoformat(),
            ]
        }
    await db.execute(update(PlatformSettings).values(**values))


async def _close_platform_stop(db: AsyncSession) -> None:
    """Clear the platform stop's state: the platform resume lifted it."""
    from app.models.platform_settings import PlatformSettings

    await db.execute(
        update(PlatformSettings).values(
            platform_stop_at=None,
            platform_stop_times=None,
            platform_stop_ai_override=None,
        )
    )


async def serialize_stop_transitions(db: AsyncSession) -> None:
    """Take the transaction-scoped lock every stop / resume transition holds.

    A ``self`` resume checks ``platform_stop_open`` and then re-enables rows;
    a platform stop being armed on another request could commit between the
    two, and the resume would lift assignments that stop just paused. Both
    paths take this advisory lock first, so the check and the write happen
    under one serialised view. Transaction-scoped: released at commit or
    rollback, nothing to clean up on failure.
    """
    from sqlalchemy import text

    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('xtm_one:emergency_stop'))")
    )


async def platform_stop_open(db: AsyncSession) -> bool:
    """Whether a platform-wide emergency stop is in effect: a platform stop
    opened its state on ``PlatformSettings`` and no platform resume has
    cleared it since (:func:`_platform_stop_state`).

    Not the AI switch: an administrator turning AI off from Settings is not
    an emergency, and a resume must not clear an override the stop did not
    set. Not the audit trail either, which can lose the stop's rows while its
    effects stay. Every viewer may read this — a user who owns no
    assignment still lives on a stopped platform.
    """
    return (await _platform_stop_state(db)).at is not None


async def _stop_in_effect_for(
    db: AsyncSession,
    *,
    actor: User,
    platform_since: datetime | None,
    pressed: datetime | None,
    rows: list[Any],
) -> Any:
    """The ``panic_stop`` row behind the caller's stop state, for the banner.

    The open platform stop first (``platform_since``: when it was first
    pressed), then the caller's own open stop, then the stop that stamped the
    most recent pause still in the caller's scope (a lifted stop whose pause
    another stop holds). Never merely the latest platform-or-own stop: that
    one may have been lifted, and the banner would name the wrong person and
    reason. ``None`` when the row behind the state is gone.
    """
    from app.models.audit_log import AuditLog

    def _latest(*conditions):
        return (
            select(AuditLog)
            .where(AuditLog.action == AUDIT_ACTION_STOP, *conditions)
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )

    if platform_since is not None:
        # Rows of the open sequence only: with its own rows deleted, an older
        # lifted stop must not stand in for it.
        query = _latest(
            AuditLog.entity_type == "platform", AuditLog.created_at >= platform_since
        )
        return (await db.execute(query)).scalar_one_or_none()
    if pressed is not None:
        query = _latest(AuditLog.entity_type == "user", AuditLog.user_id == actor.id)
        return (await db.execute(query)).scalar_one_or_none()
    stamped = max((row.paused_at for row in rows if row.paused_at), default=None)
    if stamped is None:
        return None
    candidates = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.action == AUDIT_ACTION_STOP)
            .order_by(AuditLog.created_at.desc())
            .limit(_STOP_ROWS_SCANNED)
        )
    ).scalars()
    for entry in candidates:
        raw = (entry.details or {}).get("stopped_at")
        if isinstance(raw, str):
            with suppress(ValueError):
                if datetime.fromisoformat(raw) == stamped:
                    return entry
    return None


#: How many recent stops the banner looks through for the one behind a pause.
_STOP_ROWS_SCANNED = 200


async def emergency_stop_status(db: AsyncSession, *, actor: User) -> dict[str, Any]:
    """What a stop left in place, for the menu's "resume" entries and the
    banner every page shows while a stop is in effect.

    ``active`` is derived from state, not from history: assignments still
    stamped ``panic_stop`` in the caller's scope (a platform stop pauses a
    user's assignments too, so a non-admin sees it), or, for an administrator,
    the platform AI override being OFF. ``last_stop`` is the stop behind
    that state (:func:`_stop_in_effect_for`), so the banner can say who, when
    and why.
    """
    open_stops = await _open_self_stops(db)

    async def _paused(owner: uuid.UUID | None) -> list[Any]:
        platform_times = await _platform_stop_times(db) if owner is None else None
        query = _panic_paused_query(
            owner, platform_stop_times=platform_times
        ).with_only_columns(
            Assignment.user_id, Assignment.run_as_user_id, Assignment.paused_at
        )
        return list((await db.execute(query)).all())

    def _resumable(rows: list[Any], owner: uuid.UUID | None) -> int:
        return sum(
            1
            for row in rows
            if not _covered_by_open_stop(row, open_stops, lifting=owner)
        )

    platform_stop = await _platform_stop_state(db)
    platform_open = platform_stop.at is not None
    own_rows = await _paused(actor.id)
    pressed = open_stops.get(actor.id)
    status: dict[str, Any] = {
        "self_paused_assignments": _resumable(own_rows, actor.id),
        # The caller's own stop still holds something, even if another stop
        # covers it too: their resume must stay on offer, or the two stops
        # would hold each other's assignments for good.
        "self_stop_active": pressed is not None
        and any(row.paused_at is None or row.paused_at <= pressed for row in own_rows),
        "platform_paused_assignments": None,
        "ai_disabled": None,
        # Everyone sees this: a platform stop is in effect until it is lifted,
        # whether or not the viewer owns an assignment it paused.
        "platform_stop_active": platform_open,
        "active": False,
        "last_stop": None,
    }
    if actor.is_admin:
        from app.api.v1.platform_settings_helpers import get_platform_settings

        ps = await get_platform_settings(db)
        status["platform_paused_assignments"] = _resumable(await _paused(None), None)
        # The switch AS THE STOP LEFT IT — an override an administrator set
        # from Settings on their own is not this feature's business.
        status["ai_disabled"] = platform_open and ps.ai_disabled is True
    status["active"] = bool(
        platform_open
        or status["self_paused_assignments"]
        or status["self_stop_active"]
        or status["platform_paused_assignments"]
    )
    if status["active"]:
        last = await _stop_in_effect_for(
            db,
            actor=actor,
            platform_since=platform_stop.at,
            pressed=pressed,
            rows=own_rows,
        )
        if last is not None:
            details = last.details or {}
            # The banner reaches every user during a platform stop: a display
            # name, like ``paused_by_label``, not the audit row's email.
            pressed_by = (
                await db.execute(select(User).where(User.id == last.user_id))
            ).scalar_one_or_none()
            status["last_stop"] = {
                "at": last.created_at.isoformat() if last.created_at else None,
                "by": (
                    actor_display_name(pressed_by)
                    if pressed_by is not None
                    else last.user_email
                ),
                "scope": details.get("scope"),
                "reason": details.get("reason"),
            }
        elif platform_stop.at is not None:
            # The open platform stop's rows are gone: still say when, not who.
            status["last_stop"] = {
                "at": platform_stop.at.isoformat(),
                "by": None,
                "scope": "platform",
                "reason": None,
            }
    return status


# ---------------------------------------------------------------------------
# Emergency token — the stop without a session
# ---------------------------------------------------------------------------
#
# An emergency does not always find an administrator signed in: the runbook
# on the wall, a monitoring hook, an on-call engineer with only a terminal.
# The token is a platform-wide credential that does exactly one thing — press
# the emergency stop — and nothing else. It is generated by an administrator
# in Settings > Access and security, shown once, and stored as a bcrypt hash like the API
# keys (never recoverable, which is why the procedure is printed at creation).

EMERGENCY_TOKEN_PREFIX = "xes-"  # noqa: S105 — a prefix / an action name, not a secret
AUDIT_ACTION_TOKEN_GENERATED = "panic_token_generated"  # noqa: S105 — a prefix / an action name, not a secret
AUDIT_ACTION_TOKEN_REVOKED = "panic_token_revoked"  # noqa: S105 — a prefix / an action name, not a secret
SECURITY_EVENT_TOKEN_REJECTED = "panic_token_rejected"  # noqa: S105 — a prefix / an action name, not a secret


def _display_prefix(raw_token: str) -> str:
    """The stored, displayed start of a token: ``xes-`` and 8 hex digits."""
    return raw_token[:12] + "..."


async def carries_current_token_prefix(
    db: AsyncSession, raw_token: str | None
) -> bool:
    """Whether *raw_token* starts like the configured emergency token.

    What the token route still checks once its per-address ceiling is spent,
    or cannot be read: a caller sharing an address with whoever spent it (one
    egress, a proxy that hides the client) must still be able to stop the
    platform, while a guess, which does not know the 32 bits of the prefix,
    never reaches the bcrypt check. The prefix is shown to administrators
    only.
    """
    from app.api.v1.platform_settings_helpers import get_platform_settings
    from app.core.security import timing_safe_str_eq

    if not raw_token or not raw_token.startswith(EMERGENCY_TOKEN_PREFIX):
        return False
    ps = await get_platform_settings(db)
    if not ps.emergency_stop_token_hash or not ps.emergency_stop_token_prefix:
        return False
    return timing_safe_str_eq(
        _display_prefix(raw_token), ps.emergency_stop_token_prefix
    )


async def emergency_token_status(db: AsyncSession) -> dict[str, Any]:
    from app.api.v1.platform_settings_helpers import get_platform_settings

    ps = await get_platform_settings(db)
    configured = bool(ps.emergency_stop_token_hash)
    return {
        "configured": configured,
        "prefix": ps.emergency_stop_token_prefix if configured else None,
        "created_at": (
            ps.emergency_stop_token_created_at.isoformat()
            if configured and ps.emergency_stop_token_created_at
            else None
        ),
        "created_by": ps.emergency_stop_token_created_by if configured else None,
        "last_used_at": (
            ps.emergency_stop_token_last_used_at.isoformat()
            if configured and ps.emergency_stop_token_last_used_at
            else None
        ),
    }


async def generate_emergency_token(
    db: AsyncSession, *, actor: User, request: Request | None = None
) -> dict[str, Any]:
    """Mint (or rotate) the emergency token. Returns the raw token — once.

    Rotation is implicit: there is one token per platform, and generating a
    new one invalidates the previous one on the spot. Stored as a bcrypt hash
    with a display prefix, exactly like the user API keys. Flushes; the
    caller commits.
    """
    import secrets

    from app.api.v1.platform_settings_helpers import get_platform_settings
    from app.core.security import bcrypt_hashpw_async
    from app.services.audit import log_audit

    ps = await get_platform_settings(db)
    raw = f"{EMERGENCY_TOKEN_PREFIX}{secrets.token_hex(24)}"
    now = datetime.now(UTC)
    rotated = bool(ps.emergency_stop_token_hash)
    ps.emergency_stop_token_hash = (
        await bcrypt_hashpw_async(raw.encode("utf-8"))
    ).decode("utf-8")
    ps.emergency_stop_token_prefix = _display_prefix(raw)
    ps.emergency_stop_token_created_at = now
    ps.emergency_stop_token_created_by = actor.email
    ps.emergency_stop_token_created_by_id = actor.id
    ps.emergency_stop_token_last_used_at = None
    await db.flush()
    await log_audit(
        db,
        user=actor,
        action=AUDIT_ACTION_TOKEN_GENERATED,
        entity_type="platform",
        entity_name="Emergency stop token",
        summary=(
            "Rotated the emergency stop API token"
            if rotated
            else "Generated the emergency stop API token"
        ),
        details={"prefix": ps.emergency_stop_token_prefix, "rotated": rotated},
        request=request,
    )
    return {
        "token": raw,
        "prefix": ps.emergency_stop_token_prefix,
        "created_at": now.isoformat(),
        "created_by": actor.email,
    }


async def revoke_emergency_token(
    db: AsyncSession, *, actor: User, request: Request | None = None
) -> bool:
    """Drop the token. Returns False when none was configured."""
    from app.api.v1.platform_settings_helpers import get_platform_settings
    from app.services.audit import log_audit

    ps = await get_platform_settings(db)
    if not ps.emergency_stop_token_hash:
        return False
    prefix = ps.emergency_stop_token_prefix
    ps.emergency_stop_token_hash = None
    ps.emergency_stop_token_prefix = None
    ps.emergency_stop_token_created_at = None
    ps.emergency_stop_token_created_by = None
    ps.emergency_stop_token_created_by_id = None
    ps.emergency_stop_token_last_used_at = None
    await db.flush()
    await log_audit(
        db,
        user=actor,
        action=AUDIT_ACTION_TOKEN_REVOKED,
        entity_type="platform",
        entity_name="Emergency stop token",
        summary="Revoked the emergency stop API token",
        details={"prefix": prefix},
        request=request,
    )
    return True


async def _token_actor(db: AsyncSession, creator_id: uuid.UUID | None) -> User | None:
    """The administrator a valid token acts as.

    Attribution prefers a live person, but a correct token must never be
    refused for want of one: the printed procedure is the way in when nobody
    can sign in, and the stop only ever cuts activity. So, in order:

    1. the minting administrator, while still an active administrator;
    2. the oldest active administrator (the minter was demoted, deleted or
       deactivated);
    3. the minting administrator even when deactivated, while still an
       administrator — no active administrator is left at all;
    4. the oldest administrator of any status.

    A demoted minter is never the actor (the platform scope refuses a
    non-admin); ``None`` only when the install has no administrator.
    """
    is_admin = User.role == "admin"
    active = User.is_active.is_(True)

    async def _creator(*conditions) -> User | None:
        if creator_id is None:
            return None
        return (
            await db.execute(select(User).where(User.id == creator_id, *conditions))
        ).scalar_one_or_none()

    async def _oldest(*conditions) -> User | None:
        return (
            await db.execute(
                select(User).where(*conditions).order_by(User.created_at.asc()).limit(1)
            )
        ).scalar_one_or_none()

    return (
        await _creator(is_admin, active)
        or await _oldest(is_admin, active)
        or await _creator(is_admin)
        or await _oldest(is_admin)
    )


async def resolve_emergency_token(
    db: AsyncSession, raw_token: str | None, *, request: Request | None = None
) -> User | None:
    """The administrator the token acts as, or ``None`` when it is refused.

    A refusal is a security event (``panic_token_rejected``): a wrong token
    on this endpoint is either a stale runbook or someone probing, and both
    deserve a line in the trail. The actor is resolved by
    :func:`_token_actor` — the minter first, never ``None`` while the install
    has an administrator: the stop must not fail for want of an attribution.
    """
    from app.api.v1.platform_settings_helpers import get_platform_settings
    from app.core.security import bcrypt_checkpw_async
    from app.services.audit import log_security_event

    ps = await get_platform_settings(db)
    accepted = bool(
        raw_token
        and raw_token.startswith(EMERGENCY_TOKEN_PREFIX)
        and ps.emergency_stop_token_hash
        and await bcrypt_checkpw_async(
            raw_token.encode("utf-8"), ps.emergency_stop_token_hash.encode("utf-8")
        )
    )
    if not accepted:
        # ``audit_logs.user_id`` is NOT NULL and the caller has no identity, so
        # the event is filed under the administrator who owns the token (or,
        # with none configured, the oldest administrator) — the person who
        # should hear that someone is knocking on this door.
        owner_id = ps.emergency_stop_token_created_by_id
        if owner_id is None:
            owner_id = (
                await db.execute(
                    select(User.id)
                    .where(User.role == "admin", User.is_active.is_(True))
                    .order_by(User.created_at.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        with suppress(Exception):
            await log_security_event(
                db,
                user_id=str(owner_id) if owner_id else "anonymous",
                action=SECURITY_EVENT_TOKEN_REJECTED,
                entity_name="Emergency stop token",
                summary="Emergency stop API call refused: invalid or missing token",
                details={
                    "configured": bool(ps.emergency_stop_token_hash),
                    "client_ip": request_client_ip(request) if request else None,
                },
            )
        return None

    actor = await _token_actor(db, ps.emergency_stop_token_created_by_id)
    if actor is not None:
        ps.emergency_stop_token_last_used_at = datetime.now(UTC)
    return actor


async def emergency_stop_with_token(
    db: AsyncSession,
    *,
    actor: User,
    reason: str | None,
    request: Request | None = None,
) -> EmergencyStopOutcome:
    """The token path: always the platform scope, reason recorded in the same
    transaction when the caller gave one (a runbook usually does).

    Same two phases as the route: the barrier is committed first, then the
    sweep — see :func:`arm_admission_barrier`. The token and the reason are
    written on the trail with the barrier, so they survive a failed phase 2.
    """
    outcome = await arm_admission_barrier(
        db, actor=actor, scope="platform", request=request
    )
    details_note = "via the emergency stop API token"
    if outcome.audit_id is not None:
        from app.models.audit_log import AuditLog

        entry = (
            await db.execute(select(AuditLog).where(AuditLog.id == outcome.audit_id))
        ).scalar_one()
        entry.details = {**(entry.details or {}), "via": "api_token"}
        if reason and reason.strip():
            await record_stop_reason(
                db, actor=actor, audit_id=outcome.audit_id, reason=reason
            )
        else:
            entry.summary = f"{entry.summary} ({details_note})"
    await db.commit()
    from app.services import ai_state

    ai_state.invalidate_cache()
    return await cancel_live_work(
        db, actor=actor, scope="platform", request=request, outcome=outcome
    )
