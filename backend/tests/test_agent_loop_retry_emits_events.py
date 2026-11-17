"""End-to-end wiring test: the agent loop pushes a Langfuse retry observer
around each ``llm.chat()`` call so transient errors retried by
``retry_llm_call`` show up as ``attempt:retry:N`` events on the
iteration generation span."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.llm.base import (
    BaseLLMProvider,
    ChatMessage,
    ChatResponse,
    retry_llm_call,
)
from app.tools import agent_loop as al


class _RetryableError(Exception):
    pass


class _RetryingStubLLM(BaseLLMProvider):
    """Stub LLM whose ``chat()`` raises a retryable error twice via
    ``retry_llm_call`` before returning a final response."""

    provider_name = "stub"
    context_window = 10_000

    def __init__(self) -> None:
        self.attempts = 0

    async def chat(
        self,
        messages,
        model=None,
        temperature=0.7,
        max_tokens=4096,
        tools=None,
        **kwargs,
    ):
        async def _do_call() -> ChatResponse:
            self.attempts += 1
            if self.attempts <= 2:
                raise _RetryableError(f"transient-{self.attempts}")
            return ChatResponse(content="final answer", model=model or "stub")

        return await retry_llm_call(
            _do_call,
            is_retryable=lambda exc: isinstance(exc, _RetryableError),
            max_retries=3,
            base_delay=0.001,
            max_delay=0.002,
        )

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


@pytest.mark.asyncio
async def test_agent_loop_emits_retry_events_under_iteration_generation(
    monkeypatch,
):
    """When the underlying provider retries inside ``retry_llm_call``, the
    iteration-generation span receives one ``attempt:retry:N`` event per
    retry, in order, before the loop succeeds."""

    # Capture the span attached to the iteration generation so we can
    # assert event creation on it.  Build a fresh per-call mock so each
    # iteration gen gets its own observable span.
    captured_spans: list[MagicMock] = []

    def _fake_start_iter_gen(_iter_ctx, **_kwargs):
        span = MagicMock()
        span.create_event = MagicMock()
        captured_spans.append(span)
        return {
            "trace": span,
            "generation": span,
            "parent_ctx": None,
            "client": None,
        }

    monkeypatch.setattr(al, "_lf_start_llm_gen", _fake_start_iter_gen)
    # Keep end_span / end_generation / iter_span / tool_span as no-ops so
    # the loop runs cleanly without pulling in real Langfuse plumbing.
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)

    llm = _RetryingStubLLM()
    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="hello")],
        tools=None,
        max_iterations=1,
    )

    assert response.content == "final answer"
    assert llm.attempts == 3, "stub should retry twice then succeed"

    # Exactly one iteration generation was created.
    assert len(captured_spans) == 1
    gen_span = captured_spans[0]

    # The iteration generation's span saw two retry events, named with
    # 1-based indices.
    retry_calls = gen_span.create_event.call_args_list
    assert len(retry_calls) == 2
    assert [c.kwargs["name"] for c in retry_calls] == [
        "attempt:retry:1",
        "attempt:retry:2",
    ]
    assert all(c.kwargs["level"] == "WARNING" for c in retry_calls)
    assert all(
        c.kwargs["metadata"]["error_type"] == "_RetryableError" for c in retry_calls
    )


@pytest.mark.asyncio
async def test_agent_loop_no_retry_events_on_first_call_success(monkeypatch):
    """A clean first-call success must NOT emit any ``attempt:retry`` events."""

    captured_spans: list[MagicMock] = []

    def _fake_start_iter_gen(_iter_ctx, **_kwargs):
        span = MagicMock()
        span.create_event = MagicMock()
        captured_spans.append(span)
        return {
            "trace": span,
            "generation": span,
            "parent_ctx": None,
            "client": None,
        }

    monkeypatch.setattr(al, "_lf_start_llm_gen", _fake_start_iter_gen)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)

    class _CleanLLM(BaseLLMProvider):
        provider_name = "stub"
        context_window = 10_000

        async def chat(self, messages, **kwargs):
            return ChatResponse(content="ok", model="stub")

        async def chat_stream(self, messages, **kwargs):
            yield "ok"

        async def embed(self, text, model=None):
            return [0.0]

        async def list_models(self):
            return []

    response = await al.run_agent_loop(
        llm=_CleanLLM(),
        messages=[ChatMessage(role="user", content="hi")],
        tools=None,
        max_iterations=1,
    )

    assert response.content == "ok"
    assert len(captured_spans) == 1
    gen_span = captured_spans[0]
    assert gen_span.create_event.call_args_list == []
