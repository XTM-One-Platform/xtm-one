"""Tests for the Elastic Security integration provider.

Covers the alert search + triage flow (status/tag), nested alert _source field
extraction, rule enable/disable, case create + status-update (version fetch),
value-list IOC enforcement, endpoint isolation, ES|QL hunting formatting, the
Elasticsearch-URL-required guard, space-aware Kibana paths, and introspection.
Every API call routes through the shared ``safe_request`` helper, which is
monkeypatched here so no network is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import elastic_security as es
from app.integrations.providers.elastic_security import _field, build_tools
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://kibana.example||https://es.example||api-key-abc||default"
_TOKEN_NO_ES = "https://kibana.example||||api-key-abc||default"
_TOKEN_SPACE = "https://kibana.example||https://es.example||api-key-abc||soc"


class _StubResponse:
    def __init__(self, payload: Any):
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _Recorder:
    def __init__(self, router):
        self._router = router
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        return _StubResponse(self._router(method, url)), None


def _build(monkeypatch, router, token=_TOKEN) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(router)
    monkeypatch.setattr(es, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=token, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Pure helpers + introspection
# ---------------------------------------------------------------------------


def test_field_reads_nested_and_flat():
    nested = {"kibana": {"alert": {"severity": "high"}}}
    assert _field(nested, "kibana.alert.severity") == "high"
    flat = {"kibana.alert.severity": "low"}
    assert _field(flat, "kibana.alert.severity") == "low"
    assert _field({}, "a.b.c") is None


def test_build_tools_returns_definitions_with_dummy_token():
    executor = CallableToolExecutor()
    tools = build_tools(token="https://dummy.example.com||__dummy__", executor=executor)
    names = {t.name for t in tools}
    assert "list_elastic_alerts" in names
    assert "isolate_elastic_endpoint" in names
    assert "run_elastic_esql_query" in names
    assert "create_elastic_case" in names
    assert len(tools) >= 20


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_alerts_builds_query_and_formats(monkeypatch):
    def router(method, url):
        return {
            "hits": {
                "total": {"value": 1},
                "hits": [
                    {
                        "_id": "a1",
                        "_source": {
                            "@timestamp": "2024-05-01T10:00:00Z",
                            "kibana": {
                                "alert": {
                                    "uuid": "a1",
                                    "rule": {"name": "Suspicious PowerShell"},
                                    "severity": "high",
                                    "workflow_status": "open",
                                    "risk_score": 73,
                                }
                            },
                            "host": {"name": "WIN-01"},
                        },
                    }
                ],
            }
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="list_elastic_alerts",
            arguments={"status": "open", "severity": "high"},
        )
    )
    assert not result.is_error
    assert "Suspicious PowerShell" in result.content
    assert "WIN-01" in result.content
    body = recorder.calls[0]["json"]
    musts = body["query"]["bool"]["must"]
    assert {"term": {"kibana.alert.workflow_status": "open"}} in musts
    assert {"term": {"kibana.alert.severity": "high"}} in musts
    assert any("range" in m for m in musts)


@pytest.mark.asyncio
async def test_list_alerts_rejects_invalid_status(monkeypatch):
    """A typo'd status must fail fast with a clear error and make no request,
    rather than silently forwarding the bad value and returning empty."""
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c1b",
            name="list_elastic_alerts",
            arguments={"status": "opened"},
        )
    )
    assert "invalid status" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_list_alerts_rejects_invalid_severity(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c1c",
            name="list_elastic_alerts",
            arguments={"severity": "sev1"},
        )
    )
    assert "invalid severity" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_set_alert_status_valid(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"success": True})
    result = await executor.execute_tool(
        ToolCall(
            id="c2",
            name="set_elastic_alert_status",
            arguments={"alert_id": "a1", "status": "closed", "reason": "false_positive"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert "signals/status" in call["url"]
    assert call["json"] == {
        "status": "closed",
        "signal_ids": ["a1"],
        "reason": "false_positive",
    }


@pytest.mark.asyncio
async def test_set_alert_status_invalid(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c3",
            name="set_elastic_alert_status",
            arguments={"alert_id": "a1", "status": "bogus"},
        )
    )
    assert "invalid status" in result.content.lower()
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_toggle_rule_bulk_action_body(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"success": True})
    result = await executor.execute_tool(
        ToolCall(
            id="c4",
            name="toggle_elastic_rule",
            arguments={"rule_id": "r1", "enable": False},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert "rules/_bulk_action" in call["url"]
    assert call["json"] == {"action": "disable", "ids": ["r1"]}


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_case_body(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"id": "case-1"})
    result = await executor.execute_tool(
        ToolCall(
            id="c5",
            name="create_elastic_case",
            arguments={
                "title": "Investigate WIN-01",
                "description": "Suspicious activity",
                "severity": "high",
                "tags": "edr,triage",
            },
        )
    )
    assert not result.is_error
    body = recorder.calls[-1]["json"]
    assert body["title"] == "Investigate WIN-01"
    assert body["owner"] == "securitySolution"
    assert body["severity"] == "high"
    assert body["tags"] == ["edr", "triage"]
    assert body["connector"]["type"] == ".none"


@pytest.mark.asyncio
async def test_update_case_status_fetches_version_then_patches(monkeypatch):
    def router(method, url):
        if method == "get":
            return {"id": "case-1", "version": "Wz1d", "status": "open"}
        return [{"id": "case-1", "status": "closed"}]

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c6",
            name="update_elastic_case_status",
            arguments={"case_id": "case-1", "status": "closed"},
        )
    )
    assert not result.is_error
    patch_call = next(c for c in recorder.calls if c["method"] == "patch")
    assert patch_call["json"] == {
        "cases": [{"id": "case-1", "version": "Wz1d", "status": "closed"}]
    }


@pytest.mark.asyncio
async def test_list_cases_rejects_invalid_status(monkeypatch):
    """An out-of-allowlist case status must fail fast with no request, instead
    of silently forwarding a value that yields empty/erroring results."""
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c6b",
            name="list_elastic_cases",
            arguments={"status": "in progress"},
        )
    )
    assert "invalid case status" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_list_cases_rejects_invalid_severity(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c6c",
            name="list_elastic_cases",
            arguments={"severity": "sev1"},
        )
    )
    assert "invalid severity" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_list_cases_forwards_normalized_filters(monkeypatch):
    """Valid filters are normalized (strip + lowercase) and forwarded."""
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"cases": [], "total": 0}
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c6d",
            name="list_elastic_cases",
            arguments={"status": " Open ", "severity": "HIGH"},
        )
    )
    assert not result.is_error
    params = recorder.calls[0]["params"]
    assert params["status"] == "open"
    assert params["severity"] == "high"


@pytest.mark.asyncio
async def test_create_case_rejects_invalid_severity(monkeypatch):
    """An explicit invalid severity must error instead of being silently
    coerced to 'low' (which would create a case with unintended severity)."""
    executor, recorder = _build(monkeypatch, lambda m, u: {"id": "case-x"})
    result = await executor.execute_tool(
        ToolCall(
            id="c6e",
            name="create_elastic_case",
            arguments={
                "title": "t",
                "description": "d",
                "severity": "urgent",
            },
        )
    )
    assert "invalid severity" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_create_case_defaults_blank_severity_to_low(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"id": "case-y"})
    result = await executor.execute_tool(
        ToolCall(
            id="c6f",
            name="create_elastic_case",
            arguments={"title": "t", "description": "d", "severity": ""},
        )
    )
    assert not result.is_error
    assert recorder.calls[-1]["json"]["severity"] == "low"


# ---------------------------------------------------------------------------
# Value lists (IOC enforcement)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_value_list_item_body(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"id": "item-1"})
    result = await executor.execute_tool(
        ToolCall(
            id="c7",
            name="add_elastic_value_list_item",
            arguments={"list_id": "bad-ips", "value": "1.2.3.4"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert "/api/lists/items" in call["url"]
    assert call["json"] == {"list_id": "bad-ips", "value": "1.2.3.4"}


# ---------------------------------------------------------------------------
# Endpoint response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_isolate_endpoint_body(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"data": {"id": "act-1"}})
    result = await executor.execute_tool(
        ToolCall(
            id="c8",
            name="isolate_elastic_endpoint",
            arguments={"endpoint_id": "ep-1", "comment": "contain"},
        )
    )
    assert not result.is_error
    assert "act-1" in result.content
    call = recorder.calls[-1]
    assert "/api/endpoint/action/isolate" in call["url"]
    assert call["json"] == {"endpoint_ids": ["ep-1"], "comment": "contain"}


# ---------------------------------------------------------------------------
# Hunting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_esql_query_formats_columns_and_values(monkeypatch):
    def router(method, url):
        return {
            "columns": [{"name": "host.name"}, {"name": "count"}],
            "values": [["WIN-01", 3], ["WIN-02", 1]],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c9",
            name="run_elastic_esql_query",
            arguments={"query": "FROM logs-* | STATS count = COUNT(*) BY host.name"},
        )
    )
    assert not result.is_error
    assert "host.name" in result.content
    assert "WIN-01" in result.content
    call = recorder.calls[-1]
    assert call["url"].endswith("/_query")
    assert call["json"] == {
        "query": "FROM logs-* | STATS count = COUNT(*) BY host.name"
    }


@pytest.mark.asyncio
async def test_esql_query_renders_zero_and_false_values(monkeypatch):
    """A falsy-but-meaningful cell (0 / False) must render its value, not blank,
    so a COUNT of 0 is distinguishable from a null/missing cell."""
    def router(method, url):
        return {
            "columns": [{"name": "host.name"}, {"name": "count"}, {"name": "ok"}],
            "values": [["WIN-03", 0, False]],
        }

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c9b",
            name="run_elastic_esql_query",
            arguments={"query": "FROM logs-* | LIMIT 1"},
        )
    )
    assert not result.is_error
    assert "WIN-03 | 0 | False" in result.content


@pytest.mark.asyncio
async def test_esql_query_requires_es_url(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {}, token=_TOKEN_NO_ES)
    result = await executor.execute_tool(
        ToolCall(
            id="c10",
            name="run_elastic_esql_query",
            arguments={"query": "FROM logs-*"},
        )
    )
    assert "elasticsearch url" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_search_elastic_rejects_bad_json(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c11",
            name="search_elastic",
            arguments={"index": "logs-*", "query": "not-json{"},
        )
    )
    assert "valid query dsl json" in result.content.lower()
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Space-aware paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_default_space_prefixes_kibana_path(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        lambda m, u: {"hits": {"total": {"value": 0}, "hits": []}},
        token=_TOKEN_SPACE,
    )
    result = await executor.execute_tool(
        ToolCall(id="c12", name="list_elastic_alerts", arguments={})
    )
    assert not result.is_error
    assert "/s/soc/api/detection_engine/signals/search" in recorder.calls[0]["url"]
