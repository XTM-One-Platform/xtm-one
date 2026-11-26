"""Every OpenAEV tool re-mints the platform-managed JWT (#2372).

A platform-managed OpenAEV integration authenticates with a short-lived
cross-platform JWT. The tools that opened a plain ``egress_client`` sent it as
is past its expiry, got a 401, and the run's auth guard then refused every
OpenAEV tool with a "re-authorize" instruction. These tests pin:

* the tools named in the issue re-mint once on a 401 and answer, and the guard
  records nothing;
* a 401 that persists after the re-mint is still recorded and blocks the
  integration (#1454), with a refusal that says the credential could not be
  refreshed, never "re-authorize";
* a static-key 401 keeps the #1454 refusal text;
* no OpenAEV tool module opens ``egress_client`` directly.
"""

from __future__ import annotations

import pathlib
import re
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.integrations.providers.openaev import _common
from app.integrations.providers.openaev._common import _headers
from app.integrations.providers.openaev.core import register_core
from app.services.integration_auth_guard import AuthGuardedExecutor, IntegrationAuthGuard
from app.tools.base import CallableToolExecutor, ToolCall, ToolDefinition

BASE = "https://demo.openaev.io"

_BODIES: dict[str, Any] = {
    "/api/scenarios/search": {"content": [{"scenario_id": "s1", "scenario_name": "Scen"}], "totalElements": 1},
    "/api/endpoints/a1": {"asset_id": "a1", "asset_name": "host-1"},
    "/api/scenarios/s1": {"scenario_id": "s1", "scenario_name": "Scen", "scenario_injects": []},
}


def _tool_set(refresh_fn: Any) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    executor = CallableToolExecutor()
    headers = _headers("stale-jwt", refresh_fn=refresh_fn)
    defs = register_core(BASE, headers, executor)
    return [
        ToolDefinition(name=d.name, description=d.description, source="integration:openaev") for d in defs
    ], executor


def _server(valid: set[str], seen: list[str | None]) -> Callable[..., Awaitable[httpx.Response]]:
    async def _fake_request(self: httpx.AsyncClient, method: str, url: Any, **kwargs: Any) -> httpx.Response:
        auth = (kwargs.get("headers") or {}).get("Authorization")
        seen.append(auth)
        request = httpx.Request(method, str(url))
        if auth not in valid:
            return httpx.Response(401, request=request, json={"message": "Unauthorized"})
        path = httpx.URL(str(url)).path
        return httpx.Response(200, request=request, json=_BODIES.get(path, {}))

    return _fake_request


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        ("search_openaev_scenarios", {"search": ""}, "Scen"),
        ("get_openaev_asset", {"asset_id": "a1"}, "host-1"),
        ("get_openaev_scenario", {"scenario_id": "s1"}, "Scen"),
    ],
)
async def test_tool_remints_once_on_401_and_answers(tool: str, arguments: dict[str, str], expected: str) -> None:
    remints = {"n": 0}

    async def _remint() -> str:
        remints["n"] += 1
        return "fresh-jwt"

    defs, inner = _tool_set(_remint)
    guard = IntegrationAuthGuard()
    executor = AuthGuardedExecutor(inner, defs, guard)
    seen: list[str | None] = []
    with patch.object(httpx.AsyncClient, "request", _server({"Bearer fresh-jwt"}, seen)):
        result = await executor.execute_tool(ToolCall(id="1", name=tool, arguments=arguments))

    assert not result.is_error, result.content
    assert expected in result.content
    assert remints["n"] == 1
    assert seen[:2] == ["Bearer stale-jwt", "Bearer fresh-jwt"]
    assert guard.failures() == []
    assert not guard.rejected("openaev")


@pytest.mark.asyncio
async def test_401_after_remint_still_blocks_with_a_refresh_refusal() -> None:
    async def _remint() -> str:
        return "also-refused-jwt"

    defs, inner = _tool_set(_remint)
    guard = IntegrationAuthGuard()
    executor = AuthGuardedExecutor(inner, defs, guard)
    with patch.object(httpx.AsyncClient, "request", _server(set(), [])):
        first = await executor.execute_tool(ToolCall(id="1", name="search_openaev_scenarios", arguments={"search": ""}))
        refused = await executor.execute_tool(ToolCall(id="2", name="get_openaev_asset", arguments={"asset_id": "a1"}))

    assert first.is_error
    assert guard.failures_of("openaev") == "401"
    assert guard.rejected("openaev")
    assert refused.refused
    assert "could not be refreshed" in refused.content
    assert "re-authorize" not in refused.content


@pytest.mark.asyncio
async def test_failed_remint_blocks_with_a_refresh_refusal() -> None:
    async def _remint() -> str | None:
        return None

    defs, inner = _tool_set(_remint)
    guard = IntegrationAuthGuard()
    executor = AuthGuardedExecutor(inner, defs, guard)
    with patch.object(httpx.AsyncClient, "request", _server(set(), [])):
        await executor.execute_tool(ToolCall(id="1", name="get_openaev_asset", arguments={"asset_id": "a1"}))
        refused = await executor.execute_tool(
            ToolCall(id="2", name="search_openaev_scenarios", arguments={"search": ""})
        )

    assert refused.refused
    assert "could not be refreshed" in refused.content


@pytest.mark.asyncio
async def test_static_key_401_keeps_the_reauthorize_refusal() -> None:
    defs, inner = _tool_set(None)
    guard = IntegrationAuthGuard()
    executor = AuthGuardedExecutor(inner, defs, guard)
    seen: list[str | None] = []
    with patch.object(httpx.AsyncClient, "request", _server(set(), seen)):
        await executor.execute_tool(ToolCall(id="1", name="get_openaev_asset", arguments={"asset_id": "a1"}))
        refused = await executor.execute_tool(
            ToolCall(id="2", name="search_openaev_scenarios", arguments={"search": ""})
        )

    assert seen == ["Bearer stale-jwt"], "no retry without a refresh callback"
    assert refused.refused
    assert "re-authorized" in refused.content


_DIRECT_EGRESS = re.compile(r"\begress_client\(")


def test_no_openaev_tool_module_opens_egress_client_directly() -> None:
    # A tool that opens its own egress_client skips the JWT re-mint: every
    # tool goes through _api or tool_client (#2372). _common.py defines both.
    pkg = pathlib.Path(_common.__file__).parent
    offenders = [
        f"{path.name}:{n}"
        for path in sorted(pkg.glob("*.py"))
        if path.name != "_common.py"
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if _DIRECT_EGRESS.search(line)
    ]
    assert not offenders, f"OpenAEV tools opening egress_client directly: {offenders}"
