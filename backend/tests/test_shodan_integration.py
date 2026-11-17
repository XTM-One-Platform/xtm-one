"""Tests for the Shodan provider (search engine for Internet-connected devices).

Covers: token/key handling and the missing-credential short-circuit,
credential validation, the ``key`` query-parameter injection, request-shape
for read + write + destructive tools, and the consolidated ``manage_*`` action
routers.
"""

from __future__ import annotations

import pytest

from app.integrations.providers import shodan
from app.integrations.providers.shodan import ShodanIntegration, build_tools
from app.services.integration_tools import _SafeIntegrationExecutor

_TOKEN = "SHODAN_TEST_KEY"


class _FakeResp:
    def __init__(self, payload, status_code: int = 200, *, raw_text: str | None = None):
        self.status_code = status_code
        self._payload = payload
        if raw_text is not None:
            self.text = raw_text
        else:
            self.text = "" if isinstance(payload, (dict, list)) else str(payload)
        self._raw_text = raw_text

    def json(self):
        if self._raw_text is not None:
            raise ValueError("not JSON")
        return self._payload


def _handlers(token: str = _TOKEN) -> dict:
    executor = _SafeIntegrationExecutor()
    build_tools(token, executor)
    return executor._handlers


def _patch(
    monkeypatch, payload, status_code: int = 200, *, raw_text: str | None = None
) -> dict:
    """Patch shodan.safe_request to capture the call and return a fake response."""
    captured: dict = {}

    async def _fake_safe_request(client, method, url, provider, action, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["action"] = action
        captured.update(kwargs)
        return _FakeResp(payload, status_code, raw_text=raw_text), None

    monkeypatch.setattr(shodan, "safe_request", _fake_safe_request)
    return captured


# ---------------------------------------------------------------------------
# Tool surface + registration
# ---------------------------------------------------------------------------


def test_build_tools_registers_full_surface():
    executor = _SafeIntegrationExecutor()
    tools = build_tools(_TOKEN, executor)
    names = {t.name for t in tools}

    # A representative slice across every domain must be present.
    for expected in {
        "get_shodan_host_info",
        "search_shodan_hosts",
        "count_shodan_hosts",
        "list_shodan_ports",
        "request_shodan_scan",
        "create_shodan_alert",
        "delete_shodan_alert",
        "manage_shodan_alert_trigger",
        "manage_shodan_notifier",
        "resolve_shodan_dns",
        "get_shodan_domain_info",
        "get_shodan_api_info",
        "manage_shodan_org_member",
    }:
        assert expected in names

    # Every registered handler has a ToolDefinition and vice versa.
    assert names == set(executor._handlers.keys())
    # All tools are tagged with the integration source.
    assert all(t.source == "integration:shodan" for t in tools)


def test_priorities_sort_reads_before_writes_before_destructive():
    tools = {t.name: t for t in build_tools(_TOKEN, _SafeIntegrationExecutor())}
    assert tools["search_shodan_hosts"].priority == 0
    assert tools["request_shodan_scan"].priority == 1
    assert tools["create_shodan_alert"].priority == 1
    assert tools["delete_shodan_alert"].priority == 2


# ---------------------------------------------------------------------------
# Missing-credential short-circuit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_key_short_circuits(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run without an API key")

    monkeypatch.setattr(shodan, "safe_request", _must_not_call)
    handler = _handlers(token="")["search_shodan_hosts"]
    result = await handler(query="port:22")
    assert "not configured" in result.lower()


# ---------------------------------------------------------------------------
# key query-parameter injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_key_is_injected_as_query_param(monkeypatch):
    captured = _patch(monkeypatch, {"total": 0, "matches": []})
    handler = _handlers()["search_shodan_hosts"]
    await handler(query="product:nginx", facets="country", page=2)

    assert captured["method"] == "get"
    assert captured["url"].endswith("/shodan/host/search")
    params = captured["params"]
    assert params["key"] == _TOKEN
    assert params["query"] == "product:nginx"
    assert params["facets"] == "country"
    assert params["page"] == 2


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_host_info_summarises_ports_and_vulns(monkeypatch):
    _patch(
        monkeypatch,
        {
            "ip_str": "8.8.8.8",
            "org": "Google",
            "country_name": "United States",
            "ports": [53, 443],
            "hostnames": ["dns.google"],
            "vulns": {"CVE-2021-1234": {}},
            "data": [
                {"port": 443, "transport": "tcp", "product": "nginx", "data": "HTTP/1.1 200"}
            ],
        },
    )
    handler = _handlers()["get_shodan_host_info"]
    result = await handler(ip="8.8.8.8")
    assert "8.8.8.8" in result
    assert "53, 443" in result
    assert "CVE-2021-1234" in result
    assert "nginx" in result


@pytest.mark.asyncio
async def test_host_info_encodes_ipv6_path_segment(monkeypatch):
    captured = _patch(monkeypatch, {"ip_str": "2001:db8::1", "ports": []})
    handler = _handlers()["get_shodan_host_info"]
    await handler(ip="2001:db8::1")
    # Colons in an IPv6 address must be percent-encoded so the path stays valid
    # and unambiguous (%3A), never interpolated raw.
    assert captured["url"].endswith("/shodan/host/2001%3Adb8%3A%3A1")
    assert ":" not in captured["url"].rsplit("/shodan/host/", 1)[1]


@pytest.mark.asyncio
async def test_search_hosts_flags_intra_page_truncation(monkeypatch):
    # Shodan returns up to 100 matches per page; the display is capped at
    # _MAX_ITEMS. When a page has more matches than are shown, the extra ones
    # are unreachable by paging, so the output must flag the truncation.
    matches = [{"ip_str": f"10.0.0.{i}", "port": 80} for i in range(80)]
    _patch(monkeypatch, {"total": 80, "matches": matches})
    handler = _handlers()["search_shodan_hosts"]
    result = await handler(query="port:80")
    assert f"showing {shodan._MAX_ITEMS}" in result
    assert "this page returned 80 matches" in result
    assert f"only the first {shodan._MAX_ITEMS} are shown" in result
    # total == matches shown on the (single) page, so no "more results" hint.
    assert "More results available" not in result


@pytest.mark.asyncio
async def test_search_hosts_no_truncation_note_when_page_fits(monkeypatch):
    matches = [{"ip_str": f"10.0.0.{i}", "port": 80} for i in range(3)]
    _patch(monkeypatch, {"total": 3, "matches": matches})
    handler = _handlers()["search_shodan_hosts"]
    result = await handler(query="port:80")
    assert "this page returned" not in result


@pytest.mark.asyncio
async def test_search_hosts_normalizes_page_in_request_and_output(monkeypatch):
    # page=0 must be clamped to 1 both in the API request params AND in the
    # user-facing output, so the two never disagree (the API always serves
    # page 1 for a non-positive page).
    captured = _patch(monkeypatch, {"total": 1, "matches": [{"ip_str": "10.0.0.1", "port": 80}]})
    handler = _handlers()["search_shodan_hosts"]
    result = await handler(query="port:80", page=0)
    assert captured["params"]["page"] == 1
    assert "page 1" in result
    assert "page 0" not in result


@pytest.mark.asyncio
async def test_list_scans_normalizes_page_in_output(monkeypatch):
    captured = _patch(monkeypatch, {"total": 1, "matches": [{"id": "s1", "status": "DONE", "size": 1, "created": "now"}]})
    handler = _handlers()["list_shodan_scans"]
    result = await handler(page=-3)
    assert captured["params"]["page"] == 1
    assert "page 1" in result
    assert "page -3" not in result


@pytest.mark.asyncio
async def test_domain_info_normalizes_page_in_more_hint(monkeypatch):
    captured = _patch(
        monkeypatch,
        {"domain": "cnn.com", "subdomains": [], "data": [], "more": True},
    )
    handler = _handlers()["get_shodan_domain_info"]
    result = await handler(domain="cnn.com", page=0)
    assert captured["params"]["page"] == 1
    # The "more" hint must advance from the normalized page (1 -> 2), not 0 -> 1.
    assert "page=2" in result


@pytest.mark.asyncio
async def test_non_json_response_surfaces_error_for_json_endpoint(monkeypatch):
    # A 200 with a non-JSON body must become a formatted error, not a raw
    # string that crashes the caller's .get(...) with an AttributeError.
    _patch(monkeypatch, None, raw_text="<html>maintenance page</html>")
    handler = _handlers()["get_shodan_api_info"]
    result = await handler()
    assert result.startswith("Error:")
    assert "non-JSON" in result


@pytest.mark.asyncio
async def test_my_ip_tolerates_plain_text_response(monkeypatch):
    # /tools/myip is the one endpoint allowed to answer with a bare text body.
    _patch(monkeypatch, None, raw_text="198.51.100.7")
    handler = _handlers()["get_shodan_my_ip"]
    result = await handler()
    assert result == "External IP: 198.51.100.7"


@pytest.mark.asyncio
async def test_count_hosts_formats_total(monkeypatch):
    _patch(monkeypatch, {"total": 19590274, "facets": {}})
    handler = _handlers()["count_shodan_hosts"]
    result = await handler(query="port:22")
    assert "19,590,274" in result


@pytest.mark.asyncio
async def test_resolve_dns_formats_map(monkeypatch):
    _patch(monkeypatch, {"google.com": "172.217.6.46"})
    handler = _handlers()["resolve_shodan_dns"]
    result = await handler(hostnames="google.com")
    assert "google.com" in result
    assert "172.217.6.46" in result


# ---------------------------------------------------------------------------
# Write / destructive tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_scan_posts_form_body(monkeypatch):
    captured = _patch(monkeypatch, {"count": 2, "id": "scanid", "credits_left": 99})
    handler = _handlers()["request_shodan_scan"]
    result = await handler(ips="8.8.8.8,1.1.1.1")
    assert captured["method"] == "post"
    assert captured["url"].endswith("/shodan/scan")
    assert captured["data"] == {"ips": "8.8.8.8,1.1.1.1"}
    assert "scanid" in result


@pytest.mark.asyncio
async def test_request_scan_requires_ips(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("must validate before requesting")

    monkeypatch.setattr(shodan, "safe_request", _must_not_call)
    handler = _handlers()["request_shodan_scan"]
    result = await handler(ips="  ")
    assert "at least one" in result.lower()


@pytest.mark.asyncio
async def test_create_alert_sends_json_body(monkeypatch):
    captured = _patch(monkeypatch, {"id": "AID", "name": "DNS", "size": 2})
    handler = _handlers()["create_shodan_alert"]
    result = await handler(name="DNS", ips="8.8.8.8, 1.1.1.1", expires=0)
    body = captured["json"]
    assert body["name"] == "DNS"
    assert body["filters"]["ip"] == ["8.8.8.8", "1.1.1.1"]
    assert "AID" in result


@pytest.mark.asyncio
async def test_delete_alert(monkeypatch):
    captured = _patch(monkeypatch, {"success": True})
    handler = _handlers()["delete_shodan_alert"]
    result = await handler(alert_id="AID")
    assert captured["method"] == "delete"
    assert captured["url"].endswith("/shodan/alert/AID")
    assert "Deleted alert AID" in result


# ---------------------------------------------------------------------------
# Consolidated action routers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_alert_trigger_enable(monkeypatch):
    captured = _patch(monkeypatch, {"success": True})
    handler = _handlers()["manage_shodan_alert_trigger"]
    result = await handler(alert_id="AID", action="enable", trigger="malware")
    assert captured["method"] == "put"
    assert captured["url"].endswith("/shodan/alert/AID/trigger/malware")
    assert "Enabled trigger" in result


@pytest.mark.asyncio
async def test_manage_alert_trigger_ignore_requires_service():
    handler = _handlers()["manage_shodan_alert_trigger"]
    result = await handler(alert_id="AID", action="ignore", trigger="new_service")
    assert "service" in result.lower()


@pytest.mark.asyncio
async def test_manage_alert_trigger_unknown_action():
    handler = _handlers()["manage_shodan_alert_trigger"]
    result = await handler(alert_id="AID", action="frobnicate", trigger="x")
    assert "unknown action" in result.lower()


@pytest.mark.asyncio
async def test_manage_alert_trigger_requires_trigger_for_enable(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("enable without a trigger must not hit the API")

    monkeypatch.setattr(shodan, "safe_request", _must_not_call)
    handler = _handlers()["manage_shodan_alert_trigger"]
    result = await handler(alert_id="AID", action="enable")
    assert "'trigger' is required" in result


@pytest.mark.asyncio
async def test_manage_alert_trigger_add_notifier_without_trigger(monkeypatch):
    # trigger is irrelevant for notifier actions and must not be required.
    captured = _patch(monkeypatch, {"success": True})
    handler = _handlers()["manage_shodan_alert_trigger"]
    result = await handler(alert_id="AID", action="add_notifier", notifier_id="NID")
    assert captured["method"] == "put"
    assert captured["url"].endswith("/shodan/alert/AID/notifier/NID")
    assert "NID" in result


@pytest.mark.asyncio
async def test_manage_notifier_create_parses_json_args(monkeypatch):
    captured = _patch(monkeypatch, {"id": "NID", "success": True})
    handler = _handlers()["manage_shodan_notifier"]
    result = await handler(
        action="create", provider="email", args='{"to": "me@example.com"}'
    )
    assert captured["method"] == "post"
    assert captured["data"]["provider"] == "email"
    assert captured["data"]["to"] == "me@example.com"
    assert "NID" in result


@pytest.mark.asyncio
async def test_manage_notifier_rejects_non_object_json_args(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run for invalid args")

    monkeypatch.setattr(shodan, "safe_request", _must_not_call)
    handler = _handlers()["manage_shodan_notifier"]
    # A valid JSON string that decodes to a list (not an object) must be
    # rejected before the create handler spreads it with **parsed_args.
    result = await handler(action="create", provider="email", args="[1, 2, 3]")
    assert "must be a JSON object" in result


@pytest.mark.asyncio
async def test_manage_notifier_delete_requires_id():
    handler = _handlers()["manage_shodan_notifier"]
    result = await handler(action="delete")
    assert "notifier_id" in result


@pytest.mark.asyncio
async def test_manage_notifier_update_requires_args(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("update with no args must not hit the API")

    monkeypatch.setattr(shodan, "safe_request", _must_not_call)
    handler = _handlers()["manage_shodan_notifier"]
    # An update with a notifier_id but no args would PUT an empty body (a
    # confusing no-op); it must be rejected before any request is made.
    result = await handler(action="update", notifier_id="NID")
    assert "args" in result.lower()


@pytest.mark.asyncio
async def test_manage_org_member_add(monkeypatch):
    captured = _patch(monkeypatch, {"success": True})
    handler = _handlers()["manage_shodan_org_member"]
    result = await handler(action="add", user="new@example.com", notify=True)
    assert captured["method"] == "put"
    # The email is percent-encoded into the path segment (@ -> %40) so reserved
    # characters cannot alter the request path.
    assert captured["url"].endswith("/org/member/new%40example.com")
    assert captured["params"]["notify"] == "true"
    assert "Added" in result


# ---------------------------------------------------------------------------
# validate_connection / get_user_info
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_connection_true_on_success(monkeypatch):
    async def _ok(client, method, url, provider, action, **kwargs):
        return _FakeResp({"plan": "dev"}), None

    monkeypatch.setattr(shodan, "safe_request", _ok)
    assert await ShodanIntegration().validate_connection(_TOKEN) is True


@pytest.mark.asyncio
async def test_validate_connection_false_on_error(monkeypatch):
    async def _err(client, method, url, provider, action, **kwargs):
        return None, "Error: Shodan authentication failed"

    monkeypatch.setattr(shodan, "safe_request", _err)
    assert await ShodanIntegration().validate_connection(_TOKEN) is False


@pytest.mark.asyncio
async def test_validate_connection_false_on_blank_token():
    assert await ShodanIntegration().validate_connection("  ") is False
