"""manage_opencti_pir names fields the action does not apply as ignored; the action runs.

Same contract as the other manage_opencti_* tools: a field the chosen action ignores
(pir_filters or pir_type on an update, matching_criteria on a create, search on a
delete, an argument the tool does not declare) is named in the answer and nothing is
sent to OpenCTI.
"""

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

import app.integrations.providers.opencti.pir as pir_mod
from app.integrations.providers.opencti import build_tools
from app.tools.base import CallableToolExecutor, ToolCall, ToolResult

TOKEN = "https://demo.opencti.io||test-token"
TOOL = "manage_opencti_pir"


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

    monkeypatch.setattr(pir_mod, "_client", _client)
    monkeypatch.setattr(pir_mod, "_graphql", _gql)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor, live_writes=True)
    return executor, calls


async def _run(executor: CallableToolExecutor, arguments: dict[str, Any]) -> ToolResult:
    return await executor.execute_tool(ToolCall(id="call-1", name=TOOL, arguments=arguments))


@pytest.mark.parametrize(
    ("arguments", "named"),
    [
        ({"action": "update", "pir_id": "p1", "name": "n", "pir_filters": "{}"}, "pir_filters"),
        ({"action": "update", "pir_id": "p1", "pir_type": "THREAT_ORIGIN"}, "pir_type"),
        ({"action": "create", "name": "n", "matching_criteria": "[]"}, "matching_criteria"),
        ({"action": "delete", "pir_id": "p1", "search": "x"}, "search"),
        ({"action": "get", "pir_id": "p1", "limit": 5}, "limit"),
        (
            {
                "action": "unflag",
                "pir_id": "p1",
                "relationship_id": "r",
                "source_id": "s",
                "relationship_author_id": "a",
            },
            "relationship_author_id",
        ),
        ({"action": "list", "public": True}, "public"),
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
        {"action": "list"},
        {"action": "list", "search": "x", "limit": 10},
        {"action": "get", "pir_id": "p1"},
        {"action": "update", "pir_id": "p1", "pir_rescan_days": 7},
        {"action": "delete", "pir_id": "p1"},
        {"action": "unflag", "pir_id": "p1", "relationship_id": "r", "source_id": "s"},
    ],
)
async def test_action_with_its_own_fields_runs(monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any]) -> None:
    executor, calls = _build(monkeypatch)
    result = await _run(executor, arguments)
    assert "does not apply" not in result.content, result.content
    assert calls.count == 1
