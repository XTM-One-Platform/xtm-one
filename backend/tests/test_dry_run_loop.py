"""Tests for dry-run agent-loop behaviour with ``executor=None`` (CORE-1).

Dry-run assignments pass ``executor=None`` while still offering tools, so
the model can describe its plan.  When the model requests tool calls the
loop must NOT return early with a broken transcript (assistant message with
``tool_calls`` but no matching tool results — a provider-invariant
violation).  Instead it appends a synthetic ``[dry-run]`` result for each
requested call and continues, letting the model produce a full plan.
"""

from __future__ import annotations

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools import agent_loop as al
from app.tools.base import ToolCall, ToolDefinition
from app.tools.tool_gating import LOAD_TOOLS_TOOL_NAME


class _ScriptedLLM(BaseLLMProvider):
    """Stub LLM returning a pre-scripted sequence of responses and recording
    the tool names offered on each ``chat`` call."""

    provider_name = "stub"
    context_window = 100_000

    def __init__(self, script: list[ChatResponse]) -> None:
        self._script = script
        self._idx = 0
        self.offered_tools: list[list[str]] = []

    async def chat(self, messages, model=None, tools=None, **kwargs):
        self.offered_tools.append([t.name for t in (tools or [])])
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


_TOOLS = [
    ToolDefinition(
        name="send_email",
        description="Send an email",
        input_schema={
            "type": "object",
            "properties": {"to": {"type": "string"}},
            "required": ["to"],
        },
        source="integration:microsoft",
    ),
    ToolDefinition(
        name="search_knowledge",
        description="Search the knowledge base",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        source="builtin:knowledge",
    ),
]


@pytest.mark.asyncio
async def test_dry_run_continues_past_tool_intent_and_returns_plan(
    _quiet_langfuse,
):
    """With ``executor=None`` and a model that first requests a tool, the
    loop must append a synthetic dry-run result and keep going until the
    model produces its final plan text."""
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="send_email",
                        arguments={"to": "user@example.com"},
                    )
                ],
            ),
            ChatResponse(
                content="Plan: I would send an email to user@example.com.",
                model="stub",
            ),
        ]
    )
    messages: list[ChatMessage] = [
        ChatMessage(role="user", content="dry run: notify the user")
    ]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=_TOOLS,
        executor=None,
        max_iterations=5,
        progressive_tools=False,
    )

    assert response.content == (
        "Plan: I would send an email to user@example.com."
    )

    # The transcript stays provider-valid: the assistant tool_calls message
    # is followed by a matching synthetic tool result.
    synthetic = [
        m for m in messages if m.role == "tool" and m.tool_call_id == "c1"
    ]
    assert len(synthetic) == 1
    assert "[dry-run] Tool 'send_email' was not executed" in synthetic[0].content
    assert "continue describing your plan" in synthetic[0].content


@pytest.mark.asyncio
async def test_dry_run_appends_synthetic_result_for_each_parallel_call(
    _quiet_langfuse,
):
    """A multi-call turn in dry-run mode gets one synthetic result per
    requested call so every tool_call id has a matching result."""
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[
                    ToolCall(id="c1", name="send_email", arguments={"to": "a@b.c"}),
                    ToolCall(
                        id="c2", name="search_knowledge", arguments={"query": "x"}
                    ),
                ],
            ),
            ChatResponse(content="Plan: email then search.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="dry run")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=_TOOLS,
        executor=None,
        max_iterations=5,
        progressive_tools=False,
    )

    assert response.content == "Plan: email then search."
    tool_results = {
        m.tool_call_id: m.content for m in messages if m.role == "tool"
    }
    assert set(tool_results) == {"c1", "c2"}
    assert "[dry-run] Tool 'send_email' was not executed" in tool_results["c1"]
    assert (
        "[dry-run] Tool 'search_knowledge' was not executed" in tool_results["c2"]
    )


@pytest.mark.asyncio
async def test_dry_run_repeated_tool_intents_stay_bounded(_quiet_langfuse):
    """A model that keeps requesting tools in dry-run mode is bounded by
    ``max_iterations`` (wind-down/tool-strip stages force a text answer)."""
    tool_turn = ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[
            ToolCall(id="c", name="send_email", arguments={"to": "a@b.c"})
        ],
    )
    # Two tool-intent turns, then the model yields the plan.
    llm = _ScriptedLLM(
        [
            tool_turn,
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="c2", name="search_knowledge", arguments={"query": "x"}
                    )
                ],
            ),
            ChatResponse(content="Final plan.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="dry run")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=_TOOLS,
        executor=None,
        max_iterations=6,
        progressive_tools=False,
    )

    assert response.content == "Final plan."
    dry_run_results = [
        m
        for m in messages
        if m.role == "tool" and "[dry-run]" in (m.content or "")
    ]
    assert len(dry_run_results) == 2


@pytest.mark.asyncio
async def test_dry_run_offers_all_tool_schemas_upfront(_quiet_langfuse):
    """Progressive tool disclosure is deliberately not activated for
    executor-less loops (the gating decision requires an executor), so a dry
    run offers EVERY tool schema up-front — the model plans with full schema
    visibility instead of having to reveal groups it can never execute.  A
    tool intent against one of those tools still gets a synthetic result."""
    gated_tools = [
        ToolDefinition(
            name=f"notion_tool_{i}",
            description=f"Notion tool {i}",
            input_schema={
                "type": "object",
                "properties": {"q": {"type": "string"}},
            },
            source="integration:notion",
        )
        for i in range(18)  # >= progressive_tools_min_tools threshold
    ]
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[
                    ToolCall(id="c1", name="notion_tool_3", arguments={"q": "x"})
                ],
            ),
            ChatResponse(content="Plan: query notion.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="dry run")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=gated_tools,
        executor=None,
        max_iterations=6,
        progressive_tools=True,
    )

    assert response.content == "Plan: query notion."

    # Gating never activated: all 18 schemas were offered on iteration 1 and
    # the ``load_tools`` meta-tool was not.
    offered = llm.offered_tools[0]
    assert LOAD_TOOLS_TOOL_NAME not in offered
    assert sum(n.startswith("notion_tool_") for n in offered) == 18

    # The tool intent still got a synthetic dry-run result.
    synthetic = [
        m for m in messages if m.role == "tool" and m.tool_call_id == "c1"
    ]
    assert len(synthetic) == 1
    assert "[dry-run] Tool 'notion_tool_3' was not executed" in synthetic[0].content
