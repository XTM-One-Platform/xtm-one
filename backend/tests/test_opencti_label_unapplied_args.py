"""manage_opencti_label refuses every field the action does not apply.

Same contract as the user, group, role, profile, ingestion and form tools: a field
the chosen action ignores (value or color on delete, or an argument the tool does
not declare) is named in the answer and nothing is sent to OpenCTI.
"""

from typing import Any

import pytest

import app.integrations.providers.opencti.knowledge as knowledge_mod
from app.integrations.providers.opencti import build_tools
from app.tools.base import CallableToolExecutor, ToolCall, ToolResult

TOKEN = "https://demo.opencti.io||test-token"


class _Calls:
    def __init__(self) -> None:
        self.count = 0


def _build(monkeypatch: pytest.MonkeyPatch) -> tuple[CallableToolExecutor, _Calls]:
    calls = _Calls()

    async def _resolve(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], str | None]:
        calls.count += 1
        return {"id": "l1", "value": "apt", "color": "#ff8800"}, None

    async def _gql(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], str | None]:
        calls.count += 1
        return {"labelDelete": "l1"}, None

    monkeypatch.setattr(knowledge_mod, "_resolve_label", _resolve)
    monkeypatch.setattr(knowledge_mod, "_graphql", _gql)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor, live_writes=True)
    return executor, calls


async def _run(executor: CallableToolExecutor, arguments: dict[str, Any]) -> ToolResult:
    return await executor.execute_tool(ToolCall(id="call-1", name="manage_opencti_label", arguments=arguments))


@pytest.mark.parametrize(
    ("arguments", "named"),
    [
        ({"action": "delete", "label": "apt", "value": "apt2"}, "value"),
        ({"action": "delete", "label": "apt", "color": "#000000"}, "color"),
        ({"action": "update", "label": "apt", "value": "x", "status": "on"}, "status"),
    ],
)
async def test_unapplied_field_is_refused_and_nothing_is_sent(
    monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any], named: str
) -> None:
    executor, calls = _build(monkeypatch)
    result = await _run(executor, arguments)
    assert f"action '{arguments['action']}' does not apply" in result.content, result.content
    assert named in result.content
    assert "nothing was written" in result.content
    assert calls.count == 0


async def test_delete_with_its_own_field_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, calls = _build(monkeypatch)
    result = await _run(executor, {"action": "delete", "label": "apt"})
    assert "does not apply" not in result.content, result.content
    assert "deleted" in result.content
    assert calls.count == 2
