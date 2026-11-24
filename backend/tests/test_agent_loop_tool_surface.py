"""The loop's per-run tool maps come from one builder (#1874).

A refresh of the tool surface inside a turn rebuilds every map through
``_build_tool_surface``; these tests pin what that builder answers so the
refactor keeps the loop's behaviour.
"""

from __future__ import annotations

from app.tools.agent_loop import _build_tool_surface
from app.tools.base import ToolDefinition


def _tool(name: str, *, execution_timeout: int = 0) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Tool {name}",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        execution_timeout=execution_timeout,
    )


def test_no_tools_gives_an_empty_surface() -> None:
    surface = _build_tool_surface(None)
    assert surface.effective_tools is None
    assert surface.schema_map == {}
    assert surface.coalescing_map == {}
    assert surface.execution_timeouts == {}
    assert surface.send_guard_map == {}
    assert surface.read_back_map == {}


def test_a_sanitised_name_maps_back_to_the_original() -> None:
    surface = _build_tool_surface([_tool("search.github.issues"), _tool("plain_tool")])
    names = [t.name for t in surface.effective_tools or []]
    assert names == ["search_github_issues", "plain_tool"]
    assert surface.name_map.get("search_github_issues") == "search.github.issues"
    assert surface.name_map.get("plain_tool", "plain_tool") == "plain_tool"
    assert set(surface.schema_map) == {"search_github_issues", "plain_tool"}


def test_the_declared_timeouts_are_kept_by_visible_name() -> None:
    surface = _build_tool_surface([_tool("slow.tool", execution_timeout=600), _tool("fast_tool")])
    assert surface.execution_timeouts == {"slow_tool": 600}


def test_two_builds_of_the_same_list_agree() -> None:
    tools = [_tool("a.b"), _tool("a/b"), _tool("c")]
    first, second = _build_tool_surface(tools), _build_tool_surface(tools)
    assert [t.name for t in first.effective_tools or []] == [t.name for t in second.effective_tools or []]
    assert dict(first.name_map) == dict(second.name_map)
