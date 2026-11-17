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

"""Screen replacement: an AI shot of a real product screen whose text stays the screenshot's own.

A video model asked to animate a picture of an interface redraws its words,
so every such shot fails the text check. A shot with a ``screen`` is made in
two halves instead:

1. the model is given the room only - the start picture with the screen's
   quad (``screen.quad``: its four corners in the picture's pixels, top left,
   top right, bottom right, bottom left) filled with one flat colour
   (:func:`pick_key`: of the candidates, the one farthest from the colours
   the picture holds) - and asked to keep that surface flat
   (:func:`provider_prompt`);
2. the shot it makes is read frame by frame (:class:`Tracker`): the flat
   colour found near where it was, the four edges of that surface fitted to
   lines and their corners intersected, the corners smoothed over time
   (:func:`smooth`); then the real screenshot is drawn into those corners by
   a homography (``pictures.perspective_coefficients``), under whatever
   stands in front of the screen (the key's matte), with the light and the
   reflections the model drew on the flat surface laid over it
   (:func:`composite`, ``screen.light``).

The words on the screen are then the screenshot's pixels; only the room, the
light and the reflections are generated. A shot whose model did not keep
the surface flat - the colour missing from too many frames, or from its first
- fails as "screen lost" (:class:`ScreenLostError`), its video kept on the
row like a drifted shot.

A screen given without its quad is found in the start picture
(:func:`find_screen`): a picture drawn with its screen one solid
green, blue or magenta surface has that surface's corners read in code,
never guessed from the picture.

**Carried to the bezel, checked pixel by pixel.** The quad the
tracker fits is the inner flat area; the darker key along the bezel - the
model's shading, the encoder's blend - stayed as a line around the
screenshot. Each frame's corners are pushed outward over the pixels still the
key by chroma distance (:func:`bezel_corners`, at most :data:`BEZEL_REACH` of
the quad's width), the key's share inside that quad is dilated to every pixel
the chroma calls the key and feathered (:func:`composite`), and the matte is
pooled over :data:`MATTE_WINDOW` frames on the surface's own plane
(:class:`MatteSmoother`): a key read against compressed footage drifts, and a
matte recomputed on every frame blinks at its edge. What is drawn is then
measured on every frame (:class:`Measure`): the key colour left in a band
:data:`SPILL_BAND` wide around the screen (``key spill``, past
:data:`MAX_SPILL_SHARE` of the band), how much of the matte holds from frame to
frame (``key flicker``, under :data:`MIN_EDGE_HOLD`), and whether the screenshot's
content stands past the bezel (``screen content cropped``); each fails the
shot like a lost screen. A screenshot is fitted by its content box and
letterboxed in its own background colour (``contain``, the default:
``pictures.letterboxed``), never cut: ``cover`` is refused before anything is
paid when it would cut the content (:func:`check_content_fit`).
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

#: The flat colours a screen may be filled with: a name the prompt says, its RGB.
KEY_CANDIDATES: tuple[tuple[str, tuple[int, int, int]], ...] = (
    ("green", (0, 177, 64)),
    ("blue", (0, 71, 187)),
    ("magenta", (230, 0, 210)),
)
FITS = ("cover", "contain", "stretch")
#: How a screenshot fills the screen when the call says nothing: whole, by its content box, letterboxed in
#: its own background colour - ``cover`` cut product UIs (a sidebar, a right column).
DEFAULT_FIT = "contain"
#: How much of the light and reflections drawn on the flat surface plays over the screenshot.
DEFAULT_LIGHT = 0.6
#: The smallest screen, as a share of the picture's area.
MIN_SCREEN_SHARE = 0.005
#: How far outside the picture a corner may lie (a screen cut by the frame), as a share of its side.
CORNER_MARGIN = 0.25
#: A pixel closer than this to the key (relative chromaticity distance) is the surface ...
KEY_IN = 0.30
#: ... one farther than this is something in front of it; between, a soft edge.
KEY_OUT = 0.55
#: What the tracker counts as the surface.
KEY_TRACK = 0.42
#: The matte: a pixel this far along the key's chromaticity is none of it / all of it ...
_MATTE_LOW, _MATTE_HIGH = 0.2, 0.75
#: ... and one straying this far from that direction is the key / is another colour.
_ACROSS_IN, _ACROSS_OUT = 0.25, 0.5
#: Pixels darker than this are never the surface (their chromaticity is noise).
_MIN_KEY_LUMA = 12.0
_LUMA_PAD = 8.0
#: The share of frames the surface must be found in, and the longest run (seconds) it may be lost for.
MIN_FOUND_SHARE = 0.8
MAX_LOST_SECONDS = 0.5
#: Frames averaged around each one when the corners are smoothed.
SMOOTH_WINDOW = 5
#: How far past the fitted corners the screenshot is drawn (pixels, along the diagonals): over the
#: rim the camera's blur and the encoder bleed the key into; the matte keeps the bezel.
_EDGE_GROW = 4.0
#: :func:`find_screen` reads a picture at most this long, ...
FIND_SIDE = 1280
#: ... takes a pixel of a key's hue band (a named key's band in degrees, :data:`FIND_HUE_BANDS`; any other key's
#: own hue within this many degrees) ...
FIND_HUE_DEGREES = 40.0
FIND_HUE_BANDS: dict[str, tuple[float, float]] = {"green": (90.0, 150.0)}
#: ... and this saturated and bright (HSV, 0-1) for the surface: a screen a video model paints green under its own
#: light is rarely the saturated key it was asked for ...
FIND_MIN_SATURATION, FIND_MIN_VALUE = 0.35, 0.3
#: ... over a connected surface of at least this share of the picture ...
FIND_MIN_SHARE = 0.02
#: ... and a surface for a screen when it and the quad fitted to it overlap this much (intersection over union).
FIND_MIN_FILL = 0.9
#: The surfaces of each hue tried, largest first: a green plant larger than a green screen hides nothing.
FIND_CANDIDATES = 3
#: The most screens :func:`found_screens` hands out of one picture: the screen inserts one scene takes at most.
FIND_SCREENS = 12
#: A surface found is a screen only when each of its four edges runs this straight: the edge's chord over the
#: length of the curve its outline follows (:func:`edge_straightness`) - 1 for a straight edge, 0.93 for the rim of a
#: mug seen from a little above. A screen bowing up to about 10% of its edge stays one: the flatness lint says so.
MIN_EDGE_STRAIGHTNESS = 0.97
#: ... its fill a key colour (its median colour of the key's hue, as saturated as :func:`_surfaces` takes) or dark -
#: a screen switched off, its luma (0-255) under this ...
DARK_FILL_LUMA = 50.0
#: ... and never the picture's brightest region: a fill brighter than any saturated key is lit to (pure green reads
#: 150) and than all the rest of the picture but its brightest share is a lamp, a window, a lit bottle.
BRIGHT_FILL_LUMA = 170.0
BRIGHT_PERCENTILE = 99.0
#: The span of an edge (shares of it, from its first corner) whose outline points tell its straightness: the corners
#: are where two edges blur into each other.
EDGE_SPAN = (0.1, 0.9)
#: A replaced screen is carried to its bezel: each fitted edge pushed outward over the pixels still the key by
#: chroma distance, at most this share of the quad's width.
BEZEL_REACH = 0.03
#: The band around a replaced screen or a keyed insert scanned for the key colour left: this share of the
#: quad's width past its edges ...
SPILL_BAND = 0.03
#: ... and the share of that band still the key colour past which the shot, or the scene, fails as "key spill".
MAX_SPILL_SHARE = 0.002
#: The frames whose mattes are pooled - their median on the surface's own plane - for a key read frame by frame.
MATTE_WINDOW = 5
#: The surface's plane a matte is pooled on is at most this long.
MATTE_SIDE = 768
#: A matte as drawn holds at least this share of its pixels from one frame to the next - what toggled back over the
#: last two frames taken out (:func:`edge_hold`) - or the key flickers.
MIN_EDGE_HOLD = 0.98
#: The dilated key's feather, in pixels of a 1080p frame: the key's share melts into the bezel, never a hard line.
BEZEL_FEATHER = 1.5
#: A screenshot's content standing past the bezel by more than this many pixels of a 1080p frame is cropped.
CONTENT_OVERFLOW_PX = 1.0
#: A ``cover`` fit that would cut more than this share of a screenshot's content is refused before anything is paid.
MAX_CONTENT_CUT = 0.01
_TO_YCC = (
    (0.299, 0.587, 0.114),
    (-0.168736, -0.331264, 0.5),
    (0.5, -0.418688, -0.081312),
)


class ScreenError(ValueError):
    """A ``screen`` the platform refuses; ``str()`` names the field and why."""


class ScreenLostError(RuntimeError):
    """The shot's model did not keep the screen a flat surface; ``str()`` is the shot's failure."""


class KeySpillError(ScreenLostError):
    """The key colour is left around the replaced screen: ``key spill``, the shot's failure."""


class KeyFlickerError(ScreenLostError):
    """The matte of the replaced screen jitters from frame to frame: ``key flicker``, the shot's failure."""


class ScreenContentCroppedError(ScreenLostError):
    """The screenshot's content stands past the screen's bezel: ``screen content cropped``."""


@dataclass(frozen=True)
class Key:
    """The flat colour a screen is filled with."""

    name: str
    rgb: tuple[int, int, int]

    @property
    def hex(self) -> str:
        return "#{:02x}{:02x}{:02x}".format(*self.rgb)

    @classmethod
    def from_hex(cls, name: str, value: str) -> Key:
        text = value.lstrip("#")
        return cls(name, (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)))


@dataclass(frozen=True)
class ScreenSpec:
    """A shot's screen: the screenshot drawn into it, where it is in the start picture, how it is fitted and lit."""

    file_id: str
    #: Empty until :func:`find_screen` finds it, for a screen asked without its quad.
    quad: tuple[tuple[float, float], ...]
    #: ``contain``: the screenshot's content box whole, letterboxed in its own background colour.
    fit: str = DEFAULT_FIT
    light: float = DEFAULT_LIGHT
    key: Key | None = None
    #: The quad was found in the start picture (:func:`find_screen`), not given: found again in another one.
    found: bool = False

    def as_params(self) -> dict:
        stored: dict[str, Any] = {
            "file_id": self.file_id,
            "quad": [list(p) for p in self.quad],
            "fit": self.fit,
            "light": self.light,
        }
        if self.key is not None:
            stored["key"] = {"name": self.key.name, "hex": self.key.hex}
        if self.found:
            stored["found"] = True
        return stored

    @classmethod
    def from_params(cls, stored: dict) -> ScreenSpec:
        key = stored.get("key") if isinstance(stored.get("key"), dict) else None
        return cls(
            str(stored["file_id"]),
            tuple((float(x), float(y)) for x, y in stored["quad"]),
            str(stored.get("fit") or DEFAULT_FIT),
            float(stored.get("light", DEFAULT_LIGHT)),
            Key.from_hex(str(key["name"]), str(key["hex"])) if key else None,
            bool(stored.get("found")),
        )


def start_quad(stored: Any) -> list[tuple[float, float]] | None:
    """A shot's stored ``screen`` as its quad on the take's first frame, 0-1; ``None`` without one.

    The quad is in the start picture's pixels (``start_size``), the frame the take starts on.
    """
    if not isinstance(stored, dict) or not stored.get("quad") or not stored.get("start_size"):
        return None
    width, height = (max(float(side), 1.0) for side in stored["start_size"])
    return [(float(x) / width, float(y) / height) for x, y in stored["quad"]]


def _number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ScreenError(f"{label} must be a number.") from None
    if not math.isfinite(number):
        raise ScreenError(f"{label} must be a number.")
    return number


def area(quad: Sequence[Sequence[float]]) -> float:
    """The signed area of a polygon (the shoelace formula)."""
    points = [tuple(p) for p in quad]
    return 0.5 * sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:] + points[:1], strict=True))


def _convex(quad: Sequence[Sequence[float]]) -> bool:
    signs = set()
    for i in range(4):
        a, b, c = quad[i], quad[(i + 1) % 4], quad[(i + 2) % 4]
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if abs(cross) > 1e-9:
            signs.add(cross > 0)
    return len(signs) == 1


def parse_screen(raw: Any) -> ScreenSpec:
    """A call's ``screen``, checked field by field (the picture sizes are checked by :func:`check_fits`).

    Without ``quad`` the spec's quad is empty: the caller finds it in the start picture (:func:`find_screen`).
    """
    from app.services.builtin_tools._shared import _file_handle_key

    if not isinstance(raw, dict) or not str(raw.get("file_id") or "").strip():
        raise ScreenError("screen names the real screenshot drawn into the screen (screen.file_id).")
    fit = str(raw.get("fit") or DEFAULT_FIT)
    if fit not in FITS:
        raise ScreenError(f"screen.fit is one of {', '.join(FITS)}.")
    light = _number(raw.get("light", DEFAULT_LIGHT), "screen.light")
    if not 0.0 <= light <= 1.0:
        raise ScreenError("screen.light is between 0 (the screenshot as it is) and 1 (all the light the shot draws).")
    file_id = _file_handle_key(str(raw["file_id"]))
    quad = raw.get("quad")
    if quad is None:
        return ScreenSpec(file_id, (), fit, light)
    if not isinstance(quad, list) or len(quad) != 4 or not all(isinstance(p, list | tuple) and len(p) == 2 for p in quad):
        raise ScreenError(
            "screen.quad is the screen's four corners [x, y] in the start picture's pixels: "
            "top left, top right, bottom right, bottom left."
        )
    points = tuple((_number(p[0], "screen.quad"), _number(p[1], "screen.quad")) for p in quad)
    if not _convex(points):
        raise ScreenError("screen.quad is not a convex quadrilateral: give the four corners in order, around the screen.")
    return ScreenSpec(file_id, points, fit, light)


def check_fits(spec: ScreenSpec, size: tuple[int, int]) -> None:
    """Refuse a quad that is not inside a start picture of *size*, or too small to hold a screen."""
    width, height = size
    for x, y in spec.quad:
        if not (-CORNER_MARGIN * width <= x <= (1 + CORNER_MARGIN) * width) or not (
            -CORNER_MARGIN * height <= y <= (1 + CORNER_MARGIN) * height
        ):
            raise ScreenError(
                f"screen.quad corner [{x:g}, {y:g}] is outside the start picture ({width}x{height}): "
                "the corners are in its pixels."
            )
    if abs(area(spec.quad)) < MIN_SCREEN_SHARE * width * height:
        raise ScreenError("screen.quad encloses almost nothing of the start picture: give the screen's four corners.")


def check_content_fit(spec: ScreenSpec, picture: Any) -> None:
    """Refuse a ``cover`` fit that would cut the screenshot's content into the screen's shape.

    A ``cover`` fit cuts the screenshot's edges to the quad's proportions: fine
    while it cuts flat margins, a lost sidebar or right column when it cuts
    the content (``pictures.content_box``, more than :data:`MAX_CONTENT_CUT`
    of it). ``contain`` keeps the content whole, letterboxed in the
    screenshot's own background colour.
    """
    from app.services.video_studio.pictures import content_box, cover_cuts, quad_size

    if spec.fit != "cover" or not spec.quad:
        return
    quad_w, quad_h = quad_size(tuple(map(tuple, spec.quad)))
    box = content_box(picture)
    cut = cover_cuts(picture.size, box, quad_w, quad_h)
    if box is not None and cut > MAX_CONTENT_CUT:
        width, height = box[2] - box[0], box[3] - box[1]
        sides = "left and right" if width / max(height, 1) > quad_w / max(quad_h, 1) else "top and bottom"
        raise ScreenError(
            f"screen.fit: cover would cut {cut:.0%} of the screenshot's content ({width}x{height} px of its "
            f"{picture.width}x{picture.height}) at its {sides} to fill the screen's {quad_w:.0f}x{quad_h:.0f} shape: "
            "use fit: contain (the default), which keeps the content whole and letterboxes in the screenshot's own "
            "background colour, or a start picture whose screen has the screenshot's proportions."
        )


# ── Colour ────────────────────────────────────────────────────────────────


def _chromaticity(rgb: Any) -> tuple[Any, Any]:
    """``(luma, chroma / luma)`` of an ``(..., 3)`` RGB array: the chroma a uniform shading leaves unchanged."""
    import numpy as np

    ycc = np.asarray(rgb, dtype=np.float32) @ np.asarray(_TO_YCC, dtype=np.float32).T
    luma = ycc[..., 0]
    return luma, ycc[..., 1:] / (luma[..., None] + _LUMA_PAD)


def key_distance(rgb: Any, key: Key) -> Any:
    """How far each pixel is from the key, relative to the key's own chromaticity (0 = the key)."""
    import numpy as np

    _, reference = _chromaticity(np.asarray(key.rgb, dtype=np.float32))
    luma, chroma = _chromaticity(rgb)
    distance = np.linalg.norm(chroma - reference, axis=-1) / max(float(np.linalg.norm(reference)), 1e-6)
    distance[luma < _MIN_KEY_LUMA] = 9.0
    return distance


def key_matte_constants() -> dict[str, Any]:
    """The numbers :func:`key_matte` reads, for the editor's preview that tests a key with the same arithmetic
    (``compositor_page.keyMatte``)."""
    return {
        "to_ycc": [list(row) for row in _TO_YCC],
        "luma_pad": _LUMA_PAD,
        "min_luma": _MIN_KEY_LUMA,
        "key_in": KEY_IN,
        "low": _MATTE_LOW,
        "high": _MATTE_HIGH,
        "across_in": _ACROSS_IN,
        "across_out": _ACROSS_OUT,
    }


def key_matte(rgb: Any, key: Key, tolerance: float | None = None) -> Any:
    """The share of the key in each pixel: 1 on the flat surface, 0 on what stands in front of it, between at an edge.

    Read along the key's own chromaticity (how far a pixel goes toward it),
    so a pixel blended with the key at an edge - by the camera's blur or the
    encoder's - is the part key it is, and gated by how far it strays from
    that direction, so another colour is never taken for the key.
    *tolerance* (a screen insert's ``key.tolerance``) widens or narrows
    how far it may stray, :data:`KEY_IN` being the screen replacement's own.
    """
    import numpy as np

    scale = (tolerance / KEY_IN) if tolerance else 1.0
    _, reference = _chromaticity(np.asarray(key.rgb, dtype=np.float32))
    magnitude = max(float(np.linalg.norm(reference)), 1e-6)
    direction = reference / magnitude
    luma, chroma = _chromaticity(rgb)
    along = (chroma @ direction) / magnitude
    across = np.linalg.norm(chroma - (chroma @ direction)[..., None] * direction, axis=-1) / magnitude
    share = np.clip((along - _MATTE_LOW) / (_MATTE_HIGH - _MATTE_LOW), 0.0, 1.0)
    pure = np.clip((_ACROSS_OUT * scale - across) / ((_ACROSS_OUT - _ACROSS_IN) * scale), 0.0, 1.0)
    matte = share * pure
    matte[luma < _MIN_KEY_LUMA * 0.5] = 0.0
    return matte


def despill(region: Any, alpha: Any, key: Key, inside: Any, reference: float | None = None) -> Any:
    """*region* (``H x W x 3`` float RGB over a keyed surface) with the key unmixed out of it, and its spill.

    A pixel there is *alpha* parts the key (at the surface's exposure,
    *reference* luma: the key's own when unknown) and ``1 - alpha`` parts
    itself: the key's share is taken out - its brightness with it, not only
    its tint - and what is left of its colour along the key's chromaticity is
    removed where *inside* (the surface's quad), so a cat's fur in front of a
    green screen keeps no green fringe.
    """
    import numpy as np

    to_ycc = np.asarray(_TO_YCC, dtype=np.float32).T
    key_rgb = np.asarray(key.rgb, dtype=np.float32)
    key_luma = float(key_rgb @ to_ycc[:, 0])
    level = key_luma if reference is None else reference
    shown = region - np.asarray(alpha, dtype=np.float32)[..., None] * (key_rgb * (level / max(key_luma, 1.0)))
    ycc = shown @ to_ycc
    _, reference_chroma = _chromaticity(key_rgb)
    direction = reference_chroma / max(float(np.linalg.norm(reference_chroma)), 1e-6)
    gate = np.asarray(inside, dtype=bool)[..., None]
    chroma = ycc[..., 1:] - np.clip(ycc[..., 1:] @ direction, 0.0, None)[..., None] * direction * gate
    return np.clip(_rgb_from(ycc[..., 0], chroma[..., 0], chroma[..., 1]), 0.0, 255.0)


#: A named key (``green``, ``blue``, ``magenta``) is the shade of it the surface shows (:func:`sample_key`): the
#: median of the quad's pixels of its hue (:data:`FIND_HUE_DEGREES`) and saturated past :data:`FIND_MIN_SATURATION`.
#: Fewer of them than this share of the quad is no surface of that colour: the name's own colour is keyed on.
SAMPLE_MIN_SHARE = 0.05
#: A colour whose mean chroma (YCbCr, 0-255) is under this has no hue to keep (``screen_insert.colour_fidelity``): a
#: grey, a white.
MIN_HUE_CHROMA = 8.0
#: The surface's spread (:func:`key_spread`): the relative chromaticity distance from the sampled key that
#: this share of the surface's pixels stay within, read on the insert's first footage frame ...
SPREAD_PERCENTILE = 97.0
#: ... and the matte's tolerance is this many times it (the surface under uneven light is all surface), widened to at
#: most this - never lowering the tolerance the author gave.
SPREAD_FACTOR = 1.6
MAX_SPREAD_TOLERANCE = 0.7


def sample_key(pixels: Any, quad: Sequence[Sequence[float]], named: Key) -> Key | None:
    """The shade of *named* the surface inside *quad* (pixels of *pixels*, ``H x W x 3``) shows; ``None`` for none.

    A video model paints a "chroma green" screen its own green - about
    (32, 255, 30), never the (0, 177, 64) a key is named for - and a matte
    read against the name keys such a surface half-way, the footage blending
    under the insert. The key is the median colour of the
    quad's pixels within :data:`FIND_HUE_DEGREES` of the name's hue and
    saturated past :data:`FIND_MIN_SATURATION`, at least
    :data:`SAMPLE_MIN_SHARE` of the quad.
    """
    import numpy as np
    from PIL import Image

    height, width = pixels.shape[:2]
    xs, ys = [p[0] for p in quad], [p[1] for p in quad]
    x0, x1 = max(0, math.floor(min(xs))), min(width, math.ceil(max(xs)))
    y0, y1 = max(0, math.floor(min(ys))), min(height, math.ceil(max(ys)))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    region = np.ascontiguousarray(pixels[y0:y1, x0:x1, :3], dtype=np.uint8)
    inside = _polygon_mask((x1 - x0, y1 - y0), [(x - x0, y - y0) for x, y in quad])
    hsv = np.asarray(Image.fromarray(region, "RGB").convert("HSV"), dtype=np.float32) / 255.0
    keyed = inside & in_hue_band(hsv[..., 0] * 360.0, hue_band(named)) & (hsv[..., 1] >= FIND_MIN_SATURATION)
    if int(keyed.sum()) < max(16, SAMPLE_MIN_SHARE * int(inside.sum())):
        return None
    median = np.median(region[keyed], axis=0)
    return Key(named.name, (int(median[0]), int(median[1]), int(median[2])))


def hue_band(key: Key | tuple[str, tuple[int, int, int]]) -> tuple[float, float]:
    """``(from, to)`` in degrees of the hues a surface of *key* is read in: its own hue within
    :data:`FIND_HUE_DEGREES`, joined to its name's band (:data:`FIND_HUE_BANDS`: green 90-150) - a green screen a
    model paints yellower or bluer than the key is still the key's surface."""
    name, rgb = (key.name, key.rgb) if isinstance(key, Key) else key
    hue = _hue_degrees(rgb)
    low, high = hue - FIND_HUE_DEGREES, hue + FIND_HUE_DEGREES
    if name in FIND_HUE_BANDS:
        named_low, named_high = FIND_HUE_BANDS[name]
        low, high = min(low, named_low), max(high, named_high)
    return low, high


def in_hue_band(degrees: Any, band: tuple[float, float]) -> Any:
    """Whether each hue of *degrees* (0-360) lies in *band* (``(from, to)``, either end past 0 or 360 wrapping round)."""
    low, high = band
    return (degrees - low) % 360.0 <= (high - low)


def key_spread(pixels: Any, quad: Sequence[Sequence[float]], key: Key) -> float | None:
    """How far the surface inside *quad* strays from *key*: the :data:`SPREAD_PERCENTILE` of its pixels'
    :func:`key_distance`, over the pixels of the key's hue; ``None`` when the surface shows too little of it.

    A surface under uneven light - lit at one corner, in shadow at another -
    is one surface: a matte read with a tolerance under its spread takes its
    lit part for something in front of it. The spread is read once, on the
    insert's first footage frame, and widens the tolerance
    (:func:`spread_tolerance`).
    """
    import numpy as np
    from PIL import Image

    height, width = pixels.shape[:2]
    xs, ys = [p[0] for p in quad], [p[1] for p in quad]
    x0, x1 = max(0, math.floor(min(xs))), min(width, math.ceil(max(xs)))
    y0, y1 = max(0, math.floor(min(ys))), min(height, math.ceil(max(ys)))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    region = np.ascontiguousarray(pixels[y0:y1, x0:x1, :3], dtype=np.uint8)
    inside = _polygon_mask((x1 - x0, y1 - y0), [(x - x0, y - y0) for x, y in quad])
    hsv = np.asarray(Image.fromarray(region, "RGB").convert("HSV"), dtype=np.float32) / 255.0
    apart = np.abs((hsv[..., 0] * 360.0 - _hue_degrees(key.rgb) + 180.0) % 360.0 - 180.0)
    keyed = inside & (apart <= FIND_HUE_DEGREES) & (hsv[..., 1] >= FIND_MIN_SATURATION * 0.6)
    if int(keyed.sum()) < max(16, SAMPLE_MIN_SHARE * int(inside.sum())):
        return None
    distance = key_distance(region.astype(np.float32), key)[keyed]
    return float(np.percentile(distance, SPREAD_PERCENTILE))


def spread_tolerance(tolerance: float, spread: float | None) -> float:
    """The matte's tolerance for a surface of *spread*: the author's *tolerance*, widened to the surface's.

    The spread widens it up to :data:`MAX_SPREAD_TOLERANCE`; a tolerance the author gave past it stands as given.
    """
    if spread is None:
        return tolerance
    return float(max(tolerance, min(MAX_SPREAD_TOLERANCE, SPREAD_FACTOR * spread)))


def _key_channels(key: Key) -> tuple[bool, int, int, int]:
    """Whether *key* is a secondary colour (two channels strong, magenta), and its channels strongest to weakest."""
    import numpy as np

    key_rgb = np.asarray(key.rgb, dtype=np.float32)
    order = np.argsort(key_rgb)[::-1]
    strongest, second, weakest = (int(index) for index in order)
    secondary = key_rgb[second] > 0.6 * key_rgb[strongest] and key_rgb[second] > 2.0 * key_rgb[weakest]
    return bool(secondary), strongest, second, weakest


def key_share(rgb: Any, key: Key) -> Any:
    """How much of each pixel of *rgb* (``... x 3`` float) is the key, by colour difference: 0-1.

    The classic linear estimate the unmix needs - :func:`key_matte` says
    whether a pixel IS the surface, saturating well before it is all key,
    which is what a matte wants and what an unmix must not read as the share
    to take out. For a primary key: the key channel above the max of the
    other two, over the key's own excess; for a secondary (magenta): the two
    strong channels' min above the weak one. A pixel of another colour reads
    0: nothing of it is taken out.
    """
    import numpy as np

    pixels = np.asarray(rgb, dtype=np.float32)
    key_rgb = np.asarray(key.rgb, dtype=np.float32)
    secondary, strongest, second, weakest = _key_channels(key)
    if secondary:
        excess = np.minimum(pixels[..., strongest], pixels[..., second]) - pixels[..., weakest]
        own = min(float(key_rgb[strongest]), float(key_rgb[second])) - float(key_rgb[weakest])
    else:
        excess = pixels[..., strongest] - np.maximum(pixels[..., second], pixels[..., weakest])
        own = float(key_rgb[strongest]) - max(float(key_rgb[second]), float(key_rgb[weakest]))
    return np.clip(excess / max(own, 1.0), 0.0, 1.0)


def despill_clamp(rgb: Any, key: Key, where: Any = None) -> Any:
    """*rgb* (``... x 3`` float) with the key's spill taken out by clamping: never by inverting its chroma.

    The classic rule: a pixel's key channel (the key's dominant one - green
    for a green key, blue for a blue one) is clamped to the max of the other
    two, so a green fringe on a person's edge goes neutral and never past it;
    for a key of two dominant channels (magenta: red and blue) the weak
    channel is lifted to the min of the two, the same rule turned around.
    Applied where *where* (a boolean mask) says, or everywhere.
    """
    import numpy as np

    pixels = np.asarray(rgb, dtype=np.float32)
    secondary, strongest, second, weakest = _key_channels(key)
    out = pixels.copy()
    if secondary:
        # A secondary (magenta): its weak channel lifted to the lesser of the two it is made of.
        lifted = np.maximum(out[..., weakest], np.minimum(out[..., strongest], out[..., second]))
        out[..., weakest] = lifted if where is None else np.where(np.asarray(where, dtype=bool), lifted, out[..., weakest])
        return out
    others = np.maximum(out[..., second], out[..., weakest])
    clamped = np.minimum(out[..., strongest], others)
    out[..., strongest] = clamped if where is None else np.where(np.asarray(where, dtype=bool), clamped, out[..., strongest])
    return out


def to_ycc(rgb: Any) -> Any:
    """*rgb* (``... x 3``) as ``N x 3`` YCbCr (BT.601, full range, chroma centred on 0)."""
    import numpy as np

    return np.asarray(rgb, dtype=np.float32).reshape(-1, 3) @ np.asarray(_TO_YCC, dtype=np.float32).T


def _polygon_mask(size: tuple[int, int], quad: Sequence[Sequence[float]]) -> Any:
    import numpy as np
    from PIL import Image, ImageDraw

    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).polygon([tuple(p) for p in quad], fill=255)
    return np.asarray(mask) > 0


def pick_key(picture: Any, quad: Sequence[Sequence[float]]) -> Key:
    """The candidate colour the picture (outside the screen) holds least of: the one a matte confuses with nothing."""
    import numpy as np
    from scipy import ndimage

    sample = picture.convert("RGB")
    scale = min(1.0, 320 / max(sample.size))
    if scale < 1.0:
        sample = sample.resize((max(1, round(sample.width * scale)), max(1, round(sample.height * scale))))
    pixels = np.asarray(sample)
    # The screen and its blended border are what gets replaced: only the room counts.
    screen = _polygon_mask(sample.size, [(x * scale, y * scale) for x, y in quad])
    outside = ~ndimage.binary_dilation(screen, iterations=3)
    best_share, best = math.inf, Key(*KEY_CANDIDATES[0])
    for name, rgb in KEY_CANDIDATES:
        key = Key(name, rgb)
        near = float((key_distance(pixels, key)[outside] < KEY_OUT).mean()) if outside.any() else 0.0
        if near < best_share:
            best_share, best = near, key
    return best


def _key_of(spec: ScreenSpec) -> Key:
    if spec.key is None:
        raise ScreenError("The screen has no flat colour chosen: ask the shot again.")
    return spec.key


def placeholder(data: bytes, spec: ScreenSpec, start_size: tuple[int, int]) -> bytes:
    """The start picture the model is given: *data* with the screen's quad filled with the key (PNG).

    *data* may have been made smaller than the picture the quad was given in
    (*start_size*, ``clips.start_picture``): the quad is scaled to it.
    """
    import io

    from PIL import Image, ImageDraw

    key = _key_of(spec)
    with Image.open(io.BytesIO(data)) as opened:
        opened.load()
        drawn = opened.convert("RGB")
    sx, sy = drawn.width / max(start_size[0], 1), drawn.height / max(start_size[1], 1)
    ImageDraw.Draw(drawn).polygon([(x * sx, y * sy) for x, y in spec.quad], fill=key.rgb)
    out = io.BytesIO()
    drawn.save(out, format="PNG")
    return out.getvalue()


def expected_start(data: bytes, picture: Any, spec: ScreenSpec, start_size: tuple[int, int]) -> bytes:
    """What the shot's first frame should be: the start picture sent (*data*, the screen flat) with *picture* drawn in (PNG).

    What a screen shot's first frame is measured against (``frame_qc``), so
    the screenshot drawn into it never reads as a drift from its start.
    """
    import io

    import numpy as np
    from PIL import Image

    from app.services.video_studio.pictures import quad_size

    key = _key_of(spec)
    with Image.open(io.BytesIO(data)) as opened:
        opened.load()
        frame = np.asarray(opened.convert("RGB")).copy()
    sx, sy = frame.shape[1] / max(start_size[0], 1), frame.shape[0] / max(start_size[1], 1)
    corners = np.array([(x * sx, y * sy) for x, y in spec.quad], dtype=float)
    shaped, _content = content_fitted(picture, *quad_size(tuple(map(tuple, corners))), spec.fit)
    drawn, _hold = composite(frame, shaped, corners, key, 0.0)
    out = io.BytesIO()
    Image.fromarray(drawn).save(out, format="PNG")
    return out.getvalue()


def provider_prompt(prompt: str, key: Key) -> str:
    """The shot's prompt as the video model is asked it: the screen kept one flat surface of the key's colour."""
    return (
        f"{prompt.strip().rstrip('.')}. The screen stays one flat, uniform, evenly lit pure {key.name} surface "
        "for the whole shot - no interface, text, image or logo on it, only soft light and reflections - and "
        "its four corners stay in view."
    )


def provider_negative(negative_prompt: str | None) -> str:
    """The negative prompt with what must never appear on the flat surface."""
    kept = (negative_prompt or "").strip().rstrip(",")
    extra = "text on the screen, user interface on the screen, picture on the screen"
    return f"{kept}, {extra}" if kept else extra


# ── Tracking ──────────────────────────────────────────────────────────────


def _intersect(p1: Any, d1: Any, p2: Any, d2: Any) -> Any | None:
    import numpy as np

    matrix = np.array([[d1[0], -d2[0]], [d1[1], -d2[1]]], dtype=float)
    if abs(np.linalg.det(matrix)) < 1e-9:
        return None
    s, _t = np.linalg.solve(matrix, np.asarray(p2, float) - np.asarray(p1, float))
    return np.asarray(p1, float) + s * np.asarray(d1, float)


def _edge_line(points: Any, a: Any, b: Any, tolerance: float) -> tuple[Any, Any] | None:
    """The line through the outline *points* lying along ``a -> b`` (within *tolerance*), fitted robustly; ``None`` when too few."""
    import numpy as np

    length = float(np.linalg.norm(b - a))
    if length < 1e-6:
        return None
    u = (b - a) / length
    n = np.array([-u[1], u[0]])
    rel = points - a
    t = rel @ u / length
    chosen = points[(t > 0.1) & (t < 0.9) & (np.abs(rel @ n) < tolerance)]
    for _ in range(3):
        if len(chosen) < 8:
            return None
        mean = chosen.mean(axis=0)
        _, _, vt = np.linalg.svd(chosen - mean, full_matrices=False)
        direction = vt[0] if vt[0] @ u >= 0 else -vt[0]
        normal = np.array([-direction[1], direction[0]])
        residual = np.abs((chosen - mean) @ normal)
        # What crosses the edge (a hand, a reflection) strays from it: dropped, the rest refitted.
        keep = residual < max(1.5, 2.5 * float(np.median(residual)))
        if keep.all():
            break
        chosen = chosen[keep]
    return mean, direction


def fit_corners(points: Any, coarse: Any) -> Any:
    """The quad's corners from the outline *points* of the surface: each edge fitted to a line, the lines intersected.

    *coarse* are the corners of the frame before (the surface moves little from
    one frame to the next): each edge keeps the outline points along the line
    between its two coarse corners, away from the corners themselves, fitted
    robustly, so a hand across an edge or covering a corner, or a corner out of
    the frame, moves nothing - the corner is where its two edges meet. An edge
    with too few points keeps its coarse line.
    """
    import numpy as np

    corners = np.asarray(coarse, dtype=float)
    for tolerance in (0.08, 0.03):
        lines = []
        for i in range(4):
            a, b = corners[i], corners[(i + 1) % 4]
            fitted = _edge_line(points, a, b, max(3.0, tolerance * float(np.linalg.norm(b - a))))
            lines.append(fitted if fitted is not None else (a, (b - a) / max(float(np.linalg.norm(b - a)), 1e-6)))
        found = []
        for i in range(4):
            before, after = lines[(i - 1) % 4], lines[i]
            point = _intersect(before[0], before[1], after[0], after[1])
            found.append(corners[i] if point is None else point)
        corners = np.array(found, dtype=float)
    return corners


class Tracker:
    """Follows the flat surface of the key from frame to frame, starting from the quad the shot was asked with."""

    def __init__(self, key: Key, quad: Sequence[Sequence[float]], size: tuple[int, int]) -> None:
        import numpy as np

        self.key = key
        self.size = size
        self.corners = np.array(quad, dtype=float)

    def _roi(self) -> tuple[int, int, int, int]:
        width, height = self.size
        low, high = self.corners.min(axis=0), self.corners.max(axis=0)
        pad = 0.3 * (high - low) + 16
        x0, y0 = (max(0, int(v)) for v in low - pad)
        x1, y1 = int(min(width, high[0] + pad[0])), int(min(height, high[1] + pad[1]))
        return x0, y0, x1, y1

    def track(self, frame: Any) -> tuple[Any, bool]:
        """The surface's corners in *frame* (``H x W x 3``), and whether it was found (else the last corners)."""
        import numpy as np
        from scipy import ndimage

        x0, y0, x1, y1 = self._roi()
        if x1 - x0 < 4 or y1 - y0 < 4:
            return self.corners, False
        mask = key_distance(frame[y0:y1, x0:x1], self.key) < KEY_TRACK
        labels, count = ndimage.label(mask)
        if count == 0:
            return self.corners, False
        sizes = ndimage.sum(mask, labels, range(1, count + 1))
        blob = ndimage.binary_fill_holes(labels == int(np.argmax(sizes)) + 1)
        expected = abs(area(self.corners))
        if blob.sum() < 0.25 * expected:
            return self.corners, False
        outline = blob & ~ndimage.binary_erosion(blob)
        ys, xs = np.nonzero(outline)
        points = np.column_stack([xs + x0, ys + y0]).astype(float)
        corners = fit_corners(points, self.corners)
        found_area = abs(area(corners))
        if not _convex(corners) or not 0.5 * expected <= found_area <= 2.0 * expected:
            return self.corners, False
        self.corners = corners
        return corners, True


# ── Finding ───────────────────────────────────────────────────────────────


def _grown(corners: Any, by: float) -> Any:
    """*corners* moved *by* pixels away from their centre, along the diagonals."""
    import numpy as np

    away = corners - corners.mean(axis=0)
    return corners + away / np.maximum(np.linalg.norm(away, axis=1, keepdims=True), 1e-6) * by


def _hue_degrees(rgb: tuple[int, int, int]) -> float:
    import colorsys

    return colorsys.rgb_to_hsv(*(c / 255.0 for c in rgb))[0] * 360.0


def _surfaces(
    pixels: Any, hsv: Any, band: tuple[float, float], smallest: float, most: int = FIND_CANDIDATES
) -> Iterator[Any]:
    """The flat surfaces of *band*'s hues in *pixels* of at least *smallest* pixels, largest first (*most* of them):
    each a filled mask.

    A connected region of that hue band, saturated and bright past :data:`FIND_MIN_SATURATION` and
    :data:`FIND_MIN_VALUE` (never its distance to a pure key colour), read again around its own median colour
    as the tracker reads a key (:func:`key_distance`), so its edges are the surface's, not the hue band's.
    """
    import numpy as np
    from scipy import ndimage

    candidate = in_hue_band(hsv[..., 0] * 360.0, band) & (hsv[..., 1] >= FIND_MIN_SATURATION) & (hsv[..., 2] >= FIND_MIN_VALUE)
    labels, count = ndimage.label(candidate)
    if count == 0:
        return
    sizes = ndimage.sum(candidate, labels, range(1, count + 1))
    boxes = ndimage.find_objects(labels)
    height, width = candidate.shape
    for index in np.argsort(sizes)[::-1][:most]:
        if sizes[index] < smallest:
            return
        rows, cols = boxes[index]
        pad_y, pad_x = (rows.stop - rows.start) // 4 + 2, (cols.stop - cols.start) // 4 + 2
        y0, y1 = max(0, rows.start - pad_y), min(height, rows.stop + pad_y)
        x0, x1 = max(0, cols.start - pad_x), min(width, cols.stop + pad_x)
        coarse = labels[y0:y1, x0:x1] == index + 1
        region = pixels[y0:y1, x0:x1]
        colour = tuple(int(v) for v in np.median(region[coarse], axis=0))
        flat_labels, flat_count = ndimage.label(key_distance(region, Key("found", colour)) < KEY_TRACK)
        if flat_count == 0:
            continue
        overlap = ndimage.sum(coarse, flat_labels, range(1, flat_count + 1))
        surface = np.zeros_like(candidate)
        surface[y0:y1, x0:x1] = ndimage.binary_fill_holes(flat_labels == int(np.argmax(overlap)) + 1)
        yield surface


def edge_parabola(u: Any, v: Any, *, min_points: int = 8) -> Any | None:
    """``(c, b, a)`` of the parabola ``v = c u^2 + b u + a`` most of an edge's points follow (*u* along the edge, *v*
    across it), or ``None`` for fewer than *min_points*: those far off it (a hand, a reflection) dropped and the rest
    fitted again. What both an edge's bow (``surface_refine``) and its straightness (:func:`edge_straightness`) read.
    """
    import numpy as np

    u, v = np.asarray(u, dtype=float), np.asarray(v, dtype=float)
    if len(u) < min_points:
        return None
    keep = np.ones(len(u), dtype=bool)
    coefficients = np.polyfit(u, v, 2)
    for _ in range(3):
        residual = np.abs(np.polyval(coefficients, u) - v)
        spread = 1.4826 * float(np.median(residual[keep])) if keep.any() else 0.0
        kept = residual <= max(3.0 * spread, 1.0)
        if int(kept.sum()) < min_points or bool((kept == keep).all()):
            break
        keep = kept
        coefficients = np.polyfit(u[keep], v[keep], 2)
    return coefficients


def edge_straightness(points: Any, corners: Any) -> tuple[float | None, ...]:
    """How straight each edge of the quad *corners* runs along the outline *points* of its surface, corner to
    corner: the edge's chord over the length of the curve its points follow (:func:`edge_parabola`), 1 for a straight
    edge. ``None`` for an edge too few points tell (out of the frame, behind a hand).

    An outline point belongs to the edge whose line it is nearest, within :data:`EDGE_SPAN` of it.
    """
    import numpy as np

    found = np.asarray(points, dtype=float).reshape(-1, 2)
    quad = np.asarray(corners, dtype=float).reshape(4, 2)
    edges, distances = [], []
    for index in range(4):
        a, b = quad[index], quad[(index + 1) % 4]
        length = max(float(np.linalg.norm(b - a)), 1e-6)
        along = (b - a) / length
        rel = found - a
        t, v = rel @ along / length, rel @ np.array([-along[1], along[0]])
        edges.append((t, v, length))
        distances.append(np.where((t >= 0.0) & (t <= 1.0), np.abs(v), np.inf))
    nearest = np.argmin(np.stack(distances), axis=0)
    out: list[float | None] = []
    for index, (t, v, length) in enumerate(edges):
        mine = (nearest == index) & (t > EDGE_SPAN[0]) & (t < EDGE_SPAN[1])
        coefficients = edge_parabola(t[mine] * length, v[mine])
        if coefficients is None or float(np.ptp(t[mine])) < 0.25:
            out.append(None)
            continue
        us = np.linspace(0.0, length, 65)
        vs = np.polyval(coefficients, us)
        arc = float(np.hypot(np.diff(us), np.diff(vs)).sum())
        out.append(round(float(np.hypot(length, vs[-1] - vs[0])) / max(arc, 1e-9), 4))
    return tuple(out)


def _key_hued(colour: Any, band: tuple[float, float]) -> bool:
    """Whether *colour* (RGB) is a pixel :func:`_surfaces` takes for a key of hue *band*: of its hue, saturated and lit."""
    import colorsys

    h, s, v = colorsys.rgb_to_hsv(*(float(c) / 255.0 for c in colour))
    return bool(in_hue_band(h * 360.0, band)) and s >= FIND_MIN_SATURATION and v >= FIND_MIN_VALUE


def _not_a_screen(surface: Any, points: Any, corners: Any, pixels: Any, luma: Any, band: tuple[float, float]) -> str | None:
    """Why the *surface* found (its outline *points*, its quad *corners*) is no screen; ``None`` when it is one.

    A screen is four straight edges (:data:`MIN_EDGE_STRAIGHTNESS`) around a key-coloured or dark fill that is not
    the picture's brightest region (:data:`BRIGHT_FILL_LUMA`): a mug, a bottle or a lamp of a key's hue is none.
    """
    import numpy as np

    straightness = [value for value in edge_straightness(points, corners) if value is not None]
    if straightness and min(straightness) < MIN_EDGE_STRAIGHTNESS:
        return f"one of its edges curves (straightness {min(straightness):.2f}, under {MIN_EDGE_STRAIGHTNESS:.2f})"
    colour = np.median(pixels[surface], axis=0)
    fill = float(np.asarray(_TO_YCC[0], dtype=float) @ colour)
    if fill >= DARK_FILL_LUMA and not _key_hued(colour, band):
        return "its fill is neither a key colour nor dark"
    rest = luma[~surface]
    if fill >= BRIGHT_FILL_LUMA and (rest.size == 0 or fill >= float(np.percentile(rest, BRIGHT_PERCENTILE))):
        return "its fill is the picture's brightest region"
    return None


@dataclass(frozen=True)
class FoundScreen:
    """A screen :func:`found_screen` read off a picture: its corners (the picture's pixels), the key colour it was found
    as, and the median hue of its surface in degrees (what the answer reports)."""

    corners: tuple[tuple[float, float], ...]
    key: str
    hue: float


def _median_hue(hsv: Any, surface: Any) -> float:
    """The median hue of *surface*'s pixels in degrees, read round the circle (a red's 355 and 5 are 0, never 180)."""
    import numpy as np

    angles = hsv[..., 0][surface].astype(np.float64) * 2.0 * math.pi
    turned = math.atan2(float(np.median(np.sin(angles))), float(np.median(np.cos(angles))))
    return round(math.degrees(turned) % 360.0, 1)


def find_screen(
    picture: Any, candidates: Sequence[tuple[str, tuple[int, int, int]]] = KEY_CANDIDATES
) -> tuple[tuple[float, float], ...]:
    """The screen of a start picture: the corners of its largest flat green, blue or magenta surface
    (:func:`found_screen`)."""
    return found_screen(picture, candidates).corners


def found_screen(picture: Any, candidates: Sequence[tuple[str, tuple[int, int, int]]] = KEY_CANDIDATES) -> FoundScreen:
    """The screen of a start picture: its largest flat surface of a key's hue band, as :class:`FoundScreen`.

    For each key colour (*candidates*: :data:`KEY_CANDIDATES`, or a screen
    insert's own key) the largest connected surfaces of its hue band
    (:func:`_surfaces`: :func:`hue_band` - green 90-150 degrees -,
    saturated and bright past :data:`FIND_MIN_SATURATION` and
    :data:`FIND_MIN_VALUE`, never read by their distance to a pure key
    colour); each outline's corners taken at its four diagonal extremes,
    then its four edges fitted to lines and intersected (:func:`fit_corners`,
    what the tracker does on every frame). Only a convex quad holding at
    least :data:`FIND_MIN_SHARE` of the picture and covering its surface as
    the surface covers it (:data:`FIND_MIN_FILL`), its edges straight and its
    fill a key colour or dark but never the picture's brightest region
    (:func:`_not_a_screen`), is a screen; the largest one wins. The
    corners are in *picture*'s pixels, a pixel past the surface's edge so the
    flat colour sent covers all of it. :class:`ScreenError` when there is
    none, saying the hue of the largest surface it refused.
    """
    return found_screens(picture, candidates, most=1)[0]


def found_screens(
    picture: Any, candidates: Sequence[tuple[str, tuple[int, int, int]]] = KEY_CANDIDATES, *, most: int = FIND_SCREENS
) -> list[FoundScreen]:
    """Every screen of a picture, largest first (at most *most*): each flat surface :func:`found_screen` takes for a
    screen, a surface standing inside one already found left out. What ``quad: auto`` hands the screen inserts of one
    scene out of, one screen each (``surface_refine.found_quads``). :class:`ScreenError` when there is none.
    """
    import numpy as np
    from scipy import ndimage

    rgb = picture.convert("RGB")
    scale = min(1.0, FIND_SIDE / max(rgb.size))
    sample = rgb.resize((max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale)))) if scale < 1.0 else rgb
    pixels = np.asarray(sample)
    hsv = np.asarray(sample.convert("HSV"), dtype=np.float32) / 255.0
    luma = pixels.astype(np.float32) @ np.asarray(_TO_YCC[0], dtype=np.float32)
    smallest = FIND_MIN_SHARE * pixels.shape[0] * pixels.shape[1]
    kept: list[tuple[float, Any, str, float, Any]] = []
    refused: tuple[float, str] | None = None
    for name, rgb_key in candidates:
        band = hue_band((name, rgb_key))
        for surface in _surfaces(pixels, hsv, band, smallest, max(FIND_CANDIDATES, most + FIND_CANDIDATES - 1)):
            ys, xs = np.nonzero(surface & ~ndimage.binary_erosion(surface))
            points = np.column_stack([xs, ys]).astype(float)
            diagonal, across = xs + ys, xs - ys
            coarse = points[[np.argmin(diagonal), np.argmax(across), np.argmax(diagonal), np.argmin(across)]]
            corners = fit_corners(points, coarse)
            if not _convex(corners):
                continue
            # The surface and its quad cover each other: a quad cut inside a round blob is no screen.
            inside = _polygon_mask(sample.size, corners)
            covered = float(surface.sum())
            if float((surface & inside).sum()) < FIND_MIN_FILL * float((surface | inside).sum()):
                continue
            hue = _median_hue(hsv, surface)
            if (why := _not_a_screen(surface, points, corners, pixels, luma, band)) is not None:
                if refused is None or covered > refused[0]:
                    refused = (covered, f"the {name} surface it shows, of hue {hue:g} degrees, is no screen: {why}")
                continue
            kept.append((covered, corners, name, hue, inside))
    if not kept:
        raise ScreenError(
            "No screen found in the start picture: give screen.quad, or start from a picture whose screen is one "
            "solid green, blue or magenta surface with its four corners in view"
            + (f" ({refused[1]})." if refused else ".")
        )
    kept.sort(key=lambda found: -found[0])
    out: list[FoundScreen] = []
    taken: list[Any] = []
    for _covered, corners, name, hue, inside in kept:
        # The same screen read twice (two key colours, or a surface inside another): the larger one stands.
        if any(float((inside & other).sum()) > 0.5 * float(inside.sum()) for other in taken):
            continue
        taken.append(inside)
        grown = _grown(corners / scale, max(1.0, 1.0 / scale))
        out.append(FoundScreen(tuple((round(float(x), 1), round(float(y), 1)) for x, y in grown), name, hue))
        if len(out) >= most:
            break
    return out


def smooth(corners: Sequence[Any], found: Sequence[bool], window: int = SMOOTH_WINDOW) -> list[Any]:
    """The corners of every frame, a lost frame's taken between its found neighbours, then averaged over *window*."""
    import numpy as np
    from scipy.ndimage import uniform_filter1d

    track = np.array(corners, dtype=float)
    seen = np.array(found, dtype=bool)
    if seen.any() and not seen.all():
        frames = np.arange(len(track))
        flat = track.reshape(len(track), -1)
        for column in range(flat.shape[1]):
            flat[:, column] = np.interp(frames, frames[seen], flat[seen, column])
        track = flat.reshape(track.shape)
    if len(track) > 2 and window > 1:
        track = uniform_filter1d(track, size=min(window, len(track)), axis=0, mode="nearest")
    return list(track)


# ── Keying, checked ───────────────────────────────────────────────


def band_mask(shape: tuple[int, int], corners: Any, width_px: float) -> Any:
    """On an array of *shape* (``H, W``), the band *width_px* wide just outside the quad *corners* (pixels).

    The quad's own edge pixels - the ones its outline crosses, a blend of
    both sides however the quad is rasterised - are not the band's: it
    starts one pixel past them.
    """
    import numpy as np
    from scipy import ndimage

    height, width = shape
    outer = _polygon_mask((width, height), [tuple(p) for p in _grown(np.asarray(corners, dtype=float), width_px)])
    inner = _polygon_mask((width, height), [tuple(p) for p in np.asarray(corners, dtype=float)])
    return outer & ~ndimage.binary_dilation(inner, iterations=1)


def residual_key_share(frame: Any, corners: Any, key: Key, *, band: float = SPILL_BAND) -> float:
    """The share of the band *band* (of the quad's width) around *corners* in *frame* (``H x W x 3``) still the key.

    The key by chroma distance (:func:`key_distance`, within :data:`KEY_TRACK`:
    what the tracker itself would call the surface), never an RGB tolerance.
    Past :data:`MAX_SPILL_SHARE` the screen - or the insert - spills its key.
    """
    import numpy as np

    from app.services.video_studio.pictures import quad_size

    points = np.asarray(corners, dtype=float)
    quad_w, _quad_h = quad_size(tuple(map(tuple, points)))
    reach = max(2.0, band * quad_w)
    height, width = frame.shape[:2]
    low, high = points.min(axis=0) - reach - 1, points.max(axis=0) + reach + 1
    x0, y0 = max(0, math.floor(low[0])), max(0, math.floor(low[1]))
    x1, y1 = min(width, math.ceil(high[0])), min(height, math.ceil(high[1]))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return 0.0
    ring = band_mask((y1 - y0, x1 - x0), points - np.array([x0, y0], dtype=float), reach)
    if not ring.any():
        return 0.0
    keyed = key_distance(np.asarray(frame[y0:y1, x0:x1, :3], dtype=np.float32), key) < KEY_TRACK
    return round(float(keyed[ring].mean()), 5)


def bezel_corners(keyed: Any, corners: Any, reach: float) -> Any:
    """*corners* (pixels of *keyed*, a boolean ``H x W`` mask of the key by chroma) pushed outward to the bezel.

    Each edge is sampled along its length and the pixels still the key just
    outside it counted, outward, up to *reach*; the edge moves out by the
    median of those runs (a hand across it moves nothing), and the corners
    are where the moved edges meet. The quad the tracker fits is the inner
    flat area; the darker key the model shades along the bezel is still the
    screen, and the screenshot is carried over it.
    """
    import numpy as np

    points = np.asarray(corners, dtype=float)
    if reach < 1.0:
        return points
    height, width = keyed.shape
    centre = points.mean(axis=0)
    lines = []
    for index in range(4):
        a, b = points[index], points[(index + 1) % 4]
        along = b - a
        length = float(np.linalg.norm(along))
        if length < 1e-6:
            lines.append((a, along))
            continue
        unit = along / length
        normal = np.array([-unit[1], unit[0]])
        if normal @ (a - centre) < 0:
            normal = -normal
        runs = []
        for t in np.linspace(0.15, 0.85, 15):
            start = a + along * t
            run, gap = 0, 0
            # Read from the first pixel outside the fitted line (half a pixel out), then outward pixel by pixel.
            for step in range(1, int(reach) + 1):
                x, y = start + normal * (step - 0.5)
                xi, yi = math.floor(x), math.floor(y)
                hit = 0 <= xi < width and 0 <= yi < height and bool(keyed[yi, xi])
                if hit:
                    run, gap = step, 0
                elif gap:
                    break
                else:
                    gap = 1
            runs.append(run)
        pushed = float(np.median(runs))
        lines.append((a + normal * pushed, along))
    moved = []
    for index in range(4):
        before, after = lines[(index - 1) % 4], lines[index]
        point = _intersect(before[0], before[1], after[0], after[1])
        moved.append(points[index] if point is None else point)
    found = np.array(moved, dtype=float)
    return found if _convex(found) else points


def _to_plane(values: Any, corners: Any, grid: tuple[int, int]) -> Any:
    """*values* (``H x W`` float, 0-1) read on the plane of the quad *corners* (its pixels), as a *grid*-sized array."""
    import numpy as np
    from PIL import Image

    gw, gh = grid
    source = ((0, 0), (gw, 0), (gw, gh), (0, gh))
    rows, targets = [], []
    for (x, y), (u, v) in zip(source, [tuple(p) for p in corners], strict=True):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        targets.append(u)
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        targets.append(v)
    coefficients = [float(c) for c in np.linalg.solve(np.array(rows, dtype=float), np.array(targets, dtype=float))]
    image = Image.fromarray(np.ascontiguousarray(values, dtype=np.float32), "F")
    return np.asarray(image.transform((gw, gh), Image.Transform.PERSPECTIVE, coefficients, resample=Image.Resampling.BILINEAR))


def _from_plane(values: Any, corners: Any, shape: tuple[int, int]) -> Any:
    """*values* (a plane's ``gh x gw`` float array) laid back onto the quad *corners* of an array of *shape*."""
    import numpy as np
    from PIL import Image

    gh, gw = values.shape
    rows, targets = [], []
    for (x, y), (u, v) in zip([tuple(p) for p in corners], ((0, 0), (gw, 0), (gw, gh), (0, gh)), strict=True):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        targets.append(u)
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        targets.append(v)
    coefficients = [float(c) for c in np.linalg.solve(np.array(rows, dtype=float), np.array(targets, dtype=float))]
    image = Image.fromarray(np.ascontiguousarray(values, dtype=np.float32), "F")
    height, width = shape
    return np.asarray(
        image.transform((width, height), Image.Transform.PERSPECTIVE, coefficients, resample=Image.Resampling.BILINEAR, fillcolor=0.0)
    )


class MatteSmoother:
    """A matte read frame by frame, pooled over :data:`MATTE_WINDOW` frames on the surface's own plane.

    A key read against compressed footage drifts from one frame to the next,
    and a matte recomputed on every frame blinks at its edge. Each frame's
    matte is read on the plane of its quad (so a camera move does not smear
    it), the median of the last :data:`MATTE_WINDOW` frames is what is drawn,
    and :meth:`smooth` also says how much of that drawn matte holds from the
    frame before (:func:`edge_hold`): under :data:`MIN_EDGE_HOLD`, the key
    flickers.
    """

    def __init__(self, window: int = MATTE_WINDOW, side: int = MATTE_SIDE) -> None:
        from collections import deque

        self.window = max(1, int(window))
        self.side = side
        self.grid: tuple[int, int] | None = None
        self.kept: deque = deque(maxlen=self.window)
        #: The last two masks drawn (past a half): what the next one is held against.
        self.last: Any = None
        self.before: Any = None

    def smooth(self, matte: Any, corners: Any) -> tuple[Any, float | None]:
        """``(the matte as drawn, how much of it holds from the frame before)`` for *matte* over the quad *corners* (its pixels)."""
        import numpy as np

        from app.services.video_studio.pictures import quad_size

        if self.grid is None:
            quad_w, quad_h = quad_size(tuple(map(tuple, np.asarray(corners, dtype=float))))
            scale = min(1.0, self.side / max(quad_w, quad_h, 1.0))
            self.grid = (max(8, round(quad_w * scale)), max(8, round(quad_h * scale)))
        source = np.asarray(matte, dtype=np.float32)
        plane = _to_plane(source, corners, self.grid)
        self.kept.append(plane)
        pooled = np.median(np.stack(self.kept), axis=0) if len(self.kept) >= 3 else plane
        mask = pooled > 0.5
        held = edge_hold(mask, self.last, self.before)
        self.last, self.before = mask, self.last
        # Laid back onto the quad; where the plane has no say - outside the quad, and the half of its own border
        # pixels the bilinear fill of zero would fade - the matte stays as read.
        back = _from_plane(pooled, corners, source.shape[:2])
        inside = _from_plane(np.ones_like(pooled), corners, source.shape[:2])
        return np.clip(back + source * (1.0 - inside), 0.0, 1.0), held


def edge_hold(mask: Any, last: Any | None, before: Any | None) -> float | None:
    """How much of the matte *mask* (binary) holds from the frame before: one less the share of its pixels that toggled back.

    A pixel that changed between *before* and *last* and changed again into
    *mask* blinked; what a moving occluder changes is a new band of pixels
    on every frame, which never toggles back, so a person crossing the
    surface holds whole while a key read against compressed footage, flipping
    the same edge pixels in and out, does not. Over the union of the two
    masks compared. ``None`` until two frames came before.
    """
    if last is None or before is None or last.shape != mask.shape or before.shape != mask.shape:
        return None
    union = int((mask | last).sum())
    if not union:
        return 1.0
    toggled = int(((mask ^ last) & (last ^ before)).sum())
    return round(1.0 - toggled / union, 5)


def content_overflow(content: tuple[int, int, int, int], picture_size: tuple[int, int], corners: Any) -> float:
    """How far (pixels) the *content* box of a picture of *picture_size* drawn into *corners* stands past them.

    The content's corners taken through the same homography the picture is
    drawn with; 0 when every one lies inside the quad, else the farthest's
    distance outside it. A ``cover`` fit that cut the content, or a quad
    smaller than what it shows, puts it past the bezel.
    """
    import numpy as np

    from app.services.video_studio.pictures import perspective_coefficients

    points = np.asarray(corners, dtype=float)
    width, height = picture_size
    a, b, c, d, e, f, g, h = perspective_coefficients(tuple(map(tuple, points)), width, height)
    # The forward map (picture -> quad) is the inverse of Pillow's (quad -> picture) homography.
    inverse = np.linalg.inv(np.array([[a, b, c], [d, e, f], [g, h, 1.0]], dtype=float))
    x0, y0, x1, y1 = content
    farthest = 0.0
    centre = points.mean(axis=0)
    for u, v in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        x, y, w = inverse @ np.array([u, v, 1.0])
        point = np.array([x / w, y / w])
        for index in range(4):
            start, end = points[index], points[(index + 1) % 4]
            edge = end - start
            length = float(np.linalg.norm(edge))
            if length < 1e-6:
                continue
            normal = np.array([-edge[1], edge[0]]) / length
            if normal @ (start - centre) < 0:
                normal = -normal
            farthest = max(farthest, float(normal @ (point - start)))
    return round(farthest, 3)


@dataclass
class Measure:
    """What the frames of a replaced screen showed: the worst spill, overlap and overflow, and where."""

    frames: int = 0
    spill: float = 0.0
    spill_frame: int | None = None
    edge_hold: float | None = None
    hold_frame: int | None = None
    overflow: float = 0.0
    overflow_frame: int | None = None

    def frame(self, index: int, *, spill: float, hold: float | None, overflow: float) -> None:
        self.frames += 1
        if spill > self.spill or self.spill_frame is None:
            self.spill, self.spill_frame = spill, index
        if hold is not None and (self.edge_hold is None or hold < self.edge_hold):
            self.edge_hold, self.hold_frame = hold, index
        if overflow > self.overflow or self.overflow_frame is None:
            self.overflow, self.overflow_frame = overflow, index

    def judge(self, key: Key, fps: float, *, scale: float = 1.0) -> None:
        """Refuse the shot its frames failed: the key left around the screen, a matte that flickers, content cropped."""
        rate = max(float(fps or 24.0), 1.0)
        if self.spill > MAX_SPILL_SHARE:
            raise KeySpillError(
                f"key spill: {self.spill:.2%} of the band around the screen is still {key.name} at t={self.spill_frame / rate:.2f} s "
                f"(frame {self.spill_frame}; at most {MAX_SPILL_SHARE:.1%}) - the replaced screen does not reach the bezel. "
                "Generate the shot again with the screen's four corners in view, or give screen.quad on the bezel's "
                "corners in the start picture."
            )
        if self.edge_hold is not None and self.edge_hold < MIN_EDGE_HOLD:
            raise KeyFlickerError(
                f"key flicker: {1.0 - self.edge_hold:.1%} of the screen's matte toggles back from one frame to the next at "
                f"t={self.hold_frame / rate:.2f} s (frame {self.hold_frame}; at most {1.0 - MIN_EDGE_HOLD:.0%}) - the "
                f"{key.name} surface is not flat enough to key steadily. Generate the shot again with a calmer camera "
                "move and the screen evenly lit."
            )
        if self.overflow > CONTENT_OVERFLOW_PX * scale:
            raise ScreenContentCroppedError(
                f"screen content cropped: the screenshot's content stands {self.overflow:.0f} px past the screen's bezel "
                f"at t={self.overflow_frame / rate:.2f} s (frame {self.overflow_frame}) - fit: cover cut it. Generate the "
                "shot again with fit: contain (the default)."
            )

    def record(self) -> dict:
        """The measurements as the take keeps them (``params.screen.checks``), for the renders playing it."""
        return {
            "frames": self.frames,
            "spill": round(self.spill, 5),
            "edge_hold": self.edge_hold,
            "overflow": round(self.overflow, 2),
        }


# ── Compositing ───────────────────────────────────────────────────────────


def _rgb_from(luma: Any, cb: Any, cr: Any) -> Any:
    import numpy as np

    return np.stack([luma + 1.402 * cr, luma - 0.344136 * cb - 0.714136 * cr, luma + 1.772 * cb], axis=-1)


#: The light a surface's shading lays over what is drawn on it (:func:`lit`): how dark its shadows may go, how
#: far its highlights may lift toward white.
SHADE_FLOOR = 0.55
GLOW_CEILING = 0.6


def lit(drawn: Any, luma: Any, reference: float, light: float) -> Any:
    """*drawn* (``... x 3`` float RGB) under the light the model drew on the surface it sits on: the ONE lighting match.

    *luma* is the surface's luma over the same pixels and *reference* its
    level where it is lit flat (its median); the shading relative to it,
    smoothed, darkens *drawn* by *light* down to :data:`SHADE_FLOOR` and
    lifts it toward white by up to :data:`GLOW_CEILING`. The screen
    replacement (:func:`composite`) and a logo drawn onto its subject
    (``tracked_logos``) read it alike.
    """
    import numpy as np
    from scipy import ndimage

    shading = ndimage.gaussian_filter(np.asarray(luma, dtype=np.float32), 1.5) / max(float(reference), 1.0)
    gain = np.clip(1.0 + light * (shading - 1.0), SHADE_FLOOR, 1.0)[..., None]
    glow = np.clip(light * (shading - 1.0), 0.0, GLOW_CEILING)[..., None]
    shaded = drawn * gain
    return shaded + (255.0 - shaded) * glow


def composite(
    frame: Any,
    picture: Any,
    corners: Any,
    key: Key,
    light: float,
    *,
    bezel: Any | None = None,
    smoother: MatteSmoother | None = None,
) -> tuple[Any, float | None]:
    """*frame* (``H x W x 3`` uint8) with *picture* (RGBA, already fitted to the quad's shape) drawn into *corners*.

    The screenshot goes under what stands in front of the screen (the key's
    matte), the key's colour is taken out of what shows around it (despill),
    and the light the model drew on the flat surface - its shading relative to
    the surface's median, smoothed - darkens and brightens the screenshot by
    *light*. *bezel* (:func:`bezel_corners`) is the quad carried to the
    bezel, drawn into instead of *corners* grown a little; inside it, every
    pixel the chroma calls the key is the key whole (the darker key the model
    shades along the bezel included), feathered by :data:`BEZEL_FEATHER`; a
    *smoother* pools the matte over the frames before. Returns the frame and
    how much of the matte drawn holds from the frame before (``None`` without a
    smoother, or on its first frames).
    """
    import numpy as np
    from PIL import Image
    from scipy import ndimage

    from app.services.video_studio.pictures import perspective_coefficients

    height, width = frame.shape[:2]
    # To the bezel, or a little past the fitted edges: the matte keeps what is in front, no rim of the key is left.
    corners = np.asarray(bezel, dtype=float) if bezel is not None else _grown(np.asarray(corners, dtype=float), _EDGE_GROW)
    low, high = corners.min(axis=0), corners.max(axis=0)
    x0, y0 = max(0, math.floor(low[0]) - 2), max(0, math.floor(low[1]) - 2)
    x1, y1 = min(width, math.ceil(high[0]) + 3), min(height, math.ceil(high[1]) + 3)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return frame, None
    local = tuple((float(x - x0), float(y - y0)) for x, y in corners)
    coefficients = perspective_coefficients(local, picture.width, picture.height)
    warped = np.asarray(
        picture.transform((x1 - x0, y1 - y0), Image.Transform.PERSPECTIVE, coefficients, resample=Image.Resampling.BICUBIC),
        dtype=np.float32,
    )
    coverage = warped[..., 3] / 255.0
    region = frame[y0:y1, x0:x1].astype(np.float32)
    matte = key_matte(region, key)
    # The key by chroma distance, whole: the darker key along the bezel is the screen too, not a rim of it.
    scale = height / 1080.0
    keyed = ((key_distance(region, key) < KEY_OUT) & (coverage > 0)).astype(np.float32)
    # Feathered outward: the key's share melts into the bezel over a pixel or two, never a hard line.
    matte = np.maximum(matte, np.clip(ndimage.gaussian_filter(keyed, max(BEZEL_FEATHER * scale, 0.3)), 0.0, 1.0))
    hold = None
    if smoother is not None:
        matte, hold = smoother.smooth(matte, np.asarray(local, dtype=float))
    alpha = (coverage * matte)[..., None]

    to_ycc = np.asarray(_TO_YCC, dtype=np.float32).T
    luma = (region @ to_ycc)[..., 0]
    screen = warped[..., :3]
    key_rgb = np.asarray(key.rgb, dtype=np.float32)
    key_luma = float(key_rgb @ to_ycc[:, 0])
    surface = (coverage > 0.99) & (matte > 0.99)
    reference = float(np.median(luma[surface])) if surface.sum() > 32 else key_luma
    if light > 0 and surface.sum() > 32:
        screen = lit(screen, luma, reference, light)

    # What stands in front of the screen, or the bezel at its edge, unmixed from the key and despilled.
    generated = despill(region, alpha[..., 0], key, coverage > 0, reference)
    # On the flat surface itself, the light was laid over the screenshot already: nothing of the key is kept.
    generated = generated * (alpha < 0.98)

    out = frame.copy()
    out[y0:y1, x0:x1] = np.clip(generated + screen * alpha, 0, 255).astype(np.uint8)
    return out, hold


def content_fitted(picture: Any, quad_w: float, quad_h: float, fit: str) -> tuple[Any, tuple[int, int, int, int]]:
    """*picture* shaped to a screen of *quad_w* x *quad_h* as its *fit* says, and where its content lies in the result.

    ``contain`` (the default) cuts the picture to its content box
    (``pictures.content_box``: its flat margins trimmed; a flat picture, with
    no content, goes whole) and letterboxes it in its own edge colours
    (``pictures.letterboxed``); ``cover`` cuts the picture to the screen's
    shape as before; ``stretch`` keeps it as it is. The content box is
    returned in the shaped picture's pixels, for :func:`content_overflow`.
    """
    from app.services.video_studio.pictures import content_box, fitted, letterboxed

    source = picture.convert("RGBA")
    x0, y0, x1, y1 = content_box(source) or (0, 0, source.width, source.height)
    if fit == "contain":
        content = source.crop((x0, y0, x1, y1))
        shaped = letterboxed(content, quad_w, quad_h)
        left, top = (shaped.width - content.width) // 2, (shaped.height - content.height) // 2
        return shaped, (left, top, left + content.width, top + content.height)
    shaped = fitted(source, quad_w, quad_h, fit)
    if fit == "cover":
        # The crop is centred: the content box moves by what the crop took off the left and the top.
        dx, dy = (source.width - shaped.width) // 2, (source.height - shaped.height) // 2
        return shaped, (x0 - dx, y0 - dy, x1 - dx, y1 - dy)
    return shaped, (x0, y0, x1, y1)


@dataclass
class Replaced:
    """What the replacement did, for the shot's notes; *record* what the take keeps of it (``params.screen``)."""

    frames: int
    found: int
    notes: list[str] = field(default_factory=list)
    record: dict = field(default_factory=dict)


def judge(found: Sequence[bool], fps: float, key: Key) -> None:
    """Refuse a shot whose surface was lost: missing from its first frame, too often, or for too long."""
    total = len(found)
    if not total:
        raise ScreenLostError("screen lost: the shot has no frame to draw the screen into.")
    seen = sum(found)
    longest, run = 0, 0
    for hit in found:
        run = 0 if hit else run + 1
        longest = max(longest, run)
    allowed = max(3, round(MAX_LOST_SECONDS * (fps or 24)))
    if not found[0] or seen < MIN_FOUND_SHARE * total or longest > allowed:
        raise ScreenLostError(
            f"screen lost: the model did not keep the screen a flat {key.name} surface (found in {seen} of {total} "
            f"frames{', not in the first' if not found[0] else ''}). Generate it again with a calmer camera move, the "
            "screen fully in view, or a quad that matches the screen's corners in the start picture."
        )


async def replace(
    video_path: str,
    out_path: str,
    picture: Any,
    spec: ScreenSpec,
    *,
    start_size: tuple[int, int],
) -> Replaced:
    """Draw *picture* (the screenshot) into the flat surface of every frame of *video_path*, to *out_path*.

    *start_size* is the start picture's size, in whose pixels ``spec.quad`` is
    given: the quad is scaled to the video's size. Raises
    :class:`ScreenLostError` when the surface could not be followed, and:
    class:`KeySpillError`, :class:`KeyFlickerError` or
    :class:`ScreenContentCroppedError` when what was drawn fails its frames'
    measure (:class:`Measure`): the key left in a band :data:`SPILL_BAND` wide
    around the screen, a matte that flickers, the screenshot's content past
    the bezel. Each frame's corners are carried to the bezel
    (:func:`bezel_corners`), smoothed over time like the corners themselves.
    """
    import numpy as np

    from app.services.video_studio import encoder
    from app.services.video_studio.pictures import quad_size

    key = _key_of(spec)
    info = await encoder.probe(video_path)
    width, height = info.width, info.height
    sx, sy = width / max(start_size[0], 1), height / max(start_size[1], 1)
    tracker = Tracker(key, [(x * sx, y * sy) for x, y in spec.quad], (width, height))
    corners: list[Any] = []
    bezels: list[Any] = []
    found: list[bool] = []
    async for raw in encoder.iter_rgb_frames(video_path, width=width, height=height):
        frame = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
        where, hit = tracker.track(frame)
        corners.append(where.copy())
        found.append(hit)
        bezels.append(_bezel_of(frame, where, key) if hit else where.copy())
    judge(found, info.fps, key)
    track = smooth(corners, found)
    bezel_track = smooth(bezels, found)
    quad_w, quad_h = quad_size(tuple(map(tuple, bezel_track[0])))
    shaped, content = content_fitted(picture, quad_w, quad_h, spec.fit)
    smoother = MatteSmoother()
    measure = Measure()

    def draw(index: int, raw: bytes) -> bytes:
        frame = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
        at = min(index, len(track) - 1)
        out, hold = composite(frame, shaped, track[at], key, spec.light, bezel=bezel_track[at], smoother=smoother)
        measure.frame(
            index,
            spill=residual_key_share(out, bezel_track[at], key),
            hold=hold,
            overflow=content_overflow(content, shaped.size, bezel_track[at]),
        )
        return out.tobytes()

    await encoder.transform_frames(video_path, out_path, draw, width=width, height=height, fps=info.fps or 24.0)
    measure.judge(key, info.fps or 24.0, scale=height / 1080.0)
    seen = sum(found)
    where = " (its corners found in the start picture)" if spec.found else ""
    notes = [
        f"The screen{where} was filled with flat {key.name} for the model and the real screenshot drawn into it "
        f"in every frame (followed in {seen} of {len(found)}, carried to the bezel): its text is the screenshot's own."
    ]
    record = {
        "picture_size": [shaped.width, shaped.height],
        "content": [int(v) for v in content],
        "checks": measure.record(),
    }
    return Replaced(len(found), seen, notes, record)


def _bezel_of(frame: Any, corners: Any, key: Key) -> Any:
    """*corners* carried to the bezel in *frame* (:func:`bezel_corners`): the key by chroma read around the quad."""
    import numpy as np

    from app.services.video_studio.pictures import quad_size

    points = np.asarray(corners, dtype=float)
    quad_w, _quad_h = quad_size(tuple(map(tuple, points)))
    reach = BEZEL_REACH * quad_w
    height, width = frame.shape[:2]
    low, high = points.min(axis=0) - reach - 2, points.max(axis=0) + reach + 2
    x0, y0 = max(0, math.floor(low[0])), max(0, math.floor(low[1]))
    x1, y1 = min(width, math.ceil(high[0])), min(height, math.ceil(high[1]))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return points
    keyed = key_distance(np.asarray(frame[y0:y1, x0:x1, :3], dtype=np.float32), key) < KEY_OUT
    return bezel_corners(keyed, points - np.array([x0, y0], dtype=float), reach) + np.array([x0, y0], dtype=float)
