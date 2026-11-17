"""Regression tests for hunting-result table cell sanitization.

Defender XDR (``run_defender_hunting_query``), Sentinel
(``run_sentinel_kql_query``), and QRadar (AQL ``_cell``) all render query
results as a ``|``-delimited table with one row per record. Hunting columns
routinely contain embedded newlines/tabs (process command lines, file/registry
paths, raw payloads), which — if rendered verbatim — break the single-row
layout and misalign the whole table.

These tests assert that ``\r`` / ``\n`` / ``\t`` in a cell value are replaced
with spaces so each record stays on exactly one visual row.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.tools.base import CallableToolExecutor, ToolCall

_DIRTY = "cmd.exe\r\n\t/c whoami"
_CLEAN = "cmd.exe   /c whoami"


class _StubResponse:
    def __init__(self, payload: dict[str, Any]):
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


async def _dummy_acquirer(_scope: str) -> str:
    return "dummy-token"


# ---------------------------------------------------------------------------
# Defender XDR — runHuntingQuery (single POST)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_defender_hunting_strips_control_chars(monkeypatch):
    from app.integrations.providers import defender as df

    payload = {
        "schema": [{"name": "DeviceName"}, {"name": "ProcessCommandLine"}],
        "results": [{"DeviceName": "host1", "ProcessCommandLine": _DIRTY}],
    }

    async def _responder(_client, _method, _url, _provider, _op, **_kwargs):
        return _StubResponse(payload), None

    monkeypatch.setattr(df, "safe_request", _responder)

    executor = CallableToolExecutor()
    df.build_tools(
        token="tenant||client||secret",
        executor=executor,
        token_acquirer=_dummy_acquirer,
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="run_defender_hunting_query",
            arguments={"query": "DeviceProcessEvents | take 1"},
        )
    )

    assert not result.is_error
    # The dirty command line must be sanitized into a single visual cell.
    assert _CLEAN in result.content
    assert _DIRTY not in result.content
    # The data row must not introduce extra physical lines from the cell value.
    data_lines = [ln for ln in result.content.splitlines() if "cmd.exe" in ln]
    assert len(data_lines) == 1


# ---------------------------------------------------------------------------
# Sentinel — Log Analytics query (workspace-id GET, then query POST)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sentinel_kql_strips_control_chars(monkeypatch):
    from app.integrations.providers import sentinel as sn

    query_payload = {
        "tables": [
            {
                "columns": [{"name": "Computer"}, {"name": "CommandLine"}],
                "rows": [["host1", _DIRTY]],
            }
        ]
    }

    calls = {"n": 0}

    async def _responder(_client, _method, _url, _provider, _op, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # First call resolves the Log Analytics workspace customerId.
            return _StubResponse({"properties": {"customerId": "ws-guid"}}), None
        return _StubResponse(query_payload), None

    monkeypatch.setattr(sn, "safe_request", _responder)

    executor = CallableToolExecutor()
    sn.build_tools(
        token="sub||rg||workspace",
        executor=executor,
        token_acquirer=_dummy_acquirer,
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="run_sentinel_kql_query",
            arguments={"query": "SecurityEvent | take 1"},
        )
    )

    assert not result.is_error
    assert _CLEAN in result.content
    assert _DIRTY not in result.content
    data_lines = [ln for ln in result.content.splitlines() if "cmd.exe" in ln]
    assert len(data_lines) == 1


# ---------------------------------------------------------------------------
# QRadar — AQL result cell helper (module-level)
# ---------------------------------------------------------------------------


def test_qradar_cell_strips_control_chars_and_handles_none():
    from app.integrations.providers.qradar import _cell

    assert _cell(_DIRTY) == _CLEAN
    assert _cell(None) == "null"
    assert "\n" not in _cell("a\nb")
    assert "\t" not in _cell("a\tb")
    assert "\r" not in _cell("a\rb")
