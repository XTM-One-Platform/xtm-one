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

"""A plane followed through footage: feature points, their track and the homography they move by.

The Video Studio follows a plane through a video in three places: the whole
frame of a drifting AI shot (``stabilise``), the bezel of a screen an insert
is mapped onto (``insert_tracking``) and a logo composited back onto a shot
(``logos``). All three read the same evidence, in code, with numpy and scipy:

* **Feature points** (:func:`good_features`): Shi-Tomasi corners - the smaller
  eigenvalue of the structure tensor (Sobel gradients, Gaussian window) -
  kept where they are the strongest within ``min_distance``, inside a
  :class:`Region`.
* **Their track** (:func:`track_points`): pyramidal Lucas-Kanade - each
  point's window followed from one frame to the next, coarse to fine, its
  mean taken out so a change of exposure moves nothing; a point that leaves
  the frame, whose window stops matching or that does not come back when
  tracked backwards is dropped.
* **The homography** (:func:`ransac_homography`): the normalised DLT
  (Hartley) fitted by RANSAC on the tracked points - the 3x3 map of the plane
  from the reference frame to this one; what it does not explain (a hand, a
  subject walking, another plane) is left out as outliers.

:class:`PlanarTracker` keeps each point's position in the reference frame, so
every frame's homography is measured against the reference, never chained
frame to frame; points lost are replaced by points found again where the
homography puts the region. A region holding two planes (a logo on a mug and
the table behind it) is proposed as several candidates
(:meth:`PlanarTracker.propose`, each plane fitted on the points the ones
before left out), and the caller adopts the one it judges is its surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Pyramid levels: a point may move about ``window * 2 ** (levels - 1) / 2`` pixels between two frames.
LEVELS = 3
#: The side of the window a point is matched by (pixels, odd).
WINDOW = 15
#: Lucas-Kanade iterations per level, and the step under which one stops.
ITERATIONS = 10
_STEP_EPSILON = 0.01
#: A point whose window differs from its reference by more than this (grey levels, the means taken out) is lost.
MAX_RESIDUAL = 18.0
#: A window whose weaker gradient direction carries less than this (grey levels squared per pixel) is not followed.
MIN_EIGENVALUE = 0.5
#: A point tracked forward then backward must come back within this many pixels.
MAX_ROUND_TRIP = 1.0
#: RANSAC: the reprojection error (pixels) of an inlier, the hypotheses drawn, and a fixed seed (reproducible).
INLIER_PIXELS = 1.5
RANSAC_HYPOTHESES = 400
_SEED = 6064


def luma(rgb: Any) -> Any:
    """The luma of an ``H x W x 3`` RGB array (Rec. 601), as float32 grey levels."""
    import numpy as np

    pixels = np.asarray(rgb, dtype=np.float32)
    return pixels[..., 0] * 0.299 + pixels[..., 1] * 0.587 + pixels[..., 2] * 0.114


def pyramid(image: Any, levels: int = LEVELS) -> list[Any]:
    """*image* and its halvings (each blurred before it is subsampled), finest first."""
    import numpy as np
    from scipy import ndimage

    levels_out = [np.asarray(image, dtype=np.float32)]
    for _ in range(1, levels):
        smaller = ndimage.gaussian_filter(levels_out[-1], 1.0)[::2, ::2]
        if min(smaller.shape) < WINDOW:
            break
        levels_out.append(np.ascontiguousarray(smaller))
    return levels_out


def _gradients(image: Any) -> tuple[Any, Any]:
    from scipy import ndimage

    return ndimage.sobel(image, axis=1) / 8.0, ndimage.sobel(image, axis=0) / 8.0


def convex(quad: Any) -> bool:
    """Whether *quad* (four corners, in order) turns the same way at every corner: a quad that neither folds nor bows."""
    import numpy as np

    points = np.asarray(quad, dtype=float)
    turns = []
    for index in range(4):
        a, b, c = points[index], points[(index + 1) % 4], points[(index + 2) % 4]
        turns.append((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]))
    return all(turn > 0 for turn in turns) or all(turn < 0 for turn in turns)


def inside(points: Any, quad: Any) -> Any:
    """Which of *points* (``N x 2``) lie inside the convex *quad*: a boolean per point."""
    import numpy as np

    pts, corners = np.asarray(points, dtype=float).reshape(-1, 2), np.asarray(quad, dtype=float)
    sides = np.asarray(
        [
            (corners[(n + 1) % 4][0] - corners[n][0]) * (pts[:, 1] - corners[n][1])
            - (corners[(n + 1) % 4][1] - corners[n][1]) * (pts[:, 0] - corners[n][0])
            for n in range(4)
        ]
    )
    return (sides >= 0).all(axis=0) | (sides <= 0).all(axis=0)


def sample(image: Any, xs: Any, ys: Any) -> Any:
    """*image* (grey, float32) read at the sub-pixel positions *xs*, *ys*: bilinear, the edge repeated outside."""
    return _sample(image, xs, ys)


def _sample(image: Any, xs: Any, ys: Any) -> Any:
    """*image* read at the sub-pixel positions *xs*, *ys* (bilinear, the edge repeated outside)."""
    import numpy as np

    height, width = image.shape
    xs = np.clip(np.asarray(xs, dtype=np.float32), 0.0, width - 1.001)
    ys = np.clip(np.asarray(ys, dtype=np.float32), 0.0, height - 1.001)
    x0, y0 = xs.astype(np.intp), ys.astype(np.intp)
    fx, fy = xs - x0, ys - y0
    flat = image.ravel()
    index = y0 * width + x0
    top = flat.take(index) * (1 - fx) + flat.take(index + 1) * fx
    bottom = flat.take(index + width) * (1 - fx) + flat.take(index + width + 1) * fx
    return top * (1 - fy) + bottom * fy


# ── Where features are ────────────────────────────────────────────────────


@dataclass(frozen=True)
class Region:
    """Where features are looked for: inside *outer*, outside *inner* (polygons, pixels); none is the whole frame."""

    outer: tuple[tuple[float, float], ...] | None = None
    inner: tuple[tuple[float, float], ...] | None = None

    def mask(self, size: tuple[int, int], homography: Any = None) -> Any:
        """The region in a frame of *size* (``W, H``) as a boolean mask, its polygons moved by *homography*."""
        import numpy as np
        from PIL import Image, ImageDraw

        width, height = size
        if self.outer is None:
            mask = Image.new("L", size, 255)
        else:
            mask = Image.new("L", size, 0)
            ImageDraw.Draw(mask).polygon(_moved(self.outer, homography), fill=255)
        if self.inner is not None:
            ImageDraw.Draw(mask).polygon(_moved(self.inner, homography), fill=0)
        return np.asarray(mask, dtype=bool)[:height, :width]

    def scaled(self, sx: float, sy: float) -> Region:
        def scale(polygon: tuple[tuple[float, float], ...] | None) -> tuple[tuple[float, float], ...] | None:
            return None if polygon is None else tuple((x * sx, y * sy) for x, y in polygon)

        return Region(scale(self.outer), scale(self.inner))


def _moved(polygon: tuple[tuple[float, float], ...], homography: Any) -> list[tuple[float, float]]:
    import numpy as np

    if homography is None:
        return [tuple(map(float, p)) for p in polygon]
    return [tuple(map(float, p)) for p in project(homography, np.asarray(polygon, dtype=float))]


def grown(quad: Any, by: float) -> tuple[tuple[float, float], ...]:
    """*quad*'s corners moved away from its centroid by *by* times their distance to it (negative: toward it)."""
    import numpy as np

    corners = np.asarray(quad, dtype=float)
    centre = corners.mean(axis=0)
    return tuple((float(x), float(y)) for x, y in centre + (corners - centre) * (1.0 + by))


def good_features(
    image: Any,
    mask: Any = None,
    *,
    max_points: int = 300,
    min_distance: int = 7,
    quality: float = 0.01,
) -> Any:
    """The strongest Shi-Tomasi corners of *image* (``N x 2``, x then y), inside *mask*, about *min_distance* apart."""
    import numpy as np
    from scipy import ndimage

    gx, gy = _gradients(image)
    sxx = ndimage.gaussian_filter(gx * gx, 1.5)
    syy = ndimage.gaussian_filter(gy * gy, 1.5)
    sxy = ndimage.gaussian_filter(gx * gy, 1.5)
    response = (sxx + syy) / 2 - np.sqrt(np.maximum(((sxx - syy) / 2) ** 2 + sxy * sxy, 0.0))
    border = WINDOW // 2 + 1
    response[:border, :] = response[-border:, :] = 0.0
    response[:, :border] = response[:, -border:] = 0.0
    if mask is not None:
        response[~np.asarray(mask, dtype=bool)] = 0.0
    peak = float(response.max())
    if peak <= 1e-6:
        return np.empty((0, 2), dtype=float)
    strongest = ndimage.maximum_filter(response, size=2 * min_distance + 1)
    ys, xs = np.nonzero((response == strongest) & (response > quality * peak))
    order = np.argsort(response[ys, xs])[::-1][:max_points]
    return np.column_stack([xs[order], ys[order]]).astype(float)


# ── How they move ─────────────────────────────────────────────────────────


def _offsets() -> tuple[Any, Any]:
    import numpy as np

    half = WINDOW // 2
    oy, ox = np.mgrid[-half : half + 1, -half : half + 1]
    return ox.ravel().astype(np.float32), oy.ravel().astype(np.float32)


def _lucas_kanade(before: list[Any], after: list[Any], points: Any) -> tuple[Any, Any]:
    """Where *points* of the pyramid *before* are in *after*, and whether each was followed (its window matched)."""
    import numpy as np

    ox, oy = _offsets()
    count = len(points)
    shift = np.zeros((count, 2), dtype=np.float64)
    alive = np.ones(count, dtype=bool)
    residual = np.zeros(count)
    for level in range(len(before) - 1, -1, -1):
        scale = 2.0**level
        image, target = before[level], after[level]
        gx, gy = _gradients(image)
        xs = points[:, 0:1] / scale + ox[None, :]
        ys = points[:, 1:2] / scale + oy[None, :]
        template = _sample(image, xs, ys)
        template = template - template.mean(axis=1, keepdims=True)
        grad_x, grad_y = _sample(gx, xs, ys), _sample(gy, xs, ys)
        gxx, gyy, gxy = (grad_x * grad_x).sum(1), (grad_y * grad_y).sum(1), (grad_x * grad_y).sum(1)
        det = gxx * gyy - gxy * gxy
        # A flat or one-edged window cannot say where it went: its weaker direction carries no gradient.
        weakest = (gxx + gyy) / 2 - np.sqrt(np.maximum(((gxx - gyy) / 2) ** 2 + gxy * gxy, 0.0))
        alive &= weakest > MIN_EIGENVALUE * WINDOW * WINDOW
        safe = np.where(det > 0, det, 1.0)
        for _ in range(ITERATIONS):
            moved = _sample(target, xs + shift[:, 0:1], ys + shift[:, 1:2])
            difference = template - (moved - moved.mean(axis=1, keepdims=True))
            bx, by = (difference * grad_x).sum(1), (difference * grad_y).sum(1)
            step = np.column_stack([(gyy * bx - gxy * by) / safe, (gxx * by - gxy * bx) / safe])
            step[~alive] = 0.0
            shift += step
            if float(np.abs(step).max(initial=0.0)) < _STEP_EPSILON:
                break
        if level == 0:
            moved = _sample(target, xs + shift[:, 0:1], ys + shift[:, 1:2])
            residual = np.abs(template - (moved - moved.mean(axis=1, keepdims=True))).mean(axis=1)
        else:
            shift *= 2.0
    found = points + shift
    height, width = before[0].shape
    half = WINDOW // 2
    inside = (found[:, 0] >= half) & (found[:, 0] < width - half) & (found[:, 1] >= half) & (found[:, 1] < height - half)
    return found, alive & inside & (residual < MAX_RESIDUAL) & np.isfinite(found).all(axis=1)


def track_points(before: list[Any], after: list[Any], points: Any, *, round_trip: bool = False) -> tuple[Any, Any]:
    """*points* of the frame whose pyramid is *before*, found in the frame of *after*; and which were followed.

    With *round_trip*, a point is kept only when tracking it back from where
    it was found lands within :data:`MAX_ROUND_TRIP` of where it was: twice
    the work, for a texture that repeats (RANSAC drops most such points anyway).
    """
    import numpy as np

    if len(points) == 0:
        return np.empty((0, 2)), np.zeros(0, dtype=bool)
    found, ok = _lucas_kanade(before, after, points)
    if not round_trip:
        return found, ok
    back, ok_back = _lucas_kanade(after, before, found)
    return found, ok & ok_back & (np.linalg.norm(back - points, axis=1) < MAX_ROUND_TRIP)


# ── The plane ─────────────────────────────────────────────────────────────


def project(homography: Any, points: Any) -> Any:
    """*points* (``N x 2``) mapped by *homography* (3x3)."""
    import numpy as np

    pts = np.asarray(points, dtype=float).reshape(-1, 2)
    mapped = np.column_stack([pts, np.ones(len(pts))]) @ np.asarray(homography, dtype=float).T
    return mapped[:, :2] / mapped[:, 2:3]


def _normaliser(points: Any) -> Any:
    """The similarity moving *points* to their centroid with a mean distance of sqrt(2) (Hartley)."""
    import numpy as np

    centre = points.mean(axis=0)
    spread = float(np.linalg.norm(points - centre, axis=1).mean()) or 1.0
    scale = np.sqrt(2.0) / spread
    return np.array([[scale, 0.0, -scale * centre[0]], [0.0, scale, -scale * centre[1]], [0.0, 0.0, 1.0]])


def fit_homography(source: Any, target: Any) -> Any | None:
    """The homography mapping *source* onto *target* (``N x 2``, N >= 4) in least squares; ``None`` when degenerate."""
    import numpy as np

    source, target = np.asarray(source, dtype=float), np.asarray(target, dtype=float)
    if len(source) < 4:
        return None
    ts, tt = _normaliser(source), _normaliser(target)
    a, b = project(ts, source), project(tt, target)
    rows = np.zeros((2 * len(a), 9))
    rows[0::2, 0:2], rows[0::2, 2] = a, 1.0
    rows[0::2, 6:8], rows[0::2, 8] = -a * b[:, 0:1], -b[:, 0]
    rows[1::2, 3:5], rows[1::2, 5] = a, 1.0
    rows[1::2, 6:8], rows[1::2, 8] = -a * b[:, 1:2], -b[:, 1]
    # The null vector of the rows: the eigenvector of their 9x9 normal matrix with the smallest eigenvalue.
    values, vectors = np.linalg.eigh(rows.T @ rows)
    if values[1] < 1e-9 * max(float(values[-1]), 1e-12):
        return None
    homography = np.linalg.inv(tt) @ vectors[:, 0].reshape(3, 3) @ ts
    if abs(homography[2, 2]) < 1e-12:
        return None
    return homography / homography[2, 2]


def _hypotheses(source: Any, target: Any, samples: Any) -> tuple[Any, Any]:
    """The homographies through each 4-point sample (normalised coordinates), and which samples gave one."""
    import numpy as np

    a, b = source[samples], target[samples]
    k = len(samples)
    rows = np.zeros((k, 8, 8))
    rhs = np.zeros((k, 8))
    rows[:, 0::2, 0:2], rows[:, 0::2, 2] = a, 1.0
    rows[:, 0::2, 6:8] = -a * b[:, :, 0:1]
    rows[:, 1::2, 3:5], rows[:, 1::2, 5] = a, 1.0
    rows[:, 1::2, 6:8] = -a * b[:, :, 1:2]
    rhs[:, 0::2], rhs[:, 1::2] = b[:, :, 0], b[:, :, 1]
    usable = np.abs(np.linalg.det(rows)) > 1e-10
    solved = np.zeros((k, 9))
    if usable.any():
        solved[usable, :8] = np.linalg.solve(rows[usable], rhs[usable][..., None])[..., 0]
    solved[:, 8] = 1.0
    return solved.reshape(k, 3, 3), usable


def ransac_homography(source: Any, target: Any, *, threshold: float = INLIER_PIXELS) -> tuple[Any | None, Any]:
    """The homography most of *source* -> *target* agree on (within *threshold* pixels), refitted on them; and which do."""
    import numpy as np

    source, target = np.asarray(source, dtype=float), np.asarray(target, dtype=float)
    count = len(source)
    none = np.zeros(count, dtype=bool)
    if count < 4:
        return None, none
    ts, tt = _normaliser(source), _normaliser(target)
    a, b = project(ts, source), project(tt, target)
    rng = np.random.default_rng(_SEED)
    samples = np.argsort(rng.random((RANSAC_HYPOTHESES, count)), axis=1)[:, :4]
    hypotheses, usable = _hypotheses(a, b, samples)
    homogeneous = np.column_stack([a, np.ones(count)])
    mapped = np.einsum("kij,nj->kni", hypotheses, homogeneous)
    with np.errstate(divide="ignore", invalid="ignore"):
        landed = mapped[..., :2] / mapped[..., 2:3]
    # Errors measured in pixels of the target: the normalisation's scale taken back out.
    error = np.linalg.norm(landed - b[None], axis=2) / tt[0, 0]
    inliers = np.where(np.isfinite(error), error, np.inf) < threshold
    inliers[~usable] = False
    best = int(np.argmax(inliers.sum(axis=1)))
    chosen = inliers[best]
    if chosen.sum() < 4:
        return None, none
    homography = None
    for _ in range(2):
        refit = fit_homography(source[chosen], target[chosen])
        if refit is None:
            break
        homography = refit
        chosen = np.linalg.norm(project(homography, source) - target, axis=1) < threshold
        if chosen.sum() < 4:
            return None, none
    return homography, chosen if homography is not None else none


# ── A plane through a video ───────────────────────────────────────────────


@dataclass(frozen=True)
class TrackStep:
    """One frame of a track: the homography from the reference frame to it, and the evidence for it."""

    homography: Any | None
    inliers: int
    points: int
    #: How many points were followed into the frame (before the points lost are replaced): what *inliers* is a share of.
    followed: int = 0

    @property
    def found(self) -> bool:
        return self.homography is not None


@dataclass(frozen=True)
class Plane:
    """One candidate of a frame: a homography from the reference frame, and which points followed agree on it."""

    homography: Any
    inliers: Any

    @property
    def count(self) -> int:
        return int(self.inliers.sum())


@dataclass(frozen=True)
class Proposal:
    """What a frame offers before a plane is adopted: its candidates, best first, and the points they were fitted on."""

    planes: tuple[Plane, ...]
    #: The points followed into the frame: where each is in the reference frame, and in this one, and whether it was
    #: found in the reference frame itself (never refilled).
    reference: Any
    current: Any
    original: Any
    #: How many points were followed into the frame, and how many agreed on its best homography (under the minimum too).
    followed: int
    best: int
    after: list[Any]
    frame: Any


class PlanarTracker:
    """Follows the plane of *region* from a *reference* grey frame through the frames given to :meth:`step`.

    Each frame's homography maps the reference frame onto it. A frame with
    fewer than *min_inliers* points agreeing on one is not found (the
    tracker's state stays at the last frame found); the region is searched
    again for points when fewer than *refill_below* are left. *round_trip*
    checks every point backwards too (:func:`track_points`).
    """

    def __init__(
        self,
        reference: Any,
        region: Region | None = None,
        *,
        max_points: int = 200,
        min_distance: int = 7,
        min_inliers: int = 12,
        refill_below: int | None = None,
        threshold: float = INLIER_PIXELS,
        round_trip: bool = False,
    ) -> None:
        import numpy as np

        self.region = region or Region()
        self.size = (reference.shape[1], reference.shape[0])
        self.max_points, self.min_distance = max_points, min_distance
        self.min_inliers = min_inliers
        self.refill_below = refill_below if refill_below is not None else max(min_inliers * 2, max_points // 2)
        self.threshold = threshold
        self.round_trip = round_trip
        self.levels = pyramid(reference)
        self.reference_points = good_features(
            reference, self.region.mask(self.size), max_points=max_points, min_distance=min_distance
        )
        self.points = self.reference_points.copy()
        #: Which points were found in the reference frame itself: a point refilled later sits where the homography
        #: put the region, which on a surface that rolls is no longer where it was.
        self.original = np.ones(len(self.points), dtype=bool)
        self.homography = np.eye(3)

    @property
    def features(self) -> int:
        """How many points the tracker follows."""
        return len(self.points)

    def step(self, frame: Any) -> TrackStep:
        """The homography from the reference frame to *frame* (grey, the reference's size)."""
        proposal = self.propose(frame)
        if not proposal.planes:
            return TrackStep(None, proposal.best, proposal.followed, proposal.followed)
        return self.adopt(proposal)

    def propose(self, frame: Any, *, planes: int = 1) -> Proposal:
        """The candidates of *frame*, the tracker unchanged: up to *planes* homographies with at least *min_inliers* each.

        The first is the one most points agree on; each next one is fitted on
        the points every one before it left out.
        """
        import numpy as np

        after = pyramid(frame)
        found, ok = track_points(self.levels, after, self.points, round_trip=self.round_trip)
        reference, current, original = self.reference_points[ok], found[ok], self.original[ok]
        candidates: list[Plane] = []
        left = np.ones(len(reference), dtype=bool)
        best = 0
        while len(candidates) < planes and int(left.sum()) >= (self.min_inliers if candidates else 4):
            homography, agree = ransac_homography(reference[left], current[left], threshold=self.threshold)
            count = int(agree.sum()) if homography is not None else 0
            if not candidates:
                best = count
            if homography is None or count < self.min_inliers:
                break
            inliers = np.zeros(len(reference), dtype=bool)
            inliers[np.flatnonzero(left)[agree]] = True
            candidates.append(Plane(homography, inliers))
            left &= ~inliers
        return Proposal(tuple(candidates), reference, current, original, int(ok.sum()), best, after, frame)

    def adopt(self, proposal: Proposal, index: int = 0) -> TrackStep:
        """The tracker moved onto candidate *index* of *proposal*: its homography, its points, the frame it was found in."""
        plane = proposal.planes[index]
        self.homography = plane.homography
        self.reference_points, self.points = proposal.reference[plane.inliers], proposal.current[plane.inliers]
        self.original = proposal.original[plane.inliers]
        if len(self.points) < self.refill_below:
            self._refill(proposal.frame)
        self.levels = proposal.after
        return TrackStep(plane.homography, plane.count, len(self.points), proposal.followed)

    def _refill(self, frame: Any) -> None:
        """New points where the homography puts the region in *frame*, their reference positions taken back through it."""
        import numpy as np

        mask = self.region.mask(self.size, self.homography)
        fresh = good_features(frame, mask, max_points=self.max_points, min_distance=self.min_distance)
        if len(fresh) == 0:
            return
        if len(self.points):
            nearest = np.min(np.linalg.norm(fresh[:, None, :] - self.points[None, :, :], axis=2), axis=1)
            fresh = fresh[nearest >= self.min_distance]
        fresh = fresh[: max(0, self.max_points - len(self.points))]
        if len(fresh) == 0:
            return
        self.points = np.vstack([self.points, fresh])
        self.reference_points = np.vstack([self.reference_points, project(np.linalg.inv(self.homography), fresh)])
        self.original = np.concatenate([self.original, np.zeros(len(fresh), dtype=bool)])
