"""Switch-over decisions on fixed numbers (#8260, B18): the live watch and the change a person made."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.health.flow_switch_over import (
    SWITCH_OVER_PAUSE_REASON,
    LiveMetrics,
    human_change,
    watch_decision,
)

# ── Live watch ─────────────────────────────────────────────────────────────


def test_watch_has_no_verdict_before_enough_runs():
    assert watch_decision(LiveMetrics(4, 0.5, None), {"failure_rate": 0.0})[0] is None


def test_one_failure_among_the_first_runs_is_noise():
    regressed, note = watch_decision(LiveMetrics(5, 0.2, None), {"failure_rate": 0.0})
    assert regressed is False
    assert note == "The flow failed 20 % of its last 5 runs (ceiling 5 %); no regression."
    assert watch_decision(LiveMetrics(5, 0.4, None), {"failure_rate": 0.0})[0] is True


def test_watch_flags_a_failure_spike():
    regressed, note = watch_decision(LiveMetrics(20, 0.10, None), {"failure_rate": 0.0, "cost_per_run": None})
    assert regressed is True
    assert "10 %" in note


def test_watch_tolerates_the_assignments_own_failure_rate():
    assert watch_decision(LiveMetrics(20, 0.10, None), {"failure_rate": 0.08})[0] is False
    assert watch_decision(LiveMetrics(20, 0.15, None), {"failure_rate": 0.08})[0] is True


def test_watch_flags_cost_drift():
    assert watch_decision(LiveMetrics(20, 0.0, 1.25), {"failure_rate": 0.0, "cost_per_run": 1.0})[0] is True
    assert watch_decision(LiveMetrics(20, 0.0, 1.15), {"failure_rate": 0.0, "cost_per_run": 1.0})[0] is False


def test_human_change_detection():
    shadow = SimpleNamespace()
    paused = SimpleNamespace(enabled=False, paused_reason=SWITCH_OVER_PAUSE_REASON)
    active = SimpleNamespace(status="active", version_number=2)
    assert human_change(shadow, paused, active, 2) is None
    assert human_change(shadow, SimpleNamespace(enabled=True, paused_reason=None), active, 2) == "the assignment was resumed"
    assert (
        human_change(shadow, SimpleNamespace(enabled=False, paused_reason="panic_stop"), active, 2)
        == "the assignment was paused for another reason"
    )
    assert human_change(shadow, paused, SimpleNamespace(status="draft", version_number=2), 2) == "the flow was deactivated"
    assert human_change(shadow, paused, SimpleNamespace(status="active", version_number=3), 2) == "the flow was edited"
