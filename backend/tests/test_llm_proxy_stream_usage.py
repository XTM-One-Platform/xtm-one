"""The LLM proxy's streams record what the provider reported.

The proxy streams through generators of its own, not through the provider
streaming paths the agent loop uses:

* ``_anthropic_stream``, the Anthropic bridge behind ``/v1/chat/completions``;
* ``_stream``, the OpenAI-compatible pass-through;
* ``_generic_stream``, the Gemini / Bedrock bridge over ``provider.chat_stream``;
* ``_pipe_stream``, the ``/v1/messages`` byte pass-through.

A client that disconnects mid-stream makes Starlette stop the generator:
``GeneratorExit`` at the ``yield`` it is suspended on, or ``CancelledError`` at
the ``await`` it is running. Neither is an ``Exception``, so the trace was never
closed (no durable ``llm_traces`` row), the usage already received was lost and
the request was never metered. The Gemini / Bedrock bridge never passed the
provider's streamed usage to the trace, even on a normal end. ``_pipe_stream``
added up the ``message_delta`` output counts, which Anthropic documents as
cumulative.

Every stream here is built from the SDKs' own event and usage types (Bedrock's
ConverseStream events as the dicts boto3 parses them into), with no network and
no key. The trace is read where it lands: the ``TraceData`` the real
``end_trace`` hands to the durable sink. What the proxy sends the client is
pinned byte for byte on the normal ends: only the accounting moves.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from anthropic.types import (
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
    Usage,
)
from anthropic.types.raw_message_delta_event import Delta
from google.genai import types as genai_types
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from openai.types.chat.chat_completion_chunk import Choice, ChoiceDelta
from openai.types.completion_usage import CompletionUsage

from app.api.v1 import llm_proxy
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.bedrock_provider import BedrockProvider, _usage_from_bedrock
from app.llm.gemini_provider import GeminiProvider, _usage_from_gemini_metadata
from app.llm.openai_provider import OpenAIProvider
from app.services import langfuse_tracing as lft

ANTHROPIC_MODEL = "claude-sonnet-4-6"
GEMINI_MODEL = "gemini-2.5-flash"
BEDROCK_MODEL = "anthropic.claude-sonnet-4-5"
OPENAI_MODEL = "gpt-4o"

_USER_ID = str(uuid.uuid4())
_NOW = 1_700_000_000


# ---------------------------------------------------------------------------
# Scripted upstream streams
# ---------------------------------------------------------------------------


class _Hang:
    """The upstream stalls here for good: a cancellation lands on this await."""


HANG = _Hang()


class _Do:
    """Run ``fn`` when the upstream reaches this point, then carry on."""

    def __init__(self, fn) -> None:
        self.fn = fn


class _AsyncScript:
    """Async iterator over a script (Anthropic, Gemini and OpenAI streams)."""

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
        if self._items:
            return self._items.pop(0)
        raise StopIteration


class _AnthropicStream(_AsyncScript):
    """What ``client.messages.stream(...)`` enters into: the events, then the
    final message the SDK assembles from them."""

    def __init__(self, items: list, final_usage: Usage) -> None:
        super().__init__(items)
        self._final_usage = final_usage

    async def get_final_message(self) -> Message:
        return _message(self._final_usage)


class _AnthropicStreamCtx:
    def __init__(self, items: list, final_usage: Usage) -> None:
        self._stream = _AnthropicStream(items, final_usage)

    async def __aenter__(self):
        return self._stream

    async def __aexit__(self, *_exc):
        return False


def _anthropic(items: list, final_usage: Usage) -> AnthropicProvider:
    provider = AnthropicProvider(api_key="test-key")
    provider.client = SimpleNamespace(
        messages=SimpleNamespace(stream=lambda **_kw: _AnthropicStreamCtx(items, final_usage))
    )
    return provider


def _gemini(items: list) -> GeminiProvider:
    provider = GeminiProvider(api_key="dummy")

    class _Models:
        async def generate_content_stream(self, **_kwargs):
            return _AsyncScript(items)

    provider.client = SimpleNamespace(aio=SimpleNamespace(models=_Models()))
    return provider


def _bedrock(items: list) -> BedrockProvider:
    provider = BedrockProvider("us-east-1")

    class _Client:
        def converse_stream(self, **_kwargs):
            return {"stream": _SyncScript(items)}

    provider._client = _Client()
    return provider


def _openai(items: list) -> OpenAIProvider:
    provider = OpenAIProvider(api_key="sk-test")
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=_AsyncScript(items))))
    )
    return provider


# ── Anthropic (the SDK's own raw stream event types) ─────────────────────

# What ``message_start`` reports for the prompt: the uncached part, the cache
# write and the cache read, plus the initial output token it always carries.
A_START_USAGE = {
    "input_tokens": 1_000,
    "output_tokens": 1,
    "cache_creation_input_tokens": 200,
    "cache_read_input_tokens": 3_000,
}


def _message(usage: Usage) -> Message:
    return Message(
        id="msg_test",
        type="message",
        role="assistant",
        model=ANTHROPIC_MODEL,
        content=[],
        stop_reason=None,
        stop_sequence=None,
        usage=usage,
    )


def _a_start() -> RawMessageStartEvent:
    return RawMessageStartEvent(
        type="message_start",
        message=_message(
            Usage(
                input_tokens=1_000,
                output_tokens=1,
                cache_creation_input_tokens=200,
                cache_read_input_tokens=3_000,
            )
        ),
    )


def _a_block_start() -> RawContentBlockStartEvent:
    return RawContentBlockStartEvent(type="content_block_start", index=0, content_block=TextBlock(type="text", text=""))


def _a_text(text: str) -> RawContentBlockDeltaEvent:
    return RawContentBlockDeltaEvent(type="content_block_delta", index=0, delta=TextDelta(type="text_delta", text=text))


def _a_block_stop() -> RawContentBlockStopEvent:
    return RawContentBlockStopEvent(type="content_block_stop", index=0)


def _a_delta(output_tokens: int) -> RawMessageDeltaEvent:
    return RawMessageDeltaEvent(
        type="message_delta",
        delta=Delta(stop_reason="end_turn", stop_sequence=None),
        usage=MessageDeltaUsage(output_tokens=output_tokens),
    )


def _a_stop() -> RawMessageStopEvent:
    return RawMessageStopEvent(type="message_stop")


def _a_final_usage(output_tokens: int) -> Usage:
    return Usage(
        input_tokens=1_000,
        output_tokens=output_tokens,
        cache_creation_input_tokens=200,
        cache_read_input_tokens=3_000,
    )


# ── Gemini (``GenerateContentResponse`` with cumulative usage) ───────────


def _g_chunk(text: str, *, prompt: int, candidates: int) -> genai_types.GenerateContentResponse:
    return genai_types.GenerateContentResponse(
        candidates=[
            genai_types.Candidate(content=genai_types.Content(role="model", parts=[genai_types.Part(text=text)]))
        ],
        usage_metadata=genai_types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt,
            candidates_token_count=candidates,
            total_token_count=prompt + candidates,
        ),
    )


# ── Bedrock ConverseStream (the dicts boto3 parses the events into) ──────


def _b_text(text: str) -> dict:
    return {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": text}}}


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


B_OPENING = [{"messageStart": {"role": "assistant"}}]
B_CLOSING = [{"contentBlockStop": {"contentBlockIndex": 0}}, {"messageStop": {"stopReason": "end_turn"}}]


# ── OpenAI (``ChatCompletionChunk``, usage on the final one) ─────────────


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
        usage=CompletionUsage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion),
    )


# ---------------------------------------------------------------------------
# Harness: the durable trace, the metering and the client
# ---------------------------------------------------------------------------


def _root_trace_ctx() -> dict:
    return {
        "trace": None,
        "generation": None,
        "client": None,
        "parent_ctx": None,
        "metadata": {"source": "llm-proxy"},
        "input_data": {"messages": []},
        "local_spans": [],
        "local_span_counter": 0,
        "local_trace_id": uuid.uuid4(),
        "local_source": "llm-proxy",
        "local_name": "llm-proxy",
        "local_started_at": datetime.now(UTC),
        "local_metadata": {"source": "llm-proxy"},
        "local_tags": [],
    }


@pytest.fixture
def traces():
    """Every trace the real ``end_trace`` hands to the durable sink."""
    submitted: list = []
    with patch(
        "app.services.tracing.dispatcher.submit_trace_for_flush",
        side_effect=lambda trace, _spans: submitted.append(trace),
    ):
        yield submitted


@pytest.fixture
def metered(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(llm_proxy, "_track_model_usage", mock)
    return mock


@pytest.fixture
def frozen_time(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: float(_NOW))


async def _settle_metering() -> None:
    """Let the metering tasks the streams scheduled finish."""
    pending = list(getattr(llm_proxy, "_STREAM_METERING", ()))
    if pending:
        await asyncio.gather(*pending)


def _chat_body(model: str) -> llm_proxy.ChatCompletionRequest:
    return llm_proxy.ChatCompletionRequest(model=model, messages=[{"role": "user", "content": "hi"}], stream=True)


async def _take(gen, n: int) -> list:
    return [await gen.__anext__() for _ in range(n)]


async def _consume(gen) -> list:
    return [chunk async for chunk in gen]


def _content_chunk(model: str, text: str) -> str:
    chunk = {
        "id": "chatcmpl-stream",
        "object": "chat.completion.chunk",
        "created": _NOW,
        "model": model,
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }
    return f"data: {json.dumps(chunk)}\n\n"


def _final_chunk(model: str) -> str:
    chunk = {
        "id": "chatcmpl-stream",
        "object": "chat.completion.chunk",
        "created": _NOW,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    return f"data: {json.dumps(chunk)}\n\n"


def _assert_closed_ok(trace, usage: dict) -> None:
    assert trace.status == "ok"
    assert trace.error_message is None
    assert trace.usage_details == lft._normalize_usage(usage)


def _assert_closed_abandoned(trace, usage: dict | None, output: str | None) -> None:
    """Closed once with the cancellation, carrying exactly what was reported."""
    assert trace.status == "error"
    assert trace.error_message == llm_proxy._CLIENT_DISCONNECTED
    assert trace.failure_cause == "run_cancelled"
    assert trace.output == output
    if usage is None:
        assert trace.usage_details is None
        assert trace.input_tokens is None
        assert trace.output_tokens is None
    else:
        normalized = lft._normalize_usage(usage)
        assert trace.usage_details == normalized
        assert trace.input_tokens == normalized["input"]
        assert trace.output_tokens == normalized["output"]


def _assert_closed_failed(trace, usage: dict) -> None:
    """Closed once with the error, carrying the usage reported before it."""
    assert trace.status == "error"
    assert trace.error_message == "reset by peer"
    assert trace.usage_details == lft._normalize_usage(usage)


# ---------------------------------------------------------------------------
# Anthropic bridge (``_anthropic_stream``)
# ---------------------------------------------------------------------------


async def _anthropic_response(items: list, final_usage: Usage, root: dict):
    return await llm_proxy._proxy_via_anthropic(
        _anthropic(items, final_usage),
        _chat_body(ANTHROPIC_MODEL),
        [{"role": "user", "content": "hi"}],
        ANTHROPIC_MODEL,
        1_024,
        root,
        user_id=_USER_ID,
    )


async def test_anthropic_bridge_normal_end_is_unchanged(traces, metered, frozen_time):
    items = [_a_start(), _a_block_start(), _a_text("Hel"), _a_text("lo"), _a_block_stop(), _a_delta(25), _a_stop()]
    response = await _anthropic_response(items, _a_final_usage(25), _root_trace_ctx())

    body = await _consume(response.body_iterator)

    assert body == [
        _content_chunk(ANTHROPIC_MODEL, "Hel"),
        _content_chunk(ANTHROPIC_MODEL, "lo"),
        _final_chunk(ANTHROPIC_MODEL),
        "data: [DONE]\n\n",
    ]
    assert len(traces) == 1
    _assert_closed_ok(traces[0], {**A_START_USAGE, "output_tokens": 25})
    metered.assert_awaited_once_with(ANTHROPIC_MODEL, user_id=_USER_ID)


async def test_anthropic_bridge_client_leaving_mid_stream_closes_the_trace(traces, metered):
    """``aclose()`` at a ``yield``: the trace carries what ``message_start``
    reported (input, cache write and read, the initial output token), never an
    output count estimated from the text."""
    items = [_a_start(), _a_block_start(), _a_text("Hel"), _a_text("lo"), _a_block_stop(), _a_delta(25), _a_stop()]
    response = await _anthropic_response(items, _a_final_usage(25), _root_trace_ctx())
    gen = response.body_iterator

    await _take(gen, 1)
    await gen.aclose()
    await _settle_metering()

    assert len(traces) == 1
    _assert_closed_abandoned(traces[0], A_START_USAGE, output="Hel")
    metered.assert_awaited_once_with(ANTHROPIC_MODEL, user_id=_USER_ID)


async def test_anthropic_bridge_cancelled_while_waiting_keeps_the_cumulative_output(traces, metered):
    """``CancelledError`` at an ``await``: the latest ``message_delta`` count."""
    reached = asyncio.Event()
    items = [_a_start(), _a_block_start(), _a_text("Hel"), _a_delta(57), _Do(reached.set), HANG]
    response = await _anthropic_response(items, _a_final_usage(57), _root_trace_ctx())

    task = asyncio.create_task(_consume(response.body_iterator))
    await asyncio.wait_for(reached.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _settle_metering()

    assert len(traces) == 1
    _assert_closed_abandoned(traces[0], {**A_START_USAGE, "output_tokens": 57}, output="Hel")
    metered.assert_awaited_once_with(ANTHROPIC_MODEL, user_id=_USER_ID)


async def test_anthropic_bridge_failing_mid_stream_keeps_the_reported_usage(traces, metered):
    """A stream that fails after the provider reported usage closes its trace
    with that usage (none). The error path meters nothing, as
    before."""
    items = [_a_start(), _a_block_start(), _a_text("Hel"), _a_delta(57), ConnectionError("reset by peer")]
    response = await _anthropic_response(items, _a_final_usage(57), _root_trace_ctx())

    body = await _consume(response.body_iterator)

    assert body[-1] == "data: [DONE]\n\n"
    assert len(traces) == 1
    _assert_closed_failed(traces[0], {**A_START_USAGE, "output_tokens": 57})
    metered.assert_not_awaited()


# ---------------------------------------------------------------------------
# OpenAI-compatible pass-through (``_stream``)
# ---------------------------------------------------------------------------


O_CHUNKS = [_o_chunk("Hel"), _o_chunk("lo"), _o_chunk(None, "stop"), _o_usage(100, 20)]
O_USAGE = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}


async def _openai_response(items: list, root: dict):
    user = SimpleNamespace(id=uuid.UUID(_USER_ID), email="user@example.com")
    provider = _openai(items)
    with (
        patch.object(llm_proxy, "_authenticate_api_key", AsyncMock(return_value=user)),
        patch.object(llm_proxy, "_refuse_unless_chat_enabled", AsyncMock()),
        patch("app.services.quota.check_llm_proxy_quota", AsyncMock()),
        patch.object(llm_proxy, "get_llm_for_user", AsyncMock(return_value=(provider, OPENAI_MODEL))),
        patch.object(llm_proxy, "trace_llm_call", AsyncMock(return_value=root)),
        patch.object(llm_proxy, "release_db_connection_before_loop", AsyncMock()),
    ):
        return await llm_proxy.proxy_chat_completions(_chat_body("auto"), SimpleNamespace(), SimpleNamespace())


async def test_openai_passthrough_normal_end_is_unchanged(traces, metered):
    response = await _openai_response(O_CHUNKS, _root_trace_ctx())

    body = await _consume(response.body_iterator)

    assert body == [f"data: {c.model_dump_json()}\n\n" for c in O_CHUNKS] + ["data: [DONE]\n\n"]
    assert len(traces) == 1
    _assert_closed_ok(traces[0], O_USAGE)
    metered.assert_awaited_once_with(OPENAI_MODEL, user_id=_USER_ID)


async def test_openai_passthrough_client_leaving_before_the_usage_chunk_records_nothing(traces, metered):
    """OpenAI reports usage only in its final chunk: nothing to record yet."""
    response = await _openai_response(O_CHUNKS, _root_trace_ctx())
    gen = response.body_iterator

    await _take(gen, 1)
    await gen.aclose()
    await _settle_metering()

    assert len(traces) == 1
    _assert_closed_abandoned(traces[0], None, output="Hel")
    metered.assert_awaited_once_with(OPENAI_MODEL, user_id=_USER_ID)


async def test_openai_passthrough_client_leaving_at_the_usage_chunk_keeps_its_usage(traces, metered):
    """The client leaves while the usage chunk is being handed to it: the
    provider reported it, so the trace carries it."""
    response = await _openai_response(O_CHUNKS, _root_trace_ctx())
    gen = response.body_iterator

    await _take(gen, 4)
    await gen.aclose()
    await _settle_metering()

    assert len(traces) == 1
    _assert_closed_abandoned(traces[0], O_USAGE, output="Hello")
    metered.assert_awaited_once_with(OPENAI_MODEL, user_id=_USER_ID)


async def test_openai_passthrough_failing_after_the_usage_chunk_keeps_its_usage(traces, metered):
    response = await _openai_response([*O_CHUNKS, ConnectionError("reset by peer")], _root_trace_ctx())

    body = await _consume(response.body_iterator)

    assert body[-1] == "data: [DONE]\n\n"
    assert len(traces) == 1
    _assert_closed_failed(traces[0], O_USAGE)
    metered.assert_not_awaited()


async def test_a_cancel_after_the_normal_end_neither_stops_nor_repeats_the_metering(traces, metered):
    """The trace is closed and the metering started when the client leaves:
    the cancellation stops the stream's wait, never the metering, which
    finishes in its own task, once; the trace is not closed again."""
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def _slow_metering(*_args, **_kwargs) -> None:
        started.set()
        await release.wait()
        finished.set()

    metered.side_effect = _slow_metering
    response = await _openai_response(O_CHUNKS, _root_trace_ctx())

    task = asyncio.create_task(_consume(response.body_iterator))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await _settle_metering()

    # Awaited inside the stream, the metering itself took the cancellation.
    assert finished.is_set()
    assert metered.await_count == 1
    assert len(traces) == 1
    _assert_closed_ok(traces[0], O_USAGE)


# ---------------------------------------------------------------------------
# Gemini / Bedrock bridge (``_generic_stream``)
# ---------------------------------------------------------------------------


async def _bridge_response(provider, model: str, root: dict):
    return await llm_proxy._proxy_via_provider_chat(
        provider,
        _chat_body(model),
        [{"role": "user", "content": "hi"}],
        model,
        1_024,
        root,
        user_id=_USER_ID,
    )


G_CHUNKS = [_g_chunk("Hel", prompt=400, candidates=3), _g_chunk("lo", prompt=400, candidates=7)]


async def test_gemini_bridge_normal_end_records_the_streamed_usage(traces, metered, frozen_time):
    response = await _bridge_response(_gemini(G_CHUNKS), GEMINI_MODEL, _root_trace_ctx())

    body = await _consume(response.body_iterator)

    # The client receives exactly what it received before.
    assert body == [
        _content_chunk(GEMINI_MODEL, "Hel"),
        _content_chunk(GEMINI_MODEL, "lo"),
        _final_chunk(GEMINI_MODEL),
        "data: [DONE]\n\n",
    ]
    assert len(traces) == 1
    # The chunks' counts are
    # cumulative: the last one's, never a sum.
    _assert_closed_ok(traces[0], _usage_from_gemini_metadata(G_CHUNKS[-1].usage_metadata))
    metered.assert_awaited_once_with(GEMINI_MODEL, user_id=_USER_ID)


async def test_gemini_bridge_client_leaving_keeps_the_latest_counts(traces, metered):
    response = await _bridge_response(_gemini(G_CHUNKS), GEMINI_MODEL, _root_trace_ctx())
    gen = response.body_iterator

    await _take(gen, 2)
    await gen.aclose()
    await _settle_metering()

    assert len(traces) == 1
    _assert_closed_abandoned(traces[0], _usage_from_gemini_metadata(G_CHUNKS[-1].usage_metadata), output="Hello")
    metered.assert_awaited_once_with(GEMINI_MODEL, user_id=_USER_ID)


async def test_gemini_bridge_failing_mid_stream_keeps_the_latest_counts(traces, metered):
    response = await _bridge_response(
        _gemini([*G_CHUNKS, ConnectionError("reset by peer")]), GEMINI_MODEL, _root_trace_ctx()
    )

    body = await _consume(response.body_iterator)

    assert body[-1] == "data: [DONE]\n\n"
    assert len(traces) == 1
    _assert_closed_failed(traces[0], _usage_from_gemini_metadata(G_CHUNKS[-1].usage_metadata))
    metered.assert_not_awaited()


B_EVENTS = [*B_OPENING, _b_text("Hel"), _b_text("lo"), *B_CLOSING, _b_metadata(500, 30)]
B_USAGE = _usage_from_bedrock({"inputTokens": 500, "outputTokens": 30, "totalTokens": 530})


async def test_bedrock_bridge_normal_end_records_the_metadata_usage(traces, metered, frozen_time):
    response = await _bridge_response(_bedrock(B_EVENTS), BEDROCK_MODEL, _root_trace_ctx())

    body = await _consume(response.body_iterator)

    assert body == [
        _content_chunk(BEDROCK_MODEL, "Hel"),
        _content_chunk(BEDROCK_MODEL, "lo"),
        _final_chunk(BEDROCK_MODEL),
        "data: [DONE]\n\n",
    ]
    assert len(traces) == 1
    _assert_closed_ok(traces[0], B_USAGE)
    metered.assert_awaited_once_with(BEDROCK_MODEL, user_id=_USER_ID)


async def test_bedrock_bridge_client_leaving_before_metadata_records_nothing(traces, metered):
    """Bedrock reports usage only in its closing ``metadata`` event."""
    response = await _bridge_response(_bedrock(B_EVENTS), BEDROCK_MODEL, _root_trace_ctx())
    gen = response.body_iterator

    await _take(gen, 1)
    await gen.aclose()
    await _settle_metering()

    assert len(traces) == 1
    _assert_closed_abandoned(traces[0], None, output="Hel")
    metered.assert_awaited_once_with(BEDROCK_MODEL, user_id=_USER_ID)


async def test_bedrock_bridge_client_leaving_after_metadata_keeps_it(traces, metered):
    """The client leaves on the closing chunk, after Bedrock's ``metadata``."""
    response = await _bridge_response(_bedrock(B_EVENTS), BEDROCK_MODEL, _root_trace_ctx())
    gen = response.body_iterator

    await _take(gen, 3)
    await gen.aclose()
    await _settle_metering()

    assert len(traces) == 1
    _assert_closed_abandoned(traces[0], B_USAGE, output="Hello")
    metered.assert_awaited_once_with(BEDROCK_MODEL, user_id=_USER_ID)


# ---------------------------------------------------------------------------
# ``/v1/messages`` pass-through (``_pipe_stream``)
# ---------------------------------------------------------------------------


def _sse(event) -> bytes:
    return f"event: {event.type}\ndata: {event.model_dump_json()}\n\n".encode()


@contextlib.asynccontextmanager
async def _messages_response(upstream: list, root: dict, metered: AsyncMock):
    """Call ``/v1/messages`` against an upstream that sends ``upstream``, one
    item per network chunk (``HANG`` / ``_Do`` markers included)."""

    async def _upstream_bytes():
        for item in upstream:
            if item is HANG:
                await asyncio.Event().wait()
            elif isinstance(item, _Do):
                item.fn()
            else:
                yield item

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_upstream_bytes())

    real_client = httpx.AsyncClient
    user = SimpleNamespace(id=uuid.UUID(_USER_ID), email="user@example.com")
    payload = {"model": ANTHROPIC_MODEL, "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    request = SimpleNamespace(headers={}, body=AsyncMock(return_value=json.dumps(payload).encode()))
    anthropic_config = SimpleNamespace(id=uuid.uuid4(), display_name=None, api_key_enc="enc")
    with (
        patch.object(llm_proxy, "_authenticate_api_key", AsyncMock(return_value=user)),
        patch.object(llm_proxy, "_refuse_unless_chat_enabled", AsyncMock()),
        patch.object(llm_proxy, "_get_anthropic_config", AsyncMock(return_value=anthropic_config)),
        patch("app.core.security.decrypt_stored_value", lambda _enc: "sk-ant-test"),
        patch("app.services.quota.check_llm_proxy_quota", AsyncMock()),
        patch.object(llm_proxy, "trace_llm_call", AsyncMock(return_value=root)),
        patch.object(llm_proxy, "release_db_connection_before_loop", AsyncMock()),
        patch.object(
            llm_proxy.httpx,
            "AsyncClient",
            lambda **kwargs: real_client(transport=httpx.MockTransport(_handler), **kwargs),
        ),
    ):
        response = await llm_proxy.proxy_anthropic_messages(request, SimpleNamespace())
    # ``/v1/messages`` meters every request before it forwards it.
    metered.assert_awaited_once_with(ANTHROPIC_MODEL, user_id=_USER_ID)
    yield response


P_OPENING = [_sse(_a_start()), _sse(_a_block_start()), _sse(_a_text("Hel")), _sse(_a_text("lo")), _sse(_a_block_stop())]


async def test_messages_passthrough_keeps_the_latest_cumulative_output(traces, metered):
    """Two ``message_delta`` events (10, then 25): cumulative, so 25."""
    upstream = [*P_OPENING, _sse(_a_delta(10)), _sse(_a_delta(25)), _sse(_a_stop())]
    async with _messages_response(upstream, _root_trace_ctx(), metered) as response:
        body = await _consume(response.body_iterator)

    # The client receives the upstream bytes, untouched.
    assert b"".join(body) == b"".join(upstream)
    assert len(traces) == 1
    # 25 output tokens, not the two counts added up (35).
    _assert_closed_ok(traces[0], {**A_START_USAGE, "output_tokens": 25})
    assert traces[0].output == "Hello"
    assert metered.await_count == 1


async def test_messages_passthrough_client_leaving_closes_the_trace(traces, metered):
    """The client leaves while ``message_start`` is being handed to it: the
    prompt side it reported is recorded, the request is not metered again."""
    upstream = [*P_OPENING, _sse(_a_delta(25)), _sse(_a_stop())]
    async with _messages_response(upstream, _root_trace_ctx(), metered) as response:
        gen = response.body_iterator
        await _take(gen, 1)
        await gen.aclose()
    await _settle_metering()

    assert len(traces) == 1
    _assert_closed_abandoned(traces[0], A_START_USAGE, output=None)
    assert metered.await_count == 1


async def test_messages_passthrough_cancelled_while_waiting_keeps_the_last_delta(traces, metered):
    reached = asyncio.Event()
    upstream = [*P_OPENING, _sse(_a_delta(42)), _Do(reached.set), HANG]
    async with _messages_response(upstream, _root_trace_ctx(), metered) as response:
        task = asyncio.create_task(_consume(response.body_iterator))
        await asyncio.wait_for(reached.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await _settle_metering()

    assert len(traces) == 1
    _assert_closed_abandoned(traces[0], {**A_START_USAGE, "output_tokens": 42}, output="Hello")
    assert metered.await_count == 1


# ---------------------------------------------------------------------------
# A refusal through the pass-through is traced as a decline
# ---------------------------------------------------------------------------

_REFUSAL = {"category": "cyber", "explanation": "Cyber harm."}


def _o_refusal() -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id="chatcmpl-test",
        object="chat.completion.chunk",
        created=0,
        model=OPENAI_MODEL,
        choices=[
            Choice(
                index=0,
                delta=ChoiceDelta(),
                finish_reason="content_filter",
                stop_details=_REFUSAL,
            )
        ],
    )


async def test_openai_passthrough_traces_a_refusal_as_a_decline(traces, metered):
    """The client gets the upstream chunks as sent; the trace is the
    ``model_declined`` failure with the usage, not an answer."""
    items = [_o_chunk("Sure"), _o_refusal(), _o_usage(100, 20)]
    response = await _openai_response(items, _root_trace_ctx())

    body = await _consume(response.body_iterator)

    assert body == [f"data: {c.model_dump_json()}\n\n" for c in items] + ["data: [DONE]\n\n"]
    assert len(traces) == 1
    assert traces[0].status == "error"
    assert traces[0].failure_cause == "model_declined"
    assert traces[0].usage_details == lft._normalize_usage(O_USAGE)
    metered.assert_awaited_once_with(OPENAI_MODEL, user_id=_USER_ID)


async def test_openai_passthrough_traces_a_refusal_answer_as_a_decline(traces):
    result = ChatCompletion.model_validate(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": OPENAI_MODEL,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "content_filter",
                    "message": {"role": "assistant", "content": None},
                    "stop_details": _REFUSAL,
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        }
    )
    provider = _openai([])
    provider.client.chat.completions.create = AsyncMock(return_value=result)
    user = SimpleNamespace(id=uuid.UUID(_USER_ID), email="user@example.com")
    body = llm_proxy.ChatCompletionRequest(
        model="auto", messages=[{"role": "user", "content": "hi"}], stream=False
    )
    with (
        patch.object(llm_proxy, "_authenticate_api_key", AsyncMock(return_value=user)),
        patch.object(llm_proxy, "_refuse_unless_chat_enabled", AsyncMock()),
        patch("app.services.quota.check_llm_proxy_quota", AsyncMock()),
        patch.object(llm_proxy, "get_llm_for_user", AsyncMock(return_value=(provider, OPENAI_MODEL))),
        patch.object(llm_proxy, "trace_llm_call", AsyncMock(return_value=_root_trace_ctx())),
        patch.object(llm_proxy, "release_db_connection_before_loop", AsyncMock()),
        patch.object(llm_proxy, "_track_model_usage", AsyncMock()),
    ):
        response = await llm_proxy.proxy_chat_completions(body, SimpleNamespace(), SimpleNamespace())

    assert response["choices"][0]["finish_reason"] == "content_filter"
    assert len(traces) == 1
    assert traces[0].status == "error"
    assert traces[0].failure_cause == "model_declined"
    assert traces[0].usage_details == lft._normalize_usage(O_USAGE)
