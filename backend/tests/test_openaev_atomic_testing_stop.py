# SPDX-License-Identifier: Apache-2.0
"""Atomic testing: 'stop' calls OpenAEV's stop route (xtm-one #339, #896).

OpenAEV #9444 added POST /api/atomic-testings/{id}/stop: it withdraws the
agent jobs not picked up yet and ends the test in ERROR; a command an agent
already started is not interrupted. 'cancel' and 'abort' map to it. 'relaunch'
duplicates the test, queues the copy and deletes the original (the inject id
changes), and a relaunch names the new inject id.
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
    def __init__(self, body=None, err=None):
        self.calls: list[tuple[str, str]] = []
        self.body = body
        self.err = err

    async def __call__(self, base_url, headers, method, path, action, **kw):
        self.calls.append((method, path))
        if self.err:
            return None, self.err
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
async def test_stop_posts_the_stop_route(monkeypatch, action):
    api = _Api({"inject_id": "a1"})
    monkeypatch.setattr(testing, "_api", api)
    content = await _run({"action": action, "inject_id": "a1"})
    assert api.calls == [("post", "/api/atomic-testings/a1/stop")]
    assert content.startswith("Stopped atomic test a1:")
    assert "NOT interrupted" in content


async def test_stop_needs_an_inject_id(monkeypatch):
    api = _Api()
    monkeypatch.setattr(testing, "_api", api)
    content = await _run({"action": "stop"})
    assert api.calls == []
    assert "inject_id is required for action 'stop'" in content


async def test_stop_of_a_test_not_running_keeps_openaev_reason(monkeypatch):
    api = _Api(err="OpenAEV error 400: The atomic testing is not running (status SUCCESS)")
    monkeypatch.setattr(testing, "_api", api)
    content = await _run({"action": "stop", "inject_id": "a1"})
    assert "not running (status SUCCESS)" in content
    assert "queuing, executing or pending" in content


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


def test_description_and_schema_offer_stop():
    executor = CallableToolExecutor()
    tools = {t.name: t for t in build_tools(TOKEN, executor)}
    tool = tools["manage_openaev_atomic_testing"]
    assert "cannot stop" not in tool.description
    assert "'stop' (inject_id)" in tool.description
    assert "NEW inject id" in tool.description
    assert "stop" in tool.input_schema["properties"]["action"]["enum"]
