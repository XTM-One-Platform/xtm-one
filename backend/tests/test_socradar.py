"""Tests for the SOCRadar integration provider.

Covers tool introspection, the incidents v4 list flow (params + envelope +
pagination hint), the alarm detail lookup, the status-change / comment /
assign / tag write paths (enum + required-arg guards), the ThreatFusion
indicator analysis and threat feed reads, the dark web / brand / fraud v2
finding flows (source/category routing + per-source filter guards), the ASM
digital asset flows (list, per-type add payloads, false-positive marking +
monitoring toggles), the takedown request flows (per-kind routing + guards +
progress tracking), the audit log trail, the in-body ``is_success: false``
envelope, and the compound-token base URL / API-Key auth header. Every API
call routes through the shared ``safe_request`` helper, which is
monkeypatched here so no network is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.credential_exposure import REDACTED
from app.integrations.providers import socradar as sr
from app.integrations.providers.socradar import build_tools, socradar_api_base
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://platform.socradar.com/api||424242||api-key-123"


class _StubResponse:
    def __init__(self, payload: Any, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.content = b"x" if payload not in (None, "") else b""

    def json(self) -> Any:
        return self._payload


class _Recorder:
    """Fake ``safe_request`` that routes by (method, url) and records calls."""

    def __init__(self, router):
        self._router = router
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        payload = self._router(method, url)
        return _StubResponse(payload), None


def _build(monkeypatch, router) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(router)
    monkeypatch.setattr(sr, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    return executor, recorder


def _alarm(alarm_id: int = 101) -> dict[str, Any]:
    return {
        "alarm_id": alarm_id,
        "alarm_risk_level": "HIGH",
        "status": "OPEN",
        "alarm_asset": "example.com",
        "date": "2026-07-01 10:30:00",
        "alarm_type_details": {
            "alarm_main_type": "Brand Protection",
            "alarm_sub_type": "Impersonating Domain",
        },
        "tags": ["reviewed"],
        "alarm_text": "Suspicious lookalike domain detected.",
        "alarm_related_entities": [{"key": "domain", "value": "examp1e.com"}],
        "content": {"dns_information": "ns1.evil.example"},
    }


def _envelope(alarms: list[dict], total: int | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {"alarms": alarms}
    if total is not None:
        data["total_pages"] = max(1, (total + 99) // 100)
        data["total_records"] = total
    return {"is_success": True, "message": "Success", "data": data}


# ---------------------------------------------------------------------------
# URL normalisation
# ---------------------------------------------------------------------------


def test_socradar_api_base_normalises_paste_forms():
    assert socradar_api_base("") == "https://platform.socradar.com/api"
    assert (
        socradar_api_base(" https://platform.socradar.com/api/ ")
        == "https://platform.socradar.com/api"
    )
    # A pasted platform host without the /api prefix gets it appended.
    assert (
        socradar_api_base("https://platform.socradar.com")
        == "https://platform.socradar.com/api"
    )


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


def test_build_tools_returns_definitions_with_dummy_token():
    executor = CallableToolExecutor()
    tools = build_tools(token="https://dummy.example.com||__dummy__", executor=executor)
    names = {t.name for t in tools}
    assert "get_socradar_status" in names
    assert "list_socradar_incidents" in names
    assert "get_socradar_incident" in names
    assert "change_socradar_incident_status" in names
    assert "add_socradar_incident_comment" in names
    assert "assign_socradar_incident" in names
    assert "manage_socradar_incident_tags" in names
    assert "analyze_socradar_indicator" in names
    assert "get_socradar_threat_feed" in names
    assert "search_socradar_dark_web" in names
    assert "list_socradar_brand_protection" in names
    assert "list_socradar_fraud_protection" in names
    assert "list_socradar_digital_assets" in names
    assert "add_socradar_digital_asset" in names
    assert "manage_socradar_digital_asset" in names
    assert "create_socradar_takedown" in names
    assert "get_socradar_takedown_progress" in names
    assert "list_socradar_audit_logs" in names
    # Exact count so an accidentally dropped tool fails loudly.
    assert len(tools) == 18


def test_all_tools_use_socradar_source_and_sorted_priority():
    tools = build_tools(
        token="https://dummy.example.com||__dummy__", executor=CallableToolExecutor()
    )
    assert all(t.source == "integration:socradar" for t in tools)
    priorities = {t.name: t.priority for t in tools}
    assert priorities["list_socradar_incidents"] == 0
    assert priorities["analyze_socradar_indicator"] == 0
    assert priorities["change_socradar_incident_status"] == 1
    assert priorities["manage_socradar_incident_tags"] == 1


def test_missing_company_id_returns_no_tools():
    executor = CallableToolExecutor()
    tools = build_tools(token="https://platform.socradar.com/api||||key", executor=executor)
    assert tools == []


# ---------------------------------------------------------------------------
# Incidents list flow: auth header, params, envelope, pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_incidents_params_envelope_and_pagination(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: _envelope([_alarm()], total=42)
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="list_socradar_incidents",
            arguments={
                "status": "OPEN",
                "severities": "HIGH,CRITICAL",
                "start_date": "2026-07-01",
                "limit": 1,
                "page": 1,
            },
        )
    )
    assert not result.is_error
    assert "Found 42 incidents" in result.content
    assert "#101" in result.content
    assert "Brand Protection / Impersonating Domain" in result.content
    # Page-based pagination hint points to the next page.
    assert "page=2" in result.content
    call = recorder.calls[0]
    assert call["method"] == "get"
    assert "/company/424242/incidents/v4" in call["url"]
    assert call["params"]["status"] == ["OPEN"]
    assert call["params"]["severities"] == ["HIGH", "CRITICAL"]
    assert call["params"]["start_date"] == "2026-07-01"
    assert call["params"]["include_total_records"] == "true"
    # API-Key (not Bearer) auth header.
    assert call["headers"]["API-Key"] == "api-key-123"


@pytest.mark.asyncio
async def test_list_incidents_rejects_invalid_enums(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: _envelope([]))
    bad_status = await executor.execute_tool(
        ToolCall(
            id="c2", name="list_socradar_incidents", arguments={"status": "NUKED"}
        )
    )
    assert "status values must be among" in bad_status.content
    bad_sev = await executor.execute_tool(
        ToolCall(
            id="c3", name="list_socradar_incidents", arguments={"severities": "EXTREME"}
        )
    )
    assert "severities values must be among" in bad_sev.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_list_incidents_handles_bare_list_payload(monkeypatch):
    """Without total counting the API returns data as a bare alarm list."""
    executor, _ = _build(
        monkeypatch,
        lambda m, u: {"is_success": True, "data": [_alarm(7)]},
    )
    result = await executor.execute_tool(
        ToolCall(id="c4", name="list_socradar_incidents", arguments={})
    )
    assert not result.is_error
    assert "#7" in result.content


@pytest.mark.asyncio
async def test_envelope_failure_is_surfaced(monkeypatch):
    executor, _ = _build(
        monkeypatch,
        lambda m, u: {"is_success": False, "message": "Check your API key."},
    )
    result = await executor.execute_tool(
        ToolCall(id="c5", name="list_socradar_incidents", arguments={})
    )
    assert "Check your API key." in result.content


# ---------------------------------------------------------------------------
# Incident detail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_incident_uses_notification_ids_and_formats_detail(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: _envelope([_alarm(555)]))
    result = await executor.execute_tool(
        ToolCall(id="c6", name="get_socradar_incident", arguments={"alarm_id": "555"})
    )
    assert not result.is_error
    assert "#555" in result.content
    assert "Related entities:" in result.content
    assert "examp1e.com" in result.content
    assert "Content fields: dns_information" in result.content
    call = recorder.calls[0]
    assert call["params"] == {"notification_ids": ["555"]}


@pytest.mark.asyncio
async def test_get_incident_not_found(monkeypatch):
    executor, _ = _build(monkeypatch, lambda m, u: _envelope([]))
    result = await executor.execute_tool(
        ToolCall(id="c7", name="get_socradar_incident", arguments={"alarm_id": "999"})
    )
    assert "No incident found" in result.content


# ---------------------------------------------------------------------------
# Status change (write)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_change_status_maps_reason_to_numeric_code(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"is_success": True, "data": {}}
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c8",
            name="change_socradar_incident_status",
            arguments={
                "alarm_ids": "101, 102",
                "status_reason": "false_positive",
                "comments": "Not a real threat",
            },
        )
    )
    assert not result.is_error
    assert "FALSE_POSITIVE" in result.content
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert "/company/424242/alarms/status/change" in call["url"]
    assert call["json"] == {
        "alarm_ids": ["101", "102"],
        "status": 9,
        "comments": "Not a real threat",
    }


@pytest.mark.asyncio
async def test_change_status_rejects_invalid_reason(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c9",
            name="change_socradar_incident_status",
            arguments={"alarm_ids": "101", "status_reason": "NUKE"},
        )
    )
    assert "status_reason must be one of" in result.content
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Comment / assign / tags (writes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_comment_body(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"is_success": True, "data": {}}
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c10",
            name="add_socradar_incident_comment",
            arguments={
                "alarm_id": "101",
                "comment": "Investigating",
                "user_email": "analyst@acme.com",
            },
        )
    )
    assert not result.is_error
    call = recorder.calls[0]
    assert "/company/424242/alarm/add/comment/v2" in call["url"]
    assert call["json"] == {
        "alarm_id": 101,
        "user_email": "analyst@acme.com",
        "comment": "Investigating",
    }


@pytest.mark.asyncio
async def test_add_comment_requires_numeric_alarm_id(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c11",
            name="add_socradar_incident_comment",
            arguments={
                "alarm_id": "abc",
                "comment": "x",
                "user_email": "a@b.c",
            },
        )
    )
    assert "must be numeric" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_assign_incident_splits_emails(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"is_success": True, "data": {}}
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c12",
            name="assign_socradar_incident",
            arguments={
                "alarm_id": "101",
                "user_emails": "a@acme.com, b@acme.com",
            },
        )
    )
    assert not result.is_error
    call = recorder.calls[0]
    assert "/company/424242/alarm/101/assignee" in call["url"]
    assert call["json"] == {"user_emails": ["a@acme.com", "b@acme.com"]}


@pytest.mark.asyncio
async def test_manage_tags_add_and_remove_verbs(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"is_success": True, "data": {}}
    )
    added = await executor.execute_tool(
        ToolCall(
            id="c13",
            name="manage_socradar_incident_tags",
            arguments={"action": "add", "alarm_id": "101", "tag": "reviewed"},
        )
    )
    assert not added.is_error
    assert recorder.calls[0]["method"] == "post"
    assert recorder.calls[0]["json"] == {"alarm_id": 101, "tag": "reviewed"}
    removed = await executor.execute_tool(
        ToolCall(
            id="c14",
            name="manage_socradar_incident_tags",
            arguments={"action": "remove", "alarm_id": "101", "tag": "reviewed"},
        )
    )
    assert not removed.is_error
    assert recorder.calls[1]["method"] == "delete"
    # Invalid action is guarded before any HTTP call.
    guarded = await executor.execute_tool(
        ToolCall(
            id="c15",
            name="manage_socradar_incident_tags",
            arguments={"action": "purge", "alarm_id": "101", "tag": "x"},
        )
    )
    assert "action must be one of" in guarded.content
    assert len(recorder.calls) == 2


# ---------------------------------------------------------------------------
# ThreatFusion analysis + threat feed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyze_indicator_formats_analysis(monkeypatch):
    payload = {
        "is_success": True,
        "data": {
            "value": "1.2.3.4",
            "classification": "ipv4",
            "score": 850,
            "is_whitelisted": False,
            "findings": [{"feed": "x"}, {"feed": "y"}],
            "whois": {"asn": "AS4134", "country": "CN"},
            "dns_info": {"A": ["1.2.3.4"]},
            "geo_location": [{"Ip": "1.2.3.4", "CountryCode": "CN"}],
        },
    }
    executor, recorder = _build(monkeypatch, lambda m, u: payload)
    result = await executor.execute_tool(
        ToolCall(
            id="c16",
            name="analyze_socradar_indicator",
            arguments={"entity": "1.2.3.4"},
        )
    )
    assert not result.is_error
    assert "850/1000" in result.content
    assert "Findings (feed encounters): 2" in result.content
    assert "asn: AS4134" in result.content
    assert "CountryCode: CN" in result.content
    call = recorder.calls[0]
    assert "/threat/analysis" in call["url"]
    # ThreatFusion takes the key as a query param (vendor client behaviour).
    assert call["params"] == {"key": "api-key-123", "entity": "1.2.3.4"}


@pytest.mark.asyncio
async def test_get_threat_feed_formats_and_caps_items(monkeypatch):
    items = [
        {
            "feed": f"bad{i}.example.com",
            "feed_type": "hostname",
            "maintainer_name": "SOCRadar",
            "first_seen_date": "2026-06-01T00:00:00",
            "latest_seen_date": "2026-07-01T00:00:00",
            "extra_info": {"score": 900, "seen_count": 3},
        }
        for i in range(5)
    ]
    executor, recorder = _build(monkeypatch, lambda m, u: items)
    result = await executor.execute_tool(
        ToolCall(
            id="c17",
            name="get_socradar_threat_feed",
            arguments={"collection_id": "col-1", "limit": 2},
        )
    )
    assert not result.is_error
    assert "bad0.example.com" in result.content
    assert "Showing the first 2 of 5 IOCs" in result.content
    call = recorder.calls[0]
    assert "/threat/intelligence/feed_list/col-1.json" in call["url"]
    assert call["params"] == {"v": 2, "key": "api-key-123"}


# ---------------------------------------------------------------------------
# Dark web monitoring
# ---------------------------------------------------------------------------


def _finding(alarm_id: int = 9001, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "alarmId": alarm_id,
        "notificationId": alarm_id + 1,
        "status": "OPEN",
        "source": "pastebin.com",
        "discoveryDate": "2026-07-02 08:00:00",
        "contentPreview": "user:pass leaked for acme.com",
        "relatedAlarm": {"status": "OPEN", "url": "https://x"},
        "history": [],
    }
    base.update(extra)
    return base


def _findings_envelope(items: list[dict], total: int | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {"data": items}
    if total is not None:
        data["total_data_count"] = total
    return {
        "is_success": True,
        "message": "Success",
        "response_code": 200,
        "data": data,
    }


@pytest.mark.asyncio
async def test_dark_web_routes_source_and_maps_params(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: _findings_envelope([_finding()], total=57)
    )
    result = await executor.execute_tool(
        ToolCall(
            id="d1",
            name="search_socradar_dark_web",
            arguments={
                "source": "botnet_data",
                "search_string": "acme.com",
                "record_status": "OPEN",
                "alarm_id": "9001",
                "limit": 1,
            },
        )
    )
    assert not result.is_error
    assert "Found 57" in result.content
    assert "#9001" in result.content
    # The preview's ``user:pass`` pair keeps the account, masks the secret.
    assert "user:[redacted] leaked for acme.com" in result.content
    assert "user:pass" not in result.content
    assert "page=2" in result.content
    call = recorder.calls[0]
    assert "/company/424242/dark-web-monitoring/botnet-data/v2" in call["url"]
    # botnet_data uses searchString/recordStatus and the lowercase alarmid.
    assert call["params"]["searchString"] == "acme.com"
    assert call["params"]["recordStatus"] == "OPEN"
    assert call["params"]["alarmid"] == "9001"


@pytest.mark.asyncio
async def test_dark_web_date_sources_use_alarm_id_camel_case(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: _findings_envelope([_finding(1)])
    )
    result = await executor.execute_tool(
        ToolCall(
            id="d2",
            name="search_socradar_dark_web",
            arguments={
                "source": "pii_exposure",
                "start_date": "2026-06-01",
                "end_date": "2026-07-01",
                "alarm_id": "42",
            },
        )
    )
    assert not result.is_error
    call = recorder.calls[0]
    assert "/dark-web-monitoring/pii-exposure/v2" in call["url"]
    assert call["params"]["startDate"] == "2026-06-01"
    assert call["params"]["endDate"] == "2026-07-01"
    assert call["params"]["alarmId"] == "42"
    assert "searchString" not in call["params"]


@pytest.mark.asyncio
async def test_dark_web_guards_invalid_source_and_param_combos(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    bad_source = await executor.execute_tool(
        ToolCall(
            id="d3", name="search_socradar_dark_web", arguments={"source": "onion"}
        )
    )
    assert "source must be one of" in bad_source.content
    bad_search = await executor.execute_tool(
        ToolCall(
            id="d4",
            name="search_socradar_dark_web",
            arguments={"source": "blackmarket", "search_string": "x"},
        )
    )
    assert "search_string is only supported" in bad_search.content
    bad_status = await executor.execute_tool(
        ToolCall(
            id="d5",
            name="search_socradar_dark_web",
            arguments={"source": "im_content", "record_status": "OPEN"},
        )
    )
    assert "record_status is only supported" in bad_status.content
    bad_alarm = await executor.execute_tool(
        ToolCall(
            id="d6",
            name="search_socradar_dark_web",
            arguments={"source": "im_content", "alarm_id": "1"},
        )
    )
    assert "alarm_id is not supported" in bad_alarm.content
    bad_dates = await executor.execute_tool(
        ToolCall(
            id="d7",
            name="search_socradar_dark_web",
            arguments={"source": "botnet_data", "start_date": "2026-06-01"},
        )
    )
    assert "start_date/end_date are not supported" in bad_dates.content
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Brand protection + fraud protection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_brand_protection_routes_category_and_filters(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        lambda m, u: _findings_envelope(
            [_finding(77, domain="examp1e.com", riskScore=88)], total=3
        ),
    )
    result = await executor.execute_tool(
        ToolCall(
            id="b1",
            name="list_socradar_brand_protection",
            arguments={
                "category": "impersonating_domains",
                "search_string": "examp1e",
                "record_status": "OPEN",
                "country_code": "US",
                "include_historical": True,
            },
        )
    )
    assert not result.is_error
    assert "#77" in result.content
    # The fields the category declares are shown by value.
    assert "domain: examp1e.com | riskScore: 88" in result.content
    call = recorder.calls[0]
    assert "/company/424242/brand-protection/impersonating-domains/v2" in call["url"]
    assert call["params"]["searchString"] == "examp1e"
    assert call["params"]["recordStatus"] == "OPEN"
    assert call["params"]["countryCode"] == "US"
    assert call["params"]["includeHistorical"] is True


@pytest.mark.asyncio
async def test_brand_protection_rejects_unknown_category(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="b2",
            name="list_socradar_brand_protection",
            arguments={"category": "typosquats"},
        )
    )
    assert "category must be one of" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_fraud_protection_filters_and_expired_flag(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: _findings_envelope([_finding(5)], total=1)
    )
    result = await executor.execute_tool(
        ToolCall(
            id="f1",
            name="list_socradar_fraud_protection",
            arguments={
                "search_string": "424242",
                "expired": "false",
                "include_history": True,
            },
        )
    )
    assert not result.is_error
    call = recorder.calls[0]
    assert "/company/424242/fraud-protection/v2" in call["url"]
    assert call["params"]["searchString"] == "424242"
    assert call["params"]["expired"] is False
    assert call["params"]["includeHistory"] is True
    guarded = await executor.execute_tool(
        ToolCall(
            id="f2",
            name="list_socradar_fraud_protection",
            arguments={"expired": "maybe"},
        )
    )
    assert "expired must be" in guarded.content
    assert len(recorder.calls) == 1


@pytest.mark.asyncio
async def test_findings_envelope_failure_is_surfaced(monkeypatch):
    executor, _ = _build(
        monkeypatch,
        lambda m, u: {"is_success": False, "message": "Module not licensed."},
    )
    result = await executor.execute_tool(
        ToolCall(
            id="f3",
            name="search_socradar_dark_web",
            arguments={"source": "blackmarket"},
        )
    )
    assert "Module not licensed." in result.content


# ---------------------------------------------------------------------------
# Leaked secrets: presence, never by value (#5686)
# ---------------------------------------------------------------------------

_FAKE_PASSWORD = "Fake-Passw0rd-xyz"
_FAKE_COOKIE = "fake-cookie-value-0123"
_FAKE_TOKEN = "fake-token-value-4567"
_FAKE_CARD = "4111111111111111"
_FAKE_CARD_MASKED = "411111******1111"
_FAKE_CVV = "987"
_UNKNOWN_VALUE = "unknown-field-value-xyz"
_FAKE_SECRETS = (_FAKE_PASSWORD, _FAKE_COOKIE, _FAKE_TOKEN, _FAKE_CARD, _FAKE_CVV, _UNKNOWN_VALUE)


def _leaking_finding() -> dict[str, Any]:
    return _finding(
        1,
        contentPreview="combo list sample",
        username="jdoe@acme.example",
        password=_FAKE_PASSWORD,
        sessionCookie=_FAKE_COOKIE,
        authToken=_FAKE_TOKEN,
        cardNumber=_FAKE_CARD,
        cvv=_FAKE_CVV,
        mysteryField=_UNKNOWN_VALUE,
    )


_LEAK_CASES = [
    *[("search_socradar_dark_web", {"source": s}) for s in sorted(sr._DARK_WEB_SOURCES)],
    *[("list_socradar_brand_protection", {"category": c}) for c in sorted(sr._BRAND_CATEGORIES)],
    ("list_socradar_fraud_protection", {}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments"),
    _LEAK_CASES,
    ids=[f"{t}:{next(iter(a.values()), 'fraud')}" for t, a in _LEAK_CASES],
)
async def test_findings_render_secrets_as_presence_never_by_value(monkeypatch, tool, arguments):
    executor, _ = _build(monkeypatch, lambda m, u: _findings_envelope([_leaking_finding()]))
    result = await executor.execute_tool(ToolCall(id="leak", name=tool, arguments=arguments))
    assert not result.is_error
    for secret in _FAKE_SECRETS:
        assert secret not in result.content
    assert (
        "password: captured | sessionCookie: captured | authToken: captured | "
        f"cardNumber: {_FAKE_CARD_MASKED} | cvv: captured"
    ) in result.content
    # An undeclared field is named, never shown by value (fail closed).
    assert "Other fields (values not shown): " in result.content
    assert "mysteryField" in result.content


@pytest.mark.asyncio
async def test_finding_secret_flags_read_as_not_captured(monkeypatch):
    executor, _ = _build(
        monkeypatch,
        lambda m, u: _findings_envelope([_finding(2, cvv=False, password="", bin="411111")]),
    )
    result = await executor.execute_tool(
        ToolCall(id="flags", name="list_socradar_fraud_protection", arguments={})
    )
    assert "bin: 411111" in result.content
    assert "cvv: not captured" in result.content
    assert "password: not captured" in result.content


@pytest.mark.asyncio
async def test_unknown_source_shape_never_shows_a_value(monkeypatch):
    """A field no source declares stays out by value, whatever its name."""
    executor, _ = _build(
        monkeypatch,
        lambda m, u: _findings_envelope(
            [_finding(3, contentPreview="", blob=_UNKNOWN_VALUE, nested={"x": _UNKNOWN_VALUE})]
        ),
    )
    result = await executor.execute_tool(
        ToolCall(id="unknown", name="search_socradar_dark_web", arguments={"source": "im_content"})
    )
    assert _UNKNOWN_VALUE not in result.content
    assert "Other fields (values not shown): blob, nested" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("preview", "masked"),
    [
        ("user:pass leaked for acme.com", "user:[redacted] leaked for acme.com"),
        (
            f"jdoe@acme.example:{_FAKE_PASSWORD} from stealer",
            "jdoe@acme.example:[redacted] from stealer",
        ),
        (
            f"https://jdoe:{_FAKE_PASSWORD}@vpn.acme.example/login",
            "https://jdoe:[redacted]@vpn.acme.example/login",
        ),
        (f"card {_FAKE_CARD} cvv={_FAKE_CVV}", f"card {_FAKE_CARD_MASKED} cvv=[redacted]"),
    ],
)
async def test_content_preview_is_masked(monkeypatch, preview, masked):
    executor, _ = _build(
        monkeypatch, lambda m, u: _findings_envelope([_finding(4, contentPreview=preview)])
    )
    result = await executor.execute_tool(
        ToolCall(id="prev", name="search_socradar_dark_web", arguments={"source": "botnet_data"})
    )
    assert f"  {masked}" in result.content
    assert _FAKE_PASSWORD not in result.content
    assert _FAKE_CARD not in result.content


def _stolen_credentials_alarm() -> dict[str, Any]:
    alarm = _alarm(777)
    alarm["alarm_type_details"] = {
        "alarm_main_type": "Dark Web Monitoring",
        "alarm_sub_type": "Stolen Credentials",
    }
    alarm["alarm_asset"] = "acme.example"
    alarm["alarm_text"] = f"Credentials jdoe@acme.example:{_FAKE_PASSWORD} found in a stealer log."
    alarm["alarm_related_entities"] = [
        {"key": "email", "value": "jdoe@acme.example"},
        {"key": "password", "value": _FAKE_PASSWORD},
        {"key": "cookie", "value": _FAKE_COOKIE},
        {"key": "credential", "value": f"jdoe:{_FAKE_PASSWORD}"},
    ]
    alarm["alarm_related_assets"] = [{"key": "card_number", "value": [_FAKE_CARD]}]
    alarm["content"] = {
        "credential_details": {"username": "jdoe@acme.example", "password": _FAKE_PASSWORD},
        "log_content_link": f"https://files.example/log?sig={_FAKE_TOKEN}",
        "asset_match": "acme.example",
        "session_token": _FAKE_TOKEN,
        "card_cvv": _FAKE_CVV,
    }
    return alarm


@pytest.mark.asyncio
async def test_stolen_credentials_alarm_renders_presence_never_by_value(monkeypatch):
    executor, _ = _build(monkeypatch, lambda m, u: _envelope([_stolen_credentials_alarm()]))
    result = await executor.execute_tool(
        ToolCall(id="sc", name="get_socradar_incident", arguments={"alarm_id": "777"})
    )
    assert not result.is_error
    for secret in _FAKE_SECRETS:
        assert secret not in result.content
    assert "Credentials jdoe@acme.example:[redacted] found in a stealer log." in result.content
    assert "    email: jdoe@acme.example" in result.content
    assert "    password: captured" in result.content
    assert "    cookie: captured" in result.content
    assert "    credential: captured" in result.content
    assert f"    card_number: {_FAKE_CARD_MASKED}" in result.content
    assert (
        "Content fields: credential_details, log_content_link, asset_match, "
        "session_token, card_cvv"
    ) in result.content
    assert "    credential_details: captured" in result.content
    assert "    asset_match: acme.example" in result.content
    assert "    session_token: captured" in result.content
    assert "    card_cvv: captured" in result.content
    # The stealer-log link is named, never shown by value.
    assert "log_content_link:" not in result.content


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        # The reviewer's case: an undeclared key is named, its value never shown.
        (
            [{"key": "Leaked Data", "value": "jdoe@acme.com|Hunter2!"}],
            ["  Related entities:", "    Other entities (values not shown): Leaked Data"],
        ),
        # Declared identifier keys by value (scrubbed), any case or separator.
        (
            [
                {"key": "domain", "value": "examp1e.com"},
                {"key": "IP Address", "value": "203.0.113.7"},
                {"key": "email", "value": "jdoe@acme.example"},
                {"key": "URL", "value": "https://bob:pw@login.acme.example/x"},
            ],
            [
                "  Related entities:",
                "    domain: examp1e.com",
                "    IP Address: 203.0.113.7",
                "    email: jdoe@acme.example",
                f"    URL: https://bob:{REDACTED}@login.acme.example/x",
            ],
        ),
        # Secret-named keys as presence or BIN + last four, the rest named once.
        (
            [
                {"key": "password", "value": _FAKE_PASSWORD},
                {"key": "card_number", "value": [_FAKE_CARD]},
                {"key": "notes", "value": _UNKNOWN_VALUE},
                {"key": "notes", "value": _UNKNOWN_VALUE},
                {"key": "raw", "value": {"password": _FAKE_PASSWORD}},
            ],
            [
                "  Related entities:",
                "    password: captured",
                f"    card_number: {_FAKE_CARD_MASKED}",
                "    Other entities (values not shown): notes, raw",
            ],
        ),
        # A declared key whose value is a structure is named, never dumped.
        (
            [{"key": "domain", "value": {"credentials": f"jdoe:{_FAKE_PASSWORD}"}}],
            ["  Related entities:", "    Other entities (values not shown): domain"],
        ),
        # Entries without a key are skipped; an empty or non-list value renders nothing.
        ([{"value": _UNKNOWN_VALUE}, "x"], ["  Related entities:"]),
        ([], []),
        (None, []),
        # More than ten entries: the first ten, then a count.
        (
            [{"key": "domain", "value": f"d{i}.example"} for i in range(12)],
            ["  Related entities:"]
            + [f"    domain: d{i}.example" for i in range(10)]
            + ["    ... and 2 more entities"],
        ),
    ],
)
def test_related_entities_go_through_the_allow_list(items, expected):
    assert sr._related_lines("entities", items) == expected


@pytest.mark.asyncio
async def test_alarm_related_entities_and_assets_fail_closed(monkeypatch):
    alarm = _alarm(780)
    alarm["alarm_related_entities"] = [
        {"key": "email", "value": "jdoe@acme.example"},
        {"key": "Leaked Data", "value": "jdoe@acme.com|Hunter2!"},
    ]
    alarm["alarm_related_assets"] = [
        {"key": "domain", "value": "acme.example"},
        {"key": "Stealer Log Line", "value": f"https://acme.example/login jdoe {_FAKE_PASSWORD}"},
    ]
    executor, _ = _build(monkeypatch, lambda m, u: _envelope([alarm]))
    result = await executor.execute_tool(
        ToolCall(id="re", name="get_socradar_incident", arguments={"alarm_id": "780"})
    )
    lines = result.content.splitlines()
    start = lines.index("  Related entities:")
    assert lines[start : start + 6] == [
        "  Related entities:",
        "    email: jdoe@acme.example",
        "    Other entities (values not shown): Leaked Data",
        "  Related assets:",
        "    domain: acme.example",
        "    Other assets (values not shown): Stealer Log Line",
    ]
    assert "Hunter2" not in result.content
    assert _FAKE_PASSWORD not in result.content


@pytest.mark.asyncio
async def test_unknown_alarm_type_lists_content_field_names_only(monkeypatch):
    alarm = _alarm(778)
    alarm["alarm_type_details"] = {"alarm_main_type": "Brand New Type", "alarm_sub_type": ""}
    alarm["content"] = {"dns_information": _UNKNOWN_VALUE, "notes": _UNKNOWN_VALUE}
    executor, _ = _build(monkeypatch, lambda m, u: _envelope([alarm]))
    result = await executor.execute_tool(
        ToolCall(id="ut", name="get_socradar_incident", arguments={"alarm_id": "778"})
    )
    assert "Content fields: dns_information, notes" in result.content
    assert _UNKNOWN_VALUE not in result.content


@pytest.mark.asyncio
async def test_declared_alarm_content_field_is_shown(monkeypatch):
    executor, _ = _build(monkeypatch, lambda m, u: _envelope([_alarm(779)]))
    result = await executor.execute_tool(
        ToolCall(id="bp", name="get_socradar_incident", arguments={"alarm_id": "779"})
    )
    assert "    dns_information: ns1.evil.example" in result.content


# ---------------------------------------------------------------------------
# Attack surface management (digital footprint)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_digital_assets_params_and_formatting(monkeypatch):
    payload = _findings_envelope(
        [
            {
                "id": 314,
                "asset_name": "shop.acme.com",
                "is_monitor_on": True,
                "source": "auto_discovery",
                "sub_type": "sub",
            }
        ],
        total=25,
    )
    executor, recorder = _build(monkeypatch, lambda m, u: payload)
    result = await executor.execute_tool(
        ToolCall(
            id="a1",
            name="list_socradar_digital_assets",
            arguments={
                "asset_type": "domain",
                "sort_by": "asset_name",
                "sort_desc": True,
                "limit": 1,
            },
        )
    )
    assert not result.is_error
    assert "#314 shop.acme.com" in result.content
    assert "monitored" in result.content
    assert "page=2" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/company/424242/asm")
    assert call["params"] == {
        "assetType": "domain",
        "pageNumber": 1,
        "pageLimit": 1,
        "sortBy": "asset_name",
        "sortDesc": True,
    }


@pytest.mark.asyncio
async def test_list_digital_assets_rejects_unknown_type(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="a2",
            name="list_socradar_digital_assets",
            arguments={"asset_type": "satellite"},
        )
    )
    assert "asset_type must be one of" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_add_digital_asset_builds_per_type_payloads(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"is_success": True, "data": {}}
    )
    added = await executor.execute_tool(
        ToolCall(
            id="a3",
            name="add_socradar_digital_asset",
            arguments={
                "asset_type": "domain",
                "asset_name": "acme-shop.com",
                "domain_type": "main",
            },
        )
    )
    assert not added.is_error
    call = recorder.calls[0]
    assert "/company/424242/asm/add/domain" in call["url"]
    assert call["json"] == {
        "asset_name": "acme-shop.com",
        "source": "manual",
        "domain": "acme-shop.com",
        "domain_type": "main",
    }
    ip_added = await executor.execute_tool(
        ToolCall(
            id="a4",
            name="add_socradar_digital_asset",
            arguments={"asset_type": "ipAddress", "asset_name": "1.2.3.4"},
        )
    )
    assert not ip_added.is_error
    assert recorder.calls[1]["json"]["ip_address"] == "1.2.3.4"
    # Domain assets without domain_type are guarded before any HTTP call.
    guarded = await executor.execute_tool(
        ToolCall(
            id="a5",
            name="add_socradar_digital_asset",
            arguments={"asset_type": "domain", "asset_name": "x.com"},
        )
    )
    assert "domain_type is required" in guarded.content
    bucket_guarded = await executor.execute_tool(
        ToolCall(
            id="a6",
            name="add_socradar_digital_asset",
            arguments={"asset_type": "cloudBuckets", "asset_name": "acme-data"},
        )
    )
    assert "cloud_platform_name and access_state are required" in (
        bucket_guarded.content
    )
    assert len(recorder.calls) == 2


@pytest.mark.asyncio
async def test_manage_digital_asset_routes_fp_and_monitoring(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"is_success": True, "data": {}}
    )
    fp = await executor.execute_tool(
        ToolCall(
            id="a7",
            name="manage_socradar_digital_asset",
            arguments={
                "action": "mark_false_positive",
                "asset_id": "314",
                "asset_type": "domain",
                "sub_type": "sub",
                "notes": "Not our domain",
            },
        )
    )
    assert not fp.is_error
    assert "/company/424242/asm/fp" in recorder.calls[0]["url"]
    assert recorder.calls[0]["json"] == {
        "asset_id": 314,
        "asset_type": "domain",
        "sub_type": "sub",
        "notes": "Not our domain",
    }
    toggled = await executor.execute_tool(
        ToolCall(
            id="a8",
            name="manage_socradar_digital_asset",
            arguments={
                "action": "disable_monitoring",
                "asset_id": "314",
                "asset_type": "domain",
            },
        )
    )
    assert not toggled.is_error
    assert "/company/424242/asm/monitor" in recorder.calls[1]["url"]
    assert recorder.calls[1]["json"] == {
        "asset_id": 314,
        "asset_type": "domain",
        "is_monitor_on": False,
    }
    guarded = await executor.execute_tool(
        ToolCall(
            id="a9",
            name="manage_socradar_digital_asset",
            arguments={
                "action": "delete",
                "asset_id": "314",
                "asset_type": "domain",
            },
        )
    )
    assert "action must be one of" in guarded.content
    non_numeric = await executor.execute_tool(
        ToolCall(
            id="a10",
            name="manage_socradar_digital_asset",
            arguments={
                "action": "enable_monitoring",
                "asset_id": "abc",
                "asset_type": "domain",
            },
        )
    )
    assert "must be numeric" in non_numeric.content
    assert len(recorder.calls) == 2


# ---------------------------------------------------------------------------
# Takedowns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_takedown_routes_kind_and_payload(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"is_success": True, "data": {}}
    )
    domain = await executor.execute_tool(
        ToolCall(
            id="t1",
            name="create_socradar_takedown",
            arguments={
                "kind": "domain",
                "entity": "acme-login.evil",
                "abuse_type": "phishing",
                "notes": "Active phishing kit",
                "send_alarm": True,
            },
        )
    )
    assert not domain.is_error
    call = recorder.calls[0]
    assert call["url"].endswith("/add/company/424242/takedown/request")
    assert call["json"] == {
        "entity": "acme-login.evil",
        "abuse_type": "phishing",
        "type": "domain",
        "send_alarm": True,
        "notes": "Active phishing kit",
    }
    social = await executor.execute_tool(
        ToolCall(
            id="t2",
            name="create_socradar_takedown",
            arguments={
                "kind": "social_media",
                "entity": "https://x.com/fake_acme",
            },
        )
    )
    assert not social.is_error
    call = recorder.calls[1]
    assert call["url"].endswith("/takedown/request/social_media_risks")
    assert call["json"]["impersonating_account"] == "https://x.com/fake_acme"
    source_code = await executor.execute_tool(
        ToolCall(
            id="t3",
            name="create_socradar_takedown",
            arguments={"kind": "source_code", "entity": "8842"},
        )
    )
    assert not source_code.is_error
    call = recorder.calls[2]
    assert call["url"].endswith("/takedown/request/source_code_leaks")
    assert call["json"]["id"] == 8842


@pytest.mark.asyncio
async def test_create_takedown_guards(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    bad_kind = await executor.execute_tool(
        ToolCall(
            id="t4",
            name="create_socradar_takedown",
            arguments={"kind": "billboard", "entity": "x"},
        )
    )
    assert "kind must be one of" in bad_kind.content
    missing_abuse = await executor.execute_tool(
        ToolCall(
            id="t5",
            name="create_socradar_takedown",
            arguments={"kind": "domain", "entity": "evil.com"},
        )
    )
    assert "abuse_type is required" in missing_abuse.content
    non_numeric_leak = await executor.execute_tool(
        ToolCall(
            id="t6",
            name="create_socradar_takedown",
            arguments={"kind": "source_code", "entity": "gh-leak"},
        )
    )
    assert "numeric source-code-leak finding ID" in non_numeric_leak.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_takedown_progress_query(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        lambda m, u: {
            "is_success": True,
            "data": {"status": "IN_PROGRESS", "last_update": "2026-07-05"},
        },
    )
    result = await executor.execute_tool(
        ToolCall(
            id="t7",
            name="get_socradar_takedown_progress",
            arguments={"asset_id": "555", "kind": "phishing_domain"},
        )
    )
    assert not result.is_error
    assert "status: IN_PROGRESS" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/get/company/424242/takedown/progress")
    assert call["params"] == {"asset_id": "555", "type": "phishing_domain"}


# ---------------------------------------------------------------------------
# Audit logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_logs_filters_and_formatting(monkeypatch):
    payload = _findings_envelope(
        [
            {
                "date": "2026-07-03T09:15:00",
                "user_email": "admin@acme.com",
                "action": "API key regenerated",
                "detail": "Settings > API Options",
            }
        ],
        total=1,
    )
    executor, recorder = _build(monkeypatch, lambda m, u: payload)
    result = await executor.execute_tool(
        ToolCall(
            id="l1",
            name="list_socradar_audit_logs",
            arguments={
                "start_date": "2026-07-01",
                "user_id": "17",
                "action_type": "settings",
            },
        )
    )
    assert not result.is_error
    assert "admin@acme.com" in result.content
    assert "API key regenerated" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/company/424242/auditlogs")
    assert call["params"]["startDate"] == "2026-07-01"
    assert call["params"]["userId"] == "17"
    assert call["params"]["actionType"] == "settings"


# ---------------------------------------------------------------------------
# Status / connection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_status_reports_company_scope(monkeypatch):
    executor, _ = _build(monkeypatch, lambda m, u: _envelope([_alarm()], total=1337))
    result = await executor.execute_tool(
        ToolCall(id="c18", name="get_socradar_status", arguments={})
    )
    assert not result.is_error
    assert "company ID 424242" in result.content
    assert "1337" in result.content
