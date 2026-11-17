"""A model's decline surfaces as one (Anthropic ``stop_reason: "refusal"``).

Claude Sonnet 5.5 declines in more categories than Sonnet 5 (``cyber``,
``bio``, ``frontier_llm``, ``reasoning_extraction``, ``general_harms``). Every
Anthropic path read the stop reason as ``end_turn``: a declined turn came back
as an empty or partial answer, the agent loop retried it as an empty response,
and failover never ran because the call returned 200. The provider now raises
``ModelDeclinedError``; the failover wrapper hands a ``cyber`` /
``frontier_llm`` decline to another model (the categories Anthropic's own
server-side fallback retries) and stops on the others; the run's failure is
classified ``model_declined`` and worded as a decline.

The OpenAI-format providers (the gateway, native OpenAI, OpenRouter, Mistral,
Azure, ...) raise the same error on a ``content_filter`` finish; those cases
sit under "The OpenAI-format providers raise it" below.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from anthropic.types import (
    Message,
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    RawContentBlockStopEvent,
    RawMessageDeltaEvent,
    RawMessageStartEvent,
    RawMessageStopEvent,
)
from openai.types.chat import ChatCompletion
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import (
    BaseLLMProvider,
    ChatMessage,
    ChatResponse,
    ModelDeclinedError,
    StreamUsageSink,
    declined_error,
)
from app.llm.openai_compat import OpenAICompatibleProvider
from app.llm.openai_provider import OpenAIProvider
from app.llm.router import AllProvidersFailedError, FailoverLLMProvider
from app.services.failure_causes import (
    FailureCause,
    FailureDetermination,
    classify_run_failure,
)
from app.services.tracing.cost_calculator import ModelPrice
from app.tools import agent_loop as al
from app.tools.base import ToolDefinition
from app.tools.run_budget import RunBudget, count_usage_tokens

_MODEL = "claude-sonnet-5-5"
_CYBER = SimpleNamespace(
    type="refusal", category="cyber", explanation="This request could enable cyber harm."
)
_TOOLS = [
    ToolDefinition(
        name="lookup",
        description="Look a key up.",
        input_schema={"type": "object", "properties": {"key": {"type": "string"}}},
    )
]


def _messages() -> list[ChatMessage]:
    return [ChatMessage(role="user", content="Analyse this sample.")]


def _provider() -> AnthropicProvider:
    provider = AnthropicProvider(api_key="test-key")
    provider.client = MagicMock()
    return provider


def _declined_response(stop_details: object = _CYBER) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text="I can't help with")],
        stop_reason="refusal",
        stop_details=stop_details,
        model=_MODEL,
        usage=SimpleNamespace(input_tokens=120, output_tokens=7),
    )


def _declined_events(stop_details: object = _CYBER) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(usage=SimpleNamespace(input_tokens=120, output_tokens=1)),
        ),
        SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="text")),
        SimpleNamespace(
            type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="Sure, the")
        ),
        SimpleNamespace(type="content_block_stop"),
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="refusal", stop_details=stop_details),
            usage=SimpleNamespace(output_tokens=7),
        ),
    ]


class _EventStream:
    """``messages.create(stream=True)`` stand-in replaying scripted events."""

    def __init__(self, events: list[SimpleNamespace]) -> None:
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


def _streaming_provider(events: list[SimpleNamespace]) -> AnthropicProvider:
    provider = _provider()
    provider.client.messages.create = AsyncMock(side_effect=lambda **_: _EventStream(events))
    return provider


# ── The provider raises the decline on every path ───────────────────────────


async def test_chat_raises_the_decline_with_its_category_and_usage():
    provider = _provider()
    provider.client.messages.create = AsyncMock(return_value=_declined_response())

    with pytest.raises(ModelDeclinedError) as caught:
        await provider.chat(messages=_messages(), model=_MODEL)

    assert caught.value.model == _MODEL
    assert caught.value.category == "cyber"
    assert caught.value.explanation == "This request could enable cyber harm."
    assert caught.value.usage == {"input_tokens": 120, "output_tokens": 7}
    # Retried once by the transient-error policy, it would be billed twice.
    provider.client.messages.create.assert_awaited_once()


async def test_chat_stream_raises_the_decline_after_reporting_its_usage():
    provider = _streaming_provider(_declined_events())
    sink = StreamUsageSink()

    with pytest.raises(ModelDeclinedError) as caught:
        async for _chunk in provider.chat_stream(messages=_messages(), model=_MODEL, usage_sink=sink):
            pass

    assert caught.value.category == "cyber"
    assert caught.value.usage == {"input_tokens": 120, "output_tokens": 7}
    assert sink.take() == {"input_tokens": 120, "output_tokens": 7}


async def test_chat_stream_with_tools_raises_the_decline_after_reporting_its_usage():
    provider = _streaming_provider(_declined_events())
    sink = StreamUsageSink()

    with pytest.raises(ModelDeclinedError) as caught:
        await provider.chat_stream_with_tools(
            messages=_messages(), tools=_TOOLS, model=_MODEL, usage_sink=sink
        )

    assert caught.value.category == "cyber"
    assert sink.take() == {"input_tokens": 120, "output_tokens": 7}
    provider.client.messages.create.assert_awaited_once()


@pytest.mark.parametrize("stop_details", [None, SimpleNamespace(type="refusal", category=None)])
async def test_a_decline_naming_no_category_still_raises(stop_details):
    provider = _provider()
    provider.client.messages.create = AsyncMock(return_value=_declined_response(stop_details))

    with pytest.raises(ModelDeclinedError) as caught:
        await provider.chat(messages=_messages(), model=_MODEL)

    assert caught.value.category is None
    assert caught.value.failover_allowed is False


async def test_an_answer_that_was_not_declined_is_unchanged():
    provider = _provider()
    provider.client.messages.create = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Done.")],
            stop_reason="end_turn",
            model=_MODEL,
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )
    )

    response = await provider.chat(messages=_messages(), model=_MODEL)

    assert response.content == "Done."
    assert response.stop_reason == "end_turn"


# ── Failover hands on only the categories Anthropic itself retries ─────────


@pytest.mark.parametrize(
    ("category", "fails_over"),
    [
        ("cyber", True),
        ("frontier_llm", True),
        ("bio", False),
        ("reasoning_extraction", False),
        ("general_harms", False),
        (None, False),
    ],
)
def test_which_declines_fail_over(category: str | None, fails_over: bool):
    exc = ModelDeclinedError(model=_MODEL, category=category)

    assert exc.failover_allowed is fails_over
    assert FailoverLLMProvider._is_non_failoverable(exc) is not fails_over


class _Declines(BaseLLMProvider):
    provider_name = "anthropic"
    default_model = _MODEL
    context_window = 100_000

    def __init__(self, category: str | None) -> None:
        self.category = category

    async def chat(self, messages, model=None, **kwargs):
        raise ModelDeclinedError(model=model or _MODEL, category=self.category)

    async def chat_stream(self, messages, model=None, **kwargs):
        raise ModelDeclinedError(model=model or _MODEL, category=self.category)
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


class _Answers(_Declines):
    provider_name = "openai"
    default_model = "gpt-6"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, model=None, **kwargs):
        self.calls += 1
        return ChatResponse(content="the fallback's answer", model=model or "gpt-6")


async def test_a_cyber_decline_is_answered_by_the_fallback():
    fallback = _Answers()
    wrapper = FailoverLLMProvider(primary=_Declines("cyber"), fallbacks=[fallback])

    response = await wrapper.chat(_messages(), model=_MODEL)

    assert response.content == "the fallback's answer"
    assert fallback.calls == 1


async def test_a_bio_decline_is_not_handed_to_another_model():
    fallback = _Answers()
    wrapper = FailoverLLMProvider(primary=_Declines("bio"), fallbacks=[fallback])

    with pytest.raises(ModelDeclinedError):
        await wrapper.chat(_messages(), model=_MODEL)

    assert fallback.calls == 0


# ── The run's failure says the model declined ──────────────────────────────


def test_a_decline_is_classified_structurally_with_its_category():
    result = classify_run_failure(ModelDeclinedError(model=_MODEL, category="cyber"))

    assert result.cause is FailureCause.MODEL_DECLINED
    assert result.determination is FailureDetermination.STRUCTURAL
    assert result.detail == "cyber"


def test_a_chain_ending_on_a_decline_failed_on_the_decline():
    exc = AllProvidersFailedError(
        [
            ("anthropic", ModelDeclinedError(model=_MODEL, category="cyber")),
            ("bedrock", ModelDeclinedError(model="anthropic.claude-sonnet-5", category="cyber")),
        ]
    )

    result = classify_run_failure(exc)

    assert result.cause is FailureCause.MODEL_DECLINED
    assert result.detail == "cyber"


def test_a_chain_whose_last_leg_failed_otherwise_keeps_its_cause():
    class _OverloadedError(Exception):
        status_code = 529

    exc = AllProvidersFailedError(
        [
            ("anthropic", ModelDeclinedError(model=_MODEL, category="cyber")),
            ("openai", _OverloadedError("overloaded")),
        ]
    )

    assert classify_run_failure(exc).cause is FailureCause.ALL_PROVIDERS_FAILED


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("cyber", "cybersecurity safeguards"),
        ("bio", "declined to answer this request under its usage policy"),
        (None, "declined to answer this request under its usage policy"),
    ],
)
def test_chat_words_a_decline_as_one(category: str | None, expected: str):
    from app.api.v1.chat import _friendly_error_message

    message = _friendly_error_message(ModelDeclinedError(model=_MODEL, category=category))

    assert expected in message
    assert "Something went wrong" not in message


# ── The SDK's own shapes ────────────────────────────────────────────────────
# The API names more categories than the pinned SDK's ``RefusalStopDetails``
# literal (``cyber`` / ``bio``). The SDK builds a response without validating
# it, so ``frontier_llm`` reaches the provider as a plain string.

_SDK_REFUSAL = {"type": "refusal", "category": "frontier_llm", "explanation": "Declined."}
_SDK_USAGE = {"input_tokens": 120, "output_tokens": 7}


def _sdk_message() -> Message:
    return Message.construct(
        id="msg_declined",
        type="message",
        role="assistant",
        model=_MODEL,
        content=[{"type": "text", "text": "I can't help with"}],
        stop_reason="refusal",
        stop_sequence=None,
        stop_details=_SDK_REFUSAL,
        usage=_SDK_USAGE,
    )


def _sdk_events() -> list:
    return [
        RawMessageStartEvent.construct(
            type="message_start",
            message={
                "id": "msg_declined",
                "type": "message",
                "role": "assistant",
                "model": _MODEL,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 120, "output_tokens": 1},
            },
        ),
        RawContentBlockStartEvent.construct(
            type="content_block_start", index=0, content_block={"type": "text", "text": ""}
        ),
        RawContentBlockDeltaEvent.construct(
            type="content_block_delta", index=0, delta={"type": "text_delta", "text": "Sure, the"}
        ),
        RawContentBlockStopEvent.construct(type="content_block_stop", index=0),
        RawMessageDeltaEvent.construct(
            type="message_delta",
            delta={"stop_reason": "refusal", "stop_sequence": None, "stop_details": _SDK_REFUSAL},
            usage={"output_tokens": 7},
        ),
        RawMessageStopEvent.construct(type="message_stop"),
    ]


async def test_an_sdk_message_declining_raises_with_its_category():
    provider = _provider()
    provider.client.messages.create = AsyncMock(return_value=_sdk_message())

    with pytest.raises(ModelDeclinedError) as caught:
        await provider.chat(messages=_messages(), model=_MODEL)

    assert caught.value.category == "frontier_llm"
    assert caught.value.explanation == "Declined."
    assert caught.value.usage == _SDK_USAGE
    assert caught.value.failover_allowed is True


@pytest.mark.parametrize("path", ["chat_stream", "chat_stream_with_tools"])
async def test_an_sdk_stream_declining_raises_with_its_category(path: str):
    provider = _streaming_provider(_sdk_events())
    sink = StreamUsageSink()

    with pytest.raises(ModelDeclinedError) as caught:
        if path == "chat_stream":
            async for _chunk in provider.chat_stream(
                messages=_messages(), model=_MODEL, usage_sink=sink
            ):
                pass
        else:
            await provider.chat_stream_with_tools(
                messages=_messages(), tools=_TOOLS, model=_MODEL, usage_sink=sink
            )

    assert caught.value.category == "frontier_llm"
    assert caught.value.usage == _SDK_USAGE
    assert sink.take() == _SDK_USAGE


async def test_an_sdk_frontier_llm_decline_is_answered_by_the_fallback():
    provider = _provider()
    provider.client.messages.create = AsyncMock(return_value=_sdk_message())
    fallback = _Answers()

    response = await FailoverLLMProvider(primary=provider, fallbacks=[fallback]).chat(
        _messages(), model=_MODEL
    )

    assert response.content == "the fallback's answer"
    assert fallback.calls == 1


# ── The run is charged for what a decline consumed ──────────────────────────

_PRICE = ModelPrice(
    provider="test",
    model_id="test-model",
    model_pattern=None,
    input_price_per_1m=Decimal("3"),
    output_price_per_1m=Decimal("15"),
    cache_read_price_per_1m=Decimal("0.30"),
    cache_write_price_per_1m=Decimal("3.75"),
    effective_from=date(2026, 1, 1),
    is_custom=False,
)
_DECLINED_USAGE = {"input_tokens": 120, "output_tokens": 7}


async def test_a_decline_the_loop_did_not_stream_is_charged_to_the_run():
    """Without ``on_token`` (an assignment run) the loop calls ``chat()``,
    which reports to no usage sink: what the decline consumed comes off the
    error."""
    provider = _provider()
    provider.client.messages.create = AsyncMock(return_value=_declined_response())
    budget = RunBudget(max_total_tokens=1_000_000, price=_PRICE)

    with pytest.raises(ModelDeclinedError):
        await al.run_agent_loop(
            llm=provider,
            messages=_messages(),
            model=_MODEL,
            max_iterations=2,
            max_tokens=1_024,
            progressive_tools=False,
            budget=budget,
        )

    assert budget.spent_tokens == count_usage_tokens(_DECLINED_USAGE) > 0


def test_a_streamed_decline_is_charged_once():
    """The sink already holds what a streamed decline consumed; the copy on
    the error is not added to it."""
    sink = StreamUsageSink()
    sink.report(dict(_DECLINED_USAGE))
    merged: list[dict] = []

    al._end_aborted_generation(
        None,
        ModelDeclinedError(model=_MODEL, usage=dict(_DECLINED_USAGE)),
        sink,
        lambda usage, served_model=None: merged.append(usage),
    )

    assert merged == [_DECLINED_USAGE]


# ── The proxy answers a decline as OpenAI's content filter ─────────────────


@pytest.fixture()
def _proxy(monkeypatch):
    from app.api.v1 import llm_proxy

    async def _track(*_args, **_kwargs):
        return None

    monkeypatch.setattr(llm_proxy, "_end_langfuse_trace", lambda *_a, **_k: None)
    monkeypatch.setattr(llm_proxy, "_track_model_usage", _track)
    return llm_proxy


def _proxy_body(*, stream: bool) -> SimpleNamespace:
    return SimpleNamespace(
        stream=stream, temperature=None, reasoning_effort=None, top_p=None, stop=None,
        tools=None, tool_choice=None, model_extra=None,
    )


_BIO = SimpleNamespace(type="refusal", category="bio", explanation=None)


def _declining_wrapper() -> FailoverLLMProvider:
    """``model: "auto"`` with several providers: the failover wrapper has no
    raw client, so the proxy reaches it through ``provider.chat()``."""
    provider = _provider()
    provider.client.messages.create = AsyncMock(return_value=_declined_response(_BIO))
    return FailoverLLMProvider(primary=provider, fallbacks=[])


@pytest.mark.parametrize(
    ("make_provider", "usage"),
    [
        pytest.param(_declining_wrapper, _DECLINED_USAGE, id="decline"),
        pytest.param(
            lambda: FailoverLLMProvider(primary=_Declines("cyber"), fallbacks=[_Declines("bio")]),
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            id="chain-ending-on-a-decline",
        ),
    ],
)
async def test_the_proxy_answers_a_decline_as_content_filter(_proxy, make_provider, usage):
    response = await _proxy._proxy_via_provider_chat(
        make_provider(), _proxy_body(stream=False), [{"role": "user", "content": "hi"}],
        _MODEL, 100, None, user_id="u1",
    )

    choice = response["choices"][0]
    assert choice["finish_reason"] == "content_filter"
    assert choice["message"]["content"] == ""
    assert response["usage"] == usage


async def test_the_proxy_stream_ends_a_decline_on_content_filter(_proxy):
    wrapper = FailoverLLMProvider(
        primary=_streaming_provider(_declined_events(_BIO)), fallbacks=[]
    )

    response = await _proxy._proxy_via_provider_chat(
        wrapper, _proxy_body(stream=True), [{"role": "user", "content": "hi"}],
        _MODEL, 100, None, user_id="u1",
    )

    frames = [frame async for frame in response.body_iterator]
    payloads = [
        json.loads(frame[len("data: ") :]) for frame in frames if frame.startswith("data: {")
    ]
    assert not any("error" in payload for payload in payloads)
    assert payloads[-1]["choices"][0]["finish_reason"] == "content_filter"
    assert frames[-1] == "data: [DONE]\n\n"


# ── The proxy's trace records a decline as one ──────────────────────────────


@pytest.fixture()
def _traces(monkeypatch):
    """Every trace the real ``end_trace`` hands to the durable sink."""
    from app.api.v1 import llm_proxy

    async def _track(*_args, **_kwargs):
        return None

    monkeypatch.setattr(llm_proxy, "_track_model_usage", _track)
    submitted: list = []
    monkeypatch.setattr(
        "app.services.tracing.dispatcher.submit_trace_for_flush",
        lambda trace, _spans: submitted.append(trace),
    )
    return llm_proxy, submitted


def _root_trace_ctx() -> dict:
    return {
        "trace": None,
        "generation": None,
        "client": None,
        "parent_ctx": None,
        "metadata": {"source": "llm-proxy"},
        "input_data": {"messages": []},
        "local_spans": [],
        "local_span_counter": 0,
        "local_trace_id": uuid.uuid4(),
        "local_source": "llm-proxy",
        "local_name": "llm-proxy",
        "local_started_at": datetime.now(UTC),
        "local_metadata": {"source": "llm-proxy"},
        "local_tags": [],
    }


def _assert_traced_as_declined(trace) -> None:
    assert trace.status == "error"
    assert trace.failure_cause == "model_declined"
    assert trace.usage_details is not None


async def test_the_provider_bridge_traces_a_decline_as_model_declined(_traces):
    llm_proxy, traces = _traces

    await llm_proxy._proxy_via_provider_chat(
        _declining_wrapper(), _proxy_body(stream=False), [{"role": "user", "content": "hi"}],
        _MODEL, 100, _root_trace_ctx(), user_id="u1",
    )

    [trace] = traces
    _assert_traced_as_declined(trace)


async def test_the_provider_bridge_stream_traces_a_decline_as_model_declined(_traces):
    llm_proxy, traces = _traces
    wrapper = FailoverLLMProvider(
        primary=_streaming_provider(_declined_events(_BIO)), fallbacks=[]
    )

    response = await llm_proxy._proxy_via_provider_chat(
        wrapper, _proxy_body(stream=True), [{"role": "user", "content": "hi"}],
        _MODEL, 100, _root_trace_ctx(), user_id="u1",
    )
    _frames = [frame async for frame in response.body_iterator]

    [trace] = traces
    _assert_traced_as_declined(trace)


class _BridgeStream:
    """``client.messages.stream(...)``: the events, then the final message."""

    def __init__(self, events: list, final: Message) -> None:
        self._events = list(events)
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)

    async def get_final_message(self) -> Message:
        return self._final


def _bridge_final(stop_reason: str) -> Message:
    message = _sdk_message()
    message.stop_reason = stop_reason
    if stop_reason != "refusal":
        message.stop_details = None
    return message


def _bridge_provider(final: Message) -> AnthropicProvider:
    provider = AnthropicProvider(api_key="test-key")
    provider.client = SimpleNamespace(
        messages=SimpleNamespace(
            stream=lambda **_kw: _BridgeStream(_sdk_events(), final),
            create=AsyncMock(return_value=final),
        )
    )
    return provider


def _bridge_body(*, stream: bool) -> SimpleNamespace:
    return SimpleNamespace(
        stream=stream, temperature=None, reasoning_effort=None, top_p=None, stop=None,
        tools=None, tool_choice=None, model_extra=None,
    )


@pytest.mark.parametrize(
    ("stop_reason", "finish_reason"),
    [("refusal", "content_filter"), ("max_tokens", "length"), ("end_turn", "stop")],
)
async def test_the_anthropic_bridge_stream_ends_on_the_finish_reason_of_its_stop(
    _traces, stop_reason: str, finish_reason: str
):
    llm_proxy, traces = _traces

    response = await llm_proxy._proxy_via_anthropic(
        _bridge_provider(_bridge_final(stop_reason)), _bridge_body(stream=True),
        [{"role": "user", "content": "hi"}], _MODEL, 1_024, _root_trace_ctx(), user_id="u1",
    )
    frames = [frame async for frame in response.body_iterator]
    payloads = [
        json.loads(frame[len("data: ") :]) for frame in frames if frame.startswith("data: {")
    ]

    assert payloads[-1]["choices"][0]["finish_reason"] == finish_reason
    assert frames[-1] == "data: [DONE]\n\n"
    [trace] = traces
    if stop_reason == "refusal":
        _assert_traced_as_declined(trace)
    else:
        assert trace.status == "ok"
        assert trace.failure_cause is None


async def test_the_anthropic_bridge_traces_a_decline_as_model_declined(_traces):
    llm_proxy, traces = _traces

    response = await llm_proxy._proxy_via_anthropic(
        _bridge_provider(_bridge_final("refusal")), _bridge_body(stream=False),
        [{"role": "user", "content": "hi"}], _MODEL, 1_024, _root_trace_ctx(), user_id="u1",
    )

    assert response["choices"][0]["finish_reason"] == "content_filter"
    [trace] = traces
    _assert_traced_as_declined(trace)


# ── Bedrock says once that it drops a structured output ────────────────────


def test_bedrock_says_once_that_it_serves_sonnet_5_5_without_structured_outputs(caplog):
    from app.llm.bedrock_provider import _apply_structured_output

    schema = {"textFormat": {"type": "json_schema"}}
    with caplog.at_level("WARNING", logger="app.llm.bedrock_provider"):
        for model in ("anthropic.claude-sonnet-5-5", "anthropic.claude-sonnet-5-5"):
            params: dict = {}
            _apply_structured_output(params, model, schema)
            assert "outputConfig" not in params
        params = {}
        _apply_structured_output(params, "anthropic.claude-sonnet-5", schema)
        assert params["outputConfig"] == schema

    notices = [r for r in caplog.records if "without structured outputs" in r.getMessage()]
    assert len(notices) == 1
    assert "anthropic.claude-sonnet-5-5" in notices[0].getMessage()


# ── The OpenAI-format providers raise it on a ``content_filter`` finish ────
# The gateway answers a model refusal with ``finish_reason: "content_filter"``
# and ``stop_details`` on the choice. The SDK leaves that
# field raw, so it is a plain ``dict``. Every provider reads the finish
# reason three ways (``chat``, ``chat_stream``, the shared tool stream).

_GATEWAY_USAGE = {"prompt_tokens": 120, "completion_tokens": 7, "total_tokens": 127}
_GATEWAY_STOP_DETAILS = {"category": "cyber", "explanation": "This request could enable cyber harm."}


@pytest.fixture(params=[OpenAICompatibleProvider, OpenAIProvider], ids=["compat", "openai"])
def provider_cls(request):
    return request.param


def _oai_provider(provider_cls, create) -> BaseLLMProvider:
    provider = provider_cls(api_key="sk-test")
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return provider


def _oai_response(finish_reason: str, *, content: str | None = None, **choice) -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 1,
            "model": _MODEL,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message": {"role": "assistant", "content": content},
                    **choice,
                }
            ],
            "usage": _GATEWAY_USAGE,
        }
    )


def _oai_stream(
    finish_reason: str, *, text: tuple[str, ...] = (), then: str | None = None, **choice
) -> _Chunks:
    def chunk(delta: dict, finish: str | None = None, **extra) -> ChatCompletionChunk:
        return ChatCompletionChunk.model_validate(
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": _MODEL,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish, **extra}],
            }
        )

    usage = ChatCompletionChunk.model_validate(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": _MODEL,
            "choices": [],
            "usage": _GATEWAY_USAGE,
        }
    )
    return _Chunks(
        [chunk({"content": part}) for part in text]
        + [chunk({}, finish_reason, **choice)]
        + ([chunk({}, then)] if then else [])
        + [usage]
    )


class _Chunks:
    def __init__(self, items: list) -> None:
        self._items = items

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for item in self._items:
            yield item


def _refusal(**overrides) -> dict:
    return {"stop_details": _GATEWAY_STOP_DETAILS, **overrides}


async def _stream_text(provider, **kwargs) -> list[str]:
    return [chunk async for chunk in provider.chat_stream(messages=_messages(), model=_MODEL, **kwargs)]


async def test_an_openai_format_chat_raises_the_gateway_decline(provider_cls):
    create = AsyncMock(return_value=_oai_response("content_filter", **_refusal()))
    provider = _oai_provider(provider_cls, create)

    with pytest.raises(ModelDeclinedError) as caught:
        await provider.chat(messages=_messages(), model=_MODEL)

    assert caught.value.model == _MODEL
    assert caught.value.category == "cyber"
    assert caught.value.explanation == "This request could enable cyber harm."
    assert caught.value.usage["prompt_tokens"] == 120
    assert caught.value.usage["completion_tokens"] == 7
    # Retried once by the transient-error policy, it would be billed twice.
    create.assert_awaited_once()


async def test_an_openai_format_chat_stream_raises_the_gateway_decline_after_reporting_its_usage(
    provider_cls,
):
    create = AsyncMock(return_value=_oai_stream("content_filter", **_refusal()))
    provider = _oai_provider(provider_cls, create)
    sink = StreamUsageSink()

    with pytest.raises(ModelDeclinedError) as caught:
        await _stream_text(provider, usage_sink=sink)

    assert caught.value.model == _MODEL
    assert caught.value.category == "cyber"
    assert caught.value.explanation == "This request could enable cyber harm."
    assert caught.value.usage["prompt_tokens"] == 120
    assert sink.take()["prompt_tokens"] == 120
    create.assert_awaited_once()


async def test_an_openai_format_tool_stream_raises_the_gateway_decline_after_reporting_its_usage(
    provider_cls,
):
    create = AsyncMock(return_value=_oai_stream("content_filter", **_refusal()))
    provider = _oai_provider(provider_cls, create)
    sink = StreamUsageSink()

    with pytest.raises(ModelDeclinedError) as caught:
        await provider.chat_stream_with_tools(
            messages=_messages(), tools=_TOOLS, model=_MODEL, usage_sink=sink
        )

    assert caught.value.model == _MODEL
    assert caught.value.category == "cyber"
    assert caught.value.explanation == "This request could enable cyber harm."
    assert caught.value.usage["prompt_tokens"] == 120
    assert sink.take()["prompt_tokens"] == 120
    create.assert_awaited_once()


@pytest.mark.parametrize(
    ("finish_reason", "stop_reason"),
    [("stop", "end_turn"), ("length", "max_tokens"), ("tool_calls", "tool_use")],
)
async def test_an_openai_format_answer_that_was_not_filtered_is_unchanged(
    provider_cls, finish_reason: str, stop_reason: str
):
    chat = _oai_provider(provider_cls, AsyncMock(return_value=_oai_response(finish_reason, content="Done.")))
    response = await chat.chat(messages=_messages(), model=_MODEL)
    assert (response.content, response.stop_reason) == ("Done.", stop_reason)

    tools = _oai_provider(
        provider_cls, AsyncMock(return_value=_oai_stream(finish_reason, text=("Done.",)))
    )
    response = await tools.chat_stream_with_tools(messages=_messages(), tools=_TOOLS, model=_MODEL)
    assert (response.content, response.stop_reason) == ("Done.", stop_reason)

    stream = _oai_provider(
        provider_cls, AsyncMock(return_value=_oai_stream(finish_reason, text=("Done.",)))
    )
    assert await _stream_text(stream) == ["Done."]
    # ``chat_stream`` has no tool-use stop reason: it never emits tool calls.
    assert stream.last_stop_reason == ("max_tokens" if finish_reason == "length" else "end_turn")


async def test_a_stream_that_filters_after_some_text_delivers_it_then_declines(provider_cls):
    create = AsyncMock(
        return_value=_oai_stream("content_filter", text=("Sure, ", "the"), **_refusal())
    )
    provider = _oai_provider(provider_cls, create)
    sink = StreamUsageSink()
    delivered: list[str] = []

    with pytest.raises(ModelDeclinedError) as caught:
        async for chunk in provider.chat_stream(messages=_messages(), model=_MODEL, usage_sink=sink):
            delivered.append(chunk)

    assert delivered == ["Sure, ", "the"]
    assert caught.value.category == "cyber"
    assert sink.take()["prompt_tokens"] == 120


# ── One builder reads both providers' ``stop_details`` ─────────────────────


@pytest.mark.parametrize(
    "stop_details",
    [
        {"category": "cyber", "explanation": "No."},
        SimpleNamespace(type="refusal", category="cyber", explanation="No."),
    ],
    ids=["dict", "object"],
)
def test_the_decline_builder_reads_a_dict_or_an_object(stop_details):
    error = declined_error(_MODEL, stop_details, {"input_tokens": 3})

    assert (error.model, error.category, error.explanation) == (_MODEL, "cyber", "No.")
    assert error.usage == {"input_tokens": 3}


@pytest.mark.parametrize(
    "stop_details",
    [
        None,
        {},
        {"category": None, "explanation": None},
        {"category": "", "explanation": ""},
        {"category": 7, "explanation": ["x"]},
        SimpleNamespace(category=None, explanation=""),
    ],
)
def test_the_decline_builder_treats_missing_empty_or_non_text_fields_as_absent(stop_details):
    error = declined_error(_MODEL, stop_details, None)

    assert error.category is None
    assert error.explanation is None
    assert error.usage == {}
    assert error.failover_allowed is False


# ── A gateway decline is handed on, or not, like Anthropic's ───────────────


def _by_model(declines: set[str], **choice) -> AsyncMock:
    """A client that refuses on the models in *declines* and answers on the others."""

    def create(**kwargs):
        refused = kwargs["model"] in declines
        finish = "content_filter" if refused else "stop"
        extra = choice if refused else {}
        if kwargs.get("stream"):
            return _oai_stream(finish, text=() if refused else (f"answered by {kwargs['model']}",), **extra)
        return _oai_response(finish, content=None if refused else f"answered by {kwargs['model']}", **extra)

    return AsyncMock(side_effect=create)


async def _chat_via(wrapper: FailoverLLMProvider) -> str:
    return (await wrapper.chat(_messages(), model=_MODEL)).content


async def _tool_stream_via(wrapper: FailoverLLMProvider) -> str:
    return (await wrapper.chat_stream_with_tools(_messages(), tools=_TOOLS, model=_MODEL)).content


@pytest.mark.parametrize("call", [_chat_via, _tool_stream_via], ids=["chat", "tool_stream"])
async def test_a_cyber_decline_through_the_gateway_is_answered_by_the_declared_fallback(call):
    create = _by_model({_MODEL}, **_refusal())
    wrapper = FailoverLLMProvider(
        primary=_oai_provider(OpenAICompatibleProvider, create), fallbacks=[]
    )

    assert await call(wrapper) == "answered by claude-sonnet-5"
    assert [c.kwargs["model"] for c in create.await_args_list] == [_MODEL, "claude-sonnet-5"]


async def test_a_bio_decline_through_the_gateway_is_not_handed_to_another_model():
    stop_details = {"category": "bio", "explanation": "No."}
    create = _by_model({_MODEL}, stop_details=stop_details)
    wrapper = FailoverLLMProvider(
        primary=_oai_provider(OpenAICompatibleProvider, create), fallbacks=[]
    )

    with pytest.raises(ModelDeclinedError) as caught:
        await wrapper.chat(_messages(), model=_MODEL)

    create.assert_awaited_once()
    result = classify_run_failure(caught.value)
    assert result.cause is FailureCause.MODEL_DECLINED
    assert result.detail == "bio"


# ── A bare content filter is a decline with no category ────────────────────

_BARE = [
    {},
    {"stop_details": None},
    {"stop_details": {}},
    {"stop_details": {"category": None}},
    {"stop_details": {"category": ""}},
]


async def _chat_call(provider) -> None:
    await provider.chat(messages=_messages(), model=_MODEL)


async def _stream_call(provider) -> None:
    await _stream_text(provider)


async def _tool_stream_call(provider) -> None:
    await provider.chat_stream_with_tools(messages=_messages(), tools=_TOOLS, model=_MODEL)


@pytest.mark.parametrize("choice", _BARE, ids=["absent", "null", "empty", "no-category", "blank"])
@pytest.mark.parametrize(
    "call", [_chat_call, _stream_call, _tool_stream_call], ids=["chat", "stream", "tool_stream"]
)
async def test_a_bare_content_filter_declines_without_a_category(provider_cls, call, choice):
    create = _by_model({_MODEL}, **choice)
    provider = _oai_provider(provider_cls, create)

    with pytest.raises(ModelDeclinedError) as caught:
        await call(provider)

    assert caught.value.category is None
    assert caught.value.failover_allowed is False
    create.assert_awaited_once()


async def test_a_bare_content_filter_is_not_handed_to_another_model():
    create = _by_model({_MODEL})
    wrapper = FailoverLLMProvider(
        primary=_oai_provider(OpenAICompatibleProvider, create), fallbacks=[]
    )

    with pytest.raises(ModelDeclinedError) as caught:
        await wrapper.chat(_messages(), model=_MODEL)

    create.assert_awaited_once()
    assert classify_run_failure(caught.value).cause is FailureCause.MODEL_DECLINED


# ── A gateway decline through the agent loop and the proxy ─────────────────


async def test_a_streamed_gateway_decline_after_some_text_is_charged_once_and_adds_no_answer(
    provider_cls,
):
    """The text that streamed out stays delivered; the declined answer is not
    added to the conversation and what it consumed is charged to the run once."""
    create = AsyncMock(
        return_value=_oai_stream("content_filter", text=("Sure, ", "the"), **_refusal())
    )
    provider = _oai_provider(provider_cls, create)
    budget = RunBudget(max_total_tokens=1_000_000, price=_PRICE)
    conversation = _messages()
    streamed: list[str] = []

    async def on_token(token: str) -> None:
        streamed.append(token)

    with pytest.raises(ModelDeclinedError) as caught:
        await al.run_agent_loop(
            llm=provider,
            messages=conversation,
            model=_MODEL,
            max_iterations=2,
            max_tokens=1_024,
            progressive_tools=False,
            budget=budget,
            on_token=on_token,
        )

    assert caught.value.category == "cyber"
    assert streamed == ["Sure, ", "the"]
    assert [m.role for m in conversation] == ["user"]
    assert budget.spent_tokens == count_usage_tokens(_GATEWAY_USAGE) > 0
    create.assert_awaited_once()


def _gateway_wrapper() -> FailoverLLMProvider:
    """A gateway refusal in a category that is never handed on (``bio``)."""
    stop_details = {"category": "bio", "explanation": "No."}
    create = _by_model({_MODEL}, stop_details=stop_details)
    return FailoverLLMProvider(
        primary=_oai_provider(OpenAICompatibleProvider, create), fallbacks=[]
    )


async def test_the_proxy_answers_a_gateway_decline_as_content_filter(_proxy):
    response = await _proxy._proxy_via_provider_chat(
        _gateway_wrapper(), _proxy_body(stream=False), [{"role": "user", "content": "hi"}],
        _MODEL, 100, None, user_id="u1",
    )

    choice = response["choices"][0]
    assert choice["finish_reason"] == "content_filter"
    assert choice["message"]["content"] == ""
    assert response["usage"] == _GATEWAY_USAGE


async def test_the_proxy_stream_ends_a_gateway_decline_on_content_filter(_proxy):
    response = await _proxy._proxy_via_provider_chat(
        _gateway_wrapper(), _proxy_body(stream=True), [{"role": "user", "content": "hi"}],
        _MODEL, 100, None, user_id="u1",
    )

    frames = [frame async for frame in response.body_iterator]
    payloads = [
        json.loads(frame[len("data: ") :]) for frame in frames if frame.startswith("data: {")
    ]
    assert not any("error" in payload for payload in payloads)
    assert payloads[-1]["choices"][0]["finish_reason"] == "content_filter"
    assert frames[-1] == "data: [DONE]\n\n"


@pytest.mark.parametrize(
    "call", [_stream_call, _tool_stream_call], ids=["stream", "tool_stream"]
)
async def test_a_later_finish_chunk_does_not_hide_the_content_filter(provider_cls, call):
    """A server that repeats a finish reason after ``content_filter`` is still a decline."""
    create = AsyncMock(return_value=_oai_stream("content_filter", then="stop", **_refusal()))
    provider = _oai_provider(provider_cls, create)

    with pytest.raises(ModelDeclinedError) as caught:
        await call(provider)

    assert caught.value.category == "cyber"


@pytest.mark.parametrize("path", ["stream", "tool_stream"])
async def test_a_refusal_with_usage_on_the_finish_chunk_is_declined_and_charged(provider_cls, path):
    """A gateway sends ``usage`` on the same chunk as ``content_filter``."""
    chunk = ChatCompletionChunk.model_validate(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": _MODEL,
            "choices": [
                {
                    "index": 0,
                    "delta": {"refusal": "No."},
                    "finish_reason": "content_filter",
                    "stop_details": _GATEWAY_STOP_DETAILS,
                }
            ],
            "usage": _GATEWAY_USAGE,
        }
    )
    provider = _oai_provider(provider_cls, AsyncMock(return_value=_Chunks([chunk])))
    sink = StreamUsageSink()

    with pytest.raises(ModelDeclinedError) as caught:
        if path == "stream":
            await _stream_text(provider, usage_sink=sink)
        else:
            await provider.chat_stream_with_tools(
                messages=_messages(), tools=_TOOLS, model=_MODEL, usage_sink=sink
            )

    assert caught.value.category == "cyber"
    assert caught.value.usage["prompt_tokens"] == 120
    assert sink.take()["prompt_tokens"] == 120


async def test_a_streamed_decline_reports_its_own_usage_not_the_providers_last_one(provider_cls):
    """The router caches one provider per config: another request's stream may
    overwrite ``last_usage`` while this one is still open."""

    class _ThenOtherRequestFinishes(_Chunks):
        def __init__(self, items: list, provider) -> None:
            super().__init__(items)
            self._provider = provider

        async def _iterate(self):
            async for item in super()._iterate():
                yield item
            self._provider.last_usage = {"prompt_tokens": 999, "completion_tokens": 1}

    create = AsyncMock()
    provider = _oai_provider(provider_cls, create)
    create.return_value = _ThenOtherRequestFinishes(
        _oai_stream("content_filter", **_refusal())._items, provider
    )

    with pytest.raises(ModelDeclinedError) as caught:
        await _stream_text(provider)

    assert caught.value.usage["prompt_tokens"] == 120
