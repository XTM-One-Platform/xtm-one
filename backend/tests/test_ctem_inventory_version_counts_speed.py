"""The device inventory counts stay linear and off the event loop (#2072).

``inventory_version_counts`` ran ``matches_case`` for every label against every row, which
blocked the web event loop for a minute on a large inventory. It now tests a row against the
labels it can match only; the answer is the same as the label-by-row rule.
"""

from __future__ import annotations

import random
import time
import uuid
from typing import Any

import pytest

from app.services import ctem_case_versions as case_versions

VENDORS = ["Google LLC", "Google", "Microsoft Corporation", "Mozilla", "", "Adobe Inc."]
PRODUCTS = [
    "Chrome",
    "Google Chrome",
    "Google LLC Google Chrome",
    "Windows",
    "Microsoft Windows",
    "Firefox",
    "Mozilla Firefox ESR",
    "Acrobat Reader",
    "Adobe Acrobat Reader",
    "",
]
LABELS = ["Google Chrome", "Microsoft Windows", "Mozilla Firefox", "Acrobat Reader", "Windows", "Chrome"]


def _reference(rows: list[Any], labels: list[str]) -> dict[str, dict[str, int]]:
    """The label-by-row rule the function replaced, kept as the oracle."""
    out: dict[str, dict[str, int]] = {}
    for label in dict.fromkeys(labels):
        on_version: dict[str, set[str]] = {}
        for vendor, product, version, asset_ids in rows:
            if not case_versions.matches_case(label, vendor, product):
                continue
            text = str(version or "").strip()
            if text and text != case_versions.UNKNOWN:
                on_version.setdefault(text, set()).update(str(a) for a in asset_ids)
        if on_version:
            out[label] = {v: len(ids) for v, ids in on_version.items()}
    return out


def test_the_indexed_counts_equal_the_label_by_row_rule() -> None:
    rng = random.Random(2069)  # noqa: S311 - a reproducible test sample
    rows = [
        (
            rng.choice(VENDORS),
            rng.choice(PRODUCTS),
            rng.choice(["1.0", "2.0", "unknown", ""]),
            [f"a{rng.randint(0, 40)}"],
        )
        for _ in range(2000)
    ]
    assert case_versions.inventory_version_counts(rows, {}, LABELS) == _reference(rows, LABELS)


def test_300_labels_over_20000_rows_count_under_a_second() -> None:
    labels = [f"Vendor{i} Product{i}" for i in range(300)]
    rows = [
        (f"Vendor{i % 400} Inc.", f"Product{i % 400} Suite {i % 7}", f"{i % 50}.0", [f"a{i}"]) for i in range(20000)
    ]
    rows += [(f"Vendor{i}", f"Product{i}", "1.0", [f"b{i}"]) for i in range(300)]
    started = time.perf_counter()
    counts = case_versions.inventory_version_counts(rows, {}, labels)
    assert time.perf_counter() - started < 1.0
    assert counts["Vendor7 Product7"] == {"1.0": 1}


@pytest.mark.asyncio
async def test_a_cached_answer_skips_the_inventory_read() -> None:
    org = uuid.uuid4()
    case_versions._INVENTORY_CACHE[(org, ("Google Chrome",))] = (time.monotonic(), {"Google Chrome": {"1.0": 2}})

    class _NoDb:
        def begin_nested(self) -> Any:
            raise AssertionError("the cached answer must not read the store")

    try:
        answer = await case_versions.inventory_versions(_NoDb(), org, ["Google Chrome"])  # type: ignore[arg-type]
    finally:
        case_versions._INVENTORY_CACHE.pop((org, ("Google Chrome",)), None)
    assert answer == {"Google Chrome": {"1.0": 2}}
