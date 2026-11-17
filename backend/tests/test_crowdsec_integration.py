"""Tests for the CrowdSec integration provider.

Covers the credential chokepoints (x-api-key header building + compound token
parsing + per-key degradation), tool registration, CTI object formatting, the
404 = "unknown IP" contract, strict IP / duration / id input validation, HAL
and fastapi-pagination hints, the premium (403) endpoint messaging, and the
Service API console domains (decisions, blocklists, allowlists, exploit
tracker, integration endpoints, metrics).
"""

from __future__ import annotations

import pytest

from app.integrations.providers.crowdsec import (
    CrowdSecIntegration,
    build_crowdsec_headers,
    build_tools,
    parse_crowdsec_token,
)
from app.integrations.providers.crowdsec import allowlists as allowlists_module
from app.integrations.providers.crowdsec import blocklists as blocklists_module
from app.integrations.providers.crowdsec import console as console_module
from app.integrations.providers.crowdsec import cti as cti_module
from app.integrations.providers.crowdsec import decisions as decisions_module
from app.integrations.providers.crowdsec import tracker as tracker_module
from app.services.integration_tools import _SafeIntegrationExecutor

_ALL_MODULES = (
    cti_module,
    decisions_module,
    blocklists_module,
    allowlists_module,
    tracker_module,
    console_module,
)

_CTI_KEY = "cti_test_key"
_SERVICE_KEY = "service_test_key"
_TOKEN = f"{_CTI_KEY}||{_SERVICE_KEY}"


class _FakeResp:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload


def _build(token: str = _TOKEN):
    executor = _SafeIntegrationExecutor()
    tools = build_tools(token, executor)
    return executor._handlers, tools


def _capture(
    monkeypatch,
    payload: dict | None = None,
    status: int = 200,
    module=cti_module,
    text: str = "",
) -> dict:
    """Patch safe_request in *module* to capture args + return a canned response.

    ``captured["calls"]`` accumulates every request so multi-call tools
    (e.g. org-id auto-resolution) can be asserted end-to-end.
    """
    captured: dict = {"calls": []}

    async def _fake_safe_request(client, method, url, provider, action, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured.update(kwargs)
        captured["calls"].append({"method": method, "url": url, **kwargs})
        return _FakeResp(status, payload or {}, text=text), None

    monkeypatch.setattr(module, "safe_request", _fake_safe_request)
    return captured


def _forbid_requests(monkeypatch, modules=(cti_module,)) -> None:
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run for invalid input")

    for module in modules:
        monkeypatch.setattr(module, "safe_request", _must_not_call)


# ---------------------------------------------------------------------------
# Credential chokepoints
# ---------------------------------------------------------------------------


def test_headers_strip_whitespace_and_use_x_api_key():
    headers = build_crowdsec_headers("  cs_key \n")
    assert headers["x-api-key"] == "cs_key"
    assert headers["Accept"] == "application/json"


def test_parse_compound_token_variants():
    assert parse_crowdsec_token("cti||svc") == ("cti", "svc")
    assert parse_crowdsec_token("cti||") == ("cti", "")
    assert parse_crowdsec_token("||svc") == ("", "svc")
    assert parse_crowdsec_token(" cti ") == ("cti", "")
    assert parse_crowdsec_token("") == ("", "")
    assert parse_crowdsec_token("  ||  ") == ("", "")


def test_blank_token_builds_no_tools():
    handlers, tools = _build("  ||  ")
    assert tools == []
    assert handlers == {}


def test_provider_class_contract():
    integ = CrowdSecIntegration()
    assert integ.provider_name == "crowdsec"
    assert integ.auth_types == ["api_key"]
    assert integ.get_oauth_defaults() is None


def test_all_tools_registered():
    handlers, tools = _build()
    names = {t.name for t in tools}
    assert names == set(handlers)
    assert names == {
        # CTI API
        "lookup_crowdsec_ip",
        "search_crowdsec_ips",
        "lookup_crowdsec_ips",
        "get_crowdsec_fire_ips",
        "get_crowdsec_service_info",
        # Console decisions
        "list_crowdsec_decisions",
        "create_crowdsec_decision",
        "delete_crowdsec_decision",
        # Blocklists
        "list_crowdsec_blocklists",
        "get_crowdsec_blocklist",
        "create_crowdsec_blocklist",
        "add_crowdsec_blocklist_ips",
        "remove_crowdsec_blocklist_ips",
        "manage_crowdsec_blocklist",
        # Allowlists
        "manage_crowdsec_allowlist",
        # Live Exploit Tracker
        "list_crowdsec_cves",
        "get_crowdsec_cve",
        "get_crowdsec_cve_ips",
        "list_crowdsec_exploitation_events",
        "manage_crowdsec_cve_tracking",
        "manage_crowdsec_tracker",
        # Integration endpoints + metrics
        "list_crowdsec_integrations",
        "manage_crowdsec_integration",
        "get_crowdsec_remediation_metrics",
    }
    assert all(t.source == "integration:crowdsec" for t in tools)
    by_name = {t.name: t for t in tools}
    # Reads sort before writes before destructive ops.
    assert by_name["lookup_crowdsec_ip"].priority == 0
    assert by_name["list_crowdsec_decisions"].priority == 0
    assert by_name["list_crowdsec_cves"].priority == 0
    assert by_name["create_crowdsec_decision"].priority == 1
    assert by_name["add_crowdsec_blocklist_ips"].priority == 1
    assert by_name["manage_crowdsec_allowlist"].priority == 1
    assert by_name["delete_crowdsec_decision"].priority == 2


# ---------------------------------------------------------------------------
# Per-key degradation (either key may be missing)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_cti_key_degrades_with_actionable_error(monkeypatch):
    _forbid_requests(monkeypatch)
    handlers, tools = _build(f"||{_SERVICE_KEY}")
    # All tools still register - the CTI subset errors at call time.
    assert len(tools) == 24
    result = await handlers["lookup_crowdsec_ip"](ip="8.8.8.8")
    assert "CTI API key is not configured" in result
    result = await handlers["search_crowdsec_ips"](query="reputation:malicious")
    assert "CTI API key is not configured" in result


@pytest.mark.asyncio
async def test_missing_service_key_degrades_with_actionable_error(monkeypatch):
    _forbid_requests(monkeypatch, _ALL_MODULES)
    handlers, _ = _build(f"{_CTI_KEY}||")
    assert "Service API key is not configured" in await handlers[
        "get_crowdsec_service_info"
    ]()
    assert "Service API key is not configured" in await handlers[
        "list_crowdsec_decisions"
    ]()
    assert "Service API key is not configured" in await handlers[
        "list_crowdsec_blocklists"
    ]()
    assert "Service API key is not configured" in await handlers[
        "manage_crowdsec_allowlist"
    ](action="list")
    assert "Service API key is not configured" in await handlers[
        "list_crowdsec_cves"
    ]()
    assert "Service API key is not configured" in await handlers[
        "get_crowdsec_remediation_metrics"
    ]()


# ---------------------------------------------------------------------------
# IP lookup formatting
# ---------------------------------------------------------------------------


_FULL_CTI_PAYLOAD = {
    "ip": "185.220.101.34",
    "reputation": "malicious",
    "confidence": "high",
    "ip_range": "185.220.100.0/22",
    "ip_range_score": 5,
    "ip_range_24": "185.220.101.0/24",
    "ip_range_24_reputation": "malicious",
    "ip_range_24_score": 5,
    "as_name": "EVIL-AS",
    "as_num": 64500,
    "background_noise": "high",
    "background_noise_score": 9,
    "location": {"country": "DE", "city": "Berlin"},
    "reverse_dns": "tor-exit.example.net",
    "history": {
        "first_seen": "2024-03-03T23:00:00",
        "last_seen": "2026-07-01T10:30:00",
        "full_age": 851,
        "days_age": 820,
    },
    "scores": {
        "overall": {"total": 5, "aggressiveness": 5, "threat": 4, "trust": 5, "anomaly": 1},
        "last_day": {"total": 3},
        "last_week": {"total": 4},
        "last_month": {"total": 5},
    },
    "behaviors": [
        {"name": "ssh:bruteforce", "label": "SSH Bruteforce"},
        {"name": "http:scan", "label": "HTTP Scan"},
    ],
    "attack_details": [
        {"name": "crowdsecurity/ssh-slow-bf", "label": "Slow SSH Bruteforce"},
    ],
    "mitre_techniques": [
        {"name": "T1110", "label": "Brute Force"},
    ],
    "cves": ["CVE-2024-3400"],
    "classifications": {
        "classifications": [
            {"name": "community-blocklist", "label": "CrowdSec Community Blocklist"},
            {"name": "profile:tor", "label": "TOR exit node"},
        ],
        "false_positives": [],
    },
    "target_countries": {"US": 40, "DE": 25, "FR": 10},
    "references": [],
}


@pytest.mark.asyncio
async def test_lookup_ip_formats_full_report(monkeypatch):
    captured = _capture(monkeypatch, _FULL_CTI_PAYLOAD)
    handlers, _ = _build()
    result = await handlers["lookup_crowdsec_ip"](ip="185.220.101.34")

    assert captured["url"].endswith("/v2/smoke/185.220.101.34")
    assert captured["headers"]["x-api-key"] == _CTI_KEY
    assert "malicious" in result
    assert "confidence: high" in result
    assert "EVIL-AS" in result
    assert "SSH Bruteforce" in result
    assert "T1110" in result
    assert "CVE-2024-3400" in result
    assert "CrowdSec Community Blocklist" in result
    assert "US (40%)" in result
    assert "185.220.101.0/24" in result
    assert "high (9/10)" in result


@pytest.mark.asyncio
async def test_lookup_ip_flags_false_positive_tags(monkeypatch):
    payload = dict(_FULL_CTI_PAYLOAD)
    payload["classifications"] = {
        "classifications": [],
        "false_positives": [{"name": "seo:crawler", "label": "SEO crawler"}],
    }
    _capture(monkeypatch, payload)
    handlers, _ = _build()
    result = await handlers["lookup_crowdsec_ip"](ip="185.220.101.34")
    assert "FALSE POSITIVE" in result
    assert "SEO crawler" in result


@pytest.mark.asyncio
async def test_lookup_ip_reports_unknown_on_404(monkeypatch):
    async def _fake_404(client, method, url, provider, action, **kwargs):
        return _FakeResp(404, {"message": "IP address information not found"}), "err"

    monkeypatch.setattr(cti_module, "safe_request", _fake_404)
    handlers, _ = _build()
    result = await handlers["lookup_crowdsec_ip"](ip="10.11.12.13")
    assert "unknown to the CrowdSec network" in result
    assert "Error" not in result


@pytest.mark.asyncio
async def test_lookup_ip_rejects_invalid_addresses(monkeypatch):
    _forbid_requests(monkeypatch)
    handlers, _ = _build()
    for bad in ("", "..", "1.2.3.4/../admin", "1.2.3.4?x=1", "not-an-ip", "999.1.1.1"):
        result = await handlers["lookup_crowdsec_ip"](ip=bad)
        assert "Error" in result


@pytest.mark.asyncio
async def test_lookup_ip_accepts_ipv6(monkeypatch):
    captured = _capture(monkeypatch, _FULL_CTI_PAYLOAD)
    handlers, _ = _build()
    await handlers["lookup_crowdsec_ip"](ip="2001:db8::1")
    assert captured["url"].endswith("/v2/smoke/2001:db8::1")


# ---------------------------------------------------------------------------
# Smoke search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_passes_params_and_hints_next_page(monkeypatch):
    captured = _capture(
        monkeypatch,
        {
            "items": [_FULL_CTI_PAYLOAD],
            "_links": {
                "self": {"href": "https://cti.api.crowdsec.net/v2/smoke/search?page=2"},
                "next": {"href": "https://cti.api.crowdsec.net/v2/smoke/search?page=3"},
            },
        },
    )
    handlers, _ = _build()
    result = await handlers["search_crowdsec_ips"](
        query='behaviors.label:"SSH Bruteforce"', since="24h", page=2, limit=10
    )

    assert captured["url"].endswith("/v2/smoke/search")
    assert captured["params"] == {
        "query": 'behaviors.label:"SSH Bruteforce"',
        "since": "24h",
        "page": 2,
        "limit": 10,
    }
    assert "185.220.101.34" in result
    assert "pass page=3" in result


@pytest.mark.asyncio
async def test_search_omits_hint_on_last_page(monkeypatch):
    _capture(
        monkeypatch,
        {"items": [_FULL_CTI_PAYLOAD], "_links": {"self": {"href": "x"}}},
    )
    handlers, _ = _build()
    result = await handlers["search_crowdsec_ips"](query="reputation:malicious")
    assert "pass page=" not in result


@pytest.mark.asyncio
async def test_search_validates_query_and_since(monkeypatch):
    _forbid_requests(monkeypatch)
    handlers, _ = _build()
    assert "Error" in await handlers["search_crowdsec_ips"](query="  ")
    assert "Error" in await handlers["search_crowdsec_ips"](
        query="reputation:malicious", since="yesterday"
    )
    assert "Error" in await handlers["search_crowdsec_ips"](
        query="reputation:malicious", since="7w"
    )


# ---------------------------------------------------------------------------
# Batch lookup (premium)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_lookup_joins_ips_and_reports_not_found(monkeypatch):
    captured = _capture(
        monkeypatch,
        {"items": [_FULL_CTI_PAYLOAD], "total": 1, "not_found": 1},
    )
    handlers, _ = _build()
    result = await handlers["lookup_crowdsec_ips"](
        ips=["185.220.101.34", "10.0.0.1", "not-an-ip"]
    )

    assert captured["params"] == {"ips": "185.220.101.34,10.0.0.1"}
    assert "185.220.101.34" in result
    assert "1 IP(s) were unknown" in result
    assert "Skipped 1 invalid IP(s): not-an-ip" in result


@pytest.mark.asyncio
async def test_batch_lookup_validates_before_any_request(monkeypatch):
    _forbid_requests(monkeypatch)
    handlers, _ = _build()
    assert "Error" in await handlers["lookup_crowdsec_ips"](ips=[])
    assert "Error" in await handlers["lookup_crowdsec_ips"](ips=["nope"])
    result = await handlers["lookup_crowdsec_ips"](
        ips=[f"10.0.{i // 250}.{i % 250}" for i in range(101)]
    )
    assert "too many IPs" in result


@pytest.mark.asyncio
async def test_batch_lookup_explains_premium_403(monkeypatch):
    async def _fake_403(client, method, url, provider, action, **kwargs):
        return _FakeResp(403, {"message": "Forbidden"}), "err"

    monkeypatch.setattr(cti_module, "safe_request", _fake_403)
    handlers, _ = _build()
    result = await handlers["lookup_crowdsec_ips"](ips=["8.8.8.8"])
    assert "premium" in result
    assert "lookup_crowdsec_ip" in result


# ---------------------------------------------------------------------------
# Fire (community blocklist, premium)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_passes_since_and_formats_state(monkeypatch):
    fire_entry = dict(_FULL_CTI_PAYLOAD)
    fire_entry["state"] = "validated"
    fire_entry["expiration"] = "2026-08-01T00:00:00"
    captured = _capture(
        monkeypatch,
        {"items": [fire_entry], "_links": {"self": {"href": "x"}}},
    )
    handlers, _ = _build()
    result = await handlers["get_crowdsec_fire_ips"](since="3d", limit=25)

    assert captured["url"].endswith("/v2/fire")
    assert captured["params"] == {"page": 1, "limit": 25, "since": "3d"}
    assert "state: validated" in result
    assert "validated = currently in the community blocklist" in result


@pytest.mark.asyncio
async def test_fire_validates_since_and_explains_403(monkeypatch):
    _forbid_requests(monkeypatch)
    handlers, _ = _build()
    assert "Error" in await handlers["get_crowdsec_fire_ips"](since="3 days")

    async def _fake_403(client, method, url, provider, action, **kwargs):
        return _FakeResp(403, {"message": "Forbidden"}), "err"

    monkeypatch.setattr(cti_module, "safe_request", _fake_403)
    result = await handlers["get_crowdsec_fire_ips"]()
    assert "premium" in result


# ---------------------------------------------------------------------------
# Service API info
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_info_uses_service_key(monkeypatch):
    captured = _capture(
        monkeypatch,
        {
            "organization_id": "org-123",
            "subscription_type": "premium",
            "api_key_name": "copilot-key",
        },
    )
    handlers, _ = _build()
    result = await handlers["get_crowdsec_service_info"]()

    assert captured["url"].endswith("/v1/info")
    assert captured["headers"]["x-api-key"] == _SERVICE_KEY
    assert "org-123" in result
    assert "premium" in result
    assert "copilot-key" in result


# ---------------------------------------------------------------------------
# Console decisions
# ---------------------------------------------------------------------------

_BL_ID = "5f9d88b9e5c4f5b9a3d3e8b1"


@pytest.mark.asyncio
async def test_list_decisions_passes_filters_and_page_footer(monkeypatch):
    captured = _capture(
        monkeypatch,
        {
            "items": [
                {
                    "uuid": "dec-1",
                    "type": "ban",
                    "scope": "ip",
                    "value": "1.2.3.4",
                    "duration": "24h",
                    "scenario": "manual",
                    "origin": "console",
                    "created_at": "2026-07-01T10:00:00Z",
                    "target": {"type": "org", "value": "org-123"},
                }
            ],
            "total": 120,
            "page": 1,
            "size": 50,
            "pages": 3,
        },
        module=decisions_module,
    )
    handlers, _ = _build()
    result = await handlers["list_crowdsec_decisions"](
        ips=["1.2.3.4"], remediation_types=["ban"]
    )

    assert captured["url"].endswith("/v1/decisions")
    assert captured["params"]["ips"] == ["1.2.3.4"]
    assert captured["params"]["remediation_types"] == ["ban"]
    assert "dec-1" in result
    assert "Page 1/3" in result
    assert "pass page=2" in result


@pytest.mark.asyncio
async def test_list_decisions_aggregated_switches_endpoint(monkeypatch):
    captured = _capture(
        monkeypatch, {"items": [], "page": 1, "pages": 1}, module=decisions_module
    )
    handlers, _ = _build()
    await handlers["list_crowdsec_decisions"](aggregated=True)
    assert captured["url"].endswith("/v1/decisions/aggregated")


@pytest.mark.asyncio
async def test_create_decision_auto_resolves_org_target(monkeypatch):
    captured = _capture(
        monkeypatch,
        {"organization_id": "org-123", "uuid": "dec-9"},
        module=decisions_module,
    )
    handlers, _ = _build()
    result = await handlers["create_crowdsec_decision"](value="203.0.113.7")

    # First call resolves the org id from /info, second posts the decision.
    assert len(captured["calls"]) == 2
    assert captured["calls"][0]["url"].endswith("/v1/info")
    post = captured["calls"][1]
    assert post["method"] == "post"
    assert post["url"].endswith("/v1/decisions")
    assert post["json"] == {
        "duration": "24h",
        "origin": "console",
        "scenario": "manual",
        "scope": "ip",
        "type": "ban",
        "value": "203.0.113.7",
        "target": {"type": "org", "value": "org-123"},
    }
    assert "Console decision created" in result


@pytest.mark.asyncio
async def test_list_decisions_rejects_unknown_remediation_types(monkeypatch):
    _forbid_requests(monkeypatch, (decisions_module,))
    handlers, _ = _build()
    result = await handlers["list_crowdsec_decisions"](
        remediation_types=["ban", "nuke"]
    )
    assert "unknown remediation type(s): nuke" in result
    assert "ban, captcha, throttle" in result


@pytest.mark.asyncio
async def test_create_decision_validates_before_any_request(monkeypatch):
    _forbid_requests(monkeypatch, (decisions_module,))
    handlers, _ = _build()
    create = handlers["create_crowdsec_decision"]

    assert "Error" in await create(value="not-an-ip")
    assert "duration" in await create(value="1.2.3.4", duration="soon")
    assert "decision_type" in await create(value="1.2.3.4", decision_type="nuke")
    assert "target_type" in await create(value="1.2.3.4", target_type="planet")
    # Non-org targets need an explicit target_value.
    assert "target_value" in await create(value="1.2.3.4", target_type="tag")


@pytest.mark.asyncio
async def test_delete_decision_guards_path_and_switches_aggregated(monkeypatch):
    _forbid_requests(monkeypatch, (decisions_module,))
    handlers, _ = _build()
    assert "Error" in await handlers["delete_crowdsec_decision"](decision_uuid="")
    assert "Error" in await handlers["delete_crowdsec_decision"](
        decision_uuid="a/../b"
    )

    captured = _capture(monkeypatch, {}, module=decisions_module)
    result = await handlers["delete_crowdsec_decision"](
        decision_uuid="agg-1", aggregated=True
    )
    assert captured["url"].endswith("/v1/decisions/aggregated/agg-1")
    assert captured["method"] == "delete"
    assert "permanently deleted" in result


# ---------------------------------------------------------------------------
# Blocklists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_blocklists_uses_search_when_query_given(monkeypatch):
    captured = _capture(
        monkeypatch,
        {"items": [{"id": _BL_ID, "name": "tor-exits", "source": "custom"}]},
        module=blocklists_module,
    )
    handlers, _ = _build()
    await handlers["list_crowdsec_blocklists"](query="tor")
    assert captured["method"] == "post"
    assert captured["url"].endswith("/v1/blocklists/search")
    assert captured["json"]["query"] == "tor"

    await handlers["list_crowdsec_blocklists"]()
    assert captured["method"] == "get"
    assert captured["url"].endswith("/v1/blocklists")


@pytest.mark.asyncio
async def test_blocklist_id_guard_rejects_non_object_ids(monkeypatch):
    _forbid_requests(monkeypatch, (blocklists_module,))
    handlers, _ = _build()
    for bad in ("", "short", "not-hex-and-way-too-long!", "a/../b"):
        assert "blocklist_id" in await handlers["get_crowdsec_blocklist"](
            blocklist_id=bad
        )
        assert "blocklist_id" in await handlers["manage_crowdsec_blocklist"](
            action="delete", blocklist_id=bad
        )


@pytest.mark.asyncio
async def test_add_blocklist_ips_requires_iso_expiration(monkeypatch):
    _forbid_requests(monkeypatch, (blocklists_module,))
    handlers, _ = _build()
    add = handlers["add_crowdsec_blocklist_ips"]
    assert "expiration" in await add(
        blocklist_id=_BL_ID, ips=["1.2.3.4"], expiration="next week"
    )
    assert "Error" in await add(blocklist_id=_BL_ID, ips=["nope"], expiration="")

    captured = _capture(monkeypatch, {}, module=blocklists_module)
    result = await add(
        blocklist_id=_BL_ID,
        ips=["1.2.3.4", "10.0.0.0/24", "junk"],
        expiration="2026-12-31T00:00:00Z",
    )
    assert captured["json"]["ips"] == ["1.2.3.4", "10.0.0.0/24"]
    assert captured["json"]["expiration"] == "2026-12-31T00:00:00Z"
    assert "Added 2" in result
    assert "Skipped 1 invalid" in result


@pytest.mark.asyncio
async def test_manage_blocklist_update_cti_query_and_subscribe(monkeypatch):
    captured = _capture(monkeypatch, {}, module=blocklists_module)
    handlers, _ = _build()
    manage = handlers["manage_crowdsec_blocklist"]

    result = await manage(
        action="update",
        blocklist_id=_BL_ID,
        from_cti_query='behaviors.label:"SSH Bruteforce"',
        since="7d",
    )
    assert captured["method"] == "patch"
    assert captured["json"]["from_cti_query"] == 'behaviors.label:"SSH Bruteforce"'
    assert captured["json"]["since"] == "7d"
    assert "auto-refreshes" in result

    result = await manage(
        action="subscribe",
        blocklist_id=_BL_ID,
        subscriber_ids=["engine-1"],
        entity_type="engine",
        remediation="ban",
    )
    assert captured["url"].endswith(f"/v1/blocklists/{_BL_ID}/subscribers")
    assert captured["json"] == {
        "ids": ["engine-1"],
        "entity_type": "engine",
        "remediation": "ban",
    }
    assert "Subscribed 1 engine(s)" in result


@pytest.mark.asyncio
async def test_manage_blocklist_validates_action_and_inputs(monkeypatch):
    _forbid_requests(monkeypatch, (blocklists_module,))
    handlers, _ = _build()
    manage = handlers["manage_crowdsec_blocklist"]
    assert "action must be one of" in await manage(
        action="explode", blocklist_id=_BL_ID
    )
    assert "entity_type" in await manage(
        action="subscribe", blocklist_id=_BL_ID, subscriber_ids=["x"]
    )
    assert "subscriber_ids" in await manage(
        action="subscribe", blocklist_id=_BL_ID, entity_type="engine"
    )
    assert "organization_id" in await manage(action="share", blocklist_id=_BL_ID)
    assert "nothing to update" in (
        await manage(action="update", blocklist_id=_BL_ID)
    ).lower()


@pytest.mark.asyncio
async def test_manage_blocklist_download_previews_and_caps(monkeypatch):
    lines = "\n".join(f"10.0.0.{i}" for i in range(250))
    _capture(monkeypatch, {}, module=blocklists_module, text=lines)
    handlers, _ = _build()
    result = await handlers["manage_crowdsec_blocklist"](
        action="download", blocklist_id=_BL_ID
    )
    assert "250 entries" in result
    assert "Showing the first 200 of 250" in result

    _capture(monkeypatch, {}, module=blocklists_module, text="10.0.0.1\n")
    result = await handlers["manage_crowdsec_blocklist"](
        action="download", blocklist_id=_BL_ID
    )
    assert "1 entry:" in result


# ---------------------------------------------------------------------------
# Allowlists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allowlist_validates_ids_and_required_fields(monkeypatch):
    _forbid_requests(monkeypatch, (allowlists_module,))
    handlers, _ = _build()
    manage = handlers["manage_crowdsec_allowlist"]

    assert "action must be one of" in await manage(action="frobnicate")
    assert "allowlist_id" in await manage(action="get")
    assert "allowlist_id" in await manage(action="items", allowlist_id="bad-id")
    assert "item_id" in await manage(action="delete_item", allowlist_id=_BL_ID)
    assert "'name' is required" in await manage(action="create")
    assert "description" in await manage(
        action="add_items", allowlist_id=_BL_ID, items=["1.2.3.4"]
    )


@pytest.mark.asyncio
async def test_allowlist_add_items_posts_expected_body(monkeypatch):
    captured = _capture(monkeypatch, {}, module=allowlists_module)
    handlers, _ = _build()
    result = await handlers["manage_crowdsec_allowlist"](
        action="add_items",
        allowlist_id=_BL_ID,
        items=["192.0.2.10", "192.0.2.0/28"],
        description="Office scanners",
        expiration="2026-12-31T00:00:00Z",
    )
    assert captured["url"].endswith(f"/v1/allowlists/{_BL_ID}/items")
    assert captured["json"] == {
        "items": ["192.0.2.10", "192.0.2.0/28"],
        "description": "Office scanners",
        "expiration": "2026-12-31T00:00:00Z",
    }
    assert "Added 2" in result


@pytest.mark.asyncio
async def test_allowlist_subscribe_validates_entity_type(monkeypatch):
    _forbid_requests(monkeypatch, (allowlists_module,))
    handlers, _ = _build()
    result = await handlers["manage_crowdsec_allowlist"](
        action="subscribe",
        allowlist_id=_BL_ID,
        subscriber_ids=["engine-1"],
        entity_type="starship",
    )
    assert "entity_type must be one of" in result


# ---------------------------------------------------------------------------
# Live Exploit Tracker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_cves_passes_phase_filter(monkeypatch):
    captured = _capture(
        monkeypatch,
        {
            "items": [
                {
                    "name": "CVE-2024-3400",
                    "title": "PAN-OS Command Injection",
                    "crowdsec_score": 9,
                    "exploitation_phase": "mass_exploitation",
                    "nb_ips": 420,
                    "momentum_score": 5,
                    "has_public_exploit": True,
                }
            ],
            "total": 1,
            "page": 1,
            "pages": 1,
        },
        module=tracker_module,
    )
    handlers, _ = _build()
    result = await handlers["list_crowdsec_cves"](
        exploitation_phase="mass_exploitation", sort_by="trending"
    )
    assert captured["params"]["exploitation_phase"] == "mass_exploitation"
    assert "CVE-2024-3400" in result
    assert "mass_exploitation" in result
    assert "public exploit" in result

    _forbid_requests(monkeypatch, (tracker_module,))
    assert "exploitation_phase must be one of" in await handlers["list_crowdsec_cves"](
        exploitation_phase="apocalypse"
    )
    assert "sort_by must be one of" in await handlers["list_crowdsec_cves"](
        sort_by="chaos"
    )


@pytest.mark.asyncio
async def test_get_cve_ips_validates_and_suggests_followups(monkeypatch):
    _forbid_requests(monkeypatch, (tracker_module,))
    handlers, _ = _build()
    assert "Error" in await handlers["get_crowdsec_cve_ips"](cve_id="a/../b")
    # Strict CVE-id validation: junk that contains no path-breaking chars is
    # still rejected before any request.
    assert "Error" in await handlers["get_crowdsec_cve_ips"](cve_id="NOT-A-CVE")
    assert "Error" in await handlers["get_crowdsec_cve"](cve_id="CVE-24-1")
    assert "Error" in await handlers["manage_crowdsec_cve_tracking"](
        action="timeline", cve_id="GHSA-xxxx-yyyy"
    )
    assert "Error" in await handlers["list_crowdsec_exploitation_events"](
        cve_id="not-a-cve"
    )
    assert "Error" in await handlers["get_crowdsec_cve_ips"](
        cve_id="CVE-2024-3400", since="fortnight"
    )

    captured = _capture(
        monkeypatch,
        {
            "items": [{"ip": "1.2.3.4", "reputation": "malicious"}],
            "total": 1,
            "page": 1,
            "pages": 1,
        },
        module=tracker_module,
    )
    result = await handlers["get_crowdsec_cve_ips"](cve_id="cve-2024-3400")
    assert captured["url"].endswith("/v1/cves/CVE-2024-3400/ips-details")
    assert "1.2.3.4" in result
    assert "add_crowdsec_blocklist_ips" in result


@pytest.mark.asyncio
async def test_cve_tracking_subscribe_and_timeline_validation(monkeypatch):
    _forbid_requests(monkeypatch, (tracker_module,))
    handlers, _ = _build()
    manage = handlers["manage_crowdsec_cve_tracking"]
    assert "action must be one of" in await manage(
        action="warp", cve_id="CVE-2024-3400"
    )
    assert "integration_name" in await manage(
        action="subscribe_integration", cve_id="CVE-2024-3400"
    )
    assert "timeline_days" in await manage(
        action="timeline", cve_id="CVE-2024-3400", timeline_days=3
    )

    captured = _capture(monkeypatch, {}, module=tracker_module)
    result = await manage(
        action="subscribe_integration",
        cve_id="CVE-2024-3400",
        integration_name="edge-firewall",
    )
    assert captured["method"] == "post"
    assert captured["url"].endswith("/v1/cves/CVE-2024-3400/integrations")
    assert captured["json"] == {"name": "edge-firewall"}
    assert "subscribed" in result


@pytest.mark.asyncio
async def test_tracker_vendor_stats_and_name_quoting(monkeypatch):
    captured = _capture(
        monkeypatch,
        {
            "value": "Palo Alto Networks",
            "nb_cves": 12,
            "nb_fingerprints": 3,
            "nb_ips": 900,
            "nb_ips_cves": 700,
            "nb_ips_fingerprints": 200,
            "top_products": [{"value": "PAN-OS", "nb_ips": 800}],
        },
        module=tracker_module,
    )
    handlers, _ = _build()
    result = await handlers["manage_crowdsec_tracker"](
        action="get_vendor_stats", name="Palo Alto Networks"
    )
    # Free-form vendor names are percent-encoded into the path.
    assert captured["url"].endswith("/v1/vendors/Palo%20Alto%20Networks/stats")
    assert "12 tracked CVE(s)" in result
    assert "PAN-OS (800 IPs)" in result

    _forbid_requests(monkeypatch, (tracker_module,))
    assert "'name' is required" in await handlers["manage_crowdsec_tracker"](
        action="get_vendor_stats"
    )
    assert "action must be one of" in await handlers["manage_crowdsec_tracker"](
        action="warp"
    )


# ---------------------------------------------------------------------------
# Integration endpoints + metrics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_integration_create_validates_enums(monkeypatch):
    _forbid_requests(monkeypatch, (console_module,))
    handlers, _ = _build()
    manage = handlers["manage_crowdsec_integration"]
    assert "'name' and 'description' are required" in await manage(action="create")
    assert "entity_type must be one of" in await manage(
        action="create", name="fw", description="d", entity_type="toaster"
    )
    assert "output_format must be one of" in await manage(
        action="create", name="fw", description="d", output_format="csv"
    )
    assert "integration_id" in await manage(action="get")

    captured = _capture(monkeypatch, {"id": "int-1"}, module=console_module)
    result = await manage(
        action="create",
        name="edge-firewall",
        description="Perimeter FortiGate",
        output_format="fortigate",
    )
    assert captured["json"] == {
        "name": "edge-firewall",
        "description": "Perimeter FortiGate",
        "entity_type": "firewall_integration",
        "output_format": "fortigate",
    }
    assert "Integration endpoint created" in result


@pytest.mark.asyncio
async def test_remediation_metrics_formats_and_validates_dates(monkeypatch):
    _forbid_requests(monkeypatch, (console_module,))
    handlers, _ = _build()
    assert "start_date" in await handlers["get_crowdsec_remediation_metrics"](
        start_date="yesterday"
    )

    _capture(
        monkeypatch,
        {
            "raw": {
                "dropped": [
                    {"total": 15000, "unit": "requests", "progression": 12, "data": []}
                ]
            },
            "computed": {
                "saved": {
                    "egress_traffic": {"total": 42, "unit": "GB"},
                    "log_lines": {"total": 90000, "unit": "lines"},
                }
            },
            "stats": {"dropped_rate": 7.5, "allowed_rate": 92.5},
        },
        module=console_module,
    )
    result = await handlers["get_crowdsec_remediation_metrics"]()
    assert "Dropped rate: 7.5%" in result
    assert "15000 requests" in result
    assert "+12%" in result
    assert "egress_traffic: 42 GB" in result
