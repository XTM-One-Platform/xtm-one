# SPDX-License-Identifier: Apache-2.0

"""Pure-stdlib colour maths shared by every place the platform measures a colour.

Lives in ``core`` because it has no dependency at all: the document-vision
tools (``compare_palette``), the image summary behind the screenshot / Notion
upload results and any future colour check import it without pulling
in Pillow, pydantic or the presentation stack. Everything here is a pure
function of small tuples.

A colour is an sRGB triple of 0-255 integers throughout. Perceptual distance
is CIE76 ``delta_e`` — twenty lines rather than the hundred CIEDE2000 needs,
and every question asked of it ("is this the same colour", "is a forbidden
colour present") sits far from the boundary where the two metrics disagree.
"""

from __future__ import annotations

import re

RGB = tuple[int, int, int]

_HEX_COLOR_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")

#: CIE76 ΔE below which two colours are treated as the same by
#: ``compare_palette``. ~1 is the just-noticeable difference for adjacent
#: patches; 2 keeps rounding between colour spaces from reading as a finding.
DELTA_E_SAME = 2.0
#: ΔE above which the difference is plainly visible side by side.
DELTA_E_VISIBLE = 5.0


def parse_hex_color(value: object) -> RGB | None:
    """``"#0b1020"`` / ``"0B1020"`` -> ``(11, 16, 32)``; anything else ``None``.

    Deliberately strict: a theme value may also be ``hsl(...)`` or a font
    stack, and guessing at those would make a colour comparison answer about
    colours it never actually read. Three-digit shorthand is refused for the
    same reason — a caller that wants it expands it first, knowingly.
    """
    if not isinstance(value, str):
        return None
    match = _HEX_COLOR_RE.match(value.strip())
    if match is None:
        return None
    digits = match.group(1)
    return (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16))


def format_hex(rgb: RGB) -> str:
    """``(15, 188, 255)`` -> ``"#0FBCFF"`` — uppercase, always six digits.

    Uppercase on purpose: it is what a brand guideline prints and what a
    model copies out of one, so the value in a tool result compares by eye.
    """
    red, green, blue = (max(0, min(255, round(channel))) for channel in rgb)
    return f"#{red:02X}{green:02X}{blue:02X}"


def srgb_to_lab(rgb: RGB) -> tuple[float, float, float]:
    """sRGB (0-255) -> CIE L*a*b* under D65, the space ΔE is defined in."""

    def _linear(channel: int) -> float:
        value = channel / 255.0
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (_linear(channel) for channel in rgb)
    # sRGB D65 matrix, scaled to the D65 white point below.
    x = (0.4124 * red + 0.3576 * green + 0.1805 * blue) / 0.95047
    y = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    z = (0.0193 * red + 0.1192 * green + 0.9505 * blue) / 1.08883

    def _f(value: float) -> float:
        return value ** (1 / 3) if value > 0.008856 else (7.787 * value) + (16 / 116)

    fx, fy, fz = _f(x), _f(y), _f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e(first: RGB, second: RGB) -> float:
    """CIE76 ΔE between two sRGB colours (see the module note on the metric)."""
    first_lab = srgb_to_lab(first)
    second_lab = srgb_to_lab(second)
    return sum((a - b) ** 2 for a, b in zip(first_lab, second_lab, strict=True)) ** 0.5


def chroma(rgb: RGB) -> int:
    """``max(channel) - min(channel)`` in 0-255 units — how far from grey a colour is.

    Zero for every grey, black and white; the HSV saturation of a dark colour
    reads high even when the colour is visually indistinguishable from black
    (``#1A1F36`` has S = 0.52), which is why the image summary keys its
    "saturated" test on chroma, not on saturation.
    """
    return max(rgb) - min(rgb)
