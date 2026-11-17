"""Leaked tool-call guard: the loop re-prompts a leak the adapter could not parse.

A model sometimes serialises a tool call's markup into its message as text
instead of making a structured call. Parsing that markup is the provider
adapter's job (``app.llm.tool_markup_recovery`` — tested in
``test_tool_markup_recovery.py``): a well-formed leak arrives here as an
ordinary tool call. Only a leak the adapter could NOT parse reaches the loop,
through the generic ``ChatResponse.leaked_tool_markup_unrecovered`` flag, and
the loop re-prompts the model to invoke the tool for real — without knowing any
vendor markup itself.
"""

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools.agent_loop import (
    _LEAKED_TOOL_CALL_NUDGE,
    CONTINUATION_COMPOSING_REASONS,
    run_agent_loop,
)
from app.tools.base import CallableToolExecutor, ToolCall, ToolDefinition


def test_guard_reason_is_registered_as_a_continuation():
    # So the turn-limit latch never mislabels the re-prompt as "turn limit reached".
    assert "leaked_tool_call_continuation" in CONTINUATION_COMPOSING_REASONS


# ── Loop-level behaviour ──────────────────────────────────────────────────


class _ScriptedLLM(BaseLLMProvider):
    """LLM stub returning a scripted response per ``chat`` call."""

    provider_name = "stub"
    context_window = 200_000

    def __init__(self, script):
        self._script = script
        self.calls = 0

    async def chat(
        self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs
    ):
        idx = self.calls
        self.calls += 1
        return self._script(idx, model)

    async def chat_stream(
        self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs
    ):
        yield "x"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


def _work_tools():
    executor = CallableToolExecutor()

    async def _do_work(**_kwargs):
        return "work done"

    executor.register("do_work", _do_work)
    tools = [
        ToolDefinition(
            name="do_work",
            description="Perform a unit of work.",
            input_schema={"type": "object", "properties": {}},
            source="test",
        )
    ]
    return tools, executor


# The text the adapter could not parse (e.g. truncated markup); it carried the
# generic ``leaked_tool_markup_unrecovered`` flag with it.
_UNPARSED_LEAK = 'Let me act.\n<function_calls>\n<invoke name="do_work">\n'


@pytest.mark.asyncio
async def test_unrecovered_leak_is_reprompted_and_never_surfaced():
    """A tool-free answer the adapter flagged as an unparsed leak is re-prompted
    to invoke the tool for real; the loop surfaces the eventual real answer."""

    def script(idx, model):
        if idx == 0:
            # A real tool call first, so had_tool_calls_this_run is set.
            return ChatResponse(
                content="",
                model=model or "stub",
                tool_calls=[ToolCall(id="t1", name="do_work", arguments={})],
                stop_reason="tool_use",
            )
        if idx == 1:
            return ChatResponse(
                content=_UNPARSED_LEAK,
                model=model or "stub",
                leaked_tool_markup_unrecovered=True,
            )
        return ChatResponse(content="Scenario created.", model=model or "stub")

    llm = _ScriptedLLM(script)
    tools, executor = _work_tools()
    messages = [ChatMessage(role="user", content="Build a scenario")]
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    response = await run_agent_loop(
        llm=llm,
        messages=messages,
        tools=tools,
        executor=executor,
        max_iterations=6,
        on_event=on_event,
    )

    assert response.content == "Scenario created."
    composing = [e for e in events if e.get("status") == "composing"]
    assert any(e.get("reason") == "leaked_tool_call_continuation" for e in composing)
    assert sum(_LEAKED_TOOL_CALL_NUDGE in (m.content or "") for m in messages) == 1


@pytest.mark.asyncio
async def test_no_guard_without_prior_tool_use():
    """A turn that never used a tool is returned as-is, even when flagged (the
    guard is gated on had_tool_calls_this_run)."""

    def script(idx, model):
        return ChatResponse(
            content=_UNPARSED_LEAK,
            model=model or "stub",
            leaked_tool_markup_unrecovered=True,
        )

    llm = _ScriptedLLM(script)
    tools, executor = _work_tools()
    messages = [ChatMessage(role="user", content="hi")]

    response = await run_agent_loop(
        llm=llm,
        messages=messages,
        tools=tools,
        executor=executor,
        max_iterations=6,
    )

    assert response.content == _UNPARSED_LEAK
    assert llm.calls == 1  # returned immediately, no re-prompt


@pytest.mark.asyncio
async def test_a_runaway_cut_by_the_repetition_guard_is_not_re_prompted():
    """A runaway that loops an unparseable leak carries the unrecovered flag,
    but the degenerate-repetition guard runs first and ends the run: the
    leaked-call guard never re-prompts a cut answer."""
    from app.tools.agent_loop import _DEGENERATE_REPETITION_NOTICE

    looped = (
        "Calling the tool now to build the scenario for the test stage.\n"
        '<function_calls>\n<invoke name="do_work">\n'
        "The parameters follow in the next block of this message.\n"
    ) * 20

    def script(idx, model):
        if idx == 0:
            return ChatResponse(
                content="",
                model=model or "stub",
                tool_calls=[ToolCall(id="t1", name="do_work", arguments={})],
                stop_reason="tool_use",
            )
        return ChatResponse(
            content=looped, model=model or "stub", leaked_tool_markup_unrecovered=True
        )

    llm = _ScriptedLLM(script)
    tools, executor = _work_tools()

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="Build it")],
        tools=tools,
        executor=executor,
        max_iterations=6,
    )

    assert llm.calls == 2
    assert response.content == _DEGENERATE_REPETITION_NOTICE
