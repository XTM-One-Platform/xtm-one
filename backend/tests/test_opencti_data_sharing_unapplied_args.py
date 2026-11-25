"""manage_opencti_data_sharing names fields the action does not apply as ignored; the action runs.

Same contract as the other manage_opencti_* tools: a field the chosen action ignores
(filters on a toggle, enabled=false on an update, search on a delete, an argument the
tool does not declare) is named in the answer as ignored and the action runs with the rest.
"""

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

import app.integrations.providers.opencti.data_sharing as data_sharing_mod
from app.integrations.providers.opencti import build_tools
from app.tools.base import CallableToolExecutor, ToolCall, ToolResult

TOKEN = "https://demo.opencti.io||test-token"
TOOL = "manage_opencti_data_sharing"


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
        return {}, None

    monkeypatch.setattr(data_sharing_mod, "_client", _client)
    monkeypatch.setattr(data_sharing_mod, "_graphql", _gql)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor, live_writes=True)
    return executor, calls


async def _run(executor: CallableToolExecutor, arguments: dict[str, Any]) -> ToolResult:
    return await executor.execute_tool(ToolCall(id="call-1", name=TOOL, arguments=arguments))


@pytest.mark.parametrize(
    ("arguments", "named"),
    [
        ({"action": "toggle", "share_type": "live_stream", "share_id": "s1", "filters": "{}"}, "filters"),
        ({"action": "update", "share_type": "feed", "share_id": "f1", "name": "n", "enabled": False}, "enabled"),
        ({"action": "delete", "share_type": "taxii_collection", "share_id": "t1", "search": "x"}, "search"),
        ({"action": "list", "share_type": "feed", "share_id": "f1"}, "share_id"),
        ({"action": "get", "share_type": "live_stream", "share_id": "s1", "limit": 5}, "limit"),
        ({"action": "create", "share_type": "taxii_collection", "name": "n", "public": True}, "public"),
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


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "list", "share_type": "live_stream"},
        {"action": "list", "share_type": "feed", "search": "x", "limit": 10},
        {"action": "toggle", "share_type": "live_stream", "share_id": "s1", "enabled": False},
        {"action": "toggle", "share_type": "taxii_collection", "share_id": "t1"},
        {"action": "update", "share_type": "taxii_collection", "share_id": "t1", "name": "n"},
        {"action": "delete", "share_type": "live_stream", "share_id": "s1"},
    ],
)
async def test_action_with_its_own_fields_runs(monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any]) -> None:
    executor, calls = _build(monkeypatch)
    result = await _run(executor, arguments)
    assert "does not apply" not in result.content, result.content
    assert calls.count == 1
