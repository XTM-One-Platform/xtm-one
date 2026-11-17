"""A mid-answer failover restarts the answer, never glues two together.

When the primary provider fails after part of its answer already streamed,
``FailoverLLMProvider`` may only hand the call to a fallback once the caller
took that partial answer back (``on_stream_reset``). The agent loop answers
the reset through the documented ``stream_retract`` path, as on the timeout
retry, so the client shows the fallback's answer alone. A caller that cannot
take text back keeps the partial answer and gets the error. A turn with no
failover streams exactly as it does through an unwrapped provider.
"""

from __future__ import annotations

import asyncio

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.llm.router import AllProvidersFailedError, FailoverLLMProvider
from app.tools.agent_loop import _PREAMBLE_HOLDBACK_CHARS, run_agent_loop
from app.tools.base import CallableToolExecutor, CompositeToolExecutor, ToolDefinition

PARTIAL = "The primary started this answer and then its connection dropped. " * 20
FINAL = "Here is the fallback's complete, data-backed answer to the question. " * 20


def _chunks(text: str) -> list[str]:
    return [text[i : i + 40] for i in range(0, len(text), 40)]


class _Stub(BaseLLMProvider):
    context_window = 100_000

    def __init__(self) -> None:
        self.kwargs_seen: list[dict] = []
        self.methods: list[str] = []

    def _seen(self, method: str, kwargs: dict) -> None:
        self.methods.append(method)
        self.kwargs_seen.append(kwargs)

    async def chat(self, messages, model=None, **kwargs):
        raise NotImplementedError

    async def chat_stream(self, messages, model=None, **kwargs):
        raise NotImplementedError
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


class _DropsMidAnswer(_Stub):
    """Streams part of an answer, then its connection drops."""

    provider_name = "primary"

    def __init__(self, error: Exception | None = None) -> None:
        super().__init__()
        self.error = error or RuntimeError("connection reset mid-stream")

    async def chat_stream_with_tools(self, messages, tools=None, *, on_token=None, model=None, **kwargs):
        self._seen("chat_stream_with_tools", kwargs)
        for chunk in _chunks(PARTIAL):
            await on_token(chunk)
        raise self.error

    async def chat_stream(self, messages, model=None, **kwargs):
        self._seen("chat_stream", kwargs)
        for chunk in _chunks(PARTIAL):
            yield chunk
        raise self.error

    async def chat(self, messages, model=None, **kwargs):
        raise self.error


class _Answers(_Stub):
    provider_name = "fallback"

    def __init__(self, text: str = FINAL) -> None:
        super().__init__()
        self.text = text

    async def chat_stream_with_tools(self, messages, tools=None, *, on_token=None, model=None, **kwargs):
        self._seen("chat_stream_with_tools", kwargs)
        for chunk in _chunks(self.text):
            await on_token(chunk)
        return ChatResponse(content=self.text, model="fallback-model")

    async def chat_stream(self, messages, model=None, **kwargs):
        self._seen("chat_stream", kwargs)
        for chunk in _chunks(self.text):
            yield chunk

    async def chat(self, messages, model=None, **kwargs):
        self._seen("chat", kwargs)
        return ChatResponse(content=self.text, model="fallback-model")


class _Client:
    """What a streaming client shows: tokens append, ``stream_retract`` clears."""

    def __init__(self) -> None:
        self.bubble = ""
        self.retracts = 0
        self.wire: list[tuple[str, str]] = []

    async def on_token(self, chunk: str) -> None:
        self.bubble += chunk
        self.wire.append(("token", chunk))

    async def on_event(self, event: dict) -> None:
        status = event.get("status")
        if status:
            self.wire.append(("status", status))
        if status == "stream_retract":
            self.bubble = ""
            self.retracts += 1


def _messages() -> list[ChatMessage]:
    return [ChatMessage(role="user", content="answer the question")]


# -- the failover wrapper ------------------------------------------------------


def test_a_stream_with_tools_is_restarted_on_the_fallback_after_a_reset():
    client = _Client()
    order: list[str] = []
    primary, fallback = _DropsMidAnswer(), _Answers()
    wrapper = FailoverLLMProvider(primary=primary, fallbacks=[fallback])

    async def _on_token(chunk: str) -> None:
        order.append("token")
        await client.on_token(chunk)

    async def _reset() -> None:
        order.append("reset")
        client.bubble = ""

    response = asyncio.run(
        wrapper.chat_stream_with_tools(_messages(), on_token=_on_token, on_stream_reset=_reset)
    )

    assert response.content == FINAL
    assert client.bubble == FINAL
    assert order.count("reset") == 1
    # The reset lands between the primary's last token and the fallback's first.
    assert order.index("reset") == len(_chunks(PARTIAL))
    # The callback is the wrapper's: no provider ever receives it.
    assert all("on_stream_reset" not in kw for kw in primary.kwargs_seen + fallback.kwargs_seen)


def test_without_a_reset_a_stream_with_tools_is_never_glued_to_a_second_answer():
    tokens: list[str] = []
    fallback = _Answers()
    wrapper = FailoverLLMProvider(primary=_DropsMidAnswer(), fallbacks=[fallback])

    async def _on_token(chunk: str) -> None:
        tokens.append(chunk)

    with pytest.raises(RuntimeError, match="connection reset"):
        asyncio.run(wrapper.chat_stream_with_tools(_messages(), on_token=_on_token))
    assert "".join(tokens) == PARTIAL
    assert fallback.kwargs_seen == [], "the fallback must not answer after the partial output"


def test_a_stream_is_restarted_on_the_fallback_after_a_reset():
    resets: list[int] = []
    chunks: list[str] = []
    wrapper = FailoverLLMProvider(primary=_DropsMidAnswer(), fallbacks=[_Answers()])

    async def _reset() -> None:
        resets.append(len(chunks))
        chunks.clear()

    async def _consume():
        async for chunk in wrapper.chat_stream(_messages(), on_stream_reset=_reset):
            chunks.append(chunk)

    asyncio.run(_consume())
    assert "".join(chunks) == FINAL
    assert resets == [len(_chunks(PARTIAL))]


def test_without_a_reset_a_stream_still_refuses_to_fail_over_after_output():
    chunks: list[str] = []
    fallback = _Answers()
    wrapper = FailoverLLMProvider(primary=_DropsMidAnswer(), fallbacks=[fallback])

    async def _consume():
        async for chunk in wrapper.chat_stream(_messages()):
            chunks.append(chunk)

    with pytest.raises(RuntimeError, match="connection reset"):
        asyncio.run(_consume())
    assert "".join(chunks) == PARTIAL
    assert fallback.kwargs_seen == []


@pytest.mark.parametrize("method", ["chat_stream_with_tools", "chat_stream"])
def test_a_reset_that_cannot_take_the_output_back_refuses_the_failover(method):
    """Fail closed: a reset that raised took nothing back, so the partial
    answer stays alone and the primary's error surfaces."""
    tokens: list[str] = []
    fallback = _Answers()
    wrapper = FailoverLLMProvider(primary=_DropsMidAnswer(), fallbacks=[fallback])

    async def _reset() -> None:
        raise RuntimeError("the retract could not be delivered")

    async def _on_token(chunk: str) -> None:
        tokens.append(chunk)

    async def _call():
        if method == "chat_stream":
            async for chunk in wrapper.chat_stream(_messages(), on_stream_reset=_reset):
                await _on_token(chunk)
        else:
            await wrapper.chat_stream_with_tools(_messages(), on_token=_on_token, on_stream_reset=_reset)

    with pytest.raises(RuntimeError, match="connection reset"):
        asyncio.run(_call())
    assert "".join(tokens) == PARTIAL
    assert fallback.kwargs_seen == []


def test_no_reset_when_no_provider_is_left_to_try():
    """Taking the partial answer back would only leave the user with nothing."""
    resets: list[str] = []

    async def _reset() -> None:
        resets.append("reset")

    async def _on_token(chunk: str) -> None:
        return None

    wrapper = FailoverLLMProvider(primary=_DropsMidAnswer(), fallbacks=[])
    with pytest.raises(RuntimeError):
        asyncio.run(wrapper.chat_stream_with_tools(_messages(), on_token=_on_token, on_stream_reset=_reset))
    assert resets == []


def test_no_reset_for_an_error_every_provider_would_repeat():
    resets: list[str] = []

    async def _reset() -> None:
        resets.append("reset")

    async def _on_token(chunk: str) -> None:
        return None

    wrapper = FailoverLLMProvider(
        primary=_DropsMidAnswer(RuntimeError("prompt is too long")), fallbacks=[_Answers()]
    )
    with pytest.raises(RuntimeError, match="prompt is too long"):
        asyncio.run(wrapper.chat_stream_with_tools(_messages(), on_token=_on_token, on_stream_reset=_reset))
    assert resets == []


def test_a_fallback_dropping_mid_answer_is_reset_again_for_the_next_one():
    client = _Client()
    resets: list[str] = []
    second_fallback = _Answers()

    async def _reset() -> None:
        resets.append("reset")
        client.bubble = ""

    class _FallbackDrops(_DropsMidAnswer):
        provider_name = "fallback-1"

    wrapper = FailoverLLMProvider(primary=_DropsMidAnswer(), fallbacks=[_FallbackDrops(), second_fallback])
    response = asyncio.run(
        wrapper.chat_stream_with_tools(_messages(), on_token=client.on_token, on_stream_reset=_reset)
    )
    assert resets == ["reset", "reset"]
    assert client.bubble == FINAL == response.content


def test_the_last_fallback_dropping_mid_answer_keeps_its_output():
    resets: list[str] = []

    async def _reset() -> None:
        resets.append("reset")

    async def _on_token(chunk: str) -> None:
        return None

    class _FallbackDrops(_DropsMidAnswer):
        provider_name = "fallback-1"

    wrapper = FailoverLLMProvider(primary=_DropsMidAnswer(), fallbacks=[_FallbackDrops()])
    with pytest.raises(AllProvidersFailedError):
        asyncio.run(wrapper.chat_stream_with_tools(_messages(), on_token=_on_token, on_stream_reset=_reset))
    # The primary's output was taken back for the fallback; the fallback's own
    # output stays, since nothing else could replace it.
    assert resets == ["reset"]


def test_a_failure_before_any_output_needs_no_reset():
    resets: list[str] = []

    class _DownBeforeOutput(_Stub):
        provider_name = "primary"

        async def chat_stream_with_tools(self, messages, tools=None, *, on_token=None, **kwargs):
            raise RuntimeError("connection refused")

    async def _reset() -> None:
        resets.append("reset")

    client = _Client()
    response = asyncio.run(
        FailoverLLMProvider(primary=_DownBeforeOutput(), fallbacks=[_Answers()]).chat_stream_with_tools(
            _messages(), on_token=client.on_token, on_stream_reset=_reset
        )
    )
    assert resets == []
    assert client.bubble == FINAL == response.content


# -- through the agent loop, as a client sees it -------------------------------


def _tool_executor() -> tuple[list[ToolDefinition], CompositeToolExecutor]:
    async def _echo() -> str:
        return "echo"

    callable_exec = CallableToolExecutor()
    callable_exec.register("echo_tool", _echo)
    definitions = [ToolDefinition(name="echo_tool", description="Echo back")]
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)
    return definitions, composite


async def _run_loop(llm: BaseLLMProvider, *, with_tools: bool) -> tuple[ChatResponse, _Client]:
    client = _Client()
    definitions, composite = _tool_executor() if with_tools else ([], CompositeToolExecutor())
    response = await run_agent_loop(
        llm=llm,
        messages=_messages(),
        tools=definitions,
        executor=composite,
        max_iterations=4,
        progressive_tools=False,
        on_event=client.on_event,
        on_token=client.on_token,
    )
    return response, client


@pytest.mark.asyncio
@pytest.mark.parametrize("with_tools", [True, False], ids=["stream_with_tools", "stream"])
async def test_the_client_shows_the_fallback_answer_alone(with_tools):
    """Before the fix the bubble read PARTIAL + FINAL (or ended on an error)."""
    assert len(PARTIAL) > _PREAMBLE_HOLDBACK_CHARS, "the partial answer must have reached the bubble"
    primary, fallback = _DropsMidAnswer(), _Answers()
    llm = FailoverLLMProvider(primary=primary, fallbacks=[fallback])

    response, client = await _run_loop(llm, with_tools=with_tools)

    assert client.bubble == FINAL
    assert response.content == FINAL
    assert client.retracts == 1
    # Each case runs the streaming path of the loop it is named after.
    method = "chat_stream_with_tools" if with_tools else "chat_stream"
    assert primary.methods == [method]
    assert fallback.methods == [method]


@pytest.mark.asyncio
@pytest.mark.parametrize("with_tools", [True, False], ids=["stream_with_tools", "stream"])
async def test_a_turn_with_no_failover_streams_exactly_as_through_the_bare_provider(with_tools):
    _bare_response, bare = await _run_loop(_Answers(), with_tools=with_tools)
    _wrapped_response, wrapped = await _run_loop(
        FailoverLLMProvider(primary=_Answers(), fallbacks=[_Answers("never streamed")]),
        with_tools=with_tools,
    )

    assert wrapped.wire == bare.wire
    assert "stream_retract" not in [payload for kind, payload in wrapped.wire if kind == "status"]
    assert wrapped.bubble == FINAL


@pytest.mark.asyncio
@pytest.mark.parametrize("with_tools", [True, False], ids=["stream_with_tools", "stream"])
async def test_without_an_event_channel_the_loop_never_glues_a_second_answer(with_tools):
    """A caller streaming tokens with no ``on_event`` cannot receive the
    retract, so the fallback must not answer after the partial output."""
    client = _Client()
    primary, fallback = _DropsMidAnswer(), _Answers()
    definitions, composite = _tool_executor() if with_tools else ([], CompositeToolExecutor())

    with pytest.raises(RuntimeError, match="connection reset"):
        await run_agent_loop(
            llm=FailoverLLMProvider(primary=primary, fallbacks=[fallback]),
            messages=_messages(),
            tools=definitions,
            executor=composite,
            max_iterations=4,
            progressive_tools=False,
            on_token=client.on_token,
        )
    assert client.bubble == PARTIAL
    assert fallback.methods == []


@pytest.mark.asyncio
async def test_held_back_text_is_dropped_without_an_event_channel():
    """Text still inside the hold-back window never reached the caller, so
    nothing needs taking back: the fallback answers with no ``on_event``."""
    preamble = "Let me check that for you."
    assert len(preamble) < _PREAMBLE_HOLDBACK_CHARS

    class _DropsDuringPreamble(_DropsMidAnswer):
        async def chat_stream_with_tools(self, messages, tools=None, *, on_token=None, model=None, **kwargs):
            self._seen("chat_stream_with_tools", kwargs)
            await on_token(preamble)
            raise self.error

    client = _Client()
    definitions, composite = _tool_executor()
    response = await run_agent_loop(
        llm=FailoverLLMProvider(primary=_DropsDuringPreamble(), fallbacks=[_Answers()]),
        messages=_messages(),
        tools=definitions,
        executor=composite,
        max_iterations=4,
        progressive_tools=False,
        on_token=client.on_token,
    )
    assert client.bubble == FINAL
    assert response.content == FINAL
