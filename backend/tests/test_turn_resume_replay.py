"""An interrupted turn resumes with every completed result, bounded per result.

The checkpoint bounded a turn's work by dropping whole rounds, oldest
first, past 1.5M characters: one round larger than that (a code cell carrying
twenty pasted repository files) left NOTHING to resume, and the next turn read
only the names of the calls. Each result is now bounded on its own - cut for
the replay, kept whole as a conversation file when the turn stops cleanly - and
the resuming turn reads a note of facts. On a graceful stop the loop winds
down at an iteration boundary, so the call in flight is in the checkpoint.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.core import worker_shutdown
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.services import turn_checkpoint
from app.services.turn_checkpoint import (
    MAX_REPLAYED_RESULT_CHARS,
    REPLAY_HEAD_CHARS,
    ResumedWork,
    TurnCheckpoint,
    bounded,
    completed_work,
    resume_note,
)
from app.tools.agent_loop import run_agent_loop
from app.tools.base import ToolCall, ToolDefinition, ToolResult

CONV = uuid.UUID("6a4e0b4c-1111-4222-8333-444455556666")


def _round(call_id: str, result: str, *, code: str = "print(1)") -> list[ChatMessage]:
    return [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id=call_id, name="code_interpreter", arguments={"code": code})],
        ),
        ChatMessage(role="tool", content=result, tool_call_id=call_id, name="code_interpreter"),
    ]


class _Writes:
    """A TurnCheckpoint whose writes are captured instead of stored."""

    def __init__(self, checkpoint: TurnCheckpoint) -> None:
        self.entries: list[list[dict]] = []

        async def _write(entries: list[dict]) -> None:
            self.entries.append(entries)

        checkpoint._write = _write  # type: ignore[method-assign]


async def test_one_oversized_round_no_longer_pushes_every_round_out():
    giant = "x" * (turn_checkpoint.MAX_CHECKPOINT_CHARS + 10)
    messages = [*_round("a", "small result a"), *_round("b", giant, code="y" * 600_000), *_round("c", "small c")]
    checkpoint = TurnCheckpoint(CONV, turn_id="t1")
    writes = _Writes(checkpoint)
    checkpoint.offer(messages)
    await checkpoint._task
    # ``bounded`` alone drops rounds to fit the giant one; the checkpoint keeps all six.
    assert len(bounded(completed_work(messages))) < 6
    (stored,) = writes.entries
    assert [e.get("tool_call_id") for e in stored] == [None, "a", None, "b", None, "c"]
    cut = stored[3]
    assert len(cut["content"]) < REPLAY_HEAD_CHARS + 1_000
    assert "the rest was not kept" in cut["content"]
    assert cut["spill"]["chars"] == len(giant) and cut["spill"]["file_id"] is None
    # The giant code cell is cut too, as a valid string argument.
    code = stored[2]["tool_calls"][0]["arguments"]["code"]
    assert len(code) < 30_000 and "cut for the replay" in code


async def test_a_flushing_close_keeps_the_whole_result_as_a_conversation_file(monkeypatch):
    kept: list[dict] = []

    async def _persist(**kwargs):
        kept.append(kwargs)
        return SimpleNamespace(id=uuid.UUID(int=len(kept)))

    monkeypatch.setattr("app.services.builtin_tools._shared._persist_generated_file", _persist)
    big = "line\n" * (MAX_REPLAYED_RESULT_CHARS // 4)
    checkpoint = TurnCheckpoint(CONV, turn_id="t1", user_id=str(uuid.uuid4()))
    writes = _Writes(checkpoint)
    checkpoint.offer(_round("a", big))
    await checkpoint.aclose(flush=True)
    assert len(kept) == 1
    assert kept[0]["raw_bytes"] == big.encode("utf-8")
    assert kept[0]["source"] == turn_checkpoint.SPILL_SOURCE
    assert kept[0]["conversation_id"] == str(CONV)
    final = writes.entries[-1][1]
    file_id = str(uuid.UUID(int=1))
    assert final["spill"]["file_id"] == file_id
    assert f'read_file_content(file_id="{file_id}")' in final["content"]
    # A second flush of the same work stores no second file.
    again = TurnCheckpoint(CONV, turn_id="t2", user_id="u", resumed=ResumedWork(
        messages=[], entries=final and [writes.entries[-1][0], final], found=True,
        spilled=[{"call_id": "a", **final["spill"]}],
    ))
    _Writes(again)
    again.offer(_round("a", big))
    await again.aclose(flush=True)
    assert len(kept) == 1


async def test_a_normal_end_creates_no_file(monkeypatch):
    async def _never(**kwargs):
        raise AssertionError("a turn that stores its answer keeps no file")

    monkeypatch.setattr("app.services.builtin_tools._shared._persist_generated_file", _never)
    checkpoint = TurnCheckpoint(CONV, turn_id="t1", user_id="u")
    writes = _Writes(checkpoint)
    checkpoint.offer(_round("a", "z" * (MAX_REPLAYED_RESULT_CHARS + 1)))
    await checkpoint._task
    await checkpoint.aclose(flush=False)
    assert writes.entries and "the rest was not kept" in writes.entries[0][1]["content"]


def test_the_resume_note_states_the_facts():
    messages = [*_round("a", "result a"), *_round("b", "result b", code="print('last')")]
    resumed = ResumedWork(
        messages=messages,
        entries=[],
        found=True,
        spilled=[{"call_id": "a", "name": "code_interpreter", "file_id": "f-123", "chars": 54321}],
    )
    note = resume_note(resumed, workspace="2 file(s), 1.0 KB of 2.0 GB:\n- workspace/a.py (1.0 KB, 10:00 UTC)")
    assert note is not None and note.role == "assistant"
    text = note.content
    assert "Its 2 completed tool call(s) are replayed above" in text
    assert "the last completed call was code_interpreter(" in text and "print('last')" in text
    assert "1 result(s) are replayed cut and kept whole" in text and "conversation file f-123" in text
    assert "54,321 characters" in text
    assert "The sandbox workspace (workspace/) holds: 2 file(s)" in text
    assert text.isascii()


def test_nothing_replayed_means_no_note():
    assert resume_note(ResumedWork()) is None


# -- The graceful stop --------------------------------------------------------


def test_the_wind_down_waits_for_the_end_of_the_grace(monkeypatch):
    monkeypatch.setenv(worker_shutdown.GRACEFUL_TIMEOUT_ENV, "120")
    monkeypatch.setattr(worker_shutdown, "_request", None)
    assert worker_shutdown.wind_down_due(now=1000.0) is False
    monkeypatch.setattr(
        worker_shutdown, "_request", worker_shutdown.ShutdownRequest(reason="stop", detail="SIGTERM", requested_at=1000.0)
    )
    # 60 s of the 120 s grace are the turn's own; the last 60 are not.
    assert worker_shutdown.wind_down_due(now=1059.0) is False
    assert worker_shutdown.wind_down_due(now=1061.0) is True
    monkeypatch.delenv(worker_shutdown.GRACEFUL_TIMEOUT_ENV)
    assert worker_shutdown.wind_down_due(now=1000.0) is True


def test_a_wound_down_turn_is_a_graceful_shutdown_cancellation():
    from app.core.utils import is_graceful_shutdown_cancellation

    exc = worker_shutdown.TurnWoundDown(worker_shutdown.WIND_DOWN_CANCEL_MESSAGE)
    assert isinstance(exc, asyncio.CancelledError)
    assert is_graceful_shutdown_cancellation(exc)


class _TwoRounds(BaseLLMProvider):
    provider_name = "stub"
    context_window = 50_000

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        self.calls += 1
        if self.calls <= 3:
            return ChatResponse(
                content="",
                model="stub",
                tool_calls=[ToolCall(id=f"tc-{self.calls}", name="slow_tool", arguments={"n": self.calls})],
            )
        return ChatResponse(content="final answer", model="stub")

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        yield "final answer"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


async def test_a_stopping_worker_winds_the_checkpointed_loop_down_after_the_call_in_flight(monkeypatch):
    monkeypatch.delenv(worker_shutdown.GRACEFUL_TIMEOUT_ENV, raising=False)
    monkeypatch.setattr(worker_shutdown, "_request", None)
    done: list[int] = []

    class _Executor:
        async def execute_tool(self, call):
            # The stop arrives while the first call runs: it finishes.
            worker_shutdown.note_shutdown(worker_shutdown.STOP, "SIGTERM")
            done.append(call.arguments["n"])
            return ToolResult(tool_call_id=call.id, content=f"done {call.arguments['n']}")

    offered: list[list[ChatMessage]] = []
    llm = _TwoRounds()
    with pytest.raises(worker_shutdown.TurnWoundDown):
        await run_agent_loop(
            llm=llm,
            messages=[ChatMessage(role="user", content="go")],
            tools=[ToolDefinition(name="slow_tool", description="slow", input_schema={"type": "object", "properties": {}})],
            executor=_Executor(),
            max_iterations=6,
            on_checkpoint=lambda work: offered.append(list(work)),
        )
    monkeypatch.setattr(worker_shutdown, "_request", None)
    assert done == [1], "the call in flight finished, no new one started"
    assert llm.calls == 1, "no model call after the stop"
    assert [e.get("tool_call_id") for e in completed_work(offered[-1])] == [None, "tc-1"]


async def test_a_loop_without_a_checkpoint_is_not_wound_down(monkeypatch):
    monkeypatch.delenv(worker_shutdown.GRACEFUL_TIMEOUT_ENV, raising=False)
    monkeypatch.setattr(
        worker_shutdown, "_request", worker_shutdown.ShutdownRequest(reason="stop", detail="SIGTERM", requested_at=0.0)
    )

    class _Executor:
        async def execute_tool(self, call):
            return ToolResult(tool_call_id=call.id, content="ok")

    try:
        response = await run_agent_loop(
            llm=_TwoRounds(),
            messages=[ChatMessage(role="user", content="go")],
            tools=[ToolDefinition(name="slow_tool", description="slow", input_schema={"type": "object", "properties": {}})],
            executor=_Executor(),
            max_iterations=6,
        )
    finally:
        monkeypatch.setattr(worker_shutdown, "_request", None)
    assert response.content == "final answer"
