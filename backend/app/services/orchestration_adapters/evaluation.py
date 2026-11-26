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

"""Evaluation adapter.

Wraps evaluation run execution and episode callback resume in the durable
engine. A normal trigger runs ``execute_evaluation`` for the whole evaluation;
a ``resume_episode`` payload drives ``process_resume_episode`` for a single
episode. Episodes still waiting on an external callback are timed out by the
``periodic`` hook, which also runs the stranded-run sweep.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import Numeric, case, cast, func, or_, select, update

from app.core.utils import format_exception_message
from app.models.orchestration import OrchestrationExecution
from app.services.evaluation.availability import (
    DISABLED_DETAIL,
    engine_accepts,
    engine_accepts_runs,
)
from app.services.evaluation.run_ownership import (
    runs_owned_by_another,
    runs_this_execution_may_touch,
)
from app.services.orchestration_adapters.base import (
    AdapterContext,
    AdapterResult,
    OrchestrationAdapter,
)

logger = logging.getLogger(__name__)

ADAPTER_NAME = "evaluation"


def _cut_by_a_stop(execution: OrchestrationExecution) -> bool:
    """Whether the runs this execution left ``"running"`` were cut by a stop outside the work, not resumed from yet.

    ``evaluation_engine.pending_stop_cut``: a hand-back's marker or a take-back
    from a worker that said it was stopping, recorded in the payload, which
    the retries, claims and AI holds in between keep, and consumed by the
    reclaim that resumed from it - unlike the ``worker_gone`` / ``handed_back``
    adapter state, which a retry clears and a hold replaces.
    """
    from app.services.evaluation_engine import pending_stop_cut

    return pending_stop_cut(getattr(execution, "payload", None)) is not None


class EvaluationAdapter(OrchestrationAdapter):
    name = ADAPTER_NAME
    # A replaced evaluation worker must stop judging runs its replacement is
    # already judging, paying twice. Its writes are ownership-checked
    # and an interrupted entry is abandoned whole.
    stop_on_lease_loss = True
    # A rollout is not the work's fault: taken back the moment its worker is
    # known gone, first in the queue, the attempt given back when that worker
    # said it was stopping (or a deploy replaced it). Its runs then resume
    # from their finished entries (``execute_evaluation(resume_stopped_runs=)``)
    # instead of being failed as abandoned, and a run of deploys no longer
    # dead-letters it.
    resume_when_worker_gone = True

    @property
    def cancel_poll_seconds(self) -> float | None:
        """Opt into the reconciler's fast cancel watcher (emergency stop)."""
        return self._configured_cancel_poll()

    async def execute(
        self, ctx: AdapterContext, execution: OrchestrationExecution
    ) -> AdapterResult:
        kind = execution.payload.get("kind")

        if kind == "resume_episode":
            from app.services.evaluation_continuation import process_resume_episode

            episode_id = execution.payload.get("episode_id")
            if not episode_id:
                return AdapterResult.failed(
                    "resume_episode payload missing episode_id", retryable=False
                )
            await process_resume_episode(ctx.db, episode_id=str(episode_id))
            return AdapterResult.completed()

        if kind == "rescore_runs":
            from app.services.evaluation_engine import (
                SharedPerformanceRecomputeError,
                execute_rescore_runs,
            )

            run_ids = execution.payload.get("run_ids") or []
            if not run_ids:
                return AdapterResult.failed(
                    "rescore_runs payload missing run_ids", retryable=False
                )
            # Evaluation off (#5503): no new work; each run goes back as it was.
            # The engine gate (#8257): a Platform Health launch runs whatever the mode.
            if not await engine_accepts_runs(ctx.db, run_ids):
                await _fail_rescore_runs(
                    run_ids, execution.id, DISABLED_DETAIL, _previous_statuses(execution, run_ids)
                )
                return AdapterResult.failed(DISABLED_DETAIL, retryable=False)
            # Resume support: the durable engine can retry the SAME
            # payload on another worker after a crash. The idempotence key is
            # the RUN's own status, trustworthy because ``rescore_run`` commits
            # the status flip in the SAME transaction as its scores;
            # otherwise a retry would re-judge, and re-bill, a scored run.
            #
            # Not ``adapter_state``: every engine retry path clears it, and
            # this adapter never parks on an event, the one transition that
            # keeps it. ``_rescore_run_with_own_session`` re-checks the status
            # and skips anything no longer ``"running"``.
            try:
                await execute_rescore_runs(
                    run_ids,
                    cancel_event=ctx.cancel_event,
                    previous_statuses=_previous_statuses(execution, run_ids),
                    execution_id=execution.id,
                )
            except asyncio.CancelledError:
                # Same classification as the evaluation branch below.
                # A raw ``CancelledError`` misses ``_do_execute``'s lease-loss
                # branch, and if ``asyncio.timeout`` converted it, "Execution
                # timed out" would be written on a row the replacement owns.
                if ctx.cancel_event is not None and ctx.cancel_event.is_set():
                    from app.services.orchestration_engine import LeaseLostError

                    logger.info(
                        "Re-score of %d run(s): stopping, this execution is no "
                        "longer ours",
                        len(run_ids),
                    )
                    raise LeaseLostError(
                        "lease lost during re-score"
                    ) from None
                raise
            except SharedPerformanceRecomputeError as exc:
                logger.error(
                    "execute_rescore_runs failed for runs %s: %s",
                    run_ids,
                    format_exception_message(exc),
                    exc_info=True,
                )
                # Distinct from the generic branch below: this is
                # raised only after every run in ``run_ids`` is terminal (see
                # ``_recompute_shared_performance_basis``), so a retry cannot
                # re-judge a run and only re-attempts this recompute.
                # ``_fail_rescore_runs`` would be a no-op: no row is still
                # ``"running"``.
                return AdapterResult.failed(str(exc), retryable=True)
            except Exception as exc:
                logger.error(
                    "execute_rescore_runs failed for runs %s: %s",
                    run_ids,
                    format_exception_message(exc),
                    exc_info=True,
                )
                # Per-run failures are isolated inside
                # ``_rescore_run_with_own_session`` (never raises), so the
                # batch itself broke. Non-retryable: a retry could
                # double-score runs that already finished.
                await _fail_rescore_runs(
                    run_ids, execution.id, str(exc), _previous_statuses(execution, run_ids)
                )
                return AdapterResult.failed(str(exc), retryable=False)
            finally:
                # A rescore's judge calls emit Langfuse spans, which would
                # otherwise sit in the exporter queue, or be lost on a restart.
                await _flush_langfuse(f"rescore of runs {run_ids}")
            return AdapterResult.completed()

        from app.services.evaluation_engine import (
            AbandonedWorkRepairError,
            execute_evaluation,
        )

        # Bound here, not inside the handler below, because an ``except``
        # clause needs the name at the time it is evaluated. Lazy rather than
        # module-scope: orchestration_engine imports the adapter registry.
        from app.services.orchestration_engine import LeaseLostError

        evaluation_id = execution.payload.get("evaluation_id")
        if not evaluation_id:
            return AdapterResult.failed(
                "Evaluation payload missing evaluation_id", retryable=False
            )
        from app.services.orchestration_adapters.sub_agent import (
            HANDED_BACK_STATE,
            _end_hand_back,
        )

        # Read before the hand-back's record and adapter state are ended.
        resume_stopped_runs = _cut_by_a_stop(execution)
        # A hand-back's deferral record only carried the attempt over to this
        # claim. It is no AI hold, so it ends here, before the gate is read: a
        # hold met next starts its own bound instead of counting from the
        # hand-back.
        execution = await _end_hand_back(ctx, execution)
        # Evaluation off: a fresh evaluation does not start, its pending
        # runs failed with the reason. A resumed episode above is work in flight.
        # The engine gate (#8257): a Platform Health launch runs whatever the mode.
        if not await engine_accepts(ctx.db, evaluation_id):
            await _fail_active_runs(
                str(evaluation_id), execution.id, DISABLED_DETAIL, statuses=("pending",)
            )
            return AdapterResult.failed(DISABLED_DETAIL, retryable=False)
        # A fresh evaluation has not started: while agentic AI is unavailable
        # here - switched off, or Community Edition - it is HELD, its
        # runs still pending, and runs once AI is available again.
        # Once the hold is over it is failed with the reason on each PENDING
        # run, the batch ``execute_evaluation`` would pick up. A running
        # episode waiting on its callback is work in flight and resumes above,
        # untouched. A refusal nothing was read for (a database blip on a cold
        # worker) fails nothing: the evaluation is handed back to the engine's
        # retry path, its runs still pending.
        from app.services.agentic_gate import (
            UNKNOWN_STATE_DETAIL,
            UNKNOWN_STATE_RETRY_SECONDS,
            agentic_verdict,
        )
        from app.services.ai_hold import (
            close_hold_if_ai_is_back,
            expired_message,
            hold_budget_left,
            hold_result,
        )
        from app.services.orchestration_engine import hold_record

        verdict = await agentic_verdict(ctx.db)
        if verdict.transient:
            return AdapterResult.failed(
                UNKNOWN_STATE_DETAIL,
                retryable=True,
                backoff_seconds=UNKNOWN_STATE_RETRY_SECONDS,
            )
        refusal = verdict.refusal
        if refusal is not None and hold_budget_left(execution):
            return hold_result(execution, refusal)
        if refusal is not None:
            message = expired_message(
                refusal, was_held=hold_record(execution) is not None
            )
            await _fail_active_runs(
                str(evaluation_id), execution.id, message, statuses=("pending",)
            )
            return AdapterResult.failed(message, retryable=False)
        # Held until now and agentic AI is back: the hold ends here,
        # before the evaluation starts, so a later hold starts its own bound.
        await close_hold_if_ai_is_back(ctx, execution, verdict)

        trigger_reason = execution.payload.get("trigger_reason", "manual")
        try:
            handed_back = await execute_evaluation(
                ctx.db,
                str(evaluation_id),
                trigger_reason,
                execution.id,
                cancel_event=ctx.cancel_event,
                # The lease this call is allowed to write under. The
                # pre-dispatch reclaim commits under it rather than merely
                # checking an event beforehand.
                worker_id=ctx.worker_id,
                # From the moment a stop is asked (``draining``, the stop
                # signal), not when the stop itself comes (``stopping``, past
                # the shutdown grace): the entry in flight gets the whole grace
                # to finish before its run pauses.
                stopping=ctx.draining or ctx.stopping,
                resume_stopped_runs=resume_stopped_runs,
            )
        except asyncio.CancelledError:
            # The batch was cancelled. If our own cancel event is what fired,
            # this is a deliberate stop rather than the loop shutting down, and
            # the work now belongs to a replacement.
            #
            # ``LeaseLostError`` lands in ``_do_execute``'s handler, which
            # writes NO terminal state: the row is no longer ours.
            # ``AdapterResult.failed`` would fail runs the replacement is about
            # to complete.
            if ctx.cancel_event is not None and ctx.cancel_event.is_set():
                # Neutral: this fires for an operator cancel too, not only a
                # reassignment.
                logger.info(
                    "Evaluation %s: stopping, this execution is no longer ours",
                    evaluation_id,
                )
                raise LeaseLostError(
                    f"lease lost during evaluation {evaluation_id}"
                ) from None
            # Not ours: a genuine task cancellation (shutdown). Propagate.
            raise
        except LeaseLostError:
            # Already classified, by ``_fence_on_lease`` refusing to commit on
            # a lease that moved. It must reach ``_do_execute``
            # untouched: the generic arm below would fail runs the replacement
            # is about to complete.
            raise
        except AbandonedWorkRepairError as exc:
            logger.error(
                "execute_evaluation could not repair abandoned work for %s: %s",
                evaluation_id,
                format_exception_message(exc),
                exc_info=True,
            )
            # Retryable, and ``_fail_active_runs`` is skipped: this
            # raises BEFORE anything is dispatched or committed, so the retry
            # must find the runs as they are. The generic branch would fail
            # them WITHOUT their episodes, the stranded state the repair
            # exists to remove.
            return AdapterResult.failed(str(exc), retryable=True)
        except Exception as exc:
            logger.error(
                "execute_evaluation failed for %s: %s", evaluation_id, format_exception_message(exc), exc_info=True
            )
            # Fail any runs the partial execution left active: the only
            # recovery path for an interrupted batch. Non-retryable,
            # to avoid double-scoring completed runs.
            await _fail_active_runs(str(evaluation_id), execution.id, str(exc))
            await _recover_stranded_headlines(str(evaluation_id))
            return AdapterResult.failed(str(exc), retryable=False)
        finally:
            await _flush_langfuse(f"evaluation {evaluation_id}")
        if handed_back is True:
            # Every run in flight paused at an entry boundary, ``pending``
            # again: handed back first in the queue, no attempt spent, for
            # the incoming release to resume (``_to_incoming_release``).
            logger.warning(
                "Evaluation %s: its worker is stopping, handed back with its "
                "runs paused at an entry boundary",
                evaluation_id,
            )
            from app.services.evaluation_engine import HANDED_BACK_PAYLOAD_KEY

            return AdapterResult.defer(
                "Its worker is stopping: the runs resume on the next worker",
                delay_seconds=0,
                adapter_state=HANDED_BACK_STATE,
                place="head",
                # The stop marker the next attempt resumes on, whatever its
                # claim, a retry or an AI hold does to the adapter state first.
                payload_update={HANDED_BACK_PAYLOAD_KEY: {"at": datetime.now(UTC).isoformat()}},
            )
        return AdapterResult.completed()

    async def on_dead_letter(
        self, ctx: AdapterContext, execution: OrchestrationExecution
    ) -> None:
        if execution.payload.get("kind") == "resume_episode":
            # A resume payload carries no ``evaluation_id``. Its
            # episode stays ``"resumed"``, the status ``execute_evaluation``'s
            # reclaim treats as "someone else owns this", so without this
            # repair the run is protected forever by a continuation nobody
            # drives.
            episode_id = execution.payload.get("episode_id")
            if episode_id:
                await _fail_abandoned_resume(
                    str(episode_id),
                    execution.payload.get("idempotency_key"),
                    execution.last_error
                    or "Resume abandoned by the orchestration engine "
                    "(deadline exceeded or attempts exhausted).",
                )
            return

        if execution.payload.get("kind") == "rescore_runs":
            run_ids = execution.payload.get("run_ids") or []
            if run_ids:
                await _fail_rescore_runs(
                    run_ids,
                    execution.id,
                    execution.last_error
                    or "Rescore abandoned by the orchestration engine "
                    "(deadline exceeded or attempts exhausted).",
                    _previous_statuses(execution, run_ids),
                )
                # Runs first, basis second: a run this hook just failed must
                # not still be in the basis the recompute reads.
                await _repair_shared_performance_basis(run_ids)
            return

        evaluation_id = execution.payload.get("evaluation_id")
        if evaluation_id:
            reason = (
                execution.last_error
                or "Evaluation abandoned by the orchestration engine "
                "(deadline exceeded or attempts exhausted)."
            )
            # Episodes too, not just runs, and in ONE transaction. A
            # persistent fault in ``execute_evaluation``'s pre-dispatch repair
            # exhausts the attempts and lands HERE, with the repair never run.
            # Failing the runs alone would leave each with a ``"running"``
            # episode nobody can move.
            await _fail_active_runs(
                str(evaluation_id), execution.id, reason, repair_episodes=True
            )
            await _recover_stranded_headlines(str(evaluation_id))

    async def periodic(self, ctx: AdapterContext) -> None:
        """The two janitors this adapter owns.

        Independent: a failure in one must not stop the other. The first
        expires callback work that overran its deadline, the second reaches
        runs no execution is going to continue; a callback provider
        outage must not keep stranded runs stranded.

        Both are bounded per tick and awaited INLINE in the reconciler cycle,
        so they share its latency budget against
        ``reconciler_unhealthy_restart_seconds``.
        """
        from app.services.evaluation_continuation import (
            sweep_stranded_runs,
            timeout_waiting_episodes,
        )

        for janitor in (timeout_waiting_episodes, sweep_stranded_runs):
            try:
                await janitor(ctx.db)
            except Exception as exc:
                # Rolled back rather than left open: both helpers may return
                # while still holding row locks, and the next one would inherit
                # a poisoned transaction.
                await ctx.db.rollback()
                logger.error(
                    "EvaluationAdapter.periodic: %s failed: %s",
                    janitor.__name__,
                    format_exception_message(exc),
                    exc_info=True,
                )
            else:
                # Both janitors return WITHOUT committing when they find
                # nothing, possibly still holding run row locks; the CALLER
                # must commit or roll back either way. Otherwise those
                # locks outlive the hook and block the live engine's
                # end-of-entry write, and the shared session would make one
                # janitor's commit the other's.
                if ctx.db.in_transaction():
                    await ctx.db.rollback()


async def _flush_langfuse(context: str) -> None:
    """Flush the Langfuse span buffer after an evaluation run (off the loop).

    ``flush_langfuse()`` (no-op when Langfuse is disabled, never raises) is a
    synchronous call that blocks on the OTEL exporter, so it runs on the
    Langfuse pool: a slow Langfuse host must never stall the event loop, and
    with it the reconciler and every concurrent run.
    """
    try:
        from app.services.langfuse_tracing import flush_langfuse, run_langfuse_blocking

        await run_langfuse_blocking(flush_langfuse)
    except Exception as exc:
        logger.warning(
            "Langfuse flush failed after %s: %s",
            context,
            format_exception_message(exc),
            exc_info=True,
        )


async def _recover_stranded_headlines(evaluation_id: str) -> None:
    """Compute headlines for any ``completed`` run left stranded (own session).

    A batch interrupted between a run's best-effort headline write and the
    batch-end recompute in ``execute_evaluation`` leaves a ``completed`` run
    with ``headline_details`` ``NULL`` and nothing that revisits it.

    ``headline_details IS NULL`` alone is not enough:
    ``_recompute_performance_headlines`` rescores EVERY completed run of the
    evaluation, so a sibling completing later writes this run's
    ``performance_*`` keys while its quality half was never computed. A missing
    ``quality_metric_keys`` marker catches that "partially stranded" shape. A
    recovered run always ends with both markers set (``quality_metric_keys`` at
    least an empty list), so it is never reselected.

    Best-effort, wrapped in a broad try/except: ``_fail_active_runs`` has
    already run, and a recovery failure must never mask or replace that
    outcome. Logged as a warning only, like ``_flush_langfuse``.
    """
    try:
        from app.core.database import async_session
        from app.models.evaluation import EvaluationRun
        from app.services.evaluation_engine import _compute_run_headlines

        eval_uuid = uuid.UUID(evaluation_id)
        async with async_session() as db:
            stranded = (
                await db.execute(
                    select(EvaluationRun).where(
                        EvaluationRun.evaluation_id == eval_uuid,
                        EvaluationRun.status == "completed",
                        or_(
                            EvaluationRun.headline_details.is_(None),
                            EvaluationRun.headline_details[
                                "quality_metric_keys"
                            ].astext.is_(None),
                        ),
                    )
                )
            ).scalars().all()
            if not stranded:
                return
            await _compute_run_headlines(
                db, eval_uuid, batch_run_ids=[str(r.id) for r in stranded]
            )
            # No commit here: ``_compute_run_headlines`` commits its own writes.
            logger.info(
                "Recovered headlines for %d stranded run(s) in evaluation %s",
                len(stranded),
                evaluation_id,
            )
    except Exception as exc:
        logger.warning(
            "Stranded-headline recovery failed for evaluation %s: %s",
            evaluation_id,
            format_exception_message(exc),
            exc_info=True,
        )


async def _log_skipped_runs(db, still_skipped, execution_id: uuid.UUID) -> None:
    """Report runs a cleanup declined to fail because ownership moved on.

    The skip is otherwise invisible: the run keeps the status a later
    attempt gave it. Naming both executions lets an operator confirm from the
    logs that a run reported as succeeded was not rescued from a bogus failure.

    *still_skipped* is a query the caller narrowed to
    ``owner_execution_id IS NOT NULL AND != execution_id``: an ownership
    takeover and nothing else. A NULL owner is never a takeover.

    Observability only, outside the atomic UPDATE, so best-effort in BOTH
    directions: ownership may move again before this read
    (over-report), or the owner may finish the row (under-report).
    ``RETURNING`` is authoritative for what was *failed*, not *skipped*. The
    cleanup's correctness rests on the UPDATE's own predicate.

    Swallows its own failures: it runs AFTER the cleanup committed,
    and both callers still have recovery work to do. A diagnostic read must
    not turn an applied cleanup into a failed one.
    """
    try:
        rows = (await db.execute(still_skipped)).all()
    except Exception as exc:
        logger.warning(
            "Could not report ownership skips for execution %s: %s",
            execution_id,
            format_exception_message(exc),
        )
        return
    for rid, owner in rows:
        logger.warning(
            "Abandoned execution %s skipped evaluation run %s: it is now owned "
            "by execution %s",
            execution_id,
            rid,
            owner,
        )


async def _fail_abandoned_resume(
    episode_id: str, idempotency_key: str | None, error: str
) -> None:
    """Terminalise the episode of a dead-lettered resume (own session).

    Own session for the same reason as the ``_fail_*`` helpers: ``on_dead_letter``
    runs from the reconciler, whose session may already be in an unusable state
    by the time the engine gives up.

    LETS EXCEPTIONS PROPAGATE. This is the only repair path for a
    dead-lettered resume, so swallowing a failure would leave the episode
    non-terminal for good. The reconciler retries ``on_dead_letter`` on
    transient classes and the repair is idempotent under its row lock, so a
    retry is safe and a permanent fault is visible. (``_log_skipped_runs``
    swallows: it is observability with real work after it.)
    """
    from app.core.database import async_session
    from app.services.evaluation_continuation import fail_abandoned_episode

    async with async_session() as db:
        repaired = await fail_abandoned_episode(
            db,
            episode_id=episode_id,
            idempotency_key=idempotency_key,
            error=error,
        )

    if repaired:
        logger.warning(
            "Terminalised episode %s whose resume execution was abandoned",
            episode_id,
        )


async def _fail_active_runs(
    evaluation_id: str,
    execution_id: uuid.UUID,
    error: str,
    *,
    statuses: tuple[str, ...] = ("pending", "running"),
    repair_episodes: bool = False,
) -> None:
    """Mark an evaluation's pending/running runs THIS execution owns as failed.

    ``completed_at IS NULL`` is what separates "this run's agent execution
    never finished" from "this run finished long ago and is only ``running``
    again because a rescore is in flight". Without it an unrelated
    evaluation-wide failure would fail a healthy rescore mid-flight AND stamp
    ``completed_at``/``duration_seconds`` — fields that describe the original
    agent execution, which a rescore never re-runs — over the real values.

    ``statuses`` narrows it by state: an expired AI hold fails only the
    runs that never started. A retry of the same execution can find runs
    ``running`` but parked on an external callback, and ``execution_id`` does
    not separate those (a retry IS the same execution); a lapsed licence must
    not discard work that is only waiting.

    ``execution_id`` narrows it to this execution's own runs: two
    launches of one evaluation can overlap, so a sweep keyed on status alone
    fails the OTHER launch's runs. Unowned rows are covered too -- see
    ``_fail_rescore_runs``.

    One conditional UPDATE, for the reason given in ``_fail_rescore_runs``, so
    ``duration_seconds`` is computed in SQL: ``started_at`` is only safely
    readable inside the statement that writes the row. Rounded to 2 decimals;
    a NULL ``started_at`` yields NULL, not zero.
    """
    from app.core.database import async_session
    from app.models.evaluation import EvaluationRun

    now = datetime.now(UTC)
    # ``now`` binds one Python instant into BOTH ``completed_at`` and the
    # duration expression, so the two can never disagree (``func.now()`` would
    # be a second clock). Both columns are timestamptz, so the arithmetic does
    # not depend on the session TimeZone.
    duration = func.round(
        cast(func.extract("epoch", now - EvaluationRun.started_at), Numeric), 2
    )
    eid = uuid.UUID(evaluation_id)
    active = (
        EvaluationRun.evaluation_id == eid,
        EvaluationRun.status.in_(list(statuses)),
        EvaluationRun.completed_at.is_(None),
    )
    async with async_session() as db:
        failed = (
            await db.execute(
                update(EvaluationRun)
                .where(*active, runs_this_execution_may_touch(execution_id, repeating_is_harmless=True))
                .values(
                    status="failed",
                    error=(error or "Evaluation failed")[:500],
                    completed_at=now,
                    duration_seconds=case(
                        (EvaluationRun.started_at.is_(None), None), else_=duration
                    ),
                )
                .returning(EvaluationRun.id)
            )
        ).scalars().all()
        if repair_episodes:
            # SAME TRANSACTION as the run failures. The execution is
            # already ``dead_letter`` when this hook runs, so it never fires
            # again: a worker dying between two commits would leave the run
            # failed and its episode stranded for good.
            from app.services.evaluation_continuation import (
                fail_orphaned_episodes,
            )

            # Reuses ``active``, so it carries the UPDATE's
            # ``completed_at IS NULL`` guard: a finished run ``"running"``
            # again for a rescore must not have its episodes finalized and
            # its original ``completed_at`` / ``duration_seconds``
            # overwritten.
            run_ids = (
                await db.execute(
                    select(EvaluationRun.id).where(
                        *active,
                        runs_this_execution_may_touch(
                            execution_id, repeating_is_harmless=True
                        ),
                    )
                )
            ).scalars().all()
            if run_ids or failed:
                repaired = await fail_orphaned_episodes(
                    db,
                    # Still active: re-validated under the episode lock,
                    # because another launch may have claimed one since.
                    owned_run_ids=list(run_ids),
                    # The rows the UPDATE just failed, in THIS transaction —
                    # it holds their locks, so no ownership check is needed,
                    # and none is possible since the reclaim covers unowned
                    # rows. They are already terminal, so not reconciled.
                    reclaimed_run_ids=list(failed),
                    execution_id=execution_id,
                    error=error,
                )
                if repaired:
                    logger.warning(
                        "Dead letter: terminalised orphaned episode(s) on %d "
                        "run(s) of evaluation %s abandoned by execution %s",
                        repaired,
                        evaluation_id,
                        execution_id,
                    )

        if failed or repair_episodes:
            await db.commit()
        # Whatever is STILL active and owned by someone else is exactly the
        # ownership-skip set — no pre-read to diff against, and NULL owners are
        # excluded by construction rather than filtered out afterwards.
        await _log_skipped_runs(
            db,
            select(EvaluationRun.id, EvaluationRun.owner_execution_id).where(
                *active,
                runs_owned_by_another(execution_id),
            ),
            execution_id,
        )


async def _repair_shared_performance_basis(run_ids: list[str]) -> None:
    """Re-attempt the shared Overall Performance pass a dead-letter skipped.

    ``execute_rescore_runs`` finalises every run BEFORE its coalesced performance
    pass, so when that pass raises past the retry budget the runs are already
    terminal and ``_fail_rescore_runs`` -- which only touches rows still
    ``"running"`` -- is a guaranteed no-op. Nothing else in this hook revisits the
    basis, so the batch keeps scores computed against the pre-rescore run set.

    NOT THE LAST LINE OF DEFENCE, so it may fail.
    ``_recompute_performance_headlines`` rescores every completed run in the
    evaluation from every scoring path, so the next completion, replay
    or rescore repairs the basis. This hook shortens the window; for an
    evaluation nobody touches again, it is the whole repair.

    Whatever broke the original pass is likely still broken. Best-effort and
    logged, like ``_recover_stranded_headlines``: it must never mask the
    outcome it follows.
    """
    from app.services.evaluation_engine import (
        _recompute_shared_performance_basis,
    )

    try:
        await _recompute_shared_performance_basis(run_ids)
        logger.info(
            "Repaired the shared performance basis for %d dead-lettered "
            "rescored run(s)",
            len(run_ids),
        )
    except Exception as exc:
        logger.warning(
            "Could not repair the shared performance basis for %d dead-lettered "
            "rescored run(s): %s. The next scoring pass in this evaluation will "
            "recompute it.",
            len(run_ids),
            format_exception_message(exc),
            # This repair swallows everything, so the traceback is the only
            # diagnosis of where a failure came from.
            exc_info=True,
        )


def _previous_statuses(
    execution: OrchestrationExecution, run_ids: list[str]
) -> dict[str, str]:
    """Each rescored run's status before the rescore."""
    from app.services.evaluation_engine import rescore_previous_status

    return {rid: rescore_previous_status(execution.payload, rid) for rid in run_ids}


async def _fail_rescore_runs(
    run_ids: list[str],
    execution_id: uuid.UUID,
    error: str,
    previous_statuses: dict[str, str],
) -> None:
    """Hand back still-``running`` rescored runs THIS execution owns, the rescore failed.

    Each run gets back its status from before the rescore (*previous_statuses*)
    and the failure goes to ``rescore_error``.

    Scoped to exactly the named ``run_ids`` (unlike ``_fail_active_runs``,
    which sweeps an entire evaluation) — a rescore batch is a narrow subset of
    an evaluation's runs, and a sibling run genuinely ``running`` from an
    unrelated trigger must not be touched. Deliberately does not stamp
    ``completed_at``/``duration_seconds``: those describe the run's original
    agent execution, which a rescore never re-runs.

    Also scoped to ``execution_id``: ``on_dead_letter`` can fire LONG
    after the work finished, so a ``running`` run may belong to a LATER
    rescore, and failing it would report a failure over correct scores. Rows
    with no ``owner_execution_id`` are still covered; nothing else would reach
    them.

    One conditional UPDATE, not SELECT-then-mutate: the ownership test is
    evaluated at write time under the row lock, so a takeover or cancellation
    landing in between is not overwritten.
    """
    from app.core.database import async_session
    from app.models.evaluation import EvaluationRun

    targets = [uuid.UUID(rid) for rid in run_ids]
    async with async_session() as db:
        failed = (
            await db.execute(
                update(EvaluationRun)
                .where(
                    EvaluationRun.id.in_(targets),
                    EvaluationRun.status == "running",
                    runs_this_execution_may_touch(execution_id, repeating_is_harmless=True),
                )
                .values(
                    status=case(
                        {uuid.UUID(rid): status for rid, status in previous_statuses.items()},
                        value=EvaluationRun.id,
                    ),
                    rescore_error=(error or "Rescore failed")[:500],
                )
                .returning(EvaluationRun.id)
            )
        ).scalars().all()
        if failed:
            await db.commit()
        await _log_skipped_runs(
            db,
            select(EvaluationRun.id, EvaluationRun.owner_execution_id).where(
                EvaluationRun.id.in_(targets),
                EvaluationRun.status == "running",
                runs_owned_by_another(execution_id),
            ),
            execution_id,
        )
