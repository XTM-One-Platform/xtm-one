# SPDX-License-Identifier: Apache-2.0

"""The running versions of a case's product, read from the asset inventory (#1252)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import ctem_case_versions as versions


def _row(
    version: str,
    count: int = 0,
    *,
    vendor: str = "Microsoft",
    product: str = "Windows",
    sources=("intune",),
    assets=None,
):
    ids = list(assets) if assets is not None else [f"{vendor}/{product}/{version}/{i}" for i in range(count)]
    return {"vendor": vendor, "product": product, "version": version, "asset_ids": ids, "sources": list(sources)}


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
    assert answer["asset_count"] == 7


def test_an_asset_is_counted_once_in_the_totals():
    # a1 runs two versions (one below, one fixed), a2 is reported under two spellings of one version,
    # a3 runs an unknown version and the fixed one.
    answer = versions.running_versions(
        [
            _row("8.0.1", assets=("a1",), vendor="Oracle", product="Java"),
            _row("8.0.9", assets=("a1", "a3"), vendor="Oracle", product="Java"),
            _row("8.0.1", assets=("a2",), vendor="", product="Oracle Java"),
            _row("8.0.1", assets=("a2",), vendor="Oracle", product="Java"),
            _row("unknown", assets=("a3",), vendor="Oracle", product="Java"),
        ],
        product_label="Oracle Java",
        fixed_version="8.0.9",
    )
    assert answer is not None
    assert {v["version"]: v["asset_count"] for v in answer["versions"]} == {"8.0.1": 2, "8.0.9": 2, "unknown": 1}
    assert answer["asset_count"] == 3
    # The worst version of each asset decides: a1 and a2 below, a3 not comparable, none on fixed alone.
    assert (answer["assets_below_fixed"], answer["assets_on_fixed"], answer["assets_not_comparable"]) == (2, 0, 1)


def test_the_case_leaves_scope_only_when_every_asset_is_on_the_fixed_version() -> None:
    def answer(*rows: dict[str, Any]) -> dict[str, Any] | None:
        return versions.running_versions(list(rows), product_label="Oracle Java", fixed_version="8.0.9")

    java = {"vendor": "Oracle", "product": "Java"}
    assert versions.all_on_fixed(answer(_row("8.0.9", 3, **java), _row("8.0.10", 2, **java)))
    assert not versions.all_on_fixed(answer(_row("8.0.9", 3, **java), _row("8.0.1", 1, **java)))
    # An unknown version is never read as fixed.
    assert not versions.all_on_fixed(answer(_row("8.0.9", 3, **java), _row("unknown", 1, **java)))
    assert not versions.all_on_fixed(
        versions.running_versions([_row("8.0.9", 3, **java)], product_label="Oracle Java", fixed_version=None)
    )
    assert not versions.all_on_fixed(None)


@pytest.mark.asyncio
async def test_a_case_without_a_fixed_version_never_leaves_scope_on_the_inventory() -> None:
    case = SimpleNamespace(
        remediation_kind="upgrade", organization_id=uuid.uuid4(), product_label="Microsoft Windows", fixed_version=None
    )
    # No fixed version: the inventory is never read, so no session is needed.
    assert await versions.fixed_in_inventory(cast(AsyncSession, None), case) is False


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


def test_affected_assets_are_one_row_per_asset_from_the_visible_issues() -> None:
    answer = versions.affected_assets(
        [
            {"assets": ["web-01", "db-01"], "open": True, "kev": False, "cvss": 7.5},
            {"assets": ["WEB-01 "], "open": True, "kev": True, "cvss": 9.8},
            {"assets": ["db-01"], "open": False, "kev": False, "cvss": None},
            {"restricted": True},
            {"source": "signal", "external_id": "x", "open": True},
        ]
    )
    assert answer["asset_count"] == 2
    assert answer["more_assets"] == 0
    web, db = answer["assets"]
    assert (web["name"], web["issue_count"], web["open_issue_count"], web["kev"], web["max_cvss"]) == (
        "web-01",
        2,
        2,
        True,
        9.8,
    )
    assert (db["name"], db["issue_count"], db["open_issue_count"], db["kev"], db["max_cvss"]) == (
        "db-01",
        2,
        1,
        False,
        7.5,
    )


def test_affected_assets_fold_kev_and_cvss_of_the_same_cve_across_sources() -> None:
    # OpenCTI marks the CVE known exploited without naming assets; the OpenAEV finding
    # of the same CVE names the assets with no KEV flag (UX r1 B3 on #1324).
    answer = versions.affected_assets(
        [
            {"source": "opencti", "cve": "CVE-2024-21762", "assets": [], "open": True, "kev": True, "cvss": 9.6},
            {
                "source": "openaev",
                "cve": "cve-2024-21762",
                "assets": ["fw-paris-01", "fw-lyon-02"],
                "open": True,
                "kev": False,
                "cvss": None,
            },
            {
                "source": "openaev",
                "cve": "CVE-2023-0001",
                "assets": ["web-01"],
                "open": True,
                "kev": False,
                "cvss": 5.0,
            },
        ]
    )
    rows = {row["name"]: row for row in answer["assets"]}
    assert (rows["fw-paris-01"]["kev"], rows["fw-paris-01"]["max_cvss"]) == (True, 9.6)
    assert (rows["fw-lyon-02"]["kev"], rows["fw-lyon-02"]["max_cvss"]) == (True, 9.6)
    assert (rows["web-01"]["kev"], rows["web-01"]["max_cvss"]) == (False, 5.0)


def test_versions_on_assets_take_the_worst_status_per_asset() -> None:
    rows = [
        {
            "name": "web-01",
            "asset_id": "a1",
            "vendor": "Oracle",
            "product": "Java",
            "version": "8.0.9",
            "sources": ["intune"],
        },
        {
            "name": "web-01",
            "asset_id": "a1",
            "vendor": "Oracle",
            "product": "Java",
            "version": "8.0.1",
            "sources": ["sbom"],
        },
        {
            "name": "db-01",
            "asset_id": "a2",
            "vendor": "Oracle",
            "product": "Java",
            "version": "",
            "sources": ["openaev"],
        },
        {"name": "db-01", "asset_id": "a2", "vendor": "Oracle", "product": "MySQL", "version": "1", "sources": []},
    ]
    out = versions.versions_on_assets(rows, product_label="Oracle Java", fixed_version="8.0.9")
    assert out["web-01"]["versions"] == ["8.0.1", "8.0.9"]
    assert out["web-01"]["sources"] == ["intune", "sbom"]
    assert out["web-01"]["status"] == "below_fixed"
    assert out["db-01"] == {
        "asset_id": "a2",
        "versions": ["unknown"],
        "sources": ["openaev"],
        "status": "not_comparable",
    }
    no_fix = versions.versions_on_assets(rows, product_label="Oracle Java", fixed_version=None)
    assert "status" not in no_fix["web-01"]


@pytest.mark.asyncio
async def test_a_case_that_is_not_an_upgrade_lists_its_assets_with_no_version_source() -> None:
    case = SimpleNamespace(
        remediation_kind="course_of_action", organization_id=uuid.uuid4(), product_label="x", fixed_version=None
    )
    answer = await versions.assets_for_case(cast(AsyncSession, None), case, [{"assets": ["web-01"], "open": True}])
    assert answer["version_source"] == "none"
    assert [a["name"] for a in answer["assets"]] == ["web-01"]
    assert "versions" not in answer["assets"][0]


@pytest.mark.asyncio
async def test_an_unreadable_inventory_keeps_the_asset_list() -> None:
    class _Broken:
        def begin_nested(self) -> None:
            raise RuntimeError("no store")

    case = SimpleNamespace(
        remediation_kind="upgrade", organization_id=uuid.uuid4(), product_label="Oracle Java", fixed_version="8.0.9"
    )
    answer = await versions.assets_for_case(cast(AsyncSession, _Broken()), case, [{"assets": ["web-01"], "open": True}])
    assert answer["version_source"] == "none"
    assert answer["asset_count"] == 1
