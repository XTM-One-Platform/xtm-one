"""OpenCRQ primary-asset sync of the organization profile (#8987 slice 3d, #1591): the pure plan.

The rows are OpenCRQ PRIMARY assets, rated by their four security needs; nothing is pushed to OpenCRQ.
"""

from __future__ import annotations

from app.services import organization_context as oc
from app.services.organization_context_opencrq import plan_sync


def _profile(*jewels: dict) -> oc.OrganizationProfile:
    return oc.parse_profile({"primary_assets": list(jewels)})


def _primary(asset_id: str, name: str, *needs: str) -> dict[str, str]:
    """An OpenCRQ primary asset whose availability, integrity, ... needs are *needs*, in that order."""
    keys = ("availability", "integrity", "confidentiality", "traceability")
    return {"id": asset_id, "name": name, **dict(zip(keys, needs, strict=False))}


def test_a_row_named_like_one_primary_asset_is_linked_and_nothing_is_pushed() -> None:
    plan = plan_sync(
        _profile({"name": "SCADA", "criticality": "critical"}),
        [_primary("a1", "scada", "medium")],
    )
    assert plan.profile.primary_assets[0].opencrq_asset_id == "a1"
    assert plan.linked == [("SCADA", "a1")]
    assert plan.pushed == [] and plan.released == []
    assert plan.profile_changed


def test_a_linked_row_is_left_as_it_is() -> None:
    plan = plan_sync(
        _profile({"name": "ERP", "criticality": "high", "opencrq_asset_id": "a2"}),
        [_primary("a2", "ERP", "low")],
    )
    assert plan.pushed == [] and plan.linked == [] and not plan.profile_changed
    assert plan.profile.primary_assets[0].criticality == "high"


def test_a_name_matching_two_primary_assets_is_reported_never_guessed() -> None:
    plan = plan_sync(
        _profile({"name": "Billing"}),
        [_primary("b1", "Billing"), _primary("b2", "billing")],
    )
    assert plan.ambiguous == ["Billing"]
    assert plan.profile.primary_assets[0].opencrq_asset_id == ""


def test_a_primary_asset_no_row_names_joins_the_profile_rated_by_its_highest_need() -> None:
    plan = plan_sync(
        _profile({"name": "SCADA", "opencrq_asset_id": "a1", "criticality": "critical"}),
        [
            _primary("a1", "SCADA", "very_high"),
            _primary("a3", "Payment", "low", "very_high", "medium"),
            _primary("a4", "Wiki", "low"),
        ],
    )
    assert plan.pulled == [("a3", "Payment"), ("a4", "Wiki")]
    rows = {j.name: (j.criticality, j.source, j.external_id) for j in plan.profile.primary_assets}
    assert rows["Payment"] == ("critical", "opencrq", "a3")
    assert rows["Wiki"] == ("low", "opencrq", "a4")


def test_a_primary_asset_with_no_need_rated_is_pulled_as_high() -> None:
    plan = plan_sync(_profile(), [_primary("a1", "Order to cash")])
    assert [(j.name, j.criticality) for j in plan.profile.primary_assets] == [("Order to cash", "high")]


def test_a_typed_row_whose_primary_asset_is_gone_is_reported_and_kept() -> None:
    # A synced row whose id OpenCRQ no longer holds is the import's (gone or superseded); a typed link is reported.
    plan = plan_sync(_profile({"name": "Old CRM", "source": "manual", "opencrq_asset_id": "gone"}), [])
    assert plan.stale == [("Old CRM", "gone")]
    assert [j.name for j in plan.profile.primary_assets] == ["Old CRM"]


def test_a_row_an_older_build_keyed_on_a_supporting_asset_is_left_to_the_import() -> None:
    # Before #1591 the import keyed rows on SUPPORTING assets; the reconcile neither reports nor links them.
    plan = plan_sync(
        _profile({"name": "Mail server", "source": "opencrq", "external_id": "s1", "opencrq_asset_id": "s1"}),
        [_primary("p1", "Mail")],
    )
    assert plan.stale == []
    assert [j.name for j in plan.profile.primary_assets] == ["Mail server", "Mail"]


def test_the_plan_leaves_the_input_profile_untouched_and_summarizes() -> None:
    profile = _profile({"name": "SCADA"})
    plan = plan_sync(profile, [_primary("a1", "SCADA", "high")])
    assert profile.primary_assets[0].opencrq_asset_id == ""
    summary = plan.summary()
    assert summary["linked"] == [{"primary_asset": "SCADA", "opencrq_asset_id": "a1"}]
    # Kept in the summary for API compatibility; always empty since #1591.
    assert summary["pushed_to_opencrq"] == [] and summary["kept_below_sync_threshold"] == []
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


def test_an_empty_profile_against_opencrq_is_proposed_every_primary_asset() -> None:
    plan = plan_sync(
        _profile(),
        [_primary("a1", "ERP", "high"), _primary("a2", "SCADA", "very_high"), _primary("a3", "Wiki", "medium")],
    )
    assert [a for a, _ in plan.pulled] == ["a1", "a2", "a3"]
    assert plan.profile_changed and not plan.in_sync


def test_the_reconcile_pulls_by_the_sync_threshold() -> None:
    assets = [_primary("a1", "ERP", "medium")]
    assert plan_sync(_profile(), assets).pulled == [("a1", "ERP")]
    assert plan_sync(_profile(), assets, "high").pulled == []
    assert plan_sync(_profile(), assets, "medium").pulled == [("a1", "ERP")]
    # An unknown threshold falls back to the default: every primary asset.
    assert plan_sync(_profile(), assets, "bogus").pulled == [("a1", "ERP")]


def test_a_profile_matching_opencrq_is_in_sync() -> None:
    plan = plan_sync(
        _profile({"name": "ERP", "criticality": "high", "opencrq_asset_id": "a1"}),
        [_primary("a1", "ERP", "high")],
    )
    assert plan.in_sync and plan.summary()["in_sync"] is True


def test_the_summary_counts_the_primary_assets_by_highest_need() -> None:
    plan = plan_sync(
        _profile(),
        [_primary("a1", "ERP"), _primary("a2", "Wiki", "low", "medium"), _primary("a3", "CRM", "medium")],
        "high",
    )
    summary = plan.summary()
    # a1 has no need rated: always kept, pulled as high; the medium ones fall below the threshold.
    assert plan.pulled == [("a1", "ERP")]
    assert summary["min_criticality"] == "high"
    assert summary["assets_read"] == 3
    assert summary["by_criticality"] == {"unknown": 1, "medium": 2}


def test_an_unknown_threshold_is_reported_as_the_default_it_fell_back_to() -> None:
    assert plan_sync(_profile(), [], "bogus").summary()["min_criticality"] == ""
