"""A run its deadline left no model output is a failure, never a completion (#1420).

A run (assignment or delegated task) that starts late, with less of its
deadline left than one model call needs, goes straight to the forced-answer
shortcut: its answer is the canned "ran out of time" notice and no model wrote
a token of it. It used to end ``completed``, so supervisors, ``run.failed``
listeners and quality signals counted it as work. It now fails as the
deadline's timeout (``engine_deadline_exceeded``, free credit, ``run.failed``,
the parent woken on a failure). A deadline stop after real work keeps its
partial result and its stop reason, and quality signals count it apart.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from app.llm.base import ChatResponse
from app.services.health.diagnosis import summarize_window
from app.tools.agent_loop import (
    DEADLINE_STOP_REASON,
    FORCED_ANSWER_TIMEOUT_MESSAGE,
    deadline_stop_without_output,
)
from tests.agentic_gate_stub import stub_agentic_gate
from tests.test_execution_deadline_bound import _run_task


@pytest.fixture(autouse=True)
def _agentic_ai_available(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    stub_agentic_gate(monkeypatch)
    yield


def _response(stop_reason: str, usage: dict[str, Any] | None = None) -> ChatResponse:
    return ChatResponse(
        content=FORCED_ANSWER_TIMEOUT_MESSAGE,
        model="m",
        usage=usage or {},
        stop_reason=stop_reason,
    )


# -- The rule ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("stop_reason", "usage", "expected"),
    [
        (DEADLINE_STOP_REASON, {}, True),
        (DEADLINE_STOP_REASON, {"input_tokens": 1200, "output_tokens": 0}, True),
        (DEADLINE_STOP_REASON, {"input_tokens": 1200, "output_tokens": 340}, False),
        (DEADLINE_STOP_REASON, {"prompt_tokens": 900, "completion_tokens": 12}, False),
        ("end_turn", {}, False),
        ("max_iterations", {}, False),
    ],
    ids=[
        "deadline-nothing-called",
        "deadline-a-call-wrote-nothing",
        "deadline-after-real-work",
        "deadline-after-work-openai-keys",
        "a-clean-answer",
        "the-iteration-cap",
    ],
)
def test_a_deadline_stop_without_output_is_read_off_the_usage(
    stop_reason: str, usage: dict[str, Any], expected: bool
) -> None:
    assert deadline_stop_without_output(_response(stop_reason, usage)) is expected


def test_no_response_is_not_a_deadline_stop() -> None:
    assert deadline_stop_without_output(None) is False


# -- The assignment run ------------------------------------------------------


def test_an_assignment_run_with_no_model_output_fails_as_the_deadlines_timeout() -> None:
    """Raised before the run is marked completed, on the failure path that
    classifies ``DeadlineCutTimeoutError`` as ``engine_deadline_exceeded``,
    credits it free and raises ``run.failed``."""
    from app.services import assignment_engine

    source = inspect.getsource(assignment_engine.execute_assignment_run)
    check_at = source.index("if deadline_stop_without_output(loop_result):")
    raise_at = source.index("raise DeadlineCutTimeoutError(", check_at)
    completed_at = source.index('run.status = "completed"', check_at)
    assert check_at < raise_at < completed_at
    # Its usage is still accounted by the failure path.
    assert source.index("_finished_loop = loop_result", check_at) < raise_at
    assert "if isinstance(e, DeadlineCutTimeoutError)" in source
    assert "_free_credit = isinstance(e, DeadlineCutTimeoutError) or (" in source


# -- The delegated task ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_delegated_task_with_no_model_output_fails_and_wakes_its_parent_on_a_failure() -> None:
    async def _chat(*_args: Any, **kwargs: Any) -> tuple[str, None]:
        kwargs["loop_outcome_out"]["stop_reason"] = DEADLINE_STOP_REASON
        kwargs["loop_outcome_out"]["deadline_stop_without_output"] = True
        return FORCED_ANSWER_TIMEOUT_MESSAGE, None

    outcome, task = await _run_task(timeout_seconds=60, deadline_at=None, run_chat=_chat)

    assert outcome.status == "failed"
    assert outcome.error.startswith("Task stopped by its deadline before any model output")
    assert task.status == "failed"
    assert task.result != FORCED_ANSWER_TIMEOUT_MESSAGE


@pytest.mark.asyncio
async def test_a_delegated_task_stopped_by_its_deadline_after_work_keeps_its_answer() -> None:
    async def _chat(*_args: Any, **kwargs: Any) -> tuple[str, None]:
        kwargs["loop_outcome_out"]["stop_reason"] = DEADLINE_STOP_REASON
        kwargs["loop_outcome_out"]["deadline_stop_without_output"] = False
        return "Three of the five alerts are triaged.", None

    outcome, task = await _run_task(timeout_seconds=60, deadline_at=None, run_chat=_chat)

    assert outcome.status == "completed"
    assert task.status == "completed"


def test_the_chat_turn_hands_the_flag_to_its_caller() -> None:
    from app.services import agent_chat

    source = inspect.getsource(agent_chat)
    assert 'loop_outcome_out["deadline_stop_without_output"] = (' in source


# -- Quality signals ---------------------------------------------------------


def _trace(status: str = "ok", stop: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        failure_cause=None,
        total_cost=0.01,
        input_tokens=10,
        output_tokens=5,
        metadata_json={"agent_stop_reason": stop} if stop else None,
        user_id=None,
    )


def test_quality_signals_count_deadline_stops_apart_from_clean_successes() -> None:
    traces = [
        _trace(),
        _trace(stop=DEADLINE_STOP_REASON),
        _trace(stop="max_iterations"),
        _trace(stop="end_turn"),
        _trace("error", stop=DEADLINE_STOP_REASON),
    ]
    summary = summarize_window(traces, total=5)

    assert summary["failed"] == 1
    assert summary["partial"] == 2
    assert summary["partial_rate_pct"] == 40.0
    assert summary["partial_stop_reasons"] == {DEADLINE_STOP_REASON: 1, "max_iterations": 1}


def test_a_window_with_no_partial_run_reads_zero() -> None:
    summary = summarize_window([_trace()], total=1)
    assert summary["partial"] == 0
    assert summary["partial_stop_reasons"] == {}
