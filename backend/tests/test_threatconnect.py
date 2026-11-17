"""Tests for the ThreatConnect integration provider.

Covers tool introspection, the HMAC-SHA256 signing helper + API base URL
normalisation, the signed-request query encoding, indicator list/get/create
shaping, tag-mode guards, case creation guards, associations, the consolidated
manage tools, and the missing-secret call-time guard. Every API call routes
through the shared ``safe_request`` helper, which is monkeypatched here so no
network is touched.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any

import pytest

from app.integrations.providers import threatconnect as tc
from app.integrations.providers.threatconnect import (
    build_tc_headers,
    build_tools,
    threatconnect_api_base,
)
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://demo.threatconnect.com||access-123||secret-456"


class _StubResponse:
    def __init__(
        self, payload: Any, status_code: int = 200, content: bytes = b"{}"
    ):
        self._payload = payload
        self.status_code = status_code
        self.content = content

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
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
    monkeypatch.setattr(tc, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Introspection + helpers
# ---------------------------------------------------------------------------


def test_build_tools_returns_definitions_with_dummy_token():
    # The catalog introspection dummy is the 2-part URL compound — the secret
    # key is runtime-only, so introspection must still yield the full tool set.
    executor = CallableToolExecutor()
    tools = build_tools(
        token="https://dummy.example.com||__dummy__", executor=executor
    )
    names = {t.name for t in tools}
    assert "list_threatconnect_owners" in names
    assert "query_threatconnect" in names
    assert "list_threatconnect_indicators" in names
    assert "create_threatconnect_indicator" in names
    assert "delete_threatconnect_indicator" in names
    assert "list_threatconnect_groups" in names
    assert "list_threatconnect_cases" in names
    assert "associate_threatconnect_objects" in names
    assert "manage_threatconnect_workflow" in names
    assert "manage_threatconnect_playbooks" in names
    assert len(tools) == 27
    # Priorities follow the repo convention (read=0, write=1, destructive=2)
    # so truncation keeps reads first — values above 2 would fall outside the
    # documented ToolDefinition range and get dropped before other providers.
    priorities = {t.name: t.priority for t in tools}
    assert set(priorities.values()) <= {0, 1, 2}
    # EVERY read-only tool must be 0 (essential) — a spot check missed three
    # list tools left at 1 in a previous pass, so assert the full read set.
    for name, priority in priorities.items():
        if name.startswith(("list_", "get_", "query_")):
            assert priority == 0, f"read tool {name} must be priority 0"
        elif name.startswith("delete_"):
            assert priority == 2, f"destructive tool {name} must be priority 2"
        else:
            assert priority == 1, f"write tool {name} must be priority 1"


def test_build_tools_empty_without_base_url_or_access_id():
    executor = CallableToolExecutor()
    assert build_tools(token="", executor=executor) == []
    assert build_tools(token="https://demo.threatconnect.com||", executor=executor) == []
    assert build_tools(token="||access-123||secret", executor=executor) == []


def test_api_base_normalisation():
    assert (
        threatconnect_api_base("https://demo.threatconnect.com")
        == "https://demo.threatconnect.com/api"
    )
    # A pasted URL already carrying /api must not double the suffix.
    assert (
        threatconnect_api_base("https://demo.threatconnect.com/api/")
        == "https://demo.threatconnect.com/api"
    )
    assert (
        threatconnect_api_base("  https://demo.threatconnect.com/  ")
        == "https://demo.threatconnect.com/api"
    )
    assert threatconnect_api_base("") == ""


def test_hmac_signature_matches_reference_implementation():
    headers = build_tc_headers(
        "access-123",
        "secret-456",
        "get",
        "/api/v3/indicators?resultLimit=25",
        timestamp=1513703787,
    )
    message = "/api/v3/indicators?resultLimit=25:GET:1513703787"
    expected = base64.b64encode(
        hmac.new(b"secret-456", message.encode(), hashlib.sha256).digest()
    ).decode()
    assert headers["Authorization"] == f"TC access-123:{expected}"
    assert headers["Timestamp"] == "1513703787"
    assert headers["Accept"] == "application/json"
    # Credentials are stripped so pasted whitespace never corrupts the HMAC.
    padded = build_tc_headers(
        "  access-123  ",
        "\tsecret-456\n",
        "GET",
        "/api/v3/indicators?resultLimit=25",
        timestamp=1513703787,
    )
    assert padded["Authorization"] == headers["Authorization"]


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_indicators_signs_and_filters(monkeypatch):
    def router(method, url):
        return {
            "data": [
                {
                    "id": 4,
                    "type": "Host",
                    "summary": "ultrabadguy.com",
                    "rating": 5.0,
                    "confidence": 85,
                    "ownerName": "Demo Organization",
                    "lastModified": "2024-01-01T00:00:00Z",
                    "active": True,
                }
            ],
            "status": "Success",
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="list_threatconnect_indicators",
            arguments={"indicator_type": "Host", "active_only": True},
        )
    )
    assert not result.is_error
    assert "ultrabadguy.com" in result.content
    assert "Rating: 5.0" in result.content
    call = recorder.calls[0]
    # The query string is pre-encoded into the URL (the signature covers it).
    assert "/api/v3/indicators?" in call["url"]
    assert "resultLimit=25" in call["url"]
    assert "typeName%20in%20%28%22Host%22%29" in call["url"]
    assert "indicatorActive%20%3D%20true" in call["url"]
    # Signed headers ride on every request.
    assert call["headers"]["Authorization"].startswith("TC access-123:")
    assert call["headers"]["Timestamp"].isdigit()


@pytest.mark.asyncio
async def test_get_indicator_detail_includes_tags_and_associations(monkeypatch):
    def router(method, url):
        return {
            "data": {
                "id": 4,
                "type": "Host",
                "summary": "ultrabadguy.com",
                "rating": 5.0,
                "confidence": 85,
                "ownerName": "Demo Organization",
                "active": True,
                "falsePositiveFlag": False,
                "tags": {"data": [{"name": "Targeted Attack"}]},
                "securityLabels": {"data": [{"name": "TLP:AMBER"}]},
                "associatedGroups": {
                    "data": [{"id": 12, "type": "Adversary", "name": "Bad Guy"}]
                },
                "webLink": "https://demo.threatconnect.com/#/details/indicators/4/overview",
            },
            "status": "Success",
        }

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c2",
            name="get_threatconnect_indicator",
            arguments={"indicator": "ultrabadguy.com"},
        )
    )
    assert not result.is_error
    assert "Targeted Attack" in result.content
    assert "TLP:AMBER" in result.content
    assert "Bad Guy" in result.content
    assert "webLink" not in result.content  # rendered as a Link line, not raw JSON
    assert "Link: https://demo.threatconnect.com" in result.content


@pytest.mark.asyncio
async def test_create_file_indicator_detects_hash_field(monkeypatch):
    sha256 = "a" * 64
    executor, recorder = _build(
        monkeypatch,
        lambda m, u: {"data": {"id": 9, "type": "File", "summary": sha256}},
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c3",
            name="create_threatconnect_indicator",
            arguments={"indicator_type": "File", "value": sha256},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["json"]["sha256"] == sha256
    # A non-hash value is rejected before any request is made.
    bad = await executor.execute_tool(
        ToolCall(
            id="c4",
            name="create_threatconnect_indicator",
            arguments={"indicator_type": "File", "value": "not-a-hash"},
        )
    )
    assert "MD5" in bad.content
    # A hash-length value with non-hex characters is rejected too.
    non_hex = await executor.execute_tool(
        ToolCall(
            id="c4b",
            name="create_threatconnect_indicator",
            arguments={"indicator_type": "File", "value": "g" * 64},
        )
    )
    assert "MD5" in non_hex.content
    assert len(recorder.calls) == 1  # only the valid SHA256 call went out


@pytest.mark.asyncio
async def test_create_indicator_maps_type_specific_value_fields(monkeypatch):
    # Single-field non-core types map ``value`` onto the documented v3 field.
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"data": {"id": 1}, "status": "Success"}
    )
    for itype, field, value in (
        ("ASN", "AS Number", "ASN204288"),
        ("CIDR", "Block", "10.10.1.0/24"),
        ("Mutex", "Mutex", "\\Sessions\\1\\WrapMutex"),
        ("User Agent", "User Agent String", "PeachWebKit/587.38"),
    ):
        result = await executor.execute_tool(
            ToolCall(
                id=f"tf-{itype}",
                name="create_threatconnect_indicator",
                arguments={"indicator_type": itype, "value": value},
            )
        )
        assert not result.is_error
        assert recorder.calls[-1]["json"][field] == value
    # Registry Key is multi-field — without extra_fields the tool must refuse
    # before any API call instead of silently dropping the value.
    before = len(recorder.calls)
    reg = await executor.execute_tool(
        ToolCall(
            id="tf-reg",
            name="create_threatconnect_indicator",
            arguments={"indicator_type": "Registry Key", "value": "HKLM\\x"},
        )
    )
    assert "extra_fields" in reg.content
    assert len(recorder.calls) == before


@pytest.mark.asyncio
async def test_create_registry_key_indicator_without_value(monkeypatch):
    # Registry Key takes extra_fields only — ``value`` is not schema-required,
    # so a caller can omit it entirely instead of sending a dummy value.
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"data": {"id": 3}, "status": "Success"}
    )
    tools = build_tools(token=_TOKEN, executor=CallableToolExecutor())
    create = next(t for t in tools if t.name == "create_threatconnect_indicator")
    assert create.input_schema["required"] == ["indicator_type"]
    result = await executor.execute_tool(
        ToolCall(
            id="tf-reg2",
            name="create_threatconnect_indicator",
            arguments={
                "indicator_type": "Registry Key",
                "extra_fields": (
                    '{"Key Name": "HKEY_LOCAL_MACHINE\\\\Software\\\\Run", '
                    '"Value Name": "Updater", "Value Type": "REG_SZ"}'
                ),
            },
        )
    )
    assert not result.is_error
    body = recorder.calls[-1]["json"]
    assert body["Key Name"] == "HKEY_LOCAL_MACHINE\\Software\\Run"
    assert body["Value Name"] == "Updater"
    assert body["Value Type"] == "REG_SZ"
    # The single-field runtime guard still rejects a missing value.
    missing = await executor.execute_tool(
        ToolCall(
            id="tf-reg3",
            name="create_threatconnect_indicator",
            arguments={"indicator_type": "Host"},
        )
    )
    assert "value is required" in missing.content


@pytest.mark.asyncio
async def test_create_indicator_rejects_unknown_type(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c5",
            name="create_threatconnect_indicator",
            arguments={"indicator_type": "Nonsense", "value": "x"},
        )
    )
    assert "invalid indicator_type" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_update_indicator_rejects_mixed_tag_modes(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c6",
            name="update_threatconnect_indicator",
            arguments={
                "indicator_id": "4",
                "add_tags": "one",
                "remove_tags": "two",
            },
        )
    )
    assert "not both" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_update_indicator_appends_tags(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"status": "Success"})
    result = await executor.execute_tool(
        ToolCall(
            id="c7",
            name="update_threatconnect_indicator",
            arguments={"indicator_id": "4", "add_tags": "apt, phishing"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "put"
    assert call["json"]["tags"]["mode"] == "append"
    assert [t["name"] for t in call["json"]["tags"]["data"]] == ["apt", "phishing"]


@pytest.mark.asyncio
async def test_delete_indicator(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"status": "Success"})
    result = await executor.execute_tool(
        ToolCall(
            id="c8",
            name="delete_threatconnect_indicator",
            arguments={"indicator_id": "badguy.com", "owner": "Demo Org"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "delete"
    assert "/api/v3/indicators/badguy.com?owner=Demo%20Org" in call["url"]


# ---------------------------------------------------------------------------
# Pagination clamping
# ---------------------------------------------------------------------------


def test_pagination_note_uses_clamped_limit_and_start():
    # An out-of-range limit is clamped to the same [1, 500] bounds used for
    # the request, so the hint can never disagree with what was actually sent.
    # limit=0 falls back to the default 25 (matching the request), so a
    # partial page must not hint (raw 0 would always hint: count >= 0).
    assert tc._pagination_note({}, 0, 0, 0) == ""
    assert tc._pagination_note({}, 1, 0, 0) == ""
    # ... and a full default-sized page does hint.
    assert "start=25" in tc._pagination_note({}, 25, 0, 0)
    # limit above the max clamps to 500: a full 500-item page -> hint (raw
    # 1000 would suppress it).
    assert "start=500" in tc._pagination_note({}, 500, 0, 1000)
    # A negative start is normalised to 0 in the hint, matching the request.
    assert "start=25" in tc._pagination_note({}, 25, -5, 25)
    # Partial page without a next marker -> no hint.
    assert tc._pagination_note({}, 10, 0, 25) == ""
    # The v3 ``next`` marker always wins.
    assert "start=10" in tc._pagination_note({"next": "..."}, 10, 0, 25)


@pytest.mark.asyncio
async def test_list_params_clamp_out_of_range_values(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"data": []})
    await executor.execute_tool(
        ToolCall(
            id="pg1",
            name="list_threatconnect_indicators",
            arguments={"limit": 99999, "start": -10},
        )
    )
    call = recorder.calls[-1]
    assert "resultLimit=500" in call["url"]
    assert "resultStart" not in call["url"]


# ---------------------------------------------------------------------------
# Cases + workflow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_case_validates_severity(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c9",
            name="create_threatconnect_case",
            arguments={"name": "Investigation", "severity": "Extreme"},
        )
    )
    assert "invalid severity" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_workflow_add_note_and_invalid_action(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"data": {"id": 77}, "status": "Success"}
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c10",
            name="manage_threatconnect_workflow",
            arguments={"action": "add_note", "case_id": "3", "text": "Triaged."},
        )
    )
    assert not result.is_error
    assert "note id: 77" in result.content
    call = recorder.calls[-1]
    assert call["json"] == {"caseId": 3, "text": "Triaged."}

    bad = await executor.execute_tool(
        ToolCall(
            id="c11",
            name="manage_threatconnect_workflow",
            arguments={"action": "explode", "case_id": "3"},
        )
    )
    assert "invalid action" in bad.content.lower()


# ---------------------------------------------------------------------------
# Associations + manage tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_associate_objects_appends_group_ids(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"status": "Success"})
    result = await executor.execute_tool(
        ToolCall(
            id="c12",
            name="associate_threatconnect_objects",
            arguments={
                "source_type": "indicator",
                "source_id": "4",
                "target_type": "group",
                "target_ids": "12, 15",
            },
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "put"
    assert "/api/v3/indicators/4" in call["url"]
    assert call["json"]["associatedGroups"]["mode"] == "append"
    assert [g["id"] for g in call["json"]["associatedGroups"]["data"]] == [12, 15]


@pytest.mark.asyncio
async def test_associate_objects_rejects_invalid_types(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c13",
            name="associate_threatconnect_objects",
            arguments={
                "source_type": "spaceship",
                "source_id": "4",
                "target_type": "group",
                "target_ids": "12",
            },
        )
    )
    assert "invalid source_type" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_playbooks_activate_uses_v2(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"status": "Success"})
    result = await executor.execute_tool(
        ToolCall(
            id="c14",
            name="manage_threatconnect_playbooks",
            arguments={"action": "activate", "playbook_id": "10"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"].endswith("/api/v2/playbooks/10/activate")


@pytest.mark.asyncio
async def test_query_threatconnect_rejects_unknown_object_type(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c15",
            name="query_threatconnect",
            arguments={"object_type": "widgets", "tql": "id = 1"},
        )
    )
    assert "unknown object_type" in result.content.lower()
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Error envelope + missing-secret guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_envelope_is_surfaced(monkeypatch):
    executor, _ = _build(
        monkeypatch,
        lambda m, u: {"status": "Error", "message": "TQL parse failure"},
    )
    result = await executor.execute_tool(
        ToolCall(id="c16", name="list_threatconnect_owners", arguments={})
    )
    assert "TQL parse failure" in result.content


@pytest.mark.asyncio
async def test_validate_connection_routes_through_safe_request(monkeypatch):
    recorder = _Recorder(lambda m, u: {"data": [{"id": 1}], "status": "Success"})
    monkeypatch.setattr(tc, "safe_request", recorder)
    ok, message = await tc.validate_connection(
        "https://demo.threatconnect.com", "access-123", "secret-456"
    )
    assert ok
    assert "connected" in message
    call = recorder.calls[0]
    assert "/api/v3/security/owners?resultLimit=1" in call["url"]
    assert call["headers"]["Authorization"].startswith("TC access-123:")
    # expected_statuses lets the probe tell an auth failure from a network error
    assert call["expected_statuses"] == {401, 403}


@pytest.mark.asyncio
async def test_validate_connection_rejects_error_envelope(monkeypatch):
    # A 2xx response that still carries a v3 error envelope must not be
    # reported as a successful connection.
    recorder = _Recorder(
        lambda m, u: {"status": "Error", "message": "Invalid signature"}
    )
    monkeypatch.setattr(tc, "safe_request", recorder)
    ok, message = await tc.validate_connection(
        "https://demo.threatconnect.com", "access-123", "secret-456"
    )
    assert not ok
    assert "Invalid signature" in message


@pytest.mark.asyncio
async def test_validate_connection_rejects_non_json_response(monkeypatch):
    # A 2xx response that is not JSON (e.g. an HTML proxy/SSO page) must not
    # be reported as a successful connection.
    recorder = _Recorder(lambda m, u: ValueError("not json"))
    monkeypatch.setattr(tc, "safe_request", recorder)
    ok, message = await tc.validate_connection(
        "https://demo.threatconnect.com", "access-123", "secret-456"
    )
    assert not ok
    assert "non-JSON" in message


@pytest.mark.asyncio
async def test_api_non_json_body_is_error_but_empty_body_is_success(monkeypatch):
    # A non-JSON 2xx with a body is not a ThreatConnect API response; an
    # empty 2xx body is a legitimate success for v2 playbook activation.
    responses = iter(
        [
            _StubResponse(ValueError("not json"), content=b"<html>portal</html>"),
            _StubResponse(ValueError("not json"), content=b""),
        ]
    )

    async def fake(_client, method, url, _provider, action, **kwargs):
        return next(responses), None

    monkeypatch.setattr(tc, "safe_request", fake)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    bad = await executor.execute_tool(
        ToolCall(id="nj1", name="list_threatconnect_owners", arguments={})
    )
    assert "non-JSON" in bad.content
    ok = await executor.execute_tool(
        ToolCall(
            id="nj2",
            name="manage_threatconnect_playbooks",
            arguments={"action": "activate", "playbook_id": "10"},
        )
    )
    assert "Playbook 10 activated" in ok.content


@pytest.mark.asyncio
async def test_validate_connection_missing_credentials():
    ok, message = await tc.validate_connection("", "access-123", "secret")
    assert not ok and "base URL" in message
    ok, message = await tc.validate_connection(
        "https://demo.threatconnect.com", "", "secret"
    )
    assert not ok and "Access ID" in message


@pytest.mark.asyncio
async def test_missing_secret_key_surfaces_actionable_error(monkeypatch):
    # The 2-part dummy builds the tools, but any call without a Secret Key
    # must return a clear configuration error instead of a broken signature.
    recorder = _Recorder(lambda m, u: {})
    monkeypatch.setattr(tc, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(
        token="https://demo.threatconnect.com||access-123||", executor=executor
    )
    result = await executor.execute_tool(
        ToolCall(id="c17", name="list_threatconnect_owners", arguments={})
    )
    assert "Secret Key is not configured" in result.content
    assert recorder.calls == []
