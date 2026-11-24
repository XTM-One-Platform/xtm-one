"""Organization profile fields agents can set and read (#1773).

PIR details, asset group criticality and membership rules, confirmation of an
unchanged value with its provenance, and the missing reference rate named to
the agent. No database: the models and the pure functions.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import organization_context as oc
from app.services import organization_context_categories as cats
from app.services import organization_context_injection as inj
from app.services import organization_context_proposals as proposals

PRIMARY = SimpleNamespace(role="primary")


def _row(profile: dict[str, Any]) -> Any:
    from datetime import UTC, datetime

    return SimpleNamespace(
        profile=profile, version=3, created_at=datetime(2026, 11, 24, tzinfo=UTC), source="manual", organization_id=None
    )


def test_pir_details_are_settable_and_read_with_the_pir() -> None:
    kept = proposals.validate_field_changes(
        PRIMARY,
        {"pir_ids": ["p1"]},
        [
            {
                "field": "pir_details",
                "value": {
                    "p1": {
                        "question": "Who targets our payment rails?",
                        "owner": "CTI lead",
                        "review_cadence": "quarterly",
                        "crown_jewels": ["Payments"],
                    }
                },
                "basis": "PIR workshop",
            }
        ],
    )
    assert kept[0]["value"]["p1"]["review_cadence"] == "quarterly"
    profile = oc.parse_profile(
        {"pir_ids": ["p1", "p2"], "pir_names": {"p1": "Payments"}, "pir_details": kept[0]["value"]}
    )
    pirs = cats.get("pirs")
    assert pirs is not None
    rows = cats.rows_of(profile, pirs)
    assert rows[0] == {
        "id": "p1",
        "name": "Payments",
        "question": "Who targets our payment rails?",
        "owner": "CTI lead",
        "review_cadence": "quarterly",
        "crown_jewels": ["Payments"],
    }
    assert rows[1] == {"id": "p2", "name": ""}


def test_a_bad_pir_cadence_is_refused() -> None:
    with pytest.raises(proposals.ProposalError, match="pir_details"):
        proposals.validate_field_changes(
            PRIMARY, {}, [{"field": "pir_details", "value": {"p1": {"review_cadence": "daily"}}}]
        )


def test_a_member_of_unknown_criticality_takes_its_groups() -> None:
    profile = oc.parse_profile(
        {
            "assets_exposure": [
                {"name": "web-1", "group": "DMZ"},
                {"name": "db-payments", "tags": ["pci"]},
                {"name": "laptop-7"},
                {"name": "core-router", "criticality": "low", "group": "DMZ"},
            ],
            "asset_groups": [
                {"name": "DMZ", "criticality": "high"},
                {"name": "PCI", "criticality": "very_high", "membership_rule": {"tags": ["PCI"]}},
            ],
        }
    )
    by_name = {a.name: a for a in profile.assets_exposure}
    assert oc.effective_criticality(profile, by_name["web-1"]) == "high"
    assert [g.name for g in oc.groups_of(profile, by_name["db-payments"])] == ["PCI"]
    assert oc.effective_criticality(profile, by_name["db-payments"]) == "very_high"
    assert oc.effective_criticality(profile, by_name["laptop-7"]) == "unknown"
    assert oc.effective_criticality(profile, by_name["core-router"]) == "low", "the asset's own value wins"


def test_an_empty_membership_rule_matches_nothing() -> None:
    rule = oc.GroupMembershipRule()
    assert rule.empty()
    assert not rule.matches(oc.AssetEntry(name="anything"))
    assert oc.GroupMembershipRule(name_contains="PAY").matches(oc.AssetEntry(name="db-payments"))
    assert oc.GroupMembershipRule(categories=["identity"]).matches(oc.AssetEntry(name="x", category="identity"))


def test_an_unchanged_value_is_confirmed_on_request_only() -> None:
    with pytest.raises(proposals.ProposalError, match="confirm: true"):
        proposals.validate_field_changes(PRIMARY, {"sector": "Retail"}, [{"field": "sector", "value": "Retail"}])
    kept = proposals.validate_field_changes(
        PRIMARY,
        {"sector": "Retail"},
        [{"field": "sector", "value": "Retail", "basis": "annual review", "confirm": True}],
    )
    assert kept[0]["confirmed"] is True
    assert kept[0]["conflict"] is False
    with pytest.raises(proposals.ProposalError, match="empty"):
        proposals.validate_field_changes(PRIMARY, {}, [{"field": "sector", "value": "", "confirm": True}])


def test_provenance_is_kept_for_every_settable_path_with_the_confirmation_time() -> None:
    kept = oc.accepted_sources(
        {
            "fx_rates": {"source": "agent:risk", "basis": "ECB"},
            "prioritization_policy.scope_min_criticality": {"source": "agent:ctem", "basis": "b"},
            "sector": {"source": "agent:x", "basis": "b", "confirmed_at": "2026-11-24T08:00:00+00:00"},
            "not_a_field": {"source": "y"},
        }
    )
    assert kept is not None
    assert set(kept) == {"fx_rates", "prioritization_policy.scope_min_criticality", "sector"}
    assert kept["sector"]["confirmed_at"] == "2026-11-24T08:00:00+00:00"
    assert "confirmed_at" not in kept["fx_rates"]


def test_a_currency_without_a_rate_into_it_is_named_to_the_agent() -> None:
    profile = {"name": "Acme", "currency": "EUR"}
    assert "No reference exchange rate into EUR" in inj.render_summary(_row(profile))
    assert "No reference exchange rate into EUR" in oc.render_for_agent(_row(profile))
    rate = {
        "from_currency": "USD",
        "to_currency": "EUR",
        "rate": 0.92,
        "as_of": date(2026, 11, 1).isoformat(),
        "source": "ECB",
    }
    assert "No reference exchange rate" not in inj.render_summary(_row({**profile, "fx_rates": [rate]}))
    assert "No reference exchange rate" not in oc.render_for_agent(_row({"name": "Acme"}))
