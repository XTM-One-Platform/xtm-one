"""Small kit assets, rule G (Sam, 19 Nov 2026): every one is a dark / light
pair taken from the official brand lockups, never recoloured by hand.
- mark(product, theme): the product emblem of the platform table, in the colour
  of theme.products (the fills of the official twins, see theme.py);
- filigran(theme): the official Filigran lockup, verbatim: filigran-logo.svg on
  dark (cyan emblem, #F2F2F3 wordmark), filigran-logo-light.svg on light."""
import os
import re

import svgkit as k

BRAND = os.path.join(os.path.dirname(__file__), "..", "brand")
OFFICIAL = {"dark": "filigran-logo.svg", "light": "filigran-logo-light.svg"}


def mark(product: str, theme: dict) -> str:
    """48 px emblem on a transparent field, the official colour of the theme."""
    inner = k.mark(product, 4, 4, 40, theme["products"][product])
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 48 48" role="img" '
            f'aria-label="{product}">{inner}</svg>')


def filigran(theme: dict) -> str:
    """The official lockup file of the theme, unchanged but its width and height
    (100 px high) and an accessible title."""
    s = open(os.path.join(BRAND, "logo", OFFICIAL[theme["name"]])).read()
    vb = re.search(r'viewBox="([^"]+)"', s).group(1)
    _, _, vw, vh = (float(v) for v in vb.split())
    H = 100
    W = round(vw * H / vh)
    s = re.sub(r'<svg\b[^>]*>', (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
                                 f'viewBox="{vb}" fill="none" role="img" aria-label="Filigran">'
                                 f'<title>Filigran</title>'), s, count=1)
    return s
