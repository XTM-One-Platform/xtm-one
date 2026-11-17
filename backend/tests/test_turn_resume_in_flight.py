"""A resumed turn is told which calls its interrupted attempt had started.

An assignment run was cut by a worker stop while a ``create_github_pr`` call
ran: the pull request was created, but the checkpoint kept completed rounds
only and the loop offered it at iteration boundaries only, so the resumed turn
replayed everything before that call, knew nothing of it, and created the pull
request again ("A pull request already exists"). The loop now offers the
checkpoint right before it dispatches a round, the checkpoint names the calls
of that round still waiting for a result, and the resume note states them.
"""

from __future__ import annotations

import json
import uuid

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.services import background_checkpoint
from app.services.turn_checkpoint import (
    IN_FLIGHT_ROLE,
    ResumedWork,
    TurnCheckpoint,
    _completed_rounds,
    _in_flight_of,
    _messages,
    call_digest,
    completed_work,
    in_flight_calls,
    resume_note,
)
from app.tools.agent_loop import run_agent_loop
from app.tools.base import ToolCall, ToolDefinition, ToolResult

CONV = uuid.UUID("6a4e0b4c-1111-4222-8333-444455556677")


def _round(call_id: str, name: str, result: str | None, **arguments: object) -> list[ChatMessage]:
    messages = [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id=call_id, name=name, arguments=dict(arguments))],
        )
    ]
    if result is not None:
        messages.append(ChatMessage(role="tool", content=result, tool_call_id=call_id, name=name))
    return messages


def test_the_trailing_unanswered_round_is_in_flight():
    messages = [*_round("a", "commit", "ok"), *_round("b", "create_pr", None, title="fix")]
    assert in_flight_calls(messages) == [
        {
            "id": "b",
            "name": "create_pr",
            "arguments": {"title": "fix"},
            "answered": False,
            "digest": call_digest("create_pr", {"title": "fix"}),
        }
    ]
    # A complete trailing round, or a round followed by another message, is not.
    assert in_flight_calls(_round("a", "commit", "ok")) == []
    assert in_flight_calls([*_round("b", "create_pr", None), ChatMessage(role="user", content="next")]) == []


def test_a_parallel_round_names_its_answered_calls_too():
    messages = [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(id="x", name="read", arguments={}),
                ToolCall(id="y", name="write", arguments={"n": 1}),
            ],
        ),
        ChatMessage(role="tool", content="read result", tool_call_id="x", name="read"),
    ]
    assert [(c["id"], c["answered"]) for c in in_flight_calls(messages)] == [("x", True), ("y", False)]


async def test_the_checkpoint_stores_the_in_flight_round_apart_from_the_replayed_rounds():
    checkpoint = TurnCheckpoint(CONV, turn_id="t1")
    written: list[list[dict]] = []

    async def _write(entries: list[dict]) -> None:
        written.append(entries)

    checkpoint._write = _write  # type: ignore[method-assign]
    checkpoint.offer([*_round("a", "commit", "ok"), *_round("b", "create_pr", None, title="fix")])
    await checkpoint._task
    stored = written[-1]
    assert stored[-1]["role"] == IN_FLIGHT_ROLE
    # Every reader of the replayed rounds leaves it out: the rounds and the meter are unchanged.
    rounds = _completed_rounds(_messages(stored))
    assert [m.tool_call_id for m in rounds] == [None, "a"]
    assert background_checkpoint._call_ids(stored) == {"a"}
    assert _in_flight_of(stored) == [
        {
            "id": "b",
            "name": "create_pr",
            "arguments": {"title": "fix"},
            "answered": False,
            "digest": call_digest("create_pr", {"title": "fix"}),
        }
    ]
    # The round answered, the next snapshot names nothing in flight.
    checkpoint.offer([*_round("a", "commit", "ok"), *_round("b", "create_pr", "created #1", title="fix")])
    await checkpoint._task
    assert all(e["role"] != IN_FLIGHT_ROLE for e in written[-1])
    assert _in_flight_of(written[-1]) == []


def test_the_resume_note_names_the_call_in_flight():
    resumed = ResumedWork(
        messages=_round("a", "commit", "ok", message="feat"),
        found=True,
        in_flight=[{"id": "b", "name": "create_pr", "arguments": {"title": "fix"}, "answered": False}],
    )
    note = resume_note(resumed)
    assert note is not None
    assert "Its 1 completed tool call(s) are replayed above" in note.content
    assert "1 call(s) were started when it was interrupted and their outcome was not recorded" in note.content
    assert 'create_pr({"title": "fix"})' in note.content
    assert note.content.isascii()
    # Nothing completed, a call in flight: the note still says so.
    alone = resume_note(ResumedWork(found=True, in_flight=resumed.in_flight))
    assert alone is not None and "No tool call completed" in alone.content


async def test_a_task_replay_carries_the_in_flight_calls(monkeypatch):
    stored = {
        "turn_id": "attempt-1",
        "entries": [
            *completed_work(_round("a", "commit", "ok")),
            {"role": IN_FLIGHT_ROLE, "content": "", "calls": [{"id": "b", "name": "create_pr", "arguments": {}}]},
        ],
    }

    class _Redis:
        async def get(self, key):
            return json.dumps(stored)

    async def _client():
        return _Redis()

    monkeypatch.setattr(background_checkpoint, "_redis", _client)
    replay = await background_checkpoint.load_task_replay(uuid.uuid4())
    assert replay.replayed_calls == 1
    assert [c["name"] for c in replay.resumed.in_flight] == ["create_pr"]
    assert "create_pr({})" in replay.history[-1].content


class _OneCall(BaseLLMProvider):
    provider_name = "stub"
    context_window = 50_000

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(
                content="",
                model="stub",
                tool_calls=[ToolCall(id="pr-1", name="create_pr", arguments={"title": "fix"})],
            )
        return ChatResponse(content="final answer", model="stub")

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        yield "final answer"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


async def test_the_loop_checkpoints_a_round_before_its_calls_run():
    offered: list[list[ChatMessage]] = []
    seen_during_call: list[list[dict]] = []

    class _Executor:
        async def execute_tool(self, call):
            # What a cut at this moment leaves: the call named as in flight.
            seen_during_call.append(in_flight_calls(offered[-1]) if offered else [])
            return ToolResult(tool_call_id=call.id, content="created")

    response = await run_agent_loop(
        llm=_OneCall(),
        messages=[ChatMessage(role="user", content="go")],
        tools=[ToolDefinition(name="create_pr", description="pr", input_schema={"type": "object", "properties": {}})],
        executor=_Executor(),
        max_iterations=4,
        on_checkpoint=lambda work: offered.append(list(work)),
    )
    assert response.content == "final answer"
    assert seen_during_call == [
        [
            {
                "id": "pr-1",
                "name": "create_pr",
                "arguments": {"title": "fix"},
                "answered": False,
                "digest": call_digest("create_pr", {"title": "fix"}),
            }
        ]
    ]
    assert in_flight_calls(offered[-1]) == []
