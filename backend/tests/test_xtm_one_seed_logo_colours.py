"""The XTM One marks the brand packs seed draw the Figma lockup, never #9C27B0.

Source of truth: Figma XTM_Hub frame 6685:1130 and filigran-design-system
docs/public/product-logos/xtmone.svg (node 2624:11345), the colours the
frontend pins in frontend/src/xtm-one-logo-colours.test.ts: dark emblem
#A47AF0 with a #F2F2F3 wordmark, light emblem #651FE5 with a #18191B wordmark.
A changed seed file is refreshed in place on the next boot (same asset id, new
bytes: template_asset_seed), so the deployed brand templates follow this file.
"""

import re
from pathlib import Path

import pytest

BRAND = Path(__file__).resolve().parents[1] / "app" / "agents" / "seed_data" / "brand_assets"

DARK = ("#A47AF0", "#F2F2F3")
LIGHT = ("#651FE5", "#18191B")


def _fills(path: Path) -> list[str]:
    return [fill.upper() for fill in re.findall(r'<path d="[^"]*"[^>]*\bfill="([^"]*)"', path.read_text())]


@pytest.mark.parametrize(
    ("relative", "emblem", "wordmark"),
    [
        ("xtm-one/xtm-one-logo.svg", *DARK),
        ("xtm-one/xtm-one-mark.svg", DARK[0], None),
        ("filigran/xtm-one-logo-light.svg", *LIGHT),
        ("filigran/xtm-one-mark-light.svg", LIGHT[0], None),
    ],
)
def test_the_seeded_xtm_one_marks_draw_the_figma_lockup(relative, emblem, wordmark):
    fills = _fills(BRAND / relative)
    assert fills[:4] == [emblem] * 4
    assert set(fills[4:]) == ({wordmark} if wordmark else set())


def test_no_seeded_xtm_one_logo_or_mark_draws_the_retired_purple():
    for path in BRAND.glob("*/xtm-one-*.svg"):
        if re.fullmatch(r"xtm-one-(logo|mark)(-light)?\.svg", path.name):
            assert "9C27B0" not in path.read_text().upper(), path
