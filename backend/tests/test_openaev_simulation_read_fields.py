# SPDX-License-Identifier: Apache-2.0
"""Simulation reads name the parent scenario, the dates and inject statuses (xtm-one #345).

A simulation a scenario recurrence created is only provably that when its
read names the scenario id, its start date and the status of each inject.
"""

from __future__ import annotations

from app.integrations.providers.openaev import build_tools, simulations
from app.integrations.providers.openaev._common import (
    SIMULATION_INJECT_ROWS,
    _simulation_inject_status_lines,
    _simulation_state_lines,
)
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.openaev.io||test-token"

_EXERCISE = {
    "exercise_id": "x-1",
    "exercise_name": "Nightly scan",
    "exercise_status": "FINISHED",
    "exercise_scenario": "s-1",
    "exercise_start_date": "2026-11-19T02:00:00Z",
    "exercise_end_date": "2026-11-19T02:20:00Z",
    "exercise_created_at": "2026-11-19T01:59:55Z",
    "exercise_updated_at": "2026-11-19T02:20:32Z",
    "exercise_injects": ["i-1"],
}

_ROWS = [
    {"inject_id": "i-1", "inject_title": "Nmap SYN", "inject_status": {"status_name": "ERROR"}},
    {"inject_id": "i-2", "inject_title": "Not yet", "inject_status": None},
]


class _Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class _Api:
    def __init__(self, rows_err=None):
        self.calls: list[tuple[str, str]] = []
        self.rows_err = rows_err

    async def __call__(self, base_url, headers, method, path, action, **kw):
        self.calls.append((method, path))
        if path.endswith("/injects/results"):
            if self.rows_err:
                return None, self.rows_err
            return _Resp(_ROWS), None
        if path.endswith("/search"):
            return _Resp({"content": [_EXERCISE]}), None
        return _Resp(_EXERCISE), None


async def _run(arguments: dict) -> str:
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor)
    result = await executor.execute_tool(ToolCall(id="call-sim", name="manage_openaev_simulation", arguments=arguments))
    assert not result.is_error, result.content
    return result.content


def test_state_lines_name_scenario_dates_and_inject_count() -> None:
    lines = _simulation_state_lines(_EXERCISE)
    assert "  Scenario: s-1" in lines
    assert "  Start: 2026-11-19T02:00:00Z" in lines
    assert "  End: 2026-11-19T02:20:00Z" in lines
    assert "  Created: 2026-11-19T01:59:55Z" in lines
    assert "  Updated: 2026-11-19T02:20:32Z" in lines
    assert "  Injects: 1" in lines


def test_scenario_as_object_and_minimal_payload() -> None:
    assert _simulation_state_lines({"exercise_scenario": {"scenario_id": "s-9"}}) == ["  Scenario: s-9"]
    assert _simulation_state_lines({"exercise_id": "x", "exercise_name": "Bare"}) == []


def test_inject_status_lines_bounded_and_not_executed() -> None:
    lines = _simulation_inject_status_lines(_ROWS)
    assert lines[0] == "  Inject statuses (2):"
    assert "    Nmap SYN (id: i-1): ERROR" in lines
    assert "    Not yet (id: i-2): not executed" in lines
    many = [{"inject_id": f"i-{n}", "inject_title": "t"} for n in range(SIMULATION_INJECT_ROWS + 3)]
    long = _simulation_inject_status_lines(many)
    assert len(long) == SIMULATION_INJECT_ROWS + 2
    assert long[-1].startswith("    3 more not shown")
    assert _simulation_inject_status_lines({"not": "a list"}) == []


async def test_get_reads_inject_statuses(monkeypatch) -> None:
    api = _Api()
    monkeypatch.setattr(simulations, "_api", api)
    content = await _run({"action": "get", "exercise_id": "x-1"})
    assert api.calls == [
        ("get", "/api/exercises/x-1"),
        ("get", "/api/exercises/x-1/injects/results"),
    ]
    assert "Scenario: s-1" in content
    assert "Start: 2026-11-19T02:00:00Z" in content
    assert "Nmap SYN (id: i-1): ERROR" in content


async def test_get_keeps_the_simulation_when_statuses_fail(monkeypatch) -> None:
    api = _Api(rows_err="OpenAEV answered 403.")
    monkeypatch.setattr(simulations, "_api", api)
    content = await _run({"action": "get", "exercise_id": "x-1"})
    assert "Scenario: s-1" in content
    assert "Inject statuses: not read (OpenAEV answered 403.)" in content


async def test_list_names_scenario_and_dates_per_row(monkeypatch) -> None:
    api = _Api()
    monkeypatch.setattr(simulations, "_api", api)
    content = await _run({"action": "list"})
    assert api.calls == [("post", "/api/exercises/search")]
    assert "Scenario: s-1" in content
    assert "Start: 2026-11-19T02:00:00Z" in content
