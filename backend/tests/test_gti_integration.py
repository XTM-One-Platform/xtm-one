"""Tests for the Google Threat Intelligence (GTI) integration provider.

Covers tool introspection (exact count across the enrichment / threat
landscape / threat list domains), the ``x-apikey`` + ``x-tool`` header
chokepoint, IoC classification for batch summaries, GTI assessment
formatting (verdict / threat score / severity + the license-gated hint),
threat search filter building, per-threat IoC search defaults, MITRE tree
filter validation, threat list packages, Code Insight base64 round-tripping,
and the DTM Link-header pagination helper. Every API call routes through the
shared ``safe_request`` helper, which is monkeypatched on each domain module
so no network is touched.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from app.integrations.providers.gti import (
    GTI_API,
    GTI_ASM_API,
    GTI_DTM_API,
    asm,
    build_gti_headers,
    build_tools,
    dtm,
    enrichment,
    ioc_stream,
    threat_lists,
    threat_profiles,
    threats,
)
from app.integrations.providers.gti._common import (
    classify_ioc,
    format_gti_assessment,
    gui_link,
    next_page_token,
    trunc,
    ts,
    url_identifier,
)
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "gti-key-123"
_DOMAIN_MODULES = (
    enrichment,
    threats,
    threat_lists,
    ioc_stream,
    threat_profiles,
    dtm,
    asm,
)


class _StubResponse:
    def __init__(self, payload: Any, status: int = 200, headers: dict[str, str] | None = None):
        self._payload = payload
        self.status_code = status
        self.is_success = 200 <= status < 300
        self.headers = headers or {}

    def json(self) -> Any:
        return self._payload


class _Recorder:
    """Fake ``safe_request`` that routes by (method, url) and records calls.

    A router may return a bare payload (treated as HTTP 200) or a
    ``(payload, status)`` tuple to simulate an error; the
    ``expected_statuses`` contract of the real helper is honoured (the failed
    response is returned so callers can branch on ``status_code``).
    """

    def __init__(self, router):
        self._router = router
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self, _client, method, url, _provider, action, *, expected_statuses=None, **kwargs
    ):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "action": action,
                "expected_statuses": expected_statuses,
                **kwargs,
            }
        )
        out = self._router(method, url)
        payload, status = out if isinstance(out, tuple) else (out, 200)
        resp = _StubResponse(payload, status)
        if status >= 400:
            err = f"Error: HTTP {status}"
            if expected_statuses is not None:
                return resp, err
            return None, err
        return resp, None


def _build(monkeypatch, router) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(router)
    for module in _DOMAIN_MODULES:
        monkeypatch.setattr(module, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Introspection + helpers
# ---------------------------------------------------------------------------


def test_build_tools_returns_definitions_with_dummy_token():
    executor = CallableToolExecutor()
    tools = build_tools(token="__dummy__", executor=executor)
    names = {t.name for t in tools}
    for expected in (
        "get_gti_file_assessment",
        "get_gti_url_assessment",
        "get_gti_domain_assessment",
        "get_gti_ip_assessment",
        "get_gti_ioc_summaries",
        "analyse_gti_code",
        "search_gti_threats",
        "get_gti_threat",
        "get_gti_threat_related_objects",
        "search_gti_threat_iocs",
        "get_gti_threat_mitre_tree",
        "get_gti_threat_timeline",
        "list_gti_threat_lists",
        "get_gti_threat_list",
        "get_gti_ioc_stream",
        "manage_gti_ioc_stream_notification",
        "list_gti_threat_profiles",
        "get_gti_threat_profile",
        "get_gti_threat_profile_recommendations",
        "manage_gti_threat_profile",
        "list_gti_dtm_alerts",
        "get_gti_dtm_alert",
        "update_gti_dtm_alert",
        "search_gti_dtm_docs",
        "list_gti_dtm_monitors",
        "get_gti_dtm_monitor",
        "manage_gti_dtm_monitor",
        "list_gti_asm_projects",
        "search_gti_asm_entities",
        "get_gti_asm_entity",
        "search_gti_asm_issues",
        "get_gti_asm_issue",
        "set_gti_asm_issue_status",
        "search_gti_asm_technologies",
    ):
        assert expected in names
    # Exact count - 34 tools across the seven domains. Assert the precise
    # number so an accidentally dropped or duplicated tool fails the build.
    assert len(tools) == 40
    # Every tool advertises the provider source.
    assert all(t.source == "integration:gti" for t in tools)
    # Reads sort before writes before destructive-capable manage tools.
    priorities = {t.name: t.priority for t in tools}
    assert priorities["get_gti_file_assessment"] == 0
    assert priorities["get_gti_ioc_stream"] == 0
    assert priorities["update_gti_dtm_alert"] == 1
    assert priorities["set_gti_asm_issue_status"] == 1
    assert priorities["manage_gti_ioc_stream_notification"] == 2
    assert priorities["manage_gti_threat_profile"] == 2
    assert priorities["manage_gti_dtm_monitor"] == 2


def test_build_tools_empty_without_token():
    executor = CallableToolExecutor()
    assert build_tools(token="", executor=executor) == []
    assert build_tools(token="   ", executor=executor) == []


def test_headers_helper_strips_key_and_sends_x_tool():
    headers = build_gti_headers("abc")
    assert headers["x-apikey"] == "abc"
    # The x-tool header is REQUIRED for the gti_assessment attribute to
    # appear in IoC responses - losing it silently degrades every
    # assessment tool.
    assert headers["x-tool"] == "xtm-one"
    assert headers["Accept"] == "application/json"
    # The key is stripped so pasted whitespace/newlines can't produce a
    # malformed (h11-rejected) x-apikey header.
    assert build_gti_headers("  abc\n")["x-apikey"] == "abc"


def test_classify_ioc_detects_types():
    assert classify_ioc("d" * 32) == "file"  # MD5
    assert classify_ioc("d" * 40) == "file"  # SHA-1
    assert classify_ioc("d" * 64) == "file"  # SHA-256
    assert classify_ioc("http://example.com/x") == "url"
    assert classify_ioc("example.com/path") == "url"
    assert classify_ioc("8.8.8.8") == "ip_address"
    assert classify_ioc("2001:db8::1") == "ip_address"
    assert classify_ioc("[2001:db8::1]") == "ip_address"
    assert classify_ioc("example.com") == "domain"
    # Colon-bearing non-IP values must NOT land in the ip_address bucket -
    # the /iocs/summary batch is all-or-nothing and one bad descriptor
    # rejects the whole request.
    assert classify_ioc("example.com:443") == "domain"
    assert classify_ioc("999.1.1.1") == "domain"


def test_format_gti_assessment_renders_verdict_score_severity():
    lines = format_gti_assessment(
        {
            "gti_assessment": {
                "verdict": {"value": "VERDICT_MALICIOUS"},
                "threat_score": {"value": 87},
                "severity": {"value": "SEVERITY_HIGH"},
                "description": "Curated summary.",
            }
        }
    )
    joined = "\n".join(lines)
    assert "verdict malicious" in joined
    assert "threat score 87/100" in joined
    assert "severity high" in joined
    assert "Curated summary." in joined


def test_format_gti_assessment_hints_when_missing():
    lines = format_gti_assessment({})
    assert any("not available" in line for line in lines)


def test_trunc_neutralizes_control_characters():
    assert trunc("a\r\nb\x1b[31mc\x00d\x7fe") == "a b [31mc d e"
    assert trunc("x" * 10, max_len=5) == "xxxxx\u2026"
    assert trunc(None) == ""


def test_ts_degrades_gracefully_on_absurd_epochs():
    assert ts(1750000000) == "2025-06-15 15:06:40"
    assert ts(None) == ""
    assert ts("garbage") == ""
    assert ts(10**18) == ""


def test_gui_link_percent_encodes_id():
    assert gui_link("collection", "threat-actor--abc") == (
        "https://www.virustotal.com/gui/collection/threat-actor--abc"
    )
    link = gui_link("domain", "evil.example/../phish?x=1 #frag")
    assert "?" not in link
    assert " " not in link
    assert "/gui/domain/evil.example%2F" in link


def test_url_identifier_forms():
    assert url_identifier("http://example.com/") == "aHR0cDovL2V4YW1wbGUuY29tLw"
    sha = "a" * 64
    assert url_identifier(sha) == sha
    assert url_identifier("") == ""


def test_next_page_token_parses_dtm_link_header():
    resp = _StubResponse(
        {},
        headers={
            "link": '<https://www.virustotal.com/api/v3/dtm/alerts?page=tok123&size=10>; rel="next"'
        },
    )
    assert next_page_token(resp) == "tok123"
    assert next_page_token(_StubResponse({})) is None
    assert next_page_token(None) is None
    # rel="prev" only -> no next page.
    resp_prev = _StubResponse(
        {}, headers={"link": '<https://x.example/a?page=zz>; rel="prev"'}
    )
    assert next_page_token(resp_prev) is None


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_file_assessment_formats_gti_verdict(monkeypatch):
    sha256 = "b" * 64

    def router(method, url):
        return {
            "data": {
                "id": sha256,
                "type": "file",
                "attributes": {
                    "meaningful_name": "evil.exe",
                    "sha256": sha256,
                    "type_description": "Win32 EXE",
                    "size": 12345,
                    "gti_assessment": {
                        "verdict": {"value": "VERDICT_MALICIOUS"},
                        "threat_score": {"value": 92},
                        "severity": {"value": "SEVERITY_HIGH"},
                    },
                    "last_analysis_stats": {
                        "malicious": 45,
                        "suspicious": 2,
                        "harmless": 0,
                        "undetected": 23,
                    },
                    "popular_threat_classification": {
                        "suggested_threat_label": "trojan.emotet/krypt"
                    },
                },
                "relationships": {
                    "malware_families": {
                        "data": [
                            {
                                "id": "malware--1",
                                "type": "collection",
                                "attributes": {"name": "EMOTET"},
                            }
                        ]
                    }
                },
            }
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="f1", name="get_gti_file_assessment", arguments={"hash": sha256})
    )
    assert not result.is_error
    assert "verdict malicious" in result.content
    assert "threat score 92/100" in result.content
    assert "severity high" in result.content
    assert "45 malicious" in result.content
    assert "trojan.emotet/krypt" in result.content
    assert "EMOTET" in result.content
    assert f"https://www.virustotal.com/gui/file/{sha256}" in result.content
    assert recorder.calls[0]["url"] == f"{GTI_API}/files/{sha256}"


@pytest.mark.asyncio
async def test_file_assessment_404(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: ({"error": {}}, 404))
    result = await executor.execute_tool(
        ToolCall(id="f2", name="get_gti_file_assessment", arguments={"hash": "d" * 64})
    )
    assert not result.is_error
    assert "never seen" in result.content.lower()
    assert recorder.calls[0]["expected_statuses"] == {404}


@pytest.mark.asyncio
async def test_file_assessment_rejects_unexpected_response_shape(monkeypatch):
    """A 200 whose ``data`` is not an object must surface an error instead of
    rendering a misleading empty report."""
    executor, _recorder = _build(monkeypatch, lambda m, u: {"data": ["not", "a", "dict"]})
    result = await executor.execute_tool(
        ToolCall(id="f3", name="get_gti_file_assessment", arguments={"hash": "d" * 64})
    )
    assert "unexpected response shape" in result.content.lower()


@pytest.mark.asyncio
async def test_url_assessment_encodes_raw_url(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        lambda m, u: {"data": {"id": "xyz", "attributes": {"url": "http://example.com/"}}},
    )
    result = await executor.execute_tool(
        ToolCall(id="u1", name="get_gti_url_assessment", arguments={"url": "http://example.com/"})
    )
    assert not result.is_error
    assert recorder.calls[0]["url"] == f"{GTI_API}/urls/aHR0cDovL2V4YW1wbGUuY29tLw"


@pytest.mark.asyncio
async def test_action_strings_sanitize_user_input(monkeypatch):
    """User-supplied identifiers are trunc()'d before entering safe_request
    action strings (which surface in logs and error messages)."""
    executor, recorder = _build(monkeypatch, lambda m, u: {"data": {}})
    hostile = "abc\r\nInjected-Log-Line: pwned\x1b[2Jcleared"
    await executor.execute_tool(
        ToolCall(id="lg1", name="get_gti_file_assessment", arguments={"hash": hostile})
    )
    await executor.execute_tool(
        ToolCall(id="lg2", name="get_gti_domain_assessment", arguments={"domain": hostile})
    )
    await executor.execute_tool(
        ToolCall(id="lg3", name="get_gti_threat", arguments={"threat_id": hostile})
    )
    for call in recorder.calls:
        assert "\n" not in call["action"]
        assert "\r" not in call["action"]
        assert "\x1b" not in call["action"]


@pytest.mark.asyncio
async def test_result_text_sanitizes_untrusted_api_attributes(monkeypatch):
    """GTI/community-supplied attributes echoed in tool RESULT text are
    trunc()'d - control characters cannot inject fake output lines."""
    hostile = "clean-part\r\nFAKE LINE: pwned\x1b[31m"

    def router(method, url):
        return {
            "data": {
                "id": hostile,
                "type": "file",
                "attributes": {"meaningful_name": hostile, "sha256": hostile},
            }
        }

    executor, _recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="sx1", name="get_gti_file_assessment", arguments={"hash": "a" * 64})
    )
    assert not result.is_error
    assert "FAKE LINE: pwned" not in result.content.split("\n")
    assert "\r" not in result.content
    assert "\x1b" not in result.content


@pytest.mark.asyncio
async def test_ioc_summaries_builds_descriptors(monkeypatch):
    def router(method, url):
        return {
            "data": [
                {
                    "data": {
                        "id": "d" * 64,
                        "type": "file",
                        "attributes": {
                            "gti_assessment": {
                                "verdict": {"value": "VERDICT_MALICIOUS"},
                                "threat_score": {"value": 80},
                            },
                            "positives": 41,
                        },
                    }
                }
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="b1",
            name="get_gti_ioc_summaries",
            arguments={"iocs": f"{'d' * 64}, evil.example, 8.8.8.8, http://example.com/x"},
        )
    )
    assert not result.is_error
    assert "malicious" in result.content
    assert "score 80" in result.content
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"] == f"{GTI_API}/iocs/summary"
    descriptors = call["json"]["iocs"]
    assert {"type": "file", "id": "d" * 64} in descriptors
    assert {"type": "domain", "id": "evil.example"} in descriptors
    assert {"type": "ip_address", "id": "8.8.8.8"} in descriptors
    # Raw URLs are converted to their unpadded base64url identifiers.
    assert {"type": "url", "id": url_identifier("http://example.com/x")} in descriptors


@pytest.mark.asyncio
async def test_ioc_summaries_rejects_oversized_batch(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    too_many = ", ".join(f"host{i}.example" for i in range(101))
    result = await executor.execute_tool(
        ToolCall(id="b2", name="get_gti_ioc_summaries", arguments={"iocs": too_many})
    )
    assert "too many" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_analyse_code_round_trips_base64(monkeypatch):
    analysis = "This code sets a Run-key persistence entry."
    encoded_analysis = base64.b64encode(analysis.encode("utf-8")).decode("ascii")

    executor, recorder = _build(monkeypatch, lambda m, u: {"data": encoded_analysis})
    code = "RegSetValueExA(hKey, \"MalwareEntry\", ...)"
    result = await executor.execute_tool(
        ToolCall(
            id="ci1",
            name="analyse_gti_code",
            arguments={"code": code, "code_type": "decompiled"},
        )
    )
    assert not result.is_error
    assert "Run-key persistence" in result.content
    call = recorder.calls[0]
    assert call["url"] == f"{GTI_API}/codeinsights/analyse-binary"
    sent = call["json"]["data"]
    assert sent["code_type"] == "decompiled"
    assert base64.b64decode(sent["code"]).decode("utf-8") == code


@pytest.mark.asyncio
async def test_analyse_code_rejects_bad_code_type(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="ci2",
            name="analyse_gti_code",
            arguments={"code": "x", "code_type": "sourcecode"},
        )
    )
    assert "code_type must be one of" in result.content.lower()
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Threat Landscape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_threats_combines_type_and_query(monkeypatch):
    def router(method, url):
        return {
            "data": [
                {
                    "id": "threat-actor--abc",
                    "type": "collection",
                    "attributes": {
                        "name": "APT99",
                        "collection_type": "threat-actor",
                        "origin": "Google Threat Intelligence",
                        "last_modification_date": 1750000000,
                    },
                }
            ],
            "meta": {"cursor": "next-cursor"},
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="t1",
            name="search_gti_threats",
            arguments={
                "query": "motivation:espionage",
                "collection_type": "threat-actor",
                "order": "relevance-",
                "limit": 5,
            },
        )
    )
    assert not result.is_error
    assert "APT99" in result.content
    assert "threat-actor--abc" in result.content
    assert 'cursor="next-cursor"' in result.content
    call = recorder.calls[0]
    assert call["url"] == f"{GTI_API}/collections"
    assert call["params"]["filter"] == "collection_type:threat-actor motivation:espionage"
    assert call["params"]["order"] == "relevance-"
    assert call["params"]["limit"] == 5


@pytest.mark.asyncio
async def test_search_threats_rejects_bad_collection_type(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="t2",
            name="search_gti_threats",
            arguments={"collection_type": "apt-group"},
        )
    )
    assert "collection_type must be one of" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_get_threat_formats_profile(monkeypatch):
    def router(method, url):
        return {
            "data": {
                "id": "malware--76feb45b",
                "type": "collection",
                "attributes": {
                    "name": "EMOTET",
                    "collection_type": "malware-family",
                    "description": "Modular banking trojan turned loader.",
                    "origin": "Google Threat Intelligence",
                    "motivations": [{"value": "Financial Gain"}],
                    "targeted_regions_hierarchy": [{"country": "France"}],
                    "counters": {"files": 120, "domains": 14},
                    "creation_date": 1600000000,
                    "last_modification_date": 1750000000,
                },
            }
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="t3", name="get_gti_threat", arguments={"threat_id": "malware--76feb45b"})
    )
    assert not result.is_error
    assert "EMOTET" in result.content
    assert "malware-family" in result.content
    assert "Financial Gain" in result.content
    assert "120 files" in result.content
    assert "https://www.virustotal.com/gui/collection/malware--76feb45b" in result.content
    assert recorder.calls[0]["url"] == f"{GTI_API}/collections/malware--76feb45b"


@pytest.mark.asyncio
async def test_threat_related_objects_paginates(monkeypatch):
    def router(method, url):
        return {
            "data": [
                {
                    "type": "file",
                    "id": "e" * 64,
                    "attributes": {"last_analysis_stats": {"malicious": 7}},
                }
            ],
            "meta": {"cursor": "more"},
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="t4",
            name="get_gti_threat_related_objects",
            arguments={
                "threat_id": "threat-actor--abc",
                "relationship": "files",
                "limit": 5,
                "cursor": "prev",
            },
        )
    )
    assert not result.is_error
    assert "[7 malicious]" in result.content
    call = recorder.calls[0]
    assert call["url"] == f"{GTI_API}/collections/threat-actor--abc/files"
    assert call["params"] == {"limit": 5, "cursor": "prev"}


@pytest.mark.asyncio
async def test_search_threat_iocs_defaults_query(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"data": [], "meta": {}})
    result = await executor.execute_tool(
        ToolCall(
            id="t5",
            name="search_gti_threat_iocs",
            arguments={"threat_id": "campaign--24f"},
        )
    )
    assert not result.is_error
    call = recorder.calls[0]
    assert call["url"] == f"{GTI_API}/collections/campaign--24f/search"
    # The endpoint requires a query; the tool defaults to the match-all p:0+.
    assert call["params"]["query"] == "p:0+"


@pytest.mark.asyncio
async def test_mitre_tree_validates_filters(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"data": {"tactics": []}})
    result = await executor.execute_tool(
        ToolCall(
            id="t6",
            name="get_gti_threat_mitre_tree",
            arguments={"threat_id": "x", "mitre_namespace": "cloud"},
        )
    )
    assert "mitre_namespace must be one of" in result.content.lower()
    assert recorder.calls == []
    result = await executor.execute_tool(
        ToolCall(
            id="t7",
            name="get_gti_threat_mitre_tree",
            arguments={
                "threat_id": "threat-actor--abc",
                "mitre_namespace": "mobile",
                "ttp_source": "operational",
            },
        )
    )
    assert not result.is_error
    call = recorder.calls[0]
    assert call["url"] == f"{GTI_API}/collections/threat-actor--abc/mitre_tree"
    assert call["params"]["filter"] == "mitre_namespace:mobile ttp_source:operational"


@pytest.mark.asyncio
async def test_mitre_tree_formats_tactics(monkeypatch):
    def router(method, url):
        return {
            "data": {
                "tactics": [
                    {
                        "id": "TA0005",
                        "name": "Defense Evasion",
                        "techniques": [
                            {
                                "id": "T1564",
                                "name": "Hide Artifacts",
                                "source": ["operational"],
                            }
                        ],
                    }
                ]
            }
        }

    executor, _recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="t8",
            name="get_gti_threat_mitre_tree",
            arguments={"threat_id": "threat-actor--abc"},
        )
    )
    assert not result.is_error
    assert "TA0005" in result.content
    assert "T1564" in result.content
    assert "operational" in result.content


@pytest.mark.asyncio
async def test_threat_timeline_formats_events(monkeypatch):
    def router(method, url):
        return {
            "data": [
                {
                    "event_at": 1714003200,
                    "name": "First Observed",
                    "event_category": "First Observed",
                    "description": "Mandiant Observed First Activity of Campaign",
                    "event_type": "first_observed",
                }
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="t9",
            name="get_gti_threat_timeline",
            arguments={"threat_id": "campaign--183a"},
        )
    )
    assert not result.is_error
    assert "First Observed" in result.content
    assert recorder.calls[0]["url"] == f"{GTI_API}/collections/campaign--183a/timeline/events"


# ---------------------------------------------------------------------------
# Threat Lists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_threat_lists(monkeypatch):
    def router(method, url):
        return {
            "data": [
                {
                    "id": "ransomware",
                    "type": "threat_list",
                    "attributes": {"description": "Ransomware Threat List"},
                }
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="l1", name="list_gti_threat_lists", arguments={})
    )
    assert not result.is_error
    assert "ransomware" in result.content
    assert recorder.calls[0]["url"] == f"{GTI_API}/threat_lists"


@pytest.mark.asyncio
async def test_get_threat_list_latest_with_filters(monkeypatch):
    def router(method, url):
        return {
            "iocs": [
                {
                    "data": {
                        "id": "f" * 64,
                        "type": "file",
                        "attributes": {
                            "gti_assessment": {"threat_score": {"value": 71}},
                            "positives": 33,
                        },
                    }
                }
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="l2",
            name="get_gti_threat_list",
            arguments={
                "threat_list_id": "ransomware",
                "ioc_types": "file",
                "query": "gti_score:60+",
            },
        )
    )
    assert not result.is_error
    assert "score 71" in result.content
    assert "33 detections" in result.content
    call = recorder.calls[0]
    assert call["url"] == f"{GTI_API}/threat_lists/ransomware/latest"
    assert call["params"] == {"type": "file", "query": "gti_score:60+"}


@pytest.mark.asyncio
async def test_get_threat_list_historical_slot(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"iocs": []})
    result = await executor.execute_tool(
        ToolCall(
            id="l3",
            name="get_gti_threat_list",
            arguments={"threat_list_id": "phishing", "time": "2026070610"},
        )
    )
    assert not result.is_error
    assert recorder.calls[0]["url"] == f"{GTI_API}/threat_lists/phishing/2026070610"


# ---------------------------------------------------------------------------
# IoC Stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ioc_stream_passes_filters(monkeypatch):
    def router(method, url):
        return {
            "data": [
                {
                    "type": "file",
                    "id": "a" * 64,
                    "attributes": {"last_analysis_stats": {"malicious": 12}},
                    "context_attributes": {
                        "origin": "hunting",
                        "notification_date": 1750000000,
                        "hunting_info": {"rule_name": "emotet_loader"},
                    },
                }
            ],
            "meta": {"cursor": "next"},
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="st1",
            name="get_gti_ioc_stream",
            arguments={
                "filter": "origin:hunting entity_type:file",
                "order": "date-",
                "limit": 5,
                "descriptors_only": True,
            },
        )
    )
    assert not result.is_error
    assert "emotet_loader" in result.content
    assert "[12 malicious]" in result.content
    assert 'cursor="next"' in result.content
    call = recorder.calls[0]
    assert call["url"] == f"{GTI_API}/ioc_stream"
    assert call["params"]["filter"] == "origin:hunting entity_type:file"
    assert call["params"]["order"] == "date-"
    assert call["params"]["descriptors_only"] == "true"


@pytest.mark.asyncio
async def test_manage_stream_notification_requires_id(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="st2",
            name="manage_gti_ioc_stream_notification",
            arguments={"action": "delete"},
        )
    )
    assert "notification_id is required" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_stream_delete_filtered_requires_filter(monkeypatch):
    """A blanket DELETE /ioc_stream without a filter would wipe the whole
    stream - the tool refuses it."""
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="st3",
            name="manage_gti_ioc_stream_notification",
            arguments={"action": "delete_filtered"},
        )
    )
    assert "filter is required" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_stream_delete_filtered_deletes(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"data": {}})
    result = await executor.execute_tool(
        ToolCall(
            id="st4",
            name="manage_gti_ioc_stream_notification",
            arguments={"action": "delete_filtered", "filter": "origin:hunting"},
        )
    )
    assert not result.is_error
    assert "permanently deleted" in result.content
    call = recorder.calls[0]
    assert call["method"] == "delete"
    assert call["url"] == f"{GTI_API}/ioc_stream"
    assert call["params"] == {"filter": "origin:hunting"}


@pytest.mark.asyncio
async def test_manage_stream_delete_single(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"data": {}})
    result = await executor.execute_tool(
        ToolCall(
            id="st5",
            name="manage_gti_ioc_stream_notification",
            arguments={"action": "delete", "notification_id": "n-123"},
        )
    )
    assert not result.is_error
    call = recorder.calls[0]
    assert call["method"] == "delete"
    assert call["url"] == f"{GTI_API}/ioc_stream_notifications/n-123"


# ---------------------------------------------------------------------------
# Threat Profiles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_threat_profiles_shows_role(monkeypatch):
    def router(method, url):
        return {
            "data": [
                {
                    "id": "tp-1",
                    "type": "threat_profile",
                    "attributes": {
                        "name": "EU Fin threats",
                        "last_modification_date": 1750000000,
                    },
                    "context_attributes": {"role": "owner", "shared_with_me": False},
                }
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="tp1", name="list_gti_threat_profiles", arguments={})
    )
    assert not result.is_error
    assert "EU Fin threats" in result.content
    assert "owner" in result.content
    assert recorder.calls[0]["url"] == f"{GTI_API}/threat_profiles"


@pytest.mark.asyncio
async def test_get_threat_profile_formats_interests(monkeypatch):
    def router(method, url):
        return {
            "data": {
                "id": "tp-1",
                "type": "threat_profile",
                "attributes": {
                    "name": "Spanish threat activity",
                    "interests": {
                        "INTEREST_TYPE_TARGETED_REGION": ["ES"],
                        "INTEREST_TYPE_ACTOR_MOTIVATION": ["Financial Gain"],
                    },
                    "recommendation_config": {"max_recs_per_type": 10},
                },
                "context_attributes": {"role": "editor"},
            }
        }

    executor, _recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="tp2", name="get_gti_threat_profile", arguments={"profile_id": "tp-1"})
    )
    assert not result.is_error
    assert "Spanish threat activity" in result.content
    assert "Targeted Region: ES" in result.content
    assert "Financial Gain" in result.content
    assert "max_recs_per_type=10" in result.content
    assert "editor" in result.content


@pytest.mark.asyncio
async def test_profile_recommendations_label_sources(monkeypatch):
    def router(method, url):
        return {
            "data": [
                {
                    "id": "threat-actor--abc",
                    "type": "collection",
                    "attributes": {
                        "name": "APT99",
                        "collection_type": "threat-actor",
                    },
                    "context_attributes": {"source": "SOURCE_RECOMMENDATION"},
                },
                {
                    "id": "malware--def",
                    "type": "collection",
                    "attributes": {
                        "name": "EMOTET",
                        "collection_type": "malware-family",
                    },
                    "context_attributes": {"source": "SOURCE_DIRECT_FOLLOW"},
                },
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="tp3",
            name="get_gti_threat_profile_recommendations",
            arguments={"profile_id": "tp-1", "filter": "collection_type:threat-actor"},
        )
    )
    assert not result.is_error
    assert "<- recommended" in result.content
    assert "<- added by user" in result.content
    call = recorder.calls[0]
    assert call["url"] == f"{GTI_API}/threat_profiles/tp-1/recommendations"
    assert call["params"]["filter"] == "collection_type:threat-actor"


@pytest.mark.asyncio
async def test_manage_profile_create_builds_body(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"data": {"id": "tp-9", "type": "threat_profile"}}
    )
    result = await executor.execute_tool(
        ToolCall(
            id="tp4",
            name="manage_gti_threat_profile",
            arguments={
                "action": "create",
                "name": "FR Gov threats",
                "interests": (
                    '{"INTEREST_TYPE_TARGETED_REGION": ["FR"], '
                    '"INTEREST_TYPE_TARGETED_INDUSTRY": ["Government"]}'
                ),
            },
        )
    )
    assert not result.is_error
    assert "tp-9" in result.content
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"] == f"{GTI_API}/threat_profiles"
    attrs = call["json"]["data"]["attributes"]
    assert attrs["name"] == "FR Gov threats"
    assert attrs["interests"]["INTEREST_TYPE_TARGETED_REGION"] == ["FR"]


@pytest.mark.asyncio
async def test_manage_profile_rejects_bad_interests(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="tp5",
            name="manage_gti_threat_profile",
            arguments={"action": "create", "name": "x", "interests": "not json"},
        )
    )
    assert "interests must be a json object" in result.content.lower()
    assert recorder.calls == []
    result = await executor.execute_tool(
        ToolCall(
            id="tp6",
            name="manage_gti_threat_profile",
            arguments={
                "action": "create",
                "name": "x",
                "interests": '{"INTEREST_TYPE_FAVOURITE_COLOUR": ["red"]}',
            },
        )
    )
    assert "unknown interest type" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_profile_add_objects_uses_relationships_endpoint(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"data": {}})
    result = await executor.execute_tool(
        ToolCall(
            id="tp7",
            name="manage_gti_threat_profile",
            arguments={
                "action": "add_objects",
                "profile_id": "tp-1",
                "object_ids": "threat-actor--abc, malware--def",
            },
        )
    )
    assert not result.is_error
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"] == f"{GTI_API}/threat_profiles/tp-1/relationships/recommendations"
    assert call["json"]["data"] == [
        {"type": "collection", "id": "threat-actor--abc"},
        {"type": "collection", "id": "malware--def"},
    ]


@pytest.mark.asyncio
async def test_manage_profile_delete(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"data": {}})
    result = await executor.execute_tool(
        ToolCall(
            id="tp8",
            name="manage_gti_threat_profile",
            arguments={"action": "delete", "profile_id": "tp-1"},
        )
    )
    assert not result.is_error
    assert "permanently" in result.content
    call = recorder.calls[0]
    assert call["method"] == "delete"
    assert call["url"] == f"{GTI_API}/threat_profiles/tp-1"


@pytest.mark.asyncio
async def test_manage_profile_rejects_bad_action(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="tp9",
            name="manage_gti_threat_profile",
            arguments={"action": "merge", "profile_id": "tp-1"},
        )
    )
    assert "action must be one of" in result.content.lower()
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Digital Threat Monitoring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dtm_alerts_filters_and_repeatable_params(monkeypatch):
    def router(method, url):
        return {
            "alerts": [
                {
                    "id": "al-1",
                    "title": "Credentials for acme.com on paste site",
                    "status": "new",
                    "alert_severity": "high",
                    "alert_type": "Compromised Credentials",
                    "created_at": "2026-07-06T10:00:00Z",
                    "monitor_name": "acme creds",
                }
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="d1",
            name="list_gti_dtm_alerts",
            arguments={"status": "new, escalated", "severity": "high", "size": 5},
        )
    )
    assert not result.is_error
    assert "acme.com" in result.content
    assert "al-1" in result.content
    call = recorder.calls[0]
    assert call["url"] == f"{GTI_DTM_API}/alerts"
    # Repeatable filters are passed as a param sequence, one pair per value.
    params = call["params"]
    assert ("status", "new") in params
    assert ("status", "escalated") in params
    assert ("severity", "high") in params
    assert ("size", 5) in params


@pytest.mark.asyncio
async def test_dtm_alerts_rejects_bad_status(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="d2", name="list_gti_dtm_alerts", arguments={"status": "snoozed"})
    )
    assert "status must be from" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_dtm_alerts_page_token_used_alone(monkeypatch):
    """The DTM API forbids combining the opaque page token with any other
    query parameter - the tool must drop everything else."""
    executor, recorder = _build(monkeypatch, lambda m, u: {"alerts": []})
    await executor.execute_tool(
        ToolCall(
            id="d3",
            name="list_gti_dtm_alerts",
            arguments={"status": "new", "size": 5, "page": "tok-42"},
        )
    )
    assert recorder.calls[0]["params"] == [("page", "tok-42")]


@pytest.mark.asyncio
async def test_update_dtm_alert_builds_patch(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"id": "al-1"})
    result = await executor.execute_tool(
        ToolCall(
            id="d4",
            name="update_gti_dtm_alert",
            arguments={"alert_id": "al-1", "status": "closed", "tags": "triaged, fp"},
        )
    )
    assert not result.is_error
    call = recorder.calls[0]
    assert call["method"] == "patch"
    assert call["url"] == f"{GTI_DTM_API}/alerts/al-1"
    assert call["json"] == {"status": "closed", "tags": ["triaged", "fp"]}


@pytest.mark.asyncio
async def test_update_dtm_alert_rejects_bad_status(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="d5",
            name="update_gti_dtm_alert",
            arguments={"alert_id": "al-1", "status": "snoozed"},
        )
    )
    assert "status must be one of" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_dtm_docs_search_posts_lucene_query(monkeypatch):
    def router(method, url):
        return {
            "docs": [
                {
                    "__id": "doc-1",
                    "__type": "forum_post",
                    "title": "selling acme creds",
                    "body": "fresh dump",
                    "timestamp": "2026-07-05T00:00:00Z",
                }
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="d6",
            name="search_gti_dtm_docs",
            arguments={"query": "acme.com AND password", "doc_types": "forum_post"},
        )
    )
    assert not result.is_error
    assert "selling acme creds" in result.content
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"] == f"{GTI_DTM_API}/docs/search"
    assert call["json"] == {"query": "acme.com AND password"}
    assert ("doc_type", "forum_post") in call["params"]


@pytest.mark.asyncio
async def test_manage_dtm_monitor_create_requires_condition(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="d7",
            name="manage_gti_dtm_monitor",
            arguments={"action": "create", "name": "acme creds"},
        )
    )
    assert "doc_condition" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_get_dtm_alert_requests_doc_refs(monkeypatch):
    """The single-alert read must request refs=true (the matched-doc excerpt
    is only returned with refs) plus sanitize + a server-side truncate."""

    def router(method, url):
        return {
            "id": "al-1",
            "title": "Credentials for acme.com",
            "status": "new",
            "monitor_id": "mon-1",
            "doc": {"body": "user:pass dump for acme.com"},
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="d11", name="get_gti_dtm_alert", arguments={"alert_id": "al-1"})
    )
    assert not result.is_error
    assert "user:pass dump" in result.content
    call = recorder.calls[0]
    assert call["url"] == f"{GTI_DTM_API}/alerts/al-1"
    assert call["params"]["refs"] == "true"
    assert call["params"]["sanitize"] == "true"
    assert call["params"]["truncate"] == 800


@pytest.mark.asyncio
async def test_manage_dtm_monitor_create_builds_body(monkeypatch):
    condition = (
        '{"operator": "all", "topic": "match_conditions", "match": '
        '[{"operator": "must_equal", "topic": "credential_domain", '
        '"match": ["acme.com"]}]}'
    )
    executor, recorder = _build(monkeypatch, lambda m, u: {"id": "mon-1"})
    result = await executor.execute_tool(
        ToolCall(
            id="d8",
            name="manage_gti_dtm_monitor",
            arguments={"action": "create", "name": "acme creds", "doc_condition": condition},
        )
    )
    assert not result.is_error
    assert "mon-1" in result.content
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"] == f"{GTI_DTM_API}/monitors"
    assert call["json"]["name"] == "acme creds"
    assert call["json"]["enabled"] is True
    assert call["json"]["doc_condition"]["topic"] == "match_conditions"


@pytest.mark.asyncio
async def test_manage_dtm_monitor_enable_disable_delete(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"id": "mon-1"})
    await executor.execute_tool(
        ToolCall(
            id="d9",
            name="manage_gti_dtm_monitor",
            arguments={"action": "disable", "monitor_id": "mon-1"},
        )
    )
    assert recorder.calls[0]["method"] == "patch"
    assert recorder.calls[0]["json"] == {"enabled": False}
    result = await executor.execute_tool(
        ToolCall(
            id="d10",
            name="manage_gti_dtm_monitor",
            arguments={"action": "delete", "monitor_id": "mon-1"},
        )
    )
    assert "permanently" in result.content
    assert recorder.calls[1]["method"] == "delete"
    assert recorder.calls[1]["url"] == f"{GTI_DTM_API}/monitors/mon-1"


# ---------------------------------------------------------------------------
# Attack Surface Management
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_asm_projects_lists_ids(monkeypatch):
    def router(method, url):
        return {
            "success": True,
            "result": [
                {
                    "id": 6797,
                    "name": "Acme Attack Surface",
                    "role": "owner",
                    "organization_name": "Acme",
                }
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="a1", name="list_gti_asm_projects", arguments={})
    )
    assert not result.is_error
    assert "Acme Attack Surface" in result.content
    assert "6797" in result.content
    assert recorder.calls[0]["url"] == f"{GTI_ASM_API}/projects"


@pytest.mark.asyncio
async def test_asm_entity_search_scopes_project_header(monkeypatch):
    def router(method, url):
        return {
            "success": True,
            "result": {
                "hits": [
                    {
                        "uid": "ent-1",
                        "name": "vpn.acme.com",
                        "type": "Domain",
                        "issue_count": 3,
                    }
                ],
                "next_page_token": "npt-1",
            },
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="a2",
            name="search_gti_asm_entities",
            arguments={"search_string": "type:Domain name:acme", "project_id": "6797"},
        )
    )
    assert not result.is_error
    assert "vpn.acme.com" in result.content
    assert "3 issue(s)" in result.content
    assert 'page_token="npt-1"' in result.content
    call = recorder.calls[0]
    assert call["url"].startswith(f"{GTI_ASM_API}/search/entities/")
    assert call["headers"]["PROJECT-ID"] == "6797"
    # Fetch the next page: page_token rides ALONGSIDE page_size (only the
    # DTM-style "page" key is exclusive), so the page size stays stable.
    await executor.execute_tool(
        ToolCall(
            id="a2b",
            name="search_gti_asm_entities",
            arguments={
                "search_string": "type:Domain name:acme",
                "project_id": "6797",
                "page_size": 10,
                "page_token": "npt-1",
            },
        )
    )
    assert recorder.calls[1]["params"] == {"page_size": 10, "page_token": "npt-1"}


@pytest.mark.asyncio
async def test_asm_search_rejects_non_numeric_project(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="a3",
            name="search_gti_asm_entities",
            arguments={"search_string": "x", "project_id": "not-a-number"},
        )
    )
    assert "project_id must be the numeric id" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_asm_scoped_tools_require_project_id(monkeypatch):
    # Every ASM endpoint except GET /projects is scoped by the required
    # PROJECT-ID header - an omitted project_id must fail fast with guidance
    # instead of sending an unscoped request.
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    for call in (
        ToolCall(
            id="p1", name="search_gti_asm_entities", arguments={"search_string": "x"}
        ),
        ToolCall(
            id="p2", name="get_gti_asm_entity", arguments={"entity_uid": "ent-1"}
        ),
        ToolCall(
            id="p3",
            name="set_gti_asm_issue_status",
            arguments={"issue_id": "iss-1", "status": "closed_resolved"},
        ),
    ):
        result = await executor.execute_tool(call)
        assert "project_id is required" in result.content.lower()
        assert "list_gti_asm_projects" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_asm_issue_search_and_detail(monkeypatch):
    def router(method, url):
        if "/search/issues/" in url:
            return {
                "result": {
                    "hits": [
                        {
                            "uid": "iss-1",
                            "pretty_name": "Exposed RDP",
                            "severity": 1,
                            "status_detailed": "open_new",
                            "entity_name": "vpn.acme.com",
                        }
                    ]
                }
            }
        return {
            "result": {
                "uid": "iss-1",
                "pretty_name": "Exposed RDP",
                "summary": "RDP service reachable from the internet.",
                "severity": 1,
                "status_detailed": "open_new",
                "entity_name": "vpn.acme.com",
            }
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="a4",
            name="search_gti_asm_issues",
            arguments={
                "search_string": "severity:1 status_new:open",
                "project_id": "6797",
            },
        )
    )
    assert not result.is_error
    assert "Exposed RDP" in result.content
    assert "severity 1/5" in result.content
    result = await executor.execute_tool(
        ToolCall(
            id="a5",
            name="get_gti_asm_issue",
            arguments={"issue_id": "iss-1", "project_id": "6797"},
        )
    )
    assert not result.is_error
    assert "RDP service reachable" in result.content
    assert recorder.calls[1]["url"] == f"{GTI_ASM_API}/issues/iss-1"
    assert recorder.calls[1]["headers"]["PROJECT-ID"] == "6797"


@pytest.mark.asyncio
async def test_asm_set_issue_status_validates_enum(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"success": True})
    result = await executor.execute_tool(
        ToolCall(
            id="a6",
            name="set_gti_asm_issue_status",
            arguments={"issue_id": "iss-1", "status": "wontfix", "project_id": "6797"},
        )
    )
    assert "status must be one of" in result.content.lower()
    assert recorder.calls == []
    result = await executor.execute_tool(
        ToolCall(
            id="a7",
            name="set_gti_asm_issue_status",
            arguments={
                "issue_id": "iss-1",
                "status": "closed_risk_accepted",
                "project_id": "6797",
            },
        )
    )
    assert not result.is_error
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"] == f"{GTI_ASM_API}/issues/iss-1/status"
    assert call["json"] == {"status": "closed_risk_accepted"}
    # The write is scoped to the project like every other ASM call.
    assert call["headers"]["PROJECT-ID"] == "6797"


@pytest.mark.asyncio
async def test_asm_technologies_search(monkeypatch):
    def router(method, url):
        return {
            "result": [
                {
                    "uid": "tech-1",
                    "name": "apache_httpd",
                    "vendor": "apache",
                    "product": "http_server",
                }
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="a8",
            name="search_gti_asm_technologies",
            arguments={"search_string": "vendor:apache", "project_id": "6797"},
        )
    )
    assert not result.is_error
    assert "apache_httpd" in result.content
    assert recorder.calls[0]["url"].startswith(f"{GTI_ASM_API}/search/technologies/")
