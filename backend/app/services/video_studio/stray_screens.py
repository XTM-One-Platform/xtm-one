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

"""Screens an AI shot drew against its negative prompt, cleaned instead of thrown away.

A video model asked for an office "without screens or readable text" draws
monitors showing an interface about half the time; the content check reads
the excluded term on the frames and fails the shot, although everything else
in it is right. A shot asked with ``stray_screens`` keeps such a take:

1. the frames reading (``content_drift.check(locate_screens=True)``) says, for
   each excluded term a frame shows, the screens it shows on - their four
   corners as shares of the frame - and whether it shows anywhere else. Only a
   shot whose every failing finding is on located screens is cleaned;
2. the shot's camera is measured from its first frame (``motion.align``, the
   screens left out of the measure); each screen located in a frame is taken
   to every frame by that motion from the located frame nearest in time, its
   edges refined on that frame (:func:`refine`: the strongest edge across each
   side, near where the side is expected) and its corners smoothed over time;
3. every frame is redrawn: each screen ``flatten``\\ ed - a screen switched
   off, keeping only the soft light the room cast on it - or drawn with a real
   screenshot (``{file_id, fit, light}``), lit by the light the model drew.

What cannot be done right is refused rather than drawn wrong
(:class:`CleanupError`, the shot then fails as before): a camera that cannot
be followed, a screen something passes in front of (its content changes
between frames, :func:`occluded`). The runner then reads the cleaned take
again: a term still shown on a cleaned screen is what the author chose (a
blank screen is still a screen), anything else still fails.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from typing import Any

from app.services.video_studio.motion import Similarity

#: The params key of a shot's cleanup: ``{"mode": "flatten" | "screenshot", file_id?, fit?, light?}``, and once
#: drawn ``cleaned_file_id`` (the cleaned take) and ``cleaned`` (each read frame's cleaned screens, shares).
KEY = "stray_screens"
FLATTEN, SCREENSHOT = "flatten", "screenshot"
FITS = ("cover", "contain", "stretch")
#: How much of the room's light a flattened screen keeps, and a screenshot is lit by, unless the call says.
FLAT_LIGHT = 0.2
SCREENSHOT_LIGHT = 0.6
#: A screen switched off: what a flattened one is, under its light.
SCREEN_OFF = (14.0, 15.0, 18.0)
#: The frames the reading looks at, in order (``content_drift.FRAMES``), and where each is in the shot.
READ_FRAMES = ("first_frame", "middle_frame", "last_frame")
#: A located screen is at least this share of the frame, its corners at most this far outside it.
MIN_SCREEN_SHARE = 0.002
CORNER_MARGIN = 0.25
#: Two sightings are the same screen when their quads, taken to the first frame, overlap this much.
SAME_SCREEN_IOU = 0.3
#: How far (a share of the screen's size) a refined side may lie from where it was expected.
REFINE_BAND = 0.08
#: The share of frames the camera must be followed in, and the longest run (seconds) it may be lost for.
MIN_FOUND_SHARE = 0.85
MAX_LOST_SECONDS = 0.5
#: A frame whose screen content differs from the screen's usual one by more than this (grey levels, mean) has
#: something in front of it; more than :data:`MAX_OCCLUDED_SHARE` of such frames refuses the screen.
OCCLUDED_CHANGE = 30.0
MAX_OCCLUDED_SHARE = 0.1
#: A frame is judged on what it shows of a screen: at least this share of it, and a screen shown so in
#: fewer than :data:`MIN_JUDGED_FRAMES` frames is refused, the camera carrying it out of the frame.
MIN_SHOWN_SHARE = 0.5
MIN_JUDGED_FRAMES = 3
#: Pixels a cleaned screen is drawn past its fitted edges: over the rim a blur leaves.
_GROW = 2.0


class CleanupError(RuntimeError):
    """A shot's stray screens that cannot be cleaned right; ``str()`` says why."""


@dataclass(frozen=True)
class Sighting:
    """A screen a read frame shows an excluded term on: the frame (:data:`READ_FRAMES`) and its corners (shares)."""

    frame: str
    corners: tuple[tuple[float, float], ...]


@dataclass
class Cleaned:
    """What the cleanup drew: how many screens, in how many frames, and each read frame's screens (shares)."""

    screens: int
    frames: int
    followed: int
    at_read_frames: dict[str, list[list[list[float]]]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


# ── The option ────────────────────────────────────────────────────────────


def parse_option(raw: Any) -> dict | None:
    """A call's ``stray_screens``: ``None`` for ``fail`` (the default), else what the params store (``ValueError``)."""
    from app.services.builtin_tools._shared import _file_handle_key

    if raw in (None, "", "fail"):
        return None
    if raw == FLATTEN:
        return {"mode": FLATTEN}
    if not isinstance(raw, dict) or not str(raw.get("file_id") or "").strip():
        raise ValueError(
            "stray_screens is fail (the default), flatten, or {file_id, fit?, light?}: a real screenshot drawn into "
            "each screen the shot shows against its negative prompt"
        )
    fit = str(raw.get("fit") or "cover")
    if fit not in FITS:
        raise ValueError(f"stray_screens.fit is one of {', '.join(FITS)}")
    try:
        light = float(raw.get("light", SCREENSHOT_LIGHT))
    except (TypeError, ValueError):
        raise ValueError("stray_screens.light is a number from 0 to 1") from None
    if not math.isfinite(light) or not 0.0 <= light <= 1.0:
        raise ValueError("stray_screens.light is a number from 0 to 1")
    return {"mode": SCREENSHOT, "file_id": _file_handle_key(str(raw["file_id"])), "fit": fit, "light": light}


def parse_corners(value: Any) -> tuple[tuple[float, float], ...] | None:
    """Four ``[x, y]`` corners in shares of a frame, a convex quad of some size; ``None`` when they are not."""
    from app.services.video_studio.screens import _convex, area

    if not isinstance(value, list | tuple) or len(value) != 4:
        return None
    points = []
    for point in value:
        if not isinstance(point, list | tuple) or len(point) != 2:
            return None
        try:
            x, y = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            return None
        if not (math.isfinite(x) and math.isfinite(y)) or not (
            -CORNER_MARGIN <= x <= 1 + CORNER_MARGIN and -CORNER_MARGIN <= y <= 1 + CORNER_MARGIN
        ):
            return None
        points.append((round(x, 4), round(y, 4)))
    corners = tuple(points)
    if not _convex(corners) or abs(area(corners)) < MIN_SCREEN_SHARE:
        return None
    # Clockwise on the screen (y down), top left first: what the drawing's homography expects.
    if area(corners) < 0:
        corners = (corners[0], corners[3], corners[2], corners[1])
    first = min(range(4), key=lambda i: corners[i][0] + corners[i][1])
    return corners[first:] + corners[:first]


def quad_iou(a: Any, b: Any, side: int = 256) -> float:
    """The intersection over union of two quads given in the same units, rasterised on their common box."""
    import numpy as np
    from PIL import Image, ImageDraw

    points = np.vstack([np.asarray(a, dtype=float), np.asarray(b, dtype=float)])
    low, high = points.min(axis=0), points.max(axis=0)
    span = float(max(high - low)) or 1.0
    scale = (side - 1) / span

    def mask(quad: Any) -> Any:
        canvas = Image.new("1", (side, side), 0)
        ImageDraw.Draw(canvas).polygon([tuple((np.asarray(p) - low) * scale) for p in quad], fill=1)
        return np.asarray(canvas, dtype=bool)

    ma, mb = mask(a), mask(b)
    union = float((ma | mb).sum())
    return float((ma & mb).sum()) / union if union else 0.0


# ── Following a screen ────────────────────────────────────────────────────


def _sides(corners: Any) -> list[tuple[Any, Any]]:
    import numpy as np

    c = np.asarray(corners, dtype=float)
    return [(c[i], c[(i + 1) % 4]) for i in range(4)]


def refine(grey: Any, corners: Any, band: float = REFINE_BAND) -> tuple[Any, bool]:
    """*corners* (``4 x 2`` pixels of *grey*) moved onto the screen's edges in *grey*, and whether all four were found.

    Each side keeps the pixels near it (within *band* of the screen's size)
    whose brightness changes across the side - an edge parallel to it - and
    is fitted to them robustly (``screens._edge_line``); the corners are where
    neighbouring sides meet. A side with too few such pixels keeps its line.
    """
    import numpy as np
    from scipy import ndimage

    from app.services.video_studio.screens import _convex, _edge_line, _intersect, area

    c = np.asarray(corners, dtype=float)
    size = math.sqrt(max(abs(area(c)), 1.0))
    reach = max(2.0, band * size)
    height, width = grey.shape
    low = np.maximum(np.floor(c.min(axis=0) - 2 * reach), 0).astype(int)
    high = np.minimum(np.ceil(c.max(axis=0) + 2 * reach), [width - 1, height - 1]).astype(int)
    if high[0] - low[0] < 8 or high[1] - low[1] < 8:
        return c, False
    patch = ndimage.gaussian_filter(np.asarray(grey, dtype=np.float32)[low[1] : high[1] + 1, low[0] : high[0] + 1], 0.8)
    gy, gx = np.gradient(patch)
    magnitude = np.hypot(gx, gy)
    ys, xs = np.nonzero(magnitude > max(4.0, float(np.percentile(magnitude, 80))))
    points = np.column_stack([xs + low[0], ys + low[1]]).astype(float)
    normals = np.column_stack([gx[ys, xs], gy[ys, xs]]) / np.maximum(magnitude[ys, xs], 1e-6)[:, None]
    lines, fitted = [], 0
    for a, b in _sides(c):
        direction = (b - a) / max(float(np.linalg.norm(b - a)), 1e-6)
        normal = np.array([-direction[1], direction[0]])
        across = np.abs(normals @ normal) > 0.85
        line = _edge_line(points[across], a, b, reach) if across.any() else None
        if line is not None:
            fitted += 1
            lines.append(line)
        else:
            lines.append((a, direction))
    found = []
    for i in range(4):
        before, after = lines[(i - 1) % 4], lines[i]
        point = _intersect(before[0], before[1], after[0], after[1])
        found.append(c[i] if point is None else point)
    refined = np.array(found, dtype=float)
    moved = float(np.max(np.linalg.norm(refined - c, axis=1)))
    if not _convex(refined) or moved > 2 * reach or not 0.6 <= abs(area(refined)) / max(abs(area(c)), 1e-6) <= 1.6:
        return c, False
    return refined, fitted == 4


def group(sightings: list[Sighting], to_first: dict[str, Similarity], size: tuple[int, int]) -> list[dict[str, Any]]:
    """The screens *sightings* show, each ``{read frame: corners in pixels}``: one sighting per frame per screen.

    *to_first* takes each read frame's pixels to the first frame's (the
    inverse of the camera's motion); sightings whose quads overlap there
    (:data:`SAME_SCREEN_IOU`) are one screen.
    """
    import numpy as np

    width, height = size
    screens: list[dict[str, Any]] = []
    anchors: list[Any] = []
    for sighting in sightings:
        pixels = np.array([(x * width, y * height) for x, y in sighting.corners], dtype=float)
        first = to_first[sighting.frame].apply(pixels)
        for index, anchor in enumerate(anchors):
            if sighting.frame not in screens[index] and quad_iou(anchor, first) >= SAME_SCREEN_IOU:
                screens[index][sighting.frame] = pixels
                break
        else:
            screens.append({sighting.frame: pixels})
            anchors.append(first)
    return screens


def occluded(patches: list[Any], shown: list[Any] | None = None) -> list[bool | None]:
    """Which frames' screen content (patches of one screen, same shape) differs from its usual one: something in front.

    *shown* (a mask per patch) is what of the screen each frame shows:
    a screen the camera carries out of the frame is compared on
    what is left of it, never on the black around it, and a frame showing
    less than :data:`MIN_SHOWN_SHARE` of it is not judged (``None``).
    """
    import warnings

    import numpy as np

    stack = np.asarray(patches, dtype=np.float32)
    inside = np.ones(stack.shape, dtype=bool) if shown is None else np.asarray(shown, dtype=bool)
    judged = inside.reshape(len(stack), -1).mean(axis=1) >= MIN_SHOWN_SHARE
    with warnings.catch_warnings():
        # A pixel no judged frame shows has no usual content: it is left out below.
        warnings.simplefilter("ignore", RuntimeWarning)
        usual = np.nanmedian(np.where(inside & judged[:, None, None], stack, np.nan), axis=0)
    answers: list[bool | None] = []
    for patch, mask, judge in zip(stack, inside, judged, strict=True):
        compared = mask & ~np.isnan(usual)
        answers.append(bool(np.abs(patch[compared] - usual[compared]).mean() > OCCLUDED_CHANGE) if judge and compared.any() else None)
    return answers


def _patch(grey: Any, corners: Any, shape: tuple[int, int] = (48, 32)) -> tuple[Any, Any]:
    """The screen's content in *grey* (``4 x 2`` corners, pixels) warped to a ``shape`` rectangle, and what of it *grey* shows."""
    import numpy as np
    from PIL import Image

    picture = Image.fromarray(np.asarray(grey, dtype=np.uint8), "L")
    # The inverse map: the patch's pixels to the frame's, read off the quad.
    quad = [float(v) for point in np.asarray(corners)[[0, 3, 2, 1]] for v in point]
    patch = picture.transform(shape, Image.Transform.QUAD, quad, resample=Image.Resampling.BILINEAR)
    inside = Image.new("L", picture.size, 255).transform(shape, Image.Transform.QUAD, quad, resample=Image.Resampling.NEAREST)
    return np.asarray(patch, dtype=np.float32), np.asarray(inside) == 255


def follow(
    greys: list[Any], read_at: dict[str, int], sightings: list[Sighting], fps: float
) -> tuple[list[list[Any]], int, dict[str, list[list[list[float]]]]]:
    """Every screen's corners in every frame (analysis pixels), how many frames the camera was followed in, and the read frames' screens.

    Raises :class:`CleanupError` when the camera cannot be followed, something passes in front of a screen, or a
    screen is shown in too few frames to tell (the camera carries it out of the frame).
    """
    import numpy as np
    from PIL import Image, ImageDraw
    from scipy.ndimage import uniform_filter1d

    from app.services.video_studio import motion

    height, width = greys[0].shape
    reference = np.asarray(greys[0], dtype=np.float32)
    weight = np.ones_like(reference)
    first_screens = [s for s in sightings if s.frame == READ_FRAMES[0]]
    if first_screens:
        # A screen's content may change on its own: the camera is read on the room.
        holes = Image.new("L", (width, height), 0)
        for s in first_screens:
            ImageDraw.Draw(holes).polygon([(x * width, y * height) for x, y in s.corners], fill=255)
        weight = 1.0 - np.asarray(holes, dtype=np.float32) / 255.0
    motions, found = [], []
    previous = Similarity()
    for grey in greys:
        fit = motion.align(reference, grey, init=previous, weight=weight)
        motions.append(fit.motion)
        found.append(fit.found)
        if fit.found:
            previous = fit.motion
    seen, total = sum(found), len(found)
    if not found[0] or seen < MIN_FOUND_SHARE * total or motion.longest_run(found) > max(3, round(MAX_LOST_SECONDS * fps)):
        raise CleanupError(
            f"the shot's camera could not be followed (found in {seen} of {total} frames): its stray screens were "
            "not cleaned"
        )
    track = motion.smooth(motions, found)
    to_first = {name: track[index].inverse() for name, index in read_at.items()}
    screens = group(sightings, to_first, (width, height))
    corners: list[list[Any]] = []
    for screen in screens:
        anchors = {read_at[name]: track[read_at[name]].inverse().apply(quad) for name, quad in screen.items()}
        per_frame = []
        for index, grey in enumerate(greys):
            nearest = min(anchors, key=lambda at, index=index: abs(at - index))
            expected = track[index].apply(anchors[nearest])
            per_frame.append(refine(grey, expected)[0])
        smoothed = np.asarray(per_frame, dtype=float)
        if len(smoothed) > 2:
            smoothed = uniform_filter1d(smoothed, size=min(motion.SMOOTH_WINDOW, len(smoothed)), axis=0, mode="nearest")
        patches, shown = zip(*(_patch(grey, quad) for grey, quad in zip(greys, smoothed, strict=True)), strict=True)
        judged = [answer for answer in occluded(list(patches), list(shown)) if answer is not None]
        if len(judged) < MIN_JUDGED_FRAMES:
            raise CleanupError(
                f"a stray screen leaves the frame (shown in {len(judged)} of {total} frames): what passes in front "
                "of it cannot be told, so it was not cleaned"
            )
        if sum(judged) > MAX_OCCLUDED_SHARE * len(judged):
            raise CleanupError(
                f"something passes in front of a stray screen ({sum(judged)} of {len(judged)} frames showing it): "
                "it was not cleaned rather than drawn over"
            )
        corners.append(list(smoothed))
    at_read = {
        name: [[[round(float(x) / width, 4), round(float(y) / height, 4)] for x, y in screen[index]] for screen in corners]
        for name, index in read_at.items()
    }
    return corners, seen, at_read


# ── Drawing ───────────────────────────────────────────────────────────────


def _polygon_alpha(size: tuple[int, int], corners: Any) -> Any:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).polygon([tuple(p) for p in corners], fill=255)
    return np.asarray(mask.filter(ImageFilter.GaussianBlur(0.7)), dtype=np.float32) / 255.0


def paint(frame: Any, corners: Any, option: dict, screenshot: Any = None) -> Any:
    """*frame* (``H x W x 3`` uint8) with the screen at *corners* (pixels) flattened or showing *screenshot* (fitted RGBA)."""
    import numpy as np
    from PIL import Image
    from scipy import ndimage

    from app.services.video_studio.pictures import perspective_coefficients
    from app.services.video_studio.screens import _grown

    height, width = frame.shape[:2]
    grown = _grown(np.asarray(corners, dtype=float), _GROW)
    low, high = grown.min(axis=0), grown.max(axis=0)
    x0, y0 = max(0, math.floor(low[0]) - 2), max(0, math.floor(low[1]) - 2)
    x1, y1 = min(width, math.ceil(high[0]) + 3), min(height, math.ceil(high[1]) + 3)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return frame
    local = grown - np.array([x0, y0], dtype=float)
    alpha = _polygon_alpha((x1 - x0, y1 - y0), local)[..., None]
    region = frame[y0:y1, x0:x1].astype(np.float32)
    # The room's light on the screen, without what the screen showed: a blur wider than any text on it.
    side = max(2.0, min(float(np.ptp(local[:, 0])), float(np.ptp(local[:, 1]))))
    light_map = ndimage.gaussian_filter(region, (0.12 * side, 0.12 * side, 0))
    if option.get("mode") == SCREENSHOT and screenshot is not None:
        light = float(option.get("light", SCREENSHOT_LIGHT))
        coefficients = perspective_coefficients(tuple(map(tuple, local)), screenshot.width, screenshot.height)
        warped = np.asarray(
            screenshot.transform((x1 - x0, y1 - y0), Image.Transform.PERSPECTIVE, coefficients, resample=Image.Resampling.BICUBIC),
            dtype=np.float32,
        )
        alpha = alpha * (warped[..., 3:4] / 255.0)
        luma = light_map @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
        inside = alpha[..., 0] > 0.99
        reference = float(np.median(luma[inside])) if inside.sum() > 16 else float(luma.mean())
        gain = np.clip(1.0 + light * (luma / max(reference, 1.0) - 1.0), 0.55, 1.0)[..., None]
        drawn = warped[..., :3] * gain
    else:
        light = float(option.get("light", FLAT_LIGHT))
        off = np.asarray(SCREEN_OFF, dtype=np.float32)
        drawn = off + light * np.clip(light_map - off, 0.0, None)
    out = frame.copy()
    out[y0:y1, x0:x1] = np.clip(region * (1.0 - alpha) + drawn * alpha, 0, 255).astype(np.uint8)
    return out


async def clean(video_path: str, out_path: str, sightings: list[Sighting], option: dict, screenshot: Any = None) -> Cleaned:
    """Clean every screen of *sightings* in every frame of *video_path* (see the module), to *out_path*.

    *screenshot* is the decoded picture (Pillow) a ``screenshot`` option draws.
    Raises :class:`CleanupError` when the screens cannot be cleaned right.
    """
    import numpy as np

    from app.services.video_studio import encoder, motion

    if not sightings:
        raise CleanupError("no screen was located in the frames: nothing was cleaned")
    info = await encoder.probe(video_path)
    width, height = info.width, info.height
    greys: list[Any] = []
    factor = 1.0
    async for raw in encoder.iter_rgb_frames(video_path, width=width, height=height):
        grey, factor = motion.grey(np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3))
        greys.append(grey.astype(np.uint8))
    if not greys:
        raise CleanupError("the shot has no frame to clean")
    last = len(greys) - 1
    read_at = {READ_FRAMES[0]: 0, READ_FRAMES[1]: min(last, round(last / 2)), READ_FRAMES[2]: last}
    corners, followed, at_read = await asyncio.to_thread(follow, greys, read_at, sightings, info.fps or 24.0)
    del greys
    full = [[np.asarray(quad, dtype=float) / factor for quad in screen] for screen in corners]
    fitted = None
    if option.get("mode") == SCREENSHOT and screenshot is not None:
        from app.services.video_studio.pictures import fitted as fit_picture
        from app.services.video_studio.pictures import quad_size

        fitted = fit_picture(screenshot.convert("RGBA"), *quad_size(tuple(map(tuple, full[0][0]))), option.get("fit", "cover"))

    def drawn(index: int, raw: bytes) -> bytes:
        frame = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
        for screen in full:
            frame = paint(frame, screen[min(index, len(screen) - 1)], option, fitted)
        return frame.tobytes()

    await encoder.transform_frames(video_path, out_path, drawn, width=width, height=height, fps=info.fps or 24.0)
    how = "flattened (switched off, the room's light kept)" if option.get("mode") != SCREENSHOT else "drawn with the screenshot"
    count = f"{len(full)} screen{'s' if len(full) > 1 else ''}"
    return Cleaned(
        len(full),
        last + 1,
        followed,
        at_read,
        [
            f"Stray screens: {count} the model drew against the negative prompt {how} in every frame, following "
            f"the shot's camera ({followed} of {last + 1} frames)."
        ],
    )


def accepted(sightings: list[Sighting], cleaned: dict[str, list]) -> bool:
    """Whether every one of *sightings* is a screen the cleanup drew (the same read frame, overlapping)."""
    for sighting in sightings:
        drawn = cleaned.get(sighting.frame) or []
        if not any(quad_iou(sighting.corners, quad) >= SAME_SCREEN_IOU for quad in drawn):
            return False
    return True
