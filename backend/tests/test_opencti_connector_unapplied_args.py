"""manage_opencti_connector names fields the action does not apply as ignored; the action runs.

Same contract as the user, group, role, profile, ingestion, form, label, workspace,
notifier, trigger and notification tools: a field the chosen action ignores (config
on start, name on delete, tail on get, an argument the tool does not declare) is
named in the answer as ignored and the action runs with the rest.
"""

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

import app.integrations.providers.opencti.connectors as connectors_mod
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
        return {
            "connectors": [],
            "updateConnectorRequestedStatus": {"id": "c1", "name": "conn", "manager_requested_status": "starting"},
        }, None

    monkeypatch.setattr(connectors_mod, "_client", _client)
    monkeypatch.setattr(connectors_mod, "_graphql", _gql)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor, live_writes=True)
    return executor, calls


async def _run(executor: CallableToolExecutor, arguments: dict[str, Any]) -> ToolResult:
    return await executor.execute_tool(ToolCall(id="call-1", name="manage_opencti_connector", arguments=arguments))


@pytest.mark.parametrize(
    ("arguments", "named"),
    [
        ({"action": "start", "connector_id": "c1", "config": '{"a": 1}'}, "config"),
        ({"action": "delete", "connector_id": "c1", "name": "x"}, "name"),
        ({"action": "get", "connector_id": "c1", "tail": 20}, "tail"),
        ({"action": "list", "connector_id": "c1"}, "connector_id"),
        (
            {"action": "update_config", "connector_id": "c1", "name": "x", "config": "{}", "automatic_user": True},
            "automatic_user",
        ),
        ({"action": "stop", "connector_id": "c1", "force": True}, "force"),
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


async def test_start_with_its_own_field_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, calls = _build(monkeypatch)
    result = await _run(executor, {"action": "start", "connector_id": "c1"})
    assert "does not apply" not in result.content, result.content
    assert "is now starting" in result.content
    assert calls.count == 1


async def test_list_with_no_field_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, calls = _build(monkeypatch)
    result = await _run(executor, {"action": "list"})
    assert "does not apply" not in result.content, result.content
    assert calls.count == 1
