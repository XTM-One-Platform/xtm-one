"""Hold-back window behaviour for stream-with-tools text.

While a tools-enabled LLM call is in flight the agent loop cannot know
whether the text being produced is a short pre-tool-call preamble
("Let me check X…") or the final answer.  The contract locked here:

* Short preamble followed by tool calls — the preamble must NEVER reach
  ``on_token`` (it would render as a full-size answer bubble that later
  vanishes); it must surface as a ``thinking_text`` event when the call
  resolves, so it lands in the reasoning window.
* Long final answer (no tool calls) — tokens flush to ``on_token`` once
  the hold-back window (``_PREAMBLE_HOLDBACK_CHARS``) overflows, preceded
  by a ``status: "streaming"`` event, so real answers stream live.
* Long text followed by tool calls — WITHOUT ``deliver_segments`` (the
  default exercised here: assignments, REST, public embed, channels) the
  loop must emit ``status: "stream_retract"`` (frontends discard the
  provisional answer bubble) and re-surface the full text as
  ``thinking_text``.  With the opt-in (WS chat) the text is delivered as a
  completed segment instead — see ``test_segment_delivery.py``.
* Short final answer (under the window, no tool calls) — replayed through
  ``on_token`` at loop end so the user still gets a streamed answer.
"""

from __future__ import annotations

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools.agent_loop import _PREAMBLE_HOLDBACK_CHARS, run_agent_loop
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
)

SHORT_PREAMBLE = "Let me check the ARR cards before answering."
LONG_TEXT = "The detailed analysis paragraph repeats. " * 30  # ~1260 chars
FINAL_ANSWER = (
    "Here is the final, fully data-backed analysis of the ARR question. "
) * 20  # ~1360 chars — well past the hold-back window
SHORT_ANSWER = "Quick answer: yes."


class _ScriptedStreamLLM(BaseLLMProvider):
    """Stub provider whose first stream-with-tools call emits scripted text
    (optionally ending in a tool call) and whose second call streams the
    final answer."""

    provider_name = "stub"
    context_window = 100_000

    def __init__(
        self,
        *,
        first_text: str,
        first_has_tools: bool,
        answer: str,
    ) -> None:
        self.first_text = first_text
        self.first_has_tools = first_has_tools
        self.answer = answer
        self.calls = 0

    async def _stream_text(self, text: str, on_token) -> None:
        if on_token is None:
            return
        step = 40
        for i in range(0, len(text), step):
            await on_token(text[i : i + step])

    # Signature mirrors ``BaseLLMProvider.chat_stream_with_tools`` (optional
    # ``tools``, keyword-only ``on_token``) so the stub stays substitutable
    # for the base class contract.
    async def chat_stream_with_tools(
        self, messages, tools=None, *, on_token=None, model=None, **kwargs
    ):
        self.calls += 1
        if self.calls == 1:
            await self._stream_text(self.first_text, on_token)
            if self.first_has_tools:
                return ChatResponse(
                    content=self.first_text,
                    model=model or "stub",
                    tool_calls=[
                        ToolCall(id="tc-1", name="echo_tool", arguments={"i": 1})
                    ],
                    stop_reason="tool_use",
                )
            return ChatResponse(content=self.first_text, model=model or "stub")
        await self._stream_text(self.answer, on_token)
        return ChatResponse(content=self.answer, model=model or "stub")

    async def chat(self, messages, model=None, **kwargs):
        return ChatResponse(content=self.answer, model=model or "stub")

    async def chat_stream(self, messages, **kwargs):
        yield self.answer

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


def _build_executor() -> tuple[list[ToolDefinition], CompositeToolExecutor]:
    async def _echo(i: int = 0) -> str:
        return f"echo-result-{i}"

    callable_exec = CallableToolExecutor()
    callable_exec.register("echo_tool", _echo)
    definitions = [ToolDefinition(name="echo_tool", description="Echo back")]
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)
    return definitions, composite


async def _run(llm: _ScriptedStreamLLM) -> tuple[ChatResponse, list[dict], str]:
    definitions, composite = _build_executor()
    events: list[dict] = []
    tokens: list[str] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    async def _on_token(chunk: str) -> None:
        tokens.append(chunk)

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="analyse the ARR")],
        tools=definitions,
        executor=composite,
        max_iterations=4,
        progressive_tools=False,
        on_event=_on_event,
        on_token=_on_token,
    )
    return response, events, "".join(tokens)


def _statuses(events: list[dict]) -> list[str]:
    return [e.get("status") for e in events if e.get("status")]


def _thinking_text(events: list[dict]) -> str:
    return "".join(
        str(e.get("content") or "")
        for e in events
        if e.get("status") == "thinking_text"
    )


@pytest.mark.asyncio
async def test_short_preamble_goes_to_reasoning_never_to_answer_stream():
    """A short pre-tool preamble must surface as thinking_text and must
    never leak into the live answer stream."""
    assert len(SHORT_PREAMBLE) < _PREAMBLE_HOLDBACK_CHARS
    llm = _ScriptedStreamLLM(
        first_text=SHORT_PREAMBLE, first_has_tools=True, answer=FINAL_ANSWER
    )
    response, events, streamed = await _run(llm)

    assert SHORT_PREAMBLE in _thinking_text(events)
    assert SHORT_PREAMBLE not in streamed
    assert "stream_retract" not in _statuses(events)
    # The real answer still streams live (it overflows the window).
    assert streamed == FINAL_ANSWER
    assert response.content == FINAL_ANSWER


@pytest.mark.asyncio
async def test_long_answer_flushes_to_live_stream_after_holdback():
    """A long final answer must flush to the live stream (preceded by a
    ``streaming`` status) once the hold-back window overflows."""
    llm = _ScriptedStreamLLM(
        first_text=FINAL_ANSWER, first_has_tools=False, answer=FINAL_ANSWER
    )
    response, events, streamed = await _run(llm)

    assert llm.calls == 1
    assert streamed == FINAL_ANSWER
    assert "streaming" in _statuses(events)
    assert "stream_retract" not in _statuses(events)
    # The answer must not pollute the reasoning channel.
    assert FINAL_ANSWER[:60] not in _thinking_text(events)
    assert response.content == FINAL_ANSWER


@pytest.mark.asyncio
async def test_flushed_text_followed_by_tools_is_retracted():
    """Rare case: more than the hold-back window streamed and tool calls
    still followed — the loop must retract the provisional answer and
    re-surface the text as reasoning."""
    assert len(LONG_TEXT) > _PREAMBLE_HOLDBACK_CHARS
    llm = _ScriptedStreamLLM(
        first_text=LONG_TEXT, first_has_tools=True, answer=FINAL_ANSWER
    )
    response, events, streamed = await _run(llm)

    statuses = _statuses(events)
    assert "stream_retract" in statuses
    # The retracted text re-arrives as reasoning prose.
    assert LONG_TEXT[:60] in _thinking_text(events)
    # The retract must precede the final answer streaming.
    assert statuses.index("stream_retract") < len(statuses) - 1
    assert response.content == FINAL_ANSWER
    # The final answer still streamed live after the retract.
    assert streamed.endswith(FINAL_ANSWER)


@pytest.mark.asyncio
async def test_short_final_answer_is_replayed_at_loop_end():
    """A final answer that never overflowed the window must still reach the
    user through the answer stream (replayed at loop end)."""
    llm = _ScriptedStreamLLM(
        first_text=SHORT_ANSWER, first_has_tools=False, answer=SHORT_ANSWER
    )
    response, events, streamed = await _run(llm)

    assert llm.calls == 1
    assert streamed == SHORT_ANSWER
    assert "streaming" in _statuses(events)
    # The short answer must not appear in the reasoning channel.
    assert SHORT_ANSWER not in _thinking_text(events)
    assert response.content == SHORT_ANSWER


# --- The evaluated turn streams to time its answer ---------------------


class _SameScriptLLM(_ScriptedStreamLLM):
    """The same scripted output whether the loop streams or not: ``chat`` answers what the stream would."""

    async def chat(self, messages, model=None, tools=None, **kwargs):
        return await self.chat_stream_with_tools(messages, tools, model=model)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_text", "first_has_tools", "answer"),
    [
        (SHORT_ANSWER, False, SHORT_ANSWER),
        (FINAL_ANSWER, False, FINAL_ANSWER),
        (SHORT_PREAMBLE, True, FINAL_ANSWER),
        (LONG_TEXT, True, FINAL_ANSWER),
    ],
    ids=["short_answer", "long_answer", "preamble_then_tool", "long_text_retracted_then_tool"],
)
async def test_an_evaluated_turn_streaming_to_time_its_answer_returns_the_same_answer(
    first_text, first_has_tools, answer
):
    """Given the same model output, the loop returns the same answer with the turn's token callback as without."""
    from app.services.agent_chat import _FirstTokenTimer

    async def _answer(_chunk: str) -> None:
        return None

    answers = []
    for streams in (False, True):
        llm = _SameScriptLLM(first_text=first_text, first_has_tools=first_has_tools, answer=answer)
        definitions, composite = _build_executor()
        response = await run_agent_loop(
            llm=_FirstTokenTimer(llm) if streams else llm,
            messages=[ChatMessage(role="user", content="analyse the ARR")],
            tools=definitions,
            executor=composite,
            max_iterations=4,
            progressive_tools=False,
            on_event=None,
            on_token=_answer if streams else None,
        )
        answers.append(response.content)

    assert answers[0] == answers[1] == answer
