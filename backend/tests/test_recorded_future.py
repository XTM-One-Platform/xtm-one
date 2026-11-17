"""Tests for the Recorded Future integration provider.

Covers tool introspection (exact count across the enrichment / search / alerts
/ cases / intelligence / management domains), the X-RFToken header + base-URL helpers,
the Connect API entity lookup (risk + Intelligence Card formatting + the benign
404 "no intelligence" path), SOAR batch enrichment and verdict triage (body
shape + context guard), entity search (risk-score filter), entity-match
resolution, classic + playbook alert updates (validation guards), consolidated
list management, Collective Insights submission, and Identity Intelligence
search. Every API call routes through the shared ``safe_request`` helper, which
is monkeypatched on each domain module so no network is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers.recorded_future import (
    alerts,
    build_rf_headers,
    build_tools,
    cases,
    enrichment,
    intelligence,
    management,
    rf_api_base,
    search,
)
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://api.recordedfuture.com||rftoken-123"
_DOMAIN_MODULES = (enrichment, search, alerts, cases, intelligence, management)


class _StubResponse:
    def __init__(self, payload: Any, status: int = 200):
        self._payload = payload
        self.status_code = status
        self.is_success = 200 <= status < 300

    def json(self) -> Any:
        return self._payload


class _Recorder:
    """Fake ``safe_request`` that routes by (method, url) and records calls.

    A router may return a bare payload (treated as HTTP 200) or a
    ``(payload, status)`` tuple to simulate an error; the ``expected_statuses``
    contract of the real helper is honoured (the failed response is returned so
    callers can branch on ``status_code``).
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
    tools = build_tools(token="https://api.recordedfuture.com||__dummy__", executor=executor)
    names = {t.name for t in tools}
    for expected in (
        "lookup_rf_ip",
        "lookup_rf_vulnerability",
        "enrich_rf_indicators",
        "triage_rf_indicators",
        "search_rf_entities",
        "resolve_rf_entity",
        "list_rf_alerts",
        "update_rf_alerts",
        "search_rf_playbook_alerts",
        "search_rf_analyst_notes",
        "search_rf_detection_rules",
        "search_rf_links",
        "get_rf_threat_map",
        "get_rf_malware_threat_map",
        "get_rf_risk_history",
        "publish_rf_analyst_note",
        "query_rf_malware_intelligence",
        "manage_rf_list",
        "submit_rf_collective_insights",
        "search_rf_collective_insights",
        "search_rf_leaked_credentials",
        "lookup_rf_identity_exposures",
        "check_rf_password_exposure",
        "lookup_rf_entity",
        "get_rf_playbook_alert_details",
        "search_rf_cases",
        "get_rf_case",
        "create_rf_case",
        "update_rf_case",
    ):
        assert expected in names
    # Exact count - 41 tools across the six domains. Assert the precise number
    # so an accidentally dropped or duplicated tool fails the build.
    assert len(tools) == 41
    # Every tool advertises the provider source.
    assert all(t.source == "integration:recorded_future" for t in tools)


def test_build_tools_empty_without_token():
    executor = CallableToolExecutor()
    assert build_tools(token="https://api.recordedfuture.com||", executor=executor) == []


def test_build_tools_empty_with_whitespace_only_token():
    executor = CallableToolExecutor()
    assert build_tools(token="https://api.recordedfuture.com||   ", executor=executor) == []


def test_headers_and_base_url_helpers():
    headers = build_rf_headers("abc")
    assert headers["X-RFToken"] == "abc"
    assert headers["Accept"] == "application/json"
    # The token is stripped so pasted whitespace/newlines can't produce a
    # malformed (h11-rejected) X-RFToken header.
    assert build_rf_headers("  abc\n")["X-RFToken"] == "abc"
    # default + trailing-slash + whitespace normalisation
    assert rf_api_base("") == "https://api.recordedfuture.com"
    assert rf_api_base("   ") == "https://api.recordedfuture.com"
    assert rf_api_base("https://api.recordedfuture.com/") == "https://api.recordedfuture.com"
    assert rf_api_base("  https://gov.recordedfuture.com/  ") == "https://gov.recordedfuture.com"


@pytest.mark.asyncio
async def test_build_tools_defaults_host_when_base_url_blank(monkeypatch):
    """A blank base_url ("||<token>") still exposes tools, defaulting to the public host."""
    recorder = _Recorder(lambda m, u: {"data": {"entity": {"name": "8.8.8.8"}}})
    for module in _DOMAIN_MODULES:
        monkeypatch.setattr(module, "safe_request", recorder)
    executor = CallableToolExecutor()
    tools = build_tools(token="||rftoken-123", executor=executor)
    assert tools, "blank base_url should fall back to the public host"
    await executor.execute_tool(
        ToolCall(id="d1", name="lookup_rf_ip", arguments={"ip": "8.8.8.8"})
    )
    assert recorder.calls[0]["url"] == "https://api.recordedfuture.com/v2/ip/8.8.8.8"


# ---------------------------------------------------------------------------
# Enrichment - lookups
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_ip_formats_risk_and_intel_card(monkeypatch):
    card = "https://app.recordedfuture.com/live/sc/entity/ip:45.207.192.190"

    def router(method, url):
        return {
            "data": {
                "entity": {
                    "id": "ip:45.207.192.190",
                    "name": "45.207.192.190",
                    "type": "IpAddress",
                },
                "risk": {
                    "score": 99,
                    "criticalityLabel": "Very Malicious",
                    "riskString": "12/87",
                    "evidenceDetails": [
                        {
                            "rule": "Validated C&C Server",
                            "criticality": 4,
                            "criticalityLabel": "Very Malicious",
                            "evidenceString": "Validated Cobalt Strike C2.",
                        }
                    ],
                },
                "intelCard": card,
            }
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="l1", name="lookup_rf_ip", arguments={"ip": "45.207.192.190"})
    )
    assert not result.is_error
    assert "99/99" in result.content
    assert "Very Malicious" in result.content
    assert "Validated C&C Server" in result.content
    assert card in result.content
    call = recorder.calls[0]
    assert call["url"] == "https://api.recordedfuture.com/v2/ip/45.207.192.190"
    assert "risk" in call["params"]["fields"]


@pytest.mark.asyncio
async def test_lookup_ip_404_returns_no_intelligence(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: ({"message": "not found"}, 404)
    )
    result = await executor.execute_tool(
        ToolCall(id="l2", name="lookup_rf_ip", arguments={"ip": "10.0.0.1"})
    )
    assert not result.is_error
    assert "no recorded future intelligence available" in result.content.lower()
    # The lookup opts into seeing the 404 response via expected_statuses.
    assert recorder.calls[0]["expected_statuses"] == {404}


@pytest.mark.asyncio
async def test_lookup_url_is_percent_encoded(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"data": {"entity": {"name": "x"}}})
    await executor.execute_tool(
        ToolCall(
            id="l3",
            name="lookup_rf_url",
            arguments={"url": "https://bad.example/a?b=c"},
        )
    )
    # Slashes/colons in the URL are percent-encoded so they stay in one path segment.
    assert "/v2/url/https%3A%2F%2Fbad.example" in recorder.calls[0]["url"]


# ---------------------------------------------------------------------------
# Enrichment - SOAR batch + triage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_indicators_builds_grouped_body(monkeypatch):
    def router(method, url):
        return {
            "data": {
                "results": [
                    {
                        "entity": {"name": "1.2.3.4", "type": "IpAddress"},
                        "risk": {"score": 90, "rule": {"count": 5, "mostCritical": "C2"}},
                    }
                ]
            },
            "counts": {"returned": 1, "total": 1},
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="s1",
            name="enrich_rf_indicators",
            arguments={"ips": "1.2.3.4", "vulnerabilities": "CVE-2021-44228"},
        )
    )
    assert not result.is_error
    assert "1.2.3.4" in result.content
    assert "90/99" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/soar/v3/enrichment")
    assert call["json"]["ip"] == ["1.2.3.4"]
    assert call["json"]["vulnerability"] == ["CVE-2021-44228"]


@pytest.mark.asyncio
async def test_enrich_indicators_requires_input(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="s2", name="enrich_rf_indicators", arguments={})
    )
    assert "provide at least one indicator" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_triage_rejects_invalid_context(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="s3",
            name="triage_rf_indicators",
            arguments={"context": "nuke", "ips": "1.2.3.4"},
        )
    )
    assert "context must be one of" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_triage_formats_verdict(monkeypatch):
    def router(method, url):
        return {
            "data": {
                "results": [
                    {"entity": {"name": "1.2.3.4", "type": "IpAddress"}, "verdict": True, "score": 90}
                ]
            }
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="s4",
            name="triage_rf_indicators",
            arguments={"context": "c2", "ips": "1.2.3.4"},
        )
    )
    assert not result.is_error
    assert "MALICIOUS" in result.content
    assert recorder.calls[0]["url"].endswith("/soar/v3/triage/contexts/c2")


@pytest.mark.asyncio
async def test_detection_rules_rejects_invalid_rule_types(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="dr1",
            name="search_rf_detection_rules",
            arguments={"rule_types": "yera"},
        )
    )
    assert "rule_types must be a subset" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_triage_distinguishes_clean_from_unknown_verdict(monkeypatch):
    def router(method, url):
        return {
            "data": {
                "results": [
                    {"entity": {"name": "1.1.1.1", "type": "IpAddress"}, "verdict": False, "score": 5},
                    {"entity": {"name": "2.2.2.2", "type": "IpAddress"}, "score": 0},
                ]
            }
        }

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="s5",
            name="triage_rf_indicators",
            arguments={"context": "malware", "ips": "1.1.1.1,2.2.2.2"},
        )
    )
    assert not result.is_error
    # verdict False -> clean; missing verdict -> unknown (never silently "clean").
    assert "clean" in result.content
    assert "unknown" in result.content
    assert "MALICIOUS" not in result.content


# ---------------------------------------------------------------------------
# Search + resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_entities_builds_risk_filter(monkeypatch):
    def router(method, url):
        return {
            "data": {
                "results": [
                    {
                        "entity": {"id": "ip:1.2.3.4", "name": "1.2.3.4", "type": "IpAddress"},
                        "risk": {"score": 80, "criticalityLabel": "Malicious"},
                    }
                ]
            },
            "counts": {"total": 1},
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="se1",
            name="search_rf_entities",
            arguments={"entity_type": "ip", "min_risk_score": 65},
        )
    )
    assert not result.is_error
    assert "1.2.3.4" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/v2/ip/search")
    assert call["params"]["riskScore"] == "[65,]"


@pytest.mark.asyncio
async def test_search_entities_clamps_out_of_range_risk_score(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"data": {"results": []}, "counts": {"total": 0}}
    )
    result = await executor.execute_tool(
        ToolCall(
            id="se_clamp",
            name="search_rf_entities",
            arguments={"entity_type": "ip", "min_risk_score": 1000},
        )
    )
    assert not result.is_error
    # An out-of-range floor is clamped into RF's documented 0-99 band, not
    # forwarded verbatim as "[1000,]" (which would error or return nothing).
    assert recorder.calls[0]["params"]["riskScore"] == "[99,]"


@pytest.mark.asyncio
async def test_search_entities_rejects_bad_type(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="se2", name="search_rf_entities", arguments={"entity_type": "router"})
    )
    assert "entity_type must be one of" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_resolve_entity_posts_name(monkeypatch):
    def router(method, url):
        return [{"id": "JLHNoH", "name": "Cobalt Strike", "type": "Malware"}]

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="r1",
            name="resolve_rf_entity",
            arguments={"name": "Cobalt Strike", "entity_types": "Malware"},
        )
    )
    assert not result.is_error
    assert "JLHNoH" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/entity-match/match")
    assert call["json"]["name"] == "Cobalt Strike"
    assert call["json"]["type"] == ["Malware"]


# ---------------------------------------------------------------------------
# Alerts (classic + playbook)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_alerts_builds_array_body(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"success": [{"id": "A1"}], "error": []})
    result = await executor.execute_tool(
        ToolCall(
            id="a1",
            name="update_rf_alerts",
            arguments={"alert_ids": "A1, A2", "status": "Resolved", "note": "Triaged"},
        )
    )
    assert not result.is_error
    call = recorder.calls[0]
    assert call["url"].endswith("/alert/v2/update")
    assert [e["id"] for e in call["json"]] == ["A1", "A2"]
    assert call["json"][0]["statusInPortal"] == "Resolved"
    assert call["json"][0]["note"] == "Triaged"


@pytest.mark.asyncio
async def test_update_alerts_rejects_bad_status(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="a2",
            name="update_rf_alerts",
            arguments={"alert_ids": "A1", "status": "Nuke"},
        )
    )
    assert "status must be one of" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_list_alerts_rejects_bad_status(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="a3", name="list_rf_alerts", arguments={"status": "Nuke"})
    )
    assert "status must be one of" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_search_playbook_alerts_filters(monkeypatch):
    def router(method, url):
        return {
            "data": [
                {
                    "playbook_alert_id": "task:123",
                    "category": "domain_abuse",
                    "status": "New",
                    "priority": "High",
                    "title": "Lookalike domain",
                    "updated": "2026-06-01T00:00:00Z",
                }
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="p1",
            name="search_rf_playbook_alerts",
            arguments={"category": "domain_abuse", "statuses": "New", "priority": "High"},
        )
    )
    assert not result.is_error
    assert "task:123" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/playbook-alert/search")
    assert call["json"]["category"] == ["domain_abuse"]
    assert call["json"]["statuses"] == ["New"]
    assert call["json"]["priority"] == ["High"]


@pytest.mark.asyncio
async def test_search_playbook_alerts_rejects_bad_statuses(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="p3", name="search_rf_playbook_alerts", arguments={"statuses": "bogus"}
        )
    )
    assert "statuses must be a subset" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_update_playbook_alert_rejects_bad_status(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="p2",
            name="update_rf_playbook_alert",
            arguments={"playbook_alert_id": "task:1", "status": "Closed"},
        )
    )
    assert "status must be one of" in result.content.lower()
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Intelligence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detection_rules_filter_by_type_and_entity(monkeypatch):
    def router(method, url):
        return {
            "total_count": 1,
            "result": [
                {"id": "doc:abc", "type": "yara", "title": "Hunt Log4Shell", "rules": []}
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="i1",
            name="search_rf_detection_rules",
            arguments={"rule_types": "yara,sigma", "entity_ids": "kvXvR5"},
        )
    )
    assert not result.is_error
    assert "Hunt Log4Shell" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/detection-rule/search")
    assert call["json"]["filter"]["types"] == ["yara", "sigma"]
    assert call["json"]["filter"]["entities"] == ["kvXvR5"]


@pytest.mark.asyncio
async def test_links_requires_entities(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="i2", name="search_rf_links", arguments={"entity_ids": ""})
    )
    assert "at least one rf entity id" in result.content.lower()
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Management (lists, collective insights, identity)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_list_create(monkeypatch):
    def router(method, url):
        return {"id": "report:abc", "name": "Block list", "type": "ip"}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="m1",
            name="manage_rf_list",
            arguments={"action": "create", "name": "Block list", "list_type": "ip"},
        )
    )
    assert not result.is_error
    assert "report:abc" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/list/create")
    assert call["json"] == {"name": "Block list", "type": "ip"}


@pytest.mark.asyncio
async def test_manage_list_add_requires_entity(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="m2",
            name="manage_rf_list",
            arguments={"action": "add", "list_id": "report:abc"},
        )
    )
    assert "provide entity_id" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_list_add_by_id(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"result": "added"})
    result = await executor.execute_tool(
        ToolCall(
            id="m3",
            name="manage_rf_list",
            arguments={
                "action": "add",
                "list_id": "report:abc",
                "entity_id": "ip:1.2.3.4",
                "context_note": "from-alert",
            },
        )
    )
    assert not result.is_error
    call = recorder.calls[0]
    assert call["url"].endswith("/list/report:abc/entity/add")
    assert call["json"]["entity"] == {"id": "ip:1.2.3.4"}
    assert call["json"]["context"] == {"annotation": "from-alert"}


@pytest.mark.asyncio
async def test_submit_collective_insights_body(monkeypatch):
    def router(method, url):
        return {"result": {"status": "OK", "summary": {"processed": {"ip": 1}}}}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="ci1",
            name="submit_rf_collective_insights",
            arguments={
                "ioc_type": "ip",
                "ioc_value": "1.2.3.4",
                "detection_name": "Suspicious beacon",
                "detection_type": "correlation",
                "mitre_codes": "T1071",
            },
        )
    )
    assert not result.is_error
    call = recorder.calls[0]
    assert call["url"].endswith("/collective-insights/detections")
    record = call["json"]["data"][0]
    assert record["ioc"] == {"type": "ip", "value": "1.2.3.4"}
    assert record["detection"]["type"] == "correlation"
    assert record["mitre_codes"] == ["T1071"]


@pytest.mark.asyncio
async def test_leaked_credentials_body(monkeypatch):
    def router(method, url):
        return {"identities": [{"login": "jane.doe", "domain": "acme.com"}], "count": 1}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="id1",
            name="search_rf_leaked_credentials",
            arguments={"domains": "acme.com", "domain_types": "Email,Authorization"},
        )
    )
    assert not result.is_error
    assert "jane.doe" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/identity/credentials/search")
    assert call["json"]["domains"] == ["acme.com"]
    assert call["json"]["domain_types"] == ["Email", "Authorization"]


@pytest.mark.asyncio
async def test_leaked_credentials_domain_types_case_insensitive(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"identities": [], "count": 0})
    await executor.execute_tool(
        ToolCall(
            id="id2",
            name="search_rf_leaked_credentials",
            arguments={"domains": "acme.com", "domain_types": "email"},
        )
    )
    # Case-insensitive match -> RF's canonical casing.
    assert recorder.calls[0]["json"]["domain_types"] == ["Email"]


@pytest.mark.asyncio
async def test_leaked_credentials_rejects_invalid_domain_types(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="id3",
            name="search_rf_leaked_credentials",
            arguments={"domains": "acme.com", "domain_types": "bogus"},
        )
    )
    assert "domain_types must be" in result.content.lower()
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# intel_card_url - surface the API's Intelligence Card link, never fabricate one
# ---------------------------------------------------------------------------


def test_intel_card_url_returns_api_value():
    from app.integrations.providers.recorded_future._common import intel_card_url

    url = "https://app.recordedfuture.com/live/sc/entity/ip:1.2.3.4"
    assert intel_card_url({"intelCard": url}) == url
    assert intel_card_url({"entity": {"id": "ip:1.2.3.4"}, "intelCard": url}) == url


def test_intel_card_url_does_not_fabricate_when_absent():
    from app.integrations.providers.recorded_future._common import intel_card_url

    # No intelCard from the API -> empty string, never a guessed portal URL
    # (the RF portal path scheme is not part of the API contract).
    assert intel_card_url({"entity": {"id": "ip:1.2.3.4"}}) == ""
    assert intel_card_url({"id": "ip:1.2.3.4"}) == ""
    assert intel_card_url({}) == ""


# ---------------------------------------------------------------------------
# list_rf_risk_rules - malware has no /v2/{type}/riskrules endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_risk_rules_rejects_malware(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"data": {"results": []}})
    result = await executor.execute_tool(
        ToolCall(
            id="rr1", name="list_rf_risk_rules", arguments={"entity_type": "malware"}
        )
    )
    assert result.is_error
    assert "malware" in result.content.lower()
    # Rejected before any HTTP request is issued.
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_list_risk_rules_accepts_ip(monkeypatch):
    def router(method, url):
        return {
            "data": {"results": [{"name": "Recent C&C", "criticality": 4, "count": 12}]}
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="rr2", name="list_rf_risk_rules", arguments={"entity_type": "ip"})
    )
    assert not result.is_error
    assert "Recent C&C" in result.content
    assert recorder.calls[0]["url"].endswith("/v2/ip/riskrules")


# ---------------------------------------------------------------------------
# Entity id lookup (Entity Match reverse direction)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_entity_resolves_id(monkeypatch):
    def router(method, url):
        return {
            "data": {
                "id": "kvXvR5",
                "type": "Malware",
                "attributes": {
                    "name": "Cobalt Strike",
                    "alias": ["CobaltStrike", "CS Beacon"],
                    "is_threat_actor": False,
                },
            }
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="el1", name="lookup_rf_entity", arguments={"entity_id": "kvXvR5"})
    )
    assert not result.is_error
    assert "Cobalt Strike" in result.content
    assert "CS Beacon" in result.content
    assert recorder.calls[0]["url"].endswith("/entity-match/entity/kvXvR5")


@pytest.mark.asyncio
async def test_lookup_entity_404_is_benign(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: ({"message": "nf"}, 404))
    result = await executor.execute_tool(
        ToolCall(id="el2", name="lookup_rf_entity", arguments={"entity_id": "nope"})
    )
    assert not result.is_error
    assert "no recorded future entity" in result.content.lower()
    assert recorder.calls[0]["expected_statuses"] == {404}


# ---------------------------------------------------------------------------
# Playbook alert category details
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_playbook_alert_details_rejects_bad_category(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="pd1",
            name="get_rf_playbook_alert_details",
            arguments={"playbook_alert_id": "task:1", "category": "ufo_sightings"},
        )
    )
    assert "category must be one of" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_playbook_alert_details_posts_panels(monkeypatch):
    def router(method, url):
        return {
            "data": {
                "playbook_alert_id": "task:123",
                "panel_status": {
                    "entity_name": "mail.g00gle.example",
                    "status": "New",
                    "priority": "High",
                    "risk_score": 87,
                },
                "panel_evidence_dns": {
                    "ip_list": [{"record": "1.2.3.4", "risk_score": 90}],
                },
            }
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="pd2",
            name="get_rf_playbook_alert_details",
            arguments={
                "playbook_alert_id": "task:123",
                "category": "domain_abuse",
                "panels": "status,dns",
            },
        )
    )
    assert not result.is_error
    assert "mail.g00gle.example" in result.content
    assert "panel_evidence_dns" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/playbook-alert/domain_abuse/task:123")
    assert call["method"] == "post"
    assert call["json"]["panels"] == ["status", "dns"]


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_cases_hydrates_summaries(monkeypatch):
    def router(method, url):
        if url.endswith("/case/search"):
            return {"data": ["task:1"], "counts": {"total": 1}}
        return {
            "data": {
                "task:1": {
                    "panel_summary": {
                        "title": "Suspicious C2 beacon",
                        "status": "New",
                        "priority": "High",
                        "assignee_name": "Jane",
                        "created": "2026-06-01T00:00:00Z",
                    }
                }
            }
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="search_rf_cases",
            arguments={"statuses": "New", "priorities": "High"},
        )
    )
    assert not result.is_error
    assert "Suspicious C2 beacon" in result.content
    assert "task:1" in result.content
    search_call, lookup_call = recorder.calls
    assert search_call["url"].endswith("/case/search")
    assert search_call["json"]["statuses"] == ["New"]
    assert search_call["json"]["priorities"] == ["High"]
    assert lookup_call["url"].endswith("/case/lookup")
    assert lookup_call["json"]["ids"] == ["task:1"]


@pytest.mark.asyncio
async def test_search_cases_rejects_bad_status(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="c2", name="search_rf_cases", arguments={"statuses": "Closed"})
    )
    assert "statuses must be a subset" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_search_cases_unassigned_maps_to_empty_assignee(monkeypatch):
    def router(method, url):
        if url.endswith("/case/search"):
            return {"data": [], "counts": {"total": 0}}
        return {"data": {}}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="c3", name="search_rf_cases", arguments={"assignee": "unassigned"})
    )
    assert not result.is_error
    assert recorder.calls[0]["json"]["assignees"] == [""]


@pytest.mark.asyncio
async def test_create_case_requires_references_for_reference_alert(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c4",
            name="create_rf_case",
            arguments={
                "title": "Case",
                "alert_notification": "4e4GmW",
                "alert_rule": "report:abc",
                "organization": "uhash:123",
            },
        )
    )
    assert "references" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_create_case_builds_context(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"data": "task:new-case", "status": {}}
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c5",
            name="create_rf_case",
            arguments={
                "title": "Investigate beacon",
                "alert_notification": "4e4GmW",
                "alert_rule": "report:abc",
                "organization": "uhash:123",
                "context_type": "signal_alert",
                "entity_id": "ip:1.2.3.4",
                "priority": "High",
            },
        )
    )
    assert not result.is_error
    assert "task:new-case" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/case/create")
    assert call["json"]["context"]["type"] == "signal_alert"
    assert call["json"]["context"]["entity"] == "ip:1.2.3.4"
    assert call["json"]["organization"] == "uhash:123"
    assert call["json"]["priority"] == "High"


@pytest.mark.asyncio
async def test_update_case_requires_a_change(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="c6", name="update_rf_case", arguments={"case_id": "task:1"})
    )
    assert "provide at least one" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_update_case_unassign_maps_to_empty_string(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"status": {"status_code": "OK"}}
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c7",
            name="update_rf_case",
            arguments={"case_id": "task:1", "status": "Resolved", "assignee": "unassigned"},
        )
    )
    assert not result.is_error
    call = recorder.calls[0]
    assert call["url"].endswith("/case/update")
    assert call["json"]["status"] == "Resolved"
    assert call["json"]["assignee"] == ""


# ---------------------------------------------------------------------------
# Threat maps (POST + filters), risk history, note publishing, malware intel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_threat_map_posts_with_filters(monkeypatch):
    def router(method, url):
        return {
            "data": [
                {"id": "abc", "name": "FIN7", "intent": 80, "opportunity": 65}
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="tm1", name="get_rf_threat_map", arguments={"actor_ids": "abc"})
    )
    assert not result.is_error
    assert "FIN7" in result.content
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/threat/map/actors")
    assert call["json"]["id"] == ["abc"]


@pytest.mark.asyncio
async def test_malware_threat_map_scores(monkeypatch):
    def router(method, url):
        return {
            "data": [
                {"id": "m1", "name": "LummaC2", "prevalence": 91, "opportunity": 70}
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="tm2", name="get_rf_malware_threat_map", arguments={})
    )
    assert not result.is_error
    assert "LummaC2" in result.content
    assert "prevalence 91" in result.content
    assert recorder.calls[0]["url"].endswith("/threat/map/malware")


@pytest.mark.asyncio
async def test_risk_history_requires_entities(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="rh1", name="get_rf_risk_history", arguments={"entity_ids": ""})
    )
    assert "at least one rf entity id" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_risk_history_formats_changes(monkeypatch):
    def router(method, url):
        return {
            "data": [
                {
                    "entity": {
                        "id": "ip:1.2.3.4",
                        "type": "IpAddress",
                        "name": "1.2.3.4",
                    },
                    "scores": [
                        {"score": 97, "added": "2026-05-02T03:25:41Z", "removed": "2026-05-05T03:06:30Z"},
                        {"score": 99, "added": "2026-05-05T03:06:30Z"},
                    ],
                    "risk_rules": [
                        {"rule": "Validated C&C Server", "added": "2026-05-02T03:25:41Z"}
                    ],
                }
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="rh2",
            name="get_rf_risk_history",
            arguments={"entity_ids": "ip:1.2.3.4", "from_date": "2026-05-01"},
        )
    )
    assert not result.is_error
    assert "Validated C&C Server" in result.content
    assert "99" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/risk/history")
    assert call["json"]["entities"] == ["ip:1.2.3.4"]
    assert call["json"]["from"] == "2026-05-01"


@pytest.mark.asyncio
async def test_publish_analyst_note_requires_title_and_text(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="pn1", name="publish_rf_analyst_note", arguments={"title": "x", "text": ""})
    )
    assert "title and text" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_publish_analyst_note_body(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"note_id": "doc:vqqO35"})
    result = await executor.execute_tool(
        ToolCall(
            id="pn2",
            name="publish_rf_analyst_note",
            arguments={
                "title": "Investigation findings",
                "text": "The beacon resolved to known Cobalt Strike infra.",
                "source": "source:VKz42X",
                "entity_ids": "ip:1.2.3.4, kvXvR5",
            },
        )
    )
    assert not result.is_error
    assert "doc:vqqO35" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/analyst-note/publish")
    assert call["json"]["source"] == "source:VKz42X"
    assert call["json"]["attributes"]["title"] == "Investigation findings"
    assert call["json"]["attributes"]["note_entities"] == ["ip:1.2.3.4", "kvXvR5"]


@pytest.mark.asyncio
async def test_query_malware_intelligence_rejects_bad_field(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="mi1",
            name="query_rf_malware_intelligence",
            arguments={"query": "beacons", "field": "favourite_colour"},
        )
    )
    assert "field must be one of" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_query_malware_intelligence_body(monkeypatch):
    def router(method, url):
        return {
            "data": [{"value": "1.2.3.4", "count": 12}],
            "executedQuery": "dst_port:5353",
            "totalPages": 3,
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="mi2",
            name="query_rf_malware_intelligence",
            arguments={"query": "samples that talked to port 5353", "field": "ip_address"},
        )
    )
    assert not result.is_error
    assert "1.2.3.4" in result.content
    assert "dst_port:5353" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/malware-intelligence/v1/query_nl")
    assert call["json"]["field"] == "ip_address"
    assert call["json"]["start_date"] == "-30d"


@pytest.mark.asyncio
async def test_detection_rules_include_content_returns_source(monkeypatch):
    def router(method, url):
        return {
            "total_count": 1,
            "result": [
                {
                    "id": "doc:abc",
                    "type": "yara",
                    "title": "Hunt Log4Shell",
                    "rules": [
                        {
                            "file_name": "log4shell.yar",
                            "content": "rule Log4Shell { condition: true }",
                        }
                    ],
                }
            ],
        }

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="dr2",
            name="search_rf_detection_rules",
            arguments={"rule_types": "yara", "include_content": True, "limit": 1},
        )
    )
    assert not result.is_error
    assert "rule Log4Shell { condition: true }" in result.content


# ---------------------------------------------------------------------------
# Collective Insights search + Identity lookup / password exposure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collective_insights_search_filters(monkeypatch):
    def router(method, url):
        return {
            "data": [
                {
                    "ioc": {"type": "ip", "value": "1.2.3.4"},
                    "detection": {"type": "correlation", "name": "SIEM rule"},
                    "detection_time": "2026-06-20T10:00:00Z",
                }
            ],
            "counts": {"total": 1},
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="cis1",
            name="search_rf_collective_insights",
            arguments={
                "detection_type": "correlation",
                "mitre_codes": "T1059, mitre:T1071",
                "days": 7,
            },
        )
    )
    assert not result.is_error
    assert "1.2.3.4" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/collective-insights/search")
    filters = call["json"]["filters"]
    assert filters["detection_type"] == ["correlation"]
    # Bare MITRE codes get the required "mitre:" prefix; existing prefixes kept.
    assert filters["associated_threats"]["mitre_code"]["id"] == [
        "mitre:T1059",
        "mitre:T1071",
    ]
    assert filters["detection_time"] == {"from": "-7d"}


@pytest.mark.asyncio
async def test_collective_insights_search_rejects_bad_type(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="cis2",
            name="search_rf_collective_insights",
            arguments={"detection_type": "vibes"},
        )
    )
    assert "detection_type must be one of" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_lookup_identity_exposures_body(monkeypatch):
    def router(method, url):
        return {
            "identities": [
                {
                    "identity": {"subjects": ["jane@acme.com"]},
                    "credentials": [
                        {
                            "dumps": [{"name": "Big Breach 2026"}],
                            "exposed_secret": {"details": {"type": "sha256"}},
                            "authorization_service": {"domain": "portal.acme.com"},
                            "malware_family": {"name": "LummaC2"},
                        }
                    ],
                }
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="ie1",
            name="lookup_rf_identity_exposures",
            arguments={"subjects": "jane@acme.com"},
        )
    )
    assert not result.is_error
    assert "jane@acme.com" in result.content
    assert "Big Breach 2026" in result.content
    assert "LummaC2" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/identity/credentials/lookup")
    assert call["json"]["subjects"] == ["jane@acme.com"]


@pytest.mark.asyncio
async def test_check_password_exposure_prefix_vs_full_hash(monkeypatch):
    def router(method, url):
        return {
            "results": [
                {
                    "password": {"algorithm": "SHA1", "hash_prefix": "14659"},
                    "exposure_status": "Common",
                }
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="pw1",
            name="check_rf_password_exposure",
            arguments={
                "hashes": "14659, 1465901e291d3d7e0bb5b49ca30e8f23ecf8101a",
                "algorithm": "sha1",
            },
        )
    )
    assert not result.is_error
    assert "Common" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/identity/password/lookup")
    passwords = call["json"]["passwords"]
    # 5-char values are k-anonymity prefixes; longer values are full hashes.
    assert passwords[0] == {"algorithm": "SHA1", "hash_prefix": "14659"}
    assert passwords[1] == {
        "algorithm": "SHA1",
        "hash": "1465901e291d3d7e0bb5b49ca30e8f23ecf8101a",
    }


@pytest.mark.asyncio
async def test_check_password_exposure_rejects_bad_algorithm(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="pw2",
            name="check_rf_password_exposure",
            arguments={"hashes": "14659", "algorithm": "ROT13"},
        )
    )
    assert "algorithm must be one of" in result.content.lower()
    assert recorder.calls == []
