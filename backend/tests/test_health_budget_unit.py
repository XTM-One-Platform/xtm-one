"""Platform Concierge level 1 (#8254), without a database: the budget's
arithmetic. Against PostgreSQL: ``test_health_budget_postgres.py``.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.health import budget
from app.services.health.settings import ConciergeBudgetCaps

# ── The budget's arithmetic ───────────────────────────────────────────────


def _row(**values):
    zero = {"spent_tokens": 0, "reserved_tokens": 0, "spent_cost_usd": Decimal(0), "reserved_cost_usd": Decimal(0)}
    return SimpleNamespace(**{**zero, **values})


CAPS = ConciergeBudgetCaps(cycle_tokens=100, cycle_cost_usd=Decimal("1"), month_tokens=1000, month_cost_usd=Decimal("10"))


def test_a_reservation_that_would_pass_a_cap_is_refused_and_one_that_reaches_it_is_not() -> None:
    assert budget._cap_refusal(CAPS, _row(), _row(spent_tokens=60), tokens=40) is None
    assert budget._cap_refusal(CAPS, _row(), _row(spent_tokens=60), tokens=41) == "cycle_budget_reached"
    assert budget._cap_refusal(CAPS, _row(reserved_tokens=990), _row(), tokens=11) == "month_budget_reached"
    assert (
        budget._cap_refusal(CAPS, _row(), _row(reserved_cost_usd=Decimal("0.5")), cost=Decimal("0.500001"))
        == "cycle_budget_reached"
    )


def test_with_nothing_more_a_reached_cap_refuses_new_work() -> None:
    assert budget._cap_refusal(CAPS, _row(spent_tokens=999), None) is None
    assert budget._cap_refusal(CAPS, _row(spent_tokens=1000), None) == "month_budget_reached"
    assert budget._cap_refusal(CAPS, _row(), _row(spent_cost_usd=Decimal("1"))) == "cycle_budget_reached"
    no_caps = ConciergeBudgetCaps(cycle_tokens=None, cycle_cost_usd=None, month_tokens=None, month_cost_usd=None)
    assert budget._cap_refusal(no_caps, _row(spent_tokens=10**12), _row(spent_tokens=10**12)) is None


def test_a_month_with_no_row_yet_has_used_nothing() -> None:
    # A cap of 0 stops the Concierge from the month's first cycle on.
    zero = ConciergeBudgetCaps(cycle_tokens=None, cycle_cost_usd=None, month_tokens=0, month_cost_usd=None)
    assert budget._cap_refusal(zero, None, None) == "month_budget_reached"
    assert budget._cap_refusal(CAPS, None, None) is None
    assert budget._cap_refusal(CAPS, None, None, tokens=1001) == "month_budget_reached"


@pytest.mark.parametrize("bad", [-1, True, 1.5])
def test_token_counts_and_costs_are_validated(bad) -> None:
    with pytest.raises(ValueError):
        budget._tokens(bad)
    with pytest.raises(ValueError):
        budget._cost(Decimal("-0.01"))
    with pytest.raises(ValueError):
        budget._cost("NaN")


def test_every_refusal_has_its_message() -> None:
    from typing import get_args

    assert set(get_args(budget.BudgetRefusalCode)) == set(budget.REFUSAL_MESSAGES)
    assert budget.BudgetRefusedError("month_budget_reached").message == (
        "Platform Health's budget for this month is reached."
    )
    assert budget.refusal_message("month_budget_reached", "{Jeeves}") == "{Jeeves}'s budget for this month is reached."
    assert budget.refusal_message("no_such_code", "Jeeves") is None


def test_a_reached_cap_refuses_even_a_zero_estimate_on_it() -> None:
    assert budget._cap_refusal(CAPS, _row(spent_cost_usd=Decimal("10")), None, tokens=5) == "month_budget_reached"
    assert budget._cap_refusal(CAPS, _row(), _row(spent_tokens=100), cost=Decimal("0.1")) == "cycle_budget_reached"
