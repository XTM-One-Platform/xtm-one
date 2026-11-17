"""A tool speaking from INSIDE its own execution.

The loop emits ``tool_start`` / ``tool_heartbeat`` / ``tool_done`` AROUND a
tool call; nothing crossed that boundary from within. ``app.tools.tool_events``
is the one channel: the loop binds a sink per call, the handler calls
``emit_tool_event``, and the payload rides the loop's ``on_event`` as a
``tool_progress`` frame tagged with the call it belongs to. These tests lock
in the contract - the frame, its position, the per-call isolation of a
parallel batch, and the "never raises, no-op when unbound" guarantee a
handler relies on.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools import tool_events
from app.tools.agent_loop import run_agent_loop
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
)
from app.tools.tool_events import (
    MAX_TOOL_EVENT_PAYLOAD_CHARS,
    PRESENTATION_SAVED_EVENT,
    TOOL_PROGRESS_STATUS,
    bind_tool_event_sink,
    current_tool_deadline,
    emit_tool_event,
    reset_tool_event_sink,
    tool_event_sink_bound,
)


class _ToolThenAnswerLLM(BaseLLMProvider):
    """First call issues the scripted tool calls, second call answers."""

    provider_name = "stub"
    context_window = 100_000

    def __init__(self, tool_calls: list[ToolCall]) -> None:
        self.scripted_calls = tool_calls
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(
                content="",
                model="stub",
                tool_calls=self.scripted_calls,
                stop_reason="tool_use",
            )
        return ChatResponse(content="All done.", model="stub")

    async def chat_stream(self, messages, **kwargs):
        yield "All done."

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


def _build_executor(**extra_payload) -> tuple[list[ToolDefinition], CompositeToolExecutor]:
    """A tool that commits (sleeps), REPORTS, then keeps working (sleeps)."""

    async def _reporting(i: int = 0) -> str:
        await asyncio.sleep(0.02)
        await emit_tool_event(PRESENTATION_SAVED_EVENT, presentation_id=f"deck-{i}", revision=i, **extra_payload)
        await asyncio.sleep(0.02)
        return f"result-{i}"

    callable_exec = CallableToolExecutor()
    callable_exec.register("reporting_tool", _reporting)
    definitions = [ToolDefinition(name="reporting_tool", description="Reports mid-execution")]
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)
    return definitions, composite


async def _run(llm, definitions, composite, *, with_consumer: bool = True) -> list[dict]:
    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="run the reporting tool")],
        tools=definitions,
        executor=composite,
        max_iterations=4,
        progressive_tools=False,
        on_event=_on_event if with_consumer else None,
    )
    assert response.content == "All done."
    return events


def _progress(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("status") == TOOL_PROGRESS_STATUS]


@pytest.mark.asyncio
async def test_a_tools_report_rides_on_event_between_start_and_done():
    definitions, composite = _build_executor()
    llm = _ToolThenAnswerLLM([ToolCall(id="tc-1", name="reporting_tool", arguments={"i": 1})])

    events = await _run(llm, definitions, composite)

    assert _progress(events) == [
        {
            "status": TOOL_PROGRESS_STATUS,
            "tool": "reporting_tool",
            "tool_call_id": "tc-1",
            "event": PRESENTATION_SAVED_EVENT,
            "presentation_id": "deck-1",
            "revision": 1,
        }
    ]
    # The report lands while the tool is still running: after its start,
    # before its completion - the whole point.
    statuses = [e.get("status") for e in events if e.get("status")]
    assert statuses.index("tool_start") < statuses.index(TOOL_PROGRESS_STATUS)
    assert statuses.index(TOOL_PROGRESS_STATUS) < statuses.index("tool_done")
    # The binding does not leak past the call.
    assert not tool_event_sink_bound()


@pytest.mark.asyncio
async def test_a_parallel_batch_keeps_each_report_on_its_own_call():
    definitions, composite = _build_executor()
    llm = _ToolThenAnswerLLM(
        [
            ToolCall(id="tc-1", name="reporting_tool", arguments={"i": 1}),
            ToolCall(id="tc-2", name="reporting_tool", arguments={"i": 2}),
        ]
    )

    events = await _run(llm, definitions, composite)

    # Both calls run concurrently and both reported; the tag on each frame is
    # the call whose handler emitted it (the sink is per task, not per batch).
    by_call = {e["tool_call_id"]: e for e in _progress(events)}
    assert set(by_call) == {"tc-1", "tc-2"}
    assert by_call["tc-1"]["presentation_id"] == "deck-1"
    assert by_call["tc-2"]["presentation_id"] == "deck-2"
    assert not tool_event_sink_bound()


@pytest.mark.asyncio
async def test_the_loop_owns_the_tagging_keys():
    # A payload that tries to spoof the frame's identity is overruled: the
    # consumer must be able to trust status / tool / tool_call_id.
    definitions, composite = _build_executor(status="done", tool="other_tool", tool_call_id="forged")
    llm = _ToolThenAnswerLLM([ToolCall(id="tc-1", name="reporting_tool", arguments={"i": 1})])

    events = await _run(llm, definitions, composite)

    (frame,) = _progress(events)
    assert frame["status"] == TOOL_PROGRESS_STATUS
    assert frame["tool"] == "reporting_tool"
    assert frame["tool_call_id"] == "tc-1"
    assert frame["event"] == PRESENTATION_SAVED_EVENT


@pytest.mark.asyncio
async def test_a_loop_without_a_consumer_runs_the_reporting_tool_unchanged():
    # No on_event: the sink is None, the emit is a no-op, the tool's result
    # is what it always was.
    definitions, composite = _build_executor()
    llm = _ToolThenAnswerLLM([ToolCall(id="tc-1", name="reporting_tool", arguments={"i": 1})])

    events = await _run(llm, definitions, composite, with_consumer=False)

    assert events == []


@pytest.mark.asyncio
async def test_emit_is_a_no_op_outside_a_bound_call():
    assert not tool_event_sink_bound()
    # A handler under test, a REST route reusing the tool function: nothing
    # to deliver to, nothing raised.
    await emit_tool_event(PRESENTATION_SAVED_EVENT, presentation_id="x", revision=3)


@pytest.mark.asyncio
async def test_a_failing_sink_never_reaches_the_handler(caplog):
    async def _broken(payload: dict) -> None:
        raise RuntimeError("socket closed")

    token = bind_tool_event_sink(_broken, tool_name="reporting_tool")
    try:
        assert tool_event_sink_bound()
        with caplog.at_level("WARNING", logger="app.tools.tool_events"):
            await emit_tool_event("anything", detail=1)
    finally:
        reset_tool_event_sink(token)
    # Logged, not raised: a progress signal is never worth failing the work.
    assert any(
        "could not be delivered" in r.getMessage() and "'reporting_tool'" in r.getMessage() for r in caplog.records
    )
    assert not tool_event_sink_bound()


@pytest.mark.asyncio
async def test_a_bound_sink_receives_the_payload_with_the_event_name():
    seen: list[dict] = []

    async def _sink(payload: dict) -> None:
        seen.append(payload)

    token = bind_tool_event_sink(_sink)
    try:
        await emit_tool_event(PRESENTATION_SAVED_EVENT, presentation_id="deck", revision=7)
    finally:
        reset_tool_event_sink(token)
    assert seen == [{"event": PRESENTATION_SAVED_EVENT, "presentation_id": "deck", "revision": 7}]


@pytest.mark.asyncio
async def test_an_emit_after_the_call_closed_is_a_silent_no_op():
    """A continuation the handler spawned inherits the binding through its
    copied context and may outlive the call. Once the loop's ``finally`` has
    closed the binding, its report is a no-op - not a frame tagged with a call
    that already finished - even though the reset itself never touched the
    continuation's context.
    """
    seen: list[dict] = []

    async def _sink(payload: dict) -> None:
        seen.append(payload)

    started = asyncio.Event()
    release = asyncio.Event()

    async def _continuation() -> None:
        started.set()
        await release.wait()
        await emit_tool_event("late", detail=1)

    binding = bind_tool_event_sink(_sink)
    continuation = asyncio.create_task(_continuation())
    await started.wait()
    # The call ends while the continuation is still running.
    reset_tool_event_sink(binding)
    assert not tool_event_sink_bound()
    release.set()
    await continuation

    assert seen == []


@pytest.mark.asyncio
async def test_a_bound_sink_receives_the_payload_with_the_event_name_json_safe():
    """The sink sees exactly what the client will: the values this codebase
    passes around (``UUID``, ``datetime``) arrive as strings and a tuple as a
    list. The WebSocket path would have caught a ``UUID`` inside ``_emit`` and
    lost the frame; the platform SSE proxy serialises in its response
    generator and would have ended the stream.
    """
    seen: list[dict] = []

    async def _sink(payload: dict) -> None:
        seen.append(payload)

    deck_id = uuid.uuid4()
    saved_at = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    token = bind_tool_event_sink(_sink)
    try:
        await emit_tool_event(PRESENTATION_SAVED_EVENT, presentation_id=deck_id, saved_at=saved_at, slides=(1, 2))
    finally:
        reset_tool_event_sink(token)

    assert seen == [
        {
            "event": PRESENTATION_SAVED_EVENT,
            "presentation_id": str(deck_id),
            "saved_at": str(saved_at),
            "slides": [1, 2],
        }
    ]
    json.dumps(seen[0])  # what every consumer does with it


class _Unprintable:
    """A value whose ``__str__`` raises - what ``default=str`` runs into."""

    def __str__(self) -> str:
        raise RuntimeError("no string form")


_UNPARSEABLE_PAYLOADS = [
    pytest.param({"blob": "x" * MAX_TOOL_EVENT_PAYLOAD_CHARS}, "chars", id="oversized"),
    pytest.param({"ratio": float("nan")}, "not JSON-serialisable", id="nan"),
    pytest.param({"index": {(1, 2): "tuple key"}}, "not JSON-serialisable", id="bad-key"),
    pytest.param({"row": _Unprintable()}, "not JSON-serialisable", id="str-raises"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("payload", "reason"), _UNPARSEABLE_PAYLOADS)
async def test_a_payload_no_consumer_could_parse_is_dropped_not_sent(caplog, payload, reason):
    seen: list[dict] = []

    async def _sink(frame: dict) -> None:
        seen.append(frame)

    token = bind_tool_event_sink(_sink, tool_name="reporting_tool")
    try:
        with caplog.at_level("WARNING", logger="app.tools.tool_events"):
            await emit_tool_event("anything", **payload)
    finally:
        reset_tool_event_sink(token)

    assert seen == []
    # The warning names the tool AND the event: two tools may emit the same
    # event (both deck tools report ``presentation_saved``).
    assert any(
        "dropped" in r.getMessage()
        and reason in r.getMessage()
        and "'reporting_tool'" in r.getMessage()
        and "'anything'" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_a_failing_decoder_on_the_round_trip_is_a_drop_not_a_raise(caplog, monkeypatch):
    # The decode of what the encoder just produced cannot realistically fail,
    # but "never raises" is the contract: the whole round-trip is guarded, so
    # a decoder failure is a logged drop like any other.
    def _refuse(_encoded: str) -> dict:
        raise RuntimeError("decoder down")

    monkeypatch.setattr(tool_events, "json", SimpleNamespace(dumps=json.dumps, loads=_refuse))
    seen: list[dict] = []

    async def _sink(frame: dict) -> None:
        seen.append(frame)

    token = bind_tool_event_sink(_sink, tool_name="reporting_tool")
    try:
        with caplog.at_level("WARNING", logger="app.tools.tool_events"):
            await emit_tool_event("anything", detail=1)
    finally:
        reset_tool_event_sink(token)

    assert seen == []
    assert any("dropped" in r.getMessage() and "'reporting_tool'" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_the_loop_names_the_tool_in_a_dropped_report(caplog):
    # Through the real loop: the handler's NaN never becomes a frame, the
    # handler's result is untouched, and the warning carries the LLM-visible
    # tool name the loop bound with the sink.
    definitions, composite = _build_executor(ratio=float("nan"))
    llm = _ToolThenAnswerLLM([ToolCall(id="tc-1", name="reporting_tool", arguments={"i": 1})])

    with caplog.at_level("WARNING", logger="app.tools.tool_events"):
        events = await _run(llm, definitions, composite)

    assert _progress(events) == []
    assert "tool_done" in [e.get("status") for e in events]
    assert any(
        "dropped" in r.getMessage()
        and "'reporting_tool'" in r.getMessage()
        and PRESENTATION_SAVED_EVENT in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_a_nested_loop_keeps_its_reports_inner_and_restores_the_outer_call():
    """A consult runs a whole inner loop INSIDE an outer tool call. The inner
    loop binds its own sink per call (its tools report to ITS consumer, which
    the consult wrapper nests under ``consult_progress``), an inner loop with
    no consumer shadows the outer sink instead of leaking onto it, and the
    outer call's own reports keep landing on the outer sink afterwards.
    """
    outer_seen: list[dict] = []

    async def _outer_sink(payload: dict) -> None:
        outer_seen.append(payload)

    inner_definitions, inner_composite = _build_executor()

    async def _outer_tool() -> str:
        await emit_tool_event("outer_step", step="before")
        # An inner loop WITH a consumer: its frames go there, not to us.
        inner_events = await _run(
            _ToolThenAnswerLLM([ToolCall(id="inner-1", name="reporting_tool", arguments={"i": 1})]),
            inner_definitions,
            inner_composite,
        )
        (inner_frame,) = _progress(inner_events)
        assert inner_frame["tool_call_id"] == "inner-1"
        # An inner loop WITHOUT a consumer: silence, not a leak onto us.
        silent_events = await _run(
            _ToolThenAnswerLLM([ToolCall(id="inner-2", name="reporting_tool", arguments={"i": 2})]),
            inner_definitions,
            inner_composite,
            with_consumer=False,
        )
        assert silent_events == []
        # Back in the outer call: the binding is ours again.
        assert tool_event_sink_bound()
        await emit_tool_event("outer_step", step="after")
        return "outer-done"

    binding = bind_tool_event_sink(_outer_sink)
    try:
        assert await _outer_tool() == "outer-done"
    finally:
        reset_tool_event_sink(binding)

    assert outer_seen == [
        {"event": "outer_step", "step": "before"},
        {"event": "outer_step", "step": "after"},
    ]
    assert not tool_event_sink_bound()


def _timed_executor(seen: dict[str, float]) -> tuple[list[ToolDefinition], CompositeToolExecutor]:
    """A tool that records how long the loop gives its call, as it reads it."""

    async def _timed(i: int = 0) -> str:
        deadline = current_tool_deadline()
        assert deadline is not None
        seen[f"call-{i}"] = deadline - time.monotonic()
        return f"result-{i}"

    callable_exec = CallableToolExecutor()
    callable_exec.register("timed_tool", _timed)
    definitions = [ToolDefinition(name="timed_tool", description="Reads its deadline")]
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)
    return definitions, composite


@pytest.mark.asyncio
@pytest.mark.parametrize("calls", [1, 2], ids=["sequential", "parallel"])
async def test_a_tool_call_knows_when_the_loop_cancels_it(calls):
    """The loop binds each call's own limit with its sink: a tool that
    must answer before it is cancelled - the ``computer_use`` result wait -
    reads the instant through ``current_tool_deadline``, in a lone call and in
    a parallel batch alike."""
    seen: dict[str, float] = {}
    definitions, composite = _timed_executor(seen)
    llm = _ToolThenAnswerLLM(
        [ToolCall(id=f"tc-{i}", name="timed_tool", arguments={"i": i}) for i in range(calls)]
    )

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="run the timed tool")],
        tools=definitions,
        executor=composite,
        max_iterations=4,
        progressive_tools=False,
        tool_timeout_seconds=42,
    )

    assert response.content == "All done."
    assert set(seen) == {f"call-{i}" for i in range(calls)}
    for left in seen.values():
        assert 40 < left <= 42
    assert current_tool_deadline() is None


def test_a_call_bound_without_a_limit_has_no_deadline():
    binding = bind_tool_event_sink(None, tool_name="reporting_tool")
    try:
        assert current_tool_deadline() is None
    finally:
        reset_tool_event_sink(binding)


def test_a_nested_call_never_outlives_the_call_it_runs_in():
    # A consult runs a whole loop INSIDE an outer tool call, which the outer
    # loop cancels at its own deadline: an inner call cannot count on more.
    outer = bind_tool_event_sink(None, timeout=10)
    try:
        assert current_tool_deadline() == outer.deadline
        longer = bind_tool_event_sink(None, timeout=300)
        try:
            assert current_tool_deadline() == outer.deadline
        finally:
            reset_tool_event_sink(longer)
        unbounded = bind_tool_event_sink(None)
        try:
            assert current_tool_deadline() == outer.deadline
        finally:
            reset_tool_event_sink(unbounded)
        shorter = bind_tool_event_sink(None, timeout=1)
        try:
            assert current_tool_deadline() == shorter.deadline < outer.deadline
        finally:
            reset_tool_event_sink(shorter)
        assert current_tool_deadline() == outer.deadline
    finally:
        reset_tool_event_sink(outer)
    assert current_tool_deadline() is None


@pytest.mark.asyncio
async def test_a_continuation_reads_no_deadline_once_the_call_closed():
    started = asyncio.Event()
    release = asyncio.Event()
    read: list[float | None] = []

    async def _continuation() -> None:
        started.set()
        await release.wait()
        read.append(current_tool_deadline())

    binding = bind_tool_event_sink(None, timeout=60)
    continuation = asyncio.create_task(_continuation())
    await started.wait()
    reset_tool_event_sink(binding)
    release.set()
    await continuation

    assert read == [None]
