# SPDX-License-Identifier: Apache-2.0

"""#2780: on the relevant_threat path a name-only, unverified hop sets no case criticality.

A threat whose OpenCTI software list names ``Citrix Analytics``, ``React Server Components``,
``Office`` or ``Safari 0`` reached inventory rows through one shared word (npm ``analytics``,
``react``, a Teams add-in ``for Microsoft Office``, ``Safari``) and took the criticality of the
primary asset behind them. Such a hop stays recorded as a possible exposure (``match: name``),
never sets the scope criticality; the other sources decide, else the signal goes to validation.
A typed profile row met by one word (``express`` for ``Express 7 0``) is not resolved either,
so it never re-enters the reach as a new reference.
"""

from __future__ import annotations

from datetime import date

from app.services import ctem_primary_assets as pa
from app.services import ctem_scope_gate as gate
from app.services import organization_context as oc

TODAY = date(2026, 11, 28)
CODE = "Source code"
WORKSTATIONS = "Employee workstations"
LAPTOPS = "Laptops"


def _profile(software: list[dict[str, str]] | None = None) -> oc.OrganizationProfile:
    return oc.parse_profile(
        {
            "primary_assets": [
                {"name": CODE, "criticality": "critical"},
                {"name": WORKSTATIONS, "criticality": "high"},
                {"name": LAPTOPS, "criticality": "high"},
            ],
            "software": software or [],
        }
    )


def _reacher(
    name: str,
    labels: list[str],
    primary_asset: str,
    *,
    repository: bool = False,
    os_family: str = "",
    versions: dict[str, set[str]] | None = None,
) -> pa.Reacher:
    reacher = pa.Reacher(
        name=name,
        keys={name.lower()},
        products={label.lower(): label for label in labels},
        repository=repository,
        os_family=os_family,
    )
    for label, found in (versions or {}).items():
        reacher.versioned.add(label)
        reacher.versions[label] = set(found)
    reacher.links.append((pa.Link(pa.primary_asset_key(primary_asset), "rule", ("used_by",)), "asset", name))
    return reacher


def _threat(*software: str) -> gate.SignalFacts:
    return gate.SignalFacts(
        trigger_path="relevant_threat",
        lens="world",
        subject_kind="threat",
        subject_ref="APT-X",
        subject_label="APT-X",
        source_provider="opencti",
        evidence={"stix_type": "intrusion-set"},
        threat_software=tuple(software),
        threat_lookup="read",
    )


def _cve(*software: str) -> gate.SignalFacts:
    return gate.SignalFacts(
        trigger_path="new_vulnerability",
        lens="world",
        subject_kind="vulnerability",
        subject_ref="CVE-2026-0001",
        source_provider="opencti",
        evidence={"software": list(software), "cvss": 9.8},
    )


def _primary_asset_sources(decision: gate.Decision) -> list[str]:
    scope = decision.record.get("scope") or {}
    return [s["name"] for s in scope.get("sources") or [] if s["from"] == "primary_asset"]


def test_one_shared_word_sets_no_criticality_and_the_verified_hop_decides() -> None:
    """npm ``analytics`` (critical) by ``Citrix Analytics``; Chrome 120 (high) verified by its CVE range."""
    index = pa.PrimaryAssetIndex(
        loaded=True,
        reachers=[
            _reacher("filigran/web", ["analytics"], CODE, repository=True),
            _reacher(
                "WS-01",
                ["Google Chrome 120.0.1"],
                WORKSTATIONS,
                os_family="Windows",
                versions={"Google Chrome 120.0.1": {"120.0.1"}},
            ),
        ],
    )
    decision = gate.decide(
        _profile(), _threat("Citrix Analytics", "Google Chrome 120"), primary_assets=index, today=TODAY
    )
    hops = {h["asset"]: h for h in decision.record["reconcile"]["primary_assets"]}
    assert hops["filigran/web"]["match"] == "name" and hops["filigran/web"]["version_verified"] is False
    assert hops["WS-01"]["version_verified"] is True
    assert _primary_asset_sources(decision) == [WORKSTATIONS]
    assert decision.record["scope"]["criticality"] == "high"
    assert decision.record["scope"]["name_only_hops"] == 1


def test_only_name_only_hops_go_to_validation() -> None:
    index = pa.PrimaryAssetIndex(
        loaded=True,
        reachers=[
            _reacher("filigran/web", ["react"], CODE, repository=True),
            _reacher(
                "LAPTOP-1",
                ["Microsoft Teams Meeting Add-in for Microsoft Office"],
                LAPTOPS,
                os_family="Windows",
            ),
            _reacher("MAC-1", ["Safari"], LAPTOPS, os_family="macOS"),
        ],
    )
    decision = gate.decide(
        _profile(), _threat("React Server Components", "Office", "Safari 0"), primary_assets=index, today=TODAY
    )
    hops = decision.record["reconcile"]["primary_assets"]
    assert hops and all(h["match"] == "name" for h in hops)
    assert decision.outcome == "to_validate"
    assert decision.record["reason_code"] == "version_not_verified"
    assert decision.record["reason_params"]["kind"] == "name_only_threat_reach"
    assert decision.severity is None


def test_a_one_word_profile_row_is_not_resolved_and_does_not_widen_the_reach() -> None:
    """``Express 0`` / ``Express 7 0`` (hardware), profile npm ``express``, inventory rows holding that word."""
    index = pa.PrimaryAssetIndex(
        loaded=True,
        reachers=[
            _reacher("filigran/api", ["express"], CODE, repository=True),
            _reacher("WS-02", ["Garmin Express"], WORKSTATIONS, os_family="Windows"),
            _reacher("WS-03", ["IIS 10.0 Express"], WORKSTATIONS, os_family="Windows"),
        ],
    )
    profile = _profile([{"product": "express"}])
    decision = gate.decide(profile, _threat("Express 0", "Express 7 0"), primary_assets=index, today=TODAY)
    reconcile = decision.record["reconcile"]
    assert reconcile["products"] == []
    assert all(h["match"] == "name" for h in reconcile["primary_assets"])
    assert not any(h["asset"] in {"WS-02", "WS-03"} for h in reconcile["primary_assets"])
    assert decision.outcome != "case"
    assert (decision.record.get("scope") or {}).get("criticality") != "critical"


def test_a_vendor_and_product_hop_still_sets_the_criticality() -> None:
    index = pa.PrimaryAssetIndex(
        loaded=True,
        reachers=[_reacher("WS-04", ["Citrix Analytics 2.1"], CODE, os_family="Windows")],
    )
    decision = gate.decide(_profile(), _threat("Citrix Analytics"), primary_assets=index, today=TODAY)
    hops = decision.record["reconcile"]["primary_assets"]
    assert [h["match"] for h in hops] == ["vendor_product"]
    assert decision.record["scope"]["criticality"] == "critical"


def test_a_typed_profile_row_named_in_full_still_resolves() -> None:
    profile = _profile([{"product": "Ivanti Connect Secure"}])
    decision = gate.decide(profile, _threat("Ivanti Connect Secure"), today=TODAY)
    assert decision.record["reconcile"]["products"] == ["Ivanti Connect Secure"]


def test_the_match_kind_of_each_shape() -> None:
    assert pa.match_kind("cpe:2.3:a:citrix:analytics:*", "analytics") == "cpe"
    assert pa.match_kind("Citrix Analytics", "Citrix Analytics 2.1") == "vendor_product"
    assert pa.match_kind("Analytics Server", "Citrix Analytics Server") == "vendor_product"
    assert pa.match_kind("Citrix Analytics", "analytics") == "name"
    assert pa.match_kind("Office", "Microsoft Teams Meeting Add-in for Microsoft Office") == "name"
    assert pa.match_kind("Safari 0", "Safari") == "name"
    assert pa.one_word_in_title("express", "Garmin Express")
    assert not pa.one_word_in_title("express", "Express 7 0")
    assert not pa.one_word_in_title("nginx", "nginx_plus")


def test_the_vulnerability_path_is_unchanged() -> None:
    """A name-only CVE reach still goes to validation by #2628's rule, not by the threat rule."""
    index = pa.PrimaryAssetIndex(loaded=True, reachers=[_reacher("filigran/web", ["react"], CODE, repository=True)])
    decision = gate.decide(_profile(), _cve("React Server Components"), primary_assets=index, today=TODAY)
    assert decision.record["reason_code"] == "version_not_verified"
    assert "kind" not in decision.record["reason_params"]
