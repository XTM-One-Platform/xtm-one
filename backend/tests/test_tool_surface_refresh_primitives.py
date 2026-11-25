"""The registries a tool surface refreshed inside the turn rewrites (#1874).

A refresh drops the operations an integration no longer carries and swaps the
allowed set of the scoped executor for the one the caller's filters rebuilt:
the resolver, the composite and the scope must forget a dropped name at once.
"""

from __future__ import annotations

import pytest

from app.tools.base import (
    CompositeToolExecutor,
    FileContext,
    ScopedToolExecutor,
    ToolCall,
    ToolDefinition,
    ToolNameResolver,
    ToolResult,
)


def _tool(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Tool {name}",
        input_schema={"type": "object", "properties": {}},
    )


class _Echo:
    file_context: FileContext | None = None

    async def execute_tool(self, call: ToolCall) -> ToolResult:
        return ToolResult(tool_call_id=call.id, content=f"ran {call.name}")


def test_a_discarded_name_no_longer_resolves_by_any_rule() -> None:
    resolver = ToolNameResolver()
    resolver.add("search_jira_issues", aliases=["list_jira_issues"])
    resolver.add("get_jira_issue")
    resolver.discard("search_jira_issues")
    assert "search_jira_issues" not in resolver
    assert resolver.canonical("jira_search_issues") is None
    assert resolver.canonical("list_jira_issues") is None
    assert resolver.canonical("get_jira_issue") == "get_jira_issue"
    resolver.discard("never_added")
    assert len(resolver) == 1


def test_discard_keeps_a_token_sibling_resolvable() -> None:
    resolver = ToolNameResolver(["a_b", "b_a"])
    assert resolver.canonical("a-b-x") is None
    resolver.discard("b_a")
    assert resolver.token_matches("b_a") == ["a_b"]


def test_remove_tools_drops_definition_executor_and_name() -> None:
    composite = CompositeToolExecutor()
    composite.add_tools([_tool("op_list"), _tool("op_delete")], _Echo())
    composite.remove_tools(["op_delete", "unknown"])
    assert [t.name for t in composite.all_tools] == ["op_list"]
    assert composite.tool_names == ["op_list"]
    assert "op_delete" not in composite.name_resolver


@pytest.mark.asyncio
async def test_replace_allowed_swaps_what_the_scope_forwards() -> None:
    composite = CompositeToolExecutor()
    composite.add_tools([_tool("op_list"), _tool("op_create")], _Echo())
    scoped = ScopedToolExecutor(composite, [_tool("op_list")])
    assert not scoped.allows("op_create")
    scoped.replace_allowed([_tool("op_create")])
    assert scoped.allows("op_create")
    assert not scoped.allows("op_list")
    result = await scoped.execute_tool(ToolCall(id="c1", name="op_create", arguments={}))
    assert result.content == "ran op_create"
