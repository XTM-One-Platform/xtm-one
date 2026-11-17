"""Tests for the Palo Alto Cortex XDR (and XSIAM) integration provider.

Covers tool registration (22 tools; api_key_id required), epoch-ms timestamp
rendering (UTC `Z` retained), version-aware auth headers (standard vs advanced,
with per-request replay fields), the `request_data` envelope + API prefix on
incident search, and the deliberate exclusion of XDR from the user-facing
public-URL providers (its `base_url` is the tenant API host, not the console UI
host). `safe_request` is monkeypatched so no network is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import xdr as xd
from app.integrations.providers.xdr import (
    _ts,
    build_tools,
    build_xdr_headers,
    xdr_api_base,
)
from app.tools.base import CallableToolExecutor, ToolCall

# base_url || api_key || api_key_id || auth_mode
_TOKEN = "https://api-tenant.xdr.example||api-key-abc||4242||standard"


class _StubResponse:
    def __init__(self, payload: Any):
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _Recorder:
    def __init__(self, router):
        self._router = router
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        return _StubResponse(self._router(method, url)), None


def _build(monkeypatch, router, token=_TOKEN) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(router)
    monkeypatch.setattr(xd, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=token, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_ts_keeps_utc_z_at_second_precision():
    # 1714557600000 ms == 2024-05-01T10:00:00Z
    assert _ts(1714557600000) == "2024-05-01T10:00:00Z"
    assert _ts(0) == ""
    assert _ts(None) == ""
    assert _ts("") == ""


def test_api_base_adds_public_prefix():
    assert xdr_api_base("https://api-x.example/") == "https://api-x.example/public_api/v1"


# ---------------------------------------------------------------------------
# Registration / introspection
# ---------------------------------------------------------------------------


def test_build_tools_registers_22_tools():
    executor = CallableToolExecutor()
    tools = build_tools(token=_TOKEN, executor=executor)
    names = {t.name for t in tools}
    assert len(tools) == 25
    assert {
        "get_xdr_status",
        "run_xdr_xql_query",
        "isolate_xdr_endpoint",
        "blocklist_xdr_files",
    } <= names


def test_build_tools_requires_api_key_id():
    """Cortex XDR mandates the API Key ID (x-xdr-auth-id); without it the
    builder must yield no tools rather than emit unauthenticated calls."""
    executor = CallableToolExecutor()
    # 3rd segment (api_key_id) empty.
    tools = build_tools(
        token="https://api.example||key||||standard", executor=executor
    )
    assert tools == []


# ---------------------------------------------------------------------------
# Auth headers
# ---------------------------------------------------------------------------


def test_standard_headers():
    h = build_xdr_headers("the-key", "4242", "standard")
    assert h["Authorization"] == "the-key"
    assert h["x-xdr-auth-id"] == "4242"
    assert "x-xdr-nonce" not in h


def test_advanced_headers_hash_and_replay_fields():
    h1 = build_xdr_headers("the-key", "4242", "advanced")
    assert h1["x-xdr-auth-id"] == "4242"
    assert h1["x-xdr-nonce"] and h1["x-xdr-timestamp"]
    # Authorization is a SHA-256 hex digest, not the raw key.
    assert h1["Authorization"] != "the-key"
    assert len(h1["Authorization"]) == 64
    # Fresh nonce per call (replay protection).
    h2 = build_xdr_headers("the-key", "4242", "advanced")
    assert h1["x-xdr-nonce"] != h2["x-xdr-nonce"]


# ---------------------------------------------------------------------------
# Incidents — request_data envelope + timestamp rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_incidents_builds_request_data_envelope(monkeypatch):
    def router(method, url):
        return {
            "reply": {
                "incidents": [
                    {
                        "incident_id": "1",
                        "severity": "high",
                        "status": "new",
                        "description": "boom",
                        "alert_count": 2,
                        "creation_time": 1714557600000,
                    }
                ],
                "total_count": 1,
            }
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="i1",
            name="list_xdr_incidents",
            arguments={"status": "new", "since_hours": 24},
        )
    )
    assert not result.is_error
    assert "boom" in result.content
    # Timestamp keeps the UTC Z (regression guard for _ts).
    assert "2024-05-01T10:00:00Z" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/public_api/v1/incidents/get_incidents/")
    request_data = call["json"]["request_data"]
    fields = {f["field"] for f in request_data["filters"]}
    assert {"status", "creation_time"} <= fields


# ---------------------------------------------------------------------------
# search_to is clamped to a positive lower bound
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_incidents_clamps_nonpositive_limit(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"reply": {"incidents": [], "total_count": 0}}
    )
    await executor.execute_tool(
        ToolCall(id="c1", name="list_xdr_incidents", arguments={"limit": 0})
    )
    assert recorder.calls[0]["json"]["request_data"]["search_to"] == 1


# ---------------------------------------------------------------------------
# Public-URL exclusion: XDR base_url is the API host, not the console UI host
# ---------------------------------------------------------------------------


def test_xdr_and_xsoar_excluded_from_user_facing_url_providers():
    from app.services.integration_tools import _USER_FACING_BASE_URL_PROVIDERS

    assert "xdr" not in _USER_FACING_BASE_URL_PROVIDERS
    assert "xsoar" not in _USER_FACING_BASE_URL_PROVIDERS
