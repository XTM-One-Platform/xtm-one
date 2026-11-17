"""Tests for the Picus Security integration provider.

Covers base-URL resolution, the refresh->access token exchange, the token
cache, list/detail tool flows and their response-envelope parsing, simulation
creation/lifecycle payload shaping, the manage-tool action guards, and
connectivity validation.

The provider authenticates via a refresh->access token exchange
(``_request_access_token``) and routes every API call through the shared
``safe_request`` helper - both are monkeypatched here so no network is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import picus
from app.integrations.providers.picus import (
    build_tools,
    resolve_base_url,
    validate_connection,
)
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://api.picussecurity.com||refresh-abc"


class _StubResponse:
    """Minimal ``httpx.Response`` stand-in (status_code, content, json)."""

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
        payload = self._router(method, url)
        if isinstance(payload, _StubResponse):
            return payload, None
        return _StubResponse(payload), None


async def _token_ok(_base_url, _refresh_token):
    return "access-token", 7200


def _build(monkeypatch, router) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(router)
    monkeypatch.setattr(picus, "safe_request", recorder)
    monkeypatch.setattr(picus, "_request_access_token", _token_ok)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_resolve_base_url_default_and_normalisation():
    assert resolve_base_url("") == "https://api.picussecurity.com"
    assert resolve_base_url("https://picus.acme.com/") == "https://picus.acme.com"
    # A bare host is upgraded to https.
    assert resolve_base_url("picus.acme.com") == "https://picus.acme.com"


def test_resolve_base_url_upgrades_http_to_https():
    # The token exchange carries the refresh token - never use cleartext HTTP.
    assert resolve_base_url("http://picus.acme.com") == "https://picus.acme.com"


def test_csv_normalises_multi_value_filters():
    assert picus._csv("High, Medium") == "High,Medium"
    # Values may legitimately contain spaces (MITRE tactics, actor names) -
    # only commas separate entries.
    assert picus._csv("Credential Access, Defense Evasion") == (
        "Credential Access,Defense Evasion"
    )
    assert picus._csv("") == ""


def test_int_ids_extracts_from_commas_and_spaces():
    assert picus._int_ids("36088, 32631") == [36088, 32631]
    assert picus._int_ids("1 2,3") == [1, 2, 3]
    assert picus._int_ids("") == []


def test_build_tools_returns_definitions_with_dummy_token():
    """Introspection must not early-return: a dummy token still yields tools."""
    executor = CallableToolExecutor()
    tools = build_tools(token="__dummy__", executor=executor)
    names = {t.name for t in tools}
    assert "list_picus_simulations" in names
    assert "create_picus_simulation" in names
    assert "manage_picus_simulation" in names
    assert "list_picus_prevention_recommendations" in names
    assert len(tools) == 24


@pytest.mark.asyncio
async def test_request_access_token_normalises_ms_expire_at(monkeypatch):
    """A millisecond ``expire_at`` must not yield a multi-millennia TTL."""
    import time as _time

    class _Resp:
        is_success = True

        @staticmethod
        def json():
            # expire_at 2 hours from now, in MILLISECONDS.
            return {
                "token": "access-token",
                "expire_at": int((_time.time() + 7200) * 1000),
            }

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return _Resp()

    monkeypatch.setattr(picus.httpx, "AsyncClient", _Client)
    token, ttl = await picus._request_access_token(
        "https://api.picussecurity.com", "refresh"
    )
    assert token == "access-token"
    assert 7000 < ttl <= 7200


@pytest.mark.asyncio
async def test_request_access_token_expired_expire_at_yields_tiny_ttl(monkeypatch):
    """An already-past ``expire_at`` must not fall back to a ~2 h TTL.

    Caching a token the endpoint says is expired would pin repeated 401s for
    two hours. The provider returns a 1 s lifetime instead - usable for the
    immediate call, but below the shared cache's expiry skew so it is never
    cached.
    """
    import time as _time

    class _Resp:
        is_success = True

        @staticmethod
        def json():
            # expire_at one hour in the PAST (clock skew / malformed value).
            return {
                "token": "access-token",
                "expire_at": int(_time.time() - 3600),
            }

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return _Resp()

    monkeypatch.setattr(picus.httpx, "AsyncClient", _Client)
    token, ttl = await picus._request_access_token(
        "https://api.picussecurity.com", "refresh"
    )
    assert token == "access-token"
    assert ttl == 1
    # The shared cache must refuse to cache such a short-lived token.
    from app.integrations.http import get_cached_app_token, set_cached_app_token

    set_cached_app_token("picus-test-expired-ttl", token, ttl)
    assert get_cached_app_token("picus-test-expired-ttl") is None


@pytest.mark.asyncio
async def test_request_access_token_non_json_response(monkeypatch):
    """A non-JSON 200 body (proxy/WAF HTML) must degrade to (None, 0)."""

    class _Resp:
        is_success = True
        status_code = 200

        @staticmethod
        def json():
            raise ValueError("not JSON")

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return _Resp()

    monkeypatch.setattr(picus.httpx, "AsyncClient", _Client)
    token, ttl = await picus._request_access_token(
        "https://api.picussecurity.com", "refresh"
    )
    assert token is None
    assert ttl == 0


@pytest.mark.asyncio
async def test_request_access_token_missing_token_key(monkeypatch):
    """A JSON body without a usable ``token`` must degrade to (None, 0)."""

    class _Resp:
        is_success = True
        status_code = 200

        @staticmethod
        def json():
            return {"message": "ok but no token"}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return _Resp()

    monkeypatch.setattr(picus.httpx, "AsyncClient", _Client)
    token, ttl = await picus._request_access_token(
        "https://api.picussecurity.com", "refresh"
    )
    assert token is None
    assert ttl == 0


# ---------------------------------------------------------------------------
# _api response handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_non_json_2xx_body_surfaces_error(monkeypatch):
    """A non-empty 2xx body that is not JSON must return an explicit error.

    Silently treating it as an empty payload would surface misleading
    downstream messages like "not found" when a proxy/WAF returns HTML.
    """

    def router(method, url):
        return _StubResponse(
            ValueError("not JSON"), status_code=200, content=b"<html>blocked</html>"
        )

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="pj1", name="list_picus_simulations", arguments={})
    )
    assert "non-JSON response" in result.content
    assert "Error" in result.content


@pytest.mark.asyncio
async def test_api_empty_body_is_success(monkeypatch):
    """A 204 / empty body (cancel, delete) is success with no payload."""

    def router(method, url):
        return _StubResponse(None, status_code=204, content=b"")

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="pj2",
            name="manage_picus_simulation",
            arguments={"action": "delete", "simulation_id": 99},
        )
    )
    assert not result.is_error
    assert "deleted" in result.content.lower()
    assert recorder.calls[-1]["method"] == "delete"


# ---------------------------------------------------------------------------
# Simulations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_simulations_builds_params_and_formats(monkeypatch):
    def router(method, url):
        return {
            "simulations": [
                {
                    "simulation_id": 1,
                    "simulation_name": "Endpoint Test",
                    "status": "COMPLETED",
                    "agent": {"name": "My Agent", "status": "alive"},
                    "template_name": "Template 1",
                    "last_prevention_security_score": 60,
                    "last_detection_security_score": 100,
                }
            ],
            "pages": {"total_count": 1, "limit": 25, "offset": 0},
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="p1",
            name="list_picus_simulations",
            arguments={"status": "COMPLETED", "prevention_result_gte": 50},
        )
    )
    assert not result.is_error
    assert "Endpoint Test" in result.content
    call = recorder.calls[-1]
    assert call["url"].endswith("/v1/simulations")
    assert call["params"]["status"] == "COMPLETED"
    assert call["params"]["prevention_result_gte"] == 50


@pytest.mark.asyncio
async def test_get_simulation_result_latest_path_and_analytics(monkeypatch):
    def router(method, url):
        return {
            "simulation_id": 6068,
            "simulation_run_id": 14993,
            "status": "COMPLETED",
            "results": {
                "prevention": {
                    "security_score": 13,
                    "threat": {"blocked_count": 1, "not_blocked_count": 2, "total_count": 3},
                    "attacker_objectives": {
                        "achived_count": 7,
                        "unachived_count": 1,
                        "total_count": 8,
                    },
                },
                "has_detection_analysis": True,
                "detection_analysis": {
                    "security_score": 66,
                    "log": {"logged_threat_count": 1, "not_logged_threat_count": 2, "score": 33},
                    "alert": {"alerted_threat_count": 3, "not_alerted_threat_count": 0, "score": 100},
                    "integrations": [{"product_name": "Crowdstrike", "id": 110}],
                },
            },
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="p2",
            name="get_picus_simulation_result",
            arguments={"simulation_id": 6068},
        )
    )
    assert not result.is_error
    assert "Prevention score: 13" in result.content
    assert "Detection score: 66" in result.content
    # No run_id -> latest run path.
    assert recorder.calls[-1]["url"].endswith("/v1/simulations/6068/run/latest")


@pytest.mark.asyncio
async def test_get_simulation_result_specific_run_path(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"simulation_id": 1, "simulation_run_id": 9}
    )
    await executor.execute_tool(
        ToolCall(
            id="p3",
            name="get_picus_simulation_result",
            arguments={"simulation_id": 1, "run_id": 9},
        )
    )
    assert recorder.calls[-1]["url"].endswith("/v1/simulations/1/run/9")


@pytest.mark.asyncio
async def test_create_simulation_payload(monkeypatch):
    def router(method, url):
        return {
            "simulation": {"id": 5865, "name": "Endpoint Attacks Test - 1"},
            "run_info": {"id": 14288, "status": "SCHEDULED"},
            "run_immediately": True,
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="p4",
            name="create_picus_simulation",
            arguments={
                "name": "Endpoint Attacks Test - 1",
                "template_id": 5832,
                "agent_id": 2874,
                "integrations": "1816, 1585",
            },
        )
    )
    assert not result.is_error
    assert "5865" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["json"]["name"] == "Endpoint Attacks Test - 1"
    assert call["json"]["template_id"] == 5832
    assert call["json"]["agent_id"] == 2874
    assert call["json"]["integrations"] == [1816, 1585]
    # run_now defaults to True -> a ONCE schedule is attached.
    assert call["json"]["schedule"]["frequency"] == "ONCE"


@pytest.mark.asyncio
async def test_create_simulation_requires_name_and_template(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="p5",
            name="create_picus_simulation",
            arguments={"name": "", "template_id": 0},
        )
    )
    assert "required" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_simulation_delete_uses_delete_verb(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="p6",
            name="manage_picus_simulation",
            arguments={"action": "delete", "simulation_id": 42},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "delete"
    assert call["url"].endswith("/v1/simulations/42")


@pytest.mark.asyncio
async def test_manage_simulation_update_omits_is_active_when_not_provided(
    monkeypatch,
):
    """A rename must not silently flip the simulation's active state."""
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="p6b",
            name="manage_picus_simulation",
            arguments={"action": "update", "simulation_id": 42, "name": "Renamed"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["json"] == {"name": "Renamed"}


@pytest.mark.asyncio
async def test_manage_simulation_update_requires_a_field(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="p6c",
            name="manage_picus_simulation",
            arguments={"action": "update", "simulation_id": 42},
        )
    )
    assert "at least one" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_simulation_cancel_uses_cancel_path(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    await executor.execute_tool(
        ToolCall(
            id="p7",
            name="manage_picus_simulation",
            arguments={"action": "cancel", "simulation_id": 7},
        )
    )
    call = recorder.calls[-1]
    assert call["method"] == "put"
    assert call["url"].endswith("/v1/simulations/7/cancel")


@pytest.mark.asyncio
async def test_manage_simulation_rejects_invalid_action(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="p8",
            name="manage_picus_simulation",
            arguments={"action": "explode", "simulation_id": 1},
        )
    )
    assert "invalid action" in result.content.lower()
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Threat library + summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_threats_formats_severity_and_mitre(monkeypatch):
    def router(method, url):
        return {
            "threats": [
                {
                    "id": 1,
                    "display_id": 1000,
                    "name": "Mimikatz",
                    "severity": "High",
                    "affected_os": ["Windows"],
                    "mitre_attack": [{"tactic": "Credential Access"}],
                }
            ],
            "pages": {"total_count": 1},
        }

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="p9", name="list_picus_threats", arguments={"severities": "High"})
    )
    assert not result.is_error
    assert "Mimikatz" in result.content
    assert "Credential Access" in result.content


@pytest.mark.asyncio
async def test_get_summary_posts_period_body(monkeypatch):
    def router(method, url):
        return {
            "prevention": {
                "score": 84,
                "threats": {"count": 76, "blocked": {"count": 58}, "notBlocked": {"count": 18}},
                "objectives": {"count": 431, "achieved": {"count": 68}, "notAchieved": {"count": 363}},
            },
            "detection": {"score": 0, "threatCount": 0},
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="p10",
            name="get_picus_summary",
            arguments={"period_type": "QUARTER", "simulation_ids": "36088, 32631"},
        )
    )
    assert not result.is_error
    assert "Prevention score: 84" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["json"]["periodType"] == "QUARTER"
    assert call["json"]["simulationIds"] == [36088, 32631]


@pytest.mark.asyncio
async def test_get_summary_rejects_bad_period(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="p11", name="get_picus_summary", arguments={"period_type": "DAILY"})
    )
    assert "period_type" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_list_threat_actors_surfaces_total_and_next_offset(monkeypatch):
    """Threat actors must paginate like the other list tools (total + hint)."""

    def router(method, url):
        return {
            "threat_actors": [{"name": "APT28"}, {"name": "Lazarus"}],
            "pages": {"total_count": 5, "limit": 2, "offset": 0},
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="p9b",
            name="list_picus_threat_actors",
            arguments={"limit": 2, "offset": 0},
        )
    )
    assert not result.is_error
    assert "APT28" in result.content
    assert "5" in result.content  # total surfaced
    assert "pass offset=2" in result.content
    assert recorder.calls[-1]["params"] == {"limit": 2, "offset": 0}


# ---------------------------------------------------------------------------
# Mitigations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_device_signatures_sends_normalised_ids(monkeypatch):
    def router(method, url):
        return {"signatures": [{"id": 9, "name": "Sig", "vendor_severity": "High"}]}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="p14",
            name="list_picus_device_signatures",
            arguments={"device_id": 5, "action_ids": "101, 102 , 103"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["url"].endswith("/v1/mitigation/devices/5/signatures")
    assert call["params"]["action_ids"] == "101,102,103"


@pytest.mark.asyncio
async def test_device_signatures_rejects_more_than_ten_ids(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="p15",
            name="list_picus_device_signatures",
            arguments={
                "device_id": 5,
                "action_ids": ",".join(str(i) for i in range(1, 12)),
            },
        )
    )
    assert "at most 10" in result.content
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Admin consolidated tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_admin_get_detection_rule_requires_source_and_rule(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="p12",
            name="manage_picus_admin",
            arguments={"action": "get_detection_rule", "source": "sigma"},
        )
    )
    assert "required" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_admin_list_roles(monkeypatch):
    def router(method, url):
        return {"roles": [{"id": 3, "name": "Admin"}]}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="p13", name="manage_picus_admin", arguments={"action": "list_roles"})
    )
    assert not result.is_error
    assert "Admin" in result.content
    assert recorder.calls[-1]["url"].endswith("/v1/users/roles")


# ---------------------------------------------------------------------------
# Connectivity validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_connection_ok(monkeypatch):
    monkeypatch.setattr(picus, "_request_access_token", _token_ok)
    ok, message = await validate_connection("https://api.picussecurity.com", "refresh")
    assert ok is True
    assert "connected" in message.lower()


@pytest.mark.asyncio
async def test_validate_connection_failure(monkeypatch):
    async def _token_fail(_b, _r):
        return None, 0

    monkeypatch.setattr(picus, "_request_access_token", _token_fail)
    ok, message = await validate_connection("https://api.picussecurity.com", "bad")
    assert ok is False
    assert "authentication failed" in message.lower()


@pytest.mark.asyncio
async def test_validate_connection_missing_refresh_token():
    ok, message = await validate_connection("https://api.picussecurity.com", "")
    assert ok is False
    assert "missing" in message.lower()


@pytest.mark.asyncio
async def test_validate_connection_whitespace_refresh_token():
    """A whitespace-only refresh token is treated as missing, not sent."""
    ok, message = await validate_connection("https://api.picussecurity.com", "  \n")
    assert ok is False
    assert "missing" in message.lower()


@pytest.mark.asyncio
async def test_build_tools_strips_compound_token_whitespace(monkeypatch):
    """A refresh token pasted with a trailing newline must be stripped."""
    seen: dict[str, str] = {}

    async def _capture(base_url, refresh_token):
        seen["base_url"] = base_url
        seen["refresh_token"] = refresh_token
        return "access-token", 7200

    monkeypatch.setattr(picus, "_request_access_token", _capture)
    monkeypatch.setattr(
        picus,
        "safe_request",
        _Recorder(lambda m, u: {"integrations": []}),
    )
    executor = CallableToolExecutor()
    # A refresh token unique to this test: the app-token cache is process
    # global and keyed by the credentials, so reusing another test's token
    # would hit the cache and skip the capture.
    build_tools(
        token=" https://api.picussecurity.com ||refresh-strip\n", executor=executor
    )
    result = await executor.execute_tool(
        ToolCall(id="pw", name="list_picus_integrations", arguments={})
    )
    assert not result.is_error
    assert seen["base_url"] == "https://api.picussecurity.com"
    assert seen["refresh_token"] == "refresh-strip"
