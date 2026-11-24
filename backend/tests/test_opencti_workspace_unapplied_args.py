"""manage_opencti_workspace names every field the action does not apply as ignored; the action runs.

Same contract as the user, group, role, profile, ingestion, form and label tools: a
field the chosen action ignores (name or tags on delete, entity_ids on update, or an
argument the tool does not declare) is named in the answer as ignored and the action
runs with the rest.
"""

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

import app.integrations.providers.opencti.workspaces as workspaces_mod
from app.integrations.providers.opencti import build_tools
from app.tools.base import CallableToolExecutor, ToolCall, ToolResult

TOKEN = "https://demo.opencti.io||test-token"


class _Calls:
    def __init__(self) -> None:
        self.count = 0


def _build(monkeypatch: pytest.MonkeyPatch) -> tuple[CallableToolExecutor, _Calls]:
    calls = _Calls()

    @contextlib.asynccontextmanager
    async def _client(*_args: Any, **_kwargs: Any) -> AsyncIterator[None]:
        yield None

    async def _gql(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], str | None]:
        calls.count += 1
        return {"workspaceDelete": "w1"}, None

    monkeypatch.setattr(workspaces_mod, "_client", _client)
    monkeypatch.setattr(workspaces_mod, "_graphql", _gql)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor, live_writes=True)
    return executor, calls


async def _run(executor: CallableToolExecutor, arguments: dict[str, Any]) -> ToolResult:
    return await executor.execute_tool(ToolCall(id="call-1", name="manage_opencti_workspace", arguments=arguments))


@pytest.mark.parametrize(
    ("arguments", "named"),
    [
        ({"action": "delete", "workspace_id": "w1", "name": "x"}, "name"),
        ({"action": "delete", "workspace_id": "w1", "tags": ["a"]}, "tags"),
        ({"action": "update", "workspace_id": "w1", "name": "x", "entity_ids": ["e1"]}, "entity_ids"),
        ({"action": "get", "workspace_id": "w1", "limit": 5}, "limit"),
        ({"action": "list", "status": "on"}, "status"),
    ],
)
async def test_unapplied_field_is_named_as_ignored_and_the_action_runs(
    monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any], named: str
) -> None:
    executor, _calls = _build(monkeypatch)
    result = await _run(executor, arguments)
    assert f"action '{arguments['action']}' does not apply" in result.content, result.content
    assert named in result.content
    assert "ignored and the rest ran" in result.content


async def test_delete_with_its_own_field_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, calls = _build(monkeypatch)
    result = await _run(executor, {"action": "delete", "workspace_id": "w1"})
    assert "does not apply" not in result.content, result.content
    assert "Deleted workspace w1" in result.content
    assert calls.count == 1


async def test_list_with_default_limit_is_not_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, calls = _build(monkeypatch)
    result = await _run(executor, {"action": "list", "limit": 20, "type": "dashboard"})
    assert "does not apply" not in result.content, result.content
    assert calls.count == 1
