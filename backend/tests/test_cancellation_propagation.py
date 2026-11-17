"""Tests for mid-call cancellation propagation (CORE-5) and the cancel
cascade to background sub-agent tasks (ORCH-6).

Part A — ``app.tools.agent_loop``:

* ``_streaming_chat`` aborts between received chunks when ``cancel_event``
  is set, raising ``_CancelledMidCallError`` and closing the provider stream.
* ``_race_cancellation`` cancels a pending LLM/tool await promptly when the
  event fires, properly cancelling + awaiting the loser task.
* ``cancel_event=None`` is a strict passthrough — zero behaviour change for
  assignments / external channels which pass no event today.
* ``run_agent_loop`` converts every mid-call abort into the exact same
  terminal outcome as the iteration-start check ("The request was
  cancelled." response).

Part B — ``app.services.sub_agent``:

* ``cancel_tasks_for_conversation`` cancels pending executions via the
  orchestration engine FSM, marks pending SubAgentTask rows cancelled, and
  suppresses proactive delivery for tasks already running.
* ``_deliver_task_result`` skips delivery for suppressed/cancelled tasks.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools.agent_loop import (
    _CANCELLED_MESSAGE,
    _CancelledMidCallError,
    _continue_truncated_response,
    _race_cancellation,
    _streaming_chat,
    _summarize_messages_for_context,
    run_agent_loop,
)
from app.tools.base import ToolCall, ToolDefinition, ToolResult

# ---------------------------------------------------------------------------
# Stub LLM providers
# ---------------------------------------------------------------------------


class _CancelMidStreamLLM(BaseLLMProvider):
    """Streams one chunk, then sets the cancel event before the next one."""

    provider_name = "stub"
    context_window = 10_000

    cancel_event: asyncio.Event  # assigned by the test after instantiation
    stream_closed = False

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        return ChatResponse(content="ok", model=model or "stub")

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        try:
            yield "Hello "
            self.cancel_event.set()
            yield "world "
            yield "should-not-be-streamed"
        finally:
            self.stream_closed = True

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


class _HangingChatLLM(BaseLLMProvider):
    """``chat`` hangs far longer than any test budget."""

    provider_name = "stub"
    context_window = 10_000

    saw_cancel = False

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.saw_cancel = True
            raise
        return ChatResponse(content="too late", model=model or "stub")

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        yield "unused"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


class _ToolCallingLLM(BaseLLMProvider):
    """First call requests a tool, second call returns a final answer."""

    provider_name = "stub"
    context_window = 10_000

    calls = 0

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(
                content="",
                model=model or "stub",
                tool_calls=[ToolCall(id="tc-1", name="slow_tool", arguments={})],
                stop_reason="tool_use",
            )
        return ChatResponse(content="final answer", model=model or "stub")

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        yield "final answer"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


class _HangingToolExecutor:
    """Tool executor whose execution hangs until cancelled."""

    def __init__(self) -> None:
        self.saw_cancel = False

    async def execute_tool(self, tool_call: ToolCall) -> ToolResult:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.saw_cancel = True
            raise
        return ToolResult(tool_call_id=tool_call.id, content="too late")


class _FastToolExecutor:
    """Tool executor that returns immediately (no-cancel control case)."""

    async def execute_tool(self, tool_call: ToolCall) -> ToolResult:
        return ToolResult(tool_call_id=tool_call.id, content="tool ok")


_SLOW_TOOL_DEF = ToolDefinition(
    name="slow_tool",
    description="A tool used by cancellation tests.",
    input_schema={"type": "object", "properties": {}},
)


def _set_event_soon(event: asyncio.Event, delay: float = 0.05) -> asyncio.Task:
    async def _setter() -> None:
        await asyncio.sleep(delay)
        event.set()

    return asyncio.create_task(_setter())


# ---------------------------------------------------------------------------
# Part A — streaming abort between chunks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_chat_aborts_between_chunks_when_cancelled():
    """The stream must abort as soon as the event is observed between chunks,
    without consuming/streaming later chunks, and close the provider stream."""
    llm = _CancelMidStreamLLM()
    llm.cancel_event = asyncio.Event()
    streamed: list[str] = []

    async def _on_token(chunk: str) -> None:
        streamed.append(chunk)

    with pytest.raises(_CancelledMidCallError):
        await _streaming_chat(
            llm,
            [ChatMessage(role="user", content="test")],
            _on_token,
            model="stub-model",
            max_tokens=256,
            cancel_event=llm.cancel_event,
        )

    assert "".join(streamed) == "Hello "
    assert "should-not-be-streamed" not in "".join(streamed)
    assert llm.stream_closed, "the provider stream generator must be closed"


@pytest.mark.asyncio
async def test_streaming_chat_unchanged_without_cancel_event():
    """No event (legacy callers) — the full stream is consumed as before."""
    llm = _CancelMidStreamLLM()
    # The event is still set by the stub mid-stream, but it is NOT passed to
    # _streaming_chat — so it must have no effect.
    llm.cancel_event = asyncio.Event()
    streamed: list[str] = []

    async def _on_token(chunk: str) -> None:
        streamed.append(chunk)

    response = await _streaming_chat(
        llm,
        [ChatMessage(role="user", content="test")],
        _on_token,
        model="stub-model",
        max_tokens=256,
    )

    assert response.content == "Hello world should-not-be-streamed"
    assert "".join(streamed) == response.content


@pytest.mark.asyncio
async def test_run_agent_loop_returns_cancelled_response_on_mid_stream_cancel():
    """A mid-stream cancel must produce the exact iteration-start outcome."""
    llm = _CancelMidStreamLLM()
    llm.cancel_event = asyncio.Event()

    async def _on_token(chunk: str) -> None:
        pass

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="question")],
        tools=None,
        max_iterations=3,
        on_token=_on_token,
        cancel_event=llm.cancel_event,
    )

    assert response.content == _CANCELLED_MESSAGE


# ---------------------------------------------------------------------------
# Part A — cancel-aware race helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_race_cancellation_cancels_pending_call_promptly():
    """When the event fires first, the inner task is cancelled AND awaited."""
    cancelled: list[bool] = []

    async def _slow() -> str:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return "too late"

    event = asyncio.Event()
    _set_event_soon(event, 0.02)

    with pytest.raises(_CancelledMidCallError):
        await asyncio.wait_for(
            _race_cancellation(_slow(), event, timeout=30), timeout=5
        )

    assert cancelled == [True], "the losing task must be cancelled and awaited"


@pytest.mark.asyncio
async def test_race_cancellation_drains_loser_exception_on_cancel():
    """A loser that raises a non-CancelledError while unwinding (or finishes
    with an exception right after ``asyncio.wait`` returns) must be drained by
    the cleanup — the event loop must never report an unretrieved task
    exception."""
    import gc

    loop = asyncio.get_running_loop()
    captured: list[dict] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, ctx: captured.append(ctx))
    try:

        async def _stubborn() -> str:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise RuntimeError("cleanup failure during unwind") from None
            return "too late"

        event = asyncio.Event()
        _set_event_soon(event, 0.02)

        with pytest.raises(_CancelledMidCallError):
            await _race_cancellation(_stubborn(), event, timeout=30)

        # Force collection of any abandoned task — an unretrieved exception
        # would surface through the loop exception handler here.
        gc.collect()
        await asyncio.sleep(0)
        assert not captured, f"unretrieved task exception leaked: {captured}"
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_race_cancellation_returns_result_when_call_wins():
    event = asyncio.Event()

    async def _fast() -> str:
        return "result"

    assert await _race_cancellation(_fast(), event, timeout=5) == "result"


@pytest.mark.asyncio
async def test_race_cancellation_propagates_call_exception():
    event = asyncio.Event()

    async def _boom() -> None:
        raise ValueError("tool exploded")

    with pytest.raises(ValueError, match="tool exploded"):
        await _race_cancellation(_boom(), event, timeout=5)


@pytest.mark.asyncio
async def test_race_cancellation_none_event_is_passthrough():
    """``cancel_event=None`` must behave exactly like ``asyncio.wait_for``."""

    async def _fast() -> str:
        return "result"

    async def _slow() -> None:
        await asyncio.sleep(60)

    assert await _race_cancellation(_fast(), None, timeout=5) == "result"
    with pytest.raises(TimeoutError):
        await _race_cancellation(_slow(), None, timeout=0.01)


@pytest.mark.asyncio
async def test_race_cancellation_times_out_like_wait_for():
    """With an event that never fires, the timeout path raises TimeoutError
    so the existing per-site timeout handlers keep working unchanged."""
    event = asyncio.Event()

    async def _slow() -> None:
        await asyncio.sleep(60)

    with pytest.raises(TimeoutError):
        await _race_cancellation(_slow(), event, timeout=0.01)


# ---------------------------------------------------------------------------
# Part A — full loop: non-streaming LLM call + tool execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_loop_cancels_non_streaming_llm_call_promptly():
    """A cancel during a hung non-streaming LLM call must abort promptly
    (well under the 900s LLM timeout) with the canonical cancelled response."""
    llm = _HangingChatLLM()
    event = asyncio.Event()
    _set_event_soon(event, 0.05)

    response = await asyncio.wait_for(
        run_agent_loop(
            llm=llm,
            messages=[ChatMessage(role="user", content="question")],
            tools=None,
            max_iterations=3,
            cancel_event=event,
        ),
        timeout=5,
    )

    assert response.content == _CANCELLED_MESSAGE
    assert llm.saw_cancel, "the in-flight LLM task must be cancelled"


@pytest.mark.asyncio
async def test_run_agent_loop_cancels_tool_execution_promptly():
    """A cancel during a hung tool execution must abort promptly with the
    canonical cancelled response and cancel the tool's asyncio task."""
    llm = _ToolCallingLLM()
    executor = _HangingToolExecutor()
    event = asyncio.Event()
    _set_event_soon(event, 0.05)

    response = await asyncio.wait_for(
        run_agent_loop(
            llm=llm,
            messages=[ChatMessage(role="user", content="question")],
            tools=[_SLOW_TOOL_DEF],
            executor=executor,
            max_iterations=3,
            cancel_event=event,
        ),
        timeout=5,
    )

    assert response.content == _CANCELLED_MESSAGE
    assert executor.saw_cancel, "the in-flight tool task must be cancelled"


@pytest.mark.asyncio
async def test_run_agent_loop_without_cancel_event_unchanged():
    """``cancel_event=None`` (assignments / channels) — the loop runs the tool
    and finishes normally; zero behaviour change."""
    llm = _ToolCallingLLM()
    executor = _FastToolExecutor()

    response = await asyncio.wait_for(
        run_agent_loop(
            llm=llm,
            messages=[ChatMessage(role="user", content="question")],
            tools=[_SLOW_TOOL_DEF],
            executor=executor,
            max_iterations=3,
            cancel_event=None,
        ),
        timeout=5,
    )

    assert response.content == "final answer"
    assert llm.calls == 2


# ---------------------------------------------------------------------------
# Part A — in-loop rescue paths (max_tokens continuation, empty-response
# retry, forced-final-answer backstop, semantic compaction)
# ---------------------------------------------------------------------------


class _TruncatingThenCancelLLM(BaseLLMProvider):
    """First call truncates at max_tokens; the continuation stream sets the
    cancel event after its first chunk."""

    provider_name = "stub"
    context_window = 10_000

    cancel_event: asyncio.Event  # assigned by the test after instantiation
    stream_calls = 0

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        return ChatResponse(content="ok", model=model or "stub")

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        self.stream_calls += 1
        if self.stream_calls == 1:
            # Initial response: truncated by max_tokens.
            self.last_stop_reason = "max_tokens"
            yield "partial "
        else:
            # Continuation: cancel fires between chunks.  When no event is
            # passed (legacy callers) the stream completes and finishes the
            # continuation loop via ``end_turn``.
            yield "more "
            self.cancel_event.set()
            yield "never-streamed"
            self.last_stop_reason = "end_turn"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


class _HangingContinuationChatLLM(BaseLLMProvider):
    """Non-streaming ``chat`` hangs (used for the continuation race path)."""

    provider_name = "stub"
    context_window = 10_000

    saw_cancel = False

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.saw_cancel = True
            raise
        return ChatResponse(content="too late", model=model or "stub")

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        yield "unused"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.mark.asyncio
async def test_continuation_streaming_aborts_on_cancel():
    """A cancel mid-continuation (streaming) must raise out of the helper so
    the caller can produce the canonical cancelled outcome — never surface the
    stitched partial response as if the request completed."""
    llm = _TruncatingThenCancelLLM()
    llm.cancel_event = asyncio.Event()

    initial = ChatResponse(
        content="partial ", model="stub", stop_reason="max_tokens"
    )

    async def _on_token(chunk: str) -> None:
        pass

    with pytest.raises(_CancelledMidCallError):
        await _continue_truncated_response(
            llm,
            [ChatMessage(role="user", content="question")],
            initial,
            on_token=_on_token,
            on_event=None,
            model="stub",
            temperature=0.7,
            max_tokens=256,
            max_session_chars=100_000,
            llm_extra={},
            merge_usage=lambda u, served_model=None: None,
            cancel_event=llm.cancel_event,
        )


@pytest.mark.asyncio
async def test_continuation_non_streaming_races_cancel():
    """The non-streaming continuation call goes through ``_race_cancellation``
    — a cancel aborts the hung call promptly and cancels its task."""
    llm = _HangingContinuationChatLLM()
    event = asyncio.Event()
    _set_event_soon(event, 0.05)

    initial = ChatResponse(
        content="partial ", model="stub", stop_reason="max_tokens"
    )

    with pytest.raises(_CancelledMidCallError):
        await asyncio.wait_for(
            _continue_truncated_response(
                llm,
                [ChatMessage(role="user", content="question")],
                initial,
                on_token=None,
                on_event=None,
                model="stub",
                temperature=0.7,
                max_tokens=256,
                max_session_chars=100_000,
                llm_extra={},
                merge_usage=lambda u, served_model=None: None,
                cancel_event=event,
            ),
            timeout=5,
        )

    assert llm.saw_cancel, "the in-flight continuation task must be cancelled"


@pytest.mark.asyncio
async def test_continuation_without_cancel_event_unchanged():
    """``cancel_event=None`` (default) — legacy callers see no change even
    when an event fires elsewhere mid-stream."""
    llm = _TruncatingThenCancelLLM()
    llm.cancel_event = asyncio.Event()

    initial = ChatResponse(
        content="partial ", model="stub", stop_reason="max_tokens"
    )

    async def _on_token(chunk: str) -> None:
        pass

    result = await _continue_truncated_response(
        llm,
        [ChatMessage(role="user", content="question")],
        initial,
        on_token=_on_token,
        on_event=None,
        model="stub",
        temperature=0.7,
        max_tokens=256,
        max_session_chars=100_000,
        llm_extra={},
        merge_usage=lambda u, served_model=None: None,
    )

    assert "never-streamed" in (result.content or "")


@pytest.mark.asyncio
async def test_summarize_messages_returns_empty_on_cancel():
    """A cancel during the semantic-compaction summarizer call must be treated
    as a summarizer miss (return "") — never raise out of the best-effort
    compaction path."""
    llm = _HangingContinuationChatLLM()
    event = asyncio.Event()
    _set_event_soon(event, 0.05)

    middle = [
        ChatMessage(role="user", content="long question " * 50),
        ChatMessage(role="assistant", content="long answer " * 50),
    ]
    summary = await asyncio.wait_for(
        _summarize_messages_for_context(
            llm, middle, model="stub", cancel_event=event
        ),
        timeout=5,
    )

    assert summary == ""
    assert llm.saw_cancel, "the in-flight summarizer task must be cancelled"


@pytest.mark.asyncio
async def test_run_agent_loop_cancelled_during_continuation():
    """Full loop: the initial stream truncates at max_tokens and the user
    cancels during the continuation — the loop must return the canonical
    cancelled response, not the stitched partial text."""
    llm = _TruncatingThenCancelLLM()
    llm.cancel_event = asyncio.Event()

    async def _on_token(chunk: str) -> None:
        pass

    response = await asyncio.wait_for(
        run_agent_loop(
            llm=llm,
            messages=[ChatMessage(role="user", content="question")],
            tools=None,
            max_iterations=3,
            on_token=_on_token,
            cancel_event=llm.cancel_event,
        ),
        timeout=5,
    )

    assert response.content == _CANCELLED_MESSAGE


# ---------------------------------------------------------------------------
# Part B — cancel cascade to background sub-agent tasks
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, *, all_rows=None, one_row=None):
        self._all_rows = all_rows or []
        self._one_row = one_row

    def scalars(self):
        return SimpleNamespace(all=lambda: self._all_rows)

    def scalar_one_or_none(self):
        return self._one_row


class _FakeSession:
    """Minimal async-session double: queued ``execute`` results + commit flag."""

    def __init__(self, results):
        self._results = list(results)
        self.committed = False
        self.execute_calls = 0

    async def execute(self, stmt):
        self.execute_calls += 1
        return self._results.pop(0)

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _make_task(status: str, metadata: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        metadata_json=metadata,
        error=None,
        completed_at=None,
        conversation_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
    )


@pytest.mark.asyncio
async def test_cancel_cascade_marks_pending_cancelled_and_suppresses_running(
    monkeypatch,
):
    """Pending tasks: engine execution cancelled + task row cancelled.
    Running tasks: left running, but delivery is suppressed."""
    from app.services.sub_agent import cancel_tasks_for_conversation

    pending_task = _make_task("pending")
    running_task = _make_task("running", metadata={"existing": "kept"})
    session = _FakeSession([_FakeResult(all_rows=[pending_task, running_task])])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    pending_execution = SimpleNamespace(id=uuid.uuid4(), state="pending")
    get_exec_mock = AsyncMock(return_value=pending_execution)
    cancel_exec_mock = AsyncMock(return_value=pending_execution)
    publish_mock = AsyncMock()
    with (
        patch(
            "app.services.orchestration_engine.get_execution_by_dedup_key",
            new=get_exec_mock,
        ),
        patch(
            "app.services.orchestration_engine.cancel_execution",
            new=cancel_exec_mock,
        ),
        patch(
            "app.services.sub_agent.publish_task_event",
            new=publish_mock,
        ),
    ):
        count = await cancel_tasks_for_conversation(
            None, str(pending_task.conversation_id)
        )

    assert count == 1
    assert session.committed

    # Real-time UI signal: exactly one ``cancelled`` event, for the
    # task that was actually flipped (the running one publishes nothing —
    # its own terminal transition will).
    publish_mock.assert_awaited_once_with(
        str(pending_task.user_id),
        "cancelled",
        str(pending_task.id),
        conversation_id=str(pending_task.conversation_id),
        status="cancelled",
    )

    # Pending task: cancelled outright, engine FSM transitioned.
    assert pending_task.status == "cancelled"
    assert pending_task.completed_at is not None
    assert pending_task.metadata_json["delivery_suppressed"] is True
    cancel_exec_mock.assert_awaited_once_with(
        session, execution_id=pending_execution.id
    )
    # The engine lookup only happens for pending tasks.
    get_exec_mock.assert_awaited_once()

    # Running task: left to finish, delivery suppressed, metadata preserved.
    assert running_task.status == "running"
    assert running_task.metadata_json["delivery_suppressed"] is True
    assert running_task.metadata_json["existing"] == "kept"


@pytest.mark.asyncio
async def test_cancel_cascade_running_execution_race_leaves_task_running(
    monkeypatch,
):
    """A pending task whose execution was claimed by a worker between the
    snapshot and the cascade is treated as running: not cancelled, delivery
    suppressed."""
    from app.services.sub_agent import cancel_tasks_for_conversation

    task = _make_task("pending")
    session = _FakeSession([_FakeResult(all_rows=[task])])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    running_execution = SimpleNamespace(id=uuid.uuid4(), state="running")
    cancel_exec_mock = AsyncMock()
    publish_mock = AsyncMock()
    with (
        patch(
            "app.services.orchestration_engine.get_execution_by_dedup_key",
            new=AsyncMock(return_value=running_execution),
        ),
        patch(
            "app.services.orchestration_engine.cancel_execution",
            new=cancel_exec_mock,
        ),
        patch(
            "app.services.sub_agent.publish_task_event",
            new=publish_mock,
        ),
    ):
        count = await cancel_tasks_for_conversation(
            None, str(task.conversation_id)
        )

    assert count == 0
    assert task.status == "pending"
    assert task.metadata_json["delivery_suppressed"] is True
    cancel_exec_mock.assert_not_called()
    # Nothing was flipped to ``cancelled`` — no lifecycle event either.
    publish_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_conversation_id", ["not-a-uuid", "", None, 12345])
async def test_cancel_cascade_invalid_conversation_id_is_noop(
    monkeypatch, bad_conversation_id
):
    from app.services.sub_agent import cancel_tasks_for_conversation

    def _unexpected_session():
        raise AssertionError(
            "async_session should not be created for an invalid conversation id"
        )

    monkeypatch.setattr("app.core.database.async_session", _unexpected_session)

    assert await cancel_tasks_for_conversation(None, bad_conversation_id) == 0


def test_delivery_suppressed_helper():
    from app.services.sub_agent import _delivery_suppressed

    assert _delivery_suppressed(None) is True
    assert _delivery_suppressed(_make_task("cancelled")) is True
    assert (
        _delivery_suppressed(_make_task("completed", {"delivery_suppressed": True}))
        is True
    )
    assert _delivery_suppressed(_make_task("completed")) is False
    assert _delivery_suppressed(_make_task("completed", {"other": 1})) is False


@pytest.mark.asyncio
async def test_deliver_task_result_skips_when_suppressed(monkeypatch):
    """The defense-in-depth re-check inside ``_deliver_task_result`` must skip
    delivery entirely when the task was suppressed/cancelled meanwhile."""
    from app.services.sub_agent import _deliver_task_result

    suppressed = _make_task("completed", {"delivery_suppressed": True})
    session = _FakeSession([_FakeResult(one_row=suppressed)])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    store_mock = AsyncMock()
    with patch("app.channels.router.store_message", new=store_mock):
        await _deliver_task_result(
            task_id=str(suppressed.id),
            conversation_id=str(uuid.uuid4()),
            agent_name="Agent",
            result_text="result that must not be delivered",
        )

    store_mock.assert_not_called()
    assert session.execute_calls == 1, "must return before the conversation lookup"
    assert not session.committed


@pytest.mark.asyncio
async def test_spawn_cancel_cascade_runs_off_the_ack_path(monkeypatch):
    """The WS cancel handler fires the cascade as a background task — it must
    invoke ``cancel_tasks_for_conversation`` without being awaited inline, and
    swallow any cascade failure."""
    from app.api.v1 import chat as chat_module

    called = asyncio.Event()
    seen: list[str] = []
    stops: list = []

    async def _fake_cascade(db, conversation_id, *, stopped_at=None):
        seen.append(conversation_id)
        stops.append(stopped_at)
        called.set()
        raise RuntimeError("cascade exploded — must be swallowed")

    monkeypatch.setattr(
        "app.services.sub_agent.cancel_tasks_for_conversation", _fake_cascade
    )

    from datetime import UTC, datetime

    pressed = datetime.now(UTC)
    chat_module._spawn_cancel_cascade("conv-123")
    spawned = datetime.now(UTC)

    await asyncio.wait_for(called.wait(), timeout=2)
    # Let the background task finish and self-remove from the strong-ref set.
    await asyncio.sleep(0.05)
    assert seen == ["conv-123"]
    # The Stop's moment is taken on the ack path, before the cascade runs, so
    # work the user starts meanwhile is not treated as stopped.
    assert stops[0] is not None and pressed <= stops[0] <= spawned
    assert not chat_module._cancel_cascade_tasks


@pytest.mark.asyncio
async def test_a_stop_is_recorded_in_the_turn_guard_before_the_cascade(monkeypatch):
    """The Stop's moment goes to the turn guard on the ack path, before
    the cascade is spawned, so a partner wake taking the turn afterwards reads
    it while ``Conversation.stopped_at`` is not committed yet. A turn guard
    that fails costs that window only, never the cascade."""
    from app.api.v1 import chat as chat_module
    from app.services import chat_turn_guard

    order: list[str] = []
    moments: list = []

    async def _record(conversation_id, stopped_at):
        order.append(f"record:{conversation_id}:{stopped_at.tzinfo is not None}")
        moments.append(stopped_at)

    def _cascade(cid, stopped_at=None):
        order.append(f"cascade:{cid}")
        moments.append(stopped_at)

    monkeypatch.setattr(chat_turn_guard, "record_stop", _record)
    monkeypatch.setattr(chat_module, "_spawn_cancel_cascade", _cascade)
    await chat_module._stop_conversation_work("conv-9")
    assert order == ["record:conv-9:True", "cascade:conv-9"]
    # One moment for both records, or work sent between two samples would be
    # newer than one Stop and older than the other.
    assert moments[0] is moments[1]
    moments.clear()

    async def _broken(*_a, **_k):
        raise RuntimeError("redis down")

    order.clear()
    monkeypatch.setattr(chat_turn_guard, "record_stop", _broken)
    await chat_module._stop_conversation_work("conv-9")
    assert order == ["cascade:conv-9"]


@pytest.mark.asyncio
async def test_a_stop_for_a_turn_elsewhere_stops_the_work_only_once_requested(monkeypatch):
    """The web stream's Stop: the cancel request goes between the turn-guard
    record and the cascade, and one that raises (the route's 503) leaves the
    background work running and withdraws the record."""
    from app.api.v1 import chat as chat_module
    from app.services import chat_turn_guard

    order: list[str] = []
    moments: list = []

    async def _record(_conversation_id, stopped_at):
        order.append("record")
        moments.append(stopped_at)

    async def _forget(_conversation_id, stopped_at):
        order.append("forget")
        moments.append(stopped_at)

    def _cascade(_cid, stopped_at=None):
        order.append("cascade")

    async def _request():
        order.append("request")
        return True

    monkeypatch.setattr(chat_turn_guard, "record_stop", _record)
    monkeypatch.setattr(chat_turn_guard, "forget_stop", _forget)
    monkeypatch.setattr(chat_module, "_spawn_cancel_cascade", _cascade)
    assert await chat_module.stop_conversation_work("conv-9", _request) is True
    assert order == ["record", "request", "cascade"]

    async def _unreachable():
        order.append("request")
        raise ConnectionError("redis down")

    order.clear()
    moments.clear()
    with pytest.raises(ConnectionError):
        await chat_module.stop_conversation_work("conv-9", _unreachable)
    # A 503 means nothing reached: the record would refuse partner answers.
    assert order == ["record", "request", "forget"]
    assert moments[0] is moments[1]


@pytest.mark.asyncio
async def test_deliver_task_result_delivers_when_not_suppressed(monkeypatch):
    """Control case: a normal completed task still delivers its result."""
    from app.services.sub_agent import _deliver_task_result

    task = _make_task("completed")
    conv = SimpleNamespace(
        id=uuid.uuid4(), channel="web", channel_thread_id=None
    )
    session = _FakeSession(
        [_FakeResult(one_row=task), _FakeResult(one_row=conv)]
    )
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    store_mock = AsyncMock()
    with patch("app.channels.router.store_message", new=store_mock):
        await _deliver_task_result(
            task_id=str(task.id),
            conversation_id=str(conv.id),
            agent_name="Agent",
            result_text="the result",
        )

    store_mock.assert_awaited_once()
    # The stored message is the lean completion notice (header + body), not the
    # raw result on its own - this is the user-visible shaping the notice adds.
    from app.services.sub_agent import _format_completion_notice

    delivered = store_mock.await_args.kwargs["content"]
    assert delivered == _format_completion_notice("Agent", "the result")
    assert delivered.startswith("**Background task completed**")
    assert delivered.endswith("the result")
    assert session.committed


# ---------------------------------------------------------------------------
# _format_completion_notice - lean proactive-delivery shaping
# ---------------------------------------------------------------------------


def test_format_completion_notice_empty_result():
    """An empty/whitespace-only result yields a header + explicit '(No output.)'."""
    from app.services.sub_agent import _format_completion_notice

    notice = _format_completion_notice("Researcher", "   ")
    assert notice.startswith("**Background task completed**")
    assert "Researcher" in notice
    assert notice.endswith("(No output.)")


def test_format_completion_notice_short_result_in_full():
    """A short result is delivered in full, untrimmed and without a footer."""
    from app.services.sub_agent import _format_completion_notice

    notice = _format_completion_notice("Researcher", "All 5 batches done.")
    assert notice.startswith("**Background task completed**")
    assert notice.endswith("All 5 batches done.")
    assert "..." not in notice
    assert "Extract" not in notice


def test_format_completion_notice_long_result_trimmed_without_task_footer():
    """A leftover dump (parent will not wake) is trimmed and must not claim
    a task page the user cannot open."""
    from app.services.sub_agent import (
        _DELIVERY_EXTRACT_CHARS,
        _format_completion_notice,
    )

    body = "x" * (_DELIVERY_EXTRACT_CHARS + 500)
    notice = _format_completion_notice("Researcher", body)
    assert notice.startswith("**Background task completed**")
    assert "..." in notice
    assert "full result available on the task" not in notice
    assert "Extract" not in notice
    assert len(notice) < len(body)
    assert ("x" * _DELIVERY_EXTRACT_CHARS) in notice
    assert ("x" * (_DELIVERY_EXTRACT_CHARS + 1)) not in notice
