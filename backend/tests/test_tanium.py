"""Tests for the Tanium integration provider.

Covers tool introspection (27 tools / empty on missing creds), the question
result-set cell sanitization (embedded CR/LF/TAB must not break the
pipe-delimited table), the `create_tanium_intel_doc` request headers (must send
`Accept: application/json` since it parses the JSON response), and the
user-facing-URL exclusion (Tanium's configured base_url is an API host, not the
console UI host). API calls route through the shared `safe_request` helper,
monkeypatched here so no network is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import tanium as t
from app.integrations.providers.tanium import _cell, _format_question_results, build_tools
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://acme-api.cloud.tanium.com||api-token-xyz"


class _StubResponse:
    def __init__(self, payload: Any):
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _Recorder:
    def __init__(self, payload: Any):
        self._payload = payload
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        return _StubResponse(self._payload), None


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


def test_build_tools_returns_27_tools():
    executor = CallableToolExecutor()
    tools = build_tools(token=_TOKEN, executor=executor)
    names = {tool.name for tool in tools}
    assert len(tools) == 27
    assert "download_tanium_file_evidence" in names
    assert "list_tanium_alerts" in names
    assert "ask_tanium_question" in names
    assert "deploy_tanium_action" in names
    assert "run_tanium_graphql" in names


def test_build_tools_empty_without_creds():
    executor = CallableToolExecutor()
    assert build_tools(token="", executor=executor) == []
    assert build_tools(token="https://acme.example||", executor=executor) == []


# ---------------------------------------------------------------------------
# Question result cell sanitization
# ---------------------------------------------------------------------------


def test_cell_strips_control_chars():
    assert _cell("cmd.exe\r\n\t/c whoami") == "cmd.exe   /c whoami"
    assert "\n" not in _cell("a\nb")
    assert "\t" not in _cell("a\tb")


def test_cell_renders_none_as_null():
    # Align with the qradar/sentinel/defender hunting-table renderers, which emit
    # the literal "null" for a missing value rather than the Python str "None".
    assert _cell(None) == "null"


def test_format_question_results_keeps_rows_single_line():
    payload = {
        "data": {
            "result_sets": [
                {
                    "columns": [{"name": "Computer Name"}, {"name": "Process"}],
                    "rows": [
                        {"data": [[{"text": "HOST-1"}], [{"text": "cmd.exe\r\n/c x"}]]}
                    ],
                    "estimated_total": 1,
                    "mr_tested": 1,
                }
            ]
        }
    }
    out = _format_question_results(payload)
    # The process command line with an embedded newline must not split the row.
    data_lines = [ln for ln in out.splitlines() if "cmd.exe" in ln]
    assert len(data_lines) == 1
    assert "cmd.exe   /c x" in out or "cmd.exe  /c x" in out


# ---------------------------------------------------------------------------
# create_tanium_intel_doc request headers (Accept: application/json)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_intel_doc_sends_accept_json(monkeypatch):
    recorder = _Recorder({"data": {"id": 7, "name": "rule"}})
    monkeypatch.setattr(t, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)

    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="create_tanium_intel_doc",
            arguments={"contents": "rule x {}", "intel_type": "yara", "name": "rule"},
        )
    )
    assert not result.is_error
    headers = recorder.calls[0]["headers"]
    assert headers["Accept"] == "application/json"
    assert headers["Content-Type"] == "application/x-yara"


# ---------------------------------------------------------------------------
# Pagination lower-bound clamps: a non-positive limit/first must never reach the
# Tanium API (limit=0/negative) nor corrupt a client-side slice.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_alerts_clamps_nonpositive_limit(monkeypatch):
    recorder = _Recorder({"data": [], "meta": {}})
    monkeypatch.setattr(t, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)

    result = await executor.execute_tool(
        ToolCall(id="c1", name="list_tanium_alerts", arguments={"limit": 0})
    )
    assert not result.is_error
    assert recorder.calls[0]["params"]["limit"] == 1


@pytest.mark.asyncio
async def test_list_actions_tail_clamps_nonpositive_limit(monkeypatch):
    # `actions[-limit:]` with limit<=0 would silently fall back to the whole list
    # (actions[-0:] == actions[:]); the clamp keeps it to the most-recent action.
    recorder = _Recorder(
        {"data": [{"id": 1, "name": "A1"}, {"id": 2, "name": "A2"}, {"id": 3, "name": "A3"}]}
    )
    monkeypatch.setattr(t, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)

    result = await executor.execute_tool(
        ToolCall(id="c1", name="list_tanium_actions", arguments={"limit": 0})
    )
    assert not result.is_error
    assert "Found 1 action" in result.content
    assert "A3" in result.content
    assert "A1" not in result.content


# ---------------------------------------------------------------------------
# User-facing-URL exclusion: Tanium base_url is the API host, not the console UI
# ---------------------------------------------------------------------------


def test_tanium_excluded_from_user_facing_url_providers():
    from app.services.integration_tools import _USER_FACING_BASE_URL_PROVIDERS

    assert "tanium" not in _USER_FACING_BASE_URL_PROVIDERS
