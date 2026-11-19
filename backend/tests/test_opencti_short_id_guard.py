"""The OpenCTI tools refuse a short id prefix before any request (#278)."""

from __future__ import annotations

import asyncio
from typing import Any

from app.integrations.providers.opencti import build_tools
from app.integrations.providers.opencti.ids import install_short_id_guard, short_id_refusal
from app.tools.base import CallableToolExecutor, ToolCall


def test_a_short_hex_prefix_in_an_id_argument_is_refused_by_name() -> None:
    refusal = short_id_refusal({"feed_id": "12aa2870", "action": "toggle"})
    assert refusal is not None
    assert refusal.startswith("Error:")
    assert "feed_id='12aa2870'" in refusal
    assert "Nothing was sent" in refusal


def test_every_id_argument_form_is_checked() -> None:
    refusal = short_id_refusal({"id": "db5c5075", "entity_ids": ["c838831c", "x"], "ids": []})
    assert refusal is not None
    assert "id='db5c5075'" in refusal
    assert "entity_ids='c838831c'" in refusal


def test_full_ids_and_other_arguments_pass() -> None:
    assert (
        short_id_refusal(
            {
                "id": "12aa2870-5f3b-4c1e-9d2a-0b6f1e2a3c4d",
                "entity_id": "indicator--12aa2870-5f3b-4c1e-9d2a-0b6f1e2a3c4d",
                "playbook_id": "12aa28705f3b4c1e9d2a0b6f1e2a3c4d",
                "component_id": "PLAYBOOK_INTERNAL_DATA_STREAM",
                "name": "deadbeef",
                "value": "12aa2870",
                "file_id": "import/global/report.pdf",
            }
        )
        is None
    )


def test_a_short_numeric_or_too_short_value_is_not_a_prefix() -> None:
    assert short_id_refusal({"id": "1234567"}) is None
    assert short_id_refusal({"user_id": None, "group_ids": None}) is None


def _run(executor: CallableToolExecutor, name: str, arguments: dict[str, Any]) -> Any:
    return asyncio.run(executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments)))


def test_the_guard_answers_before_the_handler_runs() -> None:
    calls: list[dict[str, Any]] = []

    async def handler(action: str, feed_id: str = "") -> str:
        calls.append({"action": action, "feed_id": feed_id})
        return "Toggled"

    executor = CallableToolExecutor()
    executor.register("manage_opencti_ingestion", handler)
    install_short_id_guard(executor, ["manage_opencti_ingestion"])

    refused = _run(executor, "manage_opencti_ingestion", {"action": "toggle", "feed_id": "12aa2870"})
    assert refused.is_error
    assert "short id prefix" in refused.content
    assert calls == []

    full = "12aa2870-5f3b-4c1e-9d2a-0b6f1e2a3c4d"
    passed = _run(executor, "manage_opencti_ingestion", {"action": "toggle", "feed_id": full})
    assert not passed.is_error
    assert passed.content == "Toggled"
    assert calls == [{"action": "toggle", "feed_id": full}]


def test_build_tools_guards_every_opencti_tool() -> None:
    executor = CallableToolExecutor()
    tools = build_tools("https://opencti.example||token", executor)
    assert tools
    for tool in tools:
        handler = executor.handler(tool.name)
        assert handler is not None
        assert getattr(handler, "__wrapped__", None) is not None, tool.name
    refused = _run(executor, "manage_opencti_playbook", {"action": "execute", "playbook_id": "58876732"})
    assert refused.is_error
    assert "playbook_id='58876732'" in refused.content
