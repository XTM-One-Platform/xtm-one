"""Tests for the Vulnerability Intelligence integration provider.

Covers the dual-tier credential chokepoints (optional NVD API key vs keyless),
tool introspection with the catalog dummy token, the NVD newest-first paging
helper (count probe + tail page + flag params), the CISA KEV cache (hit/miss,
reuse, filters, due-date markers), EPSS single/batch/top-N lookups, the merged
assess tool (verdict ladder + partial-failure resilience), input validation,
and the connectivity-test fail-fast. Every API call routes through the shared
``safe_request`` helper, which is monkeypatched here so no network is touched;
the NVD rate limiter is replaced with a no-op so tests never sleep.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from app.integrations.providers import vuln_intel as vi
from app.integrations.providers.vuln_intel import (
    EPSS_BASE,
    KEV_FEED_URL,
    NVD_CVE_BASE,
    NVD_HISTORY_BASE,
    VulnIntelIntegration,
    build_tools,
    build_vulnintel_headers,
    resolve_nvd_key,
    vulnintel_status_url,
)
from app.tools.base import CallableToolExecutor, ToolCall

_API_KEY = "nvd-test-api-key"
_NO_AUTH = "__no_auth__"

_TOOL_NAMES = {
    "assess_vulnintel_cve",
    "get_vulnintel_cve",
    "search_vulnintel_cves",
    "list_vulnintel_recent_cves",
    "get_vulnintel_cve_history",
    "check_vulnintel_kev",
    "search_vulnintel_kev",
    "get_vulnintel_epss",
    "list_vulnintel_top_epss",
    "get_vulnintel_status",
}


class _StubResponse:
    def __init__(self, payload: Any, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.content = b"x"

    def json(self) -> Any:
        return self._payload


class _Recorder:
    """Fake ``safe_request`` that routes by URL and records calls."""

    def __init__(self, router):
        self._router = router
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        call = {"method": method, "url": url, "action": action, **kwargs}
        self.calls.append(call)
        return _StubResponse(self._router(url, call)), None


class _ErrorRecorder:
    """Fake ``safe_request`` failing every call with an error string."""

    def __init__(self, err: str):
        self._err = err
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        return None, self._err


class _NoopLimiter:
    """Rate limiter stand-in so tests never sleep on the NVD bucket."""

    @asynccontextmanager
    async def slot(self):
        yield


@pytest.fixture(autouse=True)
def _fresh_kev_cache(monkeypatch):
    """Reset the module-global KEV cache and neutralize the NVD limiter."""
    monkeypatch.setattr(vi, "_KEV_CACHE", {})
    monkeypatch.setattr(
        vi, "get_shared_rate_limiter", lambda *_a, **_k: _NoopLimiter()
    )


def _build(monkeypatch, router, token=_API_KEY) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(router)
    monkeypatch.setattr(vi, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=token, executor=executor)
    return executor, recorder


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query, keep_blank_values=True)


def _nvd_record(cve_id: str, **extra: Any) -> dict:
    record = {
        "id": cve_id,
        "published": "2026-07-01T10:00:00.000",
        "lastModified": "2026-07-05T10:00:00.000",
        "vulnStatus": "Analyzed",
        "descriptions": [{"lang": "en", "value": f"{cve_id} allows remote code execution."}],
        "metrics": {
            "cvssMetricV31": [
                {
                    "cvssData": {
                        "baseScore": 9.8,
                        "baseSeverity": "CRITICAL",
                        "vectorString": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
                    }
                }
            ]
        },
        "weaknesses": [{"description": [{"value": "CWE-502"}]}],
        "references": [{"url": "https://example.com/advisory", "tags": ["Vendor Advisory"]}],
    }
    record.update(extra)
    return record


def _nvd_envelope(records: list[dict], total: int | None = None) -> dict:
    return {
        "totalResults": total if total is not None else len(records),
        "vulnerabilities": [{"cve": r} for r in records],
    }


_KEV_PAYLOAD = {
    "title": "CISA Catalog of Known Exploited Vulnerabilities",
    "catalogVersion": "2026.07.07",
    "dateReleased": "2026-07-07T18:28:17.892Z",
    "count": 3,
    "vulnerabilities": [
        {
            "cveID": "CVE-2024-3400",
            "vendorProject": "Palo Alto Networks",
            "product": "PAN-OS",
            "vulnerabilityName": "PAN-OS Command Injection Vulnerability",
            "dateAdded": "2024-04-12",
            "shortDescription": "PAN-OS GlobalProtect command injection.",
            "requiredAction": "Apply mitigations per vendor instructions.",
            "dueDate": "2024-04-19",
            "knownRansomwareCampaignUse": "Unknown",
            "cwes": ["CWE-77"],
        },
        {
            "cveID": "CVE-2023-4966",
            "vendorProject": "Citrix",
            "product": "NetScaler ADC",
            "vulnerabilityName": "Citrix Bleed",
            "dateAdded": "2023-10-18",
            "shortDescription": "Session token leakage.",
            "requiredAction": "Apply updates per vendor instructions.",
            "dueDate": "2023-11-08",
            "knownRansomwareCampaignUse": "Known",
            "cwes": ["CWE-119"],
        },
        {
            "cveID": "CVE-2026-0001",
            "vendorProject": "Cisco",
            "product": "IOS XE",
            "vulnerabilityName": "Cisco IOS XE Something",
            "dateAdded": "2099-01-01",
            "shortDescription": "Freshly added entry.",
            "requiredAction": "Apply updates per vendor instructions.",
            "dueDate": "2099-01-22",
            "knownRansomwareCampaignUse": "Unknown",
        },
    ],
}


# ---------------------------------------------------------------------------
# Introspection + chokepoints
# ---------------------------------------------------------------------------


def test_build_tools_returns_definitions_with_catalog_dummy():
    executor = CallableToolExecutor()
    tools = build_tools(token="__dummy__", executor=executor)
    assert {t.name for t in tools} == _TOOL_NAMES
    assert all(t.source == "integration:vuln_intel" for t in tools)
    # All three sources are read-only, so every tool sorts as a read.
    assert all(t.priority == 0 for t in tools)


def test_tool_set_is_identical_across_tiers():
    keyed = {t.name for t in build_tools(_API_KEY, CallableToolExecutor())}
    keyless = {t.name for t in build_tools(_NO_AUTH, CallableToolExecutor())}
    assert keyed == keyless == _TOOL_NAMES


def test_resolve_key_and_header_chokepoints():
    assert resolve_nvd_key(_API_KEY) == _API_KEY
    assert resolve_nvd_key("  spaced-key  ") == "spaced-key"
    assert resolve_nvd_key(_NO_AUTH) == ""
    assert resolve_nvd_key("__dummy__") == ""
    assert resolve_nvd_key("") == ""
    assert resolve_nvd_key(None) == ""
    keyed = build_vulnintel_headers(_API_KEY)
    assert keyed["apiKey"] == _API_KEY
    assert keyed["Accept"] == "application/json"
    assert "apiKey" not in build_vulnintel_headers(_NO_AUTH)
    # A key pasted with whitespace never yields a malformed header.
    assert build_vulnintel_headers("  k \n")["apiKey"] == "k"


def test_status_url_tiering():
    assert vulnintel_status_url(_API_KEY).startswith(NVD_CVE_BASE)
    assert vulnintel_status_url(_NO_AUTH).startswith(EPSS_BASE)
    assert vulnintel_status_url("").startswith(EPSS_BASE)


# ---------------------------------------------------------------------------
# NVD: CVE detail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_cve_returns_detail(monkeypatch):
    def router(url, _call):
        assert url.startswith(NVD_CVE_BASE)
        return _nvd_envelope(
            [
                _nvd_record(
                    "CVE-2021-44228",
                    cisaExploitAdd="2021-12-10",
                    cisaActionDue="2021-12-24",
                    cisaRequiredAction="Apply updates per vendor instructions.",
                    configurations=[
                        {
                            "nodes": [
                                {
                                    "cpeMatch": [
                                        {
                                            "vulnerable": True,
                                            "criteria": "cpe:2.3:a:apache:log4j:2.14.1:*:*:*:*:*:*:*",
                                        }
                                    ]
                                }
                            ]
                        }
                    ],
                )
            ]
        )

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="c1", name="get_vulnintel_cve", arguments={"cve": "cve-2021-44228"})
    )
    assert not result.is_error
    assert "CVE-2021-44228" in result.content
    assert "CVSS v3.1: 9.8 CRITICAL" in result.content
    assert "CWE-502" in result.content
    assert "CISA KEV: LISTED" in result.content
    assert "cpe:2.3:a:apache:log4j" in result.content
    call = recorder.calls[-1]
    assert _query(call["url"])["cveId"] == ["CVE-2021-44228"]
    assert call["headers"]["apiKey"] == _API_KEY


@pytest.mark.asyncio
async def test_get_cve_rejects_invalid_id(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda u, c: {})
    result = await executor.execute_tool(
        ToolCall(id="c2", name="get_vulnintel_cve", arguments={"cve": "not-a-cve"})
    )
    assert "valid CVE id" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_get_cve_not_found_is_friendly(monkeypatch):
    executor, _ = _build(monkeypatch, lambda u, c: _nvd_envelope([]))
    result = await executor.execute_tool(
        ToolCall(id="c3", name="get_vulnintel_cve", arguments={"cve": "CVE-2099-99999"})
    )
    assert "was not found in the NVD" in result.content


@pytest.mark.asyncio
async def test_keyless_tier_sends_no_api_key_header(monkeypatch):
    def router(url, _call):
        return _nvd_envelope([_nvd_record("CVE-2021-44228")])

    executor, recorder = _build(monkeypatch, router, token=_NO_AUTH)
    result = await executor.execute_tool(
        ToolCall(id="c4", name="get_vulnintel_cve", arguments={"cve": "CVE-2021-44228"})
    )
    assert not result.is_error
    assert "apiKey" not in recorder.calls[-1]["headers"]


# ---------------------------------------------------------------------------
# NVD: search + recent (newest-first paging)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_requires_a_filter(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda u, c: {})
    result = await executor.execute_tool(
        ToolCall(id="s1", name="search_vulnintel_cves", arguments={})
    )
    assert "at least one filter" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_search_validates_severity(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda u, c: {})
    result = await executor.execute_tool(
        ToolCall(
            id="s2",
            name="search_vulnintel_cves",
            arguments={"cvss_v3_severity": "EXTREME"},
        )
    )
    assert "must be one of" in result.content
    both = await executor.execute_tool(
        ToolCall(
            id="s3",
            name="search_vulnintel_cves",
            arguments={"cvss_v3_severity": "HIGH", "cvss_v4_severity": "HIGH"},
        )
    )
    assert "pick one" in both.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_search_pages_newest_first_with_flags(monkeypatch):
    records = [
        _nvd_record("CVE-2026-0001", published="2026-07-01T00:00:00.000"),
        _nvd_record("CVE-2026-0002", published="2026-07-02T00:00:00.000"),
    ]

    def router(url, _call):
        q = _query(url)
        if q.get("resultsPerPage") == ["1"]:
            return _nvd_envelope([], total=5)
        # Tail page fetch: startIndex = total - limit = 5 - 2 = 3.
        assert q["startIndex"] == ["3"]
        assert q["resultsPerPage"] == ["2"]
        return _nvd_envelope(records, total=5)

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="s4",
            name="search_vulnintel_cves",
            arguments={"keyword": "apache log4j", "exact_match": True, "kev_only": True, "limit": 2},
        )
    )
    assert not result.is_error
    # Two NVD requests: the count probe then the tail page.
    assert len(recorder.calls) == 2
    for call in recorder.calls:
        q = _query(call["url"])
        assert q["keywordSearch"] == ["apache log4j"]
        # Bare flag params ride without a value.
        assert "keywordExactMatch" in q
        assert "hasKev" in q
        assert "noRejected" in q
    # Ascending API order is reversed to newest-first.
    assert result.content.index("CVE-2026-0002") < result.content.index("CVE-2026-0001")
    assert "Found 5 CVEs" in result.content
    # 5 total, 2 shown -> pagination hint pointing at the older results.
    assert "start_index=2" in result.content


@pytest.mark.asyncio
async def test_recent_cves_sets_pub_window(monkeypatch):
    seen = {"count": 0}

    def router(url, _call):
        # First call is the count probe, second the tail-page fetch (with
        # total=1 both carry resultsPerPage=1, so route by call order).
        seen["count"] += 1
        if seen["count"] == 1:
            return _nvd_envelope([], total=1)
        return _nvd_envelope([_nvd_record("CVE-2026-0100")], total=1)

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="r1",
            name="list_vulnintel_recent_cves",
            arguments={"days": 3, "cvss_v3_severity": "CRITICAL"},
        )
    )
    assert not result.is_error
    q = _query(recorder.calls[-1]["url"])
    assert "pubStartDate" in q and "pubEndDate" in q
    # Extended ISO-8601 with the explicit UTC designator (e.g.
    # 2026-07-10T00:00:00.000Z) per the NVD date-parameter contract.
    assert q["pubStartDate"][0].endswith(".000Z")
    assert q["pubEndDate"][0].endswith(".000Z")
    assert q["cvssV3Severity"] == ["CRITICAL"]
    assert "noRejected" in q
    assert "CVE-2026-0100" in result.content


@pytest.mark.asyncio
async def test_recent_cves_empty_window(monkeypatch):
    def router(url, _call):
        return _nvd_envelope([], total=0)

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="r2", name="list_vulnintel_recent_cves", arguments={"days": 2})
    )
    assert "No CVEs published in the last 2 days" in result.content


# ---------------------------------------------------------------------------
# NVD: change history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cve_history_newest_first(monkeypatch):
    def router(url, _call):
        assert url.startswith(NVD_HISTORY_BASE)
        return {
            "cveChanges": [
                {
                    "change": {
                        "cveId": "CVE-2024-3400",
                        "eventName": "CVE Received",
                        "created": "2024-04-12T08:00:00.000",
                        "sourceIdentifier": "psirt@paloaltonetworks.com",
                        "details": [],
                    }
                },
                {
                    "change": {
                        "cveId": "CVE-2024-3400",
                        "eventName": "Initial Analysis",
                        "created": "2024-04-13T09:00:00.000",
                        "sourceIdentifier": "nvd@nist.gov",
                        "details": [
                            {"action": "Added", "type": "CVSS V3.1"},
                            {"action": "Added", "type": "CWE"},
                        ],
                    }
                },
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="h1", name="get_vulnintel_cve_history", arguments={"cve": "CVE-2024-3400"}
        )
    )
    assert not result.is_error
    q = _query(recorder.calls[-1]["url"])
    assert q["cveId"] == ["CVE-2024-3400"]
    # Full audit trail in one page (history API max) so newest-first is
    # truthful even for heavily-revised CVEs.
    assert q["resultsPerPage"] == ["5000"]
    # Ascending API order reversed: analysis (newest) before reception.
    assert result.content.index("Initial Analysis") < result.content.index("CVE Received")
    assert "Added CVSS V3.1" in result.content


# ---------------------------------------------------------------------------
# CISA KEV
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_kev_hit_and_miss(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda u, c: _KEV_PAYLOAD)
    hit = await executor.execute_tool(
        ToolCall(id="k1", name="check_vulnintel_kev", arguments={"cve": "CVE-2023-4966"})
    )
    assert "IS in the CISA KEV catalog" in hit.content
    assert "Citrix Bleed" in hit.content
    assert "due: 2023-11-08 (OVERDUE)" in hit.content
    assert "Known" in hit.content
    assert recorder.calls[-1]["url"] == KEV_FEED_URL
    miss = await executor.execute_tool(
        ToolCall(id="k2", name="check_vulnintel_kev", arguments={"cve": "CVE-2020-0001"})
    )
    assert "NOT in the CISA KEV catalog" in miss.content
    assert "2026.07.07" in miss.content


@pytest.mark.asyncio
async def test_kev_catalog_is_cached_across_calls(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda u, c: _KEV_PAYLOAD)
    await executor.execute_tool(
        ToolCall(id="k3", name="check_vulnintel_kev", arguments={"cve": "CVE-2024-3400"})
    )
    await executor.execute_tool(
        ToolCall(id="k4", name="search_vulnintel_kev", arguments={"vendor": "cisco"})
    )
    # One single feed download serves both tool calls.
    feed_calls = [c for c in recorder.calls if c["url"] == KEV_FEED_URL]
    assert len(feed_calls) == 1


@pytest.mark.asyncio
async def test_search_kev_filters(monkeypatch):
    executor, _ = _build(monkeypatch, lambda u, c: _KEV_PAYLOAD)
    vendor = await executor.execute_tool(
        ToolCall(id="k5", name="search_vulnintel_kev", arguments={"vendor": "citrix"})
    )
    assert "CVE-2023-4966" in vendor.content
    assert "CVE-2024-3400" not in vendor.content
    ransomware = await executor.execute_tool(
        ToolCall(
            id="k6", name="search_vulnintel_kev", arguments={"ransomware_only": True}
        )
    )
    assert "CVE-2023-4966" in ransomware.content
    assert "CVE-2024-3400" not in ransomware.content
    assert "RANSOMWARE" in ransomware.content
    overdue = await executor.execute_tool(
        ToolCall(id="k8", name="search_vulnintel_kev", arguments={"overdue_only": True})
    )
    assert "CVE-2023-4966" in overdue.content
    assert "CVE-2026-0001" not in overdue.content


@pytest.mark.asyncio
async def test_search_kev_recent_additions_window(monkeypatch):
    """added_within_days is deterministic relative to today.

    In-window entries match; older entries and invalid/future-dated feed
    glitches are excluded (a future dateAdded must never count as "recent").
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    today = _dt.now(_UTC).date()

    def _entry(cve: str, added) -> dict:
        return {
            "cveID": cve,
            "vendorProject": "Vendor",
            "product": "Product",
            "vulnerabilityName": f"{cve} thing",
            "dateAdded": added.isoformat() if added else "not-a-date",
            "dueDate": (today + _td(days=21)).isoformat(),
            "knownRansomwareCampaignUse": "Unknown",
        }

    payload = {
        **_KEV_PAYLOAD,
        "vulnerabilities": [
            _entry("CVE-2026-1111", today - _td(days=5)),  # in window
            _entry("CVE-2026-2222", today - _td(days=45)),  # too old
            _entry("CVE-2026-3333", today + _td(days=3)),  # future glitch
            _entry("CVE-2026-4444", None),  # unparseable date
        ],
    }
    executor, _ = _build(monkeypatch, lambda u, c: payload)
    result = await executor.execute_tool(
        ToolCall(
            id="k7",
            name="search_vulnintel_kev",
            arguments={"added_within_days": 30},
        )
    )
    assert "CVE-2026-1111" in result.content
    assert "CVE-2026-2222" not in result.content
    assert "CVE-2026-3333" not in result.content
    assert "CVE-2026-4444" not in result.content


@pytest.mark.asyncio
async def test_kev_feed_failure_is_actionable(monkeypatch):
    recorder = _ErrorRecorder("Error: Vulnerability Intelligence server error (503).")
    monkeypatch.setattr(vi, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_NO_AUTH, executor=executor)
    result = await executor.execute_tool(
        ToolCall(id="k9", name="check_vulnintel_kev", arguments={"cve": "CVE-2024-3400"})
    )
    assert "503" in result.content


# ---------------------------------------------------------------------------
# EPSS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_epss_single_and_batch(monkeypatch):
    def router(url, call):
        assert url == EPSS_BASE
        return {
            "total": 2,
            "data": [
                {"cve": "CVE-2024-3400", "epss": "0.945320000", "percentile": "0.999750000", "date": "2026-07-09"},
                {"cve": "CVE-2023-4966", "epss": "0.973310000", "percentile": "0.999870000", "date": "2026-07-09"},
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="e1",
            name="get_vulnintel_epss",
            arguments={"cves": "cve-2024-3400, CVE-2023-4966, CVE-2024-3400"},
        )
    )
    assert not result.is_error
    # Deduplicated, normalized batch parameter.
    assert recorder.calls[-1]["params"]["cve"] == "CVE-2024-3400,CVE-2023-4966"
    assert "0.94532" in result.content
    assert "percentile 100.0%" in result.content


@pytest.mark.asyncio
async def test_epss_validates_input(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda u, c: {})
    bad = await executor.execute_tool(
        ToolCall(id="e2", name="get_vulnintel_epss", arguments={"cves": "CVE-1, nope"})
    )
    assert "not a valid CVE id" in bad.content
    too_many = await executor.execute_tool(
        ToolCall(
            id="e3",
            name="get_vulnintel_epss",
            arguments={"cves": ",".join(f"CVE-2024-{10000 + i}" for i in range(101))},
        )
    )
    assert "at most 100" in too_many.content
    bad_date = await executor.execute_tool(
        ToolCall(
            id="e4",
            name="get_vulnintel_epss",
            arguments={"cves": "CVE-2024-3400", "date": "07/09/2026"},
        )
    )
    assert "YYYY-MM-DD" in bad_date.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_epss_reports_missing_scores(monkeypatch):
    def router(url, call):
        return {
            "total": 1,
            "data": [
                {"cve": "CVE-2024-3400", "epss": "0.945320000", "percentile": "0.999", "date": "2026-07-09"}
            ],
        }

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="e5",
            name="get_vulnintel_epss",
            arguments={"cves": "CVE-2024-3400, CVE-2099-11111"},
        )
    )
    assert "No score returned for: CVE-2099-11111" in result.content


@pytest.mark.asyncio
async def test_top_epss_params_and_output(monkeypatch):
    def router(url, call):
        assert call["params"]["order"] == "!epss"
        assert call["params"]["epss-gt"] == "0.9"
        return {
            "total": 1200,
            "data": [
                {"cve": "CVE-2023-4966", "epss": "0.973310000", "percentile": "0.999870000"}
            ],
        }

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="t1",
            name="list_vulnintel_top_epss",
            arguments={"min_epss": 0.9, "limit": 1},
        )
    )
    assert not result.is_error
    assert "CVE-2023-4966" in result.content
    assert "offset=1" in result.content  # more pages available


@pytest.mark.asyncio
async def test_top_epss_validates_thresholds(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda u, c: {})
    result = await executor.execute_tool(
        ToolCall(id="t2", name="list_vulnintel_top_epss", arguments={"min_epss": 3})
    )
    assert "between 0 and 1" in result.content
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Merged assessment
# ---------------------------------------------------------------------------


def _assessment_router(kev: bool, epss: str):
    def router(url, _call):
        if url.startswith(NVD_CVE_BASE):
            return _nvd_envelope([_nvd_record("CVE-2024-3400")])
        if url == KEV_FEED_URL:
            return _KEV_PAYLOAD if kev else {**_KEV_PAYLOAD, "vulnerabilities": []}
        if url == EPSS_BASE:
            return {
                "total": 1,
                "data": [
                    {"cve": "CVE-2024-3400", "epss": epss, "percentile": "0.999", "date": "2026-07-09"}
                ],
            }
        raise AssertionError(f"unexpected url {url}")

    return router


@pytest.mark.asyncio
async def test_assess_kev_listed_is_critical(monkeypatch):
    executor, _ = _build(monkeypatch, _assessment_router(kev=True, epss="0.945"))
    result = await executor.execute_tool(
        ToolCall(id="a1", name="assess_vulnintel_cve", arguments={"cve": "CVE-2024-3400"})
    )
    assert not result.is_error
    assert "VERDICT: CRITICAL - EXPLOITED IN THE WILD" in result.content
    assert "CISA KEV: LISTED" in result.content
    assert "CVSS v3.1: 9.8 CRITICAL" in result.content
    assert "EPSS: 0.94500" in result.content


@pytest.mark.asyncio
async def test_assess_high_epss_without_kev(monkeypatch):
    executor, _ = _build(monkeypatch, _assessment_router(kev=False, epss="0.62"))
    result = await executor.execute_tool(
        ToolCall(id="a2", name="assess_vulnintel_cve", arguments={"cve": "CVE-2024-3400"})
    )
    assert "VERDICT: CRITICAL - very high exploitation probability" in result.content
    assert "CISA KEV: not listed" in result.content


@pytest.mark.asyncio
async def test_assess_low_epss_high_cvss_is_high(monkeypatch):
    executor, _ = _build(monkeypatch, _assessment_router(kev=False, epss="0.004"))
    result = await executor.execute_tool(
        ToolCall(id="a3", name="assess_vulnintel_cve", arguments={"cve": "CVE-2024-3400"})
    )
    # CVSS 9.8 without KEV/EPSS signals -> HIGH.
    assert "VERDICT: HIGH" in result.content


@pytest.mark.asyncio
async def test_assess_survives_partial_source_failure(monkeypatch):
    class _PartialRecorder(_Recorder):
        """NVD down; KEV + EPSS healthy."""

        async def __call__(self, _client, method, url, _provider, action, **kwargs):
            self.calls.append({"method": method, "url": url, "action": action, **kwargs})
            if url.startswith(NVD_CVE_BASE):
                return None, "Error: Vulnerability Intelligence request timed out."
            return _StubResponse(self._router(url, None)), None

    def data_router(url, _call):
        if url == KEV_FEED_URL:
            return _KEV_PAYLOAD
        return {
            "total": 1,
            "data": [{"cve": "CVE-2024-3400", "epss": "0.9", "percentile": "0.99"}],
        }

    recorder = _PartialRecorder(data_router)
    monkeypatch.setattr(vi, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_NO_AUTH, executor=executor)
    result = await executor.execute_tool(
        ToolCall(id="a4", name="assess_vulnintel_cve", arguments={"cve": "CVE-2024-3400"})
    )
    assert not result.is_error
    # KEV membership still drives the verdict even with the NVD down.
    assert "VERDICT: CRITICAL - EXPLOITED IN THE WILD" in result.content
    assert "NVD: unavailable" in result.content


@pytest.mark.asyncio
async def test_assess_unknown_everywhere(monkeypatch):
    def router(url, _call):
        if url.startswith(NVD_CVE_BASE):
            return _nvd_envelope([])
        if url == KEV_FEED_URL:
            return {**_KEV_PAYLOAD, "vulnerabilities": []}
        return {"total": 0, "data": []}

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="a5", name="assess_vulnintel_cve", arguments={"cve": "CVE-2099-42424"})
    )
    assert "was not found" in result.content


# ---------------------------------------------------------------------------
# Status + integration class + connectivity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_reports_all_three_sources(monkeypatch):
    def router(url, _call):
        if url.startswith(NVD_CVE_BASE):
            return {"totalResults": 364499, "vulnerabilities": []}
        if url == KEV_FEED_URL:
            return _KEV_PAYLOAD
        return {
            "total": 1,
            "data": [{"cve": "CVE-2021-44228", "epss": "0.99999", "percentile": "1.0", "date": "2026-07-09"}],
        }

    executor, _ = _build(monkeypatch, router, token=_NO_AUTH)
    result = await executor.execute_tool(
        ToolCall(id="st1", name="get_vulnintel_status", arguments={})
    )
    # A SUCCESS: all three sources report OK.  It stays unflagged even though
    # the tip mentions the NVD "rate limit" — ``looks_like_tool_error`` scans
    # only the head of the content for rate-limit phrasing, so a benign
    # mention further down cannot mislabel a real result.
    assert result.is_error is False
    assert "public tier, 5 requests" in result.content
    assert "NVD CVE API 2.0: OK" in result.content
    assert "CISA KEV catalog: OK (version 2026.07.07" in result.content
    assert "FIRST EPSS API: OK" in result.content
    assert "not endorsed or certified by the NVD" in result.content
    # Keyless tier advertises the free key upgrade.
    assert "free NVD API key" in result.content


def test_integration_class_metadata():
    integ = VulnIntelIntegration()
    assert integ.provider_name == "vuln_intel"
    assert integ.display_name == "Vulnerability Intelligence"
    assert integ.auth_types == ["api_key", "none"]
    assert integ.get_oauth_defaults() is None


@pytest.mark.asyncio
async def test_connectivity_blank_key_fails_fast_instead_of_epss_probe():
    # An api_key-mode integration missing its key must NOT silently pass the
    # connectivity test by probing the unauthenticated EPSS endpoint (the
    # keyless tier is represented by the explicit "__no_auth__" sentinel).
    from app.api.v1.integration_testing import test_provider_connectivity

    result = await test_provider_connectivity(
        "vuln_intel", "  ", {}, "Vulnerability Intelligence"
    )
    assert result["status"] == "error"
    assert "Missing API key" in result["message"]
