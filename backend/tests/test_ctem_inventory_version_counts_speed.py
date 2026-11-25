# SPDX-License-Identifier: Apache-2.0
"""inventory_version_counts: same results as matches_case, linear on a large inventory."""

from __future__ import annotations

import random
import time

from app.services import ctem_case_versions as cv


def _brute(rows, labels):
    out = {}
    for label in dict.fromkeys(labels):
        on = {}
        for vendor, product, version, ids in rows:
            if not cv.matches_case(label, vendor, product):
                continue
            text = str(version or "").strip()
            if text and text != cv.UNKNOWN:
                on.setdefault(text, set()).update(str(a) for a in ids)
        if on:
            out[label] = {v: len(i) for v, i in on.items()}
    return out


def test_same_results_as_matches_case():
    rng = random.Random(1)  # noqa: S311 - seeded test data, not crypto
    vendors = ["Google LLC", "Microsoft Corporation", "Mozilla", "Apple Inc.", "", "Oracle, Inc", "google"]
    products = [
        "Chrome", "Google Chrome", "Google LLC Google Chrome", "Windows", "Microsoft Windows",
        "Firefox", "Mozilla Firefox", "VirtualBox", "Oracle VirtualBox", "chrome", "Microsoft Edge WebView2",
    ]
    rows = [
        (rng.choice(vendors), rng.choice(products), rng.choice(["1.0", "2.0", "unknown", ""]), [str(rng.randint(1, 40))])
        for _ in range(2000)
    ]
    labels = ["Google Chrome", "Firefox", "Mozilla Firefox", "Chrome", "Oracle VirtualBox", "Microsoft Edge", "google chrome"]
    assert cv.inventory_version_counts(rows, {}, labels) == _brute(rows, labels)


def test_linear_on_a_large_inventory():
    rows = [(f"Vendor{i % 500} Inc", f"Product {i % 3000}", f"{i % 40}.0", [str(i)]) for i in range(20000)]
    labels = [f"Vendor{i} Product {i}" for i in range(300)]
    started = time.perf_counter()
    cv.inventory_version_counts(rows, {}, labels)
    assert time.perf_counter() - started < 1.0
