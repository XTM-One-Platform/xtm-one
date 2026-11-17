"""Mid-run system messages stay in place on the models that accept it.

The agentic loop appends system messages mid-run (loop-breaker, step-budget,
wall-clock and wind-down nudges, retries, notices). The Anthropic builder
merged every one of them into the top-level ``system``, and changing
``system`` invalidates the cached conversation: on a ~40K-token prompt the
next request's time to first token went from ~1.25 s to ~1.9 s, plus a full
cache write. The Claude 5 family and Opus 4.8 accept ``role: system`` inside
``messages`` (verified against the API), so a nudge is sent where it sits
when the API's placement rule holds, and merged as before otherwise - and on
every older model.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import MagicMock

import anthropic
import httpx
import pytest

from app.llm.anthropic_provider import (
    AnthropicProvider,
    _fold_in_place_system,
    _refuses_in_place_system,
)
from app.llm.anthropic_thinking_replay import current_tool_round
from app.llm.base import ChatMessage
from app.llm.model_capabilities import supports_in_place_system_messages
from app.tools.base import ToolCall

_NUDGE = "You are at 60% of your step budget: finish or report."


def _call(call_id: str) -> ToolCall:
    return ToolCall(id=call_id, name="lookup", arguments={"q": call_id})


def _tool(call_id: str, text: str = "result") -> ChatMessage:
    return ChatMessage(role="tool", content=text, tool_call_id=call_id, name="lookup")


def _assert_api_placement(conversation: list[dict]) -> None:
    """The API's rule for an in-place system message."""
    for index, entry in enumerate(conversation):
        if entry["role"] != "system":
            continue
        assert index > 0 and conversation[index - 1]["role"] in ("user", "system")
        following = conversation[index + 1] if index + 1 < len(conversation) else None
        assert following is None or following["role"] in ("assistant", "system")


async def _build(messages: list[ChatMessage], *, in_place: bool):
    provider = AnthropicProvider(api_key="test-key")
    return await provider._build_anthropic_messages(messages, in_place_system=in_place)


# ---------------------------------------------------------------------------
# Which models
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-sonnet-5", True),
        ("claude-opus-5", True),
        ("claude-opus-5-5", True),
        ("claude-fable-5", True),
        ("claude-fable-5-1", True),
        ("claude-opus-4-8", True),
        ("claude-opus-4-7", False),
        ("claude-sonnet-4-6", False),
        ("claude-sonnet-4-5", False),
        ("claude-haiku-4-5", False),
        ("claude-mythos-5", False),
        ("gpt-5", False),
        (None, False),
    ],
)
def test_only_verified_models_take_in_place_system_messages(model, expected):
    assert supports_in_place_system_messages(model) is expected


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


async def test_a_nudge_after_tool_results_stays_in_place():
    messages = [
        ChatMessage(role="system", content="You are an analyst."),
        ChatMessage(role="user", content="Look it up."),
        ChatMessage(role="assistant", content="", tool_calls=[_call("c1")]),
        _tool("c1"),
        ChatMessage(role="system", content=_NUDGE),
    ]

    system, conversation = await _build(messages, in_place=True)

    assert system == "You are an analyst."
    assert conversation[-1] == {"role": "system", "content": _NUDGE}
    _assert_api_placement(conversation)


async def test_nudges_in_history_stay_in_place_before_the_next_assistant_turn():
    messages = [
        ChatMessage(role="system", content="You are an analyst."),
        ChatMessage(role="user", content="Look it up."),
        ChatMessage(role="assistant", content="", tool_calls=[_call("c1")]),
        _tool("c1"),
        ChatMessage(role="system", content=_NUDGE),
        ChatMessage(role="system", content="Be terse."),
        ChatMessage(role="assistant", content="", tool_calls=[_call("c2")]),
        _tool("c2"),
    ]

    system, conversation = await _build(messages, in_place=True)

    assert system == "You are an analyst."
    assert [e["role"] for e in conversation] == ["user", "assistant", "user", "system", "system", "assistant", "user"]
    _assert_api_placement(conversation)


@pytest.mark.parametrize(
    "messages",
    [
        pytest.param(
            [
                ChatMessage(role="user", content="Look them up."),
                ChatMessage(role="assistant", content="", tool_calls=[_call("c1"), _call("c2")]),
                _tool("c1"),
                ChatMessage(role="system", content=_NUDGE),
                _tool("c2"),
            ],
            id="inside a batch of tool results",
        ),
        pytest.param(
            [
                ChatMessage(role="user", content="Write the records."),
                ChatMessage(role="assistant", content="Let me write them now."),
                ChatMessage(role="system", content=_NUDGE),
            ],
            id="after assistant text",
        ),
        pytest.param(
            [
                ChatMessage(role="user", content="Hello."),
                ChatMessage(role="system", content=_NUDGE),
                ChatMessage(role="user", content="Are you there?"),
            ],
            id="before a human turn",
        ),
    ],
)
async def test_a_nudge_the_api_would_refuse_there_is_merged_as_before(messages):
    in_place_system, in_place = await _build(messages, in_place=True)
    merged_system, merged = await _build(messages, in_place=False)

    assert (in_place_system, in_place) == (merged_system, merged)
    assert _NUDGE in in_place_system
    _assert_api_placement(in_place)


async def test_without_the_capability_every_system_message_is_merged():
    messages = [
        ChatMessage(role="system", content="You are an analyst."),
        ChatMessage(role="user", content="Look it up."),
        ChatMessage(role="assistant", content="", tool_calls=[_call("c1")]),
        _tool("c1"),
        ChatMessage(role="system", content=_NUDGE),
    ]

    system, conversation = await _build(messages, in_place=False)

    assert system == f"You are an analyst.\n\n{_NUDGE}"
    assert all(entry["role"] != "system" for entry in conversation)


async def test_the_request_renders_in_place_only_for_a_capable_model():
    messages = [
        ChatMessage(role="system", content="You are an analyst."),
        ChatMessage(role="user", content="Look it up."),
        ChatMessage(role="assistant", content="", tool_calls=[_call("c1")]),
        _tool("c1"),
        ChatMessage(role="system", content=_NUDGE),
    ]
    provider = AnthropicProvider(api_key="test-key")

    capable_system, capable = await provider._prepare_request(messages, None, "claude-sonnet-5")
    legacy_system, legacy = await provider._prepare_request(messages, None, "claude-sonnet-4-5")

    assert capable_system == "You are an analyst."
    assert capable[-1]["role"] == "system"
    assert _NUDGE in legacy_system
    assert legacy[-1]["role"] == "user"


# ---------------------------------------------------------------------------
# The fold-back and the refusal retry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cached_system", [False, True])
async def test_the_fold_back_is_the_merged_rendering(cached_system):
    messages = [
        ChatMessage(role="system", content="You are an analyst."),
        ChatMessage(role="user", content="Look it up."),
        ChatMessage(role="assistant", content="", tool_calls=[_call("c1")]),
        _tool("c1"),
        ChatMessage(role="system", content=_NUDGE),
        ChatMessage(role="system", content="Be terse."),
    ]
    in_place_system, in_place = await _build(messages, in_place=True)
    merged_system, merged = await _build(messages, in_place=False)
    cache = {"type": "ephemeral"}
    system = [{"type": "text", "text": in_place_system, "cache_control": cache}] if cached_system else in_place_system

    folded = _fold_in_place_system({"model": "m", "system": system, "messages": in_place})

    assert folded["messages"] == merged
    if cached_system:
        assert folded["system"] == [{"type": "text", "text": merged_system, "cache_control": cache}]
    else:
        assert folded["system"] == merged_system
    assert _fold_in_place_system({"system": "s", "messages": merged}) is None


def _status_error(message: str) -> anthropic.APIStatusError:
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    response = httpx.Response(400, json=body, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    return anthropic.BadRequestError(f"Error code: 400 - {body}", response=response, body=body)


def test_only_a_system_role_refusal_triggers_the_fold_back():
    assert _refuses_in_place_system(_status_error("role 'system' is not supported on this model"))
    assert _refuses_in_place_system(
        _status_error("messages.2: role 'system' must follow a 'user' message or an 'assistant' message")
    )
    assert not _refuses_in_place_system(_status_error("tools.0.input_schema: invalid"))


def _response_stub():
    return SimpleNamespace(
        content=[],
        stop_reason="end_turn",
        model="claude-sonnet-5",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )


async def test_a_refused_in_place_system_message_is_retried_once_merged():
    sent: list[dict] = []
    outcomes: list = [_status_error("role 'system' is not supported on this model"), _response_stub()]

    async def _create(**kwargs):
        sent.append(copy.deepcopy(kwargs))
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    provider = AnthropicProvider(api_key="test-key")
    provider.client = MagicMock()
    provider.client.messages.create = _create
    messages = [
        ChatMessage(role="system", content="You are an analyst."),
        ChatMessage(role="user", content="Look it up."),
        ChatMessage(role="assistant", content="", tool_calls=[_call("c1")]),
        _tool("c1"),
        ChatMessage(role="system", content=_NUDGE),
    ]

    await provider.chat(messages=messages, model="claude-sonnet-5")

    assert len(sent) == 2
    assert sent[0]["messages"][-1]["role"] == "system"
    assert all(entry["role"] != "system" for entry in sent[1]["messages"])
    system_text = sent[1]["system"][0]["text"] if isinstance(sent[1]["system"], list) else sent[1]["system"]
    assert _NUDGE in system_text


_REPLAY_REFUSAL = "messages.1.content.0: thinking blocks cannot be modified"
_SYSTEM_REFUSAL = "role 'system' is not supported on this model"


def _system_text(params: dict) -> str:
    system = params.get("system")
    return system[0]["text"] if isinstance(system, list) else system


def _refusing_provider(monkeypatch, outcomes: list, sent: list[dict]) -> AnthropicProvider:
    """A provider whose replay is refused by ``_REPLAY_REFUSAL`` and whose
    fallback conversation is the one first sent (in-place system included)."""
    import app.llm.anthropic_provider as module

    monkeypatch.setattr(
        module.ThinkingReplay,
        "refused_by",
        lambda self, exc: _REPLAY_REFUSAL in str(exc),
    )
    monkeypatch.setattr(
        module.ThinkingReplay,
        "retry_without",
        lambda self, exc: copy.deepcopy(sent[0]["messages"]),
    )

    async def _create(**kwargs):
        sent.append(copy.deepcopy(kwargs))
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    provider = AnthropicProvider(api_key="test-key")
    provider.client = MagicMock()
    provider.client.messages.create = _create
    return provider


_NUDGED = [
    ChatMessage(role="system", content="You are an analyst."),
    ChatMessage(role="user", content="Look it up."),
    ChatMessage(role="assistant", content="", tool_calls=[_call("c1")]),
    _tool("c1"),
    ChatMessage(role="system", content=_NUDGE),
]


async def test_a_model_refusing_the_replay_then_the_in_place_message_still_answers(monkeypatch):
    sent: list[dict] = []
    outcomes: list = [_status_error(_REPLAY_REFUSAL), _status_error(_SYSTEM_REFUSAL), _response_stub()]
    provider = _refusing_provider(monkeypatch, outcomes, sent)

    await provider.chat(messages=list(_NUDGED), model="claude-sonnet-5")

    assert len(sent) == 3
    assert sent[1]["messages"][-1]["role"] == "system"
    assert all(entry["role"] != "system" for entry in sent[2]["messages"])
    assert _system_text(sent[2]).count(_NUDGE) == 1


async def test_a_model_refusing_the_in_place_message_then_the_replay_folds_once(monkeypatch):
    sent: list[dict] = []
    outcomes: list = [_status_error(_SYSTEM_REFUSAL), _status_error(_REPLAY_REFUSAL), _response_stub()]
    provider = _refusing_provider(monkeypatch, outcomes, sent)

    await provider.chat(messages=list(_NUDGED), model="claude-sonnet-5")

    assert len(sent) == 3
    assert all(entry["role"] != "system" for entry in sent[2]["messages"])
    assert _system_text(sent[2]).count(_NUDGE) == 1
    assert _system_text(sent[2]) == _system_text(sent[1])


@pytest.mark.parametrize(
    "refusals",
    [
        (_SYSTEM_REFUSAL, _REPLAY_REFUSAL, _SYSTEM_REFUSAL),
        (_REPLAY_REFUSAL, _SYSTEM_REFUSAL, _REPLAY_REFUSAL),
    ],
)
async def test_each_refusal_is_taken_back_once_only(monkeypatch, refusals):
    sent: list[dict] = []
    outcomes: list = [_status_error(message) for message in refusals]
    provider = _refusing_provider(monkeypatch, outcomes, sent)

    with pytest.raises(anthropic.BadRequestError):
        await provider.chat(messages=list(_NUDGED), model="claude-sonnet-5")
    assert len(sent) == 3


async def test_the_streaming_paths_recover_from_both_refusals(monkeypatch):
    sent: list[dict] = []
    provider = _refusing_provider(monkeypatch, [], sent)
    errors = [_status_error(_REPLAY_REFUSAL), _status_error(_SYSTEM_REFUSAL)]

    class _EmptyStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    async def _open(kwargs):
        sent.append(copy.deepcopy(kwargs))
        if errors:
            raise errors.pop(0)
        return _EmptyStream()

    monkeypatch.setattr(provider, "_open_event_stream", _open)

    chunks = [chunk async for chunk in provider.chat_stream(messages=list(_NUDGED), model="claude-sonnet-5")]

    assert chunks == []
    assert len(sent) == 3
    assert all(entry["role"] != "system" for entry in sent[2]["messages"])
    assert _system_text(sent[2]).count(_NUDGE) == 1

    sent.clear()
    errors.extend([_status_error(_SYSTEM_REFUSAL), _status_error(_REPLAY_REFUSAL)])
    await provider.chat_stream_with_tools(messages=list(_NUDGED), model="claude-sonnet-5")

    assert len(sent) == 3
    assert all(entry["role"] != "system" for entry in sent[2]["messages"])
    assert _system_text(sent[2]).count(_NUDGE) == 1


# ---------------------------------------------------------------------------
# Thinking replay: a nudge does not end the tool round
# ---------------------------------------------------------------------------


def test_an_in_place_system_message_does_not_end_the_tool_round():
    use = {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]}
    result = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "r"}]}
    use2 = {"role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "x", "input": {}}]}
    result2 = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "r"}]}
    nudge = {"role": "system", "content": _NUDGE}
    ask = {"role": "user", "content": "go"}

    assert current_tool_round([ask, use, result, nudge]) == frozenset({1})
    assert current_tool_round([ask, use, result, nudge, use2, result2]) == frozenset({1, 4})
    assert current_tool_round([ask, use, result]) == frozenset({1})
