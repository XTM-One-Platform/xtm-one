# SPDX-License-Identifier: Apache-2.0

"""#2711: a word of a compound package name or of a long title reaches nothing.

A CVE on the npm package ``mail-mcp-bridge`` reached 132 Macs running Apple Mail and two
repositories whose SBOM holds ``mcp``, every hop version verified and named as the CVE
package. A joined package name names a product only by its full name, a long title by its
full name or its vendor and product words, never by one of its words; the hop names the
label the asset runs, with the CVE reference beside it.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services import ctem_primary_assets as pa
from app.services import ctem_scope_gate as gate
from app.services import organization_context as oc

TODAY = date(2026, 11, 27)
WORKSTATIONS = "Employee workstations"
CET = "CET Automated Grading System with AI Predictive Analytics 1.0"


@pytest.fixture(autouse=True)
def _no_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reproduction of the issue: no platform read of a reference, the match rule alone decides."""
    monkeypatch.setattr(pa, "_platform_of_ref", lambda _ref: None)


def _reacher(name: str, label: str, *, os_family: str = "", repository: bool = False) -> pa.Reacher:
    reacher = pa.Reacher(
        name=name,
        keys={name.lower()},
        products={label.lower(): label},
        os_family=os_family,
        versioned={label},
        repository=repository,
    )
    reacher.links.append((pa.Link(pa.primary_asset_key(WORKSTATIONS), "rule", ("used_by",)), "asset", name))
    return reacher


def _index(*reachers: pa.Reacher) -> pa.PrimaryAssetIndex:
    return pa.PrimaryAssetIndex(loaded=True, reachers=list(reachers))


def test_the_issue_reproduction_reaches_nothing() -> None:
    r = pa.Reacher(name="mac")
    r.products = {"a": "Mail"}
    r.versioned = {"Mail"}
    r.os_family = "macOS"
    r.links = [(pa.Link("pa", "rule"), "asset", "mac")]
    assert pa.PrimaryAssetIndex(loaded=True, reachers=[r]).reach(["mail-mcp-bridge 1.3.0"]) == []
    assert not pa.product_matches("Mail", "mail-mcp-bridge 1.3.0")


@pytest.mark.parametrize(
    ("label", "ref", "repository"),
    [
        ("Mail", "mail-mcp-bridge 1.3.0", False),
        ("Xcode", "xcode-mcp-server 1.0.0", False),
        ("mcp", "mcp-dnstwist 1.0.3", True),
        ("analytics", CET, False),
        ("analytics", CET, True),
        ("Xcode", "xcode_mcp_server", False),
    ],
)
def test_a_word_of_a_compound_name_or_long_title_reaches_no_asset(label: str, ref: str, repository: bool) -> None:
    assert not pa.product_matches(label, ref)
    assert not gate._product_matches(label, ref)
    assert (
        _index(_reacher("ASSET", label, os_family="" if repository else "macOS", repository=repository)).reach([ref])
        == []
    )


def test_the_full_package_name_still_reaches_and_is_verified() -> None:
    hits = _index(_reacher("repo", "mail-mcp-bridge", repository=True)).reach(["mail-mcp-bridge 1.3.0"])
    assert [h["asset"] for h in hits] == ["repo"]
    assert hits[0]["version_verified"] is True
    assert hits[0]["software"] == "mail-mcp-bridge" and hits[0]["reference"] == "mail-mcp-bridge 1.3.0"


def test_a_long_title_matches_by_full_name_or_vendor_and_product() -> None:
    assert pa.product_matches("AnyConnect Secure Mobility Client", "Cisco AnyConnect Secure Mobility Client")
    assert pa.product_matches("Microsoft Excel", "Microsoft Excel for Windows")
    assert pa.product_matches(CET, CET)
    assert not pa.product_matches("Predictive Analytics", CET)


@pytest.mark.parametrize("label", ["Microsoft Windows", "Mac OS X", "GNU Linux", "Chrome OS"])
def test_a_family_label_never_opens_a_long_title(label: str) -> None:
    """#2628 kept on long titles: a bare family names only itself (review of #2717)."""
    ref = f"{label} Server 2019 Datacenter Edition"
    assert not pa.product_matches(label, ref)
    assert not gate._product_matches(label, ref)
    assert _index(_reacher("HOST", label, os_family=label)).reach([ref]) == []


@pytest.mark.parametrize(
    ("label", "ref"),
    [
        ("Windows Server 2022", "Windows Server 2019 Standard Edition"),
        ("Microsoft Office 2013", "Microsoft Office 2019 for Mac"),
    ],
)
def test_a_label_of_another_release_never_opens_a_long_title(label: str, ref: str) -> None:
    """The label keeps its version when it is compared with a long title (review of #2717)."""
    assert not pa.product_matches(label, ref)
    assert not gate._product_matches(label, ref)
    assert _index(_reacher("HOST", label, os_family="Windows")).reach([ref]) == []


def test_a_label_of_the_same_release_still_opens_a_long_title() -> None:
    assert pa.product_matches("Microsoft Office 2019", "Microsoft Office 2019 for Mac")
    assert pa.product_matches("Windows Server 2019", "Windows Server 2019 Standard Edition")


def test_the_hop_names_the_label_the_asset_runs_and_the_reference_apart() -> None:
    hits = _index(_reacher("LAPTOP", "Google Chrome", os_family="Windows")).reach(["Google Chrome 120"])
    assert hits[0]["software"] == "Google Chrome" and hits[0]["reference"] == "Google Chrome 120"
    assert hits[0]["version_verified"] is True


def test_short_references_keep_the_whole_word_rule() -> None:
    assert pa.product_matches("Nginx", "nginx_plus")
    hits = _index(_reacher("WEB-01", "Nginx", os_family="Linux")).reach(["nginx_plus"])
    assert [h["asset"] for h in hits] == ["WEB-01"] and hits[0]["software"] == "Nginx"
    assert pa.product_matches("Google Chrome", "Google Chrome 120")
    assert not pa.product_matches("Windows", "Windows Server 2019")
    assert not pa.product_matches("Client", "TrueConf Client")


def test_a_cve_on_mail_mcp_bridge_opens_no_case_on_macs_running_mail() -> None:
    profile = oc.parse_profile(
        {
            "primary_assets": [{"name": WORKSTATIONS, "criticality": "critical"}],
            "software": [{"product": "Mail"}],
            "prioritization_policy": {"min_primary_asset_criticality": "medium"},
        }
    )
    facts = gate.SignalFacts(
        trigger_path="new_vulnerability",
        lens="world",
        subject_kind="vulnerability",
        subject_ref="CVE-2026-99711",
        source_provider="opencti",
        evidence={"cvss": 9.8},
        cve_software=("mail-mcp-bridge 1.3.0",),
    )
    index = _index(_reacher("MAC-01", "Mail", os_family="macOS"), _reacher("repo", "mcp", repository=True))
    decision = gate.decide(profile, facts, primary_assets=index, today=TODAY)
    assert decision.record["reconcile"]["primary_assets"] == []
    assert decision.outcome != "case"
