"""manage_opencti_notifier, _trigger and _notification name fields the action does not apply as ignored; the action runs.

Same contract as the user, group, role, profile, ingestion, form, label and workspace
tools: a field the chosen action ignores (name on delete, recipients on update,
event_types on an activity live trigger, an argument the tool does not declare) is
named in the answer as ignored and the action runs with the rest.
"""

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

import app.integrations.providers.opencti.notifications as notifications_mod
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
        return {"notifiers": {"edges": []}, "triggersKnowledge": {"edges": []}}, None

    monkeypatch.setattr(notifications_mod, "_client", _client)
    monkeypatch.setattr(notifications_mod, "_graphql", _gql)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor, live_writes=True)
    return executor, calls


async def _run(executor: CallableToolExecutor, tool: str, arguments: dict[str, Any]) -> ToolResult:
    return await executor.execute_tool(ToolCall(id="call-1", name=tool, arguments=arguments))


@pytest.mark.parametrize(
    ("tool", "arguments", "named"),
    [
        ("manage_opencti_notifier", {"action": "delete", "notifier_id": "n1", "name": "x"}, "name"),
        ("manage_opencti_notifier", {"action": "get", "notifier_id": "n1", "limit": 5}, "limit"),
        (
            "manage_opencti_notifier",
            {"action": "update", "notifier_id": "n1", "notifier_test_id": "default_digest"},
            "notifier_test_id",
        ),
        ("manage_opencti_trigger", {"action": "update", "trigger_id": "t1", "recipients": ["u1"]}, "recipients"),
        ("manage_opencti_trigger", {"action": "delete", "trigger_id": "t1", "period": "week"}, "period"),
        (
            "manage_opencti_trigger",
            {"action": "create_live", "kind": "activity", "name": "x", "recipients": ["u1"], "event_types": ["create"]},
            "event_types",
        ),
        ("manage_opencti_trigger", {"action": "list", "status": "on"}, "status"),
        ("manage_opencti_notification", {"action": "mark_read", "notification_id": "x1", "search": "a"}, "search"),
        ("manage_opencti_notification", {"action": "delete", "notification_id": "x1", "read": True}, "read"),
    ],
)
async def test_unapplied_field_is_named_as_ignored_and_the_action_runs(
    monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict[str, Any], named: str
) -> None:
    executor, _calls = _build(monkeypatch)
    result = await _run(executor, tool, arguments)
    assert f"action '{arguments['action']}' does not apply" in result.content, result.content
    assert named in result.content
    assert "ignored and the rest ran" in result.content


async def test_notifier_delete_with_its_own_field_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, calls = _build(monkeypatch)
    result = await _run(executor, "manage_opencti_notifier", {"action": "delete", "notifier_id": "n1"})
    assert "does not apply" not in result.content, result.content
    assert "Deleted notifier n1" in result.content
    assert calls.count == 1


async def test_notifier_update_sends_the_stored_connector_and_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """#2632: OpenCTI notifierEdit validates both keys read from the edit input on every patch."""
    sent: list[dict[str, Any]] = []

    @contextlib.asynccontextmanager
    async def _client(*_args: Any, **_kwargs: Any) -> AsyncIterator[None]:
        yield None

    async def _gql(
        _c: Any, _b: Any, _h: Any, query: str, variables: dict[str, Any] | None = None, **_kw: Any
    ) -> tuple[dict[str, Any], str | None]:
        if "NotifierEditBase" in query:
            return {"notifier": {"notifier_connector": {"id": "conn-1"}, "notifier_configuration": "{}"}}, None
        sent.extend((variables or {}).get("input") or [])
        return {"notifierFieldPatch": {"id": "n1"}}, None

    monkeypatch.setattr(notifications_mod, "_client", _client)
    monkeypatch.setattr(notifications_mod, "_graphql", _gql)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor, live_writes=True)
    result = await _run(
        executor, "manage_opencti_notifier", {"action": "update", "notifier_id": "n1", "description": "d"}
    )
    assert "Updated notifier n1" in result.content, result.content
    assert {e["key"]: e["value"] for e in sent} == {
        "description": ["d"],
        "notifier_configuration": ["{}"],
        "notifier_connector_id": ["conn-1"],
    }


async def test_trigger_list_with_defaults_is_not_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, calls = _build(monkeypatch)
    result = await _run(executor, "manage_opencti_trigger", {"action": "list", "limit": 50, "period": "day"})
    assert "does not apply" not in result.content, result.content
    assert calls.count == 1


async def test_knowledge_live_trigger_keeps_event_types(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, calls = _build(monkeypatch)
    result = await _run(
        executor, "manage_opencti_trigger", {"action": "create_live", "name": "x", "event_types": ["create"]}
    )
    assert "does not apply" not in result.content, result.content
    assert calls.count == 1
