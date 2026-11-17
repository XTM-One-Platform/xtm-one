"""Every provider reports an answer cut at its output budget as ``"max_tokens"``.

The callers that refuse a cut answer - conversation compaction and memory
reconciliation, memory consolidation, the evaluation judge -
and the agent loop's continuation all key on that one canonical
``ChatResponse.stop_reason`` value, so a provider spelling its truncation
differently silently bypasses every one of them.

Gemini did: the SDK hands back a ``types.FinishReason`` member, a
``(str, Enum)`` whose ``str()`` is ``"FinishReason.MAX_TOKENS"``, and the
provider compared ``str(finish_reason)`` with ``"MAX_TOKENS"``. A cut Gemini
answer therefore read as complete on all three paths. The Gemini cases below
parse real payloads through the SDK's own models instead of hand-built stubs,
which is how the mismatch went unnoticed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from google.genai import types

from app.llm import (
    azure_openai_provider,
    custom_openai_provider,
    gateway_provider,
    litellm_provider,
    mistral_provider,
    ollama_provider,
    openrouter_provider,
    xai_provider,
)
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import ChatMessage
from app.llm.bedrock_provider import BedrockProvider
from app.llm.gemini_provider import GeminiProvider
from app.llm.openai_compat import OpenAICompatibleProvider
from app.llm.openai_provider import OpenAIProvider

_MESSAGES = [ChatMessage(role="user", content="compact this")]
_CUT_JSON = '{"directives": ["use the subregion filter", "curr'


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


def _gemini_payload(finish_reason: str | None, text: str = _CUT_JSON) -> dict:
    candidate: dict[str, Any] = {
        "content": {"role": "model", "parts": [{"text": text}]}
    }
    if finish_reason is not None:
        candidate["finish_reason"] = finish_reason
    return {"candidates": [candidate]}


def _gemini_response(finish_reason: str | None, text: str = _CUT_JSON):
    return types.GenerateContentResponse.model_validate(
        _gemini_payload(finish_reason, text)
    )


class _GeminiStream:
    def __init__(self, chunks: list) -> None:
        self._chunks = chunks

    def __aiter__(self):
        self._iter = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None


def _gemini_provider(*, response=None, chunks=None) -> GeminiProvider:
    provider = GeminiProvider(api_key="dummy")

    class _Models:
        async def generate_content(self, **_kwargs):
            return response

        async def generate_content_stream(self, **_kwargs):
            return _GeminiStream(chunks or [])

    provider.client = SimpleNamespace(aio=SimpleNamespace(models=_Models()))
    return provider


def _gemini_chunks(finish_reason: str) -> list:
    # The text arrives first, the finish reason on the last chunk - the shape
    # of a real stream.
    return [
        _gemini_response(None, text='{"directives": ["use the subregion'),
        _gemini_response(finish_reason, text=' filter", "curr'),
    ]


def test_the_sdk_finish_reason_is_not_its_own_name_as_a_string():
    """The premise: ``str()`` of the SDK member is not ``"MAX_TOKENS"``, so no
    comparison may go through ``str()``."""
    member = _gemini_response("MAX_TOKENS").candidates[0].finish_reason

    assert isinstance(member, types.FinishReason)
    assert str(member) != "MAX_TOKENS"


@pytest.mark.parametrize(
    ("finish_reason", "expected"),
    [
        (types.FinishReason.MAX_TOKENS, True),
        ("MAX_TOKENS", True),
        (2, True),
        ("2", True),
        (types.FinishReason.STOP, False),
        (types.FinishReason.SAFETY, False),
        ("STOP", False),
        (None, False),
        (SimpleNamespace(), False),
    ],
)
def test_finish_reason_is_max_tokens(finish_reason: object, expected: bool):
    from app.llm.gemini_provider import _finish_reason_is_max_tokens

    assert _finish_reason_is_max_tokens(finish_reason) is expected


@pytest.mark.parametrize(
    ("finish_reason", "expected"), [("MAX_TOKENS", "max_tokens"), ("STOP", "end_turn")]
)
def test_gemini_chat_maps_the_sdk_finish_reason(finish_reason: str, expected: str):
    provider = _gemini_provider(response=_gemini_response(finish_reason))

    response = asyncio.run(provider.chat(_MESSAGES))

    assert response.stop_reason == expected


@pytest.mark.parametrize(
    ("finish_reason", "expected"), [("MAX_TOKENS", "max_tokens"), ("STOP", "end_turn")]
)
def test_gemini_chat_stream_maps_the_sdk_finish_reason(finish_reason: str, expected: str):
    provider = _gemini_provider(chunks=_gemini_chunks(finish_reason))

    async def _drain() -> str:
        return "".join([token async for token in provider.chat_stream(_MESSAGES)])

    text = asyncio.run(_drain())

    assert text == _CUT_JSON
    assert provider.last_stop_reason == expected


@pytest.mark.parametrize(
    ("finish_reason", "expected"), [("MAX_TOKENS", "max_tokens"), ("STOP", "end_turn")]
)
def test_gemini_chat_stream_with_tools_maps_the_sdk_finish_reason(
    finish_reason: str, expected: str
):
    provider = _gemini_provider(chunks=_gemini_chunks(finish_reason))

    response = asyncio.run(provider.chat_stream_with_tools(_MESSAGES))

    assert response.content == _CUT_JSON
    assert response.stop_reason == expected


# ---------------------------------------------------------------------------
# The other providers already map their truncation signal: pinned here so the
# contract every guard relies on is held in one place.
# ---------------------------------------------------------------------------


def _openai_completion(finish_reason: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=_CUT_JSON, tool_calls=None, reasoning_content=None
                ),
                finish_reason=finish_reason,
            )
        ],
        model="gpt-test",
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )


def _with_openai_client(provider, finish_reason: str):
    class _Completions:
        async def create(self, **_kwargs):
            return _openai_completion(finish_reason)

    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    return provider


@pytest.mark.parametrize(
    ("finish_reason", "expected"), [("length", "max_tokens"), ("stop", "end_turn")]
)
def test_openai_chat_maps_length(finish_reason: str, expected: str):
    provider = _with_openai_client(OpenAIProvider(api_key="dummy"), finish_reason)

    response = asyncio.run(provider.chat(_MESSAGES, model="gpt-5.5"))

    assert response.stop_reason == expected


@pytest.mark.parametrize(
    ("finish_reason", "expected"), [("length", "max_tokens"), ("stop", "end_turn")]
)
def test_openai_compatible_chat_maps_length(finish_reason: str, expected: str):
    provider = _with_openai_client(
        OpenAICompatibleProvider(api_key="dummy"), finish_reason
    )

    response = asyncio.run(provider.chat(_MESSAGES, model="some-model"))

    assert response.stop_reason == expected


def test_every_openai_compatible_provider_shares_the_mapped_chat():
    """Azure, OpenRouter, Ollama, Mistral, LiteLLM, custom endpoints, xAI and
    the gateway reach ``chat()`` through :class:`OpenAICompatibleProvider`, so
    the case above covers them. A subclass that grows its own ``chat()`` must
    map ``length`` itself - this fails first so it cannot do so silently."""
    subclasses = [
        azure_openai_provider.AzureOpenAIProvider,
        custom_openai_provider.CustomOpenAIProvider,
        gateway_provider.FiligranGatewayProvider,
        litellm_provider.LiteLLMProvider,
        mistral_provider.MistralProvider,
        ollama_provider.OllamaProvider,
        openrouter_provider.OpenRouterProvider,
        xai_provider.XAIProvider,
    ]

    for cls in subclasses:
        assert issubclass(cls, OpenAICompatibleProvider), cls
        assert cls.chat is OpenAICompatibleProvider.chat, cls


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [("max_tokens", "max_tokens"), ("end_turn", "end_turn"), ("tool_use", "tool_use")],
)
def test_anthropic_chat_maps_max_tokens(stop_reason: str, expected: str):
    provider = AnthropicProvider(api_key="test-key")
    provider.client = MagicMock()

    async def _create(**_kwargs):
        return SimpleNamespace(
            content=[],
            stop_reason=stop_reason,
            model="claude-sonnet-4-5",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )

    provider.client.messages.create = _create

    response = asyncio.run(provider.chat(_MESSAGES, model="claude-sonnet-4-5"))

    assert response.stop_reason == expected


@pytest.mark.parametrize(
    ("stop_reason", "expected"), [("max_tokens", "max_tokens"), ("end_turn", "end_turn")]
)
def test_bedrock_chat_maps_max_tokens(stop_reason: str, expected: str):
    provider = BedrockProvider("us-east-1")

    class _Client:
        def converse(self, **_kwargs):
            return {
                "output": {"message": {"content": [{"text": _CUT_JSON}]}},
                "stopReason": stop_reason,
                "usage": {"inputTokens": 1, "outputTokens": 1},
            }

    provider._client = _Client()

    response = asyncio.run(
        provider.chat(_MESSAGES, model="anthropic.claude-sonnet-4-5")
    )

    assert response.stop_reason == expected
