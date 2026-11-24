# SPDX-License-Identifier: Apache-2.0
"""The Cases attention points in the organization profile (#1176 slice 2b)."""

import pytest
from pydantic import ValidationError

from app.services import organization_context as oc


def test_the_defaults_are_the_shipped_ranking():
    weights = oc.OrganizationProfile().case_attention
    assert (weights.priority_critical, weights.priority_high, weights.priority_medium, weights.priority_low) == (
        40,
        25,
        10,
        0,
    )
    assert (weights.kev, weights.crown_jewel, weights.overdue, weights.due_soon) == (15, 15, 20, 10)
    assert (weights.due_soon_days, weights.no_owner, weights.age_per_day, weights.age_max) == (3, 5, 1, 10)


def test_saved_points_are_read_back():
    profile = oc.parse_profile({"name": "Acme", "case_attention": {"kev": 30, "due_soon_days": 7}})
    assert profile.case_attention.kev == 30
    assert profile.case_attention.due_soon_days == 7
    assert profile.case_attention.priority_critical == 40


@pytest.mark.parametrize("bad", [{"kev": -1}, {"kev": 101}, {"due_soon_days": 0}, {"unknown": 1}])
def test_out_of_bounds_or_unknown_points_are_refused(bad):
    with pytest.raises(ValidationError):
        oc.CaseAttentionWeights(**bad)


def test_the_points_stay_out_of_the_legacy_dump_and_the_agent_block():
    assert "case_attention" in oc.EXPOSURE_FIELDS
    assert "case_attention" not in dict(oc.CONTRACT_FIELDS)
    profile = oc.parse_profile({"name": "Acme", "case_attention": {"kev": 30}})
    assert "case_attention" not in oc.legacy_dump(profile)
