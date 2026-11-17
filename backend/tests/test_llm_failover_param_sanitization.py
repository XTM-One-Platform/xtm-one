import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.llm.router import FailoverLLMProvider


class _PrimaryFailingProvider(BaseLLMProvider):
    provider_name = "anthropic"

    def __init__(self):
        self.last_kwargs: dict[str, Any] | None = None

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        self.last_kwargs = kwargs
        raise RuntimeError("primary failure")

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, **kwargs) -> AsyncIterator[str]:
        self.last_kwargs = kwargs
        # Keep this as an async generator so ``async for`` failover paths are
        # exercised against this mock implementation.
        if False:
            yield ""
        raise RuntimeError("primary failure")

    async def chat_stream_with_tools(
        self,
        messages,
        tools=None,
        *,
        on_token=None,
        model=None,
        temperature=0.7,
        max_tokens=4096,
        **kwargs,
    ):
        self.last_kwargs = kwargs
        raise RuntimeError("primary failure")

    async def embed(self, text, model=None):
        return []

    async def list_models(self):
        return []


class _FallbackCapturingProvider(BaseLLMProvider):
    provider_name = "mock_fallback"

    def __init__(self):
        self.last_kwargs: dict[str, Any] | None = None
        # Capture the explicit named-argument values too — ``kwargs`` only
        # contains the residual ``**kwargs`` from the fallback's signature,
        # so ``temperature``/``max_tokens``/``tools`` would not appear there
        # even when they were forwarded by the failover layer. Tests that
        # care about those explicit args should read these fields.
        self.last_temperature: float | None = None
        self.last_max_tokens: int | None = None
        self.last_tools: list | None = None

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        self.last_kwargs = kwargs
        self.last_temperature = temperature
        self.last_max_tokens = max_tokens
        self.last_tools = tools
        return ChatResponse(content="ok", model="fallback", usage={})

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, **kwargs) -> AsyncIterator[str]:
        self.last_kwargs = kwargs
        self.last_temperature = temperature
        self.last_max_tokens = max_tokens
        yield "ok"

    async def chat_stream_with_tools(
        self,
        messages,
        tools=None,
        *,
        on_token=None,
        model=None,
        temperature=0.7,
        max_tokens=4096,
        **kwargs,
    ):
        self.last_kwargs = kwargs
        self.last_temperature = temperature
        self.last_max_tokens = max_tokens
        self.last_tools = tools
        return ChatResponse(content="ok", model="fallback", usage={})

    async def embed(self, text, model=None):
        return []

    async def list_models(self):
        return []


def test_failover_strips_provider_specific_kwargs_for_chat_stream_with_tools():
    primary = _PrimaryFailingProvider()
    fallback = _FallbackCapturingProvider()
    provider = FailoverLLMProvider(primary=primary, fallbacks=[fallback])

    response = asyncio.run(
        provider.chat_stream_with_tools(
            [ChatMessage(role="user", content="hello")],
            reasoning_effort="high",
            structured_output={"type": "json_schema"},
        )
    )

    assert response.content == "ok"
    assert primary.last_kwargs is not None
    assert fallback.last_kwargs is not None
    assert "reasoning_effort" not in fallback.last_kwargs
    assert "structured_output" not in fallback.last_kwargs


def test_failover_strips_provider_specific_kwargs_in_non_streaming_chat():
    primary = _PrimaryFailingProvider()
    fallback = _FallbackCapturingProvider()
    provider = FailoverLLMProvider(primary=primary, fallbacks=[fallback])

    response = asyncio.run(
        provider.chat(
            [ChatMessage(role="user", content="hello")],
            reasoning_effort="high",
            structured_output={"type": "json_schema"},
        )
    )

    assert response.content == "ok"
    assert primary.last_kwargs is not None
    assert fallback.last_kwargs is not None
    assert "reasoning_effort" not in fallback.last_kwargs
    assert "structured_output" not in fallback.last_kwargs


def test_failover_strips_provider_specific_kwargs_for_chat_stream():
    primary = _PrimaryFailingProvider()
    fallback = _FallbackCapturingProvider()
    provider = FailoverLLMProvider(primary=primary, fallbacks=[fallback])

    chunks = asyncio.run(
        _collect_stream(
            provider.chat_stream(
                [ChatMessage(role="user", content="hello")],
                reasoning_effort="high",
                structured_output={"type": "json_schema"},
            )
        )
    )

    assert chunks == ["ok"]
    assert primary.last_kwargs is not None
    assert fallback.last_kwargs is not None
    assert "reasoning_effort" not in fallback.last_kwargs
    assert "structured_output" not in fallback.last_kwargs


async def _collect_stream(stream: AsyncIterator[str]) -> list[str]:
    chunks: list[str] = []
    async for chunk in stream:
        chunks.append(chunk)
    return chunks


class _FakeAnthropicStream:
    def __init__(self, events: list[Any]):
        self._events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def __aiter__(self):
        self._idx = 0
        return self

    async def __anext__(self):
        if self._idx >= len(self._events):
            raise StopAsyncIteration
        event = self._events[self._idx]
        self._idx += 1
        return event


def test_anthropic_omits_temperature_for_claude_opus_4_7():
    provider = AnthropicProvider(api_key="dummy")
    captured: dict[str, Any] = {}

    class _FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                content=[],
                stop_reason="end_turn",
                model="claude-opus-4-7",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )

    provider.client = SimpleNamespace(messages=_FakeMessages())

    asyncio.run(
        provider.chat(
            [ChatMessage(role="user", content="hello")],
            model="claude-opus-4-7",
            temperature=0.2,
        )
    )

    assert "temperature" not in captured


def test_anthropic_omits_temperature_for_claude_opus_4_7_stream():
    provider = AnthropicProvider(api_key="dummy")
    captured: dict[str, Any] = {}

    class _FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            events = [
                SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="text_delta", text="hello"),
                ),
                SimpleNamespace(
                    type="message_delta",
                    delta=SimpleNamespace(stop_reason="end_turn"),
                ),
            ]
            return _FakeAnthropicStream(events)

    provider.client = SimpleNamespace(messages=_FakeMessages())

    chunks = asyncio.run(
        _collect_stream(
            provider.chat_stream(
                [ChatMessage(role="user", content="hello")],
                model="claude-opus-4-7",
                temperature=0.2,
            )
        )
    )

    assert chunks == ["hello"]
    assert "temperature" not in captured


def test_anthropic_omits_temperature_for_claude_opus_4_7_stream_with_tools():
    provider = AnthropicProvider(api_key="dummy")
    captured: dict[str, Any] = {}

    class _FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            events = [
                SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="text_delta", text="hello"),
                ),
                SimpleNamespace(
                    type="message_delta",
                    delta=SimpleNamespace(stop_reason="end_turn"),
                ),
            ]
            return _FakeAnthropicStream(events)

    provider.client = SimpleNamespace(messages=_FakeMessages())

    response = asyncio.run(
        provider.chat_stream_with_tools(
            [ChatMessage(role="user", content="hello")],
            model="claude-opus-4-7",
            temperature=0.2,
        )
    )

    assert response.content == "hello"
    assert "temperature" not in captured


def test_anthropic_omits_temperature_for_claude_opus_4_8():
    provider = AnthropicProvider(api_key="dummy")
    captured: dict[str, Any] = {}

    class _FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                content=[],
                stop_reason="end_turn",
                model="claude-opus-4-8",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )

    provider.client = SimpleNamespace(messages=_FakeMessages())

    asyncio.run(
        provider.chat(
            [ChatMessage(role="user", content="hello")],
            model="claude-opus-4-8",
            temperature=0.2,
        )
    )

    assert "temperature" not in captured


def test_anthropic_omits_temperature_for_claude_opus_4_8_stream():
    provider = AnthropicProvider(api_key="dummy")
    captured: dict[str, Any] = {}

    class _FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            events = [
                SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="text_delta", text="hello"),
                ),
                SimpleNamespace(
                    type="message_delta",
                    delta=SimpleNamespace(stop_reason="end_turn"),
                ),
            ]
            return _FakeAnthropicStream(events)

    provider.client = SimpleNamespace(messages=_FakeMessages())

    chunks = asyncio.run(
        _collect_stream(
            provider.chat_stream(
                [ChatMessage(role="user", content="hello")],
                model="claude-opus-4-8",
                temperature=0.2,
            )
        )
    )

    assert chunks == ["hello"]
    assert "temperature" not in captured


def test_anthropic_omits_temperature_for_claude_opus_4_8_stream_with_tools():
    provider = AnthropicProvider(api_key="dummy")
    captured: dict[str, Any] = {}

    class _FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            events = [
                SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="text_delta", text="hello"),
                ),
                SimpleNamespace(
                    type="message_delta",
                    delta=SimpleNamespace(stop_reason="end_turn"),
                ),
            ]
            return _FakeAnthropicStream(events)

    provider.client = SimpleNamespace(messages=_FakeMessages())

    response = asyncio.run(
        provider.chat_stream_with_tools(
            [ChatMessage(role="user", content="hello")],
            model="claude-opus-4-8",
            temperature=0.2,
        )
    )

    assert response.content == "hello"
    assert "temperature" not in captured


def test_anthropic_omits_temperature_for_claude_fable_5_chat():
    provider = AnthropicProvider(api_key="dummy")
    captured: dict[str, Any] = {}

    class _FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                content=[],
                stop_reason="end_turn",
                model="claude-fable-5",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )

    provider.client = SimpleNamespace(messages=_FakeMessages())

    asyncio.run(
        provider.chat(
            [ChatMessage(role="user", content="hello")],
            model="claude-fable-5",
            temperature=0.2,
        )
    )

    assert "temperature" not in captured


def test_anthropic_omits_temperature_for_claude_fable_5_stream():
    provider = AnthropicProvider(api_key="dummy")
    captured: dict[str, Any] = {}

    class _FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            events = [
                SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="text_delta", text="hello"),
                ),
                SimpleNamespace(
                    type="message_delta",
                    delta=SimpleNamespace(stop_reason="end_turn"),
                ),
            ]
            return _FakeAnthropicStream(events)

    provider.client = SimpleNamespace(messages=_FakeMessages())

    chunks = asyncio.run(
        _collect_stream(
            provider.chat_stream(
                [ChatMessage(role="user", content="hello")],
                model="claude-fable-5",
                temperature=0.2,
            )
        )
    )

    assert chunks == ["hello"]
    assert "temperature" not in captured


def test_anthropic_omits_temperature_for_claude_fable_5_stream_with_tools():
    provider = AnthropicProvider(api_key="dummy")
    captured: dict[str, Any] = {}

    class _FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            events = [
                SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="text_delta", text="hello"),
                ),
                SimpleNamespace(
                    type="message_delta",
                    delta=SimpleNamespace(stop_reason="end_turn"),
                ),
            ]
            return _FakeAnthropicStream(events)

    provider.client = SimpleNamespace(messages=_FakeMessages())

    response = asyncio.run(
        provider.chat_stream_with_tools(
            [ChatMessage(role="user", content="hello")],
            model="claude-fable-5",
            temperature=0.2,
        )
    )

    assert response.content == "hello"
    assert "temperature" not in captured


def test_anthropic_omits_temperature_for_claude_opus_5_chat():
    provider = AnthropicProvider(api_key="dummy")
    captured: dict[str, Any] = {}

    class _FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                content=[],
                stop_reason="end_turn",
                model="claude-opus-5",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )

    provider.client = SimpleNamespace(messages=_FakeMessages())

    asyncio.run(
        provider.chat(
            [ChatMessage(role="user", content="hello")],
            model="claude-opus-5",
            temperature=0.2,
        )
    )

    assert "temperature" not in captured


def test_anthropic_omits_temperature_for_claude_opus_5_stream():
    provider = AnthropicProvider(api_key="dummy")
    captured: dict[str, Any] = {}

    class _FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            events = [
                SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="text_delta", text="hello"),
                ),
                SimpleNamespace(
                    type="message_delta",
                    delta=SimpleNamespace(stop_reason="end_turn"),
                ),
            ]
            return _FakeAnthropicStream(events)

    provider.client = SimpleNamespace(messages=_FakeMessages())

    chunks = asyncio.run(
        _collect_stream(
            provider.chat_stream(
                [ChatMessage(role="user", content="hello")],
                model="claude-opus-5",
                temperature=0.2,
            )
        )
    )

    assert chunks == ["hello"]
    assert "temperature" not in captured


def test_anthropic_omits_temperature_for_claude_opus_5_stream_with_tools():
    provider = AnthropicProvider(api_key="dummy")
    captured: dict[str, Any] = {}

    class _FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            events = [
                SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="text_delta", text="hello"),
                ),
                SimpleNamespace(
                    type="message_delta",
                    delta=SimpleNamespace(stop_reason="end_turn"),
                ),
            ]
            return _FakeAnthropicStream(events)

    provider.client = SimpleNamespace(messages=_FakeMessages())

    response = asyncio.run(
        provider.chat_stream_with_tools(
            [ChatMessage(role="user", content="hello")],
            model="claude-opus-5",
            temperature=0.2,
        )
    )

    assert response.content == "hello"
    assert "temperature" not in captured


def test_anthropic_keeps_temperature_for_other_models():
    provider = AnthropicProvider(api_key="dummy")
    captured: dict[str, Any] = {}

    class _FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                content=[],
                stop_reason="end_turn",
                model="claude-sonnet-4-20250514",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )

    provider.client = SimpleNamespace(messages=_FakeMessages())

    asyncio.run(
        provider.chat(
            [ChatMessage(role="user", content="hello")],
            model="claude-sonnet-4-20250514",
            temperature=0.2,
        )
    )

    assert captured["temperature"] == 0.2


def test_anthropic_clamps_temperature_for_claude_opus_4_6_chat():
    provider = AnthropicProvider(api_key="dummy")
    captured: dict[str, Any] = {}

    class _FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                content=[],
                stop_reason="end_turn",
                model="claude-opus-4-6",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )

    provider.client = SimpleNamespace(messages=_FakeMessages())

    asyncio.run(
        provider.chat(
            [ChatMessage(role="user", content="hello")],
            model="claude-opus-4-6",
            temperature=1.8,
        )
    )

    assert captured["temperature"] == 1.0


def test_anthropic_clamps_temperature_for_claude_opus_4_6_stream():
    provider = AnthropicProvider(api_key="dummy")
    captured: dict[str, Any] = {}

    class _FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            events = [
                SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="text_delta", text="hello"),
                ),
                SimpleNamespace(
                    type="message_delta",
                    delta=SimpleNamespace(stop_reason="end_turn"),
                ),
            ]
            return _FakeAnthropicStream(events)

    provider.client = SimpleNamespace(messages=_FakeMessages())

    chunks = asyncio.run(
        _collect_stream(
            provider.chat_stream(
                [ChatMessage(role="user", content="hello")],
                model="claude-opus-4-6",
                temperature=1.8,
            )
        )
    )

    assert chunks == ["hello"]
    assert captured["temperature"] == 1.0


def test_anthropic_clamps_temperature_for_claude_opus_4_6_stream_with_tools():
    provider = AnthropicProvider(api_key="dummy")
    captured: dict[str, Any] = {}

    class _FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            events = [
                SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="text_delta", text="hello"),
                ),
                SimpleNamespace(
                    type="message_delta",
                    delta=SimpleNamespace(stop_reason="end_turn"),
                ),
            ]
            return _FakeAnthropicStream(events)

    provider.client = SimpleNamespace(messages=_FakeMessages())

    response = asyncio.run(
        provider.chat_stream_with_tools(
            [ChatMessage(role="user", content="hello")],
            model="claude-opus-4-6",
            temperature=1.8,
        )
    )

    assert response.content == "hello"
    assert captured["temperature"] == 1.0


# ─── temperature clamp edge cases ────────────────────────────────────────────
# The original PR only covered the 1.8 → 1.0 (above-range) scenario. The
# helper also has to handle:
#   * below-range values (e.g. -0.5 → 0.0) — Anthropic also rejects negatives;
#   * non-finite values (NaN / +inf / -inf) — must fall back to 0.7 instead
#     of forwarding garbage that the API would 400 on (and that would also
#     bypass the 0..1 clamp, since min/max of NaN is NaN).
# These tests pin both contracts so a refactor of ``_normalize_temperature``
# doesn't silently regress the unhappy edges.


def test_anthropic_clamps_negative_temperature_to_zero():
    import math as _math

    from app.llm.anthropic_provider import _normalize_temperature

    assert _normalize_temperature("claude-opus-4-6", -0.5) == 0.0
    # And the non-finite cases use the documented 0.7 default rather than
    # silently propagating NaN / inf into the API payload.
    assert _normalize_temperature("claude-sonnet-4", _math.nan) == 0.7
    assert _normalize_temperature("claude-sonnet-4", _math.inf) == 0.7
    assert _normalize_temperature("claude-sonnet-4", -_math.inf) == 0.7


def test_anthropic_clamps_negative_temperature_in_chat_path():
    """End-to-end through ``chat`` so we lock the wiring, not just the helper."""
    provider = AnthropicProvider(api_key="dummy")
    captured: dict[str, Any] = {}

    class _FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                content=[],
                stop_reason="end_turn",
                model="claude-opus-4-6",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )

    provider.client = SimpleNamespace(messages=_FakeMessages())

    asyncio.run(
        provider.chat(
            [ChatMessage(role="user", content="hello")],
            model="claude-opus-4-6",
            temperature=-0.5,
        )
    )

    assert captured["temperature"] == 0.0


# ─── temperature preservation across failover ────────────────────────────────
# The ``_FALLBACK_EXCLUDED_KWARGS`` set intentionally excludes
# ``temperature`` because it's a universal sampling parameter that every
# provider accepts. Stripping it would silently regress caller intent
# (e.g. a temperature=0 deterministic run becoming a default 0.7 on
# fallback). These tests lock that contract.


def test_failover_preserves_temperature_for_chat():
    """``FailoverLLMProvider.chat()`` forwards ``temperature`` through
    ``_failover_call``'s ``**kwargs`` to the fallback. The fallback must
    receive the caller's value, not its own default."""
    primary = _PrimaryFailingProvider()
    fallback = _FallbackCapturingProvider()
    provider = FailoverLLMProvider(primary=primary, fallbacks=[fallback])

    asyncio.run(
        provider.chat(
            [ChatMessage(role="user", content="hello")],
            temperature=0.1,
            max_tokens=512,
        )
    )

    assert fallback.last_temperature == 0.1
    assert fallback.last_max_tokens == 512


def test_failover_preserves_temperature_for_chat_stream():
    """``FailoverLLMProvider.chat_stream()`` bypasses ``_failover_call``
    and threads ``temperature`` explicitly into ``fallback.chat_stream``.
    The fallback must still receive the caller's value."""
    primary = _PrimaryFailingProvider()
    fallback = _FallbackCapturingProvider()
    provider = FailoverLLMProvider(primary=primary, fallbacks=[fallback])

    asyncio.run(
        _collect_stream(
            provider.chat_stream(
                [ChatMessage(role="user", content="hello")],
                temperature=0.2,
                max_tokens=256,
            )
        )
    )

    assert fallback.last_temperature == 0.2
    assert fallback.last_max_tokens == 256


def test_failover_preserves_temperature_for_chat_stream_with_tools():
    """``FailoverLLMProvider.chat_stream_with_tools()`` also forwards
    ``temperature`` through ``_failover_call``'s ``**kwargs``."""
    primary = _PrimaryFailingProvider()
    fallback = _FallbackCapturingProvider()
    provider = FailoverLLMProvider(primary=primary, fallbacks=[fallback])

    asyncio.run(
        provider.chat_stream_with_tools(
            [ChatMessage(role="user", content="hello")],
            temperature=0.3,
            max_tokens=128,
        )
    )

    assert fallback.last_temperature == 0.3
    assert fallback.last_max_tokens == 128


# ─── max_tokens clamping across failover ─────────────────────────────────────
# Production incident (May 13, 2026): when the primary (Anthropic) failed on
# an unknown model and the failover layer forwarded the primary's resolved
# ``max_tokens=128_000`` to the OpenAI fallback, OpenAI returned
# ``400 max_tokens is too large`` for gpt-5-mini (16K cap). Both providers
# failed, the assignment hung, and the contention added to the DB pool
# exhaustion / OOM cascade. The router now clamps ``max_tokens`` to the
# fallback's declared ``max_output_tokens`` ceiling, inside ``_fallback_kwargs_for_provider``.


class _SmallCeilingFallback(_FallbackCapturingProvider):
    """Mimics OpenAI gpt-5-mini's small completion cap (16_384)."""

    provider_name = "openai"
    max_output_tokens = 16_384


def test_failover_clamps_max_tokens_for_chat():
    primary = _PrimaryFailingProvider()
    fallback = _SmallCeilingFallback()
    provider = FailoverLLMProvider(primary=primary, fallbacks=[fallback])

    asyncio.run(
        provider.chat(
            [ChatMessage(role="user", content="hello")],
            max_tokens=128_000,  # primary-resolved value, oversized for fallback
        )
    )

    # Fallback must receive a clamped value, not the original 128K.
    assert fallback.last_max_tokens == 16_384


def test_failover_clamps_max_tokens_for_chat_stream():
    primary = _PrimaryFailingProvider()
    fallback = _SmallCeilingFallback()
    provider = FailoverLLMProvider(primary=primary, fallbacks=[fallback])

    asyncio.run(
        _collect_stream(
            provider.chat_stream(
                [ChatMessage(role="user", content="hello")],
                max_tokens=128_000,
            )
        )
    )

    assert fallback.last_max_tokens == 16_384


def test_failover_clamps_max_tokens_for_chat_stream_with_tools():
    primary = _PrimaryFailingProvider()
    fallback = _SmallCeilingFallback()
    provider = FailoverLLMProvider(primary=primary, fallbacks=[fallback])

    asyncio.run(
        provider.chat_stream_with_tools(
            [ChatMessage(role="user", content="hello")],
            max_tokens=128_000,
        )
    )

    assert fallback.last_max_tokens == 16_384


def test_failover_does_not_clamp_when_max_tokens_within_ceiling():
    primary = _PrimaryFailingProvider()
    fallback = _SmallCeilingFallback()
    provider = FailoverLLMProvider(primary=primary, fallbacks=[fallback])

    asyncio.run(
        provider.chat(
            [ChatMessage(role="user", content="hello")],
            max_tokens=4_096,  # well within 16K ceiling
        )
    )

    # Caller intent must be preserved when there is no need to clamp.
    assert fallback.last_max_tokens == 4_096


# ─── Sentinel-default behaviour on ``_fallback_kwargs_for_provider`` ────────
# These tests pin the ``max_tokens=None`` contract so a future refactor doesn't reintroduce the
# ``KeyError`` from the streaming-failover ``pop("max_tokens")`` call.


def test_failover_chat_stream_with_explicit_max_tokens_none_does_not_crash():
    """Regression for the streaming-failover crash on ``max_tokens=None``.

    Before the sentinel-default fix, ``_fallback_kwargs_for_provider``
    only wrote ``max_tokens`` into its returned dict when the
    parameter was an ``int`` (``if max_tokens is not None``), but the
    streaming-failover path unconditionally called
    ``fallback_kwargs.pop("max_tokens")`` on the assumption that the
    key was always present.  A caller invoking
    ``provider.chat_stream(..., max_tokens=None)`` would therefore
    crash the failover with a ``KeyError`` — locking us out of the
    fallback entirely.

    The fix uses a sentinel default on the helper so callers
    passing ``None`` (a valid "no cap" signal upstream) get an
    explicit ``max_tokens=None`` written into the dict.  This test
    pins the contract by triggering the primary's failure, asking
    the failover to handle the stream, and asserting the fallback
    received ``max_tokens=None`` without any ``KeyError`` being
    raised.
    """
    primary = _PrimaryFailingProvider()
    fallback = _FallbackCapturingProvider()
    provider = FailoverLLMProvider(primary=primary, fallbacks=[fallback])

    chunks = asyncio.run(
        _collect_stream(
            provider.chat_stream(
                [ChatMessage(role="user", content="hello")],
                temperature=0.7,
                max_tokens=None,
            )
        )
    )

    assert chunks == ["ok"]
    assert fallback.last_max_tokens is None


def test_failover_chat_with_explicit_max_tokens_none_does_not_crash():
    """Same regression on the non-streaming chat path.

    ``_failover_call`` passes ``max_tokens=kwargs.get("max_tokens")``
    to the helper; the previous behaviour treated a missing-or-None
    value as "skip the key in the dict", which left the
    ``max_tokens`` key absent from the per-fallback kwargs and the
    chat path's ``**per_fallback_kwargs`` expansion dropped it.
    With the sentinel default, explicit-None is preserved and
    propagated to the fallback — locking the contract for clients
    that send ``max_tokens=None`` to opt into the provider default.
    """
    primary = _PrimaryFailingProvider()
    fallback = _FallbackCapturingProvider()
    provider = FailoverLLMProvider(primary=primary, fallbacks=[fallback])

    response = asyncio.run(
        provider.chat(
            [ChatMessage(role="user", content="hello")],
            max_tokens=None,
        )
    )

    assert response.content == "ok"
    assert fallback.last_max_tokens is None


def test_fallback_kwargs_helper_keeps_explicit_none_in_output_dict():
    """``_fallback_kwargs_for_provider`` writes ``max_tokens=None``
    into the returned dict when the caller explicitly passes ``None``.

    The streaming failover path relies on this — a missing key would
    make its unconditional ``.pop("max_tokens")`` raise.
    """
    fallback = _FallbackCapturingProvider()

    out = FailoverLLMProvider._fallback_kwargs_for_provider(
        fallback,
        model=None,
        kwargs={},
        max_tokens=None,
    )

    assert "max_tokens" in out, (
        "Explicit ``max_tokens=None`` must round-trip through the "
        "helper as a None entry in the returned dict — otherwise "
        "callers that unconditionally pop the key crash with KeyError."
    )
    assert out["max_tokens"] is None


def test_fallback_kwargs_helper_sentinel_skips_when_kwargs_lacks_max_tokens():
    """Backwards-compat: when the caller doesn't pass ``max_tokens``
    explicitly AND ``kwargs`` doesn't carry it either, the helper
    leaves the key out of the returned dict — the pre-PR behaviour
    that ``_failover_call``'s ``**per_fallback_kwargs`` expansion
    relies on (a missing ``max_tokens`` lets the fallback's signature
    default apply).
    """
    fallback = _FallbackCapturingProvider()

    out = FailoverLLMProvider._fallback_kwargs_for_provider(
        fallback,
        model=None,
        kwargs={"temperature": 0.5},
    )

    assert "max_tokens" not in out
    assert out["temperature"] == 0.5
