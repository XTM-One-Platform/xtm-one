# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""PPTX writers for presentation export.

Two variants, both fed by the headless render in ``presentation_export.py``:

* :func:`build_image_pptx` — **pixel-perfect**: one full-bleed PNG of each slide
  (what Chromium painted) plus the speaker notes. The deck looks exactly like
  the screen in PowerPoint / Keynote / Google Slides; nothing is editable but
  the notes.
* :func:`build_native_pptx` — **editable**: the primitives measured from the
  rendered DOM (``EXTRACT_PRIMITIVES_JS``) become native shapes — a text block
  is a text box with the same runs, fonts, sizes and colours at the same
  position; a card is a rounded rectangle with the same fill, border and
  radius; an icon, logo, image or Mermaid diagram is the PNG captured from the
  page. Positions are the browser's, so nothing is re-laid-out here; PowerPoint
  may still wrap a long line one word differently, which is why the image
  variant exists. A ``chart`` block is the one primitive that arrives
  as DATA rather than as pixels: the figure publishes its series, categories,
  format and the value axis it settled on, and :func:`_add_chart` rebuilds it
  as a native PowerPoint chart (bar, stacked bar, line, area, pie, doughnut,
  a combo's bars and lines as plots of one chart, a right-hand value axis for
  the series read against it - editable series, its own legend, data
  labels in the viewer's number format) pinned by manual layout to where the
  viewer drew the legend and the plot. A waterfall is a stacked column
  chart whose steps stand on an invisible base series. A butterfly is
  a clustered bar chart whose left series is stored negative, written without
  its sign by its number format, which labels a zero with nothing. A gauge
  and a map have no PowerPoint equivalent that opens outside Office,
  and bars read against two value axes PowerPoint draws one over the other:
  those stay a picture, captured at 2x, their title, caption and legend
  written as text; every such stand-in is one line of the export log. An
  ``org_chart`` arrives as the shapes its layout engine drew: rounded
  rectangles, ovals of their size, text, and straight connectors along its
  lines, the chart one group shape and each seat a group inside it.

Why measure the DOM rather than map the spec block by block: a mapping would
be a second implementation of every layout that drifts from the stylesheet on
each change, while the measurement is by construction what the screen shows.

Geometry: the canvas is 1920x1080 px (1440x1080 for 4:3) and the slide is
13.333x7.5in (10x7.5in), so 1 canvas px = 1/144 in = 0.5 pt in both slide
ratios; the A4 / letter portrait pages keep the 1920px-wide canvas on real
paper (21x29.7cm / 8.5x11in), so a canvas px there is ~0.31pt. Colours arrive
with alpha (CSS ``rgba``) and are blended against the slide background here
because a PPTX solid fill has no alpha channel.
"""

from __future__ import annotations

import io
import itertools
import logging
import math
import re
from collections.abc import Sequence
from typing import Any

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Pt

from app.services.presentation_markup import LINK_URL_PATTERN
from app.services.presentation_notes import notes_plain_text
from app.services.presentation_spec import CANVAS_SIZES_PX as _SPEC_CANVAS_SIZES_PX
from app.services.presentation_spec import LEGEND_POSITIONS, PRESENTATION_TRANSITIONS

logger = logging.getLogger(__name__)

_BLANK_LAYOUT = 6

#: Slide size per format, in EMU — PowerPoint's own 16:9 (13.333x7.5in) and
#: 4:3 (10x7.5in) defaults as exact integers so a slide is byte-identical to
#: one PowerPoint creates and matches the PDF page (``PDF_PAGE_SIZES_IN``);
#: exact ISO A4 (210x297mm, 360 000 EMU/cm) and US Letter (8.5x11in) for the
#: portrait pages.
SLIDE_SIZES_EMU: dict[str, tuple[int, int]] = {
    "16:9": (12_192_000, 6_858_000),
    "4:3": (9_144_000, 6_858_000),
    "A4": (7_560_000, 10_692_000),
    "letter": (7_772_400, 10_058_400),
}
#: The authoring canvas the primitives are measured on (``presentation_spec``,
#: mirrored by ``geometry.ts``).
CANVAS_SIZES_PX: dict[str, tuple[int, int]] = _SPEC_CANVAS_SIZES_PX
assert set(SLIDE_SIZES_EMU) == set(CANVAS_SIZES_PX)  # noqa: S101 - import-time contract

#: Generic CSS families → a font every office suite has. The deck's real fonts
#: (Geologica, IBM Plex Sans, IBM Plex Mono) are written by name: a machine
#: that has them renders the deck exactly, one that does not substitutes.
_GENERIC_FONTS = {
    "system-ui": "Calibri",
    "sans-serif": "Calibri",
    "serif": "Cambria",
    "monospace": "Consolas",
    "ui-monospace": "Consolas",
    "-apple-system": "Calibri",
    "blinkmacsystemfont": "Calibri",
}

_ALIGN = {
    "center": PP_ALIGN.CENTER,
    "right": PP_ALIGN.RIGHT,
    "end": PP_ALIGN.RIGHT,
    "justify": PP_ALIGN.JUSTIFY,
}

#: Extra width, as a fraction of the measured box, given to a WRAPPED text box.
#: PowerPoint's glyph advances run a few percent wider than Chromium's for the
#: same font at the same size; on a box that wraps, that difference is one
#: extra line, which lands the last one outside the box (measured on the
#: product-brief key / value cells: two lines in the browser, three in
#: PowerPoint). 4% absorbs it without a text box crossing into its neighbour's
#: column at any layout gap the stylesheet uses.
_WRAP_SLACK_RATIO = 0.04


def _slide_geometry(aspect_ratio: str) -> tuple[int, int, float, float]:
    """``(slide_w_emu, slide_h_emu, emu_per_px, pt_per_px)`` for a ratio."""
    ratio = aspect_ratio if aspect_ratio in SLIDE_SIZES_EMU else "16:9"
    slide_w, slide_h = SLIDE_SIZES_EMU[ratio]
    canvas_w, _canvas_h = CANVAS_SIZES_PX[ratio]
    emu_per_px = slide_w / canvas_w
    # 12700 EMU per point; both slide ratios come out at exactly 0.5pt per
    # canvas px, the portrait pages at ~0.31pt (a denser, document type scale).
    pt_per_px = emu_per_px / 12_700
    return slide_w, slide_h, emu_per_px, pt_per_px


def _new_presentation(aspect_ratio: str, title: str) -> Presentation:
    slide_w, slide_h, _emu, _pt = _slide_geometry(aspect_ratio)
    prs = Presentation()
    prs.slide_width = Emu(slide_w)
    prs.slide_height = Emu(slide_h)
    prs.core_properties.title = (title or "Presentation")[:255]
    return prs


#: How each deck transition (``PRESENTATION_TRANSITIONS``, played by reveal.js
#: on screen) plays in PowerPoint: the closest transition of the base
#: PresentationML set, which every PowerPoint version reads without a repair
#: prompt - reveal's 3D ``convex`` / ``concave`` have no flat twin, so they
#: keep the movement (the next slide pushes in, or covers the last one from
#: the right). ``None`` is no transition: the slide cuts in.
_PPTX_TRANSITIONS: dict[str, tuple[str, dict[str, str]] | None] = {
    "none": None,
    "fade": ("fade", {}),
    "slide": ("push", {"dir": "l"}),
    "convex": ("push", {"dir": "l"}),
    "concave": ("cover", {"dir": "l"}),
    "zoom": ("zoom", {"dir": "in"}),
}
assert set(_PPTX_TRANSITIONS) == set(PRESENTATION_TRANSITIONS)  # noqa: S101 - import-time contract


def _set_transition(slide, transition: str | None) -> None:
    """Give *slide* the deck's transition, at reveal's default pace (``med``, 0.75 s).

    ``<p:transition>`` follows ``<p:clrMapOvr>`` in a slide part (the schema
    order: cSld, clrMapOvr, transition, timing).
    """
    effect = _PPTX_TRANSITIONS.get(transition or "none")
    if effect is None:
        return
    name, attrs = effect
    sld = slide._element
    element = sld.makeelement(qn("p:transition"), {"spd": "med"})
    element.append(sld.makeelement(qn(f"p:{name}"), attrs))
    anchor = sld.find(qn("p:clrMapOvr"))
    (anchor if anchor is not None else sld.find(qn("p:cSld"))).addnext(element)


def _set_notes(slide, notes: str | None) -> None:
    """Write the speaker notes as PLAIN text.

    The notes are authored in the deck's inline markup; PowerPoint's notes
    pane has no renderer for it, so the markers are stripped and the line
    breaks kept - the presenter reads "Land the 42%", not "Land the **42%**".
    """
    text = notes_plain_text(notes)
    if text:
        slide.notes_slide.notes_text_frame.text = text


# ── Slide links ───────────────────────────────────────────────────────────────

_LINK_URL_RE = re.compile(LINK_URL_PATTERN)


def add_slide_link_hotspots(prs, slide_links: Sequence[Sequence[dict]], aspect_ratio: str) -> None:
    """Lay an invisible click target over every slide link of every slide.

    *slide_links* is what ``SLIDE_LINK_BOXES_JS`` measured: per slide, the
    canvas-px boxes of its ``[text](#/<slide id>)`` links and the 0-based
    slide each jumps to (``target``), and of its web and e-mail links with
    their address (``href``, checked again against ``LINK_URL_PATTERN`` - a
    measured value is not trusted). Each box becomes a rectangle whose click
    action is PowerPoint's "jump to slide" (``ppaction://hlinksldjump``, set
    through ``click_action.target_slide``, which also writes the
    relationship) or a hyperlink to the address,
    filled at 0% opacity: a shape with NO fill is hit only on its outline in
    a slide show, a transparent fill is hit everywhere and paints nothing.
    A shape rather than a hyperlink on the text run, so the slide looks
    exactly as it renders - PowerPoint recolours and underlines hyperlinked
    text - and the same code serves the picture variant, whose text is a
    screenshot. Runs once every slide exists: a link may jump forward.
    """
    _slide_w, _slide_h, emu_per_px, _pt = _slide_geometry(aspect_ratio)
    slides = list(prs.slides)
    for index, boxes in enumerate(slide_links):
        if index >= len(slides):
            break
        slide = slides[index]
        for box in boxes or ():
            if not isinstance(box, dict):
                continue
            href = box.get("href")
            target = box.get("target")
            if href is not None:
                if not isinstance(href, str) or not _LINK_URL_RE.fullmatch(href):
                    continue
            elif not isinstance(target, int) or isinstance(target, bool) or not 0 <= target < len(slides):
                continue
            elif target == index:
                continue  # a link to its own slide goes nowhere
            w, h = float(box.get("w") or 0), float(box.get("h") or 0)
            if w <= 0 or h <= 0:
                continue
            shape = slide.shapes.add_shape(
                MSO_SHAPE.RECTANGLE,
                Emu(int(float(box.get("x") or 0) * emu_per_px)),
                Emu(int(float(box.get("y") or 0) * emu_per_px)),
                Emu(int(w * emu_per_px)),
                Emu(int(h * emu_per_px)),
            )
            shape.name = f"Link to {href[:200]}" if href else f"Link to slide {target + 1}"
            shape.fill.solid()
            shape.fill.fore_color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
            color = shape.fill._xPr.find(qn("a:solidFill")).find(qn("a:srgbClr"))
            color.append(color.makeelement(qn("a:alpha"), {"val": "0"}))
            shape.line.fill.background()
            shape.shadow.inherit = False
            if href:
                shape.click_action.hyperlink.address = href
            else:
                shape.click_action.target_slide = slides[target]


# ── Pixel-perfect variant ─────────────────────────────────────────────────────


def build_image_pptx(
    slide_images: Sequence[bytes],
    *,
    aspect_ratio: str,
    notes: Sequence[str | None] = (),
    title: str = "",
    transition: str = "none",
    slide_links: Sequence[Sequence[dict]] = (),
) -> bytes:
    """One slide per PNG, full bleed, with the matching speaker notes and the deck's transition.

    *slide_links* (``SLIDE_LINK_BOXES_JS``) puts a click target over each
    slide link drawn in the pictures (:func:`add_slide_link_hotspots`).
    """
    if not slide_images:
        raise ValueError("no slide images to write")
    prs = _new_presentation(aspect_ratio, title)
    for index, png in enumerate(slide_images):
        slide = prs.slides.add_slide(prs.slide_layouts[_BLANK_LAYOUT])
        slide.shapes.add_picture(io.BytesIO(png), 0, 0, width=prs.slide_width, height=prs.slide_height)
        _set_transition(slide, transition)
        _set_notes(slide, notes[index] if index < len(notes) else None)
    add_slide_link_hotspots(prs, slide_links, aspect_ratio)
    out = io.BytesIO()
    prs.save(out)
    return out.getvalue()


# ── Editable variant ──────────────────────────────────────────────────────────


def blend(color: dict | None, background: tuple[int, int, int], opacity: float = 1.0):
    """Flatten an ``{r,g,b,a}`` colour (times *opacity*) onto *background*.

    Returns an ``(r, g, b)`` tuple, or ``None`` when the colour is absent or
    fully transparent — the caller then draws nothing rather than a black box.
    """
    if not color:
        return None
    alpha = float(color.get("a", 1.0)) * max(0.0, min(1.0, float(opacity)))
    if alpha <= 0.002:
        return None
    channels = []
    for key, base in zip(("r", "g", "b"), background, strict=True):
        value = float(color.get(key, 0))
        channels.append(round(base + (value - base) * alpha))
    return tuple(max(0, min(255, c)) for c in channels)


def pick_font(css_family: str | None, *, mono: bool = False) -> str:
    """First concrete family of a CSS ``font-family`` list, generics mapped."""
    if not css_family:
        return "Consolas" if mono else "Calibri"
    for raw in css_family.split(","):
        name = raw.strip().strip("\"'")
        if not name:
            continue
        generic = _GENERIC_FONTS.get(name.lower())
        if generic:
            return generic
        return name[:60]
    return "Consolas" if mono else "Calibri"


def _rgb(rgb: tuple[int, int, int]) -> RGBColor:
    return RGBColor(*rgb)


def _gradient_stops(item: dict, background: tuple[int, int, int]) -> tuple[tuple[int, int, int], ...] | None:
    """The two colours of a rect's ``gradient`` (a formula's result card), blended over the slide."""
    gradient = item.get("gradient")
    if not isinstance(gradient, dict):
        return None
    stops = gradient.get("stops")
    if not isinstance(stops, list) or len(stops) != 2:
        return None
    blended = tuple(blend(stop, background, item.get("opacity", 1.0)) for stop in stops)
    if any(color is None for color in blended):
        return None
    return blended  # type: ignore[return-value]


def _set_gradient_fill(shape, stops: tuple[tuple[int, int, int], ...], css_angle: float) -> None:
    """A native two-stop linear gradient fill, at the CSS angle the page drew.

    CSS measures ``0deg`` as "to top" and turns clockwise; DrawingML's
    ``a:lin ang`` measures 0 as "to the right", clockwise, in 60000ths of a
    degree: the DrawingML angle is the CSS one minus 90.
    """
    shape.fill.gradient()
    gradient_stops = shape.fill.gradient_stops
    for stop, color in zip(gradient_stops, stops, strict=False):
        stop.color.rgb = _rgb(color)
    gradient_stops[0].position = 0.0
    gradient_stops[1].position = 1.0
    shape.fill.gradient_angle = (css_angle - 90.0) % 360.0


def _add_rect(slide, item: dict, geometry, background: tuple[int, int, int]) -> None:
    _slide_w, _slide_h, emu_per_px, _pt = geometry
    fill = blend(item.get("fill"), background, item.get("opacity", 1.0))
    line = item.get("line") or None
    line_rgb = blend(line, background, item.get("opacity", 1.0)) if line else None
    edges = item.get("edges") or None
    gradient = _gradient_stops(item, background)
    if fill is None and line_rgb is None and not edges and gradient is None:
        return
    x, y, w, h = (float(item.get(k, 0)) for k in ("x", "y", "w", "h"))
    if w <= 0 or h <= 0:
        return
    if fill is not None or line_rgb is not None or gradient is not None:
        radius = float(item.get("radius") or 0)
        shape_type = MSO_SHAPE.ROUNDED_RECTANGLE if radius > 0 else MSO_SHAPE.RECTANGLE
        shape = slide.shapes.add_shape(
            shape_type,
            Emu(int(x * emu_per_px)),
            Emu(int(y * emu_per_px)),
            Emu(int(w * emu_per_px)),
            Emu(int(h * emu_per_px)),
        )
        if radius > 0:
            # The adjustment is the corner radius as a fraction of the shorter side.
            shape.adjustments[0] = max(0.0, min(0.5, radius / max(1.0, min(w, h))))
        shape.shadow.inherit = False
        if gradient is not None:
            _set_gradient_fill(shape, gradient, float((item.get("gradient") or {}).get("angle", 180)))
        elif fill is not None:
            shape.fill.solid()
            shape.fill.fore_color.rgb = _rgb(fill)
        else:
            shape.fill.background()
        if line_rgb is not None:
            shape.line.color.rgb = _rgb(line_rgb)
            shape.line.width = Emu(int(float(line.get("width", 1)) * emu_per_px))
            dash = _dash_style(item.get("dash"))
            if dash is not None:
                shape.line.dash_style = dash
        else:
            shape.line.fill.background()
        shape.text_frame.text = ""
    if edges:
        # A one-sided border (the accent bar of a callout, a table rule) has no
        # shape equivalent: draw each painted edge as its own thin rectangle.
        # Each side keeps its own colour when the extractor reports them (a
        # card is a thin grey frame with a thick toned top rule); the single
        # ``color`` is the fallback for a payload that predates that.
        fallback_rgb = blend(edges.get("color"), background, item.get("opacity", 1.0))
        colors = edges.get("colors") or {}
        sides = {
            "top": (x, y, w, float(edges.get("top", 0))),
            "bottom": (x, y + h - float(edges.get("bottom", 0)), w, float(edges.get("bottom", 0))),
            "left": (x, y, float(edges.get("left", 0)), h),
            "right": (x + w - float(edges.get("right", 0)), y, float(edges.get("right", 0)), h),
        }
        for side, (ex, ey, ew, eh) in sides.items():
            if ew <= 0 or eh <= 0:
                continue
            edge_rgb = (
                blend(colors[side], background, item.get("opacity", 1.0))
                if colors.get(side)
                else fallback_rgb
            )
            if edge_rgb is None:
                continue
            bar = slide.shapes.add_shape(
                MSO_SHAPE.RECTANGLE,
                Emu(int(ex * emu_per_px)),
                Emu(int(ey * emu_per_px)),
                Emu(max(1, int(ew * emu_per_px))),
                Emu(max(1, int(eh * emu_per_px))),
            )
            bar.shadow.inherit = False
            bar.fill.solid()
            bar.fill.fore_color.rgb = _rgb(edge_rgb)
            bar.line.fill.background()


def _dash_style(dash: object):
    """The PowerPoint preset of a dashed outline the page drew (``dash`` / ``dot``), or ``None`` for a solid one."""
    from pptx.enum.dml import MSO_LINE_DASH_STYLE

    return {"dash": MSO_LINE_DASH_STYLE.DASH, "dot": MSO_LINE_DASH_STYLE.ROUND_DOT}.get(str(dash or ""))


def _add_oval(target, item: dict, geometry, background: tuple[int, int, int]) -> None:
    """A circle of an org chart (a team's bubble, a person's tile) as an oval of its size."""
    _slide_w, _slide_h, emu_per_px, _pt = geometry
    fill = blend(item.get("fill"), background, item.get("opacity", 1.0))
    line = item.get("line") or None
    line_rgb = blend(line, background, item.get("opacity", 1.0)) if line else None
    x, y, w, h = (float(item.get(k, 0)) for k in ("x", "y", "w", "h"))
    if (fill is None and line_rgb is None) or w <= 0 or h <= 0:
        return
    shape = target.shapes.add_shape(
        MSO_SHAPE.OVAL, Emu(int(x * emu_per_px)), Emu(int(y * emu_per_px)), Emu(int(w * emu_per_px)), Emu(int(h * emu_per_px))
    )
    shape.shadow.inherit = False
    if fill is not None:
        shape.fill.solid()
        shape.fill.fore_color.rgb = _rgb(fill)
    else:
        shape.fill.background()
    if line_rgb is not None:
        shape.line.color.rgb = _rgb(line_rgb)
        shape.line.width = Emu(int(float(line.get("width", 1)) * emu_per_px))
        dash = _dash_style(item.get("dash"))
        if dash is not None:
            shape.line.dash_style = dash
    else:
        shape.line.fill.background()
    shape.text_frame.text = ""


def _add_connector(target, item: dict, geometry, background: tuple[int, int, int]) -> None:
    """One line of an org chart (a bus, a drop, a dotted line): a straight connector per segment of its polyline."""
    from pptx.enum.shapes import MSO_CONNECTOR

    _slide_w, _slide_h, emu_per_px, _pt = geometry
    line = item.get("line") or None
    rgb = blend(line, background, item.get("opacity", 1.0)) if line else None
    points = [p for p in item.get("points") or [] if isinstance(p, dict)]
    if rgb is None or len(points) < 2:
        return
    dash = _dash_style(item.get("dash"))
    for a, b in itertools.pairwise(points):
        ends = [_number(value, float("nan")) for value in (a.get("x"), a.get("y"), b.get("x"), b.get("y"))]
        if not all(math.isfinite(end) for end in ends) or (ends[0] == ends[2] and ends[1] == ends[3]):
            continue
        connector = target.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, *(Emu(int(end * emu_per_px)) for end in ends))
        connector.line.color.rgb = _rgb(rgb)
        connector.line.width = Emu(int(max(0.5, float(line.get("width", 1))) * emu_per_px))
        if dash is not None:
            connector.line.dash_style = dash


def _add_text(slide, item: dict, geometry, background: tuple[int, int, int]) -> None:
    _slide_w, _slide_h, emu_per_px, pt_per_px = geometry
    runs = [r for r in item.get("runs") or [] if isinstance(r, dict)]
    if not any((r.get("text") or "").strip() for r in runs):
        return
    x, y, w, h = (float(item.get(k, 0)) for k in ("x", "y", "w", "h"))
    if w <= 0 or h <= 0:
        return
    # Text set on its side (a vertical axis title): the measured box is the
    # turned one, so the shape is laid out upright about the same centre and
    # turned by the reading angle the page measured.
    rotation = float(item.get("rotate") or 0) % 360
    if rotation in (90.0, 270.0):
        cx, cy = x + w / 2, y + h / 2
        w, h = h, w
        x, y = cx - w / 2, cy - h / 2
    line_height_px = float(item.get("lineHeight") or 0)
    padding = item.get("padding") or {}
    # A box Chromium laid out as ONE line stays one line: PowerPoint's glyph
    # metrics differ slightly (a lot at display sizes) and would otherwise
    # break a tight single-line box into two.
    inner_h = h - float(padding.get("top", 0)) - float(padding.get("bottom", 0))
    single_line = line_height_px > 0 and inner_h < line_height_px * 1.6
    # Slack for PowerPoint's wider glyph advances: a hair on a single line, a
    # few percent on a wrapped box, where the same difference would break one
    # more line than Chromium did and push the last one out of the box. The box
    # itself is transparent, so widening it costs nothing visually - as long
    # as the text keeps its anchor: a right-aligned column grows to the LEFT
    # and a centred one on both sides, or the alignment would drift.
    slack = 6.0 if single_line else max(6.0, w * _WRAP_SLACK_RATIO)
    align = str(item.get("align") or "")
    shift = slack if align in ("right", "end") else slack / 2 if align == "center" else 0.0
    box = slide.shapes.add_textbox(
        Emu(int((x - shift) * emu_per_px)),
        Emu(int(y * emu_per_px)),
        Emu(int((w + slack) * emu_per_px)),
        Emu(int(h * emu_per_px)),
    )
    if rotation:
        box.rotation = rotation
    frame = box.text_frame
    frame.word_wrap = not single_line
    frame.auto_size = MSO_AUTO_SIZE.NONE
    frame.margin_left = Emu(int(float(padding.get("left", 0)) * emu_per_px))
    frame.margin_right = Emu(int(float(padding.get("right", 0)) * emu_per_px))
    frame.margin_top = Emu(int(float(padding.get("top", 0)) * emu_per_px))
    frame.margin_bottom = Emu(int(float(padding.get("bottom", 0)) * emu_per_px))
    frame.vertical_anchor = MSO_ANCHOR.MIDDLE if item.get("valign") == "middle" else MSO_ANCHOR.TOP
    alignment = _ALIGN.get(str(item.get("align") or ""), PP_ALIGN.LEFT)
    letter_spacing_px = float(item.get("letterSpacing") or 0)
    opacity = float(item.get("opacity", 1.0))

    paragraph = frame.paragraphs[0]
    paragraph.alignment = alignment
    if line_height_px > 0:
        paragraph.line_spacing = Pt(line_height_px * pt_per_px)
    for run_spec in runs:
        text = str(run_spec.get("text") or "")
        if not text:
            continue
        pieces = text.split("\n")
        for piece_index, piece in enumerate(pieces):
            if piece_index > 0:
                paragraph = frame.add_paragraph()
                paragraph.alignment = alignment
                if line_height_px > 0:
                    paragraph.line_spacing = Pt(line_height_px * pt_per_px)
            if not piece:
                continue
            run = paragraph.add_run()
            run.text = piece
            font = run.font
            font.name = pick_font(run_spec.get("font"), mono=bool(run_spec.get("mono")))
            size_px = float(run_spec.get("size") or 0)
            if size_px > 0:
                font.size = Pt(round(size_px * pt_per_px, 1))
            font.bold = int(run_spec.get("weight") or 400) >= 600
            font.italic = bool(run_spec.get("italic"))
            if run_spec.get("underline"):
                font.underline = True
            color = _parse_css_color(run_spec.get("color"))
            rgb = blend(color, background, opacity)
            if rgb is not None:
                font.color.rgb = _rgb(rgb)
            stroke = run_spec.get("stroke")
            if isinstance(stroke, dict):
                _set_run_outline(run, stroke, background, opacity, pt_per_px, filled=rgb is not None)
            if letter_spacing_px:
                # ``spc`` is in hundredths of a point; python-pptx has no API for it.
                run.font._element.set("spc", str(int(letter_spacing_px * pt_per_px * 100)))


def _set_run_outline(run, stroke: dict, background, opacity: float, pt_per_px: float, *, filled: bool) -> None:
    """Outline a run (``-webkit-text-stroke``) with an ``a:ln`` on its properties.

    python-pptx has no API for a run outline; the element goes first in
    ``a:rPr`` (schema order: ``ln``, fill, effects, ``latin``...). A stroke with
    no fill colour — the ghost section number — is a hollow glyph, which is
    ``a:noFill`` rather than the default (inherited, solid) text colour.
    """
    from lxml import etree
    from pptx.oxml.ns import qn

    line_rgb = blend(stroke, background, opacity)
    if line_rgb is None:
        return
    rpr = run.font._element
    for existing in rpr.findall(qn("a:ln")):
        rpr.remove(existing)
    width_pt = max(0.25, float(stroke.get("width", 1.0)) * pt_per_px)
    ln = etree.Element(qn("a:ln"), w=str(int(width_pt * 12_700)))
    solid = etree.SubElement(ln, qn("a:solidFill"))
    etree.SubElement(solid, qn("a:srgbClr"), val=str(_rgb(line_rgb)))
    rpr.insert(0, ln)
    if not filled:
        rpr.get_or_change_to_noFill()


_CSS_COLOR_RE = re.compile(r"rgba?\(([^)]+)\)")
_CSS_SRGB_RE = re.compile(r"color\(srgb\s+([^)]+)\)")


def _parse_css_color(value: object) -> dict | None:
    """A computed CSS colour → ``{r,g,b,a}``.

    The extractor normally hands over the parsed object already; the string
    forms are kept for ``rgb(a)`` and for ``color(srgb r g b / a)`` — what
    Chromium computes a ``color-mix()`` tint to, with 0..1 channels.
    """
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    srgb = False
    match = _CSS_COLOR_RE.search(value)
    if not match:
        match = _CSS_SRGB_RE.search(value)
        srgb = True
    if not match:
        return None
    parts = [p for p in re.split(r"[\s,/]+", match.group(1).strip()) if p]
    if len(parts) < 3:
        return None

    def channel(part: str) -> float:
        # → 0..255. A percentage is a fraction of the full range in both forms.
        if part.endswith("%"):
            return float(part[:-1]) * 2.55
        return float(part) * (255.0 if srgb else 1.0)

    def alpha_of(part: str) -> float:
        # ``rgb(0 0 0 / 50%)`` and ``rgba(0, 0, 0, 0.5)`` both occur.
        return float(part[:-1]) / 100.0 if part.endswith("%") else float(part)

    try:
        r, g, b = (channel(p) for p in parts[:3])
        alpha = alpha_of(parts[3]) if len(parts) > 3 else 1.0
    except ValueError:
        return None
    return {"r": r, "g": g, "b": b, "a": alpha}


#: The largest movie an editable deck embeds; a larger video keeps its poster.
MAX_EMBEDDED_MOVIE_BYTES = 100 * 1024 * 1024


def _add_movie(
    slide: Any, item: dict[str, Any], geometry: tuple[int, int, float, float], poster: bytes, movie: tuple[bytes, str]
) -> bool:
    """A video block as a native PowerPoint movie, its poster as the preview frame.

    Returns whether it was written; a refused movie (python-pptx's own checks)
    falls back to the poster picture.
    """
    _slide_w, _slide_h, emu_per_px, _pt = geometry
    data, mime_type = movie
    x, y, w, h = (float(item.get(k, 0)) for k in ("x", "y", "w", "h"))
    try:
        slide.shapes.add_movie(
            io.BytesIO(data),
            Emu(int(x * emu_per_px)),
            Emu(int(y * emu_per_px)),
            Emu(int(w * emu_per_px)),
            Emu(int(h * emu_per_px)),
            poster_frame_image=io.BytesIO(poster),
            mime_type=mime_type or "video/mp4",
        )
    except Exception:  # a movie PowerPoint cannot take keeps its poster
        logger.debug("PPTX export: movie %s kept as its poster", item.get("video"), exc_info=True)
        return False
    return True


def _add_image(
    slide: Any,
    item: dict[str, Any],
    geometry: tuple[int, int, float, float],
    captures: dict[str, bytes],
    movies: dict[str, tuple[bytes, str]] | None = None,
) -> None:
    _slide_w, _slide_h, emu_per_px, _pt = geometry
    png = captures.get(str(item.get("id") or ""))
    if not png:
        return
    x, y, w, h = (float(item.get(k, 0)) for k in ("x", "y", "w", "h"))
    if w <= 0 or h <= 0:
        return
    movie = (movies or {}).get(str(item.get("video") or ""))
    if movie is not None and _add_movie(slide, item, geometry, png, movie):
        return
    picture = slide.shapes.add_picture(
        io.BytesIO(png),
        Emu(int(x * emu_per_px)),
        Emu(int(y * emu_per_px)),
        width=Emu(int(w * emu_per_px)),
        height=Emu(int(h * emu_per_px)),
    )
    # The `<img>` alt text the page announced is the picture's
    # description: PowerPoint reads it as the picture's alt text.
    alt = item.get("alt")
    if isinstance(alt, str) and alt.strip():
        picture._element.nvPicPr.cNvPr.set("descr", alt.strip())


# ── Native charts ─────────────────────────────────────────────────────
#
# A `chart` primitive is the figure AS DATA (series, categories, format, the
# value axis the viewer settled on, resolved colours) plus the boxes Chromium
# gave its legend, its canvas and its inner plot. PowerPoint draws the chart
# itself - editable series, a legend of its own, data labels in the viewer's
# number format - inside a graphic frame placed on the frame the figure took,
# with the plot area and the legend pinned by manual layout to where the
# viewer drew them, so the exported slide reads like the screen and the
# numbers behind it are the deck's, not a picture of them.

_CHART_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"

#: Gap between category groups as a percentage of a bar's width, and the
#: overlap of the bars of one group (a stack overlaps fully). The viewer's
#: bars (`barCategoryGap` 24% a side for a lone series, 12% for a group, with
#: `barGap={6}`) read about like this on a slide; a stack takes 76% of its
#: band there (`BAR_CATEGORY_GAP_DENSE` in `chart-layout.ts`), which is
#: a gap of 24 / 76 of its width - room for the values inside its segments.
_BAR_GAP_WIDTH = 60
_BAR_GAP_WIDTH_STACKED = 32
_BAR_OVERLAP_CLUSTERED = -8
_BAR_OVERLAP_STACKED = 100
#: PowerPoint's bounds on a bar plot's ``c:gapWidth`` and ``c:overlap`` (percent of a bar's width).
_GAP_WIDTH_RANGE = (0, 500)
_OVERLAP_RANGE = (-100, 100)
#: The ``value_label_fit`` plans whose values past the bars the viewer places
#: point by point (``ValueLabelFit`` in ``presentation_spec.py``).
_FITTED_VALUES = frozenset({"stagger", "upright"})
#: A data label turned on its side, as the viewer turns a bar's value that is
#: wider than its bar (``a:bodyPr/@rot``, 60000ths of a degree).
_LABEL_ROTATION_UPRIGHT = "-5400000"
#: The viewer's area fill opacities (`ChartCanvas.tsx`).
_AREA_FILL_OPACITY = 0.22
_AREA_FILL_OPACITY_STACKED = 0.55
#: The donut's ring (`innerRadius = radius * 0.62`), as PowerPoint's hole size.
_DONUT_HOLE_PERCENT = 62
#: A line's dots: r = 6 canvas px in the viewer, so a 12px marker.
_LINE_MARKER_PX = 12
#: Canvas px above the right end of the target line for its name.
_TARGET_LABEL_HEIGHT_PX = 30
_TARGET_LABEL_WIDTH_PX = 320
#: Extra width given to the legend box around the entries the viewer measured.
_LEGEND_SLACK_RATIO = 0.12
#: And per line series: PowerPoint keys a line with a stroke and its marker,
#: wider than the viewer's square swatch. An entry that does not fit the box
#: is dropped from the legend, not wrapped.
_LEGEND_LINE_KEY_PX = 32
#: Extra height (px) an axis chart's frame runs past the viewer's canvas.
_AXIS_FRAME_SLACK_PX = 48
#: The viewer's target line and waterfall connectors (`ChartCanvas.tsx`), canvas px.
_TARGET_STROKE_PX = 3
_CONNECTOR_STROKE_PX = 2
#: The series a waterfall's steps stand on, and the part of a step below zero.
_WATERFALL_BASE_NAME = "Base"
_WATERFALL_BELOW_SUFFIX = "(below zero)"
#: A tick glyph's advance as a share of its size, and the room between a
#: category label and the plot (``GLYPH_EM`` / ``AXIS_PADDING`` in
#: ``chart-layout.ts``): the column a butterfly's categories take on
#: PowerPoint's left axis when the viewer wrote them on its spine.
_TICK_GLYPH_EM = 0.58
_CATEGORY_AXIS_PADDING_PX = 16
#: The order a chart's plots are written in, which is the order PowerPoint
#: paints them: bars under lines, as the viewer draws them.
_PLOT_ORDER: tuple[tuple[str, str], ...] = (
    ("bar", "primary"),
    ("bar", "secondary"),
    ("line", "primary"),
    ("line", "secondary"),
)
#: Ids of the right-hand value axis and of the hidden category axis it crosses.
_SECONDARY_CATEGORY_AXIS_ID = "500500001"
_SECONDARY_VALUE_AXIS_ID = "500500002"


def _chart_type(chart: dict):
    """The PowerPoint chart type of a native chart payload, or ``None``. A
    combo is built as a column chart; its lines become plots of their own."""
    from pptx.enum.chart import XL_CHART_TYPE

    kind = chart.get("kind")
    horizontal = bool(chart.get("horizontal"))
    stacked = bool(chart.get("stacked")) or kind == "stacked_bar"
    if kind == "waterfall":
        return XL_CHART_TYPE.COLUMN_STACKED
    if kind == "butterfly":
        return XL_CHART_TYPE.BAR_CLUSTERED
    if kind in ("bar", "stacked_bar", "combo"):
        if horizontal:
            return XL_CHART_TYPE.BAR_STACKED if stacked else XL_CHART_TYPE.BAR_CLUSTERED
        return XL_CHART_TYPE.COLUMN_STACKED if stacked else XL_CHART_TYPE.COLUMN_CLUSTERED
    if kind == "line":
        return XL_CHART_TYPE.LINE_MARKERS
    if kind == "area":
        return XL_CHART_TYPE.AREA_STACKED if stacked else XL_CHART_TYPE.AREA
    if kind == "pie":
        return XL_CHART_TYPE.PIE
    if kind == "donut":
        return XL_CHART_TYPE.DOUGHNUT
    return None


def _number(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _c(tag: str):
    return f"{{{_CHART_NS}}}{tag}"


def _a(tag: str):
    return f"{{{_DRAWING_NS}}}{tag}"


def _insert_after(parent, new_child, *predecessors: str) -> None:
    """Insert *new_child* right after the last present element among
    *predecessors* (schema order), or first when none is present."""
    from lxml import etree

    index = 0
    for position, child in enumerate(parent):
        if etree.QName(child).localname in predecessors:
            index = position + 1
    parent.insert(index, new_child)


def _no_fill_no_line():
    """An ``a:spPr``-shaped element (built as ``c:spPr``) painting nothing."""
    from lxml import etree

    sp_pr = etree.Element(_c("spPr"))
    etree.SubElement(sp_pr, _a("noFill"))
    ln = etree.SubElement(sp_pr, _a("ln"))
    etree.SubElement(ln, _a("noFill"))
    return sp_pr


def _manual_layout(element, box: dict, frame: dict, *, inner: bool = False) -> None:
    """Pin a chart part (legend, plot area) to *box*, a canvas-px rectangle,
    as fractions of the graphic *frame* - ``c:layout/c:manualLayout`` with
    edge-mode x / y and factor-mode w / h, built by hand in schema order
    (python-pptx exposes the layout element but not its children)."""
    from lxml import etree

    if frame["w"] <= 0 or frame["h"] <= 0:
        return
    get_or_add = getattr(element, "get_or_add_layout", None)
    if callable(get_or_add):
        # The legend knows where its layout goes (after `c:legendPos`).
        layout = get_or_add()
    else:
        # The plot area's layout is its first child.
        layout = element.find(_c("layout"))
        if layout is None:
            layout = etree.Element(_c("layout"))
            element.insert(0, layout)
    for existing in layout.findall(_c("manualLayout")):
        layout.remove(existing)
    manual = etree.SubElement(layout, _c("manualLayout"))
    if inner:
        etree.SubElement(manual, _c("layoutTarget"), val="inner")
    etree.SubElement(manual, _c("xMode"), val="edge")
    etree.SubElement(manual, _c("yMode"), val="edge")
    x = max(0.0, min(1.0, (box["x"] - frame["x"]) / frame["w"]))
    y = max(0.0, min(1.0, (box["y"] - frame["y"]) / frame["h"]))
    w = max(0.0, min(1.0 - x, box["w"] / frame["w"]))
    h = max(0.0, min(1.0 - y, box["h"] / frame["h"]))
    for tag, value in (("x", x), ("y", y), ("w", w), ("h", h)):
        etree.SubElement(manual, _c(tag), val=f"{value:.4f}")


def _box(item: dict | None) -> dict | None:
    if not isinstance(item, dict):
        return None
    box = {k: _number(item.get(k)) for k in ("x", "y", "w", "h")}
    return box if box["w"] > 0 and box["h"] > 0 else None


def _style_rgb(style: dict, key: str, background: tuple[int, int, int], fallback: tuple[int, int, int] | None):
    rgb = blend(_parse_css_color(style.get(key)), background)
    return rgb if rgb is not None else fallback


def _axis_ticks(axis: dict) -> list[float]:
    lo, hi, step = _number(axis.get("min")), _number(axis.get("max")), _number(axis.get("step"))
    if step <= 0 or hi <= lo:
        return [lo, hi]
    ticks: list[float] = []
    value = lo
    while value <= hi + step * 1e-6 and len(ticks) < 64:
        ticks.append(round(value, 10))
        value += step
    return ticks


def _waterfall_columns(steps: Sequence[dict]) -> tuple[list[float], list[float], list[float]]:
    """The steps of a waterfall (each the span ``start`` -> ``end`` the viewer
    drew) as the three series of a stacked column chart: the invisible base
    under each bar, the bar, and the part of a bar below zero when it crosses
    the axis - PowerPoint stacks positive values up from zero and negative
    ones down from it, so one bar across zero is two segments."""
    base: list[float] = []
    bar: list[float] = []
    below: list[float] = []
    for step in steps:
        low, high = sorted((_number(step.get("start")), _number(step.get("end"))))
        if low >= 0:
            base.append(low)
            bar.append(high - low)
            below.append(0.0)
        elif high <= 0:
            base.append(high)
            bar.append(low - high)
            below.append(0.0)
        else:
            base.append(0.0)
            bar.append(high)
            below.append(low)
    return base, bar, below


def _plot_groups(chart: dict, series: list[dict]) -> list[tuple[str, str, list[int]]]:
    """The plots an axis chart is written as: ``(drawn kind, value axis,
    series positions)``, one per way a series is drawn and axis it reads
    against, in paint order."""
    from app.services.chart_export import drawn_kind, series_axis

    buckets: dict[tuple[str, str], list[int]] = {}
    for index, entry in enumerate(series):
        buckets.setdefault((drawn_kind(chart, entry), series_axis(entry)), []).append(index)
    ordered = [(*key, buckets.pop(key)) for key in _PLOT_ORDER if key in buckets]
    return ordered + [(*key, indices) for key, indices in buckets.items()]


def _set_child_val(parent, tag: str, value: str) -> None:
    child = parent.find(_c(tag))
    if child is not None:
        child.set("val", value)


def _split_plots(pptx_chart, data, groups: list[tuple[str, str, list[int]]]) -> None:
    """Rewrite a chart python-pptx built as ONE plot of every series as one
    plot per group: a combo's bars and its lines, and the series read
    against the right-hand axis on plots of their own, crossing a second
    value axis. Each series keeps its ``c:idx`` - its column of the embedded
    workbook - whichever plot it lands in."""
    import copy

    from lxml import etree
    from pptx.chart.xmlwriter import ChartXmlWriter
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.oxml import parse_xml

    plot_area = pptx_chart._chartSpace.chart.plotArea
    built = next(iter(plot_area.iter_xCharts()))
    templates = {"line" if etree.QName(built).localname == "lineChart" else "bar": built}
    if "line" not in templates and any(drawn == "line" for drawn, _axis, _indices in groups):
        # python-pptx's own line plot of the same data: its series carry the
        # line-only children (markers, smoothing) and the same workbook refs.
        line_space = parse_xml(ChartXmlWriter(XL_CHART_TYPE.LINE_MARKERS, data).xml.encode("utf-8"))
        templates["line"] = line_space.find(f".//{_c('lineChart')}")
    primary_ids = [axis_id.get("val") for axis_id in built.findall(_c("axId"))]
    secondary_ids = [_SECONDARY_CATEGORY_AXIS_ID, _SECONDARY_VALUE_AXIS_ID]
    position = list(plot_area).index(built)
    plot_area.remove(built)
    for offset, (drawn, axis, indices) in enumerate(groups):
        plot = copy.deepcopy(templates[drawn])
        for ser in plot.findall(_c("ser")):
            if int(ser.find(_c("idx")).get("val")) not in indices:
                plot.remove(ser)
        ids = secondary_ids if axis == "secondary" else primary_ids
        for axis_id, value in zip(plot.findall(_c("axId")), ids, strict=True):
            axis_id.set("val", value)
        plot_area.insert(position + offset, plot)
    if any(axis == "secondary" for _drawn, axis, _indices in groups):
        _add_secondary_axes(plot_area)


def _add_secondary_axes(plot_area) -> None:
    """The right-hand value axis and the hidden category axis it crosses at
    its far end - copies of the primary pair, so both value axes meet their
    categories the same way."""
    import copy

    category = plot_area.find(_c("catAx"))
    value = plot_area.find(_c("valAx"))
    second_category = copy.deepcopy(category)
    second_value = copy.deepcopy(value)
    _set_child_val(second_category, "axId", _SECONDARY_CATEGORY_AXIS_ID)
    _set_child_val(second_category, "delete", "1")
    _set_child_val(second_category, "crossAx", _SECONDARY_VALUE_AXIS_ID)
    _set_child_val(second_value, "axId", _SECONDARY_VALUE_AXIS_ID)
    _set_child_val(second_value, "axPos", "r")
    _set_child_val(second_value, "crossAx", _SECONDARY_CATEGORY_AXIS_ID)
    _set_child_val(second_value, "crosses", "max")
    for gridlines in second_value.findall(_c("majorGridlines")):
        second_value.remove(gridlines)
    index = list(plot_area).index(value)
    plot_area.insert(index + 1, second_category)
    plot_area.insert(index + 2, second_value)


def _value_axes(pptx_chart):
    """The left-hand and the right-hand (or ``None``) value axes of a chart.

    Never ``Chart.value_axis``: with two ``c:valAx`` it answers the second,
    the Y axis of an XY chart, which here is the right-hand one."""
    from pptx.chart.axis import ValueAxis

    primary = secondary = None
    for value_axis in pptx_chart._chartSpace.chart.plotArea.findall(_c("valAx")):
        axis_id = value_axis.find(_c("axId"))
        if axis_id is not None and axis_id.get("val") == _SECONDARY_VALUE_AXIS_ID:
            secondary = ValueAxis(value_axis)
        elif primary is None:
            primary = ValueAxis(value_axis)
    return primary, secondary


def _style_value_axis(value_axis, axis: dict | None, number_format: str, text) -> None:
    """A value axis on the viewer's ticks (min / max / step) with no tick
    marks and no axis line, its labels in *text* (``(font, size, rgb)``)."""
    from pptx.enum.chart import XL_TICK_MARK

    if axis and _axis_ticks(axis):
        value_axis.minimum_scale = _number(axis.get("min"))
        value_axis.maximum_scale = _number(axis.get("max"))
        step = _number(axis.get("step"))
        if step > 0:
            value_axis.major_unit = step
    font_name, font_size, rgb = text
    value_axis.format.line.fill.background()
    value_axis.major_tick_mark = XL_TICK_MARK.NONE
    value_axis.tick_labels.font.name = font_name
    value_axis.tick_labels.font.size = font_size
    value_axis.tick_labels.font.color.rgb = _rgb(rgb)
    value_axis.tick_labels.number_format = number_format
    value_axis.tick_labels.number_format_is_linked = False


def _add_chart(
    slide, item: dict, geometry, background: tuple[int, int, int], *, slide_number: int, export_log: list[str]
) -> None:
    """One native ``chart`` primitive -> an editable PowerPoint chart.

    A chart whose series are drawn more than one way (a combo's bars and
    lines) or read against two value axes is one plot per group
    (``_plot_groups``), the right-hand axis on the viewer's own ticks.

    A butterfly is a clustered bar chart whose bars overlap fully, its
    left series stored negative and every label and tick written without the
    sign (``mirrored_number_format``); its categories go on the left axis,
    which the log says when the viewer wrote them on its spine."""
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import (
        XL_LABEL_POSITION,
        XL_LEGEND_POSITION,
        XL_MARKER_STYLE,
        XL_TICK_LABEL_POSITION,
        XL_TICK_MARK,
    )
    from pptx.enum.dml import MSO_LINE_DASH_STYLE

    from app.services.chart_export import (
        EMULATED_CHART_KINDS,
        MIRRORED_CHART_KINDS,
        axis_number_format,
        axis_view,
        chart_tick_px,
        chart_value_px,
        drawn_kind,
        emulation_note,
        label_number_format,
        mirrored_number_format,
        series_values,
        spine_note,
    )

    _slide_w, _slide_h, emu_per_px, pt_per_px = geometry
    chart: dict = item.get("chart") if isinstance(item.get("chart"), dict) else {}
    frame = _box(item)
    chart_type = _chart_type(chart)
    categories = [str(c) for c in chart.get("categories") or []]
    series = [s for s in (chart.get("series") or []) if isinstance(s, dict)]
    kind = str(chart.get("kind"))
    waterfall = kind == "waterfall"
    # A waterfall's steps are the spans the viewer drew, one per category.
    steps = [s for s in (chart.get("steps") or []) if isinstance(s, dict)]
    unreadable = waterfall and len(steps) != len(categories)
    if chart_type is None or frame is None or not categories or not series or unreadable:
        export_log.append(f"Slide {slide_number}: a chart could not be read from the rendered page and was left out.")
        return
    stacked = bool(chart.get("stacked")) or kind == "stacked_bar"
    # A butterfly's bars run sideways from its spine, its two sides on one row.
    mirrored = kind in MIRRORED_CHART_KINDS
    spine_categories = mirrored and chart.get("spine") != "left"
    horizontal = bool(chart.get("horizontal")) or mirrored
    single = kind in ("pie", "donut")
    if not single:
        # PowerPoint sets its bottom axis labels lower than the viewer draws
        # its ticks and clips them to the frame: the (transparent) frame runs
        # a little past the canvas so the last row of labels is never cut. A
        # horizontal chart's category labels sit right-aligned against the
        # axis, so room added on the LEFT keeps them where they are instead
        # of wrapping them in the viewer's tight tick column.
        slide_bottom_px = _slide_h / emu_per_px
        frame = {**frame, "h": max(frame["h"], min(frame["h"] + _AXIS_FRAME_SLACK_PX, slide_bottom_px - frame["y"]))}
        if horizontal:
            left = max(0.0, frame["x"] - _AXIS_FRAME_SLACK_PX)
            frame = {**frame, "x": left, "w": frame["w"] + (frame["x"] - left)}
    style = item.get("style") if isinstance(item.get("style"), dict) else {}
    opacity = _number(item.get("opacity"), 1.0)
    # The surface the chart sits on (a panel, the slide): what alpha blends onto.
    surface = blend(_parse_css_color(style.get("background")), background) or background
    text_rgb = _style_rgb(style, "tick", surface, (255, 255, 255) if sum(surface) < 384 else (0, 0, 0))
    font_name = pick_font(style.get("font"))
    # The tick font as the page measured it (`label_size` sets it); the
    # chart's own scale where the page published none.
    tick_px = _number(style.get("size"), chart_tick_px(chart) * _number(style.get("fit"), 1.0))
    font_size = Pt(round(max(6.0, tick_px * pt_per_px), 1))
    # The values where the page measured none: the size the chart tries them
    # at first (`value_label_size`), else the ticks'.
    value_px = chart_value_px(chart)
    value_font_size = (
        font_size if value_px is None else Pt(round(max(6.0, value_px * _number(style.get("fit"), 1.0) * pt_per_px), 1))
    )

    data = CategoryChartData()
    data.categories = categories
    below_zero = False
    if waterfall:
        plotted = series[:1]
        base, bar, below = _waterfall_columns(steps)
        name = str(series[0].get("name") or "")[:255]
        data.add_series(_WATERFALL_BASE_NAME, base)
        data.add_series(name, bar)
        below_zero = any(below)
        if below_zero:
            data.add_series(f"{name} {_WATERFALL_BELOW_SUFFIX}"[:255], below)
    else:
        plotted = series[:1] if single else series
        for position, entry in enumerate(plotted):
            values = [_number(v) for v in (entry.get("values") or [])]
            values = (values + [0.0] * len(categories))[: len(categories)]
            if mirrored and position == 0:
                # The left side runs leftwards from zero: stored negative, written unsigned.
                values = [-abs(value) if value else 0.0 for value in values]
            data.add_series(str(entry.get("name") or "")[:255], values)

    graphic_frame = slide.shapes.add_chart(
        chart_type,
        Emu(int(frame["x"] * emu_per_px)),
        Emu(int(frame["y"] * emu_per_px)),
        Emu(int(frame["w"] * emu_per_px)),
        Emu(int(frame["h"] * emu_per_px)),
        data,
    )
    pptx_chart = graphic_frame.chart
    # A waterfall is one plot of its own making: base, bar, below zero.
    groups = [] if single or waterfall else _plot_groups(chart, plotted)
    if len(groups) > 1:
        _split_plots(pptx_chart, data, groups)
    two_axes = any(axis == "secondary" for _drawn, axis, _indices in groups)
    chart_space = pptx_chart._chartSpace
    pptx_chart.has_title = False
    pptx_chart.font.name = font_name
    pptx_chart.font.size = font_size
    pptx_chart.font.color.rgb = _rgb(text_rgb)
    # The frame paints nothing of its own: no fill, no border, square corners
    # (PowerPoint rounds a chart frame unless told not to), a transparent plot.
    from lxml import etree

    rounded = chart_space.find(_c("roundedCorners"))
    if rounded is None:
        _insert_after(chart_space, etree.Element(_c("roundedCorners"), val="0"), "date1904", "lang")
    else:
        rounded.set("val", "0")
    _insert_after(chart_space, _no_fill_no_line(), "chart")
    plot_area = chart_space.chart.plotArea
    plot_area.append(_no_fill_no_line())

    # Legend: PowerPoint's own, on the box the viewer's legend took.
    legend_box = _box(item.get("legend"))
    show_legend = bool(chart.get("legend")) and legend_box is not None
    pptx_chart.has_legend = show_legend
    if show_legend:
        assert legend_box is not None  # noqa: S101 - narrowed by show_legend
        legend = pptx_chart.legend
        # The side the slide put its legend on (`legend_position`):
        # PowerPoint's legend goes to the same side, on the box the viewer gave
        # it. Unset is the historical top legend, with its one fallback: a
        # legend the viewer wrapped (a long one in a narrow panel) is stacked,
        # one row per entry - PowerPoint cannot be given the viewer's wrap, and
        # a top legend it cannot fit in its box is laid out anew, over the
        # panel title the frame covers.
        asked = chart.get("legend_position")
        asked = asked if asked in LEGEND_POSITIONS else None
        rows = max(1, int(_number(item.get("legend_rows"), 1)))
        stacked_legend = asked is None and rows > 1
        if asked == "bottom":
            legend.position = XL_LEGEND_POSITION.BOTTOM
        elif asked == "right" or stacked_legend:
            legend.position = XL_LEGEND_POSITION.RIGHT
        else:
            legend.position = XL_LEGEND_POSITION.TOP
        legend.include_in_layout = False
        legend.font.name = font_name
        # The legend is the viewer's own text, not the chart's ticks: its own size.
        legend.font.size = _label_font_size(style.get("legend_size"), pt_per_px, font_size)
        legend.font.color.rgb = _rgb(_style_rgb(style, "legend", surface, text_rgb))
        if stacked_legend:
            entries = 1 if single else len(plotted)
            legend_box = {**legend_box, "h": max(legend_box["h"], legend_box["h"] / rows * entries)}
        # A hair wider than the entries measured: PowerPoint's glyph advances
        # run wider than Chromium's and would otherwise wrap the last entry,
        # and its line keys are wider than the viewer's swatches. The box grows
        # into the room the frame has on its right, the rest to the left - a
        # dashboard panel's legend ends at the frame's right edge. A bottom
        # legend is centred under the plot, so it grows both ways alike.
        line_keys = sum(1 for entry in plotted if drawn_kind(chart, entry) == "line") if not single else 0
        slack = legend_box["w"] * _LEGEND_SLACK_RATIO + line_keys * _LEGEND_LINE_KEY_PX
        room_right = max(0.0, frame["x"] + frame["w"] - (legend_box["x"] + legend_box["w"]))
        grow_right = min(slack / 2, room_right)
        if asked == "bottom":
            left = max(frame["x"], legend_box["x"] - slack / 2)
        else:
            left = max(frame["x"], legend_box["x"] - (slack - grow_right))
        right = legend_box["x"] + legend_box["w"] + grow_right
        _manual_layout(legend._element, {**legend_box, "x": left, "w": right - left}, frame)
        if waterfall:
            # The viewer's legend names the one series; the base and the
            # below-zero part are how PowerPoint draws it, not entries.
            _hide_legend_entries(legend._element, [0, 2] if below_zero else [0])

    # The plot on the viewer's inner plot box (the grid, or the pie itself).
    inner = _box(item.get("inner"))
    if inner is not None and spine_categories:
        # The categories the viewer wrote on a butterfly's spine go on
        # PowerPoint's left axis, outside the plot: the plot gives them its left.
        longest = max((len(category) for category in categories), default=0)
        column = min(inner["w"] / 2, longest * tick_px * _TICK_GLYPH_EM + _CATEGORY_AXIS_PADDING_PX)
        inner = {**inner, "x": inner["x"] + column, "w": inner["w"] - column}
    if inner is not None:
        _manual_layout(plot_area, inner, frame, inner=True)

    plots = list(pptx_chart.plots)
    plot = plots[0]
    # What each plot holds: its drawn kind, the axis it reads against and its
    # series (one group, every series, when the chart is a single plot).
    plan = list(zip(plots, groups, strict=True)) if len(groups) == len(plots) else []
    colors = [blend(_parse_css_color(c), surface, opacity) for c in item.get("colors") or []]
    slice_colors = [blend(_parse_css_color(c), surface, opacity) for c in item.get("slice_colors") or []]
    line_px = Pt(max(0.75, 3 * pt_per_px))

    if single:
        plot.vary_by_categories = True
        first = plot.series[0]
        for index in range(len(categories)):
            point = first.points[index]
            rgb = slice_colors[index] if index < len(slice_colors) else None
            if rgb is not None:
                point.format.fill.solid()
                point.format.fill.fore_color.rgb = _rgb(rgb)
            point.format.line.color.rgb = _rgb(surface)
            point.format.line.width = line_px
        if kind == "donut":
            doughnut = plot._element
            hole = doughnut.find(_c("holeSize"))
            if hole is None:
                hole = etree.SubElement(doughnut, _c("holeSize"))
            hole.set("val", str(_DONUT_HOLE_PERCENT))
    else:
        if waterfall:
            # Said explicitly: with `c:varyColors` absent PowerPoint colours a lone
            # series by category and lists the categories in the legend.
            plot.vary_by_categories = False
            # A waterfall is one bar per band, like a lone series in the viewer.
            plot.gap_width = _BAR_GAP_WIDTH_STACKED if stacked else _BAR_GAP_WIDTH
            plot.overlap = _BAR_OVERLAP_STACKED
            step_colors = [blend(_parse_css_color(c), surface, opacity) for c in item.get("step_colors") or []]
            for index, pptx_series in enumerate(plot.series):
                # The base paints nothing; each bar wears its step's colour
                # (an increase, a decrease, a total, a gap).
                pptx_series.format.line.fill.background()
                if index == 0:
                    pptx_series.format.fill.background()
                    continue
                for point_index, rgb in enumerate(step_colors[: len(categories)]):
                    if rgb is None:
                        continue
                    point = pptx_series.points[point_index]
                    point.format.fill.solid()
                    point.format.fill.fore_color.rgb = _rgb(rgb)
        # Bars the viewer fitted their values around (`value_label_fit`)
        # - that gave way to them, or a group of upright bars it kept
        # tight - are spaced as it drew them, not as its default reads.
        # A butterfly's two sides share each row, as a stack's segments do.
        one_row = stacked or mirrored
        spacing = _bar_spacing(item.get("bar_spacing"), one_row)
        for each, (drawn, _axis, _indices) in plan:
            # Said explicitly: with `c:varyColors` absent PowerPoint colours a lone
            # series by category and lists the categories in the legend.
            each.vary_by_categories = False
            if drawn == "bar" and spacing is not None:
                each.gap_width, each.overlap = spacing
            elif drawn == "bar":
                each.gap_width = _BAR_GAP_WIDTH_STACKED if one_row else _BAR_GAP_WIDTH
                each.overlap = _BAR_OVERLAP_STACKED if one_row else _BAR_OVERLAP_CLUSTERED
            for pptx_series in each.series:
                # The series' position in the payload: its `c:idx`, kept by every plot.
                index = pptx_series.index
                rgb = colors[index] if index < len(colors) else None
                if rgb is None:
                    continue
                if drawn == "line":
                    pptx_series.format.line.color.rgb = _rgb(rgb)
                    pptx_series.format.line.width = Pt(max(1.0, 4 * pt_per_px))
                    pptx_series.smooth = True
                    marker = pptx_series.marker
                    marker.style = XL_MARKER_STYLE.CIRCLE
                    marker.size = max(2, min(72, round(_LINE_MARKER_PX * pt_per_px)))
                    marker.format.fill.solid()
                    marker.format.fill.fore_color.rgb = _rgb(rgb)
                    marker.format.line.color.rgb = _rgb(surface)
                    marker.format.line.width = Pt(max(0.5, 2 * pt_per_px))
                elif drawn == "area":
                    fill_alpha = _AREA_FILL_OPACITY_STACKED if stacked else _AREA_FILL_OPACITY
                    fill_rgb = blend({"r": rgb[0], "g": rgb[1], "b": rgb[2], "a": fill_alpha}, surface) or rgb
                    pptx_series.format.fill.solid()
                    pptx_series.format.fill.fore_color.rgb = _rgb(fill_rgb)
                    pptx_series.format.line.color.rgb = _rgb(rgb)
                    pptx_series.format.line.width = line_px
                else:
                    pptx_series.format.fill.solid()
                    pptx_series.format.fill.fore_color.rgb = _rgb(rgb)
                    pptx_series.format.line.fill.background()
                    if mirrored:
                        # Left of the spine is a side, not a loss: PowerPoint
                        # fills a negative bar white unless told otherwise.
                        pptx_series.invert_if_negative = False

        # Axes: the viewer's ticks (min / max / step), its dashed grid, its
        # number format; no tick marks, no value-axis line. A two-unit chart's
        # axes each write their own unit, in the colour the viewer gave them.
        def axis_indices(side: str) -> list[int]:
            return [index for _drawn, axis_name, indices in groups if axis_name == side for index in indices]

        axis = item.get("axis") if isinstance(item.get("axis"), dict) else None
        value_axis, secondary_value_axis = _value_axes(pptx_chart)
        category_axis = pptx_chart.category_axis
        ticks = _axis_ticks(axis) if axis else []
        primary_values = ticks or series_values(chart, axis_indices("primary") if two_axes else None)
        _style_value_axis(
            value_axis,
            axis,
            mirrored_number_format(chart, primary_values, axis=True)
            if mirrored
            else axis_number_format(axis_view(chart, "primary"), primary_values, with_unit=two_axes),
            (font_name, font_size, _style_rgb(style, "primary_tick", surface, text_rgb) if two_axes else text_rgb),
        )
        value_axis.has_major_gridlines = True
        grid_rgb = _style_rgb(style, "grid", surface, None)
        gridlines = value_axis.major_gridlines.format.line
        if grid_rgb is not None:
            gridlines.color.rgb = _rgb(grid_rgb)
        gridlines.width = Pt(0.75)
        gridlines.dash_style = MSO_LINE_DASH_STYLE.DASH
        if secondary_value_axis is not None:
            secondary = item.get("secondary_axis") if isinstance(item.get("secondary_axis"), dict) else None
            secondary_ticks = _axis_ticks(secondary) if secondary else []
            _style_value_axis(
                secondary_value_axis,
                secondary,
                axis_number_format(
                    axis_view(chart, "secondary"),
                    secondary_ticks or series_values(chart, axis_indices("secondary")),
                    with_unit=True,
                ),
                (font_name, font_size, _style_rgb(style, "secondary_tick", surface, text_rgb)),
            )
            secondary_value_axis.has_major_gridlines = False
        category_axis.major_tick_mark = XL_TICK_MARK.NONE
        category_axis.tick_label_position = XL_TICK_LABEL_POSITION.LOW
        category_axis.tick_labels.font.name = font_name
        category_axis.tick_labels.font.size = font_size
        category_axis.tick_labels.font.color.rgb = _rgb(text_rgb)
        axis_rgb = _style_rgb(style, "axis", surface, None)
        if axis_rgb is not None and not horizontal:
            category_axis.format.line.color.rgb = _rgb(axis_rgb)
        else:
            category_axis.format.line.fill.background()
        if horizontal:
            # The viewer lists the categories top-down; PowerPoint's bar chart
            # puts the first one at the bottom. Reverse the category axis and
            # keep the value axis at the bottom: `c:valAx/c:crosses` is where
            # the VALUE axis crosses the category axis (python-pptx's
            # `value_axis.crosses` writes the category axis' element instead).
            category_axis.reverse_order = True
            crosses = value_axis._element.find(_c("crosses"))
            if crosses is not None:
                crosses.set("val", "max")

    # Value labels: PowerPoint's own where the semantics match (one label per
    # plotted value, in the viewer's number format, where the viewer put it,
    # at the size it was set in and turned on its side when it was).
    # A stacked chart's TOTALS have no native equivalent, a doughnut can only
    # label its ring where the viewer labels outside it, and a waterfall's
    # bars would be labelled with their lengths, not the signed changes the
    # viewer wrote: those are placed as the text the viewer wrote, at the
    # positions it measured. The values the viewer wrote INSIDE a stack's
    # segments are PowerPoint's own labels, centred, on exactly those segments;
    # a thin segment's value it wrote PAST the stack is its text, in
    # the series' colour, with its leader's legs drawn as connectors.
    # A combo's lines carry theirs where the viewer wrote them - the
    # page says it wrote some with their size, whether the bars' values are
    # written or not - and each plot writes its values in the format of the
    # axis it reads against. A series whose own `show_values` is off
    # keeps no label, and a line's or an area's values the viewer moved clear
    # of something, or did not write, are placed point by point like a
    # combo line's - every series' when the viewer wrote them alternating
    # over and under their points (`values_alternate`). A bar's values
    # the viewer fitted (`values_fit`) are labelled point by point:
    # the ones written a row out raised as far, the ones left out deleted.
    show_values = bool(item.get("values"))
    points_written = kind == "combo" and (show_values or item.get("point_size") is not None)
    values_alternate = bool(item.get("values_alternate"))
    values_fitted = item.get("values_fit") in _FITTED_VALUES
    label_rgb = _style_rgb(style, "label", surface, text_rgb)
    label_font_size = _label_font_size(item.get("label_size"), pt_per_px, value_font_size)
    point_font_size = _label_font_size(item.get("point_size"), pt_per_px, label_font_size)
    measured = [label for label in item.get("labels") or [] if isinstance(label, dict)]
    quiet = {index for index, entry in enumerate(series) if entry.get("show_values") is False}
    if (show_values or points_written) and not stacked and kind not in ("donut", "waterfall"):
        if single:
            labelled: list[tuple[object, str, str, list[int] | None]] = [(plot, kind, "primary", None)]
        else:
            labelled = [(each, drawn, axis_name, indices) for each, (drawn, axis_name, indices) in plan]
        for each, drawn, axis_name, indices in labelled:
            number_format = (
                mirrored_number_format(chart, series_values(chart, indices))
                if mirrored
                else label_number_format(axis_view(chart, axis_name), series_values(chart, indices))
            )
            if kind == "combo" and drawn == "line":
                if points_written:
                    _add_point_labels(
                        each.series, number_format, measured, len(categories), font_name, point_font_size, label_rgb
                    )
                continue
            if not show_values:
                continue
            each.has_data_labels = True
            labels = each.data_labels
            labels.number_format = number_format
            labels.number_format_is_linked = False
            labels.show_value = True
            labels.show_category_name = False
            labels.show_series_name = False
            labels.show_percentage = False
            labels.font.name = font_name
            labels.font.size = label_font_size
            labels.font.bold = True
            labels.font.color.rgb = _rgb(label_rgb)
            if item.get("label_rotate") and drawn == "bar":
                _turn_labels_on_their_side(labels._element)
            # An area chart accepts no label position (PowerPoint repairs the file).
            if drawn == "line":
                labels.position = XL_LABEL_POSITION.ABOVE
            elif drawn != "area":
                labels.position = XL_LABEL_POSITION.OUTSIDE_END
            if single:
                _place_slice_labels(
                    plot,
                    str(item.get("values_placement") or ""),
                    measured,
                    len(categories),
                    slice_colors,
                    (font_name, label_font_size, label_rgb),
                )
                continue
            for pptx_series in each.series:
                if pptx_series.index in quiet:
                    _delete_series_labels(pptx_series)
                elif mirrored and pptx_series.index == 0:
                    _place_mirrored_labels(
                        pptx_series,
                        number_format,
                        (font_name, label_font_size, label_rgb),
                        measured,
                        list(series[0].get("values") or []),
                        (
                            item.get("axis") if isinstance(item.get("axis"), dict) else None,
                            inner["w"] if inner is not None else 0.0,
                        ),
                        colors[0] if colors else None,
                    )
            if drawn == "bar" and values_fitted:
                _add_fitted_bar_labels(
                    [pptx_series for pptx_series in each.series if pptx_series.index not in quiet],
                    series,
                    number_format,
                    measured,
                    len(categories),
                    (font_name, label_font_size, label_rgb),
                    (frame["w"], frame["h"]),
                    horizontal=horizontal,
                )
            if drawn in ("line", "area"):
                moved = [
                    pptx_series
                    for pptx_series in each.series
                    if pptx_series.index not in quiet
                    and (values_alternate or _points_moved(pptx_series.index, measured, len(categories)))
                ]
                if moved:
                    _add_point_labels(
                        moved,
                        number_format,
                        measured,
                        len(categories),
                        font_name,
                        label_font_size,
                        label_rgb,
                        positioned=drawn == "line",
                    )
        # A value the viewer raised or slid is the text it wrote, where it wrote
        # it; one under an area's point too (an area's labels take no position).
        for label in measured:
            placement = label.get("placement")
            index = label.get("series")
            on_area = (
                isinstance(index, (int, float))
                and 0 <= int(index) < len(series)
                and drawn_kind(chart, series[int(index)]) == "area"
            )
            if placement in _PLACED_AS_TEXT or (placement == "below" and on_area):
                _add_chart_label(slide, label, geometry, font_name, point_font_size, label_rgb)
    elif show_values or stacked:
        # A thin segment's value written past its stack: its leader,
        # leg by leg, then the text, both in the series' colour, the text at
        # the size the page published - drawn wherever the viewer drew them.
        # A donut's value outside its ring keeps its leader, from the rim at
        # the slice's angle; one inside its slice is inked from the slice, as
        # the viewer inks it.
        annotation_font_size = _label_font_size(item.get("annotation_size"), pt_per_px, label_font_size)
        for leg in item.get("leaders") or []:
            line = _box(leg) if isinstance(leg, dict) else None
            if line is not None:
                rgb = _series_rgb(colors, leg.get("series"), label_rgb)
                _add_rule(slide, line, geometry, rgb, min(line["w"], line["h"]), MSO_LINE_DASH_STYLE.SOLID)
        leader_rgb = _style_rgb(style, "leader", surface, label_rgb)
        for leader in item.get("slice_leaders") or []:
            if isinstance(leader, dict):
                _add_slice_leader(slide, leader, geometry, leader_rgb)
        for label in measured:
            role = label.get("role")
            if role == "annotation":
                rgb = _series_rgb(colors, label.get("series"), label_rgb)
                _add_chart_label(slide, label, geometry, font_name, annotation_font_size, rgb)
            elif role == "segment" and single:
                rgb = _slice_ink(slice_colors, label.get("category"), label_rgb)
                _add_chart_label(slide, label, geometry, font_name, label_font_size, rgb)
            elif role != "segment":
                _add_chart_label(slide, label, geometry, font_name, label_font_size, label_rgb)
    if stacked and kind in ("bar", "stacked_bar"):
        segment_font_size = _label_font_size(item.get("segment_size"), pt_per_px, label_font_size)
        _add_segment_labels(plot, chart, measured, len(categories), colors, font_name, segment_font_size, label_rgb)

    font = (font_name, font_size)
    target_line = _box(item.get("target_line")) if chart.get("target") is not None else None
    if target_line is not None:
        rgb = _style_rgb(style, "target", surface, text_rgb)
        _add_target_line(slide, chart.get("target_name"), target_line, geometry, rgb, font, MSO_LINE_DASH_STYLE.DASH)
    secondary_line = (
        _box(item.get("secondary_target_line")) if two_axes and chart.get("secondary_target") is not None else None
    )
    if secondary_line is not None:
        rgb = _style_rgb(style, "secondary_target", surface, text_rgb)
        name = chart.get("secondary_target_name")
        _add_target_line(slide, name, secondary_line, geometry, rgb, font, MSO_LINE_DASH_STYLE.ROUND_DOT)
    if waterfall:
        connector_rgb = _style_rgb(style, "connector", surface, text_rgb)
        for connector in item.get("connectors") or []:
            line = _box(connector)
            if line is not None:
                _add_rule(slide, line, geometry, connector_rgb, _CONNECTOR_STROKE_PX)
    if kind in EMULATED_CHART_KINDS:
        export_log.append(emulation_note(slide_number, kind))
    if spine_categories:
        export_log.append(spine_note(slide_number, kind))


def _hide_legend_entries(legend, indices: Sequence[int]) -> None:
    """Leave the series at *indices* out of a chart legend (``c:legendEntry``
    + ``c:delete``, in schema order right after ``c:legendPos``)."""
    from lxml import etree

    for index in indices:
        entry = etree.Element(_c("legendEntry"))
        etree.SubElement(entry, _c("idx"), val=str(index))
        etree.SubElement(entry, _c("delete"), val="1")
        _insert_after(legend, entry, "legendPos", "legendEntry")


def _series_rgb(colors: list, index: object, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    """The resolved colour of the series at *index* (a label's or a leader's ``series``), *fallback* when unknown."""
    if isinstance(index, (int, float)) and 0 <= int(index) < len(colors) and colors[int(index)] is not None:
        return colors[int(index)]
    return fallback


def _label_font_size(size_px: object, pt_per_px: float, fallback):
    """The point size of a run of values the viewer set at *size_px* canvas px (fit included)."""
    size = _number(size_px)
    return Pt(round(max(6.0, size * pt_per_px), 1)) if size > 0 else fallback


def _turn_labels_on_their_side(d_lbls) -> None:
    """Data labels read bottom to top, as the viewer turns a value wider than its bar."""
    body = d_lbls.get_or_add_txPr().find(_a("bodyPr"))
    if body is not None:
        body.set("rot", _LABEL_ROTATION_UPRIGHT)
        body.set("vert", "horz")


#: The viewer inks a value inside a segment from the segment's OKLCH
#: lightness (`.deck-chart-value[data-role="segment"]` in presentation.css):
#: white-ish below the threshold, near-black above it.
_SEGMENT_INK_THRESHOLD = 0.62
_SEGMENT_INK_LIGHT = 0.99
_SEGMENT_INK_DARK = 0.18


def _oklab_lightness(rgb: tuple[int, int, int]) -> float:
    def linear(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(v) for v in rgb)
    long = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    medium = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    short = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return 0.2104542553 * long + 0.7936177850 * medium - 0.0040720468 * short


def _achromatic(lightness: float) -> tuple[int, int, int]:
    """The sRGB grey of OKLCH lightness *lightness* (``oklch(L 0 0)``)."""
    y = lightness**3
    encoded = 12.92 * y if y <= 0.0031308 else 1.055 * y ** (1 / 2.4) - 0.055
    value = round(max(0.0, min(1.0, encoded)) * 255)
    return (value, value, value)


def _segment_ink(fill: tuple[int, int, int]) -> tuple[int, int, int]:
    """The ink of a value written inside a segment of colour *fill*, as the viewer inks it."""
    light = _oklab_lightness(fill) < _SEGMENT_INK_THRESHOLD
    return _achromatic(_SEGMENT_INK_LIGHT if light else _SEGMENT_INK_DARK)


def _slice_ink(slice_colors: list, category: object, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    """The ink of a value written inside the slice at *category*, as the
    viewer inks one inside a segment; *fallback* when the slice's colour is unknown."""
    if isinstance(category, (int, float)) and 0 <= int(category) < len(slice_colors):
        fill = slice_colors[int(category)]
        if fill is not None:
            return _segment_ink(fill)
    return fallback


def _place_slice_labels(
    plot,
    placement: str,
    measured: list[dict],
    category_count: int,
    slice_colors: list,
    font: tuple[str, object, tuple[int, int, int]],
) -> None:
    """A pie's values where the viewer wrote them: PowerPoint's own
    labels at the size the viewer set them in, outside the slices with their
    leader lines on, or centred inside them in the ink the viewer gives a value
    on its slice (``_slice_ink``) - and none on a slice the viewer wrote none
    on. A page that measured no value at all says nothing either way: the
    plot's labels stand, outside.

    A point's ink and a deleted point are ``c:dLbl`` children of the SERIES'
    labels, which PowerPoint reads over the plot's; a point's ``c:dLbl``
    inherits nothing from them, so their number format and text properties
    are copied in."""
    from copy import deepcopy

    from lxml import etree
    from pptx.enum.chart import XL_LABEL_POSITION

    font_name, font_size, rgb = font
    written = {
        int(label["category"])
        for label in measured
        if label.get("role") in ("value", "segment") and isinstance(label.get("category"), (int, float))
    }
    inside = placement == "inside" and bool(written)
    plot_labels = plot.data_labels
    plot_labels.position = XL_LABEL_POSITION.CENTER if inside else XL_LABEL_POSITION.OUTSIDE_END
    if not inside:
        _show_leader_lines(plot_labels._element)
    if not written or (not inside and len(written) >= category_count):
        return
    series = plot.series[0]
    labels = series.data_labels
    labels.number_format = plot_labels.number_format
    labels.number_format_is_linked = False
    labels.show_value = True
    labels.show_category_name = False
    labels.show_series_name = False
    labels.show_percentage = False
    labels.position = XL_LABEL_POSITION.CENTER if inside else XL_LABEL_POSITION.OUTSIDE_END
    labels.font.name = font_name
    labels.font.size = font_size
    labels.font.bold = True
    labels.font.color.rgb = _rgb(rgb)
    series_labels = labels._element
    if not inside:
        _show_leader_lines(series_labels)
    for category in range(category_count):
        if category in written and not inside:
            continue
        d_lbl = series_labels.get_or_add_dLbl_for_point(category)
        for child in list(d_lbl):
            if child.tag != _c("idx"):
                d_lbl.remove(child)
        if category not in written:
            etree.SubElement(d_lbl, _c("delete"), val="1")
            continue
        for tag in ("numFmt", "spPr", "txPr"):
            source = series_labels.find(_c(tag))
            if source is not None:
                d_lbl.append(deepcopy(source))
        etree.SubElement(d_lbl, _c("dLblPos"), val="ctr")
        for tag, flag in _POINT_LABEL_FLAGS:
            etree.SubElement(d_lbl, _c(tag), val=flag)
        series.points[category].data_label.font.color.rgb = _rgb(_slice_ink(slice_colors, category, rgb))


def _show_leader_lines(d_lbls) -> None:
    """Leader lines on a pie's labels (``c:showLeaderLines``), which python-pptx does not model."""
    from lxml import etree

    flag = d_lbls.find(_c("showLeaderLines"))
    if flag is None:
        flag = etree.Element(_c("showLeaderLines"))
        d_lbls.insert_element_before(flag, "c:leaderLines", "c:extLst")
    flag.set("val", "1")


def _add_segment_labels(
    plot,
    chart: dict,
    measured: list[dict],
    category_count: int,
    colors: list,
    font_name: str,
    font_size,
    fallback_ink: tuple[int, int, int],
) -> None:
    """PowerPoint's own labels on the segments the viewer wrote a value inside.

    One centred label per segment of each series that held its value in the
    viewer (``role: segment`` labels, by series and category); every other
    point of the series has its label deleted (``c:dLbl/c:delete``), so a
    segment too thin for its value stays as clean as on screen. Inked as the
    viewer inks it, from the segment's own colour (``_segment_ink``).
    """
    from lxml import etree
    from pptx.enum.chart import XL_LABEL_POSITION

    from app.services.chart_export import label_number_format, series_values

    written: dict[int, set[int]] = {}
    for label in measured:
        if label.get("role") != "segment":
            continue
        series_index, category = label.get("series"), label.get("category")
        if isinstance(series_index, (int, float)) and isinstance(category, (int, float)):
            written.setdefault(int(series_index), set()).add(int(category))
    if not written:
        return
    number_format = label_number_format(chart, series_values(chart))
    for index, pptx_series in enumerate(plot.series):
        categories = written.get(index)
        if not categories:
            continue
        labels = pptx_series.data_labels
        labels.number_format = number_format
        labels.number_format_is_linked = False
        labels.show_value = True
        labels.show_category_name = False
        labels.show_series_name = False
        labels.show_percentage = False
        labels.position = XL_LABEL_POSITION.CENTER
        labels.font.name = font_name
        labels.font.size = font_size
        labels.font.bold = True
        fill = colors[index] if index < len(colors) else None
        labels.font.color.rgb = _rgb(fallback_ink if fill is None else _segment_ink(fill))
        for category in range(category_count):
            if category in categories:
                continue
            d_lbl = labels._element.get_or_add_dLbl_for_point(category)
            for child in list(d_lbl):
                if child.tag != _c("idx"):
                    d_lbl.remove(child)
            etree.SubElement(d_lbl, _c("delete"), val="1")


#: The children a point's own data label carries after the ones copied from
#: its series' labels, in ``c:dLbl`` schema order: the value alone.
_POINT_LABEL_FLAGS: tuple[tuple[str, str], ...] = (
    ("showLegendKey", "0"),
    ("showVal", "1"),
    ("showCatName", "0"),
    ("showSerName", "0"),
    ("showPercent", "0"),
    ("showBubbleSize", "0"),
)


#: Where the viewer moved a value on a point that PowerPoint's own label
#: cannot follow (``pointPlacementName`` in ``chart-layout.ts``): raised over
#: what was in its way, or slid clear of a value axis' ticks. Written as the
#: text the viewer placed.
_PLACED_AS_TEXT = frozenset({"lifted", "shifted"})


def _value_places(measured: list[dict]) -> dict[tuple[int, int], str]:
    """Where the viewer wrote each value on a point, by series and category:
    ``above`` its point unless the label says otherwise (``data-placement``)."""
    places: dict[tuple[int, int], str] = {}
    for label in measured:
        series_index, category = label.get("series"), label.get("category")
        if label.get("role") == "value" and isinstance(series_index, (int, float)) and isinstance(category, (int, float)):
            places[(int(series_index), int(category))] = str(label.get("placement") or "above")
    return places


def _points_moved(series_index: int, measured: list[dict], category_count: int) -> bool:
    """Whether the viewer wrote a value of a line's or an area's series anywhere
    but over its point, or left one out. A series the page measured no
    value of at all says nothing either way: PowerPoint keeps its own labels."""
    places = {category: place for (index, category), place in _value_places(measured).items() if index == series_index}
    if not places:
        return False
    return len(places) < category_count or any(place != "above" for place in places.values())


def _bar_spacing(spacing: object, stacked: bool) -> tuple[int, int] | None:
    """PowerPoint's ``(gap_width, overlap)`` for bars the viewer spaced itself
    (``bar_spacing``; a group of upright bars it kept tight):
    the room between two groups and between two bars of a group, each as a
    percent of a bar's width; a stack overlaps fully. ``None`` when the
    viewer published no spacing."""
    if not isinstance(spacing, dict):
        return None
    width = _number(spacing.get("width"))
    if width <= 0:
        return None
    gap_width = round(100 * max(0.0, _number(spacing.get("category_gap"))) / width)
    overlap = _BAR_OVERLAP_STACKED if stacked else -round(100 * max(0.0, _number(spacing.get("gap"))) / width)
    return (
        max(_GAP_WIDTH_RANGE[0], min(_GAP_WIDTH_RANGE[1], gap_width)),
        max(_OVERLAP_RANGE[0], min(_OVERLAP_RANGE[1], overlap)),
    )


#: How far the viewer writes a value past its bar (``LABEL_OFFSET`` in ``chart-layout.ts``), canvas px.
_LABEL_OFFSET_PX = 6


def _label_widths(measured: list[dict]) -> dict[tuple[int, int], float]:
    """The width (page px) of each value the viewer wrote past a bar, by ``(series, category)``."""
    widths: dict[tuple[int, int], float] = {}
    for label in measured:
        series_index, category = label.get("series"), label.get("category")
        if label.get("role") == "value" and isinstance(series_index, (int, float)) and isinstance(category, (int, float)):
            widths[(int(series_index), int(category))] = _number(label.get("w"))
    return widths


def _place_mirrored_labels(
    pptx_series,
    number_format: str,
    font: tuple[str, object, tuple[int, int, int]],
    measured: list[dict],
    values: list,
    reach: tuple[dict | None, float],
    fill: tuple[int, int, int] | None,
) -> None:
    """The labels of a butterfly's left side where the viewer writes
    them, as far as PowerPoint follows: outside each bar's outer end,
    leftwards. A value whose bar ends too near the plot's left edge for its
    label - PowerPoint writes the categories hard against that edge - needs
    a label of its own, written inside the end of its bar (``inEnd``), inked
    from the bar's colour as a stack's segment is (``_segment_ink``): a bar
    that long holds its label, and the axis stays the viewer's, both sides to
    one extent. A zero is never one: it has no bar, and the series' number
    format writes it as nothing (``mirrored_number_format``). ``reach`` is
    the viewer's axis and the plot's width (page px). The series then carries
    its own labels, which PowerPoint reads over the plot's, and each such
    point the series' number format and text properties, as a point's label
    inherits none."""
    from copy import deepcopy

    from lxml import etree
    from pptx.enum.chart import XL_LABEL_POSITION

    axis, plot_width = reach
    widths = _label_widths(measured)
    lo, hi = (_number(axis.get("min")), _number(axis.get("max"))) if axis else (0.0, 0.0)
    per_unit = plot_width / (hi - lo) if hi > lo and plot_width > 0 else 0.0
    if not per_unit:
        return
    inside: list[int] = []
    for category, value in enumerate(values):
        size = abs(_number(value))
        if size == 0 or (0, category) not in widths:
            continue
        need = widths[(0, category)] * (1 + _LEGEND_SLACK_RATIO) + 2 * _LABEL_OFFSET_PX
        if (abs(lo) - size) * per_unit < need:
            inside.append(category)
    if not inside:
        return
    font_name, font_size, rgb = font
    labels = pptx_series.data_labels
    labels.number_format = number_format
    labels.number_format_is_linked = False
    labels.show_value = True
    labels.show_category_name = False
    labels.show_series_name = False
    labels.show_percentage = False
    labels.position = XL_LABEL_POSITION.OUTSIDE_END
    labels.font.name = font_name
    labels.font.size = font_size
    labels.font.bold = True
    labels.font.color.rgb = _rgb(rgb)
    series_labels = labels._element
    ink = _segment_ink(fill) if fill is not None else rgb
    for category in inside:
        d_lbl = series_labels.get_or_add_dLbl_for_point(category)
        for child in list(d_lbl):
            if child.tag != _c("idx"):
                d_lbl.remove(child)
        for tag in ("numFmt", "spPr", "txPr"):
            source = series_labels.find(_c(tag))
            if source is not None:
                d_lbl.append(deepcopy(source))
        for color in d_lbl.iterfind(f"{_c('txPr')}//{_a('defRPr')}/{_a('solidFill')}/{_a('srgbClr')}"):
            color.set("val", str(_rgb(ink)))
        etree.SubElement(d_lbl, _c("dLblPos"), val="inEnd")
        for tag, flag in _POINT_LABEL_FLAGS:
            etree.SubElement(d_lbl, _c(tag), val=flag)


def _add_fitted_bar_labels(
    series,
    chart_series: list[dict],
    number_format: str,
    measured: list[dict],
    category_count: int,
    font: tuple[str, object, tuple[int, int, int]],
    chart_size: tuple[float, float],
    *,
    horizontal: bool = False,
) -> None:
    """A bar's values as the viewer fitted them (``value_label_fit``):
    PowerPoint's own labels, outside the end of each bar, at the size the
    viewer set them in. A value it wrote a row out (``lift``, page px) has a
    point label of its own moved that far from its place - ``c:layout`` /
    ``c:manualLayout``, a share of the chart's height, the offset PowerPoint
    records for a label dragged by hand; on a horizontal chart the
    lift runs along the value's row, a share of the chart's width - and one
    it left out, or that has no bar (a zero), keeps none. A point's
    ``c:dLbl`` inherits nothing from its series' labels, so their number
    format and text properties are copied in."""
    from copy import deepcopy

    from lxml import etree
    from pptx.enum.chart import XL_LABEL_POSITION

    font_name, font_size, rgb = font
    lifts: dict[tuple[int, int], float] = {}
    for label in measured:
        series_index, category = label.get("series"), label.get("category")
        if label.get("role") == "value" and isinstance(series_index, (int, float)) and isinstance(category, (int, float)):
            lifts[(int(series_index), int(category))] = _number(label.get("lift"))
    width = max(1.0, _number(chart_size[0], 1.0))
    height = max(1.0, _number(chart_size[1], 1.0))
    for pptx_series in series:
        labels = pptx_series.data_labels
        labels.number_format = number_format
        labels.number_format_is_linked = False
        labels.show_value = True
        labels.show_category_name = False
        labels.show_series_name = False
        labels.show_percentage = False
        labels.position = XL_LABEL_POSITION.OUTSIDE_END
        labels.font.name = font_name
        labels.font.size = font_size
        labels.font.bold = True
        labels.font.color.rgb = _rgb(rgb)
        series_labels = labels._element
        entry = chart_series[pptx_series.index] if pptx_series.index < len(chart_series) else {}
        values = (entry.get("values") if isinstance(entry, dict) else None) or []
        for category in range(category_count):
            key = (pptx_series.index, category)
            lift = lifts.get(key)
            if lift is not None and lift <= 0:
                continue
            d_lbl = series_labels.get_or_add_dLbl_for_point(category)
            for child in list(d_lbl):
                if child.tag != _c("idx"):
                    d_lbl.remove(child)
            if lift is None:
                etree.SubElement(d_lbl, _c("delete"), val="1")
                continue
            # Past the end of its bar: up over a rise, down under a fall - on
            # a horizontal chart right of a rise, left of a fall.
            value = _number(values[category]) if category < len(values) else 0.0
            if horizontal:
                x, y = (-lift if value < 0 else lift) / width, 0.0
            else:
                x, y = 0.0, (lift if value < 0 else -lift) / height
            layout = etree.SubElement(etree.SubElement(d_lbl, _c("layout")), _c("manualLayout"))
            etree.SubElement(layout, _c("x"), val="0" if x == 0 else f"{x:.6f}")
            etree.SubElement(layout, _c("y"), val="0" if y == 0 else f"{y:.6f}")
            for tag in ("numFmt", "spPr", "txPr"):
                source = series_labels.find(_c(tag))
                if source is not None:
                    d_lbl.append(deepcopy(source))
            etree.SubElement(d_lbl, _c("dLblPos"), val="outEnd")
            for tag, flag in _POINT_LABEL_FLAGS:
                etree.SubElement(d_lbl, _c(tag), val=flag)


def _delete_series_labels(pptx_series) -> None:
    """No label on any point of a series (``c:dLbls`` with ``c:delete``): its
    own ``show_values`` is off while the chart writes the other series'."""
    from lxml import etree

    d_lbls = pptx_series._element.get_or_add_dLbls()
    for child in list(d_lbls):
        d_lbls.remove(child)
    etree.SubElement(d_lbls, _c("delete"), val="1")


def _add_point_labels(
    series,
    number_format: str,
    measured: list[dict],
    category_count: int,
    font_name: str,
    font_size,
    rgb: tuple[int, int, int],
    *,
    positioned: bool = True,
) -> None:
    """Values on points (a combo's line; a line's or an area's the
    viewer moved, or wrote alternating over and under their points) as
    PowerPoint's own labels, where the viewer wrote them: over each point, and
    under one the viewer moved clear of what holds the room over it or wrote
    under it by turns - a point's own ``c:dLbl``, which inherits nothing from
    its series' labels, so their number format and text properties are copied in.
    A point the viewer raised or slid (``_PLACED_AS_TEXT``) is the text it
    wrote (``_add_chart_label``), and one it wrote no value for keeps none:
    both have their label deleted. An area's labels take no position
    (``positioned`` false): a value under its point is the text written too."""
    from copy import deepcopy

    from lxml import etree
    from pptx.enum.chart import XL_LABEL_POSITION

    places = _value_places(measured)
    for pptx_series in series:
        labels = pptx_series.data_labels
        labels.number_format = number_format
        labels.number_format_is_linked = False
        labels.show_value = True
        labels.show_category_name = False
        labels.show_series_name = False
        labels.show_percentage = False
        if positioned:
            labels.position = XL_LABEL_POSITION.ABOVE
        labels.font.name = font_name
        labels.font.size = font_size
        labels.font.bold = True
        labels.font.color.rgb = _rgb(rgb)
        series_labels = labels._element
        for category in range(category_count):
            place = places.get((pptx_series.index, category))
            if place == "above":
                continue
            d_lbl = series_labels.get_or_add_dLbl_for_point(category)
            for child in list(d_lbl):
                if child.tag != _c("idx"):
                    d_lbl.remove(child)
            if place != "below" or not positioned:
                etree.SubElement(d_lbl, _c("delete"), val="1")
                continue
            for tag in ("numFmt", "spPr", "txPr"):
                source = series_labels.find(_c(tag))
                if source is not None:
                    d_lbl.append(deepcopy(source))
            etree.SubElement(d_lbl, _c("dLblPos"), val="b")
            for tag, value in _POINT_LABEL_FLAGS:
                etree.SubElement(d_lbl, _c(tag), val=value)


#: The end of a value's text the viewer pinned (SVG ``text-anchor``), and the
#: paragraph alignment that keeps it pinned in a text box that does not wrap:
#: a run PowerPoint sets wider than Chromium grows away from that end.
_LABEL_ANCHOR_ALIGNMENT: dict[str, PP_ALIGN] = {"start": PP_ALIGN.LEFT, "end": PP_ALIGN.RIGHT}


def _add_chart_label(slide, label: dict, geometry, font_name: str, font_size, rgb: tuple[int, int, int]) -> None:
    """A value the viewer wrote on the plot (a stack's total), as a text box - turned
    on its side when the viewer turned it, around the same centre.

    The box is pinned where the viewer pinned the text (``anchor``): a
    total written past the end of a horizontal bar starts at the bar and reads
    rightward, so it is a left-aligned box on the measured run - whatever width
    PowerPoint's glyphs add runs into the room the viewer kept inside the frame,
    never back over the bar's end; a value written back toward the axis keeps
    its right edge. A centred value (an upright chart's) grows both ways, a hair
    wider than measured. A turned value keeps its centre.
    """
    box = _box(label)
    text = str(label.get("text") or "").strip() if isinstance(label, dict) else ""
    if box is None or not text:
        return
    _slide_w, _slide_h, emu_per_px, _pt = geometry
    slack = 6.0
    rotated = bool(label.get("rotate"))
    alignment = None if rotated else _LABEL_ANCHOR_ALIGNMENT.get(str(label.get("anchor") or ""))
    if rotated:
        # The measured box is the turned text's: the shape is laid out upright
        # (its width the text's length) and turned about its centre.
        cx, cy = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2
        box = {"x": cx - box["h"] / 2, "y": cy - box["w"] / 2, "w": box["h"], "h": box["w"]}
    if alignment is None:
        alignment = PP_ALIGN.CENTER
        box = {**box, "x": box["x"] - slack / 2, "w": box["w"] + slack}
    shape = slide.shapes.add_textbox(
        Emu(int(box["x"] * emu_per_px)),
        Emu(int(box["y"] * emu_per_px)),
        Emu(int(box["w"] * emu_per_px)),
        Emu(int(box["h"] * emu_per_px)),
    )
    if rotated:
        shape.rotation = 270.0
    frame = shape.text_frame
    frame.word_wrap = False
    frame.auto_size = MSO_AUTO_SIZE.NONE
    for side in ("margin_left", "margin_right", "margin_top", "margin_bottom"):
        setattr(frame, side, Emu(0))
    frame.vertical_anchor = MSO_ANCHOR.MIDDLE
    paragraph = frame.paragraphs[0]
    paragraph.alignment = alignment
    run = paragraph.add_run()
    run.text = text
    run.font.name = font_name
    run.font.size = font_size
    run.font.bold = True
    run.font.color.rgb = _rgb(rgb)


def _add_rule(slide, line: dict, geometry, rgb: tuple[int, int, int], stroke_px: float, dash_style=None):
    """A straight connector along the centre of *line*, a stroke box in canvas
    px (the extractor gives an SVG line its stroke width as thickness), dashed
    unless *dash_style* says otherwise. Returns its two ends and whether it
    runs vertically."""
    from pptx.enum.dml import MSO_LINE_DASH_STYLE
    from pptx.enum.shapes import MSO_CONNECTOR

    _slide_w, _slide_h, emu_per_px, pt_per_px = geometry
    vertical = line["h"] > line["w"]
    if vertical:
        x1 = x2 = line["x"] + line["w"] / 2
        y1, y2 = line["y"], line["y"] + line["h"]
    else:
        y1 = y2 = line["y"] + line["h"] / 2
        x1, x2 = line["x"], line["x"] + line["w"]
    connector = slide.shapes.add_connector(
        MSO_CONNECTOR.STRAIGHT,
        Emu(int(x1 * emu_per_px)),
        Emu(int(y1 * emu_per_px)),
        Emu(int(x2 * emu_per_px)),
        Emu(int(y2 * emu_per_px)),
    )
    connector.line.color.rgb = _rgb(rgb)
    connector.line.width = Pt(max(0.75, stroke_px * pt_per_px))
    connector.line.dash_style = MSO_LINE_DASH_STYLE.DASH if dash_style is None else dash_style
    return x1, y1, x2, y2, vertical


def _add_slice_leader(slide, leader: dict, geometry, rgb: tuple[int, int, int]) -> None:
    """A donut's leader from its rim to a value written outside it: a
    straight connector between the two ends the page measured (slide px), at
    the slice's angle and in its stroke."""
    from pptx.enum.dml import MSO_LINE_DASH_STYLE
    from pptx.enum.shapes import MSO_CONNECTOR

    ends = [_number(leader.get(key), float("nan")) for key in ("x1", "y1", "x2", "y2")]
    if not all(math.isfinite(end) for end in ends):
        return
    _slide_w, _slide_h, emu_per_px, pt_per_px = geometry
    x1, y1, x2, y2 = (Emu(int(end * emu_per_px)) for end in ends)
    connector = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, x1, y1, x2, y2)
    connector.line.color.rgb = _rgb(rgb)
    connector.line.width = Pt(max(0.75, _number(leader.get("w"), 2.0) * pt_per_px))
    connector.line.dash_style = MSO_LINE_DASH_STYLE.SOLID


def _add_target_line(
    slide, name: object, line: dict, geometry, rgb: tuple[int, int, int], font: tuple[str, object], dash_style
) -> None:
    """A target line where the viewer drew it, in *rgb* and *dash_style* (the
    right-hand axis' target is dotted), named above its end in *font*
    (``(name, size)``).

    The viewer names the target in its legend; a PowerPoint legend lists
    series only, so the name sits by the line instead.
    """
    _slide_w, _slide_h, emu_per_px, _pt = geometry
    font_name, font_size = font
    x1, y1, x2, _y2, vertical = _add_rule(slide, line, geometry, rgb, _TARGET_STROKE_PX, dash_style)
    name = str(name or "").strip()
    if not name:
        return
    if vertical:
        label_x, label_y = x1 + 8, y1
        align = PP_ALIGN.LEFT
    else:
        label_x, label_y = x2 - _TARGET_LABEL_WIDTH_PX, y1 - _TARGET_LABEL_HEIGHT_PX
        align = PP_ALIGN.RIGHT
    shape = slide.shapes.add_textbox(
        Emu(int(label_x * emu_per_px)),
        Emu(int(label_y * emu_per_px)),
        Emu(int(_TARGET_LABEL_WIDTH_PX * emu_per_px)),
        Emu(int(_TARGET_LABEL_HEIGHT_PX * emu_per_px)),
    )
    frame = shape.text_frame
    frame.word_wrap = False
    frame.auto_size = MSO_AUTO_SIZE.NONE
    for side in ("margin_left", "margin_right", "margin_top", "margin_bottom"):
        setattr(frame, side, Emu(0))
    frame.vertical_anchor = MSO_ANCHOR.BOTTOM
    paragraph = frame.paragraphs[0]
    paragraph.alignment = align
    run = paragraph.add_run()
    run.text = name
    run.font.name = font_name
    run.font.size = font_size
    run.font.color.rgb = _rgb(rgb)


def _add_primitive(
    target,
    item: dict,
    geometry,
    background: tuple[int, int, int],
    captures: dict[str, bytes],
    log: list[str],
    *,
    slide_number: int,
    movies: dict[str, tuple[bytes, str]] | None = None,
) -> None:
    """Write one measured primitive onto *target* (the slide, or a group shape on it)."""
    from app.services.chart_export import picture_note

    kind = item.get("kind")
    if kind == "rect":
        _add_rect(target, item, geometry, background)
    elif kind == "oval":
        _add_oval(target, item, geometry, background)
    elif kind == "connector":
        _add_connector(target, item, geometry, background)
    elif kind == "text":
        _add_text(target, item, geometry, background)
    elif kind == "image":
        chart_kind = item.get("chart")
        if isinstance(chart_kind, str) and chart_kind:
            reason = item.get("chart_reason")
            log.append(picture_note(slide_number, chart_kind, reason if isinstance(reason, str) else None))
        _add_image(target, item, geometry, captures, movies)
    elif kind == "chart":
        _add_chart(target, item, geometry, background, slide_number=slide_number, export_log=log)


def _grouped_runs(items: Sequence[dict]) -> list[list[dict]]:
    """*items* in paint order, consecutive members of one render group joined.

    The extractor tags every primitive drawn inside a ``[data-export-group]``
    element with that group's id; the DOM walk is depth-first, so a group's
    primitives are consecutive. An untagged item is a run of its own.
    """
    runs: list[list[dict]] = []
    for item in items:
        group_id = item.get("group")
        if group_id and runs and runs[-1][0].get("group") == group_id:
            runs[-1].append(item)
        else:
            runs.append([item])
    return runs


def _subgroup_runs(items: Sequence[dict]) -> list[list[dict]]:
    """*items* of one group, consecutive members of one ``subgroup`` (an org chart's seat) joined."""
    runs: list[list[dict]] = []
    for item in items:
        subgroup = item.get("subgroup")
        if subgroup and runs and runs[-1][0].get("subgroup") == subgroup:
            runs[-1].append(item)
        else:
            runs.append([item])
    return runs


def _covers_slide(item: dict, canvas: tuple[int, int]) -> bool:
    return (
        float(item.get("x", 0)) <= 0.5
        and float(item.get("y", 0)) <= 0.5
        and float(item.get("w", 0)) >= canvas[0] - 1
        and float(item.get("h", 0)) >= canvas[1] - 1
    )


def build_native_pptx(
    slides: Sequence[dict],
    *,
    captures: dict[str, bytes],
    aspect_ratio: str,
    notes: Sequence[str | None] = (),
    title: str = "",
    export_log: list[str] | None = None,
    transition: str = "none",
    slide_links: Sequence[Sequence[dict]] = (),
    movies: dict[str, tuple[bytes, str]] | None = None,
) -> bytes:
    """Editable deck from the measured primitives of every slide.

    *slides* is the list ``EXTRACT_PRIMITIVES_JS`` returned (one entry per
    slide with ``background`` and ``items`` in paint order); *captures* maps an
    ``image`` item's ``id`` to the PNG screenshot of that element. A ``chart``
    item becomes a native PowerPoint chart; a picture standing in for a chart
    PowerPoint has no equivalent of (an ``image`` tagged ``chart``) is noted in
    *export_log*, the lines the export surfaces to the person as its log. The
    consecutive items of one render ``group`` (a logo wall card) are
    written into one PowerPoint group shape. *slide_links* puts a click
    target over each slide link (:func:`add_slide_link_hotspots`), above the
    slide's shapes. *movies* maps a video block's file id to its bytes and MIME
    type: the poster captured for it becomes a native movie with that poster as
    its preview frame; a video missing from it keeps its poster picture.
    """
    if not slides:
        raise ValueError("no slides to write")
    log = export_log if export_log is not None else []
    prs = _new_presentation(aspect_ratio, title)
    geometry = _slide_geometry(aspect_ratio)
    canvas = CANVAS_SIZES_PX.get(aspect_ratio, CANVAS_SIZES_PX["16:9"])

    for index, measured in enumerate(slides):
        slide = prs.slides.add_slide(prs.slide_layouts[_BLANK_LAYOUT])
        items = [i for i in (measured.get("items") or []) if isinstance(i, dict)]
        background = blend(measured.get("background"), (0, 0, 0)) or (0, 0, 0)
        # A rectangle painting the whole slide IS the background (a slide-level
        # fill or background image); fold it in rather than stacking a shape.
        remaining: list[dict] = []
        for item in items:
            if item.get("kind") == "rect" and _covers_slide(item, canvas):
                folded = blend(item.get("fill"), background, item.get("opacity", 1.0))
                if folded is not None:
                    background = folded
                continue
            remaining.append(item)
        fill = slide.background.fill
        fill.solid()
        fill.fore_color.rgb = _rgb(background)

        for run in _grouped_runs(remaining):
            target = slide
            if run[0].get("group"):
                # A render group (a logo wall card; an org chart): the
                # primitives drawn inside it - its box, its header, its logo
                # pictures - become ONE PowerPoint group shape, so the card
                # moves as one.
                target = slide.shapes.add_group_shape()
            for members in _subgroup_runs(run):
                # A seat of an org chart: its shape, its words and its badge,
                # one group inside the chart's.
                inner = target.shapes.add_group_shape() if members[0].get("subgroup") and target is not slide else target
                for item in members:
                    _add_primitive(
                        inner, item, geometry, background, captures, log, slide_number=index + 1, movies=movies
                    )
                if inner is not target and len(inner.shapes) == 0:
                    inner._element.getparent().remove(inner._element)
            if target is not slide and len(target.shapes) == 0:
                target._element.getparent().remove(target._element)
        _set_transition(slide, transition)
        _set_notes(slide, notes[index] if index < len(notes) else None)
    add_slide_link_hotspots(prs, slide_links, aspect_ratio)

    out = io.BytesIO()
    prs.save(out)
    return out.getvalue()
