"""Tests for the optional per-run token budget in the agent loop (CORE-7).

``run_agent_loop(max_total_tokens=...)`` enforces a ceiling on the
accumulated LLM usage across all iterations.  When exceeded, the loop
reuses the existing Stage-2 wind-down — tools are stripped and the
last-chance prompt is injected — so the LLM is forced to produce a final
answer instead of burning more budget.  ``None`` (the default) disables
the budget entirely for full backward compatibility.
"""

from __future__ import annotations

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools import agent_loop as al
from app.tools.agent_loop import _LAST_CHANCE_PROMPT
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
)


class _ScriptedLLM(BaseLLMProvider):
    """Stub LLM returning a pre-scripted sequence of responses and recording
    the tool names offered on each ``chat`` call."""

    provider_name = "stub"
    context_window = 100_000

    def __init__(self, script: list[ChatResponse]) -> None:
        self._script = script
        self._idx = 0
        self.offered_tools: list[list[str] | None] = []

    async def chat(self, messages, model=None, tools=None, **kwargs):
        self.offered_tools.append(
            [t.name for t in tools] if tools is not None else None
        )
        response = self._script[self._idx]
        self._idx += 1
        return response

    async def chat_stream(self, messages, **kwargs):
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.fixture
def _quiet_langfuse(monkeypatch):
    """Neutralise Langfuse plumbing so the loop runs without real tracing."""
    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)


def _executor() -> tuple[CompositeToolExecutor, list[str]]:
    executed: list[str] = []
    callable_exec = CallableToolExecutor()
    definitions = [
        ToolDefinition(name="lookup", description="lookup", source="custom:test")
    ]

    async def _handler(**kwargs):
        executed.append("lookup")
        return "ran lookup"

    callable_exec.register("lookup", _handler)
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)
    return composite, executed


def _tool_call(call_id: str, q: str) -> ToolCall:
    return ToolCall(id=call_id, name="lookup", arguments={"q": q})


@pytest.mark.asyncio
async def test_budget_exceeded_strips_tools_and_returns_final_answer(
    _quiet_langfuse,
):
    """Once the accumulated usage crosses the budget, the next iteration runs
    tool-free with the last-chance prompt — and the loop still returns a
    final answer."""
    composite, executed = _executor()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[_tool_call("c1", "x")],
                usage={"total_tokens": 600},
            ),
            ChatResponse(content="Budget answer.", model="stub", usage={}),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        max_total_tokens=500,
    )

    assert response.content == "Budget answer."
    assert executed == ["lookup"]
    # First call offered the tool; the post-budget call must be tool-free.
    # ``wait`` rides along with the agent's tools — the loop offers its own
    # timer primitive. ``None`` on the second call is the point here:
    # the budget stripped every tool, including the timer.
    assert llm.offered_tools[0] == ["lookup", "wait"]
    assert llm.offered_tools[1] is None
    # The Stage-2 last-chance prompt was injected (same wind-down mechanism
    # as the iteration cap).
    assert any(
        getattr(m, "role", None) == "system" and m.content == _LAST_CHANCE_PROMPT
        for m in messages
    )


@pytest.mark.asyncio
async def test_budget_disabled_by_default(_quiet_langfuse):
    """Without ``max_total_tokens`` the loop never strips tools because of
    usage — full backward compatibility."""
    composite, executed = _executor()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[_tool_call("c1", "x")],
                usage={"total_tokens": 1_000_000},
            ),
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[_tool_call("c2", "y")],
                usage={"total_tokens": 1_000_000},
            ),
            ChatResponse(content="Done.", model="stub", usage={}),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
    )

    assert response.content == "Done."
    assert executed == ["lookup", "lookup"]
    # Tools were offered on every tool-calling iteration.
    assert llm.offered_tools[0] == ["lookup", "wait"]
    assert llm.offered_tools[1] == ["lookup", "wait"]
    assert not any(
        getattr(m, "role", None) == "system" and m.content == _LAST_CHANCE_PROMPT
        for m in messages
    )


@pytest.mark.asyncio
async def test_budget_under_limit_does_not_strip(_quiet_langfuse):
    """Usage below the budget never triggers the wind-down."""
    composite, executed = _executor()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[_tool_call("c1", "x")],
                usage={"total_tokens": 100},
            ),
            ChatResponse(content="Done.", model="stub", usage={"total_tokens": 100}),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        max_total_tokens=10_000,
    )

    assert response.content == "Done."
    assert executed == ["lookup"]
    assert llm.offered_tools[1] == ["lookup", "wait"]


@pytest.mark.asyncio
async def test_budget_sums_provider_specific_usage_keys(_quiet_langfuse):
    """Providers without a ``total_tokens`` key (Anthropic-style
    input/output split) must still trip the budget."""
    composite, _executed = _executor()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[_tool_call("c1", "x")],
                usage={"input_tokens": 400, "output_tokens": 200},
            ),
            ChatResponse(content="Budget answer.", model="stub", usage={}),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        max_total_tokens=500,
    )

    assert response.content == "Budget answer."
    assert llm.offered_tools[1] is None
