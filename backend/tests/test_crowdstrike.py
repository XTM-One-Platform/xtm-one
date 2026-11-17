"""Tests for the CrowdStrike Falcon integration provider.

Covers cloud-region resolution, the query→entities tool flow, alert/incident
triage payload shaping, host containment, IOC creation, FQL quoting, the
read-only Real Time Response guardrail, and connectivity validation.

The provider authenticates via an OAuth2 client-credentials token exchange
(``_request_token_with_ttl``) and routes every API call through the shared
``safe_request`` helper — both are monkeypatched here so no network is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import crowdstrike as cs
from app.integrations.providers.crowdstrike import (
    build_tools,
    resolve_base_url,
    validate_connection,
)
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://api.crowdstrike.com||client-id||client-secret"


class _StubResponse:
    def __init__(self, payload: dict[str, Any]):
        self._payload = payload

    def json(self) -> dict[str, Any]:
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


async def _token_ok(_base_url, _client_id, _client_secret):
    return "dummy-token", 1799


def _build(monkeypatch, router) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(router)
    monkeypatch.setattr(cs, "safe_request", recorder)
    monkeypatch.setattr(cs, "_request_token_with_ttl", _token_ok)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_resolve_base_url_known_clouds():
    assert resolve_base_url("us-1") == "https://api.crowdstrike.com"
    assert resolve_base_url("US-2") == "https://api.us-2.crowdstrike.com"
    assert resolve_base_url("eu-1") == "https://api.eu-1.crowdstrike.com"
    assert resolve_base_url("us-gov-1") == "https://api.laggar.gcw.crowdstrike.com"
    assert resolve_base_url("us-gov-2") == "https://api.us-gov-2.crowdstrike.mil"


def test_resolve_base_url_defaults_and_raw_url():
    assert resolve_base_url("") == "https://api.crowdstrike.com"
    assert resolve_base_url("totally-unknown") == "https://api.crowdstrike.com"
    assert resolve_base_url("https://api.eu-1.crowdstrike.com/") == (
        "https://api.eu-1.crowdstrike.com"
    )


def test_resolve_base_url_upgrades_http_to_https():
    # The token exchange carries the client secret — never use cleartext HTTP.
    assert resolve_base_url("http://api.crowdstrike.com") == (
        "https://api.crowdstrike.com"
    )


def test_resolve_base_url_preserves_raw_url_case():
    # Only the cloud-alias lookup is case-insensitive; a raw URL keeps its case
    # so a case-sensitive path component isn't mangled by lowercasing.
    assert resolve_base_url("https://api.crowdstrike.com/Custom/Path") == (
        "https://api.crowdstrike.com/Custom/Path"
    )


def test_fql_quote_escapes_quotes_and_backslashes():
    assert cs._fql_quote("O'Brien") == "O\\'Brien"
    assert cs._fql_quote("a\\b") == "a\\\\b"
    assert cs._fql_quote("clean") == "clean"


def test_build_tools_returns_definitions_with_dummy_token():
    """Introspection must not early-return: a dummy token still yields tools."""
    executor = CallableToolExecutor()
    tools = build_tools(token="__dummy__", executor=executor)
    names = {t.name for t in tools}
    assert "list_crowdstrike_alerts" in names
    assert "contain_crowdstrike_host" in names
    assert "run_crowdstrike_rtr_command" in names
    assert len(tools) >= 20


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_alerts_builds_filter_and_fetches_entities(monkeypatch):
    def router(method, url):
        if "queries/alerts" in url:
            return {"resources": ["cid:aid:1", "cid:aid:2"]}
        return {
            "resources": [
                {
                    "composite_id": "cid:aid:1",
                    "display_name": "Suspicious PowerShell",
                    "severity_name": "High",
                    "status": "new",
                    "device": {"hostname": "WIN-01"},
                    "tactic": "Execution",
                    "technique": "PowerShell",
                }
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="list_crowdstrike_alerts",
            arguments={"status": "new", "severity": "High"},
        )
    )

    assert not result.is_error
    assert "Suspicious PowerShell" in result.content
    # The query call's FQL combines status + severity_name with the AND (+) op.
    query_call = next(c for c in recorder.calls if "queries/alerts" in c["url"])
    assert query_call["params"]["filter"] == "status:'new'+severity_name:'High'"
    # Entity details are fetched by composite ID.
    detail_call = next(c for c in recorder.calls if "entities/alerts" in c["url"])
    assert detail_call["json"] == {"composite_ids": ["cid:aid:1", "cid:aid:2"]}


@pytest.mark.asyncio
async def test_update_alert_builds_action_parameters(monkeypatch):
    def router(method, url):
        return {"resources": [], "errors": []}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c2",
            name="update_crowdstrike_alert",
            arguments={
                "alert_id": "cid:aid:1",
                "status": "in_progress",
                "assigned_to": "analyst@example.com",
                "comment": "Investigating",
            },
        )
    )

    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "patch"
    assert call["json"]["composite_ids"] == ["cid:aid:1"]
    params = {p["name"]: p["value"] for p in call["json"]["action_parameters"]}
    assert params["update_status"] == "in_progress"
    assert params["assign_to_user_id"] == "analyst@example.com"
    assert params["append_comment"] == "Investigating"


@pytest.mark.asyncio
async def test_update_alert_requires_a_field(monkeypatch):
    executor, _ = _build(monkeypatch, lambda m, u: {"resources": []})
    result = await executor.execute_tool(
        ToolCall(id="c3", name="update_crowdstrike_alert", arguments={"alert_id": "x"})
    )
    assert "nothing to update" in result.content.lower()


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_incident_maps_status_to_numeric_code(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"resources": []})
    result = await executor.execute_tool(
        ToolCall(
            id="c4",
            name="update_crowdstrike_incident",
            arguments={"incident_id": "inc:1", "status": "closed"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    params = {p["name"]: p["value"] for p in call["json"]["action_parameters"]}
    assert params["update_status"] == "40"  # closed → 40
    assert call["json"]["ids"] == ["inc:1"]


@pytest.mark.asyncio
async def test_update_incident_rejects_invalid_status(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"resources": []})
    result = await executor.execute_tool(
        ToolCall(
            id="c5",
            name="update_crowdstrike_incident",
            arguments={"incident_id": "inc:1", "status": "bogus"},
        )
    )
    assert "invalid status" in result.content.lower()
    assert recorder.calls == []  # no API call made


# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_hosts_hostname_filter_builds_valid_fql(monkeypatch):
    """The hostname filter must produce a valid quoted-wildcard FQL literal
    (``hostname:'*value*'``) — no stray ``*`` before the opening quote — and
    combine with the platform clause via the AND (+) operator."""

    def router(method, url):
        if "queries/devices" in url:
            return {"resources": ["aid-1"]}
        return {"resources": [{"hostname": "WIN-01", "platform_name": "Windows"}]}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c6a",
            name="list_crowdstrike_hosts",
            arguments={"hostname": "WIN", "platform": "Windows"},
        )
    )

    assert not result.is_error
    query_call = next(c for c in recorder.calls if "queries/devices" in c["url"])
    fql = query_call["params"]["filter"]
    assert fql == "hostname:'*WIN*'+platform_name:'Windows'"
    # Guard against the regression: never emit the malformed ``:*'`` hint form.
    assert ":*'" not in fql


@pytest.mark.asyncio
async def test_contain_host_uses_contain_action(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"resources": [{}]})
    result = await executor.execute_tool(
        ToolCall(
            id="c6",
            name="contain_crowdstrike_host",
            arguments={"device_id": "aid-123"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert "devices-actions" in call["url"]
    assert call["params"]["action_name"] == "contain"
    assert call["json"] == {"ids": ["aid-123"]}


# ---------------------------------------------------------------------------
# IOC management
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_ioc_payload(monkeypatch):
    def router(method, url):
        return {"resources": [{"id": "ioc-1"}]}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c7",
            name="create_crowdstrike_ioc",
            arguments={
                "ioc_type": "domain",
                "value": "evil.example.com",
                "action": "prevent",
                "severity": "high",
                "platforms": "windows,linux",
            },
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    indicator = call["json"]["indicators"][0]
    assert indicator["type"] == "domain"
    assert indicator["value"] == "evil.example.com"
    assert indicator["action"] == "prevent"
    assert indicator["platforms"] == ["windows", "linux"]
    assert indicator["applied_globally"] is True


# ---------------------------------------------------------------------------
# Real Time Response (read-only guardrail)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rtr_rejects_non_readonly_command(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"resources": []})
    result = await executor.execute_tool(
        ToolCall(
            id="c8",
            name="run_crowdstrike_rtr_command",
            arguments={"device_id": "aid-1", "command": "rm -rf /"},
        )
    )
    assert "not an allowed read-only" in result.content.lower()
    assert recorder.calls == []  # never opened a session


@pytest.mark.asyncio
async def test_rtr_runs_readonly_command_and_closes_session(monkeypatch):
    async def _fast_sleep(_seconds):
        return None

    monkeypatch.setattr(cs.asyncio, "sleep", _fast_sleep)

    def router(method, url):
        if method == "post" and url.endswith("/sessions/v1"):
            return {"resources": [{"session_id": "sess-1"}]}
        if method == "post" and url.endswith("/command/v1"):
            return {"resources": [{"cloud_request_id": "cr-1"}]}
        if method == "get" and url.endswith("/command/v1"):
            return {"resources": [{"complete": True, "stdout": "file1\nfile2", "stderr": ""}]}
        return {"resources": []}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c9",
            name="run_crowdstrike_rtr_command",
            arguments={"device_id": "aid-1", "command": "ls C:\\Windows"},
        )
    )
    assert not result.is_error
    assert "file1" in result.content
    # The session must always be closed in the finally block.
    assert any(c["method"] == "delete" and "/sessions/v1" in c["url"] for c in recorder.calls)


@pytest.mark.asyncio
async def test_rtr_reg_requires_query_subcommand(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"resources": []})
    result = await executor.execute_tool(
        ToolCall(
            id="c10",
            name="run_crowdstrike_rtr_command",
            arguments={"device_id": "aid-1", "command": "reg set foo bar"},
        )
    )
    assert "reg query" in result.content.lower()
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Connectivity validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_connection_ok(monkeypatch):
    monkeypatch.setattr(cs, "_request_token_with_ttl", _token_ok)
    ok, message = await validate_connection(
        "https://api.crowdstrike.com", "cid", "secret"
    )
    assert ok is True
    assert "connected" in message.lower()


@pytest.mark.asyncio
async def test_validate_connection_failure(monkeypatch):
    async def _token_fail(_b, _c, _s):
        return None, 0

    monkeypatch.setattr(cs, "_request_token_with_ttl", _token_fail)
    ok, message = await validate_connection(
        "https://api.crowdstrike.com", "cid", "bad-secret"
    )
    assert ok is False
    assert "authentication failed" in message.lower()


@pytest.mark.asyncio
async def test_validate_connection_missing_credentials():
    # Empty credentials short-circuit before any token exchange.
    ok, message = await validate_connection("https://api.crowdstrike.com", "", "")
    assert ok is False
    assert "missing" in message.lower()
