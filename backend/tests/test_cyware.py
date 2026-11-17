"""Tests for the Cyware Intel Exchange (CTIX) integration provider.

Covers tool introspection, the HMAC-SHA1 signed-query auth helper + API base
URL normalisation, CQL search shaping, object detail/relations shaping,
context list tools, pagination clamping, intel production (quick-add body
shaping, free-text IOC parse submit/poll/results, tag creation), IOC
curation bulk actions and their guards, knowledge reads (bulletins, rules,
ATT&CK navigator, enrichment), the consolidated admin reads + delete
routing, the non-JSON guard, and the missing-secret call-time guard. Every
API call routes through the shared ``safe_request`` helper, which is
monkeypatched here so no network is touched.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any

import pytest

from app.integrations.providers import cyware as cw
from app.integrations.providers.cyware import (
    build_cyware_auth_params,
    build_tools,
    cyware_api_base,
)
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://demo.cyware.com/ctixapi||access-123||secret-456"


class _StubResponse:
    def __init__(
        self, payload: Any, status_code: int = 200, content: bytes = b"{}"
    ):
        self._payload = payload
        self.status_code = status_code
        self.content = content

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
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
    monkeypatch.setattr(cw, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Introspection + helpers
# ---------------------------------------------------------------------------


def test_build_tools_returns_definitions_with_dummy_token():
    # The catalog introspection dummy is the 2-part URL compound — the secret
    # key is runtime-only, so introspection must still yield the full tool set.
    executor = CallableToolExecutor()
    tools = build_tools(
        token="https://dummy.example.com||__dummy__", executor=executor
    )
    names = {t.name for t in tools}
    assert "search_cyware_threat_data" in names
    assert "get_cyware_object" in names
    assert "get_cyware_object_relations" in names
    assert "list_cyware_tags" in names
    assert "list_cyware_sources" in names
    assert "list_cyware_collections" in names
    assert "list_cyware_subscribers" in names
    assert "list_cyware_saved_searches" in names
    assert "create_cyware_intel" in names
    assert "parse_cyware_iocs" in names
    assert "create_cyware_tags" in names
    assert "tag_cyware_objects" in names
    assert "update_cyware_threat_data" in names
    assert "list_cyware_threat_bulletins" in names
    assert "get_cyware_threat_bulletin" in names
    assert "list_cyware_notes" in names
    assert "list_cyware_tasks" in names
    assert "list_cyware_rules" in names
    assert "list_cyware_allowed_indicators" in names
    assert "list_cyware_watchlist" in names
    assert "get_cyware_enrichment" in names
    assert "list_cyware_attack_techniques" in names
    assert "manage_cyware_admin" in names
    assert "delete_cyware_object" in names
    assert len(tools) == 24
    # Priorities follow the repo convention (read=0, write=1, destructive=2).
    priorities = {t.name: t.priority for t in tools}
    assert set(priorities.values()) <= {0, 1, 2}
    for name, priority in priorities.items():
        if name.startswith(("list_", "get_", "search_", "manage_")):
            assert priority == 0, f"read tool {name} must be priority 0"
        elif name.startswith("delete_"):
            assert priority == 2, f"destructive tool {name} must be priority 2"
        else:
            assert priority == 1, f"write tool {name} must be priority 1"
    assert all(t.source == "integration:cyware" for t in tools)


def test_build_tools_empty_without_base_url_or_access_id():
    executor = CallableToolExecutor()
    assert build_tools(token="", executor=executor) == []
    assert build_tools(token="https://demo.cyware.com/ctixapi||", executor=executor) == []
    assert build_tools(token="||access-123||secret", executor=executor) == []


def test_api_base_normalisation():
    assert (
        cyware_api_base("https://demo.cyware.com/ctixapi")
        == "https://demo.cyware.com/ctixapi"
    )
    assert (
        cyware_api_base("  https://demo.cyware.com/ctixapi/  ")
        == "https://demo.cyware.com/ctixapi"
    )
    assert cyware_api_base("") == ""


def test_hmac_signature_matches_reference_implementation():
    params = build_cyware_auth_params("access-123", "secret-456", now=1700000000)
    expires = 1700000000 + cw._SIGNATURE_TTL_SECONDS
    message = f"access-123\n{expires}"
    expected = base64.b64encode(
        hmac.new(b"secret-456", message.encode(), hashlib.sha1).digest()
    ).decode()
    assert params == {
        "AccessID": "access-123",
        "Signature": expected,
        "Expires": str(expires),
    }
    # Credentials are stripped so pasted whitespace never corrupts the HMAC.
    padded = build_cyware_auth_params(
        "  access-123  ", "\tsecret-456\n", now=1700000000
    )
    assert padded == params


# ---------------------------------------------------------------------------
# Threat data search + object detail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_threat_data_sends_cql_and_signed_params(monkeypatch):
    def router(method, url):
        return {
            "next": None,
            "previous": None,
            "results": [
                {
                    "id": "49d5e95e-3889-42de-8280-1ebf5c7cb95a",
                    "name": "evil.com",
                    "type": "indicator",
                    "ioc_type": "domain-name",
                    "confidence_score": 85,
                    "tlp": "AMBER",
                    "ctix_created": 1700000000,
                    "ctix_modified": 1700003600,
                    "is_deprecated": False,
                    "is_false_positive": False,
                }
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="search_cyware_threat_data",
            arguments={"query": 'type = indicator AND value = "evil.com"'},
        )
    )
    assert not result.is_error
    assert "evil.com" in result.content
    assert "Risk: 85" in result.content
    assert "49d5e95e-3889-42de-8280-1ebf5c7cb95a" in result.content
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/ingestion/threat-data/list/")
    assert call["json"] == {"query": 'type = indicator AND value = "evil.com"'}
    # Signed auth params ride on every request alongside pagination.
    assert call["params"]["AccessID"] == "access-123"
    assert call["params"]["Expires"].isdigit()
    assert call["params"]["Signature"]
    assert call["params"]["page"] == 1
    assert call["params"]["page_size"] == 10


@pytest.mark.asyncio
async def test_get_object_detail_includes_sources_and_tags(monkeypatch):
    def router(method, url):
        return {
            "id": "abc-123",
            "name": "evil.com",
            "type": "indicator",
            "ioc_type": "domain-name",
            "confidence_score": 90,
            "analyst_score": 75,
            "tlp": "RED",
            "ctix_created": 1700000000,
            "sources": [{"name": "AlienVault OTX"}],
            "published_collections": [{"name": "Curated IOCs"}],
            "tags": [{"name": "phishing"}, {"name": "apt"}],
            "is_whitelisted": False,
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c2",
            name="get_cyware_object",
            arguments={"object_type": "indicator", "object_id": "abc-123"},
        )
    )
    assert not result.is_error
    assert "AlienVault OTX" in result.content
    assert "Curated IOCs" in result.content
    assert "phishing" in result.content
    assert "Analyst score: 75" in result.content
    assert recorder.calls[0]["url"].endswith(
        "/ingestion/threat-data/indicator/abc-123/basic/"
    )


@pytest.mark.asyncio
async def test_get_object_rejects_unknown_type(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c3",
            name="get_cyware_object",
            arguments={"object_type": "widget", "object_id": "abc"},
        )
    )
    assert "unknown object_type" in result.content.lower()
    assert recorder.calls == []
    # Custom x-* object types pass the guard.
    ok = await executor.execute_tool(
        ToolCall(
            id="c4",
            name="get_cyware_object",
            arguments={"object_type": "x-custom", "object_id": "abc"},
        )
    )
    assert not ok.is_error
    assert recorder.calls[-1]["url"].endswith(
        "/ingestion/threat-data/x-custom/abc/basic/"
    )


@pytest.mark.asyncio
async def test_relations_formats_related_objects(monkeypatch):
    def router(method, url):
        return {
            "results": [
                {
                    "relationship_type": "indicates",
                    "object": {
                        "id": "mal-1",
                        "type": "malware",
                        "name": "Emotet",
                    },
                }
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c5",
            name="get_cyware_object_relations",
            arguments={"object_type": "indicator", "object_id": "abc-123"},
        )
    )
    assert not result.is_error
    assert "--indicates-->" in result.content
    assert "Emotet" in result.content
    assert "mal-1" in result.content
    assert recorder.calls[0]["url"].endswith(
        "/ingestion/threat-data/indicator/abc-123/relations/"
    )


# ---------------------------------------------------------------------------
# Context list tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tags_filters_by_type(monkeypatch):
    def router(method, url):
        return {
            "results": [
                {
                    "id": "tag-1",
                    "name": "phishing",
                    "tag_type": "user",
                    "created": 1700000000,
                    "created_by": {"first_name": "Ana", "last_name": "Lyst"},
                }
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="c6", name="list_cyware_tags", arguments={"tag_type": "user"})
    )
    assert not result.is_error
    assert "phishing" in result.content
    assert "tag-1" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/ingestion/tags/")
    assert call["params"]["tag_type"] == "user"


@pytest.mark.asyncio
async def test_list_collections_and_subscribers(monkeypatch):
    def router(method, url):
        if "collection" in url:
            return {
                "results": [
                    {"id": "col-1", "name": "Curated IOCs", "is_active": True}
                ]
            }
        return {
            "results": [
                {
                    "id": "sub-1",
                    "name": "Acme CERT",
                    "active": True,
                    "organization_name": "Acme",
                }
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    cols = await executor.execute_tool(
        ToolCall(
            id="c7",
            name="list_cyware_collections",
            arguments={"q": "curated", "active_only": True},
        )
    )
    assert "Curated IOCs" in cols.content
    call = recorder.calls[0]
    assert call["url"].endswith("/publishing/collection/")
    assert call["params"]["q"] == "curated"
    assert call["params"]["is_active"] == "true"
    subs = await executor.execute_tool(
        ToolCall(id="c8", name="list_cyware_subscribers", arguments={})
    )
    assert "Acme CERT" in subs.content
    assert recorder.calls[-1]["url"].endswith("/publishing/subscriber/")


@pytest.mark.asyncio
async def test_list_saved_searches_surfaces_query(monkeypatch):
    def router(method, url):
        return {
            "results": [
                {
                    "id": "ss-1",
                    "name": "High risk IPs",
                    "meta_data": {
                        "query": "type = indicator AND confidence_score >= 80"
                    },
                }
            ]
        }

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="c9", name="list_cyware_saved_searches", arguments={})
    )
    assert "High risk IPs" in result.content
    assert "confidence_score >= 80" in result.content


# ---------------------------------------------------------------------------
# Pagination clamping
# ---------------------------------------------------------------------------


def test_pagination_note_uses_clamped_values():
    # A full page hints at the next page; a partial page does not.
    assert "page=2" in cw._pagination_note({}, 25, 1, 25)
    assert cw._pagination_note({}, 10, 1, 25) == ""
    # The DRF ``next`` marker always wins.
    assert "page=2" in cw._pagination_note({"next": "..."}, 10, 1, 25)
    # Nested data envelopes are unwrapped.
    assert "page=3" in cw._pagination_note({"data": {"next": "..."}}, 5, 2, 25)


@pytest.mark.asyncio
async def test_page_params_clamp_out_of_range_values(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"results": []})
    await executor.execute_tool(
        ToolCall(
            id="pg1",
            name="list_cyware_tags",
            arguments={"page": -3, "page_size": 99999},
        )
    )
    call = recorder.calls[-1]
    assert call["params"]["page"] == 1
    assert call["params"]["page_size"] == 100


# ---------------------------------------------------------------------------
# Intel production (writes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_intel_builds_quick_add_body(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        lambda m, u: {"details": "queued", "task_id": "task-1"},
    )
    result = await executor.execute_tool(
        ToolCall(
            id="w1",
            name="create_cyware_intel",
            arguments={
                "title": "Emotet campaign IOCs",
                "description": "IOCs from the latest Emotet wave",
                "indicators": '{"ipv4-addr": "1.2.3.4,5.6.7.8", "domain": "evil.com"}',
                "sdos": '{"malware": "Emotet"}',
                "tlp": "amber",
                "confidence": 80,
                "tag_ids": "tag-1,tag-2",
            },
        )
    )
    assert not result.is_error
    assert "task-1" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"].endswith("/conversion/quick-intel/create-stix/")
    body = call["json"]
    assert body["title"] == "Emotet campaign IOCs"
    assert body["indicators"] == {
        "ipv4-addr": "1.2.3.4,5.6.7.8",
        "domain": "evil.com",
    }
    assert body["sdos"] == {"malware": "Emotet"}
    assert body["create_intel_feed"] is True
    assert body["metadata"]["tlp"] == "AMBER"
    assert body["metadata"]["confidence"] == 80
    assert body["metadata"]["labels"] == ["tag-1", "tag-2"]
    assert body["metadata"]["is_apply_all"] is True


@pytest.mark.asyncio
async def test_create_intel_normalises_json_array_values(monkeypatch):
    # LLMs naturally pass JSON arrays instead of the documented
    # comma-separated strings — both must produce the same quick-add body.
    executor, recorder = _build(monkeypatch, lambda m, u: {"task_id": "task-2"})
    result = await executor.execute_tool(
        ToolCall(
            id="w1b",
            name="create_cyware_intel",
            arguments={
                "indicators": '{"ipv4-addr": ["1.2.3.4", "5.6.7.8"]}',
                "sdos": '{"malware": ["Emotet", "TrickBot"]}',
            },
        )
    )
    assert not result.is_error
    body = recorder.calls[-1]["json"]
    assert body["indicators"] == {"ipv4-addr": "1.2.3.4,5.6.7.8"}
    assert body["sdos"] == {"malware": "Emotet,TrickBot"}


@pytest.mark.asyncio
async def test_create_intel_guards_before_any_request(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    empty = await executor.execute_tool(
        ToolCall(id="w2", name="create_cyware_intel", arguments={})
    )
    assert "at least one of" in empty.content
    bad_key = await executor.execute_tool(
        ToolCall(
            id="w3",
            name="create_cyware_intel",
            arguments={"indicators": '{"ipv999": "1.2.3.4"}'},
        )
    )
    assert "unknown indicator key" in bad_key.content
    bad_json = await executor.execute_tool(
        ToolCall(
            id="w4",
            name="create_cyware_intel",
            arguments={"indicators": "not json"},
        )
    )
    assert "valid JSON object" in bad_json.content
    bad_tlp = await executor.execute_tool(
        ToolCall(
            id="w5",
            name="create_cyware_intel",
            arguments={"indicators": '{"domain": "a.com"}', "tlp": "PURPLE"},
        )
    )
    assert "tlp must be one of" in bad_tlp.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_parse_iocs_submits_polls_and_fetches_results(monkeypatch):
    def router(method, url):
        if url.endswith("/conversion/quick-intel/free-text/"):
            return {"task_id": "task-9"}
        if url.endswith("/conversion/tasks/task-9/"):
            return {"num_status": 3, "status": "done"}
        return {
            "ips": [{"value": "1.2.3.4", "is_whitelisted": False}],
            "domains": [{"value": "evil.com", "is_whitelisted": True}],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="w6",
            name="parse_cyware_iocs",
            arguments={"text": "1.2.3.4 contacted evil.com"},
        )
    )
    assert not result.is_error
    assert "1.2.3.4" in result.content
    assert "evil.com (on allowed list)" in result.content
    urls = [c["url"] for c in recorder.calls]
    assert urls[0].endswith("/conversion/quick-intel/free-text/")
    assert urls[1].endswith("/conversion/tasks/task-9/")
    assert urls[-1].endswith("/conversion/tasks/task-9/parse-iocs/")
    assert recorder.calls[0]["json"] == {
        "data": "1.2.3.4 contacted evil.com",
        "url": False,
    }


@pytest.mark.asyncio
async def test_parse_iocs_reports_failed_task(monkeypatch):
    def router(method, url):
        if url.endswith("/conversion/quick-intel/free-text/"):
            return {"task_id": "task-9"}
        return {"num_status": 2, "status": "failed"}

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="w7", name="parse_cyware_iocs", arguments={"text": "abc"})
    )
    assert "failed" in result.content


@pytest.mark.asyncio
async def test_create_tags_sends_bulk_action_body(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"result": "created"})
    result = await executor.execute_tool(
        ToolCall(
            id="w8",
            name="create_cyware_tags",
            arguments={"names": "phishing, apt29", "tag_type": "user"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["url"].endswith("/ingestion/tags/bulk-actions/")
    assert call["json"] == {
        "name": "phishing,apt29",
        "tag_type": "user",
        "action": "add",
    }
    too_many = await executor.execute_tool(
        ToolCall(
            id="w9",
            name="create_cyware_tags",
            arguments={"names": ",".join(f"t{i}" for i in range(16))},
        )
    )
    assert "maximum of 15" in too_many.content


# ---------------------------------------------------------------------------
# IOC curation bulk actions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tag_objects_uses_action_specific_data_keys(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"message": "Action Successfully Executed"}
    )
    add = await executor.execute_tool(
        ToolCall(
            id="b1",
            name="tag_cyware_objects",
            arguments={
                "action": "add",
                "object_type": "indicator",
                "object_ids": "obj-1, obj-2",
                "tag_ids": "tag-1",
            },
        )
    )
    assert not add.is_error
    call = recorder.calls[-1]
    assert call["url"].endswith("/ingestion/threat-data/bulk-action/add_tag/")
    assert call["json"]["data"] == {"tag_id": ["tag-1"]}
    assert call["json"]["object_ids"] == ["obj-1", "obj-2"]
    remove = await executor.execute_tool(
        ToolCall(
            id="b2",
            name="tag_cyware_objects",
            arguments={
                "action": "remove",
                "object_type": "indicator",
                "object_ids": "obj-1",
                "tag_ids": "tag-1,tag-2",
            },
        )
    )
    assert not remove.is_error
    call = recorder.calls[-1]
    assert call["url"].endswith("/ingestion/threat-data/bulk-action/remove_tag/")
    assert call["json"]["data"] == {"tag_ids": ["tag-1", "tag-2"]}


@pytest.mark.asyncio
async def test_update_threat_data_simple_and_data_actions(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"message": "Action Successfully Executed"}
    )
    fp = await executor.execute_tool(
        ToolCall(
            id="b3",
            name="update_cyware_threat_data",
            arguments={
                "action": "false_positive",
                "object_type": "indicator",
                "object_ids": "obj-1",
            },
        )
    )
    assert not fp.is_error
    call = recorder.calls[-1]
    assert call["url"].endswith("/ingestion/threat-data/bulk-action/false_positive/")
    assert "data" not in call["json"]
    score = await executor.execute_tool(
        ToolCall(
            id="b4",
            name="update_cyware_threat_data",
            arguments={
                "action": "analyst_score",
                "object_type": "indicator",
                "object_ids": "obj-1",
                "value": "85",
            },
        )
    )
    assert not score.is_error
    assert recorder.calls[-1]["json"]["data"] == {"analyst_score": 85}
    tlp = await executor.execute_tool(
        ToolCall(
            id="b5",
            name="update_cyware_threat_data",
            arguments={
                "action": "analyst_tlp",
                "object_type": "indicator",
                "object_ids": "obj-1",
                "value": "amber",
            },
        )
    )
    assert not tlp.is_error
    assert recorder.calls[-1]["json"]["data"] == {"analyst_tlp": "AMBER"}
    rule = await executor.execute_tool(
        ToolCall(
            id="b6",
            name="update_cyware_threat_data",
            arguments={
                "action": "run_rule",
                "object_type": "indicator",
                "object_ids": "obj-1",
                "value": "rule-1",
            },
        )
    )
    assert not rule.is_error
    assert recorder.calls[-1]["json"]["data"] == {"id": "rule-1"}
    watch = await executor.execute_tool(
        ToolCall(
            id="b7",
            name="update_cyware_threat_data",
            arguments={
                "action": "watchlist",
                "object_type": "indicator",
                "object_ids": "obj-1",
                "value": "vip-keywords",
            },
        )
    )
    assert not watch.is_error
    assert recorder.calls[-1]["json"]["data"] == {"name": ["vip-keywords"]}


@pytest.mark.asyncio
async def test_update_threat_data_guards(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    bad_action = await executor.execute_tool(
        ToolCall(
            id="b8",
            name="update_cyware_threat_data",
            arguments={
                "action": "explode",
                "object_type": "indicator",
                "object_ids": "obj-1",
            },
        )
    )
    assert "invalid action" in bad_action.content.lower()
    missing_value = await executor.execute_tool(
        ToolCall(
            id="b9",
            name="update_cyware_threat_data",
            arguments={
                "action": "analyst_score",
                "object_type": "indicator",
                "object_ids": "obj-1",
            },
        )
    )
    assert "requires value" in missing_value.content
    bad_score = await executor.execute_tool(
        ToolCall(
            id="b10",
            name="update_cyware_threat_data",
            arguments={
                "action": "analyst_score",
                "object_type": "indicator",
                "object_ids": "obj-1",
                "value": "high",
            },
        )
    )
    assert "integer score" in bad_score.content
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Knowledge reads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulletins_list_and_detail(monkeypatch):
    def router(method, url):
        if url.endswith("/conversion/threat-bulletin/"):
            return {
                "results": [
                    {
                        "id": "tb-1",
                        "metadata": {"title": "APT29 weekly recap"},
                        "published": True,
                        "tlp": "GREEN",
                        "created": 1700000000,
                        "created_by": {"first_name": "Ana", "last_name": "Lyst"},
                    }
                ]
            }
        return {
            "id": "tb-1",
            "metadata": {"title": "APT29 weekly recap"},
            "published": True,
            "tlp": "GREEN",
            "created": 1700000000,
            "description": "Cozy Bear activity summary ...",
            "attachments": [{"id": "a1"}],
        }

    executor, _ = _build(monkeypatch, router)
    listed = await executor.execute_tool(
        ToolCall(id="k1", name="list_cyware_threat_bulletins", arguments={})
    )
    assert "APT29 weekly recap" in listed.content
    assert "published" in listed.content
    detail = await executor.execute_tool(
        ToolCall(
            id="k2",
            name="get_cyware_threat_bulletin",
            arguments={"bulletin_id": "tb-1"},
        )
    )
    assert "Cozy Bear activity summary" in detail.content
    assert "Attachments: 1" in detail.content


@pytest.mark.asyncio
async def test_rules_list_and_detail(monkeypatch):
    def router(method, url):
        if url.endswith("/ingestion/rules/rule-1/"):
            return {
                "id": "rule-1",
                "name": "Push high-risk IOCs to SIEM",
                "is_active": True,
                "description": "Sends IOCs with score >= 80 to Splunk",
                "actions": [{"name": "splunk-push"}],
            }
        return {
            "results": [
                {"id": "rule-1", "name": "Push high-risk IOCs to SIEM", "is_active": True}
            ]
        }

    executor, _ = _build(monkeypatch, router)
    listed = await executor.execute_tool(
        ToolCall(id="k3", name="list_cyware_rules", arguments={})
    )
    assert "Push high-risk IOCs to SIEM" in listed.content
    detail = await executor.execute_tool(
        ToolCall(id="k4", name="list_cyware_rules", arguments={"rule_id": "rule-1"})
    )
    assert "splunk-push" in detail.content


@pytest.mark.asyncio
async def test_attack_techniques_and_tactics_switch_endpoints(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        lambda m, u: {
            "results": [
                {"id": "T1566", "name": "Phishing", "tactics": [{"name": "Initial Access"}]}
            ]
        },
    )
    techniques = await executor.execute_tool(
        ToolCall(
            id="k5",
            name="list_cyware_attack_techniques",
            arguments={"q": "phishing"},
        )
    )
    assert "T1566" in techniques.content
    assert recorder.calls[-1]["url"].endswith("/ingestion/attack-navigator/techniques/")
    await executor.execute_tool(
        ToolCall(
            id="k6",
            name="list_cyware_attack_techniques",
            arguments={"tactics": True},
        )
    )
    assert recorder.calls[-1]["url"].endswith("/ingestion/attack-navigator/tactics/")


@pytest.mark.asyncio
async def test_enrichment_requires_slug_and_formats_payload(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"verdict": "malicious", "positives": 42}
    )
    missing = await executor.execute_tool(
        ToolCall(
            id="k7",
            name="get_cyware_enrichment",
            arguments={"object_type": "indicator", "object_id": "obj-1", "app_slug": ""},
        )
    )
    assert "app_slug" in missing.content
    assert recorder.calls == []
    ok = await executor.execute_tool(
        ToolCall(
            id="k8",
            name="get_cyware_enrichment",
            arguments={
                "object_type": "indicator",
                "object_id": "obj-1",
                "app_slug": "virustotal",
            },
        )
    )
    assert "malicious" in ok.content
    assert recorder.calls[-1]["params"]["app_slug"] == "virustotal"


# ---------------------------------------------------------------------------
# Long tail (admin + delete)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_admin_paginated_and_singleton_areas(monkeypatch):
    def router(method, url):
        if url.endswith("/rest-auth/users/"):
            return {
                "results": [
                    {"id": "u1", "email": "ana@corp.com", "is_active": True}
                ]
            }
        return {"result": "pong"}

    executor, recorder = _build(monkeypatch, router)
    users = await executor.execute_tool(
        ToolCall(id="a1", name="manage_cyware_admin", arguments={"area": "users"})
    )
    assert "ana@corp.com" in users.content
    assert recorder.calls[-1]["params"]["page"] == 1
    beat = await executor.execute_tool(
        ToolCall(id="a2", name="manage_cyware_admin", arguments={"area": "heartbeat"})
    )
    assert "pong" in beat.content
    bad = await executor.execute_tool(
        ToolCall(id="a3", name="manage_cyware_admin", arguments={"area": "nope"})
    )
    assert "invalid area" in bad.content.lower()


@pytest.mark.asyncio
async def test_delete_object_routes_by_kind(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"details": "deleted"})
    result = await executor.execute_tool(
        ToolCall(
            id="d1",
            name="delete_cyware_object",
            arguments={"kind": "saved_search", "object_id": "ss-1"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "delete"
    assert call["url"].endswith("/ingestion/saved-searches/ss-1/")
    bad = await executor.execute_tool(
        ToolCall(
            id="d2",
            name="delete_cyware_object",
            arguments={"kind": "tenant", "object_id": "x"},
        )
    )
    assert "invalid kind" in bad.content.lower()
    assert len(recorder.calls) == 1


# ---------------------------------------------------------------------------
# validate_connection + guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_connection_routes_through_safe_request(monkeypatch):
    recorder = _Recorder(lambda m, u: {"result": "pong"})
    monkeypatch.setattr(cw, "safe_request", recorder)
    ok, message = await cw.validate_connection(
        "https://demo.cyware.com/ctixapi", "access-123", "secret-456"
    )
    assert ok
    assert "connected" in message
    call = recorder.calls[0]
    assert call["url"].endswith("/ping/")
    assert call["params"]["AccessID"] == "access-123"
    assert call["params"]["Signature"]
    # expected_statuses lets the probe tell an auth failure from a network error
    assert call["expected_statuses"] == {401, 403}


@pytest.mark.asyncio
async def test_validate_connection_rejects_auth_failure(monkeypatch):
    async def fake(_client, method, url, _provider, action, **kwargs):
        return _StubResponse({"detail": "Invalid signature"}, status_code=401), None

    monkeypatch.setattr(cw, "safe_request", fake)
    ok, message = await cw.validate_connection(
        "https://demo.cyware.com/ctixapi", "access-123", "bad-secret"
    )
    assert not ok
    assert "Access ID and Secret Key" in message


@pytest.mark.asyncio
async def test_validate_connection_rejects_non_json_response(monkeypatch):
    recorder = _Recorder(lambda m, u: ValueError("not json"))
    monkeypatch.setattr(cw, "safe_request", recorder)
    ok, message = await cw.validate_connection(
        "https://demo.cyware.com/ctixapi", "access-123", "secret-456"
    )
    assert not ok
    assert "non-JSON" in message


@pytest.mark.asyncio
async def test_validate_connection_missing_credentials():
    ok, message = await cw.validate_connection("", "access-123", "secret")
    assert not ok and "base URL" in message
    ok, message = await cw.validate_connection(
        "https://demo.cyware.com/ctixapi", "", "secret"
    )
    assert not ok and "Access ID" in message


@pytest.mark.asyncio
async def test_missing_secret_key_surfaces_actionable_error(monkeypatch):
    # The 2-part dummy builds the tools, but any call without a Secret Key
    # must return a clear configuration error instead of a broken signature.
    recorder = _Recorder(lambda m, u: {})
    monkeypatch.setattr(cw, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(
        token="https://demo.cyware.com/ctixapi||access-123||", executor=executor
    )
    result = await executor.execute_tool(
        ToolCall(id="c10", name="search_cyware_threat_data", arguments={})
    )
    assert "Secret Key is not configured" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_api_non_json_body_is_error_but_empty_body_is_success(monkeypatch):
    # A non-JSON 2xx with a body is not a CTIX API response; an empty 2xx
    # body is treated as a benign success payload.
    responses = iter(
        [
            _StubResponse(ValueError("not json"), content=b"<html>portal</html>"),
            _StubResponse(ValueError("not json"), content=b""),
        ]
    )

    async def fake(_client, method, url, _provider, action, **kwargs):
        return next(responses), None

    monkeypatch.setattr(cw, "safe_request", fake)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    bad = await executor.execute_tool(
        ToolCall(id="nj1", name="list_cyware_tags", arguments={})
    )
    assert "non-JSON" in bad.content
    ok = await executor.execute_tool(
        ToolCall(id="nj2", name="list_cyware_tags", arguments={})
    )
    assert not ok.is_error
    assert "No tags found" in ok.content


def test_object_type_schema_allows_custom_x_types():
    # The runtime accepts custom x-* CTIX object types, so the tool schemas
    # must NOT pin object_type to a fixed enum (schema-validating clients
    # would otherwise reject valid x-* calls).
    executor = CallableToolExecutor()
    tools = build_tools(token=_TOKEN, executor=executor)
    for name in (
        "get_cyware_object",
        "get_cyware_object_relations",
        "tag_cyware_objects",
        "update_cyware_threat_data",
        "get_cyware_enrichment",
    ):
        tool = next(t for t in tools if t.name == name)
        prop = tool.input_schema["properties"]["object_type"]
        assert "enum" not in prop, f"{name} object_type must not be an enum"
        assert "x-*" in prop["description"]
