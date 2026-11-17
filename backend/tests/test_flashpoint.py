"""Tests for the Flashpoint integration provider.

Covers tool introspection, the Bearer header + base-URL helpers, report
search (param forwarding + pagination hint), report detail shaping, indicator
hunting (filter forwarding + score-tier guard), single-IOC reputation lookup
(known + unknown), indicator detail, event listing, communities search
(POST body shape), CCM credential search (Lucene query composition), alert
listing (source/status mapping + cursor extraction from pagination.next),
vulnerability search (query params + JSON filter body + validation guards),
CVE lookup, affected-components listing, the vendors/products catalog, and
percent-encoding of user-supplied ids used as URL path segments. Every API
call routes through the shared ``safe_request`` helper, which is
monkeypatched here so no network is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import flashpoint as fp
from app.integrations.providers.flashpoint import (
    build_flashpoint_headers,
    build_tools,
    flashpoint_api_base,
)
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://api.flashpoint.io||fp-api-token-123"
_EXPECTED_TOOL_COUNT = 19


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
    monkeypatch.setattr(fp, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Introspection + helpers
# ---------------------------------------------------------------------------


def test_catalog_dummy_token_yields_all_tools():
    """Catalog introspection must see all tools, not an empty list.

    ``build_dummy_token("flashpoint")`` must return a compound dummy: the
    plain ``__dummy__`` parses to (base_url="__dummy__", api_token="") and
    the empty-token guard in ``build_tools`` would hide every tool from the
    catalog/UI (see the _URL_DUMMY_PROVIDERS registry).
    """
    from app.api.v1.integration_catalog import build_dummy_token

    tools = build_tools(
        token=build_dummy_token("flashpoint"), executor=CallableToolExecutor()
    )
    assert len(tools) == _EXPECTED_TOOL_COUNT


def test_build_tools_returns_definitions_with_dummy_token():
    executor = CallableToolExecutor()
    tools = build_tools(token="https://dummy.example.com||__dummy__", executor=executor)
    names = {t.name for t in tools}
    assert "get_flashpoint_status" in names
    assert "search_flashpoint_reports" in names
    assert "get_flashpoint_report" in names
    assert "list_flashpoint_related_reports" in names
    assert "search_flashpoint_indicators" in names
    assert "lookup_flashpoint_ioc" in names
    assert "get_flashpoint_indicator" in names
    assert "list_flashpoint_events" in names
    assert "get_flashpoint_event" in names
    assert "search_flashpoint_communities" in names
    assert "get_flashpoint_community_post" in names
    assert "search_flashpoint_credentials" in names
    assert "list_flashpoint_alerts" in names
    assert "search_flashpoint_vulnerabilities" in names
    assert "lookup_flashpoint_cve" in names
    assert "get_flashpoint_vulnerability" in names
    assert "list_flashpoint_affected_components" in names
    assert "search_flashpoint_vuln_catalog" in names
    assert len(tools) == _EXPECTED_TOOL_COUNT


def test_build_tools_empty_without_api_token():
    executor = CallableToolExecutor()
    # base URL only, no API token -> no tools
    assert build_tools(token="https://api.flashpoint.io||", executor=executor) == []
    # whitespace-only token is treated as missing -> no tools
    assert build_tools(token="https://api.flashpoint.io||   ", executor=executor) == []


def test_headers_and_base_url_helpers():
    headers = build_flashpoint_headers("tok-1")
    assert headers["Authorization"] == "Bearer tok-1"
    assert headers["Accept"] == "application/json"
    assert headers["x-fp-integrationplatform"] == "XTM One"
    # Pasted whitespace never produces a malformed Authorization header.
    assert build_flashpoint_headers("  tok-1 \n")["Authorization"] == "Bearer tok-1"
    # default + trailing-slash + whitespace normalisation
    assert flashpoint_api_base("") == "https://api.flashpoint.io"
    # whitespace-only values fall back to the default host (not "")
    assert flashpoint_api_base("   ") == "https://api.flashpoint.io"
    assert flashpoint_api_base("https://proxy.corp.example/fp/") == "https://proxy.corp.example/fp"
    assert flashpoint_api_base("  https://proxy.corp.example/fp  ") == "https://proxy.corp.example/fp"


def test_build_tools_strips_whitespace():
    tools = build_tools(
        token="  https://api.flashpoint.io  ||  fp-tok  ",
        executor=CallableToolExecutor(),
    )
    assert len(tools) == _EXPECTED_TOOL_COUNT


def test_all_tools_are_reads_and_status_sorts_last():
    """The Ignite API is read-only: every tool must be a read (priority 0)
    except the connection-status tool (priority 2)."""
    tools = build_tools(token=_TOKEN, executor=CallableToolExecutor())
    for t in tools:
        if t.name == "get_flashpoint_status":
            assert t.priority == 2
        else:
            assert t.priority == 0, t.name


# ---------------------------------------------------------------------------
# Finished intelligence - reports
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_reports_forwards_params_and_paginates(monkeypatch):
    def router(method, url):
        return {
            "total": 12,
            "data": [
                {
                    "id": "rep-1",
                    "title": "AI Threat Landscape",
                    "summary": "Evolving AI threats.",
                    "posted_at": "2026-06-01T00:00:00Z",
                    "updated_at": "2026-06-02T00:00:00Z",
                    "tags": ["Cyber Threats", "Technology"],
                    "platform_url": "https://app.flashpoint.io/cti/intelligence/report/rep-1",
                }
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="search_flashpoint_reports",
            arguments={"query": "ChatGPT", "since": "-30d", "limit": 1},
        )
    )
    assert not result.is_error
    assert "AI Threat Landscape" in result.content
    assert "rep-1" in result.content
    assert "skip=1" in result.content  # pagination hint (12 total, 1 shown)
    call = recorder.calls[0]
    assert call["url"].endswith("/finished-intelligence/v1/reports")
    assert call["params"]["query"] == "ChatGPT"
    assert call["params"]["since"] == "-30d"
    assert call["params"]["limit"] == 1


@pytest.mark.asyncio
async def test_get_report_detail(monkeypatch):
    def router(method, url):
        return {
            "id": "rep-2",
            "title": "Key Developments",
            "summary": "A weekly update.",
            "body": "<p>Full body</p>",
            "tags": ["Energy"],
            "posted_at": "2026-06-01T00:00:00Z",
            "updated_at": "2026-06-02T00:00:00Z",
            "platform_url": "https://app.flashpoint.io/cti/intelligence/report/rep-2",
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="c2", name="get_flashpoint_report", arguments={"report_id": "rep-2"})
    )
    assert not result.is_error
    assert "Key Developments" in result.content
    assert "A weekly update." in result.content
    assert "Energy" in result.content
    assert recorder.calls[0]["url"].endswith("/finished-intelligence/v1/reports/rep-2")


@pytest.mark.asyncio
async def test_related_reports(monkeypatch):
    def router(method, url):
        return {"total": 1, "data": [{"id": "rep-3", "title": "Related One"}]}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c3",
            name="list_flashpoint_related_reports",
            arguments={"report_id": "rep-2"},
        )
    )
    assert not result.is_error
    assert "Related One" in result.content
    assert recorder.calls[0]["url"].endswith(
        "/finished-intelligence/v1/reports/rep-2/related"
    )


# ---------------------------------------------------------------------------
# Technical intelligence - indicators
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_indicators_forwards_filters(monkeypatch):
    def router(method, url):
        return {
            "items": [
                {
                    "id": "ind-1",
                    "type": "domain",
                    "value": "evil.example.com",
                    "score": {"value": "malicious"},
                    "last_seen_at": "2026-07-01T00:00:00Z",
                    "modified_at": "2026-07-02T00:00:00Z",
                    "total_sightings": 11,
                    "latest_sighting": {"tags": ["malware:xworm"]},
                    "platform_urls": {"ignite": "https://app.flashpoint.io/cti/malware/iocs/ind-1"},
                }
            ],
            "total": {"value": 1},
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c4",
            name="search_flashpoint_indicators",
            arguments={
                "ioc_types": "domain, url",
                "malware": "xworm",
                "mitre_attack_ids": "T1071",
                "min_score": "suspicious",
                "last_seen_after": "-7d",
            },
        )
    )
    assert not result.is_error
    assert "evil.example.com" in result.content
    assert "malicious" in result.content
    params = recorder.calls[0]["params"]
    assert params["ioc_types"] == "domain,url"
    assert params["malware"] == "xworm"
    assert params["mitre_attack_ids"] == "T1071"
    assert params["min_score"] == "suspicious"
    assert params["last_seen_after"] == "-7d"
    assert recorder.calls[0]["url"].endswith("/technical-intelligence/v2/indicators")


@pytest.mark.asyncio
async def test_search_indicators_rejects_bad_score_tier(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c5",
            name="search_flashpoint_indicators",
            arguments={"min_score": "catastrophic"},
        )
    )
    assert "min_score must be one of" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_lookup_ioc_known_and_unknown(monkeypatch):
    def router(method, url):
        return {
            "items": [
                {
                    "id": "ind-2",
                    "type": "ipv4",
                    "value": "1.2.3.4",
                    "score": {"value": "suspicious"},
                    "malware_description": "Used by Redline stealer C2.",
                    "relationships": {
                        "iocs": [{"id": "ind-3", "type": "domain", "value": "c2.example.com"}]
                    },
                }
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="c6", name="lookup_flashpoint_ioc", arguments={"value": "1.2.3.4"})
    )
    assert not result.is_error
    assert "suspicious" in result.content
    assert "Redline" in result.content
    assert "c2.example.com" in result.content
    assert recorder.calls[0]["params"]["ioc_value"] == "1.2.3.4"
    assert recorder.calls[0]["params"]["embed"] == "all"

    # unknown IOC
    executor2, _ = _build(monkeypatch, lambda m, u: {"items": []})
    result2 = await executor2.execute_tool(
        ToolCall(id="c7", name="lookup_flashpoint_ioc", arguments={"value": "8.8.8.8"})
    )
    assert "unknown" in result2.content.lower()


@pytest.mark.asyncio
async def test_get_indicator_detail(monkeypatch):
    def router(method, url):
        return {
            "id": "ind-1",
            "type": "domain",
            "value": "evil.example.com",
            "score": {"value": "malicious"},
            "mitre_attack_ids": [{"id": "T1071", "name": "Application Layer Protocol"}],
            "sightings": [
                {
                    "sighted_at": "2026-07-01T00:00:00Z",
                    "source": "flashpoint_extraction",
                    "tags": ["malware:xworm"],
                }
            ],
            "hashes": {"md5": "a" * 32},
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c8",
            name="get_flashpoint_indicator",
            arguments={"indicator_id": "ind-1", "sighting_count": 3},
        )
    )
    assert not result.is_error
    assert "evil.example.com" in result.content
    assert "T1071" in result.content
    assert "flashpoint_extraction" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/technical-intelligence/v2/indicators/ind-1")
    assert call["params"]["sighting_count"] == 3


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_events_forwards_filters(monkeypatch):
    def router(method, url):
        return [
            {
                "fpid": "ev-1",
                "Event": {"info": "Observation: xworm", "timestamp": "1717130952"},
                "Tag": [{"name": "mitre:T1001"}],
            }
        ]

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c9",
            name="list_flashpoint_events",
            arguments={"attack_ids": "T1001", "limit": 2, "time_period": "7d"},
        )
    )
    assert not result.is_error
    assert "Observation: xworm" in result.content
    assert "mitre:T1001" in result.content
    params = recorder.calls[0]["params"]
    assert params["attack_ids"] == "T1001"
    assert params["time_period"] == "7d"
    assert params["sort_timestamp"] == "desc"
    assert recorder.calls[0]["url"].endswith("/technical-intelligence/v1/event")


# ---------------------------------------------------------------------------
# Communities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_communities_posts_body(monkeypatch):
    def router(method, url):
        return {
            "items": [
                {
                    "id": "doc-1",
                    "title": "selling corp vpn access",
                    "message": "access to acme corp vpn for sale",
                    "date": "2026-07-01T00:00:00Z",
                    "site": {"title": "exploit forum"},
                    "container": {"title": "access market"},
                    "site_actor": {"names": {"handle": "broker99"}},
                }
            ],
            "total": {"value": 1},
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c10",
            name="search_flashpoint_communities",
            arguments={"query": '"acme corp"', "since": "now-30d", "size": 5},
        )
    )
    assert not result.is_error
    assert "selling corp vpn access" in result.content
    assert "broker99" in result.content
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/sources/v2/communities")
    assert call["json"]["query"] == '"acme corp"'
    assert call["json"]["size"] == 5
    assert call["json"]["include"]["date"]["start"] == "now-30d"


@pytest.mark.asyncio
async def test_search_communities_requires_query(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="c11", name="search_flashpoint_communities", arguments={"query": "  "})
    )
    assert "query is required" in result.content.lower()
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# CCM - compromised credentials
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_credentials_builds_lucene_query(monkeypatch):
    def router(method, url):
        return {
            "hits": {
                "total": 150,
                "hits": [
                    {
                        "_id": "cs-1",
                        "_source": {
                            "email": "someone@example.com",
                            "domain": "example.com",
                            "fpid": "fp-1",
                            "is_fresh": True,
                            "password": "hunter2",
                            "breach": {
                                "title": "Redline stealer logs May 2026",
                                "source": "Analyst Research",
                                "source_type": "Credential Stealer",
                                "first_observed_at": {"date-time": "2026-05-10T16:13:34Z"},
                            },
                        },
                    }
                ],
            }
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c12",
            name="search_flashpoint_credentials",
            arguments={
                "query": "+domain:example.com",
                "start_date": "2026-05-01T00:00:00Z",
                "fresh_only": True,
                "sort_order": "asc",
            },
        )
    )
    assert not result.is_error
    assert "someone@example.com" in result.content
    assert "Redline stealer logs" in result.content
    params = recorder.calls[0]["params"]
    assert params["query"].startswith("+basetypes:(credential-sighting)")
    assert "+is_fresh:true" in params["query"]
    assert "+breach.created_at.date-time: [2026-05-01T00:00:00Z TO now]" in params["query"]
    assert "+domain:example.com" in params["query"]
    assert params["sort"] == "breach.created_at.timestamp:asc"
    assert recorder.calls[0]["url"].endswith("/sources/v1/noncommunities/search")


@pytest.mark.asyncio
async def test_search_credentials_rejects_bad_filter_date(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c13",
            name="search_flashpoint_credentials",
            arguments={"filter_date": "last_seen"},
        )
    )
    assert "filter_date must be" in result.content.lower()
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_alerts_maps_sources_and_extracts_cursor(monkeypatch):
    def router(method, url):
        return {
            "items": [
                {
                    "id": "al-1",
                    "source": "github",
                    "status": None,
                    "created_at": "2026-07-01T00:00:00Z",
                    "reason": {"name": "acme keyword", "origin": "searches"},
                    "resource": {
                        "title": "leaked config",
                        "url": "https://github.example/repo/file",
                    },
                }
            ],
            "pagination": {
                "next": "https://api.flashpoint.io/alert-management/v1/notifications?cursor=abc123&size=20"
            },
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c14",
            name="list_flashpoint_alerts",
            arguments={"sources": "Github, Communities", "status": "starred"},
        )
    )
    assert not result.is_error
    assert "leaked config" in result.content
    assert "abc123" in result.content  # cursor pagination hint
    params = recorder.calls[0]["params"]
    assert params["sources"] == "data_exposure__github,communities"
    assert params["status"] == "flagged"  # "starred" maps to "flagged" on the wire
    assert recorder.calls[0]["url"].endswith("/alert-management/v1/notifications")


@pytest.mark.asyncio
async def test_list_alerts_rejects_unknown_source(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c15",
            name="list_flashpoint_alerts",
            arguments={"sources": "pastebin"},
        )
    )
    assert "sources values must be among" in result.content.lower()
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Vulnerability intelligence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_vulnerabilities_splits_query_and_body(monkeypatch):
    def router(method, url):
        return {
            "total": 42,
            "results": [
                {
                    "id": 351481,
                    "title": "pdfmake RCE",
                    "cve_ids": ["CVE-2024-25180"],
                    "description": "Remote code execution in pdfmake.",
                    "scores": {"severity": "Critical", "epss_score": 0.43, "cvssv3_score": 9.8},
                    "timelines": {"published_at": "2024-03-05T08:35:30Z"},
                    "vuln_status": "Active",
                    "exploits_count": 2,
                }
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c16",
            name="search_flashpoint_vulnerabilities",
            arguments={
                "severities": "Critical, High",
                "products": "pdfmake",
                "min_epss_score": 0.4,
                "published_after": "-90d",
                "size": 1,
            },
        )
    )
    assert not result.is_error
    assert "pdfmake RCE" in result.content
    assert "CVE-2024-25180" in result.content
    assert "from_offset=1" in result.content  # pagination hint (42 total)
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/vulnerability-intelligence/v1/vulnerabilities")
    assert call["params"]["size"] == 1
    assert call["params"]["sort"] == "published_at:desc"
    assert call["json"]["severity"] == "critical,high"
    assert call["json"]["products"] == "pdfmake"
    assert call["json"]["min_epss_score"] == 0.4
    assert call["json"]["published_after"] == "-90d"


@pytest.mark.asyncio
async def test_search_vulnerabilities_validation_guards(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c17",
            name="search_flashpoint_vulnerabilities",
            arguments={"severities": "catastrophic"},
        )
    )
    assert "severities must be among" in result.content.lower()
    result = await executor.execute_tool(
        ToolCall(
            id="c18",
            name="search_flashpoint_vulnerabilities",
            arguments={"min_epss_score": 3.5},
        )
    )
    assert "min_epss_score must be between" in result.content.lower()
    result = await executor.execute_tool(
        ToolCall(
            id="c19",
            name="search_flashpoint_vulnerabilities",
            arguments={"sort_by": "epss"},
        )
    )
    assert "sort_by must be one of" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_lookup_cve_forwards_ids(monkeypatch):
    def router(method, url):
        return {
            "results": [
                {
                    "id": 351481,
                    "title": "pdfmake RCE",
                    "cve_ids": ["CVE-2024-25180"],
                    "solution": "Upgrade pdfmake.",
                    "scores": {"severity": "Critical"},
                    "timelines": {"published_at": "2024-03-05T08:35:30Z"},
                    "cwes": [{"cwe_id": "94", "name": "Code Injection"}],
                }
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c20",
            name="lookup_flashpoint_cve",
            arguments={"cve_ids": "CVE-2024-25180, CVE-2024-0001"},
        )
    )
    assert not result.is_error
    assert "pdfmake RCE" in result.content
    assert "Upgrade pdfmake." in result.content
    assert recorder.calls[0]["params"]["cves"] == "CVE-2024-25180,CVE-2024-0001"


@pytest.mark.asyncio
async def test_lookup_cve_requires_ids(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="c21", name="lookup_flashpoint_cve", arguments={"cve_ids": " , "})
    )
    assert "cve_ids is required" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_affected_components_routes_by_type(monkeypatch):
    def router(method, url):
        return {
            "total": 2,
            "results": [
                {
                    "id": 10,
                    "name": "requests",
                    "version": "2.31.0",
                    "type": "pypi",
                    "affected": True,
                    "constructed_purl": "pkg:pypi/requests@2.31.0",
                }
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c22",
            name="list_flashpoint_affected_components",
            arguments={"vulnerability_id": "351481", "component_type": "libraries"},
        )
    )
    assert not result.is_error
    assert "requests" in result.content
    # Explicit noun label - never the broken "librarie"/"librarys" forms that
    # naive rstrip("s") singularization would produce.
    assert "library components" in result.content
    assert "librarie" not in result.content.replace("library components", "")
    assert recorder.calls[0]["url"].endswith(
        "/vulnerability-intelligence/v1/vulnerabilities/351481/libraries"
    )

    result = await executor.execute_tool(
        ToolCall(
            id="c23",
            name="list_flashpoint_affected_components",
            arguments={"vulnerability_id": "351481", "component_type": "firmware"},
        )
    )
    assert "component_type must be" in result.content.lower()


@pytest.mark.asyncio
async def test_vuln_catalog_vendors_and_products(monkeypatch):
    def router(method, url):
        return {"total": 1, "results": [{"id": 1001, "name": "Fortinet"}]}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c24",
            name="search_flashpoint_vuln_catalog",
            arguments={"entity_type": "vendors", "name": "fortinet"},
        )
    )
    assert not result.is_error
    assert "Fortinet" in result.content
    assert recorder.calls[0]["url"].endswith("/vulnerability-intelligence/v1/vendors")
    assert recorder.calls[0]["params"]["name"] == "fortinet"

    result = await executor.execute_tool(
        ToolCall(
            id="c25",
            name="search_flashpoint_vuln_catalog",
            arguments={"entity_type": "people"},
        )
    )
    assert "entity_type must be" in result.content.lower()


# ---------------------------------------------------------------------------
# Path-segment encoding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_path_segments_are_percent_encoded(monkeypatch):
    """Ids containing ``/`` cannot escape their URL path segment."""
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    await executor.execute_tool(
        ToolCall(
            id="c26",
            name="get_flashpoint_report",
            arguments={"report_id": "rep/../../admin"},
        )
    )
    assert recorder.calls[0]["url"].endswith(
        "/finished-intelligence/v1/reports/rep%2F..%2F..%2Fadmin"
    )

    recorder.calls.clear()
    await executor.execute_tool(
        ToolCall(
            id="c27",
            name="get_flashpoint_indicator",
            arguments={"indicator_id": "ind/1?x=1"},
        )
    )
    assert recorder.calls[0]["url"].endswith(
        "/technical-intelligence/v2/indicators/ind%2F1%3Fx%3D1"
    )

    recorder.calls.clear()
    await executor.execute_tool(
        ToolCall(
            id="c28",
            name="get_flashpoint_community_post",
            arguments={"document_id": "doc/../secret"},
        )
    )
    assert recorder.calls[0]["url"].endswith(
        "/sources/v2/communities/doc%2F..%2Fsecret"
    )


@pytest.mark.asyncio
async def test_deep_links_percent_encode_api_returned_ids(monkeypatch):
    """API-returned ids interpolated into app deep links are encoded too."""

    def router(method, url):
        return {
            "id": "doc/1?x=1",
            "title": "odd id",
            "message": "body",
            "site": {"title": "forum"},
        }

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c29",
            name="get_flashpoint_community_post",
            arguments={"document_id": "doc-1"},
        )
    )
    assert not result.is_error
    assert "/search/context/communities/doc%2F1%3Fx%3D1" in result.content
    assert "/search/context/communities/doc/1?x=1" not in result.content
