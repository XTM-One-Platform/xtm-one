"""Tests for the GreyNoise integration provider.

Covers the credential chokepoint (header building + blank-key guard), tool
registration, the combined ``/v3/ip`` response formatting, GNQL scroll
pagination, strict IP / CVE id / workspace-UUID input validation, and the
consolidated blocklist tool's validation.
"""

from __future__ import annotations

import pytest

from app.integrations.providers import greynoise
from app.integrations.providers.greynoise import (
    GreyNoiseIntegration,
    build_greynoise_headers,
    build_tools,
)
from app.services.integration_tools import _SafeIntegrationExecutor

_TOKEN = "gn_test_key"


class _FakeResp:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


def _build(token: str = _TOKEN):
    executor = _SafeIntegrationExecutor()
    tools = build_tools(token, executor)
    return executor._handlers, tools


def _capture(monkeypatch, payload: dict | None = None, status: int = 200) -> dict:
    """Patch safe_request to capture call args and return a canned response."""
    captured: dict = {}

    async def _fake_safe_request(client, method, url, provider, action, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured.update(kwargs)
        return _FakeResp(status, payload or {}), None

    monkeypatch.setattr(greynoise, "safe_request", _fake_safe_request)
    return captured


# ---------------------------------------------------------------------------
# Credential chokepoint
# ---------------------------------------------------------------------------


def test_headers_strip_whitespace_and_use_key_header():
    headers = build_greynoise_headers("  gn_key \n")
    assert headers["key"] == "gn_key"
    assert headers["Accept"] == "application/json"


def test_blank_token_builds_no_tools():
    handlers, tools = _build("   ")
    assert tools == []
    assert handlers == {}


def test_provider_class_contract():
    integ = GreyNoiseIntegration()
    assert integ.provider_name == "greynoise"
    assert integ.auth_types == ["api_key"]
    assert integ.get_oauth_defaults() is None


def test_all_tools_registered():
    handlers, tools = _build()
    names = {t.name for t in tools}
    assert names == set(handlers)
    assert names == {
        "get_greynoise_status",
        "lookup_greynoise_ip",
        "lookup_greynoise_ips",
        "lookup_greynoise_community_ip",
        "query_greynoise_gnql",
        "get_greynoise_gnql_stats",
        "get_greynoise_ip_timeline",
        "lookup_greynoise_cve",
        "list_greynoise_cves",
        "list_greynoise_tags",
        "manage_greynoise_blocklist",
    }
    # Reads sort before the mixed blocklist manage tool.
    by_name = {t.name: t for t in tools}
    assert by_name["lookup_greynoise_ip"].priority == 0
    assert by_name["manage_greynoise_blocklist"].priority == 1


# ---------------------------------------------------------------------------
# IP lookup formatting
# ---------------------------------------------------------------------------


_FULL_IP_PAYLOAD = {
    "ip": "64.39.104.22",
    "business_service_intelligence": {
        "found": True,
        "category": "vulnerability_management",
        "name": "Qualys",
        "trust_level": "1",
        "explanation": "Deliberate vulnerability scanning traffic.",
    },
    "internet_scanner_intelligence": {
        "found": True,
        "classification": "benign",
        "actor": "Qualys",
        "first_seen": "2025-05-25",
        "last_seen": "2025-05-25",
        "spoofable": True,
        "tags": [
            {"name": "Qualys", "intention": "benign"},
        ],
        "cves": [],
        "metadata": {
            "asn": "AS27385",
            "organization": "QUALYS, Inc.",
            "source_country": "United States",
            "category": "business",
            "rdns": "",
        },
    },
    "request_metadata": {"restricted_fields": []},
}


@pytest.mark.asyncio
async def test_lookup_ip_formats_combined_response(monkeypatch):
    captured = _capture(monkeypatch, _FULL_IP_PAYLOAD)
    handlers, _ = _build()
    result = await handlers["lookup_greynoise_ip"](ip="64.39.104.22")

    assert captured["url"].endswith("/v3/ip/64.39.104.22")
    assert captured["headers"]["key"] == _TOKEN
    assert "benign" in result
    assert "Qualys" in result
    assert "trust level 1" in result
    assert "spoofable" in result


@pytest.mark.asyncio
async def test_lookup_ip_rejects_invalid_addresses(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run for an invalid IP")

    monkeypatch.setattr(greynoise, "safe_request", _must_not_call)
    handlers, _ = _build()
    for bad in ("", "..", "1.2.3.4/../admin", "1.2.3.4?x=1", "not-an-ip", "999.1.1.1"):
        result = await handlers["lookup_greynoise_ip"](ip=bad)
        assert "Error" in result
        result = await handlers["lookup_greynoise_community_ip"](ip=bad)
        assert "Error" in result
        result = await handlers["get_greynoise_ip_timeline"](ip=bad)
        assert "Error" in result


@pytest.mark.asyncio
async def test_lookup_ip_accepts_ipv6(monkeypatch):
    captured = _capture(monkeypatch, _FULL_IP_PAYLOAD)
    handlers, _ = _build()
    await handlers["lookup_greynoise_ip"](ip="2001:db8::1")
    assert captured["url"].endswith("/v3/ip/2001:db8::1")


@pytest.mark.asyncio
async def test_multi_lookup_reports_not_found_and_caps_batch(monkeypatch):
    captured = _capture(
        monkeypatch,
        {
            "data": [
                {
                    "ip": "8.8.8.8",
                    "internet_scanner_intelligence": {"found": False},
                    "business_service_intelligence": {"found": True, "trust_level": "1"},
                }
            ],
            "request_metadata": {"ips_not_found": ["10.0.0.1"]},
        },
    )
    handlers, _ = _build()
    result = await handlers["lookup_greynoise_ips"](
        ips=["8.8.8.8", "10.0.0.1", "not-an-ip"]
    )

    assert captured["method"] == "post"
    # Invalid entries are dropped from the request and reported in the output.
    assert captured["json"] == {"ips": ["8.8.8.8", "10.0.0.1"]}
    assert "8.8.8.8" in result
    assert "Not found in GreyNoise (1): 10.0.0.1" in result
    assert "Skipped 1 invalid IP(s): not-an-ip" in result

    # Over-limit batches are rejected before any request is made.
    result = await handlers["lookup_greynoise_ips"](
        ips=[f"10.0.0.{i % 250}" for i in range(1001)]
    )
    assert "too many IPs" in result


# ---------------------------------------------------------------------------
# GNQL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gnql_query_paginates_with_scroll(monkeypatch):
    captured = _capture(
        monkeypatch,
        {
            "data": [
                {
                    "ip": "1.2.3.4",
                    "internet_scanner_intelligence": {
                        "found": True,
                        "classification": "malicious",
                        "actor": "unknown",
                        "last_seen": "2026-07-01",
                        "tags": [{"name": "RDP Scanner"}],
                        "metadata": {
                            "organization": "Evil Hosting",
                            "source_country_code": "XX",
                        },
                    },
                }
            ],
            "request_metadata": {"count": 5000, "complete": False, "scroll": "tok123"},
        },
    )
    handlers, _ = _build()
    result = await handlers["query_greynoise_gnql"](query="classification:malicious")

    assert captured["params"]["query"] == "classification:malicious"
    assert captured["params"]["exclude"] == "raw_data"
    assert "1.2.3.4" in result
    assert "RDP Scanner" in result
    assert 'scroll="tok123"' in result  # pagination hint surfaced


@pytest.mark.asyncio
async def test_gnql_requires_a_query(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run without a query")

    monkeypatch.setattr(greynoise, "safe_request", _must_not_call)
    handlers, _ = _build()
    assert "Error" in await handlers["query_greynoise_gnql"](query="  ")
    assert "Error" in await handlers["get_greynoise_gnql_stats"](query="")


# ---------------------------------------------------------------------------
# CVE lookup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cve_lookup_validates_and_reports_404(monkeypatch):
    handlers, _ = _build()

    for bad in ("not-a-cve", "CVE-24-1", "CVE-2024-3400/../x"):
        result = await handlers["lookup_greynoise_cve"](cve_id=bad)
        assert "Error" in result

    async def _fake_404(client, method, url, provider, action, **kwargs):
        return _FakeResp(404, {"message": "CVE not found"}), "GreyNoise error: 404"

    monkeypatch.setattr(greynoise, "safe_request", _fake_404)
    result = await handlers["lookup_greynoise_cve"](cve_id="CVE-2024-99999")
    assert "not tracked by GreyNoise" in result


@pytest.mark.asyncio
async def test_cve_lookup_formats_exploitation_intel(monkeypatch):
    _capture(
        monkeypatch,
        {
            "id": "CVE-2024-3400",
            "details": {
                "vulnerability_name": "PAN-OS Command Injection",
                "vendor": "Palo Alto Networks",
                "product": "PAN-OS",
                "cve_cvss_score": 10.0,
            },
            "timeline": {"cisa_kev_date_added": "2024-04-12"},
            "exploitation_details": {
                "attack_vector": "Network",
                "exploit_found": True,
                "epss_score": 0.96,
            },
            "exploitation_activity": {
                "activity_seen": True,
                "threat_ip_count_30d": 42,
            },
        },
    )
    handlers, _ = _build()
    result = await handlers["lookup_greynoise_cve"](cve_id="cve-2024-3400")
    assert "CVE-2024-3400" in result
    assert "PAN-OS" in result
    assert "2024-04-12" in result
    assert "cve:CVE-2024-3400" in result  # GNQL follow-up hint


# ---------------------------------------------------------------------------
# Blocklist manage tool validation
# ---------------------------------------------------------------------------


_WS_UUID = "0e1f2a3b-4c5d-6e7f-8a9b-0c1d2e3f4a5b"


@pytest.mark.asyncio
async def test_blocklist_validates_before_any_request(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run for invalid input")

    monkeypatch.setattr(greynoise, "safe_request", _must_not_call)
    handlers, _ = _build()
    manage = handlers["manage_greynoise_blocklist"]

    assert "action must be one of" in await manage(action="explode", workspace_id=_WS_UUID)
    assert "workspace_id" in await manage(action="list", workspace_id="")
    # Non-UUID ids are rejected before any request is made.
    assert "workspace_id" in await manage(action="list", workspace_id="ws-1")
    assert "blocklist_id" in await manage(action="delete", workspace_id=_WS_UUID)
    assert "blocklist_id" in await manage(
        action="delete", workspace_id=_WS_UUID, blocklist_id="not-a-uuid"
    )
    assert "GNQL query is required" in await manage(action="create", workspace_id=_WS_UUID)


@pytest.mark.asyncio
async def test_blocklist_create_posts_expected_body(monkeypatch):
    captured = _capture(
        monkeypatch, {"id": "bl-1", "name": "Malicious scanners"}
    )
    handlers, _ = _build()
    result = await handlers["manage_greynoise_blocklist"](
        action="create",
        workspace_id=_WS_UUID,
        query="classification:malicious last_seen:1d",
        name="Malicious scanners",
        ip_limit=500,
    )

    assert captured["method"] == "post"
    assert captured["url"].endswith(f"/v3/workspaces/{_WS_UUID}/blocklists")
    assert captured["json"] == {
        "query": "classification:malicious last_seen:1d",
        "name": "Malicious scanners",
        "ip_limit": 500,
    }
    assert "Blocklist created" in result
