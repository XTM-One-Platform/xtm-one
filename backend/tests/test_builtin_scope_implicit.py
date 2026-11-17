"""A built-in allow list the platform wrote, not a person, never freezes the group (#39).

A former id folded into its group, or a group a fleet role granted scoped, is
marked in the agent's ``config``; a release that adds a function to such a
group grants it and sends no "outside the allowlist" notice. A list a person
writes leaves the marker and keeps least privilege.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import builtin_tool_release_notice as notice_mod
from app.services.builtin_allowed_tools import (
    IMPLICIT_SCOPE_CONFIG_KEY,
    groups_a_person_changed,
    implicit_scope_groups,
    record_person_scope,
    with_implicit_scope,
)
from app.services.builtin_tool_aliases import fold_builtin_selection, platform_scoped_groups
from app.services.fleets import grant_group_functions

_NEW = "fixture_new_platform_tool"


def _agent(ids, allowed, config=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="A",
        user_id=str(uuid.uuid4()),
        builtin_tool_ids=ids,
        builtin_allowed_tools=allowed,
        config=config,
    )


def test_marker_round_trip_drops_the_key_when_empty() -> None:
    config = with_implicit_scope({"x": 1}, add=["platform", "platform", "coordination"])
    assert implicit_scope_groups(config) == ["platform", "coordination"]
    assert with_implicit_scope(config, remove=["platform", "coordination"]) == {"x": 1}
    assert implicit_scope_groups(None) == []


def test_a_former_id_fold_marks_the_group_it_scopes() -> None:
    agent = _agent(["fleets"], None, {})
    assert fold_builtin_selection(agent)
    assert agent.builtin_tool_ids == ["platform"]
    assert agent.builtin_allowed_tools and agent.builtin_allowed_tools.get("platform")
    assert implicit_scope_groups(agent.config) == ["platform"]


def test_a_list_a_person_named_is_not_marked() -> None:
    assert platform_scoped_groups({"platform": ["list_agents"]}, {"platform": ["list_agents"]}) == []
    assert platform_scoped_groups({"fleets": ["list_fleets"]}, {"platform": ["list_fleets"]}) == []
    assert platform_scoped_groups(None, {"platform": ["list_fleets"]}) == ["platform"]


def test_a_fleet_grant_marks_a_group_it_adds_scoped_only() -> None:
    added = _agent([], None, {})
    assert grant_group_functions(added, "coordination", ("list_programs",))
    assert implicit_scope_groups(added.config) == ["coordination"]
    unscoped = _agent(["coordination"], None, {})
    assert not grant_group_functions(unscoped, "coordination", ("list_programs",))
    assert IMPLICIT_SCOPE_CONFIG_KEY not in unscoped.config


def test_a_person_writing_the_list_takes_it_over() -> None:
    agent = _agent(["platform"], {"platform": ["list_agents", "get_agent"]}, with_implicit_scope({}, add=["platform"]))
    before = agent.builtin_allowed_tools
    agent.builtin_allowed_tools = {"platform": ["get_agent", "list_agents"]}  # same list, other order
    record_person_scope(agent, before)
    assert implicit_scope_groups(agent.config) == ["platform"]
    agent.builtin_allowed_tools = {"platform": ["list_agents"]}
    record_person_scope(agent, before)
    assert implicit_scope_groups(agent.config) == []
    assert groups_a_person_changed({"a": ["x"]}, None) == ["a"]


def test_the_release_notice_skips_platform_written_lists() -> None:
    marked = _agent(["platform"], {"platform": ["list_agents"]}, with_implicit_scope({}, add=["platform"]))
    person = _agent(["platform"], {"platform": ["list_agents"]}, {})
    added = {"platform": [_NEW]}
    assert [a.agent_id for a in notice_mod.agents_left_out(added, [marked, person])] == [str(person.id)]
    assert notice_mod.implicit_left_out(marked, added) == {"platform": [_NEW]}
    assert notice_mod.implicit_left_out(person, added) == {}
    assert notice_mod.granted_allowlist(marked, {"platform": [_NEW]}) == {"platform": ["list_agents", _NEW]}


class _Db:
    def __init__(self) -> None:
        self.statements: list[object] = []

    async def execute(self, statement):
        self.statements.append(statement)


@pytest.mark.asyncio
async def test_the_release_grants_new_functions_to_platform_written_lists() -> None:
    marked = _agent(["platform"], {"platform": ["list_agents"]}, with_implicit_scope({}, add=["platform"]))
    person = _agent(["platform"], {"platform": ["list_agents"]}, {})
    db = _Db()
    assert await notice_mod.grant_implicit(db, {"platform": [_NEW]}, [marked, person]) == 1
    assert len(db.statements) == 1
    assert await notice_mod.grant_implicit(db, {}, [marked]) == 0
