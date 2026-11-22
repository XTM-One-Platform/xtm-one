"""The canonical asset store, its pure part (#1178, S2): profile rows -> canonical assets.

The SQL (rebuild, pagination, organization isolation, assets of a software)
runs against PostgreSQL in ``test_organization_asset_store_postgres.py``.
"""

from __future__ import annotations

from typing import Any

from app.services.organization_asset_store import canonical_rows


def _row(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"name": "laptop-01", "category": "host", "source": "manual", "external_id": ""}
    base.update(kw)
    return base


def test_the_same_device_from_two_sources_is_one_canonical_asset_with_two_records() -> None:
    rows = [
        _row(source="intune", external_id="d-1", serial="SN-778", os_version="14.1", criticality="unknown"),
        _row(source="openaev", external_id="a-9", serial="sn-778", name="LAPTOP-01.corp", criticality="high"),
    ]
    [asset] = canonical_rows(rows)
    assert {r["source"] for r in asset["sources"]} == {"intune", "openaev"}
    assert asset["explanation"] == "merged on serial"
    assert asset["fields"]["criticality"]["value"] == "high"
    assert {c["source"] for c in asset["fields"]["criticality"]["candidates"]} == {"intune", "openaev"}


def test_two_different_devices_stay_two_assets() -> None:
    rows = [
        _row(source="intune", external_id="d-1", serial="SN-1", name="a"),
        _row(source="intune", external_id="d-2", serial="SN-2", name="b"),
    ]
    assert len(canonical_rows(rows)) == 2


def test_software_merges_across_sources_and_an_unknown_version_stays_unknown() -> None:
    rows = [
        _row(
            source="intune",
            external_id="d-1",
            serial="SN-1",
            software=[{"vendor": "F5", "product": "nginx", "version": "1.27.1"}, {"product": "curl"}],
        ),
        _row(
            source="defender",
            external_id="m-1",
            serial="SN-1",
            software=[{"vendor": "f5", "product": "NGINX", "version": "1.27.1"}],
        ),
    ]
    [asset] = canonical_rows(rows)
    by_product = {s["product"].casefold(): s for s in asset["software"]}
    assert set(by_product) == {"nginx", "curl"}
    assert sorted(by_product["nginx"]["sources"]) == ["defender", "intune"]
    assert by_product["curl"]["version"] == "unknown"


def test_the_answer_does_not_depend_on_the_order_of_the_rows() -> None:
    rows = [
        _row(source="intune", external_id="d-1", serial="SN-1"),
        _row(source="openaev", external_id="a-1", serial="SN-1"),
        _row(source="csv", external_id="p-1", name="printer", category="iot_ot_device"),
    ]
    keys = sorted(a["canonical_key"] for a in canonical_rows(rows))
    assert keys == sorted(a["canonical_key"] for a in canonical_rows(list(reversed(rows))))


def test_an_ip_alone_never_merges_and_is_answered_as_a_possible_match() -> None:
    rows = [
        _row(source="intune", external_id="d-1", name="alpha", ip="10.0.0.5"),
        _row(source="openaev", external_id="a-1", name="beta", ip="10.0.0.5"),
    ]
    assets = canonical_rows(rows)
    assert len(assets) == 2
    assert all(a["possible_matches"] for a in assets)
