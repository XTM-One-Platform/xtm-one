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

"""The Flow Lab's experiment kinds and flow finding access, on level 6's hooks (#8260).

Level 6 runs, settles and files every experiment the same way; each change
type only says how it is drafted, kept current and measured
(``experiments.register_experiment_kind``). The four flow types:

| Type | Finding target | Dataset (level 4) | Baseline arm | Variant arm |
|---|---|---|---|---|
| ``flow_repair``, ``flow_optimization`` | the flow | the flow's runs | a fresh copy of the flow | a fresh copy carrying the drafted graph |
| ``new_flow`` | the agent whose chats repeat the sequence | the agent's traces | a copy of the agent | a prototype copy carrying the graph |
| ``agent_to_flow`` | the assignment | its agent's traces | a copy of the agent | a prototype copy carrying the graph |

Both arms are level 2 sandbox copies created for the run, so what is measured
is exactly the graph drafted (``variant["graph"]``), whatever happens to the
Flow Lab's working copy afterwards. A repaired or optimized copy replays its
source's recorded writes; a prototype has no recording, so its writes are
simulated and counted.

``register_target_access("flow", ...)`` makes flow findings and experiments
readable under the same rule as the Flow Lab's tools: the Concierge within
its supervision scope (content when the flow is opted in), a person who may
manage the flow (content too: they read its run journal).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.flow import Flow
from app.services.health import access as quality_access
from app.services.health import quality_deps as deps
from app.services.health.experiments import DraftSpec, ExperimentError, register_experiment_kind

FLOW_CHANGE_TYPES = ("flow_repair", "flow_optimization", "new_flow", "agent_to_flow")
SUMMARY_MAX_CHARS = 500

#: How many of a source flow's latest settled runs a baseline copy replays writes from.
REPLAY_RUNS = 50


async def _sandbox_flow(db: AsyncSession, payload: dict[str, Any], *, source_id: uuid.UUID | None) -> tuple[Any, Flow]:
    """The drafted sandbox flow and its copy row; it must copy *source_id* (``None``: a prototype)."""
    from app.services.health.sandbox.copies import sandbox_copy_of

    try:
        flow_id = uuid.UUID(str(payload.get("sandbox_flow_id") or ""))
    except ValueError:
        raise ExperimentError("sandbox_flow_id must be the id of a sandbox flow.") from None
    copy = await sandbox_copy_of(db, flow_id=flow_id)
    flow = await db.get(Flow, flow_id) if copy is not None else None
    if flow is None or copy.source_type != "flow" or copy.source_id != source_id:
        expected = (
            "a copy of the finding's flow" if source_id else "a new prototype (create_sandbox_flow without from_flow)"
        )
        raise ExperimentError(f"The sandbox flow must be {expected}.")
    from app.services.flow_graph import validate_graph

    if not validate_graph(flow.graph or {}).is_valid:
        raise ExperimentError("The sandbox flow has validation errors: fix it before drafting.")
    return copy, flow


def _summary(payload: dict[str, Any]) -> str:
    summary = " ".join(str(payload.get("summary") or "").split())[:SUMMARY_MAX_CHARS]
    if not summary:
        raise ExperimentError("summary must say in one line what the variant changes.")
    return summary


async def _flow_copy(
    db: AsyncSession, *, source_id: uuid.UUID | None, graph: dict | None, created_for: str, author_agent_id: Any
) -> deps.Subject:
    from app.models.flow_run import FlowRun
    from app.services.health.sandbox.copies import SandboxOverrides, create_sandbox_copy

    replay_from: list[Any] = []
    if source_id is not None:
        replay_from = list(
            (
                await db.execute(
                    select(FlowRun.id)
                    .where(FlowRun.flow_id == source_id, FlowRun.status.in_(("completed", "failed")))
                    .order_by(FlowRun.created_at.desc())
                    .limit(REPLAY_RUNS)
                )
            ).scalars()
        )
    copy = await create_sandbox_copy(
        db,
        source_type="flow",
        source_id=source_id,
        created_for=created_for,
        created_by_agent_id=author_agent_id,
        overrides=SandboxOverrides(graph=graph) if graph is not None else None,
        source_run_ids=replay_from,
    )
    return deps.Subject(kind="flow", id=copy.copy_flow_id, copy_id=copy.id)


async def _agent_copy(db: AsyncSession, agent_id: uuid.UUID, *, created_for: str, author_agent_id: Any) -> deps.Subject:
    copy_id, copy_agent_id = await deps.create_agent_copy(
        db,
        agent_id=agent_id,
        created_for=created_for,
        created_by_agent_id=author_agent_id,
        persona=None,
        source_trace_ids=[],
    )
    return deps.Subject(kind="agent", id=copy_agent_id, copy_id=copy_id)


def _change(experiment: Any) -> dict[str, Any]:
    variant = experiment.variant or {}
    return {
        "base_content": "",
        "proposed_content": "",
        "change": {"sandbox_flow_id": variant.get("sandbox_flow_id"), "graph": variant.get("graph") or {}},
        # The flow version the variant was measured on: approving it over a newer one is refused (stale).
        "base_version_number": experiment.base_version,
    }


class FlowGraphKind:
    """``flow_repair`` / ``flow_optimization``: a new graph for an existing flow."""

    def __init__(self, change_type: str) -> None:
        self.change_type = change_type

    async def prepare_draft(
        self, db: AsyncSession, *, finding: Any, target_id: uuid.UUID, payload: dict[str, Any]
    ) -> DraftSpec:
        if finding.target_type != "flow" or finding.target_id != target_id:
            raise ExperimentError("A flow repair or optimization targets the finding's flow.")
        target = await db.get(Flow, target_id)
        if target is None:
            raise ExperimentError("The finding's flow no longer exists.")
        _copy, sandbox = await _sandbox_flow(db, payload, source_id=target.id)
        return DraftSpec(
            target_type="flow",
            subject_agent_id=None,
            base_text="",
            base_version=target.version_number,
            variant_text="",
            variant={"sandbox_flow_id": str(sandbox.id), "graph": dict(sandbox.graph or {})},
            summary=_summary(payload),
        )

    async def check_stale(self, db: AsyncSession, experiment: Any) -> bool:
        target = await db.get(Flow, experiment.target_id)
        return target is None or target.version_number != experiment.base_version

    async def dataset_target(self, db: AsyncSession, experiment: Any) -> tuple[str, uuid.UUID] | None:
        return ("flow", experiment.target_id)

    async def subjects(
        self, db: AsyncSession, experiment: Any, *, created_for: str, author_agent_id: Any
    ) -> tuple[deps.Subject, deps.Subject]:
        baseline = await _flow_copy(
            db, source_id=experiment.target_id, graph=None, created_for=created_for, author_agent_id=author_agent_id
        )
        variant = await _flow_copy(
            db,
            source_id=experiment.target_id,
            graph=(experiment.variant or {}).get("graph"),
            created_for=created_for,
            author_agent_id=author_agent_id,
        )
        return baseline, variant

    def proposal_content(self, experiment: Any) -> dict[str, Any]:
        return _change(experiment)


class NewFlowKind:
    """``new_flow``: a prototype automating what an agent's users repeat, measured against the agent."""

    change_type = "new_flow"

    async def prepare_draft(
        self, db: AsyncSession, *, finding: Any, target_id: uuid.UUID, payload: dict[str, Any]
    ) -> DraftSpec:
        if finding.target_type != "agent" or finding.target_id != target_id:
            raise ExperimentError("A new flow targets the agent whose usage it automates (the finding's agent).")
        _copy, sandbox = await _sandbox_flow(db, payload, source_id=None)
        return DraftSpec(
            target_type="agent",
            subject_agent_id=target_id,
            base_text="",
            base_version=None,
            variant_text="",
            variant={"sandbox_flow_id": str(sandbox.id), "graph": dict(sandbox.graph or {})},
            summary=_summary(payload),
        )

    async def check_stale(self, db: AsyncSession, experiment: Any) -> bool:
        from app.models.agent import Agent

        return await db.get(Agent, experiment.subject_agent_id) is None

    async def dataset_target(self, db: AsyncSession, experiment: Any) -> tuple[str, uuid.UUID] | None:
        return ("agent", experiment.subject_agent_id)

    async def subjects(
        self, db: AsyncSession, experiment: Any, *, created_for: str, author_agent_id: Any
    ) -> tuple[deps.Subject, deps.Subject]:
        baseline = await _agent_copy(
            db, experiment.subject_agent_id, created_for=created_for, author_agent_id=author_agent_id
        )
        variant = await _flow_copy(
            db,
            source_id=None,
            graph=(experiment.variant or {}).get("graph"),
            created_for=created_for,
            author_agent_id=author_agent_id,
        )
        return baseline, variant

    def proposal_content(self, experiment: Any) -> dict[str, Any]:
        return _change(experiment)


class AgentToFlowKind(NewFlowKind):
    """``agent_to_flow``: a flow reproducing an assignment, measured against its agent on the same cases."""

    change_type = "agent_to_flow"

    async def prepare_draft(
        self, db: AsyncSession, *, finding: Any, target_id: uuid.UUID, payload: dict[str, Any]
    ) -> DraftSpec:
        from app.models.agent import Agent
        from app.models.assignment import Assignment
        from app.services.health.flow_lab import trigger_takeover_refusal

        if finding.target_type != "assignment" or finding.target_id != target_id:
            raise ExperimentError("An agent-to-flow replacement targets the finding's assignment.")
        assignment = await db.get(Assignment, target_id)
        if assignment is None:
            raise ExperimentError("The finding's assignment no longer exists.")
        _copy, sandbox = await _sandbox_flow(db, payload, source_id=None)
        refusal = trigger_takeover_refusal(assignment, await db.get(Agent, assignment.agent_id), sandbox.graph)
        if refusal:
            raise ExperimentError(f"This flow cannot take the assignment's trigger over: {refusal}.")
        return DraftSpec(
            target_type="assignment",
            subject_agent_id=assignment.agent_id,
            base_text="",
            base_version=None,
            variant_text="",
            variant={"sandbox_flow_id": str(sandbox.id), "graph": dict(sandbox.graph or {})},
            summary=_summary(payload),
        )

    async def check_stale(self, db: AsyncSession, experiment: Any) -> bool:
        from app.models.agent import Agent
        from app.models.assignment import Assignment
        from app.services.health.flow_lab import trigger_takeover_refusal

        assignment = await db.get(Assignment, experiment.target_id)
        if assignment is None:
            return True
        agent = await db.get(Agent, assignment.agent_id)
        return trigger_takeover_refusal(assignment, agent, (experiment.variant or {}).get("graph")) is not None


async def flow_target_access(db: AsyncSession, reader: Any, flow_id: uuid.UUID) -> quality_access.AgentAccess | None:
    """Who reads a flow finding: the Flow Lab's own rule (``flow_lab_access``), from level 6's reader."""
    from app.services.health.consent import content_readable
    from app.services.health.flow_lab_access import FlowLabCaller, flow_reachable

    flow = await db.get(Flow, flow_id)
    if flow is None:
        return None
    if reader.is_concierge:
        caller = FlowLabCaller(concierge=reader.principal, user=None)
    elif reader.user is not None:
        from app.api.v1.helpers import get_user_group_ids

        caller = FlowLabCaller(
            concierge=None, user=reader.user, group_ids=tuple(await get_user_group_ids(db, reader.user.id))
        )
    else:
        return None
    if not await flow_reachable(db, caller, flow):
        return None
    content = await content_readable(db, flow_id=flow.id) if reader.is_concierge else True
    return quality_access.AgentAccess(agent=None, content=bool(content))


def register_flow_experiment_kinds() -> None:
    """Register the four flow kinds and the flow finding rule with level 6 (idempotent)."""
    for change_type in ("flow_repair", "flow_optimization"):
        register_experiment_kind(change_type, FlowGraphKind(change_type))
    register_experiment_kind("new_flow", NewFlowKind())
    register_experiment_kind("agent_to_flow", AgentToFlowKind())
    quality_access.register_target_access("flow", flow_target_access)


register_flow_experiment_kinds()
