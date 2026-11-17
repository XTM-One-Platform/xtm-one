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

"""Autonomous attack-path orchestration adapter.

Drives the OpenAEV Autonomous Attack Path (AI penetration testing) through the
durable orchestration engine. One ``OrchestrationExecution`` represents the whole
engagement; each ``execute`` call runs a single bounded decision cycle of the
orchestrator agent (see :mod:`app.services.autonomous_pentest`).

Long-running results (the design crux)
--------------------------------------
A pentest step can resolve in seconds or in hours/days. The adapter never holds
an agent loop open across a slow wait. It maps each cycle's decision onto an FSM
transition:

- ``continue``      -> keep running further decision cycles IN-PROCESS under the
                       SAME lease (a bounded "burst") so back-to-back progress
                       doesn't pay the park + reschedule + reconciler-poll latency
                       between every micro-decision. Once the burst bound is hit
                       (``autonomous_burst_max_cycles`` /
                       ``autonomous_burst_budget_seconds``) it yields back to the
                       reconciler via ``await_event`` + a short-delay self-resume,
                       so leases still rotate fairly across concurrent runs.
- ``await_finding`` -> pause via ``await_event`` + emit a self-resume scheduled at
                       the finding window (default 24h for a phishing/human step).
                       An OpenAEV "finding arrived" callback can emit an EARLIER
                       resume so the run wakes the instant credentials land -
                       event-driven, with the scheduled resume as the fallback.
                       Zero tokens are burned while parked.
- ``waiting_input`` -> pause via ``await_event`` with NO deadline, waiting for an
                       operator directive (resume emitted by the directive path).
- ``completed`` / ``failed`` -> terminal.

An absolute engagement deadline (``deadline_at`` on the execution) is the ultimate
safety net; the deadline sweep dead-letters a run that never terminates, and
``on_dead_letter`` reports that back to OpenAEV so the live view reflects it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time

from sqlalchemy import select

from app.core.config import settings
from app.models.orchestration import (
    TERMINAL_EXECUTION_STATES,
    OrchestrationExecution,
    OrchestrationOutboxEvent,
)
from app.services import orchestration_engine as engine
from app.services.autonomous_pentest import (
    DECISION_AWAIT_FINDING,
    DECISION_COMPLETED,
    DECISION_CONTINUE,
    DECISION_FAILED,
    DECISION_WAITING_INPUT,
    DEFAULT_FINDING_WAIT_SECONDS,
    next_run_at,
    run_autonomous_cycle,
)
from app.services.orchestration_adapters.base import (
    AdapterContext,
    AdapterResult,
    OrchestrationAdapter,
)

logger = logging.getLogger(__name__)

ADAPTER_NAME = "autonomous_pentest"

# Short gap between "continue" cycles - long enough to release the lease and let
# the outbox settle, short enough that the run feels live.
CONTINUE_RESUME_DELAY_SECONDS = 5

# Safety cap on the number of decision cycles a single run may execute, so a
# mis-behaving "always continue" loop can never run forever even if the overall
# deadline is generous.
MAX_CYCLES = 1000

# Built-in cap on consecutive UNANSWERED ``waiting_input`` rechecks for a BUILD
# (plan / author-scenario) run when the operator has NOT configured one
# (``autonomous_max_waiting_input_rechecks`` <= 0). A build is a bounded
# planning session, never an indefinite wait: with the default 5-minute base
# recheck and its exponential back-off, 8 unanswered rechecks span roughly an
# hour, so the build stops with a precise "operator never answered" message
# well inside the 2-hour plan-mode engagement deadline - with a clearer reason
# than the deadline sweep's generic timeout.
DEFAULT_PLAN_WAITING_INPUT_RECHECK_CAP = 8

# Outbox event types this adapter re-arms on. Only ``resume`` (assignment engine +
# this adapter) and ``finding_arrived`` (the platform finding-callback wake path) are
# ever emitted; no code path emits ``directive``, so it is not listed.
_RESUME_EVENT_TYPES = frozenset({"resume", "finding_arrived"})


def _plan_waiting_input_recheck_cap() -> int:
    """Max consecutive unanswered waiting-input rechecks for a BUILD (plan/author).

    Uses the operator-configured cap when positive, else the built-in default -
    never "unlimited" for a build. Live runs do not call this (they are never
    capped on operator wait).
    """
    cap = settings.autonomous_max_waiting_input_rechecks
    if isinstance(cap, int) and cap > 0:
        return cap
    return DEFAULT_PLAN_WAITING_INPUT_RECHECK_CAP


def _waiting_input_timeout_error(rechecks: int) -> str:
    """Human-readable FAILED reason for a build stopped on an unanswered question.

    Surfaced on OpenAEV's run status via ``on_dead_letter`` -> ``_report_run_failed``
    so the operator sees the build did not crash - it waited for an answer that
    never came - and how to resume it.
    """
    return (
        "Plan build stopped: the operator did not answer the orchestrator's "
        f"question after {rechecks} unanswered checks (the first is the question "
        "itself, the rest are periodic rechecks). A build is a bounded planning "
        "session, so it does not wait indefinitely for input. Review the "
        "QUESTION on the run timeline, then relaunch the build with the answer "
        "as the starting directive."
    )


def execution_dedup_key(openaev_run_id: str) -> str:
    """Canonical dedup key for an autonomous run's orchestration execution.

    Shared by the platform enqueue endpoint and the finding-arrived callback so
    both resolve the SAME execution - never inline the format.
    """
    return f"autonomous_run:{openaev_run_id}"


def _parse_adapter_state(raw: str | None) -> dict:
    """Decode the JSON adapter_state carrying the cycle counter + last mode."""
    if not raw:
        return {"cycle": 0}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, TypeError):
        pass
    return {"cycle": 0}


def _engagement_deadline(payload: dict):
    """Parse the absolute engagement deadline stored in the payload, if any.

    ``await_event`` re-assigns ``deadline_at`` every cycle (including ``None``),
    so to keep the ultimate dead-letter safety net alive across parked cycles the
    adapter must re-pass it each time. Returns a timezone-aware datetime or
    ``None`` (indefinite) when unset/unparseable.
    """
    raw = payload.get("engagement_deadline")
    if not raw:
        return None
    try:
        from datetime import UTC, datetime

        dt = datetime.fromisoformat(str(raw))
    except (ValueError, TypeError):
        return None
    # Normalise to a timezone-aware UTC instant. A stored value without an offset
    # would parse as naive, and a naive deadline_at later blows up the deadline
    # sweep's comparison against datetime.now(UTC) with a TypeError.
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _auth_failed_error(outcome) -> str:
    """Human-readable FAILED reason for a run stopped by an unrecoverable auth storm.

    The platform-managed JWT is re-minted transparently on a mid-run 401 (WS2).
    When that re-mint ALSO fails - the operator user was removed, the audience
    base_url is gone, or XTM One's JWKS is unreachable from OpenAEV - every
    OpenAEV REST call 401s and the run can neither progress nor settle itself.
    Rather than burn the whole turn budget re-issuing calls that can never
    succeed, the adapter dead-letters PROMPTLY and reports this to OpenAEV (the
    terminal status is posted with a freshly minted token, so it still lands).
    """
    last_reason = (getattr(outcome, "reason", None) or "").strip()
    tail = f" Last cycle reason: {last_reason}" if last_reason else ""
    return (
        "Autonomous run stopped: every OpenAEV call in the last decision cycle "
        "failed authentication and the platform token could not be re-minted "
        "(the operator account may have been removed, the OpenAEV base URL "
        "changed, or OpenAEV cannot reach XTM One's key endpoint). The run was "
        "dead-lettered immediately instead of looping on calls that cannot "
        "succeed. Verify the OpenAEV integration's operator and base URL, then "
        "relaunch." + tail
    )


def _no_progress_error(streak: int, outcome) -> str:
    """Human-readable FAILED reason for a run stopped by the progress guard.

    Surfaced on OpenAEV's run status via ``on_dead_letter`` -> ``_report_run_failed``
    so the operator sees WHY the run stopped (a stall, not a crash) and what to do.
    """
    last_reason = (getattr(outcome, "reason", None) or "").strip()
    tail = f" Last cycle reason: {last_reason}" if last_reason else ""
    return (
        f"Autonomous run stopped after {streak} consecutive decision cycles with no "
        "forward progress (no attack-path step authored or executed, no arsenal "
        "action created/updated, no scope/finding change). The orchestrator was "
        "looping on reads/searches/consults without advancing the attack path - "
        "likely a capability gap, an unresolved scope question, or an objective "
        "unreachable with the current arsenal. Review the timeline and operator "
        "input, then relaunch." + tail
    )


class AutonomousPentestAdapter(OrchestrationAdapter):
    name = ADAPTER_NAME

    @property
    def concurrency_limit(self) -> int | None:
        """Dedicated concurrency budget for autonomous engagements.

        Autonomous runs are long-lived and cheaply parked; giving them their own
        reconciler semaphore (separate from the shared assignment / sub-agent
        pool) lets dozens run in parallel without starving normal execution, and
        vice-versa. ``0`` / negative -> share the pool (legacy behaviour). Read
        once by the reconciler when the dedicated semaphore is first built.
        """
        limit = settings.max_concurrent_autonomous_runs
        return limit if isinstance(limit, int) and limit > 0 else None

    @property
    def cancel_poll_seconds(self) -> float | None:
        """Fast operator-cancel poll cadence for an in-flight burst.

        An autonomous ``execute`` can run several decision cycles (each a full
        agent loop) under one lease. Without a fast cancel signal, an operator
        Stop only lands at the next cross-side stop check (start of the NEXT
        cycle), so a whole extra decision is authored after the cancel. This
        opts the adapter into the reconciler's fast cancel watcher; the fired
        ``cancel_event`` is threaded into ``run_agent_chat`` so the agent loop
        aborts between iterations. ``0`` disables it (heartbeat backstop only).
        """
        poll = settings.autonomous_cancel_poll_seconds
        return float(poll) if isinstance(poll, (int, float)) and poll > 0 else None

    async def execute(
        self, ctx: AdapterContext, execution: OrchestrationExecution
    ) -> AdapterResult:
        payload = execution.payload or {}
        openaev_run_id = payload.get("openaev_run_id")
        if not openaev_run_id:
            return AdapterResult.failed(
                "Autonomous payload missing openaev_run_id", retryable=False
            )

        state = _parse_adapter_state(execution.adapter_state)
        cycle = int(state.get("cycle", 0))
        # Consecutive no-progress ``continue`` streak, carried across parks. The
        # forward-progress guard (below) trips when it reaches ``max_no_progress``
        # (0 or negative disables the guard).
        try:
            no_progress_streak = max(0, int(state.get("no_progress_streak", 0)))
        except (TypeError, ValueError):
            no_progress_streak = 0
        max_no_progress = settings.autonomous_max_no_progress_cycles
        # Consecutive UNANSWERED ``waiting_input`` rechecks, carried across parks
        # SEPARATELY from the no-progress streak. A waiting_input park resets the
        # no-progress streak (it is an intentional wait, not a stall), so without
        # a dedicated counter a run parked on an unanswered question would recheck
        # forever. It backs off the recheck interval, and caps a BUILD's wait.
        try:
            waiting_input_rechecks = max(0, int(state.get("waiting_input_rechecks", 0)))
        except (TypeError, ValueError):
            waiting_input_rechecks = 0
        plan_mode = bool(payload.get("plan_mode"))

        # Read-transaction hygiene: the cycle owns its own session(s) and can run
        # for minutes, so release any pooled connection this reconciler
        # transaction holds before the long call (mirrors the sub-agent adapter).
        await ctx.db.commit()

        engagement_deadline = _engagement_deadline(payload)

        # In-process "continue" burst bounds. On a ``continue`` decision the run
        # keeps cycling UNDER THE SAME LEASE instead of parking + rescheduling
        # after every micro-decision, until EITHER bound is hit - then it yields
        # back to the reconciler so leases rotate fairly (see config.py).
        burst_budget = max(0, settings.autonomous_burst_budget_seconds)
        burst_max_cycles = max(1, settings.autonomous_burst_max_cycles)
        burst_started = time.monotonic()
        cycles_this_burst = 0

        # Fired by the reconciler's fast cancel watcher the instant an operator
        # stops/deletes the run. Threaded into every cycle's agent loop so it
        # aborts between iterations, and checked between cycles so the burst
        # stops NOW instead of authoring another decision (the bug this fixes).
        cancel_event = ctx.cancel_event

        while True:
            if cancel_event is not None and cancel_event.is_set():
                return self._cancelled_result()

            if cycle >= MAX_CYCLES:
                return AdapterResult.failed(
                    f"Autonomous run exceeded the cycle cap ({MAX_CYCLES}); "
                    "stopping to prevent an unbounded loop.",
                    retryable=False,
                )

            # Pass the current stall streak so the cycle prompt can warn the
            # orchestrator it is looping (the soft guard) before the hard stop.
            cycle_payload = (
                {**payload, "no_progress_streak": no_progress_streak}
                if no_progress_streak > 0
                else payload
            )
            outcome = await run_autonomous_cycle(
                payload=cycle_payload, cycle=cycle, cancel_event=cancel_event
            )
            cycle += 1
            cycles_this_burst += 1

            # A cancel that landed mid-cycle already aborted the agent loop; do
            # NOT act on the (now meaningless) decision - stop the burst so no
            # further step is authored against a cancelled run.
            if cancel_event is not None and cancel_event.is_set():
                return self._cancelled_result()

            if outcome.decision == DECISION_COMPLETED:
                # Gap 1 (success-path decoupling): the orchestrator is SUPPOSED to call
                # update_openaev_run_status(status='COMPLETED') itself, but if the LLM ends a cycle on a
                # "completed" decision without emitting that tool call, OpenAEV would stay RUNNING
                # forever. Settle it here as a best-effort backstop. Skip when (a) the run is in plan
                # mode - it ends PLANNED, set by the orchestrator's own tool call, never COMPLETED - and
                # (b) the completion came from the cross-side stop check (OpenAEV already terminal), so
                # we never overwrite a real CANCELED/FINISHED with COMPLETED. OpenAEV also guards its
                # side (a CANCELED run ignores further status writes), so this is safe either way.
                if not outcome.openaev_already_terminal:
                    if payload.get("plan_mode"):
                        # Plan mode settles to PLANNED, not COMPLETED, and that transition is the
                        # orchestrator's own update_openaev_run_status(status='PLANNED') tool call.
                        # But if the LLM ended the cycle on a "completed" decision without emitting
                        # that call (dropped under the oversized prompt / empty-response recovery),
                        # OpenAEV would be stranded in its pre-PLANNED state forever - the reported
                        # "stuck, header says Waiting for input" stall. Settle PLANNED here as a
                        # best-effort backstop; OpenAEV ignores a redundant PLANNED on an already
                        # PLANNED/terminal run, so a well-behaved cycle is unaffected.
                        await self._settle_openaev_planned(payload, outcome)
                    else:
                        await self._settle_openaev_completed(payload, outcome)
                return AdapterResult.completed(
                    payload_update={
                        "last_reason": outcome.reason or "objective achieved"
                    }
                )

            if outcome.decision == DECISION_FAILED:
                # A failed cycle is definitive for the engagement - the orchestrator
                # already recorded the failure to OpenAEV's timeline. Non-retryable so
                # the reconciler routes it to on_dead_letter for the status callback.
                return AdapterResult.failed(
                    outcome.error or outcome.reason or "Autonomous run failed",
                    retryable=False,
                )

            # ── Unrecoverable auth-storm guard (trips immediately) ─────────
            # The platform JWT is re-minted transparently on a mid-run 401 (WS2
            # part 1). When that re-mint ALSO fails, EVERY OpenAEV call 401s and
            # the run can neither progress nor settle itself. A cycle dominated by
            # auth failures that authored nothing is unrecoverable, so dead-letter
            # it NOW (one auth-only cycle, not ``max_no_progress`` of them) rather
            # than run out the turn budget re-issuing calls that cannot succeed.
            # Non-retryable -> on_dead_letter posts FAILED with a freshly minted
            # token, so the terminal status lands despite the expired loop token.
            if (
                outcome.decision == DECISION_CONTINUE
                and outcome.auth_failed
                and not outcome.made_progress
            ):
                logger.warning(
                    "[Autonomous] Run %s hit an unrecoverable auth storm "
                    "(JWT re-mint failed); dead-lettering immediately.",
                    openaev_run_id,
                )
                return AdapterResult.failed(
                    _auth_failed_error(outcome), retryable=False
                )

            # ── Forward-progress (convergence) guard ──────────────────────
            # Only a ``continue`` decision can stall. The terminal decisions
            # returned above, and the genuine parks below (await_finding /
            # waiting_input), all represent real progress or an intentional wait,
            # so they RESET the streak. A ``continue`` cycle that changed no
            # OpenAEV state (see PROGRESS_TOOLS) advances the streak; once it
            # reaches the configured cap the run is stopped and reported FAILED to
            # OpenAEV, instead of looping all the way to MAX_CYCLES burning
            # tokens/quota while authoring nothing (the observed stall).
            if outcome.decision == DECISION_CONTINUE and not outcome.made_progress:
                no_progress_streak += 1
                if 0 < max_no_progress <= no_progress_streak:
                    logger.warning(
                        "[Autonomous] Run %s stalled: %s consecutive no-progress "
                        "cycles; stopping to prevent an unbounded loop.",
                        openaev_run_id,
                        no_progress_streak,
                    )
                    return AdapterResult.failed(
                        _no_progress_error(no_progress_streak, outcome),
                        retryable=False,
                    )
            else:
                # A progress cycle or a genuine park - the run is advancing.
                no_progress_streak = 0

            # Track consecutive UNANSWERED waiting-input rechecks: bump it on a
            # waiting_input park where the operator did NOT respond this cycle,
            # and reset it otherwise (any other decision, or a waiting_input park
            # that consumed a fresh directive - a live dialogue, not a stall).
            if (
                outcome.decision == DECISION_WAITING_INPUT
                and not outcome.consumed_directive
            ):
                waiting_input_rechecks += 1
            else:
                waiting_input_rechecks = 0

            # Every remaining decision PARKS the run (await_event releases the
            # lease). adapter_state carries the NEXT cycle number so the resume
            # continues from where we stopped. We emit the resume that will wake
            # it, then return the await transition; the reconciler applies
            # await_event AFTER this returns, and the outbox resume settles once
            # the pause has released the lease (ResumeNotReadyError back-off
            # handles the race by design).
            next_state = self._serialize_state(
                cycle, outcome.decision, no_progress_streak, waiting_input_rechecks
            )

            if outcome.decision == DECISION_WAITING_INPUT:
                # A BUILD (plan / author) is a bounded planning session, so an
                # unanswered HITL question must not park it indefinitely. Stop it
                # after a finite number of unanswered rechecks with a clear
                # operator-facing reason. Live runs are NOT capped here - a real
                # engagement legitimately waits on a human for as long as it takes.
                if plan_mode:
                    cap = _plan_waiting_input_recheck_cap()
                    if 0 < cap <= waiting_input_rechecks:
                        return AdapterResult.failed(
                            _waiting_input_timeout_error(waiting_input_rechecks),
                            retryable=False,
                        )
                # Wait for the operator; the directive callback emits the resume.
                # Schedule a periodic self-resume re-check (exponentially backed
                # off with the recheck count) so an answered directive (or an
                # out-of-band OpenAEV stop) is still picked up even if the
                # directive callback's resume event was lost. Re-pass the absolute
                # engagement deadline for a BUILD so a parked plan stays bounded
                # (the start wiring's 1h plan deadline); a LIVE run keeps NO
                # deadline - a human may take arbitrarily long and it must never
                # be silently dead-lettered.
                await self._schedule_waiting_input_recheck(
                    ctx,
                    execution,
                    cycle,
                    outcome.decision,
                    rechecks=waiting_input_rechecks,
                )
                waiting_deadline = engagement_deadline if plan_mode else None
                return AdapterResult.await_event(
                    adapter_state=next_state, deadline_at=waiting_deadline
                )

            if outcome.decision == DECISION_AWAIT_FINDING:
                # Long wait for a finding (default 24h for a phishing/human step);
                # an earlier OpenAEV finding-arrived callback can pre-empt it.
                await self._emit_resume(
                    ctx,
                    execution,
                    cycle,
                    outcome.decision,
                    delay=outcome.wait_seconds or DEFAULT_FINDING_WAIT_SECONDS,
                )
                return AdapterResult.await_event(
                    adapter_state=next_state, deadline_at=engagement_deadline
                )

            # DECISION_CONTINUE (default): the orchestrator has more to do right
            # now. Keep looping IN-PROCESS while burst budget remains so the run
            # doesn't pay the park + reschedule + poll latency between cycles.
            elapsed = time.monotonic() - burst_started
            if cycles_this_burst < burst_max_cycles and elapsed < burst_budget:
                # Checkpoint the progress cursor before looping so a worker crash
                # mid-burst resumes from THIS cycle instead of rewinding to the
                # last park and re-running the whole burst. It also re-asserts
                # lease ownership: if we lost the run out-of-band, stop the burst
                # now rather than author another cycle against it.
                if not await self._checkpoint_state(ctx, execution, next_state):
                    return self._cancelled_result()
                continue
            # Burst bound hit: yield back to the reconciler with a short-delay
            # self-resume so leases rotate fairly across concurrent runs.
            await self._emit_resume(
                ctx,
                execution,
                cycle,
                outcome.decision,
                delay=CONTINUE_RESUME_DELAY_SECONDS,
            )
            return AdapterResult.await_event(
                adapter_state=next_state, deadline_at=engagement_deadline
            )

    def _cancelled_result(self) -> AdapterResult:
        """Result to return when a cancel was detected mid-burst.

        The execution is already terminal (``cancel_execution`` set it
        ``cancelled`` + cleared the lease), so the reconciler's follow-up
        transition raises ``LeaseLostError`` and is swallowed - the run stays
        ``cancelled``. We stop the burst rather than parking a self-resume, so
        no scheduled event can revive it and no further step is authored.
        """
        return AdapterResult.completed(
            payload_update={"last_reason": "run cancelled by operator"}
        )

    async def _emit_resume(
        self,
        ctx: AdapterContext,
        execution: OrchestrationExecution,
        cycle: int,
        mode: str,
        *,
        delay: float,
    ) -> None:
        """Emit the self-resume outbox event that wakes a parked run's next cycle.

        ``dedup_key`` is unique per cycle so each park gets a fresh resume (never
        deduped against a prior cycle's event).
        """
        await engine.emit_outbox_event(
            ctx.db,
            execution_id=execution.id,
            event_type="resume",
            dedup_key=f"cycle-resume:{execution.id}:{cycle}",
            payload={"cycle": cycle, "mode": mode},
            available_at=next_run_at(delay_seconds=delay),
        )

    @staticmethod
    def _serialize_state(
        cycle: int, mode: str, no_progress_streak: int, waiting_input_rechecks: int
    ) -> str:
        """Serialize the adapter's progress cursor for adapter_state.

        Single source of truth for the JSON shape written by both the park
        transitions and the mid-burst checkpoint, so a recovery resume reads the
        same fields wherever the crash landed.
        """
        return json.dumps(
            {
                "cycle": cycle,
                "mode": mode,
                "no_progress_streak": no_progress_streak,
                "waiting_input_rechecks": waiting_input_rechecks,
            }
        )

    async def _checkpoint_state(
        self,
        ctx: AdapterContext,
        execution: OrchestrationExecution,
        state_json: str,
    ) -> bool:
        """Mid-burst persistence of the progress cursor. Returns keep-going.

        A ``continue`` burst runs several cycles under one lease without parking,
        so without this a worker crash mid-burst rewinds the run to the last PARK
        and re-runs every cycle since. Persist (and commit, so it survives a
        crash) after each in-burst cycle.

        Returns ``True`` when the burst may keep running and ``False`` when it
        must stop NOW because this worker no longer owns the run (the lease was
        stolen or the row went terminal out-of-band - ``checkpoint_adapter_state``
        raises ``LeaseLostError``): continuing would author another cycle against
        a run a second worker is already driving. Any OTHER failure (a transient
        DB hiccup) is best-effort - swallowed so a healthy burst is never aborted,
        recovery just rewinds to the previous cursor as it did before.
        """
        try:
            await engine.checkpoint_adapter_state(
                ctx.db,
                execution_id=execution.id,
                worker_id=ctx.worker_id,
                adapter_state=state_json,
            )
            await ctx.db.commit()
            return True
        except engine.LeaseLostError:
            with contextlib.suppress(Exception):
                await ctx.db.rollback()
            logger.info(
                "[Autonomous] Lost ownership of run %s mid-burst; stopping "
                "before authoring another cycle.",
                execution.id,
            )
            return False
        except Exception:
            logger.debug(
                "[Autonomous] Mid-burst adapter_state checkpoint failed for %s; "
                "recovery will rewind to the previous cursor.",
                execution.id,
                exc_info=True,
            )
            with contextlib.suppress(Exception):
                await ctx.db.rollback()
            return True

    async def _schedule_waiting_input_recheck(
        self,
        ctx: AdapterContext,
        execution: OrchestrationExecution,
        cycle: int,
        mode: str,
        *,
        rechecks: int = 0,
    ) -> None:
        """Schedule a periodic re-check for a run parked on ``waiting_input``.

        A ``waiting_input`` park's directive-callback resume could be lost, so
        this belt-and-suspenders self-resume wakes the run to re-consume any
        answered directive and re-run the cross-side stop check; the cycle either
        proceeds or re-parks (scheduling the next re-check). ``0`` disables it.

        The interval is exponentially BACKED OFF by the consecutive unanswered
        recheck count (``rechecks``), from
        ``autonomous_waiting_input_recheck_seconds`` up to
        ``autonomous_waiting_input_recheck_max_seconds``, so a run waiting a long
        time on a human does not burn a full decision cycle every base interval
        forever. A genuine operator answer wakes the run IMMEDIATELY via the
        event-driven directive resume regardless of this back-off, so it costs
        nothing in responsiveness. The ``waiting-input-recheck`` dedup prefix
        keeps these distinct from the ``cycle-resume`` events used by continue /
        await_finding.
        """
        base = max(0, settings.autonomous_waiting_input_recheck_seconds)
        if base <= 0:
            return
        cap = settings.autonomous_waiting_input_recheck_max_seconds
        cap = max(base, cap) if isinstance(cap, int) and cap > 0 else base
        # rechecks is the post-increment count (1 on the first park): exponent 0
        # -> base, 1 -> base*2, ... clamped so the shift can never overflow.
        exponent = min(max(0, rechecks - 1), 20)
        delay = min(base * (2**exponent), cap)
        await engine.emit_outbox_event(
            ctx.db,
            execution_id=execution.id,
            event_type="resume",
            dedup_key=f"waiting-input-recheck:{execution.id}:{cycle}",
            payload={"cycle": cycle, "mode": mode},
            available_at=next_run_at(delay_seconds=delay),
        )

    async def handle_event(
        self,
        ctx: AdapterContext,
        execution: OrchestrationExecution,
        event: OrchestrationOutboxEvent,
    ) -> AdapterResult | None:
        """Re-arm a parked run on resume / finding-arrived / directive events.

        Returns ``None`` - the re-arm IS the handling; ``claim_due`` then re-runs
        ``execute`` for the next cycle under a fresh lease. ``rearm_for_resume``
        raises ``ResumeNotReadyError`` if the pause has not released the lease yet
        (the resume raced the await); that propagates so the outbox backs the
        event off and retries, never stealing a live lease.
        """
        if event.event_type not in _RESUME_EVENT_TYPES:
            logger.debug(
                "AutonomousPentestAdapter ignoring event_type %s", event.event_type
            )
            return None
        # A cancel (operator stop / delete / restart on the OpenAEV side) makes the
        # execution terminal. A self-resume the prior cycle already scheduled can
        # still fire afterwards; re-arming a terminal row would raise
        # InvalidTransitionError and pointlessly back the event off. No-op instead
        # so a cancelled run stays stopped and the loop dies cleanly.
        if execution.state in TERMINAL_EXECUTION_STATES:
            logger.debug(
                "AutonomousPentestAdapter ignoring %s for terminal execution %s",
                event.event_type,
                execution.id,
            )
            return None
        await engine.rearm_for_resume(ctx.db, execution_id=execution.id)
        return None

    async def _settle_openaev_completed(self, payload: dict, outcome) -> None:
        """Best-effort backstop that reports COMPLETED to OpenAEV (gap 1).

        Called only for a genuine live-run completion (not plan mode, not a stop-check completion).
        Failing to report must never turn a finished engagement into a crash, so every error is
        swallowed - OpenAEV's own timeout watchdog is the ultimate liveness guarantee.
        """
        openaev_run_id = payload.get("openaev_run_id")
        callback_base_url = payload.get("callback_base_url")
        if not (openaev_run_id and callback_base_url):
            return
        try:
            await _report_run_completed(
                base_url=str(callback_base_url),
                openaev_run_id=str(openaev_run_id),
                reason=outcome.reason or "Objective achieved.",
                user_id=payload.get("user_id"),
            )
        except Exception:  # a finished run must never fail on the status callback
            logger.warning(
                "[Autonomous] Failed to report COMPLETED for run %s to OpenAEV",
                openaev_run_id,
                exc_info=True,
            )

    async def _settle_openaev_planned(self, payload: dict, outcome) -> None:
        """Best-effort backstop that reports PLANNED to OpenAEV for a plan-mode run.

        The orchestrator is SUPPOSED to call ``update_openaev_run_status(status='PLANNED')`` itself
        when a plan cycle signals "completed". If it drops that call, this ensures the run still
        reaches its settled state instead of stranding in WAITING_INPUT/PLANNING. Every error is
        swallowed - a finished plan must never fail on the status callback (OpenAEV's timeout
        watchdog is the ultimate backstop, though a plan run is untimed).
        """
        openaev_run_id = payload.get("openaev_run_id")
        callback_base_url = payload.get("callback_base_url")
        if not (openaev_run_id and callback_base_url):
            return
        try:
            await _report_run_planned(
                base_url=str(callback_base_url),
                openaev_run_id=str(openaev_run_id),
                reason=outcome.reason or "Plan ready.",
                user_id=payload.get("user_id"),
            )
        except Exception:  # a settled plan must never fail on the status callback
            logger.warning(
                "[Autonomous] Failed to report PLANNED for run %s to OpenAEV",
                openaev_run_id,
                exc_info=True,
            )

    async def on_dead_letter(
        self, ctx: AdapterContext, execution: OrchestrationExecution
    ) -> None:
        """Report a dead-lettered engagement back to OpenAEV's run status.

        Reached when the absolute engagement deadline is exceeded or attempts are
        exhausted (e.g. repeated worker crashes). Best-effort: the callback failing
        must never crash the reconciler's cleanup pass.
        """
        payload = execution.payload or {}
        openaev_run_id = payload.get("openaev_run_id")
        callback_base_url = payload.get("callback_base_url")
        if not (openaev_run_id and callback_base_url):
            return
        error = (
            execution.last_error
            or "Autonomous run abandoned by the orchestration engine "
            "(deadline exceeded or attempts exhausted)."
        )
        try:
            await _report_run_failed(
                base_url=str(callback_base_url),
                openaev_run_id=str(openaev_run_id),
                error=error,
                user_id=payload.get("user_id"),
            )
        except Exception:  # cleanup must not raise
            logger.warning(
                "[Autonomous] Failed to report dead-lettered run %s to OpenAEV",
                openaev_run_id,
                exc_info=True,
            )


async def _report_run_failed(
    *, base_url: str, openaev_run_id: str, error: str, user_id: object
) -> None:
    """POST a FAILED status update to OpenAEV's autonomous-run status endpoint."""
    await _post_run_status(
        base_url=base_url,
        openaev_run_id=openaev_run_id,
        user_id=user_id,
        body={
            "status": "FAILED",
            "last_error": error[:2000],
            "title": "Run abandoned",
            "content": error[:2000],
        },
    )


async def _report_run_completed(
    *, base_url: str, openaev_run_id: str, reason: str, user_id: object
) -> None:
    """POST a COMPLETED status update to OpenAEV's autonomous-run status endpoint (gap 1 backstop)."""
    await _post_run_status(
        base_url=base_url,
        openaev_run_id=openaev_run_id,
        user_id=user_id,
        body={
            "status": "COMPLETED",
            "title": "Run completed",
            "content": (reason or "Objective achieved.")[:2000],
        },
    )


async def _report_run_planned(
    *, base_url: str, openaev_run_id: str, reason: str, user_id: object
) -> None:
    """POST a PLANNED status update to OpenAEV's autonomous-run status endpoint (plan-mode backstop)."""
    await _post_run_status(
        base_url=base_url,
        openaev_run_id=openaev_run_id,
        user_id=user_id,
        body={
            "status": "PLANNED",
            "title": "Plan ready",
            "content": (reason or "Plan ready.")[:2000],
        },
    )


async def _post_run_status(
    *, base_url: str, openaev_run_id: str, user_id: object, body: dict
) -> None:
    """POST a status update to OpenAEV's autonomous-run status endpoint.

    Mints a cross-platform Ed25519 JWT for the run's operator (verified by
    OpenAEV against XTM One's JWKS) so the update is attributed to the real user,
    exactly like the orchestrator's in-cycle callbacks. No-op when the operator
    can't be resolved.
    """
    import httpx

    from app.core.database import async_session
    from app.core.platform_identity import resolve_platform_call
    from app.core.utils import safe_uuid
    from app.core.xtm_auth import issue_xtm_jwt
    from app.models.user import User

    user_uuid = safe_uuid(str(user_id)) if user_id else None
    if user_uuid is None:
        # A status callback that silently no-ops leaves OpenAEV's run view stuck
        # on the last state (e.g. RUNNING) even though the engagement ended -
        # the operator has no way to know the callback was dropped. Warn so it is
        # visible in the worker logs. There is deliberately NO service-JWT
        # fallback here: OpenAEV's XtmJwksExtractor resolves the callback token
        # strictly by its ``email`` claim to an EXISTING user (findByEmailIgnoreCase)
        # and has no service identity, so a synthetic service token would 401.
        # A real callback service identity is OpenAEV-side work;
        # until then the freshly minted operator token below is the only token
        # that lands, which already fixes the observed failure (token EXPIRY, not
        # an unresolvable operator).
        logger.warning(
            "[Autonomous] No resolvable operator id on run %s; status callback "
            "(%s) was NOT delivered - OpenAEV's run view may lag its real state.",
            openaev_run_id,
            body.get("status"),
        )
        return
    async with async_session() as db:
        user = (
            await db.execute(select(User).where(User.id == user_uuid))
        ).scalar_one_or_none()
        call = await resolve_platform_call(db, base_url)
    if user is None:
        logger.warning(
            "[Autonomous] Operator %s for run %s not found; status callback (%s) "
            "was NOT delivered - OpenAEV's run view may lag its real state.",
            user_uuid,
            openaev_run_id,
            body.get("status"),
        )
        return

    token = issue_xtm_jwt(user_uuid, user.email, call.audience)
    url = f"{call.base_url}/api/autonomous-runs/{openaev_run_id}/status"
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            url, json=body, headers={"Authorization": f"Bearer {token}"}
        )
        resp.raise_for_status()
