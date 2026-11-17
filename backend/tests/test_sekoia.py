"""Tests for the Sekoia.io integration provider.

Covers tool introspection, the Bearer header + base-URL helpers, alert listing
(filter forwarding + formatting), alert detail shaping, workflow status
transitions (valid action UUID mapping + invalid status guard), commenting,
comment-thread limit forwarding, indicator lookup/context, intelligence
search (request shape + formatting), object detail with and without the
context bundle, the asynchronous event search flow (create job -> poll ->
results), feed consumption (cursor hint), and the consolidated IOC collection
management tool, and percent-encoding of user-supplied ids used as URL path
segments. Every API call routes through the shared ``safe_request`` helper,
which is monkeypatched here so no network is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import sekoia as sk
from app.integrations.providers.sekoia import (
    build_sekoia_headers,
    build_tools,
    sekoia_api_base,
)
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://api.sekoia.io||sk-api-key-123"


class _StubResponse:
    def __init__(self, payload: Any, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.content = b"x"
        self.is_success = 200 <= status_code < 300

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
    monkeypatch.setattr(sk, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Introspection + helpers
# ---------------------------------------------------------------------------


def test_catalog_dummy_token_yields_all_tools():
    """Catalog introspection must see all tools, not an empty list.

    ``build_dummy_token("sekoia")`` must return a compound dummy: the plain
    ``__dummy__`` parses to (base_url="__dummy__", api_key="") and the
    empty-key guard in ``build_tools`` would hide every tool from the
    catalog/UI (see the _URL_DUMMY_PROVIDERS registry).
    """
    from app.api.v1.integration_catalog import build_dummy_token

    tools = build_tools(
        token=build_dummy_token("sekoia"), executor=CallableToolExecutor()
    )
    assert len(tools) == 18


def test_build_tools_returns_definitions_with_dummy_token():
    executor = CallableToolExecutor()
    tools = build_tools(token="https://dummy.example.com||__dummy__", executor=executor)
    names = {t.name for t in tools}
    assert "get_sekoia_status" in names
    assert "lookup_sekoia_indicator" in names
    assert "get_sekoia_indicator_context" in names
    assert "search_sekoia_intelligence" in names
    assert "get_sekoia_feed_objects" in names
    assert "manage_sekoia_ioc_collections" in names
    assert "list_sekoia_alerts" in names
    assert "get_sekoia_alert" in names
    assert "update_sekoia_alert_status" in names
    assert "search_sekoia_events" in names
    assert "list_sekoia_rules" in names
    assert "list_sekoia_assets" in names
    assert len(tools) == 18


def test_build_tools_empty_without_api_key():
    executor = CallableToolExecutor()
    # base URL only, no API key -> no tools
    assert build_tools(token="https://api.sekoia.io||", executor=executor) == []
    # whitespace-only key is treated as missing -> no tools
    assert build_tools(token="https://api.sekoia.io||   ", executor=executor) == []


def test_headers_and_base_url_helpers():
    headers = build_sekoia_headers("key-1")
    assert headers["Authorization"] == "Bearer key-1"
    assert headers["Accept"] == "application/json"
    # Pasted whitespace never produces a malformed Authorization header.
    assert build_sekoia_headers("  key-1 \n")["Authorization"] == "Bearer key-1"
    # default + trailing-slash + whitespace normalisation
    assert sekoia_api_base("") == "https://api.sekoia.io"
    # whitespace-only values fall back to the default host (not "")
    assert sekoia_api_base("   ") == "https://api.sekoia.io"
    assert sekoia_api_base("https://app.mco1.sekoia.io/api/") == "https://app.mco1.sekoia.io/api"
    assert sekoia_api_base("  https://app.mco1.sekoia.io/api  ") == "https://app.mco1.sekoia.io/api"


def test_build_tools_strips_whitespace():
    tools = build_tools(
        token="  https://api.sekoia.io  ||  sk-key  ", executor=CallableToolExecutor()
    )
    assert len(tools) == 18


def test_read_tools_sort_before_writes():
    tools = build_tools(token=_TOKEN, executor=CallableToolExecutor())
    by_name = {t.name: t for t in tools}
    assert by_name["list_sekoia_alerts"].priority == 0
    assert by_name["update_sekoia_alert_status"].priority == 1
    assert by_name["manage_sekoia_ioc_collections"].priority == 1


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_alerts_forwards_filters(monkeypatch):
    def router(method, url):
        return {
            "items": [
                {
                    "uuid": "u-1",
                    "short_id": "ALAAA",
                    "title": "Suspicious PowerShell",
                    "urgency": 90,
                    "status": {"name": "Ongoing"},
                    "rule": {"name": "PowerShell spawn"},
                    "entity": {"name": "entity1"},
                    "alert_type": {"category": "exploit"},
                    "created_at": "2026-07-01T10:00:00Z",
                }
            ],
            "total": 1,
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="list_sekoia_alerts",
            arguments={"status": "Ongoing", "urgency": "80,100", "created_at": "-7d,now"},
        )
    )
    assert not result.is_error
    assert "ALAAA" in result.content
    assert "Suspicious PowerShell" in result.content
    params = recorder.calls[0]["params"]
    assert params["match[status_name]"] == "Ongoing"
    assert params["range[urgency]"] == "80,100"
    assert params["date[created_at]"] == "-7d,now"
    assert recorder.calls[0]["url"].endswith("/v1/sic/alerts")


@pytest.mark.asyncio
async def test_get_alert_detail(monkeypatch):
    def router(method, url):
        return {
            "uuid": "u-2",
            "short_id": "ALBBB",
            "title": "Beaconing detected",
            "urgency": 70,
            "status": {"name": "Pending"},
            "rule": {"name": "C2 beacon", "severity": 80, "uuid": "r-1"},
            "entity": {"name": "prod", "uuid": "e-1"},
            "alert_type": {"category": "c2"},
            "created_at": "2026-07-01T10:00:00Z",
            "number_of_total_comments": 2,
            "details": "Periodic outbound connections to 1.2.3.4.",
        }

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="c2", name="get_sekoia_alert", arguments={"alert_id": "ALBBB"})
    )
    assert not result.is_error
    assert "Beaconing detected" in result.content
    assert "C2 beacon" in result.content
    assert "1.2.3.4" in result.content


@pytest.mark.asyncio
async def test_update_alert_status_maps_action_uuid(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c3",
            name="update_sekoia_alert_status",
            arguments={"alert_id": "ALCCC", "status": "acknowledged", "comment": "triaged"},
        )
    )
    assert not result.is_error
    assert "acknowledged" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "patch"
    assert call["url"].endswith("/v1/sic/alerts/ALCCC/workflow")
    assert call["json"]["action_uuid"] == "8f206505-af6d-433e-93f4-775d46dc7d0f"
    assert call["json"]["comment"] == "triaged"


@pytest.mark.asyncio
async def test_update_alert_status_rejects_invalid(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c4",
            name="update_sekoia_alert_status",
            arguments={"alert_id": "ALCCC", "status": "escalated"},
        )
    )
    assert "status must be one of" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_comment_alert_posts_body(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c5",
            name="comment_sekoia_alert",
            arguments={"alert_id": "ALDDD", "content": "Escalating to L2."},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"].endswith("/v1/sic/alerts/ALDDD/comments")
    assert call["json"] == {"content": "Escalating to L2."}


@pytest.mark.asyncio
async def test_list_alert_comments_forwards_limit(monkeypatch):
    def router(method, url):
        return {
            "items": [
                {
                    "created_at": "2026-07-01T10:00:00Z",
                    "author": "analyst1",
                    "created_by_type": "user",
                    "content": "Confirmed true positive.",
                }
            ],
            "total": 1,
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c17",
            name="list_sekoia_alert_comments",
            arguments={"alert_id": "ALEEE", "limit": 5},
        )
    )
    assert not result.is_error
    assert "Confirmed true positive." in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/v1/sic/alerts/ALEEE/comments")
    assert call["params"] == {"limit": 5}


@pytest.mark.asyncio
async def test_path_segments_are_percent_encoded(monkeypatch):
    """Ids containing ``/`` cannot escape their URL path segment."""
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    await executor.execute_tool(
        ToolCall(
            id="c21",
            name="get_sekoia_alert",
            arguments={"alert_id": "ALXXX/../../admin"},
        )
    )
    assert recorder.calls[0]["url"].endswith("/v1/sic/alerts/ALXXX%2F..%2F..%2Fadmin")

    recorder.calls.clear()
    await executor.execute_tool(
        ToolCall(
            id="c22",
            name="get_sekoia_intelligence_object",
            arguments={"object_id": "malware--1/context?x=1"},
        )
    )
    assert recorder.calls[0]["url"].endswith(
        "/v2/inthreat/objects/malware--1%2Fcontext%3Fx%3D1"
    )

    recorder.calls.clear()
    await executor.execute_tool(
        ToolCall(
            id="c23",
            name="manage_sekoia_ioc_collections",
            arguments={
                "action": "delete_indicator",
                "collection_id": "col/1",
                "indicator_id": "ind/2",
            },
        )
    )
    assert recorder.calls[0]["url"].endswith(
        "/v2/inthreat/ioc-collections/col%2F1/indicators/ind%2F2"
    )


# ---------------------------------------------------------------------------
# Intelligence Center (CTI)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_intelligence_request_shape_and_formatting(monkeypatch):
    def router(method, url):
        return {
            "items": [
                {
                    "type": "intrusion-set",
                    "id": "intrusion-set--1",
                    "name": "APT28",
                    "description": "Russian state-sponsored threat actor.",
                }
            ],
            "total": 1,
            "has_more": True,
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c18",
            name="search_sekoia_intelligence",
            arguments={"term": "APT28", "limit": 5},
        )
    )
    assert not result.is_error
    assert "APT28" in result.content
    assert "intrusion-set" in result.content
    assert "More results available" in result.content
    call = recorder.calls[0]
    # The vendor's own automation library sends the search as a POST with
    # term/limit as query parameters (no JSON body).
    assert call["method"] == "post"
    assert call["url"].endswith("/v2/inthreat/objects/search")
    assert call["params"] == {"term": "APT28", "limit": 5}
    assert "json" not in call


@pytest.mark.asyncio
async def test_get_intelligence_object_with_context(monkeypatch):
    def router(method, url):
        if url.endswith("/v2/inthreat/objects/malware--1/context"):
            return {
                "items": [
                    {"type": "malware", "id": "malware--1", "name": "Emotet"},
                    {"type": "intrusion-set", "id": "intrusion-set--1", "name": "TA542"},
                    {"type": "course-of-action", "id": "course-of-action--1", "name": "Block C2"},
                    {"type": "relationship", "id": "relationship--1"},
                    {"type": "marking-definition", "id": "marking-definition--1"},
                ]
            }
        return {
            "type": "malware",
            "id": "malware--1",
            "name": "Emotet",
            "created": "2020-01-01T00:00:00Z",
            "modified": "2026-01-01T00:00:00Z",
            "description": "Modular banking trojan turned loader.",
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c19",
            name="get_sekoia_intelligence_object",
            arguments={"object_id": "malware--1", "include_context": True},
        )
    )
    assert not result.is_error
    assert "Emotet" in result.content
    assert "Related objects (2)" in result.content
    assert "TA542" in result.content
    assert "Block C2" in result.content
    # The object itself and STIX plumbing are filtered out of the context list.
    assert "relationship" not in result.content
    assert "marking-definition" not in result.content
    urls = [c["url"] for c in recorder.calls]
    assert urls[0].endswith("/v2/inthreat/objects/malware--1")
    assert urls[1].endswith("/v2/inthreat/objects/malware--1/context")


@pytest.mark.asyncio
async def test_get_intelligence_object_without_context(monkeypatch):
    def router(method, url):
        return {
            "type": "malware",
            "id": "malware--1",
            "name": "Emotet",
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c20",
            name="get_sekoia_intelligence_object",
            arguments={"object_id": "malware--1"},
        )
    )
    assert not result.is_error
    assert "Emotet" in result.content
    # No context request is made unless include_context is set.
    assert len(recorder.calls) == 1


@pytest.mark.asyncio
async def test_lookup_indicator_unknown_ioc(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"items": [], "total": 0})
    result = await executor.execute_tool(
        ToolCall(
            id="c6",
            name="lookup_sekoia_indicator",
            arguments={"value": "10.9.8.7", "type": "ipv4-addr"},
        )
    )
    assert not result.is_error
    assert "unknown" in result.content.lower()
    params = recorder.calls[0]["params"]
    assert params["value"] == "10.9.8.7"
    assert params["type"] == "ipv4-addr"


@pytest.mark.asyncio
async def test_indicator_context_groups_related_objects(monkeypatch):
    def router(method, url):
        return {
            "items": [
                {"type": "indicator", "id": "indicator--1", "name": "[ipv4] 1.2.3.4"},
                {"type": "malware", "id": "malware--1", "name": "Emotet"},
                {"type": "intrusion-set", "id": "intrusion-set--1", "name": "TA542"},
                {"type": "relationship", "id": "relationship--1"},
            ]
        }

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c7",
            name="get_sekoia_indicator_context",
            arguments={"value": "1.2.3.4", "type": "ipv4-addr"},
        )
    )
    assert not result.is_error
    assert "Emotet" in result.content
    assert "TA542" in result.content
    assert "4 related objects" in result.content


@pytest.mark.asyncio
async def test_feed_objects_appends_cursor_hint_on_full_page(monkeypatch):
    def router(method, url):
        return {
            "items": [{"type": "indicator", "id": f"indicator--{i}", "name": f"ioc {i}"} for i in range(2)],
            "next_cursor": "cur-123",
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c8",
            name="get_sekoia_feed_objects",
            arguments={"feed_id": "feed-1", "limit": 2},
        )
    )
    assert not result.is_error
    assert "cur-123" in result.content
    assert recorder.calls[0]["url"].endswith("/v2/inthreat/collections/feed-1/objects")


@pytest.mark.asyncio
async def test_manage_ioc_collections_guards(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="c9", name="manage_sekoia_ioc_collections", arguments={"action": "nuke"})
    )
    assert "action must be one of" in result.content.lower()
    result = await executor.execute_tool(
        ToolCall(
            id="c10",
            name="manage_sekoia_ioc_collections",
            arguments={"action": "list_indicators"},
        )
    )
    assert "collection_id is required" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_ioc_collections_add_indicators(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c11",
            name="manage_sekoia_ioc_collections",
            arguments={
                "action": "add_indicators",
                "collection_id": "ioc-collection--1",
                "indicators": "1.2.3.4\nevil.example.com",
            },
        )
    )
    assert not result.is_error
    assert "2 indicator(s)" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"].endswith("/v2/inthreat/ioc-collections/ioc-collection--1/indicators/text")
    assert call["json"]["indicators"] == "1.2.3.4\nevil.example.com"


# ---------------------------------------------------------------------------
# Event search (async job flow)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_events_full_flow(monkeypatch):
    async def _fast_sleep(_seconds):
        return None

    monkeypatch.setattr(sk.asyncio, "sleep", _fast_sleep)

    def router(method, url):
        if url.endswith("/v1/sic/conf/events/search/jobs"):
            return {"uuid": "job-1"}
        if url.endswith("/v1/sic/conf/events/search/jobs/job-1"):
            return {"status": 2}
        if url.endswith("/v1/sic/conf/events/search/jobs/job-1/events"):
            return {
                "items": [{"@timestamp": "2026-07-01T10:00:00Z", "source": {"ip": "1.2.3.4"}}],
                "total": 1,
            }
        return {}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c12",
            name="search_sekoia_events",
            arguments={"query": 'source.ip:"1.2.3.4"', "earliest_time": "-30d"},
        )
    )
    assert not result.is_error
    assert "1.2.3.4" in result.content
    start = next(c for c in recorder.calls if c["url"].endswith("/search/jobs"))
    assert start["json"]["term"] == 'source.ip:"1.2.3.4"'
    assert start["json"]["earliest_time"] == "-30d"
    assert start["json"]["term_lang"] == "dork"


@pytest.mark.asyncio
async def test_search_events_reports_failed_job(monkeypatch):
    async def _fast_sleep(_seconds):
        return None

    monkeypatch.setattr(sk.asyncio, "sleep", _fast_sleep)

    def router(method, url):
        if url.endswith("/search/jobs"):
            return {"uuid": "job-2"}
        return {"status": 4}

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="c13", name="search_sekoia_events", arguments={"query": "x"})
    )
    assert "failed" in result.content.lower()


# ---------------------------------------------------------------------------
# Rules + assets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_rules_forwards_name_match(monkeypatch):
    def router(method, url):
        return {
            "items": [
                {"uuid": "r-1", "name": "Suspicious Defender Exclusion", "enabled": True, "severity": 40, "type": "sigma"}
            ],
            "total": 1,
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="c14", name="list_sekoia_rules", arguments={"name": "Defender"})
    )
    assert not result.is_error
    assert "Suspicious Defender Exclusion" in result.content
    assert recorder.calls[0]["params"]["match[name]"] == "Defender"
    assert recorder.calls[0]["url"].endswith("/v1/sic/conf/rules-catalog/rules")


@pytest.mark.asyncio
async def test_list_rules_handles_bare_list_response(monkeypatch):
    def router(method, url):
        return [
            {"uuid": "r-2", "name": "Bare List Rule", "enabled": False, "severity": 20, "type": "sigma"}
        ]

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="c16", name="list_sekoia_rules", arguments={})
    )
    assert not result.is_error
    assert "Bare List Rule" in result.content


@pytest.mark.asyncio
async def test_get_asset_detail(monkeypatch):
    def router(method, url):
        return {
            "uuid": "a-1",
            "name": "web-server-01",
            "asset_type": {"name": "computer"},
            "criticity": {"value": 60},
            "created_at": "2026-01-01T00:00:00Z",
            "attributes": [{"uuid": "at-1", "name": "ip", "value": "10.0.0.5"}],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="c15", name="get_sekoia_asset", arguments={"asset_uuid": "a-1"})
    )
    assert not result.is_error
    assert "web-server-01" in result.content
    assert "10.0.0.5" in result.content
    assert recorder.calls[0]["url"].endswith("/v1/asset-management/assets/a-1")
