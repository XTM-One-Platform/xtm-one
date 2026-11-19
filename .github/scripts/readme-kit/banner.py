"""Header banner: one 1280x300 SVG per product, identical geometry in all five.

Brand blue #001BDA field, the product's official brand lockup (brand deck asset
<product>-logo.svg) drawn from its own outlines, never typeset, every fill white
(Sam, 19 Nov 2026). Each lockup is split into its emblem and its wordmark, and both
are scaled apart so that, in all five banners:
  - the emblem has the same height (EMBLEM_H) and the same top;
  - the wordmark has the same cap height (CAP_H) and the same baseline (BASELINE),
    measured on a flat-topped capital of each wordmark (REF below);
  - the space between emblem and wordmark is one value (GAP; the shipped OpenCRQ
    lockup has a tighter gap than the other four);
  - the lockup is centred on the canvas.
"By" plus the official Filigran lockup (filigran-logo.svg, white) sit under the
wordmark, right-aligned to its end, at one cap height and baseline. Nothing needs
a font except "By", which is outlined."""
import os
import re
import xml.etree.ElementTree as ET
import textpath as tp

BRAND = os.path.join(os.path.dirname(__file__), "..", "brand", "logo")
W, H = 1280, 300
BLUE = "#001BDA"
WHITE = "#FFFFFF"
EMBLEM_H = 130.0      # px, every product emblem
CAP_H = 80.0          # px, cap height of every product wordmark
GAP = 54.0            # px, emblem right edge to wordmark left edge
EMBLEM_MID = 122.0    # px, emblem and wordmark cap share this vertical centre
BY_CAP = 26.0         # px, cap height of "By" and of the Filigran wordmark
BY_BASELINE = 240.0   # px
BY_SPACE = 12.0       # px, "By" to the Filigran emblem

# Which top-level paths of each brand logo form the emblem, and which flat-topped
# capital (path index, sub-path index) sets the wordmark's cap height and baseline.
SPLIT = {
    "opencti": {"emblem": [7, 8, 9, 10, 11, 12], "ref": (6, 0)},    # I
    "openaev": {"emblem": [0, 1, 2, 3, 4, 5], "ref": (11, 0)},      # E
    "opencrq": {"emblem": [6, 7, 8], "ref": (4, 1)},                # R
    "xtm-one": {"emblem": [0, 1, 2, 3], "ref": (4, 0)},             # X
    "xtm-hub": {"emblem": [0], "ref": (1, 0)},                      # X
    "filigran": {"emblem": [0], "ref": (8, 0)},                     # F
}

_SVG = "{http://www.w3.org/2000/svg}"
_TOK = re.compile(r"[A-Za-z]|-?\d*\.?\d+(?:e-?\d+)?")


def _mark(product: str) -> str:
    """White emblem alone (used by lockup.py for the platform table tiles)."""
    s = open(os.path.join(BRAND, "..", f"{product}-mark.svg")).read()
    inner = re.sub(r"^.*?<svg[^>]*>", "", s, flags=re.S)
    inner = re.sub(r"</svg>\s*$", "", inner, flags=re.S)
    inner = re.sub(r'fill="(?!none)[^"]+"', f'fill="{WHITE}"', inner)
    inner = re.sub(r'stroke="(?!none)[^"]+"', f'stroke="{WHITE}"', inner)
    inner = inner.replace('id="', f'id="{product}-').replace('url(#', f'url(#{product}-')
    return inner


def _paths(product: str) -> list[dict]:
    """The drawn <path> elements of a brand logo, in document order, outside any
    mask, clipPath or defs (the clip boxes of the deck exports draw nothing)."""
    root = ET.parse(os.path.join(BRAND, f"{product}-logo.svg")).getroot()
    out: list[dict] = []

    def walk(e, hidden):
        tag = e.tag.replace(_SVG, "")
        hidden = hidden or tag in ("mask", "clipPath", "defs")
        if tag == "path" and not hidden:
            out.append(dict(e.attrib))
        for c in e:
            walk(c, hidden)

    walk(root, False)
    return out


def _subpaths(d: str) -> list[list[tuple[float, float]]]:
    """Points of each sub-path (absolute M L H V C Z, which is all the deck exports
    use), cubic segments sampled finely enough for a sub-pixel bounding box."""
    t = _TOK.findall(d)
    i, cmd, x, y = 0, "M", 0.0, 0.0
    subs: list[list[tuple[float, float]]] = []
    pts: list[tuple[float, float]] = []
    while i < len(t):
        if t[i].isalpha():
            cmd = t[i]
            i += 1
            if cmd == "Z":
                if pts:
                    subs.append(pts)
                pts = []
                continue
        if cmd.islower():
            raise ValueError(f"relative command {cmd} not supported")
        if cmd == "M":
            if pts:
                subs.append(pts)
            x, y = float(t[i]), float(t[i + 1])
            i += 2
            pts = [(x, y)]
            cmd = "L"
        elif cmd == "L":
            x, y = float(t[i]), float(t[i + 1])
            i += 2
            pts.append((x, y))
        elif cmd == "H":
            x = float(t[i])
            i += 1
            pts.append((x, y))
        elif cmd == "V":
            y = float(t[i])
            i += 1
            pts.append((x, y))
        elif cmd == "C":
            c = [float(v) for v in t[i:i + 6]]
            i += 6
            x0, y0 = x, y
            for k in range(1, 33):
                s = k / 32
                a = 1 - s
                pts.append((a ** 3 * x0 + 3 * a * a * s * c[0] + 3 * a * s * s * c[2] + s ** 3 * c[4],
                            a ** 3 * y0 + 3 * a * a * s * c[1] + 3 * a * s * s * c[3] + s ** 3 * c[5]))
            x, y = c[4], c[5]
        else:
            raise ValueError(f"path command {cmd} not supported")
    if pts:
        subs.append(pts)
    return subs


def _bbox(ds: list[str]) -> tuple[float, float, float, float]:
    xs, ys = [], []
    for d in ds:
        for sp in _subpaths(d):
            xs += [p[0] for p in sp]
            ys += [p[1] for p in sp]
    return min(xs), min(ys), max(xs), max(ys)


def _group(paths: list[dict], tx: float, ty: float, k: float) -> str:
    body = "".join(
        "<path" + "".join(f' {a}="{p[a]}"' for a in ("fill-rule", "clip-rule") if a in p)
        + f' d="{p["d"]}"/>' for p in paths)
    return f'<g fill="{WHITE}" transform="translate({tx:.3f} {ty:.3f}) scale({k:.5f})">{body}</g>'


def parts(product: str) -> dict:
    """Emblem and wordmark of a brand logo with their native boxes."""
    ps = _paths(product)
    split = SPLIT[product]
    emblem = [p for i, p in enumerate(ps) if i in split["emblem"]]
    word = [p for i, p in enumerate(ps) if i not in split["emblem"]]
    pi, si = split["ref"]
    ref = _subpaths(ps[pi]["d"])[si]
    return {
        "emblem": emblem, "word": word,
        "emblem_box": _bbox([p["d"] for p in emblem]),
        "word_box": _bbox([p["d"] for p in word]),
        "cap_top": min(p[1] for p in ref), "baseline": max(p[1] for p in ref),
    }


def layout(product: str) -> dict:
    """Placement of every element in banner pixels (also read by the gate)."""
    p = parts(product)
    ex0, ey0, ex1, ey1 = p["emblem_box"]
    ke = EMBLEM_H / (ey1 - ey0)
    wx0, _, wx1, _ = p["word_box"]
    kw = CAP_H / (p["baseline"] - p["cap_top"])
    ew = (ex1 - ex0) * ke
    ww = (wx1 - wx0) * kw
    x0 = (W - (ew + GAP + ww)) / 2
    emblem_top = EMBLEM_MID - EMBLEM_H / 2
    baseline = EMBLEM_MID + CAP_H / 2
    word_x = x0 + ew + GAP
    return {
        "emblem": (x0 - ex0 * ke, emblem_top - ey0 * ke, ke),
        "word": (word_x - wx0 * kw, baseline - p["baseline"] * kw, kw),
        "emblem_px": (x0, emblem_top, x0 + ew, emblem_top + EMBLEM_H),
        "cap_top_px": baseline - CAP_H, "baseline_px": baseline,
        "word_end_px": word_x + ww, "parts": p,
    }


def banner(product: str, name: str) -> str:
    L = layout(product)
    p = L["parts"]
    f = parts("filigran")
    kf = BY_CAP / (f["baseline"] - f["cap_top"])
    fx0, _, fx1, _ = _bbox([q["d"] for q in f["emblem"] + f["word"]])
    fil_w = (fx1 - fx0) * kf
    fil_x = L["word_end_px"] - fil_w
    by_size = BY_CAP / 0.7          # Geologica cap height is 0.7 em
    by_w = tp.measure("By", by_size, "heading", 400)
    tp.reset_defs()
    by = tp.path("By", fil_x - BY_SPACE - by_w, BY_BASELINE, by_size, "heading", 400, fill=WHITE)
    glyphs = tp.defs()
    tp.reset_defs()
    fil = _group(f["emblem"] + f["word"], fil_x - fx0 * kf, BY_BASELINE - f["baseline"] * kf, kf)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="{name} by Filigran">',
        f"<title>{name} by Filigran</title>",
        f"<defs>{glyphs}</defs>",
        f'<rect width="{W}" height="{H}" fill="{BLUE}"/>',
        _group(p["emblem"], *L["emblem"]),
        _group(p["word"], *L["word"]),
        by,
        fil,
        "</svg>",
    ]
    return "\n".join(out)
