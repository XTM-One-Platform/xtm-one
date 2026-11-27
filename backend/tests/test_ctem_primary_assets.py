# SPDX-License-Identifier: Apache-2.0

"""#1591 S3: the scope gate reaches primary assets through inventory links (pure rules)."""

from __future__ import annotations

from datetime import date

from app.services import ctem_primary_assets as pa
from app.services import ctem_scope_gate as gate
from app.services import organization_context as oc

TODAY = date(2026, 11, 24)
WORKSTATIONS = "Employee workstations (laptops and desktops)"


def _profile(**policy: object) -> oc.OrganizationProfile:
    return oc.parse_profile(
        {
            "primary_assets": [
                {"name": WORKSTATIONS, "criticality": "high", "source": "opencrq", "external_id": "pa-1"}
            ],
            "software": [{"product": "Windows 11"}],
            "prioritization_policy": {"min_primary_asset_criticality": "medium", "cvss_min": 7, **policy},
        }
    )


def _key(name: str) -> str:
    return " ".join(name.split()).casefold()


def _index(
    *, source: str = "rule", products: tuple[str, ...] = ("Microsoft Windows 11 Enterprise",), via_group: bool = True
) -> pa.PrimaryAssetIndex:
    link = pa.Link(_key(WORKSTATIONS), source, ("used_by",))
    reacher = pa.Reacher(
        name="LAPTOP-042", keys={"laptop-042"}, products={_key(p): p for p in products}, os_family="Windows"
    )
    reacher.links.append(
        (link, "asset_group" if via_group else "asset", "All Windows x86_64" if via_group else "LAPTOP-042")
    )
    return pa.PrimaryAssetIndex(loaded=True, reachers=[reacher])


def _cve(**evidence: object) -> gate.SignalFacts:
    return gate.SignalFacts(
        trigger_path="new_vulnerability",
        lens="threat",
        subject_kind="vulnerability",
        subject_ref="CVE-2026-88249",
        source_provider="opencti",
        evidence={"software": ["Windows 11"], "cvss": 8.8, **evidence},
    )


def test_without_links_a_windows_cve_stays_held_by_the_primary_asset_policy() -> None:
    decision = gate.decide(_profile(), _cve(), today=TODAY)
    assert decision.outcome == "watchlist"
    assert decision.record["reason_code"] == "primary_asset_below_policy"
    assert decision.record["reconcile"]["inventory_links_read"] is False


def test_a_link_through_an_asset_group_of_intune_devices_opens_the_case_with_the_chain() -> None:
    decision = gate.decide(_profile(), _cve(), primary_assets=_index(), today=TODAY)
    assert decision.outcome == "case" and decision.state == "confirmed"
    pa_row = next(s for s in decision.record["scope"]["sources"] if s["from"] == "primary_asset")
    assert pa_row["name"] == WORKSTATIONS and pa_row["criticality"] == "high"
    hop = pa_row["chain"][0]
    # #2711: the label the device runs, the CVE software reference beside it.
    assert hop["software"] == "Microsoft Windows 11 Enterprise" and hop["reference"] == "Windows 11"
    assert hop["asset"] == "LAPTOP-042"
    assert hop["through_kind"] == "asset_group" and hop["through"] == "All Windows x86_64"
    assert hop["source"] == "rule" and hop["roles"] == ["used_by"]
    assert decision.record["reconcile"]["primary_assets"][0]["primary_asset_key"] == _key(WORKSTATIONS)


def test_a_direct_record_link_and_the_device_os_family_reach_the_primary_asset_to_validate() -> None:
    """#2628: a CVE naming only the OS family reaches the device, never proven: to_validate."""
    decision = gate.decide(
        _profile(),
        _cve(software=["Windows"]),
        primary_assets=_index(via_group=False, products=("Windows",)),
        today=TODAY,
    )
    assert decision.record["reconcile"]["primary_assets"]
    assert decision.outcome == "to_validate"
    assert decision.record["reason_code"] == "version_not_verified"


def test_software_the_devices_do_not_run_reaches_nothing() -> None:
    facts = _cve(software=["nginx"])
    decision = gate.decide(_profile(), facts, primary_assets=_index(), today=TODAY)
    assert decision.record["reconcile"]["primary_assets"] == []
    assert decision.outcome != "case"


def test_the_link_removed_sends_it_back_to_the_watchlist() -> None:
    empty = pa.PrimaryAssetIndex(loaded=True)
    decision = gate.decide(_profile(), _cve(), primary_assets=empty, today=TODAY)
    assert decision.outcome == "watchlist" and decision.record["reason_code"] == "primary_asset_below_policy"
    assert decision.record["reconcile"]["inventory_links_read"] is True


def test_the_subject_key_of_a_linked_device_reaches_its_primary_asset() -> None:
    facts = gate.SignalFacts(
        trigger_path="new_exposure", lens="outside", subject_kind="asset", subject_ref="LAPTOP-042", source_provider="x"
    )
    decision = gate.decide(_profile(cvss_min=None), facts, primary_assets=_index(), today=TODAY)
    assert decision.record["reconcile"]["primary_assets"]
    assert decision.outcome == "case"


def test_a_manual_link_is_listed_before_a_synced_one() -> None:
    index = _index(source="opencrq")
    index.reachers[0].links.append((pa.Link(_key(WORKSTATIONS), "manual"), "asset", "LAPTOP-042"))
    hits = index.reach(["Windows"])
    assert [h["source"] for h in hits][:1] == ["manual"]


def test_the_threshold_says_where_it_comes_from() -> None:
    default = gate.decide(_profile(), _cve(), primary_assets=_index(), today=TODAY)
    assert default.record["scope"]["threshold"] == "high" and default.record["scope"]["threshold_source"] == "default"
    set_ = gate.decide(_profile(scope_min_criticality="medium"), _cve(), primary_assets=_index(), today=TODAY)
    assert set_.record["scope"]["threshold_source"] == "policy"


def _fleet(count: int) -> pa.PrimaryAssetIndex:
    """*count* Windows laptops in one group linked to the workstations, one also linked directly."""
    link = pa.Link(_key(WORKSTATIONS), "rule", ("stores",))
    reachers = []
    for n in range(count):
        reacher = pa.Reacher(
            name=f"LAPTOP-{n:05d}", keys={f"laptop-{n:05d}"}, products={"windows": "Windows 11"}, os_family="Windows"
        )
        reacher.links.append((link, "asset_group", "All Windows x86_64"))
        reachers.append(reacher)
    reachers[0].links.append((pa.Link(_key(WORKSTATIONS), "manual", ("processes",)), "asset", "LAPTOP-00000"))
    return pa.PrimaryAssetIndex(loaded=True, reachers=reachers)


def test_the_record_counts_every_supporting_asset_behind_the_chains_it_keeps() -> None:
    """#1979: 10,000 laptops read 10,000 on the primary asset source, never the 5 chains kept."""
    decision = gate.decide(_profile(), _cve(), primary_assets=_fleet(10_000), today=TODAY)
    pa_row = next(s for s in decision.record["scope"]["sources"] if s["from"] == "primary_asset")
    assert len(pa_row["chain"]) <= gate.CHAINS_KEPT
    assert pa_row["assets_total"] == 10_000
    assert pa_row["chain_total"] == len(
        [h for h in decision.record["reconcile"]["primary_assets"] if h["primary_asset_key"] == _key(WORKSTATIONS)]
    )
    totals = decision.record["reconcile"]["primary_asset_totals"][_key(WORKSTATIONS)]
    assert totals == {"assets": 10_000, "direct": 1, "via_group": 9_999}
    assert decision.record["reconcile"]["inventory_links_truncated"] is False


def test_supporting_lists_each_asset_once_best_link_first_sorted_by_name() -> None:
    rows = _fleet(3).supporting(["Windows"], [], _key(WORKSTATIONS))
    assert [r["asset"] for r in rows] == ["LAPTOP-00000", "LAPTOP-00001", "LAPTOP-00002"]
    first = rows[0]
    assert first["source"] == "manual" and first["through_kind"] == "asset"
    assert first["roles"] == ["processes", "stores"] and first["software"] == "Windows"
    assert rows[1]["through_kind"] == "asset_group" and rows[1]["through"] == "All Windows x86_64"
    assert _fleet(3).supporting(["nginx"], [], _key(WORKSTATIONS)) == []
    assert _fleet(3).supporting(["Windows"], [], "payroll") == []


def test_product_matching_is_by_whole_word_and_reads_a_cpe() -> None:
    assert pa.product_matches("Windows 11", "Microsoft Windows 11 Enterprise")
    # #2628: a bare family word names only itself unless the caller reads it as a family reference.
    assert not pa.product_matches("Windows", "Microsoft Windows 11 Enterprise")
    assert pa.product_matches("Windows", "Microsoft Windows 11 Enterprise", family_exact=False)
    assert pa.product_matches("windows_11", "cpe:2.3:o:microsoft:windows_11:22h2:*:*:*:*:*:*:*")
    assert not pa.product_matches("win", "Windows")
