"""The Anthropic bridge of ``/v1/chat/completions`` carries tool calling.

An OpenAI-compatible client (an XTM One backend whose custom OpenAI provider
points at the proxy, an IDE, any SDK) sends ``tools``, ``tool_choice``,
assistant ``tool_calls`` and ``role: tool`` results. On a model served by an
Anthropic provider the bridge used to drop all of it, so the model never
called a tool and wrote the call as prose. Each conversion is pinned here:
the request (tools and tool choice), the history replay, the non-streaming
response and the streamed chunks, built from the Anthropic SDK's own types
with no network and no key.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from anthropic.types import (
    InputJSONDelta,
    Message,
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    RawContentBlockStopEvent,
    RawMessageStopEvent,
    TextBlock,
    TextDelta,
    ToolUseBlock,
    Usage,
)

from app.api.v1 import llm_proxy
from app.llm.anthropic_provider import AnthropicProvider

MODEL = "claude-sonnet-4-6"
USER_ID = str(uuid.uuid4())

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "The weather of a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


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


@pytest.fixture(autouse=True)
def _quiet_side_effects(monkeypatch):
    monkeypatch.setattr(llm_proxy, "_track_model_usage", AsyncMock())
    monkeypatch.setattr(llm_proxy, "_meter_finished_stream", AsyncMock())
    with patch("app.services.tracing.dispatcher.submit_trace_for_flush"):
        yield


def _message(content: list, stop_reason: str | None) -> Message:
    return Message(
        id="msg_test",
        type="message",
        role="assistant",
        model=MODEL,
        content=content,
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=Usage(input_tokens=10, output_tokens=5),
    )


class _Stream:
    def __init__(self, events: list, final: Message) -> None:
        self._events = list(events)
        self._final = final

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._events:
            return self._events.pop(0)
        raise StopAsyncIteration

    async def get_final_message(self) -> Message:
        return self._final


class _StreamCtx:
    def __init__(self, stream: _Stream) -> None:
        self._stream = stream

    async def __aenter__(self):
        return self._stream

    async def __aexit__(self, *_exc):
        return False


def _provider(*, result: Message | None = None, events: list | None = None) -> tuple[AnthropicProvider, list[dict]]:
    """A provider whose client records the params it is called with."""
    sent: list[dict] = []
    provider = AnthropicProvider(api_key="test-key")

    async def _create(**params):
        sent.append(params)
        return result

    def _stream(**params):
        sent.append(params)
        return _StreamCtx(_Stream(events or [], result or _message([], "end_turn")))

    provider.client = SimpleNamespace(messages=SimpleNamespace(create=_create, stream=_stream))
    return provider, sent


async def _call(provider, body: llm_proxy.ChatCompletionRequest):
    raw = llm_proxy._build_raw_openai_messages(body.messages)
    return await llm_proxy._proxy_via_anthropic(
        provider, body, raw, MODEL, 1_024, _root_trace_ctx(), user_id=USER_ID
    )


# ── Request: tools and tool_choice ───────────────────────────────────────


async def test_request_forwards_tools_in_anthropic_form():
    provider, sent = _provider(result=_message([TextBlock(type="text", text="ok")], "end_turn"))
    body = llm_proxy.ChatCompletionRequest(
        model=MODEL, messages=[{"role": "user", "content": "weather?"}], tools=[WEATHER_TOOL]
    )

    await _call(provider, body)

    assert sent[0]["tools"] == [
        {
            "name": "get_weather",
            "description": "The weather of a city.",
            "input_schema": WEATHER_TOOL["function"]["parameters"],
        }
    ]
    assert "tool_choice" not in sent[0]


async def test_request_without_tools_sends_none():
    provider, sent = _provider(result=_message([TextBlock(type="text", text="ok")], "end_turn"))
    body = llm_proxy.ChatCompletionRequest(model=MODEL, messages=[{"role": "user", "content": "hi"}])

    await _call(provider, body)

    assert "tools" not in sent[0]
    assert "tool_choice" not in sent[0]


@pytest.mark.parametrize(
    ("openai_choice", "anthropic_choice"),
    [
        ("auto", {"type": "auto"}),
        ("none", {"type": "none"}),
        ("required", {"type": "any"}),
        ({"type": "function", "function": {"name": "get_weather"}}, {"type": "tool", "name": "get_weather"}),
        ({"type": "allowed_tools", "allowed_tools": {"mode": "required", "tools": []}}, {"type": "any"}),
        ({"type": "tool", "name": "get_weather"}, {"type": "tool", "name": "get_weather"}),
    ],
)
def test_tool_choice_maps_to_anthropic(openai_choice, anthropic_choice):
    assert llm_proxy._anthropic_tool_choice(openai_choice) == anthropic_choice


def test_parallel_tool_calls_false_disables_parallel_use():
    assert llm_proxy._anthropic_tool_choice("auto", False) == {"type": "auto", "disable_parallel_tool_use": True}
    assert llm_proxy._anthropic_tool_choice(None, False) == {"type": "auto", "disable_parallel_tool_use": True}
    assert llm_proxy._anthropic_tool_choice("none", False) == {"type": "none"}
    assert llm_proxy._anthropic_tool_choice(None) is None


async def test_request_forwards_tool_choice_and_counts_tools_in_overflow_check():
    provider, sent = _provider(result=_message([TextBlock(type="text", text="ok")], "end_turn"))
    seen_tools: list = []
    original = provider._check_resolved_overflow

    def _spy(**kwargs):
        seen_tools.append(kwargs.get("tools"))
        return original(**kwargs)

    provider._check_resolved_overflow = _spy
    body = llm_proxy.ChatCompletionRequest(
        model=MODEL,
        messages=[{"role": "user", "content": "weather?"}],
        tools=[WEATHER_TOOL],
        tool_choice={"type": "function", "function": {"name": "get_weather"}},
    )

    await _call(provider, body)

    assert sent[0]["tool_choice"] == {"type": "tool", "name": "get_weather"}
    assert [t.name for t in seen_tools[0]] == ["get_weather"]


# ── History replay: tool_calls and role tool ─────────────────────────────


def test_history_replays_tool_calls_and_results():
    raw = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "weather in Paris and Lyon?"},
        {
            "role": "assistant",
            "content": "Checking.",
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}},
                {"id": "call_2", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Lyon"}'}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "12C"},
        {"role": "tool", "tool_call_id": "call_2", "content": [{"type": "text", "text": "9C"}]},
        {"role": "user", "content": "thanks"},
    ]

    system, msgs = llm_proxy._openai_msgs_to_anthropic(raw)

    assert system == "be brief"
    assert msgs == [
        {"role": "user", "content": "weather in Paris and Lyon?"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Checking."},
                {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "Paris"}},
                {"type": "tool_use", "id": "call_2", "name": "get_weather", "input": {"city": "Lyon"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": "12C"},
                {"type": "tool_result", "tool_use_id": "call_2", "content": "9C"},
            ],
        },
        {"role": "user", "content": "thanks"},
    ]


def test_history_replays_a_tool_call_without_text_and_bad_arguments():
    raw = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "noop", "arguments": "not json"}}],
        },
    ]

    _system, msgs = llm_proxy._openai_msgs_to_anthropic(raw)

    assert msgs[1] == {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "call_1", "name": "noop", "input": {}}],
    }


# ── Non-streaming response ───────────────────────────────────────────────


async def test_response_tool_use_becomes_tool_calls():
    result = _message(
        [
            TextBlock(type="text", text="Let me check."),
            ToolUseBlock(type="tool_use", id="toolu_1", name="get_weather", input={"city": "Paris"}),
        ],
        "tool_use",
    )
    provider, _sent = _provider(result=result)
    body = llm_proxy.ChatCompletionRequest(
        model=MODEL, messages=[{"role": "user", "content": "weather?"}], tools=[WEATHER_TOOL]
    )

    response = await _call(provider, body)

    choice = response["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] == "Let me check."
    assert choice["message"]["tool_calls"] == [
        {
            "id": "toolu_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": json.dumps({"city": "Paris"})},
        }
    ]


async def test_text_response_carries_no_tool_calls():
    provider, _sent = _provider(result=_message([TextBlock(type="text", text="hi")], "end_turn"))
    body = llm_proxy.ChatCompletionRequest(model=MODEL, messages=[{"role": "user", "content": "hi"}])

    response = await _call(provider, body)

    assert "tool_calls" not in response["choices"][0]["message"]
    assert response["choices"][0]["finish_reason"] == "stop"


# ── Streaming chunks ─────────────────────────────────────────────────────


def _chunks(body: list[str]) -> list[dict]:
    out = []
    for line in body:
        payload = line.removeprefix("data: ").strip()
        if payload and payload != "[DONE]":
            out.append(json.loads(payload))
    return out


async def test_stream_emits_tool_call_chunks():
    events = [
        RawContentBlockStartEvent(type="content_block_start", index=0, content_block=TextBlock(type="text", text="")),
        RawContentBlockDeltaEvent(type="content_block_delta", index=0, delta=TextDelta(type="text_delta", text="On it.")),
        RawContentBlockStopEvent(type="content_block_stop", index=0),
        RawContentBlockStartEvent(
            type="content_block_start",
            index=1,
            content_block=ToolUseBlock(type="tool_use", id="toolu_1", name="get_weather", input={}),
        ),
        RawContentBlockDeltaEvent(
            type="content_block_delta", index=1, delta=InputJSONDelta(type="input_json_delta", partial_json='{"city": ')
        ),
        RawContentBlockDeltaEvent(
            type="content_block_delta", index=1, delta=InputJSONDelta(type="input_json_delta", partial_json='"Paris"}')
        ),
        RawContentBlockStopEvent(type="content_block_stop", index=1),
        RawMessageStopEvent(type="message_stop"),
    ]
    provider, sent = _provider(result=_message([], "tool_use"), events=events)
    body = llm_proxy.ChatCompletionRequest(
        model=MODEL, messages=[{"role": "user", "content": "weather?"}], tools=[WEATHER_TOOL], stream=True
    )

    response = await _call(provider, body)
    chunks = _chunks([line async for line in response.body_iterator])

    assert sent[0]["tools"][0]["name"] == "get_weather"
    deltas = [c["choices"][0]["delta"] for c in chunks]
    assert deltas[0] == {"content": "On it."}
    assert deltas[1] == {
        "tool_calls": [
            {"index": 0, "id": "toolu_1", "type": "function", "function": {"name": "get_weather", "arguments": ""}}
        ]
    }
    arguments = "".join(d["tool_calls"][0]["function"]["arguments"] for d in deltas[2:4])
    assert json.loads(arguments) == {"city": "Paris"}
    assert all(d["tool_calls"][0]["index"] == 0 for d in deltas[2:4])
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


async def test_stream_numbers_parallel_tool_calls_from_zero():
    events = [
        RawContentBlockStartEvent(
            type="content_block_start",
            index=0,
            content_block=ToolUseBlock(type="tool_use", id="toolu_a", name="get_weather", input={}),
        ),
        RawContentBlockStopEvent(type="content_block_stop", index=0),
        RawContentBlockStartEvent(
            type="content_block_start",
            index=1,
            content_block=ToolUseBlock(type="tool_use", id="toolu_b", name="get_weather", input={}),
        ),
        RawContentBlockStopEvent(type="content_block_stop", index=1),
    ]
    provider, _sent = _provider(result=_message([], "tool_use"), events=events)
    body = llm_proxy.ChatCompletionRequest(
        model=MODEL, messages=[{"role": "user", "content": "weather?"}], tools=[WEATHER_TOOL], stream=True
    )

    response = await _call(provider, body)
    chunks = _chunks([line async for line in response.body_iterator])

    starts = [c["choices"][0]["delta"]["tool_calls"][0] for c in chunks if c["choices"][0]["delta"].get("tool_calls")]
    assert [(s["index"], s["id"]) for s in starts] == [(0, "toolu_a"), (1, "toolu_b")]
