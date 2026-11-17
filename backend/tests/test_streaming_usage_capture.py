"""Streaming token-usage capture (issue: Langfuse missing tokens/cost).

Every provider's streaming path used to return ``usage={}`` (and the
streaming-text generators returned no usage at all), so interactive chat —
which always streams — recorded zero tokens and therefore zero cost in
Langfuse, while non-streaming assignment runs were costed correctly
("not always logged").  These tests pin the contract that all streaming
paths now surface real token usage.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.genai import types as genai_types

from app.llm.anthropic_provider import AnthropicProvider, _StreamUsageAccumulator
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.llm.bedrock_provider import BedrockProvider, _usage_from_bedrock
from app.llm.gemini_provider import GeminiProvider, _usage_from_gemini_metadata
from app.llm.openai_compat import (
    consume_openai_stream_with_tools,
    extract_openai_usage,
)
from app.services.langfuse_tracing import _normalize_usage
from app.services.tracing.cost_calculator import ModelPrice, calculate_costs
from app.tools.agent_loop import _streaming_chat
from app.tools.base import ToolDefinition
from app.tools.run_budget import RunBudget

# ---------------------------------------------------------------------------
# Async stream stubs
# ---------------------------------------------------------------------------


class _AsyncEventIterator:
    """Async iterator over a fixed list of events."""

    def __init__(self, events: list):
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


class _StreamCtx:
    """The stream ``client.messages.create(stream=True)`` returns."""

    def __init__(self, events: list):
        self._events = events

    async def __aenter__(self):
        return _AsyncEventIterator(self._events)

    async def __aexit__(self, exc_type, exc, tb):
        return False


# ---------------------------------------------------------------------------
# Pure usage-normalization helpers
# ---------------------------------------------------------------------------


def test_extract_openai_usage_normalizes_tokens():
    usage = SimpleNamespace(prompt_tokens=120, completion_tokens=34)
    assert extract_openai_usage(usage) == {
        "prompt_tokens": 120,
        "completion_tokens": 34,
    }


def test_extract_openai_usage_keeps_nested_token_details():
    usage = SimpleNamespace(
        prompt_tokens=5,
        completion_tokens=11,
        total_tokens=16,
        prompt_tokens_details=SimpleNamespace(cached_tokens=2, audio_tokens=3),
        completion_tokens_details=SimpleNamespace(
            text_tokens=3,
            audio_tokens=4,
            reasoning_tokens=4,
        ),
    )
    assert extract_openai_usage(usage) == {
        "prompt_tokens": 5,
        "completion_tokens": 11,
        "total_tokens": 16,
        "prompt_tokens_details": {"cached_tokens": 2, "audio_tokens": 3},
        "completion_tokens_details": {
            "text_tokens": 3,
            "audio_tokens": 4,
            "reasoning_tokens": 4,
        },
    }


def test_extract_openai_usage_handles_missing():
    assert extract_openai_usage(None) == {}
    assert extract_openai_usage(SimpleNamespace(prompt_tokens=0, completion_tokens=0)) == {}


def test_usage_from_gemini_metadata():
    meta = SimpleNamespace(prompt_token_count=80, candidates_token_count=12)
    assert _usage_from_gemini_metadata(meta) == {
        "prompt_tokens": 80,
        "completion_tokens": 12,
    }
    assert _usage_from_gemini_metadata(None) == {}


def test_usage_from_bedrock():
    assert _usage_from_bedrock({"inputTokens": 200, "outputTokens": 50}) == {
        "prompt_tokens": 200,
        "completion_tokens": 50,
    }
    assert _usage_from_bedrock(None) == {}
    assert _usage_from_bedrock({}) == {}


# ---------------------------------------------------------------------------
# Anthropic accumulator + chat_stream_with_tools
# ---------------------------------------------------------------------------


def test_anthropic_usage_accumulator_folds_start_and_delta():
    acc = _StreamUsageAccumulator()
    acc.absorb_message_start(
        SimpleNamespace(
            message=SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=100,
                    output_tokens=1,
                    cache_creation_input_tokens=10,
                    cache_read_input_tokens=5,
                )
            )
        )
    )
    acc.absorb_message_delta(SimpleNamespace(usage=SimpleNamespace(output_tokens=42)))
    assert acc.to_dict() == {
        "input_tokens": 100,
        "output_tokens": 42,
        "cache_creation_input_tokens": 10,
        "cache_read_input_tokens": 5,
    }


def test_anthropic_usage_accumulator_empty_is_falsy():
    assert _StreamUsageAccumulator().to_dict() == {}


@pytest.mark.asyncio
async def test_anthropic_chat_stream_with_tools_captures_usage():
    provider = AnthropicProvider(api_key="test-key")
    events = [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=321,
                    output_tokens=1,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=0,
                )
            ),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="Hello world"),
        ),
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="end_turn"),
            usage=SimpleNamespace(output_tokens=99),
        ),
    ]
    provider.client = MagicMock()
    provider.client.messages.create = AsyncMock(return_value=_StreamCtx(events))

    resp = await provider.chat_stream_with_tools(
        messages=[ChatMessage(role="user", content="hi")],
        model="claude-sonnet-4-6",
    )

    assert resp.content == "Hello world"
    assert resp.usage == {"input_tokens": 321, "output_tokens": 99}


# ---------------------------------------------------------------------------
# OpenAI-compatible streaming-with-tools
# ---------------------------------------------------------------------------


def _openai_text_chunk(text: str):
    delta = SimpleNamespace(content=text, tool_calls=None, reasoning_content=None)
    choice = SimpleNamespace(delta=delta, finish_reason=None)
    return SimpleNamespace(choices=[choice], usage=None)


def _openai_usage_chunk(prompt: int, completion: int):
    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
    )


@pytest.mark.asyncio
async def test_consume_openai_stream_captures_usage_and_requests_it():
    chunks = [_openai_text_chunk("Hi "), _openai_text_chunk("there"), _openai_usage_chunk(40, 8)]
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=_AsyncEventIterator(chunks))

    resp = await consume_openai_stream_with_tools(
        client,
        model="gpt-4o",
        oai_messages=[{"role": "user", "content": "hi"}],
    )

    assert resp.content == "Hi there"
    assert resp.usage == {"prompt_tokens": 40, "completion_tokens": 8}
    # stream_options must be requested so OpenAI emits the usage chunk.
    sent = client.chat.completions.create.call_args.kwargs
    assert sent.get("stream_options") == {"include_usage": True}


@pytest.mark.asyncio
async def test_consume_openai_stream_can_disable_usage():
    chunks = [_openai_text_chunk("ok")]
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=_AsyncEventIterator(chunks))

    resp = await consume_openai_stream_with_tools(
        client,
        model="gpt-4o",
        oai_messages=[{"role": "user", "content": "hi"}],
        include_usage=False,
    )

    assert resp.usage == {}
    sent = client.chat.completions.create.call_args.kwargs
    assert "stream_options" not in sent


# ---------------------------------------------------------------------------
# Agentic loop wiring: _streaming_chat must surface last_usage
# ---------------------------------------------------------------------------


class _FakeStreamingLLM(BaseLLMProvider):
    provider_name = "fake"
    default_model = "fake-model"

    def __init__(self, usage: dict | None):
        self._usage = usage

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        return ChatResponse(content="", model=model or "fake-model")

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        self.last_stop_reason = None
        self.last_usage = None
        for tok in ("Hello", " ", "world"):
            yield tok
        self.last_stop_reason = "end_turn"
        self.last_usage = self._usage

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return ["fake-model"]


@pytest.mark.asyncio
async def test_streaming_chat_propagates_last_usage():
    llm = _FakeStreamingLLM(usage={"input_tokens": 11, "output_tokens": 7})
    resp = await _streaming_chat(
        llm,
        [ChatMessage(role="user", content="hi")],
        on_token=None,
        model="fake-model",
        max_tokens=1024,
    )
    assert resp.content == "Hello world"
    assert resp.usage == {"input_tokens": 11, "output_tokens": 7}


@pytest.mark.asyncio
async def test_streaming_chat_usage_defaults_to_empty():
    llm = _FakeStreamingLLM(usage=None)
    resp = await _streaming_chat(
        llm,
        [ChatMessage(role="user", content="hi")],
        on_token=None,
        model="fake-model",
        max_tokens=1024,
    )
    assert resp.usage == {}


# ---------------------------------------------------------------------------
# Failover wrapper must surface the underlying provider's streaming usage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failover_chat_stream_propagates_last_usage():
    from app.llm.router import FailoverLLMProvider

    primary = _FakeStreamingLLM(usage={"input_tokens": 5, "output_tokens": 3})
    failover = FailoverLLMProvider(primary=primary, fallbacks=[])

    collected = [
        chunk
        async for chunk in failover.chat_stream(
            [ChatMessage(role="user", content="hi")], model="fake-model"
        )
    ]

    assert "".join(collected) == "Hello world"
    assert failover.last_usage == {"input_tokens": 5, "output_tokens": 3}
    assert failover.last_stop_reason == "end_turn"


# ---------------------------------------------------------------------------
# Bedrock and Gemini streaming paths, end to end
#
# Fake SDK responses only. The Bedrock client stub returns the ConverseStream
# event sequence Bedrock documents, and the Gemini client streams real
# ``google.genai.types`` responses whose ``usage_metadata`` is cumulative, as
# the API sends it. Each usage is also priced through the run budget.
# ---------------------------------------------------------------------------


def _model_price(
    input_rate: str,
    output_rate: str,
    *,
    cache_read: str | None = None,
    cache_write: str | None = None,
) -> ModelPrice:
    return ModelPrice(
        provider="stub",
        model_id="stub",
        model_pattern=None,
        input_price_per_1m=Decimal(input_rate),
        output_price_per_1m=Decimal(output_rate),
        cache_read_price_per_1m=None if cache_read is None else Decimal(cache_read),
        cache_write_price_per_1m=None if cache_write is None else Decimal(cache_write),
        effective_from=date(2026, 1, 1),
        is_custom=False,
    )


def _budget_spend(usage: dict, price: ModelPrice) -> tuple[Decimal, int]:
    """What one call with this usage debits from a run budget: (cost, tokens)."""
    budget = RunBudget(max_cost_usd=100.0, max_total_tokens=10_000_000, price=price)
    budget.record_llm_usage(usage)
    return budget.spent_cost_usd, budget.spent_tokens


_HI = [ChatMessage(role="user", content="hi")]

# -- Bedrock -----------------------------------------------------------------

_BEDROCK_MODEL = "anthropic.claude-sonnet-5"
# A call whose prompt prefix hit the Bedrock prompt cache: ``inputTokens`` is
# the non-cached input only, and the cache reads and writes come in their own
# counters. AWS does not say whether ``totalTokens`` also counts them, so the
# fixture takes ``inputTokens + outputTokens`` and the usage never carries it.
_BEDROCK_USAGE = {
    "inputTokens": 1_200,
    "outputTokens": 300,
    "totalTokens": 1_500,
    "cacheReadInputTokens": 24_000,
    "cacheWriteInputTokens": 6_000,
}
_BEDROCK_EXPECTED_USAGE = {
    "prompt_tokens": 1_200,
    "completion_tokens": 300,
    "cache_read_input_tokens": 24_000,
    "cache_creation_input_tokens": 6_000,
}
# $3 in, $15 out, $0.30 cache read, $3.75 cache write, per million tokens:
# 1,200 x $3 + 300 x $15 + 24,000 x $0.30 + 6,000 x $3.75.
_BEDROCK_PRICE = _model_price("3", "15", cache_read="0.30", cache_write="3.75")
_BEDROCK_COST = Decimal("0.0036") + Decimal("0.0045") + Decimal("0.0072") + Decimal("0.0225")


def _converse_stream_events(usage: dict | None, *, tool_call: bool = False) -> list[dict]:
    """A ConverseStream event sequence, in the order Bedrock sends it."""
    events: list[dict] = [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": "Hello"}, "contentBlockIndex": 0}},
        {"contentBlockDelta": {"delta": {"text": " world"}, "contentBlockIndex": 0}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
    ]
    if tool_call:
        events += [
            {
                "contentBlockStart": {
                    "start": {"toolUse": {"toolUseId": "tooluse_1", "name": "get_weather"}},
                    "contentBlockIndex": 1,
                }
            },
            {"contentBlockDelta": {"delta": {"toolUse": {"input": '{"city": '}}, "contentBlockIndex": 1}},
            {"contentBlockDelta": {"delta": {"toolUse": {"input": '"Paris"}'}}, "contentBlockIndex": 1}},
            {"contentBlockStop": {"contentBlockIndex": 1}},
        ]
    events.append({"messageStop": {"stopReason": "tool_use" if tool_call else "end_turn"}})
    if usage is not None:
        # The usage comes once, in the metadata event that closes the stream.
        events.append({"metadata": {"usage": usage, "metrics": {"latencyMs": 812}}})
    return events


def _bedrock(stream_events: list[dict] | None = None, *, converse_usage: dict | None = None) -> BedrockProvider:
    provider = BedrockProvider(api_key="test-access-key||test-secret||us-east-1")
    converse_response = {
        "output": {"message": {"role": "assistant", "content": [{"text": "Hello world"}]}},
        "stopReason": "end_turn",
        "usage": converse_usage or {},
        "metrics": {"latencyMs": 812},
    }
    provider._client = SimpleNamespace(
        converse=lambda **_kwargs: converse_response,
        converse_stream=lambda **_kwargs: {"stream": list(stream_events or [])},
    )
    return provider


@pytest.mark.asyncio
async def test_bedrock_chat_stream_with_tools_reads_the_closing_metadata_event():
    provider = _bedrock(_converse_stream_events(_BEDROCK_USAGE, tool_call=True))
    weather = ToolDefinition(
        name="get_weather",
        description="Current weather for a city.",
        input_schema={"type": "object", "properties": {"city": {"type": "string"}}},
    )

    resp = await provider.chat_stream_with_tools(
        [ChatMessage(role="user", content="Weather in Paris?")], tools=[weather], model=_BEDROCK_MODEL
    )

    assert resp.content == "Hello world"
    assert resp.stop_reason == "tool_use"
    assert [(tc.name, tc.arguments) for tc in resp.tool_calls or []] == [("get_weather", {"city": "Paris"})]
    assert resp.usage == _BEDROCK_EXPECTED_USAGE


@pytest.mark.asyncio
async def test_bedrock_streaming_and_converse_report_and_price_the_same_usage():
    """One Converse usage maps to one dict on all three paths. The budget prices
    the cache tokens at the cache rates but counts only the non-cached input and
    the output as tokens, as it does for a native Anthropic call. Both readings
    of ``totalTokens`` (with or without the cache counters) give the same."""
    for total in (1_500, 31_500):
        reported = {**_BEDROCK_USAGE, "totalTokens": total}
        converse = await _bedrock(converse_usage=reported).chat(_HI, model=_BEDROCK_MODEL)
        with_tools = await _bedrock(_converse_stream_events(reported)).chat_stream_with_tools(
            _HI, model=_BEDROCK_MODEL
        )
        streamed = await _streaming_chat(
            _bedrock(_converse_stream_events(reported)), _HI, on_token=None, model=_BEDROCK_MODEL, max_tokens=1024
        )

        assert streamed.content == "Hello world"
        assert converse.usage == with_tools.usage == streamed.usage == _BEDROCK_EXPECTED_USAGE
        for usage in (converse.usage, with_tools.usage, streamed.usage):
            assert _budget_spend(usage, _BEDROCK_PRICE) == (_BEDROCK_COST, 1_500)


@pytest.mark.asyncio
async def test_bedrock_stream_assigns_the_reported_usage_never_adds_it():
    """Bedrock reports a stream's usage once, in its closing ``metadata`` event.
    Both streaming paths assign it rather than add it: were a stream to carry a
    second event, the later one would replace the earlier."""
    early = {"inputTokens": 1_000, "outputTokens": 1, "totalTokens": 1_001}
    events = [*_converse_stream_events(early), {"metadata": {"usage": _BEDROCK_USAGE, "metrics": {"latencyMs": 900}}}]

    with_tools = await _bedrock(events).chat_stream_with_tools(_HI, model=_BEDROCK_MODEL)
    streamed = await _streaming_chat(_bedrock(events), _HI, on_token=None, model=_BEDROCK_MODEL, max_tokens=1024)

    assert with_tools.usage == streamed.usage == _BEDROCK_EXPECTED_USAGE


@pytest.mark.asyncio
async def test_bedrock_stream_without_usage_reports_no_usage():
    zero = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}
    for events in (_converse_stream_events(None), _converse_stream_events(zero)):
        with_tools = await _bedrock(events).chat_stream_with_tools(_HI, model=_BEDROCK_MODEL)
        streamed = await _streaming_chat(_bedrock(events), _HI, on_token=None, model=_BEDROCK_MODEL, max_tokens=1024)

        assert with_tools.usage == {}
        assert streamed.usage == {}


# -- Gemini ------------------------------------------------------------------

_GEMINI_MODEL = "gemini-2.5-flash"
# The last chunk's totals: 12,000 prompt tokens, 96 answer + 2,000 thinking
# output tokens. Summing the three cumulative chunks would give 36,000 / 5,636.
_GEMINI_EXPECTED_USAGE = {"prompt_tokens": 12_000, "completion_tokens": 2_096}


def _gemini_usage_metadata(
    prompt: int,
    *,
    candidates: int | None = None,
    thoughts: int | None = None,
    cached: int | None = None,
    tool_use: int | None = None,
) -> genai_types.GenerateContentResponseUsageMetadata:
    """The counts as Google reports them: ``cached`` is part of ``prompt``, and
    the total is the sum of the prompt, candidates, tool-use prompt and thoughts."""
    return genai_types.GenerateContentResponseUsageMetadata(
        prompt_token_count=prompt,
        cached_content_token_count=cached,
        candidates_token_count=candidates,
        tool_use_prompt_token_count=tool_use,
        thoughts_token_count=thoughts,
        total_token_count=prompt + (candidates or 0) + (tool_use or 0) + (thoughts or 0),
    )


def _gemini_response(
    parts: list[genai_types.Part],
    usage: genai_types.GenerateContentResponseUsageMetadata | None,
    *,
    finished: bool = False,
) -> genai_types.GenerateContentResponse:
    return genai_types.GenerateContentResponse(
        candidates=[
            genai_types.Candidate(
                content=genai_types.Content(role="model", parts=parts),
                finish_reason=genai_types.FinishReason.STOP if finished else None,
            )
        ],
        usage_metadata=usage,
    )


def _gemini_stream(*, with_usage: bool = True) -> list[genai_types.GenerateContentResponse]:
    """A thinking model's stream. Every chunk repeats the running totals
    (``usage_metadata`` is cumulative), so only the last one is the call's."""
    usage = (
        [
            _gemini_usage_metadata(12_000, thoughts=1_500),
            _gemini_usage_metadata(12_000, candidates=40, thoughts=2_000),
            _gemini_usage_metadata(12_000, candidates=96, thoughts=2_000),
        ]
        if with_usage
        else [None, None, None]
    )
    return [
        _gemini_response([genai_types.Part(text="Weighing the options.", thought=True)], usage[0]),
        _gemini_response([genai_types.Part(text="The answer")], usage[1]),
        _gemini_response([genai_types.Part(text=" is 42.")], usage[2], finished=True),
    ]


def _gemini(
    chunks: list[genai_types.GenerateContentResponse] | None = None,
    *,
    response: genai_types.GenerateContentResponse | None = None,
) -> GeminiProvider:
    provider = GeminiProvider(api_key="test-key")

    class _Models:
        async def generate_content_stream(self, **_kwargs):
            return _AsyncEventIterator(list(chunks or []))

        async def generate_content(self, **_kwargs):
            return response

    provider.client = SimpleNamespace(aio=SimpleNamespace(models=_Models()))
    return provider


@pytest.mark.asyncio
async def test_gemini_stream_keeps_the_last_cumulative_usage_never_the_sum():
    with_tools = await _gemini(_gemini_stream()).chat_stream_with_tools(_HI, model=_GEMINI_MODEL)
    streamed = await _streaming_chat(
        _gemini(_gemini_stream()), _HI, on_token=None, model=_GEMINI_MODEL, max_tokens=1024
    )

    assert with_tools.content == streamed.content == "The answer is 42."
    assert with_tools.thinking == "Weighing the options."
    assert with_tools.usage == streamed.usage == _GEMINI_EXPECTED_USAGE


@pytest.mark.asyncio
async def test_gemini_streaming_and_generate_content_report_and_price_the_same_usage():
    """The streamed usage equals the one ``generate_content`` returns for the same
    call, and the budget debits the thinking tokens at the output rate."""
    unary_response = _gemini_response(
        [
            genai_types.Part(text="Weighing the options.", thought=True),
            genai_types.Part(text="The answer is 42."),
        ],
        _gemini_usage_metadata(12_000, candidates=96, thoughts=2_000),
        finished=True,
    )
    unary = await _gemini(response=unary_response).chat(_HI, model=_GEMINI_MODEL)
    with_tools = await _gemini(_gemini_stream()).chat_stream_with_tools(_HI, model=_GEMINI_MODEL)
    streamed = await _streaming_chat(
        _gemini(_gemini_stream()), _HI, on_token=None, model=_GEMINI_MODEL, max_tokens=1024
    )

    assert unary.usage == with_tools.usage == streamed.usage == _GEMINI_EXPECTED_USAGE
    # $0.30 in, $2.50 out, per million tokens: 12,000 x $0.30 + (96 + 2,000) x $2.50.
    price = _model_price("0.30", "2.50")
    for usage in (unary.usage, with_tools.usage, streamed.usage):
        assert _budget_spend(usage, price) == (Decimal("0.0036") + Decimal("0.00524"), 14_096)


@pytest.mark.asyncio
async def test_gemini_stream_without_usage_metadata_reports_no_usage():
    with_tools = await _gemini(_gemini_stream(with_usage=False)).chat_stream_with_tools(_HI, model=_GEMINI_MODEL)
    streamed = await _streaming_chat(
        _gemini(_gemini_stream(with_usage=False)), _HI, on_token=None, model=_GEMINI_MODEL, max_tokens=1024
    )

    assert with_tools.usage == {}
    assert streamed.usage == {}


@pytest.mark.asyncio
async def test_gemini_prompt_tokens_keep_the_cached_tokens_inside():
    """``prompt_token_count`` includes ``cached_content_token_count`` and the
    usage keeps it whole: proxy clients read ``prompt_tokens`` as the full
    prompt. The cached part also rides as ``prompt_tokens_details.cached_tokens``,
    which the budget prices at the cache-read rate."""
    cached = _gemini_usage_metadata(12_000, candidates=96, cached=9_000)
    chunks = [_gemini_response([genai_types.Part(text="Hi")], cached, finished=True)]

    unary = await _gemini(response=chunks[0]).chat(_HI, model=_GEMINI_MODEL)
    with_tools = await _gemini(chunks).chat_stream_with_tools(_HI, model=_GEMINI_MODEL)
    streamed = await _streaming_chat(_gemini(chunks), _HI, on_token=None, model=_GEMINI_MODEL, max_tokens=1024)

    expected = {"prompt_tokens": 12_000, "completion_tokens": 96, "prompt_tokens_details": {"cached_tokens": 9_000}}
    assert unary.usage == with_tools.usage == streamed.usage == expected
    # $0.30 in, $2.50 out, $0.03 cache read, per million tokens:
    # 3,000 uncached x $0.30 + 9,000 cached x $0.03 + 96 x $2.50.
    price = _model_price("0.30", "2.50", cache_read="0.03")
    for usage in (unary.usage, with_tools.usage, streamed.usage):
        assert _budget_spend(usage, price) == (Decimal("0.0009") + Decimal("0.00027") + Decimal("0.00024"), 12_096)


@pytest.mark.asyncio
async def test_gemini_tool_use_prompt_tokens_count_once_as_input():
    """``tool_use_prompt_token_count`` counts the results of a tool Google runs
    itself, billed as input, which ``prompt_token_count`` leaves out. With every
    count at once, all three paths report the same usage, it adds up to Google's
    total, and the budget and the trace price each token once: the cached part at
    the cache-read rate, the tool-use tokens at the input rate, the thinking at
    the output rate."""
    usage = [
        _gemini_usage_metadata(12_000, cached=9_000, tool_use=4_000, thoughts=1_500),
        _gemini_usage_metadata(12_000, cached=9_000, tool_use=4_000, candidates=40, thoughts=2_000),
        _gemini_usage_metadata(12_000, cached=9_000, tool_use=4_000, candidates=96, thoughts=2_000),
    ]
    chunks = [
        _gemini_response([genai_types.Part(text="Weighing the options.", thought=True)], usage[0]),
        _gemini_response([genai_types.Part(text="The answer")], usage[1]),
        _gemini_response([genai_types.Part(text=" is 42.")], usage[2], finished=True),
    ]
    unary_response = _gemini_response(
        [
            genai_types.Part(text="Weighing the options.", thought=True),
            genai_types.Part(text="The answer is 42."),
        ],
        usage[2],
        finished=True,
    )

    unary = await _gemini(response=unary_response).chat(_HI, model=_GEMINI_MODEL)
    with_tools = await _gemini(chunks).chat_stream_with_tools(_HI, model=_GEMINI_MODEL)
    streamed = await _streaming_chat(_gemini(chunks), _HI, on_token=None, model=_GEMINI_MODEL, max_tokens=1024)

    expected = {"prompt_tokens": 16_000, "completion_tokens": 2_096, "prompt_tokens_details": {"cached_tokens": 9_000}}
    assert unary.usage == with_tools.usage == streamed.usage == expected
    assert expected["prompt_tokens"] + expected["completion_tokens"] == usage[2].total_token_count
    # $0.30 in, $2.50 out, $0.03 cache read, per million tokens: (3,000 uncached
    # prompt + 4,000 tool-use) x $0.30 + 9,000 cached x $0.03 + (96 + 2,000) x $2.50.
    price = _model_price("0.30", "2.50", cache_read="0.03")
    billed = Decimal("0.0021") + Decimal("0.00027") + Decimal("0.00524")
    for reported in (unary.usage, with_tools.usage, streamed.usage):
        assert _budget_spend(reported, price) == (billed, 18_096)
        assert calculate_costs(_normalize_usage(reported), price)["total_cost"] == billed
