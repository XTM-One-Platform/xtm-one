"""Mid-run steering: user messages pushed onto ``steering_queue`` while the
agent loop is running are injected as ``user`` messages at the next
iteration boundary, and a ``steering`` event is emitted so the UI can show
"Incorporating your message..."."""

from __future__ import annotations

import asyncio

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools.agent_loop import drain_steering_queue, run_agent_loop
from app.tools.base import ToolCall, ToolDefinition, ToolResult


class _SteeredStubLLM(BaseLLMProvider):
    """First call returns a tool call, second call returns a final answer.

    Captures a snapshot of the messages it was given on each call so the
    test can assert the steering text was visible to the second call.
    """

    provider_name = "stub"
    context_window = 50_000

    def __init__(self) -> None:
        self.calls: list[list[ChatMessage]] = []

    async def chat(
        self,
        messages,
        model=None,
        temperature=0.7,
        max_tokens=4096,
        tools=None,
        **kwargs,
    ):
        self.calls.append(list(messages))
        if len(self.calls) == 1:
            return ChatResponse(
                content="",
                model=model or "stub",
                tool_calls=[ToolCall(id="tc-1", name="noop_tool", arguments={})],
            )
        return ChatResponse(content="final answer", model=model or "stub")

    async def chat_stream(
        self,
        messages,
        model=None,
        temperature=0.7,
        max_tokens=4096,
        tools=None,
        **kwargs,
    ):
        yield "final answer"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


class _NoopExecutor:
    """Minimal executor satisfying the agent loop's tool-execution contract."""

    async def execute_tool(self, tool_call):
        return ToolResult(tool_call_id=tool_call.id, content="ok")


class _FileAwareNoopExecutor(_NoopExecutor):
    """No-op executor that also accepts the loop's FileContext propagation."""

    file_context = None

    def set_file_context(self, ctx) -> None:
        self.file_context = ctx


@pytest.mark.asyncio
async def test_steering_message_injected_at_iteration_boundary():
    """A steering text queued during the tool iteration is appended to the
    messages list as a ``user`` message before the next LLM call, and the
    loop emits a ``steering`` event."""
    llm = _SteeredStubLLM()
    queue: asyncio.Queue[str] = asyncio.Queue()
    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)
        # Simulate the user steering while the first iteration's tool is
        # being dispatched — anything queued before iteration 2 works.
        if event.get("status") == "tool_start" and queue.empty():
            queue.put_nowait("actually, use the EMEA subregion filter")

    tool = ToolDefinition(
        name="noop_tool",
        description="No-op test tool",
        input_schema={"type": "object", "properties": {}},
    )
    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="build the report")],
        tools=[tool],
        executor=_NoopExecutor(),
        max_iterations=5,
        on_event=_on_event,
        steering_queue=queue,
    )

    assert response.content == "final answer"
    assert len(llm.calls) == 2
    # The steering text must be visible to the second LLM call as a user turn.
    second_call_user_contents = [
        m.content for m in llm.calls[1] if m.role == "user"
    ]
    assert "actually, use the EMEA subregion filter" in second_call_user_contents
    # ...but not to the first call (it was queued mid-iteration).
    first_call_user_contents = [
        m.content for m in llm.calls[0] if m.role == "user"
    ]
    assert (
        "actually, use the EMEA subregion filter" not in first_call_user_contents
    )
    # A steering event was emitted for the UI.
    steering_events = [e for e in events if e.get("status") == "steering"]
    assert len(steering_events) == 1
    assert steering_events[0].get("count") == 1
    # Queue fully consumed — nothing left over for the caller to fold in.
    assert queue.empty()


@pytest.mark.asyncio
async def test_a_steering_queue_admits_what_it_held_as_the_loop_takes_it():
    """What a ``SteeringQueue`` held is admitted once more at the
    iteration boundary, on what was drained: an item that stopped being
    deliverable while it waited is not injected, and the rest is."""
    from app.tools.agent_loop import SteeringQueue

    class _HoldsBackTheStale(SteeringQueue):
        def __init__(self) -> None:
            super().__init__()
            self.admitted_from: list[list] = []

        async def admit(self, items):
            self.admitted_from.append(list(items))
            return [item for item in items if item != "stale partner answer"]

    llm = _SteeredStubLLM()
    queue = _HoldsBackTheStale()
    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)
        if (
            event.get("status") == "tool_start"
            and not queue.admitted_from
            and queue.empty()
        ):
            queue.put_nowait("stale partner answer")
            queue.put_nowait("actually, use the EMEA subregion filter")

    tool = ToolDefinition(
        name="noop_tool",
        description="No-op test tool",
        input_schema={"type": "object", "properties": {}},
    )
    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="build the report")],
        tools=[tool],
        executor=_NoopExecutor(),
        max_iterations=5,
        on_event=_on_event,
        steering_queue=queue,
    )

    assert response.content == "final answer"
    second_call_user_contents = [m.content for m in llm.calls[1] if m.role == "user"]
    assert "actually, use the EMEA subregion filter" in second_call_user_contents
    assert "stale partner answer" not in second_call_user_contents
    assert queue.admitted_from == [
        ["stale partner answer", "actually, use the EMEA subregion filter"]
    ]
    steering_events = [e for e in events if e.get("status") == "steering"]
    assert [e.get("count") for e in steering_events] == [1]


@pytest.mark.asyncio
async def test_steering_files_registered_in_file_context_and_announced():
    """A file attached to a mid-run steering message must be exactly as
    usable as a file attached at turn start: registered into the
    run's ``FileContext`` (so tools can resolve its file_id) and announced
    to the model via a file-availability system message (the system
    prompt's "Available Files" section predates the run and would
    otherwise tell the model the file is NOT available)."""
    from app.llm.base import FileRef
    from app.tools.agent_loop import SteeringMessage
    from app.tools.base import FileContext

    llm = _SteeredStubLLM()
    queue: asyncio.Queue = asyncio.Queue()
    file_context = FileContext()
    steer_ref = FileRef(
        file_id="11111111-2222-3333-4444-555555555555",
        filename="notification-cnil.pdf",
        content_type="application/pdf",
        size=139776,
        s3_object_name="chat/notification-cnil.pdf",
    )

    async def _on_event(event: dict) -> None:
        if event.get("status") == "tool_start" and queue.empty():
            queue.put_nowait(
                SteeringMessage(
                    text="use this file and upload it appropriately",
                    files=[steer_ref],
                )
            )

    tool = ToolDefinition(
        name="noop_tool",
        description="No-op test tool",
        input_schema={"type": "object", "properties": {}},
    )
    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="fill the Notion page")],
        tools=[tool],
        executor=_FileAwareNoopExecutor(),
        max_iterations=5,
        on_event=_on_event,
        steering_queue=queue,
        file_context=file_context,
    )

    assert response.content == "final answer"
    # The steered file is resolvable by tools for the rest of the run.
    assert file_context.get(steer_ref.file_id) is steer_ref
    # The steering user message carries the attachment.
    steered_user = [
        m
        for m in llm.calls[1]
        if m.role == "user" and m.content.startswith("use this file")
    ]
    assert len(steered_user) == 1
    assert steered_user[0].files == [steer_ref]
    # The availability update supersedes the stale "Available Files" list.
    availability_notes = [
        m.content
        for m in llm.calls[1]
        if m.role == "system"
        and "[File availability update]" in (m.content or "")
    ]
    assert len(availability_notes) == 1
    assert steer_ref.file_id in availability_notes[0]
    assert "notification-cnil.pdf" in availability_notes[0]


@pytest.mark.asyncio
async def test_steering_file_already_known_is_not_reannounced():
    """A steering message re-attaching a file that is already in the
    FileContext must not duplicate the context entry nor emit the
    file-availability note (the file is already in the prompt's list)."""
    from app.llm.base import FileRef
    from app.tools.agent_loop import SteeringMessage
    from app.tools.base import FileContext

    llm = _SteeredStubLLM()
    queue: asyncio.Queue = asyncio.Queue()
    known_ref = FileRef(
        file_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        filename="already-known.pdf",
        content_type="application/pdf",
        size=1024,
        s3_object_name="chat/already-known.pdf",
    )
    file_context = FileContext.from_file_refs([known_ref])

    async def _on_event(event: dict) -> None:
        if event.get("status") == "tool_start" and queue.empty():
            queue.put_nowait(
                SteeringMessage(text="use the known file", files=[known_ref])
            )

    tool = ToolDefinition(
        name="noop_tool",
        description="No-op test tool",
        input_schema={"type": "object", "properties": {}},
    )
    await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="do the thing")],
        tools=[tool],
        executor=_FileAwareNoopExecutor(),
        max_iterations=5,
        on_event=_on_event,
        steering_queue=queue,
        file_context=file_context,
    )

    assert len(file_context.list_files()) == 1
    availability_notes = [
        m
        for m in llm.calls[1]
        if m.role == "system"
        and "[File availability update]" in (m.content or "")
    ]
    assert availability_notes == []


@pytest.mark.asyncio
async def test_drain_steering_queue_skips_blank_and_handles_none():
    queue: asyncio.Queue[str] = asyncio.Queue()
    queue.put_nowait("keep me")
    queue.put_nowait("   ")
    queue.put_nowait("")
    queue.put_nowait("and me")

    assert drain_steering_queue(queue) == ["keep me", "and me"]
    assert drain_steering_queue(queue) == []
    assert drain_steering_queue(None) == []


# ── Steering resume-promise guard ─────────────────────────────────────────
# A steered run must not end on a text-only answer that PROMISES to resume
# the pre-steer task ("Resuming the ComCyberMI draft now...") — the loop
# re-prompts once so the model either performs the remaining work or closes
# without promising imminent work.

_PROMISE_TEXT = (
    "BlackRock is now validated in the tracker. Resuming the ComCyberMI "
    "draft now, unless you want something else first."
)


class _ScriptedStubLLM(BaseLLMProvider):
    """Returns a scripted sequence of responses, capturing each call's messages."""

    provider_name = "stub"
    context_window = 50_000

    def __init__(self, script: list[ChatResponse]) -> None:
        self.script = list(script)
        self.calls: list[list[ChatMessage]] = []

    async def chat(
        self,
        messages,
        model=None,
        temperature=0.7,
        max_tokens=4096,
        tools=None,
        **kwargs,
    ):
        self.calls.append(list(messages))
        return self.script[len(self.calls) - 1]

    async def chat_stream(
        self,
        messages,
        model=None,
        temperature=0.7,
        max_tokens=4096,
        tools=None,
        **kwargs,
    ):
        yield "unused"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


def _tool_call_response(model: str | None) -> ChatResponse:
    return ChatResponse(
        content="",
        model=model or "stub",
        tool_calls=[ToolCall(id="tc-x", name="noop_tool", arguments={})],
    )


def _steering_harness(script: list[ChatResponse]):
    """Build (llm, queue, events, on_event, tool) with steering queued at the
    first ``tool_start`` event — the same pattern as the tests above."""
    llm = _ScriptedStubLLM(script)
    queue: asyncio.Queue[str] = asyncio.Queue()
    events: list[dict] = []
    steered_once = False

    async def _on_event(event: dict) -> None:
        nonlocal steered_once
        events.append(event)
        if event.get("status") == "tool_start" and not steered_once:
            steered_once = True
            queue.put_nowait("BlackRock is validated")

    tool = ToolDefinition(
        name="noop_tool",
        description="No-op test tool",
        input_schema={"type": "object", "properties": {}},
    )
    return llm, queue, events, _on_event, tool


def _nudge_messages(call_messages) -> list:
    from app.tools.agent_loop import _RESUME_PROMISE_NUDGE

    return [
        m
        for m in call_messages
        if m.role == "system" and m.content == _RESUME_PROMISE_NUDGE
    ]


@pytest.mark.asyncio
async def test_steered_resume_promise_nudges_once_then_model_finishes():
    """A steered run ending text-only on a resume promise is re-prompted:
    the model then makes the tool call and delivers the real final answer."""
    script = [
        _tool_call_response("stub"),  # iter 1: handle the original task
        ChatResponse(content=_PROMISE_TEXT, model="stub"),  # promise -> nudge
        _tool_call_response("stub"),  # post-nudge: actually resumes
        ChatResponse(content="ComCyberMI draft complete.", model="stub"),
    ]
    llm, queue, events, on_event, tool = _steering_harness(script)

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="Do ComCyberMI")],
        tools=[tool],
        executor=_NoopExecutor(),
        max_iterations=8,
        on_event=on_event,
        steering_queue=queue,
    )

    assert response.content == "ComCyberMI draft complete."
    assert len(llm.calls) == 4
    # The nudge was injected exactly once, visible from the third call on.
    assert len(_nudge_messages(llm.calls[2])) == 1
    assert len(_nudge_messages(llm.calls[3])) == 1
    assert _nudge_messages(llm.calls[1]) == []
    # The promise stays in the working transcript as an assistant turn.
    assert any(
        m.role == "assistant" and m.content == _PROMISE_TEXT
        for m in llm.calls[2]
    )
    # The continuation is announced as composing (not a turn-limit event).
    assert any(
        e.get("status") == "composing"
        and e.get("reason") == "resume_promise_continuation"
        for e in events
    )


@pytest.mark.asyncio
async def test_steered_resume_promise_nudge_fires_at_most_once():
    """If the model AGAIN answers text-only after the nudge, that answer is
    final — the guard never loops."""
    second_promise = "Understood. Resuming the ComCyberMI draft now."
    script = [
        _tool_call_response("stub"),
        ChatResponse(content=_PROMISE_TEXT, model="stub"),
        ChatResponse(content=second_promise, model="stub"),
    ]
    llm, queue, _events, on_event, tool = _steering_harness(script)

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="Do ComCyberMI")],
        tools=[tool],
        executor=_NoopExecutor(),
        max_iterations=8,
        on_event=on_event,
        steering_queue=queue,
    )

    assert response.content == second_promise
    assert len(llm.calls) == 3
    assert len(_nudge_messages(llm.calls[2])) == 1


@pytest.mark.asyncio
async def test_steered_text_only_without_promise_exits_normally():
    """A steered run whose final text does NOT promise to resume ends the
    turn with no nudge."""
    final_text = "BlackRock is validated and the ComCyberMI draft is complete."
    script = [
        _tool_call_response("stub"),
        ChatResponse(content=final_text, model="stub"),
    ]
    llm, queue, _events, on_event, tool = _steering_harness(script)

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="Do ComCyberMI")],
        tools=[tool],
        executor=_NoopExecutor(),
        max_iterations=8,
        on_event=on_event,
        steering_queue=queue,
    )

    assert response.content == final_text
    assert len(llm.calls) == 2
    assert all(_nudge_messages(call) == [] for call in llm.calls)


@pytest.mark.asyncio
async def test_non_steered_run_with_promise_text_is_not_nudged():
    """Without any steering injection (assignments, sub-agents, consults),
    promise-like closing text ends the turn as before."""
    script = [
        _tool_call_response("stub"),
        ChatResponse(content=_PROMISE_TEXT, model="stub"),
    ]
    llm = _ScriptedStubLLM(script)
    tool = ToolDefinition(
        name="noop_tool",
        description="No-op test tool",
        input_schema={"type": "object", "properties": {}},
    )

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="Do ComCyberMI")],
        tools=[tool],
        executor=_NoopExecutor(),
        max_iterations=8,
        steering_queue=None,
    )

    assert response.content == _PROMISE_TEXT
    assert len(llm.calls) == 2
    assert all(_nudge_messages(call) == [] for call in llm.calls)


@pytest.mark.asyncio
async def test_truncated_rescue_trace_reflects_continuation():
    """When the empty-response rescue is itself truncated (``max_tokens``)
    and the shared continuation block finishes it, the flagged
    ``empty_response_retry`` ``llm_step`` must be emitted AFTER the
    continuation and reflect the post-continuation state: the final
    stitched text, the final stop reason, and the summed usage of the
    empty primary attempt, the rescue call, and the continuation."""
    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    tool = ToolDefinition(
        name="noop_tool",
        description="No-op test tool",
        input_schema={"type": "object", "properties": {}},
    )
    script = [
        _tool_call_response("stub"),  # iter 1: a tool call
        # iter 2 primary: empty, but it consumed tokens.
        ChatResponse(
            content="",
            model="stub",
            usage={"input_tokens": 100, "output_tokens": 0},
        ),
        # Rescue: succeeds but is TRUNCATED.
        ChatResponse(
            content="Part one ",
            model="stub",
            stop_reason="max_tokens",
            usage={"input_tokens": 120, "output_tokens": 40},
        ),
        # Continuation: finishes the answer.
        ChatResponse(
            content="and part two.",
            model="stub",
            stop_reason="end_turn",
            usage={"input_tokens": 50, "output_tokens": 20},
        ),
    ]
    llm = _ScriptedStubLLM(script)

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="Do ComCyberMI")],
        tools=[tool],
        executor=_NoopExecutor(),
        max_iterations=8,
        on_event=_on_event,
    )

    assert response.content == "Part one and part two."
    assert len(llm.calls) == 4
    rescue_steps = [
        e
        for e in events
        if e.get("status") == "llm_step" and e.get("empty_response_retry")
    ]
    # Exactly one flagged step, reflecting the POST-continuation state.
    assert len(rescue_steps) == 1
    step = rescue_steps[0]
    assert step.get("llm_raw_response") == "Part one and part two."
    assert step.get("llm_stop_reason") == "end_turn"
    assert step.get("llm_usage") == {
        "input_tokens": 270,
        "output_tokens": 60,
    }


@pytest.mark.asyncio
async def test_steered_empty_then_promise_rescue_is_nudged():
    """The guard must also cover the empty-response rescue path: when the
    primary tool-enabled call returns empty and the no-tools retry succeeds
    with text that closes on a resume promise, the rescued text must flow
    through the shared no-tool finalization (not return early) so the
    steered run is still nudged to execute instead of abandoning the
    pre-steer task."""
    from app.tools.agent_loop import _EMPTY_RESPONSE_RETRY

    script = [
        _tool_call_response("stub"),  # iter 1: handle the original task
        ChatResponse(content="", model="stub"),  # iter 2 primary: empty
        ChatResponse(content=_PROMISE_TEXT, model="stub"),  # rescue -> promise
        _tool_call_response("stub"),  # post-nudge: actually resumes
        ChatResponse(content="ComCyberMI draft complete.", model="stub"),
    ]
    llm, queue, events, on_event, tool = _steering_harness(script)

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="Do ComCyberMI")],
        tools=[tool],
        executor=_NoopExecutor(),
        max_iterations=8,
        on_event=on_event,
        steering_queue=queue,
    )

    assert response.content == "ComCyberMI draft complete."
    assert len(llm.calls) == 5
    # The rescue call saw the empty-response recovery instruction.
    assert any(
        m.role == "system" and m.content == _EMPTY_RESPONSE_RETRY
        for m in llm.calls[2]
    )
    # The nudge fired exactly once, after the rescued promise.
    assert len(_nudge_messages(llm.calls[3])) == 1
    assert _nudge_messages(llm.calls[2]) == []
    # The rescued promise stays in the working transcript.
    assert any(
        m.role == "assistant" and m.content == _PROMISE_TEXT
        for m in llm.calls[3]
    )
    # The continuation is announced as composing (not a turn-limit event).
    assert any(
        e.get("status") == "composing"
        and e.get("reason") == "resume_promise_continuation"
        for e in events
    )
    # The non-streamed rescue also reports its own payload via the flagged
    # ``llm_step`` (it supersedes the stashed primary payload in the
    # labeller instead of patching an already-closed span).
    assert any(
        e.get("status") == "llm_step"
        and e.get("empty_response_retry")
        and e.get("llm_raw_response") == _PROMISE_TEXT
        for e in events
    )


@pytest.mark.asyncio
async def test_non_steered_empty_then_promise_rescue_returns_promise():
    """Routing the rescue through the shared finalization must not change
    the empty-retry semantics for non-steered runs: the rescued text is the
    final answer, exactly as before."""
    script = [
        _tool_call_response("stub"),
        ChatResponse(content="", model="stub"),
        ChatResponse(content=_PROMISE_TEXT, model="stub"),
    ]
    llm = _ScriptedStubLLM(script)
    tool = ToolDefinition(
        name="noop_tool",
        description="No-op test tool",
        input_schema={"type": "object", "properties": {}},
    )

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="Do ComCyberMI")],
        tools=[tool],
        executor=_NoopExecutor(),
        max_iterations=8,
        steering_queue=None,
    )

    assert response.content == _PROMISE_TEXT
    assert len(llm.calls) == 3
    assert all(_nudge_messages(call) == [] for call in llm.calls)


# Confirmed stall-detector positive (see _UNFINISHED in
# test_agent_loop_stall_guard.py) that is NOT a resume promise.
_STALLY_TEXT = "I'll submit these now."


def _stall_nudge_messages(call_messages) -> list:
    from app.tools.agent_loop import _STALL_CONTINUATION_NUDGE

    return [
        m
        for m in call_messages
        if m.role == "system" and m.content == _STALL_CONTINUATION_NUDGE
    ]


@pytest.mark.asyncio
async def test_non_steered_stally_rescue_is_terminal_without_stall_nudge():
    """A successful empty-response rescue was terminal before the guard
    route-through, and non-steered runs must keep that contract: rescued
    text that MATCHES the stall detector (prior tool call sets
    ``had_tool_calls_this_run``) must still return immediately, without
    spending a stall-nudge LLM round on a context that just returned
    empty."""
    script = [
        _tool_call_response("stub"),  # sets had_tool_calls_this_run
        ChatResponse(content="", model="stub"),  # iter 2 primary: empty
        ChatResponse(content=_STALLY_TEXT, model="stub"),  # rescue: stall-y
    ]
    llm = _ScriptedStubLLM(script)
    tool = ToolDefinition(
        name="noop_tool",
        description="No-op test tool",
        input_schema={"type": "object", "properties": {}},
    )

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="Do ComCyberMI")],
        tools=[tool],
        executor=_NoopExecutor(),
        max_iterations=8,
        steering_queue=None,
    )

    assert response.content == _STALLY_TEXT
    assert len(llm.calls) == 3
    assert all(_stall_nudge_messages(call) == [] for call in llm.calls)


@pytest.mark.asyncio
async def test_steered_stally_rescue_without_promise_is_terminal():
    """The stall-guard exemption applies to steered runs too: a rescued
    answer that matches the stall detector but does NOT promise to resume
    steered-over work is terminal — only the resume-promise guard applies
    to rescued text."""
    script = [
        _tool_call_response("stub"),  # iter 1: steered at tool_start
        ChatResponse(content="", model="stub"),  # iter 2 primary: empty
        ChatResponse(content=_STALLY_TEXT, model="stub"),  # rescue: stall-y
    ]
    llm, queue, _events, on_event, tool = _steering_harness(script)

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="Do ComCyberMI")],
        tools=[tool],
        executor=_NoopExecutor(),
        max_iterations=8,
        on_event=on_event,
        steering_queue=queue,
    )

    assert response.content == _STALLY_TEXT
    assert len(llm.calls) == 3
    assert all(_stall_nudge_messages(call) == [] for call in llm.calls)
    assert all(_nudge_messages(call) == [] for call in llm.calls)


@pytest.mark.asyncio
async def test_non_rescued_stally_answer_still_triggers_stall_guard():
    """Contrast case locking the exemption's scope: the SAME stall-y text
    from an ordinary (non-rescued) no-tool answer still triggers the stall
    guard — the exemption keys on the rescue, not on the text."""
    script = [
        _tool_call_response("stub"),  # sets had_tool_calls_this_run
        ChatResponse(content=_STALLY_TEXT, model="stub"),  # ordinary stall
        _tool_call_response("stub"),  # post-nudge: actually executes
        ChatResponse(content="Submitted. All done.", model="stub"),
    ]
    llm = _ScriptedStubLLM(script)
    tool = ToolDefinition(
        name="noop_tool",
        description="No-op test tool",
        input_schema={"type": "object", "properties": {}},
    )

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="Do ComCyberMI")],
        tools=[tool],
        executor=_NoopExecutor(),
        max_iterations=8,
        steering_queue=None,
    )

    assert response.content == "Submitted. All done."
    assert len(llm.calls) == 4
    assert len(_stall_nudge_messages(llm.calls[2])) == 1


@pytest.mark.asyncio
async def test_promise_rescue_resets_consecutive_empty_counter():
    """A content-bearing rescue breaks the empty streak, so it must reset
    ``consecutive_empty_responses``. This used to be moot (the successful
    rescue returned immediately); now that it falls through so the
    resume-promise guard can re-prompt, a stale count from earlier
    double-empty rounds would let post-nudge empty rounds hit the
    forced-answer backstop prematurely even though the empties are no
    longer consecutive."""
    from app.tools.agent_loop import _EMPTY_RESPONSE_RETRY_ESCALATION

    final_text = "ComCyberMI draft complete."
    script = [
        _tool_call_response("stub"),  # iter 1: steered at tool_start
        ChatResponse(content="", model="stub"),  # iter 2 primary: empty
        ChatResponse(content="", model="stub"),  # iter 2 retry: empty (streak 1)
        ChatResponse(content="", model="stub"),  # iter 3 primary: empty
        ChatResponse(content="", model="stub"),  # iter 3 retry: empty (streak 2)
        ChatResponse(content="", model="stub"),  # iter 4 primary: empty
        ChatResponse(content=_PROMISE_TEXT, model="stub"),  # rescue -> reset
        ChatResponse(content="", model="stub"),  # iter 5 primary: empty
        ChatResponse(content="", model="stub"),  # iter 5 retry: empty (streak 1)
        ChatResponse(content=final_text, model="stub"),  # iter 6: real answer
    ]
    llm, queue, _events, on_event, tool = _steering_harness(script)

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="Do ComCyberMI")],
        tools=[tool],
        executor=_NoopExecutor(),
        max_iterations=12,
        on_event=on_event,
        steering_queue=queue,
    )

    assert response.content == final_text
    assert len(llm.calls) == 10
    # The nudge fired once, right after the rescued promise.
    assert len(_nudge_messages(llm.calls[7])) == 1
    # THREE double-empty escalations reached the transcript (iterations 2, 3
    # and 5): the post-rescue round counted from ZERO. With a stale count,
    # iteration 5 would have been the third consecutive empty round and the
    # run would have broken to the forced-answer backstop instead of
    # escalating and letting iteration 6 answer.
    escalations = [
        m
        for m in llm.calls[-1]
        if m.role == "system" and m.content == _EMPTY_RESPONSE_RETRY_ESCALATION
    ]
    assert len(escalations) == 3


class _ScriptedStreamStubLLM(BaseLLMProvider):
    """Streaming-capable scripted stub: tool-enabled iterations go through
    ``chat_stream_with_tools`` (streaming the scripted content through the
    hold-back window) and the empty-response rescue's plain streaming call
    goes through ``chat_stream``. Each call consumes the next scripted
    response, whichever entry point served it."""

    provider_name = "stub"
    context_window = 50_000

    def __init__(self, script: list[ChatResponse]) -> None:
        self.script = list(script)
        self.calls: list[list[ChatMessage]] = []

    def _next(self) -> ChatResponse:
        return self.script[len(self.calls) - 1]

    async def chat_stream_with_tools(
        self, messages, tools=None, *, on_token=None, model=None, **kwargs
    ):
        self.calls.append(list(messages))
        resp = self._next()
        if resp.content and on_token is not None:
            await on_token(resp.content)
        return resp

    async def chat(self, messages, model=None, **kwargs):
        self.calls.append(list(messages))
        return self._next()

    async def chat_stream(self, messages, model=None, **kwargs):
        self.calls.append(list(messages))
        resp = self._next()
        # Real providers park the streamed call's usage and stop reason on
        # ``last_usage`` / ``last_stop_reason``; ``_streaming_chat`` rebuilds
        # the ChatResponse from them.
        self.last_usage = resp.usage or {}
        self.last_stop_reason = resp.stop_reason
        if resp.content:
            yield resp.content

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.mark.asyncio
async def test_steered_streamed_promise_rescue_is_retracted_then_nudged():
    """Streaming variant of the empty-then-promise rescue: the rescue call
    streams its text live (plain streaming, no hold-back window), so when
    the guard re-prompts it must RETRACT the already-visible provisional
    bubble (``stream_retract``), re-surface the promise as ``thinking_text``,
    and only then continue — the post-nudge final answer is what the user
    ends up with in the answer stream."""
    final_text = "ComCyberMI draft complete."
    script = [
        _tool_call_response("stub"),  # iter 1: handle the original task
        # iter 2 primary: empty — but it still consumed tokens.
        ChatResponse(
            content="",
            model="stub",
            usage={"input_tokens": 100, "output_tokens": 0},
        ),
        # rescue -> promise, with its own spend.
        ChatResponse(
            content=_PROMISE_TEXT,
            model="stub",
            usage={"input_tokens": 120, "output_tokens": 40},
        ),
        _tool_call_response("stub"),  # post-nudge: actually resumes
        ChatResponse(content=final_text, model="stub"),
    ]
    llm = _ScriptedStreamStubLLM(script)
    queue: asyncio.Queue[str] = asyncio.Queue()
    events: list[dict] = []
    tokens: list[str] = []
    steered_once = False

    async def on_event(event: dict) -> None:
        nonlocal steered_once
        events.append(event)
        if event.get("status") == "tool_start" and not steered_once:
            steered_once = True
            queue.put_nowait("BlackRock is validated")

    async def on_token(chunk: str) -> None:
        tokens.append(chunk)

    tool = ToolDefinition(
        name="noop_tool",
        description="No-op test tool",
        input_schema={"type": "object", "properties": {}},
    )

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="Do ComCyberMI")],
        tools=[tool],
        executor=_NoopExecutor(),
        max_iterations=8,
        on_event=on_event,
        on_token=on_token,
        steering_queue=queue,
    )

    assert response.content == final_text
    assert len(llm.calls) == 5
    # The rescued promise streamed live, then was retracted.
    streamed = "".join(tokens)
    assert _PROMISE_TEXT in streamed
    retract_idx = next(
        i for i, e in enumerate(events) if e.get("status") == "stream_retract"
    )
    # The retracted text re-surfaces in the reasoning pane.
    thinking = "".join(
        str(e.get("content") or "")
        for e in events
        if e.get("status") == "thinking_text"
    )
    assert _PROMISE_TEXT in thinking
    # Retract precedes the continuation announcement, which precedes the
    # final answer reaching the stream.
    composing_idx = next(
        i
        for i, e in enumerate(events)
        if e.get("status") == "composing"
        and e.get("reason") == "resume_promise_continuation"
    )
    assert retract_idx < composing_idx
    # The user's answer stream ends with the real final answer (replayed at
    # loop end — it stayed under the hold-back window).
    assert streamed.endswith(final_text)
    # The nudge fired exactly once, after the rescued promise.
    assert len(_nudge_messages(llm.calls[3])) == 1
    # Real event sequence for the eval labeller: the pre-rescue ``streaming``
    # event provisionally closes the span with the PRIMARY (empty) call's
    # payload, so the rescue must report its own payload via a flagged
    # ``llm_step`` — after that ``streaming``, before the continuation
    # announcement — carrying the rescued text the primary step lacked.
    rescue_step_idx = next(
        i
        for i, e in enumerate(events)
        if e.get("status") == "llm_step" and e.get("empty_response_retry")
    )
    rescue_step = events[rescue_step_idx]
    assert rescue_step.get("llm_raw_response") == _PROMISE_TEXT
    # The span covers both attempts, so the flagged step reports the SUMMED
    # usage — an empty primary response still consumed tokens.
    assert rescue_step.get("llm_usage") == {
        "input_tokens": 220,
        "output_tokens": 40,
    }
    assert any(
        e.get("status") == "streaming" for e in events[:rescue_step_idx]
    )
    assert rescue_step_idx < composing_idx
    # The primary call's own llm_step (same iteration, unflagged) carried an
    # empty raw response — the flagged step is what makes the span truthful.
    primary_steps = [
        e
        for e in events
        if e.get("status") == "llm_step"
        and not e.get("empty_response_retry")
        and e.get("iteration") == rescue_step.get("iteration")
    ]
    assert primary_steps and all(
        not (s.get("llm_raw_response") or "") for s in primary_steps
    )


@pytest.mark.asyncio
async def test_steered_streamed_promise_is_kept_as_a_segment_with_deliver_segments():
    """Twin of the test above: with ``deliver_segments`` on (WS chat)
    the streamed promise the user already read is NOT taken back — it is
    handed over as a completed segment, never retracted and never re-emitted
    as reasoning prose — and the nudge still fires so the post-nudge answer
    streams as the turn's own final content."""
    final_text = "ComCyberMI draft complete."
    script = [
        _tool_call_response("stub"),
        ChatResponse(content="", model="stub"),
        ChatResponse(content=_PROMISE_TEXT, model="stub"),
        _tool_call_response("stub"),
        ChatResponse(content=final_text, model="stub"),
    ]
    llm = _ScriptedStreamStubLLM(script)
    queue: asyncio.Queue[str] = asyncio.Queue()
    events: list[dict] = []
    tokens: list[str] = []
    steered_once = False

    async def on_event(event: dict) -> None:
        nonlocal steered_once
        events.append(event)
        if event.get("status") == "tool_start" and not steered_once:
            steered_once = True
            queue.put_nowait("BlackRock is validated")

    async def on_token(chunk: str) -> None:
        tokens.append(chunk)

    tool = ToolDefinition(
        name="noop_tool",
        description="No-op test tool",
        input_schema={"type": "object", "properties": {}},
    )

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="Do ComCyberMI")],
        tools=[tool],
        executor=_NoopExecutor(),
        max_iterations=8,
        on_event=on_event,
        on_token=on_token,
        steering_queue=queue,
        deliver_segments=True,
    )

    assert response.content == final_text
    statuses = [e.get("status") for e in events]
    assert "stream_retract" not in statuses
    segments = [
        e.get("content") for e in events if e.get("status") == "segment_complete"
    ]
    assert segments == [_PROMISE_TEXT]
    thinking = "".join(
        str(e.get("content") or "")
        for e in events
        if e.get("status") == "thinking_text"
    )
    assert _PROMISE_TEXT not in thinking
    # Segment first, then the continuation announcement.
    assert statuses.index("segment_complete") < next(
        i
        for i, e in enumerate(events)
        if e.get("status") == "composing"
        and e.get("reason") == "resume_promise_continuation"
    )
    assert "".join(tokens).endswith(final_text)
    assert len(_nudge_messages(llm.calls[3])) == 1


@pytest.mark.asyncio
async def test_truncated_streamed_rescue_step_follows_every_streaming_event():
    """Streamed variant of the truncated-rescue trace: the loop emits
    ``streaming`` once before the rescue call AND once before every
    max_tokens continuation call, and the flagged ``empty_response_retry``
    ``llm_step`` only after the LAST of them. This ordering is exactly why
    the eval labeller must measure the rescue span from the timestamp
    latched when the primary span was provisionally closed (the FIRST of
    those ``streaming`` events) rather than from the repeatedly-overwritten
    ``_eval_streaming_at``, which by then covers only the final
    continuation call. The flagged step still reflects the
    post-continuation state: final stitched text, final stop reason, and
    the summed usage of all three attempts."""
    events: list[dict] = []
    tokens: list[str] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    async def _on_token(chunk: str) -> None:
        tokens.append(chunk)

    tool = ToolDefinition(
        name="noop_tool",
        description="No-op test tool",
        input_schema={"type": "object", "properties": {}},
    )
    script = [
        _tool_call_response("stub"),  # iter 1: a tool call
        # iter 2 primary: empty, but it consumed tokens.
        ChatResponse(
            content="",
            model="stub",
            usage={"input_tokens": 100, "output_tokens": 0},
        ),
        # Rescue: streams live but is TRUNCATED.
        ChatResponse(
            content="Part one ",
            model="stub",
            stop_reason="max_tokens",
            usage={"input_tokens": 120, "output_tokens": 40},
        ),
        # Continuation: streams the rest.
        ChatResponse(
            content="and part two.",
            model="stub",
            stop_reason="end_turn",
            usage={"input_tokens": 50, "output_tokens": 20},
        ),
    ]
    llm = _ScriptedStreamStubLLM(script)

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="Do ComCyberMI")],
        tools=[tool],
        executor=_NoopExecutor(),
        max_iterations=8,
        on_event=_on_event,
        on_token=_on_token,
    )

    assert response.content == "Part one and part two."
    assert len(llm.calls) == 4
    flagged_idx = [
        i
        for i, e in enumerate(events)
        if e.get("status") == "llm_step" and e.get("empty_response_retry")
    ]
    assert len(flagged_idx) == 1
    step = events[flagged_idx[0]]
    assert step.get("llm_raw_response") == "Part one and part two."
    assert step.get("llm_stop_reason") == "end_turn"
    assert step.get("llm_usage") == {
        "input_tokens": 270,
        "output_tokens": 60,
    }
    # One ``streaming`` before the rescue, one before the continuation —
    # and the flagged step after ALL of them (the labeller hazard).
    streaming_idx = [
        i for i, e in enumerate(events) if e.get("status") == "streaming"
    ]
    assert len(streaming_idx) >= 2
    assert flagged_idx[0] > streaming_idx[-1]


@pytest.mark.asyncio
async def test_promise_at_last_tool_enabled_iteration_is_not_nudged():
    """The nudge asks for tool calls, so it must only fire when the
    CONTINUATION round is still tool-enabled. With ``max_iterations=4``,
    ``strip_tools_at == 2`` — tools are offered on 0-based iterations 0
    and 1 only — so a promise at iteration 1, the last tool-enabled
    round, ends the turn instead of nudging into a tool-stripped
    continuation."""
    script = [
        _tool_call_response("stub"),  # iter 1: tools available
        ChatResponse(content=_PROMISE_TEXT, model="stub"),  # last tool round
    ]
    llm, queue, _events, on_event, tool = _steering_harness(script)

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="Do ComCyberMI")],
        tools=[tool],
        executor=_NoopExecutor(),
        max_iterations=4,
        on_event=on_event,
        steering_queue=queue,
    )

    assert response.content == _PROMISE_TEXT
    assert len(llm.calls) == 2
    assert all(_nudge_messages(call) == [] for call in llm.calls)


def test_resume_promise_reason_is_a_continuation_reason():
    """The truncation latches (chat WS, platform proxy) and the eval-trace
    labeller single-source their "this composing is a re-prompt, not a
    wind-down" exclusions from ``CONTINUATION_COMPOSING_REASONS``. The
    resume-promise re-prompt must be listed there, or those paths would flag
    the turn as "turn limit reached" even though the run continues with its
    full budget."""
    from app.tools.agent_loop import CONTINUATION_COMPOSING_REASONS

    assert "resume_promise_continuation" in CONTINUATION_COMPOSING_REASONS
    assert "stall_continuation" in CONTINUATION_COMPOSING_REASONS


class TestEndsWithResumePromise:
    """Unit coverage for the deterministic resume-promise detector."""

    @pytest.mark.parametrize(
        "text",
        [
            "Resuming the ComCyberMI draft now, unless you want something else first.",
            "Noted. I'll resume the ComCyberMI draft.",
            "I will resume where I left off.",
            "Recorded. Let me get back to the ComCyberMI draft.",
            "Now resuming the analysis.",
            "Continuing with the report now.",
            # Anchored gerund sign-offs: the bare alternatives require a
            # first-person progressive or "now" lead — those forms cannot
            # take a finite predicate, so they stay unambiguous sign-offs.
            "I'm getting back to the draft.",
            "Now getting back to the draft.",
            "We're picking the deployment back up.",
            "Back to the draft now.",
            "I'll continue with the remaining sections.",
            "Done with that. I'll pick the draft back up.",
            "I'm resuming the draft now.",
            "OK, resuming the validation work now.",
            "I'm back to the draft now.",
            # Vague imminence is NOT a deferral: an unqualified promise the
            # turn cannot keep is exactly the hollow sign-off the guard
            # exists for ("moment" is deliberately absent from the
            # relative-time unit list).
            "I'll resume the draft shortly.",
            "I'll resume the draft in a moment.",
            # A sentence-final plural is a bare noun phrase, not a finite
            # predicate — the structural mid-remainder -s belt must not
            # reject it.
            "Now resuming the remaining sections.",
            # A quoted NAME inside the assistant's own promise is masked
            # away without breaking the promise around it.
            "I'll resume the \u201cComCyberMI\u201d draft now.",
            # UNQUOTED positive control for the quoted-span mask: the same
            # promise asserted directly (not inside quotation marks) must
            # keep tripping the guard.
            "I'll resume the deployment now.",
            # The deferral veto's wider window is BOUNDED (closing sentence
            # plus two before it): a time mentioned three sentences back —
            # about something else entirely — must not veto an immediate
            # closing promise.
            "The report was due tomorrow. It shipped early. All checks passed. I'll resume the draft now.",
            # The negation veto is CLAUSE-scoped: a negation before a
            # comma/semicolon boundary belongs to an earlier clause and must
            # not veto the genuine promise after it.
            "No problem, I'll resume the draft now.",
            "That issue won't block us; I'll resume the draft now.",
            # The first-person branch stays clause-initial-or-coordinated:
            # a promise after a coordinating conjunction or a dash is the
            # assistant's own commitment.
            "BlackRock validated and I'll resume the draft now.",
            "Understood - I'll resume the draft now.",
        ],
    )
    def test_positives(self, text: str) -> None:
        from app.tools.agent_loop import _ends_with_resume_promise

        assert _ends_with_resume_promise(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "The draft is complete.",
            "Let me know if you want anything else.",
            "Want me to resume the draft?",
            "Should I continue with the draft?",
            "Resuming the draft now - or shall I do something else?",
            "I'll get back to you shortly.",
            "Let me get back to you with the results.",
            "I resumed the draft earlier and it is attached above.",
            "After resuming the sync, I found two errors and stopped.",
            "I'll continue to monitor the situation.",
            "The ComCyberMI draft has been delivered to your inbox.",
            # Completed-work recaps: the promise-like phrase sits in an
            # EARLIER sentence, the closing sentence states completion —
            # one per pattern alternative (closing-sentence anchoring).
            "Getting back to the draft, I completed every section. The deliverable is attached.",
            "Resuming the ComCyberMI draft now took only a moment. The full draft is attached above.",
            "Resuming the draft was quick. Every section is complete.",
            "I said I'll resume the draft, and I did. It is attached above.",
            "Let me get back to the draft, I noted earlier - and I did. The final version is attached.",
            "Continuing with the report now proved unnecessary. It was already finished.",
            "Picking the draft back up earlier let me finish it. All sections are complete.",
            "Back to the draft now, I wrote above. Since then I completed and delivered it.",
            # Same-sentence completion language: "resuming ... now" /
            # "back to ... now" mid-clause after a past-tense verb is a
            # recap of finished work, not a promise (clause-initial
            # anchoring on those alternatives).
            "I finished resuming the draft just now.",
            "I got back to the draft just now.",
            # Same-sentence completion veto — one single-sentence recap per
            # pattern alternative: the clause AFTER the promise-shaped
            # phrase states the work is done, so no promise.
            "Now resuming was unnecessary; everything is already done.",
            "Resuming the report early let me get it fully finished.",
            "Let me get back to the draft, I said, and it is already delivered.",
            "Continuing with the report now is unnecessary because it is already complete.",
            "Getting back to the draft, I completed every section.",
            "Picking the draft back up earlier let me finish it.",
            "Back to the draft now that everything is finished.",
            # Sign-off shape constraint on "getting back to" / "picking ...
            # back up": anchorless forms never match, and even anchored
            # forms are rejected by a further clause after the phrase
            # (completed recap) or a copula/modal in the remainder
            # (blocked closing).
            "Getting back to the draft, I wrote every section and sent the final version.",
            "Getting back to the draft would be unsafe, so I need your approval.",
            "I'm getting back to the draft, I wrote every section earlier.",
            # Explicitly deferred or prerequisite-gated commitments: the
            # closing names a future time or a gate, so acting NOW (what the
            # nudge asks for) would be premature — deferral veto.
            "The maintenance window opens tomorrow. I will resume the deployment tomorrow.",
            "I need your approval first. I'll continue with the migration after you approve it.",
            "I'll pick the deployment work back up next week.",
            "Once you confirm the scope, I'll resume the migration.",
            "I'll resume the rollout after your approval.",
            # Approval gates named after any short holder — not only
            # "your" / "the user's" — and explicit need/await-approval
            # clauses in the same closing sentence.
            "I'll resume the deployment following security approval.",
            "I'll resume the deployment with CAB approval.",
            "Resuming the deployment now, but I need your approval first.",
            # Unmodified forms: no named gate-holder at all.
            "I'll resume the deployment with approval.",
            "I'll resume the rollout following approval.",
            # Prerequisites stated in the sentence(s) JUST BEFORE the
            # promise: the promise match stays anchored to the closing
            # sentence, but the deferral veto scans the two sentences
            # before it too — one veto-in-earlier-sentence case per class
            # (need/await clause, time, conditional marker, approval gate).
            "I need your approval first. I'll resume the deployment.",
            "The maintenance window opens tomorrow. I'll resume the deployment.",
            "Once the CAB signs off, we are clear. I'll resume the deployment.",
            "This change ships only with CAB approval. I'll resume the deployment.",
            # General conditional markers and relative time: the gate does
            # not have to be the user — any "after/when/pending <something>"
            # clause or relative-time expression defers the commitment.
            "I'll resume the deployment after the maintenance window opens.",
            "I'll resume the deployment in 30 minutes.",
            "I'll continue with the rollout pending the security review.",
            "I'll pick the migration back up when the window reopens.",
            # Bare relative deferral: no fixed time, but still not imminent.
            "I'll resume the deployment later.",
            # Part-of-day and absolute-date deferrals.
            "I'll resume the rollout this afternoon.",
            "I'll resume the rollout on September 10.",
            # Worded quantities and named clock times defer like digits do.
            "I'll resume the deployment in two hours.",
            "I'll resume the deployment at noon.",
            "I'll resume the rollout by end of day.",
            # "within" is the same commitment shape as "in", and business
            # days the same unit as days.
            "I'll resume the deployment within 30 minutes.",
            "I'll resume the deployment in two business days.",
            # Quoted closing questions: the question veto must see through
            # trailing quote/markdown delimiters.
            "\u201cI'll resume the draft?\u201d",
            "\"I'll resume the draft?\"",
            # Blocked/modal-state closings: a copula or modal after the
            # promise-shaped phrase reasons about the action instead of
            # committing to it — shared veto across ALL alternatives, not
            # only the getting/picking branch.
            "Resuming the deployment now would be unsafe.",
            "Continuing with the deployment now would violate the maintenance policy.",
            # Contracted copula/modal ("isn't", straight or curly apostrophe)
            # is the same blocked-state shape.
            "Resuming the deployment now isn't safe.",
            "Resuming the deployment now isn\u2019t safe.",
            # Gerund-as-SUBJECT warnings: a finite lexical predicate after
            # the gerund phrase means the sentence warns about the action.
            # The clause-closing "now" anchor rejects the "... now <verb>"
            # forms structurally; the anchorless gerund shapes below no
            # longer match any alternative at all (sign-off anchor
            # required).
            "Resuming the deployment now risks corrupting the database.",
            "Continuing with the migration now violates the maintenance policy.",
            "Resuming the deployment risks corrupting the database.",
            "Getting back to the deployment risks losing the session.",
            "Picking the deployment back up risks breaking the freeze.",
            "Resuming the draft now seems risky.",
            # Anchorless gerund subjects: warnings and completed/failure
            # reports of every tense and verb class. The bare alternatives
            # require a first-person or "now" sign-off anchor precisely
            # because no verb list (or -s/-ed shape check) can enumerate
            # the finite predicates that follow a gerund SUBJECT — these
            # never match at all now, whatever the verb.
            "Resuming the deployment incurs downtime.",
            "Resuming the deployment triggers an outage.",
            "Resuming the deployment triggered an outage.",
            "Getting back to the deployment triggered an outage.",
            "Picking the deployment back up caused data loss.",
            "Resuming the deployment broke the replication.",
            "Picking the deployment back up put production at risk.",
            "Getting back to the deployment hurt production.",
            "Resuming the deployment cost us the maintenance window.",
            "Resuming the deployment took down the cluster.",
            # Bare irregular pasts no list could ever be complete for —
            # the review examples that motivated requiring the anchor.
            "Resuming the deployment ran the migration twice.",
            "Resuming the deployment froze production.",
            "Picking the deployment back up sent duplicate notifications.",
            # Structural belt on the ANCHORED gerund alternatives: even with
            # the anchor (where a finite predicate is ungrammatical), a
            # mid-remainder -s token still vetoes — models write
            # ungrammatical prose.
            "Now resuming the deployment causes an outage.",
            # ACCEPTED false negatives: anchorless gerund sign-offs are
            # syntactically identical to report subjects, so the detector
            # deliberately declines them — missing a hollow sign-off only
            # leaves the pre-existing behaviour in place (the cheap side),
            # while matching a report would retract completed work and
            # nudge duplicate side effects.
            "BlackRock validated. Resuming the draft.",
            "Resuming the remaining sections.",
            "Getting back to the draft.",
            "Picking the ComCyberMI work back up.",
            # Numeric/ISO date deferrals.
            "I'll resume the rollout on 2026-09-10.",
            "I'll resume the rollout on 10/09.",
            # Quoted/reported promises: the quoted span is masked before the
            # promise search, so an explicit cancellation of a quoted
            # promise is never itself treated as the promise.
            "Please disregard \u201cI'll resume the deployment.\u201d",
            # The exact reported form with a CURLY apostrophe inside the
            # curly quotes: the mask must not depend on the contraction's
            # apostrophe style.
            "Please disregard \u201cI\u2019ll resume the deployment.\u201d",
            'Please disregard "I\'ll resume the deployment."',
            # Negated/declined commitments: the negation sits in the same
            # clause BEFORE the promise-shaped phrase, where the remainder
            # vetoes cannot see it — pre-clause negation veto (straight or
            # curly apostrophe, and full-word "cannot"/"not").
            "I can't promise I'll resume the deployment.",
            "I can\u2019t promise I\u2019ll resume the deployment.",
            "I cannot promise I'll resume the deployment.",
            "I don't think I'll resume the deployment.",
            "We are not now resuming the deployment.",
            # Subordinate reported text: a promise phrase governed by a
            # third-party verb is not the assistant's own commitment — the
            # first-person and "continuing ... now" branches are
            # clause-initial (or conjunction-led), so these never match.
            "The audit incorrectly claims I'll resume the deployment.",
            "The runbook says continuing with the rollout now.",
        ],
    )
    def test_negatives(self, text: str) -> None:
        from app.tools.agent_loop import _ends_with_resume_promise

        assert _ends_with_resume_promise(text) is False


def test_retracted_streamed_rescue_relabels_eval_trace_span():
    """The eval-trace ``streaming`` branch records a provisionally streamed
    answer as "agent produces final answer" and clears the span timer, so a
    guard continuation arriving afterwards has no open span to close. The
    relabel helper must rewrite the most recent LLM span in place for
    continuation reasons — and leave the trace alone for wind-down
    ``composing`` events, which are genuine final answers."""
    from app.services.agent_chat import _relabel_retracted_llm_span

    traces = [
        {"type": "tool", "description": "Tool call: noop_tool"},
        {"type": "llm", "description": "LLM call #3 — agent produces final answer"},
    ]
    assert _relabel_retracted_llm_span(traces, 3, "resume_promise_continuation")
    assert "resume-promise guard" in traces[1]["description"]
    assert "retracted" in traces[1]["description"]
    # The tool span before it is untouched.
    assert traces[0]["description"] == "Tool call: noop_tool"

    # The stall guard's continuation reason relabels too (shared set).
    stall = [{"type": "llm", "description": "LLM call #2 — direct response"}]
    assert _relabel_retracted_llm_span(stall, 2, "stall_continuation")
    assert "stall guard" in stall[0]["description"]

    # A wind-down composing (no reason) is a genuine final answer: no rewrite.
    final = [{"type": "llm", "description": "LLM call #4 — agent produces final answer"}]
    assert not _relabel_retracted_llm_span(final, 4, None)
    assert final[0]["description"].endswith("final answer")

    # No LLM span at all: nothing to relabel, no crash.
    assert not _relabel_retracted_llm_span(
        [{"type": "tool", "description": "Tool call: x"}], 1, "stall_continuation"
    )


def test_patch_rescued_llm_span_folds_rescue_payload_into_closed_span():
    """On the streamed rescue path the labeller closes call #N's span with
    the PRIMARY (empty) call's payload before the rescue even runs. The
    patch helper must fold the rescue's own payload (reported via the
    flagged ``llm_step``) into that span — model, stop reason, usage,
    prompts, raw response — leaving its duration as recorded, so the trace
    never shows an empty raw_response for a call
    that produced the (retracted or final) answer."""
    from app.services.agent_chat import _patch_rescued_llm_span

    traces = [
        {"type": "tool", "description": "Tool call: noop_tool"},
        {
            "type": "llm",
            "description": "LLM call #3 — agent produces final answer",
            "duration_ms": 120,
            "model": "primary-model",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10},
            "raw_response": "",
        },
    ]
    payload = {
        "model": "rescue-model",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 900, "output_tokens": 40},
        "system_prompt": "sys",
        "user_prompt": "user",
        "raw_response": "Resuming the draft now.",
        "iteration": 2,
    }
    assert _patch_rescued_llm_span(traces, payload)
    span = traces[1]
    assert span["raw_response"] == "Resuming the draft now."
    assert span["model"] == "rescue-model"
    assert span["usage"] == {"input_tokens": 900, "output_tokens": 40}
    assert span["system_prompt"] == "sys"
    assert span["user_prompt"] == "user"
    assert span["iteration"] == 2
    assert span["duration_ms"] == 120
    # The description is untouched — relabelling (if a guard retracts the
    # answer afterwards) is _relabel_retracted_llm_span's job.
    assert span["description"].endswith("final answer")
    # The tool span before it is untouched.
    assert traces[0]["description"] == "Tool call: noop_tool"

    # No LLM span at all: nothing to patch, no crash.
    assert not _patch_rescued_llm_span(
        [{"type": "tool", "description": "Tool call: x"}], payload
    )
