"""``manage_opencti_playbook`` action ``get`` lists each failed step of the
recent executions with its reason.

A failed AI agent step (the call to the agent errored or timed out) was only
counted: the agent read "recent executions: 1" and nothing told it the step
failed or why. The step's status and message are now shown, with the name of
the node that ran it, wrapped as untrusted data.

No network: ``_graphql`` is replaced by a stub answering the playbook read.
"""

import json

import app.integrations.providers.opencti.playbooks as playbooks_mod
from app.tools.base import CallableToolExecutor, ToolCall

_DEFINITION = json.dumps(
    {
        "nodes": [
            {"id": "entry", "name": "Listen", "component_id": "PLAYBOOK_INTERNAL_DATA_STREAM"},
            {"id": "agent-node", "name": "Summarise with AI", "component_id": "PLAYBOOK_AI_AGENT"},
        ],
        "links": [{"id": "l1", "from": {"id": "entry", "port": "out"}, "to": {"id": "agent-node"}}],
    }
)


def _stub(executions):
    async def _graphql(client, base_url, headers, query, variables=None):
        if "query Playbook" in query:
            return {
                "playbook": {
                    "id": "pb-1",
                    "name": "Triage",
                    "playbook_running": True,
                    "playbook_definition": _DEFINITION,
                    "queue_messages": 0,
                    "last_executions": executions,
                }
            }, None
        return {}, None

    return _graphql


async def _get(monkeypatch, executions) -> str:
    monkeypatch.setattr(playbooks_mod, "_graphql", _stub(executions))
    executor = CallableToolExecutor()
    playbooks_mod.register("https://opencti.test", {}, executor)
    result = await executor.execute_tool(
        ToolCall(id="c1", name="manage_opencti_playbook", arguments={"action": "get", "playbook_id": "pb-1"})
    )
    return result.content


async def test_a_failed_ai_agent_step_is_shown_with_its_reason(monkeypatch) -> None:
    content = await _get(
        monkeypatch,
        [
            {
                "id": "exec-1",
                "execution_start": "2026-11-21T05:41:00Z",
                "steps": [
                    {"id": "entry", "status": "success", "message": "ok", "duration": 3},
                    {
                        "id": "agent-node",
                        "status": "error",
                        "message": "AI agent call failed: other side closed",
                        "duration": 200000,
                    },
                ],
            }
        ],
    )

    assert "recent executions: 1" in content
    assert "2 step(s), 1 failed" in content
    assert "failed step Summarise with AI (agent-node), status error" in content
    assert "AI agent call failed: other side closed" in content


async def test_an_execution_without_failure_says_so(monkeypatch) -> None:
    content = await _get(
        monkeypatch,
        [{"id": "exec-2", "execution_start": "t", "steps": [{"id": "entry", "status": "success"}]}],
    )

    assert "1 step(s), no failed step" in content
    assert "failed step " not in content


async def test_a_long_step_message_is_bounded(monkeypatch) -> None:
    content = await _get(
        monkeypatch,
        [{"id": "exec-3", "steps": [{"id": "agent-node", "status": "failed", "message": "x" * 2000}]}],
    )

    assert "... (truncated)" in content
    assert "x" * 2000 not in content
