"""The pure rules of the Experimenter (#8259): budget sizing, and what a
finished experiment may file."""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.concierge_experiment import (
    STATUS_COMPLETED,
    STATUS_DRAFT,
    STATUS_FAILED,
    STATUS_INCOMPLETE,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    STATUS_STALE,
)
from app.services.concierge import experiments
from app.services.concierge.experiments import (
    DEFAULT_CASE_TOKENS,
    MAX_CASES,
    MIN_CASES,
    filing_verdict,
    size_to_budget,
)
from app.services.concierge.quality_deps import Budget

NO_CAP = Budget(tokens=None, cost_usd=None, refusal=None)


class TestSizing:
    def test_no_cap_takes_the_request(self) -> None:
        sizing = size_to_budget(NO_CAP, requested_cases=20, trials=1, case_tokens=1_000, case_cost_usd=0.01)
        assert sizing.cases == 20
        # Two arms, one trial, the judge's quarter on top.
        assert sizing.case_tokens == 2_500
        assert sizing.tokens == 50_000
        assert sizing.cost_usd == Decimal("0.01") * 2 * Decimal("1.25") * 20

    def test_token_cap_sizes_down(self) -> None:
        budget = Budget(tokens=30_000, cost_usd=None, refusal=None)
        sizing = size_to_budget(budget, requested_cases=20, trials=2, case_tokens=1_000, case_cost_usd=None)
        assert sizing.case_tokens == 5_000
        assert sizing.cases == 6
        assert sizing.tokens == 30_000
        assert sizing.cost_usd == Decimal(0)

    def test_cost_cap_sizes_down(self) -> None:
        budget = Budget(tokens=None, cost_usd=Decimal("1.00"), refusal=None)
        sizing = size_to_budget(budget, requested_cases=50, trials=1, case_tokens=1_000, case_cost_usd=0.1)
        assert sizing.cases == 4  # 0.25 per case
        assert sizing.cases < MIN_CASES

    @pytest.mark.parametrize("tokens", [None, 10_000_000])
    def test_a_cost_cap_with_no_measured_cost_launches_nothing(self, tokens: int | None) -> None:
        budget = Budget(tokens=tokens, cost_usd=Decimal("1.00"), refusal=None)
        sizing = size_to_budget(budget, requested_cases=20, trials=1, case_tokens=1_000, case_cost_usd=None)
        assert (sizing.cases, sizing.tokens, sizing.cost_usd) == (0, 0, Decimal(0))

    def test_a_measured_free_run_is_not_held_by_the_cost_cap(self) -> None:
        budget = Budget(tokens=None, cost_usd=Decimal("1.00"), refusal=None)
        sizing = size_to_budget(budget, requested_cases=20, trials=1, case_tokens=1_000, case_cost_usd=0.0)
        assert sizing.cases == 20

    def test_unknown_tokens_use_the_default(self) -> None:
        sizing = size_to_budget(NO_CAP, requested_cases=10, trials=1, case_tokens=None, case_cost_usd=None)
        assert sizing.case_tokens == int(DEFAULT_CASE_TOKENS * 2 * 1.25)

    @pytest.mark.parametrize(("requested", "trials", "cases", "kept_trials"), [(0, 0, 20, 1), (999, 9, MAX_CASES, 3)])
    def test_bounds(self, requested: int, trials: int, cases: int, kept_trials: int) -> None:
        sizing = size_to_budget(NO_CAP, requested_cases=requested, trials=trials, case_tokens=10, case_cost_usd=None)
        assert (sizing.cases, sizing.trials) == (cases, kept_trials)

    def test_exhausted_budget_affords_nothing(self) -> None:
        budget = Budget(tokens=0, cost_usd=Decimal(0), refusal=None)
        sizing = size_to_budget(budget, requested_cases=20, trials=1, case_tokens=1_000, case_cost_usd=0.01)
        assert sizing.cases == 0


def _experiment(status: str, *, reason: str | None = None, result: dict | None = None):
    return SimpleNamespace(status=status, status_reason=reason, result=result or {})


class TestFilingVerdict:
    def test_progression_filed(self) -> None:
        assert filing_verdict(_experiment(STATUS_COMPLETED, result={"verdict": "progression"})) == ("progression", None)

    def test_regression_refused(self) -> None:
        verdict, refusal = filing_verdict(_experiment(STATUS_COMPLETED, result={"verdict": "regression"}))
        assert verdict is None
        assert refusal == "The variant measured a regression: nothing to propose."

    def test_neutral_shorter_filed(self) -> None:
        result = {"verdict": "neutral", "base_chars": 1_000, "variant_chars": 800}
        assert filing_verdict(_experiment(STATUS_COMPLETED, result=result)) == ("neutral", None)

    def test_a_neutral_structured_variant_is_never_shorter(self) -> None:
        # A flow kind stores no variant text: its 0 characters are no gain.
        result = {"verdict": "neutral", "base_chars": 1_000, "variant_chars": 0, "cost_usd": {"baseline": 1.0, "variant": 1.0}}
        verdict, refusal = filing_verdict(_experiment(STATUS_COMPLETED, result=result))
        assert verdict is None
        assert refusal == "The variant measured no better, shorter or cheaper than the current text: nothing to propose."

    def test_neutral_cheaper_filed(self) -> None:
        result = {
            "verdict": "neutral",
            "base_chars": 800,
            "variant_chars": 900,
            "cost_usd": {"baseline": 1.0, "variant": 0.9},
        }
        assert filing_verdict(_experiment(STATUS_COMPLETED, result=result)) == ("neutral", None)

    def test_neutral_without_gain_refused(self) -> None:
        result = {
            "verdict": "neutral",
            "base_chars": 800,
            "variant_chars": 900,
            "cost_usd": {"baseline": 1.0, "variant": 0.98},
        }
        verdict, refusal = filing_verdict(_experiment(STATUS_COMPLETED, result=result))
        assert verdict is None
        assert refusal is not None

    @pytest.mark.parametrize(
        ("status", "reason"),
        [
            (STATUS_SKIPPED, "budget"),
            (STATUS_SKIPPED, "no_dataset"),
            (STATUS_SKIPPED, "no_consent"),
            (STATUS_SKIPPED, "evaluation_unavailable"),
            (STATUS_INCOMPLETE, "budget_reached_mid_run"),
            (STATUS_FAILED, "evaluation_unavailable"),
        ],
    )
    def test_unmeasured_states_its_reason(self, status: str, reason: str) -> None:
        assert filing_verdict(_experiment(status, reason=reason)) == ("unmeasured", None)

    @pytest.mark.parametrize(
        ("status", "reason"),
        [(STATUS_SKIPPED, None), (STATUS_SKIPPED, "made-up"), (STATUS_STALE, None), (STATUS_DRAFT, None), (STATUS_RUNNING, None)],
    )
    def test_nothing_filed(self, status: str, reason: str | None) -> None:
        verdict, refusal = filing_verdict(_experiment(status, reason=reason))
        assert verdict is None
        assert refusal is not None

    def test_a_verdict_is_never_read_from_an_unfinished_row(self) -> None:
        running = _experiment(STATUS_RUNNING, result={"verdict": "progression"})
        assert filing_verdict(running)[0] is None


def test_an_unknown_change_type_is_refused() -> None:
    with pytest.raises(experiments.ExperimentError):
        experiments.experiment_kind("flow_repair_v2")
    with pytest.raises(ValueError):
        experiments.register_experiment_kind("flow_repair_v2", object())


def test_the_payload_serves_refs_never_a_graph_nor_the_reservation() -> None:
    experiment = SimpleNamespace(
        id=uuid.UUID(int=1),
        finding_id=uuid.UUID(int=2),
        change_type="flow_repair",
        target_type="flow",
        target_id=uuid.UUID(int=3),
        status=STATUS_DRAFT,
        status_reason=None,
        variant_summary="Retry the failing node.",
        variant={"sandbox_flow_id": "f", "graph": {"nodes": ["big"]}},
        base_text="",
        variant_text="",
        sizing={"cases": 10, "reservation": {"cycle_id": "c"}},
        result={},
        evaluation_id=None,
        proposal_id=None,
        created_at=None,
        completed_at=None,
    )
    payload = experiments.experiment_payload(experiment)
    assert payload["variant"] == {"sandbox_flow_id": "f"}
    assert payload["sizing"] == {"cases": 10}
    assert (payload["base_chars"], payload["variant_chars"], payload["result"]) == (0, 0, None)
