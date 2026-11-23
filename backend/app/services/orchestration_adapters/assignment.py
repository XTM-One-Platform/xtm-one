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

"""Assignment dispatch adapter.

Wraps the existing ``execute_assignment_run`` agentic-loop executor in the
durable engine: each ``AssignmentRun`` dispatch is one orchestration execution
(deduped on ``assignment_run:<run_id>``). A human-in-the-loop pause maps to an
AWAIT; the HITL respond endpoint emits a ``resume`` outbox event that re-arms
the execution for re-claim.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import release_db_connection_before_loop
from app.core.utils import format_exception_message, safe_uuid
from app.models.assignment import AssignmentRun
from app.models.orchestration import OrchestrationExecution, OrchestrationOutboxEvent
from app.services import assignment_run_resume, assignment_run_wake
from app.services import orchestration_engine as engine
from app.services.failure_causes import (
    FailureCause,
    FailureContext,
    FailureDetermination,
    classify_run_failure,
)
from app.services.orchestration_adapters.base import (
    AdapterContext,
    AdapterResult,
    OrchestrationAdapter,
)

logger = logging.getLogger(__name__)

ADAPTER_NAME = "assignment"
WAKE_SWEEP_ADAPTER = "assignment_run_wake_sweep"


def execution_dedup_key(run_id: str | uuid.UUID) -> str:
    """Canonical dedup key for an assignment run's dispatch execution."""
    return f"assignment_run:{run_id}"


async def _held_run_person(ctx: AdapterContext, execution: OrchestrationExecution, run_id: object):
    """The person a run held for ``ai_disabled`` acts as, for the gate that
    closes its hold: a group of theirs may still switch AI off.

    ``None`` - nothing read - for a run that was never held, as
    ``close_hold_if_ai_is_back`` reads nothing for it, and for a hold of any
    other reason (the licence, the quota store), which no group causes; also
    for a person that cannot be read: the hold then closes on the platform's
    reading alone, and the run's own start asks the gate again.
    """
    from app.services.agentic_gate import AI_DISABLED_CODE
    from app.services.ai_hold import held_adapter_state

    record = engine.hold_record(execution)
    if record is None or record.get("reason") != held_adapter_state(AI_DISABLED_CODE):
        return None
    try:
        row = await ctx.db.execute(
            select(AssignmentRun.user_id).where(AssignmentRun.id == uuid.UUID(str(run_id)))
        )
        return row.scalar_one_or_none()
    except Exception:
        logger.debug("Could not read the person of held assignment run %s", run_id, exc_info=True)
        # A failed SELECT aborts the transaction: the hold is closed on this
        # session next.
        await ctx.db.rollback()
        return None


async def _bind_memory_chain(ctx: AdapterContext, run_id: object):
    """Bind the memory event chain frozen on this run, if it carries one.

    Returns the reset token, or ``None`` when the run has no chain (every
    trigger that is not a flow's ``action.trigger_assignment``) or the context
    cannot be read — a missing chain must never stop an assignment running.
    """
    try:
        row = await ctx.db.execute(
            select(AssignmentRun.trigger_context).where(
                AssignmentRun.id == uuid.UUID(str(run_id))
            )
        )
        chain = (row.scalar_one_or_none() or {}).get("memory_chain")
        if not isinstance(chain, dict):
            return None
        from app.services.memory_flow_events import set_memory_chain_context

        return set_memory_chain_context(
            safe_uuid(chain.get("flow_id")),
            int(chain.get("chain_depth") or 0),
        )
    except Exception:
        logger.debug(
            "Could not rebind the memory event chain for assignment run %s",
            run_id,
            exc_info=True,
        )
        return None


#: The run status a parent holds its slot in while it waits for a delegated child.
_LIVE_PARENT_STATUSES = ("running",)


async def _pool_slots() -> int:
    """The assignment slots of the instance: ``max_concurrent_runs`` x the workers claiming assignment runs
    (one when Redis cannot say)."""
    from app.core.config import settings
    from app.services import worker_presence

    try:
        workers = await worker_presence.count(claiming=ADAPTER_NAME)
    except Exception:
        workers = None
    return max(1, int(settings.max_concurrent_runs)) * max(1, int(workers or 1))


async def children_of_live_parents(db: AsyncSession, due: list[OrchestrationExecution]) -> list[OrchestrationExecution]:
    """The *due* assignment executions whose run is a delegated child of a run still running, oldest first."""
    run_of: dict[uuid.UUID, object] = {}
    for execution in due:
        run_id = safe_uuid((execution.payload or {}).get("run_id"))
        if run_id is not None:
            run_of[execution.id] = run_id
    if not run_of:
        return []
    parent_of = {
        row[0]: row[1]
        for row in (
            await db.execute(
                select(AssignmentRun.id, AssignmentRun.parent_run_id).where(
                    AssignmentRun.id.in_(list(run_of.values())), AssignmentRun.parent_run_id.is_not(None)
                )
            )
        ).all()
    }
    if not parent_of:
        return []
    live = set(
        (
            await db.execute(
                select(AssignmentRun.id).where(
                    AssignmentRun.id.in_(list(set(parent_of.values()))),
                    AssignmentRun.status.in_(_LIVE_PARENT_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
    return [e for e in due if parent_of.get(run_of.get(e.id)) in live]


async def fair_share_deal(
    db: AsyncSession, due: list[OrchestrationExecution], worker_id: str
) -> list[OrchestrationExecution]:
    """The *due* assignment executions in the order a free slot claims them
    (``assignment_dispatch.fair_share_order``): delegated children of a live
    parent first, then one run per fleet in turn, a fleet or agent over its
    share of the pool after the others. An execution whose run cannot be read
    keeps its place at the end, oldest first.
    """
    from sqlalchemy import func

    from app.core.config import settings
    from app.models.fleet import FLEET_ROLE_MEMBER, FleetMember
    from app.services import assignment_dispatch

    if not due or not settings.assignment_fair_share_enabled:
        return list(due)
    run_of: dict[uuid.UUID, object] = {}
    for execution in due:
        run_id = safe_uuid((execution.payload or {}).get("run_id"))
        if run_id is not None:
            run_of[execution.id] = run_id
    if not run_of:
        return list(due)
    runs = {
        row[0]: (row[1], row[2])
        for row in (
            await db.execute(
                select(AssignmentRun.id, AssignmentRun.agent_id, AssignmentRun.parent_run_id).where(
                    AssignmentRun.id.in_(list(run_of.values()))
                )
            )
        ).all()
    }
    parents = {parent for _agent, parent in runs.values() if parent is not None}
    live_parents: set[uuid.UUID] = set()
    if parents:
        live_parents = set(
            (
                await db.execute(
                    select(AssignmentRun.id).where(
                        AssignmentRun.id.in_(list(parents)),
                        AssignmentRun.status.in_(_LIVE_PARENT_STATUSES),
                    )
                )
            )
            .scalars()
            .all()
        )
    running = (
        await db.execute(
            select(AssignmentRun.agent_id, func.count(AssignmentRun.id))
            .where(AssignmentRun.status == "running")
            .group_by(AssignmentRun.agent_id)
        )
    ).all()
    running_by_agent = {str(agent): int(count) for agent, count in running if agent is not None}
    agents = {agent for agent, _parent in runs.values() if agent is not None} | {
        agent for agent, _count in running if agent is not None
    }
    home: dict[str, str] = {}
    if agents:
        home = {
            str(agent): f"fleet:{fleet}"
            for agent, fleet in (
                await db.execute(
                    select(FleetMember.agent_id, FleetMember.fleet_id).where(
                        FleetMember.agent_id.in_(list(agents)),
                        FleetMember.role == FLEET_ROLE_MEMBER,
                    )
                )
            ).all()
        }

    def group_of(agent: str) -> str:
        return home.get(agent, f"agent:{agent}")

    running_by_group: dict[str, int] = {}
    for agent, count in running_by_agent.items():
        running_by_group[group_of(agent)] = running_by_group.get(group_of(agent), 0) + count
    candidates = []
    unread = []
    for execution in due:
        run = runs.get(run_of.get(execution.id))
        if run is None:
            unread.append(execution)
            continue
        agent, parent = str(run[0]), run[1]
        candidates.append(
            assignment_dispatch.ClaimCandidate(
                key=execution,
                group=group_of(agent),
                agent=agent,
                run_at=execution.run_at,
                child_of_live_parent=parent is not None and parent in live_parents,
            )
        )
    ordered = assignment_dispatch.fair_share_order(
        candidates,
        running_by_group=running_by_group,
        running_by_agent=running_by_agent,
        pool=await _pool_slots(),
        fleet_share=float(settings.assignment_fleet_max_share),
        agent_share=float(settings.assignment_agent_max_share),
    )
    return [*ordered, *unread]


#: What a run dead-lettered after a take-back from a worker gone fails with.
WORKER_GONE_DEAD_LETTER_ERROR = (
    "Assignment run was abandoned by the orchestration engine: its worker was lost "
    "with no attempt left."
)


def _public_dead_letter_error(execution: OrchestrationExecution) -> str | None:
    """The engine's error, as a run's viewers may read it.

    A take-back from a worker gone (``reclaim_from_gone_owners``, now that
    this adapter resumes when its worker goes) writes the worker's
    host, version and memory into ``last_error``: a run's error is read by
    whoever can see the run, so that case reads the generic words.
    Any other error stands.
    """
    record = (getattr(execution, "payload", None) or {}).get(engine.LEASE_EXPIRY_PAYLOAD_KEY)
    if isinstance(record, dict) and record.get("owner_record") is not None:
        return WORKER_GONE_DEAD_LETTER_ERROR
    return execution.last_error


class AssignmentAdapter(OrchestrationAdapter):
    name = ADAPTER_NAME
    # A run resumes from its own checkpoint and a replaced attempt writes no
    # ending: taken back the moment its worker is known gone,
    # first in the queue, not after its lease. A worker that said it was
    # stopping, or that a deploy replaced, gives the attempt back and the
    # resume is graceful (as a background task).
    resume_when_worker_gone = True
    #: One writer per run turn: an attempt the engine took back stops before its next tool round.
    single_writer_turns = True

    async def deal_claims(
        self, db: AsyncSession, due: list[OrchestrationExecution], worker_id: str
    ) -> list[OrchestrationExecution]:
        """Fair share across fleets, delegated children first (``fair_share_deal``), without the runs of a
        fleet over its run budget (``fleet_run_budget.hold_over_budget``): those stay queued."""
        from app.services.fleet_run_budget import hold_over_budget

        return await hold_over_budget(db, await fair_share_deal(db, due, worker_id))

    async def deal_reserved_claims(
        self, db: AsyncSession, due: list[OrchestrationExecution], worker_id: str
    ) -> list[OrchestrationExecution]:
        """What the delegation reserve may claim once the shared pool is full: the delegated runs whose parent
        run is still running, oldest first."""
        return await children_of_live_parents(db, due)

    def raised_attempt_bound(self, max_attempts: int) -> int:
        """A run queued under the old floor (4) is taken back under the task bound.

        Read by the engine before a take-back's exhaustion check, so a legacy
        row losing its worker before its next claim raises it is not
        dead-lettered at 4.
        """
        from app.services.orchestration_adapters.sub_agent import raised_task_bound

        return raised_task_bound(max_attempts)

    @property
    def cancel_poll_seconds(self) -> float | None:
        """Opt into the reconciler's fast cancel watcher (emergency stop)."""
        return self._configured_cancel_poll()

    async def execute(
        self, ctx: AdapterContext, execution: OrchestrationExecution
    ) -> AdapterResult:
        from app.services.ai_hold import (
            close_hold_if_ai_is_back,
            hold_budget_left,
            hold_result,
        )
        from app.services.assignment_engine import (
            ACTIVE_RUN_STATUSES,
            RUN_DEFERRED,
            TERMINAL_RUN_STATUSES,
            RunHeld,
            execute_assignment_run,
        )

        run_id = execution.payload.get("run_id")
        if not run_id:
            return AdapterResult.failed(
                "Assignment execution payload missing run_id", retryable=False
            )

        from app.services.orchestration_adapters.sub_agent import (
            _end_hand_back,
            _raise_attempt_bound,
            _taken_back_on_its_word,
        )

        # Taken back from a worker that said it was stopping, or that a deploy
        # replaced: a graceful resume (as a background task).
        # Read before the hand-back's record and adapter state are ended. The
        # engine's own reason on a wordless take-back (the worker's host, its
        # version and memory) is not recorded on the run: its
        # ``trigger_context`` is read by whoever can see the run, so a run's resume names the generic cause.
        taken_back_on_its_word = _taken_back_on_its_word(execution)
        # An execution created under the old fixed floor (4) is raised to the
        # bound a new run gets, so the engine does not dead-letter a run that
        # progressed between its losses at its fourth.
        await _raise_attempt_bound(ctx, execution)
        # Handed back by a stopping worker (as a background task):
        # the deferral's record only carried the attempt and runnable
        # budget over to this claim. It is no AI hold, so it ends here, before
        # the gate is read: a hold met next starts its own bound instead of
        # counting from the hand-back.
        execution = await _end_hand_back(ctx, execution)
        # Held until now and agentic AI is back: the hold ends here,
        # before the run starts, so a later hold starts its own bound. The
        # run acts on the same reading of the gate (``None``: never held).
        verdict = await close_hold_if_ai_is_back(
            ctx, execution, user_id=await _held_run_person(ctx, execution, run_id)
        )

        # Rebind the memory event chain the dispatching flow froze onto the run.
        # This is the process boundary: the flow's ContextVar does not
        # exist here, so without this the agent's memory writes would emit at
        # depth 0 with no originating flow, and a flow whose assignment writes
        # memory would retrigger itself indefinitely instead of converging on
        # EVENT_CHAIN_MAX_DEPTH. Bound around the executor and released after,
        # so a worker that runs assignments back to back cannot leak one run's
        # chain into the next.
        chain_token = await _bind_memory_chain(ctx, run_id)
        # ``execute_assignment_run`` owns its own session for the whole run, so
        # this one's connection goes back to the pool first: held, each run pins
        # two. ``expire_on_commit=False`` keeps ``execution`` readable.
        await release_db_connection_before_loop(ctx.db)
        # The run binds its assignment as the author of what it posts;
        # released here too, so the next run of this worker starts unbound.
        from app.services import chat_reply_guard
        from app.services.agent_posts import (
            reset_posting_assignment,
            set_posting_assignment,
        )

        posting_token = set_posting_assignment(None)
        turn_token = chat_reply_guard.bind_turn(None)
        try:
            # ``execute_assignment_run`` owns its own session + transaction,
            # manages the AssignmentRun lifecycle (including internal
            # retry_on_failure and HITL pausing), and never raises on a normal
            # failure. It is re-entrant: invoked on an ``awaiting_human`` run it
            # resumes from the pause.
            outcome = await execute_assignment_run(
                str(run_id),
                hold=hold_budget_left(execution),
                verdict=verdict,
                cancel_event=getattr(ctx, "cancel_event", None),
                deadline_at=getattr(execution, "deadline_at", None),
                attempt=getattr(execution, "attempt_count", None),
                taken_back_on_its_word=taken_back_on_its_word,
            )
        finally:
            reset_posting_assignment(posting_token)
            chat_reply_guard.reset_turn(turn_token)
            if chain_token is not None:
                from app.services.memory_flow_events import (
                    reset_memory_chain_context,
                )

                reset_memory_chain_context(chain_token)

        if isinstance(outcome, RunHeld):
            # Agentic AI is unavailable: the run was left as it was -
            # pending, delayed, or paused with its answer recorded - so it
            # waits, without consuming an attempt, and is asked again later.
            # Read before the status mapping, for the same reason as below.
            return hold_result(execution, outcome.refusal)
        if outcome == assignment_run_resume.RUN_HANDED_BACK:
            # Its worker was asked to stop and the run wound down at an
            # iteration boundary, checkpointed: due again now, resumed
            # by the next worker from there, its work item claims carried. A
            # deferral first in the queue, not a failed attempt (as a
            # background task): a retryable failure spent an attempt
            # per rollout and dead-lettered the run at its fourth. The resume
            # that follows is graceful, bounded by ``MAX_GRACEFUL_RESUMES``,
            # never by ``MAX_RESUMES``.
            from app.services.orchestration_adapters.sub_agent import HANDED_BACK_STATE

            return AdapterResult.defer(
                assignment_run_resume.HANDED_BACK_ERROR,
                delay_seconds=0.0,
                adapter_state=HANDED_BACK_STATE,
                place="head",
            )
        if outcome == RUN_DEFERRED:
            # Whether agentic AI may run could not be read: the run was
            # left as it was - pending, delayed or paused - so hand it back to
            # the engine's retry path. Read before the status mapping below: a
            # paused run must not be re-paused waiting for a resume that
            # already arrived.
            from app.services.agentic_gate import (
                UNKNOWN_STATE_DETAIL,
                UNKNOWN_STATE_RETRY_SECONDS,
            )

            return AdapterResult.failed(
                UNKNOWN_STATE_DETAIL,
                retryable=True,
                backoff_seconds=UNKNOWN_STATE_RETRY_SECONDS,
            )

        run = (
            await ctx.db.execute(
                select(AssignmentRun).where(AssignmentRun.id == uuid.UUID(str(run_id)))
            )
        ).scalar_one_or_none()
        if run is None:
            return AdapterResult.failed(
                f"AssignmentRun {run_id} vanished during execution", retryable=False
            )

        if run.status == "awaiting_human":
            # Pause: keep the execution alive (lease released) with no deadline
            # so it can wait for a human indefinitely. The respond endpoint
            # emits a resume outbox event that re-arms it.
            return AdapterResult.await_event(adapter_state="awaiting_human")
        if run.status in TERMINAL_RUN_STATUSES:
            # The chat that triggered the run hears how it ended.
            assignment_run_wake.schedule(run_id)
            context = getattr(run, "trigger_context", None)
            context = context if isinstance(context, dict) else {}
            if context.get("type") == "work_item":
                # A serial inbox is free again: its next item now, not at the next tick.
                from app.services.work_item_dispatch_wake import notify_dispatch

                await notify_dispatch()
        if run.status == "failed":
            # retry_on_failure is handled inside execute_assignment_run, so a
            # terminal failed status here is definitive — don't double-retry.
            return AdapterResult.failed(
                run.error or "Assignment run failed", retryable=False
            )
        if run.status in TERMINAL_RUN_STATUSES:
            # completed / skipped / cancelled — the dispatch unit is done.
            return AdapterResult.completed()

        # Fail CLOSED: ``execute_assignment_run`` must never return while the
        # run is still active (every legitimate exit commits a terminal status
        # or ``awaiting_human``), and an unknown status string means schema
        # drift/corruption. Finalize the run here so the orchestration
        # execution can never go terminal while the run stays pending/running
        # (the desync class previously recovered only by
        # stale-run cleanup). The write commits atomically with the execution
        # transition in the reconciler's ``_apply_result``.
        prior_status = run.status
        if prior_status in ACTIVE_RUN_STATUSES:
            reason = (
                "Assignment run did not reach a terminal status before the "
                "adapter finished; force-failed to keep run/execution state "
                f"in sync (stuck in '{prior_status}')."
            )
        else:
            reason = (
                "Assignment run has unknown status after execution and was "
                f"force-failed for safety: '{prior_status}'."
            )
        run.status = "failed"
        run.error = reason[:2000]
        run.completed_at = datetime.now(UTC)
        logger.error(
            "Assignment run %s ended adapter execution in non-terminal status "
            "'%s'; force-failing to prevent execution/run terminal-state desync",
            getattr(run, "id", run_id),
            prior_status,
        )
        assignment_run_wake.schedule(run_id)
        return AdapterResult.failed(run.error, retryable=False)

    async def periodic(self, ctx: AdapterContext) -> None:
        """Promote queued deliveries into the slots their assignments have free.

        The per-assignment delivery queue parks an accepted delivery
        as a ``queued`` ``AssignmentRun`` when every slot of its assignment is
        taken. Draining here - rather than from a completion hook alone -
        covers EVERY way a run can stop owning its slot: normal completion,
        failure, dead-letter, and the stale-run sweep's force-fail. A hook on
        the happy path alone would leave a backlog stranded behind a crashed
        run. The hooks exist too, as wakes: an enqueue and the end of an
        assignment execution each nudge the reconciler, so a slot is refilled
        within the cycle rather than the poll interval.

        ``drain_assignment_queues`` opens its own sessions (one per assignment,
        each taking the per-assignment advisory lock and committing) so it never
        interferes with ``ctx.db``, whose transaction the reconciler commits
        after this returns. It is cheap and idempotent: a few grouped queries
        that return nothing at all when no delivery is queued.
        """
        from app.services.assignment_engine import drain_assignment_queues

        await drain_assignment_queues()

    async def handle_event(
        self,
        ctx: AdapterContext,
        execution: OrchestrationExecution,
        event: OrchestrationOutboxEvent,
    ) -> AdapterResult | None:
        """Handle a ``resume`` event for a HITL-paused run.

        Re-arms the (paused, ``running``, lease-released) execution back to
        ``pending`` so ``claim_due`` re-runs ``execute_assignment_run`` under a
        fresh lease + heartbeat. Returns ``None`` — the re-arm is the handling.

        ``rearm_for_resume`` raises ``ResumeNotReadyError`` if the execution is
        still actively lease-held (the resume raced ahead of the AWAIT pause);
        that propagates so the reconciler backs the event off and retries once
        the worker has paused, rather than stealing the live lease.
        """
        if event.event_type != "resume":
            logger.debug("AssignmentAdapter ignoring event_type %s", event.event_type)
            return None
        await engine.rearm_for_resume(ctx.db, execution_id=execution.id)
        return None

    @staticmethod
    async def _settle_failed_run_charge(ctx: AdapterContext, run: AssignmentRun) -> None:
        """Credit back, free, a charge a failed run still records.

        A failure handler takes its own record before it credits, so a record
        left on a failed run is one nobody credited: a stale attempt failed the
        run while the attempt that replaced it was charged, or a handler could
        not take its record. Only a record naming its charge is credited: one
        written before charges were fenced may have been credited already. A
        completed run keeps its charge, the work ran.
        """
        from app.services.attempt_charges import recorded_charge, settle_recorded_charge

        if run.status != "failed":
            return
        charge = recorded_charge(getattr(run, "trigger_context", None))
        if charge is None or charge.charge_id is None:
            return
        # Credit first, record off after: the credit and the charge's
        # receipt move together, so a settling record whose credit the engine
        # landed before its worker died is not credited again, and a credit
        # that did not apply leaves the record for the next pass.
        await settle_recorded_charge(
            ctx.db,
            run,
            charge,
            reason=f"abandoned assignment run {run.id}",
            cause=FailureCause.ENGINE_ABANDONED.value,
        )

    async def on_dead_letter(
        self, ctx: AdapterContext, execution: OrchestrationExecution
    ) -> None:
        """Mark the AssignmentRun failed when the engine gives up on it."""
        from app.services.assignment_engine import TERMINAL_RUN_STATUSES
        from app.services.attempt_charges import (
            attempt_fence,
            recorded_charge,
            without_recorded_charge,
        )

        run_id = execution.payload.get("run_id")
        if not run_id:
            return
        run = (
            await ctx.db.execute(
                select(AssignmentRun)
                .where(AssignmentRun.id == uuid.UUID(str(run_id)))
                .with_for_update()
            )
        ).scalar_one_or_none()
        # Never overwrite an already-terminal run (completed / failed /
        # skipped / cancelled) — only still-active runs need finalizing.
        if run is None:
            return
        if run.status in TERMINAL_RUN_STATUSES:
            await self._settle_failed_run_charge(ctx, run)
            return
        if engine.expired_before_start(execution):
            # Its queue-wait bound passed before any worker started it: nothing
            # ran, nothing was charged, no partner was started. Failed with its
            # own cause and the wait it spent, and NO ``run.failed``: observers
            # of that event must not add work to the queue that starved it.
            await _fail_expired_before_start(ctx, run, execution)
            return
        run.status = "failed"
        # A run lost once more than it may be resumed fails with the resume
        # cap's reason, never the hand-back's "runs again at once".
        run.error = (
            assignment_run_resume.cap_reached_error(
                run.trigger_context,
                attempts=execution.attempt_count or 0,
                allowed=execution.max_attempts or 0,
            )
            or _public_dead_letter_error(execution)
            or "Assignment run was abandoned by the orchestration engine "
            "(deadline exceeded or attempts exhausted)."
        )
        # No attempt will come to collect what its partners still work on,
        # whether its attempts handed it back, were resumed, or a first one
        # lost its worker with nothing recorded: they are cancelled here, the
        # delegation rows concluded with the run's failure (the reconciler
        # commits both). The checkpoint gives way to a tombstone at or after
        # the place of every writer this execution started, a first attempt's
        # included (it records no fence), so no stale writer brings it back.
        await assignment_run_resume.cancel_partner_tasks(ctx.db, run.id)
        await assignment_run_resume.discard(
            run.id,
            place=assignment_run_resume.dead_letter_place(
                attempt_fence(run.trigger_context),
                attempts=execution.attempt_count,
                handed_back=assignment_run_resume.handed_back_attempt(run.trigger_context),
            ),
        )
        # Record WHICH abandonment this was. The engine already knows —
        # ``attempt_count`` separates "nothing ever claimed it" from "claimed
        # and failed until the attempts ran out", and ``deadline_at`` marks the
        # hard timeout — and until now that distinction died with the sweep,
        # leaving every abandoned run with the same opaque sentence.
        #
        # Observability only: the credit-back below stays unconditionally free,
        # exactly as it is written. Worth revisiting in phase 2 with real data
        # though — ``engine_attempts_exhausted`` means the run WAS claimed and
        # kept failing, which is the one dead_letter story that is not purely
        # infrastructure. This is the evidence that argument will need.
        classification = classify_run_failure(
            None,
            FailureContext(
                engine_state=execution.state,
                engine_attempts=execution.attempt_count,
                engine_max_attempts=execution.max_attempts,
                engine_deadline_exceeded=(
                    execution.deadline_at is not None
                    and execution.deadline_at < datetime.now(UTC)
                ),
            ),
        )
        run.failure_cause = classification.cause.value
        run.failure_determination = classification.determination.value
        run.completed_at = datetime.now(UTC)
        # Read after the grace, once the reconciler committed the failure.
        assignment_run_wake.schedule(run_id)

        # ── Credit the agentic action back ───────────────────────────────
        # This is the OTHER way a metered run dies, and it never reaches
        # ``execute_assignment_run``'s failure handler: the engine gives up
        # from a different process (lease expired, worker killed, deadline
        # blown mid-run), so nothing in that function ever runs again. Without
        # this, an action charged up-front stays charged forever for a run the
        # platform simply dropped.
        #
        # Free, never from the agent's failure pool: every way a run reaches
        # dead_letter is infrastructure — nobody claimed it, the lease expired,
        # the process died. The agent did not fail; it was never allowed to
        # finish. Rationing that would let one worker crash spend an agent's
        # whole yearly allowance.
        #
        # ``quota_metered`` is written by the engine at charge time (see
        # ``execute_assignment_run``) precisely so this out-of-process path can
        # tell a charged run from a run that never started or one the quota
        # backend let through unmetered. Its user is written as ``None`` for a
        # run with no account behind it, charged to the platform-wide counters
        # alone; a record without the key, or with a user that does not
        # parse, is not acted on - it could give back another run's charge.
        #
        # The record is marked settling and committed with the failure BEFORE
        # the credit: the attempt the engine gave up on may
        # still be running, and its own failure handler then leaves it. It is
        # taken off the row only once the credit landed (credit first, record
        # off after): a credit that did not apply leaves it to the next pass,
        # and the charge's receipt stops a retried pass crediting it twice.
        charge = recorded_charge(run.trigger_context)
        if charge is not None:
            from app.services.attempt_charges import settle_recorded_charge
            from app.services.quota import refund_agentic_usage_for_provider_outage

            # Which abandonment it was, so the admin card can say what it
            # is forgiving. "Never claimed" and "attempts
            # exhausted" are both free here, and an install seeing mostly
            # the first has a capacity problem, not an agent problem —
            # a distinction the two headline totals cannot express.
            reason = f"abandoned assignment run {run.id}"
            if charge.charge_id is not None:
                await settle_recorded_charge(
                    ctx.db, run, charge, reason=reason, cause=classification.cause.value
                )
            else:
                # A record from before charges were fenced has no receipt:
                # taken off first, then credited, as it always was.
                run.trigger_context = without_recorded_charge(run.trigger_context)
                await ctx.db.commit()
                await refund_agentic_usage_for_provider_outage(
                    charge.user_id,
                    run.agent_id,
                    db=ctx.db,
                    reason=reason,
                    cause=classification.cause.value,
                )

        # The engine gave up on the run: ``run.failed`` goes out once the
        # failure is committed, so a subscriber that reads the run sees it failed.
        from app.services.assignment_engine import _publish_run_failed

        await ctx.db.commit()
        await _publish_run_failed(run, run.error)  # never raises


async def _fail_expired_before_start(
    ctx: AdapterContext, run: AssignmentRun, execution: OrchestrationExecution
) -> None:
    """Fail *run*, never started before its queue-wait bound, with ``deadline_exceeded_before_start``.

    The run record carries ``queue_wait_seconds`` and ``started: false`` under
    ``trigger_context['queue']``. No ``run.failed`` is published.
    """
    waited = engine.queue_wait_seconds(execution)
    run.status = "failed"
    run.error = engine.expired_before_start_message("The assignment run", execution)
    run.failure_cause = FailureCause.DEADLINE_EXCEEDED_BEFORE_START.value
    run.failure_determination = FailureDetermination.STRUCTURAL.value
    run.trigger_context = {
        **(run.trigger_context or {}),
        "queue": {"queue_wait_seconds": waited, "started": False},
    }
    run.completed_at = datetime.now(UTC)
    await ctx.db.commit()
    logger.warning(
        "Assignment run %s never started: its queue-wait bound passed after %s s; failed with %s, no run.failed",
        run.id,
        waited,
        FailureCause.DEADLINE_EXCEEDED_BEFORE_START.value,
    )


class AssignmentRunWakeSweepAdapter(OrchestrationAdapter):
    """The worker's sweep of the run wakes a stopped worker took along.

    No execution is ever created for it: it exists for its periodic hook, at
    most every ``assignment_run_wake.SWEEP_INTERVAL_SECONDS`` per process, in a
    task of its own so the reconciler's cycle never waits on it.
    """

    name = WAKE_SWEEP_ADAPTER
    _sweep_task: asyncio.Task | None = None
    _last_sweep: float | None = None

    async def execute(self, ctx: AdapterContext, execution: OrchestrationExecution) -> AdapterResult:
        return AdapterResult.failed(f"{self.name} runs no executions", retryable=False)

    async def periodic(self, ctx: AdapterContext) -> None:
        if self._sweep_task is not None and not self._sweep_task.done():
            return
        now = time.monotonic()
        if self._last_sweep is not None and now - self._last_sweep < assignment_run_wake.SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep = now
        self._sweep_task = asyncio.create_task(self._sweep())

    async def _sweep(self) -> None:
        try:
            await assignment_run_wake.sweep_lost_wakes()
        except Exception as exc:
            logger.warning(
                "The assignment run wake sweep failed: %s", format_exception_message(exc), exc_info=True
            )
