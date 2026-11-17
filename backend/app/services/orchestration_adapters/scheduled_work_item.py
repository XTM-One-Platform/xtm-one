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

"""Scheduled work item adapter.

Runs the assignment-less ``AssignmentRun`` the scheduler tick created for a
due scheduled work item (``scheduled_work_item_runner.execute_scheduled_run``).
The execution is keyed ``assignment_run:<run id>``, like any run's dispatch,
so the stale-run sweep, a run deletion and the emergency stop find it.

A run its stopping worker handed back (a rollout, a scale-in, a node drain) is
deferred to the head of the queue without spending an attempt, and resumed
from its checkpoint by the next worker (as an assignment run). A run whose worker
was killed before it could hand back (a tool call longer than the pod's
termination grace) is taken back the moment that worker is known gone, first
in the queue, and resumes as graceful when the worker had said it was stopping
(as an assignment run).
"""

from __future__ import annotations

from app.models.orchestration import OrchestrationExecution
from app.services.orchestration_adapters.base import (
    AdapterContext,
    AdapterResult,
    OrchestrationAdapter,
)
from app.services.scheduled_work_items import ADAPTER_NAME


class ScheduledWorkItemAdapter(OrchestrationAdapter):
    name = ADAPTER_NAME
    # An instruction resumes from its run's checkpoint and a replaced attempt
    # writes no ending: taken back the moment its worker is known gone,
    # not after its lease, on that worker's last word.
    resume_when_worker_gone = True

    def raised_attempt_bound(self, max_attempts: int) -> int:
        """A run queued under the old floor (4) is taken back under the task bound."""
        from app.services.orchestration_adapters.sub_agent import raised_task_bound

        return raised_task_bound(max_attempts)

    @property
    def cancel_poll_seconds(self) -> float | None:
        """An instruction runs an agent loop: an emergency stop aborts it between iterations."""
        return self._configured_cancel_poll()

    async def execute(
        self, ctx: AdapterContext, execution: OrchestrationExecution
    ) -> AdapterResult:
        from app.services import assignment_run_resume
        from app.services.orchestration_adapters.sub_agent import (
            HANDED_BACK_STATE,
            _end_hand_back,
            _taken_back_on_its_word,
        )
        from app.services.scheduled_work_item_runner import (
            HANDED_BACK,
            REQUEUED,
            execute_scheduled_run,
        )

        run_id = (execution.payload or {}).get("run_id")
        if not run_id:
            return AdapterResult.failed(
                "Scheduled work item execution payload missing run_id", retryable=False
            )
        # Taken back from a worker that said it was stopping, or that a deploy
        # replaced: a graceful resume. Read before the hand-back's
        # record and adapter state are ended.
        taken_back_on_its_word = _taken_back_on_its_word(execution)
        # A hand-back's deferral record only carried the attempt over to this
        # claim: it ends here, so a later hold starts its own bound.
        execution = await _end_hand_back(ctx, execution)
        # The runner owns its sessions; release this one's connection first.
        await ctx.db.commit()
        outcome = await execute_scheduled_run(
            str(run_id),
            attempt=getattr(execution, "attempt_count", None) or 1,
            cancel_event=getattr(ctx, "cancel_event", None),
            deadline_at=getattr(execution, "deadline_at", None),
            taken_back_on_its_word=taken_back_on_its_word,
        )
        if outcome.kind == HANDED_BACK:
            # Its worker was asked to stop: the turn wound down at an iteration
            # boundary, checkpointed. Due again now, first in the queue, without
            # spending an attempt; the resume that follows is graceful.
            return AdapterResult.defer(
                assignment_run_resume.HANDED_BACK_ERROR,
                delay_seconds=0.0,
                adapter_state=HANDED_BACK_STATE,
                place="head",
            )
        if outcome.kind == REQUEUED:
            # Ended here; its owner agent runs the action again as a fresh run.
            return AdapterResult.completed()
        if outcome.kind == "retry":
            return AdapterResult.failed(
                outcome.error or "Scheduled work item deferred",
                retryable=True,
                backoff_seconds=outcome.backoff_seconds,
            )
        if outcome.kind == "blocked":
            return AdapterResult.failed(
                outcome.error or "Scheduled work item blocked", retryable=False
            )
        return AdapterResult.completed()

    async def on_dead_letter(
        self, ctx: AdapterContext, execution: OrchestrationExecution
    ) -> None:
        from app.services.scheduled_work_item_runner import settle_dead_letter

        await ctx.db.commit()
        await settle_dead_letter(
            (execution.payload or {}).get("run_id"),
            execution.last_error,
            int(getattr(execution, "attempt_count", 0) or 0),
        )
