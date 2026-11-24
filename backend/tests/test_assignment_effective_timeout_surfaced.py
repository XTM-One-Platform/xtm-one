"""The effective assignment timeout is shown, and a run it stopped says so (#1695)."""

import pytest

from app.services import assignment_engine
from app.services.assignment_engine import (
    AssignmentTimeoutError,
    DeadlineCutTimeoutError,
    _apply_iteration_timeout_floor,
    _loop_timeout_error,
    describe_effective_timeout,
)


def test_the_iteration_floor_is_named_with_its_value() -> None:
    line = describe_effective_timeout(1800, 150, 120)
    assert line.startswith("3000s (raised to the iteration floor, max_iterations 150 x 20s)")
    assert "plus 120s grace" in line
    assert "stop_reason timeout" in line


def test_the_static_floor_is_named() -> None:
    assert describe_effective_timeout(300, 10, 120).startswith("600s (raised to the 600s static floor)")


def test_a_stored_value_above_the_floor_is_kept() -> None:
    assert describe_effective_timeout(3600, 25, 120).startswith("3600s (the stored value, above the floor)")


def test_a_disabled_timeout_has_no_bound() -> None:
    assert describe_effective_timeout(0, 50, 120) == "no wall-clock bound (timeout disabled)"


@pytest.mark.parametrize(("stored", "iterations"), [(1800, 150), (300, 10), (3600, 25), (None, 50), (600, 30)])
def test_the_described_value_is_the_one_the_engine_applies(stored: int | None, iterations: int) -> None:
    applied = _apply_iteration_timeout_floor(
        float(stored if stored is not None else assignment_engine._UNSET_TIMEOUT_FALLBACK_SECONDS),
        iterations,
        "run",
        stored,
    )
    assert applied is not None
    assert describe_effective_timeout(stored, iterations, 0).startswith(f"{int(applied)}s ")


def test_the_budget_firing_is_a_timeout_stop() -> None:
    error = _loop_timeout_error(3000.0, 3000.0, 3000.4, 150, deadline_cut=False)
    assert type(error) is AssignmentTimeoutError
    assert error.run_stop_reason == "timeout"


def test_a_cut_bound_firing_is_a_deadline_stop() -> None:
    error = _loop_timeout_error(600.0, 3000.0, 600.2, 150, deadline_cut=True)
    assert isinstance(error, DeadlineCutTimeoutError)
    assert error.run_stop_reason == "deadline"


def test_a_setup_timeout_is_not_a_timeout_stop() -> None:
    error = _loop_timeout_error(3000.0, 3000.0, 12.0, 150, deadline_cut=False)
    assert type(error) is TimeoutError
    assert getattr(error, "run_stop_reason", None) is None


def test_the_failed_run_records_the_stop_reason() -> None:
    import inspect

    source = inspect.getsource(assignment_engine.execute_assignment_run)
    failed_at = source.index('run.status = "failed"\n            run.error = error_msg')
    stop_at = source.index('getattr(e, "run_stop_reason", None)', failed_at)
    assert stop_at - failed_at < 400
