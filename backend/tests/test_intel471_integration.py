"""Regression tests for the Intel 471 (Titan API) integration provider.

Covers the foundation slice (compound ``email||api_key`` token parsing + HTTP
Basic header construction, credentials/required-filter guards, camelCase
query-param mapping, offset/count clamping, consolidated get_intel471_report
routing), the malware / IOC / CVE / credential / underground slice (dataset
dispatch, at-least-one-filter guards, cursor stream mode), and the alert +
watcher write surface (UID-based alert paging, watcher payload construction,
tool priorities, destructive-action guards).
"""

from __future__ import annotations

import base64

import pytest

from app.integrations.providers import intel471
from app.integrations.providers.intel471 import (
    Intel471Integration,
    _common,
    build_basic_headers,
    build_tools,
)
from app.services.integration_tools import _SafeIntegrationExecutor

# api_username||api_key (HTTP Basic compound token: email + Titan API key)
_TOKEN = "analyst@example.com||titan-key-value"


class _FakeResp:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.headers: dict = {}

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


class _FakeClient:
    """httpx.AsyncClient stand-in that records requests and serves one payload."""

    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self._status = status_code
        self.requests: list[tuple[str, str, dict]] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def request(self, method, url, **kwargs) -> _FakeResp:
        self.requests.append((str(method), str(url), kwargs))
        return _FakeResp(self._payload, self._status)


def _patch(monkeypatch, payload, status_code: int = 200) -> dict:
    holder: dict = {}

    def _factory(*args, **kwargs):
        holder["client"] = _FakeClient(payload, status_code)
        return holder["client"]

    monkeypatch.setattr(_common.httpx, "AsyncClient", _factory)
    return holder


def _handlers() -> dict:
    executor = _SafeIntegrationExecutor()
    build_tools(_TOKEN, executor)
    return executor._handlers


# ---------------------------------------------------------------------------
# Auth + token parsing
# ---------------------------------------------------------------------------


def test_basic_headers_encode_email_and_key():
    headers = build_basic_headers("analyst@example.com", "titan-key-value")
    expected = "Basic " + base64.b64encode(
        b"analyst@example.com:titan-key-value"
    ).decode("ascii")
    assert headers["Authorization"] == expected
    assert "application/json" in headers["Accept"]


def test_basic_headers_strip_whitespace():
    """A pasted email/key with trailing newline must not corrupt the header."""
    headers = build_basic_headers(" analyst@example.com\n", " titan-key-value \n")
    expected = "Basic " + base64.b64encode(
        b"analyst@example.com:titan-key-value"
    ).decode("ascii")
    assert headers["Authorization"] == expected


def test_build_tools_returns_empty_without_key():
    """No API key -> no tools (integration is unconfigured)."""
    executor = _SafeIntegrationExecutor()
    tools = build_tools("analyst@example.com||", executor)
    assert tools == []


def test_build_tools_returns_empty_without_email():
    """A delimiter-less token means api_username was never set -> no tools."""
    executor = _SafeIntegrationExecutor()
    tools = build_tools("just-a-raw-api-key", executor)
    assert tools == []


def test_build_tools_registers_adversary_surface():
    handlers = _handlers()
    for name in (
        "search_intel471",
        "list_intel471_actors",
        "get_intel471_actor",
        "search_intel471_reports",
        "get_intel471_report",
        "search_intel471_breach_alerts",
        "search_intel471_spot_reports",
        "search_intel471_situation_reports",
        "search_intel471_entities",
        "search_intel471_news",
        "list_intel471_girs",
        "list_intel471_tags",
    ):
        assert name in handlers, f"missing tool {name}"


# ---------------------------------------------------------------------------
# Required-filter + id guards (no HTTP call)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_actors_requires_actor_or_forum():
    result = await _handlers()["list_intel471_actors"]()
    assert "actor" in result.lower() and "forum" in result.lower()


@pytest.mark.asyncio
async def test_global_search_requires_a_term():
    result = await _handlers()["search_intel471"]()
    assert "at least one search term" in result.lower()


@pytest.mark.asyncio
async def test_breach_alerts_require_a_filter():
    result = await _handlers()["search_intel471_breach_alerts"]()
    assert "breach_alert" in result and "victim" in result


@pytest.mark.asyncio
async def test_get_report_rejects_unknown_type():
    result = await _handlers()["get_intel471_report"](uid="abc", report_type="bogus")
    assert "report_type must be one of" in result


@pytest.mark.asyncio
async def test_get_actor_rejects_path_separator():
    result = await _handlers()["get_intel471_actor"](uid="../me")
    assert "path separators" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_uid", ["..", ".", "a\\b", "a\x00b", ""])
async def test_get_actor_rejects_unsafe_segments(bad_uid):
    """The actor uid guard matches the stricter watcher-side validation."""
    result = await _handlers()["get_intel471_actor"](uid=bad_uid)
    assert "path separators" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_uid", ["..", "a\\b", "a\x00b"])
async def test_get_report_rejects_unsafe_segments(bad_uid):
    result = await _handlers()["get_intel471_report"](uid=bad_uid)
    assert "path separators" in result


def test_more_hint_suggests_last_reachable_page():
    """The page at exactly MAX_OFFSET is reachable and must be suggested."""
    hint = _common.more_hint(total=5000, shown=10, offset=_common.MAX_OFFSET - 10)
    assert f"offset={_common.MAX_OFFSET}" in hint
    # Past the window the hint switches to the narrow-your-filters message.
    beyond = _common.more_hint(total=5000, shown=10, offset=_common.MAX_OFFSET)
    assert "narrow the" in beyond and "pass offset" not in beyond


# ---------------------------------------------------------------------------
# camelCase param mapping + clamping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_actor_search_maps_camelcase_and_clamps(monkeypatch):
    holder = _patch(
        monkeypatch,
        {"actorTotalCount": 1, "actors": [{"uid": "a1", "handle": "synthx"}]},
    )
    result = await _handlers()["list_intel471_actors"](
        actor="synthx", last_updated_from="2days", count=999, offset=99999
    )
    assert "synthx" in result
    _method, url, kwargs = holder["client"].requests[0]
    params = kwargs["params"]
    assert url.endswith("/actors")
    assert params["actor"] == "synthx"
    assert params["lastUpdatedFrom"] == "2days"  # camelCase mapping
    assert params["count"] == _common.MAX_COUNT  # clamped from 999
    assert params["offset"] == _common.MAX_OFFSET  # clamped from 99999


@pytest.mark.asyncio
async def test_bogus_sort_falls_back_to_default(monkeypatch):
    """A hallucinated sort value must degrade to the tool's default, not error."""
    holder = _patch(monkeypatch, {"actorTotalCount": 0, "actors": []})
    await _handlers()["list_intel471_actors"](actor="synthx", sort="bogus")
    assert holder["client"].requests[0][2]["params"]["sort"] == "latest"


@pytest.mark.asyncio
async def test_reports_search_maps_report_title_camelcase(monkeypatch):
    holder = _patch(
        monkeypatch,
        {"reportTotalCount": 1, "reports": [{"uid": "r1", "subject": "APT report"}]},
    )
    result = await _handlers()["search_intel471_reports"](
        report_title="ransomware", document_type="Malware Report"
    )
    assert "APT report" in result
    _method, _url, kwargs = holder["client"].requests[0]
    params = kwargs["params"]
    assert params["reportTitle"] == "ransomware"
    assert params["documentType"] == "Malware Report"


@pytest.mark.asyncio
async def test_get_report_routes_by_type(monkeypatch):
    holder = _patch(monkeypatch, {"uid": "b1", "data": {"breachAlert": {"title": "Acme breach"}}})
    result = await _handlers()["get_intel471_report"](
        uid="b1", report_type="breach_alert"
    )
    assert "Acme breach" in result
    _method, url, _kwargs = holder["client"].requests[0]
    assert url.endswith("/breachAlerts/b1")


@pytest.mark.asyncio
async def test_get_report_routes_malware_and_cve_types(monkeypatch):
    holder = _patch(monkeypatch, {"uid": "m1", "data": {"title": "Loader analysis"}})
    await _handlers()["get_intel471_report"](uid="m1", report_type="malware_report")
    assert holder["client"].requests[0][1].endswith("/malwareReports/m1")

    holder = _patch(monkeypatch, {"uid": "c1", "data": {"title": "CVE analysis"}})
    await _handlers()["get_intel471_report"](uid="c1", report_type="cve_report")
    assert holder["client"].requests[0][1].endswith("/cve/reports/c1")


@pytest.mark.asyncio
async def test_entities_search_sends_entity_and_type(monkeypatch):
    holder = _patch(
        monkeypatch,
        {"entityTotalCount": 1, "entities": [{"value": "a@b.com", "type": "EmailAddress"}]},
    )
    result = await _handlers()["search_intel471_entities"](
        entity="a@b.com", entity_type="EmailAddress"
    )
    assert "a@b.com" in result
    _method, url, kwargs = holder["client"].requests[0]
    assert url.endswith("/entities")
    assert kwargs["params"]["entity"] == "a@b.com"
    assert kwargs["params"]["entityType"] == "EmailAddress"


# ---------------------------------------------------------------------------
# Connectivity validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_connection_ok(monkeypatch):
    _patch(monkeypatch, {"girs": [{"data": {"gir": {"path": "1.1"}}}]})
    integ = Intel471Integration()
    assert await integ.validate_connection(_TOKEN) is True


@pytest.mark.asyncio
async def test_validate_connection_rejects_missing_key():
    ok, message = await intel471.validate_connection("analyst@example.com", "")
    assert ok is False
    assert "email" in message.lower() or "key" in message.lower()


# ---------------------------------------------------------------------------
# Malware / IOC / CVE / credential / underground surface (PR 2)
# ---------------------------------------------------------------------------


def test_build_tools_registers_malware_surface():
    handlers = _handlers()
    for name in (
        "search_intel471_indicators",
        "search_intel471_yara",
        "search_intel471_malware_events",
        "search_intel471_malware_reports",
        "search_intel471_malware_families",
        "search_intel471_iocs",
        "search_intel471_cve_reports",
        "search_intel471_credentials",
        "search_intel471_forum_posts",
        "search_intel471_private_messages",
        "search_intel471_instant_messages",
        "search_intel471_marketplaces",
        "search_intel471_data_leaks",
    ):
        assert name in handlers, f"missing tool {name}"


@pytest.mark.asyncio
async def test_iocs_require_value():
    result = await _handlers()["search_intel471_iocs"](ioc="")
    assert "'ioc'" in result and "required" in result.lower()


@pytest.mark.asyncio
async def test_yara_requires_a_filter():
    result = await _handlers()["search_intel471_yara"]()
    assert "at least one filter" in result.lower()


@pytest.mark.asyncio
async def test_forum_posts_require_a_filter():
    result = await _handlers()["search_intel471_forum_posts"]()
    assert "post" in result.lower() and "forum" in result.lower()


@pytest.mark.asyncio
async def test_credentials_reject_unknown_dataset():
    result = await _handlers()["search_intel471_credentials"](dataset="bogus", domain="x.com")
    assert "dataset must be one of" in result


@pytest.mark.asyncio
async def test_marketplaces_reject_unknown_dataset():
    result = await _handlers()["search_intel471_marketplaces"](dataset="bogus")
    assert "dataset must be one of" in result


@pytest.mark.asyncio
async def test_credentials_dataset_routes_to_path(monkeypatch):
    holder = _patch(
        monkeypatch,
        {"credentialSetTotalCount": 1, "credentialSets": [{"uid": "cs1", "data": {"name": "Acme breach"}}]},
    )
    result = await _handlers()["search_intel471_credentials"](
        dataset="credential_sets", domain="acme.com"
    )
    assert "Acme breach" in result
    _method, url, kwargs = holder["client"].requests[0]
    assert url.endswith("/credentialSets")
    assert kwargs["params"]["domain"] == "acme.com"


@pytest.mark.asyncio
async def test_indicators_stream_mode_uses_cursor(monkeypatch):
    holder = _patch(
        monkeypatch,
        {
            "indicators": [{"data": {"indicatorType": "ipv4", "indicatorData": {"address": "1.2.3.4"}}}],
            "cursorNext": "CURSOR2",
        },
    )
    result = await _handlers()["search_intel471_indicators"](
        malware_family="trickbot", stream=True, cursor="CURSOR1"
    )
    assert "1.2.3.4" in result
    assert "CURSOR2" in result  # surfaces the next cursor
    _method, url, kwargs = holder["client"].requests[0]
    assert url.endswith("/indicators/stream")
    assert kwargs["params"]["cursor"] == "CURSOR1"
    assert kwargs["params"]["malwareFamily"] == "trickbot"


@pytest.mark.asyncio
async def test_cve_reports_map_camelcase(monkeypatch):
    holder = _patch(
        monkeypatch,
        {"cveReportTotalCount": 1, "cveReports": [{"uid": "c1", "data": {"cveReport": {"name": "CVE-2021-44228", "riskLevel": "high"}}}]},
    )
    result = await _handlers()["search_intel471_cve_reports"](
        cve_name="CVE-2021-44228", risk_level="high", vendor_name="Apache"
    )
    assert "CVE-2021-44228" in result
    params = holder["client"].requests[0][2]["params"]
    assert params["cveName"] == "CVE-2021-44228"
    assert params["riskLevel"] == "high"
    assert params["vendorName"] == "Apache"


@pytest.mark.asyncio
async def test_marketplaces_products_dataset(monkeypatch):
    holder = _patch(
        monkeypatch,
        {"productTotalCount": 1, "products": [{"uid": "p1", "data": {"title": "Stolen DB"}}]},
    )
    result = await _handlers()["search_intel471_marketplaces"](
        dataset="products", text="database", product_type="data"
    )
    assert "Stolen DB" in result
    _method, url, kwargs = holder["client"].requests[0]
    assert url.endswith("/marketplaces/products")
    assert kwargs["params"]["productType"] == "data"


# ---------------------------------------------------------------------------
# Alerts + watcher management surface (PR 3, the write surface)
# ---------------------------------------------------------------------------


def test_build_tools_registers_watcher_surface():
    handlers = _handlers()
    for name in (
        "list_intel471_alerts",
        "list_intel471_watcher_groups",
        "get_intel471_watcher_group",
        "create_intel471_watcher_group",
        "create_intel471_watcher",
        "manage_intel471_watchers",
    ):
        assert name in handlers, f"missing tool {name}"


def test_watcher_tool_priorities():
    """Reads sort before creates before the destructive consolidated manager."""
    tools = {t.name: t for t in build_tools(_TOKEN, _SafeIntegrationExecutor())}
    assert tools["list_intel471_alerts"].priority == 0
    assert tools["get_intel471_watcher_group"].priority == 0
    assert tools["create_intel471_watcher_group"].priority == 1
    assert tools["create_intel471_watcher"].priority == 1
    assert tools["manage_intel471_watchers"].priority == 2


@pytest.mark.asyncio
async def test_alerts_page_with_uid_offset(monkeypatch):
    holder = _patch(
        monkeypatch,
        {"alerts": [{"uid": "alert-9", "report": {"subject": "Match!"}}]},
    )
    result = await _handlers()["list_intel471_alerts"](
        watcher_group="g1", show_read=False, mark_as_read=True, offset="alert-5"
    )
    assert "Match!" in result
    assert 'offset="alert-9"' in result  # next-page hint uses last alert UID
    params = holder["client"].requests[0][2]["params"]
    assert params["watcherGroup"] == "g1"
    assert params["showRead"] == "false"
    assert params["markAsRead"] == "true"
    assert params["offset"] == "alert-5"


@pytest.mark.asyncio
async def test_create_watcher_group_requires_name():
    result = await _handlers()["create_intel471_watcher_group"](name="")
    assert "'name' is required" in result


@pytest.mark.asyncio
async def test_create_search_watcher_requires_pattern():
    result = await _handlers()["create_intel471_watcher"](
        group_uid="g1", watcher_type="search"
    )
    assert "free_text_pattern" in result


@pytest.mark.asyncio
async def test_create_thread_watcher_requires_thread_uid():
    result = await _handlers()["create_intel471_watcher"](
        group_uid="g1", watcher_type="thread"
    )
    assert "thread_uid" in result


@pytest.mark.asyncio
async def test_create_search_watcher_builds_payload(monkeypatch):
    holder = _patch(monkeypatch, {"uid": "w1"})
    result = await _handlers()["create_intel471_watcher"](
        group_uid="g1",
        watcher_type="search",
        free_text_pattern="acme.com",
        data_sets="malware, credentials",
    )
    assert "w1" in result
    method, url, kwargs = holder["client"].requests[0]
    assert method == "POST"
    assert url.endswith("/watcherGroups/g1/watchers")
    body = kwargs["json"]
    assert body["type"] == "search"
    assert body["freeTextPattern"] == "acme.com"
    assert body["dataSets"] == ["malware", "credentials"]


@pytest.mark.asyncio
async def test_watcher_groups_reject_unknown_section():
    result = await _handlers()["list_intel471_watcher_groups"](section="bogus")
    assert "section must be one of" in result


@pytest.mark.asyncio
async def test_create_watcher_rejects_bad_notification_channel():
    result = await _handlers()["create_intel471_watcher"](
        group_uid="g1", free_text_pattern="acme", notification_channel="sms"
    )
    assert "notification_channel must be one of" in result


@pytest.mark.asyncio
async def test_create_email_watcher_requires_frequency():
    result = await _handlers()["create_intel471_watcher"](
        group_uid="g1", free_text_pattern="acme", notification_channel="email"
    )
    assert "notification_frequency" in result and "required" in result


@pytest.mark.asyncio
async def test_create_email_watcher_rejects_bad_frequency():
    result = await _handlers()["create_intel471_watcher"](
        group_uid="g1",
        free_text_pattern="acme",
        notification_channel="email",
        notification_frequency="hourly",
    )
    assert "notification_frequency must be one of" in result


@pytest.mark.asyncio
async def test_create_website_watcher_omits_frequency(monkeypatch):
    """Frequency is email-only; the website channel must never send it."""
    holder = _patch(monkeypatch, {"uid": "w2"})
    await _handlers()["create_intel471_watcher"](
        group_uid="g1",
        free_text_pattern="acme",
        notification_channel="website",
        notification_frequency="daily",
    )
    body = holder["client"].requests[0][2]["json"]
    assert body["notificationChannel"] == "website"
    assert "notificationFrequency" not in body


@pytest.mark.asyncio
async def test_create_email_watcher_builds_payload(monkeypatch):
    holder = _patch(monkeypatch, {"uid": "w3"})
    await _handlers()["create_intel471_watcher"](
        group_uid="g1",
        free_text_pattern="acme",
        notification_channel="email",
        notification_frequency="daily",
    )
    body = holder["client"].requests[0][2]["json"]
    assert body["notificationChannel"] == "email"
    assert body["notificationFrequency"] == "daily"


@pytest.mark.asyncio
async def test_manage_watchers_rejects_unknown_action():
    result = await _handlers()["manage_intel471_watchers"](action="nope", group_uid="g1")
    assert "unknown action" in result.lower()
    assert "delete_group" in result


@pytest.mark.asyncio
async def test_manage_watchers_delete_group_is_destructive_path(monkeypatch):
    holder = _patch(monkeypatch, {})
    result = await _handlers()["manage_intel471_watchers"](
        action="delete_group", group_uid="g1"
    )
    assert "Deleted watcher group g1" in result
    method, url, _kwargs = holder["client"].requests[0]
    assert method == "DELETE"
    assert url.endswith("/watcherGroups/g1")


@pytest.mark.asyncio
async def test_manage_watchers_delete_watcher_requires_uid():
    result = await _handlers()["manage_intel471_watchers"](
        action="delete_watcher", group_uid="g1", watcher_uid=""
    )
    assert "watcher_uid" in result


@pytest.mark.asyncio
async def test_manage_watchers_rejects_group_uid_traversal():
    result = await _handlers()["manage_intel471_watchers"](
        action="delete_group", group_uid="../g1"
    )
    assert "path separators" in result
