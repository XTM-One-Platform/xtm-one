"""Small kit assets: the product mark tile of the platform table (one file per
product, brand blue tile with the white emblem, readable on both themes) and the
Filigran footer lockup pair (emblem + outlined wordmark, light and dark)."""
import os
import re

import textpath as tp
from banner import _mark  # white emblem

BRAND = os.path.join(os.path.dirname(__file__), "..", "brand")


def mark_tile(product: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 48 48" role="img" '
            f'aria-label="{product}"><rect width="48" height="48" rx="10" fill="#001BDA"/>'
            f'<svg x="8" y="8" width="32" height="32" viewBox="0 0 24 24" fill="none">{_mark(product)}</svg></svg>')


def filigran(theme: dict) -> str:
    fg = theme["fg"]
    s = open(os.path.join(BRAND, "filigran-mark.svg")).read()
    inner = re.sub(r"^.*?<svg[^>]*>", "", s, flags=re.S)
    inner = re.sub(r"</svg>\s*$", "", inner, flags=re.S)
    inner = re.sub(r'fill="(?!none)[^"]+"', f'fill="{fg}"', inner)
    size, emb, gap = 72, 88, 20
    w = tp.measure("Filigran", size, "heading", 600)
    W, H = int(emb + gap + w + 8), 100
    tp.reset_defs()
    txt = tp.path("Filigran", emb + gap, 76, size, "heading", 600, fill=fg)
    glyphs = tp.defs()
    tp.reset_defs()
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" '
            f'aria-label="Filigran"><title>Filigran</title><defs>{glyphs}</defs>'
            f'<svg x="0" y="6" width="{emb}" height="{emb}" viewBox="0 0 24 24" fill="none">{inner}</svg>{txt}</svg>')
