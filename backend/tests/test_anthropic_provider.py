import copy
import dataclasses
from types import SimpleNamespace
from typing import get_args, get_type_hints
from unittest.mock import AsyncMock, MagicMock

import anthropic
import httpx
import pytest

from app.core.config import settings
from app.llm.anthropic_provider import (
    _ADAPTIVE_FORM_HEADROOM,
    _LOWEST_EFFORT,
    _MODELS_REFUSING_DISABLED_THINKING,
    _MODELS_TURNING_THINKING_OFF_BETWEEN_TOOLS,
    AnthropicProvider,
    _apply_thinking_off,
    _asks_for_between_tools,
    _refuses_disabled_thinking,
    _sanitize_input_schema,
    _supports_structured_output,
)
from app.llm.base import ChatMessage, ThinkingActivitySink
from app.llm.model_capabilities import MODEL_PARAMETER_RULES
from app.tools.base import ToolCall, ToolDefinition


@pytest.mark.asyncio
async def test_build_messages_removes_trailing_assistant_prefill():
    provider = AnthropicProvider(api_key="test-key")

    _system, conversation = await provider._build_anthropic_messages(
        [
            ChatMessage(role="user", content="Question"),
            ChatMessage(role="assistant", content="Partial answer"),
        ]
    )

    assert [msg["role"] for msg in conversation] == ["user"]
    assert conversation[-1]["content"] == "Question"


@pytest.mark.asyncio
async def test_build_messages_keeps_assistant_when_user_turn_follows():
    provider = AnthropicProvider(api_key="test-key")

    _system, conversation = await provider._build_anthropic_messages(
        [
            ChatMessage(role="user", content="Question"),
            ChatMessage(role="assistant", content="Answer"),
            ChatMessage(role="user", content="Follow-up"),
        ]
    )

    assert [msg["role"] for msg in conversation] == ["user", "assistant", "user"]
    assert conversation[-1]["content"] == "Follow-up"


@pytest.mark.asyncio
async def test_build_messages_removes_multiple_trailing_assistants():
    provider = AnthropicProvider(api_key="test-key")

    _system, conversation = await provider._build_anthropic_messages(
        [
            ChatMessage(role="user", content="Question"),
            ChatMessage(role="assistant", content="Answer 1"),
            ChatMessage(role="assistant", content="Answer 2"),
        ]
    )

    assert [msg["role"] for msg in conversation] == ["user"]
    assert conversation[-1]["content"] == "Question"


@pytest.mark.asyncio
async def test_build_messages_all_assistant_raises_with_trimmed_count():
    provider = AnthropicProvider(api_key="test-key")

    with pytest.raises(ValueError) as exc_info:
        await provider._build_anthropic_messages(
            [
                ChatMessage(role="assistant", content="A"),
                ChatMessage(role="assistant", content="B"),
            ]
        )
    message = str(exc_info.value)
    assert "conversation list is empty after filtering" in message
    assert "Trailing assistant messages removed: 2." in message
    assert "Original messages count: 2" in message
    assert "roles: ['assistant', 'assistant']" in message


@pytest.mark.asyncio
async def test_build_messages_removes_trailing_assistant_with_tool_calls():
    provider = AnthropicProvider(api_key="test-key")

    _system, conversation = await provider._build_anthropic_messages(
        [
            ChatMessage(role="user", content="Run tool"),
            ChatMessage(
                role="assistant",
                content="Calling tool",
                tool_calls=[ToolCall(id="tc_1", name="lookup", arguments={"q": "x"})],
            ),
        ]
    )

    assert [msg["role"] for msg in conversation] == ["user"]
    assert conversation[-1]["content"] == "Run tool"


@pytest.mark.asyncio
async def test_build_messages_single_user_is_noop():
    provider = AnthropicProvider(api_key="test-key")

    _system, conversation = await provider._build_anthropic_messages(
        [ChatMessage(role="user", content="Hello")]
    )

    assert conversation == [{"role": "user", "content": "Hello"}]


# ---------------------------------------------------------------------------
# Structured-output gating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    [
        "claude-fable-5",
        "claude-sonnet-5",
        "claude-sonnet-4-5",
        "claude-sonnet-4-5-20250929",
        "claude-sonnet-4-6",
        "claude-opus-5",
        "claude-opus-4-5",
        "claude-opus-4-5-20251101",
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-haiku-4-5",
        "claude-haiku-4-5-20251001",
    ],
)
def test_supports_structured_output_for_ga_models(model: str) -> None:
    assert _supports_structured_output(model) is True


@pytest.mark.parametrize(
    "model",
    [
        # Default platform model — Anthropic rejected output_config.format
        # on this one with HTTP 400.
        "claude-sonnet-4-20250514",
        "claude-opus-4-20250514",
        "claude-opus-4-1",
        "claude-opus-4-1-20250805",
        "claude-3-7-sonnet-20250219",
        "claude-3-5-sonnet-20241022",
        "claude-3-5-haiku-20241022",
        "claude-3-opus-20240229",
        "claude-3-haiku-20240307",
        "",
    ],
)
def test_supports_structured_output_rejects_unsupported_models(model: str) -> None:
    assert _supports_structured_output(model) is False


# ---------------------------------------------------------------------------
# Branch-level wiring: chat() and chat_stream() must actually consult the
# gate before forwarding ``output_config.format`` to the Anthropic client.
# The helper-only tests above would still pass if a future refactor
# accidentally dropped the gate; these tests capture the kwargs that
# reach the SDK and pin the contract end-to-end.
# ---------------------------------------------------------------------------


_STRUCTURED_OUTPUT_KWARG = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "additionalProperties": False,
    },
}


def _build_chat_response_stub():
    """Return a minimal stub matching the Anthropic Messages API response shape.

    The provider unpacks ``response.content`` (iterable of content blocks),
    ``response.stop_reason``, ``response.model``, and
    ``response.usage.{input_tokens,output_tokens}``. We give it an empty
    content list so no real parsing logic runs — these tests only care
    about what was passed *in*.
    """
    return SimpleNamespace(
        content=[],
        stop_reason="end_turn",
        model="claude-sonnet-4-5-20250929",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )


@pytest.mark.asyncio
async def test_chat_omits_output_config_format_for_unsupported_model():
    """chat() must NOT pass ``output_config.format`` to the Anthropic
    client for models outside the GA allow-list, even when the caller
    forwards a ``structured_output`` kwarg. Pins the runtime contract
    that the gate is applied at the call site, not just exposed via the
    helper.
    """
    provider = AnthropicProvider(api_key="test-key")
    provider.client = MagicMock()
    provider.client.messages.create = AsyncMock(return_value=_build_chat_response_stub())

    await provider.chat(
        messages=[ChatMessage(role="user", content="hi")],
        model="claude-sonnet-4-20250514",  # legacy / unsupported
        structured_output=_STRUCTURED_OUTPUT_KWARG,
    )

    provider.client.messages.create.assert_awaited_once()
    sent_kwargs = provider.client.messages.create.call_args.kwargs
    # Either ``output_config`` is fully absent OR it exists for an
    # unrelated reason (e.g. reasoning_effort) but does NOT carry the
    # ``format`` key — both shapes are acceptable.
    if "output_config" in sent_kwargs:
        assert "format" not in sent_kwargs["output_config"], (
            "chat() leaked output_config.format to a model that rejects it"
        )


@pytest.mark.asyncio
async def test_chat_passes_output_config_format_for_supported_model():
    """chat() must forward ``output_config.format`` for GA-list models so
    native structured outputs continue to work on Sonnet 4.5+ / Opus 4.5+
    / Haiku 4.5+ — the gate is a drop, not a global removal.
    """
    provider = AnthropicProvider(api_key="test-key")
    provider.client = MagicMock()
    provider.client.messages.create = AsyncMock(return_value=_build_chat_response_stub())

    await provider.chat(
        messages=[ChatMessage(role="user", content="hi")],
        model="claude-sonnet-4-5-20250929",  # GA-supported
        structured_output=_STRUCTURED_OUTPUT_KWARG,
    )

    sent_kwargs = provider.client.messages.create.call_args.kwargs
    assert "output_config" in sent_kwargs, "GA model lost output_config block"
    assert sent_kwargs["output_config"].get("format") == _STRUCTURED_OUTPUT_KWARG, (
        "GA model dropped output_config.format unexpectedly"
    )


@pytest.mark.asyncio
async def test_chat_disables_thinking_for_adaptive_model_when_requested():
    """chat() must send ``thinking: {type: disabled}`` for new-gen models that
    default to adaptive thinking (Sonnet 5 / Opus 4.7+) when the caller passes
    ``disable_thinking=True``. The bounded structured-output utility calls
    (conversation compaction / memory reconciliation) rely on this so adaptive
    thinking does not consume the ``max_tokens`` budget meant for the JSON
    answer — the root cause of the Sonnet 5 non-JSON regression.
    """
    provider = AnthropicProvider(api_key="test-key")
    provider.client = MagicMock()
    provider.client.messages.create = AsyncMock(return_value=_build_chat_response_stub())

    await provider.chat(
        messages=[ChatMessage(role="user", content="hi")],
        model="claude-sonnet-5",
        disable_thinking=True,
    )

    sent_kwargs = provider.client.messages.create.call_args.kwargs
    assert sent_kwargs.get("thinking") == {"type": "disabled"}


@pytest.mark.asyncio
async def test_chat_omits_thinking_disable_for_legacy_model():
    """chat() must NOT send the ``thinking`` parameter to older models (they
    leave thinking off by default and pre-4.x models reject it) even when the
    caller passes ``disable_thinking=True``.
    """
    provider = AnthropicProvider(api_key="test-key")
    provider.client = MagicMock()
    provider.client.messages.create = AsyncMock(return_value=_build_chat_response_stub())

    await provider.chat(
        messages=[ChatMessage(role="user", content="hi")],
        model="claude-3-5-sonnet-20241022",
        disable_thinking=True,
    )

    sent_kwargs = provider.client.messages.create.call_args.kwargs
    assert "thinking" not in sent_kwargs


@pytest.mark.asyncio
async def test_chat_keeps_thinking_when_not_disabled():
    """chat() must not send ``thinking`` when the caller doesn't request it —
    adaptive thinking stays on by default for normal calls.
    """
    provider = AnthropicProvider(api_key="test-key")
    provider.client = MagicMock()
    provider.client.messages.create = AsyncMock(return_value=_build_chat_response_stub())

    await provider.chat(
        messages=[ChatMessage(role="user", content="hi")],
        model="claude-sonnet-5",
    )

    sent_kwargs = provider.client.messages.create.call_args.kwargs
    assert "thinking" not in sent_kwargs


# ---------------------------------------------------------------------------
# Models that refuse ``thinking.type.disabled``
#
# A model the capability rules do not declare (``claude-sonnet-5-1`` here, a
# 5.x release newer than the table) may answer ``thinking: {"type":
# "disabled"}`` with a 400 naming the parameter path and pointing at the
# adaptive form, as Fable 5.1 did before it was declared. The provider
# retries THAT exact refusal once as ``thinking: {"type": "adaptive"}`` plus
# ``output_config.effort`` at its lowest level and remembers the model for the
# rest of the process. Anything else keeps its normal path: the transient
# retry for a 429 / 5xx, the router's failover chain for the rest.
# ---------------------------------------------------------------------------


_REFUSAL_MESSAGE = (
    '"thinking.type.disabled" is not supported for this model. Use '
    '"thinking.type.adaptive" and "output_config.effort" to control thinking '
    "behavior."
)

_MESSAGES_API = "https://api.anthropic.com/v1/messages"


@pytest.fixture(autouse=True)
def _forget_models_refusing_disabled_thinking():
    """The memos are process-wide; keep every test on a clean slate."""
    _MODELS_REFUSING_DISABLED_THINKING.clear()
    _MODELS_TURNING_THINKING_OFF_BETWEEN_TOOLS.clear()
    yield
    _MODELS_REFUSING_DISABLED_THINKING.clear()
    _MODELS_TURNING_THINKING_OFF_BETWEEN_TOOLS.clear()


def _status_error(status: int, message: str) -> anthropic.APIStatusError:
    """Build the SDK error a real HTTP *status* answer with *message* raises.

    Same body shape and same class dispatch as
    ``anthropic._client.AsyncAnthropic._make_status_error``, so the predicate
    is exercised against what production actually receives.
    """
    body = {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": message},
    }
    response = httpx.Response(
        status, json=body, request=httpx.Request("POST", _MESSAGES_API)
    )
    err_msg = f"Error code: {status} - {body}"
    if status == 400:
        return anthropic.BadRequestError(err_msg, response=response, body=body)
    if status == 429:
        return anthropic.RateLimitError(err_msg, response=response, body=body)
    if status == 529:
        return anthropic.InternalServerError(err_msg, response=response, body=body)
    return anthropic.APIStatusError(err_msg, response=response, body=body)


def _recording_create(*outcomes):
    """``messages.create`` stand-in that snapshots each call's kwargs.

    Deep copies pin what EACH attempt sent even if the provider derives one
    request dict from another; an ``AsyncMock`` keeps references only. An
    outcome that is an exception is raised, anything else is returned.
    """
    pending = list(outcomes)
    sent: list[dict] = []

    async def _create(**kwargs):
        sent.append(copy.deepcopy(kwargs))
        outcome = pending.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return _create, sent


def _provider_with(*outcomes) -> tuple[AnthropicProvider, list[dict]]:
    provider = AnthropicProvider(api_key="test-key")
    provider.client = MagicMock()
    provider.client.messages.create, sent = _recording_create(*outcomes)
    return provider, sent


def test_lowest_effort_is_a_level_the_pinned_sdk_accepts():
    """``output_config.effort`` is typed by the SDK; the constant must name
    its lowest level so an SDK upgrade that renames the levels fails here
    instead of as a 400 in production.
    """
    effort_hint = get_type_hints(anthropic.types.OutputConfigParam)["effort"]
    levels = get_args(get_args(effort_hint)[0])  # unwrap Optional[Literal[...]]
    assert _LOWEST_EFFORT in levels
    assert _LOWEST_EFFORT == "low"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        # The production refusal, verbatim.
        (_status_error(400, _REFUSAL_MESSAGE), True),
        # Quoting variants around the parameter path.
        (_status_error(400, "'thinking.type.disabled' is not supported."), True),
        (_status_error(400, "thinking.type.disabled is not supported."), True),
        (_status_error(400, "`Thinking.Type.Disabled` is not supported."), True),
        # A different 400 keeps its normal path.
        (_status_error(400, "tools.0.input_schema: does not support oneOf"), False),
        (_status_error(400, "thinking.budget_tokens: must be >= 1024"), False),
        # Only a 400 qualifies, whatever the message says.
        (_status_error(429, _REFUSAL_MESSAGE), False),
        (_status_error(500, _REFUSAL_MESSAGE), False),
        (_status_error(529, _REFUSAL_MESSAGE), False),
        (
            anthropic.APIConnectionError(request=httpx.Request("POST", _MESSAGES_API)),
            False,
        ),
        (ValueError(_REFUSAL_MESSAGE), False),
    ],
)
def test_refuses_disabled_thinking_predicate(exc: Exception, expected: bool):
    assert _refuses_disabled_thinking(exc) is expected


# The production refusal on claude-sonnet-5-5 before its rule declared
# ``between_tools_thinking_off``, verbatim.
_BETWEEN_TOOLS_REFUSAL_MESSAGE = (
    'To turn thinking off on this model, send "thinking": {"type": "between_tools"} '
    'instead of {"type": "disabled"}. The model does not think before responding. '
    "The short updates it writes between tool calls come back as thinking blocks."
)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_status_error(400, _BETWEEN_TOOLS_REFUSAL_MESSAGE), True),
        (_status_error(400, _REFUSAL_MESSAGE), False),
        (_status_error(400, "between_tools is not supported for this model."), False),
        (_status_error(429, _BETWEEN_TOOLS_REFUSAL_MESSAGE), False),
        (ValueError(_BETWEEN_TOOLS_REFUSAL_MESSAGE), False),
    ],
)
def test_asks_for_between_tools_predicate(exc: Exception, expected: bool):
    assert _asks_for_between_tools(exc) is expected


def test_between_tools_refusal_does_not_name_the_parameter_path():
    """Why it needs a predicate of its own: the dotted path the adaptive
    refusal names is absent, so that refusal's match misses it."""
    assert not _refuses_disabled_thinking(_status_error(400, _BETWEEN_TOOLS_REFUSAL_MESSAGE))


@pytest.mark.asyncio
async def test_chat_retries_a_between_tools_refusal_once_with_between_tools():
    """A model no rule declares yet, refusing ``disabled`` with the wording
    that names ``between_tools``, is retried once in that form - no adaptive
    thinking, no added headroom, an effort held to ``high`` - and remembered.
    """
    provider, sent = _provider_with(
        _status_error(400, _BETWEEN_TOOLS_REFUSAL_MESSAGE),
        _build_chat_response_stub(),
        _build_chat_response_stub(),
    )

    for _ in range(2):
        await provider.chat(
            messages=[ChatMessage(role="user", content="hi")],
            model="claude-sonnet-5-6",
            max_tokens=1500,
            disable_thinking=True,
            reasoning_effort="max",
        )

    first, second, third = sent
    assert first["thinking"] == {"type": "disabled"}
    assert second["thinking"] == third["thinking"] == {"type": "between_tools"}
    assert second["max_tokens"] == third["max_tokens"] == 1500
    assert second["output_config"] == third["output_config"] == {"effort": "high"}
    assert set(_MODELS_TURNING_THINKING_OFF_BETWEEN_TOOLS) == {"claude-sonnet-5-6"}
    assert not _MODELS_REFUSING_DISABLED_THINKING


@pytest.mark.asyncio
async def test_between_tools_retry_sends_no_empty_output_config():
    provider, sent = _provider_with(
        _status_error(400, _BETWEEN_TOOLS_REFUSAL_MESSAGE), _build_chat_response_stub()
    )

    await provider.chat(
        messages=[ChatMessage(role="user", content="hi")],
        model="claude-sonnet-5-6",
        disable_thinking=True,
    )

    assert sent[1]["thinking"] == {"type": "between_tools"}
    assert "output_config" not in sent[1]


@pytest.mark.asyncio
async def test_chat_keeps_disabled_thinking_on_a_model_that_accepts_it():
    """A model that accepts ``disabled`` keeps receiving it, in ONE call, and
    the hint adds no effort setting of its own - thinking off shares no
    budget with the answer.
    """
    provider, sent = _provider_with(_build_chat_response_stub())

    await provider.chat(
        messages=[ChatMessage(role="user", content="hi")],
        model="claude-sonnet-5",
        disable_thinking=True,
    )

    assert len(sent) == 1
    assert sent[0]["thinking"] == {"type": "disabled"}
    assert "output_config" not in sent[0]
    assert sent[0]["max_tokens"] == 4096
    assert not _MODELS_REFUSING_DISABLED_THINKING


@pytest.mark.asyncio
async def test_chat_disabled_thinking_leaves_a_caller_reasoning_effort_alone():
    """``disable_thinking`` governs thinking only. On a model that accepts
    ``disabled`` a caller's ``reasoning_effort`` stays on the request exactly
    as before this fix: effort applies to every output token and is valid
    with thinking off, where it bounds the text rather than any thinking.
    (On the adaptive form effort IS the thinking lever and is pinned low -
    see the remembered-model test below.)
    """
    provider, sent = _provider_with(_build_chat_response_stub())

    await provider.chat(
        messages=[ChatMessage(role="user", content="hi")],
        model="claude-sonnet-5",
        disable_thinking=True,
        reasoning_effort="medium",
    )

    assert len(sent) == 1
    assert sent[0]["thinking"] == {"type": "disabled"}
    assert sent[0]["output_config"] == {"effort": "medium"}


@pytest.mark.asyncio
async def test_chat_retries_a_refused_disabled_thinking_once_as_adaptive_low_effort():
    """The exact 400 is retried exactly once, in the adaptive form the API
    documents, on the SAME request; the answer comes back through the normal
    path and the model is remembered.
    """
    provider, sent = _provider_with(
        _status_error(400, _REFUSAL_MESSAGE), _build_chat_response_stub()
    )

    result = await provider.chat(
        messages=[
            ChatMessage(role="system", content="Extract the directives."),
            ChatMessage(role="user", content="hi"),
        ],
        model="claude-sonnet-5-1",
        max_tokens=1500,
        disable_thinking=True,
        structured_output=_STRUCTURED_OUTPUT_KWARG,
    )

    assert len(sent) == 2
    first, second = sent
    assert first["thinking"] == {"type": "disabled"}
    assert first["output_config"] == {"format": _STRUCTURED_OUTPUT_KWARG}
    assert second["thinking"] == {"type": "adaptive"}
    assert second["output_config"] == {
        "format": _STRUCTURED_OUTPUT_KWARG,
        "effort": _LOWEST_EFFORT,
    }
    # The adaptive form cannot switch thinking off, so its budget gains room
    # for it on top of the one the answer was given.
    assert first["max_tokens"] == 1500
    assert second["max_tokens"] == 1500 + _ADAPTIVE_FORM_HEADROOM
    # Everything else - model, system (with its cache breakpoint), messages,
    # automatic caching - is the request that was refused.
    varied = ("thinking", "output_config", "max_tokens")
    assert {k: v for k, v in second.items() if k not in varied} == {
        k: v for k, v in first.items() if k not in varied
    }
    assert result.usage == {"input_tokens": 1, "output_tokens": 1}
    assert set(_MODELS_REFUSING_DISABLED_THINKING) == {"claude-sonnet-5-1"}


@pytest.mark.asyncio
async def test_chat_sends_the_adaptive_form_first_on_a_remembered_model():
    """Once remembered, the model gets the adaptive form on the FIRST attempt:
    the second call does not pay the 400 round trip either.
    """
    _MODELS_REFUSING_DISABLED_THINKING.add("claude-sonnet-5-1")
    provider, sent = _provider_with(_build_chat_response_stub())

    await provider.chat(
        messages=[ChatMessage(role="user", content="hi")],
        model="claude-sonnet-5-1",
        disable_thinking=True,
    )

    assert len(sent) == 1
    assert sent[0]["thinking"] == {"type": "adaptive"}
    assert sent[0]["output_config"] == {"effort": _LOWEST_EFFORT}
    assert sent[0]["max_tokens"] == 4096 + _ADAPTIVE_FORM_HEADROOM


def test_adaptive_form_leaves_a_budget_that_is_not_a_number_alone():
    """The margin is added to a token count only: a request carrying none
    keeps what it had and still gets the adaptive form."""
    params: dict = {"model": "claude-mythos-5", "max_tokens": None}

    _apply_thinking_off(params, "claude-mythos-5")

    assert params["max_tokens"] is None
    assert params["thinking"] == {"type": "adaptive"}


@pytest.mark.parametrize(
    ("budget", "ceiling", "expected"),
    [
        (2000, None, 2000 + _ADAPTIVE_FORM_HEADROOM),
        (2000, 128_000, 2000 + _ADAPTIVE_FORM_HEADROOM),
        # A ``max_tokens`` above the model's ceiling is an HTTP 400: the margin
        # stops there...
        (2000, 5000, 5000),
        # ...and never lowers a budget the caller already set above it.
        (6000, 5000, 6000),
    ],
)
def test_adaptive_form_margin_stops_at_the_output_ceiling(budget, ceiling, expected):
    params: dict = {"model": "claude-mythos-5", "max_tokens": budget}

    _apply_thinking_off(params, "claude-mythos-5", max_output_tokens=ceiling)

    assert params["max_tokens"] == expected


@pytest.mark.asyncio
async def test_chat_caps_the_adaptive_margin_at_the_configured_ceiling():
    """The ceiling read is the one the agent loop reads: an admin per-model
    override wins over the model's documented 128K."""
    provider, sent = _provider_with(_build_chat_response_stub())
    provider.max_output_tokens_overrides = {"claude-mythos-5": 5000}

    await provider.chat(
        messages=[ChatMessage(role="user", content="hi")],
        model="claude-mythos-5",
        max_tokens=2000,
        disable_thinking=True,
    )

    assert sent[0]["thinking"] == {"type": "adaptive"}
    assert sent[0]["max_tokens"] == 5000


@pytest.mark.asyncio
async def test_refused_then_adaptive_retry_caps_the_margin_at_the_ceiling():
    provider, sent = _provider_with(
        _status_error(400, _REFUSAL_MESSAGE), _build_chat_response_stub()
    )
    provider.max_output_tokens_overrides = {"claude-sonnet-5-1": 3000}

    await provider.chat(
        messages=[ChatMessage(role="user", content="hi")],
        model="claude-sonnet-5-1",
        max_tokens=1500,
        disable_thinking=True,
    )

    assert [request["max_tokens"] for request in sent] == [1500, 3000]


@pytest.mark.asyncio
async def test_chat_lowest_effort_overrides_a_caller_reasoning_effort_on_a_remembered_model():
    """``disable_thinking`` asked for no thinking at all; on a model that
    cannot switch it off the lowest effort is the closest it gets, whatever
    ``reasoning_effort`` the caller also passed.
    """
    _MODELS_REFUSING_DISABLED_THINKING.add("claude-sonnet-5-1")
    provider, sent = _provider_with(_build_chat_response_stub())

    await provider.chat(
        messages=[ChatMessage(role="user", content="hi")],
        model="claude-sonnet-5-1",
        disable_thinking=True,
        reasoning_effort="high",
    )

    assert sent[0]["output_config"] == {"effort": _LOWEST_EFFORT}


@pytest.mark.asyncio
async def test_chat_memo_is_keyed_by_exact_model_id():
    """The memo remembers the model id that refused, not a family: a sibling
    id has not been observed and still gets ``disabled`` (one round trip of
    its own if it refuses too).
    """
    _MODELS_REFUSING_DISABLED_THINKING.add("claude-sonnet-5-1")
    provider, sent = _provider_with(_build_chat_response_stub())

    await provider.chat(
        messages=[ChatMessage(role="user", content="hi")],
        model="claude-sonnet-5",
        disable_thinking=True,
    )

    assert sent[0]["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_chat_does_not_retry_a_different_bad_request():
    """A 400 that does not name ``thinking.type.disabled`` is not this
    branch's business: one call, the error propagates unchanged (the router's
    failover decides what happens next), nothing is remembered.
    """
    other = _status_error(400, "tools.0.input_schema: does not support oneOf")
    provider, sent = _provider_with(other)

    with pytest.raises(anthropic.BadRequestError) as exc_info:
        await provider.chat(
            messages=[ChatMessage(role="user", content="hi")],
            model="claude-sonnet-5-1",
            disable_thinking=True,
        )

    assert exc_info.value is other
    assert len(sent) == 1
    assert not _MODELS_REFUSING_DISABLED_THINKING


@pytest.mark.asyncio
async def test_chat_does_not_enter_the_branch_when_disabled_was_not_sent():
    """Without the ``disabled`` form on the wire the refusal text cannot be
    about this request: even a 400 carrying it propagates as-is, and the
    model is not remembered.
    """
    provider, sent = _provider_with(_status_error(400, _REFUSAL_MESSAGE))

    with pytest.raises(anthropic.BadRequestError):
        await provider.chat(
            messages=[ChatMessage(role="user", content="hi")],
            model="claude-sonnet-5-1",
        )

    assert len(sent) == 1
    assert "thinking" not in sent[0]
    assert not _MODELS_REFUSING_DISABLED_THINKING


@pytest.mark.asyncio
async def test_chat_adaptive_retry_failure_propagates_for_the_router_to_fail_over():
    """When the adaptive form fails too, THAT error propagates unchanged after
    exactly two calls, so the router's existing failover chain runs. The
    refusal itself was real, so the model stays remembered.
    """
    adaptive_failure = _status_error(400, "output_config.effort: unsupported value")
    provider, sent = _provider_with(
        _status_error(400, _REFUSAL_MESSAGE), adaptive_failure
    )

    with pytest.raises(anthropic.BadRequestError) as exc_info:
        await provider.chat(
            messages=[ChatMessage(role="user", content="hi")],
            model="claude-sonnet-5-1",
            disable_thinking=True,
        )

    assert exc_info.value is adaptive_failure
    assert len(sent) == 2
    assert sent[1]["thinking"] == {"type": "adaptive"}
    assert set(_MODELS_REFUSING_DISABLED_THINKING) == {"claude-sonnet-5-1"}


@pytest.mark.asyncio
async def test_chat_never_retries_the_adaptive_form_a_second_time():
    """A refusal answered to the adaptive form (which never carries
    ``disabled``) cannot re-enter the branch: two calls, then it propagates.
    """
    provider, sent = _provider_with(
        _status_error(400, _REFUSAL_MESSAGE), _status_error(400, _REFUSAL_MESSAGE)
    )

    with pytest.raises(anthropic.BadRequestError):
        await provider.chat(
            messages=[ChatMessage(role="user", content="hi")],
            model="claude-sonnet-5-1",
            disable_thinking=True,
        )

    assert len(sent) == 2


@pytest.mark.asyncio
async def test_chat_adaptive_retry_keeps_the_transient_error_policy(monkeypatch):
    """The adaptive retry is an ordinary request: a 529 on it goes through
    ``retry_llm_call`` like any other, and the answer still comes back.
    """
    # Full-jitter back-off draws its delay from ``random.uniform``; pin it to
    # zero so the test does not sleep.
    monkeypatch.setattr("app.llm.base.random.uniform", lambda _a, _b: 0.0)
    provider, sent = _provider_with(
        _status_error(400, _REFUSAL_MESSAGE),
        _status_error(529, "Overloaded"),
        _build_chat_response_stub(),
    )

    result = await provider.chat(
        messages=[ChatMessage(role="user", content="hi")],
        model="claude-sonnet-5-1",
        disable_thinking=True,
    )

    assert len(sent) == 3
    assert sent[0]["thinking"] == {"type": "disabled"}
    assert sent[1]["thinking"] == {"type": "adaptive"}
    assert sent[2] == sent[1]
    assert result.usage == {"input_tokens": 1, "output_tokens": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{}, {"disable_thinking": False}])
async def test_chat_leaves_thinking_alone_when_not_asked_to_disable_it(kwargs: dict):
    """A remembered model changes nothing for a call that did not opt out of
    thinking: no ``thinking`` field, no effort - adaptive thinking stays on
    by default for normal calls.
    """
    _MODELS_REFUSING_DISABLED_THINKING.add("claude-sonnet-5-1")
    provider, sent = _provider_with(_build_chat_response_stub())

    await provider.chat(
        messages=[ChatMessage(role="user", content="hi")],
        model="claude-sonnet-5-1",
        **kwargs,
    )

    assert len(sent) == 1
    assert "thinking" not in sent[0]
    assert "output_config" not in sent[0]


class _AsyncIteratorStub:
    """Async-iterator stub that yields nothing — enough for ``async for``
    to terminate immediately without driving any of the streaming
    accumulation logic. The streaming tests only care about the kwargs
    handed to ``client.messages.create(stream=True)``, not the response body.
    """

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _StreamCtxStub:
    """Stub for the stream ``client.messages.create(stream=True)`` returns.

    The provider reads it as ``async with await client.messages.create(...,
    stream=True) as stream: async for event in stream: ...`` and only reads
    from it after ``__aenter__`` returns, so a no-op iterator finishes the
    loop instantly.
    """

    async def __aenter__(self):
        return _AsyncIteratorStub()

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _patch_stream(provider: AnthropicProvider) -> AsyncMock:
    """Replace ``provider.client.messages.create`` with a kwargs-capturing
    AsyncMock that returns a fresh stream stub.
    """
    stream_mock = AsyncMock(return_value=_StreamCtxStub())
    provider.client = MagicMock()
    provider.client.messages.create = stream_mock
    return stream_mock


@pytest.mark.asyncio
async def test_chat_stream_omits_output_config_format_for_unsupported_model():
    """chat_stream() must NOT pass ``output_config.format`` to
    the streaming request for models outside the GA allow-list. Mirrors
    the chat() test on the streaming branch — the gate must be applied
    on BOTH code paths or the bug re-emerges as soon as the agentic
    loop hits a streaming iteration.
    """
    provider = AnthropicProvider(api_key="test-key")
    stream_mock = _patch_stream(provider)

    async for _ in provider.chat_stream(
        messages=[ChatMessage(role="user", content="hi")],
        model="claude-sonnet-4-20250514",  # legacy / unsupported
        structured_output=_STRUCTURED_OUTPUT_KWARG,
    ):
        pass

    stream_mock.assert_called_once()
    sent_kwargs = stream_mock.call_args.kwargs
    if "output_config" in sent_kwargs:
        assert "format" not in sent_kwargs["output_config"], (
            "chat_stream() leaked output_config.format to a legacy model"
        )


@pytest.mark.asyncio
async def test_chat_stream_passes_output_config_format_for_supported_model():
    """chat_stream() must forward ``output_config.format`` for GA-list
    models so the streaming path preserves native structured outputs.
    """
    provider = AnthropicProvider(api_key="test-key")
    stream_mock = _patch_stream(provider)

    async for _ in provider.chat_stream(
        messages=[ChatMessage(role="user", content="hi")],
        model="claude-sonnet-4-6",  # GA-supported
        structured_output=_STRUCTURED_OUTPUT_KWARG,
    ):
        pass

    sent_kwargs = stream_mock.call_args.kwargs
    assert "output_config" in sent_kwargs, "GA streaming lost output_config block"
    assert sent_kwargs["output_config"].get("format") == _STRUCTURED_OUTPUT_KWARG, (
        "GA streaming dropped output_config.format unexpectedly"
    )


# ---------------------------------------------------------------------------
# Prompt caching
#
# Best-practice layout per Anthropic's prompt-caching guide: explicit
# cache breakpoints on the last tool definition and the system prompt,
# plus automatic (top-level, via extra_body) caching for the growing
# conversation. All three call paths must apply the same strategy, and
# the global kill switch / per-call opt-out must restore the legacy
# uncached request shape byte-for-byte.
# ---------------------------------------------------------------------------


_CACHE_TOOLS = [
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

_CACHE_MESSAGES = [
    ChatMessage(role="system", content="You are a helpful agent."),
    ChatMessage(role="user", content="hi"),
]


def _assert_cached_params(sent_kwargs: dict, ttl: str | None = None) -> None:
    expected = {"type": "ephemeral"}
    if ttl:
        expected["ttl"] = ttl

    # Breakpoint on the LAST tool definition only.
    sent_tools = sent_kwargs["tools"]
    assert sent_tools[-1]["cache_control"] == expected
    assert all("cache_control" not in t for t in sent_tools[:-1])

    # System converted to a block list with a trailing breakpoint.
    sent_system = sent_kwargs["system"]
    assert isinstance(sent_system, list)
    assert sent_system[-1]["cache_control"] == expected
    assert sent_system[-1]["text"] == "You are a helpful agent."

    # Automatic conversation caching via top-level cache_control.
    assert sent_kwargs["extra_body"]["cache_control"] == expected

    # Message blocks must never carry explicit markers (automatic caching
    # owns the moving breakpoint; an explicit conflicting marker on the
    # last block is a documented 400).
    for msg in sent_kwargs["messages"]:
        content = msg.get("content")
        if isinstance(content, list):
            assert all("cache_control" not in block for block in content)


@pytest.mark.asyncio
async def test_chat_applies_cache_breakpoints():
    provider = AnthropicProvider(api_key="test-key")
    provider.client = MagicMock()
    provider.client.messages.create = AsyncMock(return_value=_build_chat_response_stub())

    await provider.chat(messages=list(_CACHE_MESSAGES), tools=_CACHE_TOOLS)

    _assert_cached_params(provider.client.messages.create.call_args.kwargs)


@pytest.mark.asyncio
async def test_chat_prompt_cache_kwarg_opts_out():
    provider = AnthropicProvider(api_key="test-key")
    provider.client = MagicMock()
    provider.client.messages.create = AsyncMock(return_value=_build_chat_response_stub())

    await provider.chat(
        messages=list(_CACHE_MESSAGES), tools=_CACHE_TOOLS, prompt_cache=False
    )

    sent_kwargs = provider.client.messages.create.call_args.kwargs
    assert "extra_body" not in sent_kwargs
    assert isinstance(sent_kwargs["system"], str)
    assert all("cache_control" not in t for t in sent_kwargs["tools"])


@pytest.mark.asyncio
async def test_chat_caching_disabled_globally(monkeypatch):
    monkeypatch.setattr(settings, "prompt_caching_enabled", False)
    provider = AnthropicProvider(api_key="test-key")
    provider.client = MagicMock()
    provider.client.messages.create = AsyncMock(return_value=_build_chat_response_stub())

    await provider.chat(messages=list(_CACHE_MESSAGES), tools=_CACHE_TOOLS)

    sent_kwargs = provider.client.messages.create.call_args.kwargs
    assert "extra_body" not in sent_kwargs
    assert isinstance(sent_kwargs["system"], str)
    assert all("cache_control" not in t for t in sent_kwargs["tools"])


@pytest.mark.asyncio
async def test_chat_prompt_cache_true_cannot_override_global_kill_switch(monkeypatch):
    """The global kill switch is authoritative: ``prompt_cache=True`` must
    NOT re-enable caching when ``PROMPT_CACHING_ENABLED`` is false — the
    per-call kwarg is opt-out only.
    """
    monkeypatch.setattr(settings, "prompt_caching_enabled", False)
    provider = AnthropicProvider(api_key="test-key")
    provider.client = MagicMock()
    provider.client.messages.create = AsyncMock(return_value=_build_chat_response_stub())

    await provider.chat(
        messages=list(_CACHE_MESSAGES), tools=_CACHE_TOOLS, prompt_cache=True
    )

    sent_kwargs = provider.client.messages.create.call_args.kwargs
    assert "extra_body" not in sent_kwargs
    assert isinstance(sent_kwargs["system"], str)
    assert all("cache_control" not in t for t in sent_kwargs["tools"])


@pytest.mark.asyncio
async def test_chat_cache_ttl_1h_propagates(monkeypatch):
    monkeypatch.setattr(settings, "prompt_cache_ttl", "1h")
    provider = AnthropicProvider(api_key="test-key")
    provider.client = MagicMock()
    provider.client.messages.create = AsyncMock(return_value=_build_chat_response_stub())

    await provider.chat(messages=list(_CACHE_MESSAGES), tools=_CACHE_TOOLS)

    _assert_cached_params(provider.client.messages.create.call_args.kwargs, ttl="1h")


@pytest.mark.asyncio
async def test_chat_stream_applies_cache_breakpoints():
    provider = AnthropicProvider(api_key="test-key")
    stream_mock = _patch_stream(provider)

    async for _ in provider.chat_stream(messages=list(_CACHE_MESSAGES)):
        pass

    sent_kwargs = stream_mock.call_args.kwargs
    sent_system = sent_kwargs["system"]
    assert isinstance(sent_system, list)
    assert sent_system[-1]["cache_control"] == {"type": "ephemeral"}
    assert sent_kwargs["extra_body"]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_chat_stream_with_tools_applies_cache_breakpoints():
    provider = AnthropicProvider(api_key="test-key")
    stream_mock = _patch_stream(provider)

    await provider.chat_stream_with_tools(
        messages=list(_CACHE_MESSAGES), tools=_CACHE_TOOLS
    )

    _assert_cached_params(stream_mock.call_args.kwargs)


# ---------------------------------------------------------------------------
# Tool input_schema sanitization
#
# The Anthropic Messages API rejects tool schemas that carry oneOf / allOf /
# anyOf at the TOP level ("input_schema does not support oneOf, allOf, or
# anyOf at the top level") - one such tool 400s the whole request, including
# every failover retry on another Anthropic model. Tools with top-level
# combinators exist legitimately (OpenCRQ conditional-requirement schemas,
# arbitrary MCP servers), so the provider must strip them from the wire copy
# while the agent loop keeps validating arguments against the full schema.
# ---------------------------------------------------------------------------


_CONDITIONAL_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["create", "delete"]},
        "family_id": {"type": "string"},
        "name": {"type": "string"},
    },
    "required": ["action"],
    "allOf": [
        {
            "if": {"properties": {"action": {"const": "create"}}},
            "then": {"required": ["name"]},
        },
    ],
    "anyOf": [
        {"required": ["family_id"]},
        {"required": ["name"]},
    ],
}


def test_sanitize_input_schema_strips_top_level_combinators():
    sanitized = _sanitize_input_schema(_CONDITIONAL_SCHEMA)
    assert "allOf" not in sanitized
    assert "anyOf" not in sanitized
    assert "oneOf" not in sanitized
    # Everything else survives untouched.
    assert sanitized["type"] == "object"
    assert sanitized["required"] == ["action"]
    assert sanitized["properties"] == _CONDITIONAL_SCHEMA["properties"]
    # The shared ToolDefinition schema must never be mutated.
    assert "allOf" in _CONDITIONAL_SCHEMA
    assert "anyOf" in _CONDITIONAL_SCHEMA


def test_sanitize_input_schema_keeps_nested_combinators():
    """Only TOP-LEVEL combinators are rejected by Anthropic - a ``oneOf``
    inside a property (e.g. OpenCTI's string-or-array ``value`` field) is
    valid and must pass through unchanged.
    """
    schema = {
        "type": "object",
        "properties": {
            "value": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
            },
        },
    }
    assert _sanitize_input_schema(schema) is schema


def test_sanitize_input_schema_passthrough_when_clean():
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    assert _sanitize_input_schema(schema) is schema


@pytest.mark.asyncio
async def test_chat_strips_top_level_combinators_from_tool_schema():
    provider = AnthropicProvider(api_key="test-key")
    provider.client = MagicMock()
    provider.client.messages.create = AsyncMock(return_value=_build_chat_response_stub())

    tool = ToolDefinition(
        name="manage_family",
        description="Manage a family",
        input_schema=dict(_CONDITIONAL_SCHEMA),
    )
    await provider.chat(
        messages=[ChatMessage(role="user", content="hi")], tools=[tool]
    )

    sent_tools = provider.client.messages.create.call_args.kwargs["tools"]
    assert len(sent_tools) == 1
    sent_schema = sent_tools[0]["input_schema"]
    assert "allOf" not in sent_schema
    assert "anyOf" not in sent_schema
    assert sent_schema["properties"] == _CONDITIONAL_SCHEMA["properties"]
    # The ToolDefinition keeps the full schema for pre-dispatch validation.
    assert "allOf" in tool.input_schema


@pytest.mark.asyncio
async def test_chat_stream_with_tools_strips_top_level_combinators():
    provider = AnthropicProvider(api_key="test-key")
    stream_mock = _patch_stream(provider)

    tool = ToolDefinition(
        name="manage_family",
        description="Manage a family",
        input_schema=dict(_CONDITIONAL_SCHEMA),
    )
    await provider.chat_stream_with_tools(
        messages=[ChatMessage(role="user", content="hi")], tools=[tool]
    )

    sent_tools = stream_mock.call_args.kwargs["tools"]
    sent_schema = sent_tools[0]["input_schema"]
    assert "allOf" not in sent_schema
    assert "anyOf" not in sent_schema
    assert "allOf" in tool.input_schema


@pytest.mark.asyncio
async def test_chat_usage_includes_cache_tokens():
    """Non-streaming chat() must surface the cache token breakdown so
    Langfuse/cost tracking price cached tokens at their discounted rates
    (previously only the streaming accumulator captured these fields).
    """
    provider = AnthropicProvider(api_key="test-key")
    provider.client = MagicMock()
    response = _build_chat_response_stub()
    response.usage = SimpleNamespace(
        input_tokens=50,
        output_tokens=10,
        cache_creation_input_tokens=1200,
        cache_read_input_tokens=8000,
    )
    provider.client.messages.create = AsyncMock(return_value=response)

    result = await provider.chat(messages=[ChatMessage(role="user", content="hi")])

    assert result.usage == {
        "input_tokens": 50,
        "output_tokens": 10,
        "cache_creation_input_tokens": 1200,
        "cache_read_input_tokens": 8000,
    }


@pytest.mark.asyncio
async def test_chat_usage_omits_zero_cache_tokens():
    """Cache fields stay absent when zero so 'no caching' remains falsy
    for downstream consumers (same convention as the stream accumulator).
    """
    provider = AnthropicProvider(api_key="test-key")
    provider.client = MagicMock()
    provider.client.messages.create = AsyncMock(return_value=_build_chat_response_stub())

    result = await provider.chat(messages=[ChatMessage(role="user", content="hi")])

    assert result.usage == {"input_tokens": 1, "output_tokens": 1}


# ---------------------------------------------------------------------------
# Streaming reasoning
#
# Claude Sonnet 5, Opus 5 / 5.5, Fable 5 / 5.1 and Mythos think by default
# but default ``thinking.display`` to "omitted": their thinking blocks stream
# no ``thinking_delta`` at all, so the chat reasoning window stayed empty. A
# streaming call with an ``on_thinking`` consumer opts into the summarized
# text; nothing else changes.
# ---------------------------------------------------------------------------


async def _discard_thinking(_text: str) -> None:
    return None


_SUMMARIZED = {"type": "adaptive", "display": "summarized"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    ["claude-sonnet-5", "claude-opus-5", "claude-opus-5-5", "claude-fable-5-1", "claude-mythos-5"],
)
async def test_streaming_with_a_reasoning_consumer_asks_for_the_thinking_text(model: str):
    provider = AnthropicProvider(api_key="test-key")
    stream_mock = _patch_stream(provider)

    async for _ in provider.chat_stream(
        messages=[ChatMessage(role="user", content="hi")],
        model=model,
        on_thinking=_discard_thinking,
    ):
        pass
    await provider.chat_stream_with_tools(
        messages=[ChatMessage(role="user", content="hi")],
        tools=_CACHE_TOOLS,
        model=model,
        on_thinking=_discard_thinking,
    )

    assert [call.kwargs.get("thinking") for call in stream_mock.call_args_list] == [
        _SUMMARIZED,
        _SUMMARIZED,
    ]
    # The API refuses ``temperature`` next to an explicit ``thinking`` config.
    assert all("temperature" not in call.kwargs for call in stream_mock.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "on_thinking",
    # The agent loop's idle watchdog passes a liveness-only sink to silent runs
    # (assignments): it reads nothing, so it must not ask for the text either.
    [None, ThinkingActivitySink(lambda: None)],
    ids=["no-callback", "liveness-sink"],
)
async def test_streaming_without_a_reasoning_consumer_keeps_the_omitted_default(on_thinking):
    provider = AnthropicProvider(api_key="test-key")
    stream_mock = _patch_stream(provider)

    async for _ in provider.chat_stream(
        messages=[ChatMessage(role="user", content="hi")],
        model="claude-sonnet-5",
        on_thinking=on_thinking,
    ):
        pass
    await provider.chat_stream_with_tools(
        messages=[ChatMessage(role="user", content="hi")],
        tools=_CACHE_TOOLS,
        model="claude-sonnet-5",
        on_thinking=on_thinking,
    )

    assert stream_mock.call_count == 2
    assert all("thinking" not in call.kwargs for call in stream_mock.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    # Opus 4.7 / 4.8 omit the text too but do not think unless asked: opting
    # into a display would turn thinking on. The others return it already.
    ["claude-opus-4-8", "claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"],
)
async def test_streaming_leaves_models_outside_the_rule_alone(model: str):
    provider = AnthropicProvider(api_key="test-key")
    stream_mock = _patch_stream(provider)

    await provider.chat_stream_with_tools(
        messages=[ChatMessage(role="user", content="hi")],
        tools=_CACHE_TOOLS,
        model=model,
        on_thinking=_discard_thinking,
    )

    assert "thinking" not in stream_mock.call_args.kwargs


@pytest.mark.asyncio
async def test_chat_never_opts_into_the_thinking_text():
    """``chat()`` has no live reasoning consumer: the non-streaming request is
    the one sent before the fix, whatever kwargs ride along."""
    provider, sent = _provider_with(_build_chat_response_stub())

    await provider.chat(
        messages=[ChatMessage(role="user", content="hi")],
        model="claude-sonnet-5",
        on_thinking=_discard_thinking,
    )

    assert "thinking" not in sent[0]


class _EventStreamCtxStub:
    """``client.messages.create(stream=True)`` stub that replays scripted events."""

    def __init__(self, events: list[SimpleNamespace]):
        self._events = events

    async def __aenter__(self):
        events = iter(self._events)

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


def _thinking_block(*deltas: str) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="thinking")),
        *(
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="thinking_delta", thinking=text),
            )
            for text in deltas
        ),
        SimpleNamespace(type="content_block_stop"),
    ]


_TWO_THINKING_BLOCKS = [
    *_thinking_block("I need the host ", "list first."),
    *_thinking_block("Reading the inventory now."),
    SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="text")),
    SimpleNamespace(
        type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="Done.")
    ),
    SimpleNamespace(type="content_block_stop"),
]


@pytest.mark.asyncio
async def test_each_thinking_block_after_the_first_opens_a_new_paragraph():
    """A response carrying two thinking blocks (a progress update is a block of
    its own on Fable 5.x) must not run them together mid-sentence."""
    provider = AnthropicProvider(api_key="test-key")
    provider.client = MagicMock()
    provider.client.messages.create = AsyncMock(
        side_effect=lambda **_: _EventStreamCtxStub(_TWO_THINKING_BLOCKS)
    )
    streamed: list[str] = []

    async def _collect(text: str) -> None:
        streamed.append(text)

    text = [
        chunk
        async for chunk in provider.chat_stream(
            messages=[ChatMessage(role="user", content="hi")],
            model="claude-sonnet-5",
            on_thinking=_collect,
        )
    ]
    response = await provider.chat_stream_with_tools(
        messages=[ChatMessage(role="user", content="hi")],
        tools=_CACHE_TOOLS,
        model="claude-sonnet-5",
        on_thinking=_collect,
    )

    expected = "I need the host list first.\n\nReading the inventory now."
    assert text == ["Done."]
    assert "".join(streamed) == expected * 2
    assert response.thinking == expected
    assert response.content == "Done."


# ---------------------------------------------------------------------------
# Claude Mythos
#
# Mythos thinks by default and refuses ``thinking.type.disabled`` on every
# model (Anthropic marks its thinking "Always on"). A ``disable_thinking``
# utility call gets adaptive thinking at the lowest effort
# on its FIRST request: the capability rules declare what the provider would
# otherwise learn from a refused ``disabled``. Every other request is the one
# sent before.
# ---------------------------------------------------------------------------


_MYTHOS_MODELS = ["claude-mythos-5", "claude-mythos-5-1", "claude-mythos-preview"]


def _rules_mythos_outside_thinking_off() -> tuple:
    """The rule table with Mythos outside the thinking-off rule."""
    return tuple(
        dataclasses.replace(rule, adaptive_thinking=False, thinking_always_on=False)
        if "claude-mythos" in rule.prefixes
        else rule
        for rule in MODEL_PARAMETER_RULES
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("model", _MYTHOS_MODELS)
async def test_mythos_utility_call_sends_adaptive_lowest_effort_on_the_first_request(
    model: str,
):
    """The memory-reconciliation shape: one request, adaptive thinking at the
    lowest effort with room for that thinking on top of the answer's budget,
    nothing else added - never ``disabled``, the caller's temperature still
    omitted, and no refusal provoked to learn from.
    """
    provider, sent = _provider_with(_build_chat_response_stub())

    await provider.chat(
        messages=[
            ChatMessage(role="system", content="Reconcile the facts."),
            ChatMessage(role="user", content="hi"),
        ],
        model=model,
        max_tokens=1500,
        temperature=0.1,
        disable_thinking=True,
    )

    assert len(sent) == 1
    request = sent[0]
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"] == {"effort": _LOWEST_EFFORT}
    assert (request["model"], request["max_tokens"]) == (
        model,
        1500 + _ADAPTIVE_FORM_HEADROOM,
    )
    assert set(request) == {
        "model",
        "max_tokens",
        "messages",
        "system",
        "thinking",
        "output_config",
        "extra_body",
    }
    assert not _MODELS_REFUSING_DISABLED_THINKING


@pytest.mark.asyncio
async def test_mythos_utility_request_differs_from_before_only_in_thinking(monkeypatch):
    """The compaction shape under the previous rule carried no ``thinking`` at
    all, so Mythos thought at its default effort; now only the thinking-off
    form and the room it needs are added, the rest of the request is
    unchanged.
    """

    async def compaction_request() -> dict:
        provider, sent = _provider_with(_build_chat_response_stub())
        await provider.chat(
            messages=[
                ChatMessage(role="system", content="Compact the conversation."),
                ChatMessage(role="user", content="hi"),
            ],
            model="claude-mythos-5",
            max_tokens=2000,
            temperature=0.2,
            disable_thinking=True,
        )
        return sent[0]

    after = await compaction_request()
    monkeypatch.setattr(
        "app.llm.model_capabilities.MODEL_PARAMETER_RULES", _rules_mythos_outside_thinking_off()
    )
    before = await compaction_request()

    assert "thinking" not in before
    assert "output_config" not in before
    assert after["thinking"] == {"type": "adaptive"}
    assert after["output_config"] == {"effort": _LOWEST_EFFORT}
    assert after["max_tokens"] == before["max_tokens"] + _ADAPTIVE_FORM_HEADROOM
    varied = ("thinking", "output_config", "max_tokens")
    assert {k: v for k, v in after.items() if k not in varied} == {
        k: v for k, v in before.items() if k not in varied
    }


@pytest.mark.asyncio
async def test_mythos_lowest_effort_overrides_a_caller_reasoning_effort():
    """Thinking cannot be switched off on Mythos, so, as on a remembered model,
    ``disable_thinking`` writes the lowest effort over the caller's value.
    """
    provider, sent = _provider_with(_build_chat_response_stub())

    await provider.chat(
        messages=[ChatMessage(role="user", content="hi")],
        model="claude-mythos-5",
        disable_thinking=True,
        reasoning_effort="high",
    )

    assert sent[0]["thinking"] == {"type": "adaptive"}
    assert sent[0]["output_config"] == {"effort": _LOWEST_EFFORT}


@pytest.mark.asyncio
async def test_mythos_is_never_sent_disabled_thinking_even_after_a_bad_request():
    """A 400 on the adaptive form propagates after ONE call, for the router's
    failover: the refusal branch only ever retries a request that sent
    ``disabled``, so it cannot send Mythos the form it refuses.
    """
    failure = _status_error(400, "output_config.effort: unsupported value")
    provider, sent = _provider_with(failure)

    with pytest.raises(anthropic.BadRequestError) as exc_info:
        await provider.chat(
            messages=[ChatMessage(role="user", content="hi")],
            model="claude-mythos-5",
            disable_thinking=True,
        )

    assert exc_info.value is failure
    assert len(sent) == 1
    assert sent[0]["thinking"] == {"type": "adaptive"}
    assert not _MODELS_REFUSING_DISABLED_THINKING


async def _mythos_main_requests() -> list[dict]:
    """Every request Mythos gets outside a ``disable_thinking`` call, in order:
    both streaming paths without and with a reasoning consumer, then
    ``chat()`` without the hint and with it explicitly off.
    """
    provider = AnthropicProvider(api_key="test-key")
    stream_mock = _patch_stream(provider)
    chat_create, sent = _recording_create(
        _build_chat_response_stub(), _build_chat_response_stub()
    )

    async def _create(**kwargs):
        return await (stream_mock if kwargs.get("stream") else chat_create)(**kwargs)

    provider.client.messages.create = _create
    for on_thinking in (None, _discard_thinking):
        async for _ in provider.chat_stream(
            messages=list(_CACHE_MESSAGES),
            model="claude-mythos-5",
            on_thinking=on_thinking,
        ):
            pass
        await provider.chat_stream_with_tools(
            messages=list(_CACHE_MESSAGES),
            tools=_CACHE_TOOLS,
            model="claude-mythos-5",
            on_thinking=on_thinking,
        )
    for kwargs in ({}, {"disable_thinking": False}):
        await provider.chat(
            messages=list(_CACHE_MESSAGES),
            tools=_CACHE_TOOLS,
            model="claude-mythos-5",
            **kwargs,
        )
    return [copy.deepcopy(call.kwargs) for call in stream_mock.call_args_list] + sent


@pytest.mark.asyncio
async def test_mythos_main_requests_are_identical_to_before(monkeypatch):
    """The flag is read by ``chat()``'s thinking-off rule only: the agent
    loop's streaming calls and a ``chat()`` without the hint send, byte for
    byte, what they sent under the previous rule - no temperature, no effort,
    and a ``thinking`` config only as the display opt-in.
    """
    after = await _mythos_main_requests()
    monkeypatch.setattr(
        "app.llm.model_capabilities.MODEL_PARAMETER_RULES", _rules_mythos_outside_thinking_off()
    )
    before = await _mythos_main_requests()

    assert after == before
    assert all("temperature" not in request for request in after)
    assert all("output_config" not in request for request in after)
    assert [request.get("thinking") for request in after] == [
        None,
        None,
        _SUMMARIZED,
        _SUMMARIZED,
        None,
        None,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-5",
        "claude-opus-5",
        "claude-opus-4-7",
        "claude-opus-4-8",
    ],
)
async def test_models_that_accept_disabled_thinking_are_sent_it(model: str):
    """A model that runs adaptive thinking by default and accepts ``disabled``
    gets it, with the caller's budget and no effort of its own."""
    provider, sent = _provider_with(_build_chat_response_stub())

    await provider.chat(
        messages=[ChatMessage(role="user", content="hi")],
        model=model,
        disable_thinking=True,
    )

    assert len(sent) == 1
    assert sent[0]["thinking"] == {"type": "disabled"}
    assert "output_config" not in sent[0]
    assert sent[0]["max_tokens"] == 4096


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-fable-5", "claude-fable-5-1"])
async def test_declared_always_on_models_get_the_adaptive_form_on_the_first_request(
    model: str,
):
    """Opus 5.5 and Fable 5 / 5.1 refuse ``disabled`` at every effort: no
    refused request is spent learning it."""
    provider, sent = _provider_with(_build_chat_response_stub())

    await provider.chat(
        messages=[ChatMessage(role="user", content="hi")],
        model=model,
        reasoning_effort="max",
        disable_thinking=True,
    )

    assert len(sent) == 1
    assert sent[0]["thinking"] == {"type": "adaptive"}
    assert sent[0]["output_config"] == {"effort": _LOWEST_EFFORT}
    assert sent[0]["max_tokens"] == 4096 + _ADAPTIVE_FORM_HEADROOM
    assert not _MODELS_REFUSING_DISABLED_THINKING


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reasoning_effort", "output_config"),
    [
        (None, None),
        ("medium", {"effort": "medium"}),
        ("high", {"effort": "high"}),
        ("max", {"effort": "high"}),
    ],
)
async def test_sonnet_5_5_turns_thinking_off_with_between_tools(
    reasoning_effort: str | None, output_config: dict | None
):
    """Sonnet 5.5 refuses ``disabled``; ``between_tools`` is its lowest
    setting, takes no other thinking field, needs no room for thinking, and is
    refused above effort ``high``."""
    provider, sent = _provider_with(_build_chat_response_stub())

    await provider.chat(
        messages=[ChatMessage(role="user", content="hi")],
        model="claude-sonnet-5-5",
        temperature=0.1,
        reasoning_effort=reasoning_effort,
        disable_thinking=True,
    )

    assert len(sent) == 1
    assert sent[0]["thinking"] == {"type": "between_tools"}
    assert sent[0].get("output_config") == output_config
    assert sent[0]["max_tokens"] == 4096
    assert "temperature" not in sent[0]
    assert not _MODELS_REFUSING_DISABLED_THINKING


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("disable_thinking", "thinking", "effort"),
    [(True, {"type": "disabled"}, "high"), (False, None, "max")],
)
async def test_opus_5_keeps_disabled_thinking_within_its_effort_ceiling(
    disable_thinking: bool, thinking: dict | None, effort: str
):
    """Opus 5 accepts ``disabled`` at effort ``high`` or below only; a call
    that keeps thinking on keeps the effort it asked for."""
    provider, sent = _provider_with(_build_chat_response_stub())

    await provider.chat(
        messages=[ChatMessage(role="user", content="hi")],
        model="claude-opus-5",
        reasoning_effort="max",
        disable_thinking=disable_thinking,
    )

    assert sent[0].get("thinking") == thinking
    assert sent[0]["output_config"] == {"effort": effort}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "requested", "output_config"),
    [
        ("claude-haiku-4-5", "high", None),
        ("claude-sonnet-4-5", "max", None),
        ("claude-opus-4-5", "max", {"effort": "high"}),
        ("claude-opus-4-6", "max", {"effort": "max"}),
        ("claude-opus-5-5", "max", {"effort": "max"}),
    ],
)
async def test_every_request_path_sends_an_effort_the_model_accepts(
    model: str, requested: str, output_config: dict | None
):
    """``chat``, ``chat_stream`` and ``chat_stream_with_tools`` all hand the
    model an effort level it accepts, or none."""
    provider, sent = _provider_with(_build_chat_response_stub())
    await provider.chat(
        messages=[ChatMessage(role="user", content="hi")],
        model=model,
        reasoning_effort=requested,
    )
    stream_mock = _patch_stream(provider)
    async for _ in provider.chat_stream(
        messages=[ChatMessage(role="user", content="hi")],
        model=model,
        reasoning_effort=requested,
    ):
        pass
    await provider.chat_stream_with_tools(
        messages=[ChatMessage(role="user", content="hi")],
        tools=_CACHE_TOOLS,
        model=model,
        reasoning_effort=requested,
    )

    streamed = [call.kwargs for call in stream_mock.call_args_list]
    assert [request.get("output_config") for request in (*sent, *streamed)] == [
        output_config
    ] * 3


@pytest.mark.parametrize(
    "model", [*_MYTHOS_MODELS, "claude-fable-5", "claude-fable-5-1", "claude-opus-5-5"]
)
def test_adaptive_form_budget_fits_the_output_ceiling_of_every_model_that_takes_it(
    model: str,
):
    """Every model declared ``thinking_always_on``: the largest
    ``disable_thinking`` budget - a compaction retry after a cut, twice
    ``_COMPACTOR_MAX_TOKENS`` - plus the margin must stay a request each of
    them accepts (a ``max_tokens`` over the ceiling is an HTTP 400)."""
    from app.llm.model_limits import resolve_effective_max_output_tokens
    from app.services.conversation_memory import _COMPACTOR_MAX_TOKENS

    ceiling = resolve_effective_max_output_tokens(
        model=model, overrides=None, provider_default=None, fallback=None
    )

    assert ceiling is not None
    assert ceiling >= 2 * _COMPACTOR_MAX_TOKENS + _ADAPTIVE_FORM_HEADROOM
