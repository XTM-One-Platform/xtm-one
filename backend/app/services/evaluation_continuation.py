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

"""Continuation of async evaluation episodes.

State-safe primitives for episode state and callback resume, plus the repairs
that close what a lost callback or a dead worker leaves open: callback
timeouts, orphaned episodes and the stranded-run sweep.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import ceil

from sqlalchemy import String, and_, any_, bindparam, false, func, or_, select, update
from sqlalchemy import cast as sa_cast
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.config import settings
from app.core.utils import format_exception_message
from app.models.evaluation import (
    Evaluation,
    EvaluationEpisode,
    EvaluationResult,
    EvaluationResultTrace,
    EvaluationRun,
)
from app.models.orchestration import (
    NON_TERMINAL_EXECUTION_STATES,
    TERMINAL_EXECUTION_STATES,
    OrchestrationExecution,
)
from app.services.evaluation.run_ownership import (
    run_owned_by_execution_join,
    runs_no_other_live_execution_may_hold,
    runs_owned_by_nobody,
    runs_this_execution_may_touch,
    runs_unowned_and_unclaimable,
    runs_with_an_owner,
)

logger = logging.getLogger(__name__)
TERMINAL_EPISODE_STATUSES = frozenset({"completed", "failed", "timed_out"})



class CallbackOutcome(StrEnum):
    """What a callback's arrival actually did.

    Separates two opposite facts: the work was already handled, or it was
    thrown away.
    """

    #: Stored, and a resume was queued.
    ACCEPTED = "accepted"
    #: This exact callback was already processed, or the episode finished on
    #: its own. Nothing to do, and nothing was lost.
    DUPLICATE = "duplicate"
    #: The episode was already closed by something that was not a callback --
    #: a repair, a callback-deadline timeout, or the entry's own failure -- so
    #: this payload is dropped. NOT a duplicate: nobody handled it.
    #:
    #: Named for what happens to the PAYLOAD, not for a cause: stored state
    #: cannot tell a repair from an ordinary entry failure, which write the
    #: same row.
    DISCARDED = "discarded"
    #: No episode carries this correlation id.
    NOT_FOUND = "not_found"


# A run that has reached one of these is DONE, and no later reconcile may
# rewrite it. ``cancelled`` is the one that matters: ``cancel_run``
# marks the run but leaves its episode non-terminal, and a callback for that
# episode is still accepted -- a reconcile must not flip the user's
# cancellation to ``failed``.
# ``timed_out`` is ABSENT: it is an EPISODE status only, and listing it would
# suggest this set is about episodes. Any NEW run status must be classified
# here.
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})


class ConversationContinuationService:
    """Episode lifecycle primitives shared by evaluation/chat flows."""

    @staticmethod
    async def start_episode(
        db: AsyncSession,
        *,
        run_id: uuid.UUID,
        entry_id: uuid.UUID,
        correlation_id: str | None = None,
    ) -> EvaluationEpisode:
        """Create or fetch the run/entry episode and mark it as running."""
        existing_result = await db.execute(
            select(EvaluationEpisode).where(
                EvaluationEpisode.run_id == run_id,
                EvaluationEpisode.entry_id == entry_id,
            )
        )
        existing = existing_result.scalar_one_or_none()
        if existing:
            if existing.status in {"pending", "resumed"}:
                existing.status = "running"
            return existing

        episode = EvaluationEpisode(
            run_id=run_id,
            entry_id=entry_id,
            correlation_id=correlation_id or f"eval:{run_id}:{entry_id}",
            status="running",
        )
        db.add(episode)
        await db.flush()
        return episode

    @staticmethod
    async def append_step(
        db: AsyncSession,
        *,
        episode: EvaluationEpisode,
        step: dict,
    ) -> None:
        """Append a normalized timeline step to an episode."""
        current_steps = list(episode.steps or [])
        current_steps.append(step)
        episode.steps = current_steps
        await db.flush()

    @staticmethod
    async def pause_for_callback(
        db: AsyncSession,
        *,
        episode: EvaluationEpisode,
        timeout_seconds: int,
    ) -> None:
        """Mark the episode as waiting for an external callback."""
        now = datetime.now(UTC)
        episode.status = "waiting_callback"
        episode.waiting_since = now
        episode.callback_deadline = now + timedelta(seconds=max(timeout_seconds, 1))
        await db.flush()

    @staticmethod
    async def resume_from_callback(
        db: AsyncSession,
        *,
        correlation_id: str,
        idempotency_key: str,
        payload: dict | None,
    ) -> tuple[EvaluationEpisode | None, CallbackOutcome]:
        """Resume an episode by correlation id.

        Returns ``(episode, outcome)``. A callback arriving after the episode
        finished (harmless) is told apart from one arriving at an episode
        closed with nothing applied, whose payload is dropped.
        """
        result = await db.execute(
            select(EvaluationEpisode)
            .where(EvaluationEpisode.correlation_id == correlation_id)
            .with_for_update()
            # Without ``populate_existing`` the lock buys nothing. The
            # endpoint has already loaded this row unlocked, so it sits in the
            # identity map, and SQLAlchemy would return its pre-lock values
            # (``waiting_callback``, no idempotency key) after waiting for a
            # repair holding the row -- accepting a callback into an episode
            # that is already closed. Same spelling as the orchestration lock
            # helpers (``orchestration_engine``) and ``ai_state``.
            .execution_options(populate_existing=True)
        )
        episode = result.scalar_one_or_none()
        if not episode:
            # An outcome, not ``False``: the annotation promises a
            # ``CallbackOutcome``.
            return None, CallbackOutcome.NOT_FOUND

        # This exact callback already landed, as the latest or an earlier one.
        # Genuinely a duplicate whatever the episode went on to do, because ITS
        # payload was applied; accepting a replay of an earlier one would put
        # its payload back over a later callback's.
        if idempotency_key in episode.accepted_idempotency_keys:
            return episode, CallbackOutcome.DUPLICATE

        if episode.status in TERMINAL_EPISODE_STATUSES:
            # Terminal, and this callback is not the one that got there.
            # ``completed``: the run has its answer, a late callback
            # loses nothing. Anything else: the episode closed without this
            # payload, so it is dropped -- calling that a duplicate would tell
            # the caller its work was handled when it was thrown away.
            if episode.status == "completed":
                return episode, CallbackOutcome.DUPLICATE

            # Not ``completed``: answers whether ANY callback was applied, not
            # what closed it. A callback error, an entry failure and
            # the repairs all write the same ``failed`` row; telling them apart
            # would need a persisted cause the caller does not branch on.
            #
            # ``resume_from_callback`` is the ONLY writer of
            # ``idempotency_key`` (the repairs read it as a generation token),
            # so its presence means some callback was accepted. An episode
            # resumed by one callback and then closed by a repair reads as
            # handled: that payload really was applied.
            if episode.idempotency_key:
                return episode, CallbackOutcome.DUPLICATE
            return episode, CallbackOutcome.DISCARDED

        # Reassigned, not appended: the JSONB list does not track in-place edits.
        episode.superseded_idempotency_keys = episode.accepted_idempotency_keys
        episode.idempotency_key = idempotency_key
        episode.callback_payload = payload
        episode.resumed_at = datetime.now(UTC)
        if episode.status in {"waiting_callback", "running", "pending"}:
            episode.status = "resumed"
        episode.waiting_since = None
        episode.callback_deadline = None
        await db.flush()
        return episode, CallbackOutcome.ACCEPTED

    @staticmethod
    async def finalize_episode(
        db: AsyncSession,
        *,
        episode: EvaluationEpisode,
        final_output: str | None,
        participants: list[str] | None = None,
        first_turn_output: str | None = None,
        error: str | None = None,
        timed_out: bool = False,
    ) -> None:
        """Mark episode terminal state and persist final outputs."""
        now = datetime.now(UTC)
        if first_turn_output is not None:
            episode.first_turn_output = first_turn_output
        if final_output is not None:
            episode.final_output = final_output
        if participants is not None:
            episode.participants = participants
        if timed_out:
            episode.status = "timed_out"
            if error:
                episode.error = error[:2000]
        elif error:
            episode.status = "failed"
            episode.error = error[:2000]
        else:
            episode.status = "completed"
            episode.error = None
        episode.completed_at = now
        await db.flush()


async def _write_canonical_headlines(db: AsyncSession, run: EvaluationRun) -> None:
    """Write this run's headlines through the ONE canonical writer.

    READ THIS BEFORE ADDING A FIELD: ``get_run`` re-reads the fields a resolving
    callback can change, to avoid handing a client stale headlines with
    ``callback_pending`` already false. That re-read enumerates them, so
    a new field written here and not added there goes stale silently on the one
    path where nothing later corrects it.

    The canonical writer applies the ``judge_on`` gate and writes ``None``, not
    ``0``, for a judge that never ran. One helper rather than the call
    repeated per site, so the argument set cannot diverge again.

    * ``require_completed=False`` -- a reconciled run may land ``failed``, and
      may already be terminal when a late result arrives.
    * ``commit=False`` -- the caller owns the transaction boundary;
      ``fail_orphaned_episodes`` folds this into the transaction that failed
      the runs, so a crash cannot apply one without the other.
    * ``skip_performance_recompute=True`` -- REQUIRED, and the reason a swept
      run carries no comparative ``performance_score``. That pass
      takes an evaluation-wide ``pg_advisory_xact_lock``; without a commit to
      release it, the lock would be pinned for the caller's whole transaction,
      so ``_compute_run_headlines`` refuses ``commit=False`` without this flag.
      Every repair path uses the same flag, so a swept run matches a
      reconciled one. The next batch or rescore recomputes every completed run.
      Recomputing in a second transaction after the terminal commit is not a
      drop-in fix: that transaction can fail on its own, after the run is
      already terminal.

    BEST-EFFORT, made true by the SAVEPOINT. This function's job is to
    get a run OUT of ``"running"``; a headline failure must not strand it,
    as on the ordinary completion path
    (``evaluation_engine.execute_evaluation_run``).

    Catching alone would not do it: a failed flush leaves the session needing a
    rollback, so the CALLER's commit raises ``PendingRollbackError`` and the
    episode repair that succeeded is lost. ``begin_nested`` confines the
    failure, and the status transition still lands. Same pattern as
    ``a2a/client/delegation.py`` and ``platform_settings_helpers``.

    Logged rather than silent: a headline that stopped updating is otherwise
    invisible until someone questions a score.
    """
    from app.services.evaluation_engine import _compute_run_headlines

    # Read BEFORE the savepoint: rolling one back EXPIRES every instance it
    # touched, so ``run.id`` in the handler below would lazy-reload on a
    # failed session and mask the original error.
    run_id, evaluation_id = run.id, run.evaluation_id
    try:
        async with db.begin_nested():
            await _compute_run_headlines(
                db,
                evaluation_id,
                batch_run_ids=[str(run_id)],
                require_completed=False,
                skip_performance_recompute=True,
                commit=False,
            )
    except Exception as headline_err:
        logger.warning(
            "Headline recompute failed for run %s; run still terminalised: %s",
            run_id,
            format_exception_message(headline_err),
        )
        # ``ROLLBACK TO SAVEPOINT`` EXPIRES ``run``. Without this refresh the
        # caller's next attribute read lazy-reloads outside async context and
        # dies with ``MissingGreenlet``, so the run is not terminalised after
        # all. Refreshing reloads it in async context, on a session the
        # savepoint rollback left usable.
        await db.refresh(run)


async def _reconcile_run_status(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    fallback_error: str | None = None,
) -> None:
    run_result = await db.execute(
        select(EvaluationRun)
        .where(EvaluationRun.id == run_id)
        .with_for_update()
        # Without ``populate_existing`` the lock buys nothing: a run this
        # session loaded in an earlier transaction (a resume's store and judge)
        # sits in the identity map, ``expire_on_commit`` being off, and the
        # locked read would return its old status and counts.
        .execution_options(populate_existing=True)
    )
    run = run_result.scalar_one_or_none()
    if not run:
        return

    completed_result = await db.execute(
        select(func.count(EvaluationEpisode.id)).where(
            EvaluationEpisode.run_id == run_id,
            EvaluationEpisode.status.in_(list(TERMINAL_EPISODE_STATUSES)),
        )
    )
    completed_entries = completed_result.scalar() or 0
    run.completed_entries = max(run.completed_entries, completed_entries)
    # Flushed HERE so the progress UPDATE is outside the savepoint
    # ``_write_canonical_headlines`` opens below. ``begin_nested()``
    # autoflushes first anyway; this does not depend on that. On the
    # terminal-guard path this is the LAST reconcile the run gets, so a lost
    # write would leave progress and score disagreeing on disk for good.
    await db.flush()

    if run.status in TERMINAL_RUN_STATUSES:
        # STATUS and ERROR are frozen past this point: a late callback must not
        # flip a user's cancellation to ``failed`` or replace their reason.
        #
        # HEADLINES and progress are NOT frozen: they describe what was
        # MEASURED, and a scored result can land after the run went terminal
        # (the orphan repair fails the run, then the parked
        # ``waiting_callback`` sibling resolves). Skipping the recompute would
        # leave the headline averaging a subset of the run's results while
        # ``completed_entries`` counts them all.
        #
        # ``cancelled`` is the exception: a partial average over whatever
        # happened to finish is noise, not a measurement.
        if run.status != "cancelled":
            await _write_canonical_headlines(db, run)
        await db.flush()
        return

    if run.total_entries and completed_entries < run.total_entries:
        if run.status == "pending":
            run.status = "running"
        await db.flush()
        return

    failures_result = await db.execute(
        select(func.count(EvaluationEpisode.id)).where(
            EvaluationEpisode.run_id == run_id,
            EvaluationEpisode.status.in_(["failed", "timed_out"]),
        )
    )
    failed_count = failures_result.scalar() or 0

    # The cost of ``skip_performance_recompute``: a callback-backed run gets
    # no ``performance_score`` here, nor from the batch pass (which requires
    # completed runs while this one is still ``"running"``). The next batch or
    # rescore on this evaluation picks it up; an evaluation that ONLY runs
    # callback-backed runs stays blank.
    await _write_canonical_headlines(db, run)

    if failed_count > 0:
        run.status = "failed"
        if fallback_error and not run.error:
            run.error = fallback_error[:500]
    else:
        run.status = "completed"
        run.error = None

    run.completed_at = datetime.now(UTC)
    if run.started_at and run.completed_at:
        run.duration_seconds = round((run.completed_at - run.started_at).total_seconds(), 2)

    await db.flush()


async def _store_resumed_answer(
    db: AsyncSession,
    *,
    episode: EvaluationEpisode,
    final_output: str,
) -> uuid.UUID:
    """Store a callback's final answer as the entry's result, unscored; returns its id.

    The result is built from what the first turn recorded on the episode: the
    dataset snapshot entry it ran on and its execution record (timeline, trace ids,
    tools, durations, call ledger and performance metrics). An episode paused before
    the record existed has none: its answer is judged on the live entry.

    A verdict that never came keeps the row out of the headlines: a new row
    starts excluded, an existing one keeps its verdict until ``_score_resumed_answer``
    replaces it. Committed before the judge runs, a new row reads as a judge
    failure for the judge call's duration, or until a retry scores it if the worker
    dies meanwhile.
    """
    from app.models.dataset_version import DatasetVersionEntry
    from app.services.evaluation.scorers.registry import judge_enabled
    from app.services.evaluation.settings import SUB_SCORE_WEIGHTS

    result = await db.execute(
        select(EvaluationResult).where(
            EvaluationResult.run_id == episode.run_id,
            EvaluationResult.entry_id == episode.entry_id,
        )
    )
    eval_result = result.scalar_one_or_none()

    run = (
        await db.execute(select(EvaluationRun).where(EvaluationRun.id == episode.run_id))
    ).scalar_one_or_none()
    # The run's own snapshot of the selection, as the first turn ran under it.
    judge_on = judge_enabled(run.scorer_selection) if run else True

    if eval_result is not None:
        eval_result.agent_output = final_output
        # Its retired token F1 described the replaced answer. Cleared here, with
        # the answer, so a scoring failure the savepoint below undoes cannot keep it.
        eval_result.f1_score = None
    else:
        # The first turn wrote no result: it parked the entry. Its record is the
        # result's execution record.
        first_turn = episode.first_turn_details or {}
        details = first_turn.get("execution_details")
        # The snapshot it ran on, unless deleted since: the callback can come hours
        # later, and the row's key must not fail the resume. Without it the answer is
        # judged on the live entry, as a rescore falls back.
        version_entry_id = None
        if first_turn.get("version_entry_id"):
            snapshot_id = uuid.UUID(first_turn["version_entry_id"])
            if await db.get(DatasetVersionEntry, snapshot_id) is not None:
                version_entry_id = snapshot_id
        eval_result = EvaluationResult(
            id=uuid.uuid4(),
            run_id=episode.run_id,
            entry_id=episode.entry_id,
            version_entry_id=version_entry_id,
            agent_output=final_output,
            # Until a verdict replaces it: a judge-off run's entry reads as a fresh
            # judge-off one; with the judge on, no verdict is not a measurement,
            # so the row starts out of the headlines.
            score=0,
            sub_scores={k: 0 for k in SUB_SCORE_WEIGHTS} if not judge_on else {},
            evaluator_reasoning=(
                "Resumed from external callback; no judge verdict recorded."
                if judge_on
                else "LLM judge scorer not selected for this evaluation."
            ),
            is_judge_failure=judge_on,
            duration_seconds=round((details or {}).get("agent_duration_ms", 0) / 1000, 2),
            execution_details=dict(details) if details else None,
        )
        db.add(eval_result)
        # The agent's traces, linked as the fresh pass links them.
        for order, trace_id in enumerate((details or {}).get("trace_ids") or [], start=1):
            try:
                parsed = uuid.UUID(str(trace_id))
            except ValueError:
                continue
            db.add(
                EvaluationResultTrace(
                    result_id=eval_result.id, trace_id=parsed, trace_role="agent", trace_order=order
                )
            )
    # Copied into the result: no second copy of the turn's tool arguments is kept. A
    # retry finds the result and updates it, so it never needs them again.
    episode.first_turn_details = None
    await db.flush()
    return eval_result.id


class _SupersededError(Exception):
    """A newer callback replaced the answer being scored: its verdict must not land."""


async def _callback_generation(db: AsyncSession, episode_id: uuid.UUID) -> tuple[str | None, str | None]:
    """The episode's status and the key of the callback it last accepted, as committed."""
    row = (
        await db.execute(
            select(EvaluationEpisode.status, EvaluationEpisode.idempotency_key).where(
                EvaluationEpisode.id == episode_id
            )
        )
    ).one_or_none()
    return (row.status, row.idempotency_key) if row else (None, None)


async def _score_resumed_answer(
    db: AsyncSession, *, episode_id: uuid.UUID, result_id: uuid.UUID, generation: str | None
) -> None:
    """Score a stored resumed answer as a fresh one, holding no run lock.

    ``score_stored_result`` scores it as a rescore would, but as an answer the agent
    produced: the judge on the run's snapshot of the selection, the package's
    quality, and the metric rows with Run Duration as agent plus judge. Its only writes
    that reference the run (the metric rows) come after the judge call, so no lock on
    the run is held while the judge runs.

    Skipped when the episode was closed before the answer is judged (a cancel, the
    emergency stop), so no judge call is billed then. A closure during the call still
    pays for it, and the finalize step then leaves the closed episode as it is. A step
    that raises keeps the row's previous verdict, or a new row's exclusion.

    *generation* is the key of the callback whose answer this scores. A newer callback
    accepted meanwhile replaces the answer and queues its own resume: this one is then
    skipped, or its verdict undone if the newer one landed during the judge call, so a
    verdict never lands on another answer than the one judged. Overlapping
    generations are otherwise last writer wins.
    """
    from app.services.evaluation.scorers.registry import judge_enabled
    from app.services.evaluation_engine import score_stored_result, stored_scoring_for_run

    if await _callback_generation(db, episode_id) != ("resumed", generation):
        return
    eval_result = await db.get(EvaluationResult, result_id)
    if eval_result is None:
        return
    run = await db.get(EvaluationRun, eval_result.run_id)
    if run is None:
        return
    try:
        # A step that raises, the database refusing a read included, leaves the
        # transaction usable through its savepoint.
        async with db.begin_nested():
            scoring = await stored_scoring_for_run(
                db, run, judge_selected=judge_enabled(run.scorer_selection)
            )
            await score_stored_result(db, scoring, eval_result, is_reused=False)
            if (await _callback_generation(db, episode_id))[1] != generation:
                raise _SupersededError
    except _SupersededError:
        # The savepoint undid this verdict; the newer callback's resume scores its own.
        return
    except Exception as exc:
        logger.warning(
            "Failed to score resumed output for episode %s: %s",
            episode_id,
            format_exception_message(exc),
        )
        # A newer callback's resume may own the row by now, its verdict included:
        # this failure is no longer the row's to report.
        if (await _callback_generation(db, episode_id))[1] != generation:
            return
        # The savepoint undid the scoring: the row keeps what it held before it,
        # an earlier verdict or a new row's exclusion.
        eval_result.evaluator_reasoning = "Resume callback received; judge scoring failed."
    await db.flush()


async def _locked_resumed_episode(
    db: AsyncSession, episode_id: uuid.UUID
) -> EvaluationEpisode | None:
    """The episode, locked, while still ``resumed``; the caller holds its run's lock."""
    episode = (
        await db.execute(
            select(EvaluationEpisode)
            .where(EvaluationEpisode.id == episode_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if episode is None or episode.status != "resumed":
        return None
    return episode


async def process_resume_episode(db: AsyncSession, *, episode_id: str) -> None:
    """Process one resumed evaluation episode and reconcile run state.

    LOCK ORDER: the run, then its episodes. Every transaction that touches both
    must acquire them in that direction, or it can deadlock against one that
    does. The abandoned-work repairs lock runs first by construction
    (an ``UPDATE`` on runs), so this path conforms. ``_reconcile_run_status``
    takes the run lock again in the last transaction and reads the run as
    committed.

    An answer is processed in three transactions, so the judge never runs under
    the run lock: the answer stored under the locks, judged with none,
    then the episode finalized under the locks again. The episode stays
    ``resumed`` until that last one, so a retry replays the whole flow and no
    other session ever sees it ``running``; one closed meanwhile is left as it is.
    """
    episode_uuid = uuid.UUID(episode_id)
    # Which run, without locking anything yet.
    run_id = (
        await db.execute(
            select(EvaluationEpisode.run_id).where(
                EvaluationEpisode.id == episode_uuid
            )
        )
    ).scalar_one_or_none()
    if run_id is None:
        return
    # Parent first.
    await db.execute(
        select(EvaluationRun.id).where(EvaluationRun.id == run_id).with_for_update()
    )
    episode = await _locked_resumed_episode(db, episode_uuid)
    if episode is None:
        return
    # The callback this execution processes; a newer one queues its own resume.
    generation = episode.idempotency_key

    callback = episode.callback_payload or {}
    final_output = callback.get("final_output")
    if not (isinstance(final_output, str) and final_output.strip()):
        final_output = callback.get("agent_output")
    callback_error = callback.get("error")
    # The answer is judged as the evaluation's owner: an owner who may no
    # longer run it closes the episode, as a callback error does.
    from app.services.evaluation.launch import owner_refusal

    evaluation = (
        await db.execute(
            select(Evaluation).join(EvaluationRun, EvaluationRun.evaluation_id == Evaluation.id).where(
                EvaluationRun.id == run_id
            )
        )
    ).scalar_one_or_none()
    refused = await owner_refusal(db, evaluation) if evaluation is not None else None
    if refused is not None:
        callback_error = refused

    if isinstance(callback_error, str) and callback_error.strip():
        episode.status = "running"
        await ConversationContinuationService.append_step(
            db,
            episode=episode,
            step={
                "type": "callback",
                "description": (
                    "The evaluation's owner may no longer run it"
                    if refused is not None
                    else "External callback reported an error"
                ),
                "duration_ms": 0,
            },
        )
        await ConversationContinuationService.finalize_episode(
            db,
            episode=episode,
            first_turn_output=episode.first_turn_output,
            final_output=episode.final_output,
            error=callback_error,
        )
        await _reconcile_run_status(
            db,
            run_id=episode.run_id,
            fallback_error=callback_error,
        )
        await db.commit()
        return

    if isinstance(final_output, str) and final_output.strip():
        result_id = await _store_resumed_answer(db, episode=episode, final_output=final_output)
        await db.commit()
        await _score_resumed_answer(
            db, episode_id=episode_uuid, result_id=result_id, generation=generation
        )
        await db.commit()
        # Parent first, again.
        await db.execute(
            select(EvaluationRun.id).where(EvaluationRun.id == run_id).with_for_update()
        )
        episode = await _locked_resumed_episode(db, episode_uuid)
        if episode is None or episode.idempotency_key != generation:
            # Closed while the answer was judged (a cancel decided its state), or a
            # newer callback replaced it, whose own resume finalizes the episode.
            await db.commit()
            return
        episode.status = "running"
        await ConversationContinuationService.append_step(
            db,
            episode=episode,
            step={
                "type": "callback",
                "description": "External callback resume payload applied",
                "duration_ms": 0,
            },
        )
        await ConversationContinuationService.finalize_episode(
            db,
            episode=episode,
            first_turn_output=episode.first_turn_output,
            final_output=final_output,
        )
        await _reconcile_run_status(db, run_id=episode.run_id)
    else:
        # Keep the resumed state when callback payload doesn't yet include output.
        await ConversationContinuationService.append_step(
            db,
            episode=episode,
            step={
                "type": "callback",
                "description": "External callback payload stored; awaiting continuation",
                "duration_ms": 0,
            },
        )

    await db.commit()


async def fail_orphaned_episodes(
    db: AsyncSession,
    *,
    owned_run_ids: list[uuid.UUID],
    reclaimed_run_ids: list[uuid.UUID],
    execution_id: uuid.UUID,
    error: str,
) -> int:
    """Terminalise episodes left ``"running"`` by an execution that is gone.

    ``execute_evaluation``'s reclaim repairs the RUN but skips a run
    holding a ``waiting_callback``/``resumed`` episode, so outstanding callback
    work survives. The sibling episode the worker died on (the entry loop
    ``continue``s past a paused entry, so one run can hold both) then stays
    ``"running"``: ``completed_entries`` never reaches ``total_entries`` and
    the run is stranded. Repairing the EPISODE lets both coexist: the
    orphan goes terminal, the parked sibling is untouched, and reconciliation
    finishes the run when the callback resolves.

    Safe before dispatch for runs this execution owns: no episode of the
    current attempt exists yet, so a ``"running"`` episode belongs to a gone
    attempt. The row lock makes it a write-time decision: an episode being
    finalized by ``process_resume_episode`` (which flips and finalizes it under
    its row lock, in one transaction) is re-read as terminal and skipped.

    An unowned, non-pending run (a legacy row) is repaired only while no live
    ``evaluation`` execution other than *execution_id* names its evaluation:
    a replica on the previous release may still be running it.

    Two run sets, safe for different reasons:

    * *owned_run_ids* are re-validated HERE, under the lock. A run the caller
      saw as unowned may have been claimed and dispatched in the gap, and
      finalizing its live episode is destructive and not idempotent.
    * *reclaimed_run_ids* were failed by the caller **in this same
      transaction**, whose ``UPDATE`` still holds their row locks. They need no
      ownership check, and must not get one: the reclaim covers rows with no
      owner, and requiring ``owner == execution_id`` would skip their episodes
      forever.

    Run locks are taken FIRST, as their own statement ordered by id; the
    episode select then locks only episodes. Locking the runs stops a re-score
    taking a just-failed run in between; a separate statement, not one
    both-tables ``with_for_update()``, fixes the lock ORDER instead of leaving
    it to the query plan.

    Does NOT commit — the caller folds this into the transaction that failed
    the runs, so a crash cannot leave one applied without the other.
    """
    # *reclaimed_run_ids* skip the ownership check, safe ONLY while the
    # caller's reclaim ``UPDATE`` holds their row locks in this transaction;
    # passed from a fresh session they would finalize live episodes.
    #
    # This catches only a caller with NO transaction open: SQLAlchemy
    # autobegins, so any earlier statement makes it ``True``. Defence in depth
    # against the crudest misuse, not a proof; the docstring is the contract.
    if reclaimed_run_ids and not db.in_transaction():
        raise RuntimeError(
            "fail_orphaned_episodes: reclaimed_run_ids is only safe inside the "
            "transaction that failed those runs, but this session has none open"
        )

    run_ids = [*owned_run_ids, *reclaimed_run_ids]
    if not run_ids:
        return 0

    # Take the RUN locks first, in id order, before touching any episode:
    # run-then-episode, the order the repair and resume paths follow (see
    # ``process_resume_episode``); id order keeps two concurrent repairs from
    # deadlocking. The live execution path inverts it -- the ``skip_locked``
    # note on the episode select below is what makes that safe.
    await db.execute(
        select(EvaluationRun.id)
        .where(EvaluationRun.id.in_(run_ids))
        .order_by(EvaluationRun.id)
        .with_for_update()
    )

    episodes = (
        (
            await db.execute(
                select(EvaluationEpisode)
                .join(EvaluationRun, EvaluationRun.id == EvaluationEpisode.run_id)
                .where(
                    EvaluationEpisode.run_id.in_(run_ids),
                    EvaluationEpisode.status == "running",
                    or_(
                        # Strict, because finalizing an episode destroys live
                        # work. The two arms below are this call site's own:
                        # unowned rows are admitted only when they cannot be
                        # claimed, and rows already locked here need no check.
                        runs_this_execution_may_touch(
                            execution_id, repeating_is_harmless=False
                        ),
                        # Legacy rows have no owner and never will; strict
                        # ownership would strand them, and an unowned run that
                        # is still claimable may hold live work. Named in the
                        # ownership module so the structural guard sees it.
                        #
                        # An unowned, non-pending run was started by the
                        # release before ownership tracking, and a live
                        # execution of that release may still be writing it
                        # during a rolling deploy: the veto the stranded-run
                        # sweep applies holds here too. The
                        # caller's own execution is excluded, so it never
                        # vetoes its own repair.
                        and_(
                            runs_unowned_and_unclaimable(),
                            runs_no_other_live_execution_may_hold(execution_id),
                        ),
                        EvaluationRun.id.in_(reclaimed_run_ids)
                        if reclaimed_run_ids
                        else false(),
                    ),
                )
                # ``of=`` + ``skip_locked``: lock the EPISODE rows only, and
                # never wait for one another transaction already holds.
                #
                # The live path inverts the run-then-episode order:
                # ``execute_evaluation_run`` holds an episode row while reaching
                # for the run this caller already locked, an ABBA during a
                # stalled-attempt overlap. Yielding is the janitor's
                # job: taking the run lock in the live loop would hold it across
                # every entry's scoring. A locked episode is one a live
                # transaction is still working on, so not abandoned; a real
                # orphan is picked up on the next reclaim cycle.
                .with_for_update(of=EvaluationEpisode, skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    if not episodes:
        return 0

    affected: set[uuid.UUID] = set()
    for episode in episodes:
        # Not dead code despite the SELECT filter: the identity map returns a
        # row this session already finalized with its IN-MEMORY terminal
        # status while the database still reads ``"running"``.
        if episode.status in TERMINAL_EPISODE_STATUSES:
            continue
        await ConversationContinuationService.finalize_episode(
            db,
            episode=episode,
            first_turn_output=episode.first_turn_output,
            final_output=episode.final_output,
            error=error,
        )
        affected.add(episode.run_id)

    # Reconcile the NON-reclaimed runs only: reclaimed runs are already
    # terminal.
    #
    # The rest DO need it: if the parked sibling completed before this repair,
    # its reconcile saw the orphan still ``"running"`` and returned early, and
    # nothing would look again -- every episode terminal, the run stuck
    # ``"running"`` for good.
    reclaimed = set(reclaimed_run_ids)
    for run_id in affected - reclaimed:
        await _reconcile_run_status(db, run_id=run_id, fallback_error=error)
    await db.flush()
    return len(affected)


async def fail_abandoned_episode(
    db: AsyncSession, *, episode_id: str, idempotency_key: str | None, error: str
) -> bool:
    """Terminalise one episode whose resume execution was abandoned.

    Called from ``EvaluationAdapter.on_dead_letter`` for a ``resume_episode``
    payload. A resume worker that died for good leaves the episode
    ``"resumed"``, which the reclaim's parked-run exclusion protects, so the run
    would stay ``"running"`` with nothing left to advance it.

    *idempotency_key* is the generation this repair belongs to, and is
    required: without it the execution cannot be told apart from a newer
    generation, so the repair is refused rather than guessed. A second
    non-duplicate callback is legal while an episode is still ``"resumed"``
    (see ``resume_from_callback``) and gets its own resume execution, so
    "non-terminal" does not mean "abandoned": a NEWER callback may own the
    row. Finalizing it would destroy a callback the caller was told had been
    accepted.

    Returns whether anything was changed, so the caller can log a no-op
    distinctly from a repair.
    """
    # Run first, then the episode — the lock order every transaction touching
    # both must follow (see ``process_resume_episode``). This path reconciles
    # the run at the end, so it needs that lock anyway.
    episode_uuid = uuid.UUID(episode_id)
    run_id = (
        await db.execute(
            select(EvaluationEpisode.run_id).where(
                EvaluationEpisode.id == episode_uuid
            )
        )
    ).scalar_one_or_none()
    if run_id is None:
        return False
    await db.execute(
        select(EvaluationRun.id).where(EvaluationRun.id == run_id).with_for_update()
    )
    episode = (
        await db.execute(
            select(EvaluationEpisode)
            .where(EvaluationEpisode.id == episode_uuid)
            .with_for_update()
        )
    ).scalar_one_or_none()
    # Both checks are under the lock, and both are about ownership rather than
    # timing: terminal means the resume landed between the engine giving up and
    # this repair; a moved key means a later generation owns the row.
    if episode is None or episode.status in TERMINAL_EPISODE_STATUSES:
        return False
    if not idempotency_key:
        # Falsy, not just None: a blank key is what
        # ``resume_from_callback`` treats as ABSENT, so it can never identify a
        # generation. The schema rejects blank now, but executions persisted
        # earlier may still carry one. Fails closed: a newer callback may own
        # the row, and repairing would destroy work the caller was told had
        # been accepted. The cost: those pre-token executions stay stranded,
        # a set bounded by one deploy.
        logger.warning(
            "Skipping abandoned-resume repair for episode %s: the execution "
            "carries no generation token, so a newer callback cannot be ruled "
            "out",
            episode_id,
        )
        return False
    if episode.idempotency_key != idempotency_key:
        logger.info(
            "Skipping abandoned-resume repair for episode %s: a newer callback "
            "owns it now",
            episode_id,
        )
        return False

    await ConversationContinuationService.finalize_episode(
        db,
        episode=episode,
        first_turn_output=episode.first_turn_output,
        final_output=episode.final_output,
        error=error,
    )
    await _reconcile_run_status(db, run_id=episode.run_id, fallback_error=error)
    await db.commit()
    return True


#: The error a swept run carries, decided once so an operator can tell it apart
#: from a run that failed on its content. Distinct from the reclaim's "Abandoned
#: by a worker that did not survive its attempt": that one means a worker died
#: mid-attempt, this one means no attempt remained at all.
STRANDED_RUN_ERROR = (
    "Stranded: the execution that owned this run finished without completing it "
    "— re-run to retry"
)

#: The error a swept run with NO owner carries: the release before
#: ``owner_execution_id`` started it, and the upgrade stopped the execution
#: running it.
UPGRADE_INTERRUPTED_RUN_ERROR = (
    "Interrupted by an upgrade: no execution was left to continue this run "
    "— re-run to retry"
)

def stranded_run_grace_floor_seconds() -> int:
    """The shortest grace period that cannot race a legitimate retry.

    DERIVED from the retry machinery, not a constant, so raising
    ``orchestration_default_max_attempts`` raises it: at seven attempts
    the real bound passes 1200s, and a fixed floor would let the sweep close
    runs the engine was still going to retry.

    Worst case a retry can still be working through: the backoff ladder at its
    jitter ceiling, plus one lease expiry per attempt.
    """
    # Imported here rather than at module scope: the engine imports from this
    # module, and the file already takes its other engine dependency lazily.
    from app.services.orchestration_engine import (
        BASE_BACKOFF_SECONDS,
        MAX_BACKOFF_SECONDS,
    )

    attempts = max(1, settings.orchestration_default_max_attempts)
    backoff = sum(
        min(BASE_BACKOFF_SECONDS * 2 ** (attempt - 1), MAX_BACKOFF_SECONDS)
        for attempt in range(1, attempts + 1)
    )
    # 1.25 is the jitter CEILING in ``_backoff_seconds``; the mean would leave
    # a quarter of the real cases above the floor.
    return ceil(backoff * 1.25 + settings.orchestration_lease_seconds * attempts)

#: Episode states this sweep understands as ACTIVE: work that no longer has
#: anyone to do it, so the sweep may finalise it. Paired with
#: ``PARKED_EPISODE_STATUSES`` below; an episode in neither defers its run
#: rather than being guessed at.
_ACTIVE_EPISODE_STATUSES = ("running", "pending")

#: Episode states that mean live callback work, which this sweep must never touch.
#: ``timeout_waiting_episodes`` owns their expiry.
PARKED_EPISODE_STATUSES = ("waiting_callback", "resumed")


#: What a user's cancellation writes on the entries it stops. Distinct from the
#: stranded and abandoned wordings so an operator can tell the three apart in the
#: same column: nobody died here, somebody pressed a button.
CANCELLED_EPISODE_ERROR = "Cancelled by user"


async def close_episodes_for_cancelled_run(
    db: AsyncSession, *, run_id: uuid.UUID, error: str = CANCELLED_EPISODE_ERROR
) -> int:
    """Terminalise every non-terminal episode of a run the user cancelled.

    One run's ``close_episodes_for_cancelled_runs``; see there.
    """
    return await close_episodes_for_cancelled_runs(db, run_ids=[run_id], error=error)


async def close_episodes_for_cancelled_runs(
    db: AsyncSession,
    *,
    run_ids: list[uuid.UUID],
    error: str = CANCELLED_EPISODE_ERROR,
) -> int:
    """Terminalise every non-terminal episode of runs that were cancelled.

    Cancel means stop. An entry left open on a cancelled run would:

    * still ACCEPT a late callback (``resume_from_callback`` takes any
      non-terminal episode), and ``_score_resumed_answer`` would bill a judge
      call on a cancelled run;
    * keep ``completed_entries`` climbing, since a terminal run's progress is
      not frozen, so progress and the frozen score would disagree;
    * strand a ``running`` episode for good: the sweep only reaches runs
      still ``"running"`` with no ``completed_at``.

    Closed as ``"failed"`` rather than a new ``"cancelled"`` episode status:
    ``TERMINAL_EPISODE_STATUSES`` is read by the sweep, the timeout janitor and
    the callback endpoint, and the error text already says why. Same choice
    as ``fail_abandoned_episode`` and the stranded sweep. A later callback then
    finds a terminal episode and gets DISCARDED with a 410.

    The same statements whatever the number of runs, a run lock and an
    episode update besides the flushes: the emergency stop closes every
    stopped run's episodes inside the one operation that must finish. The ids
    go as ONE array parameter, so no number of runs reaches the driver's
    limit on query parameters.

    Both locks are plain ``FOR UPDATE``. Neither ``process_resume_episode`` nor
    the main entry loop holds the run lock across a model call, so a
    cancel waits only for their short database writes.

    No ``skip_locked``, unlike the sweep: there a skipped run is picked up on
    the next tick, here a skipped episode stays OPEN on a cancelled run that
    nothing revisits. Cancelling the execution first would not shorten the
    wait: ``cancel_event`` is not checked inside the scoring call.

    ``error`` is what the closed episodes say: the user's cancel by default,
    the emergency stop's own message when it is the caller.

    Returns the number closed. Flushes; the caller owns the transaction, so the
    runs' own status change and this land together or not at all.
    """
    if not run_ids:
        return 0
    now = datetime.now(UTC)
    ids = bindparam("run_ids", list(run_ids), type_=ARRAY(EvaluationRun.id.type))
    # LOCK ORDER: runs, then episodes, each level in id order, so two
    # overlapping cancels take the same sequence. Taken HERE rather than left
    # to the caller: ``cancel_run`` reads its run without a lock.
    # ``test_every_episode_write_path_locks_its_run_first`` pins it.
    #
    # Under ``cancel_run`` and the emergency stop this sits beneath execution
    # locks: the global order is execution -> run -> episode, set by the
    # worker's reclaim (``_fence_on_lease``), and run-then-episode is its tail.
    await db.execute(
        select(EvaluationRun.id)
        .where(EvaluationRun.id == any_(ids))
        .order_by(EvaluationRun.id)
        .with_for_update()
    )
    # The episodes are locked by the sub-select, in id order, before the
    # UPDATE writes them; an UPDATE alone would lock them in whatever order
    # its scan meets them. A row a callback finishes meanwhile is re-checked
    # once its lock is granted and drops out, so a terminal episode is never
    # rewritten.
    open_episodes = (
        select(EvaluationEpisode.id)
        .where(
            EvaluationEpisode.run_id == any_(ids),
            EvaluationEpisode.status.notin_(list(TERMINAL_EPISODE_STATUSES)),
        )
        .order_by(EvaluationEpisode.id)
        .with_for_update()
    )
    closed = await db.execute(
        update(EvaluationEpisode)
        .where(EvaluationEpisode.id.in_(open_episodes))
        .values(
            status="failed",
            error=error,
            completed_at=now,
            # Cleared so the timeout janitor, which selects on a passed
            # deadline, cannot pick the row up afterwards.
            waiting_since=None,
            callback_deadline=None,
        )
        # Episodes the session already holds (the entry loop's) take the
        # written values instead of keeping stale ones.
        .execution_options(synchronize_session="fetch")
    )
    await db.flush()
    return closed.rowcount or 0


async def is_callback_pending_for_run(db: AsyncSession, run_id: str) -> bool:
    """True while a late callback can still rewrite this run's headlines.

    The run-detail poll stops as soon as ``status`` leaves
    ``"running"``/``"pending"``, but a callback-backed run keeps changing after
    that: when the outside system answers, ``_reconcile_run_status`` recomputes
    the headlines. This flag lets the client keep polling, as
    ``rescore_pending`` does for rescores; that flag cannot be reused,
    since a callback can land hours later.

    TWO ARMS, each bounded by something that always ends:

    * PARKED (``waiting_callback``) -- a callback can still be accepted, and
      ``timeout_waiting_episodes`` terminalises the episode once its deadline
      passes. The deadline must EXIST, because that sweep will not touch a row
      without one; it is NOT compared to now: ``resume_from_callback`` never
      reads the deadline, so a callback is still taken between expiry and the
      sweep. Do not add ``> now()``.
    * ACCEPTED (``resumed``) -- a resume execution is in flight and will rewrite
      the headlines when it lands. The engine ends it by completing,
      dead-lettering or blowing its deadline.

    KNOWN LIMITATION. A callback carrying neither output nor error
    leaves the episode ``resumed`` with no deadline and its execution
    COMPLETED. A later callback may still rewrite the run, but neither arm
    holds, so an open page misses it. No bounded signal exists for that state:
    covering it means polling forever (rejected), closing it breaks
    the continuation flow. The page self-corrects on reload, and on window
    focus once the query is stale (``staleTime`` 30s, not overridden by
    ``useEvaluationRun``).

    Only called for a run whose status already reads terminal (see the call
    site in ``get_run``); a run still ``"running"``/``"pending"`` is already
    covered by the frontend's status-based poll and does not need the query.
    """
    result = await db.execute(
        select(EvaluationEpisode.id)
        .where(
            EvaluationEpisode.run_id == uuid.UUID(str(run_id)),
            or_(
                # Accepted, not yet processed. The deadline is no use here:
                # ``resume_from_callback`` clears it on acceptance, before the
                # queued ``resume_episode`` execution rewrites the headlines.
                # The status alone is unbounded (a callback with
                # neither output nor error leaves the episode ``resumed``
                # forever), so the bound is the EXECUTION ITSELF: a
                # non-terminal one IS "the resume is still coming", and the
                # engine ends it by completing, dead-lettering or blowing its
                # deadline. Read from the execution, not recomputed from
                # timeout + grace: that formula belongs to the callback
                # endpoint, and a second copy would drift.
                #
                # No dedicated index, unlike ``is_rescore_pending_for_run``,
                # whose GIN containment over a JSONB list needs one.
                # This is scalar equality after ``state IN (...)``, served by
                # ``ix_orchestration_executions_state`` (measured at 30k
                # executions). Measure before adding an index.
                and_(
                    EvaluationEpisode.status == "resumed",
                    select(OrchestrationExecution.id)
                    .where(
                        OrchestrationExecution.adapter == "evaluation",
                        # Matched to the index's ``postgresql_where`` rather
                        # than an equivalent ``not_in(TERMINAL)``: the planner
                        # cannot prove the latter satisfies a partial index
                        # defined with ``state IN (...)``.
                        OrchestrationExecution.state.in_(
                            NON_TERMINAL_EXECUTION_STATES
                        ),
                        # ``.op("->>")`` and NOT the ``["episode_id"]``
                        # subscript: SQLAlchemy 2.0 renders the subscript as
                        # PostgreSQL's native jsonb subscripting, a different
                        # parse-tree node from the ``->>`` the index expression
                        # uses, so the index would never be chosen.
                        OrchestrationExecution.payload.op("->>")("episode_id")
                        == sa_cast(EvaluationEpisode.id, String),
                    )
                    .exists(),
                ),
                # Parked. NOT "and the deadline has not passed":
                # ``resume_from_callback`` never looks at the deadline, so a
                # callback is still accepted between expiry and the sweep.
                # Keying on the status mirrors the acceptance rule: pending
                # exactly while a callback can still be taken, bounded by
                # ``timeout_waiting_episodes``. Rejecting expired callbacks
                # instead would discard work the product accepts.
                #
                # The deadline must still EXIST: the sweep that bounds this arm
                # skips an episode without one. ``pause_for_callback`` always
                # sets it; if it ever did not, not pending is the fail-safe
                # answer for a row nothing can close.
                and_(
                    EvaluationEpisode.status == "waiting_callback",
                    EvaluationEpisode.callback_deadline.isnot(None),
                ),
            ),
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


def _no_execution_left_to_continue(cutoff: datetime) -> ColumnElement[bool]:
    """A run nothing is going to continue, owned or not. Needs the OUTER join to its owner.

    * OWNED: its execution has been terminal since before *cutoff*.
    * UNOWNED: the release before ``owner_execution_id`` started it, and
      an upgrade stopped the execution running it. It is abandoned once no live
      execution names its evaluation (``runs_no_other_live_execution_may_hold``).
      While one does, the run is left alone: an old replica may still be
      running it during a rolling deploy, or a trigger path failed to stamp the
      run and its worker still holds it.

    The unowned clock is the run's own start, because no execution names the
    run. Long evaluations are protected by the live-execution veto, not by the
    grace, which only covers a run committed before its execution. It does NOT
    cover a straggler write from a stopped replica (``main``'s adapter has no
    lease fencing): the upgrade stops those workers, and a stalled one that
    completes the run afterwards still wins, its completion write being
    unconditional, as for the reclaim's stalled attempt. Waiting for every
    execution of the evaluation to have been terminal for the grace instead
    (``updated_at >= cutoff``) has no index to serve it: measured at 30k
    executions, it turns the scan into a full read of the table, 7.8 ms a tick
    and growing with it.

    Measured at 30k executions and 200 runs in flight: 0.3 ms. The subquery is
    planned once as a hashed SubPlan over ``ix_orchestration_executions_state``
    (the in-flight executions only), and the owner join stays a primary-key
    lookup per run.
    """
    return or_(
        and_(
            runs_with_an_owner(),
            OrchestrationExecution.state.in_(TERMINAL_EXECUTION_STATES),
            OrchestrationExecution.updated_at < cutoff,
        ),
        and_(
            runs_owned_by_nobody(),
            EvaluationRun.started_at < cutoff,
            runs_no_other_live_execution_may_hold(),
        ),
    )


async def sweep_stranded_runs(db: AsyncSession, *, limit: int | None = None) -> int:
    """Terminalise runs left ``"running"`` that no execution is going to continue.

    The safety net underneath the inline repairs, which all work on an episode
    ROW. Two shapes leave no such row to act on:

    * the episode never materialised -- ``execute_evaluation_run`` COMMITS the
      run as ``"running"`` while ``start_episode`` only FLUSHES the episode, so a
      worker dying in that window leaves no trace of the entry it died on;
    * ``fail_orphaned_episodes`` SKIPPED the row (``skip_locked``, against an
      ABBA deadlock with the live path), and the holder then died.

    Both leave a run ``"running"`` with ``completed_entries < total_entries``.
    Working from the RUN reaches the first shape and subsumes the second.

    THE CLOCK IS THE EXECUTION, NEVER THE RUN. ``EvaluationRun`` has no
    ``updated_at``, and measuring from ``started_at`` would sweep legitimately
    long evaluations. Staleness is how long the owning execution has been
    TERMINAL, so the grace period only guards the window in which a straggler
    write from a dead worker might still land.

    A run with NO owner, which only an upgrade leaves, has no such
    clock: it is swept once no live execution names its evaluation, and fails
    with ``UPGRADE_INTERRUPTED_RUN_ERROR`` (``_no_execution_left_to_continue``).

    COMMIT CONTRACT: commits on the path where it terminalises something,
    returns WITHOUT committing when it finds nothing. Same as
    ``timeout_waiting_episodes``, the OPPOSITE of ``fail_orphaned_episodes``.

    FOUR OUTCOMES PER RUN:

    * every episode active -> the run and its episodes go ``"failed"``, and the
      run gets ``completed_at``, ``duration_seconds`` and a recomputed
      ``completed_entries`` so its numbers match a run finished any other way;
    * an episode is PARKED -> the run is skipped; the callback still owns it;
    * an episode is HELD by another transaction -> deferred to the next pass,
      because a live writer is evidence against abandonment;
    * an episode is in a status classified as NEITHER -> deferred, with a
      warning naming it. The only unbounded outcome of the four.

    LOCK ORDER: run, then episode, pinned by
    ``test_every_episode_write_path_locks_its_run_first``.
    """
    now = datetime.now(UTC)
    if limit is None:
        limit = settings.orchestration_claim_batch
    # Clamped at the USE SITE, like ``stale_assignment_run_seconds``
    # (``autonomous.py``), rather than with a settings validator. Zero
    # or negative would sweep a just-terminal execution's run with no grace.
    # The floor is the worst-case retry window, so no setting can race a
    # legitimate retry; a lower value is a code change.
    cutoff = now - timedelta(
        seconds=max(
            stranded_run_grace_floor_seconds(),
            settings.evaluation_stranded_run_grace_seconds,
        )
    )

    # Candidates first, locking NOTHING.
    #
    # OUTER join: an unowned run has no owner row to join. An owned run whose
    # execution row is gone matches neither arm, since without the row there is
    # no clock and no evidence of abandonment.
    candidates = (
        await db.execute(
            select(EvaluationRun.id)
            .outerjoin(OrchestrationExecution, run_owned_by_execution_join())
            .where(
                EvaluationRun.status == "running",
                # A re-score flips an ALREADY FINISHED run back to "running":
                # that case is not this sweep's.
                EvaluationRun.completed_at.is_(None),
                _no_execution_left_to_continue(cutoff),
                # The parked veto belongs HERE, before the limit. A run
                # parked on a callback matches every clause above (the callback
                # endpoint never restamps ``owner_execution_id``), so it is a
                # permanent candidate. Filtered only after the limit, enough of
                # them would fill every pass and a stranded run sorting after
                # them would never be selected.
                ~select(EvaluationEpisode.id)
                .where(
                    EvaluationEpisode.run_id == EvaluationRun.id,
                    EvaluationEpisode.status.in_(PARKED_EPISODE_STATUSES),
                )
                .exists(),
            )
            .order_by(EvaluationRun.id)
            .limit(limit)
        )
    ).scalars().all()
    if not candidates:
        return 0

    # Lock the runs, then RE-APPLY the whole predicate. A candidate that stopped
    # qualifying in the gap -- its execution requeued, a re-score started, a
    # callback landed -- is dropped rather than acted on stale.
    locked = (
        await db.execute(
            select(EvaluationRun)
            .outerjoin(OrchestrationExecution, run_owned_by_execution_join())
            .where(
                EvaluationRun.id.in_(candidates),
                EvaluationRun.status == "running",
                EvaluationRun.completed_at.is_(None),
                _no_execution_left_to_continue(cutoff),
            )
            .order_by(EvaluationRun.id)
            # ``skip_locked`` here too: a replica must never WAIT on
            # another with an overlapping candidate set. A row another replica
            # holds is one it is already sweeping. The concurrency test cannot
            # catch its removal: serialising also sweeps each run once.
            .with_for_update(of=EvaluationRun, skip_locked=True)
        )
    ).scalars().all()
    if not locked:
        return 0

    # The same veto again, unlocked: a cheap early exit for a run whose
    # callback landed since the candidate query. NOT the decision, which is
    # taken under the episode lock below.
    parked = set(
        (
            await db.execute(
                select(EvaluationEpisode.run_id).where(
                    EvaluationEpisode.run_id.in_([r.id for r in locked]),
                    EvaluationEpisode.status.in_(PARKED_EPISODE_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
    targets = [r for r in locked if r.id not in parked]
    if not targets:
        return 0

    swept = 0
    terminal_episode_statuses = list(TERMINAL_EPISODE_STATUSES)
    for run in targets:
        # Every NON-TERMINAL episode of this run, taken WITHOUT waiting. Lock
        # order is run-then-episode and the run is already held above.
        #
        # The wider set, not just ``"running"``, makes the parked check
        # authoritative. The run lock does not exclude the callback
        # path: ``resume_from_callback`` locks the EPISODE alone. A callback
        # landing after the unlocked read would move the last ``"running"``
        # episode to ``"resumed"``, and the run would go ``failed`` with the
        # callback already accepted.
        #
        # Locking the episodes leaves the callback three orderings: committed
        # already (read back below as parked), holding the row (the count
        # mismatch defers us), or arriving after our commit (it finds a
        # terminal episode).
        episodes = list(
            (
                await db.execute(
                    select(EvaluationEpisode)
                    .where(
                        EvaluationEpisode.run_id == run.id,
                        EvaluationEpisode.status.notin_(terminal_episode_statuses),
                    )
                    .order_by(EvaluationEpisode.id)
                    .with_for_update(of=EvaluationEpisode, skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        expected = (
            await db.execute(
                select(func.count(EvaluationEpisode.id)).where(
                    EvaluationEpisode.run_id == run.id,
                    EvaluationEpisode.status.notin_(terminal_episode_statuses),
                )
            )
        ).scalar_one()
        if len(episodes) < expected:
            # A held row means a live transaction is writing to this run:
            # EVIDENCE AGAINST abandonment. Terminalising around it would leave
            # a "running" episode under a "failed" run that this sweep never
            # selects again. Deferring costs one tick.
            logger.info(
                "sweep_stranded_runs: deferring run %s, %d of %d episode(s) held "
                "by another transaction",
                run.id,
                expected - len(episodes),
                expected,
            )
            continue

        # The parked veto, decided from rows THIS TRANSACTION HOLDS. The
        # unlocked pre-filter is only an optimisation: it keeps a healthy run
        # parked for hours from taking episode locks every tick, which would
        # make a live callback queue behind this batch-long transaction.
        # OVERLAPS the unrecognised guard below, on purpose: that guard would
        # defer a parked run too, but with a misleading warning. Kept for the
        # message.
        parked_now = [e for e in episodes if e.status in PARKED_EPISODE_STATUSES]
        if parked_now:
            logger.info(
                "sweep_stranded_runs: skipping run %s, %d episode(s) hold live "
                "callback work",
                run.id,
                len(parked_now),
            )
            continue

        # Anything left whose status this function does not RECOGNISE defers the
        # run rather than being swept with it. Both lists are hand-maintained:
        # a status added later that means "parked" would otherwise be driven to
        # ``"failed"`` by a janitor that never heard of it. Destroying live work
        # is worse than leaving it stranded.
        #
        # The cost: this defer is UNBOUNDED, and such a run keeps a candidate
        # slot and re-logs on every tick; enough of them would crowd stranded
        # runs out of the pass. Accepted: no such status exists today, and the
        # warning names the status and where to classify it.
        unrecognised = [
            e for e in episodes if e.status not in _ACTIVE_EPISODE_STATUSES
        ]
        if unrecognised:
            logger.warning(
                "sweep_stranded_runs: deferring run %s, episode status(es) %s "
                "are neither active nor parked -- classify them in "
                "evaluation_continuation before this sweep can close such a run",
                run.id,
                sorted({e.status for e in unrecognised}),
            )
            continue

        # Read off the locked row, so it is the owner the predicate matched.
        error = (
            STRANDED_RUN_ERROR
            if run.owner_execution_id is not None
            else UPGRADE_INTERRUPTED_RUN_ERROR
        )
        # Every remaining episode, not only the ``"running"`` ones: a
        # ``"pending"`` one left behind would stay non-terminal under a
        # ``"failed"`` run this sweep never selects again. (``start_episode``
        # sets ``"running"`` today; a second creation site might not, and the
        # column defaults to ``"pending"``.)
        for episode in episodes:
            episode.status = "failed"
            episode.error = error
            episode.completed_at = now

        run.status = "failed"
        run.error = error
        run.completed_at = now
        # Set here because nothing else will: every other terminal path
        # populates it, and a blank duration reads in the UI as a bug.
        if run.started_at is not None:
            started = run.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            run.duration_seconds = round((now - started).total_seconds(), 2)
        # Flushed BEFORE the count below, so it reads the episode UPDATEs
        # without relying on autoflush ordering.
        await db.flush()
        # Recomputed the way ``_reconcile_run_status`` computes it -- count of
        # TERMINAL episodes, ``max``-ed so it can only advance -- so a swept
        # run's numbers match a reconciled one's. A query rather than
        # arithmetic, which would break the moment the loop above stops
        # finalising every episode it holds.
        swept_completed = (
            await db.execute(
                select(func.count(EvaluationEpisode.id)).where(
                    EvaluationEpisode.run_id == run.id,
                    EvaluationEpisode.status.in_(list(TERMINAL_EPISODE_STATUSES)),
                )
            )
        ).scalar_one()
        run.completed_entries = max(run.completed_entries, swept_completed)
        # Flushed HERE so this status transition is OUTSIDE the savepoint
        # ``_write_canonical_headlines`` opens below, as in
        # ``_reconcile_run_status``: ``begin_nested()`` autoflushes
        # anyway, this does not depend on it. A rolled-back transition would be
        # dropped by the handler's ``db.refresh(run)`` while the run is still
        # counted and logged as swept.
        await db.flush()
        # The ONE canonical writer, so a swept run's numbers match any
        # other run's.
        await _write_canonical_headlines(db, run)
        swept += 1
        if run.owner_execution_id is None:
            logger.warning(
                "sweep_stranded_runs: run %s has no owner and no live execution "
                "of evaluation %s — interrupted by an upgrade, driven to failed",
                run.id,
                run.evaluation_id,
            )
        else:
            logger.warning(
                "sweep_stranded_runs: run %s left running by execution %s, which "
                "is terminal — driven to failed",
                run.id,
                run.owner_execution_id,
            )

    if not swept:
        return 0
    await db.commit()
    return swept


async def timeout_waiting_episodes(db: AsyncSession, *, limit: int | None = None) -> int:
    """Mark overdue waiting_callback episodes as timed_out.

    BOUNDED, like every other sweep in the reconciler cycle. Without a
    cap, a callback-provider outage leaves thousands of overdue episodes, and
    one transaction locks every run behind them while the live engine's
    end-of-entry writes queue up. ``periodic()`` is awaited INLINE, so the
    backlog can also trip the reconciler liveness probe. Residual rows are
    taken next tick. The cap bounds the episodes taken and the runs locked;
    the batch read also passes over the overdue episodes of runs other
    transactions hold, which it reads without waiting or locking any.

    COMMIT CONTRACT: commits on the path where it finalizes something, and
    returns WITHOUT committing on both early returns; the ``not episodes`` one
    still holds the run locks taken below, so the caller must commit or roll
    back either way. The OPPOSITE of ``fail_orphaned_episodes``.

    LOCK ORDER: run, then episode -- see ``process_resume_episode``.
    ``finalize_episode`` flushes an ``UPDATE``, which takes the episode row
    lock as ``FOR UPDATE`` does, so finalizing before locking the run would
    deadlock against ``fail_abandoned_episode`` and ``process_resume_episode``.
    An audit of ``with_for_update`` sites would not surface that.
    """
    now = datetime.now(UTC)
    if limit is None:
        limit = settings.orchestration_claim_batch
    # The batch and its runs' locks in ONE statement: PostgreSQL applies
    # ``LIMIT`` after ``SKIP LOCKED``, so an episode whose run another
    # transaction holds is passed over before it counts. Picked first and
    # locked after, a held run's overdue episodes, being the oldest, would fill
    # every batch while it stays held and no other overdue episode would be
    # reached. ``skip_locked``: ``periodic()`` is awaited inline in the
    # reconciler cycle every adapter shares, so it never waits on a run; a
    # skipped run's episodes are timed out on the first tick after its release.
    # Taken in deadline order, not by id: a statement that never waits cannot
    # deadlock. Ordered so a bounded sweep drains the backlog deterministically.
    rows = (
        await db.execute(
            select(EvaluationEpisode.id)
            .join(EvaluationRun, EvaluationRun.id == EvaluationEpisode.run_id)
            .where(
                EvaluationEpisode.status == "waiting_callback",
                EvaluationEpisode.callback_deadline.isnot(None),
                EvaluationEpisode.callback_deadline < now,
            )
            .order_by(EvaluationEpisode.callback_deadline, EvaluationEpisode.id)
            .limit(limit)
            .with_for_update(of=EvaluationRun, skip_locked=True)
        )
    ).all()
    if not rows:
        return 0
    episodes = list(
        (
            await db.execute(
                select(EvaluationEpisode)
                .where(
                    EvaluationEpisode.id.in_([r.id for r in rows]),
                    # Re-checked UNDER the lock: ``resume_from_callback`` may
                    # have accepted a callback in the gap, and stamping
                    # ``timed_out`` over it would drop an ACCEPTED payload.
                    EvaluationEpisode.status == "waiting_callback",
                )
                .order_by(EvaluationEpisode.id)
                # ``skip_locked``: never WAIT on a row another transaction
                # holds. The holder is ``resume_from_callback`` on the
                # REQUEST path, until the endpoint commits; waiting would leave
                # this sweep sitting on run rows, blocking the live engine's
                # end-of-entry run write. A skipped row either resolves to
                # ``resumed`` or is timed out next tick.
                .with_for_update(of=EvaluationEpisode, skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    if len(episodes) < len(rows):
        # Otherwise a row held forever by a wedged ``resume_from_callback`` is
        # invisible. Debug level: the count conflates "locked" with "already
        # ``resumed``", both benign.
        logger.debug(
            "timeout sweep skipped %d locked/resolved overdue episode(s) of %d",
            len(rows) - len(episodes),
            len(rows),
        )

    if not episodes:
        return 0

    for episode in episodes:
        await ConversationContinuationService.finalize_episode(
            db,
            episode=episode,
            first_turn_output=episode.first_turn_output,
            final_output=episode.final_output,
            timed_out=True,
            error="Callback deadline exceeded",
        )
    # Once per RUN, after every episode of it is finalized, as in
    # ``fail_orphaned_episodes``.
    #
    # Ids read BEFORE the first reconcile: ``_reconcile_run_status``
    # may roll back a savepoint, which EXPIRES the instances it touched, so
    # ``episode.run_id`` afterwards could lazy-reload.
    run_ids = sorted({episode.run_id for episode in episodes})
    for run_id in run_ids:
        await _reconcile_run_status(
            db,
            run_id=run_id,
            fallback_error="Callback deadline exceeded",
        )
    await db.commit()
    return len(episodes)
