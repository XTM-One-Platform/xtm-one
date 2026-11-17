"""``/v1/chat/completions``: Mistral list content and the effort matrix.

With ``reasoning_effort`` on, Mistral Small 4 / Medium 3.5 send ``content`` as
a list of ``thinking`` / ``text`` chunks instead of a string. The
OpenAI-compatible pass-through added that list to a string: the stream raised
after its chunks were forwarded (a trailing ``server_error`` frame, a trace
closed as an error, no metering), and the non-streaming answer handed the list
to the trace as its output. The client still gets the upstream chunks
unchanged; the trace and the metering read the answer text.

The proxy also forwarded the client's ``reasoning_effort`` as sent, so a level
the model refuses was an HTTP 400. It now goes through the per-model effort
matrix the providers apply (``model_capabilities.MODEL_PARAMETER_RULES``), on
the OpenAI-compatible pass-through and on the Anthropic bridge alike.
"""

from __future__ import annotations

import asyncio
import uuid
import warnings
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from app.api.v1 import llm_proxy
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.mistral_provider import MistralProvider
from app.services import langfuse_tracing as lft

MISTRAL_MODEL = "mistral-small-2603"
_USER_ID = str(uuid.uuid4())
_USAGE = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}


def _thinking(text: str) -> dict:
    return {"type": "thinking", "thinking": [{"type": "text", "text": text}]}


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


# The SDK builds its response models without validation, so a list reaches
# ``content`` as Mistral sent it: ``construct`` does the same here.
def _chunk(content: object, finish_reason: str | None = None) -> ChatCompletionChunk:
    return ChatCompletionChunk.construct(
        id="chatcmpl-test",
        object="chat.completion.chunk",
        created=0,
        model=MISTRAL_MODEL,
        choices=[
            {
                "index": 0,
                "delta": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
    )


def _usage_chunk() -> ChatCompletionChunk:
    return ChatCompletionChunk.construct(
        id="chatcmpl-test",
        object="chat.completion.chunk",
        created=0,
        model=MISTRAL_MODEL,
        choices=[],
        usage=_USAGE,
    )


def _completion(content: object) -> ChatCompletion:
    return ChatCompletion.construct(
        id="chatcmpl-test",
        object="chat.completion",
        created=0,
        model=MISTRAL_MODEL,
        choices=[
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        usage=_USAGE,
    )


class _Stream:
    def __init__(self, items: list) -> None:
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._items:
            return self._items.pop(0)
        raise StopAsyncIteration


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


@pytest.fixture
def traces():
    """Every trace the real ``end_trace`` hands to the durable sink."""
    submitted: list = []
    with patch(
        "app.services.tracing.dispatcher.submit_trace_for_flush",
        side_effect=lambda trace, _spans: submitted.append(trace),
    ):
        yield submitted


@pytest.fixture
def metered(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(llm_proxy, "_track_model_usage", mock)
    return mock


async def _proxy(
    create: AsyncMock,
    model: str,
    *,
    stream: bool,
    reasoning_effort: str | None = None,
    tools: list[dict] | None = None,
):
    """``proxy_chat_completions`` on an OpenAI-compatible provider serving *model*."""
    provider = MistralProvider(api_key="test-key")
    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    user = SimpleNamespace(id=uuid.UUID(_USER_ID), email="user@example.com")
    body = llm_proxy.ChatCompletionRequest(
        model="auto",
        messages=[{"role": "user", "content": "hi"}],
        stream=stream,
        reasoning_effort=reasoning_effort,
        tools=tools,
    )
    with (
        patch.object(llm_proxy, "_authenticate_api_key", AsyncMock(return_value=user)),
        patch.object(llm_proxy, "_refuse_unless_chat_enabled", AsyncMock()),
        patch("app.services.quota.check_llm_proxy_quota", AsyncMock()),
        patch.object(llm_proxy, "get_llm_for_user", AsyncMock(return_value=(provider, model))),
        patch.object(llm_proxy, "trace_llm_call", AsyncMock(return_value=_root_trace_ctx())),
        patch.object(llm_proxy, "release_db_connection_before_loop", AsyncMock()),
    ):
        return await llm_proxy.proxy_chat_completions(body, SimpleNamespace(), SimpleNamespace())


async def _settle_metering() -> None:
    pending = list(getattr(llm_proxy, "_STREAM_METERING", ()))
    if pending:
        await asyncio.gather(*pending)


# ---------------------------------------------------------------------------
# Mistral list content
# ---------------------------------------------------------------------------


async def test_stream_of_mistral_chunk_lists_traces_the_answer_and_meters(traces, metered):
    items = [
        _chunk([_thinking("Weighing ")]),
        _chunk([_thinking("it.")]),
        _chunk([_text("The ")]),
        _chunk("answer."),
        _chunk(None, "stop"),
        _usage_chunk(),
    ]
    response = await _proxy(AsyncMock(return_value=_Stream(items)), MISTRAL_MODEL, stream=True)

    # No serializer warning quoting the model's output on the way out.
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        body = [chunk async for chunk in response.body_iterator]
    await _settle_metering()

    # Forwarded as the upstream sent it, reasoning chunks included, and no
    # trailing error frame.
    assert body == [f"data: {c.model_dump_json(warnings=False)}\n\n" for c in items] + ["data: [DONE]\n\n"]
    assert '"thinking":[{"type":"text","text":"Weighing "}]' in body[0]
    assert len(traces) == 1
    assert traces[0].status == "ok"
    assert traces[0].error_message is None
    assert traces[0].output == "The answer."
    assert traces[0].usage_details == lft._normalize_usage(_USAGE)
    metered.assert_awaited_once_with(MISTRAL_MODEL, user_id=_USER_ID)


async def test_non_stream_mistral_chunk_list_traces_the_answer_and_meters(traces, metered):
    content = [_thinking("Weighing it."), _text("The answer.")]
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        response = await _proxy(AsyncMock(return_value=_completion(content)), MISTRAL_MODEL, stream=False)

    # The client gets the response as the upstream sent it.
    assert response["choices"][0]["message"]["content"] == content
    assert len(traces) == 1
    assert traces[0].status == "ok"
    assert traces[0].output == "The answer."
    metered.assert_awaited_once_with(MISTRAL_MODEL, user_id=_USER_ID)


async def test_non_stream_string_content_is_unchanged(traces, metered):
    response = await _proxy(AsyncMock(return_value=_completion("Plain.")), MISTRAL_MODEL, stream=False)

    assert response["choices"][0]["message"]["content"] == "Plain."
    assert len(traces) == 1
    assert traces[0].output == "Plain."
    metered.assert_awaited_once_with(MISTRAL_MODEL, user_id=_USER_ID)


# ---------------------------------------------------------------------------
# reasoning_effort through the per-model matrix
# ---------------------------------------------------------------------------

_MISSING = object()
_TOOL = {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}


@pytest.mark.parametrize(
    ("model", "requested", "sent"),
    [
        # Mistral Small 4: reasoning on is ``high``; below it, no effort.
        (MISTRAL_MODEL, "high", "high"),
        (MISTRAL_MODEL, "xhigh", "high"),
        (MISTRAL_MODEL, "low", _MISSING),
        # A model that takes no effort at all gets none.
        ("mistral-large-latest", "high", _MISSING),
        ("gpt-4o", "medium", _MISSING),
        # ``max`` is ``xhigh`` outside the families that take it natively.
        ("gpt-5.5", "max", "xhigh"),
        ("gpt-5", "xhigh", "high"),
        # A value outside the platform's levels passes through.
        ("gpt-5", "minimal", "minimal"),
        # No effort asked, none sent.
        (MISTRAL_MODEL, None, _MISSING),
    ],
)
async def test_reasoning_effort_follows_the_model_matrix(traces, metered, model, requested, sent):
    create = AsyncMock(return_value=_completion("ok"))

    await _proxy(create, model, stream=False, reasoning_effort=requested)

    create.assert_awaited_once()
    assert create.await_args.kwargs.get("reasoning_effort", _MISSING) == sent


@pytest.mark.parametrize(
    ("effort", "tools", "sent"),
    [("high", None, "high"), ("high", [_TOOL], "none"), (None, None, _MISSING), (None, [_TOOL], "none")],
)
async def test_gpt6_effort_is_none_next_to_tools(traces, metered, effort, tools, sent):
    """gpt-6 refuses any effort but ``none`` next to tools, its default included (#11571)."""
    create = AsyncMock(return_value=_completion("ok"))

    await _proxy(create, "gpt-6", stream=False, reasoning_effort=effort, tools=tools)

    assert create.await_args.kwargs.get("reasoning_effort", _MISSING) == sent


async def test_streamed_request_sends_the_normalized_effort(traces, metered):
    create = AsyncMock(return_value=_Stream([_chunk("ok", "stop"), _usage_chunk()]))

    response = await _proxy(create, MISTRAL_MODEL, stream=True, reasoning_effort="xhigh")
    [chunk async for chunk in response.body_iterator]
    await _settle_metering()

    create.assert_awaited_once()
    assert create.await_args.kwargs["reasoning_effort"] == "high"
    assert create.await_args.kwargs["stream"] is True


class _SentError(Exception):
    """Stops the Anthropic bridge once its request is built."""


@pytest.mark.parametrize(
    ("model", "requested", "sent"),
    [
        ("claude-sonnet-4-6", "xhigh", {"effort": "high"}),
        ("claude-sonnet-4-6", "max", {"effort": "max"}),
        ("claude-opus-4-5", "max", {"effort": "high"}),
        # Sonnet 4.5 takes no effort: none is sent.
        ("claude-sonnet-4-5", "high", _MISSING),
        ("claude-sonnet-4-6", None, _MISSING),
    ],
)
async def test_anthropic_bridge_effort_follows_the_model_matrix(model, requested, sent):
    create = AsyncMock(side_effect=_SentError())
    provider = AnthropicProvider(api_key="test-key")
    provider.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    body = llm_proxy.ChatCompletionRequest(
        model=model,
        messages=[{"role": "user", "content": "hi"}],
        reasoning_effort=requested,
    )

    with pytest.raises(HTTPException) as exc_info:
        await llm_proxy._proxy_via_anthropic(
            provider,
            body,
            [{"role": "user", "content": "hi"}],
            model,
            1_024,
            _root_trace_ctx(),
            user_id=_USER_ID,
        )

    assert exc_info.value.status_code == 502
    create.assert_awaited_once()
    assert create.await_args.kwargs.get("output_config", _MISSING) == sent
