"""Every fleet orchestrator reads its programs live (#10979)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents.builtin_agents import BUILTIN_AGENTS
from app.agents.builtin_fleets import BUILTIN_FLEETS
from app.services.builtin_tool_aliases import (
    alias_scoped_functions,
    canonical_builtin_selection,
    runtime_builtin_tool_ids,
)
from app.services.fleets import ORCHESTRATOR_PROGRAM_FUNCTIONS, grant_program_status

PROGRAMS = set(ORCHESTRATOR_PROGRAM_FUNCTIONS)


def _agent(ids, allowed=None):
    return SimpleNamespace(builtin_tool_ids=list(ids), builtin_allowed_tools=allowed)


def _offers_programs(agent) -> bool:
    ids, allowed = canonical_builtin_selection(agent.builtin_tool_ids, agent.builtin_allowed_tools)
    if "coordination" not in ids:
        return False
    kept = (allowed or {}).get("coordination")
    return kept is None or set(kept) >= PROGRAMS


def test_no_coordination_gets_the_two_reads_alone():
    agent = _agent(["web"])
    assert grant_program_status(agent) is True
    assert agent.builtin_tool_ids == ["web", "coordination"]
    assert agent.builtin_allowed_tools == {"coordination": ["list_programs", "get_program_status"]}
    # The runtime wires them through the work_items former id, nothing more.
    assert "work_items" in runtime_builtin_tool_ids(agent.builtin_tool_ids, agent.builtin_allowed_tools)


def test_a_scoped_former_id_keeps_its_functions_and_adds_the_reads():
    agent = _agent(["shared_state"])
    assert grant_program_status(agent) is True
    kept = agent.builtin_allowed_tools["coordination"]
    assert set(alias_scoped_functions("shared_state")) <= set(kept)
    assert set(kept) >= PROGRAMS
    assert "create_work_item" not in kept


def test_an_unscoped_group_is_left_as_is():
    agent = _agent(["coordination"])
    assert grant_program_status(agent) is False
    assert agent.builtin_tool_ids == ["coordination"]
    assert agent.builtin_allowed_tools is None


def test_work_items_already_grants_them():
    agent = _agent(["work_items", "shared_state"])
    before = canonical_builtin_selection(agent.builtin_tool_ids, None)
    assert grant_program_status(agent) is False
    assert (agent.builtin_tool_ids, agent.builtin_allowed_tools) == (["work_items", "shared_state"], None)
    assert set(before[1]["coordination"]) >= PROGRAMS


def test_idempotent():
    agent = _agent(["web"])
    grant_program_status(agent)
    once = (list(agent.builtin_tool_ids), dict(agent.builtin_allowed_tools))
    assert grant_program_status(agent) is False
    assert (agent.builtin_tool_ids, agent.builtin_allowed_tools) == once


@pytest.mark.parametrize("fleet", BUILTIN_FLEETS, ids=lambda f: f["slug"])
def test_every_seeded_orchestrator_offers_the_program_reads(fleet):
    by_slug = {a["slug"]: a for a in BUILTIN_AGENTS}
    seed = by_slug[fleet["orchestrator"]]
    agent = _agent(seed.get("builtin_tool_ids") or [])
    grant_program_status(agent)
    assert _offers_programs(agent)
