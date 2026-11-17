"""The SecurityScorecard integration provider.

Covers the catalog entry and registration, the token header, the strict
patterns guarding every path segment (domain, portfolio id, issue type), the
read/write split a read-only binding relies on, and each tool against a mocked
API (``httpx.MockTransport``): the request it sends and the text it answers.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG, build_dummy_token
from app.integrations.providers import securityscorecard
from app.integrations.providers.securityscorecard import (
    SECURITYSCORECARD_API,
    build_tools,
    normalize_domain,
)
from app.services.integration_read_only import integration_tool_access
from app.services.integration_tools import PROVIDER_TOOLS, _SafeIntegrationExecutor

_TOKEN = "sscToken123"


def _build(token: str = _TOKEN):
    executor = _SafeIntegrationExecutor()
    tools = build_tools(token, executor)
    return executor._handlers, tools


def _mock(monkeypatch, responder) -> list[httpx.Request]:
    """Route the provider's client to *responder*; return the requests sent."""
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responder(request)

    def _client(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(_handler), **kwargs)

    monkeypatch.setattr(securityscorecard, "egress_client", _client)
    return seen


def _json(payload, status: int = 200):
    return lambda _request: httpx.Response(status, json=payload)


def test_catalog_entry_and_registration():
    entry = PROVIDER_CATALOG["securityscorecard"]
    assert entry["auth_type"] == "api_key"
    assert entry["fields"] == ["api_key"]
    assert entry["integration_category"] == "exposure_management"
    assert "extra_fields" not in entry
    assert PROVIDER_TOOLS["securityscorecard"] is build_tools


def test_dummy_token_builds_the_tools():
    assert build_tools(build_dummy_token("securityscorecard"), _SafeIntegrationExecutor())


@pytest.mark.parametrize("token", ["", "   "])
def test_blank_token_builds_no_tools(token):
    handlers, tools = _build(token)
    assert tools == []
    assert handlers == {}


def test_reads_and_writes_are_classified():
    _, tools = _build()
    names = {t.name for t in tools}
    writes = {
        "create_securityscorecard_portfolio",
        "delete_securityscorecard_portfolio",
        "add_securityscorecard_portfolio_company",
        "remove_securityscorecard_portfolio_company",
        "generate_securityscorecard_report",
    }
    assert {n for n in names if integration_tool_access(n) == "write"} == writes
    assert len(names) == 14


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("example.com", "example.com"),
        ("  Example.COM ", "example.com"),
        ("https://www.example.com/path?q=1", "www.example.com"),
        ("sub.example.co.uk.", "sub.example.co.uk"),
        ("example", None),
        ("../etc/passwd", None),
        ("example.com/../../portfolios", "example.com"),
        ("exa mple.com", None),
        ("example.com#frag", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_domain(raw, expected):
    assert normalize_domain(raw) == expected


@pytest.mark.asyncio
async def test_company_scorecard_sends_the_token_and_formats(monkeypatch):
    seen = _mock(
        monkeypatch,
        _json(
            {
                "name": "Example Inc",
                "domain": "example.com",
                "score": 84,
                "grade": "B",
                "last30day_score_change": -3,
                "industry": "technology",
                "size": "large",
            }
        ),
    )
    handlers, _ = _build()
    out = await handlers["get_securityscorecard_company"](domain="https://example.com/")
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert str(seen[0].url) == f"{SECURITYSCORECARD_API}/companies/example.com"
    assert seen[0].headers["Authorization"] == f"Token {_TOKEN}"
    assert out == (
        "Example Inc (example.com)\n"
        "  Score: 84 | Grade: B | 30-day change: -3\n"
        "  Industry: technology | Size: large"
    )


@pytest.mark.asyncio
async def test_invalid_domain_sends_nothing(monkeypatch):
    seen = _mock(monkeypatch, _json({}))
    handlers, _ = _build()
    out = await handlers["get_securityscorecard_company"](domain="not a domain")
    assert out.startswith("Error:")
    assert seen == []


@pytest.mark.asyncio
async def test_factors_list_their_issue_types(monkeypatch):
    _mock(
        monkeypatch,
        _json(
            {
                "entries": [
                    {
                        "name": "patching_cadence",
                        "score": 61,
                        "grade": "D",
                        "issue_summary": [
                            {
                                "type": "patching_cadence_high",
                                "count": 4,
                                "severity": "high",
                                "total_score_impact": 2.5,
                            }
                        ],
                    }
                ]
            }
        ),
    )
    handlers, _ = _build()
    out = await handlers["list_securityscorecard_factors"](domain="example.com")
    assert "- patching_cadence: score 61 (grade D)" in out
    assert "    patching_cadence_high: 4 finding(s), severity high, score impact 2.5" in out


@pytest.mark.asyncio
async def test_issue_findings_are_limited_and_flattened(monkeypatch):
    findings = [
        {"hostname": f"h{i}.example.com", "port": 443, "cve": "CVE-2024-0001", "noise": "x"}
        for i in range(5)
    ]
    findings[0]["hostname"] = "evil\nInjected: line"
    seen = _mock(monkeypatch, _json({"entries": findings}))
    handlers, _ = _build()
    out = await handlers["list_securityscorecard_issues"](
        domain="example.com", issue_type="patching_cadence_high", limit=2
    )
    assert str(seen[0].url) == (
        f"{SECURITYSCORECARD_API}/companies/example.com/issues/patching_cadence_high"
    )
    lines = out.splitlines()
    assert lines[0] == "Found 5 findings:"
    assert lines[1] == "- hostname: evil Injected: line | port: 443 | cve: CVE-2024-0001"
    assert lines[3] == "[3 more not shown: raise limit (max 100).]"
    assert len(lines) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("issue_type", ["../portfolios", "a/b", "", "UPPER"])
async def test_issue_type_is_a_slug(monkeypatch, issue_type):
    seen = _mock(monkeypatch, _json({}))
    handlers, _ = _build()
    out = await handlers["list_securityscorecard_issues"](domain="example.com", issue_type=issue_type)
    assert out.startswith("Error:")
    assert seen == []


@pytest.mark.asyncio
async def test_score_history_keeps_the_last_days(monkeypatch):
    entries = [{"date": f"2026-10-{d:02d}", "score": 80 + d % 3} for d in range(1, 11)]
    _mock(monkeypatch, _json({"entries": entries}))
    handlers, _ = _build()
    out = await handlers["get_securityscorecard_score_history"](domain="example.com", days=3)
    assert out.splitlines() == [
        "Daily score of example.com (3 day(s)):",
        "- 2026-10-08: 82",
        "- 2026-10-09: 80",
        "- 2026-10-10: 81",
    ]


@pytest.mark.asyncio
async def test_score_history_tolerates_a_non_numeric_days(monkeypatch):
    _mock(monkeypatch, _json({"entries": [{"date": "2026-10-01", "score": 70}]}))
    handlers, _ = _build()
    out = await handlers["get_securityscorecard_score_history"](domain="example.com", days="lots")
    assert out == "Daily score of example.com (1 day(s)):\n- 2026-10-01: 70"


@pytest.mark.asyncio
async def test_portfolio_companies(monkeypatch):
    seen = _mock(
        monkeypatch,
        _json({"entries": [{"name": "Vendor", "domain": "vendor.io", "score": 72, "grade": "C", "industry": "retail"}]}),
    )
    handlers, _ = _build()
    out = await handlers["list_securityscorecard_portfolio_companies"](portfolio_id="5f1a2b")
    assert str(seen[0].url) == f"{SECURITYSCORECARD_API}/portfolios/5f1a2b/companies"
    assert out == "Portfolio 5f1a2b has 1 company(ies):\n- Vendor (vendor.io): score 72, grade C, industry retail"


@pytest.mark.asyncio
async def test_add_company_puts_on_the_portfolio(monkeypatch):
    seen = _mock(monkeypatch, _json({}))
    handlers, _ = _build()
    out = await handlers["add_securityscorecard_portfolio_company"](portfolio_id="5f1a2b", domain="Vendor.io")
    assert seen[0].method == "PUT"
    assert str(seen[0].url) == f"{SECURITYSCORECARD_API}/portfolios/5f1a2b/companies/vendor.io"
    assert out == "Added vendor.io to portfolio 5f1a2b."


@pytest.mark.asyncio
async def test_portfolio_id_is_checked(monkeypatch):
    seen = _mock(monkeypatch, _json({}))
    handlers, _ = _build()
    out = await handlers["delete_securityscorecard_portfolio"](portfolio_id="../reports")
    assert out.startswith("Error:")
    assert seen == []


@pytest.mark.asyncio
async def test_create_portfolio_body(monkeypatch):
    seen = _mock(monkeypatch, _json({"id": "abc", "name": "Critical vendors"}))
    handlers, _ = _build()
    out = await handlers["create_securityscorecard_portfolio"](name="  Critical   vendors ", description="tier 1")
    assert seen[0].method == "POST"
    assert json.loads(seen[0].content) == {
        "name": "Critical vendors",
        "privacy": "private",
        "description": "tier 1",
    }
    assert out == "Created portfolio Critical vendors (id: abc)."


@pytest.mark.asyncio
async def test_generate_report_refuses_an_unknown_type(monkeypatch):
    seen = _mock(monkeypatch, _json({}))
    handlers, _ = _build()
    out = await handlers["generate_securityscorecard_report"](domain="example.com", report_type="../x")
    assert out.startswith("Error:")
    assert seen == []


@pytest.mark.asyncio
async def test_generate_report_posts_the_identifier(monkeypatch):
    seen = _mock(monkeypatch, _json({"id": "r1"}))
    handlers, _ = _build()
    out = await handlers["generate_securityscorecard_report"](domain="example.com", report_type="issues")
    assert str(seen[0].url) == f"{SECURITYSCORECARD_API}/reports/issues"
    assert json.loads(seen[0].content) == {"scorecard_identifier": "example.com"}
    assert "id: r1" in out


@pytest.mark.asyncio
async def test_api_error_reads_as_an_error(monkeypatch):
    _mock(monkeypatch, _json({"error": {"message": "Not found"}}, status=404))
    handlers, _ = _build()
    out = await handlers["get_securityscorecard_company"](domain="example.com")
    assert out.startswith("Error")


@pytest.mark.asyncio
async def test_a_write_is_not_retried_on_a_gateway_error(monkeypatch):
    seen = _mock(monkeypatch, _json({}, status=503))
    handlers, _ = _build()
    out = await handlers["delete_securityscorecard_portfolio"](portfolio_id="abc")
    assert out.startswith("Error")
    assert len(seen) == 1
