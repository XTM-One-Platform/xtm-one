"""Tests for the IBM QRadar integration provider.

Covers tool introspection, the UTC timestamp rendering (trailing ``Z`` must be
retained), the paginated ``Range`` header clamp, and URL-encoding of reference
set names / IOC values in request paths (reserved characters like spaces and
``/`` must not break or mis-target the request). API calls route through the
shared ``safe_request`` helper, monkeypatched here so no network is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import qradar as q
from app.integrations.providers.qradar import _ts, build_tools
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://qradar.example.com||sec-token-123||20.0"


class _StubResponse:
    def __init__(self, payload: Any):
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _Recorder:
    """Fake ``safe_request`` that records calls and returns a fixed payload."""

    def __init__(self, payload: Any):
        self._payload = payload
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        return _StubResponse(self._payload), None


def _build(monkeypatch, payload: Any) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(payload)
    monkeypatch.setattr(q, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


def test_build_tools_returns_19_tools():
    executor = CallableToolExecutor()
    tools = build_tools(token=_TOKEN, executor=executor)
    names = {t.name for t in tools}
    assert len(tools) == 19
    assert "list_qradar_offenses" in names
    assert "run_qradar_aql_query" in names
    assert "delete_qradar_reference_set_value" in names


def test_build_tools_empty_without_creds():
    executor = CallableToolExecutor()
    assert build_tools(token="", executor=executor) == []
    assert build_tools(token="https://q.example.com||", executor=executor) == []


# ---------------------------------------------------------------------------
# Timestamp rendering (Finding A — trailing Z must be retained)
# ---------------------------------------------------------------------------


def test_ts_retains_utc_z_marker():
    # 2023-11-14T22:13:20Z
    assert _ts(1700000000000) == "2023-11-14T22:13:20Z"
    assert _ts(0) == ""
    assert _ts(None) == ""
    # Non-numeric falls back to the raw string rather than raising.
    assert _ts("garbage") == "garbage"


# ---------------------------------------------------------------------------
# Range clamp (Finding B — unbounded limit must be capped)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_range_header_clamps_excessive_limit(monkeypatch):
    executor, recorder = _build(monkeypatch, [])
    await executor.execute_tool(
        ToolCall(
            id="c1",
            name="list_qradar_offenses",
            arguments={"limit": 10_000_000},
        )
    )
    rng = recorder.calls[0]["headers"]["Range"]
    # items=0-999 (clamped to _MAX_RANGE=1000 items), never items=0-9999999.
    assert rng == "items=0-999"


# ---------------------------------------------------------------------------
# URL-encoding of reference set name + value (Findings C / D)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_reference_set_url_encodes_name(monkeypatch):
    executor, recorder = _build(monkeypatch, {"name": "Bad Hosts", "data": []})
    await executor.execute_tool(
        ToolCall(
            id="c1",
            name="get_qradar_reference_set",
            arguments={"name": "Bad Hosts"},
        )
    )
    url = recorder.calls[0]["url"]
    assert url.endswith("/reference_data/sets/Bad%20Hosts")
    assert " " not in url


@pytest.mark.asyncio
async def test_delete_reference_set_value_url_encodes_name_and_value(monkeypatch):
    executor, recorder = _build(monkeypatch, {})
    await executor.execute_tool(
        ToolCall(
            id="c1",
            name="delete_qradar_reference_set_value",
            arguments={"name": "Bad Hosts", "value": "http://evil.test/path"},
        )
    )
    url = recorder.calls[0]["url"]
    # Both the set name (space) and the IOC value (slashes) must be encoded so
    # the DELETE targets the intended entry instead of a wrong/!malformed path.
    assert url.endswith(
        "/reference_data/sets/Bad%20Hosts/http%3A%2F%2Fevil.test%2Fpath"
    )
    # The raw, unencoded IOC value must never appear in the path.
    assert "evil.test/path" not in url
