"""OpenCRQ crown-jewel sync of the organization profile (#8987, slice 3d): the pure plan."""

from __future__ import annotations

from app.services import organization_context as oc
from app.services.organization_context_opencrq import plan_sync


def _profile(*jewels: dict) -> oc.OrganizationProfile:
    return oc.parse_profile({"crown_jewels": list(jewels)})


def test_a_jewel_named_like_one_asset_is_linked_and_its_criticality_pushed():
    plan = plan_sync(
        _profile({"name": "SCADA", "criticality": "critical"}),
        [{"id": "a1", "name": "scada", "criticality": "medium"}],
    )
    assert plan.profile.crown_jewels[0].opencrq_asset_id == "a1"
    assert plan.linked == [("SCADA", "a1")]
    assert plan.pushed == [("a1", "scada", "very_high")]
    assert plan.profile_changed


def test_an_asset_already_at_the_profile_criticality_is_not_written():
    plan = plan_sync(
        _profile({"name": "ERP", "criticality": "high", "opencrq_asset_id": "a2"}),
        [{"id": "a2", "name": "ERP", "criticality": "high"}],
    )
    assert plan.pushed == [] and plan.linked == [] and not plan.profile_changed


def test_a_name_matching_two_assets_is_reported_never_guessed():
    plan = plan_sync(
        _profile({"name": "Billing"}),
        [{"id": "b1", "name": "Billing"}, {"id": "b2", "name": "billing"}],
    )
    assert plan.ambiguous == ["Billing"]
    assert plan.profile.crown_jewels[0].opencrq_asset_id == ""
    assert plan.pushed == []


def test_a_very_high_asset_no_jewel_names_joins_the_profile():
    plan = plan_sync(
        _profile({"name": "SCADA", "opencrq_asset_id": "a1", "criticality": "critical"}),
        [
            {"id": "a1", "name": "SCADA", "criticality": "very_high"},
            {"id": "a3", "name": "Payment gateway", "criticality": "very_high"},
            {"id": "a4", "name": "Wiki", "criticality": "low"},
        ],
    )
    assert plan.pulled == [("a3", "Payment gateway")]
    pulled = plan.profile.crown_jewels[-1]
    assert (pulled.name, pulled.criticality, pulled.opencrq_asset_id) == ("Payment gateway", "critical", "a3")
    assert [j.name for j in plan.profile.crown_jewels] == ["SCADA", "Payment gateway"]


def test_a_jewel_whose_asset_is_gone_is_reported_and_kept():
    plan = plan_sync(_profile({"name": "Old CRM", "opencrq_asset_id": "gone"}), [])
    assert plan.stale == [("Old CRM", "gone")]
    assert [j.name for j in plan.profile.crown_jewels] == ["Old CRM"]


def test_the_plan_leaves_the_input_profile_untouched_and_summarizes():
    profile = _profile({"name": "SCADA"})
    plan = plan_sync(profile, [{"id": "a1", "name": "SCADA", "criticality": "high"}])
    assert profile.crown_jewels[0].opencrq_asset_id == ""
    summary = plan.summary()
    assert summary["linked"] == [{"crown_jewel": "SCADA", "opencrq_asset_id": "a1"}]
    assert summary["pushed_to_opencrq"] == []
    assert set(summary) == {"linked", "pushed_to_opencrq", "pulled_from_opencrq", "ambiguous", "not_in_opencrq"}
