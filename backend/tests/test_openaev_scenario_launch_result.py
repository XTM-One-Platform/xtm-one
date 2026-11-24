# SPDX-License-Identifier: Apache-2.0
"""manage_openaev_scenario launch names the simulation it created.

OpenAEV's POST /api/scenarios/{id}/exercise/running answers {simulation_id}
since its launch-with-targets change (older builds answered the whole
exercise). The tool reads either shape and fetches the simulation for its
name and status, so a caller can chain on the launch.
"""

from __future__ import annotations

import pytest

from app.integrations.providers.openaev import build_tools, simulations
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.openaev.io||test-token"


class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def _api(launch_body, simulation=None, simulation_err=None):
    calls: list[tuple[str, str]] = []

    async def fake(base_url, headers, method, path, action, **kw):
        calls.append((method, path))
        if path.endswith("/exercise/running"):
            return _Resp(launch_body), None
        if path.startswith("/api/exercises/"):
            if simulation_err:
                return None, simulation_err
            return _Resp(simulation), None
        raise AssertionError(path)

    return fake, calls


async def _launch() -> str:
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor)
    result = await executor.execute_tool(
        ToolCall(id="c1", name="manage_openaev_scenario", arguments={"action": "launch", "scenario_id": "s1"})
    )
    assert not result.is_error, result.content
    return result.content


async def test_simulation_id_shape_is_read_and_the_simulation_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, calls = _api({"simulation_id": "sim-1"}, {"exercise_name": "Weekly run", "exercise_status": "RUNNING"})
    monkeypatch.setattr(simulations, "_api", fake)
    content = await _launch()
    assert "Weekly run (id: sim-1, status: RUNNING)" in content
    assert ("get", "/api/exercises/sim-1") in calls
    assert "None" not in content and "?" not in content


async def test_legacy_exercise_shape_needs_no_second_read(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, calls = _api({"exercise_id": "e1", "exercise_name": "Old", "exercise_status": "RUNNING"})
    monkeypatch.setattr(simulations, "_api", fake)
    content = await _launch()
    assert "Old (id: e1, status: RUNNING)" in content
    assert len(calls) == 1


async def test_failed_simulation_read_still_returns_the_id(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = _api({"simulation_id": "sim-2"}, simulation_err="OpenAEV error (HTTP 403): Forbidden")
    monkeypatch.setattr(simulations, "_api", fake)
    content = await _launch()
    assert "(name not read) (id: sim-2, status: not read)" in content


async def test_no_id_in_the_answer_is_said(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = _api({})
    monkeypatch.setattr(simulations, "_api", fake)
    content = await _launch()
    assert "returned no simulation id" in content
