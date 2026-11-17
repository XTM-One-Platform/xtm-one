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

"""How a render's pages share its frames: runs of the same cost, then the frames left taken by idle pages.

A render used to cut its segments into one contiguous run a page by frame count: the 22 Oct 2026 ultra render of a
136 s video drew its 7,100 plain frames on 8 pages in 15 min, then its two screen-insert scenes - 780 frames - on
the 2 pages whose runs held them, for 40 min more, 6 pages idle and the render "running" at 81-94% with no end time.
Now:

* **Priced frames.** A frame costs a page what a frame of its kind (``render_preflight.FRAME_KINDS``) cost
  (:class:`FrameCosts`): what this render measured once it drew :data:`MIN_DRAWN_FRAMES` of the kind, else what the
  previous render of its video measured, else the platform's median (``render_preflight.page_costs``).
  :class:`FramePrices` counts each kind over any range at once.
* **Runs of the same cost.** The segments left are shared out in contiguous runs of the same cost, one a page, a
  segment a run ends inside cut on that frame (:func:`cost_runs`): a segment of insert frames is shared like the rest.
* **The tail, frame by frame.** A page whose run is done takes the next piece no page has started - the last of the
  run with the most work left -, and once every piece has started, the second half of the piece with the most work
  left, cut on a frame (:meth:`PageWork.take`): in the tail the unit of work is a frame range, never a whole scene.
  A cut leaves each side :data:`MIN_SPLIT_FRAMES` frames and the page taking it :data:`MIN_SPLIT_SECONDS` of
  frames: a page idles only once what is left is not worth an encoder of its own.
* **Into a matted insert, warmed.** A page that starts a piece inside a matted screen insert anywhere but where it
  left off first draws the :data:`PREROLL_FRAMES` frames before it, uncaptured (:meth:`PageWork.preroll`): the bezel
  lock's temporal prior and the matte's window then read what one page drawing on would have read.
* **Estimated the same way.** :func:`simulate` plays the schedule over priced frames: the pre-flight's time on the
  pages (``render_preflight.page_run_seconds``) and the ETA of the status line (:func:`status_line`).
"""

from __future__ import annotations

import heapq
import math
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.services.video_studio.render_preflight import DEFAULT_KIND_COST, FRAME_KINDS, FrameKinds
from app.services.video_studio.surface_refine import PRIOR_SETTLE_FRAMES

#: A cut leaves each side this many frames at least ...
MIN_SPLIT_FRAMES = 2
#: ... and the page taking the second half this many seconds of frames: below it, what is left is not worth an
#: encoder of its own, and the page that has it finishes it.
MIN_SPLIT_SECONDS = 5.0
#: The frames a page draws, uncaptured, before a piece it starts inside a matted screen insert anywhere but where it
#: left off: as many as the bezel lock's temporal prior takes to agree with itself (``surface_refine``), more than the
#: matte's median reads behind the frame (``screen_insert.MATTE_WINDOW``). Its whole window (``PRIOR_FRAMES``) would
#: cost a page 15 insert frames a seam.
PREROLL_FRAMES = PRIOR_SETTLE_FRAMES
#: The frames of a kind this render draws before it prices that kind at its own measure.
MIN_DRAWN_FRAMES = 30
#: A kind neither drawn nor priced before costs its default share (``DEFAULT_KIND_COST``) of a kind priced, else of
#: this.
NOMINAL_SECONDS = 0.1
#: The window the status line's frames a second are read over.
RATE_WINDOW_SECONDS = 60.0

Span = tuple[int, int]


@dataclass(frozen=True)
class FrameCosts:
    """What one page takes for one frame of each kind - its sub-frames, its capture and its share of its encoder -,
    and for each segment it opens and closes beside its frames, in seconds."""

    seconds: dict[str, float] = field(default_factory=dict)
    segment: float = 0.0
    #: Where each kind's price comes from: ``drawn`` (this render), else what its prior said (``video``: the previous
    #: render of the same video; ``platform``: the platform's median), ``default`` for a kind nobody measured.
    sources: dict[str, str] = field(default_factory=dict)

    def of(self, kind: str) -> float:
        return self.seconds[kind] if kind in self.seconds else DEFAULT_KIND_COST.get(kind, 1.0) * NOMINAL_SECONDS

    @classmethod
    def priced(cls, prior: FrameCosts | None = None, drawn: dict[str, tuple[int, float]] | None = None) -> FrameCosts:
        """Each kind at what this render drew of it (*drawn*: ``{kind: (frames, page-seconds)}``) once it drew
        :data:`MIN_DRAWN_FRAMES` of it, else at *prior*'s price, else at its default share of a priced kind's."""
        seconds: dict[str, float] = {}
        sources: dict[str, str] = {}
        for kind in FRAME_KINDS:
            frames, spent = (drawn or {}).get(kind, (0, 0.0))
            if frames >= MIN_DRAWN_FRAMES and spent > 0:
                seconds[kind], sources[kind] = spent / frames, "drawn"
            elif prior is not None and prior.seconds.get(kind, 0.0) > 0:
                seconds[kind], sources[kind] = prior.seconds[kind], prior.sources.get(kind, "prior")
        reference = "plain" if "plain" in seconds else next(iter(seconds), None)
        unit = seconds[reference] / DEFAULT_KIND_COST.get(reference, 1.0) if reference else NOMINAL_SECONDS
        for kind in FRAME_KINDS:
            if kind not in seconds:
                seconds[kind], sources[kind] = DEFAULT_KIND_COST.get(kind, 1.0) * unit, "default"
        return cls(seconds, prior.segment if prior is not None else 0.0, sources)


class FramePrices:
    """How many frames of each kind any range of ``[first, end)`` holds, read at once: what a range costs a page."""

    def __init__(self, kinds: FrameKinds, first: int, end: int) -> None:
        self.first, self.end = first, max(first, end)
        labels = kinds.labels(self.first, self.end)
        self._sums: dict[str, list[int]] = {}
        for kind in FRAME_KINDS:
            running, sums = 0, [0]
            for label in labels:
                running += label == kind
                sums.append(running)
            self._sums[kind] = sums

    def _at(self, frame: int) -> int:
        return min(max(frame, self.first), self.end) - self.first

    def counts(self, a: int, b: int) -> dict[str, int]:
        low, high = self._at(a), self._at(b)
        return {kind: sums[high] - sums[low] for kind, sums in self._sums.items()} if high > low else dict.fromkeys(FRAME_KINDS, 0)

    def cost(self, a: int, b: int, costs: FrameCosts) -> float:
        low, high = self._at(a), self._at(b)
        if high <= low:
            return 0.0
        return sum((sums[high] - sums[low]) * costs.of(kind) for kind, sums in self._sums.items())

    def cut(self, a: int, b: int, share: float, costs: FrameCosts) -> int:
        """The first frame ``m`` in ``(a, b]`` where ``cost(a, m)`` reaches *share*; *b* when the range costs less."""
        if b <= a or self.cost(a, b, costs) < share:
            return b
        low, high = a + 1, b
        while low < high:
            middle = (low + high) // 2
            if self.cost(a, middle, costs) >= share:
                high = middle
            else:
                low = middle + 1
        return low


def contiguous_runs(todo: list[Span], pages: int) -> list[list[Span]]:
    """*todo* in contiguous runs of about the same frames, one a page: at most *pages*, never more than the pieces. A
    piece goes to the run whose share holds its middle."""
    if not todo:
        return []
    count = max(1, min(pages, len(todo)))
    total = sum(b - a for a, b in todo) or 1
    runs: list[list[Span]] = [[] for _ in range(count)]
    done = 0
    for a, b in todo:
        runs[min(count - 1, int((done + (b - a) / 2) * count / total))].append((a, b))
        done += b - a
    return [run for run in runs if run]


def cost_runs(
    todo: list[Span], pages: int, cost: Callable[[int, int], float], cut: Callable[[int, int, float], int]
) -> list[list[Span]]:
    """*todo* in *pages* contiguous runs of the same *cost*: a piece a run's end falls inside is cut on that frame
    (*cut*: ``(a, b, share) ->`` the frame where ``[a, frame)`` costs *share*), each side :data:`MIN_SPLIT_FRAMES`
    frames at least - a piece too short to cut goes to the side its middle is on."""
    if not todo:
        return []
    count = max(1, pages)
    share = sum(cost(a, b) for a, b in todo) / count
    if share <= 0:
        return contiguous_runs(todo, pages)
    runs: list[list[Span]] = [[]]
    done = 0.0
    for a, b in todo:
        while True:
            weight, boundary = cost(a, b), share * len(runs)
            if len(runs) >= count or done + weight <= boundary:
                runs[-1].append((a, b))
                done += weight
                break
            if b - a >= 2 * MIN_SPLIT_FRAMES:
                middle = cut(a, b, boundary - done)
                if middle - a < MIN_SPLIT_FRAMES and runs[-1]:
                    # The run ends where the piece starts.
                    runs.append([])
                    continue
                if b - middle < MIN_SPLIT_FRAMES:
                    # ... or where it ends.
                    runs[-1].append((a, b))
                    done += weight
                    runs.append([])
                    break
                middle = max(middle, a + MIN_SPLIT_FRAMES)
                runs[-1].append((a, middle))
                done += cost(a, middle)
                a = middle
                runs.append([])
                continue
            if done + weight / 2 <= boundary:
                runs[-1].append((a, b))
                runs.append([])
            else:
                if runs[-1]:
                    runs.append([])
                runs[-1].append((a, b))
            done += weight
            break
    return [run for run in runs if run]


@dataclass(eq=False)
class Piece:
    """Frames ``[first, end)`` a page draws and encodes as one segment. *end* comes down when another page takes its
    tail (:meth:`PageWork.take`); *next* is the first frame its page has not started."""

    first: int
    end: int
    next: int = -1
    page: int | None = None

    def __post_init__(self) -> None:
        if self.next < 0:
            self.next = self.first

    @property
    def span(self) -> Span:
        return self.first, self.end


class PageWork:
    """The pieces a render's pages draw: each page's run first, then what the other pages have left.

    *todo* are the segments left to draw, *kept* the finished ones (:meth:`plan` returns both), *warm* the frame
    spans of the matted screen inserts (:meth:`preroll`). The pages draw at most as many pieces at once as there are.
    """

    def __init__(
        self,
        todo: list[Span],
        pages: int,
        prices: FramePrices,
        costs: FrameCosts,
        *,
        warm: Iterable[Span] = (),
        kept: Iterable[Span] = (),
    ) -> None:
        self.prices = prices
        self.costs = costs
        self.warm = sorted(warm)
        self.kept = sorted(kept)
        runs = cost_runs(todo, pages, self.cost, lambda a, b, share: prices.cut(a, b, share, self.costs))
        self.queues: list[deque[Piece]] = [deque(Piece(a, b) for a, b in run) for run in runs]
        self.pieces: list[Piece] = [piece for queue in self.queues for piece in queue]
        #: The piece each page draws, and the frame after the last one each page drew.
        self.drawing: dict[int, Piece] = {}
        self.left_off: dict[int, int] = {}
        #: Whole pieces taken from another page's run, pieces cut in two, and the page whose piece was cut last.
        self.moved = 0
        self.cuts = 0
        self.cut_from: int | None = None

    @property
    def pages(self) -> int:
        return len(self.queues)

    def cost(self, a: int, b: int) -> float:
        return self.prices.cost(a, b, self.costs)

    def left(self, page: int) -> float:
        """What *page* has left: the rest of the piece it draws and its run."""
        drawing = self.drawing.get(page)
        own = self.cost(drawing.next, drawing.end) if drawing is not None else 0.0
        return own + sum(self.cost(piece.first, piece.end) for piece in self.queues[page])

    def preroll(self, page: int, first: int) -> int:
        """The frames *page* draws uncaptured before starting at *first*: none where it left off or outside a matted
        insert, else up to :data:`PREROLL_FRAMES` of the insert before it."""
        if self.left_off.get(page) == first:
            return 0
        for a, b in self.warm:
            if a < first < b:
                return min(PREROLL_FRAMES, first - a)
        return 0

    def take(self, page: int) -> Piece | None:
        """The next piece *page* draws, the one it drew being done: its run's next, else the last piece of the run
        with the most work left, else the second half of the piece with the most work left; ``None`` once nothing
        left is worth taking."""
        done = self.drawing.pop(page, None)
        if done is not None:
            self.left_off[page] = done.end
        self.cut_from = None
        queue = self.queues[page]
        piece = queue.popleft() if queue else self._whole(page)
        if piece is None:
            piece = self._cut(page)
        if piece is not None:
            piece.page = page
            self.drawing[page] = piece
        return piece

    def _whole(self, page: int) -> Piece | None:
        holders = [other for other, queue in enumerate(self.queues) if queue and other != page]
        if not holders:
            return None
        self.moved += 1
        return self.queues[max(holders, key=self.left)].pop()

    def _cut(self, page: int) -> Piece | None:
        drawn = [(self.cost(piece.next, piece.end), other) for other, piece in self.drawing.items() if other != page]
        if not drawn:
            return None
        left, victim = max(drawn)
        piece = self.drawing[victim]
        # Both halves end together: the taker's half bears its encoder and its pre-roll.
        guess = self.prices.cut(piece.next, piece.end, left / 2, self.costs)
        setup = self.costs.segment + self.cost(guess - self.preroll(page, guess), guess)
        middle = self.prices.cut(piece.next, piece.end, (left + setup) / 2, self.costs)
        if middle - piece.next < MIN_SPLIT_FRAMES or piece.end - middle < MIN_SPLIT_FRAMES:
            return None
        if self.cost(middle, piece.end) < MIN_SPLIT_SECONDS:
            return None
        taken = Piece(middle, piece.end)
        piece.end = middle
        self.pieces.append(taken)
        self.cuts += 1
        self.cut_from = victim
        return taken

    def frames_left(self) -> int:
        """The frames no page has started yet."""
        return sum(piece.end - piece.next for piece in self.drawing.values()) + sum(
            piece.end - piece.first for queue in self.queues for piece in queue
        )

    def plan(self) -> list[Span]:
        """Every segment of the range in order, the kept ones with those drawn."""
        return sorted([piece.span for piece in self.pieces if piece.end > piece.first] + self.kept)

    def copy(self) -> PageWork:
        twin = PageWork.__new__(PageWork)
        twin.prices, twin.costs, twin.warm, twin.kept = self.prices, self.costs, list(self.warm), list(self.kept)
        copies = {id(piece): Piece(piece.first, piece.end, piece.next, piece.page) for piece in self.pieces}
        twin.queues = [deque(copies[id(piece)] for piece in queue) for queue in self.queues]
        twin.pieces = list(copies.values())
        twin.drawing = {page: copies[id(piece)] for page, piece in self.drawing.items()}
        twin.left_off = dict(self.left_off)
        twin.moved, twin.cuts, twin.cut_from = self.moved, self.cuts, None
        return twin

    def seconds_left(self) -> float:
        """How long the pages take for what is left, at :attr:`costs` (:func:`simulate`)."""
        return max(simulate(self), default=0.0)


def simulate(work: PageWork) -> list[float]:
    """When each page draws its last frame under *work*'s schedule, in seconds from now, every frame priced at its
    costs: a piece opened costs its segment and its pre-roll first, a piece being drawn goes on from its next frame.
    *work* itself is left as it is."""
    work = work.copy()
    pages = work.pages
    finish = [0.0] * pages
    began: dict[int, tuple[float, int]] = {}
    version = [0] * pages
    heap: list[tuple[float, int, int]] = []

    def start(page: int, now: float, piece: Piece, *, opened: bool) -> None:
        setup = 0.0 if opened else work.costs.segment + work.cost(piece.first - work.preroll(page, piece.first), piece.first)
        began[page] = (now + setup, piece.next)
        version[page] += 1
        heapq.heappush(heap, (now + setup + work.cost(piece.next, piece.end), version[page], page))

    def catch_up(now: float) -> None:
        for page, piece in work.drawing.items():
            since, origin = began[page]
            if now > since:
                piece.next = max(piece.next, work.prices.cut(origin, piece.end, now - since, work.costs))

    def take(page: int, now: float) -> None:
        piece = work.take(page)
        if piece is None:
            return
        victim = work.cut_from
        if victim is not None:
            since, origin = began[victim]
            version[victim] += 1
            heapq.heappush(heap, (since + work.cost(origin, work.drawing[victim].end), version[victim], victim))
        start(page, now, piece, opened=False)

    for page, piece in list(work.drawing.items()):
        start(page, 0.0, piece, opened=True)
    for page in range(pages):
        if page not in work.drawing:
            take(page, 0.0)
    while heap:
        now, seen, page = heapq.heappop(heap)
        if seen != version[page]:
            continue
        finish[page] = now
        catch_up(now)
        take(page, now)
    return finish


@dataclass
class Rate:
    """Frames drawn a second over the last :data:`RATE_WINDOW_SECONDS`: what the status line says of a render's pace."""

    samples: deque = field(default_factory=deque)

    def note(self, at: float, done: int) -> None:
        self.samples.append((at, done))
        while len(self.samples) > 2 and self.samples[1][0] <= at - RATE_WINDOW_SECONDS:
            self.samples.popleft()

    def per_second(self) -> float | None:
        if len(self.samples) < 2:
            return None
        (began, first), (now, last) = self.samples[0], self.samples[-1]
        return (last - first) / (now - began) if now > began else None


def _span(seconds: float) -> str:
    """``2 min 30 s``, ``45 s``, ``1 h 5 min``."""
    seconds = max(0, round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    if minutes:
        return f"{minutes} min {seconds} s" if seconds else f"{minutes} min"
    return f"{seconds} s"


def status_line(
    percent: int, *, left: int, per_second: float | None, seconds_left: float | None, now: datetime | None = None
) -> str:
    """``Drawing frames (87%): 1,104 left, 7.4 frames/s over the last minute, ETA 17:42 UTC (in about 2 min 30 s)``:
    read later than it was written, the ETA stays a time."""
    said = f"Drawing frames ({percent}%): {left:,} left"
    if per_second is not None:
        said += f", {per_second:.1f} frames/s over the last minute"
    if seconds_left is not None and math.isfinite(seconds_left):
        eta = (now or datetime.now(UTC)) + timedelta(seconds=max(0.0, seconds_left))
        said += f", ETA {eta:%H:%M} UTC (in about {_span(seconds_left)})"
    return said
