"""Tests for the Palo Alto Cortex XSOAR integration provider.

Covers tool registration count, the incidents/search severity filter (which the
XSOAR API keys as ``level``, not ``severity``), version-aware auth headers
(XSOAR 6 vs 8 standard/advanced), and the deliberate exclusion of XSOAR from the
user-facing public-URL providers (its v8 ``base_url`` is the tenant API host,
not the console UI host). ``safe_request`` is monkeypatched so no network is
touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import xsoar as xs
from app.integrations.providers.xsoar import build_tools, build_xsoar_headers
from app.tools.base import CallableToolExecutor, ToolCall

# base_url || api_key || api_key_id || auth_mode
_TOKEN_V8 = "https://api-tenant.xsoar.example||api-key-abc||4242||advanced"


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


def _build(monkeypatch, router, token=_TOKEN_V8) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(router)
    monkeypatch.setattr(xs, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=token, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Registration + introspection
# ---------------------------------------------------------------------------


def test_build_tools_registers_sixteen_tools():
    """Locks the tool count that the module docstring documents (16)."""
    executor = CallableToolExecutor()
    tools = build_tools(token=_TOKEN_V8, executor=executor)
    names = {t.name for t in tools}
    assert len(tools) == 16
    assert {"get_xsoar_status", "list_xsoar_incidents", "run_xsoar_command",
            "create_xsoar_indicator"} <= names


def test_build_tools_returns_empty_without_creds():
    executor = CallableToolExecutor()
    assert build_tools(token="||||", executor=executor) == []


# ---------------------------------------------------------------------------
# Incidents search — severity is filtered via ``level`` (XSOAR API contract)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_incidents_severity_filter_uses_level(monkeypatch):
    """The XSOAR incidents/search filter keys severity as ``level`` (numeric
    array), distinct from the incident object's ``severity`` field. Renaming it
    to ``severity`` would silently drop the filter — guard against that."""
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"data": [], "total": 0}
    )
    result = await executor.execute_tool(
        ToolCall(
            id="i1",
            name="list_xsoar_incidents",
            arguments={"status": "active", "severity": "high"},
        )
    )
    assert not result.is_error
    ifilter = recorder.calls[0]["json"]["filter"]
    assert ifilter["level"] == [3]  # high == numeric level 3
    assert "severity" not in ifilter
    assert ifilter["status"] == [1]  # active == numeric status 1


@pytest.mark.asyncio
async def test_list_incidents_query_overrides_structured_filters(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"data": [], "total": 0}
    )
    result = await executor.execute_tool(
        ToolCall(
            id="i2",
            name="list_xsoar_incidents",
            arguments={"query": "type:Phishing", "severity": "high"},
        )
    )
    assert not result.is_error
    ifilter = recorder.calls[0]["json"]["filter"]
    assert ifilter["query"] == "type:Phishing"
    assert "level" not in ifilter  # structured filters ignored when query is set


# ---------------------------------------------------------------------------
# Auth headers — XSOAR 6 vs 8 (standard / advanced)
# ---------------------------------------------------------------------------


def test_v6_headers_single_authorization():
    headers = build_xsoar_headers("the-key", "", "")
    assert headers["Authorization"] == "the-key"
    assert "x-xdr-auth-id" not in headers


def test_v8_standard_headers():
    headers = build_xsoar_headers("the-key", "4242", "standard")
    assert headers["Authorization"] == "the-key"
    assert headers["x-xdr-auth-id"] == "4242"
    assert "x-xdr-nonce" not in headers


def test_v8_advanced_headers_hash_and_replay_fields():
    h1 = build_xsoar_headers("the-key", "4242", "advanced")
    assert h1["x-xdr-auth-id"] == "4242"
    assert h1["x-xdr-nonce"] and h1["x-xdr-timestamp"]
    # Authorization is a SHA-256 hex digest of key+nonce+timestamp, not the raw key.
    assert h1["Authorization"] != "the-key"
    assert len(h1["Authorization"]) == 64
    # A fresh nonce is generated per call (replay protection).
    h2 = build_xsoar_headers("the-key", "4242", "advanced")
    assert h1["x-xdr-nonce"] != h2["x-xdr-nonce"]


def test_api_base_prefix_per_generation():
    assert xs.xsoar_api_base("https://api-x.example/", "4242").endswith(
        "/xsoar/public/v1"
    )
    assert xs.xsoar_api_base("https://xsoar.example/", "") == "https://xsoar.example"


# ---------------------------------------------------------------------------
# Pagination size is clamped to a positive lower bound
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_incidents_clamps_nonpositive_size(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"data": [], "total": 0})
    await executor.execute_tool(
        ToolCall(id="c1", name="list_xsoar_incidents", arguments={"size": 0})
    )
    assert recorder.calls[0]["json"]["filter"]["size"] == 1


@pytest.mark.asyncio
async def test_war_room_clamps_nonpositive_count(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"entries": []})
    await executor.execute_tool(
        ToolCall(
            id="c1",
            name="get_xsoar_incident_war_room",
            arguments={"investigation_id": "INC-1", "count": -5},
        )
    )
    assert recorder.calls[0]["json"]["pageSize"] == 1


# ---------------------------------------------------------------------------
# get_xsoar_indicator escapes the value into the Lucene phrase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_indicator_escapes_lucene_phrase(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"iocObjects": []})
    # A value with a double-quote and a backslash would break the
    # ``value:"…"`` Lucene phrase if interpolated raw.
    await executor.execute_tool(
        ToolCall(
            id="c1",
            name="get_xsoar_indicator",
            arguments={"value": 'evil"\\path'},
        )
    )
    query = recorder.calls[0]["json"]["query"]
    # Backslash doubled, embedded quote backslash-escaped — the surrounding
    # phrase quotes stay balanced.
    assert query == 'value:"evil\\"\\\\path"'


def test_lucene_phrase_escaping():
    from app.integrations.providers.xsoar import _lucene_phrase

    assert _lucene_phrase('a"b') == 'a\\"b'
    assert _lucene_phrase("a\\b") == "a\\\\b"
    assert _lucene_phrase("plain") == "plain"


# ---------------------------------------------------------------------------
# Public-URL exclusion (Finding 3): XSOAR 8 base_url is an API host, not UI
# ---------------------------------------------------------------------------


def test_xsoar_excluded_from_user_facing_url_providers():
    from app.services.integration_tools import _USER_FACING_BASE_URL_PROVIDERS

    assert "xsoar" not in _USER_FACING_BASE_URL_PROVIDERS
