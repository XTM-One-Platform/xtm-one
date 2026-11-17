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

"""Static page chrome drawn at two places in turn on a recording scene of a render (issue #9590).

A page's own chrome - a toolbar, a button over a graph canvas, a panel the step does not act on - never moves while a
canvas lays out or zooms under it. When a recording frame draws it shifted (a canvas copy or a keyframe composited at
another registration, a resynthesised frame carrying the page at another offset), the audience sees the chrome ghost:
a second copy of a button, a stray bar, flickering at a few hertz while the canvas moves. ``double_frames`` never reads
it: a frame whose canvas moved on is no blend of its neighbours, whatever its chrome does.

This check reads the same encoded grey frames the frozen-picture watch decodes (``frame_freeze.check_frames``), every
frame of every ``recording`` scene, in tiles of :data:`TILE` px of the watch's copy. A tile **flips** when it leaves the
picture it had (by :data:`FLIP_LEVELS` grey levels on average) for one to :data:`MAX_AWAY` frames and comes back to it
(within :data:`SAME_LEVELS`): a picture that moves on (a canvas, a scroll), a fade, a counter never comes back; a caret
or a cursor changes too few pixels of a tile. A frame where :data:`MIN_TILES` tiles flip at once is a flicker frame; a
run of :data:`MIN_FRAMES` of them, none further apart than :data:`RUN_GAP` frames, is one ``static_flicker`` finding: a
failure of the picture (``render_checks.PICTURE_KINDS``), with its time, its scene, the box of the tiles that flipped and
the frame where most did, outside every blended transition, declared transition in and the video's own fades.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.services.video_studio.render_checks import FAIL, STATIC_FLICKER, Finding, clock

#: The scenes read: a recording's page chrome is the only picture that must never stand at two places in turn.
READ_TYPES = frozenset({"recording"})
#: The side of a tile, in px of the watch's grey copy (``frame_freeze.ANALYSIS_WIDTH`` wide).
TILE = 8
#: The mean grey levels a tile changes by to have left its picture, and the most it may differ by to be back at it.
FLIP_LEVELS = 6.0
SAME_LEVELS = 2.5
#: The tile's own contrast (grey levels, standard deviation) under which it is flat: nothing there to displace.
MIN_TEXTURE = 4.0
#: The most frames a tile stays away before coming back for the excursion to be a flip.
MAX_AWAY = 3
#: The tiles that flip on one frame for it to be a flicker frame: a button or a bar spans several.
MIN_TILES = 2
#: The flicker frames a run needs to be a finding, and the most watch frames between two of one run.
MIN_FRAMES = 3
RUN_GAP = 6
#: A single excursion over a still page - a **blink** - is a finding on its own once it holds :data:`BLINK_FRAMES`
#: frames away (two frames of a 30 fps watch: two to four of a 60 fps take), flips :data:`BLINK_TILES` tiles at once and
#: comes back to a page that differs from the one it left by :data:`BLINK_STILL_TILES` tiles at most (a cursor): a
#: node's hover dim drawn for 66 ms and gone before the real hover (#9590, retest of #9960), never a run of three.
#: One frame away stays no finding (a frame shifted once), and a page that moves on never comes back still.
BLINK_FRAMES = 2
BLINK_TILES = 6
BLINK_STILL_TILES = 2
#: What to change for it.
REMEDY = (
    "the page's static chrome (a toolbar, a button over the canvas) is drawn at two places in turn while the page "
    "moves under it: record the take again on the current recorder, then preview the scene; report the render with "
    "this frame if a new take still shows it"
)


def _tiles(grey: Any) -> tuple[int, int]:
    rows, cols = grey.shape
    return rows // TILE, cols // TILE


def _tile_means(values: Any, shape: tuple[int, int]) -> Any:
    rows, cols = shape
    cut = values[: rows * TILE, : cols * TILE]
    return cut.reshape(rows, TILE, cols, TILE).mean(axis=(1, 3))


def _tile_texture(grey: Any, shape: tuple[int, int]) -> Any:
    rows, cols = shape
    cut = grey[: rows * TILE, : cols * TILE]
    return cut.reshape(rows, TILE, cols, TILE).std(axis=(1, 3))


def flipped_tiles(frames: list[Any]) -> tuple[int, Any] | None:
    """The tiles of the frames *frames* (oldest first, the newest the one a flip comes back on) that left the picture
    of the oldest and came back to it on the newest, ``(frames away, mask)``; ``None`` when none did.

    ``len(frames) - 2`` is the number of frames away, one to :data:`MAX_AWAY`."""
    if len(frames) < 3:
        return None
    reference, back = frames[0], frames[-1]
    shape = _tiles(reference)
    if shape[0] == 0 or shape[1] == 0:
        return None
    returned = _tile_means(np.abs(back - reference), shape) < SAME_LEVELS
    mask = returned & (_tile_texture(reference, shape) >= MIN_TEXTURE)
    for away in frames[1:-1]:
        if not mask.any():
            return None
        mask &= _tile_means(np.abs(away - reference), shape) > FLIP_LEVELS
    return (len(frames) - 2, mask) if int(mask.sum()) >= MIN_TILES else None


def returns_after(reference: Any, later: list[Any]) -> int | None:
    """The index in *later* (grey frames after *reference*, oldest first) of the first frame on which
    :data:`MIN_TILES` textured tiles of *reference* that left it on every frame before come back to it; ``None`` when
    none does before every tile that left has moved on for good.

    The test of :func:`flipped_tiles` with no bound on the frames away: a take frame read at its own rate is not a
    frame the render reads. A recording scene plays a pause compressed (``spec.IdleCompression``, six times by
    default), so a picture that left for a dozen take frames comes back on the next frame or two the render shows."""
    if not later:
        return None
    shape = _tiles(reference)
    if shape[0] == 0 or shape[1] == 0:
        return None
    textured = _tile_texture(reference, shape) >= MIN_TEXTURE
    away = textured & (_tile_means(np.abs(later[0] - reference), shape) > FLIP_LEVELS)
    for index, frame in enumerate(later[1:], start=1):
        if not away.any():
            return None
        diff = _tile_means(np.abs(frame - reference), shape)
        if int((away & (diff < SAME_LEVELS)).sum()) >= MIN_TILES:
            return index
        away &= diff > FLIP_LEVELS
    return None


def still_between(a: Any, b: Any) -> bool:
    """Whether the grey frames *a* and *b* show one still page: :data:`BLINK_STILL_TILES` tiles at most differ by
    :data:`FLIP_LEVELS` grey levels on average (a cursor drawn at two places)."""
    shape = _tiles(a)
    if shape[0] == 0 or shape[1] == 0 or a.shape != b.shape:
        return False
    return int((_tile_means(np.abs(b - a), shape) > FLIP_LEVELS).sum()) <= BLINK_STILL_TILES


def is_run(hits: list) -> bool:
    """Whether the watch hits *hits* (one run, :attr:`ChromeWatch.hits`) are a finding: :data:`MIN_FRAMES` flicker
    frames, or a blink (a hit flagged so, see :data:`BLINK_FRAMES`) of :data:`BLINK_FRAMES` frames at least."""
    frames = len({int(h[0]) for h in hits})
    if frames >= MIN_FRAMES:
        return True
    return frames >= BLINK_FRAMES and any(len(h) > 6 and h[6] for h in hits)


def same_picture(a: Any, b: Any) -> bool:
    """Whether the grey frames *a* and *b* show one moment of the page: no tile of the frame (a cursor's, a tooltip's
    included) differs by :data:`FLIP_LEVELS` grey levels on average. A page that answered a moving pointer differs at
    least where the pointer is drawn."""
    shape = _tiles(a)
    if shape[0] == 0 or shape[1] == 0 or a.shape != b.shape:
        return False
    return bool((_tile_means(np.abs(b - a), shape) <= FLIP_LEVELS).all())


@dataclass
class ChromeWatch:
    """The watch over a render's frames, fed each frame the frozen-picture watch reads: keeps the last frames of the
    recording scene on screen and, as each arrives, the tiles that left and came back to it (:attr:`hits`: ``[watch
    frame, tiles, x0, y0, x1, y1, blink]``, the box a share of the frame, one per frame away, *blink* 1 when the
    excursion is a blink over a still page, see :data:`BLINK_FRAMES`)."""

    hits: list[list[float]] = field(default_factory=list)
    #: Read the longer excursions too when a shorter one flipped but is no blink: a blink whose away frames differ
    #: among themselves (a canvas dimmed, then dimmed with another layer) also flips tiles between its own away
    #: frames, and that shorter flip hid the blink of the whole excursion (#9590, retest 4c4194f6). The take's gate
    #: replay reads so (``capture_rate._gate_flickers``): it drops every blink the render's watch could read.
    every_away: bool = False
    _kept: list[tuple[int, Any, Any]] = field(default_factory=list)
    _seen: set[int] = field(default_factory=set)

    def feed(self, frame: Any, count: int, scene: dict) -> None:
        if scene.get("type") not in READ_TYPES:
            self._kept.clear()
            return
        scene_id = scene.get("id")
        if self._kept and (self._kept[-1][0] != count - 1 or self._kept[-1][1] != scene_id):
            # A frame not read, or another scene: never compared across it.
            self._kept.clear()
        self._kept.append((count, scene_id, np.asarray(frame, dtype=np.float32)))
        del self._kept[: max(0, len(self._kept) - (MAX_AWAY + 2))]
        greys = [grey for _count, _scene, grey in self._kept]
        for away in range(1, min(MAX_AWAY, len(greys) - 2) + 1):
            found = flipped_tiles(greys[-(away + 2) :])
            if found is None:
                continue
            _away, mask = found
            blink = int(
                away >= BLINK_FRAMES and int(mask.sum()) >= BLINK_TILES and still_between(greys[-(away + 2)], greys[-1])
            )
            ys, xs = np.nonzero(mask)
            height, width = greys[-1].shape
            box = [
                round(float(xs.min() * TILE) / width, 3),
                round(float(ys.min() * TILE) / height, 3),
                round(float((xs.max() + 1) * TILE) / width, 3),
                round(float((ys.max() + 1) * TILE) / height, 3),
            ]
            for index in range(len(self._kept) - away - 1, len(self._kept) - 1):
                frame_at = self._kept[index][0]
                if frame_at in self._seen:
                    continue
                self._seen.add(frame_at)
                self.hits.append([frame_at, int(mask.sum()), *box, blink])
            if not self.every_away or blink:
                break

    def restore(self, hits: list | None) -> None:
        # A hit kept by an older release has no blink flag: read as none.
        self.hits = [
            [int(h[0]), int(h[1]), *(float(v) for v in h[2:6]), int(h[6]) if len(h) == 7 else 0]
            for h in hits or []
            if isinstance(h, list | tuple) and len(h) in (6, 7)
        ]
        self._seen = {int(h[0]) for h in self.hits}

    def kept(self, at: int) -> list[list[float]]:
        """The hits found before watch frame *at* (a checkpoint's)."""
        return [hit for hit in self.hits if hit[0] < at]


def findings(hits: list, rate: float, document: dict, *, offset: float = 0.0, shown: Any = None) -> list[Finding]:
    """A ``static_flicker`` finding for each run of flicker frames (*hits*: :attr:`ChromeWatch.hits`, read at *rate*)
    nothing in *document* blends, timed in the MP4 (*offset*: where it starts in *document*), its scene the one its
    strongest frame shows (*shown*: ``frame_freeze.Shown``), with that frame's encoded number and second and the box of
    every tile that flipped."""
    from app.services.video_studio.double_frames import _excused
    from app.services.video_studio.frame_freeze import Shown

    if not hits:
        return []
    if shown is None:
        shown = Shown.of(document, float(document.get("fps") or rate), rate, offset=offset)
    runs: list[list[list[float]]] = []
    for hit in sorted(hits, key=lambda h: h[0]):
        if runs and hit[0] - runs[-1][-1][0] <= RUN_GAP:
            runs[-1].append(hit)
        else:
            runs.append([hit])
    out: list[Finding] = []
    for run in runs:
        if not is_run(run):
            continue
        first, last = int(run[0][0]), int(run[-1][0])
        start, end = first / rate, (last + 1) / rate
        if _excused(document, start + offset, end + offset):
            continue
        strongest = max(run, key=lambda h: h[1])
        count = int(strongest[0])
        scene = shown.scene(count)
        frame = shown.frame(count)
        at = count / rate
        box = [
            min(h[2] for h in run),
            min(h[3] for h in run),
            max(h[4] for h in run),
            max(h[5] for h in run),
        ]
        frames = len({int(h[0]) for h in run})
        out.append(
            Finding(
                STATIC_FLICKER,
                start,
                end,
                scene.get("id"),
                f"{frames} frames of a {scene.get('type') or 'scene'} draw the page's static chrome at another place "
                f"and back ({clock(start)} to {clock(end)}, box {box}), frame {frame} at {clock(at)} the worst: {REMEDY}",
                FAIL,
                {
                    "frames": frames,
                    "frame": frame,
                    "frame_at": round(at, 3),
                    "box": box,
                    "tiles": int(strongest[1]),
                    "scene_type": scene.get("type"),
                },
                code="static_flicker",
                params={"frames": frames, "start": clock(start), "end": clock(end)},
            )
        )
    return out
