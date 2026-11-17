"""Liveness heartbeat during long tool executions.

Long-running tools (consults, background-task batches, big integration
calls) produce no events between ``tool_start`` and ``tool_done``.  The
loop must emit periodic ``{"status": "tool_heartbeat", "tools": [...],
"elapsed_s": N}`` events while a tool batch runs so frontends can show a
live elapsed-time indicator — the user must never stare at a static
status bubble wondering whether the run is stuck.
"""

from __future__ import annotations

import asyncio

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools import agent_loop
from app.tools.agent_loop import run_agent_loop
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
)


class _ToolThenAnswerLLM(BaseLLMProvider):
    """First call issues the scripted tool calls, second call answers."""

    provider_name = "stub"
    context_window = 100_000

    def __init__(self, tool_calls: list[ToolCall]) -> None:
        self.scripted_calls = tool_calls
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(
                content="",
                model="stub",
                tool_calls=self.scripted_calls,
                stop_reason="tool_use",
            )
        return ChatResponse(content="All done.", model="stub")

    async def chat_stream(self, messages, **kwargs):
        yield "All done."

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


def _build_executor(delay_s: float) -> tuple[list[ToolDefinition], CompositeToolExecutor]:
    async def _slow(i: int = 0) -> str:
        await asyncio.sleep(delay_s)
        return f"slow-result-{i}"

    callable_exec = CallableToolExecutor()
    callable_exec.register("slow_tool", _slow)
    definitions = [ToolDefinition(name="slow_tool", description="Slow tool")]
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)
    return definitions, composite


async def _run(llm, definitions, composite) -> list[dict]:
    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="run the slow tool")],
        tools=definitions,
        executor=composite,
        max_iterations=4,
        progressive_tools=False,
        on_event=_on_event,
    )
    assert response.content == "All done."
    return events


def _heartbeats(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("status") == "tool_heartbeat"]


@pytest.mark.asyncio
async def test_heartbeat_emitted_during_slow_single_tool(monkeypatch):
    monkeypatch.setattr(agent_loop, "_TOOL_HEARTBEAT_INTERVAL_S", 0.05)
    definitions, composite = _build_executor(delay_s=0.3)
    llm = _ToolThenAnswerLLM(
        [ToolCall(id="tc-1", name="slow_tool", arguments={"i": 1})]
    )

    events = await _run(llm, definitions, composite)

    beats = _heartbeats(events)
    assert len(beats) >= 2, "expected periodic heartbeats during the slow tool"
    for beat in beats:
        assert beat["tools"] == ["slow_tool"]
        assert isinstance(beat["elapsed_s"], int)
    # Ordering: heartbeats sit between tool_start and the tool_done.
    statuses = [e.get("status") for e in events if e.get("status")]
    assert statuses.index("tool_start") < statuses.index("tool_heartbeat")
    assert statuses.index("tool_heartbeat") < statuses.index("tool_done")


@pytest.mark.asyncio
async def test_heartbeat_emitted_during_parallel_batch(monkeypatch):
    monkeypatch.setattr(agent_loop, "_TOOL_HEARTBEAT_INTERVAL_S", 0.05)
    definitions, composite = _build_executor(delay_s=0.3)
    llm = _ToolThenAnswerLLM(
        [
            ToolCall(id="tc-1", name="slow_tool", arguments={"i": 1}),
            ToolCall(id="tc-2", name="slow_tool", arguments={"i": 2}),
        ]
    )

    events = await _run(llm, definitions, composite)

    beats = _heartbeats(events)
    assert len(beats) >= 2
    for beat in beats:
        assert beat["tools"] == ["slow_tool", "slow_tool"]


@pytest.mark.asyncio
async def test_fast_tool_emits_no_heartbeat():
    # Default 10s interval: a fast tool must complete without any heartbeat.
    definitions, composite = _build_executor(delay_s=0.0)
    llm = _ToolThenAnswerLLM(
        [ToolCall(id="tc-1", name="slow_tool", arguments={"i": 1})]
    )

    events = await _run(llm, definitions, composite)

    assert _heartbeats(events) == []
