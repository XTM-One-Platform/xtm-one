"""Anthropic provider error paths, driven through the real SDK.

Every exception here is raised by the SDK's own public client over an
in-memory ``httpx.MockTransport``: no socket is opened, no key is read.

* The ``thinking.type.disabled`` refusal is read off the API's
  structured error message only, like the thinking replay's refusal checks:
  a proxy body echoing the request must never get a model remembered
  process-wide as refusing ``disabled``.
* An overload is retried while nothing reached the consumer: an HTTP 529 at
  request time, and an ``overloaded_error`` event mid-stream, which the SDK
  raises with the stream's own 200 response.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import anthropic
import httpx
import pytest

from app.llm.anthropic_provider import _MODELS_REFUSING_DISABLED_THINKING, AnthropicProvider
from app.llm.base import ChatMessage

_BASE_URL = "https://api.anthropic.com"
MODEL = "claude-sonnet-5"
# A 5.x id the capability rules do not declare: its refusal is learned.
REFUSING_MODEL = "claude-sonnet-5-1"
_REFUSAL = (
    '"thinking.type.disabled" is not supported for this model. Use '
    '"thinking.type.adaptive" and "output_config.effort" to control thinking behavior.'
)
# A utility call's prompt quoting the parameter, as a compaction of a
# conversation about this very error would.
_PROMPT = "Summarise: the user asked why the API says thinking.type.disabled is not supported."


@pytest.fixture(autouse=True)
def _clean_memo():
    _MODELS_REFUSING_DISABLED_THINKING.clear()
    yield
    _MODELS_REFUSING_DISABLED_THINKING.clear()


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    async def _no_sleep(_):
        return None

    monkeypatch.setattr("app.llm.base.asyncio.sleep", _no_sleep)


Answer = httpx.Response | Callable[[dict], httpx.Response]


class _Server:
    """Answers each request with the next scripted response (a callable is
    handed the request it answers) and keeps every request body it got."""

    def __init__(self, *answers: Answer) -> None:
        self._answers = list(answers)
        self.sent: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.sent.append(body)
        answer = self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]
        return answer(body) if callable(answer) else answer


def _provider(server: _Server) -> AnthropicProvider:
    provider = AnthropicProvider(api_key="test-key")
    provider.client = anthropic.AsyncAnthropic(
        api_key="test-key",
        base_url=_BASE_URL,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(server)),
    )
    return provider


def _api_error(status: int, error_type: str, message: str) -> httpx.Response:
    return httpx.Response(
        status, json={"type": "error", "error": {"type": error_type, "message": message}}
    )


def _message(text: str, model: str = MODEL) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 3, "output_tokens": 2},
        },
    )


def _sse(*events: dict) -> httpx.Response:
    body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


_START = {
    "type": "message_start",
    "message": {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 3, "output_tokens": 1},
    },
}


def _block_start(block: dict) -> dict:
    return {"type": "content_block_start", "index": 0, "content_block": block}


def _delta(delta: dict) -> dict:
    return {"type": "content_block_delta", "index": 0, "delta": delta}


def _error_event(error_type: str, message: str) -> dict:
    return {"type": "error", "error": {"type": error_type, "message": message}}


_TEXT_START = _block_start({"type": "text", "text": ""})
_OVERLOADED = _error_event("overloaded_error", "Overloaded")
_DONE = _sse(
    _START,
    _TEXT_START,
    _delta({"type": "text_delta", "text": "Done."}),
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 2}},
    {"type": "message_stop"},
)


async def _utility_call(provider: AnthropicProvider):
    return await provider.chat(
        messages=[ChatMessage(role="user", content=_PROMPT)],
        model=REFUSING_MODEL,
        max_tokens=500,
        disable_thinking=True,
    )


# ---------------------------------------------------------------------------
# The ``thinking.type.disabled`` refusal on the structured message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_api_refusal_is_still_learned_and_retried_in_the_adaptive_form():
    server = _Server(_api_error(400, "invalid_request_error", _REFUSAL), _message("ok", REFUSING_MODEL))
    provider = _provider(server)
    async with provider.client:
        result = await _utility_call(provider)

    assert result.content == "ok"
    assert [request["thinking"] for request in server.sent] == [
        {"type": "disabled"},
        {"type": "adaptive"},
    ]
    assert {REFUSING_MODEL} == _MODELS_REFUSING_DISABLED_THINKING


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "echo",
    [
        pytest.param(lambda sent: httpx.Response(400, json={"detail": "Rejected by policy", "request": sent}), id="json"),
        pytest.param(
            lambda sent: httpx.Response(
                400,
                text=f"<html><body><pre>{json.dumps(sent)}</pre></body></html>",
                headers={"content-type": "text/html"},
            ),
            id="html",
        ),
    ],
)
async def test_a_proxy_400_echoing_the_request_never_teaches_the_model_to_refuse_disabled(echo):
    """The request quotes ``thinking.type.disabled`` and a proxy answers 400
    with it echoed back and no ``error.message``: the SDK's rendering of that
    body names the parameter, the API's structured message is absent. The 400
    propagates on the first request and no model is remembered."""
    server = _Server(echo)
    provider = _provider(server)
    async with provider.client:
        with pytest.raises(anthropic.BadRequestError) as info:
            await _utility_call(provider)

    assert "thinking.type.disabled" in str(info.value)
    assert len(server.sent) == 1
    assert not _MODELS_REFUSING_DISABLED_THINKING


# ---------------------------------------------------------------------------
# Overloads are retried while nothing reached the consumer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_attempt",
    [
        pytest.param(_sse(_START, _OVERLOADED), id="before-any-content"),
        pytest.param(
            _sse(
                _START,
                _block_start({"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {}}),
                _delta({"type": "input_json_delta", "partial_json": '{"q": '}),
                _OVERLOADED,
            ),
            id="after-a-tool-call-delta",
        ),
        pytest.param(
            _sse(_START, _block_start({"type": "thinking", "thinking": "", "signature": ""}),
                 _delta({"type": "thinking_delta", "thinking": "Let me"}), _OVERLOADED),
            id="after-a-thinking-delta-nobody-reads",
        ),
        pytest.param(_api_error(529, "overloaded_error", "Overloaded"), id="http-529-at-open"),
    ],
)
async def test_an_overload_before_anything_reached_the_consumer_is_retried(first_attempt):
    """Tool calls are only handed over in the final response and a thinking
    delta is only forwarded to an ``on_thinking`` consumer, so none of these
    attempts delivered anything: the request is sent again."""
    server = _Server(first_attempt, _DONE)
    provider = _provider(server)
    tokens: list[str] = []

    async def on_token(text: str) -> None:
        tokens.append(text)

    async with provider.client:
        response = await provider.chat_stream_with_tools(
            messages=[ChatMessage(role="user", content="hi")], model=MODEL, on_token=on_token
        )

    assert response.content == "Done."
    assert response.tool_calls is None
    assert tokens == ["Done."]
    assert len(server.sent) == 2
    assert server.sent[0] == server.sent[1]


@pytest.mark.asyncio
async def test_an_overload_after_answer_text_reached_the_consumer_fails_as_before():
    server = _Server(
        _sse(_START, _TEXT_START, _delta({"type": "text_delta", "text": "Hel"}), _OVERLOADED), _DONE
    )
    provider = _provider(server)
    tokens: list[str] = []

    async def on_token(text: str) -> None:
        tokens.append(text)

    async with provider.client:
        with pytest.raises(anthropic.APIStatusError) as info:
            await provider.chat_stream_with_tools(
                messages=[ChatMessage(role="user", content="hi")], model=MODEL, on_token=on_token
            )

    assert info.value.type == "overloaded_error"
    assert tokens == ["Hel"]
    assert len(server.sent) == 1


@pytest.mark.asyncio
async def test_an_overload_after_a_forwarded_thinking_delta_fails_as_before():
    server = _Server(
        _sse(
            _START,
            _block_start({"type": "thinking", "thinking": "", "signature": ""}),
            _delta({"type": "thinking_delta", "thinking": "Let me"}),
            _OVERLOADED,
        ),
        _DONE,
    )
    provider = _provider(server)
    thoughts: list[str] = []

    async def on_thinking(text: str) -> None:
        thoughts.append(text)

    async with provider.client:
        with pytest.raises(anthropic.APIStatusError):
            await provider.chat_stream_with_tools(
                messages=[ChatMessage(role="user", content="hi")], model=MODEL, on_thinking=on_thinking
            )

    assert thoughts == ["Let me"]
    assert len(server.sent) == 1


@pytest.mark.asyncio
async def test_another_error_mid_stream_is_not_retried():
    server = _Server(_sse(_START, _error_event("api_error", "Internal error")), _DONE)
    provider = _provider(server)
    async with provider.client:
        with pytest.raises(anthropic.APIStatusError) as info:
            await provider.chat_stream_with_tools(messages=[ChatMessage(role="user", content="hi")], model=MODEL)

    assert info.value.status_code == 200
    assert len(server.sent) == 1


@pytest.mark.asyncio
async def test_chat_retries_an_http_529():
    server = _Server(_api_error(529, "overloaded_error", "Overloaded"), _message("ok"))
    provider = _provider(server)
    async with provider.client:
        result = await provider.chat(messages=[ChatMessage(role="user", content="hi")], model=MODEL)

    assert result.content == "ok"
    assert len(server.sent) == 2


@pytest.mark.asyncio
async def test_chat_stream_still_fails_on_an_overload_mid_stream():
    """``chat_stream`` hands each chunk straight to its caller from inside the
    stream, outside the retry: a mid-stream overload fails as before."""
    server = _Server(_sse(_START, _OVERLOADED), _DONE)
    provider = _provider(server)
    async with provider.client:
        with pytest.raises(anthropic.APIStatusError):
            _ = [chunk async for chunk in provider.chat_stream(messages=[ChatMessage(role="user", content="hi")], model=MODEL)]

    assert len(server.sent) == 1
