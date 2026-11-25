"""#2129: an agent writes every scalar rule of the prioritization policy, one dotted path each.

Before, only ``prioritization_policy.scope_min_criticality`` was a settable path, so an
administrator's agent could not record a CISO's thresholds, KEV override or time to fix.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import organization_context as org_context
from app.services import organization_context_proposals as proposals


def _primary() -> SimpleNamespace:
    return SimpleNamespace(role="primary", name="Acme")


def test_every_scalar_policy_rule_is_a_settable_path_of_the_primary_profile() -> None:
    paths = proposals._contract_paths("primary")
    for path in org_context.POLICY_SETTABLE_PATHS:
        assert path in paths
        assert path in org_context.PROVENANCE_EXTRA_PATHS
    assert "prioritization_policy.sla_hours" in paths
    assert "prioritization_policy.kev_always_in" in paths
    assert not any(p.startswith("prioritization_policy.") for p in proposals._contract_paths("tracked"))


def test_each_settable_path_names_a_field_of_the_policy_model() -> None:
    fields = set(org_context.PrioritizationPolicy.model_fields)
    for path in org_context.POLICY_SETTABLE_PATHS:
        head, _, tail = path.partition(".")
        assert head == "prioritization_policy"
        assert tail in fields


def test_a_policy_rule_change_validates_and_normalises() -> None:
    current = org_context.OrganizationProfile().model_dump(mode="json")
    kept = proposals.validate_field_changes(
        _primary(),
        current,
        [
            {"field": "prioritization_policy.kev_always_in", "value": False, "basis": "CISO policy"},
            {"field": "prioritization_policy.cvss_min", "value": 7.0, "basis": "CISO policy"},
        ],
    )
    assert [k["field"] for k in kept] == ["prioritization_policy.kev_always_in", "prioritization_policy.cvss_min"]
    assert kept[1]["value"] == 7.0


def test_an_invalid_policy_value_is_refused_with_its_path() -> None:
    current = org_context.OrganizationProfile().model_dump(mode="json")
    with pytest.raises(proposals.ProposalError, match=r"prioritization_policy\.cvss_min"):
        proposals.validate_field_changes(
            _primary(),
            current,
            [{"field": "prioritization_policy.cvss_min", "value": "very high", "basis": "b"}],
        )
