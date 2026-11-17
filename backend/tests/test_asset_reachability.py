"""The reachability rule of the Exposed layer (``app.services.asset_reachability``).

A vulnerability drops only when every asset it sits on is proven unreachable;
a user endpoint is always reachable whatever ``internet_facing`` says;
``unknown`` stays in; an asset group marked internet-facing makes its assets
reachable.
"""

from __future__ import annotations

import pytest

from app.services.asset_reachability import (
    REACHABLE,
    REASON_ENDPOINT,
    REASON_GROUP,
    REASON_INTERNET_FACING,
    REASON_UNKNOWN,
    REASON_UNREACHABLE,
    UNKNOWN,
    UNREACHABLE,
    asset_reachability,
    exposed_count,
    is_user_endpoint,
    reachability_split,
    unreachable_keys,
)
from app.services.organization_context import ASSET_TAXONOMY, AssetEntry, AssetGroupEntry


def _asset(name: str, category: str, subcategory: str = "", internet_facing: str = "no", **extra) -> AssetEntry:
    return AssetEntry(name=name, category=category, subcategory=subcategory, internet_facing=internet_facing, **extra)


@pytest.mark.parametrize(
    "category,subcategory",
    [
        ("host", "laptop"),
        ("host", "workstation"),
        ("host", "thin_client"),
        ("mobile_device", "smartphone"),
        ("mobile_device", "tablet"),
        ("mobile_device", ""),
    ],
)
def test_a_user_endpoint_saved_not_internet_facing_is_reachable(category, subcategory):
    assert asset_reachability(_asset("a", category, subcategory, "no")) == (REACHABLE, REASON_ENDPOINT)


def test_a_legacy_endpoint_row_is_a_reachable_workstation():
    entry = AssetEntry.model_validate({"name": "pc-1", "type": "endpoint", "internet_facing": "no"})
    assert (entry.category, entry.subcategory) == ("host", "workstation")
    assert asset_reachability(entry) == (REACHABLE, REASON_ENDPOINT)
    # A raw dict not upgraded yet reads the same.
    assert asset_reachability({"name": "pc-2", "type": "endpoint", "internet_facing": "no"}) == (
        REACHABLE,
        REASON_ENDPOINT,
    )


def test_a_server_saved_not_internet_facing_is_proven_unreachable():
    assert asset_reachability(_asset("db", "host", "server", "no")) == (UNREACHABLE, REASON_UNREACHABLE)


def test_a_server_of_unknown_exposure_is_kept():
    assert asset_reachability(_asset("db", "host", "server", "unknown")) == (UNKNOWN, REASON_UNKNOWN)


def test_an_internet_facing_server_is_reachable():
    assert asset_reachability(_asset("web", "host", "server", "yes")) == (REACHABLE, REASON_INTERNET_FACING)


def test_a_server_in_an_internet_facing_group_is_reachable():
    groups = [AssetGroupEntry(name="DMZ", internet_facing="yes"), AssetGroupEntry(name="Core", internet_facing="no")]
    assert asset_reachability(_asset("db", "host", "server", "no", group="dmz"), groups) == (REACHABLE, REASON_GROUP)
    assert asset_reachability(_asset("db", "host", "server", "no", group="Core"), groups) == (
        UNREACHABLE,
        REASON_UNREACHABLE,
    )


@pytest.mark.parametrize("category", [c for c in ASSET_TAXONOMY if c != "mobile_device"])
def test_no_other_category_is_an_endpoint_without_its_subcategory(category):
    assert not is_user_endpoint(_asset("x", category, "", "no"))
    assert asset_reachability(_asset("x", category, "", "no")) == (UNREACHABLE, REASON_UNREACHABLE)


@pytest.mark.parametrize(
    "subcategory", [s for s in ASSET_TAXONOMY["host"] if s not in ("workstation", "laptop", "thin_client")]
)
def test_the_other_host_subcategories_are_not_endpoints(subcategory):
    assert not is_user_endpoint(_asset("x", "host", subcategory, "no"))


def test_the_split_counts_every_verdict_and_the_endpoints():
    assets = [
        _asset("laptop", "host", "laptop", "no"),
        _asset("srv-no", "host", "server", "no"),
        _asset("srv-unknown", "host", "server", "unknown"),
        _asset("web", "web_application", "website", "yes"),
    ]
    assert reachability_split(assets) == {"total": 4, REACHABLE: 2, UNREACHABLE: 1, UNKNOWN: 1, "endpoints": 1}
    assert reachability_split([]) == {"total": 0, REACHABLE: 0, UNREACHABLE: 0, UNKNOWN: 0, "endpoints": 0}


def _profile():
    return [
        _asset("laptop-1", "host", "laptop", "no", source="openaev", external_id="a1"),
        _asset("srv-1", "host", "server", "no", source="openaev", external_id="a2"),
        _asset("srv-2", "host", "server", "unknown", source="openaev", external_id="a3"),
    ]


def test_only_the_findings_on_proven_unreachable_assets_drop():
    findings = [
        [{"source": "openaev", "external_id": "a1"}],  # laptop: kept
        [{"source": "openaev", "external_id": "a2"}],  # server no: dropped
        [{"source": "openaev", "external_id": "a3"}],  # server unknown: kept
        [{"source": "openaev", "external_id": "a2"}, {"name": "laptop-1"}],  # also on the laptop: kept
        [{"name": "SRV-1"}],  # matched by name: dropped
        [{"source": "openaev", "external_id": "zz"}],  # not in the profile: kept
        [],  # no asset: kept
    ]
    assert exposed_count(findings, _profile()) == 5


def test_laptops_only_drop_nothing():
    laptops = [_asset(f"l{i}", "host", "laptop", "no", source="openaev", external_id=f"l{i}") for i in range(3)]
    findings = [[{"source": "openaev", "external_id": f"l{i}"}] for i in range(3)]
    assert exposed_count(findings, laptops) == 3


def test_unreachable_keys_name_the_vulnerabilities_exposed_drops():
    findings = {
        "CVE-B": [{"source": "openaev", "external_id": "a2"}],  # server no: dropped
        "CVE-A": [{"name": "SRV-1"}],  # matched by name: dropped
        "CVE-C": [{"source": "openaev", "external_id": "a2"}, {"source": "openaev", "external_id": "a1"}],
        "CVE-D": [],  # no asset: kept
        "CVE-E": [{"source": "openaev", "external_id": "zz"}],  # not in the profile: kept
    }
    assert unreachable_keys(findings, _profile()) == ["CVE-A", "CVE-B"]
