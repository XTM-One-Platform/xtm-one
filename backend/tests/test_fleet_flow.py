"""The fleet data flow assembled from configuration, no database (#9829)."""

from __future__ import annotations

import uuid

from app.services.fleet_flow import (
    EDGE_HANDOVER,
    EDGE_READ,
    EDGE_TRIGGER,
    EDGE_WRITE,
    LANE_AGENT,
    LANE_OUTPUT,
    LANE_SOURCE,
    FlowAgent,
    FlowAssignment,
    FlowIntegration,
    assemble_fleet_flow,
)


def _agent(name: str, **kw) -> FlowAgent:
    return FlowAgent(
        id=kw.pop("id", uuid.uuid4()),
        name=name,
        icon=None,
        enabled=kw.pop("enabled", True),
        visible=kw.pop("visible", True),
        **kw,
    )


def _assignment(agent: FlowAgent, name: str, trigger_type: str, config: dict, **kw) -> FlowAssignment:
    return FlowAssignment(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name=name,
        trigger_type=trigger_type,
        trigger_config=config,
        enabled=kw.pop("enabled", True),
        **kw,
    )


def _integration(provider: str, status: str = "connected", visible: bool = True) -> FlowIntegration:
    return FlowIntegration(id=uuid.uuid4(), provider=provider, name=f"{provider} prod", status=status, visible=visible)


def test_every_assignment_is_exactly_one_trigger_edge_and_sources_group_by_integration() -> None:
    outlook = _integration("microsoft")
    github = _integration("github")
    ea = _agent("Assistant", integration_bindings={"microsoft": [str(outlook.id)]}, builtin_tool_ids=["coordination"])
    dev = _agent("Developer", integration_bindings={"github": [str(github.id)]})
    assignments = [
        _assignment(
            ea,
            "Mail triage",
            "integration_event",
            {
                "provider": "microsoft",
                "event": "new_email",
                "integration_id": str(outlook.id),
                "filter": {"from": {"ends_with": "@filigran.io"}},
            },
        ),
        _assignment(
            ea,
            "Calendar",
            "integration_event",
            {"provider": "microsoft", "event": "event_created", "integration_id": str(outlook.id)},
        ),
        _assignment(ea, "Daily brief", "schedule", {"cron": "0 7 * * 1-5", "timezone": "Europe/Paris"}),
        _assignment(
            ea,
            "Completed items",
            "platform_event",
            {"event": ["work_item.completed", "run.orphaned"], "scope": "fleet"},
        ),
        _assignment(dev, "Pick up", "work_item", {"correlation_prefix": "dev:"}),
        _assignment(
            dev,
            "Red check",
            "integration_event",
            {"provider": "github", "event": "check_run", "integration_id": str(github.id)},
            enabled=False,
            paused_reason="failures",
        ),
        _assignment(dev, "Sweep", "manual", {}, has_activation_prompt=True, activation_tool="list_work_items"),
    ]
    flow = assemble_fleet_flow(
        agents=[ea, dev], assignments=assignments, integrations={outlook.id: outlook, github.id: github}
    )

    triggers = [e for e in flow.edges if e.kind == EDGE_TRIGGER]
    assert sorted(e.assignment_id for e in triggers) == sorted(a.id for a in assignments)
    nodes = {n.id: n for n in flow.nodes}
    # One source node per integration, its events as chips.
    outlook_source = nodes[f"source:integration:microsoft:{outlook.id}"]
    assert outlook_source.lane == LANE_SOURCE and outlook_source.events == ["event_created", "new_email"]
    assert nodes["source:platform"].events == ["run.orphaned", "work_item.completed"]
    by_name = {e.assignment_name: e for e in triggers}
    assert by_name["Daily brief"].detail == "Every weekday at 07:00 (Europe/Paris)"
    # The client words the schedule in the reader's language from the cron and its timezone.
    assert (by_name["Daily brief"].cron, by_name["Daily brief"].timezone) == ("0 7 * * 1-5", "Europe/Paris")
    assert by_name["Pick up"].cron == ""
    assert by_name["Pick up"].detail == "dev:*"
    assert by_name["Mail triage"].filter.startswith("from ends_with @filigran.io")
    assert by_name["Sweep"].gate == "tool check list_work_items, activation question"
    assert (by_name["Red check"].enabled, by_name["Red check"].reason) == (False, "paused")
    # Lanes are ordered left to right.
    lanes = [n.lane for n in flow.nodes]
    assert lanes == sorted(lanes, key=[LANE_SOURCE, LANE_AGENT, LANE_OUTPUT].index)
    assert nodes["output:work_items"].lane == LANE_OUTPUT


def test_nothing_is_shown_that_the_configuration_does_not_hold() -> None:
    lonely = _agent("Lonely")
    flow = assemble_fleet_flow(agents=[lonely], assignments=[], integrations={})
    assert [n.id for n in flow.nodes] == [f"agent:{lonely.id}"]
    assert flow.edges == []
    # An assignment of an agent outside the fleet is not drawn.
    stranger = _agent("Stranger")
    flow = assemble_fleet_flow(agents=[lonely], assignments=[_assignment(stranger, "x", "manual", {})], integrations={})
    assert flow.edges == []


def test_outputs_read_or_write_from_the_bindings_read_only_and_allow_lists() -> None:
    cti, mail, gh = _integration("opencti"), _integration("microsoft", status="disconnected"), _integration("github")
    agent = _agent(
        "Analyst",
        integration_bindings={"opencti": [str(cti.id)], "microsoft": [str(mail.id)], "github": [str(gh.id)]},
        integration_read_only={"opencti": [str(cti.id)]},
        integration_allowed_tools={"github": ["list_github_prs", "get_github_pr"]},
        integration_template_bindings=["openaev"],
    )
    flow = assemble_fleet_flow(agents=[agent], assignments=[], integrations={cti.id: cti, mail.id: mail, gh.id: gh})
    kinds = {e.target: e.kind for e in flow.edges}
    assert kinds[f"output:integration:opencti:{cti.id}"] == EDGE_READ
    assert kinds[f"output:integration:github:{gh.id}"] == EDGE_READ
    assert kinds[f"output:integration:microsoft:{mail.id}"] == EDGE_WRITE
    assert kinds["output:integration:openaev"] == EDGE_WRITE
    nodes = {n.id: n for n in flow.nodes}
    assert (
        nodes[f"output:integration:microsoft:{mail.id}"].enabled,
        nodes[f"output:integration:microsoft:{mail.id}"].reason,
    ) == (False, "not connected")


def test_handovers_stay_inside_the_fleet_and_hidden_agents_keep_a_placeholder() -> None:
    outside = uuid.uuid4()
    b = _agent("B", visible=False)
    a = _agent("A", handover_agent_ids=[b.id, outside], handover_types={str(b.id): "consult"})
    flow = assemble_fleet_flow(
        agents=[a, b], assignments=[_assignment(b, "secret name", "manual", {})], integrations={}
    )
    handovers = [e for e in flow.edges if e.kind == EDGE_HANDOVER]
    assert [(e.source, e.target, e.type) for e in handovers] == [(f"agent:{a.id}", f"agent:{b.id}", "consult")]
    nodes = {n.id: n for n in flow.nodes}
    assert nodes[f"agent:{b.id}"].name == "" and not nodes[f"agent:{b.id}"].visible
    trigger = next(e for e in flow.edges if e.kind == EDGE_TRIGGER)
    assert trigger.assignment_name == ""


def test_implicit_reads_draw_a_read_edge_never_a_write_and_merge_with_a_binding() -> None:
    """#101 S15f: a module checker reads its module's integration without a binding."""
    cti, aev = _integration("opencti"), _integration("openaev", status="disconnected")
    checker = _agent("OpenCTI Health Checker")
    bound = _agent("Bound", integration_bindings={"opencti": [str(cti.id)]})
    flow = assemble_fleet_flow(
        agents=[checker, bound],
        assignments=[],
        integrations={cti.id: cti, aev.id: aev},
        implicit_reads={checker.id: [("opencti", cti.id), ("openaev", aev.id)], bound.id: [("opencti", cti.id)]},
    )
    edges = [(e.source, e.target, e.kind) for e in flow.edges]
    assert (f"agent:{checker.id}", f"output:integration:opencti:{cti.id}", EDGE_READ) in edges
    assert (f"agent:{checker.id}", f"output:integration:openaev:{aev.id}", EDGE_READ) in edges
    # A member bound to the same integration keeps its one edge, the binding's access.
    assert [e for e in edges if e[0] == f"agent:{bound.id}"] == [
        (f"agent:{bound.id}", f"output:integration:opencti:{cti.id}", EDGE_WRITE)
    ]
    nodes = {n.id: n for n in flow.nodes}
    assert nodes[f"output:integration:openaev:{aev.id}"].reason == "not connected"
    assert sum(1 for n in flow.nodes if n.id == f"output:integration:opencti:{cti.id}") == 1


def test_the_flow_is_deterministic() -> None:
    gh = _integration("github")
    a = _agent("A", integration_bindings={"github": [str(gh.id)]})
    assignments = [_assignment(a, f"n{i}", "schedule", {"cron": "*/5 * * * *"}) for i in range(5)]
    first = assemble_fleet_flow(agents=[a], assignments=assignments, integrations={gh.id: gh})
    again = assemble_fleet_flow(agents=[a], assignments=list(reversed(assignments)), integrations={gh.id: gh})
    assert first == again
