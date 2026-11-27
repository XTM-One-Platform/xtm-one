# SPDX-License-Identifier: Apache-2.0

"""#2679: a generic CVE product name never matches a url-like inventory row nor marks it verified.

A CVE whose software list held ``SharePoint`` beside ``SharePoint Server 2019`` matched a
web-app shortcut row (``<tenant>-my.sharepoint.com-<id>``, a placeholder version, no CPE),
recorded ``version_verified: true`` and opened a P1 case. A URL or host name label names
only itself, and a generic row that a more specific row of the same list names is a
family match, never version-verified.
"""

from __future__ import annotations

from datetime import date

from app.services import ctem_primary_assets as pa
from app.services import ctem_scope_gate as gate
from app.services import organization_context as oc

TODAY = date(2026, 11, 27)
WORKSTATIONS = "Employee workstations"
SHORTCUT = "filigranio-my.sharepoint.com-C9CA3EEB"


def _profile() -> oc.OrganizationProfile:
    return oc.parse_profile(
        {
            "primary_assets": [{"name": WORKSTATIONS, "criticality": "critical"}],
            "prioritization_policy": {"min_primary_asset_criticality": "medium"},
        }
    )


def _device(name: str, label: str) -> pa.Reacher:
    reacher = pa.Reacher(
        name=name,
        keys={name.lower()},
        products={label.lower(): label, "windows": "Windows"},
        os_family="Windows",
        versioned={label},
    )
    reacher.links.append((pa.Link(pa.primary_asset_key(WORKSTATIONS), "rule", ("used_by",)), "asset_group", "All"))
    return reacher


def _cve(*software: str) -> gate.SignalFacts:
    return gate.SignalFacts(
        trigger_path="new_vulnerability",
        lens="world",
        subject_kind="vulnerability",
        subject_ref="CVE-2026-99002",
        source_provider="opencti",
        evidence={"software": list(software), "cvss": 9.8},
    )


def test_a_url_like_label_names_only_itself() -> None:
    assert pa.is_url_like(SHORTCUT)
    assert pa.is_url_like("https://teams.microsoft.com")
    assert pa.is_url_like("www.example.org")
    for product in ("SharePoint", "node.js", "ASP.NET Core", ".NET Framework 4.8.1", "socket.io", "7-zip"):
        assert not pa.is_url_like(product), product
    assert not pa.is_url_like("cpe:2.3:a:microsoft:sharepoint_server:2019:*:*:*:*:*:*:*")
    assert not pa.product_matches("SharePoint", SHORTCUT)
    assert not pa.product_matches(SHORTCUT, "SharePoint")
    assert not gate._product_matches("SharePoint", SHORTCUT)
    assert pa.product_matches(SHORTCUT, SHORTCUT)
    assert pa.product_matches("SharePoint Server 2019", "Microsoft SharePoint Server 2019")


def test_a_generic_sharepoint_row_never_reaches_a_web_shortcut_nor_opens_a_case() -> None:
    index = pa.PrimaryAssetIndex(loaded=True, reachers=[_device("LAPTOP-01", SHORTCUT)])
    assert index.reach(["SharePoint Server 2019", "SharePoint"]) == []
    decision = gate.decide(_profile(), _cve("SharePoint Server 2019", "SharePoint"), primary_assets=index, today=TODAY)
    assert decision.record["reconcile"]["primary_assets"] == []
    assert decision.outcome != "case"


def test_a_generic_row_beside_a_specific_one_is_never_verified() -> None:
    assert pa.generic_refs(["SharePoint Server 2019", "SharePoint"]) == frozenset({"SharePoint"})
    assert pa.generic_refs(["SharePoint Server 2019"]) == frozenset()
    index = pa.PrimaryAssetIndex(loaded=True, reachers=[_device("SRV-01", "SharePoint Foundation 2013")])
    hits = index.reach(["SharePoint Server 2019", "SharePoint"])
    assert hits and all(h["version_verified"] is False for h in hits)
    decision = gate.decide(_profile(), _cve("SharePoint Server 2019", "SharePoint"), primary_assets=index, today=TODAY)
    assert decision.outcome == "to_validate"
    assert decision.record["reason_code"] == "version_not_verified"


def test_an_installed_sharepoint_server_in_the_list_still_opens_a_case() -> None:
    index = pa.PrimaryAssetIndex(loaded=True, reachers=[_device("SRV-02", "Microsoft SharePoint Server 2019")])
    decision = gate.decide(_profile(), _cve("SharePoint Server 2019", "SharePoint"), primary_assets=index, today=TODAY)
    assert decision.record["reconcile"]["primary_assets"][0]["version_verified"] is True
    assert decision.outcome == "case"
