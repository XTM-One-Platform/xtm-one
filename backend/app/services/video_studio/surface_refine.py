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

"""The surface a screen insert is mapped onto, snapped to the bezel the footage shows.

A ``screen_insert`` quad - given in the spec, or followed by the planar
tracker (``insert_tracking``) - is where its author or the tracker put the
surface, a few pixels off the bezel the video model drew: drawn there, the
insert is keystoned against the bezel on every frame, and a static quad on
a moving camera slides off it. This module reads the bezel itself:

* **The seed.** The quad is a seed. On each footage frame, each of its four
  edges is looked for in a band :data:`BAND` of the frame's width either side
  of the seed's edge: along the edge, at :data:`SAMPLES_PER_EDGE` points, the
  field (the footage's luma, or a keyed surface's matte) is read along the
  edge's normal, and of the gradient peaks in the band within
  :data:`PEAK_SHARE` of the strongest, the one with the surface on its inward
  side - its inward reading the surface's own, read deeper in - is a point
  of the surface's edge: the inner edge of a bezel wins over its outer one
  whichever the seed is nearer to.
* **The lines.** Each edge's points are fitted to the line most of them
  agree on (every pair proposes one; a reflection, a hand or a person across
  the edge agrees with none and is left out), the four lines intersected:
  the refined quad. An edge with too few points keeps the seed's line, so a
  surface partly out of frame keeps its true corners outside the frame
  (never squeezed to its edge).
* **The lock.** :class:`Refiner` carries the correction found on one frame
  to the next as its seed: as the camera moves, the lock follows the bezel
  (re-seeded on every frame) while the alignment error - the GIVEN quad
  against the detected bezel, a share of the frame's width - says how far
  the quad is from where the surface is. Every :data:`RESEED_EVERY` frames
  the given quad is tried as the seed again, so a lock on a static camera
  never drifts from the quad it was given.
* **The prior.** On an untracked quad, whose bezel cannot move, a
  frame's correction moving a corner more than :data:`PRIOR_MAX_SHARE` of the
  frame's width from the median of the last :data:`PRIOR_FRAMES` kept is an
  inner edge the refiner caught - a text line painted on the surface - and
  the median is drawn instead, unless :data:`PRIOR_SETTLE_FRAMES` frames in a
  row agree on it: then the bezel moved, and the lock follows. The error of
  the bezel it rejected is kept (``Refined.found_error``): the gate reads it,
  so a bezel that really stepped is never hidden while the prior settles.

The alignment error above :data:`ALIGNMENT_MAX_SHARE` - its mean, or one
side's - on :data:`ALIGNMENT_MIN_RUN` consecutive frames
(:func:`alignment_failing`) is a failing finding of the render
(``insert_checks``, ``insert_alignment``): the audience would see the insert
off its bezel, refined or not. A few frames past it, or frames scattered, are
not.

* **What stands in front.** A reading that crosses what stands in
  front of the surface (*excluded*: ``screen_insert.edge_front``, the
  foreground grown as every gate grows it) gives no point, so neither the
  lock nor the flatness ever fits a subject's silhouette as the bezel. The
  share of an edge's readings left is its visibility (:attr:`Refined.visible`):
  under :data:`MIN_EDGE_VISIBLE`, the edge is mostly covered - its line is the
  seed's and its bow is not judged on that frame, said as such.
* **The flatness.** The points found along each
  visible edge also say how far it bows (a parabola fitted across them,
  :attr:`Refined.bows`), how many of them lie on the line most agree on
  (:attr:`Refined.inliers`) and how far they stand off one straight line
  (:attr:`Refined.residuals`, the alignment residual of that edge). A bow is
  confirmed only by all three together - past :data:`FLAT_MAX_SHARE`, under
  :data:`BOW_MAX_INLIERS` of its points on one line, and its residual past
  :data:`ALIGNMENT_MAX_SHARE` of the frame's width -, never by one alone: a
  bow read on a few points is a line of what the screen shows, a low inlier
  share alone is a silhouette crossing the edge. A curved surface - or one
  whose quad is not convex - no planar insert covers: only a shot made
  again with a flat screen. Opposite sides of unequal length are a flat
  surface seen at an angle, which the eight-coefficient perspective warp maps
  any convex planar quad onto exactly: said in degrees (:func:`viewing_angle`,
  :meth:`Flatness.seen_at`), never failed. :func:`measure_bezel` is
  what a save, the pre-render lint and a shot's checks read: the bezel a frame
  shows, its flatness and, for a quad given, how far that quad is from it -
  keyed and masked exactly as the render's lock reads a frame, so the two
  stages measure one thing.
* **Persistence.** A frame-by-frame gate fails on
  :data:`PERSIST_FRAMES` consecutive frames (:func:`persistent`), or on every
  frame of an insert drawn on fewer: one weak frame never blocks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

#: The band searched either side of the seed's edge, as a share of the frame's width (a quad up to
#: :data:`MAX_CORNER_BANDS` bands off its bezel is measured and snapped; one further off is not found) ...
BAND = 0.03
#: ... and at least this many pixels.
MIN_BAND_PX = 6.0
#: Points along each edge where the edge is looked for ...
SAMPLES_PER_EDGE = 24
#: ... leaving this share of the edge out at both ends: the corners are where two edges blur into each other.
EDGE_INSET = 0.08
#: The step along the normal the field is read at (pixels).
STEP = 0.5
#: The profile along the normal is smoothed by this (pixels) before its gradient is read.
SMOOTH_SIGMA = 1.0
#: A gradient under this (grey levels per pixel) is no edge: a featureless band gives no point.
MIN_GRADIENT = 3.0
#: A peak this share of the band's strongest counts; of those, the one with the surface on its inward side wins ...
PEAK_SHARE = 0.5
#: ... and peaks whose inward reading is within this share of their step of the best one's are equals, the nearest to
#: the seed winning: a keyed surface's green running past a thin bezel reads as the surface on both edges.
PEAK_EQUAL = 0.15
#: How far (pixels) either side of a peak the field is read to tell the surface's side from the bezel's.
SIDE_PX = 2.5
#: Fewer points than this agreeing on a line: the edge keeps the seed's line ...
MIN_POINTS = 8
#: ... agreeing within this many pixels of it (a person across the edge agrees with none), on a frame up to
#: :data:`REFERENCE_WIDTH` wide; on a wider one, in proportion: a 2160p frame's blurred edge is twice as wide,
#: and its points agree with the line as a 1080p one's do (:func:`inlier_px`).
INLIER_PX = 1.25
REFERENCE_WIDTH = 1920.0
#: An edge whose readings cross what stands in front on more than this share of them is mostly covered:
#: its line is the seed's, and its bow is not judged on that frame.
MIN_EDGE_VISIBLE = 0.4
#: A keyed surface's side whose readings show its bezel on less than this share of them is hidden behind what stands
#: in front: its line stops at the occluder's edge, not the screen's. :func:`hidden_sides` places it at the key colour's
#: extent past it - the screen continuing behind - never at the occluder (dev:screen-insert-occluded-edge-quad) ...
HIDDEN_EDGE_VISIBLE = 0.2
#: ... moved out no farther than this share of the quad's span across it (a key-coloured wall joined to the screen is
#: no part of it) ...
HIDDEN_REACH = 0.25
#: ... and only past this many pixels of a 1080p frame: closer, the side found is the screen's.
HIDDEN_MIN_PX = 2.0
#: A fitted line turned more than this (degrees) from the seed's edge is not that edge.
MAX_TURN_DEGREES = 12.0
#: The refined quad's area stays within this share of the seed's, and each corner within this many bands of its seed.
MAX_AREA_CHANGE = 0.15
MAX_CORNER_BANDS = 1.5
#: The given quad is tried as the seed again every this many frames (a lock never drifts on a still camera).
RESEED_EVERY = 24
#: The gate: the given quad to the detected bezel, mean over the corners, as a share of the frame's width.
ALIGNMENT_MAX_SHARE = 0.005
#: The temporal prior: a frame's refinement - its correction of the given quad - whose corner moves more than
#: this share of the frame's width from the median of the last :data:`PRIOR_FRAMES` refinements kept is an inner
#: edge the refiner caught (a line painted on the surface), rejected for that median ...
PRIOR_MAX_SHARE = 0.005
PRIOR_FRAMES = 15
#: ... unless this many frames in a row are rejected and agree with each other: the bezel moved, the lock follows.
PRIOR_SETTLE_FRAMES = 5
#: A tracked quad drawn steady (``step(steady=True)``): its corners follow the bezel found through an alpha-beta
#: filter, a constant motion predicted from the frames before and this share of the bezel's distance from it taken
#: on each frame - a read a pixel off from one frame to the next moves the quad by about a third of it, under the
#: jitter gate (``insert_checks.JITTER_MAX_SHARE``), a real jump of a few pixels still steps past it ...
STEADY_WEIGHT = 0.35
#: ... and this share of it taken into the motion, so a steady drift is followed with no lag.
STEADY_MOTION_WEIGHT = 0.06
#: A frame-by-frame gate (the alignment, the flatness) fails on this many consecutive frames past it:
#: one or a few frames an inner edge or a passing silhouette caught are never the surface.
PERSIST_FRAMES = 5
ALIGNMENT_MIN_RUN = PERSIST_FRAMES
#: A surface one of whose edges bows by more than this share of its length (its sagitta) is not flat,
#: its points confirming it (:meth:`Flatness.confirmed`): a planar insert cannot cover it. Opposite sides of
#: unequal length are a flat surface seen at an angle, said.
FLAT_MAX_SHARE = 0.05
#: A flat surface turned this many degrees or more from the camera is said so, as a fact ...
ANGLE_FACT_DEGREES = 10.0
#: ... and one whose angle was not read (a record older than it) when its opposite sides differ by more than this share.
ANGLE_FACT_SHARE = 0.05
#: Its angle is read with the focal length its two vanishing points give, or this one (in frame widths: a 36 mm lens on
#: a full-frame sensor, the field a video model frames a room with) when one of them is at infinity ...
ASSUMED_FOCAL = 1.0
#: ... and a focal length they give outside this range (frame widths) is noise: the assumed one stands.
FOCAL_RANGE = (0.3, 5.0)
#: A bow is the edge's own only when, besides passing :data:`FLAT_MAX_SHARE`, fewer than this share of its visible
#: points lie on the line most agree on AND they stand off one straight line by more than
#: :data:`ALIGNMENT_MAX_SHARE` of the frame's width (median): a bow most points deny is a line of the
#: screen's content caught on a few of them, and a low share alone is a silhouette crossing a straight edge.
BOW_MAX_INLIERS = 0.7
#: The sides of a quad, corner to corner (top left, top right, bottom right, bottom left), and their names.
SIDES = ("top", "right", "bottom", "left")


def inlier_px(band: float) -> float:
    """How close (pixels) a point stands to its edge's line to agree with it, for a search *band* that wide:
    :data:`INLIER_PX` up to a :data:`REFERENCE_WIDTH`-wide frame, in proportion past it."""
    return max(INLIER_PX, INLIER_PX * float(band) / (BAND * REFERENCE_WIDTH))


def longest_run(frames: list[int]) -> int:
    """The longest run of consecutive numbers in *frames*."""
    ordered = sorted(set(frames))
    longest = run = 0
    for index, frame in enumerate(ordered):
        run = run + 1 if index and frame == ordered[index - 1] + 1 else 1
        longest = max(longest, run)
    return longest


def persistent(failing: list[int], frames: int) -> bool:
    """Whether a frame-by-frame gate failing on the frames *failing* - of an insert's *frames* drawn - fails it:
    :data:`PERSIST_FRAMES` consecutive frames, or every frame of an insert drawn on fewer (a save's one frame)."""
    if not failing:
        return False
    return longest_run(failing) >= min(PERSIST_FRAMES, max(int(frames), 1))


def alignment_failing(off: list[int], frames: int) -> bool:
    """Whether the frames *off* - those past :data:`ALIGNMENT_MAX_SHARE` (mean or one side), of an insert's *frames*
    drawn - fail it (:func:`persistent`): one or a few frames an inner edge caught never do."""
    return persistent(off, frames)


@dataclass(frozen=True)
class Refined:
    """A quad snapped to the bezel: its corners (pixels), how far the seed was from them, what was found."""

    quad: Any
    #: Mean distance (pixels) between the seed's corners and the refined ones, over the corners whose two edges
    #: were found (:attr:`measured`); ``None`` when fewer than two corners were.
    seed_error: float | None
    #: Which of the four edges were found on the footage (top, right, bottom, left).
    edges_found: tuple[bool, bool, bool, bool] = (False, False, False, False)
    #: The temporal prior rejected this frame's refinement and kept the median of the last frames ...
    held: bool = False
    #: ... and the given quad's error against the bezel it rejected, which the gate still reads; ``None`` unheld.
    found_error: float | None = None
    #: How far each edge's points bow off a straight line, a share of its length (:func:`_bow`); ``None`` for
    #: an edge with too few points to tell.
    bows: tuple[float | None, float | None, float | None, float | None] = (None, None, None, None)
    #: The share of each edge's points on the line most of them agree on, and how far they stand off one straight line
    #: through them all (pixels, median) - what confirms a bow (:class:`EdgeFit`); ``None``: too few points.
    inliers: tuple[float | None, float | None, float | None, float | None] = (None, None, None, None)
    residuals: tuple[float | None, float | None, float | None, float | None] = (None, None, None, None)
    #: The share of each edge's readings clear of what stands in front; ``None``: not read.
    visible: tuple[float | None, float | None, float | None, float | None] = (None, None, None, None)

    @property
    def edges(self) -> int:
        return sum(self.edges_found)

    @property
    def found(self) -> bool:
        return self.edges >= 2

    @property
    def measured(self) -> tuple[int, ...]:
        """The corners whose two edges were found: the ones the bezel placed, which an alignment error is read on."""
        return tuple(index for index in range(4) if self.edges_found[(index - 1) % 4] and self.edges_found[index])


def _bow(points: Any, a: Any, direction: Any, length: float) -> float | None:
    """How far the edge *points* bow off a straight line, a share of the edge's *length*; ``None``: too few.

    A parabola across the edge (``v = c u^2 + b u + a``, *u* along *direction* from *a*, *v* across it) is fitted
    to the points, those far off it (a hand, a reflection) dropped and fitted again: its sagitta over the whole
    edge, ``|c| length^2 / 4``, is how far a curved edge stands off its chord. A straight edge gives none.
    """
    import numpy as np

    from app.services.video_studio.screens import edge_parabola

    if len(points) < MIN_POINTS or length < 4.0:
        return None
    rel = np.asarray(points, dtype=float) - np.asarray(a, dtype=float)
    u = rel @ direction
    v = rel @ np.array([-direction[1], direction[0]])
    if float(u.max() - u.min()) < length / 4:
        return None
    coefficients = edge_parabola(u, v, min_points=MIN_POINTS)
    if coefficients is None:
        return None
    return round(abs(float(coefficients[0])) * length / 4.0, 5)


@dataclass(frozen=True)
class EdgeFit:
    """One edge of the surface as its points found it (:func:`_edge_line`)."""

    #: ``(point, direction)`` of the line most points agree on; ``None``: no line.
    line: tuple[Any, Any] | None = None
    #: How far the points bow, a share of the edge's length (:func:`_bow`).
    bow: float | None = None
    #: The share of the points within :func:`inlier_px` of that line: a curved edge leaves most of them off it.
    inliers: float | None = None
    #: The points' median distance (pixels) from one straight line fitted through them all.
    residual: float | None = None
    #: The share of its readings that cross nothing standing in front: under :data:`MIN_EDGE_VISIBLE`, the
    #: edge is mostly covered - no line, no shape read.
    visible: float | None = None


def _edge_line(field: Any, a: Any, b: Any, inward: Any, band: float, excluded: Any = None) -> EdgeFit:
    """The :class:`EdgeFit` of the surface's edge near the seed edge ``a -> b`` of *field*: its line, how far the points
    found along it bow and how many of them that line holds.

    *inward* is the edge's normal pointing into the quad. A reading that crosses *excluded* (a float mask of
    *field*'s shape, what stands in front of the surface) gives no point: an occluder's own edge is never the bezel's.
    Fewer than :data:`MIN_EDGE_VISIBLE` of the readings left: the edge is mostly covered, nothing is fitted.
    """
    import numpy as np
    from scipy import ndimage

    from app.services.video_studio.tracking import sample

    length = float(np.linalg.norm(b - a))
    if length < 4.0:
        return EdgeFit()
    direction = (b - a) / length
    along = np.linspace(EDGE_INSET, 1.0 - EDGE_INSET, SAMPLES_PER_EDGE)
    # The band either side of the seed, and past it inward: what the surface itself reads as, deeper in.
    offsets = np.arange(-band, 2 * band + STEP / 2, STEP, dtype=np.float32)
    searched = int(np.searchsorted(offsets, band + 1e-6))
    bases = a[None, :] + along[:, None] * (b - a)[None, :]
    positions = bases[:, None, :] + offsets[None, :, None] * inward[None, None, :]
    profile = sample(field, positions[..., 0], positions[..., 1])
    blocked = np.zeros(SAMPLES_PER_EDGE, dtype=bool)
    if excluded is not None:
        blocked = (sample(excluded, positions[..., 0], positions[..., 1])[:, :searched] > 0.5).any(axis=1)
    visible = round(1.0 - float(blocked.mean()), 3)
    if visible < MIN_EDGE_VISIBLE:
        return EdgeFit(visible=visible)
    smoothed = ndimage.gaussian_filter1d(profile, SMOOTH_SIGMA / STEP, axis=1, mode="nearest")
    gradient = np.abs(np.gradient(smoothed, STEP, axis=1))
    side = max(2, round(SIDE_PX / STEP))
    # What the surface reads as, deeper in past the band, over the edge's readings that cross no occluder: a person
    # standing inside the surface near one part of the edge is in the readings left out, never in this median.
    clear = ~blocked if (~blocked).any() else np.ones_like(blocked)
    deep = float(np.median(smoothed[clear, searched:]))
    points = []
    for row, strength in enumerate(gradient[:, :searched]):
        strongest = float(strength.max())
        if blocked[row] or strongest < MIN_GRADIENT:
            continue
        # The local maxima of the gradient that count: the bezel's two edges, a line of the surface's own.
        peaks = np.nonzero((strength >= PEAK_SHARE * strongest) & (strength >= np.roll(strength, 1)) & (strength > np.roll(strength, -1)))[0]
        peaks = peaks[(peaks > 0) & (peaks < len(strength) - 1)]
        if len(peaks) == 0:
            continue
        # The surface's edge is the one with the surface on its inward side: the peak whose inward reading is the
        # surface's own, the nearest to the seed among equals (within PEAK_EQUAL of the best).
        line = smoothed[row]
        scores = []
        for index in peaks:
            inner = float(line[min(index + side, len(line) - 1)])
            outer = float(line[max(index - side, 0)])
            step = max(abs(inner - outer), 1e-6)
            scores.append(abs(inner - deep) / step)
        best_score = min(scores)
        equals = [k for k in range(len(peaks)) if scores[k] <= best_score + PEAK_EQUAL]
        index = int(peaks[min(equals, key=lambda k: abs(float(offsets[peaks[k]])))])
        # The peak's position to a fraction of a step: a parabola through it and its neighbours.
        left, middle, right = strength[index - 1], strength[index], strength[index + 1]
        denominator = left - 2 * middle + right
        shift = 0.5 * (left - right) / denominator if abs(denominator) > 1e-9 else 0.0
        where = offsets[index] + float(np.clip(shift, -1.0, 1.0)) * STEP
        points.append(bases[row] + where * inward)
    if len(points) < MIN_POINTS:
        return EdgeFit(visible=visible)
    found = np.asarray(points, dtype=float)
    bow = _bow(found, a, direction, length)
    # How far the points stand off one straight line through them all: a curved edge's points spread off any line,
    # a straight edge's few points on a content line inside the bezel move the median of them nowhere.
    centre = found.mean(axis=0)
    _, _, through = np.linalg.svd(found - centre, full_matrices=False)
    residual = round(float(np.median(np.abs((found - centre) @ np.array([-through[0][1], through[0][0]])))), 3)
    # The line most points agree on (a hand, a person across the edge is a minority that agrees on none): every
    # pair of points proposes one, the one with the most points within inlier_px wins and is refitted on them.
    agreeing = inlier_px(band)
    best: Any = None
    for i in range(len(found)):
        for j in range(i + 1, len(found)):
            along_pair = found[j] - found[i]
            length_pair = float(np.linalg.norm(along_pair))
            if length_pair < 1e-6:
                continue
            unit = along_pair / length_pair
            normal = np.array([-unit[1], unit[0]])
            agree = np.abs((found - found[i]) @ normal) < agreeing
            count = int(agree.sum())
            if best is None or count > best[0]:
                best = (count, agree)
    inliers = round(best[0] / len(found), 3) if best is not None else 0.0
    if best is None or best[0] < MIN_POINTS:
        return EdgeFit(None, bow, inliers, residual, visible)
    chosen = found[best[1]]
    mean = chosen.mean(axis=0)
    _, _, vt = np.linalg.svd(chosen - mean, full_matrices=False)
    fitted = vt[0] if vt[0] @ direction >= 0 else -vt[0]
    turn = float(np.degrees(np.arccos(np.clip(fitted @ direction, -1.0, 1.0))))
    if turn > MAX_TURN_DEGREES:
        return EdgeFit(None, bow, inliers, residual, visible)
    return EdgeFit((mean, fitted), bow, inliers, residual, visible)


def _intersect(p1: Any, d1: Any, p2: Any, d2: Any) -> Any | None:
    import numpy as np

    matrix = np.array([[d1[0], -d2[0]], [d1[1], -d2[1]]], dtype=float)
    if abs(np.linalg.det(matrix)) < 1e-9:
        return None
    s, _t = np.linalg.solve(matrix, np.asarray(p2, float) - np.asarray(p1, float))
    return np.asarray(p1, float) + s * np.asarray(d1, float)


def _area(quad: Any) -> float:
    import numpy as np

    points = np.asarray(quad, dtype=float)
    x, y = points[:, 0], points[:, 1]
    return float(0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y)))


def refine(field: Any, seed: Any, band: float, excluded: Any = None) -> Refined:
    """*seed* (four corners, pixels of *field*) snapped to the surface's edges found within *band* pixels of its own.

    *field* is a 2D float32 array: the footage's luma, or a keyed surface's
    matte scaled to 0-255 (its edge is the surface's); *excluded* (a boolean
    or float mask of its shape) what stands in front of the surface, which
    gives no edge. An edge not found keeps the seed's line; a result that
    folds, changes area past :data:`MAX_AREA_CHANGE` or moves a corner past
    :data:`MAX_CORNER_BANDS` bands is no surface: the seed comes back,
    nothing found.
    """
    import numpy as np

    from app.services.video_studio.tracking import convex

    corners = np.asarray(seed, dtype=float).reshape(4, 2)
    centre = corners.mean(axis=0)
    mask = None if excluded is None else np.ascontiguousarray(excluded, dtype=np.float32)
    lines: list[tuple[Any, Any]] = []
    found: list[bool] = []
    fits: list[EdgeFit] = []
    for index in range(4):
        a, b = corners[index], corners[(index + 1) % 4]
        edge = b - a
        normal = np.array([-edge[1], edge[0]])
        normal /= max(float(np.linalg.norm(normal)), 1e-6)
        if normal @ (centre - a) < 0:
            normal = -normal
        fit = _edge_line(field, a, b, normal, band, mask)
        found.append(fit.line is not None)
        fits.append(fit)
        lines.append(fit.line if fit.line is not None else (a, edge / max(float(np.linalg.norm(edge)), 1e-6)))
    shape = {
        "bows": (fits[0].bow, fits[1].bow, fits[2].bow, fits[3].bow),
        "inliers": (fits[0].inliers, fits[1].inliers, fits[2].inliers, fits[3].inliers),
        "residuals": (fits[0].residual, fits[1].residual, fits[2].residual, fits[3].residual),
        "visible": (fits[0].visible, fits[1].visible, fits[2].visible, fits[3].visible),
    }
    nothing = Refined(corners, None, **shape)
    if sum(found) < 2:
        return nothing
    refined = []
    for index in range(4):
        before, after = lines[(index - 1) % 4], lines[index]
        point = _intersect(before[0], before[1], after[0], after[1])
        if point is None:
            return nothing
        refined.append(point)
    quad = np.asarray(refined, dtype=float)
    moved = np.linalg.norm(quad - corners, axis=1)
    area_before, area_after = _area(corners), _area(quad)
    if (
        not convex(quad)
        or area_before <= 0
        or abs(area_after - area_before) > MAX_AREA_CHANGE * area_before
        or float(moved.max()) > MAX_CORNER_BANDS * band
    ):
        return nothing
    result = Refined(quad, None, tuple(found))
    # A corner is measured when both its edges were found; the seed's lines move nothing on their own.
    measured = list(result.measured)
    error = float(moved[measured].mean()) if len(measured) >= 2 else None
    return Refined(quad, error, tuple(found), **shape)


def _four(values: Any) -> tuple[float | None, float | None, float | None, float | None]:
    read = [None if value is None else float(value) for value in (values or (None,) * 4)]
    return read[0], read[1], read[2], read[3]


@dataclass(frozen=True)
class Flatness:
    """How flat a bezel is: its edges' bows, each confirmed by its visible points or not,
    the edges mostly covered, whether its quad is convex, and - a fact, never a fault - how far it is seen at an angle
    (its opposite sides' lengths, and the degrees it is turned)."""

    #: Its sides' lengths in pixels, in :data:`SIDES` order.
    sides: tuple[float, float, float, float]
    #: How far each edge bows, a share of its length (:attr:`Refined.bows`); ``None`` where too few points told.
    bows: tuple[float | None, float | None, float | None, float | None] = (None, None, None, None)
    #: The share of each edge's points on its line, and how far they stand off one straight line, a share of the
    #: frame's width (:class:`EdgeFit`); ``None``: not measured.
    inliers: tuple[float | None, float | None, float | None, float | None] = (None, None, None, None)
    residuals: tuple[float | None, float | None, float | None, float | None] = (None, None, None, None)
    #: Whether its corners make a convex quad: a folded one is no planar surface.
    convex: bool = True
    #: How far it is turned from the camera, in degrees (:func:`viewing_angle`); ``None``: not read.
    degrees: float | None = None
    #: The share of each edge's readings clear of what stands in front (:attr:`Refined.visible`); ``None``: not
    #: read (a record older than it, or nothing in front was known).
    visible: tuple[float | None, float | None, float | None, float | None] = (None, None, None, None)

    def covered(self) -> list[str]:
        """The sides mostly covered by what stands in front: under :data:`MIN_EDGE_VISIBLE` of their readings
        clear, their bow not judged."""
        return [name for name, share in zip(SIDES, self.visible, strict=True) if share is not None and share < MIN_EDGE_VISIBLE]

    def covered_words(self) -> str | None:
        """``right edge mostly covered (34% visible)``: the edges whose bow was not judged; ``None`` for none."""
        named = [
            f"{name} edge mostly covered ({float(share):.0%} visible)"
            for name, share in zip(SIDES, self.visible, strict=True)
            if share is not None and share < MIN_EDGE_VISIBLE
        ]
        return ", ".join(named) or None

    def confirmed(self, index: int) -> bool:
        """Whether edge *index* is curved: visible on at least :data:`MIN_EDGE_VISIBLE` of it, bowing past
        :data:`FLAT_MAX_SHARE`, AND fewer than :data:`BOW_MAX_INLIERS` of its points on one line AND its residual past
        :data:`ALIGNMENT_MAX_SHARE` of the frame's width - never one of them alone: a bow whose points' agreement was
        not read (an older record) is not judged."""
        bow, seen = self.bows[index], self.visible[index]
        if bow is None or bow <= FLAT_MAX_SHARE or (seen is not None and seen < MIN_EDGE_VISIBLE):
            return False
        inliers, residual = self.inliers[index], self.residuals[index]
        return inliers is not None and inliers < BOW_MAX_INLIERS and residual is not None and residual > ALIGNMENT_MAX_SHARE

    def edges(self) -> dict[str, dict]:
        """Per side, what its points said: ``{side: {bow, visible, inliers, residual, judged}}`` - ``judged``
        False for an edge mostly covered."""
        return {
            name: {
                "bow": self.bows[index],
                "visible": self.visible[index],
                "inliers": self.inliers[index],
                "residual": self.residuals[index],
                "judged": self.visible[index] is None or self.visible[index] >= MIN_EDGE_VISIBLE,
            }
            for index, name in enumerate(SIDES)
        }

    def _bows(self) -> list[tuple[float, str]]:
        """``(bow, words)`` of each edge whose bow its points confirm (:meth:`confirmed`)."""
        return [
            (float(bow), f"its {name} edge bows {float(bow):.0%} of its length")
            for index, (name, bow) in enumerate(zip(SIDES, self.bows, strict=True))
            if bow is not None and self.confirmed(index)
        ]

    def _angles(self) -> list[tuple[float, str]]:
        """``(difference, words)`` of each pair of opposite sides: a share of the shorter."""
        top, right, bottom, left = self.sides
        found = []
        for (one, first), (other, second), longer in (((left, "left"), (right, "right"), "taller"), ((top, "top"), (bottom, "bottom"), "wider")):
            (big, big_name), (small, small_name) = ((one, first), (other, second)) if one >= other else ((other, second), (one, first))
            error = big / small - 1.0 if small > 0 else 1.0
            found.append((error, f"{big_name} side {error:.0%} {longer} than {small_name}"))
        return found

    @property
    def error(self) -> float:
        """Its worst confirmed bow, a share of its edge's length (0 when none is)."""
        return max((bow for bow, _words in self._bows()), default=0.0)

    @property
    def flat(self) -> bool:
        return self.convex and self.error <= FLAT_MAX_SHARE

    @property
    def angle(self) -> float:
        """How far it is seen at an angle: its opposite sides' worst difference, a share of the shorter. A fact - the
        perspective warp maps the source onto any convex planar quad exactly."""
        return max(error for error, _words in self._angles())

    def angle_words(self) -> str:
        """``left side 25% taller than right``: the pair of opposite sides that differ most."""
        return max(self._angles(), key=lambda measure: measure[0])[1]

    def seen_at(self) -> str | None:
        """``seen at 35 degrees``: how far a surface turned :data:`ANGLE_FACT_DEGREES` or more is, as a fact; for
        one whose degrees were not read, its sides past :data:`ANGLE_FACT_SHARE`; ``None`` for a surface seen square-on."""
        if self.degrees is not None:
            return f"seen at {self.degrees:.0f} degrees" if self.degrees >= ANGLE_FACT_DEGREES else None
        return f"seen at an angle: {self.angle_words()}" if self.angle > ANGLE_FACT_SHARE else None

    def words(self) -> str:
        """``its top edge bows 8% of its length``: its worst confirmed bow, or its folded corners; else how far it is
        seen at an angle."""
        if not self.convex:
            return "its corners fold into a quad that is not convex"
        bows = self._bows()
        if bows:
            return max(bows, key=lambda measure: measure[0])[1]
        return self.seen_at() or "flat and square-on"

    def fault(self) -> str | None:
        """What makes it no planar surface, with the limit it passes; ``None`` when it is flat."""
        if self.flat:
            return None
        return self.words() if not self.convex else f"{self.words()}, past {FLAT_MAX_SHARE:.0%}"

    def as_dict(self) -> dict:
        return {
            "sides": [round(side, 1) for side in self.sides],
            "bows": list(self.bows),
            "inliers": list(self.inliers),
            "residuals": list(self.residuals),
            "visible": list(self.visible),
            "convex": self.convex,
            "degrees": self.degrees,
            "error": round(self.error, 4),
            "flat": self.flat,
            "worst": self.words(),
            "covered": self.covered(),
            "angle": round(self.angle, 4),
        }

    @classmethod
    def from_dict(cls, data: dict) -> Flatness:
        sides = tuple(float(side) for side in data["sides"])
        degrees = data.get("degrees")
        return cls(
            (sides[0], sides[1], sides[2], sides[3]),
            _four(data.get("bows")),
            _four(data.get("inliers")),
            _four(data.get("residuals")),
            bool(data.get("convex", True)),
            None if degrees is None else float(degrees),
            _four(data.get("visible")),
        )


def flatness(
    quad: Any,
    bows: Any = None,
    inliers: Any = None,
    residuals: Any = None,
    size: tuple[float, float] | None = None,
    visible: Any = None,
) -> Flatness:
    """The :class:`Flatness` of *quad* (four corners in pixels of a frame of *size*, which its degrees are read in)
    whose edges bow by *bows*, their points on their line by *inliers* and off one straight line by *residuals* (shares
    of the frame's width), *visible* of each clear of what stands in front."""
    import numpy as np

    from app.services.video_studio.tracking import convex

    corners = np.asarray(quad, dtype=float).reshape(4, 2)
    sides = [float(np.linalg.norm(corners[(index + 1) % 4] - corners[index])) for index in range(4)]
    folded = not convex(corners)
    degrees = None if size is None or folded else viewing_angle(corners, size)
    return Flatness(
        (sides[0], sides[1], sides[2], sides[3]),
        _four(bows),
        _four(inliers),
        _four(residuals),
        not folded,
        degrees,
        _four(visible),
    )


def viewing_angle(quad: Any, size: tuple[float, float]) -> float | None:
    """How far the flat surface at *quad* (four corners in pixels of a frame of *size*) is turned from the camera, in
    degrees: the angle between its normal and the line of sight to its middle. ``None`` for a quad no
    rectangle maps to.

    The quad is the picture of a rectangle by a homography: its two vanishing points give the focal length when both
    are finite (their directions are perpendicular in space), else :data:`ASSUMED_FOCAL` stands; the rectangle's two
    directions, brought back through that camera, span its plane.
    """
    import numpy as np

    from app.services.video_studio.pictures import PictureError, perspective_coefficients

    corners = np.asarray(quad, dtype=float).reshape(4, 2)
    width, height = float(size[0]), float(size[1])
    try:
        a, b, c, d, e, f, g, h = perspective_coefficients(tuple(map(tuple, corners)), 1, 1)
        # Pillow's coefficients map the frame back to the unit square: inverted, the square drawn into the frame.
        drawn = np.linalg.inv(np.array([[a, b, c], [d, e, f], [g, h, 1.0]]))
    except (PictureError, np.linalg.LinAlgError):
        return None
    middle = np.array([width / 2.0, height / 2.0])
    focal = ASSUMED_FOCAL * width
    # The vanishing points of the rectangle's two directions, homogeneous; one past 50 frame widths is at infinity.
    across, down = drawn[:, 0], drawn[:, 1]
    reach = 50.0 * width
    if all(abs(point[2]) * reach > float(np.linalg.norm(point[:2])) for point in (across, down)):
        squared = -float((across[:2] / across[2] - middle) @ (down[:2] / down[2] - middle))
        if (FOCAL_RANGE[0] * width) ** 2 <= squared <= (FOCAL_RANGE[1] * width) ** 2:
            focal = squared**0.5
    to_rays = np.array([[1.0 / focal, 0.0, -middle[0] / focal], [0.0, 1.0 / focal, -middle[1] / focal], [0.0, 0.0, 1.0]])
    first, second = to_rays @ across, to_rays @ down
    normal = np.cross(first / np.linalg.norm(first), second / np.linalg.norm(second))
    sight = to_rays @ (drawn @ np.array([0.5, 0.5, 1.0]))
    lengths = float(np.linalg.norm(normal) * np.linalg.norm(sight))
    if lengths < 1e-12:
        return None
    return round(float(np.degrees(np.arccos(np.clip(abs(float(normal @ sight)) / lengths, 0.0, 1.0)))), 1)


def shares_of(residuals: Any, width: float) -> tuple[float | None, float | None, float | None, float | None]:
    """*residuals* (pixels, :attr:`Refined.residuals`) as shares of a frame *width* pixels wide."""
    return _four([None if value is None else round(float(value) / max(float(width), 1.0), 5) for value in residuals or (None,) * 4])


@dataclass(frozen=True)
class Bezel:
    """The bezel a frame shows (:func:`measure_bezel`): its corners, 0-1, how flat it is, and how far off it a
    quad given was."""

    quad: tuple[tuple[float, float], ...]
    flatness: Flatness
    #: The quad given against it, mean over the corners its edges placed, a share of the frame's width; ``None``
    #: when none was given.
    error: float | None = None
    #: ``seed``: snapped to the edges found near the quad given; ``frame``: found on the frame, its flat key surface.
    found_by: str = "frame"
    #: The key's spill on the edge of what stands in front of the surface on that frame, and what the edge despill
    #: leaves of it (``screen_insert.EdgeSpill``); ``None``: no key, or nothing in front.
    spill: Any = None
    #: Found on the frame (``found_by: frame``): the median hue of its surface in degrees
    #: (``screens.FoundScreen.hue``).
    hue: float | None = None
    #: The sides hidden behind what stands in front placed at the key colour's extent past them (:func:`hidden_sides`):
    #: ``(side, x or y 0-1 of the frame)`` each; empty for none.
    hidden: tuple[tuple[str, float], ...] = ()

    def as_dict(self) -> dict:
        return {
            "quad": [[x, y] for x, y in self.quad],
            "flatness": self.flatness.as_dict(),
            "error": None if self.error is None else round(self.error, 5),
            "found_by": self.found_by,
            **({"spill": self.spill.as_dict()} if self.spill is not None else {}),
            **({"hue": self.hue} if self.hue is not None else {}),
            **({"hidden": [[side, value] for side, value in self.hidden]} if self.hidden else {}),
        }

    @classmethod
    def from_dict(cls, data: dict) -> Bezel:
        from app.services.video_studio.screen_insert import EdgeSpill

        return cls(
            tuple((float(x), float(y)) for x, y in data["quad"]),
            Flatness.from_dict(data["flatness"]),
            None if data.get("error") is None else float(data["error"]),
            str(data.get("found_by") or "frame"),
            EdgeSpill.from_dict(data["spill"]) if isinstance(data.get("spill"), dict) else None,
            None if data.get("hue") is None else float(data["hue"]),
            tuple((str(side), float(value)) for side, value in data.get("hidden") or ()),
        )

    def hidden_words(self) -> str | None:
        """``right side hidden behind the foreground: placed at the key colour's extent, x 0.985``; ``None`` for none."""
        if not self.hidden:
            return None
        return "; ".join(
            f"{side} side hidden behind the foreground: placed at the key colour's extent, "
            f"{'y' if side in ('top', 'bottom') else 'x'} {value:.3f}"
            for side, value in self.hidden
        )


def _refined(read: tuple[Any, Any], seed: Any, band: float) -> Refined:
    """:func:`refine` of *seed* on a ``(field, excluded)`` pair."""
    field, excluded = read
    return refine(field, seed, band, excluded)


def edge_field(
    pixels: Any,
    seed: Any,
    key: Any = None,
    tolerance: float | None = None,
    *,
    feather: float = 1.5,
    frame: tuple[int, int] | None = None,
) -> tuple[Any, Any]:
    """``(field, excluded)`` the bezel near *seed* (four corners, pixels of *pixels*, ``H x W x 3`` uint8) is read on, as
    the render's lock reads a frame: the luma, or a keyed surface's matte (*key*, a ``screens.Key``, read with
    *tolerance*) with what stands in front of its edges left out (``screen_insert.edge_front``, *feather* in pixels of
    a 1080p frame). *frame* is the size ``(w, h)`` of the frame *pixels* is a part of, its band and its scale read off
    it; ``None``: *pixels* is the whole frame."""
    import numpy as np

    from app.services.video_studio import screens
    from app.services.video_studio.screen_insert import edge_front
    from app.services.video_studio.tracking import luma

    if key is None:
        return np.ascontiguousarray(luma(pixels), dtype=np.float32), None
    width, height = frame or (pixels.shape[1], pixels.shape[0])
    scale = height / 1080.0
    matte = screens.key_matte(pixels, key, tolerance)
    band = max(MIN_BAND_PX, BAND * width)
    return np.ascontiguousarray(matte * 255.0, dtype=np.float32), edge_front(matte, seed, band, scale, feather * scale)


def read_near(pixels: Any, seed: Any, key: Any = None, tolerance: float | None = None, *, feather: float = 1.5) -> Refined:
    """The bezel *pixels* (``H x W x 3`` uint8) shows near *seed* (four corners, its pixels): :func:`refine` on the
    field :func:`edge_field` gives, within :data:`BAND` of the frame's width of the seed's edges - what a tracked insert
    keeps on a frame its tracker is not sure of (``insert_tracking``). Read on the part of the frame a refined
    quad can reach (:data:`MAX_CORNER_BANDS` bands past the seed, and the band searched past that); the quad found in
    the frame's pixels."""
    import dataclasses

    import numpy as np

    pixels = np.asarray(pixels)[..., :3]
    height, width = pixels.shape[:2]
    corners = np.asarray(seed, dtype=float).reshape(4, 2)
    band = max(MIN_BAND_PX, BAND * width)
    reach = (MAX_CORNER_BANDS + 1.0) * band + 2.0 * SIDE_PX
    x0, y0 = (max(0, math.floor(value - reach)) for value in corners.min(axis=0))
    x1, y1 = min(width, math.ceil(corners[:, 0].max() + reach)), min(height, math.ceil(corners[:, 1].max() + reach))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return Refined(corners, None)
    origin = np.array([x0, y0], dtype=float)
    crop = np.ascontiguousarray(pixels[y0:y1, x0:x1], dtype=np.uint8)
    read = edge_field(crop, corners - origin, key, tolerance, feather=feather, frame=(width, height))
    found = _refined(read, corners - origin, band)
    return dataclasses.replace(found, quad=np.asarray(found.quad, dtype=float) + origin)


def hidden_sides(
    pixels: Any, quad: Any, visible: Any, key: Any, tolerance: float | None = None
) -> tuple[Any, tuple[tuple[str, float], ...]]:
    """*quad* (four corners, pixels of *pixels*, ``H x W x 3`` uint8) with each side whose bezel shows on less than
    :data:`HIDDEN_EDGE_VISIBLE` of its readings (*visible*, :attr:`Refined.visible`) moved out to the key colour's
    extent past it (*key*, a ``screens.Key``, read with *tolerance*), and those sides: ``(side, x or y 0-1)``.

    A side hidden behind what stands in front stops at the occluder's edge, and the screen's own key runs on past it
    up to the occluder: read along the side's outward normal at :data:`SAMPLES_PER_EDGE` points, the longest run of
    the key from the side outward is how far the screen continues, the frame's edge at most. A run past
    :data:`HIDDEN_REACH` of the quad's span across that side is a key-coloured wall, never the screen: that side
    stays; one under :data:`HIDDEN_MIN_PX` (of a 1080p frame) is the screen's own edge. Sides whose bezel shows are
    never moved."""
    import numpy as np

    from app.services.video_studio import screens

    corners = np.asarray(quad, dtype=float).reshape(4, 2).copy()
    shares = list(visible or ())
    hidden = [
        index for index, share in enumerate(shares[:4]) if share is not None and float(share) < HIDDEN_EDGE_VISIBLE
    ]
    if key is None or not hidden:
        return corners, ()
    pixels = np.asarray(pixels)[..., :3]
    height, width = pixels.shape[:2]
    keyed = np.asarray(screens.key_matte(pixels, key, tolerance)) >= 0.5
    centre = corners.mean(axis=0)
    # Every side's line first - moved out for a hidden side, as found otherwise -, then every corner where its two
    # lines meet: two neighbouring sides hidden (a person over a corner of the screen) both keep their shift there.
    shifts = [0.0, 0.0, 0.0, 0.0]
    normals = [np.zeros(2), np.zeros(2), np.zeros(2), np.zeros(2)]
    for index in hidden:
        start, end = corners[index], corners[(index + 1) % 4]
        along = end - start
        length = float(np.linalg.norm(along))
        if length < 2.0:
            continue
        normal = np.array([along[1], -along[0]]) / length
        middle = (start + end) / 2.0
        if float(np.dot(normal, middle - centre)) < 0.0:
            normal = -normal
        opposite = (corners[(index + 2) % 4] + corners[(index + 3) % 4]) / 2.0
        reach = math.ceil(HIDDEN_REACH * float(np.linalg.norm(middle - opposite)))
        if reach < 2:
            continue
        points = start[None, :] + np.linspace(0.1, 0.9, SAMPLES_PER_EDGE)[:, None] * along[None, :]
        steps = np.arange(1, reach + 2, dtype=float)
        probes = points[:, None, :] + steps[None, :, None] * normal[None, None, :]
        xs, ys = np.rint(probes[..., 0]).astype(int), np.rint(probes[..., 1]).astype(int)
        inside = (xs >= 0) & (xs < width) & (ys >= 0) & (ys < height)
        on_key = np.zeros(xs.shape, dtype=bool)
        on_key[inside] = keyed[ys[inside], xs[inside]]
        # The run of the key from the side outward, per point: up to the first pixel off it (or off the frame).
        runs = np.where(on_key.all(axis=1), steps.size, np.argmin(on_key, axis=1))
        if int(runs.max()) > reach:
            continue
        extent = float(runs.max())
        if extent < HIDDEN_MIN_PX * height / 1080.0:
            continue
        shifts[index], normals[index] = extent, normal
    placed = [index for index in range(4) if shifts[index] > 0.0]
    if not placed:
        return corners, ()
    # Each side's line moved out by its run, each corner where its two sides' lines meet: a quad seen at an angle keeps
    # its perspective.
    lines = [
        (corners[index] + shifts[index] * normals[index], corners[(index + 1) % 4] - corners[index])
        for index in range(4)
    ]
    moved = corners.copy()
    for corner in range(4):
        before, after = (corner - 1) % 4, corner
        met = _intersect(lines[after][0], lines[after][1], lines[before][0], lines[before][1])
        moved[corner] = (
            met
            if met is not None
            else corners[corner] + shifts[before] * normals[before] + shifts[after] * normals[after]
        )
    moved[:, 0] = np.clip(moved[:, 0], 0.0, float(width))
    moved[:, 1] = np.clip(moved[:, 1], 0.0, float(height))
    said: list[tuple[str, float]] = []
    for index in placed:
        side = SIDES[index]
        pair = moved[[index, (index + 1) % 4]]
        value = float(pair[:, 1].mean()) / height if side in ("top", "bottom") else float(pair[:, 0].mean()) / width
        said.append((side, round(value, 4)))
    return moved, tuple(said)


def measure_bezel(
    frame: Any,
    key: Any = None,
    tolerance: float | None = None,
    *,
    given: Any = None,
    feather: float = 1.5,
    despill: Any = None,
    surface: Any = None,
) -> Bezel | None:
    """The bezel *frame* shows, its flatness and how far off it the quad *given* (0-1) is; ``None``: none.

    *frame* is the footage frame the insert comes in on (``H x W x 3``
    uint8), *key* its chroma key (``screens.Key``, read with the author's
    *tolerance* and *feather*, in pixels of a 1080p frame) or ``None``. The
    frame is read as the render's lock reads it
    (``screen_insert._locked``): a named key as the shade the surface shows,
    its tolerance widened to the surface's spread (``KeySpec.sampled``), and
    what stands in front of the surface's edges left out of their readings
    (``screen_insert.edge_front``). A quad *given* is the seed first:
    :func:`refine` snaps it to the edges found near it, as the render snaps it
    on every frame. Otherwise - or with nothing found near it - the seed is
    the flat surface of the key's colour the frame shows (green, blue or
    magenta without a key, ``screens.find_screen``), snapped the same way; a
    surface whose edges give no line keeps the outline found. No such surface
    and nothing near the quad given: no bezel. With *surface* (a
    ``screens.FoundScreen`` of a frame of the same footage: the screen of a
    scene's several inserts this one was handed, :func:`screens_on`) that
    screen is the seed instead of the frame's largest.

    With a key, the key's spill on the edge of what stands in front of the
    surface is read on the bezel found (``screen_insert.edge_spill``):
    its share of that edge, and what the edge despill (*despill*, the
    insert's ``despill`` - its default when ``None``) leaves of it.
    """
    import numpy as np
    from PIL import Image

    from app.services.video_studio import screens
    from app.services.video_studio.screen_insert import Despill, _coverage, edge_spill

    pixels = np.ascontiguousarray(np.asarray(frame)[..., :3], dtype=np.uint8)
    height, width = pixels.shape[:2]
    band = max(MIN_BAND_PX, BAND * width)
    scale = height / 1080.0

    def shade_of(corners: list) -> tuple[Any, float | None]:
        """The key as the render keys it on the surface within *corners*, and its tolerance widened to its spread."""
        shade = key
        if shade.name in dict(screens.KEY_CANDIDATES):
            # A named key is the shade the surface shows, as the render keys it (``screen_insert.KeySpec.sampled``).
            shade = screens.sample_key(pixels, corners, shade) or shade
        widened = screens.spread_tolerance(tolerance, screens.key_spread(pixels, corners, shade)) if tolerance is not None else None
        return shade, widened

    def field_of(seed: Any) -> tuple[Any, Any]:
        """``(field, excluded)`` of *seed*: what the render's lock reads, and what stands in front of its edges."""
        if key is None:
            return edge_field(pixels, seed)
        shade, widened = shade_of([tuple(point) for point in seed])
        return edge_field(pixels, seed, shade, widened, feather=feather)

    def spill_on(corners: Any) -> Any:
        """The key's spill on the edge of what stands in front inside *corners* (pixels), on the box around them."""
        if key is None:
            return None
        x0, y0 = (max(0, math.floor(v - band)) for v in corners.min(axis=0))
        x1, y1 = min(width, math.ceil(corners[:, 0].max() + band)), min(height, math.ceil(corners[:, 1].max() + band))
        if x1 - x0 < 8 or y1 - y0 < 8:
            return None
        shade, widened = shade_of([tuple(point) for point in corners])
        region = pixels[y0:y1, x0:x1].astype(np.float32)
        inside = _coverage(corners, (x0, y0, x1, y1)) > 0.5
        matte = screens.key_matte(region, shade, widened)
        return edge_spill(region, matte, inside, shade, despill or Despill(), scale=scale, feather=feather * scale)

    def bezel(refined: Refined, error: float | None, found_by: str, hue: float | None = None) -> Bezel:
        corners = np.asarray(refined.quad, dtype=float).reshape(4, 2)
        hidden: tuple[tuple[str, float], ...] = ()
        if key is not None:
            # A side hidden behind what stands in front: at the key colour's extent past it, never at the occluder.
            shade, widened = shade_of([tuple(point) for point in corners])
            placed, hidden = hidden_sides(pixels, corners, refined.visible, shade, widened)
            if hidden:
                # ``error`` stays the quad given against the bezel its visible sides placed: what the snap judges.
                corners = placed
        quad = tuple((round(float(x) / width, 5), round(float(y) / height, 5)) for x, y in corners)
        shape = flatness(
            corners,
            refined.bows,
            refined.inliers,
            shares_of(refined.residuals, width),
            (width, height),
            refined.visible,
        )
        return Bezel(quad, shape, error, found_by, spill_on(corners), hue, hidden)

    seed = None
    if given is not None:
        seed = np.asarray([(float(x) * width, float(y) * height) for x, y in given], dtype=float).reshape(4, 2)
        near = _refined(field_of(seed), seed, band)
        if near.found and near.seed_error is not None:
            return bezel(near, near.seed_error / max(width, 1), "seed")
    candidates = [(key.name, key.rgb)] if key is not None else screens.KEY_CANDIDATES
    if surface is not None:
        found = surface
    else:
        try:
            shown = screens.found_screens(Image.fromarray(pixels, "RGB"), candidates)
        except screens.ScreenError:
            return None
        found = shown[0]
        if seed is not None and len(shown) > 1:
            # A quad given far off its screen is measured against the screen nearest it, never the frame's largest:
            # on a frame of several screens each insert's quad is judged by its own.
            centre = seed.mean(axis=0)
            found = min(
                shown,
                key=lambda item: float(np.linalg.norm(np.asarray(item.corners, dtype=float).mean(axis=0) - centre)),
            )
    outline = np.asarray(found.corners, dtype=float)
    refined = _refined(field_of(outline), outline, band)
    error = None
    if seed is not None:
        error = float(np.linalg.norm(np.asarray(refined.quad, dtype=float) - seed, axis=1).mean()) / max(width, 1)
    return bezel(refined, error, "frame", found.hue)


def found_quad(frame: Any, key: Any = None, tolerance: float | None = None) -> tuple[tuple[float, float], ...] | None:
    """The bezel *frame* shows, 0-1 across and down it: a static insert's ``quad: auto``; ``None`` for none
    (:func:`measure_bezel` with no quad given)."""
    found = measure_bezel(frame, key, tolerance)
    return found.quad if found is not None else None


def screens_on(frame: Any, key: Any = None) -> list[Any]:
    """Every screen *frame* shows (``screens.found_screens``: green, blue or magenta without a *key*), in its
    pixels, in reading order (:func:`reading_order`); empty for none. What the screen inserts of one
    scene left to the footage are handed out of, one screen each."""
    import numpy as np
    from PIL import Image

    from app.services.video_studio import screens

    pixels = np.ascontiguousarray(np.asarray(frame)[..., :3], dtype=np.uint8)
    candidates = [(key.name, key.rgb)] if key is not None else screens.KEY_CANDIDATES
    try:
        found = screens.found_screens(Image.fromarray(pixels, "RGB"), candidates)
    except screens.ScreenError:
        return []
    return [found[i] for i in reading_order([item.corners for item in found])]


def reading_order(quads: Any) -> list[int]:
    """The indexes of *quads* (any unit, one frame's) as a page is read: row by row from the top, left to right in
    a row. A quad whose centre stands within the height of the row's first quad is in that row."""
    boxes = [
        (
            sum(float(x) for x, _y in quad) / 4.0,
            sum(float(y) for _x, y in quad) / 4.0,
            min(float(y) for _x, y in quad),
            max(float(y) for _x, y in quad),
        )
        for quad in quads
    ]
    rows: list[list[int]] = []
    for index in sorted(range(len(boxes)), key=lambda i: boxes[i][1]):
        if rows and boxes[rows[-1][0]][2] <= boxes[index][1] <= boxes[rows[-1][0]][3]:
            rows[-1].append(index)
        else:
            rows.append([index])
    return [index for row in rows for index in sorted(row, key=lambda i: boxes[i][0])]


@dataclass
class Refiner:
    """The bezel lock of one insert, frame after frame: the correction found carried as the next seed.

    Held by a temporal prior on an untracked quad (``step(prior=...)``):
    the corrections kept over the last :data:`PRIOR_FRAMES` frames, a
    refinement moving a corner more than :data:`PRIOR_MAX_SHARE` of the
    frame's width from their median rejected for it - on a locked-off shot the
    bezel never jumps, the refiner caught an inner edge (a text line painted
    on the surface) - unless :data:`PRIOR_SETTLE_FRAMES` frames in a row say
    the same.
    """

    #: The band searched, as a share of the frame's width.
    band_share: float = BAND
    #: The last correction found (refined minus given, per corner), carried to the next frame's seed.
    correction: Any = None
    steps: int = 0
    #: How many frames in a row the bezel was not found: the lock is given up after a while (the seed comes back).
    missed: int = 0
    found_frames: int = field(default=0)
    #: The corrections kept, oldest first (at most :data:`PRIOR_FRAMES`), with the edges each was found on.
    kept: list = field(default_factory=list)
    #: The refinements rejected in a row, newest last: once :data:`PRIOR_SETTLE_FRAMES` agree, they are the bezel.
    rejected: list = field(default_factory=list)
    #: A tracked quad drawn steady (:meth:`_steady`): its corners' filtered position and motion (pixels, per frame).
    steady: Any = None

    def _prior(self, correction: Any, edges: tuple, frame_width: float) -> tuple[Any, tuple, bool]:
        """The correction this frame keeps, the edges it stands on and whether it is the median the prior held:
        *correction* (found on *edges*) when it stays by the median of those kept, else that median - *correction*
        recorded as rejected, adopted once :data:`PRIOR_SETTLE_FRAMES` agree."""
        import numpy as np

        reach = PRIOR_MAX_SHARE * max(float(frame_width), 1.0)

        def apart(a: Any, b: Any) -> float:
            return float(np.linalg.norm(np.asarray(a) - np.asarray(b), axis=1).max())

        if self.kept:
            median = np.median(np.stack([kept for kept, _edges in self.kept]), axis=0)
            if apart(correction, median) > reach:
                self.rejected = [*self.rejected, (correction, edges)][-PRIOR_SETTLE_FRAMES:]
                settled = self.rejected
                newest = settled[-1][0]
                if len(settled) < PRIOR_SETTLE_FRAMES or any(apart(c, newest) > reach for c, _e in settled):
                    return median, self.kept[-1][1], True
                # The bezel did move (a camera's step, a lock first set on an inner edge): the lock follows.
                self.kept = list(settled)
                self.rejected = []
                return correction, edges, False
        self.rejected = []
        self.kept = [*self.kept, (correction, edges)][-PRIOR_FRAMES:]
        return correction, edges, False

    def _steady(self, found: Any) -> Any:
        """The quad drawn for the bezel *found* (corners, pixels) on a tracked quad: an alpha-beta filter of the
        corners (:data:`STEADY_WEIGHT`, :data:`STEADY_MOTION_WEIGHT`), its position and motion carried frame to
        frame; the first bezel found, or one found again after the lock was lost, is drawn as found."""
        import numpy as np

        found = np.asarray(found, dtype=float)
        if self.steady is None:
            self.steady = (found, np.zeros_like(found))
            return found
        position, motion = self.steady
        predicted = position + motion
        residual = found - predicted
        position = predicted + STEADY_WEIGHT * residual
        self.steady = (position, motion + STEADY_MOTION_WEIGHT * residual)
        return position

    def step(
        self,
        field: Any,
        quad: Any,
        frame_width: float,
        excluded: Any = None,
        *,
        prior: bool = True,
        steady: bool = False,
    ) -> Refined:
        """*quad* (pixels of *field*, the given or tracked corners of this frame) snapped to the bezel; the alignment
        error is against *quad*, as a share of *frame_width*. *excluded* is what stands in front (:func:`refine`).

        The seed is *quad* moved by the last correction; every
        :data:`RESEED_EVERY` frames, and whenever the lock was lost, *quad*
        itself is tried first. With *prior* (an untracked quad, whose bezel
        cannot move: a tracker's own correction is a real one), the bezel found
        is held to the temporal prior (:meth:`_prior`): a refinement off the
        median of the last frames is drawn as that median, its own error kept
        for the gate (``found_error``). With *steady* (a tracked quad, whose bezel moves with the footage), the quad
        drawn is the bezel found filtered frame to frame (:meth:`_steady`): the bezel read on one frame stands a pixel
        off the one on the next, which drawn as read steps past the jitter gate where the footage moves smoothly; the
        alignment error stays the bezel found's.
        """
        import numpy as np

        given = np.asarray(quad, dtype=float).reshape(4, 2)
        band = max(MIN_BAND_PX, self.band_share * frame_width)
        self.steps += 1
        tried: list[Any] = []
        if self.correction is None or self.steps % RESEED_EVERY == 1 or self.missed >= 3:
            tried.append(given)
        if self.correction is not None:
            tried.append(given + self.correction)
        best: Refined | None = None
        result = None
        for seed in tried:
            result = refine(field, seed, band, excluded)
            if result.found:
                best = result
                break
        if best is None:
            self.missed += 1
            if self.missed >= 3:
                self.steady = None
            carried = given + self.correction if self.correction is not None and self.missed < 3 else given
            # No bezel placed, but its edges' points still say how they bow: what the flatness gate reads.
            return Refined(carried, None, **(_shape(result) if result is not None else {}))
        self.missed = 0
        self.found_frames += 1
        found = best.quad - given
        if prior:
            correction, edges, held = self._prior(found, best.edges_found, frame_width)
        elif steady:
            correction, edges, held = self._steady(best.quad) - given, best.edges_found, False
        else:
            correction, edges, held = found, best.edges_found, False
        self.correction = correction

        def error_of(shift: Any, corners: tuple) -> float | None:
            # The GIVEN quad against a bezel, whatever seed found it: mean over the corners its edges placed.
            measured = list(Refined(given + shift, None, corners).measured)
            if len(measured) < 2:
                return None
            return float(np.linalg.norm(shift[measured], axis=1).mean()) / max(float(frame_width), 1.0)

        found_error = error_of(found, best.edges_found) if held else None
        # A steady correction is drawn, never measured: the given quad's error is against the bezel found.
        error = error_of(found, edges) if steady and not prior else error_of(correction, edges)
        return Refined(given + correction, error, edges, held, found_error, **_shape(best))


def _shape(refined: Refined) -> dict:
    """What *refined*'s edge points say of its shape: their bows, inliers, residuals and how much of each
    edge was visible, to carry on."""
    return {"bows": refined.bows, "inliers": refined.inliers, "residuals": refined.residuals, "visible": refined.visible}
