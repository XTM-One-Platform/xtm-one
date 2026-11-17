"""Tests for the abuse.ch integration provider.

Covers tool introspection (including the catalog dummy token), the
``Auth-Key`` header helper, the in-body ``query_status`` envelope handling
(ok/success vs benign misses vs auth errors), the MalwareBazaar hash lookup /
recent / consolidated search tools, the URLhaus URL / host / payload /
recent / search tools, the ThreatFox IOC search / detail / recent / label /
reference / submission tools, and the call-time credential guard. Every API
call routes through the shared ``safe_request`` helper, which is
monkeypatched here so no network is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import abusech as ab
from app.integrations.providers.abusech import (
    MALWAREBAZAAR_API,
    THREATFOX_API,
    URLHAUS_API,
    AbuseChIntegration,
    build_abusech_headers,
    build_tools,
)
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "abusech-test-auth-key"

_SHA256 = "094fd325049b8a9cf6d3e5ef2a6d4cc6a567d7d49c35f8bb8dd9e3c6acf3d78d"
_MD5 = "7338b335ad5471cb67658f27836374f0"
_SHA1 = "eb0e81598d8526d88cac4695a3e9360cc8fbb331"

_ALL_TOOLS = {
    "lookup_abusech_malware_hash",
    "list_abusech_recent_malware",
    "search_abusech_malware",
    "comment_abusech_sample",
    "lookup_abusech_url",
    "lookup_abusech_host",
    "lookup_abusech_payload",
    "list_abusech_recent_urls",
    "list_abusech_recent_payloads",
    "search_abusech_urls",
    "search_abusech_iocs",
    "get_abusech_ioc",
    "list_abusech_recent_iocs",
    "resolve_abusech_malware_label",
    "list_abusech_threatfox_reference",
    "submit_abusech_ioc",
}


class _StubResponse:
    def __init__(self, payload: Any):
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, ValueError):
            raise self._payload
        return self._payload


class _Recorder:
    """Fake ``safe_request`` that routes by (method, url) and records calls."""

    def __init__(self, router):
        self._router = router
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        return _StubResponse(self._router(method, url, kwargs)), None


def _build(monkeypatch, router) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(router)
    monkeypatch.setattr(ab, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Introspection + helpers
# ---------------------------------------------------------------------------


def test_build_tools_returns_definitions_with_catalog_dummy():
    # The catalog introspects providers with a dummy token; the tools must
    # still build (credential validation happens at call time).
    executor = CallableToolExecutor()
    tools = build_tools(token="__dummy__", executor=executor)
    assert {t.name for t in tools} == _ALL_TOOLS
    assert all(t.source == "integration:abusech" for t in tools)


def test_tool_priorities_sort_reads_before_writes():
    executor = CallableToolExecutor()
    tools = {t.name: t for t in build_tools(token=_TOKEN, executor=executor)}
    assert tools["lookup_abusech_malware_hash"].priority == 0
    assert tools["search_abusech_iocs"].priority == 0
    assert tools["list_abusech_recent_urls"].priority == 0
    assert tools["comment_abusech_sample"].priority == 1
    assert tools["submit_abusech_ioc"].priority == 1


def test_headers_helper_strips_and_sets_accept():
    headers = build_abusech_headers("  key-123 \n")
    assert headers["Auth-Key"] == "key-123"
    assert headers["Accept"] == "application/json"


def test_provider_class_metadata():
    integ = AbuseChIntegration()
    assert integ.provider_name == "abusech"
    assert integ.display_name == "abuse.ch"
    assert integ.auth_types == ["api_key"]
    assert integ.get_oauth_defaults() is None


@pytest.mark.asyncio
async def test_tools_without_credentials_return_error(monkeypatch):
    recorder = _Recorder(lambda m, u, k: {})
    monkeypatch.setattr(ab, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token="   ", executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="c0",
            name="lookup_abusech_malware_hash",
            arguments={"hash": _SHA256},
        )
    )
    assert "credentials are not configured" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_validate_connection_blank_key_short_circuits():
    assert await AbuseChIntegration().validate_connection("   ") is False


# ---------------------------------------------------------------------------
# In-body query_status envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_body_status_not_found_is_benign(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u, k: {"query_status": "hash_not_found"}
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c1", name="lookup_abusech_malware_hash", arguments={"hash": _SHA256}
        )
    )
    assert not result.is_error
    assert "not known to MalwareBazaar" in result.content
    assert len(recorder.calls) == 1


@pytest.mark.asyncio
async def test_body_status_no_api_key_surfaces_auth_guidance(monkeypatch):
    executor, _ = _build(monkeypatch, lambda m, u, k: {"query_status": "no_api_key"})
    result = await executor.execute_tool(
        ToolCall(
            id="c2", name="lookup_abusech_malware_hash", arguments={"hash": _SHA256}
        )
    )
    assert "Auth-Key" in result.content
    assert "auth.abuse.ch" in result.content


@pytest.mark.asyncio
async def test_non_json_response_returns_readable_error(monkeypatch):
    # safe_request only validates the HTTP status - an HTML/empty body makes
    # resp.json() raise. Every parse path must map that to a readable error
    # instead of crashing the tool.
    executor, _ = _build(
        monkeypatch, lambda m, u, k: ValueError("Expecting value: line 1")
    )
    for call in (
        ToolCall(
            id="j1", name="lookup_abusech_malware_hash", arguments={"hash": _SHA256}
        ),
        ToolCall(
            id="j2",
            name="lookup_abusech_url",
            arguments={"url": "http://evil.example.com/x"},
        ),
        ToolCall(
            id="j3",
            name="search_abusech_iocs",
            arguments={"search_by": "term", "value": "evil"},
        ),
        ToolCall(id="j4", name="list_abusech_recent_urls", arguments={}),
        ToolCall(id="j5", name="list_abusech_recent_payloads", arguments={}),
    ):
        result = await executor.execute_tool(call)
        assert "non-JSON response" in result.content, call.name


@pytest.mark.asyncio
async def test_unexpected_data_shape_degrades_to_empty_result(monkeypatch):
    # query_status ok but data is an object/string instead of the expected
    # list - tools must degrade to their empty-result message instead of
    # raising through slicing/indexing.
    executor, _ = _build(
        monkeypatch,
        lambda m, u, k: {"query_status": "ok", "data": {"unexpected": "shape"}},
    )
    for call, expected in (
        (
            ToolCall(
                id="s1",
                name="lookup_abusech_malware_hash",
                arguments={"hash": _SHA256},
            ),
            "not known to MalwareBazaar",
        ),
        (
            ToolCall(id="s2", name="list_abusech_recent_malware", arguments={}),
            "No malware samples",
        ),
        (
            ToolCall(
                id="s3",
                name="search_abusech_malware",
                arguments={"search_by": "tag", "value": "TrickBot"},
            ),
            "No MalwareBazaar samples found",
        ),
    ):
        result = await executor.execute_tool(call)
        assert not result.is_error, call.name
        assert expected in result.content, call.name


@pytest.mark.asyncio
async def test_body_status_unknown_error_is_reported(monkeypatch):
    executor, _ = _build(
        monkeypatch, lambda m, u, k: {"query_status": "illegal_hash"}
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c3", name="lookup_abusech_malware_hash", arguments={"hash": _SHA256}
        )
    )
    assert "Error:" in result.content
    assert "illegal_hash" in result.content


# ---------------------------------------------------------------------------
# MalwareBazaar
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_malware_hash_formats_dossier(monkeypatch):
    def router(method, url, kwargs):
        return {
            "query_status": "ok",
            "data": [
                {
                    "sha256_hash": _SHA256,
                    "sha1_hash": _SHA1,
                    "md5_hash": _MD5,
                    "file_name": "invoice.exe",
                    "file_size": 62118,
                    "file_type": "exe",
                    "file_type_mime": "application/x-dosexec",
                    "first_seen": "2026-07-01 05:57:01",
                    "last_seen": None,
                    "reporter": "abuse_ch",
                    "origin_country": "CH",
                    "signature": "AgentTesla",
                    "imphash": "f34d5f2d4577ed6d9ceec516c1f5a744",
                    "tags": ["AgentTesla", "exe"],
                    "code_sign": [
                        {
                            "subject_cn": "Ekitai Data Inc.",
                            "issuer_cn": "Sectigo RSA Code Signing CA",
                            "serial_number": "B8164F7143E1A313003AB0C834562F1F",
                            "cscb_listed": True,
                            "cscb_reason": "TA505",
                        }
                    ],
                    "yara_rules": [{"rule_name": "win_agent_tesla_g2"}],
                    "vendor_intel": {"ANY.RUN": [], "Triage": {}},
                    "intelligence": {"clamav": "Win.Trojan.Generic-6332612-0"},
                }
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c4", name="lookup_abusech_malware_hash", arguments={"hash": _SHA256}
        )
    )
    assert not result.is_error
    assert "AgentTesla" in result.content
    assert "CSCB BLOCKLISTED (TA505)" in result.content
    assert "win_agent_tesla_g2" in result.content
    assert f"https://bazaar.abuse.ch/sample/{_SHA256}/" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"] == MALWAREBAZAAR_API
    assert call["data"] == {"query": "get_info", "hash": _SHA256}
    assert call["headers"]["Auth-Key"] == _TOKEN


@pytest.mark.asyncio
async def test_lookup_malware_hash_rejects_invalid_hash(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u, k: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c5",
            name="lookup_abusech_malware_hash",
            arguments={"hash": "not-a-hash"},
        )
    )
    assert "not a valid SHA256, SHA1 or MD5 hash" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_list_recent_malware_selectors(monkeypatch):
    def router(method, url, kwargs):
        return {"query_status": "ok", "data": [{"sha256_hash": _SHA256}]}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="c6", name="list_abusech_recent_malware", arguments={})
    )
    assert not result.is_error
    assert recorder.calls[-1]["data"] == {"query": "get_recent", "selector": "time"}

    result = await executor.execute_tool(
        ToolCall(
            id="c7",
            name="list_abusech_recent_malware",
            arguments={"time_window": False},
        )
    )
    assert not result.is_error
    assert recorder.calls[-1]["data"] == {"query": "get_recent", "selector": "100"}


@pytest.mark.asyncio
async def test_search_malware_selector_mapping_and_limits(monkeypatch):
    def router(method, url, kwargs):
        return {"query_status": "ok", "data": [{"sha256_hash": _SHA256}]}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c8",
            name="search_abusech_malware",
            arguments={"search_by": "tag", "value": "TrickBot", "limit": 5000},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    # limit is clamped to the API max of 1000.
    assert call["data"] == {"query": "get_taginfo", "tag": "TrickBot", "limit": 1000}

    # Certificate selectors don't support limit.
    await executor.execute_tool(
        ToolCall(
            id="c9",
            name="search_abusech_malware",
            arguments={"search_by": "issuer_cn", "value": "Sectigo RSA Code Signing CA"},
        )
    )
    call = recorder.calls[-1]
    assert call["data"] == {
        "query": "get_issuerinfo",
        "issuer_cn": "Sectigo RSA Code Signing CA",
    }

    bad = await executor.execute_tool(
        ToolCall(
            id="c10",
            name="search_abusech_malware",
            arguments={"search_by": "bogus", "value": "x"},
        )
    )
    assert "not a valid search_by value" in bad.content


@pytest.mark.asyncio
async def test_comment_sample_requires_sha256(monkeypatch):
    def router(method, url, kwargs):
        return {"query_status": "success"}

    executor, recorder = _build(monkeypatch, router)
    bad = await executor.execute_tool(
        ToolCall(
            id="c11",
            name="comment_abusech_sample",
            arguments={"sha256_hash": _MD5, "comment": "hi"},
        )
    )
    assert "not a valid SHA256" in bad.content
    assert recorder.calls == []

    blank = await executor.execute_tool(
        ToolCall(
            id="c11b",
            name="comment_abusech_sample",
            arguments={"sha256_hash": "", "comment": "hi"},
        )
    )
    assert "sha256_hash is required" in blank.content
    assert recorder.calls == []

    ok = await executor.execute_tool(
        ToolCall(
            id="c12",
            name="comment_abusech_sample",
            arguments={"sha256_hash": _SHA256, "comment": "C2 extracted: 1.2.3.4"},
        )
    )
    assert not ok.is_error
    assert "Comment published" in ok.content
    call = recorder.calls[-1]
    assert call["data"]["query"] == "add_comment"
    assert call["data"]["sha256_hash"] == _SHA256


# ---------------------------------------------------------------------------
# URLhaus
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_url_formats_entry_with_payloads(monkeypatch):
    def router(method, url, kwargs):
        return {
            "query_status": "ok",
            "id": "105821",
            "url": "http://evil.example.com/payload.exe",
            "url_status": "online",
            "host": "evil.example.com",
            "date_added": "2026-07-01 01:33:26 UTC",
            "threat": "malware_download",
            "blacklists": {"spamhaus_dbl": "abused_legit_malware", "surbl": "listed"},
            "reporter": "Cryptolaemus1",
            "larted": "true",
            "tags": ["emotet"],
            "payloads": [
                {
                    "firstseen": "2026-07-01",
                    "filename": "payload.exe",
                    "file_type": "exe",
                    "response_sha256": _SHA256,
                    "signature": "Heodo",
                }
            ],
            "urlhaus_reference": "https://urlhaus.abuse.ch/url/105821/",
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c13",
            name="lookup_abusech_url",
            arguments={"url": "http://evil.example.com/payload.exe"},
        )
    )
    assert not result.is_error
    assert "online" in result.content
    assert "spamhaus_dbl: abused_legit_malware" in result.content
    assert "Heodo" in result.content
    call = recorder.calls[-1]
    assert call["url"] == f"{URLHAUS_API}/url/"
    assert call["data"] == {"url": "http://evil.example.com/payload.exe"}


@pytest.mark.asyncio
async def test_lookup_host_formats_history(monkeypatch):
    def router(method, url, kwargs):
        return {
            "query_status": "ok",
            "host": "vektorex.com",
            "firstseen": "2026-01-15 07:09:01 UTC",
            "url_count": "120",
            "blacklists": {"spamhaus_dbl": "abused_legit_malware", "surbl": "not listed"},
            "urls": [
                {
                    "id": "121319",
                    "url": "http://vektorex.com/source/Z/5016223.exe",
                    "url_status": "online",
                    "date_added": "2026-02-11 07:45:05 UTC",
                    "threat": "malware_download",
                    "tags": ["AZORult", "exe"],
                }
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="c14", name="lookup_abusech_host", arguments={"host": "vektorex.com"})
    )
    assert not result.is_error
    assert "120" in result.content
    assert "AZORult" in result.content
    assert recorder.calls[-1]["url"] == f"{URLHAUS_API}/host/"
    assert recorder.calls[-1]["data"] == {"host": "vektorex.com"}


@pytest.mark.asyncio
async def test_lookup_payload_selects_hash_param_and_rejects_sha1(monkeypatch):
    def router(method, url, kwargs):
        return {
            "query_status": "ok",
            "sha256_hash": _SHA256,
            "md5_hash": _MD5,
            "file_type": "exe",
            "file_size": "241664",
            "signature": "Heodo",
            "firstseen": "2026-07-01 13:59:06",
            "url_count": "3",
            "virustotal": {"result": "17 / 69", "percent": "24.64"},
            "urls": [],
        }

    executor, recorder = _build(monkeypatch, router)
    ok = await executor.execute_tool(
        ToolCall(id="c15", name="lookup_abusech_payload", arguments={"hash": _MD5})
    )
    assert not ok.is_error
    assert recorder.calls[-1]["data"] == {"md5_hash": _MD5}

    ok = await executor.execute_tool(
        ToolCall(id="c16", name="lookup_abusech_payload", arguments={"hash": _SHA256})
    )
    assert not ok.is_error
    assert recorder.calls[-1]["data"] == {"sha256_hash": _SHA256}

    # URLhaus payload lookups accept MD5/SHA256 only - SHA1 short-circuits.
    bad = await executor.execute_tool(
        ToolCall(id="c17", name="lookup_abusech_payload", arguments={"hash": _SHA1})
    )
    assert "MD5 or SHA256" in bad.content

    # A blank hash short-circuits with an explicit required-parameter error
    # (not the confusing "'' is not a valid ... hash" message).
    calls_before = len(recorder.calls)
    blank = await executor.execute_tool(
        ToolCall(id="c17b", name="lookup_abusech_payload", arguments={"hash": "  "})
    )
    assert "hash is required" in blank.content
    assert len(recorder.calls) == calls_before


@pytest.mark.asyncio
async def test_recent_urls_and_payloads_use_get_with_limit_path(monkeypatch):
    def router(method, url, kwargs):
        if "/urls/recent/" in url:
            return {
                "query_status": "ok",
                "urls": [
                    {
                        "id": "223622",
                        "url": "http://45.61.49.78/razor/r4z0r.mips",
                        "url_status": "offline",
                        "date_added": "2026-07-09 09:02:05 UTC",
                        "threat": "malware_download",
                        "tags": ["elf", "mirai"],
                    }
                ],
            }
        return {
            "query_status": "ok",
            "payloads": [
                {
                    "sha256_hash": _SHA256,
                    "file_type": "exe",
                    "file_size": "656896",
                    "signature": None,
                    "firstseen": "2026-07-09 11:09:23",
                    "virustotal": {"percent": "24.14"},
                }
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    urls = await executor.execute_tool(
        ToolCall(id="c18", name="list_abusech_recent_urls", arguments={"limit": 3})
    )
    assert not urls.is_error
    assert "mirai" in urls.content
    call = recorder.calls[-1]
    assert call["method"] == "get"
    assert call["url"] == f"{URLHAUS_API}/urls/recent/limit/3/"

    payloads = await executor.execute_tool(
        ToolCall(id="c19", name="list_abusech_recent_payloads", arguments={})
    )
    assert not payloads.is_error
    assert _SHA256 in payloads.content
    assert recorder.calls[-1]["url"] == f"{URLHAUS_API}/payloads/recent/limit/25/"


@pytest.mark.asyncio
async def test_search_urls_by_tag_and_signature(monkeypatch):
    def router(method, url, kwargs):
        return {
            "query_status": "ok",
            "firstseen": "2026-03-06 15:27:00",
            "lastseen": "2026-07-09 06:00:11",
            "url_count": "265",
            "urls": [
                {
                    "url_id": "98627",
                    "url": "https://bad.example.com/sup.exe",
                    "url_status": "offline",
                    "dateadded": "2026-07-08 06:00:11",
                    "threat": "malware_download",
                }
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c20",
            name="search_abusech_urls",
            arguments={"search_by": "signature", "value": "Gozi"},
        )
    )
    assert not result.is_error
    assert "265" in result.content
    assert recorder.calls[-1]["url"] == f"{URLHAUS_API}/signature/"
    assert recorder.calls[-1]["data"] == {"signature": "Gozi"}

    bad = await executor.execute_tool(
        ToolCall(
            id="c21",
            name="search_abusech_urls",
            arguments={"search_by": "host", "value": "x"},
        )
    )
    assert "must be 'tag' or 'signature'" in bad.content


@pytest.mark.asyncio
async def test_truncation_hint_reflects_display_cap(monkeypatch):
    # 150 results with limit=10 -> "raise limit"; with limit>=100 (the display
    # cap) raising limit cannot reveal more rows, so the hint says so instead.
    def router(method, url, kwargs):
        return {
            "query_status": "ok",
            "data": [{"sha256_hash": _SHA256} for _ in range(150)],
        }

    executor, _ = _build(monkeypatch, router)
    low = await executor.execute_tool(
        ToolCall(
            id="c37",
            name="search_abusech_malware",
            arguments={"search_by": "tag", "value": "TrickBot", "limit": 10},
        )
    )
    assert "raise limit to see more" in low.content

    high = await executor.execute_tool(
        ToolCall(
            id="c38",
            name="search_abusech_malware",
            arguments={"search_by": "tag", "value": "TrickBot", "limit": 1000},
        )
    )
    assert "raise limit" not in high.content
    assert "capped at 100 rows" in high.content


# ---------------------------------------------------------------------------
# ThreatFox
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_iocs_modes_build_correct_json_bodies(monkeypatch):
    def router(method, url, kwargs):
        return {
            "query_status": "ok",
            "data": [
                {
                    "id": "12",
                    "ioc": "139.180.203.104:443",
                    "threat_type": "botnet_cc",
                    "ioc_type": "ip:port",
                    "malware_printable": "Cobalt Strike",
                    "confidence_level": 75,
                    "first_seen": "2026-07-06 09:10:23 UTC",
                }
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c22",
            name="search_abusech_iocs",
            arguments={
                "search_by": "term",
                "value": "139.180.203.104",
                "exact_match": True,
            },
        )
    )
    assert not result.is_error
    assert "Cobalt Strike" in result.content
    call = recorder.calls[-1]
    assert call["url"] == THREATFOX_API
    assert call["json"] == {
        "query": "search_ioc",
        "search_term": "139.180.203.104",
        "exact_match": True,
    }

    await executor.execute_tool(
        ToolCall(
            id="c23",
            name="search_abusech_iocs",
            arguments={"search_by": "hash", "value": _MD5},
        )
    )
    assert recorder.calls[-1]["json"] == {"query": "search_hash", "hash": _MD5}

    await executor.execute_tool(
        ToolCall(
            id="c24",
            name="search_abusech_iocs",
            arguments={"search_by": "tag", "value": "Magecart", "limit": 10},
        )
    )
    assert recorder.calls[-1]["json"] == {
        "query": "taginfo",
        "tag": "Magecart",
        "limit": 10,
    }

    await executor.execute_tool(
        ToolCall(
            id="c25",
            name="search_abusech_iocs",
            arguments={"search_by": "malware", "value": "Cobalt Strike"},
        )
    )
    assert recorder.calls[-1]["json"] == {
        "query": "malwareinfo",
        "malware": "Cobalt Strike",
        "limit": 25,
    }

    bad = await executor.execute_tool(
        ToolCall(
            id="c26",
            name="search_abusech_iocs",
            arguments={"search_by": "hash", "value": _SHA1},
        )
    )
    assert "MD5 or SHA256" in bad.content


@pytest.mark.asyncio
async def test_get_ioc_formats_detail_with_samples(monkeypatch):
    def router(method, url, kwargs):
        return {
            "query_status": "ok",
            "data": [
                {
                    "id": "41",
                    "ioc": "gaga.com",
                    "threat_type": "botnet_cc",
                    "threat_type_desc": "Indicator that identifies a botnet C&C",
                    "ioc_type": "domain",
                    "ioc_type_desc": "Domain used for botnet C&C",
                    "malware": "win.dridex",
                    "malware_printable": "Dridex",
                    "malware_alias": None,
                    "malware_malpedia": "https://malpedia.caad.fkie.fraunhofer.de/details/win.dridex",
                    "confidence_level": 50,
                    "first_seen": "2026-07-08 13:36:27 UTC",
                    "reporter": "abuse_ch",
                    "tags": ["exe"],
                    "malware_samples": [
                        {"time_stamp": "2026-07-08 08:18:06 UTC", "sha256_hash": _SHA256}
                    ],
                }
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="c27", name="get_abusech_ioc", arguments={"ioc_id": "#41"})
    )
    assert not result.is_error
    assert "gaga.com" in result.content
    assert "Dridex" in result.content
    assert _SHA256 in result.content
    assert recorder.calls[-1]["json"] == {"query": "ioc", "id": 41}

    bad = await executor.execute_tool(
        ToolCall(id="c28", name="get_abusech_ioc", arguments={"ioc_id": "abc"})
    )
    assert "numeric ThreatFox IOC id" in bad.content


@pytest.mark.asyncio
async def test_recent_iocs_clamps_days(monkeypatch):
    def router(method, url, kwargs):
        return {"query_status": "ok", "data": []}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="c29", name="list_abusech_recent_iocs", arguments={"days": 30})
    )
    assert not result.is_error
    # The API caps the window at 7 days.
    assert recorder.calls[-1]["json"] == {"query": "get_iocs", "days": 7}


@pytest.mark.asyncio
async def test_resolve_malware_label_and_platform_validation(monkeypatch):
    def router(method, url, kwargs):
        return {
            "query_status": "ok",
            "data": [
                {
                    "malware": "win.ave_maria",
                    "malware_printable": "Ave Maria",
                    "malware_alias": "AVE_MARIA,Warzone RAT",
                }
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c30",
            name="resolve_abusech_malware_label",
            arguments={"malware": "warzone", "platform": "win"},
        )
    )
    assert not result.is_error
    assert "win.ave_maria" in result.content
    assert recorder.calls[-1]["json"] == {
        "query": "get_label",
        "malware": "warzone",
        "platform": "win",
    }

    bad = await executor.execute_tool(
        ToolCall(
            id="c31",
            name="resolve_abusech_malware_label",
            arguments={"malware": "warzone", "platform": "amiga"},
        )
    )
    assert "platform must be one of" in bad.content


@pytest.mark.asyncio
async def test_threatfox_reference_filtering(monkeypatch):
    def router(method, url, kwargs):
        query = kwargs.get("json", {}).get("query")
        if query == "types":
            return {
                "query_status": "ok",
                "data": {
                    "1": {
                        "ioc_type": "url",
                        "fk_threat_type": "payload_delivery",
                        "description": "URL that delivers a malware payload",
                    },
                    "2": {
                        "ioc_type": "ip:port",
                        "fk_threat_type": "botnet_cc",
                        "description": "ip:port used for botnet C&C",
                    },
                },
            }
        return {"query_status": "ok", "data": {}}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c32",
            name="list_abusech_threatfox_reference",
            arguments={"reference": "threat_types", "filter": "botnet"},
        )
    )
    assert not result.is_error
    assert "botnet_cc" in result.content
    assert "payload_delivery" not in result.content
    assert recorder.calls[-1]["json"] == {"query": "types"}

    bad = await executor.execute_tool(
        ToolCall(
            id="c33",
            name="list_abusech_threatfox_reference",
            arguments={"reference": "bogus"},
        )
    )
    assert "reference must be one of" in bad.content


@pytest.mark.asyncio
async def test_submit_ioc_builds_body_and_reports_result(monkeypatch):
    def router(method, url, kwargs):
        return {
            "query_status": "ok",
            "data": {
                "ok": ["198.51.100.7:443"],
                "ignored": [],
                "duplicated": ["203.0.113.9:80"],
                "reward": 5,
            },
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c34",
            name="submit_abusech_ioc",
            arguments={
                "iocs": "198.51.100.7:443, 203.0.113.9:80",
                "threat_type": "botnet_cc",
                "ioc_type": "ip:port",
                "malware": "win.cobalt_strike",
                "confidence_level": 500,
                "tags": "CobaltStrike, exe",
                "reference": "https://example.com/report",
                "comment": "Beacon C2 extracted from sandbox run",
            },
        )
    )
    assert not result.is_error
    assert "1 accepted" in result.content
    assert "1 duplicate(s)" in result.content
    assert "Credits earned: 5" in result.content
    body = recorder.calls[-1]["json"]
    assert body["query"] == "submit_ioc"
    assert body["iocs"] == ["198.51.100.7:443", "203.0.113.9:80"]
    # confidence_level is clamped to the API maximum of 100.
    assert body["confidence_level"] == 100
    assert body["tags"] == ["CobaltStrike", "exe"]
    assert body["anonymous"] == 0


@pytest.mark.asyncio
async def test_submit_ioc_reports_zero_reward(monkeypatch):
    # reward=0 must still be reported (None means the field was absent).
    def router(method, url, kwargs):
        return {
            "query_status": "ok",
            "data": {"ok": [], "ignored": ["1.2.3.4:80"], "duplicated": [], "reward": 0},
        }

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c34b",
            name="submit_abusech_ioc",
            arguments={
                "iocs": "1.2.3.4:80",
                "threat_type": "botnet_cc",
                "ioc_type": "ip:port",
                "malware": "win.zloader",
            },
        )
    )
    assert not result.is_error
    assert "Credits earned: 0" in result.content


@pytest.mark.asyncio
async def test_submit_ioc_requires_all_taxonomy_fields(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u, k: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c35",
            name="submit_abusech_ioc",
            arguments={
                "iocs": "1.2.3.4:443",
                "threat_type": "botnet_cc",
                "ioc_type": "",
                "malware": "win.zloader",
            },
        )
    )
    assert "threat_type, ioc_type and malware are all required" in result.content
    assert recorder.calls == []

    result = await executor.execute_tool(
        ToolCall(
            id="c36",
            name="submit_abusech_ioc",
            arguments={
                "iocs": "  ",
                "threat_type": "botnet_cc",
                "ioc_type": "ip:port",
                "malware": "win.zloader",
            },
        )
    )
    assert "iocs is required" in result.content
    assert recorder.calls == []

    # A dotless malware name is not a canonical Malpedia label
    # (platform.family) - reject locally before any API call.
    result = await executor.execute_tool(
        ToolCall(
            id="c36b",
            name="submit_abusech_ioc",
            arguments={
                "iocs": "1.2.3.4:443",
                "threat_type": "botnet_cc",
                "ioc_type": "ip:port",
                "malware": "zloader",
            },
        )
    )
    assert "not a canonical Malpedia label" in result.content
    assert "resolve_abusech_malware_label" in result.content
    assert recorder.calls == []
