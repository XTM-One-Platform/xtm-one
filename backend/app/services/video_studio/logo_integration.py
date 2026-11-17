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

"""A logo drawn onto an AI shot made part of the surface it sits on, and measured so.

A logo composited back onto a shot (``logos``, ``ingredients_path: composited``) or drawn onto its subject
(``tracked_logos``, ``ingredients_path: tracked``) is the brand's own pixels: right, but drawn flat over a surface
that is lit, textured and moving, it reads as a sticker pasted on the frame. Two things here, shared by both paths:

1. **Drawn into the surface** (:func:`integrated`). The logo, warped onto the
   surface by its path (the tracked homography, its mesh on a curved surface),
   takes the surface's broad light (``screens.lit``: a fold's shadow, a lit
   flank) and its fine relief - quilting, weave, creases - through the
   lettering (:data:`TEXTURE`): it reads printed or embroidered, not stuck on.
2. **Measured** (:class:`Probe`). On frames sampled every :data:`SAMPLE_EVERY`:

   - *shading*: how much the light varies across the logo as drawn (what was
     drawn over what the asset is) against how much it varies across the
     surface around it. A flat patch on a shaded surface varies close to
     nothing of it (a score of 1 from :data:`SHADING_TARGET` of it).
   - *follow*: the surface's own points under and around the logo, followed
     from one sampled frame through the next ones, against where the logo's
     drawing sent the same points: a logo moving rigidly, or not at all,
     while the surface under it bends or moves misses more than
     :data:`MAX_MISSED` of the surface's motion.
   - *size*: how the extent of those points changed against how the logo's
     changed: a logo keeping its size while the surface grows or shrinks by
     more than :data:`SIZE_CHANGE` misses more than :data:`MAX_MISSED` of it.

   Each measure is a 0-1 score per frame, read over the frames at its
   :data:`ROBUST_SHARE` quantile (one noisy frame never decides); the
   integration score is the worst of the three. Under :data:`MIN_SCORE` the
   logo is not integrated, at the worst frame of the measure that failed: the
   take's logo check records it, every render playing the shot fails its scene
   with a ``logo_not_integrated`` finding (``logos.integration_findings``), and
   so does the video's save quality check - unless the shot was asked with
   ``ingredients_quality: "flat"`` (:data:`QUALITY_FLAT`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

#: The integration is measured on one frame in this many of the take.
SAMPLE_EVERY = 3
#: The longest side of the frames the surface's points are followed on, and of the frame a failure is shown in.
WORK_SIDE = 640
SHOWN_SIDE = 960
#: How much of the surface's broad shading the logo takes (``screens.lit``), read in this many cells across it: the
#: light across the surface, never the strokes of what the model drew under the logo.
LIGHT = 0.6
LIGHT_CELLS = 6
#: How much of the surface's fine relief (its luma above a blur of this many pixels, relative to its light) prints
#: through the logo: the quilting, the weave, a crease. A relief far past this share is a stroke - the model's own
#: redraw of the logo under it - and fades out instead of printing through.
TEXTURE = 0.6
RELIEF_SIGMA = 4.0
RELIEF_CLIP = 0.4
#: The asset's own pixels the shading is read on: opaque, lighter than this grey level (a black stroke has no light to
#: show), at least this many of them once their edges are eroded away.
SHADING_ALPHA = 0.9
SHADING_FLOOR = 24.0
SHADING_PIXELS = 64
#: A surface whose shading varies less than this (std of its log luma) is lit flat: a flat logo on it is no defect.
FLAT_SURFACE = 0.03
#: The logo's shading varies this share of its surroundings' or more, and it scores 1 (a logo the light shades as it
#: shades the surface around it, less what its own colour hides); half of it scores :data:`MIN_SCORE`.
SHADING_TARGET = 0.3
#: The surface's points followed over up to this many sampled frames from each one; fewer than this many points, or a
#: surface moving less than this many working pixels, says nothing of the follow.
WINDOW = 4
MIN_POINTS = 8
MIN_MOTION = 1.5
#: The logo misses more than this share of the surface's motion, or of its change of size, and it does not follow it.
MAX_MISSED = 0.5
SIZE_CHANGE = 0.1
#: A measure's score is read at this quantile of its frames' scores; the integration fails under :data:`MIN_SCORE`.
ROBUST_SHARE = 0.25
MIN_SCORE = 0.5
#: The mesh a logo drawn as one quad is read on: this many cells across and down.
GRID_CELLS = 8
#: ``ingredients_quality`` on a shot: ``"flat"`` accepts a logo drawn flat (the gate records it, never fails it).
QUALITY_KEY = "ingredients_quality"
QUALITY_FLAT = "flat"
#: The measures, in the order a failure is named, and what each failing says in a finding.
MEASURES = ("shading", "follow", "size")
MEASURE_WORDS = {
    "shading": "lit flat on a shaded surface",
    "follow": "moving rigidly while the surface under it bends or moves",
    "size": "keeping its size while the surface under it grows or shrinks",
}
#: The logo check's status of a take whose logo is not integrated (``frame_qc.LOGO_CHECK``).
NOT_INTEGRATED = "not_integrated"


def luma(rgb: Any) -> Any:
    """The luma of an ``H x W x 3`` RGB array (Rec. 601), as float32 grey levels."""
    from app.services.video_studio.tracking import luma as rec601

    return rec601(rgb)


def broad_light(shade: Any) -> Any:
    """*shade* (a region's luma) with only its broad light left: brought down to :data:`LIGHT_CELLS` cells and back.

    The surface under the logo may hold the model's own redraw of it; its
    strokes must not print through the logo drawn over them, only the light
    across the surface - a fold's shadow, a lit side - may.
    """
    import numpy as np
    from PIL import Image

    height, width = shade.shape
    cells = (LIGHT_CELLS, max(2, round(LIGHT_CELLS * height / max(width, 1))))
    picture = Image.fromarray(np.ascontiguousarray(shade, dtype=np.float32), "F")
    broad = picture.resize(cells, Image.Resampling.BOX).resize((width, height), Image.Resampling.BILINEAR)
    return np.asarray(broad, dtype=np.float32)


def integrated(drawn: Any, alpha: Any, region: Any) -> Any:
    """*drawn* (``H x W x 3`` float RGB, the logo as warped) under the light and relief of *region* (the surface under it).

    *alpha* (0-1) is the logo's coverage: the reference light is the
    surface's median under it. The broad light shades the logo
    (``screens.lit``, :data:`LIGHT`); the fine relief of the surface - its
    luma above a blur of :data:`RELIEF_SIGMA` pixels, clipped to
    :data:`RELIEF_CLIP` - prints through it at :data:`TEXTURE`.
    """
    import numpy as np

    from app.services.video_studio import screens

    broad, reference, relief = surface_light(region, alpha)
    rgb = screens.lit(np.asarray(drawn, dtype=np.float32), broad, reference, LIGHT)
    return np.clip(rgb * (1.0 + TEXTURE * relief)[..., None], 0.0, 255.0)


def surface_light(region: Any, alpha: Any) -> tuple[Any, float, Any]:
    """The light *region* (``H x W x 3``) shows: its broad light, that light's level under *alpha*, its fine relief.

    The relief is the luma above a blur of :data:`RELIEF_SIGMA` pixels over
    the level; one far past :data:`RELIEF_CLIP` is a stroke - the model's
    own redraw of the logo under it - and fades out instead of counting.
    """
    import numpy as np
    from scipy import ndimage

    shade = luma(region)
    broad = broad_light(shade)
    under = np.asarray(alpha) > 0.5
    reference = max(float(np.median(broad[under])) if int(under.sum()) > 32 else float(np.median(broad)), 1.0)
    fine = (ndimage.gaussian_filter(shade, 0.7) - ndimage.gaussian_filter(shade, RELIEF_SIGMA)) / reference
    return broad, reference, fine * np.exp(-((fine / RELIEF_CLIP) ** 2))


def blend(frame: Any, warped: Any) -> Any:
    """*frame* (``H x W x 3`` uint8) with *warped* (``screen_insert.Warped``, straight alpha) drawn into its surface."""
    import numpy as np

    x0, y0, x1, y1 = warped.box
    layer = np.asarray(warped.image, dtype=np.float32)
    alpha = layer[..., 3] / 255.0
    region = frame[y0:y1, x0:x1].astype(np.float32)
    rgb = integrated(layer[..., :3], alpha, region)
    out = frame.copy()
    out[y0:y1, x0:x1] = np.clip(rgb * alpha[..., None] + region * (1 - alpha[..., None]), 0, 255).astype(np.uint8)
    return out


def quad_grid(corners: Any, cells: int = GRID_CELLS) -> Any:
    """The ``(cells + 1) x (cells + 1) x 2`` vertices of a logo drawn as one quad onto *corners* (pixels, TL TR BR BL)."""
    import numpy as np

    from app.services.video_studio.tracking import fit_homography, project

    square = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])
    homography = fit_homography(square, np.asarray(corners, dtype=float))
    steps = np.linspace(0.0, 1.0, cells + 1)
    unit = np.array([[x, y] for y in steps for x in steps])
    if homography is None:
        quad = np.asarray(corners, dtype=float)
        top = quad[0] + (quad[1] - quad[0]) * unit[:, :1]
        bottom = quad[3] + (quad[2] - quad[3]) * unit[:, :1]
        points = top + (bottom - top) * unit[:, 1:]
    else:
        points = project(homography, unit)
    return np.asarray(points, dtype=float).reshape(cells + 1, cells + 1, 2)


def _outer(vertices: Any) -> Any:
    import numpy as np

    grid = np.asarray(vertices, dtype=float)
    return np.array([grid[0, 0], grid[0, -1], grid[-1, -1], grid[-1, 0]])


def _polygon_mask(corners: Any, size: tuple[int, int], offset: tuple[float, float] = (0.0, 0.0)) -> Any:
    import numpy as np
    from PIL import Image, ImageDraw

    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).polygon([(float(x) - offset[0], float(y) - offset[1]) for x, y in corners], fill=255)
    return np.asarray(mask) > 0


def shading(source: Any, out: Any, warped: Any) -> tuple[float, float] | None:
    """The spread of the light across the logo as drawn, and of the light the surface shows there; ``None`` unreadable.

    Read on the asset's opaque, light, edge-eroded pixels. As drawn: the log
    of what *out* shows over what the asset is (*warped*'s colour) - zero
    for a logo stamped flat. The surface: the log of its light across the
    logo's box (:func:`surface_light`: its broad light and its fine relief,
    never the strokes the model drew there) - near zero on a smooth surface
    lit flat, where a flat logo is no defect.
    """
    import numpy as np
    from scipy import ndimage

    x0, y0, x1, y1 = warped.box
    layer = np.asarray(warped.image, dtype=np.float32)
    alpha = layer[..., 3] / 255.0
    asset = luma(layer[..., :3])
    shown = luma(out[y0:y1, x0:x1])
    readable = ndimage.binary_erosion((alpha >= SHADING_ALPHA) & (asset > SHADING_FLOOR), iterations=2)
    if int(readable.sum()) < SHADING_PIXELS or min(x1 - x0, y1 - y0) < 4:
        return None
    inside = np.log((shown[readable] + 4.0) / (asset[readable] + 4.0))
    broad, reference, relief = surface_light(source[y0:y1, x0:x1].astype(np.float32), alpha)
    surface = np.log((broad[readable] + 4.0) / (reference + 4.0)) + np.log1p(np.clip(relief[readable], -0.9, None))
    return float(np.std(inside)), float(np.std(surface))


@dataclass
class _Sample:
    index: int
    grey: Any
    vertices: Any
    shading: tuple[float, float] | None


@dataclass
class Integration:
    """How a drawn logo reads against its surface, over the frames sampled (:class:`Probe`)."""

    label: str
    frames_sampled: int
    #: Per measure: ``{score, value, frames}`` (``value``: the shading ratio, or the share of motion or size missed).
    measures: dict[str, dict] = field(default_factory=dict)
    score: float = 1.0
    #: The measure that failed, its worst frame and the frame's time in the shot; ``None`` when integrated.
    failed: str | None = None
    frame: int | None = None
    seconds: float | None = None
    value: float | None = None
    frame_jpeg: bytes | None = None

    @property
    def passed(self) -> bool:
        return self.failed is None

    def why(self) -> str:
        """What the failing frame shows of the logo, in the words a viewer would use."""
        value = self.value or 0.0
        if self.failed == "shading":
            return (
                f"is lit flat: its shading varies {value:.0%} as much as the surface around it "
                f"(under {SHADING_TARGET * MIN_SCORE:.0%}), a patch pasted over a shaded surface"
            )
        if self.failed == "follow":
            return (
                f"moves rigidly while the surface under it bends or moves: it misses {value:.0%} of the surface's "
                f"motion (over {MAX_MISSED:.0%})"
            )
        if self.failed == "size":
            return (
                f"keeps its size while the surface under it grows or shrinks: it misses {value:.0%} of the surface's "
                f"change of size (over {MAX_MISSED:.0%})"
            )
        return "reads as part of its surface"

    def reason(self) -> str:
        """``logo not integrated at t=...``: what the take's check records and every render playing it says."""
        return (
            f"logo not integrated at t={self.seconds or 0.0:.2f} s (frame {self.frame}) of the shot: {self.label} "
            f"{self.why()} - generate the shot again with a model that takes the logo as a picture (its own reference "
            f'mechanism), or accept it drawn flat with ingredients_quality: "{QUALITY_FLAT}".'
        )

    def record(self, *, waived: bool = False) -> dict:
        """What the take's logo check keeps of it (``frame_qc.LOGO_CHECK``'s ``integration``)."""
        return {
            "label": self.label,
            "score": round(self.score, 3),
            "passed": self.passed,
            "frames_sampled": self.frames_sampled,
            **{name: dict(self.measures[name]) for name in MEASURES if name in self.measures},
            **(
                {
                    "failed": self.failed,
                    "frame": self.frame,
                    "seconds": round(self.seconds or 0.0, 3),
                    "reason": self.reason(),
                }
                if self.failed
                else {}
            ),
            **({"waived": QUALITY_FLAT} if waived and self.failed else {}),
        }


def note(result: Integration, *, flat: bool = False) -> str:
    """The shot's note of *result*: its integration score and its measures, or why it is not integrated."""
    read = ", ".join(f"{name} {result.measures[name]['score']:.2f}" for name in MEASURES if name in result.measures)
    scored = f"integration score {result.score:.2f}" + (f" ({read})" if read else "")
    if result.passed:
        return f"{result.label[:1].upper()}{result.label[1:]} reads as part of its surface: {scored}."
    if flat:
        return (
            f"{result.label[:1].upper()}{result.label[1:]} {result.why()} at t={result.seconds or 0.0:.2f} s (frame "
            f'{result.frame}) - accepted, the shot was asked with ingredients_quality: "{QUALITY_FLAT}" ({scored}).'
        )
    reason = result.reason()
    return f"{reason[:1].upper()}{reason[1:]} ({scored})"


def flat_accepted(params: dict | None) -> bool:
    """Whether a shot was asked with ``ingredients_quality: "flat"``: a logo drawn flat is recorded, never failed."""
    return str((params or {}).get(QUALITY_KEY) or "").strip().lower() == QUALITY_FLAT


def worst(results: list[Integration]) -> Integration | None:
    """The least integrated of *results* (a failure first); ``None`` when there is none."""
    if not results:
        return None
    return min(results, key=lambda one: (one.passed, one.score))


def _quantile(values: list[float]) -> float:
    import numpy as np

    return float(np.quantile(np.asarray(values, dtype=float), ROBUST_SHARE)) if values else 1.0


class Probe:
    """The integration of one logo, read on the frames it is drawn in (:meth:`add`), then measured (:meth:`measure`)."""

    def __init__(self, label: str, *, fps: float, size: tuple[int, int]) -> None:
        self.label = label
        self.fps = max(float(fps), 1.0)
        width, height = size
        self.scale = min(1.0, WORK_SIDE / max(width, height, 1))
        self.work = (max(16, round(width * self.scale)), max(16, round(height * self.scale)))
        self.samples: list[_Sample] = []
        #: Each sampled frame as drawn, a JPEG at most :data:`SHOWN_SIDE` long: the frame a failure names.
        self.frames: dict[int, bytes] = {}

    def wants(self, index: int) -> bool:
        return index % SAMPLE_EVERY == 0

    def add(self, index: int, source: Any, out: Any, warped: Any, vertices: Any) -> None:
        """Frame *index*: *source* before the logo was drawn, *out* after; *warped* the asset as warped, at *vertices*."""
        import numpy as np
        from PIL import Image

        grey = Image.fromarray(np.ascontiguousarray(source), "RGB").convert("L").resize(self.work, Image.Resampling.BILINEAR)
        self.samples.append(
            _Sample(index, np.asarray(grey), np.asarray(vertices, dtype=float) * self.scale, shading(source, out, warped))
        )
        self.frames[index] = _jpeg(out)

    def measure(self) -> Integration:
        """The three measures over the samples and the integration score; the worst frame of a failing measure named."""
        per_frame: dict[str, list[tuple[float, int, float]]] = {name: [] for name in MEASURES}
        for sample in self.samples:
            if sample.shading is None:
                continue
            inside, around = sample.shading
            if around < FLAT_SURFACE:
                continue
            ratio = inside / around
            per_frame["shading"].append((min(1.0, ratio / SHADING_TARGET), sample.index, ratio))
        pyramids: dict[int, Any] = {}
        for start in range(len(self.samples) - 1):
            followed = self._follow(start, pyramids)
            if followed is None:
                continue
            index, missed, size_missed = followed
            if missed is not None:
                per_frame["follow"].append((max(0.0, 1.0 - missed / (2 * MAX_MISSED)), index, missed))
            if size_missed is not None:
                per_frame["size"].append((max(0.0, 1.0 - size_missed / (2 * MAX_MISSED)), index, size_missed))
        result = Integration(self.label, len(self.samples))
        failing: list[tuple[float, str, tuple[float, int, float]]] = []
        for name in MEASURES:
            frames = per_frame[name]
            if not frames:
                continue
            score = _quantile([s for s, _i, _v in frames])
            values = sorted(v for _s, _i, v in frames)
            result.measures[name] = {"score": round(score, 3), "value": round(values[len(values) // 2], 3), "frames": len(frames)}
            result.score = min(result.score, score)
            if score < MIN_SCORE:
                failing.append((score, name, min(frames)))
        if failing:
            _score, name, (_s, index, value) = min(failing)
            result.failed, result.frame, result.value = name, index, round(value, 3)
            result.seconds = index / self.fps
            result.frame_jpeg = self.frames.get(index)
        result.score = round(result.score, 3)
        return result

    def _pyramid(self, position: int, pyramids: dict[int, Any]) -> Any:
        from app.services.video_studio.tracking import pyramid

        if position not in pyramids:
            pyramids[position] = pyramid(self.samples[position].grey.astype("float32"))
            for old in [key for key in pyramids if key < position - WINDOW]:
                del pyramids[old]
        return pyramids[position]

    def _follow(self, start: int, pyramids: dict[int, Any]) -> tuple[int, float | None, float | None] | None:
        """From sample *start*, the surface's points followed up to :data:`WINDOW` samples on: the frame reached, the
        share of the surface's motion the logo missed there and the share of its change of size; ``None`` when unread."""
        import numpy as np
        from scipy.interpolate import RBFInterpolator
        from scipy.spatial import ConvexHull, QhullError

        from app.services.video_studio.tracking import good_features, grown, track_points

        first = self.samples[start]
        width, height = self.work
        area = _polygon_mask(grown(_outer(first.vertices), 0.25), (width, height))
        points = good_features(first.grey.astype("float32"), area, max_points=200, min_distance=4)
        if len(points) < MIN_POINTS:
            return None
        seeds, current, reached = points, points, start
        for position in range(start + 1, min(start + WINDOW, len(self.samples) - 1) + 1):
            if self.samples[position].index - self.samples[position - 1].index > 2 * SAMPLE_EVERY:
                break
            found, alive = track_points(self._pyramid(position - 1, pyramids), self._pyramid(position, pyramids), current, round_trip=True)
            if int(alive.sum()) < MIN_POINTS:
                break
            seeds, current, reached = seeds[alive], found[alive], position
        if reached == start:
            return None
        last = self.samples[reached]
        motion = np.linalg.norm(current - seeds, axis=1)
        mapping = RBFInterpolator(first.vertices.reshape(-1, 2), last.vertices.reshape(-1, 2), kernel="thin_plate_spline")
        sent = mapping(seeds)
        missed = None
        if float(np.median(motion)) >= MIN_MOTION:
            missed = float(np.median(np.linalg.norm(current - sent, axis=1)) / np.median(motion))
        size_missed = None
        try:
            before = max(ConvexHull(seeds).volume, 1e-6)
            surface = math.log(max(ConvexHull(current).volume, 1e-6) / before)
            logo = math.log(max(ConvexHull(sent).volume, 1e-6) / before)
        except (QhullError, ValueError):
            surface = logo = 0.0
        if abs(surface) >= math.log(1.0 + SIZE_CHANGE):
            size_missed = abs(surface - logo) / abs(surface)
        return last.index, missed, size_missed


def _jpeg(frame: Any) -> bytes:
    """*frame* (``H x W x 3`` uint8) as a JPEG at most :data:`SHOWN_SIDE` long."""
    import io

    import numpy as np
    from PIL import Image

    picture = Image.fromarray(np.ascontiguousarray(frame), "RGB")
    picture.thumbnail((SHOWN_SIDE, SHOWN_SIDE), Image.Resampling.BILINEAR)
    buffer = io.BytesIO()
    picture.save(buffer, "JPEG", quality=85)
    return buffer.getvalue()
