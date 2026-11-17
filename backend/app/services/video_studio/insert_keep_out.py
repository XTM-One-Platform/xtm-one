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

"""The screen of an AI shot carrying a screen insert, kept clear: asked of the model, measured on the take.

A video model told by the negative prompt never to draw a head, a hand or an animal in front of the screen draws it
anyway (video b56997a2: four takes of six), and the content check, reading three frames, failed each paid take on it
- even a hand crossing a few percent of the screen, which the insert's matte keeps in front of the insert anyway. So
for a shot carrying a screen insert (``clips.carries_insert``) whose screen is known on its start frame - a screen
replacement's quad (``clips.SCREEN``) or the screen measured on its start picture (``clips.SURFACE``, 0-1):

1. **Asked.** The prompt sent ends with a keep-out sentence (:func:`line`): nothing enters the rectangle the quad
   spans (:func:`rectangle`, shares of the frame). A model that takes reference images beside a start frame is sent
   the start picture a second time, as a reference image (:func:`start_as_reference`).
2. **Measured.** Every frame of the take as made, before a screenshot is drawn into it (:func:`measure_take`), is
   matted as the render mattes an insert: the key the take's screen shows (``screen_insert.KeySpec.sampled``), its
   matte (``screens.key_matte``), what stands in front of the surface (``screen_insert._foreground``) and how sure
   that mask is (``screen_insert.key_confidence``). The largest share of the quad it covers, its frame and the
   mask's lowest confidence are kept with the take (``frame_qc.FOREGROUND_OVERLAP``, :class:`Overlap`) and served in
   the shot's answer. A screen showing no key colour leaves the foreground matte off.
3. **Judged.** The content check is told the rectangle and says, of each excluded term seen, whether it shows only
   where it enters it (``content_drift.Finding.keep_out``). Those terms are the measure's to judge (:func:`judge`):
   the matte on, under :data:`MAX_SHARE` of the quad and the mask sure past :data:`MIN_CONFIDENCE`, the take is kept
   with a note - the insert is drawn under what stands in front; otherwise it fails as content drift, the measure in
   its reason. Any other finding fails the take as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from dataclasses import dataclass, field
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: A negative-prompt term seen in front of the screen keeps the take while what stands there covers less than this
#: share of the quad on every frame ...
MAX_SHARE = 0.10
#: ... and the mask of it is surer than this (``screen_insert.key_confidence``: the share of it the key reads clear).
MIN_CONFIDENCE = 0.7
#: The mask's confidence is read on the frames where it covers at least this share of the quad: a few pixels of a
#: blurred edge say nothing of how sure the mask is.
MIN_READ_SHARE = 0.005
#: The take is read at most this long a side.
READ_SIDE = 960

Quad = tuple[tuple[float, float], ...]


def quad_of(params: dict) -> Quad | None:
    """The screen of a shot carrying a screen insert on its start frame, 0-1 across and down; ``None`` for none.

    A screen replacement's quad (``screens.start_quad``), else the screen measured on its start picture.
    """
    from app.services.video_studio import clips, screens

    if not clips.carries_insert(params):
        return None
    quad = screens.start_quad(params.get(clips.SCREEN))
    surface = params.get(clips.SURFACE)
    if quad is None and isinstance(surface, dict) and isinstance(surface.get("quad"), list):
        quad = [(float(x), float(y)) for x, y in surface["quad"]]
    return tuple(quad) if quad is not None and len(quad) == 4 else None


def rectangle(quad: Quad) -> tuple[float, float, float, float]:
    """``(x0, x1, y0, y1)``: the rectangle *quad* spans, in hundredths of the frame, widened to cover it whole."""
    xs, ys = [point[0] for point in quad], [point[1] for point in quad]

    def low(value: float) -> float:
        return max(0.0, math.floor(value * 100.0) / 100.0)

    def high(value: float) -> float:
        return min(1.0, math.ceil(value * 100.0) / 100.0)

    return low(min(xs)), high(max(xs)), low(min(ys)), high(max(ys))


def rectangle_of(params: dict) -> tuple[float, float, float, float] | None:
    """The keep-out rectangle of a shot (:func:`rectangle` of :func:`quad_of`); ``None`` for a shot without one."""
    quad = quad_of(params)
    return rectangle(quad) if quad is not None else None


def covered_box(params: dict) -> tuple[float, float, float, float] | None:
    """The rectangle (``x0, x1, y0, y1``, shares of the frame) a screen insert drawn at render time covers on the
    take, for the text check to leave unread (``text_drift.check`` *covered*): the screen measured on the take as made
    (``frame_qc.SURFACE``), else the screen of its start frame (:func:`quad_of`). ``None`` for a shot carrying no
    insert, and for a screen replacement, whose screenshot is drawn into the take and read as exact text."""
    from app.services.video_studio import clips, frame_qc

    if isinstance(params.get(clips.SCREEN), dict):
        return None
    quad = quad_of(params)
    if quad is None:
        return None
    surface = (params.get(frame_qc.QC_KEY) or {}).get(frame_qc.SURFACE)
    if isinstance(surface, dict) and isinstance(surface.get("quad"), list) and len(surface["quad"]) == 4:
        quad = tuple((float(x), float(y)) for x, y in surface["quad"])
    return rectangle(quad)


def key_advisory(params: dict) -> str | None:
    """What the answer says when the screen a text check left unread shows a key colour and something on it is not
    that colour on some frames: lettering drawn there in another colour stays in front of a keyed insert. ``None`` when
    the take was not measured, its screen shows no key, or nothing on it differs from the key."""
    from app.services.video_studio import frame_qc

    measure = Overlap.from_dict((params.get(frame_qc.QC_KEY) or {}).get(frame_qc.FOREGROUND_OVERLAP))
    if measure is None or not measure.matted or measure.frames_in_front <= 0:
        return None
    return (
        f"Screen insert: the take's screen is keyed by its {measure.key} colour, and something on it is not that colour "
        f"on {measure.occluded_share:.0%} of the frames (at most {measure.max_share:.0%} of its quad) - lettering drawn "
        "there stays in front of the keyed insert. When nothing crosses the screen, set key: null on that insert to draw "
        "it over the whole screen, with fit: cover or stretch: a contain fit left unkeyed shows the screen's "
        f"{measure.key} in its bands (insert_band_key_colour)."
    )


def spans(box: tuple[float, float, float, float]) -> str:
    """*box* as the prompt and the answer say it: ``x 0.32-0.68, y 0.20-0.55``."""
    x0, x1, y0, y1 = box
    return f"x {x0:.2f}-{x1:.2f}, y {y0:.2f}-{y1:.2f}"


def line(box: tuple[float, float, float, float]) -> str:
    """The keep-out sentence the prompt sent ends with."""
    return (
        f"Keep-out: nothing enters the rectangle {spans(box)} of the frame (shares of its width and height from its top "
        "left corner), where the screen is - no subject, no part of one and no object passes in front of the screen "
        "at any moment of the shot."
    )


def with_line(params: dict, prompt: str) -> str:
    """*prompt* ending with the shot's keep-out sentence (:func:`line`), within ``clips.MAX_PROMPT_CHARS``."""
    from app.services.video_studio.clips import MAX_PROMPT_CHARS

    box = rectangle_of(params)
    if box is None:
        return prompt
    said = line(box)
    room = max(0, MAX_PROMPT_CHARS - len(said) - 1)
    return f"{prompt.rstrip()[:room]} {said}".strip()


def start_as_reference(params: dict, provider: Any, model: str) -> bool:
    """Whether the take is sent its start picture a second time, as a reference image: a shot with a keep-out
    rectangle and a start picture, on a model that takes reference images beside a start frame and has room for one."""
    from app.llm.router import video_reference_limits
    from app.services.video_studio import clips

    if provider is None or quad_of(params) is None or not clips.starts_from_picture(params):
        return False
    limits = video_reference_limits(provider, model)
    room = min(clips.MAX_REFERENCE_IMAGES, limits["reference_images"])
    taken = len(params.get(clips.REFERENCE_IMAGES) or [])
    return limits["reference_images_with_start_frame"] and taken < room


def note(params: dict, provider: Any, model: str) -> str | None:
    """What the answer says the shot is asked of its screen; ``None`` for a shot without a keep-out rectangle."""
    from app.services.video_studio import clips

    box = rectangle_of(params)
    if box is None:
        return None
    if start_as_reference(params, provider, model):
        again = ", its start picture sent a second time as a reference image"
    elif clips.starts_from_picture(params):
        again = f" ({model} takes no reference image beside a start frame: its start picture is sent once)"
    else:
        again = ""
    return (
        f"Keep-out: the prompt sent asks that nothing enters the screen's rectangle ({spans(box)} of the frame){again}; "
        "what stands in front of the screen is measured on every frame of the take."
    )


# ── Measured ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Overlap:
    """What stands in front of the screen over a take (:func:`measure_take`)."""

    #: The frames read; the key what stands in front was matted by (``None``: the screen showed none, the matte off).
    frames: int
    key: str | None
    #: The largest share of the quad what stands in front covers, on which frame (0-based) and at which second.
    max_share: float = 0.0
    frame: int | None = None
    at: float | None = None
    #: How sure the mask is: its lowest confidence over the frames it covers :data:`MIN_READ_SHARE` of the quad on.
    confidence: float | None = None
    #: The frames it covers that much of the quad on.
    frames_in_front: int = 0
    #: The first frame (0-based) it covers that much of the quad on, its second, and the box it covers there (0-1 of
    #: the frame: ``x0, y0, x1, y1``): where the screen is first occluded (dev:screen-occlusion-check).
    first_frame: int | None = None
    first_at: float | None = None
    first_box: tuple[float, float, float, float] | None = None
    #: The frame rate the take was read at, to suggest a trim before :attr:`first_at`.
    fps: float | None = None

    def as_dict(self) -> dict:
        return {
            "frames": self.frames,
            "key": self.key,
            "max_share": self.max_share,
            "frame": self.frame,
            "at": self.at,
            "confidence": self.confidence,
            "frames_in_front": self.frames_in_front,
            "first_frame": self.first_frame,
            "first_at": self.first_at,
            "first_box": list(self.first_box) if self.first_box is not None else None,
            "fps": self.fps,
            "occluded_share": self.occluded_share,
            "trim_end": self.trim_end(),
        }

    @classmethod
    def from_dict(cls, data: Any) -> Overlap | None:
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                int(data.get("frames") or 0),
                str(data["key"]) if data.get("key") else None,
                float(data.get("max_share") or 0.0),
                None if data.get("frame") is None else int(data["frame"]),
                None if data.get("at") is None else float(data["at"]),
                None if data.get("confidence") is None else float(data["confidence"]),
                int(data.get("frames_in_front") or 0),
                None if data.get("first_frame") is None else int(data["first_frame"]),
                None if data.get("first_at") is None else float(data["first_at"]),
                _box(data.get("first_box")),
                None if data.get("fps") is None else float(data["fps"]),
            )
        except (TypeError, ValueError):
            return None

    @property
    def occluded_share(self) -> float:
        """The share of the frames read on which something stands in front of the screen (:data:`MIN_READ_SHARE`)."""
        return round(self.frames_in_front / self.frames, 4) if self.frames else 0.0

    def trim_end(self) -> float | None:
        """The ``trim_end`` (seconds into the take) that ends the shot one frame before its screen is first occluded;
        ``None`` when nothing stands in front of it, or when it is occluded from its first frame (nothing to keep)."""
        if not self.matted or self.first_frame is None or self.first_frame <= 0:
            return None
        fps = self.fps or 24.0
        return round(max(0.0, (self.first_frame - 1) / fps), 2)

    def occlusion(self) -> str | None:
        """What the answer says of the screen's occlusion over the take; ``None`` when nothing stands in front of it."""
        if not self.matted or self.first_frame is None:
            return None
        box = f", over x {self.first_box[0]:.2f}-{self.first_box[2]:.2f}, y {self.first_box[1]:.2f}-{self.first_box[3]:.2f}" if self.first_box else ""
        trim = self.trim_end()
        cut = (
            f"; set trim_end to {trim:.2f} s to end the shot before it"
            if trim is not None
            else "; it stands there from the first frame, so no trim keeps the screen clear: regenerate the take"
        )
        return (
            f"Occlusion: something stands in front of the screen on {self.occluded_share:.0%} of the take's frames, "
            f"first at {self.first_at:.2f} s (frame {self.first_frame}){box}, at most {self.max_share:.0%} of its quad{cut}."
        )

    @property
    def matted(self) -> bool:
        return self.key is not None

    def tolerated(self) -> bool:
        """Whether a negative-prompt term seen in front of the screen keeps the take: matted, small and sure."""
        sure = self.confidence is None or self.confidence > MIN_CONFIDENCE
        return self.matted and self.max_share < MAX_SHARE and sure

    def measured(self) -> str:
        """What the measure found, as a clause."""
        if not self.matted:
            return (
                "the foreground matte is off - the take's screen shows no green, blue or magenta key to matte what "
                "stands in front of it by"
            )
        if self.frame is None or self.max_share <= 0.0:
            return f"nothing stands in front of it on any of the {self.frames} frames, matted by its {self.key} key"
        sure = f", the mask {self.confidence:.2f} sure" if self.confidence is not None else ""
        return (
            f"what stands in front of it covers at most {self.max_share:.0%} of its quad (frame {self.frame}, "
            f"{self.at:.2f} s), matted by its {self.key} key{sure}"
        )

    def short_of(self) -> str:
        """Why a term seen in front of the screen fails the take, by the measure."""
        if not self.matted:
            return "a term in front of the screen is kept only with the foreground matte on"
        if self.max_share >= MAX_SHARE:
            return f"a term in front of the screen is kept only under {MAX_SHARE:.0%} of its quad"
        return f"a term in front of the screen is kept only with a mask over {MIN_CONFIDENCE:.1f} sure"


def _box(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, list | tuple) or len(value) != 4:
        return None
    x0, y0, x1, y1 = (float(item) for item in value)
    return x0, y0, x1, y1


def _keyed(frame: Any, quad_px: Any, names: list[str]) -> Any:
    """The key the screen within *quad_px* shows on *frame*, as the render keys it (``KeySpec.sampled``); ``None``."""
    from app.services.video_studio import screens
    from app.services.video_studio.screen_insert import KeySpec

    colours = dict(screens.KEY_CANDIDATES)
    corners = [tuple(point) for point in quad_px]
    for name in names:
        if screens.sample_key(frame, corners, screens.Key(name, colours[name])) is not None:
            return KeySpec(color=name).sampled(frame, quad_px)
    return None


def _front(
    frame: Any, key: Any, tolerance: float, box: tuple[int, int, int, int], inside: Any, scale: float
) -> tuple[float, float | None, tuple[int, int, int, int] | None]:
    """``(share of the quad, confidence, pixel box)`` of what stands in front of the screen on *frame*, as the render
    mattes it: the box (``x0, y0, x1, y1`` in the frame's pixels) it covers within the quad, ``None`` for nothing."""
    import numpy as np

    from app.services.video_studio import screens
    from app.services.video_studio.screen_insert import _foreground, key_confidence

    x0, y0, x1, y1 = box
    matte = screens.key_matte(frame[y0:y1, x0:x1].astype(np.float32), key, tolerance)
    front = _foreground(matte < 0.5, inside, scale)
    covered = front & inside
    share = float(covered.sum()) / max(1, int(inside.sum()))
    rows, cols = np.nonzero(covered)
    where = (x0 + int(cols.min()), y0 + int(rows.min()), x0 + int(cols.max()) + 1, y0 + int(rows.max()) + 1) if rows.size else None
    return round(share, 4), key_confidence(matte, front, inside), where


def key_names(params: dict) -> list[str]:
    """The key colours the take's screen is read for, the one it was made with first: a screen replacement's key, or
    the hue the screen of its start picture was found in (``surface_refine.Bezel.hue``)."""
    from app.services.video_studio import clips, screens

    candidates = [name for name, _rgb in screens.KEY_CANDIDATES]
    named: list[str] = []
    screen = params.get(clips.SCREEN)
    if isinstance(screen, dict) and isinstance(screen.get("key"), dict):
        named.append(str(screen["key"].get("name")))
    surface = params.get(clips.SURFACE)
    hue = surface.get("hue") if isinstance(surface, dict) else None
    if isinstance(hue, int | float):
        named += [name for name, rgb in screens.KEY_CANDIDATES if screens.in_hue_band(float(hue), screens.hue_band((name, rgb)))]
    return list(dict.fromkeys([name for name in named if name in candidates] + candidates))


async def overlap(path: str, info: Any, quad: Quad, names: list[str]) -> Overlap | None:
    """What stands in front of the screen *quad* (0-1) over every frame of the take at *path* (*info*: its probe)."""
    import numpy as np

    from app.services.video_studio import encoder
    from app.services.video_studio.screen_insert import _coverage, bbox

    factor = min(1.0, READ_SIDE / max(int(info.width), int(info.height), 1))
    width, height = max(2, round(info.width * factor)), max(2, round(info.height * factor))
    box = bbox(list(quad), width, height)
    if box is None:
        return None
    quad_px = np.asarray([(x * width, y * height) for x, y in quad], dtype=float)
    inside = _coverage(quad_px, box) > 0.5
    if not inside.any():
        return None
    fps = float(info.fps or 0.0) or 24.0
    scale = height / 1080.0
    spec = None
    count, in_front = 0, 0
    worst, worst_frame, lowest = 0.0, None, None
    first, first_box = None, None
    async with contextlib.aclosing(encoder.iter_rgb_frames(path, width=width, height=height, resize=True)) as frames:
        async for raw in frames:
            frame = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
            if spec is None:
                spec = await asyncio.to_thread(_keyed, frame, quad_px, names)
                if spec is None:
                    return Overlap(0, None)
            share, confidence, where = await asyncio.to_thread(
                _front, frame, spec.key(), spec.matte_tolerance(), box, inside, scale
            )
            if share > worst:
                worst, worst_frame = share, count
            if share >= MIN_READ_SHARE:
                in_front += 1
                if first is None and where is not None:
                    first = count
                    first_box = tuple(round(value / side, 3) for value, side in zip(where, (width, height, width, height), strict=True))
                if confidence is not None:
                    lowest = confidence if lowest is None else min(lowest, confidence)
            count += 1
    if not count:
        return None
    at = None if worst_frame is None else round(worst_frame / fps, 2)
    first_at = None if first is None else round(first / fps, 2)
    return Overlap(count, spec.color, worst, worst_frame, at, lowest, in_front, first, first_at, first_box, fps)


async def measure_take(path: str, info: Any, params: dict, *, take_file_id: str) -> None:
    """Measure what stands in front of the screen of the take at *path* (*info*: its probe) and keep it with its frames
    (``frame_qc.FOREGROUND_OVERLAP``), once per take: on the screen the take's first frame shows (``frame_qc.SURFACE``,
    measured on this take), else on the screen of its start frame (:func:`quad_of`). Advisory: a take that cannot be
    read keeps no measure, and a term seen in front of its screen fails it as before."""
    from app.services.video_studio import frame_qc

    quad = quad_of(params)
    qc = params.get(frame_qc.QC_KEY) if isinstance(params.get(frame_qc.QC_KEY), dict) else {}
    kept = qc.get(frame_qc.FOREGROUND_OVERLAP)
    if quad is None or (isinstance(kept, dict) and kept.get("take_file_id") == take_file_id):
        return
    surface = qc.get(frame_qc.SURFACE)
    if isinstance(surface, dict) and surface.get("take_file_id") == take_file_id and isinstance(surface.get("quad"), list):
        quad = tuple((float(x), float(y)) for x, y in surface["quad"])
    try:
        measured = await overlap(path, info, quad, key_names(params))
    except Exception as exc:  # advisory: the content check judges the take without it
        logger.info("What stands in front of the take's screen was not measured: %s", format_exception_message(exc))
        return
    if measured is not None:
        params[frame_qc.QC_KEY] = {**qc, frame_qc.FOREGROUND_OVERLAP: {**measured.as_dict(), "take_file_id": take_file_id}}


def occlusion_note(params: dict) -> str | None:
    """What the shot's answer says of its screen occluded over its take (:meth:`Overlap.occlusion`): the share of
    frames, the first time and box, the trim_end that ends the shot before it; ``None`` when not measured or clear."""
    from app.services.video_studio import frame_qc

    measure = Overlap.from_dict((params.get(frame_qc.QC_KEY) or {}).get(frame_qc.FOREGROUND_OVERLAP))
    return measure.occlusion() if measure is not None else None


# ── Judged ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Judged:
    """The content check's failing findings once the measure judged those in front of the screen (:func:`judge`)."""

    #: What still fails the take.
    failing: list = field(default_factory=list)
    #: The note a take kept carries: the terms the measure accepted, or what it measured.
    note: str | None = None
    #: What the reason of a take that still fails says of the measure (empty for no measure).
    clause: str = ""
    #: Whether the measure accepted findings the content check had failed.
    accepted: bool = False


def judge(failing: list, params: dict) -> Judged:
    """*failing* (``content_drift.Finding``) with each negative-prompt term seen only in front of the screen
    (``Finding.keep_out``) judged by the take's measure (:class:`Overlap`): accepted when it is tolerated, failing as
    before otherwise. Without a measure, *failing* stands as it is."""
    from app.services.video_studio import frame_qc

    measure = Overlap.from_dict((params.get(frame_qc.QC_KEY) or {}).get(frame_qc.FOREGROUND_OVERLAP))
    if measure is None:
        return Judged(list(failing))
    ours = [finding for finding in failing if finding.excluded and finding.keep_out]
    if ours and measure.tolerated():
        terms = ", ".join(dict.fromkeys(finding.subject for finding in ours))
        kept = (
            f"Kept: {terms}, which the negative prompt excludes, shows only in front of the screen, and "
            f"{measure.measured()} - the insert is drawn under it; past {MAX_SHARE:.0%} of the quad the take fails as "
            "content drift."
        )
        rest = [finding for finding in failing if finding not in ours]
        return Judged(rest, kept, f" On the screen, {measure.measured()}." if rest else "", True)
    if failing:
        why = f" - {measure.short_of()}" if ours else ""
        return Judged(list(failing), clause=f" On the screen, {measure.measured()}{why}.")
    return Judged([], f"On the screen of the shot, {measure.measured()}.")
