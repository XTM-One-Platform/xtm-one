# SPDX-License-Identifier: Apache-2.0

"""Risk figures are never compared across two currencies (#1383).

OpenCRQ prices in USD; the organization's thresholds are in its own currency.
The profile carries reference exchange rates (``fx_rates``) the risk agents
cite to convert, and the Risk Quantifier's output says which rate it used, or
``above_approval_threshold: null`` when none exists.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from app.services import organization_context as oc
from app.services import organization_context_injection as inj
from app.services.organization_context_proposals import _contract_paths

_SEED = Path(__file__).resolve().parents[1] / "app" / "agents" / "seed_data"
_SCHEMA = json.loads((_SEED / "agent_output_schema" / "ctem-risk-quantifier.json").read_text(encoding="utf-8"))
_PERSONA = (_SEED / "agent_persona" / "ctem-risk-quantifier.md").read_text(encoding="utf-8")
_DESK = (_SEED / "agent_persona" / "opencrq-risk-desk-orchestrator.md").read_text(encoding="utf-8")

_RATE = {
    "from_currency": "usd",
    "to_currency": "EUR",
    "rate": 0.9,
    "as_of": "2026-11-20",
    "source": "Central bank reference",
}


def _errors(output: dict[str, Any]) -> list[str]:
    Draft202012Validator.check_schema(_SCHEMA)
    return [error.message for error in Draft202012Validator(_SCHEMA).iter_errors(output)]


# ── Profile field ────────────────────────────────────────────────────────────


def test_a_reference_rate_is_normalised_and_resolved_for_the_profile_currency() -> None:
    profile = oc.parse_profile({"currency": "EUR", "fx_rates": [_RATE]})
    rate = oc.reference_rate(profile, "USD")
    assert rate is not None
    assert (rate.from_currency, rate.to_currency, rate.rate, rate.as_of) == ("USD", "EUR", 0.9, date(2026, 11, 20))


def test_the_latest_rate_wins_and_a_rate_into_another_currency_is_ignored() -> None:
    older = {**_RATE, "rate": 0.8, "as_of": "2026-10-01"}
    other = {**_RATE, "to_currency": "GBP", "rate": 0.7, "as_of": "2026-11-22"}
    profile = oc.parse_profile({"currency": "EUR", "fx_rates": [older, _RATE, other]})
    rate = oc.reference_rate(profile, "USD")
    assert rate is not None
    assert rate.rate == 0.9


@pytest.mark.parametrize(
    ("data", "source"),
    [
        ({"currency": "EUR"}, "USD"),
        ({"currency": "", "fx_rates": [_RATE]}, "USD"),
        ({"currency": "USD", "fx_rates": [_RATE]}, "USD"),
    ],
)
def test_no_rate_resolves_to_none(data: dict[str, Any], source: str) -> None:
    assert oc.reference_rate(oc.parse_profile(data), source) is None


@pytest.mark.parametrize(
    "bad",
    [
        {**_RATE, "rate": 0},
        {**_RATE, "rate": -1},
        {**_RATE, "from_currency": "dollars"},
        {**_RATE, "to_currency": "USD", "from_currency": "USD"},
        {**_RATE, "source": ""},
        {k: v for k, v in _RATE.items() if k != "as_of"},
    ],
)
def test_a_bad_rate_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        oc.parse_profile({"currency": "EUR", "fx_rates": [bad]})


def test_the_rates_are_not_a_contract_field_and_an_empty_list_dumps_as_before() -> None:
    assert "fx_rates" not in dict(oc.CONTRACT_FIELDS)
    assert "fx_rates" not in oc.legacy_dump(oc.parse_profile({"currency": "EUR"}))
    assert oc.legacy_dump(oc.parse_profile({"currency": "EUR", "fx_rates": [_RATE]}))["fx_rates"][0]["rate"] == 0.9


def test_an_agent_may_set_the_rates_on_the_primary_profile() -> None:
    assert "fx_rates" in _contract_paths("primary")


def test_the_always_on_block_carries_the_rate_only_when_one_is_set() -> None:
    class Row:
        version = 3
        profile: dict[str, Any] = {"name": "Acme", "currency": "EUR", "fx_rates": [_RATE]}

    text = inj.render_summary(Row())
    assert "1 USD = 0.9 EUR (2026-11-20, Central bank reference)" in text
    Row.profile = {"name": "Acme", "currency": "EUR"}
    assert "fx_rates" not in inj.render_summary(Row())


def test_the_rates_are_not_a_category_of_the_index() -> None:
    from app.services import organization_context_categories as cats

    assert "fx_rates" not in cats.CATEGORY_NAMES


# ── Output schema: the three acceptance cases ───────────────────────────────

_BASE = {"name": "Ransomware on billing", "opencrq_id": "s-1", "asset": "Billing", "draft": False}


def test_eur_with_a_rate_converts_and_cites_the_rate() -> None:
    # 220,000 USD at 0.9 is 198,000 EUR: under a 200,000 EUR approval threshold.
    scenario = {
        **_BASE,
        "currency": "USD",
        "expected_loss_before": 220000,
        "threshold_currency": "EUR",
        "expected_loss_before_converted": 198000,
        "fx_rate": 0.9,
        "fx_rate_date": "2026-11-20",
        "fx_source": "Central bank reference",
        "above_approval_threshold": False,
        "assumptions": [],
    }
    assert _errors({"scenarios": [scenario]}) == []


def test_eur_with_no_rate_leaves_the_flag_null() -> None:
    scenario = {
        **_BASE,
        "currency": "USD",
        "expected_loss_before": 220000,
        "threshold_currency": "EUR",
        "fx_rate": None,
        "above_approval_threshold": None,
        "assumptions": ["no USD to EUR reference rate in the organization profile"],
    }
    assert _errors({"scenarios": [scenario]}) == []


def test_usd_needs_no_conversion() -> None:
    scenario = {
        **_BASE,
        "currency": "USD",
        "expected_loss_before": 220000,
        "threshold_currency": "USD",
        "above_approval_threshold": True,
        "assumptions": [],
    }
    assert _errors({"scenarios": [scenario]}) == []


def test_the_flag_and_the_threshold_currency_are_required() -> None:
    scenario = {**_BASE, "currency": "USD", "assumptions": []}
    errors = _errors({"scenarios": [scenario]})
    assert any("above_approval_threshold" in e for e in errors)
    assert any("threshold_currency" in e for e in errors)


def test_an_unknown_field_is_refused() -> None:
    scenario = {
        **_BASE,
        "currency": "USD",
        "threshold_currency": "EUR",
        "above_approval_threshold": None,
        "assumptions": [],
        "exchange": 1.1,
    }
    assert _errors({"scenarios": [scenario]})


# ── Personas ────────────────────────────────────────────────────────────────


def test_the_personas_forbid_a_comparison_across_currencies_and_a_guessed_rate() -> None:
    for persona in (_PERSONA, _DESK):
        lowered = persona.lower()
        assert "never compare two currencies" in lowered
        assert "fx_rates" in persona
        assert "own estimate" in lowered
    assert "`null` (never `false`)" in _PERSONA
    for field in ("fx_rate", "fx_rate_date", "fx_source", "threshold_currency"):
        assert f"`{field}`" in _PERSONA
