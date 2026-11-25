"""A refresh inside the turn replays the caller's own build and narrowing (#1874)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from app.services.tool_surface_refresh import IntegrationSurfaceRefresher, integration_tool_names
from app.tools.base import (
    CompositeToolExecutor,
    FileContext,
    ScopedToolExecutor,
    ToolCall,
    ToolDefinition,
    ToolExecutor,
    ToolResult,
)


def _tool(name: str, source: str = "builtin") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Tool {name}",
        input_schema={"type": "object", "properties": {}},
        source=source,
    )


class _Echo:
    file_context: FileContext | None = None

    def __init__(self, label: str) -> None:
        self.label = label

    async def execute_tool(self, call: ToolCall) -> ToolResult:
        return ToolResult(tool_call_id=call.id, content=f"{self.label} ran {call.name}")


def _deny(names: set[str]) -> Callable[[list[ToolDefinition]], list[ToolDefinition]]:
    def narrow(tools: list[ToolDefinition]) -> list[ToolDefinition]:
        return [t for t in tools if t.name not in names]

    return narrow


def test_integration_tool_names_reads_the_source() -> None:
    tools = [_tool("load_skill"), _tool("api_list", "integration:openapi")]
    assert integration_tool_names(tools) == {"api_list"}


@pytest.mark.asyncio
async def test_refresh_adds_new_operations_and_drops_gone_ones() -> None:
    composite = CompositeToolExecutor()
    composite.add_tools([_tool("load_skill")], _Echo("builtin"))
    composite.add_tools(
        [_tool("api_list", "integration:openapi"), _tool("api_old", "integration:openapi")], _Echo("v1")
    )
    narrow = _deny(set())
    scoped = ScopedToolExecutor(composite, narrow(composite.all_tools))

    async def rebuild() -> tuple[list[ToolDefinition], ToolExecutor]:
        return [_tool("api_list", "integration:openapi"), _tool("api_new", "integration:openapi")], _Echo("v2")

    refresh = await IntegrationSurfaceRefresher(composite, scoped, rebuild, narrow).refresh()

    assert refresh.added == ("api_new",)
    assert refresh.removed == ("api_old",)
    assert sorted(t.name for t in refresh.tools) == ["api_list", "api_new", "load_skill"]
    assert not scoped.allows("api_old")
    result = await scoped.execute_tool(ToolCall(id="c1", name="api_new", arguments={}))
    assert result.content == "v2 ran api_new"
    # A built-in tool is never touched by an integration refresh.
    assert scoped.allows("load_skill")


@pytest.mark.asyncio
async def test_refresh_never_widens_past_the_callers_narrowing() -> None:
    composite = CompositeToolExecutor()
    composite.add_tools([_tool("api_list", "integration:openapi")], _Echo("v1"))
    narrow = _deny({"api_delete"})
    scoped = ScopedToolExecutor(composite, narrow(composite.all_tools))

    async def rebuild() -> tuple[list[ToolDefinition], ToolExecutor]:
        return [_tool("api_list", "integration:openapi"), _tool("api_delete", "integration:openapi")], _Echo("v2")

    refresh = await IntegrationSurfaceRefresher(composite, scoped, rebuild, narrow).refresh()

    assert refresh.added == ()
    assert not scoped.allows("api_delete")
    result = await scoped.execute_tool(ToolCall(id="c2", name="api_delete", arguments={}))
    assert result.is_error


def test_follows_only_the_agent_the_run_serves() -> None:
    composite = CompositeToolExecutor()
    scoped = ScopedToolExecutor(composite, [])

    async def rebuild() -> tuple[list[ToolDefinition], ToolExecutor]:
        return [], _Echo("none")

    refresher = IntegrationSurfaceRefresher(composite, scoped, rebuild, _deny(set()), agent_id="agent-a")
    assert refresher.follows("agent-a")
    assert not refresher.follows("agent-b")
    assert not refresher.follows(None)
    # A caller that names no agent never refreshes on an update_agent result.
    assert not IntegrationSurfaceRefresher(composite, scoped, rebuild, _deny(set())).follows("agent-a")


@pytest.mark.asyncio
async def test_refresh_adds_a_builtin_group_granted_in_the_run() -> None:
    composite = CompositeToolExecutor()
    composite.add_tools([_tool("load_skill")], _Echo("builtin"))
    narrow = _deny({"delete_user"})
    scoped = ScopedToolExecutor(composite, narrow(composite.all_tools))

    async def rebuild() -> tuple[list[ToolDefinition], ToolExecutor]:
        return [], _Echo("none")

    async def rebuild_builtins() -> list[tuple[list[ToolDefinition], ToolExecutor]]:
        return [
            ([_tool("load_skill")], _Echo("again")),
            ([_tool("list_users"), _tool("delete_user")], _Echo("user_admin")),
        ]

    refresh = await IntegrationSurfaceRefresher(
        composite, scoped, rebuild, narrow, agent_id="agent-a", rebuild_builtins=rebuild_builtins
    ).refresh()

    assert refresh.added == ("list_users",)
    assert refresh.removed == ()
    result = await scoped.execute_tool(ToolCall(id="c3", name="list_users", arguments={}))
    assert result.content == "user_admin ran list_users"
    # The held built-in keeps its own executor; the narrowing still holds.
    result = await scoped.execute_tool(ToolCall(id="c4", name="load_skill", arguments={}))
    assert result.content == "builtin ran load_skill"
    assert not scoped.allows("delete_user")
