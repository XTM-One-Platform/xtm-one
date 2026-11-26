"""An event-driven run works in a bounded window (#2430).

A single-case "Case opened or reopened" run of the Loop Orchestrator on a
1M-token model re-sent ~120k input tokens per action: every working-context
budget of the loop (the live tool-result budget, the unwritten-turn
protection, the compaction trigger) was scaled on the model's window, and a
tool-call-only loop never releases the turn protection, so up to four live
budgets of 160,000 characters stayed whole on every request.

``run_agent_loop(working_window_tokens=)`` computes those budgets on a smaller
window; the assignment engine passes ``AGENT_LOOP_EVENT_RUN_WORKING_WINDOW_TOKENS``
for platform events, integration events and webhooks. Pinned here: the
request size of a 46-call loop on a 1M window stays bounded after the first
calls, the latest batch is always sent whole, and runs of other trigger types
keep the model's window.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import settings
from app.llm.base import ChatMessage, ChatResponse
from app.services import assignment_dispatch
from app.tools import agent_loop as al
from tests.test_tool_call_dedup import (
    _quiet_langfuse,  # noqa: F401 - pytest fixture
    _ScriptedLLM,
    _tool_call_response,
)
from tests.test_turn_pruning_and_recall import _call, _reader_executor

_CALLS = 46
_RESULT_CHARS = 12_000
_WORKING_WINDOW = 128_000


class _BigWindowLLM(_ScriptedLLM):
    """A scripted 1M-token model that records every request's size."""

    context_window = 1_000_000

    def __init__(self, script: list[ChatResponse]) -> None:
        super().__init__(script)
        self.request_chars: list[int] = []
        self.request_tools: list[dict[str, str]] = []

    async def chat(self, messages: Any, model: Any = None, tools: Any = None, **kwargs: Any) -> Any:  # type: ignore[override]
        self.request_chars.append(sum(len(m.content or "") for m in messages))
        self.request_tools.append({m.tool_call_id or "": m.content or "" for m in messages if m.role == "tool"})
        return await super().chat(messages, model=model, tools=tools, **kwargs)  # type: ignore[no-untyped-call]


async def _single_case_loop(**kwargs: Any) -> _BigWindowLLM:
    """46 tool-call-only steps of one 12,000-character read each, then the answer."""
    script = [_tool_call_response(_call(f"c{i}", i)) for i in range(_CALLS)]
    script.append(ChatResponse(content="Case worked: owner, validation and ticket written.", model="stub"))
    llm = _BigWindowLLM(script)
    composite, executed = _reader_executor(payload_chars=_RESULT_CHARS)
    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="work the case")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=80,
        progressive_tools=False,
        **kwargs,
    )
    assert len(executed) == _CALLS
    return llm


def test_the_working_window_is_the_smaller_of_the_two() -> None:
    assert al._working_window(1_000_000, 128_000) == 128_000
    assert al._working_window(64_000, 128_000) == 64_000, "never larger than the model's"
    assert al._working_window(1_000_000, None) == 1_000_000
    assert al._working_window(1_000_000, 0) == 1_000_000


def test_the_turn_protection_holds_one_live_budget_under_a_working_window() -> None:
    live = al._compute_live_tool_result_budget(_WORKING_WINDOW)
    assert al._turn_protection_budget(1_000_000, _WORKING_WINDOW) == live
    # Unchanged without one: four live budgets of the model's window.
    assert al._turn_protection_budget(1_000_000, None) == 4 * al._compute_live_tool_result_budget(1_000_000)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_quiet_langfuse")
async def test_a_long_tool_call_only_loop_on_a_1m_window_keeps_its_requests_bounded() -> None:
    """The acceptance test of #2430: after the first calls, no request of a
    46-call loop carries more than the working window's live budget of tool
    results plus the batch it shows and the stubs of the cleared ones."""
    bounded = await _single_case_loop(working_window_tokens=_WORKING_WINDOW)
    unbounded = await _single_case_loop()

    live = al._compute_live_tool_result_budget(_WORKING_WINDOW)
    # One live budget kept whole (the protection), one live budget the
    # ordinary pass may still keep, the unseen batch and ~300 chars per stub.
    ceiling = 2 * live + _RESULT_CHARS + _CALLS * 400
    steady = bounded.request_chars[10:]
    assert max(steady) <= ceiling, (max(steady), ceiling)
    # The premise: on the model's window the same loop re-sends everything.
    assert unbounded.request_chars[-1] > _CALLS * _RESULT_CHARS * 0.9, unbounded.request_chars[-1]
    assert sum(bounded.request_chars) * 3 < sum(unbounded.request_chars), (
        sum(bounded.request_chars),
        sum(unbounded.request_chars),
    )
    # The batch a request exists to show is always whole.
    for i in range(1, _CALLS):
        assert bounded.request_tools[i][f"c{i - 1}"].startswith(f"page {i - 1} xxx")


@pytest.mark.parametrize(
    ("trigger_type", "expected"),
    [
        ("platform_event", 128_000),
        ("integration_event", 128_000),
        ("webhook", 128_000),
        ("work_item", None),
        ("schedule", None),
        ("manual", None),
    ],
)
def test_only_event_driven_runs_get_the_working_window(
    monkeypatch: pytest.MonkeyPatch, trigger_type: str, expected: int | None
) -> None:
    monkeypatch.setattr(settings, "agent_loop_event_run_working_window_tokens", 128_000)
    assert assignment_dispatch.event_run_working_window({"type": trigger_type}) == expected


def test_zero_switches_the_working_window_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "agent_loop_event_run_working_window_tokens", 0)
    assert assignment_dispatch.event_run_working_window({"type": "platform_event"}) is None
    assert assignment_dispatch.event_run_working_window(None) is None
