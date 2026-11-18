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

"""A flow as evaluation subject (#8257).

An evaluation's subject is an agent or a flow (``EvaluationAgent.flow_id``). A
flow is run once per entry, as a manual run, and its final output - the one a
``flow.call`` parent reads (``flow_runner.final_step_output``) - is judged and
measured like an agent's answer. Its models are per node, so a flow runs once
per launch on ``MODEL_INDEPENDENT``, in the ``flow`` execution mode.

An entry becomes the run's parameters: an input that is a JSON object is the
parameter map, coerced against the manual trigger's declaration; any other
input is the run's ``prompt`` (``message`` for a chat trigger). A flow that
ends failed, cancelled or past the bound answers ``Error: ...``: judged and
counted, never reused (``fingerprint._ERROR_OUTPUT_PREFIXES``). A run that could
not start raises, and the engine leaves the entry out as a platform error.

Cost and latency come from the run's own trace spans, per serving model
(``run_ledger.calls_from_spans``): a ledger the traces did not finish writing
is reported incomplete, never as the part measured.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.flow import Flow
from app.models.flow_run import (
    FLOW_RUN_ACTIVE_STATUSES,
    FLOW_RUN_STATUS_COMPLETED,
    FlowRun,
    FlowRunStep,
)
from app.models.llm_trace import LLMSpan, LLMTrace
from app.services.run_ledger import RunLedger, calls_from_spans

logger = logging.getLogger(__name__)

#: The execution mode a flow subject's runs and results record.
FLOW_EXECUTION_MODE = "flow"

#: Where an evaluation's flow run says what started it, in ``trigger_context``.
EVALUATION_TRIGGER_SOURCE = "evaluation"

_POLL_SECONDS = 2.0
#: How long the run's traces may take to land after it ends (written in the
#: background): past it, the ledger is incomplete.
_TRACE_SETTLE_SECONDS = 10.0


class FlowSubjectUnavailableError(Exception):
    """A flow run that could not start: the entry is left out, with the reason."""


@dataclass
class FlowEntryOutcome:
    """One entry's flow run, as the engine scores it."""

    output: str
    flow_run_id: uuid.UUID | None
    status: str
    wall_clock_ms: int
    trace_ids: list[str] = field(default_factory=list)
    #: ``None`` when the run's traces were not all written in time.
    ledger: RunLedger | None = None
    #: The run's steps, for the result's timeline.
    timeline: list[dict] = field(default_factory=list)
    cancelled: bool = False
    #: A sandbox copy's run (level 2): its report, simulated writes included.
    sandbox: dict | None = None


def entry_parameters(input_prompt: str | None) -> dict:
    """An entry's run parameters: a JSON object input as is, else ``prompt`` and ``message``."""
    text = input_prompt or ""
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    return {"prompt": text, "message": text}


async def flow_run_trace_ids(db: AsyncSession, run: FlowRun) -> tuple[list[str], bool]:
    """The run's trace ids (one per burst) and whether every one has ended.

    Read through ``ix_llm_traces_source_started_at`` over the run's own time
    window, and matched in the query on the run id the walker writes in each
    trace's metadata.
    """
    if run.started_at is None:
        return [], True
    query = select(LLMTrace.id, LLMTrace.ended_at).where(
        LLMTrace.source == "flow",
        LLMTrace.started_at >= run.started_at - timedelta(seconds=5),
        LLMTrace.metadata_json["flow_run_id"].as_string() == str(run.id),
    )
    if run.completed_at is not None:
        query = query.where(LLMTrace.started_at <= run.completed_at + timedelta(seconds=5))
    rows = (await db.execute(query)).all()
    return [str(row.id) for row in rows], all(row.ended_at is not None for row in rows)


async def flow_run_ledger(db: AsyncSession, run: FlowRun) -> tuple[list[str], RunLedger | None]:
    """The run's trace ids and its ledger, ``None`` when its traces did not settle in time."""
    deadline = time.monotonic() + _TRACE_SETTLE_SECONDS
    while True:
        trace_ids, settled = await flow_run_trace_ids(db, run)
        if settled or time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.5)
    if not settled or not trace_ids:
        return trace_ids, None
    spans = (
        await db.execute(
            select(LLMSpan)
            .where(LLMSpan.trace_id.in_([uuid.UUID(t) for t in trace_ids]), LLMSpan.type.in_(("GENERATION", "TOOL")))
            .order_by(LLMSpan.started_at, LLMSpan.position)
        )
    ).scalars()
    calls = calls_from_spans(
        {
            "type": span.type,
            "name": span.name,
            "model": span.model,
            "duration_ms": span.duration_ms,
            "level": span.level,
            "status_message": span.status_message,
            "usage_details": span.usage_details,
            "metadata": span.metadata_json,
        }
        for span in spans
    )
    return trace_ids, {"calls": calls, "spawned_task_ids": []}


async def _timeline(db: AsyncSession, run_id: uuid.UUID) -> list[dict]:
    steps = (
        await db.execute(select(FlowRunStep).where(FlowRunStep.flow_run_id == run_id).order_by(FlowRunStep.seq))
    ).scalars()
    timeline: list[dict] = []
    for step in steps:
        duration_ms = 0
        if step.completed_at is not None and step.started_at is not None:
            duration_ms = int((step.completed_at - step.started_at).total_seconds() * 1000)
        timeline.append(
            {
                "type": "flow_step",
                "node_id": step.node_id,
                "node_kind": step.node_kind,
                "status": step.status,
                "duration_ms": duration_ms,
                "description": f"{step.node_kind} ({step.node_id}): {step.status}",
            }
        )
    return timeline


async def run_flow_for_entry(
    db: AsyncSession,
    flow: Flow,
    *,
    evaluation_run_id: uuid.UUID,
    entry_id: uuid.UUID | None,
    input_prompt: str | None,
    owner_id: uuid.UUID | None,
    timeout_seconds: float,
    is_cancelled: Callable[[], Awaitable[bool]],
    case_ref: str | None = None,
) -> FlowEntryOutcome:
    """Run *flow* on one entry and wait for its final output, bounded by *timeout_seconds*.

    The run is a manual run owned by *owner_id*, marked as an evaluation's in
    its ``trigger_context`` (``evaluation_entry_id`` is how a sandbox replay
    finds the entry's recorded tool outputs). A cancelled evaluation run stops
    the flow run. A sandbox copy (level 2) starts through
    ``start_sandbox_flow_run``, its writes intercepted, as case *case_ref*,
    replaying the entry's recordings.
    """
    from app.core.database import release_db_connection_before_loop
    from app.services import orchestration_engine as engine
    from app.services.assignment_pause import FlowOwnerDeactivatedError
    from app.services.flow_runner import (
        TriggerParametersError,
        create_flow_run,
        final_step_output,
        manual_run_trigger,
        stop_flow_run,
    )
    from app.services.health.sandbox.runs import SandboxRunError

    try:
        _entry, trigger_type, trigger_payload = manual_run_trigger(flow.graph or {}, entry_parameters(input_prompt))
    except LookupError:
        raise FlowSubjectUnavailableError("The flow has no trigger node.") from None
    except TriggerParametersError as exc:
        raise FlowSubjectUnavailableError(
            "The entry does not match the flow's run parameters: " + "; ".join(exc.errors)
        ) from None
    sandboxed = bool(getattr(flow, "is_sandbox", False))
    try:
        if sandboxed:
            from app.services.health.sandbox.copies import sandbox_copy_of
            from app.services.health.sandbox.runs import start_sandbox_flow_run

            copy = await sandbox_copy_of(db, flow_id=flow.id)
            if copy is None:
                raise FlowSubjectUnavailableError("The sandbox copy is not available.")
            flow_run = await start_sandbox_flow_run(
                db,
                copy,
                trigger_type=trigger_type,
                trigger_payload=trigger_payload,
                case_ref=case_ref,
                entry_id=entry_id,
            )
        else:
            flow_run = await create_flow_run(
                db,
                flow,
                trigger_type=trigger_type,
                trigger_payload=trigger_payload,
                trigger_context={
                    "source": EVALUATION_TRIGGER_SOURCE,
                    "evaluation_run_id": str(evaluation_run_id),
                    "evaluation_entry_id": str(entry_id) if entry_id else None,
                },
                user_id=owner_id,
            )
    except FlowOwnerDeactivatedError:
        raise FlowSubjectUnavailableError("The flow's owner is deactivated.") from None
    except SandboxRunError as exc:
        logger.info("Sandbox flow run of %s refused: %s", flow.id, exc)
        raise FlowSubjectUnavailableError("The sandbox copy could not run.") from None
    flow_run_id = flow_run.id
    await db.commit()
    await engine.notify_orchestration_wake()

    deadline = time.monotonic() + max(timeout_seconds, 1.0)
    cancelled = timed_out = False
    while True:
        await release_db_connection_before_loop(db)
        await asyncio.sleep(_POLL_SECONDS)
        status = await db.scalar(select(FlowRun.status).where(FlowRun.id == flow_run_id))
        if status not in FLOW_RUN_ACTIVE_STATUSES:
            break
        cancelled = await is_cancelled()
        timed_out = time.monotonic() >= deadline
        if cancelled or timed_out:
            row = await db.get(FlowRun, flow_run_id, with_for_update=True)
            if row is not None:
                await stop_flow_run(
                    db,
                    row,
                    step_error="The evaluation stopped this run.",
                    command_reason="The evaluation that started this flow run stopped it.",
                )
            await db.commit()
            break

    flow_run = await db.get(FlowRun, flow_run_id, populate_existing=True)
    if flow_run is None:
        raise FlowSubjectUnavailableError("The flow run vanished.")
    # The run's own clock, from its creation to its end: the poll above only
    # notices the end, a beat later.
    ended = flow_run.completed_at or datetime.now(UTC)
    wall_clock_ms = max(int((ended - flow_run.created_at).total_seconds() * 1000), 0)
    trace_ids, ledger = await flow_run_ledger(db, flow_run)
    timeline = await _timeline(db, flow_run_id)
    if cancelled:
        output = ""
    elif timed_out:
        output = f"Error: the flow run did not finish within {int(timeout_seconds)} s."
    elif flow_run.status != FLOW_RUN_STATUS_COMPLETED:
        detail = (flow_run.error or flow_run.result_summary or "").strip()[:500]
        output = f"Error: the flow run ended {flow_run.status}" + (f": {detail}" if detail else ".")
    else:
        output = await final_step_output(db, flow_run_id)
    sandbox = None
    if sandboxed:
        from app.services.health.sandbox.runs import sandbox_run_report

        report = await sandbox_run_report(db, flow_run_id=flow_run_id)
        sandbox = report.to_dict() if report is not None else None
    return FlowEntryOutcome(
        output=output,
        flow_run_id=flow_run_id,
        status="timed_out" if timed_out else flow_run.status,
        wall_clock_ms=wall_clock_ms,
        trace_ids=trace_ids,
        ledger=ledger,
        timeline=timeline,
        cancelled=cancelled,
        sandbox=sandbox,
    )
