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

"""A logo ingredient drawn onto its subject wherever an AI shot shows it: the ``tracked`` path.

The composited path (``logos.py``) draws a logo back from where the
start frame shows it. A shot that opens on a building or a street, the branded
subject walking in later, has no logo in its start frame: nothing to draw back
from, and the brand had to be dropped from the shot. On the tracked path the
logo ingredient names its subject instead (``ingredients[].subject``: a picture
of the subject wearing the logo, and what to call it), and the logo is drawn
onto that subject wherever the made take shows it, start frame or not:

1. **Seen.** The take's frames are sampled every :data:`DETECT_EVERY_SECONDS`
   (at most :data:`MAX_KEYFRAMES`, the first and the last among them), and the
   person's chat model is shown the subject's picture - the logo's place on it
   outlined when ``logos.locate`` finds it - beside them, in ONE call, and
   asked for every instance of the subject in each frame: its box and the
   four corners of the surface the logo sits on (the same patch of the
   garment, the blanket, the panel), as shares of the frame
   (:func:`detect`). The code bounds what comes back (:func:`sightings_of`):
   a surface in a line, off the frame or too small to hold a logo is none,
   two sightings of one surface are one instance.
2. **Followed.** Between two sampled frames each instance's surface is
   followed by a ``tracking.PlanarTracker`` started on its surface at the
   sampled frame (the homography of the surface and a margin of what it is
   on, :data:`REGION_GROW`); an instance first seen at a sampled frame is
   followed backward too, through the frames since the one before
   (:func:`_backfill`), so the logo is on the subject from the frame it
   enters. At the next sampled frame the detector confirms each instance
   (the followed surface and a sighting overlapping by :data:`MATCH_IOU`): a
   track that drifted off its sighting, or slipped on that very frame, is
   re-seated on it, one the detector no longer sees ends, a sighting matching
   no track starts an instance. Each
   instance's quads are temporally smoothed over :data:`SMOOTH_FRAMES` frames
   (:func:`smoothed`), and a run of fewer than :data:`MIN_RUN_FRAMES` frames
   is a flicker, never drawn.
3. **Drawn.** In every frame each instance gets the logo's own pixels, once -
   two instances whose surfaces overlap in a frame are one subject and get
   one logo (:func:`_distinct`) - fitted inside its surface with its shape
   kept (``screen_insert.warp``, premultiplied edges: no fringe), under the
   light the model drew on the surface (``screens.lit``: the ONE lighting
   match, the screen replacement's). The take, encoded, is read back and
   every logo found as drawn (``logos._verify``).

What fails the take, each with its frame (:attr:`IngredientLostError.frame_jpeg`):

* ``ingredient subject not found`` (:class:`SubjectNotFoundError`): the
  subject is seen in no sampled frame, or followed for fewer than
  :data:`MIN_RUN_FRAMES` consecutive frames anywhere;
* ``ingredient surface lost at t=...`` (:class:`SurfaceLostError`): the
  surface cannot be followed past a frame while the subject stays in the
  take (the detector sees it again at the next sampled frame, not at the
  frame's edge) - never bridged, a logo is not drawn where the take does
  not show its surface. A subject that walks out of the frame ends its
  instance, nothing fails.

A model that takes the logo as a reference image is sent it first
(``clips.PATH_NATIVE``); its take is checked for the logo as given
(:func:`native_check`: ``logos.locate`` on :data:`NATIVE_CHECK_FRAMES` sampled
frames) and, when the check fails, the logo is drawn onto the subject by this
path, nothing generated again (``runners.run_clip``). The record
(``frame_qc.LOGO_TRACKED``: the share of frames the logo was drawn on, the
instances, the sampled frames it was seen in) is served with the take's frames.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: How often the take is sampled for the detector (seconds), how many frames at most and at least.
DETECT_EVERY_SECONDS = 0.5
MAX_KEYFRAMES = 12
MIN_KEYFRAMES = 3
#: The longest side of a frame the detector is shown, and of the subject's picture.
DETECT_MAX_SIDE = 768
#: A surface under this share of the frame's area is too small to hold a logo anyone can read.
MIN_SURFACE_SHARE = 0.0004
#: Two sightings of one frame whose surfaces' boxes overlap by this much (IoU) are one instance ...
SAME_INSTANCE_IOU = 0.5
#: ... a followed surface and a sighting overlapping by this much are the same instance, confirmed ...
MATCH_IOU = 0.3
#: ... and one overlapping by less than this has drifted: the track is re-seated on the sighting.
KEEP_IOU = 0.5
#: A surface lost between two sampled frames whose subject the next one shows within this share of the frame's
#: diagonal from where it was: the surface was lost, the subject was not.
REACQUIRE_SHARE = 0.3
#: The surface is followed at about this many working pixels on its longer side, within the take's own size.
TRACK_SURFACE_SIDE = 160
TRACK_MAX_SIDE = 1280
#: Around the surface, how much of what it is on is followed with it (a share of its size).
REGION_GROW = 0.5
#: Fewer points than this agreeing on a frame's homography, or a point further than this from the plane (working
#: pixels): a garment is no plane, so the fit is told to be lenient.
MIN_INLIERS = 8
INLIER_PIXELS = 3.0
#: The candidate planes a frame proposes (the surface, what is behind it); the one adopted has at least this share
#: of the points on the surface itself (its quad grown by this much) agreeing with it - with fewer than this many
#: points there, the share of every point followed.
TRACK_PLANES = 3
MIN_CONFIDENCE = 0.5
SURFACE_REACH = 0.15
MIN_SURFACE_POINTS = 6
#: A followed surface stays convex, its area within this factor of the sighting's either way.
MAX_AREA_CHANGE = 4.0
#: A surface whose centre leaves the frame, or with fewer than this many corners in it, is leaving: its instance ends.
MIN_CORNERS_INSIDE = 2
#: Each instance's quads are averaged over this many frames (odd), and a run shorter than this is a flicker.
SMOOTH_FRAMES = 5
MIN_RUN_FRAMES = 6
#: The native path's check: the logo as given looked for in this many frames of the take.
NATIVE_CHECK_FRAMES = 5
NATIVE_CHECK_SIDE = 960


class IngredientLostError(RuntimeError):
    """The take does not show the logo's subject, or loses its surface; ``str()`` is the take's failure."""

    def __init__(self, message: str, *, frame: int = 0, seconds: float = 0.0, frame_jpeg: bytes | None = None) -> None:
        super().__init__(message)
        self.frame = frame
        self.seconds = seconds
        self.frame_jpeg = frame_jpeg


class SubjectNotFoundError(IngredientLostError):
    """``ingredient subject not found``: the subject is in no sampled frame, or followed too briefly anywhere."""


class SurfaceLostError(IngredientLostError):
    """``ingredient surface lost at t=...``: the surface could not be followed past a frame the subject stays in."""


class DetectionUnavailableError(RuntimeError):
    """The detector gave no usable answer: the job's retry asks again, the take is never kept without its logo."""


@dataclass(frozen=True)
class Subject:
    """The subject a logo sits on: a picture of it wearing the logo (Pillow), and what to call it."""

    picture: Any
    label: str | None = None

    @property
    def name(self) -> str:
        return self.label or "the subject"


@dataclass(frozen=True)
class TrackedLogo:
    """A logo ingredient on the tracked path: its picture (RGBA), its name, and its subject."""

    picture: Any
    label: str
    subject: Subject


@dataclass(frozen=True)
class Sighting:
    """One instance of the subject in a sampled frame: its box and its surface's quad, 0-1 across and down the frame."""

    box: tuple[float, float, float, float]
    surface: tuple[tuple[float, float], ...]


#: ``detect(item, frames, reference, outlined) -> sightings by frame name``: the detector :func:`composite` asks,
#: *reference* the subject's picture and *outlined* whether the logo's place is drawn on it.
Detector = Callable[
    [TrackedLogo, dict[str, tuple[bytes, str]], bytes, bool], Awaitable[dict[str, list[Sighting]] | None]
]


@dataclass
class Tracked:
    """What the tracked path did to the take, for the shot's notes and its record (``frame_qc.LOGO_TRACKED``)."""

    frames: int
    drawn_frames: int
    instances: int
    keyframes: int
    seen_in: int
    labels: list[str] = field(default_factory=list)
    subjects: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    fell_back_from: str | None = None
    #: The least integrated instance drawn (``logo_integration.Integration``), and whether a flat one is accepted.
    integration: Any | None = None
    flat: bool = False

    @property
    def integrated(self) -> bool:
        """Whether the take's logo reads as part of its subject, or a flat one was accepted."""
        return self.integration is None or self.integration.passed or self.flat

    @property
    def share(self) -> float:
        """The share of the take's frames the logo was drawn on."""
        return round(self.drawn_frames / self.frames, 3) if self.frames else 0.0

    def record(self) -> dict:
        """The take's record of the path (``frame_qc.LOGO_TRACKED``), served with its frames."""
        return {
            "status": "drawn",
            "frames": self.frames,
            "drawn_frames": self.drawn_frames,
            "share": self.share,
            "instances": self.instances,
            "keyframes_sampled": self.keyframes,
            "keyframes_seen": self.seen_in,
            "labels": list(self.labels),
            "subjects": list(self.subjects),
            **({"fell_back_from": self.fell_back_from} if self.fell_back_from else {}),
            "integration": self.integration.record(waived=self.flat) if self.integration is not None else None,
        }


@dataclass(frozen=True)
class NativeCheck:
    """The native path's logo check: in how many of the frames sampled each logo was found as given."""

    found: dict[str, int]
    sampled: int

    @property
    def passed(self) -> bool:
        return bool(self.found) and all(count > 0 for count in self.found.values())

    def reason(self) -> str:
        missing = [label for label, count in self.found.items() if count == 0]
        return (
            f"{', '.join(missing)} {'was' if len(missing) == 1 else 'were'} found as given in none of the "
            f"{self.sampled} frames sampled of the model's take"
        )


# ── Sampling ─────────────────────────────────────────────────────────────


def keyframes(frame_count: int, fps: float) -> list[int]:
    """The frames the detector is shown: every :data:`DETECT_EVERY_SECONDS`, the first and the last among them."""
    if frame_count <= 0:
        return []
    wanted = round(frame_count / max(fps, 1.0) / DETECT_EVERY_SECONDS) + 1
    count = min(frame_count, max(MIN_KEYFRAMES, min(MAX_KEYFRAMES, wanted)))
    if count >= frame_count:
        return list(range(frame_count))
    return sorted({round(index * (frame_count - 1) / (count - 1)) for index in range(count)})


def _jpeg(rgb: Any, max_side: int = DETECT_MAX_SIDE) -> bytes:
    import io

    import numpy as np
    from PIL import Image

    picture = Image.fromarray(np.ascontiguousarray(rgb), "RGB")
    scale = min(1.0, max_side / max(picture.size))
    if scale < 1.0:
        picture = picture.resize((max(1, round(picture.width * scale)), max(1, round(picture.height * scale))), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    picture.save(out, format="JPEG", quality=88)
    return out.getvalue()


def reference_picture(item: TrackedLogo) -> tuple[bytes, bool]:
    """The subject's picture as the detector sees it (PNG), the logo's place outlined on it when it is found there."""
    import io

    from PIL import Image, ImageDraw

    from app.services.video_studio import logos

    picture = item.subject.picture.convert("RGB")
    scale = min(1.0, DETECT_MAX_SIDE / max(picture.size))
    if scale < 1.0:
        picture = picture.resize((max(1, round(picture.width * scale)), max(1, round(picture.height * scale))), Image.Resampling.LANCZOS)
    found = logos.locate(item.picture, picture)
    if found is not None:
        corners = [(x * picture.width, y * picture.height) for x, y in found[0]]
        ImageDraw.Draw(picture).line([*corners, corners[0]], fill=(255, 40, 40), width=max(2, round(picture.width / 200)))
    out = io.BytesIO()
    picture.save(out, format="PNG")
    return out.getvalue(), found is not None


# ── The detector ─────────────────────────────────────────────────────────

_DETECT_INSTRUCTION = (
    "The first attached image (subject_reference) shows a subject - {subject} - wearing or carrying a mark, "
    "{logo}.{outline} The other images are frames of a generated video shot, in order: {names}. In each frame find "
    "every instance of that same subject (none, one or several) and give for each its bounding box (subject: "
    "[x, y, width, height] as shares of the frame's width and height from 0 to 1, x and y its top left corner) and "
    "the four corners of the surface where that mark sits on it, as in the reference - the same patch of the "
    "garment, blanket or panel, whether or not a mark is drawn there in the frame (surface: [[x, y], [x, y], "
    "[x, y], [x, y]], top left, top right, bottom right, bottom left of the surface as it faces the camera, shares "
    "from 0 to 1). Leave out an instance whose surface is hidden or out of the frame. Report only what you see. "
    'Answer with one JSON object only, each frame\'s name as a key: {{"{first}": [{{"subject": [0.1, 0.2, 0.3, 0.4], '
    '"surface": [[0.2, 0.3], [0.3, 0.3], [0.3, 0.4], [0.2, 0.4]]}}]}} (an empty list for a frame showing none).'
)


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value) else None


def _box_of(raw: Any) -> tuple[float, float, float, float] | None:
    """``(x0, y0, x1, y1)`` of a ``[x, y, width, height]`` box, inside the frame; ``None`` for a malformed one."""
    if not isinstance(raw, list | tuple) or len(raw) != 4:
        return None
    values = [_number(v) for v in raw]
    if any(v is None for v in values):
        return None
    x, y, width, height = values
    x0, y0, x1, y1 = max(0.0, x), max(0.0, y), min(1.0, x + width), min(1.0, y + height)
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


def _ordered(points: list[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    """*points* (four corners) in the order drawn: clockwise on the frame from the one nearest the top left."""
    cx, cy = sum(p[0] for p in points) / 4, sum(p[1] for p in points) / 4
    around = sorted(points, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))
    start = min(range(4), key=lambda n: around[n][0] + around[n][1])
    return tuple(around[start:] + around[:start])


def _surface_of(raw: Any) -> tuple[tuple[float, float], ...] | None:
    """A sighting's surface: four corners 0-1, convex, large enough to hold a logo, in order; ``None`` otherwise."""
    from app.services.video_studio.tracking import convex

    if not isinstance(raw, list | tuple) or len(raw) != 4:
        return None
    points: list[tuple[float, float]] = []
    for corner in raw:
        if not isinstance(corner, list | tuple) or len(corner) != 2:
            return None
        x, y = _number(corner[0]), _number(corner[1])
        if x is None or y is None or not -0.25 <= x <= 1.25 or not -0.25 <= y <= 1.25:
            return None
        points.append((x, y))
    quad = _ordered(points)
    if not convex(quad) or abs(_signed_area(quad)) < MIN_SURFACE_SHARE:
        return None
    return quad


def _signed_area(corners: Any) -> float:
    points = [tuple(map(float, p)) for p in corners]
    return 0.5 * sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:] + points[:1], strict=True))


def _bounds(quad: Any) -> tuple[float, float, float, float]:
    xs, ys = [float(p[0]) for p in quad], [float(p[1]) for p in quad]
    return min(xs), min(ys), max(xs), max(ys)


def iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """The intersection over union of two boxes ``(x0, y0, x1, y1)``."""
    width, height = min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0.0
    overlap = width * height
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - overlap
    return overlap / union if union > 0 else 0.0


def sightings_of(content: str, names: list[str]) -> dict[str, list[Sighting]] | None:
    """The detector's answer bounded: per frame name, the instances seen; ``None`` when the answer is unusable.

    A sighting without a surface the code can draw on is left out; two
    sightings of one frame whose surfaces overlap by :data:`SAME_INSTANCE_IOU`
    are one instance, kept once.
    """
    text = (content or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    out: dict[str, list[Sighting]] = {}
    for name in names:
        kept: list[Sighting] = []
        for item in data.get(name) if isinstance(data.get(name), list) else []:
            if not isinstance(item, dict):
                continue
            surface = _surface_of(item.get("surface"))
            if surface is None:
                continue
            box = _box_of(item.get("subject")) or _bounds(surface)
            if any(iou(_bounds(surface), _bounds(other.surface)) >= SAME_INSTANCE_IOU for other in kept):
                continue
            kept.append(Sighting(box, surface))
        out[name] = kept
    return out


async def detect(
    user_id: object, item: TrackedLogo, frames: dict[str, tuple[bytes, str]], reference: bytes, *, outlined: bool = True
) -> dict[str, list[Sighting]] | None:
    """Every instance of *item*'s subject in *frames* (``name -> (bytes, mime)``), read by the person's chat model in one call.

    *reference* is the subject's picture as :func:`reference_picture` made
    it, *outlined* whether the logo's place is drawn on it. ``None`` when the
    answer is unusable.
    """
    from app.core.database import async_session
    from app.llm.base import ChatMessage, FileRef
    from app.llm.router import get_llm_for_user

    names = list(frames)
    files = [FileRef.from_bytes(reference, "subject_reference.png", "image/png")]
    files += [FileRef.from_bytes(data, f"{name}.{mime.rsplit('/', 1)[-1]}", mime) for name, (data, mime) in frames.items()]
    prompt = _DETECT_INSTRUCTION.format(
        subject=item.subject.name,
        logo=item.label,
        outline=" The red outline on it marks that surface." if outlined else "",
        names=", ".join(names),
        first=names[0],
    )
    async with async_session() as db:
        llm, model = await get_llm_for_user(db, str(user_id))
    response = await llm.chat(
        [ChatMessage(role="user", content=prompt, files=files)], model=model, temperature=0.0, max_tokens=4000
    )
    return sightings_of(response.content, names)


def detector(user_id: object) -> Detector:
    """The :data:`Detector` of a shot's person: :func:`detect` with their chat model."""

    async def read(
        item: TrackedLogo, frames: dict[str, tuple[bytes, str]], reference: bytes, outlined: bool
    ) -> dict[str, list[Sighting]] | None:
        return await detect(user_id, item, frames, reference, outlined=outlined)

    return read


# ── Following each instance ──────────────────────────────────────────────


def _working_size(size: tuple[int, int], surfaces: list[Any]) -> tuple[int, int]:
    """The size the take is followed at: the median surface about :data:`TRACK_SURFACE_SIDE` long, within the take's own."""
    import numpy as np

    from app.services.video_studio.pictures import quad_size

    width, height = size
    sides = [max(quad_size(tuple((x * width, y * height) for x, y in quad))) for quad in surfaces] or [TRACK_SURFACE_SIDE]
    longest = max(float(np.median(sides)), 1.0)
    scale = min(1.0, max(TRACK_SURFACE_SIDE / longest, 0.25), TRACK_MAX_SIDE / max(width, height))
    return max(16, round(width * scale / 2) * 2), max(16, round(height * scale / 2) * 2)


def _tracker(grey: Any, quad_px: Any) -> Any:
    from app.services.video_studio.tracking import PlanarTracker, Region, grown

    return PlanarTracker(
        grey,
        Region(outer=grown(quad_px, REGION_GROW)),
        max_points=200,
        min_distance=4,
        min_inliers=MIN_INLIERS,
        threshold=INLIER_PIXELS,
    )


def _leaving(quad: Any, size: tuple[int, int]) -> bool:
    """Whether *quad* (working pixels) is leaving the frame: its centre out of it, or too few corners in it."""
    import numpy as np

    corners = np.asarray(quad, dtype=float)
    width, height = size
    cx, cy = corners.mean(axis=0)
    if not (0 <= cx < width and 0 <= cy < height):
        return True
    inside = ((corners[:, 0] >= 0) & (corners[:, 0] < width) & (corners[:, 1] >= 0) & (corners[:, 1] < height)).sum()
    return int(inside) < MIN_CORNERS_INSIDE


@dataclass
class _Instance:
    """One instance of a subject followed through the take: its tracker, and its surface in every frame it holds."""

    number: int
    item: int
    origin: Any
    area: float
    tracker: Any = None
    quads: dict[int, Any] = field(default_factory=dict)
    last: Any = None
    ended: bool = False
    #: The track slipped at a sampled frame the detector still sees the surface in: seated on the sighting again.
    slipped: bool = False

    def seat(self, grey: Any, quad: Any) -> None:
        """Follow the surface from *quad* (working pixels) on the frame *grey*."""
        import numpy as np

        self.origin = np.asarray(quad, dtype=float)
        self.area = abs(_signed_area(self.origin))
        self.tracker = _tracker(grey, self.origin)

    def step(self, grey: Any) -> Any | None:
        """The surface in the frame *grey*, followed from the frame before; ``None`` when it is not found there.

        The region followed holds the surface and a margin of what is behind
        it - the table a card slides over - so the frame proposes up to
        :data:`TRACK_PLANES` candidates and the one adopted is the plane the
        points on the surface itself agree with most (at least
        :data:`MIN_CONFIDENCE` of them): the subject, never the background.
        """
        from app.services.video_studio.tracking import convex, grown, inside, project

        if self.tracker is None:
            return None
        proposal = self.tracker.propose(grey, planes=TRACK_PLANES)
        on_surface = inside(proposal.reference, grown(self.origin, SURFACE_REACH)) if len(proposal.reference) else None
        best: tuple[float, int, Any] | None = None
        for n, plane in enumerate(proposal.planes):
            quad = project(plane.homography, self.origin)
            ratio = abs(_signed_area(quad)) / max(self.area, 1e-6)
            if not convex(quad) or not 1 / MAX_AREA_CHANGE <= ratio <= MAX_AREA_CHANGE:
                continue
            if on_surface is not None and int(on_surface.sum()) >= MIN_SURFACE_POINTS:
                confidence = float(plane.inliers[on_surface].mean())
            else:
                confidence = plane.count / max(proposal.followed, 1)
            if confidence >= MIN_CONFIDENCE and (best is None or confidence > best[0]):
                best = (confidence, n, quad)
        if best is None:
            return None
        self.tracker.adopt(proposal, best[1])
        return best[2]


def _pixels(quad: Any, size: tuple[int, int]) -> Any:
    import numpy as np

    return np.asarray(quad, dtype=float) * np.array(size, dtype=float)


def _match(instances: list[_Instance], seen: list[Sighting], size: tuple[int, int]) -> dict[int, int]:
    """Which active instance (by position) each sighting (by position) confirms: greedy on the overlap of their boxes."""
    pairs = []
    for i, instance in enumerate(instances):
        if instance.ended or instance.last is None:
            continue
        for s, sighting in enumerate(seen):
            overlap = iou(_bounds(instance.last), _bounds(_pixels(sighting.surface, size)))
            if overlap >= MATCH_IOU:
                pairs.append((overlap, i, s))
    matched: dict[int, int] = {}
    taken: set[int] = set()
    for _overlap, i, s in sorted(pairs, reverse=True):
        if i in taken or s in matched:
            continue
        matched[s] = i
        taken.add(i)
    return matched


def _near(sightings: list[Sighting], quad: Any, size: tuple[int, int]) -> bool:
    """Whether a sighting's subject stands within :data:`REACQUIRE_SHARE` of the frame's diagonal from *quad*'s centre."""
    import numpy as np

    width, height = size
    centre = np.asarray(quad, dtype=float).mean(axis=0)
    reach = REACQUIRE_SHARE * math.hypot(width, height)
    for sighting in sightings:
        x0, y0, x1, y1 = sighting.box
        box_centre = np.array([(x0 + x1) / 2 * width, (y0 + y1) / 2 * height])
        if float(np.linalg.norm(box_centre - centre)) <= reach:
            return True
    return False


def _lost(item: TrackedLogo, index: int, fps: float, why: str, shown: bytes | None) -> SurfaceLostError:
    return SurfaceLostError(
        f"ingredient surface lost at t={index / fps:.2f} s (frame {index}) of the shot: the surface {item.label} sits "
        f"on, on {item.subject.name}, {why} - generate the shot again in place (clip_id) with a calmer move that keeps "
        "the subject's surface in view.",
        frame=index,
        seconds=index / fps,
        frame_jpeg=shown,
    )


def _backfill(instance: _Instance, grey: Any, quad: Any, buffer: list[tuple[int, Any]], size: tuple[int, int]) -> None:
    """*instance* followed backward from *quad* on the frame *grey* through *buffer* (``(index, grey)``, oldest first)."""
    import numpy as np

    back = _Instance(instance.number, instance.item, np.asarray(quad, dtype=float), abs(_signed_area(quad)))
    back.seat(grey, quad)
    for index, earlier in reversed(buffer):
        found = back.step(earlier)
        if found is None or _leaving(found, size):
            break
        instance.quads[index] = found


async def _follow(
    video_path: str,
    items: list[TrackedLogo],
    sightings: list[dict[int, list[Sighting]]],
    size: tuple[int, int],
    fps: float,
    spacing: int,
) -> tuple[list[_Instance], int]:
    """Every instance of every item's subject followed through the take, and how many frames the take has.

    *sightings* is, per item, the detector's sightings by frame index. One
    pass over the take at *size*: at a sampled frame the sightings confirm,
    re-seat, end or start instances; in between each instance's tracker
    follows its surface. A surface lost while the subject stays raises
    :class:`SurfaceLostError` with that frame.
    """
    import numpy as np

    from app.services.video_studio import encoder, logos
    from app.services.video_studio.tracking import luma

    width, height = size
    instances: list[_Instance] = []
    keyed = [sorted(by_frame) for by_frame in sightings]
    buffer: list[tuple[int, Any]] = []
    index = 0
    frames = encoder.iter_rgb_frames(video_path, width=width, height=height, resize=True)
    async with contextlib.aclosing(frames):
        async for raw in frames:
            rgb = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
            grey = luma(rgb)
            for n, item in enumerate(items):
                seen = sightings[n].get(index)
                active = [one for one in instances if one.item == n and not one.ended]
                # Each live instance follows its surface into this frame.
                for one in active:
                    quad = await asyncio.to_thread(one.step, grey)
                    if quad is not None and not _leaving(quad, size):
                        one.quads[index] = quad
                        one.last = quad
                        continue
                    last = one.last if one.last is not None else one.origin
                    if quad is not None or _leaving(last, size):
                        # Walking out of the frame: the instance ends, nothing fails.
                        one.ended = True
                        continue
                    if seen is not None:
                        # A sampled frame: a sighting of the surface here seats the track on it again (below); none
                        # near ends the instance - the detector's word, not the track's.
                        one.slipped = any(iou(_bounds(last), _bounds(_pixels(s.surface, size))) >= MATCH_IOU for s in seen)
                        one.ended = not one.slipped
                        continue
                    following = [k for k in keyed[n] if k > index]
                    if following and _near(sightings[n][following[0]], last, size):
                        shown = await asyncio.to_thread(logos._frame_jpeg, rgb, last)
                        raise _lost(item, index, fps, "could not be followed into that frame", shown)
                    one.ended = True
                if seen is None:
                    continue
                # A sampled frame: the detector confirms, re-seats, ends or starts the instances.
                active = [one for one in instances if one.item == n and not one.ended]
                matched = _match(active, seen, size)
                for s, sighting in enumerate(seen):
                    quad = _pixels(sighting.surface, size)
                    if s in matched:
                        one = active[matched[s]]
                        if one.slipped or iou(_bounds(one.last), _bounds(quad)) < KEEP_IOU:
                            # Slipped, or drifted off its subject: seated on the sighting again.
                            one.seat(grey, quad)
                            one.quads[index] = quad
                            one.last = quad
                            one.slipped = False
                        continue
                    one = _Instance(len(instances) + 1, n, np.asarray(quad, dtype=float), abs(_signed_area(quad)))
                    one.seat(grey, quad)
                    one.quads[index] = quad
                    one.last = quad
                    instances.append(one)
                    if buffer:
                        await asyncio.to_thread(_backfill, one, grey, quad, buffer, size)
                for i, one in enumerate(active):
                    if i not in matched.values():
                        # The detector no longer sees its subject there: not drawn from here on.
                        one.ended = True
            buffer.append((index, grey))
            if len(buffer) > spacing:
                buffer.pop(0)
            index += 1
    return instances, index


def _runs(indices: list[int]) -> list[list[int]]:
    """*indices* (sorted) split into runs of consecutive frames."""
    runs: list[list[int]] = []
    for index in indices:
        if runs and index == runs[-1][-1] + 1:
            runs[-1].append(index)
        else:
            runs.append([index])
    return runs


def smoothed(quads: dict[int, Any]) -> dict[int, Any]:
    """*quads* (frame index -> quad) averaged over :data:`SMOOTH_FRAMES` frames within each run of consecutive frames.

    Runs shorter than :data:`MIN_RUN_FRAMES` are dropped: a surface the
    detector saw once and the track held for a moment is a flicker. The
    window stays centred - as many frames each side as the run has there -
    so a subject on the move is never drawn behind itself at a run's end.
    """
    import numpy as np

    half = SMOOTH_FRAMES // 2
    out: dict[int, Any] = {}
    for run in _runs(sorted(quads)):
        if len(run) < MIN_RUN_FRAMES:
            continue
        for position, index in enumerate(run):
            reach = min(half, position, len(run) - 1 - position)
            window = run[position - reach : position + reach + 1]
            out[index] = np.mean([np.asarray(quads[k], dtype=float) for k in window], axis=0)
    return out


def _distinct(placed: list[tuple[int, Any]]) -> list[tuple[int, Any]]:
    """*placed* (``(instance number, quad)``) with one quad per subject: a later one overlapping an earlier is the same."""
    kept: list[tuple[int, Any]] = []
    for number, quad in sorted(placed, key=lambda p: p[0]):
        if any(iou(_bounds(quad), _bounds(other)) >= SAME_INSTANCE_IOU for _n, other in kept):
            continue
        kept.append((number, quad))
    return kept


# ── Drawing ───────────────────────────────────────────────────────────────


def _draw_one(frame: Any, picture: Any, quad: Any, size: tuple[int, int]) -> tuple[Any, Any | None]:
    """*frame* with *picture* (RGBA) fitted inside *quad* (0-1), into the surface's light and relief; and what was drawn.

    The surface's broad light shades it and its fine relief prints through
    it (``logo_integration.blend``, the composited path's own drawing): the
    strokes of the model's own redraw under it never do.
    """
    from app.services.video_studio import logo_integration, screen_insert

    corners = [tuple(map(float, p)) for p in quad]
    warped = screen_insert.warp(picture, corners, size, "contain")
    if warped is None:
        return frame, None
    return logo_integration.blend(frame, warped), warped


async def composite(
    video_path: str, out_path: str, items: list[TrackedLogo], detect: Detector, *, flat: bool = False
) -> Tracked:
    """*items* drawn onto their subjects in every frame of *video_path* that shows them, to *out_path*.

    Raises :class:`SubjectNotFoundError` or :class:`SurfaceLostError` (the
    take fails with the frame), :class:`DetectionUnavailableError` when the
    detector gives no usable answer (the job asks again). Each instance's
    integration is measured as the composited path's
    (``logo_integration.Probe``): the least integrated is the take's, a
    failure recorded unless *flat* (``ingredients_quality: "flat"``).
    """
    import numpy as np

    from app.services.video_studio import encoder, logo_integration, logos

    info = await encoder.probe(video_path)
    width, height = int(getattr(info, "width", 0) or 0), int(getattr(info, "height", 0) or 0)
    fps = float(getattr(info, "fps", None) or 24.0)
    duration = float(getattr(info, "duration", 0.0) or 0.0)
    if width < 16 or height < 16:
        raise SubjectNotFoundError("ingredient subject not found: the take has no frames to look for it in.")
    estimate = max(1, round(duration * fps)) if duration > 0 else 1
    sampled = keyframes(estimate, fps)
    spacing = max(1, sampled[1] - sampled[0]) if len(sampled) > 1 else 1

    # Pass 1: the frames the detector is shown.
    shown: dict[int, bytes] = {}
    latest: tuple[int, bytes] | None = None
    scale = min(1.0, DETECT_MAX_SIDE / max(width, height))
    detect_size = (max(16, round(width * scale / 2) * 2), max(16, round(height * scale / 2) * 2))
    index = 0
    frames = encoder.iter_rgb_frames(video_path, width=detect_size[0], height=detect_size[1], resize=True)
    async with contextlib.aclosing(frames):
        async for raw in frames:
            if index in sampled or index == estimate - 1:
                rgb = np.frombuffer(raw, dtype=np.uint8).reshape(detect_size[1], detect_size[0], 3)
                picture = await asyncio.to_thread(_jpeg, rgb)
                if index in sampled:
                    shown[index] = picture
                latest = (index, picture)
            index += 1
    if not shown and latest is not None:
        shown[latest[0]] = latest[1]
    missing = [k for k in sampled if k not in shown]
    if missing and latest is not None:
        # The take is shorter than its duration said: its last frame stands for the sampled ones past it.
        shown = {k: v for k, v in shown.items() if k < latest[0]}
        shown[latest[0]] = latest[1]
    sampled = sorted(shown)
    # Each frame named by its index in the take: what the detector is told, and what a sighting is placed at.
    names = [f"frame_{k}" for k in sampled]
    pictures = {name: (shown[k], "image/jpeg") for name, k in zip(names, sampled, strict=True)}

    # The detector, once per item.
    sightings: list[dict[int, list[Sighting]]] = []
    seen_in: list[int] = []
    for item in items:
        reference, outlined = await asyncio.to_thread(reference_picture, item)
        try:
            answer = await detect(item, pictures, reference, outlined)
        except Exception as exc:
            raise DetectionUnavailableError(
                f"the subject of {item.label} could not be looked for: {format_exception_message(exc)[:200]}"
            ) from exc
        if answer is None:
            raise DetectionUnavailableError(f"the subject of {item.label} could not be looked for: the reading came back unusable.")
        by_frame = {k: answer.get(name, []) for name, k in zip(names, sampled, strict=True)}
        sightings.append(by_frame)
        seen_in.append(sum(1 for seen in by_frame.values() if seen))
        if not seen_in[-1]:
            raise SubjectNotFoundError(
                f"ingredient subject not found: {item.subject.name}, which {item.label} sits on, is seen in none of the "
                f"{len(sampled)} frames sampled of the take - generate the shot again in place (clip_id) with a prompt "
                "that brings the subject into view, or give the shot a start frame showing it."
            )

    # Pass 2: each instance followed between the sampled frames.
    surfaces = [sighting.surface for by_frame in sightings for seen in by_frame.values() for sighting in seen]
    size = _working_size((width, height), surfaces)
    instances, count = await _follow(video_path, items, sightings, size, fps, spacing)
    tracks: list[tuple[_Instance, dict[int, Any]]] = []
    for item_index, item in enumerate(items):
        own = [(one, smoothed(one.quads)) for one in instances if one.item == item_index]
        kept = [(one, quads) for one, quads in own if quads]
        if not kept:
            longest = max((len(run) for one in instances if one.item == item_index for run in _runs(sorted(one.quads))), default=0)
            raise SubjectNotFoundError(
                f"ingredient subject not found: {item.subject.name}, which {item.label} sits on, is seen in "
                f"{seen_in[item_index]} of the {len(sampled)} frames sampled but followed for {longest} consecutive "
                f"frame{'s' if longest != 1 else ''} at most, under {MIN_RUN_FRAMES} - generate the shot again in place "
                "(clip_id) with a calmer move that keeps the subject in view."
            )
        tracks.extend(kept)

    # Pass 3: drawn, once per subject per frame.
    scale_back = np.array(size, dtype=float)
    expected: list[list[Any]] = [[] for _ in tracks]
    probes = [logo_integration.Probe(items[one.item].label, fps=fps, size=(width, height)) for one, _quads in tracks]
    pixels = np.array([width, height], dtype=float)
    drawn_frames = 0

    def draw(index: int, raw: bytes) -> bytes:
        nonlocal drawn_frames
        frame = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
        placed = [(one.number, quads[index] / scale_back) for one, quads in tracks if index in quads]
        chosen = {number for number, _quad in _distinct(placed)}
        any_drawn = False
        for n, (one, quads) in enumerate(tracks):
            if index not in quads or one.number not in chosen:
                expected[n].append(None)
                continue
            source = frame
            frame, warped = _draw_one(frame, items[one.item].picture, quads[index] / scale_back, (width, height))
            if warped is None:
                expected[n].append(None)
                continue
            if probes[n].wants(index):
                vertices = logo_integration.quad_grid(np.asarray(quads[index], dtype=float) / scale_back * pixels)
                probes[n].add(index, source, frame, warped, vertices)
            any_drawn = True
            alpha = np.asarray(warped.image)[..., 3]
            grey, weight = logos._thumbnail(frame, warped.box, alpha)
            expected[n].append((warped.box, grey, weight))
        drawn_frames += int(any_drawn)
        return frame.tobytes()

    frames_written = await encoder.transform_frames(video_path, out_path, draw, width=width, height=height, fps=fps)
    # Pass 4: read back, every logo found as drawn.
    try:
        await logos._verify(
            out_path,
            expected,
            [logos.Logo(items[one.item].picture, items[one.item].label) for one, _quads in tracks],
            width=width,
            height=height,
            fps=fps,
        )
    except logos.LogoLostError as exc:
        raise SurfaceLostError(
            f"ingredient surface lost at t={exc.seconds:.2f} s (frame {exc.frame}) of the shot: the take read back does "
            f"not hold {exc.label} as drawn there ({exc.why}) - generate the shot again in place (clip_id).",
            frame=exc.frame,
            seconds=exc.seconds,
            frame_jpeg=exc.frame_jpeg,
        ) from None

    total = frames_written or count
    integration = logo_integration.worst([await asyncio.to_thread(probe.measure) for probe in probes])
    done = Tracked(
        frames=total,
        drawn_frames=drawn_frames,
        instances=len(tracks),
        keyframes=len(sampled),
        seen_in=max(seen_in) if seen_in else 0,
        labels=[item.label for item in items],
        subjects=[item.subject.name for item in items],
        integration=integration,
        flat=flat,
    )
    names_drawn = ", ".join(item.label for item in items)
    subjects = ", ".join(dict.fromkeys(item.subject.name for item in items))
    done.notes.append(
        f"{_capital(names_drawn)} drawn with {'its' if len(items) == 1 else 'their'} own pixels onto {subjects} in "
        f"{drawn_frames} of the take's {total} frames ({done.share:.0%}: the subject is seen in {done.seen_in} of "
        f"{len(sampled)} frames sampled, followed in between), fitted inside the surface the subject wears it on, "
        f"under the light and relief the model drew there (ingredients_path: tracked)."
    )
    if len(tracks) > 1:
        done.notes.append(
            f"{len(tracks)} instances of the subject carry the logo, one logo each in every frame they show in."
        )
    if integration is not None:
        done.notes.append(logo_integration.note(integration, flat=flat))
    return done


def _capital(text: str) -> str:
    return f"{text[:1].upper()}{text[1:]}"


# ── The native path's check ──────────────────────────────────────────────


async def native_check(video_path: str, marks: list[tuple[Any, str]]) -> NativeCheck:
    """Whether the model's own take shows each of *marks* (``(RGBA picture, label)``) as given.

    :data:`NATIVE_CHECK_FRAMES` frames of the take are sampled and each
    logo looked for in them as ``logos.locate`` finds it in a start frame: a
    logo found in none of them was redrawn by the model, or not drawn, and
    the tracked path draws it onto its subject instead.
    """
    import numpy as np
    from PIL import Image

    from app.services.video_studio import encoder, logos

    info = await encoder.probe(video_path)
    width, height = int(getattr(info, "width", 0) or 0), int(getattr(info, "height", 0) or 0)
    fps = float(getattr(info, "fps", None) or 24.0)
    duration = float(getattr(info, "duration", 0.0) or 0.0)
    found = {label: 0 for _picture, label in marks}
    if width < 16 or height < 16:
        return NativeCheck(found, 0)
    estimate = max(1, round(duration * fps)) if duration > 0 else 1
    wanted = set(np.linspace(0, estimate - 1, min(NATIVE_CHECK_FRAMES, estimate)).round().astype(int).tolist())
    scale = min(1.0, NATIVE_CHECK_SIDE / max(width, height))
    size = (max(16, round(width * scale / 2) * 2), max(16, round(height * scale / 2) * 2))
    sampled = 0
    index = 0
    frames = encoder.iter_rgb_frames(video_path, width=size[0], height=size[1], resize=True)
    async with contextlib.aclosing(frames):
        async for raw in frames:
            if index in wanted:
                sampled += 1
                frame = Image.fromarray(np.frombuffer(raw, dtype=np.uint8).reshape(size[1], size[0], 3), "RGB")
                for picture, label in marks:
                    if await asyncio.to_thread(logos.locate, picture, frame) is not None:
                        found[label] += 1
            index += 1
    return NativeCheck(found, sampled)
