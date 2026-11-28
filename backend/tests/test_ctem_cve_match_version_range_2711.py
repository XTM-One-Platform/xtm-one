# SPDX-License-Identifier: Apache-2.0

"""#2711 reopened: a hop is version verified only on the version or range the reference names.

Retest on a rolling CTEM build after a full signal reset: a CVE naming
``hermes-agent >= 0.21.6, < 0.23.2`` reached two Macs running ``Hermes 0.0.1`` with
``version_verified: true``, the compound npm package ``hermes-estree`` reached two
repositories, and a CVE naming ``FFmpeg 8.0`` was verified on FFmpeg 9.0 and 9.0.2.
"""

from __future__ import annotations

import pytest

from app.services import ctem_primary_assets as pa

WORKSTATIONS = "Employee workstations"
HERMES = "hermes-agent >= 0.21.6, < 0.23.2"


@pytest.fixture(autouse=True)
def _no_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pa, "_platform_of_ref", lambda _ref: None)


def _reacher(name: str, label: str, *versions: str, os_family: str = "", repository: bool = False) -> pa.Reacher:
    reacher = pa.Reacher(
        name=name,
        keys={name.lower()},
        products={label.lower(): label},
        os_family=os_family,
        versioned={label} if versions else set(),
        versions={label: set(versions)} if versions else {},
        repository=repository,
    )
    reacher.links.append((pa.Link(pa.primary_asset_key(WORKSTATIONS), "rule", ("used_by",)), "asset", name))
    return reacher


def _reach(software: list[str], *reachers: pa.Reacher) -> list[dict]:
    return pa.PrimaryAssetIndex(loaded=True, reachers=list(reachers)).reach(software)


def test_a_range_reference_names_its_package_not_its_first_word() -> None:
    assert pa.reference_versions(HERMES) == ((">=", "0.21.6"), ("<", "0.23.2"))
    assert not pa.product_matches("Hermes", HERMES)
    assert _reach([HERMES], _reacher("MAC-01", "Hermes", "0.0.1", os_family="macOS")) == []


def test_a_compound_inventory_label_is_never_reduced_to_one_of_its_words() -> None:
    # The reverse direction of a match: the reference is the needle, the inventory label the haystack.
    assert not pa.product_matches("Hermes", "hermes-estree", family_exact=False, ref_exact=False)
    assert not pa.product_matches("Gateway", "@apollo/server-gateway-interface", ref_exact=False)
    assert not pa.product_matches("hermes-estree", "Hermes")
    for repo in ("FiligranHQ/website", "FiligranHQ/xtm-browser-extension"):
        assert _reach(["Hermes 0.22"], _reacher(repo, "hermes-estree", "0.22.0", repository=True)) == []


def test_a_distribution_package_of_the_product_keeps_the_whole_word_rule() -> None:
    assert pa.product_matches("OpenSSL", "openssl-libs", family_exact=False, ref_exact=False)
    assert pa.product_matches("requests", "python3-requests", family_exact=False, ref_exact=False)


@pytest.mark.parametrize("label", ["python-dateutil", "node-fetch", "ruby-json", "php-mbstring", "rust-openssl"])
def test_an_ecosystem_prefix_never_names_a_package_by_itself(label: str) -> None:
    assert not pa.product_matches(label.split("-")[0].title(), label, family_exact=False, ref_exact=False)
    assert pa.product_matches(label.split("-", 1)[1], label, family_exact=False, ref_exact=False)


def test_a_packaging_suffix_never_names_a_package_by_itself() -> None:
    assert not pa.product_matches("libs", "openssl-libs", family_exact=False, ref_exact=False)


def test_the_package_in_range_is_verified_and_one_outside_is_not() -> None:
    inside = _reach([HERMES], _reacher("MAC-02", "hermes-agent", "0.22.1", os_family="macOS"))
    assert [h["version_verified"] for h in inside] == [True]
    outside = _reach([HERMES], _reacher("MAC-03", "hermes-agent", "0.0.1", os_family="macOS"))
    assert [h["version_verified"] for h in outside] == [False]


@pytest.mark.parametrize(("installed", "verified"), [("9.0", False), ("9.0.2", False), ("8.0", True), ("8.0.2", True)])
def test_an_exact_version_reference_verifies_only_that_release_line(installed: str, verified: bool) -> None:
    hits = _reach(["FFmpeg 8.0"], _reacher("LAPTOP", "FFmpeg", installed, os_family="macOS"))
    assert [h["version_verified"] for h in hits] == [verified]


@pytest.mark.parametrize("installed", ["v8.0.2", "7:8.0.1-0ubuntu0.22.04.1"])
def test_an_exact_version_reads_the_epoch_and_v_prefix(installed: str) -> None:
    assert pa.version_in_reference("FFmpeg", {installed}, "FFmpeg 8.0")
    assert not pa.version_in_reference("FFmpeg", {installed}, "FFmpeg 9.0")


def test_a_version_span_reference_verifies_only_inside_the_span() -> None:
    assert pa.reference_versions("mail-mcp-bridge 1.3.0-1.3.3") == ((">=", "1.3.0"), ("<=", "1.3.3"))
    assert pa.version_in_reference("mail-mcp-bridge", {"1.3.2"}, "mail-mcp-bridge 1.3.0-1.3.3")
    assert not pa.version_in_reference("mail-mcp-bridge", {"1.4.0"}, "mail-mcp-bridge 1.3.0-1.3.3")


def test_a_reference_without_version_and_a_release_in_the_label_stay_verified() -> None:
    hits = _reach(["nginx_plus"], _reacher("WEB-01", "Nginx", "1.25.3", os_family="Linux"))
    assert [h["version_verified"] for h in hits] == [True]
    assert pa.version_in_reference("Microsoft SharePoint Server 2019", {"16.0.10337"}, "SharePoint Server 2019")


def test_a_cpe_version_is_the_version_compared() -> None:
    cpe = "cpe:2.3:a:ffmpeg:ffmpeg:8.0:*:*:*:*:*:*:*"
    assert pa.reference_versions(cpe) == (("=", "8.0"),)
    assert not pa.version_in_reference("FFmpeg", {"9.0.2"}, cpe)
    assert pa.reference_versions("cpe:2.3:a:ffmpeg:ffmpeg:*:*:*:*:*:*:*:*") is None
