"""A call in flight at two consecutive cuts is never run again unchanged (#9078).

A chat turn was cut four times in a row while it read a 43 MB job log: the
read took its worker down, the resume named it in flight, the in-flight guard
let it run again because a read may run again, and the turn was cut again,
never answering. The resume note now carries, per in-flight call, the
consecutive cuts it was in flight at, kept by the checkpoint, and the loop
answers every identical repeat of a call in flight at two cuts without running
it, read or write. The same tool with other arguments runs.
"""

from __future__ import annotations

import uuid

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.services.turn_checkpoint import (
    ResumedWork,
    TurnCheckpoint,
    _completed_rounds,
    _entries,
    _in_flight_of,
    _messages,
    call_digest,
    in_flight_poison,
    resume_note,
)
from app.tools.agent_loop import run_agent_loop
from app.tools.base import ToolCall, ToolDefinition, ToolResult

CONV = uuid.UUID("7b5f1c5d-2222-4333-8444-555566667799")
LOG_ARGS = {"repo": "o/r", "job_id": 127609262804}


def _round(call_id: str, name: str, result: str | None, **arguments: object) -> list[ChatMessage]:
    messages = [
        ChatMessage(
            role="assistant", content="", tool_calls=[ToolCall(id=call_id, name=name, arguments=dict(arguments))]
        )
    ]
    if result is not None:
        messages.append(ChatMessage(role="tool", content=result, tool_call_id=call_id, name=name))
    return messages


async def _stored_after(checkpoint: TurnCheckpoint, messages: list[ChatMessage]) -> list[dict]:
    written: list[list[dict]] = []

    async def _write(entries: list[dict]) -> None:
        written.append(entries)

    checkpoint._write = _write  # type: ignore[method-assign]
    checkpoint.offer(messages)
    await checkpoint._task
    return written[-1]


def _resume(stored: list[dict]) -> tuple[ResumedWork, list[ChatMessage]]:
    messages = _completed_rounds(_messages(stored))
    resumed = ResumedWork(messages=messages, entries=_entries(messages), found=True, in_flight=_in_flight_of(stored))
    note = resume_note(resumed)
    return resumed.with_note(note), [*messages, *([note] if note is not None else [])]


async def _cut_twice(second_args: dict) -> list[ChatMessage]:
    """Attempt 1 cut while the log read runs; attempt 2 cut while a read with *second_args* runs."""
    first = await _stored_after(
        TurnCheckpoint(CONV, turn_id="attempt-1"),
        [*_round("a", "list_github_prs", "prs"), *_round("b", "get_github_job_logs", None, **LOG_ARGS)],
    )
    resumed, _history = _resume(first)
    second = await _stored_after(
        TurnCheckpoint(CONV, turn_id="attempt-2", resumed=resumed),
        [*_round("c", "get_github_pr", "pr"), *_round("d", "get_github_job_logs", None, **second_args)],
    )
    _resumed2, history2 = _resume(second)
    return history2


async def test_one_cut_counts_one_and_guards_nothing():
    first = await _stored_after(
        TurnCheckpoint(CONV, turn_id="attempt-1"), _round("b", "get_github_job_logs", None, **LOG_ARGS)
    )
    _resumed, history = _resume(first)
    note = history[-1]
    assert note.in_flight_seen == [
        {"name": "get_github_job_logs", "digest": call_digest("get_github_job_logs", LOG_ARGS), "cuts": 1}
    ]
    assert in_flight_poison(history) == {}
    assert "consecutive interruptions" not in note.content


async def test_the_same_call_in_flight_at_two_cuts_is_poisoned_read_or_not():
    history = await _cut_twice(LOG_ARGS)
    note = history[-1]
    assert "2 consecutive interruptions: get_github_job_logs" in note.content
    poisoned = in_flight_poison(history)
    assert list(poisoned) == [call_digest("get_github_job_logs", LOG_ARGS)]
    assert poisoned[call_digest("get_github_job_logs", LOG_ARGS)]["cuts"] == 2


async def test_another_call_at_the_second_cut_starts_counting_again():
    history = await _cut_twice({"repo": "o/r", "job_id": 1})
    assert in_flight_poison(history) == {}
    assert history[-1].in_flight_seen and history[-1].in_flight_seen[0]["cuts"] == 1


async def test_the_count_survives_the_store_round_trip():
    history = await _cut_twice(LOG_ARGS)
    stored = _entries(history)
    assert stored[-1]["in_flight_seen"][0]["cuts"] == 2
    assert _messages(stored)[-1].in_flight_seen == history[-1].in_flight_seen
    # A malformed record is left out, never raised.
    malformed = [{"role": "assistant", "content": "n", "in_flight_seen": [{"digest": "x"}, 3]}]
    assert _messages(malformed)[0].in_flight_seen == []


class _Script(BaseLLMProvider):
    provider_name = "stub"
    context_window = 50_000

    def __init__(self, plan: list[dict]) -> None:
        self.plan = plan
        self.calls = 0

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        self.calls += 1
        if self.calls <= len(self.plan):
            return ChatResponse(
                content="",
                model="stub",
                tool_calls=[
                    ToolCall(id=f"r{self.calls}", name="get_github_job_logs", arguments=dict(self.plan[self.calls - 1]))
                ],
            )
        return ChatResponse(content="done", model="stub")

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        yield "done"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


async def test_the_loop_never_runs_the_poisoned_call_and_runs_another():
    ran: list[dict] = []

    class _Executor:
        async def execute_tool(self, call):
            ran.append(dict(call.arguments))
            return ToolResult(tool_call_id=call.id, content="log")

    history = await _cut_twice(LOG_ARGS)
    messages = [ChatMessage(role="system", content="s"), ChatMessage(role="user", content="go"), *history]
    other = {"repo": "o/r", "job_id": 127609262804, "failures_only": True}
    tool = ToolDefinition(name="get_github_job_logs", description="logs", read_only=True)
    await run_agent_loop(
        llm=_Script([LOG_ARGS, LOG_ARGS, other]),
        messages=messages,
        tools=[tool],
        executor=_Executor(),
        max_iterations=8,
    )
    answers = [m for m in messages if m.role == "tool" and m.tool_call_id in {"r1", "r2", "r3"}]
    # Both identical repeats answered unrun, unlike a write (the second repeat of a write runs).
    assert [m.not_run for m in answers[:2]] == ["in_flight_poison", "in_flight_poison"]
    assert "2 consecutive interruptions" in answers[0].content
    # The same tool with other arguments runs.
    assert ran == [other]


def test_a_cut_safe_tool_is_never_poisoned():
    seen = [{"name": "sandbox_start_service", "digest": "d1", "cuts": 2}, {"name": "get_github_job_logs", "digest": "d2", "cuts": 3}]
    history = [ChatMessage(role="assistant", content="[Resumed: ...]", in_flight_seen=seen)]
    assert set(in_flight_poison(history)) == {"d1", "d2"}
    assert set(in_flight_poison(history, cut_safe=frozenset({"sandbox_start_service"}))) == {"d2"}


async def test_the_loop_runs_a_cut_safe_service_start_after_two_cuts():
    """A lab start in flight at two rollouts runs again and answers the held service (#9243)."""
    start = {"checkout": "main", "service": "lab-light"}
    first = await _stored_after(
        TurnCheckpoint(CONV, turn_id="attempt-1"), _round("b", "sandbox_start_service", None, **start)
    )
    resumed, _history = _resume(first)
    second = await _stored_after(
        TurnCheckpoint(CONV, turn_id="attempt-2", resumed=resumed), _round("d", "sandbox_start_service", None, **start)
    )
    _resumed2, history = _resume(second)
    assert in_flight_poison(history) == {call_digest("sandbox_start_service", start): history[-1].in_flight_seen[0]}
    ran: list[dict] = []

    class _Executor:
        async def execute_tool(self, call):
            ran.append(dict(call.arguments))
            return ToolResult(tool_call_id=call.id, content="service_id lab-1 (already running)")

    class _StartScript(_Script):
        async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ChatResponse(
                    content="", model="stub", tool_calls=[ToolCall(id="r1", name="sandbox_start_service", arguments=dict(start))]
                )
            return ChatResponse(content="done", model="stub")

    messages = [ChatMessage(role="system", content="s"), ChatMessage(role="user", content="go"), *history]
    tool = ToolDefinition(name="sandbox_start_service", description="start", replay_safe=True, cut_safe=True)
    await run_agent_loop(llm=_StartScript([]), messages=messages, tools=[tool], executor=_Executor(), max_iterations=4)
    answer = next(m for m in messages if m.role == "tool" and m.tool_call_id == "r1")
    # Run on the first repeat, neither poisoned nor held back by the write guard.
    assert answer.not_run is None
    assert ran == [start]


VIDEO_ARGS = {"recording_ids": ["rec-1"]}


async def _stored_cut_by(
    monkeypatch, cause: str | None, checkpoint: TurnCheckpoint, messages: list[ChatMessage]
) -> list[dict]:
    """The entries *checkpoint* writes while its worker's recorded stop cause is *cause*."""
    from app.core import worker_shutdown

    monkeypatch.setattr(worker_shutdown, "cut_cause", lambda: cause)
    return await _stored_after(checkpoint, messages)


async def _wait_cut_twice(monkeypatch, first_cause: str | None, second_cause: str | None) -> list[ChatMessage]:
    """A long wait in flight at two cuts, each with its worker's recorded stop cause."""
    first = await _stored_cut_by(
        monkeypatch,
        first_cause,
        TurnCheckpoint(CONV, turn_id="attempt-1"),
        _round("b", "get_video_jobs", None, **VIDEO_ARGS),
    )
    resumed, _history = _resume(first)
    second = await _stored_cut_by(
        monkeypatch,
        second_cause,
        TurnCheckpoint(CONV, turn_id="attempt-2", resumed=resumed),
        _round("d", "get_video_jobs", None, **VIDEO_ARGS),
    )
    _resumed2, history = _resume(second)
    return history


async def test_two_cuts_by_a_worker_stop_never_poison_a_long_wait(monkeypatch):
    """A rollout cut the same wait twice: the cuts were the platform's, the call runs again (#9912)."""
    history = await _wait_cut_twice(monkeypatch, "stop", "stop")
    note = history[-1]
    assert note.in_flight_seen == [
        {"name": "get_video_jobs", "digest": call_digest("get_video_jobs", VIDEO_ARGS), "cuts": 0}
    ]
    assert in_flight_poison(history) == {}
    assert "consecutive interruptions" not in note.content


async def test_a_recycle_counts_as_a_worker_stop(monkeypatch):
    history = await _wait_cut_twice(monkeypatch, "recycle", "stop")
    assert in_flight_poison(history) == {}


async def test_two_cuts_with_no_recorded_cause_still_poison(monkeypatch):
    history = await _wait_cut_twice(monkeypatch, None, None)
    poisoned = in_flight_poison(history)
    assert list(poisoned) == [call_digest("get_video_jobs", VIDEO_ARGS)]
    assert poisoned[call_digest("get_video_jobs", VIDEO_ARGS)]["cuts"] == 2


async def test_a_worker_stop_between_two_unexplained_cuts_keeps_the_count(monkeypatch):
    """An unexplained cut, a rollout, then a second unexplained cut: two cuts the call may cause."""
    history = await _wait_cut_twice(monkeypatch, None, "stop")
    assert history[-1].in_flight_seen and history[-1].in_flight_seen[0]["cuts"] == 1
    resumed = ResumedWork(
        messages=history,
        found=True,
        in_flight=[
            {
                "id": "f",
                "name": "get_video_jobs",
                "arguments": VIDEO_ARGS,
                "answered": False,
                "digest": call_digest("get_video_jobs", VIDEO_ARGS),
            }
        ],
    )
    note = resume_note(resumed)
    assert note is not None and note.in_flight_seen and note.in_flight_seen[0]["cuts"] == 2
    assert list(in_flight_poison([*history, note])) == [call_digest("get_video_jobs", VIDEO_ARGS)]


async def test_the_cut_cause_survives_the_store_round_trip(monkeypatch):
    stored = await _stored_cut_by(
        monkeypatch,
        "stop",
        TurnCheckpoint(CONV, turn_id="attempt-1"),
        _round("b", "get_video_jobs", None, **VIDEO_ARGS),
    )
    assert stored[-1]["role"] == "in_flight" and stored[-1]["cut_by"] == "stop"
    assert _in_flight_of(stored)[0]["cut_by"] == "stop"
    # A worker that does not know why it stops stamps nothing.
    plain = await _stored_cut_by(
        monkeypatch, None, TurnCheckpoint(CONV, turn_id="attempt-1"), _round("b", "get_video_jobs", None, **VIDEO_ARGS)
    )
    assert "cut_by" not in plain[-1]
    assert "cut_by" not in _in_flight_of(plain)[0]
