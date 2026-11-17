"""Tests for the Infoblox (BloxOne / CSP) integration provider.

Covers tool introspection, the ``Authorization: Token`` header + base-URL
helpers, the IPAM listing flow (``_filter``/``_limit`` building + ``results``
envelope parsing), DNS record filtering, the TIDE threat-intel IOC lookup
(``/tide/api/data/threats/{type}`` + the data-type query param), and the
consolidated custom-list manager (create / add / remove + the action and
required-argument guards). Every API call routes through the shared
``safe_request`` helper, which is monkeypatched here so no network is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import infoblox as ib
from app.integrations.providers.infoblox import (
    build_infoblox_headers,
    build_tools,
    infoblox_api_base,
)
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://csp.infoblox.com||key-123"


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
    monkeypatch.setattr(ib, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Introspection + helpers
# ---------------------------------------------------------------------------


def test_build_tools_returns_definitions_with_dummy_token():
    executor = CallableToolExecutor()
    tools = build_tools(token="https://dummy.example.com||__dummy__", executor=executor)
    names = {t.name for t in tools}
    assert "get_infoblox_status" in names
    assert "list_infoblox_subnets" in names
    assert "list_infoblox_addresses" in names
    assert "list_infoblox_dns_records" in names
    assert "lookup_infoblox_threat" in names
    assert "manage_infoblox_custom_list" in names
    assert len(tools) >= 14


def test_build_tools_empty_without_key():
    executor = CallableToolExecutor()
    # base URL only, no API key -> no tools
    assert build_tools(token="https://csp.infoblox.com||", executor=executor) == []


def test_build_tools_empty_with_whitespace_key():
    # A whitespace-only key must be treated as absent (parse_compound_token does
    # not strip), otherwise unusable tools register and an illegal "Token "
    # header is built on the first request.
    executor = CallableToolExecutor()
    assert build_tools(token="https://csp.infoblox.com||   ", executor=executor) == []


def test_token_header_and_base_url_helpers():
    headers = build_infoblox_headers("abc")
    # Infoblox uses the "Token" prefix, NOT "Bearer".
    assert headers["Authorization"] == "Token abc"
    assert headers["Accept"] == "application/json"
    # surrounding whitespace is stripped from the key
    assert build_infoblox_headers("  abc  ")["Authorization"] == "Token abc"
    # default + trailing-slash normalisation
    assert infoblox_api_base("") == "https://csp.infoblox.com"
    assert infoblox_api_base("https://csp.infoblox.com/") == "https://csp.infoblox.com"


def test_build_headers_omits_authorization_when_blank():
    # A blank / whitespace-only key must not build a "Token " header (httpx/h11
    # rejects the trailing space with an opaque LocalProtocolError); the header
    # is omitted so the request falls through to a clean 401.
    for blank in ("", "   ", None):  # type: ignore[arg-type]
        headers = build_infoblox_headers(blank)
        assert "Authorization" not in headers
        assert headers["Accept"] == "application/json"


# ---------------------------------------------------------------------------
# Connection + IPAM / DNS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_status_lists_ip_spaces(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"results": [{"name": "default", "id": "1"}]})
    result = await executor.execute_tool(ToolCall(id="c0", name="get_infoblox_status", arguments={}))
    assert not result.is_error
    assert "default" in result.content
    assert recorder.calls[0]["url"].endswith("/api/ddi/v1/ipam/ip_space")


@pytest.mark.asyncio
async def test_list_subnets_builds_filter_and_parses_results(monkeypatch):
    def router(method, url):
        return {
            "results": [
                {
                    "address": "10.0.0.0",
                    "cidr": 24,
                    "name": "prod-net",
                    "id": "ipam/subnet/abc",
                    "utilization": {"used": 10, "total": 254},
                }
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="list_infoblox_subnets",
            arguments={"address": "10.0.0.0"},
        )
    )
    assert not result.is_error
    assert "10.0.0.0/24" in result.content
    assert "prod-net" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/api/ddi/v1/ipam/subnet")
    # structured "address" shortcut becomes an equality _filter; paging forwarded
    assert call["params"]["_filter"] == 'address=="10.0.0.0"'
    assert call["params"]["_limit"] == 25
    assert call["params"]["_offset"] == 0


@pytest.mark.asyncio
async def test_list_dns_records_builds_name_and_type_filter(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        lambda m, u: {
            "results": [
                {
                    "absolute_name_spec": "www.example.com.",
                    "type": "A",
                    "dns_rdata": "10.0.0.5",
                    "id": "dns/record/x",
                }
            ]
        },
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c2",
            name="list_infoblox_dns_records",
            arguments={"name": "www", "record_type": "a"},
        )
    )
    assert not result.is_error
    assert "www.example.com" in result.content
    assert "10.0.0.5" in result.content
    # record_type is upper-cased and combined with name into the _filter
    assert recorder.calls[0]["params"]["_filter"] == 'name_in_zone=="www" and type=="A"'


# ---------------------------------------------------------------------------
# Threat Defense - TIDE lookup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_threat_hits_tide_and_parses(monkeypatch):
    def router(method, url):
        return {
            "threat": [
                {
                    "host": "bad.example.com",
                    "class": "Phishing",
                    "property": "Phishing_Generic",
                    "threat_level": 90,
                    "type": "HOST",
                    "detected": "2024-01-01T00:00:00.000Z",
                }
            ],
            "record_count": 1,
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c3",
            name="lookup_infoblox_threat",
            arguments={"indicator": "bad.example.com", "indicator_type": "host"},
        )
    )
    assert not result.is_error
    assert "bad.example.com" in result.content
    assert "Phishing" in result.content
    call = recorder.calls[-1]
    assert call["url"].endswith("/tide/api/data/threats/host")
    # the data type is the query param name carrying the indicator value
    assert call["params"]["host"] == "bad.example.com"


@pytest.mark.asyncio
async def test_lookup_threat_rejects_invalid_type(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c4",
            name="lookup_infoblox_threat",
            arguments={"indicator": "x", "indicator_type": "hash"},
        )
    )
    assert "indicator_type must be one of" in result.content.lower()
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Threat Defense - custom (named) list management
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_custom_list_create(monkeypatch):
    def router(method, url):
        return {"results": {"id": "atcfw/named_list/99", "name": "blocklist"}}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c5",
            name="manage_infoblox_custom_list",
            arguments={
                "action": "create",
                "name": "blocklist",
                "items": "bad.com, 1.2.3.4",
            },
        )
    )
    assert not result.is_error
    assert "blocklist" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"].endswith("/api/atcfw/v1/named_lists")
    assert call["json"]["name"] == "blocklist"
    assert call["json"]["type"] == "custom_list"
    # comma-separated items are split and described
    assert len(call["json"]["items_described"]) == 2
    assert call["json"]["items_described"][0]["item"] == "bad.com"


@pytest.mark.asyncio
async def test_manage_custom_list_add_items_uses_post(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c6",
            name="manage_infoblox_custom_list",
            arguments={"action": "add_items", "list_id": "L1", "items": "evil.com"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"].endswith("/api/atcfw/v1/named_lists/L1/items")
    assert call["json"]["items_described"][0]["item"] == "evil.com"


@pytest.mark.asyncio
async def test_manage_custom_list_remove_items_uses_delete(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c7",
            name="manage_infoblox_custom_list",
            arguments={
                "action": "remove_items",
                "list_id": "L1",
                "items": "evil.com,bad.com",
            },
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "delete"
    assert call["url"].endswith("/api/atcfw/v1/named_lists/L1/items")
    assert call["json"]["items"] == ["evil.com", "bad.com"]


@pytest.mark.asyncio
async def test_manage_custom_list_rejects_invalid_action(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c8",
            name="manage_infoblox_custom_list",
            arguments={"action": "nuke"},
        )
    )
    assert "action must be one of" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_custom_list_create_requires_name(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c9",
            name="manage_infoblox_custom_list",
            arguments={"action": "create"},
        )
    )
    assert "name" in result.content.lower()
    assert recorder.calls == []
