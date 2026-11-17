"""Run comparison verdicts on fixed sides (#8260): agreement, failures, incomplete, simulated writes."""

from __future__ import annotations

import uuid

from app.services.concierge.flow_comparison import RunSide, compare_sides

# ── Run comparison ─────────────────────────────────────────────────────────


def _side(kind="flow_run", status="completed", output="Done.", simulated=0) -> RunSide:
    return RunSide(kind=kind, run_id=uuid.uuid4(), status=status, output=output, simulated_writes=simulated)


def test_same_output_modulo_whitespace_and_case_agrees():
    result = compare_sides(_side("assignment_run", output="Ticket  CREATED\n"), _side(output="ticket created"))
    assert result.verdict == "agree"
    assert result.similarity == 1.0


def test_different_output_disagrees():
    result = compare_sides(_side("assignment_run", output="Escalated to tier 2."), _side(output="Closed as benign."))
    assert result.verdict == "disagree"
    assert result.similarity < 0.85


def test_a_running_side_is_incomplete_never_agreement():
    result = compare_sides(_side("assignment_run", status="running"), _side())
    assert result.verdict == "incomplete"
    assert result.similarity is None


def test_a_missing_side_is_incomplete():
    assert compare_sides(None, _side()).verdict == "incomplete"


def test_one_side_failing_disagrees_and_both_failing_is_its_own_verdict():
    assert compare_sides(_side("assignment_run"), _side(status="failed")).verdict == "disagree"
    assert compare_sides(_side("assignment_run", status="failed"), _side(status="failed")).verdict == "both_failed"


def test_simulated_writes_are_reported_with_the_comparison():
    result = compare_sides(_side("assignment_run"), _side(simulated=3))
    assert result.simulated_writes == 3
    assert result.as_dict()["right"]["simulated_writes"] == 3
