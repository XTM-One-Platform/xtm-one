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

"""The brand a video wears: defaults, the template's palette, the spec's own colours.

The compositor reads one flat palette (``build_theme``): background, surface,
foreground, muted, the two accents, the gradient pair, the six series colours
a ``mosaic`` backdrop draws in, the line colour, the text on an accent, the
browser window's chrome, the fonts, the backdrop style, which marks exist and
how the brand moves (``motion``, the tokens of ``motion_design.resolve_motion``).
It is built in that order of precedence: the spec's explicit colours, then the
template's palette (``presentations.resolve_template_theme``, the deck
resolver, so a brand dresses its videos like its decks), then the defaults for
the colour scheme.

A template colour is any CSS colour the deck sanitizer let through - hex,
``rgb()`` or ``hsl()`` - and is handed to the page as written; it is only
parsed (:func:`parse_color`) where a decision depends on its luminance.
"""

from __future__ import annotations

import colorsys
import re

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_SHORT_HEX = re.compile(r"^#[0-9a-fA-F]{3}$")
_FUNC = re.compile(r"^(rgb|rgba|hsl|hsla)\(\s*([^)]*)\)$", re.IGNORECASE)

HEADING_FONT = "'Geologica', 'IBM Plex Sans', 'Segoe UI', system-ui, sans-serif"
BODY_FONT = "'IBM Plex Sans', 'Segoe UI', system-ui, sans-serif"

DEFAULTS: dict[str, dict[str, str]] = {
    "dark": {
        "background": "#070b16",
        "surface": "#111827",
        "foreground": "#f4f6fb",
        "muted": "#9aa4b8",
        "accent": "#0fbcff",
        "accent_2": "#7b61ff",
        "line": "rgba(255,255,255,0.06)",
        "chrome": "#151b29",
        "chrome_field": "#0b101c",
    },
    "light": {
        "background": "#eef2f8",
        "surface": "#ffffff",
        "foreground": "#0b1020",
        "muted": "#5b6478",
        "accent": "#0a84ff",
        "accent_2": "#7b61ff",
        "line": "rgba(10,20,40,0.07)",
        "chrome": "#e6eaf2",
        "chrome_field": "#ffffff",
    },
}

#: Template palette key -> compositor key.
_TEMPLATE_KEYS: dict[str, str] = {
    "background": "background",
    "surface": "surface",
    "foreground": "foreground",
    "muted": "muted",
    "accent": "accent",
    "gradient_to": "accent_2",
}
_FONT_KEYS = ("heading_font", "body_font")


def _number(text: str, scale: float) -> float:
    text = text.strip()
    if text.endswith("%"):
        return float(text[:-1]) / 100 * scale
    return float(text)


def parse_color(value: object) -> tuple[float, float, float] | None:
    """``(r, g, b)`` in 0-1 of a CSS hex / ``rgb()`` / ``hsl()`` colour, or ``None``."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if _SHORT_HEX.match(text):
        text = "#" + "".join(ch * 2 for ch in text[1:])
    if _HEX.match(text):
        return tuple(int(text[i : i + 2], 16) / 255 for i in (1, 3, 5))  # type: ignore[return-value]
    match = _FUNC.match(text)
    if not match:
        return None
    name = match.group(1).lower()
    parts = [p for p in re.split(r"[\s,/]+", match.group(2).strip()) if p]
    if len(parts) < 3:
        return None
    try:
        if name.startswith("rgb"):
            r, g, b = (_number(p, 255) / 255 for p in parts[:3])
        else:
            hue = float(parts[0].removesuffix("deg")) % 360 / 360
            sat = _number(parts[1], 1)
            light = _number(parts[2], 1)
            r, g, b = colorsys.hls_to_rgb(hue, light, sat)
    except ValueError:
        return None
    return (min(max(r, 0.0), 1.0), min(max(g, 0.0), 1.0), min(max(b, 0.0), 1.0))


def _is_color(value: object) -> bool:
    return parse_color(value) is not None


def _luminance(rgb: tuple[float, float, float]) -> float:
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def build_theme(
    *,
    mode: str,
    background_style: str,
    template_palette: dict | None = None,
    accent: str | None = None,
    accent_2: str | None = None,
    background_color: str | None = None,
    text_color: str | None = None,
    has_logo: bool = False,
    has_mark: bool = False,
    motion: object = None,
) -> dict:
    from app.services.video_studio.motion_design import resolve_motion

    palette = template_palette or {}
    theme: dict = dict(DEFAULTS["light" if mode == "light" else "dark"])
    for source_key, key in _TEMPLATE_KEYS.items():
        if _is_color(palette.get(source_key)):
            theme[key] = palette[source_key].strip()
    if not _is_color(palette.get("gradient_to")) and _is_color(palette.get("chart_2")):
        theme["accent_2"] = palette["chart_2"].strip()
    for key, value in (
        ("accent", accent),
        ("accent_2", accent_2),
        ("background", background_color),
        ("foreground", text_color),
    ):
        if _is_color(value):
            theme[key] = value
    theme["gradient_from"] = (
        palette["gradient_from"].strip() if _is_color(palette.get("gradient_from")) and not accent else theme["accent"]
    )
    theme["gradient_to"] = (
        palette["gradient_to"].strip() if _is_color(palette.get("gradient_to")) and not accent_2 else theme["accent_2"]
    )
    series = [palette[f"chart_{i}"].strip() for i in range(1, 7) if _is_color(palette.get(f"chart_{i}"))]
    theme["series"] = series or [theme["accent"], theme["accent_2"], theme["gradient_from"], theme["gradient_to"]]
    accent_rgb = parse_color(theme["accent"]) or (0.0, 0.7, 1.0)
    theme["on_accent"] = "#0b0f19" if _luminance(accent_rgb) > 0.45 else "#ffffff"
    background_rgb = parse_color(theme["background"])
    if background_rgb is not None and _luminance(background_rgb) > 0.4 and mode != "light":
        # A light page under the dark scheme's chrome would read as a hole.
        theme["chrome"], theme["chrome_field"] = DEFAULTS["light"]["chrome"], DEFAULTS["light"]["chrome_field"]
    theme["heading_font"] = HEADING_FONT
    theme["body_font"] = BODY_FONT
    for key in _FONT_KEYS:
        value = palette.get(key)
        if isinstance(value, str) and value.strip():
            theme[key] = value.strip()
    theme["background_style"] = background_style
    theme["logo"] = bool(has_logo)
    theme["mark"] = bool(has_mark)
    # How the brand moves: the tokens every choreography of the page reads.
    theme["motion"] = resolve_motion(motion)
    return theme
