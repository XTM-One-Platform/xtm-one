"""Outline text into SVG paths so the README images render in the brand fonts
on github.com, where an SVG served as an image cannot load a web font."""
from functools import lru_cache
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
import os
import re

_NUM = re.compile(r"-?\d+\.?\d*(?:e-?\d+)?")


def _fmt(v: float) -> str:
    s = f"{v:.1f}"
    return s[:-2] if s.endswith(".0") else s

FONT_DIR = os.path.join(os.path.dirname(__file__), "..", "fonts")
FILES = {"heading": "Geologica.ttf", "body": "IBMPlexSans.ttf"}


@lru_cache(maxsize=None)
def _font(face: str, weight: int):
    f = TTFont(os.path.join(FONT_DIR, FILES[face]))
    if "fvar" in f:
        loc = {a.axisTag: a.defaultValue for a in f["fvar"].axes}
        loc["wght"] = weight
        if "wdth" in loc:
            loc["wdth"] = 100
        f = instancer.instantiateVariableFont(f, loc)
    return f


def measure(text: str, size: float, face: str = "body", weight: int = 400) -> float:
    f = _font(face, weight)
    cmap = f.getBestCmap()
    hmtx = f["hmtx"]
    upm = f["head"].unitsPerEm
    return sum(hmtx[cmap.get(ord(c), ".notdef")][0] for c in text) * size / upm


_DEFS: dict[str, str] = {}


def reset_defs() -> None:
    _DEFS.clear()


def defs() -> str:
    """The glyph outlines used since the last reset, each once, in font units
    scaled to a 1000-unit em and flipped to SVG's y-down axis."""
    return "".join(_DEFS.values())


def _glyph_id(face: str, weight: int, g: str) -> str:
    gid = f"g{face[0]}{weight}-{''.join(ch if ch.isalnum() else '_' for ch in g)}"
    if gid not in _DEFS:
        f = _font(face, weight)
        gs = f.getGlyphSet()
        k = 1000 / f["head"].unitsPerEm
        pen = SVGPathPen(gs)
        gs[g].draw(TransformPen(pen, (k, 0, 0, -k, 0, 0)))
        d = _NUM.sub(lambda m: str(round(float(m.group(0)))), pen.getCommands())
        _DEFS[gid] = f'<path id="{gid}" d="{d}"/>'
    return gid


def path(text: str, x: float, y: float, size: float, face: str = "body", weight: int = 400,
         anchor: str = "start", fill: str = "#000", opacity: float | None = None) -> str:
    """A string as <use> references to shared glyph outlines; y is the baseline."""
    f = _font(face, weight)
    cmap = f.getBestCmap()
    hmtx = f["hmtx"]
    upm = f["head"].unitsPerEm
    w = measure(text, size, face, weight)
    if anchor == "middle":
        x -= w / 2
    elif anchor == "end":
        x -= w
    s = size / 1000
    uses = []
    cx = 0.0
    for c in text:
        g = cmap.get(ord(c), ".notdef")
        if c != " ":
            uses.append(f'<use href="#{_glyph_id(face, weight, g)}" x="{_fmt(cx * 1000 / upm)}"/>')
        cx += hmtx[g][0]
    op = f' fill-opacity="{opacity}"' if opacity is not None else ""
    return (f'<g fill="{fill}"{op} transform="translate({_fmt(x)} {_fmt(y)}) scale({s:.4f})">'
            + "".join(uses) + "</g>")


def wrap(text: str, size: float, width: float, face: str = "body", weight: int = 400) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for wd in words:
        t = (cur + " " + wd).strip()
        if measure(t, size, face, weight) <= width or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    return lines
