"""Reasoning text reaches the chat reasoning window.

The window stayed empty on most models because no reasoning TEXT was ever
streamed: Gemini returns thought summaries only with ``include_thoughts``,
OpenRouter streams reasoning as ``delta.reasoning`` (only
``reasoning_content`` was read), and the reasoning of consecutive model calls
ran together once it did flow. The Anthropic half (``thinking.display``) is
pinned in ``test_anthropic_provider.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import types

from app.llm.base import (
    BaseLLMProvider,
    ChatMessage,
    ChatResponse,
    ThinkingActivitySink,
    reads_reasoning,
)
from app.llm.gemini_provider import GeminiProvider, _supports_thinking
from app.llm.openai_compat import (
    consume_openai_stream_with_tools,
    openai_content_parts,
    openai_reasoning_text,
)
from app.tools.agent_loop import _streaming_chat, run_agent_loop
from app.tools.base import CallableToolExecutor, CompositeToolExecutor, ToolCall, ToolDefinition

# ---------------------------------------------------------------------------
# Gemini: thought summaries are requested when a consumer streams them
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "thinks"),
    [
        ("gemini-2.5-flash", True),
        ("gemini-2.5-flash-lite", True),
        ("gemini-2.5-pro", True),
        ("gemini-3-pro-preview", True),
        ("gemini-3.8-flash", True),
        ("gemini-4.0-flash", True),
        ("gemini-2.0-flash", False),
        ("gemini-1.5-pro", False),
        ("gemini-flash-latest", False),
        ("gemma-3-27b-it", False),
    ],
)
def test_gemini_thinking_families(model: str, thinks: bool):
    assert _supports_thinking(model) is thinks


def _thought_chunk(text: str) -> Any:
    part = SimpleNamespace(text=text, thought=True, function_call=None)
    candidate = SimpleNamespace(finish_reason=None, content=SimpleNamespace(parts=[part]))
    return SimpleNamespace(candidates=[candidate], text=None)


def _text_chunk(text: str) -> Any:
    part = SimpleNamespace(text=text, thought=False, function_call=None)
    candidate = SimpleNamespace(finish_reason=None, content=SimpleNamespace(parts=[part]))
    return SimpleNamespace(candidates=[candidate], text=None)


class _FakeGeminiStream:
    def __init__(self, chunks: list) -> None:
        self._chunks = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration from None


def _gemini_provider(
    configs: list[types.GenerateContentConfig], chunks: list | None = None
) -> GeminiProvider:
    provider = GeminiProvider(api_key="dummy")

    class _Models:
        async def generate_content_stream(self, *, config, **_kwargs):
            configs.append(config)
            return _FakeGeminiStream(
                chunks
                if chunks is not None
                else [_thought_chunk("Weighing the sources."), _text_chunk("Done.")]
            )

    provider.client = SimpleNamespace(aio=SimpleNamespace(models=_Models()))
    return provider


@pytest.mark.asyncio
async def test_gemini_streaming_with_a_consumer_asks_for_thought_summaries():
    configs: list[types.GenerateContentConfig] = []
    provider = _gemini_provider(configs)
    thoughts: list[str] = []

    async def _collect(text: str) -> None:
        thoughts.append(text)

    tokens = [
        t
        async for t in provider.chat_stream(
            [ChatMessage(role="user", content="hi")], model="gemini-3.8-flash", on_thinking=_collect
        )
    ]
    response = await provider.chat_stream_with_tools(
        [ChatMessage(role="user", content="hi")],
        model="gemini-3.8-flash",
        on_thinking=_collect,
        reasoning_effort="low",
    )

    assert [c.thinking_config.include_thoughts for c in configs] == [True, True]
    # The reasoning-effort dialect rides in the same config.
    assert configs[1].thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert tokens == ["Done."]
    assert thoughts == ["Weighing the sources.", "Weighing the sources."]
    assert response.thinking == "Weighing the sources."


@pytest.mark.asyncio
async def test_gemini_streaming_without_a_consumer_or_thinking_model_is_unchanged():
    configs: list[types.GenerateContentConfig] = []
    provider = _gemini_provider(configs)

    async def _collect(_text: str) -> None:
        return None

    async for _ in provider.chat_stream([ChatMessage(role="user", content="hi")], model="gemini-3.8-flash"):
        pass
    async for _ in provider.chat_stream(
        [ChatMessage(role="user", content="hi")], model="gemini-2.0-flash", on_thinking=_collect
    ):
        pass
    await provider.chat_stream_with_tools(
        [ChatMessage(role="user", content="hi")], model="gemini-2.5-flash", reasoning_effort="high"
    )
    # The agent loop's idle-watchdog sink reads nothing, so it asks for nothing.
    await provider.chat_stream_with_tools(
        [ChatMessage(role="user", content="hi")],
        model="gemini-3.8-flash",
        on_thinking=ThinkingActivitySink(lambda: None),
    )

    assert configs[0].thinking_config is None
    assert configs[1].thinking_config is None
    assert configs[2].thinking_config.thinking_budget == 8192
    assert not configs[2].thinking_config.include_thoughts
    assert configs[3].thinking_config is None


@pytest.mark.asyncio
async def test_gemini_reasoning_consumer_never_turns_thinking_on():
    """2.5 Flash-Lite thinks only when the request asks for it. A consumer asks
    for the thoughts of thinking that happens, never for thinking of its own: a
    budget would change the model's cost and latency for the sake of a display.
    With an explicit effort it does think, and the thoughts come back."""
    configs: list[types.GenerateContentConfig] = []
    provider = _gemini_provider(configs)

    async def _collect(_text: str) -> None:
        return None

    for effort in (None, "low"):
        await provider.chat_stream_with_tools(
            [ChatMessage(role="user", content="hi")],
            model="gemini-2.5-flash-lite",
            on_thinking=_collect,
            reasoning_effort=effort,
        )

    assert configs[0].thinking_config.include_thoughts is True
    assert configs[0].thinking_config.thinking_budget is None
    assert configs[1].thinking_config.include_thoughts is True
    assert configs[1].thinking_config.thinking_budget == 1024


def _part_chunk(part: types.Part) -> Any:
    candidate = SimpleNamespace(finish_reason=None, content=SimpleNamespace(parts=[part]))
    return SimpleNamespace(candidates=[candidate], text=None)


@pytest.mark.asyncio
async def test_gemini_thoughts_stay_out_of_the_answer_and_the_turn_replays_verbatim():
    """A thought part reaches the reasoning pane only, never ``content`` or
    ``on_token``; and the model turn replays every part as it came, in order.
    On ``generateContent`` a thought signature can ride on any part, so a
    replay that dropped the thought parts could drop a signature with them."""
    thought = types.Part(
        text="Listing the hosts first.", thought=True, thought_signature=b"signature-0"
    )
    call = types.Part(
        function_call=types.FunctionCall(name="list_hosts", args={}),
        thought_signature=b"signature-1",
    )
    provider = _gemini_provider([], chunks=[_part_chunk(thought), _part_chunk(call)])
    tokens: list[str] = []
    thoughts: list[str] = []

    async def _on_token(text: str) -> None:
        tokens.append(text)

    async def _collect(text: str) -> None:
        thoughts.append(text)

    question = ChatMessage(role="user", content="how many hosts?")
    response = await provider.chat_stream_with_tools(
        [question],
        tools=[ToolDefinition(name="list_hosts", description="List the hosts")],
        model="gemini-3.8-flash",
        on_token=_on_token,
        on_thinking=_collect,
    )

    assert thoughts == ["Listing the hosts first."]
    assert tokens == []
    assert response.content == ""
    assert response.thinking == "Listing the hosts first."
    assert [tc.name for tc in response.tool_calls] == ["list_hosts"]

    turn = ChatMessage(role="assistant", content="", tool_calls=response.tool_calls)
    turn._provider_extra = response._provider_extra
    result = ChatMessage(
        role="tool", content="3", tool_call_id=response.tool_calls[0].id, name="list_hosts"
    )
    _system, contents = await provider._build_gemini_contents([question, turn, result])

    assert contents[1].role == "model"
    assert contents[1].parts == [thought, call]
    assert [p.thought_signature for p in contents[1].parts] == [b"signature-0", b"signature-1"]


# ---------------------------------------------------------------------------
# OpenAI-compatible: ``reasoning_content`` or ``reasoning``, never both
# ---------------------------------------------------------------------------


def test_openai_reasoning_text_reads_either_field_once():
    assert openai_reasoning_text(SimpleNamespace(reasoning_content="a")) == "a"
    assert openai_reasoning_text(SimpleNamespace(reasoning="b")) == "b"
    assert openai_reasoning_text(SimpleNamespace(reasoning_content="a", reasoning="a")) == "a"
    assert openai_reasoning_text(SimpleNamespace(reasoning_content="", reasoning="b")) == "b"
    assert openai_reasoning_text(SimpleNamespace(reasoning=[{"type": "reasoning.text"}])) is None
    assert openai_reasoning_text(SimpleNamespace(content="answer")) is None


@pytest.mark.asyncio
async def test_openrouter_style_reasoning_deltas_reach_the_consumer():
    chunks = [
        SimpleNamespace(
            usage=None,
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(reasoning="Checking the feed.", content=None, tool_calls=None),
                    finish_reason=None,
                )
            ],
        ),
        SimpleNamespace(
            usage=None,
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="Done.", tool_calls=None),
                    finish_reason="stop",
                )
            ],
        ),
    ]

    class _Stream:
        def __init__(self):
            self._it = iter(chunks)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration from None

    class _Completions:
        async def create(self, **_kwargs):
            return _Stream()

    client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    thoughts: list[str] = []

    async def _collect(text: str) -> None:
        thoughts.append(text)

    response = await consume_openai_stream_with_tools(
        client,
        model="anthropic/claude-sonnet-5",
        oai_messages=[{"role": "user", "content": "hi"}],
        on_thinking=_collect,
    )

    assert thoughts == ["Checking the feed."]
    assert response.thinking == "Checking the feed."
    assert response.content == "Done."


# ---------------------------------------------------------------------------
# Mistral: ``content`` as a list of ``thinking`` and ``text`` chunks
# ---------------------------------------------------------------------------


def _mistral_thinking(text: str) -> dict:
    return {"type": "thinking", "thinking": [{"type": "text", "text": text}]}


def test_openai_content_parts_splits_mistral_chunks():
    content = [_mistral_thinking("Weighing it."), {"type": "text", "text": "The answer."}]
    assert openai_content_parts(content) == ("The answer.", "Weighing it.")
    assert openai_content_parts("plain") == ("plain", "")
    assert openai_content_parts(None) == ("", "")
    assert openai_content_parts([SimpleNamespace(type="text", text="as attributes")]) == ("as attributes", "")
    assert openai_content_parts([{"type": "image_url", "image_url": {}}]) == ("", "")


@pytest.mark.asyncio
async def test_mistral_chunk_list_deltas_split_answer_and_reasoning():
    deltas = [
        [_mistral_thinking("Weighing ")],
        [_mistral_thinking("it.")],
        [{"type": "text", "text": "The "}],
        "answer.",
    ]
    chunks = [
        SimpleNamespace(
            usage=None,
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=content, tool_calls=None),
                    finish_reason="stop" if index == len(deltas) - 1 else None,
                )
            ],
        )
        for index, content in enumerate(deltas)
    ]

    class _Stream:
        def __init__(self):
            self._it = iter(chunks)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration from None

    class _Completions:
        async def create(self, **_kwargs):
            return _Stream()

    client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    thoughts: list[str] = []
    tokens: list[str] = []

    async def _think(text: str) -> None:
        thoughts.append(text)

    async def _token(text: str) -> None:
        tokens.append(text)

    response = await consume_openai_stream_with_tools(
        client,
        model="mistral-medium-latest",
        oai_messages=[{"role": "user", "content": "hi"}],
        on_thinking=_think,
        on_token=_token,
    )

    assert thoughts == ["Weighing ", "it."]
    assert tokens == ["The ", "answer."]
    assert response.thinking == "Weighing it."
    assert response.content == "The answer."


# ---------------------------------------------------------------------------
# Agent loop: every model call's reasoning opens a new paragraph
# ---------------------------------------------------------------------------


class _ThinkingLLM(BaseLLMProvider):
    """Two model calls, each streaming its reasoning in several deltas."""

    provider_name = "stub"
    context_window = 100_000

    def __init__(self) -> None:
        self.calls = 0

    async def chat_stream_with_tools(self, messages, tools=None, *, on_token=None, model=None, **kwargs):
        self.calls += 1
        on_thinking = kwargs["on_thinking"]
        if self.calls == 1:
            for delta in ("I need ", "the host list first."):
                await on_thinking(delta)
            return ChatResponse(
                content="",
                model="stub",
                tool_calls=[ToolCall(id="tc-1", name="list_hosts", arguments={})],
                stop_reason="tool_use",
            )
        for delta in ("Three hosts ", "came back."):
            await on_thinking(delta)
        return ChatResponse(content="Done.", model="stub")

    async def chat(self, messages, model=None, **kwargs):
        return ChatResponse(content="Done.", model="stub")

    async def chat_stream(self, messages, **kwargs):
        yield "Done."

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


class _PreambleThenReasoningLLM(_ThinkingLLM):
    """A first call that only writes a short preamble, then a call that reasons."""

    async def chat_stream_with_tools(self, messages, tools=None, *, on_token=None, model=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            await on_token("Let me check the hosts.")
            return ChatResponse(
                content="Let me check the hosts.",
                model="stub",
                tool_calls=[ToolCall(id="tc-1", name="list_hosts", arguments={})],
                stop_reason="tool_use",
            )
        await kwargs["on_thinking"]("Three hosts came back.")
        return ChatResponse(content="Done.", model="stub")


async def _host_loop_pane(llm: BaseLLMProvider) -> str:
    async def _list_hosts() -> str:
        return "web-1, web-2, db-1"

    callable_exec = CallableToolExecutor()
    callable_exec.register("list_hosts", _list_hosts)
    definitions = [ToolDefinition(name="list_hosts", description="List the hosts")]
    executor = CompositeToolExecutor()
    executor.add_tools(definitions, callable_exec)
    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    async def _on_token(_chunk: str) -> None:
        return None

    await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="how many hosts?")],
        tools=definitions,
        executor=executor,
        max_iterations=4,
        progressive_tools=False,
        on_event=_on_event,
        on_token=_on_token,
    )
    return "".join(e["content"] for e in events if e.get("status") == "thinking_text")


@pytest.mark.asyncio
async def test_consecutive_model_calls_reason_in_separate_paragraphs():
    pane = await _host_loop_pane(_ThinkingLLM())

    assert pane.strip() == "I need the host list first.\n\nThree hosts came back."


@pytest.mark.asyncio
async def test_reasoning_after_a_held_preamble_opens_its_own_paragraph():
    """A held-back preamble lands in the pane as a block that ends without a
    break: the next call's reasoning must open its own paragraph, even when it
    is the first reasoning the turn streams."""
    pane = await _host_loop_pane(_PreambleThenReasoningLLM())

    assert pane.strip() == "Let me check the hosts.\n\nThree hosts came back."


class _ScriptedReasoningLLM(BaseLLMProvider):
    """No-tools streaming stub: every call reasons once, then streams its script."""

    provider_name = "stub"
    context_window = 50_000

    def __init__(self, script: list[tuple[str, list[str], str]]) -> None:
        self.script = script
        self.calls = 0
        self.last_stop_reason: str | None = None

    async def chat_stream(self, messages, **kwargs):
        reasoning, chunks, stop_reason = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        await kwargs["on_thinking"](reasoning)
        for chunk in chunks:
            yield chunk
        self.last_stop_reason = stop_reason

    async def chat(self, messages, model=None, **kwargs):
        return ChatResponse(content="", model="stub")

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


_RECOVERED_ANSWER = (
    "Here is the full answer, composed by the forced final answer once both "
    "earlier calls came back empty, long enough to need no retry."
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("script", "pane"),
    [
        pytest.param(
            [
                ("Drafting the report.", ["part one"], "max_tokens"),
                ("Picking up the list.", [" part two"], "end_turn"),
            ],
            "Drafting the report.\n\nPicking up the list.",
            id="max-tokens-continuation",
        ),
        pytest.param(
            [
                ("First look.", [], "end_turn"),
                ("Second look.", [], "end_turn"),
                ("Composing now.", [_RECOVERED_ANSWER], "end_turn"),
            ],
            "First look.\n\nSecond look.\n\nComposing now.",
            id="forced-final-answer",
        ),
    ],
)
async def test_continuations_and_forced_answers_reason_in_their_own_paragraph(script, pane):
    """A continuation or a forced answer is a model call of its own: its
    reasoning must not run into the previous call's mid-sentence."""
    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    async def _on_token(_chunk: str) -> None:
        return None

    await run_agent_loop(
        llm=_ScriptedReasoningLLM(script),
        messages=[ChatMessage(role="user", content="write the report")],
        tools=None,
        executor=None,
        max_iterations=5,
        model="stub",
        on_event=_on_event,
        on_token=_on_token,
    )

    shown = "".join(e["content"] for e in events if e.get("status") == "thinking_text")
    assert shown.strip() == pane


# ---------------------------------------------------------------------------
# Agent loop: the idle watchdog's sink is not a reasoning consumer
# ---------------------------------------------------------------------------


class _RecordingStreamLLM(BaseLLMProvider):
    """Records the ``on_thinking`` each streaming call hands the provider."""

    provider_name = "stub"
    context_window = 100_000

    def __init__(self) -> None:
        self.on_thinking: list[object] = []

    async def chat_stream(self, messages, **kwargs):
        self.on_thinking.append(kwargs.get("on_thinking"))
        yield "Done."

    async def chat(self, messages, model=None, **kwargs):
        return ChatResponse(content="Done.", model="stub")

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.mark.asyncio
async def test_a_silent_stream_hands_the_provider_a_sink_that_reads_nothing():
    """Without a consumer the idle watchdog still passes a callback, to count
    thinking deltas as liveness. It must read as no consumer, or every silent
    run (an assignment) would ask the model for its reasoning text."""
    llm = _RecordingStreamLLM()

    async def _collect(_text: str) -> None:
        return None

    for on_thinking in (None, _collect):
        await _streaming_chat(
            llm,
            [ChatMessage(role="user", content="hi")],
            None,
            model="stub",
            max_tokens=128,
            on_thinking=on_thinking,
        )

    silent, reading = llm.on_thinking
    assert isinstance(silent, ThinkingActivitySink)
    assert not reads_reasoning(silent)
    assert reads_reasoning(reading)
