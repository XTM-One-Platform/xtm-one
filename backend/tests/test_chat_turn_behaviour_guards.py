"""Tests for the Phase-4 chat-turn behavioural guards.

Covers:

* P4.1 — the consecutive-same-tool-failure brake: once one tool fails
  ``_TOOL_FAILURE_BRAKE_THRESHOLD`` times in a row, tools are withdrawn and
  the loop winds down to an honest final answer instead of burning the
  whole iteration budget (the 2026-07-09 incident pattern: 30+ minutes of
  failing Metabase SQL).
* P4.2 — the wall-clock budget-warning nudge at ~50% of the turn budget.
* P4.3 — compaction-state hygiene: a persisted watermark that no longer
  matches the message log (purged messages) is classified stale.
"""

from __future__ import annotations

import asyncio
import time

import pytest

import app.tools.agent_loop as al
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.services.conversation_memory import (
    compaction_state_is_stale,
    parse_compaction_state,
)
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _build_failing_executor() -> tuple[list[ToolDefinition], CompositeToolExecutor]:
    async def _always_fails(q: str = "") -> str:
        raise RuntimeError("SQL syntax error near SELECT")

    callable_exec = CallableToolExecutor()
    callable_exec.register("run_sql", _always_fails)
    definitions = [ToolDefinition(name="run_sql", description="Run a SQL query")]
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)
    return definitions, composite


class _GrindingLLM(BaseLLMProvider):
    """Keeps hammering the same failing tool while tools are offered.

    Each with-tools iteration issues a parallel batch of
    ``calls_per_iteration`` identical-tool calls (distinct arguments so the
    dedup cache never kicks in).  Once tools are stripped it produces an
    honest final answer.
    """

    provider_name = "stub"
    context_window = 100_000

    def __init__(self, calls_per_iteration: int = 4) -> None:
        self.chat_calls = 0
        self.with_tools_calls = 0
        self._per_iter = calls_per_iteration

    async def chat(self, messages, tools=None, model=None, **kwargs):
        self.chat_calls += 1
        if tools:
            self.with_tools_calls += 1
            return ChatResponse(
                content="",
                model=model or "stub",
                tool_calls=[
                    ToolCall(
                        id=f"tc-{self.chat_calls}-{i}",
                        name="run_sql",
                        arguments={"q": f"SELECT {self.chat_calls}-{i}"},
                    )
                    for i in range(self._per_iter)
                ],
                stop_reason="tool_use",
            )
        return ChatResponse(
            content="I could not get run_sql working after several attempts.",
            model=model or "stub",
        )

    async def chat_stream(self, messages, **kwargs):
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


# ---------------------------------------------------------------------------
# P4.1 — consecutive-same-tool-failure brake
# ---------------------------------------------------------------------------


def _build_failing_executor_with_fallback() -> tuple[list[ToolDefinition], CompositeToolExecutor]:
    """``run_sql`` always fails; ``list_tables`` always works, so the turn keeps a
    tool while ``run_sql`` sits out its repeated-failure cooldown."""

    async def _always_fails(q: str = "") -> str:
        raise RuntimeError("SQL syntax error near SELECT")

    async def _works(q: str = "") -> str:
        return f"tables: users, orders ({q})"

    callable_exec = CallableToolExecutor()
    callable_exec.register("run_sql", _always_fails)
    callable_exec.register("list_tables", _works)
    definitions = [
        ToolDefinition(name="run_sql", description="Run a SQL query"),
        ToolDefinition(name="list_tables", description="List the tables"),
    ]
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)
    return definitions, composite


class _BatchThenRetryLLM(BaseLLMProvider):
    """Issues ``calls_per_turn`` parallel ``run_sql`` calls every turn it is offered
    ``run_sql`` (at most ``run_sql_turns`` such turns), one ``list_tables`` call
    when only that is offered, and a final answer once no tool is offered."""

    provider_name = "stub"
    context_window = 100_000

    def __init__(self, calls_per_turn: int, run_sql_turns: int) -> None:
        self.chat_calls = 0
        self.run_sql_turns = 0
        self.offered: list[list[str]] = []
        self._per_turn = calls_per_turn
        self._max_turns = run_sql_turns

    async def chat(self, messages, tools=None, model=None, **kwargs):
        self.chat_calls += 1
        names = sorted(t.name for t in tools or [])
        self.offered.append(names)
        if "run_sql" in names and self.run_sql_turns < self._max_turns:
            self.run_sql_turns += 1
            calls = [
                ToolCall(id=f"tc-{self.chat_calls}-{i}", name="run_sql", arguments={"q": f"SELECT {self.chat_calls}-{i}"})
                for i in range(self._per_turn)
            ]
            return ChatResponse(content="", model=model or "stub", tool_calls=calls, stop_reason="tool_use")
        if "list_tables" in names and self.run_sql_turns < self._max_turns:
            return ChatResponse(
                content="",
                model=model or "stub",
                tool_calls=[ToolCall(id=f"tc-{self.chat_calls}", name="list_tables", arguments={"q": str(self.chat_calls)})],
                stop_reason="tool_use",
            )
        return ChatResponse(content="I could not get run_sql working after several attempts.", model=model or "stub")

    async def chat_stream(self, messages, **kwargs):
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.mark.asyncio
async def test_one_turn_of_parallel_failures_is_one_failed_turn():
    """Five parallel calls of one tool failing in ONE turn are one decision the
    model took before seeing any result: no brake, no nudge, and the next turn
    is offered the tool again so the model can change approach."""
    definitions, composite = _build_failing_executor()
    llm = _BatchThenRetryLLM(calls_per_turn=5, run_sql_turns=1)

    messages = [ChatMessage(role="user", content="look the users up")]
    await al.run_agent_loop(
        llm=llm, messages=messages, tools=definitions, executor=composite, max_iterations=6, progressive_tools=False
    )

    failed = [m for m in messages if m.role == "tool" and "SQL syntax error" in (m.content or "")]
    assert len(failed) == 5, "every parallel call ran and failed"
    assert "run_sql" in llm.offered[1], "the turn after the failed batch is offered the tool"
    assert not any(m.role == "system" and "repeated tool failure" in (m.content or "") for m in messages)


@pytest.mark.asyncio
async def test_tool_failure_brake_forces_wind_down():
    """Four successive turns failing on the same tool must engage the brake:
    tools stripped from the next iteration, honest final answer, iteration
    budget NOT burned (P4.1). Each turn is a parallel batch of four calls:
    it counts once, so the brake is reached at the fourth failed turn."""
    definitions, composite = _build_failing_executor_with_fallback()
    llm = _BatchThenRetryLLM(calls_per_turn=4, run_sql_turns=10)
    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    messages = [ChatMessage(role="user", content="run the report")]
    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=definitions,
        executor=composite,
        max_iterations=20,
        progressive_tools=False,
        on_event=_on_event,
    )

    # The turn ended with the honest wind-down answer, not the backstop.
    assert "could not get run_sql working" in (response.content or "")
    # Four failed turns of run_sql (the repeated-failure cooldown in between
    # filled with list_tables), then one stripped iteration.
    assert llm.run_sql_turns == al._TOOL_FAILURE_BRAKE_THRESHOLD == 4
    assert llm.offered[-1] == [], "tools must be withdrawn after the brake engages"
    stops = [m.content for m in messages if m.role == "system" and "repeated tool failure, stopping" in (m.content or "")]
    assert len(stops) == 1 and "The tool 'run_sql' has failed in 4 turns in a row" in stops[0]
    # The user-visible budget warning was emitted.
    assert any(
        e.get("status") == "thinking_text"
        and "keeps failing" in str(e.get("content", ""))
        and "4 turns in a row" in str(e.get("content", ""))
        for e in events
    )


@pytest.mark.asyncio
async def test_brake_not_engaged_below_threshold():
    """Fewer consecutive failures than the threshold must NOT strip tools —
    the existing nudge/cooldown behaviour still applies (P4.1)."""
    definitions, composite = _build_failing_executor()
    llm = _GrindingLLM(calls_per_iteration=2)

    messages = [ChatMessage(role="user", content="run the report")]
    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=definitions,
        executor=composite,
        max_iterations=6,
        progressive_tools=False,
    )

    assert not any(
        m.role == "system" and "repeated tool failure, stopping" in (m.content or "")
        for m in messages
    ), "2 consecutive failures are below the brake threshold"


def _build_rejected_generation_executor(succeeds: frozenset[str] = frozenset()) -> tuple[list[ToolDefinition], CompositeToolExecutor]:
    """A tool whose call runs and whose work comes out rejected: ``outcome_failure``; the prompts in *succeeds* pass."""
    from app.tools.base import ToolResult

    async def _generate(prompt: str = "") -> ToolResult:
        if prompt in succeeds:
            return ToolResult(tool_call_id="", content=f"shot ready ({prompt})")
        return ToolResult(tool_call_id="", content=f"shot failed: content drift ({prompt})", is_error=True, outcome_failure=True)

    callable_exec = CallableToolExecutor()
    callable_exec.register("generate_shot", _generate)
    definitions = [ToolDefinition(name="generate_shot", description="Generate a shot")]
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)
    return definitions, composite


class _RetryingLLM(BaseLLMProvider):
    """One ``generate_shot`` call per with-tools iteration, for *calls* iterations, then an answer."""

    provider_name = "stub"
    context_window = 100_000

    def __init__(self, calls: int, *, change_prompt: bool) -> None:
        self.with_tools_calls = 0
        self._calls = calls
        self._change = change_prompt

    async def chat(self, messages, tools=None, model=None, **kwargs):
        if tools and self.with_tools_calls < self._calls:
            self.with_tools_calls += 1
            prompt = f"take {self.with_tools_calls}" if self._change else "the same shot"
            return ChatResponse(
                content="",
                model=model or "stub",
                tool_calls=[ToolCall(id=f"tc-{self.with_tools_calls}", name="generate_shot", arguments={"prompt": prompt})],
                stop_reason="tool_use",
            )
        return ChatResponse(content="Done.", model=model or "stub")

    async def chat_stream(self, messages, **kwargs):
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.mark.asyncio
async def test_brake_ignores_rejected_generations_the_model_fixed():
    """A generation rejected for its work, retried each time with a changed prompt, is never braked."""
    definitions, composite = _build_rejected_generation_executor()
    llm = _RetryingLLM(calls=6, change_prompt=True)
    messages = [ChatMessage(role="user", content="make the shot")]
    await al.run_agent_loop(
        llm=llm, messages=messages, tools=definitions, executor=composite, max_iterations=12, progressive_tools=False
    )
    assert llm.with_tools_calls == 6, "every fixed retry was offered the tool"
    assert not any(m.role == "system" and "repeated tool failure, stopping" in (m.content or "") for m in messages)


@pytest.mark.asyncio
async def test_brake_stops_the_same_rejected_generation_asked_again():
    """The identical call rejected four times in a row still engages the brake."""
    definitions, composite = _build_rejected_generation_executor()
    llm = _RetryingLLM(calls=8, change_prompt=False)
    messages = [ChatMessage(role="user", content="make the shot")]
    await al.run_agent_loop(
        llm=llm, messages=messages, tools=definitions, executor=composite, max_iterations=12, progressive_tools=False
    )
    assert llm.with_tools_calls == 4, "tools are withdrawn after the fourth identical rejection"
    assert any(m.role == "system" and "repeated tool failure, stopping" in (m.content or "") for m in messages)


@pytest.mark.asyncio
async def test_rejected_generations_asked_differently_are_braked_at_the_cumulative_backstop():
    """A model rewording each paid generation a little is braked once eight in a row came out rejected."""
    definitions, composite = _build_rejected_generation_executor()
    llm = _RetryingLLM(calls=12, change_prompt=True)
    messages = [ChatMessage(role="user", content="make the shot")]
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=definitions,
        executor=composite,
        max_iterations=20,
        progressive_tools=False,
        on_event=on_event,
    )
    assert llm.with_tools_calls == al._OUTCOME_FAILURE_BRAKE_THRESHOLD == 8
    stops = [m.content for m in messages if m.role == "system" and "rejected work, stopping" in (m.content or "")]
    assert len(stops) == 1 and "8 results of 'generate_shot' in a row came out rejected" in stops[0]
    assert any("8 attempts of 'generate_shot' came out rejected" in str(e.get("content", "")) for e in events)


@pytest.mark.asyncio
async def test_a_success_of_the_tool_starts_the_rejected_streak_again():
    """Seven rejected, one made, seven rejected again: never eight in a row, never braked."""
    definitions, composite = _build_rejected_generation_executor(succeeds=frozenset({"take 8"}))
    llm = _RetryingLLM(calls=15, change_prompt=True)
    messages = [ChatMessage(role="user", content="make the shots")]
    await al.run_agent_loop(
        llm=llm, messages=messages, tools=definitions, executor=composite, max_iterations=24, progressive_tools=False
    )
    assert llm.with_tools_calls == 15, "the success in between reset the streak"
    assert not any(m.role == "system" and "stopping" in (m.content or "") for m in messages)


def _build_refusing_executor() -> tuple[list[ToolDefinition], CompositeToolExecutor]:
    """A tool that turns every call down for a validation reason (``refused``), saying what to do instead."""
    from app.tools.base import ToolResult

    async def _edit(prompt: str = "") -> ToolResult:
        return ToolResult(
            tool_call_id="",
            content=f"Nothing was changed: '{prompt}' cannot be applied as written; search first.",
            is_error=True,
            refused=True,
        )

    callable_exec = CallableToolExecutor()
    callable_exec.register("generate_shot", _edit)
    definitions = [ToolDefinition(name="generate_shot", description="Edit a thing")]
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)
    return definitions, composite


@pytest.mark.asyncio
async def test_validation_refusals_never_withdraw_the_tool():
    """Six refusals of differently-worded calls: no repeated-failure nudge, no
    cooldown, no brake - only runtime errors feed those. The tool stays offered."""
    definitions, composite = _build_refusing_executor()
    llm = _RetryingLLM(calls=6, change_prompt=True)
    messages = [ChatMessage(role="user", content="edit it")]
    await al.run_agent_loop(
        llm=llm, messages=messages, tools=definitions, executor=composite, max_iterations=12, progressive_tools=False
    )
    assert llm.with_tools_calls == 6, "every refused call was followed by another offer of the tool"
    assert not any(m.role == "system" and "stopping" in (m.content or "") for m in messages)
    assert not any(m.role == "system" and (m.content or "").startswith(al._REPEATED_FAILURE_NUDGE.split("]", 1)[0]) for m in messages)


@pytest.mark.asyncio
async def test_the_same_refused_call_asked_again_is_still_stopped_by_the_loop_breaker():
    """``refused`` exempts a result from the failure streak, nothing else: the
    identical call asked over and over is refused by the loop breaker from the
    fifth ask, and those refusals do count as failures - the repeated-failure
    nudge withholds the tool, so the turn still ends."""
    definitions, composite = _build_refusing_executor()
    llm = _RetryingLLM(calls=20, change_prompt=False)
    messages = [ChatMessage(role="user", content="edit it")]
    await al.run_agent_loop(
        llm=llm, messages=messages, tools=definitions, executor=composite, max_iterations=30, progressive_tools=False
    )
    breaker = [m.tool_call_id for m in messages if m.role == "tool" and (m.content or "").startswith("[Loop breaker] Refused")]
    assert breaker == ["tc-5", "tc-6"]
    assert any(m.role == "system" and (m.content or "").startswith(al._REPEATED_FAILURE_NUDGE.split("]", 1)[0]) for m in messages)
    assert llm.with_tools_calls == 6, "the tool is withheld once the loop breaker's refusals reach the nudge"


# ---------------------------------------------------------------------------
# P4.2 — wall-clock budget-warning nudge
# ---------------------------------------------------------------------------


class _SlowFirstCallLLM(BaseLLMProvider):
    """First (with-tools) call burns most of the wall-clock budget."""

    provider_name = "stub"
    context_window = 100_000

    def __init__(self, first_call_sleep: float) -> None:
        self.chat_calls = 0
        self._sleep = first_call_sleep

    async def chat(self, messages, tools=None, model=None, **kwargs):
        self.chat_calls += 1
        if self.chat_calls == 1 and tools:
            await asyncio.sleep(self._sleep)
            return ChatResponse(
                content="",
                model=model or "stub",
                tool_calls=[
                    ToolCall(id="tc-1", name="echo_tool", arguments={})
                ],
                stop_reason="tool_use",
            )
        return ChatResponse(content="final answer", model=model or "stub")

    async def chat_stream(self, messages, **kwargs):
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.mark.asyncio
async def test_wall_clock_nudge_injected_at_half_budget(monkeypatch):
    """Once ``_WALL_CLOCK_NUDGE_FRACTION`` of the wall-clock budget (half, in
    production) has elapsed, the one-shot consolidation nudge is injected and
    the user warned (P4.2).

    The fraction is shrunk so the one slow call crosses it with a minute of
    budget left: with a budget barely longer than the call, a loaded runner
    reaches the deadline before the next iteration and the turn ends in the
    forced-final-answer backstop instead of the final answer.
    """
    budget_seconds = 60.0
    first_call_sleep = 0.6
    monkeypatch.setattr(
        al, "_WALL_CLOCK_NUDGE_FRACTION", first_call_sleep / 2 / budget_seconds
    )

    async def _echo() -> str:
        return "ok"

    callable_exec = CallableToolExecutor()
    callable_exec.register("echo_tool", _echo)
    definitions = [ToolDefinition(name="echo_tool", description="echo")]
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)

    llm = _SlowFirstCallLLM(first_call_sleep=first_call_sleep)
    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    messages = [ChatMessage(role="user", content="do the thing")]
    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=definitions,
        executor=composite,
        max_iterations=5,
        progressive_tools=False,
        on_event=_on_event,
        wall_clock_deadline=time.monotonic() + budget_seconds,
    )

    assert "final answer" in (response.content or "")
    nudges = [
        m
        for m in messages
        if m.role == "system" and "time budget warning" in (m.content or "")
    ]
    assert len(nudges) == 1, "the wall-clock nudge must be injected exactly once"
    assert any(
        e.get("status") == "thinking_text"
        and "time budget" in str(e.get("content", ""))
        for e in events
    )


@pytest.mark.asyncio
async def test_wall_clock_nudge_absent_without_deadline():
    """Without a ``wall_clock_deadline`` the nudge machinery stays off."""
    llm = _SlowFirstCallLLM(first_call_sleep=0.0)
    messages = [ChatMessage(role="user", content="hello")]
    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=None,
        executor=None,
        max_iterations=3,
        progressive_tools=False,
    )
    assert not any(
        m.role == "system" and "time budget warning" in (m.content or "")
        for m in messages
    )


# ---------------------------------------------------------------------------
# P4.3 — compaction-state hygiene
# ---------------------------------------------------------------------------


def _state(through_sequence: int) -> dict:
    raw = (
        '{"directives": "- use EUR", "summary": "Analysed Q1 data.", '
        f'"through_sequence": {through_sequence}, "compacted_count": 12}}'
    )
    parsed = parse_compaction_state(raw)
    assert parsed is not None
    return parsed


def test_compaction_state_stale_detection():
    """A watermark above the conversation's max message sequence means
    covered messages were purged — the state is stale (P4.3)."""
    state = _state(through_sequence=40)

    # Healthy: newer messages exist past the watermark.
    assert compaction_state_is_stale(state, max_sequence=45) is False
    # Healthy: compaction just ran, no new message yet.
    assert compaction_state_is_stale(state, max_sequence=40) is False
    # Stale: the newest surviving message is BELOW the watermark.
    assert compaction_state_is_stale(state, max_sequence=12) is True
    # Stale: every message was purged.
    assert compaction_state_is_stale(state, max_sequence=None) is True


def test_compaction_state_stale_on_malformed_watermark():
    """A state without a usable integer watermark can never be anchored to
    the message log — treat it as stale."""
    assert compaction_state_is_stale({}, max_sequence=10) is True
    assert (
        compaction_state_is_stale({"through_sequence": "40"}, max_sequence=50)
        is True
    )
