"""A tool that calls a model itself spends the run's allowance (#9126).

The agent loop binds a ``ToolModelSpend`` around each tool call: the tool's own model
usage is the run's (its usage total, its per-run budget), each call reserves its worst
case before it is made, and a call the run can no longer pay for is refused.
"""

from __future__ import annotations

import pytest

from app.llm.base import ChatMessage, ChatResponse
from app.tools import agent_loop as al
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
    current_tool_model_spend,
)
from app.tools.run_budget import RunBudget
from tests.test_agent_loop_run_budget import _PADDING, _quiet_langfuse, _ScriptedLLM  # noqa: F401


def _spending_tool(observed: list[bool]) -> CompositeToolExecutor:
    async def _handler(**_: object) -> str:
        spend = current_tool_model_spend()
        assert spend is not None, "a tool call runs under the run's spend"
        release = spend.reserve(10_000, 16_000)
        observed.append(release is not None)
        if release is not None:
            spend.record({"input_tokens": 300, "output_tokens": 200})
            release()
        return "extracted"

    executor = CallableToolExecutor()
    executor.register("extract", _handler)
    composite = CompositeToolExecutor()
    composite.add_tools([ToolDefinition(name="extract", description="extract", source="custom:test")], executor)
    return composite


def _script() -> _ScriptedLLM:
    return _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[ToolCall(id="c1", name="extract", arguments={})],
                usage={"input_tokens": 40, "output_tokens": 10},
            ),
            ChatResponse(content="Done." + _PADDING, model="stub", usage={"input_tokens": 60, "output_tokens": 20}),
        ]
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_quiet_langfuse")
async def test_a_tools_model_usage_is_the_runs() -> None:
    observed: list[bool] = []
    composite = _spending_tool(observed)
    response = await al.run_agent_loop(
        llm=_script(),
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
    )
    assert observed == [True]
    assert response.usage["input_tokens"] == 40 + 300 + 60
    assert response.usage["output_tokens"] == 10 + 200 + 20
    assert current_tool_model_spend() is None, "the spend is the tool call's alone"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_quiet_langfuse")
async def test_a_tool_call_the_runs_budget_cannot_pay_for_is_refused() -> None:
    observed: list[bool] = []
    composite = _spending_tool(observed)
    await al.run_agent_loop(
        llm=_script(),
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        budget=RunBudget(max_total_tokens=5_000),
    )
    assert observed == [False]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_quiet_langfuse")
async def test_calls_made_at_once_each_hold_their_reservation() -> None:
    """Four calls behind one gate cannot all pass with room for one: each holds its
    worst case until it is released."""
    observed: list[list[bool]] = []

    async def _handler(**_: object) -> str:
        spend = current_tool_model_spend()
        assert spend is not None
        held = [spend.reserve(10_000, 16_000) for _ in range(4)]
        observed.append([release is not None for release in held])
        for release in held:
            if release is not None:
                release()
        again = spend.reserve(10_000, 16_000)
        observed.append([again is not None])
        if again is not None:
            again()
        return "extracted"

    executor = CallableToolExecutor()
    executor.register("extract", _handler)
    composite = CompositeToolExecutor()
    composite.add_tools([ToolDefinition(name="extract", description="extract", source="custom:test")], executor)
    await al.run_agent_loop(
        llm=_script(),
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        budget=RunBudget(max_total_tokens=60_000),
    )
    assert observed == [[True, False, False, False], [True]]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_quiet_langfuse")
async def test_calls_made_at_once_hold_their_reservation_against_max_total_tokens() -> None:
    """Without a run budget, the run's ``max_total_tokens`` gates them the same way: four
    calls of 26,000 tokens each at most, behind a ceiling of 40,000, cannot all pass (the
    flag ``_merge_usage`` sets after a call would let every one through)."""
    observed: list[list[bool]] = []

    async def _handler(**_: object) -> str:
        spend = current_tool_model_spend()
        assert spend is not None
        held = [spend.reserve(10_000, 16_000) for _ in range(4)]
        observed.append([release is not None for release in held])
        for release in held:
            if release is not None:
                release()
        return "extracted"

    executor = CallableToolExecutor()
    executor.register("extract", _handler)
    composite = CompositeToolExecutor()
    composite.add_tools([ToolDefinition(name="extract", description="extract", source="custom:test")], executor)
    await al.run_agent_loop(
        llm=_script(),
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        max_total_tokens=40_000,
    )
    assert observed == [[True, False, False, False]]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_quiet_langfuse")
async def test_a_tool_call_sees_the_timeout_the_loop_applies_to_it() -> None:
    """The loop binds the timeout it applies to the call in progress (the tool's declared
    one, or less when the run's wall clock has less left): a tool that answers with what it
    finished keeps inside it."""
    from app.tools.base import tool_call_timeout

    seen: list[float | None] = []

    async def _handler(**_: object) -> str:
        seen.append(tool_call_timeout())
        return "extracted"

    executor = CallableToolExecutor()
    executor.register("extract", _handler)
    composite = CompositeToolExecutor()
    composite.add_tools([ToolDefinition(name="extract", description="extract", source="custom:test", execution_timeout=1800)], executor)
    await al.run_agent_loop(llm=_script(), messages=[ChatMessage(role="user", content="go")], tools=composite.all_tools,
                            executor=composite, max_iterations=5)
    assert seen and seen[0] is not None and 0 < seen[0] <= 1800
    assert tool_call_timeout() is None, "bound for the call alone"
