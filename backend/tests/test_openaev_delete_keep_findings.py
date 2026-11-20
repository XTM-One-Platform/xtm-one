# SPDX-License-Identifier: Apache-2.0
"""OpenAEV deletes keep findings by default (keep_findings, OpenAEV #9414, #9550).

OpenAEV deletes the findings of a simulation, scenario, inject or atomic test with it unless
the DELETE carries keep_findings=true. The tools send keep_findings=true by
default so an agent's clean-up never destroys evidence silently, send false
when asked, and say in the answer what happened to the findings.
"""

from __future__ import annotations

import pytest

from app.integrations.providers.openaev import build_tools, core, injects, simulations, testing
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.openaev.io||test-token"


class _Resp:
    content = b""

    def json(self):
        return None


class _Api:
    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []

    async def __call__(self, base_url, headers, method, path, action, **kw):
        self.calls.append((method, path, kw.get("params")))
        return _Resp(), None


async def _run(name: str, arguments: dict) -> tuple[str, list[dict]]:
    executor = CallableToolExecutor()
    tools = build_tools(TOKEN, executor)
    result = await executor.execute_tool(ToolCall(id="call-del", name=name, arguments=arguments))
    assert not result.is_error, result.content
    return result.content, tools


def _schema(tools, name: str) -> dict:
    tool = next(t for t in tools if t.name == name)
    return tool.input_schema["properties"]["keep_findings"]


@pytest.mark.parametrize(
    ("arguments", "expected", "kept"),
    [({}, "true", True), ({"keep_findings": True}, "true", True), ({"keep_findings": False}, "false", False)],
)
async def test_simulation_delete_sends_keep_findings(monkeypatch, arguments, expected, kept):
    api = _Api()
    monkeypatch.setattr(simulations, "_api", api)
    content, tools = await _run("manage_openaev_simulation", {"action": "delete", "exercise_id": "e1", **arguments})
    assert api.calls == [("delete", "/api/exercises/e1", {"keep_findings": expected})]
    assert content.startswith("Deleted simulation e1.")
    if kept:
        assert "detached" in content and "ignores it" in content
    else:
        assert "findings were deleted with the simulation" in content
    assert _schema(tools, "manage_openaev_simulation")["default"] is True


@pytest.mark.parametrize(("arguments", "expected"), [({}, "true"), ({"keep_findings": False}, "false")])
async def test_inject_delete_sends_keep_findings(monkeypatch, arguments, expected):
    api = _Api()
    monkeypatch.setattr(injects, "_api", api)
    content, tools = await _run(
        "manage_openaev_inject",
        {"action": "delete", "scope": "simulation", "parent_id": "e1", "inject_id": "i1", **arguments},
    )
    assert api.calls == [("delete", "/api/exercises/e1/injects/i1", {"keep_findings": expected})]
    assert content.startswith("Deleted inject i1.")
    assert ("deleted with the inject" in content) is (expected == "false")
    assert _schema(tools, "manage_openaev_inject")["type"] == "boolean"


@pytest.mark.parametrize(("arguments", "expected"), [({}, "true"), ({"keep_findings": False}, "false")])
async def test_scenario_delete_sends_keep_findings(monkeypatch, arguments, expected):
    calls: list[tuple[str, str, dict | None]] = []

    async def fake_request(client, method, url, provider, action, **kw):
        calls.append((method, url, kw.get("params")))
        return _Resp(), None

    monkeypatch.setattr(core, "scoped_safe_request", fake_request)
    content, tools = await _run("delete_openaev_scenario", {"scenario_id": "s1", **arguments})
    assert calls == [("delete", "https://demo.openaev.io/api/scenarios/s1", {"keep_findings": expected})]
    assert content.startswith("Scenario s1 deleted.")
    assert ("deleted with the scenario" in content) is (expected == "false")
    assert _schema(tools, "delete_openaev_scenario")["default"] is True


@pytest.mark.parametrize(
    ("arguments", "expected"), [({}, "true"), ({"keep_findings": True}, "true"), ({"keep_findings": False}, "false")]
)
async def test_atomic_testing_delete_sends_keep_findings(monkeypatch, arguments, expected):
    api = _Api()
    monkeypatch.setattr(testing, "_api", api)
    content, tools = await _run("manage_openaev_atomic_testing", {"action": "delete", "inject_id": "a1", **arguments})
    assert api.calls == [("delete", "/api/atomic-testings/a1", {"keep_findings": expected})]
    assert content.startswith("Deleted atomic test a1.")
    if expected == "true":
        assert "detached" in content and "ignores it" in content
    else:
        assert "findings were deleted with the atomic test" in content
    assert _schema(tools, "manage_openaev_atomic_testing")["default"] is True


async def test_schemas_do_not_share_one_dict():
    executor = CallableToolExecutor()
    tools = build_tools(TOKEN, executor)
    schemas = [
        _schema(tools, name)
        for name in (
            "manage_openaev_simulation",
            "manage_openaev_inject",
            "delete_openaev_scenario",
            "manage_openaev_atomic_testing",
        )
    ]
    assert len({id(s) for s in schemas}) == 4
