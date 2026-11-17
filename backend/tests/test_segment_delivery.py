"""Mid-turn segment delivery instead of retraction.

Since wake-on-first-finished-task, report-then-keep-waiting is
the COMMON path for multi-delegation turns: the parent wakes on the first
finished task, streams a full report to the user, then keeps waiting for the
remaining delegations.  The retract paths — designed for short pre-tool
preambles and timeout-retry duplicate avoidance — were erasing that report
from the chat.

Contract pinned here (``deliver_segments`` opt-in on ``run_agent_loop``):

* Flushed live-streamed answer + a wait-family-only tool round (``wait`` /
  ``check_task_status``) -> ``segment_complete`` with the full text, NO
  ``stream_retract``, and the text is NOT re-emitted as ``thinking_text``
  (it is a real message now).  With the flag off, the old behavior
  (retract + thinking_text) is byte-identical.
* Implicit-park site (text-only response while spawned tasks are live) ->
  ``segment_complete`` instead of retract when opted in.
* Budget-exhausted park-return -> the flushed report becomes a segment and
  the ``waiting_on_tasks_message`` park text still streams as the turn's own
  final content (no duplication: the segment carries the report only).
* Flushed answer followed by ANY other tool call (``store_memory``,
  ``search_knowledge``, MCP tools...) -> ``segment_complete`` too:
  shown text is never taken back.  Retracting it into the reasoning pane
  made the answer visibly vanish and come back merged at ``done`` through
  the rescue.  The flag-off legacy contract (retract + thinking_text)
  is byte-identical.
* Stall-guard and steering resume-promise re-prompts after a flushed text ->
  ``segment_complete`` when opted in (the re-prompt continues the turn, it
  does not regenerate the text).
* Shown text is retracted ONLY when it is about to be regenerated: LLM
  timeout retries (the same content re-streams) and max_tokens-truncated
  tool rounds (discarded and retried by the truncation guard).
* Web chat send: the intermediate ``done`` + ``thinking`` pair produced for a
  segment (loop event AND steering fold-in, one shared helper) carries
  ``intermediate: True``; the turn's final ``done`` never does.
* Web chat send: reasoning paragraphs that the deliverable rescue merged into
  the reply are dropped from the persisted / framed ``reasoning``.
"""

from __future__ import annotations

import asyncio
import time

from app.api.v1 import chat as chat_module
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools import agent_loop as al
from app.tools import wait_policy as wp
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
)

# The send tests reuse the real-connection harness from the steering suite
# (same precedent as test_chat_turn_singleflight.py).  The fixtures must be
# imported under their own names for pytest to resolve them; the F811 noqas
# on the test signatures cover the fixture-as-parameter shadowing.
from tests.test_chat_web_steering import (  # noqa: F401
    _FakeConnection,
    _run_connection,
    _send,
    _steer,
    _wait_for_frame,
    fake_rl,
    ws_env,
)

_TASK_ID = "11111111-1111-4111-8111-111111111111"

# Long enough to overflow the hold-back window so the text streams live
# (``_answer_flushed`` True) before the continuation decision is made.
REPORT = (
    "Task 1 finished - here is the detailed intermediate report. " * 20
)  # ~1220 chars
assert len(REPORT) > al._PREAMBLE_HOLDBACK_CHARS
FINAL_ANSWER = "All delegations finished - here is the final synthesis."


class _ScriptedStreamLLM(BaseLLMProvider):
    """Scripted provider whose stream-with-tools calls stream each response's
    content through ``on_token`` (so the hold-back window logic runs for
    real) before returning the scripted response."""

    provider_name = "stub"
    context_window = 100_000

    def __init__(self, script: list[ChatResponse]) -> None:
        self._script = script
        self.call_count = 0

    def _next(self) -> ChatResponse:
        response = self._script[min(self.call_count, len(self._script) - 1)]
        self.call_count += 1
        return response

    # Signature mirrors ``BaseLLMProvider.chat_stream_with_tools`` (optional
    # ``tools``, keyword-only ``on_token``).
    async def chat_stream_with_tools(
        self, messages, tools=None, *, on_token=None, model=None, **kwargs
    ):
        response = self._next()
        if on_token and response.content:
            step = 40
            for i in range(0, len(response.content), step):
                await on_token(response.content[i : i + step])
        return response

    async def chat(self, messages, model=None, tools=None, **kwargs):
        return self._next()

    async def chat_stream(self, messages, **kwargs):
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


def _dummy_executor() -> tuple[list[ToolDefinition], CompositeToolExecutor]:
    inner = CallableToolExecutor()

    async def _noop() -> str:
        return "ok"

    inner.register("noop", _noop)
    composite = CompositeToolExecutor()
    definitions = [
        ToolDefinition(
            name="noop",
            description="Does nothing",
            input_schema={"type": "object", "properties": {}},
            source="builtin",
        )
    ]
    composite.add_tools(definitions, inner)
    return definitions, composite


def _quiet_langfuse(monkeypatch) -> None:
    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_set_ctx", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_reset_ctx", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_set_retry_observer", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_reset_retry_observer", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_make_retry_observer", lambda *a, **kw: None)


def _statuses(events: list[dict]) -> list[str]:
    return [e.get("status") for e in events if e.get("status")]


def _segments(events: list[dict]) -> list[str]:
    return [
        str(e.get("content") or "")
        for e in events
        if e.get("status") == "segment_complete"
    ]


def _thinking_text(events: list[dict]) -> str:
    return "".join(
        str(e.get("content") or "")
        for e in events
        if e.get("status") == "thinking_text"
    )


def _wait_call(until: str = _TASK_ID) -> ChatResponse:
    return ChatResponse(
        content=REPORT,
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[
            ToolCall(
                id="w1",
                name="wait",
                arguments={"duration": "5m", "until": until},
            )
        ],
    )


def _spawned_messages() -> list[ChatMessage]:
    """A transcript that already spawned a background task."""
    return [
        ChatMessage(role="user", content="run the delegations"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(
                    id="s1",
                    name="spawn_background_task",
                    arguments={"agent_id": "x", "task": "go"},
                )
            ],
        ),
        ChatMessage(
            role="tool",
            name="spawn_background_task",
            tool_call_id="s1",
            content=(
                "Background task spawned successfully.\n"
                f"Task ID: {_TASK_ID}\n"
                "Target agent: x\nStatus: pending\n"
            ),
        ),
    ]


async def _run_loop(
    llm: _ScriptedStreamLLM,
    *,
    messages: list[ChatMessage],
    deliver_segments: bool,
    wait_condition=None,
    wait_policy: wp.WaitPolicy | None = None,
) -> tuple[ChatResponse, list[dict], str]:
    definitions, executor = _dummy_executor()
    events: list[dict] = []
    tokens: list[str] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    async def _on_token(chunk: str) -> None:
        tokens.append(chunk)

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=definitions,
        executor=executor,
        max_iterations=5,
        progressive_tools=False,
        on_event=_on_event,
        on_token=_on_token,
        wait_policy=wait_policy or wp.WaitPolicy(per_call_seconds=1, total_seconds=30),
        wait_condition=wait_condition,
        deliver_segments=deliver_segments,
    )
    return response, events, "".join(tokens)


# ── Site: flushed report followed by a wait-family-only tool round ─────────


async def test_wait_only_flushed_report_is_delivered_as_a_segment(monkeypatch):
    """Report-then-keep-waiting: the flushed report becomes a completed
    segment, is never retracted, and is not duplicated into the reasoning
    pane; the loop continues to the real final answer."""
    _quiet_langfuse(monkeypatch)
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(al, "MIN_GRANTED_WAIT_SECONDS", 0.05)

    async def _all_done(ids):
        return True, {tid: "completed" for tid in ids}

    llm = _ScriptedStreamLLM(
        [
            _wait_call(),
            ChatResponse(content=FINAL_ANSWER, model="stub", stop_reason="stop"),
        ]
    )
    response, events, _streamed = await _run_loop(
        llm,
        messages=[ChatMessage(role="user", content="report then wait")],
        deliver_segments=True,
        wait_condition=_all_done,
    )

    assert _segments(events) == [REPORT]
    assert "stream_retract" not in _statuses(events)
    assert REPORT[:60] not in _thinking_text(events), (
        "a delivered segment is a real message — it must not be re-emitted "
        "as reasoning prose"
    )
    assert response.content == FINAL_ANSWER
    assert llm.call_count == 2


async def test_wait_only_flushed_report_still_retracts_by_default(monkeypatch):
    """With ``deliver_segments`` off (assignments, sub-agents, REST, public
    embed, channels) the behavior is byte-identical to before: retract plus
    thinking_text re-surface."""
    _quiet_langfuse(monkeypatch)
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(al, "MIN_GRANTED_WAIT_SECONDS", 0.05)

    async def _all_done(ids):
        return True, {tid: "completed" for tid in ids}

    llm = _ScriptedStreamLLM(
        [
            _wait_call(),
            ChatResponse(content=FINAL_ANSWER, model="stub", stop_reason="stop"),
        ]
    )
    response, events, _streamed = await _run_loop(
        llm,
        messages=[ChatMessage(role="user", content="report then wait")],
        deliver_segments=False,
        wait_condition=_all_done,
    )

    assert _segments(events) == []
    assert "stream_retract" in _statuses(events)
    assert REPORT[:60] in _thinking_text(events)
    assert response.content == FINAL_ANSWER


def _tool_round(name: str = "noop", stop_reason: str = "tool_calls") -> ChatResponse:
    """A flushed answer followed by a REAL (non-wait) tool call — the
    ``store_memory`` / ``search_knowledge`` shape."""
    return ChatResponse(
        content=REPORT,
        model="stub",
        stop_reason=stop_reason,
        tool_calls=[ToolCall(id="t1", name=name, arguments={})],
    )


async def test_flushed_answer_with_non_wait_tool_is_delivered_as_a_segment(
    monkeypatch,
):
    """A flushed answer followed by a side-effect / lookup tool call
    is a deliverable the user already read.  With segment delivery on it
    becomes a completed segment — never retracted, never re-emitted as
    reasoning prose — and the post-tool continuation is the turn's own
    final answer."""
    _quiet_langfuse(monkeypatch)

    llm = _ScriptedStreamLLM(
        [
            _tool_round(),
            ChatResponse(content=FINAL_ANSWER, model="stub", stop_reason="stop"),
        ]
    )
    response, events, streamed = await _run_loop(
        llm,
        messages=[ChatMessage(role="user", content="report then act")],
        deliver_segments=True,
    )

    assert _segments(events) == [REPORT]
    assert "stream_retract" not in _statuses(events)
    assert REPORT[:60] not in _thinking_text(events), (
        "a delivered segment is a real message — it must not scroll through "
        "the reasoning pane a second time"
    )
    assert response.content == FINAL_ANSWER
    # The segment streamed live once and the final answer streamed after it;
    # neither was replayed.
    assert streamed == REPORT + FINAL_ANSWER
    assert llm.call_count == 2
    # The tool round itself still ran (the segment is emitted BEFORE the
    # tools execute, so the tool activity shows below the finalized bubble).
    statuses = _statuses(events)
    assert statuses.index("segment_complete") < statuses.index("tool_start")


async def test_flushed_answer_with_non_wait_tool_still_retracts_by_default(
    monkeypatch,
):
    """Flag off (assignments, sub-agents, REST, public embed widget,
    channels): the legacy contract is byte-identical — retract, then the
    text re-surfaces as ``thinking_text``."""
    _quiet_langfuse(monkeypatch)

    llm = _ScriptedStreamLLM(
        [
            _tool_round(),
            ChatResponse(content=FINAL_ANSWER, model="stub", stop_reason="stop"),
        ]
    )
    response, events, _streamed = await _run_loop(
        llm,
        messages=[ChatMessage(role="user", content="report then act")],
        deliver_segments=False,
    )

    assert _segments(events) == []
    assert "stream_retract" in _statuses(events)
    assert REPORT[:60] in _thinking_text(events)
    assert response.content == FINAL_ANSWER


async def test_max_tokens_truncated_non_wait_round_still_retracts(monkeypatch):
    """A max_tokens-truncated tool round is discarded and retried by the
    truncation guard — the text IS regenerated, so it keeps the retract even
    with segment delivery on (the one legitimate reason to take shown text
    back)."""
    _quiet_langfuse(monkeypatch)

    llm = _ScriptedStreamLLM(
        [
            _tool_round(stop_reason="max_tokens"),
            ChatResponse(content=FINAL_ANSWER, model="stub", stop_reason="stop"),
        ]
    )
    response, events, _streamed = await _run_loop(
        llm,
        messages=[ChatMessage(role="user", content="report then act")],
        deliver_segments=True,
    )

    assert _segments(events) == []
    assert "stream_retract" in _statuses(events)
    assert response.content == FINAL_ANSWER


async def test_max_tokens_truncated_wait_round_still_retracts(monkeypatch):
    """A wait-family round whose response hit ``max_tokens`` is discarded and
    retried by the truncated-tool-call guard — its narration is a
    half-finished checkpoint the retry regenerates, so it must keep the
    retract instead of persisting a truncated segment."""
    _quiet_langfuse(monkeypatch)
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(al, "MIN_GRANTED_WAIT_SECONDS", 0.05)

    truncated_wait = ChatResponse(
        content=REPORT,
        model="stub",
        stop_reason="max_tokens",
        tool_calls=[
            ToolCall(
                id="w1",
                name="wait",
                arguments={"duration": "5m", "until": _TASK_ID},
            )
        ],
    )
    llm = _ScriptedStreamLLM(
        [
            truncated_wait,
            ChatResponse(content=FINAL_ANSWER, model="stub", stop_reason="stop"),
        ]
    )
    response, events, _streamed = await _run_loop(
        llm,
        messages=[ChatMessage(role="user", content="report then wait")],
        deliver_segments=True,
    )

    assert _segments(events) == []
    assert "stream_retract" in _statuses(events)
    assert response.content == FINAL_ANSWER


# ── Site: stall guard re-prompt after a flushed narration ──────────────────


STALL_NARRATION = REPORT + " Now executing the writes."
assert len(STALL_NARRATION) > al._PREAMBLE_HOLDBACK_CHARS


def _stall_script() -> list[ChatResponse]:
    """Tool round, then a long text-only narration that trips the stall
    guard (present-tense "executing now" tail), then the real answer."""
    return [
        ChatResponse(
            content="",
            model="stub",
            stop_reason="tool_calls",
            tool_calls=[ToolCall(id="t1", name="noop", arguments={})],
        ),
        ChatResponse(content=STALL_NARRATION, model="stub", stop_reason="stop"),
        ChatResponse(content=FINAL_ANSWER, model="stub", stop_reason="stop"),
    ]


async def test_stall_guard_keeps_flushed_narration_as_a_segment(monkeypatch):
    """The stall guard re-prompts the model to execute, it does not
    regenerate the narration the user already read — with segment delivery
    on the narration stays on screen as a completed segment and the
    post-nudge answer is the turn's own final content."""
    _quiet_langfuse(monkeypatch)

    llm = _ScriptedStreamLLM(_stall_script())
    response, events, _streamed = await _run_loop(
        llm,
        messages=[ChatMessage(role="user", content="assign the owners")],
        deliver_segments=True,
    )

    statuses = _statuses(events)
    assert _segments(events) == [STALL_NARRATION]
    assert "stream_retract" not in statuses
    stall_idx = next(
        i
        for i, e in enumerate(events)
        if e.get("status") == "composing" and e.get("reason") == "stall_continuation"
    )
    assert statuses.index("segment_complete") < stall_idx
    assert response.content == FINAL_ANSWER
    assert llm.call_count == 3


async def test_stall_guard_still_retracts_by_default(monkeypatch):
    """Flag off: the stall guard keeps the retract (legacy contract)."""
    _quiet_langfuse(monkeypatch)

    llm = _ScriptedStreamLLM(_stall_script())
    response, events, _streamed = await _run_loop(
        llm,
        messages=[ChatMessage(role="user", content="assign the owners")],
        deliver_segments=False,
    )

    assert _segments(events) == []
    assert "stream_retract" in _statuses(events)
    assert response.content == FINAL_ANSWER


# ── Site: implicit park (text-only response, spawned tasks still live) ─────


async def test_implicit_park_delivers_flushed_report_as_segment(monkeypatch):
    """A text-only flushed report while spawned tasks are live parks the loop
    (implicit wait) — the report must be delivered, not retracted."""
    _quiet_langfuse(monkeypatch)
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(al, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    polls = {"n": 0}

    async def _condition(ids):
        polls["n"] += 1
        if polls["n"] == 1:
            return False, {ids[0]: "running"}
        return True, {ids[0]: "completed"}

    llm = _ScriptedStreamLLM(
        [
            ChatResponse(content=REPORT, model="stub", stop_reason="stop"),
            ChatResponse(content=FINAL_ANSWER, model="stub", stop_reason="stop"),
        ]
    )
    response, events, _streamed = await _run_loop(
        llm,
        messages=_spawned_messages(),
        deliver_segments=True,
        wait_condition=_condition,
    )

    assert _segments(events) == [REPORT]
    assert "stream_retract" not in _statuses(events)
    assert response.content == FINAL_ANSWER
    assert llm.call_count == 2


async def test_implicit_park_still_retracts_by_default(monkeypatch):
    """Flag off: the implicit-park site keeps the retract."""
    _quiet_langfuse(monkeypatch)
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(al, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    polls = {"n": 0}

    async def _condition(ids):
        polls["n"] += 1
        if polls["n"] == 1:
            return False, {ids[0]: "running"}
        return True, {ids[0]: "completed"}

    llm = _ScriptedStreamLLM(
        [
            ChatResponse(content=REPORT, model="stub", stop_reason="stop"),
            ChatResponse(content=FINAL_ANSWER, model="stub", stop_reason="stop"),
        ]
    )
    response, events, _streamed = await _run_loop(
        llm,
        messages=_spawned_messages(),
        deliver_segments=False,
        wait_condition=_condition,
    )

    assert _segments(events) == []
    assert "stream_retract" in _statuses(events)
    assert response.content == FINAL_ANSWER


# ── Site: budget-exhausted park-return ──────────────────────────────────────


async def test_spent_wait_budget_park_delivers_segment_and_streams_park_message(
    monkeypatch,
):
    """When the wait budget is spent, the turn ends as ``waiting_on_tasks``:
    the flushed report becomes a segment and the park message streams as the
    turn's own final content — the segment never contains the park text."""
    _quiet_langfuse(monkeypatch)
    # Total budget (1s) below the minimum useful pause -> the park-return
    # fires without any wait round.
    monkeypatch.setattr(al, "MIN_GRANTED_WAIT_SECONDS", 5)

    async def _still_running(ids):
        return False, {ids[0]: "running"}

    llm = _ScriptedStreamLLM(
        [ChatResponse(content=REPORT, model="stub", stop_reason="stop")]
    )
    response, events, streamed = await _run_loop(
        llm,
        messages=_spawned_messages(),
        deliver_segments=True,
        wait_condition=_still_running,
        wait_policy=wp.WaitPolicy(per_call_seconds=1, total_seconds=1),
    )

    assert _segments(events) == [REPORT]
    assert "stream_retract" not in _statuses(events)
    assert response.stop_reason == "waiting_on_tasks"
    assert response.pending_task_ids == [_TASK_ID]
    # Named by agent + task from the spawn transcript, never by id.
    assert _TASK_ID not in (response.content or "")
    assert '**x** - "go"' in (response.content or "")
    assert REPORT[:60] not in (response.content or ""), (
        "the park message is the turn's own content — the delivered segment "
        "must not be duplicated into it"
    )
    assert streamed.endswith(response.content), (
        "the park message must still stream live after the segment"
    )
    assert llm.call_count == 1


async def test_spent_wait_budget_park_still_retracts_by_default(monkeypatch):
    """Flag off: the budget-exhausted park keeps the retract."""
    _quiet_langfuse(monkeypatch)
    monkeypatch.setattr(al, "MIN_GRANTED_WAIT_SECONDS", 5)

    async def _still_running(ids):
        return False, {ids[0]: "running"}

    llm = _ScriptedStreamLLM(
        [ChatResponse(content=REPORT, model="stub", stop_reason="stop")]
    )
    response, events, _streamed = await _run_loop(
        llm,
        messages=_spawned_messages(),
        deliver_segments=False,
        wait_condition=_still_running,
        wait_policy=wp.WaitPolicy(per_call_seconds=1, total_seconds=1),
    )

    assert _segments(events) == []
    assert "stream_retract" in _statuses(events)
    assert response.stop_reason == "waiting_on_tasks"


# ── Timeout-retry retraction is unaffected by the opt-in ────────────────────


class _TimeoutThenSuccessStreamLLM(BaseLLMProvider):
    """First streaming call stalls past the capped LLM timeout; the retry
    streams a real answer (mirrors ``test_llm_timeout_retry`` stubs)."""

    provider_name = "stub"
    context_window = 100_000

    def __init__(self) -> None:
        self.call_count = 0

    async def chat(self, messages, model=None, tools=None, **kwargs):
        return ChatResponse(content="recovered answer", model=model or "stub")

    async def chat_stream(self, messages, **kwargs):
        self.call_count += 1
        self.last_stop_reason = None
        self.last_usage = None
        if self.call_count == 1:
            await asyncio.sleep(5.0)
        self.last_stop_reason = "end_turn"
        self.last_usage = {"input_tokens": 5, "output_tokens": 3}
        yield "recovered stream"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


async def test_timeout_retry_still_retracts_with_segments_enabled(monkeypatch):
    """The timeout-retry retract is duplicate avoidance (the retried call
    re-streams the same answer) — ``deliver_segments`` must not turn it into
    a segment."""
    _quiet_langfuse(monkeypatch)
    monkeypatch.setattr(al, "_LLM_TIMEOUT_RETRY_BACKOFF_SECS", 0.01)
    monkeypatch.setattr(al, "_LLM_CALL_TIMEOUT", 0.1)

    llm = _TimeoutThenSuccessStreamLLM()
    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    async def _on_token(_chunk: str) -> None:
        pass

    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="stream hi")],
        tools=None,
        max_iterations=5,
        on_event=_on_event,
        on_token=_on_token,
        wall_clock_deadline=time.monotonic() + 120.0,
        deliver_segments=True,
    )

    assert "timed out" not in (response.content or "").lower()
    assert llm.call_count >= 2
    assert "stream_retract" in _statuses(events)
    assert _segments(events) == []


# ── Web chat send: segment_complete -> persisted message + done/thinking ──


SEGMENT_TEXT = "Task 1 report: the scan finished, three findings, details follow."


def _patch_general_chat(monkeypatch, stub) -> None:
    import app.services.general_chat as general_chat_module

    monkeypatch.setattr(general_chat_module, "run_general_chat", stub)


async def test_segment_complete_persists_and_sends_done_then_thinking(
    monkeypatch, ws_env  # noqa: F811
):
    """A ``segment_complete`` event mid-turn must persist the segment as an
    assistant message and send the steering-fold-in frame contract: ``done``
    carrying the stored payload (with ``message_id``), then ``thinking`` to
    re-arm the working UI — all BEFORE the turn's own final ``done``."""
    seen_kwargs: dict = {}

    async def _run(_db, _uid, _history, _content, **kwargs):
        seen_kwargs.update(kwargs)
        await kwargs["on_event"](
            {"status": "segment_complete", "content": SEGMENT_TEXT}
        )
        return ("final answer", None)

    _patch_general_chat(monkeypatch, _run)
    conn = await _send(ws_env, {"content": "run the delegations"})

    # The web chat send is the one surface that opts into segment delivery —
    # for the General Assistant path too (it delegates and waits like any
    # agent), not just agent-bound conversations.
    assert seen_kwargs.get("deliver_segments") is True

    done_frames = [f for f in conn.sent if f.get("type") == "done"]
    assert [f["content"] for f in done_frames] == [SEGMENT_TEXT, "final answer"]
    assert done_frames[0].get("message_id"), (
        "the segment done frame must carry the stored message id"
    )
    # The segment was persisted as its own assistant row, before the final
    # answer's row.
    assert ws_env.stored.index(("assistant", SEGMENT_TEXT)) < ws_env.stored.index(
        ("assistant", "final answer")
    )
    # Frame order on the stream: done(segment) -> thinking -> done(final).
    sent_frames = [e[1] for e in ws_env.timeline if e[0] == "sent"]
    seg_done_idx = next(
        i
        for i, f in enumerate(sent_frames)
        if f.get("type") == "done" and f.get("content") == SEGMENT_TEXT
    )
    rearm_idx = next(
        i
        for i, f in enumerate(sent_frames)
        if i > seg_done_idx and f.get("type") == "thinking"
    )
    final_done_idx = next(
        i
        for i, f in enumerate(sent_frames)
        if f.get("type") == "done" and f.get("content") == "final answer"
    )
    assert seg_done_idx < rearm_idx < final_done_idx
    # The persist must precede the segment's done frame (a reload must
    # never lose a bubble the client already finalized).
    persist_idx = ws_env.timeline.index(("persist", "assistant", SEGMENT_TEXT))
    seg_done_timeline_idx = next(
        i
        for i, e in enumerate(ws_env.timeline)
        if e[0] == "sent"
        and e[1].get("type") == "done"
        and e[1].get("content") == SEGMENT_TEXT
    )
    assert persist_idx < seg_done_timeline_idx


async def test_intermediate_frames_are_flagged_and_final_done_is_not(
    monkeypatch, ws_env  # noqa: F811
):
    """Wire contract: the segment's ``done`` and its ``thinking``
    re-arm frame both carry ``intermediate: True`` (a segment-aware client
    keeps the working UI armed and skips the end-of-turn refetch), while the
    turn's final ``done`` never does."""

    async def _run(_db, _uid, _history, _content, **kwargs):
        await kwargs["on_event"](
            {"status": "segment_complete", "content": SEGMENT_TEXT}
        )
        return ("final answer", None)

    _patch_general_chat(monkeypatch, _run)
    conn = await _send(ws_env, {"content": "run the delegations"})

    frames = conn.sent
    seg_done = next(
        f for f in frames if f.get("type") == "done" and f["content"] == SEGMENT_TEXT
    )
    assert seg_done.get("intermediate") is True
    rearm = frames[frames.index(seg_done) + 1]
    # Both name the turn they belong to.
    assert rearm == {"type": "thinking", "intermediate": True, "turn_id": seg_done["turn_id"]}
    final_done = next(
        f for f in frames if f.get("type") == "done" and f["content"] == "final answer"
    )
    assert "intermediate" not in final_done
    # The initial ack of the turn is a plain (unflagged) ``thinking``.
    assert frames[0].get("type") == "thinking" and "intermediate" not in frames[0]


async def test_steering_fold_in_segment_uses_the_same_flagged_frames(
    monkeypatch, ws_env  # noqa: F811
):
    """The steering fold-in's intermediate segment goes through the SAME
    helper as ``segment_complete`` — flagged ``done`` + flagged ``thinking``
    — so the two producers cannot drift."""
    late = "and add a summary table"

    async def _run(_db, _uid, _history, _content, **kwargs):
        if _content == "build the report":
            queue = kwargs["steering_queue"]
            # The steering text arrives too late to be injected: it stays in
            # the queue as a leftover and triggers the fold-in follow-up.
            deadline = asyncio.get_event_loop().time() + 2
            while queue.qsize() == 0:
                assert asyncio.get_event_loop().time() < deadline
                await asyncio.sleep(0.01)
            return ("segment one", None)
        return ("final answer", None)

    _patch_general_chat(monkeypatch, _run)
    conn = _FakeConnection(str(ws_env.conv.id), {"content": "build the report"}, ws_env.timeline)
    task = asyncio.create_task(_run_connection(conn, ws_env.user_id))
    assert await _wait_for_frame(conn, "turn_started")
    assert (await _steer(ws_env, late))["status"] == "steered"
    await asyncio.wait_for(task, 5)

    frames = conn.sent
    done_frames = [f for f in frames if f.get("type") == "done"]
    assert [f["content"] for f in done_frames] == ["segment one", "final answer"]
    assert done_frames[0].get("intermediate") is True
    assert done_frames[0].get("message_id")
    assert frames[frames.index(done_frames[0]) + 1] == {
        "type": "thinking",
        "intermediate": True,
        "turn_id": done_frames[0]["turn_id"],
    }
    assert "intermediate" not in done_frames[1]


async def test_reasoning_does_not_duplicate_text_rescued_into_the_reply(
    monkeypatch, ws_env  # noqa: F811
):
    """Held-back pre-tool text (under the hold-back window) reaches the web
    chat send as ``thinking_text``; when the deliverable rescue then
    merges that same text into the final reply, the persisted / framed
    ``reasoning`` must not carry a second copy of it.  Unrelated
    reasoning prose is kept."""
    held = (
        "Here is the answer, straight from the configuration: the model is "
        "gpt-6-astra, temperature 0.9, memory on all four tiers."
    )
    thought = "Let me double-check the configuration before I store anything."
    rescued_reply = f"{held}\n\n---\n\nStored both facts."

    async def _run(_db, _uid, _history, _content, **kwargs):
        await kwargs["on_event"]({"status": "thinking_text", "content": f"\n\n{thought}"})
        await kwargs["on_event"]({"status": "thinking_text", "content": f"\n\n{held}"})
        return (rescued_reply, None)

    _patch_general_chat(monkeypatch, _run)
    conn = await _send(ws_env, {"content": "what model are you?"})

    final_done = next(f for f in conn.sent if f.get("type") == "done")
    assert final_done["content"] == rescued_reply
    assert final_done.get("reasoning") == thought


def test_strip_reasoning_echoed_in_reply_contract():
    """Pure helper: only paragraphs of at least ``_REASONING_ECHO_MIN_CHARS``
    that occur verbatim in the reply are dropped; blank runs collapse; an
    untouched input is returned as-is (no re-rendering)."""
    strip = chat_module.strip_reasoning_echoed_in_reply
    long_echo = "This paragraph is long enough to be treated as an echoed answer."
    assert len(long_echo) >= chat_module._REASONING_ECHO_MIN_CHARS
    short_echo = "Yes."
    thought = "Considering the two options before answering the question."

    reasoning = f"\n\n{thought}\n\n{long_echo}\n\n{short_echo}"
    reply = f"{long_echo}\n\n{short_echo}"
    assert strip(reasoning, reply) == f"{thought}\n\n{short_echo}"
    # Nothing echoed -> byte-identical input back.
    assert strip(reasoning, "unrelated reply") == reasoning
    assert strip("", reply) == ""
    assert strip(reasoning, "") == reasoning
    # Everything echoed -> empty (the caller then omits the key).
    assert strip(f"\n\n{long_echo}", long_echo) == ""


async def test_segment_persist_failure_still_finalizes_the_bubble(
    monkeypatch, ws_env  # noqa: F811
):
    """A failed segment persist must never break the running turn: the
    ``done`` frame still goes out (without ``message_id``) so the text the
    user already read survives on screen, and the turn completes."""
    ws_env.fail_persist_for.add(SEGMENT_TEXT)

    async def _run(_db, _uid, _history, _content, **kwargs):
        await kwargs["on_event"](
            {"status": "segment_complete", "content": SEGMENT_TEXT}
        )
        return ("final answer", None)

    _patch_general_chat(monkeypatch, _run)
    conn = await _send(ws_env, {"content": "run the delegations"})

    done_frames = [f for f in conn.sent if f.get("type") == "done"]
    assert [f["content"] for f in done_frames] == [SEGMENT_TEXT, "final answer"]
    assert "message_id" not in done_frames[0]
    assert ("assistant", SEGMENT_TEXT) not in ws_env.stored
    assert ("assistant", "final answer") in ws_env.stored


async def test_empty_segment_is_ignored(monkeypatch, ws_env):  # noqa: F811
    """A blank ``segment_complete`` (defensive) must produce no frames and
    no persisted row."""

    async def _run(_db, _uid, _history, _content, **kwargs):
        await kwargs["on_event"]({"status": "segment_complete", "content": "   "})
        return ("final answer", None)

    _patch_general_chat(monkeypatch, _run)
    conn = await _send(ws_env, {"content": "run the delegations"})

    done_frames = [f for f in conn.sent if f.get("type") == "done"]
    assert [f["content"] for f in done_frames] == ["final answer"]
    assert [r for r in ws_env.stored if r[0] == "assistant"] == [
        ("assistant", "final answer")
    ]


async def test_segment_content_is_persisted_verbatim(
    monkeypatch, ws_env  # noqa: F811
):
    """The persisted segment and its ``done`` frame must carry the text
    byte-identical to what streamed: strip() is used only for the blank
    check.  A report opening with an indented Markdown code block would
    otherwise lose its leading indentation and stop rendering as code
    after a reload."""
    indented = "    scan_result = run()\n    print(scan_result)\n\nDone.\n"

    async def _run(_db, _uid, _history, _content, **kwargs):
        await kwargs["on_event"](
            {"status": "segment_complete", "content": indented}
        )
        return ("final answer", None)

    _patch_general_chat(monkeypatch, _run)
    conn = await _send(ws_env, {"content": "run the delegations"})

    assert ("assistant", indented) in ws_env.stored
    done_frames = [f for f in conn.sent if f.get("type") == "done"]
    assert done_frames[0]["content"] == indented


# ── Service-level segment output finalization ───────────────
#
# ``run_agent_chat`` / ``run_general_chat`` wrap their ``on_event`` so a
# ``segment_complete`` receives the same deterministic output pipeline as the
# turn's final answer (chart injection + URL guard) before the send's bridge
# persists it verbatim.  The shared helper is pinned here.  Base-URL
# resolution (integration scan + decryption / a DB query) is expensive, so it
# must stay behind the guard's own "/dashboard" + kill-switch gates and only
# ever run once per turn — the resolver-call assertions below are the
# "no DB work" contract (the resolver is the only path to that work).


async def _no_resolve() -> list[str]:  # pragma: no cover - must not be called
    raise AssertionError(
        "base URLs must not be resolved for a segment that never passes "
        "the /dashboard + kill-switch gates"
    )


async def test_finalize_segment_injects_and_drains_pending_charts(monkeypatch):
    """Charts accumulated before the segment belong to the report the user
    just read: they are appended to the segment text and DRAINED from the
    shared list in place, so the post-loop injection pass (which reads the
    same list object) cannot attach them to the final answer again."""
    import app.services.code_interpreter as ci
    from app.services.segment_finalize import finalize_segment_content

    async def _fake_store(_img, index, **_kwargs):
        return f"/api/v1/chat/files/fake-{index}/download"

    monkeypatch.setattr(ci, "_store_chart_image", _fake_store)

    images = ["b64-one", "b64-two"]
    out = await finalize_segment_content(
        "Report body.",
        code_images=images,
        resolve_base_urls=_no_resolve,
        db=None,
        user_id="u",
        conversation_id="c",
    )

    assert "![Figure 1](/api/v1/chat/files/fake-0/download)" in out
    assert "![Figure 2](/api/v1/chat/files/fake-1/download)" in out
    assert out.startswith("Report body.")
    # Drained in place — the caller's post-loop pass sees an empty list.
    assert images == []


async def test_finalize_segment_failure_leaves_charts_for_the_final_answer(
    monkeypatch,
):
    """If chart injection raises, the caller delivers the raw segment — and
    the shared list must be left UNdrained, so the post-loop final-answer
    injection can still attach the charts instead of losing them from both
    messages."""
    import pytest

    import app.services.code_interpreter as ci
    from app.services.segment_finalize import finalize_segment_content

    async def _boom_store(*_a, **_k):
        raise RuntimeError("storage down")

    monkeypatch.setattr(ci, "_store_chart_image", _boom_store)

    images = ["b64-one", "b64-two"]
    with pytest.raises(RuntimeError):
        await finalize_segment_content(
            "Report body.",
            code_images=images,
            resolve_base_urls=_no_resolve,
            db=None,
            user_id="u",
            conversation_id="c",
        )
    assert images == ["b64-one", "b64-two"]


async def test_finalize_segment_guards_platform_urls_and_resolves_once(
    monkeypatch,
):
    """A guardable segment goes through ``guard_urls`` exactly like the
    final answer, with the LAZILY resolved base URLs — and a second
    guardable segment in the same turn reuses the cached resolution instead
    of re-scanning integrations."""
    import app.services.url_guard as ug
    from app.services.segment_finalize import (
        finalize_segment_content,
        make_lazy_base_url_resolver,
    )

    seen: dict = {}
    resolve_calls = 0

    def _fake_guard(text, base_urls, **kwargs):
        seen["base_urls"] = base_urls
        return text.replace("/dashboard/bogus", "/dashboard/corrected")

    monkeypatch.setattr(ug, "guard_urls", _fake_guard)

    async def _resolve() -> list[str]:
        nonlocal resolve_calls
        resolve_calls += 1
        return ["https://octi.example.com"]

    resolver = make_lazy_base_url_resolver(_resolve)

    guardable = "See https://octi.example.com/dashboard/bogus for details."
    out = await finalize_segment_content(
        guardable,
        code_images=[],
        resolve_base_urls=resolver,
        db=None,
        user_id="u",
        conversation_id="c",
    )
    assert "/dashboard/corrected" in out
    assert seen["base_urls"] == ["https://octi.example.com"]
    assert resolve_calls == 1

    # Second guardable segment in the same turn: cached, no re-resolution.
    await finalize_segment_content(
        guardable,
        code_images=[],
        resolve_base_urls=resolver,
        db=None,
        user_id="u",
        conversation_id="c",
    )
    assert resolve_calls == 1


async def test_finalize_segment_is_a_noop_without_charts_or_links(monkeypatch):
    """No pending charts and no ``/dashboard`` substring: the text passes
    through byte-identical, the guard is never invoked, and the base-URL
    resolution (the only DB/decryption work) never runs — same cheap gates
    as the final-answer path."""
    import app.services.url_guard as ug
    from app.services.segment_finalize import finalize_segment_content

    def _boom(*_a, **_k):  # pragma: no cover - must not be called
        raise AssertionError("guard_urls must not run without /dashboard")

    monkeypatch.setattr(ug, "guard_urls", _boom)

    text = "  indented report, no links, no charts \n"
    out = await finalize_segment_content(
        text,
        code_images=[],
        resolve_base_urls=_no_resolve,
        db=None,
        user_id="u",
        conversation_id="c",
    )
    assert out == text


async def test_finalize_segment_never_resolves_when_guard_disabled(monkeypatch):
    """With the URL-guard kill switch off, even a ``/dashboard`` segment must
    not trigger base-URL resolution — the kill switch gates the expensive
    work exactly as it does on the final-answer path."""
    from app.core.config import settings as app_settings
    from app.services.segment_finalize import finalize_segment_content

    monkeypatch.setattr(app_settings, "url_guard_enabled", False)

    text = "See https://octi.example.com/dashboard/bogus for details."
    out = await finalize_segment_content(
        text,
        code_images=[],
        resolve_base_urls=_no_resolve,
        db=None,
        user_id="u",
        conversation_id="c",
    )
    assert out == text


async def test_finalize_segment_resolves_for_a_later_guardable_segment(
    monkeypatch,
):
    """An unguardable first segment must leave the cache UNRESOLVED (not an
    empty-list sentinel), so a later guardable segment in the same turn
    still triggers the one-time resolution."""
    import app.services.url_guard as ug
    from app.services.segment_finalize import (
        finalize_segment_content,
        make_lazy_base_url_resolver,
    )

    monkeypatch.setattr(ug, "guard_urls", lambda text, _urls, **_k: text)

    resolve_calls = 0

    async def _resolve() -> list[str]:
        nonlocal resolve_calls
        resolve_calls += 1
        return []

    resolver = make_lazy_base_url_resolver(_resolve)

    await finalize_segment_content(
        "First report, nothing guardable here.",
        code_images=[],
        resolve_base_urls=resolver,
        db=None,
        user_id="u",
        conversation_id="c",
    )
    assert resolve_calls == 0

    await finalize_segment_content(
        "Second report: https://octi.example.com/dashboard/foo",
        code_images=[],
        resolve_base_urls=resolver,
        db=None,
        user_id="u",
        conversation_id="c",
    )
    assert resolve_calls == 1


async def test_finalize_segment_guard_failure_keeps_injected_charts(
    monkeypatch,
):
    """A base-URL resolution failure after charts were injected AND drained
    must not raise out of the helper — that would fall the caller back to
    the RAW event and lose the drained charts from both messages.  The
    injected-but-unguarded text is returned instead."""
    import app.services.code_interpreter as ci
    from app.services.segment_finalize import finalize_segment_content

    async def _fake_store(_img, index, **_kwargs):
        return f"/api/v1/chat/files/fake-{index}/download"

    monkeypatch.setattr(ci, "_store_chart_image", _fake_store)

    async def _boom_resolve() -> list[str]:
        raise RuntimeError("db down")

    images = ["b64-one"]
    out = await finalize_segment_content(
        "Report: https://octi.example.com/dashboard/foo",
        code_images=images,
        resolve_base_urls=_boom_resolve,
        db=None,
        user_id="u",
        conversation_id="c",
    )
    assert "![Figure 1](/api/v1/chat/files/fake-0/download)" in out
    assert out.startswith("Report: https://octi.example.com/dashboard/foo")
    assert images == []


# ── Binding-derived base URL resolution ──────────────
#
# The lazy segment resolver (and the agent path's final-answer guard) must
# derive base URLs from the FINAL effective bindings dict: template
# resolution ADDS integrations to it and the send's ``session_integration_ids``
# override REPLACES it entirely, neither of which updates the direct-binding
# row cache (``integ_map``) — a resolver frozen on that cache would leave a
# template- or session-selected platform's /dashboard links unguarded.
# ``resolve_binding_base_urls`` is that single bindings-driven path, shared
# by both chat services.


class _FakeScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeGuardSession:
    """Short-lived session stub: records the integration ids queried."""

    def __init__(self, rows_by_id, queried):
        self._rows_by_id = rows_by_id
        self._queried = queried

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def begin_nested(self):
        return self

    async def execute(self, stmt):
        ids = []
        for value in stmt.compile().params.values():
            if isinstance(value, (list, tuple)):
                ids.extend(value)
            elif value is not None:
                ids.append(value)
        self._queried.extend(ids)
        return _FakeScalarResult(
            [self._rows_by_id[i] for i in ids if i in self._rows_by_id]
        )


def _patch_guard_session(monkeypatch, rows_by_id, url_by_row):
    import app.core.database as core_db
    import app.services.integration_tools as it

    queried: list = []
    monkeypatch.setattr(
        core_db, "async_session", lambda: _FakeGuardSession(rows_by_id, queried)
    )
    monkeypatch.setattr(
        it, "get_integration_public_url", lambda integ, _platform=None: url_by_row[id(integ)]
    )
    return queried


async def test_resolver_uses_session_swapped_bindings(monkeypatch):
    """A session_integration_ids override replaces the bindings with
    ``{provider: [ids]}`` lists — the resolver must query exactly those ids
    (never the stale direct-binding rows) and skip non-platform providers."""
    import uuid as _uuid
    from types import SimpleNamespace

    from app.services.segment_finalize import resolve_binding_base_urls

    session_octi = _uuid.uuid4()
    session_oaev = _uuid.uuid4()
    octi_row = SimpleNamespace(provider="opencti")
    oaev_row = SimpleNamespace(provider="openaev")
    queried = _patch_guard_session(
        monkeypatch,
        {session_octi: octi_row, session_oaev: oaev_row},
        {
            id(octi_row): "https://octi.session.example.com",
            id(oaev_row): "https://oaev.session.example.com",
        },
    )

    urls = await resolve_binding_base_urls(
        {
            "opencti": [str(session_octi)],
            "openaev": [str(session_oaev)],
            "github": [str(_uuid.uuid4())],
        }
    )

    assert sorted(urls.urls) == [
        "https://oaev.session.example.com",
        "https://octi.session.example.com",
    ]
    assert set(queried) == {session_octi, session_oaev}


async def test_resolver_accepts_template_resolved_str_bindings(monkeypatch):
    """Template resolution assigns a bare ``str(id)`` (not a list) into the
    effective bindings — the original direct-binding shape.  Both shapes
    must resolve."""
    import uuid as _uuid
    from types import SimpleNamespace

    from app.services.segment_finalize import resolve_binding_base_urls

    tmpl_octi = _uuid.uuid4()
    octi_row = SimpleNamespace(provider="opencti")
    queried = _patch_guard_session(
        monkeypatch,
        {tmpl_octi: octi_row},
        {id(octi_row): "https://octi.template.example.com"},
    )

    urls = await resolve_binding_base_urls({"opencti": str(tmpl_octi)})

    assert urls.urls == ["https://octi.template.example.com"]
    assert queried == [tmpl_octi]


async def test_resolver_includes_suffixed_multi_instance_bindings(monkeypatch):
    """``flatten_bindings`` names extra direct instances of one provider
    ``opencti#2``, ``opencti#3``, ... — the collector must normalize the key
    to its base provider so a second bound OpenCTI/OpenAEV instance's
    /dashboard links are guardable too, not just the first instance's."""
    import uuid as _uuid
    from types import SimpleNamespace

    from app.services.segment_finalize import resolve_binding_base_urls

    first_octi = _uuid.uuid4()
    second_octi = _uuid.uuid4()
    first_row = SimpleNamespace(provider="opencti")
    second_row = SimpleNamespace(provider="opencti")
    queried = _patch_guard_session(
        monkeypatch,
        {first_octi: first_row, second_octi: second_row},
        {
            id(first_row): "https://octi-one.example.com",
            id(second_row): "https://octi-two.example.com",
        },
    )

    urls = await resolve_binding_base_urls(
        {"opencti": str(first_octi), "opencti#2": str(second_octi)}
    )

    assert sorted(urls.urls) == [
        "https://octi-one.example.com",
        "https://octi-two.example.com",
    ]
    assert set(queried) == {first_octi, second_octi}


async def test_resolver_never_opens_a_session_for_empty_bindings(monkeypatch):
    """No bindings -> no session checkout at all (not even an empty query)."""
    import app.core.database as core_db
    from app.services.segment_finalize import resolve_binding_base_urls

    def _no_session():  # pragma: no cover - must not be called
        raise AssertionError("no session may be opened for empty bindings")

    monkeypatch.setattr(core_db, "async_session", _no_session)

    assert await resolve_binding_base_urls({}) == []
    assert await resolve_binding_base_urls(None) == []


async def test_guardable_segment_guards_session_platform_url(monkeypatch):
    """End-to-end at the helper level: a segment with a /dashboard link for
    a session-selected platform reaches ``guard_urls`` with the base URLs
    resolved from the swapped bindings."""
    import uuid as _uuid
    from types import SimpleNamespace

    import app.services.url_guard as ug
    from app.services.segment_finalize import (
        finalize_segment_content,
        make_lazy_base_url_resolver,
        resolve_binding_base_urls,
    )

    session_octi = _uuid.uuid4()
    octi_row = SimpleNamespace(provider="opencti")
    _patch_guard_session(
        monkeypatch,
        {session_octi: octi_row},
        {id(octi_row): "https://octi.session.example.com"},
    )

    seen_base_urls: list[list[str]] = []

    def _fake_guard(text, base_urls, **_kwargs):
        seen_base_urls.append(list(base_urls))
        return text + " [guarded]"

    monkeypatch.setattr(ug, "guard_urls", _fake_guard)

    # The wiring the services use: lazy cache over the bindings resolver.
    session_bindings = {"opencti": [str(session_octi)]}

    async def _collect() -> list[str]:
        return await resolve_binding_base_urls(session_bindings)

    resolver = make_lazy_base_url_resolver(_collect)

    out = await finalize_segment_content(
        "See https://octi.session.example.com/dashboard/id/abc",
        code_images=[],
        resolve_base_urls=resolver,
        db=None,
        user_id="u",
        conversation_id="c",
    )

    assert out.endswith(" [guarded]")
    assert seen_base_urls == [["https://octi.session.example.com"]]
