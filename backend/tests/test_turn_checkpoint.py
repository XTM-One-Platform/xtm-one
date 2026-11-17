"""The checkpoint of a chat turn's tool work, and its resume."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.dialects import postgresql

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.services import turn_checkpoint
from app.services.turn_checkpoint import (
    ResumedWork,
    TurnCheckpoint,
    bounded,
    completed_work,
    in_flight_calls,
    load_resumable,
)
from app.tools.agent_loop import run_agent_loop
from app.tools.base import ToolCall, ToolDefinition, ToolResult

CONV = uuid.uuid4()


def _call(call_id: str, name: str = "read_file", **arguments) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def _asks(*calls: ToolCall, content: str = "") -> ChatMessage:
    return ChatMessage(role="assistant", content=content, tool_calls=list(calls))


def _answer(call_id: str, content: str = "result", name: str = "read_file") -> ChatMessage:
    return ChatMessage(role="tool", content=content, tool_call_id=call_id, name=name)


def _shape(entries: list[dict]) -> list[tuple]:
    return [
        (
            e["role"],
            e.get("tool_call_id"),
            tuple(c["id"] for c in e.get("tool_calls") or ()),
        )
        for e in entries
    ]


# ── What a checkpoint keeps ──────────────────────────────────────────────────


def test_only_completed_rounds_are_kept():
    work = [
        _asks(_call("a"), _call("b"), content="Let me read both."),
        _answer("a"),
        ChatMessage(role="system", content="[nudge]"),
        _answer("b"),
        ChatMessage(role="user", content="steering, stored already"),
        _asks(_call("c")),  # waiting for its result
    ]
    entries = completed_work(work)
    assert _shape(entries) == [
        ("assistant", None, ("a", "b")),
        ("tool", "a", ()),
        ("tool", "b", ()),
    ]
    assert entries[0]["content"] == "Let me read both."
    assert entries[0]["tool_calls"][0] == {"id": "a", "name": "read_file", "arguments": {}}
    assert entries[1] == {
        "role": "tool",
        "content": "result",
        "tool_call_id": "a",
        "name": "read_file",
    }


def test_a_round_cut_short_by_the_next_assistant_message_is_dropped():
    work = [
        _asks(_call("a"), _call("b")),
        _answer("a"),
        _asks(_call("c")),
        _answer("c"),
        _answer("b"),  # its round is over
    ]
    assert _shape(completed_work(work)) == [
        ("assistant", None, ("c",)),
        ("tool", "c", ()),
    ]


def test_an_answer_without_its_call_and_duplicated_ids_are_dropped():
    work = [
        _answer("orphan"),
        _asks(_call("x"), _call("x")),
        _answer("x"),
        _answer("x"),
    ]
    assert completed_work(work) == []


def test_assistant_text_is_kept_and_an_empty_one_is_not():
    work = [
        ChatMessage(role="assistant", content="Half of it is done."),
        ChatMessage(role="assistant", content="   "),
    ]
    assert _shape(completed_work(work)) == [("assistant", None, ())]


def test_arguments_are_stored_json_safe():
    when = datetime(2026, 10, 11, tzinfo=UTC)
    entries = completed_work([_asks(_call("a", since=when)), _answer("a")])
    assert entries[0]["tool_calls"][0]["arguments"] == {"since": str(when)}


def test_a_refused_call_keeps_why_it_did_not_run():
    refused = _answer("a", "Not executed")
    refused.not_run = "budget"
    entries = completed_work([_asks(_call("a")), refused])
    assert entries[1]["not_run"] == "budget"


def test_the_bound_drops_the_oldest_rounds_whole():
    entries = completed_work(
        [
            _asks(_call("old")),
            _answer("old", "x" * 600),
            _asks(_call("new")),
            _answer("new", "y" * 300),
        ]
    )
    assert _shape(bounded(entries, limit=500)) == [
        ("assistant", None, ("new",)),
        ("tool", "new", ()),
    ]
    assert bounded(entries, limit=10_000) == entries
    assert bounded(entries, limit=100) == []


# ── Reading a checkpoint back ────────────────────────────────────────────────


class _Rows:
    def __init__(self, row):
        self._row = row

    def one_or_none(self):
        return self._row


class _ReadDb:
    def __init__(self, row=None, *, error: Exception | None = None):
        self._row = row
        self._error = error

    async def execute(self, _statement):
        if self._error is not None:
            raise self._error
        return _Rows(self._row)


def _stored(*messages: ChatMessage) -> list[dict]:
    return completed_work(messages)


async def test_no_checkpoint_resumes_nothing():
    resumed = await load_resumable(_ReadDb(None), CONV)
    assert resumed == ResumedWork()


async def test_a_recent_checkpoint_is_resumed_as_messages():
    now = datetime.now(UTC)
    stored = _stored(_asks(_call("a", path="/x")), _answer("a", "contents"))
    resumed = await load_resumable(_ReadDb((stored, "turn-1", now)), CONV, now=now)

    assert resumed.found
    assert [(m.role, m.tool_call_id) for m in resumed.messages] == [
        ("assistant", None),
        ("tool", "a"),
    ]
    assert resumed.messages[0].tool_calls == [_call("a", path="/x")]
    assert resumed.messages[1].content == "contents"
    assert resumed.entries == stored


async def test_an_old_checkpoint_is_found_but_not_resumed():
    now = datetime.now(UTC)
    stored = _stored(_asks(_call("a")), _answer("a"))
    old = now - turn_checkpoint.RESUME_MAX_AGE - timedelta(minutes=1)
    resumed = await load_resumable(_ReadDb((stored, "turn-1", old)), CONV, now=now)
    assert resumed == ResumedWork(found=True)


async def test_a_naive_timestamp_reads_as_utc():
    now = datetime.now(UTC)
    stored = _stored(_asks(_call("a")), _answer("a"))
    naive = now.replace(tzinfo=None) - timedelta(minutes=5)
    resumed = await load_resumable(_ReadDb((stored, None, naive)), CONV, now=now)
    assert len(resumed.messages) == 2


async def test_a_malformed_checkpoint_resumes_what_is_sound_in_it():
    now = datetime.now(UTC)
    sound = _stored(_asks(_call("b")), _answer("b"))
    stored = [
        "not an entry",
        {"role": "assistant", "content": "", "tool_calls": [{"name": "no id"}]},
        {"role": "tool", "content": "orphan", "tool_call_id": "a"},
        {"role": "user", "content": "never resumed"},
        *sound,
    ]
    resumed = await load_resumable(_ReadDb((stored, None, now)), CONV, now=now)
    assert resumed.entries == sound
    assert (await load_resumable(_ReadDb(({"x": 1}, None, now)), CONV, now=now)) == (
        ResumedWork(found=True)
    )


async def test_an_unreadable_checkpoint_costs_the_resume_only():
    resumed = await load_resumable(_ReadDb(error=RuntimeError("db down")), CONV)
    assert resumed == ResumedWork()


# ── Writing a checkpoint ─────────────────────────────────────────────────────


class _Sessions:
    """A session factory recording the entries each upsert writes."""

    def __init__(self, *, gate: asyncio.Event | None = None, error: Exception | None = None):
        self.writes: list[list[dict]] = []
        self.gate = gate
        self.error = error

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, statement):
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        self.writes.append(statement.compile(dialect=postgresql.dialect()).params["entries"])

    async def commit(self):
        return None


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_each_new_round_is_written_and_an_unchanged_offer_is_not():
    sessions = _Sessions()
    checkpoint = TurnCheckpoint(CONV, session_factory=sessions)
    first = [_asks(_call("a")), _answer("a")]

    checkpoint.offer(first)
    await _settle()
    checkpoint.offer([*first, ChatMessage(role="system", content="[nudge]")])
    await _settle()
    checkpoint.offer([*first, _asks(_call("b")), _answer("b")])
    await _settle()

    assert [_shape(w) for w in sessions.writes] == [
        [("assistant", None, ("a",)), ("tool", "a", ())],
        [
            ("assistant", None, ("a",)),
            ("tool", "a", ()),
            ("assistant", None, ("b",)),
            ("tool", "b", ()),
        ],
    ]
    assert checkpoint.may_hold_row


async def test_a_text_only_message_is_kept_but_is_no_progress():
    """A narration with no tool call holds work for the next turn, yet it does not advance."""
    sessions = _Sessions()
    checkpoint = TurnCheckpoint(CONV, session_factory=sessions)
    checkpoint.offer([ChatMessage(role="assistant", content="I will run the tests now.")])
    await _settle()

    assert checkpoint.holds_work
    assert not checkpoint.advanced


async def test_a_completed_tool_round_is_progress():
    sessions = _Sessions()
    checkpoint = TurnCheckpoint(CONV, session_factory=sessions)
    assert not checkpoint.advanced

    checkpoint.offer([_asks(_call("a")), _answer("a")])
    await _settle()

    assert checkpoint.holds_work
    assert checkpoint.advanced


async def test_nothing_offered_writes_nothing():
    sessions = _Sessions()
    checkpoint = TurnCheckpoint(CONV, session_factory=sessions)
    checkpoint.offer([])
    await _settle()
    assert sessions.writes == []
    assert not checkpoint.may_hold_row


async def test_an_unanswered_round_writes_only_its_in_flight_calls():
    """A round dispatched and not answered is named for the next turn, never replayed."""
    sessions = _Sessions()
    checkpoint = TurnCheckpoint(CONV, session_factory=sessions)
    checkpoint.offer([_asks(_call("a"))])
    await _settle()
    assert sessions.writes == [
        [
            {
                "role": "in_flight",
                "content": "",
                "calls": [
                    {
                        "id": "a",
                        "name": "read_file",
                        "arguments": {},
                        "answered": False,
                        "digest": turn_checkpoint.call_digest("read_file", {}),
                    }
                ],
            }
        ]
    ]
    assert not checkpoint.advanced


async def test_the_latest_snapshot_wins_while_a_write_is_in_flight():
    gate = asyncio.Event()
    sessions = _Sessions(gate=gate)
    checkpoint = TurnCheckpoint(CONV, session_factory=sessions)

    rounds: list[ChatMessage] = []
    for call_id in ("a", "b", "c"):
        rounds += [_asks(_call(call_id)), _answer(call_id)]
        checkpoint.offer(list(rounds))
        await _settle()
    gate.set()
    await checkpoint.aclose(flush=True)

    assert [len(w) for w in sessions.writes] == [2, 6]


async def test_resumed_work_is_carried_in_front_of_the_turn_s_own():
    resumed = ResumedWork(entries=_stored(_asks(_call("old")), _answer("old")), found=True)
    sessions = _Sessions()
    checkpoint = TurnCheckpoint(CONV, resumed=resumed, session_factory=sessions)
    assert checkpoint.may_hold_row

    checkpoint.offer([_asks(_call("new")), _answer("new")])
    await checkpoint.aclose(flush=True)

    assert _shape(sessions.writes[-1]) == [
        ("assistant", None, ("old",)),
        ("tool", "old", ()),
        ("assistant", None, ("new",)),
        ("tool", "new", ()),
    ]


async def test_closing_without_flush_drops_the_pending_snapshot():
    gate = asyncio.Event()
    sessions = _Sessions(gate=gate)
    checkpoint = TurnCheckpoint(CONV, session_factory=sessions)
    checkpoint.offer([_asks(_call("a")), _answer("a")])
    await _settle()
    checkpoint.offer([_asks(_call("a")), _answer("a"), _asks(_call("b")), _answer("b")])

    closing = asyncio.create_task(checkpoint.aclose(flush=False))
    await _settle()
    gate.set()
    await closing
    checkpoint.offer([_asks(_call("c")), _answer("c")])
    await _settle()

    assert [len(w) for w in sessions.writes] == [2]


async def test_a_write_still_running_at_close_is_cancelled(monkeypatch):
    monkeypatch.setattr(turn_checkpoint, "CLOSE_TIMEOUT_SECONDS", 0.05)
    sessions = _Sessions(gate=asyncio.Event())
    checkpoint = TurnCheckpoint(CONV, session_factory=sessions)
    checkpoint.offer([_asks(_call("a")), _answer("a")])
    await _settle()

    await checkpoint.aclose(flush=True)

    assert sessions.writes == []
    assert checkpoint._task is not None and checkpoint._task.done()


async def test_a_failing_write_never_reaches_the_turn():
    sessions = _Sessions(error=RuntimeError("db down"))
    checkpoint = TurnCheckpoint(CONV, session_factory=sessions)
    checkpoint.offer([_asks(_call("a")), _answer("a")])
    await checkpoint.aclose(flush=True)
    assert checkpoint.may_hold_row


# ── Deleting a checkpoint ────────────────────────────────────────────────────


class _Savepoint:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        self._db.savepoints += 1
        return self

    async def __aexit__(self, *_exc):
        return False


class _DeleteDb:
    def __init__(self, *, error: Exception | None = None):
        self.savepoints = 0
        self.statements: list = []
        self._error = error

    def begin_nested(self):
        return _Savepoint(self)

    async def execute(self, statement):
        if self._error is not None:
            raise self._error
        self.statements.append(statement)


async def test_discard_deletes_the_row_inside_a_savepoint():
    db = _DeleteDb()
    await turn_checkpoint.discard(db, CONV)
    assert db.savepoints == 1
    (statement,) = db.statements
    assert statement.table.name == "chat_turn_checkpoints"


async def test_a_failing_discard_never_fails_the_answer():
    await turn_checkpoint.discard(_DeleteDb(error=RuntimeError("lock timeout")), CONV)


# ── The loop hands its work over ─────────────────────────────────────────────


class _ScriptedLLM(BaseLLMProvider):
    provider_name = "stub"
    context_window = 50_000

    def __init__(self) -> None:
        self.calls: list[list[ChatMessage]] = []

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        self.calls.append(list(messages))
        if len(self.calls) <= 2:
            n = len(self.calls)
            return ChatResponse(
                content="",
                model="stub",
                tool_calls=[ToolCall(id=f"tc-{n}", name="read_file", arguments={"n": n})],
            )
        return ChatResponse(content="final answer", model="stub")

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        yield "final answer"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


class _Executor:
    async def execute_tool(self, tool_call):
        return ToolResult(tool_call_id=tool_call.id, content=f"read {tool_call.arguments}")


_TOOL = ToolDefinition(
    name="read_file",
    description="Read a file",
    input_schema={"type": "object", "properties": {"n": {"type": "integer"}}},
)


async def test_the_loop_offers_the_work_it_appended_at_each_iteration():
    history = [
        ChatMessage(role="user", content="earlier question"),
        ChatMessage(role="assistant", content="earlier answer"),
        ChatMessage(role="user", content="read the files"),
    ]
    offered: list[list[ChatMessage]] = []

    response = await run_agent_loop(
        llm=_ScriptedLLM(),
        messages=list(history),
        tools=[_TOOL],
        executor=_Executor(),
        max_iterations=6,
        on_checkpoint=lambda work: offered.append(list(work)),
    )

    assert response.content == "final answer"
    # The loop offers right before it dispatches a round and again at the next
    # iteration: the completed rounds grow by one tool round at a time.
    shapes: list[list[tuple]] = []
    for work in offered:
        shape = _shape(completed_work(work))
        if shape and (not shapes or shapes[-1] != shape):
            shapes.append(shape)
    assert shapes == [
        [("assistant", None, ("tc-1",)), ("tool", "tc-1", ())],
        [
            ("assistant", None, ("tc-1",)),
            ("tool", "tc-1", ()),
            ("assistant", None, ("tc-2",)),
            ("tool", "tc-2", ()),
        ],
    ]
    # Each round is offered once in flight, before its call runs.
    flights: list[list[str]] = []
    for work in offered:
        ids = [call["id"] for call in in_flight_calls(work)]
        if ids and (not flights or flights[-1] != ids):
            flights.append(ids)
    assert flights == [["tc-1"], ["tc-2"]]
    for work in offered:
        assert not any(m is h for m in work for h in history)


async def test_a_failing_checkpoint_callback_never_breaks_the_loop():
    def _boom(_work):
        raise RuntimeError("boom")

    response = await run_agent_loop(
        llm=_ScriptedLLM(),
        messages=[ChatMessage(role="user", content="read the files")],
        tools=[_TOOL],
        executor=_Executor(),
        max_iterations=6,
        on_checkpoint=_boom,
    )
    assert response.content == "final answer"


async def test_resumed_rounds_reach_the_next_turn_s_model_request():
    """The next turn's model sees the work, before the new message."""
    sessions = _Sessions()
    checkpoint = TurnCheckpoint(CONV, session_factory=sessions)

    await run_agent_loop(
        llm=_ScriptedLLM(),
        messages=[ChatMessage(role="user", content="read the files")],
        tools=[_TOOL],
        executor=_Executor(),
        max_iterations=6,
        on_checkpoint=checkpoint.offer,
    )
    await checkpoint.aclose(flush=True)
    stored = sessions.writes[-1]
    now = datetime.now(UTC)
    resumed = await load_resumable(_ReadDb((stored, None, now)), CONV, now=now)

    second = _ScriptedLLM()
    second.calls = [[], []]  # answers straight away
    await run_agent_loop(
        llm=second,
        messages=[
            ChatMessage(role="user", content="read the files"),
            *resumed.messages,
            ChatMessage(role="user", content="you crashed, go on"),
        ],
        tools=[_TOOL],
        executor=_Executor(),
        max_iterations=6,
    )
    request = [m for m in second.calls[-1] if m.role != "system"]
    assert [(m.role, m.tool_call_id) for m in request] == [
        ("user", None),
        ("assistant", None),
        ("tool", "tc-1"),
        ("assistant", None),
        ("tool", "tc-2"),
        ("user", None),
    ]
    assert request[-1].content == "you crashed, go on"
    assert request[2].content == "read {'n': 1}"
