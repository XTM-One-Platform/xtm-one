# SPDX-License-Identifier: Apache-2.0
"""#2450: a case whose vendor has several words finds the versions its inventory runs."""

from __future__ import annotations

import contextlib
import random
import uuid
from typing import Any

import pytest

from app.services import ctem_case_versions as versions
from app.services.builtin_tools.ctem_read import empty_sections

_SPLITS = [
    ("Node.js Foundation", "Node.js"),
    ("Oracle America", "Java SE"),
    ("The Apache Software Foundation", "Tomcat"),
    ("Mozilla Foundation", "Firefox"),
    ("Google LLC", "Chrome"),
    ("Microsoft Corporation", "Windows"),
    ("", "nginx"),
]


def _sql_rule(stored_key: str, keys: tuple[str, ...]) -> bool:
    """What :func:`product_filter` selects: an exact key, or the whole label behind a prefix."""
    return stored_key in keys or stored_key.endswith(" " + keys[0])


def test_the_keys_of_a_multiword_vendor_label_cover_the_bare_product() -> None:
    assert versions.product_keys("Node.js Foundation Node.js") == (
        "node.js foundation node.js",
        "foundation node.js",
        "node.js",
    )
    assert versions.product_keys("Microsoft Windows") == ("microsoft windows", "windows")
    assert versions.product_keys("nginx") == ("nginx",)
    assert versions.product_keys("  ") == ()


def test_the_database_filter_covers_every_row_matches_case_accepts() -> None:
    """The read is never narrower than the matching rule: for any split of a label, the row is read."""
    rng = random.Random(2450)  # noqa: S311 - seeded test data, not crypto
    words = ["node.js", "foundation", "the", "apache", "software", "oracle", "america", "google", "llc", "chrome"]
    pairs = list(_SPLITS)
    for _ in range(2000):
        vendor = " ".join(rng.choice(words) for _ in range(rng.randint(0, 3)))
        product = " ".join(rng.choice(words) for _ in range(rng.randint(1, 3)))
        pairs.append((vendor, product))
    for vendor, product in pairs:
        label = f"{vendor} {product}".strip()
        for candidate in {label, product, *versions.product_keys(label)}:
            if versions.matches_case(candidate, vendor, product):
                keys = versions.product_keys(candidate)
                assert _sql_rule(versions._key(product), keys), (candidate, vendor, product)


def test_the_bare_product_key_is_bound_in_the_read() -> None:
    from sqlalchemy.dialects import postgresql

    from app.models.organization_asset import OrganizationAssetSoftware

    keys = versions.product_keys("Node.js Foundation Node.js")
    clause = versions.product_filter(OrganizationAssetSoftware.product_key, keys)
    compiled = clause.compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
    bound = [v for value in compiled.params.values() for v in (value if isinstance(value, list | tuple) else [value])]
    assert "node.js" in bound


def test_matching_product_keys_covers_the_multiword_vendor_product() -> None:
    stored = ["node.js", "google llc google chrome", "chrome", "windows", "minio"]
    assert versions.matching_product_keys(stored, ["Node.js Foundation Node.js"]) == ["node.js"]
    assert versions.matching_product_keys(stored, ["Google Chrome"]) == ["chrome", "google llc google chrome"]


class _Db:
    def begin_nested(self) -> Any:
        return contextlib.AsyncExitStack()


@pytest.mark.asyncio
async def test_read_running_versions_finds_a_two_word_vendor_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """The issue's acceptance test: vendor ``Node.js Foundation``, product ``Node.js``, one asset."""
    stored = [("node.js foundation", "node.js", "20.11.0", "Node.js Foundation", "Node.js", ["a1"])]
    seen: list[tuple[str, ...]] = []

    async def _rows(_db: Any, _org: Any, keys: tuple[str, ...], _family: Any = None, _case: Any = None) -> Any:
        seen.append(keys)
        counted = [row for row in stored if _sql_rule(row[1], keys)]
        return counted, {(row[0], row[1], row[2]): {"intune"} for row in counted}

    async def _platforms(_db: Any, _org: Any, _keys: Any) -> dict[str, str | None]:
        return {"a1": "Windows"}

    monkeypatch.setattr(versions, "_read_rows", _rows)
    monkeypatch.setattr(versions, "_read_platforms", _platforms)
    answer = await versions.read_running_versions(
        _Db(),  # type: ignore[arg-type]
        uuid.uuid4(),
        product_label="Node.js Foundation Node.js",
        fixed_version="20.11.1",
    )
    assert seen and "node.js" in seen[0]
    assert answer is not None
    assert answer["asset_count"] == 1
    assert [(v["version"], v["asset_ids"]) for v in answer["versions"]] == [("20.11.0", ["a1"])]
    assert answer["assets_below_fixed"] == 1


def test_one_word_vendors_and_publisher_prefixes_still_match() -> None:
    """#2450 regression: Google Chrome, Microsoft Windows and a publisher-prefixed product (#1736)."""
    rows = [
        {"vendor": "Google LLC", "product": "Google LLC Google Chrome", "version": "131.0", "asset_ids": ["c1"]},
        {"vendor": "Google", "product": "Chrome", "version": "130.0", "asset_ids": ["c2"]},
    ]
    chrome = versions.running_versions(rows, product_label="Google Chrome", fixed_version=None)
    assert chrome is not None and chrome["asset_count"] == 2
    windows = versions.running_versions(
        [{"vendor": "Microsoft Corporation", "product": "Windows", "version": "10.0", "asset_ids": ["w1"]}],
        product_label="Microsoft Windows",
        fixed_version=None,
    )
    assert windows is not None and windows["asset_count"] == 1
    assert _sql_rule("google llc google chrome", versions.product_keys("Google Chrome"))
    assert _sql_rule("windows", versions.product_keys("Microsoft Windows"))
    # The broader keys never widen the rule: a suffix alone is not the case's product.
    assert not versions.matches_case("Node.js Foundation Node.js", "Joyent", "Node.js")
    assert not versions.matches_case("Node.js Foundation Node.js", "", "Foundation Node.js")


def test_a_language_package_row_stays_out_of_a_product_case() -> None:
    """#2223 is unchanged: the package condition still applies to the wider key set."""
    from sqlalchemy.dialects import postgresql

    from app.models.organization_asset import OrganizationAssetSoftware

    case_rows = versions.package_case_rows(OrganizationAssetSoftware, uuid.uuid4())
    compiled = case_rows[0].compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
    assert "purl" in str(compiled) and "ctem_signals" in str(compiled)
    assert "case_fix" in compiled.params.values()


def test_an_unmatched_software_row_says_the_match_failed() -> None:
    rendered = {"remediation_kind": "upgrade"}
    reason = empty_sections(rendered, None, ["github", "intune"], 12)["running_versions"]
    assert "could not be matched" in reason and "12 asset(s)" in reason
    assert "no asset of the inventory runs this product" not in reason
    assert "inventory sources checked" in empty_sections(rendered, None, ["intune"], 0)["running_versions"]


@pytest.mark.asyncio
async def test_the_software_count_never_breaks_a_case_read() -> None:
    class _Broken:
        def begin_nested(self) -> Any:
            raise RuntimeError("no store")

    assert await versions.software_asset_count(_Broken(), uuid.uuid4(), "Node.js Foundation Node.js") == 0  # type: ignore[arg-type]
    assert await versions.software_asset_count(_Broken(), uuid.uuid4(), "") == 0  # type: ignore[arg-type]
