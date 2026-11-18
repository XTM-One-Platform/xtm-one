"""The flow_lab tool group (#8260): what each surface registers and who may write."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

# ── Tools ──────────────────────────────────────────────────────────────────


def test_the_mcp_tool_set_registers_the_reads_only():
    from app.services.builtin_tools.flow_lab import FLOW_LAB_READ_TOOLS, build_flow_lab_tools

    read_only, executor = build_flow_lab_tools("", read_only=True)
    assert {d.name for d in read_only} == FLOW_LAB_READ_TOOLS
    assert {d.name for d in build_flow_lab_tools("")[0]} - FLOW_LAB_READ_TOOLS == {
        "create_sandbox_flow",
        "update_sandbox_flow",
        "run_flow_shadow",
        "record_flow_finding",
        "draft_flow_variant",
    }
    assert "run_flow_shadow" not in executor._handlers


@pytest.mark.parametrize("handler", ["_create_sandbox_flow", "_update_sandbox_flow", "_run_flow_shadow"])
async def test_a_person_never_writes_through_the_flow_lab(handler):
    from app.services.builtin_tools import flow_lab as tools

    answer = await getattr(tools, handler)(None, SimpleNamespace(is_concierge=False))
    assert answer == tools._CONCIERGE_ONLY


async def test_a_sandbox_flow_shadows_only_an_assignment_whose_agent_reaches_what_it_names(monkeypatch):
    from app.services.builtin_tools import flow_lab as tools
    from app.services.health import consent, flow_shadow
    from app.services.health import flow_lab_access as access

    agent_id, own, foreign = uuid.uuid4(), str(uuid.uuid4()), str(uuid.uuid4())
    assignment = SimpleNamespace(agent_id=agent_id, trigger_config={"owner_agent_id": own})
    flow = SimpleNamespace(graph={})
    started: list = []

    async def resolve_assignment(_db, _caller, _ref):
        return assignment

    async def resolve_sandbox_flow(_db, _caller, _ref):
        return flow

    async def sandbox_reach(_db, *, source_flow, basis_agent_id):
        assert basis_agent_id == agent_id
        return {str(agent_id)}

    async def readable(_db, **_kwargs):
        return True

    async def start_shadow(_db, **kwargs):
        started.append(kwargs)
        raise flow_shadow.ShadowError("stop here")

    class Db:
        async def rollback(self):
            pass

    monkeypatch.setattr(access, "resolve_assignment", resolve_assignment)
    monkeypatch.setattr(access, "resolve_sandbox_flow", resolve_sandbox_flow)
    monkeypatch.setattr(access, "sandbox_reach", sandbox_reach)
    monkeypatch.setattr(consent, "content_readable", readable)
    monkeypatch.setattr(flow_shadow, "start_shadow", start_shadow)
    caller = SimpleNamespace(is_concierge=True)

    flow.graph = {"nodes": [{"id": "t", "kind": "trigger.work_item", "config": {"owner_agent_id": foreign}}]}
    assert await tools._run_flow_shadow(Db(), caller, "a", "f") == (
        "Error: the sandbox flow names entities this assignment's agent does not use, so it cannot shadow it."
    )
    assert started == []
    flow.graph = {"nodes": [{"id": "t", "kind": "trigger.work_item", "config": {"owner_agent_id": own}}]}
    assert await tools._run_flow_shadow(Db(), caller, "a", "f") == "Error: stop here"
    assert len(started) == 1


@pytest.mark.parametrize("graph", [None, "not json", "[1, 2]", 42])
def test_a_graph_that_is_not_an_object_is_refused(graph):
    from app.services.builtin_tools.flow_lab import _checked_graph

    checked, refusal = _checked_graph(graph)
    assert checked is None
    assert refusal == "Error: the graph must be a JSON object (see get_flow_node_catalog)."


def test_an_invalid_graph_is_refused_with_its_errors():
    from app.services.builtin_tools.flow_lab import _checked_graph

    checked, refusal = _checked_graph({"nodes": [{"id": "x", "kind": "no.such.kind", "config": {}}], "edges": []})
    assert checked is None
    assert refusal.startswith("Error: the graph cannot be saved as it stands:\n- ")


def test_a_sandbox_graph_may_only_name_what_its_source_reaches():
    from app.services.builtin_tools.flow_lab import _reach_refusal

    known, foreign = str(uuid.uuid4()), str(uuid.uuid4())

    def graph(*ids):
        return {"nodes": [{"id": "n", "kind": "integration", "config": {"integration_id": i}} for i in ids]}

    assert _reach_refusal(graph(known), {known}) is None
    assert _reach_refusal(graph(known, foreign), {known}).startswith("Error: the graph names entities its source does not use.")


async def test_a_turn_s_tools_are_the_concierge_s_only_for_the_calling_agent(monkeypatch):
    """The wiring hands the calling agent to the caller check: a run's inline agent reads as its runner."""
    from app.services.builtin_tools import flow_lab as tools
    from app.services.health import flow_lab_access

    seen: list[tuple] = []

    async def resolve_caller(_db, user_id, *, agent_id=None):
        seen.append((user_id, agent_id))
        return None

    monkeypatch.setattr(flow_lab_access, "resolve_caller", resolve_caller)
    monkeypatch.setattr(tools, "_enabled", lambda: True)
    added: list = []
    composite = SimpleNamespace(add_tools=lambda _definitions, executor: added.append(executor))
    agent_id = str(uuid.uuid4())
    tools.add_flow_lab_tools(composite, "", [tools.FLOW_LAB_GROUP_ID], agent_id=agent_id)
    (executor,) = added
    assert await executor._handlers["get_flow_node_catalog"]() == tools._NO_CALLER
    assert seen == [(None, agent_id)]


@pytest.mark.parametrize("from_flow", ["", "f"])
async def test_a_sandbox_flow_needs_the_consent_of_what_it_stands_for(monkeypatch, from_flow):
    """Scope alone is not enough: the basis agent, and a copied flow, must be
    opted in with their owner not opted out (the sandbox's own rule)."""
    from app.services.builtin_tools import flow_lab as tools
    from app.services.health import flow_lab_access as access
    from app.services.health.sandbox import copies
    from app.services.health.sandbox import tools as sandbox_tools

    agent = SimpleNamespace(id=uuid.uuid4())
    flow = SimpleNamespace(id=uuid.uuid4())
    asked: list = []

    async def resolve_agent(_db, _caller, _ref):
        return agent

    async def resolve_flow(_db, _caller, _ref):
        return flow

    async def refused(_db, kind, row_id):
        asked.append((kind, row_id))
        return "not opted in"

    async def never(*_args, **_kwargs):
        raise AssertionError("a copy was made")

    monkeypatch.setattr(tools, "_checked_graph", lambda graph: (graph, None))
    monkeypatch.setattr(access, "resolve_agent", resolve_agent)
    monkeypatch.setattr(access, "resolve_flow", resolve_flow)
    monkeypatch.setattr(sandbox_tools, "subject_refusal", refused)
    monkeypatch.setattr(copies, "create_sandbox_copy", never)
    caller = SimpleNamespace(is_concierge=True, concierge=SimpleNamespace(agent_id=uuid.uuid4()))

    answer = await tools._create_sandbox_flow(None, caller, graph={}, from_flow=from_flow, basis_agent="a")
    assert answer == tools._not_found("flow" if from_flow else "agent")
    assert asked == ([("flow", flow.id)] if from_flow else [("agent", agent.id)])
