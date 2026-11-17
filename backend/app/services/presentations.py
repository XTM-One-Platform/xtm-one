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

"""Presentation persistence + serialization.

Single place that turns a validated ``PresentationSpec`` into a ``Presentation``
row and back into the JSON the frontend deck renderer consumes. Shared by the
agent-facing ``generate_presentation`` tool and the REST read endpoint so the
two cannot drift on visibility, revisioning or the resolved theme.

The theme resolution here is where a template's theme is consumed: when a deck links
a ``TemplateContainer``, its ``theme_config`` (brand palette / fonts) and
``aspect_ratio`` are read back — visibility-checked, then filtered through a
whitelist — and returned alongside the spec. Everything else about a deck lives
in its own ``spec``, so deleting a template never breaks a deck built from it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import is_valid_uuid
from app.models.presentation import (
    VERSION_SOURCE_AGENT,
    VERSION_SOURCE_DUPLICATE,
    Presentation,
    PresentationVersion,
)
from app.services.image_geometry import backfill_image_geometry, render_geometry
from app.services.presentation_chrome import TemplateChrome, template_chrome
from app.services.presentation_design import TemplateDesign, template_design
from app.services.presentation_pictures import canonical_file_reference as _canonical_file_reference
from app.services.presentation_pictures import slide_file_references
from app.services.presentation_spec import (
    DEFAULT_SLIDE_CAP,
    HERO_TONES,
    HERO_VARIANTS,
    MAX_SLIDE_CAP,
    PALETTE_COLORS,
    PRESENTATION_THEMES,
    SHARED_HEROES,
    SLIDE_LAYOUTS,
    TEMPLATE_HEROES,
    clamp_slide_cap,
    logo_file_ids,
    normalize_aspect_ratio,
    normalize_theme,
    presentation_kind,
    slide_theme_overrides,
    unit_noun,
    validate_spec,
)

if TYPE_CHECKING:
    from fastapi import Request

    from app.models.user import User

logger = logging.getLogger(__name__)

#: The brand palette every branded artifact understands: background,
#: foreground, accent, muted text and the two font families. Decks and reports
#: both honour these six; each then adds keys of its own below.
BRAND_THEME_CONFIG_KEYS: tuple[str, ...] = (
    "background",
    "foreground",
    "accent",
    "muted",
    "heading_font",
    "body_font",
)

#: Theme keys that name a platform FILE rather than a CSS token: the
#: brand logo drawn by the deck chrome, its dark-on-light variant for the
#: light theme, and the brand MARK - the symbol without the wordmark -
#: the ``gradient`` hero draws as a faint watermark in the corner of a cover,
#: the way the Filigran PowerPoint cover carries it. Validated as UUIDs — the
#: frontend builds a download URL from the value, so a free-form string here
#: would be a path-traversal primitive.
THEME_FILE_ID_KEYS: tuple[str, ...] = ("logo_file_id", "logo_light_file_id", "mark_file_id")

#: Asset role that backs each file-id theme key when the config names none
#: (see :func:`resolve_template_theme`).
THEME_FILE_ID_ROLES: dict[str, str] = {
    "logo_file_id": "logo",
    "logo_light_file_id": "logo_light",
    "mark_file_id": "logo_mark",
}

#: The chart palette: six ORDERED series colours a ``chart`` block's
#: untoned series take in order (a toned series takes the semantic tone
#: instead). ``chart_1`` is the primary series - the template's accent on the
#: brand decks - and a template that sets none leaves the renderer's own
#: sequence, derived from the accent, in place.
CHART_THEME_CONFIG_KEYS: tuple[str, ...] = tuple(f"chart_{index}" for index in range(1, 7))

#: The brand hues: up to four colours a template declares beside its
#: accent - the product colours on the built-in brand decks (XTM One, OpenCTI,
#: OpenAEV, OpenCRQ). An architecture node or zone wears one through ``color``;
#: a palette that declares none leaves the renderer's fallback, its chart
#: series, in place. Brand identity, not scheme-bound: they carry to either
#: colour scheme, and a ``theme_variants`` palette may re-tune them for its
#: own. Not to be confused with :data:`BRAND_THEME_CONFIG_KEYS`, the brand six.
BRAND_COLOR_THEME_CONFIG_KEYS: tuple[str, ...] = tuple(f"brand_{index}" for index in range(1, 5))

#: The feedback tones as TEXT: the colour a title, a list item, a badge,
#: a metric delta or a table cell drawn in the success / warning / error tone
#: takes, where the plain tone keeps the fills, borders, accent bars and chart
#: series. A bright tone that reads as a fill can sit under the text contrast
#: floor on light paper (the Filigran Brand Deck's #F9A825 is 1.9:1 on its
#: #FBFBFB cards), so a palette may name a darker text tone. Unset, the text
#: takes the plain tone, so every other template keeps its look.
FEEDBACK_TEXT_THEME_CONFIG_KEYS: tuple[str, ...] = ("success_text", "warning_text", "error_text")

#: Theme keys a ``TemplateContainer.theme_config`` may contribute to a deck.
#: The frontend maps each one to a CSS custom property on the deck container
#: (``THEME_VARIABLES`` in ``PresentationDeck.tsx``) — anything else in the
#: container's config is ignored rather than forwarded, so a template can
#: restyle a deck's palette but never inject arbitrary CSS. The v2 renderer
#: adds the surface / line pair, the four semantic tones the block
#: catalog reads through ``data-tone``, the brand gradient, a mono font and
#: the logo file ids, the chart palette, the mark (`mark_file_id`) and the
#: brand hues.
THEME_CONFIG_KEYS: tuple[str, ...] = (
    *BRAND_THEME_CONFIG_KEYS,
    "mono_font",
    "surface",
    "line",
    "info",
    "success",
    "warning",
    "error",
    *FEEDBACK_TEXT_THEME_CONFIG_KEYS,
    "gradient_from",
    "gradient_to",
    *CHART_THEME_CONFIG_KEYS,
    *BRAND_COLOR_THEME_CONFIG_KEYS,
    *THEME_FILE_ID_KEYS,
)
# The colours a diagram node or zone may name are exactly the palette keys the
# renderer resolves them from: a value the validator accepts always paints.
assert (*BRAND_COLOR_THEME_CONFIG_KEYS, *CHART_THEME_CONFIG_KEYS) == PALETTE_COLORS  # noqa: S101 - import-time contract

#: The longest theme value a template may carry: a colour, a font stack and a
#: gradient of colours all fit.
MAX_THEME_VALUE_CHARS = 120

#: A character a theme value never needs: anything but letters, digits, the
#: punctuation colours, gradients and font stacks are written with, and the
#: ASCII space. That keeps out what would leave the single CSS declaration the
#: value lands in (``;``, braces), open a comment (``*``) or an at-rule
#: (``@``), escape a character (``\``), name a URL scheme (``:``), close the
#: surrounding markup (``<``, ``>``), and every control or non-ASCII character.
_DISALLOWED_THEME_CHAR_RE = re.compile(r"[^A-Za-z0-9#(),.%_\-'\"/ ]")

#: What a theme value is trimmed of at both ends before it is judged and
#: served: every character ``str.strip()`` removes, plus U+FEFF, which
#: JavaScript's ``trim()`` removes too (and which lacks U+001C-U+001F and
#: U+0085). The template preview trims with the same set (``TRIM_CHARS`` in
#: ``theme-value.ts``), so both read and serve the same value.
THEME_VALUE_TRIM_CHARS = (
    "\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)

#: The one spelling a file-id key takes: the canonical 8-4-4-4-12 UUID
#: (mirrors ``UUID`` in ``theme-value.ts``). The frontend builds a download
#: path from the value, so what ``uuid.UUID()`` also parses - braces, a
#: ``urn:uuid:`` prefix, no dashes - is refused rather than served as written.
_THEME_FILE_ID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

#: The CSS functions a theme value may call. A browser loads a
#: resource only through a FUNCTION - ``url()``, ``image-set()``,
#: ``cross-fade()``, ``image()``, ``element()``, ``paint()`` and their vendor
#: forms - never through a keyword or a quoted string, and a template palette
#: lands in CSS every viewer's browser paints (``background: var(--deck-bg)``),
#: so a fetching value would report each viewer's address and the time they
#: opened the deck to whoever hosts the image. Hence an ALLOW-list of the
#: functions that compute a colour, a gradient of colours or a length and
#: cannot load anything: a deny-list is one new CSS function (or one vendor
#: prefix) away from that leak, and an unknown function is refused. Mirrored by
#: ``SAFE_THEME_FUNCTIONS`` in ``frontend/src/components/presentation/theme-value.ts``
#: (pinned by a test).
SAFE_THEME_FUNCTIONS: frozenset[str] = frozenset(
    {
        # Colours.
        "rgb",
        "rgba",
        "hsl",
        "hsla",
        "hwb",
        "lab",
        "lch",
        "oklab",
        "oklch",
        "color",
        "color-mix",
        "light-dark",
        # Math.
        "calc",
        "min",
        "max",
        "clamp",
        "var",
        # Gradients: standard, repeating, vendor-prefixed, and the legacy
        # WebKit syntax whose from() / to() / color-stop() are allowed inside it.
        "linear-gradient",
        "radial-gradient",
        "conic-gradient",
        "repeating-linear-gradient",
        "repeating-radial-gradient",
        "repeating-conic-gradient",
        "-webkit-linear-gradient",
        "-webkit-radial-gradient",
        "-webkit-conic-gradient",
        "-webkit-repeating-linear-gradient",
        "-webkit-repeating-radial-gradient",
        "-webkit-repeating-conic-gradient",
        "-moz-linear-gradient",
        "-moz-radial-gradient",
        "-moz-conic-gradient",
        "-moz-repeating-linear-gradient",
        "-moz-repeating-radial-gradient",
        "-moz-repeating-conic-gradient",
        "-webkit-gradient",
    }
)

#: The functions the legacy ``-webkit-gradient()`` takes as arguments
#: (``from(#000)``, ``to(#fff)``, ``color-stop(50%, red)``): allowed inside it
#: and nowhere else.
_WEBKIT_GRADIENT_ARGUMENTS: frozenset[str] = frozenset({"from", "to", "color-stop"})

#: One CSS escape: a backslash and up to six hex digits (plus the single
#: whitespace that ends them), or a backslash and any other character.
_CSS_ESCAPE_RE = re.compile(r"\\(?:([0-9A-Fa-f]{1,6})[ \t\n\r\f]?|(.)|$)", re.DOTALL)
_CSS_WHITESPACE = " \t\n\r\f"
_FUNCTION_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_")


def _decode_css_escapes(value: str) -> str:
    """*value* with its CSS escapes decoded, the way a browser reads them.

    ``\\75 rl(``, ``u\\rl(`` and ``\\000075rl(`` all read ``url(`` - a function
    name is matched on what it decodes to, not on how it is spelled.
    """

    def decode(match: re.Match[str]) -> str:
        digits, char = match.group(1), match.group(2)
        if digits is None:
            return char if char is not None else "\ufffd"
        code = int(digits, 16)
        if code == 0 or 0xD800 <= code <= 0xDFFF or code > 0x10FFFF:
            return "\ufffd"
        return chr(code)

    return _CSS_ESCAPE_RE.sub(decode, value)


def _theme_function_problem(text: str) -> str | None:
    """Why the CSS functions *text* calls make it unsafe, or ``None``.

    *text* is the value as a browser reads it: escapes decoded, lower-cased.
    Every ``(`` belongs to the name written right before it; that name must be
    in :data:`SAFE_THEME_FUNCTIONS` (``from`` / ``to`` / ``color-stop`` only
    inside ``-webkit-gradient()``), with nothing between the name and the
    ``(`` - CSS calls no function across a space, so a gap there is an attempt
    to slip one past a name match. A ``(`` after no name (after an operator, a
    comma, another ``(``) opens a bare group, as in ``calc((100% - 8px) / 2)``,
    which calls nothing; a run of hyphens and digits that starts with ``--`` is
    a name all the same (CSS reads ``--1(`` as a call of an author-defined
    function). Every parenthesis must be closed.

    A quoted string is text, never a call - ``"Foo (Bar)", sans-serif`` is a
    font family - so what sits between a quote and its closing twin is skipped.
    A string left open is refused: a value the guard accepts carries no
    backslash and no newline, so its strings end exactly where a browser ends
    them and nothing hidden "inside a string" can be read as a call.

    Checking the source is enough because a function token only ever forms
    from a name written right before its ``(``: ``var()`` substitutes TOKENS,
    so ``var(--x, url)(//host/x)`` yields the identifier ``url`` then a bare
    group - never a ``url(`` call - and a browser serializing the result writes
    ``url/**/(//host/x)`` to keep the two apart when the text is parsed again
    (the ``?print-pdf`` body background, Mermaid's ``<style>``).
    """
    open_calls: list[str] = []
    quote: str | None = None
    for index, char in enumerate(text):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            continue
        if char == ")":
            if not open_calls:
                return "it closes a parenthesis it never opened"
            open_calls.pop()
            continue
        if char != "(":
            continue
        end = index
        while end > 0 and text[end - 1] in _CSS_WHITESPACE:
            end -= 1
        start = end
        while start > 0 and text[start - 1] in _FUNCTION_NAME_CHARS:
            start -= 1
        name = text[start:end]
        if not name.startswith("--") and not any(c.isalpha() or c == "_" for c in name):
            open_calls.append("")
            continue
        if end != index:
            return f"it puts a space between {name} and its '('"
        allowed = name in SAFE_THEME_FUNCTIONS or (
            name in _WEBKIT_GRADIENT_ARGUMENTS and "-webkit-gradient" in open_calls
        )
        if not allowed:
            return f"it calls {name}()"
        open_calls.append(name)
    if quote is not None:
        return "it leaves a quoted string open"
    if open_calls:
        return "it leaves a parenthesis open"
    return None


def trim_theme_value(value: str) -> str:
    """*value* without the :data:`THEME_VALUE_TRIM_CHARS` at either end - what is judged and served."""
    return value.strip(THEME_VALUE_TRIM_CHARS)


def theme_value_problem(key: str, value: object) -> str | None:
    """Why *value* is not acceptable for theme key *key*, or ``None`` when it is.

    File-id keys must be a canonical UUID (the frontend turns them into a download URL);
    every other key is a CSS colour, gradient, length or font stack of at most
    :data:`MAX_THEME_VALUE_CHARS` characters that calls no function outside
    :data:`SAFE_THEME_FUNCTIONS` (a quoted string is text, not a call) and holds
    no character outside the set a colour or a font stack is written with (see
    ``_DISALLOWED_THEME_CHAR_RE``). The functions are read through the escapes
    and the case a browser reads them through, so the reason names what the
    value would have called. The reason reads after "the value is refused
    because": ``"it calls image-set()"``.
    """
    if not isinstance(value, str):
        return "it is not a string"
    text = trim_theme_value(value)
    if not text:
        return "it is empty"
    if key in THEME_FILE_ID_KEYS:
        return None if _THEME_FILE_ID_RE.fullmatch(text) else "it is not the UUID of a template asset"
    if len(text) > MAX_THEME_VALUE_CHARS:
        return f"it is longer than {MAX_THEME_VALUE_CHARS} characters"
    function_problem = _theme_function_problem(_decode_css_escapes(text).lower())
    if function_problem is not None:
        return function_problem
    if "\\" in text:
        return "it contains a backslash (a CSS escape)"
    if any(ord(c) < 0x20 or ord(c) == 0x7F or unicodedata.category(c) in ("Cc", "Cf") for c in text):
        return "it contains a control character"
    stray = _DISALLOWED_THEME_CHAR_RE.search(text)
    if stray is not None:
        return f"it contains {stray.group(0)!r}"
    return None


def is_safe_theme_value(key: str, value: object) -> bool:
    """Whether *value* is acceptable for theme key *key* (:func:`theme_value_problem`).

    The single definition of "safe" shared by the sanitizer applied on every
    read and the authoring-side validator (``template_containers.validate_theme_config``),
    so what a template author is told is accepted is exactly what gets forwarded.
    """
    return theme_value_problem(key, value) is None


def sanitize_theme_config(
    raw: object, *, keys: tuple[str, ...] = THEME_CONFIG_KEYS
) -> dict[str, str]:
    """Keep only whitelisted, syntactically safe theme values from a container.

    *keys* is the whitelist to apply. It defaults to the deck's keys, and reports
    pass their own set — a report needs semantic risk colours a deck has
    no use for. The value safety rules are deliberately NOT parameterized: what
    counts as a safe CSS token (or a valid file id) is a property of the
    renderer, not of the consumer, so every caller gets the same check.
    """
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, str] = {}
    for key in keys:
        value = raw.get(key)
        if is_safe_theme_value(key, value):
            cleaned[key] = trim_theme_value(str(value))
    return cleaned


#: The two colour schemes a deck is read in — and, the only two
#: theme names there are: ``light`` is the paper-white renderer theme, ``dark``
#: the dark surfaces. A stored ``filigran`` (the retired brand preset) reads as
#: ``dark`` through :func:`~app.services.presentation_spec.normalize_theme`.
THEME_SCHEMES: tuple[str, ...] = ("dark", "light")

#: ``extra_metadata`` slot where a template carries a palette for the OTHER
#: colour scheme: ``{"theme_variants": {"light": {<theme_config keys>}}}``.
#:
#: A template's ``theme_config`` is ONE palette, and it always applies — that is
#: what makes a brand template brand a deck. But a deck also names a theme
#: (``dark`` / ``light``), and a dark-palette template on a deck the user asked
#: to be ``light`` used to yield a dark deck that merely CALLED itself light:
#: the chrome then drew the dark-on-light logo onto the dark background,
#: invisibly. With a variant, ``theme: light`` on the Filigran deck is a light
#: Filigran deck — same fonts, same logo assets (the chrome picks the
#: ``logo_light`` one), the design system's light tokens for the colours. A
#: template with NO variant for the deck's scheme does not get to keep the
#: surfaces of the other one: :func:`scheme_palette` reads which scheme the base
#: palette is designed for off its ``background`` and forwards only the brand
#: half (accent, gradient, fonts, logos) to the opposite scheme, so the
#: renderer's own tokens supply paper and ink. That fallback is what a deck
#: renders when a template that never supported the scheme is nevertheless
#: asked for it (an older deck, a restored version) — it is no longer offered:
#: :func:`template_supported_schemes` is the list the theme picker is
#: restricted to and a template link snaps the theme against.
THEME_VARIANTS_KEY = "theme_variants"

#: Theme keys whose value only makes sense on ONE colour scheme: the surfaces,
#: the ink drawn on them, the feedback tones tuned for that surface, and the
#: chart series palette - six hues picked for contrast against that
#: surface (the seeded brand deck ships a brighter set for dark and a deeper
#: set for light; ``chart_1`` falls back to the accent either way). The rest
#: of a palette — accent, gradient, fonts, logo ids — is the brand, and carries
#: to either scheme. Mirrored by ``SCHEME_BOUND_THEME_KEYS`` in
#: ``frontend/src/components/presentation/previewSpec.ts`` (pinned by a test).
SCHEME_BOUND_THEME_KEYS: tuple[str, ...] = (
    "background",
    "surface",
    "line",
    "foreground",
    "muted",
    "info",
    "success",
    "warning",
    "error",
    *FEEDBACK_TEXT_THEME_CONFIG_KEYS,
    *CHART_THEME_CONFIG_KEYS,
)

#: Relative luminance below which white ink has more contrast than black ink
#: on a surface — the WCAG crossover (1.05 / (L + 0.05) = (L + 0.05) / 0.05),
#: and therefore where a background stops being "light".
_DARK_SURFACE_LUMINANCE = 0.1791

_HEX_COLOR_RE = re.compile(r"^#([0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$", re.IGNORECASE)
_FUNC_COLOR_RE = re.compile(r"^(rgba?|hsla?)\((.*)\)$", re.IGNORECASE | re.DOTALL)
_NAMED_COLOR_RGB: dict[str, tuple[float, float, float]] = {
    "white": (1.0, 1.0, 1.0),
    "black": (0.0, 0.0, 0.0),
}


def _color_channel(token: str, scale: float) -> float | None:
    """One ``rgb()`` / ``hsl()`` argument as a 0..1 fraction (``%`` or a number over *scale*)."""
    token = token.strip()
    try:
        if token.endswith("%"):
            return min(1.0, max(0.0, float(token[:-1]) / 100.0))
        return min(1.0, max(0.0, float(token) / scale))
    except ValueError:
        return None


def _hsl_to_rgb(hue: float, sat: float, light: float) -> tuple[float, float, float]:
    """CSS ``hsl()`` to sRGB fractions (CSS Color 4, section 7.1)."""
    hue = hue % 360.0

    def channel(n: int) -> float:
        k = (n + hue / 30.0) % 12.0
        a = sat * min(light, 1.0 - light)
        return light - a * max(-1.0, min(k - 3.0, 9.0 - k, 1.0))

    return channel(0), channel(8), channel(4)


def css_color_luminance(value: object) -> float | None:
    """The WCAG relative luminance (0 = black, 1 = white) of a CSS colour token.

    Understands what a theme value can be — ``#rgb`` / ``#rrggbb`` (alpha
    digits ignored), ``rgb()`` / ``rgba()``, ``hsl()`` / ``hsla()`` in the
    comma and the space syntax, ``white`` / ``black`` — and returns ``None``
    for anything else (a gradient, a ``var()``, a colour name it does not know),
    which every caller reads as "cannot tell".
    """
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    rgb: tuple[float, float, float] | None = _NAMED_COLOR_RGB.get(text)
    if rgb is None and (match := _HEX_COLOR_RE.match(text)):
        digits = match.group(1)
        if len(digits) <= 4:
            digits = "".join(ch * 2 for ch in digits[:3])
        digits = digits[:6]
        rgb = (
            int(digits[0:2], 16) / 255.0,
            int(digits[2:4], 16) / 255.0,
            int(digits[4:6], 16) / 255.0,
        )
    elif rgb is None and (match := _FUNC_COLOR_RE.match(text)):
        fn, body = match.group(1), match.group(2)
        # Space syntax carries the alpha after a slash; the comma syntax as a
        # fourth argument. Either way only the first three channels matter.
        parts = [p for p in re.split(r"[,\s]+", body.split("/", 1)[0].strip()) if p]
        if len(parts) < 3:
            return None
        if fn.startswith("rgb"):
            channels = [_color_channel(p, 255.0) for p in parts[:3]]
            if any(c is None for c in channels):
                return None
            rgb = (channels[0], channels[1], channels[2])  # type: ignore[assignment]
        else:
            try:
                hue = float(parts[0].removesuffix("deg"))
            except ValueError:
                return None
            sat = _color_channel(parts[1], 100.0)
            light = _color_channel(parts[2], 100.0)
            if sat is None or light is None:
                return None
            rgb = _hsl_to_rgb(hue, sat, light)
    if rgb is None:
        return None

    def linear(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b)


def palette_scheme(palette: Mapping[str, str]) -> str | None:
    """The colour scheme a palette is designed for, read off its ``background``.

    ``dark`` when the surface is dark enough for white ink, ``light`` otherwise;
    ``None`` when the palette names no background or one that cannot be read,
    which :func:`scheme_palette` treats as "apply the palette as authored".
    """
    luminance = css_color_luminance(palette.get("background"))
    if luminance is None:
        return None
    return "dark" if luminance < _DARK_SURFACE_LUMINANCE else "light"


def scheme_palette(
    base: Mapping[str, str], variant: Mapping[str, str], scheme: str
) -> dict[str, str]:
    """The palette a template contributes to a deck of colour scheme *scheme*.

    The *variant* (``theme_variants[scheme]``, possibly empty) is laid over the
    *base*. When the base is designed for the OTHER scheme, its scheme-bound
    keys (:data:`SCHEME_BOUND_THEME_KEYS`) are left out first: a dark brand's
    surfaces never reach a ``light`` deck, whether the template ships a light
    variant (which then supplies what it supplies — a partial variant does not
    inherit a dark ``muted``) or none at all (the renderer's light tokens take
    over and only the brand half — accent, gradient, fonts, logos — carries).
    A base whose scheme cannot be read is applied as authored.
    """
    designed_for = palette_scheme(base)
    if designed_for is not None and designed_for != scheme:
        base = {k: v for k, v in base.items() if k not in SCHEME_BOUND_THEME_KEYS}
    return {**base, **variant}


def theme_scheme(theme: str | None) -> str:
    """The colour scheme of a deck theme name: ``light`` for ``light``, else ``dark``.

    Tolerant of the retired names on purpose: a stored ``filigran`` is dark,
    and so is anything unreadable — the renderer's own default.
    """
    return "light" if normalize_theme(theme) == "light" else "dark"


def template_theme_variant(
    extra_metadata: object,
    scheme: str,
    *,
    keys: tuple[str, ...] = THEME_CONFIG_KEYS,
) -> dict[str, str]:
    """The sanitized ``theme_variants[scheme]`` of a container, or ``{}``.

    Runs through :func:`sanitize_theme_config` with the same *keys* as the base
    palette, so a variant can never forward a key or a value the base could not.
    """
    if scheme not in THEME_SCHEMES or not isinstance(extra_metadata, dict):
        return {}
    variants = extra_metadata.get(THEME_VARIANTS_KEY)
    if not isinstance(variants, dict):
        return {}
    return sanitize_theme_config(variants.get(scheme), keys=keys)


#: ``extra_metadata`` key under which a template DECLARES the colour schemes it
#: supports: ``["dark"]``, ``["light"]`` or ``["dark", "light"]``. The
#: designer's decision, made on the New Template form and in Configuration;
#: what :func:`template_supported_schemes` answers whenever it is present.
SUPPORTED_SCHEMES_KEY = "supported_schemes"

#: ``extra_metadata.max_slides``: how many slides a deck wearing the
#: template may hold. A template is for a kind of deck - a board pack runs
#: longer than a pitch - so the length a deck may reach is the template's to
#: say, within the platform ceiling; a free deck, and a template that says
#: nothing, take ``DEFAULT_SLIDE_CAP``. Read by :func:`template_slide_cap`,
#: written through ``template_containers.validate_slide_cap``.
SLIDE_CAP_KEY = "max_slides"


def template_slide_cap(extra_metadata: object) -> int:
    """How many slides a deck of this template may hold.

    :data:`SLIDE_CAP_KEY` of *extra_metadata* as :func:`clamp_slide_cap` reads
    it: a whole number from 1 to :data:`MAX_SLIDE_CAP`, the ceiling for a
    larger one, :data:`DEFAULT_SLIDE_CAP` for anything else - an absent key, a
    row written before the setting existed, a value that is not a number. The
    one reader of the setting: the write validators store it as an int, but a
    row edited by hand is read the same way as a clean one.
    """
    if not isinstance(extra_metadata, dict):
        return DEFAULT_SLIDE_CAP
    return clamp_slide_cap(extra_metadata.get(SLIDE_CAP_KEY))


def effective_slide_cap(slide_cap: int, stored_spec: object) -> int:
    """The cap a re-save of a stored deck is held to: *slide_cap*, or its current size if larger.

    A deck can be over its cap without anyone having broken a rule: it was
    written under a template whose ``max_slides`` was since lowered, or it was
    re-linked to a shorter template, or it came in as a copy (a duplicate, a
    restore, a package import - held to the ceiling only). Such a deck stays
    editable and saveable as it is; it just cannot GROW until it is back under
    the cap. ``insert_slide`` and the editor's "Add slide" refuse at the cap
    regardless, so the growth rule never admits one more slide.
    """
    cap = clamp_slide_cap(slide_cap)
    slides = stored_spec.get("slides") if isinstance(stored_spec, dict) else None
    stored = len(slides) if isinstance(slides, list) else 0
    return max(cap, min(stored, MAX_SLIDE_CAP))


def declared_supported_schemes(extra_metadata: object) -> list[str] | None:
    """The schemes a template declares under :data:`SUPPORTED_SCHEMES_KEY`, or ``None``.

    Canonical (:data:`THEME_SCHEMES`) order whatever the stored order, unknown
    entries ignored; ``None`` when the template declares nothing readable — a
    row created before the declaration existed, or a value that is not a list
    — so the caller falls back to :func:`derived_supported_schemes`. An empty
    declaration is ``None`` too: "supports no theme" is not a state a template
    can be in (the write validators refuse it), so a stray ``[]`` must not
    strand every deck wearing the template.
    """
    if not isinstance(extra_metadata, dict):
        return None
    raw = extra_metadata.get(SUPPORTED_SCHEMES_KEY)
    if not isinstance(raw, (list, tuple)):
        return None
    declared = {item.strip().lower() for item in raw if isinstance(item, str)}
    ordered = [scheme for scheme in THEME_SCHEMES if scheme in declared]
    return ordered or None


def derived_supported_schemes(
    theme_config: object,
    extra_metadata: object,
    *,
    keys: tuple[str, ...] = THEME_CONFIG_KEYS,
) -> list[str]:
    """The colour schemes a template's PALETTES can dress a deck in, in :data:`THEME_SCHEMES` order.

    A template's ``theme_config`` is ONE palette, designed for one scheme
    (:func:`palette_scheme` reads which off its ``background``): that scheme is
    always supported. The OTHER scheme is supported only when the template
    ships a ``theme_variants`` palette for it that actually reads as that
    scheme (a "light" variant whose own background is dark is not a light
    palette; a variant that names no background is taken at its word). Without
    such a variant :func:`scheme_palette` would drop the surfaces and let the
    renderer's own paper and ink through — a deck no longer in the brand it was
    asked to be in.

    A palette whose background cannot be read (``var()``, a gradient, none at
    all) is applied as authored on either scheme, so both are supported. The
    order is the canonical one, never the template's: the first entry is the
    scheme a deck is snapped to.

    This is the FALLBACK for a template that declares nothing (see
    :func:`template_supported_schemes`) and the SUGGESTION the template form
    starts from; the declaration is what the designer decides.
    """
    base = sanitize_theme_config(theme_config, keys=keys)
    designed_for = palette_scheme(base)
    if designed_for is None:
        return list(THEME_SCHEMES)
    supported: list[str] = []
    for scheme in THEME_SCHEMES:
        if scheme == designed_for:
            supported.append(scheme)
            continue
        variant = template_theme_variant(extra_metadata, scheme, keys=keys)
        if variant and palette_scheme(variant) in (None, scheme):
            supported.append(scheme)
    return supported


def template_supported_schemes(
    theme_config: object,
    extra_metadata: object,
    *,
    keys: tuple[str, ...] = THEME_CONFIG_KEYS,
) -> list[str]:
    """The colour schemes a template can dress a deck in, in :data:`THEME_SCHEMES` order.

    The template's own DECLARATION when it makes one
    (:func:`declared_supported_schemes`, the Dark / Light switches of the
    template form), else what its palettes can dress
    (:func:`derived_supported_schemes`, kept for rows created
    before the declaration existed so they keep answering what they answered).

    This list is what the deck editor's theme picker is restricted to, what a
    template link snaps the deck's theme against (:func:`fit_theme_to_schemes`)
    and why a direct change to an unsupported theme is refused — one answer for
    the REST serializer, the agent tools and the write paths.
    """
    declared = declared_supported_schemes(extra_metadata)
    if declared is not None:
        return declared
    return derived_supported_schemes(theme_config, extra_metadata, keys=keys)


def template_supported_layouts(layouts: object) -> list[str]:
    """The layouts a template lets the editor move a slide to, in catalog order.

    A template ADVERTISES layouts (``TemplateContainer.layouts``, ids checked
    against the catalog by ``validate_layouts``); the built-in brand decks
    advertise the whole catalog. The picker offers the advertised ids
    restricted to :data:`SLIDE_LAYOUTS` and the whole catalog when the template
    advertises nothing renderable (no catalog, unknown ids, or only ids of the
    retired v1 layouts an older row may still carry): a template
    that says nothing about layouts restricts nothing, the way a palette that
    says nothing about schemes dresses both. Advisory, like the catalog itself:
    ``validate_presentation`` reports an off-template slide, the renderer still
    draws it, and the picker keeps a slide's current layout listed whatever the
    template says. Entries may be ``{"id": ...}`` objects (the stored shape) or
    bare ids.
    """
    advertised: list[str] = []
    if isinstance(layouts, (list, tuple)):
        for entry in layouts:
            layout = entry.get("id") if isinstance(entry, Mapping) else entry
            if isinstance(layout, str) and layout in SLIDE_LAYOUTS and layout not in advertised:
                advertised.append(layout)
    if not advertised:
        return list(SLIDE_LAYOUTS)
    return [layout for layout in SLIDE_LAYOUTS if layout in advertised]


def theme_fits_schemes(theme: object, supported_schemes: Sequence[str]) -> bool:
    """Whether a deck theme reads in one of *supported_schemes* (an empty list supports nothing)."""
    return theme_scheme(theme if isinstance(theme, str) else None) in supported_schemes


def fit_theme_to_schemes(theme: object, supported_schemes: Sequence[str]) -> str:
    """*theme*'s scheme when the template supports it, else the template's first scheme.

    This is the "template wins" rule of a template link: the deck keeps the
    scheme it had whenever the template can dress it, and is snapped to the
    template's designed scheme when it cannot — a Filigran deck asked to be
    light stays light, an XTM One deck (dark only) becomes dark. A theme is a
    scheme, so the returned value is a valid theme name.
    """
    current = theme_scheme(theme if isinstance(theme, str) else None)
    if current in supported_schemes or not supported_schemes:
        return current
    return supported_schemes[0]


def unsupported_theme_message(
    theme: object, *, template_name: str, supported_schemes: Sequence[str]
) -> str:
    """The one sentence every surface (REST 422, tool error) uses for a theme a template cannot dress."""
    wanted = theme_scheme(theme if isinstance(theme, str) else None)
    offered = " / ".join(supported_schemes) if supported_schemes else "no scheme"
    only = " only" if len(supported_schemes) == 1 else ""
    return (
        f"Theme '{wanted}' is not supported by the template '{template_name}' "
        f"({offered}{only}). Pick a supported theme, link a template that supports "
        f"'{wanted}', or unlink the template."
    )


# ── Per-slide themes against the template ────────────────────────────
#
# A slide's own theme is held to the deck's template exactly like the deck
# theme: SETTING one the template cannot dress is refused (the write names the
# slide and the field), and a TEMPLATE CHANGE that cannot dress an overridden
# slide snaps that slide to the template's scheme the way the deck is snapped -
# the snap stated, the slides listed. Pure functions over a spec dict; the
# write paths (the agent tools, the REST PATCH) call them where they already
# call ``theme_fits_schemes`` / ``fit_theme_to_schemes`` for the deck.


def unsupported_slide_themes(spec: Mapping[str, Any] | None, supported_schemes: Sequence[str]) -> list[tuple[int, str]]:
    """``(0-based index, scheme)`` of every slide whose own theme *supported_schemes* cannot dress.

    An empty *supported_schemes* supports nothing, like :func:`theme_fits_schemes`.
    """
    return [
        (index, scheme)
        for index, scheme in slide_theme_overrides(dict(spec or {}))
        if scheme not in supported_schemes
    ]


def introduced_slide_themes(
    baseline: Mapping[str, Any] | None,
    spec: Mapping[str, Any] | None,
    offending: Sequence[tuple[int, str]],
) -> list[tuple[int, str]]:
    """The *offending* ``(index, scheme)`` of *spec* that *baseline* did not already carry.

    A write under the deck's EXISTING template refuses only the slide theme it
    INTRODUCES: an older deck may hold an override the template never
    supported (the template narrowed its schemes after the save), and a title
    edit or a reorder of that slide is not a request for the scheme. The
    comparison is by the slide's STABLE ``id``, never by its position - a
    reorder moves the slide, a replacement at the same position is a new slide
    - and falls back to the position only where the STORED slide carries no id
    (a row older than slide ids). A slide with no id in *spec* is new
    otherwise, so its theme is introduced even at a position whose stored
    slide happened to carry the same scheme.
    """
    return _introduced_slide_values(baseline, spec, offending, lambda deck: slide_theme_overrides(dict(deck)))


def _introduced_slide_values(
    baseline: Mapping[str, Any] | None,
    spec: Mapping[str, Any] | None,
    offending: Sequence[tuple[int, str]],
    values_of: Callable[[Mapping[str, Any]], Sequence[tuple[int, str]]],
) -> list[tuple[int, str]]:
    """The *offending* ``(index, value)`` of *spec* that *baseline* did not already carry.

    *values_of* reads the per-slide values of a deck (a slide theme, a cover's
    hero); the comparison is the one :func:`introduced_slide_themes` documents.
    """
    stored_slides = (baseline or {}).get("slides") or []
    by_id: dict[str, str] = {}
    by_index: dict[int, str] = {}
    for index, value in values_of(baseline or {}):
        stored = stored_slides[index] if index < len(stored_slides) else None
        stored_id = stored.get("id") if isinstance(stored, dict) else None
        if stored_id:
            by_id[str(stored_id)] = value
        else:
            by_index[index] = value
    slides = (spec or {}).get("slides") or []
    introduced: list[tuple[int, str]] = []
    for index, value in offending:
        slide = slides[index] if index < len(slides) else None
        slide_id = slide.get("id") if isinstance(slide, dict) else None
        already = by_id.get(str(slide_id)) if slide_id else by_index.get(index)
        if already != value:
            introduced.append((index, value))
    return introduced


def fit_slide_themes_to_schemes(spec: dict, supported_schemes: Sequence[str]) -> list[tuple[int, str, str]]:
    """Snap every overridden slide the template cannot dress, IN PLACE; return ``(index, from, to)``.

    The "template wins" rule of :func:`fit_theme_to_schemes` applied slide by
    slide: a slide whose scheme the template supports keeps it, one it cannot
    dress takes the template's first scheme. A slide that inherits is never
    touched - it follows the deck, which the caller snaps separately. Nothing
    is snapped when *supported_schemes* is empty (there is nothing to snap to).
    """
    if not supported_schemes:
        return []
    snapped: list[tuple[int, str, str]] = []
    slides = spec.get("slides") or []
    for index, scheme in unsupported_slide_themes(spec, supported_schemes):
        target = supported_schemes[0]
        slides[index]["theme"] = target
        snapped.append((index, scheme, target))
    return snapped


def unsupported_slide_theme_message(
    unsupported: Sequence[tuple[int, str]],
    *,
    spec: Mapping[str, Any] | None,
    template_name: str,
    supported_schemes: Sequence[str],
) -> str:
    """The one sentence for slide themes a template cannot dress: ``slide 3 theme 'light' ...``.

    Names every offending slide by its 1-based number and the field, so an
    agent patches THAT slide (``update_slide {theme: null}`` or a supported
    scheme) and a person finds it in the panel.
    """
    offered = " / ".join(supported_schemes) if supported_schemes else "no scheme"
    only = " only" if len(supported_schemes) == 1 else ""
    noun = unit_noun(dict(spec or {}), 1)
    where = "; ".join(f"{noun} {index + 1} theme '{scheme}'" for index, scheme in unsupported)
    return (
        f"{where[:1].upper()}{where[1:]}: not supported by the template '{template_name}' "
        f"({offered}{only}). Set a supported theme on that {noun}, remove its theme so it "
        "inherits the deck's, link a template that supports it, or unlink the template."
    )


def slide_theme_snap_note(
    snapped: Sequence[tuple[int, str, str]], *, spec: Mapping[str, Any] | None, template_name: str
) -> str:
    """The result line for slides a template change snapped, ``""`` when none were."""
    if not snapped:
        return ""
    noun = unit_noun(dict(spec or {}), len(snapped))
    moves = "; ".join(f"{index + 1}: '{before}' -> '{after}'" for index, before, after in snapped)
    own, were = ("its own", "was") if len(snapped) == 1 else ("their own", "were")
    return (
        f"{len(snapped)} {noun} kept a theme of {own} that the template '{template_name}' "
        f"cannot dress and {were} snapped ({moves})."
    )


def deck_theme_change_note(spec: Mapping[str, Any] | None) -> str:
    """How a deck theme change landed on the slides: how many followed, which kept their own.

    ``update_deck {theme}`` changes only the slides that inherit; an overridden
    slide is not touched, not rewritten, not normalised - and the result says
    so, or the agent reports a whole deck turned light while the cover stayed
    dark on purpose.
    """
    spec = dict(spec or {})
    slides = spec.get("slides") or []
    overrides = slide_theme_overrides(spec)
    followed = len(slides) - len(overrides)
    theme = theme_scheme(spec.get("theme") if isinstance(spec.get("theme"), str) else None)
    line = f"Deck theme '{theme}': {followed} {unit_noun(spec, followed)} inherited the change"
    if overrides:
        kept = ", ".join(f"{index + 1} ({scheme})" for index, scheme in overrides)
        own, left = ("its own", "was left as it is") if len(overrides) == 1 else ("their own", "were left as they are")
        line += (
            f"; {len(overrides)} kept {own} theme and {left}: "
            f"{unit_noun(spec, len(overrides))} {kept} (update_slide {{theme: null}} gives one back to the deck)"
        )
    return line + "."


# ── The covers a template offers ─────────────────────────────────────
#
# Every deck may wear the shared heroes (``SHARED_HEROES``). The tiled ones
# (``TEMPLATE_HEROES``) are a brand's own artwork, so a deck wears one only
# under a template that declares it in ``extra_metadata.heroes``; a free deck
# offers the shared ones. A cover naming a hero its deck's template does not
# offer is drawn as that template's own default for the cover when it offers
# it, else as ``gradient`` - on read (the spec a reader is served), and on a
# write the way a slide theme is held to the template: a write that SETS one
# under the deck's existing template is refused, a new deck or a template
# change snaps it and says so.

#: The ``extra_metadata`` key a template declares its own covers under.
TEMPLATE_HEROES_KEY = "heroes"
#: The cover a hero the template does not offer falls back to when the
#: template sets no default of its own for that cover.
FALLBACK_COVER_HERO = "gradient"


def template_heroes(extra_metadata: object) -> tuple[str, ...]:
    """The heroes a deck of this template may wear, in catalog order.

    The shared heroes, plus the template-only covers ``extra_metadata.heroes``
    declares; an unknown or shared name in the declaration is ignored.
    """
    declared = extra_metadata.get(TEMPLATE_HEROES_KEY) if isinstance(extra_metadata, dict) else None
    own = (
        {value.strip().lower() for value in declared if isinstance(value, str)}
        if isinstance(declared, (list, tuple))
        else set()
    )
    return tuple(hero for hero in HERO_VARIANTS if hero in SHARED_HEROES or (hero in TEMPLATE_HEROES and hero in own))


def slide_heroes(spec: Mapping[str, Any] | None) -> list[tuple[int, str]]:
    """``(0-based index, hero)`` of every title / closing slide that names a hero."""
    slides = (spec or {}).get("slides") or []
    return [
        (index, slide["hero"])
        for index, slide in enumerate(slides)
        if isinstance(slide, dict)
        and slide.get("layout") in DECK_SLIDE_DEFAULT_LAYOUTS
        and isinstance(slide.get("hero"), str)
        and slide["hero"]
    ]


def cover_hero(layout: object, heroes: Sequence[str], hero_defaults: Mapping[str, str] | None) -> str:
    """The hero a *layout* cover is drawn with when its own is not offered."""
    default = (hero_defaults or {}).get(str(layout))
    return default if default in heroes else FALLBACK_COVER_HERO


def unsupported_slide_heroes(spec: Mapping[str, Any] | None, heroes: Sequence[str]) -> list[tuple[int, str]]:
    """``(0-based index, hero)`` of every cover whose hero *heroes* does not offer."""
    return [(index, hero) for index, hero in slide_heroes(spec) if hero not in heroes]


def introduced_slide_heroes(
    baseline: Mapping[str, Any] | None,
    spec: Mapping[str, Any] | None,
    offending: Sequence[tuple[int, str]],
) -> list[tuple[int, str]]:
    """The *offending* covers of *spec* whose hero *baseline* did not already carry (by slide id)."""
    return _introduced_slide_values(baseline, spec, offending, slide_heroes)


def fit_slide_heroes(
    spec: dict, heroes: Sequence[str], hero_defaults: Mapping[str, str] | None
) -> list[tuple[int, str, str]]:
    """Snap every cover whose hero *heroes* does not offer, IN PLACE; return ``(index, from, to)``.

    A snapped slide is replaced by a copy, so a slide dict shared with a
    stored spec is never mutated.
    """
    slides = spec.get("slides") or []
    snapped: list[tuple[int, str, str]] = []
    for index, hero in unsupported_slide_heroes(spec, heroes):
        target = cover_hero(slides[index].get("layout"), heroes, hero_defaults)
        slides[index] = {**slides[index], "hero": target}
        snapped.append((index, hero, target))
    return snapped


def served_hero_spec(spec: Any, heroes: Sequence[str], hero_defaults: Mapping[str, str] | None) -> Any:
    """*spec* as a reader sees it: every cover drawn with a hero its template offers.

    *spec* itself when nothing needs fitting, else a copy - the stored row is
    left alone, and the next save writes what the reader saw.
    """
    if not isinstance(spec, dict) or not unsupported_slide_heroes(spec, heroes):
        return spec
    served = {**spec, "slides": list(spec.get("slides") or [])}
    fit_slide_heroes(served, heroes, hero_defaults)
    return served


def _template_label(template_name: str | None) -> str:
    return f"the template '{template_name}'" if template_name else "a deck without a template"


def unsupported_slide_hero_message(
    unsupported: Sequence[tuple[int, str]],
    *,
    spec: Mapping[str, Any] | None,
    template_name: str | None,
    heroes: Sequence[str],
) -> str:
    """The one sentence for covers whose hero the deck's template does not offer: ``slide 1 hero 'mosaic' ...``."""
    noun = unit_noun(dict(spec or {}), 1)
    where = "; ".join(f"{noun} {index + 1} hero '{hero}'" for index, hero in unsupported)
    return (
        f"{where[:1].upper()}{where[1:]}: a cover {_template_label(template_name)} does not offer "
        f"({' / '.join(heroes)}). Pick one of those, or link a template whose own covers include it."
    )


def slide_hero_snap_note(
    snapped: Sequence[tuple[int, str, str]], *, spec: Mapping[str, Any] | None, template_name: str | None
) -> str:
    """The result line for covers drawn with another hero than asked, ``""`` when none were."""
    if not snapped:
        return ""
    noun = unit_noun(dict(spec or {}), len(snapped))
    moves = "; ".join(f"{index + 1}: '{before}' -> '{after}'" for index, before, after in snapped)
    takes = "it takes" if len(snapped) == 1 else "they take"
    return (
        f"Hero: {len(snapped)} {noun} asked for a cover {_template_label(template_name)} does not "
        f"offer; {takes} the default cover instead ({moves})."
    )


@dataclass(frozen=True, slots=True)
class TemplateFacts:
    """What a write path needs to know about a template before linking it.

    Loaded once per save by :func:`load_visible_template_facts`; the palette
    itself is resolved on READ (:func:`resolve_template_palette`) so a template
    edit restyles every deck it dresses without a re-save.
    """

    id: uuid.UUID
    name: str
    kind: str
    supported_schemes: tuple[str, ...]
    #: :func:`template_deck_defaults`: the deck keys (strings) and the
    #: slide-level ``hero_defaults`` / ``slide_themes`` objects.
    deck_defaults: dict[str, Any]
    #: The footer / header pattern the template draws on every deck,
    #: ``None`` when it declares none (the deck's own v1 footer then shows).
    chrome: TemplateChrome | None = None
    #: How many slides a deck of this template may hold
    #: (:func:`template_slide_cap`): what every authoring save and
    #: ``insert_slide`` are held to.
    slide_cap: int = DEFAULT_SLIDE_CAP
    #: The heroes its covers may wear (:func:`template_heroes`).
    heroes: tuple[str, ...] = SHARED_HEROES

    @property
    def hero_defaults(self) -> Mapping[str, str]:
        """The hero each cover of a new deck opens on, among :attr:`heroes`."""
        return self.deck_defaults.get("hero_defaults") or {}

    @property
    def is_presentation_template(self) -> bool:
        # The same reading of a kind as every other surface (trimmed,
        # case-insensitive), so a legacy row spelt ``Presentation`` links.
        from app.services.template_containers import is_presentation_kind

        return is_presentation_kind(self.kind)


def template_facts(container) -> TemplateFacts:
    """The :class:`TemplateFacts` of a loaded ``TemplateContainer`` row."""
    return TemplateFacts(
        id=container.id,
        name=str(getattr(container, "name", "") or "").strip() or "template",
        kind=str(getattr(container, "kind", "") or ""),
        supported_schemes=tuple(
            template_supported_schemes(container.theme_config, container.extra_metadata)
        ),
        deck_defaults=template_deck_defaults(container.extra_metadata),
        chrome=template_chrome(container.extra_metadata),
        slide_cap=template_slide_cap(container.extra_metadata),
        heroes=template_heroes(container.extra_metadata),
    )


def deck_heroes(facts: TemplateFacts | None) -> tuple[tuple[str, ...], Mapping[str, str]]:
    """The heroes a deck under *facts* may wear and its covers' defaults; the shared ones for a free deck."""
    return (facts.heroes, facts.hero_defaults) if facts is not None else (SHARED_HEROES, {})


async def load_visible_template_facts(
    db: AsyncSession,
    template_id: uuid.UUID | None,
    *,
    user_id: uuid.UUID | None,
    is_admin: bool = False,
) -> TemplateFacts | None:
    """The facts of *template_id* as *user_id* may see it, or ``None`` (not found OR not visible).

    The ONLY facts loader, on purpose: every write path - the REST edit, where
    the id is typed by a person, AND the agent tool, where the model passes
    whatever UUID it holds - resolves a template through the same visibility
    as :func:`resolve_template_palette` (own + group-shared + company-managed).
    An unchecked lookup would let a deck be linked to another user's private
    template and echo its name, its colour schemes and its ``deck_defaults``
    back into the tool result. The REST edit passes *is_admin* for an admin,
    who edits a colleague's deck as its owner does; the agent tool never does.
    ``None`` for ``template_id`` is the free deck and loads nothing.
    """
    if template_id is None:
        return None
    container = await _load_visible_template(
        db, template_id, user_id=user_id, is_admin=is_admin
    )
    return template_facts(container) if container is not None else None


#: What a caller may do with a deck, as reported by :func:`deck_permission`.
#: ``owner`` and ``config`` may edit (REST + agent tool); ``read_only`` may
#: present, export and duplicate; ``None`` means the deck is not visible.
DeckPermission = str


def deck_permission(
    presentation: Presentation,
    *,
    user_id: uuid.UUID | str | None,
    user_group_ids: list[str] | None = None,
    is_admin: bool = False,
) -> DeckPermission | None:
    """Resolve the acting user's tier on a deck.

    * the owner → ``owner``;
    * an admin → ``owner`` on a company-managed deck (the only people who may
      change a published deck) and ``config`` on anyone else's personal deck
      when *is_admin* is passed — the REST surface passes it, the agent tool
      does NOT (see :func:`load_writable_presentation`);
    * a ``config`` / ``config_with_chats`` grant, by user or through a group →
      ``config``;
    * any other grant, or a company-managed deck → ``read_only``;
    * otherwise ``None``.

    Single source of truth for the REST serializer's ``can_manage`` /
    ``permission`` and for the tool's writable-deck check, so the UI can never
    offer an edit the tool would refuse.
    """
    from app.api.v1.helpers import (
        SHARE_PERMISSIONS_CONFIG,
        get_effective_permission,
    )

    canonical_user = str(user_id) if user_id is not None else None
    owner = str(presentation.user_id) if presentation.user_id else None
    if canonical_user is not None and owner == canonical_user:
        return "owner"
    if presentation.is_company_managed:
        # Published deck: readable by everyone the visibility filter admits,
        # changeable by admins only — like every company-managed entity.
        return "owner" if is_admin else "read_only"
    if is_admin:
        return "config"
    perm = get_effective_permission(presentation, canonical_user, user_group_ids)
    if perm is None:
        return None
    return "config" if perm in SHARE_PERMISSIONS_CONFIG else "read_only"


def serialize_presentation(
    presentation: Presentation,
    *,
    theme_config: dict[str, str] | None = None,
    theme_configs: dict[str, dict[str, str]] | None = None,
    owner_name: str | None = None,
    permission: DeckPermission | None = None,
    include_spec: bool = True,
    updated_by_name: str | None = None,
    open_comment_count: int = 0,
    supported_schemes: Sequence[str] | None = None,
    template_name: str | None = None,
    template_chrome: TemplateChrome | None = None,
    template_design: TemplateDesign | None = None,
    supported_layouts: Sequence[str] | None = None,
    asset_meta: dict[str, dict] | None = None,
    picture_labels: Mapping[str, Mapping[str, str | None]] | None = None,
    has_public_link: bool = False,
    slide_cap: int = DEFAULT_SLIDE_CAP,
    supported_heroes: Sequence[str] | None = None,
    hero_defaults: Mapping[str, str] | None = None,
) -> dict:
    """Serialize a deck to the JSON-safe payload the renderer consumes.

    *permission* is the caller's tier from :func:`deck_permission`; it drives
    the ``can_manage`` flag the UI gates its edit / share / delete controls on.
    ``include_spec=False`` produces the list-view shape (everything but the
    potentially large spec).

    *updated_by_name* and *open_comment_count* are the collaboration facts a
    card shows without opening the deck: who saved last, and how many
    review threads are still open. Both are resolved by the caller in one
    batch query per list, never per row.

    ``aspect_ratio`` and ``kind`` (``deck`` / ``page``) are lifted out of the
    spec onto the payload so the list view — which never carries the spec —
    can badge a page as a page and the viewer can pick the document view
    before the spec is even parsed.

    *theme_config* is the template palette resolved for the deck's SAVED theme
    — what a reader renders. *theme_configs* is the same palette per colour
    scheme (``{"dark": …, "light": …}``, :func:`resolve_template_themes`): the
    editor previews a theme switch from it, on the client, before anything is
    saved. Without it the preview of ``theme: light`` would lay the light
    renderer tokens under the template's still-dark inline palette — and the
    inline palette wins, so the deck would merely CALL itself light.

    *supported_schemes* and *template_name* describe the linked
    template as the reader sees it: the schemes its theme picker may offer
    (``None`` — a free deck, or an invisible template — offers both; detail
    shape only, like the spec) and the name the editor and the library card
    label the template with (both shapes; ``None`` reads as a free deck).
    *template_chrome* is the footer / header pattern that template
    declares, served as ``template_chrome`` on the detail shape so the renderer
    fills it from the deck (title, opening subtitle, event, ``chrome.fields``)
    exactly as the export and the tool read-back do; ``None`` keeps the deck's
    own footer. *template_design* (``presentation_design``) is the
    template's design tokens, served as ``template_design`` on the detail
    shape; ``None`` keeps the renderer's own look. *supported_layouts* is the template's advertised
    layout catalog restricted to the current layouts
    (:func:`template_supported_layouts`) — what the editor's slide layout
    picker offers; ``None`` (a free deck, an invisible template) offers the
    whole current catalog. Detail shape only, like the schemes.
    *asset_meta* (:func:`load_logo_asset_meta`) is the stored geometry
    of the logos the deck draws - ``{file_id: {w, h, trim[, ink]}}`` - so the
    renderer crops each logo to its mark before the image loads; a logo with
    none is drawn as its file. Detail shape only.
    *has_public_link* (``presentation_public_links.public_deck_ids``)
    says a public link opens the deck now; both shapes, resolved by the caller.
    *slide_cap* (``TemplatePalette.slide_cap``) is served as
    ``max_slides`` on the detail shape: how many slides the deck may hold under
    its template, what the editor's "Add slide" and Duplicate stop at.
    *supported_heroes* (``TemplatePalette.heroes``) are the covers the
    hero picker offers, served on the detail shape; when given, a cover whose
    hero is not among them is served drawn with *hero_defaults* for that
    cover or ``gradient`` (:func:`served_hero_spec`).

    The theme is served NORMALISED (``filigran`` reads as ``dark``, in the
    payload AND the spec's ``theme`` key) so no client ever sees the retired
    preset name; the stored row is left alone — the next save rewrites it.
    """
    spec = presentation.spec or {}
    theme = normalize_theme(presentation.theme)
    if not isinstance(theme, str) or theme not in THEME_SCHEMES:
        theme = theme_scheme(presentation.theme)
    if isinstance(spec, dict) and spec.get("theme") not in (None, theme):
        spec = {**spec, "theme": theme}
    if supported_heroes is not None:
        spec = served_hero_spec(spec, supported_heroes, hero_defaults)
    aspect_ratio = normalize_aspect_ratio(spec.get("aspect_ratio")) or "16:9"
    payload = {
        "id": str(presentation.id),
        "title": presentation.title,
        "theme": theme,
        "aspect_ratio": aspect_ratio,
        "kind": presentation_kind(aspect_ratio),
        "slide_count": presentation.slide_count,
        "revision": presentation.revision,
        "user_id": str(presentation.user_id) if presentation.user_id else None,
        "owner_name": owner_name,
        "conversation_id": (
            str(presentation.conversation_id) if presentation.conversation_id else None
        ),
        "assignment_run_id": (
            str(presentation.assignment_run_id)
            if presentation.assignment_run_id
            else None
        ),
        "template_container_id": (
            str(presentation.template_container_id)
            if presentation.template_container_id
            else None
        ),
        "is_company_managed": bool(presentation.is_company_managed),
        "shared_group_ids": presentation.shared_group_ids,
        "shared_group_permissions": presentation.shared_group_permissions,
        "shared_user_ids": presentation.shared_user_ids,
        "shared_user_permissions": presentation.shared_user_permissions,
        "access_restricted": bool(presentation.access_restricted),
        "permission": permission,
        "can_manage": permission in ("owner", "config"),
        "has_public_link": bool(has_public_link),
        "updated_by_user_id": (
            str(presentation.updated_by_user_id)
            if presentation.updated_by_user_id
            else None
        ),
        "updated_by_name": updated_by_name,
        "open_comment_count": int(open_comment_count or 0),
        # The template the deck WEARS, by name, on both shapes: the
        # library card says "Filigran Brand Deck - Dark" instead of a bare
        # scheme, and a free deck reads ``None``. Resolved by the caller (one
        # batch per list, the palette load per read), never per row here.
        "template_name": template_name,
        "created_at": (
            presentation.created_at.isoformat() if presentation.created_at else None
        ),
        "updated_at": (
            presentation.updated_at.isoformat() if presentation.updated_at else None
        ),
    }
    if include_spec:
        payload["spec"] = spec
        payload["theme_config"] = theme_config or {}
        # A caller that resolved no per-scheme map emits an EMPTY map, not one
        # of empty palettes: the renderer falls back to ``theme_config`` for a
        # scheme it cannot find, whereas ``{"dark": {}}`` would read as "this
        # template has no dark palette" and strip the brand off the deck.
        payload["theme_configs"] = (
            {scheme: dict(theme_configs.get(scheme) or {}) for scheme in THEME_SCHEMES}
            if theme_configs is not None
            else {}
        )
        # The schemes the theme picker may offer: a free deck offers both, a
        # template only what it can dress. Canonical order, always a
        # real list - a picker with no option to offer is the template's bug,
        # never a blank control.
        payload["supported_schemes"] = (
            [scheme for scheme in THEME_SCHEMES if scheme in supported_schemes]
            if supported_schemes is not None
            else list(THEME_SCHEMES)
        ) or list(THEME_SCHEMES)
        # The footer / header pattern the deck's template draws,
        # resolved live like the palette: ``None`` for a free deck, an
        # invisible template, or a template that declares none - the renderer
        # then draws the deck's own event label + footer text.
        payload["template_chrome"] = template_chrome.as_dict() if template_chrome is not None else None
        # The template's design tokens: how flat its blocks are drawn,
        # its radius and spacing scale. ``None`` (no declaration) keeps the
        # renderer's own look.
        payload["template_design"] = template_design.as_dict() if template_design is not None else None
        # The layouts the slide picker may move a slide to: the
        # template's advertised catalog restricted to the platform's, the
        # whole catalog for a free deck or an invisible template. Catalog
        # order, never empty - same contract as the schemes above.
        payload["supported_layouts"] = (
            [layout for layout in SLIDE_LAYOUTS if layout in supported_layouts]
            if supported_layouts is not None
            else list(SLIDE_LAYOUTS)
        ) or list(SLIDE_LAYOUTS)
        # The covers the hero picker may offer: the shared heroes,
        # plus the template's own. Catalog order.
        payload["supported_heroes"] = [
            hero for hero in HERO_VARIANTS if hero in (supported_heroes or SHARED_HEROES)
        ]
        payload["asset_meta"] = dict(asset_meta or {})
        # How many slides the deck may hold: the template's
        # ``max_slides`` as the reader sees the template (an invisible one
        # reads as the default, like its palette reads as none). A deck
        # already over it is served its cap all the same: the editor refuses
        # one more slide, and the save keeps the deck at its size
        # (``effective_slide_cap``).
        payload["max_slides"] = clamp_slide_cap(slide_cap)
        # What a quote picture no visible credit names is announced as
        # (`load_quote_picture_labels`): the file's description or filename,
        # for the quote pictures the spec references only. Detail shape only.
        payload["picture_labels"] = {
            file_id: {"description": label.get("description"), "filename": label.get("filename")}
            for file_id, label in (picture_labels or {}).items()
        }
    return payload


async def load_logo_asset_meta(db: AsyncSession, spec: object) -> dict[str, dict]:
    """The stored geometry of every logo *spec* draws, keyed by file id.

    One query over the logo file ids of the deck (``logo_file_ids``); a logo
    whose geometry was never read, or read as unreadable, is simply absent -
    the renderer then draws it from the file's own size. The geometry is a
    property of the bytes (a box and an opacity), served to whoever reads the
    deck: the deck already names the file, and its readers may fetch it.
    """
    from app.models.file_attachment import FileAttachment

    # Keyed by the id AS THE SPEC SPELLS IT: the renderer looks a logo up by
    # its item's `file_id`, whatever case or hyphenation the agent wrote.
    spelled: dict[uuid.UUID, list[str]] = {}
    for raw in logo_file_ids(spec):
        try:
            spelled.setdefault(uuid.UUID(raw), []).append(raw)
        except (TypeError, ValueError):
            continue
    if not spelled:
        return {}
    rows = await db.execute(
        select(FileAttachment.id, FileAttachment.image_meta, FileAttachment.s3_object_name).where(
            FileAttachment.id.in_(list(spelled))
        )
    )
    out: dict[str, dict] = {}
    for file_id, meta, storage_key in rows.all():
        # `rev` changes when the file is replaced in place (same id, new
        # bytes): the renderer versions the logo's URL by it.
        geometry = render_geometry(meta, storage_key=storage_key)
        if geometry is not None:
            for raw in spelled.get(file_id, []):
                out[raw] = geometry
    return out


async def _load_visible_template(
    db: AsyncSession,
    container_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None,
    is_admin: bool = False,
):
    """The template container *container_id* as *user_id* may see it, or ``None``.

    Visibility is re-checked on every read (the container may have been
    un-shared since the deck was generated) and a container the caller can no
    longer see simply contributes nothing — the deck still renders with its
    built-in theme rather than 404-ing. This is the part of theme resolution
    that must not be duplicated: a second copy could fall behind this one and
    start leaking an un-shared container's brand config.

    *is_admin* is the REST surface's admin bypass: an admin opening a
    colleague's deck by id sees it dressed in its template, as its owner does.
    The agent tools never pass it.
    """
    from app.api.v1.helpers import (
        caller_visible_filter,
        get_user_group_ids,
        user_visible_filter,
    )
    from app.models.template_container import TemplateContainer

    try:
        if user_id is not None and is_admin:
            query = select(TemplateContainer).where(
                user_visible_filter(TemplateContainer, user_id, is_admin=True)
            )
        else:
            group_ids = await get_user_group_ids(db, user_id) if user_id is not None else []
            query = select(TemplateContainer).where(
                caller_visible_filter(TemplateContainer, user_id, group_ids)
            )
        result = await db.execute(query.where(TemplateContainer.id == container_id))
        return result.scalar_one_or_none()
    except Exception:  # pragma: no cover - defensive, theme is decorative
        logger.warning("Could not resolve template theme %s", container_id, exc_info=True)
        return None


async def resolve_template_names(
    db: AsyncSession,
    container_ids: Sequence[uuid.UUID | None],
    *,
    user_id: uuid.UUID | None,
    user_group_ids: list[str] | None = None,
) -> dict[str, str]:
    """The names of the templates in *container_ids* that *user_id* may see.

    One query for a whole list of decks - the library card names the template a
    deck wears without a per-row load. A template the reader cannot see is
    simply absent (the card then shows the deck as free, exactly as its own
    palette resolution does): the SAME visibility predicate as
    :func:`_load_visible_template`, so a list can never name a template a read
    would refuse to dress the deck with.
    """
    from app.api.v1.helpers import caller_visible_filter
    from app.models.template_container import TemplateContainer

    wanted = {cid for cid in container_ids if cid is not None}
    if not wanted:
        return {}
    try:
        visible = caller_visible_filter(TemplateContainer, user_id, user_group_ids or [])
        result = await db.execute(
            select(TemplateContainer.id, TemplateContainer.name)
            .where(visible)
            .where(TemplateContainer.id.in_(list(wanted)))
        )
        return {
            str(cid): (str(name or "").strip() or "template") for cid, name in result.all()
        }
    except Exception:  # pragma: no cover - defensive, the name is decorative
        logger.warning("Could not resolve template names", exc_info=True)
        return {}


async def _fill_role_logos(
    db: AsyncSession,
    container,
    themes: list[dict[str, str]],
    *,
    keys: tuple[str, ...],
) -> None:
    """Resolve the logo file-id keys every palette in *themes* leaves unset, in place.

    The logo the deck chrome draws is resolved by ASSET ROLE when the theme
    config does not name one: the container's ``logo`` asset (and
    ``logo_light`` for the light theme). Uploading a logo in the Templates UI is
    therefore enough on its own — an admin never has to copy a file id into
    ``theme_config`` for it to show up — and the seeded brand containers carry
    no file ids in code at all, since the ids only exist once the assets are
    uploaded. An explicit ``logo_file_id`` still wins, so a template can point
    the chrome at a different asset than the one tagged ``logo``.

    One lookup serves every palette: a role is a property of the container, not
    of a colour scheme.
    """
    from app.services.template_containers import load_role_asset_ids

    missing_logo_keys = [
        k for k in THEME_FILE_ID_KEYS if k in keys and any(k not in theme for theme in themes)
    ]
    if not missing_logo_keys:
        return
    try:
        role_ids = await load_role_asset_ids(
            db, container.id, [THEME_FILE_ID_ROLES[k] for k in missing_logo_keys]
        )
    except Exception:  # pragma: no cover - defensive, the logo is decorative
        logger.warning("Could not resolve logo assets of %s", container.id, exc_info=True)
        role_ids = {}
    for theme in themes:
        for key in missing_logo_keys:
            asset_id = role_ids.get(THEME_FILE_ID_ROLES[key])
            if key not in theme and asset_id is not None:
                theme[key] = str(asset_id)


async def resolve_template_theme(
    db: AsyncSession,
    container_id: uuid.UUID | None,
    *,
    user_id: uuid.UUID | None,
    keys: tuple[str, ...] = THEME_CONFIG_KEYS,
    deck_theme: str | None = None,
    is_admin: bool = False,
) -> dict[str, str]:
    """Return the whitelisted ``theme_config`` of a deck's linked template.

    Visibility is re-checked on every read (:func:`_load_visible_template`); a
    container the caller can no longer see contributes nothing.

    Shared with reports, which pass their own wider *keys* whitelist.

    *deck_theme* is the deck's own theme name. When given, the palette is the
    one :func:`scheme_palette` builds for that theme's scheme: the template's
    ``theme_variants`` entry (see :data:`THEME_VARIANTS_KEY`) laid over the base
    ``theme_config``, minus the base's scheme-bound surfaces when it is designed
    for the other scheme — so a brand template brands a ``light`` deck light
    whether or not it ships a light palette. Reports pass nothing and get the
    one palette, as before. A deck read wants BOTH schemes at once —
    :func:`resolve_template_themes`.

    Logo file ids the config leaves unset are resolved by asset role
    (:func:`_fill_role_logos`). *is_admin*: see :func:`_load_visible_template`.
    """
    if container_id is None:
        return {}
    container = await _load_visible_template(
        db, container_id, user_id=user_id, is_admin=is_admin
    )
    if container is None:
        return {}
    theme = sanitize_theme_config(container.theme_config, keys=keys)
    if deck_theme is not None:
        scheme = theme_scheme(deck_theme)
        theme = scheme_palette(
            theme,
            template_theme_variant(container.extra_metadata, scheme, keys=keys),
            scheme,
        )
    await _fill_role_logos(db, container, [theme], keys=keys)
    return theme


async def resolve_template_export_stamp(
    db: AsyncSession,
    container_id: uuid.UUID | None,
    *,
    user_id: uuid.UUID | None,
    keys: tuple[str, ...] = THEME_CONFIG_KEYS,
    deck_theme: str | None = None,
) -> str:
    """What a document's export depends on besides its own revision.

    A deck or report wears its template live: editing the template restyles
    the document without a new revision. The export tools keep each file by
    document, revision, format and this stamp, so a file is handed back again
    only while the template is exactly what the render would resolve now. The
    stamp is a digest of what the render reads from the template: the palette
    :func:`resolve_template_theme` resolves (theme config, scheme variant, role
    logos), the ``theme_config`` and ``extra_metadata`` it comes from (the
    chrome pattern, the heroes, the deck defaults the palette does not carry),
    and every asset's id, role, description (a picture's alt text in the
    file) and version (``file_version``, which moves when an asset is replaced
    in place on its own row). It is not the container's
    ``updated_at``: that misses an asset replaced in place and moves on the
    ``lookup_count`` analytics, which change nothing the render reads.

    Empty when the document has no template or the caller may not see it (the
    render then dresses it in the built-in theme, which has no version).
    """
    if container_id is None:
        return ""
    container = await _load_visible_template(db, container_id, user_id=user_id)
    if container is None:
        return ""
    from app.services.file_responses import file_version
    from app.services.template_containers import load_assets

    palette = await resolve_template_theme(
        db, container_id, user_id=user_id, keys=keys, deck_theme=deck_theme
    )
    assets = sorted(
        (
            str(asset.id),
            asset.asset_role or "",
            (asset.description or "").strip(),
            file_version(asset),
        )
        for asset in await load_assets(db, container.id)
    )
    render_inputs = {
        "palette": palette,
        "theme_config": container.theme_config or {},
        "extra_metadata": container.extra_metadata or {},
        "assets": assets,
    }
    return hashlib.sha256(
        json.dumps(render_inputs, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


async def resolve_template_themes(
    db: AsyncSession,
    container_id: uuid.UUID | None,
    *,
    user_id: uuid.UUID | None,
    keys: tuple[str, ...] = THEME_CONFIG_KEYS,
) -> dict[str, dict[str, str]]:
    """The template palette of a deck for EVERY colour scheme, keyed by scheme.

    ``{"dark": scheme_palette(base, dark variant, "dark"), "light": ...}`` —
    each entry is exactly what :func:`resolve_template_theme` returns for a deck
    theme of that scheme, computed from one container load and one logo-role
    lookup. Both entries are ``{}`` when the deck has no template or the caller
    may not see it.

    This is what lets the editor preview a theme switch without a round trip:
    the deck response carries the palette of the saved theme AND this
    map, and the renderer picks ``theme_configs[theme_scheme(spec.theme)]`` for
    whatever theme is on screen — the saved one for a reader, the draft's for
    an editor. Resolving on the client instead would mean re-deriving the
    visibility check and the role lookup there, which is the duplication the
    single resolver exists to prevent.
    """
    return (await resolve_template_palette(db, container_id, user_id=user_id, keys=keys)).themes


@dataclass(frozen=True, slots=True)
class TemplatePalette:
    """Everything a deck READ learns from its template, from one container load.

    ``themes`` is :func:`resolve_template_themes`'s per-scheme map;
    ``supported_schemes`` the schemes the template can dress (``None`` when the
    deck is free or the template invisible — the picker then offers both);
    ``name`` the template's name for the editor's label (``None`` likewise);
    ``chrome`` the footer / header pattern it draws on every slide
    (``None`` when it declares none); ``supported_layouts`` the layouts its
    catalog lets the slide picker offer (``None`` for a free deck or an
    invisible template — the picker then offers the whole current catalog);
    ``slide_cap`` how many slides the deck may hold (the template's
    ``max_slides``; the default for a free deck or an invisible template).
    ``heroes`` the covers it offers and ``hero_defaults`` the hero each cover
    opens on (the shared heroes and none for a free deck or an
    invisible template).
    """

    themes: dict[str, dict[str, str]]
    supported_schemes: tuple[str, ...] | None = None
    name: str | None = None
    chrome: TemplateChrome | None = None
    supported_layouts: tuple[str, ...] | None = None
    slide_cap: int = DEFAULT_SLIDE_CAP
    #: The design tokens it declares (``extra_metadata.design``),
    #: ``None`` when it declares none: the renderer then draws as before.
    design: TemplateDesign | None = None
    heroes: tuple[str, ...] = SHARED_HEROES
    hero_defaults: Mapping[str, str] = field(default_factory=dict)

    def theme_for(self, theme: str | None) -> dict[str, str]:
        """The palette a deck saved with *theme* renders."""
        return self.themes.get(theme_scheme(theme)) or {}


async def resolve_template_palette(
    db: AsyncSession,
    container_id: uuid.UUID | None,
    *,
    user_id: uuid.UUID | None,
    keys: tuple[str, ...] = THEME_CONFIG_KEYS,
    is_admin: bool = False,
) -> TemplatePalette:
    """The per-scheme palettes PLUS the template facts a deck response carries.

    One visibility-checked container load and one logo-role lookup, exactly
    like :func:`resolve_template_themes` (which is this, minus the facts).
    *is_admin*: see :func:`_load_visible_template`.
    """
    empty = TemplatePalette(themes={scheme: {} for scheme in THEME_SCHEMES})
    if container_id is None:
        return empty
    container = await _load_visible_template(
        db, container_id, user_id=user_id, is_admin=is_admin
    )
    if container is None:
        return empty
    base = sanitize_theme_config(container.theme_config, keys=keys)
    themes = {
        scheme: scheme_palette(
            base,
            template_theme_variant(container.extra_metadata, scheme, keys=keys),
            scheme,
        )
        for scheme in THEME_SCHEMES
    }
    await _fill_role_logos(db, container, list(themes.values()), keys=keys)
    return TemplatePalette(
        themes=themes,
        supported_schemes=tuple(
            template_supported_schemes(container.theme_config, container.extra_metadata, keys=keys)
        ),
        name=str(getattr(container, "name", "") or "").strip() or None,
        chrome=template_chrome(container.extra_metadata),
        design=template_design(container.extra_metadata),
        supported_layouts=tuple(template_supported_layouts(getattr(container, "layouts", None))),
        slide_cap=template_slide_cap(container.extra_metadata),
        heroes=template_heroes(container.extra_metadata),
        hero_defaults=template_deck_defaults(container.extra_metadata).get("hero_defaults") or {},
    )


#: The seeded brand containers, in the order they are tried when no explicit
#: platform default is configured: the Filigran brand deck where it ships
#: (Copilot mode only), else the XTM One deck, which ships in every mode.
DEFAULT_TEMPLATE_SLUGS: tuple[str, ...] = ("filigran-brand", "xtm-one-brand")


async def resolve_default_template_id(db: AsyncSession) -> uuid.UUID | None:
    """The template a deck is styled with when the agent names none.

    Resolution order: the admin-set ``PlatformSettings.default_presentation_template_id``
    when it still points at a company-managed container, else the first seeded
    brand container found (``DEFAULT_TEMPLATE_SLUGS``), else ``None`` — the deck
    then renders with the renderer's built-in theme, which is what happened
    before a default existed. A NULL setting deliberately means "the built-in
    default", not "no template": the seed never writes the setting, so an admin
    who clears it gets the shipped brand back rather than unstyled decks.

    The default is only ever a company-managed container (the setting is
    validated that way on write, and the seed produces nothing else), so it is
    visible to every reader — the visibility re-check in
    :func:`resolve_template_theme` can never hide it from the user it was
    applied for.
    """
    from app.models.platform_settings import PlatformSettings
    from app.models.template_container import TemplateContainer
    from app.services.template_container_seed import builtin_container_filter

    settings_row = (await db.execute(select(PlatformSettings).limit(1))).scalar_one_or_none()
    configured = getattr(settings_row, "default_presentation_template_id", None)
    if configured is not None:
        exists = (
            await db.execute(
                select(TemplateContainer.id).where(
                    TemplateContainer.id == configured,
                    TemplateContainer.is_company_managed.is_(True),
                )
            )
        ).scalar_one_or_none()
        if exists is not None:
            return exists
        logger.warning(
            "Default presentation template %s is gone or private; using the built-in",
            configured,
        )
    for slug in DEFAULT_TEMPLATE_SLUGS:
        builtin = (
            await db.execute(
                select(TemplateContainer.id)
                .where(builtin_container_filter(slug))
                .order_by(TemplateContainer.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        if builtin is not None:
            return builtin
    return None


#: ``extra_metadata.deck_defaults`` keys a template may pre-fill on a deck the
#: agent did not set explicitly. Each is validated by ``PresentationSpec`` like
#: any agent-supplied value, so a template cannot smuggle an invalid deck.
DECK_DEFAULT_KEYS: tuple[str, ...] = ("theme", "transition", "aspect_ratio")

#: The SLIDE-level deck defaults a template may carry, each an object
#: keyed by the opening / closing layout: ``hero_defaults`` (``{"title":
#: "gradient", "closing": "gradient"}``) fills the ``hero`` of a title /
#: closing slide the agent left unset, ``hero_tones`` (``{"title": "accent"}``)
#: the colour that hero is drawn in, ``slide_themes`` (``{"title": "dark",
#: "closing": "dark"}``) fills its ``theme`` - so a brand opens and closes dark
#: on a light deck by default. Filled on absent keys only, like the deck keys,
#: so an agent's explicit ``hero: "plain"`` or a stored slide's own value wins.
DECK_SLIDE_DEFAULT_KEYS: tuple[str, ...] = ("hero_defaults", "hero_tones", "slide_themes")
#: The layouts the slide-level defaults apply to: the two that have a hero.
DECK_SLIDE_DEFAULT_LAYOUTS: tuple[str, ...] = ("title", "closing")
#: The catalog each slide-level default is read against - the ONE whitelist
#: behind :func:`template_deck_defaults`; the template generator quotes it in
#: the warning for a value it dropped. A template that declares covers of its
#: own adds them to ``hero_defaults``' (:func:`template_heroes`).
DECK_SLIDE_DEFAULT_VALUES: dict[str, tuple[str, ...]] = {
    "hero_defaults": SHARED_HEROES,
    "hero_tones": HERO_TONES,
    "slide_themes": PRESENTATION_THEMES,
}
_DECK_SLIDE_DEFAULT_FIELDS: dict[str, str] = {
    "hero_defaults": "hero",
    "hero_tones": "hero_tone",
    "slide_themes": "theme",
}


def template_deck_defaults(extra_metadata: object) -> dict[str, Any]:
    """The whitelisted ``deck_defaults`` of a container's ``extra_metadata``.

    The deck keys (:data:`DECK_DEFAULT_KEYS`) are strings; the slide keys
    (:data:`DECK_SLIDE_DEFAULT_KEYS`) are objects keyed by ``title`` /
    ``closing`` whose values are read against the catalog they fill (a hero
    the template offers - :func:`template_heroes` - a tone, a scheme) - an
    unknown layout or value is dropped, never forwarded to a slide the
    validator or the template's covers would then refuse.
    """
    if not isinstance(extra_metadata, dict):
        return {}
    defaults = extra_metadata.get("deck_defaults")
    if not isinstance(defaults, dict):
        return {}
    cleaned: dict[str, Any] = {
        key: value.strip()
        for key, value in defaults.items()
        if key in DECK_DEFAULT_KEYS and isinstance(value, str) and value.strip()
    }
    for key in DECK_SLIDE_DEFAULT_KEYS:
        raw = defaults.get(key)
        if not isinstance(raw, dict):
            continue
        allowed = template_heroes(extra_metadata) if key == "hero_defaults" else DECK_SLIDE_DEFAULT_VALUES[key]
        per_layout = {
            layout: value.strip().lower()
            for layout, value in raw.items()
            if layout in DECK_SLIDE_DEFAULT_LAYOUTS
            and isinstance(value, str)
            and value.strip().lower() in allowed
        }
        if per_layout:
            cleaned[key] = per_layout
    return cleaned


def apply_deck_defaults(spec_payload: dict, defaults: Mapping[str, Any]) -> dict:
    """Fill ``theme`` / ``transition`` / ``aspect_ratio`` the agent left unset.

    Only absent or null keys are filled — an explicit choice by the agent (or a
    value already stored on a deck being revised) always wins over the template.
    The one exception is a theme whose SCHEME the template cannot dress, which
    the write path snaps afterwards with :func:`fit_theme_to_schemes`:
    a default is a suggestion, a supported scheme is a constraint. The
    slide-level defaults (:data:`DECK_SLIDE_DEFAULT_KEYS`) fill the
    ``hero`` / ``hero_tone`` / ``theme`` of every title and closing slide that
    names none, on the same absent-only rule - on a NEW deck only: a rewrite
    of an existing deck has no slide identity to read a stored value from, so
    the ``generate_presentation`` revision path strips the slide-level keys
    before calling this and a re-emitted cover left unset stays plain and
    inheriting. Returns *spec_payload* itself, mutated, for call-site brevity -
    and the slide dicts inside ``spec_payload["slides"]`` are mutated in place
    too when a slide-level default lands, so never hand this a shared or cached
    spec: the callers pass the agent's own payload.
    """
    for key, value in defaults.items():
        if key in DECK_SLIDE_DEFAULT_KEYS:
            continue
        if spec_payload.get(key) in (None, ""):
            spec_payload[key] = value
    slides = spec_payload.get("slides")
    if isinstance(slides, list):
        for key in DECK_SLIDE_DEFAULT_KEYS:
            per_layout = defaults.get(key)
            if not isinstance(per_layout, Mapping):
                continue
            field = _DECK_SLIDE_DEFAULT_FIELDS[key]
            for slide in slides:
                if not isinstance(slide, dict):
                    continue
                value = per_layout.get(slide.get("layout"))
                if value and slide.get(field) in (None, ""):
                    slide[field] = value
    return spec_payload


#: The one ``template_id`` value the tools declare for "no template at all",
#: the opt-out from the platform default. Compared case-insensitively; any other
#: non-UUID value is refused with the accepted shapes, never guessed.
TEMPLATE_NONE = "none"


class TemplateChoiceError(ValueError):
    """A ``template_id`` argument that is neither a UUID nor an opt-out."""


async def resolve_deck_template_id(
    db: AsyncSession,
    *,
    requested: object,
    existing_template_id: uuid.UUID | None,
    is_edit: bool,
) -> tuple[uuid.UUID | None, bool]:
    """Decide which template a deck is styled with. Returns ``(id, explicit_none)``.

    * A UUID names the template (an agent found it through ``search_templates``).
    * :data:`TEMPLATE_NONE` opts out: no template, renderer
      theme only — ``explicit_none`` is ``True`` so an edit can UNLINK a template
      the deck already carries.
    * Omitted: an edit keeps the deck's current template (re-defaulting a deck
      mid-revision would restyle it under the user); a new deck takes the
      platform default (:func:`resolve_default_template_id`).

    Raises :class:`TemplateChoiceError` for anything else, so the tool can tell
    the agent exactly what it accepts.
    """
    if requested is None or (isinstance(requested, str) and not requested.strip()):
        if is_edit:
            return existing_template_id, False
        return await resolve_default_template_id(db), False
    text = str(requested).strip()
    if text.lower() == TEMPLATE_NONE:
        return None, True
    if not is_valid_uuid(text):
        raise TemplateChoiceError(
            "template_id must be the UUID of a template container (from "
            "search_templates), 'none' for no template, or omitted for the "
            "platform default."
        )
    return uuid.UUID(text), False


@dataclass(frozen=True, slots=True)
class TemplateOption:
    """One presentation template a user may dress a NEW deck with.

    What the chooser (the agent asking the user, the library's "create with"
    picker) needs to present the choice: the name, the colour schemes the
    template can dress a deck in, a one-line description, and whether it is the
    platform default (the one an automation would take without asking).
    """

    id: uuid.UUID
    name: str
    description: str | None
    supported_schemes: tuple[str, ...]
    is_default: bool
    is_company_managed: bool
    #: How many slides a deck of this template may hold.
    slide_cap: int = DEFAULT_SLIDE_CAP


async def list_template_options(db: AsyncSession, *, user_id: uuid.UUID) -> list[TemplateOption]:
    """The presentation templates *user_id* may pick for a new deck, default first.

    Visibility is the standard three-tier model (own + group-shared +
    company-managed) through :func:`~app.services.template_containers.search_containers`;
    only ``kind = presentation`` containers are offered - an asset library or a
    report skin dresses no deck. Ordered so a list read aloud makes sense: the
    platform default first, then the company-managed templates, then the user's
    own and shared ones, each group by name.
    """
    from app.api.v1.helpers import get_user_group_ids
    from app.services.template_containers import (
        MAX_SEARCH_RESULTS,
        PRESENTATION_KIND,
        search_containers,
    )

    group_ids = await get_user_group_ids(db, user_id)
    rows = await search_containers(
        db,
        user_id=user_id,
        user_group_ids=group_ids,
        kind=PRESENTATION_KIND,
        limit=MAX_SEARCH_RESULTS,
    )
    default_id = await resolve_default_template_id(db)
    options = [
        TemplateOption(
            id=row.id,
            name=str(row.name or "").strip() or "template",
            description=(str(row.description).strip() or None) if row.description else None,
            supported_schemes=tuple(
                template_supported_schemes(row.theme_config, row.extra_metadata)
            ),
            is_default=default_id is not None and row.id == default_id,
            is_company_managed=bool(row.is_company_managed),
            slide_cap=template_slide_cap(row.extra_metadata),
        )
        for row in rows
    ]
    options.sort(key=lambda option: (not option.is_default, not option.is_company_managed, option.name.lower()))
    return options


def format_template_choice(options: Sequence[TemplateOption], *, proposed: str | None = None) -> str:
    """The ``generate_presentation`` result when the template is still the user's call.

    Returned INSTEAD of a deck when a chat user has several presentation
    templates and the choice is not yet the user's: the call named no template,
    or it named one (*proposed*) that nothing the user wrote asked for. The
    template is the deck's design and decides which themes it can wear, so it is
    a choice the user makes, not one the tool - or the model - makes for them.
    Tool-result grounding, deliberately: no system prompt text asks the agent to
    remember this - the tool simply does not create the deck until the choice
    is settled (:func:`template_choice_settled`).
    """
    proposed_name = None
    if proposed:
        wanted = proposed.strip().lower()
        if wanted == TEMPLATE_NONE:
            proposed_name = "no template"
        else:
            proposed_name = next((o.name for o in options if str(o.id) == wanted), None)
    if proposed_name:
        opening = (
            f"No deck was created yet: the call picked '{proposed_name}', but "
            "nothing the user wrote asked for it, and this user can pick from "
            "several templates. The template is the deck's design (palette, fonts, "
            "logos, predefined assets) and decides which themes (dark / light) the "
            "deck can use, so it is the user's choice, not yours. Available:"
        )
    else:
        opening = (
            "No deck was created yet: this user can pick from several presentation "
            "templates and template_id names none of them. The template is the "
            "deck's design (palette, fonts, logos, predefined assets) and decides "
            "which themes (dark / light) the deck can use, so it is the user's "
            "choice. Available:"
        )
    lines = [opening]
    for option in options:
        schemes = " / ".join(option.supported_schemes) if option.supported_schemes else "dark / light"
        flags = []
        if option.is_default:
            flags.append("platform default")
        if not option.is_company_managed:
            flags.append("the user's own or shared with them")
        detail = f" - {option.description}" if option.description else ""
        # The cap is named when the template sets its own: the
        # length a deck may run to is part of what the user is choosing.
        if option.slide_cap != DEFAULT_SLIDE_CAP:
            detail += f" - up to {option.slide_cap} slides"
        suffix = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"- {option.name} (template_id {option.id}; themes: {schemes}){detail}{suffix}")
    lines.append("- No template (template_id 'none'): the renderer's plain look, dark or light.")
    recommend = f" You may recommend '{proposed_name}', but the user decides." if proposed_name else ""
    lines.append(
        "Ask the user which template to use - one short question listing these "
        f"names - and generate once they answer.{recommend} A request that already "
        "names one of these (or asks for no template) is accepted as the answer. "
        "Settle this BEFORE writing the slides next time: the choice never depends "
        "on the content. The user can change the template and the theme later from "
        "the deck's Design panel."
    )
    return "\n".join(lines)


def fold_text(text: str) -> str:
    """Case- and diacritics-insensitive form of *text*, whitespace collapsed.

    ``Filigran  Brand Deck``, ``FILIGRAN BRAND DECK`` and a decomposed (NFD)
    ``Filigrán brand deck`` all fold to ``filigran brand deck``, so a template
    name is matched the way a person types it.
    """
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.split())


#: How a request says "no template" in the languages the platform's users
#: write in (folded, see :func:`fold_text`). Only checked when the call opts out
#: (a :data:`TEMPLATE_NONE` ``template_id``): the phrase is what makes
#: the opt-out the USER'S, not the model's.
_NO_TEMPLATE_PHRASES: tuple[str, ...] = (
    "no template",
    "without template",
    "without a template",
    "without any template",
    "not use a template",
    "don't use a template",
    "do not use a template",
    "plain look",
    "plain deck",
    "free deck",
    "sans template",  # fr
    "sans modele",  # fr, folded
    "aucun template",  # fr
    "aucun modele",  # fr, folded
    "sin plantilla",  # es
    "ohne vorlage",  # de
)


def template_choice_settled(
    request_text: str | None,
    template_id: str | None,
    options: Sequence[TemplateOption],
) -> bool:
    """Whether the user's OWN words settle the template a call passes.

    The deterministic half of the template-choice contract. ``format_template_choice``
    used to fire only when ``template_id`` was blank, which left a loophole a model
    walks through every time: it looks the templates up with ``search_templates``,
    picks one it likes and passes its id - and the user, who was never asked, gets
    a branded deck they did not choose. So a NEW deck on an interactive turn is
    only created when the choice is demonstrably the user's:

    * the request text names the chosen template - its name (folded, see
      :func:`fold_text`) or its id appears in what the user wrote. A name has
      to appear as WORDS, not as a fragment of one: a template called ``Brand``
      is not named by "rebranding", nor ``AI`` by "said" - a fragment match
      would settle the choice on the user's behalf, which is the loophole this
      gate exists to close. When several option names appear (``Filigran Brand
      Deck`` is a substring of ``Filigran Brand Deck (copy)``), only the
      LONGEST match counts, so a call picking the base template out of a
      request that named the copy is refused;
    * or the request asks for no template in so many words
      (:data:`_NO_TEMPLATE_PHRASES`) and the call opts out.

    A conversation in which the question was already put to the user (see
    :func:`template_choice_asked`) is the other way the choice becomes theirs;
    the caller checks that marker separately, because it is a per-conversation
    state, not a property of the text.
    """
    if not request_text or not template_id or not template_id.strip():
        return False
    text = fold_text(request_text)
    wanted = template_id.strip().lower()
    if wanted == TEMPLATE_NONE:
        return any(phrase in text for phrase in _NO_TEMPLATE_PHRASES)
    if wanted in text:
        return True
    matched = [o for o in options if _names_template(text, o.name)]
    if not matched:
        return False
    longest = max(len(fold_text(o.name)) for o in matched)
    return any(str(o.id) == wanted for o in matched if len(fold_text(o.name)) == longest)


def _names_template(folded_text: str, name: str) -> bool:
    """Whether *folded_text* names template *name* as whole words (see :func:`fold_text`)."""
    folded = fold_text(name)
    if not folded:
        return False
    # Alphanumerics on either side would make the name a fragment of a longer
    # word; punctuation and spaces are fine ("the Brand template", "Brand:").
    return re.search(rf"(?<![^\W_]){re.escape(folded)}(?![^\W_])", folded_text) is not None


#: How long "the template question was put to the user in this conversation"
#: is remembered. A conversation that answered the question once does
#: not get asked again for every deck it creates; a week outlives any live chat.
TEMPLATE_CHOICE_ASKED_TTL_SECS = 7 * 24 * 3600


def template_choice_asked_key(conversation_id: str) -> str:
    """Redis key of the per-conversation "asked" marker (single source of truth)."""
    return f"presentation:template_asked:{conversation_id}"


async def mark_template_choice_asked(conversation_id: str | None) -> None:
    """Remember that ``format_template_choice`` was returned in this conversation.

    Fail-open: without Redis the marker is simply not written, and the next
    call is settled by the request text instead (the user's answer names the
    template) - one question more at worst, never a deck the user did not choose.
    """
    if not conversation_id:
        return
    from app.core.redis import redis_client

    key = template_choice_asked_key(conversation_id)
    try:
        redis = await redis_client()
        await redis.set(key, "1", ex=TEMPLATE_CHOICE_ASKED_TTL_SECS)
    except Exception as exc:
        logger.debug("template-choice marker skipped for %s: %s", key, exc)


async def template_choice_asked(conversation_id: str | None) -> bool:
    """Whether the template question was already put to the user in this conversation.

    Once it was, the model's next ``template_id`` is the user's answer relayed -
    in whatever words or language they used - and is accepted as such. Fail-open
    to ``False``: an unreachable Redis means the question may be asked once more.
    """
    if not conversation_id:
        return False
    from app.core.redis import redis_client

    key = template_choice_asked_key(conversation_id)
    try:
        redis = await redis_client()
        return bool(await redis.exists(key))
    except Exception as exc:
        logger.debug("template-choice marker read skipped for %s: %s", key, exc)
        return False


def spec_references_file(spec: object, file_id: uuid.UUID) -> bool:
    """Does one of *spec*'s slides STRUCTURALLY name *file_id*?

    Structural means a ``file_id`` / ``*_file_id`` field, never a mention: a
    UUID pasted into a slide's prose is text, not a reference, and grants
    nothing. The SQL pre-filter that finds candidate decks is a text
    match, so this is the check that decides.
    """
    wanted = str(file_id)
    return any(_canonical_file_reference(value) == wanted for _, _, value in slide_file_references(spec))


async def verify_file_references(
    db: AsyncSession,
    spec: dict,
    *,
    user,
    baseline: dict | None = None,
    carried: Collection[str] = (),
) -> list[tuple[str, str | None, str]]:
    """Every file id a validated deck names must be a platform file *user* may read.

    Tool-result grounding, not a prompt rule. The ``image`` block and the
    ``*_file_id`` slide fields take a bare UUID, and a model that has not called
    ``get_template`` will sooner or later INVENT one that looks right: the
    validator accepts it (it is a UUID), the renderer asks for it, gets nothing
    and draws nothing — a slide silently missing its diagram, with no error
    anywhere. Checking here turns that into a field-addressed
    :class:`PresentationSpecError` naming the slide, the field and the id, so the
    agent fixes that one slide instead of shipping a hole. The same pass refuses a
    file the user cannot read (:func:`authorize_file_access`), and
    that refusal is what keeps the deck-reference grant honest: everyone who may
    READ a deck may fetch the files its slides name, so the writer's own read
    access is the only thing standing between a foreign file id typed into a
    deck and that file served to the deck's readers — and to the writer.

    *user* is the acting :class:`~app.models.user.User`, or its id: given an id,
    the row is loaded here and only once the spec turns out to name a file — a
    file-free deck (the common case) costs no round-trip at all. An id that
    resolves to no user cannot read anything, so every reference is refused
    rather than the check being skipped.

    *baseline* is the spec the write REPLACES (the REST edit, a version
    restore): a reference already present in it was vouched for by whoever
    introduced it and is skipped, so only the references this write introduces
    are checked. Without it a file deleted since slide 3 was written would
    block every later edit of slide 12 — and the owner's own upload would block
    a colleague's edit of a shared deck. The agent's wholesale rewrite passes no
    baseline: every id it emits is its own claim.

    *carried* holds the canonical file ids the write carries over from a spec
    that was already stored and that the writer reads - the deck being
    rewritten or edited, the revision being restored. A carried reference whose
    file no longer exists at all (a built-in template the mode does not ship
    removed with its assets; a file deleted since) is not refused: the
    picture is already gone from every surface, and nobody can bring it back to
    satisfy the check. It is returned instead - ``(path, layout, file id)`` - so
    the caller says which pictures are gone. A carried id that still has a row
    is checked like any other, and one the write introduces is refused as before.

    Only the deck's own spec is walked (:func:`slide_file_references`).
    """
    from fastapi import HTTPException

    from app.models.file_attachment import FileAttachment
    from app.services.file_access import authorize_file_access
    from app.services.presentation_spec import PresentationSpecError, SpecProblem

    vouched: set[str] = set()
    if _unresolved_video_blocks(spec) or _unimported_video_links(spec) or _posterless_player_links(spec):
        if isinstance(user, str):
            user = _coerce_user_id(user)
        if isinstance(user, uuid.UUID):
            from app.models.user import User

            user = (await db.execute(select(User).where(User.id == user))).scalar_one_or_none()
        vouched = await resolve_video_blocks(db, spec, user)
        vouched |= await import_video_links(spec, user)
        vouched |= await import_player_posters(spec, user)

    references = slide_file_references(spec)
    if baseline is not None and references:
        known = {_canonical_file_reference(value) for _, _, value in slide_file_references(baseline)}
        references = [ref for ref in references if _canonical_file_reference(ref[2]) not in known]
    if not references:
        return []
    wanted = {uuid.UUID(value) for _, _, value in references if is_valid_uuid(value)}
    rows = {}
    if wanted:
        result = await db.execute(select(FileAttachment).where(FileAttachment.id.in_(wanted)))
        rows = {row.id: row for row in result.scalars().all()}
    if isinstance(user, str):
        user = _coerce_user_id(user)
    if isinstance(user, uuid.UUID):
        from app.models.user import User

        user = (await db.execute(select(User).where(User.id == user))).scalar_one_or_none()

    problems: list[SpecProblem] = []
    gone: list[tuple[str, str | None, str]] = []
    carried_ids = set(carried)
    for path, layout, value in references:
        row = rows.get(uuid.UUID(value)) if is_valid_uuid(value) else None
        if row is not None and _canonical_file_reference(value) in vouched:
            # A render of a Video Studio video the writer can open, taken from
            # its ``video_id`` just now: vouched by the video's own visibility.
            continue
        if row is None and _canonical_file_reference(value) in carried_ids:
            gone.append((path, layout, value))
            continue
        if row is not None and user is not None:
            try:
                await authorize_file_access(row, user, db)
                continue
            except HTTPException:
                pass
        # One wording for a missing file and one the writer may not read, so the
        # refusal never confirms which ids exist.
        problems.append(
            SpecProblem(path=path, layout=layout, blocks=(), message=f"`{value}` is not a platform file you can read")
        )
    if problems:
        # The agent line keeps the ``slides.1 (layout=image).file_id = `id` ...``
        # shape the tool loop has always seen; the structured problems are what
        # a human-facing route renders instead.
        def _agent_line(problem: SpecProblem) -> str:
            head, _, field = problem.path.partition(".")
            index, _, rest = field.partition(".")
            return f"{head}.{index} (layout={problem.layout}).{rest} = {problem.message}"

        raise PresentationSpecError(
            "; ".join(_agent_line(problem) for problem in problems)
            + ". File ids come from get_template (the predefined assets of the deck's "
            "template — call it with no id for the default brand template) or from a "
            "file generated in this conversation; never invent one.",
            problems=problems,
        )
    return gone


def _unresolved_video_blocks(spec: object) -> list[tuple[str, str | None, dict[str, Any]]]:
    """``(dotted path, slide layout, block)`` for each ``video`` block naming a
    ``video_id`` whose render (``file_id``) is not resolved yet."""
    found: list[tuple[str, str | None, dict[str, Any]]] = []

    def _walk(node: object, path: str, layout: str | None) -> None:
        if isinstance(node, dict):
            if node.get("type") == "video" and node.get("video_id") and not node.get("file_id"):
                found.append((path, layout, node))
                return
            for key, value in node.items():
                _walk(value, f"{path}.{key}", layout)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                _walk(item, f"{path}.{index}", layout)

    if isinstance(spec, dict):
        for index, slide in enumerate(spec.get("slides") or []):
            if isinstance(slide, dict):
                layout = slide.get("layout")
                _walk(slide, f"slides.{index}", layout if isinstance(layout, str) else None)
    return found


async def resolve_video_blocks(db: AsyncSession, spec: dict[str, Any], user: User | None) -> set[str]:
    """Fill the files of every ``video`` block that names a Video Studio video.

    A block written ``{"type": "video", "video_id": ...}`` takes the video's
    current ready render on save: its MP4 (``file_id``), its poster
    (``poster_file_id``, unless the block names its own) and its WebVTT
    captions (``captions_file_id``). The video must be one *user* can open
    (``load_visible_project``, the visibility the Video Studio applies) and
    must have a ready render; otherwise the save is refused with a
    field-addressed problem. The block is changed in place.

    Returns the canonical ids of the files filled in: vouched for by the
    video's visibility, so :func:`verify_file_references` does not ask again
    whether the writer owns the render (a colleague who renders a shared
    video owns its MP4, not the writer).
    """
    from app.services.presentation_spec import PresentationSpecError, SpecProblem
    from app.services.video_studio.projects import load_visible_project
    from app.services.video_studio.public_links import current_render

    vouched: set[str] = set()
    problems: list[SpecProblem] = []
    for path, layout, block in _unresolved_video_blocks(spec):
        video_id = str(block.get("video_id") or "")
        project = await load_visible_project(db, video_id, user) if user is not None else None
        if project is None:
            problems.append(
                SpecProblem(
                    path=f"{path}.video_id",
                    layout=layout,
                    blocks=(),
                    message=f"`{video_id}` is not a Video Studio video you can open",
                )
            )
            continue
        render = await current_render(db, project)
        if render is None or render.file_id is None:
            problems.append(
                SpecProblem(
                    path=f"{path}.video_id",
                    layout=layout,
                    blocks=(),
                    message=f"the video `{video_id}` has no ready render yet: render it first, then place it",
                )
            )
            continue
        block["video_id"] = str(project.id)
        block["file_id"] = str(render.file_id)
        vouched.add(str(render.file_id))
        if render.poster_file_id is not None and not block.get("poster_file_id"):
            block["poster_file_id"] = str(render.poster_file_id)
            vouched.add(str(render.poster_file_id))
        if render.captions_file_id is not None and not block.get("captions_file_id"):
            block["captions_file_id"] = str(render.captions_file_id)
            vouched.add(str(render.captions_file_id))
    if problems:
        raise PresentationSpecError(
            "; ".join(f"{problem.path} (layout={problem.layout}) = {problem.message}" for problem in problems)
            + ". A video block plays a Video Studio video's ready render (video_id) or an MP4 / WebM "
            "platform file (file_id).",
            problems=problems,
        )
    return vouched


#: The largest MP4 / WebM a deck imports from a link: the size an editable
#: PPTX still embeds as a movie (``pptx_writer.MAX_EMBEDDED_MOVIE_BYTES``).
MAX_IMPORTED_VIDEO_BYTES = 100 * 1024 * 1024
#: What a video link may serve, with the extension its imported file takes.
IMPORTED_VIDEO_TYPES: dict[str, str] = {"video/mp4": ".mp4", "video/webm": ".webm"}


def _unimported_video_links(spec: object) -> list[tuple[str, str | None, dict[str, Any]]]:
    """``(dotted path, slide layout, block)`` for each ``video`` block whose
    ``url`` is a direct file link (not a YouTube / Vimeo player) not imported yet."""
    from app.services.presentation_spec import video_embed

    found: list[tuple[str, str | None, dict[str, Any]]] = []

    def _walk(node: object, path: str, layout: str | None) -> None:
        if isinstance(node, dict):
            url = node.get("url")
            if node.get("type") == "video" and isinstance(url, str) and url and not node.get("file_id"):
                try:
                    embedded = video_embed(url)
                except ValueError:
                    return  # refused by the spec validation, never fetched
                if embedded is None:
                    found.append((path, layout, node))
                return
            for key, value in node.items():
                _walk(value, f"{path}.{key}", layout)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                _walk(item, f"{path}.{index}", layout)

    if isinstance(spec, dict):
        for index, slide in enumerate(spec.get("slides") or []):
            if isinstance(slide, dict):
                layout = slide.get("layout")
                _walk(slide, f"slides.{index}", layout if isinstance(layout, str) else None)
    return found


def _video_filename(url: str, mime: str) -> str:
    """The imported file's name: the link's last path segment, with the type's extension."""
    from urllib.parse import unquote, urlsplit

    stem = unquote(urlsplit(url).path.rsplit("/", 1)[-1]).rsplit(".", 1)[0]
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "-", stem).strip(" .-")[:80] or "video"
    return f"{stem}{IMPORTED_VIDEO_TYPES.get(mime, '.mp4')}"


async def import_video_links(spec: dict[str, Any], user: User | None) -> set[str]:
    """Import the MP4 / WebM link of every ``video`` block as a platform file.

    A block written ``{"type": "video", "url": "https://.../tour.mp4"}`` is
    downloaded once on save, over the outbound policy of ``fetch_image_asset``
    (public addresses only, every redirect hop checked, paced per host), within
    :data:`MAX_IMPORTED_VIDEO_BYTES` and only when the server types it
    ``video/mp4`` or ``video/webm``. The file is stored as the writer's
    (``persist_tool_artifact``) and its id written to the block's ``file_id``:
    from then on the slide plays, shares, exports and survives like an uploaded
    file; ``url`` stays as the source it came from. YouTube and Vimeo links are
    players, never downloaded. Anything refused is a field-addressed problem.

    Returns the canonical ids of the files imported (the writer's own files).
    """
    from app.services.builtin_tools._shared import _persist_generated_file
    from app.services.builtin_tools.image_assets import ImageAssetError, download_public_image
    from app.services.presentation_spec import PresentationSpecError, SpecProblem

    pending = _unimported_video_links(spec)
    if not pending:
        return set()
    imported: set[str] = set()
    problems: list[SpecProblem] = []
    for path, layout, block in pending:
        url = str(block.get("url") or "")
        if user is None:
            problems.append(
                SpecProblem(path=f"{path}.url", layout=layout, blocks=(), message="no user to import the video as")
            )
            continue
        try:
            data, mime, final_url = await download_public_image(
                url, allowed_types=IMPORTED_VIDEO_TYPES, max_bytes=MAX_IMPORTED_VIDEO_BYTES, noun="video"
            )
        except ImageAssetError as exc:
            problems.append(SpecProblem(path=f"{path}.url", layout=layout, blocks=(), message=str(exc)))
            continue
        stored = await _persist_generated_file(
            user_id=str(user.id),
            filename=_video_filename(final_url, mime),
            raw_bytes=data,
            content_type=mime,
            conversation_id=None,
            assignment_run_id=None,
            source="presentation_video_import",
        )
        file_id = str(getattr(stored, "id", "") or "")
        block["file_id"] = file_id
        imported.add(file_id)
    if problems:
        raise PresentationSpecError(
            "; ".join(f"{problem.path} (layout={problem.layout}) = {problem.message}" for problem in problems)
            + ". A video link is a YouTube or Vimeo video, or a public MP4 / WebM file of at most "
            f"{MAX_IMPORTED_VIDEO_BYTES // (1024 * 1024)} MB.",
            problems=problems,
        )
    return imported


def _posterless_player_links(spec: object) -> list[tuple[str, str, dict[str, Any]]]:
    """``(provider, video id, block)`` for each YouTube / Vimeo ``video`` block with no poster yet."""
    from app.services.presentation_spec import video_embed

    found: list[tuple[str, str, dict[str, Any]]] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            url = node.get("url")
            if node.get("type") == "video" and isinstance(url, str) and url and not node.get("poster_file_id"):
                try:
                    embedded = video_embed(url)
                except ValueError:
                    embedded = None
                if embedded is not None:
                    found.append((embedded[0], embedded[1], node))
                return
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(spec)
    return found


async def _player_thumbnail_url(provider: str, video: str) -> str | None:
    """Where the provider serves the still of *video*: YouTube's own, Vimeo's through oEmbed."""
    if provider == "youtube":
        return f"https://i.ytimg.com/vi/{video}/hqdefault.jpg"
    from app.services.builtin_tools.image_assets import download_public_image

    data, _mime, _url = await download_public_image(
        f"https://vimeo.com/api/oembed.json?url=https%3A%2F%2Fvimeo.com%2F{video}&width=1280",
        allowed_types={"application/json": ".json"},
        max_bytes=64 * 1024,
        noun="application",
    )
    thumbnail = json.loads(data).get("thumbnail_url")
    return thumbnail if isinstance(thumbnail, str) and thumbnail.startswith("https://") else None


async def import_player_posters(spec: dict[str, Any], user: User | None) -> set[str]:
    """Give every YouTube / Vimeo ``video`` block a poster: the provider's still, imported.

    The player itself cannot be packed into a PDF or a PPTX, so the export
    draws the poster with a link to the video; the poster is the provider's
    thumbnail, downloaded once on save under the outbound policy and stored
    as the writer's file (``poster_file_id``), so the export never reaches out.
    Best effort: a still that cannot be fetched leaves the block without a
    poster (the static render keeps its placeholder), never refuses the save.

    Returns the canonical ids of the posters imported (the writer's own files).
    """
    from app.services.builtin_tools._shared import _persist_generated_file
    from app.services.builtin_tools.image_assets import ALLOWED_IMAGE_TYPES, download_public_image

    imported: set[str] = set()
    if user is None:
        return imported
    for provider, video, block in _posterless_player_links(spec):
        try:
            thumbnail = await _player_thumbnail_url(provider, video)
            if thumbnail is None:
                continue
            data, mime, _url = await download_public_image(thumbnail)
        except Exception:  # a missing still only costs the poster
            logger.info("Deck video poster of %s %s not imported", provider, video, exc_info=True)
            continue
        stored = await _persist_generated_file(
            user_id=str(user.id),
            filename=f"{provider}-{video}{ALLOWED_IMAGE_TYPES.get(mime, '.jpg')}",
            raw_bytes=data,
            content_type=mime,
            conversation_id=None,
            assignment_run_id=None,
            source="presentation_video_import",
        )
        file_id = str(getattr(stored, "id", "") or "")
        block["poster_file_id"] = file_id
        imported.add(file_id)
    return imported


def gone_pictures_note(summary: str | None, gone: Sequence[tuple[str, str | None, str]]) -> str | None:
    """*summary* followed by how many pictures of the write no longer exist.

    The history line is where a person reads what a revision did, so a write
    saved around pictures whose files are gone says so there.
    """
    if not gone:
        return summary
    count = len({_canonical_file_reference(value) for _, _, value in gone})
    note = f"{count} picture{'s' if count != 1 else ''} no longer on the platform left empty"
    return f"{summary} ({note})" if summary else note[:1].upper() + note[1:]


async def save_presentation(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    spec_payload: object,
    conversation_id: uuid.UUID | None = None,
    assignment_run_id: uuid.UUID | None = None,
    template_container_id: uuid.UUID | None = None,
    presentation: Presentation | None = None,
    unlink_template: bool = False,
    verify_files_for=None,
    verify_baseline: dict | None = None,
    verify_carried: Collection[str] = (),
    gone_pictures: list[tuple[str, str | None, str]] | None = None,
    author_id: uuid.UUID | str | None = None,
    change_source: str = VERSION_SOURCE_AGENT,
    change_summary: str | None = None,
    restored_from_revision: int | None = None,
    slide_cap: int = DEFAULT_SLIDE_CAP,
    edit_session: bool = False,
    continued_sessions: list[PresentationVersion] | None = None,
) -> Presentation:
    """Validate *spec_payload* and create — or update in place — a deck.

    Passing an existing *presentation* rewrites its spec and bumps ``revision``
    instead of inserting a second row: that is what keeps the
    ``[[PRESENTATION:<id>]]`` marker already sitting in the conversation valid
    when the agent adds, removes or reorders slides, so the frontend reconciles
    the slide list rather than mounting a brand-new deck.

    ``unlink_template`` drops the template an edited deck carries (the agent
    passed ``template_id='none'``); without it an omitted template keeps
    the current link.

    ``verify_files_for`` is the acting :class:`~app.models.user.User` — or its
    id, resolved lazily: when given, every file id the spec names is checked to
    exist and to be readable by that user (:func:`verify_file_references`) AFTER
    the shape validation, so a malformed spec is reported before a dangling
    reference. The agent's wholesale rewrite and the package import check the
    whole spec; the REST edit and a version restore also pass
    ``verify_baseline`` — the spec being replaced — so only the references the
    write INTRODUCES are checked (a deck's readers may fetch what its
    slides name, so a write that can add a reference must prove the writer could
    read it). The library duplicate passes nothing: it copies a deck the caller
    can already read, reference for reference. ``verify_carried`` names the file
    ids carried over from a stored spec (the deck being rewritten, the revision
    being restored): one whose file no longer exists is saved as it is instead
    of refusing the write, appended to *gone_pictures* when given, and
    counted in the history line (:func:`gone_pictures_note`).

    Every save is also a **version**: the written spec is snapshotted
    under the new revision by :func:`~app.services.presentation_versions.record_presentation_version`,
    tagged with ``change_source`` (``agent`` / ``manual`` / ``restore`` /
    ``duplicate``), the optional one-line ``change_summary`` and the acting
    ``author_id`` — which also lands on ``updated_by_user_id`` for the
    "edited by" line. *author_id* defaults to *user_id*, the deck's owner: a
    colleague saving through a ``config`` grant passes their own id so the
    history names them, not the owner. A save from the editor's autosave is
    part of an *edit_session*: it continues the author's open session
    (:func:`~app.services.presentation_versions.open_edit_session`), appended
    to *continued_sessions* when given, or opens one - a run of saves by one
    person is one history entry. The session is looked up once the write is
    vouched for, so a refused write reads no history.

    *slide_cap* is how many slides the deck may hold: the linked
    template's ``TemplateFacts.slide_cap`` on an authoring path (the agent's
    rewrite, the editor's save), which the caller resolved when it checked the
    link; the copies (duplicate, restore, import) pass ``MAX_SLIDE_CAP``, the
    ceiling, since they carry content already accepted. The default is the
    platform default, so a path that passes nothing is held to it, never freed
    of it. On a re-save the deck's current size is admitted too
    (:func:`effective_slide_cap`): an over-cap deck stays saveable, never grows.

    Raises ``PresentationSpecError`` (from :func:`validate_spec`, or from the
    reference check) if the payload is malformed — the caller surfaces it
    verbatim as a tool error.
    """
    from app.services.presentation_versions import open_edit_session, record_presentation_version

    cap = effective_slide_cap(slide_cap, presentation.spec) if presentation is not None else slide_cap
    spec = validate_spec(spec_payload, slide_cap=cap)
    if verify_files_for is not None:
        gone = await verify_file_references(
            db, spec, user=verify_files_for, baseline=verify_baseline, carried=verify_carried
        )
        if gone:
            if gone_pictures is not None:
                gone_pictures.extend(gone)
            change_summary = gone_pictures_note(change_summary, gone)
    title = str(spec["title"])
    theme = str(spec.get("theme") or "dark")
    slide_count = len(spec.get("slides") or [])
    editor = _coerce_user_id(author_id) if author_id is not None else user_id

    if presentation is None:
        presentation = Presentation(
            # Assigned here rather than left to the column default so the id is
            # readable before the INSERT lands — the tool has to put it in the
            # ``[[PRESENTATION:<id>]]`` marker it returns. Same reason
            # ``_persist_generated_file`` stamps ``FileAttachment.id``.
            id=uuid.uuid4(),
            user_id=user_id,
            conversation_id=conversation_id,
            assignment_run_id=assignment_run_id,
            template_container_id=template_container_id,
            title=title,
            theme=theme,
            slide_count=slide_count,
            revision=1,
            spec=spec,
            updated_by_user_id=editor,
        )
        db.add(presentation)
    else:
        presentation.title = title
        presentation.theme = theme
        presentation.slide_count = slide_count
        presentation.spec = spec
        presentation.revision = (presentation.revision or 1) + 1
        presentation.updated_by_user_id = editor
        # An edit may re-point the deck at a different template, but omitting
        # the argument must not silently unlink the one already set — only an
        # explicit opt-out does.
        if unlink_template:
            presentation.template_container_id = None
        elif template_container_id is not None:
            presentation.template_container_id = template_container_id
        if conversation_id is not None and presentation.conversation_id is None:
            presentation.conversation_id = conversation_id

    await db.flush()
    logos = logo_file_ids(spec)
    if logos:
        # A logo stored before its geometry was read is read now, so
        # the next read of this deck draws its mark, not its padding.
        await backfill_image_geometry(db, logos)
    session_entry = None
    if edit_session:
        session_entry = await open_edit_session(db, presentation, author_id=editor)
        if session_entry is not None and continued_sessions is not None:
            continued_sessions.append(session_entry)
    await record_presentation_version(
        db,
        presentation,
        source=change_source,
        author_id=editor,
        summary=change_summary,
        restored_from_revision=restored_from_revision,
        edit_session=session_entry,
        opens_edit_session=edit_session,
    )
    # ROI of chat work (#10544): a deck built in a conversation is one record, refreshed by every round.
    from app.services.roi_chat_records import record_deliverable

    await record_deliverable(
        db,
        kind="presentation",
        deliverable_id=presentation.id,
        conversation_id=presentation.conversation_id,
        user_id=presentation.user_id,
        title=title,
        size=slide_count,
    )
    return presentation


def _coerce_user_id(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


async def load_visible_presentation(
    db: AsyncSession,
    presentation_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None,
    user_group_ids: list[str] | None = None,
    for_update: bool = False,
) -> tuple[Presentation, list[str]] | None:
    """Load a deck the acting user may READ, or ``None``.

    Visibility is the platform's three-tier predicate (``user_visible_filter``:
    own + user-shared + group-shared + company-managed), evaluated in SQL so a
    deck the user cannot see is indistinguishable from one that does not exist.
    Returns the row together with the group ids that were resolved, so a caller
    that goes on to compute a permission tier does not query them twice.

    ``for_update`` takes a ``SELECT ... FOR UPDATE`` row lock for the rest of
    the transaction: a write path that reads the stored spec, patches it
    and saves it back must not race another editor of the same deck - two
    agents loading revision N would otherwise both write N+1 and the second
    would silently drop the first one's change. Under READ COMMITTED the
    blocked reader gets the row as the first writer committed it, so it
    patches the fresh revision rather than a stale baseline.

    No admin bypass here on purpose: this is the loader behind the AGENT tools,
    which act for a user from inside a conversation. An admin's agent surfacing
    other users' private decks would be the same leak ``search_templates``
    guards against; the REST surface, where an admin acts as themselves, adds
    ``is_admin`` on its own.
    """
    if user_id is None:
        return None
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter

    if user_group_ids is None:
        user_group_ids = await get_user_group_ids(db, user_id)
    query = select(Presentation).where(
        Presentation.id == presentation_id,
        user_visible_filter(Presentation, user_id, user_group_ids),
    )
    if for_update:
        query = query.with_for_update(of=Presentation)
    result = await db.execute(query)
    presentation = result.scalar_one_or_none()
    if presentation is None:
        return None
    return presentation, user_group_ids


async def load_writable_presentation(
    db: AsyncSession,
    presentation_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None,
    for_update: bool = False,
) -> Presentation | None:
    """Load a deck the acting user may EDIT through an agent, or ``None``.

    ``for_update`` locks the row for the transaction (see
    :func:`load_visible_presentation`) - pass it from every read-modify-write
    path, so concurrent editors of one deck serialise instead of overwriting
    each other from a stale revision.

    Writable means: the user owns it, or holds a ``config``-tier grant on it
    (directly or through a group) — the sharing contract that lets a colleague
    keep working on someone else's deck with their own assistant. A
    ``read_only`` grantee gets ``None`` here exactly like a stranger does: the
    tool then says the deck cannot be edited rather than leaking that it exists
    with a different message.

    A company-managed (published) deck is writable only by an admin — checked
    against the user row, since the tool only knows a user id. There is
    deliberately NO blanket admin bypass for other users' personal decks: an
    agent runs on a user's behalf inside a conversation, and an admin's agent
    must never be able to rewrite a colleague's artifact from a chat (the REST
    surface, where the admin acts in person, is where ``can_manage`` applies).
    """
    loaded = await load_visible_presentation(
        db, presentation_id, user_id=user_id, for_update=for_update
    )
    if loaded is None:
        return None
    presentation, group_ids = loaded
    tier = await agent_deck_permission(
        db, presentation, user_id=user_id, user_group_ids=group_ids
    )
    if tier not in ("owner", "config"):
        return None
    return presentation


async def agent_deck_permission(
    db: AsyncSession,
    presentation: Presentation,
    *,
    user_id: uuid.UUID | None,
    user_group_ids: list[str],
) -> DeckPermission | None:
    """The tier an AGENT acting for *user_id* holds on a deck it can already see.

    This is :func:`deck_permission` with the agent-side reading of ``is_admin``:
    an administrator counts as one only on a company-managed deck (the only
    place the platform lets admins change what they do not own), never on a
    colleague's personal deck — see :func:`load_writable_presentation` for why
    the agent path has no blanket admin bypass. Shared by that loader and by
    the Presentation Studio (``services/presentation_studio.py``), so a chat
    bound to a deck reports exactly the access the deck tool will enforce.
    """
    is_admin = False
    if presentation.is_company_managed and presentation.user_id != user_id:
        from app.models.user import User

        role = (
            await db.execute(select(User.role).where(User.id == user_id))
        ).scalar_one_or_none()
        is_admin = role == "admin"
    return deck_permission(
        presentation, user_id=user_id, user_group_ids=user_group_ids, is_admin=is_admin
    )


async def remove_presentation(
    db: AsyncSession,
    presentation: Presentation,
    *,
    actor: User | None,
    request: Request | None = None,
    via_agent: bool = False,
) -> None:
    """Delete a deck the caller was allowed to delete, with its audit row.

    The one delete behind the library's ``Delete`` (``DELETE
    /presentations/{id}``) and the agent's ``delete_presentation`` tool; who
    may call it is the caller's to check first. There is no trash: the row
    goes, and the database takes everything that hangs off it - the version
    history, the review comments and the public link (``ON DELETE CASCADE``)
    - while the grants were columns of the row. A studio conversation bound
    to the deck stays the user's (``SET NULL``), and a
    ``[[PRESENTATION:<id>]]`` marker in a chat then reads "no longer
    available". Nothing a render cached is reachable afterwards: the layout
    report is keyed by the deck and served only to a caller that loaded it,
    the public PDF only through the link row. Flushes; the caller commits.
    """
    from app.services.audit import log_audit

    if actor is not None:
        details: dict[str, Any] = {
            "slide_count": presentation.slide_count,
            "revision": presentation.revision,
            "is_company_managed": bool(presentation.is_company_managed),
        }
        if via_agent:
            details["via"] = "agent"
        await log_audit(
            db,
            user=actor,
            action="delete",
            entity_type="presentation",
            entity_id=presentation.id,
            entity_name=presentation.title,
            summary=f"Deleted presentation '{presentation.title}'" + (" through an agent" if via_agent else ""),
            details=details,
            request=request,
        )
    # ROI of chat work (#10544, S2b): a deck deleted before any use counts nothing.
    from app.services.roi_chat_records import record_deleted

    await record_deleted(db, kind="presentation", deliverable_id=presentation.id)
    await db.delete(presentation)
    await db.flush()


async def duplicate_presentation(
    db: AsyncSession,
    source: Presentation,
    *,
    user_id: uuid.UUID,
    title: str | None = None,
    conversation_id: uuid.UUID | None = None,
    assignment_run_id: uuid.UUID | None = None,
) -> Presentation:
    """Copy a deck the caller can read into a new deck they own.

    The copy starts at revision 1 with no grants and no comments of its own —
    sharing and review are properties of the original, not of its content —
    and keeps the template link (the copy should look like the deck it was
    taken from). The library copy is bound to no conversation; the agent's
    ``duplicate_presentation`` tool passes the conversation or run it was
    called from, like every deck an agent makes.

    Goes through :func:`save_presentation`, so a stored spec that no longer
    validates (a deck written by an older release, then the catalog tightened)
    surfaces as ``PresentationSpecError`` rather than being copied blindly. A
    copy is held to the slide ceiling only: it is as long as the deck
    it was taken from, and that deck was accepted.
    """
    spec = dict(source.spec or {})
    if title:
        spec["title"] = title
    return await save_presentation(
        db,
        user_id=user_id,
        spec_payload=spec,
        conversation_id=conversation_id,
        assignment_run_id=assignment_run_id,
        template_container_id=source.template_container_id,
        change_source=VERSION_SOURCE_DUPLICATE,
        change_summary=f"Duplicated from “{source.title}”",
        slide_cap=MAX_SLIDE_CAP,
    )
