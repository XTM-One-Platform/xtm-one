"""An assignment's declared tool groups are on the wire from the first request.

``trigger_config.preload_tool_groups`` names the groups every run of the
assignment needs, so the tool list (the head of the provider's cached prompt
prefix) never changes mid-run through a ``load_tools`` call, which would
invalidate the whole cached prefix.
"""

from __future__ import annotations

from app.services.assignment_dispatch import PRELOAD_TOOL_GROUPS_KEY, preload_tool_groups
from app.tools.base import ToolDefinition
from app.tools.tool_gating import build_tool_gate


def _tools(provider: str, n: int) -> list[ToolDefinition]:
    return [
        ToolDefinition(name=f"{provider}_tool_{i}", description=f"{provider} {i}", source=f"integration:{provider}")
        for i in range(n)
    ]


def _gate():
    tools = [
        ToolDefinition(name="search_knowledge", description="kb", source="builtin:knowledge"),
        *_tools("github", 6),
        *_tools("jira", 6),
        *_tools("notion", 6),
    ]
    gate = build_tool_gate(tools, enabled=True, min_tools=1)
    assert gate is not None
    return gate


def test_declared_groups_are_revealed_before_the_first_request():
    gate = _gate()
    revealed = gate.preload_groups(["integration:github", "jira"])
    assert revealed == ["integration:github", "integration:jira"]
    visible = {t.name for t in gate.visible_tools()}
    assert {"github_tool_0", "jira_tool_5"} <= visible
    assert not any(name.startswith("notion_") for name in visible), "an undeclared group stays gated"
    assert gate.preloaded_group_ids == revealed


def test_a_group_the_agent_does_not_hold_preloads_nothing():
    gate = _gate()
    assert gate.preload_groups(["integration:slack", "nope"]) == []
    assert not any(t.name.startswith(("github_", "jira_", "notion_")) for t in gate.visible_tools())


def test_a_malformed_declaration_preloads_nothing():
    gate = _gate()
    assert gate.preload_groups(42) == []
    assert gate.preload_groups(None) == []
    assert gate.preload_groups({"integration:github": True}) == []


def test_trigger_config_reader_is_lenient_and_absent_means_none():
    assert PRELOAD_TOOL_GROUPS_KEY == "preload_tool_groups"
    assert preload_tool_groups(None) == []
    assert preload_tool_groups({}) == []
    assert preload_tool_groups({"preload_tool_groups": "integration:github"}) == ["integration:github"]
    assert preload_tool_groups(
        {"preload_tool_groups": [" integration:github ", "integration:github", "", 3, None, "jira"]}
    ) == ["integration:github", "jira"]
    assert preload_tool_groups({"preload_tool_groups": {"a": 1}}) == []
