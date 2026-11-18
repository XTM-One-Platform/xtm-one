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

"""The one access rule of the Flow Lab, on every path (#8260).

Every Flow Lab tool, its platform MCP exposure and the proposal appliers
resolve their targets here, on the database row, by id or by exact name, so
the rule cannot drift between surfaces:

* **The Concierge** (a run of a Concierge agent, level 1's
  ``current_concierge()``, when the agent calling the tool is the
  principal's own; an agent its run executes inline reads as its runner):
  a flow or an assignment's agent in the supervision scope; content
  (trigger payloads, outputs) only where ``consent.content_readable``
  admits it, audited when it is returned. No admin bypass: the Concierge
  has no user row. Agents of the fleet itself are never in scope (level 1).
* **A person** (the platform MCP server): a flow they may manage
  (``can_manage``), an assignment they may modify (``writable_assignment_error``);
  a person who manages a flow reads its run journal, as on the flow's page.
* **Sandbox copies** (level 2): reachable by the Concierge only, which owns
  them; a person never writes one through these tools.

A target the caller may not reach answers exactly as a missing one, so the
refusal is not an oracle on what exists.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.flow import Flow

NOT_FOUND = "not found, or not within your reach"
#: Rows sharing one name a lookup by name reads, a bound on a pathological name.
MAX_NAMESAKES = 200


@dataclass(frozen=True, slots=True)
class FlowLabCaller:
    """Who is asking: the Concierge principal, or a person."""

    concierge: Any | None
    user: Any | None
    group_ids: tuple[str, ...] = ()

    @property
    def is_concierge(self) -> bool:
        return self.concierge is not None


async def resolve_caller(
    db: AsyncSession, user_id: str | uuid.UUID | None, *, agent_id: str | None = None
) -> FlowLabCaller | None:
    """The caller of a tool call: the Concierge when *agent_id*, the agent calling the tool, is its
    principal's own (level 3's ``calling_concierge``), else the user row."""
    from app.services.builtin_tools.improvements import calling_concierge

    principal = calling_concierge(agent_id)
    if principal is not None:
        return FlowLabCaller(concierge=principal, user=None)
    if user_id is None:
        return None
    from app.api.v1.helpers import get_user_group_ids
    from app.models.user import User

    try:
        uid = uuid.UUID(str(user_id))
    except ValueError:
        return None
    user = await db.scalar(select(User).where(User.id == uid, User.is_active.is_(True)))
    if user is None:
        return None
    return FlowLabCaller(concierge=None, user=user, group_ids=tuple(await get_user_group_ids(db, user.id)))


def parse_id(ref: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(ref).strip())
    except ValueError:
        return None


async def rows_by_ref(db: AsyncSession, model: Any, ref: str) -> list[Any]:
    """Rows of *model* named by *ref*: its id, else its exact (case-insensitive) name."""
    ref = (ref or "").strip()
    if not ref:
        return []
    rid = parse_id(ref)
    if rid is not None:
        row = await db.get(model, rid)
        return [row] if row is not None else []
    # Not a first few: the reach rule runs after this read, so a tight cut
    # could drop the one row the caller may reach.
    return list(
        (
            await db.execute(
                select(model).where(func.lower(model.name) == ref.lower()).order_by(model.id).limit(MAX_NAMESAKES)
            )
        ).scalars()
    )


async def one_reachable(db: AsyncSession, model: Any, ref: str, reachable: Any) -> Any | None:
    """The one row *ref* names that *reachable* admits; ``None`` when none or several."""
    found = None
    for row in await rows_by_ref(db, model, ref):
        if await reachable(row):
            if found is not None:
                return None
            found = row
    return found


def is_sandbox_flow(flow: Flow) -> bool:
    from app.services.health.sandbox.copies import is_sandbox_copy

    return is_sandbox_copy(flow)


async def flow_reachable(db: AsyncSession, caller: FlowLabCaller, flow: Flow) -> bool:
    """Whether *caller* may diagnose and experiment on *flow* (read the row; metrics)."""
    sandbox = is_sandbox_flow(flow)
    if caller.is_concierge:
        if sandbox:
            return True
        from app.services.health.consent import in_supervision_scope

        return await in_supervision_scope(db, "flow", flow.id)
    if sandbox:
        return False
    from app.api.v1.helpers import can_manage

    return can_manage(caller.user, flow, list(caller.group_ids))


async def resolve_flow(db: AsyncSession, caller: FlowLabCaller, ref: str) -> Flow | None:
    """The one flow *ref* names that *caller* may reach; ``None`` when none or several."""
    return await one_reachable(db, Flow, ref, lambda flow: flow_reachable(db, caller, flow))


async def resolve_sandbox_flow(db: AsyncSession, caller: FlowLabCaller, ref: str) -> Flow | None:
    """A sandbox flow the Flow Lab authored, for the Concierge only.

    Never another copy: an experiment's arms and an approved proposal's copy
    (``created_for`` ``proposal:...``) are what was measured or approved, so
    no tool may rewrite or re-run them.
    """
    from app.services.health.sandbox.copies import sandbox_copy_of

    if not caller.is_concierge:
        return None
    rid = parse_id(ref)
    flow = await db.get(Flow, rid) if rid else None
    if flow is None or not is_sandbox_flow(flow):
        return None
    copy = await sandbox_copy_of(db, flow_id=flow.id)
    if copy is None or not is_flow_lab_copy(copy.created_for):
        return None
    return flow


async def assignment_reachable(db: AsyncSession, caller: FlowLabCaller, assignment: Assignment) -> bool:
    agent = await db.get(Agent, assignment.agent_id)
    if agent is None:
        return False
    if caller.is_concierge:
        from app.services.health.consent import in_supervision_scope

        return await in_supervision_scope(db, "agent", agent.id)
    from app.api.v1.helpers import writable_assignment_error

    return (
        writable_assignment_error(
            assignment, agent, caller.user, list(caller.group_ids), action="experiment on", allow_builtin=True
        )
        is None
    )


async def resolve_assignment(db: AsyncSession, caller: FlowLabCaller, ref: str) -> Assignment | None:
    return await one_reachable(db, Assignment, ref, lambda assignment: assignment_reachable(db, caller, assignment))


async def _shadowed_origin(db: AsyncSession, run: Any) -> tuple[Any, Any] | None:
    """``(agent_id, user_id)`` of the assignment run a shadow flow run copied, else ``None``."""
    from sqlalchemy import select

    from app.models.assignment import AssignmentRun
    from app.models.flow_shadow import FlowShadowPair

    row = (
        await db.execute(
            select(AssignmentRun.agent_id, AssignmentRun.user_id)
            .join(FlowShadowPair, FlowShadowPair.assignment_run_id == AssignmentRun.id)
            .where(FlowShadowPair.flow_run_id == run.id)
        )
    ).first()
    return (row.agent_id, row.user_id) if row is not None else None


async def flow_run_content_allowed(db: AsyncSession, caller: FlowLabCaller, flow: Flow, run: Any) -> bool:
    """Whether *caller* may read *run*'s content (trigger payload, outputs), at read time.

    A shadow run carries the event of the assignment run it copied: its content
    follows that run's consent (the agent's opt-in, the user's opt-out), not
    the sandbox flow's.
    """
    if not caller.is_concierge:
        # ``flow_reachable`` already holds: a person who manages the flow reads its journal.
        return True
    from app.services.health.consent import content_readable

    if is_sandbox_flow(flow):
        origin = await _shadowed_origin(db, run)
        if origin is None:
            # An experiment run: its cases are level 4's consented dataset entries.
            return True
        return await content_readable(db, agent_id=origin[0], user_id=origin[1])
    return await content_readable(db, flow_id=flow.id, user_id=run.user_id)


async def audit_flow_run_read(db: AsyncSession, caller: FlowLabCaller, flow: Flow, run: Any) -> None:
    """Level 1's audit entry for content the Concierge is about to hand to a model."""
    if not caller.is_concierge:
        return
    from app.services.health.consent import audit_content_read

    if is_sandbox_flow(flow):
        origin = await _shadowed_origin(db, run)
        if origin is not None:
            await audit_content_read(db, kind="flow_run", ref_id=run.id, agent_id=origin[0], user_id=origin[1])
        return
    await audit_content_read(db, kind="flow_run", ref_id=run.id, flow_id=flow.id, user_id=run.user_id)


async def assignment_run_content_allowed(
    db: AsyncSession, caller: FlowLabCaller, assignment: Assignment, run: Any
) -> bool:
    if not caller.is_concierge:
        return True
    from app.services.health.consent import content_readable

    return await content_readable(db, agent_id=assignment.agent_id, user_id=run.user_id)


async def resolve_agent(db: AsyncSession, caller: FlowLabCaller, ref: str) -> Agent | None:
    """The one agent *ref* names whose usage *caller* may mine (new-flow mode)."""

    async def reachable(agent: Agent) -> bool:
        if caller.is_concierge:
            from app.services.health.consent import in_supervision_scope

            return await in_supervision_scope(db, "agent", agent.id)
        from app.api.v1.helpers import can_manage

        return can_manage(caller.user, agent, list(caller.group_ids))

    return await one_reachable(db, Agent, ref, reachable)


async def resolve_run(db: AsyncSession, caller: FlowLabCaller, kind: str, ref: str) -> Any | None:
    """A flow or assignment run whose content *caller* may compare; ``None`` otherwise."""
    from app.models.assignment import AssignmentRun
    from app.models.flow_run import FlowRun

    rid = parse_id(ref)
    if rid is None:
        return None
    if kind == "flow_run":
        run = await db.get(FlowRun, rid)
        flow = await db.get(Flow, run.flow_id) if run else None
        if flow is None or not await flow_reachable(db, caller, flow):
            return None
        return run if await flow_run_content_allowed(db, caller, flow, run) else None
    if kind == "assignment_run":
        run = await db.get(AssignmentRun, rid)
        assignment = await db.get(Assignment, run.assignment_id) if run and run.assignment_id else None
        if assignment is None or not await assignment_reachable(db, caller, assignment):
            return None
        return run if await assignment_run_content_allowed(db, caller, assignment, run) else None
    return None


# ── Sandbox reach ──────────────────────────────────────────────────────────

#: How a prototype copy remembers the agent whose reach it keeps
#: (``SandboxCopy.created_for``, level 2).
BASIS_AGENT_PREFIX = "flow_lab:agent:"
#: ``created_for`` of a Flow Lab copy of a flow (repair, optimize).
FLOW_LAB_COPY = "flow_lab"
#: ``created_for`` of an experiment's arms (level 4, ``experiments.run``).
EXPERIMENT_COPY_PREFIX = "concierge_experiment:"
#: ``created_for`` of the copy an approved flow proposal shadows, then switches over to.
PROPOSAL_COPY_PREFIX = "proposal:"
#: What only the server writes in ``created_for``: a caller's text never starts
#: with one, so what these name is never a model's word (``is_server_marker``).
SERVER_COPY_MARKERS = (FLOW_LAB_COPY, EXPERIMENT_COPY_PREFIX, PROPOSAL_COPY_PREFIX)


def graph_reference_ids(value: Any) -> set[str]:
    """Every UUID a value holds anywhere in its strings (a ``{{var_id.UUID}}`` included)."""
    found: set[str] = set()

    def walk(item: Any) -> None:
        if isinstance(item, str):
            for token in item.replace("{", " ").replace("}", " ").replace(".", " ").split():
                parsed = parse_id(token)
                if parsed is not None:
                    found.add(str(parsed))
        elif isinstance(item, dict):
            for nested in item.values():
                walk(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                walk(nested)

    walk(value)
    return found


def node_config_ids(graph: Any) -> set[str]:
    """The ids a graph's node configs name: the entities its runs reach."""
    nodes = graph.get("nodes") if isinstance(graph, dict) else None
    return graph_reference_ids([node.get("config") for node in nodes or [] if isinstance(node, dict)])


async def sandbox_reach(db: AsyncSession, *, source_flow: Flow | None, basis_agent_id: Any) -> set[str]:
    """What a sandbox graph may name: what its source flow, or the agent whose usage it automates, names.

    A run reads every id a node names as stored, so a copy that named more
    would reach integrations, agents and tools beyond its source (PRD 9).
    """
    from sqlalchemy.types import JSON

    reach: set[str] = set()
    if source_flow is not None:
        reach |= node_config_ids(source_flow.graph)
    agent = await db.get(Agent, parse_id(str(basis_agent_id))) if basis_agent_id else None
    if agent is not None:
        reach.add(str(agent.id))
        reach |= graph_reference_ids(
            [getattr(agent, column.key) for column in Agent.__table__.columns if isinstance(column.type, JSON)]
        )
    return reach


def is_flow_lab_copy(created_for: str | None) -> bool:
    """Whether a sandbox copy is one the Flow Lab authored (``create_sandbox_flow``)."""
    return created_for == FLOW_LAB_COPY or bool(created_for and created_for.startswith(BASIS_AGENT_PREFIX))


def basis_agent_of(created_for: str | None) -> uuid.UUID | None:
    if created_for and created_for.startswith(BASIS_AGENT_PREFIX):
        return parse_id(created_for.removeprefix(BASIS_AGENT_PREFIX))
    return None


def is_server_marker(created_for: str | None) -> bool:
    """Whether *created_for* is one only the server writes (:data:`SERVER_COPY_MARKERS`)."""
    text = (created_for or "").strip().lower()
    return any(text.startswith(marker) for marker in SERVER_COPY_MARKERS)


async def copy_subject(db: AsyncSession, copy: Any) -> tuple[str, uuid.UUID] | None:
    """``(source_type, id)`` a sandbox copy stands for, or ``None``: its source,
    else the agent whose usage it automates as the server recorded it (a Flow
    Lab prototype's basis, a new-flow experiment's agent).

    A copy's runs follow it: they run as its owner, within its scope and its
    consent, and name nothing it does not (PRD 9). A copy standing for nothing
    has nobody to run as and no reach, so it does not run on the Concierge's
    behalf.
    """
    if copy is None:
        return None
    if copy.source_id is not None:
        return copy.source_type, copy.source_id
    basis = basis_agent_of(copy.created_for)
    created_for = copy.created_for or ""
    if basis is None and created_for.startswith(EXPERIMENT_COPY_PREFIX):
        from app.models.health_experiment import ConciergeExperiment

        experiment_id = parse_id(created_for.removeprefix(EXPERIMENT_COPY_PREFIX))
        experiment = await db.get(ConciergeExperiment, experiment_id) if experiment_id else None
        basis = experiment.subject_agent_id if experiment is not None else None
    return ("agent", basis) if basis is not None else None
