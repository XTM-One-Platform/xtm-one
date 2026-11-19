# SPDX-License-Identifier: Apache-2.0
"""An empty expectation result names its case (xtm-one #454).

"No expectation results" used to say "either not executed yet, or no
expectations" even when the inject had run: the caller could not tell whether
to wait or to stop looking. The answers now read the inject status and say
which case applies.
"""

from __future__ import annotations

from app.integrations.providers.openaev import build_tools, expectations, injects, simulations
from app.integrations.providers.openaev._common import (
    _fmt_expectation_results_by_type,
    _inject_row_executed,
    _simulation_no_results_message,
)
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.openaev.io||test-token"

_EXECUTED = {
    "inject_id": "i-1",
    "inject_title": "Nmap SYN",
    "inject_status": {"status_name": "ERROR"},
    "inject_expectation_results": [],
}
_NOT_RUN = {"inject_id": "i-2", "inject_title": "Later", "inject_status": None}


class _Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class _Api:
    def __init__(self, results, rows, rows_err=None):
        self.calls: list[tuple[str, str]] = []
        self.results = results
        self.rows = rows
        self.rows_err = rows_err

    async def __call__(self, base_url, headers, method, path, action, **kw):
        self.calls.append((method, path))
        if path.endswith("/injects/results"):
            if self.rows_err:
                return None, self.rows_err
            return _Resp(self.rows), None
        return _Resp(self.results), None


async def _run(tool: str, arguments: dict) -> str:
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor)
    result = await executor.execute_tool(ToolCall(id="call-x", name=tool, arguments=arguments))
    assert not result.is_error, result.content
    return result.content


def test_inject_row_executed_reads_the_status() -> None:
    assert _inject_row_executed(_EXECUTED) is True
    assert _inject_row_executed(_NOT_RUN) is False
    assert _inject_row_executed({"inject_status": {"status_name": ""}}) is False
    assert _inject_row_executed({"inject_status": "SUCCESS"}) is True


def test_empty_results_name_the_case_when_known() -> None:
    executed = _fmt_expectation_results_by_type([], executed=True)
    assert "has executed and declares no expectations" in executed
    assert "Either" not in executed
    pending = _fmt_expectation_results_by_type([], executed=False)
    assert "has not executed" in pending
    assert "Either" in _fmt_expectation_results_by_type([])


def test_simulation_message_counts_each_case() -> None:
    text = _simulation_no_results_message("simulation x-1", [_EXECUTED, _NOT_RUN], None)
    assert text.startswith("No expectation results for simulation x-1.")
    assert "1 inject(s) executed and declare no expectations" in text
    assert "1 inject(s) not executed yet" in text
    assert "Nmap SYN (id: i-1): executed, no expectations" in text
    assert "Later (id: i-2): not executed" in text
    assert "has no inject" in _simulation_no_results_message("s", [], None)
    unread = _simulation_no_results_message("s", None, "OpenAEV answered 403.")
    assert "inject statuses not read: OpenAEV answered 403." in unread


async def test_simulation_results_read_inject_statuses_when_empty(monkeypatch) -> None:
    api = _Api([], [_EXECUTED])
    monkeypatch.setattr(simulations, "_api", api)
    content = await _run("manage_openaev_simulation", {"action": "results", "exercise_id": "x-1"})
    assert api.calls == [
        ("get", "/api/exercises/x-1/results"),
        ("get", "/api/exercises/x-1/injects/results"),
    ]
    assert "executed and declare no expectations" in content


async def test_expectation_simulation_results_same_answer(monkeypatch) -> None:
    api = _Api([], [_NOT_RUN])
    monkeypatch.setattr(expectations, "_api", api)
    content = await _run("manage_openaev_expectation", {"action": "simulation_results", "exercise_id": "x-1"})
    assert "not executed yet" in content


async def test_non_empty_results_make_one_call(monkeypatch) -> None:
    results = [{"type": "DETECTION", "avgResult": "SUCCESS", "distribution": [{"id": "SUCCESS", "value": 1}]}]
    api = _Api(results, [_EXECUTED])
    monkeypatch.setattr(simulations, "_api", api)
    content = await _run("manage_openaev_simulation", {"action": "results", "exercise_id": "x-1"})
    assert api.calls == [("get", "/api/exercises/x-1/results")]
    assert "Expectation results for simulation x-1" in content


async def test_inject_results_name_executed_with_no_expectations(monkeypatch) -> None:
    api = _Api(None, [_EXECUTED, _NOT_RUN])
    monkeypatch.setattr(injects, "_api", api)
    content = await _run("manage_openaev_inject", {"action": "results", "scope": "simulation", "parent_id": "x-1"})
    assert "has executed and declares no expectations" in content
    assert "has not executed" in content
