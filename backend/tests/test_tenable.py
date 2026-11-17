"""Tests for the Tenable Vulnerability Management integration provider.

Covers tool introspection, the X-ApiKeys header + base-URL helpers, the
workbenches vulnerability listing (severity filter + severity/count sort),
plugin/CVE detail shaping, scan control (launch + pause/resume/stop guard),
and the async vulnerability export flow (request -> poll -> first chunk).
Every API call routes through the shared ``safe_request`` helper, which is
monkeypatched here so no network is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import tenable as tn
from app.integrations.providers.tenable import (
    build_tenable_headers,
    build_tools,
    tenable_api_base,
)
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://cloud.tenable.com||ak-123||sk-456"


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
    monkeypatch.setattr(tn, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Introspection + helpers
# ---------------------------------------------------------------------------


def test_build_tools_returns_definitions_with_dummy_token():
    executor = CallableToolExecutor()
    tools = build_tools(
        token="https://dummy.example.com||__dummy__||__dummy__", executor=executor
    )
    names = {t.name for t in tools}
    assert "list_tenable_vulnerabilities" in names
    assert "get_tenable_vulnerability" in names
    assert "list_tenable_assets" in names
    assert "export_tenable_vulnerabilities" in names
    assert "launch_tenable_scan" in names
    assert "manage_tenable_scan" in names
    assert len(tools) >= 14


def test_build_tools_empty_without_keys():
    executor = CallableToolExecutor()
    # base URL only, no access/secret key -> no tools
    assert build_tools(token="https://cloud.tenable.com||", executor=executor) == []


def test_headers_and_base_url_helpers():
    headers = build_tenable_headers("ak", "sk")
    assert headers["X-ApiKeys"] == "accessKey=ak;secretKey=sk"
    assert headers["Accept"] == "application/json"
    # default + trailing-slash normalisation
    assert tenable_api_base("") == "https://cloud.tenable.com"
    assert tenable_api_base("https://fedcloud.tenable.com/") == "https://fedcloud.tenable.com"
    # Credentials/base URL are stripped so pasted leading/trailing whitespace can
    # never produce a malformed X-ApiKeys header or base URL.
    assert (
        build_tenable_headers("  ak  ", "\tsk\n")["X-ApiKeys"]
        == "accessKey=ak;secretKey=sk"
    )
    assert tenable_api_base("  https://fedcloud.tenable.com/  ") == "https://fedcloud.tenable.com"


def test_build_tools_strips_whitespace_and_guards_blank_keys():
    # Whitespace around each compound-token segment is stripped, so tools still
    # build cleanly from a copy/pasted token.
    tools = build_tools(
        token="  https://cloud.tenable.com  ||  ak-123  ||  sk-456  ",
        executor=CallableToolExecutor(),
    )
    assert len(tools) >= 14
    # A whitespace-only key is treated as missing -> no tools.
    assert (
        build_tools(token="https://cloud.tenable.com||   ||   ", executor=CallableToolExecutor())
        == []
    )


# ---------------------------------------------------------------------------
# Vulnerabilities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_vulnerabilities_filters_and_sorts(monkeypatch):
    def router(method, url):
        return {
            "vulnerabilities": [
                {"plugin_id": 1, "plugin_name": "Low bug", "plugin_family": "General", "severity": 1, "count": 9},
                {"plugin_id": 2, "plugin_name": "Crit RCE", "plugin_family": "Web", "severity": 4, "count": 3},
                {"plugin_id": 3, "plugin_name": "Crit auth", "plugin_family": "Web", "severity": 4, "count": 7},
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="c1", name="list_tenable_vulnerabilities", arguments={"severity": "Critical"})
    )
    assert not result.is_error
    # Only critical findings, highest count first
    assert "Crit auth" in result.content
    assert "Low bug" not in result.content
    assert result.content.index("Crit auth") < result.content.index("Crit RCE")
    # date_range param forwarded
    assert recorder.calls[0]["params"]["date_range"] == 30


@pytest.mark.asyncio
async def test_get_vulnerability_extracts_cves_and_cvss(monkeypatch):
    def router(method, url):
        return {
            "info": {
                "plugin_details": {"name": "Apache RCE", "family": "Web Servers", "severity": 4},
                "risk_information": {"cvss3_base_score": "9.8", "risk_factor": "Critical"},
                "reference_information": [{"name": "cve", "values": ["CVE-2021-1234", "CVE-2021-9999"]}],
                "solution": "Upgrade Apache.",
                "count": 5,
            }
        }

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="c2", name="get_tenable_vulnerability", arguments={"plugin_id": 42})
    )
    assert not result.is_error
    assert "CVE-2021-1234" in result.content
    assert "9.8" in result.content
    assert "Upgrade Apache" in result.content


# ---------------------------------------------------------------------------
# Scans
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_launch_scan(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"scan_uuid": "run-789"})
    result = await executor.execute_tool(
        ToolCall(id="c3", name="launch_tenable_scan", arguments={"scan_id": 5})
    )
    assert not result.is_error
    assert "run-789" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"].endswith("/scans/5/launch")


@pytest.mark.asyncio
async def test_manage_scan_rejects_invalid_action(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="c4", name="manage_tenable_scan", arguments={"scan_id": 5, "action": "nuke"})
    )
    assert "action must be one of" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_scan_valid_action(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="c5", name="manage_tenable_scan", arguments={"scan_id": 5, "action": "pause"})
    )
    assert not result.is_error
    assert recorder.calls[-1]["url"].endswith("/scans/5/pause")


# ---------------------------------------------------------------------------
# Bulk vulnerability export (request -> poll -> chunk)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_full_flow(monkeypatch):
    async def _fast_sleep(_seconds):
        return None

    monkeypatch.setattr(tn.asyncio, "sleep", _fast_sleep)

    def router(method, url):
        if url.endswith("/vulns/export"):
            return {"export_uuid": "exp-1"}
        if url.endswith("/vulns/export/exp-1/status"):
            return {"status": "FINISHED", "chunks_available": [1]}
        if url.endswith("/vulns/export/exp-1/chunks/1"):
            return [
                {
                    "asset": {"hostname": "web-01"},
                    "plugin": {"id": 99, "name": "Critical RCE", "severity": 4},
                    "state": "OPEN",
                }
            ]
        return {}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c6",
            name="export_tenable_vulnerabilities",
            arguments={"severities": "critical,high", "state": "open"},
        )
    )
    assert not result.is_error
    assert "web-01" in result.content
    assert "Critical RCE" in result.content
    # The export request body carries the normalised filters
    start = next(c for c in recorder.calls if c["url"].endswith("/vulns/export"))
    assert start["json"]["filters"]["severity"] == ["critical", "high"]
    assert start["json"]["filters"]["state"] == ["open"]
    assert "since" in start["json"]["filters"]


@pytest.mark.asyncio
async def test_export_drops_invalid_severities(monkeypatch):
    async def _fast_sleep(_seconds):
        return None

    monkeypatch.setattr(tn.asyncio, "sleep", _fast_sleep)

    def router(method, url):
        if url.endswith("/vulns/export"):
            return {"export_uuid": "exp-2"}
        if url.endswith("/status"):
            return {"status": "FINISHED", "chunks_available": [1]}
        return []

    executor, recorder = _build(monkeypatch, router)
    await executor.execute_tool(
        ToolCall(
            id="c7",
            name="export_tenable_vulnerabilities",
            arguments={"severities": "bogus,critical", "state": "weird"},
        )
    )
    start = next(c for c in recorder.calls if c["url"].endswith("/vulns/export"))
    # invalid severity dropped (only critical kept); invalid state falls back to default
    assert start["json"]["filters"]["severity"] == ["critical"]
    assert start["json"]["filters"]["state"] == ["open", "reopened"]
