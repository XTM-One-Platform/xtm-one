# SPDX-License-Identifier: Apache-2.0
"""manage_openaev_inject target_traces lists every target with its terminal trace.

The global status trace names only the targets an executor reached. A target
OpenAEV closed before the distribution (agentless asset, inactive agent, no
executor) has its own COMPLETE trace, served per target by
/api/injects/execution-traces: the read lists every target with that trace.
"""

from __future__ import annotations

import pytest

from app.integrations.providers.openaev import build_tools
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.openaev.io||test-token"

_ASSETS = [{"target_id": f"a{i}", "target_type": "ASSETS", "target_name": f"host-{i}"} for i in range(1, 5)]


class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _Api:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []

    async def __call__(self, base_url, headers, method, path, action, **kw):
        self.calls.append((method, path, kw.get("params")))
        if path.endswith("/targets/ASSETS/search"):
            return _Resp({"content": _ASSETS, "totalElements": 4}), None
        if "/targets/" in path:
            return _Resp({"content": [], "totalElements": 0}), None
        if path == "/api/injects/execution-traces":
            target = kw["params"]["targetId"]
            if target == "a4":
                return _Resp(
                    [
                        {
                            "execution_status": "ASSET_AGENTLESS",
                            "execution_action": "COMPLETE",
                            "execution_time": "2026-11-23T10:00:00Z",
                            "execution_message": "Asset has no agent",
                        }
                    ]
                ), None
            return _Resp(
                [
                    {
                        "execution_status": "INFO",
                        "execution_action": "START",
                        "execution_time": "2026-11-23T10:00:00Z",
                        "execution_message": "started",
                    },
                    {
                        "execution_status": "SUCCESS",
                        "execution_action": "COMPLETE",
                        "execution_time": "2026-11-23T10:01:00Z",
                        "execution_message": "done",
                    },
                ]
            ), None
        if path == "/api/injects/status":
            return _Resp({"status_name": "PENDING", "tracking_sent_date": "2026-11-23T16:28:01Z"}), None
        if path == "/api/injects/i1":
            return _Resp(
                {
                    "inject_id": "i1",
                    "inject_content": {
                        "expectations": [
                            {"expectation_type": "DETECTION", "expectation_expiration_time": 21600},
                            {"expectation_type": "PREVENTION", "expectation_expiration_time": 21600},
                        ]
                    },
                }
            ), None
        if path == "/api/settings":
            return _Resp({"runtime_settings": [{"key": "inject_execution_threshold_minutes", "value": "60"}]}), None
        raise AssertionError(path)


async def _run(arguments: dict) -> str:
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor)
    result = await executor.execute_tool(ToolCall(id="c1", name="manage_openaev_inject", arguments=arguments))
    assert not result.is_error, result.content
    return result.content


async def test_every_target_is_listed_with_its_terminal_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations.providers.openaev import injects

    api = _Api()
    monkeypatch.setattr(injects, "_api", api)
    content = await _run({"action": "target_traces", "scope": "global", "inject_id": "i1"})
    assert "4 target(s)" in content
    assert "ASSETS host-4 (id: a4): ASSET_AGENTLESS COMPLETE" in content
    assert "Asset has no agent" in content
    for i in range(1, 4):
        assert f"ASSETS host-{i} (id: a{i}): SUCCESS COMPLETE" in content
    trace_reads = [c for c in api.calls if c[1] == "/api/injects/execution-traces"]
    assert {c[2]["targetId"] for c in trace_reads} == {"a1", "a2", "a3", "a4"}
    assert all(c[2]["injectId"] == "i1" and c[2]["targetType"] == "ASSETS" for c in trace_reads)


async def test_execution_timeout_and_expectation_window_are_two_named_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run ends at distribution + threshold; the 6 h expectation expiry is a scoring window."""
    from app.integrations.providers.openaev import injects

    monkeypatch.setattr(injects, "_api", _Api())
    content = await _run({"action": "target_traces", "scope": "global", "inject_id": "i1"})
    timeout = next(line for line in content.splitlines() if line.startswith("execution_timeout:"))
    window = next(line for line in content.splitlines() if line.startswith("expectation_window:"))
    assert "60 min after distribution" in timeout
    assert "deadline 2026-11-23T17:28:01Z" in timeout
    assert "DETECTION 6 h (expires 2026-11-23T22:28:01Z)" in window
    assert "not the end of the execution" in window


def test_timeout_line_without_threshold_or_distribution() -> None:
    from app.integrations.providers.openaev.injects import _execution_timeout_line, _expectation_window_line

    line = _execution_timeout_line(None, None)
    assert "threshold not exposed" in line and "not distributed yet" in line
    assert "no expiration time" in _expectation_window_line({"inject_content": "{}"}, None)


async def test_target_traces_is_global_only() -> None:
    content = await _run({"action": "target_traces", "scope": "scenario", "parent_id": "s1", "inject_id": "i1"})
    assert "only available with scope='global'" in content


async def test_target_traces_needs_an_inject_id() -> None:
    content = await _run({"action": "target_traces", "scope": "global"})
    assert content == "inject_id is required."


def test_terminal_trace_prefers_the_last_complete() -> None:
    from app.integrations.providers.openaev.injects import _terminal_trace

    traces = [
        {"execution_action": "COMPLETE", "execution_time": "2026-11-23T10:00:00Z", "execution_status": "A"},
        {"execution_action": "EXECUTION", "execution_time": "2026-11-23T10:05:00Z", "execution_status": "B"},
    ]
    assert _terminal_trace(traces)["execution_status"] == "A"
    assert _terminal_trace([]) is None


def test_failure_cause_names_the_failing_step_of_an_error_complete() -> None:
    """ERROR + 'Payload completed': the cause is the earlier failing execution step."""
    from app.integrations.providers.openaev.injects import _cause_line, _failure_cause, _terminal_trace

    failing = {
        "execution_action": "COMMAND_EXECUTION",
        "execution_status": "ERROR",
        "execution_time": "2026-11-23T10:00:30Z",
        "execution_message": '{"stdout":"","stderr":"access denied","exit_code":5}',
    }
    traces = [
        {"execution_action": "START", "execution_status": "INFO", "execution_time": "2026-11-23T10:00:00Z"},
        failing,
        {
            "execution_action": "COMPLETE",
            "execution_status": "ERROR",
            "execution_time": "2026-11-23T10:01:00Z",
            "execution_message": "Payload completed",
        },
    ]
    terminal = _terminal_trace(traces)
    cause = _failure_cause(traces, terminal)
    assert cause is failing
    line = _cause_line(cause)
    assert "COMMAND_EXECUTION ERROR" in line and "access denied" in line and "exit_code" in line


def test_failure_cause_is_none_on_success_and_message_is_truncated() -> None:
    from app.integrations.providers.openaev.injects import _CAUSE_MESSAGE_CHARS, _cause_line, _failure_cause

    ok = {"execution_action": "COMPLETE", "execution_status": "SUCCESS"}
    assert _failure_cause([{"execution_action": "EXECUTION", "execution_status": "ERROR"}, ok], ok) is None
    line = _cause_line({"execution_action": "X", "execution_status": "ERROR", "execution_message": "a" * 2000})
    assert f"[{2000 - _CAUSE_MESSAGE_CHARS} more chars]" in line
