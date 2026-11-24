"""manage_opencti_role and manage_opencti_profile name every field the action does not apply as ignored; the action runs.

Same contract as the user, group and ingestion tools: a field the chosen action
ignores (or an argument the tool does not declare) is named in the answer and
nothing is sent to OpenCTI.
"""

from typing import Any

import pytest

import app.integrations.providers.opencti.access as access_mod
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
        if "roles(" in query:
            return {"roles": {"edges": [{"node": {"id": "r1", "name": "Analyst"}}]}}, None
        if "me {" in query:
            return {"me": {"id": "m1", "name": "Me", "user_email": "me@x.test", "groups": {"edges": []}}}, None
        if "meEdit" in query:
            return {"meEdit": {"id": "m1", "name": "Me"}}, None
        return {}, None


def _build(monkeypatch: pytest.MonkeyPatch) -> tuple[CallableToolExecutor, _Stub]:
    stub = _Stub()
    monkeypatch.setattr(access_mod, "_graphql", stub)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor, scope_headers={})
    return executor, stub


async def _run(executor: CallableToolExecutor, tool: str, arguments: dict[str, Any]) -> ToolResult:
    return await executor.execute_tool(ToolCall(id="call-1", name=tool, arguments=arguments))


@pytest.mark.parametrize(
    ("tool", "arguments", "named"),
    [
        ("manage_opencti_role", {"action": "list", "role_id": "r1"}, "role_id"),
        ("manage_opencti_role", {"action": "get", "role_id": "r1", "name": "A"}, "name"),
        ("manage_opencti_role", {"action": "create", "name": "A", "capability_id": "c1"}, "capability_id"),
        ("manage_opencti_role", {"action": "delete", "role_id": "r1", "search": "x"}, "search"),
        (
            "manage_opencti_role",
            {"action": "add_capability", "role_id": "r1", "capability_id": "c1", "description": "d"},
            "description",
        ),
        ("manage_opencti_role", {"action": "update", "role_id": "r1", "name": "B", "limit": 10}, "limit"),
        ("manage_opencti_role", {"action": "list", "capabilities": ["c1"]}, "capabilities"),
        ("manage_opencti_profile", {"action": "get", "theme": "dark"}, "theme"),
        ("manage_opencti_profile", {"action": "update", "theme": "dark", "account_status": "Active"}, "account_status"),
    ],
)
async def test_unapplied_field_is_named_as_ignored_and_the_action_runs(
    monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict[str, Any], named: str
) -> None:
    executor, _stub = _build(monkeypatch)
    result = await _run(executor, tool, arguments)
    assert f"action '{arguments['action']}' does not apply" in result.content, result.content
    assert named in result.content
    assert "ignored and the rest ran" in result.content


async def test_role_list_with_its_own_fields_and_default_limit_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, stub = _build(monkeypatch)
    result = await _run(executor, "manage_opencti_role", {"action": "list", "search": "A", "limit": 50})
    assert "does not apply" not in result.content, result.content
    assert "Roles (1)" in result.content
    assert stub.calls


async def test_profile_get_and_update_with_their_fields_run(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, stub = _build(monkeypatch)
    got = await _run(executor, "manage_opencti_profile", {"action": "get"})
    assert "does not apply" not in got.content, got.content
    updated = await _run(executor, "manage_opencti_profile", {"action": "update", "theme": "dark", "language": "fr-fr"})
    assert "does not apply" not in updated.content, updated.content
    assert "Updated your profile" in updated.content
    assert len(stub.calls) == 2
