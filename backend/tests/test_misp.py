"""Tests for the MISP integration provider.

Covers tool introspection, the RAW ``Authorization`` header (no Bearer/Token
prefix) + base-URL helpers, the attribute/event restSearch flows (body building
+ envelope parsing), event creation with friendly enum inputs, the UUID-based
tag attach/remove tools, sightings, the warninglist checkValue triage tool, and
the consolidated feed manager (action + required-argument guards). Every API
call routes through the shared ``safe_request`` helper, which is monkeypatched
here so no network is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import misp as misp_mod
from app.integrations.providers.misp import (
    build_misp_headers,
    build_tools,
    misp_api_base,
)
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://misp.example.com||key-123"


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
    monkeypatch.setattr(misp_mod, "safe_request", recorder)
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
    assert "search_misp_attributes" in names
    assert "search_misp_events" in names
    assert "get_misp_event" in names
    assert "create_misp_event" in names
    assert "add_misp_attribute" in names
    assert "add_misp_tag" in names
    assert "add_misp_sighting" in names
    assert "check_misp_warninglists" in names
    assert "manage_misp_feed" in names
    assert "delete_misp_event" in names
    assert len(tools) >= 28


def test_tool_priorities_read_write_destructive():
    executor = CallableToolExecutor()
    tools = build_tools(token=_TOKEN, executor=executor)
    by_name = {t.name: t for t in tools}
    # reads sort first, writes next, destructive last
    assert by_name["search_misp_attributes"].priority == 0
    assert by_name["create_misp_event"].priority == 1
    assert by_name["add_misp_attribute"].priority == 1
    assert by_name["delete_misp_event"].priority == 2
    assert by_name["delete_misp_attribute"].priority == 2


def test_build_tools_empty_without_base_url():
    executor = CallableToolExecutor()
    assert build_tools(token="||key-123", executor=executor) == []


def test_build_tools_empty_without_key():
    executor = CallableToolExecutor()
    assert build_tools(token="https://misp.example.com||", executor=executor) == []
    # A whitespace-only key must be treated as absent as well.
    assert build_tools(token="https://misp.example.com||   ", executor=executor) == []


def test_headers_send_raw_key_no_prefix():
    headers = build_misp_headers("abc")
    # MISP sends the automation key RAW - no "Bearer"/"Token" prefix.
    assert headers["Authorization"] == "abc"
    assert headers["Accept"] == "application/json"
    assert headers["Content-Type"] == "application/json"
    # surrounding whitespace is stripped from the key
    assert build_misp_headers("  abc  ")["Authorization"] == "abc"


def test_build_headers_omits_authorization_when_blank():
    for blank in ("", "   ", None):
        headers = build_misp_headers(blank)
        assert "Authorization" not in headers
        assert headers["Accept"] == "application/json"


def test_api_base_normalisation():
    assert misp_api_base("https://misp.example.com/") == "https://misp.example.com"
    assert misp_api_base("  https://misp.example.com  ") == "https://misp.example.com"
    assert misp_api_base("") == ""


@pytest.mark.asyncio
async def test_get_user_info_guards_misconfigured_credentials():
    # A misconfigured integration produces a ``||key`` (no base_url) or
    # ``base||`` (no key) compound token - get_user_info must surface a clear
    # configuration error instead of firing a relative-URL request.
    integration = misp_mod.MISPIntegration()
    info = await integration.get_user_info("||key-123")
    assert "base URL is not configured" in info["error"]
    info = await integration.get_user_info("https://misp.example.com||   ")
    assert "API key is not configured" in info["error"]


# ---------------------------------------------------------------------------
# Search flows (restSearch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_attributes_builds_body_and_parses_envelope(monkeypatch):
    def router(method, url):
        return {
            "response": {
                "Attribute": [
                    {
                        "id": "42",
                        "event_id": "7",
                        "type": "ip-dst",
                        "category": "Network activity",
                        "value": "203.0.113.7",
                        "to_ids": True,
                        "uuid": "aaaa-bbbb",
                    }
                ]
            }
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="search_misp_attributes",
            arguments={
                "value": "203.0.113.7",
                "type": "ip-dst",
                "tags": "tlp:red,!tlp:white",
                "to_ids": True,
            },
        )
    )
    assert not result.is_error
    assert "203.0.113.7" in result.content
    assert "event 7" in result.content
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/attributes/restSearch")
    body = call["json"]
    assert body["value"] == "203.0.113.7"
    assert body["type"] == "ip-dst"
    # comma-separated tags become a list, '!' exclusions preserved
    assert body["tags"] == ["tlp:red", "!tlp:white"]
    assert body["to_ids"] == 1
    assert body["returnFormat"] == "json"


@pytest.mark.asyncio
async def test_search_events_metadata_only_and_threat_level_mapping(monkeypatch):
    def router(method, url):
        return {
            "response": [
                {
                    "Event": {
                        "id": "7",
                        "uuid": "ev-uuid",
                        "info": "Phishing campaign",
                        "date": "2026-07-01",
                        "threat_level_id": "1",
                        "analysis": "2",
                        "published": True,
                        "attribute_count": "12",
                        "Orgc": {"name": "CIRCL"},
                        "Tag": [{"name": "tlp:amber"}],
                    }
                }
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c2",
            name="search_misp_events",
            arguments={"keyword": "phishing", "threat_level": "high"},
        )
    )
    assert not result.is_error
    assert "Phishing campaign" in result.content
    assert "High" in result.content
    assert "tlp:amber" in result.content
    body = recorder.calls[0]["json"]
    # metadata flag prevents pulling full attribute payloads in search results
    assert body["metadata"] == 1
    assert body["searchall"] == 1
    # bare keywords are wrapped in '%' so the LIKE-based wildcard search
    # matches substrings (MISP's searchall is a raw SQL LIKE)
    assert body["value"] == "%phishing%"
    # friendly threat level maps to the numeric MISP ID
    assert body["threat_level_id"] == "1"


@pytest.mark.asyncio
async def test_search_events_rejects_invalid_threat_level(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c3",
            name="search_misp_events",
            arguments={"threat_level": "apocalyptic"},
        )
    )
    assert "threat_level must be one of" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_get_event_appends_deep_link(monkeypatch):
    def router(method, url):
        return {
            "Event": {
                "id": "7",
                "uuid": "ev-uuid",
                "info": "Test event",
                "date": "2026-07-01",
                "threat_level_id": "3",
                "analysis": "0",
                "published": False,
                "attribute_count": "1",
                "Orgc": {"name": "ACME"},
                "Attribute": [
                    {
                        "id": "42",
                        "type": "domain",
                        "category": "Network activity",
                        "value": "evil.example.com",
                        "to_ids": True,
                    }
                ],
            }
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="c4", name="get_misp_event", arguments={"event_id": "7"})
    )
    assert not result.is_error
    assert "evil.example.com" in result.content
    assert "https://misp.example.com/events/view/7" in result.content
    assert recorder.calls[0]["url"].endswith("/events/view/7")


# ---------------------------------------------------------------------------
# Writes - events + attributes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_event_maps_friendly_enums(monkeypatch):
    def router(method, url):
        return {"Event": {"id": "99", "uuid": "new-uuid", "info": "New incident"}}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c5",
            name="create_misp_event",
            arguments={
                "info": "New incident",
                "threat_level": "medium",
                "analysis": "ongoing",
                "tags": "tlp:amber",
            },
        )
    )
    assert not result.is_error
    assert "id 99" in result.content
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/events/add")
    event = call["json"]["Event"]
    assert event["info"] == "New incident"
    assert event["threat_level_id"] == "2"
    assert event["analysis"] == "1"
    assert event["Tag"] == [{"name": "tlp:amber"}]


@pytest.mark.asyncio
async def test_add_attribute_posts_to_event(monkeypatch):
    def router(method, url):
        return {"Attribute": {"id": "43", "type": "sha256", "value": "ab" * 32}}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c6",
            name="add_misp_attribute",
            arguments={
                "event_id": "99",
                "type": "sha256",
                "value": "ab" * 32,
                "comment": "dropper hash",
            },
        )
    )
    assert not result.is_error
    assert "id 43" in result.content
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/attributes/add/99")
    assert call["json"]["type"] == "sha256"
    assert call["json"]["to_ids"] is True
    assert call["json"]["comment"] == "dropper hash"


@pytest.mark.asyncio
async def test_publish_event_uses_action_endpoint(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"message": "published"})
    result = await executor.execute_tool(
        ToolCall(
            id="c7",
            name="publish_misp_event",
            arguments={"event_id": "99", "action": "unpublish"},
        )
    )
    assert not result.is_error
    assert recorder.calls[0]["url"].endswith("/events/unpublish/99")


@pytest.mark.asyncio
async def test_publish_event_rejects_invalid_action(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c8",
            name="publish_misp_event",
            arguments={"event_id": "99", "action": "broadcast"},
        )
    )
    assert "action must be one of" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_delete_attribute_hard_appends_flag(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"message": "deleted"})
    result = await executor.execute_tool(
        ToolCall(
            id="c9",
            name="delete_misp_attribute",
            arguments={"attribute_id": "43", "hard": True},
        )
    )
    assert not result.is_error
    assert "permanent" in result.content
    call = recorder.calls[0]
    # POST is the documented MISP automation form (PyMISP uses it too)
    assert call["method"] == "post"
    assert call["url"].endswith("/attributes/delete/43/1")


@pytest.mark.asyncio
async def test_delete_event_uses_post(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"message": "deleted"})
    result = await executor.execute_tool(
        ToolCall(id="c21", name="delete_misp_event", arguments={"event_id": "99"})
    )
    assert not result.is_error
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/events/delete/99")


# ---------------------------------------------------------------------------
# Tags + sightings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_tag_attaches_by_uuid(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"saved": True})
    result = await executor.execute_tool(
        ToolCall(
            id="c10",
            name="add_misp_tag",
            arguments={"uuid": "ev-uuid", "tag": "tlp:amber", "local": True},
        )
    )
    assert not result.is_error
    assert "tlp:amber" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/tags/attachTagToObject")
    assert call["json"] == {"uuid": "ev-uuid", "tag": "tlp:amber", "local": 1}


@pytest.mark.asyncio
async def test_add_sighting_requires_target(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="c11", name="add_misp_sighting", arguments={})
    )
    assert "provide either value or attribute_id" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_add_sighting_maps_type(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"message": "ok"})
    result = await executor.execute_tool(
        ToolCall(
            id="c12",
            name="add_misp_sighting",
            arguments={
                "value": "203.0.113.7",
                "sighting_type": "false_positive",
                "source": "SIEM",
            },
        )
    )
    assert not result.is_error
    assert "False positive" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/sightings/add")
    assert call["json"]["type"] == "1"
    assert call["json"]["source"] == "SIEM"


@pytest.mark.asyncio
async def test_list_tags_quotes_search_path_segment(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"Tag": []})
    result = await executor.execute_tool(
        ToolCall(
            id="c17",
            name="list_misp_tags",
            arguments={"search": "misp-galaxy:threat-actor/APT29"},
        )
    )
    assert not result.is_error
    # bare terms are wrapped in '%' (the endpoint is a raw SQL LIKE on the
    # tag name) and then percent-encoded so '/' and '%' cannot break the path
    assert recorder.calls[0]["url"].endswith(
        "/tags/search/%25misp-galaxy%3Athreat-actor%2FAPT29%25"
    )


@pytest.mark.asyncio
async def test_list_tags_preserves_explicit_wildcards(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"Tag": []})
    result = await executor.execute_tool(
        ToolCall(id="c22", name="list_misp_tags", arguments={"search": "tlp:%"})
    )
    assert not result.is_error
    # a term that already contains '%' is used as-is (caller controls the LIKE)
    assert recorder.calls[0]["url"].endswith("/tags/search/tlp%3A%25")


@pytest.mark.asyncio
async def test_list_galaxy_clusters_search_posts_searchall_body(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: [])
    result = await executor.execute_tool(
        ToolCall(
            id="c18",
            name="list_misp_galaxy_clusters",
            arguments={"galaxy_id": "3", "search": "APT29"},
        )
    )
    assert not result.is_error
    call = recorder.calls[0]
    # the documented search operation is POST index/{id} + searchall body
    assert call["method"] == "post"
    assert call["url"].endswith("/galaxy_clusters/index/3")
    assert call["json"] == {"searchall": "APT29"}


@pytest.mark.asyncio
async def test_list_galaxy_clusters_without_search_uses_get(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: [])
    result = await executor.execute_tool(
        ToolCall(
            id="c19",
            name="list_misp_galaxy_clusters",
            arguments={"galaxy_id": "3"},
        )
    )
    assert not result.is_error
    call = recorder.calls[0]
    assert call["method"] == "get"
    assert "json" not in call


# ---------------------------------------------------------------------------
# Warninglists + feeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_warninglists_reports_hits_and_misses(monkeypatch):
    def router(method, url):
        return {"8.8.8.8": [{"id": "1", "name": "List of known DNS resolvers"}]}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c13",
            name="check_misp_warninglists",
            arguments={"value": "8.8.8.8, 203.0.113.7"},
        )
    )
    assert not result.is_error
    assert "8.8.8.8: HIT" in result.content
    assert "203.0.113.7: no hits" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/warninglists/checkValue")
    # values are posted as a JSON array
    assert call["json"] == ["8.8.8.8", "203.0.113.7"]


@pytest.mark.asyncio
async def test_manage_feed_fetch_all(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"message": "queued"})
    result = await executor.execute_tool(
        ToolCall(id="c14", name="manage_misp_feed", arguments={"action": "fetch_all"})
    )
    assert not result.is_error
    assert recorder.calls[0]["url"].endswith("/feeds/fetchFromAllFeeds")


@pytest.mark.asyncio
async def test_manage_feed_tolerates_non_dict_response(monkeypatch):
    # MISP occasionally answers with a non-dict JSON payload - the tool must
    # not crash on .get() and should still report success.
    executor, _recorder = _build(monkeypatch, lambda m, u: ["queued"])
    result = await executor.execute_tool(
        ToolCall(id="c20", name="manage_misp_feed", arguments={"action": "fetch_all"})
    )
    assert not result.is_error
    assert "OK" in result.content


@pytest.mark.asyncio
async def test_manage_feed_enable_requires_feed_id(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="c15", name="manage_misp_feed", arguments={"action": "enable"})
    )
    assert "feed_id is required" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_feed_rejects_invalid_action(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="c16", name="manage_misp_feed", arguments={"action": "nuke"})
    )
    assert "action must be one of" in result.content
    assert recorder.calls == []
