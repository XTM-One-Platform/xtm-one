"""The OpenCTI customization tools name fields the action does not apply as ignored; the action runs.

Same contract as the user, group, role, profile, ingestion, form, label, workspace,
notifier, trigger, notification and connector tools: a field the chosen action ignores
(scope on a retention rule update, enabled on an exclusion list create, color on a
status template delete, an argument the tool does not declare) is named in the answer
as ignored and the action runs with the rest.
"""

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

import app.integrations.providers.opencti.customization as customization_mod
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
        return {}, None

    monkeypatch.setattr(customization_mod, "_client", _client)
    monkeypatch.setattr(customization_mod, "_graphql", _gql)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor, live_writes=True)
    return executor, calls


async def _run(executor: CallableToolExecutor, tool: str, arguments: dict[str, Any]) -> ToolResult:
    return await executor.execute_tool(ToolCall(id="call-1", name=tool, arguments=arguments))


@pytest.mark.parametrize(
    ("tool", "arguments", "named"),
    [
        ("manage_opencti_entity_setting", {"action": "patch", "target_type": "Report", "order": 2}, "order"),
        ("manage_opencti_entity_setting", {"action": "list", "target_type": "Report"}, "target_type"),
        (
            "manage_opencti_entity_setting",
            {"action": "delete_status", "target_type": "Report", "status_id": "s1", "scope": "request_access"},
            "scope",
        ),
        ("manage_opencti_vocabulary", {"action": "get", "vocabulary_id": "v1", "search": "x"}, "search"),
        ("manage_opencti_vocabulary", {"action": "update", "vocabulary_id": "v1", "category": "c"}, "category"),
        ("manage_opencti_status_template", {"action": "delete", "template_id": "t1", "color": "#fff"}, "color"),
        (
            "manage_opencti_decay_rule",
            {"action": "delete", "decay_rule_id": "d1", "decay_lifetime": 10},
            "decay_lifetime",
        ),
        ("manage_opencti_retention_rule", {"action": "update", "retention_rule_id": "r1", "scope": "file"}, "scope"),
        (
            "manage_opencti_exclusion_list",
            {"action": "get", "exclusion_list_id": "x1", "enabled": True},
            "enabled",
        ),
        (
            "manage_opencti_exclusion_list",
            {"action": "get", "exclusion_list_id": "x1", "enabled": False},
            "enabled",
        ),
        ("manage_opencti_decay_rule", {"action": "delete", "decay_rule_id": "d1", "active": False}, "active"),
        ("manage_opencti_entity_setting", {"action": "list", "enforce_reference": False}, "enforce_reference"),
        ("manage_opencti_exclusion_list", {"action": "list", "force": True}, "force"),
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


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("manage_opencti_entity_setting", {"action": "list"}),
        ("manage_opencti_vocabulary", {"action": "list", "category": "c", "search": "x", "limit": 10}),
        ("manage_opencti_status_template", {"action": "delete", "template_id": "t1"}),
        ("manage_opencti_decay_rule", {"action": "list"}),
        ("manage_opencti_retention_rule", {"action": "delete", "retention_rule_id": "r1"}),
        (
            "manage_opencti_exclusion_list",
            {"action": "update", "exclusion_list_id": "e1", "enabled": False, "name": "n"},
        ),
    ],
)
async def test_action_with_its_own_fields_runs(
    monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict[str, Any]
) -> None:
    executor, calls = _build(monkeypatch)
    result = await _run(executor, tool, arguments)
    assert "does not apply" not in result.content, result.content
    assert calls.count == 1
