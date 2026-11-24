"""manage_opencti_form names every field the action does not apply as ignored; the action runs.

Same contract as the user, group, role, profile and ingestion tools: a field the
chosen action ignores (or an argument the tool does not declare) is named in the
answer as ignored and the action runs with the rest.
"""

from typing import Any

import pytest

import app.integrations.providers.opencti.ingestion as ingestion_mod
from app.integrations.providers.opencti import build_tools
from app.tools.base import CallableToolExecutor, ToolCall, ToolResult

TOKEN = "https://demo.opencti.io||test-token"


class _Stub:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(
        self, client: Any, base_url: str, headers: dict[str, str], query: str, variables: Any = None
    ) -> tuple[dict[str, Any], str | None]:
        self.calls.append(query)
        if "forms(" in query:
            return {"forms": {"edges": [{"node": {"id": "f1", "name": "Intake", "active": True}}]}}, None
        if "formDelete" in query:
            return {"formDelete": "f1"}, None
        return {}, None


def _build(monkeypatch: pytest.MonkeyPatch) -> tuple[CallableToolExecutor, _Stub]:
    stub = _Stub()
    monkeypatch.setattr(ingestion_mod, "_graphql", stub)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor, scope_headers={})
    return executor, stub


async def _run(executor: CallableToolExecutor, arguments: dict[str, Any]) -> ToolResult:
    return await executor.execute_tool(ToolCall(id="call-1", name="manage_opencti_form", arguments=arguments))


@pytest.mark.parametrize(
    ("arguments", "named"),
    [
        ({"action": "list", "form_id": "f1"}, "form_id"),
        ({"action": "get", "form_id": "f1", "name": "A"}, "name"),
        ({"action": "delete", "form_id": "f1", "search": "x"}, "search"),
        ({"action": "submit", "form_id": "f1", "values": "{}", "active": True}, "active"),
        ({"action": "submit", "form_id": "f1", "values": "{}", "active": False}, "active"),
        ({"action": "list", "active": False}, "active"),
        ({"action": "import", "content": "{}", "form_schema": "{}"}, "form_schema"),
        ({"action": "update", "form_id": "f1", "name": "B", "limit": 10}, "limit"),
        ({"action": "list", "status": "active"}, "status"),
    ],
)
async def test_unapplied_field_is_named_as_ignored_and_the_action_runs(
    monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any], named: str
) -> None:
    executor, _stub = _build(monkeypatch)
    result = await _run(executor, arguments)
    assert f"action '{arguments['action']}' does not apply" in result.content, result.content
    assert named in result.content
    assert "ignored and the rest ran" in result.content


async def test_list_and_delete_with_their_own_fields_run(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, stub = _build(monkeypatch)
    listed = await _run(executor, {"action": "list", "search": "Int", "limit": 50})
    assert "does not apply" not in listed.content, listed.content
    assert "Intake forms (1)" in listed.content
    deleted = await _run(executor, {"action": "delete", "form_id": "f1"})
    assert "Deleted intake form f1" in deleted.content, deleted.content
    assert len(stub.calls) == 2
