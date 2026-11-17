"""Tests for delegated-OAuth token acquisition in Defender XDR and Sentinel.

Both providers span two Microsoft API audiences. When an integration uses
delegated OAuth, ``build_tools`` receives a ``token_acquirer`` callback and must
request a token for the correct per-tool audience:

* Defender XDR → Microsoft Graph (incidents/alerts/hunting) and Defender for
  Endpoint (machines/IOCs/response actions).
* Sentinel → Azure Resource Manager (incidents/rules) and Log Analytics (KQL).

The app-only (client-credentials) path must keep working unchanged when no
``token_acquirer`` is supplied.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import defender as df
from app.integrations.providers import sentinel as sn
from app.integrations.providers.defender import (
    _GRAPH_SCOPE,
    _MDE_SCOPE,
)
from app.integrations.providers.defender import (
    build_tools as defender_build_tools,
)
from app.integrations.providers.sentinel import (
    _LA_SCOPE,
    _MGMT_SCOPE,
)
from app.integrations.providers.sentinel import (
    build_tools as sentinel_build_tools,
)
from app.tools.base import CallableToolExecutor, ToolCall


class _StubResponse:
    def __init__(self, payload: dict[str, Any]):
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


def _empty_value_responder():
    async def _responder(_client, _method, _url, _provider, _op, **_kwargs):
        return _StubResponse({"value": []}), None

    return _responder


class _ScopeRecordingAcquirer:
    """Fake ``token_acquirer`` that records the scopes it is asked for."""

    def __init__(self) -> None:
        self.scopes: list[str] = []

    async def __call__(self, scope: str) -> str:
        self.scopes.append(scope)
        return "dummy-delegated-token"


@pytest.mark.asyncio
async def test_defender_delegated_graph_tool_uses_graph_scope(monkeypatch):
    """A Graph-backed Defender tool redeems the refresh token for the Graph audience."""
    monkeypatch.setattr(df, "safe_request", _empty_value_responder())
    acquirer = _ScopeRecordingAcquirer()

    executor = CallableToolExecutor()
    defender_build_tools(token="__oauth__", executor=executor, token_acquirer=acquirer)

    result = await executor.execute_tool(
        ToolCall(id="c1", name="list_defender_incidents", arguments={})
    )

    assert not result.is_error
    assert acquirer.scopes == [_GRAPH_SCOPE]


@pytest.mark.asyncio
async def test_defender_delegated_mde_tool_uses_mde_scope(monkeypatch):
    """An MDE-backed Defender tool redeems the refresh token for the Defender for
    Endpoint audience."""
    async def _machine_responder(_client, _method, _url, _provider, _op, **_kwargs):
        return _StubResponse({"id": "m1", "computerDnsName": "host"}), None

    monkeypatch.setattr(df, "safe_request", _machine_responder)
    acquirer = _ScopeRecordingAcquirer()

    executor = CallableToolExecutor()
    defender_build_tools(token="__oauth__", executor=executor, token_acquirer=acquirer)

    result = await executor.execute_tool(
        ToolCall(id="c2", name="get_defender_machine", arguments={"machine_id": "m1"})
    )

    assert not result.is_error
    assert acquirer.scopes == [_MDE_SCOPE]


@pytest.mark.asyncio
async def test_sentinel_delegated_uses_management_scope(monkeypatch):
    """A Sentinel ARM tool redeems the refresh token for the Azure management audience,
    and the workspace coordinates are parsed from the (coordinates-only) token."""
    monkeypatch.setattr(sn, "safe_request", _empty_value_responder())
    acquirer = _ScopeRecordingAcquirer()

    executor = CallableToolExecutor()
    sentinel_build_tools(
        token="sub-123||rg-soc||ws-prod", executor=executor, token_acquirer=acquirer
    )

    result = await executor.execute_tool(
        ToolCall(id="c3", name="list_sentinel_incidents", arguments={})
    )

    assert not result.is_error
    assert acquirer.scopes == [_MGMT_SCOPE]


@pytest.mark.asyncio
async def test_sentinel_delegated_kql_uses_management_then_log_analytics_scope(
    monkeypatch,
):
    """The KQL tool resolves the workspace ID via ARM (management audience) first,
    then queries Log Analytics (log-analytics audience) — so delegated mode must
    redeem the refresh token for both, in that order."""

    async def _kql_responder(_client, method, _url, _provider, _op, **_kwargs):
        if method == "get":
            # ARM: resolve the Log Analytics workspace customer ID.
            return _StubResponse({"properties": {"customerId": "ws-guid-123"}}), None
        # Log Analytics: KQL query result.
        return (
            _StubResponse({"tables": [{"columns": [{"name": "Count"}], "rows": [[1]]}]}),
            None,
        )

    monkeypatch.setattr(sn, "safe_request", _kql_responder)
    acquirer = _ScopeRecordingAcquirer()

    executor = CallableToolExecutor()
    sentinel_build_tools(
        token="sub-123||rg-soc||ws-prod", executor=executor, token_acquirer=acquirer
    )

    result = await executor.execute_tool(
        ToolCall(
            id="c-kql",
            name="run_sentinel_kql_query",
            arguments={"query": "SecurityEvent | count"},
        )
    )

    assert not result.is_error
    assert acquirer.scopes == [_MGMT_SCOPE, _LA_SCOPE]


@pytest.mark.asyncio
async def test_sentinel_apponly_path_uses_client_credentials(monkeypatch):
    """Without a token_acquirer, Sentinel parses the full compound token and uses
    the client-credentials grant (app-only) — unchanged behaviour."""
    captured: dict[str, Any] = {}

    async def _fake_mgmt_token(tenant_id, client_id, client_secret):
        captured["creds"] = (tenant_id, client_id, client_secret)
        return "app-only-token"

    monkeypatch.setattr(sn, "_get_mgmt_token", _fake_mgmt_token)
    monkeypatch.setattr(sn, "safe_request", _empty_value_responder())

    executor = CallableToolExecutor()
    sentinel_build_tools(
        token="tenant||client||secret||sub||rg||ws", executor=executor
    )

    result = await executor.execute_tool(
        ToolCall(id="c4", name="list_sentinel_incidents", arguments={})
    )

    assert not result.is_error
    assert captured["creds"] == ("tenant", "client", "secret")


def test_sentinel_delegated_missing_coordinates_yields_no_tools():
    """Delegated mode with incomplete workspace coordinates builds no tools."""
    executor = CallableToolExecutor()
    tools = sentinel_build_tools(
        token="sub-only", executor=executor, token_acquirer=_ScopeRecordingAcquirer()
    )
    assert tools == []
