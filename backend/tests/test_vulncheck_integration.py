"""Tests for the VulnCheck integration provider.

Covers the credential chokepoint (Bearer header + blank-key guard), tool
registration and priorities, index-name / CVE-id input validation (the
path-injection guards), cursor pagination surfacing, the NVD-2 enriched ->
community fallback, and the KEV IS / IS NOT verdict.
"""

from __future__ import annotations

import pytest

from app.integrations.providers import vulncheck
from app.integrations.providers.vulncheck import (
    VulnCheckIntegration,
    build_tools,
    build_vulncheck_headers,
)
from app.services.integration_tools import _SafeIntegrationExecutor

_TOKEN = "vc_test_key"


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

    monkeypatch.setattr(vulncheck, "safe_request", _fake_safe_request)
    return captured


# ---------------------------------------------------------------------------
# Credential chokepoint + provider contract
# ---------------------------------------------------------------------------


def test_headers_strip_whitespace_and_use_bearer():
    headers = build_vulncheck_headers("  vc_key \n")
    assert headers["Authorization"] == "Bearer vc_key"
    assert headers["Accept"] == "application/json"


def test_blank_token_builds_no_tools():
    handlers, tools = _build("   ")
    assert tools == []
    assert handlers == {}


def test_provider_class_contract():
    integ = VulnCheckIntegration()
    assert integ.provider_name == "vulncheck"
    assert integ.auth_types == ["api_key"]
    assert integ.get_oauth_defaults() is None


def test_all_tools_registered():
    handlers, tools = _build()
    names = {t.name for t in tools}
    assert names == set(handlers)
    assert names == {
        "list_vulncheck_indexes",
        "query_vulncheck_index",
        "lookup_vulncheck_cve",
        "get_vulncheck_kev",
        "get_vulncheck_exploits",
        "get_vulncheck_epss",
        "query_vulncheck_advisory",
        "list_vulncheck_threat_actors",
        "list_vulncheck_ransomware",
        "list_vulncheck_botnets",
        "get_vulncheck_initial_access",
        "lookup_vulncheck_ip_intel",
        "get_vulncheck_ip_tag",
        "get_vulncheck_pdns_list",
        "get_vulncheck_cpe_cves",
        "search_vulncheck_cpe",
        "lookup_vulncheck_purls",
        "identify_vulncheck_product",
        "get_vulncheck_initial_access_rules",
    }
    # Every VulnCheck tool is a read.
    assert all(t.priority == 0 for t in tools)


# ---------------------------------------------------------------------------
# Input validation (path-injection guards)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cve_tools_reject_bad_ids(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run for an invalid CVE id")

    monkeypatch.setattr(vulncheck, "safe_request", _must_not_call)
    handlers, _ = _build()
    for bad in ("", "not-a-cve", "CVE-24-1", "CVE-2024-3400/../x"):
        assert "Error" in await handlers["lookup_vulncheck_cve"](cve=bad)
        assert "Error" in await handlers["get_vulncheck_exploits"](cve=bad)
        assert "Error" in await handlers["get_vulncheck_epss"](cve=bad)


@pytest.mark.asyncio
async def test_query_index_rejects_bad_index_name(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run for an invalid index name")

    monkeypatch.setattr(vulncheck, "safe_request", _must_not_call)
    handlers, _ = _build()
    for bad in ("", "../secrets", "index/with/slash", "UPPER?x=1"):
        assert "Error" in await handlers["query_vulncheck_index"](index=bad)


@pytest.mark.asyncio
async def test_advisory_requires_a_filter(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run without a filter")

    monkeypatch.setattr(vulncheck, "safe_request", _must_not_call)
    handlers, _ = _build()
    assert "at least one filter" in await handlers["query_vulncheck_advisory"]()
    # Whitespace-only inputs are stripped BEFORE the truthiness check, so they
    # can neither satisfy the guard nor be sent as empty-string filters.
    assert "at least one filter" in await handlers["query_vulncheck_advisory"](
        vendor="   ", product=" \t"
    )


@pytest.mark.asyncio
async def test_query_index_drops_whitespace_only_filters(monkeypatch):
    captured = _capture(monkeypatch, {"data": []})
    handlers, _ = _build()
    await handlers["query_vulncheck_index"](
        index="exploits", alias="   ", threat_actor=" ", cursor="  "
    )
    assert "alias" not in captured["params"]
    assert "threat_actor" not in captured["params"]
    # A whitespace-only cursor counts as absent -> first-page request.
    assert "cursor" not in captured["params"]
    assert captured["params"]["start_cursor"] == "true"


@pytest.mark.asyncio
async def test_advisory_clamps_non_numeric_limit_and_page(monkeypatch):
    captured = _capture(monkeypatch, {"data": [], "_meta": {}})
    handlers, _ = _build()
    await handlers["query_vulncheck_advisory"](vendor="cisco", limit="abc", page=-3)
    assert captured["params"]["limit"] == 10
    assert captured["params"]["page"] == 1


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_index_surfaces_cursor(monkeypatch):
    captured = _capture(
        monkeypatch,
        {
            "data": [{"id": "CVE-2024-1", "_timestamp": "2024-01-01T00:00:00Z"}],
            "_meta": {"total_documents": 500, "next_cursor": "CUR123"},
        },
    )
    handlers, _ = _build()
    result = await handlers["query_vulncheck_index"](index="exploits", limit=1)

    assert captured["url"].endswith("/v3/index/exploits")
    assert captured["params"]["start_cursor"] == "true"
    assert captured["headers"]["Authorization"] == f"Bearer {_TOKEN}"
    assert "CVE-2024-1" in result
    assert 'cursor="CUR123"' in result


@pytest.mark.asyncio
async def test_lookup_cve_falls_back_to_community_index(monkeypatch):
    calls: list[str] = []

    async def _fake_safe_request(client, method, url, provider, action, **kwargs):
        calls.append(url)
        if url.endswith("/vulncheck-nvd2"):
            # Not licensed for the enriched index.
            return None, "VulnCheck error: 402 subscription required"
        return (
            _FakeResp(
                200,
                {
                    "data": [
                        {
                            "id": "CVE-2024-3400",
                            "descriptions": [{"lang": "en", "value": "PAN-OS bug"}],
                            "metrics": {
                                "cvssMetricV31": [
                                    {"cvssData": {"baseScore": 10.0, "baseSeverity": "CRITICAL"}}
                                ]
                            },
                        }
                    ]
                },
            ),
            None,
        )

    monkeypatch.setattr(vulncheck, "safe_request", _fake_safe_request)
    handlers, _ = _build()
    result = await handlers["lookup_vulncheck_cve"](cve="cve-2024-3400")

    assert any(u.endswith("/vulncheck-nvd2") for u in calls)
    assert any(u.endswith("/nist-nvd2") for u in calls)
    assert "source: nist-nvd2" in result
    assert "PAN-OS bug" in result
    assert "10.0 (CRITICAL)" in result


@pytest.mark.asyncio
async def test_lookup_cve_reports_not_found_when_fallback_succeeds_empty(monkeypatch):
    """A 402 on the enriched index must not mask a truthful empty fallback."""

    async def _fake_safe_request(client, method, url, provider, action, **kwargs):
        if url.endswith("/vulncheck-nvd2"):
            return None, "VulnCheck error: 402 subscription required"
        return _FakeResp(200, {"data": []}), None

    monkeypatch.setattr(vulncheck, "safe_request", _fake_safe_request)
    handlers, _ = _build()
    result = await handlers["lookup_vulncheck_cve"](cve="CVE-1999-0001")
    assert "was not found" in result
    assert "402" not in result


@pytest.mark.asyncio
async def test_lookup_cve_surfaces_error_when_all_indexes_fail(monkeypatch):
    async def _fail(*args, **kwargs):
        return None, "VulnCheck error: 401 unauthorized"

    monkeypatch.setattr(vulncheck, "safe_request", _fail)
    handlers, _ = _build()
    result = await handlers["lookup_vulncheck_cve"](cve="CVE-2024-3400")
    assert "401" in result


@pytest.mark.asyncio
async def test_list_indexes_sorted_alphabetically(monkeypatch):
    _capture(
        monkeypatch,
        {
            "data": [
                {"name": "zeta", "description": "last"},
                {"name": "alpha", "description": "first"},
            ]
        },
    )
    handlers, _ = _build()
    result = await handlers["list_vulncheck_indexes"]()
    assert result.index("alpha") < result.index("zeta")


@pytest.mark.asyncio
async def test_get_user_info_counts_indexes(monkeypatch):
    _capture(monkeypatch, {"data": [{"name": "a"}, {"name": "b"}]})
    integ = VulnCheckIntegration()
    assert await integ.get_user_info(_TOKEN) == {"licensed_indexes": 2}


@pytest.mark.asyncio
async def test_validate_connection_and_user_info_degrade_on_error(monkeypatch):
    async def _fail(*args, **kwargs):
        return None, "VulnCheck error: 401 unauthorized"

    monkeypatch.setattr(vulncheck, "safe_request", _fail)
    integ = VulnCheckIntegration()
    # An invalid key must degrade to False / {} - never raise (safe_request
    # returns an error tuple instead of raising, and blank keys short-circuit).
    assert await integ.validate_connection(_TOKEN) is False
    assert await integ.get_user_info(_TOKEN) == {}
    assert await integ.validate_connection("   ") is False
    assert await integ.get_user_info("") == {}


@pytest.mark.asyncio
async def test_validate_connection_ok(monkeypatch):
    _capture(monkeypatch, {"data": []})
    integ = VulnCheckIntegration()
    assert await integ.validate_connection(_TOKEN) is True


@pytest.mark.asyncio
async def test_kev_reports_not_in_kev(monkeypatch):
    _capture(monkeypatch, {"data": [], "_meta": {"total_documents": 0}})
    handlers, _ = _build()
    result = await handlers["get_vulncheck_kev"](cve="CVE-2000-1234")
    assert "is NOT in the VulnCheck KEV" in result


@pytest.mark.asyncio
async def test_kev_reports_in_kev(monkeypatch):
    _capture(
        monkeypatch,
        {
            "data": [
                {
                    "cve": ["CVE-2024-3400"],
                    "vulnerabilityName": "PAN-OS Command Injection",
                    "vendorProject": "Palo Alto Networks",
                    "product": "PAN-OS",
                    "date_added": "2024-04-12",
                    "knownRansomwareCampaignUse": "Known",
                }
            ],
            "_meta": {"total_documents": 1},
        },
    )
    handlers, _ = _build()
    result = await handlers["get_vulncheck_kev"](cve="cve-2024-3400")
    assert "IS in the VulnCheck KEV" in result
    assert "PAN-OS" in result
    assert "Ransomware use: Known" in result


@pytest.mark.asyncio
async def test_ip_intel_validates_window_and_detection_id(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run for invalid enum input")

    monkeypatch.setattr(vulncheck, "safe_request", _must_not_call)
    handlers, _ = _build()
    assert "window must be one of" in await handlers["lookup_vulncheck_ip_intel"](
        window="7d"
    )
    assert "detection_id must be one of" in await handlers["lookup_vulncheck_ip_intel"](
        window="3d", detection_id="beacon"
    )


@pytest.mark.asyncio
async def test_ip_intel_targets_window_index(monkeypatch):
    captured = _capture(
        monkeypatch,
        {
            "data": [
                {
                    "ip": "1.2.3.4",
                    "country": "China",
                    "country_code": "CN",
                    "asn": "AS4134",
                    "last_seen": "2026-07-01",
                    "matches": ["Cobalt Strike"],
                }
            ],
            "_meta": {"total_documents": 1},
        },
    )
    handlers, _ = _build()
    result = await handlers["lookup_vulncheck_ip_intel"](
        window="30d", detection_id="c2", country_code="CN"
    )
    assert captured["url"].endswith("/v3/index/ipintel-30d")
    assert captured["params"]["id"] == "c2"
    assert "1.2.3.4" in result
    assert "Cobalt Strike" in result


@pytest.mark.asyncio
async def test_ip_tag_rejects_bad_name_and_parses_lines(monkeypatch):
    handlers, _ = _build()

    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run for an invalid tag name")

    monkeypatch.setattr(vulncheck, "safe_request", _must_not_call)
    assert "Error" in await handlers["get_vulncheck_ip_tag"](tag="../etc/passwd")

    class _TextResp:
        status_code = 200
        text = "1.1.1.1\n2.2.2.2\n\n3.3.3.3\n"

    async def _fake(client, method, url, provider, action, **kwargs):
        assert url.endswith("/v3/tags/vulncheck-c2")
        return _TextResp(), None

    monkeypatch.setattr(vulncheck, "safe_request", _fake)
    result = await handlers["get_vulncheck_ip_tag"](tag="vulncheck-c2")
    assert "3 IP address(es)" in result
    assert "1.1.1.1" in result and "3.3.3.3" in result


@pytest.mark.asyncio
async def test_ip_tag_degrades_non_numeric_limit(monkeypatch):
    """A hallucinated non-numeric limit falls back to the default, never raises."""

    class _TextResp:
        status_code = 200
        text = "\n".join(f"10.0.0.{i}" for i in range(1, 4))

    async def _fake(client, method, url, provider, action, **kwargs):
        return _TextResp(), None

    monkeypatch.setattr(vulncheck, "safe_request", _fake)
    handlers, _ = _build()
    result = await handlers["get_vulncheck_ip_tag"](tag="vulncheck-c2", limit="lots")
    assert "3 IP address(es)" in result
    result = await handlers["get_vulncheck_pdns_list"](tag="vulncheck-c2", limit=2)
    assert "[Showing 2 of 3" in result


@pytest.mark.asyncio
async def test_threat_actors_filters_and_formats(monkeypatch):
    captured = _capture(
        monkeypatch,
        {
            "data": [
                {
                    "threat_actor_name": "UNC2630",
                    "date_added": "2024-01-01",
                    "cve_references": [{"cve": ["CVE-2021-22893"]}],
                }
            ],
            "_meta": {"total_documents": 1},
        },
    )
    handlers, _ = _build()
    result = await handlers["list_vulncheck_threat_actors"](mitre_id="G0013")
    assert captured["url"].endswith("/v3/index/threat-actors")
    assert captured["params"]["mitre_id"] == "G0013"
    assert "UNC2630" in result
    assert "CVE-2021-22893" in result


@pytest.mark.asyncio
async def test_threat_actors_omit_mitre_when_absent(monkeypatch):
    _capture(
        monkeypatch,
        {
            "data": [
                {
                    "threat_actor_name": "APT41",
                    "date_added": "2024-01-01",
                    "misp_threat_actor": {"meta": {"country": "CN"}},
                },
                {
                    "threat_actor_name": "Sandworm",
                    "date_added": "2024-02-02",
                    "misp_threat_actor": {"meta": {"mitre-attack": "G0034"}},
                },
            ],
            "_meta": {"total_documents": 2},
        },
    )
    handlers, _ = _build()
    result = await handlers["list_vulncheck_threat_actors"]()
    assert "MITRE: None" not in result
    assert "MITRE: G0034" in result


@pytest.mark.asyncio
async def test_cpe_cves_validates_and_lists(monkeypatch):
    handlers, _ = _build()

    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run for an invalid CPE")

    monkeypatch.setattr(vulncheck, "safe_request", _must_not_call)
    assert "Error" in await handlers["get_vulncheck_cpe_cves"](cpe="not-a-cpe")

    _capture(
        monkeypatch,
        {"data": ["CVE-2010-0246", "CVE-2010-0248"], "_meta": {"total_documents": 2}},
    )
    result = await handlers["get_vulncheck_cpe_cves"](
        cpe="cpe:/a:microsoft:internet_explorer:8.0.6001:beta"
    )
    assert "2 CVE(s)" in result
    assert "CVE-2010-0246" in result


@pytest.mark.asyncio
async def test_search_cpe_requires_a_field_and_validates_part(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run for invalid search input")

    monkeypatch.setattr(vulncheck, "safe_request", _must_not_call)
    handlers, _ = _build()
    assert "at least one" in await handlers["search_vulncheck_cpe"]()
    assert "part must be one of" in await handlers["search_vulncheck_cpe"](
        vendor="x", part="z"
    )
    # Whitespace-only inputs are stripped BEFORE the truthiness check, so they
    # can neither satisfy the guard nor be sent as empty-string params.
    assert "at least one" in await handlers["search_vulncheck_cpe"](
        vendor="   ", product=" \t"
    )


@pytest.mark.asyncio
async def test_identify_drops_whitespace_only_fields(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run without a query field")

    monkeypatch.setattr(vulncheck, "safe_request", _must_not_call)
    handlers, _ = _build()
    assert "at least one" in await handlers["identify_vulncheck_product"](
        vendor="  ", product=" \t", version="   "
    )


@pytest.mark.asyncio
async def test_purls_posts_list_and_formats(monkeypatch):
    captured = _capture(
        monkeypatch,
        {
            "data": [
                {
                    "purl": "pkg:hex/coherence@0.1.2",
                    "cves": ["CVE-2018-20301"],
                    "vulnerabilities": [
                        {"detection": "CVE-2018-20301", "fixed_version": "0.5.2"}
                    ],
                }
            ],
            "_meta": {"total_documents": 1},
        },
    )
    handlers, _ = _build()
    result = await handlers["lookup_vulncheck_purls"](
        purls=["pkg:hex/coherence@0.1.2", "  "]
    )
    assert captured["method"] == "post"
    assert captured["json"] == ["pkg:hex/coherence@0.1.2"]
    assert "CVE-2018-20301" in result
    assert "fixed in 0.5.2" in result


@pytest.mark.asyncio
async def test_identify_requires_a_field_and_formats_identifiers(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run without a query field")

    monkeypatch.setattr(vulncheck, "safe_request", _must_not_call)
    handlers, _ = _build()
    assert "at least one" in await handlers["identify_vulncheck_product"]()

    # The endpoint returns a bare JSON array (not the {"data": ...} envelope).
    captured: dict = {}

    class _ListResp:
        status_code = 200

        def json(self):
            return [
                {
                    "identity": {"vendor": "microsoft", "product": "office", "version": "2016"},
                    "identifiers": {
                        "cpe": [
                            {
                                "value": "cpe:2.3:a:microsoft:office:2016:*:*:*:*:*:*:*",
                                "confidence": {"level": "medium"},
                            }
                        ],
                        "purl": ["pkg:generic/office@2016"],
                    },
                }
            ]

    async def _fake(client, method, url, provider, action, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _ListResp(), None

    monkeypatch.setattr(vulncheck, "safe_request", _fake)
    result = await handlers["identify_vulncheck_product"](
        vendor="Microsoft Corporation", product="Office 2016"
    )
    assert captured["url"].endswith("/v3/identify")
    assert captured["params"]["vendor"] == "Microsoft Corporation"
    assert "microsoft office 2016" in result
    assert "cpe:2.3:a:microsoft:office:2016" in result
    assert "[confidence: medium]" in result
    # Bare-string PURL entries must render without crashing.
    assert "pkg:generic/office@2016" in result


@pytest.mark.asyncio
async def test_initial_access_rules_validates_type(monkeypatch):
    handlers, _ = _build()

    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run for an invalid rule type")

    monkeypatch.setattr(vulncheck, "safe_request", _must_not_call)
    assert "Error" in await handlers["get_vulncheck_initial_access_rules"](
        rule_type="yara"
    )

    class _TextResp:
        status_code = 200
        text = "# comment\nalert tcp any any -> any any (msg:\"rule 1\";)\nalert tcp any any -> any any (msg:\"rule 2\";)\n"

    async def _fake(client, method, url, provider, action, **kwargs):
        assert url.endswith("/v3/rules/initial-access/suricata")
        return _TextResp(), None

    monkeypatch.setattr(vulncheck, "safe_request", _fake)
    result = await handlers["get_vulncheck_initial_access_rules"](rule_type="suricata")
    assert "2 total" in result
    assert "rule 1" in result
    # A hallucinated non-numeric limit falls back to the default, never raises.
    result = await handlers["get_vulncheck_initial_access_rules"](
        rule_type="suricata", limit="lots"
    )
    assert "2 total" in result


@pytest.mark.asyncio
async def test_purls_accepts_bare_string_input(monkeypatch):
    """A single PURL passed as a bare string is not iterated char-by-char."""
    captured = _capture(monkeypatch, {"data": [], "_meta": {}})
    handlers, _ = _build()
    await handlers["lookup_vulncheck_purls"](purls="pkg:npm/lodash@4.17.20")
    assert captured["json"] == ["pkg:npm/lodash@4.17.20"]


@pytest.mark.asyncio
async def test_exploits_formats_intelligence(monkeypatch):
    _capture(
        monkeypatch,
        {
            "data": [
                {
                    "id": "CVE-2023-50387",
                    "public_exploit_found": True,
                    "weaponized_exploit_found": False,
                    "max_exploit_maturity": "poc",
                    "reported_exploited_by_ransomware": False,
                    "inKEV": False,
                    "inVCKEV": True,
                    "counts": {"exploits": 1},
                    "epss": {"epss_score": 0.038, "epss_percentile": 0.9159},
                    "exploits": [
                        {
                            "name": "knqyf263 PoC",
                            "url": "https://github.com/knqyf263/CVE-2023-50387",
                            "exploit_maturity": "poc",
                        }
                    ],
                }
            ]
        },
    )
    handlers, _ = _build()
    result = await handlers["get_vulncheck_exploits"](cve="CVE-2023-50387")
    assert "CVE-2023-50387 exploit intelligence" in result
    assert "In VulnCheck KEV: True" in result
    assert "knqyf263 PoC" in result
    assert "0.038" in result
