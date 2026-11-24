"""Fleet views that need no database: tree, graph, health."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Column, MetaData, Table
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import UUID

from app.models.fleet import FLEET_ROLE_CONNECTED, FLEET_ROLE_MEMBER, Fleet, FleetMember
from app.services.fleet_views import (
    EDGE_INBOUND,
    EDGE_INTERNAL,
    EDGE_OUTBOUND,
    NODE_CONNECTED,
    NODE_EXTERNAL,
    NODE_FLEET,
    NODE_MEMBER,
    AgentFacts,
    assemble_fleet_graph,
    case_run_clause,
    fleet_node_id,
    fleet_scope_clause,
    fleet_tree,
    member_health,
    run_case_id,
    worst_status,
)
from app.services.fleets import HomeFleet
from app.services.run_outcome import RunFact, member_verdict


def test_a_run_started_by_a_case_event_names_its_case_and_nothing_else_does():
    """#915: the fleet timeline ties a run to its CTEM case by the event's object, an id only."""
    case_id = uuid.uuid4()
    assert run_case_id("ctem_case", str(case_id)) == str(case_id)
    assert run_case_id("work_item", str(case_id)) is None
    assert run_case_id("ctem_case", None) is None
    assert run_case_id("ctem_case", "not-a-uuid") is None
    assert run_case_id(None, None) is None


def test_the_case_run_clause_reads_the_event_object_of_the_run():
    """#915: the case page reads the runs whose trigger object is that case, by key, never the whole JSON."""
    case_id = uuid.uuid4()
    sql = str(case_run_clause(case_id).compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "object_type" in sql and "ctem_case" in sql
    assert "object_id" in sql and str(case_id) in sql


def _fleet(orchestrator: uuid.UUID | None = None, name: str = "CEO Office") -> Fleet:
    return Fleet(id=uuid.uuid4(), name=name, slug="f", workspace_id=uuid.uuid4(), orchestrator_agent_id=orchestrator)


def _home(fleet: Fleet, agent: uuid.UUID, parent: FleetMember | None = None, *, exposed: bool = False) -> FleetMember:
    return FleetMember(
        id=uuid.uuid4(),
        fleet_id=fleet.id,
        agent_id=agent,
        role=FLEET_ROLE_MEMBER,
        parent_member_id=parent.id if parent else None,
        is_exposed=exposed,
    )


def _facts(agent: uuid.UUID, name: str, handovers: list[uuid.UUID] | None = None, **kw) -> AgentFacts:
    return AgentFacts(
        id=agent,
        name=name,
        icon=None,
        enabled=kw.get("enabled", True),
        handover_agent_ids=handovers or [],
        handover_types=kw.get("types", {}),
        visible=kw.get("visible", True),
    )


# ── Tree ────────────────────────────────────────────────────────────────────


def test_the_tree_puts_the_orchestrator_first_and_lifts_orphans_to_it() -> None:
    ea, dev, ux, analyst = (uuid.uuid4() for _ in range(4))
    fleet = _fleet(ea)
    dev_row = _home(fleet, dev)
    rows = [
        dev_row,
        _home(fleet, ux, dev_row),
        _home(fleet, ea),
        # A parent that left the fleet: the member reports to the orchestrator.
        FleetMember(id=uuid.uuid4(), fleet_id=fleet.id, agent_id=analyst, role=FLEET_ROLE_MEMBER,
                    parent_member_id=uuid.uuid4()),
    ]
    tree = fleet_tree(ea, rows)
    assert [(r.agent_id, r.depth, r.parent_agent_id) for r in tree] == [
        (ea, 0, None),
        (dev, 1, ea),
        (ux, 2, dev),
        (analyst, 1, ea),
    ]
    assert [r.is_orchestrator for r in tree] == [True, False, False, False]
    # Deterministic: the same rows draw the same tree.
    assert fleet_tree(ea, rows) == tree


def test_a_parent_loop_is_cut_and_connected_rows_are_not_in_the_tree() -> None:
    a, b, outside = (uuid.uuid4() for _ in range(3))
    fleet = _fleet(None)
    row_a = _home(fleet, a)
    row_b = _home(fleet, b, row_a)
    row_a.parent_member_id = row_b.id
    connected = FleetMember(id=uuid.uuid4(), fleet_id=fleet.id, agent_id=outside, role=FLEET_ROLE_CONNECTED)
    tree = fleet_tree(None, [row_a, row_b, connected])
    assert sorted(r.agent_id for r in tree) == sorted([a, b])
    assert len({r.member_id for r in tree}) == 2
    assert tree[0].depth == 0


# ── Graph ───────────────────────────────────────────────────────────────────


def test_the_graph_draws_members_connected_fleets_and_crossing_edges() -> None:
    ea, dev, partner, stranger, hidden_lead, other_entry = (uuid.uuid4() for _ in range(6))
    fleet = _fleet(ea)
    other = _fleet(other_entry, name="SOC")
    ea_row = _home(fleet, ea)
    rows = [
        ea_row,
        _home(fleet, dev, ea_row),
        FleetMember(id=uuid.uuid4(), fleet_id=fleet.id, agent_id=partner, role=FLEET_ROLE_CONNECTED,
                    collaboration_instruction="Ask for a brief", context_inclusion="summary"),
        FleetMember(id=uuid.uuid4(), fleet_id=fleet.id, target_fleet_id=other.id, role=FLEET_ROLE_CONNECTED),
    ]
    agents = {
        ea: _facts(ea, "EA", [dev, partner, stranger], types={str(dev): "consult"}),
        dev: _facts(dev, "Developer", [hidden_lead]),
        partner: _facts(partner, "Partner"),
        stranger: _facts(stranger, "Stranger", visible=False),
        hidden_lead: _facts(hidden_lead, "Lead"),
    }
    homes = {
        hidden_lead: HomeFleet(fleet_id=other.id, fleet_name="SOC", member_id=uuid.uuid4(),
                               is_orchestrator=False, is_exposed=False),
    }
    graph = assemble_fleet_graph(
        fleet=fleet,
        rows=rows,
        agents=agents,
        homes=homes,
        inbound_sources=[],
        connected_fleets={other.id: ("SOC", other_entry, True)},
        governed_inbound=set(),
    )
    kinds = {n.id: n.kind for n in graph.nodes}
    assert kinds == {
        str(ea): NODE_MEMBER,
        str(dev): NODE_MEMBER,
        str(partner): NODE_CONNECTED,
        fleet_node_id(other.id): NODE_FLEET,
        str(stranger): NODE_EXTERNAL,
        str(hidden_lead): NODE_EXTERNAL,
    }
    nodes = {n.id: n for n in graph.nodes}
    assert nodes[str(ea)].is_orchestrator and nodes[str(ea)].is_exposed
    assert nodes[str(dev)].parent_agent_id == ea
    assert nodes[str(partner)].collaboration_instruction == "Ask for a brief"
    assert nodes[fleet_node_id(other.id)].entry_agent_id == other_entry
    # An agent the caller cannot see keeps its node, nameless.
    assert nodes[str(stranger)].name == "" and nodes[str(stranger)].visible is False

    edges = {(e.source, e.target): e for e in graph.edges}
    assert edges[(str(ea), str(dev))].scope == EDGE_INTERNAL
    assert edges[(str(ea), str(dev))].type == "consult"
    assert edges[(str(ea), str(partner))].governed is True
    stranger_edge = edges[(str(ea), str(stranger))]
    assert (stranger_edge.scope, stranger_edge.governed, stranger_edge.type) == (EDGE_OUTBOUND, False, "delegate")
    # Into another fleet: governed by the fleet-level connected row, flagged as not exposed.
    lead_edge = edges[(str(dev), str(hidden_lead))]
    assert (lead_edge.governed, lead_edge.allowed, lead_edge.target_fleet_id) == (True, False, other.id)


def test_inbound_edges_carry_governance_and_exposure() -> None:
    ea, desk, caller, rogue = (uuid.uuid4() for _ in range(4))
    fleet = _fleet(ea)
    caller_fleet = uuid.uuid4()
    rows = [_home(fleet, ea), _home(fleet, desk)]
    agents = {
        ea: _facts(ea, "EA"),
        desk: _facts(desk, "Desk"),
        caller: _facts(caller, "Caller", [ea]),
        rogue: _facts(rogue, "Rogue", [desk]),
    }
    homes = {caller: HomeFleet(fleet_id=caller_fleet, fleet_name="SOC", member_id=uuid.uuid4(),
                               is_orchestrator=True, is_exposed=True)}
    graph = assemble_fleet_graph(
        fleet=fleet,
        rows=rows,
        agents=agents,
        homes=homes,
        inbound_sources=[caller, rogue],
        connected_fleets={},
        governed_inbound={(caller, ea)},
    )
    edges = {(e.source, e.target): e for e in graph.edges}
    governed = edges[(str(caller), str(ea))]
    assert (governed.scope, governed.governed, governed.allowed, governed.source_fleet_id) == (
        EDGE_INBOUND, True, True, caller_fleet,
    )
    ungoverned = edges[(str(rogue), str(desk))]
    assert (ungoverned.governed, ungoverned.allowed, ungoverned.source_fleet_id) == (False, False, None)
    assert [n.kind for n in graph.nodes if n.id in {str(caller), str(rogue)}] == [NODE_EXTERNAL, NODE_EXTERNAL]


# ── Health ──────────────────────────────────────────────────────────────────


_NOW = datetime(2026, 10, 31, tzinfo=UTC)


def _health(runs: tuple[tuple[str, str | None], ...] = (("completed", None),), **kw):
    facts = [RunFact(created_at=_NOW, status=s, error=e) for s, e in runs]
    base = dict(agent_id=uuid.uuid4(), enabled=True, verdict=member_verdict(facts, now=_NOW),
                runs_in_flight=0, open_work_items=0, stale_work_items=0)
    return member_health(**{**base, **kw})


def test_a_member_status_is_the_worst_that_applies() -> None:
    assert _health().status == "ok"
    assert _health((("failed", "boom"),), enabled=False).status == "failing"
    assert _health(enabled=False, stale_work_items=2).status == "disabled"
    assert _health(stale_work_items=1).status == "stale"
    assert _health(()).status == "idle"
    assert _health((), runs_in_flight=1).status == "ok"


def test_a_member_interrupted_by_the_platform_is_not_failing() -> None:
    cut = "the consulted turn (child run x) was cut by a rollout: its server process is stopping (stop, SIGTERM)"
    health = _health((("failed", cut), ("completed", None)))
    assert health.status == "ok"
    assert (health.last_run_status, health.last_run_outcome) == ("failed", "interrupted")
    assert (health.failures_24h, health.recent_runs, health.recent_interruptions) == (0, 1, 1)


def test_the_fleet_status_is_the_worst_of_its_members() -> None:
    assert worst_status([]) == "ok"
    assert worst_status(["ok", "idle", "stale"]) == "stale"
    assert worst_status(["ok", "failing", "disabled"]) == "failing"
    assert worst_status(["unknown", "ok"]) == "ok"


def test_a_fleet_is_idle_only_when_every_member_is() -> None:
    # One member with a run in flight is "ok"; its idle siblings never turn the fleet "Idle" (#9133 A4).
    busy = _health((), runs_in_flight=1)
    quiet = _health(())
    assert worst_status([busy.status, quiet.status, quiet.status]) == "ok"
    assert worst_status(["idle", "idle"]) == "idle"
    assert worst_status(["idle", "ok", "stale"]) == "stale"


# ── ?fleet= filter ──────────────────────────────────────────────────────────


def _compiled(clause) -> str:
    return str(clause.compile(dialect=postgresql.dialect()))


def test_the_fleet_filter_keeps_a_fleet_home_members_or_the_playground() -> None:
    column = Table("t", MetaData(), Column("agent_id", UUID(as_uuid=True))).c.agent_id
    assert fleet_scope_clause(column, None) is None
    one = _compiled(fleet_scope_clause(column, uuid.uuid4()))
    assert "t.agent_id IN (SELECT fleet_members.agent_id" in one
    assert "fleet_members.fleet_id =" in one
    playground = _compiled(fleet_scope_clause(column, "none"))
    assert "t.agent_id IS NULL OR" in playground and "NOT IN" in playground
