"""Stream-establishment retry for streaming-with-tools paths (CORE-3).

Non-streaming ``chat()`` calls have always been wrapped with
``retry_llm_call`` — but the streaming-with-tools paths (the primary
interactive chat UX) used to open their streams directly with no retry,
so a transient 429/5xx when OPENING the stream failed the whole agentic
loop iteration immediately.

These tests pin the new contract:

* a retryable error while establishing the stream (or before anything has
  been delivered to the caller) is retried up to ``LLM_MAX_RETRIES`` times;
* a non-retryable error fails immediately;
* an error AFTER the first chunk has been emitted to the caller is NOT
  retried (replaying the stream would duplicate output).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anthropic
import httpx
import openai
import pytest

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import ChatMessage
from app.llm.gemini_provider import GeminiProvider
from app.llm.openai_compat import (
    OpenAICompatibleProvider,
    consume_openai_stream_with_tools,
)


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    """Skip the exponential back-off sleeps so retry tests run instantly."""

    async def _no_sleep(_):
        return None

    monkeypatch.setattr("app.llm.base.asyncio.sleep", _no_sleep)


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


class _ExplodingAsyncIterator:
    """Async iterator that yields its events, then raises *exc*."""

    def __init__(self, events: list, exc: Exception):
        self._events = list(events)
        self._exc = exc

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._events:
            return self._events.pop(0)
        raise self._exc


class _StreamCtx:
    """The stream ``client.messages.create(stream=True)`` returns; a failed
    establishment is raised by that awaited call itself."""

    def __init__(self, iterator):
        self._iterator = iterator

    async def __aenter__(self):
        return self._iterator

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _openai_conn_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(
        request=httpx.Request("POST", "https://api.test/v1/chat/completions")
    )


def _anthropic_conn_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.test/v1/messages")
    )


def _openai_text_chunk(text: str):
    delta = SimpleNamespace(content=text, tool_calls=None, reasoning_content=None)
    choice = SimpleNamespace(delta=delta, finish_reason=None)
    return SimpleNamespace(choices=[choice], usage=None)


# ---------------------------------------------------------------------------
# OpenAI-compatible streaming-with-tools (shared by OpenAI, OpenRouter,
# Ollama, Mistral, Azure OpenAI, LiteLLM, Custom OpenAI)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_stream_open_retries_then_succeeds():
    """Two retryable failures opening the stream, then the full stream flows."""
    chunks = [_openai_text_chunk("Hi "), _openai_text_chunk("there")]
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        side_effect=[
            _openai_conn_error(),
            _openai_conn_error(),
            _AsyncEventIterator(chunks),
        ]
    )

    tokens: list[str] = []

    async def _on_token(t: str) -> None:
        tokens.append(t)

    resp = await consume_openai_stream_with_tools(
        client,
        model="gpt-4o",
        oai_messages=[{"role": "user", "content": "hi"}],
        on_token=_on_token,
    )

    assert resp.content == "Hi there"
    assert tokens == ["Hi ", "there"]
    assert client.chat.completions.create.call_count == 3


@pytest.mark.asyncio
async def test_openai_stream_open_non_retryable_fails_immediately():
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=ValueError("bad request"))

    with pytest.raises(ValueError):
        await consume_openai_stream_with_tools(
            client,
            model="gpt-4o",
            oai_messages=[{"role": "user", "content": "hi"}],
        )

    assert client.chat.completions.create.call_count == 1


@pytest.mark.asyncio
async def test_openai_stream_error_after_first_emitted_chunk_not_retried():
    """Once a token has been delivered to the caller, errors must propagate."""
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=_ExplodingAsyncIterator(
            [_openai_text_chunk("Hi ")], _openai_conn_error()
        )
    )

    tokens: list[str] = []

    async def _on_token(t: str) -> None:
        tokens.append(t)

    with pytest.raises(openai.APIConnectionError):
        await consume_openai_stream_with_tools(
            client,
            model="gpt-4o",
            oai_messages=[{"role": "user", "content": "hi"}],
            on_token=_on_token,
        )

    # The token reached the caller exactly once — no duplicated output.
    assert tokens == ["Hi "]
    assert client.chat.completions.create.call_count == 1


@pytest.mark.asyncio
async def test_openai_stream_error_before_first_emitted_chunk_is_retried():
    """A retryable mid-stream error BEFORE any delivery is still retried."""
    chunks = [_openai_text_chunk("ok")]
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        side_effect=[
            _ExplodingAsyncIterator([], _openai_conn_error()),
            _AsyncEventIterator(chunks),
        ]
    )

    tokens: list[str] = []

    async def _on_token(t: str) -> None:
        tokens.append(t)

    resp = await consume_openai_stream_with_tools(
        client,
        model="gpt-4o",
        oai_messages=[{"role": "user", "content": "hi"}],
        on_token=_on_token,
    )

    assert resp.content == "ok"
    assert tokens == ["ok"]
    assert client.chat.completions.create.call_count == 2


@pytest.mark.asyncio
async def test_openai_compat_plain_chat_stream_open_retries_then_succeeds():
    """Plain (no-tools) chat_stream retries transient errors on establishment."""
    provider = OpenAICompatibleProvider(api_key="test-key")
    chunks = [_openai_text_chunk("Hello"), _openai_text_chunk(" world")]
    provider.client = MagicMock()
    provider.client.chat.completions.create = AsyncMock(
        side_effect=[
            _openai_conn_error(),
            _openai_conn_error(),
            _AsyncEventIterator(chunks),
        ]
    )

    collected = [
        chunk
        async for chunk in provider.chat_stream(
            [ChatMessage(role="user", content="hi")], model="gpt-4o"
        )
    ]

    assert "".join(collected) == "Hello world"
    assert provider.client.chat.completions.create.call_count == 3


# ---------------------------------------------------------------------------
# Anthropic streaming-with-tools
# ---------------------------------------------------------------------------


def _anthropic_success_events() -> list:
    return [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=10,
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
            usage=SimpleNamespace(output_tokens=5),
        ),
    ]


@pytest.mark.asyncio
async def test_anthropic_stream_open_retries_then_succeeds():
    provider = AnthropicProvider(api_key="test-key")
    provider.client = MagicMock()
    provider.client.messages.create = AsyncMock(
        side_effect=[
            _anthropic_conn_error(),
            _anthropic_conn_error(),
            _StreamCtx(_AsyncEventIterator(_anthropic_success_events())),
        ]
    )

    resp = await provider.chat_stream_with_tools(
        messages=[ChatMessage(role="user", content="hi")],
        model="claude-sonnet-4-6",
    )

    assert resp.content == "Hello world"
    assert provider.client.messages.create.call_count == 3


@pytest.mark.asyncio
async def test_anthropic_stream_open_non_retryable_fails_immediately():
    provider = AnthropicProvider(api_key="test-key")
    provider.client = MagicMock()
    provider.client.messages.create = AsyncMock(side_effect=[ValueError("bad request")])

    with pytest.raises(ValueError):
        await provider.chat_stream_with_tools(
            messages=[ChatMessage(role="user", content="hi")],
            model="claude-sonnet-4-6",
        )

    assert provider.client.messages.create.call_count == 1


@pytest.mark.asyncio
async def test_anthropic_stream_error_after_first_emitted_chunk_not_retried():
    provider = AnthropicProvider(api_key="test-key")
    events = [
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="Hi "),
        ),
    ]
    provider.client = MagicMock()
    provider.client.messages.create = AsyncMock(
        return_value=_StreamCtx(
            _ExplodingAsyncIterator(events, _anthropic_conn_error())
        )
    )

    tokens: list[str] = []

    async def _on_token(t: str) -> None:
        tokens.append(t)

    with pytest.raises(anthropic.APIConnectionError):
        await provider.chat_stream_with_tools(
            messages=[ChatMessage(role="user", content="hi")],
            model="claude-sonnet-4-6",
            on_token=_on_token,
        )

    # The token reached the caller exactly once — no duplicated output.
    assert tokens == ["Hi "]
    assert provider.client.messages.create.call_count == 1


@pytest.mark.asyncio
async def test_anthropic_plain_chat_stream_open_retries_then_succeeds():
    """Plain (no-tools) chat_stream retries transient errors on establishment."""
    provider = AnthropicProvider(api_key="test-key")
    provider.client = MagicMock()
    provider.client.messages.create = AsyncMock(
        side_effect=[
            _anthropic_conn_error(),
            _anthropic_conn_error(),
            _StreamCtx(_AsyncEventIterator(_anthropic_success_events())),
        ]
    )

    collected = [
        chunk
        async for chunk in provider.chat_stream(
            [ChatMessage(role="user", content="hi")], model="claude-sonnet-4-6"
        )
    ]

    assert "".join(collected) == "Hello world"
    assert provider.client.messages.create.call_count == 3


# ---------------------------------------------------------------------------
# Gemini streaming-with-tools
# ---------------------------------------------------------------------------


def _gemini_text_chunk(text: str):
    part = SimpleNamespace(text=text, thought=False, function_call=None)
    candidate = SimpleNamespace(
        finish_reason=None, content=SimpleNamespace(parts=[part])
    )
    return SimpleNamespace(candidates=[candidate], usage_metadata=None)


@pytest.mark.asyncio
async def test_gemini_stream_open_retries_then_succeeds():
    provider = GeminiProvider(api_key="test-key")
    chunks = [_gemini_text_chunk("Hello"), _gemini_text_chunk(" world")]
    provider.client = MagicMock()
    provider.client.aio.models.generate_content_stream = AsyncMock(
        side_effect=[
            ConnectionError("boom"),
            ConnectionError("boom"),
            _AsyncEventIterator(chunks),
        ]
    )

    resp = await provider.chat_stream_with_tools(
        messages=[ChatMessage(role="user", content="hi")],
        model="gemini-2.5-flash",
    )

    assert resp.content == "Hello world"
    assert provider.client.aio.models.generate_content_stream.call_count == 3


@pytest.mark.asyncio
async def test_gemini_stream_open_non_retryable_fails_immediately():
    provider = GeminiProvider(api_key="test-key")
    provider.client = MagicMock()
    provider.client.aio.models.generate_content_stream = AsyncMock(
        side_effect=ValueError("bad request")
    )

    with pytest.raises(ValueError):
        await provider.chat_stream_with_tools(
            messages=[ChatMessage(role="user", content="hi")],
            model="gemini-2.5-flash",
        )

    assert provider.client.aio.models.generate_content_stream.call_count == 1


@pytest.mark.asyncio
async def test_gemini_stream_error_after_first_emitted_chunk_not_retried():
    provider = GeminiProvider(api_key="test-key")
    provider.client = MagicMock()
    provider.client.aio.models.generate_content_stream = AsyncMock(
        return_value=_ExplodingAsyncIterator(
            [_gemini_text_chunk("Hi ")], ConnectionError("boom")
        )
    )

    tokens: list[str] = []

    async def _on_token(t: str) -> None:
        tokens.append(t)

    with pytest.raises(ConnectionError):
        await provider.chat_stream_with_tools(
            messages=[ChatMessage(role="user", content="hi")],
            model="gemini-2.5-flash",
            on_token=_on_token,
        )

    # The token reached the caller exactly once — no duplicated output.
    assert tokens == ["Hi "]
    assert provider.client.aio.models.generate_content_stream.call_count == 1
