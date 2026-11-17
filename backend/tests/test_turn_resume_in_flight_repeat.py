"""A resumed turn never repeats, unseen, a write its interrupted attempt had started.

An assignment run resumed six times by worker stops re-ran a
``sandbox_edit_file`` its interrupted attempt had started (refused as "find not
found": the first execution had landed), and its transcript held one resume
note, the last: each attempt's checkpoint started from the replayed rounds
alone, so every earlier note, with its in-flight line, was gone. The note is now
kept by the following checkpoints, each in-flight call carries a digest of its
whole arguments, and the loop answers the first identical repeat of a write
without running it.
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
    in_flight_calls,
    in_flight_writes,
    resume_note,
)
from app.tools.agent_loop import run_agent_loop
from app.tools.base import ToolCall, ToolDefinition, ToolResult

CONV = uuid.UUID("7b5f1c5d-2222-4333-8444-555566667788")
EDIT_ARGS = {"path": "a.py", "edits": [{"find": "old", "replace": "new"}]}


def _round(call_id: str, name: str, result: str | None, **arguments: object) -> list[ChatMessage]:
    messages = [
        ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id=call_id, name=name, arguments=dict(arguments))])
    ]
    if result is not None:
        messages.append(ChatMessage(role="tool", content=result, tool_call_id=call_id, name=name))
    return messages


def test_the_digest_reads_the_whole_arguments_key_order_aside():
    assert call_digest("w", {"a": 1, "b": 2}) == call_digest("w", {"b": 2, "a": 1})
    assert call_digest("w", {"a": 1}) != call_digest("w", {"a": 2})
    assert call_digest("w", {"a": 1}) != call_digest("v", {"a": 1})
    long_text = "x" * 50_000
    # Two long writes differing past the replay cut still differ.
    assert call_digest("w", {"t": long_text + "1"}) != call_digest("w", {"t": long_text + "2"})
    [call] = in_flight_calls(_round("b", "sandbox_edit_file", None, **EDIT_ARGS))
    assert call["digest"] == call_digest("sandbox_edit_file", EDIT_ARGS)


async def _stored_after(checkpoint: TurnCheckpoint, messages: list[ChatMessage]) -> list[dict]:
    written: list[list[dict]] = []

    async def _write(entries: list[dict]) -> None:
        written.append(entries)

    checkpoint._write = _write  # type: ignore[method-assign]
    checkpoint.offer(messages)
    await checkpoint._task
    return written[-1]


def _resume(stored: list[dict]) -> tuple[ResumedWork, list[ChatMessage]]:
    """What a resume reads from *stored*, as ``load_task_replay`` / ``load_replay`` build it."""
    messages = _completed_rounds(_messages(stored))
    from app.services.turn_checkpoint import _entries

    resumed = ResumedWork(messages=messages, entries=_entries(messages), found=True, in_flight=_in_flight_of(stored))
    note = resume_note(resumed)
    return resumed.with_note(note), [*messages, *([note] if note is not None else [])]


async def test_a_second_resume_still_shows_the_first_resumes_in_flight_line():
    # Attempt 1 is cut while an edit runs.
    first = await _stored_after(
        TurnCheckpoint(CONV, turn_id="attempt-1"),
        [*_round("a", "sandbox_read_file", "text"), *_round("b", "sandbox_edit_file", None, **EDIT_ARGS)],
    )
    assert first[-1]["role"] == IN_FLIGHT_ROLE
    resumed, history = _resume(first)
    assert "outcome was not recorded" in history[-1].content
    assert history[-1].in_flight and history[-1].in_flight[0]["name"] == "sandbox_edit_file"

    # Attempt 2 reads the file, then is cut again while a commit runs.
    second = await _stored_after(
        TurnCheckpoint(CONV, turn_id="attempt-2", resumed=resumed),
        [*_round("c", "sandbox_grep", "applied"), *_round("d", "create_github_commit", None, message="fix")],
    )
    _resumed2, history2 = _resume(second)
    notes = [m.content for m in history2 if m.role == "assistant" and not m.tool_calls]
    # Both notes: the first one's in-flight edit, the second one's in-flight commit.
    assert len(notes) == 2
    assert "sandbox_edit_file" in notes[0] and "outcome was not recorded" in notes[0]
    assert "create_github_commit" in notes[1] and "outcome was not recorded" in notes[1]
    # The guard reads the latest note only: the commit, not the edit settled in attempt 2.
    assert [c["name"] for c in in_flight_writes(history2).values()] == ["create_github_commit"]


async def test_a_task_replay_keeps_its_note_for_the_next_attempt(monkeypatch):
    stored = {
        "turn_id": "attempt-1",
        "entries": [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "name": "read", "arguments": {}}]},
            {"role": "tool", "content": "ok", "tool_call_id": "a", "name": "read"},
            {"role": IN_FLIGHT_ROLE, "content": "", "calls": in_flight_calls(_round("b", "create_github_pr", None))},
        ],
    }

    class _Redis:
        async def get(self, key):
            return json.dumps(stored)

    async def _client():
        return _Redis()

    monkeypatch.setattr(background_checkpoint, "_redis", _client)
    replay = await background_checkpoint.load_task_replay(uuid.uuid4())
    assert replay.resumed.entries[-1]["role"] == "assistant"
    assert "create_github_pr" in replay.resumed.entries[-1]["content"]
    # The note is no replayed call: the count of calls is unchanged.
    assert replay.replayed_calls == 1


def test_a_read_in_flight_may_run_again():
    note = resume_note(
        ResumedWork(found=True, in_flight=in_flight_calls(_round("b", "sandbox_read_file", None, path="a.py")))
    )
    assert note is not None
    assert in_flight_writes([note]) == {}


def test_a_replay_safe_write_in_flight_is_not_guarded():
    args = {"video_id": "v", "scene": "s1"}
    note = resume_note(
        ResumedWork(found=True, in_flight=in_flight_calls(_round("b", "render_video_scene", None, **args)))
    )
    assert note is not None
    assert next(iter(in_flight_writes([note]).values()))["name"] == "render_video_scene"
    assert in_flight_writes([note], replay_safe=frozenset({"render_video_scene"})) == {}


class _Script(BaseLLMProvider):
    """Asks for the in-flight call again, twice, then answers."""

    provider_name = "stub"
    context_window = 50_000

    def __init__(self, name: str = "sandbox_edit_file", arguments: dict | None = None) -> None:
        self.calls = 0
        self.name = name
        self.arguments = dict(arguments if arguments is not None else EDIT_ARGS)

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        self.calls += 1
        if self.calls <= 2:
            return ChatResponse(
                content="",
                model="stub",
                tool_calls=[ToolCall(id=f"r{self.calls}", name=self.name, arguments=dict(self.arguments))],
            )
        return ChatResponse(content="done", model="stub")

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        yield "done"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


async def test_the_loop_answers_the_first_identical_repeat_unrun_and_runs_the_second():
    ran: list[dict] = []

    class _Executor:
        async def execute_tool(self, call):
            ran.append(dict(call.arguments))
            return ToolResult(tool_call_id=call.id, content="edited")

    note = resume_note(
        ResumedWork(found=True, in_flight=in_flight_calls(_round("b", "sandbox_edit_file", None, **EDIT_ARGS)))
    )
    assert note is not None
    messages = [ChatMessage(role="system", content="s"), ChatMessage(role="user", content="go"), note]
    tool = ToolDefinition(
        name="sandbox_edit_file", description="edit", input_schema={"type": "object", "properties": {}}
    )
    await run_agent_loop(
        llm=_Script(),
        messages=messages,
        tools=[tool],
        executor=_Executor(),
        max_iterations=6,
    )
    answers = [m for m in messages if m.role == "tool"]
    assert answers[0].not_run == "in_flight_repeat"
    assert "Not run" in answers[0].content
    # The model repeated it once more after the refusal: it ran, once.
    assert len(ran) == 1
    assert answers[1].not_run is None and answers[1].content == "edited"


async def test_the_loop_runs_a_replay_safe_write_on_the_first_repeat():
    # A render whose job never queued before the worker stopped: the tool answers or replaces
    # an identical job by itself, so the resumed turn's first repeat runs.
    args = {"video_id": "v", "scene": "s1"}
    ran: list[dict] = []

    class _Executor:
        async def execute_tool(self, call):
            ran.append(dict(call.arguments))
            return ToolResult(tool_call_id=call.id, content=f"queued {len(ran)}")

    note = resume_note(
        ResumedWork(found=True, in_flight=in_flight_calls(_round("b", "render_video_scene", None, **args)))
    )
    assert note is not None
    messages = [ChatMessage(role="system", content="s"), ChatMessage(role="user", content="go"), note]
    tool = ToolDefinition(
        name="render_video_scene",
        description="preview",
        input_schema={"type": "object", "properties": {}},
        replay_safe=True,
    )
    await run_agent_loop(
        llm=_Script("render_video_scene", args),
        messages=messages,
        tools=[tool],
        executor=_Executor(),
        max_iterations=6,
    )
    answers = [m for m in messages if m.role == "tool"]
    assert answers and all(m.not_run != "in_flight_repeat" for m in answers)
    assert answers[0].content == "queued 1"
    assert ran and ran[0] == args


def test_a_tool_declared_read_only_in_flight_is_not_guarded():
    # "compare" is no verb the classifier reads (an unknown verb fails safe to a write): the tool declares it.
    args = {"repo": "o/r", "base": "10b4272c", "head": "main"}
    note = resume_note(
        ResumedWork(found=True, in_flight=in_flight_calls(_round("b", "compare_github_commits", None, **args)))
    )
    assert note is not None
    assert next(iter(in_flight_writes([note]).values()))["name"] == "compare_github_commits"
    assert in_flight_writes([note], read_only=frozenset({"compare_github_commits"})) == {}


def test_call_is_read_only_reads_the_declaration_and_fails_safe():
    from app.tools.base import call_is_read_only

    schema = {"type": "object", "properties": {}}
    waits = ToolDefinition(
        name="sandbox_run_command",
        description="run",
        input_schema=schema,
        read_only_call=lambda a: bool(a.get("run_id")) and not a.get("stop"),
    )
    assert call_is_read_only(waits, {"checkout": "xo", "run_id": "r1"}) is True
    assert call_is_read_only(waits, {"checkout": "xo", "run_id": "r1", "stop": True}) is False
    assert call_is_read_only(waits, {"checkout": "xo", "check": "backend-unit"}) is False
    assert call_is_read_only(None, {}) is False
    broken = ToolDefinition(name="w", description="w", input_schema=schema, read_only_call=lambda a: 1 / 0)
    assert call_is_read_only(broken, {}) is False
    assert call_is_read_only(ToolDefinition(name="r", description="r", read_only=True), None) is True


async def _loop_repeats(tool: ToolDefinition, args: dict) -> tuple[list[ChatMessage], list[dict]]:
    ran: list[dict] = []

    class _Executor:
        async def execute_tool(self, call):
            ran.append(dict(call.arguments))
            return ToolResult(tool_call_id=call.id, content=f"answer {len(ran)}")

    note = resume_note(ResumedWork(found=True, in_flight=in_flight_calls(_round("b", tool.name, None, **args))))
    assert note is not None
    messages = [ChatMessage(role="system", content="s"), ChatMessage(role="user", content="go"), note]
    await run_agent_loop(
        llm=_Script(tool.name, args), messages=messages, tools=[tool], executor=_Executor(), max_iterations=6
    )
    return [m for m in messages if m.role == "tool"], ran


async def test_the_loop_runs_a_repeated_compare_on_the_first_repeat():
    args = {"repo": "o/r", "base": "10b4272c", "head": "main"}
    tool = ToolDefinition(name="compare_github_commits", description="compare", read_only=True)
    answers, ran = await _loop_repeats(tool, args)
    assert answers and all(m.not_run != "in_flight_repeat" for m in answers)
    assert ran and ran[0] == args


async def test_the_loop_runs_a_repeated_wait_on_a_running_command_but_guards_a_stop():
    def waits(a):
        return bool(a.get("run_id")) and not a.get("stop")

    tool = ToolDefinition(name="sandbox_run_command", description="run", read_only_call=waits)
    answers, ran = await _loop_repeats(tool, {"checkout": "xo", "run_id": "r1"})
    assert answers and all(m.not_run != "in_flight_repeat" for m in answers)
    assert ran and ran[0]["run_id"] == "r1"
    # A stop changes something: the first repeat is still held back once.
    answers, ran = await _loop_repeats(tool, {"checkout": "xo", "run_id": "r1", "stop": True})
    assert answers[0].not_run == "in_flight_repeat"
    assert len(ran) == 1


def test_the_reads_the_classifier_cannot_name_are_declared():
    from app.integrations.providers.github.commits import register as register_commits
    from app.services.tool_classification import infer_tool_type
    from app.tools.base import CallableToolExecutor, call_is_read_only

    # The unknown verb a resumed desk run was refused on: a write by default, a read by declaration.
    assert infer_tool_type("compare_github_commits") == "write"
    definitions = {t.name: t for t in register_commits({}, CallableToolExecutor(), None)}
    assert call_is_read_only(definitions["compare_github_commits"], {"repo": "o/r", "base": "a", "head": "b"})
    assert not call_is_read_only(definitions.get("revert_github_commit"), {})


def test_the_render_tools_are_declared_replay_safe():
    from app.services.builtin_tools.video_studio import _definitions
    from app.services.video_studio.spec import QUALITY_PRESETS

    flags = {t.name: t.replay_safe for t in _definitions(list(QUALITY_PRESETS))}
    assert flags["render_video"] is True
    assert flags["render_video_scene"] is True
    # A write that is not idempotent keeps the guard.
    assert flags["delete_video"] is False
