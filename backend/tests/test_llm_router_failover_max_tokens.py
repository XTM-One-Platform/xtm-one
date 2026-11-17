"""Regression tests for FailoverLLMProvider max_tokens clamping.

These tests assert that when the primary provider fails and the router
falls back, the fallback receives a ``max_tokens`` value clamped to the
fallback provider/model limits — and that the clamp logic does not
interfere with the unrelated fallback paths (no-limit, within-limit,
``max_tokens=None``, multiple fallbacks).
"""

from collections.abc import AsyncIterator

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.llm.router import FailoverLLMProvider


class _AlwaysFailPrimary(BaseLLMProvider):
    provider_name = "anthropic"

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        raise RuntimeError("primary max_tokens is too large")

    async def chat_stream(
        self, messages, model=None, temperature=0.7, max_tokens=4096, **kwargs
    ) -> AsyncIterator[str]:
        if False:
            yield ""
        raise RuntimeError("primary max_tokens is too large")

    async def chat_stream_with_tools(
        self, messages, tools=None, *, on_token=None, model=None, temperature=0.7, max_tokens=4096, **kwargs
    ) -> ChatResponse:
        raise RuntimeError("primary max_tokens is too large")

    async def embed(self, text, model=None):
        return []

    async def list_models(self):
        return []


class _RecordingFallback(BaseLLMProvider):
    provider_name = "openai"
    # Deliberately a fictional id absent from the metadata snapshot and the
    # model-family table, so these tests exercise the flat-default / override
    # clamp tier rather than a model's real (now correctly resolved) cap.
    default_model = "test-fallback-model"
    max_output_tokens = 16_384

    def __init__(self) -> None:
        self.chat_calls: list[dict] = []
        self.stream_calls: list[dict] = []
        self.stream_with_tools_calls: list[dict] = []

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        self.chat_calls.append({"model": model, "max_tokens": max_tokens})
        return ChatResponse(content="ok", model=model or self.default_model, usage={})

    async def chat_stream(
        self, messages, model=None, temperature=0.7, max_tokens=4096, **kwargs
    ) -> AsyncIterator[str]:
        self.stream_calls.append({"model": model, "max_tokens": max_tokens})
        yield "ok"

    async def chat_stream_with_tools(
        self, messages, tools=None, *, on_token=None, model=None, temperature=0.7, max_tokens=4096, **kwargs
    ) -> ChatResponse:
        self.stream_with_tools_calls.append({"model": model, "max_tokens": max_tokens})
        return ChatResponse(content="ok", model=model or self.default_model, usage={})

    async def embed(self, text, model=None):
        return []

    async def list_models(self):
        return []


class _FailingFallback(_RecordingFallback):
    provider_name = "openrouter"

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        self.chat_calls.append({"model": model, "max_tokens": max_tokens})
        raise RuntimeError("fallback provider unavailable")


@pytest.mark.asyncio
async def test_failover_chat_clamps_max_tokens_to_fallback_limit():
    provider = _RecordingFallback()
    failover = FailoverLLMProvider(primary=_AlwaysFailPrimary(), fallbacks=[provider])

    response = await failover.chat(
        [ChatMessage(role="user", content="Hello")], model="gpt-5-mini", max_tokens=128_000
    )

    assert response.content == "ok"
    assert provider.chat_calls == [{"model": None, "max_tokens": 16_384}]


@pytest.mark.asyncio
async def test_failover_chat_uses_fallback_model_override_limit():
    provider = _RecordingFallback()
    provider.max_output_tokens_overrides = {"test-fallback-model": 8_192}
    failover = FailoverLLMProvider(primary=_AlwaysFailPrimary(), fallbacks=[provider])

    await failover.chat(
        [ChatMessage(role="user", content="Hello")], model="claude-3-7-sonnet", max_tokens=128_000
    )

    assert provider.chat_calls == [{"model": None, "max_tokens": 8_192}]


@pytest.mark.asyncio
async def test_failover_stream_clamps_max_tokens_to_fallback_limit():
    provider = _RecordingFallback()
    failover = FailoverLLMProvider(primary=_AlwaysFailPrimary(), fallbacks=[provider])

    chunks = [
        chunk
        async for chunk in failover.chat_stream(
            [ChatMessage(role="user", content="Hello")],
            model="gpt-5-mini",
            max_tokens=128_000,
        )
    ]

    assert chunks == ["ok"]
    assert provider.stream_calls == [{"model": None, "max_tokens": 16_384}]


@pytest.mark.asyncio
async def test_failover_stream_with_tools_clamps_max_tokens():
    provider = _RecordingFallback()
    failover = FailoverLLMProvider(primary=_AlwaysFailPrimary(), fallbacks=[provider])

    response = await failover.chat_stream_with_tools(
        [ChatMessage(role="user", content="Hello")],
        model="gpt-5-mini",
        max_tokens=128_000,
        tools=[],
    )

    assert response.content == "ok"
    assert provider.stream_with_tools_calls == [{"model": None, "max_tokens": 16_384}]


@pytest.mark.asyncio
async def test_failover_chat_does_not_clamp_when_requested_tokens_within_limit():
    provider = _RecordingFallback()
    failover = FailoverLLMProvider(primary=_AlwaysFailPrimary(), fallbacks=[provider])

    await failover.chat(
        [ChatMessage(role="user", content="Hello")], model="gpt-5-mini", max_tokens=1024
    )

    assert provider.chat_calls == [{"model": None, "max_tokens": 1024}]


@pytest.mark.asyncio
async def test_failover_chat_passes_through_when_no_limit_configured():
    provider = _RecordingFallback()
    provider.max_output_tokens = 0
    provider.max_output_tokens_overrides = {}
    failover = FailoverLLMProvider(primary=_AlwaysFailPrimary(), fallbacks=[provider])

    await failover.chat(
        [ChatMessage(role="user", content="Hello")], model="gpt-5-mini", max_tokens=128_000
    )

    assert provider.chat_calls == [{"model": None, "max_tokens": 128_000}]


@pytest.mark.asyncio
async def test_failover_chat_with_none_max_tokens_does_not_raise():
    provider = _RecordingFallback()
    failover = FailoverLLMProvider(primary=_AlwaysFailPrimary(), fallbacks=[provider])

    await failover.chat(
        [ChatMessage(role="user", content="Hello")], model="gpt-5-mini", max_tokens=None
    )

    assert provider.chat_calls == [{"model": None, "max_tokens": None}]


@pytest.mark.asyncio
async def test_failover_clamps_each_fallback_independently():
    first_fallback = _FailingFallback()
    first_fallback.max_output_tokens = 4_096
    second_fallback = _RecordingFallback()
    second_fallback.max_output_tokens = 2_048
    failover = FailoverLLMProvider(
        primary=_AlwaysFailPrimary(),
        fallbacks=[first_fallback, second_fallback],
    )

    response = await failover.chat(
        [ChatMessage(role="user", content="Hello")], model="gpt-5-mini", max_tokens=128_000
    )

    assert response.content == "ok"
    assert first_fallback.chat_calls == [{"model": None, "max_tokens": 4_096}]
    assert second_fallback.chat_calls == [{"model": None, "max_tokens": 2_048}]


@pytest.mark.asyncio
async def test_failover_uses_provider_default_model_for_override_lookup():
    """When the failover path strips ``model`` (passes ``None`` to the fallback),
    the per-model override must still be resolved via ``provider.default_model``."""
    provider = _RecordingFallback()
    # ``default_model='test-fallback-model'`` (set on _RecordingFallback) — override should apply.
    provider.max_output_tokens_overrides = {"test-fallback-model": 4_096}
    failover = FailoverLLMProvider(primary=_AlwaysFailPrimary(), fallbacks=[provider])

    # Caller passes ``model=None`` at the chat-method level (e.g. when the
    # router has stripped the primary model and the agent did not pick one).
    await failover.chat(
        [ChatMessage(role="user", content="Hello")], model=None, max_tokens=128_000
    )

    # Override for ``default_model`` ("test-fallback-model") wins → clamp to 4_096
    # rather than the provider-level 16_384.
    assert provider.chat_calls == [{"model": None, "max_tokens": 4_096}]
