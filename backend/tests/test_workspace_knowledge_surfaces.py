"""Workspace knowledge reaches the surfaces with no conversation of their own.

Spec 032 FR-027: a delegated task works in the workspace of the conversation
that delegated it, and a flow's agent node in the workspace a wired
``capability.workspace`` node names. Asserted on the call into
``run_agent_chat`` (a delegated task) and on the walker's own method (a flow),
with the harnesses the neighbouring suites use.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.flow_runner import FlowWalker
from tests.agentic_gate_stub import stub_agentic_gate
from tests.test_sub_agent_result_files import (
    _exec_db,
    _FakeSessionCM,
    _incremental_writer,
    _task_and_agent,
)


@pytest.fixture(autouse=True)
def _agentic_ai_available(monkeypatch):
    """The task body asks the background gate first; these tests are about
    what it hands the turn once it may run."""
    stub_agentic_gate(monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace_id", [uuid.uuid4(), None])
async def test_a_delegated_task_runs_in_the_delegating_conversations_workspace(workspace_id):
    from app.services.sub_agent import _execute_sub_agent_task

    task, agent = _task_and_agent()
    task.conversation_id = uuid.uuid4()
    db = _exec_db(task, agent)
    run = AsyncMock(return_value=("done", None))
    lookup = AsyncMock(return_value=workspace_id)

    with (
        patch("app.core.database.async_session", lambda: _FakeSessionCM(db)),
        patch("app.services.agent_chat.run_agent_chat", new=run),
        patch("app.services.workspace_knowledge.load_conversation_workspace_id", new=lookup),
        patch("app.services.sub_agent._update_task_delegation", new=AsyncMock()),
        patch("app.services.sub_agent_files.write_task_result_file_ids", new=_incremental_writer()),
    ):
        await _execute_sub_agent_task(
            task_id=str(uuid.uuid4()),
            user_id=str(uuid.uuid4()),
            target_agent_id=str(uuid.uuid4()),
            task_description="summarise the brief",
            file_context=None,
        )

    lookup.assert_awaited_once_with(db, str(task.conversation_id))
    assert run.await_args.kwargs["session_workspace_id"] == workspace_id


def _walker(edges: list[dict], config: dict | None = None) -> FlowWalker:
    walker = FlowWalker.__new__(FlowWalker)
    walker.graph = {
        "nodes": [
            {"id": "agent-1", "kind": "agent"},
            {"id": "ws-1", "kind": "capability.workspace", "config": config or {}},
            {"id": "ss-1", "kind": "capability.shared_state", "config": {}},
        ],
        "edges": edges,
    }
    walker.nodes = {n["id"]: n for n in walker.graph["nodes"]}
    walker.run = SimpleNamespace(id=uuid.uuid4(), flow_id=uuid.uuid4())
    return walker


def _edge(source: str, handle: str) -> dict:
    return {"id": f"{source}-{handle}", "source": source, "target": "agent-1", "target_handle": handle}


def test_a_wired_workspace_node_names_its_workspace():
    workspace_id = str(uuid.uuid4())
    walker = _walker([_edge("ws-1", "workspace")], {"workspace_id": workspace_id})
    assert walker._wired_workspace_ref("agent-1") == workspace_id


def test_no_wired_workspace_node_means_no_workspace():
    walker = _walker([_edge("ss-1", "shared_state")])
    assert walker._wired_workspace_ref("agent-1") is None


def test_an_empty_wired_node_is_refused_not_skipped():
    """``""`` reaches ``resolve_workspace_scope``, which refuses it ("missing"):
    an agent wired to a workspace never runs without it."""
    walker = _walker([_edge("ws-1", "workspace")], {})
    assert walker._wired_workspace_ref("agent-1") == ""


def test_another_kind_on_the_workspace_handle_is_ignored():
    walker = _walker([_edge("ss-1", "workspace")])
    assert walker._wired_workspace_ref("agent-1") is None
