# SPDX-License-Identifier: Apache-2.0
"""#2748: a product synced with an architecture, locale or version suffix is its product."""

from __future__ import annotations

import pytest

from app.services import ctem_case_versions as versions


@pytest.mark.parametrize(
    ("stored", "folded"),
    [
        ("Mozilla Thunderbird (x64 fr)", "mozilla thunderbird"),
        ("Mozilla Thunderbird (x64 en-US)", "mozilla thunderbird"),
        ("Mozilla Firefox (x64 en-GB)", "mozilla firefox"),
        ("Mozilla Firefox (x86 en-US)", "mozilla firefox"),
        ("7-Zip (arm64)", "7-zip"),
        ("Notepad++ (64-bit x64)", "notepad++"),
        ("Oracle VirtualBox 7.2.6", "oracle virtualbox"),
        ("Oracle VirtualBox 7.2.6 (x64)", "oracle virtualbox"),
        ("Git x64 2.47.1", "git"),
        ("Mozilla Thunderbird ESR (x64 fr)", "mozilla thunderbird esr"),
    ],
)
def test_trailing_qualifiers_are_dropped(stored: str, folded: str) -> None:
    assert versions.product_match_key(stored) == folded


@pytest.mark.parametrize(
    "name",
    [
        "Microsoft Windows 11",
        "Microsoft Office 2019",
        "Python 3",
        "SQL Server 2022",
        "Windows Defender",
        "Mozilla Thunderbird",
        "x64",
        "7.2.6",
        "Intel Wi-Fi",
        "Acme Tool (fr)",
        "Acme Tool (Community Edition)",
    ],
)
def test_a_name_without_trailing_qualifier_is_unchanged(name: str) -> None:
    assert versions.product_match_key(name) == versions._key(name)


def test_suffixed_rows_match_their_product_and_nothing_else() -> None:
    assert versions.matches_case("Mozilla Thunderbird", "Mozilla", "Mozilla Thunderbird (x64 fr)")
    assert versions.matches_case("Thunderbird", "Mozilla", "Mozilla Thunderbird (x64 en-US)")
    assert versions.matches_case("Mozilla Firefox", "Mozilla", "Mozilla Firefox (x64 en-GB)")
    assert versions.matches_case("Oracle VirtualBox", "Oracle Corporation", "Oracle VirtualBox 7.2.22")
    assert versions.matches_case("VirtualBox", "Oracle Corporation", "Oracle VirtualBox 7.0.20")
    # Distinct products never meet: an edition, another product, a word inside the name.
    assert not versions.matches_case("Mozilla Thunderbird", "Mozilla", "Mozilla Firefox (x64 fr)")
    assert not versions.matches_case("Mozilla Thunderbird", "Mozilla", "Mozilla Thunderbird ESR (x64 fr)")
    assert not versions.matches_case("Windows", "Microsoft", "Windows Defender")
    assert not versions.matches_case("Microsoft Windows", "Microsoft", "Microsoft Windows 11")
    # A label naming a version is never folded: it does not widen to every row of the product.
    assert not versions.matches_case("Oracle VirtualBox 7.2.6", "Oracle", "Oracle VirtualBox 7.0.20")


def test_the_database_filter_reads_the_suffixed_rows() -> None:
    from sqlalchemy.dialects import postgresql

    from app.models.organization_asset import OrganizationAssetSoftware

    keys = versions.product_keys("Mozilla Thunderbird")
    clause = versions.product_filter(OrganizationAssetSoftware.product_key, keys)
    compiled = clause.compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
    assert "mozilla thunderbird %" in compiled.params.values()


def test_running_versions_count_the_suffixed_rows() -> None:
    rows = [
        {"vendor": "Mozilla", "product": "Mozilla Thunderbird (x64 fr)", "version": "115.10.1", "asset_ids": ["a1"]},
        {"vendor": "Mozilla", "product": "Mozilla Thunderbird (x64 en-US)", "version": "115.16.0", "asset_ids": ["a2"]},
        {"vendor": "Mozilla", "product": "Mozilla Firefox (x64 fr)", "version": "100.0", "asset_ids": ["a3"]},
    ]
    answer = versions.running_versions(rows, product_label="Mozilla Thunderbird", fixed_version="115.16")
    assert answer is not None
    assert answer["asset_count"] == 2
    assert {v["version"]: v["status"] for v in answer["versions"]} == {"115.10.1": "below_fixed", "115.16.0": "fixed"}


def test_inventory_counts_and_covered_keys_follow_the_same_rule() -> None:
    rows = [
        ("Oracle Corporation", "Oracle VirtualBox 7.0.20", "7.0.20", ["w1"]),
        ("Oracle Corporation", "Oracle VirtualBox 7.2.22", "7.2.22", ["w2"]),
        ("Oracle", "VirtualBox", "7.2.6", ["m1"]),
        ("Oracle", "VirtualBox Extension Pack", "7.2.6", ["m2"]),
    ]
    counts = versions.inventory_version_counts(rows, {}, ["Oracle VirtualBox"])
    assert counts == {"Oracle VirtualBox": {"7.0.20": 1, "7.2.22": 1, "7.2.6": 1}}
    stored = ["oracle virtualbox 7.0.20", "virtualbox", "virtualbox extension pack", "mozilla thunderbird (x64 fr)"]
    assert versions.matching_product_keys(stored, ["Oracle VirtualBox"]) == ["oracle virtualbox 7.0.20", "virtualbox"]
    assert versions.matching_product_keys(stored, ["Mozilla Thunderbird"]) == ["mozilla thunderbird (x64 fr)"]
