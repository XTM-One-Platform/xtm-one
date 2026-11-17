"""A run still making progress at its cap keeps going in blocks."""

from __future__ import annotations

import inspect
import time

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.services import agent_chat, assignment_engine, general_chat
from app.tools import agent_loop
from app.tools.agent_loop import run_agent_loop
from app.tools.base import ToolCall, ToolDefinition, ToolResult

_TOOL = ToolDefinition(
    name="read_page",
    description="Read one page",
    input_schema={"type": "object", "properties": {"n": {"type": "integer"}}},
)


class _WorkingLLM(BaseLLMProvider):
    """Calls the tool while it is offered, answers once tools are withdrawn.

    ``rounds`` tool rounds then a final answer; ``same_arguments`` repeats one
    call instead of reading a new page each time.
    """

    provider_name = "stub"
    context_window = 200_000

    def __init__(self, *, rounds: int = 10_000, same_arguments: bool = False) -> None:
        self.rounds = rounds
        self.same_arguments = same_arguments
        self.tool_rounds = 0
        self.calls = 0

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        self.calls += 1
        if not tools:
            return ChatResponse(content="wrapped up", model="stub")
        if self.tool_rounds >= self.rounds:
            return ChatResponse(content="final answer", model="stub")
        self.tool_rounds += 1
        n = 1 if self.same_arguments else self.tool_rounds
        return ChatResponse(
            content="",
            model="stub",
            tool_calls=[ToolCall(id=f"tc-{self.calls}", name="read_page", arguments={"n": n})],
        )

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        yield "wrapped up"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


class _Executor:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def execute_tool(self, tool_call):
        if self.fail:
            return ToolResult(tool_call_id=tool_call.id, content="Error: upstream down", is_error=True)
        return ToolResult(tool_call_id=tool_call.id, content=f"page {tool_call.arguments['n']}")


async def _run(llm: _WorkingLLM, *, fail: bool = False, **kwargs):
    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="read everything")],
        tools=[_TOOL],
        executor=_Executor(fail=fail),
        on_event=_on_event,
        **kwargs,
    )
    return response, events


def _extensions(events: list[dict]) -> list[str]:
    return [
        e["content"]
        for e in events
        if e.get("status") == "thinking_text" and "Still making progress" in str(e.get("content"))
    ]


def _wind_downs(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("status") == "wind_down"]


async def test_without_a_ceiling_the_cap_ends_the_run():
    llm = _WorkingLLM(rounds=20)
    response, events = await _run(llm, max_iterations=6)
    assert response.content == "wrapped up"
    assert llm.tool_rounds < 6
    assert _wind_downs(events)
    assert _extensions(events) == []


async def test_a_run_that_keeps_working_gets_blocks_until_it_is_done():
    llm = _WorkingLLM(rounds=20)
    response, events = await _run(llm, max_iterations=6, extend_iterations_to=30)
    assert response.content == "final answer"
    assert llm.tool_rounds == 20
    assert _wind_downs(events) == []
    assert len(_extensions(events)) >= 3


async def test_the_ceiling_bounds_the_extensions():
    llm = _WorkingLLM()
    response, events = await _run(llm, max_iterations=6, extend_iterations_to=12)
    assert response.content == "wrapped up"
    assert 6 < llm.tool_rounds < 12
    assert len(_extensions(events)) == 1
    assert _wind_downs(events)[-1]["max_iterations"] == 12


async def test_a_block_whose_every_call_failed_is_not_extended():
    llm = _WorkingLLM()
    response, events = await _run(llm, fail=True, max_iterations=6, extend_iterations_to=30)
    assert response.content == "wrapped up"
    assert llm.tool_rounds < 6
    assert _extensions(events) == []


async def test_a_block_the_loop_breaker_refused_in_is_not_extended():
    llm = _WorkingLLM(same_arguments=True)
    response, events = await _run(llm, max_iterations=12, extend_iterations_to=60)
    assert response.content == "wrapped up"
    assert llm.tool_rounds < 12
    assert _extensions(events) == []


async def test_no_extension_when_the_wall_clock_has_no_room_for_another_wind_down(monkeypatch):
    monkeypatch.setattr(agent_loop, "_EXTENSION_WIND_DOWN_ROUNDS", 10**12)
    llm = _WorkingLLM()
    response, events = await _run(
        llm,
        max_iterations=6,
        extend_iterations_to=30,
        wall_clock_deadline=time.monotonic() + 600,
    )
    assert response.content == "wrapped up"
    assert llm.tool_rounds < 6
    assert _extensions(events) == []


def test_the_chat_services_and_assignments_opt_in_on_the_runs_of_their_own():
    chat_source = inspect.getsource(agent_chat.run_agent_chat)
    reach = chat_source.split("_iteration_reach = (", 1)[1].split("else effective_max_iterations", 1)[0]
    for condition in (
        "meter_quota",
        "not _evaluation_chain",
        "max_iterations_override is None",
        "not is_anonymous_user",
    ):
        assert condition in reach, condition
    assert 'loop_kwargs["extend_iterations_to"] = _iteration_reach' in chat_source
    assert "chat_loop_timeout_within(\n                _iteration_reach," in chat_source

    ga_source = inspect.getsource(general_chat.run_general_chat)
    assert "extend_iterations_to=ga_iteration_reach" in ga_source
    assert "chat_loop_timeout = max(300, ga_iteration_reach * 60)" in ga_source

    engine_source = inspect.getsource(assignment_engine.execute_assignment_run)
    assert 'loop_kwargs["extend_iterations_to"] = _iteration_reach' in engine_source
    assert "not assignment.dry_run" in engine_source
