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

"""Whether what a screen insert shows can be read where it is drawn, measured when the video is saved.

Every alignment, key and colour gate passed on a wall of screens nobody could
read: each pane drew a 3200 px recording a few hundred pixels wide. Whether an
insert is legible is the size its source's text is drawn at, and that is two
numbers the save already has or can read:

* **How large the source is drawn.** Its quad (each pane's on a wall) on the
  1080p frame of the video's shape, the footage covering the frame, the
  source fitted into it as the render fits it (``screen_insert.density``):
  ``scale`` render pixels per source pixel.
* **How large its text is.** The smallest text of one frame of the source
  (:func:`text_size`), read off the picture alone - a recording, a shot, an
  uploaded screenshot or video alike: its text lines, their cap height over
  :data:`CAP_EM`, the share of a font's size a capital stands in every UI face.

Drawn under :data:`MIN_TEXT_PX` on the 1080p frame - at the peak of the
insert's own push (``zoom``) -, the insert is said (``insert_legibility``,
advisory: it never blocks a render); under :data:`ILLEGIBLE_TEXT_PX`, it is a
finding the render waits for: no viewer reads it. Both offer only
what keeps the page's layout, each bringing its smallest text to
:data:`TARGET_TEXT_PX`: the share of the frame its quad needs, a push on the
insert (``zoom: {start, end, scale}``) with what keeps that push sharp - a
recording at a higher pixel ratio, a larger picture -, and a recording laid
out narrower - on a quad under :data:`NARROW_SHARE` of the frame's width a
higher browser zoom (``spec.TakeScript.browser_zoom``), its window and frame
keeping their size, else a narrower viewport - only down to the width the
recorder saw its page keep its layout (``VideoTake.layout``,
``capture.probe_layout``): a 1600 px dashboard told to record at 548 px came
back as its mobile layout, stacked and cropped on a wall screen, and a
browser zoom lays the page out just as narrow. How far the source is scaled
down is said with it, never judged alone: a recording at pixel ratio 2 is
drawn under half its size by design, and stays legible when its text
is large enough. A source drawn under :data:`MIN_PUSH_SCALE` shows as a whole
page - a dashboard on a wall screen -, which a push crops: it is offered no
push, and the advice says why. Every advice offers ``legibility:
ambient`` for an insert that is scenery (:data:`AMBIENT_OFFER`). A save says
the advisory once per insert, a wall's panes together (:func:`said_once`),
never on every save; the finding, every time.

What draws the source larger - a larger surface, a push, a narrower window -
is offered only at a scale the source carries at the resolution the video's
renders are made at (``render_checks.target_quality``): at least
``screen_insert.MIN_DENSITY`` of its pixels per output pixel, what the soft
gate holds a render to. A scale it does not carry is offered with what makes a
source carry it - a higher pixel ratio, a larger picture -, else, for a
recording, a higher browser zoom where its layout holds - its text larger in
the same pixels - and the push left after it, else the largest push it carries;
never a push the gate refuses. A push sharp at that quality that an ultra
render refuses says so.

A larger surface is offered only where the scene has it: the
surfaces side by side with an insert at the same time - the other inserts of
its scene, each as wide as its own advice asks - share the frame's width with
it, and two monitors on one wall cannot each take 68% of it. Where they sum
past the frame's width, the advice says so and offers what needs no more of
the frame: a push, a recording made again at a higher browser zoom up to the
largest its page keeps its layout at (the window over the width the probe saw
it hold down to, :attr:`Legibility.largest_zoom`), or a tighter page - a
single panel of it. A wall's own panes are left out of it: fewer panes gives
each the room the others leave.

Only the sources made to be read are judged (:data:`READ_KINDS`): a recording
and an uploaded picture or video. An AI shot is generated without readable
text, a brand mark is a logo. An insert declared ``legibility: ambient`` is
scenery - three monitors sharing one frame, each recording drawn at 0.2x -:
its text is read and never judged, and the check says its size as a fact
(:func:`unchecked`: ``ambient, not judged - its smallest text ... reads about
5 px``), never as a finding. A source is read once per file and
second (``render_cache``, :data:`CACHE_KIND`); one that cannot be read (a file
gone, a decoder failing, past :data:`MEASURE_SECONDS`) is not judged, and never
fails a save.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import math
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from app.core.utils import format_exception_message
from app.services.video_studio.spec import (
    DEFAULT_QUALITY,
    MAX_BROWSER_ZOOM,
    MAX_INSERT_ZOOM,
    MIN_VIEWPORT_WIDTH,
    QUALITY_PRESETS,
)

logger = logging.getLogger(__name__)

#: Text drawn under this many pixels of a video's 1080p frame (a font's size, as CSS gives it) is said, advisory
#: ...
MIN_TEXT_PX = 10.0
#: ... and under this, a finding the render waits for: no viewer reads it ...
ILLEGIBLE_TEXT_PX = 6.0
#: ... both told what draws it this large and keeps the page's layout (``Legibility.fix``).
TARGET_TEXT_PX = 12.0
#: The short side of the frame legibility is judged on: the size a viewer watches, whatever the render's quality.
REFERENCE_SHORT_SIDE = 1080
#: A capital's height, a share of its font's size: Inter 0.73, Roboto 0.71, Arial 0.72, Segoe UI 0.70, SF 0.71.
CAP_EM = 0.72
#: The side of the tiles the picture is cut into to tell ink from paper, and the least spread of grey in a tile
#: that holds any text at all (text is drawn to be read: well above it).
_TILE = 64
_MIN_SPREAD = 48.0
#: A glyph's height, in pixels of the source: under it, a dot or a rule; over it, a picture.
_MIN_GLYPH = 4
_MAX_GLYPH = 160
#: The least glyphs a text line holds, and the least lines a picture shows to be judged.
_MIN_LINE_GLYPHS = 4
_MIN_LINES = 3
#: The smallest text is the text of the smallest tenth of the glyphs read, never one stray line.
_SMALLEST_SHARE = 0.1
#: The second of a video source read after its start: past a first frame that is often still loading.
_INTO_SOURCE = 1.0
#: The longest the reads of one save take, every source together: past it, the rest are not judged.
MEASURE_SECONDS = 30.0
CACHE_KIND = "insert-legibility"
MEASURE_VERSION = 1
#: The sources whose text is there to be read: a recording, an uploaded picture or video.
READ_KINDS = ("take", "file")
#: Under this share of the frame's width, a recording is told the browser zoom that brings its text there, never a
#: viewport: one that narrow would make it a tall strip on a wide screen, where a zoom keeps its window and frame.
NARROW_SHARE = 0.4
#: How a screen insert says it is scenery, never read (``spec.INSERT_LEGIBILITY``).
AMBIENT = "ambient"


def ambient(overlay: Any) -> bool:
    """Whether *overlay* (a screen insert) is declared scenery: its text is never judged."""
    return getattr(overlay, "legibility", None) == AMBIENT


@dataclass(frozen=True)
class TextSize:
    """The text a picture shows: the size of its smallest text, in its own pixels, and how many lines it was read on."""

    px: float
    lines: int

    def as_dict(self) -> dict:
        return {"px": round(self.px, 2), "lines": self.lines}

    @classmethod
    def from_dict(cls, data: dict) -> TextSize:
        return cls(float(data["px"]), int(data["lines"]))


def _ink(grey: np.ndarray) -> np.ndarray:
    """Where *grey* (``H x W``, 0-255) draws text strokes: in each tile with contrast, the side of its middle grey
    that covers less of it - dark text on a light panel, light text on a dark one."""
    height, width = grey.shape
    padded = np.pad(grey, ((0, -height % _TILE), (0, -width % _TILE)), mode="edge")
    rows, cols = padded.shape[0] // _TILE, padded.shape[1] // _TILE
    tiles = padded.reshape(rows, _TILE, cols, _TILE).swapaxes(1, 2)
    flat = tiles.reshape(rows, cols, _TILE * _TILE)
    low, high = np.percentile(flat, 2, axis=2), np.percentile(flat, 98, axis=2)
    dark = tiles < ((low + high) / 2)[..., None, None]
    ink = np.where((dark.mean(axis=(2, 3)) < 0.5)[..., None, None], dark, ~dark)
    ink &= ((high - low) >= _MIN_SPREAD)[..., None, None]
    return ink.swapaxes(1, 2).reshape(padded.shape)[:height, :width]


def _glyphs(ink: np.ndarray) -> np.ndarray:
    """``N x 4`` of the glyph-like strokes of *ink*: top, bottom, left, right (bottom and right exclusive).

    A glyph is a connected stroke between :data:`_MIN_GLYPH` and :data:`_MAX_GLYPH` tall, no wider than a few
    letters run together, neither a filled box nor a frame - except a single upright stroke (``l``, ``I``, ``1``).
    """
    from scipy import ndimage

    labels, count = ndimage.label(ink, structure=np.ones((3, 3), dtype=bool))
    if not count:
        return np.zeros((0, 4), dtype=np.int64)
    areas = np.bincount(labels.ravel(), minlength=count + 1)
    boxes = []
    for index, found in enumerate(ndimage.find_objects(labels), start=1):
        if found is None:
            continue
        rows, cols = found
        height, width = rows.stop - rows.start, cols.stop - cols.start
        if not _MIN_GLYPH <= height <= _MAX_GLYPH or width > 2.5 * height:
            continue
        fill = areas[index] / float(height * width)
        if fill < 0.08 or (fill > 0.92 and width > 0.35 * height):
            continue
        boxes.append((rows.start, rows.stop, cols.start, cols.stop))
    return np.asarray(boxes, dtype=np.int64).reshape(-1, 4)


def _lines(boxes: np.ndarray) -> list[np.ndarray]:
    """The glyphs of *boxes* run into text lines: each glyph joined to the next one to its right within one and a
    half of its height that shares half the height of the shorter; lines of :data:`_MIN_LINE_GLYPHS` or more."""
    count = len(boxes)
    parent = list(range(count))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    order = np.argsort(boxes[:, 2], kind="stable")
    lefts = boxes[order, 2]
    for position, index in enumerate(order):
        top, bottom, _left, right = boxes[index]
        height = bottom - top
        reach = np.searchsorted(lefts, right + 1.5 * height, side="right")
        for other in order[position + 1 : reach]:
            o_top, o_bottom, o_left, _o_right = boxes[other]
            if o_left < right - 0.5 * height:
                continue
            shared = min(bottom, o_bottom) - max(top, o_top)
            if shared >= 0.5 * min(height, o_bottom - o_top):
                parent[root(other)] = root(index)
    groups: dict[int, list[int]] = {}
    for index in range(count):
        groups.setdefault(root(index), []).append(index)
    return [boxes[members] for members in groups.values() if len(members) >= _MIN_LINE_GLYPHS]


def text_size(pixels: np.ndarray) -> TextSize | None:
    """The size of the smallest text *pixels* (``H x W x 3``, RGB) shows, in its pixels; ``None`` for too little text.

    Each text line's cap height - from the baseline most of its glyphs stand on to the top of its tallest tenth -
    over :data:`CAP_EM`; the smallest text is the size of the smallest :data:`_SMALLEST_SHARE` of the glyphs, so one
    stray row of marks is never it. Fewer than :data:`_MIN_LINES` lines: not judged.
    """
    rgb = np.asarray(pixels, dtype=np.float32)
    grey = rgb[..., :3] @ np.array([0.299, 0.587, 0.114], dtype=np.float32) if rgb.ndim == 3 else rgb
    lines = _lines(_glyphs(_ink(grey)))
    if len(lines) < _MIN_LINES:
        return None
    sizes, weights = [], []
    for line in lines:
        baseline = float(np.median(line[:, 1]))
        cap = baseline - float(np.percentile(line[:, 0], 10))
        if cap < _MIN_GLYPH:
            continue
        sizes.append(cap / CAP_EM)
        weights.append(len(line))
    if len(sizes) < _MIN_LINES:
        return None
    order = np.argsort(sizes)
    ranked, counted = np.asarray(sizes)[order], np.cumsum(np.asarray(weights)[order])
    smallest = ranked[int(np.searchsorted(counted, _SMALLEST_SHARE * counted[-1]))]
    return TextSize(float(smallest), len(sizes))


#: The pixel ratios a recording is made at (``spec.TakeScript.pixel_ratio``).
RECORD_RATIOS = (1.0, 1.5, 2.0)
#: The push on an insert is advised to a tenth.
_PUSH_STEP = 10
#: Drawn under this scale (render pixels of the 1080p frame per source pixel), a source shows as a whole page - a
#: dashboard on a wall screen - and a push crops it: no push is offered, the advice says why.
MIN_PUSH_SCALE = 0.3
#: What every advice ends with: an insert nobody is meant to read is declared so, once, and never judged again.
AMBIENT_OFFER = (
    'scenery nobody is meant to read: declare legibility: "ambient" on the insert, once, and the save no longer judges it'
)


def _short_side(quality: str) -> int:
    preset = QUALITY_PRESETS.get(quality)
    return preset.short_side if preset is not None else REFERENCE_SHORT_SIDE


def render_words(quality: str) -> str:
    """``an ultra render (2160p)``: a render at *quality*, as the advice names it."""
    article = "an" if quality[:1] in "aeiou" else "a"
    return f"{article} {quality} render ({_short_side(quality)}p)"


@dataclass(frozen=True)
class Legibility:
    """One surface of a screen insert (a pane of a wall included), its source's text as drawn on the 1080p frame."""

    #: Its :func:`timeline.insert_key`, and what it shows: ``("take" | "file", id)``.
    key: str
    source: tuple[str, str]
    #: Its source's size in pixels, and the width of the quad it is drawn on, on the 1080p frame.
    source_size: tuple[int, int]
    quad_width: float
    #: Render pixels of the 1080p frame per source pixel, its fit applied (``screen_insert.density``).
    scale: float
    #: The source's smallest text, in its pixels (:func:`text_size`).
    text: TextSize
    #: A recording's window width (its pixel ratio taken out, its zoom kept); ``None`` for a file.
    viewport: float | None = None
    #: The pane of a wall it is, ``None`` for an insert of one source.
    pane: int | None = None
    #: A recording's browser zoom, in percent (``spec.TakeScript.browser_zoom``).
    zoom: int = 100
    #: The width of the 1080p frame of the video's shape (16:9 unless judged on another).
    frame_width: float = REFERENCE_SHORT_SIDE * 16 / 9
    #: The peak of the insert's own push (``Overlay.zoom.scale``): its text drawn that much larger there.
    push: float = 1.0
    #: A recording's pixel ratio, device pixels per pixel of its window; ``None`` for a file.
    pixel_ratio: float | None = None
    #: How far down a recording's page keeps its layout, in CSS pixels (``VideoTake.layout``,
    #: ``capture.probe_layout``); ``None``: not probed - a take recorded before the probe, or a file.
    layout: dict | None = None
    #: The quality its sharpness is judged at (``render_checks.target_quality``): what draws its source larger
    #: is offered only at a scale the source carries there.
    quality: str = DEFAULT_QUALITY
    #: Declared scenery (:func:`ambient`): its text is measured, said as a fact, never judged.
    ambient: bool = False
    #: The width, on the 1080p frame, of the other inserts side by side with it at the same time, each as wide as its
    #: own advice asks (:func:`_beside`), and how many they are; its own wall's other panes apart: as wide as
    #: they ask, and as they are.
    beside: float = 0.0
    inserts_beside: int = 0
    panes_beside: float = 0.0
    panes_width: float = 0.0

    @property
    def drawn_text(self) -> float:
        """Its smallest text's size on the 1080p frame, at the peak of its push."""
        return self.text.px * self.scale * self.push

    @property
    def readable(self) -> bool:
        return self.drawn_text >= MIN_TEXT_PX

    @property
    def needed_width(self) -> int:
        """The width its quad needs on the 1080p frame for its smallest text to read :data:`TARGET_TEXT_PX`."""
        return math.ceil(self.quad_width * self._grow())

    @property
    def asked_width(self) -> float:
        """The width it asks of its scene: :attr:`needed_width` when its advice asks a larger surface - judged
        and too small to read -, else the width it has."""
        return float(self.needed_width) if not (self.readable or self.ambient) else self.quad_width

    @property
    def crowded(self) -> bool:
        """Whether its scene has no surface wide enough for it: with the other inserts side by side with it,
        each as wide as it asks, past the frame's width. A wall's own panes are left out: fewer panes frees theirs."""
        return self.inserts_beside > 0 and self.needed_width + self.beside > self.frame_width

    @property
    def largest_zoom(self) -> int | None:
        """The largest browser zoom, in percent to the 10 below, at which a recording's window lays its page out no
        narrower than the probe saw it keep its layout (``holds_down_to``); ``None`` for a page not probed or a
        file. A zoom lays the page out at the window over it (``spec.zoomed_window``)."""
        if not self.viewport or not isinstance(self.layout, dict):
            return None
        holds = self.layout.get("holds_down_to")
        if not isinstance(holds, int | float) or holds <= 0:
            return None
        return min(MAX_BROWSER_ZOOM, math.floor(self.viewport * 100 / float(holds) / 10) * 10)

    @property
    def illegible(self) -> bool:
        """Drawn under :data:`ILLEGIBLE_TEXT_PX`: a finding, no longer advice."""
        return self.drawn_text < ILLEGIBLE_TEXT_PX

    @property
    def identity(self) -> str:
        """The surface and what it shows, the advisory said once for (:func:`said_once`)."""
        return f"{self.key}|{self.source[0]}:{self.source[1]}"

    def _grow(self) -> float:
        """How many times larger its smallest text must be drawn to read :data:`TARGET_TEXT_PX`."""
        return TARGET_TEXT_PX / max(self.drawn_text, 1e-6)

    def _need(self) -> float:
        """How many times larger than unpushed its source must be drawn for its smallest text to read
        :data:`TARGET_TEXT_PX`: the push that does it, the factor a surface or a window would draw it larger by."""
        return self.push * self._grow()

    def carried(self, quality: str | None = None) -> float:
        """How many times larger than unpushed its source can be drawn - pushed, on a larger surface, laid out in a
        narrower window - keeping ``screen_insert.MIN_DENSITY`` of its pixels per pixel of a render at *quality* (its
        :attr:`quality`): the largest scale the soft gate passes there (``insert_checks.soft_before``)."""
        from app.services.video_studio.screen_insert import MIN_DENSITY

        return REFERENCE_SHORT_SIDE / (self.scale * _short_side(quality or self.quality) * MIN_DENSITY)

    def density(self, scale: float, quality: str | None = None) -> float:
        """Its source's pixels per pixel of a render at *quality* (its :attr:`quality`), drawn *scale* times larger than
        unpushed: what the soft gate reads (``screen_insert.density``)."""
        return REFERENCE_SHORT_SIDE / (self.scale * _short_side(quality or self.quality) * scale)

    def _carry(self) -> float:
        """:meth:`carried` to the tenth below: the largest push advised."""
        return math.floor(self.carried() * _PUSH_STEP) / _PUSH_STEP

    def _surface(self) -> str | None:
        """The width its quad needs on the 1080p frame, when the frame and its scene have it: a pane whose
        wall's other panes leave it no room is offered fewer panes alone - on a larger wall when its wall is narrower."""
        needed = self.needed_width
        if needed > self.frame_width or self.crowded:
            return None
        if self.pane is None:
            place = "a larger surface"
        elif needed + self.beside + self.panes_beside <= self.frame_width:
            place = "a larger surface, or fewer panes on the wall"
        elif needed <= self.quad_width + self.panes_width:
            place = "fewer panes on the wall"
        else:
            place = "fewer panes on a larger wall"
        return f"give it about {needed} px of the 1080p frame's width ({needed / self.frame_width:.0%} of it: {place})"

    def _crowding(self) -> str:
        """``the scene has no such surface for it: ...`` - what a :attr:`crowded` surface is told first."""
        needed, frame = self.needed_width, self.frame_width
        others = f"{self.inserts_beside} other insert{'s' if self.inserts_beside != 1 else ''}"
        return (
            f"the scene has no such surface for it: about {needed} px of the 1080p frame's width ({needed / frame:.0%} of "
            f"it) beside the {others} side by side with it ({self.beside / frame:.0%} of it, each at the size it asks), "
            f"{(needed + self.beside) / frame:.0%} together"
        )

    def _safe_zoom(self) -> tuple[str | None, str | None]:
        """``(advice, caution)`` for a :attr:`crowded` recording, whatever its quad's share of the frame: recorded
        again at the browser zoom that draws its text :data:`TARGET_TEXT_PX`, up to the :attr:`largest_zoom` its page
        keeps its layout at - its window and frame keeping their size, so its quad draws it at the same scale."""
        if not self.viewport:
            return None, None
        largest = self.largest_zoom
        if largest is None:
            return None, (
                "no browser zoom offered: how far down its page keeps its layout was not probed when it was recorded "
                "(a recording made again is)"
            )
        holds = int(self.layout["holds_down_to"]) if isinstance(self.layout, dict) else 0
        safe = f"largest safe zoom {largest} on this page (its layout holds down to {holds} px, probed when it was recorded)"
        wanted = math.ceil(self.zoom * self._grow() / 10) * 10
        zoom = min(wanted, largest)
        if zoom <= self.zoom:
            return None, f"no higher browser zoom keeps its layout: {safe}"
        reads = f", its smallest text then about {self.drawn_text * zoom / self.zoom:.0f} px" if zoom < wanted else ""
        advice = (
            f"record it again with a browser zoom of {zoom}% (browser_zoom: {zoom}), its window and frame keeping their "
            f"size - {safe}{reads}"
        )
        changes, why = (self.layout or {}).get("changes_at"), (self.layout or {}).get("why")
        if zoom < wanted and isinstance(changes, int | float):
            return advice, f"a browser zoom of {wanted}% would change its layout ({why or 'it changes'} at {int(changes)} px)"
        return advice, None

    def _tighter(self) -> str:
        """What draws a :attr:`crowded` source's text larger on the quad it has: less to show in the same width."""
        if self.source[0] != "take":
            return "a tighter picture: the part to read cropped out of it, its text larger for the width it is drawn at"
        return (
            "a tighter page: a single panel of it - one dashboard panel, not the whole dashboard - recorded on its own, "
            "its text larger for the width it is drawn at"
        )

    @property
    def pushable(self) -> bool:
        """Whether a push may be offered: drawn under :data:`MIN_PUSH_SCALE`, the source shows as a whole page,
        and a push crops it."""
        return self.scale >= MIN_PUSH_SCALE

    def _no_push(self) -> str:
        """Why no push is offered, when none is (:attr:`pushable`); empty otherwise."""
        if self.pushable:
            return ""
        return (
            f"no push: drawn at {self.scale:.2f}x, under {MIN_PUSH_SCALE:g}x, it shows as a whole page and a push would "
            "crop it"
        )

    def _sized_call(self) -> str:
        """The call that records a recording at the size its saved quad asks for the render it is judged for
        #(6323): ``record_browser_demo(target_render: "ultra", video_id, scene: "s1", insert: 0)`` - its pixel ratio,
        window and browser zoom picked from the quad, passing the soft check by construction."""
        parts = self.key.removeprefix("insert/").split(".")
        index = parts[1] if len(parts) > 1 else "0"
        return f'record_browser_demo(target_render: "{self.quality}", video_id, scene: "{parts[0]}", insert: {index}) records it at that size'

    def _push(self) -> str | None:
        """The push that draws its text large enough, within :data:`spec.MAX_INSERT_ZOOM`, at a scale its source carries
        at the render it is judged for - or with the source that carries it: a recording at a higher pixel ratio
        (the same layout, more pixels), a larger picture -, else, for a recording, the largest push it carries. None
        for a source drawn under :data:`MIN_PUSH_SCALE`: a push crops its page. A recording recorded again is
        told the call that sizes it (:meth:`_sized_call`)."""
        push = math.ceil(self._need() * _PUSH_STEP) / _PUSH_STEP
        if push > MAX_INSERT_ZOOM or not self.pushable:
            return None
        towards = "toward the region to read (focus [x, y], 0-1 across and down the source)"
        said = f"push in on it with zoom {{start, end, scale: {push:g}}} {towards}"
        carry = self._carry()
        if push <= carry:
            return said
        render = render_words(self.quality)
        if self.pixel_ratio is None:
            wide = math.ceil(self.source_size[0] * push / self.carried())
            return f"{said}, from a picture at least {wide} px wide so the push stays sharp at {render}"
        wanted = self.pixel_ratio * push / self.carried()
        ratios = [ratio for ratio in RECORD_RATIOS if self.pixel_ratio < ratio and ratio >= wanted - 1e-9]
        if ratios:
            return (
                f"{said}, recorded again at pixel ratio {ratios[0]:g} - the same layout, more pixels - so the push stays "
                f"sharp at {render} ({self._sized_call()})"
            )
        if carry > max(1.0, self.push):
            reads = self.drawn_text * carry / self.push
            return (
                f"push in on it with zoom {{start, end, scale: {carry:g}}} {towards} - the most the recording carries "
                f"sharp at {render}, its smallest text then about {reads:.0f} px"
            )
        return None

    def _rezoomed(self) -> tuple[str | None, str | None]:
        """``(advice, caution)`` for a recording whose source does not carry the scale that draws its text large enough:
        recorded again at a higher browser zoom - its text larger in the same pixels, its window and frame
        keeping their size -, the push left after it one the source carries, only where its page keeps its layout at
        that zoom (the probe's ``holds_down_to``), else why not when the probe saw it change."""
        if not self.viewport or not isinstance(self.layout, dict):
            return None, None
        limit = min(self._carry(), MAX_INSERT_ZOOM)
        if limit < 1.0:
            return None, None
        need = self._need()
        zoom = min(math.ceil(self.zoom * need / limit / 10) * 10, MAX_BROWSER_ZOOM)
        left = math.ceil(need * self.zoom / zoom * _PUSH_STEP) / _PUSH_STEP
        if zoom <= self.zoom or left > limit or (left > 1.0 and not self.pushable):
            return None, None
        css = self.viewport * 100 / zoom
        holds = self.layout.get("holds_down_to")
        if isinstance(holds, int | float) and css >= float(holds):
            then = f", then push in on it with zoom {{start, end, scale: {left:g}}}" if left > 1.0 else ", no push needed"
            return (
                f"record it again with a browser zoom of {zoom}% (browser_zoom: {zoom}; its layout holds down to "
                f"{int(holds)} px, probed when it was recorded){then}: its text larger in the same pixels, sharp at "
                f"{render_words(self.quality)} ({self._sized_call()})"
            ), None
        changes, why = self.layout.get("changes_at"), self.layout.get("why")
        if isinstance(changes, int | float) and css < float(changes) + 1:
            return None, f"a browser zoom of {zoom}% would change its layout ({why or 'it changes'} at {int(changes)} px)"
        return None, None

    def _ceiling(self) -> str:
        """``drawn that large, it stays sharp up to a high render (1080p): an ultra render (2160p) draws it at 0.65 of
        its pixels per output pixel (under 0.8) and refuses it`` - the first render larger than the one it is judged for
        that refuses the scale drawing its text large enough; empty when none does."""
        from app.services.video_studio.screen_insert import MIN_DENSITY

        scale = math.ceil(self._need() * _PUSH_STEP) / _PUSH_STEP
        larger = sorted(
            (name for name in QUALITY_PRESETS if _short_side(name) > _short_side(self.quality)),
            key=lambda name: (_short_side(name), name != DEFAULT_QUALITY),
        )
        refusing = next((name for name in larger if scale > self.carried(name)), None)
        if refusing is None:
            return ""
        return (
            f"drawn that large, it stays sharp up to {render_words(self.quality)}: {render_words(refusing)} draws it "
            f"at {self.density(scale, refusing):.2f} of its pixels per output pixel (under {MIN_DENSITY:g}) and "
            "refuses it"
        )

    def _relayout(self) -> tuple[str | None, str | None]:
        """``(advice, caution)``: a recording laid out narrower so its text is larger for its width - on a quad under
        :data:`NARROW_SHARE` of the frame a higher browser zoom (its window and frame keep their size), on a
        wider one a narrower window - only where its page keeps the layout it was recorded with (the probe's
        ``holds_down_to``, in CSS pixels: the window over the zoom), else why not when the probe saw it change."""
        if not self.viewport or not isinstance(self.layout, dict):
            return None, None
        grow = self._grow()
        if self.quad_width < NARROW_SHARE * self.frame_width:
            zoom = math.ceil(self.zoom * grow / 10) * 10
            capped = zoom > MAX_BROWSER_ZOOM
            zoom = min(zoom, MAX_BROWSER_ZOOM)
            css = self.viewport * 100 / zoom
            advice = (
                f"record it again with a browser zoom of {zoom}% (browser_zoom: {zoom})"
                + (
                    f" on a larger surface: no zoom alone brings its text to {TARGET_TEXT_PX:g} px"
                    if capped
                    else ", its window and frame keeping their size"
                )
            )
            change = "a higher browser zoom"
        else:
            window = math.floor(self.viewport / grow)
            if window < MIN_VIEWPORT_WIDTH:
                return None, None
            css = window * 100 / self.zoom
            advice = f"record it again with a viewport about {window} px wide"
            change = "a narrower viewport"
        holds = self.layout.get("holds_down_to")
        if isinstance(holds, int | float) and css >= float(holds):
            return f"{advice} (its layout holds down to {int(holds)} px, probed when it was recorded)", None
        changes, why = self.layout.get("changes_at"), self.layout.get("why")
        if isinstance(changes, int | float) and css < float(changes) + 1:
            return None, f"{change} would change its layout ({why or 'it changes'} at {int(changes)} px)"
        return None, None

    def fix(self) -> str:
        """What brings its smallest text to :data:`TARGET_TEXT_PX` while the page keeps its layout, at a scale its
        source carries at the render it is judged for: a larger surface, a push on the insert, a recording laid
        out narrower only where the probe saw the layout hold - or, where the source does not carry that scale, what
        makes a source carry it (:meth:`_push`, :meth:`_rezoomed`), never a scale the soft gate refuses. A surface its
        scene has no room for (:attr:`crowded`) is said so, and offered what needs no more of the frame: a push,
        a higher browser zoom its page keeps its layout at (:meth:`_safe_zoom`), a tighter page (:meth:`_tighter`)."""
        from app.services.video_studio.screen_insert import MIN_DENSITY

        carried = self._need() <= self.carried()
        crowding = self._crowding() if carried and self.crowded else ""
        if crowding:
            advice, caution = self._safe_zoom()
            drawn = [said for said in (self._push(),) if said]
            options = [*drawn, *([advice] if advice else []), self._tighter()]
            ceiling = self._ceiling() if drawn else ""
        elif carried:
            advice, caution = self._relayout()
            drawn = [said for said in (self._surface(), self._push()) if said]
            options = [*drawn, *([advice] if advice else [])]
            ceiling = self._ceiling() if drawn else ""
        else:
            advice, caution = self._rezoomed()
            options = [said for said in (self._push(), advice) if said]
            ceiling = ""
        if options:
            said = ", or ".join(options)
        elif carried:
            pushed = f" pushed in {MAX_INSERT_ZOOM:g}x" if self.pushable else ""
            said = (
                f"even the frame's whole width{pushed} draws it under {TARGET_TEXT_PX:g} px: "
                "use a source whose text is larger for its width"
            )
        else:
            said = (
                f"drawn large enough to read, its source is soft at {render_words(self.quality)} "
                f"({self.density(self._need()):.2f} of its pixels per output pixel, under {MIN_DENSITY:g}): use a source "
                "whose text is larger for its width"
            )
        # Declared scenery is never offered the declaration it already carries (issue #10093).
        offer = "" if self.ambient else AMBIENT_OFFER
        return "; ".join(part for part in (crowding, said, caution, ceiling, self._no_push(), offer) if part)

    def fact(self) -> str:
        """What the check says of an :attr:`ambient` surface in place of a finding: its smallest text's size."""
        pane = f" (pane {self.pane + 1})" if self.pane is not None else ""
        return (
            f"its smallest text{pane}, about {self.text.px:.0f} px in the source, reads about {self.drawn_text:.0f} px "
            f"on the 1080p frame (drawn at {self.scale:.2f}x)"
        )

    def message(self) -> str:
        from app.services.video_studio import timeline

        kind, ref = self.source
        named = "recording" if kind == "take" else "file"
        width, height = self.source_size
        pushed = f" ({self.scale * self.push:.2f}x at the peak of its zoom)" if self.push > 1 else ""
        return (
            f"{timeline.insert_label(self.key)} draws {named} `{ref}` ({width}x{height} px) at {self.scale:.2f}x on the "
            f"1080p frame{pushed}: its smallest text, about {self.text.px:.0f} px in the source, reads about "
            f"{self.drawn_text:.0f} px (advisory under {MIN_TEXT_PX:g} px, a finding under {ILLEGIBLE_TEXT_PX:g} px) - "
            f"{self.fix()}"
        )


def _read_parts(spec: Any) -> dict[str, Any]:
    """``key -> timeline.InsertPart`` of every surface of the parsed *spec*'s screen inserts whose text is read: a
    recording or an uploaded file (:data:`READ_KINDS`) - an :func:`ambient` insert's too, measured, never judged."""
    from app.services.video_studio import timeline

    return {key: part for key, part in timeline.insert_sources(spec).items() if part.kind in READ_KINDS}


@dataclass(frozen=True)
class _Placed:
    """Where a screen insert surface stands on the 1080p frame, and when (:func:`_beside`)."""

    scene: str
    index: int
    box: tuple[float, float, float, float]
    overlay: Any

    @property
    def width(self) -> float:
        return self.box[2] - self.box[0]


def _side_by_side(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    """Whether boxes *a* and *b* (``left, top, right, bottom``) share the frame's width: across from each other - no
    more than half the narrower overlapping it -, sharing half the shorter's height."""
    across = min(a[2], b[2]) - max(a[0], b[0])
    shared = min(a[3], b[3]) - max(a[1], b[1])
    return across <= 0.5 * min(a[2] - a[0], b[2] - b[0]) and shared >= 0.5 * min(a[3] - a[1], b[3] - b[1])


def _together(a: Any, b: Any) -> bool:
    """Whether overlays *a* and *b* are drawn at the same time: their spans overlap - counted from different steps of
    a recording, they may."""
    if a.at_step != b.at_step:
        return True
    return a.start < b.start + b.duration and b.start < a.start + a.duration


def _beside(item: Legibility, placed: dict[str, _Placed], asked: dict[str, float]) -> Legibility:
    """*item* told what shares its scene's width: every other surface side by side with it at the same time,
    as wide as its own advice asks (*asked*, a surface not judged as wide as it is) - its own wall's panes apart."""
    me = placed.get(item.key)
    if me is None:
        return item
    inserts, indices, panes, wall = 0.0, set(), 0.0, 0.0
    for key, other in placed.items():
        if key == item.key or other.scene != me.scene:
            continue
        if not (_side_by_side(me.box, other.box) and _together(me.overlay, other.overlay)):
            continue
        width = asked.get(key, other.width)
        if other.index == me.index:
            panes += width
            wall += other.width
        else:
            inserts += width
            indices.add(other.index)
    return dataclasses.replace(
        item, beside=inserts, inserts_beside=len(indices), panes_beside=panes, panes_width=wall
    )


def judge(
    spec: Any,
    texts: dict[tuple[str, str], TextSize],
    *,
    sources: dict[tuple[str, str], tuple[int, int]],
    footage: dict[str, tuple[int, int]],
    pixel_ratios: dict[str, float],
    browser_zooms: dict[str, int] | None = None,
    layouts: dict[str, dict] | None = None,
    quality: str = DEFAULT_QUALITY,
) -> list[Legibility]:
    """The :class:`Legibility` of every surface of the parsed *spec*'s screen inserts whose text is read - an
    :func:`ambient` one's said, never judged - whose source's size (*sources*, ``render_checks.insert_geometry``) and
    text (*texts*) are known, on the 1080p frame of its shape; a recording's *layouts* (``VideoTake.layout``) say how
    far down its page keeps its layout, and *quality* the render its sharpness is judged at. Each is told the
    width the other inserts side by side with it ask of its scene (:func:`_beside`)."""
    from app.services.video_studio import insert_checks, screen_insert, timeline
    from app.services.video_studio.spec import output_size

    frame = output_size(spec.format.aspect_ratio, REFERENCE_SHORT_SIDE)
    placed = {
        key: _Placed(
            timeline.insert_scene(key),
            part.index,
            insert_checks.box_on_frame(part.quad, footage.get(timeline.insert_scene(key)), frame),
            part.overlay,
        )
        for key, part in timeline.insert_sources(spec).items()
    }
    out: list[Legibility] = []
    for key, part in _read_parts(spec).items():
        source = (part.kind, part.ref)
        size, text = sources.get(source), texts.get(source)
        if size is None or text is None:
            continue
        quad_w, quad_h = insert_checks.quad_on_frame(part.quad, footage.get(timeline.insert_scene(key)), frame)
        fitted = screen_insert.density(size, quad_w, quad_h, part.overlay.fit or "cover")
        if fitted is None:
            continue
        ratio = pixel_ratios.get(part.ref) if part.kind == "take" else None
        out.append(
            Legibility(
                key,
                source,
                (int(size[0]), int(size[1])),
                quad_w,
                1.0 / fitted.value,
                text,
                viewport=size[0] / ratio if ratio else None,
                pane=part.pane,
                zoom=(browser_zooms or {}).get(part.ref, 100),
                frame_width=float(frame[0]),
                push=float(part.overlay.zoom.scale) if part.overlay.zoom is not None else 1.0,
                pixel_ratio=ratio,
                layout=(layouts or {}).get(part.ref) if part.kind == "take" else None,
                quality=quality,
                ambient=ambient(part.overlay),
            )
        )
    asked = {item.key: item.asked_width for item in out}
    return [_beside(item, placed, asked) for item in out]


def findings(measured: list[Legibility]) -> list[tuple[str, str, str, str]]:
    """``(scene id, words, severity, the surface's identity)`` of every surface of *measured* whose text is drawn too
    small to read: ``advisory`` under :data:`MIN_TEXT_PX`, ``dense`` - a finding the render waits for - under
    :data:`ILLEGIBLE_TEXT_PX`. An :attr:`~Legibility.ambient` one is never one (:func:`unchecked` says it)."""
    from app.services.video_studio import timeline

    return [
        (timeline.insert_scene(item.key), item.message(), "dense" if item.illegible else "advisory", item.identity)
        for item in measured
        if not (item.readable or item.ambient)
    ]


def ambient_overrides(
    measured: Sequence[Legibility], statements: set[str] | frozenset[str] = frozenset()
) -> list[tuple[str, str, str]]:
    """``(scene id, words, the surface's identity)`` of every :attr:`~Legibility.ambient` recording surface of
    *measured* whose UI text is under :data:`ILLEGIBLE_TEXT_PX` and which cannot be the scenery it is declared: drawn
    at :data:`MIN_PUSH_SCALE` or more, alone on its scene's frame - no other insert, nor another pane of its wall -,
    and not in one of *statements* (scene ids of statement scenes, dimmed under their text). Advice only: the caller
    says it as an ``advisory`` the render gate never counts. Declared scenery under any of those is honoured -
    :func:`unchecked` says its text size, and nothing else is said (issue #10093)."""
    from app.services.video_studio import timeline

    inserts: dict[str, set[str]] = {}
    for item in measured:
        inserts.setdefault(timeline.insert_scene(item.key), set()).add(insert_of(item.identity))
    out: list[tuple[str, str, str]] = []
    for item in measured:
        scene_id = timeline.insert_scene(item.key)
        if not (item.ambient and item.source[0] == "take" and item.illegible):
            continue
        shares = item.pane is not None or item.inserts_beside > 0 or len(inserts[scene_id]) > 1
        if item.scale < MIN_PUSH_SCALE or shares or scene_id in statements:
            continue
        out.append(
            (
                scene_id,
                f"{item.message()} - marked legibility: ambient, but drawn alone on its frame at {item.scale:.2f}x it "
                "reads as a screen a viewer tries to read (advice, the render does not wait for it)",
                item.identity,
            )
        )
    return out


#: Where an advisory said in a save's answer is remembered, by video and surface (:func:`said_once`).
SAID_KIND = "insert-legibility-said"
#: The advisories of a screen insert a save's answer says once (:func:`said_once`): its text drawn too small,
#: its source soft at a quality the video may be rendered at (``quality`` ``insert_resolution``).
SAID_ONCE_KINDS = frozenset({"insert_legibility", "insert_resolution"})


def _said_digest(project_id: Any, identity: str) -> str:
    shown = {"version": MEASURE_VERSION, "video": str(project_id), "insert": identity}
    return hashlib.sha256(json.dumps(shown, sort_keys=True).encode("utf-8")).hexdigest()


def insert_of(identity: str) -> str:
    """The screen insert a surface's *identity* is on (``Legibility.identity``, ``insert_checks.SoftAt.identity``): a
    wall's pane - ``insert/s1.0.2|take:...`` - is on its wall, ``insert/s1.0``."""
    key = identity.split("|", 1)[0]
    return ".".join(key.split(".")[:2]) if key.startswith("insert/") else key


def advice_of(message: str) -> str:
    """What an advisory of :data:`SAID_ONCE_KINDS` tells to do: its message past the measure it opens with
    (``... reads about 8 px (advisory under 12 px) - <advice>``) - the same for two panes the same change fixes."""
    return message.split(" - ", 1)[1] if " - " in message else message


async def said_once(project_id: Any, report: Any, *, shown: int) -> Any:
    """*report* (a ``quality.QualityReport``) without the advisories of :data:`SAID_ONCE_KINDS` a save of video
    *project_id* already said for their surface: said once per insert, never on every save - a finding
    the render waits for is said every time. Those among the first *shown* findings - what the answer lists - are
    remembered (``render_cache``, :data:`SAID_KIND`), with every advisory of the same kind on the same insert whose
    advice is the one listed (:func:`advice_of`): a wall's panes told the same are said together, never
    one more pane on each save, while a pane with another problem is said by a later save. One the answer leaves out of
    an insert it lists nothing of is said by a later save. A memory that cannot be read says it again."""
    from app.services.video_studio import render_cache

    kept, fresh = [], []
    for finding in report.findings:
        identity = getattr(finding, "insert", None)
        if finding.kind not in SAID_ONCE_KINDS or finding.severity != "advisory" or not identity:
            kept.append(finding)
            continue
        digest = _said_digest(project_id, identity)
        try:
            said = await render_cache.get_json(SAID_KIND, digest)
        except Exception:  # unread: said again
            said = None
        if said is None:
            kept.append(finding)
            fresh.append((finding, digest))
    listed = {id(finding) for finding in kept[:shown]}
    told = {
        (finding.kind, insert_of(finding.insert), advice_of(finding.message)) for finding, _digest in fresh if id(finding) in listed
    }
    for finding, digest in fresh:
        if id(finding) in listed or (finding.kind, insert_of(finding.insert), advice_of(finding.message)) in told:
            try:
                await render_cache.put_json(SAID_KIND, digest, {"said": True})
            except Exception as exc:  # said again by the next save
                logger.info("Screen insert legibility advisory not remembered: %s", format_exception_message(exc))
    return dataclasses.replace(report, findings=tuple(kept))


def unchecked(spec: Any, measured: Sequence[Legibility] = ()) -> list[tuple[str, str]]:
    """``(scene id, words)`` of every :func:`ambient` screen insert of the parsed *spec* showing a source whose text
    is read, once per insert - what the check says in place of a finding: ``its screen insert 0: ambient, not judged -
    its smallest text, about 24 px in the source, reads about 5 px on the 1080p frame (drawn at 0.21x)``, the
    smallest of its panes' in *measured*, a fact the author reads; ``(its text not read)`` when it was not."""
    from app.services.video_studio import timeline

    smallest: dict[str, Legibility] = {}
    for item in measured:
        insert = insert_of(item.identity)
        if item.ambient and (insert not in smallest or item.drawn_text < smallest[insert].drawn_text):
            smallest[insert] = item
    out: dict[str, tuple[str, str]] = {}
    for key, part in timeline.insert_sources(spec).items():
        if part.kind in READ_KINDS and ambient(part.overlay):
            scene_id = timeline.insert_scene(key)
            insert = timeline.insert_key(scene_id, part.index)
            read = smallest.get(insert)
            said = f" - {read.fact()}" if read is not None else " (its text not read)"
            out.setdefault(insert, (scene_id, f"{timeline.insert_label(insert)}: {AMBIENT}, not judged{said}"))
    return list(out.values())


def _digest(file_id: str, seconds: float) -> str:
    shown = {"version": MEASURE_VERSION, "file": file_id, "at": round(float(seconds), 3)}
    return hashlib.sha256(json.dumps(shown, sort_keys=True).encode("utf-8")).hexdigest()


async def _file_of(db: Any, kind: str, ref: str) -> tuple[str, bool] | None:
    """``(the file a source plays, whether it is a picture)``; ``None`` for one gone, not made or not raster."""
    from app.core.utils import safe_uuid
    from app.models.file_attachment import FileAttachment
    from app.models.video_studio import JOB_READY, VideoTake

    uid = safe_uuid(ref)
    if uid is None:
        return None
    if kind == "take":
        take = await db.get(VideoTake, uid)
        if (
            take is None
            or take.status != JOB_READY
            or not take.file_id
            or getattr(take, "expired_at", None) is not None
        ):
            return None
        return str(take.file_id), False
    row = await db.get(FileAttachment, uid)
    content_type = (getattr(row, "content_type", None) or "").lower()
    if row is None or content_type == "image/svg+xml" or not content_type.startswith(("image/", "video/")):
        return None
    return ref, content_type.startswith("image/")


async def _read(file_id: str, picture: bool, seconds: float, workdir: Path) -> TextSize | None:
    """The :func:`text_size` of *file_id* - a picture, or a video's frame at *seconds* -, kept by file and second."""
    from PIL import Image

    from app.services.video_studio import assets, encoder, render_cache

    digest = _digest(file_id, 0.0 if picture else seconds)
    kept = await render_cache.get_json(CACHE_KIND, digest)
    if isinstance(kept, dict):
        return TextSize.from_dict(kept["text"]) if kept.get("text") else None
    base = workdir / f"source-{uuid.uuid4().hex}"
    if await assets.download_file(file_id, str(base)) is None:
        return None
    frame = base.with_suffix(".png")
    if picture:
        frame = base
    else:
        try:
            await encoder.extract_frame(str(base), at=seconds, out_path=str(frame))
        except Exception:  # past a short video's end: its first frame
            await encoder.extract_frame(str(base), at=0.0, out_path=str(frame))
    with Image.open(frame) as image:
        pixels = np.asarray(image.convert("RGB"))
    found = await asyncio.to_thread(text_size, pixels)
    await render_cache.put_json(CACHE_KIND, digest, {"text": found.as_dict() if found else None})
    return found


async def measure(db: Any, spec: Any, *, quality: str = DEFAULT_QUALITY) -> list[Legibility]:
    """The :class:`Legibility` of every surface of the parsed effective *spec*'s screen inserts that shows a
    recording or an uploaded file, an :func:`ambient` insert's read and never judged, its sharpness judged at
    *quality*. Never raises; bounded by :data:`MEASURE_SECONDS`."""
    if not _read_parts(spec):
        return []
    try:
        return await asyncio.wait_for(_measure(db, spec, quality), timeout=MEASURE_SECONDS)
    except TimeoutError:
        logger.info("Screen insert legibility not measured within %.0f s", MEASURE_SECONDS)
    except Exception as exc:
        logger.info("Screen insert legibility not measured: %s", format_exception_message(exc))
    return []


async def _measure(db: Any, spec: Any, quality: str) -> list[Legibility]:
    from app.services.video_studio import render_checks

    geometry = await render_checks.insert_geometry(db, spec)
    texts: dict[tuple[str, str], TextSize] = {}
    with tempfile.TemporaryDirectory(prefix="xtm-insert-legibility-") as work:
        for part in _read_parts(spec).values():
            source = (part.kind, part.ref)
            if source in texts or source not in geometry.sources:
                continue
            played = await _file_of(db, *source)
            if played is None:
                continue
            seconds = float(part.overlay.trim_start or 0.0) + _INTO_SOURCE
            try:
                found = await _read(played[0], played[1], seconds, Path(work))
            except Exception as exc:  # one source unread: the others are still judged
                logger.info(
                    "Screen insert source %s %s not read for legibility: %s", *source, format_exception_message(exc)
                )
                continue
            if found is not None:
                texts[source] = found
    return judge(
        spec,
        texts,
        sources=geometry.sources,
        footage=geometry.footage,
        pixel_ratios=geometry.pixel_ratios,
        browser_zooms=geometry.browser_zooms,
        layouts=geometry.layouts,
        quality=quality,
    )
