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
    (row,) = answer["versions"]
    assert (row["version"], row["platform"], row["asset_count"], row["sources"]) == ("1.2", None, 7, ["intune", "sbom"])
    assert len(row["asset_ids"]) == 7 and row["more_asset_ids"] == 0
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


def test_an_operating_system_case_names_the_os_family_of_the_assets() -> None:
    assert versions.os_family_of("Microsoft Windows") == "Windows"
    assert versions.os_family_of("  apple  macOS ") == "macOS"
    # Whole labels only: a product that only contains an OS word is not the asset's OS.
    assert versions.os_family_of("Cisco IOS") is None
    assert versions.os_family_of("Google Chrome") is None
    assert versions.os_family_of("Microsoft Windows Server Update Services") is None
    assert versions.os_family_of(None) is None


def test_windows_assets_count_as_running_versions_from_their_os_version() -> None:
    """#1430: Intune assets with os_family Windows answer the Windows case's running versions."""
    assets = [
        {"asset_id": "a1", "name": "lapt-0002", "os_version": "10.0.26200.9911", "source": "intune"},
        {"asset_id": "a2", "name": "lapt-0100", "os_version": "10.0.26200.9974", "source": "intune"},
        {"asset_id": "a3", "name": "lapt-0058", "os_version": "", "source": "openaev"},
        {"asset_id": "a4", "name": "srv-01", "os_version": "10.0.20348.1", "source": "defender"},
    ]
    # a4 already reports Windows as a software row: counted once, from that row.
    software = [_row("10.0.20348.2700", assets=["a4"], sources=("sbom",))]
    rows = software + versions.os_rows(assets, product_label="Microsoft Windows", skip={"a4"})
    answer = versions.running_versions(rows, product_label="Microsoft Windows", fixed_version="10.0.26200.9974")
    assert answer is not None
    assert answer["asset_count"] == 4
    found = {v["version"]: (v["asset_count"], v["sources"]) for v in answer["versions"]}
    assert found == {
        "10.0.26200.9911": (1, ["intune"]),
        "10.0.26200.9974": (1, ["intune"]),
        "unknown": (1, ["openaev"]),
        "10.0.20348.2700": (1, ["sbom"]),
    }
    assert (answer["assets_below_fixed"], answer["assets_on_fixed"], answer["assets_not_comparable"]) == (2, 1, 1)


def test_os_rows_join_the_affected_assets_by_name() -> None:
    rows = versions.os_rows(
        [{"asset_id": "a1", "name": "lapt-0002", "os_version": "10.0.26200.9911", "source": "intune"}],
        product_label="Microsoft Windows",
    )
    by_asset = versions.versions_on_assets(rows, product_label="Microsoft Windows", fixed_version="10.0.26200.9974")
    assert by_asset["lapt-0002"] == {
        "asset_id": "a1",
        "platform": "Windows",
        "versions": ["10.0.26200.9911"],
        "sources": ["intune"],
        "status": "below_fixed",
    }


def _browser_inventory() -> tuple[list[dict[str, Any]], dict[str, str]]:
    """#1572 acceptance: 3 Windows on X.36, 2 macOS on X.36, 1 macOS on X.37, 1 Windows on X.35 and X.36."""
    rows = [
        _row("36.0", assets=("w1", "w2", "w3", "w4", "m1", "m2"), vendor="Acme", product="Browser"),
        _row("37.0", assets=("m3",), vendor="Acme", product="Browser"),
        _row("35.0", assets=("w4",), vendor="Acme", product="Browser"),
    ]
    platforms = {**{a: "Windows" for a in ("w1", "w2", "w3", "w4")}, **{a: "macOS" for a in ("m1", "m2", "m3")}}
    return rows, platforms


def test_running_versions_are_grouped_by_version_and_platform_with_a_fix_per_platform() -> None:
    rows, platforms = _browser_inventory()
    answer = versions.running_versions(
        rows,
        product_label="Acme Browser",
        fixed_version="37.0",
        fixed_versions_by_platform={"Windows": "36.0", "macOS": "37.0"},
        asset_platforms=platforms,
    )
    assert answer is not None
    found = {(v["version"], v["platform"]): (v["asset_count"], v["asset_ids"], v["status"]) for v in answer["versions"]}
    assert found == {
        ("36.0", "Windows"): (4, ["w1", "w2", "w3", "w4"], "fixed"),
        ("35.0", "Windows"): (1, ["w4"], "below_fixed"),
        ("36.0", "macOS"): (2, ["m1", "m2"], "below_fixed"),
        ("37.0", "macOS"): (1, ["m3"], "fixed"),
    }
    assert (answer["asset_count"], answer["assets_multi_version"], answer["version_row_asset_sum"]) == (7, 1, 8)
    assert answer["version_rows_note"] == versions.VERSION_ROWS_NOTE
    assert (answer["assets_below_fixed"], answer["assets_on_fixed"], answer["assets_not_comparable"]) == (3, 4, 0)
    assert answer["fixed_versions"] == {"Windows": "36.0", "macOS": "37.0"}
    assert answer["platforms"] == ["Windows", "macOS"]
    assert answer["platform_unverified"] is False
    assert not versions.all_on_fixed(answer)


def test_a_generic_fix_over_several_platforms_never_resolves_the_case() -> None:
    rows, platforms = _browser_inventory()
    answer = versions.running_versions(
        rows, product_label="Acme Browser", fixed_version="36.0", asset_platforms=platforms
    )
    assert answer is not None
    assert answer["platform_unverified"] is True
    statuses = {(v["version"], v["platform"]): (v["status"], v.get("status_reason")) for v in answer["versions"]}
    assert statuses[("36.0", "macOS")] == ("not_comparable", versions.FIX_NOT_KNOWN_FOR_PLATFORM)
    assert statuses[("35.0", "Windows")] == ("below_fixed", None)
    # Below the fix stays below; nothing reads fixed while the fix is not verified per platform.
    assert answer["assets_below_fixed"] == 1
    assert answer["assets_on_fixed"] == 0
    assert answer["assets_fix_not_known_for_platform"] == 6
    assert not versions.all_on_fixed(answer)
    # Even with every asset at or above the generic fix.
    only_fixed = versions.running_versions(
        rows[:2], product_label="Acme Browser", fixed_version="36.0", asset_platforms=platforms
    )
    assert only_fixed is not None and not versions.all_on_fixed(only_fixed)


def test_a_platform_the_per_platform_fix_does_not_name_is_not_comparable() -> None:
    rows, platforms = _browser_inventory()
    answer = versions.running_versions(
        rows,
        product_label="Acme Browser",
        fixed_version="36.0",
        fixed_versions_by_platform={"Windows": "36.0"},
        asset_platforms=platforms,
    )
    assert answer is not None
    assert answer["assets_not_comparable"] == 3
    assert answer["assets_fix_not_known_for_platform"] == 3
    assert not versions.all_on_fixed(answer)


def test_a_single_platform_with_a_generic_fix_is_unchanged() -> None:
    answer = versions.running_versions(
        [_row("8.0.9", assets=("a1", "a2"), vendor="Oracle", product="Java")],
        product_label="Oracle Java",
        fixed_version="8.0.9",
        asset_platforms={"a1": "Linux", "a2": "Linux"},
    )
    assert answer is not None
    assert answer["platform_unverified"] is False
    assert versions.all_on_fixed(answer)


def test_versions_on_assets_use_the_fix_of_the_asset_platform() -> None:
    browser = {"vendor": "Acme", "product": "Browser"}
    rows = [
        {"name": "mac-1", "asset_id": "m1", "version": "36.0", "sources": ["intune"], "platform": "macOS", **browser},
        {"name": "win-1", "asset_id": "w1", "version": "36.0", "sources": ["intune"], "platform": "Windows", **browser},
    ]
    by_platform = versions.versions_on_assets(
        rows,
        product_label="Acme Browser",
        fixed_version="37.0",
        fixed_versions_by_platform={"Windows": "36.0", "macOS": "37.0"},
    )
    assert (by_platform["mac-1"]["status"], by_platform["win-1"]["status"]) == ("below_fixed", "fixed")
    generic = versions.versions_on_assets(rows, product_label="Acme Browser", fixed_version="36.0")
    assert generic["mac-1"]["status_reason"] == versions.FIX_NOT_KNOWN_FOR_PLATFORM
