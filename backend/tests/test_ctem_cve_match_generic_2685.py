# SPDX-License-Identifier: Apache-2.0

"""#2685: a generic product word never matches every device and repository.

A KEV CVE on a vendor's client updater, whose OpenCTI software list held only
``Client``, reached 68 workstations and 8 repositories, every hop version verified,
and opened a proven P1 case. A bare generic word names only an equal label and is
never verified, a CPE whose product is one names only a label of its vendor, a
repository never runs it through its SBOM, and KEV alone no longer makes a
name-only reach proven.
"""

from __future__ import annotations

from datetime import date

from app.services import ctem_primary_assets as pa
from app.services import ctem_scope_gate as gate
from app.services import organization_context as oc

TODAY = date(2026, 11, 27)
WORKSTATIONS = "Employee workstations"
ANYCONNECT = "Cisco AnyConnect Secure Mobility Client"
TRUECONF_CPE = "cpe:2.3:a:trueconf:client:8.1.0:*:*:*:*:*:*:*"


def _profile() -> oc.OrganizationProfile:
    return oc.parse_profile(
        {
            "primary_assets": [{"name": WORKSTATIONS, "criticality": "critical"}],
            "prioritization_policy": {"min_primary_asset_criticality": "medium"},
        }
    )


def _reacher(name: str, products: dict[str, str], *, os_family: str = "", repository: bool = False) -> pa.Reacher:
    reacher = pa.Reacher(
        name=name,
        keys={name.lower()},
        products=dict(products),
        os_family=os_family,
        versioned=set(products.values()),
        repository=repository,
    )
    reacher.links.append((pa.Link(pa.primary_asset_key(WORKSTATIONS), "rule", ("used_by",)), "asset_group", "All"))
    return reacher


def _fleet() -> pa.PrimaryAssetIndex:
    return pa.PrimaryAssetIndex(
        loaded=True,
        reachers=[
            _reacher("LAPTOP-W11", {"a": ANYCONNECT}, os_family="Windows"),
            _reacher("MAC-01", {"t": "TrueConf Client 8.1"}, os_family="macOS"),
            # A repository whose SBOM declares a dependency named ``client``.
            _reacher("filigran/website", {"x": "client"}, repository=True),
        ],
    )


def _kev_cve(*software: str) -> gate.SignalFacts:
    return gate.SignalFacts(
        trigger_path="new_vulnerability",
        lens="world",
        subject_kind="vulnerability",
        subject_ref="CVE-2026-99002",
        source_provider="opencti",
        evidence={"cvss": 8.8},
        cve_software=tuple(software),
        cve_kev=True,
    )


def test_a_generic_word_names_only_an_equal_label() -> None:
    assert pa.is_generic_word("Client")
    assert not pa.product_matches("Client", ANYCONNECT)
    assert not pa.product_matches("Client", "TrueConf Client")
    assert not pa.product_matches("Client", "TrueConf Client", family_exact=False)
    assert pa.product_matches("Client", "client")
    assert pa.product_matches("AnyConnect Secure Mobility Client", ANYCONNECT)
    assert not gate._product_matches("Client", "TrueConf Client")


def test_the_issue_reproduction_reaches_nothing() -> None:
    r = pa.Reacher(name="laptop")
    r.products = {"a": ANYCONNECT}
    r.versioned = {ANYCONNECT}
    r.links = [(pa.Link("pa", "inferred"), "asset", "laptop")]
    assert pa.PrimaryAssetIndex(loaded=True, reachers=[r]).reach(["Client"]) == []


def test_a_generic_word_reaches_no_repository_and_is_never_verified() -> None:
    assert _fleet().reach(["Client"]) == []
    device = _reacher("LAPTOP-X", {"c": "Client"}, os_family="Windows")
    hits = pa.PrimaryAssetIndex(loaded=True, reachers=[device]).reach(["Client"])
    assert [h["asset"] for h in hits] == ["LAPTOP-X"]
    assert hits[0]["version_verified"] is False
    assert not pa.runs_versioned(device, "Client")


def test_a_generic_cpe_reaches_only_the_vendor_product() -> None:
    assert pa.product_matches("TrueConf Client 8.1", TRUECONF_CPE)
    assert not pa.product_matches(ANYCONNECT, TRUECONF_CPE)
    assert not pa.product_matches("client", TRUECONF_CPE)
    hits = _fleet().reach([TRUECONF_CPE])
    assert [h["asset"] for h in hits] == ["MAC-01"]
    assert hits[0]["version_verified"] is True


def test_a_kev_cve_naming_only_client_opens_no_case_and_is_not_proven() -> None:
    decision = gate.decide(_profile(), _kev_cve("Client"), primary_assets=_fleet(), today=TODAY)
    assert decision.outcome != "case"
    assert decision.record["reconcile"]["primary_assets"] == []
    assert decision.record["reconcile"]["exploited_in_wild"] is True


def test_a_kev_reach_by_name_alone_is_to_validate_and_not_proven() -> None:
    index = pa.PrimaryAssetIndex(loaded=True, reachers=[_reacher("LAPTOP-X", {"c": "Client"}, os_family="Windows")])
    decision = gate.decide(_profile(), _kev_cve("Client"), primary_assets=index, today=TODAY)
    assert decision.outcome == "to_validate"
    assert decision.record["reason_code"] == "version_not_verified"
    assert decision.record["reconcile"]["proven"] is False
    assert decision.record["reconcile"]["confidence"] != "high"


def test_a_kev_reach_from_a_versioned_row_stays_proven() -> None:
    decision = gate.decide(_profile(), _kev_cve(TRUECONF_CPE), primary_assets=_fleet(), today=TODAY)
    assert decision.record["reconcile"]["primary_assets"][0]["version_verified"] is True
    assert decision.record["reconcile"]["proven"] is True
    assert decision.outcome == "case"
