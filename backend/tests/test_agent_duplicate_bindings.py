"""Unit tests for the binding filter a duplicate and the company-managed switch
share. The copier's visibility is exercised against PostgreSQL in
``test_agent_duplicate_bindings_postgres.py``."""

from __future__ import annotations

import uuid

import pytest

from app.api.v1 import agents as agents_api
from app.models.agent import Agent
from app.services import agent_bindings


def _ids(n: int) -> list[str]:
    return [str(uuid.uuid4()) for _ in range(n)]


def test_dropped_bindings_are_reported_as_counts_only():
    warnings = agents_api._copy_warnings(
        {"mcp_server_ids": 2, "integration_bindings": 1, "prompt_ids": 0}
    )

    assert warnings == [
        "2 MCP servers of the original agent were not copied: you cannot use them.",
        "1 integration of the original agent was not copied: you cannot use it.",
    ]
    assert agents_api._copy_warnings({"assignment_tool_sources": 1}) == [
        "1 assignment MCP server of the original agent was not copied: you cannot use it."
    ]


def test_retain_keeps_the_listed_ids_and_the_maps_keyed_by_them():
    servers, skills, agents, integrations = _ids(2), _ids(2), _ids(2), _ids(2)
    agent = Agent(
        mcp_server_ids=list(servers),
        mcp_allowed_tools={servers[0]: ["a"], servers[1]: ["b"]},
        skill_ids=list(skills),
        skill_load_modes={skills[1]: "always"},
        handover_agent_ids=list(agents),
        handover_types={agents[0]: "consult", agents[1]: "transfer"},
        handover_conditions={agents[1]: "when asked"},
        integration_bindings={"github": [integrations[0]], "slack": [integrations[1]]},
        prompt_ids=[str(uuid.uuid4())],
    )

    dropped = agent_bindings.retain_agent_bindings(
        agent,
        {
            "mcp_server_ids": {servers[0]},
            "skill_ids": {skills[0]},
            "handover_agent_ids": {agents[0]},
            "integration_bindings": {integrations[1]},
            "prompt_ids": set(),
        },
    )

    assert agent.mcp_server_ids == [servers[0]]
    assert agent.mcp_allowed_tools == {servers[0]: ["a"]}
    assert agent.skill_ids == [skills[0]]
    assert agent.skill_load_modes is None
    assert agent.handover_agent_ids == [agents[0]]
    assert agent.handover_types == {agents[0]: "consult"}
    assert agent.handover_conditions is None
    assert agent.integration_bindings == {"slack": [integrations[1]]}
    assert agent.prompt_ids is None
    assert dropped == {
        "mcp_server_ids": 1,
        "skill_ids": 1,
        "handover_agent_ids": 1,
        "integration_bindings": 1,
        "prompt_ids": 1,
    }


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _CompanyDB:
    """Answers each company-managed lookup with the ids listed for its table."""

    def __init__(self, company_ids_by_table):
        self._by_table = company_ids_by_table

    async def execute(self, stmt):
        table = stmt.get_final_froms()[0].name
        return _Rows([(uuid.UUID(i),) for i in self._by_table.get(table, [])])


@pytest.mark.asyncio
async def test_switching_to_company_managed_still_strips_personal_bindings_only():
    servers, kbs, integrations, agents, tools = _ids(2), _ids(2), _ids(2), _ids(2), _ids(1)
    agent = Agent(
        mcp_server_ids=list(servers),
        mcp_allowed_tools={servers[1]: ["x"]},
        knowledge_base_ids=list(kbs),
        integration_bindings={"github": list(integrations)},
        handover_agent_ids=list(agents),
        handover_types={agents[1]: "consult"},
        custom_tool_ids=list(tools),
    )
    db = _CompanyDB(
        {
            "mcp_servers": [servers[0]],
            "knowledge_bases": [kbs[1]],
            "integrations": [integrations[0]],
            "agents": [],
        }
    )

    await agent_bindings.strip_personal_bindings(db, agent)

    assert agent.mcp_server_ids == [servers[0]]
    assert agent.mcp_allowed_tools is None
    assert agent.knowledge_base_ids == [kbs[1]]
    assert agent.integration_bindings == {"github": [integrations[0]]}
    assert agent.handover_agent_ids is None
    assert agent.handover_types is None and agent.handover_conditions is None
    # Custom tools stay exempt.
    assert agent.custom_tool_ids == tools
