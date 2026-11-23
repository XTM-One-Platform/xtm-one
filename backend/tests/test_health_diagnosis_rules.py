"""The pure rules of the Quality Analyst's diagnosis (#8259): the summary of one
window of an agent's traces, read from their stored rollups."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.services.health.diagnosis import summarize_window, unstarted_cause


def _trace(status: str = "ok", cause: str | None = None, cost: float | None = 0.01, rollup: dict | None = None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        failure_cause=cause,
        total_cost=cost,
        input_tokens=100,
        output_tokens=50,
        metadata_json={"rollup": rollup} if rollup is not None else None,
        user_id=None,
    )


def _run(cause: str | None, error: str | None = None):
    """A run failed before start, as ``_list_unstarted`` lists it."""
    return SimpleNamespace(
        id=uuid.uuid4(), user_id=None, failure_cause=cause, error_head=(error or "")[: len("deadline exceeded")]
    )


class TestSummarizeWindow:
    def test_figures(self) -> None:
        traces = [
            _trace(rollup={"wall_ms": 1_000, "tools": {"search": {"calls": 2, "errors": 1}}}),
            _trace("error", "tool_timeout", rollup={"wall_ms": 3_000, "tools": {"search": {"calls": 1, "errors": 1}}}),
            _trace("error", None, cost=None),
            _trace(),
        ]
        summary = summarize_window(traces, total=10)
        assert summary["runs"] == 4
        assert summary["runs_in_window"] == 10
        assert summary["sampled"] is True
        assert summary["failed"] == 2
        assert summary["failure_rate_pct"] == 50.0
        assert summary["failure_causes"] == {"tool_timeout": 1, "unrecorded": 1}
        assert summary["tools"] == [{"tool": "search", "calls": 3, "errors": 2, "error_rate_pct": 66.7}]
        assert summary["cost_per_run"] == 0.01
        assert summary["tokens_per_run"] == 150.0
        assert summary["runs_with_rollup"] == 2
        assert summary["percentiles"]["wall_ms"] == {"p50": 2000.0, "p90": 2800.0, "n": 2}

    def test_runs_failed_before_start_count_and_are_served_apart(self) -> None:
        # #1416: 2 traced ok, 1 traced failed, 3 runs failed before start (no trace).
        traces = [_trace(), _trace(), _trace("error", "tool_timeout")]
        unstarted = [
            _run("engine_deadline_exceeded"),
            _run(None, "Deadline exceeded at 2026-11-23T08:00:00+00:00."),
            _run("engine_never_claimed"),
        ]
        summary = summarize_window(traces, total=3, unstarted=unstarted, unstarted_total=3)
        assert (summary["runs"], summary["runs_in_window"], summary["failed"]) == (6, 6, 4)
        assert summary["failure_rate_pct"] == 66.7
        assert summary["failed_before_start"] == 3
        assert summary["failed_before_start_causes"] == {"deadline_before_start": 3}
        assert summary["failure_causes"] == {
            "engine_deadline_exceeded": 1,
            "engine_never_claimed": 1,
            "tool_timeout": 1,
            "unrecorded": 1,
        }
        # Cost and tokens stay per traced run: a run that never started spent nothing measured.
        assert summary["cost_per_run"] == 0.01
        assert summary["sampled"] is False

    def test_a_run_refused_for_another_reason_is_other(self) -> None:
        summary = summarize_window([], total=0, unstarted=[_run("ai_disabled", "AI is disabled.")], unstarted_total=5)
        assert summary["failed_before_start_causes"] == {"other": 1}
        assert (summary["failed_before_start"], summary["runs_in_window"], summary["sampled"]) == (5, 5, True)
        assert unstarted_cause(_run(None, None)) == "other"

    def test_empty_window(self) -> None:
        summary = summarize_window([], total=0)
        assert summary["failed_before_start"] == 0
        assert summary["failed_before_start_causes"] == {}
        assert summary["runs"] == 0
        assert summary["sampled"] is False
        assert summary["failure_rate_pct"] is None
        assert summary["cost_per_run"] is None
        assert summary["tools"] == []
