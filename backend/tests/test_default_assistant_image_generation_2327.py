"""The default assistants offer image generation again (#2327, regression of #1842).

Since #1842 the seed stores a definition's former ids canonically. A default
assistant that named ``generate_file`` alone was stored as ``files`` scoped to
what ``generate_file`` granted, and ``runtime_builtin_tool_ids`` applied that
scope to a chat session's selection too: ``generate_image`` was never wired.
The default assistants now name both former ids of ``files``, so the group is
stored unscoped; and a group the fold scopes without a list in the definition
is marked platform-written, so a later release's functions reach the agent.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agents.builtin_agents import BUILTIN_AGENTS, DEFAULT_ASSISTANT_SLUGS
from app.agents.seed import _sync_agent_fields
from app.models.agent import Agent
from app.services.builtin_allowed_tools import implicit_scope_groups
from app.services.builtin_tool_aliases import runtime_builtin_tool_ids


def _definition(slug: str) -> dict[str, Any]:
    return next(agent for agent in BUILTIN_AGENTS if agent["slug"] == slug)


def _seeded(data: dict[str, Any]) -> Agent:
    agent = Agent(slug=data["slug"], name=data["name"])
    _sync_agent_fields(agent, data)
    return agent


@pytest.mark.parametrize("slug", DEFAULT_ASSISTANT_SLUGS)
def test_every_default_assistant_stores_files_unscoped(slug: str) -> None:
    agent = _seeded(_definition(slug))
    assert "files" in (agent.builtin_tool_ids or []), slug
    assert "files" not in (agent.builtin_allowed_tools or {}), slug


@pytest.mark.parametrize("slug", DEFAULT_ASSISTANT_SLUGS)
def test_a_chat_turn_with_files_on_wires_image_generation(slug: str) -> None:
    agent = _seeded(_definition(slug))
    wired = runtime_builtin_tool_ids(agent.builtin_tool_ids, agent.builtin_allowed_tools)
    assert "generate_image" in wired, slug
    assert "generate_file" in wired, slug


def test_a_session_selecting_image_generation_gets_it_on_the_ctem_assistant() -> None:
    agent = _seeded(_definition("ctem-assistant"))
    wired = runtime_builtin_tool_ids(["generate_image"], agent.builtin_allowed_tools)
    assert "generate_image" in wired


def test_a_session_selecting_files_gets_image_generation_on_the_ctem_assistant() -> None:
    agent = _seeded(_definition("ctem-assistant"))
    wired = runtime_builtin_tool_ids(["files"], agent.builtin_allowed_tools)
    assert "generate_image" in wired


def test_a_group_the_seed_scopes_is_marked_platform_written() -> None:
    agent = _seeded({"slug": "x-desk", "name": "X", "builtin_tool_ids": ["shared_state", "generate_file"]})
    assert set(agent.builtin_allowed_tools or {}) == {"coordination", "files"}
    assert set(implicit_scope_groups(agent.config)) == {"coordination", "files"}


def test_a_list_the_definition_names_is_not_marked() -> None:
    agent = _seeded(
        {
            "slug": "y-desk",
            "name": "Y",
            "builtin_tool_ids": ["files"],
            "builtin_allowed_tools": {"files": ["generate_file"]},
        }
    )
    assert agent.builtin_allowed_tools == {"files": ["generate_file"]}
    assert implicit_scope_groups(agent.config) == []


def test_an_unscoped_definition_keeps_a_config_without_the_marker() -> None:
    agent = _seeded({"slug": "z-desk", "name": "Z", "builtin_tool_ids": ["coordination"], "config": {"k": 1}})
    assert agent.config == {"k": 1}
