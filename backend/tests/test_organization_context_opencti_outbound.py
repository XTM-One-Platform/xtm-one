# SPDX-License-Identifier: Apache-2.0

"""Outbound assets + software to OpenCTI (#1982 S3): the pure plan."""

from __future__ import annotations

from typing import Any

from app.services import organization_context as oc
from app.services import organization_context_opencti_outbound as cti
from app.services import organization_context_outbound as outbound


def _profile(*assets: dict[str, Any]) -> oc.OrganizationProfile:
    return oc.parse_profile({"name": "Acme", "assets_exposure": list(assets)})


WEB = {"name": "web-01", "category": "host", "criticality": "high", "environment": "production", "tags": ["dmz"]}
DB = {"name": "db-01", "category": "host", "criticality": "low", "tags": ["core"]}
NGINX = {"vendor": "F5", "product": "nginx", "version": "1.25.3", "cpe": "cpe:2.3:a:f5:nginx:1.25.3:*:*:*:*:*:*:*"}


def test_assets_can_sync_outbound_to_opencti() -> None:
    assert outbound.supports("assets", "opencti", outbound.OUTBOUND)
    assert outbound.directions("assets", "opencti")[1] == ""


def test_first_run_creates_systems_software_and_uses() -> None:
    plan = cti.plan_opencti_outbound(_profile(WEB, DB), {"web-01": [NGINX]}, {}, {})
    assert plan.platform == "opencti"
    assert [n for n, _ in plan.create] == ["web-01", "db-01"]
    body = plan.bodies["web-01"]
    assert body["name"] == "web-01"
    assert body["x_opencti_system_category"] == "host"
    assert body["x_opencti_criticality"] == "high"
    assert body["x_opencti_environment"] == "production"
    assert "x_opencti_internet_facing" not in body  # unknown is never written
    assert plan.software == [
        ("software:f5|nginx|1.25.3", {"name": "nginx", "vendor": "F5", "version": "1.25.3", "cpe": NGINX["cpe"]})
    ]
    assert plan.relations == [("uses:web-01|f5|nginx|1.25.3", "system:web-01", "software:f5|nginx|1.25.3")]
    summary = plan.summary()
    assert summary["installations_create"] == ["uses:web-01|f5|nginx|1.25.3"]


def test_second_run_is_idempotent() -> None:
    ids = {"system:web-01": "s1", "software:f5|nginx|1.25.3": "o1", "uses:web-01|f5|nginx|1.25.3": "r1"}
    current = {
        "s1": {
            "id": "s1",
            "name": "web-01",
            "x_opencti_system_category": "host",
            "x_opencti_criticality": "high",
            "x_opencti_environment": "production",
        },
        "o1": {"id": "o1"},
        "r1": {"id": "r1"},
    }
    plan = cti.plan_opencti_outbound(_profile(WEB), {"web-01": [NGINX]}, current, ids)
    assert not (plan.create or plan.update or plan.delete or plan.software or plan.relations)
    assert not (plan.relations_delete or plan.released)


def test_a_changed_asset_is_patched_on_the_attributes_it_states() -> None:
    ids = {"system:web-01": "s1"}
    current = {
        "s1": {"id": "s1", "name": "web-01", "x_opencti_system_category": "host", "x_opencti_criticality": "low"}
    }
    plan = cti.plan_opencti_outbound(_profile(WEB), {}, current, ids)
    assert plan.update == [("s1", "web-01", "high")]
    keys = {e["key"] for e in plan.patches["web-01"]}
    assert keys == {"x_opencti_criticality", "x_opencti_environment"}


def test_an_asset_leaving_the_filter_is_deleted_only_when_this_sync_created_it() -> None:
    ids = {"system:db-01": "s2", "uses:db-01|f5|nginx|1.25.3": "r2", "software:f5|nginx|1.25.3": "o1"}
    current = {"s2": {"id": "s2", "name": "db-01"}, "r2": {"id": "r2"}, "o1": {"id": "o1"}}
    plan = cti.plan_opencti_outbound(_profile(WEB, DB), {"db-01": [NGINX]}, current, ids, {"tags": ["dmz"]})
    assert plan.delete == [("system:db-01", "s2")]
    assert plan.relations_delete == []  # deleted with its System
    assert plan.released == ["software:f5|nginx|1.25.3"]  # the observable is never deleted
    assert [n for n, _ in plan.create] == ["web-01"]


def test_an_uninstalled_software_drops_only_its_relationship() -> None:
    ids = {"system:web-01": "s1", "software:f5|nginx|1.25.3": "o1", "uses:web-01|f5|nginx|1.25.3": "r1"}
    current = {"s1": {"id": "s1", "name": "web-01"}, "o1": {"id": "o1"}, "r1": {"id": "r1"}}
    plan = cti.plan_opencti_outbound(_profile(WEB), {}, current, ids, taxonomy=False)
    assert plan.delete == []
    assert plan.relations_delete == [("uses:web-01|f5|nginx|1.25.3", "r1")]
    assert plan.released == ["software:f5|nginx|1.25.3"]


def test_opencti_rows_are_never_pushed_back_and_a_vanished_object_is_created_again() -> None:
    own = {**WEB, "source": "opencti", "external_id": "x"}
    plan = cti.plan_opencti_outbound(_profile(own, DB), {}, {}, {"system:db-01": "gone"})
    assert plan.skipped == ["web-01"]
    assert [n for n, _ in plan.create] == ["db-01"]
    assert plan.delete == []


def test_without_the_system_taxonomy_only_the_name_is_written() -> None:
    plan = cti.plan_opencti_outbound(_profile(WEB), {}, {}, {}, taxonomy=False)
    assert plan.bodies["web-01"] == {"name": "web-01", "description": cti.SYSTEM_DESCRIPTION}


def test_software_body_drops_an_unknown_version() -> None:
    assert cti.software_body({"product": "curl", "version": "unknown"}) == {"name": "curl"}


def test_selected_asset_names_follow_the_filters() -> None:
    # The installation cap counts only these assets: narrowing the filters lowers it.
    own = {**DB, "name": "cti-01", "source": "opencti", "external_id": "x"}
    profile = _profile(WEB, DB, own)
    assert cti.selected_asset_names(profile, {"tags": ["dmz"]}) == {"web-01": "web-01"}
    assert cti.selected_asset_names(profile, {}) == {"web-01": "web-01", "db-01": "db-01"}
