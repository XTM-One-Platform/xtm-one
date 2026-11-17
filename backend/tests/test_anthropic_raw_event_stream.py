"""Both Anthropic streaming paths read the raw server-sent events.

The SDK's ``messages.stream()`` helper rebuilds a snapshot of the whole
message on every event and re-parses a tool call's accumulated input JSON on
each ``input_json_delta``, which the API sends 7 bytes at a time: quadratic
in the input's size, on the event loop. These tests run the real SDK client
over an in-memory transport with that helper's accumulation disabled, so a
path that goes back to it fails here.
"""

from __future__ import annotations

import json

import anthropic
import httpx
import pytest

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import ChatMessage
from app.tools.base import ToolDefinition

_TOOL = ToolDefinition(
    name="generate_presentation",
    description="Create a deck.",
    input_schema={"type": "object", "properties": {"spec": {"type": "object"}}},
    source="builtin",
)
_SPEC = {
    "spec": {
        "title": "Quarterly threat review",
        "slides": [
            {"id": f"s{i}", "layout": "content", "title": f"Slide {i}", "notes": "n" * 40}
            for i in range(40)
        ],
    }
}


def _sse(events: list[dict]) -> bytes:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


def _message_start() -> dict:
    return {
        "type": "message_start",
        "message": {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 120, "output_tokens": 1},
        },
    }


def _end(stop_reason: str, output_tokens: int) -> list[dict]:
    return [
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        },
        {"type": "message_stop"},
    ]


def _tool_round_events() -> list[dict]:
    raw = json.dumps(_SPEC)
    return [
        _message_start(),
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Building."}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "generate_presentation",
                "input": {},
            },
        },
        *(
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": raw[i : i + 7]},
            }
            for i in range(0, len(raw), 7)
        ),
        {"type": "content_block_stop", "index": 1},
        *_end("tool_use", 900),
    ]


def _text_events() -> list[dict]:
    return [
        _message_start(),
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        *(
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": part}}
            for part in ("Three ", "findings ", "stand out.")
        ),
        {"type": "content_block_stop", "index": 0},
        *_end("end_turn", 12),
    ]


def _provider(events: list[dict], sent: list[dict]) -> AnthropicProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=_sse(events)
        )

    provider = AnthropicProvider(api_key="test-key")
    provider.client = anthropic.AsyncAnthropic(
        api_key="test-key",
        base_url="https://api.anthropic.test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return provider


@pytest.fixture
def no_sdk_accumulation(monkeypatch):
    def _refuse(**_kwargs):
        raise AssertionError("the SDK's per-event message snapshot was built")

    monkeypatch.setattr("anthropic.lib.streaming._messages.accumulate_event", _refuse)


@pytest.mark.asyncio
async def test_a_tool_round_streams_from_the_raw_events(no_sdk_accumulation):
    sent: list[dict] = []
    provider = _provider(_tool_round_events(), sent)
    try:
        response = await provider.chat_stream_with_tools(
            [ChatMessage(role="user", content="Build the deck.")],
            tools=[_TOOL],
            model="claude-sonnet-5",
        )
    finally:
        await provider.client.close()

    assert sent[0]["stream"] is True
    assert response.stop_reason == "tool_use"
    assert response.content == "Building."
    assert [(call.id, call.name) for call in response.tool_calls] == [
        ("toolu_1", "generate_presentation")
    ]
    assert response.tool_calls[0].arguments == _SPEC
    assert response.usage["input_tokens"] == 120
    assert response.usage["output_tokens"] == 900


@pytest.mark.asyncio
async def test_a_text_stream_reads_the_raw_events(no_sdk_accumulation):
    sent: list[dict] = []
    provider = _provider(_text_events(), sent)
    try:
        chunks = [
            chunk
            async for chunk in provider.chat_stream(
                [ChatMessage(role="user", content="Summarise.")], model="claude-sonnet-5"
            )
        ]
    finally:
        await provider.client.close()

    assert sent[0]["stream"] is True
    assert chunks == ["Three ", "findings ", "stand out."]
    assert provider.last_stop_reason == "end_turn"
    assert provider.last_usage["output_tokens"] == 12
