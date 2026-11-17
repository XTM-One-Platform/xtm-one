"""The pure rules of the Quality Analyst's diagnosis (#8259): the summary of one
window of an agent's traces, read from their stored rollups."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.services.concierge.diagnosis import summarize_window


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

    def test_empty_window(self) -> None:
        summary = summarize_window([], total=0)
        assert summary["runs"] == 0
        assert summary["sampled"] is False
        assert summary["failure_rate_pct"] is None
        assert summary["cost_per_run"] is None
        assert summary["tools"] == []
