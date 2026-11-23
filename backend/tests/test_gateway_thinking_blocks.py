"""Claude thinking blocks round-trip through the AI Gateway.

The gateway returns them as ``thinking_blocks`` and Anthropic refuses a
tool-use continuation without them, so the provider keeps them on the tool
turn and sends them back within the round.
"""

import asyncio
import json
import uuid

import httpx
import openai
import pytest

from app.core.xtm_auth import _normalize_url
from app.llm.base import ChatMessage
from app.llm.gateway_provider import FiligranGatewayProvider, XtmJwtAuth, _gateway_http_client
from app.llm.identity import _llm_caller, set_llm_caller
from app.llm.litellm_provider import LiteLLMProvider
from app.llm.openai_thinking_blocks import EXTRA_KEY, ThinkingBlocks
from app.tools.base import ToolCall, ToolDefinition
from tests.gateway_helpers import gateway_refusal_body

GATEWAY_URL = "http://gateway.local:8080"
MODEL = "claude-sonnet-4-5"
BLOCK = {"type": "thinking", "thinking": "Need weather.", "signature": "sig=="}
REDACTED = {"type": "redacted_thinking", "data": "opaque"}
TOOL = ToolDefinition(name="weather", description="Weather", input_schema={"type": "object", "properties": {}})


@pytest.fixture(autouse=True)
def _caller():
    token = _llm_caller.set(None)
    set_llm_caller(uuid.uuid4(), "alice@example.com")
    yield
    _llm_caller.reset(token)


def _completion(message: dict, finish: str = "stop") -> dict:
    return {
        "id": "c", "object": "chat.completion", "created": 1, "model": MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", **message}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


def _tool_call_message(blocks: list[dict]) -> dict:
    return {
        "content": None,
        "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "weather", "arguments": "{}"}}],
        "thinking_blocks": blocks,
    }


class _Gateway:
    """Answers with queued responses and records every request body."""

    def __init__(self, *answers: httpx.Response) -> None:
        self.answers = list(answers)
        self.sent: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(json.loads(request.content))
        return self.answers.pop(0)

    def provider(self, cls=FiligranGatewayProvider):
        transport = httpx.MockTransport(self)
        if cls is FiligranGatewayProvider:
            # The real client: its hook stops the SDK re-sending a ``retryable: false`` 502.
            client = _gateway_http_client(XtmJwtAuth(_normalize_url(GATEWAY_URL)), None, transport=transport)
            return cls(api_key=GATEWAY_URL, http_client=client)
        return cls(api_key="key", base_url=f"{GATEWAY_URL}/v1", http_client=httpx.AsyncClient(transport=transport))

    def assistant_turns(self, index: int = -1) -> list[dict]:
        return [m for m in self.sent[index]["messages"] if m["role"] == "assistant"]


def _gateway_refusal(provider_message: str | None, status: str = "400 Bad Request") -> httpx.Response:
    return httpx.Response(502, json=gateway_refusal_body(provider_message, status=status))


def _ok() -> httpx.Response:
    return httpx.Response(200, json=_completion({"content": "Sunny."}))


def _round(extra: dict | None, *, model: str = MODEL, provider: str = "filigran_gateway") -> list[ChatMessage]:
    """A user question, a tool turn carrying *extra*, and its tool result."""
    turn = ChatMessage(
        role="assistant", content="", tool_calls=[ToolCall(id="call_1", name="weather", arguments={})]
    )
    if extra is not None:
        turn._provider_extra = extra
    return [
        ChatMessage(role="user", content="Weather?"),
        turn,
        ChatMessage(role="tool", content="Sunny", tool_call_id="call_1"),
    ]


def _kept(**overrides) -> dict:
    fields = {"blocks": (BLOCK, REDACTED), "provider": "filigran_gateway", "model": MODEL, "call_ids": ("call_1",)}
    return {EXTRA_KEY: ThinkingBlocks(**{**fields, **overrides})}


@pytest.mark.asyncio
async def test_a_tool_turn_keeps_its_blocks_and_sends_them_back():
    gateway = _Gateway(
        httpx.Response(200, json=_completion(_tool_call_message([BLOCK, REDACTED]), "tool_calls")), _ok()
    )
    provider = gateway.provider()
    response = await provider.chat([ChatMessage(role="user", content="Weather?")], model=MODEL, tools=[TOOL])
    kept = response._provider_extra[EXTRA_KEY]
    assert kept.blocks == (BLOCK, REDACTED)

    messages = _round(response._provider_extra)
    await provider.chat(messages, model=MODEL, tools=[TOOL])
    assert gateway.assistant_turns()[0]["thinking_blocks"] == [BLOCK, REDACTED]


@pytest.mark.asyncio
async def test_streamed_blocks_are_kept():
    chunk = {"id": "1", "object": "chat.completion.chunk", "created": 1, "model": MODEL}
    chunks = [
        {**chunk, "choices": [{"index": 0, "delta": {"reasoning_content": "Need weather."}, "finish_reason": None}]},
        {**chunk, "choices": [{"index": 0, "delta": {"thinking_blocks": [BLOCK]}, "finish_reason": None}]},
        {**chunk, "choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "type": "function", "function": {"name": "weather", "arguments": "{}"}}
        ]}, "finish_reason": "tool_calls"}]},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    gateway = _Gateway(httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"}))
    response = await gateway.provider().chat_stream_with_tools(
        [ChatMessage(role="user", content="Weather?")], tools=[TOOL], model=MODEL
    )
    assert response._provider_extra[EXTRA_KEY].blocks == (BLOCK,)
    assert response.thinking == "Need weather."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        {"content": "Sunny.", "thinking_blocks": [BLOCK]},  # no tool call: never replayed
        _tool_call_message([{**BLOCK, "signature": ""}]),  # cannot be re-sent as returned
        _tool_call_message([]),
        {**_tool_call_message([BLOCK]), "tool_calls": [
            {"id": "", "type": "function", "function": {"name": "weather", "arguments": "{}"}}
        ]},  # no call id to match the turn by
    ],
)
async def test_nothing_is_kept_when_it_could_not_be_sent_back(message):
    gateway = _Gateway(httpx.Response(200, json=_completion(message)))
    response = await gateway.provider().chat([ChatMessage(role="user", content="Weather?")], model=MODEL)
    assert response._provider_extra is None


@pytest.mark.asyncio
async def test_another_openai_compatible_provider_neither_keeps_nor_sends_them():
    gateway = _Gateway(httpx.Response(200, json=_completion(_tool_call_message([BLOCK]), "tool_calls")), _ok())
    provider = gateway.provider(LiteLLMProvider)
    response = await provider.chat([ChatMessage(role="user", content="Weather?")], model=MODEL, tools=[TOOL])
    assert response._provider_extra is None
    await provider.chat(_round(_kept()), model=MODEL, tools=[TOOL])
    assert "thinking_blocks" not in gateway.assistant_turns()[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra", "model", "follow_up"),
    [
        (_kept(model="claude-opus-4-5"), MODEL, False),  # another model answers
        (_kept(provider="litellm"), MODEL, False),
        (_kept(call_ids=("call_other",)), MODEL, False),  # the loop rewrote the calls
        (_kept(refused=True), MODEL, False),
        (_kept(), MODEL, True),  # a user message ended the round
    ],
)
async def test_blocks_go_back_only_within_the_round_on_the_same_model(extra, model, follow_up):
    gateway = _Gateway(_ok())
    messages = _round(extra)
    if follow_up:
        messages += [ChatMessage(role="assistant", content="Sunny."), ChatMessage(role="user", content="And tomorrow?")]
    await gateway.provider().chat(messages, model=model, tools=[TOOL])
    assert all("thinking_blocks" not in m for m in gateway.assistant_turns())


@pytest.mark.asyncio
async def test_a_loop_nudge_does_not_end_the_round():
    gateway = _Gateway(_ok())
    messages = [*_round(_kept()), ChatMessage(role="system", content="Keep going.")]
    await gateway.provider().chat(messages, model=MODEL, tools=[TOOL])
    assert gateway.assistant_turns()[0]["thinking_blocks"] == [BLOCK, REDACTED]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refusal",
    [
        httpx.Response(400, json={"error": {"message": "Invalid `signature` in `thinking` block"}}),
        httpx.Response(502, json={"error": {"type": "provider_error", "message": "upstream returned status 400",
                                            "retryable": False}}),
        _gateway_refusal("messages.1.content.0: Invalid `signature` in `thinking` block"),
        _gateway_refusal("Request exceeds the maximum size", status="413 Request Entity Too Large"),
    ],
    ids=["direct 400", "gateway 502 without the provider's message", "gateway 502 with it", "gateway 502 for a 413"],
)
@pytest.mark.parametrize("call", ["chat", "chat_stream", "chat_stream_with_tools"])
async def test_a_refused_replay_is_retried_once_without_and_never_sent_again(refusal, call):
    stream_ok = httpx.Response(
        200,
        content=b'data: {"id":"1","object":"chat.completion.chunk","created":1,"model":"m",'
        b'"choices":[{"index":0,"delta":{"content":"Sunny."},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
        headers={"content-type": "text/event-stream"},
    )
    ok = _ok if call == "chat" else lambda: stream_ok
    gateway = _Gateway(refusal, ok())
    provider = gateway.provider()
    messages = _round(_kept())
    if call == "chat_stream":
        async for _ in provider.chat_stream(messages, model=MODEL):
            pass
    else:
        await getattr(provider, call)(messages, model=MODEL, tools=[TOOL])
    assert len(gateway.sent) == 2
    assert "thinking_blocks" in gateway.assistant_turns(0)[0]
    assert "thinking_blocks" not in gateway.assistant_turns(1)[0]
    assert messages[1]._provider_extra[EXTRA_KEY].refused


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refusal",
    [
        _gateway_refusal("tools.0.custom.name: String should match pattern"),
        _gateway_refusal("model: claude-x not found", status="404 Not Found"),
        _gateway_refusal(None, status="404 Not Found"),
    ],
    ids=["400 naming another cause", "404", "404 without the provider's message"],
)
async def test_a_gateway_refusal_naming_another_cause_keeps_the_blocks(refusal):
    """As a direct 404 is: never re-sent without the blocks."""
    gateway = _Gateway(refusal)
    messages = _round(_kept())
    with pytest.raises(openai.InternalServerError):
        await gateway.provider().chat(messages, model=MODEL, tools=[TOOL])
    assert len(gateway.sent) == 1
    assert not messages[1]._provider_extra[EXTRA_KEY].refused


REFUSED_BLOCKS = {"error": {"message": "Invalid `signature` in `thinking` block"}}


def _stream_ok() -> httpx.Response:
    return httpx.Response(
        200,
        content=b'data: {"id":"1","object":"chat.completion.chunk","created":1,"model":"m",'
        b'"choices":[{"index":0,"delta":{"content":"Sunny."},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
        headers={"content-type": "text/event-stream"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["chat", "chat_stream", "chat_stream_with_tools"])
async def test_the_retry_without_blocks_keeps_the_prompt_cache_breakpoints(call):
    gateway = _Gateway(httpx.Response(400, json=REFUSED_BLOCKS), _ok() if call == "chat" else _stream_ok())
    provider = gateway.provider()
    messages = [ChatMessage(role="system", content="You are helpful."), *_round(_kept())]
    if call == "chat_stream":
        async for _ in provider.chat_stream(messages, model=MODEL):
            pass
    else:
        await getattr(provider, call)(messages, model=MODEL, tools=[TOOL])
    first, retry = gateway.sent
    assert "thinking_blocks" in first["messages"][2]
    assert "thinking_blocks" not in retry["messages"][2]
    assert retry["messages"][0]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    # Everything else (top-level and tool breakpoints) goes out as first sent.
    assert {k: v for k, v in retry.items() if k != "messages"} == {k: v for k, v in first.items() if k != "messages"}


@pytest.mark.asyncio
async def test_blocks_refused_again_without_them_were_not_the_cause(caplog):
    bad = {"error": {"message": "bad tool schema"}}
    gateway = _Gateway(httpx.Response(400, json=REFUSED_BLOCKS), httpx.Response(400, json=bad))
    messages = _round(_kept())
    with pytest.raises(Exception, match="bad tool schema"):
        await gateway.provider().chat(messages, model=MODEL, tools=[TOOL])
    assert len(gateway.sent) == 2
    assert not messages[1]._provider_extra[EXTRA_KEY].refused
    # Only the structured message is logged, never the SDK's rendering of the body.
    assert "Thinking blocks refused (1 turn(s)); retrying once without them: Invalid `signature`" in caplog.text


@pytest.mark.asyncio
async def test_blocks_stay_retired_when_the_retry_only_hits_an_outage(monkeypatch):
    sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _delay: sleep(0))
    # The gateway's chat client leaves retries to the provider (#1468).
    outage = httpx.Response(503, json={"error": {"message": "overloaded"}})
    gateway = _Gateway(httpx.Response(400, json=REFUSED_BLOCKS), outage, _ok())
    messages = _round(_kept())
    await gateway.provider().chat(messages, model=MODEL, tools=[TOOL])
    assert len(gateway.sent) == 3
    assert "thinking_blocks" not in gateway.assistant_turns(2)[0]
    assert messages[1]._provider_extra[EXTRA_KEY].refused


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "bad tool schema",
        "tools.0.input_schema: property `signature` is invalid",  # the word alone is not a block refusal
    ],
)
async def test_a_400_that_does_not_refuse_the_blocks_is_not_retried_without_them(message):
    gateway = _Gateway(httpx.Response(400, json={"error": {"message": message}}))
    messages = _round(_kept())
    with pytest.raises(Exception, match="400"):
        await gateway.provider().chat(messages, model=MODEL, tools=[TOOL])
    assert len(gateway.sent) == 1
    assert not messages[1]._provider_extra[EXTRA_KEY].refused


@pytest.mark.asyncio
async def test_a_prompt_too_long_400_is_retried_without_the_blocks():
    too_long = {"error": {"message": "prompt is too long: 210000 tokens > 200000 maximum"}}
    gateway = _Gateway(httpx.Response(400, json=too_long), _ok())
    await gateway.provider().chat(_round(_kept()), model=MODEL, tools=[TOOL])
    assert "thinking_blocks" not in gateway.assistant_turns(1)[0]


@pytest.mark.asyncio
async def test_a_call_with_thinking_off_never_sends_blocks():
    gateway = _Gateway(_ok())
    await gateway.provider().chat(_round(_kept()), model=MODEL, tools=[TOOL], disable_thinking=True)
    assert "thinking_blocks" not in gateway.assistant_turns()[0]


@pytest.mark.asyncio
async def test_an_unrelated_refusal_without_blocks_is_not_retried():
    gateway = _Gateway(httpx.Response(400, json={"error": {"message": "bad tool schema"}}))
    with pytest.raises(Exception, match="bad tool schema"):
        await gateway.provider().chat(_round(None), model=MODEL, tools=[TOOL])
    assert len(gateway.sent) == 1


@pytest.mark.asyncio
async def test_blocks_that_would_overflow_the_window_are_dropped_for_the_request(monkeypatch):
    gateway = _Gateway(_ok())
    provider = gateway.provider()
    monkeypatch.setattr(provider, "_resolve_context_window", lambda model: 1000)
    huge = {**BLOCK, "signature": "s" * 8000}
    messages = _round(_kept(blocks=(huge,)))
    await provider.chat(messages, model=MODEL, tools=[TOOL])
    assert "thinking_blocks" not in gateway.assistant_turns()[0]
    assert not messages[1]._provider_extra[EXTRA_KEY].refused


@pytest.mark.asyncio
async def test_a_refused_temperature_is_still_retried_once_the_blocks_are_dropped():
    refused_blocks = httpx.Response(400, json=REFUSED_BLOCKS)
    refused_temperature = httpx.Response(
        400, json={"error": {"message": "Unsupported parameter: 'temperature'", "param": "temperature"}}
    )
    gateway = _Gateway(refused_blocks, refused_temperature, _ok())
    messages = _round(_kept())
    await gateway.provider().chat(messages, model=MODEL, tools=[TOOL], temperature=0.5)
    sent = [("thinking_blocks" in r["messages"][1], "temperature" in r) for r in gateway.sent]
    assert sent == [(True, True), (False, True), (False, False)]
    assert messages[1]._provider_extra[EXTRA_KEY].refused


@pytest.mark.asyncio
async def test_each_turn_of_a_multi_turn_round_gets_its_own_blocks():
    """Turns are paired by position from the end: two rounds' turns never swap blocks."""
    other = {**BLOCK, "thinking": "Need the time.", "signature": "other=="}
    first = ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id="call_1", name="weather", arguments={})])
    second = ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id="call_2", name="weather", arguments={})])
    first._provider_extra = _kept()
    second._provider_extra = _kept(blocks=(other,), call_ids=("call_2",))
    messages = [
        ChatMessage(role="user", content="Weather?"),
        first,
        ChatMessage(role="tool", content="Sunny", tool_call_id="call_1"),
        second,
        ChatMessage(role="tool", content="Rainy", tool_call_id="call_2"),
    ]
    gateway = _Gateway(_ok())
    await gateway.provider().chat(messages, model=MODEL, tools=[TOOL])
    assert [t["thinking_blocks"] for t in gateway.assistant_turns()] == [[BLOCK, REDACTED], [other]]
