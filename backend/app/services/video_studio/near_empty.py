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

"""A recording that shows a near-empty canvas for longer than a beat, anywhere in a render (issue #9451).

A recording step that hides most of what the canvas shows (a "hide selection", a filter that empties a graph) can
leave one small node and the cursor on screen. The frozen-picture watch (``frame_freeze.FreezeWatch``) never fires
there - the cursor keeps moving - and no gate measured how much of the frame carries content, so a render held an
empty canvas for well over a second with every gate green.

This check reads the grey frames the frozen-picture watch already decodes (``frame_freeze.check_frames``), over every
frame of every ``recording`` scene, a render's and a scene preview's:

* the frame's **background** is its most common grey level; a pixel carries content when it differs from it by more
  than :data:`CONTENT_LEVELS`;
* its **canvas** is the longest run of columns, then of rows inside them, mostly background (:data:`CANVAS_ROW_SHARE`):
  the application's shell around it (a sidebar, a header, a toolbar, dark or light) is busy on every column or row it
  crosses and is left out, so it never counts as content;
* inside the canvas, the specks of at most :data:`SPECK_PIXELS` pixels are dropped (the cursor, a caret);
* the frame is **near empty** when what is left covers under :data:`EMPTY_SHARE` of the canvas, once the scene showed
  content on at least :data:`SHOWN_SHARE` of it (a page sparse from its first frame is no emptied canvas).

A run of near-empty frames longer than :data:`EMPTY_SECONDS` is one ``near_empty`` finding (a warning held like a frozen
picture) with its scene, its time, the take's source seconds it plays and the trim that removes it, outside every
blended transition and the video's own fades. A title, a statement or any other scene is never judged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

import numpy as np

from app.services.video_studio.render_checks import NEAR_EMPTY, WARN, Finding, clock

#: The scenes read: only a recording plays a product canvas that a step can empty.
READ_TYPES = frozenset({"recording"})
#: Grey levels off the frame's background that make a pixel content (the watch's copies are area averaged).
CONTENT_LEVELS = 10.0
#: The share of a row or column that must be background for it to be part of the canvas: a shell's header or sidebar
#: fills its rows or columns whole, a canvas's own line (a graph edge) rarely more than half of one.
CANVAS_ROW_SHARE = 0.4
#: The smallest canvas judged, as a share of the frame: a page with no open area has no canvas to empty.
MIN_CANVAS_SHARE = 0.25
#: Content blobs of at most this many pixels of the watch's copy are dropped: the cursor, a caret.
SPECK_PIXELS = 12
#: A canvas whose content covers under this share of it is near empty ...
EMPTY_SHARE = 0.004
#: ... once the scene showed content on at least this share of its canvas.
SHOWN_SHARE = 0.01
#: A run of near-empty frames longer than this is a finding.
EMPTY_SECONDS = 0.8
#: Two near-empty runs with at most this many frames between them are one run.
RUN_GAP = 1
#: A run ending this close to its scene's end is trimmed off the end, one starting this close to its start off the start.
EDGE_SECONDS = 0.25


@dataclass(frozen=True)
class Emptiness:
    """How much of a frame's canvas carries content: its share of the canvas (``None``: no canvas to judge)."""

    share: float | None
    canvas: float


def read_emptiness(frame: Any) -> Emptiness:
    """The content share of *frame*'s canvas (a grey array, any numeric type), cursor and specks left out."""
    from scipy.ndimage import label

    picture = np.asarray(frame, dtype=np.float32)
    if picture.ndim != 2 or picture.size == 0:
        return Emptiness(None, 0.0)
    levels = np.clip(np.rint(picture), 0, 255).astype(np.int64)
    background = float(np.bincount(levels.ravel(), minlength=256).argmax())
    content = np.abs(picture - background) > CONTENT_LEVELS
    # Columns first (a sidebar fills every row it crosses), then rows inside them (a header), then columns again.
    busy = 1.0 - CANVAS_ROW_SHARE
    cols = _longest_run(content.mean(axis=0) <= busy)
    if cols is None:
        return Emptiness(None, 0.0)
    rows = _longest_run(content[:, cols[0] : cols[1]].mean(axis=1) <= busy)
    if rows is None:
        return Emptiness(None, 0.0)
    cols = _longest_run(content[rows[0] : rows[1]].mean(axis=0) <= busy)
    if cols is None:
        return Emptiness(None, 0.0)
    region = content[rows[0] : rows[1], cols[0] : cols[1]]
    canvas = region.size / content.size
    if canvas < MIN_CANVAS_SHARE:
        return Emptiness(None, round(canvas, 3))
    labelled, count = label(region)
    if count:
        sizes = np.bincount(labelled.ravel())
        sizes[0] = 0
        kept = int(sizes[sizes > SPECK_PIXELS].sum())
    else:
        kept = 0
    return Emptiness(kept / region.size, round(canvas, 3))


def _longest_run(mask: Any) -> tuple[int, int] | None:
    best: tuple[int, int] | None = None
    start = None
    for index, value in enumerate([*mask.tolist(), False]):
        if value and start is None:
            start = index
        elif not value and start is not None:
            if best is None or index - start > best[1] - best[0]:
                best = (start, index)
            start = None
    return best


@dataclass
class NearEmptyWatch:
    """The watch over a render's frames, fed each frame the frozen-picture watch reads: judges each frame of a
    recording scene once the scene showed content (:attr:`hits`: ``[watch frame, share]``)."""

    hits: list[list[float]] = field(default_factory=list)
    _scene: Any = None
    _shown: bool = False

    def feed(self, frame: Any, count: int, scene: dict) -> None:
        if scene.get("type") not in READ_TYPES:
            self._scene, self._shown = None, False
            return
        if scene.get("id") != self._scene:
            self._scene, self._shown = scene.get("id"), False
        read = read_emptiness(frame)
        if read.share is None:
            return
        if read.share >= SHOWN_SHARE:
            self._shown = True
        elif self._shown and read.share < EMPTY_SHARE:
            self.hits.append([count, round(read.share, 5)])

    def restore(self, hits: list | None) -> None:
        self.hits = [[int(h[0]), float(h[1])] for h in hits or [] if isinstance(h, list | tuple) and len(h) == 2]

    def kept(self, at: int) -> list[list[float]]:
        """The hits judged before watch frame *at* (a checkpoint's)."""
        return [hit for hit in self.hits if hit[0] < at]


def _source_at(scene: dict, local: float) -> float | None:
    """The take's second the scene plays *local* seconds into it (its ``time_map``), ``None`` without one."""
    points = [(float(a), float(b)) for a, b in scene.get("time_map") or [] if a is not None and b is not None]
    if not points:
        return None
    if local <= points[0][0]:
        return points[0][1]
    for (a0, b0), (a1, b1) in pairwise(points):
        if a0 <= local <= a1:
            return b0 if a1 <= a0 else b0 + (b1 - b0) * (local - a0) / (a1 - a0)
    return points[-1][1]


def trim_words(scene: dict, start: float, end: float) -> tuple[str, dict]:
    """``(what to change, its data)`` for a near-empty span from *start* to *end* (seconds of the document) of
    *scene*: a ``trim_end`` when it runs to the scene's end, a ``trim_start`` when it opens it, else the step to cut."""
    scene_start, scene_end = float(scene.get("start") or 0.0), float(scene.get("end") or 0.0)
    source_from = _source_at(scene, start - scene_start)
    source_to = _source_at(scene, end - scene_start)
    data: dict = {}
    if source_from is not None and source_to is not None:
        data["source_seconds"] = [round(source_from, 2), round(source_to, 2)]
    if scene_end and end >= scene_end - EDGE_SECONDS and source_from is not None:
        data["trim_end"] = round(source_from, 2)
        return (
            f"set the scene's trim_end to {source_from:.2f} s of its take, which ends it before the canvas empties",
            data,
        )
    if start <= scene_start + EDGE_SECONDS and source_to is not None:
        data["trim_start"] = round(source_to, 2)
        return (
            f"set the scene's trim_start to {source_to:.2f} s of its take, which starts it once the canvas is drawn",
            data,
        )
    where = f" ({source_from:.2f} to {source_to:.2f} s of its take)" if "source_seconds" in data else ""
    return (
        f"cut the step that empties the canvas{where} out of the take, or record it again with the canvas kept "
        "drawn through that step",
        data,
    )


def _excused(document: dict, start: float, end: float) -> bool:
    from app.services.video_studio.frame_freeze import blended_windows

    return any(a < end and start < b for a, b in blended_windows(document))


def findings(hits: list, rate: float, document: dict, *, offset: float = 0.0, shown: Any = None) -> list[Finding]:
    """A ``near_empty`` finding for each run of near-empty frames longer than :data:`EMPTY_SECONDS` (*hits*:
    :attr:`NearEmptyWatch.hits`, read at *rate*) outside what *document* blends, timed in the MP4 (*offset*: where it
    starts in *document*), its scene the one its middle frame shows (*shown*: ``frame_freeze.Shown``)."""
    from app.services.video_studio.frame_freeze import Shown

    if not hits:
        return []
    if shown is None:
        shown = Shown.of(document, float(document.get("fps") or rate), rate, offset=offset)
    runs: list[list[list[float]]] = []
    for hit in sorted(hits, key=lambda h: h[0]):
        if (
            runs
            and hit[0] - runs[-1][-1][0] <= RUN_GAP + 1
            and shown.scene(int(hit[0])).get("id") == shown.scene(int(runs[-1][-1][0])).get("id")
        ):
            runs[-1].append(hit)
        else:
            runs.append([hit])
    out: list[Finding] = []
    for run in runs:
        first, last = int(run[0][0]), int(run[-1][0])
        start, end = first / rate, (last + 1) / rate
        if end - start <= EMPTY_SECONDS or _excused(document, start + offset, end + offset):
            continue
        middle = (first + last) // 2
        scene = shown.scene(middle)
        frame = shown.frame(middle)
        at = middle / rate
        fix, data = trim_words(scene, start + offset, end + offset)
        share = max(h[1] for h in run)
        out.append(
            Finding(
                NEAR_EMPTY,
                start,
                end,
                scene.get("id"),
                f"the canvas of a {scene.get('type') or 'scene'} is near empty for {end - start:.1f} s "
                f"({clock(start)} to {clock(end)}): under {100 * EMPTY_SHARE:.1f}% of it shows content once the cursor "
                f"is left out, frame {frame} at {clock(at)}: {fix}",
                WARN,
                {
                    "seconds": round(end - start, 3),
                    "frame": frame,
                    "frame_at": round(at, 3),
                    "share": share,
                    "scene_type": scene.get("type"),
                    **data,
                },
                fix=fix,
                code="near_empty",
                params={"seconds": round(end - start, 1), "start": clock(start), "end": clock(end)},
            )
        )
    return out
