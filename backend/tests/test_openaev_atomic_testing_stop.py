# SPDX-License-Identifier: Apache-2.0
"""Atomic testing: the tool says a running test cannot be stopped (xtm-one #339).

OpenAEV 3.x has no route that stops one atomic test execution. 'relaunch'
duplicates the test, queues the copy and deletes the original (the inject id
changes); 'delete' removes the queued jobs but not an execution an agent or
injector already picked up. The tool answers this instead of a generic
unknown action, and a relaunch names the new inject id.
"""

from __future__ import annotations

import pytest

from app.integrations.providers.openaev import build_tools, testing
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.openaev.io||test-token"


class _Resp:
    def __init__(self, body):
        self._body = body
        self.content = b"{}" if body is not None else b""

    def json(self):
        return self._body


class _Api:
    def __init__(self, body=None):
        self.calls: list[tuple[str, str]] = []
        self.body = body

    async def __call__(self, base_url, headers, method, path, action, **kw):
        self.calls.append((method, path))
        return _Resp(self.body), None


async def _run(arguments: dict) -> str:
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor)
    result = await executor.execute_tool(
        ToolCall(id="call-stop", name="manage_openaev_atomic_testing", arguments=arguments)
    )
    assert not result.is_error, result.content
    return result.content


@pytest.mark.parametrize("action", ["stop", "cancel", "abort"])
async def test_stop_answers_no_such_route_without_any_call(monkeypatch, action):
    api = _Api()
    monkeypatch.setattr(testing, "_api", api)
    content = await _run({"action": action, "inject_id": "a1"})
    assert api.calls == []
    assert f"no '{action}'" in content
    assert "delete" in content and "already picked up" in content


async def test_relaunch_names_the_new_inject_id(monkeypatch):
    api = _Api({"inject_id": "a2"})
    monkeypatch.setattr(testing, "_api", api)
    content = await _run({"action": "relaunch", "inject_id": "a1"})
    assert api.calls == [("post", "/api/atomic-testings/a1/relaunch")]
    assert "now inject a2" in content
    assert "NOT stopped" in content


async def test_relaunch_same_id_does_not_claim_a_move(monkeypatch):
    api = _Api({"inject_id": "a1"})
    monkeypatch.setattr(testing, "_api", api)
    content = await _run({"action": "relaunch", "inject_id": "a1"})
    assert "now inject" not in content
    assert content.startswith("Relaunched atomic test a1.")


async def test_launch_answer_is_unchanged(monkeypatch):
    api = _Api({"inject_id": "a1"})
    monkeypatch.setattr(testing, "_api", api)
    content = await _run({"action": "launch", "inject_id": "a1"})
    assert content.startswith("Launched atomic test a1.")
    assert "NOT stopped" not in content


async def test_delete_says_a_picked_up_execution_keeps_running(monkeypatch):
    api = _Api()
    monkeypatch.setattr(testing, "_api", api)
    content = await _run({"action": "delete", "inject_id": "a1"})
    assert api.calls == [("delete", "/api/atomic-testings/a1")]
    assert "NOT stopped" in content


def test_description_states_no_stop():
    executor = CallableToolExecutor()
    tools = {t.name: t for t in build_tools(TOKEN, executor)}
    description = tools["manage_openaev_atomic_testing"].description
    assert "cannot stop a running atomic test" in description
    assert "NEW inject id" in description
