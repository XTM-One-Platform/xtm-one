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

"""Sub-agent task adapter.

Runs background agent-to-agent delegations through the durable engine instead
of in-process ``asyncio.create_task``, so a delegation survives a restart and
is recovered like any other execution. The parent conversation's working files
are persisted as ``file_ids`` in the payload and rebuilt into a ``FileContext``
on the worker before running the existing ``_execute_sub_agent_task`` body.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import safe_uuid
from app.llm.provider_access import acting_for
from app.models.orchestration import OrchestrationExecution
from app.services.orchestration_adapters.base import (
    AdapterContext,
    AdapterResult,
    OrchestrationAdapter,
)

logger = logging.getLogger(__name__)

ADAPTER_NAME = "sub_agent"


def execution_dedup_key(task_id: str | uuid.UUID) -> str:
    """Canonical dedup key for a sub-agent task's execution."""
    return f"sub_agent_task:{task_id}"


#: The run status a parent holds its slot in while it waits for its delegated child.
LIVE_PARENT_STATUSES = ("running",)


async def children_of_live_parents(db: AsyncSession, due: list[OrchestrationExecution]) -> set[uuid.UUID]:
    """The ids of the *due* task executions whose parent run (``payload.parent_run_id``) is still running.

    Such a parent holds a slot of the shared pool while it waits for its child: the child must not queue
    behind the work that slot is part of. One read; an execution with no parent run is never one.
    """
    parent_of: dict[uuid.UUID, uuid.UUID] = {}
    for execution in due:
        parent = safe_uuid((execution.payload or {}).get("parent_run_id"))
        if parent is not None:
            parent_of[execution.id] = parent
    if not parent_of:
        return set()
    from app.models.assignment import AssignmentRun

    live = set(
        (
            await db.execute(
                select(AssignmentRun.id).where(
                    AssignmentRun.id.in_(list(set(parent_of.values()))),
                    AssignmentRun.status.in_(LIVE_PARENT_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
    return {execution_id for execution_id, parent in parent_of.items() if parent in live}


async def _rebuild_file_context(ctx: AdapterContext, file_ids: list[str] | None):
    """Reconstruct a FileContext from persisted file-attachment ids."""
    if not file_ids:
        return None
    from app.llm.base import FileRef
    from app.models.file_attachment import FileAttachment
    from app.tools.base import FileContext

    parsed: list[uuid.UUID] = []
    for fid in file_ids:
        try:
            parsed.append(uuid.UUID(str(fid)))
        except (ValueError, TypeError):
            continue
    if not parsed:
        return None
    rows = (
        await ctx.db.execute(
            select(FileAttachment).where(FileAttachment.id.in_(parsed))
        )
    ).scalars().all()
    fc = FileContext()
    for fa in rows:
        fc.add(FileRef.from_attachment(fa))
    return fc


#: The ``adapter_state`` of a background task its stopping worker handed back.
HANDED_BACK_STATE = "handed_back"


async def _end_hand_back(
    ctx: AdapterContext, execution: OrchestrationExecution
) -> OrchestrationExecution:
    """Close the deferral record a hand-back left, before the task resumes.

    ``engine.defer`` writes ``payload['hold']`` for every deferral, and the AI
    hold reads that record as its own (``hold_budget_left``,
    ``close_hold_if_ai_is_back``): left in place, a hand-back followed by
    agentic AI going away would count its hold from the hand-back and expire
    early. The record has done its work once this claim started (attempt and
    budget restored), so it is dropped with its adapter state and message and
    committed. The record, not the adapter state, says it was a hand-back: a
    claim lost before this point is taken back as ``worker_gone`` with the
    record kept, and that state ends here too.
    Anything else - an AI hold, no record - is left as it is.
    """
    from app.services import orchestration_engine

    record = orchestration_engine.hold_record(execution)
    if record is None or record.get("reason") != HANDED_BACK_STATE:
        return execution
    ended = await orchestration_engine.end_hold(
        ctx.db,
        execution_id=execution.id,
        worker_id=ctx.worker_id,
        held_adapter_state=True,
    )
    await ctx.db.commit()
    return ended


def _taken_back_on_its_word(execution: OrchestrationExecution) -> bool:
    """Whether this claim follows a take-back from a worker that said it was stopping.

    ``reclaim_from_gone_owners`` leaves the ``worker_gone`` adapter state and
    the gone worker's last presence record (``lease_expired.owner_record``);
    a worker that was ``draining`` or ``stopped`` went on its word
    (``orchestration_engine.stopped_on_its_word``), so the resume that follows
    is graceful, like a hand-back. Read before the hand-back record is ended.
    """
    from app.services import orchestration_engine

    if getattr(execution, "adapter_state", None) != orchestration_engine.OWNER_GONE_STATE:
        return False
    record = (getattr(execution, "payload", None) or {}).get(orchestration_engine.LEASE_EXPIRY_PAYLOAD_KEY)
    return isinstance(record, dict) and orchestration_engine.stopped_on_its_word(record.get("owner_record"))


def _killed_by_rollout(execution: OrchestrationExecution) -> bool:
    """Whether this claim follows a take-back from a worker a rollout killed without a word
    (``orchestration_engine.killed_by_rollout``): its resume counts on no streak (#12386)."""
    from app.services import orchestration_engine

    if getattr(execution, "adapter_state", None) != orchestration_engine.OWNER_GONE_STATE:
        return False
    record = (getattr(execution, "payload", None) or {}).get(orchestration_engine.LEASE_EXPIRY_PAYLOAD_KEY)
    return isinstance(record, dict) and orchestration_engine.killed_by_rollout(record.get("owner_record"))


def _taken_back_reason(execution: OrchestrationExecution) -> str | None:
    """The engine's own word on the take-back without a word this claim follows, else ``None``.

    ``_take_back`` records ``lease_expired`` at the attempt it ended: the claim
    after a loss without a word runs one attempt later (a take-back on its word
    gives the attempt back, and is graceful: no reason needed). Its ``why``
    names the worker gone, its host, version and memory at its last word
    (``worker_presence.stop_reason``), or "Lease expired"; the silence follows.
    """
    from app.services import orchestration_engine

    record = (getattr(execution, "payload", None) or {}).get(orchestration_engine.LEASE_EXPIRY_PAYLOAD_KEY)
    if not isinstance(record, dict) or orchestration_engine.stopped_on_its_word(record.get("owner_record")):
        return None
    attempt = getattr(execution, "attempt_count", None)
    if not isinstance(attempt, int) or record.get("attempt") != attempt - 1:
        return None
    why = record.get("why")
    why = why.strip().rstrip(".") if isinstance(why, str) and why.strip() else "its lease expired"
    silent = record.get("silent_seconds")
    heard = f", last heartbeat {silent} s before it was taken back" if isinstance(silent, int) else ""
    return f"{why}{heard}"


def raised_task_bound(max_attempts: int) -> int:
    """The bound a task execution created at *max_attempts* runs under now.

    A row at exactly the old fixed floor (``background_checkpoint.MAX_ATTEMPTS``,
    4) is a legacy row whatever the setting is now: it gets what a new task
    gets (``task_max_attempts``). Any other value is an operator's or a newer
    row's and is kept. Pure: what the engine's take-back reads before it
    decides a row has no attempt left (``OrchestrationAdapter.raised_attempt_bound``)
    and what :func:`_raise_attempt_bound` writes at the next claim.
    """
    from app.core.config import settings
    from app.services import background_checkpoint

    if not isinstance(max_attempts, int) or max_attempts != background_checkpoint.MAX_ATTEMPTS:
        return max_attempts
    return max(max_attempts, background_checkpoint.task_max_attempts(settings.orchestration_default_max_attempts))


async def _raise_attempt_bound(ctx: AdapterContext, execution: OrchestrationExecution) -> None:
    """Raise a task execution created under the old fixed floor to the task bound.

    An execution queued under the old floor ran under ``max(configured, MAX_ATTEMPTS)``:
    4 unless an operator set more. At 4 the engine would dead-letter it at its
    third loss without a word whatever its progress. A row at exactly that
    fixed floor is a legacy row whatever the setting is now (no row created
    since is ever at 4, ``task_max_attempts``), so it is raised to what a new
    task gets; any other value is an operator's or a newer row's and is left
    alone. Only that column is written, under the claim this worker holds.
    """
    from sqlalchemy import update

    from app.services import background_checkpoint

    old_floor = background_checkpoint.MAX_ATTEMPTS
    current = getattr(execution, "max_attempts", None)
    if not isinstance(current, int) or getattr(execution, "id", None) is None:
        return
    bound = raised_task_bound(current)
    if bound <= current:
        return
    await ctx.db.execute(
        update(OrchestrationExecution)
        .where(OrchestrationExecution.id == execution.id, OrchestrationExecution.max_attempts == old_floor)
        .values(max_attempts=bound)
    )
    await ctx.db.commit()
    execution.max_attempts = bound


def _cap_reached_error(task, execution: OrchestrationExecution) -> str | None:
    """The resume cap's reason for a task dead-lettered with no attempt left after it resumed or was handed back.

    ``None`` for any other dead letter (a deadline, a task never resumed): its
    own error stands. Only a loss without a word spends an attempt (a
    hand-back is a deferral, a take-back on its word gives the attempt back),
    so the attempts run out with the losses ``MAX_RESUMES`` counts;
    the reason names both counts, the graceful resumes included.
    """
    from app.services import background_checkpoint

    attempts = getattr(execution, "attempt_count", None) or 0
    allowed = getattr(execution, "max_attempts", None) or 0
    if not allowed or attempts < allowed:
        return None
    metadata = task.metadata_json if isinstance(task.metadata_json, dict) else {}
    if not background_checkpoint.resumes_of(metadata) and background_checkpoint.HANDED_BACK_KEY not in metadata:
        return None
    return background_checkpoint.cap_reached_reason(metadata)


class SubAgentAdapter(OrchestrationAdapter):
    name = ADAPTER_NAME
    #: One writer per task turn: an attempt the engine took back stops before its next tool round.
    single_writer_turns = True
    # A background task resumes from its own checkpoint and a stale attempt
    # writes no ending: taken back the moment its worker is known gone
    # (a rollout, a node drain), not after its 900 s lease.
    resume_when_worker_gone = True

    def raised_attempt_bound(self, max_attempts: int) -> int:
        """A task queued under the old floor (4) is taken back under the task bound."""
        return raised_task_bound(max_attempts)

    async def deal_claims(
        self, db: AsyncSession, due: list[OrchestrationExecution], worker_id: str
    ) -> list[OrchestrationExecution]:
        """Children of a live parent first, then the rest; oldest first in each (the order *due* comes in)."""
        if not due:
            return []
        first = await children_of_live_parents(db, due)
        return [e for e in due if e.id in first] + [e for e in due if e.id not in first]

    async def deal_reserved_claims(
        self, db: AsyncSession, due: list[OrchestrationExecution], worker_id: str
    ) -> list[OrchestrationExecution]:
        """What the delegation reserve may claim once the shared pool is full: children of a live parent alone."""
        if not due:
            return []
        first = await children_of_live_parents(db, due)
        return [e for e in due if e.id in first]

    @property
    def cancel_poll_seconds(self) -> float | None:
        """Opt into the reconciler's fast cancel watcher (emergency stop)."""
        return self._configured_cancel_poll()

    @property
    def lease_seconds(self) -> int | None:
        """The lease a background task runs under (``SUB_AGENT_LEASE_SECONDS``).

        Long enough for the longest tool call of an agent turn (a wait, a CI
        poll, a large read) to pass without a renewal, so a run that is still
        working is never taken back and re-run from scratch; read live, so a
        settings change applies to the next claim. ``0`` keeps the engine's.
        """
        from app.core.config import settings

        own = settings.sub_agent_lease_seconds
        return int(own) if isinstance(own, (int, float)) and own > 0 else None

    async def execute(
        self, ctx: AdapterContext, execution: OrchestrationExecution
    ) -> AdapterResult:
        from app.a2a.partner_scope import (
            partner_task_scope,
            queued_partner_principal,
            queued_partner_task,
        )
        from app.models.sub_agent_task import SubAgentTask
        from app.services.element_scope import queued_task_scope
        from app.services.sub_agent import _execute_sub_agent_task

        payload = execution.payload or {}
        task_id = payload.get("task_id")
        target_agent_id = payload.get("target_agent_id")
        user_id = payload.get("user_id")
        task_description = payload.get("task_description")
        # ``user_id`` is required: ``_execute_sub_agent_task`` parses it with
        # ``uuid.UUID(user_id)`` for the agent-access query, so a missing/empty
        # value would raise deep in the body and surface as an unhandled
        # execution error instead of a clean, non-retryable adapter failure.
        if not (task_id and target_agent_id and task_description and user_id):
            return AdapterResult.failed(
                "Sub-agent payload missing required fields", retryable=False
            )

        # A corrupted payload with a non-UUID task_id can never succeed: parse
        # it gracefully (like get_task_statuses does) so it surfaces as a clean
        # non-retryable failure instead of a raise the reconciler would retry.
        task_uuid = safe_uuid(str(task_id))
        if task_uuid is None:
            return AdapterResult.failed(
                f"Sub-agent payload task_id is not a valid UUID: {task_id}",
                retryable=False,
            )

        # Terminal-status guard: a re-claimed execution (e.g. a retry after the
        # task body finished but the execution row update was lost) must not
        # re-run the agentic loop for a task already in a terminal state — just
        # mirror that state onto the execution.
        task_row = (
            await ctx.db.execute(
                select(SubAgentTask).where(SubAgentTask.id == task_uuid)
            )
        ).scalar_one_or_none()
        if task_row is not None and task_row.status in (
            "completed",
            "failed",
            "incomplete",
        ):
            if task_row.status == "failed":
                return AdapterResult.failed(
                    task_row.error or "Sub-agent task failed", retryable=False
                )
            return AdapterResult.completed()

        file_context = await _rebuild_file_context(ctx, payload.get("file_ids"))

        # Release any pooled connection held by the reads above before the
        # long-running task. ``_execute_sub_agent_task`` owns its own
        # session(s), so leaving this read transaction open would idle-pin a
        # connection for the whole delegation. ``expire_on_commit=False`` keeps
        # loaded state usable.
        await ctx.db.commit()

        # ``_execute_sub_agent_task`` owns its own session(s), enforces its own
        # timeout, and records the SubAgentTask / linked AssignmentRun outcome —
        # it never raises on a normal failure and returns the terminal outcome.
        from app.services.ai_hold import (
            close_hold_if_ai_is_back,
            hold_budget_left,
            hold_result,
        )

        # Taken back from a worker that said it was stopping: a graceful
        # resume, not counted against the task's ``MAX_RESUMES``.
        # Read before the hand-back's record and adapter state are ended.
        taken_back_on_its_word = _taken_back_on_its_word(execution)
        killed_by_rollout = _killed_by_rollout(execution)
        # Taken back without a word: the engine's reason, for the cause the
        # resume records instead of the generic one.
        taken_back_reason = _taken_back_reason(execution)
        await _raise_attempt_bound(ctx, execution)
        # Handed back by a stopping worker: the deferral's record only
        # carried the attempt and runnable budget over to this claim. It is no
        # AI hold, so it ends here, before the gate is read: a hold met next
        # starts its own bound instead of counting from the hand-back.
        execution = await _end_hand_back(ctx, execution)
        # Held until now and agentic AI is back: the hold ends here,
        # before the task starts, so a later hold starts its own bound. The
        # task acts on the same reading of the gate (``None``: never held).
        verdict = await close_hold_if_ai_is_back(ctx, execution, user_id=user_id)
        # Delegated while answering an A2A partner's task: the task,
        # and anything it delegates in turn, runs in the partner-task scope.
        # Every AI call of the task is for the user who delegated it, those with
        # no user of their own included: their LLM provider exclusions hold.
        with (
            partner_task_scope(queued_partner_task(payload), principal=queued_partner_principal(payload)),
            acting_for(user_id),
        ):
            outcome = await _execute_sub_agent_task(
                task_id=str(task_id),
                user_id=str(user_id),
                target_agent_id=str(target_agent_id),
                task_description=str(task_description),
                assignment_run_id=payload.get("assignment_run_id"),
                delegation_depth=str(payload.get("delegation_depth", "0")),
                scope_headers_json=payload.get("scope_headers_json"),
                file_context=file_context,
                parent_trace_context=payload.get("parent_trace_context"),
                visited_agents=payload.get("visited_agents"),
                # Inherited parent budget — absent on pre-inheritance
                # payloads, in which case the standalone defaults apply.
                timeout_seconds=payload.get("timeout_seconds"),
                max_iterations=payload.get("max_iterations"),
                # The delegating run's enforced Scope(s) - absent,
                # or unmarked on a task queued by an older release: the target then keeps
                # its own.
                element_scope=queued_task_scope(payload),
                hold=hold_budget_left(execution),
                verdict=verdict,
                # Fired by the reconciler's fast cancel watcher when this execution
                # is cancelled out of band (emergency stop, operator cancel) so the
                # agent loop aborts between iterations instead of at the lease.
                cancel_event=getattr(ctx, "cancel_event", None),
                deadline_at=getattr(execution, "deadline_at", None),
                attempt=getattr(execution, "attempt_count", None),
                taken_back_on_its_word=taken_back_on_its_word,
                taken_back_reason=taken_back_reason,
                killed_by_rollout=killed_by_rollout,
            )
        if outcome.held:
            # Agentic AI is unavailable: the task is still pending, so
            # it waits without consuming an attempt and is asked again later.
            return hold_result(execution, outcome.refusal or "")
        if outcome.deferred:
            # Whether agentic AI may run could not be read: the task was
            # left pending, untouched - hand it back to the engine's retry path.
            from app.services.agentic_gate import UNKNOWN_STATE_RETRY_SECONDS

            return AdapterResult.failed(
                outcome.error or "Sub-agent task deferred",
                retryable=True,
                backoff_seconds=UNKNOWN_STATE_RETRY_SECONDS,
            )
        if outcome.handed_back:
            # Its worker was asked to stop and the turn wound down, checkpointed:
            # due again now, first in the queue, resumed by the next
            # worker from there. A deferral, not a failed attempt: a
            # retryable failure on the last attempt dead-lettered the task, so
            # "runs again at once" never ran. The task's own
            # ``MAX_GRACEFUL_RESUMES`` bounds how many times it is handed back;
            # a hand-back never spends ``MAX_RESUMES``.
            return AdapterResult.defer(
                outcome.error or "Background task handed back by its stopping worker",
                delay_seconds=0.0,
                adapter_state=HANDED_BACK_STATE,
                place="head",
            )
        if not outcome.succeeded:
            # The task body already recorded the terminal SubAgentTask /
            # AssignmentRun failure (including its own timeout handling), so a
            # retry would re-run an already-failed task — fail permanently.
            return AdapterResult.failed(
                outcome.error or "Sub-agent task failed", retryable=False
            )
        return AdapterResult.completed()

    async def on_dead_letter(
        self, ctx: AdapterContext, execution: OrchestrationExecution
    ) -> None:
        """Fail a task the engine gave up on, release what it held, wake its parent.

        A task that resumed (or was handed back) and lost its worker with no
        attempt left fails with the resume cap's reason, never the
        hand-back's "runs again at once" that was the execution's last error.
        """
        from app.models.sub_agent_task import SubAgentTask
        from app.services.orchestration_engine import lease_expiry

        payload = execution.payload or {}
        task_id = payload.get("task_id")
        error = (
            execution.last_error
            or "Sub-agent task abandoned by the orchestration engine "
            "(deadline exceeded or attempts exhausted)."
        )
        now = datetime.now(UTC)
        expiry = lease_expiry(execution)
        # A task WE transition to ``failed`` here (never one already terminal,
        # and never an ``incomplete`` wait-budget task) whose parent chat is
        # parked must resume that conversation — see the wake block at the end.
        woke_task: SubAgentTask | None = None
        if task_id:
            # safe_uuid: a corrupted payload must not crash the cleanup pass —
            # the delegation row is matched by string key below regardless.
            task_uuid = safe_uuid(str(task_id))
            task = None
            if task_uuid is not None:
                task = (
                    await ctx.db.execute(
                        select(SubAgentTask)
                        .where(SubAgentTask.id == task_uuid)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
            # A completed or incomplete task ran its work, and keeps its charge.
            # A failed one may still hold a charge nobody credited: a stale
            # attempt can write ``failed`` while the re-run that replaced it is
            # charged under a receipt of its own. A failure handler
            # that credited its charge redeemed that receipt, so crediting
            # the receipts again is a no-op for it.
            abandoned = task is None or task.status in ("pending", "running")
            settle = abandoned or task.status == "failed"
            if (
                task is not None
                and task.status == "running"
                and expiry is not None
                # Taken back from a worker known gone: no worker is
                # left to write its ending, so it is failed now.
                and expiry.owner_record is None
            ):
                # Its lease lapsed, not its work: the engine cannot see
                # its worker, which may still be running the turn - a tool call
                # that held the worker for minutes, three renewals that hung -
                # and about to write the ending the caller waits for. The task
                # is left running, marked orphaned, and keeps that ending when
                # it comes (``_locked_task_for_ending`` finds it unended and
                # unreplaced); the orphan sweep (``recover_orphaned_sub_agent_tasks``)
                # fails it only once its worker's own time is over. Nothing is
                # credited back and nobody is woken: the worker is still
                # charged and still working.
                from app.services.sub_agent import mark_task_orphaned

                record = mark_task_orphaned(task, execution, expiry, now=now)
                await ctx.db.commit()
                logger.warning(
                    "Background task %s lost its lease (%s) with no attempt left; its worker may still be "
                    "running, so the task stays running until %s before it is failed",
                    task_id,
                    expiry.describe(),
                    record["fail_after"],
                )
                return
            if task is not None and task.status in ("pending", "running"):
                from app.services import orchestration_engine as engine

                if engine.expired_before_start(execution):
                    # Its queue-wait bound passed before any worker started it
                    # (deadline_exceeded_before_start): its budget counts from its
                    # start, so this is a capacity failure, named with its wait.
                    error = engine.expired_before_start_message("The background task", execution)
                else:
                    error = _cap_reached_error(task, execution) or error
                task.status = "failed"
                task.error = error
                task.completed_at = now
                woke_task = task
                # The claims it held go back to the queue on the next tick,
                # each release logged; this line names the cause.
                logger.error(
                    "Background task %s (run %s) failed: %s Its work item claims are released now, the next "
                    "scheduler tick as the backstop",
                    task_id,
                    payload.get("assignment_run_id") or "none",
                    error,
                )
                # Its turn never ended, so it never wrote its own trace: close
                # it with the spans its turn checkpointed and what ended it,
                # found by the task id.
                from app.services.tracing.background_run import record_lost_background_run

                record_lost_background_run(
                    task,
                    reason=error,
                    last_heartbeat_at=expiry.last_heartbeat_at if expiry is not None else None,
                    attempt=getattr(execution, "attempt_count", None),
                    now=now,
                )
            # Close the delegation audit row too (even when the task row is
            # gone or already terminal), so dead-lettered background tasks
            # don't leave it stuck on 'pending'. ``update_delegation`` only
            # matches ACTIVE rows, so a delegation already closed by the task
            # body is never downgraded here.
            from app.services.sub_agent import _update_task_delegation

            await _update_task_delegation(ctx.db, str(task_id), "failed", error=error)

            # ── Credit the agentic action back ───────────────────────────
            # A background task is charged one action INSIDE its agent turn,
            # and this path exists precisely because that turn will never run
            # again (worker killed, lease expired, deadline blown). Without
            # this, an action charged for work the platform dropped stays
            # charged forever.
            #
            # Keyed on the receipts the charges themselves wrote, one per
            # attempt, so nothing is guessed: no receipt means the
            # attempt was never charged (refused by the quota gate, or it never
            # got that far), and crediting then would take an action from
            # another run. Each receipt is redeemed once, so a second
            # dead-letter pass - or an attempt still running - credits nothing.
            #
            # Free, never from the agent's failure pool: every route here is
            # infrastructure. The agent did not fail; it was not allowed to
            # finish.
            if settle:
                from app.services.attempt_charges import credit_abandoned_task

                # A failed task's task-wide receipt is an older release's,
                # whose failure handler credited without redeeming it.
                await credit_abandoned_task(
                    task_id,
                    attempts=getattr(execution, "attempt_count", 0) or 0,
                    db=ctx.db,
                    include_task_wide=abandoned,
                )

        assignment_run_id = payload.get("assignment_run_id")
        run_uuid = safe_uuid(str(assignment_run_id)) if assignment_run_id else None
        if run_uuid is not None:
            from app.models.assignment import AssignmentRun

            run = (
                await ctx.db.execute(
                    select(AssignmentRun)
                    .where(AssignmentRun.id == run_uuid)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if run is not None and run.status in ("pending", "running"):
                run.status = "failed"
                run.error = error
                run.completed_at = now

        # ── Resume the parked parent conversation ─────────────────
        # A background task the engine ABANDONS (worker killed, lease expired,
        # deadline blown) is flipped to ``failed`` above, but — unlike every
        # in-body terminal path in ``_execute_sub_agent_task`` — nothing here
        # woke the chat conversation that parked waiting on it. So a parent
        # stayed stuck on "Incomplete - Background work is still running"
        # indefinitely even though the task had already stopped. Mirror the
        # in-body commit-then-wake ordering: commit the ``failed`` status
        # first (the fire-and-forget wake re-reads it in its own session) then
        # schedule the wake. Only a task WE transitioned qualifies — a task the
        # body already drove terminal woke its parent there, and an
        # ``incomplete`` wait-budget task has its own continuation mechanism.
        if woke_task is not None and getattr(woke_task, "conversation_id", None):
            from app.models.agent import Agent
            from app.services.sub_agent import _schedule_parent_wake_if_needed

            agent_name = "Agent"
            try:
                resolved = (
                    await ctx.db.execute(
                        select(Agent.name).where(Agent.id == woke_task.target_agent_id)
                    )
                ).scalar_one_or_none()
                if resolved:
                    agent_name = resolved
            except Exception:
                logger.debug(
                    "Could not resolve agent name for parent wake of task %s",
                    task_id,
                    exc_info=True,
                )

            await ctx.db.commit()
            _schedule_parent_wake_if_needed(
                woke_task, agent_name=agent_name, terminal_status="failed"
            )
            committed = True
        else:
            committed = False

        # ── Release its work item claims now ──────────────────────
        # Last, once the failure is committed (by the wake above, else here),
        # so the holder reads as ended: the claims the task (or its run) held
        # go back to the queue in this pass, not at the next scheduler tick.
        # The cap's text says they are released, and a sweep or a pick-up
        # reading the item meanwhile must not find it held by a dead holder.
        if woke_task is not None:
            from app.services.sub_agent import release_failed_task_claims

            if not committed:
                await ctx.db.commit()
            await release_failed_task_claims(task_id, assignment_run_id)
