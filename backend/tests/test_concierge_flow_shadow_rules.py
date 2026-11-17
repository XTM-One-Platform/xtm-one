"""The settlement of a shadow on fixed numbers (#8260): enough pairs, success rate, agreement."""

from __future__ import annotations

from app.services.concierge.flow_shadow import settle_decision


def _pair(verdict="agree", left="completed", right="completed", simulated=0) -> dict:
    return {
        "verdict": verdict,
        "left": {"status": left, "simulated_writes": 0},
        "right": {"status": right, "simulated_writes": simulated},
    }


def test_a_shadow_with_too_few_pairs_is_incomplete():
    verdict = settle_decision([_pair()] * 4, incomplete=2, min_pairs=5)
    assert verdict.status == "incomplete"
    assert verdict.pairs == 6
    assert verdict.agreement_share is None


def test_a_shadow_that_agrees_holds_and_states_simulated_writes():
    verdict = settle_decision([_pair(simulated=1)] * 9 + [_pair("disagree")], incomplete=0, min_pairs=5)
    assert verdict.status == "held"
    assert verdict.agreement_share == 0.9
    assert verdict.flow_success_rate == 1.0
    assert verdict.simulated_writes == 9
    assert verdict.note.endswith("9 writes were simulated, not executed.")


def test_a_shadow_whose_flow_fails_more_does_not_hold():
    comparisons = [_pair()] * 8 + [_pair("disagree", right="failed")] * 2
    verdict = settle_decision(comparisons, incomplete=0, min_pairs=5)
    assert verdict.status == "not_held"
    assert verdict.assignment_success_rate == 1.0
    assert verdict.flow_success_rate == 0.8


def test_a_shadow_that_disagrees_does_not_hold():
    verdict = settle_decision([_pair()] * 7 + [_pair("disagree")] * 3, incomplete=0, min_pairs=5)
    assert verdict.status == "not_held"
    assert verdict.agreement_share == 0.7
