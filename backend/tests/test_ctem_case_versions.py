# SPDX-License-Identifier: Apache-2.0

"""The running versions of a case's product, read from the asset inventory (#1252)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import ctem_case_versions as versions


def _row(version: str, count: int, *, vendor: str = "Microsoft", product: str = "Windows", sources=("intune",)):
    return {"vendor": vendor, "product": product, "version": version, "asset_count": count, "sources": list(sources)}


def test_versions_are_listed_with_their_asset_counts_and_sources():
    answer = versions.running_versions(
        [_row("10.0.22631.4317", 640, sources=("intune", "defender")), _row("10.0.22621.3880", 274)],
        product_label="Microsoft Windows",
        fixed_version=None,
    )
    assert answer is not None
    assert [(v["version"], v["asset_count"]) for v in answer["versions"]] == [
        ("10.0.22631.4317", 640),
        ("10.0.22621.3880", 274),
    ]
    assert answer["versions"][0]["sources"] == ["defender", "intune"]
    assert answer["sources"] == ["defender", "intune"]
    # Unknown fixed version: the sources checked are named, nothing asks for a typed version.
    assert answer["fixed_version_checked"] == list(versions.FIXED_VERSION_SOURCES)
    assert "assets_below_fixed" not in answer


def test_a_known_fixed_version_counts_the_assets_below_and_on_it():
    answer = versions.running_versions(
        [_row("10.0.22631.4317", 640), _row("10.0.22621.3880", 274), _row("unknown", 3), _row("10.0.22631.4460", 10)],
        product_label="Microsoft Windows",
        fixed_version="10.0.22631.4460",
    )
    assert answer is not None
    assert (answer["assets_below_fixed"], answer["assets_on_fixed"], answer["assets_not_comparable"]) == (914, 10, 3)
    status = {v["version"]: v["status"] for v in answer["versions"]}
    assert status == {
        "10.0.22631.4317": "below_fixed",
        "10.0.22621.3880": "below_fixed",
        "unknown": "not_comparable",
        "10.0.22631.4460": "fixed",
    }
    assert "fixed_version_checked" not in answer


def test_the_product_matches_on_the_label_or_vendor_and_product():
    assert versions.matches_case("Microsoft Windows", "Microsoft", "Windows")
    assert versions.matches_case("Microsoft Windows", "", "microsoft  windows")
    assert not versions.matches_case("Microsoft Windows", "Microsoft", "Office")
    assert not versions.matches_case("nginx", "F5", "nginx plus")
    assert versions.product_keys("Microsoft Windows") == ("microsoft windows", "windows")
    assert versions.product_keys("nginx") == ("nginx",)
    assert versions.product_keys(None) == ()


def test_no_asset_running_the_product_answers_none():
    assert (
        versions.running_versions(
            [_row("1.0", 2, product="Office")], product_label="Microsoft Windows", fixed_version=None
        )
        is None
    )
    assert versions.running_versions([], product_label="Microsoft Windows", fixed_version="1.0") is None


def test_the_same_version_from_two_rows_is_one_entry():
    answer = versions.running_versions(
        [_row("1.2", 3, vendor="", product="Microsoft Windows", sources=("sbom",)), _row("1.2", 4)],
        product_label="Microsoft Windows",
        fixed_version=None,
    )
    assert answer is not None
    assert answer["versions"] == [{"version": "1.2", "asset_count": 7, "sources": ["intune", "sbom"]}]


@pytest.mark.asyncio
async def test_a_case_that_is_not_an_upgrade_reads_nothing():
    case = SimpleNamespace(
        remediation_kind="course_of_action", organization_id=uuid.uuid4(), product_label="x", fixed_version=None
    )
    assert await versions.for_case(object(), case) is None


@pytest.mark.asyncio
async def test_an_unreadable_inventory_never_breaks_the_case_read():
    class _Broken:
        def begin_nested(self):
            raise RuntimeError("no store")

    case = SimpleNamespace(
        remediation_kind="upgrade", organization_id=uuid.uuid4(), product_label="Microsoft Windows", fixed_version=None
    )
    assert await versions.for_case(_Broken(), case) is None
