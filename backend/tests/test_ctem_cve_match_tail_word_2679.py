# SPDX-License-Identifier: Apache-2.0

"""#2679 reopened: a vendor-qualified product never matches on its generic last word.

Retest on a rolling CTEM build after a full signal reset: a CVE on Panabit Smart Application
Gateway reached JetBrains Gateway, a ``Gateway`` 2026.2.1 app (version verified) and the npm
package ``@apollo/server-gateway-interface``.
"""

from __future__ import annotations

import pytest

from app.services import ctem_primary_assets as pa
from app.services import ctem_scope_gate as gate

WORKSTATIONS = "Employee workstations"
PANABIT = "Panabit Smart Application Gateway"


@pytest.fixture(autouse=True)
def _no_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pa, "_platform_of_ref", lambda _ref: None)


def _reacher(name: str, label: str, version: str = "", *, repository: bool = False) -> pa.Reacher:
    reacher = pa.Reacher(
        name=name,
        keys={name.lower()},
        products={label.lower(): label},
        versioned={label} if version else set(),
        versions={label: {version}} if version else {},
        repository=repository,
    )
    reacher.links.append((pa.Link(pa.primary_asset_key(WORKSTATIONS), "rule", ("used_by",)), "asset", name))
    return reacher


@pytest.mark.parametrize(
    ("label", "version", "repository"),
    [
        ("JetBrains Gateway", "2026.2", False),
        ("Gateway", "2026.2.1", False),
        ("@apollo/server-gateway-interface", "2.2.0", True),
    ],
)
def test_a_vendor_qualified_product_reaches_nothing_by_its_last_word(
    label: str, version: str, repository: bool
) -> None:
    index = pa.PrimaryAssetIndex(loaded=True, reachers=[_reacher("ASSET", label, version, repository=repository)])
    assert index.reach([PANABIT, "Gateway"]) == []
    assert index.reach([PANABIT]) == []
    assert not pa.product_matches("Gateway", PANABIT)
    assert not gate._product_matches("Gateway", PANABIT)


def test_the_generic_last_word_beside_its_full_name_is_set_aside() -> None:
    assert pa.tail_refs([PANABIT, "Gateway"]) == frozenset({"Gateway"})
    assert pa.tail_refs(["Gateway"]) == frozenset()
    assert pa.tail_word_only("Gateway", "JetBrains Gateway")
    assert not pa.tail_word_only("Nginx", "Nginx Plus")


def test_the_product_itself_still_reaches() -> None:
    index = pa.PrimaryAssetIndex(loaded=True, reachers=[_reacher("FW-01", PANABIT, "3.0")])
    assert [h["asset"] for h in index.reach([PANABIT, "Gateway"])] == ["FW-01"]
