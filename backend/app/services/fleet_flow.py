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

"""Fleet data flow: what starts each member, and what each member writes to.

The read behind the fleet Graph tab's "Data flow" view (#9829). The agent
view (:func:`app.services.fleet_views.build_fleet_graph`) draws who hands over
to whom; this one draws the lanes around it, derived from live configuration
only, never invented:

* **Sources** (left): one node per integration that triggers a member (its
  events as chips), one ``platform`` node for the platform events (each event a
  chip), and one node per other trigger kind (schedule, webhook, work item
  inbox, manual).
* **Agents** (middle): the fleet's home members, in tree order; the configured
  handovers between them, as the agent view draws them.
* **Outputs** (right): one node per integration a member is bound to, the edge
  ``write`` when a write tool is reachable and ``read`` when the binding is
  read-only or its allow list names reads only; a ``work_items`` node for a
  member with the coordination tools. A member that reads an integration it is
  not bound to (the Platform Health module checkers, wired at run start from a
  module registration, #101 S15f) draws a ``read`` edge to it too
  (``implicit_reads``).

Every assignment of a home member is exactly one ``trigger`` edge, disabled
ones included (drawn greyed with the reason). Nothing here starts a run or
calls a model: the view is pure configuration, so it works with the AI off and
in Community Edition. A webhook URL, a secret, a prompt or an id held in a
trigger never leaves the server; an agent or integration the caller cannot see
keeps its node with an empty name, like the agent view.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import safe_uuid
from app.models.agent import Agent
from app.models.assignment import Assignment, read_trigger_config
from app.models.fleet import Fleet
from app.models.integration import Integration
from app.services.fleet_doc_generation import describe_cron
from app.services.fleet_views import DEFAULT_HANDOVER_TYPE, home_member_ids
from app.services.fleets import fleet_members
from app.services.integration_read_only import (
    integration_tool_access,
    read_only_integration_ids,
    template_read_only_providers,
)

#: ``FlowNode.lane`` values, left to right.
LANE_SOURCE = "source"
LANE_AGENT = "agent"
LANE_OUTPUT = "output"

#: ``FlowEdge.kind`` values.
EDGE_TRIGGER = "trigger"
EDGE_HANDOVER = "handover"
EDGE_WRITE = "write"
EDGE_READ = "read"

#: Source node ids of the trigger kinds that have no integration.
SOURCE_PLATFORM = "source:platform"
SOURCE_SCHEDULE = "source:schedule"
SOURCE_WEBHOOK = "source:webhook"
SOURCE_WORK_ITEM = "source:work_item"
SOURCE_MANUAL = "source:manual"
_KIND_SOURCES: dict[str, str] = {
    "platform_event": SOURCE_PLATFORM,
    "schedule": SOURCE_SCHEDULE,
    "webhook": SOURCE_WEBHOOK,
    "work_item": SOURCE_WORK_ITEM,
    "manual": SOURCE_MANUAL,
}
#: The output node of the coordination tools (work items, shared state).
OUTPUT_WORK_ITEMS = "output:work_items"
#: The built-in tool group that files work items and writes shared state.
COORDINATION_GROUP = "coordination"
#: Characters a filter or gate is cut to (one line on the edge).
LINE_CHARS = 160

#: ``reason`` values of a greyed node or edge.
REASON_DISABLED = "disabled"
REASON_AGENT_DISABLED = "agent disabled"
REASON_NOT_CONNECTED = "not connected"
REASON_PAUSED = "paused"


@dataclass(frozen=True)
class FlowAgent:
    """What the flow needs of a home member, loaded once."""

    id: uuid.UUID
    name: str
    icon: str | None
    enabled: bool
    visible: bool
    integration_bindings: Mapping[str, Any] | None = None
    integration_template_bindings: Sequence[str] | None = None
    integration_allowed_tools: Mapping[str, Any] | None = None
    integration_read_only: Mapping[str, Any] | None = None
    integration_template_read_only: Sequence[str] | None = None
    builtin_tool_ids: Sequence[str] | None = None
    handover_agent_ids: Sequence[uuid.UUID] = ()
    handover_types: Mapping[str, str] | None = None


@dataclass(frozen=True)
class FlowAssignment:
    """One assignment of a home member, its trigger as stored."""

    id: uuid.UUID
    agent_id: uuid.UUID
    name: str
    trigger_type: str
    trigger_config: Mapping[str, Any]
    enabled: bool
    paused_reason: str | None = None
    has_activation_prompt: bool = False
    activation_tool: str | None = None


@dataclass(frozen=True)
class FlowIntegration:
    id: uuid.UUID
    provider: str
    name: str
    status: str
    visible: bool


@dataclass
class FlowNode:
    id: str
    lane: str
    kind: str
    name: str = ""
    icon: str | None = None
    provider: str | None = None
    integration_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    #: Event names (integration or platform) drawn as chips, sorted.
    events: list[str] = field(default_factory=list)
    enabled: bool = True
    reason: str | None = None
    visible: bool = True


@dataclass
class FlowEdge:
    source: str
    target: str
    kind: str
    #: ``trigger`` edges: the assignment, its event(s), filter and gate in one line.
    assignment_id: uuid.UUID | None = None
    assignment_name: str = ""
    events: list[str] = field(default_factory=list)
    detail: str = ""
    #: ``schedule`` triggers: the cron and its timezone, worded by the client in the reader's language.
    cron: str = ""
    timezone: str = ""
    filter: str = ""
    gate: str = ""
    #: ``handover`` edges: the handover type.
    type: str | None = None
    enabled: bool = True
    reason: str | None = None


@dataclass
class FleetFlow:
    nodes: list[FlowNode] = field(default_factory=list)
    edges: list[FlowEdge] = field(default_factory=list)


def agent_node_id(agent_id: uuid.UUID) -> str:
    return f"agent:{agent_id}"


def _line(value: object) -> str:
    """A filter or condition in one line, keys and values as stored, cut to :data:`LINE_CHARS`."""
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, Mapping):
        text = ", ".join(f"{k} {_line(v) if isinstance(v, Mapping) else v}" for k, v in value.items())
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= LINE_CHARS else text[: LINE_CHARS - 1] + "…"


def _names(raw: object) -> list[str]:
    values = [raw] if isinstance(raw, str) else list(raw) if isinstance(raw, list) else []
    return sorted({v.strip() for v in values if isinstance(v, str) and v.strip()})


def _ids(raw: object) -> list[uuid.UUID]:
    values = raw if isinstance(raw, list) else [raw]
    return [p for v in values if v is not None and (p := safe_uuid(str(v))) is not None]


def _gate(assignment: FlowAssignment) -> str:
    parts = []
    if assignment.activation_tool:
        parts.append(f"tool check {assignment.activation_tool}")
    if assignment.has_activation_prompt:
        parts.append("activation question")
    return ", ".join(parts)


def _binding_access(
    agent: FlowAgent, provider: str, integration_id: uuid.UUID | None, read_only: frozenset[str]
) -> str:
    """``read`` or ``write``: whether a write tool of this binding can be reached."""
    if integration_id is not None and str(integration_id) in read_only:
        return EDGE_READ
    allowed = agent.integration_allowed_tools or {}
    scoped = allowed.get(f"{provider}@{integration_id}") if integration_id is not None else None
    tools = scoped if scoped is not None else allowed.get(provider)
    if not isinstance(tools, list):
        # Missing or null: every tool of the integration, writes included.
        return EDGE_WRITE
    if any(isinstance(t, str) and integration_tool_access(t) == "write" for t in tools):
        return EDGE_WRITE
    return EDGE_READ


def assemble_fleet_flow(
    *,
    agents: Sequence[FlowAgent],
    assignments: Iterable[FlowAssignment],
    integrations: Mapping[uuid.UUID, FlowIntegration],
    implicit_reads: Mapping[uuid.UUID, Sequence[tuple[str, uuid.UUID]]] | None = None,
) -> FleetFlow:
    """The flow of *agents* (home members, tree order) from their configuration.

    *implicit_reads* maps a member to the ``(provider, integration id)`` it reads
    without a binding (always ``read``, never a write). Pure: the same inputs draw
    the same nodes and edges in the same order.
    """
    flow = FleetFlow()
    by_id = {a.id: a for a in agents}
    sources: dict[str, FlowNode] = {}
    outputs: dict[str, FlowNode] = {}
    trigger_edges: list[FlowEdge] = []

    def integration_node(lane: str, provider: str, iid: uuid.UUID | None, store: dict[str, FlowNode]) -> FlowNode:
        key = f"{lane}:integration:{provider}" + (f":{iid}" if iid else "")
        node = store.get(key)
        if node is None:
            facts = integrations.get(iid) if iid else None
            node = FlowNode(id=key, lane=lane, kind="integration", provider=provider, integration_id=iid)
            if facts is not None:
                node.name = facts.name if facts.visible else ""
                node.visible = facts.visible
                if facts.status != "connected":
                    node.enabled, node.reason = False, REASON_NOT_CONNECTED
            store[key] = node
        return node

    for assignment in sorted(assignments, key=lambda a: (str(a.agent_id), a.name, str(a.id))):
        agent = by_id.get(assignment.agent_id)
        if agent is None:
            continue
        kind = assignment.trigger_type or "manual"
        config = read_trigger_config(kind, dict(assignment.trigger_config or {}))
        config = config if isinstance(config, Mapping) else {}
        events: list[str] = []
        detail = ""
        cron = timezone = ""
        if kind == "integration_event":
            provider = str(config.get("provider") or "unknown")
            iid = safe_uuid(str(config.get("integration_id"))) if config.get("integration_id") else None
            node = integration_node(LANE_SOURCE, provider, iid, sources)
            events = _names(config.get("event")) or _names(config.get("events"))
        else:
            source_id = _KIND_SOURCES.get(kind, f"source:{kind}")
            node = sources.get(source_id) or sources.setdefault(
                source_id, FlowNode(id=source_id, lane=LANE_SOURCE, kind=kind)
            )
            if kind == "platform_event":
                events = _names(config.get("event"))
            elif kind == "schedule":
                detail = describe_cron(config.get("cron"), config.get("timezone"))
                cron = str(config.get("cron") or "").strip()
                timezone = str(config.get("timezone") or "").strip()
            elif kind == "work_item":
                prefix = config.get("correlation_prefix")
                detail = f"{prefix}*" if isinstance(prefix, str) and prefix else ""
                owner = safe_uuid(str(config.get("owner_agent_id"))) if config.get("owner_agent_id") else None
                if owner is not None and owner != agent.id:
                    detail = f"{detail} (queue of another agent)".strip()
        node.events = sorted(set(node.events) | set(events))
        enabled, reason = True, None
        if not agent.enabled:
            enabled, reason = False, REASON_AGENT_DISABLED
        elif not assignment.enabled:
            enabled, reason = False, REASON_PAUSED if assignment.paused_reason else REASON_DISABLED
        trigger_edges.append(
            FlowEdge(
                source=node.id,
                target=agent_node_id(agent.id),
                kind=EDGE_TRIGGER,
                assignment_id=assignment.id,
                assignment_name=assignment.name if agent.visible else "",
                events=events,
                detail=detail if agent.visible else "",
                cron=cron if agent.visible else "",
                timezone=timezone if agent.visible else "",
                filter=_line(config.get("filter")) if agent.visible else "",
                gate=_gate(assignment) if agent.visible else "",
                enabled=enabled,
                reason=reason,
            )
        )

    write_edges: list[FlowEdge] = []
    for agent in agents:
        target_agent = agent_node_id(agent.id)
        read_only = read_only_integration_ids(agent)
        seen: set[str] = set()
        for provider, raw in sorted((agent.integration_bindings or {}).items()):
            for iid in _ids(raw):
                node = integration_node(LANE_OUTPUT, str(provider), iid, outputs)
                if node.id in seen:
                    continue
                seen.add(node.id)
                access = _binding_access(agent, str(provider), iid, read_only)
                write_edges.append(
                    FlowEdge(
                        source=target_agent,
                        target=node.id,
                        kind=access,
                        enabled=agent.enabled,
                        reason=None if agent.enabled else REASON_AGENT_DISABLED,
                    )
                )
        for provider, iid in sorted((implicit_reads or {}).get(agent.id, ()), key=lambda r: (r[0], str(r[1]))):
            node = integration_node(LANE_OUTPUT, provider, iid, outputs)
            if node.id in seen:
                continue
            seen.add(node.id)
            write_edges.append(
                FlowEdge(
                    source=target_agent,
                    target=node.id,
                    kind=EDGE_READ,
                    enabled=agent.enabled,
                    reason=None if agent.enabled else REASON_AGENT_DISABLED,
                )
            )
        bound_providers = {str(p) for p in (agent.integration_bindings or {})}
        for provider in sorted({str(p) for p in (agent.integration_template_bindings or []) if p}):
            if provider in bound_providers:
                continue
            node = integration_node(LANE_OUTPUT, provider, None, outputs)
            access = (
                EDGE_READ
                if provider in template_read_only_providers(agent)
                else _binding_access(agent, provider, None, frozenset())
            )
            write_edges.append(
                FlowEdge(
                    source=target_agent,
                    target=node.id,
                    kind=access,
                    enabled=agent.enabled,
                    reason=None if agent.enabled else REASON_AGENT_DISABLED,
                )
            )
        if COORDINATION_GROUP in (agent.builtin_tool_ids or []):
            outputs.setdefault(OUTPUT_WORK_ITEMS, FlowNode(id=OUTPUT_WORK_ITEMS, lane=LANE_OUTPUT, kind="work_items"))
            write_edges.append(
                FlowEdge(
                    source=target_agent,
                    target=OUTPUT_WORK_ITEMS,
                    kind=EDGE_WRITE,
                    enabled=agent.enabled,
                    reason=None if agent.enabled else REASON_AGENT_DISABLED,
                )
            )

    handover_edges: list[FlowEdge] = []
    for agent in agents:
        for target in agent.handover_agent_ids:
            if target in by_id and target != agent.id:
                raw_type = (agent.handover_types or {}).get(str(target))
                handover_edges.append(
                    FlowEdge(
                        source=agent_node_id(agent.id),
                        target=agent_node_id(target),
                        kind=EDGE_HANDOVER,
                        type=raw_type if isinstance(raw_type, str) and raw_type else DEFAULT_HANDOVER_TYPE,
                    )
                )

    def order(node: FlowNode) -> tuple[str, str, str]:
        return (node.kind, node.provider or "", node.id)

    flow.nodes.extend(sorted(sources.values(), key=order))
    flow.nodes.extend(
        FlowNode(
            id=agent_node_id(a.id),
            lane=LANE_AGENT,
            kind="agent",
            name=a.name if a.visible else "",
            icon=a.icon if a.visible else None,
            agent_id=a.id,
            enabled=a.enabled,
            reason=None if a.enabled else REASON_DISABLED,
            visible=a.visible,
        )
        for a in agents
    )
    flow.nodes.extend(sorted(outputs.values(), key=order))
    flow.edges.extend(trigger_edges)
    flow.edges.extend(handover_edges)
    flow.edges.extend(write_edges)
    return flow


async def build_fleet_flow(
    db: AsyncSession,
    fleet: Fleet,
    *,
    visible_agent_clause: ColumnElement[bool],
    visible_integration_clause: ColumnElement[bool],
) -> FleetFlow:
    """Load what :func:`assemble_fleet_flow` needs, in a fixed number of queries.

    The visibility clauses are the caller's ``user_visible_filter`` on
    ``Agent`` / ``Integration``: they decide which names are shown, never which
    nodes exist.
    """
    rows = await fleet_members(db, fleet.id)
    ids = home_member_ids(rows, fleet.orchestrator_agent_id)
    if not ids:
        return FleetFlow()
    agent_rows = {a.id: a for a in (await db.execute(select(Agent).where(Agent.id.in_(ids)))).scalars().all()}
    visible = set((await db.execute(select(Agent.id).where(Agent.id.in_(ids), visible_agent_clause))).scalars().all())
    agents = [
        FlowAgent(
            id=a.id,
            name=a.name,
            icon=a.icon,
            enabled=bool(a.enabled) if a.enabled is not None else True,
            visible=a.id in visible,
            integration_bindings=a.integration_bindings if isinstance(a.integration_bindings, dict) else None,
            integration_template_bindings=(
                a.integration_template_bindings if isinstance(a.integration_template_bindings, list) else None
            ),
            integration_allowed_tools=(
                a.integration_allowed_tools if isinstance(a.integration_allowed_tools, dict) else None
            ),
            integration_read_only=a.integration_read_only if isinstance(a.integration_read_only, dict) else None,
            integration_template_read_only=(
                a.integration_template_read_only if isinstance(a.integration_template_read_only, list) else None
            ),
            builtin_tool_ids=a.builtin_tool_ids if isinstance(a.builtin_tool_ids, list) else None,
            handover_agent_ids=_ids(a.handover_agent_ids or []),
            handover_types=a.handover_types if isinstance(a.handover_types, dict) else None,
        )
        for aid in ids
        if (a := agent_rows.get(aid)) is not None
    ]
    found = (
        await db.execute(
            select(
                Assignment.id,
                Assignment.agent_id,
                Assignment.name,
                Assignment.trigger_type,
                Assignment.trigger_config,
                Assignment.enabled,
                Assignment.paused_reason,
                Assignment.activation_prompt,
                Assignment.activation_check,
            ).where(Assignment.agent_id.in_(ids))
        )
    ).all()
    assignments = [
        FlowAssignment(
            id=aid,
            agent_id=agent_id,
            name=name,
            trigger_type=str(trigger_type or "manual"),
            trigger_config=config if isinstance(config, dict) else {},
            enabled=bool(enabled),
            paused_reason=paused,
            has_activation_prompt=bool(prompt and str(prompt).strip()),
            activation_tool=(str(check.get("tool")) if isinstance(check, dict) and check.get("tool") else None),
        )
        for aid, agent_id, name, trigger_type, config, enabled, paused, prompt, check in found
    ]
    implicit_reads: dict[uuid.UUID, list[tuple[str, uuid.UUID]]] = {}
    from app.services.health.read_only import is_health_fleet

    if is_health_fleet(fleet):
        from app.services.health.module_analyst import checker_reads

        reads = await checker_reads(db)
        for agent in agents:
            row = agent_rows.get(agent.id)
            role = getattr(row, "health_role", None) if row is not None else None
            if row is not None and row.is_company_managed and role in reads:
                implicit_reads[agent.id] = reads[role]
    integration_ids: set[uuid.UUID] = set()
    for found_reads in implicit_reads.values():
        integration_ids.update(iid for _provider, iid in found_reads)
    for agent in agents:
        for raw in (agent.integration_bindings or {}).values():
            integration_ids.update(_ids(raw))
    for item in assignments:
        if item.trigger_type == "integration_event" and item.trigger_config.get("integration_id"):
            integration_ids.update(_ids(item.trigger_config.get("integration_id")))
    integrations: dict[uuid.UUID, FlowIntegration] = {}
    if integration_ids:
        seen = set(
            (
                await db.execute(
                    select(Integration.id).where(Integration.id.in_(integration_ids), visible_integration_clause)
                )
            )
            .scalars()
            .all()
        )
        for iid, provider, name, status in (
            await db.execute(
                select(Integration.id, Integration.provider, Integration.name, Integration.status).where(
                    Integration.id.in_(integration_ids)
                )
            )
        ).all():
            integrations[iid] = FlowIntegration(
                id=iid, provider=provider, name=name, status=str(status or ""), visible=iid in seen
            )
    return assemble_fleet_flow(
        agents=agents, assignments=assignments, integrations=integrations, implicit_reads=implicit_reads
    )


__all__ = [
    "EDGE_HANDOVER",
    "EDGE_READ",
    "EDGE_TRIGGER",
    "EDGE_WRITE",
    "LANE_AGENT",
    "LANE_OUTPUT",
    "LANE_SOURCE",
    "FleetFlow",
    "FlowAgent",
    "FlowAssignment",
    "FlowEdge",
    "FlowIntegration",
    "FlowNode",
    "assemble_fleet_flow",
    "build_fleet_flow",
]
