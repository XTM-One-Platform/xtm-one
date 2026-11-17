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

"""Logo ingredients composited back onto an AI shot: the brand's own pixels, never the model's redraw.

A shot asked with a logo ingredient on a model that cannot take its picture
(``clips.ingredient_path``) used to have the logo named in its prompt only,
and the model redraws it, garbled. Its logo is composited back instead
(``ingredients_path: composited``):

1. **Where.** The logo is found in the start frame the shot is asked from:
   at its ``quad`` when the ingredient gives one (0-1 across and down the
   start frame), else by normalised cross-correlation at :data:`FIND_SCALES`
   sizes then refined (:func:`locate`): of its colours on its own opaque
   pixels (whatever it is printed on is left out), at least
   :data:`MIN_MATCH` - a logo of one flat colour, which has no colours to
   correlate, by its shape drawn over a neutral grey, at least
   :data:`MIN_SHAPE_MATCH`. A place under that is no logo: a logo drawn in
   the wrong place is worse than one only named.
2. **Followed.** A ``tracking.PlanarTracker`` follows the surface the logo is
   on - the logo and a margin of what it is printed on - from the start
   frame through every frame of the take. That margin can hold two planes -
   a mug and the table behind it - so each frame proposes up to
   :data:`TRACK_PLANES` candidate quads. A candidate's confidence is the
   share of the points on the logo itself that agree with it
   (:func:`_confidence`: the model's redraw of the logo never decides it);
   of the candidates at or above :data:`MIN_CONFIDENCE`, the one whose
   corners moved least from the quad drawn in the frame before is the one
   followed and drawn (:func:`_choose`). One place a frame, never every
   candidate. On a curved surface (a mug) what the chosen homography leaves
   of the points followed inside the chosen quad is the surface's bend,
   interpolated over a mesh of the logo (:func:`_local_warp`); a mesh
   whose cells fold over each other is dropped for the flat quad.
3. **Turning away and resting.** How far the surface turned from
   the view axis is read off the quad's foreshortening (:func:`_turned`).
   Past :data:`AWAY_DEGREES` the logo fades out over :data:`FADE_FRAMES`
   frames instead of being squeezed into a smear, and a surface the track
   loses once it turned past :data:`TURNING_DEGREES` has turned away too.
   Turned away, it is looked for again from its pixels in the start frame
   (:func:`_search`), and fades back in where it faces the camera again. An
   object that comes to rest keeps the quad of its first stable frame
   (:func:`_settled`): no shimmer of the track on a mug standing still.
4. **Drawn.** The logo's own pixels are warped onto that place in every
   frame over what the model drew: one quad (``screen_insert.warp``:
   premultiplied edges, no fringe), or cell by cell on its mesh
   (:func:`_mesh_layer`) - never a flat quad on a curved surface.
5. **Found again.** The take, encoded, is read back: in every frame
   the pixels at the logo's place keep at least :data:`MIN_SSIM` of the
   asset as it was drawn there (:func:`_verify`).
6. **Once.** Every :data:`DUPLICATE_SAMPLE_EVERY` frames of the take
   read back, the asset is template-matched over the whole frame at the size
   it was tracked at (:func:`find_duplicates`): more than one place matching
   it is a duplicated logo. It is recorded on the take with that frame
   (``frame_qc.LOGO_CHECK``) and fails the scene of every render playing it
   with a ``logo_duplicated`` finding (:func:`duplicate_findings`).

A logo facing the camera is located in every frame or the take fails as
"logo lost": a frame where the track does not find its surface (a
hand over it, the model cutting away, a blur), or whose pixels at its place
are no longer the asset, fails it with that frame
(:attr:`LogoLostError.frame_jpeg`), its raw take kept on the row like a lost
screen - never a take with the logo missing, bridged over or redrawn. A logo
not found in the start frame stays named in the prompt, with a note.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from dataclasses import dataclass, field
from typing import Any

#: The longest side of the start frame the logo is looked for in.
FIND_SIDE = 640
#: The logo's width searched, as a share of the start frame's: from, to, and in how many sizes.
FIND_MIN_SHARE, FIND_MAX_SHARE, FIND_SCALES = 0.03, 0.7, 22
#: The sizes tried around the best one, each this much apart, to refine it.
_REFINE_STEPS, _REFINE_SPREAD = 9, 0.08
#: A place whose colours correlate with the logo's less than this is not the logo (a random textured frame
#: reaches about 0.75, the logo itself about 0.97) ...
MIN_MATCH = 0.85
#: ... and a size where the logo has fewer opaque pixels than this says too little to be told apart.
FIND_MIN_PIXELS = 200
#: A logo of one flat colour is looked for by its shape over a neutral grey, which a match must reach.
MIN_SHAPE_MATCH = 0.75
#: Opaque pixels spread less than this (grey levels) around their mean colour: one flat colour.
_FLAT_SPREAD = 6.0
#: The grey a flat logo is drawn over to be looked for: its shape shows against a light or a dark surface alike.
_NEUTRAL = 128.0
#: The track works on frames where the logo's longer side is at least this many pixels (at most the take's own size).
TRACK_LOGO_SIDE = 120
TRACK_MAX_SIDE = 1280
#: Around the logo, how much of what it is printed on is followed with it (a share of its size).
REGION_GROW = 0.6
#: Fewer points than this agreeing on a frame's homography: the logo is not found in that frame, and it is lost.
MIN_INLIERS = 8
#: How far (working pixels) a point of the logo's surface may stand from the plane and still be on it: what the
#: plane leaves of it is the surface's bend, drawn with the logo (:func:`_local_warp`).
LOCAL_INLIER_PIXELS = 3.0
#: The mesh a logo on a curved surface is drawn on: this many cells across and down.
MESH_CELLS = 8
#: Residuals under this (working pixels) everywhere on the mesh: the surface is flat, the logo one quad.
FLAT_RESIDUAL = 0.75
#: Read back, the pixels at a logo's place keep at least this structural similarity with it as drawn, in every frame.
MIN_SSIM = 0.7
#: The longest side of a logo's place compared (grey, box-averaged): its shape, not the encoder's noise.
VERIFY_SIDE = 96
#: The candidate quads a frame proposes: the logo's surface, and what else its margin holds.
TRACK_PLANES = 3
#: At least this share of the points followed on the logo (grown by :data:`LOGO_REACH`) agree with a candidate, or it is
#: not the logo's surface; candidates this close to the most confident one are told apart by their continuity.
MIN_CONFIDENCE = 0.5
CONFIDENCE_TIE = 0.05
LOGO_REACH = 0.15
#: A candidate quad stays convex, its area within this factor of the logo's in the start frame either way.
MAX_AREA_CHANGE = 4.0
#: A mesh cell with less than this share of a cell's mean area, or turned the other way, folds: the mesh is dropped.
MIN_CELL_SHARE = 0.2
#: Past this angle from the view axis the logo's surface faces away: it fades out over this many frames ...
AWAY_DEGREES = 80.0
FADE_FRAMES = 3
#: ... and so does one turned past this angle that the track loses, or that keeps under this share of the points the
#: tracker found on the logo itself in the frame it started from: the rest of the surface rolled into view, the edge
#: of the logo past :data:`AWAY_DEGREES`. With fewer points than :data:`MIN_LOGO_POINTS` on it, every point counts.
TURNING_DEGREES = 55.0
LOGO_POINTS_LEFT = 0.5
MIN_LOGO_POINTS = 6
#: Corners staying within this many working pixels of a frame's for :data:`REST_FRAMES` frames: the object rests,
#: its quad kept from that first stable frame until a corner moves :data:`UNLOCK_PIXELS` from it.
REST_PIXELS = 0.75
REST_FRAMES = 6
UNLOCK_PIXELS = 1.25
#: Turned away, the logo is looked for again every this many frames, over the whole frame at this share of its size.
SEARCH_EVERY = 3
SEARCH_SHRINK = 0.5
#: The sizes and turns (degrees) it is looked for at, around the last place it was drawn; turned away, at every turn:
#: an object that fell lies turned in the frame.
SEARCH_SCALES = (0.8, 1.0, 1.25)
SEARCH_TURNS = (-20.0, 0.0, 20.0)
SEARCH_TURNS_WHOLE = tuple(float(turn) for turn in range(-150, 181, 30))
#: The duplicate check: every this many frames of the composited take, the asset matched at its tracked size,
#: that size brought to at most this many pixels on its longer side.
DUPLICATE_SAMPLE_EVERY = 3
DUPLICATE_LOGO_SIDE = 64
#: The most places a frame is searched for before the check stops counting.
DUPLICATE_PEAKS = 4


class LogoLostError(RuntimeError):
    """The take does not hold a logo in some frame; ``str()`` is the take's failure, *frame_jpeg* that frame.

    *label* names the logo and *why* says what the frame showed of it (the tracked path says them its own way).
    """

    def __init__(
        self,
        message: str,
        *,
        frame: int = 0,
        seconds: float = 0.0,
        frame_jpeg: bytes | None = None,
        label: str = "the logo",
        why: str = "",
    ) -> None:
        super().__init__(message)
        self.frame = frame
        self.seconds = seconds
        self.frame_jpeg = frame_jpeg
        self.label = label
        self.why = why


@dataclass(frozen=True)
class Logo:
    """A logo ingredient as it is composited: its picture (RGBA), where it is in the start frame if given, its name."""

    picture: Any
    label: str
    #: Its corners, 0-1 across and down the start frame (top left, top right, bottom right, bottom left); ``None``: found.
    quad: tuple[tuple[float, float], ...] | None = None


@dataclass(frozen=True)
class Duplicate:
    """A frame of a composited take showing a logo in more than one place (:func:`find_duplicates`)."""

    label: str
    frame: int
    seconds: float
    #: The places matching it, 0-1 boxes (``x0, y0, x1, y1``), best first.
    boxes: tuple[tuple[float, float, float, float], ...]
    frame_jpeg: bytes | None = None

    @property
    def count(self) -> int:
        return len(self.boxes)

    def reason(self) -> str:
        """``logo duplicated at t=...``: what the take's check records and every render playing it says."""
        return (
            f"logo duplicated at t={self.seconds:.2f} s (frame {self.frame}) of the shot: {self.label} shows "
            f"{self.count} times in that frame - generate the shot again in place (clip_id)."
        )


@dataclass
class Composited:
    """What compositing did, for the shot's notes; *check* is the duplicate check's record (``frame_qc.LOGO_CHECK``)."""

    frames: int
    drawn: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    check: dict | None = None
    duplicate: Duplicate | None = None
    #: The least integrated logo drawn (``logo_integration.Integration``): the frame a failure names is its own.
    integration: Any | None = None


def _grey_over_neutral(picture: Any, size: tuple[int, int]) -> Any:
    import numpy as np
    from PIL import Image

    rgba = picture.convert("RGBA").resize(size, Image.Resampling.LANCZOS)
    pixels = np.asarray(rgba, dtype=np.float64)
    alpha = pixels[..., 3:4] / 255.0
    grey = (pixels[..., :3] * alpha + _NEUTRAL * (1 - alpha)) @ np.array([0.299, 0.587, 0.114])
    return grey


def _correlation(image: Any, template: Any) -> Any:
    """The normalised cross-correlation of *template* at every place it fits in *image* (both grey)."""
    import numpy as np
    from scipy.signal import fftconvolve

    height, width = template.shape
    count = height * width
    centred = template - template.mean()
    spread = float(np.sqrt((centred * centred).sum()))
    if spread < 1e-6:
        return None
    ones = np.ones_like(template)
    sums = fftconvolve(image, ones, mode="valid")
    squares = fftconvolve(image * image, ones, mode="valid")
    products = fftconvolve(image, centred[::-1, ::-1], mode="valid")
    variance = np.maximum(squares - sums * sums / count, 1e-6)
    return products / (np.sqrt(variance) * spread)


def _colour_correlation(image: Any, logo: Any, size: tuple[int, int]) -> Any:
    """The normalised cross-correlation of the logo's colours at *size*, on its opaque pixels only, over *image* (RGB).

    ``None`` when the logo has fewer than :data:`FIND_MIN_PIXELS` opaque pixels at that size.
    """
    import numpy as np
    from PIL import Image
    from scipy.signal import fftconvolve

    rgba = np.asarray(logo.convert("RGBA").resize(size, Image.Resampling.LANCZOS), dtype=np.float64)
    mask = (rgba[..., 3] > 127).astype(np.float64)
    opaque = float(mask.sum())
    if opaque < FIND_MIN_PIXELS:
        return None
    count = 3 * opaque
    centred = (rgba[..., :3] - (rgba[..., :3] * mask[..., None]).sum() / count) * mask[..., None]
    spread = float(np.sqrt((centred * centred).sum()))
    if spread < 1e-6:
        return None
    flipped = mask[::-1, ::-1]
    sums = fftconvolve(image.sum(axis=2), flipped, mode="valid")
    squares = fftconvolve((image * image).sum(axis=2), flipped, mode="valid")
    products = sum(fftconvolve(image[..., c], centred[::-1, ::-1, c], mode="valid") for c in range(3))
    variance = np.maximum(squares - sums * sums / count, 1e-6)
    return products / (np.sqrt(variance) * spread)


def _flat(logo: Any) -> bool:
    """Whether *logo*'s opaque pixels are one colour: nothing for its colours to be told apart by."""
    import numpy as np

    rgba = np.asarray(logo.convert("RGBA"), dtype=np.float64)
    opaque = rgba[..., 3] > 127
    if not opaque.any():
        return True
    return float(rgba[..., :3][opaque].std(axis=0).max()) < _FLAT_SPREAD


def locate(logo: Any, picture: Any) -> tuple[tuple[tuple[float, float], ...], float] | None:
    """Where *logo* (RGBA) shows in *picture*: its quad 0-1 across and down the picture and the match; ``None`` when absent."""
    import numpy as np
    from PIL import Image

    sample = picture.convert("RGB")
    scale = min(1.0, FIND_SIDE / max(sample.size))
    if scale < 1.0:
        sample = sample.resize((max(1, round(sample.width * scale)), max(1, round(sample.height * scale))), Image.Resampling.BOX)
    pixels = np.asarray(sample, dtype=np.float64)
    grey = pixels @ np.array([0.299, 0.587, 0.114])
    height, width = grey.shape
    aspect = logo.height / max(logo.width, 1)
    flat = _flat(logo)
    best: tuple[float, float, int, int] | None = None

    def tried(logo_width: float) -> None:
        nonlocal best
        w = round(logo_width)
        h = round(w * aspect)
        if w < 8 or h < 8 or w >= width or h >= height:
            return
        if flat:
            scores = _correlation(grey, _grey_over_neutral(logo, (w, h)))
        else:
            scores = _colour_correlation(pixels, logo, (w, h))
        if scores is None:
            return
        index = int(np.argmax(scores))
        score = float(scores.flat[index])
        if best is None or score > best[0]:
            y, x = divmod(index, scores.shape[1])
            best = (score, float(w), x, y)

    for share in np.geomspace(FIND_MIN_SHARE, FIND_MAX_SHARE, FIND_SCALES):
        tried(share * width)
    if best is None:
        return None
    for factor in np.linspace(1 - _REFINE_SPREAD, 1 + _REFINE_SPREAD, _REFINE_STEPS):
        tried(best[1] * factor)
    score, w, x, y = best
    if score < (MIN_SHAPE_MATCH if flat else MIN_MATCH):
        return None
    h = round(w * aspect)
    x0, y0, x1, y1 = x / width, y / height, (x + w) / width, (y + h) / height
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1)), round(score, 3)


def _working_size(size: tuple[int, int], quads: list[Any]) -> tuple[int, int]:
    """The size the take is tracked at: each logo's longer side about :data:`TRACK_LOGO_SIDE`, within the take's own."""
    width, height = size
    longest = min(
        max(math.dist(q[0], q[1]) * width, math.dist(q[0], q[3]) * height, 1.0) for q in quads
    )
    scale = min(1.0, max(TRACK_LOGO_SIDE / longest, 0.25), TRACK_MAX_SIDE / max(width, height))
    return max(16, round(width * scale / 2) * 2), max(16, round(height * scale / 2) * 2)


def _tracker(grey: Any, quad_px: Any) -> Any:
    from app.services.video_studio.tracking import PlanarTracker, Region, grown

    return PlanarTracker(
        grey,
        Region(outer=grown(quad_px, REGION_GROW)),
        max_points=200,
        min_distance=4,
        min_inliers=MIN_INLIERS,
        threshold=LOCAL_INLIER_PIXELS,
    )


def _pixels(quad: Any, size: tuple[int, int]) -> Any:
    import numpy as np

    return np.asarray(quad, dtype=float) * np.array(size, dtype=float)


def _frame_jpeg(rgb: Any, *outlines: Any | None) -> bytes:
    """*rgb* (a frame of the take) as a JPEG, each of *outlines* (a quad, pixels) drawn on it: the frame a failure shows."""
    import io

    import numpy as np
    from PIL import Image, ImageDraw

    picture = Image.fromarray(np.ascontiguousarray(rgb), "RGB")
    for outline in outlines:
        if outline is None:
            continue
        corners = [tuple(map(float, p)) for p in outline]
        ImageDraw.Draw(picture).line([*corners, corners[0]], fill=(255, 40, 40), width=3)
    out = io.BytesIO()
    picture.save(out, format="JPEG", quality=88)
    return out.getvalue()


def _lost(label: str, frame: int, fps: float, why: str, shown: bytes | None = None) -> LogoLostError:
    return LogoLostError(
        f"logo lost: {label} at t={frame / fps:.2f} s (frame {frame}) of the shot: {why} - generate it again in "
        "place (clip_id) with a calmer camera that keeps the logo in view, or with a model that takes reference images.",
        frame=frame,
        seconds=frame / fps,
        frame_jpeg=shown,
        label=label,
        why=why,
    )


@dataclass(frozen=True)
class Placement:
    """Where a logo is drawn in one frame: its quad (0-1), and on a curved surface the mesh of its local warp (0-1)."""

    quad: Any
    mesh: Any | None = None
    #: How much of it is drawn: under 1 while it fades out as its surface turns away, or back in.
    opacity: float = 1.0


def _mesh_vertices(corners: Any, cells: int) -> Any:
    """The ``(cells + 1) x (cells + 1)`` vertices of *corners* (a quad) by bilinear interpolation, rows from the top."""
    import numpy as np

    tl, tr, br, bl = (np.asarray(p, dtype=float) for p in corners)
    steps = np.linspace(0.0, 1.0, cells + 1)
    rows = []
    for v in steps:
        left, right = tl + (bl - tl) * v, tr + (br - tr) * v
        rows.append([left + (right - left) * u for u in steps])
    return np.asarray(rows, dtype=float)


def _folds(mesh: Any) -> bool:
    """Whether a mesh (``rows x columns x 2`` vertices) folds: a cell turned the other way, or squeezed to almost nothing.

    A folded mesh draws pieces of the logo over each other - the same mark
    several times on the take - so it is never drawn.
    """
    import numpy as np

    cells = mesh.shape[0] - 1
    areas = np.array(
        [
            _signed_area([mesh[row, column], mesh[row, column + 1], mesh[row + 1, column + 1], mesh[row + 1, column]])
            for row in range(cells)
            for column in range(cells)
        ]
    )
    whole = _signed_area([mesh[0, 0], mesh[0, -1], mesh[-1, -1], mesh[-1, 0]])
    if abs(whole) < 1e-9:
        return True
    same_way = np.sign(areas) == np.sign(whole)
    return not bool(same_way.all()) or float(np.abs(areas).min()) < MIN_CELL_SHARE * abs(whole) / len(areas)


def _local_warp(tracker: Any, homography: Any, origin: Any, size: tuple[int, int]) -> Any | None:
    """The mesh (0-1) a logo is drawn on when its surface curves; ``None`` when one homography holds it.

    What the homography leaves of the points followed on the logo's surface
    (each point's place minus where the plane puts it) is interpolated over
    the logo's mesh by a smoothed thin-plate spline: the surface's own bend -
    a mug's side - carried into every cell. *tracker* holds the points of the
    candidate chosen in that frame alone (``PlanarTracker.adopt``): a
    point of another candidate never bends the logo. A residual under
    :data:`FLAT_RESIDUAL` anywhere is a flat surface, and a mesh that folds
    (:func:`_folds`) is no surface at all: one quad draws either.
    """
    import numpy as np
    from scipy.interpolate import RBFInterpolator

    from app.services.video_studio.tracking import project

    reference, current = np.asarray(tracker.reference_points, dtype=float), np.asarray(tracker.points, dtype=float)
    if len(reference) < 12:
        return None
    residual = current - project(homography, reference)
    vertices = _mesh_vertices(origin, MESH_CELLS).reshape(-1, 2)
    try:
        field_ = RBFInterpolator(reference, residual, kernel="thin_plate_spline", smoothing=float(len(reference)), degree=1)
        shift = field_(vertices)
    except (np.linalg.LinAlgError, ValueError):
        return None
    if float(np.abs(shift).max(initial=0.0)) < FLAT_RESIDUAL:
        return None
    moved = ((project(homography, vertices) + shift) / np.array(size, dtype=float)).reshape(MESH_CELLS + 1, MESH_CELLS + 1, 2)
    return None if _folds(moved) else moved


def _turned(quad: Any, frontal: float) -> float:
    """How far (degrees) the surface of *quad* turned from the view axis: its foreshortening against *frontal*.

    *frontal* is the logo's width over its height facing the camera (its quad
    in the start frame); a surface turned by an angle shows one of its sides
    shortened by its cosine, whichever way it turned.
    """
    from app.services.video_studio.pictures import quad_size

    width, height = quad_size(tuple((float(x), float(y)) for x, y in quad))
    if width < 1e-6 or height < 1e-6 or frontal <= 0:
        return 90.0
    ratio = (width / height) / frontal
    return math.degrees(math.acos(max(0.0, min(1.0, min(ratio, 1.0 / ratio)))))


def _toward(opacity: float, target: float) -> float:
    """*opacity* one fade step (1 / :data:`FADE_FRAMES`) closer to *target* (0 or 1), in whole steps."""
    level, goal = round(opacity * FADE_FRAMES), round(target * FADE_FRAMES)
    return (level + (goal > level) - (goal < level)) / FADE_FRAMES


def _corner_shift(quad: Any, other: Any) -> float:
    """The mean distance between the corners of two quads (same units)."""
    import numpy as np

    return float(np.linalg.norm(np.asarray(quad, dtype=float) - np.asarray(other, dtype=float), axis=1).mean())


@dataclass
class _Following:
    """One logo followed through the take: what it is compared against, and where it was drawn."""

    logo: Logo
    tracker: Any
    #: Its corners in the frame the tracker started from (working pixels): the start frame, or where it was found again.
    origin: Any
    #: Its corners, width over height and area in the start frame: what a quad's turn and size are read against.
    start: Any
    frontal: float
    area: float
    #: Its pixels in the start frame (RGB), rectified: what it is looked for again by.
    colours: Any
    places: list[Placement | None] = field(default_factory=list)
    last: Any = None
    before: Any = None
    #: How many points the tracker found on the logo in the frame it started from.
    logo_points: int = 0
    angle: float = 0.0
    opacity: float = 1.0
    #: Turning away: fading out, then (*away*) drawn nowhere until it is found again facing the camera.
    turning: bool = False
    away: bool = False

    def start_tracker(self, grey: Any, quad: Any) -> None:
        """Follow the logo from *quad* (working pixels) on the frame *grey*: a tracker of its own, its points counted."""
        import numpy as np

        self.tracker, self.origin = _tracker(grey, quad), np.asarray(quad, dtype=float)
        self.logo_points = _on_logo(self.tracker, self.origin)


def _on_logo(tracker: Any, origin: Any) -> int:
    """How many points *tracker* found in the frame it started from it still follows on the logo (*origin*, grown)."""
    from app.services.video_studio.tracking import grown, inside

    if not len(tracker.reference_points):
        return 0
    return int((tracker.original & inside(tracker.reference_points, grown(origin, LOGO_REACH))).sum())


def _following(start_rgb: Any, reference: Any, quad: Any, logo: Logo) -> _Following:
    """A logo to follow from its *quad* (working pixels) on the start frame (*start_rgb*, its grey *reference*)."""
    import numpy as np

    from app.services.video_studio.pictures import quad_size

    width, height = quad_size(tuple((float(x), float(y)) for x, y in quad))
    side = max(width, height, 1.0)
    shrink = min(1.0, 256.0 / side)
    size = (max(8, round(width * shrink)), max(8, round(height * shrink)))
    one = _Following(
        logo=logo,
        tracker=None,
        origin=np.asarray(quad, dtype=float),
        start=np.asarray(quad, dtype=float),
        frontal=width / max(height, 1e-6),
        area=abs(_signed_area([tuple(p) for p in quad])),
        colours=_rectified(start_rgb, quad, size),
    )
    one.start_tracker(reference, quad)
    return one


def _rectified(pixels: Any, quad: Any, size: tuple[int, int]) -> Any:
    """The pixels inside *quad* (pixels) resampled onto a rectangle of *size* (``W, H``).

    *pixels* is a frame, ``H x W x 3`` (answered as uint8 RGB) or ``H x W``
    grey (answered as float grey levels); ``None`` for a quad in a line.
    """
    import numpy as np

    from app.services.video_studio.tracking import fit_homography, project, sample

    width, height = size
    rectangle = np.array([[0.0, 0.0], [width, 0.0], [width, height], [0.0, height]])
    homography = fit_homography(rectangle, np.asarray(quad, dtype=float))
    if homography is None:
        return None
    ys, xs = np.mgrid[0:height, 0:width]
    points = project(homography, np.column_stack([xs.ravel() + 0.5, ys.ravel() + 0.5]))
    if not np.isfinite(points).all():
        return None
    if pixels.ndim == 2:
        return sample(np.ascontiguousarray(pixels, dtype=np.float32), points[:, 0], points[:, 1]).reshape(height, width)
    channels = [
        sample(np.ascontiguousarray(pixels[..., channel], dtype=np.float32), points[:, 0], points[:, 1])
        for channel in range(3)
    ]
    return np.clip(np.stack(channels, axis=-1).reshape(height, width, 3), 0, 255).astype(np.uint8)


def _confidence(one: _Following, proposal: Any, plane: Any) -> float:
    """The share of the points followed on the logo (grown by :data:`LOGO_REACH`) that agree with *plane*.

    The points are read where they were in the frame the tracker started
    from: a static table behind a mug that moves holds none of them. With
    fewer than :data:`MIN_LOGO_POINTS` there, the share of every point followed.
    """
    from app.services.video_studio.tracking import grown, inside

    on_logo = inside(proposal.reference, grown(one.origin, LOGO_REACH)) if len(proposal.reference) else None
    if on_logo is not None and int(on_logo.sum()) >= MIN_LOGO_POINTS:
        return float(plane.inliers[on_logo].mean())
    return plane.count / max(proposal.followed, 1)


def _choose(options: list[tuple[int, Any, float]], last: Any | None) -> tuple[int, Any, float]:
    """The one candidate drawn of *options* ``(index, quad, confidence)``, all at or above the threshold.

    The most confident; of candidates within :data:`CONFIDENCE_TIE` of it, the
    one whose corners moved least from the quad drawn in the frame before
    (*last*): temporal continuity, never two places for one logo.
    """
    best = max(option[2] for option in options)
    near = [option for option in options if option[2] >= best - CONFIDENCE_TIE]
    if last is None:
        return max(near, key=lambda option: option[2])
    return min(near, key=lambda option: (_corner_shift(option[1], last), -option[2]))


def _search(rgb: Any, one: _Following, *, whole: bool) -> tuple[Any, float] | None:
    """*one*'s logo looked for again in *rgb* (a working frame) from its pixels in the start frame: its quad and match.

    Around the place it was drawn last at :data:`SEARCH_SCALES` of the size it
    was drawn at and :data:`SEARCH_TURNS` of the turn it was drawn at - or,
    with *whole* (turned away, it comes back facing the camera), over the
    whole frame at :data:`SEARCH_SHRINK` of it, of the size it had in the
    start frame and at :data:`SEARCH_TURNS_WHOLE`; a place matching it under
    :data:`MIN_MATCH` is not it.
    """
    import numpy as np
    from PIL import Image

    from app.services.video_studio.pictures import quad_size

    if one.colours is None:
        return None
    last = one.last if one.last is not None else one.origin
    width, height = quad_size(tuple((float(x), float(y)) for x, y in (one.start if whole else last)))
    turn = math.degrees(math.atan2(-(last[1][1] - last[0][1]), last[1][0] - last[0][0]))
    frame_h, frame_w = rgb.shape[:2]
    if whole:
        x0, y0, x1, y1 = 0, 0, frame_w, frame_h
    else:
        centre, reach = np.asarray(last, dtype=float).mean(axis=0), 1.5 * max(width, height)
        x0, y0 = max(0, int(centre[0] - reach)), max(0, int(centre[1] - reach))
        x1, y1 = min(frame_w, int(centre[0] + reach) + 1), min(frame_h, int(centre[1] + reach) + 1)
    shrink = SEARCH_SHRINK if whole else 1.0
    window = Image.fromarray(np.ascontiguousarray(rgb[y0:y1, x0:x1]), "RGB")
    if shrink < 1.0:
        window = window.resize((max(1, round(window.width * shrink)), max(1, round(window.height * shrink))), Image.Resampling.BOX)
    pixels = np.asarray(window, dtype=np.float64)
    source = Image.fromarray(one.colours, "RGB").convert("RGBA")
    best: tuple[float, int, int, tuple[int, int], tuple[float, float], float] | None = None
    for scale in SEARCH_SCALES:
        size = (round(width * scale * shrink), round(height * scale * shrink))
        if min(size) < 8:
            continue
        sized = source.resize(size, Image.Resampling.BILINEAR)
        for offset in SEARCH_TURNS_WHOLE if whole else SEARCH_TURNS:
            angle = turn + offset
            template = sized.rotate(angle, resample=Image.Resampling.BILINEAR, expand=True, fillcolor=(0, 0, 0, 0))
            if template.width >= pixels.shape[1] or template.height >= pixels.shape[0]:
                continue
            scores = _colour_correlation(pixels, template, template.size)
            if scores is None:
                continue
            index = int(np.argmax(scores))
            score = float(scores.flat[index])
            if best is None or score > best[0]:
                y, x = divmod(index, scores.shape[1])
                best = (score, x, y, template.size, (size[0] / shrink, size[1] / shrink), angle)
    if best is None or best[0] < MIN_MATCH:
        return None
    score, x, y, (template_w, template_h), (quad_w, quad_h), angle = best
    centre = np.array([(x + template_w / 2) / shrink + x0, (y + template_h / 2) / shrink + y0])
    a = math.radians(angle)
    rotation = np.array([[math.cos(a), math.sin(a)], [-math.sin(a), math.cos(a)]])
    half = np.array([[-quad_w / 2, -quad_h / 2], [quad_w / 2, -quad_h / 2], [quad_w / 2, quad_h / 2], [-quad_w / 2, quad_h / 2]])
    return centre + half @ rotation.T, round(score, 3)


def _follow(one: _Following, rgb: Any, grey: Any, index: int, size: tuple[int, int], fps: float) -> None:
    """*one* followed into frame *index* (*rgb*, its grey): one place drawn there, or none; or :class:`LogoLostError`.

    The frame's candidates (:data:`TRACK_PLANES`) are judged by their
    confidence (:func:`_confidence`) and one is chosen (:func:`_choose`); on
    the take's first frame - the model's drawing against the start picture -
    the one most points agree on. With none at or above
    :data:`MIN_CONFIDENCE`, the logo is looked for again (:func:`_search`; on
    the take's first frame, where the model may have reframed it, by
    :func:`locate`). A logo facing the camera that is found nowhere fails the
    take; one turning away fades out on where it was going.
    """
    import numpy as np
    from PIL import Image

    from app.services.video_studio.tracking import convex, project

    scale = np.array(size, dtype=float)
    chosen: tuple[Any, Any] | None = None
    why = "it is found nowhere in that frame"
    if one.tracker is not None:
        proposal = one.tracker.propose(grey, planes=TRACK_PLANES)
        options: list[tuple[int, Any, float]] = []
        judged: list[float] = []
        for n, plane in enumerate(proposal.planes):
            quad = project(plane.homography, one.origin)
            ratio = abs(_signed_area([tuple(p) for p in quad])) / max(one.area, 1e-6)
            if not convex(quad) or not 1 / MAX_AREA_CHANGE <= ratio <= MAX_AREA_CHANGE:
                continue
            confidence = (1.0 if n == 0 else 0.0) if index == 0 else _confidence(one, proposal, plane)
            judged.append(confidence)
            if confidence >= MIN_CONFIDENCE:
                options.append((n, quad, confidence))
        if options:
            n, quad, _confidence_of = _choose(options, one.last)
            one.tracker.adopt(proposal, n)
            chosen = (quad, proposal.planes[n].homography)
        elif judged:
            why = (
                f"no place its track proposes there is its surface (at most {max(judged):.0%} of the points on it "
                f"agree, under {MIN_CONFIDENCE:.0%})"
            )
        else:
            why = f"only {proposal.best} points of its surface agree there"
    if chosen is None and index == 0:
        # The model reframed its first frame: the logo is looked for there, and followed from it.
        found = locate(one.logo.picture, Image.fromarray(rgb))
        if found is not None:
            quad = _pixels(found[0], size)
            one.start_tracker(grey, quad)
            chosen = (quad, None)
    facing = not one.turning and one.angle < TURNING_DEGREES
    if chosen is None and ((one.away and index % SEARCH_EVERY == 0) or (not one.away and facing)):
        found_again = _search(rgb, one, whole=one.away)
        if found_again is not None:
            quad = found_again[0]
            one.start_tracker(grey, quad)
            one.away = False
            chosen = (quad, None)
    if chosen is not None:
        quad, homography = chosen
        one.angle = _turned(quad, one.frontal)
        if homography is not None and one.angle >= TURNING_DEGREES and one.logo_points >= MIN_LOGO_POINTS:
            # The logo's own points rolled out of view: what is followed now is the rest of its surface.
            one.turning |= _on_logo(one.tracker, one.origin) < LOGO_POINTS_LEFT * one.logo_points
        one.turning |= one.angle > AWAY_DEGREES
        one.opacity = _toward(one.opacity, 0.0 if one.turning else 1.0)
        mesh = None
        if one.opacity > 0 and homography is not None and not one.turning:
            mesh = _local_warp(one.tracker, homography, one.origin, size)
        one.places.append(Placement(quad / scale, mesh, one.opacity) if one.opacity > 0 else None)
        one.before, one.last = one.last, quad
        if one.opacity <= 0:
            one.away, one.turning, one.tracker = True, False, None
        return
    if one.away:
        one.places.append(None)
        one.tracker = None
        return
    if facing and one.opacity >= 1.0:
        last = one.last if one.last is not None else one.origin
        raise _lost(one.logo.label, index, fps, why, _frame_jpeg(rgb, last))
    # Turned away from the camera: faded out on where it was going, never drawn where the take does not show it.
    one.turning = True
    one.opacity = _toward(one.opacity, 0.0)
    quad = one.last if one.before is None else one.last + (one.last - one.before)
    one.places.append(Placement(quad / scale, None, one.opacity) if one.opacity > 0 else None)
    one.before, one.last = one.last, quad
    if one.opacity <= 0:
        one.away, one.turning, one.tracker = True, False, None


async def _track(
    video_path: str, start: Any, quads: list[Any], size: tuple[int, int], fps: float, logos: list[Logo]
) -> list[list[Placement | None]]:
    """Each logo's place in every frame of the take (``None``: not drawn there), from its quad in the start frame *start*.

    One place a frame, chosen among the candidates the track proposes
    (:func:`_follow`). Never bridged: a frame where a logo facing the camera
    is not found raises :class:`LogoLostError` with that frame, the last place
    found drawn on it - a logo is never drawn where the take does not show
    its surface; one that turned away fades out and is drawn nowhere.
    """
    import numpy as np
    from PIL import Image

    from app.services.video_studio import encoder
    from app.services.video_studio.tracking import luma

    width, height = size
    start_rgb = np.asarray(start.convert("RGB").resize(size, Image.Resampling.BOX))
    reference = luma(start_rgb)
    following = [
        await asyncio.to_thread(_following, start_rgb, reference, _pixels(quad, size), logo)
        for quad, logo in zip(quads, logos, strict=True)
    ]
    frames = encoder.iter_rgb_frames(video_path, width=width, height=height, resize=True)
    index = 0
    async with contextlib.aclosing(frames):
        async for raw in frames:
            rgb = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
            grey = luma(rgb)
            for one in following:
                await asyncio.to_thread(_follow, one, rgb, grey, index, size, fps)
            index += 1
    for one in following:
        if not one.places:
            raise _lost(one.logo.label, 0, fps, "the take has no frame")
    return [one.places for one in following]


def _settled(track: list[Placement | None], size: tuple[int, int]) -> tuple[list[Placement | None], int]:
    """*track* with the quad of an object at rest kept from its first stable frame, and how many frames were kept.

    A frame starts a rest when the quads of the :data:`REST_FRAMES` frames
    from it all stay within :data:`REST_PIXELS` (working pixels, *size*) of
    its own; its place (quad and mesh) is drawn from then on, each frame at
    its own opacity, until a quad moves :data:`UNLOCK_PIXELS` from it.
    """
    import dataclasses

    import numpy as np

    scale = np.array(size, dtype=float)
    out = list(track)
    locked: Placement | None = None
    kept = 0
    for index, place in enumerate(out):
        if place is None:
            locked = None
            continue
        if locked is not None:
            if _corner_shift(place.quad * scale, locked.quad * scale) <= UNLOCK_PIXELS:
                out[index] = dataclasses.replace(locked, opacity=place.opacity)
                kept += 1
                continue
            locked = None
        ahead = out[index : index + REST_FRAMES]
        if len(ahead) == REST_FRAMES and all(
            other is not None and _corner_shift(other.quad * scale, place.quad * scale) <= REST_PIXELS for other in ahead
        ):
            locked = place
    return out, kept


def _signed_area(corners: Any) -> float:
    points = list(corners)
    return 0.5 * sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:] + points[:1], strict=True))


def _mesh_layer(picture: Any, mesh: Any, size: tuple[int, int]) -> Any:
    """*picture* drawn cell by cell onto *mesh* (0-1 vertices) in a frame of *size*: a ``screen_insert.Warped``, or ``None``."""
    import numpy as np
    from PIL import Image, ImageDraw

    from app.services.video_studio import screen_insert
    from app.services.video_studio.pictures import perspective_coefficients

    width, height = size
    points = np.asarray(mesh, dtype=float) * np.array([width, height], dtype=float)
    x0, y0 = max(0, math.floor(points[..., 0].min())), max(0, math.floor(points[..., 1].min()))
    x1, y1 = min(width, math.ceil(points[..., 0].max())), min(height, math.ceil(points[..., 1].max()))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    source = picture.convert("RGBA").convert("RGBa")
    cells = points.shape[0] - 1
    cell_w, cell_h = source.width / cells, source.height / cells
    layer = Image.new("RGBa", (x1 - x0, y1 - y0), (0, 0, 0, 0))
    for row in range(cells):
        for column in range(cells):
            corners = [points[row, column], points[row, column + 1], points[row + 1, column + 1], points[row + 1, column]]
            low = np.floor(np.min(corners, axis=0)).astype(int)
            high = np.ceil(np.max(corners, axis=0)).astype(int) + 1
            bx0, by0 = max(int(low[0]), x0), max(int(low[1]), y0)
            bx1, by1 = min(int(high[0]), x1), min(int(high[1]), y1)
            if bx1 - bx0 < 1 or by1 - by0 < 1:
                continue
            local = tuple((float(x - bx0), float(y - by0)) for x, y in corners)
            if abs(_signed_area(local)) < 0.5:
                # A cell folded flat (its corners in a line) covers nothing to draw.
                continue
            coefficients = screen_insert.shift_source(perspective_coefficients(local, cell_w, cell_h), column * cell_w, row * cell_h)
            piece = source.transform(
                (bx1 - bx0, by1 - by0), Image.Transform.PERSPECTIVE, coefficients, resample=Image.Resampling.BILINEAR, fillcolor=(0, 0, 0, 0)
            )
            mask = Image.new("L", piece.size, 0)
            ImageDraw.Draw(mask).polygon(list(local), fill=255, outline=255)
            layer.paste(piece, (bx0 - x0, by0 - y0), mask)
    return screen_insert.Warped((x0, y0, x1, y1), layer.convert("RGBA"))


def _thumbnail(frame: Any, box: tuple[int, int, int, int], alpha: Any | None = None) -> tuple[Any, Any | None]:
    """The grey of *frame* in *box* at most :data:`VERIFY_SIDE` long (and *alpha* at the same size): what is compared."""
    import numpy as np
    from PIL import Image

    x0, y0, x1, y1 = box
    region = Image.fromarray(np.ascontiguousarray(frame[y0:y1, x0:x1]), "RGB").convert("L")
    scale = min(1.0, VERIFY_SIDE / max(region.size))
    size = (max(8, round(region.width * scale)), max(8, round(region.height * scale)))
    grey = np.asarray(region.resize(size, Image.Resampling.BOX), dtype=np.float64)
    if alpha is None:
        return grey, None
    weight = np.asarray(Image.fromarray(np.ascontiguousarray(alpha), "L").resize(size, Image.Resampling.BOX), dtype=np.float64)
    return grey, weight / 255.0


def masked_ssim(expected: Any, seen: Any, weight: Any) -> float:
    """The structural similarity of two grey arrays where *weight* (0-1) is, from -1 to 1: 1 where nothing is to compare."""
    import numpy as np
    from scipy.ndimage import gaussian_filter

    c1, c2 = (0.01 * 255.0) ** 2, (0.03 * 255.0) ** 2
    mu_a, mu_b = gaussian_filter(expected, 1.5), gaussian_filter(seen, 1.5)
    var_a = gaussian_filter(expected * expected, 1.5) - mu_a * mu_a
    var_b = gaussian_filter(seen * seen, 1.5) - mu_b * mu_b
    cov = gaussian_filter(expected * seen, 1.5) - mu_a * mu_b
    index = ((2 * mu_a * mu_b + c1) * (2 * cov + c2)) / ((mu_a**2 + mu_b**2 + c1) * (var_a + var_b + c2))
    total = float(np.asarray(weight).sum())
    return 1.0 if total < 1.0 else round(float((index * weight).sum() / total), 4)


async def _verify(
    out_path: str, expected: list[list[tuple[Any, Any, Any]]], logos: list[Logo], *, width: int, height: int, fps: float
) -> tuple[int, float]:
    """Every frame of the composited take, read back, holds each logo as drawn at its place; or :class:`LogoLostError`.

    *expected* is, per logo and frame, the box it was drawn in and the grey and
    alpha of that box as drawn, before the encode: the asset at its tracked
    place. A frame whose pixels there keep under :data:`MIN_SSIM` of it (a
    redraw over it, an encode that lost it) fails the take with that frame.
    Returns how many places were compared and the lowest similarity found:
    the logo check's ``similarity``.
    """
    import numpy as np

    from app.services.video_studio import encoder

    index = compared = 0
    lowest = 1.0
    frames = encoder.iter_rgb_frames(out_path, width=width, height=height)
    async with contextlib.aclosing(frames):
        async for raw in frames:
            frame = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
            for logo, kept in zip(logos, expected, strict=True):
                if index >= len(kept) or kept[index] is None:
                    continue
                box, grey, weight = kept[index]
                seen, _ = _thumbnail(frame, box)
                if seen.shape != grey.shape:
                    continue
                similarity = masked_ssim(grey, seen, weight)
                compared += 1
                lowest = min(lowest, similarity)
                if similarity < MIN_SSIM:
                    x0, y0, x1, y1 = box
                    shown = await asyncio.to_thread(_frame_jpeg, frame, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
                    raise _lost(
                        logo.label,
                        index,
                        fps,
                        f"what the take shows at its place keeps {similarity:.2f} of the asset (SSIM, under {MIN_SSIM:g})",
                        shown,
                    )
            index += 1
    return compared, round(lowest, 4)


async def composite(video_path: str, out_path: str, logos: list[Logo], start: Any, *, flat: bool = False) -> Composited:
    """*logos* drawn onto every frame of *video_path* to *out_path*, followed from the start frame *start* (Pillow).

    A logo not found in the start frame is left out with a note; when none is
    found, *out_path* is not written (``drawn`` empty). Raises
    :class:`LogoLostError` when the track loses one, or when the take read
    back does not hold one as drawn (:func:`_verify`). Each logo is drawn
    into its surface's light and relief and its integration measured
    (``logo_integration``); *flat* (the shot's
    ``ingredients_quality: "flat"``) records a logo not integrated without
    failing it.
    """
    import numpy as np

    from app.services.video_studio import encoder, logo_integration, screen_insert

    info = await encoder.probe(video_path)
    width, height = int(getattr(info, "width", 0) or 0), int(getattr(info, "height", 0) or 0)
    fps = float(getattr(info, "fps", None) or 24.0)
    placed: list[tuple[Logo, Any]] = []
    notes: list[str] = []
    for logo in logos:
        quad = logo.quad
        if quad is None:
            found = await asyncio.to_thread(locate, logo.picture, start)
            if found is None:
                notes.append(
                    f"{_capital(logo.label)} was not found in the start frame: it was named in the prompt only - give "
                    "its place (ingredients[].quad) to have it drawn."
                )
                continue
            quad = found[0]
        if (same := _drawn_already(logo, quad, placed)) is not None:
            notes.append(f"{_capital(logo.label)} is the picture {same} shows at the same place: drawn once.")
            continue
        placed.append((logo, quad))
    if not placed or width < 16 or height < 16:
        return Composited(0, [], notes)
    size = _working_size((width, height), [quad for _logo, quad in placed])
    followed = await _track(video_path, start, [quad for _logo, quad in placed], size, fps, [logo for logo, _q in placed])
    settled = [_settled(track, size) for track in followed]
    tracks = [track for track, _kept in settled]
    curved = [sum(1 for place in track if place is not None and place.mesh is not None) for track in tracks]
    expected: list[list[Any]] = [[] for _ in placed]
    probes = [logo_integration.Probe(logo.label, fps=fps, size=(width, height)) for logo, _quad in placed]
    pixels = np.array([width, height], dtype=float)

    def draw(index: int, raw: bytes) -> bytes:
        frame = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
        for n, ((logo, _quad), track) in enumerate(zip(placed, tracks, strict=True)):
            place = track[min(index, len(track) - 1)]
            if place is None or place.opacity <= 0:
                expected[n].append(None)
                continue
            warped = _mesh_layer(logo.picture, place.mesh, (width, height)) if place.mesh is not None else None
            if warped is None:
                corners = [tuple(map(float, p)) for p in place.quad]
                warped = screen_insert.warp(logo.picture, corners, (width, height), "stretch")
            if warped is None:
                expected[n].append(None)
                continue
            if place.opacity < 1.0:
                warped = _faded(warped, place.opacity)
            source, frame = frame, logo_integration.blend(frame, warped)
            if probes[n].wants(index) and place.opacity >= 1.0:
                vertices = (
                    np.asarray(place.mesh, dtype=float) * pixels
                    if place.mesh is not None
                    else logo_integration.quad_grid(np.asarray(place.quad, dtype=float) * pixels)
                )
                probes[n].add(index, source, frame, warped, vertices)
            alpha = np.asarray(warped.image)[..., 3]
            grey, weight = _thumbnail(frame, warped.box, alpha)
            expected[n].append((warped.box, grey, weight))
        return frame.tobytes()

    frames = await encoder.transform_frames(video_path, out_path, draw, width=width, height=height, fps=fps)
    compared, lowest = await _verify(out_path, expected, [logo for logo, _q in placed], width=width, height=height, fps=fps)
    duplicate, sampled = await find_duplicates(
        out_path, [logo for logo, _q in placed], tracks, width=width, height=height, fps=fps
    )
    measured = [await asyncio.to_thread(probe.measure) for probe in probes]
    integration = logo_integration.worst(measured)
    drawn = [logo.label for logo, _quad in placed]
    hidden = [sum(1 for place in track if place is None or place.opacity < 1.0) for track in tracks]
    notes.insert(
        0,
        f"{_capital(', '.join(drawn))} drawn with {'its' if len(drawn) == 1 else 'their'} own pixels onto every frame "
        f"{'it faces' if len(drawn) == 1 else 'they face'} the camera in, followed from the start frame (the model "
        "takes no reference image), under the light and relief of the surface it sits on, and found again as drawn "
        "in every frame of the take.",
    )
    for (logo, _quad), count, turned, (_track_of, kept) in zip(placed, curved, hidden, settled, strict=True):
        name = _capital(logo.label)
        if count:
            notes.append(
                f"{name} is on a curved surface: drawn with its local warp in {count} frame{'s' if count != 1 else ''}, "
                "not as a flat quad."
            )
        if turned:
            notes.append(
                f"{name} turns away from the camera: faded out over {FADE_FRAMES} frames past {AWAY_DEGREES:g} degrees "
                f"and not drawn while it faces away ({turned} frame{'s' if turned != 1 else ''})."
            )
        if kept:
            notes.append(f"{name} rests: kept where its first still frame shows it ({kept} frame{'s' if kept != 1 else ''}).")
    shown_frames = sum(1 for track in tracks for place in track if place is not None and place.opacity > 0)
    measures = {
        "presence": {"frames": frames, "drawn_frames": shown_frames, "logos": len(placed)},
        "similarity": {"min_ssim": lowest, "places_compared": compared, "threshold": MIN_SSIM},
        "integration": integration.record(waived=flat) if integration is not None else None,
    }
    check: dict[str, Any] = {"status": "passed", "frames_sampled": sampled, **measures}
    if duplicate is not None:
        check = {
            "status": "duplicated",
            "frames_sampled": sampled,
            "label": duplicate.label,
            "frame": duplicate.frame,
            "seconds": round(duplicate.seconds, 3),
            "count": duplicate.count,
            "boxes": [list(box) for box in duplicate.boxes],
            **measures,
        }
        notes.append(duplicate.reason()[:1].upper() + duplicate.reason()[1:])
    elif integration is not None and not integration.passed and not flat:
        check = {"status": logo_integration.NOT_INTEGRATED, "frames_sampled": sampled, **measures}
    if integration is not None:
        notes.append(logo_integration.note(integration, flat=flat))
    return Composited(frames, drawn, notes, check, duplicate, integration)


def _capital(text: str) -> str:
    return f"{text[:1].upper()}{text[1:]}"


def _drawn_already(logo: Logo, quad: Any, placed: list[tuple[Logo, Any]]) -> str | None:
    """The label of a logo of *placed* with *logo*'s very picture at the same place as *quad*; ``None`` when none.

    The same ingredient given twice is drawn once: two copies of one
    picture over each other are the logo twice where it slips, never more of it.
    """
    import numpy as np

    pixels = np.asarray(logo.picture.convert("RGBA"))
    box = np.asarray(quad, dtype=float)
    low, high = box.min(axis=0), box.max(axis=0)
    for other, other_quad in placed:
        if other.picture.size != logo.picture.size or not np.array_equal(np.asarray(other.picture.convert("RGBA")), pixels):
            continue
        corners = np.asarray(other_quad, dtype=float)
        other_low, other_high = corners.min(axis=0), corners.max(axis=0)
        overlap = np.clip(np.minimum(high, other_high) - np.maximum(low, other_low), 0.0, None).prod()
        union = (high - low).prod() + (other_high - other_low).prod() - overlap
        if union > 0 and overlap / union > 0.3:
            return other.label
    return None


def _faded(warped: Any, opacity: float) -> Any:
    """*warped* (``screen_insert.Warped``) drawn at *opacity*: its alpha scaled."""
    import numpy as np
    from PIL import Image

    from app.services.video_studio import screen_insert

    pixels = np.asarray(warped.image.convert("RGBA")).copy()
    pixels[..., 3] = np.round(pixels[..., 3].astype(np.float32) * float(opacity)).astype(np.uint8)
    return screen_insert.Warped(warped.box, Image.fromarray(pixels, "RGBA"))


def _peaks(scores: Any, threshold: float, reach: tuple[int, int]) -> list[tuple[int, int, float]]:
    """The places of *scores* at or above *threshold*, best first, each keeping *reach* (``x, y``) around it to itself."""
    import numpy as np

    left = np.array(scores, dtype=np.float64, copy=True)
    reach_x, reach_y = reach
    found: list[tuple[int, int, float]] = []
    while len(found) < DUPLICATE_PEAKS:
        index = int(np.argmax(left))
        value = float(left.flat[index])
        if not math.isfinite(value) or value < threshold:
            break
        y, x = divmod(index, left.shape[1])
        found.append((x, y, round(value, 3)))
        left[max(0, y - reach_y) : y + reach_y + 1, max(0, x - reach_x) : x + reach_x + 1] = -np.inf
    return found


def matches(frame: Any, logo: Any, quad: Any) -> list[tuple[tuple[float, float, float, float], float]]:
    """The places of *frame* (``H x W x 3``) showing *logo* (RGBA) at the size of *quad* (pixels): 0-1 boxes and scores.

    The asset is template-matched over the whole frame at that size, brought to
    about :data:`DUPLICATE_LOGO_SIDE` pixels on its longer side - by its colours
    on its own opaque pixels from :data:`MIN_MATCH`, a logo of one flat colour
    by its shape from :data:`MIN_SHAPE_MATCH`, as :func:`locate` finds it.
    """
    import numpy as np
    from PIL import Image

    from app.services.video_studio.pictures import quad_size

    width, height = quad_size(tuple((float(x), float(y)) for x, y in quad))
    scale = min(2.0, DUPLICATE_LOGO_SIDE / max(width, height, 1.0))
    template = (round(width * scale), round(height * scale))
    frame_h, frame_w = frame.shape[:2]
    shown = Image.fromarray(np.ascontiguousarray(frame), "RGB")
    sized = shown.resize((max(1, round(frame_w * scale)), max(1, round(frame_h * scale))), Image.Resampling.BOX)
    if min(template) < 8 or template[0] >= sized.width or template[1] >= sized.height:
        return []
    pixels = np.asarray(sized, dtype=np.float64)
    if _flat(logo):
        scores = _correlation(pixels @ np.array([0.299, 0.587, 0.114]), _grey_over_neutral(logo, template))
        threshold = MIN_SHAPE_MATCH
    else:
        scores = _colour_correlation(pixels, logo, template)
        threshold = MIN_MATCH
    if scores is None:
        return []
    reach = (max(1, round(template[0] * 0.6)), max(1, round(template[1] * 0.6)))
    return [
        ((x / sized.width, y / sized.height, (x + template[0]) / sized.width, (y + template[1]) / sized.height), score)
        for x, y, score in _peaks(scores, threshold, reach)
    ]


async def find_duplicates(
    video_path: str, logos: list[Logo], tracks: list[list[Placement | None]], *, width: int, height: int, fps: float
) -> tuple[Duplicate | None, int]:
    """The first frame of *video_path* showing one of *logos* in more than one place, and how many frames were sampled.

    Every :data:`DUPLICATE_SAMPLE_EVERY` frames each logo is matched over the
    whole frame (:func:`matches`) at the size its track drew it at in that
    frame, or in the nearest frame it drew it (a logo turned away can still
    show twice). The duplicate carries that frame, every place outlined.
    """
    import numpy as np

    from app.services.video_studio import encoder

    drawn = [[index for index, place in enumerate(track) if place is not None] for track in tracks]
    scale = np.array([width, height], dtype=float)
    index = sampled = 0
    frames = encoder.iter_rgb_frames(video_path, width=width, height=height)
    async with contextlib.aclosing(frames):
        async for raw in frames:
            if index % DUPLICATE_SAMPLE_EVERY == 0:
                frame = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
                sampled += 1
                for logo, track, shows in zip(logos, tracks, drawn, strict=True):
                    if not shows:
                        continue
                    nearest = min(shows, key=lambda frame_index: abs(frame_index - index))
                    quad = np.asarray(track[nearest].quad, dtype=float) * scale
                    places = await asyncio.to_thread(matches, frame, logo.picture, quad)
                    if len(places) > 1:
                        boxes = tuple(tuple(round(v, 4) for v in box) for box, _score in places)
                        outlines = [
                            [(x0 * width, y0 * height), (x1 * width, y0 * height), (x1 * width, y1 * height), (x0 * width, y1 * height)]
                            for x0, y0, x1, y1 in boxes
                        ]
                        shown = await asyncio.to_thread(_frame_jpeg, frame, *outlines)
                        return Duplicate(logo.label, index, index / max(fps, 1.0), boxes, shown), sampled
            index += 1
    return None, sampled


def _played_at(scene: dict, seconds: float, *, offset: float, span: tuple[float, float] | None) -> float | None:
    """Where *seconds* of the AI shot *scene* plays falls in the MP4 (from *offset*); ``None`` when not played there.

    Placed in its scene's time through the scene's time map (a statement's
    backdrop's own); a moment outside what the scene plays, or outside a
    preview's *span* ``(start, seconds)``, is not this render's.
    """
    from app.services.video_studio import timeline

    footage = scene.get("backdrop") if scene.get("type") == "statement" and isinstance(scene.get("backdrop"), dict) else scene
    time_map = [tuple(point) for point in footage.get("time_map") or []]
    if time_map and not time_map[0][1] - 1e-3 <= seconds <= time_map[-1][1] + 1e-3:
        return None
    start = float(scene.get("start") or 0.0)
    when = start + (timeline.map_inverse(time_map, seconds) if time_map else seconds)
    if when > float(scene.get("end") or when) + 1e-3:
        return None
    if span is not None and not span[0] - 1e-3 <= when <= span[0] + span[1] + 1e-3:
        return None
    return max(0.0, when - offset)


def integration_findings(
    document: dict, shots: dict[str, str], checks: dict[str, dict], *, offset: float = 0.0, span: tuple[float, float] | None = None
) -> list[tuple[Any, str | None]]:
    """A ``logo_not_integrated`` finding for each scene of *document* playing a take whose logo reads as a sticker.

    *checks* maps an AI shot to its take's logo check with the integration
    of the logo drawn onto it, composited or tracked
    (``frame_qc.logo_gate``). A logo not integrated
    and not accepted flat (``ingredients_quality: "flat"``) fails every
    scene playing that frame, in the scene's time (:func:`_played_at`), with
    the frame's file (``frame_file_id``) and one fix. A failure like the
    audio's: the next render is refused.
    """
    from app.services.video_studio import logo_integration
    from app.services.video_studio.render_checks import FAIL, LOGO_NOT_INTEGRATED, Finding, clock

    out: list[tuple[Any, str | None]] = []
    for scene in document.get("scenes") or []:
        sid = scene.get("id")
        record = checks.get(shots.get(sid or "", "")) if sid else None
        integration = record.get("integration") if isinstance(record, dict) else None
        if not isinstance(integration, dict) or integration.get("passed", True) or integration.get("waived"):
            continue
        seconds = float(integration.get("seconds") or 0.0)
        if (at := _played_at(scene, seconds, offset=offset, span=span)) is None:
            continue
        label = str(integration.get("label") or "its logo")
        measure = str(integration.get("failed") or "")
        out.append(
            (
                Finding(
                    LOGO_NOT_INTEGRATED,
                    at,
                    at,
                    sid,
                    f"logo not integrated at t={clock(at)}: Scene {sid}: its AI shot draws {label} as a flat sticker "
                    f"({logo_integration.MEASURE_WORDS.get(measure, measure)}, integration score "
                    f"{float(integration.get('score') or 0.0):.2f} under {logo_integration.MIN_SCORE:g}; frame "
                    f"{integration.get('frame')} of the shot, {seconds:.2f} s) - generate the shot again with a model "
                    "that takes the logo as a picture, or accept it flat (ingredients_quality: \"flat\")",
                    FAIL,
                    {
                        "clip_id": shots.get(sid or ""),
                        "score": integration.get("score"),
                        "measure": measure,
                        "shot_frame": integration.get("frame"),
                        "shot_seconds": round(seconds, 3),
                    },
                ),
                integration.get("frame_file_id"),
            )
        )
    return out


#: The brand marks looked for on a drawn frame (:func:`brand_marks_on`): their width searched as a share of the
#: frame's, from, to and in how many sizes, on a frame brought to at most :data:`FIND_SIDE` pixels. A logo filling the
#: frame (a picture of the wordmark itself) is matched up to nearly its whole width.
MARK_MIN_SHARE, MARK_MAX_SHARE, MARK_SCALES = 0.03, 0.95, 40
#: The best size of each mark is refined around it, this many sizes across one coarse step each side: a wordmark's
#: match peaks within about 1 % of its size, finer than the coarse steps (about 9 % apart).
MARK_REFINE_STEPS, MARK_REFINE_SPREAD = 13, 0.095
#: A mark whose best coarse score is under this is not refined: nothing of it is on the frame.
MARK_REFINE_FROM = 0.4
#: Two places sharing more than this share of the smaller one are one mark (the bare mark inside the logo, one mark
#: matched at two near sizes).
MARK_SAME_PLACE = 0.3
#: A mark of one flat colour matched by its shape alone over the whole frame must reach this, past :data:`MIN_SHAPE_MATCH`
#: (dev:render-picture-check-gate-drift): a statement's white lettering over dimmed footage correlates with a white
#: wordmark's shape at about 0.76, the mark itself above 0.9. The corner watermark, read where it is drawn
#: (:func:`watermark_on`), keeps :data:`MIN_SHAPE_MATCH`.
MARK_MIN_SHAPE_MATCH = 0.85
#: A place whose mark's own pixels differ from the pixels around them in its box by less than this (grey levels, the
#: larger of the channel means) holds no mark a viewer sees, whatever its correlation (dev:render-picture-check-gate-
#: drift-2): a normalised correlation ignores contrast, so a faint reflection on a glossy floor can match a mark's shape.
MARK_MIN_CONTRAST = 16.0


def mark_contrast(pixels: Any, mark: Any, box_px: tuple[int, int, int, int]) -> float:
    """How far *mark*'s opaque pixels stand out from its transparent ones over *box_px* (``x, y, w, h``, pixels) of a
    frame (*pixels*, RGB float array): the largest absolute difference of the channel means, in grey levels; ``0.0``
    when either side is empty."""
    import numpy as np
    from PIL import Image

    x, y, w, h = box_px
    crop = pixels[y : y + h, x : x + w]
    if crop.shape[0] != h or crop.shape[1] != w:
        return 0.0
    alpha = np.asarray(mark.convert("RGBA").resize((w, h), Image.Resampling.LANCZOS), dtype=np.float64)[..., 3]
    on, off = alpha > 127, alpha <= 127
    if not on.any() or not off.any():
        return 0.0
    return float(np.abs(crop[on].mean(axis=0) - crop[off].mean(axis=0)).max())


def _shared(one: tuple[float, float, float, float], other: tuple[float, float, float, float]) -> float:
    """The share of the smaller of two 0-1 boxes the other covers."""
    width = min(one[2], other[2]) - max(one[0], other[0])
    height = min(one[3], other[3]) - max(one[1], other[1])
    if width <= 0 or height <= 0:
        return 0.0
    smaller = min((one[2] - one[0]) * (one[3] - one[1]), (other[2] - other[0]) * (other[3] - other[1]))
    return width * height / max(smaller, 1e-9)


def brand_marks_on(frame: Any, marks: dict[str, Any]) -> list[tuple[str, tuple[float, float, float, float], float]]:
    """Every place of *frame* (a drawn frame, RGB) showing one of the template's brand *marks* (``role -> RGBA``):
    ``(role, 0-1 box, score)``, best first, one per place (``duplicate_logo``).

    Each mark is template-matched over the whole frame at :data:`MARK_SCALES` sizes - by its colours on its own opaque
    pixels from :data:`MIN_MATCH`, a mark of one flat colour by its shape from :data:`MIN_SHAPE_MATCH`, as
    :func:`locate` finds it -, every place past the threshold kept; places sharing more than :data:`MARK_SAME_PLACE`
    are one (the bare mark inside the logo is the logo). The best size of each mark is refined around it
    (:data:`MARK_REFINE_STEPS`), so a logo between two coarse sizes is matched too. The corner watermark,
    drawn smaller than :data:`MARK_MIN_SHARE`, is :func:`watermark_on`'s.
    """
    import numpy as np
    from PIL import Image

    sample = _rgb(frame)
    scale = min(1.0, FIND_SIDE / max(sample.size))
    if scale < 1.0:
        sample = sample.resize((max(1, round(sample.width * scale)), max(1, round(sample.height * scale))), Image.Resampling.BOX)
    pixels = np.asarray(sample, dtype=np.float64)
    grey = pixels @ np.array([0.299, 0.587, 0.114])
    width = grey.shape[1]
    found: list[tuple[str, tuple[float, float, float, float], float]] = []
    for role, mark in marks.items():
        if mark is None:
            continue
        best: tuple[float, float] | None = None
        for share in np.geomspace(MARK_MIN_SHARE, MARK_MAX_SHARE, MARK_SCALES):
            top = _mark_places(role, mark, share * width, pixels, grey, found)
            if top is not None and (best is None or top > best[0]):
                best = (top, share * width)
        if best is not None and best[0] >= MARK_REFINE_FROM:
            for factor in np.linspace(1 - MARK_REFINE_SPREAD, 1 + MARK_REFINE_SPREAD, MARK_REFINE_STEPS):
                if factor != 1.0:
                    _mark_places(role, mark, best[1] * factor, pixels, grey, found)
    found.sort(key=lambda item: -item[2])
    kept: list[tuple[str, tuple[float, float, float, float], float]] = []
    for item in found:
        if all(_shared(item[1], other[1]) <= MARK_SAME_PLACE for other in kept):
            kept.append(item)
    return kept


def _mark_places(
    role: str, mark: Any, logo_width: float, pixels: Any, grey: Any, found: list[tuple[str, tuple[float, float, float, float], float]]
) -> float | None:
    """*mark* matched at *logo_width* pixels wide over a frame (*pixels* RGB, *grey* its grey): each place past the
    threshold appended to *found* as ``(role, 0-1 box, score)``; the best score, ``None`` when that size cannot be
    tried (:func:`brand_marks_on`)."""
    import numpy as np

    height, width = grey.shape
    flat = _flat(mark)
    w = round(logo_width)
    h = round(w * mark.height / max(mark.width, 1))
    if w < 8 or h < 8 or w >= width or h >= height:
        return None
    scores = _correlation(grey, _grey_over_neutral(mark, (w, h))) if flat else _colour_correlation(pixels, mark, (w, h))
    if scores is None:
        return None
    reach = (max(1, round(w * 0.6)), max(1, round(h * 0.6)))
    for x, y, score in _peaks(scores, MARK_MIN_SHAPE_MATCH if flat else MIN_MATCH, reach):
        if mark_contrast(pixels, mark, (x, y, w, h)) < MARK_MIN_CONTRAST:
            continue
        found.append((role, (x / width, y / height, (x + w) / width, (y + h) / height), score))
    return float(np.max(scores))


def _rgb(frame: Any) -> Any:
    """*frame* (a picture or an RGB array) as an RGB picture."""
    import numpy as np
    from PIL import Image

    return (frame if hasattr(frame, "convert") else Image.fromarray(np.ascontiguousarray(frame), "RGB")).convert("RGB")


#: The corner watermark as the page draws it (``#watermark`` in ``compositor_page``): its height in CSS pixels
#: of the design canvas, and its margin from the frame's edges as a share of the canvas height.
WATERMARK_HEIGHT_PX, WATERMARK_MARGIN_SHARE = 44, 0.04
#: The watermark is looked for at sizes this far around the drawn one, in this many steps ...
WATERMARK_SPREAD, WATERMARK_STEPS = 0.2, 9
#: ... on its corner brought up so the mark is at least this many pixels high (a 720p draft draws it 29 px high).
WATERMARK_FIND_SIDE = 72
#: The role a watermark place carries in ``duplicate_logo``'s boxes.
WATERMARK_ROLE = "watermark"


def watermark_on(
    frame: Any, mark: Any, corner: str, *, design_height: float
) -> tuple[str, tuple[float, float, float, float], float] | None:
    """The corner watermark on *frame* (a drawn frame, RGB) where and at the size the page draws it:
    ``("watermark", 0-1 box, score)``, ``None`` when it does not show there.

    The page draws *mark* (RGBA, the template's mark, else its logo) :data:`WATERMARK_HEIGHT_PX` CSS pixels high on a
    canvas *design_height* pixels high, :data:`WATERMARK_MARGIN_SHARE` of that height from the two edges of *corner*
    (``bottom-right``, ``top-left``...): about 2 % of the frame's width, under what :func:`brand_marks_on` searches.
    Only that corner is read, at the frame's own resolution brought up to :data:`WATERMARK_FIND_SIDE`, at
    :data:`WATERMARK_STEPS` sizes around the drawn one, by colours or shape as :func:`locate` does.
    """
    import numpy as np
    from PIL import Image

    sample = _rgb(frame)
    frame_w, frame_h = sample.size
    per_px = frame_h / max(float(design_height), 1.0)
    mark_h = WATERMARK_HEIGHT_PX * per_px
    mark_w = mark_h * mark.width / max(mark.height, 1)
    margin = WATERMARK_MARGIN_SHARE * frame_h
    region_w = min(frame_w, round(2 * (mark_w + margin)))
    region_h = min(frame_h, round(2 * (mark_h + margin)))
    left = 0 if "left" in corner else frame_w - region_w
    top = 0 if "top" in corner else frame_h - region_h
    region = sample.crop((left, top, left + region_w, top + region_h))
    zoom = max(1.0, WATERMARK_FIND_SIDE / max(mark_h, 1.0))
    if zoom > 1.0:
        region = region.resize((round(region_w * zoom), round(region_h * zoom)), Image.Resampling.BICUBIC)
    pixels = np.asarray(region, dtype=np.float64)
    grey = pixels @ np.array([0.299, 0.587, 0.114])
    height, width = grey.shape
    flat = _flat(mark)
    best: tuple[float, int, int, int, int] | None = None
    for factor in np.linspace(1 - WATERMARK_SPREAD, 1 + WATERMARK_SPREAD, WATERMARK_STEPS):
        w, h = round(mark_w * zoom * factor), round(mark_h * zoom * factor)
        if w < 8 or h < 8 or w >= width or h >= height:
            continue
        scores = _correlation(grey, _grey_over_neutral(mark, (w, h))) if flat else _colour_correlation(pixels, mark, (w, h))
        if scores is None:
            continue
        index = int(np.argmax(scores))
        score = float(scores.flat[index])
        if math.isfinite(score) and (best is None or score > best[0]):
            y, x = divmod(index, scores.shape[1])
            best = (score, x, y, w, h)
    if best is None or best[0] < (MIN_SHAPE_MATCH if flat else MIN_MATCH):
        return None
    score, x, y, w, h = best
    x0, y0 = left + x / zoom, top + y / zoom
    box = (x0 / frame_w, y0 / frame_h, (x0 + w / zoom) / frame_w, (y0 + h / zoom) / frame_h)
    return WATERMARK_ROLE, box, round(score, 3)


def with_watermark(
    places: list[tuple[str, tuple[float, float, float, float], float]],
    watermark: tuple[str, tuple[float, float, float, float], float] | None,
) -> list[tuple[str, tuple[float, float, float, float], float]]:
    """*places* (:func:`brand_marks_on`) with the *watermark* found (:func:`watermark_on`) among the first two, so a
    ``duplicate_logo`` finding outlines it; a place of *places* over the watermark is the watermark."""
    if watermark is None:
        return places
    others = [place for place in places if _shared(place[1], watermark[1]) <= MARK_SAME_PLACE]
    return [*others[:1], watermark, *others[1:]]


#: A region an AI shot draws back from its start picture (``keep_regions``, shares of the frame) is grown by this share
#: of the frame before a place is said to sit over it: the shot's camera moves the region a little.
KEPT_REGION_GROW = 0.02
#: A place over a kept region is a brand mark only when the template's mark is matched again inside it, at the frame's
#: own resolution, from these scores (dev:duplicate-logo-gate-fires-on-keep-regions): the whole-frame search reads a
#: downscaled frame from :data:`MIN_MATCH`, where small third-party lettering drawn back from the picture can reach it;
#: the brand's own mark matched at full size scores about 0.97.
KEPT_MIN_MATCH, KEPT_MIN_SHAPE_MATCH = 0.93, 0.92
#: The sizes the mark is matched at inside the place, around its width, and the margin read around the place (shares
#: of its size).
KEPT_SIZES, KEPT_SPREAD, KEPT_PAD = 9, 0.15, 0.25


def in_kept_region(box: tuple[float, float, float, float], regions: list[list[float]] | None) -> bool:
    """Whether *box* (a 0-1 ``x0, y0, x1, y1`` place) sits over one of *regions* (``[x, y, width, height]`` shares a
    shot keeps from its start picture), grown by :data:`KEPT_REGION_GROW`: more than :data:`MARK_SAME_PLACE` of the
    smaller of the two shared."""
    grow = KEPT_REGION_GROW
    for region in regions or []:
        x, y, w, h = (float(v) for v in region)
        if _shared(box, (x - grow, y - grow, x + w + grow, y + h + grow)) > MARK_SAME_PLACE:
            return True
    return False


def mark_matched_in(frame: Any, mark: Any, box: tuple[float, float, float, float]) -> float:
    """The best match of *mark* (RGBA) inside *box* (0-1) of *frame*, read at the frame's own resolution with a margin
    of :data:`KEPT_PAD` around the place, at :data:`KEPT_SIZES` widths within :data:`KEPT_SPREAD` of the place's: by its
    colours, or by its shape for a mark of one flat colour, a place with no contrast scoring nothing; ``0.0`` when no
    size fits."""
    import numpy as np

    sample = _rgb(frame)
    frame_w, frame_h = sample.size
    x0, y0, x1, y1 = box
    box_w, box_h = (x1 - x0) * frame_w, (y1 - y0) * frame_h
    left = max(0, math.floor(x0 * frame_w - KEPT_PAD * box_w))
    top = max(0, math.floor(y0 * frame_h - KEPT_PAD * box_h))
    right = min(frame_w, math.ceil(x1 * frame_w + KEPT_PAD * box_w))
    bottom = min(frame_h, math.ceil(y1 * frame_h + KEPT_PAD * box_h))
    if right - left < 8 or bottom - top < 8:
        return 0.0
    pixels = np.asarray(sample.crop((left, top, right, bottom)), dtype=np.float64)
    grey = pixels @ np.array([0.299, 0.587, 0.114])
    height, width = grey.shape
    flat = _flat(mark)
    best = 0.0
    for factor in np.linspace(1 - KEPT_SPREAD, 1 + KEPT_SPREAD, KEPT_SIZES):
        w = round(box_w * factor)
        h = round(w * mark.height / max(mark.width, 1))
        if w < 8 or h < 8 or w > width or h > height:
            continue
        scores = _correlation(grey, _grey_over_neutral(mark, (w, h))) if flat else _colour_correlation(pixels, mark, (w, h))
        if scores is None:
            continue
        scores = np.nan_to_num(scores, nan=-1.0, posinf=-1.0, neginf=-1.0)
        index = int(np.argmax(scores))
        score = float(scores.flat[index])
        if score <= best:
            continue
        y, x = divmod(index, scores.shape[1])
        if mark_contrast(pixels, mark, (x, y, w, h)) >= MARK_MIN_CONTRAST:
            best = score
    return best


#: A place the whole-frame search found (on a frame brought down to :data:`FIND_SIDE`) is a brand mark only when the
#: template's mark is matched again on the place itself, at the frame's own resolution (:func:`mark_matched_in`), from
#: these scores (dev:duplicate-logo-gate-false-positive-after-8465): a plain background or third-party lettering reached
#: the whole-frame bars only once scaled down, and scores far under these on the cropped box; the brand's own mark in
#: footage (soft, at an angle) keeps about 0.9, so a real mark under the watermark still counts.
BOX_MIN_MATCH, BOX_MIN_SHAPE_MATCH = 0.8, 0.8


def page_draws_brand(document: dict, scene: dict, *, overlay: str | None = None) -> bool:
    """Whether the page composites a brand mark over *scene* of a planned *document* at the moment read: the corner
    watermark (:func:`scene_watermarked`), a brand image overlay on screen (*overlay*), or a card carrying the brand
    itself (a title, an outro, a statement with ``show_logo``, a motion scene with ``brands``). When it does not, every
    mark on the frame is the footage's own (its start picture, its kept regions, a logo worn by several subjects): a
    fact of the shot, never a ``duplicate_logo``."""
    if overlay or scene_watermarked(document, scene):
        return True
    kind = scene.get("type")
    return kind in ("title", "outro") or bool(kind == "statement" and scene.get("show_logo")) or bool(
        kind == "motion" and scene.get("brands")
    )


def confirmed_places(
    frame: Any,
    marks: dict[str, Any],
    places: list[tuple[str, tuple[float, float, float, float], float]],
    regions: list[list[float]] | None = None,
) -> tuple[
    list[tuple[str, tuple[float, float, float, float], float]],
    list[tuple[str, tuple[float, float, float, float], float, float, bool]],
]:
    """*places* (:func:`brand_marks_on`, :func:`with_watermark`) with each place kept only when the template's mark of
    its role is matched again on the place itself (:func:`mark_matched_in`, the cropped box read at the frame's own
    resolution): from :data:`KEPT_MIN_MATCH` (:data:`KEPT_MIN_SHAPE_MATCH` for a flat mark) over a region the shot draws
    back from its start picture (*regions*, ``keep_regions``), else from :data:`BOX_MIN_MATCH`
    (:data:`BOX_MIN_SHAPE_MATCH`): ``(places kept, places cleared as (role, box, score, score on the box, over a kept
    region))``. The watermark is the page's own drawing, read where it is drawn (:func:`watermark_on`), and stays."""
    kept: list[tuple[str, tuple[float, float, float, float], float]] = []
    cleared: list[tuple[str, tuple[float, float, float, float], float, float, bool]] = []
    for role, box, score in places:
        mark = marks.get(role)
        if role == WATERMARK_ROLE or mark is None:
            kept.append((role, box, score))
            continue
        over_kept = in_kept_region(box, regions)
        flat = _flat(mark)
        if over_kept:
            bar = KEPT_MIN_SHAPE_MATCH if flat else KEPT_MIN_MATCH
        else:
            bar = BOX_MIN_SHAPE_MATCH if flat else BOX_MIN_MATCH
        matched = mark_matched_in(frame, mark, box)
        if matched >= bar:
            kept.append((role, box, score))
        else:
            cleared.append((role, box, score, round(matched, 3), over_kept))
    return kept, cleared


def scene_watermarked(document: dict, scene: dict) -> bool:
    """Whether the corner watermark stands over *scene* of a planned *document* once it is settled: the video
    has one, the scene's spec does not turn it off (``watermark: false``) and it is not a card carrying the brand itself
    (``brandsItself`` in ``compositor_page``)."""
    corner = document.get("watermark")
    if not corner or corner == "none" or scene.get("watermark") is False:
        return False
    kind = scene.get("type")
    if kind in ("title", "outro") or (kind == "statement" and scene.get("show_logo")):
        return False
    return not (kind == "motion" and scene.get("brands"))


def duplicate_logo_finding(
    scene: dict,
    places: list[tuple[str, tuple[float, float, float, float], float]],
    *,
    at: float,
    watermarked: bool,
    clip_id: str | None = None,
    overlay: str | None = None,
    regions: list[list[float]] | None = None,
    assets: dict[str, str] | None = None,
) -> Any | None:
    """The ``duplicate_logo`` finding of *scene* when the brand's marks show in more than one place of its frame
    at *at* (``places``, :func:`brand_marks_on`), with the two best boxes, what each matched (``matched``: the mark's
    role, its file in *assets*, the score, whether it sits over a region the shot keeps from its start picture,
    *regions*) and one fix: when a brand image overlay is on screen at *at* (*overlay*, its asset: ``logo`` or
    ``mark``) that overlay dropped, else the watermark off on that scene when it is on, else, when a place sits over a
    kept region, that region named (the start picture itself shows the mark there: a new take re-applies it), else the
    shot generated again with one logo; ``None`` with one place or none."""
    from app.services.video_studio.render_checks import DUPLICATE_LOGO, FAIL, Finding, clock

    if len(places) < 2:
        return None
    sid = scene.get("id")
    over_kept = [in_kept_region(box, regions) for _role, box, _score in places[:2]]
    if overlay:
        fix = f"drop the image overlay of the template's {overlay} on scene {sid}" + (
            " or turn the watermark off on it (watermark: false on the scene)" if watermarked else ""
        )
    elif watermarked:
        fix = f"turn the watermark off on scene {sid} (watermark: false on the scene) - its footage shows the brand already"
    elif any(over_kept):
        fix = (
            "the shot's start picture shows the brand's mark inside a region it keeps (keep_regions draws it back in "
            "every frame, a new take re-applies it): start from a picture with one logo, or give the shot keep_regions "
            "without that mark"
        )
    elif clip_id:
        fix = "generate the shot again in place (clip_id) with one logo"
    else:
        fix = "keep one brand mark on the scene: change its footage or drop the mark it draws on top"
    roles = ", ".join(role for role, _box, _score in places[:2])
    data: dict[str, Any] = {
        "count": len(places),
        "boxes": [[round(v, 4) for v in box] for _role, box, _score in places[:2]],
        "roles": [role for role, _box, _score in places[:2]],
        "scores": [score for _role, _box, score in places[:2]],
        "matched": [
            {"role": role, "asset": (assets or {}).get(role), "score": score, "kept_region": kept}
            for (role, _box, score), kept in zip(places[:2], over_kept, strict=True)
        ],
        "watermark": watermarked,
    }
    if overlay:
        data["overlay"] = True
        data["overlay_asset"] = overlay
    if clip_id:
        data["clip_id"] = clip_id
    return Finding(
        DUPLICATE_LOGO,
        at,
        at,
        sid,
        f"duplicate logo at t={clock(at)}: Scene {sid} shows the brand's mark in {len(places)} places of one frame "
        f"({roles}{', the watermark among them' if watermarked else ''}; matched "
        + ", ".join(f"{role} {score:.2f}" for role, _box, score in places[:2])
        + f") - {fix}",
        FAIL,
        data,
        fix=fix,
    )


#: The matched crops under a ``duplicate_logo`` frame are drawn at 100 %, brought up to at least this height.
CROP_STRIP_MIN_HEIGHT = 120


def boxed_frame_jpeg(frame: Any, boxes: list[tuple[float, float, float, float]]) -> bytes:
    """*frame* (RGB, a picture or an array) as a JPEG with each 0-1 box of *boxes* outlined, and the matched crops of
    those boxes at 100 % in a strip under it (dev:render-picture-check-gate-drift-2): the frame a ``duplicate_logo``
    finding shows, with what each box holds readable at a glance."""
    import io

    import numpy as np
    from PIL import Image

    rgb = np.asarray(frame.convert("RGB")) if hasattr(frame, "convert") else np.asarray(frame)
    height, width = rgb.shape[:2]
    outlines = [((x0 * width, y0 * height), (x1 * width, y0 * height), (x1 * width, y1 * height), (x0 * width, y1 * height)) for x0, y0, x1, y1 in boxes]
    boxed = Image.open(io.BytesIO(_frame_jpeg(rgb, *outlines))).convert("RGB")
    picture = Image.fromarray(np.ascontiguousarray(rgb), "RGB")
    crops = []
    for x0, y0, x1, y1 in boxes:
        left, top = max(0, round(x0 * width)), max(0, round(y0 * height))
        right, bottom = min(width, round(x1 * width)), min(height, round(y1 * height))
        if right - left < 2 or bottom - top < 2:
            continue
        crop = picture.crop((left, top, right, bottom))
        zoom = max(1.0, CROP_STRIP_MIN_HEIGHT / crop.height)
        crops.append(crop.resize((round(crop.width * zoom), round(crop.height * zoom)), Image.Resampling.NEAREST))
    if not crops:
        out = io.BytesIO()
        boxed.save(out, format="JPEG", quality=88)
        return out.getvalue()
    gap = 8
    strip_h = max(crop.height for crop in crops)
    strip_w = sum(crop.width for crop in crops) + gap * (len(crops) - 1)
    scale = min(1.0, width / max(strip_w, 1))
    if scale < 1.0:
        crops = [crop.resize((max(1, round(crop.width * scale)), max(1, round(crop.height * scale)))) for crop in crops]
        strip_h = max(crop.height for crop in crops)
    sheet = Image.new("RGB", (width, height + gap + strip_h), (24, 24, 24))
    sheet.paste(boxed, (0, 0))
    x = 0
    for crop in crops:
        sheet.paste(crop, (x, height + gap))
        x += crop.width + gap
    out = io.BytesIO()
    sheet.save(out, format="JPEG", quality=88)
    return out.getvalue()


def duplicate_findings(
    document: dict, shots: dict[str, str], checks: dict[str, dict], *, offset: float = 0.0, span: tuple[float, float] | None = None
) -> list[tuple[Any, str | None]]:
    """A ``logo_duplicated`` finding for each scene of *document* playing a take whose logo shows twice.

    *shots* maps a scene id to the AI shot it plays (its footage, a
    statement's backdrop), *checks* an AI shot to its take's duplicate check
    (``frame_qc.LOGO_CHECK``). The frame the take recorded is placed in its
    scene's time through the scene's time map, in the MP4's time from
    *offset*; one outside what the scene plays, or outside a preview's *span*
    ``(start, seconds)``, is not this render's. Each finding comes with the
    file id of the take's frame (``frame_file_id``), stored again as a file
    of the render. A failure like the audio's: the next render is refused.
    """
    from app.services.video_studio.render_checks import FAIL, LOGO_DUPLICATED, Finding, clock

    out: list[tuple[Any, str | None]] = []
    for scene in document.get("scenes") or []:
        sid = scene.get("id")
        record = checks.get(shots.get(sid or "", "")) if sid else None
        if not isinstance(record, dict) or record.get("status") != "duplicated":
            continue
        seconds = float(record.get("seconds") or 0.0)
        if (at := _played_at(scene, seconds, offset=offset, span=span)) is None:
            continue
        label = str(record.get("label") or "its logo")
        count = int(record.get("count") or 2)
        out.append(
            (
                Finding(
                    LOGO_DUPLICATED,
                    at,
                    at,
                    sid,
                    f"logo duplicated at t={clock(at)}: Scene {sid}: its AI shot shows {label} {count} times in one frame "
                    f"(frame {record.get('frame')} of the shot, {seconds:.2f} s) - generate the shot again in place "
                    "(clip_id)",
                    FAIL,
                    {
                        "clip_id": shots.get(sid or ""),
                        "count": count,
                        "shot_frame": record.get("frame"),
                        "shot_seconds": round(seconds, 3),
                    },
                ),
                record.get("frame_file_id"),
            )
        )
    return out


