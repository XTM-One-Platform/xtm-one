# SPDX-License-Identifier: Apache-2.0

"""Asset freshness from OpenAEV on the profile's asset rows (#1134 S4, CTEM-27)."""

from datetime import UTC, datetime
from typing import Any

from app.services import organization_asset_identity as identity
from app.services.organization_context import AssetEntry, OrganizationProfile
from app.services.organization_context_import import ReadResult, openaev_asset, openaev_last_seen, plan_import


def _endpoint(**extra: Any) -> dict[str, Any]:
    return {"asset_id": "a-1", "asset_name": "web-01", "endpoint_platform": "Linux", **extra}


def test_openaev_asset_carries_last_seen_and_status() -> None:
    row = openaev_asset(_endpoint(asset_last_seen="2026-11-20T08:15:30.123Z", asset_status="STALE"))
    assert row is not None
    assert row.last_seen == "2026-11-20T08:15:30Z"
    assert row.status == "stale"


def test_openaev_asset_without_freshness_keeps_defaults() -> None:
    row = openaev_asset(_endpoint())
    assert row is not None
    assert row.last_seen == ""
    assert row.status == ""


def test_unknown_status_is_dropped() -> None:
    row = openaev_asset(_endpoint(asset_status="SOMETHING_NEW"))
    assert row is not None and row.status == ""


def test_last_seen_reads_epoch_and_naive_and_refuses_garbage() -> None:
    assert openaev_last_seen(0) == "1970-01-01T00:00:00Z"
    assert openaev_last_seen("2026-11-20T08:15:30") == "2026-11-20T08:15:30Z"
    assert openaev_last_seen("2026-11-20T10:15:30+02:00") == "2026-11-20T08:15:30Z"
    assert openaev_last_seen("not a date") == ""
    assert openaev_last_seen(None) == ""


def test_profile_from_before_freshness_still_loads() -> None:
    profile = OrganizationProfile.model_validate(
        {"assets_exposure": [{"name": "db-01", "category": "host", "source": "openaev", "external_id": "x"}]}
    )
    assert profile.assets_exposure[0].last_seen == ""
    assert profile.assets_exposure[0].status == ""


def test_a_sync_run_moves_freshness_with_the_source() -> None:
    before = AssetEntry(
        name="web-01",
        category="host",
        source="openaev",
        external_id="a-1",
        last_seen="2026-11-01T00:00:00Z",
        status="active",
    )
    profile = OrganizationProfile(assets_exposure=[before])
    fresh = openaev_asset(_endpoint(asset_last_seen="2026-11-20T08:15:30Z", asset_status="STALE"))
    plan = plan_import(profile, "assets", "openaev", ReadResult(rows=[fresh], total=1), sync_id="s-1")
    assert plan.profile is not None
    row = plan.profile.assets_exposure[0]
    assert row.last_seen == "2026-11-20T08:15:30Z"
    assert row.status == "stale"
    assert plan.updated == ["web-01"]


def test_section_record_reads_last_seen() -> None:
    record = identity.section_record(
        "assets", {"name": "web-01", "source": "openaev", "external_id": "a-1", "last_seen": "2026-11-20T08:15:30Z"}
    )
    assert record.last_seen == datetime(2026, 11, 20, 8, 15, 30, tzinfo=UTC)
    assert identity.section_record("assets", {"name": "x", "last_seen": "bad"}).last_seen is None
    assert identity.section_record("assets", {"name": "x"}).last_seen is None
