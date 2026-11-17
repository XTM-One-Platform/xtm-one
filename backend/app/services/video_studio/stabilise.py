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

"""A drifting AI shot stabilised in code: the camera locked off after the shot is made.

A video model asked for a locked-off camera often lets it drift: a slow
push, a pan of a few pixels a second. Measured on the take's first and last
frames (``frame_qc.camera_motion``), that drift fails a shot asked with
``max_camera_motion`` and flags one whose prompt asks for a still camera,
though most of it is one global motion of the whole frame, which code undoes:

1. **Estimate.** The take is decoded small (:data:`ESTIMATE_SIDE`) and a
   ``tracking.PlanarTracker`` follows features of the whole frame from frame
   0: each frame's homography against frame 0, RANSAC keeping what the camera
   moves and leaving out what moves in the scene.
2. **Crop.** Every frame warped back onto frame 0 leaves a border its own
   picture does not cover. The largest window of the frame's shape inside
   every warped frame (:func:`crop_window`) is kept, scaled back up to the
   frame's size.
3. **Warp.** Each frame is drawn through its homography and the crop in one
   perspective transform (Pillow, bicubic) and the take re-encoded.

The camera is the plane the background moves by, never what moves in front
of it: each frame offers the two planes most points agree on
(``PlanarTracker.propose``) and the one closest to the camera's last
position is adopted, so a large subject crossing the frame, more textured
than the room behind it, is never taken for the camera. The camera's own
motion from the first frame to the last, as a share of the frame
(:func:`camera_share`), is what ``max_camera_motion`` is held to
(:func:`camera_of`); the whole frame's change (``1 - SSIM``) is reported
beside it.

The stabilised take replaces the raw one only when it moves less (its
residual motion, ``1 - SSIM`` of its first and last frames, under the raw
take's by :data:`MIN_GAIN`) and keeps at least :data:`MIN_KEPT_SHARE` of the
frame. Otherwise the raw take stays the take, with the reason: its camera
could not be followed (too few features agree), it did not drift, the drift
is too large to crop, or locking it would not make it steadier.

A screen the shot shows (``keep``: a ``screen`` replaced in it) is
never cropped into: the window kept must hold it whole, or the take is not
stabilised. The record keeps each frame's map from a point of the stabilised
take to the same point of the raw one (:data:`MAPS`, 0-1 both), so a screen
insert followed through the raw take is placed where the stabilised take
draws its screen (``insert_tracking.track_stabilised``).
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from dataclasses import dataclass
from typing import Any

from app.services.video_studio.frame_qc import STABILISATION_MAPS

#: The longest side the motion is estimated at: a camera's drift moves the whole frame, not its detail.
ESTIMATE_SIDE = 640
#: A drift under this share of the frame's diagonal (its largest corner displacement) is no drift.
MIN_DRIFT = 0.002
#: The least share of the frame's side a stabilised take keeps: past it, the crop zooms in too far.
MIN_KEPT_SHARE = 0.8
#: The camera is lost on a frame where fewer than this many points agree on its homography.
MIN_INLIERS = 24
#: The stabilised take replaces the raw one only when it moves at least this much less (1 - SSIM).
MIN_GAIN = 0.01
#: How far from the frame's centre the window kept may sit (a share of its side), searched on a grid.
_CENTRE_RANGE = 0.12
_CENTRE_STEPS = 9
_SHARE_STEPS = 18
#: The record's key of each frame's map, stabilised take -> raw take (0-1 both): eight coefficients of a 3x3 map.
MAPS = STABILISATION_MAPS
#: The significant digits a map's coefficient keeps: well under a pixel of a 4K frame.
_MAP_DIGITS = 7
#: The planes each frame offers the camera: the background and what crosses in front of it.
_CAMERA_PLANES = 2
#: The record's keys of the camera's own motion, first frame to last (a share of the frame, :func:`camera_share`):
#: the raw take's, and the stabilised take's when it is the one that plays.
CAMERA_MOTION = "camera_motion"
RESIDUAL_CAMERA_MOTION = "residual_camera_motion"


@dataclass
class Stabilised:
    """What stabilising a take did: ``applied`` (the stabilised take is written), or why not."""

    applied: bool
    frames: int = 0
    reason: str | None = None
    #: The raw take's motion: ``1 - SSIM`` of its first and last frames (``frame_qc.camera_motion``).
    raw_motion: float | None = None
    #: The same, once stabilised.
    residual_motion: float | None = None
    #: The largest corner displacement corrected, as a share of the frame's diagonal.
    max_drift: float | None = None
    #: The share of the frame's side the stabilised take keeps.
    kept_share: float | None = None
    #: The part of the first frame it keeps, 0-1: ``x0, y0, x1, y1`` (what its start picture is compared on).
    crop: tuple[float, float, float, float] | None = None
    #: The frame the camera was lost on.
    lost_at: int | None = None
    #: Each frame's map from a point of the stabilised take to the same point of the raw take (:data:`MAPS`).
    maps: list[list[float]] | None = None
    #: The camera's own motion from the first frame to the last, measured on the background (:func:`camera_share`).
    camera_motion: float | None = None
    #: The same, once stabilised.
    residual_camera_motion: float | None = None

    def as_params(self) -> dict:
        """The record a shot keeps with its take (``frame_qc``): rounded, the unknowns left out."""
        values = {
            "applied": self.applied,
            "reason": self.reason,
            "raw_motion": self.raw_motion,
            "residual_motion": self.residual_motion,
            "max_drift": self.max_drift,
            "kept_share": self.kept_share,
            "crop": [round(v, 4) for v in self.crop] if self.crop else None,
            "lost_at": self.lost_at,
            CAMERA_MOTION: self.camera_motion,
            RESIDUAL_CAMERA_MOTION: self.residual_camera_motion,
        }
        record = {key: (round(value, 4) if isinstance(value, float) else value) for key, value in values.items() if value is not None}
        if self.maps:
            record[MAPS] = self.maps
        return record


#: Why a take with nothing to correct is left as it is: said in its record, never as a note.
NO_DRIFT = "its camera did not drift"


def note(record: dict) -> str | None:
    """The note a shot carries of its stabilisation, from its record (:meth:`Stabilised.as_params`).

    ``None`` for a take whose camera did not drift: there was nothing to say.
    """
    raw, residual = record.get("raw_motion"), record.get("residual_motion")
    if not record.get("applied") and record.get("reason") == NO_DRIFT:
        return None
    if record.get("applied"):
        return (
            f"Stabilised: the camera drifted up to {float(record.get('max_drift') or 0):.1%} of the frame; each frame "
            f"was locked to the first and {float(record.get('kept_share') or 1):.0%} of the frame kept, its motion "
            f"{float(raw or 0):.2f} -> {float(residual or 0):.2f} (1 - SSIM). The un-stabilised take is kept beside it."
        )
    measured = f" (its motion {float(raw):.2f}, 1 - SSIM)" if raw is not None else ""
    return f"Not stabilised: {record.get('reason') or 'no reason given'}{measured}; the take is the un-stabilised one."


# ── The geometry ──────────────────────────────────────────────────────────


def to_full(homography: Any, sx: float, sy: float) -> Any:
    """A homography measured on frames scaled by ``1/sx, 1/sy``, in the full frame's pixels."""
    import numpy as np

    scale = np.diag([sx, sy, 1.0])
    full = scale @ homography @ np.linalg.inv(scale)
    return full / full[2, 2]


def _corners(size: tuple[int, int]) -> Any:
    import numpy as np

    width, height = size
    return np.array([[0.0, 0.0], [width, 0.0], [width, height], [0.0, height]])


def camera_share(homography: Any, size: tuple[int, int]) -> float:
    """How far *homography* moves the frame: the largest corner displacement along each axis, as a share of that side.

    A pan of a fifth of the frame's width is 0.2, whatever the frame's size: the share is the same on the small
    frames the motion is estimated on and on the take itself.
    """
    import numpy as np

    from app.services.video_studio.tracking import project

    width, height = size
    corners = _corners(size)
    moved = np.abs(project(homography, corners) - corners)
    return round(float(max(moved[:, 0].max() / width, moved[:, 1].max() / height)), 4)


def drift(homographies: list[Any], size: tuple[int, int]) -> float:
    """The largest distance a corner of the frame moved, over every frame, as a share of the frame's diagonal."""
    import numpy as np

    from app.services.video_studio.tracking import project

    corners = _corners(size)
    largest = max((float(np.linalg.norm(project(h, corners) - corners, axis=1).max()) for h in homographies), default=0.0)
    return largest / math.hypot(*size)


@dataclass(frozen=True)
class Window:
    """The part of frame 0 every stabilised frame covers: its top-left corner and the share of the frame's side."""

    x: float
    y: float
    share: float

    def to_frame(self) -> Any:
        """The 3x3 map from an output pixel to frame 0's pixels."""
        import numpy as np

        return np.array([[self.share, 0.0, self.x], [0.0, self.share, self.y], [0.0, 0.0, 1.0]])


def crop_window(homographies: list[Any], size: tuple[int, int], keep: list[Any] | None = None) -> Window | None:
    """The largest window of the frame's shape, in frame 0's pixels, that every frame warped back onto frame 0 covers.

    Frame *i*'s picture lands on frame 0 inside the quad ``H_i^-1(frame)``; a
    window is kept when its four corners are inside every such quad (each is
    convex). Its centre is searched on a grid around the frame's centre, its
    size by bisection. *keep* (quads in frame 0's pixels: a screen the
    shot shows) must lie inside the window: a centre whose largest window
    cuts into one is not taken. ``None`` when no window holds.
    """
    import numpy as np

    from app.services.video_studio.tracking import project

    width, height = size
    rect = _corners(size)
    kept = np.asarray(keep, dtype=float).reshape(-1, 2) if keep else np.empty((0, 2))

    def holds(x0: float, y0: float, w: float, h: float) -> bool:
        if not len(kept):
            return True
        return bool(((kept[:, 0] >= x0) & (kept[:, 0] <= x0 + w) & (kept[:, 1] >= y0) & (kept[:, 1] <= y0 + h)).all())

    quads = np.array([project(np.linalg.inv(h), rect) for h in homographies])
    edges = np.roll(quads, -1, axis=1) - quads
    # Each quad's own turning sign: a frame mirrored by a bad estimate counts as covering nothing.
    turn = np.sign(edges[:, 0, 0] * edges[:, 1, 1] - edges[:, 0, 1] * edges[:, 1, 0])[:, None, None]

    def covered(corners: Any) -> bool:
        towards = corners[None, None, :, :] - quads[:, :, None, :]
        cross = edges[:, :, None, 0] * towards[..., 1] - edges[:, :, None, 1] * towards[..., 0]
        return bool(((turn * cross) >= -1e-6).all())

    best: Window | None = None
    for dx in np.linspace(-_CENTRE_RANGE, _CENTRE_RANGE, _CENTRE_STEPS):
        for dy in np.linspace(-_CENTRE_RANGE, _CENTRE_RANGE, _CENTRE_STEPS):
            cx, cy = width * (0.5 + dx), height * (0.5 + dy)
            low, high = 0.0, 1.0
            for _ in range(_SHARE_STEPS):
                share = (low + high) / 2
                w, h = width * share, height * share
                x0, y0 = cx - w / 2, cy - h / 2
                corners = np.array([[x0, y0], [x0 + w, y0], [x0 + w, y0 + h], [x0, y0 + h]])
                if covered(corners):
                    low = share
                else:
                    high = share
            x0, y0 = cx - width * low / 2, cy - height * low / 2
            if low > 0 and (best is None or low > best.share) and holds(x0, y0, width * low, height * low):
                best = Window(x0, y0, low)
    return best


def normalised_maps(maps: list[Any], size: tuple[int, int]) -> list[list[float]]:
    """Each frame's map (output pixel -> its raw pixel) as one from a 0-1 point of the output to a 0-1 point of the raw frame.

    Eight coefficients each, row by row (the ninth is 1), to :data:`_MAP_DIGITS` significant digits.
    """
    import numpy as np

    width, height = size
    scale = np.diag([float(width), float(height), 1.0])
    out = []
    for matrix in maps:
        normalised = np.linalg.inv(scale) @ np.asarray(matrix, dtype=float) @ scale
        normalised = normalised / normalised[2, 2]
        out.append([float(f"{value:.{_MAP_DIGITS}g}") for value in normalised.ravel()[:8]])
    return out


def map_matrix(coefficients: list[float]) -> Any:
    """A frame's map (:data:`MAPS`) as its 3x3 matrix."""
    import numpy as np

    return np.array([*map(float, coefficients[:8]), 1.0]).reshape(3, 3)


def raw_take(params: dict | None) -> tuple[str, list[list[float]], float | None] | None:
    """The un-stabilised take a shot's playing take was stabilised from: its file, each frame's map and the share kept.

    ``None`` when the take that plays was not stabilised, or was before the maps were kept.
    """
    from app.services.video_studio import clips, frame_qc

    qc = clips.take_params(params).get(frame_qc.QC_KEY)
    record = qc.get(frame_qc.STABILISATION) if isinstance(qc, dict) else None
    if not isinstance(record, dict) or not record.get("applied") or not record.get(frame_qc.UNSTABILISED):
        return None
    maps = record.get(MAPS)
    if not isinstance(maps, list) or not maps or not all(isinstance(m, list) and len(m) == 8 for m in maps):
        return None
    share = record.get("kept_share")
    return str(record[frame_qc.UNSTABILISED]), maps, float(share) if isinstance(share, int | float) else None


def _coefficients(matrix: Any) -> list[float]:
    """Pillow's ``PERSPECTIVE`` coefficients of a 3x3 map from output pixels to the input picture."""
    normalised = matrix / matrix[2, 2]
    return [float(value) for value in normalised.ravel()[:8]]


def _drawn(rgb: Any, matrix: Any, size: tuple[int, int]) -> Any:
    """*rgb* (``H x W x 3``) drawn through *matrix* (output pixel -> its pixel) at *size*, as a Pillow image."""
    from PIL import Image

    picture = Image.fromarray(rgb)
    return picture.transform(size, Image.Transform.PERSPECTIVE, _coefficients(matrix), resample=Image.Resampling.BICUBIC)


def _motion(first: Any, last: Any) -> float:
    from app.services.video_studio import frame_qc

    # Measured on the small frames on purpose: ``frame_qc.ssim`` compares grey copies SSIM_SIDE long whatever it is
    # given, so this is the figure ``frame_qc.camera_motion`` reads off the stored full-size take.
    return round(min(1.0, max(0.0, 1.0 - frame_qc.ssim(first, last))), 4)


# ── The take ──────────────────────────────────────────────────────────────


def _tracker(grey: Any) -> Any:
    from app.services.video_studio.tracking import PlanarTracker, Region

    height, width = grey.shape
    margin = 0.04
    inset = (
        (width * margin, height * margin),
        (width * (1 - margin), height * margin),
        (width * (1 - margin), height * (1 - margin)),
        (width * margin, height * (1 - margin)),
    )
    return PlanarTracker(grey, Region(outer=inset), max_points=240, min_inliers=MIN_INLIERS)


def _camera_step(tracker: Any, grey: Any) -> Any:
    """The camera followed into *grey*: of the planes the frame offers, the one closest to where it was.

    The plane most points agree on is the camera's only while the background
    holds most of the detail: a subject filling the frame outvotes it. The
    camera moves little from one frame to the next, what crosses in front of
    it moves on its own, so the plane nearest the last homography is the camera's.
    """
    import numpy as np

    from app.services.video_studio.tracking import TrackStep, project

    proposal = tracker.propose(grey, planes=_CAMERA_PLANES)
    if not proposal.planes:
        return TrackStep(None, proposal.best, proposal.followed, proposal.followed)
    corners = _corners(tracker.size)
    were = project(tracker.homography, corners)
    moved = [float(np.linalg.norm(project(plane.homography, corners) - were, axis=1).max()) for plane in proposal.planes]
    return tracker.adopt(proposal, int(np.argmin(moved)))


def background_motion(first: Any, last: Any) -> float | None:
    """The camera's motion from *first* to *last* (``H x W x 3`` RGB, the same size), read on the two frames alone.

    For a take whose camera barely moves - a stabilised one: what
    crosses the frame is left out like in :func:`estimate`. ``None`` when
    the camera cannot be followed between the two.
    """
    import numpy as np

    from app.services.video_studio.tracking import luma

    tracker = _tracker(luma(np.asarray(first)))
    if tracker.features < MIN_INLIERS:
        return None
    step = _camera_step(tracker, luma(np.asarray(last)))
    return camera_share(step.homography, tracker.size) if step.found else None


async def estimate(video_path: str, *, size: tuple[int, int], fps: float) -> tuple[list[Any], Any, Any, Stabilised | None]:
    """Each frame's homography against frame 0 at *size* (the small frames), the first and last small frames.

    The last item is a :class:`Stabilised` saying why the camera could not be
    followed, else ``None``.
    """
    import numpy as np

    from app.services.video_studio import encoder
    from app.services.video_studio.tracking import luma

    width, height = size
    homographies: list[Any] = []
    tracker = None
    first = last = None
    frames = encoder.iter_rgb_frames(video_path, width=width, height=height, resize=True)
    async with contextlib.aclosing(frames):
        async for raw in frames:
            rgb = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
            grey = luma(rgb)
            if tracker is None:
                tracker = await asyncio.to_thread(_tracker, grey)
                if tracker.features < MIN_INLIERS:
                    return [], rgb, rgb, Stabilised(False, reason="the take has too little detail to follow its camera")
                homographies.append(np.eye(3))
                first = rgb
            else:
                step = await asyncio.to_thread(_camera_step, tracker, grey)
                if not step.found:
                    index = len(homographies)
                    return (
                        homographies,
                        first,
                        last,
                        Stabilised(
                            False,
                            frames=index,
                            reason=f"its camera could not be followed past frame {index - 1} ({(index - 1) / fps:.1f} s)",
                            lost_at=index,
                        ),
                    )
                homographies.append(step.homography)
            last = rgb
    if len(homographies) < 2:
        return homographies, first, last, Stabilised(False, frames=len(homographies), reason="the take has a single frame")
    return homographies, first, last, None


async def stabilise(video_path: str, out_path: str, *, keep: list[Any] | None = None) -> Stabilised:
    """*video_path* stabilised to *out_path* (see the module); when not ``applied``, *out_path* is not written.

    *keep* are quads on its first frame (0-1), a screen it shows: the crop never cuts into one.
    """
    from PIL import Image

    from app.services.video_studio import encoder

    info = await encoder.probe(video_path)
    width, height = int(getattr(info, "width", 0) or 0), int(getattr(info, "height", 0) or 0)
    fps = float(getattr(info, "fps", None) or 24.0)
    if width < 16 or height < 16:
        return Stabilised(False, reason="the take's size could not be read")
    scale = min(1.0, ESTIMATE_SIDE / max(width, height))
    small = (max(16, round(width * scale / 2) * 2), max(16, round(height * scale / 2) * 2))
    homographies, first, last, lost = await estimate(video_path, size=small, fps=fps)
    raw_motion = _motion(Image.fromarray(first), Image.fromarray(last)) if first is not None else None
    if lost is not None:
        lost.raw_motion = raw_motion
        return lost
    sx, sy = width / small[0], height / small[1]
    full = [to_full(h, sx, sy) for h in homographies]
    drifted = round(drift(full, (width, height)), 4)
    outcome = Stabilised(
        False,
        frames=len(full),
        raw_motion=raw_motion,
        max_drift=drifted,
        camera_motion=camera_share(homographies[-1], small),
    )
    if drifted < MIN_DRIFT:
        outcome.reason = NO_DRIFT
        return outcome
    screens = [[(float(x) * width, float(y) * height) for x, y in quad] for quad in keep or []]
    window = await asyncio.to_thread(crop_window, full, (width, height), screens)
    if window is None or window.share < MIN_KEPT_SHARE:
        kept = window.share if window is not None else 0.0
        outcome.kept_share = round(kept, 4)
        free = await asyncio.to_thread(crop_window, full, (width, height)) if screens else None
        if free is not None and free.share >= MIN_KEPT_SHARE:
            outcome.reason = (
                f"its camera drifted {drifted:.1%} of the frame: locking it would crop into its screen "
                f"(a window holding the whole screen keeps only {kept:.0%} of the frame)"
            )
            return outcome
        outcome.reason = f"its camera drifted {drifted:.1%} of the frame: locking it would keep only {kept:.0%} of the frame"
        return outcome
    outcome.kept_share = round(window.share, 4)
    # The residual, on the small frames: the first and the last drawn as the stabilised take draws them.
    small_window = Window(window.x / sx, window.y / sy, window.share)
    crop = small_window.to_frame()
    steady_first, steady_last = _drawn(first, crop, small), _drawn(last, homographies[-1] @ crop, small)
    residual = _motion(steady_first, steady_last)
    outcome.residual_motion = residual
    outcome.residual_camera_motion = await asyncio.to_thread(background_motion, steady_first, steady_last)
    if raw_motion is not None and residual > raw_motion - MIN_GAIN:
        outcome.reason = f"locking its camera would not make it steadier (its motion {raw_motion:.2f} -> {residual:.2f})"
        return outcome
    maps = [h @ window.to_frame() for h in full]

    def draw(index: int, raw: bytes) -> bytes:
        import numpy as np

        rgb = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
        return _drawn(rgb, maps[min(index, len(maps) - 1)], (width, height)).tobytes()

    await encoder.transform_frames(video_path, out_path, draw, width=width, height=height, fps=fps)
    outcome.applied = True
    outcome.maps = normalised_maps(maps, (width, height))
    outcome.crop = (
        window.x / width,
        window.y / height,
        (window.x + width * window.share) / width,
        (window.y + height * window.share) / height,
    )
    return outcome


async def measure_camera(video_path: str) -> tuple[float | None, str | None]:
    """The camera's own motion through *video_path*, first frame to last (:func:`camera_share`), or why it was not read.

    The pass :func:`stabilise` makes, without writing a take: for a take that is not stabilised.
    """
    from app.services.video_studio import encoder

    info = await encoder.probe(video_path)
    width, height = int(getattr(info, "width", 0) or 0), int(getattr(info, "height", 0) or 0)
    if width < 16 or height < 16:
        return None, "the take's size could not be read"
    scale = min(1.0, ESTIMATE_SIDE / max(width, height))
    small = (max(16, round(width * scale / 2) * 2), max(16, round(height * scale / 2) * 2))
    homographies, _first, _last, lost = await estimate(video_path, size=small, fps=float(getattr(info, "fps", None) or 24.0))
    if lost is not None:
        return None, lost.reason
    return camera_share(homographies[-1], small), None


def recorded_camera(record: dict | None) -> float | None:
    """The camera motion of the take that plays, from its stabilisation record; ``None`` when it holds none."""
    if not isinstance(record, dict):
        return None
    value = record.get(RESIDUAL_CAMERA_MOTION if record.get("applied") else CAMERA_MOTION)
    return float(value) if isinstance(value, int | float) else None


async def camera_of(video_path: str, record: dict | None, *, measure: bool) -> tuple[float | None, str | None]:
    """The camera motion of the take at *video_path*, or why it is not known.

    Read off its stabilisation *record* when it was stabilised (the same
    pass); otherwise, with *measure*, measured on the take. A record that
    could not follow the camera is not measured again: the same pass would
    lose it at the same frame.
    """
    if (recorded := recorded_camera(record)) is not None:
        return recorded, None
    if isinstance(record, dict) and record:
        return None, str(record.get("reason") or "its camera could not be followed")
    if not measure:
        return None, None
    return await measure_camera(video_path)


def cropped(picture: Any, crop: Any) -> Any:
    """*picture* (Pillow) cut to *crop* (0-1, ``x0, y0, x1, y1``): what a stabilised take's first frame shows of it."""
    width, height = picture.size
    x0, y0, x1, y1 = (float(v) for v in crop)
    box = (round(x0 * width), round(y0 * height), round(x1 * width), round(y1 * height))
    if box[2] - box[0] < 8 or box[3] - box[1] < 8:
        return picture
    return picture.crop(box)
