"""Shared drawing primitives of the README kit: canvas, cards, chips, arrows.
Every text is outlined with textpath; a label that does not fit its box raises,
so no box ever wraps or clips its text."""
import os
import re
import textpath as tp

BRAND = os.path.join(os.path.dirname(__file__), "..", "brand")
W, H = 1440, 820          # every diagram canvas, all five READMEs
RADIUS = 18


class Overflow(ValueError):
    pass


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;")


def canvas(theme: dict, title: str, body: list[str], w: int = W, h: int = H) -> str:
    glyphs = tp.defs()
    tp.reset_defs()
    return "\n".join([
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="{esc(title)}">',
        f"<title>{esc(title)}</title>",
        "<defs>",
        f'<marker id="arr" viewBox="0 0 10 10" refX="8" refY="5" markerUnits="userSpaceOnUse" markerWidth="13" markerHeight="13" orient="auto-start-reverse"><path d="M0 1L10 5L0 9z" fill="{theme["accent"]}"/></marker>',
        f'<marker id="arrm" viewBox="0 0 10 10" refX="8" refY="5" markerUnits="userSpaceOnUse" markerWidth="13" markerHeight="13" orient="auto-start-reverse"><path d="M0 1L10 5L0 9z" fill="{theme["muted"]}"/></marker>',
        glyphs,
        "</defs>",
        f'<rect width="{w}" height="{h}" rx="24" fill="{theme["page"]}"/>',
        *body,
        "</svg>",
    ])


def text(s, x, y, size, theme_color, face="body", weight=400, anchor="start", maxw=None, opacity=None):
    if maxw is not None and tp.measure(s, size, face, weight) > maxw:
        raise Overflow(f"{s!r} at {size}px is wider than {maxw}px")
    return tp.path(s, x, y, size, face, weight, anchor=anchor, fill=theme_color, opacity=opacity)


def mark(product: str, x, y, size, color) -> str:
    s = open(os.path.join(BRAND, f"{product}-mark.svg")).read()
    inner = re.sub(r"^.*?<svg[^>]*>", "", s, flags=re.S)
    inner = re.sub(r"</svg>\s*$", "", inner, flags=re.S)
    inner = re.sub(r'fill="(?!none)[^"]+"', f'fill="{color}"', inner)
    inner = re.sub(r'stroke="(?!none)[^"]+"', f'stroke="{color}"', inner)
    uid = f"{product}{int(x)}{int(y)}"
    inner = inner.replace('id="', f'id="{uid}-').replace("url(#", f"url(#{uid}-")
    return f'<svg x="{x:.1f}" y="{y:.1f}" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none">{inner}</svg>'


def card(theme, x, y, w, h, title, sub=None, color=None, product=None, title_size=26, sub_size=19,
         fill=None, stroke=None, center=False, pad=24):
    """A rounded card: optional product mark, a title line, an optional subtitle line."""
    color = color or theme["accent"]
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{RADIUS}" fill="{fill or theme["card"]}" stroke="{stroke or theme["line"]}" stroke-width="1.5"/>']
    tx = x + pad
    if product:
        ms = min(40, h - 32)
        out.append(mark(product, x + pad, y + (h - ms) / 2, ms, color))
        tx = x + pad + ms + 18
    avail = x + w - pad - tx
    if sub:
        ty = y + h / 2 - 4
        sy = y + h / 2 + sub_size + 4
    else:
        ty = y + h / 2 + title_size * 0.36
    if center and not product:
        cx = x + w / 2
        out.append(text(title, cx, ty, title_size, theme["fg"], "heading", 600, "middle", maxw=w - 2 * pad))
        if sub:
            out.append(text(sub, cx, sy, sub_size, theme["muted"], "body", 400, "middle", maxw=w - 2 * pad))
    else:
        out.append(text(title, tx, ty, title_size, theme["fg"], "heading", 600, maxw=avail))
        if sub:
            out.append(text(sub, tx, sy, sub_size, theme["muted"], "body", 400, maxw=avail))
    return "\n".join(out)


def accent_bar(x, y, h, color):
    return f'<rect x="{x}" y="{y + 18}" width="6" height="{h - 36}" rx="3" fill="{color}"/>'


def line(x1, y1, x2, y2, color, dashed=False, marker="arr", width=2.5):
    d = ' stroke-dasharray="7 7"' if dashed else ""
    m = f' marker-end="url(#{marker})"' if marker else ""
    return f'<path d="M{x1:.1f} {y1:.1f}L{x2:.1f} {y2:.1f}" stroke="{color}" stroke-width="{width}" fill="none"{d}{m}/>'


def curve(x1, y1, x2, y2, color, dashed=False, marker="arr", width=2.5):
    mx = (x1 + x2) / 2
    d = ' stroke-dasharray="7 7"' if dashed else ""
    m = f' marker-end="url(#{marker})"' if marker else ""
    return f'<path d="M{x1:.1f} {y1:.1f}C{mx:.1f} {y1:.1f} {mx:.1f} {y2:.1f} {x2:.1f} {y2:.1f}" stroke="{color}" stroke-width="{width}" fill="none"{d}{m}/>'


def chip(theme, cx, cy, label, color, size=20, padx=18, h=40):
    w = tp.measure(label, size, "heading", 600) + 2 * padx
    return (f'<rect x="{cx - w / 2:.1f}" y="{cy - h / 2:.1f}" width="{w:.1f}" height="{h}" rx="{h / 2}" fill="{color}"/>'
            + text(label, cx, cy + size * 0.36, size, theme["page"], "heading", 600, "middle"))


def header(theme, title, subtitle):
    return "\n".join([
        text(title, 56, 82, 40, theme["fg"], "heading", 600),
        text(subtitle, 56, 120, 22, theme["muted"], "body", 400),
    ])
