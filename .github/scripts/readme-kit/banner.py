"""Header banner: one 1280x300 SVG per product, identical geometry in all five.
Brand blue #001BDA field, white emblem, white Geologica wordmark, "By Filigran"
right-aligned under the wordmark. Text is outlined (no font needed to render)."""
import os
import re
import textpath as tp

BRAND = os.path.join(os.path.dirname(__file__), "..", "brand")
W, H = 1280, 300
EMBLEM = 156          # emblem box, px
GAP = 44              # emblem to wordmark
NAME_SIZE = 124       # wordmark font size
BASELINE = 178        # wordmark baseline, same in all five
BY_SIZE = 40
BY_BASELINE = 246
BLUE = "#001BDA"
WHITE = "#FFFFFF"


def _mark(product: str) -> str:
    s = open(os.path.join(BRAND, f"{product}-mark.svg")).read()
    inner = re.sub(r"^.*?<svg[^>]*>", "", s, flags=re.S)
    inner = re.sub(r"</svg>\s*$", "", inner, flags=re.S)
    inner = re.sub(r'fill="(?!none)[^"]+"', f'fill="{WHITE}"', inner)
    inner = re.sub(r'stroke="(?!none)[^"]+"', f'stroke="{WHITE}"', inner)
    inner = inner.replace('id="', f'id="{product}-').replace('url(#', f'url(#{product}-')
    return inner


def banner(product: str, name: str) -> str:
    nw = tp.measure(name, NAME_SIZE, "heading", 500)
    total = EMBLEM + GAP + nw
    x0 = (W - total) / 2
    ey = BASELINE - 0.72 * NAME_SIZE / 2 - EMBLEM / 2 + 0  # emblem centred on cap height
    tx = x0 + EMBLEM + GAP
    by = "By "
    fw = tp.measure("Filigran", BY_SIZE, "heading", 600)
    bw = tp.measure(by, BY_SIZE, "heading", 400)
    end = tx + nw
    tp.reset_defs()
    texts = [
        tp.path(name, tx, BASELINE, NAME_SIZE, "heading", 500, fill=WHITE),
        tp.path(by, end - fw - bw, BY_BASELINE, BY_SIZE, "heading", 400, fill=WHITE),
        tp.path("Filigran", end - fw, BY_BASELINE, BY_SIZE, "heading", 600, fill=WHITE),
    ]
    glyphs = tp.defs()
    tp.reset_defs()
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="{name} by Filigran">',
        f'<title>{name} by Filigran</title>',
        f"<defs>{glyphs}</defs>",
        f'<rect width="{W}" height="{H}" fill="{BLUE}"/>',
        f'<svg x="{x0:.2f}" y="{ey:.2f}" width="{EMBLEM}" height="{EMBLEM}" viewBox="0 0 24 24" fill="none">{_mark(product)}</svg>',
        *texts,
        "</svg>",
    ]
    return "\n".join(parts)
