"""Continuation visibility: ``_continue_truncated_response`` opens an
``attempt:continuation:N`` generation per continuation under the
iteration span, and nested retries inside a continuation nest under
that generation (not the iteration span)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from app.llm.base import (
    BaseLLMProvider,
    ChatMessage,
    ChatResponse,
    retry_llm_call,
)
from app.tools import agent_loop as al


class _MaxTokensLLM(BaseLLMProvider):
    """Stub whose ``chat()`` returns ``max_tokens`` for the first
    ``truncated_for`` calls, then ``end_turn`` to terminate the loop."""

    provider_name = "stub"
    context_window = 10_000

    def __init__(self, truncated_for: int = 2) -> None:
        self.truncated_for = truncated_for
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        if self.calls <= self.truncated_for:
            return ChatResponse(
                content=f"chunk-{self.calls}",
                model="stub",
                stop_reason="max_tokens",
            )
        return ChatResponse(
            content="final",
            model="stub",
            stop_reason="end_turn",
        )

    async def chat_stream(self, messages, **kwargs):
        yield "ignored"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []

    async def list_embedding_models(self):
        return []

    async def list_image_models(self):
        return []


def _make_iter_ctx_with_span() -> tuple[dict, MagicMock]:
    span = MagicMock()
    span.start_observation = MagicMock(return_value=MagicMock())
    return {"trace": span, "generation": span, "parent_ctx": None, "client": None}, span


@pytest.mark.asyncio
async def test_continuation_emits_subspan_per_continuation():
    iter_ctx, iter_span = _make_iter_ctx_with_span()
    # truncated_for=1 → 1 truncated continuation + 1 final = 2 continuations.
    llm = _MaxTokensLLM(truncated_for=1)

    initial = ChatResponse(content="head", model="stub", stop_reason="max_tokens")
    out = await al._continue_truncated_response(
        llm,
        [ChatMessage(role="user", content="go")],
        initial,
        on_token=None,
        on_event=None,
        model="stub",
        temperature=0.7,
        max_tokens=1024,
        max_session_chars=10_000,
        llm_extra={},
        merge_usage=lambda _u, served_model=None: None,
        parent_iter_ctx=iter_ctx,
    )

    assert out.content == "headchunk-1final"
    assert out.stop_reason == "end_turn"

    cont_calls = [
        c
        for c in iter_span.start_observation.call_args_list
        if c.kwargs.get("name", "").startswith("attempt:continuation:")
    ]
    assert [c.kwargs["name"] for c in cont_calls] == [
        "attempt:continuation:1",
        "attempt:continuation:2",
    ]
    assert all(c.kwargs["as_type"] == "generation" for c in cont_calls)
    assert all(
        c.kwargs["metadata"]["observation_kind"] == "continuation" for c in cont_calls
    )
    # delta_messages carries only the appended pair, never the full history.
    for c in cont_calls:
        assert len(c.kwargs["input"]["messages"]) == 2


@pytest.mark.asyncio
async def test_continuation_silent_when_no_parent_iter_ctx():
    llm = _MaxTokensLLM(truncated_for=1)
    initial = ChatResponse(content="head", model="stub", stop_reason="max_tokens")
    out = await al._continue_truncated_response(
        llm,
        [ChatMessage(role="user", content="go")],
        initial,
        on_token=None,
        on_event=None,
        model="stub",
        temperature=0.7,
        max_tokens=1024,
        max_session_chars=10_000,
        llm_extra={},
        merge_usage=lambda _u, served_model=None: None,
        # parent_iter_ctx omitted → defaults to None
    )
    # Loop still runs cleanly and produces final content; absence of
    # tracing must not break anything.
    assert out.content == "headchunk-1final"


@pytest.mark.asyncio
async def test_continuation_caps_at_max_and_tags_cap_hit():
    """When every attempt still returns ``max_tokens``, exactly
    ``_MAX_CONTINUATIONS`` continuations fire and the last one is
    tagged ``cap_hit=True``."""
    iter_ctx, iter_span = _make_iter_ctx_with_span()
    # Always truncated → forces the loop to exhaust its budget.
    llm = _MaxTokensLLM(truncated_for=999)

    initial = ChatResponse(content="head", model="stub", stop_reason="max_tokens")
    out = await al._continue_truncated_response(
        llm,
        [ChatMessage(role="user", content="go")],
        initial,
        on_token=None,
        on_event=None,
        model="stub",
        temperature=0.7,
        max_tokens=1024,
        max_session_chars=10_000,
        llm_extra={},
        merge_usage=lambda _u, served_model=None: None,
        parent_iter_ctx=iter_ctx,
    )

    assert out.stop_reason == "max_tokens"
    cont_calls = [
        c
        for c in iter_span.start_observation.call_args_list
        if c.kwargs.get("name", "").startswith("attempt:continuation:")
    ]
    assert len(cont_calls) == al._MAX_CONTINUATIONS
    # Only the last attempt carries cap_hit.
    cap_hits = [c.kwargs["metadata"].get("cap_hit") for c in cont_calls]
    assert cap_hits[:-1] == [None] * (al._MAX_CONTINUATIONS - 1)
    assert cap_hits[-1] is True


@pytest.mark.asyncio
async def test_continuation_error_closes_span_with_error(monkeypatch):
    """When the continuation LLM call raises, the continuation
    generation must be closed with an ``error=...`` annotation."""
    iter_ctx, _iter_span = _make_iter_ctx_with_span()

    end_calls: list[dict] = []

    def _fake_end_span(ctx, **kwargs):
        end_calls.append({"ctx": ctx, **kwargs})

    monkeypatch.setattr(al, "_lf_end_generation", _fake_end_span)

    class _FailingLLM(BaseLLMProvider):
        provider_name = "stub"
        context_window = 10_000

        async def chat(self, messages, **kwargs):
            raise RuntimeError("provider boom")

        async def chat_stream(self, messages, **kwargs):
            yield "x"

        async def embed(self, text, model=None):
            return [0.0]

        async def list_models(self):
            return []

        async def list_embedding_models(self):
            return []

        async def list_image_models(self):
            return []

    initial = ChatResponse(content="head", model="stub", stop_reason="max_tokens")
    await al._continue_truncated_response(
        _FailingLLM(),
        [ChatMessage(role="user", content="go")],
        initial,
        on_token=None,
        on_event=None,
        model="stub",
        temperature=0.7,
        max_tokens=1024,
        max_session_chars=10_000,
        llm_extra={},
        merge_usage=lambda _u, served_model=None: None,
        parent_iter_ctx=iter_ctx,
    )

    # Exactly one continuation was opened, and it was closed with an error.
    # ``error`` is the exception object itself (not ``str(exc)``) so the
    # Langfuse facade can render it as ``"RuntimeError: provider boom"`` and
    # keep the exception type visible in the trace.  This contract matters
    # for ``asyncio.CancelledError`` whose ``str()`` is empty — passing the
    # object preserves the cancellation signal.
    cont_ends = [c for c in end_calls if "error" in c]
    assert len(cont_ends) == 1
    err = cont_ends[0]["error"]
    assert isinstance(err, RuntimeError)
    assert "provider boom" in str(err)


@pytest.mark.asyncio
async def test_retry_inside_continuation_nests_under_continuation_gen():
    """Retries raised inside a continuation's LLM call must produce
    ``attempt:retry:N`` sub-spans on the continuation generation, not
    on the iteration span."""
    iter_ctx, iter_span = _make_iter_ctx_with_span()

    class _RetryThenSucceedLLM(BaseLLMProvider):
        provider_name = "stub"
        context_window = 10_000

        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages, **kwargs):
            self.calls += 1
            # Continuation call: use retry_llm_call to retry once, then
            # return a final answer to terminate the loop.
            attempts = {"n": 0}

            async def _inner():
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise RuntimeError("transient")
                return ChatResponse(
                    content="tail", model="stub", stop_reason="end_turn"
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

        async def list_embedding_models(self):
            return []

        async def list_image_models(self):
            return []

    llm = _RetryThenSucceedLLM()
    initial = ChatResponse(content="head", model="stub", stop_reason="max_tokens")
    out = await al._continue_truncated_response(
        llm,
        [ChatMessage(role="user", content="go")],
        initial,
        on_token=None,
        on_event=None,
        model="stub",
        temperature=0.7,
        max_tokens=1024,
        max_session_chars=10_000,
        llm_extra={},
        merge_usage=lambda _u, served_model=None: None,
        parent_iter_ctx=iter_ctx,
    )

    assert out.content == "headtail"
    # iter_span got exactly one child: the continuation gen. No retry spans.
    iter_children = iter_span.start_observation.call_args_list
    assert len(iter_children) == 1
    assert iter_children[0].kwargs["name"] == "attempt:continuation:1"
    assert all(
        not c.kwargs.get("name", "").startswith("attempt:retry:")
        for c in iter_children
    )

    # The continuation gen (returned mock from iter_span.start_observation)
    # saw the retry event.
    cont_gen_mock = iter_span.start_observation.return_value
    retry_calls = cont_gen_mock.create_event.call_args_list
    assert [c.kwargs["name"] for c in retry_calls] == ["attempt:retry:1"]


@pytest.mark.asyncio
async def test_continuation_propagates_cancellederror_after_closing_gen(monkeypatch):
    """``asyncio.CancelledError`` mid-continuation MUST close the
    ``attempt:continuation:N`` generation with the cancellation
    recorded AND propagate so the asyncio scheduler sees it.  The
    earlier shape (``except Exception``) silently swallowed
    ``CancelledError`` because it's a ``BaseException`` subclass;
    widening to ``except BaseException`` fixed visibility but tripped
    CodeQL's ``py/catch-base-exception`` rule.  The explicit
    ``except asyncio.CancelledError`` split (this test) preserves both
    contracts — trace fidelity AND structured-concurrency invariants.
    """
    iter_ctx, _iter_span = _make_iter_ctx_with_span()

    end_calls: list[dict] = []

    def _fake_end_span(ctx, **kwargs):
        end_calls.append({"ctx": ctx, **kwargs})

    monkeypatch.setattr(al, "_lf_end_generation", _fake_end_span)

    class _CancellingLLM(BaseLLMProvider):
        provider_name = "stub"
        context_window = 10_000

        async def chat(self, messages, **kwargs):
            raise asyncio.CancelledError()

        async def chat_stream(self, messages, **kwargs):
            yield "x"

        async def embed(self, text, model=None):
            return [0.0]

        async def list_models(self):
            return []

        async def list_embedding_models(self):
            return []

        async def list_image_models(self):
            return []

    initial = ChatResponse(content="head", model="stub", stop_reason="max_tokens")

    with pytest.raises(asyncio.CancelledError):
        await al._continue_truncated_response(
            _CancellingLLM(),
            [ChatMessage(role="user", content="go")],
            initial,
            on_token=None,
            on_event=None,
            model="stub",
            temperature=0.7,
            max_tokens=1024,
            max_session_chars=10_000,
            llm_extra={},
            merge_usage=lambda _u, served_model=None: None,
            parent_iter_ctx=iter_ctx,
        )

    # The continuation generation was closed with the cancellation
    # recorded (not left dangling) before the CancelledError propagated.
    cont_ends = [c for c in end_calls if "error" in c]
    assert len(cont_ends) == 1, (
        f"continuation generation must be closed exactly once on cancellation; "
        f"got {cont_ends}"
    )
    assert isinstance(cont_ends[0]["error"], asyncio.CancelledError), (
        f"error annotation must be the CancelledError instance so the facade "
        f"renders 'CancelledError: ...'; got {cont_ends[0]['error']!r}"
    )


@pytest.mark.asyncio
async def test_continuation_streaming_strips_on_thinking_from_llm_extra():
    """``_continue_truncated_response`` passes its own ``on_thinking`` cb
    explicitly to ``_streaming_chat`` AND spreads ``llm_extra``.  If a
    caller's ``llm_extra`` ever contains an ``on_thinking`` key (no current
    site does, but the function signature allows any dict), the un-filtered
    spread would crash with ``TypeError: got multiple values for keyword
    argument 'on_thinking'`` — and the crash would only surface when the
    LLM happened to truncate, turning an unrelated config change into a
    rescue-path failure.  The fix strips ``on_thinking`` from the spread
    dict so the explicit kwarg always wins; this test pins the contract.
    """
    captured_kwargs: dict = {}

    async def _fake_streaming_chat(_llm, _msgs, _on_token, **kwargs):
        captured_kwargs.update(kwargs)
        return ChatResponse(content="tail", model="stub", stop_reason="end_turn")

    import app.tools.agent_loop as _al_mod

    _orig_streaming = _al_mod._streaming_chat

    class _DummyLLM(BaseLLMProvider):
        provider_name = "stub"
        context_window = 10_000

        async def chat(self, messages, **kwargs):  # pragma: no cover - unused on streaming
            return ChatResponse(content="x", model="stub", stop_reason="end_turn")

        async def chat_stream(self, messages, **kwargs):
            yield "x"

        async def embed(self, text, model=None):
            return [0.0]

        async def list_models(self):
            return []

        async def list_embedding_models(self):
            return []

        async def list_image_models(self):
            return []

    explicit_cb = lambda _t: None  # noqa: E731 - sentinel, identity only
    poisoned_cb = lambda _t: None  # noqa: E731
    llm_extra_with_collision = {"reasoning_effort": "medium", "on_thinking": poisoned_cb}

    async def _on_token(_t):
        return None

    try:
        _al_mod._streaming_chat = _fake_streaming_chat
        initial = ChatResponse(content="head", model="stub", stop_reason="max_tokens")
        await al._continue_truncated_response(
            _DummyLLM(),
            [ChatMessage(role="user", content="go")],
            initial,
            on_token=_on_token,
            on_event=None,
            model="stub",
            temperature=0.7,
            max_tokens=1024,
            max_session_chars=10_000,
            llm_extra=llm_extra_with_collision,
            merge_usage=lambda _u, served_model=None: None,
            new_on_thinking=lambda: explicit_cb,
            parent_iter_ctx=None,
        )
    finally:
        _al_mod._streaming_chat = _orig_streaming

    # The continuation's own ``on_thinking`` won — the poisoned key from
    # ``llm_extra`` was filtered out before the dict spread, so Python
    # never had two values for the same kwarg.  ``reasoning_effort``
    # survived because it isn't the collision target.
    assert captured_kwargs["on_thinking"] is explicit_cb, (
        "the continuation's own on_thinking must be forwarded; got "
        f"{captured_kwargs.get('on_thinking')!r}"
    )
    assert captured_kwargs["reasoning_effort"] == "medium", (
        "non-colliding llm_extra keys must still flow through"
    )
