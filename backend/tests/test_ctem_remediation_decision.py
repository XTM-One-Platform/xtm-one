"""Remediation decision of a CTEM case (CTEM-17, gate B): decided by policy out of the box, configurable.

``services/ctem_remediation_decision.py``: the gate's level or pin always
wins; otherwise the approval threshold applies only when the organization set
one; with none, the recommended option is applied by policy. A waiting
decision is an ask of the unified decision system, never a CTEM-only card, and
a user who cannot read OpenCRQ sees no expected-loss figure.
"""

from __future__ import annotations

import pytest

from app.agents.builtin_fleet_docs import CTEM_GATE_KEYS
from app.services import ctem_remediation_decision as rd

OPTIONS = [
    {
        "key": "A",
        "label": "Turn HTTP/2 off on all controllers",
        "cost": 6000,
        "expected_loss_before": 19_600_000,
        "expected_loss_after": 900_000,
        "time_to_apply": "2 h per cluster",
        "runbook": "disable-http2",
        "preapproved": True,
    },
    {
        "key": "C",
        "label": "Move routes to the new gateway",
        "cost": 120_000,
        "expected_loss_before": 19_600_000,
        "expected_loss_after": 150_000,
        "time_to_apply": "8 weeks",
    },
]


def _parsed() -> list[rd.RemediationOption]:
    return rd.parse_options(OPTIONS)


def test_the_gate_key_is_in_the_ctem_catalog() -> None:
    assert rd.GATE_KEY in CTEM_GATE_KEYS


def test_no_threshold_is_decided_by_policy() -> None:
    decision = rd.decide(_parsed(), "C", gate_waits=False, gate_by="level low", approval_threshold=None)
    assert not decision.waits and decision.chosen == "C"
    assert decision.reason == "decided by policy (level low)"


def test_a_gate_that_waits_always_wins() -> None:
    decision = rd.decide(_parsed(), "A", gate_waits=True, gate_by="pin wait", approval_threshold=None)
    assert decision.waits and decision.chosen == "" and "pin wait" in decision.reason


@pytest.mark.parametrize(
    ("threshold", "waits"),
    [(19_600_001, False), (19_600_000, False), (19_599_999, True)],
)
def test_threshold_at_above_and_below(threshold: float, waits: bool) -> None:
    decision = rd.decide(_parsed(), "C", gate_waits=False, gate_by="level low", approval_threshold=threshold)
    assert decision.waits is waits
    assert decision.chosen == ("" if waits else "C")


def test_a_preapproved_runbook_proceeds_above_the_threshold() -> None:
    decision = rd.decide(_parsed(), "A", gate_waits=False, gate_by="level low", approval_threshold=1000)
    assert not decision.waits and decision.chosen == "A" and "disable-http2" in decision.reason


def test_a_threshold_with_unknown_loss_waits() -> None:
    parsed = rd.parse_options([{"key": "A", "label": "Patch"}])
    decision = rd.decide(parsed, "A", gate_waits=False, gate_by="level low", approval_threshold=10)
    assert decision.waits and "unknown" in decision.reason


def test_a_decider_without_opencrq_read_sees_no_figures() -> None:
    decision = rd.decide(
        _parsed(), "C", gate_waits=False, gate_by="level low", approval_threshold=None, figures_visible=False
    )
    note = rd.outcome_note(decision, _parsed())
    assert "19,600,000" not in note and "cost" not in note and "8 weeks" in note
    ask = rd.decision_ask("Disable HTTP/2", _parsed(), "C", figures=False, reason="the gate waits (pin wait)")
    assert all("expected loss" not in o["effect"] for o in ask["options"])


@pytest.mark.parametrize("threshold", [None, 100.0, 50_000_000.0])
def test_the_case_trail_never_carries_a_figure_even_for_a_decider_with_opencrq_read(threshold: float | None) -> None:
    """Every viewer of the case reads its history, whatever their own OpenCRQ access (review on #1003)."""
    decision = rd.decide(
        _parsed(), "C", gate_waits=False, gate_by="level low", approval_threshold=threshold, figures_visible=True
    )
    note = rd.outcome_note(decision, _parsed())
    for figure in ("19,600,000", "19600000", "120,000", "150,000", "cost", "expected loss 1", "100", "5e+07"):
        assert figure not in decision.case_reason and figure not in note
    assert "8 weeks" in note


def test_the_waiting_ask_is_a_unified_decision_with_figures() -> None:
    ask = rd.decision_ask("Disable HTTP/2", _parsed(), "A", figures=True, reason="the gate waits (pin wait)")
    assert set(ask) >= {"question", "context", "options", "recommendation"}
    assert ask["recommendation"] == "A"
    assert "expected loss 19,600,000 before, 900,000 after" in ask["options"][0]["effect"]


@pytest.mark.parametrize(
    "options",
    [
        [],
        [{"key": "A"}],
        [{"key": "A", "label": "x"}, {"key": "a", "label": "y"}],
        [{"key": "A", "label": "x", "cost": -1}],
    ],
)
def test_bad_options_are_refused(options: list[dict[str, object]]) -> None:
    with pytest.raises(rd.DecisionInputError):
        rd.parse_options(options)


def test_an_unknown_recommendation_is_refused() -> None:
    with pytest.raises(rd.DecisionInputError):
        rd.decide(_parsed(), "Z", gate_waits=False, gate_by="level low", approval_threshold=None)


def test_preapproved_needs_a_runbook() -> None:
    (option,) = rd.parse_options([{"key": "A", "label": "x", "preapproved": True}])
    assert option.preapproved is False
