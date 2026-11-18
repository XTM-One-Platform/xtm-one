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

"""The four flow proposal types of the Flow Lab: how an approved one is applied, watched, rolled back (#8260).

Level 3 owns the proposal lifecycle and its approval rule; this module is the
``ProposalHandler`` of each flow type (PRD section 6.9, decisions B12, B14):

| Type | Target | Applying | Watch | Rollback |
|---|---|---|---|---|
| ``flow_repair``, ``flow_optimization`` | the flow | its graph becomes the approved one, as a new version | live failure rate and cost against the flow's own runs before | a new version with the graph it had |
| ``new_flow`` | the agent whose usage it automates | a new **draft** flow: its manager activates it | live runs once activated | the draft is archived, unless someone edited or activated it |
| ``agent_to_flow`` | the assignment | a shadow on its live triggers (``flow_shadow``), then the switch-over | ``flow_shadow`` owns it | assignment resumed, flow deactivated |

The approved change is the graph stored on the proposal (``change["graph"]``),
filed from the sandbox copy it was measured on (``change["sandbox_flow_id"]``):
what the approver reads is what gets applied, whatever happens to the copy
afterwards. Every apply and rollback is audited with the proposal id; who may
approve is level 3's rule, read through ``manage_error`` here.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.flow import FLOW_STATUS_ACTIVE, FLOW_STATUS_ARCHIVED, FLOW_STATUS_DRAFT, Flow
from app.services.health.flow_lab_access import PROPOSAL_COPY_PREFIX
from app.services.improvement_proposals import (
    Applied,
    LiveVerdict,
    ProposalError,
    TargetInfo,
    register_live_watcher,
    register_proposal_type,
)

logger = logging.getLogger(__name__)

FLOW_GRAPH_TYPES = ("flow_repair", "flow_optimization")
_GENERIC_INVALID = "This proposal's change cannot be applied as it stands."


def _graph_of(spec_or_proposal: Any) -> dict:
    change = getattr(spec_or_proposal, "change", None) or {}
    graph = change.get("graph") if isinstance(change, dict) else None
    return graph if isinstance(graph, dict) else {}


def _sandbox_id(spec_or_proposal: Any) -> uuid.UUID | None:
    change = getattr(spec_or_proposal, "change", None) or {}
    try:
        return uuid.UUID(str(change.get("sandbox_flow_id")))
    except (TypeError, ValueError):
        return None


def _valid_graph(graph: dict) -> dict:
    """*graph* laid out and normalized, or ``ProposalError(422)`` with the reason in the logs."""
    from app.services.flow_graph import auto_layout, normalize_graph, validate_graph

    laid_out = auto_layout(graph)
    result = validate_graph(laid_out)
    if not result.is_valid:
        logger.info("Flow proposal refused: %d graph errors", len(result.errors))
        raise ProposalError(422, _GENERIC_INVALID)
    return normalize_graph(laid_out)


async def check_writer_references(
    db: AsyncSession,
    approver: Any,
    graph: dict,
    *,
    previous_graph: dict | None,
    flow_owner_id: Any,
    is_company_managed: bool,
) -> None:
    """The flow builder's save-time rules, the approver as the writer; ``ProposalError(422)`` otherwise.

    A run reads every id a node names as stored (``flow_references``), so the
    save is the only guard: the approver may only put on a flow the agents,
    integrations, tools and other entities they may use themselves, and a
    Budget node above the platform ceiling only as an administrator - the
    rules of ``api/v1/flows`` for a person editing the canvas. A graph the
    Platform Health drafted gets no more reach than the person approving it.
    """
    from fastapi import HTTPException

    from app.api.v1.helpers import assert_budget_within_ceiling, get_user_group_ids
    from app.services.flow_references import (
        check_integration_references,
        check_source_references,
        check_three_tier_references,
        check_tool_references,
    )

    group_ids = await get_user_group_ids(db, approver.id)
    issues = await check_tool_references(
        db,
        approver,
        graph=graph,
        user_group_ids=group_ids,
        previous_graph=previous_graph,
        flow_owner_id=flow_owner_id,
        is_company_managed=is_company_managed,
    )
    issues += await check_integration_references(
        db, approver, graph=graph, previous_graph=previous_graph, flow_owner_id=flow_owner_id
    )
    issues += await check_source_references(
        db, approver, graph=graph, previous_graph=previous_graph, flow_owner_id=flow_owner_id
    )
    issues += await check_three_tier_references(db, approver, graph=graph, previous_graph=previous_graph)
    refused = [issue for issue in issues if issue.get("severity", "error") == "error"]
    stored = {
        node.get("id"): node.get("config")
        for node in (previous_graph or {}).get("nodes") or []
        if isinstance(node, dict) and node.get("kind") == "capability.budget"
    }
    for node in graph.get("nodes") or []:
        if isinstance(node, dict) and node.get("kind") == "capability.budget":
            try:
                assert_budget_within_ceiling(
                    node.get("config"), previous=stored.get(node.get("id")), user=approver, what="this flow"
                )
            except HTTPException:
                refused.append({"code": "budget.above_ceiling", "node_id": node.get("id")})
    if refused:
        logger.info(
            "Flow proposal refused at approval: %s",
            sorted({str(issue.get("code")) for issue in refused}),
        )
        raise ProposalError(
            422,
            "This flow names agents, integrations or tools you may not use, or a budget above the platform "
            "ceiling: it cannot be applied as it stands.",
        )


async def _sandbox_copy(db: AsyncSession, spec: Any) -> Any:
    from app.services.health.sandbox.copies import sandbox_copy_of

    sandbox_id = _sandbox_id(spec)
    copy = await sandbox_copy_of(db, flow_id=sandbox_id) if sandbox_id else None
    if copy is None:
        raise ProposalError(422, _GENERIC_INVALID)
    return copy


def _manage_error(user: Any, entity: Any, user_group_ids: list[str] | None, action: str, kind: str) -> str | None:
    from app.api.v1.helpers import can_manage

    if can_manage(user, entity, user_group_ids):
        return None
    if entity.is_company_managed:
        return f"Only admins can {action} company-managed {kind}s."
    return f"You don't have permission to {action} this {kind}."


async def _system_audit(
    db: AsyncSession, *, anchor: Any, action: str, entity: Any, entity_type: str, summary: str, details: dict
) -> None:
    from app.services.audit import log_system_audit
    from app.services.health.identity import HEALTH_ACTOR

    if anchor is None:
        logger.warning("%s of %s %s has no user to anchor its audit entry", action, entity_type, entity.id)
        return
    await log_system_audit(
        db,
        actor=HEALTH_ACTOR,
        anchor_user_id=anchor,
        action=action,
        entity_type=entity_type,
        entity_id=entity.id,
        entity_name=entity.name,
        summary=summary,
        details=details,
    )


# ── flow_repair, flow_optimization ─────────────────────────────────────────


class FlowGraphHandler:
    """A new version of an existing flow's graph."""

    target_type = "flow"
    subject = "flow graph"
    owns_watch = False

    def __init__(self, proposal_type: str) -> None:
        self.proposal_type = proposal_type
        self.noun = "flow repair" if proposal_type == "flow_repair" else "flow optimization"

    async def load_target(self, db: AsyncSession, target_id: uuid.UUID, *, lock: bool) -> Flow | None:
        query = select(Flow).where(Flow.id == target_id)
        return await db.scalar(query.with_for_update() if lock else query)

    def describe(self, target: Flow) -> TargetInfo:
        return TargetInfo(name=target.name, agent_id=None, is_company_managed=target.is_company_managed, managed=target)

    def manage_error(self, target: Flow, user: Any, user_group_ids: list[str] | None, *, action: str) -> str | None:
        return _manage_error(user, target, user_group_ids, action, "flow")

    async def validate(self, db: AsyncSession, spec: Any, target: Flow) -> None:
        from app.services.health.sandbox.copies import is_sandbox_copy

        copy = await _sandbox_copy(db, spec)
        # The variant was measured on a copy of THIS flow, never on another one.
        if is_sandbox_copy(target) or copy.source_type != "flow" or copy.source_id != target.id:
            raise ProposalError(422, _GENERIC_INVALID)
        _valid_graph(_graph_of(spec))

    async def stale_error(self, db: AsyncSession, proposal: Any, target: Flow) -> str | None:
        if proposal.base_version_number is not None and target.version_number != proposal.base_version_number:
            return "The flow changed since this variant was measured. Reject it and let the Flow Lab draft again."
        return None

    async def apply(self, db: AsyncSession, proposal: Any, target: Flow, approver: Any) -> Applied:
        from app.services.audit import log_audit
        from app.services.flow_versioning import create_flow_version
        from app.services.health.flow_switch_over import flow_live_metrics

        graph = _valid_graph(_graph_of(proposal))
        await check_writer_references(
            db,
            approver,
            graph,
            previous_graph=target.graph or {},
            flow_owner_id=target.user_id,
            is_company_managed=bool(target.is_company_managed),
        )
        now = datetime.now(UTC)
        baseline = await flow_live_metrics(db, target.id, since=now - timedelta(days=30))
        proposal.apply_state = {
            "base_graph": target.graph or {},
            "base_version": target.version_number,
            "baseline": baseline.as_dict(),
        }
        target.graph = graph
        target.version_number = (target.version_number or 1) + 1
        await create_flow_version(
            db,
            flow_id=target.id,
            graph=graph,
            author_id=approver.id,
            commit_message=f"Platform Health proposal {proposal.id} ({self.proposal_type}), approved",
        )
        await log_audit(
            db,
            user=approver,
            action="update",
            entity_type="flow",
            entity_id=target.id,
            entity_name=target.name,
            summary=f"Applied the approved {self.proposal_type.replace('_', ' ')} of flow '{target.name}'",
            details={"improvement_proposal_id": str(proposal.id), "version": target.version_number},
        )
        return Applied(version_number=target.version_number, status="watching", candidate_run_id=None)

    async def is_still_applied(self, db: AsyncSession, proposal: Any, target: Flow) -> bool:
        return target.version_number == proposal.applied_version_number

    async def rollback(self, db: AsyncSession, proposal: Any, target: Flow, reason: str) -> int | None:
        from app.services.flow_versioning import create_flow_version

        base = (proposal.apply_state or {}).get("base_graph")
        if not isinstance(base, dict):
            logger.warning("Flow proposal %s has no base graph to roll back to", proposal.id)
            return None
        target.graph = base
        target.version_number = (target.version_number or 1) + 1
        await create_flow_version(
            db,
            flow_id=target.id,
            graph=base,
            author_id=None,
            commit_message=f"Automatic rollback of Platform Health proposal {proposal.id}"[:200],
        )
        await _system_audit(
            db,
            anchor=proposal.decided_by_user_id or target.user_id,
            action="rollback",
            entity=target,
            entity_type="flow",
            summary=f"Rolled back flow '{target.name}' after the approved change regressed",
            details={"improvement_proposal_id": str(proposal.id), "version": target.version_number, "reason": reason},
        )
        return target.version_number


async def flow_graph_watch(db: AsyncSession, proposal: Any) -> LiveVerdict:
    """Live watch of a repaired or optimized flow against its own runs before the change."""
    from app.services.health.flow_switch_over import flow_live_metrics, watch_decision

    since = proposal.applied_at or datetime.now(UTC)
    current = await flow_live_metrics(db, proposal.target_id, since=since)
    regressed, note = watch_decision(current, (proposal.apply_state or {}).get("baseline"))
    return LiveVerdict(regressed=regressed, note=note)


# ── new_flow ───────────────────────────────────────────────────────────────


class NewFlowHandler:
    """A new draft flow automating what an agent's users repeat by hand."""

    proposal_type = "new_flow"
    target_type = "agent"
    noun = "new flow"
    subject = "flow"
    owns_watch = False

    async def load_target(self, db: AsyncSession, target_id: uuid.UUID, *, lock: bool) -> Agent | None:
        query = select(Agent).where(Agent.id == target_id)
        return await db.scalar(query.with_for_update() if lock else query)

    def describe(self, target: Agent) -> TargetInfo:
        return TargetInfo(
            name=target.name, agent_id=target.id, is_company_managed=target.is_company_managed, managed=target
        )

    def manage_error(self, target: Agent, user: Any, user_group_ids: list[str] | None, *, action: str) -> str | None:
        return _manage_error(user, target, user_group_ids, action, "agent")

    async def validate(self, db: AsyncSession, spec: Any, target: Agent) -> None:
        copy = await _sandbox_copy(db, spec)
        if copy.source_id is not None:
            raise ProposalError(422, _GENERIC_INVALID)
        _valid_graph(_graph_of(spec))

    async def stale_error(self, db: AsyncSession, proposal: Any, target: Agent) -> str | None:
        return None

    async def apply(self, db: AsyncSession, proposal: Any, target: Agent, approver: Any) -> Applied:
        from app.services.audit import log_audit
        from app.services.flow_versioning import create_flow_version

        graph = _valid_graph(_graph_of(proposal))
        await check_writer_references(
            db,
            approver,
            graph,
            previous_graph=None,
            flow_owner_id=None,
            is_company_managed=bool(target.is_company_managed),
        )
        flow = Flow(
            id=uuid.uuid4(),
            user_id=approver.id,
            name=(str((proposal.change or {}).get("name") or "") or f"{target.name} automation")[:200],
            description=(proposal.summary or "")[:2000],
            # Draft: a new automation starts when its manager activates it.
            status=FLOW_STATUS_DRAFT,
            graph=graph,
            version_number=1,
            is_company_managed=bool(target.is_company_managed),
        )
        db.add(flow)
        await db.flush()
        await create_flow_version(
            db,
            flow_id=flow.id,
            graph=graph,
            author_id=approver.id,
            commit_message=f"Platform Health proposal {proposal.id}",
        )
        proposal.apply_state = {"flow_id": str(flow.id)}
        await log_audit(
            db,
            user=approver,
            action="create",
            entity_type="flow",
            entity_id=flow.id,
            entity_name=flow.name,
            summary=f"Created the draft flow '{flow.name}' from an approved Platform Health proposal",
            details={"improvement_proposal_id": str(proposal.id), "agent_id": str(target.id)},
        )
        return Applied(version_number=1, status="watching", candidate_run_id=None)

    async def _flow(self, db: AsyncSession, proposal: Any) -> Flow | None:
        flow_id = (proposal.apply_state or {}).get("flow_id")
        return await db.get(Flow, uuid.UUID(flow_id)) if flow_id else None

    async def is_still_applied(self, db: AsyncSession, proposal: Any, target: Agent) -> bool:
        # The proposal only created a draft: activating it is the manager's
        # decision, so a regression of an activated flow is theirs to keep.
        flow = await self._flow(db, proposal)
        return flow is not None and flow.status == FLOW_STATUS_DRAFT and flow.version_number == 1

    async def rollback(self, db: AsyncSession, proposal: Any, target: Agent, reason: str) -> int | None:
        flow = await self._flow(db, proposal)
        if flow is None or flow.status != FLOW_STATUS_DRAFT:
            return None
        flow.status = FLOW_STATUS_ARCHIVED
        await _system_audit(
            db,
            anchor=proposal.decided_by_user_id or flow.user_id,
            action="flow.deactivate",
            entity=flow,
            entity_type="flow",
            summary=f"Archived flow '{flow.name}' after it regressed",
            details={"improvement_proposal_id": str(proposal.id), "reason": reason},
        )
        return flow.version_number


async def new_flow_watch(db: AsyncSession, proposal: Any) -> LiveVerdict:
    """Live watch of a new flow: no verdict while its manager has not activated it."""
    from app.services.health.flow_switch_over import flow_live_metrics, watch_decision

    flow_id = (proposal.apply_state or {}).get("flow_id")
    flow = await db.get(Flow, uuid.UUID(flow_id)) if flow_id else None
    if flow is None or flow.status != FLOW_STATUS_ACTIVE:
        return LiveVerdict(regressed=None, note="The flow is not active, so it has no live runs to watch.")
    current = await flow_live_metrics(db, flow.id, since=proposal.applied_at or datetime.now(UTC))
    regressed, note = watch_decision(current, None)
    return LiveVerdict(regressed=regressed, note=note)


# ── agent_to_flow ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AssignmentTarget:
    """An assignment with the agent its manage rule reads (``writable_assignment_error``)."""

    assignment: Assignment
    agent: Agent

    @property
    def id(self) -> uuid.UUID:
        return self.assignment.id

    @property
    def name(self) -> str:
        return self.assignment.name


class AgentToFlowHandler:
    """A flow replacing an assignment: shadow, switch-over, watch (B12), driven by ``flow_shadow``."""

    proposal_type = "agent_to_flow"
    target_type = "assignment"
    noun = "flow replacement"
    subject = "assignment"
    owns_watch = True

    async def load_target(self, db: AsyncSession, target_id: uuid.UUID, *, lock: bool) -> AssignmentTarget | None:
        query = select(Assignment).where(Assignment.id == target_id)
        assignment = await db.scalar(query.with_for_update() if lock else query)
        agent = await db.get(Agent, assignment.agent_id) if assignment else None
        return AssignmentTarget(assignment, agent) if agent is not None else None

    def describe(self, target: AssignmentTarget) -> TargetInfo:
        return TargetInfo(
            name=target.name,
            agent_id=target.agent.id,
            is_company_managed=bool(target.assignment.is_company_managed),
            # Who manages an assignment is who manages its agent (writable_assignment_error).
            managed=target.agent,
        )

    def manage_error(
        self, target: AssignmentTarget, user: Any, user_group_ids: list[str] | None, *, action: str
    ) -> str | None:
        from app.api.v1.helpers import writable_assignment_error

        return writable_assignment_error(target.assignment, target.agent, user, user_group_ids, action=action)

    async def validate(self, db: AsyncSession, spec: Any, target: AssignmentTarget) -> None:
        from app.services.health.flow_lab import trigger_takeover_refusal

        copy = await _sandbox_copy(db, spec)
        if copy.source_id is not None:
            raise ProposalError(422, _GENERIC_INVALID)
        refusal = trigger_takeover_refusal(target.assignment, target.agent, _valid_graph(_graph_of(spec)))
        if refusal:
            logger.info("agent_to_flow proposal on assignment %s refused: %s", target.id, refusal)
            raise ProposalError(422, f"This flow cannot take the assignment's trigger over: {refusal}.")

    async def stale_error(self, db: AsyncSession, proposal: Any, target: AssignmentTarget) -> str | None:
        from app.services.health.flow_lab import trigger_takeover_refusal

        if not target.assignment.enabled:
            return "The assignment is paused: a flow cannot take over a trigger that does not fire."
        refusal = trigger_takeover_refusal(target.assignment, target.agent, _graph_of(proposal))
        if refusal:
            return f"The assignment changed since this replacement was measured: {refusal}."
        return None

    async def apply(self, db: AsyncSession, proposal: Any, target: AssignmentTarget, approver: Any) -> Applied:
        from app.models.flow_shadow import SHADOW_PURPOSE_SWITCH_OVER
        from app.services.health.flow_shadow import ShadowError, start_shadow
        from app.services.health.sandbox.copies import SandboxOverrides, create_sandbox_copy
        from app.services.trigger_integration_access import assignment_acting_user_id

        graph = _valid_graph(_graph_of(proposal))
        # Checked now, the approver as the writer: the switch-over creates the
        # production flow from exactly this graph days later, with no person.
        await check_writer_references(
            db,
            approver,
            graph,
            previous_graph=None,
            flow_owner_id=assignment_acting_user_id(target.assignment, target.agent),
            is_company_managed=bool(target.assignment.is_company_managed),
        )

        # A fresh copy of exactly the approved graph: what runs in shadow, then
        # in production, is what the approver read, whatever became of the
        # Flow Lab's working copy. No recording: every write is simulated.
        row = await create_sandbox_copy(
            db,
            source_type="flow",
            source_id=None,
            created_for=f"{PROPOSAL_COPY_PREFIX}{proposal.id}",
            overrides=SandboxOverrides(graph=graph),
        )
        copy = await db.get(Flow, row.copy_flow_id)
        try:
            shadow = await start_shadow(
                db,
                assignment=target.assignment,
                flow=copy,
                purpose=SHADOW_PURPOSE_SWITCH_OVER,
                proposal_id=proposal.id,
                started_by_user_id=approver.id,
            )
        except ShadowError as exc:
            raise ProposalError(409, str(exc)) from None
        proposal.apply_state = {"flow_shadow_id": str(shadow.id)}
        proposal.shadow_until = shadow.ends_at
        return Applied(version_number=None, status="shadow", candidate_run_id=None)

    async def _shadow(self, db: AsyncSession, proposal: Any) -> Any:
        from app.models.flow_shadow import FlowShadow

        shadow_id = (proposal.apply_state or {}).get("flow_shadow_id")
        return await db.get(FlowShadow, uuid.UUID(shadow_id)) if shadow_id else None

    async def is_still_applied(self, db: AsyncSession, proposal: Any, target: AssignmentTarget) -> bool:
        from app.services.health.flow_switch_over import human_change

        shadow = await self._shadow(db, proposal)
        flow = await db.get(Flow, shadow.production_flow_id) if shadow and shadow.production_flow_id else None
        if flow is None:
            return False
        return human_change(shadow, target.assignment, flow, (shadow.verdict or {}).get("applied_version")) is None

    async def rollback(self, db: AsyncSession, proposal: Any, target: AssignmentTarget, reason: str) -> int | None:
        from app.models.flow_shadow import SHADOW_STATUS_CANCELLED, SHADOW_STATUS_RUNNING, SHADOW_STATUS_WATCHING
        from app.services.health.flow_switch_over import rollback_switch_over

        shadow = await self._shadow(db, proposal)
        if shadow is None:
            return None
        now = datetime.now(UTC)
        if shadow.status == SHADOW_STATUS_WATCHING and shadow.production_flow_id:
            flow = await db.get(Flow, shadow.production_flow_id)
            if flow is not None:
                await rollback_switch_over(db, shadow, target.assignment, flow, reason=reason, now=now)
        elif shadow.status == SHADOW_STATUS_RUNNING:
            shadow.status = SHADOW_STATUS_CANCELLED
            shadow.settled_at = now
            shadow.note = reason[:1000]
        return None


def register_flow_proposal_types() -> None:
    """Register the four handlers and the two live watchers with level 3 (idempotent per process)."""
    for proposal_type in FLOW_GRAPH_TYPES:
        register_proposal_type(proposal_type, FlowGraphHandler(proposal_type))
        register_live_watcher(proposal_type, flow_graph_watch)
    register_proposal_type("new_flow", NewFlowHandler())
    register_live_watcher("new_flow", new_flow_watch)
    register_proposal_type("agent_to_flow", AgentToFlowHandler())


register_flow_proposal_types()
