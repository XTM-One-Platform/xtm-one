"""Header banner: one 1280x300 SVG per product, identical geometry in all five.
Brand blue #001BDA field and the product's OWN brand logo (mark + wordmark, light-on-dark,
the brand deck asset <product>-logo.svg, kept byte for byte: cyan mark for OpenCTI, OpenAEV,
OpenCRQ, white mark for XTM One and XTM Hub, white brand wordmark), scaled to one cap height
so the five wordmarks line up; "By Filigran" right-aligned under the logo. Text is outlined
(no font needed to render)."""
import os
import re
import textpath as tp

BRAND = os.path.join(os.path.dirname(__file__), "..", "brand", "logo")
W, H = 1280, 300
LOGO_H = 26 * 5.2     # every product logo drawn from a 26-unit-high artboard, scaled x5.2
LOGO_TOP = 62
BY_SIZE = 38
BY_BASELINE = 250
BLUE = "#001BDA"
WHITE = "#FFFFFF"


def _mark(product: str) -> str:
    """White emblem alone (used by lockup.py for the platform table tiles)."""
    s = open(os.path.join(BRAND, "..", f"{product}-mark.svg")).read()
    inner = re.sub(r"^.*?<svg[^>]*>", "", s, flags=re.S)
    inner = re.sub(r"</svg>\s*$", "", inner, flags=re.S)
    inner = re.sub(r'fill="(?!none)[^"]+"', f'fill="{WHITE}"', inner)
    inner = re.sub(r'stroke="(?!none)[^"]+"', f'stroke="{WHITE}"', inner)
    inner = inner.replace('id="', f'id="{product}-').replace('url(#', f'url(#{product}-')
    return inner


def _logo(product: str) -> tuple[str, float, float]:
    """Inner markup, viewBox width and height of the brand logo, ids made unique."""
    s = open(os.path.join(BRAND, f"{product}-logo.svg")).read()
    vb = [float(x) for x in re.search(r'viewBox="([^"]+)"', s).group(1).split()]
    inner = re.sub(r"^.*?<svg[^>]*>", "", s, flags=re.S)
    inner = re.sub(r"</svg>\s*$", "", inner, flags=re.S)
    inner = inner.replace('id="', f'id="{product}-').replace('url(#', f'url(#{product}-')
    return inner, vb[2], vb[3]


def banner(product: str, name: str) -> str:
    inner, vw, vh = _logo(product)
    scale = LOGO_H / vh
    lw = vw * scale
    x0 = (W - lw) / 2
    fw = tp.measure("Filigran", BY_SIZE, "heading", 600)
    bw = tp.measure("By ", BY_SIZE, "heading", 400)
    end = x0 + lw
    tp.reset_defs()
    texts = [
        tp.path("By ", end - fw - bw, BY_BASELINE, BY_SIZE, "heading", 400, fill=WHITE),
        tp.path("Filigran", end - fw, BY_BASELINE, BY_SIZE, "heading", 600, fill=WHITE),
    ]
    glyphs = tp.defs()
    tp.reset_defs()
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="{name} by Filigran">',
        f'<title>{name} by Filigran</title>',
        f"<defs>{glyphs}</defs>",
        f'<rect width="{W}" height="{H}" fill="{BLUE}"/>',
        f'<svg x="{x0:.2f}" y="{LOGO_TOP}" width="{lw:.2f}" height="{LOGO_H:.2f}" viewBox="0 0 {vw} {vh}" fill="none">{inner}</svg>',
        *texts,
        "</svg>",
    ]
    return "\n".join(parts)
