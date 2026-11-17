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

"""A presentation template's design tokens (``extra_metadata.design``).

A template may declare how flat its blocks are drawn and the radius and
spacing scale its slides use. The declaration is OPT-IN: a template that
declares nothing renders exactly as before (shadows, card light, eyebrows,
the renderer's own radius and margins), so the THREAD Conference Deck, the
XTM One Brand Deck, every user template and the video templates keep their
look. The seeded Filigran Brand Deck opts in (spec "PPT Design System",
section 3: no shadow or light effect, no eyebrow, solid strokes, radius
12 / 8 / 4 / 0, an 8 px spacing grid on the 1920 x 1080 canvas).

Shape (every key optional)::

    {
      "shadows": false,              # no drop shadow, inner shadow or text shadow
      "glow": false,                 # no glow, halo, blur or card light
      "eyebrows": false,             # no small label above a title
      "gradient_strokes": "never",   # strokes, bars, rules and axes in one colour
                                     # (the pricing tier and a highlighted callout
                                     # keep their gradient border)
      "radius": {"card": 12, "inner": 8, "chip": 4, "table": 0},
      "spacing": {"margin_x": 96, "margin_top": 72, "footer": 56,
                  "title_body": 40, "title_subtitle": 12, "tile_gap": 24,
                  "block_gap": 32, "block_gap_max": 48, "card_padding": 24,
                  "kpi_padding": 20, "banner_padding": 32},
      "body_align": "top",           # the body is one group under the title
      "title_underline": true,       # the bar under a slide title (false: none)
      "title_ink": "brand",          # slide titles white on dark, accent on light
      "cover": "brand",              # cover / closing: logo top centre (closing:
                                     # bottom centre), emblem at 7%, Sky Blue into
                                     # Royal Blue in the bottom-right corner
      "section": "outline",          # section: title left, large outline number
                                     # right, the body slides' footer
      "agenda": "plain",             # agenda: numbered grid without cards, 2
                                     # columns (3 from 7 items), rows on top,
                                     # every item drawn alike
      "cover_emblem": {              # the mark of the `cover: brand` backdrop
        "scale": 95,                  # % of the slide height (20 to 140)
        "anchor": "bottom_right",     # cropped by the bottom-right corner, or
                                     # "right": inside the right edge, centred
        "opacity": 7                  # % (1 to 15)
      }
    }

The reader (:func:`template_design`) is lenient like ``template_chrome``: it
never raises and keeps what it can read; the strict twin
(:func:`normalize_template_design`) returns the canonical shape and the
problems an author is told about.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: The key in ``extra_metadata``.
DESIGN_KEY = "design"

#: The boolean switches, each ``True`` (today's rendering) unless declared off.
DESIGN_SWITCHES: tuple[str, ...] = ("shadows", "glow", "eyebrows")

#: ``gradient_strokes``: ``allowed`` (today) or ``never``.
GRADIENT_STROKES: tuple[str, ...] = ("allowed", "never")

#: ``body_align``: ``top`` (one group under the title) or ``center``.
BODY_ALIGNS: tuple[str, ...] = ("top", "center")

#: The slide compositions a template may opt into,
#: each a closed enum whose absence is today's composition.
COMPOSITIONS: dict[str, tuple[str, ...]] = {
    "title_ink": ("brand",),
    "cover": ("brand",),
    "section": ("outline",),
    "agenda": ("plain",),
}

#: The boolean switches added with the compositions (``True`` = today).
COMPOSITION_SWITCHES: tuple[str, ...] = ("title_underline",)

#: The brand cover emblem: the mark the ``cover: brand`` backdrop draws
#: on the cover and the closing. ``scale`` is a percentage of the slide height,
#: ``opacity`` a percentage; an undeclared key is the stylesheet's own value
#: (95, bottom_right, 7: the corner-cropped geometry).
COVER_EMBLEM_KEY = "cover_emblem"
COVER_EMBLEM_ANCHORS: tuple[str, ...] = ("bottom_right", "right")
COVER_EMBLEM_SCALE = (20, 140)
COVER_EMBLEM_OPACITY = (1, 15)

#: The radius roles, canvas pixels.
RADIUS_KEYS: tuple[str, ...] = ("card", "inner", "chip", "table")
RADIUS_MAX = 48

#: The spacing roles, canvas pixels (1920 x 1080 canvas).
SPACING_KEYS: tuple[str, ...] = (
    "margin_x",
    "margin_top",
    "footer",
    "title_body",
    "title_subtitle",
    "tile_gap",
    "block_gap",
    "block_gap_max",
    "card_padding",
    "kpi_padding",
    "banner_padding",
)
SPACING_MAX = 240


@dataclass(frozen=True, slots=True)
class TemplateDesign:
    """The design tokens a template declares; an undeclared key is ``None``."""

    shadows: bool | None = None
    glow: bool | None = None
    eyebrows: bool | None = None
    gradient_strokes: str | None = None
    radius: Mapping[str, int] = field(default_factory=dict)
    spacing: Mapping[str, int] = field(default_factory=dict)
    body_align: str | None = None
    title_underline: bool | None = None
    title_ink: str | None = None
    cover: str | None = None
    section: str | None = None
    agenda: str | None = None
    cover_emblem: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.as_dict()

    def as_dict(self) -> dict[str, Any]:
        """The canonical stored shape: declared keys only, in a fixed order."""
        out: dict[str, Any] = {}
        for key in DESIGN_SWITCHES:
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        if self.gradient_strokes is not None:
            out["gradient_strokes"] = self.gradient_strokes
        if self.radius:
            out["radius"] = {key: self.radius[key] for key in RADIUS_KEYS if key in self.radius}
        if self.spacing:
            out["spacing"] = {key: self.spacing[key] for key in SPACING_KEYS if key in self.spacing}
        if self.body_align is not None:
            out["body_align"] = self.body_align
        for key in (*COMPOSITION_SWITCHES, *COMPOSITIONS):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        if self.cover_emblem:
            out[COVER_EMBLEM_KEY] = {
                key: self.cover_emblem[key] for key in ("scale", "anchor", "opacity") if key in self.cover_emblem
            }
        return out


def _percent(value: object, bounds: tuple[int, int]) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    low, high = bounds
    return value if low <= value <= high else None


def _cover_emblem(raw: object) -> dict[str, Any]:
    """The readable keys of a ``cover_emblem`` declaration (lenient)."""
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, Any] = {}
    scale = _percent(raw.get("scale"), COVER_EMBLEM_SCALE)
    if scale is not None:
        out["scale"] = scale
    if raw.get("anchor") in COVER_EMBLEM_ANCHORS:
        out["anchor"] = raw["anchor"]
    opacity = _percent(raw.get("opacity"), COVER_EMBLEM_OPACITY)
    if opacity is not None:
        out["opacity"] = opacity
    return out


def _pixel(value: object, limit: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= limit else None


def _scale(raw: object, keys: tuple[str, ...], limit: int) -> dict[str, int]:
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, int] = {}
    for key in keys:
        value = _pixel(raw.get(key), limit)
        if value is not None:
            out[key] = value
    return out


def template_design(extra_metadata: object) -> TemplateDesign | None:
    """The design tokens stored in *extra_metadata*, ``None`` when none is usable.

    The READ side: never raises. A switch is read only from a literal boolean,
    an enum only from one of its values, a pixel only from an integer in range;
    anything else is skipped as if undeclared.
    """
    if not isinstance(extra_metadata, Mapping):
        return None
    raw = extra_metadata.get(DESIGN_KEY)
    if not isinstance(raw, Mapping):
        return None
    switches = {key: raw.get(key) if isinstance(raw.get(key), bool) else None for key in DESIGN_SWITCHES}
    strokes = raw.get("gradient_strokes")
    align = raw.get("body_align")
    underline = raw.get("title_underline")
    compositions = {key: raw.get(key) if raw.get(key) in values else None for key, values in COMPOSITIONS.items()}
    design = TemplateDesign(
        shadows=switches["shadows"],
        glow=switches["glow"],
        eyebrows=switches["eyebrows"],
        gradient_strokes=strokes if strokes in GRADIENT_STROKES else None,
        radius=_scale(raw.get("radius"), RADIUS_KEYS, RADIUS_MAX),
        spacing=_scale(raw.get("spacing"), SPACING_KEYS, SPACING_MAX),
        body_align=align if align in BODY_ALIGNS else None,
        title_underline=underline if isinstance(underline, bool) else None,
        title_ink=compositions["title_ink"],
        cover=compositions["cover"],
        section=compositions["section"],
        agenda=compositions["agenda"],
        cover_emblem=_cover_emblem(raw.get(COVER_EMBLEM_KEY)),
    )
    return None if design.is_empty else design


def normalize_template_design(raw: object) -> tuple[dict[str, Any] | None, list[str]]:
    """Validate a ``design`` declaration as an author wrote it.

    Returns ``(canonical, problems)``: the shape to store (``None`` when the
    declaration says nothing, so the caller drops the key) and one sentence
    per value that was refused.
    """
    if raw is None:
        return None, []
    if not isinstance(raw, Mapping):
        return None, ["design must be an object"]
    problems: list[str] = []
    known = (
        set(DESIGN_SWITCHES)
        | set(COMPOSITION_SWITCHES)
        | set(COMPOSITIONS)
        | {"gradient_strokes", "radius", "spacing", "body_align", COVER_EMBLEM_KEY}
    )
    for key in raw:
        if key not in known:
            problems.append(f"design.{key} is not a design token")
    for key in (*DESIGN_SWITCHES, *COMPOSITION_SWITCHES):
        if key in raw and not isinstance(raw[key], bool):
            problems.append(f"design.{key} must be true or false")
    for key, values in COMPOSITIONS.items():
        if key in raw and raw[key] not in values:
            problems.append(f"design.{key} must be one of {', '.join(values)}")
    if "gradient_strokes" in raw and raw["gradient_strokes"] not in GRADIENT_STROKES:
        problems.append(f"design.gradient_strokes must be one of {', '.join(GRADIENT_STROKES)}")
    if "body_align" in raw and raw["body_align"] not in BODY_ALIGNS:
        problems.append(f"design.body_align must be one of {', '.join(BODY_ALIGNS)}")
    for group, keys, limit in (("radius", RADIUS_KEYS, RADIUS_MAX), ("spacing", SPACING_KEYS, SPACING_MAX)):
        scale = raw.get(group)
        if scale is None:
            continue
        if not isinstance(scale, Mapping):
            problems.append(f"design.{group} must be an object")
            continue
        for key, value in scale.items():
            if key not in keys:
                problems.append(f"design.{group}.{key} is not a {group} token")
            elif _pixel(value, limit) is None:
                problems.append(f"design.{group}.{key} must be a whole number of pixels from 0 to {limit}")
    emblem = raw.get(COVER_EMBLEM_KEY)
    if emblem is not None:
        if not isinstance(emblem, Mapping):
            problems.append(f"design.{COVER_EMBLEM_KEY} must be an object")
        else:
            for key, value in emblem.items():
                if key == "scale" and _percent(value, COVER_EMBLEM_SCALE) is None:
                    low, high = COVER_EMBLEM_SCALE
                    problems.append(f"design.{COVER_EMBLEM_KEY}.scale must be a whole percentage from {low} to {high}")
                elif key == "opacity" and _percent(value, COVER_EMBLEM_OPACITY) is None:
                    low, high = COVER_EMBLEM_OPACITY
                    problems.append(
                        f"design.{COVER_EMBLEM_KEY}.opacity must be a whole percentage from {low} to {high}"
                    )
                elif key == "anchor" and value not in COVER_EMBLEM_ANCHORS:
                    problems.append(
                        f"design.{COVER_EMBLEM_KEY}.anchor must be one of {', '.join(COVER_EMBLEM_ANCHORS)}"
                    )
                elif key not in ("scale", "anchor", "opacity"):
                    problems.append(f"design.{COVER_EMBLEM_KEY}.{key} is not a cover emblem token")
    design = template_design({DESIGN_KEY: raw})
    return (design.as_dict() if design is not None else None), problems
