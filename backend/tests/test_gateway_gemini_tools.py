"""A Gemini model behind the AI Gateway gets what the direct Gemini provider sends.

The gateway passes OpenAI-format tool schemas through, so they are cleaned as
``GeminiProvider`` cleans them; tool results carry the tool name, and Gemini's
thought signatures go back on the current round's tool calls.
"""

import json
import uuid

import httpx
import openai
import pytest

from app.core.xtm_auth import _normalize_url
from app.llm.base import ChatMessage
from app.llm.gateway_provider import FiligranGatewayProvider, XtmJwtAuth, _gateway_http_client
from app.llm.identity import _llm_caller, set_llm_caller
from app.llm.openai_thinking_blocks import EXTRA_KEY, ThinkingBlocks
from app.tools.base import ToolCall, ToolDefinition

GATEWAY_URL = "http://gateway.local:8080"
GEMINI = "gemini-4-argon"
SIGNATURE = {"google": {"thought_signature": "c2ln"}}

DUE = ToolDefinition(
    name="schedule",
    description="Schedule a task",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "due_at": {"type": ["string", "null"], "default": None},
            "object_type": {"type": "string", "enum": ["", "report"]},
        },
        "required": ["object_type"],
    },
)
BARE = ToolDefinition(name="now", description="Current time", input_schema={"type": "object", "properties": {}})


@pytest.fixture(autouse=True)
def _caller():
    token = _llm_caller.set(None)
    set_llm_caller(uuid.uuid4(), "alice@example.com")
    yield
    _llm_caller.reset(token)


def _completion(message: dict, finish: str = "stop") -> dict:
    return {
        "id": "c", "object": "chat.completion", "created": 1, "model": GEMINI,
        "choices": [{"index": 0, "message": {"role": "assistant", **message}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


def _ok() -> httpx.Response:
    return httpx.Response(200, json=_completion({"content": "Done."}))


def _stream(*deltas: dict) -> httpx.Response:
    chunk = {"id": "1", "object": "chat.completion.chunk", "created": 1, "model": GEMINI}
    body = "".join(
        f"data: {json.dumps({**chunk, 'choices': [{'index': 0, 'delta': d, 'finish_reason': None}]})}\n\n"
        for d in deltas
    )
    return httpx.Response(200, content=(body + "data: [DONE]\n\n").encode(), headers={"content-type": "text/event-stream"})


class _Gateway:
    """Answers chat calls with queued responses, lists models with *owner*, records bodies."""

    def __init__(self, *answers: httpx.Response, owner: str = "openai") -> None:
        self.answers = list(answers)
        self.owner = owner
        self.sent: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": "default", "object": "model", "created": 1, "owned_by": self.owner},
            ]})
        self.sent.append(json.loads(request.content))
        return self.answers.pop(0)

    def provider(self, credential: str = GATEWAY_URL) -> FiligranGatewayProvider:
        transport = httpx.MockTransport(self)
        client = _gateway_http_client(XtmJwtAuth(_normalize_url(GATEWAY_URL)), None, transport=transport)
        return FiligranGatewayProvider(api_key=credential, http_client=client)


async def _send(provider: FiligranGatewayProvider, call: str, messages: list[ChatMessage], model: str, tools):
    if call == "chat":
        return await provider.chat(messages, model=model, tools=tools)
    return await provider.chat_stream_with_tools(messages, tools=tools, model=model)


def _answer(call: str) -> httpx.Response:
    return _ok() if call == "chat" else _stream({"content": "Done."})


# --- tool schemas -----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["chat", "chat_stream_with_tools"])
@pytest.mark.parametrize(
    ("model", "credential", "owner"),
    [
        (GEMINI, GATEWAY_URL, "openai"),  # by name
        ("default", f"{GATEWAY_URL}||Gemini", "openai"),  # pinned target
        ("default", GATEWAY_URL, "Gemini"),  # listed owner
    ],
)
async def test_gemini_gets_the_direct_providers_schemas(call, model, credential, owner):
    gateway = _Gateway(_answer(call), owner=owner)
    stale = ToolDefinition(name="now", description="Old", input_schema={"type": "object", "properties": {}})
    await _send(gateway.provider(credential), call, [ChatMessage(role="user", content="Plan it")], model,
                [stale, DUE, BARE])
    tools = [t["function"] for t in gateway.sent[0]["tools"]]
    assert [t["name"] for t in tools] == ["now", "schedule"]  # duplicates: the last wins
    assert tools[0] == {"name": "now", "description": "Current time"}  # no parameters without properties
    assert tools[1]["parameters"] == {
        "type": "object",
        "properties": {
            "due_at": {"type": "string", "nullable": True},
            "object_type": {"type": "string", "enum": ["report"]},
        },
        "required": ["object_type"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["chat", "chat_stream_with_tools"])
async def test_another_model_gets_its_schemas_unchanged(call):
    gateway = _Gateway(_answer(call))
    await _send(gateway.provider(), call, [ChatMessage(role="user", content="Plan it")], "gpt-5", [DUE])
    assert gateway.sent[0]["tools"][0]["function"]["parameters"] == DUE.input_schema


# --- tool result names --------------------------------------------------------


def _round_of_two_turns(extra_first=None, extra_second=None) -> list[ChatMessage]:
    """Two tool turns of one round, each calling ``call_0`` as the gateway numbers them."""
    first = ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id="call_0", name="now", arguments={})])
    second = ChatMessage(
        role="assistant", content="", tool_calls=[ToolCall(id="call_0", name="schedule", arguments={})]
    )
    first._provider_extra = extra_first
    second._provider_extra = extra_second
    return [
        ChatMessage(role="user", content="Plan it"),
        first,
        ChatMessage(role="tool", content="09:00", tool_call_id="call_0"),
        second,
        ChatMessage(role="tool", content="ok", tool_call_id="call_0"),
    ]


@pytest.mark.asyncio
async def test_gemini_tool_results_carry_their_turns_tool_name():
    gateway = _Gateway(_ok())
    await gateway.provider().chat(_round_of_two_turns(), model=GEMINI, tools=[BARE, DUE])
    tool_messages = [m for m in gateway.sent[0]["messages"] if m["role"] == "tool"]
    assert [m["name"] for m in tool_messages] == ["now", "schedule"]


@pytest.mark.asyncio
async def test_other_models_tool_results_are_unchanged():
    gateway = _Gateway(_ok())
    await gateway.provider().chat(_round_of_two_turns(), model="gpt-5", tools=[BARE, DUE])
    assert all("name" not in m for m in gateway.sent[0]["messages"] if m["role"] == "tool")


# --- thought signatures --------------------------------------------------------


def _kept(call_id: str = "call_0", signature: str = "c2ln", model: str = GEMINI) -> dict:
    return {EXTRA_KEY: ThinkingBlocks((), "filigran_gateway", model, (call_id,), signatures=((call_id, signature),))}


@pytest.mark.asyncio
async def test_a_signature_is_kept_and_sent_back_on_its_call():
    call = {"id": "call_0", "type": "function", "function": {"name": "now", "arguments": "{}"},
            "extra_content": SIGNATURE}
    gateway = _Gateway(httpx.Response(200, json=_completion({"content": None, "tool_calls": [call]}, "tool_calls")),
                       _ok())
    provider = gateway.provider()
    response = await provider.chat([ChatMessage(role="user", content="Time?")], model=GEMINI, tools=[BARE])
    assert response._provider_extra[EXTRA_KEY].signatures == (("call_0", "c2ln"),)

    turn = ChatMessage(role="assistant", content="", tool_calls=response.tool_calls)
    turn._provider_extra = response._provider_extra
    messages = [ChatMessage(role="user", content="Time?"), turn,
                ChatMessage(role="tool", content="09:00", tool_call_id="call_0")]
    await provider.chat(messages, model=GEMINI, tools=[BARE])
    replayed = next(m for m in gateway.sent[1]["messages"] if m["role"] == "assistant")
    assert replayed["tool_calls"][0]["extra_content"] == SIGNATURE
    assert "thinking_blocks" not in replayed


@pytest.mark.asyncio
async def test_a_streamed_signature_is_kept():
    gateway = _Gateway(_stream({"tool_calls": [
        {"index": 0, "id": "call_0", "type": "function", "function": {"name": "now", "arguments": "{}"},
         "extra_content": SIGNATURE},
    ]}))
    response = await gateway.provider().chat_stream_with_tools(
        [ChatMessage(role="user", content="Time?")], tools=[BARE], model=GEMINI
    )
    assert response._provider_extra[EXTRA_KEY].signatures == (("call_0", "c2ln"),)


@pytest.mark.asyncio
async def test_turns_reusing_a_call_id_each_get_their_own_signature():
    gateway = _Gateway(_ok())
    await gateway.provider().chat(_round_of_two_turns(_kept(signature="Zmlyc3Q="), _kept(signature="c2Vjb25k")),
                                  model=GEMINI, tools=[BARE, DUE])
    turns = [m for m in gateway.sent[0]["messages"] if m["role"] == "assistant"]
    assert [t["tool_calls"][0]["extra_content"]["google"]["thought_signature"] for t in turns] == [
        "Zmlyc3Q=", "c2Vjb25k"
    ]


@pytest.mark.asyncio
async def test_a_signature_from_another_model_is_not_sent():
    gateway = _Gateway(_ok())
    await gateway.provider().chat(_round_of_two_turns(None, _kept(model="gemini-4-flash")), model=GEMINI, tools=[DUE])
    assert all("extra_content" not in c for m in gateway.sent[0]["messages"] for c in m.get("tool_calls") or [])


@pytest.mark.asyncio
async def test_a_refused_signature_is_retried_once_without_it():
    refusal = httpx.Response(502, json={"error": {"type": "provider_error", "message": "Corrupted thought signature.",
                                                  "retryable": False}})
    gateway = _Gateway(refusal, _ok())
    messages = _round_of_two_turns(None, _kept())
    await gateway.provider().chat(messages, model=GEMINI, tools=[DUE])
    first, retry = ([m for m in sent["messages"] if m["role"] == "assistant"][-1] for sent in gateway.sent)
    assert first["tool_calls"][0]["extra_content"] == SIGNATURE
    assert "extra_content" not in retry["tool_calls"][0]
    assert messages[3]._provider_extra[EXTRA_KEY].signatures_refused


@pytest.mark.asyncio
async def test_a_signature_is_sent_with_thinking_off():
    gateway = _Gateway(_ok())
    await gateway.provider().chat(_round_of_two_turns(None, _kept()), model=GEMINI, tools=[DUE],
                                  disable_thinking=True)
    replayed = [m for m in gateway.sent[0]["messages"] if m["role"] == "assistant"][-1]
    assert replayed["tool_calls"][0]["extra_content"] == SIGNATURE


@pytest.mark.asyncio
async def test_a_400_naming_the_signature_is_retried_once_without_it():
    refusal = httpx.Response(400, json={"error": {"message": "Corrupted thought signature."}})
    gateway = _Gateway(refusal, _ok())
    messages = _round_of_two_turns(None, _kept())
    await gateway.provider().chat(messages, model=GEMINI, tools=[DUE])
    retry = [m for m in gateway.sent[1]["messages"] if m["role"] == "assistant"][-1]
    assert "extra_content" not in retry["tool_calls"][0]
    assert messages[3]._provider_extra[EXTRA_KEY].signatures_refused


@pytest.mark.asyncio
async def test_streamed_tool_results_carry_their_tool_name():
    gateway = _Gateway(_stream({"content": "Done."}))
    await gateway.provider().chat_stream_with_tools(_round_of_two_turns(), tools=[BARE, DUE], model=GEMINI)
    assert [m["name"] for m in gateway.sent[0]["messages"] if m["role"] == "tool"] == ["now", "schedule"]


@pytest.mark.asyncio
async def test_an_alias_whose_listing_fails_keeps_its_schemas():
    gateway = _Gateway(_ok())
    listing = gateway.__call__

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(500, json={"error": {"message": "down"}})
        return listing(request)

    transport = httpx.MockTransport(handler)
    client = _gateway_http_client(XtmJwtAuth(_normalize_url(GATEWAY_URL)), None, transport=transport)
    provider = FiligranGatewayProvider(api_key=GATEWAY_URL, http_client=client)
    await provider.chat([ChatMessage(role="user", content="Plan it")], model="default", tools=[DUE])
    assert gateway.sent[0]["tools"][0]["function"]["parameters"] == DUE.input_schema


# --- signatures outlive what is not about them (#15) -------------------------


def _signature_sent(body: dict) -> bool:
    return any("extra_content" in c for m in body["messages"] for c in m.get("tool_calls") or [])


@pytest.mark.asyncio
async def test_a_signature_survives_the_overflow_drop(monkeypatch):
    gateway = _Gateway(_ok())
    provider = gateway.provider()
    monkeypatch.setattr(provider, "_resolve_context_window", lambda model: 1000)
    messages = _round_of_two_turns(None, _kept(signature="s" * 8000))
    await provider.chat(messages, model=GEMINI, tools=[DUE])
    assert _signature_sent(gateway.sent[0])
    assert not messages[3]._provider_extra[EXTRA_KEY].signatures_refused


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refusal",
    [
        httpx.Response(502, json={"error": {"type": "provider_error", "message": "upstream returned status 400",
                                            "retryable": False}}),
        httpx.Response(413, json={"error": {"message": "request too large"}}),
        httpx.Response(400, json={"error": {"message": "prompt is too long: 9 tokens > 8 maximum"}}),
    ],
)
async def test_a_refusal_not_naming_the_signature_is_not_retried_without_it(refusal):
    gateway = _Gateway(refusal)
    messages = _round_of_two_turns(None, _kept())
    with pytest.raises(openai.APIStatusError):
        await gateway.provider().chat(messages, model=GEMINI, tools=[DUE])
    # Nothing else was replayed: a retry would have sent the same request again.
    assert len(gateway.sent) == 1
    assert not messages[3]._provider_extra[EXTRA_KEY].signatures_refused


@pytest.mark.asyncio
async def test_a_signature_refused_again_without_it_was_not_the_cause():
    named = httpx.Response(400, json={"error": {"message": "Corrupted thought signature."}})
    other = httpx.Response(400, json={"error": {"message": "bad tool schema"}})
    gateway = _Gateway(named, other)
    messages = _round_of_two_turns(None, _kept())
    with pytest.raises(Exception, match="bad tool schema"):
        await gateway.provider().chat(messages, model=GEMINI, tools=[DUE])
    assert [_signature_sent(body) for body in gateway.sent] == [True, False]
    assert not messages[3]._provider_extra[EXTRA_KEY].signatures_refused
