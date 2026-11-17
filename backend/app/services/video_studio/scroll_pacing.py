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

"""A recording's scrolls paced at its frame rate.

Chromium's screencast hands over a frame each time the page has painted and the
frame before was taken: at 3200x1800 (a 1600x900 window at pixel ratio 2) that
is about 2 frames a second, at pixel ratio 1 about 7. A smooth scroll in the
browser then reached the insert as 2 to 7 positions a second - lag, at any
render quality.

A scroll is a vertical translation of a page already painted. So while a step
scrolls, the recorder logs where its scroller stands at 60 Hz (``capture``:
the window's ``scrollY``, or a scroll container's ``scrollTop`` and box), and
once recorded each scroll whose keyframes are fewer than its take's rate
(``TakeScript.fps``: 30, 60 for a 60 fps scene) is drawn again at that rate
(:func:`pace`): every frame of the scroll is the page at the offset the log
gives for its instant, cut from the keyframes that painted those rows - the
one before it first, then the one after. Each keyframe's own offset is what
it painted for a window's scroll (the screencast's ``scrollOffsetY``),
else read off the pixels (:func:`register`, against the keyframe before it), the lag
between the log's clock and the paint's fitted on them (:func:`clock_lag`), so
the rows cut from two keyframes meet without a seam and a frame on a
keyframe's instant is that keyframe. A wheel-driven scroll is played through
the instants it reached each increment (:func:`moves`), never holding where
the wheel stalled. What never moves - a fixed header, a sidebar, what lies
outside a scroll container - is found on every keyframe of the scroll and kept
as painted (:func:`static_bands`).

A wheel over a canvas or a WebGL surface - a graph - zooms it rather than
scrolling the page: no scroller moves, and the recorder logged the
canvas under the cursor and, at each turn of its wheel, the delta sent and the
canvas's 2D transform where it has one (``capture``). Such a zoom is drawn
again at the take's rate too (:func:`pace`): how the canvas moved from each
keyframe to the next is read off their pixels (:func:`register_zoom`: a zoom
about the cursor or the canvas's centre, refined to the pixel), and each frame
between them is the NEARER keyframe moved by its share of that zoom - the one
before zoomed on as far as the log says (its transform's scale, else the
wheel's delta), or the one after zoomed back the rest -, never the two
cross-dissolved (a blend draws every label twice, a double image).
A pair whose zoom is not registered holds the nearer keyframe (``held``,
``held_not_blended``); the entry says ``synthesis: warped``, and a paced zoom
without it, drawn by the old cross-dissolve, is a save finding
(``quality``: ``double_image``). What stands still over the canvas (a
toolbar, a legend) is kept as the nearer keyframe painted it, the one the
canvas under it is drawn from. A box the page draws over the canvas
that moves with it (a label a graph lays over a node, which the zoom carries)
is drawn with the canvas, never held (``riding``: held, its inside read
unwarped in a warped canvas, the labels crossing its edge split); the entry says
``held_from: standing``, and a warped zoom that held its boxes before (from the
keyframe before, or from the nearer one whether or not they moved) is a
``double_image`` finding too. Within one take the boxes are judged across
every zoom of the same canvas (:func:`carried_boxes`): a box held on one
wheel step while another step of the take found labels riding that canvas, and
neither standing still on its own step nor held at the same place on another
(a toolbar), is drawn with the canvas too (``carried``), never held on one step
and drawn on the next.

The scroller is the one that moved: the window, or a container the
recorder watched - the deepest under the cursor, one holding the element
brought into view, one that fired a scroll - named in the log (``element``: its
tag, id and classes). A panel under the wheel scrolled to its end, then the
page: each is paced over the span it moved in; two moving at once are not. A
frame's offset stays between the offsets the keyframes either side painted, so
a slow page's log - its main thread answering once a paint - never carries a
frame past them. A page that paints slowly also hands over a frame
saying the next offset over the paint before: each offset a window's keyframe
says it painted is checked against its pixels (:func:`_checked`), a keyframe
painting the offset of the one before is the paint lagging, never a stop
(:func:`_anchors`), and a scroll the wheel turned on a clock plays at the pace
it turned, as late as the page painted it (:func:`_wheel_late`): what a
compositor shows. A scroll that outran the page's paints - two keyframes sharing
none of its rows - holds the keyframe before there, plays at the share of its
frames that moved, and the take's notes say the ``seconds`` its step takes for
every two paints to overlap (``outran``, :attr:`Paced.notes`).

A step that changes the page has its CSS transitions paced the same way
(``transition_pacing``): its keyframes blended at the take's rate over
the window each transition runs. Each scroll and each transition of a take then
says the rate its keyframes were painted at and the rate it plays at
(``painted``, ``fps``, ``paced``), each step the lowest of them and the element
it scrolled (``painted_fps``, ``fps``, ``scrolled_element``, :func:`summarise`),
and a take its real frame rate (:func:`take_rate`): the lowest a step plays at,
and that step - for a take recorded before, the frames it painted over its
length. A take under :data:`MIN_TAKE_FPS` on a screen insert or a recording
scene is a save finding naming that step (``quality``: ``take_frame_rate``);
its answer says each scroll and transition step's rates
(:func:`scroll_steps_words`).

**Limits.** A paced frame is the page translated, cut from the
keyframes either side: what does not move with the content is right only in
the bands :func:`static_bands` finds - rows at the scroller's top and bottom,
columns at its sides that every keyframe paints alike. A ``position: fixed``
or sticky element elsewhere (a chat bubble, a cookie banner in the middle of
the page) slides with the content between keyframes, and so does what the
page animates on its own during the scroll - a CSS animation, a video
playing: frozen at the keyframe it is cut from, translated with the rows it
sits on. A flat page, or two keyframes sharing under :data:`MIN_OVERLAP` of
their rows, give :func:`register` nothing to match: the offset the log or the
screencast gave stands there. Content that repeats within :data:`SEARCH` rows
(identical list rows, a striped background) can register a period off, and
one such pair skews the clock lag fitted on them all (:func:`clock_lag`): a
window's keyframes say the offset they painted, which is checked
against their pixels before any is registered. A scroll faster than a screen between two
paints holds the keyframe before it where no keyframe painted its rows
(:data:`MAX_GAP`), and the take's notes say the ``seconds`` its step takes
for every two paints to overlap.
"""

from __future__ import annotations

import bisect
import copy
import itertools
import logging
import math
from collections import Counter, OrderedDict
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The rates a take is paced at (``TakeScript.fps``).
PACED_RATES = (30, 60)
DEFAULT_TAKE_FPS = 30
#: Under this rate a take's scrolls step on screen: a save finding where a screen insert or a recording scene plays it.
MIN_TAKE_FPS = 24
#: How often the recorder reads its scroller while a step scrolls.
SCROLL_SAMPLE_HZ = 60
#: A scroll painted at this share of its take's rate or more keeps its own frames - unless they step (:func:`cadence`).
PAINTED_ENOUGH = 0.9
#: A scroll whose distance from one frame of its take to the next varies by more than this share of its mean steps on
#: screen: a move-move-hold cadence, "stepping motion" (:func:`cadence`).
STEPPING_SHARE = 0.5
#: A scroll painted at its rate keeps its frames only while they move evenly: a stdev of the distance a frame within
#: this share of its mean, no frame repeating the one before and none past twice the mean (:func:`even`). Past it they
#: are drawn again at the take's rate like a scroll painted at fewer frames.
EVEN_SHARE = 0.25
#: CSS pixels a frame under which a scroll's first and last frames are its rest, not its motion.
_AT_REST = 0.5
#: A scroll painted at its rate whose frames move unevenly is drawn again from keyframes this share of its scroller's
#: height apart (:func:`_spread`), at most :data:`MAX_RESTEP_SOURCES` of them: a 60 fps take never decodes a hundred
#: full frames at once. One that would take more is kept as painted, its cadence said.
RESTEP_REACH = 0.4
MAX_RESTEP_SOURCES = 24
#: CSS pixels a scroller must move for its scroll to be paced; two scrollers moving past it at once are not.
MIN_MOTION = 2.0
#: The keyframes are compared at this many device pixels a step, then to the pixel around the best match, over
#: :data:`SEARCH` CSS pixels each side of the offset the log gives.
COARSE = 4
SEARCH = 24
#: Grey levels two rows (or columns) may differ by on average and still be the same pixels: what stands still.
STILL_LEVELS = 2.5
#: The least share of a scroller's rows two keyframes must share to be registered, and what is left moving.
MIN_OVERLAP = 0.15
MIN_MOVING = 0.2
#: Device px a run of rows two keyframes paint alike at the same screen rows while the content moves must span to be a
#: sticky or fixed band (:func:`pinned_bands`): a header, a table's header row - never a line of text.
MIN_BAND = 12
#: The most of its scroller's height a sticky or fixed band may span: rows alike over more are the content not
#: moved between two keyframes (a paint lagging) or the scroller as a whole, never a band.
MAX_BAND_SHARE = 0.35
#: The pairs of keyframes, the content moving between each, a band must stand still on to be pinned: one pair
#: alone is a paint that lagged.
MIN_BAND_PAIRS = 2
#: Grey levels under which a band's rows on a keyframe are one flat colour (a blank repaint): never drawn as it.
BLANK_LEVELS = 2.0
#: Grey levels a flat band row on a keyframe must stand from the band's own colour there to be a blank repaint.
BLANK_APART = 24.0
#: Grey levels a held band may differ by from the page's own paint of it once the scroll settled before its take says so.
BAND_REPAINT_LEVELS = 24.0
#: The share of a band's width the page's fixed or sticky element under it must cover to vouch for it: not all
#: of it, so a full-width bar the scroller's scrollbar or a sticky header's inset padding narrows still does.
BAND_WIDTH_SHARE = 0.9
#: The runs refused as bands a scroll's answer lists at most (``rejected``).
MAX_REJECTED = 12
#: A held band's rows differing from the frame before by this many grey levels on average moved (:func:`band_blinks`);
#: back within this many frames, they blinked. The instants a scroll's answer names at most.
BLINK_LEVELS = 12.0
BLINK_FRAMES = 3
#: Rows a band may stand off the frame before and still be the band in place: a decoded MP4 moves an edge by a
#: device row; a header pushed and back moves it by several.
BLINK_SHIFT = 1
MAX_BLINKS = 12
#: Rows of a frame no keyframe painted (a scroll faster than a screen a keyframe): up to this share are drawn from the
#: nearest row painted; past it the frame holds the keyframe before it.
MAX_GAP = 0.02
#: The JPEG quality paced frames are written at: the screencast's own.
QUALITY = 95
#: The farthest from a canvas zoom's span the keyframe before or after it may be painted and still be drawn from
#: (:func:`zoom_sources`): as ``transition_pacing.MAX_LATE`` bounds the keyframe after a transition.
ZOOM_KEYFRAME_REACH = 0.75
#: Why a zoom whose keyframes all lie outside its stretch is not paced: the take holds the last frame painted.
OUTSIDE_STRETCH = "its keyframes were painted outside its stretch"
#: The most the scroll log's clock (the page's main thread) and the paint's (the screencast's stamps) differ by.
MAX_LAG = 0.1
#: The share of its scroller's rows a scroll may move between two paints for every frame between them to be cut from
#: the two: what the seconds a take advises for a scroll that outran its paints keep to.
OVERLAPPING = 0.6
#: A canvas zoomed by the wheel. Its keyframes are compared at these widths of its box (device px over a whole
#: factor), coarse to fine; on the coarsest, a zoom of up to :data:`ZOOM_REACH` (ln of its scale, either way) about the
#: cursor or the box's centre is searched at :data:`ZOOM_STEP`, then refined on each finer one.
ZOOM_WIDTHS = (200, 400, 800)
ZOOM_REACH = 1.4
ZOOM_STEP = 0.02
#: Device px a corner of the canvas moves under a zoom for its keyframes to differ by it.
ZOOM_MOVED_PX = 1.0
#: The least share of what tells two keyframes apart a zoom explains for its keyframes to be registered.
ZOOM_EXPLAINS = 0.25
#: Fixed points a side (over the canvas's box) a pair no zoom about its anchors explains is searched again from.
ZOOM_GRID = 5
#: Device px about the cursor a wheel zoom's pair is judged without when no zoom explains it whole: what the page
#: lights under a pointer that stood still while the canvas moved (a graph's hovered link and its card) changes there
#: as the zoom brings another link under it, which no zoom explains.
HOVER_PX = 120.0
#: Parts a side a canvas's box is cut into to say where what the best zoom leaves of a held pair sits, and the share
#: of it the parts named hold: a few parts, a change in one place (a card, a label); most, the picture changed whole.
LEFT_PARTS = 4
LEFT_SHARE = 0.8
#: Grey levels two keyframes differ by somewhere on the coarsest width when the canvas moved between them.
ZOOM_SAME_LEVELS = 4.0
#: Grey levels a pixel's neighbours differ by on a width for it to be drawn (text, an icon) rather than plain.
DRAWN_LEVELS = 6.0
#: Device px the rows and columns each keyframe painted fade over where what it painted ends.
FEATHER = 24
#: Frames of a zoom held (the keyframes either side not registered) past this share: it keeps its keyframes.
MAX_HELD = 0.25
#: Why a pair of a canvas zoom's keyframes stayed unregistered (``held_why``), in the step's words.
HELD_WHY = {
    "no_scale": "with no registered pair to scale the canvas's log by",
    "log_still": "the canvas's log did not move across",
    "past_reach": "the log says zoomed past the search's reach",
    "unexplained": "no zoom explains",
    # Counted in frames, not pairs: a registered pair's frame neither keyframe painted enough of.
    "uncovered": "frame(s) neither keyframe painted enough of",
}
#: How a paced canvas zoom's frames are drawn: the nearer keyframe moved by its share of the zoom, never
#: two keyframes cross-dissolved. A paced zoom without it was drawn by the cross-dissolve (a double image).
ZOOM_SYNTHESIS = "warped"
#: Which keyframe a paced canvas zoom's frames hold what stands over the canvas from: the nearer one, the
#: canvas under it drawn from the same. A warped zoom holding such boxes without it held them from the keyframe before
#: while the canvas came from the one after: labels split or offset on those frames (``quality``: ``double_image``).
ZOOM_HELD_FROM = "standing"
#: Only the boxes over a canvas that stand still or change in place (a toolbar, a hover) are held: one the
#: zoom carries (:func:`riding_boxes`) is drawn with the canvas. A zoom paced with ``held_from: nearer`` held
#: every box, a label riding the canvas split against the canvas warped around it.
#: A box rides the canvas when the zoom draws it from the keyframe before at most this share of the grey levels it
#: stands apart from it by unmoved, and it stands apart by more than :data:`STILL_LEVELS`.
RIDING_SHARE = 0.6
#: A box no single pair of keyframes tells rides the canvas when the zoom draws it, over the whole zoom, closer than
#: this share of the grey levels it stands apart from itself unmoved.
RIDING_OVERALL = 1.0
#: :func:`double_image_score`: grey levels of a softened gradient that make an edge, the fewest edges a picture has
#: of its own for the score to tell, and the share of both sets a frame draws past which it is a double image.
DOUBLE_EDGE_LEVELS = 8.0
#: The share of an edge's contrast a frame still draws it at when it is a blend of two pictures (a third of it).
DOUBLE_EDGE_FAINT = 0.3
DOUBLE_EDGE_MIN = 200
DOUBLE_IMAGE_SHARE = 0.5
#: The ln of a canvas's 2D transform's scale changes by this much for its zoom to follow it.
TRANSFORM_MOVED = 0.005
#: Frames of a zoom drawn at once.
_DRAWING = 4
#: Decoded keyframes kept at once (a 3200x1800 frame is 17 MB), and paced frames waiting to be written.
_KEPT = 4
_SAVING = 8
#: Why a scroll that moved nothing was not paced: the recorder looked for a canvas under the wheel too, so
#: nothing on the page moved; one logged before it looked may have zoomed a canvas it did not see.
NOTHING_MOVED = "nothing moved under the wheel"
DID_NOT_MOVE = "it did not move"
PAINTED_AT_RATE = "painted at its rate"
#: A transition whose keyframes are all alike (``transition_pacing``): it changed nothing on screen.
NOTHING_CHANGED = "nothing changed on its keyframes"
#: What a scroll or a transition says when nothing moved on screen: it counts in no rate (:func:`counts`).
UNSEEN = frozenset({DID_NOT_MOVE, NOTHING_MOVED, NOTHING_CHANGED})


@dataclass
class Scroll:
    """One scroll of a take, as :func:`pace` read it: when, which scroller, and where it stood at each sample."""

    span: tuple[float, float]
    #: ``(t, offset)`` in seconds of the take and CSS pixels, in order; a canvas's ``(t, progress)``: the ln of its
    #: transform's scale, or the wheel's delta sent (:attr:`progress`).
    log: list[tuple[float, float]]
    #: ``window``, ``element`` (a scroll container) or ``canvas``, with its client box in CSS pixels of the viewport ...
    scroller: str = "window"
    box: tuple[float, float, float, float] | None = None
    #: ... and how the page names a container (``main#content.panel``).
    element: str | None = None
    #: Why it was not paced (two scrollers moved, it moved sideways), when it was not.
    unpaceable: str | None = None
    #: The recorder looked for a canvas under the wheel: a scroll that moved nothing moved nothing on the page.
    probed: bool = False
    #: A canvas's: where the wheel turned (CSS px of the viewport), what its log follows (``transform``, ``wheel``),
    #: and the boxes of what the page draws over it (CSS px of the viewport).
    anchor: tuple[float, float] | None = None
    progress: str | None = None
    over: list[tuple[float, float, float, float]] = field(default_factory=list)
    #: A canvas's: the boxes the page draws over it once its zoom settled that were not there as the wheel began - a
    #: tooltip the cursor opened over a link the zoom brought under it (CSS px of the viewport).
    appeared: list[tuple[float, float, float, float]] = field(default_factory=list)
    #: A canvas's: the indices of ``over`` the page still drew at the same place once its zoom settled (``capture``)
    #: - a toolbar, a Legend button pinned over the canvas. Never carried by the zoom, whatever its pixels say: a box
    #: mostly see-through reads the canvas moving under it as the box riding it, and the page chrome drawn with the
    #: canvas shows at a second offset on every frame drawn from a keyframe (#9590).
    stayed: frozenset[int] = frozenset()
    #: ``(t, share)`` of the wheel that turned it on a clock (``capture``): the share of its pixels it had turned
    #: at each instant, from 0 to 1 - the motion a compositor shows, however late the page paints it.
    wheel: tuple[tuple[float, float], ...] | None = None
    #: The page's ``position: fixed`` and ``position: sticky`` boxes as the scroll began and once it settled
    #: (``capture``): ``(x, y, w, h, position)`` in CSS px of the viewport; ``None``: not read. A band is pinned
    #: only on one of them.
    fixed: list[tuple[float, float, float, float, str]] | None = None

    def offset(self, t: float) -> float:
        """Where the scroller stood at *t* (CSS px), between the samples either side."""
        times = [sample[0] for sample in self.log]
        i = bisect.bisect_right(times, t)
        if i <= 0:
            return self.log[0][1]
        if i >= len(self.log):
            return self.log[-1][1]
        (t0, y0), (t1, y1) = self.log[i - 1], self.log[i]
        return y0 if t1 <= t0 else y0 + (y1 - y0) * (t - t0) / (t1 - t0)

    @property
    def motion(self) -> float:
        offsets = [sample[1] for sample in self.log]
        return max(offsets) - min(offsets) if offsets else 0.0


def moves(log: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The motion a scroll *log* (``(t, offset)`` at 60 Hz) records: the scroller at rest before it, each instant it
    reached a new offset, at rest after it. A wheel-driven scroll lands on each increment and stalls there until the
    next (a browser that does not animate the wheel): played through the instants it arrived, the scroll moves on
    between them as the increments meant it to, never holding where the wheel stalled."""
    if len(log) < 3:
        return list(log)
    kept = [log[0]]
    for previous, sample in itertools.pairwise(log):
        if sample[1] == previous[1]:
            continue
        if len(kept) == 1 and previous[0] > kept[0][0]:
            kept.append(previous)  # the last instant at rest: the motion starts after it
        kept.append(sample)
    if log[-1][0] > kept[-1][0]:
        kept.append(log[-1])
    return kept


def canvas_progress(ticks: Sequence[Sequence[Any]]) -> tuple[list[tuple[float, float]], str]:
    """How far a canvas's zoom had gone at each turn of the wheel the recorder logged (``[t, delta sent, [a, b, c, d,
    e, f] or null]``): the ln of its 2D transform's scale where every turn read one that changed (``transform``),
    else the wheel's delta sent (``wheel``: a WebGL canvas, or a 2D one that draws under a transform it resets)."""
    times = [float(tick[0]) for tick in ticks]
    transforms = [tick[2] if len(tick) > 2 else None for tick in ticks]
    if all(isinstance(m, list | tuple) and len(m) == 6 for m in transforms):
        scales = [
            0.5 * math.log(max(abs(float(m[0]) * float(m[3]) - float(m[1]) * float(m[2])), 1e-12)) for m in transforms
        ]
        if max(scales) - min(scales) >= TRANSFORM_MOVED:
            return list(zip(times, scales, strict=True)), "transform"
    return [(t, float(tick[1])) for t, tick in zip(times, ticks, strict=True)], "wheel"


def _canvas_scroll(span: tuple[float, float], surface: dict) -> Scroll | None:
    """The zoom of the canvas the recorder found under the wheel (``surface``: ``{box, at, ticks, ...}``), or ``None``
    when its entry cannot be read."""
    box, ticks = (
        surface.get("box"),
        [t for t in surface.get("ticks") or [] if isinstance(t, list | tuple) and len(t) >= 2],
    )
    if not isinstance(box, list | tuple) or len(box) != 4 or len(ticks) < 2:
        return None
    at = surface.get("at")
    log, progress = canvas_progress(ticks)

    def rect(values: Sequence[Any]) -> tuple[float, float, float, float]:
        return float(values[0]), float(values[1]), float(values[2]), float(values[3])

    return Scroll(
        span,
        log,
        scroller="canvas",
        box=rect(box),
        probed=True,
        anchor=(float(at[0]), float(at[1])) if isinstance(at, list | tuple) and len(at) == 2 else None,
        progress=progress,
        over=[
            rect(drawn) for drawn in surface.get("over") or [] if isinstance(drawn, list | tuple) and len(drawn) == 4
        ],
        appeared=[
            rect(drawn)
            for drawn in surface.get("appeared") or []
            if isinstance(drawn, list | tuple) and len(drawn) == 4
        ],
        stayed=frozenset(i for i in surface.get("stayed") or [] if isinstance(i, int) and not isinstance(i, bool)),
    )


def _moving(track: Sequence[tuple[float, ...]]) -> tuple[float, float]:
    """``(the last instant at rest before it moved, the instant it last moved)`` of a scroller's *track* or a scroll's
    log: ``(t, offset, ...)`` in order."""
    changed = [i for i in range(1, len(track)) if track[i][1] != track[i - 1][1]]
    return track[changed[0] - 1][0], track[changed[-1]][0]


def read_scrolls(raw: dict) -> list[Scroll]:
    """The :class:`Scroll` s the recorder logged in one entry (``capture._Recorder.scroll_log``): ``{"t": [start, end],
    "samples": [[t, scrollY, scrollX, [[id, top, left, [x, y, w, h]], ...]], ...], "names"?: {id: name}, "surface"?:
    {...}}`` - the window and every scroll container it watched, each named as the page names it, and for a
    ``scroll`` step the canvas under the wheel. Its scroller is the one that moved, the canvas when none did;
    several that moved one after the other (a panel under the wheel scrolled to its end, then the page) are a
    scroll each, over the span it moved in; several moving at once are one scroll, not paced. Empty for an entry with
    no sample."""
    samples = [s for s in raw.get("samples") or [] if isinstance(s, list | tuple) and len(s) >= 3]
    if not samples:
        return []
    span = (
        (float(raw["t"][0]), float(raw["t"][1]))
        if len(raw.get("t") or []) == 2
        else (float(samples[0][0]), float(samples[-1][0]))
    )
    tracks: dict[str, list[tuple[float, float, float]]] = {
        "window": [(float(s[0]), float(s[1]), float(s[2])) for s in samples]
    }
    boxes: dict[str, tuple[float, float, float, float]] = {}
    names = raw.get("names") if isinstance(raw.get("names"), dict) else {}
    for s in samples:
        for entry in s[3] if len(s) > 3 and isinstance(s[3], list) else []:
            key = f"element:{entry[0]}"
            tracks.setdefault(key, []).append((float(s[0]), float(entry[1]), float(entry[2])))
            boxes[key] = tuple(float(v) for v in entry[3])  # type: ignore[assignment]

    def moved(track: list[tuple[float, float, float]], axis: int) -> float:
        values = [sample[axis] for sample in track]
        return max(values) - min(values)

    surface = raw.get("surface")
    if (
        all(moved(track, 1) < MIN_MOTION and moved(track, 2) < MIN_MOTION for track in tracks.values())
        and isinstance(surface, dict)
        and (canvas := _canvas_scroll(span, surface))
    ):
        return [canvas]
    # The log's first sample holds every scroller watched before the step moved, on the clock its samples are stamped
    # on - never the recorder's, which opened the span a round trip of the page earlier.
    first = tracks["window"][0][0]

    def scroll_of(key: str, within: tuple[float, float]) -> Scroll:
        track = tracks[key]
        kept = [sample for sample in track if within[0] <= sample[0] <= within[1]] or track
        scroll = Scroll(
            within,
            moves([(t, top) for t, top, _left in kept]),
            scroller="window" if key == "window" else "element",
            box=boxes.get(key),
            element=None if key == "window" else str(names.get(key.split(":", 1)[1]) or "") or None,
            probed="surface" in raw,
            fixed=_fixed(raw.get("fixed")),
        )
        if moved(kept, 2) >= MIN_MOTION:
            scroll.unpaceable = "it scrolled sideways"
        elif key != "window" and track[0][0] > first and track[0][1] != track[-1][1]:
            # A container first seen once it had begun to scroll: where it started is not known.
            scroll.unpaceable = "its scroll container was found once it had begun to scroll"
        return scroll

    moving = sorted((_moving(track), key) for key, track in tracks.items() if moved(track, 1) >= MIN_MOTION)
    if len(moving) <= 1:
        # One scroller moved: the wheel's turns are its own.
        alone = scroll_of(moving[0][1] if moving else "window", span)
        alone.wheel = _wheel(raw.get("wheel"))
        return [alone]
    if any(earlier[1] > later[0] for (earlier, _a), (later, _b) in itertools.pairwise(moving)):
        scroll = scroll_of(max(moving, key=lambda run: moved(tracks[run[1]], 1))[1], span)
        scroll.unpaceable = "two scrollers moved at once"
        return [scroll]
    # One after the other: each over its own span, cut halfway between the end of one and the start of the next.
    cuts = [span[0], *((a[1] + b[0]) / 2 for (a, _x), (b, _y) in itertools.pairwise(moving)), span[1]]
    return [scroll_of(key, (cuts[i], cuts[i + 1])) for i, (_run, key) in enumerate(moving)]


def _fixed(raw: object) -> list[tuple[float, float, float, float, str]] | None:
    """The page's fixed and sticky boxes the recorder read for a scroll (``capture``): ``(x, y, w, h, position)``
    in CSS px of the viewport; ``None`` when it read none (a take recorded before, a page that would not say)."""
    if not isinstance(raw, list):
        return None
    boxes: list[tuple[float, float, float, float, str]] = []
    for entry in raw:
        if not isinstance(entry, list | tuple) or len(entry) < 4:
            continue
        try:
            x, y, w, h = (float(v) for v in entry[:4])
        except (TypeError, ValueError):
            continue
        boxes.append((x, y, w, h, str(entry[4]) if len(entry) > 4 else "fixed"))
    return boxes


def _wheel(raw: object) -> tuple[tuple[float, float], ...] | None:
    """The wheel's turns the recorder logged (``{t0, pixels, turns: [[elapsed, sent], ...]}``) as ``(t, share)``
    on the take's clock, the points on a straight line between their neighbours left out; ``None`` without any."""
    if (
        not isinstance(raw, dict)
        or not raw.get("pixels")
        or not isinstance(raw.get("turns"), list)
        or len(raw["turns"]) < 2
    ):
        return None
    t0, pixels = float(raw.get("t0") or 0.0), float(raw["pixels"])
    points = [(t0 + float(elapsed), float(sent) / pixels) for elapsed, sent in raw["turns"]]
    kept = [points[0]]
    for k in range(1, len(points) - 1):
        (ta, sa), (t, s), (tb, sb) = kept[-1], points[k], points[k + 1]
        if tb <= ta or abs(sa + (sb - sa) * (t - ta) / (tb - ta) - s) * abs(pixels) >= 0.5:
            kept.append(points[k])
    kept.append(points[-1])
    return tuple(kept)


def cadence(shown: Sequence[float], *, painted: float) -> dict | None:
    """How evenly a scroll moves on its take's frames: *shown* the offset (CSS px) each frame of the take shows over
    its span, in order, at the take's rate; *painted* the rate its keyframes were painted at. ``{painted_fps, max,
    mean, stdev, stepping}`` of the distance it moves from one frame to the next, its rest before and after left out -
    ``stepping`` when the stdev exceeds :data:`STEPPING_SHARE` of the mean (a frame repeated, then a jump of twice
    the mean); ``None`` for a scroll that moved over fewer than two frames."""
    import statistics

    moved = [abs(b - a) for a, b in itertools.pairwise(shown)]
    first = next((i for i, d in enumerate(moved) if d >= _AT_REST), len(moved))
    last = next((len(moved) - i for i, d in enumerate(reversed(moved)) if d >= _AT_REST), first)
    moved = moved[first:last]
    if len(moved) < 2:
        return None
    mean, stdev = statistics.fmean(moved), statistics.pstdev(moved)
    return {
        "painted_fps": round(painted, 2),
        "min": round(min(moved), 1),
        "max": round(max(moved), 1),
        "mean": round(mean, 1),
        "stdev": round(stdev, 1),
        "stepping": stdev > STEPPING_SHARE * mean,
    }


def even(steady: dict) -> bool:
    """Whether a scroll's :func:`cadence` moves evenly enough to be kept as painted (:data:`EVEN_SHARE`)."""
    mean = float(steady["mean"])
    return float(steady["stdev"]) <= EVEN_SHARE * mean and float(steady["min"]) > 0 and float(steady["max"]) <= 2 * mean


def _painted_cadence(
    scroll: Scroll, frames: Keyframes, times: list[float], painted: dict[str, float] | None, *, fps: int, rate: float
) -> dict | None:
    """The :func:`cadence` of a window's scroll kept as painted: each frame of the take at *fps* shows the keyframe
    at or before it, at the offset the screencast says it painted (*painted*, CSS px by file name); ``None`` for a
    scroll container, or keyframes that did not say."""
    if scroll.scroller != "window" or not painted:
        return None
    said = [(t, painted[name]) for t, (_at, name) in zip(times, frames.frames, strict=True) if name in painted]
    if len(said) < 2:
        return None
    stamps = [t for t, _offset in said]
    start, end = scroll.span
    shown = []
    for n in range(math.ceil(start * fps - 1e-6), math.floor(end * fps + 1e-6) + 1):
        k = bisect.bisect_right(stamps, n / fps) - 1
        if k >= 0:
            shown.append(said[k][1])
    return cadence(shown, painted=rate)


def _spread(
    scroll: Scroll, frames: Keyframes, sources: list[int], painted: dict[str, float] | None, *, ratio: float
) -> list[int] | None:
    """The keyframes a window's scroll painted at its rate but unevenly is drawn again from (:data:`RESTEP_REACH`): the
    first, each next one the scroll moved that share of the window's height from the one kept before, the last;
    ``None`` when a keyframe did not say its offset or more than :data:`MAX_RESTEP_SOURCES` would be needed."""
    if not painted or len(sources) < 2 or any(frames.frames[i][1] not in painted for i in sources):
        return None
    offsets = [painted[frames.frames[i][1]] for i in sources]
    reach = RESTEP_REACH * (scroll.box[3] if scroll.box is not None else frames.rgb(sources[0]).shape[0] / ratio)
    kept = [0]
    for j in range(1, len(sources) - 1):
        if abs(offsets[j] - offsets[kept[-1]]) >= reach:
            kept.append(j)
    kept.append(len(sources) - 1)
    return [sources[j] for j in kept] if len(kept) <= MAX_RESTEP_SOURCES else None


def cadence_words(steady: object) -> str:
    """`` (stepping motion: its scroll moved up to 21 px a frame, stdev 9.8 px against a 10.6 px mean, painted at
    58 fps)`` for a step whose scroll steps on its take's frames (:func:`cadence`); empty for any other."""
    if not isinstance(steady, dict) or not steady.get("stepping"):
        return ""
    return (
        f" (stepping motion: its scroll moved up to {float(steady['max']):g} px a frame, stdev {float(steady['stdev']):g} px"
        f" against a {float(steady['mean']):g} px mean, painted at {float(steady['painted_fps']):g} fps)"
    )


def _compact(scroll: Scroll) -> dict:
    """What a take keeps of one scroll once paced: its scroller, the element the page names it by, and its log, rounded."""
    digits = 4 if scroll.progress == "transform" else 1
    kept: dict[str, Any] = {
        "t": [round(scroll.span[0], 4), round(scroll.span[1], 4)],
        "scroller": scroll.scroller,
        "log": [[round(t, 4), round(value, digits)] for t, value in scroll.log],
    }
    if scroll.element:
        kept["element"] = scroll.element
    if scroll.box is not None:
        kept["box"] = [round(v, 1) for v in scroll.box]
    if scroll.progress is not None:
        kept["progress"] = scroll.progress
    if scroll.wheel is not None:
        kept["wheel"] = [[round(t, 4), round(share, 4)] for t, share in scroll.wheel]
    return kept


# ── Pixels ────────────────────────────────────────────────────────────────


class Keyframes:
    """The keyframes of a take, decoded on demand, the last few kept."""

    def __init__(self, workdir: str, frames: Sequence[tuple[float, str]]) -> None:
        self.workdir = Path(workdir)
        self.frames = list(frames)
        self._kept: OrderedDict[int, Any] = OrderedDict()

    def rgb(self, index: int) -> Any:
        import numpy as np
        from PIL import Image

        if index in self._kept:
            self._kept.move_to_end(index)
            return self._kept[index]
        with Image.open(self.workdir / self.frames[index][1]) as opened:
            picture = np.asarray(opened.convert("RGB"))
        self._kept[index] = picture
        while len(self._kept) > _KEPT:
            self._kept.popitem(last=False)
        return picture

    def grey(self, index: int) -> Any:
        import numpy as np

        return self.rgb(index).astype(np.float32).mean(axis=2)


def static_bands(greys: Sequence[Any], region: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """``(top, bottom, left, right)`` device pixels of *region* (``x0, y0, x1, y1``) that stand still on every one of
    *greys* (each keyframe of a scroll, grey): the rows from its top and its bottom, then the columns from its sides,
    that every keyframe paints the same - a fixed header, a footer, a sidebar. What scrolls moves on at least one."""
    import numpy as np

    x0, y0, x1, y1 = region
    if len(greys) < 2:
        return 0, 0, 0, 0
    first = greys[0][y0:y1, x0:x1]
    worst = np.zeros(first.shape, dtype=np.float32)
    for grey in greys[1:]:
        worst = np.maximum(worst, np.abs(grey[y0:y1, x0:x1] - first))

    def run(profile: Any) -> int:
        moving = np.flatnonzero(profile > STILL_LEVELS)
        return int(moving[0]) if moving.size else len(profile)

    rows = worst.mean(axis=1)
    top = run(rows)
    bottom = run(rows[::-1]) if top < len(rows) else 0
    columns = worst[top : len(rows) - bottom].mean(axis=0) if top + bottom < len(rows) else np.zeros(1)
    left = run(columns)
    right = run(columns[::-1]) if left < len(columns) else 0
    return top, bottom, left, right


def pinned_bands(
    moving: Sequence[Any], offsets: Sequence[float], rejected: list[tuple[int, int, str]] | None = None
) -> list[Any]:
    """The rows of each keyframe of a scroll (*moving*: the part of its scroller that moves, grey, device px) a sticky
    or fixed element holds while the content under it moves: a run of at least :data:`MIN_BAND` rows, drawn on
    (a line of text, an icon), that a keyframe and one beside it whose offset (*offsets*, device px) differs paint
    alike at the same screen rows, which the content's shift between them does not explain. :func:`static_bands` keeps
    only what every keyframe paints alike at the scroller's edges; a header pinned under a fixed bar, a table's header
    inside a scroll container, is held here, never translated with the rows under it. One boolean per row each.

    Two keyframes alike at some rows are not enough: a page whose paint lagged hands over content that has not
    moved yet, or the whole scroller unchanged. A run is held only where the content moved past it, it standing still,
    on :data:`MIN_BAND_PAIRS` pairs of keyframes at least, and never over :data:`MAX_BAND_SHARE` of the scroller's height;
    every run refused for either is added to *rejected* as ``(first row, past the last, why)``."""
    import numpy as np

    count = len(moving)
    height = moving[0].shape[0] if count else 0
    longest = MAX_BAND_SHARE * height
    #: What keyframe *j* holds against its neighbour *n*, by ``(j, n)``.
    seen: dict[tuple[int, int], Any] = {}
    for j, grey in enumerate(moving):
        rows = grey.shape[0]
        detail = np.abs(np.diff(grey[:, ::2], axis=1)).max(axis=1, initial=0.0) > DRAWN_LEVELS
        # Against the keyframes either side and the ones past them: a keyframe repainted between two (blank)
        # never hides a band both hold.
        for n in (j - 2, j - 1, j + 1, j + 2):
            if not 0 <= n < len(moving) or abs(offsets[n] - offsets[j]) < 1:
                continue
            mask = np.zeros(rows, dtype=bool)
            other = moving[n]
            shift = round(offsets[n] - offsets[j])
            alike = np.abs(grey[:, ::2] - other[:, ::2]).mean(axis=1) <= STILL_LEVELS
            edges = np.flatnonzero(np.diff(np.concatenate(([0], alike.astype(np.int8), [0]))))
            for a, b in zip(edges[::2], edges[1::2], strict=True):
                if b - a < MIN_BAND or not detail[a:b].any():
                    continue
                if b - a > longest:
                    if rejected is not None:
                        rejected.append(
                            (
                                int(a),
                                int(b),
                                f"it spans {100 * (b - a) / rows:.0f}% of its scroller: content its paint had not moved yet",
                            )
                        )
                    continue
                # The content moved *shift* rows up from this keyframe to the other: rows it explains are content alike.
                if a + shift >= 0 and b + shift <= rows:
                    at_shift = float(np.abs(other[a:b, ::2] - grey[a + shift : b + shift, ::2]).mean())
                    at_rest = float(np.abs(other[a:b, ::2] - grey[a:b, ::2]).mean())
                    if at_shift <= at_rest + STILL_LEVELS:
                        continue
                mask[a:b] = True
            seen[(j, n)] = mask
    # Each pair of keyframes the content moved between counts once for the rows either of the two holds.
    pairs = np.zeros(height, dtype=np.int16)
    for j, n in seen:
        if j < n:
            pairs += seen[(j, n)] | seen.get((n, j), False)
    confirmed = pairs >= MIN_BAND_PAIRS
    found = []
    for j in range(count):
        mask = np.zeros(height, dtype=bool)
        for key in ((j, j - 2), (j, j - 1), (j, j + 1), (j, j + 2)):
            if key in seen:
                mask |= seen[key]
        for a, b in _band_runs(mask & ~confirmed):
            if rejected is not None and b - a >= MIN_BAND:
                rejected.append((a, b, "it stood still on one pair of keyframes only: a paint that lagged"))
        mask &= confirmed
        for a, b in _band_runs(mask):
            if b - a < MIN_BAND:
                mask[a:b] = False
        found.append(mask)
    return found


def _candidate(
    fixed: Sequence[tuple[float, float, float, float, str]], ratio: float, top: int, bottom: int, left: int, right: int
) -> tuple[float, float, float, float, str] | None:
    """The page's fixed or sticky box (*fixed*, CSS px) a band of screen rows *top* to *bottom* (device px, across
    *left* to *right*) stands on: the one covering most of its rows, at least half of them, and
    :data:`BAND_WIDTH_SHARE` of its width - the band is copied across the whole moving width, so a narrow element (a
    floating button, a side rail) over the same rows never vouches for it; ``None``: none."""
    best: tuple[float, tuple[float, float, float, float, str]] | None = None
    for box in fixed:
        x, y, w, h, _position = box
        across = min((x + w) * ratio, right) - max(x * ratio, left)
        over = min((y + h) * ratio, bottom) - max(y * ratio, top)
        wide = across >= BAND_WIDTH_SHARE * (right - left)
        if wide and over >= 0.5 * (bottom - top) and (best is None or over > best[0]):
            best = (over, box)
    return best[1] if best is not None else None


def on_the_page(
    bands: Sequence[Any],
    fixed: Sequence[tuple[float, float, float, float, str]] | None,
    *,
    ratio: float,
    top: int,
    left: int,
    right: int,
    rejected: list[tuple[int, int, str]],
) -> None:
    """*bands* (:func:`pinned_bands`, rows of the moving part from screen row *top*) kept only where the page has a
    ``position: fixed`` or ``position: sticky`` element (*fixed*, ``capture``); every other run is cleared in
    place and added to *rejected*. Nothing is checked when the page's boxes were not read (``None``)."""
    if fixed is None:
        return
    for mask in bands:
        for a, b in _band_runs(mask):
            if _candidate(fixed, ratio, top + a, top + b, left, right) is None:
                mask[a:b] = False
                rejected.append((a, b, "no fixed or sticky element of the page spans it"))


def steady_bands(
    bands: Sequence[Any],
    fixed: Sequence[tuple[float, float, float, float, str]] | None,
    *,
    ratio: float,
    top: int,
    left: int,
    right: int,
) -> list[tuple[int, int]]:
    """The rows of the moving part (from screen row *top*, across *left* to *right*) that stand still by construction:
    those a ``position: fixed`` or ``position: sticky`` element of the page (*fixed*, ``capture``) covers at
    the same place as the scroll began and once it settled, across :data:`BAND_WIDTH_SHARE` of the moving width, which
    the pixels confirmed as a band (:func:`pinned_bands`) on half its rows at least on one keyframe. Each is set on
    every keyframe of *bands* in place, whatever the pixel pairs of that keyframe said: a top bar a keyframe's pairs
    missed (its run merged with rows that stood still on that pair only, a repaint) is held on every frame drawn and
    is never content translated with the scroll - a second copy of it. ``(first row, past the last)`` each."""
    import numpy as np

    if not fixed or not bands:
        return []
    rows = bands[0].shape[0]
    union = np.logical_or.reduce(bands)

    def place(box: tuple[float, float, float, float, str]) -> tuple[float, float, float, float]:
        return round(box[0], 1), round(box[1], 1), round(box[2], 1), round(box[3], 1)

    # The boxes read as the scroll began and once it settled come in one list: a box twice is where it was both times.
    seen = Counter(place(box) for box in fixed)
    found: list[tuple[int, int]] = []
    for box in fixed:
        x, y, w, h, _position = box
        if seen[place(box)] < 2:
            continue
        if min((x + w) * ratio, right) - max(x * ratio, left) < BAND_WIDTH_SHARE * (right - left):
            continue
        a, b = max(0, round(y * ratio) - top), min(rows, round((y + h) * ratio) - top)
        if b - a < MIN_BAND or b - a > MAX_BAND_SHARE * rows or int(union[a:b].sum()) < 0.5 * (b - a):
            continue
        if (a, b) not in found:
            found.append((a, b))
    for mask in bands:
        for a, b in found:
            mask[a:b] = True
    return found


def _blank_rows(
    frames: Keyframes,
    sources: Sequence[int],
    bands: Sequence[Any],
    runs: Sequence[tuple[int, int]],
    *,
    box: tuple[int, int, int],
) -> dict[int, Any]:
    """The band rows each keyframe of a scroll repainted blank: rows of a band (*runs*, of the moving part from
    screen row *top* across *left* to *right*, *box*) a keyframe paints one flat colour, :data:`BLANK_APART` grey levels
    or more from the colour the keyframes holding it paint there (their median) - a page's blank repaint, a
    region the screencast handed over transparent. Never drawn as the band, nor as content. One boolean per row each."""
    import numpy as np

    top, left, right = box
    count = len(sources)
    height = bands[0].shape[0] if count else 0
    blank = {j: np.zeros(height, dtype=bool) for j in range(count)}
    for a, b in runs:
        regions = [frames.rgb(sources[j])[top + a : top + b, left:right:2].astype(np.float32) for j in range(count)]
        means = np.stack([region.mean(axis=1) for region in regions])
        flat = np.stack([region.std(axis=1).max(axis=-1) < BLANK_LEVELS for region in regions])
        holders = [j for j in range(count) if bands[j][a:b].any()]
        if not holders:
            continue
        reference = np.median(means[holders], axis=0)
        apart = np.abs(means - reference[None]).max(axis=-1) >= BLANK_APART
        for j in range(count):
            blank[j][a:b] |= flat[j] & apart[j]
    return blank


def _band_runs(mask: Any) -> list[tuple[int, int]]:
    """The runs of rows *mask* holds, ``(first, past the last)``."""
    import numpy as np

    edges = np.flatnonzero(np.diff(np.concatenate(([0], mask.astype(np.int8), [0]))))
    return [(int(a), int(b)) for a, b in zip(edges[::2], edges[1::2], strict=True)]


def _band_before(grey: Any, band: Any, near: int, reach: int) -> int | None:
    """The row a band of rows (*band*, grey) stands at on the keyframe *grey* before it pinned, within *reach* rows of
    *near*; ``None`` where no row matches it within :data:`STILL_LEVELS` twice."""
    import numpy as np

    height = band.shape[0]
    best: tuple[float, int] | None = None
    for r in range(max(0, near - reach), min(grey.shape[0] - height, near + reach) + 1):
        cost = float(np.abs(grey[r : r + height, ::4] - band[:, ::4]).mean())
        if best is None or cost < best[0]:
            best = (cost, r)
    return best[1] if best is not None and best[0] <= 2 * STILL_LEVELS else None


def _pins(
    moving: Sequence[Any], offsets: Sequence[float], bands: Sequence[Any]
) -> dict[int, list[tuple[int, int, int, int]]]:
    """The bands that pin (or let go) between each keyframe *k* of a scroll and the next: ``{k: [(first row,
    past its last, the keyframe pinning it, the row it rode the content at on the other)]}`` - a band one of the two
    holds (:func:`pinned_bands`) that the other painted elsewhere, found within the content's shift between them."""
    found: dict[int, list[tuple[int, int, int, int]]] = {}
    for k in range(len(moving) - 1):
        shift = round(offsets[k + 1] - offsets[k])
        for pinner, rider, towards in ((k + 1, k, shift), (k, k + 1, -shift)):
            for a, b in _band_runs(bands[pinner] & ~bands[rider]):
                row = _band_before(moving[rider], moving[pinner][a:b], a + towards // 2, abs(towards) // 2 + COARSE)
                if row is not None and row != a:
                    found.setdefault(k, []).append((a, b, pinner, row))
    return found


def _held_bands(
    bands: Sequence[Any],
    pins: dict[int, list[tuple[int, int, int, int]]],
    k: int,
    offset: float,
    offsets: Sequence[float],
    excluded: dict[int, Any],
) -> list[tuple[int, int, int]]:
    """The bands a frame showing *offset* (device px) between keyframes *k* and *k + 1* holds as painted:
    ``(first row, past its last, the keyframe it is drawn from)`` - every band both keyframes pin, from *k*, and a band
    that pins between them once the content carried it past where it pins, from the keyframe pinning it; the rows the
    other painted it at, riding the content, then no longer count as content (*excluded*, updated in place).

    A band one of the two keyframes missed (confirmed on the keyframes about it, not on this one - a hover, a
    repaint, a pair the content barely moved between) and that never rode the content between them (no pin) is held
    too, from the nearest keyframe pinning it: translated with the content, it is a header pushed off for a frame or
    two, a blink."""
    import numpy as np

    held = [(a, b, k) for a, b in _band_runs(bands[k] & bands[k + 1])] if k + 1 < len(bands) else []
    for a, b, pinner, row in pins.get(k, []):
        rider = k if pinner == k + 1 else k + 1
        riding = row - (offset - offsets[rider])
        if (row - a) * (riding - a) <= 0:
            held.append((a, b, pinner))
            excluded[rider][row : row + (b - a)] = True
    if k + 1 < len(bands):
        moved = [(a, b) for a, b, _pinner, _row in pins.get(k, [])]
        for a, b in _band_runs(np.logical_or.reduce([bands[j] for j in range(max(0, k - 1), min(len(bands), k + 3))])):
            if any(ha < b and a < hb for ha, hb, _p in held) or any(ma < b and a < mb for ma, mb in moved):
                continue
            before = [j for j in (k, k - 1) if j >= 0 and bands[j][a:b].all()]
            after = [j for j in (k + 1, k + 2) if j < len(bands) and bands[j][a:b].all()]
            if before and after:
                held.append((a, b, before[0]))
    return held


def _blocks(grey: Any, size: int) -> Any:
    """*grey* averaged over *size* x *size* blocks: fine detail (a line of text) no longer aliases a coarse search."""
    rows, columns = grey.shape[0] // size * size, grey.shape[1] // size * size
    return grey[:rows, :columns].reshape(rows // size, size, columns // size, size).mean(axis=(1, 3))


def _shift_cost(first: Any, second: Any, d: int, columns: int = 2) -> float | None:
    """The grey levels *second* differs from *first* by on average, its rows *d* further down the content
    (``second[r]`` against ``first[r + d]``), every *columns*-th column; ``None`` when they share too few rows."""
    import numpy as np

    rows = first.shape[0]
    if abs(d) >= rows * (1 - MIN_OVERLAP):
        return None
    top_a, top_b = (d, 0) if d >= 0 else (0, -d)
    span = rows - abs(d)
    return float(np.abs(first[top_a : top_a + span, ::columns] - second[top_b : top_b + span, ::columns]).mean())


def register(a: Any, b: Any, expected: float, *, search: int) -> float | None:
    """How many rows the content moved up from grey *a* to grey *b* (the moving part of a scroller on two keyframes):
    ``b[r] == a[r + d]``, searched within *search* rows of *expected* - on :data:`COARSE`-pixel blocks, then to the row.
    ``None`` when the pixels do not tell (too little shared, or nothing to match on)."""
    small_a, small_b = _blocks(a, COARSE), _blocks(b, COARSE)
    centre, reach = round(expected / COARSE), max(1, round(search / COARSE))
    coarse = {
        d: c
        for d in range(centre - reach, centre + reach + 1)
        if (c := _shift_cost(small_a, small_b, d, 1)) is not None
    }
    if not coarse:
        return None
    values = sorted(coarse.values())
    if values[-1] - values[0] < 0.5:
        return None
    best = min(coarse, key=coarse.__getitem__) * COARSE
    fine = {d: c for d in range(best - COARSE, best + COARSE + 1) if (c := _shift_cost(a, b, d, 2)) is not None}
    return float(min(fine, key=fine.__getitem__)) if fine else float(best)


def clock_lag(scroll: Scroll, times: Sequence[float], offsets: Sequence[float]) -> float:
    """The seconds the scroll log runs behind the paint (within :data:`MAX_LAG`): how far the scroller moved from each
    keyframe painted at *times* to the next (their *offsets*, CSS px) is how far the log moved between their times plus
    it - the closest in sum (a pair misread counts once, for what it is off by), the smallest lag on a tie."""
    pairs = list(zip(times, offsets, strict=True))

    def cost(ms: int) -> tuple[float, int]:
        lag = ms / 1000
        moved = sum(
            abs((scroll.offset(t1 + lag) - scroll.offset(t0 + lag)) - (o1 - o0))
            for (t0, o0), (t1, o1) in itertools.pairwise(pairs)
        )
        return moved, abs(ms)

    bound = round(MAX_LAG * 1000)
    return min(range(-bound, bound + 1), key=cost) / 1000


# ── A canvas zoomed by the wheel ──────────────────────────────────


@dataclass(frozen=True)
class Zoom:
    """How a canvas's content moved from one keyframe to the next, in device px of its box: ``p -> scale * p + t``
    (a zoom about a fixed point, or a pan; a wheel never rotates it)."""

    scale: float
    tx: float
    ty: float

    def share(self, u: float) -> Zoom:
        """*u* of it, a negative *u* undoing it: scaled ``scale ** u`` about the same fixed point, or *u* of its pan."""
        ln = math.log(self.scale)
        gain = u if abs(ln) < 1e-9 else math.expm1(u * ln) / math.expm1(ln)
        return Zoom(math.exp(u * ln), self.tx * gain, self.ty * gain)

    def moved(self, size: tuple[int, int]) -> float:
        """The farthest a corner of a box of *size* (``w, h``, device px) moves under it."""
        width, height = size
        return max(
            math.hypot((self.scale - 1) * x + self.tx, (self.scale - 1) * y + self.ty)
            for x in (0, width - 1)
            for y in (0, height - 1)
        )

    def at(self, factor: int) -> Zoom:
        """It on a width block-averaged by *factor* (block ``i`` stands for device px ``i * factor + (factor - 1) / 2``)."""
        shift = (factor - 1) / 2 * (self.scale - 1)
        return Zoom(self.scale, (self.tx + shift) / factor, (self.ty + shift) / factor)

    def device(self, factor: int) -> Zoom:
        """It, read on a width block-averaged by *factor*, in device px (:meth:`at` undone)."""
        shift = (factor - 1) / 2 * (self.scale - 1)
        return Zoom(self.scale, self.tx * factor - shift, self.ty * factor - shift)


IDENTITY = Zoom(1.0, 0.0, 0.0)


def _zoomed(grey: Any, zoom: Zoom, shape: tuple[int, int]) -> tuple[Any, slice, slice] | None:
    """*grey* drawn under *zoom* on a picture of *shape*: ``out[p] = grey[(p - t) / scale]``, bilinear and separable,
    over the rows and columns of *shape* it covers (``None``: none)."""
    import numpy as np

    rows_q = (np.arange(shape[0], dtype=np.float32) - zoom.ty) / zoom.scale
    cols_q = (np.arange(shape[1], dtype=np.float32) - zoom.tx) / zoom.scale
    rows = np.flatnonzero((rows_q >= 0) & (rows_q <= grey.shape[0] - 1))
    cols = np.flatnonzero((cols_q >= 0) & (cols_q <= grey.shape[1] - 1))
    if not rows.size or not cols.size:
        return None
    y, x = rows_q[rows], cols_q[cols]
    y0 = np.minimum(y.astype(np.int64), grey.shape[0] - 2)
    x0 = np.minimum(x.astype(np.int64), grey.shape[1] - 2)
    wy, wx = (y - y0)[:, None], (x - x0)[None, :]
    band = grey[y0] * (1 - wy) + grey[y0 + 1] * wy
    out = band[:, x0] * (1 - wx) + band[:, x0 + 1] * wx
    return out, slice(int(rows[0]), int(rows[-1]) + 1), slice(int(cols[0]), int(cols[-1]) + 1)


def _zoom_error(a: Any, b: Any, weight: Any, zoom: Zoom) -> tuple[float, float]:
    """How far *b* is from *a* drawn under *zoom* and from *a* as it stood (mean grey levels), over what *zoom* covers -
    at least :data:`MIN_OVERLAP` of *b* -, each pixel counted by *weight* (what stands still over the canvas counts for
    nothing)."""
    import numpy as np

    drawn = _zoomed(a, zoom, b.shape)
    if drawn is None:
        return math.inf, math.inf
    out, rows, cols = drawn
    counted = weight[rows, cols]
    total = float(counted.sum())
    if total <= 0 or counted.size < MIN_OVERLAP * b.size:
        return math.inf, math.inf
    seen = b[rows, cols]
    return float((np.abs(seen - out) * counted).sum()) / total, float(
        (np.abs(seen - a[rows, cols]) * counted).sum()
    ) / total


def zoom_levels(grey: Any) -> list[tuple[int, Any]]:
    """A canvas's box on one keyframe (grey, device px) at each of :data:`ZOOM_WIDTHS`: ``(factor, picture)``, the
    picture block-averaged by the factor and softened a pixel, so a coarse search is not lost between thin lines."""
    from scipy.ndimage import gaussian_filter

    levels = []
    for width in ZOOM_WIDTHS:
        factor = max(1, round(grey.shape[1] / width))
        small = _blocks(grey, factor) if factor > 1 else grey
        levels.append((factor, gaussian_filter(small, 1.0)))
    return levels


def _inside(
    shape: tuple[int, int], box: tuple[float, float, float, float], factor: int, *, inset: int = 0
) -> tuple[slice, slice]:
    """The rows and columns of a width block-averaged by *factor* (of *shape*) a box (device px) covers, *inset* of
    them left out at each edge."""
    x, y, w, h = box
    rows = slice(
        min(shape[0], max(0, math.floor(y / factor) + inset)),
        min(shape[0], max(0, math.ceil((y + h) / factor) - inset)),
    )
    columns = slice(
        min(shape[1], max(0, math.floor(x / factor) + inset)),
        min(shape[1], max(0, math.ceil((x + w) / factor) - inset)),
    )
    return rows, columns


def standing(
    levels: Sequence[Sequence[tuple[int, Any]]], over: Sequence[tuple[float, float, float, float]] = ()
) -> tuple[list[Any], list[tuple[float, float, float, float]]]:
    """What stands still over a canvas on every one of its keyframes (each :func:`zoom_levels`), on each width, and
    the boxes of *over* (device px of its box: what the page draws over it) that stand still whole - a toolbar, a
    legend, judged inside their edge, which the canvas around them blurs into. Elsewhere, a pixel drawn - its
    neighbours unlike it, as text or an icon is - that every keyframe paints the same. The canvas's own plain ground is
    never counted: it is what a wrong zoom is told apart on, and what its content zooms across between two keyframes."""
    import numpy as np
    from scipy.ndimage import binary_dilation

    unchanged = []
    for n, (_factor, first) in enumerate(levels[0]):
        same = np.ones(first.shape, dtype=bool)
        for previous, current in itertools.pairwise(level[n][1] for level in levels):
            same &= np.abs(current - previous) <= STILL_LEVELS
        unchanged.append(same)
    factor, finest = levels[0][-1]
    kept = []
    for box in over:
        inner = unchanged[-1][_inside(finest.shape, box, factor, inset=2)]
        if not inner.size:
            # A box too small to have an inside past its edge (an icon, a count in a legend row) is judged whole.
            inner = unchanged[-1][_inside(finest.shape, box, factor)]
        if inner.size and float(inner.mean()) >= PAINTED_ENOUGH:
            kept.append(box)
    # A box inside one that stands still whole (a row, an icon, a count of a legend) stands with it: a hover lighting
    # one row of a legend changes that row in place, it never makes the row a label the zoom carries.
    kept += [box for box in over if box not in kept and any(_within(box, outer) for outer in kept)]
    found = []
    for (factor, first), same in zip(levels[0], unchanged, strict=True):
        dy, dx = np.gradient(first)
        mask = binary_dilation(same & (np.hypot(dy, dx) > DRAWN_LEVELS), iterations=1)
        for box in kept:
            mask[_inside(first.shape, box, factor)] = True
        found.append(mask)
    return found, kept


#: Device px a box drawn over a canvas may pass the edge of another and still lie inside it (:func:`_within`).
INSIDE_PX = 1.0


def _within(box: tuple[float, float, float, float], outer: tuple[float, float, float, float]) -> bool:
    """Whether *box* (``(x, y, w, h)``) lies inside *outer* whole, to :data:`INSIDE_PX`, and is not *outer* itself."""
    x, y, w, h = box
    ox, oy, ow, oh = outer
    inside = (
        x >= ox - INSIDE_PX and y >= oy - INSIDE_PX and x + w <= ox + ow + INSIDE_PX and y + h <= oy + oh + INSIDE_PX
    )
    return inside and w * h < ow * oh


def riding_boxes(
    levels: Sequence[Sequence[tuple[int, Any]]],
    over: Sequence[tuple[float, float, float, float]],
    zooms: Sequence[Zoom | None],
) -> set[int]:
    """The boxes of *over* (device px of a canvas's box: what the page draws over it) the canvas's zoom carries:
    on a pair of keyframes (each :func:`zoom_levels`, *zooms* between them) whose canvas moved, the box on the second is
    drawn from the first under the zoom within :data:`RIDING_SHARE` of the grey levels it stands apart from it unmoved -
    a label a graph lays over a node, which moves and scales with it. Holding one as painted draws its inside unwarped
    in a warped canvas: the labels crossing its edge split. A box that stands still or changes in place (a toolbar, a
    hover lighting a button) is not one: the zoom explains it no better than standing still.

    A box no single pair tells (a label the page moves a frame late, a short pair) rides too when, over every pair
    whose canvas moved, it changed by more than :data:`STILL_LEVELS` and the zoom draws it closer than standing still
    does (:data:`RIDING_OVERALL`): what changes there is the canvas moving under it, not the box changing in place."""
    import numpy as np

    found: set[int] = set()
    for index, box in enumerate(over):
        zoomed_sum = unmoved_sum = 0.0
        pairs = 0
        for k, zoom in enumerate(zooms):
            if zoom is None or zoom == IDENTITY:
                continue
            factor, first = levels[k][-1]
            second = levels[k + 1][-1][1]
            weight = np.zeros(second.shape, dtype=np.float32)
            weight[_inside(second.shape, box, factor)] = 1.0
            if not weight.any():
                continue
            zoomed, unmoved = _zoom_error(first, second, weight, zoom.at(factor))
            if not math.isfinite(zoomed):
                continue
            if unmoved > STILL_LEVELS and zoomed <= RIDING_SHARE * unmoved:
                found.add(index)
                break
            zoomed_sum, unmoved_sum, pairs = zoomed_sum + zoomed, unmoved_sum + unmoved, pairs + 1
        else:
            if pairs and unmoved_sum > STILL_LEVELS * pairs and zoomed_sum < RIDING_OVERALL * unmoved_sum:
                found.add(index)
    return found


#: CSS px two boxes (a canvas's, one drawn over it) may differ by on two steps of a take and still be the same.
SAME_BOX_PX = 2.0


def _same_box(a: Sequence[float] | None, b: Sequence[float] | None) -> bool:
    """Whether *a* and *b* (``[x, y, w, h]``, CSS px) are the same box to :data:`SAME_BOX_PX`."""
    return (
        a is not None
        and b is not None
        and len(a) == len(b) == 4
        and all(abs(float(p) - float(q)) <= SAME_BOX_PX for p, q in zip(a, b, strict=True))
    )


def carried_boxes(paced: Sequence[Sequence[Any]]) -> dict[int, frozenset[int]]:
    """The canvas zooms of a take to pace again with boxes carried, ``entry -> indices of its scroll.over``.

    *paced*: ``[scroll, kept, frames drawn, keyframes replaced, serial, seen]`` of each scroll (:func:`pace`), *seen*
    as :func:`_pace_zoom` filled it. On a canvas where one zoom of the take found boxes riding it (:func:`riding_boxes`),
    a box another zoom of the same canvas held - neither standing still on it, nor drawn at the same place on a zoom
    of that canvas that held it there too (a toolbar, a legend: where it stood on every step) - is a label riding the
    canvas too: its zoom was too short, or its pixels too alike, to tell. Held, it splits against the warped canvas on
    one wheel step while the others draw it with the canvas ("N box(es) over its canvas held as painted")."""
    zooms = [
        (n, entry[0], entry[5])
        for n, entry in enumerate(paced)
        if entry[0].scroller == "canvas" and entry[2] and entry[5].get("over") and entry[1].get("paced")
    ]
    found: dict[int, frozenset[int]] = {}
    for n, scroll, seen in zooms:
        same = [(m, other) for m, other_scroll, other in zooms if m != n and _same_box(other_scroll.box, scroll.box)]
        if not any(other.get("riding") for _m, other in same):
            continue
        over = seen["over"]
        held = [
            i for i in range(len(over)) if i not in seen.get("riding", set()) and i not in seen.get("standing", set())
        ]
        carry = set()
        for i in held:
            # Held at the same place by another zoom of the canvas, it stands there on every step: never carried.
            recurs = any(
                _same_box(over[i], box) and j not in other.get("riding", set())
                for _m, other in same
                for j, box in enumerate(other["over"])
            )
            if not recurs:
                carry.add(i)
        if carry:
            found[n] = frozenset(carry)
    return found


def _refined(a: Any, b: Any, weight: Any, zoom: Zoom, factor: int, scale_step: float) -> Zoom:
    """*zoom* refined on one width: its scale (about the box's centre) and its pan each moved a step while that draws
    *b* closer from *a*, the steps halved until under a tenth of a pixel."""
    centre = ((b.shape[1] - 1) / 2, (b.shape[0] - 1) / 2)
    current = zoom.at(factor)
    best = _zoom_error(a, b, weight, current)[0]
    steps = [scale_step, 1.0, 1.0]
    for _ in range(60):
        improved = False
        for axis in range(3):
            for sign in (1.0, -1.0):
                delta = sign * steps[axis]
                if axis == 0:
                    scale = current.scale * math.exp(delta)
                    moved = (current.scale - scale) * centre[0], (current.scale - scale) * centre[1]
                    candidate = Zoom(scale, current.tx + moved[0], current.ty + moved[1])
                elif axis == 1:
                    candidate = Zoom(current.scale, current.tx + delta, current.ty)
                else:
                    candidate = Zoom(current.scale, current.tx, current.ty + delta)
                error = _zoom_error(a, b, weight, candidate)[0]
                if error < best - 1e-9:
                    current, best, improved = candidate, error, True
                    break
        if not improved:
            steps = [step / 2 for step in steps]
            if steps[1] < 0.1:
                break
    return current.device(factor)


def _left_parts(a: Any, b: Any, weight: Any, zoom: Zoom) -> int:
    """In how many of the :data:`LEFT_PARTS` x :data:`LEFT_PARTS` parts of *b* what *a* drawn under *zoom* leaves of it
    sits, the parts holding most counted until they hold :data:`LEFT_SHARE` of it: 0 when nothing is left."""
    import numpy as np

    drawn = _zoomed(a, zoom, b.shape)
    if drawn is None:
        return 0
    out, rows, cols = drawn
    left = np.abs(b[rows, cols] - out) * weight[rows, cols]
    total = float(left.sum())
    if total <= 0:
        return 0
    h, w = left.shape
    parts = sorted(
        (
            float(
                left[
                    h * i // LEFT_PARTS : h * (i + 1) // LEFT_PARTS, w * j // LEFT_PARTS : w * (j + 1) // LEFT_PARTS
                ].sum()
            )
            for i in range(LEFT_PARTS)
            for j in range(LEFT_PARTS)
        ),
        reverse=True,
    )
    held = 0.0
    for count, part in enumerate(parts, start=1):
        held += part
        if held >= LEFT_SHARE * total:
            return count
    return len(parts)


def _explained(zoomed: float, unmoved: float) -> float:
    """The share of what tells two keyframes apart (*unmoved*) a zoom explains, leaving *zoomed*: 0 when it tells none."""
    if not math.isfinite(zoomed) or not math.isfinite(unmoved) or unmoved <= 0:
        return 0.0
    return max(0.0, 1 - zoomed / unmoved)


def appeared_still(
    still: Sequence[Any],
    levels: Sequence[Sequence[tuple[int, Any]]],
    boxes: Sequence[tuple[float, float, float, float]],
) -> list[Any]:
    """*still* (:func:`standing`, one mask per width) with each of *boxes* (device px of the canvas's box: what the
    page opened over it during its zoom) marked on every width: left out of what tells two keyframes apart."""
    marked = [mask.copy() for mask in still]
    for mask, (factor, _picture) in zip(marked, levels[0], strict=True):
        for box in boxes:
            mask[_inside(mask.shape, box, factor)] = True
    return marked


def register_zoom(
    a: Sequence[tuple[int, Any]],
    b: Sequence[tuple[int, Any]],
    anchors: Sequence[tuple[float, float]],
    still: Sequence[Any],
    explained: list[float] | None = None,
    hover: tuple[float, float] | None = None,
    hovered: list[int] | None = None,
    left: list[int] | None = None,
) -> Zoom | None:
    """How a canvas moved from one keyframe to the next (*a*, *b*: :func:`zoom_levels` of its box): the zoom about one
    of *anchors* (device px of the box: where the wheel turned, its centre) that best draws *b* from *a* on the coarsest
    width, refined on each finer one; *still* (:func:`standing`) is left out. :data:`IDENTITY` when the canvas did not
    move; ``None`` when its pixels do not tell (no zoom explains :data:`ZOOM_EXPLAINS` of what tells them apart), the
    share the best zoom found explains then appended to *explained* when given.

    *hover* (device px of the box: where the cursor stood): a pair no zoom explains whole is judged again on the
    coarsest width without :data:`HOVER_PX` about it, where the page lights what the pointer is over; registered so,
    a 1 is appended to *hovered* when given. The bar is unchanged. A pair held appends to *left*, when given, in how
    many parts of the box what its best zoom leaves sits (:func:`_left_parts`, on the coarsest width)."""
    import numpy as np

    weights = [(~mask).astype(np.float32) for mask in still]
    factor, small_a = a[0]
    small_b = b[0][1]
    if float((np.abs(small_b - small_a) * weights[0]).max(initial=0.0)) <= ZOOM_SAME_LEVELS:
        return IDENTITY
    fine_factor, fine_a = a[-1]
    size = (fine_a.shape[1] * fine_factor, fine_a.shape[0] * fine_factor)

    coarse_found: list[Zoom] = []

    def searched(about: Sequence[tuple[float, float]]) -> tuple[Zoom, float, float]:
        # The best zoom about one of *about* on the coarsest width, refined on each finer one, and its fine errors;
        # the coarse best, before any refining, kept in *coarse_found*.
        reach = round(ZOOM_REACH / ZOOM_STEP)
        tried: dict[Zoom, float] = {}
        for n in range(-reach, reach + 1):
            scale = math.exp(n * ZOOM_STEP)
            for x, y in about:
                zoom = Zoom(scale, (1 - scale) * x, (1 - scale) * y)
                tried[zoom] = _zoom_error(small_a, small_b, weights[0], zoom.at(factor))[0]
        best = min(tried, key=tried.__getitem__)
        coarse_found.append(best)
        scale_step = ZOOM_STEP / 2
        for (level_factor, level_a), (_same, level_b), weight in zip(a[1:], b[1:], weights[1:], strict=True):
            best = _refined(level_a, level_b, weight, best, level_factor, scale_step)
            scale_step /= 2
        zoomed, standing_still = _zoom_error(fine_a, b[-1][1], weights[-1], best.at(fine_factor))
        return best, zoomed, standing_still

    def coarse_share(zoom: Zoom) -> float:
        # The share *zoom* explains on the coarsest width: a graph draws its lines and labels at the screen's size
        # whatever its zoom, which a fine width counts against a zoom its nodes and links follow, and a coarse one blurs.
        return _explained(*_zoom_error(small_a, small_b, weights[0], zoom.at(factor)))

    def explains(zoomed: float, standing_still: float) -> bool:
        return math.isfinite(zoomed) and zoomed <= (1 - ZOOM_EXPLAINS) * standing_still

    best, zoomed, standing_still = searched(anchors)
    if best.moved(size) < ZOOM_MOVED_PX:
        return IDENTITY
    if not explains(zoomed, standing_still):
        # The cursor, the box's centre or the fixed point given may sit in another basin than the zoom the canvas
        # made (a graph zooms about its own point): searched again from a grid of fixed points over the box, the best
        # of every search kept.
        grid = [
            ((size[0] - 1) * (i + 0.5) / ZOOM_GRID, (size[1] - 1) * (j + 0.5) / ZOOM_GRID)
            for i in range(ZOOM_GRID)
            for j in range(ZOOM_GRID)
        ]
        again, again_zoomed, again_still = searched(grid)
        if math.isfinite(again_zoomed) and (not math.isfinite(zoomed) or again_zoomed < zoomed):
            best, zoomed, standing_still = again, again_zoomed, again_still
            if best.moved(size) < ZOOM_MOVED_PX:
                return IDENTITY
    if not explains(zoomed, standing_still):
        # The fine widths refine a zoom away from what its nodes and links made when the graph redraws its lines and
        # labels at the screen's size: the best zoom of the coarsest width, which blurs them, is judged there. The
        # coarse search only scales about fixed points a step apart; each of its bests is refined there too (scale and
        # pan, free of the fixed point), so a zoom the step or the fixed point missed by a little is not held.
        refined = [_refined(small_a, small_b, weights[0], zoom, factor, ZOOM_STEP / 2) for zoom in coarse_found]
        coarse = max([best, *coarse_found, *refined], key=coarse_share)
        share = coarse_share(coarse)
        if share >= ZOOM_EXPLAINS and coarse.moved(size) >= ZOOM_MOVED_PX:
            return coarse
        if hover is not None:
            # What the pointer lights changes about it as the zoom brings another link under it: judged without it.
            rows, cols = np.ogrid[: small_a.shape[0], : small_a.shape[1]]
            centre = ((hover[0] - (factor - 1) / 2) / factor, (hover[1] - (factor - 1) / 2) / factor)
            unlit = weights[0] * (np.hypot(cols - centre[0], rows - centre[1]) > HOVER_PX / factor)
            candidates = [best, *coarse_found, *refined]
            candidates += [_refined(small_a, small_b, unlit, zoom, factor, ZOOM_STEP / 2) for zoom in candidates]

            def unlit_share(zoom: Zoom) -> float:
                return _explained(*_zoom_error(small_a, small_b, unlit, zoom.at(factor)))

            lit = max(candidates, key=unlit_share)
            lit_share = unlit_share(lit)
            if lit_share >= ZOOM_EXPLAINS and lit.moved(size) >= ZOOM_MOVED_PX:
                if hovered is not None:
                    hovered.append(1)
                return lit
            share = max(share, lit_share)
        if explained is not None:
            explained.append(max(_explained(zoomed, standing_still), share))
        if left is not None:
            left.append(_left_parts(small_a, small_b, weights[0], coarse))
        return None
    return best


def implied_zooms(
    zooms: Sequence[Zoom | None],
    scroll: Scroll,
    times: Sequence[float],
    levels: Sequence[Sequence[tuple[int, Any]]],
    still: Sequence[Any],
    why: dict[int, str] | None = None,
    explained: dict[int, float] | None = None,
    hover: tuple[float, float] | None = None,
    hovered: list[int] | None = None,
    left_parts: dict[int, int] | None = None,
) -> tuple[list[Zoom | None], int]:
    """*zooms* (one per pair of the keyframes painted at *times*, :func:`register_zoom`) with each pair its pixels did
    not register (``None``) given the zoom the canvas's log implies, and how many were. The log's progress between the
    two keyframes is the ln of the scale (``transform``) or, for the wheel's delta, scaled by what the registered pairs
    zoomed per unit of it; the zoom is about the fixed point the registered pairs share. A pair keeps ``None`` - its
    nearer keyframe held - unless the zoom implied explains :data:`ZOOM_EXPLAINS` of what tells its two keyframes apart,
    the bar :func:`register_zoom` sets: two pictures no zoom relates are never drawn one from the other.

    *why*, when given, is filled with each pair left unregistered and the reason (:data:`HELD_WHY`): what the take's
    words say of the frames it holds, so a probe names the lever. *explained*, when given, is filled with the share of
    what tells each such pair's keyframes apart the best zoom tried explains (the log's, the fixed point's): near
    :data:`ZOOM_EXPLAINS`, a zoom the bar misses; near 0, two pictures no zoom relates (the graph moved its nodes).
    *left_parts*, when given, is filled with the parts of the box what the best zoom leaves of each such pair sits in."""
    import numpy as np

    filled = list(zooms)
    unregistered = [k for k, zoom in enumerate(zooms) if zoom is None]

    def left(reason: str) -> tuple[list[Zoom | None], int]:
        if why is not None:
            why.update({k: reason for k in unregistered if filled[k] is None and k not in why})
        return filled, sum(1 for k in unregistered if filled[k] is not None)

    known = [(k, zoom) for k, zoom in enumerate(zooms) if zoom is not None and abs(zoom.scale - 1) > 1e-4]
    if not unregistered:
        return filled, 0
    if not known or len(times) != len(zooms) + 1:
        return left("no_scale")
    progress = [scroll.offset(t) for t in times]
    if scroll.progress == "transform":
        per = 1.0
    else:
        moved = sum(progress[k + 1] - progress[k] for k, _zoom in known)
        if abs(moved) < 1e-9:
            return left("no_scale")
        per = sum(math.log(zoom.scale) for _k, zoom in known) / moved
    fx = float(np.median([zoom.tx / (1 - zoom.scale) for _k, zoom in known]))
    fy = float(np.median([zoom.ty / (1 - zoom.scale) for _k, zoom in known]))
    weight = (~still[-1]).astype(np.float32)
    count = 0
    for k, zoom in enumerate(zooms):
        if zoom is not None:
            continue
        ln = per * (progress[k + 1] - progress[k])
        reason = ""
        shares: list[float] = []
        if not math.isfinite(ln) or abs(ln) < 1e-4:
            reason = "log_still"
        elif abs(ln) > ZOOM_REACH:
            reason = "past_reach"
        else:
            scale = math.exp(ln)
            candidate = Zoom(scale, (1 - scale) * fx, (1 - scale) * fy)
            factor, first = levels[k][-1]
            zoomed, unmoved = _zoom_error(first, levels[k + 1][-1][1], weight, candidate.at(factor))
            # Judged on the coarsest width too, as register_zoom judges a pair (a graph's lines and labels keep their size).
            coarse_factor, coarse_first = levels[k][0]
            coarse_weight = (~still[0]).astype(np.float32)
            coarse = _explained(
                *_zoom_error(coarse_first, levels[k + 1][0][1], coarse_weight, candidate.at(coarse_factor))
            )
            if math.isfinite(zoomed) and (zoomed <= (1 - ZOOM_EXPLAINS) * unmoved or coarse >= ZOOM_EXPLAINS):
                filled[k] = candidate
                count += 1
                continue
            reason = "unexplained"
            shares.append(max(_explained(zoomed, unmoved), coarse))
        # The log does not tell it: searched again about the fixed point the registered pairs share, which the
        # cursor or the box's centre (where :func:`register_zoom` searched) may not be - a graph zooms about its own.
        parts: list[int] = []
        again = register_zoom(levels[k], levels[k + 1], [(fx, fy)], still, shares, hover, hovered, parts)
        if again is not None:
            filled[k] = again
            count += 1
            continue
        if why is not None:
            why[k] = reason
        if explained is not None:
            explained[k] = max(shares, default=0.0)
        if left_parts is not None and parts:
            left_parts[k] = parts[-1]
    return filled, count


# ── Pacing ────────────────────────────────────────────────────────────────


@dataclass
class Paced:
    """What :func:`pace` made of a take: its frames (its keyframes, a paced scroll's or transition's frames in place of
    those they were cut from), each scroll as the take keeps it, and what its notes say of a scroll that outran the
    page's paints."""

    frames: list[tuple[float, str]]
    scrolls: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: The steps reused from a stopped worker's checkpoint (``pacing_checkpoint``), and the step it resumed at.
    reused: list[str] = field(default_factory=list)
    resumed_at: int | None = None

    @property
    def paced(self) -> int:
        return sum(1 for scroll in self.scrolls if scroll.get("paced"))


class FrameWriter:
    """Drawn frames written into *out_dir* as JPEG at :data:`QUALITY`, at most :data:`_SAVING` waiting at once."""

    def __init__(self, out_dir: Path, saver: ThreadPoolExecutor) -> None:
        self.out_dir, self.saver = out_dir, saver
        self._waiting: list[Any] = []

    def write(self, picture: Any, name: str) -> None:
        import numpy as np
        from PIL import Image

        self._waiting.append(
            self.saver.submit(Image.fromarray(np.ascontiguousarray(picture)).save, self.out_dir / name, quality=QUALITY)
        )
        while len(self._waiting) > _SAVING:
            self._waiting.pop(0).result()

    def flush(self) -> None:
        for future in self._waiting:
            future.result()
        self._waiting = []


def span_frames(times: list[float], span: tuple[float, float]) -> tuple[int | None, list[int], int | None]:
    """``(the keyframe at or before the span, those inside it, the first at or after its end)``."""
    start, end = span
    before = bisect.bisect_right(times, start) - 1
    inside = [i for i in range(max(before + 1, 0), len(times)) if start < times[i] < end]
    after = bisect.bisect_left(times, end)
    return (before if before >= 0 else None), inside, (after if after < len(times) else None)


def zoom_sources(times: list[float], span: tuple[float, float], reach: float | None = None) -> tuple[list[int], int]:
    """The keyframes a canvas zoom over *span* is drawn from: those painted inside it, the one before it and the one
    after it only when painted within *reach* (:data:`ZOOM_KEYFRAME_REACH`) of its edges; and how many were left out.

    A zoom in a stretch the screencast barely painted (a wheel, a marker, then a long pause: 0 to 1 frame a second for
    7 s) found its keyframe after 13 s later and its keyframe before 8 s earlier, and drew frames of those moments into
    the stretch - a cursor on a node clicked later, a tooltip of a button clicked before, the toolbar moved
    (``static_flicker``, #9744 retest). A keyframe outside the reach is no picture of the stretch: the zoom is not
    paced from it, and the take holds the last frame painted, as the encoder does between two keyframes."""
    reach = ZOOM_KEYFRAME_REACH if reach is None else reach
    start, end = span
    before, inside, after = span_frames(times, span)
    left_out = 0
    sources = list(inside)
    if before is not None:
        if start - times[before] <= reach:
            sources.insert(0, before)
        else:
            left_out += 1
    if after is not None:
        if times[after] - end <= reach:
            sources.append(after)
        else:
            left_out += 1
    return sources, left_out


def _checked(
    offsets: list[float], moving: list[Any], scroll: Scroll, times: list[float], ratio: float
) -> tuple[list[float], int]:
    """The offsets (device px) a window's keyframes said they painted, each checked against its pixels, and
    how many were read off them instead. A page that paints slowly hands over a frame already saying the scroll's next
    offset over the paint of the one before: a keyframe showing the keyframe before as it was takes its offset, and
    one whose pixels match another shift from it - within :data:`STILL_LEVELS` twice, and better than the shift it
    says by as much - takes that shift, searched from not moving to as far as it says it moved, or the log does. Two
    keyframes sharing no rows say nothing of each other: the offset said stands."""
    out = [offsets[0]]
    read = 0
    for k in range(1, len(offsets)):
        said = offsets[k] - out[-1]
        at_said = _shift_cost(moving[k - 1], moving[k], round(said))
        if at_said is not None and at_said <= STILL_LEVELS:
            out.append(offsets[k])
            continue
        still = _shift_cost(moving[k - 1], moving[k], 0)
        if still is not None and still <= STILL_LEVELS:
            out.append(out[-1])
            read += 1
            continue
        found = None
        if at_said is not None:
            logged = abs(scroll.offset(times[k]) - scroll.offset(times[k - 1])) * ratio
            found = register(
                moving[k - 1], moving[k], said / 2, search=round(max(abs(said) / 2, logged) + SEARCH * ratio)
            )
        better = _shift_cost(moving[k - 1], moving[k], round(found)) if found is not None else None
        if (
            better is not None
            and at_said is not None
            and better <= 2 * STILL_LEVELS
            and better + STILL_LEVELS < at_said
        ):
            out.append(out[-1] + found)
            read += 1
        else:
            out.append(offsets[k])
    return out, read


def _anchors(offsets: list[float]) -> list[int]:
    """The keyframes a scroll is drawn between: the first of each run painting one offset - the ones after it
    are the page's paint lagging the scroll, which never stood there - but the last of the run it rests in before it
    moves."""
    runs: list[list[int]] = []
    for k, offset in enumerate(offsets):
        if runs and abs(offset - offsets[runs[-1][0]]) < 0.5:
            runs[-1].append(k)
        else:
            runs.append([k])
    return [run[-1] if i == 0 else run[0] for i, run in enumerate(runs)]


def _wheel_late(scroll: Scroll, times: list[float], offsets: list[float]) -> float:
    """How much later than the wheel the page painted its scroll (seconds): the median, over the keyframes
    painted while it moved (*times*, *offsets*), of how much later each painted its offset than the wheel had turned
    that far - never before the wheel turned, nor past the end of the scroll's log."""
    import statistics

    import numpy as np

    wheel = scroll.wheel or ()
    first, last = offsets[0], offsets[-1]
    if last == first or len(wheel) < 2 or wheel[-1][1] <= 0:
        return 0.0
    turned_at = [t for t, _share in wheel]
    shares = [share / wheel[-1][1] for _t, share in wheel]
    late = [
        t - float(np.interp((offset - first) / (last - first), shares, turned_at))
        for t, offset in zip(times, offsets, strict=True)
        if min(first, last) < offset < max(first, last)
    ]
    return min(max(statistics.median(late), 0.0), max(scroll.span[1] - turned_at[-1], 0.0)) if late else 0.0


def _pace_scroll(
    scroll: Scroll,
    frames: Keyframes,
    times: list[float],
    *,
    fps: int,
    ratio: float,
    out_dir: Path,
    serial: int,
    saver: ThreadPoolExecutor,
    painted_offsets: dict[str, float] | None = None,
    seen: dict | None = None,
) -> tuple[dict, list[tuple[float, str]], set[int]]:
    """One scroll paced (:func:`pace`): what the take keeps of it, the frames drawn for it and the keyframes they
    replace. *painted_offsets*: the window's offset each keyframe painted, by file name (CSS px). *seen*: filled by a
    canvas zoom with what it read of the boxes over its canvas (:func:`_pace_zoom`)."""
    import numpy as np
    from PIL import Image

    if scroll.scroller == "canvas":
        return _pace_zoom(
            scroll, frames, times, fps=fps, ratio=ratio, out_dir=out_dir, serial=serial, saver=saver, seen=seen
        )
    kept = _compact(scroll)
    start, end = scroll.span
    length = max(end - start, 1e-6)
    before, inside, after = span_frames(times, scroll.span)
    painted = len(inside) / length
    kept.update({"painted": round(painted, 2), "fps": round(painted, 2), "paced": False})
    if scroll.motion < MIN_MOTION:
        kept["why"] = NOTHING_MOVED if scroll.probed else DID_NOT_MOVE
        return kept, [], set()
    if scroll.unpaceable:
        kept["why"] = scroll.unpaceable
        return kept, [], set()
    sources = [i for i in ([before] if before is not None else []) + inside + ([after] if after is not None else [])]
    if painted >= PAINTED_ENOUGH * fps:
        # Painted at its rate: kept as painted unless its frames move unevenly - a frame repeated, then one twice as far
        # -, which the browser paints when its scroll and its paints beat against each other. Those are drawn again
        # too, from keyframes spread over it. So is a window's scroll the wheel turned on the take's frame clock
        # (``capture``) whatever its keyframes' cadence: what the host painted carries its load - a paint
        # dropped, a turn sent late then caught up -, the wheel's clock does not, and drawn on it the scroll moves the
        # same distance every frame.
        steady = _painted_cadence(scroll, frames, times, painted_offsets, fps=fps, rate=painted)
        clocked = scroll.wheel is not None and scroll.wheel[-1][1] > 0
        uneven = steady is not None and (clocked or not even(steady))
        spread = _spread(scroll, frames, sources, painted_offsets, ratio=ratio) if uneven else None
        if spread is None:
            if steady is not None:
                kept["cadence"] = steady
            kept["why"] = PAINTED_AT_RATE
            return kept, [], set()
        kept["painted_cadence"] = steady
        sources = spread
    if len(sources) < 2:
        kept["why"] = "fewer than two keyframes painted it"
        return kept, [], set()
    height, width = frames.rgb(sources[0]).shape[:2]
    if scroll.box is not None:
        bx, by, bw, bh = (v * ratio for v in scroll.box)
        region = (max(0, round(bx)), max(0, round(by)), min(width, round(bx + bw)), min(height, round(by + bh)))
    else:
        region = (0, 0, width, height)
    x0, y0, x1, y1 = region
    if x1 - x0 < 16 or y1 - y0 < 16:
        kept["why"] = "its scroll container is too small"
        return kept, [], set()
    greys = [frames.grey(i) for i in sources]
    top, bottom, left, right = static_bands(greys, region)
    moving_top, moving_bottom = y0 + top, y1 - bottom
    moving_left, moving_right = x0 + left, x1 - right
    rows = moving_bottom - moving_top
    if rows < MIN_MOVING * (y1 - y0) or moving_right - moving_left < MIN_MOVING * (x1 - x0):
        kept["why"] = "nothing of it moved on its keyframes"
        return kept, [], set()
    source_times = [times[i] for i in sources]
    moving = [grey[moving_top:moving_bottom, moving_left:moving_right] for grey in greys]

    def registered(lag: float, *, widened: bool) -> list[float]:
        """Each keyframe's offset (device px): the first where the log has it (at rest, before the scroll), each next
        one read off its pixels against the one before around where the log says, *lag* seconds on - the log's where
        the pixels do not tell. *widened*: searched as far as the log's lag could carry it at the scroll's speed."""
        found_offsets = [scroll.offset(source_times[0] + lag) * ratio]
        for k in range(1, len(sources)):
            t = source_times[k]
            logged = scroll.offset(t + lag) * ratio
            search = SEARCH
            if widened:
                search += abs(scroll.offset(t + 0.05) - scroll.offset(t - 0.05)) / 0.1 * MAX_LAG
            found = register(moving[k - 1], moving[k], logged - found_offsets[-1], search=round(search * ratio))
            found_offsets.append(found_offsets[-1] + found if found is not None else logged)
        return found_offsets

    # The log is read on the page's main thread, the frames stamped as they are painted: the lag between the two clocks
    # is read off the keyframes' offsets, and what is left of each keyframe's offset is met on its instant. A window's
    # keyframes say the offset they painted (the screencast's ``scrollOffsetY``), checked against their pixels
    # (a page that paints slowly hands over a frame saying the next offset over the paint before). Else - a
    # scroll container, or a frame that did not say - each is registered widely, the lag fitted on them, then
    # registered again around the log on the paint's clock.
    window = scroll.scroller == "window" and painted_offsets
    shown = [painted_offsets.get(frames.frames[i][1]) for i in sources] if window else []
    if shown and all(value is not None for value in shown):
        offsets, read = _checked([float(value) * ratio for value in shown], moving, scroll, source_times, ratio)
        lag = clock_lag(scroll, source_times, [offset / ratio for offset in offsets])
        kept["offsets"] = "painted"
        if read:
            kept["read"] = read
    else:
        lag = clock_lag(scroll, source_times, [offset / ratio for offset in registered(0.0, widened=True)])
        offsets = registered(lag, widened=False)
        kept["offsets"] = "registered"
    # A keyframe painting the offset of the one before is the paint lagging, never a stop.
    anchors = _anchors(offsets)
    anchor_times = [source_times[j] for j in anchors]
    anchor_offsets = [offsets[j] for j in anchors]
    residuals = [
        offset - scroll.offset(t + lag) * ratio for offset, t in zip(anchor_offsets, anchor_times, strict=True)
    ]
    wheel = scroll.wheel if scroll.wheel is not None and scroll.wheel[-1][1] > 0 else None
    wheel_t = [t for t, _share in wheel or ()]
    wheel_share = [share / wheel[-1][1] for _t, share in wheel] if wheel else []
    late = _wheel_late(scroll, anchor_times, anchor_offsets) if wheel is not None else 0.0
    if wheel is not None:
        kept["late"] = round(late, 3)

    def at(t: float) -> float:
        """The offset the frame at *t* shows (device px). Turned by the wheel on a clock: from where the page
        rested to where it came to rest, at the pace the wheel turned, as late as the page painted it
        (:func:`_wheel_late`) - what a compositor shows of a page that paints late. Else the log's on the paint's
        clock, moved to meet every keyframe on its instant - the keyframe's own where the log has not moved since it,
        or will not until it (at rest) -, and between the offsets the keyframes either side painted: a log
        running behind a slow page's paints, its main thread answering once a paint, never carries a frame past what
        they painted."""
        if wheel is not None:
            share = float(np.interp(t - late, wheel_t, wheel_share))
            return anchor_offsets[0] + (anchor_offsets[-1] - anchor_offsets[0]) * share
        k = bisect.bisect_right(anchor_times, t)
        logged = scroll.offset(t + lag)
        for j in (k - 1, k):
            if 0 <= j < len(anchors) and abs(logged - scroll.offset(anchor_times[j] + lag)) < 0.01:
                return anchor_offsets[j]
        if k <= 0:
            residual = residuals[0]
        elif k >= len(anchors):
            residual = residuals[-1]
        else:
            t0, t1 = anchor_times[k - 1], anchor_times[k]
            u = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
            residual = residuals[k - 1] + (residuals[k] - residuals[k - 1]) * u
        low, high = sorted((anchor_offsets[max(k - 1, 0)], anchor_offsets[min(k, len(anchors) - 1)]))
        return min(max(logged * ratio + residual, low), high)

    # Sticky and fixed bands inside the scroller: held as painted, never translated with the content under them.
    # Pinned only where the content moved past it on two pairs of keyframes, a part of the scroller, and on the page's own
    # fixed or sticky element; every run refused is said with why.
    refused: list[tuple[int, int, str]] = []
    bands = pinned_bands(moving, offsets, refused)
    on_the_page(
        bands, scroll.fixed, ratio=ratio, top=moving_top, left=moving_left, right=moving_right, rejected=refused
    )
    # A page's own fixed bar, the same before and after the scroll, stands still on every keyframe.
    steady_bands(bands, scroll.fixed, ratio=ratio, top=moving_top, left=moving_left, right=moving_right)
    pins = _pins(moving, offsets, bands)
    union = np.logical_or.reduce(bands) if bands else np.zeros(rows, dtype=bool)
    runs = _band_runs(union)
    if runs:
        kept["pinned"] = [[round((moving_top - y0 + a) / ratio), round((b - a) / ratio)] for a, b in runs]
        if scroll.fixed is not None:
            kept["pinned_on"] = [
                _candidate_payload(
                    _candidate(scroll.fixed, ratio, moving_top + a, moving_top + b, moving_left, moving_right)
                )
                for a, b in runs
            ]
    if refused:
        said: dict[tuple[int, int, str], None] = {}
        for a, b, why in refused:
            said[(round((moving_top - y0 + a) / ratio), round((b - a) / ratio), why)] = None
        kept["rejected"] = [[top_css, height_css, why] for top_css, height_css, why in list(said)[:MAX_REJECTED]]
    # A keyframe blank where a band stands (a repaint) is never drawn there: neither as the band nor as content.
    blank_rows = _blank_rows(frames, sources, bands, runs, box=(moving_top, moving_left, moving_right))
    # Once the scroller stopped, the frames are the page's own: no band composited (the browser's frame).
    stopped = (
        _moving(scroll.log)[1]
        if len(scroll.log) > 1 and any(scroll.log[i][1] != scroll.log[i - 1][1] for i in range(1, len(scroll.log)))
        else end
    )
    # The log runs *lag* ahead of the paints: a keyframe at *t* shows where the log stood at *t + lag*.
    rest = anchor_offsets[-1]
    settled_from = next(
        (j for j in range(len(sources)) if source_times[j] + lag >= stopped - 1e-6 and abs(offsets[j] - rest) < 1), None
    )
    drawn: list[tuple[float, str]] = []
    saving: list[Any] = []
    #: The offset (CSS px) each frame drawn shows: what its cadence is measured on.
    shown: list[float] = []
    #: Each held band's rows on every frame drawn (grey, every 4th column): what the blink self-check reads.
    strips: dict[tuple[int, int], list[Any]] = {run: [] for run in runs}
    strip_times: list[float] = []

    def keep_strips(picture: Any, t: float) -> None:
        strip_times.append(t)
        for a, b in runs:
            strips[(a, b)].append(
                picture[moving_top + a : moving_top + b, moving_left:moving_right:4].astype(np.float32).mean(axis=2)
            )

    held = 0
    first, last = math.ceil(start * fps - 1e-6), math.floor(end * fps + 1e-6)
    for n in range(first, last + 1):
        t = n / fps
        k = max(0, bisect.bisect_right(source_times, t) - 1)
        base = frames.rgb(sources[k])
        if settled_from is not None and k >= settled_from:
            # Settled and painted at rest: the keyframe as the browser painted it, nothing held over it.
            shown.append(offsets[k] / ratio)
            keep_strips(base, t)
            name = f"p{serial:02d}{n:07d}.jpg"
            saving.append(
                saver.submit(Image.fromarray(np.ascontiguousarray(base)).save, out_dir / name, quality=QUALITY)
            )
            while len(saving) > _SAVING:
                saving.pop(0).result()
            drawn.append((round(t, 6), name))
            continue
        offset = at(t)
        shown.append(round(offset) / ratio)
        picture = base.copy()
        filled = np.zeros(rows, dtype=bool)
        # A keyframe's band rows are never content; a band pinned on the frame is drawn from the keyframe pinning it.
        excluded = {j: bands[j] | blank_rows[j] for j in range(len(sources))}
        held_now = _held_bands(bands, pins, k, offset, offsets, excluded)
        # A band a keyframe either side repainted blank is held from the nearest keyframe that painted it.
        near = [j for j in (k, k + 1) if j < len(sources)]
        for a, b in runs:
            if any(ha <= a and b <= hb for ha, hb, _p in held_now) or not any(blank_rows[j][a:b].any() for j in near):
                continue
            around = [j for j in (k - 1, k, k + 1, k + 2) if 0 <= j < len(sources) and not blank_rows[j][a:b].any()]
            if (
                any(bands[j][a:b].all() for j in around)
                and (donor := _band_donor(bands, blank_rows, a, b, k)) is not None
            ):
                held_now.append((a, b, donor))
        for a, b, holder in held_now:
            pinner = _band_donor(bands, blank_rows, a, b, holder) if blank_rows[holder][a:b].any() else holder
            if pinner is None:
                continue
            picture[moving_top + a : moving_top + b, moving_left:moving_right] = frames.rgb(sources[pinner])[
                moving_top + a : moving_top + b, moving_left:moving_right
            ]
            filled[a:b] = True
        hidden = np.zeros(rows, dtype=bool)
        order = [
            k,
            *([k + 1] if k + 1 < len(sources) else []),
            *sorted(range(len(sources)), key=lambda j: abs(source_times[j] - t)),
        ]
        for j in dict.fromkeys(order):
            shift = round(offset - offsets[j])
            low, high = max(0, -shift), min(rows, rows - shift)
            if high <= low:
                continue
            wanted = np.flatnonzero(~filled[low:high]) + low
            if not wanted.size:
                continue
            blocked = excluded[j][wanted + shift]
            hidden[wanted[blocked]] = True
            wanted = wanted[~blocked]
            if not wanted.size:
                continue
            source = frames.rgb(sources[j])
            picture[moving_top + wanted, moving_left:moving_right] = source[
                moving_top + wanted + shift, moving_left:moving_right
            ]
            filled[wanted] = True
            if filled.all():
                break
        # Rows only a band hid on every keyframe that reached them are drawn from the nearest row painted, never held.
        missing = int((~filled & ~hidden).sum())
        if missing or (~filled).any():
            if missing > MAX_GAP * rows or not filled.any():
                # Scrolled past what any keyframe painted: the frame holds the keyframe before it.
                held += 1
                picture = base
                shown[-1] = offsets[k] / ratio
            else:
                painted_rows = np.flatnonzero(filled)
                nearest = painted_rows[
                    np.clip(np.searchsorted(painted_rows, np.flatnonzero(~filled)), 0, len(painted_rows) - 1)
                ]
                picture[moving_top + np.flatnonzero(~filled), moving_left:moving_right] = picture[
                    moving_top + nearest, moving_left:moving_right
                ]
        keep_strips(picture, t)
        name = f"p{serial:02d}{n:07d}.jpg"
        saving.append(
            saver.submit(Image.fromarray(np.ascontiguousarray(picture)).save, out_dir / name, quality=QUALITY)
        )
        while len(saving) > _SAVING:
            saving.pop(0).result()
        drawn.append((round(t, 6), name))
    for future in saving:
        future.result()
    if not drawn:
        kept["why"] = "shorter than a frame"
        return kept, [], set()
    kept.update({"fps": float(fps), "paced": True, "frames": len(drawn)})
    if (steady := cadence(shown, painted=painted)) is not None:
        kept["cadence"] = steady
    if held:
        # The frames it held step: it plays at the share that moved.
        kept["held"] = held
        kept["fps"] = round(fps * (len(drawn) - held) / len(drawn), 2)
        moved, between = max(
            (
                (abs(b - a) / ratio, tb - ta)
                for (a, ta), (b, tb) in itertools.pairwise(zip(offsets, source_times, strict=True))
            ),
            key=lambda pair: pair[0],
        )
        shows = rows / ratio
        if moved > shows:
            # It outran the page's paints: two of them share none of its rows. The take says how long the step takes
            # for every two to share enough.
            began, stopped = _moving(scroll.log)
            kept["outran"] = {"moved": round(moved), "between": round(between, 2), "shows": round(shows)}
            kept["seconds"] = math.ceil((stopped - began) * moved / (OVERLAPPING * shows) * 2) / 2
    if top or bottom or left or right:
        kept["still"] = {"top": top, "bottom": bottom, "left": left, "right": right}
    if runs and (
        repaints := _band_repaints(
            frames, sources, bands, blank_rows, runs, box=(moving_top, moving_left, moving_right)
        )
    ):
        kept["band_repaint"] = [
            [round((moving_top - y0 + a) / ratio), round((b - a) / ratio), level] for a, b, level in repaints
        ]
    # Self-check: a held band that moved and came back within a few frames drawn is a blink, named by its frames.
    blinks = sorted({t for run in runs for t in band_blinks(strips[run], strip_times)})
    if blinks:
        kept["band_blink"] = blinks[:MAX_BLINKS]
    return kept, drawn, set(inside)


def band_blinks(strips: Sequence[Any], times: Sequence[float], *, within: int = BLINK_FRAMES) -> list[float]:
    """The instants of the frames drawn for a scroll whose band (*strips*: its rows on each frame drawn, grey, one per
    instant of *times*) moved off what the frame before drew and came back within *within* frames: a sticky
    header pushed for a frame or two, a blink. A band that changes and stays changed (a hover, a counter) is no blink."""
    found: list[float] = []
    count = len(strips)
    n = 1
    while n < count:
        if strips[n].shape != strips[n - 1].shape or _band_alike(strips[n], strips[n - 1]):
            n += 1
            continue
        back = next(
            (
                m
                for m in range(n + 1, min(count, n + within + 1))
                if strips[m].shape == strips[n - 1].shape and _band_alike(strips[m], strips[n - 1])
            ),
            None,
        )
        if back is None:
            n += 1
            continue
        found.extend(round(float(times[m]), 3) for m in range(n, back))
        n = back + 1
    return found


def _band_alike(a: Any, b: Any, *, shift: int = BLINK_SHIFT) -> bool:
    """Whether two strips of a band are the same band: their mean grey difference under
    :data:`BLINK_LEVELS` as they stand or with one moved by up to *shift* rows - a band a decoded MP4 (a take repaired
    from it) draws a device row off, an edge the encoder moved, is the band in place, never a blink."""
    import numpy as np

    if float(np.abs(a - b).mean()) < BLINK_LEVELS:
        return True
    rows = a.shape[0]
    for d in range(1, min(shift, rows - 1) + 1):
        if float(np.abs(a[d:] - b[:-d]).mean()) < BLINK_LEVELS or float(np.abs(a[:-d] - b[d:]).mean()) < BLINK_LEVELS:
            return True
    return False


def _band_donor(bands: Sequence[Any], blank_rows: dict[int, Any], a: int, b: int, k: int) -> int | None:
    """The keyframe a band (rows *a* to *b*) is drawn from where a keyframe painted it blank: *k* or the nearest
    before it that holds the band and painted it, else the nearest after; ``None`` when none did (it is not drawn)."""
    for j in [*range(k, -1, -1), *range(k + 1, len(bands))]:
        if bands[j][a:b].all() and not blank_rows[j][a:b].any():
            return j
    return None


def _band_repaints(
    frames: Keyframes,
    sources: Sequence[int],
    bands: Sequence[Any],
    blank_rows: dict[int, Any],
    runs: Sequence[tuple[int, int]],
    *,
    box: tuple[int, int, int],
) -> list[tuple[int, int, float]]:
    """The bands held as painted whose pixels differ from the page's own paint of them once the scroll settled:
    ``(first row, past the last, grey levels apart)``, read on the last keyframe against each keyframe that held it -
    :data:`BAND_REPAINT_LEVELS` or more on one is a band repainted, said by the take."""
    import numpy as np

    top, left, right = box
    last = len(sources) - 1
    found = []
    final = frames.grey(sources[last])
    for a, b in runs:
        if blank_rows[last][a:b].any():
            continue
        own = final[top + a : top + b, left:right:2]
        worst = 0.0
        for j in range(last):
            if bands[j][a:b].all() and not blank_rows[j][a:b].any():
                worst = max(worst, float(np.abs(frames.grey(sources[j])[top + a : top + b, left:right:2] - own).mean()))
        if worst >= BAND_REPAINT_LEVELS:
            found.append((a, b, round(worst, 1)))
    return found


def _candidate_payload(box: tuple[float, float, float, float, str] | None) -> list[Any] | None:
    """How the take keeps the page's element a band stands on: ``[position, [x, y, w, h]]`` in CSS px."""
    if box is None:
        return None
    x, y, w, h, position = box
    return [position, [round(x, 1), round(y, 1), round(w, 1), round(h, 1)]]


def _covered(size: int, low: float, high: float) -> Any:
    """How much of each of *size* rows (or columns) a keyframe drawn under a zoom paints, from ``low`` to ``high``: 1
    inside, fading to 0 over :data:`FEATHER` px where what it painted ends inside the box (never at the box's edge)."""
    import numpy as np

    at = np.arange(size, dtype=np.float32)
    low = -math.inf if low <= 0.5 else low
    high = math.inf if high >= size - 1.5 else high
    return np.clip(np.minimum(at - low, high - at) / FEATHER, 0.0, 1.0)


def _drawn_under(picture: Any, zoom: Zoom) -> tuple[Any, Any, Any]:
    """*picture* (a keyframe's box, PIL) drawn as the frame shows it: ``frame[p] = picture[zoom(p)]``, and how much of
    its rows and columns it paints (:func:`_covered`)."""
    from PIL import Image

    width, height = picture.size
    # PIL maps pixel edges, the zoom pixel centres.
    edge = 0.5 * (1 - zoom.scale)
    drawn = picture.transform(
        picture.size,
        Image.Transform.AFFINE,
        (zoom.scale, 0.0, zoom.tx + edge, 0.0, zoom.scale, zoom.ty + edge),
        resample=Image.Resampling.BILINEAR,
    )
    columns = _covered(width, -zoom.tx / zoom.scale, (width - 1 - zoom.tx) / zoom.scale)
    rows = _covered(height, -zoom.ty / zoom.scale, (height - 1 - zoom.ty) / zoom.scale)
    return drawn, rows, columns


def double_image_score(frame: Any, one: Any, other: Any) -> float:
    """How much of a frame (grey, any float array) carries two pictures at once: of the edges only *one* has
    and of those only *other* has (the two keyframes drawn where the frame stands), the smaller share the frame draws
    too. A frame that is one of them draws one set and almost none of the other (near 0); a cross-dissolve of the two
    draws both (towards 1): a double image past :data:`DOUBLE_IMAGE_SHARE`. ``0.0`` when either has too few edges
    of its own to tell."""
    import numpy as np
    from scipy.ndimage import binary_dilation, gaussian_filter

    def edges(picture: Any, levels: float) -> Any:
        dy, dx = np.gradient(gaussian_filter(np.asarray(picture, dtype=np.float32), 1.0))
        return np.hypot(dy, dx) > levels

    # A blend draws each keyframe's edges at its share of their contrast: the frame's are read at a fraction of it.
    drawn = edges(frame, DOUBLE_EDGE_LEVELS * DOUBLE_EDGE_FAINT)
    first, second = edges(one, DOUBLE_EDGE_LEVELS), edges(other, DOUBLE_EDGE_LEVELS)
    only_first = first & ~binary_dilation(second, iterations=2)
    only_second = second & ~binary_dilation(first, iterations=2)
    if int(only_first.sum()) < DOUBLE_EDGE_MIN or int(only_second.sum()) < DOUBLE_EDGE_MIN:
        return 0.0
    seen = binary_dilation(drawn, iterations=1)
    return min(float(seen[only_first].mean()), float(seen[only_second].mean()))


def _draw_zoom_frame(
    base: Any,
    before: Any,
    after: Any,
    zoom: Zoom,
    u: float,
    origin: tuple[int, int],
    still: Any | None,
    path: Path,
    near_after: bool | None = None,
) -> bool:
    """One frame of a canvas's zoom, *u* of the way from the keyframe *before* to the one *after* (their boxes, PIL),
    written at *path*: the NEARER keyframe moved geometrically - the one before zoomed on *u* of *zoom*, or
    the one after zoomed back the rest -, never the two blended: a cross-dissolve of two keyframes draws every label
    and node twice wherever the zoom does not explain them to the pixel (labels drawn at a fixed size, a re-layout).
    Where the nearer one painted nothing of the box (a zoom out uncovering its edges), the other is drawn there,
    hard-edged; what lies outside its box as *base* (the whole keyframe before) painted it, and what stands *still*
    over the canvas (a box the page draws over it, a label that never moved) as the NEARER keyframe painted it - the
    keyframe the canvas under it is drawn from, never the other (held from the keyframe before while the canvas
    came from the one after, a label crossing the held box's edge read split, the ones inside it offset by the zoom).
    ``False`` when neither painted too much of it (a frame the nearer keyframe holds). *near_after* says which
    keyframe is the nearer one (:func:`nearer_sides`), ``u >= 0.5`` when not given."""
    import numpy as np
    from PIL import Image

    if near_after is None:
        near_after = u >= 0.5
    width, height = before.size
    ahead, ahead_rows, ahead_columns = _drawn_under(before, zoom.share(-u))
    back, back_rows, back_columns = _drawn_under(after, zoom.share(1 - u))
    # Coverage is weighed on every fourth pixel and drawn back to the box: it only changes over the feather.
    covered_before = np.minimum.outer(ahead_rows[2::4], ahead_columns[2::4]) >= 0.5
    covered_after = np.minimum.outer(back_rows[2::4], back_columns[2::4]) >= 0.5
    if near_after:
        near, far, near_covered, far_covered = back, ahead, covered_after, covered_before
    else:
        near, far, near_covered, far_covered = ahead, back, covered_before, covered_after
    holes = ~near_covered & ~far_covered
    if holes.mean() > MAX_GAP:
        return False
    box = near
    if not near_covered.all():
        uncovered = Image.fromarray((~near_covered * 255).astype(np.uint8)).resize(
            (width, height), Image.Resampling.NEAREST
        )
        box = Image.composite(far, near, uncovered)
    if holes.any():
        hole = Image.fromarray((holes * 255).astype(np.uint8)).resize((width, height), Image.Resampling.NEAREST)
        box = Image.composite(after if near_after else before, box, hole)
    if still is not None:
        # Held as the nearer keyframe painted it, the one the canvas around it is drawn from.
        box = Image.composite(after if near_after else before, box, still)
    frame = base.copy()
    frame.paste(box, origin)
    frame.save(path, quality=QUALITY)
    return True


def nearer_sides(placed: list[tuple[int, float]]) -> list[bool]:
    """For each frame of a zoom (*placed*: its keyframe pair and its share *u* of the way through it, in frame order),
    whether it is drawn from the keyframe after: from the first frame of the pair at half way on, and never back to the
    keyframe before within that pair.

    The share is read off the canvas's log (:func:`_zoom_share`); a log that jitters about the midpoint (a wheel's
    deltas, an easing overshoot) would draw the canvas, and every box held over it (a toolbar, a floating button), from
    one keyframe, then the other, then the first again: the page's static chrome at two places in turn, the render's
    ``static_flicker`` (#9590, a canvas wheel zoom after a pause). Once the keyframe after is the source, it stays."""
    sides: list[bool] = []
    latched: int | None = None
    for k, u in placed:
        after = u >= 0.5 or latched == k
        if after:
            latched = k
        sides.append(after)
    return sides


def _zoom_share(scroll: Scroll, t: float, t0: float, t1: float, reach: float) -> float:
    """How far the frame at *t* is from the keyframe at *t0* to the one at *t1*: how far the canvas's log went between
    them (its transform's scale, the wheel's delta), else how far in time when the log stood still (a canvas easing
    on after the wheel)."""
    p0, p1 = scroll.offset(t0), scroll.offset(t1)
    if abs(p1 - p0) > 1e-3 * reach:
        u = (scroll.offset(t) - p0) / (p1 - p0)
    else:
        u = (t - t0) / (t1 - t0) if t1 > t0 else 1.0
    return min(1.0, max(0.0, u))


def _pace_zoom(
    scroll: Scroll,
    frames: Keyframes,
    times: list[float],
    *,
    fps: int,
    ratio: float,
    out_dir: Path,
    serial: int,
    saver: ThreadPoolExecutor,
    carry: frozenset[int] = frozenset(),
    seen: dict | None = None,
) -> tuple[dict, list[tuple[float, str]], set[int]]:
    """A canvas's zoom paced: what the take keeps of it, the frames drawn for it and the keyframes they
    replace. How it moved from each keyframe to the next is read off their pixels (:func:`register_zoom`); each frame
    between them is drawn by :func:`_draw_zoom_frame`, as far between them as the canvas's log says.

    *carry*: indices of ``scroll.over`` the take's other zooms of the same canvas say ride it
    (:func:`carried_boxes`): drawn with the canvas unless they stand still on this one. *seen*, when given, is filled
    with what this zoom read of its boxes: ``over`` (CSS px), ``standing`` and ``riding`` (indices)."""
    import numpy as np
    from PIL import Image, ImageChops, ImageFilter

    kept = _compact(scroll)
    start, end = scroll.span
    length = max(end - start, 1e-6)
    _before, inside, _after = span_frames(times, scroll.span)
    painted = len(inside) / length
    kept.update({"painted": round(painted, 2), "fps": round(painted, 2), "paced": False})
    if scroll.unpaceable:
        kept["why"] = scroll.unpaceable
        return kept, [], set()
    if painted >= PAINTED_ENOUGH * fps:
        kept["why"] = PAINTED_AT_RATE
        return kept, [], set()
    sources, left_out = zoom_sources(times, scroll.span)
    if left_out:
        kept["outside"] = left_out
    if len(sources) < 2:
        kept["why"] = (
            OUTSIDE_STRETCH if left_out and len(sources) + left_out >= 2 else "fewer than two keyframes painted it"
        )
        return kept, [], set()
    height, width = frames.rgb(sources[0]).shape[:2]
    bx, by, bw, bh = (v * ratio for v in scroll.box or (0.0, 0.0, width / ratio, height / ratio))
    x0, y0 = max(0, round(bx)), max(0, round(by))
    x1, y1 = min(width, round(bx + bw)), min(height, round(by + bh))
    if x1 - x0 < 64 or y1 - y0 < 64:
        kept["why"] = "its canvas is too small"
        return kept, [], set()
    levels = [zoom_levels(frames.grey(i)[y0:y1, x0:x1]) for i in sources]
    over = [(ox * ratio - x0, oy * ratio - y0, ow * ratio, oh * ratio) for ox, oy, ow, oh in scroll.over]
    still, still_over = standing(levels, over)
    if scroll.appeared:
        # A box the page opened over the canvas during the zoom (a tooltip) tells its keyframes apart where no zoom
        # does: left out of the registration and held as the nearer keyframe painted it.
        still = appeared_still(
            still,
            levels,
            [(ox * ratio - x0, oy * ratio - y0, ow * ratio, oh * ratio) for ox, oy, ow, oh in scroll.appeared],
        )
        kept["appeared"] = len(scroll.appeared)
    anchors = [((x1 - x0 - 1) / 2, (y1 - y0 - 1) / 2)]
    hover = None
    if scroll.anchor is not None:
        hover = (scroll.anchor[0] * ratio - x0, scroll.anchor[1] * ratio - y0)
        anchors.insert(0, hover)
    # Pairs registered once judged without what the cursor lights (:data:`HOVER_PX`).
    hovered: list[int] = []
    zooms = [
        register_zoom(levels[k - 1], levels[k], anchors, still, None, hover, hovered) for k in range(1, len(sources))
    ]
    # A pair its pixels did not register takes the zoom the canvas's log says it made, where that draws it closer.
    unregistered_why: dict[int, str] = {}
    unregistered_explained: dict[int, float] = {}
    unregistered_left: dict[int, int] = {}
    zooms, implied = implied_zooms(
        zooms,
        scroll,
        [times[i] for i in sources],
        levels,
        still,
        unregistered_why,
        unregistered_explained,
        hover,
        hovered,
        unregistered_left,
    )
    if hovered:
        kept["hovered"] = len(hovered)
    moving = [zoom for zoom in zooms if zoom is not None and zoom != IDENTITY]
    if not moving:
        kept["why"] = (
            NOTHING_MOVED if all(zoom == IDENTITY for zoom in zooms) else "its keyframes could not be registered"
        )
        return kept, [], set()
    factor = levels[0][-1][0]
    standing_mask = None
    if still[-1].any():
        # A box the page draws over the canvas stands still to its device pixel, not to the finest width's.
        drawn_still = still[-1].copy()
        for box in still_over:
            drawn_still[_inside(drawn_still.shape, box, factor)] = False
        upscaled = np.kron(drawn_still, np.ones((factor, factor), dtype=bool))
        full = np.zeros((y1 - y0, x1 - x0), dtype=np.uint8)
        full[: upscaled.shape[0], : upscaled.shape[1]] = upscaled[: y1 - y0, : x1 - x0] * 255
        for box in still_over:
            full[_inside(full.shape, box, 1)] = 255
        standing_mask = Image.fromarray(full).filter(ImageFilter.GaussianBlur(1))
    # A box the zoom carries - a label a graph lays over a node - is drawn with the canvas, never held. A box the page
    # still drew at the same place once the zoom settled (a toolbar, a Legend button) stands, whatever its pixels say.
    standing_boxes = {index for index, box in enumerate(over) if box in still_over or index in scroll.stayed}
    riding = (riding_boxes(levels, over, zooms) - standing_boxes) if over else set()
    # A box the take's other zooms of this canvas say rides it is drawn with the canvas here too, unless it
    # stands still on this zoom: one take never draws a label with the canvas on one wheel step and holds it on the next.
    carried = {index for index in carry if 0 <= index < len(over)} - standing_boxes - riding
    riding |= carried
    if seen is not None:
        seen.update(over=list(scroll.over), standing=standing_boxes, riding=set(riding))
    held_over = [box for index, box in enumerate(over) if index not in riding]
    if riding:
        kept["riding"] = len(riding)
    if carried:
        kept["carried"] = len(carried)
    if held_over:
        # Every other box the page draws over the canvas - a toolbar, a legend, a rail - is drawn as the nearer
        # keyframe painted it, never zoomed with the canvas, whether or not it changed on a keyframe (a hover, a count).
        drawn_over = np.zeros((y1 - y0, x1 - x0), dtype=np.uint8)
        for box in held_over:
            drawn_over[_inside(drawn_over.shape, box, 1)] = 255
        if drawn_over.any():
            over_mask = Image.fromarray(drawn_over)
            standing_mask = over_mask if standing_mask is None else ImageChops.lighter(standing_mask, over_mask)
            kept["overlays"] = len(held_over)
            # Of them, those that stand still on every keyframe (a toolbar, a legend): held as they are, not a defect.
            kept["overlays_still"] = sum(
                1 for index in range(len(over)) if index not in riding and index in standing_boxes
            )
    source_times = [times[i] for i in sources]
    values = [value for _t, value in scroll.log]
    reach = (max(values) - min(values)) or 1.0
    names = [frames.frames[i][1] for i in sources]
    region = (x0, y0, x1, y1)
    opened: dict[int, tuple[Any, Any]] = {}

    def keyframe(k: int) -> tuple[Any, Any]:
        """The keyframe *k* of the zoom (PIL) and its canvas's box, the last two kept."""
        if k not in opened:
            with Image.open(frames.workdir / names[k]) as image:
                whole = image.convert("RGB")
            whole.load()
            opened[k] = (whole, whole.crop(region))
            for old in [key for key in opened if key < k - 1]:
                del opened[old]
        return opened[k]

    drawn: list[tuple[float, str]] = []
    drawing: list[tuple[int, Any]] = []
    held = 0
    # Of them, frames of a registered pair neither keyframe painted enough of (a zoom out past both edges).
    uncovered = 0

    def nearest(t: float) -> str:
        """The keyframe nearest *t*: what a frame the zoom cannot draw holds - never two blended."""
        k = min(range(len(source_times)), key=lambda i: abs(source_times[i] - t))
        return names[k]

    def finish(job: tuple[int, Any]) -> None:
        nonlocal held, uncovered
        index, future = job
        if not future.result():
            t, _name = drawn[index]
            drawn[index] = (t, nearest(t))
            held += 1
            uncovered += 1

    first, last = math.ceil(start * fps - 1e-6), math.floor(end * fps + 1e-6)
    placed: list[tuple[float, int, float]] = []
    for n in range(first, last + 1):
        t = n / fps
        k = min(max(bisect.bisect_right(source_times, t) - 1, 0), len(sources) - 2)
        placed.append((t, k, _zoom_share(scroll, t, source_times[k], source_times[k + 1], reach)))
    sides = nearer_sides([(k, u) for _t, k, u in placed])
    for n, ((t, k, u), after) in enumerate(zip(placed, sides, strict=True), start=first):
        zoom = zooms[k]
        if zoom is None:
            # Not registered: the nearer keyframe held, never the two blended.
            held += 1
            drawn.append((round(t, 6), names[k + 1] if after else names[k]))
            continue
        if zoom == IDENTITY or (u <= 1e-4 and not after):
            # Nothing to move: the keyframe the pair is drawn from (once the one after, never back).
            drawn.append((round(t, 6), names[k + 1] if after else names[k]))
            continue
        if u >= 1 - 1e-4:
            drawn.append((round(t, 6), names[k + 1]))
            continue
        name = f"z{serial:02d}{n:07d}.jpg"
        base, box_before = keyframe(k)
        _after_whole, box_after = keyframe(k + 1)
        future = saver.submit(
            _draw_zoom_frame,
            base,
            box_before,
            box_after,
            zoom,
            u,
            (x0, y0),
            standing_mask,
            out_dir / name,
            near_after=after,
        )
        drawn.append((round(t, 6), name))
        drawing.append((len(drawn) - 1, future))
        while len(drawing) > _DRAWING:
            finish(drawing.pop(0))
    for job in drawing:
        finish(job)
    if not drawn:
        kept["why"] = "shorter than a frame"
        return kept, [], set()
    if held > MAX_HELD * len(drawn):
        kept["why"] = "its keyframes could not be registered"
        return kept, [], set()
    kept.update({"fps": float(fps), "paced": True, "frames": len(drawn), "offsets": "registered"})
    if implied:
        kept["implied"] = implied
    # Drawn by moving one keyframe, never a cross-dissolve of two: what the ``double_image`` finding reads.
    kept["synthesis"] = ZOOM_SYNTHESIS
    kept["zoom"] = round(math.prod(zoom.scale for zoom in moving), 3)
    if held:
        kept["held"] = held
        kept["held_not_blended"] = True
        # Why the pairs left unregistered were (:data:`HELD_WHY`), each counted once: what the step's words name.
        reasons: dict[str, int] = {}
        for k in sorted(unregistered_why):
            if zooms[k] is None:
                reasons[unregistered_why[k]] = reasons.get(unregistered_why[k], 0) + 1
        if uncovered:
            reasons["uncovered"] = uncovered
        if reasons:
            kept["held_why"] = reasons
        # How near the bar the best zoom tried came on those pairs: a zoom missed, or pictures no zoom relates.
        shares = [share for k, share in unregistered_explained.items() if zooms[k] is None]
        if shares:
            kept["held_explained"] = round(max(shares), 3)
        # Where what the best zoom leaves sits: in a few parts of the box (a change in one place), or all over it.
        parts = [count for k, count in unregistered_left.items() if zooms[k] is None]
        if parts:
            kept["held_left_parts"] = max(parts)
    if standing_mask is not None or riding:
        kept["held_from"] = ZOOM_HELD_FROM
    if standing_mask is not None:
        kept["still_share"] = round(float(still[-1].mean()), 3)
    return kept, drawn, set(inside)


def pace(
    frames: Sequence[tuple[float, str]],
    events: list[dict],
    *,
    workdir: str,
    fps: int,
    pixel_ratio: float,
    painted: dict[str, float] | None = None,
    done: dict[str, dict] | None = None,
    on_step: Any = None,
    canvas_runs: Sequence[dict] = (),
) -> Paced:
    """The keyframes *frames* (``(seconds, file name in workdir)``) of a take, each scroll its *events* logged drawn
    again at *fps* where its keyframes were fewer (see the module's docstring). *painted* is the window's scroll offset
    each keyframe painted, by file name (CSS px, ``capture.CaptureResult.painted``). Each event's ``scrolls``
    are rewritten in place as the take keeps them: ``{t, scroller, element?, box?, wheel?, log, progress?, painted, fps,
    paced, frames?, offsets?, read?, late?, still?, pinned?, pinned_on?, rejected?, band_repaint?, band_blink?, zoom?, still_share?,
    held?, implied?, outran?, seconds?, why?}`` - ``pinned``: the sticky bands held as painted ``[top, height]`` (CSS px of the
    scroller), ``pinned_on`` the page's fixed or sticky element each stands on, ``rejected`` the candidates refused
    ``[top, height, why]``, ``band_repaint`` the held bands unlike the page's own paint ``[top, height, levels]``;
    ``offsets``: ``painted`` (``read`` of them off their pixels) or ``registered`` off the pixels; ``late``: how
    much later than its wheel the page painted the scroll; a canvas's: ``progress`` what its log follows, ``zoom`` how far it zoomed,
    ``still_share`` the share of its box kept as painted. Each event's ``motion`` windows are drawn again at *fps*
    too (``transition_pacing.pace_transitions``), and each step says the rates it plays at and what it
    scrolled (:func:`summarise`).

    *done*: the steps a stopped worker paced already (``pacing_checkpoint``, #10030), reused where their scroll is the
    same and their frames are in *workdir*, never drawn again; *on_step*: called with each step paced here, ``(key,
    record)``, once its frames are written. ``out.resumed_at`` says the step it resumed at, when it reused any.
    """
    from app.services.video_studio import pacing_checkpoint, transition_pacing

    times = [float(t) for t, _name in frames]
    reader = Keyframes(workdir, frames)
    out = Paced(frames=list(frames))
    replaced: set[int] = set()
    drawn: list[tuple[float, str]] = []
    claimed: list[tuple[float, float]] = []
    #: Every scroll that moved, paced or not: no transition is blended across it.
    moving: list[tuple[float, float]] = []
    serial = 0
    #: Every scroll paced, in order: ``[scroll, kept, frames drawn, keyframes replaced, serial, seen]`` (a
    #: canvas zoom whose boxes the take's other zooms say ride it is paced again with them carried, before any is used).
    paced: list[list[Any]] = []
    with ThreadPoolExecutor(max_workers=4) as saver:
        for event in events:
            raws = event.get("scrolls")
            if not isinstance(raws, list):
                continue
            kept_scrolls = []
            for raw in raws:
                read = read_scrolls(raw) if isinstance(raw, dict) and "samples" in raw else []
                if not read:
                    if isinstance(raw, dict) and "log" in raw:
                        kept_scrolls.append(raw)
                    continue
                for scroll in read:
                    if any(scroll.span[0] < b and a < scroll.span[1] for a, b in claimed):
                        scroll.unpaceable = "it overlaps a scroll paced before"
                    key = pacing_checkpoint.scroll_key(serial)
                    record = (done or {}).get(key)
                    if (
                        record is not None
                        and record.get("span") == pacing_checkpoint.span_of(scroll.span)
                        and pacing_checkpoint.usable(record, workdir)
                    ):
                        # Paced by the worker stopped before: its frames are the same files, never drawn again.
                        kept, frames_drawn, used, seen = pacing_checkpoint.read_scroll_step(record)
                        out.reused.append(key)
                        if frames_drawn:
                            claimed.append(scroll.span)
                        if kept.get("why") not in UNSEEN:
                            moving.append(scroll.span)
                        if kept.get("outran"):
                            out.notes.append(outran_note(event.get("step"), kept))
                        paced.append([scroll, kept, frames_drawn, used, serial, seen])
                        serial += 1
                        kept_scrolls.append(kept)
                        continue
                    seen = {}
                    try:
                        kept, frames_drawn, used = _pace_scroll(
                            scroll,
                            reader,
                            times,
                            fps=fps,
                            ratio=float(pixel_ratio),
                            out_dir=Path(workdir),
                            serial=serial,
                            saver=saver,
                            painted_offsets=painted,
                            seen=seen,
                        )
                    except Exception as exc:  # a scroll not paced keeps its keyframes: the take is never refused for it
                        logger.warning("A scroll of the take was not paced: %s", exc, exc_info=True)
                        kept = {**_compact(scroll), "paced": False, "why": "it could not be paced"}
                        frames_drawn, used, seen = [], set(), {}
                    if frames_drawn:
                        claimed.append(scroll.span)
                    if kept.get("why") not in UNSEEN:
                        moving.append(scroll.span)
                    if kept.get("outran"):
                        out.notes.append(outran_note(event.get("step"), kept))
                    if on_step is not None:
                        on_step(
                            key,
                            pacing_checkpoint.scroll_step(scroll.span, copy.deepcopy(kept), frames_drawn, used, seen),
                        )
                    paced.append([scroll, kept, frames_drawn, used, serial, seen])
                    serial += 1
                    kept_scrolls.append(kept)
            event["scrolls"] = kept_scrolls
        for row, carry in carried_boxes(paced).items():
            scroll, kept, _frames, _used, at, _seen = paced[row]
            try:
                again, frames_drawn, used = _pace_zoom(
                    scroll,
                    reader,
                    times,
                    fps=fps,
                    ratio=float(pixel_ratio),
                    out_dir=Path(workdir),
                    serial=at,
                    saver=saver,
                    carry=carry,
                )
            except Exception as exc:  # the first pacing stands
                logger.warning(
                    "A canvas zoom of the take was not paced again with its boxes carried: %s", exc, exc_info=True
                )
                continue
            if bool(frames_drawn) != bool(paced[row][2]):
                continue
            # The take's entries are the same dicts: rewritten in place, every event keeps them.
            kept.clear()
            kept.update(again)
            paced[row][2], paced[row][3] = frames_drawn, used
        for _scroll, kept, frames_drawn, used, _at, _seen in paced:
            if frames_drawn:
                drawn.extend(frames_drawn)
                replaced |= used
            out.scrolls.append(kept)
        record = (done or {}).get(pacing_checkpoint.TRANSITIONS)
        motion = record.get("motion") if record is not None else None
        if (
            record is not None
            and isinstance(motion, list)
            and len(motion) == len(events)
            and pacing_checkpoint.usable(record, workdir)
        ):
            # Its transitions paced by the worker stopped before, once every scroll was.
            for event, kept_motion in zip(events, motion, strict=True):
                if event.get("motion") or kept_motion:
                    event["motion"] = kept_motion
            blended = [(float(t), str(name)) for t, name in record.get("frames") or []]
            used = {int(i) for i in record.get("used") or []}
            out.reused.append(pacing_checkpoint.TRANSITIONS)
        else:
            blended, used = transition_pacing.pace_transitions(
                reader,
                times,
                events,
                fps=fps,
                writer=FrameWriter(Path(workdir), saver),
                scrolls=moving,
                canvas_runs=canvas_runs,
            )
            if on_step is not None:
                on_step(
                    pacing_checkpoint.TRANSITIONS,
                    pacing_checkpoint.transitions_step(
                        blended, used, [copy.deepcopy(event.get("motion")) for event in events]
                    ),
                )
        drawn.extend(blended)
        replaced |= used
    for event in events:
        summarise(event)
    if drawn:
        out.frames = sorted([frame for i, frame in enumerate(frames) if i not in replaced] + drawn)
    out.resumed_at = pacing_checkpoint.resumed_at(done, out.reused)
    return out


def outran_note(step: Any, scroll: dict) -> str:
    """What a take's notes say of a *scroll* that outran the page's paints (``outran``): how far it moved
    between two of them, the frames it held, and the ``seconds`` its step takes for every two paints to overlap."""
    outran = scroll["outran"]
    return (
        f"Step {step}'s scroll moved {outran['moved']} px between two paints {float(outran['between']):.2f} s apart, "
        f"further than the {outran['shows']} px its scroller shows: {scroll.get('held')} of its frames hold the one "
        f"before. Record it again with seconds: {float(scroll['seconds']):g} on step {step} - slower, every two paints "
        "overlapping."
    )


def blended_zooms(events: Sequence[dict] | None) -> list[tuple[Any, float, float]]:
    """The canvas zooms of a take drawn by the cross-dissolve of two keyframes: ``(step, start, end)`` of each
    paced canvas scroll without ``synthesis: warped`` - a double image (every label and node twice) on its in-between
    frames, which ``quality`` says as ``double_image`` and a repair (``get_video_recording(repair=true)``) redraws."""
    found: list[tuple[Any, float, float]] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        for scroll in event.get("scrolls") or []:
            if not (isinstance(scroll, dict) and scroll.get("scroller") == "canvas" and scroll.get("paced")):
                continue
            if scroll.get("synthesis") == ZOOM_SYNTHESIS:
                continue
            span = scroll.get("t")
            try:
                start, end = (
                    (float(span[0]), float(span[1]))
                    if isinstance(span, list | tuple) and len(span) == 2
                    else (0.0, 0.0)
                )
            except (TypeError, ValueError):
                start, end = 0.0, 0.0
            found.append((event.get("step"), start, end))
    return found


def misheld_zooms(events: Sequence[dict] | None) -> list[tuple[Any, float, float]]:
    """The canvas zooms of a take drawn by moving one keyframe (``synthesis: warped``) that held what stands over the
    canvas (``overlays``, ``still_share``) the old way (no ``held_from``, or ``nearer``): from the keyframe before on
    every frame, or every box the recorder read over the canvas held, a label the zoom carries included -
    split or offset against the canvas warped around it, a flicker of the labels - which ``quality`` says as
    ``double_image`` and a repair redraws."""
    found: list[tuple[Any, float, float]] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        for scroll in event.get("scrolls") or []:
            if not (isinstance(scroll, dict) and scroll.get("scroller") == "canvas" and scroll.get("paced")):
                continue
            if scroll.get("synthesis") != ZOOM_SYNTHESIS or scroll.get("held_from") == ZOOM_HELD_FROM:
                continue
            if not (scroll.get("overlays") or scroll.get("still_share")):
                continue
            span = scroll.get("t")
            try:
                start, end = (
                    (float(span[0]), float(span[1]))
                    if isinstance(span, list | tuple) and len(span) == 2
                    else (0.0, 0.0)
                )
            except (TypeError, ValueError):
                start, end = 0.0, 0.0
            found.append((event.get("step"), start, end))
    return found


#: A transition (a canvas zoom, a layout change, any paced motion) more than this share of whose frames at the take's
#: rate were drawn from keyframes - synthesised between two of them or the earlier held - is not what the page did on
#: screen: :func:`synthesised_transitions`, a blocking ``synthesised_transition`` finding where a video shows it.
SYNTHESISED_SHARE_MAX = 0.3
#: A transition of the page (not a canvas's) held then cut over at most this long is one repaint of a discrete change
#: (a legend toggled, an svg swapped): it moved nothing between its pictures, never said synthesised (#10943:
#: 0.2 s stretches, "6 held then cut", refused a take).
REPAINT_SECONDS = 0.25


def one_repaint(entry: dict) -> bool:
    """Whether a paced transition is a discrete change cut in one repaint: held, not blended, on the page (not a
    canvas zoom), over :data:`REPAINT_SECONDS` at most."""
    if entry.get("scroller") == "canvas" or not entry.get("held_not_blended"):
        return False
    span = entry.get("t")
    try:
        return float(span[1]) - float(span[0]) <= REPAINT_SECONDS + 1e-6
    except (TypeError, ValueError, IndexError):
        return False


def page_frame_times(events: Sequence[dict] | None) -> list[float] | None:
    """The times the page's renderer produced a frame at over a take (each step's ``page_frames``,
    ``capture._MOTION_WATCH_JS``), sorted; ``None`` for a take recorded before the page counted them (#10970)."""
    if not any(isinstance(event, dict) and isinstance(event.get("page_frames"), list) for event in events or []):
        return None
    return sorted(
        float(t)
        for event in events or []
        if isinstance(event, dict)
        for t in event.get("page_frames") or []
        if isinstance(t, int | float)
    )


def page_limited_fps(
    page: Sequence[float] | None, start: float, end: float, *, painted: float, fps: float
) -> float | None:
    """The rate the page's renderer produced frames at over ``[start, end]`` where the page itself drew under its take's
    rate *fps* and the capture kept (*painted*) every frame it drew: no recorder could have captured more (#10970).
    ``None`` where it drew at the rate, where the capture fell short of it, or where the take did not count the page's
    frames (*page* ``None``)."""
    if page is None or end <= start or fps <= 0:
        return None
    drawn = (bisect.bisect_right(page, end) - bisect.bisect_left(page, start)) / (end - start)
    if drawn >= PAINTED_ENOUGH * fps or painted < PAINTED_ENOUGH * drawn:
        return None
    return round(drawn, 2)


def canvas_limited(
    runs: Sequence[dict] | None,
    start: float,
    end: float,
    *,
    painted: float,
    fps: float,
    composited: bool = False,
) -> tuple[float | None, float | None]:
    """What the canvas copied alone over ``[start, end]`` says of a screencast window there (``alone.runs``,
    ``canvas_alone.composite``: each run's ``drawn`` is the frames a second the page drew its canvas in), #10970
    retest: ``(page_limited, None)`` where the page drew it under the take's rate *fps* and the screencast kept
    (*painted*) every frame it drew - the same classification as its canvas copy, no recorder could capture more -;
    ``(None, page_drew)`` where the page drew under the rate but the screencast kept fewer (a capture short of the
    page); ``(None, None)`` only where no copy that counted the page's frames overlaps the window. A copy run that
    starts before the window and ends after it counts for it as much as one attached to its step (#11032 retest: step
    11, a click inside a copy run of 49 copies at 29.2 fps, was said unmeasured): where the page drew the canvas at the
    rate and every frame it drew was kept, the window is the page's own frames too (``(drawn, None)``: held at them,
    nothing synthesised a recorder could have captured), and where fewer were kept it is a capture short of the page
    (``(None, drawn)``), never unmeasured. The frames kept are the most of the screencast's and the copies' (a copy
    left on the screencast "copied no faster than painted" had the screencast paint at least as many), counted within
    one frame (:func:`kept_every_drawn`). A run composited into the take is left out unless *composited*: a canvas
    scroll or zoom over it plays its copies, so their count tells it (#11097, step 8's wheel zoom inside a composited
    run of 66 copies was said unmeasured)."""
    if fps <= 0 or end <= start:
        return None, None
    # The copy that overlaps the window most tells it (#11021 retest): a neighbour that only grazes its edge does not.
    best: tuple[float, float, float, float] | None = None
    for run in runs or []:
        if not isinstance(run, dict) or not isinstance(run.get("drawn"), int | float):
            continue
        if run.get("composited") and not composited:
            continue
        try:
            a, b = float(run["t"][0]), float(run["t"][1])
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        overlap = min(b, end) - max(a, start)
        if overlap <= 0:
            continue
        copied = float(run["fps"]) if isinstance(run.get("fps"), int | float) else 0.0
        if best is None or overlap > best[0]:
            best = (overlap, float(run["drawn"]), copied, overlap)
    if best is None:
        return None, None
    _overlap, drawn, copied, seconds = best
    # A page drawing its canvas faster than the take's rate needs only the rate kept (:func:`page_drawn_rate`).
    need = page_drawn_rate(drawn, fps)
    if kept_every_drawn(max(painted, copied), need, seconds):
        return need, None
    return None, round(drawn, 2)


def page_drawn_rate(drawn: float, fps: float) -> float:
    """The frames a second of a page's own a take at *fps* can keep where the page drew *drawn*: the take's rate at
    most, so a canvas drawn above it and copied at it is held at the page's frames (#11032 retest, step 11)."""
    return round(min(float(drawn), float(fps)), 2)


def kept_every_drawn(kept: float, drawn: float, seconds: float) -> bool:
    """Whether frames kept at *kept* a second held every frame the page drew at *drawn* a second over *seconds*
    (:data:`PAINTED_ENOUGH` of them), within one frame: over a fifth of a second a rate counts three or four frames,
    and a run of n copies spans n - 1 intervals while the page's counter steps once a frame (#10970 retest: 11 fps
    copied of 17 drawn over 0.2 s is three copies of three or four frames)."""
    if drawn <= 0:
        return True
    return kept * max(seconds, 0.0) + 1.0 >= PAINTED_ENOUGH * drawn * max(seconds, 0.0)


def refused_cause(entry: Any) -> str:
    """Why a span still mostly synthesised is refused under ``fps_fallback: refuse`` (#10970, #11021 retests), so the
    notes tell each cause apart: ``starved`` (the worker's CPU quota held it back over it, or it overlaps a window the
    capture was starved in: ``starved_window``, ``insert_rate.starved``), ``capture short`` (the page drew under the
    rate, the capture kept fewer), ``page-limited, quota unread`` (the page's frames counted, every one kept, the
    quota's hold-back not read), ``unmeasured`` (no count of the page's own frames over it)."""
    entry = entry if isinstance(entry, dict) else {}
    held = entry.get("quota_held_back")
    if isinstance(held, int | float) and float(held) > 0:
        return f"starved: the worker's CPU quota held it back {float(held):.2f} s"
    if (window := _window(entry.get("starved_window"))) is not None:
        return f"starved: it overlaps the capture's starved window {window[0]:g}-{window[1]:g} s"
    if isinstance(entry.get("page_drew"), int | float):
        return f"capture short: the page drew {float(entry['page_drew']):g} fps"
    if isinstance(entry.get("page_limited"), int | float):
        drew = float(entry["page_limited"])
        if isinstance(held, int | float):
            # Every frame the page drew kept, the motion between them drawn from keyframes (a scroll or a zoom paced).
            return f"page-limited, paced between: the page drew {drew:g} fps, the frames between drawn from keyframes"
        return f"page-limited, quota unread: the page drew {drew:g} fps, the worker's CPU quota hold-back was not read"
    if (probe := _window(entry.get("recorder_probe"))) is not None:
        # Drawn while the recorder probed its target (#11063): the page repainting under its own probing, never unmeasured.
        quota = "" if isinstance(held, int | float) else ", quota unread"
        return (
            f"page-limited, recorder probe{quota}: drawn while the recorder probed its target at "
            f"{probe[0]:.2f}-{probe[1]:.2f} s, no copy counted the page's frames there"
        )
    if isinstance(entry.get("screencast_rate"), int | float):
        # No count of the page's frames over it, the quota holding nothing back (#11097): the page's own rate is what
        # the screencast delivered, never unmeasured.
        return (
            f"page-limited, screencast rate: the page delivered {float(entry['screencast_rate']):g} fps on screen, "
            "the worker's CPU quota held nothing back"
        )
    return "unmeasured: the page's own frames were not counted"


#: Seconds past a node's highlight probe its frames are still the probe's: the page repaints once its list gives the
#: focus back (``capture_rate._locate_probes``).
PROBE_TAIL = 0.1
#: The spans a pointer step logs of the recorder's own probing (``capture.Recorder``), each with the seconds past it
#: still the probe's and whether the take holds a frame of before over it (its frames never shown): the node's
#: highlight probe (``probed``), the search of its card (``hover_searched``), the node read and the cursor's approach
#: (``located_in``, ``approached``).
RECORDER_PROBES = (
    ("probed", PROBE_TAIL, True),
    ("hover_searched", 0.0, True),
    ("located_in", 0.0, False),
    ("approached", 0.0, False),
)


def recorder_probes(events: Sequence[dict[str, Any]] | None) -> list[tuple[float, float, bool]]:
    """The spans of *events* the recorder probed its targets over (:data:`RECORDER_PROBES`): ``(start, end, hidden)``,
    *hidden* where the take holds a frame of before over it."""
    found: list[tuple[float, float, bool]] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        for key, tail, hidden in RECORDER_PROBES:
            if (span := _window(event.get(key))) is not None and span[1] > span[0]:
                found.append((span[0], span[1] + tail, hidden))
    return found


def mark_probes(spans: Any, events: Sequence[dict[str, Any]] | None, held_back: Any) -> None:
    """Each span of *spans* (a stretch, a scroll, a transition: ``{t, ...}``) no count of the page's frames classified
    that overlaps the recorder's own probing (:func:`recorder_probes`) told it, in place (#11063, step 16 at the tail of
    a node's locate probe said unmeasured): ``recorder_probe`` the probe's span, ``quota_held_back`` the seconds the
    worker's quota held it back there (starved where it did, else page-limited), ``probe_hidden`` where the take holds a
    frame of before over the whole of it (its frames never shown: not judged as motion)."""
    probes = recorder_probes(events)
    if not probes:
        return
    for entry in spans or []:
        if not isinstance(entry, dict) or entry.get("page_limited") is not None or entry.get("page_drew") is not None:
            continue
        if (span := _window(entry.get("t"))) is None:
            continue
        start, end = span
        hit = next(((a, b) for a, b, _hidden in probes if a < end and b > start), None)
        if hit is None:
            continue
        entry["recorder_probe"] = [round(hit[0], 3), round(hit[1], 3)]
        if any(hidden and a <= start + 1e-6 and end <= b + 1e-6 for a, b, hidden in probes):
            entry["probe_hidden"] = True
        if (held := held_back_over(held_back, start, end)) is not None:
            entry["quota_held_back"] = held


def probe_kept(entry: Any) -> bool:
    """Whether a span the recorder's probing drew (:func:`mark_probes`) is kept under ``fps_fallback: refuse``: its
    frames never shown, or the worker's quota measured and holding nothing back there (page-limited, #11063)."""
    if not isinstance(entry, dict) or not entry.get("recorder_probe"):
        return False
    if entry.get("probe_hidden"):
        return True
    held = entry.get("quota_held_back")
    return isinstance(held, int | float) and float(held) <= 0.0 and not entry.get("starved_window")


def _window(value: Any) -> tuple[float, float] | None:
    try:
        return float(value[0]), float(value[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None


def starved_window(starved: Any, start: float, end: float) -> list[float] | None:
    """The first window the capture was starved in (``insert_rate.starved``: ``[start, end, fps]`` spans,
    ``insert_floor.classify``) that overlaps ``[start, end]``, as ``[start, end]``; ``None`` where none does or the take
    did not tell starved seconds from idle ones (#11021 retest)."""
    for span in starved if isinstance(starved, list) else []:
        if (window := _window(span)) is not None and window[0] < end and window[1] > start:
            return [round(window[0], 2), round(window[1], 2)]
    return None


def mark_starved(spans: Any, starved: Any) -> None:
    """Each span of *spans* (a stretch, a scroll, a transition: ``{t, ...}``) that overlaps a window the capture was
    starved in told it (``starved_window``, :func:`starved_window`), in place: refused as starved, never page-limited."""
    for entry in spans or []:
        if not isinstance(entry, dict) or (span := _window(entry.get("t"))) is None:
            continue
        if (window := starved_window(starved, span[0], span[1])) is not None:
            entry["starved_window"] = window


def held_back_over(held_back: Any, start: float, end: float) -> float | None:
    """The seconds the worker's CPU quota held it back over ``[start, end]`` (``alone.held_back``,
    ``canvas_alone.Tap.sample_held_back``: ``[start, end, seconds]`` spans, each counted in proportion of its overlap);
    ``None`` where the take did not measure it."""
    if not isinstance(held_back, list):
        return None
    total = 0.0
    for span in held_back:
        try:
            a, b, seconds = float(span[0]), float(span[1]), float(span[2])
        except (TypeError, ValueError, IndexError):
            continue
        overlap = min(b, end) - max(a, start)
        if overlap > 0 and b > a:
            total += seconds * min(1.0, overlap / (b - a))
    return round(total, 3)


#: Seconds under which a motion the screencast delivered under the take's rate, its worker's quota holding nothing
#: back over it, is page-limited advice rather than a refusal (#11355: a 0.2 s svg transition at 10 fps refused a 51 s
#: take): too brief for a count of the page's own frames, the quota's read is the proof.
BRIEF_SECONDS = 0.3


def page_rate(entry: Any) -> float | None:
    """The page's own rate over a span kept at it (:func:`page_limited_kept`): its counted frames' (``page_limited``),
    else, for a brief span, the screencast's (``screencast_rate``, #11355)."""
    if not isinstance(entry, dict):
        return None
    for key in ("page_limited", "screencast_rate"):
        if isinstance(entry.get(key), int | float):
            return float(entry[key])
    return None


def brief_at_screencast_rate(entry: Any) -> bool:
    """Whether a span is told page-limited by the quota's read alone (``screencast_rate``, :func:`mark_unclassified`)
    and lasts under :data:`BRIEF_SECONDS`: a click's transition the page itself delivered under the rate (#11355)."""
    if not isinstance(entry, dict) or not isinstance(entry.get("screencast_rate"), int | float):
        return False
    if (span := _window(entry.get("t"))) is None:
        return False
    return span[1] - span[0] < BRIEF_SECONDS


def page_limited_kept(entry: Any) -> bool:
    """Whether a stretch or a paced transition is the page's own rate, kept under ``fps_fallback: refuse`` (Sam's
    decision on #10970): the page drew it under the take's rate (``page_limited``, or a span under
    :data:`BRIEF_SECONDS` the screencast delivered under it, ``screencast_rate``, #11355), every frame it drew kept,
    held, never blended, and the worker's quota held nothing back over it (``quota_held_back`` measured and 0). A span
    the quota starved, one overlapping a window the capture was starved in (``starved_window``, #11021 retest), or one
    whose hold-back was not measured, is no proof the page was slow on its own."""
    return (
        isinstance(entry, dict)
        and (isinstance(entry.get("page_limited"), int | float) or brief_at_screencast_rate(entry))
        and isinstance(entry.get("quota_held_back"), int | float)
        and float(entry["quota_held_back"]) <= 0.0
        and not entry.get("starved_window")
        # A scroll or a zoom paced from keyframes drew the frames between the page's own: kept only when held.
        and ("scroller" not in entry or bool(entry.get("held_not_blended")))
    )


def mark_quota(
    events: Sequence[dict[str, Any]] | None,
    held_back: Any,
    *,
    canvas_runs: Sequence[dict[str, Any]] | None = None,
    fps: float | None = None,
) -> None:
    """Each paced scroll and transition of *events* the page itself drew under its rate (``page_limited``) told the
    seconds the worker's quota held it back over it (``quota_held_back``, :func:`held_back_over`), in place. A paced
    scroll (a canvas wheel zoom, a window scroll) is classified first, as a transition is when it is paced
    (#11021 retest: step 16's canvas zoom, copied 8 of 8 drawn, was said unmeasured): over a canvas by its copy
    (*canvas_runs*, :func:`canvas_limited`), elsewhere by the page's frames (:func:`page_limited_fps`) then its copy."""
    page = page_frame_times(events)
    rate = float(fps or 0.0)
    for event in events or []:
        if not isinstance(event, dict):
            continue
        for entry in event.get("scrolls") or []:
            if isinstance(entry, dict) and rate > 0:
                _mark_scroll(entry, page, canvas_runs or (), rate)
        for entry in [*(event.get("scrolls") or []), *(event.get("motion") or [])]:
            if not isinstance(entry, dict) or (entry.get("page_limited") is None and entry.get("page_drew") is None):
                continue
            try:
                start, end = float(entry["t"][0]), float(entry["t"][1])
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            if (held := held_back_over(held_back, start, end)) is not None:
                entry["quota_held_back"] = held
        # What no count of the page's frames classified, drawn under the recorder's own probing (#11063).
        mark_probes([*(event.get("scrolls") or []), *(event.get("motion") or [])], events, held_back)
        # What neither classified: starved or page-limited by the quota's read, never unmeasured (#11097).
        mark_unclassified([*(event.get("scrolls") or []), *(event.get("motion") or [])], held_back)


#: Seconds before a span the worker's quota holding it back still starves it: a click's transition starts on frames
#: the worker owed the step before (#11097, step 1's svg transition right after a held-back second).
QUOTA_LEAD = 0.5


def mark_unclassified(spans: Any, held_back: Any) -> None:
    """Each span of *spans* (a stretch, a scroll, a transition: ``{t, ...}``) that no count of the page's frames and no
    recorder probe classified told, in place (#11097: a click transition and a wheel zoom said unmeasured):
    ``quota_held_back`` the seconds the worker's quota held it back over it or :data:`QUOTA_LEAD` before it (starved
    where it did), else ``screencast_rate`` the rate the screencast delivered it at (page-limited). Nothing where the
    take did not read the quota: the cause stays unmeasured. Never kept by it: only its label changes."""
    for entry in spans or []:
        if not isinstance(entry, dict) or _classified_span(entry):
            continue
        if (span := _window(entry.get("t"))) is None:
            continue
        held = held_back_over(held_back, span[0] - QUOTA_LEAD, span[1])
        if held is None:
            continue
        entry["quota_held_back"] = held
        if held <= 0.0 and (rate := _delivered(entry, span)) is not None:
            entry["screencast_rate"] = rate


def _classified_span(entry: dict) -> bool:
    return any(entry.get(key) is not None for key in ("page_limited", "page_drew", "recorder_probe", "quota_held_back"))


def _delivered(entry: dict, span: tuple[float, float]) -> float | None:
    """The frames a second the screencast delivered a span at: a paced entry's ``painted``, a stretch's frames not
    synthesised over its seconds."""
    if isinstance(entry.get("painted"), int | float):
        return round(float(entry["painted"]), 2)
    if isinstance(entry.get("frames"), int):
        seconds = max(span[1] - span[0], 1e-6)
        return round(float(max(0, int(entry["frames"]) - int(entry.get("synthesised") or 0))) / seconds, 2)
    return None


def _mark_scroll(
    entry: dict[str, Any], page: list[float] | None, canvas_runs: Sequence[dict[str, Any]], fps: float
) -> None:
    """A paced scroll told ``page_limited`` or ``page_drew`` where the page's own frames say so (:func:`mark_quota`)."""
    if not entry.get("paced") or entry.get("page_limited") is not None or entry.get("page_drew") is not None:
        return
    if (span := _window(entry.get("t"))) is None or not isinstance(entry.get("painted"), int | float):
        return
    start, end, painted = span[0], span[1], float(entry["painted"])
    # The page's frame loop runs at the display's rate whatever its canvas draws at: a canvas is told by its copy.
    drawn = None if entry.get("scroller") == "canvas" else page_limited_fps(page, start, end, painted=painted, fps=fps)
    page_drew = None
    if drawn is None:
        # A canvas scroll over a run composited into the take plays its copies: their count tells it (#11097).
        drawn, page_drew = canvas_limited(
            canvas_runs, start, end, painted=painted, fps=fps, composited=entry.get("scroller") == "canvas"
        )
    if drawn is not None:
        entry["page_limited"] = drawn
    elif page_drew is not None:
        entry["page_drew"] = page_drew


def synthesised_transitions(events: Sequence[dict] | None, *, fps: float) -> list[dict]:
    """The paced scrolls and transitions of a take whose frames at its rate *fps* were more than
    :data:`SYNTHESISED_SHARE_MAX` drawn from keyframes rather than captured: ``{step, t, painted, share, held}``, one
    each - ``share`` the frames not painted by the page (``1 - painted / fps``), ``held`` the frames held, not
    blended (a discrete change cut in, or a zoom whose keyframes were not registered). A transition held then cut is
    said whatever its painted rate. A discrete change cut with no state between (``discrete``,
    ``transition_pacing.discrete_cut``: an icon swapped) moved nothing between its two pictures: never said. Nor is a
    transition the page itself drew under the rate, held at its own frames, its quota holding nothing back there
    (:func:`page_limited_kept`, #10970): page-limited, no recorder could capture more."""
    found: list[dict] = []
    rate = float(fps or 0.0)
    if rate <= 0:
        return found
    for event in events or []:
        if not isinstance(event, dict):
            continue
        for entry in [*(event.get("scrolls") or []), *(event.get("motion") or [])]:
            if not (isinstance(entry, dict) and entry.get("paced") and counts(entry)) or entry.get("discrete"):
                continue
            if one_repaint(entry) or page_limited_kept(entry) or probe_kept(entry):
                continue
            painted = float(entry.get("painted") or 0.0)
            share = round(max(0.0, 1.0 - painted / rate), 4)
            held = int(entry.get("held") or 0) if entry.get("held_not_blended") or entry.get("outran") else 0
            if share <= SYNTHESISED_SHARE_MAX and not held:
                continue
            span = entry.get("t")
            try:
                start, end = (
                    (float(span[0]), float(span[1]))
                    if isinstance(span, list | tuple) and len(span) == 2
                    else (0.0, 0.0)
                )
            except (TypeError, ValueError):
                start, end = 0.0, 0.0
            found.append(
                {
                    "step": event.get("step"),
                    "t": [round(start, 3), round(end, 3)],
                    "painted": round(painted, 1),
                    "share": round(share, 2),
                    "held": held,
                    "canvas": entry.get("scroller") == "canvas",
                    "cause": refused_cause(entry),
                }
            )
    return found


def synthesised_words(found: Sequence[dict]) -> str:
    """``step 6 at 16.80-17.40 s painted at 4.0 fps (93% of its frames synthesised, 12 held then cut)``, each."""
    said = []
    for item in found:
        held = f", {item['held']} held then cut" if item.get("held") else ""
        cause = f"; {item['cause']}" if item.get("cause") else ""
        said.append(
            f"step {item.get('step')} at {item['t'][0]:.2f}-{item['t'][1]:.2f} s painted at {item['painted']:g} fps "
            f"({round(item['share'] * 100)}% of its frames synthesised{held}{cause})"
        )
    return "; ".join(said)


def counts(entry: Any) -> bool:
    """Whether a scroll or a transition a step keeps counts in its rates: paced or not, something moved on screen."""
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("fps"), int | float)
        and entry.get("why") not in UNSEEN
        # A discrete change cut with no state between has no motion to paint: no rate of it counts.
        and not entry.get("discrete")
    )


#: How a step's summary names what a scroll moved when the page named nothing (:func:`summarise`).
_SCROLLED = {"window": "window", "element": "a scroll container", "canvas": "a canvas"}


def summarise(event: dict) -> None:
    """A step's rates and what it scrolled, from its scrolls and transitions as paced: ``painted_fps`` the
    lowest rate its keyframes were painted at where something moved, ``fps`` the lowest it plays at, and
    ``scrolled_element`` the element it scrolled most - ``window`` for the page itself; ``scroll_cadence`` the least
    even :func:`cadence` of its scrolls, ``stepping`` where it steps on screen."""
    entries = [entry for entry in [*(event.get("scrolls") or []), *(event.get("motion") or [])] if counts(entry)]
    steadies = [
        s["cadence"] for s in event.get("scrolls") or [] if isinstance(s, dict) and isinstance(s.get("cadence"), dict)
    ]
    if steadies:
        event["scroll_cadence"] = max(steadies, key=lambda c: float(c["stdev"]) / max(float(c["mean"]), 1e-6))
    if entries:
        event["painted_fps"] = round(min(float(entry.get("painted") or 0.0) for entry in entries), 2)
        event["fps"] = round(min(float(entry["fps"]) for entry in entries), 2)
    scrolled = [scroll for scroll in event.get("scrolls") or [] if counts(scroll) and scroll.get("log")]
    if scrolled:
        most = max(
            scrolled,
            key=lambda scroll: max(value for _t, value in scroll["log"]) - min(value for _t, value in scroll["log"]),
        )
        event["scrolled_element"] = most.get("element") or _SCROLLED.get(
            str(most.get("scroller")), "a scroll container"
        )


# ── The rate a take plays at ──────────────────────────────────────────────


def _rate(fps: float) -> str:
    return f"{round(fps, 1):g}"


@dataclass(frozen=True)
class TakeRate:
    """A take's real frame rate (:func:`take_rate`): the lowest a step plays at, and that step - else its paint rate."""

    fps: float
    #: Whether it scrolls, and whether it has transitions: its rate is what they play at.
    scrolls: bool
    #: Its motion was drawn again at its rate (:func:`pace`) ...
    paced: bool = False
    #: ... with this many frames a second synthesised between keyframes, where its keyframes were fewest.
    synthesised: float = 0.0
    transitions: bool = False
    #: The step that plays slowest, its action, and what moved there: ``scroll`` or ``transition``.
    step: int | None = None
    action: str | None = None
    moving: str = "scroll"
    #: Why that step was not drawn again at its rate, and the seconds its scroll takes for every two paints to overlap.
    why: str | None = None
    seconds: float | None = None

    @property
    def steps(self) -> bool:
        """Its scrolls or its transitions step on screen: under :data:`MIN_TAKE_FPS`."""
        return (self.scrolls or self.transitions) and self.fps < MIN_TAKE_FPS

    def words(self) -> str:
        """``scrolls at 30 fps (24 frames a second synthesised between keyframes)``, or ``2.1 fps painted``."""
        if not (self.scrolls or self.transitions):
            return f"{_rate(self.fps)} fps painted"
        what = (
            "scrolls and transitions"
            if self.scrolls and self.transitions
            else "scrolls"
            if self.scrolls
            else "transitions"
        )
        said = f"{what} at {_rate(self.fps)} fps"
        if self.paced and self.synthesised >= 1:
            said += f" ({self.synthesised:.0f} frames a second synthesised between keyframes)"
        return said

    def where(self) -> str:
        """``step 3 (scroll)``: the step that plays slowest; ``its scroll steps`` for a take that names none."""
        return f"step {self.step} ({self.action})" if self.step is not None else "its scroll steps"

    def stepping(self) -> str:
        """What steps on screen there: ``scrolls``, or ``transitions``."""
        return "transitions" if self.moving == "transition" else "scrolls"

    def remedy(self) -> str:
        """What to do about a take that steps (:attr:`steps`): the step's ``seconds`` for a scroll that outran its
        paints, why it was not drawn again, else to record it again at its rate."""
        if self.seconds is not None and self.step is not None:
            return f"record it again with seconds: {self.seconds:g} on step {self.step} (its scroll outran the page's paints)"
        if self.why and self.step is not None:
            return f"step {self.step} was not drawn again at its rate ({self.why}): record it again"
        # The recorder's own fix: it measures the worker before step 1 and records at a ratio it sustains.
        return (
            f"record it again at {DEFAULT_TAKE_FPS} fps (record_browser_demo draws its scrolls again at its fps, and "
            "measures the worker's capture rate before step 1, recording at a pixel ratio it sustains)"
        )


def take_rate(events: Sequence[dict] | None, *, captured_frames: int | None, duration: float) -> TakeRate:
    """The real frame rate of a take of *events*, *captured_frames* painted over *duration* seconds: the lowest rate a
    step plays at - each scroll's (a canvas's zoom included) and each transition's ``fps`` - and that step.
    What moved nothing on screen counts in no rate (:data:`UNSEEN`: a wheel that moved nothing on the page, an
    element brought into view that was already there); a ``scroll`` step whose scroll did not move before a canvas
    was looked for (:data:`DID_NOT_MOVE`: it may have zoomed one) plays at the frames the take painted over its
    length, and so does a take recorded before its scrolls were logged, a scroll told by a ``scroll`` step or a step
    that ``scrolled``."""
    painted = (captured_frames or 0) / duration if duration > 0 else 0.0
    #: ``(fps, its event, its entry, scroll | transition)``: what each step plays at.
    rates: list[tuple[float, dict, dict | None, str]] = []
    paced, logged, kinds, synthesised = False, False, set(), 0.0
    told: list[dict] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        if event.get("action") == "scroll" or event.get("scrolled"):
            told.append(event)
        scrolls = [
            s for s in event.get("scrolls") or [] if isinstance(s, dict) and isinstance(s.get("fps"), int | float)
        ]
        for moving, entries in (
            ("scroll", scrolls),
            ("transition", [m for m in event.get("motion") or [] if isinstance(m, dict)]),
        ):
            for entry in entries:
                logged = True
                if not counts(entry):
                    continue
                kinds.add(moving)
                rates.append((float(entry["fps"]), event, entry, moving))
                if entry.get("paced"):
                    paced = True
                    synthesised = max(synthesised, float(entry["fps"]) - float(entry.get("painted") or 0.0))
        if (
            event.get("action") == "scroll"
            and scrolls
            and not any(counts(s) for s in scrolls)
            and not any(s.get("why") == NOTHING_MOVED for s in scrolls)
        ):
            kinds.add("scroll")
            rates.append((painted, event, None, "scroll"))
    if not logged and told:
        kinds.add("scroll")
        rates.extend((painted, event, None, "scroll") for event in told)
    if not rates:
        return TakeRate(round(painted, 1), False)
    fps, event, entry, moving = min(rates, key=lambda rate: rate[0])
    return TakeRate(
        round(fps, 1),
        "scroll" in kinds,
        paced,
        round(synthesised),
        "transition" in kinds,
        step=event.get("step") if isinstance(event.get("step"), int) else None,
        action=event.get("action"),
        moving=moving,
        why=entry.get("why")
        if entry is not None and not entry.get("paced") and entry.get("why") != PAINTED_AT_RATE
        else None,
        seconds=entry.get("seconds") if entry is not None else None,
    )


#: What a step's scroll moved, as its take's answer says it when the page named nothing (:func:`scroll_steps_words`).
_MOVED = {"window": "scrolled the window", "element": "scrolled a scroll container", "canvas": "zoomed a canvas"}


def _how(entry: dict, painted: float) -> str:
    """How a scroll or a transition plays (:func:`scroll_steps_words`): drawn again, kept as painted, or why not."""
    why = entry.get("why")
    if entry.get("discrete"):
        held = int(entry.get("held") or 0)
        how = f"discrete, cut: one picture before, one after, nothing between ({held} frame(s) held, not blended)"
        # Keyframes the screencast painted mid-fade inside the window: dropped, never shown, and said.
        if isinstance(blends := entry.get("painted_blends"), list) and blends:
            how += f", {len(blends)} keyframe(s) painted mid-fade dropped (at {', '.join(f'{float(t):g}' for t in blends)} s)"
        return how
    if entry.get("paced"):
        how = f"drawn again from its keyframes at {float(entry.get('fps') or 0):g} fps"
        # What it held as painted: the sticky bands inside its scroller, the boxes over its canvas.
        if pinned := entry.get("pinned"):
            bands = ", ".join(f"{int(top)}-{int(top) + int(height)} px" for top, height in pinned[:4])
            how += f", {len(pinned)} sticky band(s) held as painted ({bands})"
        # What it refused to hold, and a band held unlike the page's own paint of it.
        if rejected := entry.get("rejected"):
            how += f", {len(rejected)} band candidate(s) refused ({rejected[0][2]})"
        if repaint := entry.get("band_repaint"):
            spans = ", ".join(f"{int(top)}-{int(top) + int(height)} px" for top, height, _level in repaint[:4])
            how += f", band repaint: {len(repaint)} held band(s) unlike the page's own paint ({spans})"
        # A held band that moved and came back within a few frames: a sticky header blinking, by its frames.
        if blink := entry.get("band_blink"):
            at = ", ".join(f"{float(t):.2f} s" for t in blink[:4])
            how += f", band blink: a sticky band moved and came back on {len(blink)} frame(s) ({at}): record it again"
        if overlays := entry.get("overlays"):
            # A box standing still on every keyframe (a toolbar, a legend and its rows) is held as it stands, which is
            # right; "held as painted" names only the boxes that changed without the zoom carrying them.
            still = min(int(overlays), int(entry.get("overlays_still") or 0))
            if still:
                how += f", {still} box(es) standing still over its canvas kept in place"
            if int(overlays) > still:
                how += f", {int(overlays) - still} box(es) over its canvas held as painted"
        if appeared := entry.get("appeared"):
            how += f", {int(appeared)} box(es) opened over its canvas during the zoom held as painted"
        if riding := entry.get("riding"):
            how += f", {int(riding)} box(es) over its canvas moving with it drawn with the canvas"
            # Read riding on another wheel step of the take: drawn with the canvas on every step.
            if carried := entry.get("carried"):
                how += f" ({int(carried)} as on the take's other zooms of it)"
        # Pairs of keyframes registered once what the cursor lights over the canvas was left out.
        if entry.get("scroller") == "canvas" and (hovered := entry.get("hovered")):
            how += f", {int(hovered)} pair(s) of keyframes registered without what the cursor lit"
        # Pairs of keyframes registered from the zoom the canvas's log implies, their pixels alone not telling.
        if entry.get("scroller") == "canvas" and (implied := entry.get("implied")):
            how += f", {int(implied)} pair(s) of keyframes registered from the canvas's log"
        # A canvas zoom's frames whose keyframes were not registered: the nearer one held, never two blended.
        if entry.get("scroller") == "canvas" and entry.get("held_not_blended"):
            how += f", {int(entry.get('held') or 0)} frame(s) held, not blended (keyframes not registered"
            reasons = entry.get("held_why")
            if isinstance(reasons, dict) and reasons:
                how += ": " + ", ".join(
                    f"{int(count)}{'' if reason == 'uncovered' else ' pair(s)'} {HELD_WHY.get(str(reason), str(reason))}"
                    for reason, count in sorted(reasons.items())
                )
            if isinstance(share := entry.get("held_explained"), int | float):
                how += (
                    f"; the best zoom tried explains {round(float(share) * 100)}% of what tells them apart,"
                    f" {round(ZOOM_EXPLAINS * 100)}% needed"
                )
            if isinstance(parts := entry.get("held_left_parts"), int) and parts > 0:
                how += f"; what it leaves sits in {parts} of {LEFT_PARTS * LEFT_PARTS} parts of the canvas"
            how += ")"
        # A transition whose keyframes differ by a discrete change (a label swapped): the earlier held, the later cut in.
        if "scroller" not in entry and entry.get("held_not_blended"):
            how += f", {int(entry.get('held') or 0)} frame(s) held, not blended (a discrete change: the earlier keyframe held, the later cut in)"
        # Keyframes the screencast painted mid-fade inside such a window: dropped, never shown.
        if "scroller" not in entry and isinstance(blends := entry.get("painted_blends"), list) and blends:
            how += f", {len(blends)} keyframe(s) painted mid-fade dropped (at {', '.join(f'{float(t):g}' for t in blends)} s)"
        if entry.get("outran"):
            how += f", {int(entry.get('held') or 0)} frames held where it outran the page's paints (seconds: {float(entry['seconds']):g})"
        return how
    if why == PAINTED_AT_RATE:
        return "kept as painted, at its rate"
    return f"not drawn again ({why or 'it could not be paced'})" + (": it steps" if painted < MIN_TAKE_FPS else "")


def scroll_steps_words(events: Sequence[dict] | None) -> str:
    """What a take's answer says of each step that scrolled and of each whose transitions moved: what
    it moved - the element the page names, for a scroll container -, the rate its keyframes were painted at and
    whether it was drawn again from them - ``Scroll steps: step 2 (scroll) scrolled main#panel: painted at 2.4 fps,
    drawn again from its keyframes at 30 fps. Transition steps: step 3 (hover) moved div#tip: painted at 0.0 fps,
    drawn again from its keyframes at 30 fps.`` -, so a step that steps is seen before the take is placed. Empty when
    no step logged either."""
    scrolled, moved = [], []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        step = f"step {event.get('step')} ({event.get('action')})"
        for scroll in event.get("scrolls") or []:
            if not isinstance(scroll, dict) or not isinstance(scroll.get("painted"), int | float):
                continue
            if scroll.get("why") in (DID_NOT_MOVE, NOTHING_MOVED):
                scrolled.append(f"{step} moved nothing")
                continue
            painted = float(scroll["painted"])
            what = (
                f"scrolled {scroll['element']}"
                if scroll.get("element")
                else _MOVED.get(str(scroll.get("scroller")), "scrolled")
            )
            scrolled.append(f"{step} {what}: painted at {painted:.1f} fps, {_how(scroll, painted)}")
        for window in event.get("motion") or []:
            # A discrete cut counts in no rate, but is said: the reader sees why it was neither synthesised nor refused.
            if not counts(window) and not (isinstance(window, dict) and window.get("discrete")):
                continue
            painted = float(window.get("painted") or 0.0)
            what = f"moved {window['element']}" if window.get("element") else "moved the page"
            moved.append(f"{step} {what}: painted at {painted:.1f} fps, {_how(window, painted)}")
    said = [
        f"Scroll steps: {'; '.join(scrolled)}." if scrolled else "",
        f"Transition steps: {'; '.join(moved)}." if moved else "",
    ]
    return " ".join(part for part in said if part)
