"""The Flow Lab's access helpers on fixed values (#8260): who calls, the ids a graph names, a prototype's basis agent."""

from __future__ import annotations

import uuid


async def test_an_agent_its_run_executes_inline_reads_as_its_runner(monkeypatch):
    """Only the principal's own agent is Platform Health; a sandbox copy's turn or a consulted agent is not."""
    from app.services.health import identity
    from app.services.health.flow_lab_access import resolve_caller
    from app.services.health.identity import HealthPrincipal, acting_as_health_principal

    monkeypatch.setattr(identity, "health_enabled", lambda: True)
    principal = HealthPrincipal(agent_id=uuid.uuid4(), role="flow_lab", on_behalf_of_admin_id=None)
    with acting_as_health_principal(principal):
        caller = await resolve_caller(None, None, agent_id=str(principal.agent_id))
        assert caller is not None and caller.principal is principal
        # No user to read for: the runner here is nobody.
        assert await resolve_caller(None, None, agent_id=str(uuid.uuid4())) is None
        assert await resolve_caller(None, None) is None


def test_a_graph_names_the_ids_of_its_node_configs_only():
    from app.services.health.flow_lab_access import node_config_ids

    integration, variable, node = (str(uuid.uuid4()) for _ in range(3))
    graph = {
        "nodes": [
            {"id": node, "kind": "integration", "config": {"integration_id": integration}},
            {"id": "n2", "kind": "agent", "config": {"prompt": f"Use {{{{var_id.{variable}}}}} today"}},
        ],
        "edges": [{"id": str(uuid.uuid4()), "source": node, "target": "n2"}],
    }
    assert node_config_ids(graph) == {integration, variable}


def test_a_prototype_remembers_the_agent_whose_reach_it_keeps():
    from app.services.health.flow_lab_access import BASIS_AGENT_PREFIX, basis_agent_of

    agent_id = uuid.uuid4()
    assert basis_agent_of(f"{BASIS_AGENT_PREFIX}{agent_id}") == agent_id
    assert basis_agent_of("flow_lab") is None
    assert basis_agent_of(None) is None
