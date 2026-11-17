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

"""Whether a recording's page has settled, and what a hold shows while it has not.

A ``wait`` after a tab click used to sleep its seconds whatever the page showed: on a page fetching for 7 to 9 s, a
6 s wait ended on a progress bar or an empty graph, seen only on the contact sheet once the take was made. While a
hold step (:data:`HOLD_ACTIONS`) plays, the recorder reads the page every :data:`SAMPLE_SECONDS` (``capture``'s settle
watch: the progress indicators it shows, when its DOM last changed, the boxes of its canvases) and the frame it last
painted (the canvases' pixels, :func:`canvas_crops`), and :class:`PageWatch` judges them:

* **settled**: no progress indicator shown, the DOM unchanged for :data:`STILL_SECONDS` and every canvas's pixels as
  long - twice the interval between two of its paints when it paints slower -; the instant the last of them moved is
  when the page settled;
* **loading**: a progress indicator shown, a canvas painting nothing but one colour (:func:`is_empty`), or an element
  running an animation that never ends whose pixels move on the frame (a spinner, a skeleton's shimmer drawn
  by CSS - which changes no DOM, so the page read as settled with one on screen) - an indicator like the others: the
  wait holds until it stops, or names it when it gives up;
* **ambient**: an element looping an animation with no loading signature (:func:`loading_signature`: no spinner,
  loader, skeleton, shimmer, progress, busy, placeholder or pulse in its name) shown from the hold's first read
  while the DOM stands still - particles, a ping, an orbit drawn for good. It is decoration, not loading: logged
  ``ambient`` on the step's event, said as a fact (:func:`ambient_words`), never a hold and never keeping a wait from
  settling. :func:`loading_hold` re-judges the logged spans the same way (:func:`ambient_names`), so a take recorded
  before this rule is cleared at its next save, with no new recording.

A ``wait`` held until its page settled (:func:`until_settled`: its ``until_settled``, else after a click that
navigated or switched a tab) lasts its seconds at least and, past them, until the page settled, at most its
``timeout`` (:data:`TIMEOUT_SECONDS`) more; its event logs ``settled``. Every hold logs the spans it showed loading
(``loading``), which :func:`loading_hold` reads: all of a ``pause``'s or a ``highlight``'s (the video plays them at
their speed), the end of a ``wait``'s (it handed a page still loading to the next step). The take's answer says them
(:func:`holds_words`), the save raises ``take_loading_hold`` on a scene showing one and ``scene_loading_span`` on a
recording scene whose kept seconds play a page still loading (:func:`loading_spans`: a click that opened a page to its
wait's settled time, every logged span), and
``snapshot_browser_profile_page`` says how long a page took to settle after it opened (:func:`snapshot_words`).
"""

from __future__ import annotations

import io
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import numpy as np

#: Seconds between two reads of the page while a hold plays.
SAMPLE_SECONDS = 0.2
#: How long the DOM and the canvases stand still before the page is settled.
STILL_SECONDS = 0.5
#: How long a canvas must stand still at most, however slowly the page paints it.
MOST_STILL_SECONDS = 2.0
#: Seconds a ``wait`` held until its page settled may last past its own, when its step gives no ``timeout``.
TIMEOUT_SECONDS = 10.0
#: How long ``snapshot_browser_profile_page`` watches a page for it to settle.
SNAPSHOT_SECONDS = 20.0
#: A pixel differing by more than this many gray levels is a change; under it, the JPEG's noise.
NOISE = 24
#: The same for an element looping an animation: a skeleton's shimmer moves a few levels, and a JPEG block whose
#: pixels did not change encodes the same - a looping element still on screen reads the same.
LOOP_NOISE = 6
#: The most looping elements a read names, and how a loading span names one moving.
MOST_LOOPING = 6
LOOPING_MARK = " (animating)"
#: The fresh frames a looping element seen moving must show unchanged, spanning :data:`STILL_SECONDS`, before it is
#: judged stopped: two, so a pair of frames a whole animation turn apart is never the proof alone.
LOOP_STILL_FRAMES = 2
#: What names a looping element a loading indicator (tag, id, classes and role, as ``capture`` names it): one
#: looping without it, shown from a hold's start to its end, is ambient decoration (:func:`ambient_names`).
LOADING_SIGNATURE = re.compile(
    r"spin|loader|loading|skeleton|shimmer|progress|busy|placeholder|pulse|\[status\]", re.IGNORECASE
)
#: Seconds from a hold's start its first loading span may begin, and from its end its last may end, for a looping
#: element in both to read as shown all along: the first read has nothing to compare with, the next is a
#: :data:`SAMPLE_SECONDS` later, a slow page read later still.
AMBIENT_LEAD = 1.0
AMBIENT_TAIL = 0.5
#: What tells a looping element's pixels from a canvas's of the same name and box (:func:`canvas_key`).
_LOOP_KEY = "loop:"
#: A canvas repainted: more than this share of its pixels changed between two reads ...
CHANGE_SHARE = 0.002
#: ... and one painting nothing: fewer than this share of its pixels stand off its main colour.
EMPTY_SHARE = 0.001
#: The width the frames are read at (a JPEG decoded at 1/2, 1/4 or 1/8 of its size).
READ_WIDTH = 800
#: A canvas is read when it shows at least this many CSS px on each side, the largest few of them.
MIN_CANVAS_SIDE = 48
MOST_CANVASES = 4
#: The steps that hold the page as it is; their frames are judged.
HOLD_ACTIONS = frozenset({"wait", "pause", "highlight"})
#: The steps after which a ``wait`` holds until its page settled, when they navigated or switched a tab.
_PAGE_CLICKS = frozenset({"click", "double_click"})
#: The most names a loading span keeps, and the most spans a step keeps.
_MOST_NAMES = 4
_MOST_SPANS = 20
#: How a loading span names a canvas painting nothing.
EMPTY_MARK = " (empty)"


def until_settled(step: Any, events: list[dict]) -> bool:
    """Whether a ``wait`` *step* holds until its page settled: its ``until_settled``, else whether the step before it
    (its markers aside) was a click that opened another page or switched a tab - logged in *events*."""
    if step.action != "wait":
        return False
    if step.until_settled is not None:
        return bool(step.until_settled)
    before = next((event for event in reversed(events) if event.get("action") != "marker"), None)
    return before is not None and before.get("action") in _PAGE_CLICKS and opened_page(before)


def opened_page(event: dict) -> bool:
    """Whether the logged step *event* opened a page: a ``goto``, or a click whose address changed or that switched a
    tab. A click that only starts an animation on the page it stands on (a graph re-laid out, a legend folded) did
    not."""
    if not event.get("ok", True):
        return False
    if event.get("action") == "goto":
        return True
    navigated = bool(event.get("url_before")) and event.get("url") != event.get("url_before")
    return navigated or bool(event.get("tab"))


def _same_route(event: dict) -> bool:
    """Whether a click's address changed in its query or fragment alone (``?layout=radial``): a view's state kept in
    its address, the page itself kept."""
    before, after = event.get("url_before"), event.get("url")
    if event.get("action") == "goto" or event.get("tab") or not before or not after:
        return False
    a, b = urlsplit(str(before)), urlsplit(str(after))
    return (a.scheme, a.netloc, a.path.rstrip("/")) == (b.scheme, b.netloc, b.path.rstrip("/"))


@dataclass(frozen=True)
class PageRead:
    """One read of the page by the recorder's settle watch, in seconds of the take."""

    t: float
    #: When its DOM last changed; ``None``: not known, read as at :attr:`t`.
    changed: float | None
    #: The progress indicators it showed, named as the page names them.
    busy: tuple[str, ...] = ()
    #: Its canvases shown: ``(name, (x, y, width, height))`` in CSS px of the viewport, the largest first.
    canvases: tuple[tuple[str, tuple[float, float, float, float]], ...] = ()
    #: The elements shown running an animation that never ends, the same way.
    looping: tuple[tuple[str, tuple[float, float, float, float]], ...] = ()

    def boxes(self) -> tuple[tuple[str, tuple[float, float, float, float]], ...]:
        """The boxes whose pixels are read on the frame (:func:`canvas_crops`): its canvases and its looping elements."""
        return (*self.canvases, *((f"{_LOOP_KEY}{name}", box) for name, box in self.looping))


def _boxes(entries: Any) -> tuple[tuple[str, tuple[float, ...]], ...]:
    found = []
    for entry in entries or []:
        if isinstance(entry, list) and len(entry) == 2 and isinstance(entry[1], list) and len(entry[1]) == 4:
            found.append((str(entry[0])[:120], tuple(float(v) for v in entry[1])))
    return tuple(found)


def page_read(raw: Any, *, origin: float, at: float) -> PageRead:
    """The settle watch's answer *raw* (its clock in ms since the epoch) in seconds of a take started at *origin*
    (seconds since the epoch); nothing to read - a page navigating - is a page changing at *at*."""
    if not isinstance(raw, dict):
        return PageRead(t=at, changed=at)

    def seconds(value: Any) -> float | None:
        return float(value) / 1000 - origin if isinstance(value, int | float) and not isinstance(value, bool) else None

    t = seconds(raw.get("t"))
    return PageRead(
        t=at if t is None else t,
        changed=seconds(raw.get("changed")),
        busy=tuple(str(name)[:120] for name in (raw.get("busy") or [])),
        canvases=_boxes(raw.get("canvases")),
        looping=_boxes(raw.get("looping"))[:MOST_LOOPING],
    )


def canvas_key(name: str, box: tuple[float, float, float, float]) -> str:
    """One canvas across reads: its name and its box, so a canvas moved or resized reads as repainted."""
    return f"{name}@{','.join(str(round(v)) for v in box)}"


def canvas_crops(jpeg: bytes, viewport_width: int, canvases: Iterable[tuple[str, tuple[float, float, float, float]]]) -> dict[str, np.ndarray]:
    """The gray pixels each canvas shows on the frame *jpeg* (the page's viewport, *viewport_width* CSS px wide), read
    about :data:`READ_WIDTH` wide, by :func:`canvas_key`."""
    from PIL import Image

    with Image.open(io.BytesIO(jpeg)) as image:
        image.draft("L", (READ_WIDTH, max(1, round(READ_WIDTH * image.height / max(1, image.width)))))
        gray = np.asarray(image.convert("L"), dtype=np.int16)
    scale = gray.shape[1] / max(1, viewport_width)
    crops: dict[str, np.ndarray] = {}
    for name, (x, y, width, height) in canvases:
        left, top = max(0, math.floor(x * scale)), max(0, math.floor(y * scale))
        right, bottom = min(gray.shape[1], math.ceil((x + width) * scale)), min(gray.shape[0], math.ceil((y + height) * scale))
        if right - left >= 2 and bottom - top >= 2:
            crops[canvas_key(name, (x, y, width, height))] = gray[top:bottom, left:right]
    return crops


def changed_share(before: np.ndarray, after: np.ndarray, noise: int = NOISE) -> float:
    """The share of the pixels of two gray pictures of one size differing by more than *noise* levels."""
    return float(np.count_nonzero(np.abs(after - before) > noise)) / max(1, after.size)


def _centred(before: np.ndarray, after: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """*before* and *after* cut to the centre both share: the crops of an element whose box changed size."""
    height, width = min(before.shape[0], after.shape[0]), min(before.shape[1], after.shape[1])

    def cut(crop: np.ndarray) -> np.ndarray:
        top, left = (crop.shape[0] - height) // 2, (crop.shape[1] - width) // 2
        return crop[top : top + height, left : left + width]

    return cut(before), cut(after)


def is_empty(crop: np.ndarray) -> bool:
    """Whether a canvas shows nothing but one colour: fewer than :data:`EMPTY_SHARE` of its pixels stand off it."""
    ground = np.median(crop)
    return float(np.count_nonzero(np.abs(crop - ground) > NOISE)) / max(1, crop.size) < EMPTY_SHARE


@dataclass
class PageWatch:
    """What the reads of one hold saw (:meth:`observe`), in seconds of the take: when the page last moved, and the
    spans it showed loading."""

    #: When the hold began.
    began: float
    #: When the DOM last changed, a canvas last repainted, and the last read showing a progress indicator.
    dom_moved: float | None = None
    canvas_moved: float | None = None
    busy_at: float | None = None
    #: The longest interval between two frames that repainted a canvas: a page painting slowly stands still longer.
    gap: float = 0.0
    #: ``[start, end, [what showed]]`` of each span the page showed loading.
    loading: list[list] = field(default_factory=list)
    #: What the last read showed loading (empty: nothing), and when it was.
    shown: list[str] = field(default_factory=list)
    last: float | None = None
    _crops: dict[str, np.ndarray] = field(default_factory=dict)
    #: Whether the last span of :attr:`loading` runs on to the next read.
    _open: bool = False
    #: The looping elements the hold's first read showed, and those judged ambient since (:func:`loading_signature`).
    _from_start: set[str] | None = None
    ambient: list[str] = field(default_factory=list)
    #: The frame on which a looping element last moved, and the distinct frames its pixels were since compared on
    #: unchanged: one read still is no proof - a frame not repainted since (a starved runner's screencast) or one a
    #: whole turn later shows a spinner as it was - so it stands still only once :data:`LOOP_STILL_FRAMES` fresh frames
    #: spanning :data:`STILL_SECONDS` show it unchanged.
    loop_moved: float | None = None
    _loop_still: set[float] = field(default_factory=set)

    def observe(self, read: PageRead, crops: dict[str, np.ndarray] | None = None, *, frame_t: float | None = None, gap: float = 0.0) -> None:
        """Take one read of the page and the pixels of its canvases on the frame painted at *frame_t*, *gap* after
        the frame before it."""
        t = read.t
        if self._from_start is None:
            self._from_start = {name for name, _box in read.looping}
        if self._open:
            self.loading[-1][1] = round(t, 2)
        self.dom_moved = max(self.dom_moved if self.dom_moved is not None else -math.inf, t if read.changed is None else read.changed)
        if read.busy:
            self.busy_at = t
        shows = list(read.busy)
        seen: dict[str, np.ndarray] = {}
        for name, box in read.canvases:
            key = canvas_key(name, box)
            crop = (crops or {}).get(key)
            if crop is None:
                continue
            seen[key] = crop
            before = self._crops.get(key)
            if before is None or before.shape != crop.shape or changed_share(before, crop) > CHANGE_SHARE:
                if before is not None:
                    self.gap = max(self.gap, min(gap, MOST_STILL_SECONDS))
                painted = t if frame_t is None else frame_t
                self.canvas_moved = max(self.canvas_moved if self.canvas_moved is not None else -math.inf, painted)
            if is_empty(crop):
                shows.append(f"{name}{EMPTY_MARK}")
        # An element looping an animation whose pixels moved since the last read: a progress indicator. Kept by
        # its name alone - a spinner turning changes its box with its angle - and compared on the centre both share.
        # One with no loading signature, shown since the hold began, on a page whose DOM stands still: ambient.
        still = self.dom_moved is None or t - self.dom_moved >= STILL_SECONDS
        if not read.looping:
            self.loop_moved = None
            self._loop_still.clear()
        loop_unchanged: set[float] = set()
        for name, box in read.looping:
            crop = (crops or {}).get(canvas_key(f"{_LOOP_KEY}{name}", box))
            if crop is None:
                continue
            key = f"{_LOOP_KEY}{name}"
            seen[key] = crop
            before = self._crops.get(key)
            if before is None:
                continue
            painted = t if frame_t is None else frame_t
            if changed_share(*_centred(before, crop), LOOP_NOISE) <= CHANGE_SHARE:
                loop_unchanged.add(painted)
            else:
                if still and name in self._from_start and not loading_signature(name):
                    if name not in self.ambient and len(self.ambient) < MOST_LOOPING:
                        self.ambient.append(name)
                    continue
                self.loop_moved = painted if self.loop_moved is None else max(self.loop_moved, painted)
                self._loop_still.clear()
                self.busy_at = t
                shows.append(f"{name}{LOOPING_MARK}")
        if self.loop_moved is not None:
            self._loop_still.update(p for p in loop_unchanged if p > self.loop_moved)
        self._crops = seen
        if shows and self._open:
            names = self.loading[-1][2]
            names.extend(name for name in shows if name not in names)
            del names[_MOST_NAMES:]
        elif shows and len(self.loading) < _MOST_SPANS:
            self.loading.append([round(t, 2), round(t, 2), shows[:_MOST_NAMES]])
            self._open = True
        else:
            self._open = False
        self.shown = shows
        self.last = t

    def still_needed(self) -> float:
        """How long the canvases must stand still: :data:`STILL_SECONDS`, twice their paint interval when longer."""
        return max(STILL_SECONDS, min(MOST_STILL_SECONDS, 2 * self.gap))

    def settled_at(self, t: float) -> float | None:
        """When the page settled - the last instant something on it moved, never before the hold began - once it has
        stood still long enough at *t*; ``None`` while it shows a progress indicator or still moves."""
        if self.last is None or self.busy_at == self.last:
            return None
        if self.dom_moved is not None and t - self.dom_moved < STILL_SECONDS:
            return None
        if self.canvas_moved is not None and t - self.canvas_moved < self.still_needed():
            return None
        if self._loop_unproven():
            return None
        moved = [m for m in (self.dom_moved, self.canvas_moved, self.busy_at) if m is not None]
        return max([self.began, *moved])

    def moving(self, t: float) -> list[str]:
        """What keeps the page from settling at *t*: the progress indicators the last read showed, its DOM changing,
        a canvas repainting."""
        if self.last is not None and self.busy_at == self.last:
            return [name for name in self.shown if not name.endswith(EMPTY_MARK)]
        out = []
        if self.dom_moved is not None and t - self.dom_moved < STILL_SECONDS:
            out.append("its DOM still changing")
        if self.canvas_moved is not None and t - self.canvas_moved < self.still_needed():
            out.append("a canvas still repainting")
        if self._loop_unproven():
            out.append("an animation not yet seen still")
        return out

    def _loop_unproven(self) -> bool:
        """Whether a looping element seen moving, still shown, has not yet stood still on frames spanning
        :data:`STILL_SECONDS`."""
        if self.loop_moved is None:
            return False
        return len(self._loop_still) < LOOP_STILL_FRAMES or max(self._loop_still) - self.loop_moved < STILL_SECONDS

    def close(self, t: float) -> None:
        """The hold ended at *t*: a span still loading runs to there."""
        if self._open:
            self.loading[-1][1] = round(t, 2)

    def logged(self, *, until_settled: bool, settled: float | None, seconds: float, ended: float) -> dict:
        """What the step's event keeps: the spans it showed loading and, held until its page settled, when it did."""
        out: dict[str, Any] = {}
        if self.loading:
            out["loading"] = [list(span) for span in self.loading]
        if self.ambient:
            out["ambient"] = list(self.ambient)
        if until_settled:
            out["until_settled"] = True
            out["settled"] = (
                {"at": round(settled, 2), "after": round(max(0.0, settled - self.began), 2), "seconds": seconds}
                if settled is not None
                else {"at": None, "waited": round(ended - self.began, 2), "seconds": seconds}
            )
        return out


@dataclass(frozen=True)
class Hold:
    """A hold step whose frames show its page still loading (:func:`loading_hold`)."""

    step: int
    action: str
    seconds: float
    span: tuple[float, float]
    shows: tuple[str, ...]
    #: A ``wait`` held until its page settled that gave up before it did.
    gave_up: bool = False


def loading_hold(event: dict, *, take_ambient: Iterable[str] = ()) -> Hold | None:
    """The seconds a hold step's frames show its page still loading, from its event's ``loading`` spans: all of a
    ``pause``'s or a ``highlight``'s, the span a ``wait`` ends on; ``None`` under :data:`SAMPLE_SECONDS`.

    *take_ambient*: the looping elements another hold of the same take showed as decoration (:func:`take_ambient`),
    struck here too - the pulse a page starts once loaded, first seen mid-hold on the wait that loads it."""
    if event.get("action") not in HOLD_ACTIONS or not event.get("ok", True):
        return None
    spans = _shown_loading(event, take_ambient)
    if not spans:
        return None
    settled = event.get("settled") or {}
    gave_up = bool(event.get("until_settled")) and settled.get("at") is None
    if event.get("action") == "wait":
        # A span's end is kept to the hundredth of a second.
        ended = float(event.get("t1") or 0.0)
        spans = [spans[-1]] if float(spans[-1][1]) >= ended - 0.01 else []
    seconds = sum(float(end) - float(start) for start, end, _shows in spans)
    if not spans or (seconds < SAMPLE_SECONDS and not gave_up):
        return None
    shows: list[str] = []
    for _start, _end, names in spans:
        shows.extend(str(name) for name in names if str(name) not in shows)
    return Hold(
        step=int(event.get("step") or 0),
        action=str(event.get("action")),
        seconds=round(seconds, 1),
        span=(float(spans[0][0]), float(spans[-1][1])),
        shows=tuple(shows[:_MOST_NAMES]),
        gave_up=gave_up,
    )


def _shown_loading(event: dict, take_ambient: Iterable[str] = ()) -> list[list]:
    """A hold step's logged ``loading`` spans, ``[start, end, names]``, the ambient loops struck from them
    (:func:`ambient_names`, *take_ambient*) and a span left with nothing dropped."""
    spans = [s for s in event.get("loading") or [] if isinstance(s, list) and len(s) == 3]
    if not spans:
        return []
    # Ambient loops are decoration, not loading: struck from the spans, a span left with nothing dropped.
    struck = [*ambient_names(event), *(name for name in take_ambient if not loading_signature(name))]
    if ambient := {f"{name}{LOOPING_MARK}" for name in struck}:
        spans = [
            [start, end, kept] for start, end, names in spans if (kept := [n for n in names if str(n) not in ambient])
        ]
    return spans


@dataclass(frozen=True)
class LoadingSpan:
    """Seconds of a take its page was still loading (:func:`loading_spans`), and what says so."""

    start: float
    end: float
    #: ``step 3 (click) opened a page that settled at 9.1 s``, ``step 4 (pause) showed span#bar[progressbar]``.
    why: str


def loading_spans(events: Iterable[dict]) -> list[LoadingSpan]:
    """Every stretch of a take its page was still loading, read from its action log alone: from a click (or a
    ``goto``) that opened a page or switched a tab (:func:`opened_page`) to the moment the ``wait`` held after it
    settled - its end when it gave up -, and each span a hold logged showing a progress indicator or an empty canvas,
    the ambient loops struck (:func:`take_ambient`). A click that stayed on its page and set a canvas moving (a graph
    re-laid out) is no load: the wait after it held until the motion ended, which is the content the scene shows, so
    only what a hold logged loading counts there. Sorted by start; the save holds a recording scene's kept seconds
    against them."""
    events = [event for event in events if isinstance(event, dict)]
    ambient = take_ambient(events)
    out: list[LoadingSpan] = []
    before: dict | None = None
    for event in events:
        action = event.get("action")
        if action == "marker":
            continue
        if event.get("ok", True) and action == "wait" and isinstance(event.get("settled"), dict):
            settled = event["settled"]
            ended = settled.get("at")
            end = float(ended if ended is not None else (event.get("t1") or 0.0))
            # Only a wait right after the step that opened the page: a wait held on a page already shown - an
            # animation a click started on it included - says nothing of a load before it.
            opener = before if before is not None and before.get("action") in {*_PAGE_CLICKS, "goto"} else None
            # A click that kept its route (its address changed in its query alone) counts only when its wait showed
            # a loading signal: else it set the view moving, it loaded nothing.
            signalled = bool(_shown_loading(event, ambient))
            if opener is not None and opened_page(opener) and (signalled or not _same_route(opener)):
                at = opener.get("ta")
                start = float(at if at is not None else (opener.get("t0") or 0.0))
                state = f"that settled at {end:.1f} s" if ended is not None else f"not settled by {end:.1f} s"
                if end > start:
                    out.append(
                        LoadingSpan(
                            round(start, 2),
                            round(end, 2),
                            f"step {opener.get('step')} ({opener.get('action')}) opened a page {state}",
                        )
                    )
        if event.get("ok", True) and action in HOLD_ACTIONS:
            for start, end, names in _shown_loading(event, ambient):
                shown = ", ".join(str(name) for name in list(names)[:_MOST_NAMES])
                if float(end) > float(start):
                    out.append(
                        LoadingSpan(
                            round(float(start), 2),
                            round(float(end), 2),
                            f"step {event.get('step')} ({action}) showed {shown}",
                        )
                    )
        before = event
    return sorted(out, key=lambda span: (span.start, span.end))


def loading_signature(name: str) -> bool:
    """Whether a looping element named *name* (``div.spinner``, ``span.MuiSkeleton-root``) reads as a loading
    indicator (:data:`LOADING_SIGNATURE`)."""
    return bool(LOADING_SIGNATURE.search(name))


def ambient_names(event: dict) -> list[str]:
    """The looping elements a hold step showed as decoration, not loading: those its recording logged ``ambient``,
    and - a take recorded before that rule - each element of its ``loading`` spans looping with no loading signature
    (:func:`loading_signature`) in the span the hold began on (within :data:`AMBIENT_LEAD`) and the one it ended on
    (within :data:`AMBIENT_TAIL`): shown from its first frame to its last."""
    found = [str(name) for name in event.get("ambient") or [] if isinstance(name, str)]
    spans = [s for s in event.get("loading") or [] if isinstance(s, list) and len(s) == 3 and isinstance(s[2], list)]
    if not spans:
        return found
    try:
        start = event.get("ta")
        began = float(start if start is not None else (event.get("t0") or 0.0))
        ended = float(event.get("t1") or 0.0)
        first, last = spans[0], spans[-1]
        if float(first[0]) - began > AMBIENT_LEAD or ended - float(last[1]) > AMBIENT_TAIL:
            return found
    except (TypeError, ValueError):
        return found
    for name in (str(n) for n in first[2]):
        bare = name[: -len(LOOPING_MARK)] if name.endswith(LOOPING_MARK) else None
        if bare and not loading_signature(bare) and name in (str(n) for n in last[2]) and bare not in found:
            found.append(bare)
    return found


def take_ambient(events: Iterable[dict]) -> list[str]:
    """The looping elements any hold of a take showed as decoration (:func:`ambient_names`): an element of the page
    proven ambient on one hold - shown from its first frame to its last with no loading signature - is the same
    element on every other hold of the take, the one it first appears on mid-hold included."""
    names: list[str] = []
    for event in events:
        if event.get("action") in HOLD_ACTIONS:
            names.extend(name for name in ambient_names(event) if name not in names)
    return names


def ambient_words(events: Iterable[dict]) -> str:
    """The take's answer on the decorative loops its holds showed (:func:`take_ambient`), a fact and no finding;
    ``""`` when none did."""
    names = take_ambient(events)
    if not names:
        return ""
    return (
        f"Ambient animation (not loading): {', '.join(names[:MOST_LOOPING])} loops on its page from the start to the "
        "end of a hold with no loading signature - decoration, never a hold on a loading page."
    )


def loading_holds(events: Iterable[dict]) -> list[Hold]:
    """Every hold step of a take whose frames show its page still loading (:func:`loading_hold`), the elements
    another of its holds showed as decoration struck from all of them (:func:`take_ambient`)."""
    events = list(events)
    ambient = take_ambient(events)
    return [hold for event in events if (hold := loading_hold(event, take_ambient=ambient)) is not None]


def hold_words(hold: Hold) -> str:
    """``step 5 (pause) holds on a loading page for 2.1 s (12.3-14.4 s: span#bar[progressbar])``."""
    gave_up = "; its wait gave up before the page settled" if hold.gave_up else ""
    looping = (
        "; an element animating without end with a loading signature (a spinner, a skeleton) reads as loading - "
        "one the page always shows without one is ambient and never flagged"
        if any(name.endswith(LOOPING_MARK) for name in hold.shows)
        else ""
    )
    return (
        f"step {hold.step} ({hold.action}) holds on a loading page for {hold.seconds:.1f} s "
        f"({hold.span[0]:.1f}-{hold.span[1]:.1f} s: {', '.join(hold.shows)}{gave_up}{looping})"
    )


#: What makes a hold show its page loaded.
REMEDY = (
    "record it again so its page has loaded before the hold: until_settled: true on the wait before it (or on the "
    "wait itself), a longer timeout where one gave up"
)


def holds_words(events: Iterable[dict]) -> str:
    """The take's answer on its holds showing a page still loading; ``""`` when none does."""
    holds = loading_holds(events)
    if not holds:
        return ""
    return f"Holds on a loading page: {'; '.join(hold_words(h) for h in holds)}. Before placing it in a video, {REMEDY}."


def settled_words(settled: Any) -> str:
    """How a ``wait`` held until its page settled ended, for its line of the action log; ``""`` for any other."""
    if not isinstance(settled, dict):
        return ""
    seconds = float(settled.get("seconds") or 0.0)
    if settled.get("at") is None:
        return f" (until settled: not settled when it gave up after {float(settled.get('waited') or 0.0):.1f} s)"
    after = float(settled.get("after") or 0.0)
    past = f", past its {seconds:g} s" if after > seconds else ""
    return f" (until settled: settled at {float(settled['at']):.1f} s, {after:.1f} s in{past})"


def snapshot_words(settled_after: float | None, watched: float, still: Iterable[str]) -> str:
    """What ``snapshot_browser_profile_page`` says of how long its page took to settle once it opened."""
    if settled_after is not None:
        return (
            f"It settled {settled_after:.1f} s after it opened (no progress indicator shown, its page still for "
            f"{STILL_SECONDS:g} s): a wait after the step that opens it holds about that long, or until_settled: true."
        )
    shown = ", ".join(still) or "its page kept changing"
    return (
        f"It had not settled {watched:.0f} s after it opened ({shown}): a wait after the step that opens it, "
        "until_settled: true, gives up after its seconds and its timeout."
    )
