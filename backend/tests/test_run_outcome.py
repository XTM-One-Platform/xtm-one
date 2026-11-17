"""The run outcome and the member verdict: one rule for the fleet's
health badge, its activity feed and the run list."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.autonomous import STALE_ASSIGNMENT_CLEANUP_ERROR_PREFIX
from app.services.consult_watchdog import (
    CONSULT_CANCELLED_ERROR,
    CONSULT_ROLLOUT_ERROR,
    ConsultCutError,
)
from app.services.orchestration_adapters.assignment import WORKER_GONE_DEAD_LETTER_ERROR
from app.services.run_outcome import (
    HEALTH_RECENT_RUNS,
    HEALTH_WINDOW,
    OUTCOME_ACTIVE,
    OUTCOME_FAILED,
    OUTCOME_INTERRUPTED,
    OUTCOME_OK,
    OUTCOME_OTHER,
    RunFact,
    is_interruption,
    member_verdict,
    run_outcome,
)

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
ROLLOUT_CUT = str(ConsultCutError("child run 1", "its server process is stopping (stop, SIGTERM)"))
STALE_CLEANUP = f"{STALE_ASSIGNMENT_CLEANUP_ERROR_PREFIX} The run sat in 'running' for ~900s because it was interrupted."


def _runs(*specs: tuple[str, str | None], step: timedelta = timedelta(minutes=10)) -> list[RunFact]:
    """Runs newest first, *step* apart, from ``(status, error)`` pairs."""
    return [RunFact(created_at=NOW - step * i, status=s, error=e) for i, (s, e) in enumerate(specs)]


def test_every_platform_interruption_error_is_recognised() -> None:
    for error in (ROLLOUT_CUT, CONSULT_ROLLOUT_ERROR, CONSULT_CANCELLED_ERROR, STALE_CLEANUP, WORKER_GONE_DEAD_LETTER_ERROR):
        assert is_interruption(error), error
    assert not is_interruption(None)
    assert not is_interruption("")
    assert not is_interruption("Tool call failed: 500 from the GitHub API")


def test_a_run_outcome_reads_its_status_then_its_error() -> None:
    assert run_outcome("completed", None) == OUTCOME_OK
    assert run_outcome("failed", "context overflow") == OUTCOME_FAILED
    assert run_outcome("failed", None) == OUTCOME_FAILED
    assert run_outcome("failed", ROLLOUT_CUT) == OUTCOME_INTERRUPTED
    assert run_outcome("running", None) == OUTCOME_ACTIVE
    assert run_outcome("awaiting_human", None) == OUTCOME_ACTIVE
    assert run_outcome("cancelled", None) == OUTCOME_OTHER
    assert run_outcome("skipped", None) == OUTCOME_OTHER


def test_a_real_failure_makes_the_member_fail() -> None:
    verdict = member_verdict(_runs(("failed", "provider error"), ("completed", None)), now=NOW)
    assert verdict.failing
    assert (verdict.recent_runs, verdict.recent_failures, verdict.recent_interruptions) == (2, 1, 0)
    assert verdict.last_run_outcome == OUTCOME_FAILED


def test_interruptions_alone_never_make_the_member_fail() -> None:
    verdict = member_verdict(_runs(("failed", ROLLOUT_CUT), ("failed", STALE_CLEANUP)), now=NOW)
    assert not verdict.failing
    assert (verdict.recent_runs, verdict.recent_failures, verdict.recent_interruptions) == (0, 0, 2)
    assert verdict.last_run_status == "failed"
    assert verdict.last_run_outcome == OUTCOME_INTERRUPTED


def test_the_reported_history_reads_ok() -> None:
    """The latest record a rollout-cut consult child, the assignment runs completed."""
    history = _runs(
        ("failed", ROLLOUT_CUT),
        ("completed", None),
        ("failed", ROLLOUT_CUT),
        ("failed", ROLLOUT_CUT),
        ("completed", None),
        ("completed", None),
        step=timedelta(minutes=5),
    )
    verdict = member_verdict(history, now=NOW + timedelta(hours=2))
    assert not verdict.failing
    assert verdict.last_run_at == history[0].created_at
    assert verdict.recent_interruptions == 3


def test_a_success_after_a_failure_clears_it() -> None:
    verdict = member_verdict(_runs(("completed", None), ("failed", "boom"), ("failed", "boom")), now=NOW)
    assert not verdict.failing
    assert (verdict.recent_runs, verdict.recent_failures) == (3, 2)


def test_a_mixed_window_judges_the_newest_run_not_interrupted() -> None:
    # Interrupted, then active, then a real failure: failing.
    assert member_verdict(
        _runs(("failed", ROLLOUT_CUT), ("running", None), ("failed", "boom"), ("completed", None)), now=NOW
    ).failing
    # Interrupted, cancelled, then a success: not failing.
    assert not member_verdict(
        _runs(("failed", CONSULT_CANCELLED_ERROR), ("cancelled", None), ("completed", None), ("failed", "boom")),
        now=NOW,
    ).failing


def test_the_window_bounds_the_verdict() -> None:
    # A failure older than the window no longer counts.
    old = [RunFact(created_at=NOW - HEALTH_WINDOW - timedelta(minutes=1), status="failed", error="boom")]
    verdict = member_verdict(old, now=NOW)
    assert not verdict.failing
    assert verdict.last_run_at == old[0].created_at
    # Only the latest HEALTH_RECENT_RUNS runs are read.
    many = _runs(*([("failed", ROLLOUT_CUT)] * HEALTH_RECENT_RUNS + [("failed", "boom")]), step=timedelta(minutes=1))
    assert not member_verdict(many, now=NOW).failing


def test_no_run_is_no_verdict() -> None:
    verdict = member_verdict([], now=NOW)
    assert not verdict.failing
    assert verdict.last_run_at is None and verdict.last_run_outcome is None


def test_a_delegation_carries_the_same_outcome_as_a_run() -> None:
    """The delegations tab reads ``outcome`` like the run list."""
    import uuid

    from app.models.agent_delegation import AgentDelegation
    from app.services.delegation import _serialize

    def _row(status: str, error: str | None) -> AgentDelegation:
        return AgentDelegation(
            id=uuid.uuid4(), parent_agent_id=uuid.uuid4(), target_agent_id=uuid.uuid4(),
            type="consult", description="d", status=status, error=error, created_at=NOW,
        )

    assert _serialize(_row("failed", ROLLOUT_CUT))["outcome"] == OUTCOME_INTERRUPTED
    assert _serialize(_row("failed", "boom"))["outcome"] == OUTCOME_FAILED
    assert _serialize(_row("completed", None))["outcome"] == OUTCOME_OK
