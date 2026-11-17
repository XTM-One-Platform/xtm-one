"""The hourly indicator decay re-check skips an empty decay window at zero tokens.

Its seeded ``activation_check`` reads the window with ``list_opencti_entities``
(limit 1), which gives its listing structured for the gate: an empty window
fails ``non_empty`` and the run is skipped with no model call.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from app.agents.builtin_agents import BUILTIN_AGENTS, BUILTIN_ASSIGNMENTS
from app.integrations.providers.opencti import build_tools
from app.services.assignment_activation_check import condition_met, parse_activation_check, structured_result
from app.tools.base import CallableToolExecutor, ToolCall

_NAME = "Hourly indicator decay re-check"


def _seeded() -> dict:
    return next(a for a in BUILTIN_ASSIGNMENTS if a["name"] == _NAME)


def _listing(*nodes: dict) -> dict:
    return {"data": {"stixDomainObjects": {"edges": [{"node": n} for n in nodes], "pageInfo": {"globalCount": len(nodes)}}}}


async def _run_check_tool(answer: dict):
    check, error, _ = parse_activation_check(_seeded()["activation_check"])
    assert error is None
    executor = CallableToolExecutor()
    build_tools("https://octi.test||static-token", executor)
    sent: list[dict] = []

    async def _fake(self, method, url, **kwargs):
        sent.append(kwargs.get("json") or {})
        return httpx.Response(200, json=answer, request=httpx.Request("POST", url))

    with patch.object(httpx.AsyncClient, "request", _fake):
        result = await executor.execute_tool(ToolCall(id="gate", name=check["tool"], arguments=check["arguments"]))
    return check, result, sent


def test_the_hourly_re_check_declares_a_read_only_gate_and_keeps_its_cadence():
    seeded = _seeded()
    check, error, _ = parse_activation_check(seeded["activation_check"])
    assert error is None
    assert check["tool"] == "list_opencti_entities"
    assert check["arguments"]["limit"] == 1
    assert seeded["trigger_config"] == {"cron": "17 * * * *"}
    assert seeded["enabled"] is False and seeded.get("admin_opt_in") is True


def test_the_gate_reads_the_window_the_prompt_reads():
    seeded = _seeded()
    filters = json.loads(seeded["activation_check"]["arguments"]["filters"])
    assert {"key": ["revoked"], "values": ["false"]} in filters["filters"]
    window = {"key": ["valid_until"], "values": ["now", "now+48h"], "operator": "within"}
    assert window in filters["filters"]
    assert '"now+48h"' in seeded["prompt"] and '"within"' in seeded["prompt"]


def test_the_guardian_holds_the_gate_tool_through_its_opencti_binding():
    guardian = next(a for a in BUILTIN_AGENTS if a["slug"] == _seeded()["agent_slug"])
    assert "opencti" in guardian["integration_template_bindings"]


@pytest.mark.asyncio
async def test_an_empty_window_fails_the_gate():
    check, result, sent = await _run_check_tool(_listing())
    data, readable = structured_result(result)
    assert readable and data == []
    assert not condition_met(data, check["condition"])
    assert sent[0]["variables"]["first"] == 1
    assert result.content == "No domain objects found."


@pytest.mark.asyncio
async def test_an_indicator_in_the_window_passes_the_gate_and_the_text_is_unchanged():
    node = {"id": "ind-1", "entity_type": "Indicator", "name": "bad.example"}
    check, result, _ = await _run_check_tool(_listing(node))
    data, readable = structured_result(result)
    assert readable and data == [node]
    assert condition_met(data, check["condition"])
    assert result.content.startswith("Found 1 domain object:")
    assert not result.is_error
