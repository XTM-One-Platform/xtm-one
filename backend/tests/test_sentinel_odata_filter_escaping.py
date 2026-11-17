"""Regression tests for Microsoft Sentinel OData ``$filter`` single-quote escaping.

Free-text filter values (threat-indicator ``source``/``patternType`` and the
incident ``status``/``severity`` enums) are interpolated into single-quoted
OData string literals. A literal single quote must be doubled per the OData
spec, otherwise the literal terminates early — breaking the query or allowing
filter injection. This mirrors the equivalent Defender XDR fix.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import sentinel as sn
from app.integrations.providers.sentinel import _odata_quote, build_tools
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "tenant||client||secret||sub||rg||ws"


class _StubResponse:
    def __init__(self, payload: dict[str, Any]):
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _RequestRecorder:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, provider, op, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return _StubResponse({"value": []}), None


async def _fake_token(*_args, **_kwargs):
    return "dummy-token"


def test_odata_quote_doubles_single_quotes():
    assert _odata_quote("O'Brien") == "O''Brien"
    assert _odata_quote("a'b'c") == "a''b''c"
    assert _odata_quote("'x'") == "''x''"


def test_odata_quote_leaves_quote_free_values_untouched():
    for value in ("Active", "High", "MyFeed", "stix", ""):
        assert _odata_quote(value) == value


@pytest.mark.asyncio
async def test_list_threat_indicators_escapes_source_quote(monkeypatch):
    """A threat-indicator ``source`` containing an apostrophe is escaped, and
    multiple filters are still joined with ``and``."""
    recorder = _RequestRecorder()
    monkeypatch.setattr(sn, "safe_request", recorder)
    monkeypatch.setattr(sn, "_get_mgmt_token", _fake_token)

    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)

    result = await executor.execute_tool(
        ToolCall(
            id="call-1",
            name="list_sentinel_threat_indicators",
            arguments={"indicator_type": "ipv4-addr", "source": "Acme's Feed"},
        )
    )

    assert not result.is_error
    assert recorder.calls[0]["params"]["$filter"] == (
        "properties/patternType eq 'ipv4-addr' and "
        "properties/source eq 'Acme''s Feed'"
    )


@pytest.mark.asyncio
async def test_list_incidents_escapes_status_quote(monkeypatch):
    recorder = _RequestRecorder()
    monkeypatch.setattr(sn, "safe_request", recorder)
    monkeypatch.setattr(sn, "_get_mgmt_token", _fake_token)

    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)

    result = await executor.execute_tool(
        ToolCall(
            id="call-2",
            name="list_sentinel_incidents",
            arguments={"status": "O'Active"},
        )
    )

    assert not result.is_error
    assert recorder.calls[0]["params"]["$filter"] == "properties/status eq 'O''Active'"
