# SPDX-License-Identifier: Apache-2.0

"""#2777: a typed profile software row proves presence, never that an affected version runs.

After a purge and resync, KEV CVEs whose OpenCTI software named no version (``react``, ``Git``)
opened P1 cases: a profile software row matched by name made ``name_only_reach`` answer False
before the hops' ``version_verified`` flags were read, so KEV counted as proof. A
``new_vulnerability`` reach whose hops are none version-verified is now ``to_validate`` and never
proven by KEV; a fix per release branch is compared with the installed version of that branch,
and a product word in a label's parenthesised aside or package path is no match.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from app.services import ctem_primary_assets as pa
from app.services import ctem_scope_gate as gate
from app.services import organization_context as oc

TODAY = date(2026, 11, 28)
WORKSTATIONS = "Employee workstations"


@pytest.fixture(autouse=True)
def _no_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pa, "_platform_of_ref", lambda _ref: None)


def _profile(product: str) -> oc.OrganizationProfile:
    return oc.parse_profile(
        {
            "software": [{"product": product}],
            "primary_assets": [{"name": WORKSTATIONS, "criticality": "critical"}],
            "prioritization_policy": {"min_primary_asset_criticality": "medium", "kev_always_in": True},
        }
    )


def _device(name: str, label: str, *versions: str) -> pa.Reacher:
    reacher = pa.Reacher(
        name=name,
        keys={name.lower()},
        products={label.lower(): label},
        os_family="",
        versioned={label} if versions else set(),
        versions={label: set(versions)} if versions else {},
    )
    reacher.links.append((pa.Link(pa.primary_asset_key(WORKSTATIONS), "rule", ("used_by",)), "asset", name))
    return reacher


def _index(*reachers: pa.Reacher) -> pa.PrimaryAssetIndex:
    return pa.PrimaryAssetIndex(loaded=True, reachers=list(reachers))


def _kev(software: str, **evidence: object) -> gate.SignalFacts:
    return gate.SignalFacts(
        trigger_path="new_vulnerability",
        lens="world",
        subject_kind="vulnerability",
        subject_ref="CVE-2025-55182",
        source_provider="opencti",
        evidence={"software": [software], "cve": "CVE-2025-55182", "kev": True, "cvss": 10.0, **evidence},
        cve_kev=True,
    )


def test_a_typed_product_with_no_verified_hop_is_to_validate_and_not_proven() -> None:
    decision = gate.decide(
        _profile("React"), _kev("react"), primary_assets=_index(_device("REPO-01", "react", "19.1.0")), today=TODAY
    )
    reconcile = decision.record["reconcile"]
    assert reconcile["products"] == ["React"]
    assert all(h["version_verified"] is False for h in reconcile["primary_assets"])
    assert decision.outcome == "to_validate"
    assert decision.record["reason_code"] == "version_not_verified"
    assert reconcile["proven"] is False
    assert reconcile["confidence"] != "high"
    assert decision.severity is None


def test_every_asset_above_the_fix_opens_no_case() -> None:
    decision = gate.decide(
        _profile("React"),
        _kev("react", fixed_version="19.2.1"),
        primary_assets=_index(_device("REPO-01", "react", "19.2.8"), _device("REPO-02", "react", "19.3.0")),
        today=TODAY,
    )
    assert decision.outcome != "case"
    assert decision.record["reconcile"]["proven"] is False


def test_a_verified_hop_on_a_typed_product_still_opens_a_case() -> None:
    decision = gate.decide(
        _profile("React"),
        _kev("react", fixed_version="19.2.1"),
        primary_assets=_index(_device("REPO-01", "react", "19.1.0")),
        today=TODAY,
    )
    assert decision.record["reconcile"]["primary_assets"][0]["version_verified"] is True
    assert decision.record["reconcile"]["proven"] is True
    assert decision.outcome == "case"


GIT_FIXES = ["2.43.7", "2.44.4", "2.45.4", "2.46.4", "2.47.3", "2.48.2", "2.49.1", "2.50.1"]


@pytest.mark.parametrize(
    ("installed", "affected"),
    [
        ("2.45.1", True),
        ("2.45.4", False),
        ("2.45.9", False),
        ("2.50.0", True),
        ("2.50.1", False),
        ("2.51.0", False),
        ("2.39.5", True),
    ],
)
def test_the_fix_of_the_installed_branch_decides(installed: str, affected: bool) -> None:
    assert pa.below_branch_fix(installed, GIT_FIXES) is affected
    assert pa.version_in_reference("Git", {installed}, "Git", fixed_version=GIT_FIXES) is affected


def test_git_reaches_only_assets_below_their_branch_fix() -> None:
    index = _index(
        _device("MAC-01", "Git", "2.45.1"),
        _device("MAC-02", "Git", "2.50.1"),
        _device("MAC-03", "Krita (git bd6e8d0)", "5.2.0"),
        _device("MAC-04", "@npmcli/git", "5.0.0"),
    )
    hits = index.reach(["Git"], fixed_version=GIT_FIXES)
    reached = {h["asset"] for h in hits}
    assert "MAC-03" not in reached
    assert "MAC-04" not in reached
    assert {h["asset"] for h in hits if h["version_verified"]} == {"MAC-01"}


def test_fixes_per_branch_are_read_from_the_evidence() -> None:
    evidence = {
        "fixed_version": "2.43.7",
        "fixed_versions": {"2.45": "2.45.4"},
        "case_fix": {"fixed_versions": ["2.50.1"]},
    }
    assert gate.evidence_branch_fixes(evidence) == ["2.43.7", "2.45.4", "2.50.1"]
    assert gate.evidence_branch_fixes({}) == []


def test_a_one_word_profile_product_is_not_one_word_of_a_longer_reference() -> None:
    """Point 5: ``unified`` (npm) never joins ``Unified Communications Manager`` by one of its words."""
    facts = _kev("Cisco Unified Communications Manager")
    facts = replace(
        facts,
        evidence={
            **facts.evidence,
            "software": ["Cisco Unified Communications Manager", "Unified Communications Manager"],
        },
    )
    decision = gate.decide(
        _profile("unified"), facts, primary_assets=_index(_device("REPO-01", "unified", "11.0.5")), today=TODAY
    )
    reconcile = decision.record["reconcile"]
    assert "unified" not in reconcile["products"]
    assert reconcile["proven"] is False
    assert decision.outcome != "case"
    assert decision.severity is None


@pytest.mark.parametrize(
    "product",
    ["Unified Communications Manager", "Cisco Unified Communications Manager", "Firefox", "Chrome"],
)
def test_a_whole_name_or_a_vendor_product_pair_still_matches(product: str) -> None:
    refs = {
        "Unified Communications Manager": "Cisco Unified Communications Manager",
        "Cisco Unified Communications Manager": "Cisco Unified Communications Manager",
        "Firefox": "Mozilla Firefox",
        "Chrome": "Google Chrome 120",
    }
    assert gate._product_matches(product, refs[product])


def test_a_cisco_cpe_still_matches_its_profile_row() -> None:
    cpe = "cpe:2.3:a:cisco:unified_communications_manager:14.0:*:*:*:*:*:*:*"
    assert gate._product_matches("Unified Communications Manager", cpe)
    assert not gate._product_matches("unified", "Unified Communications Manager")


def test_a_product_word_in_an_aside_or_a_package_path_is_no_match() -> None:
    assert not pa.label_names_word("git", "Krita (git bd6e8d0)")
    assert not pa.label_names_word("git", "@npmcli/git")
    assert pa.label_names_word("git", "Git for Windows")
    assert pa.label_names_word("krita", "Krita (git bd6e8d0)")


def test_a_one_word_vendor_still_confirms_a_shipped_product_by_name() -> None:
    # Review of #2790: the one-word rule is for profile software rows, never the vendor check of
    # ``shipped_match`` (``Acme`` in ``Acme Agent 6.0``, ``Mozilla`` in ``Mozilla Firefox ESR``).
    agent = oc.ShippedProduct(product="Acme Agent", vendor="Acme")
    assert gate.shipped_match(agent, "Acme Agent 6.0") == "product"
    firefox = oc.ShippedProduct(product="Firefox", vendor="Mozilla")
    assert gate.shipped_match(firefox, "Mozilla Firefox ESR") == "product"
    assert gate.shipped_match(oc.ShippedProduct(product="Agent", vendor="Acme"), "Agent for Windows") == ""
