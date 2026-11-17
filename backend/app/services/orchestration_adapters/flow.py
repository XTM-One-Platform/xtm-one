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

"""Flow adapter — drives FlowRun bursts through the orchestration engine.

One execution == one flow run, cradle to grave (the autonomous adapter's
shape, not sub_agent's): the walker runs a **burst** of nodes under a single
lease, and parks with ``await_event`` + a scheduled self-resume outbox event
whenever a node needs to wait (``util.delay``, a triggered assignment, a
sub-flow).  Sub-flow children additionally emit a ``resume`` event at the
parent's execution the moment they finish, so the parent wakes immediately
rather than on its safety recheck.

Why the cursor is NOT in ``adapter_state``: that column is 50 chars and is
wiped by ``fail``-with-retry, lease reclaim and requeue.  The whole cursor
lives in ``FlowRun.state`` + the ``flow_run_steps`` journal, written on
``ctx.db`` inside the burst.  The walker commits per step transition (live
journal + SSE frames — see ``flow_run_stream``), so a crash mid-burst
resumes at the last finished node; the engine's own state transition in
``_apply_result`` still commits last, and the terminal-status guard below
absorbs a re-claim between the walker's terminal commit and that transition.

Deadlines are a **rolling window**: every park re-arms ``deadline_at`` to
``now + flow_run_timeout_seconds`` (``await_event`` overwrites the column
unconditionally, so it must be re-passed anyway).  A run therefore fails only
after a full window with no progress — a flow that sleeps on delays for days
keeps living as long as each hop happens on time.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.config import settings
from app.core.redaction import known_secrets_scope, redact_known_secrets_for_display
from app.llm.provider_access import acting_for
from app.models.flow_run import (
    FLOW_RUN_ACTIVE_STATUSES,
    FLOW_RUN_STATUS_FAILED,
    FlowRun,
)
from app.models.orchestration import OrchestrationExecution, OrchestrationOutboxEvent
from app.services import orchestration_engine as engine
from app.services.orchestration_adapters.base import (
    AdapterContext,
    AdapterResult,
    OrchestrationAdapter,
)

logger = logging.getLogger(__name__)

ADAPTER_NAME = "flow"


def execution_dedup_key(run_id: str) -> str:
    return f"flow_run:{run_id}"


class FlowAdapter(OrchestrationAdapter):
    """Walks a FlowRun's graph in bursts; parks between waits."""

    name = ADAPTER_NAME

    @property
    def cancel_poll_seconds(self) -> float | None:
        """Opt into the reconciler's fast cancel watcher (emergency stop)."""
        return self._configured_cancel_poll()

    async def execute(
        self, ctx: AdapterContext, execution: OrchestrationExecution
    ) -> AdapterResult:
        payload = execution.payload or {}
        run_id = payload.get("run_id")
        if not run_id:
            return AdapterResult.failed(
                "flow execution payload has no run_id", retryable=False
            )

        run = (
            await ctx.db.execute(
                select(FlowRun).where(FlowRun.id == uuid.UUID(str(run_id)))
            )
        ).scalar_one_or_none()
        if run is None:
            return AdapterResult.failed(
                f"flow run {run_id} not found", retryable=False
            )
        # Terminal-status guard (same as sub_agent): a re-claim after a crash
        # between the domain write and the engine transition must not re-run.
        if run.status not in FLOW_RUN_ACTIVE_STATUSES:
            return (
                AdapterResult.completed()
                if run.status != FLOW_RUN_STATUS_FAILED
                else AdapterResult.failed(run.error or "flow run failed", retryable=False)
            )

        from app.models.user import User
        from app.services.flow_runner import FlowWalker

        user = None
        if run.user_id is not None:
            user = (
                await ctx.db.execute(select(User).where(User.id == run.user_id))
            ).scalar_one_or_none()
        if user is None:
            run.status = FLOW_RUN_STATUS_FAILED
            run.error = "The run's user no longer exists."
            run.completed_at = datetime.now(UTC)
            return AdapterResult.failed(run.error, retryable=False)

        walker = FlowWalker(
            ctx.db, run, user, cancel_event=getattr(ctx, "cancel_event", None)
        )
        # The run's known-secrets scope: the engine commits after this
        # returns, so what the burst wrote is flushed while the scope is open,
        # and the failure it records as ``last_error`` is redacted in it.
        # Every AI call of the burst is for the run's user, those with no user
        # of their own included: their LLM provider exclusions hold.
        with known_secrets_scope(run.id), acting_for(run.user_id):
            outcome = await walker.run_burst()
            await ctx.db.flush()
            failure = redact_known_secrets_for_display(outcome.error)

        if outcome.kind == "completed":
            return AdapterResult.completed()
        if outcome.kind == "cancelled":
            # Cancelled by the user — the execution ends cleanly.
            return AdapterResult.completed(payload_update={"cancelled": True})
        if outcome.kind == "skipped":
            # Never started: the flow's owner is deactivated.
            return AdapterResult.completed(payload_update={"skipped": True})
        if outcome.kind == "failed":
            # The walker already wrote run.status/error; not retryable — a
            # node failure is deterministic, replaying the burst repeats it.
            return AdapterResult.failed(
                failure or "flow run failed", retryable=False
            )

        # Park.  Schedule the self-resume when the walker asked for one
        # (delay wake, assignment poll, sub-flow safety recheck) and re-arm
        # the rolling deadline.
        deadline = datetime.now(UTC) + timedelta(
            seconds=settings.flow_run_timeout_seconds
        )
        if outcome.wake_at is not None:
            await engine.emit_outbox_event(
                ctx.db,
                execution_id=execution.id,
                event_type="resume",
                # The wake instant keys the event: each distinct park emits
                # its own resume; an identical re-park after an early wake
                # collides on purpose (same instant == same wake).
                dedup_key=f"wake:{outcome.wake_at.isoformat()}",
                available_at=outcome.wake_at,
            )
        return AdapterResult.await_event(
            adapter_state="awaiting", deadline_at=deadline
        )

    async def handle_event(
        self,
        ctx: AdapterContext,
        execution: OrchestrationExecution,
        event: OrchestrationOutboxEvent,
    ) -> AdapterResult | None:
        if event.event_type != "resume":
            logger.debug("flow adapter ignoring event %s", event.event_type)
            return None
        if execution.state in ("completed", "failed", "dead_letter", "cancelled"):
            return None
        await engine.rearm_for_resume(ctx.db, execution_id=execution.id)
        return None

    async def on_dead_letter(
        self, ctx: AdapterContext, execution: OrchestrationExecution
    ) -> None:
        """Deadline blown or attempts exhausted — fail the run honestly."""
        run_id = (execution.payload or {}).get("run_id")
        if not run_id:
            return
        run = (
            await ctx.db.execute(
                select(FlowRun)
                .where(FlowRun.id == uuid.UUID(str(run_id)))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if run is None or run.status not in FLOW_RUN_ACTIVE_STATUSES:
            return
        run.status = FLOW_RUN_STATUS_FAILED
        run.error = (
            execution.last_error
            or "The run made no progress for too long and was abandoned."
        )
        run.completed_at = datetime.now(UTC)
        # No burst will follow — close any journal row left `running` by the
        # abandoned execution (see flow_runner.close_running_steps).
        from app.services.flow_runner import (
            close_running_steps,
            release_runner_commands,
        )

        await close_running_steps(ctx.db, run.id, error=run.error)
        await release_runner_commands(
            ctx.db, run, reason="The flow run that dispatched this command was abandoned."
        )
        logger.warning("flow run %s dead-lettered: %s", run_id, run.error)
