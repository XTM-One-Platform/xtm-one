"""Tests for the Rapid7 Insight Platform integration provider.

Covers tool introspection, the X-Api-Key header + base-URL helpers, the
InsightIDR investigation listing (filter forwarding) and consolidated update
(per-field PUT fan-out + validation guard), the comment body shape, the async
Log Search flow (start -> poll links[].href -> events), the InsightVM
vulnerability detail shaping (CVSS / CVEs / solution), and the consolidated
threat-indicator management (action guard + create/add). Every API call routes
through the shared ``safe_request`` helper, which is monkeypatched here so no
network is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import rapid7 as rp
from app.integrations.providers.rapid7 import (
    build_rapid7_headers,
    build_tools,
    rapid7_api_base,
)
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://us.api.insight.rapid7.com||key-123"


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
    monkeypatch.setattr(rp, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Introspection + helpers
# ---------------------------------------------------------------------------


def test_build_tools_returns_definitions_with_dummy_token():
    executor = CallableToolExecutor()
    tools = build_tools(
        token="https://dummy.example.com||__dummy__", executor=executor
    )
    names = {t.name for t in tools}
    assert "get_rapid7_status" in names
    assert "list_rapid7_investigations" in names
    assert "update_rapid7_investigation" in names
    assert "add_rapid7_investigation_comment" in names
    assert "run_rapid7_log_search" in names
    assert "list_rapid7_assets" in names
    assert "get_rapid7_vulnerability" in names
    assert "manage_rapid7_threat_indicators" in names
    # Exact count - the provider registers a fixed set of 15 tools when the API
    # key is present, so assert the precise number to catch an accidentally
    # dropped (or duplicated) tool instead of letting it slip past a >= guard.
    assert len(tools) == 15


def test_build_tools_empty_without_key():
    executor = CallableToolExecutor()
    # base URL only, no API key -> no tools
    assert (
        build_tools(token="https://us.api.insight.rapid7.com||", executor=executor)
        == []
    )


def test_build_tools_empty_with_whitespace_only_key():
    executor = CallableToolExecutor()
    # A whitespace-only API key is stripped and treated as missing -> no tools.
    assert (
        build_tools(
            token="https://us.api.insight.rapid7.com||   ", executor=executor
        )
        == []
    )


@pytest.mark.asyncio
async def test_build_tools_defaults_region_when_base_url_blank(monkeypatch):
    """A blank base_url ("||<key>") still exposes tools, defaulting to the us host."""
    recorder = _Recorder(lambda m, u: {"message": "Authorized"})
    monkeypatch.setattr(rp, "safe_request", recorder)
    executor = CallableToolExecutor()
    tools = build_tools(token="||key-123", executor=executor)
    assert tools, "blank base_url should fall back to the default region host"
    result = await executor.execute_tool(
        ToolCall(id="d1", name="get_rapid7_status", arguments={})
    )
    assert not result.is_error
    assert recorder.calls[0]["url"] == "https://us.api.insight.rapid7.com/validate"


def test_headers_and_base_url_helpers():
    headers = build_rapid7_headers("abc")
    assert headers["X-Api-Key"] == "abc"
    assert headers["Accept"] == "application/json"
    # The API key is stripped so pasted whitespace/newlines can't produce a
    # malformed (h11-rejected) X-Api-Key header.
    assert build_rapid7_headers("  abc\n")["X-Api-Key"] == "abc"
    # default + trailing-slash normalisation
    assert rapid7_api_base("") == "https://us.api.insight.rapid7.com"
    assert (
        rapid7_api_base("https://eu.api.insight.rapid7.com/")
        == "https://eu.api.insight.rapid7.com"
    )
    # Surrounding whitespace is stripped before the trailing-slash trim, and a
    # whitespace-only base_url falls back to the default region host.
    assert (
        rapid7_api_base("  https://eu.api.insight.rapid7.com/  ")
        == "https://eu.api.insight.rapid7.com"
    )
    assert rapid7_api_base("   ") == "https://us.api.insight.rapid7.com"


# ---------------------------------------------------------------------------
# Investigations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_investigations_forwards_filters(monkeypatch):
    def router(method, url):
        return {
            "data": [
                {
                    "rrn": "rrn:investigation:us:o:investigation:ABC",
                    "title": "Suspicious login",
                    "status": "OPEN",
                    "priority": "CRITICAL",
                    "disposition": "UNDECIDED",
                    "assignee": {"email": "soc@acme.com"},
                    "created_time": "2026-01-02T03:04:05Z",
                    "source": "ALERT",
                }
            ],
            "metadata": {"total_data": 1},
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="list_rapid7_investigations",
            arguments={"statuses": "open", "priorities": "high,critical"},
        )
    )
    assert not result.is_error
    assert "Suspicious login" in result.content
    assert "soc@acme.com" in result.content
    params = recorder.calls[0]["params"]
    assert params["statuses"] == "OPEN"
    assert params["priorities"] == "HIGH,CRITICAL"
    # InsightIDR investigations require the preview Accept-version header
    assert recorder.calls[0]["headers"]["Accept-version"] == "investigations-preview"


@pytest.mark.asyncio
async def test_list_investigations_normalizes_spaced_filters(monkeypatch):
    """Spaces in comma-separated filters are stripped so the API never sees them."""
    executor, recorder = _build(monkeypatch, lambda m, u: {"data": []})
    result = await executor.execute_tool(
        ToolCall(
            id="c1b",
            name="list_rapid7_investigations",
            arguments={"statuses": "open, closed", "priorities": " high , critical "},
        )
    )
    assert not result.is_error
    params = recorder.calls[0]["params"]
    assert params["statuses"] == "OPEN,CLOSED"
    assert params["priorities"] == "HIGH,CRITICAL"


@pytest.mark.asyncio
async def test_update_investigation_fans_out_per_field(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c2",
            name="update_rapid7_investigation",
            arguments={
                "investigation_id": "INV-1",
                "status": "closed",
                "disposition": "benign",
                "assignee_email": "analyst@acme.com",
            },
        )
    )
    assert not result.is_error
    urls = [c["url"] for c in recorder.calls]
    assert any(u.endswith("/investigations/INV-1/status/CLOSED") for u in urls)
    assert any(u.endswith("/investigations/INV-1/disposition/BENIGN") for u in urls)
    assignee_call = next(c for c in recorder.calls if c["url"].endswith("/assignee"))
    assert assignee_call["method"] == "put"
    assert assignee_call["json"] == {"user_email_address": "analyst@acme.com"}


@pytest.mark.asyncio
async def test_update_investigation_rejects_invalid_status(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c3",
            name="update_rapid7_investigation",
            arguments={"investigation_id": "INV-1", "status": "nuke"},
        )
    )
    assert "status must be one of" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_update_investigation_requires_a_field(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c4",
            name="update_rapid7_investigation",
            arguments={"investigation_id": "INV-1"},
        )
    )
    assert "no fields provided" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_add_comment_body_and_version(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    rrn = "rrn:investigation:us:o:investigation:ABC"
    result = await executor.execute_tool(
        ToolCall(
            id="c5",
            name="add_rapid7_investigation_comment",
            arguments={"investigation_rrn": rrn, "body": "Triaged - benign."},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["url"].endswith("/idr/v1/comments")
    assert call["json"]["target"] == rrn
    assert call["json"]["body"] == "Triaged - benign."
    assert call["headers"]["Accept-version"] == "comments-preview"


# ---------------------------------------------------------------------------
# Log search (async poll)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_log_search_polls_until_events(monkeypatch):
    async def _fast_sleep(_seconds):
        return None

    monkeypatch.setattr(rp.asyncio, "sleep", _fast_sleep)
    poll_href = "https://us.api.insight.rapid7.com/log_search/query/poll-1"

    def router(method, url):
        if url.endswith("/log_search/query/logs/log-1"):
            return {"links": [{"href": poll_href}]}
        if url.endswith("/query/poll-1"):
            return {
                "events": [
                    {"timestamp": 1574208000000, "message": "FAILED login from 10.0.0.1"}
                ]
            }
        return {}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c6",
            name="run_rapid7_log_search",
            arguments={"log_id": "log-1", "query": "where(FAILED_BAD_LOGIN)"},
        )
    )
    assert not result.is_error
    assert "FAILED login from 10.0.0.1" in result.content
    # The initial request forwards the time_range + LEQL query
    start = recorder.calls[0]
    assert start["params"]["query"] == "where(FAILED_BAD_LOGIN)"
    assert start["params"]["time_range"] == "Last 24 Hours"
    # It followed the poll href
    assert any(c["url"] == poll_href for c in recorder.calls)


@pytest.mark.asyncio
async def test_run_log_search_rejects_offhost_poll_url(monkeypatch):
    """A poll href pointing at another host is refused (no second request)."""

    async def _fast_sleep(_seconds):
        return None

    monkeypatch.setattr(rp.asyncio, "sleep", _fast_sleep)

    def router(method, url):
        if url.endswith("/log_search/query/logs/log-1"):
            return {"links": [{"href": "https://evil.example.com/steal"}]}
        return {}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c6b",
            name="run_rapid7_log_search",
            arguments={"log_id": "log-1", "query": "where(x)"},
        )
    )
    assert "unexpected host" in result.content.lower()
    # Only the initial start request was made; the off-host href was not followed.
    assert len(recorder.calls) == 1
    assert not any("evil.example.com" in c["url"] for c in recorder.calls)


# ---------------------------------------------------------------------------
# Vulnerabilities (InsightVM)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_vulnerability_extracts_cves_and_cvss(monkeypatch):
    def router(method, url):
        return {
            "data": [
                {
                    "id": "apache-log4j-cve-2021-44228",
                    "title": "Apache Log4j RCE (Log4Shell)",
                    "severity": "Critical",
                    "cves": ["CVE-2021-44228"],
                    "cvss_v3": {"score": 10.0},
                    "cvss_v2": {"score": 9.3},
                    "description": "Remote code execution in Log4j.",
                    "solutions": [{"fix": "Upgrade Log4j to 2.17+."}],
                }
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c7",
            name="get_rapid7_vulnerability",
            arguments={"vulnerability_id": "apache-log4j-cve-2021-44228"},
        )
    )
    assert not result.is_error
    assert "CVE-2021-44228" in result.content
    assert "10.0" in result.content
    assert "Upgrade Log4j" in result.content
    # The lookup filters by vulnKey
    body = recorder.calls[-1]["json"]
    assert (
        body["vulnerability"]
        == "vulnerability.vulnKey = 'apache-log4j-cve-2021-44228'"
    )


@pytest.mark.asyncio
async def test_get_vulnerability_handles_empty(monkeypatch):
    executor, _ = _build(monkeypatch, lambda m, u: {"data": []})
    result = await executor.execute_tool(
        ToolCall(
            id="c8",
            name="get_rapid7_vulnerability",
            arguments={"vulnerability_id": "nope"},
        )
    )
    assert "no vulnerability found" in result.content.lower()


@pytest.mark.asyncio
async def test_get_vulnerability_rejects_quote(monkeypatch):
    """A single quote in the id is rejected before any query is built/sent."""
    executor, recorder = _build(monkeypatch, lambda m, u: {"data": []})
    result = await executor.execute_tool(
        ToolCall(
            id="c8b",
            name="get_rapid7_vulnerability",
            arguments={"vulnerability_id": "x' or '1'='1"},
        )
    )
    assert "single quote" in result.content.lower()
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Threat indicators (consolidated)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_threat_create_returns_key(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"key": "threat-abc"})
    result = await executor.execute_tool(
        ToolCall(
            id="c9",
            name="manage_rapid7_threat_indicators",
            arguments={
                "action": "create",
                "threat_name": "APT-Lab",
                "ips": "1.2.3.4, 5.6.7.8",
            },
        )
    )
    assert not result.is_error
    assert "threat-abc" in result.content
    call = recorder.calls[-1]
    assert call["url"].endswith("/idr/v1/customthreats")
    assert call["json"]["threat"] == "APT-Lab"
    assert call["json"]["indicators"]["ips"] == ["1.2.3.4", "5.6.7.8"]


@pytest.mark.asyncio
async def test_manage_threat_rejects_invalid_action(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c10",
            name="manage_rapid7_threat_indicators",
            arguments={"action": "nuke"},
        )
    )
    assert "action must be one of" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_threat_add_requires_key(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c11",
            name="manage_rapid7_threat_indicators",
            arguments={"action": "add", "ips": "1.2.3.4"},
        )
    )
    assert "threat_key is required" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_threat_add_posts_indicators(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c12",
            name="manage_rapid7_threat_indicators",
            arguments={
                "action": "add",
                "threat_key": "threat-abc",
                "hashes": "deadbeef",
                "domain_names": "evil.test",
            },
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["url"].endswith("/customthreats/key/threat-abc/indicators/add")
    assert call["params"] == {"format": "json"}
    assert call["json"]["hashes"] == ["deadbeef"]
    assert call["json"]["domain_names"] == ["evil.test"]
