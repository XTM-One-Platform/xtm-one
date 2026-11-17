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

"""A recording frame drawn as two pictures at once, anywhere in a render (point 4).

A recording scene shows one take frame per output frame (``compositor_page.frameSource``). The take-level
checks (``scroll_pacing.double_image_score``) read a take and the camera's keyframes; nothing read the encoded
render, so a blend made anywhere further down the pipeline (a retime, a camera move, a hold, a transition drawn inside
the scene) reached the audience with every gate green.

This check reads the finished MP4 only - the grey frames the frozen-picture watch already decodes
(``frame_freeze.check_frames``) - over every frame of every ``recording`` scene, a render's and a scene preview's:
a frame is a double image when

* it is a blend of the frames on either side of it: the least-squares mix ``before + blend * (after - before)``
  leaves under :data:`MAX_RESIDUAL` of their difference unexplained, with *blend* inside :data:`BLEND_RANGE` (a
  frame equal to one of them, or a page that moves on between them, is no mix of the two), and
* it draws the edges each neighbour has of its own (:data:`MIN_SHARE` of both sets), the two pictures told apart by
  at least :data:`MIN_OWN_EDGES` edge pixels each (a cursor, a caret, a small fade of one control are never judged).

Content drawn on one side only - text appearing where a small spinner stood - leaves the other neighbour too few
edges of its own to be judged so; a watch told to read such pairs (``DoubleWatch(appearing=True)``, the scene
preview's read of a take's synthesised spans, ``double_take``) judges the frame on the drawing side's edges alone
(:attr:`Blend.drawn`) when the two neighbours differ by such an appearance (``transition_pacing.appeared``, #10100).

A run of such frames is one ``double_image`` finding with its time, its scene and the frame at its strongest blend,
outside every blended transition, every declared transition in and the video's own fades; three frames of the
same scene are compared, never across a cut.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.services.video_studio.render_checks import DOUBLE_IMAGE, WARN, Finding, clock

#: The scenes read: a recording's take frames are the only pictures that must never be two at once.
READ_TYPES = frozenset({"recording"})
#: Grey levels of a softened gradient that make an edge; a blend draws each picture's at its share of their contrast,
#: so the frame's own are read at :data:`FAINT` of it.
EDGE_LEVELS = 8.0
FAINT = 0.3
#: The fewest edge pixels each neighbour must have of its own, on the watch's grey copy, for the frame to be judged.
MIN_OWN_EDGES = 40
#: The smaller share of the two sets of own edges the frame must draw.
MIN_SHARE = 0.5
#: The mix of the two neighbours a double image is: under the first or past the last, one picture reads alone.
BLEND_RANGE = (0.15, 0.85)
#: The share of the neighbours' difference (root of the energies) a mix of the two may leave unexplained: a blend
#: reads about 0.02 (the encoder's noise), a page that moves on between its neighbours 0.5 and more.
MAX_RESIDUAL = 0.25
#: Two double frames with at most this many watch frames between them are one run, one finding.
RUN_GAP = 1
#: What to change for a double image.
REMEDY = (
    "the frame shows two pictures of the page at once: preview the scene again, and report the render with this frame "
    "if it still draws one (a retime, a camera move or a hold blended two take frames)"
)


def _edges(picture: Any, levels: float) -> Any:
    from scipy.ndimage import gaussian_filter

    dy, dx = np.gradient(gaussian_filter(picture, 1.0))
    return np.hypot(dy, dx) > levels


@dataclass(frozen=True)
class Blend:
    """How a frame reads against its two neighbours: the mix of the two it is, what that mix leaves unexplained and
    the share of each neighbour's own edges it draws (``None``: too few own edges to tell)."""

    blend: float
    residual: float
    share: float | None
    #: The share of the drawing neighbour's own edges the frame draws when the other has too few of its own (content
    #: drawn on one side only); ``None`` otherwise.
    drawn: float | None = None

    @property
    def double(self) -> bool:
        low, high = BLEND_RANGE
        return (
            self.share is not None
            and self.share >= MIN_SHARE
            and low < self.blend < high
            and self.residual < MAX_RESIDUAL
        )

    @property
    def one_sided(self) -> bool:
        """A mix of two neighbours one of which draws content the other has not (a spinner, then a panel's text)."""
        low, high = BLEND_RANGE
        return (
            self.drawn is not None
            and self.drawn >= MIN_SHARE
            and low < self.blend < high
            and self.residual < MAX_RESIDUAL
        )


def read_blend(before: Any, frame: Any, after: Any) -> Blend | None:
    """*frame* (grey arrays, any numeric type) read as a mix of *before* and *after*; ``None`` when the two are the
    same picture (nothing to tell apart)."""
    from scipy.ndimage import binary_dilation

    a = np.asarray(before, dtype=np.float32)
    f = np.asarray(frame, dtype=np.float32)
    b = np.asarray(after, dtype=np.float32)
    diff = b - a
    energy = float((diff * diff).sum())
    if energy <= 1e-6:
        return None
    blend = float(((f - a) * diff).sum() / energy)
    rest = f - a - blend * diff
    residual = float(np.sqrt(float((rest * rest).sum()) / energy))
    first, second = _edges(a, EDGE_LEVELS), _edges(b, EDGE_LEVELS)
    only_first = first & ~binary_dilation(second, iterations=2)
    only_second = second & ~binary_dilation(first, iterations=2)
    seen = binary_dilation(_edges(f, EDGE_LEVELS * FAINT), iterations=1)
    if int(only_first.sum()) < MIN_OWN_EDGES or int(only_second.sum()) < MIN_OWN_EDGES:
        drawing = only_first if int(only_first.sum()) >= int(only_second.sum()) else only_second
        drawn = float(seen[drawing].mean()) if int(drawing.sum()) >= MIN_OWN_EDGES else None
        return Blend(blend, residual, None, drawn)
    return Blend(blend, residual, min(float(seen[only_first].mean()), float(seen[only_second].mean())))


@dataclass
class DoubleWatch:
    """The watch over a render's frames, fed each frame the frozen-picture watch reads: keeps the last two frames of
    the recording scene on screen and judges the middle one once the third arrives (:attr:`hits`: ``[watch frame,
    blend, share]``)."""

    hits: list[list[float]] = field(default_factory=list)
    #: Also judge a frame between two neighbours that differ by content drawn on one side only (#10100).
    appearing: bool = False
    _kept: list[tuple[int, Any, Any]] = field(default_factory=list)

    def feed(self, frame: Any, count: int, scene: dict) -> None:
        if scene.get("type") not in READ_TYPES:
            self._kept.clear()
            return
        scene_id = scene.get("id")
        if self._kept and (self._kept[-1][0] != count - 1 or self._kept[-1][1] != scene_id):
            # A frame not read, or another scene: never compared across it.
            self._kept.clear()
        self._kept.append((count, scene_id, np.asarray(frame, dtype=np.float32)))
        if len(self._kept) < 3:
            return
        (_, _, before), (middle, _, current), (_, _, after) = self._kept[-3:]
        del self._kept[0]
        read = read_blend(before, current, after)
        if read is None:
            return
        if read.double:
            self.hits.append([middle, round(read.blend, 3), round(float(read.share or 0.0), 3)])
        elif self.appearing and read.one_sided:
            from app.services.video_studio.transition_pacing import appeared

            if appeared(before, after):
                self.hits.append([middle, round(read.blend, 3), round(float(read.drawn or 0.0), 3)])

    def restore(self, hits: list | None) -> None:
        self.hits = [
            [int(h[0]), float(h[1]), float(h[2])] for h in hits or [] if isinstance(h, list | tuple) and len(h) == 3
        ]

    def kept(self, at: int) -> list[list[float]]:
        """The hits judged before watch frame *at* (a checkpoint's)."""
        return [hit for hit in self.hits if hit[0] < at]


def _excused(document: dict, start: float, end: float) -> bool:
    from app.services.video_studio.frame_freeze import blended_windows

    if any(a < end and start < b for a, b in blended_windows(document)):
        return True
    for scene in document.get("scenes") or []:
        scene_start = float(scene.get("start") or 0.0)
        seconds = float((scene.get("transition_in") or {}).get("duration") or 0.0)
        if seconds > 0 and scene_start < end and start < scene_start + seconds:
            return True
    return False


def findings(hits: list, rate: float, document: dict, *, offset: float = 0.0, shown: Any = None) -> list[Finding]:
    """A ``double_image`` finding for each run of double frames (*hits*: :attr:`DoubleWatch.hits`, read at *rate*)
    nothing in *document* blends, timed in the MP4 (*offset*: where it starts in *document*), its scene the one its
    strongest frame shows (*shown*: ``frame_freeze.Shown``), with that frame's encoded number and second."""
    from app.services.video_studio.frame_freeze import Shown

    if not hits:
        return []
    if shown is None:
        shown = Shown.of(document, float(document.get("fps") or rate), rate, offset=offset)
    runs: list[list[list[float]]] = []
    for hit in sorted(hits, key=lambda h: h[0]):
        if runs and hit[0] - runs[-1][-1][0] <= RUN_GAP + 1:
            runs[-1].append(hit)
        else:
            runs.append([hit])
    out: list[Finding] = []
    for run in runs:
        first, last = int(run[0][0]), int(run[-1][0])
        start, end = first / rate, (last + 1) / rate
        if _excused(document, start + offset, end + offset):
            continue
        strongest = max(run, key=lambda h: (min(h[1], 1 - h[1]), h[2]))
        count = int(strongest[0])
        scene = shown.scene(count)
        frame = shown.frame(count)
        at = count / rate
        out.append(
            Finding(
                DOUBLE_IMAGE,
                start,
                end,
                scene.get("id"),
                f"{len(run)} frame{'s' if len(run) > 1 else ''} of a {scene.get('type') or 'scene'} "
                f"{'draw' if len(run) > 1 else 'draws'} two pictures "
                f"at once ({clock(start)} to {clock(end)}), frame {frame} at {clock(at)} a {round(100 * strongest[1])}% "
                f"mix of the frames around it: {REMEDY}",
                WARN,
                {
                    "frames": len(run),
                    "frame": frame,
                    "frame_at": round(at, 3),
                    "blend": strongest[1],
                    "share": strongest[2],
                    "scene_type": scene.get("type"),
                },
                code="double_image",
                params={"frames": len(run), "start": clock(start), "end": clock(end)},
            )
        )
    return out
