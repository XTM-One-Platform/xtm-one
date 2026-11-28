"""A run that ends on one of its limits says so.

The agent loop answers even when it runs out of iterations or of wall clock:
the tools are taken away and the model summarises what it has. That answer
used to come back as an ordinary ``end_turn``, so an assignment run that hit
its cap was recorded as a clean success and the person who configured the
agent could not tell "finished" from "stopped at the limit". The loop now
names the limit in ``stop_reason`` and the run keeps it.
"""

from __future__ import annotations

import time

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools import agent_loop
from app.tools.agent_loop import (
    BUDGET_EXHAUSTED_STOP_REASON,
    DEADLINE_STOP_REASON,
    FORCED_ANSWER_TIMEOUT_MESSAGE,
    ITERATION_LIMIT_STOP_REASON,
    _with_limit_stop,
    run_agent_loop,
    unfinished_stop_reason,
)
from app.tools.base import ToolCall, ToolDefinition, ToolResult
from tests.agentic_gate_stub import stub_agentic_gate

_TOOL = ToolDefinition(
    name="read_page",
    description="Read one page",
    input_schema={"type": "object", "properties": {"n": {"type": "integer"}}},
)


class _LLM(BaseLLMProvider):
    """Calls the tool for ``rounds`` rounds while it is offered, then answers.

    ``rounds=0`` answers straight away. Once the tools are withdrawn it answers
    "wrapped up" (the wind-down summary) - or nothing when ``silent_when_stripped``.
    """

    provider_name = "stub"
    context_window = 200_000

    def __init__(self, *, rounds: int, silent_when_stripped: bool = False) -> None:
        self.rounds = rounds
        self.silent_when_stripped = silent_when_stripped
        self.tool_rounds = 0
        self.calls = 0

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        self.calls += 1
        if not tools:
            if self.silent_when_stripped:
                return ChatResponse(content="", model="stub")
            return ChatResponse(content="wrapped up", model="stub")
        if self.tool_rounds >= self.rounds:
            return ChatResponse(content="final answer", model="stub")
        self.tool_rounds += 1
        return ChatResponse(
            content="",
            model="stub",
            tool_calls=[
                ToolCall(id=f"tc-{self.calls}", name="read_page", arguments={"n": self.tool_rounds})
            ],
        )

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        yield "wrapped up"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


class _Executor:
    async def execute_tool(self, tool_call):
        return ToolResult(tool_call_id=tool_call.id, content=f"page {tool_call.arguments['n']}")


async def _run(llm: _LLM, **kwargs) -> ChatResponse:
    return await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="read everything")],
        tools=[_TOOL],
        executor=_Executor(),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The loop names the limit it ended on
# ---------------------------------------------------------------------------


async def test_a_run_that_finishes_on_its_own_ends_on_end_turn():
    llm = _LLM(rounds=2)
    response = await _run(llm, max_iterations=10)
    assert response.content == "final answer"
    assert response.stop_reason == "end_turn"
    assert unfinished_stop_reason(response) is None


async def test_a_run_whose_tools_are_withdrawn_at_the_cap_ends_on_the_iteration_limit():
    llm = _LLM(rounds=10_000)
    response = await _run(llm, max_iterations=6)
    # The wind-down summary is still the answer the run delivers...
    assert response.content == "wrapped up"
    # ...but it is not passed off as the finished task.
    assert response.stop_reason == ITERATION_LIMIT_STOP_REASON
    assert unfinished_stop_reason(response) == ITERATION_LIMIT_STOP_REASON


async def test_a_run_that_answers_nothing_at_the_cap_ends_on_the_iteration_limit():
    """The tool-free iterations at the cap come back empty, so the loop
    reaches the forced-final-answer backstop: the limit rides through it."""
    llm = _LLM(rounds=10_000, silent_when_stripped=True)
    response = await _run(llm, max_iterations=6)
    assert response.stop_reason == ITERATION_LIMIT_STOP_REASON


async def test_a_run_out_of_wall_clock_ends_on_the_deadline():
    llm = _LLM(rounds=10_000)
    response = await _run(llm, max_iterations=10, wall_clock_deadline=time.monotonic())
    assert response.content.startswith(FORCED_ANSWER_TIMEOUT_MESSAGE)
    assert response.stop_reason == DEADLINE_STOP_REASON
    assert llm.calls == 0


async def test_the_extended_cap_is_still_reported_as_the_iteration_limit():
    """A run that keeps working gets blocks up to its ceiling; at the
    ceiling it ends on the iteration limit like any capped run."""
    llm = _LLM(rounds=10_000)
    response = await _run(llm, max_iterations=6, extend_iterations_to=12)
    assert response.content == "wrapped up"
    assert response.stop_reason == ITERATION_LIMIT_STOP_REASON


# ---------------------------------------------------------------------------
# What counts as unfinished, and what the limit never overwrites
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stop_reason", "unfinished"),
    [
        ("end_turn", None),
        ("tool_use", None),
        ("finalize_tool", None),
        ("max_tokens", None),
        (None, None),
        (ITERATION_LIMIT_STOP_REASON, ITERATION_LIMIT_STOP_REASON),
        (DEADLINE_STOP_REASON, DEADLINE_STOP_REASON),
        (BUDGET_EXHAUSTED_STOP_REASON, BUDGET_EXHAUSTED_STOP_REASON),
        ("timeout", "timeout"),
        ("waiting_on_tasks", "waiting_on_tasks"),
    ],
)
def test_unfinished_stop_reason(stop_reason, unfinished):
    response = ChatResponse(content="x", model="m")
    response.stop_reason = stop_reason
    assert unfinished_stop_reason(response) == unfinished


def test_unfinished_stop_reason_of_no_response_is_none():
    assert unfinished_stop_reason(None) is None


@pytest.mark.parametrize(
    "kept", ["max_tokens", BUDGET_EXHAUSTED_STOP_REASON, "timeout", "waiting_on_tasks", "finalize_tool"]
)
def test_the_limit_never_overwrites_a_reason_the_answer_carries(kept):
    response = ChatResponse(content="x", model="m", stop_reason=kept)
    assert _with_limit_stop(response, ITERATION_LIMIT_STOP_REASON).stop_reason == kept


@pytest.mark.parametrize("plain", ["end_turn", None])
def test_the_limit_replaces_a_plain_end(plain):
    response = ChatResponse(content="x", model="m")
    response.stop_reason = plain
    assert _with_limit_stop(response, DEADLINE_STOP_REASON).stop_reason == DEADLINE_STOP_REASON


def test_no_limit_leaves_the_answer_alone():
    response = ChatResponse(content="x", model="m")
    assert _with_limit_stop(response, None).stop_reason == "end_turn"


def test_every_runtime_stop_reason_of_the_loop_is_classified():
    """The loop's own non-finished stop reasons are all unfinished ones."""
    assert {
        agent_loop.ITERATION_LIMIT_STOP_REASON,
        agent_loop.DEADLINE_STOP_REASON,
        agent_loop.BUDGET_EXHAUSTED_STOP_REASON,
        agent_loop.DEGENERATE_REPETITION_STOP_REASON,
        "timeout",
        "waiting_on_tasks",
        "schema_violation",
    } == agent_loop.UNFINISHED_STOP_REASONS


# ---------------------------------------------------------------------------
# The assignment run keeps the reason
# ---------------------------------------------------------------------------


@pytest.fixture
def _agentic_ai_available(monkeypatch):
    stub_agentic_gate(monkeypatch)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_agentic_ai_available")
@pytest.mark.parametrize(
    "stop_reason", [ITERATION_LIMIT_STOP_REASON, DEADLINE_STOP_REASON, "timeout"]
)
async def test_a_run_that_stopped_on_a_limit_completes_flagged(monkeypatch, stop_reason):
    from tests.test_assignment_stale_reference import SUMMARY, _drive_run

    run, _session, _mocks = await _drive_run(
        monkeypatch, report_integration_id=None, loop_stop_reason=stop_reason
    )

    # The answer is kept and the run is not a failure...
    assert run.status == "completed", run.error
    assert run.result_summary == SUMMARY
    assert run.failure_cause is None
    # ...but the record says why it stopped before the task was done.
    assert run.stop_reason == stop_reason


@pytest.mark.asyncio
@pytest.mark.usefixtures("_agentic_ai_available")
@pytest.mark.parametrize("stop_reason", ["end_turn", "finalize_tool"])
async def test_a_run_that_finished_carries_no_stop_reason(monkeypatch, stop_reason):
    from tests.test_assignment_stale_reference import _drive_run, _engine_objects

    engine_objects = _engine_objects()
    # A retried run that finishes is not left flagged by its earlier attempt.
    engine_objects[0].stop_reason = ITERATION_LIMIT_STOP_REASON
    run, _session, _mocks = await _drive_run(
        monkeypatch,
        report_integration_id=None,
        loop_stop_reason=stop_reason,
        engine_objects=engine_objects,
    )

    assert run.status == "completed", run.error
    assert run.stop_reason is None
