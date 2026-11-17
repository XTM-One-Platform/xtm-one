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

"""Frozen pictures in a rendered video.

A scene that outlasts its footage used to hold the footage's last frame for
as long as the narration kept talking. The render's MP4 is now watched
frame by frame (``encoder.iter_gray_frames``: small grey copies, area
averaged, streamed - never the video in memory) and every run of identical
frames longer than :data:`FROZEN_SECONDS` is a finding with its time and its
scene - outside what is still by design (:func:`designed_stills`): the cards
(:data:`STILL_SCENES`: a title, a closing card, a statement), an image or a
slide asked with ``motion: none``, a figure (it rests between its steps), a
recording up to its end (a product at rest is what the recording shows;
only its held end is footage that ran out), a scene's ``hold_end``, the
still beat its author asked for at its end, and a blend
(:func:`blended_windows`: a crossfade or a fade through black into a scene,
the video's own fades): the picture is meant to hold while it fades.

Two frames are the same picture when their mean absolute difference stays
under :data:`SAME_DIFFERENCE` grey levels, both from the frame before and
from the first frame of the run, so a slow drift that changes a fraction
of a level per frame still breaks the run once it has moved. Motion is also
read where it happens, never only averaged over the frame: a
locked-off shot whose only motion is a small subject (a cat far down a
corridor) changes a few pixels of the copy by tens of levels and the rest
by nothing, which the mean reads as still. Past :data:`MOVED_PIXELS` pixels
off by more than :data:`MOVED_LEVELS` from the run's first frame - far more
than an encoder's noise on a held frame, area-averaged - the picture moved.
A subject low in contrast never moves a pixel that far: motion is also read
on the take's subject (:class:`FreezeWatch`) - the pixels that
changed over the scene's frames so far - with the same threshold the whole
frame is held to, so its motion is never diluted by a background that holds.

A clip's screen inserts are watched apart from its footage
(:func:`frame_parts`): a static page on a screen covers much of the frame,
and the footage around it - a candle flickering in a locked-off room, a
sitting animal breathing - was judged against the whole frame, its motion
diluted under the thresholds. Each part (:data:`FOOTAGE`, each insert shown,
placed where the page draws it) is held to the thresholds on its own pixels
and keeps its own run; the picture is frozen only while every part is
still, and the finding says which part stopped moving and which were still
(:class:`StillParts`). A static page over moving footage, or a still subject
under a page that scrolls, is never frozen.

The watch judges the video one :data:`WINDOW_SECONDS` window at a time and
says, as each one starts, how far it is and which window and scenes it reads
(:class:`Window`), so a render's check that stalls is told
from a long one, and by where. A window is bounded: one not judged
within its budget (:func:`window_budget`, well inside the render's stall
window) - a decoder that stopped delivering frames, or frames that take too
long - is left unjudged, its time range and scenes logged and kept in the
check's summary, and the watch goes on from the next window on a decoder
started there; it never holds the render until its supervisor stops it.
A scene still by design from end to end (:func:`still_by_design`: a card -
the template's intro and outro among them -, a statement, an image asked
still, a figure) is never compared: how long its picture holds is measured
on the frame alone (:class:`DesignedHolds`) and reported ``by_design``. What
it judged is kept after each window (:class:`Checkpoint`), keyed by what the
encoded video was drawn from: a render resumed after a stall goes on from
there, never from its first frame.

The planner's ``extend_mode`` (``spec.EXTEND_MODES``) is the cause's fix: a
scene extended to its narration fills it and drifts in on what still holds
(``zoom``, the default) or loops its last second crossfaded
(``loop``) instead of holding.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from app.core.utils import format_exception_message
from app.services.video_studio.double_frames import DoubleWatch
from app.services.video_studio.frame_owners import FrameOwners
from app.services.video_studio.near_empty import NearEmptyWatch
from app.services.video_studio.render_checks import FINDINGS, FROZEN, PASSED, WARN, Finding, clock

logger = logging.getLogger(__name__)

#: A run of identical frames longer than this is a frozen picture.
FROZEN_SECONDS = 0.5
#: Two frames under this mean absolute difference (0-255 grey) show the same picture ...
SAME_DIFFERENCE = 0.3
#: ... unless more than this many pixels of the copy moved from the run's first frame by more than this many levels ...
MOVED_PIXELS = 2
MOVED_LEVELS = 12.0
#: ... or the take's subject moved: the pixels whose grey spans more than this over the take so far, at least
#: this share of the copy, changed from the run's first frame by more than :data:`SAME_DIFFERENCE` on average.
SUBJECT_LEVELS = 4.0
SUBJECT_MIN_SHARE = 0.001
#: A pixel that stops changing leaves the subject as its span closes by this many levels a frame, each side.
SUBJECT_DECAY = 0.25
#: A picture that leaves its run for at most this long and comes back to the run's own first frame, under the same
#: thresholds, never broke the run: an encoder keyframe that re-quantises a held still for a frame or two
#: (a short segment of the compositor's) is no motion a viewer sees.
BLIP_SECONDS = 0.1
#: The transitions that blend the picture out while it holds: never a frozen run over their window.
BLENDED_TRANSITIONS = frozenset({"crossfade", "fade_black"})
#: The width of the grey copies compared; the frame rate they are taken at, at most (:func:`analysis_rate`).
ANALYSIS_WIDTH = 192
ANALYSIS_FPS = 30.0
#: The seconds of video judged at once, each window reported as it starts.
WINDOW_SECONDS = 10.0
#: The wall-clock seconds a window may take to be judged, at most half the render's stall window
#: (:func:`window_budget`); past them it is left unjudged and the watch goes on.
WINDOW_BUDGET_SECONDS = 60.0
#: The clock a window's judging budget is read on: a test freezes it to keep the deadline out of its result.
_now: Callable[[], float] = time.monotonic
#: Scenes whose stillness is their design: a motion scene rests once its elements are in.
STILL_SCENES = frozenset({"title", "outro", "statement", "motion"})
#: Why each of :data:`STILL_SCENES` holds still: what a by-design hold says.
_STILL_BECAUSE = {
    "title": "a title card holds still by design",
    "outro": "a closing card holds still by design",
    "statement": "a statement is held on purpose",
    "motion": "a motion scene rests once its elements are in",
}
#: The part of a frame that is its footage - everything but the screen inserts shown over it.
FOOTAGE = "footage"


@dataclass(frozen=True)
class StillParts:
    """What a frozen run of a frame split in parts holds still: every part shown, and the ones whose own
    still run starts with it - the part that stopped moving, the others still already."""

    still: tuple[str, ...]
    stopped: tuple[str, ...]


class _Part:
    """One part of the frame, watched on its own pixels: its run's first frame, the frame before, its subject.

    The take's subject is the pixels whose grey has spanned more than
    :data:`SUBJECT_LEVELS` over the take, a pixel forgotten once it stops
    changing (:meth:`_subject`) - a cat crossing a locked-off
    corridor, low in contrast, whose motion the whole frame's mean dilutes
    under :data:`SAME_DIFFERENCE` and whose pixels never move past
    :data:`MOVED_LEVELS` - and the part moved when those pixels did.
    """

    def __init__(self, blip: int = 0) -> None:
        self.anchor: np.ndarray | None = None
        self.previous: np.ndarray | None = None
        self.first = 0
        #: The frames a picture may leave its run for and come back to it (:data:`BLIP_SECONDS`).
        self.blip = blip
        #: Whether the last frame came back to the run broken :attr:`blip` frames ago at most.
        self.resumed = False
        #: The run broken last: its first frame, the frame before the break, its first index, the break's index.
        self._broken: tuple[np.ndarray, np.ndarray, int, int] | None = None
        self._take: object = None
        self._low: np.ndarray | None = None
        self._high: np.ndarray | None = None

    def _subject(self, current: np.ndarray, take: object) -> np.ndarray | None:
        """The take's subject, *current* in it: the pixels that changed over the take - each span closing by
        :data:`SUBJECT_DECAY` a frame once it stops changing, so a subject crossing a long take is where it moves
        now, never the whole path it swept; ``None`` for too few."""
        if take != self._take or self._low is None or self._low.shape != current.shape:
            self._take = take
            self._low, self._high = current.astype(np.float32), current.astype(np.float32)
        else:
            np.minimum(self._low + SUBJECT_DECAY, current, out=self._low)
            np.maximum(self._high - SUBJECT_DECAY, current, out=self._high)
        subject = (self._high - self._low) > SUBJECT_LEVELS
        return subject if int(np.count_nonzero(subject)) >= max(8, SUBJECT_MIN_SHARE * current.size) else None

    def same(self, current: np.ndarray, take: object, index: int) -> bool:
        """Whether *current* - this part's pixels of frame *index* - is the picture its run holds; else its run starts again."""
        subject = self._subject(current, take)
        self.resumed = False
        if self.anchor is not None and self.previous is not None and self.anchor.shape == current.shape:
            if _holds(current, self.anchor, self.previous, subject):
                self.previous = current
                return True
            broken = self._broken
            if broken is not None and index - broken[3] <= self.blip and _holds(current, broken[0], broken[1], subject):
                # Back to the run it left a blip ago: that run goes on.
                self.anchor, self.previous, self.first = broken[0], current, broken[2]
                self._broken, self.resumed = None, True
                return True
            if broken is None or index - broken[3] > self.blip:
                self._broken = (self.anchor, self.previous, self.first, index)
        self.anchor, self.first, self.previous = current, index, current
        return False


def _holds(current: np.ndarray, anchor: np.ndarray, previous: np.ndarray, subject: np.ndarray | None) -> bool:
    """Whether *current* is the picture of the run started on *anchor*, *previous* the frame before it."""
    if anchor.shape != current.shape or previous.shape != current.shape:
        return False
    moved = np.abs(current - anchor)
    return (
        float(np.abs(current - previous).mean()) <= SAME_DIFFERENCE
        and float(moved.mean()) <= SAME_DIFFERENCE
        and int(np.count_nonzero(moved > MOVED_LEVELS)) <= MOVED_PIXELS
        and (subject is None or float(moved[subject].mean()) <= SAME_DIFFERENCE)
    )


class FreezeWatch:
    """Frames fed one at a time; :attr:`runs` the ``[first, end)`` indexes of every run of identical ones over *seconds*.

    Each frame names its take (its scene, :func:`check_frames`), and the
    parts it is watched in (:func:`frame_parts`: its footage and each
    screen insert shown, as masks; the whole frame without). Every part keeps
    its own run (:class:`_Part`); the picture holds while every part does and
    the same parts are shown, and :attr:`stills` says, for each run of a frame
    split in parts, which part stopped moving and which were still.
    """

    def __init__(self, fps: float, *, seconds: float = FROZEN_SECONDS) -> None:
        self.fps, self.seconds = fps, seconds
        self.runs: list[tuple[int, int]] = []
        self.stills: dict[tuple[int, int], StillParts] = {}
        self.count = 0
        self._parts: dict[str, _Part] = {}
        self._first = 0
        self._running = False
        #: The parts of the run under way, read when it starts: the frame that ends it resets a part's own run.
        self._held: StillParts | None = None
        #: The frames a picture may leave its run for and come back to it (:data:`BLIP_SECONDS`).
        self.blip = max(1, round(BLIP_SECONDS * fps))
        #: The run the last break ended: its first frame, its parts, the run it closed (if any), the break's index.
        self._broken: tuple[int, StillParts | None, tuple[int, int] | None, int] | None = None

    def _close(self, end: int) -> tuple[int, int] | None:
        if self._running and (end - self._first) / self.fps > self.seconds:
            self.runs.append((self._first, end))
            if self._held is not None:
                self.stills[(self._first, end)] = self._held
            return (self._first, end)
        return None

    def feed(self, frame: np.ndarray, take: object = None, parts: list[tuple[str, np.ndarray]] | None = None) -> None:
        current = frame.astype(np.int16)
        index = self.count
        self.count += 1
        shown = parts or [(FOOTAGE, None)]
        names = [name for name, _mask in shown]
        unchanged = self._running and set(names) == set(self._parts)
        for gone in [name for name in self._parts if name not in names]:
            del self._parts[gone]
        held = [
            self._parts.setdefault(name, _Part(self.blip)).same(
                current if mask is None else current[mask], (take, name), index
            )
            for name, mask in shown
        ]
        if unchanged and all(held):
            broken = self._broken
            if broken is not None and any(self._parts[name].resumed for name in names):
                # The picture came back to the run the blip broke: that run goes on, as if never closed.
                self._broken = None
                if index - broken[3] <= self.blip:
                    first, parts, closed, _at = broken
                    if closed is not None and self.runs and self.runs[-1] == closed:
                        self.runs.pop()
                        self.stills.pop(closed, None)
                    self._first, self._held = first, parts
            return
        running = unchanged
        if running and not (self._broken is not None and index - self._broken[3] <= self.blip):
            self._broken = (self._first, self._held, None, index)
        closed = self._close(index)
        if running and self._broken is not None and self._broken[3] == index:
            self._broken = (self._broken[0], self._broken[1], closed, index)
        self._first, self._running = index, True
        self._held = (
            StillParts(tuple(names), tuple(name for name in names if self._parts[name].first == index))
            if len(shown) > 1
            else None
        )

    def rest(self) -> None:
        """One frame left uncompared, still by design: it ends the run under way."""
        self.skip_to(self.count + 1)

    def skip_to(self, end: int) -> None:
        """The frames up to *end* left unjudged: the run under way ends at the last frame judged."""
        self._close(self.count)
        self._running = False
        self._broken = None
        self._parts.clear()
        self.count = max(self.count, end)

    @property
    def resume_at(self) -> int:
        """The frame a watch resumed from what it kept starts again at: the run under way's first, else the next."""
        return self._first if self._running else self.count

    def restore(self, at: int, runs: list[tuple[int, int]], stills: dict[tuple[int, int], StillParts]) -> None:
        """Resume at frame *at* with the *runs* (and their *stills*) closed before it."""
        self.count, self.runs, self.stills = at, list(runs), dict(stills)
        self._broken = None
        self._parts.clear()
        self._running = False

    def finish(self) -> list[tuple[int, int]]:
        self._close(self.count)
        self._running = False
        return self.runs


def frozen_runs(
    frames: Iterable[np.ndarray], fps: float, *, seconds: float = FROZEN_SECONDS, takes: Iterable[object] | None = None
) -> list[tuple[int, int]]:
    """``[first, end)`` frame indexes of every run of identical *frames* lasting more than *seconds*; *takes* names
    each frame's take (one take without)."""
    watch = FreezeWatch(fps, seconds=seconds)
    named = iter(takes) if takes is not None else None
    for frame in frames:
        watch.feed(frame, next(named, None) if named is not None else None)
    return watch.finish()


def footage_box(scene: dict, width: float, height: float) -> tuple[float, float, float, float]:
    """``(left, top, w, h)`` of a clip scene's footage on a *width* x *height* frame, as the page places it
    (``buildStill``): covering the frame when asked (``fit: cover``) or of its shape, else held in the frame clear of
    its edges - clear of a caption band too on the page, whose height is not known here."""
    ar = float(scene.get("src_w") or width) / max(float(scene.get("src_h") or height), 1.0)
    car = width / max(height, 1.0)
    if scene.get("fit") == "cover" or abs(ar - car) / car < 0.03:
        w, h = (height * ar, height) if ar > car else (width, width / ar)
        return (width - w) / 2, (height - h) / 2, w, h
    edge = height * 0.09
    w = min(width * 0.86, (height - 2 * edge) * ar)
    h = w / ar
    return (width - w) / 2, edge + (height - 2 * edge - h) / 2, w, h


#: A screen insert smaller than this on the grey copy is watched with its footage.
MIN_PART_PIXELS = 16


def frame_parts(
    scene: dict, at: float, size: tuple[int, int], cache: dict | None = None
) -> list[tuple[str, np.ndarray]] | None:
    """The parts a frame *at* seconds into a clip *scene* is watched in, as masks of a *size* ``(w, h)`` copy:
    its :data:`FOOTAGE`, and each screen insert whose layer it draws (``timeline.insert_drawn``: a keyed insert's whole
    scene) where the page draws it - its quad on the footage (:func:`footage_box`), or the box a tracked one
    moves in. ``None``: no insert shown, the frame is one part.

    The masks hold still while the page drifts the shot in (``extend_mode: zoom``): a drift moves every part, never
    a still run. *cache* keeps them per scene and inserts shown.
    """
    if scene.get("type") != "clip":
        return None
    from app.services.video_studio.timeline import insert_drawn

    shown = [
        insert
        for insert in scene.get("inserts") or []
        if (drawn := insert_drawn(insert))[0] <= at < drawn[1]
        and (insert.get("box") if insert.get("track") else insert.get("quad"))
    ]
    if not shown:
        return None
    key = (scene.get("id"), tuple(str(insert.get("src")) for insert in shown), size)
    if cache is not None and key in cache:
        return cache[key]
    from PIL import Image, ImageDraw

    w, h = size
    left, top, box_w, box_h = footage_box(scene, w, h)
    covered = np.zeros((h, w), dtype=bool)
    inserts: list[tuple[str, np.ndarray]] = []
    # The page draws them in order, a later one over an earlier one: each pixel is the topmost insert's.
    for insert in reversed(shown):
        corners = insert.get("box") if insert.get("track") else insert.get("quad")
        picture = Image.new("L", (w, h), 0)
        ImageDraw.Draw(picture).polygon([(left + float(x) * box_w, top + float(y) * box_h) for x, y in corners], fill=1)
        mask = np.asarray(picture, dtype=bool) & ~covered
        if int(np.count_nonzero(mask)) >= MIN_PART_PIXELS:
            covered |= mask
            inserts.insert(0, (str(insert.get("src")), mask))
    footage = ~covered
    parts = ([(FOOTAGE, footage)] if int(np.count_nonzero(footage)) >= MIN_PART_PIXELS else []) + inserts
    found = parts if len(parts) > 1 else None
    if cache is not None:
        cache[key] = found
    return found


def blended_windows(document: dict) -> list[tuple[float, float]]:
    """The spans of *document* where a picture is blended out while it holds: each scene's crossfade (or fade
    through black) in, and the video's own fades from and to black."""
    spans = []
    for scene in document.get("scenes") or []:
        transition = scene.get("transition_in") or {}
        seconds = float(transition.get("duration") or 0.0)
        if transition.get("type") in BLENDED_TRANSITIONS and seconds > 0:
            start = float(scene.get("start") or 0.0)
            spans.append((start, start + seconds))
    duration = float(document.get("duration") or 0.0)
    if fade_in := float(document.get("fade_in") or 0.0):
        spans.append((0.0, fade_in))
    if (fade_out := float(document.get("fade_out") or 0.0)) and duration:
        spans.append((max(0.0, duration - fade_out), duration))
    return spans


def _minus(span: tuple[float, float], holes: list[tuple[float, float]]) -> list[tuple[float, float]]:
    pieces = [span]
    for a, b in holes:
        pieces = [
            part
            for start, end in pieces
            for part in ((start, min(end, a)), (max(start, b), end))
            if part[1] - part[0] > 1e-6
        ]
    return pieces


def _played(time_map: list) -> float:
    """Output seconds into a scene its footage plays before it holds: where its time map reaches its last source time."""
    if not time_map:
        return 0.0
    last = time_map[-1][1]
    return float(next((t for t, source in time_map if source >= last), time_map[-1][0]))


def still_by_design(scene: dict) -> str | None:
    """Why a planned *scene* holds still by design from end to end - a card (a template's intro and outro are cards),
    a statement, an image or a slide asked with ``motion: none``, a figure -; ``None`` for a scene that should move."""
    kind = scene.get("type")
    if kind in STILL_SCENES:
        return _STILL_BECAUSE[kind]
    if scene.get("figure"):
        return "a figure rests between its steps"
    if kind in ("image", "slide") and scene.get("motion") == "none":
        return f"{'an image' if kind == 'image' else 'a slide'} asked with motion none holds still"
    return None


def designed_stills(scene: dict) -> list[tuple[float, float]]:
    """The parts of a planned *scene* still by design: a card, a still asked still, a figure, a recording's own time, a ``hold_end``."""
    start, end = float(scene.get("start") or 0.0), float(scene.get("end") or 0.0)
    kind = scene.get("type")
    if still_by_design(scene) is not None:
        return [(start, end)]
    spans = []
    if kind == "recording":
        spans.append((start, start + _played(scene.get("time_map") or [])))
    if still := float(scene.get("hold_end") or 0.0):
        spans.append((max(start, end - still), end))
    return spans


#: What a frozen picture asks of its footage, which ran out or holds.
FOOTAGE_REMEDY = "give its footage more length, or its extend_mode zoom or loop"


def _part_label(name: str) -> str:
    from app.services.video_studio.timeline import insert_label

    return "its footage" if name == FOOTAGE else insert_label(name)


def _listed(labels: list[str]) -> str:
    return labels[0] if len(labels) == 1 else f"{', '.join(labels[:-1])} and {labels[-1]}"


def held_words(held: StillParts) -> tuple[str, str]:
    """``(which parts hold, what to do)`` of a frozen run of a frame split in parts: the part that stopped
    moving first, the others still already - ``its footage stops moving while its screen insert 0 is still``."""
    stopped = [_part_label(name) for name in held.stopped] or [_part_label(name) for name in held.still]
    rest = [_part_label(name) for name in held.still if name not in held.stopped] if held.stopped else []
    if rest:
        said = (
            f"{_listed(stopped)} {'stops' if len(stopped) == 1 else 'stop'} moving while {_listed(rest)} "
            f"{'is' if len(rest) == 1 else 'are'} still"
        )
    else:
        said = f"{_listed(stopped)} {'is' if len(stopped) == 1 else 'are'} still"
    if FOOTAGE in held.stopped or not held.stopped:
        return said, FOOTAGE_REMEDY
    return said, (
        f"its footage holds still under it: give {stopped[0]} a source that moves through its time on screen, or the "
        "footage a subject that moves"
    )


def analysis_rate(fps: float) -> float:
    """The frame rate the watch reads a video of *fps* at: *fps* itself up to :data:`ANALYSIS_FPS`, else the
    largest whole fraction of it under that - 25 for 50 fps, 30 for 60 -, so every frame it reads is one encoded frame
    (:class:`Shown`), whatever the decoder's rounding and wherever a resumed watch seeks."""
    rate = max(float(fps), 1.0)
    return rate / math.ceil(rate / ANALYSIS_FPS - 1e-9)


@dataclass(frozen=True)
class Shown:
    """Which planned scene each frame the watch reads shows: the encoded frame it is - from the MP4's first
    (*first*, a preview's window's), one in *step* - and that frame's owner in the render's table
    (``frame_owners.FrameOwners``), never a second the watch works a scene out from on its own."""

    owners: FrameOwners
    scenes: tuple[dict, ...]
    first: int
    step: int

    @classmethod
    def of(
        cls, document: dict, fps: float, rate: float, *, offset: float = 0.0, owners: FrameOwners | None = None
    ) -> Shown:
        """For a video of *fps* read at *rate*, starting *offset* seconds into *document*."""
        owners = owners if owners is not None else FrameOwners.of({**document, "fps": fps})
        scenes = tuple(scene for scene in document.get("scenes") or [] if isinstance(scene, dict))
        return cls(owners, scenes, round(offset * owners.fps), max(1, round(owners.fps / rate)))

    def frame(self, count: int) -> int:
        """The encoded frame the watch's *count*-th frame is."""
        return self.first + count * self.step

    def scene(self, count: int) -> dict:
        """The planned scene the watch's *count*-th frame shows (``{}``: none) - past the plan's last frame, its last's."""
        if not self.owners.total:
            return {}
        index = self.owners.owner(min(self.frame(count), self.owners.total - 1))
        return self.scenes[index] if index is not None else {}


def findings_of_runs(
    runs: list[tuple[int, int]],
    fps: float,
    document: dict,
    *,
    offset: float = 0.0,
    stills: dict[tuple[int, int], StillParts] | None = None,
    shown: Shown | None = None,
) -> list[Finding]:
    """A finding for each frozen run outside what is still by design - and outside a blend (:func:`blended_windows`):
    the picture is meant to hold while it fades -, its time in the MP4 (*offset*: where it starts), its scene the one
    its middle frame shows (*shown*: :class:`Shown`, the document's at its own rate by default). *runs* are frames read
    at *fps*. *stills* (``FreezeWatch.stills``) says which part of a frame split in parts stopped moving and which were
    still."""
    shown = shown if shown is not None else Shown.of(document, float(document.get("fps") or fps), fps, offset=offset)
    designed = [
        (start - offset, end - offset)
        for scene in document.get("scenes") or []
        for start, end in designed_stills(scene)
        if end > start
    ] + [(start - offset, end - offset) for start, end in blended_windows(document)]
    findings = []
    for first, end in runs:
        held = (stills or {}).get((first, end))
        said, remedy = held_words(held) if held is not None else (None, FOOTAGE_REMEDY)
        for start, stop in _minus((first / fps, end / fps), designed):
            if stop - start <= FROZEN_SECONDS:
                continue
            scene = shown.scene(int((start + stop) / 2 * fps))
            data = {"seconds": round(stop - start, 3), "scene_type": scene.get("type")}
            if held is not None:
                data.update(still=list(held.still), stopped=list(held.stopped))
            findings.append(
                Finding(
                    FROZEN,
                    start,
                    stop,
                    scene.get("id"),
                    f"the picture is frozen for {stop - start:.1f} s ({clock(start)} to {clock(stop)}) in a "
                    f"{scene.get('type') or 'scene'}{': ' + said if said else ''}: {remedy}",
                    WARN,
                    data,
                    code="frozen",
                    params={"seconds": round(stop - start, 1), "start": clock(start), "end": clock(stop)},
                )
            )
    return findings


@dataclass
class FrameCheck:
    status: str
    findings: list[Finding] = field(default_factory=list)
    summary: dict = field(default_factory=dict)

    def as_summary(self) -> dict:
        return {"status": self.status, **self.summary}


def window_budget(stall_seconds: float) -> float:
    """The wall-clock seconds a window of the watch may take: :data:`WINDOW_BUDGET_SECONDS`, never past half
    the render's stall window (*stall_seconds*, ``0`` for none) - the watch moves on long before its supervisor would
    stop the render."""
    return WINDOW_BUDGET_SECONDS if stall_seconds <= 0 else min(WINDOW_BUDGET_SECONDS, stall_seconds / 2)


def _scene_words(scene: dict) -> str:
    return f"`{scene.get('id')}` ({scene.get('type') or 'scene'})"


@dataclass(frozen=True)
class Window:
    """A stretch of the MP4 the watch judges at once: ``[start, end)`` seconds into it, and its scenes."""

    start: float
    end: float
    #: The scenes it shows, ``(id, type)``, in order.
    scenes: tuple[tuple[str, str], ...] = ()

    @classmethod
    def of(cls, document: dict, start: float, end: float, *, offset: float = 0.0) -> Window:
        shown = tuple(
            (str(scene.get("id")), str(scene.get("type") or "scene"))
            for scene in document.get("scenes") or []
            if float(scene.get("start") or 0.0) < end + offset and float(scene.get("end") or 0.0) > start + offset
        )
        return cls(start, end, shown)

    def where(self) -> str:
        """``3:10.00 to 3:14.00, scene `s12` (outro)``."""
        said = f"{clock(self.start)} to {clock(self.end)}"
        if not self.scenes:
            return said
        named = [_scene_words({"id": scene, "type": kind}) for scene, kind in self.scenes]
        return f"{said}, scene{'s' if len(named) > 1 else ''} {_listed(named)}"

    def unjudged(self, why: str) -> dict:
        return {
            "at": round(self.start, 3),
            "end": round(self.end, 3),
            "scenes": [scene for scene, _kind in self.scenes],
            "why": why,
        }


class DesignedHolds:
    """How long the picture of each scene still by design holds (:func:`still_by_design`): measured on the
    frame alone - its mean difference from the hold's first frame -, never judged; what the check reports
    ``by_design``. A hold shorter than :data:`FROZEN_SECONDS` is not reported."""

    def __init__(self) -> None:
        #: ``scene id -> [first, end, type, because]`` of its longest hold.
        self.longest: dict[str, list] = {}
        self._scene: dict | None = None
        self._anchor: np.ndarray | None = None
        self.first = 0

    def feed(self, scene: dict, frame: np.ndarray, index: int) -> None:
        current = frame.astype(np.int16)
        same = (
            self._scene is not None
            and self._scene.get("id") == scene.get("id")
            and self._anchor is not None
            and float(np.abs(current - self._anchor).mean()) <= SAME_DIFFERENCE
        )
        if not same:
            self.leave(index)
            self._scene, self._anchor, self.first = scene, current, index

    def leave(self, index: int) -> None:
        """The hold under way ends before frame *index*: kept when it is its scene's longest."""
        if self._scene is not None:
            scene_id = str(self._scene.get("id"))
            best = self.longest.get(scene_id)
            if best is None or index - self.first > best[1] - best[0]:
                self.longest[scene_id] = [self.first, index, self._scene.get("type"), still_by_design(self._scene)]
        self._scene, self._anchor = None, None

    @property
    def open(self) -> bool:
        return self._scene is not None

    def items(self, rate: float) -> list[dict]:
        """``{scene, type, at, end, seconds, because}`` of each scene's longest hold over :data:`FROZEN_SECONDS`."""
        out = []
        for scene_id, (first, end, kind, because) in self.longest.items():
            seconds = (end - first) / rate
            if seconds > FROZEN_SECONDS:
                out.append(
                    {
                        "scene": scene_id,
                        "type": kind,
                        "at": round(first / rate, 3),
                        "end": round(end / rate, 3),
                        "seconds": round(seconds, 3),
                        "because": because,
                    }
                )
        return sorted(out, key=lambda item: item["at"])


@dataclass(frozen=True)
class Checkpoint:
    """Where the watch of one encoded video keeps what it judged, after each window: read back only by a watch
    of the same video (*key*: what its frames were drawn from, and how the watch reads them), which goes on from
    there."""

    path: Path
    key: str
    #: Called with :attr:`path` after each save: the render stores it beside itself, so the attempt that
    #: resumes on another worker - a deploy replaced this one and its disk - goes on from there too. Best effort.
    stored: Callable[[Path], Awaitable[None]] | None = None

    def load(self) -> dict | None:
        try:
            kept = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return kept if isinstance(kept, dict) and kept.get("key") == self.key else None

    def save(self, state: dict) -> None:
        # Written whole then moved over the last one: a process stopped mid-write leaves the window before.
        partial = self.path.with_name(self.path.name + ".part")
        try:
            partial.write_text(json.dumps({"key": self.key, **state}), encoding="utf-8")
            os.replace(partial, self.path)
        except OSError as exc:  # a watch that cannot keep its place still judges the video
            logger.warning("Frozen-picture watch: its place not kept in %s: %s", self.path, exc)


def _restore(
    kept: dict,
    watch: FreezeWatch,
    holds: DesignedHolds,
    unjudged: list[dict],
    lumas: list[float] | None = None,
    doubles: DoubleWatch | None = None,
    empties: NearEmptyWatch | None = None,
    chrome: Any = None,
) -> int:
    """The frame a watch resumed from *kept* starts at, *watch*, *holds*, *unjudged*, the frames' mean *lumas*,
    the double frames found (*doubles*) and the near-empty canvases (*empties*) as they were then."""
    runs = [(int(a), int(b)) for a, b in kept.get("runs") or []]
    stills = {
        (int(a), int(b)): StillParts(tuple(still), tuple(stopped)) for a, b, still, stopped in kept.get("stills") or []
    }
    at = int(kept.get("frame") or 0)
    watch.restore(at, runs, stills)
    holds.longest = {str(scene): list(held) for scene, held in (kept.get("held") or {}).items()}
    unjudged.extend(kept.get("unjudged") or [])
    if lumas is not None:
        # A watch kept by an older release kept none: those frames are not read for a dip (nan).
        read = [math.nan if value is None else float(value) for value in (kept.get("lumas") or [])][:at]
        lumas.extend(read + [math.nan] * (at - len(read)))
    if doubles is not None:
        # A watch kept by an older release kept none: its frames are not judged again for a double image.
        doubles.restore([hit for hit in kept.get("doubles") or [] if isinstance(hit, list) and hit and hit[0] < at])
    if empties is not None:
        # A watch kept by an older release kept none: its frames are not judged again for an empty canvas.
        empties.restore([hit for hit in kept.get("empties") or [] if isinstance(hit, list) and hit and hit[0] < at])
    if chrome is not None:
        # A watch kept by an older release kept none: its frames are not judged again for static chrome.
        chrome.restore([hit for hit in kept.get("chrome") or [] if isinstance(hit, list) and hit and hit[0] < at])
    return at


def _kept_state(
    watch: FreezeWatch,
    holds: DesignedHolds,
    unjudged: list[dict],
    lumas: list[float] | None = None,
    doubles: DoubleWatch | None = None,
    empties: NearEmptyWatch | None = None,
    chrome: Any = None,
) -> dict:
    # The run or the hold under way is judged again from its first frame: what is kept closed before it.
    at = min(watch.resume_at, holds.first) if holds.open else watch.resume_at
    kept = {
        "frame": at,
        "runs": [list(run) for run in watch.runs if run[1] <= at],
        "stills": [[a, b, list(held.still), list(held.stopped)] for (a, b), held in watch.stills.items() if b <= at],
        "held": {scene: held for scene, held in holds.longest.items() if held[1] <= at},
        "unjudged": unjudged,
    }
    if lumas is not None:
        kept["lumas"] = [None if math.isnan(value) else round(value, 2) for value in lumas[:at]]
    if doubles is not None:
        kept["doubles"] = doubles.kept(at)
    if empties is not None:
        kept["empties"] = empties.kept(at)
    if chrome is not None:
        kept["chrome"] = chrome.kept(at)
    return kept


async def _store_checkpoint(checkpoint: Checkpoint, budget: float) -> None:
    """Hand the place just kept to *checkpoint*'s ``stored``, within *budget* seconds: a store slow or failing
    costs the watch nothing but that copy - the next window's goes again."""
    if checkpoint.stored is None or not checkpoint.path.is_file():
        return
    try:
        await asyncio.wait_for(checkpoint.stored(checkpoint.path), budget)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.info("Frozen-picture watch: its place not stored beside the render: %s", format_exception_message(exc))


def _seek(frame: int, rate: float) -> float:
    # A quarter of a frame early: the decoded seek keeps the frame *frame* is, never the one before it.
    return max(0.0, (frame - 0.25) / rate) if frame else 0.0


async def check_frames(
    video_path: str,
    *,
    document: dict,
    width: int,
    height: int,
    fps: float,
    offset: float = 0.0,
    on_frame: Callable[[], None] | None = None,
    duration: float | None = None,
    watched: Callable[[float, float, Window | None], Awaitable[None]] | None = None,
    budget: float = WINDOW_BUDGET_SECONDS,
    checkpoint: Checkpoint | None = None,
    owners: FrameOwners | None = None,
) -> FrameCheck:
    """The rendered *video_path* watched for frozen pictures; *offset*: where it starts in *document* (a preview).

    It reads the video at :func:`analysis_rate`, each frame the scene its encoded frame belongs to (*owners*:
    the render's ``frame_owners.FrameOwners``, the document's at *fps* by default; :class:`Shown`).

    *on_frame* is called after each frame watched: a render renews its lease from it. *watched*
    ``(seconds watched, *duration*, the window it reads)`` is told as each :data:`WINDOW_SECONDS` window starts and
    once more, with no window, when it ends: the render's progress through its check. *duration* is
    the MP4's length, the *document*'s by default. A window not judged within *budget* seconds is left unjudged
    (``unjudged`` in the summary: its time range, its scenes, why) and the watch goes on from the next one. With a
    *checkpoint*, what was judged is kept after each window and a watch of the same video resumes from it.
    """
    from app.services.video_studio import encoder

    rate = analysis_rate(fps)
    scenes = Shown.of(document, fps, rate, offset=offset, owners=owners)
    w = ANALYSIS_WIDTH
    h = max(2, round(w * height / max(width, 1) / 2) * 2)
    total = float(duration if duration is not None else document.get("duration") or 0.0)
    window = max(1, round(WINDOW_SECONDS * rate))
    watch = FreezeWatch(rate)
    holds = DesignedHolds()
    unjudged: list[dict] = []
    # Each frame's mean luma, by the watch's frame number (``brightness_dip``): nan for a frame not read.
    lumas: list[float] = []
    # Every recording frame read against the two around it (``double_frames``): two pictures at once.
    doubles = DoubleWatch()
    # Every recording frame's canvas read for how much of it carries content (``near_empty``), the cursor left out.
    empties = NearEmptyWatch()
    # Every recording frame's static chrome read against the frames around it (``static_chrome``): drawn at two places.
    from app.services.video_studio.static_chrome import ChromeWatch

    chrome = ChromeWatch()
    kept = checkpoint.load() if checkpoint is not None else None
    resumed = _restore(kept, watch, holds, unjudged, lumas, doubles, empties, chrome) if kept is not None else 0
    masks: dict = {}
    stream = None
    ended = False
    try:
        while not ended:
            first = watch.count
            end = (first // window + 1) * window
            stop = min(end / rate, total) if total else end / rate
            span = Window.of(document, first / rate, stop, offset=offset)
            if watched is not None and (not total or first / rate < total):
                await watched(first / rate, max(total, first / rate), span)
            if stream is None:
                stream = encoder.iter_gray_frames(video_path, width=w, height=h, fps=rate, start=_seek(first, rate))
            deadline = _now() + budget
            why = None
            while watch.count < end:
                left = deadline - _now()
                if left <= 0:
                    why = f"not judged in {budget:g} s"
                    break
                try:
                    raw = await asyncio.wait_for(anext(stream), left)
                except StopAsyncIteration:
                    ended = True
                    break
                except TimeoutError:
                    why = f"no frame decoded in {budget:g} s"
                    break
                frame = np.frombuffer(raw, dtype=np.uint8).reshape(h, w)
                lumas.extend([math.nan] * (watch.count - len(lumas)))
                del lumas[watch.count :]
                lumas.append(float(frame.mean()))
                at = scenes.frame(watch.count) / scenes.owners.fps
                shown = scenes.scene(watch.count)
                doubles.feed(frame, watch.count, shown)
                empties.feed(frame, watch.count, shown)
                chrome.feed(frame, watch.count, shown)
                if still_by_design(shown) is not None:
                    # Never compared: its stillness is its design, measured for the by-design report alone.
                    holds.feed(shown, frame, watch.count)
                    watch.rest()
                else:
                    holds.leave(watch.count)
                    # Each frame's take is the scene it shows: its subject is read over that scene's frames
                    # alone, its footage and each screen insert it shows apart.
                    parts = frame_parts(shown, at - float(shown.get("start") or 0.0), (w, h), masks)
                    watch.feed(frame, shown.get("id"), parts)
                if on_frame is not None:
                    on_frame()
            if why is not None:
                await stream.aclose()
                stream = None
                left_out = Window.of(document, watch.count / rate, stop, offset=offset)
                unjudged.append(left_out.unjudged(why))
                logger.warning(
                    "Frozen-picture watch of %s: %s left unjudged (%s); the watch goes on from %s",
                    video_path,
                    left_out.where(),
                    why,
                    clock(stop),
                )
                holds.leave(watch.count)
                watch.skip_to(end)
                ended = bool(total) and end / rate >= total
            if checkpoint is not None:
                checkpoint.save(_kept_state(watch, holds, unjudged, lumas, doubles, empties, chrome))
                await _store_checkpoint(checkpoint, budget)
    finally:
        if stream is not None:
            await stream.aclose()
    holds.leave(watch.count)
    if watched is not None:
        seen = watch.count / rate
        await watched(seen, max(total, seen), None)
    findings = findings_of_runs(watch.finish(), rate, document, offset=offset, stills=watch.stills, shown=scenes)
    # The whole frame dimming for a moment outside any transition: read on the same frames.
    from app.services.video_studio import brightness_dip

    dipped = brightness_dip.findings(lumas, rate, document, offset=offset, shown=scenes)
    findings.extend(dipped)
    # A recording frame drawn as two pictures at once: read on the same frames.
    from app.services.video_studio import double_frames

    doubled = double_frames.findings(doubles.hits, rate, document, offset=offset, shown=scenes)
    findings.extend(doubled)
    # A recording's canvas left near empty past a beat, the cursor alone moving: read on the same frames.
    from app.services.video_studio import near_empty

    emptied = near_empty.findings(empties.hits, rate, document, offset=offset, shown=scenes)
    findings.extend(emptied)
    # The page's static chrome drawn at another place and back: read on the same frames.
    from app.services.video_studio import static_chrome

    flickered = static_chrome.findings(chrome.hits, rate, document, offset=offset, shown=scenes)
    findings.extend(flickered)
    summary: dict = {"frames": watch.count, "fps": rate}
    if dipped:
        summary["brightness_dips"] = len(dipped)
    if doubled:
        summary["double_images"] = len(doubled)
    if emptied:
        summary["near_empty"] = len(emptied)
    if flickered:
        summary["static_flickers"] = len(flickered)
    if by_design := holds.items(rate):
        summary["by_design"] = by_design
    if unjudged:
        summary["unjudged"] = unjudged
    if resumed:
        summary["resumed_at"] = round(resumed / rate, 3)
    return FrameCheck(FINDINGS if findings else PASSED, findings, summary)
