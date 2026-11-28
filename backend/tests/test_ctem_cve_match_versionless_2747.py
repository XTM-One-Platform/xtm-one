# SPDX-License-Identifier: Apache-2.0

"""#2747: a software reference that names no version is never verified by a versioned row alone.

After a cases reader catch-up, KEV CVEs whose OpenCTI software named no version
(``Firefox``, ``VirtualBox``, ``Orion``) were ``version_verified`` on any device running a
versioned row of the product, so the decision said proven, high, and opened P1 cases on
Firefox 146 for a CVE fixed in 131.0.2. A versionless reference is now compared with the
CVE's fixed version when the evidence carries one, and verified by nothing otherwise.
"""

from __future__ import annotations

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


def _profile() -> oc.OrganizationProfile:
    return oc.parse_profile(
        {
            "primary_assets": [{"name": WORKSTATIONS, "criticality": "critical"}],
            "prioritization_policy": {"min_primary_asset_criticality": "medium", "kev_always_in": True},
        }
    )


def _device(name: str, label: str, *versions: str, os_family: str = "macOS") -> pa.Reacher:
    reacher = pa.Reacher(
        name=name,
        keys={name.lower()},
        products={label.lower(): label, os_family.lower(): os_family},
        os_family=os_family,
        versioned={label},
        versions={label: set(versions)},
    )
    reacher.links.append((pa.Link(pa.primary_asset_key(WORKSTATIONS), "rule", ("used_by",)), "asset", name))
    return reacher


def _index(*reachers: pa.Reacher) -> pa.PrimaryAssetIndex:
    return pa.PrimaryAssetIndex(loaded=True, reachers=list(reachers))


def _kev_cve(software: str, *, fixed_version: str | None = None) -> gate.SignalFacts:
    evidence: dict[str, object] = {"software": [software], "cve": "CVE-2024-9680", "kev": True, "cvss": 9.8}
    if fixed_version:
        evidence["fixed_version"] = fixed_version
    return gate.SignalFacts(
        trigger_path="new_vulnerability",
        lens="world",
        subject_kind="vulnerability",
        subject_ref="CVE-2024-9680",
        source_provider="opencti",
        evidence=evidence,
        cve_kev=True,
    )


def test_a_versionless_reference_with_no_fixed_version_verifies_nothing() -> None:
    assert not pa.version_in_reference("Firefox", {"146.0"}, "Firefox")
    hits = _index(_device("MAC-01", "Firefox", "146.0")).reach(["Firefox"])
    assert [h["version_verified"] for h in hits] == [False]


def test_a_versionless_reference_is_compared_with_the_fixed_version() -> None:
    assert not pa.version_in_reference("Firefox", {"146.0", "160.0.1"}, "Firefox", fixed_version="131.0.2")
    assert pa.version_in_reference("Firefox", {"146.0", "120.0"}, "Firefox", fixed_version="131.0.2")
    above = _index(_device("MAC-01", "Firefox", "146.0")).reach(["Firefox"], fixed_version="131.0.2")
    assert [h["version_verified"] for h in above] == [False]
    below = _index(_device("MAC-02", "Firefox", "130.0")).reach(["Firefox"], fixed_version="131.0.2")
    assert [h["version_verified"] for h in below] == [True]


def test_a_kev_cve_above_the_fix_opens_no_case() -> None:
    decision = gate.decide(
        _profile(),
        _kev_cve("Firefox", fixed_version="131.0.2"),
        primary_assets=_index(_device("MAC-01", "Firefox", "146.0")),
        today=TODAY,
    )
    assert decision.outcome != "case"
    assert all(h["version_verified"] is False for h in decision.record["reconcile"]["primary_assets"])
    assert decision.record["reconcile"]["proven"] is False


def test_a_kev_cve_with_no_range_and_no_fix_is_to_validate_and_not_proven() -> None:
    decision = gate.decide(
        _profile(), _kev_cve("Firefox"), primary_assets=_index(_device("MAC-01", "Firefox", "146.0")), today=TODAY
    )
    assert decision.outcome == "to_validate"
    assert decision.record["reason_code"] == "version_not_verified"
    assert decision.record["reconcile"]["proven"] is False
    assert decision.record["reconcile"]["confidence"] != "high"
    assert all(h["version_verified"] is False for h in decision.record["reconcile"]["primary_assets"])


def test_a_range_the_running_version_is_above_verifies_nothing() -> None:
    hits = _index(_device("MAC-01", "VirtualBox", "7.2.6")).reach(["VirtualBox < 1.6.4"])
    assert all(h["version_verified"] is False for h in hits)
    decision = gate.decide(
        _profile(),
        _kev_cve("VirtualBox < 1.6.4"),
        primary_assets=_index(_device("MAC-01", "VirtualBox", "7.2.6")),
        today=TODAY,
    )
    assert decision.outcome != "case"


def test_a_name_collision_with_no_version_is_not_a_case() -> None:
    decision = gate.decide(
        _profile(), _kev_cve("SolarWinds Orion"), primary_assets=_index(_device("MAC-01", "Orion", "1.1")), today=TODAY
    )
    assert decision.outcome != "case"
    assert decision.record["reconcile"]["proven"] is False


def test_a_kev_cve_whose_fix_is_above_the_running_version_still_opens_a_case() -> None:
    decision = gate.decide(
        _profile(),
        _kev_cve("Firefox", fixed_version="131.0.2"),
        primary_assets=_index(_device("MAC-02", "Firefox", "130.0")),
        today=TODAY,
    )
    assert decision.record["reconcile"]["primary_assets"][0]["version_verified"] is True
    assert decision.record["reconcile"]["proven"] is True
    assert decision.outcome == "case"


def test_the_fixed_version_is_read_from_the_evidence_or_its_case_fix() -> None:
    assert gate.evidence_fixed_version({"fixed_version": " 131.0.2 "}) == "131.0.2"
    assert gate.evidence_fixed_version({"case_fix": {"fixed_version": "86.0"}}) == "86.0"
    assert gate.evidence_fixed_version({"case_fix": {"fixed_version": None}}) is None
    assert gate.evidence_fixed_version({}) is None
