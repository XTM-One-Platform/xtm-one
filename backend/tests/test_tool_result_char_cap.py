"""The agent loop binds its tool-result cap around each tool call.

``run_agent_loop`` keeps the first ``max_tool_result_chars`` characters of a
tool result. A tool that pages its own answer (``get_work_item``) reads that
cap through ``tool_result_char_cap()`` during its call, so the offset it names
for "read on" is where the delivered part ends, on any model.
"""

from __future__ import annotations

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools import agent_loop as al
from app.tools.base import (
    CallableToolExecutor,
    ToolCall,
    ToolDefinition,
    limiting_tool_results,
    tool_result_char_cap,
)


class _ScriptedLLM(BaseLLMProvider):
    provider_name = "stub"
    context_window = 100_000

    def __init__(self, script: list[ChatResponse]) -> None:
        self._script = script
        self._idx = 0

    async def chat(self, messages, model=None, tools=None, **kwargs):
        response = self._script[min(self._idx, len(self._script) - 1)]
        self._idx += 1
        return response

    async def chat_stream(self, messages, **kwargs):
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.fixture(autouse=True)
def _quiet_langfuse(monkeypatch):
    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)


def test_no_cap_is_bound_outside_a_loop():
    assert tool_result_char_cap() is None
    with limiting_tool_results(1_234):
        assert tool_result_char_cap() == 1_234
    assert tool_result_char_cap() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [False, True])
async def test_a_tool_reads_the_loops_cap_during_its_call(parallel):
    seen: list[int | None] = []

    async def _probe(**kwargs):
        seen.append(tool_result_char_cap())
        return "ok"

    executor = CallableToolExecutor()
    executor.register("probe", _probe)
    calls = [ToolCall(id="c1", name="probe", arguments={})]
    if parallel:
        calls.append(ToolCall(id="c2", name="probe", arguments={"i": 2}))
    llm = _ScriptedLLM(
        [
            ChatResponse(content="", model="stub", stop_reason="tool_calls", tool_calls=calls),
            ChatResponse(content="done", model="stub"),
        ]
    )
    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=[ToolDefinition(name="probe", description="probe", source="builtin:test")],
        executor=executor,
        max_iterations=3,
        max_tool_result_chars=4_321,
        progressive_tools=False,
    )

    assert seen and all(cap == 4_321 for cap in seen)
    assert tool_result_char_cap() is None
