"""Empty-response retry visibility: when the iteration LLM call returns
empty content with no tool calls, the agent loop's one-shot rescue
call surfaces as an ``attempt:empty_response_retry`` sub-span under
the iteration generation."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import MagicMock

import pytest

from app.llm.base import (
    BaseLLMProvider,
    ChatMessage,
    ChatResponse,
    retry_llm_call,
)
from app.tools import agent_loop as al
from app.tools.base import ToolCall, ToolDefinition


def _patch_lf_capture(monkeypatch) -> list[MagicMock]:
    """Stub the Langfuse hooks in agent_loop and capture the per-iteration
    span(s) so tests can assert sub-span creation on them.

    Returns the list of captured iteration *spans* (the parent of the
    rescue/continuation generations, not the per-iteration ``llm_call``
    generation).
    """
    captured_iter_spans: list[MagicMock] = []

    def _fake_start_iter_span(**_kwargs):
        span = MagicMock()
        span.start_observation = MagicMock(return_value=MagicMock())
        captured_iter_spans.append(span)
        return {
            "trace": span,
            "generation": None,
            "parent_ctx": None,
            "client": None,
        }

    def _fake_start_iter_gen(_iter_ctx, **_kwargs):
        # Return a ctx whose "trace" is a sink mock — the iteration gen
        # itself is irrelevant for these tests.
        return {
            "trace": MagicMock(),
            "generation": MagicMock(),
            "parent_ctx": _iter_ctx,
            "client": None,
        }

    monkeypatch.setattr(al, "_lf_start_iter_span", _fake_start_iter_span)
    monkeypatch.setattr(al, "_lf_start_llm_gen", _fake_start_iter_gen)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)
    return captured_iter_spans


class _EmptyThenRecoveryLLM(BaseLLMProvider):
    """Returns empty content on call#1 (triggers rescue), real content
    on call#2."""

    provider_name = "stub"
    context_window = 10_000

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(content="", model="stub", stop_reason="end_turn")
        return ChatResponse(content="recovered", model="stub", stop_reason="end_turn")

    async def chat_stream(self, messages, **kwargs):
        yield "x"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.mark.asyncio
async def test_empty_then_recovery_emits_empty_response_retry_subspan(monkeypatch):
    captured = _patch_lf_capture(monkeypatch)
    llm = _EmptyThenRecoveryLLM()

    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="hi")],
        tools=None,
        max_iterations=1,
    )

    assert response.content == "recovered"
    assert llm.calls == 2
    assert len(captured) == 1
    iter_gen = captured[0]
    rescue_calls = [
        c
        for c in iter_gen.start_observation.call_args_list
        if c.kwargs.get("name") == "attempt:empty_response_retry"
    ]
    assert len(rescue_calls) == 1
    kwargs = rescue_calls[0].kwargs
    assert kwargs["as_type"] == "generation"
    assert kwargs["metadata"]["observation_kind"] == "empty_response_retry"
    assert "context_chars" in kwargs["metadata"]
    # Delta-only payload: the appended system message, not the full history.
    assert len(kwargs["input"]["messages"]) == 1
    assert kwargs["input"]["messages"][0]["role"] == "system"


@pytest.mark.asyncio
async def test_empty_response_retry_error_records_error_span(monkeypatch):
    captured = _patch_lf_capture(monkeypatch)

    end_calls: list[dict] = []

    def _capturing_end_span(ctx, **kwargs):
        end_calls.append({"ctx": ctx, **kwargs})

    monkeypatch.setattr(al, "_lf_end_generation", _capturing_end_span)

    class _EmptyThenRaiseLLM(BaseLLMProvider):
        provider_name = "stub"
        context_window = 10_000

        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ChatResponse(content="", model="stub", stop_reason="end_turn")
            raise RuntimeError("rescue boom")

        async def chat_stream(self, messages, **kwargs):
            yield "x"

        async def embed(self, text, model=None):
            return [0.0]

        async def list_models(self):
            return []

    # The forced-final-answer backstop also raises, so the loop may
    # bubble an exception or return a degraded response. We only care
    # about the rescue span closure, not the outer outcome.
    llm = _EmptyThenRaiseLLM()

    with contextlib.suppress(Exception):
        await al.run_agent_loop(
            llm=llm,
            messages=[ChatMessage(role="user", content="hi")],
            tools=None,
            max_iterations=1,
        )

    assert len(captured) == 1
    # ``error`` is the exception object itself (not ``str(exc)``) so the
    # Langfuse facade can render it as ``"RuntimeError: rescue boom"`` and
    # keep the exception type visible in the trace — critical for
    # ``asyncio.CancelledError`` whose ``str()`` is empty.
    rescue_ends = [c for c in end_calls if "error" in c]
    assert any(
        isinstance(c.get("error"), RuntimeError)
        and "rescue boom" in str(c["error"])
        for c in rescue_ends
    ), f"expected rescue end_span with RuntimeError('rescue boom'), got: {rescue_ends}"


@pytest.mark.asyncio
async def test_empty_response_retry_propagates_cancellederror_after_closing_gen(monkeypatch):
    """``asyncio.CancelledError`` mid-empty-response-rescue MUST close
    the ``attempt:empty_response_retry`` generation with the
    cancellation recorded AND propagate so the asyncio scheduler sees
    it.  The earlier shape (``except Exception``) silently swallowed
    ``CancelledError`` because it's a ``BaseException`` subclass; the
    explicit ``except asyncio.CancelledError`` split (this test)
    preserves both contracts — trace fidelity AND structured-concurrency
    invariants.
    """
    _patch_lf_capture(monkeypatch)

    end_calls: list[dict] = []

    def _capturing_end_span(ctx, **kwargs):
        end_calls.append({"ctx": ctx, **kwargs})

    monkeypatch.setattr(al, "_lf_end_generation", _capturing_end_span)

    class _EmptyThenCancelLLM(BaseLLMProvider):
        provider_name = "stub"
        context_window = 10_000

        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ChatResponse(content="", model="stub", stop_reason="end_turn")
            raise asyncio.CancelledError()

        async def chat_stream(self, messages, **kwargs):
            yield "x"

        async def embed(self, text, model=None):
            return [0.0]

        async def list_models(self):
            return []

    llm = _EmptyThenCancelLLM()

    with pytest.raises(asyncio.CancelledError):
        await al.run_agent_loop(
            llm=llm,
            messages=[ChatMessage(role="user", content="hi")],
            tools=None,
            max_iterations=1,
        )

    rescue_ends = [c for c in end_calls if "error" in c]
    assert any(
        isinstance(c.get("error"), asyncio.CancelledError)
        for c in rescue_ends
    ), (
        f"expected rescue end_span with CancelledError so the facade "
        f"records the cancellation reason before propagating; got: {rescue_ends}"
    )


class _EmptyThenRetryingLLM(BaseLLMProvider):
    """Call#1: empty. Call#2 (the rescue): wraps a transient failure in
    ``retry_llm_call`` and succeeds on the inner retry."""

    provider_name = "stub"
    context_window = 10_000

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(content="", model="stub", stop_reason="end_turn")

        attempts = {"n": 0}

        async def _inner():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("transient")
            return ChatResponse(
                content="recovered", model="stub", stop_reason="end_turn"
            )

        return await retry_llm_call(
            _inner,
            is_retryable=lambda exc: isinstance(exc, RuntimeError),
            max_retries=3,
            base_delay=0.001,
            max_delay=0.002,
        )

    async def chat_stream(self, messages, **kwargs):
        yield "x"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


class _EmptyEmptyThenRecoveryLLM(BaseLLMProvider):
    """Empty on the first iteration's primary call AND its no-tools retry,
    then real content on the second iteration's primary call."""

    provider_name = "stub"
    context_window = 10_000

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        if self.calls <= 2:
            return ChatResponse(content="", model="stub", stop_reason="end_turn")
        return ChatResponse(
            content="recovered at iteration 2", model="stub", stop_reason="end_turn"
        )

    async def chat_stream(self, messages, **kwargs):
        yield "x"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.mark.asyncio
async def test_double_empty_escalates_and_recovers_without_backstop(monkeypatch):
    """When both the primary turn and its no-tools retry come back
    empty, the loop must NOT bail to the forced-final-answer backstop at
    iteration 2 — it escalates and keeps iterating, and here recovers on the
    next iteration."""
    _patch_lf_capture(monkeypatch)
    llm = _EmptyEmptyThenRecoveryLLM()

    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="hi")],
        tools=None,
        max_iterations=5,
    )

    assert response.content == "recovered at iteration 2"
    # call1 = iter1 primary (empty), call2 = iter1 no-tools retry (empty),
    # call3 = iter2 primary (recovered). No forced-final-answer backstop ran.
    assert llm.calls == 3


class _AlwaysEmptyLLM(BaseLLMProvider):
    """Always returns empty content with no tool calls (systematic failure)."""

    provider_name = "stub"
    context_window = 10_000

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        return ChatResponse(content="", model="stub", stop_reason="end_turn")

    async def chat_stream(self, messages, **kwargs):
        yield "x"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.mark.asyncio
async def test_persistent_empty_is_bounded_not_proportional_to_budget(monkeypatch):
    """A systematic empty-output condition must reach the backstop after a
    BOUNDED number of empty rounds — not iterate the full (large) budget.

    This is the regression an unbounded 'continue' on empty turns would
    introduce: with max_iterations=30 it would make ~30 empty LLM calls
    before giving up. The cap keeps it to a handful regardless of budget.
    """
    _patch_lf_capture(monkeypatch)
    llm = _AlwaysEmptyLLM()

    with contextlib.suppress(Exception):
        await al.run_agent_loop(
            llm=llm,
            messages=[ChatMessage(role="user", content="hi")],
            tools=None,
            max_iterations=30,
        )

    # Each empty round costs 2 calls (primary + no-tools retry); the loop
    # escalate-continues at most _MAX_CONSECUTIVE_EMPTY_RESPONSES rounds, then
    # the (one-shot) forced-final-answer backstop runs. The total stays far
    # below the 30-iteration budget — proving the bound.
    assert llm.calls < 30
    assert llm.calls <= 2 * al._MAX_CONSECUTIVE_EMPTY_RESPONSES + 4


class _TruncationResetsEmptyCounterLLM(BaseLLMProvider):
    """Drives the loop through: one double-empty round, then a *truncated*
    tool-call turn (genuine progress), then enough further double-empty rounds
    that recovery is only reachable if the truncated turn reset the counter.

    Call sequence:
      1. empty                          (iter1 primary)        → no-tools retry
      2. empty                          (iter1 no-tools retry) → counter = 1
      3. truncated tool call            (iter2 primary)        → counter reset → 0
      4. empty                          (iter3 primary)        → no-tools retry
      5. empty                          (iter3 no-tools retry) → counter = 1
      6. empty                          (iter4 primary)        → no-tools retry
      7. empty                          (iter4 no-tools retry) → counter = 2 (< cap)
      8. real content                   (iter5 primary)        → returns

    Without the reset on the truncated turn the counter would already be at 3
    by call 7 and the loop would bail to the forced-final-answer backstop
    instead of recovering cleanly at call 8.
    """

    provider_name = "stub"
    context_window = 10_000

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 3:
            return ChatResponse(
                content="",
                model="stub",
                stop_reason="max_tokens",
                tool_calls=[ToolCall(id="1", name="noop", arguments={})],
            )
        if self.calls >= 8:
            return ChatResponse(
                content="recovered after truncation reset",
                model="stub",
                stop_reason="end_turn",
            )
        return ChatResponse(content="", model="stub", stop_reason="end_turn")

    async def chat_stream(self, messages, **kwargs):
        yield "x"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.mark.asyncio
async def test_truncated_tool_call_resets_empty_counter(monkeypatch):
    """A truncated tool-call turn is genuine (non-empty) progress
    and must reset ``consecutive_empty_responses``. Otherwise empty rounds on
    either side of it are miscounted as consecutive and the loop bails to the
    backstop one round too early."""
    _patch_lf_capture(monkeypatch)
    # Sanity-check the scenario actually depends on the reset: the cap must be
    # small enough that miscounting would change the outcome.
    assert al._MAX_CONSECUTIVE_EMPTY_RESPONSES == 3
    llm = _TruncationResetsEmptyCounterLLM()

    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="hi")],
        tools=None,
        max_iterations=10,
    )

    # Recovered cleanly on the post-truncation iteration — no backstop ran.
    assert response.content == "recovered after truncation reset"
    assert llm.calls == 8


@pytest.mark.asyncio
async def test_retry_inside_empty_response_recovery_nests_under_rescue_gen(
    monkeypatch,
):
    captured = _patch_lf_capture(monkeypatch)
    llm = _EmptyThenRetryingLLM()

    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="hi")],
        tools=None,
        max_iterations=1,
    )

    assert response.content == "recovered"
    assert len(captured) == 1
    iter_gen = captured[0]

    # Iteration generation must NOT see any attempt:retry:* events —
    # the retry happened inside the rescue gen.
    iter_retry_calls = [
        c
        for c in iter_gen.start_observation.call_args_list
        if c.kwargs.get("name", "").startswith("attempt:retry:")
    ]
    assert iter_retry_calls == []

    # The rescue gen (returned mock from iter_gen.start_observation)
    # carries the retry event.
    rescue_gen = iter_gen.start_observation.return_value
    retry_calls = rescue_gen.create_event.call_args_list
    assert [c.kwargs["name"] for c in retry_calls] == ["attempt:retry:1"]
    assert retry_calls[0].kwargs["level"] == "WARNING"


class _LargeContextEmptyUntilCompactedLLM(BaseLLMProvider):
    """Returns empty while the prompt is large, then recovers once it has been
    compacted below a threshold.

    Reproduces the case where the conversation is dominated by long user/assistant
    turns with NO tool results, so the regular tool-result-only prune is a
    no-op — only the emergency compaction path (assistant truncation / oldest
    group eviction) can shrink the context enough to recover.
    """

    provider_name = "stub"
    context_window = 200_000

    def __init__(self, *, empty_above_chars: int = 60_000) -> None:
        self.calls = 0
        self.empty_above_chars = empty_above_chars
        self.context_sizes: list[int] = []

    async def chat(self, messages, **kwargs):
        self.calls += 1
        size = sum(len(m.content or "") for m in messages)
        self.context_sizes.append(size)
        if size > self.empty_above_chars:
            return ChatResponse(content="", model="stub", stop_reason="end_turn")
        return ChatResponse(
            content=f"recovered after compaction ({size} chars)",
            model="stub",
            stop_reason="end_turn",
        )

    async def chat_stream(self, messages, **kwargs):
        yield "x"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.mark.asyncio
async def test_large_context_empty_response_semantic_compacts_and_recovers(monkeypatch):
    """A large context that elicits an empty response must be SEMANTICALLY
    compacted (older turns summarised, task + recent turns kept) before the
    no-tools retry so the loop recovers instead of churning to the generic
    fallback.

    The summariser is the same ``llm`` (provider-agnostic): call#1 = empty on
    the oversized context, call#2 = the compaction summary, call#3 = the
    no-tools retry on the now-focused context.
    """
    _patch_lf_capture(monkeypatch)
    llm = _LargeContextEmptyUntilCompactedLLM(empty_above_chars=60_000)

    messages = [
        ChatMessage(role="system", content="You are a helpful SRE agent."),
        ChatMessage(role="user", content="Summarise the incident."),
    ]
    # ~88k chars of history with NO tool results — the regular tool-result
    # prune cannot shrink this, so semantic compaction must rescue it.
    for i in range(22):
        messages.append(
            ChatMessage(role="assistant", content=f"turn-{i}-" + ("x" * 4_000))
        )

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=None,
        max_iterations=21,
    )

    assert response.content.startswith("recovered after compaction")
    # call#1 empty (oversized), call#2 summary, call#3 retry on focused context.
    assert llm.calls == 3
    assert llm.context_sizes[0] > 60_000
    # The final retry runs on a context shrunk well below the empty threshold.
    assert llm.context_sizes[-1] < llm.context_sizes[0]
    assert llm.context_sizes[-1] <= 60_000
    # A compaction summary marker must be present in the surviving messages.
    assert any(
        al._SEMANTIC_COMPACTION_MARKER in (m.content or "") for m in messages
    )
    # The original task and the most recent turns are kept verbatim.
    assert any(
        m.role == "user" and "Summarise the incident." in (m.content or "")
        for m in messages
    )
    assert any("turn-21-" in (m.content or "") for m in messages)


@pytest.mark.asyncio
async def test_empty_recovery_falls_back_to_emergency_prune_when_summary_unavailable(
    monkeypatch,
):
    """When semantic compaction can't help (the summariser returns nothing) the
    recovery must fall back to the deterministic emergency prune so a large
    empty-response context is still shrunk and the loop recovers."""
    _patch_lf_capture(monkeypatch)
    llm = _LargeContextEmptyUntilCompactedLLM(empty_above_chars=60_000)

    async def _no_summary(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(al, "_summarize_messages_for_context", _no_summary)

    emergency_calls = {"n": 0}
    real_emergency = al._emergency_prune_session_context

    def _spy(messages, **kwargs):
        emergency_calls["n"] += 1
        return real_emergency(messages, **kwargs)

    monkeypatch.setattr(al, "_emergency_prune_session_context", _spy)

    messages = [
        ChatMessage(role="system", content="You are a helpful SRE agent."),
        ChatMessage(role="user", content="Summarise the incident."),
    ]
    for i in range(22):
        messages.append(
            ChatMessage(role="assistant", content=f"turn-{i}-" + ("x" * 4_000))
        )

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=None,
        max_iterations=21,
    )

    assert response.content.startswith("recovered after compaction")
    # No summary call is made (it's stubbed out), so only the empty turn and the
    # no-tools retry hit the LLM.
    assert llm.calls == 2
    assert emergency_calls["n"] >= 1
    assert llm.context_sizes[0] > 60_000
    assert llm.context_sizes[-1] <= 60_000


@pytest.mark.asyncio
async def test_small_context_empty_response_is_not_emergency_compacted(monkeypatch):
    """Below the large-context trigger, recovery must keep using the cheaper
    tool-result prune — emergency compaction (which drops/truncates turns) must
    not kick in for ordinary-sized conversations."""
    _patch_lf_capture(monkeypatch)
    llm = _EmptyThenRecoveryLLM()

    emergency_calls = {"n": 0}
    real_emergency = al._emergency_prune_session_context

    def _spy(messages, **kwargs):
        emergency_calls["n"] += 1
        return real_emergency(messages, **kwargs)

    monkeypatch.setattr(al, "_emergency_prune_session_context", _spy)

    messages = [ChatMessage(role="user", content="hello")]
    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=None,
        max_iterations=5,
    )

    assert response.content == "recovered"
    assert emergency_calls["n"] == 0


class _LargeContextTruncatingThenSynthesisLLM(BaseLLMProvider):
    """Returns a TRUNCATED tool call (``stop_reason="max_tokens"``) on every
    turn while it has tools, then a real synthesis once the loop calls it
    tool-free for the forced-answer backstop.

    Reproduces the *other* way of reaching the backstop: the truncation
    retry budget is exhausted (``consecutive_empty_responses`` stays 0, since a
    tool-calling turn resets it), so the large context is the product of
    legitimate work — NOT a string of empty responses.
    """

    provider_name = "stub"
    context_window = 200_000

    def __init__(self) -> None:
        self.calls = 0
        self.context_sizes: list[int] = []
        self.tooled_calls = 0

    async def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        self.context_sizes.append(sum(len(m.content or "") for m in messages))
        if tools:
            # Truncated tool-call JSON → discarded by the loop's truncation
            # guard, which retries without executing (no executor needed).
            self.tooled_calls += 1
            return ChatResponse(
                content="",
                model="stub",
                stop_reason="max_tokens",
                tool_calls=[
                    ToolCall(id=f"call-{self.calls}", name="noop", arguments={})
                ],
            )
        # Tool-free backstop synthesis call → produce a substantial answer so
        # the >=100-char guard is satisfied and no stronger-prompt retry runs.
        return ChatResponse(
            content="final synthesis " + ("y" * 200),
            model="stub",
            stop_reason="end_turn",
        )

    async def chat_stream(self, messages, **kwargs):
        yield "x"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.mark.asyncio
async def test_budget_exhaustion_backstop_does_not_emergency_compact_large_context(
    monkeypatch,
):
    """When the forced-answer backstop is reached via budget exhaustion (here:
    repeated tool-call truncation, not empty responses) the large context is
    legitimate work and must NOT be emergency-compacted — truncating it would
    degrade the final synthesis. The cheaper tool-result prune (the original
    behaviour for this path) must be used instead."""
    _patch_lf_capture(monkeypatch)

    emergency_calls = {"n": 0}
    real_emergency = al._emergency_prune_session_context

    def _spy(messages, **kwargs):
        emergency_calls["n"] += 1
        return real_emergency(messages, **kwargs)

    monkeypatch.setattr(al, "_emergency_prune_session_context", _spy)

    llm = _LargeContextTruncatingThenSynthesisLLM()
    tools = [ToolDefinition(name="noop", description="does nothing")]

    messages = [
        ChatMessage(role="system", content="You are a helpful SRE agent."),
        ChatMessage(role="user", content="Investigate the incident."),
    ]
    # ~88k chars of legitimate assistant history with NO tool results — the
    # regular tool-result prune is a no-op here, so the only thing that could
    # shrink it is emergency compaction (which this path must avoid).
    for i in range(22):
        messages.append(
            ChatMessage(role="assistant", content=f"turn-{i}-" + ("z" * 4_000))
        )

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=tools,
        max_iterations=10,
    )

    # Backstop synthesised a real answer from the full (un-compacted) history.
    assert response.content.startswith("final synthesis")
    # Two truncated tool-call turns exhaust the retry budget, then the tool-free
    # backstop synthesis call recovers — never an empty-response escalation.
    assert llm.tooled_calls == 2
    assert llm.context_sizes[0] > 60_000
    # The crux of the fix: a budget-exhaustion backstop must NOT emergency-prune.
    assert emergency_calls["n"] == 0


# ── Streaming-path empty-response retry ──────────────────────────────
#
# When the streaming-no-tools branch returns an empty response (stop_reason=
# end_turn), the loop must attempt ONE retry (with _EMPTY_RESPONSE_RETRY
# appended + context compaction) before falling through to the
# forced-final-answer backstop.  Previously the branch broke to the backstop
# immediately, burning the backstop budget for what was often a transient blip.


class _StreamingEmptyThenRecoveryLLM(BaseLLMProvider):
    """First streaming call yields nothing (empty); second call yields content.

    Simulates the transient-provider-blip pattern —
    a single empty ``end_turn`` response that the retry recovers from.
    """

    provider_name = "stub"
    context_window = 10_000

    def __init__(self) -> None:
        self.stream_calls = 0
        # last_stop_reason is read by _streaming_chat to build ChatResponse
        self.last_stop_reason = "end_turn"

    async def chat(self, messages, **kwargs):
        # Fallback for non-streaming paths (backstop if streaming retry fails).
        return ChatResponse(
            content="backstop answer", model="stub", stop_reason="end_turn"
        )

    async def chat_stream(self, messages, **kwargs):
        self.stream_calls += 1
        if self.stream_calls == 1:
            # Empty stream — simulates the provider returning end_turn with no tokens.
            return
        # Second call: the retry yields real content.
        yield "recovered via streaming retry"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.mark.asyncio
async def test_streaming_empty_response_retries_before_forced_answer(monkeypatch):
    """The streaming-no-tools branch must attempt a retry when the LLM
    returns an empty streamed response, and return the retry content instead of
    falling through to the forced-final-answer backstop.
    """
    _patch_lf_capture(monkeypatch)
    llm = _StreamingEmptyThenRecoveryLLM()

    tokens: list[str] = []

    async def _on_token(chunk: str) -> None:
        tokens.append(chunk)

    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="hello")],
        tools=None,
        on_token=_on_token,
        max_iterations=5,
    )

    assert response.content == "recovered via streaming retry"
    # Primary streaming call (empty) + retry streaming call (recovered).
    assert llm.stream_calls == 2
    # The retry tokens must have been delivered to the caller.
    assert "".join(tokens) == "recovered via streaming retry"


class _StreamingAlwaysEmptyLLM(BaseLLMProvider):
    """First 2 streaming calls yield nothing; subsequent calls yield content.

    Simulates: primary streaming call empty → retry empty → backstop succeeds.
    """

    provider_name = "stub"
    context_window = 10_000

    def __init__(self) -> None:
        self.stream_calls = 0
        self.last_stop_reason = "end_turn"

    async def chat(self, messages, **kwargs):
        # Only reached by backstop when on_token is NOT set (not our case here).
        return ChatResponse(
            content="backstop synthesis " + ("z" * 200),
            model="stub",
            stop_reason="end_turn",
        )

    async def chat_stream(self, messages, **kwargs):
        self.stream_calls += 1
        if self.stream_calls <= 2:
            # Call 1 = primary; call 2 = streaming retry — both empty.
            return
        # Call 3+ = forced-final-answer backstop: provide a real answer.
        yield "backstop synthesis " + "z" * 200

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.mark.asyncio
async def test_streaming_empty_response_retry_also_empty_falls_through_to_backstop(
    monkeypatch,
):
    """When both the primary streaming call AND the retry return empty,
    the loop must fall through to the forced-final-answer backstop (not hang
    or return an empty response to the caller).
    """
    _patch_lf_capture(monkeypatch)
    llm = _StreamingAlwaysEmptyLLM()

    # Spy on _compact_for_empty_recovery to prove the backstop saw
    # consecutive_empty_responses > 0 (the emergency-compaction gate).
    compact_phases: list[str] = []
    real_compact = al._compact_for_empty_recovery

    async def _compact_spy(*args, **kwargs):
        compact_phases.append(kwargs.get("phase", ""))
        return await real_compact(*args, **kwargs)

    monkeypatch.setattr(al, "_compact_for_empty_recovery", _compact_spy)

    async def _on_token(chunk: str) -> None:
        pass

    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="hello")],
        tools=None,
        on_token=_on_token,
        max_iterations=5,
    )

    # Both the primary and retry streaming calls were made and both were empty.
    assert llm.stream_calls >= 2, (
        f"Expected at least 2 streaming calls (primary + retry), got {llm.stream_calls}"
    )
    # The backstop ran and produced content (call 3).
    assert response.content.startswith("backstop synthesis")
    # consecutive_empty_responses was incremented, so the backstop took the
    # empty-response compaction path (not the cheap budget-exhaustion prune).
    assert "streaming empty-response retry" in compact_phases
    assert "forced-answer backstop" in compact_phases


@pytest.mark.asyncio
async def test_streaming_empty_response_retry_emits_empty_response_retry_subspan(
    monkeypatch,
):
    """The streaming-path one-shot retry must surface as an
    ``attempt:empty_response_retry`` sub-span under the iteration ctx,
    just like the with-tools retry does.
    """
    captured = _patch_lf_capture(monkeypatch)
    llm = _StreamingEmptyThenRecoveryLLM()

    async def _on_token(chunk: str) -> None:
        pass

    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="hello")],
        tools=None,
        on_token=_on_token,
        max_iterations=5,
    )

    assert response.content == "recovered via streaming retry"
    assert len(captured) == 1
    iter_gen = captured[0]
    rescue_calls = [
        c
        for c in iter_gen.start_observation.call_args_list
        if c.kwargs.get("name") == "attempt:empty_response_retry"
    ]
    assert len(rescue_calls) == 1
    kwargs = rescue_calls[0].kwargs
    assert kwargs["as_type"] == "generation"
    assert kwargs["metadata"]["observation_kind"] == "empty_response_retry"
    assert "context_chars" in kwargs["metadata"]
