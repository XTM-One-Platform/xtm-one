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

"""The camera's motion through an AI shot, measured on its frames.

A video model moves its camera as it likes - a push-in, a pan, a slow drift -
whatever its prompt asks. What the platform draws over a shot after it is made
(a region kept from the start picture, a screen cleaned) has to move with that
camera, so the motion is measured, never assumed: a similarity (scale,
rotation, translation) from a reference picture to each frame, fitted to their
grey levels by a coarse-to-fine Gauss-Newton alignment (Lucas-Kanade):

* each picture is brought to zero mean and unit spread, so a model shifting
  its exposure or grading does not read as a move;
* the residuals are weighted by Huber's function, so what moves inside the
  picture - people, leaves, light - pulls on the fit far less than the room;
* the caller's *weight* leaves out what is not to be trusted (a region whose
  text the model redraws, a screen whose content changes);
* :attr:`Fit.score` is the correlation of the aligned pictures over the
  weighted pixels: a fit under :data:`MIN_SCORE` did not find the reference
  in the frame, and its caller says so rather than drawing in the wrong place.

The pictures are read at most :data:`ANALYSIS_SIDE` long; :meth:`Similarity.scaled`
carries a motion to the frame's own pixels.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

#: The long side of the grey copies the motion is measured on: the room's structure, not its noise.
ANALYSIS_SIDE = 400
#: Pyramid levels, each half the size of the one above.
LEVELS = 3
#: Gauss-Newton steps at most per level, and how little (pixels at the picture's corners) a converged step moves it.
ITERATIONS = 30
_CONVERGED_PIXELS = 0.05
#: Huber's constant, in units of the residuals' robust spread.
_HUBER = 1.345
#: The correlation of the aligned pictures under which a fit did not find its reference.
MIN_SCORE = 0.55
#: Frames averaged around each one when a track is smoothed.
SMOOTH_WINDOW = 5


@dataclass(frozen=True)
class Similarity:
    """``x' = a x - b y + tx``, ``y' = b x + a y + ty``: a uniform scale, a rotation and a translation."""

    a: float = 1.0
    b: float = 0.0
    tx: float = 0.0
    ty: float = 0.0

    @property
    def scale(self) -> float:
        return math.hypot(self.a, self.b)

    def apply(self, points: Any) -> Any:
        """*points* (``N x 2``) moved by the similarity."""
        import numpy as np

        p = np.asarray(points, dtype=float).reshape(-1, 2)
        return np.column_stack([self.a * p[:, 0] - self.b * p[:, 1] + self.tx, self.b * p[:, 0] + self.a * p[:, 1] + self.ty])

    def inverse(self) -> Similarity:
        norm = self.a * self.a + self.b * self.b
        a, b = self.a / norm, -self.b / norm
        return Similarity(a, b, -(a * self.tx - b * self.ty), -(b * self.tx + a * self.ty))

    def then(self, other: Similarity) -> Similarity:
        """*self* followed by *other*."""
        return Similarity(
            other.a * self.a - other.b * self.b,
            other.a * self.b + other.b * self.a,
            other.a * self.tx - other.b * self.ty + other.tx,
            other.b * self.tx + other.a * self.ty + other.ty,
        )

    def scaled(self, factor: float) -> Similarity:
        """The same motion in pictures *factor* times larger (both sides)."""
        return Similarity(self.a, self.b, self.tx * factor, self.ty * factor)

    def coefficients(self) -> tuple[float, float, float, float, float, float]:
        """Pillow's ``AFFINE`` data: the inverse map, output pixel to source pixel."""
        inverse = self.inverse()
        return (inverse.a, -inverse.b, inverse.tx, inverse.b, inverse.a, inverse.ty)


@dataclass(frozen=True)
class Fit:
    motion: Similarity
    #: The correlation of the reference and the frame once aligned, over the weighted pixels (-1 to 1).
    score: float

    @property
    def found(self) -> bool:
        return self.score >= MIN_SCORE


def grey(rgb: Any, side: int = ANALYSIS_SIDE) -> tuple[Any, float]:
    """``(grey copy at most *side* long, its size over the original's)`` of an ``H x W x 3`` array or a Pillow image."""
    import numpy as np
    from PIL import Image

    picture = rgb if isinstance(rgb, Image.Image) else Image.fromarray(np.asarray(rgb, dtype=np.uint8))
    factor = min(1.0, side / max(picture.size))
    if factor < 1.0:
        size = (max(8, round(picture.width * factor)), max(8, round(picture.height * factor)))
        picture = picture.resize(size, Image.Resampling.BOX)
    return np.asarray(picture.convert("L"), dtype=np.float32), factor


def _normalised(image: Any, weight: Any) -> Any:
    total = float(weight.sum())
    if total <= 0:
        return image - float(image.mean())
    mean = float((image * weight).sum() / total)
    spread = math.sqrt(max(float((((image - mean) ** 2) * weight).sum() / total), 1e-6))
    return (image - mean) / spread


def _down(image: Any) -> Any:
    from scipy import ndimage

    return ndimage.zoom(ndimage.gaussian_filter(image, 1.0), 0.5, order=1)


def _pyramid(image: Any, levels: int) -> list[Any]:
    stack = [image]
    for _ in range(levels - 1):
        if min(stack[-1].shape) < 32:
            break
        stack.append(_down(stack[-1]))
    return stack


def _refine(reference: Any, image: Any, weight: Any, start: Similarity) -> Similarity:
    """Gauss-Newton steps of the similarity at one level, from *start*.

    A gain and an offset of the reference's grey levels are fitted with it:
    the frame is normalised over all of it, the reference over its weighted
    part only, and the model drifts its exposure besides.
    """
    import numpy as np
    from scipy import ndimage

    height, width = reference.shape
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
    # Centred coordinates keep the four parameters apart (a zoom about the corner is a translation too).
    cx, cy = (width - 1) / 2.0, (height - 1) / 2.0
    x, y = xs - cx, ys - cy
    grad_y, grad_x = np.gradient(image)
    a, b = start.a, start.b
    # The translation about the centre: x' = a x - b y + cx + u.
    u = start.tx + start.a * cx - start.b * cy - cx
    v = start.ty + start.b * cx + start.a * cy - cy
    img_h, img_w = image.shape
    reach = math.hypot(cx, cy)
    gain, offset = 1.0, 0.0
    for _ in range(ITERATIONS):
        xw = a * x - b * y + cx + u
        yw = b * x + a * y + cy + v
        inside = (xw >= 0) & (xw <= img_w - 1) & (yw >= 0) & (yw <= img_h - 1)
        coords = np.stack([yw, xw])
        warped = ndimage.map_coordinates(image, coords, order=1, mode="nearest")
        gx = ndimage.map_coordinates(grad_x, coords, order=1, mode="nearest")
        gy = ndimage.map_coordinates(grad_y, coords, order=1, mode="nearest")
        residual = warped - (gain * reference + offset)
        w = weight * inside
        mask = w > 0
        if mask.sum() < 32:
            break
        spread = 1.4826 * float(np.median(np.abs(residual[mask]))) + 1e-6
        limit = _HUBER * spread
        magnitude = np.abs(residual)
        w = w * np.where(magnitude <= limit, 1.0, limit / np.maximum(magnitude, 1e-9))
        jacobian = np.stack(
            [gx * x + gy * y, -gx * y + gy * x, gx, gy, -reference, -np.ones_like(reference)], axis=-1
        )[mask]
        weights = w[mask]
        hessian = (jacobian * weights[:, None]).T @ jacobian
        gradient = (jacobian * (weights * residual[mask])[:, None]).sum(axis=0)
        try:
            step = -np.linalg.solve(hessian + np.eye(6) * 1e-6, gradient)
        except np.linalg.LinAlgError:
            break
        a, b, u, v = a + step[0], b + step[1], u + step[2], v + step[3]
        gain, offset = gain + step[4], offset + step[5]
        # How far the step moved the picture's corners: under a twentieth of a pixel, it has converged.
        if (abs(step[0]) + abs(step[1])) * reach + math.hypot(step[2], step[3]) < _CONVERGED_PIXELS:
            break
    return Similarity(a, b, u + cx - a * cx + b * cy, v + cy - b * cx - a * cy)


def _score(reference: Any, image: Any, weight: Any, motion: Similarity) -> float:
    import numpy as np
    from scipy import ndimage

    height, width = reference.shape
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
    xw = motion.a * xs - motion.b * ys + motion.tx
    yw = motion.b * xs + motion.a * ys + motion.ty
    inside = (xw >= 0) & (xw <= image.shape[1] - 1) & (yw >= 0) & (yw <= image.shape[0] - 1)
    w = weight * inside
    total = float(w.sum())
    if total < 32:
        return 0.0
    warped = ndimage.map_coordinates(image, np.stack([yw, xw]), order=1, mode="nearest")
    ra = reference - float((reference * w).sum() / total)
    rb = warped - float((warped * w).sum() / total)
    denominator = math.sqrt(float((ra * ra * w).sum()) * float((rb * rb * w).sum()))
    return float((ra * rb * w).sum() / denominator) if denominator > 0 else 0.0


def align(reference: Any, image: Any, *, init: Similarity | None = None, weight: Any = None) -> Fit:
    """The similarity taking *reference*'s pixels to *image*'s (two grey arrays), and how well it aligns them.

    *init* is where to start (the previous frame's motion); *weight* (``0-1``,
    *reference*'s shape) the share each reference pixel counts for.
    """
    import numpy as np

    reference = np.asarray(reference, dtype=np.float32)
    image = np.asarray(image, dtype=np.float32)
    weight = np.ones_like(reference) if weight is None else np.clip(np.asarray(weight, dtype=np.float32), 0.0, 1.0)
    ref_n = _normalised(reference, weight)
    img_n = _normalised(image, np.ones_like(image))
    references, images, weights = _pyramid(ref_n, LEVELS), _pyramid(img_n, LEVELS), _pyramid(weight, LEVELS)
    levels = min(len(references), len(images), len(weights))
    motion = init or Similarity()
    for level in range(levels - 1, -1, -1):
        factor = 0.5**level
        at_level = _refine(references[level], images[level], np.clip(weights[level], 0, 1), motion.scaled(factor))
        motion = at_level.scaled(1.0 / factor)
    return Fit(motion, round(_score(ref_n, img_n, weight, motion), 4))


def smooth(motions: Sequence[Similarity], found: Sequence[bool], window: int = SMOOTH_WINDOW) -> list[Similarity]:
    """Every frame's motion: a frame not found taken between its found neighbours, then averaged over *window*."""
    import numpy as np
    from scipy.ndimage import uniform_filter1d

    track = np.array([[m.a, m.b, m.tx, m.ty] for m in motions], dtype=float)
    seen = np.array(found, dtype=bool)
    if len(track) and seen.any() and not seen.all():
        frames = np.arange(len(track))
        for column in range(4):
            track[:, column] = np.interp(frames, frames[seen], track[seen, column])
    if len(track) > 2 and window > 1:
        track = uniform_filter1d(track, size=min(window, len(track)), axis=0, mode="nearest")
    return [Similarity(*row) for row in track]


def longest_run(found: Sequence[bool]) -> int:
    """The most frames in a row not found."""
    longest = run = 0
    for hit in found:
        run = 0 if hit else run + 1
        longest = max(longest, run)
    return longest
