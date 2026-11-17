"""Leaked tool-call recovery at every adapter call site, and through the loop.

``app.llm.tool_markup_recovery`` is unit-tested in ``test_tool_markup_recovery.py``.
Here each Claude-serving entry point that calls it — Anthropic ``chat`` and
``chat_stream_with_tools``, Bedrock ``chat`` and ``chat_stream_with_tools`` — is
driven with a scripted SDK response, and a recovered call is run by the agent
loop like a structured one.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from anthropic.types import (
    Message,
    MessageDeltaUsage,
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    RawContentBlockStopEvent,
    RawMessageDeltaEvent,
    RawMessageStartEvent,
    RawMessageStopEvent,
    TextBlock,
    TextDelta,
    Usage,
)
from anthropic.types.raw_message_delta_event import Delta

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import ChatMessage
from app.llm.bedrock_provider import BedrockProvider
from app.llm.model_capabilities import is_claude_model
from app.tools.agent_loop import run_agent_loop
from app.tools.base import CallableToolExecutor, ToolDefinition

ANTHROPIC_MODEL = "claude-sonnet-4-6"
BEDROCK_CLAUDE = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
BEDROCK_LLAMA = "meta.llama3-70b-instruct-v1:0"

_MESSAGES = [ChatMessage(role="user", content="Do the work")]

_TOOLS = [
    ToolDefinition(
        name="do_work",
        description="Perform a unit of work.",
        input_schema={"type": "object", "properties": {"n": {"type": "integer"}}},
        source="test",
    )
]

_LEAK = (
    "Running it now.\n"
    '<function_calls><invoke name="do_work"><parameter name="n">3</parameter>'
    "</invoke></function_calls>"
)


def _assert_recovered(response) -> None:
    assert response.stop_reason == "tool_use"
    assert [(c.name, c.arguments) for c in response.tool_calls] == [("do_work", {"n": 3})]
    assert response.content == "Running it now."
    assert response.leaked_tool_markup_unrecovered is False


def _assert_untouched(response) -> None:
    assert not response.tool_calls
    assert response.content == _LEAK
    assert response.leaked_tool_markup_unrecovered is False


# ── Anthropic ─────────────────────────────────────────────────────────────


def _anthropic_message(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=[TextBlock(type="text", text=text)],
        stop_reason="end_turn",
        model=ANTHROPIC_MODEL,
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )


def _anthropic_chat(*texts: str) -> AnthropicProvider:
    provider = AnthropicProvider(api_key="test-key")
    queue = list(texts)

    async def _create(**_kwargs):
        return _anthropic_message(queue.pop(0))

    provider.client = SimpleNamespace(messages=SimpleNamespace(create=_create))
    return provider


class _AsyncScript:
    def __init__(self, items: list) -> None:
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class _AnthropicStreamCtx:
    def __init__(self, items: list) -> None:
        self._items = items

    async def __aenter__(self):
        return _AsyncScript(self._items)

    async def __aexit__(self, *_exc):
        return False


def _anthropic_stream_events(text: str) -> list:
    return [
        RawMessageStartEvent(
            type="message_start",
            message=Message(
                id="msg_test",
                type="message",
                role="assistant",
                model=ANTHROPIC_MODEL,
                content=[],
                stop_reason=None,
                stop_sequence=None,
                usage=Usage(input_tokens=1, output_tokens=1),
            ),
        ),
        RawContentBlockStartEvent(
            type="content_block_start", index=0, content_block=TextBlock(type="text", text="")
        ),
        RawContentBlockDeltaEvent(
            type="content_block_delta", index=0, delta=TextDelta(type="text_delta", text=text)
        ),
        RawContentBlockStopEvent(type="content_block_stop", index=0),
        RawMessageDeltaEvent(
            type="message_delta",
            delta=Delta(stop_reason="end_turn", stop_sequence=None),
            usage=MessageDeltaUsage(output_tokens=5),
        ),
        RawMessageStopEvent(type="message_stop"),
    ]


def _anthropic_stream(text: str) -> AnthropicProvider:
    provider = AnthropicProvider(api_key="test-key")

    async def _create(**kwargs):
        assert kwargs.get("stream") is True
        return _AnthropicStreamCtx(_anthropic_stream_events(text))

    provider.client = SimpleNamespace(messages=SimpleNamespace(create=_create))
    return provider


@pytest.mark.asyncio
async def test_anthropic_chat_recovers_a_trailing_leak_of_a_declared_tool():
    response = await _anthropic_chat(_LEAK).chat(_MESSAGES, model=ANTHROPIC_MODEL, tools=_TOOLS)
    _assert_recovered(response)


@pytest.mark.asyncio
async def test_anthropic_chat_without_tools_leaves_the_text_alone():
    response = await _anthropic_chat(_LEAK).chat(_MESSAGES, model=ANTHROPIC_MODEL)
    _assert_untouched(response)


@pytest.mark.asyncio
async def test_anthropic_stream_recovers_a_trailing_leak_of_a_declared_tool():
    tokens: list[str] = []

    async def on_token(chunk: str) -> None:
        tokens.append(chunk)

    response = await _anthropic_stream(_LEAK).chat_stream_with_tools(
        _MESSAGES, _TOOLS, on_token=on_token, model=ANTHROPIC_MODEL
    )
    _assert_recovered(response)


@pytest.mark.asyncio
async def test_anthropic_stream_flags_a_leak_of_an_undeclared_tool():
    leak = '<function_calls><invoke name="drop_database"></invoke></function_calls>'
    response = await _anthropic_stream(leak).chat_stream_with_tools(
        _MESSAGES, _TOOLS, model=ANTHROPIC_MODEL
    )
    assert not response.tool_calls
    assert response.content == leak
    assert response.leaked_tool_markup_unrecovered is True


# ── Bedrock ───────────────────────────────────────────────────────────────


class _SyncScript:
    def __init__(self, items: list) -> None:
        self._items = list(items)

    def __iter__(self):
        return self

    def __next__(self):
        if not self._items:
            raise StopIteration
        return self._items.pop(0)


def _bedrock(text: str) -> BedrockProvider:
    provider = BedrockProvider("us-east-1")

    class _Client:
        def converse(self, **_kwargs):
            return {
                "output": {"message": {"content": [{"text": text}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 1, "outputTokens": 1},
            }

        def converse_stream(self, **_kwargs):
            return {
                "stream": _SyncScript(
                    [
                        {"messageStart": {"role": "assistant"}},
                        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": text}}},
                        {"contentBlockStop": {"contentBlockIndex": 0}},
                        {"messageStop": {"stopReason": "end_turn"}},
                        {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1}}},
                    ]
                )
            }

    provider._client = _Client()
    return provider


@pytest.mark.asyncio
async def test_bedrock_chat_recovers_a_claude_leak():
    response = await _bedrock(_LEAK).chat(_MESSAGES, model=BEDROCK_CLAUDE, tools=_TOOLS)
    _assert_recovered(response)


@pytest.mark.asyncio
async def test_bedrock_stream_recovers_a_claude_leak():
    response = await _bedrock(_LEAK).chat_stream_with_tools(
        _MESSAGES, _TOOLS, model=BEDROCK_CLAUDE
    )
    _assert_recovered(response)


@pytest.mark.asyncio
async def test_bedrock_leaves_another_model_family_alone():
    # The markup is Claude's own: a Llama answer that shows it stays text.
    chat = await _bedrock(_LEAK).chat(_MESSAGES, model=BEDROCK_LLAMA, tools=_TOOLS)
    stream = await _bedrock(_LEAK).chat_stream_with_tools(_MESSAGES, _TOOLS, model=BEDROCK_LLAMA)
    _assert_untouched(chat)
    _assert_untouched(stream)


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-sonnet-4-6", True),
        ("anthropic.claude-sonnet-4-5", True),
        (BEDROCK_CLAUDE, True),
        ("anthropic/claude-opus-4.7", True),
        (BEDROCK_LLAMA, False),
        ("amazon.nova-pro-v1:0", False),
        ("mistral.mistral-large-2402-v1:0", False),
        (None, False),
    ],
)
def test_is_claude_model(model, expected):
    assert is_claude_model(model) is expected


# ── Through the loop ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_recovered_call_runs_through_the_loop():
    """The adapter turns the leak into a real call; the loop executes it with
    the typed arguments and the run ends on the model's next answer."""
    seen: list[dict] = []
    executor = CallableToolExecutor()

    async def _do_work(**kwargs):
        seen.append(kwargs)
        return "work done"

    executor.register("do_work", _do_work)
    llm = _anthropic_chat(_LEAK, "Done: the work ran.")

    response = await run_agent_loop(
        llm=llm,
        messages=list(_MESSAGES),
        tools=_TOOLS,
        executor=executor,
        model=ANTHROPIC_MODEL,
        max_iterations=4,
    )

    assert seen == [{"n": 3}]
    assert response.content == "Done: the work ran."
