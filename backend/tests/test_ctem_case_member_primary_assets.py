# SPDX-License-Identifier: Apache-2.0

"""#2563: a product case member reaches the primary assets its software or its assets reach."""

from __future__ import annotations

import pytest

from app.services import ctem_cases as rules
from app.services import ctem_cases_source as source
from app.services import ctem_primary_assets as pa

WORKSTATIONS = "Employee workstations (laptops and desktops)"
NAMES = {pa.primary_asset_key(WORKSTATIONS): WORKSTATIONS}


def _index(source_kind: str = "manual") -> pa.PrimaryAssetIndex:
    link = pa.Link(pa.primary_asset_key(WORKSTATIONS), source_kind, ("used_by",))
    # #2680: a bare family product (``Windows``) reaches only a device of that known OS family.
    reacher = pa.Reacher(
        name="LAPTOP-042", keys={"laptop-042"}, products={"windows 11": "Microsoft Windows 11"}, os_family="Windows"
    )
    reacher.links.append((link, "asset_group", "All Windows x86_64"))
    return pa.PrimaryAssetIndex(loaded=True, reachers=[reacher])


def _octi(product: str = "Windows", vuln: str = "v1") -> rules.SourceIssue:
    return rules.SourceIssue(source="opencti", external_id=vuln, name="CVE-2026-1", cve="CVE-2026-1", product=product)


def test_an_opencti_member_whose_product_runs_on_a_linked_asset_counts_one_proven_primary_asset() -> None:
    [member] = source.with_primary_assets([_octi()], _index("manual"), NAMES)
    assert member.primary_assets == (WORKSTATIONS,) and member.inferred_primary_assets == ()
    agg = rules.aggregates([member])
    assert agg["primary_asset_count"] == 1 and agg["inferred_primary_asset_count"] == 0


def test_an_opencrq_link_proves_too() -> None:
    [member] = source.with_primary_assets([_octi()], _index("opencrq"), NAMES)
    assert rules.aggregates([member])["primary_asset_count"] == 1


def test_a_rule_link_counts_as_inferred() -> None:
    [member] = source.with_primary_assets([_octi()], _index("rule"), NAMES)
    assert member.inferred_primary_assets == (WORKSTATIONS,)
    agg = rules.aggregates([member])
    assert agg["primary_asset_count"] == 0 and agg["inferred_primary_asset_count"] == 1


def test_a_product_no_linked_asset_runs_reaches_nothing() -> None:
    [member] = source.with_primary_assets([_octi("Chrome")], _index(), NAMES)
    assert member.primary_assets == ()
    assert rules.aggregates([member])["primary_asset_count"] == 0


def test_an_openaev_member_reaches_through_the_asset_it_was_found_on() -> None:
    finding = rules.SourceIssue(source="openaev", external_id="f1", name="CVE-2026-2", assets=("LAPTOP-042",))
    [member] = source.with_primary_assets([finding], _index(), NAMES)
    assert member.primary_assets == (WORKSTATIONS,)


def test_a_signal_member_keeps_what_its_gate_decision_recorded() -> None:
    signal = rules.SourceIssue(source="signal", external_id="s1", name="Windows", product="Windows")
    [member] = source.with_primary_assets([signal], _index(), NAMES)
    assert member is signal


def test_without_links_the_members_are_unchanged() -> None:
    issue = _octi()
    assert source.with_primary_assets([issue], pa.PrimaryAssetIndex(loaded=True), NAMES) == [issue]


def test_a_link_to_a_primary_asset_the_profile_no_longer_names_keeps_its_key() -> None:
    [member] = source.with_primary_assets([_octi()], _index(), {})
    assert member.primary_assets == (pa.primary_asset_key(WORKSTATIONS),)


def test_the_members_of_one_product_share_one_reach(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    real = pa.member_primary_assets

    def counted(
        index: pa.PrimaryAssetIndex, names: dict[str, str], *, software: list[str], keys: list[str]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        calls.append(tuple(software))
        return real(index, names, software=software, keys=keys)

    monkeypatch.setattr(pa, "member_primary_assets", counted)
    members = source.with_primary_assets([_octi(vuln="v1"), _octi(vuln="v2")], _index(), NAMES)
    assert calls == [("Windows",)]
    assert all(m.primary_assets == (WORKSTATIONS,) for m in members)
