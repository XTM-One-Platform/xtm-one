"""A text stream a failover fallback answers is recorded under its model.

A text stream carries no metadata, so the failover wrapper forwards what the
provider that served it reported (usage, stop reason) on its ``last_*``
companions and the agent loop builds the call's response from them. The model
was not among them: a call a fallback answered came back, and was traced,
under the primary's model. ``chat()`` and ``chat_stream_with_tools()`` return
the fallback's own response, which names the model the way the API does (a
non-streamed OpenAI or Anthropic call, its dated name): the wrapper names the
model it called the fallback with for them too, the one the agent loop counts
the call toward among the models serving the run.
"""

from __future__ import annotations

import asyncio

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse, StreamUsageSink, usage_sink_of
from app.llm.router import AllProvidersFailedError, FailoverLLMProvider
from app.tools import agent_loop as al
from app.tools.base import CompositeToolExecutor

_USAGE = {"prompt_tokens": 40, "completion_tokens": 8, "total_tokens": 48}


class _Stub(BaseLLMProvider):
    context_window = 100_000

    async def chat(self, messages, model=None, **kwargs):
        raise NotImplementedError

    async def chat_stream(self, messages, model=None, **kwargs):
        raise NotImplementedError
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


class _Down(_Stub):
    """Fails before any output, as many times as asked."""

    provider_name = "primary"

    def __init__(self, failures: int = 1_000) -> None:
        self.failures = failures
        self.default_model = "primary-default"

    async def chat_stream(self, messages, model=None, **kwargs):
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("connection refused")
        self.last_usage = dict(_USAGE)
        self.last_stop_reason = "end_turn"
        yield "the primary's answer"


class _Streams(_Stub):
    provider_name = "fallback"

    def __init__(self, default_model: str | None = "fallback-default") -> None:
        self.default_model = default_model
        self.models: list[str | None] = []

    async def chat_stream(self, messages, model=None, **kwargs):
        self.models.append(model)
        self.last_usage = dict(_USAGE)
        self.last_stop_reason = "end_turn"
        yield "the fallback's answer"


class _StartsThenDrops(_Stub):
    """A fallback that reports usage, streams part of its answer, then drops."""

    provider_name = "fallback"

    def __init__(self) -> None:
        self.default_model = "fallback-default"

    def _report(self, kwargs: dict) -> None:
        sink = usage_sink_of(kwargs)
        if sink is not None:
            sink.restart()
            sink.report(dict(_USAGE))

    async def chat_stream(self, messages, model=None, **kwargs):
        self._report(kwargs)
        yield "the fallback started"
        raise RuntimeError("connection reset mid-stream")

    async def chat_stream_with_tools(self, messages, tools=None, *, on_token=None, model=None, **kwargs):
        self._report(kwargs)
        await on_token("the fallback started")
        raise RuntimeError("connection reset mid-stream")


class _DownWithTools(_Down):
    async def chat_stream_with_tools(self, messages, tools=None, *, on_token=None, model=None, **kwargs):
        raise RuntimeError("connection refused")


class _DownChats(_Down):
    """``_Down`` for ``chat()`` and ``chat_stream_with_tools()`` too."""

    async def _answer(self) -> ChatResponse:
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("connection refused")
        return ChatResponse(content="the primary's answer", model="primary-default-2026-01-01", usage=dict(_USAGE))

    async def chat(self, messages, model=None, **kwargs):
        return await self._answer()

    async def chat_stream_with_tools(self, messages, tools=None, *, on_token=None, model=None, **kwargs):
        return await self._answer()


class _Answers(_Stub):
    """A fallback whose responses name its model as the API dates it."""

    provider_name = "fallback"

    def __init__(self) -> None:
        self.default_model = "fallback-default"

    async def chat(self, messages, model=None, **kwargs):
        return ChatResponse(content="the fallback's answer", model="fallback-default-2026-01-01", usage=dict(_USAGE))

    async def chat_stream_with_tools(self, messages, tools=None, *, on_token=None, model=None, **kwargs):
        return await self.chat(messages, model=model)


def _messages() -> list[ChatMessage]:
    return [ChatMessage(role="user", content="answer the question")]


def _drain(llm: BaseLLMProvider, model: str) -> str:
    async def _run() -> str:
        return "".join([chunk async for chunk in llm.chat_stream(_messages(), model=model)])

    return asyncio.run(_run())


def test_a_fallback_serving_a_text_stream_names_the_model_it_was_called_with():
    fallback = _Streams()
    wrapper = FailoverLLMProvider(primary=_Down(), fallbacks=[fallback])

    assert _drain(wrapper, "primary-model") == "the fallback's answer"

    # The fallback ran on its own default model, and the wrapper says so.
    assert fallback.models == [None]
    assert wrapper.last_model == "fallback-default"
    assert wrapper.last_usage == _USAGE


def test_the_primary_serving_a_text_stream_names_no_model():
    wrapper = FailoverLLMProvider(primary=_Down(failures=0), fallbacks=[_Streams()])

    assert _drain(wrapper, "primary-model") == "the primary's answer"
    assert wrapper.last_model is None


def test_a_fallback_model_does_not_outlive_its_stream():
    wrapper = FailoverLLMProvider(primary=_Down(failures=1), fallbacks=[_Streams()])

    _drain(wrapper, "primary-model")
    assert wrapper.last_model == "fallback-default"
    _drain(wrapper, "primary-model")
    assert wrapper.last_model is None


def test_a_fallback_without_a_known_model_names_none():
    wrapper = FailoverLLMProvider(primary=_Down(), fallbacks=[_Streams(default_model=None)])

    _drain(wrapper, "primary-model")
    assert wrapper.last_model is None


@pytest.mark.parametrize("method", ["chat", "chat_stream_with_tools"])
async def test_a_fallback_serving_a_call_names_the_model_it_was_called_with(method):
    wrapper = FailoverLLMProvider(primary=_DownChats(), fallbacks=[_Answers()])

    response = await getattr(wrapper, method)(_messages(), model="primary-model")

    # The response names the model as the API dates it; the wrapper names the
    # one the fallback was called with, as for a text stream.
    assert response.model == "fallback-default-2026-01-01"
    assert wrapper.last_model == "fallback-default"


@pytest.mark.parametrize("method", ["chat", "chat_stream_with_tools"])
async def test_a_fallback_model_does_not_outlive_its_call(method):
    wrapper = FailoverLLMProvider(primary=_DownChats(failures=1), fallbacks=[_Answers()])

    await getattr(wrapper, method)(_messages(), model="primary-model")
    assert wrapper.last_model == "fallback-default"
    response = await getattr(wrapper, method)(_messages(), model="primary-model")
    assert response.model == "primary-default-2026-01-01"
    assert wrapper.last_model is None


async def test_a_text_stream_fallback_model_does_not_outlive_it_into_the_next_call():
    wrapper = FailoverLLMProvider(primary=_DownChats(failures=1), fallbacks=[_Streams()])

    assert "".join([chunk async for chunk in wrapper.chat_stream(_messages(), model="primary-model")]) == (
        "the fallback's answer"
    )
    assert wrapper.last_model == "fallback-default"
    await wrapper.chat(_messages(), model="primary-model")
    assert wrapper.last_model is None


async def test_the_streamed_response_carries_the_model_that_served_it():
    wrapper = FailoverLLMProvider(primary=_Down(), fallbacks=[_Streams()])

    response = await al._streaming_chat(wrapper, _messages(), None, model="primary-model", max_tokens=256)

    # Before: "primary-model", with the fallback's usage.
    assert response.model == "fallback-default"
    assert response.usage == _USAGE


@pytest.mark.parametrize("last_model", [None, "", 42], ids=["unset", "empty", "not-a-name"])
async def test_any_other_stream_keeps_the_model_it_asked_for(last_model):
    provider = _Streams()
    provider.last_model = last_model

    response = await al._streaming_chat(provider, _messages(), None, model="asked-model", max_tokens=256)

    assert response.model == "asked-model"


def test_the_sink_keeps_the_served_model_of_the_attempt_in_flight_only():
    sink = StreamUsageSink()
    assert sink.served_model is None
    sink.note_served_model("fallback-default")
    assert sink.served_model == "fallback-default"
    # A new attempt drops the abandoned one's model with its usage.
    sink.restart()
    assert sink.served_model is None
    sink.settle()
    sink.note_served_model("late")
    assert sink.served_model is None


def test_a_fallback_cut_mid_text_stream_notes_its_model_on_the_call_sink():
    sink = StreamUsageSink()
    wrapper = FailoverLLMProvider(primary=_Down(), fallbacks=[_StartsThenDrops()])

    async def _run() -> None:
        async for _chunk in wrapper.chat_stream(_messages(), model="primary-model", usage_sink=sink):
            pass

    with pytest.raises(AllProvidersFailedError):
        asyncio.run(_run())

    assert sink.served_model == "fallback-default"
    assert wrapper.last_model == "fallback-default"
    assert sink.take() == _USAGE


def test_a_fallback_cut_mid_stream_with_tools_notes_its_model_on_the_call_sink():
    sink = StreamUsageSink()
    tokens: list[str] = []

    async def _on_token(chunk: str) -> None:
        tokens.append(chunk)

    wrapper = FailoverLLMProvider(primary=_DownWithTools(), fallbacks=[_StartsThenDrops()])
    with pytest.raises(AllProvidersFailedError):
        asyncio.run(wrapper.chat_stream_with_tools(_messages(), on_token=_on_token, usage_sink=sink))

    assert tokens == ["the fallback started"]
    assert sink.served_model == "fallback-default"


async def test_a_fallback_text_stream_cut_mid_answer_is_closed_under_the_fallback(monkeypatch):
    closed: list[dict] = []
    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: {"generation": True})
    monkeypatch.setattr(al, "_lf_end_generation", lambda _ctx, **kw: closed.append(kw))
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)

    async def _on_token(chunk: str) -> None:
        return None

    with pytest.raises(AllProvidersFailedError):
        await al.run_agent_loop(
            llm=FailoverLLMProvider(primary=_Down(), fallbacks=[_StartsThenDrops()]),
            messages=_messages(),
            tools=[],
            executor=CompositeToolExecutor(),
            max_iterations=2,
            model="primary-model",
            on_token=_on_token,
        )

    aborted = [kw for kw in closed if kw.get("error") is not None]
    # Before: closed with the fallback's usage under the primary's model.
    assert [(kw.get("model"), kw.get("usage")) for kw in aborted] == [("fallback-default", _USAGE)]


async def test_a_run_answered_by_a_fallback_text_stream_is_traced_under_the_fallback(monkeypatch):
    closed: list[dict] = []
    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: {"generation": True})
    monkeypatch.setattr(al, "_lf_end_generation", lambda _ctx, **kw: closed.append(kw))
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    tokens: list[str] = []

    async def _on_token(chunk: str) -> None:
        tokens.append(chunk)

    response = await al.run_agent_loop(
        llm=FailoverLLMProvider(primary=_Down(), fallbacks=[_Streams()]),
        messages=_messages(),
        tools=[],
        executor=CompositeToolExecutor(),
        max_iterations=2,
        model="primary-model",
        on_token=_on_token,
    )

    assert "".join(tokens) == "the fallback's answer"
    assert response.model == "fallback-default"
    assert [kw.get("model") for kw in closed if "usage" in kw] == ["fallback-default"]
