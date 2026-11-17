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

"""Interception of a sandbox flow's nodes (#8255, PRD 6.6).

Every burst of a run of a sandbox flow (``Flow.is_sandbox``), however it was
started, is bound to a ``sandbox`` session (:func:`flow_burst_session`). Each
node then goes through :func:`intercept_flow_node` before it executes:

- a ``none`` or ``nested`` node runs (an agent node's tool calls are
  intercepted by the same session; a called flow is a sandbox flow or not);
- a ``read`` node replays a matching recorded step, else runs;
- a ``write`` node replays a matching recorded step, else is answered by a
  model simulating its outputs from the node's recorded outputs.

Recordings are the evaluation entry's recorded calls when the run has an
entry that holds some, else the completed steps of the copy's source runs:
matched on node id and kind, then on the step's rendered input (exact, then
normalized - the replay module's rule). A step a session answered is stamped
(``SANDBOX_OUTCOME_KEY``) and a run started by, or called from, a sandbox run
is marked (:data:`SANDBOX_RUN_SOURCE`): neither is ever a recording, and the
marked runs stay out of production run listings (:func:`production_runs_only`).
The counts live in ``run.state["sandbox"]`` across bursts.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.concierge.sandbox.replay import MAX_RECORDED_CALLS, SANDBOX_OUTCOME_KEY, RecordedCall
from app.services.concierge.sandbox.session import SandboxReport, SandboxSession
from app.services.concierge.sandbox.side_effects import node_effect, node_tool_vouched

logger = logging.getLogger(__name__)

#: ``run.state`` key of a sandbox run's report.
STATE_KEY = "sandbox"
#: ``FlowRun.trigger_context["source"]`` of a sandbox run and of every run it calls.
SANDBOX_RUN_SOURCE = "concierge_sandbox"


def production_runs_only() -> Any:
    """The clause keeping sandbox runs, and the runs they call, out of a
    listing of ``FlowRun`` rows."""
    from app.models.flow_run import FlowRun

    return func.coalesce(FlowRun.trigger_context["source"].as_string(), "") != SANDBOX_RUN_SOURCE


def node_recording_key(node_id: str, kind: str) -> str:
    """The name a node's recordings are indexed under."""
    return f"{node_id}:{kind}"


async def load_recorded_steps(
    db: AsyncSession, run_ids: Sequence[Any], *, flow_id: uuid.UUID | None
) -> list[RecordedCall]:
    """The completed steps of *run_ids* that are production runs of *flow_id*,
    as recordings, in run then step order (none without a source flow); a
    step a sandbox session answered is never one."""
    from app.models.flow_run import FlowRun, FlowRunStep

    ids: list[uuid.UUID] = []
    for value in run_ids:
        try:
            ids.append(uuid.UUID(str(value)))
        except (TypeError, ValueError):
            continue
    if not ids or flow_id is None:
        return []
    order = {rid: i for i, rid in enumerate(ids)}
    rows = (
        await db.execute(
            select(FlowRunStep, FlowRun.flow_id, FlowRun.user_id)
            .join(FlowRun, FlowRun.id == FlowRunStep.flow_run_id)
            .where(
                FlowRunStep.flow_run_id.in_(ids),
                FlowRunStep.status == "completed",
                FlowRunStep.output[SANDBOX_OUTCOME_KEY].as_string().is_(None),
                FlowRun.flow_id == flow_id,
                production_runs_only(),
            )
            .limit(MAX_RECORDED_CALLS)
        )
    ).all()
    rows.sort(key=lambda r: (order.get(r[0].flow_run_id, len(order)), r[0].seq))
    return [
        RecordedCall(
            tool=node_recording_key(step.node_id, step.node_kind),
            arguments=step.input if isinstance(step.input, dict) else {},
            output=json.dumps(step.output or {}, ensure_ascii=False, default=str),
            trace_id=str(step.flow_run_id),
            flow_id=str(flow_id),
            user_id=str(user_id) if user_id else None,
        )
        for step, flow_id, user_id in rows
    ]


#: Parent runs walked up to find a sandbox run (a flow call nests a few levels).
_MAX_CALL_DEPTH = 32


async def _sandboxed_flow_id(db: AsyncSession, run: Any) -> uuid.UUID | None:
    """The sandbox flow *run* belongs to: its own, or the one of the run that
    called it, at any depth. A flow called from a sandbox run runs in a burst
    of its own, where the caller's session is not bound: it is intercepted
    because its caller was, whatever flow it is."""
    from app.models.flow import Flow
    from app.models.flow_run import FlowRun

    flow_id, parent_id = run.flow_id, run.parent_run_id
    for _ in range(_MAX_CALL_DEPTH):
        if await db.scalar(select(Flow.is_sandbox).where(Flow.id == flow_id)) is True:
            return flow_id
        if not isinstance(parent_id, uuid.UUID):
            return None
        row = (await db.execute(select(FlowRun.flow_id, FlowRun.parent_run_id).where(FlowRun.id == parent_id))).first()
        if row is None:
            return None
        flow_id, parent_id = row
    # Deeper than any call chain the runner allows: intercepted, the safe side.
    return flow_id


#: ``FlowRun.trigger_context`` key of the evaluation entry a sandbox run answers.
ENTRY_KEY = "evaluation_entry_id"


async def _entry_source_run(db: AsyncSession, entry_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID | None] | None:
    """``(flow run, its flow)`` an entry was built from, ``None`` for an entry
    built from anything else."""
    from app.models.concierge_dataset import SOURCE_FLOW_RUN, ConciergeDatasetEntrySource

    row = (
        await db.execute(
            select(ConciergeDatasetEntrySource.source_id, ConciergeDatasetEntrySource.source_flow_id).where(
                ConciergeDatasetEntrySource.entry_id == entry_id,
                ConciergeDatasetEntrySource.source_kind == SOURCE_FLOW_RUN,
            )
        )
    ).first()
    return (row[0], row[1]) if row is not None else None


async def _entry_recordings(db: AsyncSession, run: Any) -> list[RecordedCall] | None:
    """The recordings of the entry *run* answers, ``None`` without one: its
    recorded tool calls (an agent node's, keyed by tool) plus, for an entry
    built from a flow run, that run's node steps (keyed by node), the ones a
    tool-calling node of the copy replays."""
    from app.services.concierge.datasets import entry_recordings

    context = run.trigger_context if isinstance(run.trigger_context, dict) else {}
    try:
        entry_id = uuid.UUID(str(context.get(ENTRY_KEY)))
    except (TypeError, ValueError):
        return None
    calls = (await entry_recordings(db, [entry_id])).get(entry_id)
    if calls is None:
        return None
    source = await _entry_source_run(db, entry_id)
    if source is None:
        return calls
    run_id, flow_id = source
    # Steps first: the replay index keeps only the first MAX_RECORDED_CALLS, and
    # a run's steps are few while an entry's tool calls can fill it.
    return await load_recorded_steps(db, [run_id], flow_id=flow_id) + calls


async def flow_burst_session(db: AsyncSession, run: Any, user: Any) -> SandboxSession | None:
    """The session a burst of *run* is bound to: ``None`` unless its flow, or
    a flow of the runs that called it, is a sandbox copy, or when the burst
    already runs inside one."""
    from app.services.concierge.sandbox.copies import sandbox_copy_of
    from app.services.concierge.sandbox.session import current_sandbox_session
    from app.services.concierge.sandbox.turns import build_sandbox_session

    if current_sandbox_session() is not None:
        return None
    sandbox_flow_id = await _sandboxed_flow_id(db, run)
    if sandbox_flow_id is None:
        return None
    # A called production flow replays nothing: the recordings are the copy's,
    # or those of the evaluation entry the run answers.
    recordings = await _entry_recordings(db, run) if sandbox_flow_id == run.flow_id else None
    if recordings is None:
        copy = await sandbox_copy_of(db, flow_id=run.flow_id) if sandbox_flow_id == run.flow_id else None
        recordings = (
            await load_recorded_steps(db, copy.source_run_ids or [], flow_id=copy.source_id) if copy else []
        )
    state = run.state if isinstance(run.state, dict) else {}
    previous = state.get(STATE_KEY) if isinstance(state.get(STATE_KEY), dict) else {}
    session = build_sandbox_session(
        user_id=str(user.id), model=None, recorded_calls=recordings, case_id=previous.get("case_id")
    )
    return session


def burst_report(base: dict | None, session: SandboxSession) -> dict:
    """The run's report: *base* (the earlier bursts') plus this burst's so far."""
    report = SandboxReport.from_dict(base)
    report.add(session.report)
    return report.to_dict()


async def record_flow_report(db: AsyncSession, run: Any, report: dict) -> None:
    """Copy a sandbox flow run's report onto its ``sandbox_runs`` row, if it
    has one (flushed with the burst)."""
    from datetime import UTC, datetime

    from app.models.flow_run import FLOW_RUN_ACTIVE_STATUSES, FLOW_RUN_STATUS_COMPLETED
    from app.models.sandbox_copy import RUN_COMPLETED, RUN_FAILED, SandboxRun

    row = (await db.execute(select(SandboxRun).where(SandboxRun.flow_run_id == run.id))).scalar_one_or_none()
    if row is None:
        return
    row.report = report
    row.simulated_writes = int(report.get("simulated_writes") or 0)
    if run.status not in FLOW_RUN_ACTIVE_STATUSES:
        row.status = RUN_COMPLETED if run.status == FLOW_RUN_STATUS_COMPLETED else RUN_FAILED
        row.finished_at = row.finished_at or datetime.now(UTC)


def _outputs(text: str) -> dict:
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"output": text}
    return parsed if isinstance(parsed, dict) else {"output": parsed}


async def intercept_flow_node(
    db: AsyncSession | None,
    session: SandboxSession,
    *,
    node: dict,
    kind: str,
    step_input: dict | None,
    llm_wired: bool,
) -> tuple[dict, str] | None:
    """``None`` when the node runs; else its outputs, stamped with how they were
    obtained (``replayed``, ``simulated`` or ``intercepted``) under
    ``SANDBOX_OUTCOME_KEY``, and that outcome."""
    config = node.get("config") or {}
    vouched = not llm_wired and await node_tool_vouched(db, kind, config)
    effect = node_effect(kind, config, llm_wired=llm_wired, vouched=vouched)
    if effect in ("none", "nested"):
        return None
    key = node_recording_key(str(node.get("id") or ""), kind)
    arguments = step_input or {}
    recording = await session.recorded(key, arguments)
    if recording is not None:
        session.report.replayed += 1
        if effect == "write":
            session.report.intercepted_writes += 1
        session.log_call(key, effect, "replayed", arguments)
        return {**_outputs(recording.output), SANDBOX_OUTCOME_KEY: "replayed"}, "replayed"
    if effect == "read":
        session.report.live_reads += 1
        return None
    from app.services.flow_catalog import NODE_KINDS_BY_ID

    spec = NODE_KINDS_BY_ID.get(kind)
    description = ""
    if spec is not None:
        names = ", ".join(v.name for v in spec.output_variables)
        description = f"{spec.label}: {spec.description} Its result is a JSON object with: {names}."
    text, outcome = await session.answer_write(key, arguments, description)
    outputs = _outputs(text)
    # A simulated output is never trusted content: it was written by a model
    # from recorded, hostile values.
    outputs["_tainted"] = True
    outputs[SANDBOX_OUTCOME_KEY] = outcome
    return outputs, outcome
