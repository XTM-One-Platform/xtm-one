"""Seeded agents are stored under canonical built-in group ids (#1837, gap ctem-seed-agent-hygiene).

A definition may still name a [DEPRECATED] former id (``work_items``, ``web_fetch``...).
The seed writes it as its group, scoped to the functions the former id granted, so the
row reads as the builder shows it and the runtime wires exactly the same tools.
"""

from app.agents.builtin_agents import BUILTIN_AGENTS
from app.agents.seed import _sync_agent_fields
from app.models.agent import Agent
from app.services.builtin_tool_aliases import is_former_builtin_id, runtime_builtin_tool_ids


def _seeded(data: dict) -> Agent:
    agent = Agent(slug=data["slug"], name=data["name"])
    _sync_agent_fields(agent, data)
    return agent


def test_former_ids_are_stored_as_their_group_scoped_to_what_they_granted() -> None:
    data = {
        "slug": "ctem-threat-relevance-analyst",
        "name": "X",
        "builtin_tool_ids": ["shared_state", "work_items", "web_fetch"],
    }
    agent = _seeded(data)
    assert agent.builtin_tool_ids == ["coordination", "web"]
    assert set(agent.builtin_allowed_tools) == {"coordination", "web"}
    assert "workset_add_item" not in agent.builtin_allowed_tools["coordination"]
    assert "browser" not in agent.builtin_allowed_tools["web"]


def test_canonical_ids_wire_the_same_functions_as_the_former_ids() -> None:
    for data in BUILTIN_AGENTS:
        ids = data.get("builtin_tool_ids") or []
        if not any(is_former_builtin_id(str(i)) for i in ids):
            continue
        agent = _seeded(data)
        before = set(runtime_builtin_tool_ids(ids, data.get("builtin_allowed_tools")))
        after = set(runtime_builtin_tool_ids(agent.builtin_tool_ids, agent.builtin_allowed_tools))
        assert before == after, data["slug"]


def test_no_seeded_ctem_agent_is_stored_with_a_former_id() -> None:
    for data in BUILTIN_AGENTS:
        if "ctem" not in (data.get("tags") or []):
            continue
        agent = _seeded(data)
        assert not [i for i in agent.builtin_tool_ids if is_former_builtin_id(i)], data["slug"]


def test_a_canonical_definition_is_left_unscoped() -> None:
    agent = _seeded({"slug": "ctem-risk-quantifier", "name": "Y", "builtin_tool_ids": ["coordination"]})
    assert agent.builtin_tool_ids == ["coordination"]
    assert agent.builtin_allowed_tools is None
