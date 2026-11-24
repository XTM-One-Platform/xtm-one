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

"""Fleet reads: graph, hierarchy, activity and health.

Everything here READS; the rules a write follows live in
:mod:`app.services.fleets`. The fleet's views (the graph, the declared tree, the
schedule, the activity feed, the health strip) are computed from data the
platform already records, scoped to the fleet's home members:

* **Graph.** The handover graph around the fleet: its home members, the
  ``connected`` agents and fleets it calls, and every agent outside it that a
  configured handover reaches or comes from. Each crossing edge says whether a
  ``connected`` row governs it (an ungoverned one is drawn dashed, with an "add
  as connected agent" action) and whether the target's fleet exposes the target
  (a handover written before an exposure change is kept and flagged, never
  broken). Handovers stay free: the graph reports them, it filters none.
* **Hierarchy.** The DECLARED tree, never inferred from handovers: the
  orchestrator is the root, a member with no parent reports to it, a parent
  that left the fleet lifts its reports one level (``SET NULL``). Ordered by
  membership age then id, so two reads draw the same tree.
* **Activity and health.** Runs follow :func:`app.api.v1.helpers.run_visible_filter`
  (own + company-managed, the one run scope of the platform); delegations are
  the caller's own, like ``GET /chat/delegations``; work items follow the work
  item visibility through :func:`app.services.work_item.open_work_item_counts`.
  The fleet's coordination is its workspace, so the activity counts the open
  items of that workspace and the health counts the items each member owns.

An agent the caller cannot see keeps its node with an empty name (a
placeholder): sharing a fleet gives nothing on the agents themselves.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.fleet import FLEET_ROLE_CONNECTED, FLEET_ROLE_MEMBER, Fleet, FleetMember
from app.services.fleets import (
    HomeFleet,
    effective_member_settings,
    exposed_agent_ids,
    fleet_members,
    home_fleets_of,
)
from app.services.run_outcome import (
    HEALTH_RECENT_RUNS,
    MemberVerdict,
    RunFact,
    member_verdict,
    run_outcome,
)

#: The type a handover target has when the agent's ``handover_types`` names
#: none for it (``Agent.handover_types``).
DEFAULT_HANDOVER_TYPE = "delegate"

#: ``GraphNode.kind`` values.
NODE_MEMBER = "member"
NODE_CONNECTED = "connected"
NODE_EXTERNAL = "external"
NODE_FLEET = "fleet"

#: ``GraphEdge.scope`` values.
EDGE_INTERNAL = "internal"
EDGE_OUTBOUND = "outbound"
EDGE_INBOUND = "inbound"

#: ``MemberHealth.status`` values, worst first.
HEALTH_STATUSES: tuple[str, ...] = ("failing", "disabled", "stale", "idle", "ok")


def _uuids(raw: Iterable[object] | None) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    for value in raw or []:
        try:
            out.append(value if isinstance(value, uuid.UUID) else uuid.UUID(str(value)))
        except (TypeError, ValueError):
            continue
    return out


#: ``?fleet=none``: the agents in no fleet (the playground), as on ``GET /agents``.
FLEET_FILTER_NONE = "none"


def fleet_scope_clause(agent_column, fleet: str | uuid.UUID | None):
    """Narrow an agent-id column to a fleet's home members, or to the playground.

    The one ``?fleet=`` filter of the schedule reads (``GET /assignments/schedule``,
    ``GET /work-items/scheduled``) and of ``GET /agents``: a fleet id keeps its
    home members, ``none`` keeps the agents in no fleet (and a row no agent
    owns, which is in no fleet either), ``None`` adds nothing. An id that is
    not a UUID raises ``ValueError``; the routes parse it first and answer 422.
    """
    if fleet is None:
        return None
    homes = select(FleetMember.agent_id).where(
        FleetMember.role == FLEET_ROLE_MEMBER, FleetMember.agent_id.isnot(None)
    )
    if fleet == FLEET_FILTER_NONE:
        return or_(agent_column.is_(None), agent_column.notin_(homes))
    fleet_id = fleet if isinstance(fleet, uuid.UUID) else uuid.UUID(str(fleet))
    return agent_column.in_(homes.where(FleetMember.fleet_id == fleet_id))


def hosted_home_member_ids(workspace_id: uuid.UUID):
    """The current home members of the fleets *workspace_id* hosts, as a subquery.

    Resolved when the query that embeds it runs, so a row follows the agents
    in the fleet now: the one membership the workspace's tabs
    and its counters share.
    """
    return (
        select(FleetMember.agent_id)
        .join(Fleet, Fleet.id == FleetMember.fleet_id)
        .where(
            Fleet.workspace_id == workspace_id,
            FleetMember.role == FLEET_ROLE_MEMBER,
            FleetMember.agent_id.isnot(None),
        )
    )


async def home_workspace_of(db: AsyncSession, agent_id: uuid.UUID | None) -> uuid.UUID | None:
    """The workspace hosting *agent_id*'s home fleet, or None.

    The workspace a home member's new work is filed in when its creator names
    none. A connected agent, an agent in no fleet and a member of an archived
    fleet have none: work is never filed into a fleet that is gone.
    """
    if agent_id is None:
        return None
    return (
        await db.execute(
            select(Fleet.workspace_id)
            .join(FleetMember, FleetMember.fleet_id == Fleet.id)
            .where(
                FleetMember.agent_id == agent_id,
                FleetMember.role == FLEET_ROLE_MEMBER,
                Fleet.is_archived.is_(False),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


def workspace_scope_clause(workspace_id: uuid.UUID, workspace_column, *agent_columns):
    """What belongs to a workspace: filed in it, or produced by its fleet.

    A row matches when *workspace_column* names the workspace, or when one of
    *agent_columns* (an owner, a creator) names a current home member of a
    fleet the workspace hosts. Membership is a subquery, resolved when the
    query runs, so a row follows the agents in the fleet now: the same
    population as :func:`app.services.work_item.fleet_work_item_clause`, for
    every table the workspace's Operations tab lists. A workspace hosting no
    fleet matches what is filed in it only. Visibility is the caller's: this
    clause narrows, it never widens.
    """
    homes = hosted_home_member_ids(workspace_id)
    return or_(workspace_column == workspace_id, *(column.in_(homes) for column in agent_columns))


def fleet_node_id(fleet_id: uuid.UUID) -> str:
    """A connected fleet's node id: never mistaken for an agent's."""
    return f"fleet:{fleet_id}"


# ── Hierarchy ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TreeRow:
    """One home member in the declared tree, depth-first order."""

    member_id: uuid.UUID
    agent_id: uuid.UUID
    parent_agent_id: uuid.UUID | None
    depth: int
    is_orchestrator: bool


def fleet_tree(orchestrator_agent_id: uuid.UUID | None, rows: Sequence[FleetMember]) -> list[TreeRow]:
    """The declared hierarchy of the home members, depth first, deterministic.

    A member with no parent, or whose parent is not a home member of the fleet,
    reports to the orchestrator; with no orchestrator it is a root. Siblings
    keep the order of *rows* (membership age, then id: :func:`fleet_members`).
    A parent chain that loops (the write path refuses one; a row edited by hand
    could hold one) is cut where it closes, so every member appears once.
    """
    homes = [r for r in rows if r.role == FLEET_ROLE_MEMBER and r.agent_id is not None]
    by_member = {r.id: r for r in homes}
    orchestrator = next((r for r in homes if r.agent_id == orchestrator_agent_id), None)

    def parent_of(row: FleetMember) -> FleetMember | None:
        if orchestrator is not None and row.id == orchestrator.id:
            return None
        parent = by_member.get(row.parent_member_id) if row.parent_member_id else None
        if parent is None or parent.id == row.id:
            return orchestrator
        return parent

    children: dict[uuid.UUID | None, list[FleetMember]] = {}
    for row in homes:
        parent = parent_of(row)
        children.setdefault(parent.id if parent else None, []).append(row)

    out: list[TreeRow] = []
    placed: set[uuid.UUID] = set()

    def walk(row: FleetMember, parent: FleetMember | None, depth: int) -> None:
        if row.id in placed:
            return
        placed.add(row.id)
        out.append(
            TreeRow(
                member_id=row.id,
                agent_id=row.agent_id,  # type: ignore[arg-type]
                parent_agent_id=parent.agent_id if parent else None,
                depth=depth,
                is_orchestrator=orchestrator is not None and row.id == orchestrator.id,
            )
        )
        for child in children.get(row.id, []):
            walk(child, row, depth + 1)

    roots = ([orchestrator] if orchestrator is not None else []) + [
        r for r in children.get(None, []) if r is not orchestrator
    ]
    for root in roots:
        walk(root, None, 0)
    # What a loop left unplaced becomes a root, in the rows' order.
    for row in homes:
        walk(row, None, 0)
    return out


# ── Graph ────────────────────────────────────────────────────────────────────


@dataclass
class GraphNode:
    id: str
    kind: str
    name: str = ""
    icon: str | None = None
    agent_id: uuid.UUID | None = None
    #: The node's HOME fleet (``None``: the playground). For a fleet node, the fleet.
    fleet_id: uuid.UUID | None = None
    fleet_name: str = ""
    member_id: uuid.UUID | None = None
    parent_agent_id: uuid.UUID | None = None
    is_orchestrator: bool = False
    is_exposed: bool = False
    enabled: bool = True
    #: False when the caller cannot see the agent (or fleet): drawn as a placeholder.
    visible: bool = True
    #: A connected row's instruction and context inclusion.
    collaboration_instruction: str = ""
    context_inclusion: str | None = None
    #: A connected fleet's entry agent (its orchestrator), resolved at read time.
    entry_agent_id: uuid.UUID | None = None


@dataclass
class GraphEdge:
    source: str
    target: str
    type: str
    scope: str
    #: Crossing edges only: a ``connected`` row covers it (drawn solid).
    governed: bool = True
    #: False when the target is a non-exposed member of another fleet than the
    #: source's: kept as configured, flagged (:func:`fleet_boundary_violations`).
    allowed: bool = True
    source_fleet_id: uuid.UUID | None = None
    target_fleet_id: uuid.UUID | None = None


@dataclass
class AgentFacts:
    """What the graph needs of an agent, loaded once."""

    id: uuid.UUID
    name: str
    icon: str | None
    enabled: bool
    handover_agent_ids: list[uuid.UUID]
    handover_types: Mapping[str, str]
    visible: bool


@dataclass
class FleetGraph:
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)


def _handover_type(agent: AgentFacts, target: uuid.UUID) -> str:
    value = agent.handover_types.get(str(target)) if agent.handover_types else None
    return value if isinstance(value, str) and value else DEFAULT_HANDOVER_TYPE


def assemble_fleet_graph(
    *,
    fleet: Fleet,
    rows: Sequence[FleetMember],
    agents: Mapping[uuid.UUID, AgentFacts],
    homes: Mapping[uuid.UUID, HomeFleet],
    inbound_sources: Sequence[uuid.UUID],
    connected_fleets: Mapping[uuid.UUID, tuple[str, uuid.UUID | None, bool]],
    governed_inbound: set[tuple[uuid.UUID, uuid.UUID]],
) -> FleetGraph:
    """The graph from loaded facts (no I/O: the unit tests drive it directly).

    *connected_fleets* maps a connected fleet id to ``(name, entry agent id,
    visible)``; *governed_inbound* holds the ``(source, target)`` pairs a
    connected row of the SOURCE's home fleet covers (it names the target agent,
    or this whole fleet).
    """
    graph = FleetGraph()
    tree = {r.agent_id: r for r in fleet_tree(fleet.orchestrator_agent_id, rows)}
    exposed = set(exposed_agent_ids(fleet, rows))
    home_ids = [r.agent_id for r in rows if r.role == FLEET_ROLE_MEMBER and r.agent_id is not None]
    home_set = set(home_ids)
    connected_agents = {
        r.agent_id: r for r in rows if r.role == FLEET_ROLE_CONNECTED and r.agent_id is not None
    }
    connected_fleet_rows = {
        r.target_fleet_id: r for r in rows if r.role == FLEET_ROLE_CONNECTED and r.target_fleet_id is not None
    }
    seen: set[str] = set()

    def agent_node(agent_id: uuid.UUID, kind: str) -> None:
        key = str(agent_id)
        if key in seen:
            return
        seen.add(key)
        facts = agents.get(agent_id)
        home = homes.get(agent_id)
        node = GraphNode(
            id=key,
            kind=kind,
            name=facts.name if facts and facts.visible else "",
            icon=facts.icon if facts and facts.visible else None,
            agent_id=agent_id,
            fleet_id=home.fleet_id if home else None,
            fleet_name=home.fleet_name if home else "",
            enabled=facts.enabled if facts else True,
            visible=bool(facts and facts.visible),
            is_exposed=bool(home and home.is_exposed),
            is_orchestrator=bool(home and home.is_orchestrator),
        )
        if kind == NODE_MEMBER:
            row = tree.get(agent_id)
            node.member_id = row.member_id if row else None
            node.parent_agent_id = row.parent_agent_id if row else None
            node.is_orchestrator = agent_id == fleet.orchestrator_agent_id
            node.is_exposed = agent_id in exposed
            node.fleet_id, node.fleet_name = fleet.id, fleet.name
        elif kind == NODE_CONNECTED:
            link = connected_agents[agent_id]
            node.member_id = link.id
            settings = effective_member_settings(fleet, link)
            node.collaboration_instruction = settings.collaboration_instruction
            node.context_inclusion = settings.context_inclusion
        graph.nodes.append(node)

    # Home members in tree order, then connected agents and fleets, then the rest.
    for agent_id in [*tree, *[a for a in home_ids if a not in tree]]:
        agent_node(agent_id, NODE_MEMBER)
    for agent_id in connected_agents:
        if agent_id not in home_set:
            agent_node(agent_id, NODE_CONNECTED)
    for fleet_id, row in connected_fleet_rows.items():
        name, entry, visible = connected_fleets.get(fleet_id, ("", None, False))
        graph.nodes.append(
            GraphNode(
                id=fleet_node_id(fleet_id),
                kind=NODE_FLEET,
                name=name if visible else "",
                fleet_id=fleet_id,
                fleet_name=name if visible else "",
                member_id=row.id,
                visible=visible,
                collaboration_instruction=effective_member_settings(fleet, row).collaboration_instruction,
                context_inclusion=effective_member_settings(fleet, row).context_inclusion,
                entry_agent_id=entry,
            )
        )
        seen.add(fleet_node_id(fleet_id))
    governed_fleets = set(connected_fleet_rows)

    # Outbound and internal: the members' own handovers.
    for source_id in home_ids:
        source = agents.get(source_id)
        if source is None:
            continue
        for target in source.handover_agent_ids:
            if target == source_id:
                continue
            target_home = homes.get(target)
            if target in home_set:
                graph.edges.append(
                    GraphEdge(
                        source=str(source_id),
                        target=str(target),
                        type=_handover_type(source, target),
                        scope=EDGE_INTERNAL,
                        source_fleet_id=fleet.id,
                        target_fleet_id=fleet.id,
                    )
                )
                continue
            if target not in connected_agents:
                agent_node(target, NODE_EXTERNAL)
            graph.edges.append(
                GraphEdge(
                    source=str(source_id),
                    target=str(target),
                    type=_handover_type(source, target),
                    scope=EDGE_OUTBOUND,
                    governed=target in connected_agents
                    or (target_home is not None and target_home.fleet_id in governed_fleets),
                    allowed=target_home is None or target_home.is_exposed,
                    source_fleet_id=fleet.id,
                    target_fleet_id=target_home.fleet_id if target_home else None,
                )
            )

    # Inbound: handovers configured outside the fleet into one of its members.
    for source_id in inbound_sources:
        source = agents.get(source_id)
        if source is None or source_id in home_set:
            continue
        source_home = homes.get(source_id)
        for target in source.handover_agent_ids:
            if target not in home_set:
                continue
            if source_id not in connected_agents:
                agent_node(source_id, NODE_EXTERNAL)
            graph.edges.append(
                GraphEdge(
                    source=str(source_id),
                    target=str(target),
                    type=_handover_type(source, target),
                    scope=EDGE_INBOUND,
                    governed=(source_id, target) in governed_inbound,
                    allowed=target in exposed,
                    source_fleet_id=source_home.fleet_id if source_home else None,
                    target_fleet_id=fleet.id,
                )
            )
    return graph


async def build_fleet_graph(
    db: AsyncSession, fleet: Fleet, *, visible_agent_clause, visible_fleet_clause
) -> FleetGraph:
    """Load what :func:`assemble_fleet_graph` needs, in a fixed number of queries.

    *visible_agent_clause* / *visible_fleet_clause* are the caller's
    ``user_visible_filter`` on ``Agent`` / ``Fleet``: they decide which names
    are shown, never which nodes exist.
    """
    rows = await fleet_members(db, fleet.id)
    home_ids = [r.agent_id for r in rows if r.role == FLEET_ROLE_MEMBER and r.agent_id is not None]
    connected_ids = [r.agent_id for r in rows if r.role == FLEET_ROLE_CONNECTED and r.agent_id is not None]
    connected_fleet_ids = [r.target_fleet_id for r in rows if r.target_fleet_id is not None]

    member_rows = (
        (
            await db.execute(
                select(Agent.id, Agent.handover_agent_ids, Agent.handover_types).where(
                    Agent.id.in_(home_ids)
                )
            )
        ).all()
        if home_ids
        else []
    )
    handovers: dict[uuid.UUID, tuple[list[uuid.UUID], Mapping[str, str]]] = {
        aid: (_uuids(targets), types if isinstance(types, dict) else {})
        for aid, targets, types in member_rows
    }
    # Inbound candidates: agents outside the fleet that hand over at all; the
    # JSON list is filtered in Python, like ``fleet_boundary_violations``.
    home_set = set(home_ids)
    inbound: list[uuid.UUID] = []
    if home_ids:
        outside = (
            await db.execute(
                select(Agent.id, Agent.handover_agent_ids, Agent.handover_types).where(
                    Agent.handover_agent_ids.isnot(None), Agent.id.notin_(home_ids)
                )
            )
        ).all()
        for aid, targets, types in outside:
            ids = _uuids(targets)
            if home_set.intersection(ids):
                inbound.append(aid)
                handovers[aid] = (ids, types if isinstance(types, dict) else {})
    inbound.sort(key=str)

    every_id = set(home_ids) | set(connected_ids) | set(inbound)
    for targets, _types in list(handovers.values()):
        every_id.update(targets)
    facts_rows = (
        (await db.execute(select(Agent.id, Agent.name, Agent.icon, Agent.enabled).where(Agent.id.in_(every_id)))).all()
        if every_id
        else []
    )
    visible_ids = (
        set((await db.execute(select(Agent.id).where(Agent.id.in_(every_id), visible_agent_clause))).scalars().all())
        if every_id
        else set()
    )
    agents = {
        aid: AgentFacts(
            id=aid,
            name=name,
            icon=icon,
            enabled=bool(enabled) if enabled is not None else True,
            handover_agent_ids=handovers.get(aid, ([], {}))[0],
            handover_types=handovers.get(aid, ([], {}))[1],
            visible=aid in visible_ids,
        )
        for aid, name, icon, enabled in facts_rows
    }
    homes = await home_fleets_of(db, every_id)

    connected_fleets: dict[uuid.UUID, tuple[str, uuid.UUID | None, bool]] = {}
    if connected_fleet_ids:
        all_rows = (
            await db.execute(
                select(Fleet.id, Fleet.name, Fleet.orchestrator_agent_id).where(Fleet.id.in_(connected_fleet_ids))
            )
        ).all()
        seen_fleets = set(
            (
                await db.execute(select(Fleet.id).where(Fleet.id.in_(connected_fleet_ids), visible_fleet_clause))
            ).scalars().all()
        )
        connected_fleets = {fid: (name, entry, fid in seen_fleets) for fid, name, entry in all_rows}

    governed_inbound: set[tuple[uuid.UUID, uuid.UUID]] = set()
    source_fleets = {homes[s].fleet_id for s in inbound if s in homes}
    if source_fleets:
        links = (
            await db.execute(
                select(FleetMember.fleet_id, FleetMember.agent_id, FleetMember.target_fleet_id).where(
                    FleetMember.role == FLEET_ROLE_CONNECTED,
                    FleetMember.fleet_id.in_(source_fleets),
                    or_(FleetMember.agent_id.in_(home_ids), FleetMember.target_fleet_id == fleet.id),
                )
            )
        ).all()
        covered = {(fid, aid, tid) for fid, aid, tid in links}
        for source in inbound:
            home = homes.get(source)
            if home is None:
                continue
            for target in handovers.get(source, ([], {}))[0]:
                if target in home_set and (
                    (home.fleet_id, target, None) in covered or (home.fleet_id, None, fleet.id) in covered
                ):
                    governed_inbound.add((source, target))

    return assemble_fleet_graph(
        fleet=fleet,
        rows=rows,
        agents=agents,
        homes=homes,
        inbound_sources=inbound,
        connected_fleets=connected_fleets,
        governed_inbound=governed_inbound,
    )


# ── Health ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MemberHealth:
    agent_id: uuid.UUID
    status: str
    enabled: bool
    last_run_at: datetime | None
    last_run_status: str | None
    #: ``run_outcome.run_outcome`` of the last run (``interrupted`` for a rollout cut).
    last_run_outcome: str | None
    runs_in_flight: int
    #: Failed runs among the judged ones of the verdict's window.
    failures_24h: int
    #: Ended runs of the window that were not interrupted.
    recent_runs: int
    #: Runs of the window the platform interrupted.
    recent_interruptions: int
    open_work_items: int
    stale_work_items: int


def member_health(
    *,
    agent_id: uuid.UUID,
    enabled: bool,
    verdict: MemberVerdict,
    runs_in_flight: int,
    open_work_items: int,
    stale_work_items: int,
) -> MemberHealth:
    """One member's health. The status is the worst that applies, in
    :data:`HEALTH_STATUSES` order: ``failing`` (the run verdict of
    :func:`app.services.run_outcome.member_verdict`: its latest recent run
    that the platform did not interrupt failed), ``disabled``, ``stale`` (it
    owns a stale work item), ``idle`` (no run the caller can see), ``ok``."""
    if verdict.failing:
        status = "failing"
    elif not enabled:
        status = "disabled"
    elif stale_work_items > 0:
        status = "stale"
    elif verdict.last_run_at is None and runs_in_flight == 0:
        status = "idle"
    else:
        status = "ok"
    return MemberHealth(
        agent_id=agent_id,
        status=status,
        enabled=enabled,
        last_run_at=verdict.last_run_at,
        last_run_status=verdict.last_run_status,
        last_run_outcome=verdict.last_run_outcome,
        runs_in_flight=runs_in_flight,
        failures_24h=verdict.recent_failures,
        recent_runs=verdict.recent_runs,
        recent_interruptions=verdict.recent_interruptions,
        open_work_items=open_work_items,
        stale_work_items=stale_work_items,
    )


def worst_status(statuses: Iterable[str]) -> str:
    """The fleet's status: the worst of its members' (``ok`` with no member),
    except that ``idle`` is not a fault: a fleet is ``idle`` only when every
    member is, so one member with a run (recent or in flight) makes it ``ok``
    beside idle siblings (#9133 A4)."""
    ranked = {s: i for i, s in enumerate(HEALTH_STATUSES)}
    present = [s for s in statuses if s in ranked]
    if not present:
        return "ok"
    worst = min(present, key=ranked.__getitem__)
    if worst == "idle" and "ok" in present:
        return "ok"
    return worst


def home_member_ids(rows: Sequence[FleetMember], orchestrator_agent_id: uuid.UUID | None) -> list[uuid.UUID]:
    """The home members' agent ids in tree order (the health and activity order)."""
    return [r.agent_id for r in fleet_tree(orchestrator_agent_id, rows)]


async def build_fleet_health(
    db: AsyncSession,
    fleet: Fleet,
    *,
    user_id: str,
    run_scope,
    now: datetime | None = None,
) -> tuple[str, list[MemberHealth], int]:
    """``(fleet status, per member in tree order, open items of the fleet)``.

    The fleet's open items are the ones filed in its workspace or owned or
    created by a home member (:func:`app.services.work_item.fleet_work_item_clause`),
    each counted for the member that owns it or else created it:
    the strip equals the members' rows plus the items filed in the
    workspace that no member owns or created.

    Five queries whatever the fleet's size: the rows, the agents' ``enabled``,
    the runs (two), the work items (one).
    """
    from app.services.work_item import open_work_item_counts

    now = now or datetime.now(UTC)
    rows = await fleet_members(db, fleet.id)
    ids = home_member_ids(rows, fleet.orchestrator_agent_id)
    enabled = (
        dict((await db.execute(select(Agent.id, Agent.enabled).where(Agent.id.in_(ids)))).all()) if ids else {}
    )
    recent, in_flight = await run_facts(db, ids, run_scope=run_scope)
    per_owner, in_workspace = await open_work_item_counts(
        db, user_id=user_id, owner_agent_ids=ids, workspace_id=fleet.workspace_id
    )
    members = [
        member_health(
            agent_id=aid,
            enabled=enabled.get(aid) is not False,
            verdict=member_verdict(recent.get(aid, []), now=now),
            runs_in_flight=in_flight.get(aid, 0),
            open_work_items=per_owner.get(aid, (0, 0))[0],
            stale_work_items=per_owner.get(aid, (0, 0))[1],
        )
        for aid in ids
    ]
    return worst_status(m.status for m in members), members, in_workspace


@dataclass(frozen=True)
class AutomationCounts:
    """The opt-in state of a fleet's automations (#9566).

    ``total`` the automations that make the fleet run, ``enabled`` the ones
    switched on, ``paused`` the ones a person or the platform switched off
    after they were on (a pause reason outside
    :data:`app.services.assignment_pause.EXPECTED_REASONS`); an automation
    still off as seeded or created counts in neither. ``off_names`` names the
    automations that count in ``total`` and are not on, in a stable order (the
    home Fleets widget's tooltip, #11996); it never takes part in equality.
    """

    total: int = 0
    enabled: int = 0
    paused: int = 0
    #: Of ``paused``, the ones the fleet's "Pause all" switched off: what
    #: "Resume" switches back on.
    fleet_paused: int = 0
    off_names: tuple[str, ...] = field(default=(), compare=False)


def is_fleet_automation(slug: str | None, agent_builtin: bool, name: str, *, builtin_fleet: bool) -> bool:
    """Whether an assignment makes its fleet run (the population of :func:`automation_counts`).

    On a built-in fleet only the seeded fleet automations count
    (:data:`app.agents.builtin_agents.FLEET_OPT_IN_ASSIGNMENTS`); on a custom
    fleet every assignment of its home members. The weekly fleet health check
    of the orchestrator (#11700) counts on both.
    """
    from app.agents.builtin_agents import FLEET_OPT_IN_ASSIGNMENTS
    from app.services.fleets import FLEET_HEALTH_ASSIGNMENT_NAMES

    # The weekly health check (#11700) and its answer listener (#1406) count on both.
    if not builtin_fleet or name in FLEET_HEALTH_ASSIGNMENT_NAMES:
        return True
    return agent_builtin and (slug, name) in FLEET_OPT_IN_ASSIGNMENTS


def automation_counts(
    rows: Iterable[tuple[str | None, bool, str, bool, str | None]], *, builtin_fleet: bool
) -> AutomationCounts:
    """Count ``(agent slug, agent is built-in, assignment name, enabled, paused_reason)`` rows.

    On a built-in fleet only the seeded fleet automations count
    (:data:`app.agents.builtin_agents.FLEET_OPT_IN_ASSIGNMENTS`): the
    dashboard and decay automations its pre-existing members carry ship
    enabled and quota exempt, so they never make a fleet read as on.
    """
    from app.services.assignment_pause import EXPECTED_REASONS, FLEET_PAUSE_REASON

    total = enabled = paused = fleet_paused = 0
    off: list[str] = []
    for slug, agent_builtin, name, is_enabled, reason in rows:
        if not is_fleet_automation(slug, agent_builtin, name, builtin_fleet=builtin_fleet):
            continue
        total += 1
        if is_enabled:
            enabled += 1
            continue
        off.append(name)
        if reason and reason not in EXPECTED_REASONS:
            paused += 1
            if reason == FLEET_PAUSE_REASON:
                fleet_paused += 1
    return AutomationCounts(
        total=total, enabled=enabled, paused=paused, fleet_paused=fleet_paused, off_names=tuple(sorted(off))
    )


#: The one automation the Platform Health fleet counts (#974): Platform Health
#: itself, switched in Settings > Platform Health, never on the fleet's rows.
HEALTH_FLEET_AUTOMATION = "Platform Health"


def health_fleet_counts(active: bool) -> AutomationCounts:
    """The automation counts of the Platform Health fleet (#974).

    Its assignments are started by the Settings cycle runner (trigger
    ``manual``), so counting them would read "Not set up" forever on a fleet
    nobody may switch. The fleet's state is Platform Health's own: on in
    Settings reads ``on``, off reads ``paused`` with Platform Health named off.
    """
    if active:
        return AutomationCounts(total=1, enabled=1)
    return AutomationCounts(total=1, paused=1, off_names=(HEALTH_FLEET_AUTOMATION,))


async def platform_health_counts(db: AsyncSession) -> AutomationCounts:
    """:func:`health_fleet_counts` read from the capability and the Settings switch."""
    from app.services.health.availability import health_active, health_enabled

    return health_fleet_counts(health_enabled() and await health_active(db))


#: The Settings schedules of Platform Health (#974), in the Settings page's order.
HEALTH_SCHEDULE_KEYS: tuple[str, ...] = ("memory", "quality", "flows", "digest")
#: The signals that start an investigation of Platform Health (``HealthSignalSettings``).
HEALTH_SIGNAL_KEYS: tuple[str, ...] = ("feedback", "failure", "cost", "model_change")
#: Where an administrator switches and schedules Platform Health (the frontend's ``HEALTH_SETTINGS_PATH``).
HEALTH_SETTINGS_ROUTE = "/settings/automations/health"
#: The core module Platform Health always covers on an XTM One platform.
HEALTH_CORE_MODULE = "xtm_one"


@dataclass(frozen=True)
class HealthFleetStarts:
    """What starts the Platform Health fleet and what it covers (#974): the
    Settings schedules and signals, never its ``manual`` assignments."""

    #: A signal pass is on: an investigation starts on an event.
    events: bool = False
    #: The schedules switched on in Settings > Platform Health.
    schedules: int = 0
    #: The soonest next start of those schedules; ``None`` when none is on.
    next_run_at: datetime | None = None
    #: The modules it covers: XTM One and every active registered module, XTM One modes only.
    modules: tuple[str, ...] = ()
    settings_route: str = HEALTH_SETTINGS_ROUTE
    #: Platform Health is on in Settings: off, nothing starts, so no next run is promised (UX r1 B2 on #980).
    active: bool = True


def health_fleet_starts(
    health_settings: object,
    *,
    xtm_one: bool,
    registered: Iterable[str] = (),
    now: datetime | None = None,
    active: bool = True,
) -> HealthFleetStarts:
    """Pure: the starts line and modules of the Platform Health fleet from its settings (#974).

    Platform Health off (*active* false) starts nothing: the schedules are still
    counted, but no next run is computed, so a paused fleet never promises one.
    """
    from zoneinfo import ZoneInfo

    from app.core.utils import next_cron_run

    schedules = getattr(health_settings, "schedules", None)
    signals = getattr(health_settings, "signals", None)
    on = [key for key in HEALTH_SCHEDULE_KEYS if schedules is not None and getattr(schedules, f"{key}_enabled", False)]
    tz = None
    if schedules is not None:
        try:
            tz = ZoneInfo(str(getattr(schedules, "timezone", "UTC") or "UTC"))
        except (KeyError, ValueError):
            tz = None
    base = now or datetime.now(UTC)
    upcoming = (
        [run for key in on if (run := next_cron_run(getattr(schedules, key, None), base, tz=tz)) is not None]
        if active
        else []
    )
    events = signals is not None and any(getattr(signals, f"{key}_enabled", False) for key in HEALTH_SIGNAL_KEYS)
    modules: tuple[str, ...] = ()
    if xtm_one:
        modules = (HEALTH_CORE_MODULE, *sorted({m for m in registered if m and m != HEALTH_CORE_MODULE}))
    return HealthFleetStarts(
        events=events,
        schedules=len(on),
        next_run_at=min(upcoming) if upcoming else None,
        modules=modules,
        active=active,
    )


async def platform_health_starts(db: AsyncSession) -> HealthFleetStarts:
    """:func:`health_fleet_starts` read from Settings > Platform Health and the module registrations."""
    from app.core.config import settings as app_settings
    from app.models.platform_registration import PlatformRegistration
    from app.services.health.availability import health_active, health_enabled
    from app.services.health.module_analyst import MODULE_PROVIDERS
    from app.services.health.settings import load_health_settings

    active = health_enabled() and await health_active(db)
    xtm_one = bool(app_settings.is_xtm_one)
    registered: list[str] = []
    if xtm_one:
        registered = [
            str(identifier)
            for (identifier,) in (
                await db.execute(
                    select(PlatformRegistration.platform_identifier)
                    .where(
                        PlatformRegistration.platform_identifier.in_(MODULE_PROVIDERS),
                        PlatformRegistration.status == "active",
                    )
                    .distinct()
                )
            ).all()
        ]
    return health_fleet_starts(await load_health_settings(db), xtm_one=xtm_one, registered=registered, active=active)


async def fleet_automation_counts(db: AsyncSession, fleet: Fleet) -> AutomationCounts:
    """The opt-in state of *fleet*'s home members' automations, one query, no run.

    The Platform Health fleet reads Platform Health itself (:func:`health_fleet_counts`).
    """
    from app.models.assignment import Assignment
    from app.services.health.read_only import is_health_fleet

    if is_health_fleet(fleet):
        return await platform_health_counts(db)
    rows = await fleet_members(db, fleet.id)
    ids = home_member_ids(rows, fleet.orchestrator_agent_id)
    if not ids:
        return AutomationCounts()
    found = (
        await db.execute(
            select(Agent.slug, Agent.is_builtin, Assignment.name, Assignment.enabled, Assignment.paused_reason)
            .join(Agent, Agent.id == Assignment.agent_id)
            .where(Assignment.agent_id.in_(ids))
        )
    ).all()
    return automation_counts(
        ((slug, bool(builtin), name, bool(on), reason) for slug, builtin, name, on, reason in found),
        builtin_fleet=bool(getattr(fleet, "is_builtin", False)),
    )


#: The opt-in states of a fleet, as the badge of the Fleets list, the home
#: Fleets row and the fleet header reads them (``fleetOptIn`` of
#: ``frontend/src/lib/ctem-fleet-activity.ts``), with the label they show.
OPT_IN_LABELS: dict[str, str] = {
    "on": "On",
    "partial": "Partially on",
    "paused": "Paused",
    "not_set_up": "Not set up",
}


def opt_in_state(counts: AutomationCounts) -> str:
    """The badge of *counts*: every automation on is ``on``, some on is
    ``partial``, none on but one switched off after being on is ``paused``,
    otherwise ``not_set_up``. The same rule as ``fleetOptIn`` (pinned by
    ``tests/test_fleet_automation_state.py``), so a tool and the badge never
    disagree. A member with no assignment adds nothing to ``total``.
    """
    if counts.total > 0 and counts.enabled >= counts.total:
        return "on"
    if counts.enabled > 0:
        return "partial"
    if counts.paused > 0:
        return "paused"
    return "not_set_up"


@dataclass(frozen=True)
class OffAutomation:
    """A counted automation of a fleet that is off, and why."""

    agent_id: uuid.UUID
    agent_name: str
    assignment_id: uuid.UUID
    assignment_name: str
    #: The assignment's ``paused_reason`` (``created_disabled``, ``fleet_pause``,
    #: ``user_toggle``, ``panic_stop``, a quarantine...); ``None`` when unstamped.
    reason: str | None


@dataclass(frozen=True)
class FleetAutomationState:
    """The opt-in badge of a fleet with what makes it: counts and the automations off."""

    counts: AutomationCounts
    off: tuple[OffAutomation, ...] = ()

    @property
    def state(self) -> str:
        return opt_in_state(self.counts)

    @property
    def label(self) -> str:
        return OPT_IN_LABELS[self.state]

    def structured(self) -> dict:
        """The JSON form a tool's structured result carries."""
        return {
            "state": self.state,
            "label": self.label,
            "total": self.counts.total,
            "enabled": self.counts.enabled,
            "paused": self.counts.paused,
            "fleet_paused": self.counts.fleet_paused,
            "off": [
                {
                    "agent_id": str(o.agent_id),
                    "agent_name": o.agent_name,
                    "assignment_id": str(o.assignment_id),
                    "assignment_name": o.assignment_name,
                    "reason": o.reason,
                    "switch_on": switch_on_action(o.reason),
                }
                for o in self.off
            ],
        }


def switch_on_action(reason: str | None) -> str:
    """What switches an automation that is off back on, by its pause reason."""
    from app.services.assignment_pause import FLEET_PAUSE_REASON, PANIC_STOP_REASON

    if reason == FLEET_PAUSE_REASON:
        return "resume (the fleet's Resume)"
    if reason == PANIC_STOP_REASON:
        return "lift the emergency stop"
    return "enable (the fleet's Enable fleet, or the assignment's own switch)"


async def fleet_automation_state(db: AsyncSession, fleet: Fleet) -> FleetAutomationState:
    """The badge of *fleet* (:func:`automation_counts` over the same rows) and
    every counted automation that is off, by agent then assignment name.
    Reads only. Agent names are the stored ones: the caller filters what it may see.
    The Platform Health fleet reads Platform Health itself (:func:`health_fleet_counts`)."""
    from app.models.assignment import Assignment
    from app.services.health.read_only import is_health_fleet

    if is_health_fleet(fleet):
        return FleetAutomationState(counts=await platform_health_counts(db))
    ids = home_member_ids(await fleet_members(db, fleet.id), fleet.orchestrator_agent_id)
    if not ids:
        return FleetAutomationState(counts=AutomationCounts())
    found = (
        await db.execute(
            select(
                Agent.id,
                Agent.name,
                Agent.slug,
                Agent.is_builtin,
                Assignment.id,
                Assignment.name,
                Assignment.enabled,
                Assignment.paused_reason,
            )
            .join(Agent, Agent.id == Assignment.agent_id)
            .where(Assignment.agent_id.in_(ids))
        )
    ).all()
    builtin_fleet = bool(getattr(fleet, "is_builtin", False))
    counts = automation_counts(
        ((slug, bool(builtin), name, bool(on), reason) for _aid, _an, slug, builtin, _sid, name, on, reason in found),
        builtin_fleet=builtin_fleet,
    )
    off = sorted(
        (
            OffAutomation(
                agent_id=aid,
                agent_name=str(agent_name or ""),
                assignment_id=sid,
                assignment_name=str(name or ""),
                reason=reason,
            )
            for aid, agent_name, slug, builtin, sid, name, on, reason in found
            if not on and is_fleet_automation(slug, bool(builtin), name, builtin_fleet=builtin_fleet)
        ),
        key=lambda o: (o.agent_name.lower(), o.assignment_name.lower(), str(o.assignment_id)),
    )
    return FleetAutomationState(counts=counts, off=tuple(off))


@dataclass(frozen=True)
class FleetCardFacts:
    """What a card of the Fleets list states beside the fleet itself (#9977 S2):
    its automations (the opt-in badge and "N of M on") and its last run."""

    automations: AutomationCounts = field(default_factory=AutomationCounts)
    #: The newest run of a home member the caller may see; ``None`` = never run.
    last_run_at: datetime | None = None
    #: The Platform Health fleet only (#974): its Settings schedules, signals and modules.
    health_starts: HealthFleetStarts | None = None


async def fleet_card_facts(
    db: AsyncSession, fleets: Sequence[Fleet], *, run_scope
) -> dict[uuid.UUID, FleetCardFacts]:
    """The card facts of every fleet of a list in three queries whatever its
    size (the home members, their assignments, their newest visible run), so
    the list never asks one health per card. Reads only: starts no run.

    *run_scope* is the caller's ``run_visible_filter``: a run the caller could
    not open never dates a card.
    """
    from sqlalchemy import func

    from app.models.assignment import Assignment, AssignmentRun
    from app.services.health.read_only import is_health_fleet

    if not fleets:
        return {}
    by_id = {f.id: f for f in fleets}
    has_health = any(is_health_fleet(f) for f in fleets)
    health = await platform_health_counts(db) if has_health else None
    health_starts = await platform_health_starts(db) if has_health else None
    homes: dict[uuid.UUID, set[uuid.UUID]] = {f.id: set() for f in fleets}
    for fleet_id, agent_id in (
        await db.execute(
            select(FleetMember.fleet_id, FleetMember.agent_id).where(
                FleetMember.fleet_id.in_(list(by_id)),
                FleetMember.role == FLEET_ROLE_MEMBER,
                FleetMember.agent_id.is_not(None),
            )
        )
    ).all():
        homes[fleet_id].add(agent_id)
    agent_ids = sorted({a for members in homes.values() for a in members})
    if not agent_ids:
        return {
            fid: FleetCardFacts(automations=health, health_starts=health_starts)
            if health is not None and is_health_fleet(fleet)
            else FleetCardFacts()
            for fid, fleet in by_id.items()
        }
    rows_by_agent: dict[uuid.UUID, list[tuple[str | None, bool, str, bool, str | None]]] = {}
    for agent_id, slug, builtin, name, on, reason in (
        await db.execute(
            select(
                Assignment.agent_id,
                Agent.slug,
                Agent.is_builtin,
                Assignment.name,
                Assignment.enabled,
                Assignment.paused_reason,
            )
            .join(Agent, Agent.id == Assignment.agent_id)
            .where(Assignment.agent_id.in_(agent_ids))
        )
    ).all():
        rows_by_agent.setdefault(agent_id, []).append((slug, bool(builtin), name, bool(on), reason))
    last_by_agent: dict[uuid.UUID, datetime] = dict(
        (
            await db.execute(
                select(AssignmentRun.agent_id, func.max(AssignmentRun.created_at))
                .where(AssignmentRun.agent_id.in_(agent_ids), run_scope)
                .group_by(AssignmentRun.agent_id)
            )
        ).all()
    )
    out: dict[uuid.UUID, FleetCardFacts] = {}
    for fid, fleet in by_id.items():
        members = homes[fid]
        if health is not None and is_health_fleet(fleet):
            counts = health
        else:
            counts = automation_counts(
                (row for aid in members for row in rows_by_agent.get(aid, [])),
                builtin_fleet=bool(getattr(fleet, "is_builtin", False)),
            )
        runs = [last_by_agent[aid] for aid in members if last_by_agent.get(aid) is not None]
        out[fid] = FleetCardFacts(
            automations=counts,
            last_run_at=max(runs) if runs else None,
            health_starts=health_starts if is_health_fleet(fleet) else None,
        )
    return out


#: The ``trigger_context.object_type`` of a run a CTEM case event started (``ctem_case_loop.case_data``).
CASE_OBJECT_TYPE = "ctem_case"


def run_case_id(object_type: str | None, object_id: str | None) -> str | None:
    """The CTEM case a run works on (#915): the object of the platform event that
    started it when that object is a case, else ``None``. An id only: the case
    itself is read through the case API, which applies the reader's view."""
    if object_type != CASE_OBJECT_TYPE or not object_id:
        return None
    try:
        return str(uuid.UUID(str(object_id)))
    except (TypeError, ValueError):
        return None


def case_run_clause(case_id: uuid.UUID):
    """The ``AssignmentRun`` rows a CTEM case event started for *case_id* (#915):
    the event's object, read key by key from ``trigger_context``."""
    from app.models.assignment import AssignmentRun

    return (AssignmentRun.trigger_context["object_type"].as_string() == CASE_OBJECT_TYPE) & (
        AssignmentRun.trigger_context["object_id"].as_string() == str(case_id)
    )


async def case_runs(db: AsyncSession, case_id: uuid.UUID, *, run_scope, limit: int) -> list[tuple]:
    """The runs the caller may see that work on one CTEM case, oldest first (#915):
    ``(id, agent_id, agent_name, status, created_at, completed_at, outcome)``,
    for the case page that interleaves them with the case history. Any fleet's
    runs: a case is worked by whichever fleet its event reached."""
    from app.models.assignment import AssignmentRun

    rows = (
        await db.execute(
            select(
                AssignmentRun.id,
                AssignmentRun.agent_id,
                Agent.name,
                AssignmentRun.status,
                AssignmentRun.created_at,
                AssignmentRun.completed_at,
                AssignmentRun.error,
            )
            .join(Agent, Agent.id == AssignmentRun.agent_id)
            .where(case_run_clause(case_id), run_scope)
            .order_by(AssignmentRun.created_at.desc(), AssignmentRun.id.desc())
            .limit(limit)
        )
    ).all()
    return [
        (run_id, agent_id, name, status, created_at, completed_at, run_outcome(status, error))
        for (run_id, agent_id, name, status, created_at, completed_at, error) in reversed(rows)
    ]


async def recent_runs(
    db: AsyncSession, agent_ids: Sequence[uuid.UUID], *, run_scope, limit: int
) -> list[tuple]:
    """The members' latest runs the caller may see, newest first:
    ``(id, agent_id, assignment_id, status, created_at, completed_at, outcome,
    trigger_type, trigger_provider, trigger_event, case_id)``, the outcome by
    :func:`app.services.run_outcome.run_outcome`, the rule the health badge
    reads. The trigger fields are read from ``trigger_context`` key by
    key (``type``, ``provider``, ``event``, ``object_type``, ``object_id``;
    ``None`` when absent), never the whole JSON, so a large delivery body is
    never loaded for a timeline row; ``case_id`` is :func:`run_case_id`."""
    from app.models.assignment import AssignmentRun

    if not agent_ids:
        return []
    rows = (
        await db.execute(
            select(
                AssignmentRun.id,
                AssignmentRun.agent_id,
                AssignmentRun.assignment_id,
                AssignmentRun.status,
                AssignmentRun.created_at,
                AssignmentRun.completed_at,
                AssignmentRun.error,
                AssignmentRun.trigger_context["type"].as_string(),
                AssignmentRun.trigger_context["provider"].as_string(),
                AssignmentRun.trigger_context["event"].as_string(),
                AssignmentRun.trigger_context["object_type"].as_string(),
                AssignmentRun.trigger_context["object_id"].as_string(),
            )
            .where(AssignmentRun.agent_id.in_(list(agent_ids)), run_scope)
            .order_by(AssignmentRun.created_at.desc(), AssignmentRun.id.desc())
            .limit(limit)
        )
    ).all()
    return [
        (
            run_id,
            agent_id,
            assignment_id,
            status,
            created_at,
            completed_at,
            run_outcome(status, error),
            trigger_type,
            trigger_provider,
            trigger_event,
            run_case_id(object_type, object_id),
        )
        for (
            run_id,
            agent_id,
            assignment_id,
            status,
            created_at,
            completed_at,
            error,
            trigger_type,
            trigger_provider,
            trigger_event,
            object_type,
            object_id,
        ) in rows
    ]


async def run_facts(
    db: AsyncSession, agent_ids: Sequence[uuid.UUID], *, run_scope
) -> tuple[dict[uuid.UUID, list[RunFact]], dict[uuid.UUID, int]]:
    """``(latest runs per agent, newest first; runs in flight per agent)``.

    *run_scope* is the caller's ``run_visible_filter``: two queries whatever
    the fleet's size (the :data:`HEALTH_RECENT_RUNS` latest per agent by a
    ``row_number`` window, one grouped count of the active runs).
    """
    from sqlalchemy import func

    from app.models.assignment import AssignmentRun
    from app.services.assignment_engine import ACTIVE_RUN_STATUSES

    if not agent_ids:
        return {}, {}
    rank = (
        func.row_number()
        .over(
            partition_by=AssignmentRun.agent_id,
            order_by=(AssignmentRun.created_at.desc(), AssignmentRun.id.desc()),
        )
        .label("rank")
    )
    ranked = (
        select(AssignmentRun.agent_id, AssignmentRun.created_at, AssignmentRun.status, AssignmentRun.error, rank)
        .where(AssignmentRun.agent_id.in_(list(agent_ids)), run_scope)
        .subquery()
    )
    recent_rows = (
        await db.execute(
            select(ranked.c.agent_id, ranked.c.created_at, ranked.c.status, ranked.c.error)
            .where(ranked.c.rank <= HEALTH_RECENT_RUNS)
            .order_by(ranked.c.agent_id, ranked.c.rank)
        )
    ).all()
    flight_rows = (
        await db.execute(
            select(AssignmentRun.agent_id, func.count())
            .where(
                AssignmentRun.agent_id.in_(list(agent_ids)),
                run_scope,
                AssignmentRun.status.in_(ACTIVE_RUN_STATUSES),
            )
            .group_by(AssignmentRun.agent_id)
        )
    ).all()
    recent: dict[uuid.UUID, list[RunFact]] = {}
    for aid, created_at, status, error in recent_rows:
        recent.setdefault(aid, []).append(RunFact(created_at=created_at, status=status, error=error))
    return recent, {aid: int(count or 0) for aid, count in flight_rows}


#: The window of the About page's run figures (#10123): the same 30 days as the ROI tab's default.
RUN_STATS_DAYS = 30


@dataclass(frozen=True)
class FleetRunStats:
    """The fleet's runs over the last :data:`RUN_STATS_DAYS` days (#10123).

    ``runs`` the runs created in the window, ``succeeded`` the completed ones,
    ``failed`` the failed ones the platform did not interrupt, ``by_day`` one
    count per day, oldest first (the sparkline), ``awaiting_approval`` the runs
    of the fleet waiting for a person now, whatever their age.
    ``succeeded_by_day`` and ``failed_by_day`` split ``by_day`` per outcome
    (the home Fleets widget's stacked bars, #11996), ``last_summary`` the
    one-line result of the newest ended run, ``None`` when it has none.
    """

    runs: int = 0
    succeeded: int = 0
    failed: int = 0
    by_day: tuple[int, ...] = ()
    awaiting_approval: int = 0
    succeeded_by_day: tuple[int, ...] = ()
    failed_by_day: tuple[int, ...] = ()
    last_summary: str | None = None


def run_stats(
    rows: Iterable[tuple[datetime, str, str | None]],
    *,
    awaiting_approval: int,
    now: datetime,
    days: int = RUN_STATS_DAYS,
) -> FleetRunStats:
    """Count ``(created_at, status, error)`` rows of the window into :class:`FleetRunStats`.

    Pure: the route reads the rows once. A row outside the window is ignored; a
    naive ``created_at`` is read as UTC.
    """
    from datetime import timedelta

    from app.services.run_outcome import OUTCOME_FAILED, OUTCOME_OK

    today = now.astimezone(UTC).date()
    first = today - timedelta(days=days - 1)
    by_day = [0] * days
    ok_by_day = [0] * days
    failed_by_day = [0] * days
    runs = succeeded = failed = 0
    for created_at, status, error in rows:
        when = created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)
        day = when.astimezone(UTC).date()
        if day < first or day > today:
            continue
        runs += 1
        by_day[(day - first).days] += 1
        outcome = run_outcome(status, error)
        if outcome == OUTCOME_OK:
            succeeded += 1
            ok_by_day[(day - first).days] += 1
        elif outcome == OUTCOME_FAILED:
            failed += 1
            failed_by_day[(day - first).days] += 1
    return FleetRunStats(
        runs=runs,
        succeeded=succeeded,
        failed=failed,
        by_day=tuple(by_day),
        awaiting_approval=awaiting_approval,
        succeeded_by_day=tuple(ok_by_day),
        failed_by_day=tuple(failed_by_day),
    )


#: The longest last-run summary the health answer carries: one line on a card.
LAST_SUMMARY_MAX_CHARS = 280


def one_line_summary(text: str | None) -> str | None:
    """*text* on one line, cut to :data:`LAST_SUMMARY_MAX_CHARS`; ``None`` when blank."""
    if not text:
        return None
    line = " ".join(text.split())
    if not line:
        return None
    if len(line) > LAST_SUMMARY_MAX_CHARS:
        line = line[: LAST_SUMMARY_MAX_CHARS - 1].rstrip() + "\u2026"
    return line


async def fleet_run_stats(
    db: AsyncSession, agent_ids: Sequence[uuid.UUID], *, run_scope, now: datetime | None = None
) -> FleetRunStats:
    """The fleet's 30-day run figures for the About page's KPI row (#10123).

    Two queries whatever the fleet's size, scoped by the caller's
    ``run_visible_filter`` like the health strip: a run the caller could not
    open is never counted. Reads only: starts no run, uses no quota.
    """
    from datetime import timedelta

    from sqlalchemy import func

    from app.models.assignment import AssignmentRun

    now = now or datetime.now(UTC)
    if not agent_ids:
        return run_stats((), awaiting_approval=0, now=now)
    since = datetime.combine(now.astimezone(UTC).date() - timedelta(days=RUN_STATS_DAYS - 1), datetime.min.time(), UTC)
    ids = list(agent_ids)
    rows = (
        await db.execute(
            select(AssignmentRun.created_at, AssignmentRun.status, AssignmentRun.error).where(
                AssignmentRun.agent_id.in_(ids), run_scope, AssignmentRun.created_at >= since
            )
        )
    ).all()
    waiting = (
        await db.execute(
            select(func.count()).where(
                AssignmentRun.agent_id.in_(ids), run_scope, AssignmentRun.status == "awaiting_human"
            )
        )
    ).scalar_one()
    summary = (
        await db.execute(
            select(AssignmentRun.result_summary)
            .where(
                AssignmentRun.agent_id.in_(ids),
                run_scope,
                AssignmentRun.status.in_(("completed", "failed")),
                AssignmentRun.result_summary.is_not(None),
            )
            .order_by(AssignmentRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    stats = run_stats(rows, awaiting_approval=int(waiting or 0), now=now)
    return replace(stats, last_summary=one_line_summary(summary))
