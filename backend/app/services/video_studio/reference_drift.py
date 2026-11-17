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

"""Cross-scene consistency of a recurring prop or character: a shot's frames read against the video's reference.

A video keeps ``references`` (``spec.VideoReference``): a crop of the prop from the scene that got it right, a
character's portrait. A clip scene names those its AI shot must match (``consistency``). Here each of the shot's
kept frames (first, middle, last: ``frame_qc.FRAMES``) is read against each reference:

* the reference is looked for in the frame by a patch-embedding sliding window (:func:`candidates`): every window
  of the frame, at :data:`FIND_SCALES` sizes and :data:`FIND_ASPECTS` shapes, is described by its colour histogram
  (hue bands at two chroma levels, neutral dark and light apart, in :data:`COLOUR_CELLS` cells), its edge
  orientations (:data:`EDGE_CELLS` cells of :data:`EDGE_BINS` directions) and its lightness layout
  (:data:`LIGHT_CELLS` cells), summed from integral tables; the window nearest the reference's description is
  where the prop shows. A match under :data:`MIN_LOCATE` is no find: the prop is not seen in the frame as the
  reference shows it, which is said (an advisory finding), never a drift;
* found, the crop's structure against the reference's: the cosine of their edge-orientation layouts
  (:func:`structure`, :data:`STRUCTURE_CELLS` cells), under :data:`MIN_SIMILARITY` a drift - a coarse guard,
  since the prop is seen from another angle and scale than the reference;
* found, the crop's colours against the reference's, the main signal: each accent of the reference (a peak of its
  hue histogram at one of :data:`ACCENT_LEVELS` chroma levels, over :data:`MIN_ACCENT_SHARE` of its pixels,
  :func:`accents`) is matched to the nearest accent of the crop in CIELAB a*, b*, the crop's accent brought to the
  reference's chroma first (a paler or deeper shade under another light is the same colour, another hue is not;
  the lightness L* is left out); the farthest is the colour distance, over :data:`MAX_DELTA_E` a drift - a teal
  piping redrawn light blue, a collar the reference has none of. Among the strongest windows (within
  :data:`LOCATE_TIE` of the best) the one nearest in colour is read: a prop seen twice is held to its best showing.

A found frame under either is a drift (:class:`Drift`, dense): the finding names the reference, the frames, the
measures and the crop (the frame's file and the box, 0-1 across and down it). A reference found in none of the
frames is an advisory :class:`Drift` (``found`` false): said, never failing a take or a save. Measured once per
take and reference (``render_cache``, keyed by the files compared and :data:`MEASURE_VERSION`): a save reads the
measure back. Pure measures here (:func:`compare`, :func:`judge`); the files are read by :func:`measure`.

Calibration (real footage, the Normandy film's booth frames at 1920x1080 and 640 wide, the rug's crop as the
reference, the fixtures of ``test_video_studio_reference_drift_6782.py``): the window match reaches 0.746-0.807
where the rug shows (from the front, from behind, at a third of the frame or a tenth of it) and 0.641-0.722 on
frames without it (a navy product screen with teal chips is the nearest miss); the colour distance stays under
10.0 where the rug is the same and reaches 18.6-21.0 with its teal piping recoloured light blue, 25-38 on frames
without it; the structure of the same rug seen from another angle scores 0.74-0.86 (a flat or empty crop, 0).
"""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: The window match (:func:`candidates`) under which the reference is not seen in the frame: calibrated between
#: real frames showing the prop (0.746 and up) and frames without it (0.722 and under); see the module.
MIN_LOCATE = 0.74
#: Windows this close to the best match are the same find: the one nearest the reference in colour is read.
LOCATE_TIE = 0.06
#: The structure (:func:`structure`) a located crop keeps of the reference, under which it drifted: a coarse guard
#: (the same prop from another angle scores 0.74 and up; a flat or empty crop scores about 0).
MIN_SIMILARITY = 0.55
#: The CIELAB chroma distance (a*, b*, at the reference's chroma) an accent of the reference may stand from the
#: crop's nearest: the same prop under another light stays under 10, a piping recoloured light blue passes 18.
MAX_DELTA_E = 14.0
#: The longest side of the frame the reference is looked for in, the window widths tried (shares of it, from, to,
#: how many), the window shapes tried (the reference's aspect times each), the step between windows (a share of
#: their side) and how many distinct windows are kept as candidates.
FIND_SIDE = 384
FIND_MIN_SHARE, FIND_MAX_SHARE, FIND_SCALES = 0.05, 0.5, 10
FIND_ASPECTS = (0.75, 1.0, 1.33)
FIND_STRIDE = 6
FIND_CANDIDATES = 4
#: The window description: colour histogram cells, edge-orientation cells and bins, lightness cells, their weights.
COLOUR_CELLS, EDGE_CELLS, EDGE_BINS, LIGHT_CELLS = 2, 3, 8, 4
WEIGHTS = (1.0, 1.0, 0.5)
#: The colour histogram's hue bands, and the chroma under which a pixel is neutral, over which it is strong; a
#: neutral pixel is dark under this L*.
HUE_BINS = 18
WEAK_CHROMA, STRONG_CHROMA, DARK = 6.0, 18.0, 35.0
#: The longest side a reference, a crop, is described at (:func:`describe`, :func:`accents`).
DESCRIBE_SIDE = 192
ACCENT_SIDE = 256
#: The structure's edge-orientation cells and the side it is read at.
STRUCTURE_CELLS, STRUCTURE_SIDE = 4, 128
#: An accent is a peak of the hue histogram (:data:`ACCENT_BINS` bands) at one of these chroma levels, covering at
#: least this share of the reference's pixels; of the crop's pixels, a smaller share (a piping seen edge-on).
ACCENT_LEVELS = ((10.0, 20.0), (20.0, math.inf))
ACCENT_BINS = 24
MIN_ACCENT_SHARE, MIN_CROP_ACCENT_SHARE = 0.02, 0.004
#: The kind the measures are kept under in ``render_cache``, and the version of how they are taken: bumped with the
#: measure, so a save never reads back a measure taken another way.
CACHE_KIND = "reference-drift"
MEASURE_VERSION = 2
#: The params key of a shot asked with the video's references (``clips.plan_clip``): the video, the references
#: (``{id, file_id, label, kind, description}``) and how they reached the model (``native`` or ``prompt``).
CONSISTENCY = "consistency"
PATH_NATIVE = "native"
PATH_PROMPT = "prompt"
#: What each kept frame is called: by its params name (``frame_qc.FRAMES``).
FRAME_WORDS = {"first_frame_file_id": "first", "mid_frame_file_id": "middle", "last_frame_file_id": "last"}
_NEUTRAL = 128


@dataclass(frozen=True)
class Measure:
    """One frame of a shot read against one reference."""

    #: ``first`` / ``middle`` / ``last``.
    frame: str
    #: The kept frame's file (``frame_qc``), ``None`` for a frame read from the take itself.
    frame_file_id: str | None
    #: The structure (:func:`structure`) the crop keeps of the reference, 0-1.
    similarity: float
    #: The farthest an accent of the reference stands from the crop's nearest, in CIELAB a*, b*.
    delta_e: float
    #: Where the crop stands in the frame: ``(x0, y0, x1, y1)`` as shares of it.
    box: tuple[float, float, float, float]
    #: How well the frame's best window matches the reference's description (:func:`candidates`), 0-1: under
    #: :data:`MIN_LOCATE` the reference is not seen in the frame, and the box is only the nearest window.
    match: float

    @property
    def located(self) -> bool:
        """Whether the reference was found in the frame: only then are its structure and colour judged."""
        return self.match >= MIN_LOCATE

    @property
    def drifted(self) -> bool:
        return self.located and (self.similarity < MIN_SIMILARITY or self.delta_e > MAX_DELTA_E)

    def why(self) -> str:
        """What drifted, as a finding words it: ``colour dE 19 over 14``, ``structure 0.42 under 0.55``; or why the
        frame says nothing: ``not seen as the reference shows it (match 0.66 under 0.74)``."""
        if not self.located:
            return f"not seen as the reference shows it (match {self.match:.2f} under {MIN_LOCATE:.2f})"
        parts = []
        if self.delta_e > MAX_DELTA_E:
            parts.append(f"colour dE {self.delta_e:.0f} over {MAX_DELTA_E:.0f}")
        if self.similarity < MIN_SIMILARITY:
            parts.append(f"structure {self.similarity:.2f} under {MIN_SIMILARITY:.2f}")
        return ", ".join(parts) or f"colour dE {self.delta_e:.0f}, structure {self.similarity:.2f}"

    def as_dict(self) -> dict:
        return {
            "frame": self.frame,
            "frame_file_id": self.frame_file_id,
            "similarity": self.similarity,
            "delta_e": self.delta_e,
            "box": [round(v, 4) for v in self.box],
            "match": self.match,
            "located": self.located,
        }

    @classmethod
    def from_dict(cls, item: dict) -> Measure:
        return cls(
            str(item["frame"]),
            str(item["frame_file_id"]) if item.get("frame_file_id") else None,
            float(item["similarity"]),
            float(item["delta_e"]),
            tuple(float(v) for v in item["box"]),  # type: ignore[arg-type]
            float(item["match"]),
        )


@dataclass(frozen=True)
class Drift:
    """A shot whose frames drifted from one of the video's references: the measures that did (``found``); or a shot
    none of whose frames shows the reference as it is: every measure, ``found`` false (advisory, never a failure)."""

    reference: dict
    measures: tuple[Measure, ...]
    found: bool = True

    @property
    def label(self) -> str:
        return str(self.reference.get("label") or self.reference.get("id") or "the reference")

    @property
    def worst(self) -> Measure:
        if not self.found:
            return max(self.measures, key=lambda m: m.match)
        return min(self.measures, key=lambda m: (-m.delta_e, m.similarity))

    def words(self) -> str:
        """``reference drift: the rug differs from the video's reference `rug` (first, last frames: colour dE 19 over
        14)``; not found: ``reference not seen: the rug is not seen in the shot as the video's reference `rug` shows
        it (first, middle, last frames: not seen as the reference shows it (match 0.66 under 0.74))``."""
        frames = ", ".join(m.frame for m in self.measures)
        reasons = "; ".join(dict.fromkeys(m.why() for m in self.measures))
        plural = "s" if len(self.measures) > 1 else ""
        if not self.found:
            return (
                f"reference not seen: {self.label} is not seen in the shot as the video's reference "
                f"`{self.reference.get('id')}` shows it ({frames} frame{plural}: {reasons})"
            )
        return (
            f"reference drift: {self.label} differs from the video's reference `{self.reference.get('id')}` "
            f"({frames} frame{plural}: {reasons})"
        )

    def crop_words(self) -> str:
        """The crop of the worst frame: ``crop: the first frame (file `id`) at x 0.31-0.52, y 0.40-0.71``."""
        worst = self.worst
        x0, y0, x1, y1 = worst.box
        file = f" (file `{worst.frame_file_id}`)" if worst.frame_file_id else ""
        return f"crop: the {worst.frame} frame{file} at x {x0:.2f}-{x1:.2f}, y {y0:.2f}-{y1:.2f}"

    def as_dict(self) -> dict:
        return {"reference": dict(self.reference), "measures": [m.as_dict() for m in self.measures], "found": self.found}


# ── Pure measures ─────────────────────────────────────────────────────────


def flattened(picture: Any) -> Any:
    """*picture* as RGB, its transparency laid over a neutral grey (a reference cut out of its scene)."""
    from PIL import Image

    if picture.mode in ("RGBA", "LA") or (picture.mode == "P" and "transparency" in picture.info):
        rgba = picture.convert("RGBA")
        ground = Image.new("RGBA", rgba.size, (_NEUTRAL, _NEUTRAL, _NEUTRAL, 255))
        return Image.alpha_composite(ground, rgba).convert("RGB")
    return picture.convert("RGB")


def _lab(rgb: Any) -> Any:
    """CIELAB L*, a*, b* of sRGB pixels (0-255, shape ``(n, 3)``), D65: shape ``(n, 3)``."""
    import numpy as np

    srgb = np.asarray(rgb, dtype=np.float64) / 255.0
    linear = np.where(srgb <= 0.04045, srgb / 12.92, ((srgb + 0.055) / 1.055) ** 2.4)
    matrix = np.array([[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750], [0.0193339, 0.1191920, 0.9503041]])
    xyz = linear @ matrix.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16.0 / 116.0)
    return np.stack([116.0 * f[:, 1] - 16.0, 500.0 * (f[:, 0] - f[:, 1]), 200.0 * (f[:, 1] - f[:, 2])], axis=1)


def _shrunk(picture: Any, side: int) -> Any:
    """*picture* flattened to RGB and brought to at most *side* on its longer side (box-averaged: its colours)."""
    from PIL import Image

    rgb = flattened(picture)
    scale = min(1.0, side / max(rgb.size))
    if scale < 1.0:
        rgb = rgb.resize((max(8, round(rgb.width * scale)), max(8, round(rgb.height * scale))), Image.Resampling.BOX)
    return rgb


def _integral(field: Any) -> Any:
    """The summed-area table of *field* (``H x W x C``): ``(H + 1) x (W + 1) x C``."""
    import numpy as np

    table = np.zeros((field.shape[0] + 1, field.shape[1] + 1, field.shape[2]))
    table[1:, 1:] = field.cumsum(axis=0).cumsum(axis=1)
    return table


def _summed(table: Any, x0: Any, y0: Any, x1: Any, y1: Any) -> Any:
    return table[y1, x1] - table[y0, x1] - table[y1, x0] + table[y0, x0]


def _cells(table: Any, x0: Any, y0: Any, x1: Any, y1: Any, count: int) -> Any:
    """The sums of *table* over each cell of a *count* x *count* grid on the boxes (``N``): ``N x count² x C``."""
    import numpy as np

    sums = []
    for row in range(count):
        for column in range(count):
            cx0, cx1 = x0 + ((x1 - x0) * column) // count, x0 + ((x1 - x0) * (column + 1)) // count
            cy0, cy1 = y0 + ((y1 - y0) * row) // count, y0 + ((y1 - y0) * (row + 1)) // count
            sums.append(_summed(table, cx0, cy0, cx1, cy1))
    return np.stack(sums, axis=-2)


class Fields:
    """A picture's per-pixel fields as summed-area tables, so any window of it is described in constant time:
    its colour class (neutral dark, neutral light, :data:`HUE_BINS` hue bands at two chroma levels), its edge
    orientations weighted by their strength (:data:`EDGE_BINS`), its lightness and its pixel count."""

    def __init__(self, picture: Any, side: int) -> None:
        import numpy as np
        from scipy import ndimage

        rgb = np.asarray(_shrunk(picture, side), dtype=np.float64)
        height, width, _ = rgb.shape
        lab = _lab(rgb.reshape(-1, 3)).reshape(height, width, 3)
        chroma = np.hypot(lab[..., 1], lab[..., 2])
        hue = (np.degrees(np.arctan2(lab[..., 2], lab[..., 1])) % 360.0 / 360.0 * HUE_BINS).astype(int) % HUE_BINS
        level = np.where(chroma < WEAK_CHROMA, 0, np.where(chroma < STRONG_CHROMA, 1, 2))
        index = np.where(level == 0, np.where(lab[..., 0] < DARK, 0, 1), 2 + (level - 1) * HUE_BINS + hue)
        rows, columns = np.arange(height)[:, None], np.arange(width)[None, :]
        colour = np.zeros((height, width, 2 + 2 * HUE_BINS))
        colour[rows, columns, index] = 1.0
        light = ndimage.gaussian_filter(lab[..., 0], 1.0)
        gx, gy = ndimage.sobel(light, axis=1) / 8.0, ndimage.sobel(light, axis=0) / 8.0
        direction = np.minimum(((np.arctan2(gy, gx) % np.pi) / np.pi * EDGE_BINS).astype(int), EDGE_BINS - 1)
        edges = np.zeros((height, width, EDGE_BINS))
        edges[rows, columns, direction] = np.hypot(gx, gy)
        self.size = (width, height)
        self.colour, self.edges = _integral(colour), _integral(edges)
        self.light, self.count = _integral(lab[..., 0:1]), _integral(np.ones((height, width, 1)))

    def describe(self, x0: Any, y0: Any, x1: Any, y1: Any) -> tuple[Any, Any, Any]:
        """The windows ``(x0, y0, x1, y1)`` (pixel arrays, ``N`` each) as unit vectors: their colour histograms
        (Hellinger, :data:`COLOUR_CELLS` cells), edge-orientation layouts (:data:`EDGE_CELLS` cells) and
        lightness layouts (:data:`LIGHT_CELLS` cells, the mean taken out)."""
        import numpy as np

        x0, y0, x1, y1 = (np.asarray(v, dtype=int).reshape(-1) for v in (x0, y0, x1, y1))
        colour = _cells(self.colour, x0, y0, x1, y1, COLOUR_CELLS)
        colour = np.sqrt(colour / np.maximum(colour.sum(axis=-1, keepdims=True), 1e-9)).reshape(len(x0), -1) / COLOUR_CELLS
        edges = _cells(self.edges, x0, y0, x1, y1, EDGE_CELLS).reshape(len(x0), -1)
        edges = edges / np.maximum(np.linalg.norm(edges, axis=-1, keepdims=True), 1e-9)
        counted = np.maximum(_cells(self.count, x0, y0, x1, y1, LIGHT_CELLS)[..., 0], 1.0)
        light = _cells(self.light, x0, y0, x1, y1, LIGHT_CELLS)[..., 0] / counted
        light = light - light.mean(axis=-1, keepdims=True)
        light = light / np.maximum(np.linalg.norm(light, axis=-1, keepdims=True), 1e-9)
        return colour, edges, light


def describe(picture: Any) -> tuple[Any, Any, Any]:
    """*picture* whole (a reference, a crop) as :meth:`Fields.describe` gives one window: colour, edges, light."""
    fields = Fields(picture, DESCRIBE_SIDE)
    width, height = fields.size
    return tuple(part[0] for part in fields.describe(0, 0, width, height))  # type: ignore[return-value]


def _overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """The intersection over union of two boxes."""
    across = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    down = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    shared = across * down
    return shared / max((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - shared, 1e-9)


def candidates(reference: Any, frame: Any) -> list[tuple[float, float, tuple[float, float, float, float]]]:
    """Where *reference* may show in *frame*: the :data:`FIND_CANDIDATES` windows (at :data:`FIND_SCALES` widths,
    :data:`FIND_ASPECTS` shapes, :data:`FIND_STRIDE` steps) nearest its description, distinct (under 0.3 overlap),
    the best first: ``(match, edge match, (x0, y0, x1, y1))``, the box as shares of the frame. Robust to the
    prop's angle and scale: the description reads what a window holds, not where each pixel is."""
    import numpy as np

    colour_ref, edges_ref, light_ref = describe(reference)
    fields = Fields(frame, FIND_SIDE)
    width, height = fields.size
    aspect = reference.height / max(reference.width, 1)
    found: list[tuple[float, float, tuple[float, float, float, float]]] = []
    for share in np.geomspace(FIND_MIN_SHARE, FIND_MAX_SHARE, FIND_SCALES):
        for factor in FIND_ASPECTS:
            w = round(share * width)
            h = round(w * aspect * factor)
            if w < 8 or h < 8 or w >= width or h >= height:
                continue
            xs = np.arange(0, width - w + 1, max(1, w // FIND_STRIDE))
            ys = np.arange(0, height - h + 1, max(1, h // FIND_STRIDE))
            grid_x, grid_y = np.meshgrid(xs, ys)
            x0, y0 = grid_x.ravel(), grid_y.ravel()
            colour, edges, light = fields.describe(x0, y0, x0 + w, y0 + h)
            edge_match = edges @ edges_ref
            score = (WEIGHTS[0] * (colour @ colour_ref) + WEIGHTS[1] * edge_match + WEIGHTS[2] * (light @ light_ref)) / sum(WEIGHTS)
            for index in np.argsort(score)[::-1][:3]:
                box = (x0[index] / width, y0[index] / height, (x0[index] + w) / width, (y0[index] + h) / height)
                found.append((float(score[index]), float(edge_match[index]), tuple(float(v) for v in box)))  # type: ignore[arg-type]
    found.sort(reverse=True)
    kept: list[tuple[float, float, tuple[float, float, float, float]]] = []
    for item in found:
        if all(_overlap(item[2], other[2]) < 0.3 for other in kept):
            kept.append(item)
        if len(kept) >= FIND_CANDIDATES:
            break
    return kept


def structure(reference: Any, crop: Any) -> float:
    """How much of *reference*'s edge layout *crop* keeps, 0-1: the cosine of their edge-orientation histograms
    over :data:`STRUCTURE_CELLS` cells (read at :data:`STRUCTURE_SIDE`, each cell normalised)."""
    import numpy as np
    from scipy import ndimage

    def layout(picture: Any) -> Any:
        rgb = np.asarray(_shrunk(picture, STRUCTURE_SIDE), dtype=np.float64)
        light = ndimage.gaussian_filter(_lab(rgb.reshape(-1, 3))[:, 0].reshape(rgb.shape[0], rgb.shape[1]), 1.0)
        gx, gy = ndimage.sobel(light, axis=1) / 8.0, ndimage.sobel(light, axis=0) / 8.0
        magnitude = np.hypot(gx, gy)
        direction = np.minimum(((np.arctan2(gy, gx) % np.pi) / np.pi * EDGE_BINS).astype(int), EDGE_BINS - 1)
        height, width = light.shape
        cells = []
        for row in range(STRUCTURE_CELLS):
            for column in range(STRUCTURE_CELLS):
                part = (
                    slice(height * row // STRUCTURE_CELLS, height * (row + 1) // STRUCTURE_CELLS),
                    slice(width * column // STRUCTURE_CELLS, width * (column + 1) // STRUCTURE_CELLS),
                )
                histogram = np.bincount(direction[part].ravel(), weights=magnitude[part].ravel(), minlength=EDGE_BINS)
                cells.append(histogram / (np.linalg.norm(histogram) + 1e-9))
        vector = np.concatenate(cells)
        return vector / (np.linalg.norm(vector) + 1e-9)

    return float(layout(reference) @ layout(crop))


def accents(picture: Any, min_share: float = MIN_ACCENT_SHARE) -> list[tuple[float, float, float]]:
    """The accent colours of *picture* as ``(share, a*, b*)``, the largest first: the peaks of its hue histogram
    (:data:`ACCENT_BINS` bands, a peak with its two neighbours) at each of :data:`ACCENT_LEVELS` chroma levels,
    those under *min_share* of its pixels left out. Neutral pixels (the navy of a rug, a grey floor) are no
    accent: what is read is the piping, the emblem, the bandana."""
    import numpy as np

    lab = _lab(np.asarray(_shrunk(picture, ACCENT_SIDE)).reshape(-1, 3))
    chroma = np.hypot(lab[:, 1], lab[:, 2])
    total = len(lab)
    found = []
    for low, high in ACCENT_LEVELS:
        strong = lab[(chroma >= low) & (chroma < high)]
        if len(strong) == 0:
            continue
        hue = np.degrees(np.arctan2(strong[:, 2], strong[:, 1])) % 360.0
        bins = (hue / 360.0 * ACCENT_BINS).astype(int) % ACCENT_BINS
        counts = np.bincount(bins, minlength=ACCENT_BINS)
        for index in range(ACCENT_BINS):
            before, after = (index - 1) % ACCENT_BINS, (index + 1) % ACCENT_BINS
            if counts[index] >= counts[before] and counts[index] > counts[after]:
                chosen = (bins == index) | (bins == before) | (bins == after)
                share = float(chosen.sum()) / total
                if share >= min_share:
                    found.append((share, float(strong[chosen, 1].mean()), float(strong[chosen, 2].mean())))
    return sorted(found, reverse=True)


def colour_distance(reference: list[tuple[float, float, float]], crop: list[tuple[float, float, float]]) -> float:
    """The farthest an accent of *reference* stands from the nearest accent of *crop*, in a*, b*, the crop's accent
    brought to the reference's chroma first: a paler or deeper shade of the same hue is the same colour."""
    if not reference or not crop:
        return 0.0
    farthest = 0.0
    for _share, a, b in reference:
        chroma = math.hypot(a, b)
        nearest = math.inf
        for _crop_share, ca, cb in crop:
            crop_chroma = math.hypot(ca, cb) or 1e-9
            nearest = min(nearest, math.hypot(a - ca * chroma / crop_chroma, b - cb * chroma / crop_chroma))
        farthest = max(farthest, nearest)
    return farthest


def _crop(frame: Any, box: tuple[float, float, float, float]) -> Any:
    width, height = frame.size
    x0, y0 = round(box[0] * width), round(box[1] * height)
    return frame.crop((x0, y0, max(round(box[2] * width), x0 + 1), max(round(box[3] * height), y0 + 1)))


def locate(reference: Any, frame: Any) -> tuple[float, float, float, float] | None:
    """Where *reference* shows in *frame*, ``(x0, y0, x1, y1)`` as shares of it; ``None`` when not found
    (:func:`compare` reads the match and the colour of every candidate; this is the box alone)."""
    measure = compare(reference, frame)
    return measure.box if measure.located else None


def compare(reference: Any, frame: Any, *, name: str = "first", frame_file_id: str | None = None) -> Measure:
    """*frame* read against *reference* (Pillow images): among the windows nearest the reference's description
    (:func:`candidates`, those within :data:`LOCATE_TIE` of the best), the one nearest it in colour; its match,
    structure and colour distance. Under :data:`MIN_LOCATE` the reference is not seen, and the measure says so."""
    picture = flattened(reference)
    shown = flattened(frame)
    found = candidates(picture, shown)
    if not found:
        return Measure(name, frame_file_id, 0.0, 0.0, (0.0, 0.0, 1.0, 1.0), 0.0)
    reference_accents = accents(picture)
    best = found[0][0]
    read = []
    for match, _structure, box in found:
        if match < best - LOCATE_TIE:
            break
        crop = _crop(shown, box)
        read.append((colour_distance(reference_accents, accents(crop, MIN_CROP_ACCENT_SHARE)), -match, box, crop))
    # The best window says whether the prop is seen; the one nearest in colour among its ties is what is judged.
    delta_e, _negative_match, box, crop = min(read, key=lambda item: item[:2])
    similarity = structure(picture, crop)
    return Measure(name, frame_file_id, round(float(similarity), 4), round(float(delta_e), 2), box, round(float(best), 4))


def judge(reference: dict, measures: list[Measure]) -> Drift | None:
    """The drift of a shot from *reference*, from its frames' *measures*; ``None`` when every frame holds. A
    reference seen in none of the frames is a :class:`Drift` with ``found`` false: an advisory, not a drift."""
    if measures and not any(m.located for m in measures):
        return Drift(reference, tuple(measures), found=False)
    drifted = tuple(m for m in measures if m.drifted)
    return Drift(reference, drifted) if drifted else None


# ── The files ─────────────────────────────────────────────────────────────


def cache_digest(reference_file_id: str, frame_file_ids: list[str]) -> str:
    text = f"{MEASURE_VERSION}|{MIN_LOCATE}|{MIN_SIMILARITY}|{MAX_DELTA_E}|{reference_file_id}|{','.join(frame_file_ids)}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def measure(reference: dict, frame_file_ids: dict[str, str], workdir: str) -> list[Measure] | None:
    """The shot's kept frames (``params name -> file id``) read against *reference* (``{id, file_id, ...}``).

    Read back from ``render_cache`` when measured before, else measured and kept. ``None`` when a file is gone or
    cannot be read: a check that cannot read says nothing (logged).
    """
    from pathlib import Path

    from app.services.video_studio import frame_qc, render_cache

    names = [name for name, _at in frame_qc.FRAMES if frame_file_ids.get(name)]
    if not names or not reference.get("file_id"):
        return None
    digest = cache_digest(str(reference["file_id"]), [str(frame_file_ids[name]) for name in names])
    kept = await render_cache.get_json(CACHE_KIND, digest)
    if isinstance(kept, list):
        try:
            return [Measure.from_dict(item) for item in kept]
        except (KeyError, TypeError, ValueError):
            pass
    try:
        from app.services.video_studio import assets

        folder = Path(workdir)
        target = folder / f"reference-{digest[:12]}"
        if await assets.download_file(reference["file_id"], str(target)) is None:
            return None
        picture = frame_qc.open_picture(target.read_bytes())
        measures: list[Measure] = []
        for name in names:
            path = folder / f"frame-{digest[:12]}-{name.split('_')[0]}"
            if await assets.download_file(frame_file_ids[name], str(path)) is None:
                return None
            frame = frame_qc.open_picture(path.read_bytes())
            measures.append(compare(picture, frame, name=FRAME_WORDS.get(name, name), frame_file_id=str(frame_file_ids[name])))
    except Exception as exc:  # advisory: a shot is never judged on a reading that failed
        logger.info("Reference drift of a shot not measured: %s", format_exception_message(exc))
        return None
    await render_cache.put_json(CACHE_KIND, digest, [m.as_dict() for m in measures])
    return measures


async def drifts_of(references: list[dict], frame_file_ids: dict[str, str], workdir: str) -> list[Drift]:
    """The drifts of one shot's kept frames from each of *references* (:func:`measure` then :func:`judge`)."""
    found: list[Drift] = []
    for reference in references:
        measures = await measure(reference, frame_file_ids, workdir)
        if measures and (drift := judge(reference, measures)) is not None:
            found.append(drift)
    return found


def frames_from_paths(paths: dict[str, Any]) -> dict[str, Any]:
    """The kept frames of a take just made (``frame_qc.cut_frames``: ``params name -> path``), opened."""
    from pathlib import Path

    from app.services.video_studio import frame_qc

    return {name: frame_qc.open_picture(Path(path).read_bytes()) for name, path in paths.items()}


def drifts_from_frames(references: list[dict], pictures: dict[str, Any], reference_pictures: dict[str, Any]) -> list[Drift]:
    """The drifts of a take's frames (``params name -> Pillow image``) from *references*, their pictures opened by id
    (``reference id -> Pillow image``): the worker's check before a take is accepted (``runners.run_clip``)."""
    found: list[Drift] = []
    for reference in references:
        picture = reference_pictures.get(str(reference.get("id")))
        if picture is None:
            continue
        measures = [compare(picture, frame, name=FRAME_WORDS.get(name, name)) for name, frame in pictures.items()]
        if (drift := judge(reference, measures)) is not None:
            found.append(drift)
    return found


def failures(drifts: list[Drift]) -> list[Drift]:
    """The drifts that fail a take or a save: those whose reference was found (``found``); an advisory is left out."""
    return [drift for drift in drifts if drift.found]


def take_reason(drifts: list[Drift]) -> str:
    """Why a take failed its consistency check, as ``take_failed`` records it (the found drifts only)."""
    said = "; ".join(drift.words() for drift in failures(drifts))
    return (
        f"{said} - generate it again in place (clip_id) with a prompt naming what must stay the same, or drop the "
        "reference from consistency."
    )
