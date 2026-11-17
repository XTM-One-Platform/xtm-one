"""Tests for the Sumo Logic integration provider.

Covers tool introspection (including the URL-shaped catalog dummy), the HTTP
Basic auth header + base-URL helpers, the asynchronous Search Job flow
(create -> poll -> messages, and the aggregation records path), Cloud SIEM
insight listing (status filter + envelope parsing), the consolidated
threat-intel indicator tool (action guard + create-requires-source guard), and
the call-time credential guard. Every API call routes through the shared
``safe_request`` helper, which is monkeypatched here so no network is touched.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from app.integrations.providers import sumologic as sl
from app.integrations.providers.sumologic import (
    build_sumologic_headers,
    build_tools,
    sumologic_api_base,
)
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://api.sumologic.com/api||aid-123||akey-456"


class _StubResponse:
    def __init__(self, payload: Any):
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _Recorder:
    """Fake ``safe_request`` that routes by (method, url) and records calls."""

    def __init__(self, router):
        self._router = router
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        return _StubResponse(self._router(method, url)), None


def _build(monkeypatch, router) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(router)
    monkeypatch.setattr(sl, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Introspection + helpers
# ---------------------------------------------------------------------------


def test_build_tools_returns_definitions_with_catalog_dummy():
    # The catalog introspects providers with a 2-part URL dummy; the tools must
    # still build (validation happens at call time, not in build_tools).
    executor = CallableToolExecutor()
    tools = build_tools(token="https://dummy.example.com||__dummy__", executor=executor)
    names = {t.name for t in tools}
    assert "get_sumologic_status" in names
    assert "run_sumologic_search" in names
    assert "list_sumologic_insights" in names
    assert "get_sumologic_insight" in names
    assert "list_sumologic_signals" in names
    assert "list_sumologic_entities" in names
    assert "manage_sumologic_threat_indicators" in names
    assert "list_sumologic_monitors" in names
    assert "list_sumologic_collectors" in names
    assert len(tools) >= 14


def test_tool_priorities_sort_reads_before_writes():
    executor = CallableToolExecutor()
    tools = {t.name: t for t in build_tools(token=_TOKEN, executor=executor)}
    # Reads are priority 0, writes 1, status/context 2.
    assert tools["run_sumologic_search"].priority == 0
    assert tools["update_sumologic_insight_status"].priority == 1
    assert tools["add_sumologic_insight_comment"].priority == 1
    assert tools["get_sumologic_status"].priority == 2


def test_headers_and_base_url_helpers():
    headers = build_sumologic_headers("aid", "akey")
    assert headers["Authorization"].startswith("Basic ")
    decoded = base64.b64decode(headers["Authorization"].split(" ", 1)[1]).decode()
    assert decoded == "aid:akey"
    assert headers["Accept"] == "application/json"
    # default + trailing-slash normalisation + /api append
    assert sumologic_api_base("") == "https://api.sumologic.com/api"
    assert sumologic_api_base("https://api.eu.sumologic.com/api/") == (
        "https://api.eu.sumologic.com/api"
    )
    # a base entered without the trailing /api segment is tolerated
    assert sumologic_api_base("https://api.au.sumologic.com") == (
        "https://api.au.sumologic.com/api"
    )


def test_tools_without_credentials_return_error():
    # base URL only, no access id/key -> tools still build but error at call time.
    executor = CallableToolExecutor()
    build_tools(token="https://api.sumologic.com/api||", executor=executor)

    async def _run() -> str:
        result = await executor.execute_tool(
            ToolCall(id="c0", name="get_sumologic_status", arguments={})
        )
        return result.content

    import asyncio

    content = asyncio.run(_run())
    assert "credentials are not configured" in content.lower()


# ---------------------------------------------------------------------------
# Search Job API (create -> poll -> fetch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_search_full_flow_messages(monkeypatch):
    async def _fast_sleep(_seconds):
        return None

    monkeypatch.setattr(sl.asyncio, "sleep", _fast_sleep)

    def router(method, url):
        if method == "post" and url.endswith("/v1/search/jobs"):
            return {"id": "JOB1"}
        if url.endswith("/messages"):
            return {
                "messages": [
                    {
                        "map": {
                            "_messagetime": "1700000000000",
                            "_sourcecategory": "prod/auth",
                            "_sourcehost": "host1",
                            "_raw": "Failed login for user bob",
                        }
                    }
                ]
            }
        if url.endswith("/v1/search/jobs/JOB1"):
            return {"state": "DONE GATHERING RESULTS", "messageCount": 1, "recordCount": 0}
        return {}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="run_sumologic_search",
            arguments={"query": "_sourceCategory=prod/auth error", "hours": 6},
        )
    )
    assert not result.is_error
    assert "prod/auth" in result.content
    assert "Failed login for user bob" in result.content
    # The create request carries the query + time window + timeZone.
    start = next(c for c in recorder.calls if c["url"].endswith("/v1/search/jobs"))
    assert start["json"]["query"] == "_sourceCategory=prod/auth error"
    assert "from" in start["json"] and "to" in start["json"]
    assert start["json"]["timeZone"] == "UTC"
    # The finished job is cleaned up.
    assert any(c["method"] == "delete" for c in recorder.calls)


@pytest.mark.asyncio
async def test_run_search_aggregation_uses_records(monkeypatch):
    async def _fast_sleep(_seconds):
        return None

    monkeypatch.setattr(sl.asyncio, "sleep", _fast_sleep)

    def router(method, url):
        if method == "post" and url.endswith("/v1/search/jobs"):
            return {"id": "JOB2"}
        if url.endswith("/records"):
            return {"records": [{"map": {"user": "bob", "_count": "42"}}]}
        if url.endswith("/messages"):
            raise AssertionError("aggregation query must fetch records, not messages")
        if url.endswith("/v1/search/jobs/JOB2"):
            return {"state": "DONE GATHERING RESULTS", "recordCount": 1}
        return {}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c2",
            name="run_sumologic_search",
            arguments={"query": "error | count by user"},
        )
    )
    assert not result.is_error
    assert "user=bob" in result.content
    assert "_count=42" in result.content
    assert any(c["url"].endswith("/records") for c in recorder.calls)


@pytest.mark.asyncio
async def test_run_search_requires_query(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="c3", name="run_sumologic_search", arguments={"query": "  "})
    )
    assert "query is required" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_run_search_handles_non_json_poll(monkeypatch):
    # A 2xx response with an undecodable body must not crash the tool; it should
    # return a clear error and still clean up the search job.
    async def _fast_sleep(_seconds):
        return None

    monkeypatch.setattr(sl.asyncio, "sleep", _fast_sleep)

    class _BadJson:
        def json(self) -> Any:
            raise ValueError("not json")

    calls: list[dict[str, Any]] = []

    async def fake_safe_request(_client, method, url, _provider, action, **kwargs):
        calls.append({"method": method, "url": url})
        if method == "post" and url.endswith("/v1/search/jobs"):
            return _StubResponse({"id": "JOBX"}), None
        if url.endswith("/v1/search/jobs/JOBX"):
            return _BadJson(), None
        return _StubResponse({}), None

    monkeypatch.setattr(sl, "safe_request", fake_safe_request)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    result = await executor.execute_tool(
        ToolCall(id="cj", name="run_sumologic_search", arguments={"query": "error"})
    )
    assert "unreadable" in result.content.lower()
    # The job is cleaned up even on the decode-failure path.
    assert any(c["method"] == "delete" for c in calls)


# ---------------------------------------------------------------------------
# Cloud SIEM insights
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_insights_parses_envelope_and_status_filter(monkeypatch):
    def router(method, url):
        return {
            "data": {
                "objects": [
                    {
                        "id": "ins-uuid-1",
                        "readableId": "INSIGHT-220",
                        "name": "Lateral Movement",
                        "severity": "HIGH",
                        "status": {"displayName": "In Progress"},
                        "entity": {"name": "1.2.3.4"},
                    }
                ],
                "hasNextPage": False,
            }
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c4",
            name="list_sumologic_insights",
            arguments={"status": "new,inprogress"},
        )
    )
    assert not result.is_error
    assert "Lateral Movement" in result.content
    assert "INSIGHT-220" in result.content
    # The status filter is folded into the Cloud SIEM query DSL.
    assert 'status:in("new","inprogress")' in recorder.calls[-1]["params"]["q"]


@pytest.mark.asyncio
async def test_list_insights_status_filter_is_case_insensitive(monkeypatch):
    def router(method, url):
        return {"data": {"objects": [], "hasNextPage": False}}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c4b",
            name="list_sumologic_insights",
            arguments={"status": "New, InProgress"},
        )
    )
    assert not result.is_error
    # Mixed-case input is normalised to the lowercase Cloud SIEM enum values.
    assert 'status:in("new","inprogress")' in recorder.calls[-1]["params"]["q"]


@pytest.mark.asyncio
async def test_list_insights_rejects_invalid_status(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c4c",
            name="list_sumologic_insights",
            arguments={"status": "open"},
        )
    )
    assert "invalid insight status filter" in result.content.lower()
    # An unknown status short-circuits before any API call is made.
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_update_insight_status_closed_sends_resolution(monkeypatch):
    def router(method, url):
        return {"data": {"status": {"displayName": "Closed"}, "resolution": "Resolved"}}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c5",
            name="update_sumologic_insight_status",
            arguments={"insight_id": "ins-1", "status": "closed"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "put"
    assert call["url"].endswith("/sec/v1/insights/ins-1/status")
    assert call["json"]["status"] == "closed"
    assert call["json"]["resolution"] == "Resolved"


@pytest.mark.asyncio
async def test_update_insight_status_rejects_invalid_status(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c6",
            name="update_sumologic_insight_status",
            arguments={"insight_id": "ins-1", "status": "bogus"},
        )
    )
    assert "status must be one of" in result.content.lower()
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Threat intelligence (consolidated list/create tool)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_threat_indicators_rejects_invalid_action(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c7",
            name="manage_sumologic_threat_indicators",
            arguments={"action": "nuke"},
        )
    )
    assert "action must be one of" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_threat_indicators_create_requires_source(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c8",
            name="manage_sumologic_threat_indicators",
            arguments={"action": "create", "value": "1.2.3.4"},
        )
    )
    assert "requires source_id" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_threat_indicators_create_posts_indicator(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"data": {}})
    result = await executor.execute_tool(
        ToolCall(
            id="c9",
            name="manage_sumologic_threat_indicators",
            arguments={"action": "create", "value": "1.2.3.4", "source_id": "54"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"].endswith("/sec/v1/threat-intel-sources/54/items")
    indicators = call["json"]["indicators"]
    assert indicators[0]["value"] == "1.2.3.4"
    assert indicators[0]["active"] is True
    assert "expiration" in indicators[0]


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_collectors_parses_envelope(monkeypatch):
    def router(method, url):
        return {
            "collectors": [
                {
                    "id": 100,
                    "name": "prod-collector",
                    "collectorType": "Hosted",
                    "alive": True,
                    "category": "linux/system",
                }
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c10",
            name="list_sumologic_collectors",
            arguments={"collector_filter": "alive"},
        )
    )
    assert not result.is_error
    assert "prod-collector" in result.content
    assert recorder.calls[-1]["params"]["filter"] == "alive"


@pytest.mark.asyncio
async def test_list_collectors_rejects_invalid_filter(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c11",
            name="list_sumologic_collectors",
            arguments={"collector_filter": "bogus"},
        )
    )
    assert "invalid collector_filter" in result.content.lower()
    # An unknown filter short-circuits before any API call is made.
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Monitors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_monitors_status_is_canonicalized(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: [])
    result = await executor.execute_tool(
        ToolCall(
            id="c12",
            name="list_sumologic_monitors",
            arguments={"monitor_status": "critical"},
        )
    )
    assert not result.is_error
    # Lowercase input is normalised to the canonical CamelCase enum value.
    assert "monitorStatus:Critical" in recorder.calls[-1]["params"]["query"]


@pytest.mark.asyncio
async def test_list_monitors_rejects_invalid_status(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: [])
    result = await executor.execute_tool(
        ToolCall(
            id="c13",
            name="list_sumologic_monitors",
            arguments={"monitor_status": "bogus"},
        )
    )
    assert "invalid monitor_status" in result.content.lower()
    # An unknown status short-circuits before any API call is made.
    assert recorder.calls == []
