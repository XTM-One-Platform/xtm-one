"""search_openaev_assets counts endpoints by model keys (#2406).

``asset_status`` is a query-time formula of the OpenAEV Asset model: the
analytics engine does not index it, so the CTEM scoping stage can only count
covered and unprotected endpoints through ``POST /api/endpoints/search`` with a
FilterGroup. These tests pin:

* ``filter_group`` is sent as the request's ``filterGroup`` and the answer
  carries ``totalElements`` (a count with ``limit=1``);
* malformed JSON, an unknown operator and a filter without a key are refused
  before any request;
* ``search`` and ``filter_group`` combine (the text search goes in
  ``textSearch``), and a bare ``search`` keeps the name / hostname / IP filter;
* the seeded Scoping prompt and ``filter_keys`` name the tool.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.agents.builtin_agents import _STAGE_DEFS
from app.integrations.providers.openaev._common import _headers
from app.integrations.providers.openaev.core import register_core
from app.tools.base import CallableToolExecutor, ToolCall

BASE = "https://demo.openaev.io"

ACTIVE = {"mode": "and", "filters": [{"key": "asset_status", "operator": "eq", "values": ["ACTIVE"]}]}


def _executor() -> CallableToolExecutor:
    executor = CallableToolExecutor()
    register_core(BASE, _headers("token"), executor)
    return executor


def _server(sent: list[dict[str, Any]], total: int = 42):
    async def _fake_request(self: httpx.AsyncClient, method: str, url: Any, **kwargs: Any) -> httpx.Response:
        request = httpx.Request(method, str(url))
        path = httpx.URL(str(url)).path
        sent.append({"path": path, "json": kwargs.get("json")})
        body = {
            "content": [{"asset_id": "a1", "asset_name": "host-1", "asset_type": "Endpoint"}],
            "totalElements": total,
        }
        return httpx.Response(200, request=request, json=body)

    return _fake_request


async def _call(arguments: dict[str, Any], sent: list[dict[str, Any]]) -> str:
    executor = _executor()
    with patch.object(httpx.AsyncClient, "request", _server(sent)):
        result = await executor.execute_tool(ToolCall(id="1", name="search_openaev_assets", arguments=arguments))
    return result.content


@pytest.mark.asyncio
async def test_filter_group_is_sent_and_the_total_is_reported() -> None:
    sent: list[dict[str, Any]] = []
    content = await _call({"filter_group": json.dumps(ACTIVE), "limit": 1}, sent)

    assert [s["path"] for s in sent] == ["/api/endpoints/search"]
    body = sent[0]["json"]
    assert body["filterGroup"] == ACTIVE
    assert body["size"] == 1
    assert "textSearch" not in body
    assert "Found 42 assets, showing 1" in content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filter_group", "expected"),
    [
        ("{not json", "filter_group must be valid JSON"),
        (
            json.dumps(
                {"mode": "and", "filters": [{"key": "asset_status", "operator": "equals", "values": ["ACTIVE"]}]}
            ),
            "Unknown filter operator",
        ),
        (json.dumps({"mode": "and", "filters": [{"operator": "eq", "values": ["ACTIVE"]}]}), "missing 'key'"),
        (json.dumps({"mode": "xor", "filters": []}), "Unknown filter mode"),
    ],
)
async def test_an_invalid_filter_group_is_refused_before_any_call(filter_group: str, expected: str) -> None:
    sent: list[dict[str, Any]] = []
    content = await _call({"filter_group": filter_group, "limit": 1}, sent)

    assert sent == []
    assert expected in content


@pytest.mark.asyncio
async def test_search_and_filter_group_combine() -> None:
    sent: list[dict[str, Any]] = []
    await _call({"search": "web", "filter_group": json.dumps(ACTIVE)}, sent)

    body = sent[0]["json"]
    assert body["filterGroup"] == ACTIVE
    assert body["textSearch"] == "web"


@pytest.mark.asyncio
async def test_a_bare_search_keeps_the_name_hostname_ip_filter() -> None:
    sent: list[dict[str, Any]] = []
    content = await _call({"search": "web"}, sent)

    body = sent[0]["json"]
    assert {f["key"] for f in body["filterGroup"]["filters"]} == {"asset_name", "asset_hostname", "asset_ips"}
    assert body["size"] == 20
    assert "Found 42 assets" in content


def test_the_tool_schema_takes_filter_group_and_requires_nothing() -> None:
    executor = CallableToolExecutor()
    defs = register_core(BASE, _headers("token"), executor)
    tool = next(d for d in defs if d.name == "search_openaev_assets")
    assert "filter_group" in tool.input_schema["properties"]
    assert tool.input_schema["required"] == []


def test_the_scoping_prompt_counts_endpoints_with_the_tool() -> None:
    scoping = next(d for d in _STAGE_DEFS if d["key"] == "scoping")["cyber"]
    assert "search_openaev_assets filter_group" in scoping
    assert "asset_status eq ACTIVE" in scoping
    assert "INACTIVE, AGENTLESS, STALE" in scoping
