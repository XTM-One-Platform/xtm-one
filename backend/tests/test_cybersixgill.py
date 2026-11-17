"""Tests for the Cybersixgill integration provider.

Covers tool introspection, compound-token parsing (the client secret is the
final greedy segment and may itself contain ``||``), base-URL normalisation,
call-time credential guards, request shapes (DVE enrichment, IOC/actor/post
enrichment, simple + advanced intel search, scroll continuation, actionable
alerts, reports, organization assets), and the MSSP ``X-Channel-Id`` header.
API calls route through the shared ``safe_request`` helper and the token mint
is monkeypatched, so no network is touched.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.integrations.providers import cybersixgill as csg
from app.integrations.providers.cybersixgill import (
    build_tools,
    cybersixgill_api_base,
)
from app.tools.base import CallableToolExecutor, ToolCall
from tests.integration_file_fakes import fake_api

_TOKEN = "https://api.cybersixgill.com||client-id-123||||s3cret-value"
_TOKEN_MSSP = "https://api.cybersixgill.com||client-id-123||org-9||s3cret-value"

_EXPECTED_TOOL_COUNT = 18


class _StubResponse:
    def __init__(self, payload: Any):
        self._payload = payload
        self.text = payload if isinstance(payload, str) else ""

    def json(self) -> Any:
        return self._payload


class _Recorder:
    """Fake ``safe_request`` that records calls and returns queued payloads."""

    def __init__(self, payloads: list[Any]):
        self._payloads = list(payloads)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        payload = self._payloads.pop(0) if self._payloads else {}
        return _StubResponse(payload), None


def _build(
    monkeypatch, payloads: list[Any], token: str = _TOKEN
) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(payloads)
    monkeypatch.setattr(csg, "safe_request", recorder)

    # The provider's _token closure is defined inside build_tools; patch the
    # underlying mint so the closure returns a token without a network call.
    async def _mint(*_a: Any):
        return "bearer-token", 1500

    monkeypatch.setattr(csg, "_request_access_token", _mint)
    monkeypatch.setattr(csg, "get_cached_app_token", lambda _k: None)
    monkeypatch.setattr(csg, "set_cached_app_token", lambda *_a: None)
    executor = CallableToolExecutor()
    build_tools(token=token, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Introspection + token parsing
# ---------------------------------------------------------------------------


def test_build_tools_returns_expected_tools():
    executor = CallableToolExecutor()
    tools = build_tools(token=_TOKEN, executor=executor)
    names = {t.name for t in tools}
    assert len(tools) == _EXPECTED_TOOL_COUNT
    assert "enrich_cybersixgill_cve" in names
    assert "get_cybersixgill_cve_remediation" in names
    assert "enrich_cybersixgill_ioc" in names
    assert "enrich_cybersixgill_actor" in names
    assert "enrich_cybersixgill_post" in names
    assert "search_cybersixgill_intel_items" in names
    assert "search_cybersixgill_intel_items_advanced" in names
    assert "get_cybersixgill_intel_items_next" in names
    assert "list_cybersixgill_alerts" in names
    assert "get_cybersixgill_alert" in names
    assert "get_cybersixgill_alert_content" in names
    assert "update_cybersixgill_alert" in names
    assert "delete_cybersixgill_alert" in names
    assert "list_cybersixgill_reports" in names
    assert "get_cybersixgill_report" in names
    assert "get_cybersixgill_report_html" in names
    assert "get_cybersixgill_organization_assets" in names
    assert "manage_cybersixgill_organization_assets" in names
    assert all(t.source == "integration:cybersixgill" for t in tools)
    # Reads sort before writes before destructive.
    by_name = {t.name: t for t in tools}
    assert by_name["enrich_cybersixgill_cve"].priority == 0
    assert by_name["list_cybersixgill_alerts"].priority == 0
    assert by_name["update_cybersixgill_alert"].priority == 1
    assert by_name["manage_cybersixgill_organization_assets"].priority == 1
    assert by_name["delete_cybersixgill_alert"].priority == 2


def test_build_tools_always_builds_full_surface_even_without_creds():
    """Missing client_id/secret still builds tools; calls guard at runtime."""
    executor = CallableToolExecutor()
    tools = build_tools(token="||||", executor=executor)
    assert len(tools) == _EXPECTED_TOOL_COUNT
    tools2 = build_tools(token="", executor=CallableToolExecutor())
    assert len(tools2) == _EXPECTED_TOOL_COUNT


@pytest.mark.asyncio
async def test_missing_credentials_surface_call_time_error(monkeypatch):
    minted = False

    async def _fail_mint(*_args: Any):
        nonlocal minted
        minted = True
        return None, 0

    monkeypatch.setattr(csg, "_request_access_token", _fail_mint)
    executor = CallableToolExecutor()
    build_tools(token="https://api.cybersixgill.com||||", executor=executor)
    result = await executor.execute_tool(
        ToolCall(id="c1", name="enrich_cybersixgill_cve", arguments={"cve_id": "CVE-2021-44228"})
    )
    assert "credentials are not configured" in str(result.content)
    assert minted is False


@pytest.mark.asyncio
async def test_client_secret_may_contain_delimiter(monkeypatch):
    """The client secret is the final greedy segment - ``||`` must survive."""
    captured: dict[str, Any] = {}

    async def _capture(base_url, client_id, client_secret):
        captured.update(
            base_url=base_url, client_id=client_id, client_secret=client_secret
        )
        return None, 0

    monkeypatch.setattr(csg, "_request_access_token", _capture)
    monkeypatch.setattr(csg, "get_cached_app_token", lambda _k: None)
    executor = CallableToolExecutor()
    build_tools(
        token="https://api.cybersixgill.com||cid||org-7||pa||ss||word",
        executor=executor,
    )
    result = await executor.execute_tool(
        ToolCall(id="c1", name="enrich_cybersixgill_cve", arguments={"cve_id": "CVE-2020-1234"})
    )
    assert captured["client_secret"] == "pa||ss||word"
    assert captured["client_id"] == "cid"
    assert "Failed to authenticate" in str(result.content)


def test_cybersixgill_api_base_defaults_and_normalises():
    assert cybersixgill_api_base("") == "https://api.cybersixgill.com"
    assert cybersixgill_api_base("https://api.cybersixgill.com/") == (
        "https://api.cybersixgill.com"
    )
    assert cybersixgill_api_base("http://api.cybersixgill.com") == (
        "https://api.cybersixgill.com"
    )
    assert cybersixgill_api_base("api.example.com") == "https://api.example.com"


# ---------------------------------------------------------------------------
# Request shapes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_cve_builds_get_and_sends_channel_header(monkeypatch):
    executor, recorder = _build(
        monkeypatch, [{"score": {"value": 8.4, "date": "2026-01-01"}}]
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="enrich_cybersixgill_cve",
            arguments={"cve_id": "cve-2021-44228"},
        )
    )
    call = recorder.calls[0]
    assert call["method"] == "get"
    # CVE id is upper-cased.
    assert call["url"] == "https://api.cybersixgill.com/dve_enrich/CVE-2021-44228"
    assert call["headers"]["X-Channel-Id"] == csg.DEFAULT_CHANNEL_ID
    assert call["headers"]["Authorization"] == "Bearer bearer-token"
    assert "DVE score: 8.4" in str(result.content)
    assert "portal.cybersixgill.com" in str(result.content)


@pytest.mark.asyncio
async def test_enrich_cve_rejects_malformed_cve_id(monkeypatch):
    """The CVE id is interpolated into the URL path - malformed input is rejected."""
    executor, recorder = _build(monkeypatch, [])
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="enrich_cybersixgill_cve",
            arguments={"cve_id": "not-a-cve/../etc"},
        )
    )
    assert "cve_id must be a CVE identifier" in str(result.content)
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_cve_remediation_uses_remediation_path(monkeypatch):
    executor, recorder = _build(
        monkeypatch, [{"Solutions": ["Upgrade to 2.17.0", "Remove JndiLookup"]}]
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="get_cybersixgill_cve_remediation",
            arguments={"cve_id": "CVE-2021-44228"},
        )
    )
    assert recorder.calls[0]["url"].endswith("/dve_enrich/CVE-2021-44228/remediation")
    assert "Upgrade to 2.17.0" in str(result.content)


@pytest.mark.asyncio
async def test_enrich_ioc_rejects_bad_type(monkeypatch):
    executor, recorder = _build(monkeypatch, [])
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="enrich_cybersixgill_ioc",
            arguments={"ioc_type": "bogus", "ioc_value": "x"},
        )
    )
    assert "ioc_type must be one of" in str(result.content)
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_enrich_ioc_posts_body(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        [{"items": [{"pattern": "[ipv4-addr:value = '1.2.3.4']"}], "total": 1}],
    )
    await executor.execute_tool(
        ToolCall(
            id="c1",
            name="enrich_cybersixgill_ioc",
            arguments={"ioc_type": "IP", "ioc_value": "1.2.3.4"},
        )
    )
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/ioc/enrich")
    assert call["json"] == {"ioc_type": "ip", "ioc_value": "1.2.3.4", "skip": 0}


@pytest.mark.asyncio
async def test_enrich_actor_uses_sixgill_field(monkeypatch):
    executor, recorder = _build(monkeypatch, [{"items": [], "total": 0}])
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="enrich_cybersixgill_actor",
            arguments={"actor_name": "pbscybsec"},
        )
    )
    call = recorder.calls[0]
    assert call["json"] == {
        "sixgill_field": "actor",
        "sixgill_field_value": "pbscybsec",
        "skip": 0,
    }
    assert "No Cybersixgill intelligence found for actor 'pbscybsec'" in str(
        result.content
    )


@pytest.mark.asyncio
async def test_enrich_post_uses_post_id_field(monkeypatch):
    executor, recorder = _build(monkeypatch, [{"items": [{"pattern": "x"}], "total": 1}])
    await executor.execute_tool(
        ToolCall(
            id="c1",
            name="enrich_cybersixgill_post",
            arguments={"post_id": "abc123", "skip": 50},
        )
    )
    assert recorder.calls[0]["json"] == {
        "sixgill_field": "post_id",
        "sixgill_field_value": "abc123",
        "skip": 50,
    }


@pytest.mark.asyncio
async def test_simple_search_uses_get_with_query_param(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        [{"intel_items": [{"title": "Leaked creds", "site": "forum", "id": "p1"}]}],
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="search_cybersixgill_intel_items",
            arguments={"query": "acme.com", "results_size": 5},
        )
    )
    call = recorder.calls[0]
    assert call["method"] == "get"
    assert call["url"].endswith("/intel/intel_items")
    assert call["params"]["query"] == "acme.com"
    assert call["params"]["results_size"] == 5
    assert "post_id: p1" in str(result.content)


@pytest.mark.asyncio
async def test_advanced_search_builds_date_range_and_scroll(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        [
            {
                "intel_items": [{"title": "Hit", "site": "market", "id": "p2"}],
                "scroll_id": "scroll-xyz",
            }
        ],
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="search_cybersixgill_intel_items_advanced",
            arguments={
                "query": "ransomware",
                "date_range_start": "2026-01-01T00:00:00",
                "date_range_end": "2026-02-01T00:00:00",
                "sort": "date",
                "sort_type": "asc",
            },
        )
    )
    call = recorder.calls[0]
    assert call["method"] == "post"
    body = call["json"]
    assert body["query"] == "ransomware"
    assert body["scroll"] is True
    assert body["date_range"] == {
        "start": "2026-01-01T00:00:00",
        "end": "2026-02-01T00:00:00",
    }
    assert body["sort"] == "date" and body["sort_type"] == "asc"
    assert "scroll-xyz" in str(result.content)


@pytest.mark.asyncio
async def test_intel_items_next_requires_scroll_id(monkeypatch):
    executor, recorder = _build(monkeypatch, [])
    result = await executor.execute_tool(
        ToolCall(
            id="c1", name="get_cybersixgill_intel_items_next", arguments={"scroll_id": ""}
        )
    )
    assert "scroll_id is required" in str(result.content)
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_intel_items_next_posts_scroll_id(monkeypatch):
    executor, recorder = _build(
        monkeypatch, [{"intel_items": [{"title": "Next", "id": "p3"}]}]
    )
    await executor.execute_tool(
        ToolCall(
            id="c1",
            name="get_cybersixgill_intel_items_next",
            arguments={"scroll_id": "scroll-xyz"},
        )
    )
    call = recorder.calls[0]
    assert call["url"].endswith("/intel/intel_items/next")
    assert call["json"] == {"scroll_id": "scroll-xyz"}


# ---------------------------------------------------------------------------
# Actionable alerts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_alerts_uses_fetch_size_and_filters(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        [[{"id": "a1", "title": "Leak", "threat_level": "imminent"}]],
    )
    await executor.execute_tool(
        ToolCall(
            id="c1",
            name="list_cybersixgill_alerts",
            arguments={
                "limit": 1000,
                "threat_level": "imminent",
                "is_read": "unread",
            },
        )
    )
    call = recorder.calls[0]
    assert call["method"] == "get"
    assert call["url"].endswith("/alerts/actionable-alert")
    assert call["params"]["fetch_size"] == 200  # clamped
    assert call["params"]["threat_level"] == "imminent"
    assert call["params"]["is_read"] == "unread"


@pytest.mark.asyncio
async def test_list_alerts_rejects_bad_threat_level(monkeypatch):
    executor, recorder = _build(monkeypatch, [])
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="list_cybersixgill_alerts",
            arguments={"threat_level": "critical"},
        )
    )
    assert "threat_level must be one of" in str(result.content)
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_list_alerts_injects_mssp_org_id(monkeypatch):
    executor, recorder = _build(monkeypatch, [[]], token=_TOKEN_MSSP)
    await executor.execute_tool(
        ToolCall(id="c1", name="list_cybersixgill_alerts", arguments={})
    )
    assert recorder.calls[0]["params"]["organization_id"] == "org-9"


@pytest.mark.asyncio
async def test_update_alert_builds_status_body_and_consumer(monkeypatch):
    executor, recorder = _build(monkeypatch, [{}])
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="update_cybersixgill_alert",
            arguments={"alert_id": "a1", "status": "resolved", "read": "read"},
        )
    )
    call = recorder.calls[0]
    assert call["method"] == "patch"
    assert call["url"].endswith("/alerts/actionable_alert/a1")
    assert call["params"]["consumer"] == csg.DEFAULT_CHANNEL_ID
    assert call["json"]["status"] == {"status": "resolved"}
    assert call["json"]["read"] is True
    assert "Updated actionable alert a1" in str(result.content)


@pytest.mark.asyncio
async def test_update_alert_requires_a_field(monkeypatch):
    executor, recorder = _build(monkeypatch, [])
    result = await executor.execute_tool(
        ToolCall(
            id="c1", name="update_cybersixgill_alert", arguments={"alert_id": "a1"}
        )
    )
    assert "provide at least one field" in str(result.content)
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_delete_alert_sends_id_list(monkeypatch):
    executor, recorder = _build(monkeypatch, [{}])
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="delete_cybersixgill_alert",
            arguments={"alert_ids": "a1, a2 ,a3"},
        )
    )
    call = recorder.calls[0]
    assert call["method"] == "delete"
    assert call["url"].endswith("/alerts/actionable-alert")
    assert call["json"] == ["a1", "a2", "a3"]
    assert "Permanently deleted 3 actionable alerts" in str(result.content)


@pytest.mark.asyncio
async def test_get_alert_formats_detail_without_echoed_id(monkeypatch):
    """A detail payload that omits id/alert_id is still a found alert."""
    executor, recorder = _build(
        monkeypatch,
        [{"title": "Leaked creds", "threat_level": "imminent", "status": {"name": "in_treatment"}}],
        token=_TOKEN_MSSP,
    )
    result = await executor.execute_tool(
        ToolCall(id="c1", name="get_cybersixgill_alert", arguments={"alert_id": "a1"})
    )
    assert recorder.calls[0]["url"].endswith("/alerts/actionable_alert/a1")
    content = str(result.content)
    assert "Actionable alert #a1" in content
    assert "Leaked creds" in content
    # Portal deep link falls back to the requested id + MSSP org.
    assert "actionable_alert=a1" in content
    assert "org=org-9" in content


@pytest.mark.asyncio
async def test_update_alert_tolerates_garbage_aggregate_alert_id(monkeypatch):
    """A non-numeric aggregate_alert_id is ignored, not a crash."""
    executor, recorder = _build(monkeypatch, [{}])
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="update_cybersixgill_alert",
            arguments={
                "alert_id": "a1",
                "read": "read",
                "aggregate_alert_id": "not-a-number",
            },
        )
    )
    call = recorder.calls[0]
    assert "sub_alert_indexes" not in call["params"]
    assert "Updated actionable alert a1" in str(result.content)


@pytest.mark.asyncio
async def test_alert_content_unwraps_items(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        [
            {
                "content": {
                    "items": [
                        {"_source": {"title": "Post", "creator": "actor", "site": "forum"}}
                    ]
                }
            }
        ],
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="get_cybersixgill_alert_content",
            arguments={"alert_id": "a1"},
        )
    )
    assert recorder.calls[0]["url"].endswith("/alerts/actionable_alert_content/a1")
    assert "Post" in str(result.content)


# ---------------------------------------------------------------------------
# Reports + organization assets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_reports_posts_body(monkeypatch):
    executor, recorder = _build(
        monkeypatch, [{"reports": [{"id": "r1", "title": "APT report"}]}]
    )
    await executor.execute_tool(
        ToolCall(
            id="c1", name="list_cybersixgill_reports", arguments={"limit": 5}
        )
    )
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/reports/reports")
    assert call["json"]["fetch_size"] == 5


@pytest.mark.asyncio
async def test_report_tools_inject_mssp_org_id(monkeypatch):
    """All three report tools scope to the MSSP organization when configured."""
    executor, recorder = _build(
        monkeypatch,
        [{"reports": []}, {"id": "r1", "title": "T"}],
        token=_TOKEN_MSSP,
    )
    await executor.execute_tool(
        ToolCall(id="c1", name="list_cybersixgill_reports", arguments={})
    )
    await executor.execute_tool(
        ToolCall(
            id="c2", name="get_cybersixgill_report", arguments={"report_id": "r1"}
        )
    )
    # The rendered report is streamed as a file (fetch_file), not a JSON call.
    with fake_api(lambda _r: httpx.Response(200, content=b"<html>report</html>")) as sent:
        await executor.execute_tool(
            ToolCall(
                id="c3",
                name="get_cybersixgill_report_html",
                arguments={"report_id": "r1"},
            )
        )
    assert len(recorder.calls) == 2
    for call in recorder.calls:
        assert call["params"]["organization_id"] == "org-9"
    (html_call,) = sent
    assert html_call.url.params["organization_id"] == "org-9"
    assert html_call.url.params["html_format"] == "html"
    assert html_call.headers["x-channel-id"] == csg.DEFAULT_CHANNEL_ID


def test_manage_org_assets_schema_accepts_object_or_string():
    """The declared schema matches the accepted inputs (dict or JSON string)."""
    executor = CallableToolExecutor()
    tools = build_tools(token=_TOKEN, executor=executor)
    schema = next(
        t for t in tools if t.name == "manage_cybersixgill_organization_assets"
    ).input_schema
    assert schema["properties"]["assets"]["type"] == ["object", "string"]


@pytest.mark.asyncio
async def test_manage_org_assets_add_posts_payload(monkeypatch):
    executor, recorder = _build(monkeypatch, [{}])
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="manage_cybersixgill_organization_assets",
            arguments={
                "action": "add",
                "assets": {"domain_names": ["acme.com"]},
            },
        )
    )
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/assets/organization")
    assert call["json"] == {"domain_names": ["acme.com"]}
    assert "Added to" in str(result.content)


@pytest.mark.asyncio
async def test_manage_org_assets_replace_uses_put_and_parses_json_string(monkeypatch):
    executor, recorder = _build(monkeypatch, [{}])
    await executor.execute_tool(
        ToolCall(
            id="c1",
            name="manage_cybersixgill_organization_assets",
            arguments={
                "action": "replace",
                "assets": '{"ip_addresses": ["1.2.3.4"]}',
            },
        )
    )
    call = recorder.calls[0]
    assert call["method"] == "put"
    assert call["json"] == {"ip_addresses": ["1.2.3.4"]}


@pytest.mark.asyncio
async def test_manage_org_assets_rejects_bad_action(monkeypatch):
    executor, recorder = _build(monkeypatch, [])
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="manage_cybersixgill_organization_assets",
            arguments={"action": "delete", "assets": {"x": 1}},
        )
    )
    assert "action must be one of" in str(result.content)
    assert recorder.calls == []
