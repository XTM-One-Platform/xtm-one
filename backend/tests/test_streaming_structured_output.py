"""Streaming structured output enforcement.

Native structured output used to be dropped on every streaming
final-answer path: ``agent_loop._streaming_chat`` neither accepted nor
forwarded the ``structured_output`` kwarg, and ``agent_chat`` stripped it
from stream kwargs.  These tests pin the new contract:

* providers that declare ``supports_streaming_structured_output`` receive
  the kwarg on ``chat_stream()`` (OpenAI-compatible providers translate it
  to ``response_format``),
* providers without the capability never receive the kwarg (prompt-based
  SAP + post-response validation remain their only enforcement),
* the agentic loop's wind-down streaming final answer forwards the extra,
* SAP post-validation still applies to the streamed result.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.llm.openai_compat import OpenAICompatibleProvider
from app.services.structured_output import (
    build_openai_response_format,
    validate_and_parse,
)
from app.tools.agent_loop import _streaming_chat, run_agent_loop
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
)

_SCHEMA: dict = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}

_RESPONSE_FORMAT = build_openai_response_format(_SCHEMA)


class _AsyncChunkIterator:
    """Async iterator over a fixed list of OpenAI-style stream chunks."""

    def __init__(self, chunks: list):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


def _openai_text_chunk(text: str):
    from types import SimpleNamespace

    delta = SimpleNamespace(content=text, tool_calls=None, reasoning_content=None)
    choice = SimpleNamespace(delta=delta, finish_reason=None)
    return SimpleNamespace(choices=[choice], usage=None)


class _RecordingStreamLLM(BaseLLMProvider):
    """Stub provider that records the kwargs of every ``chat_stream`` and
    ``chat_stream_with_tools`` call.

    ``chat_stream_with_tools`` always returns a tool call, so a loop run
    with tools exercises one tool-calling iteration before the wind-down
    strips tools and streams the final answer via ``chat_stream``.
    """

    provider_name = "recording"
    default_model = "recording-model"

    def __init__(
        self,
        *,
        supports_streaming_structured_output: bool = False,
        streamed_text: str = '{"answer": "42"}',
    ):
        self.supports_streaming_structured_output = (
            supports_streaming_structured_output
        )
        self._streamed_text = streamed_text
        self.chat_stream_kwargs: list[dict] = []
        self.chat_stream_with_tools_kwargs: list[dict] = []

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        return ChatResponse(content=self._streamed_text, model=model or self.default_model)

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        self.chat_stream_kwargs.append(dict(kwargs))
        self.last_stop_reason = None
        self.last_usage = None
        yield self._streamed_text
        self.last_stop_reason = "end_turn"

    async def chat_stream_with_tools(
        self, messages, tools=None, *, on_token=None, model=None,
        temperature=0.7, max_tokens=4096, **kwargs,
    ):
        self.chat_stream_with_tools_kwargs.append(dict(kwargs))
        return ChatResponse(
            content="",
            model=model or self.default_model,
            tool_calls=[ToolCall(id="tc-1", name="dummy_tool", arguments={})],
            stop_reason="tool_use",
        )

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return [self.default_model]


# ---------------------------------------------------------------------------
# OpenAI-compatible chat_stream: structured_output -> response_format
# ---------------------------------------------------------------------------


def _openai_compat_with_mock_client() -> OpenAICompatibleProvider:
    provider = object.__new__(OpenAICompatibleProvider)
    provider.client = MagicMock()
    provider.client.chat.completions.create = AsyncMock(
        return_value=_AsyncChunkIterator([_openai_text_chunk('{"answer": "42"}')])
    )
    return provider


@pytest.mark.asyncio
async def test_openai_compat_chat_stream_forwards_response_format():
    provider = _openai_compat_with_mock_client()

    chunks = [
        chunk
        async for chunk in provider.chat_stream(
            [ChatMessage(role="user", content="hi")],
            model="gpt-4o",
            structured_output=_RESPONSE_FORMAT,
        )
    ]

    assert "".join(chunks) == '{"answer": "42"}'
    sent = provider.client.chat.completions.create.call_args.kwargs
    assert sent.get("response_format") == _RESPONSE_FORMAT


@pytest.mark.asyncio
async def test_openai_compat_chat_stream_omits_response_format_when_absent():
    provider = _openai_compat_with_mock_client()

    async for _ in provider.chat_stream(
        [ChatMessage(role="user", content="hi")], model="gpt-4o"
    ):
        pass

    sent = provider.client.chat.completions.create.call_args.kwargs
    assert "response_format" not in sent


@pytest.mark.asyncio
async def test_openai_compat_chat_stream_honours_capability_opt_out():
    """A subclass that sets ``supports_streaming_structured_output = False``
    must never send ``response_format`` — even when a caller passes the
    kwarg to ``chat_stream()`` directly (the agentic loop gates upstream,
    but the flag must hold for every caller)."""
    provider = _openai_compat_with_mock_client()
    provider.supports_streaming_structured_output = False

    async for _ in provider.chat_stream(
        [ChatMessage(role="user", content="hi")],
        model="gpt-4o",
        structured_output=_RESPONSE_FORMAT,
    ):
        pass

    sent = provider.client.chat.completions.create.call_args.kwargs
    assert "response_format" not in sent


def test_openai_compat_declares_streaming_structured_output_support():
    assert OpenAICompatibleProvider.supports_streaming_structured_output is True


def test_base_provider_defaults_to_no_streaming_structured_output():
    assert BaseLLMProvider.supports_streaming_structured_output is False


# ---------------------------------------------------------------------------
# Gemini chat_stream: structured_output -> GenerateContentConfig
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gemini_chat_stream_wires_response_schema_into_config():
    """``GeminiProvider.chat_stream()`` must copy ``response_mime_type`` /
    ``response_schema`` from the structured-output config onto the
    ``GenerateContentConfig`` — same fields as the non-streaming path."""
    from types import SimpleNamespace

    from app.llm.gemini_provider import GeminiProvider
    from app.services.structured_output import build_gemini_response_schema

    provider = object.__new__(GeminiProvider)
    provider.client = MagicMock()
    provider.client.aio.models.generate_content_stream = AsyncMock(
        return_value=_AsyncChunkIterator(
            [
                SimpleNamespace(
                    usage_metadata=None,
                    candidates=None,
                    text='{"answer": "42"}',
                )
            ]
        )
    )

    gemini_cfg = build_gemini_response_schema(_SCHEMA)
    chunks = [
        chunk
        async for chunk in provider.chat_stream(
            [ChatMessage(role="user", content="hi")],
            model="gemini-2.5-pro",
            structured_output=gemini_cfg,
        )
    ]

    assert "".join(chunks) == '{"answer": "42"}'
    sent_config = provider.client.aio.models.generate_content_stream.call_args.kwargs[
        "config"
    ]
    assert sent_config.response_mime_type == "application/json"
    assert sent_config.response_schema == _SCHEMA


# ---------------------------------------------------------------------------
# _streaming_chat: capability-gated forwarding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_chat_forwards_structured_output_when_supported():
    llm = _RecordingStreamLLM(supports_streaming_structured_output=True)

    resp = await _streaming_chat(
        llm,
        [ChatMessage(role="user", content="hi")],
        on_token=None,
        model="recording-model",
        max_tokens=1024,
        structured_output=_RESPONSE_FORMAT,
    )

    assert resp.content == '{"answer": "42"}'
    assert len(llm.chat_stream_kwargs) == 1
    assert llm.chat_stream_kwargs[0].get("structured_output") == _RESPONSE_FORMAT


@pytest.mark.asyncio
async def test_streaming_chat_drops_structured_output_when_unsupported():
    llm = _RecordingStreamLLM(supports_streaming_structured_output=False)

    resp = await _streaming_chat(
        llm,
        [ChatMessage(role="user", content="hi")],
        on_token=None,
        model="recording-model",
        max_tokens=1024,
        structured_output=_RESPONSE_FORMAT,
    )

    assert resp.content == '{"answer": "42"}'
    assert len(llm.chat_stream_kwargs) == 1
    assert "structured_output" not in llm.chat_stream_kwargs[0]


@pytest.mark.asyncio
async def test_streaming_chat_respects_failover_wrapper_capability():
    from app.llm.router import FailoverLLMProvider

    primary = _RecordingStreamLLM(supports_streaming_structured_output=True)
    failover = FailoverLLMProvider(primary=primary, fallbacks=[])

    # The wrapper mirrors the primary's capability flag...
    assert failover.supports_streaming_structured_output is True

    # ...so _streaming_chat forwards the kwarg through the wrapper.
    await _streaming_chat(
        failover,
        [ChatMessage(role="user", content="hi")],
        on_token=None,
        model="recording-model",
        max_tokens=1024,
        structured_output=_RESPONSE_FORMAT,
    )
    assert primary.chat_stream_kwargs[0].get("structured_output") == _RESPONSE_FORMAT


# ---------------------------------------------------------------------------
# Agentic loop: streaming final answers include the structured extra
# ---------------------------------------------------------------------------


async def _noop_token(_: str) -> None:
    return None


@pytest.mark.asyncio
async def test_loop_streaming_final_answer_includes_structured_output():
    """No tools + on_token: the loop streams the final answer immediately
    and must forward the structured extra to ``chat_stream``."""
    llm = _RecordingStreamLLM(supports_streaming_structured_output=True)

    response = await run_agent_loop(
        llm,
        [ChatMessage(role="user", content="answer in JSON")],
        tools=None,
        executor=None,
        max_iterations=3,
        model="recording-model",
        max_tokens=1024,
        on_token=_noop_token,
        structured_output=_RESPONSE_FORMAT,
        progressive_tools=False,
    )

    assert response.content == '{"answer": "42"}'
    assert len(llm.chat_stream_kwargs) == 1
    assert llm.chat_stream_kwargs[0].get("structured_output") == _RESPONSE_FORMAT


@pytest.mark.asyncio
async def test_wind_down_streaming_final_answer_includes_structured_output():
    """Tools present: the LLM calls a tool first, then the loop strips
    tools (wind-down) and the streaming final answer must carry the
    structured extra — previously dropped."""
    llm = _RecordingStreamLLM(supports_streaming_structured_output=True)

    async def dummy_tool() -> str:
        return "tool result"

    callable_exec = CallableToolExecutor()
    callable_exec.register("dummy_tool", dummy_tool)
    tool_defs = [ToolDefinition(name="dummy_tool", description="A dummy tool")]
    composite = CompositeToolExecutor()
    composite.add_tools(tool_defs, callable_exec)

    response = await run_agent_loop(
        llm,
        [ChatMessage(role="user", content="answer in JSON")],
        tools=tool_defs,
        executor=composite,
        max_iterations=2,
        model="recording-model",
        max_tokens=1024,
        on_token=_noop_token,
        structured_output=_RESPONSE_FORMAT,
        progressive_tools=False,
    )

    assert response.content == '{"answer": "42"}'
    # Iteration 1 streamed with tools (tool call); the structured extra is
    # only merged on final-answer calls, never on tool-calling iterations.
    assert len(llm.chat_stream_with_tools_kwargs) == 1
    assert "structured_output" not in llm.chat_stream_with_tools_kwargs[0]
    # Iteration 2 stripped tools and streamed the final answer with the extra.
    assert len(llm.chat_stream_kwargs) == 1
    assert llm.chat_stream_kwargs[0].get("structured_output") == _RESPONSE_FORMAT


@pytest.mark.asyncio
async def test_wind_down_streaming_drops_extra_for_unsupported_provider():
    llm = _RecordingStreamLLM(supports_streaming_structured_output=False)

    response = await run_agent_loop(
        llm,
        [ChatMessage(role="user", content="answer in JSON")],
        tools=None,
        executor=None,
        max_iterations=3,
        model="recording-model",
        max_tokens=1024,
        on_token=_noop_token,
        structured_output=_RESPONSE_FORMAT,
        progressive_tools=False,
    )

    assert response.content == '{"answer": "42"}'
    assert len(llm.chat_stream_kwargs) == 1
    assert "structured_output" not in llm.chat_stream_kwargs[0]


# ---------------------------------------------------------------------------
# Truncation continuations must never carry the structured extra
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_truncation_continuation_strips_structured_output():
    """``max_tokens`` continuations cannot use native schema enforcement:
    grammar-constrained decoding emits a fresh, complete JSON document
    instead of resuming the truncated text, which would corrupt the
    stitched payload.  ``_continue_truncated_response`` must strip
    ``structured_output`` from ``llm_extra`` (direct-chat callers forward
    their full stream kwargs) before calling the LLM again."""
    from app.llm.base import ChatResponse as _ChatResponse
    from app.tools.agent_loop import _continue_truncated_response

    llm = _RecordingStreamLLM(supports_streaming_structured_output=True)
    initial = _ChatResponse(
        content='{"answer": "trunc',
        model="recording-model",
        stop_reason="max_tokens",
    )

    result = await _continue_truncated_response(
        llm,
        [ChatMessage(role="user", content="answer in JSON")],
        initial,
        on_token=_noop_token,
        on_event=None,
        model="recording-model",
        temperature=0.7,
        max_tokens=1024,
        max_session_chars=100_000,
        llm_extra={"structured_output": _RESPONSE_FORMAT},
        merge_usage=lambda u, served_model=None: None,
    )

    assert len(llm.chat_stream_kwargs) == 1
    assert "structured_output" not in llm.chat_stream_kwargs[0]
    # The continuation text is stitched onto the truncated content.
    assert result.content.startswith('{"answer": "trunc')


@pytest.mark.asyncio
async def test_truncation_continuation_strips_structured_output_non_streaming():
    """Same contract for the non-streaming continuation branch
    (``on_token=None`` -> ``llm.chat``)."""
    from app.llm.base import ChatResponse as _ChatResponse
    from app.tools.agent_loop import _continue_truncated_response

    class _RecordingChatLLM(_RecordingStreamLLM):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.chat_kwargs: list[dict] = []

        async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
            self.chat_kwargs.append(dict(kwargs))
            return _ChatResponse(
                content=self._streamed_text,
                model=model or self.default_model,
                stop_reason="end_turn",
            )

    llm = _RecordingChatLLM(supports_streaming_structured_output=True)
    initial = _ChatResponse(
        content='{"answer": "trunc',
        model="recording-model",
        stop_reason="max_tokens",
    )

    await _continue_truncated_response(
        llm,
        [ChatMessage(role="user", content="answer in JSON")],
        initial,
        on_token=None,
        on_event=None,
        model="recording-model",
        temperature=0.7,
        max_tokens=1024,
        max_session_chars=100_000,
        llm_extra={"structured_output": _RESPONSE_FORMAT},
        merge_usage=lambda u, served_model=None: None,
    )

    assert len(llm.chat_kwargs) == 1
    assert "structured_output" not in llm.chat_kwargs[0]


# ---------------------------------------------------------------------------
# SAP post-validation remains the universal safety net
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sap_validation_still_applies_to_streamed_result():
    """The streamed final answer still goes through ``validate_and_parse``
    downstream (agent_chat post-validation is unchanged) — pin that the
    pipeline accepts the streamed payload and rejects schema violations."""
    llm = _RecordingStreamLLM(supports_streaming_structured_output=True)

    response = await run_agent_loop(
        llm,
        [ChatMessage(role="user", content="answer in JSON")],
        tools=None,
        executor=None,
        max_iterations=3,
        model="recording-model",
        max_tokens=1024,
        on_token=_noop_token,
        structured_output=_RESPONSE_FORMAT,
        progressive_tools=False,
    )

    parsed, errors = validate_and_parse(response.content, _SCHEMA)
    assert errors == []
    assert parsed == {"answer": "42"}

    # A schema-violating payload is still caught by post-validation.
    _, bad_errors = validate_and_parse('{"wrong_key": 1}', _SCHEMA)
    assert bad_errors
