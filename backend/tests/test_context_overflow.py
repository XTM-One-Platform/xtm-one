"""Tests for the pre-flight context overflow detection system.

Covers:
  - ContextOverflowError construction and properties
  - BaseLLMProvider._measure_oai_payload (text, images, data URLs, tool_calls)
  - BaseLLMProvider._check_resolved_overflow (threshold trigger / no-trigger)
  - BaseLLMProvider._resolve_context_window (with / without per-model overrides)
  - overflow_friendly_message / overflow_api_message / overflow_error_type (both branches)
  - FailoverLLMProvider._is_non_failoverable returns True for ContextOverflowError
  - Provider chat() raises ContextOverflowError when payload exceeds threshold
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.llm.base import (
    BaseLLMProvider,
    ChatMessage,
    ChatResponse,
    ContextOverflowError,
    overflow_api_message,
    overflow_error_type,
    overflow_friendly_message,
)
from app.tools.base import ToolDefinition

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_overflow_error(
    estimated: int = 200_000,
    window: int = 128_000,
    model: str | None = "gpt-4o",
    tool_tokens: int = 0,
    message_tokens: int = 0,
) -> ContextOverflowError:
    return ContextOverflowError(
        estimated_tokens=estimated,
        context_window=window,
        model=model,
        tool_tokens=tool_tokens,
        message_tokens=message_tokens,
    )


def _make_tool(name: str, description: str = "", schema: dict | None = None) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        input_schema=schema or {"type": "object", "properties": {}},
    )


# ---------------------------------------------------------------------------
# Minimal concrete provider for testing (no real HTTP calls)
# ---------------------------------------------------------------------------


class _MinimalProvider(BaseLLMProvider):
    """A minimal concrete provider for testing BaseLLMProvider overflow detection."""

    provider_name = "test"
    context_window = 128_000

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolDefinition] | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        # Build a minimal OAI-format representation so the measurement works
        oai_msgs = [
            {"role": m.role, "content": m.content or ""}
            for m in messages
        ]
        tc, ic, nb = self._measure_oai_payload(oai_msgs)
        self._check_resolved_overflow(
            text_chars=tc,
            image_count=ic,
            native_file_bytes=nb,
            tools=tools,
            model=model,
        )
        return ChatResponse(content="ok", model=model or "test-model")

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        return [0.0]

    async def list_models(self) -> list[str]:
        return ["test-model"]


# ---------------------------------------------------------------------------
# 1. ContextOverflowError — construction and properties
# ---------------------------------------------------------------------------


class TestContextOverflowError:
    def test_basic_attributes(self) -> None:
        exc = _make_overflow_error(estimated=150_000, window=128_000)
        assert exc.estimated_tokens == 150_000
        assert exc.context_window == 128_000
        assert exc.model == "gpt-4o"
        assert exc.over_by == 22_000

    def test_over_by_clamped_to_zero(self) -> None:
        exc = ContextOverflowError(
            estimated_tokens=100_000,
            context_window=128_000,
        )
        assert exc.over_by == 0

    def test_is_tool_dominated_false(self) -> None:
        exc = ContextOverflowError(
            estimated_tokens=200_000,
            context_window=128_000,
            tool_tokens=30_000,
            message_tokens=60_000,
        )
        assert exc.is_tool_dominated is False

    def test_is_tool_dominated_true(self) -> None:
        exc = ContextOverflowError(
            estimated_tokens=200_000,
            context_window=128_000,
            tool_tokens=80_000,
            message_tokens=40_000,
        )
        assert exc.is_tool_dominated is True

    def test_str_includes_key_info(self) -> None:
        exc = _make_overflow_error(estimated=200_000, window=128_000)
        msg = str(exc)
        assert "200,000" in msg
        assert "128,000" in msg

    def test_model_none(self) -> None:
        exc = ContextOverflowError(
            estimated_tokens=200_000,
            context_window=128_000,
            model=None,
        )
        assert exc.model is None
        # Should not raise — model is optional
        assert "200,000" in str(exc)


# ---------------------------------------------------------------------------
# 2. overflow_friendly_message / overflow_api_message / overflow_error_type
# ---------------------------------------------------------------------------


class TestOverflowMessages:
    def test_friendly_message_context_overflow(self) -> None:
        exc = ContextOverflowError(
            estimated_tokens=200_000, context_window=128_000,
            tool_tokens=10_000, message_tokens=80_000,
        )
        msg = overflow_friendly_message(exc)
        assert "conversation" in msg.lower() or "context" in msg.lower()
        assert "tool" not in msg.lower()

    def test_friendly_message_tool_overflow(self) -> None:
        exc = ContextOverflowError(
            estimated_tokens=200_000, context_window=128_000,
            tool_tokens=80_000, message_tokens=20_000,
        )
        msg = overflow_friendly_message(exc)
        assert "tool" in msg.lower()

    def test_api_message_context_overflow(self) -> None:
        exc = ContextOverflowError(
            estimated_tokens=200_000, context_window=128_000,
            tool_tokens=10_000, message_tokens=80_000,
        )
        msg = overflow_api_message(exc)
        assert "context" in msg.lower() or "conversation" in msg.lower()

    def test_api_message_tool_overflow(self) -> None:
        exc = ContextOverflowError(
            estimated_tokens=200_000, context_window=128_000,
            tool_tokens=80_000, message_tokens=20_000,
        )
        msg = overflow_api_message(exc)
        assert "tool" in msg.lower()

    def test_error_type_context(self) -> None:
        exc = ContextOverflowError(
            estimated_tokens=200_000, context_window=128_000,
            tool_tokens=10_000, message_tokens=80_000,
        )
        assert overflow_error_type(exc) == "context_overflow"

    def test_error_type_tool(self) -> None:
        exc = ContextOverflowError(
            estimated_tokens=200_000, context_window=128_000,
            tool_tokens=80_000, message_tokens=20_000,
        )
        assert overflow_error_type(exc) == "tool_overflow"


# ---------------------------------------------------------------------------
# 3. _resolve_context_window
# ---------------------------------------------------------------------------


class TestResolveContextWindow:
    def test_uses_provider_default_when_no_overrides(self) -> None:
        p = _MinimalProvider()
        p.context_window = 64_000
        p.context_windows = None
        assert p._resolve_context_window("in-house-model") == 64_000

    def test_catalog_outranks_provider_default(self) -> None:
        p = _MinimalProvider()
        p.context_window = 64_000
        p.context_windows = None
        assert p._resolve_context_window("gpt-4o") == 128_000

    def test_uses_per_model_override(self) -> None:
        p = _MinimalProvider()
        p.context_window = 64_000
        p.context_windows = {"gpt-4o": 128_000}
        assert p._resolve_context_window("gpt-4o") == 128_000

    def test_falls_back_to_default_for_unknown_model(self) -> None:
        p = _MinimalProvider()
        p.context_window = 64_000
        p.context_windows = {"gpt-4o": 128_000}
        assert p._resolve_context_window("claude-3-opus") == 64_000

    def test_model_none_uses_provider_default(self) -> None:
        p = _MinimalProvider()
        p.context_window = 64_000
        p.context_windows = {"gpt-4o": 128_000}
        assert p._resolve_context_window(None) == 64_000


# ---------------------------------------------------------------------------
# 4. _check_resolved_overflow — trigger / no-trigger
# ---------------------------------------------------------------------------


class TestCheckResolvedOverflow:
    def _make_provider(self, window: int = 128_000) -> _MinimalProvider:
        p = _MinimalProvider()
        p.context_window = window
        return p

    def test_no_overflow_small_payload(self) -> None:
        p = self._make_provider()
        # 10 chars → ~3 tokens, far under 128K
        p._check_resolved_overflow(
            text_chars=10,
            image_count=0,
            native_file_bytes=0,
            tools=None,
            model=None,
        )

    def test_overflow_raised_at_threshold(self) -> None:
        p = self._make_provider(window=10_000)
        # 95% of 10_000 = 9_500 tokens → 28_500 chars needed to trigger
        with pytest.raises(ContextOverflowError) as exc_info:
            p._check_resolved_overflow(
                text_chars=30_000,  # 10_000 tokens > 9_500 threshold
                image_count=0,
                native_file_bytes=0,
                tools=None,
                model=None,
            )
        err = exc_info.value
        assert err.context_window == 10_000
        assert err.estimated_tokens == 30_000 // 3  # chars / 3

    def test_no_overflow_exactly_at_threshold(self) -> None:
        p = self._make_provider(window=10_000)
        # 9_499 tokens = just under 95% threshold
        p._check_resolved_overflow(
            text_chars=28_497,  # 9_499 tokens
            image_count=0,
            native_file_bytes=0,
            tools=None,
            model=None,
        )

    def test_images_counted(self) -> None:
        p = self._make_provider(window=1_000)
        # 2 images × 1_000 tokens/image = 2_000 tokens > 950 threshold
        with pytest.raises(ContextOverflowError) as exc_info:
            p._check_resolved_overflow(
                text_chars=0,
                image_count=2,
                native_file_bytes=0,
                tools=None,
                model=None,
            )
        assert exc_info.value.message_tokens == 2_000

    def test_tools_counted_and_reflected_in_tool_tokens(self) -> None:
        p = self._make_provider(window=1_000)
        # tool with long schema string
        large_schema = {"type": "object", "properties": {"x": {"description": "y" * 3_000}}}
        tools = [_make_tool("big_tool", "desc", large_schema)]
        with pytest.raises(ContextOverflowError) as exc_info:
            p._check_resolved_overflow(
                text_chars=0,
                image_count=0,
                native_file_bytes=0,
                tools=tools,
                model=None,
            )
        assert exc_info.value.tool_tokens > 0

    def test_per_model_override_used(self) -> None:
        p = self._make_provider(window=1_000)
        p.context_windows = {"big-model": 200_000}
        # same payload, but big-model has a 200K window — no overflow
        p._check_resolved_overflow(
            text_chars=100_000,  # ~33_333 tokens, under 190K threshold
            image_count=0,
            native_file_bytes=0,
            tools=None,
            model="big-model",
        )


# ---------------------------------------------------------------------------
# 5. _measure_oai_payload — text, images, data URLs, tool_calls
# ---------------------------------------------------------------------------


class TestMeasureOaiPayload:
    def test_plain_text_content(self) -> None:
        msgs = [{"role": "user", "content": "hello world"}]
        tc, ic, nb = BaseLLMProvider._measure_oai_payload(msgs)
        assert tc == len("hello world")
        assert ic == 0
        assert nb == 0

    def test_none_content_skipped(self) -> None:
        msgs = [{"role": "assistant", "content": None}]
        tc, ic, nb = BaseLLMProvider._measure_oai_payload(msgs)
        assert tc == 0
        assert ic == 0
        assert nb == 0

    def test_image_url_data_counted_as_image(self) -> None:
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc123"}},
                ],
            }
        ]
        _, ic, nb = BaseLLMProvider._measure_oai_payload(msgs)
        assert ic == 1
        assert nb == 0

    def test_non_image_data_url_counted_as_bytes(self) -> None:
        # A PDF data URL — chars after comma are the b64 payload
        b64_data = "A" * 1000
        url = f"data:application/pdf;base64,{b64_data}"
        msgs = [
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": url}}],
            }
        ]
        _, ic, nb = BaseLLMProvider._measure_oai_payload(msgs)
        assert ic == 0
        assert nb == pytest.approx(1000 * 3 // 4, abs=5)

    def test_http_image_url_counted_as_image(self) -> None:
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
                ],
            }
        ]
        _, ic, _ = BaseLLMProvider._measure_oai_payload(msgs)
        assert ic == 1

    def test_mixed_content_parts(self) -> None:
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "abc"},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,xyz"}},
                ],
            }
        ]
        tc, ic, _ = BaseLLMProvider._measure_oai_payload(msgs)
        assert tc == 3
        assert ic == 1

    def test_tool_calls_in_assistant_message_counted(self) -> None:
        msgs = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "function": {
                            "name": "my_tool",
                            "arguments": json.dumps({"key": "value"}),
                        }
                    }
                ],
            }
        ]
        tc, ic, _ = BaseLLMProvider._measure_oai_payload(msgs)
        expected = len("my_tool") + len(json.dumps({"key": "value"}))
        assert tc == expected
        assert ic == 0

    def test_multiple_tool_calls_summed(self) -> None:
        msgs = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"function": {"name": "tool_a", "arguments": '{"x": 1}'}},
                    {"function": {"name": "tool_b", "arguments": '{"y": 2}'}},
                ],
            }
        ]
        tc, _, _ = BaseLLMProvider._measure_oai_payload(msgs)
        expected = (
            len("tool_a") + len('{"x": 1}')
            + len("tool_b") + len('{"y": 2}')
        )
        assert tc == expected

    def test_assistant_message_with_content_and_tool_calls(self) -> None:
        msgs = [
            {
                "role": "assistant",
                "content": "thinking...",
                "tool_calls": [
                    {"function": {"name": "do_it", "arguments": "{}"}},
                ],
            }
        ]
        tc, _, _ = BaseLLMProvider._measure_oai_payload(msgs)
        assert tc == len("thinking...") + len("do_it") + len("{}")

    # -- Defensive parsing: malformed proxy input --------------------------

    def test_image_url_as_plain_string(self) -> None:
        """Proxy clients may send image_url as a bare string instead of dict."""
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": "https://example.com/img.png"},
                ],
            }
        ]
        tc, ic, _ = BaseLLMProvider._measure_oai_payload(msgs)
        assert ic == 1
        assert tc == 0

    def test_image_url_as_none(self) -> None:
        """image_url: null should be silently skipped."""
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": None},
                ],
            }
        ]
        tc, ic, _ = BaseLLMProvider._measure_oai_payload(msgs)
        assert ic == 0
        assert tc == 0

    def test_tool_call_arguments_as_dict(self) -> None:
        """Proxy may forward arguments as a parsed dict instead of JSON string."""
        args_dict = {"key": "value", "nested": [1, 2]}
        msgs = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"function": {"name": "my_tool", "arguments": args_dict}},
                ],
            }
        ]
        tc, _, _ = BaseLLMProvider._measure_oai_payload(msgs)
        assert tc == len("my_tool") + len(json.dumps(args_dict))

    def test_tool_call_arguments_as_none(self) -> None:
        """arguments: null should not crash."""
        msgs = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"function": {"name": "tool", "arguments": None}},
                ],
            }
        ]
        tc, _, _ = BaseLLMProvider._measure_oai_payload(msgs)
        assert tc == len("tool")

    def test_tool_call_name_as_none(self) -> None:
        """name: null should not crash."""
        msgs = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"function": {"name": None, "arguments": "{}"}},
                ],
            }
        ]
        tc, _, _ = BaseLLMProvider._measure_oai_payload(msgs)
        assert tc == len("{}")


# ---------------------------------------------------------------------------
# 6. Provider integration — chat() raises ContextOverflowError when over limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_chat_raises_on_overflow() -> None:
    """A provider with a tiny context window raises ContextOverflowError."""
    p = _MinimalProvider()
    p.context_window = 10  # absurdly small

    msg = ChatMessage(role="user", content="x" * 1_000)
    with pytest.raises(ContextOverflowError):
        await p.chat([msg])


@pytest.mark.asyncio
async def test_provider_chat_succeeds_within_limit() -> None:
    """A provider with a large window does not raise for a small payload."""
    p = _MinimalProvider()
    p.context_window = 1_000_000

    msg = ChatMessage(role="user", content="hello")
    result = await p.chat([msg])
    assert result.content == "ok"


# ---------------------------------------------------------------------------
# 7. FailoverLLMProvider._is_non_failoverable returns True for ContextOverflowError
# ---------------------------------------------------------------------------


def test_failover_is_non_failoverable_context_overflow() -> None:
    from app.llm.router import FailoverLLMProvider

    exc = _make_overflow_error()
    assert FailoverLLMProvider._is_non_failoverable(exc) is True


def test_failover_is_non_failoverable_generic_exception() -> None:
    from app.llm.router import FailoverLLMProvider

    exc = RuntimeError("some transient network error")
    # Generic errors should NOT be treated as non-failoverable
    assert FailoverLLMProvider._is_non_failoverable(exc) is False


def test_failover_is_non_failoverable_known_keyword() -> None:
    from app.llm.router import FailoverLLMProvider

    exc = RuntimeError("prompt is too long for the model")
    assert FailoverLLMProvider._is_non_failoverable(exc) is True


# ---------------------------------------------------------------------------
# 8. chat_stream tools parameter is passed to _check_resolved_overflow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_stream_raises_on_overflow_with_tools() -> None:
    """chat_stream() should account for tools and raise ContextOverflowError."""
    p = _MinimalProvider()
    p.context_window = 100  # tiny window

    large_schema = {
        "type": "object",
        "properties": {"x": {"description": "y" * 500}},
    }
    tools = [_make_tool("big_tool", "description", large_schema)]

    msg = ChatMessage(role="user", content="hi")

    chunks = []
    with pytest.raises(ContextOverflowError):
        async for chunk in p.chat_stream([msg], tools=tools):
            chunks.append(chunk)


# ---------------------------------------------------------------------------
# 9. AnthropicProvider._measure_anthropic_payload
# ---------------------------------------------------------------------------


class TestMeasureAnthropicPayload:
    """Tests for AnthropicProvider._measure_anthropic_payload."""

    @staticmethod
    def _measure(
        system_msg: str | None, conversation: list[dict]
    ) -> tuple[int, int, int]:
        from app.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider._measure_anthropic_payload(system_msg, conversation)

    def test_system_message_counted(self) -> None:
        tc, ic, nb = self._measure("You are helpful.", [])
        assert tc == len("You are helpful.")
        assert ic == 0
        assert nb == 0

    def test_system_none(self) -> None:
        tc, _, _ = self._measure(None, [])
        assert tc == 0

    def test_string_content(self) -> None:
        msgs = [{"role": "user", "content": "Hello world"}]
        tc, ic, nb = self._measure(None, msgs)
        assert tc == len("Hello world")
        assert ic == 0
        assert nb == 0

    def test_text_block(self) -> None:
        msgs = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "Analyze this."}],
            }
        ]
        tc, _, _ = self._measure(None, msgs)
        assert tc == len("Analyze this.")

    def test_image_block(self) -> None:
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "data": "abc"}},
                    {"type": "image", "source": {"type": "base64", "data": "def"}},
                ],
            }
        ]
        tc, ic, nb = self._measure(None, msgs)
        assert ic == 2
        assert tc == 0
        assert nb == 0

    def test_document_block_base64(self) -> None:
        # 400 base64 chars → ~300 raw bytes (400 * 3 // 4)
        b64 = "A" * 400
        msgs = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {"type": "base64", "data": b64},
                    }
                ],
            }
        ]
        tc, ic, nb = self._measure(None, msgs)
        assert nb == 300
        assert ic == 0
        assert tc == 0

    def test_tool_use_block(self) -> None:
        tool_input = {"query": "search term", "limit": 10}
        msgs = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "search",
                        "input": tool_input,
                    }
                ],
            }
        ]
        tc, _, _ = self._measure(None, msgs)
        assert tc == len(json.dumps(tool_input))

    def test_tool_result_block(self) -> None:
        msgs = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": "Result text here",
                    }
                ],
            }
        ]
        tc, _, _ = self._measure(None, msgs)
        assert tc == len("Result text here")

    def test_tool_result_block_list_content(self) -> None:
        """tool_result content can be a list of content blocks (text, image)."""
        msgs = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": [
                            {"type": "text", "text": "Here is the result"},
                            {"type": "image", "source": {"type": "base64", "data": "x"}},
                            {"type": "text", "text": " with more detail"},
                        ],
                    }
                ],
            }
        ]
        tc, ic, nb = self._measure(None, msgs)
        assert tc == len("Here is the result") + len(" with more detail")
        assert ic == 1
        assert nb == 0

    def test_mixed_content(self) -> None:
        """Multiple block types in a single message and across messages."""
        b64 = "B" * 200  # → 150 bytes
        tool_input = {"x": 1}
        msgs = [
            {"role": "user", "content": "Hello"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Look:"},
                    {"type": "image", "source": {"type": "base64", "data": "x"}},
                    {
                        "type": "document",
                        "source": {"type": "base64", "data": b64},
                    },
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "t", "input": tool_input},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
                ],
            },
        ]
        tc, ic, nb = self._measure("sys", msgs)
        expected_text = (
            len("sys")
            + len("Hello")
            + len("Look:")
            + len(json.dumps(tool_input))
            + len("ok")
        )
        assert tc == expected_text
        assert ic == 1
        assert nb == 150


# ---------------------------------------------------------------------------
# 10. BedrockProvider._measure_bedrock_payload
# ---------------------------------------------------------------------------


class TestMeasureBedrockPayload:
    """Tests for BedrockProvider._measure_bedrock_payload."""

    @staticmethod
    def _measure(
        system_parts: list[dict], converse_messages: list[dict]
    ) -> tuple[int, int, int]:
        from app.llm.bedrock_provider import BedrockProvider

        return BedrockProvider._measure_bedrock_payload(system_parts, converse_messages)

    def test_system_parts(self) -> None:
        tc, ic, nb = self._measure([{"text": "Be brief."}], [])
        assert tc == len("Be brief.")
        assert ic == 0
        assert nb == 0

    def test_text_block(self) -> None:
        msgs = [{"role": "user", "content": [{"text": "Hello"}]}]
        tc, _, _ = self._measure([], msgs)
        assert tc == len("Hello")

    def test_image_block(self) -> None:
        msgs = [
            {
                "role": "user",
                "content": [
                    {"image": {"format": "png", "source": {"bytes": b"\x89PNG"}}},
                ],
            }
        ]
        tc, ic, nb = self._measure([], msgs)
        assert ic == 1
        assert tc == 0
        assert nb == 0

    def test_document_block(self) -> None:
        raw_bytes = b"\x00" * 500
        msgs = [
            {
                "role": "user",
                "content": [
                    {
                        "document": {
                            "name": "doc.pdf",
                            "format": "pdf",
                            "source": {"bytes": raw_bytes},
                        }
                    }
                ],
            }
        ]
        tc, ic, nb = self._measure([], msgs)
        assert nb == 500
        assert ic == 0
        assert tc == 0

    def test_document_non_bytes_ignored(self) -> None:
        """If source.bytes is not bytes (e.g. missing), native_file_bytes stays 0."""
        msgs = [
            {
                "role": "user",
                "content": [
                    {
                        "document": {
                            "name": "doc.pdf",
                            "source": {"bytes": "not-bytes"},
                        }
                    }
                ],
            }
        ]
        _, _, nb = self._measure([], msgs)
        assert nb == 0

    def test_tool_use_block(self) -> None:
        tool_input = {"action": "lookup", "id": 42}
        msgs = [
            {
                "role": "assistant",
                "content": [
                    {"toolUse": {"toolUseId": "t1", "name": "fn", "input": tool_input}}
                ],
            }
        ]
        tc, _, _ = self._measure([], msgs)
        assert tc == len(json.dumps(tool_input))

    def test_tool_result_block(self) -> None:
        msgs = [
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "t1",
                            "content": [{"text": "answer A"}, {"text": "answer B"}],
                        }
                    }
                ],
            }
        ]
        tc, _, _ = self._measure([], msgs)
        assert tc == len("answer A") + len("answer B")

    def test_mixed_content(self) -> None:
        raw_pdf = b"\x25PDF" * 100  # 400 bytes
        tool_input = {"q": "v"}
        msgs = [
            {
                "role": "user",
                "content": [
                    {"text": "See this:"},
                    {"image": {"format": "jpeg", "source": {"bytes": b"\xff"}}},
                    {
                        "document": {
                            "name": "f.pdf",
                            "format": "pdf",
                            "source": {"bytes": raw_pdf},
                        },
                    },
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"toolUse": {"toolUseId": "t1", "name": "fn", "input": tool_input}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "t1",
                            "content": [{"text": "done"}],
                        }
                    },
                ],
            },
        ]
        tc, ic, nb = self._measure([{"text": "sys"}], msgs)
        expected_text = (
            len("sys") + len("See this:") + len(json.dumps(tool_input)) + len("done")
        )
        assert tc == expected_text
        assert ic == 1
        assert nb == 400


# ---------------------------------------------------------------------------
# 11. GeminiProvider._measure_gemini_payload
# ---------------------------------------------------------------------------


class TestMeasureGeminiPayload:
    """Tests for GeminiProvider._measure_gemini_payload."""

    @staticmethod
    def _measure(
        system_instruction: str | None,
        contents: list,
    ) -> tuple[int, int, int]:
        from app.llm.gemini_provider import GeminiProvider

        return GeminiProvider._measure_gemini_payload(system_instruction, contents)

    @staticmethod
    def _content(role: str, parts: list) -> Any:
        from google.genai import types

        return types.Content(role=role, parts=parts)

    @staticmethod
    def _part_text(text: str) -> Any:
        from google.genai import types

        return types.Part(text=text)

    @staticmethod
    def _part_inline_image(data: bytes = b"\x89PNG", mime: str = "image/png") -> Any:
        from google.genai import types

        return types.Part(inline_data=types.Blob(mime_type=mime, data=data))

    @staticmethod
    def _part_inline_file(data: bytes, mime: str = "application/pdf") -> Any:
        from google.genai import types

        return types.Part(inline_data=types.Blob(mime_type=mime, data=data))

    @staticmethod
    def _part_file_data(uri: str = "gs://bucket/img.png", mime: str = "image/png") -> Any:
        from google.genai import types

        return types.Part(file_data=types.FileData(file_uri=uri, mime_type=mime))

    def test_system_instruction(self) -> None:
        tc, ic, nb = self._measure("You are helpful.", [])
        assert tc == len("You are helpful.")
        assert ic == 0
        assert nb == 0

    def test_system_none(self) -> None:
        tc, _, _ = self._measure(None, [])
        assert tc == 0

    def test_text_part(self) -> None:
        contents = [self._content("user", [self._part_text("Hello")])]
        tc, ic, nb = self._measure(None, contents)
        assert tc == len("Hello")
        assert ic == 0
        assert nb == 0

    def test_inline_image(self) -> None:
        contents = [
            self._content("user", [self._part_inline_image(b"\x89PNG", "image/png")])
        ]
        tc, ic, nb = self._measure(None, contents)
        assert ic == 1
        assert tc == 0
        assert nb == 0

    def test_inline_non_image_file(self) -> None:
        pdf_data = b"\x25PDF" * 100  # 400 bytes
        contents = [
            self._content(
                "user", [self._part_inline_file(pdf_data, "application/pdf")]
            )
        ]
        tc, ic, nb = self._measure(None, contents)
        assert nb == 400
        assert ic == 0
        assert tc == 0

    def test_file_data_counts_as_image(self) -> None:
        contents = [
            self._content("user", [self._part_file_data("gs://b/img.png")])
        ]
        tc, ic, nb = self._measure(None, contents)
        assert ic == 1
        assert tc == 0
        assert nb == 0

    def test_mixed_content(self) -> None:
        pdf_data = b"\x00" * 250
        contents = [
            self._content(
                "user",
                [
                    self._part_text("Analyze:"),
                    self._part_inline_image(),
                    self._part_inline_file(pdf_data),
                ],
            ),
            self._content(
                "model",
                [self._part_text("Here is the analysis.")],
            ),
            self._content(
                "user",
                [
                    self._part_file_data("gs://b/photo.jpg"),
                    self._part_text("And this?"),
                ],
            ),
        ]
        tc, ic, nb = self._measure("sys", contents)
        expected_text = len("sys") + len("Analyze:") + len("Here is the analysis.") + len("And this?")
        assert tc == expected_text
        assert ic == 2  # 1 inline image + 1 file_data
        assert nb == 250
