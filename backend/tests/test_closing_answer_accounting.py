"""The closing answer's usage is counted once.

The forced-final-answer backstop debits its synthesis through ``_merge_usage``,
the one sink behind the run budget, the usage the run reports (root trace,
``AssignmentRun`` token totals) and the legacy token wind-down.  Two of its
exits counted the synthesis wrongly:

* a synthesis cut at ``max_tokens`` was debited before its continuation and
  again at the final exit - counted twice;
* a synthesis too short to keep was replaced by the stronger-prompt retry
  before it was debited - never counted.

Each case below reads every debit in order, and the usage the run returns.
"""

from __future__ import annotations

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools import agent_loop as al
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
)
from app.tools.run_budget import RunBudget

_LONG = "The incident was traced to a failed deploy, and this is the full report. " * 3
_TOOL = {"input_tokens": 2_000, "output_tokens": 150}
_SYNTHESIS = {"input_tokens": 4_000, "output_tokens": 700}
_CONTINUATION = {"input_tokens": 4_800, "output_tokens": 300}
_RETRY = {"input_tokens": 4_300, "output_tokens": 900}


@pytest.fixture(autouse=True)
def _quiet_langfuse(monkeypatch):
    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)


class _ScriptedLLM(BaseLLMProvider):
    provider_name = "stub"
    context_window = 200_000

    def __init__(self, script: list[ChatResponse | BaseException]) -> None:
        self._script = list(script)

    async def chat(self, messages, model=None, tools=None, **kwargs):
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def chat_stream(self, messages, **kwargs):
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


class _RecordingBudget(RunBudget):
    """A real budget that keeps every debit, in order."""

    def __init__(self) -> None:
        # One tool call: the second gate sends the run to the backstop.
        super().__init__(max_tool_calls=1)
        self.debits: list[dict] = []

    def record_llm_usage(self, usage: dict | None) -> None:
        self.debits.append(dict(usage or {}))
        super().record_llm_usage(usage)


def _answer(content: str, usage: dict, stop_reason: str = "end_turn") -> ChatResponse:
    return ChatResponse(content=content, model="stub", stop_reason=stop_reason, usage=dict(usage))


def _tool_round() -> ChatResponse:
    return ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_use",
        tool_calls=[ToolCall(id="c1", name="lookup", arguments={"q": "deploys"})],
        usage=dict(_TOOL),
    )


async def _run(script) -> tuple[ChatResponse, _RecordingBudget]:
    executor = CallableToolExecutor()

    async def _lookup(**_kwargs) -> str:
        return "found it"

    executor.register("lookup", _lookup)
    composite = CompositeToolExecutor()
    composite.add_tools(
        [ToolDefinition(name="lookup", description="lookup", source="custom:test")], executor
    )
    budget = _RecordingBudget()
    response = await al.run_agent_loop(
        llm=_ScriptedLLM(script),
        messages=[ChatMessage(role="user", content="Investigate the incident and report.")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        max_tokens=1_024,
        progressive_tools=False,
        budget=budget,
    )
    return response, budget


def _total(*usages: dict) -> dict:
    return {
        "input_tokens": sum(u["input_tokens"] for u in usages),
        "output_tokens": sum(u["output_tokens"] for u in usages),
    }


def _reported(response: ChatResponse) -> dict:
    return {k: response.usage[k] for k in ("input_tokens", "output_tokens")}


@pytest.mark.asyncio
async def test_a_cut_synthesis_is_counted_once_with_its_continuation():
    response, budget = await _run(
        [
            _tool_round(),
            _answer(_LONG, _SYNTHESIS, stop_reason="max_tokens"),
            _answer(" And the rest of it.", _CONTINUATION),
        ]
    )

    # Before the fix: [_TOOL, _SYNTHESIS, _CONTINUATION, _SYNTHESIS].
    assert budget.debits == [_TOOL, _SYNTHESIS, _CONTINUATION]
    assert _reported(response) == _total(_TOOL, _SYNTHESIS, _CONTINUATION)
    assert response.content.endswith("And the rest of it.")


@pytest.mark.asyncio
async def test_the_first_synthesis_is_counted_when_the_retry_replaces_it():
    response, budget = await _run(
        [_tool_round(), _answer("Too short.", _SYNTHESIS), _answer(_LONG, _RETRY)]
    )

    # Before the fix: [_TOOL, _RETRY] - the billed first attempt was lost.
    assert budget.debits == [_TOOL, _SYNTHESIS, _RETRY]
    assert _reported(response) == _total(_TOOL, _SYNTHESIS, _RETRY)
    assert response.content.strip() == _LONG.strip()


@pytest.mark.asyncio
async def test_a_cut_retry_is_counted_once_after_the_first_synthesis():
    response, budget = await _run(
        [
            _tool_round(),
            _answer("Too short.", _SYNTHESIS),
            _answer(_LONG, _RETRY, stop_reason="max_tokens"),
            _answer(" And the rest of it.", _CONTINUATION),
        ]
    )

    assert budget.debits == [_TOOL, _SYNTHESIS, _RETRY, _CONTINUATION]
    assert _reported(response) == _total(_TOOL, _SYNTHESIS, _RETRY, _CONTINUATION)


# ── Paths that already counted correctly: unchanged ──────────────────────


@pytest.mark.asyncio
async def test_a_synthesis_that_is_kept_is_counted_once():
    response, budget = await _run([_tool_round(), _answer(_LONG, _SYNTHESIS)])

    assert budget.debits == [_TOOL, _SYNTHESIS]
    assert _reported(response) == _total(_TOOL, _SYNTHESIS)


@pytest.mark.asyncio
async def test_a_failed_retry_still_counts_the_first_synthesis_once():
    response, budget = await _run(
        [_tool_round(), _answer("Too short.", _SYNTHESIS), RuntimeError("provider down")]
    )

    assert budget.debits == [_TOOL, _SYNTHESIS]
    assert _reported(response) == _total(_TOOL, _SYNTHESIS)
