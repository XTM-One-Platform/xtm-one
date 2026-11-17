"""Guarded replay of Anthropic thinking blocks within a tool round.

The provider used to replay an assistant turn as text + ``tool_use`` only, so
every tool round of a model that thinks by default resumed without the
reasoning that asked for the tools. A turn now gets its blocks back - verbatim,
in place - only when the feature flag is on and the request prefix it was
produced under is provably unchanged; otherwise it is sent exactly as before.

No real Anthropic call anywhere: the SDK is stubbed.
"""

from __future__ import annotations

import copy
import json
import logging
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import anthropic
import httpx
import pytest

from app.core.config import settings
from app.llm import anthropic_provider as provider_mod
from app.llm import anthropic_thinking_replay as replay_mod
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.anthropic_thinking_replay import (
    EXTRA_KEY,
    StreamedBlocks,
    ThinkingCapture,
    blocks_from_content,
    capture_of,
    current_tool_round,
    plain_assistant_content,
    rejects_replayed_blocks,
)
from app.llm.base import (
    BaseLLMProvider,
    ChatMessage,
    ChatResponse,
    message_payload_chars,
    provider_extra_chars,
)
from app.llm.model_capabilities import thinks_by_default
from app.llm.router import FailoverLLMProvider
from app.services import langfuse_tracing as lft
from app.tools.agent_loop import (
    _continue_truncated_response,
    _record_request_trace_metadata,
    _streaming_chat,
    run_agent_loop,
)
from app.tools.base import ToolCall, ToolDefinition

MODEL = "claude-sonnet-5"

_TOOLS = [
    ToolDefinition(
        name="lookup",
        description="Look something up",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
    ),
    ToolDefinition(
        name="store",
        description="Store something",
        input_schema={"type": "object", "properties": {"v": {"type": "string"}}},
    ),
]
# What ``load_tools`` does mid-run: the next request offers one more tool.
_REVEALED_TOOL = ToolDefinition(
    name="notion_search",
    description="Search Notion",
    input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
)

_SIG_1 = "EqQBCkYIBRgCKkA" + "S" * 400
_SIG_2 = "ErcBCkYIBRgCKkB" + "T" * 400


@pytest.fixture
def replay_on(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_thinking_replay_enabled", True)


@pytest.fixture
def replay_off(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_thinking_replay_enabled", False)


# ---------------------------------------------------------------------------
# Scripted SDK stream / create
# ---------------------------------------------------------------------------


def _start(block: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(type="content_block_start", content_block=block)


def _delta(delta: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(type="content_block_delta", delta=delta)


_STOP = SimpleNamespace(type="content_block_stop")


def _thinking(deltas: list[str], signature: str) -> list[SimpleNamespace]:
    return [
        _start(SimpleNamespace(type="thinking", thinking="", signature="")),
        *(_delta(SimpleNamespace(type="thinking_delta", thinking=t)) for t in deltas),
        _delta(SimpleNamespace(type="signature_delta", signature=signature)),
        _STOP,
    ]


def _redacted(data: str) -> list[SimpleNamespace]:
    return [_start(SimpleNamespace(type="redacted_thinking", data=data)), _STOP]


def _text(text: str) -> list[SimpleNamespace]:
    return [
        _start(SimpleNamespace(type="text", text="")),
        _delta(SimpleNamespace(type="text_delta", text=text)),
        _STOP,
    ]


def _tool(tool_id: str, name: str, args: dict) -> list[SimpleNamespace]:
    return [
        _start(SimpleNamespace(type="tool_use", id=tool_id, name=name, input={})),
        _delta(SimpleNamespace(type="input_json_delta", partial_json=json.dumps(args))),
        _STOP,
    ]


def _message(*blocks: list[SimpleNamespace], stop_reason: str = "tool_use") -> list:
    return [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(usage=SimpleNamespace(input_tokens=10, output_tokens=1)),
        ),
        *(event for block in blocks for event in block),
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason=stop_reason),
            usage=SimpleNamespace(output_tokens=7),
        ),
    ]


class _StreamCtx:
    def __init__(self, outcome: Any) -> None:
        self._outcome = outcome

    async def __aenter__(self):
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        events = iter(self._outcome)

        class _Iterator:
            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(events)
                except StopIteration:
                    raise StopAsyncIteration from None

        return _Iterator()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _StreamRecorder:
    """``client.messages.create(stream=True)`` stand-in: snapshots each
    request, replays the next scripted outcome (events, or an exception
    raised at open)."""

    def __init__(self, *outcomes: Any) -> None:
        self._outcomes = list(outcomes)
        self.sent: list[dict] = []

    async def __call__(self, **kwargs):
        self.sent.append(copy.deepcopy(kwargs))
        return _StreamCtx(self._outcomes.pop(0))


def _create_recorder(*outcomes: Any, stream: _StreamRecorder | None = None):
    """``client.messages.create`` stand-in for ``chat()``; a streaming
    request goes to *stream* when the test streams on the same provider."""
    sent: list[dict] = []
    pending = list(outcomes)

    async def _create(**kwargs):
        if stream is not None and kwargs.get("stream"):
            return await stream(**kwargs)
        sent.append(copy.deepcopy(kwargs))
        outcome = pending.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return _create, sent


def _status_error(status: int, message: str) -> anthropic.APIStatusError:
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    response = httpx.Response(
        status, json=body, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    err = f"Error code: {status} - {body}"
    if status == 400:
        return anthropic.BadRequestError(err, response=response, body=body)
    return anthropic.APIStatusError(err, response=response, body=body)


_SIGNATURE_REFUSAL = "messages.1.content.0: Invalid `signature` in `thinking` block"


def _provider(recorder: _StreamRecorder | None = None) -> AnthropicProvider:
    provider = AnthropicProvider(api_key="test-key")
    provider.client = MagicMock()
    if recorder is not None:
        provider.client.messages.create = recorder
    return provider


async def _read(_text: str) -> None:
    return None


async def _call(provider, transcript, *, tools=_TOOLS, model=MODEL) -> ChatResponse:
    return await provider.chat_stream_with_tools(
        messages=list(transcript), tools=tools, model=model, on_thinking=_read
    )


def _answer(transcript: list[ChatMessage], response: ChatResponse, result: str = "42") -> None:
    """What the agent loop does with a tool-calling response."""
    message = ChatMessage(
        role="assistant", content=response.content or "", tool_calls=response.tool_calls
    )
    if response._provider_extra:
        message._provider_extra = response._provider_extra
    transcript.append(message)
    for tc in response.tool_calls or []:
        transcript.append(
            ChatMessage(role="tool", content=result, tool_call_id=tc.id, name=tc.name)
        )


def _base_transcript() -> list[ChatMessage]:
    return [
        ChatMessage(role="system", content="You are a helpful agent."),
        ChatMessage(role="user", content="Find a and store b."),
    ]


_TURN_1 = _message(
    _thinking(["Let me ", "look it up."], _SIG_1),
    _text("Checking."),
    _tool("toolu_1", "lookup", {"q": "a"}),
)
_TURN_2 = _message(
    _thinking(["Now store it."], _SIG_2),
    _redacted("EncryptedPayload=="),
    _tool("toolu_2", "store", {"v": "b"}),
)
_FINAL = _message(_text("Done."), stop_reason="end_turn")

_REPLAYED_TURN_1 = {
    "role": "assistant",
    "content": [
        {"type": "thinking", "thinking": "Let me look it up.", "signature": _SIG_1},
        {"type": "text", "text": "Checking."},
        {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "a"}},
    ],
}
_PLAIN_TURN_1 = {
    "role": "assistant",
    "content": [
        {"type": "text", "text": "Checking."},
        {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "a"}},
    ],
}


def _report(response: ChatResponse) -> dict:
    return response.trace_metadata["thinking_replay"]


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def test_streamed_blocks_mirror_the_sdk_snapshot():
    """Thinking text is the concatenated raw deltas, the signature is the one
    ``signature_delta`` carries (the SDK assigns it), a redacted block arrives
    whole, and the text / tool_use blocks keep their positions."""
    blocks = StreamedBlocks()
    for event in [
        *_thinking(["A ", "b."], "first"),
        *_text("Hello"),
        *_redacted("cipher"),
        *_thinking(["C."], "second"),
        *_tool("toolu_9", "lookup", {"q": "z"}),
    ]:
        if event.type == "content_block_start":
            blocks.start(event.content_block)
        elif event.type == "content_block_delta":
            blocks.delta(event.delta)
        else:
            blocks.stop()

    assert blocks.layout() == (
        {"type": "thinking", "thinking": "A b.", "signature": "first"},
        {"type": "text", "text": "Hello"},
        {"type": "redacted_thinking", "data": "cipher"},
        {"type": "thinking", "thinking": "C.", "signature": "second"},
        {"type": "tool_use", "id": "toolu_9"},
    )


def test_streamed_blocks_drop_empty_text_and_refuse_what_they_cannot_resend():
    blocks = StreamedBlocks()
    blocks.start(SimpleNamespace(type="text", text=""))
    blocks.stop()
    assert blocks.layout() == ()

    cited = StreamedBlocks()
    cited.start(SimpleNamespace(type="text", text=""))
    cited.delta(SimpleNamespace(type="citations_delta", citation={}))
    assert cited.layout() is None

    server_tool = StreamedBlocks()
    server_tool.start(SimpleNamespace(type="server_tool_use", id="srvtoolu_1"))
    assert server_tool.layout() is None


def test_blocks_from_a_non_streamed_message_keep_their_order():
    content = [
        SimpleNamespace(type="thinking", thinking="Plan.", signature="sig"),
        SimpleNamespace(type="text", text="On it."),
        SimpleNamespace(type="redacted_thinking", data="xyz"),
        SimpleNamespace(type="tool_use", id="toolu_3", name="lookup", input={"q": "c"}),
    ]
    assert blocks_from_content(content) == (
        {"type": "thinking", "thinking": "Plan.", "signature": "sig"},
        {"type": "text", "text": "On it."},
        {"type": "redacted_thinking", "data": "xyz"},
        {"type": "tool_use", "id": "toolu_3"},
    )
    assert blocks_from_content([SimpleNamespace(type="web_search_tool_result")]) is None


def _feed(events: list[SimpleNamespace]) -> StreamedBlocks:
    blocks = StreamedBlocks()
    for event in events:
        if event.type == "content_block_start":
            blocks.start(event.content_block)
        elif event.type == "content_block_delta":
            blocks.delta(event.delta)
        else:
            blocks.stop()
    return blocks


def test_a_block_that_could_not_be_resent_as_returned_is_never_kept():
    """The SDK ends a stream without ``message_stop`` when the connection
    closes cleanly, so the last block may never close, and a thinking block
    may stop before its ``signature_delta``: neither could be re-sent as the
    API returned it."""
    still_open = _feed([*_thinking(["A."], "sig"), *_tool("toolu_1", "lookup", {"q": "a"})[:-1]])
    assert still_open.layout() is None

    unsigned = _feed(
        [
            *_tool("toolu_1", "lookup", {"q": "a"}),
            _start(SimpleNamespace(type="thinking", thinking="", signature="")),
            _delta(SimpleNamespace(type="thinking_delta", thinking="Half a thought")),
            _STOP,
        ]
    )
    assert unsigned.layout() is None

    call = SimpleNamespace(type="tool_use", id="toolu_1", name="lookup", input={})
    assert blocks_from_content([SimpleNamespace(type="thinking", thinking="x", signature=""), call]) is None
    assert blocks_from_content([SimpleNamespace(type="redacted_thinking", data=""), call]) is None


@pytest.mark.asyncio
async def test_a_response_cut_before_its_last_signature_is_not_captured(replay_on):
    events = _message(
        _thinking(["Plan."], _SIG_1),
        _tool("toolu_1", "lookup", {"q": "a"}),
        [
            _start(SimpleNamespace(type="thinking", thinking="", signature="")),
            _delta(SimpleNamespace(type="thinking_delta", thinking="More")),
        ],
    )
    provider = _provider(_StreamRecorder(events))

    response = await _call(provider, _base_transcript())

    assert [tc.id for tc in response.tool_calls] == ["toolu_1"]
    assert response._provider_extra is None


@pytest.mark.asyncio
async def test_streaming_capture_keeps_the_blocks_verbatim_and_the_display_path_unchanged(
    replay_on,
):
    """The captured blocks are the raw ones; the reasoning pane still gets the
    joined display text and ``response.thinking`` is unchanged."""
    events = _message(
        _thinking(["First ", "block."], "sig-a"),
        _thinking(["Second block."], "sig-b"),
        _tool("toolu_1", "lookup", {"q": "a"}),
    )
    provider = _provider(_StreamRecorder(events))
    shown: list[str] = []

    async def _collect(text: str) -> None:
        shown.append(text)

    response = await provider.chat_stream_with_tools(
        messages=_base_transcript(), tools=_TOOLS, model=MODEL, on_thinking=_collect
    )

    assert "".join(shown) == "First block.\n\nSecond block."
    assert response.thinking == "First block.\n\nSecond block."
    capture = response._provider_extra[EXTRA_KEY]
    assert isinstance(capture, ThinkingCapture)
    assert capture.blocks == (
        {"type": "thinking", "thinking": "First block.", "signature": "sig-a"},
        {"type": "thinking", "thinking": "Second block.", "signature": "sig-b"},
        {"type": "tool_use", "id": "toolu_1"},
    )
    assert capture.model == MODEL
    assert capture.family == "adaptive"
    assert capture.prefix is not None


@pytest.mark.asyncio
async def test_chat_captures_the_blocks_of_a_non_streamed_response(replay_on):
    reply = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="Plan.", signature="sig-c"),
            SimpleNamespace(type="text", text="On it."),
            SimpleNamespace(type="tool_use", id="toolu_5", name="lookup", input={"q": "d"}),
        ],
        stop_reason="tool_use",
        model=MODEL,
        usage=SimpleNamespace(input_tokens=3, output_tokens=4),
    )
    provider = _provider()
    provider.client.messages.create, _sent = _create_recorder(reply)

    response = await provider.chat(messages=_base_transcript(), model=MODEL, tools=_TOOLS)

    assert response.thinking == "Plan."
    assert response._provider_extra[EXTRA_KEY].blocks == (
        {"type": "thinking", "thinking": "Plan.", "signature": "sig-c"},
        {"type": "text", "text": "On it."},
        {"type": "tool_use", "id": "toolu_5"},
    )


@pytest.mark.asyncio
async def test_chat_stream_exposes_its_capture_like_last_usage(replay_on):
    provider = _provider(_StreamRecorder(_message(_thinking(["Why."], "sig-d"), _text("Because."))))

    chunks = [
        chunk
        async for chunk in provider.chat_stream(
            messages=_base_transcript(), model=MODEL, on_thinking=_read
        )
    ]

    assert chunks == ["Because."]
    capture = provider.last_provider_extra[EXTRA_KEY]
    assert capture.blocks == (
        {"type": "thinking", "thinking": "Why.", "signature": "sig-d"},
        {"type": "text", "text": "Because."},
    )
    # An answer calls no tool: it can never be re-sent, so no fingerprint.
    assert capture.prefix is None


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["claude-opus-4-8", "claude-opus-4-7", "claude-sonnet-4-6"])
async def test_models_that_do_not_think_by_default_capture_nothing(replay_on, model):
    provider = _provider(_StreamRecorder(_TURN_1))

    response = await _call(provider, _base_transcript(), model=model)

    assert response._provider_extra is None
    assert response.trace_metadata is None


@pytest.mark.parametrize(
    ("model", "thinks"),
    [
        ("claude-sonnet-5", True),
        ("claude-opus-5", True),
        ("claude-opus-5-5", True),
        ("claude-fable-5", True),
        ("claude-fable-5-1", True),
        ("claude-mythos-5", True),
        ("claude-opus-4-8", False),
        ("claude-opus-4-7", False),
        ("claude-sonnet-4-6", False),
        ("claude-haiku-4-5", False),
        (None, False),
    ],
)
def test_thinks_by_default(model, thinks):
    assert thinks_by_default(model) is thinks


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unchanged_prefix_re_sends_every_turn_of_the_round_byte_for_byte(replay_on):
    recorder = _StreamRecorder(_TURN_1, _TURN_2, _FINAL)
    provider = _provider(recorder)
    transcript = _base_transcript()

    first = await _call(provider, transcript)
    assert first.trace_metadata is None  # nothing captured yet
    _answer(transcript, first)
    second = await _call(provider, transcript)
    _answer(transcript, second)
    final = await _call(provider, transcript)

    assert recorder.sent[1]["messages"][1] == _REPLAYED_TURN_1
    assert _report(second) == {
        "enabled": True,
        "outcome": "replayed",
        "replayed_turns": 1,
        "replayed_blocks": 1,
        "stripped_turns": 0,
        "stripped_blocks": 0,
        "reason": None,
        "reasons": {},
    }
    third = recorder.sent[2]["messages"]
    # The later turn's prefix covers the earlier turn AS SENT (with its blocks).
    assert third[1] == _REPLAYED_TURN_1
    assert third[3]["content"] == [
        {"type": "thinking", "thinking": "Now store it.", "signature": _SIG_2},
        {"type": "redacted_thinking", "data": "EncryptedPayload=="},
        {"type": "tool_use", "id": "toolu_2", "name": "store", "input": {"v": "b"}},
    ]
    assert _report(final)["replayed_turns"] == 2
    assert _report(final)["replayed_blocks"] == 3
    assert final.content == "Done."


@pytest.mark.asyncio
async def test_flag_off_sends_the_request_it_always_sent(monkeypatch):
    """Byte-identical to main: the turn goes out as text + ``tool_use``, the
    same bytes as a transcript that never carried a capture."""
    monkeypatch.setattr(settings, "anthropic_thinking_replay_enabled", True)
    recorder = _StreamRecorder(_TURN_1, _FINAL, _FINAL)
    provider = _provider(recorder)
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))

    monkeypatch.setattr(settings, "anthropic_thinking_replay_enabled", False)
    response = await _call(provider, transcript)
    bare = [copy.copy(m) for m in transcript]
    for message in bare:
        message._provider_extra = None
    await _call(provider, bare)

    with_capture, without_capture = recorder.sent[1], recorder.sent[2]
    assert with_capture == without_capture
    assert json.dumps(with_capture, sort_keys=True) == json.dumps(without_capture, sort_keys=True)
    assert with_capture["messages"][1] == _PLAIN_TURN_1
    report = _report(response)
    assert report["enabled"] is False
    assert report["outcome"] == "stripped"
    assert report["reason"] == "flag_off"
    assert report["stripped_blocks"] == 1


def _reply(*content: SimpleNamespace, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        content=list(content),
        stop_reason=stop_reason,
        model=MODEL,
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["chat", "chat_stream", "failover"])
async def test_flag_off_is_byte_identical_on_every_path(monkeypatch, path):
    """``chat()``, ``chat_stream()`` and the failover wrapper send exactly the
    request a capture-free transcript produces, like ``chat_stream_with_tools``."""
    monkeypatch.setattr(settings, "anthropic_thinking_replay_enabled", True)
    recorder = _StreamRecorder(_TURN_1, _FINAL, _FINAL)
    provider = _provider(recorder)
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))
    monkeypatch.setattr(settings, "anthropic_thinking_replay_enabled", False)
    bare = [copy.copy(m) for m in transcript]
    for message in bare:
        message._provider_extra = None

    if path == "chat":
        done = _reply(SimpleNamespace(type="text", text="Done."))
        provider.client.messages.create, sent = _create_recorder(done, done)
        for messages in (transcript, bare):
            await provider.chat(messages=messages, model=MODEL, tools=_TOOLS)
    elif path == "chat_stream":
        for messages in (transcript, bare):
            async for _ in provider.chat_stream(messages=messages, model=MODEL, on_thinking=_read):
                pass
        sent = recorder.sent[1:]
    else:
        wrapper = FailoverLLMProvider(provider)
        for messages in (transcript, bare):
            await wrapper.chat_stream_with_tools(
                messages, tools=_TOOLS, model=MODEL, on_thinking=_read
            )
        sent = recorder.sent[1:]

    with_capture, without_capture = sent
    assert json.dumps(with_capture, sort_keys=True) == json.dumps(without_capture, sort_keys=True)
    assert with_capture["messages"][1] == _PLAIN_TURN_1


@pytest.mark.asyncio
async def test_flag_off_never_hashes_anything(replay_off, monkeypatch):
    """Captured in memory for the report, never fingerprinted: no digest, no
    running digest, across a whole tool round."""

    def _no_hashing(*_args, **_kwargs):
        raise AssertionError("hashed with ANTHROPIC_THINKING_REPLAY_ENABLED off")

    monkeypatch.setattr(replay_mod, "digest", _no_hashing)
    monkeypatch.setattr(replay_mod, "_MessagesDigest", _no_hashing)
    provider = _provider(_StreamRecorder(_TURN_1, _FINAL))
    transcript = _base_transcript()

    first = await _call(provider, transcript)
    _answer(transcript, first)
    second = await _call(provider, transcript)

    capture = first._provider_extra[EXTRA_KEY]
    assert capture.prefix is None
    assert capture.turn == ""
    assert _report(second)["reason"] == "flag_off"


@pytest.mark.asyncio
async def test_flag_off_hashes_nothing_on_chat_and_chat_stream_either(replay_off, monkeypatch):
    """The round above runs on ``chat_stream_with_tools``; ``chat()`` and
    ``chat_stream()`` capture through their own code and hash nothing either."""

    def _no_hashing(*_args, **_kwargs):
        raise AssertionError("hashed with ANTHROPIC_THINKING_REPLAY_ENABLED off")

    monkeypatch.setattr(replay_mod, "digest", _no_hashing)
    monkeypatch.setattr(replay_mod, "_MessagesDigest", _no_hashing)
    recorder = _StreamRecorder(_TURN_1, _message(_text("Done.")))
    provider = _provider(recorder)
    transcript = _base_transcript()

    _answer(transcript, await _call(provider, transcript))
    provider.client.messages.create, _sent = _create_recorder(
        _reply(
            SimpleNamespace(type="thinking", thinking="Next.", signature="sig-e"),
            SimpleNamespace(type="tool_use", id="toolu_7", name="store", input={"v": "c"}),
            stop_reason="tool_use",
        ),
        stream=recorder,
    )
    _answer(transcript, await provider.chat(messages=transcript, model=MODEL, tools=_TOOLS))
    async for _ in provider.chat_stream(messages=transcript, model=MODEL, on_thinking=_read):
        pass

    assert provider.last_trace_metadata["thinking_replay"]["reasons"] == {"flag_off": 2}


@pytest.mark.asyncio
async def test_a_tool_list_changed_by_load_tools_strips_the_blocks(replay_on):
    recorder = _StreamRecorder(_TURN_1, _FINAL)
    provider = _provider(recorder)
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))

    response = await _call(provider, transcript, tools=[*_TOOLS, _REVEALED_TOOL])

    assert recorder.sent[1]["messages"][1] == _PLAIN_TURN_1
    assert _report(response)["reason"] == "prefix_changed:tools"


@pytest.mark.asyncio
async def test_a_turn_stripped_for_a_prefix_that_came_back_is_replayed_again(replay_on):
    """A strip that can come undone is decided again by every request: tools
    withdrawn for a cooldown and then restored give the turn its blocks back."""
    recorder = _StreamRecorder(_TURN_1, _FINAL, _FINAL)
    provider = _provider(recorder)
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))
    turn = transcript[2]

    withdrawn = await _call(provider, transcript, tools=_TOOLS[:1])
    assert recorder.sent[1]["messages"][1] == _PLAIN_TURN_1
    assert _report(withdrawn)["reason"] == "prefix_changed:tools"
    assert capture_of(turn).stripped_reason == "prefix_changed:tools"

    restored = await _call(provider, transcript)

    assert recorder.sent[2]["messages"][1] == _REPLAYED_TURN_1
    assert _report(restored)["outcome"] == "replayed"


@pytest.mark.asyncio
async def test_a_stubbed_earlier_tool_result_strips_the_later_turns_only(replay_on):
    """The curation rewrote the first tool result in place: the second turn's
    prefix changed, the first turn's did not."""
    recorder = _StreamRecorder(_TURN_1, _TURN_2, _FINAL)
    provider = _provider(recorder)
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))
    _answer(transcript, await _call(provider, transcript))
    transcript[3].content = (
        "[Earlier lookup result (2 chars) cleared to keep the context focused; it is "
        "stale and re-fetchable. Re-call the tool only if you still need this exact data.]"
    )

    response = await _call(provider, transcript)

    sent = recorder.sent[2]["messages"]
    assert sent[1] == _REPLAYED_TURN_1
    assert [b["type"] for b in sent[3]["content"]] == ["tool_use"]
    report = _report(response)
    assert report["outcome"] == "partial"
    assert report["reason"] == "prefix_changed:messages"
    assert report["reasons"] == {"prefix_changed:messages": 1}


@pytest.mark.asyncio
async def test_a_compacted_transcript_strips_the_blocks(replay_on):
    """Compaction folds old turns into a summary system message and drops
    them: the merged system prompt no longer matches."""
    recorder = _StreamRecorder(_TURN_1, _TURN_2, _FINAL)
    provider = _provider(recorder)
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))
    _answer(transcript, await _call(provider, transcript))
    compacted = [
        transcript[0],
        ChatMessage(role="system", content="[Semantic compaction] earlier turns: looked a up."),
        *transcript[4:],
    ]

    response = await _call(provider, compacted)

    assert all(
        b["type"] not in ("thinking", "redacted_thinking")
        for m in recorder.sent[2]["messages"]
        if isinstance(m["content"], list)
        for b in m["content"]
    )
    assert _report(response)["reason"] == "prefix_changed:system"


@pytest.mark.asyncio
async def test_an_injected_system_nudge_sent_in_place_keeps_the_blocks(replay_on):
    """A model that takes a system message inside ``messages`` gets the nudge
    where it was appended: the top-level prompt and the turn's prefix
    are unchanged, so the turn is still re-sent with its blocks."""
    recorder = _StreamRecorder(_TURN_1, _FINAL)
    provider = _provider(recorder)
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))
    transcript.append(ChatMessage(role="system", content="You have used 60% of your steps."))

    response = await _call(provider, transcript)

    assert recorder.sent[1]["messages"][1] == _REPLAYED_TURN_1
    assert recorder.sent[1]["messages"][-1] == {
        "role": "system",
        "content": "You have used 60% of your steps.",
    }
    assert _report(response)["outcome"] == "replayed"


@pytest.mark.asyncio
async def test_an_injected_system_nudge_merged_into_the_prompt_strips_the_blocks(
    replay_on, monkeypatch
):
    """Without in-place system messages the nudge is merged into the top-level
    prompt, which is a changed prefix."""
    monkeypatch.setattr(
        "app.llm.anthropic_provider.supports_in_place_system_messages", lambda _model: False
    )
    recorder = _StreamRecorder(_TURN_1, _FINAL)
    provider = _provider(recorder)
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))
    transcript.append(ChatMessage(role="system", content="You have used 60% of your steps."))

    response = await _call(provider, transcript)

    assert recorder.sent[1]["messages"][1] == _PLAIN_TURN_1
    assert _report(response)["reason"] == "prefix_changed:system"


@pytest.mark.asyncio
async def test_a_turn_outside_the_current_tool_round_is_stripped(replay_on):
    """A human message (a steering message folded mid-run) ends the round."""
    recorder = _StreamRecorder(_TURN_1, _FINAL)
    provider = _provider(recorder)
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))
    transcript.append(ChatMessage(role="user", content="Actually, look up c too."))

    response = await _call(provider, transcript)

    assert recorder.sent[1]["messages"][1] == _PLAIN_TURN_1
    assert _report(response)["reason"] == "not_tool_round"
    # A round never resumes: the turn is retired for good.
    assert capture_of(transcript[2]).stripped_reason == "not_tool_round"


@pytest.mark.asyncio
async def test_a_turn_the_loop_rewrote_is_stripped(replay_on):
    """The argument coercion replaces ``tc.arguments`` on the transcript's own
    ToolCall: the turn is no longer what the model produced."""
    recorder = _StreamRecorder(_TURN_1, _FINAL)
    provider = _provider(recorder)
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))
    transcript[2].tool_calls[0].arguments = {"q": "A"}

    response = await _call(provider, transcript)

    assert [b["type"] for b in recorder.sent[1]["messages"][1]["content"]] == [
        "text",
        "tool_use",
    ]
    assert _report(response)["reason"] == "turn_changed"


@pytest.mark.asyncio
async def test_another_model_never_receives_the_blocks(replay_on):
    """A failover or a budget downgrade to another model: the blocks belong
    to the model that wrote them."""
    recorder = _StreamRecorder(_TURN_1, _FINAL)
    provider = _provider(recorder)
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))

    response = await _call(provider, transcript, model="claude-opus-5")

    assert recorder.sent[1]["messages"][1] == _PLAIN_TURN_1
    assert _report(response)["reason"] == "model_changed"


@pytest.mark.asyncio
async def test_a_request_with_thinking_turned_off_is_stripped(replay_on):
    recorder = _StreamRecorder(_TURN_1)
    provider = _provider(recorder)
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))
    reply = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="{}")],
        stop_reason="end_turn",
        model=MODEL,
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    provider.client.messages.create, sent = _create_recorder(reply)

    response = await provider.chat(
        messages=transcript, model=MODEL, tools=_TOOLS, disable_thinking=True
    )

    assert sent[0]["thinking"] == {"type": "disabled"}
    assert sent[0]["messages"][1] == _PLAIN_TURN_1
    assert _report(response)["reason"] == "thinking_config"


@pytest.mark.asyncio
async def test_a_disable_thinking_call_never_replays_even_in_the_adaptive_form(
    replay_on, monkeypatch
):
    """A model that refuses ``thinking.type.disabled`` is sent the adaptive
    form instead (Mythos from its first request): the call still
    asked for no thinking, so it never re-sends blocks, whatever form turned
    thinking off - even with the tools, system prompt and round unchanged."""
    monkeypatch.setattr(provider_mod, "_MODELS_REFUSING_DISABLED_THINKING", {MODEL})
    recorder = _StreamRecorder(_TURN_1)
    provider = _provider(recorder)
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))
    provider.client.messages.create, sent = _create_recorder(
        _reply(SimpleNamespace(type="text", text="{}"))
    )

    response = await provider.chat(
        messages=transcript, model=MODEL, tools=_TOOLS, disable_thinking=True
    )

    assert sent[0]["thinking"] == {"type": "adaptive"}
    assert sent[0]["messages"][1] == _PLAIN_TURN_1
    assert _report(response)["reason"] == "thinking_config"


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["claude-opus-4-8", "claude-sonnet-4-6"])
async def test_a_model_that_does_not_think_by_default_gets_no_replay(replay_on, model):
    """A budget downgrade to a model that does not think: the turn goes out
    bare, and the report still says why."""
    recorder = _StreamRecorder(_TURN_1, _FINAL)
    provider = _provider(recorder)
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))

    response = await _call(provider, transcript, model=model)

    assert recorder.sent[1]["messages"][1] == _PLAIN_TURN_1
    assert _report(response)["reason"] == "model_changed"
    assert response._provider_extra is None


@pytest.mark.asyncio
async def test_chat_stream_sends_no_tools_so_it_never_replays(replay_on):
    recorder = _StreamRecorder(_TURN_1, _FINAL)
    provider = _provider(recorder)
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))

    async for _ in provider.chat_stream(messages=transcript, model=MODEL, on_thinking=_read):
        pass

    assert recorder.sent[1]["messages"][1] == _PLAIN_TURN_1
    assert provider.last_trace_metadata["thinking_replay"]["reason"] == "prefix_changed:tools"


@pytest.mark.asyncio
async def test_blocks_that_would_overflow_the_window_are_dropped_not_fatal(replay_on):
    big = _message(
        _thinking(["Long plan."], "S" * 90_000), _tool("toolu_1", "lookup", {"q": "a"})
    )
    recorder = _StreamRecorder(big, _FINAL)
    provider = _provider(recorder)
    provider.context_windows = {MODEL: 20_000}
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))

    response = await _call(provider, transcript)

    assert [b["type"] for b in recorder.sent[1]["messages"][1]["content"]] == ["tool_use"]
    assert _report(response)["reason"] == "context_budget"


# Option A safety: the loop gates never see the blocks, so the provider
# must never send a request that fits only without them. On every request path
# a near-limit transcript - the plain request fits, the blocks would take it
# over the threshold - goes out plain and succeeds, and a plain request over
# the threshold is refused exactly as without the replay.
_WINDOW = 20_000  # tokens: 95% = 57,000 chars at 3 chars per token
_HUGE_SIGNATURE = "S" * 90_000  # 67,500 chars of re-sent reasoning


def _huge_turn() -> list:
    return _message(
        _thinking(["Long plan."], _HUGE_SIGNATURE), _tool("toolu_1", "lookup", {"q": "a"})
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["chat", "chat_stream", "chat_stream_with_tools"])
async def test_every_path_drops_blocks_a_near_limit_request_cannot_carry(replay_on, path):
    # ``chat_stream`` sends no tools, so its capture is taken without them too.
    recorder = _StreamRecorder(_huge_turn(), _FINAL)
    provider = _provider(recorder)
    provider.context_windows = {MODEL: _WINDOW}
    transcript = _base_transcript()
    first = await provider.chat_stream_with_tools(
        messages=list(transcript),
        tools=None if path == "chat_stream" else _TOOLS,
        model=MODEL,
        on_thinking=_read,
    )
    _answer(transcript, first)
    # The plain request fits; with the blocks it would not.
    assert sum(len(m.content or "") for m in transcript) < _WINDOW
    assert len(_HUGE_SIGNATURE) * 3 // 4 > _WINDOW * 3 * 0.95

    if path == "chat":
        provider.client.messages.create, sent = _create_recorder(
            _reply(SimpleNamespace(type="text", text="Done."))
        )
        response = await provider.chat(messages=transcript, model=MODEL, tools=_TOOLS)
        request, report = sent[0], _report(response)
    elif path == "chat_stream":
        chunks = [
            c
            async for c in provider.chat_stream(
                messages=transcript, model=MODEL, on_thinking=_read
            )
        ]
        assert chunks == ["Done."]
        request, report = recorder.sent[1], provider.last_trace_metadata["thinking_replay"]
    else:
        response = await _call(provider, transcript)
        assert response.content == "Done."
        request, report = recorder.sent[1], _report(response)

    turn = next(m for m in request["messages"] if m["role"] == "assistant")
    assert all(b["type"] not in ("thinking", "redacted_thinking") for b in turn["content"])
    assert report["reason"] == "context_budget"


@pytest.mark.asyncio
async def test_a_plain_request_over_the_window_is_refused_whatever_it_could_replay(
    replay_on,
):
    """The blocks never decide the refusal: the provider's own check runs on
    the plain request first, exactly as without the replay."""
    from app.llm.base import ContextOverflowError

    recorder = _StreamRecorder(_huge_turn())
    provider = _provider(recorder)
    provider.context_windows = {MODEL: _WINDOW}
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript), result="r" * 60_000)

    with pytest.raises(ContextOverflowError):
        await _call(provider, transcript)
    assert len(recorder.sent) == 1


@pytest.mark.asyncio
async def test_the_loop_gates_and_a_max_tokens_continuation_never_need_the_blocks_measured(
    replay_on,
):
    """End to end through ``run_agent_loop`` on a near-limit transcript: the
    gates measure the text alone (no compaction for blocks), the tool-round
    request and the max-tokens continuation both go out plain because the
    provider shed the blocks, and the turn completes."""
    from app.tools.base import CallableToolExecutor, CompositeToolExecutor

    provider = _provider()
    provider.context_windows = {MODEL: _WINDOW}
    provider.client.messages.create, sent = _create_recorder(
        _reply(
            SimpleNamespace(type="thinking", thinking="Long plan.", signature=_HUGE_SIGNATURE),
            SimpleNamespace(type="tool_use", id="toolu_1", name="lookup", input={"q": "a"}),
            stop_reason="tool_use",
        ),
        _reply(SimpleNamespace(type="text", text="Part one"), stop_reason="max_tokens"),
        _reply(SimpleNamespace(type="text", text=" and done.")),
    )
    callables = CallableToolExecutor()

    async def _lookup(q: str) -> str:
        return f"found {q}"

    callables.register("lookup", _lookup)
    executor = CompositeToolExecutor()
    executor.add_tools([_TOOLS[0]], callables)
    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    response = await run_agent_loop(
        llm=provider,
        messages=_base_transcript(),
        tools=[_TOOLS[0]],
        executor=executor,
        model=MODEL,
        max_iterations=4,
        on_event=_on_event,
    )

    assert response.content.endswith("and done.")
    assert len(sent) == 3
    for request in sent[1:]:
        for message in request["messages"]:
            if isinstance(message["content"], list):
                assert all(b["type"] != "thinking" for b in message["content"])
    assert not any(e.get("status") == "context_compaction" for e in events)


# ---------------------------------------------------------------------------
# Fail safe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refused_replay_is_retried_once_without_the_blocks(replay_on):
    recorder = _StreamRecorder(_TURN_1, _status_error(400, _SIGNATURE_REFUSAL), _FINAL)
    provider = _provider(recorder)
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))

    response = await _call(provider, transcript)

    assert response.content == "Done."
    assert recorder.sent[1]["messages"][1] == _REPLAYED_TURN_1
    assert recorder.sent[2]["messages"][1] == _PLAIN_TURN_1
    report = _report(response)
    assert report["reason"] == "retry_without"
    assert report["replayed_blocks"] == 0
    # The API's own message, not the SDK's rendering of the whole body.
    assert report["retry_error"] == f"BadRequestError: {_SIGNATURE_REFUSAL}"


_SIZE_REFUSALS = [
    pytest.param(
        _status_error(400, "prompt is too long: 1000456 tokens > 1000000 maximum"),
        id="prompt-too-long",
    ),
    pytest.param(_status_error(413, "Request exceeds the maximum size"), id="request-too-large"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", _SIZE_REFUSALS)
async def test_a_request_the_blocks_took_over_the_limit_is_sent_again_without_them(
    replay_on, exc
):
    """The pre-flight estimate of a re-sent block is chars-based: when the API
    still finds the request too large, the blocks may be what tipped it over,
    and the setting must never fail a turn."""
    recorder = _StreamRecorder(_TURN_1, exc, _FINAL)
    provider = _provider(recorder)
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))

    response = await _call(provider, transcript)

    assert response.content == "Done."
    assert recorder.sent[2]["messages"][1] == _PLAIN_TURN_1
    assert _report(response)["reason"] == "retry_without"


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", _SIZE_REFUSALS)
async def test_a_size_refusal_of_a_request_that_replayed_nothing_propagates(replay_off, exc):
    recorder = _StreamRecorder(_TURN_1, exc)
    provider = _provider(recorder)
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))

    with pytest.raises(anthropic.APIStatusError):
        await _call(provider, transcript)
    assert len(recorder.sent) == 2


@pytest.mark.asyncio
async def test_a_refused_turn_is_never_re_sent_its_blocks(replay_on):
    """The API refused the turn's blocks once: it is retired, so the next
    request of the round sends it bare instead of paying another 400, while a
    later turn - captured under the bare prefix - still gets its own back."""
    recorder = _StreamRecorder(_TURN_1, _status_error(400, _SIGNATURE_REFUSAL), _TURN_2, _FINAL)
    provider = _provider(recorder)
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))
    _answer(transcript, await _call(provider, transcript))

    response = await _call(provider, transcript)

    assert len(recorder.sent) == 4
    sent = recorder.sent[3]["messages"]
    assert sent[1] == _PLAIN_TURN_1
    assert sent[3]["content"][0]["type"] == "thinking"
    report = _report(response)
    assert report["outcome"] == "partial"
    assert report["reasons"] == {"retry_without": 1}
    assert "retry_error" not in report
    assert capture_of(transcript[2]).stripped_reason == "retry_without"


@pytest.mark.asyncio
async def test_chat_retries_a_refused_replay_once(replay_on):
    recorder = _StreamRecorder(_TURN_1)
    provider = _provider(recorder)
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))
    reply = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="Done.")],
        stop_reason="end_turn",
        model=MODEL,
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    provider.client.messages.create, sent = _create_recorder(
        _status_error(400, "thinking blocks cannot be modified"), reply
    )

    response = await provider.chat(messages=transcript, model=MODEL, tools=_TOOLS)

    assert response.content == "Done."
    assert sent[0]["messages"][1] == _REPLAYED_TURN_1
    assert sent[1]["messages"][1] == _PLAIN_TURN_1
    assert _report(response)["reason"] == "retry_without"


@pytest.mark.asyncio
async def test_chat_stream_retries_a_refused_replay_once(replay_on):
    """``chat_stream`` sends no tools, so it only replays when the captured
    turn was produced without tools too - forced here to reach the path."""
    recorder = _StreamRecorder(
        _TURN_1, _status_error(400, _SIGNATURE_REFUSAL), _message(_text("Done."))
    )
    provider = _provider(recorder)
    transcript = _base_transcript()
    first = await provider.chat_stream_with_tools(
        messages=list(transcript), tools=None, model=MODEL, on_thinking=_read
    )
    _answer(transcript, first)

    chunks = [
        c async for c in provider.chat_stream(messages=transcript, model=MODEL, on_thinking=_read)
    ]

    assert chunks == ["Done."]
    assert recorder.sent[1]["messages"][1] == _REPLAYED_TURN_1
    assert recorder.sent[2]["messages"][1] == _PLAIN_TURN_1
    assert provider.last_trace_metadata["thinking_replay"]["reason"] == "retry_without"


@pytest.mark.asyncio
async def test_a_400_naming_the_rewritten_turn_is_a_refusal_whatever_its_wording(replay_on):
    """The phrasing of a refusal is not known in advance; the turn it names
    is. An error on a turn the replay laid out again is the replay's."""
    recorder = _StreamRecorder(
        _TURN_1, _status_error(400, "messages.1.content.0.signature: Invalid signature"), _FINAL
    )
    provider = _provider(recorder)
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))

    response = await _call(provider, transcript)

    assert response.content == "Done."
    assert recorder.sent[2]["messages"][1] == _PLAIN_TURN_1
    assert _report(response)["reason"] == "retry_without"


@pytest.mark.asyncio
async def test_a_400_naming_another_message_is_not_a_refusal(replay_on):
    recorder = _StreamRecorder(
        _TURN_1, _status_error(400, "messages.0.content: text content blocks must be non-empty")
    )
    provider = _provider(recorder)
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))

    with pytest.raises(anthropic.BadRequestError):
        await _call(provider, transcript)
    assert len(recorder.sent) == 2


@pytest.mark.asyncio
async def test_any_other_400_on_a_replaying_request_is_not_swallowed(replay_on):
    recorder = _StreamRecorder(
        _TURN_1, _status_error(400, "tools.0.input_schema: does not support oneOf")
    )
    provider = _provider(recorder)
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))

    with pytest.raises(anthropic.BadRequestError):
        await _call(provider, transcript)
    assert len(recorder.sent) == 2


@pytest.mark.asyncio
async def test_a_thinking_400_on_a_request_that_replayed_nothing_is_not_retried(replay_off):
    recorder = _StreamRecorder(_TURN_1, _status_error(400, _SIGNATURE_REFUSAL))
    provider = _provider(recorder)
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))

    with pytest.raises(anthropic.BadRequestError):
        await _call(provider, transcript)
    assert len(recorder.sent) == 2


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_status_error(400, _SIGNATURE_REFUSAL), True),
        (
            _status_error(
                400,
                "messages.1.content.0.thinking: `thinking` or `redacted_thinking` blocks in "
                "the latest assistant message cannot be modified. These blocks must remain "
                "as they were in the original response.",
            ),
            True,
        ),
        (_status_error(400, "messages.1.content.0.thinking.signature: Field required"), True),
        (_status_error(400, "messages.3.content.0.redacted_thinking.data: invalid value"), True),
        (_status_error(400, "Invalid signature in thinking block"), True),
        (_status_error(400, "Invalid `redacted_thinking` block"), True),
        (_status_error(400, "thinking blocks cannot be modified"), True),
        # Any other 400 keeps its normal path, whatever words it carries.
        (_status_error(400, "tools.0.input_schema: does not support oneOf"), False),
        (_status_error(400, "tools.3.input_schema.properties.signature: invalid type"), False),
        (_status_error(400, "tools.1.name: 'thinking_helper' is not a valid name"), False),
        (
            _status_error(400, "tools.0.input_schema.properties.redacted_thinking: invalid type"),
            False,
        ),
        (
            _status_error(400, "tools.2.input_schema.properties.thinking.signature: invalid type"),
            False,
        ),
        (
            _status_error(
                400,
                '"thinking.type.disabled" is not supported for this model. Use '
                '"thinking.type.adaptive" and "output_config.effort" to control thinking behavior.',
            ),
            False,
        ),
        (_status_error(529, "Overloaded: thinking blocks"), False),
        (ValueError("thinking blocks"), False),
    ],
)
def test_rejects_replayed_blocks(exc, expected):
    assert rejects_replayed_blocks(exc) is expected


def test_only_the_structured_error_message_is_read():
    """The SDK folds the whole body into ``str(exc)``; a thinking word in the
    rest of the body must not turn an unrelated 400 into a replay refusal."""
    body = {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": "max_tokens: too large"},
        "request": {"note": "thinking blocks"},
    }
    response = httpx.Response(
        400, json=body, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    exc = anthropic.BadRequestError(f"Error code: 400 - {body}", response=response, body=body)
    assert "thinking blocks" in str(exc)
    assert rejects_replayed_blocks(exc) is False


def _unstructured_400(text: str) -> anthropic.BadRequestError:
    """A 400 with no ``error.message`` (a proxy page, a malformed body): only
    the SDK's rendering carries the words."""
    body = {"detail": text}
    response = httpx.Response(
        400, json=body, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    return anthropic.BadRequestError(f"Error code: 400 - {body}", response=response, body=body)


@pytest.mark.parametrize(
    "text",
    [
        "thinking blocks cannot be modified",
        "messages.1.content.0: invalid",
        "prompt is too long: 1000456 tokens > 1000000 maximum",
    ],
)
def test_a_400_without_a_structured_message_is_never_a_replay_refusal(text):
    exc = _unstructured_400(text)
    assert text in str(exc)
    assert rejects_replayed_blocks(exc) is False
    assert replay_mod._is_size_refusal(exc) is False


@pytest.mark.asyncio
async def test_an_unstructured_400_on_a_replaying_request_is_not_retried(replay_on):
    """The classifiers read ``body.error.message`` only: a proxy 400 whose
    other fields name a thinking block or the replayed turn propagates."""
    recorder = _StreamRecorder(
        _TURN_1, _unstructured_400("messages.1.content.0.thinking.signature: thinking blocks")
    )
    provider = _provider(recorder)
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))

    with pytest.raises(anthropic.BadRequestError):
        await _call(provider, transcript)
    assert len(recorder.sent) == 2


def test_retry_error_is_the_structured_message_or_the_status_alone():
    """What the trace and the log line say: the API's own message, or - when
    the body carries none - the error and its HTTP status, never the body."""
    replay = replay_mod.ThinkingReplay(model=MODEL, thinking=None, tools=None, enabled=True)
    replay._stripped = []
    replay.retry_without(_status_error(400, _SIGNATURE_REFUSAL))
    assert replay._retry_error == f"BadRequestError: {_SIGNATURE_REFUSAL}"

    replay.retry_without(_unstructured_400("upstream proxy error"))
    assert replay._retry_error == "BadRequestError: HTTP 400 (no structured error message)"


# ---------------------------------------------------------------------------
# Error shapes, as the SDK itself raises them
# ---------------------------------------------------------------------------

_SDK_BASE_URL = "https://api.anthropic.com"
# The words every classifier looks for, as a user may well type them.
_TRIGGER_WORDS = (
    "Why can thinking blocks not be modified, what does 'prompt is too long' mean, "
    "and what is messages.1.content?"
)


def _sdk_client(handler) -> anthropic.AsyncAnthropic:
    """The real SDK client over an in-memory transport: no socket is opened."""
    return anthropic.AsyncAnthropic(
        api_key="test-key",
        base_url=_SDK_BASE_URL,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _sdk_error(response: httpx.Response) -> anthropic.APIStatusError:
    """The exception the SDK raises for *response*, built by the SDK itself."""
    client = anthropic.Anthropic(
        api_key="test-key",
        base_url=_SDK_BASE_URL,
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda _request: response)),
    )
    with client, pytest.raises(anthropic.APIStatusError) as info:
        client.messages.create(
            model=MODEL, max_tokens=1, messages=[{"role": "user", "content": "hi"}]
        )
    return info.value


def _anthropic_body(error_type: str, message: str) -> dict:
    return {"type": "error", "error": {"type": error_type, "message": message}, "request_id": "req_1"}


def _json_response(status: int, body: object):
    return lambda: httpx.Response(status, json=body)


def _page_response(status: int, text: str, content_type: str):
    return lambda: httpx.Response(status, text=text, headers={"content-type": content_type})


_LITELLM_MESSAGE = "litellm.BadRequestError: AnthropicException - " + json.dumps(
    _anthropic_body("invalid_request_error", _SIGNATURE_REFUSAL)
)
_NGINX_413 = (
    "<html><head><title>413 Request Entity Too Large</title></head><body><center>"
    "<h1>413 Request Entity Too Large</h1></center><hr><center>nginx</center></body></html>"
)

_ERROR_SHAPES = [
    # The API's own envelope: the structured message decides.
    pytest.param(
        _json_response(400, _anthropic_body("invalid_request_error", _SIGNATURE_REFUSAL)),
        _SIGNATURE_REFUSAL,
        True,
        id="anthropic-400-signature-refusal",
    ),
    pytest.param(
        _json_response(
            400, _anthropic_body("invalid_request_error", "prompt is too long: 1000456 tokens > 1000000 maximum")
        ),
        "prompt is too long: 1000456 tokens > 1000000 maximum",
        True,
        id="anthropic-400-prompt-too-long",
    ),
    pytest.param(
        _json_response(413, _anthropic_body("request_too_large", "Request exceeds the maximum size")),
        "Request exceeds the maximum size",
        True,
        id="anthropic-413",
    ),
    pytest.param(
        _json_response(
            400, _anthropic_body("invalid_request_error", "tools.0.input_schema: does not support oneOf")
        ),
        "tools.0.input_schema: does not support oneOf",
        False,
        id="anthropic-400-other",
    ),
    # A gateway that re-wraps the API's error in the same envelope (LiteLLM).
    pytest.param(
        _json_response(
            400, {"error": {"message": _LITELLM_MESSAGE, "type": None, "param": None, "code": "400"}}
        ),
        _LITELLM_MESSAGE,
        True,
        id="litellm-400-wrapped-refusal",
    ),
    # No structured message: whatever the body says is never classified -
    # an echo of the request, a proxy page, another envelope.
    pytest.param(
        _json_response(
            400,
            {"detail": [{"msg": "blocked", "input": {"messages": [{"role": "user", "content": _TRIGGER_WORDS}]}}]},
        ),
        "",
        False,
        id="proxy-400-json-echo",
    ),
    pytest.param(
        _page_response(400, f"<html><body><pre>{_TRIGGER_WORDS}</pre></body></html>", "text/html"),
        "",
        False,
        id="proxy-400-html-echo",
    ),
    pytest.param(
        _json_response(400, {"error": "thinking blocks cannot be modified"}),
        "",
        False,
        id="proxy-400-error-as-string",
    ),
    pytest.param(
        _page_response(400, _SIGNATURE_REFUSAL, "text/plain"),
        "",
        False,
        id="proxy-400-plain-text",
    ),
    pytest.param(
        _json_response(400, _anthropic_body("invalid_request_error", ["thinking blocks"])),  # type: ignore[arg-type]
        "",
        False,
        id="400-message-not-a-string",
    ),
    pytest.param(
        _json_response(400, [{"error": {"message": _SIGNATURE_REFUSAL}}]),
        "",
        False,
        id="400-json-list-body",
    ),
    # A 413 is a size refusal whatever its body.
    pytest.param(_page_response(413, _NGINX_413, "text/html"), "", True, id="proxy-413-page"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("response", "message", "refused"), _ERROR_SHAPES)
async def test_every_error_shape_is_classified_on_its_structured_message(
    replay_on, response, message, refused
):
    """Each error as the SDK raises it: the message the classifiers read, and
    whether a request that re-sent blocks is retried without them. A refusal a
    proxy re-wraps in another envelope is not recognised - the price of never
    reading a body that can echo the request."""
    exc = _sdk_error(response())
    assert replay_mod.structured_error_message(exc) == message

    recorder = _StreamRecorder(_TURN_1, exc, _FINAL)
    provider = _provider(recorder)
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))

    if refused:
        assert (await _call(provider, transcript)).content == "Done."
        assert recorder.sent[2]["messages"][1] == _PLAIN_TURN_1
    else:
        with pytest.raises(anthropic.APIStatusError):
            await _call(provider, transcript)
        assert len(recorder.sent) == 2
        assert capture_of(transcript[2]).stripped_reason is None
    assert recorder.sent[1]["messages"][1] == _REPLAYED_TURN_1


@pytest.mark.asyncio
async def test_a_proxy_400_echoing_the_users_own_words_is_not_a_refusal(replay_on):
    """The user asks about the very words the classifiers look for, and a proxy
    answers 400 with the request it received echoed back and no
    ``error.message``: the SDK's rendering of that body reads like a refusal of
    the replayed blocks. Only the structured message is read, so the 400
    propagates on the first request and the turn keeps its blocks."""
    provider = _provider(_StreamRecorder(_TURN_1))
    transcript = [
        ChatMessage(role="system", content="You are a helpful agent."),
        ChatMessage(role="user", content=_TRIGGER_WORDS),
    ]
    _answer(transcript, await _call(provider, transcript))
    received: list[dict] = []

    def proxy(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        received.append(sent)
        return httpx.Response(400, json={"detail": "Rejected by policy", "request": sent})

    provider.client = _sdk_client(proxy)
    try:
        with pytest.raises(anthropic.BadRequestError) as info:
            await _call(provider, transcript)
    finally:
        await provider.client.close()

    assert "thinking blocks" in str(info.value)
    assert len(received) == 1
    assert received[0]["messages"][1] == _REPLAYED_TURN_1
    assert capture_of(transcript[2]).stripped_reason is None


@pytest.mark.asyncio
async def test_an_error_event_mid_stream_is_never_a_refusal(replay_on):
    """An ``error`` event in a stream that opened with 200 is raised by the SDK
    with that 200 response, whatever its payload says: the API refuses
    replayed blocks when it validates the request, before any event, so this
    propagates untouched."""
    provider = _provider(_StreamRecorder(_TURN_1))
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))
    payload = json.dumps(_anthropic_body("invalid_request_error", _SIGNATURE_REFUSAL))
    received: list[dict] = []

    def stream(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"event: error\ndata: {payload}\n\n".encode(),
        )

    provider.client = _sdk_client(stream)
    try:
        with pytest.raises(anthropic.APIStatusError) as info:
            await _call(provider, transcript)
    finally:
        await provider.client.close()

    assert info.value.status_code == 200
    assert replay_mod.structured_error_message(info.value) == _SIGNATURE_REFUSAL
    assert len(received) == 1
    assert received[0]["messages"][1] == _REPLAYED_TURN_1
    assert capture_of(transcript[2]).stripped_reason is None


@pytest.mark.asyncio
async def test_retry_error_never_carries_a_body_without_a_structured_message(replay_on, caplog):
    """A 413 is retried without the blocks whatever its body, and a proxy's
    page carries no structured message - here it echoes the user's words: the
    trace metadata and the log line name the error and its status only."""
    page = f"<html><body><h1>413 Request Entity Too Large</h1><pre>{_TRIGGER_WORDS}</pre></body></html>"
    exc = _sdk_error(httpx.Response(413, text=page, headers={"content-type": "text/html"}))
    recorder = _StreamRecorder(_TURN_1, exc, _FINAL)
    provider = _provider(recorder)
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))

    with caplog.at_level(logging.WARNING, logger=replay_mod.__name__):
        response = await _call(provider, transcript)

    assert response.content == "Done."
    assert recorder.sent[2]["messages"][1] == _PLAIN_TURN_1
    report = _report(response)
    assert report["reason"] == "retry_without"
    assert report["retry_error"] == "RequestTooLargeError: HTTP 413 (no structured error message)"
    assert "retrying the request once without them" in caplog.text
    assert _TRIGGER_WORDS not in caplog.text
    assert "<html>" not in caplog.text


# ---------------------------------------------------------------------------
# Round structure
# ---------------------------------------------------------------------------


def _assistant(tool_id: str) -> dict:
    return {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": tool_id, "name": "lookup", "input": {}}],
    }


def _results(tool_id: str) -> dict:
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}],
    }


def test_current_tool_round_is_the_trailing_unbroken_run():
    user = {"role": "user", "content": "go"}
    conversation = [user, _assistant("a"), _results("a"), _assistant("b"), _results("b")]
    assert current_tool_round(conversation) == frozenset({1, 3})

    steered = [*conversation, {"role": "user", "content": "and c"}]
    assert current_tool_round(steered) == frozenset()

    broken = [user, _assistant("a"), _results("a"), user, _assistant("b"), _results("b")]
    assert current_tool_round(broken) == frozenset({4})


def test_plain_assistant_content_is_the_builders_rendering():
    call = ToolCall(id="toolu_1", name="lookup", arguments={"q": "a"})
    assert plain_assistant_content("Hi", [call]) == [
        {"type": "text", "text": "Hi"},
        {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "a"}},
    ]
    assert plain_assistant_content("", None) == ""
    assert plain_assistant_content("Only text", None) == [{"type": "text", "text": "Only text"}]


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------


def test_the_payload_measure_counts_replayed_blocks():
    conversation = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "x" * 10, "signature": "s" * 400},
                {"type": "redacted_thinking", "data": "d" * 80},
            ],
        },
    ]
    text_chars, _images, _bytes = AnthropicProvider._measure_anthropic_payload(None, conversation)
    assert text_chars == 2 + 300 + 60


def test_the_replayed_blocks_are_never_registered_on_the_loop_gates():
    """Option A: the replayed blocks are the provider's own - it
    counts them in its overflow check and sheds what would not fit - so they
    are never registered with the provider-extras hook, whose every
    reader is a loop gate that prunes, compacts, stops the run or raises."""
    from app.llm import base as llm_base

    assert EXTRA_KEY not in llm_base._PROVIDER_EXTRA_COUNTERS
    assert not hasattr(replay_mod, "thinking_extra_chars")


@pytest.mark.asyncio
async def test_no_loop_gate_measure_sees_the_replayable_blocks(replay_on):
    """With the replay on and a turn whose blocks the next request may
    re-send, the one measure every loop gate reads is the turn's text alone."""
    provider = _provider(_StreamRecorder(_TURN_1))
    transcript = _base_transcript()
    _answer(transcript, await _call(provider, transcript))
    turn = transcript[2]

    assert capture_of(turn).prefix is not None
    assert provider_extra_chars(turn) == 0
    assert message_payload_chars(turn) == len("Checking.")


class _StubLLM(BaseLLMProvider):
    provider_name = "stub"
    context_window = 10_000

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        return ChatResponse(content="final answer", model=model or "stub")

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        yield "final answer"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


# ---------------------------------------------------------------------------
# Trace metadata
# ---------------------------------------------------------------------------


def _local_root() -> dict:
    return {
        "trace": None,
        "local_trace_id": uuid.uuid4(),
        "local_spans": [],
        "local_span_counter": 0,
    }


def test_the_report_lands_in_the_durable_generation_metadata():
    root = _local_root()
    gen_ctx = lft.start_llm_generation(root, model=MODEL, messages=[])
    report = {"thinking_replay": {"outcome": "replayed", "replayed_blocks": 2}}

    _record_request_trace_metadata(gen_ctx, ChatResponse(content="", model=MODEL, trace_metadata=report))
    lft.end_generation(gen_ctx, output="ok")

    (span,) = root["local_spans"]
    assert span["metadata"]["thinking_replay"] == report["thinking_replay"]
    assert span["metadata"]["observation_kind"] == "llm_generation"


class _ReportingLLM(_StubLLM):
    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        return ChatResponse(
            content="final answer",
            model=model or "stub",
            trace_metadata={"thinking_replay": {"outcome": "stripped", "reason": "flag_off"}},
        )


@pytest.mark.asyncio
async def test_the_agent_loop_records_the_report_on_its_llm_generation():
    root = _local_root()
    token = lft.set_current_trace_ctx(root)
    try:
        await run_agent_loop(
            llm=_ReportingLLM(),
            messages=[ChatMessage(role="user", content="question")],
            tools=None,
            max_iterations=1,
        )
    finally:
        lft.reset_current_trace_ctx(token)

    generations = [s for s in root["local_spans"] if s["type"] == "GENERATION"]
    assert generations
    assert generations[0]["metadata"]["thinking_replay"] == {
        "outcome": "stripped",
        "reason": "flag_off",
    }


class _ContinuationLLM(_StubLLM):
    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        return ChatResponse(
            content=" the rest.",
            model="stub",
            trace_metadata={"thinking_replay": {"outcome": "stripped", "reason": "not_tool_round"}},
        )


@pytest.mark.asyncio
async def test_a_max_tokens_continuation_records_the_report_on_its_generation():
    root = _local_root()

    response = await _continue_truncated_response(
        _ContinuationLLM(),
        [ChatMessage(role="user", content="question")],
        ChatResponse(content="Partial", model="stub", stop_reason="max_tokens"),
        on_token=None,
        on_event=None,
        model=None,
        temperature=0.7,
        max_tokens=64,
        max_session_chars=100_000,
        llm_extra={},
        merge_usage=lambda _usage, served_model=None: None,
        parent_iter_ctx=root,
    )

    assert response.content == "Partial the rest."
    (generation,) = [s for s in root["local_spans"] if s["type"] == "GENERATION"]
    assert generation["metadata"]["thinking_replay"]["reason"] == "not_tool_round"


class _StreamOnlyLLM(_StubLLM):
    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        self.last_provider_extra = {EXTRA_KEY: "capture"}
        self.last_trace_metadata = {"thinking_replay": {"outcome": "stripped"}}
        yield "streamed"


@pytest.mark.asyncio
async def test_streaming_chat_carries_the_chat_stream_companions():
    response = await _streaming_chat(
        _StreamOnlyLLM(), [ChatMessage(role="user", content="q")], None, max_tokens=64
    )
    assert response._provider_extra == {EXTRA_KEY: "capture"}
    assert response.trace_metadata == {"thinking_replay": {"outcome": "stripped"}}

    loose = MagicMock()
    loose.provider_name = "mock"

    async def _gen(**_kwargs):
        yield "x"

    loose.chat_stream = _gen
    loose.last_stop_reason = None
    loose.last_usage = None
    response = await _streaming_chat(loose, [ChatMessage(role="user", content="q")], None, max_tokens=64)
    # A test double's auto-attributes are not a capture or a report.
    assert response._provider_extra is None
    assert response.trace_metadata is None


@pytest.mark.asyncio
async def test_the_failover_wrapper_proxies_the_chat_stream_companions():
    primary = _StreamOnlyLLM()
    wrapper = FailoverLLMProvider(primary)

    async for _ in wrapper.chat_stream([ChatMessage(role="user", content="q")]):
        pass

    assert wrapper.last_provider_extra == {EXTRA_KEY: "capture"}
    assert wrapper.last_trace_metadata == {"thinking_replay": {"outcome": "stripped"}}


def test_the_module_never_imports_the_display_joiner():
    """The reasoning pane's joiner edits display text; the replayed blocks
    must never pass through it."""
    assert not hasattr(replay_mod, "_ThinkingBlockJoiner")
