"""Tests for the Splunk integration provider package.

Covers tool introspection, the Bearer/Basic header selection, SPL normalisation
and quoting, URL-encoding of names in request paths, and the core flows
(async search job, notable-event update with status-label mapping, threat-intel
add, KV Store push). API calls route through the shared ``safe_request`` helper,
monkeypatched here so no network is touched.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from app.integrations.providers.splunk import _common, build_tools
from app.integrations.providers.splunk._common import (
    build_splunk_headers,
    normalize_spl,
    spl_quote,
    splunk_api_base,
)
from app.tools.base import CallableToolExecutor, ToolCall

_BASE = "https://splunk.example.com:8089"
_TOKEN = f"{_BASE}||||tok-123"  # Bearer (no username)
_TOKEN_BASIC = f"{_BASE}||admin||pw"  # HTTP Basic


class _Stub:
    def __init__(self, payload: Any):
        self._payload = payload
        self.content = b"{}"
        self.status_code = 200

    def json(self) -> Any:
        return self._payload


class _Router:
    """Fake ``safe_request`` that routes by (method, url) and records calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        m = method.lower()
        self.calls.append({"method": m, "url": url, "action": action, **kwargs})
        if url.endswith("/services/search/jobs") and m == "post":
            return _Stub({"sid": "SID1"}), None
        if url.rstrip("/").endswith("/services/search/jobs/SID1"):
            return _Stub(
                {"entry": [{"content": {"isDone": True, "dispatchState": "DONE", "resultCount": 1}}]}
            ), None
        if "/services/search/v2/jobs/SID1/results" in url:
            return _Stub(
                {"results": [{"_time": "2024-01-01T00:00:00", "user": "alice", "count": "5"}]}
            ), None
        if url.endswith("/services/notable_update") and m == "post":
            return _Stub({"success": True, "message": "1 event updated"}), None
        if "/storage/collections/data/" in url and m == "post":
            return _Stub({"_key": "K1"}), None
        if "/threat_intel/item/" in url and m == "post":
            return _Stub({"status": True, "message": "created"}), None
        return _Stub({"entry": []}), None


def _build(monkeypatch, token: str = _TOKEN) -> tuple[CallableToolExecutor, _Router]:
    router = _Router()
    monkeypatch.setattr(_common, "safe_request", router)

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(_common.asyncio, "sleep", _noop)
    executor = CallableToolExecutor()
    build_tools(token=token, executor=executor)
    return executor, router


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


def test_build_tools_returns_20_tools():
    executor = CallableToolExecutor()
    tools = build_tools(token=_TOKEN, executor=executor)
    names = {t.name for t in tools}
    assert len(tools) == 20
    assert "run_splunk_search" in names
    assert "list_splunk_saved_searches" in names
    assert "list_splunk_es_notable_events" in names
    assert "update_splunk_es_notable_event" in names
    assert "add_splunk_es_threat_intel" in names
    assert "manage_splunk_kvstore_record" in names


def test_build_tools_empty_without_base_url():
    executor = CallableToolExecutor()
    assert build_tools(token="", executor=executor) == []
    # base_url is the first segment; a missing one yields no tools.
    assert build_tools(token="||admin||pw", executor=executor) == []


def test_build_tools_builds_without_secret_for_introspection():
    # base_url present but no secret -> tools still build (call-time creds guard),
    # so the integration catalog can introspect the full tool surface.
    executor = CallableToolExecutor()
    assert len(build_tools(token=f"{_BASE}||||", executor=executor)) == 20


# ---------------------------------------------------------------------------
# Auth header + helpers
# ---------------------------------------------------------------------------


def test_build_splunk_headers_bearer_vs_basic():
    bearer = build_splunk_headers("", "tok")
    assert bearer["Authorization"] == "Bearer tok"
    basic = build_splunk_headers("admin", "pw")
    assert basic["Authorization"] == "Basic " + base64.b64encode(b"admin:pw").decode()


@pytest.mark.asyncio
async def test_basic_auth_compound_token_sends_basic_header(monkeypatch):
    # A base_url||username||token compound with a username selects HTTP Basic
    # end-to-end (token parsing -> make_api -> build_splunk_headers), not just at
    # the header-builder unit level.
    executor, router = _build(monkeypatch, token=_TOKEN_BASIC)
    await executor.execute_tool(
        ToolCall(id="c1", name="list_splunk_indexes", arguments={})
    )
    auth = router.calls[0]["headers"]["Authorization"]
    assert auth == "Basic " + base64.b64encode(b"admin:pw").decode()


@pytest.mark.asyncio
async def test_make_api_forces_output_mode_json(monkeypatch):
    # output_mode=json is authoritative: a caller-supplied params can never
    # override it (the response is always parsed as JSON).
    captured: dict[str, Any] = {}

    async def _fake(_client, method, url, _provider, action, **kwargs):
        captured.update(kwargs)
        return _Stub({"ok": True}), None

    monkeypatch.setattr(_common, "safe_request", _fake)
    api = _common.make_api("https://x:8089", "", "tok")
    await api("get", "/services/x", "test", params={"output_mode": "xml", "count": 5})
    assert captured["params"]["output_mode"] == "json"
    assert captured["params"]["count"] == 5


def test_normalize_spl_prefixes_bare_search():
    assert normalize_spl("index=main error") == "search index=main error"
    assert normalize_spl("| tstats count where index=main by host") == (
        "| tstats count where index=main by host"
    )
    assert normalize_spl("`notable`") == "`notable`"
    assert normalize_spl("search index=x") == "search index=x"


def test_spl_quote_escapes_quotes_and_backslashes():
    assert spl_quote('a"b\\c') == 'a\\"b\\\\c'


def test_splunk_api_base_strips_whitespace_and_slash():
    assert splunk_api_base("  https://x:8089/  ") == "https://x:8089"


# ---------------------------------------------------------------------------
# Search Job flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_search_creates_job_and_formats_results(monkeypatch):
    executor, router = _build(monkeypatch)
    res = await executor.execute_tool(
        ToolCall(id="c1", name="run_splunk_search", arguments={"query": "index=main error"})
    )
    assert "alice" in res.content
    create = next(
        c for c in router.calls if c["url"].endswith("/services/search/jobs") and c["method"] == "post"
    )
    # Bare SPL is auto-prefixed; default lookback applied.
    assert create["data"]["search"] == "search index=main error"
    assert create["data"]["earliest_time"] == "-24h"


@pytest.mark.asyncio
async def test_creds_error_without_secret(monkeypatch):
    executor, _ = _build(monkeypatch, token=f"{_BASE}||||")
    res = await executor.execute_tool(
        ToolCall(id="c1", name="run_splunk_search", arguments={"query": "index=main"})
    )
    assert "credentials are not configured" in res.content


# ---------------------------------------------------------------------------
# Enterprise Security notable events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_notable_maps_status_label_to_id(monkeypatch):
    executor, router = _build(monkeypatch)
    res = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="update_splunk_es_notable_event",
            arguments={
                "event_id": "RULE@@notable@@h",
                "status": "In Progress",
                "comment": "triaging",
            },
        )
    )
    call = next(c for c in router.calls if c["url"].endswith("/services/notable_update"))
    assert call["data"]["ruleUIDs"] == "RULE@@notable@@h"
    assert call["data"]["status"] == "2"  # "In Progress" -> default reviewstatus id 2
    assert call["data"]["comment"] == "triaging"
    assert "updated" in res.content.lower()


@pytest.mark.asyncio
async def test_update_notable_requires_a_field(monkeypatch):
    executor, _ = _build(monkeypatch)
    res = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="update_splunk_es_notable_event",
            arguments={"event_id": "RULE@@notable@@h"},
        )
    )
    assert "at least one of" in res.content


# ---------------------------------------------------------------------------
# Threat intelligence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_threat_intel_sends_item_json(monkeypatch):
    executor, router = _build(monkeypatch)
    res = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="add_splunk_es_threat_intel",
            arguments={"collection": "ip_intel", "item": {"ip": "1.2.3.4"}},
        )
    )
    call = next(
        c for c in router.calls if "/threat_intel/item/ip_intel" in c["url"] and c["method"] == "post"
    )
    assert json.loads(call["data"]["item"]) == {"ip": "1.2.3.4"}
    assert "added" in res.content.lower()


@pytest.mark.asyncio
async def test_add_threat_intel_rejects_unknown_collection(monkeypatch):
    executor, _ = _build(monkeypatch)
    res = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="add_splunk_es_threat_intel",
            arguments={"collection": "bogus", "item": {"ip": "1.2.3.4"}},
        )
    )
    assert "collection must be one of" in res.content


# ---------------------------------------------------------------------------
# KV Store push + URL encoding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kvstore_add_uses_json_body_and_returns_key(monkeypatch):
    executor, router = _build(monkeypatch)
    res = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="manage_splunk_kvstore_record",
            arguments={"action": "add", "collection": "threatlist", "record": {"ip": "1.2.3.4"}},
        )
    )
    call = next(
        c
        for c in router.calls
        if "/storage/collections/data/threatlist" in c["url"] and c["method"] == "post"
    )
    assert call["json"] == {"ip": "1.2.3.4"}
    assert "K1" in res.content


@pytest.mark.asyncio
async def test_get_saved_search_url_encodes_name(monkeypatch):
    executor, router = _build(monkeypatch)
    await executor.execute_tool(
        ToolCall(
            id="c1", name="get_splunk_saved_search", arguments={"name": "My Search/Foo"}
        )
    )
    url = router.calls[0]["url"]
    assert url.endswith("/services/saved/searches/My%20Search%2FFoo")
    assert "My Search/Foo" not in url
