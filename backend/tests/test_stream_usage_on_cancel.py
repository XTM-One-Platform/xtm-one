"""The usage of a streamed LLM call that never returns.

A provider learns a stream's usage on the way: Anthropic's ``message_start``
(input and cache) and cumulative ``message_delta`` (output), Gemini's
cumulative ``usage_metadata`` on every chunk, and only at the very end
Bedrock's ``metadata`` event and OpenAI's ``include_usage`` chunk. A stream
cut short - a user stop, the loop's wall clock, a task cancel, an error
mid-way - used to record nothing, so the run budget, the traces and the usage
the run reports all undercounted the call.

These tests drive the real providers through ``run_agent_loop`` with fake
streams built from each SDK's own event and usage types (no network, no key),
cut them at several points, and read the result where it is recorded: the
per-run budget and the durable trace sink (``LocalPGSink`` pricing the spans
and the root trace the way it does in production).
"""

from __future__ import annotations

import asyncio
import inspect
import re
import threading
import time
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from anthropic.types import (
    InputJSONDelta,
    Message,
    MessageDeltaUsage,
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    RawContentBlockStopEvent,
    RawMessageDeltaEvent,
    RawMessageStartEvent,
    RawMessageStopEvent,
    TextBlock,
    TextDelta,
    ToolUseBlock,
    Usage,
)
from anthropic.types.raw_message_delta_event import Delta
from google.genai import types as genai_types
from openai.types.chat import ChatCompletionChunk
from openai.types.chat.chat_completion_chunk import Choice, ChoiceDelta
from openai.types.completion_usage import CompletionUsage

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.llm.bedrock_provider import BedrockProvider, _usage_from_bedrock
from app.llm.gemini_provider import GeminiProvider, _usage_from_gemini_metadata
from app.llm.openai_compat import OpenAICompatibleProvider, extract_openai_usage
from app.llm.openai_provider import OpenAIProvider
from app.services import langfuse_tracing as lft
from app.services.tracing.cost_calculator import ModelPrice, calculate_costs
from app.services.tracing.local_sink import LocalPGSink
from app.tools import agent_loop as al
from app.tools.base import CallableToolExecutor, CompositeToolExecutor, ToolDefinition
from app.tools.run_budget import RunBudget, count_usage_tokens

ANTHROPIC_MODEL = "claude-sonnet-4-6"
GEMINI_MODEL = "gemini-2.5-flash"
BEDROCK_MODEL = "anthropic.claude-sonnet-4-5"
OPENAI_MODEL = "gpt-4o"

# ---------------------------------------------------------------------------
# Scripted streams: SDK events, plus markers that cut the stream
# ---------------------------------------------------------------------------


class _Hang:
    """The stream stalls here for good: a cancellation lands on this await."""


HANG = _Hang()


class _Do:
    """Run ``fn`` when the stream reaches this point, then carry on."""

    def __init__(self, fn):
        self.fn = fn


class _Gate:
    """Block the (thread-pool) iterator until the test opens the gate."""

    def __init__(self) -> None:
        self.opened = threading.Event()


class _AsyncScript:
    """Async iterator over a script (Anthropic, Gemini, OpenAI streams)."""

    def __init__(self, items: list) -> None:
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        while self._items:
            item = self._items.pop(0)
            if item is HANG:
                await asyncio.Event().wait()
            if isinstance(item, _Do):
                item.fn()
                continue
            if isinstance(item, BaseException):
                raise item
            return item
        raise StopAsyncIteration


class _SyncScript:
    """Blocking iterator over a script (boto3's ``EventStream`` for Bedrock)."""

    def __init__(self, items: list) -> None:
        self._items = list(items)

    def __iter__(self):
        return self

    def __next__(self):
        while self._items:
            item = self._items.pop(0)
            if isinstance(item, _Gate):
                item.opened.wait(timeout=30)
                continue
            if isinstance(item, _Do):
                item.fn()
                continue
            if isinstance(item, BaseException):
                raise item
            return item
        raise StopIteration


class _AnthropicStreamCtx:
    def __init__(self, items: list) -> None:
        self._items = items

    async def __aenter__(self):
        return _AsyncScript(self._items)

    async def __aexit__(self, *_exc):
        return False


def _anthropic(*streams: list) -> AnthropicProvider:
    provider = AnthropicProvider(api_key="test-key")
    queue = list(streams)

    async def _create(**kwargs):
        # The provider reads the raw event stream (``messages.create(stream=True)``).
        assert kwargs.get("stream") is True
        return _AnthropicStreamCtx(queue.pop(0))

    provider.client = SimpleNamespace(messages=SimpleNamespace(create=_create))
    return provider


def _gemini(*streams: list) -> GeminiProvider:
    provider = GeminiProvider(api_key="dummy")
    queue = list(streams)

    class _Models:
        async def generate_content_stream(self, **_kwargs):
            return _AsyncScript(queue.pop(0))

    provider.client = SimpleNamespace(aio=SimpleNamespace(models=_Models()))
    return provider


def _bedrock(*streams: list) -> BedrockProvider:
    provider = BedrockProvider("us-east-1")
    queue = list(streams)

    class _Client:
        def converse_stream(self, **_kwargs):
            return {"stream": _SyncScript(queue.pop(0))}

    provider._client = _Client()
    return provider


def _openai(*streams: list) -> OpenAIProvider:
    provider = OpenAIProvider(api_key="sk-test")
    create = AsyncMock(side_effect=[_AsyncScript(items) for items in streams])
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return provider


def _compat(*streams: list) -> OpenAICompatibleProvider:
    """Every OpenAI-compatible provider (OpenRouter, Ollama, Mistral, ...):
    a ``chat_stream`` of its own, the shared consumer for tool calls."""
    provider = OpenAICompatibleProvider(api_key="sk-test")
    create = AsyncMock(side_effect=[_AsyncScript(items) for items in streams])
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return provider


# ── Anthropic events (the SDK's own raw stream event types) ──────────────


def _a_start(input_tokens: int, *, cache_read: int = 0, cache_write: int = 0):
    return RawMessageStartEvent(
        type="message_start",
        message=Message(
            id="msg_test",
            type="message",
            role="assistant",
            model=ANTHROPIC_MODEL,
            content=[],
            stop_reason=None,
            stop_sequence=None,
            usage=Usage(
                input_tokens=input_tokens,
                output_tokens=1,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=cache_write,
            ),
        ),
    )


def _a_text(*texts: str) -> list:
    events: list = [
        RawContentBlockStartEvent(
            type="content_block_start", index=0, content_block=TextBlock(type="text", text="")
        )
    ]
    events += [
        RawContentBlockDeltaEvent(
            type="content_block_delta", index=0, delta=TextDelta(type="text_delta", text=text)
        )
        for text in texts
    ]
    events.append(RawContentBlockStopEvent(type="content_block_stop", index=0))
    return events


def _a_tool_use() -> list:
    return [
        RawContentBlockStartEvent(
            type="content_block_start",
            index=0,
            content_block=ToolUseBlock(type="tool_use", id="toolu_1", name="lookup", input={}),
        ),
        RawContentBlockDeltaEvent(
            type="content_block_delta",
            index=0,
            delta=InputJSONDelta(type="input_json_delta", partial_json='{"q": "x"}'),
        ),
        RawContentBlockStopEvent(type="content_block_stop", index=0),
    ]


def _a_delta(output_tokens: int, stop_reason: str = "end_turn"):
    return RawMessageDeltaEvent(
        type="message_delta",
        delta=Delta(stop_reason=stop_reason, stop_sequence=None),
        usage=MessageDeltaUsage(output_tokens=output_tokens),
    )


def _a_stop():
    return RawMessageStopEvent(type="message_stop")


# What Anthropic reported at ``message_start`` for the prompt below: the
# uncached part, the cache write and the cache read (billed at three rates),
# plus the initial output token ``message_start`` always carries.
A_START_USAGE = {
    "input_tokens": 1_000,
    "output_tokens": 1,
    "cache_creation_input_tokens": 200,
    "cache_read_input_tokens": 3_000,
}


def _a_opening() -> RawMessageStartEvent:
    return _a_start(1_000, cache_write=200, cache_read=3_000)


# ── Gemini chunks (``GenerateContentResponse`` with cumulative usage) ────


def _g_chunk(text: str, *, prompt: int, candidates: int):
    return genai_types.GenerateContentResponse(
        candidates=[
            genai_types.Candidate(
                content=genai_types.Content(role="model", parts=[genai_types.Part(text=text)])
            )
        ],
        usage_metadata=genai_types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt,
            candidates_token_count=candidates,
            total_token_count=prompt + candidates,
        ),
    )


# ── Bedrock ConverseStream events (boto3 parses them into these dicts) ───


def _b_text(text: str) -> list:
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": text}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
    ]


def _b_stop() -> dict:
    return {"messageStop": {"stopReason": "end_turn"}}


def _b_metadata(input_tokens: int, output_tokens: int) -> dict:
    return {
        "metadata": {
            "usage": {
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "totalTokens": input_tokens + output_tokens,
            },
            "metrics": {"latencyMs": 12},
        }
    }


# ── OpenAI chunks (``ChatCompletionChunk``, usage on the final one) ──────


def _o_chunk(content: str | None, finish_reason: str | None = None) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id="chatcmpl-test",
        object="chat.completion.chunk",
        created=0,
        model=OPENAI_MODEL,
        choices=[
            Choice(
                index=0,
                delta=ChoiceDelta(role="assistant", content=content),
                finish_reason=finish_reason,
            )
        ],
    )


def _o_usage(prompt: int, completion: int) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id="chatcmpl-test",
        object="chat.completion.chunk",
        created=0,
        model=OPENAI_MODEL,
        choices=[],
        usage=CompletionUsage(
            prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion
        ),
    )


# ---------------------------------------------------------------------------
# Recording harness: a run budget and the durable trace sink
# ---------------------------------------------------------------------------


def _price() -> ModelPrice:
    return ModelPrice(
        provider="test",
        model_id="test-model",
        model_pattern=None,
        input_price_per_1m=Decimal("3"),
        output_price_per_1m=Decimal("15"),
        cache_read_price_per_1m=Decimal("0.30"),
        cache_write_price_per_1m=Decimal("3.75"),
        effective_from=date(2026, 1, 1),
        is_custom=False,
    )


def _budget() -> RunBudget:
    return RunBudget(max_total_tokens=100_000_000, price=_price())


def _root_trace_ctx() -> dict:
    return {
        "trace": None,
        "generation": None,
        "client": None,
        "parent_ctx": None,
        "metadata": {"source": "test"},
        "input_data": {"messages": []},
        "local_spans": [],
        "local_span_counter": 0,
        "local_trace_id": uuid.uuid4(),
        "local_source": "agent_chat",
        "local_name": "trace",
        "local_started_at": datetime.now(UTC),
        "local_metadata": {"source": "test"},
        "local_tags": [],
    }


class _FakeSession:
    def __init__(self) -> None:
        self.added: list = []
        self.added_all: list = []

    def add(self, row) -> None:
        self.added.append(row)

    def add_all(self, rows) -> None:
        self.added_all.extend(rows)

    async def flush(self) -> None:
        return None


def _lookup_tools() -> tuple[list[ToolDefinition], CompositeToolExecutor, list[str]]:
    ran: list[str] = []
    executor = CallableToolExecutor()

    async def _lookup(**_kwargs) -> str:
        ran.append("lookup")
        return "found it"

    executor.register("lookup", _lookup)
    definitions = [ToolDefinition(name="lookup", description="lookup", source="custom:test")]
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, executor)
    return definitions, composite, ran


async def _on_token(_chunk: str) -> None:
    return None


async def _run_loop(llm, root: dict, **kwargs) -> ChatResponse:
    kwargs.setdefault("model", ANTHROPIC_MODEL)
    token = lft.set_current_trace_ctx(root)
    try:
        return await al.run_agent_loop(
            llm=llm,
            messages=[ChatMessage(role="user", content="go")],
            max_iterations=4,
            max_tokens=1_024,
            progressive_tools=False,
            **kwargs,
        )
    finally:
        lft.reset_current_trace_ctx(token)


async def _flush_through_sink(root: dict, response: ChatResponse | None):
    """End the root trace like a caller does and price it in ``LocalPGSink``."""
    with patch("app.services.tracing.dispatcher.submit_trace_for_flush") as submit:
        if response is not None:
            lft.end_trace(root, output=response.content, usage=response.usage, model="test-model")
        else:
            lft.end_trace(root, error="cancelled")
    trace, spans = submit.call_args.args
    session = _FakeSession()
    price = AsyncMock(return_value=_price())
    with (
        patch("app.services.tracing.local_sink.resolve_model_price", price),
        patch("app.services.tracing.local_sink.resolve_model_price_any", price),
    ):
        await LocalPGSink(session_factory=lambda: None)._flush_trace_with_session(
            session, trace, spans
        )
    trace_row = session.added[0]
    generations = [row for row in session.added_all if row.type == "GENERATION"]
    return trace_row, generations


def _assert_budget_debited(budget: RunBudget, usage: dict) -> None:
    assert budget.spent_tokens == count_usage_tokens(usage)
    assert budget.spent_cost_usd == calculate_costs(usage, _price())["total_cost"]


def _assert_span_carries(span, usage: dict) -> None:
    normalized = lft._normalize_usage(usage)
    assert span.input_tokens == normalized["input"]
    assert span.output_tokens == normalized["output"]
    assert span.usage_details == normalized
    assert span.total_cost == calculate_costs(normalized, _price())["total_cost"]
    assert span.total_cost > 0


def _assert_span_carries_nothing(span) -> None:
    assert span.input_tokens is None
    assert span.output_tokens is None
    assert span.usage_details is None


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_cancel_after_message_start_records_the_reported_input():
    cancel = asyncio.Event()
    llm = _anthropic([_a_opening(), *_a_text("Let me"), _Do(cancel.set), HANG])
    tools, executor, ran = _lookup_tools()
    budget, root = _budget(), _root_trace_ctx()

    response = await _run_loop(
        llm, root, tools=tools, executor=executor, on_token=_on_token,
        cancel_event=cancel, budget=budget,
    )

    assert response.stop_reason == "cancelled"
    assert ran == []
    _assert_budget_debited(budget, A_START_USAGE)
    assert response.usage == A_START_USAGE
    trace_row, generations = await _flush_through_sink(root, response)
    assert len(generations) == 1
    assert generations[0].level == "ERROR"
    _assert_span_carries(generations[0], A_START_USAGE)
    assert trace_row.input_tokens == lft._normalize_usage(A_START_USAGE)["input"]
    assert trace_row.total_cost == calculate_costs(
        lft._normalize_usage(A_START_USAGE), _price()
    )["total_cost"]


@pytest.mark.asyncio
async def test_anthropic_cancel_after_text_deltas_never_estimates_output():
    """A user stop between chunks: the output streamed so far is text the
    provider never counted, so only what ``message_start`` reported is kept."""
    cancel = asyncio.Event()

    async def _stop_on_second_chunk(chunk: str) -> None:
        if chunk == "two ":
            cancel.set()

    llm = _anthropic(
        [_a_opening(), *_a_text("One ", "two ", "three ", "four"), HANG]
    )
    budget, root = _budget(), _root_trace_ctx()

    response = await _run_loop(
        llm, root, tools=None, on_token=_stop_on_second_chunk,
        cancel_event=cancel, budget=budget,
    )

    assert response.stop_reason == "cancelled"
    _assert_budget_debited(budget, A_START_USAGE)
    assert response.usage == A_START_USAGE
    _trace_row, generations = await _flush_through_sink(root, response)
    assert len(generations) == 1
    _assert_span_carries(generations[0], A_START_USAGE)


@pytest.mark.asyncio
async def test_anthropic_task_cancel_after_message_delta_records_the_cumulative_output():
    """The task itself is cancelled (a WebSocket teardown, a deadline): the
    cancellation still propagates, and the output ``message_delta`` reported
    rides with the input on the span and the budget."""
    parked = asyncio.Event()
    llm = _anthropic(
        [_a_opening(), *_a_text("Hello"), _a_delta(57), _Do(parked.set), HANG]
    )
    budget, root = _budget(), _root_trace_ctx()

    run = asyncio.ensure_future(
        _run_loop(llm, root, tools=None, on_token=_on_token, budget=budget)
    )
    await asyncio.wait_for(parked.wait(), timeout=5)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    reported = {**A_START_USAGE, "output_tokens": 57}
    _assert_budget_debited(budget, reported)
    _trace_row, generations = await _flush_through_sink(root, None)
    assert len(generations) == 1
    _assert_span_carries(generations[0], reported)


@pytest.mark.asyncio
async def test_anthropic_timeout_records_the_aborted_attempt_and_the_retry():
    """The loop's wall clock cuts the first stream after ``message_start``;
    the retried call then ends normally. Both were billed, both are recorded."""
    llm = _anthropic(
        [_a_opening(), *_a_text("Thinking"), HANG],
        [_a_start(1_500), *_a_text("Done."), _a_delta(40), _a_stop()],
    )
    tools, executor, _ran = _lookup_tools()
    budget, root = _budget(), _root_trace_ctx()

    # The first stream stalls for good, so this is how long the test waits;
    # the retried stream ends in microseconds, well inside the same cap.
    with (
        patch.object(al, "_LLM_CALL_TIMEOUT", 1.0),
        patch.object(al, "_LLM_TIMEOUT_RETRY_BACKOFF_SECS", 0.01),
    ):
        response = await _run_loop(
            llm, root, tools=tools, executor=executor, on_token=_on_token,
            budget=budget, wall_clock_deadline=time.monotonic() + 120,
        )

    assert response.content == "Done."
    final = {"input_tokens": 1_500, "output_tokens": 40}
    assert budget.spent_tokens == count_usage_tokens(A_START_USAGE) + count_usage_tokens(final)
    assert budget.spent_cost_usd == (
        calculate_costs(A_START_USAGE, _price())["total_cost"]
        + calculate_costs(final, _price())["total_cost"]
    )
    _trace_row, generations = await _flush_through_sink(root, response)
    assert len(generations) == 2
    aborted, completed = sorted(generations, key=lambda row: row.position)
    assert aborted.level == "ERROR"
    _assert_span_carries(aborted, A_START_USAGE)
    _assert_span_carries(completed, final)


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gemini_cancel_after_two_cumulative_chunks_records_the_latest_counts():
    cancel = asyncio.Event()
    first = _g_chunk("Part one, ", prompt=500, candidates=5)
    second = _g_chunk("part two", prompt=500, candidates=12)
    llm = _gemini([first, second, _Do(cancel.set), HANG])
    tools, executor, _ran = _lookup_tools()
    budget, root = _budget(), _root_trace_ctx()

    response = await _run_loop(
        llm, root, tools=tools, executor=executor, on_token=_on_token,
        model=GEMINI_MODEL, cancel_event=cancel, budget=budget,
    )

    # Cumulative counts: the second chunk's, never the two added together.
    reported = _usage_from_gemini_metadata(second.usage_metadata)
    assert reported["completion_tokens"] == 12
    assert response.stop_reason == "cancelled"
    _assert_budget_debited(budget, reported)
    _trace_row, generations = await _flush_through_sink(root, response)
    assert len(generations) == 1
    _assert_span_carries(generations[0], reported)


# ---------------------------------------------------------------------------
# Bedrock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bedrock_cancel_before_metadata_records_nothing():
    """ConverseStream reports usage only in its final ``metadata`` event: a
    stream stopped before it has nothing to record, and nothing is invented."""
    cancel = asyncio.Event()
    loop = asyncio.get_running_loop()
    gate = _Gate()
    llm = _bedrock(
        [
            *_b_text("Hi"),
            _Do(lambda: loop.call_soon_threadsafe(cancel.set)),
            gate,
            _b_stop(),
            _b_metadata(900, 30),
        ]
    )
    tools, executor, _ran = _lookup_tools()
    budget, root = _budget(), _root_trace_ctx()

    try:
        response = await _run_loop(
            llm, root, tools=tools, executor=executor, on_token=_on_token,
            model=BEDROCK_MODEL, cancel_event=cancel, budget=budget,
        )
    finally:
        gate.opened.set()

    assert response.stop_reason == "cancelled"
    assert budget.spent_tokens == 0
    assert budget.spent_cost_usd == 0
    assert response.usage == {}
    _trace_row, generations = await _flush_through_sink(root, response)
    assert len(generations) == 1
    assert generations[0].level == "ERROR"
    _assert_span_carries_nothing(generations[0])


@pytest.mark.asyncio
async def test_bedrock_abort_after_metadata_records_it():
    llm = _bedrock(
        [*_b_text("Hi"), _b_stop(), _b_metadata(900, 30), ConnectionError("reset by peer")]
    )
    tools, executor, _ran = _lookup_tools()
    budget, root = _budget(), _root_trace_ctx()

    token = lft.set_current_trace_ctx(root)
    try:
        with pytest.raises(ConnectionError):
            await al.run_agent_loop(
                llm=llm,
                messages=[ChatMessage(role="user", content="go")],
                tools=tools,
                executor=executor,
                on_token=_on_token,
                model=BEDROCK_MODEL,
                max_iterations=4,
                max_tokens=1_024,
                progressive_tools=False,
                budget=budget,
            )
    finally:
        lft.reset_current_trace_ctx(token)

    reported = _usage_from_bedrock({"inputTokens": 900, "outputTokens": 30})
    _assert_budget_debited(budget, reported)
    _trace_row, generations = await _flush_through_sink(root, None)
    assert len(generations) == 1
    _assert_span_carries(generations[0], reported)


# ---------------------------------------------------------------------------
# OpenAI (and every OpenAI-compatible provider: the same consumer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_cancel_before_the_usage_chunk_records_nothing():
    cancel = asyncio.Event()
    llm = _openai(
        [_o_chunk("Hi"), _o_chunk(" there", finish_reason="stop"), _Do(cancel.set), HANG]
    )
    tools, executor, _ran = _lookup_tools()
    budget, root = _budget(), _root_trace_ctx()

    response = await _run_loop(
        llm, root, tools=tools, executor=executor, on_token=_on_token,
        model=OPENAI_MODEL, cancel_event=cancel, budget=budget,
    )

    assert response.stop_reason == "cancelled"
    assert budget.spent_tokens == 0
    _trace_row, generations = await _flush_through_sink(root, response)
    assert len(generations) == 1
    _assert_span_carries_nothing(generations[0])


@pytest.mark.asyncio
async def test_openai_abort_after_the_usage_chunk_records_it():
    usage_chunk = _o_usage(800, 20)
    llm = _openai(
        [
            _o_chunk("Hi"),
            _o_chunk(" there", finish_reason="stop"),
            usage_chunk,
            ConnectionError("stream closed before [DONE]"),
        ]
    )
    tools, executor, _ran = _lookup_tools()
    budget, root = _budget(), _root_trace_ctx()

    token = lft.set_current_trace_ctx(root)
    try:
        with pytest.raises(ConnectionError):
            await al.run_agent_loop(
                llm=llm,
                messages=[ChatMessage(role="user", content="go")],
                tools=tools,
                executor=executor,
                on_token=_on_token,
                model=OPENAI_MODEL,
                max_iterations=4,
                max_tokens=1_024,
                progressive_tools=False,
                budget=budget,
            )
    finally:
        lft.reset_current_trace_ctx(token)

    reported = extract_openai_usage(usage_chunk.usage)
    _assert_budget_debited(budget, reported)
    _trace_row, generations = await _flush_through_sink(root, None)
    assert len(generations) == 1
    _assert_span_carries(generations[0], reported)


# ---------------------------------------------------------------------------
# A stream that ends normally records exactly what it recorded before
# ---------------------------------------------------------------------------


def _normal_end_cases() -> list:
    return [
        pytest.param(
            lambda: _anthropic(
                [_a_opening(), *_a_text("All done."), _a_delta(40), _a_stop()]
            ),
            ANTHROPIC_MODEL,
            {**A_START_USAGE, "output_tokens": 40},
            id="anthropic",
        ),
        pytest.param(
            lambda: _gemini(
                [
                    _g_chunk("All ", prompt=500, candidates=4),
                    _g_chunk("done.", prompt=500, candidates=9),
                ]
            ),
            GEMINI_MODEL,
            {"prompt_tokens": 500, "completion_tokens": 9},
            id="gemini",
        ),
        pytest.param(
            lambda: _bedrock([*_b_text("All done."), _b_stop(), _b_metadata(900, 30)]),
            BEDROCK_MODEL,
            {"prompt_tokens": 900, "completion_tokens": 30},
            id="bedrock",
        ),
        pytest.param(
            lambda: _openai(
                [_o_chunk("All done.", finish_reason="stop"), _o_usage(800, 20)]
            ),
            OPENAI_MODEL,
            {"prompt_tokens": 800, "completion_tokens": 20, "total_tokens": 820},
            id="openai",
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("make_llm", "model", "final_usage"), _normal_end_cases())
async def test_a_normally_ended_stream_is_recorded_once_and_unchanged(
    make_llm, model, final_usage
):
    tools, executor, _ran = _lookup_tools()
    budget, root = _budget(), _root_trace_ctx()

    response = await _run_loop(
        make_llm(), root, tools=tools, executor=executor, on_token=_on_token,
        model=model, budget=budget,
    )

    assert response.content == "All done."
    assert response.usage == final_usage
    _assert_budget_debited(budget, final_usage)
    _trace_row, generations = await _flush_through_sink(root, response)
    assert len(generations) == 1
    assert generations[0].level == "DEFAULT"
    _assert_span_carries(generations[0], final_usage)


@pytest.mark.asyncio
async def test_a_normally_ended_text_stream_is_recorded_once_and_unchanged():
    llm = _anthropic([_a_opening(), *_a_text("All done."), _a_delta(40), _a_stop()])
    budget, root = _budget(), _root_trace_ctx()

    response = await _run_loop(llm, root, tools=None, on_token=_on_token, budget=budget)

    final = {**A_START_USAGE, "output_tokens": 40}
    assert response.content == "All done."
    assert response.usage == final
    _assert_budget_debited(budget, final)
    _trace_row, generations = await _flush_through_sink(root, response)
    assert len(generations) == 1
    _assert_span_carries(generations[0], final)


# ---------------------------------------------------------------------------
# A turn without tools: every provider's own ``chat_stream``
# ---------------------------------------------------------------------------


def _text_normal_end_cases() -> list:
    return [
        pytest.param(
            lambda: _gemini(
                [
                    _g_chunk("All ", prompt=500, candidates=4),
                    _g_chunk("done.", prompt=500, candidates=9),
                ]
            ),
            GEMINI_MODEL,
            {"prompt_tokens": 500, "completion_tokens": 9},
            id="gemini",
        ),
        pytest.param(
            lambda: _bedrock([*_b_text("All done."), _b_stop(), _b_metadata(900, 30)]),
            BEDROCK_MODEL,
            {"prompt_tokens": 900, "completion_tokens": 30},
            id="bedrock",
        ),
        pytest.param(
            lambda: _openai([_o_chunk("All done.", finish_reason="stop"), _o_usage(800, 20)]),
            OPENAI_MODEL,
            {"prompt_tokens": 800, "completion_tokens": 20, "total_tokens": 820},
            id="openai",
        ),
        pytest.param(
            lambda: _compat([_o_chunk("All done.", finish_reason="stop"), _o_usage(800, 20)]),
            OPENAI_MODEL,
            {"prompt_tokens": 800, "completion_tokens": 20, "total_tokens": 820},
            id="openai-compatible",
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("make_llm", "model", "final_usage"), _text_normal_end_cases())
async def test_every_provider_records_a_normally_ended_text_stream_once_and_unchanged(
    make_llm, model, final_usage
):
    budget, root = _budget(), _root_trace_ctx()

    response = await _run_loop(
        make_llm(), root, tools=None, on_token=_on_token, model=model, budget=budget
    )

    assert response.content == "All done."
    assert response.usage == final_usage
    _assert_budget_debited(budget, final_usage)
    _trace_row, generations = await _flush_through_sink(root, response)
    assert len(generations) == 1
    assert generations[0].level == "DEFAULT"
    _assert_span_carries(generations[0], final_usage)


_TEXT_LAST_GEMINI_CHUNK = _g_chunk("part two", prompt=500, candidates=12)
_TEXT_USAGE_CHUNK = _o_usage(800, 20)


def _text_cut_after_usage_cases() -> list:
    def _openai_items() -> list:
        return [
            _o_chunk("Hi"),
            _o_chunk(" there", finish_reason="stop"),
            _TEXT_USAGE_CHUNK,
            ConnectionError("stream closed before [DONE]"),
        ]

    return [
        pytest.param(
            lambda: _gemini(
                [
                    _g_chunk("Part one, ", prompt=500, candidates=5),
                    _TEXT_LAST_GEMINI_CHUNK,
                    ConnectionError("reset by peer"),
                ]
            ),
            GEMINI_MODEL,
            _usage_from_gemini_metadata(_TEXT_LAST_GEMINI_CHUNK.usage_metadata),
            id="gemini",
        ),
        pytest.param(
            lambda: _bedrock(
                [*_b_text("Hi"), _b_stop(), _b_metadata(900, 30), ConnectionError("reset by peer")]
            ),
            BEDROCK_MODEL,
            _usage_from_bedrock({"inputTokens": 900, "outputTokens": 30}),
            id="bedrock",
        ),
        pytest.param(
            lambda: _openai(_openai_items()),
            OPENAI_MODEL,
            extract_openai_usage(_TEXT_USAGE_CHUNK.usage),
            id="openai",
        ),
        pytest.param(
            lambda: _compat(_openai_items()),
            OPENAI_MODEL,
            extract_openai_usage(_TEXT_USAGE_CHUNK.usage),
            id="openai-compatible",
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("make_llm", "model", "reported"), _text_cut_after_usage_cases())
async def test_a_text_stream_failing_after_its_usage_records_it_once(make_llm, model, reported):
    """The error reaches the caller, and what the provider reported before it
    rides on the span and the budget, once."""
    budget, root = _budget(), _root_trace_ctx()

    with pytest.raises(ConnectionError):
        await _run_loop(
            make_llm(), root, tools=None, on_token=_on_token, model=model, budget=budget
        )

    _assert_budget_debited(budget, reported)
    _trace_row, generations = await _flush_through_sink(root, None)
    assert len(generations) == 1
    assert generations[0].level == "ERROR"
    _assert_span_carries(generations[0], reported)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("make_llm", "model"),
    [
        pytest.param(
            lambda: _bedrock([*_b_text("Hi"), ConnectionError("reset by peer")]),
            BEDROCK_MODEL,
            id="bedrock",
        ),
        pytest.param(
            lambda: _compat([_o_chunk("Hi"), ConnectionError("reset by peer")]),
            OPENAI_MODEL,
            id="openai-compatible",
        ),
    ],
)
async def test_a_text_stream_failing_before_its_usage_records_nothing(make_llm, model):
    budget, root = _budget(), _root_trace_ctx()

    with pytest.raises(ConnectionError):
        await _run_loop(
            make_llm(), root, tools=None, on_token=_on_token, model=model, budget=budget
        )

    assert budget.spent_tokens == 0
    _trace_row, generations = await _flush_through_sink(root, None)
    assert len(generations) == 1
    assert generations[0].level == "ERROR"
    _assert_span_carries_nothing(generations[0])


# ---------------------------------------------------------------------------
# A cancellation right after the end: recorded once, whichever side wins
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tail",
    [
        # The stream finishes as the cancel lands: the call returns first.
        pytest.param("finishes", id="call-returns"),
        # The end usage is reported, then the stream stalls: the cancel wins.
        pytest.param("stalls", id="cancel-wins"),
    ],
)
async def test_a_cancel_right_after_the_end_usage_is_not_counted_twice(tail):
    cancel = asyncio.Event()
    end = [_Do(cancel.set), _a_stop()] if tail == "finishes" else [_Do(cancel.set), HANG]
    llm = _anthropic([_a_opening(), *_a_text("All done."), _a_delta(40), *end])
    tools, executor, _ran = _lookup_tools()
    budget, root = _budget(), _root_trace_ctx()

    response = await _run_loop(
        llm, root, tools=tools, executor=executor, on_token=_on_token,
        cancel_event=cancel, budget=budget,
    )

    final = {**A_START_USAGE, "output_tokens": 40}
    _assert_budget_debited(budget, final)
    assert response.usage == final
    _trace_row, generations = await _flush_through_sink(root, response)
    assert len(generations) == 1
    _assert_span_carries(generations[0], final)


@pytest.mark.asyncio
async def test_a_cancel_after_a_completed_call_keeps_its_usage_once():
    """The call returned (a tool call), the user stopped the run during the
    tool: the completed call is on the budget once, and the cancelled
    response carries what the run spent."""
    cancel = asyncio.Event()
    llm = _anthropic([_a_opening(), *_a_tool_use(), _a_delta(25, stop_reason="tool_use"), _a_stop()])
    executor = CallableToolExecutor()

    async def _lookup(**_kwargs) -> str:
        cancel.set()
        return "found it"

    executor.register("lookup", _lookup)
    tools = [ToolDefinition(name="lookup", description="lookup", source="custom:test")]
    composite = CompositeToolExecutor()
    composite.add_tools(tools, executor)
    budget, root = _budget(), _root_trace_ctx()

    response = await _run_loop(
        llm, root, tools=tools, executor=composite, on_token=_on_token,
        cancel_event=cancel, budget=budget,
    )

    completed = {**A_START_USAGE, "output_tokens": 25}
    assert response.stop_reason == "cancelled"
    assert response.usage == completed
    _assert_budget_debited(budget, completed)
    _trace_row, generations = await _flush_through_sink(root, response)
    assert len(generations) == 1
    assert generations[0].level == "DEFAULT"
    _assert_span_carries(generations[0], completed)


def test_every_cancelled_response_of_the_loop_carries_the_run_usage():
    """Read off the source: a cancel site building its response without the
    run's accumulated usage drops it from the root trace and the run's token
    totals, and nothing else would notice (a new site, or a merge bringing
    one)."""
    source = inspect.getsource(al.run_agent_loop)
    calls = re.findall(r"_cancelled_chat_response\(([^)]*)\)", source)

    assert calls, "run_agent_loop no longer builds a cancelled response"
    assert {args.replace(" ", "") for args in calls} == {"model,accumulated_usage"}, calls


# ---------------------------------------------------------------------------
# The loop's other streamed calls: the idle watchdog, continuations, backstop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_idle_watchdog_leaves_what_the_stream_reported_in_the_sink():
    """The watchdog aborts a stalled stream with ``TimeoutError``; the sink
    still holds what ``message_start`` reported, for the error handler."""
    from app.llm.base import StreamUsageSink

    llm = _anthropic([_a_opening(), *_a_text("Thinking"), HANG])
    sink = StreamUsageSink()

    # The stream stalls for good, so this is how long the test waits; a
    # second leaves a loaded runner room to open the stream before it fires.
    with pytest.raises(TimeoutError):
        await al._streaming_chat(
            llm,
            [ChatMessage(role="user", content="go")],
            _on_token,
            model=ANTHROPIC_MODEL,
            max_tokens=1_024,
            idle_timeout=1.0,
            usage_sink=sink,
        )

    assert sink.take() == A_START_USAGE


@pytest.mark.asyncio
async def test_an_aborted_continuation_records_what_its_stream_reported_once():
    """A max_tokens continuation stopped by the user: its ``message_start``
    reaches the usage hook once, and the cancellation reaches the caller."""
    cancel = asyncio.Event()

    async def _stop_on_first_chunk(_chunk: str) -> None:
        cancel.set()

    llm = _anthropic([_a_start(2_000), *_a_text("more ", "text"), HANG])
    merged: list[dict] = []
    truncated = ChatResponse(
        content="Part one", model=ANTHROPIC_MODEL, stop_reason="max_tokens"
    )

    with pytest.raises(al._CancelledMidCallError):
        await al._continue_truncated_response(
            llm,
            [ChatMessage(role="user", content="go")],
            truncated,
            on_token=_stop_on_first_chunk,
            on_event=None,
            model=ANTHROPIC_MODEL,
            temperature=0.7,
            max_tokens=1_024,
            max_session_chars=1_000_000,
            llm_extra={},
            merge_usage=lambda usage, served_model=None: merged.append(usage),
            cancel_event=cancel,
        )

    assert merged == [{"input_tokens": 2_000, "output_tokens": 1}]


@pytest.mark.asyncio
async def test_an_aborted_forced_answer_records_what_its_stream_reported_once():
    """Two empty answers send the loop to the forced-answer backstop, and a
    user stop cuts the synthesis after ``message_start``: the synthesis is on
    the budget once, next to the two completed calls, and the cancelled
    response carries all three."""
    cancel = asyncio.Event()

    async def _stop_on_the_synthesis(chunk: str) -> None:
        if chunk == "Partial ":
            cancel.set()

    llm = _anthropic(
        [_a_start(1_000), _a_delta(3), _a_stop()],
        [_a_start(1_100), _a_delta(2), _a_stop()],
        [_a_start(1_200), *_a_text("Partial ", "answer"), HANG],
    )
    budget, root = _budget(), _root_trace_ctx()

    response = await _run_loop(
        llm, root, tools=None, on_token=_stop_on_the_synthesis,
        cancel_event=cancel, budget=budget,
    )

    calls = (
        {"input_tokens": 1_000, "output_tokens": 3},
        {"input_tokens": 1_100, "output_tokens": 2},
        {"input_tokens": 1_200, "output_tokens": 1},
    )
    assert response.stop_reason == "cancelled"
    assert response.usage == {"input_tokens": 3_300, "output_tokens": 6}
    assert budget.spent_tokens == sum(count_usage_tokens(usage) for usage in calls)
    assert budget.spent_cost_usd == sum(
        calculate_costs(usage, _price())["total_cost"] for usage in calls
    )
    trace_row, _generations = await _flush_through_sink(root, response)
    assert trace_row.input_tokens == 3_300


# ---------------------------------------------------------------------------
# The sink contract and the paths around it
# ---------------------------------------------------------------------------


def test_the_sink_keeps_the_latest_snapshot_and_hands_it_out_once():
    from app.llm.base import StreamUsageSink

    sink = StreamUsageSink()
    sink.report({"input_tokens": 10, "output_tokens": 1})
    sink.report({"input_tokens": 10, "output_tokens": 7})
    sink.report({})
    assert sink.take() == {"input_tokens": 10, "output_tokens": 7}
    assert sink.take() == {}
    sink.report({"input_tokens": 99})
    assert sink.take() == {}


def test_a_settled_sink_records_nothing_and_a_restart_forgets_the_abandoned_attempt():
    from app.llm.base import StreamUsageSink

    settled = StreamUsageSink()
    settled.report({"input_tokens": 10})
    settled.settle()
    assert settled.take() == {}

    retried = StreamUsageSink()
    retried.report({"input_tokens": 10, "output_tokens": 1})
    retried.restart()
    assert retried.take() == {}


def test_a_failing_usage_hook_never_replaces_the_error_being_handled():
    """The aborted call is closed from inside the handler of its error: a
    usage hook that raises is logged, and the handler still re-raises the
    cancellation it was handling."""
    from app.llm.base import StreamUsageSink

    sink = StreamUsageSink()
    sink.report({"input_tokens": 5, "output_tokens": 2})

    def _raising_hook(_usage: dict, served_model: str | None = None) -> None:
        raise ValueError("unreadable usage")

    with pytest.raises(asyncio.CancelledError):
        try:
            raise asyncio.CancelledError
        except asyncio.CancelledError as exc:
            al._end_aborted_generation(None, exc, sink, _raising_hook)
            raise
    assert sink.take() == {}


@pytest.mark.asyncio
async def test_a_failover_attempt_restarts_the_sink():
    """The primary streams ``message_start`` and fails before emitting
    anything; the fallback's own attempt is the one the sink describes, and
    the returned usage is the fallback's, as before."""
    from app.llm.base import StreamUsageSink
    from app.llm.router import FailoverLLMProvider

    # A builtin ``ConnectionError`` is not an Anthropic transient error, so
    # the primary gives up at once and the chain moves to the fallback.
    primary = _anthropic([_a_opening(), ConnectionError("dropped")])
    fallback = _anthropic([_a_start(700), *_a_text("From the fallback."), _a_delta(9), _a_stop()])
    wrapper = FailoverLLMProvider(primary=primary, fallbacks=[fallback])
    sink = StreamUsageSink()

    response = await wrapper.chat_stream_with_tools(
        [ChatMessage(role="user", content="go")],
        tools=None,
        model=ANTHROPIC_MODEL,
        usage_sink=sink,
    )

    assert response.usage == {"input_tokens": 700, "output_tokens": 9}
    assert sink.take() == {"input_tokens": 700, "output_tokens": 9}


@pytest.mark.asyncio
async def test_the_non_streaming_fallback_reports_the_usage_it_received():
    """A provider without native streaming answers in one ``chat()`` call:
    once that returns, the whole usage is known, even if the simulated token
    delivery that follows is cancelled."""
    from app.llm.base import StreamUsageSink

    class _OneShot(BaseLLMProvider):
        provider_name = "one-shot"

        async def chat(self, messages, model=None, **kwargs):
            assert "usage_sink" not in kwargs
            return ChatResponse(
                content="answer", model="m", usage={"input_tokens": 30, "output_tokens": 4}
            )

        async def embed(self, text, model=None):
            return [0.0]

        async def list_models(self):
            return []

    async def _cancelled(_chunk: str) -> None:
        raise asyncio.CancelledError

    sink = StreamUsageSink()
    with pytest.raises(asyncio.CancelledError):
        await _OneShot().chat_stream_with_tools(
            [ChatMessage(role="user", content="go")], on_token=_cancelled, usage_sink=sink
        )
    assert sink.take() == {"input_tokens": 30, "output_tokens": 4}


def test_an_error_close_keeps_the_usage_it_is_given_and_only_that():
    root = _root_trace_ctx()
    iter_ctx = lft.start_iteration_span(iteration=1, parent_ctx=root)
    with_usage = lft.start_llm_generation(iter_ctx, model="test-model")
    without_usage = lft.start_llm_generation(iter_ctx, model="test-model")

    lft.end_generation(
        with_usage, error=asyncio.CancelledError(), usage={"input_tokens": 5, "output_tokens": 2}
    )
    lft.end_generation(without_usage, error=asyncio.CancelledError())

    first, second = root["local_spans"]
    assert first["level"] == "ERROR"
    assert first["input_tokens"] == 5
    assert first["output_tokens"] == 2
    assert first["usage_details"] == lft._normalize_usage({"input_tokens": 5, "output_tokens": 2})
    assert second["level"] == "ERROR"
    assert second["input_tokens"] is None
    assert second["usage_details"] is None


class _RecordingSpan:
    """A Langfuse root span that records what it is told."""

    trace_id = "trace-test"

    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.ended = False

    def update(self, **kwargs) -> None:
        self.updates.append(kwargs)

    def end(self) -> None:
        self.ended = True

    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


@pytest.mark.parametrize(
    "usage",
    [pytest.param({"input_tokens": 5, "output_tokens": 2}, id="reported"), pytest.param(None, id="none")],
)
def test_an_error_end_of_the_root_trace_keeps_the_usage_it_is_given_and_only_that(usage):
    """A request cut short (a proxy stream its client left) closes its root
    trace with an error: the usage it reported reaches Langfuse's rollup like
    on a normal end, not only the durable trace."""
    span = _RecordingSpan()
    root = {**_root_trace_ctx(), "trace": span}

    with patch("app.services.tracing.dispatcher.submit_trace_for_flush") as submit:
        lft.end_trace(root, error="The client disconnected", usage=usage)

    rollups = [
        update["metadata"]["total_usage"]
        for update in span.updates
        if "total_usage" in (update.get("metadata") or {})
    ]
    trace, _spans = submit.call_args.args
    assert span.ended
    assert trace.level == "ERROR"
    if usage:
        assert rollups == [lft._normalize_usage(usage)]
        assert trace.usage_details == lft._normalize_usage(usage)
    else:
        assert rollups == []
        assert trace.usage_details is None
