"""Regression tests for Defender XDR OData ``$filter`` single-quote escaping.

A value interpolated into a single-quoted OData string literal (e.g.
``assignedTo eq '{value}'``) must have any literal single quote doubled per the
OData spec. Display names, UPN local parts, and service-source names can
legitimately contain an apostrophe (``O'Brien``); without escaping the literal
terminates early and Graph/MDE rejects the request (or the filter is silently
mis-parsed). Raw caller-supplied ``odata_filter`` expressions are intentionally
passed through verbatim and are not covered here.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers.defender import _odata_quote, build_tools
from app.tools.base import CallableToolExecutor, ToolCall


class _StubResponse:
    def __init__(self, payload: dict[str, Any]):
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _RequestRecorder:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, provider, op, **kwargs):
        self.calls.append(
            {"method": method, "url": url, "provider": provider, "op": op, **kwargs}
        )
        return _StubResponse({"value": []}), None


def test_odata_quote_doubles_single_quotes():
    assert _odata_quote("O'Brien") == "O''Brien"
    assert _odata_quote("a'b'c") == "a''b''c"
    # Leading/trailing quotes are doubled too.
    assert _odata_quote("'x'") == "''x''"


def test_odata_quote_leaves_quote_free_values_untouched():
    for value in ("active", "High", "windows10", "user@example.com", ""):
        assert _odata_quote(value) == value


@pytest.mark.asyncio
async def test_list_incidents_escapes_assigned_to_quote(monkeypatch):
    """A ``assigned_to`` containing an apostrophe must reach the API with the
    quote doubled, keeping the ``$filter`` literal balanced."""
    recorder = _RequestRecorder()
    from app.integrations.providers import defender as df

    monkeypatch.setattr(df, "safe_request", recorder)
    monkeypatch.setattr(df, "_get_token", lambda *a, **k: _async_token())

    executor = CallableToolExecutor()
    build_tools(token="tenant||client||secret", executor=executor)

    result = await executor.execute_tool(
        ToolCall(
            id="call-1",
            name="list_defender_incidents",
            arguments={"assigned_to": "O'Brien"},
        )
    )

    assert not result.is_error
    assert recorder.calls[0]["params"]["$filter"] == "assignedTo eq 'O''Brien'"


@pytest.mark.asyncio
async def test_list_alerts_escapes_service_source_quote(monkeypatch):
    """A ``service_source`` carrying an apostrophe is escaped, and multiple
    filters are still joined with ``and``."""
    recorder = _RequestRecorder()
    from app.integrations.providers import defender as df

    monkeypatch.setattr(df, "safe_request", recorder)
    monkeypatch.setattr(df, "_get_token", lambda *a, **k: _async_token())

    executor = CallableToolExecutor()
    build_tools(token="tenant||client||secret", executor=executor)

    result = await executor.execute_tool(
        ToolCall(
            id="call-2",
            name="list_defender_alerts",
            arguments={"status": "new", "service_source": "Acme's EDR"},
        )
    )

    assert not result.is_error
    assert (
        recorder.calls[0]["params"]["$filter"]
        == "status eq 'new' and serviceSource eq 'Acme''s EDR'"
    )


async def _async_token() -> str:
    return "dummy-token"
