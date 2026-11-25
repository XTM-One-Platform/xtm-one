"""manage_opencti_playbook names fields the action does not apply as ignored; the action runs.

Same contract as the other manage_opencti_* tools: a field the chosen action ignores
(running on an execute, entity_id on a toggle, name on a get, component_id on a list,
an argument the tool does not declare) is named in the answer as ignored and the action
runs with the rest.
"""

from typing import Any

import pytest

import app.integrations.providers.opencti.playbooks as playbooks_mod
from app.integrations.providers.opencti.access import install_ignored_arguments_note
from app.tools.base import CallableToolExecutor, ToolCall

TOOL = "manage_opencti_playbook"


class _Calls:
    def __init__(self) -> None:
        self.count = 0

    async def __call__(self, *_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], str | None]:
        self.count += 1
        return {}, None


async def _run(monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any]) -> tuple[str, _Calls]:
    calls = _Calls()
    monkeypatch.setattr(playbooks_mod, "_graphql", calls)
    executor = CallableToolExecutor()
    playbooks_mod.register("https://demo.opencti.io", {}, executor)
    install_ignored_arguments_note(executor, [TOOL])
    result = await executor.execute_tool(ToolCall(id="call-1", name=TOOL, arguments=arguments))
    return result.content, calls


@pytest.mark.parametrize(
    ("arguments", "named"),
    [
        ({"action": "execute", "playbook_id": "pb", "entity_id": "e", "running": True}, "running"),
        ({"action": "toggle", "playbook_id": "pb", "running": True, "entity_id": "e"}, "entity_id"),
        ({"action": "get", "playbook_id": "pb", "name": "n"}, "name"),
        ({"action": "list", "component_id": "PLAYBOOK_LABELING_COMPONENT"}, "component_id"),
        ({"action": "create", "name": "n", "playbook_id": "pb"}, "playbook_id"),
        ({"action": "delete", "playbook_id": "pb", "limit": 5}, "limit"),
        ({"action": "duplicate", "playbook_id": "pb", "search": "x"}, "search"),
        ({"action": "list", "public": True}, "public"),
    ],
)
async def test_unapplied_field_is_named_as_ignored_and_the_action_runs(
    monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any], named: str
) -> None:
    content, _calls = await _run(monkeypatch, arguments)
    assert f"action '{arguments['action']}' does not apply" in content, content
    assert named in content
    assert "ignored and the rest ran" in content


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "list"},
        {"action": "list", "search": "x", "limit": 10},
        {"action": "get", "playbook_id": "pb"},
        {"action": "create", "name": "n", "description": "d"},
        {"action": "toggle", "playbook_id": "pb", "running": True},
        {"action": "execute", "playbook_id": "pb", "entity_id": "e"},
        {"action": "delete", "playbook_id": "pb"},
        {"action": "duplicate", "playbook_id": "pb"},
        {"action": "components"},
    ],
)
async def test_action_with_its_own_fields_runs(monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any]) -> None:
    content, calls = await _run(monkeypatch, arguments)
    assert "does not apply" not in content, content
    assert calls.count >= 1
