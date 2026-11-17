"""Unit tests for the per-run spending budget object.

``RunBudget`` unifies cost / tokens / tool calls / duration behind the
reserve -> call -> debit -> block pattern.  These tests pin the accounting
itself (the agent-loop wiring is covered by
``tests/test_agent_loop_run_budget.py``):

* a granted reservation holds worst-case spend, a released one hands it back;
* the actual usage debited after a call is what counts against the budget;
* cost fails OPEN when the model has no price row;
* the closing-synthesis reserve applies to cost/tokens but never to tool
  calls or duration;
* each dimension warns at most once; and
* the third outcome: a cost refusal that WOULD fit at a configured
  fallback model's rates degrades instead, once per run, and re-prices
  everything after it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services.tracing.cost_calculator import ModelPrice
from app.tools.run_budget import (
    DEFAULT_FINAL_ANSWER_RESERVE,
    BudgetDimension,
    BudgetFallbackModel,
    RunBudget,
    budget_configured,
    build_run_budget,
    count_usage_tokens,
)


def _price(input_per_1m: str = "3", output_per_1m: str = "15") -> ModelPrice:
    """A price row shaped like the seeded ones (USD per 1M tokens)."""
    return ModelPrice(
        provider="stub",
        model_id="stub-model",
        model_pattern=None,
        input_price_per_1m=Decimal(input_per_1m),
        output_price_per_1m=Decimal(output_per_1m),
        cache_read_price_per_1m=None,
        cache_write_price_per_1m=None,
        effective_from=date(2026, 1, 1),
        is_custom=False,
    )


class _FakeClock:
    """Monotonic clock the test drives by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ── Activation / configuration ───────────────────────────────────────────


def test_all_zero_limits_is_inactive():
    """Every dimension at 0 (the default) means "no budget"."""
    budget = RunBudget(
        max_cost_usd=0, max_total_tokens=0, max_tool_calls=0, max_duration_seconds=0
    )
    assert budget.active is False
    assert budget.check() is None
    assert budget.describe_limits() == "disabled"


def test_negative_limits_are_treated_as_disabled():
    budget = RunBudget(max_total_tokens=-1, max_cost_usd=-0.5)
    assert budget.active is False


def test_reserve_fraction_is_clamped():
    """A reserve of 1.0 would refuse every call, synthesis included."""
    assert RunBudget(max_total_tokens=10, final_answer_reserve_fraction=5.0).final_answer_reserve_fraction == 0.5
    assert RunBudget(max_total_tokens=10, final_answer_reserve_fraction=-1.0).final_answer_reserve_fraction == 0.0


# ── reserve → call → debit → release ─────────────────────────────────────


def test_reservation_blocks_a_second_call_then_release_frees_it():
    """The worst case of an in-flight call is held against the run."""
    budget = RunBudget(max_total_tokens=10_000, final_answer_reserve_fraction=0.0)
    first = budget.try_reserve(estimated_input_tokens=4_000, max_output_tokens=2_000)
    assert first.granted
    assert first.tokens == 6_000

    # 6k reserved + 6k projected = 12k > 10k → refused, and nothing is held
    # for the refused call.
    second = budget.try_reserve(estimated_input_tokens=4_000, max_output_tokens=2_000)
    assert second.granted is False
    assert second.violation is not None
    assert second.violation.dimension is BudgetDimension.TOKENS

    # The call came back cheap: debit the real usage, release the worst case.
    budget.record_llm_usage({"input_tokens": 4_000, "output_tokens": 100})
    budget.release(first)
    assert budget.spent_tokens == 4_100

    # 4.1k actually spent + a 5k worst case still fits under 10k; with the
    # first call's 6k reservation still held it would not.
    third = budget.try_reserve(estimated_input_tokens=4_000, max_output_tokens=1_000)
    assert third.granted, "released worst-case headroom must return to the run"


def test_release_of_a_refused_reservation_is_a_no_op():
    budget = RunBudget(max_total_tokens=100, final_answer_reserve_fraction=0.0)
    refused = budget.try_reserve(estimated_input_tokens=1_000, max_output_tokens=10)
    assert refused.granted is False
    budget.release(refused)
    budget.release(None)
    # Still nothing held: a call within budget is granted.
    assert budget.try_reserve(estimated_input_tokens=10, max_output_tokens=10).granted


def test_actual_usage_is_what_counts_against_the_budget():
    """Debits are exact; the reservation is only a gate."""
    budget = RunBudget(max_cost_usd=1.0, price=_price(), final_answer_reserve_fraction=0.0)
    reservation = budget.try_reserve(estimated_input_tokens=10_000, max_output_tokens=10_000)
    assert reservation.granted
    # Worst case: 10k in @ $3/1M + 10k out @ $15/1M = $0.18
    assert reservation.cost_usd == Decimal("0.18")

    budget.record_llm_usage({"input_tokens": 10_000, "output_tokens": 100})
    budget.release(reservation)
    # Actual: 10k in ($0.03) + 100 out ($0.0015)
    assert budget.spent_cost_usd == Decimal("0.0315")


def test_cost_limit_blocks_the_next_call():
    budget = RunBudget(max_cost_usd=0.05, price=_price(), final_answer_reserve_fraction=0.0)
    budget.record_llm_usage({"input_tokens": 10_000, "output_tokens": 2_000})  # $0.06
    violation = budget.check()
    assert violation is not None
    assert violation.dimension is BudgetDimension.COST
    assert "cost budget" in violation.user_message()


def test_cost_fails_open_without_a_price_row():
    """No pricing data must never stop a run — the other dimensions still do."""
    budget = RunBudget(max_cost_usd=0.000_001, max_total_tokens=1_000, price=None)
    assert budget.active is True
    assert budget.enforces_cost is False
    budget.record_llm_usage({"input_tokens": 500_000, "output_tokens": 500_000})
    violation = budget.check()
    assert violation is not None
    assert violation.dimension is BudgetDimension.TOKENS, "cost must not be enforced"
    assert "NOT enforced" in budget.describe_limits()


# ── The closing-synthesis reserve ────────────────────────────────────────


def test_final_call_may_spend_the_reserved_slice():
    """In-loop calls are gated at 90%; the synthesis gets the full limit."""
    budget = RunBudget(max_total_tokens=1_000, final_answer_reserve_fraction=0.1)
    budget.record_llm_usage({"total_tokens": 920})
    assert budget.check() is not None, "in-loop gate is 900 tokens"
    assert budget.check(final=True) is None, "the synthesis may use the last 10%"


def test_final_gate_still_refuses_a_call_over_the_full_limit():
    budget = RunBudget(max_total_tokens=1_000, final_answer_reserve_fraction=0.1)
    budget.record_llm_usage({"total_tokens": 1_050})
    assert budget.check(final=True) is not None


def test_remaining_tool_calls_is_the_allowance_left():
    """What the loop cuts a parallel batch to."""
    assert RunBudget(max_total_tokens=10).remaining_tool_calls() is None
    budget = RunBudget(max_tool_calls=3)
    assert budget.remaining_tool_calls() == 3
    budget.record_tool_calls(2)
    assert budget.remaining_tool_calls() == 1
    budget.record_tool_calls(5)
    assert budget.remaining_tool_calls() == 0, "never negative"


def test_remaining_tool_calls_agrees_with_the_gate_for_any_limit():
    """At least one call is left exactly when the gate still admits one, a
    fractional limit included: a truncated allowance would cut every batch to
    nothing while the gate kept admitting the next call."""
    budget = RunBudget(max_tool_calls=2.5)
    budget.record_tool_calls(2)
    assert budget.check() is None
    assert budget.remaining_tool_calls() == 1
    budget.record_tool_calls(1)
    assert budget.check() is not None
    assert budget.remaining_tool_calls() == 0


def test_tool_call_and_duration_limits_ignore_the_reserve():
    """A tool-free synthesis needs no tool calls, and duration already gets a
    reserve in seconds from the loop's wall-clock machinery — so neither
    dimension is shrunk by the fraction."""
    clock = _FakeClock()
    budget = RunBudget(
        max_tool_calls=10,
        max_duration_seconds=100,
        final_answer_reserve_fraction=DEFAULT_FINAL_ANSWER_RESERVE,
        clock=clock,
    )
    budget.start()
    budget.record_tool_calls(9)
    clock.advance(95)
    assert budget.check() is None, "9/10 calls and 95/100s must both still pass"

    budget.record_tool_calls(1)
    violation = budget.check()
    assert violation is not None
    assert violation.dimension is BudgetDimension.TOOL_CALLS


def test_duration_limit_and_deadline():
    clock = _FakeClock()
    budget = RunBudget(max_duration_seconds=60, clock=clock)
    assert budget.deadline() is None, "no deadline before start()"
    budget.start()
    started = clock.now
    assert budget.deadline() == started + 60
    clock.advance(30)
    budget.start()  # idempotent — must not restart the clock
    assert budget.deadline() == started + 60
    assert budget.check() is None
    clock.advance(31)
    violation = budget.check()
    assert violation is not None
    assert violation.dimension is BudgetDimension.DURATION


# ── Warnings ─────────────────────────────────────────────────────────────


def test_warning_fires_once_per_dimension():
    budget = RunBudget(max_total_tokens=1_000, max_tool_calls=10, warn_fraction=0.75)
    assert budget.take_warning() is None
    budget.record_llm_usage({"total_tokens": 800})
    first = budget.take_warning()
    assert first is not None
    assert "token budget" in first
    assert budget.take_warning() is None, "the token warning is one-shot"

    budget.record_tool_calls(8)
    second = budget.take_warning()
    assert second is not None
    assert "tool-call budget" in second


def test_warning_disabled_by_zero_fraction():
    budget = RunBudget(max_total_tokens=100, warn_fraction=0.0)
    budget.record_llm_usage({"total_tokens": 99})
    assert budget.take_warning() is None


# ── Usage-shape handling ─────────────────────────────────────────────────


def test_count_usage_tokens_across_provider_conventions():
    assert count_usage_tokens(None) == 0
    assert count_usage_tokens({}) == 0
    assert count_usage_tokens({"total_tokens": 42}) == 42
    assert count_usage_tokens({"input_tokens": 10, "output_tokens": 5}) == 15
    assert count_usage_tokens({"prompt_tokens": 10, "completion_tokens": 5}) == 15
    # Langfuse-normalised shape, consulted only when no raw key is present.
    assert count_usage_tokens({"input": 10, "output": 5}) == 15
    # An explicit total wins so a provider sending both is not double counted.
    assert count_usage_tokens({"total_tokens": 20, "input_tokens": 10, "output_tokens": 10}) == 20
    # Non-numeric junk is ignored rather than raising.
    assert count_usage_tokens({"input_tokens": "lots"}) == 0


def test_total_only_usage_is_debited_at_the_higher_rate():
    """A usage that reports only a total used to cost $0."""
    budget = RunBudget(max_cost_usd=10.0, price=_price("3", "15"))
    budget.record_llm_usage({"total_tokens": 1_000_000})
    assert budget.spent_cost_usd == Decimal("15")
    assert budget.spent_tokens == 1_000_000


def test_the_openai_compatible_zero_split_with_a_total_is_debited():
    """The shape ``extract_openai_usage`` returns for a server reporting only a
    total: the split keys are present, but zero."""
    budget = RunBudget(max_cost_usd=10.0, price=_price("3", "15"))
    budget.record_llm_usage({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 200_000})
    assert budget.spent_cost_usd == Decimal("3")


def test_a_split_usage_keeps_its_exact_price():
    budget = RunBudget(max_cost_usd=10.0, price=_price("3", "15"))
    budget.record_llm_usage({"input_tokens": 1_000_000, "output_tokens": 100_000, "total_tokens": 1_100_000})
    assert budget.spent_cost_usd == Decimal("3") + Decimal("1.5")


def test_a_total_only_estimate_warns_once_per_run(caplog):
    import logging

    budget = RunBudget(max_cost_usd=10.0, price=_price())
    with caplog.at_level(logging.WARNING, logger="app.tools.run_budget"):
        budget.record_llm_usage({"total_tokens": 10})
        budget.record_llm_usage({"total_tokens": 10})
    warnings = [r for r in caplog.records if "total with no input/output split" in r.getMessage()]
    assert len(warnings) == 1


def test_a_total_only_usage_trips_the_cost_gate():
    budget = RunBudget(max_cost_usd=1.0, price=_price("1", "1"), final_answer_reserve_fraction=0.0)
    budget.record_llm_usage({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 2_000_000})
    violation = budget.check()
    assert violation is not None
    assert violation.dimension is BudgetDimension.COST


def test_a_total_above_its_split_debits_the_difference_at_the_higher_rate():
    """xAI leaves reasoning tokens out of ``completion_tokens`` but counts them
    in ``total_tokens``: pricing the split alone debited them nothing."""
    budget = RunBudget(max_cost_usd=10.0, price=_price("3", "15"))
    budget.record_llm_usage(
        {"prompt_tokens": 100_000, "completion_tokens": 10_000, "total_tokens": 210_000}
    )
    # $0.30 in + $0.15 out, plus the 100k tokens only the total reports at $15.
    assert budget.spent_cost_usd == Decimal("1.95")
    assert budget.spent_tokens == 210_000


def test_the_split_is_read_the_way_calculate_costs_reads_it():
    """The first key present in a bucket is that bucket's count: a zero
    ``input_tokens`` hides ``prompt_tokens`` from the formula, so this call is
    not priced and its total must be estimated rather than debited $0."""
    budget = RunBudget(max_cost_usd=10.0, price=_price("3", "15"))
    budget.record_llm_usage(
        {"input_tokens": 0, "prompt_tokens": 500_000, "total_tokens": 500_000}
    )
    assert budget.spent_cost_usd == Decimal("7.5")


def test_a_total_the_split_already_covers_is_never_estimated():
    """Cache counters sit beside ``input_tokens`` in the Anthropic shape, so a
    total can be smaller than what the formula prices: nothing to add."""
    budget = RunBudget(max_cost_usd=10.0, price=_price("3", "15"))
    budget.record_llm_usage(
        {
            "input_tokens": 1_000_000,
            "cache_read_input_tokens": 500_000,
            "output_tokens": 0,
            "total_tokens": 1_000_000,
        }
    )
    # 1.5M prompt tokens at $3 (no cache tariff: cache reads at the input
    # rate) and nothing estimated on top.
    assert budget.spent_cost_usd == Decimal("4.5")


def test_a_remainder_beside_a_split_is_logged_once_at_info(caplog):
    """It is how xAI reports every reasoning call: worth one line per run, not
    a WARNING an operator can do nothing about."""
    import logging

    budget = RunBudget(max_cost_usd=10.0, price=_price())
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 25}
    with caplog.at_level(logging.INFO, logger="app.tools.run_budget"):
        budget.record_llm_usage(usage)
        budget.record_llm_usage(usage)
    noted = [r for r in caplog.records if "beyond its input/output counts" in r.getMessage()]
    assert [r.levelno for r in noted] == [logging.INFO]


def test_snapshot_reports_state_and_limits():
    budget = RunBudget(max_cost_usd=1.0, max_total_tokens=500, price=_price())
    budget.start()
    budget.record_llm_usage({"input_tokens": 1_000, "output_tokens": 100})
    budget.record_tool_calls(3)
    snap = budget.snapshot()
    assert snap["tokens"] == 1_100
    assert snap["tool_calls"] == 3
    assert snap["max_total_tokens"] == 500
    assert snap["max_tool_calls"] is None
    assert snap["cost_enforced"] is True
    assert snap["cost_usd"] > 0


# ── The third outcome: degrade instead of refuse ─────────────────


def _cheap_fallback(*, per_1m: str = "0.3", model_id: str = "cheap-model"):
    """A fallback model 10x cheaper than ``_price()`` on both directions."""
    price = ModelPrice(
        provider="stub",
        model_id=model_id,
        model_pattern=None,
        input_price_per_1m=Decimal(per_1m),
        output_price_per_1m=Decimal(per_1m),
        cache_read_price_per_1m=None,
        cache_write_price_per_1m=None,
        effective_from=date(2026, 1, 1),
        is_custom=False,
    )
    return BudgetFallbackModel(model_id=model_id, price=price, provider=object())


def _exhausted_cost_budget(**kwargs) -> tuple[RunBudget, object]:
    """A budget whose next 100K-token call no longer fits at $3/$15 per 1M."""
    budget = RunBudget(
        max_cost_usd=0.5, price=_price(), final_answer_reserve_fraction=0.0, **kwargs
    )
    budget.record_llm_usage({"input_tokens": 100_000, "output_tokens": 10_000})
    violation = budget.can_afford(
        estimated_input_tokens=20_000, max_output_tokens=8_000
    )
    assert violation is not None and violation.dimension is BudgetDimension.COST
    return budget, violation


def test_no_fallback_means_no_downgrade():
    """Without a configured fallback the refusal stands."""
    budget, violation = _exhausted_cost_budget()
    assert budget.fallback_available is False
    assert (
        budget.can_downgrade(
            violation, estimated_input_tokens=20_000, max_output_tokens=8_000
        )
        is False
    )


def test_cost_refusal_degrades_when_the_fallback_can_pay():
    """The same reservation, re-priced at the fallback's rates, fits."""
    budget, violation = _exhausted_cost_budget(fallback=_cheap_fallback())
    assert budget.fallback_available is True
    assert (
        budget.can_downgrade(
            violation, estimated_input_tokens=20_000, max_output_tokens=8_000
        )
        is True
    )


def test_only_cost_can_degrade():
    """Tokens / tool calls / duration are model-independent — a cheaper model
    buys no extra headroom, so they must keep hard-stopping."""
    budget = RunBudget(
        max_cost_usd=10.0,
        max_total_tokens=50_000,
        max_tool_calls=2,
        price=_price(),
        fallback=_cheap_fallback(),
        final_answer_reserve_fraction=0.0,
    )
    budget.record_llm_usage({"input_tokens": 45_000, "output_tokens": 1_000})
    token_violation = budget.can_afford(
        estimated_input_tokens=20_000, max_output_tokens=8_000
    )
    assert token_violation is not None
    assert token_violation.dimension is BudgetDimension.TOKENS
    assert (
        budget.can_downgrade(
            token_violation, estimated_input_tokens=20_000, max_output_tokens=8_000
        )
        is False
    )

    budget.record_tool_calls(2)
    tool_violation = budget.check()
    assert tool_violation is not None
    assert tool_violation.dimension is BudgetDimension.TOOL_CALLS
    assert (
        budget.can_downgrade(
            tool_violation, estimated_input_tokens=0, max_output_tokens=10
        )
        is False
    )


def test_a_fallback_with_no_price_cannot_decide_a_downgrade():
    """With no price row there is no way to know the call is cheaper on it, so
    cost enforcement stays closed rather than guessing (fail-open applies to
    *enforcement*, never to spending more)."""
    budget, violation = _exhausted_cost_budget(
        fallback=BudgetFallbackModel(model_id="cheap-model", price=None)
    )
    assert budget.fallback_available is False
    assert (
        budget.can_downgrade(
            violation, estimated_input_tokens=20_000, max_output_tokens=8_000
        )
        is False
    )


def test_a_fallback_that_is_also_too_expensive_does_not_degrade():
    """Repricing is a real check, not a rubber stamp."""
    budget, violation = _exhausted_cost_budget(
        fallback=_cheap_fallback(per_1m="15")
    )
    assert (
        budget.can_downgrade(
            violation, estimated_input_tokens=20_000, max_output_tokens=8_000
        )
        is False
    )


def test_commit_reprices_the_rest_of_the_run():
    """Every later reservation AND debit uses the fallback's rates — without
    this the accounting silently misprices the remainder."""
    budget, violation = _exhausted_cost_budget(fallback=_cheap_fallback())
    spent_before = budget.spent_cost_usd

    downgrade = budget.commit_downgrade(violation, from_model="expensive-model")
    assert (downgrade.from_model, downgrade.to_model) == (
        "expensive-model",
        "cheap-model",
    )
    assert budget.downgrade is downgrade
    assert budget.price is not None and budget.price.model_id == "cheap-model"

    # 1M in + 1M out at $0.30/1M each = $0.60 on the fallback (it would have
    # been $18 at the primary's $3/$15).
    budget.record_llm_usage({"input_tokens": 1_000_000, "output_tokens": 1_000_000})
    assert budget.spent_cost_usd - spent_before == Decimal("0.6")

    # And the call that was refused is now affordable, priced cheaply.
    assert (
        budget.estimate_call_cost(input_tokens=1_000_000, output_tokens=0)
        == Decimal("0.3")
    )


def test_one_downgrade_per_run_not_a_ladder():
    """A second downgrade is refused even when a fallback is still set — a run
    limping along on progressively worse models produces an answer nobody
    wants."""
    budget, violation = _exhausted_cost_budget(fallback=_cheap_fallback())
    budget.commit_downgrade(violation)
    assert budget.fallback_available is False
    assert (
        budget.can_downgrade(
            violation, estimated_input_tokens=20_000, max_output_tokens=8_000
        )
        is False
    )
    with pytest.raises(RuntimeError):
        budget.commit_downgrade(violation)


def test_downgrade_shows_up_in_the_snapshot_and_the_limits_line():
    budget, violation = _exhausted_cost_budget(fallback=_cheap_fallback())
    assert "downgrades to cheap-model" in budget.describe_limits()
    assert budget.snapshot()["fallback_model"] == "cheap-model"
    assert budget.snapshot()["downgraded_to"] is None
    budget.commit_downgrade(violation)
    assert budget.snapshot()["downgraded_to"] == "cheap-model"


def test_the_final_gate_can_also_degrade():
    """Writing the closing answer on the cheap model beats returning the
    deterministic digest."""
    budget = RunBudget(
        max_cost_usd=0.5,
        price=_price(),
        fallback=_cheap_fallback(),
        final_answer_reserve_fraction=0.1,
    )
    budget.record_llm_usage({"input_tokens": 150_000, "output_tokens": 2_000})
    final_violation = budget.can_afford(
        estimated_input_tokens=20_000, max_output_tokens=8_000, final=True
    )
    assert final_violation is not None
    assert (
        budget.can_downgrade(
            final_violation,
            estimated_input_tokens=20_000,
            max_output_tokens=8_000,
            final=True,
        )
        is True
    )


# ── Settings-driven construction ─────────────────────────────────────────


@pytest.fixture
def _budget_settings(monkeypatch):
    """Platform settings with every budget dimension disabled."""
    from app.core.config import settings

    for field, value in (
        ("agent_loop_max_cost_usd", 0.0),
        ("agent_loop_max_total_tokens", 0),
        ("agent_loop_max_tool_calls", 0),
        ("agent_loop_max_tool_calls_strict", False),
        ("agent_loop_max_duration_seconds", 0),
        ("agent_loop_budget_final_answer_reserve", 0.1),
        ("agent_loop_budget_fallback_enabled", False),
    ):
        monkeypatch.setattr(settings, field, value)
    return settings


def test_unconfigured_settings_build_no_budget(_budget_settings):
    assert budget_configured() is False
    assert build_run_budget() is None
    # A caller's own deadline must not activate a budget: the loop's
    # ``wall_clock_deadline`` already enforces that.
    assert build_run_budget(max_duration_seconds=600) is None


def test_configured_settings_build_a_budget(_budget_settings):
    _budget_settings.agent_loop_max_tool_calls = 25
    assert budget_configured() is True
    budget = build_run_budget()
    assert budget is not None
    assert budget.max_tool_calls == 25
    assert budget.max_total_tokens is None


def test_the_exact_tool_call_cut_is_opt_in(_budget_settings):
    """The per-call cut is ``AGENT_LOOP_MAX_TOOL_CALLS_STRICT``: off by
    default, so a batch that crosses a configured budget runs as it always did."""
    assert RunBudget(max_tool_calls=2).strict_tool_calls is False
    _budget_settings.agent_loop_max_tool_calls = 25
    assert build_run_budget().strict_tool_calls is False
    _budget_settings.agent_loop_max_tool_calls_strict = True
    assert build_run_budget().strict_tool_calls is True


@pytest.mark.asyncio
async def test_the_audit_flags_a_tool_call_budget_without_the_exact_cut(_budget_settings):
    from app.api.v1.security_audit import _check_tool_call_budget

    findings: list = []
    await _check_tool_call_budget(findings)
    assert findings == [], "no budget, nothing to flag"

    _budget_settings.agent_loop_max_tool_calls = 50
    await _check_tool_call_budget(findings)
    assert [(f.category, f.severity) for f in findings] == [("tools", "info")]
    assert "AGENT_LOOP_MAX_TOOL_CALLS_STRICT=true" in (findings[0].recommendation or "")

    # No silent pass: the strict cut on a configured budget says so.
    _budget_settings.agent_loop_max_tool_calls_strict = True
    findings = []
    await _check_tool_call_budget(findings)
    assert [(f.id, f.severity) for f in findings] == [("tools.tool_call_budget_enforced", "pass")]


def test_tighter_of_configured_and_caller_duration_wins(_budget_settings):
    _budget_settings.agent_loop_max_duration_seconds = 900
    assert build_run_budget(max_duration_seconds=300).max_duration_seconds == 300
    assert build_run_budget(max_duration_seconds=1_800).max_duration_seconds == 900
    assert build_run_budget().max_duration_seconds == 900


@pytest.mark.asyncio
async def test_for_run_skips_pricing_unless_cost_is_bounded(_budget_settings, monkeypatch):
    """No cost limit means no pricing query — and nothing configured means no
    query and no budget at all."""
    import app.tools.run_budget as rb

    queried: list[str] = []

    async def _fake_resolve(db, *, model_id, provider):
        queried.append(model_id or "")
        return _price()

    monkeypatch.setattr(rb, "resolve_budget_price", _fake_resolve)

    assert await rb.build_run_budget_for_run(None, model_id="m", provider="p") is None
    assert queried == []

    _budget_settings.agent_loop_max_total_tokens = 100_000
    budget = await rb.build_run_budget_for_run(None, model_id="m", provider="p")
    assert budget is not None and budget.price is None
    assert queried == [], "a token-only budget needs no prices"

    _budget_settings.agent_loop_max_cost_usd = 0.5
    budget = await rb.build_run_budget_for_run(None, model_id="m", provider="p")
    assert budget is not None and budget.enforces_cost is True
    assert queried == ["m"]


@pytest.mark.asyncio
async def test_the_model_downgrade_is_opt_in(_budget_settings, monkeypatch):
    """Off by default: finishing a run on a weaker model must be a choice an
    operator made, so the flag also gates the router lookup entirely."""
    import app.tools.run_budget as rb

    resolved: list[str | None] = []

    async def _fake_target(db, *, current_model, user_id=None):
        resolved.append(current_model)
        return object(), "cheap-model"

    monkeypatch.setattr(
        "app.llm.router.resolve_fallback_model_provider", _fake_target
    )

    async def _fake_price(db, *, model_id, provider):
        return _price()

    monkeypatch.setattr(rb, "resolve_budget_price", _fake_price)

    assert await rb.resolve_budget_fallback(None, current_model="m") is None
    assert resolved == [], "the flag must gate the lookup, not just the result"

    _budget_settings.agent_loop_budget_fallback_enabled = True
    fallback = await rb.resolve_budget_fallback(None, current_model="m")
    assert fallback is not None and fallback.model_id == "cheap-model"
    assert fallback.usable is True
    assert resolved == ["m"]


@pytest.mark.asyncio
async def test_a_fallback_without_pricing_is_not_offered(_budget_settings, monkeypatch):
    """With no price row for the fallback there is no way to know the next call
    is cheaper on it — so the run keeps its hard stop."""
    import app.tools.run_budget as rb

    _budget_settings.agent_loop_budget_fallback_enabled = True

    async def _fake_target(db, *, current_model, user_id=None):
        return object(), "cheap-model"

    async def _no_price(db, *, model_id, provider):
        return None

    monkeypatch.setattr(
        "app.llm.router.resolve_fallback_model_provider", _fake_target
    )
    monkeypatch.setattr(rb, "resolve_budget_price", _no_price)

    assert await rb.resolve_budget_fallback(None, current_model="m") is None


@pytest.mark.asyncio
async def test_for_run_resolves_a_fallback_only_for_a_priced_cost_budget(
    _budget_settings, monkeypatch
):
    """A token/tool-call/duration budget cannot benefit from a cheaper model,
    and neither can an unpriced cost budget — neither should pay for the
    lookup."""
    import app.tools.run_budget as rb

    _budget_settings.agent_loop_budget_fallback_enabled = True
    _budget_settings.agent_loop_max_total_tokens = 100_000
    calls: list[str | None] = []

    async def _fake_fallback(db, *, current_model, overrides=None, user_id=None):
        calls.append(current_model)
        return BudgetFallbackModel(model_id="cheap-model", price=_price("0.1", "0.1"))

    monkeypatch.setattr(rb, "resolve_budget_fallback", _fake_fallback)

    budget = await rb.build_run_budget_for_run(None, model_id="m", provider="p")
    assert budget is not None and budget.fallback is None
    assert calls == [], "a token-only budget needs no fallback model"

    _budget_settings.agent_loop_max_cost_usd = 0.5

    async def _no_price(db, *, model_id, provider):
        return None

    monkeypatch.setattr(rb, "resolve_budget_price", _no_price)
    budget = await rb.build_run_budget_for_run(None, model_id="m", provider="p")
    assert budget is not None and budget.fallback is None
    assert calls == [], "an unenforceable cost budget needs no fallback either"

    async def _priced(db, *, model_id, provider):
        return _price()

    monkeypatch.setattr(rb, "resolve_budget_price", _priced)
    budget = await rb.build_run_budget_for_run(None, model_id="m", provider="p")
    assert budget is not None and budget.fallback is not None
    assert budget.fallback_available is True
    assert calls == ["m"]
