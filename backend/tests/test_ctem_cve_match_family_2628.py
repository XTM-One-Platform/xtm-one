# SPDX-License-Identifier: Apache-2.0

"""#2628: a bare product word never matches a whole vendor family.

A Windows Server CVE opened P2 cases on Windows 11 and macOS workstations and on
GitHub repositories because the device's OS family ``Windows`` matched
``Windows Server 2019`` as a word. A family names only itself, software built for
one platform never matches a device of another, a repository runs only the
dependencies its SBOM declares, and a CVE match no versioned installed row proves
goes to ``to_validate`` with the reason ``version_not_verified``.
"""

from __future__ import annotations

from datetime import date

from app.services import ctem_primary_assets as pa
from app.services import ctem_scope_gate as gate
from app.services import organization_context as oc

TODAY = date(2026, 11, 27)
WORKSTATIONS = "Employee workstations"


def _profile() -> oc.OrganizationProfile:
    return oc.parse_profile(
        {
            "primary_assets": [{"name": WORKSTATIONS, "criticality": "critical"}],
            "prioritization_policy": {"min_primary_asset_criticality": "medium"},
        }
    )


def _device(name: str, os_family: str, products: dict[str, str], versioned: set[str] | None = None) -> pa.Reacher:
    reacher = pa.Reacher(
        name=name,
        keys={name.lower()},
        products={**products, os_family.lower(): os_family} if os_family else dict(products),
        os_family=os_family,
        versioned=set(versioned or ()),
    )
    reacher.links.append((pa.Link(pa.primary_asset_key(WORKSTATIONS), "rule", ("used_by",)), "asset_group", "All"))
    return reacher


def _fleet() -> pa.PrimaryAssetIndex:
    return pa.PrimaryAssetIndex(
        loaded=True,
        reachers=[
            _device("LAPTOP-W11", "Windows", {"microsoft windows 11": "Microsoft Windows 11"}),
            _device("MAC-01", "macOS", {"microsoft excel": "Microsoft Excel"}),
            # A repository: no OS family, nothing but what its SBOM declares (here nothing).
            _device("filigran/website", "", {}),
        ],
    )


def _cve(*software: str) -> gate.SignalFacts:
    return gate.SignalFacts(
        trigger_path="new_vulnerability",
        lens="world",
        subject_kind="vulnerability",
        subject_ref="CVE-2026-99001",
        source_provider="opencti",
        evidence={"software": list(software), "cvss": 8.8},
    )


def test_a_family_word_names_only_itself() -> None:
    assert not pa.product_matches("Windows", "Windows Server 2019")
    assert not pa.product_matches("windows", "cpe:2.3:o:microsoft:windows_server_2022:-:*:*:*:*:*:*:*")
    assert not pa.product_matches("Linux", "Linux Kernel")
    assert pa.product_matches("Windows", "Windows")
    assert pa.product_matches("Windows Server", "Microsoft Windows Server 2019")
    assert not gate._product_matches("Windows", "Windows Server 2016")


def test_a_windows_server_cve_reaches_no_workstation_and_opens_no_case() -> None:
    decision = gate.decide(
        _profile(), _cve("Windows Server 2019", "Windows Server 2022"), primary_assets=_fleet(), today=TODAY
    )
    assert decision.record["reconcile"]["primary_assets"] == []
    assert decision.outcome != "case"


def test_an_office_for_windows_cve_never_matches_a_macos_device() -> None:
    assert not pa.platform_compatible("Microsoft Excel for Windows", "macOS")
    assert not pa.platform_compatible("cpe:2.3:a:microsoft:excel:2016:*:*:*:*:windows:*:*", "macOS")
    assert pa.platform_compatible("Microsoft Excel for Windows", "Windows")
    assert pa.platform_compatible("Microsoft Excel", "macOS")
    hits = _fleet().reach(["Microsoft Excel for Windows"])
    assert [h["asset"] for h in hits] == []


def test_a_linux_kernel_cve_reaches_no_repository_through_a_word() -> None:
    assert _fleet().reach(["Linux Kernel"]) == []


def test_a_repository_record_runs_no_product_by_name_or_family() -> None:
    fields = {"products": ["Windows"], "os_family": "Linux", "subcategory": "code_repository"}
    assert pa._record_products(fields, category="source_code") == {}
    assert pa._record_products({"products": ["nginx"]}, category="host") == {"nginx": "nginx"}


def test_an_unproven_match_is_to_validate_with_its_reason() -> None:
    decision = gate.decide(_profile(), _cve("Microsoft Windows 11"), primary_assets=_fleet(), today=TODAY)
    assert decision.record["reconcile"]["primary_assets"]
    assert decision.outcome == "to_validate"
    assert decision.record["reason_code"] == "version_not_verified"
    assert decision.record["reason_params"] == {"assets": 1}
    assert "version not verified" in decision.reason


def test_a_versioned_installed_row_keeps_the_case() -> None:
    index = pa.PrimaryAssetIndex(
        loaded=True,
        reachers=[
            _device(
                "LAPTOP-W11",
                "Windows",
                {"microsoft windows 11": "Microsoft Windows 11"},
                versioned={"Microsoft Windows 11"},
            )
        ],
    )
    decision = gate.decide(_profile(), _cve("Microsoft Windows 11"), primary_assets=index, today=TODAY)
    assert decision.record["reconcile"]["primary_assets"][0]["version_verified"] is True
    assert decision.outcome == "case"


def test_a_group_chain_is_verified_when_any_device_of_the_group_reports_a_version() -> None:
    """Both devices reach the primary asset through the same group: the order of the rows never decides."""
    unversioned = {"microsoft windows 11": "Microsoft Windows 11"}
    for order in (0, 1):
        devices = [
            _device("LAPTOP-A", "Windows", unversioned),
            _device("LAPTOP-B", "Windows", unversioned, versioned={"Microsoft Windows 11"}),
        ]
        if order:
            devices.reverse()
        index = pa.PrimaryAssetIndex(loaded=True, reachers=devices)
        hits = index.reach(["Microsoft Windows 11"])
        assert len(hits) == 1
        assert hits[0]["version_verified"] is True
        decision = gate.decide(_profile(), _cve("Microsoft Windows 11"), primary_assets=index, today=TODAY)
        assert decision.outcome == "case"


def test_a_family_only_reference_is_never_verified() -> None:
    reacher = _device("LAPTOP-W11", "Windows", {}, versioned={"Microsoft Windows 11"})
    assert not pa.runs_versioned(reacher, "Windows")
    assert pa.runs_versioned(reacher, "Microsoft Windows 11")
