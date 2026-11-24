"""The OpenAEV tools refuse a short id prefix before any request (#1028)."""

from __future__ import annotations

import asyncio
from typing import Any

from app.integrations.providers.openaev import build_tools
from app.integrations.short_id import install_short_id_guard, short_id_refusal
from app.tools.base import CallableToolExecutor, ToolCall


def _run(executor: CallableToolExecutor, name: str, arguments: dict[str, Any]) -> Any:
    return asyncio.run(executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments)))


def test_the_refusal_names_the_platform_and_the_argument() -> None:
    refusal = short_id_refusal({"inject_id": "58876732", "action": "get"}, platform="OpenAEV")
    assert refusal is not None
    assert refusal.startswith("Error: inject_id='58876732' looks like a short id prefix; OpenAEV")
    assert "a full id (its UUID)" in refusal
    assert "Nothing was sent" in refusal


def test_full_uuids_and_other_arguments_pass() -> None:
    assert (
        short_id_refusal(
            {
                "inject_id": "58876732-5f3b-4c1e-9d2a-0b6f1e2a3c4d",
                "asset_ids": ["58876732-5f3b-4c1e-9d2a-0b6f1e2a3c4d"],
                "name": "deadbeef",
                "action": "get",
            },
            platform="OpenAEV",
        )
        is None
    )


def test_the_guard_answers_before_the_handler_runs() -> None:
    calls: list[str] = []

    async def handler(action: str, inject_id: str = "") -> str:
        calls.append(inject_id)
        return "Inject not found"

    executor = CallableToolExecutor()
    executor.register("manage_openaev_inject", handler)
    install_short_id_guard(executor, ["manage_openaev_inject"], platform="OpenAEV")

    refused = _run(executor, "manage_openaev_inject", {"action": "get", "inject_id": "58876732"})
    assert refused.is_error
    assert "short id prefix" in refused.content
    assert calls == []


def test_build_tools_guards_every_openaev_tool() -> None:
    executor = CallableToolExecutor()
    tools = build_tools("https://openaev.example||token", executor)
    assert tools
    for tool in tools:
        handler = executor.handler(tool.name)
        assert handler is not None
        assert getattr(handler, "__wrapped__", None) is not None, tool.name
    refused = _run(executor, "manage_openaev_inject", {"action": "get", "inject_id": "58876732"})
    assert refused.is_error
    assert "inject_id='58876732'" in refused.content
    assert "OpenAEV" in refused.content
