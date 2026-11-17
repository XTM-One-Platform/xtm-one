"""Regression tests for ``get_defender_machine`` IP-address formatting.

The MDE Machine resource returns ``ipAddresses`` as a collection of *IpAddress
objects* (``{"ipAddress", "macAddress", "operationalStatus", …}``), not bare
strings. ``get_defender_machine`` must extract the address field from each
object before joining — joining the dicts directly raises ``TypeError`` — and
fall back to ``lastIpAddress`` when the collection is absent.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers.defender import build_tools
from app.tools.base import CallableToolExecutor, ToolCall


class _StubResponse:
    def __init__(self, payload: dict[str, Any]):
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


def _machine_responder(payload: dict[str, Any]):
    async def _responder(_client, _method, _url, _provider, _op, **_kwargs):
        return _StubResponse(payload), None

    return _responder


async def _async_token() -> str:
    return "dummy-token"


async def _run_get_machine(monkeypatch, payload: dict[str, Any]):
    from app.integrations.providers import defender as df

    monkeypatch.setattr(df, "safe_request", _machine_responder(payload))
    monkeypatch.setattr(df, "_get_token", lambda *a, **k: _async_token())

    executor = CallableToolExecutor()
    build_tools(token="tenant||client||secret", executor=executor)
    return await executor.execute_tool(
        ToolCall(
            id="c1",
            name="get_defender_machine",
            arguments={"machine_id": "m1"},
        )
    )


@pytest.mark.asyncio
async def test_get_machine_extracts_ip_addresses_from_objects(monkeypatch):
    """``ipAddresses`` objects must be rendered by their ``ipAddress`` field,
    not crash on ``", ".join`` of dicts."""
    payload = {
        "id": "m1",
        "computerDnsName": "host1.contoso.com",
        "lastIpAddress": "10.0.0.9",
        "ipAddresses": [
            {"ipAddress": "10.166.113.47", "macAddress": "AABBCC", "operationalStatus": "Up"},
            {"ipAddress": "2a01:110:68:4::1", "macAddress": "AABBCC", "operationalStatus": "Up"},
        ],
    }
    result = await _run_get_machine(monkeypatch, payload)

    assert not result.is_error
    assert "10.166.113.47" in result.content
    assert "2a01:110:68:4::1" in result.content
    # The raw dict repr must never leak into the output.
    assert "operationalStatus" not in result.content
    assert "{" not in result.content


@pytest.mark.asyncio
async def test_get_machine_falls_back_to_last_ip_when_no_collection(monkeypatch):
    """With no ``ipAddresses`` collection, the IP line uses ``lastIpAddress``."""
    payload = {
        "id": "m1",
        "computerDnsName": "host2.contoso.com",
        "lastIpAddress": "172.16.4.5",
    }
    result = await _run_get_machine(monkeypatch, payload)

    assert not result.is_error
    assert "172.16.4.5" in result.content
