"""Tests for the SentinelOne integration provider.

Covers the threat triage flow (mitigate / verdict / incident-status / notes),
agent containment, Deep Visibility hunting (init → poll → events), IOC and
blocklist payload shaping, the nested ``data.sites`` envelope, and tool
introspection. Every API call routes through the shared ``safe_request``
helper, which is monkeypatched here so no network is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import sentinelone as s1
from app.integrations.providers.sentinelone import build_tools
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://mgmt.sentinelone.net||api-token-123"


class _StubResponse:
    def __init__(self, payload: dict[str, Any]):
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _Recorder:
    """Fake ``safe_request`` that routes by (method, url) and records calls."""

    def __init__(self, router):
        self._router = router
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        payload = self._router(method, url)
        return _StubResponse(payload), None


def _build(monkeypatch, router) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(router)
    monkeypatch.setattr(s1, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


def test_build_tools_returns_definitions_with_dummy_token():
    executor = CallableToolExecutor()
    tools = build_tools(token="https://dummy.example.com||__dummy__", executor=executor)
    names = {t.name for t in tools}
    assert "list_sentinelone_threats" in names
    assert "mitigate_sentinelone_threat" in names
    assert "isolate_sentinelone_agent" in names
    assert "run_sentinelone_dv_query" in names
    assert len(tools) >= 20


# ---------------------------------------------------------------------------
# Threats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_threats_builds_filters_and_formats(monkeypatch):
    def router(method, url):
        return {
            "pagination": {"totalItems": 1},
            "data": [
                {
                    "id": "t1",
                    "threatInfo": {
                        "threatName": "evil.exe",
                        "classification": "Malware",
                        "mitigationStatus": "active",
                        "analystVerdict": "true_positive",
                        "incidentStatus": "unresolved",
                        "createdAt": "2024-05-01T12:00:00Z",
                    },
                    "agentRealtimeInfo": {"agentComputerName": "WIN-01"},
                }
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="list_sentinelone_threats",
            arguments={"analyst_verdict": "true_positive", "incident_status": "unresolved"},
        )
    )
    assert not result.is_error
    assert "evil.exe" in result.content
    call = recorder.calls[0]
    assert call["params"]["analystVerdicts"] == "true_positive"
    assert call["params"]["incidentStatuses"] == "unresolved"


@pytest.mark.asyncio
async def test_mitigate_threat_valid_action(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"data": {"affected": 1}})
    result = await executor.execute_tool(
        ToolCall(
            id="c2",
            name="mitigate_sentinelone_threat",
            arguments={"threat_id": "t1", "action": "quarantine"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert "/threats/mitigate/quarantine" in call["url"]
    assert call["json"] == {"filter": {"ids": ["t1"]}}


@pytest.mark.asyncio
async def test_mitigate_threat_rejects_invalid_action(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"data": {}})
    result = await executor.execute_tool(
        ToolCall(
            id="c3",
            name="mitigate_sentinelone_threat",
            arguments={"threat_id": "t1", "action": "nuke"},
        )
    )
    assert "invalid mitigation action" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_update_verdict_body(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"data": {"affected": 1}})
    result = await executor.execute_tool(
        ToolCall(
            id="c4",
            name="update_sentinelone_threat_verdict",
            arguments={"threat_id": "t1", "verdict": "false_positive"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert "/threats/analyst-verdict" in call["url"]
    assert call["json"]["data"] == {"analystVerdict": "false_positive"}
    assert call["json"]["filter"] == {"ids": ["t1"]}


@pytest.mark.asyncio
async def test_update_status_rejects_invalid(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"data": {}})
    result = await executor.execute_tool(
        ToolCall(
            id="c5",
            name="update_sentinelone_threat_status",
            arguments={"threat_id": "t1", "status": "bogus"},
        )
    )
    assert "invalid incident status" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_add_threat_note_body(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"data": {}})
    result = await executor.execute_tool(
        ToolCall(
            id="c6",
            name="add_sentinelone_threat_note",
            arguments={"threat_id": "t1", "text": "Investigated, benign."},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert "/threats/notes" in call["url"]
    assert call["json"] == {
        "data": {"text": "Investigated, benign."},
        "filter": {"ids": ["t1"]},
    }


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_isolate_agent_uses_disconnect(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"data": {"affected": 1}})
    result = await executor.execute_tool(
        ToolCall(
            id="c7",
            name="isolate_sentinelone_agent",
            arguments={"agent_id": "a1"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert "/agents/actions/disconnect" in call["url"]
    assert call["json"] == {"filter": {"ids": ["a1"]}}


# ---------------------------------------------------------------------------
# Deep Visibility hunting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dv_query_full_flow(monkeypatch):
    async def _fast_sleep(_seconds):
        return None

    monkeypatch.setattr(s1.asyncio, "sleep", _fast_sleep)

    def router(method, url):
        if url.endswith("/dv/init-query"):
            return {"data": {"queryId": "q-1"}}
        if url.endswith("/dv/query-status"):
            return {"data": {"responseState": "FINISHED"}}
        if url.endswith("/dv/events"):
            return {
                "data": [
                    {
                        "eventType": "Process Creation",
                        "agentName": "WIN-01",
                        "processName": "powershell.exe",
                        "eventTime": "2024-05-01T12:00:00Z",
                    }
                ]
            }
        return {"data": []}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c8",
            name="run_sentinelone_dv_query",
            arguments={"query": 'ProcessName containscis "powershell"'},
        )
    )
    assert not result.is_error
    assert "powershell.exe" in result.content
    # init-query must carry the query + a time range
    init_call = next(c for c in recorder.calls if c["url"].endswith("/dv/init-query"))
    assert init_call["json"]["query"] == 'ProcessName containscis "powershell"'
    assert "fromDate" in init_call["json"] and "toDate" in init_call["json"]
    # events fetched with the returned queryId
    events_call = next(c for c in recorder.calls if c["url"].endswith("/dv/events"))
    assert events_call["params"]["queryId"] == "q-1"


@pytest.mark.asyncio
async def test_dv_query_requires_query(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"data": {}})
    result = await executor.execute_tool(
        ToolCall(id="c9", name="run_sentinelone_dv_query", arguments={"query": "  "})
    )
    assert "query string is required" in result.content.lower()
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Threat intelligence IOCs + blocklist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_ioc_payload_uppercases_type(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"data": [{"uuid": "i1"}]})
    result = await executor.execute_tool(
        ToolCall(
            id="c10",
            name="create_sentinelone_ioc",
            arguments={"ioc_type": "sha256", "value": "abc123"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert "/threat-intelligence/iocs" in call["url"]
    indicator = call["json"]["data"][0]
    assert indicator["type"] == "SHA256"
    assert indicator["value"] == "abc123"
    assert indicator["method"] == "EQUALS"
    assert indicator["source"] and indicator["externalId"]


@pytest.mark.asyncio
async def test_add_blocklist_hash_body(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"data": {}})
    result = await executor.execute_tool(
        ToolCall(
            id="c11",
            name="add_sentinelone_blocklist_hash",
            arguments={"sha1": "deadbeef", "os_type": "windows"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert "/restrictions" in call["url"]
    assert call["json"]["data"]["type"] == "black_hash"
    assert call["json"]["data"]["value"] == "deadbeef"
    assert call["json"]["filter"] == {"tenant": True}


# ---------------------------------------------------------------------------
# Inventory (nested data.sites envelope)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_sites_handles_nested_envelope(monkeypatch):
    def router(method, url):
        return {"data": {"sites": [{"id": "s1", "name": "HQ", "state": "active"}]}}

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="c12", name="list_sentinelone_sites", arguments={})
    )
    assert not result.is_error
    assert "HQ" in result.content
