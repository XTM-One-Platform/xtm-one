"""Row-level writes on the organization profile and the Intune mapping (#11888).

Pure: the plans are computed on a profile in memory, so the rules hold without
a database: hand rows never touched, a re-sync updates its own rows only,
fields the source does not send are kept, refusals carry their reason.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services import organization_context as oc
from app.services import organization_context_rows as rows

#: Two Intune managed devices as Graph answers them (trimmed), plus detected apps.
_DEVICES = [
    {
        "id": "d-1",
        "deviceName": "LAPTOP-ALICE",
        "operatingSystem": "Windows",
        "chassisType": "laptop",
        "complianceState": "compliant",
        "userDisplayName": "Alice Martin",
    },
    {
        "id": "d-2",
        "deviceName": "iPhone-Bob",
        "operatingSystem": "iOS",
        "complianceState": "noncompliant",
        "userDisplayName": "Bob Durand",
    },
]
_APPS = [
    {"displayName": "Microsoft Defender for Endpoint"},
    {"displayName": "CrowdStrike Windows Sensor"},
    {"displayName": "Google Chrome"},
    {"displayName": "CrowdStrike Falcon Sensor"},
]


def _profile(**kwargs: Any) -> oc.OrganizationProfile:
    return oc.OrganizationProfile(**kwargs)


def _saved(plan: rows.RowPlan) -> oc.OrganizationProfile:
    assert plan.profile is not None
    return plan.profile


def test_an_intune_device_maps_to_the_platform_taxonomy() -> None:
    laptop = rows.intune_device_row(_DEVICES[0])
    assert laptop["category"] == "host" and laptop["subcategory"] == "laptop"
    assert laptop["external_id"] == "d-1"
    assert "compliance:compliant" in laptop["tags"] and "os:windows" in laptop["tags"]
    assert "owner" not in laptop
    phone = rows.intune_device_row(_DEVICES[1], include_owner=True)
    assert (phone["category"], phone["subcategory"]) == ("mobile_device", "smartphone")
    assert phone["owner"] == "Bob Durand"
    oc.AssetEntry.model_validate({**phone, "source": "intune"})


def test_detected_apps_keep_security_products_only_once() -> None:
    found = rows.intune_security_products(_APPS)
    assert [r["product"] for r in found] == ["Microsoft Defender", "CrowdStrike Falcon"]


def test_upsert_adds_then_updates_its_own_rows_and_never_a_hand_row() -> None:
    hand = oc.AssetEntry(name="LAPTOP-ALICE", category="host", source="manual")
    base = _profile(assets_exposure=[hand])
    mapped = rows.map_rows(_DEVICES, "intune_device")
    plan = rows.plan_upsert(base, "assets", "Intune", mapped)
    # The hand row named alike is never touched: Intune's device is a second record of it (#1178, S3),
    # added beside it for the canonical asset store to merge.
    assert plan.added == ["LAPTOP-ALICE", "iPhone-Bob"] and not plan.refused
    assert base.assets_exposure == [hand]  # the input profile is untouched
    assert _saved(plan).assets_exposure[0] == hand
    alice, synced = _saved(plan).assets_exposure[1:]
    assert alice.source == "intune" and alice.name == "LAPTOP-ALICE"
    assert synced.source == "intune" and synced.external_id == "d-2"

    # A person sets the owner and environment on the synced row; the next sync keeps them.
    edited = _profile(assets_exposure=[hand, synced.model_copy(update={"owner": "IT", "environment": "production"})])
    again = rows.plan_upsert(edited, "assets", "intune", rows.map_rows(_DEVICES[1:], "intune_device"))
    assert again.unchanged == ["iPhone-Bob"] and not again.changed
    moved = [{**_DEVICES[1], "complianceState": "compliant"}]
    third = rows.plan_upsert(edited, "assets", "intune", rows.map_rows(moved, "intune_device"))
    assert third.updated == ["iPhone-Bob"]
    after = _saved(third).assets_exposure[1]
    assert after.owner == "IT" and after.environment == "production" and "compliance:compliant" in after.tags


def test_bad_rows_are_refused_with_their_reason_and_the_rest_goes_on() -> None:
    plan = rows.plan_upsert(
        _profile(),
        "assets",
        "cmdb",
        [
            {"name": "srv-1", "external_id": "1", "category": "host", "subcategory": "smartphone"},
            {"name": "srv-2"},
            {"name": "srv-3", "external_id": "3", "category": "host", "subcategory": "server"},
            {"name": "srv-3", "external_id": "3"},
        ],
    )
    reasons = dict(plan.refused)
    assert "not a subcategory of host" in reasons["srv-1"]
    assert "external_id is required" in reasons["srv-2"]
    assert "sent twice" in reasons["srv-3"]
    assert plan.added == ["srv-3"]


@pytest.mark.parametrize("source", ["", "manual", "CSV", "api"])
def test_a_hand_source_or_no_source_is_refused_for_sourced_sections(source: str) -> None:
    with pytest.raises(rows.RowError):
        rows.plan_upsert(_profile(), "assets", source, [])


def test_tech_stack_and_ai_systems_add_new_names_and_never_overwrite() -> None:
    base = _profile(tech_stack=[oc.TechStackEntry(product="Microsoft Defender", vendor="MS", cpe="cpe:x")])
    plan = rows.plan_upsert(base, "tech_stack", "intune", rows.map_rows(_APPS, "intune_detected_app"))
    assert plan.unchanged == ["Microsoft Defender"] and plan.added == ["CrowdStrike Falcon"]
    assert _saved(plan).tech_stack[0].cpe == "cpe:x"
    ai = rows.plan_upsert(_profile(), "ai_systems", "", [{"name": "Copilot", "kind": "robot"}])
    assert ai.refused and "kind" in ai.refused[0][1]


def test_remove_drops_the_sources_rows_only() -> None:
    base = _profile(
        assets_exposure=[
            oc.AssetEntry(name="a", source="intune", external_id="d-1"),
            oc.AssetEntry(name="b", source="manual", external_id="d-1"),
            oc.AssetEntry(name="c", source="tanium", external_id="d-2"),
        ]
    )
    plan = rows.plan_remove(base, "assets", "intune", ["d-1", "d-2"])
    assert plan.removed == ["a"]
    assert [r.name for r in _saved(plan).assets_exposure] == ["b", "c"]
    assert [ext for ext, _ in plan.refused] == ["d-2"]
    with pytest.raises(rows.RowError):
        rows.plan_remove(base, "tech_stack", "intune", ["x"])


def test_a_batch_over_the_cap_and_an_unknown_format_are_refused() -> None:
    with pytest.raises(rows.RowError):
        rows.plan_upsert(_profile(), "assets", "intune", [{}] * (rows.MAX_BATCH + 1))
    with pytest.raises(rows.RowError):
        rows.map_rows([], "jamf_device")
    assert rows.plan_upsert(_profile(), "assets", "intune", []).as_dict()["counts"]["added"] == 0
