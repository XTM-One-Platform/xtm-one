# SPDX-License-Identifier: Apache-2.0

"""#2680: a bare OS family CVE reference reaches only devices of that known OS family.

Follow-up of #2628. A live Windows CVE (CVE-2026-20934) whose software list was the bare
family ``windows`` plus ``Windows Server 2016 10.0.14393.0`` still reached code
repositories (a ``windows`` package in their SBOM), macOS workstations and devices of
unknown OS family. A family reference names no product: it never reaches a repository,
never a device of unknown or another OS family, and the device it reaches is a
family-only hop that counts in no total.
"""

from __future__ import annotations

from datetime import date

from app.services import ctem_primary_assets as pa
from app.services import ctem_scope_gate as gate
from app.services import organization_context as oc

TODAY = date(2026, 11, 27)
WORKSTATIONS = "Employee workstations"
CODE = "Source code"


def _profile() -> oc.OrganizationProfile:
    return oc.parse_profile(
        {
            "primary_assets": [
                {"name": WORKSTATIONS, "criticality": "critical"},
                {"name": CODE, "criticality": "critical"},
            ],
            "prioritization_policy": {"min_primary_asset_criticality": "medium"},
        }
    )


def _reacher(
    name: str,
    *,
    os_family: str = "",
    products: dict[str, str] | None = None,
    repository: bool = False,
    primary_asset: str = WORKSTATIONS,
) -> pa.Reacher:
    labels = dict(products or {})
    if os_family:
        labels.setdefault(os_family.lower(), os_family)
    reacher = pa.Reacher(name=name, keys={name.lower()}, products=labels, os_family=os_family, repository=repository)
    reacher.links.append((pa.Link(pa.primary_asset_key(primary_asset), "rule", ("used_by",)), "asset", name))
    return reacher


def _fleet() -> pa.PrimaryAssetIndex:
    return pa.PrimaryAssetIndex(
        loaded=True,
        reachers=[
            # A repository whose SBOM lists the Rust ``windows`` crate.
            _reacher("filigran/agent", products={"windows": "windows"}, repository=True, primary_asset=CODE),
            _reacher("MAC-01", os_family="macOS", products={"windows": "windows"}),
            _reacher("MAC-02", os_family="Darwin", products={"windows": "Windows"}),
            _reacher("HOST-UNKNOWN", products={"windows": "windows"}),
            _reacher("LAPTOP-W11", os_family="Windows", products={"microsoft windows 11": "Microsoft Windows 11"}),
        ],
    )


CVE_2026_20934 = ["windows", "Windows", "Windows Server 2016 10.0.14393.0"]


def _cve(*software: str) -> gate.SignalFacts:
    return gate.SignalFacts(
        trigger_path="new_vulnerability",
        lens="world",
        subject_kind="vulnerability",
        subject_ref="CVE-2026-20934",
        source_provider="opencti",
        evidence={"software": list(software), "cvss": 7.8},
    )


def test_the_issue_reproduction_reaches_no_repository() -> None:
    r = pa.Reacher(name="repo", products={"windows": "windows"}, repository=True)
    r.links = [(pa.Link("pa", "inferred"), "asset", "repo")]
    assert pa.PrimaryAssetIndex(loaded=True, reachers=[r]).reach(["windows"]) == []


def test_a_family_reference_reaches_only_a_device_of_that_known_os_family() -> None:
    hits = _fleet().reach(CVE_2026_20934)
    assert [h["asset"] for h in hits] == ["LAPTOP-W11"]
    hop = hits[0]
    assert hop["family_only"] is True and hop["version_verified"] is False
    assert hop["software"].lower() == "windows"


def test_a_family_only_hop_counts_in_no_total() -> None:
    totals = _fleet().totals(CVE_2026_20934)
    assert totals == {pa.primary_asset_key(WORKSTATIONS): {"assets": 0, "direct": 0, "via_group": 0, "family_only": 1}}


def test_windows_server_never_reaches_a_windows_11_workstation() -> None:
    hits = _fleet().reach(["Windows Server 2016 10.0.14393.0"])
    assert hits == []


def test_a_named_product_wins_over_the_family_on_the_same_device() -> None:
    hits = _fleet().reach(["windows", "Microsoft Windows 11"])
    assert [(h["asset"], h["software"], h["family_only"]) for h in hits] == [
        ("LAPTOP-W11", "Microsoft Windows 11", False)
    ]
    assert _fleet().totals(["windows", "Microsoft Windows 11"])[pa.primary_asset_key(WORKSTATIONS)]["assets"] == 1


def test_the_os_family_strings_an_import_writes_are_read() -> None:
    assert pa.device_platform("Darwin") == "macOS"
    assert pa.device_platform("Mac OS X") == "macOS"
    assert pa.device_platform("Windows NT") == "Windows"
    assert pa.device_platform("") is None
    assert not pa.platform_compatible("Microsoft Excel for Windows", "Darwin")
    assert not pa.family_reach_allowed("windows", "")
    assert not pa.family_reach_allowed("windows", "Linux")
    assert not pa.family_reach_allowed("windows", "Windows", repository=True)
    assert pa.family_reach_allowed("windows", "Windows NT")
    # Unknown OS family: only a named product of the family on the device itself counts.
    assert pa.family_reach_allowed("windows", "", labels=["Microsoft Windows 11"])
    assert not pa.family_reach_allowed("windows", "", labels=["windows", "Windows"])


def test_the_gate_keeps_the_case_closed_and_every_hop_says_version_verified() -> None:
    decision = gate.decide(_profile(), _cve(*CVE_2026_20934), primary_assets=_fleet(), today=TODAY)
    hops = decision.record["reconcile"]["primary_assets"]
    assert [h["asset"] for h in hops] == ["LAPTOP-W11"]
    assert all("version_verified" in h and "family_only" in h for h in hops)
    assert decision.outcome != "case"
    scope = decision.record.get("scope") or {}
    sources = [s for s in scope.get("sources") or [] if s["from"] == "primary_asset"]
    assert all(s.get("assets_total", 0) == 0 for s in sources)
    assert not any(s["name"] == CODE and s.get("chain") for s in sources)
