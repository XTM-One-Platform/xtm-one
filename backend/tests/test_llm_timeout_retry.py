"""LLM timeout retry with backoff.

When a per-iteration LLM call times out due to a transient provider
latency spike, the agent loop retries up to ``_LLM_TIMEOUT_MAX_RETRIES``
times (with a short backoff) before declaring the iteration terminal.

Contract pinned here:

TIMEOUT-RETRY-1 — non-streaming (chat) path: a single-call timeout is
    retried and the loop succeeds when the retry call returns normally.

TIMEOUT-RETRY-2 — streaming (no-tools) path: same guarantee for the
    streaming branch.

TIMEOUT-RETRY-3 — exhaustion: when retries are all consumed, the loop
    returns the terminal "timed out" response instead of retrying.

TIMEOUT-RETRY-4 — budget guard: when the remaining wall-clock budget is
    below ``_LLM_TIMEOUT_RETRY_MIN_REMAINING_SECS``, no retry is attempted
    (retrying a nearly-expired budget would just time out again immediately).

TIMEOUT-RETRY-5 — counter reset: after a successful LLM call the retry
    counter resets, so a later isolated timeout can still retry.

TIMEOUT-RETRY-6 — cancel-aware backoff: a user cancellation fired during
    the backoff sleep wakes it immediately and the loop returns the
    standard cancelled response instead of waiting out the full backoff.
"""

from __future__ import annotations

import asyncio
import time

import pytest

import app.tools.agent_loop as al
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_echo_executor() -> tuple[list[ToolDefinition], CompositeToolExecutor]:
    async def _echo(i: int = 0) -> str:
        return f"echo-{i}"

    exe = CallableToolExecutor()
    exe.register("echo_tool", _echo)
    defs = [ToolDefinition(name="echo_tool", description="echo")]
    composite = CompositeToolExecutor()
    composite.add_tools(defs, exe)
    return defs, composite


class _TimeoutThenSuccessLLM(BaseLLMProvider):
    """Sleeps longer than the capped LLM timeout for the first
    ``timeout_calls`` calls, then returns a real answer.

    Used to simulate a transient provider latency spike on the first N
    calls followed by a recovery.
    """

    provider_name = "stub"
    context_window = 100_000

    def __init__(self, *, timeout_calls: int = 1, sleep_secs: float = 30.0) -> None:
        self.call_count = 0
        self.timeout_calls = timeout_calls
        self.sleep_secs = sleep_secs

    async def chat(
        self,
        messages,
        model=None,
        temperature=0.7,
        max_tokens=4096,
        tools=None,
        **kwargs,
    ):
        self.call_count += 1
        if self.call_count <= self.timeout_calls:
            await asyncio.sleep(self.sleep_secs)
        return ChatResponse(content="recovered answer", model=model or "stub")

    async def chat_stream(self, messages, **kwargs):
        # Honour the BaseLLMProvider streaming contract: reset the
        # metadata side-channel at call start, populate it before the
        # stream ends so ``_streaming_chat()`` can propagate stop reason
        # and usage to the returned ChatResponse.
        self.call_count += 1
        self.last_stop_reason = None
        self.last_usage = None
        if self.call_count <= self.timeout_calls:
            await asyncio.sleep(self.sleep_secs)
        self.last_stop_reason = "end_turn"
        self.last_usage = {"input_tokens": 5, "output_tokens": 3}
        yield "recovered stream"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


class _AlwaysTimeoutLLM(BaseLLMProvider):
    """Always sleeps longer than the capped LLM timeout, simulating a
    persistent provider outage.
    """

    provider_name = "stub"
    context_window = 100_000

    def __init__(self) -> None:
        self.call_count = 0

    async def chat(self, messages, model=None, tools=None, **kwargs):
        self.call_count += 1
        await asyncio.sleep(30.0)
        return ChatResponse(content="too late", model=model or "stub")

    async def chat_stream(self, messages, **kwargs):
        # Same contract as above; the sleep always outlives the capped
        # per-call timeout, so the post-sleep metadata is never reached
        # in practice — kept for contract fidelity.
        self.call_count += 1
        self.last_stop_reason = None
        self.last_usage = None
        await asyncio.sleep(30.0)
        self.last_stop_reason = "end_turn"
        self.last_usage = {"input_tokens": 5, "output_tokens": 3}
        yield "too late"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


def _patch_lf(monkeypatch) -> None:
    """Stub Langfuse hooks so tests don't require a real Langfuse client."""
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **_kw: None)
    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *_a, **_kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *_a, **_kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *_a, **_kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *_a, **_kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **_kw: None)
    monkeypatch.setattr(al, "_lf_set_ctx", lambda *_a, **_kw: None)
    monkeypatch.setattr(al, "_lf_reset_ctx", lambda *_a, **_kw: None)
    monkeypatch.setattr(al, "_lf_set_retry_observer", lambda *_a, **_kw: None)
    monkeypatch.setattr(al, "_lf_reset_retry_observer", lambda *_a, **_kw: None)
    monkeypatch.setattr(al, "_lf_make_retry_observer", lambda *_a, **_kw: None)


# ---------------------------------------------------------------------------
# TIMEOUT-RETRY-1 — non-streaming (chat with tools) path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_timeout_retries_and_succeeds(monkeypatch):
    """A single-call LLM timeout is retried; the loop returns successfully
    when the retry call completes normally (non-streaming / tools path)."""
    _patch_lf(monkeypatch)
    # Speed up the test: tiny backoff
    monkeypatch.setattr(al, "_LLM_TIMEOUT_RETRY_BACKOFF_SECS", 0.01)
    # Cap the per-call LLM timeout to a very short window so the first
    # sleep(30s) fires quickly.
    monkeypatch.setattr(al, "_LLM_CALL_TIMEOUT", 0.1)

    # First call times out; second call returns immediately.
    llm = _TimeoutThenSuccessLLM(timeout_calls=1, sleep_secs=5.0)
    defs, composite = _build_echo_executor()

    # Start deadline far enough away that retries are allowed.
    deadline = time.monotonic() + 120.0
    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="do it")],
        tools=defs,
        executor=composite,
        max_iterations=5,
        progressive_tools=False,
        wall_clock_deadline=deadline,
    )

    # The loop must have recovered and returned real content, not the
    # error message produced by the terminal timeout handler.
    assert "timed out" not in (response.content or "").lower()
    # LLM was called at least twice: once timing out, once succeeding.
    assert llm.call_count >= 2


# ---------------------------------------------------------------------------
# TIMEOUT-RETRY-2 — streaming (no-tools) path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_timeout_retries_and_succeeds(monkeypatch):
    """A single-call streaming LLM timeout is retried; the loop returns
    successfully when the retry call completes normally.  Any partial
    answer tokens streamed before the stall are retracted
    (``stream_retract``) so the retried call starts with a clean bubble.

    The 0.1 s outer per-call timeout fires long before the 120 s
    idle-chunk watchdog, so the watchdog needs no adjustment here.
    """
    _patch_lf(monkeypatch)
    monkeypatch.setattr(al, "_LLM_TIMEOUT_RETRY_BACKOFF_SECS", 0.01)
    monkeypatch.setattr(al, "_LLM_CALL_TIMEOUT", 0.1)

    llm = _TimeoutThenSuccessLLM(timeout_calls=1, sleep_secs=5.0)

    deadline = time.monotonic() + 120.0

    async def _on_token(tok: str) -> None:
        pass

    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="stream hi")],
        tools=None,
        max_iterations=5,
        on_token=_on_token,
        on_event=_on_event,
        wall_clock_deadline=deadline,
    )

    assert "timed out" not in (response.content or "").lower()
    assert llm.call_count >= 2
    # The retry must retract any provisional answer bubble before the
    # fresh attempt streams its own tokens.
    assert "stream_retract" in [e.get("status") for e in events]
    # The stub honours the last_stop_reason / last_usage streaming
    # contract — the successful retry must propagate that metadata.
    assert response.stop_reason == "end_turn"
    assert (response.usage or {}).get("output_tokens", 0) > 0


# ---------------------------------------------------------------------------
# TIMEOUT-RETRY-3 — exhaustion: all retries consumed → terminal response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_timeout_exhausts_retries_returns_terminal(monkeypatch):
    """When every LLM call times out, the loop returns the terminal
    'timed out' response after exhausting ``_LLM_TIMEOUT_MAX_RETRIES``."""
    _patch_lf(monkeypatch)
    monkeypatch.setattr(al, "_LLM_TIMEOUT_RETRY_BACKOFF_SECS", 0.01)
    monkeypatch.setattr(al, "_LLM_CALL_TIMEOUT", 0.1)
    monkeypatch.setattr(al, "_LLM_TIMEOUT_MAX_RETRIES", 2)

    llm = _AlwaysTimeoutLLM()
    defs, composite = _build_echo_executor()

    deadline = time.monotonic() + 120.0
    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="do it")],
        tools=defs,
        executor=composite,
        max_iterations=10,
        progressive_tools=False,
        wall_clock_deadline=deadline,
    )

    # Terminal response must contain the user-facing timeout message.
    assert "took too long" in (response.content or "").lower()
    # Called exactly 1 (initial) + 2 (retries) = 3 times before giving up.
    assert llm.call_count == 1 + 2


# ---------------------------------------------------------------------------
# TIMEOUT-RETRY-4 — budget guard: no retry when deadline is too close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_timeout_no_retry_when_budget_exhausted(monkeypatch):
    """When the remaining wall-clock budget is below the retry guard's
    floor (``_LLM_TIMEOUT_RETRY_MIN_REMAINING_SECS`` plus the backoff),
    retries are skipped and the terminal response is returned
    immediately."""
    _patch_lf(monkeypatch)
    monkeypatch.setattr(al, "_LLM_TIMEOUT_RETRY_BACKOFF_SECS", 0.01)
    monkeypatch.setattr(al, "_LLM_CALL_TIMEOUT", 0.1)
    # The guard requires remaining > MIN_REMAINING + BACKOFF (~30.01 s
    # with the values patched here); the 10 s deadline below stays well
    # under it so the retry is deterministically skipped.
    monkeypatch.setattr(al, "_LLM_TIMEOUT_RETRY_MIN_REMAINING_SECS", 30.0)
    # Disable the pre-iteration budget guard so the 10 s deadline
    # fixture still reaches the LLM call whose timeout exercises the
    # RETRY-layer budget guard pinned by this test.
    monkeypatch.setattr(al, "_IN_LOOP_MIN_REMAINING_SECS", 1)

    llm = _AlwaysTimeoutLLM()
    defs, composite = _build_echo_executor()

    # Deadline is only 10 s away — well below the ~30 s guard floor, so
    # the retry guard must skip the retry immediately.
    deadline = time.monotonic() + 10.0
    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="do it")],
        tools=defs,
        executor=composite,
        max_iterations=5,
        progressive_tools=False,
        wall_clock_deadline=deadline,
    )

    assert "took too long" in (response.content or "").lower()
    # Only the initial call — no retries attempted.
    assert llm.call_count == 1


# ---------------------------------------------------------------------------
# TIMEOUT-RETRY-5 — counter reset after successful call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_retry_counter_resets_after_success(monkeypatch):
    """The per-run retry counter resets after each successful LLM call so
    a later isolated timeout can still retry even if an earlier iteration
    also timed out and recovered."""
    _patch_lf(monkeypatch)
    monkeypatch.setattr(al, "_LLM_TIMEOUT_RETRY_BACKOFF_SECS", 0.01)
    monkeypatch.setattr(al, "_LLM_CALL_TIMEOUT", 0.1)
    monkeypatch.setattr(al, "_LLM_TIMEOUT_MAX_RETRIES", 1)

    class _TwoSpikedLLM(BaseLLMProvider):
        """Call pattern:
        1. timeout (iteration 1 — spike)
        2. return tool call  (retry of iteration 1 — success, counter resets)
        3. timeout (iteration 2 — spike, counter is 0 again → retry allowed)
        4. return final answer  (retry of iteration 2 — success)
        """

        provider_name = "stub"
        context_window = 100_000

        def __init__(self) -> None:
            self.seq = 0

        async def chat(self, messages, model=None, tools=None, **kwargs):
            self.seq += 1
            if self.seq in (1, 3):
                await asyncio.sleep(5.0)  # > _LLM_CALL_TIMEOUT (0.1s)
            if self.seq == 2 and tools:
                return ChatResponse(
                    content="",
                    model=model or "stub",
                    tool_calls=[
                        ToolCall(id="tc-2", name="echo_tool", arguments={"i": 2})
                    ],
                    stop_reason="tool_use",
                )
            return ChatResponse(content="all done", model=model or "stub")

        async def chat_stream(self, messages, **kwargs):
            yield ""

        async def embed(self, text, model=None):
            return [0.0]

        async def list_models(self):
            return []

    llm = _TwoSpikedLLM()
    defs, composite = _build_echo_executor()

    deadline = time.monotonic() + 120.0
    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="do it")],
        tools=defs,
        executor=composite,
        max_iterations=10,
        progressive_tools=False,
        wall_clock_deadline=deadline,
    )

    # Both spikes were retried successfully; the final answer must be real.
    assert "took too long" not in (response.content or "").lower()
    # Calls: 1(timeout) + 1(success) + 1(timeout) + 1(success) = 4
    assert llm.seq == 4


# ---------------------------------------------------------------------------
# TIMEOUT-RETRY-6 — cancel-aware backoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_during_backoff_wakes_immediately(monkeypatch):
    """A user cancellation fired during the retry backoff sleep must wake
    the backoff immediately and produce the standard cancelled response,
    instead of waiting out the full backoff duration."""
    _patch_lf(monkeypatch)
    monkeypatch.setattr(al, "_LLM_CALL_TIMEOUT", 0.1)
    # Deliberately long backoff: the test only passes quickly if the
    # cancellation wakes the sleep early.
    monkeypatch.setattr(al, "_LLM_TIMEOUT_RETRY_BACKOFF_SECS", 30.0)

    llm = _AlwaysTimeoutLLM()
    defs, composite = _build_echo_executor()
    cancel_event = asyncio.Event()

    async def _cancel_soon() -> None:
        # Fire after the first call has timed out (0.1 s) and the loop has
        # entered the 30 s backoff sleep.
        await asyncio.sleep(0.5)
        cancel_event.set()

    canceller = asyncio.create_task(_cancel_soon())
    started = time.monotonic()
    try:
        response = await al.run_agent_loop(
            llm=llm,
            messages=[ChatMessage(role="user", content="do it")],
            tools=defs,
            executor=composite,
            max_iterations=5,
            progressive_tools=False,
            wall_clock_deadline=time.monotonic() + 120.0,
            cancel_event=cancel_event,
        )
    finally:
        canceller.cancel()
        await asyncio.gather(canceller, return_exceptions=True)

    elapsed = time.monotonic() - started
    assert response.content == al._CANCELLED_MESSAGE
    # Far below the 30 s backoff — the cancel woke the sleep early.
    assert elapsed < 10.0
