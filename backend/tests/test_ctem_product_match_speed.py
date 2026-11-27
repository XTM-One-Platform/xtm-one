# SPDX-License-Identifier: Apache-2.0

"""#2032: CTEM product matching stays cheap on a real inventory.

``funnel_scope`` checked every product of every supporting asset against every
stack entry with a regex compiled per pair; on a production inventory it
blocked the web event loop for minutes and the liveness probe restarted the
pods. The match is now a ``str.find`` whole-word check and a token index.
"""

from __future__ import annotations

import random
import re
import time
from datetime import date
from types import SimpleNamespace
from typing import Any

from app.services import ctem_primary_assets as pa
from app.services.ctem_funnel import funnel_scope
from app.services.ctem_scope_gate import _product_matches as gate_product_matches


def _former_rule(product: str, ref: str, *, family_exact: bool = True, ref_exact: bool = True) -> bool:
    """The regex rule of ``product_matches`` before #2032, the reference, with the #2628, #2685 and #2711 guards."""
    p, r = str(product or "").strip().lower(), str(ref or "").strip().lower()
    if not p or not r:
        return False
    named = pa.reference_names(product, ref) if ref_exact else None
    if named is not None:
        return named
    if r.startswith("cpe:"):
        parts = r.split(":")
        r = parts[4] if len(parts) > 4 else r
    r = r.replace("_", " ")
    p = p.replace("_", " ")
    if p in pa.GENERIC_PRODUCT_WORDS and p != r:
        return False
    if family_exact and p in pa.FAMILY_WORDS and p != r:
        return False
    if p != r and (pa.is_url_like(p) or pa.is_url_like(r)):
        return False
    return p == r or re.search(rf"(^|[^a-z0-9]){re.escape(p)}($|[^a-z0-9])", r) is not None


_SAMPLES = [
    "Windows",
    "windows_11",
    "Microsoft Windows 11 Enterprise",
    "cpe:2.3:o:microsoft:windows_11:22h2:*:*:*:*:*:*:*",
    "win",
    "Win",
    ".NET",
    ".net framework 4.8",
    "c++",
    "Visual C++ 2019 Redistributable",
    "node",
    "node.js",
    "Node.js 20",
    "openssl",
    "OpenSSL 3.0.13",
    "libssl3",
    "ssl",
    "Apache HTTP Server",
    "apache",
    "http",
    "  ",
    "",
    "Zoom",
    "zoom-client",
    "Café",
    "café pro",
    "7-zip",
    "7zip",
    "++",
    "cpe:/a:apache:http_server:2.4",
    "cpe:2.3:a:apache:http_server:2.4.58:*:*:*:*:*:*:*",
    "SharePoint",
    "filigranio-my.sharepoint.com-C9CA3EEB",
    "https://teams.microsoft.com",
]


def test_product_matches_keeps_the_former_rule() -> None:
    for p in _SAMPLES:
        for r in _SAMPLES:
            assert pa.product_matches(p, r) == _former_rule(p, r), (p, r)
            named = pa.reference_names(p, r) if p.strip() and r.strip() else None
            gate_expected = named if named is not None else (bool(p.strip()) and bool(r.strip()) and _gate_former(p, r))
            assert gate_product_matches(p, r) == gate_expected, (p, r)


def _gate_former(product: str, ref: str) -> bool:
    p, r = product.strip().lower(), ref.strip().lower()
    if r.startswith("cpe:"):
        parts = r.split(":")
        r = parts[4] if len(parts) > 4 else r
    if (p in pa.FAMILY_WORDS or p in pa.GENERIC_PRODUCT_WORDS) and p != r.replace("_", " "):
        return False
    if p != r and (pa.is_url_like(p) or pa.is_url_like(r)):
        return False
    return p == r or re.search(rf"(^|[^a-z0-9]){re.escape(p)}($|[^a-z0-9])", r) is not None


def test_product_index_answers_what_a_full_scan_answers() -> None:
    index = pa.ProductIndex(_SAMPLES)
    for ref in _SAMPLES:
        expected = [
            v for v in index.values if _former_rule(v, ref) or _former_rule(ref, v, family_exact=False, ref_exact=False)
        ]
        assert index.matching(ref) == expected, ref


def _inventory(seed: int, products: int, stack: int) -> tuple[list[pa.Reacher], list[dict[str, Any]]]:
    rng = random.Random(seed)  # noqa: S311 - a reproducible test inventory, no secret
    words = [f"w{i}" for i in range(600)] + ["windows", "server", "client", "java", "agent", "sql", "runtime"]

    def name() -> str:
        return " ".join(rng.choice(words) for _ in range(rng.randint(1, 4)))

    reachers = [
        pa.Reacher(
            name=f"host-{i}",
            products={f"k{i}-{j}": name() for j in range(rng.randint(5, 30))},
            links=[(pa.Link(primary_asset_key="crown", source="manual"), "asset", f"host-{i}")],
        )
        for i in range(products)
    ]
    entries = [{"label": f"label-{i}", "product": name()} for i in range(stack)]
    return reachers, entries


def test_labels_match_a_full_scan_on_a_random_inventory() -> None:
    reachers, stack = _inventory(7, 40, 400)
    index = pa.PrimaryAssetIndex(loaded=True, reachers=reachers)
    scope = funnel_scope(profile=SimpleNamespace(primary_assets=[]), index=index, stack=stack, today=date(2026, 11, 25))
    products = {p for r in reachers for p in r.products.values()}
    expected = [
        e["label"]
        for e in stack
        if any(_former_rule(p, e["product"]) or _former_rule(e["product"], p, ref_exact=False) for p in products)
    ]
    assert scope["business"]["labels"] == (expected or None)
    for e in stack[:50]:
        running = index._running(e["product"])
        assert running == frozenset(
            i
            for i, r in enumerate(reachers)
            if any(
                _former_rule(v, e["product"]) or _former_rule(e["product"], v, ref_exact=False)
                for v in r.products.values()
            )
        )


def test_funnel_scope_on_a_large_inventory_stays_within_budget() -> None:
    # Hundreds of supporting assets (thousands of installed products) against
    # thousands of stack rows: the former scan compiled tens of millions of patterns.
    reachers, stack = _inventory(11, 400, 5000)
    index = pa.PrimaryAssetIndex(loaded=True, reachers=reachers)
    started = time.perf_counter()
    scope = funnel_scope(profile=SimpleNamespace(primary_assets=[]), index=index, stack=stack, today=date(2026, 11, 25))
    elapsed = time.perf_counter() - started
    assert scope["business"]["configured"] is True
    assert elapsed < 5.0, f"funnel_scope took {elapsed:.2f}s"
