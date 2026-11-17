"""Tests for the AbuseIPDB integration provider.

Covers tool introspection (including the catalog dummy token), the `Key`
header helper, the check/reports/check-block/blacklist read tools (parameter
clamping, envelope parsing, pagination hints), the local category reference,
the report tools (category validation + form/multipart payloads), the
destructive clear-address tool, and the call-time credential guard. Every API
call routes through the shared ``safe_request`` helper, which is monkeypatched
here so no network is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import abuseipdb as ab
from app.integrations.providers.abuseipdb import (
    ABUSEIPDB_API,
    ABUSEIPDB_CATEGORIES,
    build_abuseipdb_headers,
    build_tools,
)
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "abuseipdb-test-key"


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
    names = {t.name for t in tools}
    assert names == {
        "check_abuseipdb_ip",
        "list_abuseipdb_ip_reports",
        "check_abuseipdb_network",
        "get_abuseipdb_blacklist",
        "list_abuseipdb_categories",
        "report_abuseipdb_ip",
        "bulk_report_abuseipdb_ips",
        "clear_abuseipdb_address",
    }
    assert all(t.source == "integration:abuseipdb" for t in tools)


def test_tool_priorities_sort_reads_before_writes_before_destructive():
    executor = CallableToolExecutor()
    tools = {t.name: t for t in build_tools(token=_TOKEN, executor=executor)}
    assert tools["check_abuseipdb_ip"].priority == 0
    assert tools["get_abuseipdb_blacklist"].priority == 0
    assert tools["list_abuseipdb_categories"].priority == 0
    assert tools["report_abuseipdb_ip"].priority == 1
    assert tools["bulk_report_abuseipdb_ips"].priority == 1
    assert tools["clear_abuseipdb_address"].priority == 2


def test_headers_helper_strips_and_sets_accept():
    headers = build_abuseipdb_headers("  key-123 \n")
    assert headers["Key"] == "key-123"
    assert headers["Accept"] == "application/json"


@pytest.mark.asyncio
async def test_tools_without_credentials_return_error(monkeypatch):
    recorder = _Recorder(lambda m, u: {})
    monkeypatch.setattr(ab, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token="   ", executor=executor)
    result = await executor.execute_tool(
        ToolCall(id="c0", name="check_abuseipdb_ip", arguments={"ip_address": "1.2.3.4"})
    )
    assert "credentials are not configured" in result.content.lower()
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Check (single IP)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_ip_parses_envelope_and_clamps_age(monkeypatch):
    def router(method, url):
        return {
            "data": {
                "ipAddress": "118.25.6.39",
                "isPublic": True,
                "ipVersion": 4,
                "isWhitelisted": False,
                "abuseConfidenceScore": 100,
                "countryCode": "CN",
                "usageType": "Data Center/Web Hosting/Transit",
                "isp": "Tencent Cloud Computing (Beijing) Co. Ltd",
                "domain": "tencent.com",
                "hostnames": [],
                "isTor": False,
                "totalReports": 42,
                "numDistinctUsers": 7,
                "lastReportedAt": "2026-07-01T20:55:14+00:00",
            }
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="check_abuseipdb_ip",
            arguments={"ip_address": "118.25.6.39", "max_age_in_days": 9999},
        )
    )
    assert not result.is_error
    assert "118.25.6.39" in result.content
    assert "100/100" in result.content
    assert "42" in result.content and "7 distinct reporters" in result.content
    call = recorder.calls[-1]
    assert call["url"] == f"{ABUSEIPDB_API}/check"
    assert call["params"]["ipAddress"] == "118.25.6.39"
    # maxAgeInDays is clamped to the API maximum of 365.
    assert call["params"]["maxAgeInDays"] == 365
    assert "verbose" not in call["params"]
    assert call["headers"]["Key"] == _TOKEN


@pytest.mark.asyncio
async def test_check_ip_verbose_includes_reports_with_category_names(monkeypatch):
    def router(method, url):
        return {
            "data": {
                "ipAddress": "118.25.6.39",
                "abuseConfidenceScore": 88,
                "totalReports": 1,
                "numDistinctUsers": 1,
                "reports": [
                    {
                        "reportedAt": "2026-07-01T20:55:14+00:00",
                        "comment": "Invalid user oracle from 118.25.6.39",
                        "categories": [18, 22],
                        "reporterId": 1,
                        "reporterCountryCode": "US",
                    }
                ],
            }
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c2",
            name="check_abuseipdb_ip",
            arguments={"ip_address": "118.25.6.39", "verbose": True},
        )
    )
    assert not result.is_error
    # Category ids are rendered with their official names.
    assert "Brute-Force (18)" in result.content
    assert "SSH (22)" in result.content
    assert "Invalid user oracle" in result.content
    # verbose is sent with an explicit truthy value (not an empty string)
    # so frameworks that drop empty query values can't disable it.
    assert recorder.calls[-1]["params"]["verbose"] == "true"


@pytest.mark.asyncio
async def test_check_ip_requires_ip(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="c3", name="check_abuseipdb_ip", arguments={"ip_address": " "})
    )
    assert "ip_address is required" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_single_ip_tools_reject_invalid_addresses(monkeypatch):
    # Non-IP inputs (hostnames, CIDR blocks, garbage) short-circuit locally
    # with a clear message instead of burning daily API quota.
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    cases = [
        ("check_abuseipdb_ip", {"ip_address": "evil.example.com"}),
        ("list_abuseipdb_ip_reports", {"ip_address": "203.0.113.0/24"}),
        ("report_abuseipdb_ip", {"ip": "not-an-ip", "categories": "18"}),
        ("clear_abuseipdb_address", {"ip_address": "999.1.2.3"}),
    ]
    for i, (name, arguments) in enumerate(cases):
        result = await executor.execute_tool(
            ToolCall(id=f"cip{i}", name=name, arguments=arguments)
        )
        assert "not a valid IPv4 or IPv6 address" in result.content
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Reports (pagination)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_reports_paginates_and_hints_next_page(monkeypatch):
    def router(method, url):
        return {
            "data": {
                "total": 2840,
                "page": 5,
                "count": 1,
                "perPage": 25,
                "lastPage": 114,
                "results": [
                    {
                        "reportedAt": "2026-05-01T21:00:03+00:00",
                        "comment": "Invalid user joseph from 176.111.173.242",
                        "categories": [18, 22],
                        "reporterId": 43121,
                        "reporterCountryCode": "DE",
                    }
                ],
            }
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c4",
            name="list_abuseipdb_ip_reports",
            arguments={"ip_address": "176.111.173.242", "page": 5, "per_page": 500},
        )
    )
    assert not result.is_error
    assert "2840" in result.content
    assert "Invalid user joseph" in result.content
    assert "Page 5 of 114" in result.content
    call = recorder.calls[-1]
    assert call["url"] == f"{ABUSEIPDB_API}/reports"
    assert call["params"]["page"] == 5
    # perPage is clamped to the API maximum of 100.
    assert call["params"]["perPage"] == 100


# ---------------------------------------------------------------------------
# Check-block (CIDR)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_network_sorts_worst_first_and_validates_cidr(monkeypatch):
    def router(method, url):
        return {
            "data": {
                "networkAddress": "203.0.113.0",
                "netmask": "255.255.255.0",
                "minAddress": "203.0.113.1",
                "maxAddress": "203.0.113.254",
                "numPossibleHosts": 254,
                "addressSpaceDesc": "Documentation",
                "reportedAddress": [
                    {
                        "ipAddress": "203.0.113.9",
                        "numReports": 5,
                        "abuseConfidenceScore": 10,
                        "mostRecentReport": "2026-06-01T00:00:00+00:00",
                    },
                    {
                        "ipAddress": "203.0.113.7",
                        "numReports": 631,
                        "abuseConfidenceScore": 99,
                        "mostRecentReport": "2026-07-01T00:00:00+00:00",
                    },
                ],
            }
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c5",
            name="check_abuseipdb_network",
            arguments={"network": "203.0.113.0/24"},
        )
    )
    assert not result.is_error
    # Worst address (score 99) is listed before the low-score one.
    assert result.content.index("203.0.113.7") < result.content.index("203.0.113.9")
    assert recorder.calls[-1]["url"] == f"{ABUSEIPDB_API}/check-block"
    assert recorder.calls[-1]["params"]["network"] == "203.0.113.0/24"

    # A bare IP without CIDR notation short-circuits before any API call.
    bad = await executor.execute_tool(
        ToolCall(
            id="c6", name="check_abuseipdb_network", arguments={"network": "203.0.113.5"}
        )
    )
    assert "CIDR notation" in bad.content


@pytest.mark.asyncio
async def test_check_network_rejects_invalid_cidr_and_normalizes(monkeypatch):
    def router(method, url):
        return {"data": {"networkAddress": "203.0.113.0", "reportedAddress": []}}

    executor, recorder = _build(monkeypatch, router)
    # Malformed CIDR values short-circuit locally before any API call.
    for bogus in ("203.0.113.0/abc", "not-a-cidr/24", "203.0.113.0/33"):
        result = await executor.execute_tool(
            ToolCall(
                id=f"c6-{bogus}",
                name="check_abuseipdb_network",
                arguments={"network": bogus},
            )
        )
        assert "not valid CIDR notation" in result.content
    assert recorder.calls == []

    # Host bits are tolerated: the network is normalized before the request.
    ok = await executor.execute_tool(
        ToolCall(
            id="c6-norm",
            name="check_abuseipdb_network",
            arguments={"network": "203.0.113.5/24"},
        )
    )
    assert not ok.is_error
    assert recorder.calls[-1]["params"]["network"] == "203.0.113.0/24"


@pytest.mark.asyncio
async def test_check_network_sort_tolerates_null_scores(monkeypatch):
    # An explicit null abuseConfidenceScore/numReports from the API must not
    # make the worst-first sort raise TypeError.
    def router(method, url):
        return {
            "data": {
                "networkAddress": "203.0.113.0",
                "reportedAddress": [
                    {"ipAddress": "203.0.113.9", "abuseConfidenceScore": None},
                    {
                        "ipAddress": "203.0.113.7",
                        "numReports": 631,
                        "abuseConfidenceScore": 99,
                    },
                ],
            }
        }

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c6-null",
            name="check_abuseipdb_network",
            arguments={"network": "203.0.113.0/24"},
        )
    )
    assert not result.is_error
    assert result.content.index("203.0.113.7") < result.content.index("203.0.113.9")


# ---------------------------------------------------------------------------
# Blacklist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blacklist_params_and_meta(monkeypatch):
    def router(method, url):
        return {
            "meta": {"generatedAt": "2026-07-04T10:00:00+00:00"},
            "data": [
                {
                    "ipAddress": "5.188.10.179",
                    "countryCode": "US",
                    "abuseConfidenceScore": 100,
                    "lastReportedAt": "2026-07-04T09:17:02+00:00",
                }
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c7",
            name="get_abuseipdb_blacklist",
            arguments={
                "confidence_minimum": 10,
                "only_countries": " us , mx ",
                "ip_version": 6,
            },
        )
    )
    assert not result.is_error
    assert "5.188.10.179" in result.content
    assert "generated at 2026-07-04" in result.content
    params = recorder.calls[-1]["params"]
    # confidenceMinimum is clamped to the API floor of 25.
    assert params["confidenceMinimum"] == 25
    # Country codes are normalised (trimmed + uppercased).
    assert params["onlyCountries"] == "US,MX"
    assert params["ipVersion"] == 6

    # A string "4" from the LLM is coerced instead of silently dropped.
    await executor.execute_tool(
        ToolCall(
            id="c7b",
            name="get_abuseipdb_blacklist",
            arguments={"ip_version": "4"},
        )
    )
    assert recorder.calls[-1]["params"]["ipVersion"] == 4


# ---------------------------------------------------------------------------
# Categories (local reference)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_categories_is_local_and_complete(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="c8", name="list_abuseipdb_categories", arguments={})
    )
    assert not result.is_error
    assert len(ABUSEIPDB_CATEGORIES) == 23
    for cid, (name, _desc) in ABUSEIPDB_CATEGORIES.items():
        assert f"{cid}: {name}" in result.content
    # No API call is made - the reference costs no quota.
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Report (write)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_ip_posts_form_data(monkeypatch):
    def router(method, url):
        return {"data": {"ipAddress": "127.0.0.1", "abuseConfidenceScore": 52}}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c9",
            name="report_abuseipdb_ip",
            arguments={
                "ip": "127.0.0.1",
                "categories": "18, 22",
                "comment": "SSH login attempts with user root.",
            },
        )
    )
    assert not result.is_error
    assert "52/100" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"] == f"{ABUSEIPDB_API}/report"
    assert call["data"]["ip"] == "127.0.0.1"
    assert call["data"]["categories"] == "18,22"
    assert call["data"]["comment"] == "SSH login attempts with user root."


@pytest.mark.asyncio
async def test_report_ip_tolerates_null_optional_params(monkeypatch):
    # LLMs sometimes send explicit JSON null for optional string params;
    # the tool must treat None like "" instead of raising AttributeError.
    def router(method, url):
        if url.endswith("/blacklist"):
            return {"data": []}
        return {"data": {"ipAddress": "127.0.0.1", "abuseConfidenceScore": 10}}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c9b",
            name="report_abuseipdb_ip",
            arguments={
                "ip": "127.0.0.1",
                "categories": "18",
                "comment": None,
                "timestamp": None,
            },
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert "comment" not in call["data"]
    assert "timestamp" not in call["data"]

    blk = await executor.execute_tool(
        ToolCall(
            id="c9c",
            name="get_abuseipdb_blacklist",
            arguments={"only_countries": None, "except_countries": None},
        )
    )
    assert not blk.is_error
    params = recorder.calls[-1]["params"]
    assert "onlyCountries" not in params
    assert "exceptCountries" not in params


@pytest.mark.asyncio
async def test_report_ip_rejects_invalid_categories(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c10",
            name="report_abuseipdb_ip",
            arguments={"ip": "1.2.3.4", "categories": "99"},
        )
    )
    assert "not a valid AbuseIPDB category id" in result.content
    assert recorder.calls == []

    result = await executor.execute_tool(
        ToolCall(
            id="c11",
            name="report_abuseipdb_ip",
            arguments={"ip": "1.2.3.4", "categories": "ssh"},
        )
    )
    assert "not a numeric category id" in result.content
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Bulk report (write, multipart CSV)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_report_uploads_csv_and_reports_rejects(monkeypatch):
    def router(method, url):
        return {
            "data": {
                "savedReports": 60,
                "invalidReports": [
                    {"error": "Duplicate IP", "input": "41.188.138.68", "rowNumber": 5}
                ],
            }
        }

    csv_content = (
        "IP,Categories,ReportDate,Comment\n"
        '203.0.113.5,"18,22",2026-07-04T11:25:11-04:00,SSH brute force'
    )
    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c12",
            name="bulk_report_abuseipdb_ips",
            arguments={"csv_content": csv_content},
        )
    )
    assert not result.is_error
    assert "60 report(s) saved" in result.content
    assert "Duplicate IP" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"] == f"{ABUSEIPDB_API}/bulk-report"
    filename, payload, content_type = call["files"]["csv"]
    assert filename == "report.csv"
    assert payload == csv_content.encode("utf-8")
    assert content_type == "text/csv"


@pytest.mark.asyncio
async def test_bulk_report_requires_csv_header(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c13",
            name="bulk_report_abuseipdb_ips",
            arguments={"csv_content": "203.0.113.5,18,now,bad row"},
        )
    )
    assert "header" in result.content.lower()
    assert recorder.calls == []

    # A header starting with "IP," but missing the other documented columns
    # is rejected too (the guard requires all four columns).
    partial = await executor.execute_tool(
        ToolCall(
            id="c13b",
            name="bulk_report_abuseipdb_ips",
            arguments={"csv_content": "IP,Cats\n203.0.113.5,18"},
        )
    )
    assert "header" in partial.content.lower()
    assert recorder.calls == []

    # Case, spaces, and quoting variations of the canonical header pass.
    ok = await executor.execute_tool(
        ToolCall(
            id="c13c",
            name="bulk_report_abuseipdb_ips",
            arguments={
                "csv_content": (
                    '"ip", "categories", "reportdate", "comment"\n'
                    '203.0.113.5,"18,22",2026-07-04T11:25:11-04:00,SSH brute force'
                )
            },
        )
    )
    assert not ok.is_error
    assert len(recorder.calls) == 1


@pytest.mark.asyncio
async def test_bulk_report_strips_utf8_bom(monkeypatch):
    # CSVs exported from Excel/Sheets commonly start with a UTF-8 BOM; the
    # guard must not reject an otherwise-correct header, and the BOM must
    # not reach the API upload.
    def router(method, url):
        return {"data": {"savedReports": 1, "invalidReports": []}}

    csv_body = (
        "IP,Categories,ReportDate,Comment\n"
        '203.0.113.5,"18,22",2026-07-04T11:25:11-04:00,SSH brute force'
    )
    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c13d",
            name="bulk_report_abuseipdb_ips",
            arguments={"csv_content": "\ufeff" + csv_body},
        )
    )
    assert not result.is_error
    assert "1 report(s) saved" in result.content
    _filename, payload, _content_type = recorder.calls[-1]["files"]["csv"]
    assert payload == csv_body.encode("utf-8")
    assert not payload.startswith(b"\xef\xbb\xbf")


# ---------------------------------------------------------------------------
# Clear address (destructive)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_address_deletes_own_reports(monkeypatch):
    def router(method, url):
        return {"data": {"numReportsDeleted": 3}}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c14",
            name="clear_abuseipdb_address",
            arguments={"ip_address": "118.25.6.39"},
        )
    )
    assert not result.is_error
    assert "3 report(s)" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "delete"
    assert call["url"] == f"{ABUSEIPDB_API}/clear-address"
    assert call["params"]["ipAddress"] == "118.25.6.39"
