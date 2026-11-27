# SPDX-License-Identifier: Apache-2.0

"""#2701: a signal whose evidence names its assets reaches those assets alone.

A manual ``relevant_threat`` signal for one confirmed device exposure, with
``evidence.assets`` naming the device and ``software`` naming the product it runs,
opened a case on the device plus every repository and device running the product.
Named assets are the whole reach: the software still labels and version-checks
them, but adds no asset, repository or primary asset of its own. Without asset
evidence, the product expansion is unchanged.
"""

from __future__ import annotations

from datetime import date

from app.services import ctem_primary_assets as pa
from app.services import ctem_scope_gate as gate
from app.services import organization_context as oc

TODAY = date(2026, 11, 27)
WORKSTATIONS = "Employee workstations"
CODE = "Source code"
PRODUCT = "Acme Browser"
DEVICE = "MAC-01"
DEVICE_HOST = "mac-01.corp.example"
REPOSITORY = "acme/acme-browser"


def _profile() -> oc.OrganizationProfile:
    return oc.parse_profile(
        {
            "primary_assets": [
                {"name": WORKSTATIONS, "criticality": "critical"},
                {"name": CODE, "criticality": "critical"},
            ],
            "software": [{"product": PRODUCT}],
            "assets_exposure": [
                {"name": DEVICE, "category": "host", "hostname": DEVICE_HOST, "products": [PRODUCT]},
                {"name": "MAC-02", "category": "host", "products": [PRODUCT]},
                {"name": REPOSITORY, "category": "source_code", "subcategory": "code_repository"},
            ],
            "prioritization_policy": {"min_primary_asset_criticality": "medium"},
        }
    )


def _reacher(name: str, *, primary_asset: str, repository: bool = False, keys: set[str] | None = None) -> pa.Reacher:
    reacher = pa.Reacher(
        name=name,
        keys=keys or {name.lower()},
        products={PRODUCT.lower(): PRODUCT},
        os_family="" if repository else "macOS",
        repository=repository,
    )
    reacher.links.append((pa.Link(pa.primary_asset_key(primary_asset), "rule", ("used_by",)), "asset", name))
    return reacher


def _index() -> pa.PrimaryAssetIndex:
    device = _reacher(DEVICE, primary_asset=WORKSTATIONS, keys={DEVICE.lower(), DEVICE_HOST})
    device.versioned.add(PRODUCT)
    return pa.PrimaryAssetIndex(
        loaded=True,
        reachers=[
            device,
            _reacher("MAC-02", primary_asset=WORKSTATIONS),
            _reacher(REPOSITORY, primary_asset=CODE, repository=True),
        ],
    )


def _signal(**evidence: object) -> gate.SignalFacts:
    return gate.SignalFacts(
        trigger_path="manual",
        lens="internal",
        subject_kind="threat",
        subject_ref="Acme Browser engine exploitation",
        source_provider="manual",
        evidence={"software": [PRODUCT], **evidence},
    )


def test_named_assets_are_the_whole_reach() -> None:
    resolution = gate.resolve(_profile(), _signal(assets=[DEVICE]), _index())
    assert [a.name for a in resolution.assets] == [DEVICE]
    assert resolution.repositories == [] and resolution.shipped == []
    assert resolution.products == [PRODUCT]
    assert {h["asset"] for h in resolution.primary_assets} == {DEVICE}
    assert resolution.primary_asset_totals == {
        pa.primary_asset_key(WORKSTATIONS): {"assets": 1, "direct": 1, "via_group": 0}
    }
    assert resolution.scope == gate.EVIDENCE_ASSETS_SCOPE


def test_the_decision_records_the_narrowing_and_names_no_repository() -> None:
    decision = gate.decide(_profile(), _signal(assets=[DEVICE]), primary_assets=_index(), today=TODAY)
    reconcile = decision.record["reconcile"]
    assert reconcile["scope"] == "evidence_assets"
    assert reconcile["assets"] == [DEVICE]
    assert all(h["asset"] == DEVICE for h in reconcile["primary_assets"])
    assert REPOSITORY not in str(reconcile["primary_assets"]) and reconcile["repositories"] == []
    hop = reconcile["primary_assets"][0]
    assert hop["software"] == PRODUCT and hop["version_verified"] is True


def test_without_named_assets_the_product_expansion_is_unchanged() -> None:
    resolution = gate.resolve(_profile(), _signal(), _index())
    assert {a.name for a in resolution.assets} == {DEVICE, "MAC-02", REPOSITORY}
    assert resolution.repositories == [REPOSITORY]
    assert {h["asset"] for h in resolution.primary_assets} == {DEVICE, "MAC-02", REPOSITORY}
    assert resolution.scope == ""
    decision = gate.decide(_profile(), _signal(), primary_assets=_index(), today=TODAY)
    assert decision.record["reconcile"]["scope"] is None


def test_a_named_hostname_with_the_cve_software_narrows_the_same_way() -> None:
    facts = gate.SignalFacts(
        trigger_path="manual",
        lens="internal",
        subject_kind="vulnerability",
        subject_ref="CVE-2026-2701",
        source_provider="manual",
        evidence={"hostnames": [DEVICE_HOST]},
        cve_software=(PRODUCT,),
    )
    resolution = gate.resolve(_profile(), facts, _index())
    assert [a.name for a in resolution.assets] == [DEVICE]
    assert {h["asset"] for h in resolution.primary_assets} == {DEVICE}
    assert resolution.repositories == []
    assert resolution.scope == gate.EVIDENCE_ASSETS_SCOPE


def test_a_single_device_signal_never_yields_more_than_its_device() -> None:
    many = [_reacher(f"MAC-{n:03d}", primary_asset=WORKSTATIONS) for n in range(169)]
    index = pa.PrimaryAssetIndex(loaded=True, reachers=[*_index().reachers, *many])
    resolution = gate.resolve(_profile(), _signal(assets=[DEVICE]), index)
    assert {h["asset"] for h in resolution.primary_assets} == {DEVICE}
    assert sum(row["assets"] for row in resolution.primary_asset_totals.values()) == 1


def test_an_inventory_device_the_profile_list_does_not_hold_still_narrows() -> None:
    """The profile's asset list is capped: a device only the link index knows still names the reach."""
    index = _index()
    index.reachers.append(_reacher("MAC-777", primary_asset=WORKSTATIONS))
    resolution = gate.resolve(_profile(), _signal(assets=["MAC-777"]), index)
    assert {h["asset"] for h in resolution.primary_assets} == {"MAC-777"}
    assert resolution.scope == gate.EVIDENCE_ASSETS_SCOPE


def test_the_supporting_list_follows_the_named_assets() -> None:
    index = _index()
    rows = index.supporting([PRODUCT], [DEVICE.lower()], pa.primary_asset_key(WORKSTATIONS), keys_only=True)
    assert [r["asset"] for r in rows] == [DEVICE]
    wide = index.supporting([PRODUCT], [DEVICE.lower()], pa.primary_asset_key(WORKSTATIONS))
    assert {r["asset"] for r in wide} == {DEVICE, "MAC-02"}
