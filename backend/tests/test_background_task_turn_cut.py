"""A background task whose turn was cut never ends ``completed``.

A rollout takes a draining worker's executions back; the lost lease fires the
task's ``cancel_event`` and the agent loop answers the call in flight (a tool,
a sandbox container starting) with its cancellation: ``stop_reason ==
"cancelled"`` and the text "The request was cancelled.". That text is no
answer:

- on a stopping worker, the task is handed back like a wound-down turn,
  its checkpoint flushed, resumed by the next worker;
- with no stop of its worker, it fails with the cut's reason;
- a task the user killed meanwhile stays ``cancelled``;
- a turn that answered still completes.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import worker_shutdown
from app.llm.base import ChatMessage
from app.services import background_checkpoint, orchestration_reconciler, sub_agent
from app.services.background_checkpoint import TaskReplay
from app.services.turn_checkpoint import ResumedWork
from app.tools import agent_loop
from app.tools.base import ToolCall

CANCELLED_TEXT = "The request was cancelled."


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture(autouse=True)
def _quiet_worker(monkeypatch):
    """No stop asked of this worker unless a test says so."""
    monkeypatch.setattr(worker_shutdown, "_request", None)
    monkeypatch.setattr(orchestration_reconciler, "_draining", None)
    monkeypatch.setattr(orchestration_reconciler, "_stop_asked_at", None)
    monkeypatch.setattr(orchestration_reconciler, "_stop_signal", None)
    fake = _FakeRedis()

    async def _client():
        return fake

    monkeypatch.setattr(background_checkpoint, "_redis", _client)


def _stopping(signal: str = "SIGTERM") -> None:
    orchestration_reconciler.note_stop_signal(signal)


def _round(call_id: str, name: str) -> list[ChatMessage]:
    return [
        ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id=call_id, name=name, arguments={})]),
        ChatMessage(role="tool", content="ok", tool_call_id=call_id, name=name),
    ]


def _task_under_test(monkeypatch, turn):
    """A task whose turn is *turn*, with the collaborators the body reaches."""
    from tests.test_background_edition_gate import (
        ENTERPRISE,
        _patch_task_collaborators,
        _pin,
        _task_row,
        _TaskSession,
    )

    _pin(monkeypatch, **ENTERPRISE)
    monkeypatch.setattr("app.services.quota.quota_store_out", AsyncMock(return_value=False))
    monkeypatch.setattr(sub_agent, "credit_lost_attempts", AsyncMock())
    task = _task_row()
    target = SimpleNamespace(id=uuid.uuid4(), name="Developer", handover_agent_ids=[])
    monkeypatch.setattr("app.core.database.async_session", lambda: _TaskSession([task, target, task]))
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", AsyncMock(return_value=[]))
    collaborators = _patch_task_collaborators(monkeypatch)
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", AsyncMock(side_effect=turn(task)))
    rounds = _round("a", "code_interpreter")
    replay = TaskReplay(
        resumed=ResumedWork(messages=rounds, found=True),
        history=[*rounds, ChatMessage(role="assistant", content="[Resumed: ...]")],
        turn_id="attempt-1",
    )
    monkeypatch.setattr(background_checkpoint, "load_task_replay", AsyncMock(return_value=replay))
    monkeypatch.setattr(background_checkpoint, "discard_task_checkpoint", AsyncMock())
    return task, target, collaborators


def _cut(status: str | None = None):
    """A turn cut mid call: the loop's cancellation outcome, its apology as text."""

    def _for(task):
        async def _turn(*_args, **kwargs):
            kwargs["loop_outcome_out"]["stop_reason"] = "cancelled"
            if status is not None:
                task.status = status
            return CANCELLED_TEXT, None

        return _turn

    return _for


def _answered(task):
    async def _turn(*_args, **kwargs):
        kwargs["loop_outcome_out"]["stop_reason"] = "end_turn"
        return "The report is ready.", None

    return _turn


async def _run(task, target):
    return await sub_agent._execute_sub_agent_task(
        task_id=str(task.id),
        user_id=str(task.user_id),
        target_agent_id=str(target.id),
        task_description="Open the pull request",
        timeout_seconds=600,
        attempt=2,
    )


def _published(collaborators) -> list[str]:
    return [call.args[1] for call in collaborators.publish.await_args_list]


# ---------------------------------------------------------------------------
# The cut, on a stopping worker: handed back, never completed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("in_flight", ["a tool call", "a sandbox container start"])
async def test_a_turn_cut_mid_call_by_its_stopping_worker_is_handed_back(monkeypatch, in_flight):
    task, target, collaborators = _task_under_test(monkeypatch, _cut())
    _stopping()

    outcome = await _run(task, target)

    assert outcome.handed_back, f"cut during {in_flight}: resumed by the next worker"
    assert outcome.error == sub_agent.HANDED_BACK_ERROR
    assert task.status == "running", "still held by a live task until the next attempt resumes it"
    assert task.metadata_json[background_checkpoint.HANDED_BACK_KEY]["signal"] == "SIGTERM"
    assert getattr(task, "result", None) != CANCELLED_TEXT
    assert "completed" not in _published(collaborators), "no completion notice"
    collaborators.wake.assert_not_called()


async def test_a_drain_seen_with_no_signal_hands_the_cut_turn_back(monkeypatch):
    task, target, _collaborators = _task_under_test(monkeypatch, _cut())
    draining = asyncio.Event()
    draining.set()
    monkeypatch.setattr(orchestration_reconciler, "_draining", draining)

    outcome = await _run(task, target)

    assert outcome.handed_back
    assert orchestration_reconciler.worker_stopping() is True


# ---------------------------------------------------------------------------
# The cut, with no stop: failed with its reason
# ---------------------------------------------------------------------------


async def test_a_turn_cut_with_no_stop_fails_with_the_cut_reason(monkeypatch):
    task, target, collaborators = _task_under_test(monkeypatch, _cut())

    outcome = await _run(task, target)

    assert not outcome.succeeded and not outcome.handed_back
    assert outcome.error == sub_agent.TURN_CUT_ERROR
    assert task.status == "failed" and task.error == sub_agent.TURN_CUT_ERROR
    assert getattr(task, "result", None) != CANCELLED_TEXT
    assert _published(collaborators) == ["failed"]
    assert collaborators.wake.call_args.kwargs["terminal_status"] == "failed"
    assert background_checkpoint.HANDED_BACK_KEY not in (task.metadata_json or {})


async def test_a_task_killed_while_its_turn_was_cut_stays_cancelled(monkeypatch):
    task, target, collaborators = _task_under_test(monkeypatch, _cut(status="cancelled"))

    outcome = await _run(task, target)

    assert outcome.status == "cancelled"
    assert task.status == "cancelled"
    assert "completed" not in _published(collaborators)


async def test_a_killed_task_on_a_stopping_worker_is_not_handed_back(monkeypatch):
    task, target, _collaborators = _task_under_test(monkeypatch, _cut(status="cancelled"))
    _stopping()

    outcome = await _run(task, target)

    assert outcome.status == "cancelled"
    assert background_checkpoint.HANDED_BACK_KEY not in (task.metadata_json or {})


# ---------------------------------------------------------------------------
# The result path is unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stopping", [False, True])
async def test_a_turn_that_answered_still_completes(monkeypatch, stopping):
    task, target, collaborators = _task_under_test(monkeypatch, _answered)
    if stopping:
        _stopping()

    outcome = await _run(task, target)

    assert outcome.succeeded
    assert task.status == "completed" and task.result == "The report is ready."
    assert "completed" in _published(collaborators)


# ---------------------------------------------------------------------------
# Where the cut comes from: the lost lease's event reaches the call in flight
# ---------------------------------------------------------------------------


async def test_the_lost_lease_cuts_a_sandbox_start_in_flight():
    cancel_event = asyncio.Event()
    started = asyncio.Event()
    stopped: list[bool] = []

    async def _container_start():
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            stopped.append(True)
            raise

    async def _lease_lost():
        await started.wait()
        cancel_event.set()

    lost = asyncio.create_task(_lease_lost())
    with pytest.raises(agent_loop._CancelledMidCallError):
        await agent_loop._race_cancellation(_container_start(), cancel_event, timeout=60)
    await lost
    assert stopped == [True], "the start in flight is cancelled, and the loop answers with its cancellation"
    response = agent_loop._cancelled_chat_response("m", None)
    assert response.stop_reason == "cancelled" and response.content == CANCELLED_TEXT


def test_worker_stopping_reads_the_signal_and_the_drain():
    assert orchestration_reconciler.worker_stopping() is False
    orchestration_reconciler.note_stop_signal("SIGTERM")
    assert orchestration_reconciler.worker_stopping() is True
