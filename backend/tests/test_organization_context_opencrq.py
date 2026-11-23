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
    assert set(summary) == {
        "linked",
        "pushed_to_opencrq",
        "pulled_from_opencrq",
        "ambiguous",
        "not_in_opencrq",
        "kept_below_sync_threshold",
        "in_sync",
        "min_criticality",
        "assets_read",
        "by_criticality",
    }
    assert summary["in_sync"] is False


def test_an_empty_profile_against_opencrq_is_proposed_the_import_never_already_agree() -> None:
    # Sam's case (#1134): every jewel deleted, OpenCRQ holds high assets; the plan must not read "agree".
    plan = plan_sync(
        _profile(),
        [
            {"id": "a1", "name": "ERP", "criticality": "high"},
            {"id": "a2", "name": "SCADA", "criticality": "very_high"},
            {"id": "a3", "name": "Wiki", "criticality": "medium"},
        ],
    )
    assert [a for a, _ in plan.pulled] == ["a1", "a2"]
    rows = {j.name: (j.criticality, j.source, j.external_id) for j in plan.profile.crown_jewels}
    assert rows == {"ERP": ("high", "opencrq", "a1"), "SCADA": ("critical", "opencrq", "a2")}
    assert plan.profile_changed and not plan.in_sync


def test_the_reconcile_pulls_by_the_sync_threshold() -> None:
    assets = [{"id": "a1", "name": "ERP", "criticality": "medium"}]
    assert plan_sync(_profile(), assets).pulled == []
    assert plan_sync(_profile(), assets, "medium").pulled == [("a1", "ERP")]
    assert plan_sync(_profile(), assets, "bogus").pulled == []


def test_a_profile_matching_opencrq_is_in_sync() -> None:
    plan = plan_sync(
        _profile({"name": "ERP", "criticality": "high", "opencrq_asset_id": "a1"}),
        [{"id": "a1", "name": "ERP", "criticality": "high"}],
    )
    assert plan.in_sync and plan.summary()["in_sync"] is True


def test_an_empty_plan_over_unrated_assets_names_the_threshold_and_the_counts() -> None:
    # #1558: 0 jewels and every OpenCRQ asset rated "unknown" - the plan is empty and says why.
    assets = [
        {"id": "a1", "name": "ERP", "criticality": "unknown"},
        {"id": "a2", "name": "Wiki", "criticality": None},
        {"id": "a3", "name": "CRM", "criticality": "medium"},
    ]
    plan = plan_sync(_profile(), assets)
    summary = plan.summary()
    assert plan.pulled == [] and summary["in_sync"] is True
    assert summary["min_criticality"] == "high"
    assert summary["assets_read"] == 3
    assert summary["by_criticality"] == {"unknown": 2, "medium": 1}


def test_a_medium_asset_is_pulled_at_the_medium_threshold_and_the_summary_names_it() -> None:
    plan = plan_sync(_profile(), [{"id": "a1", "name": "ERP", "criticality": "medium"}], "medium")
    assert plan.pulled == [("a1", "ERP")]
    assert plan.summary()["min_criticality"] == "medium"


def test_an_unknown_threshold_is_reported_as_the_default_it_fell_back_to() -> None:
    assert plan_sync(_profile(), [], "bogus").summary()["min_criticality"] == "high"
