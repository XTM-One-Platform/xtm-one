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

"""The preview lane's dealer: one scene preview per video in turn, dealt when a slot claims.

A sweep of every scene of a video queues a preview per scene at once, and a
preview of another video asked after them must not wait for all of them.
Dealing the lane when a preview is queued, permuting the queued
previews' ``run_at`` values once, would not survive the first wait:
a preview that waits - for its video's cap, for its worker's memory, handed
to another worker (``defer(place="keep")``, a ``not_before`` window) - would
be skipped by the claim, which takes whatever is due next, so one video's
previews could sit behind another video's whole batch while the queue line,
counting ``run_at`` order, said "0 ahead".

So the lane is dealt at the moment a slot claims (:func:`deal_claims`, the
engine's ``claim_due(deal=...)``, the reconciler passing the adapter's
``deal_claims``):

* a render a stopped worker left (``jobs.RESUMING_STATES``), a preview that
  gave its lent render slot back (``jobs.SLOT_GIVEN_BACK_STATE``) and a
  preview handed to this worker (``claim_for``) first, in their order:
  their turn was dealt already;
* then one preview per video in turn (:func:`dealt`): the video with the
  fewest previews being made now first, then the one served longest ago (its
  latest preview started, within :data:`SERVED_WINDOW`; never served first),
  then the one whose first queued preview was asked first - each video's own
  previews in the order it asked them;
* a video running its cap of previews (``jobs.max_scene_previews_per_video``)
  is not claimed for until one of them ends (``jobs.preview_waits`` still
  refuses one two workers claim at once).

The queue line reads the same deal (:func:`place_of`): what a free slot takes
before a queued preview, by video, with the previews being made - "1 preview
of video Launch film ahead" -, never a bare count, and how many workers draw
previews (``preview_routing.lane_workers``): a deployment that runs one
reads "1 worker draws previews, 2 at a time". A preview that waited
:data:`LANE_WAIT_SECONDS` behind other videos' previews on such a lane says
once what a second preview worker gives (:func:`lane_advice`).
"""

from __future__ import annotations

import math
import uuid
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any

from sqlalchemy import func, select

from app.models.orchestration import OrchestrationExecution
from app.models.video_studio import JOB_READY, VideoProject, VideoRender

#: How far back a video's latest preview start is read for its turn: a video served earlier than that has waited longest.
SERVED_WINDOW = timedelta(hours=12)
#: A preview queued this long before it starts, while other videos have previews in a lane of one worker, says what a
#: second preview worker gives (:func:`lane_advice`).
LANE_WAIT_SECONDS = 120.0
#: The scene previews drawn last whose median draw time a queued preview's wait is estimated with
#: (:func:`draw_seconds`).
DRAW_SAMPLE = 20
#: A video asks the lane (:meth:`Lane.asking`) while a preview of it is being made or is due within this long:
#: one waiting for memory, a route or its video's cap is claimed again well within it
#: (``preview_routing.WAIT_POLL_SECONDS``, ``ROUTE_HOLD_SECONDS``, ``jobs.PREVIEW_WAIT_SECONDS``).
SHARE_HORIZON = timedelta(seconds=60)


@dataclass(frozen=True)
class Deal:
    """A lane's queue dealt (:func:`dealt`): the order free slots take it in, then the jobs of the videos running their
    cap of previews, which wait for one of theirs to end."""

    order: list
    held: list

    @property
    def jobs(self) -> list:
        return [*self.order, *self.held]


def _turn(served: Any) -> tuple:
    """When a video was last served, as its turn sorts it: never first, then the earliest, then those served in this deal."""
    if served is None:
        return (0, 0.0)
    if isinstance(served, tuple):
        return served
    return (1, served.timestamp())


def dealt(
    queue: Sequence[tuple[Any, Any]],
    running: Mapping[Any, int],
    served: Mapping[Any, datetime | None] | None = None,
    *,
    cap: int | None = None,
    caps: Mapping[Any, int] | None = None,
) -> Deal:
    """The jobs of *queue* - ``(job, video)`` in the order asked - dealt one per video in turn.

    Each turn goes to the video with the fewest jobs *running* - those it
    already has, and those dealt before in this deal -, then to the one
    *served* longest ago (its latest start; never first), then to the one
    whose first job was asked first. A video's own jobs keep their order.
    With *cap*, a video running that many waits: its jobs are :attr:`Deal.held`;
    *caps* gives a video a cap of its own (its fair share, :meth:`Lane.caps`).
    """
    served = served or {}
    caps = caps or {}
    lines: dict[Any, deque] = {}
    first: dict[Any, int] = {}
    for position, (job, video) in enumerate(queue):
        lines.setdefault(video, deque()).append(job)
        first.setdefault(video, position)
    busy = {video: int(running.get(video, 0)) for video in lines}
    last: dict[Any, Any] = {video: served.get(video) for video in lines}
    limit = {video: caps.get(video, cap) for video in lines}
    order: list = []
    while lines:
        ready = [video for video in lines if limit[video] is None or busy[video] < limit[video]]
        if not ready:
            break
        video = min(ready, key=lambda v: (busy[v], _turn(last[v]), first[v]))
        order.append(lines[video].popleft())
        busy[video] += 1
        last[video] = (2, len(order))
        if not lines[video]:
            del lines[video]
    held = [job for video in sorted(lines, key=first.__getitem__) for job in lines[video]]
    return Deal(order, held)


@dataclass(frozen=True)
class VideoAhead:
    """The previews of one video a queued preview waits behind: those being made, and those dealt before it."""

    video_id: str
    title: str
    previews: int
    being_made: int
    #: The queued preview's own video.
    own: bool = False

    def words(self) -> str:
        """``2 previews of video Launch film (1 being made)``, ``1 of this video's previews (being made)``."""
        noun = "preview" if self.previews == 1 else "previews"
        said = f"{self.previews} of this video's previews" if self.own else f"{self.previews} {noun} of video {self.title}"
        if self.being_made:
            said += " (being made)" if self.being_made == self.previews else f" ({self.being_made} being made)"
        return said

    def as_dict(self) -> dict:
        return {
            "video_id": self.video_id,
            "title": self.title,
            "previews": self.previews,
            "being_made": self.being_made,
            "own": self.own,
        }


def ahead_words(videos: Sequence[VideoAhead]) -> str:
    """``1 preview of video Launch film ahead``: what a queued preview waits behind, video by video; empty for nothing."""
    parts = [video.words() for video in videos]
    if not parts:
        return ""
    said = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return f"{said} ahead"


@dataclass
class Lane:
    """The preview lane as one read sees it: its previews queued (``run_at`` order) and being made, by video."""

    pending: list
    running: list
    video_of: dict
    served: dict

    def video(self, execution: Any) -> Any:
        """The video of *execution*: its render's; a preview whose row is gone is a video of its own."""
        return self.video_of.get(execution.id, execution.id)

    def busy(self) -> dict[Any, int]:
        counts: dict[Any, int] = {}
        for execution in [*self.running, *self.resuming]:
            counts[self.video(execution)] = counts.get(self.video(execution), 0) + 1
        return counts

    @property
    def resuming(self) -> list:
        return [execution for execution in self.pending if first_in_line(execution)]

    @property
    def waiting(self) -> list:
        return [execution for execution in self.pending if not first_in_line(execution)]

    def asking(self, now: datetime | None = None) -> set:
        """The videos asking the lane for a slot now: a preview of each being made, or due within
        :data:`SHARE_HORIZON` - one waiting for memory, a route or its video's cap is claimed again by then."""
        until = (now or datetime.now(UTC)) + SHARE_HORIZON
        due = {self.video(e) for e in self.pending if (_aware(e.run_at) or until) <= until}
        return due | set(self.busy())

    def caps(self, slots: int | None, cap: int, now: datetime | None = None) -> dict:
        """Each video's cap in a deal of a lane of *slots*: while a video asking the lane holds fewer previews
        than its fair share (:func:`fair_share`), every one asking is held to that share - one video's batch never
        keeps the slots a preview of another, not claimable at that instant (waiting for memory, handed to another
        worker), is about to take. Once each holds its share, none: the slots left go to whoever is due."""
        videos = self.asking(now)
        share = fair_share(slots, len(videos), cap)
        if share >= cap:
            return {}
        busy = self.busy()
        if all(busy.get(video, 0) >= share for video in videos):
            return {}
        return dict.fromkeys(videos, share)


def fair_share(slots: int | None, videos: int, cap: int) -> int:
    """The previews one of *videos* asking a lane of *slots* runs at once: an even share of the slots, rounded
    up so none stays idle for want of a video, one at least, never past *cap*; *cap* while one video asks, or the
    slots are not known."""
    if not slots or videos <= 1:
        return cap
    return max(1, min(cap, math.ceil(slots / videos)))


def open_slots(workers: Any) -> int | None:
    """The preview slots the lane's *workers* (``preview_routing.LaneWorkers``) run now: theirs less those held back
    while a whole render draws on their CPUs; ``None`` when no worker said it."""
    if workers is None:
        return None
    return max(1, int(workers.slots) - int(getattr(workers, "held_back", 0) or 0))


async def queued_count(db: Any) -> int:
    """How many scene previews wait in the preview lane now: what a worker's added preview slots are sized to
    (``render_process.note_queued``), counted once a reconcile tick."""
    from app.services.video_studio.jobs import PREVIEW_ADAPTER

    counted = await db.execute(
        select(func.count())
        .select_from(OrchestrationExecution)
        .where(OrchestrationExecution.adapter == PREVIEW_ADAPTER, OrchestrationExecution.state == "pending")
    )
    return int(counted.scalar() or 0)


def first_in_line(execution: Any) -> bool:
    """Whether the pending preview of *execution* was dealt its turn already: a stopped worker left it, or it gave the
    render slot it was lent back - it goes back first, counted with the previews being made."""
    from app.services.video_studio.jobs import RESUMING_STATES, SLOT_GIVEN_BACK_STATE

    return execution.adapter_state in RESUMING_STATES or execution.adapter_state == SLOT_GIVEN_BACK_STATE


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _queued_order(execution: Any) -> tuple:
    run_at = _aware(execution.run_at) or datetime.min.replace(tzinfo=UTC)
    return (run_at, _aware(execution.created_at) or run_at)


async def read_lane(db: Any) -> Lane:
    """The preview lane now: its pending and running executions, the video of each, and when each video was last served."""
    from app.services.video_studio.jobs import PREVIEW_ADAPTER

    lane = OrchestrationExecution.adapter == PREVIEW_ADAPTER
    pending = (await db.execute(select(OrchestrationExecution).where(lane, OrchestrationExecution.state == "pending"))).scalars().all()
    running = (await db.execute(select(OrchestrationExecution).where(lane, OrchestrationExecution.state == "running"))).scalars().all()
    ids = [execution.id for execution in [*pending, *running]]
    video_of: dict = {}
    if ids:
        rows = (await db.execute(select(VideoRender).where(VideoRender.execution_id.in_(ids)))).scalars().all()
        video_of = {row.execution_id: row.project_id for row in rows}
    served: dict = {}
    videos = {video_of[execution.id] for execution in pending if execution.id in video_of}
    if videos:
        since = datetime.now(UTC) - SERVED_WINDOW
        started = (
            await db.execute(
                select(VideoRender).where(
                    VideoRender.project_id.in_(list(videos)),
                    VideoRender.scene_id.is_not(None),
                    VideoRender.started_at.is_not(None),
                    VideoRender.started_at >= since,
                )
            )
        ).scalars().all()
        for row in started:
            at = _aware(row.started_at)
            if at is not None and (row.project_id not in served or at > served[row.project_id]):
                served[row.project_id] = at
    return Lane(sorted(pending, key=_queued_order), list(running), video_of, served)


def _handed_to(execution: Any, worker_id: str) -> bool:
    from app.services.orchestration_engine import CLAIM_FOR_PAYLOAD_KEY

    handed = (execution.payload or {}).get(CLAIM_FOR_PAYLOAD_KEY)
    return isinstance(handed, dict) and str(handed.get("worker") or "") == str(worker_id)


async def deal_claims(db: Any, due: list, worker_id: str) -> list:
    """The *due* previews in the order a free slot of *worker_id* claims them: a resumed render and a preview
    handed to this worker first, then one preview per video in turn (:func:`dealt`); a video at its cap, or holding
    its fair share of the lane's slots while another video asking holds less (:meth:`Lane.caps`), left out."""
    from app.services.video_studio import preview_routing
    from app.services.video_studio.jobs import max_scene_previews_per_video

    if not due:
        return []
    lane = await read_lane(db)
    cap = max_scene_previews_per_video()
    caps = lane.caps(open_slots(await preview_routing.lane_workers()), cap)
    first = [e for e in due if first_in_line(e) or _handed_to(e, worker_id)]
    taken = {e.id for e in first}
    rest = sorted((e for e in due if e.id not in taken), key=_queued_order)
    deal = dealt([(e, lane.video(e)) for e in rest], lane.busy(), lane.served, cap=cap, caps=caps)
    return [*first, *deal.order]


async def lane_advice(db: Any, own: Any, workers: Any, *, now: datetime | None = None) -> Any:
    """The advice on the preview lane's sizing the preview of execution *own* gives as it starts
    (``render_preflight.Infra``): one worker draws previews (*workers*, ``preview_routing.lane_workers``), the preview
    was queued :data:`LANE_WAIT_SECONDS` or more, and other videos have previews in the lane - a second preview-only
    worker is the deployment's to add, nothing in the video. ``None`` otherwise, and when no worker said it."""
    from app.services.video_studio.render_preflight import Infra

    if workers is None or workers.workers != 1:
        return None
    queued = _aware(getattr(own, "created_at", None))
    if queued is None:
        return None
    waited = ((now or datetime.now(UTC)) - queued).total_seconds()
    if waited < LANE_WAIT_SECONDS:
        return None
    lane = await read_lane(db)
    mine = lane.video(own)
    others = {lane.video(execution) for execution in [*lane.running, *lane.pending] if execution.id != own.id} - {mine}
    if not others:
        return None
    return Infra(
        kind="preview_workers",
        key=f"preview_workers:{workers.workers}:{workers.slots}",
        text=(
            f"Scene previews of {len(others) + 1} videos share one worker's {workers.slots} preview slots: this one "
            f"waited {max(1, round(waited / 60))} min in the preview queue. Run a second worker that draws scene "
            "previews only (VIDEO_STUDIO_RENDER_CONCURRENCY=0: worker-preview in docker-compose.yml, a second worker "
            "Deployment on Kubernetes): the queue deals each video's next preview to whichever worker has a free slot. "
            "The video needs no change."
        ),
    )


@dataclass(frozen=True)
class PreviewPlace:
    """Where a queued preview stands in the dealt lane: the previews being made, those a free slot takes before it,
    and both by video (:class:`VideoAhead`)."""

    running: int
    ahead: int
    videos: tuple[VideoAhead, ...]


async def _titles(db: Any, videos: list) -> dict:
    keys = [video for video in videos if isinstance(video, uuid.UUID)]
    if not keys:
        return {}
    rows = (await db.execute(select(VideoProject).where(VideoProject.id.in_(keys)))).scalars().all()
    return {row.id: row.title for row in rows}


async def place_of(db: Any, own: Any, workers: Any = None) -> PreviewPlace:
    """Where the queued preview of execution *own* stands: the lane dealt as a free slot would deal it now,
    each video held to its fair share of the *workers*' slots as a claim holds it."""
    from app.services.video_studio.jobs import max_scene_previews_per_video

    lane = await read_lane(db)
    being_made = [*lane.running, *lane.resuming]
    if first_in_line(own):
        before: list = []
    else:
        queue = [(e, lane.video(e)) for e in lane.waiting]
        cap = max_scene_previews_per_video()
        jobs = dealt(queue, lane.busy(), lane.served, cap=cap, caps=lane.caps(open_slots(workers), cap)).jobs
        index = next((n for n, e in enumerate(jobs) if e.id == own.id), len(jobs))
        before = jobs[:index]
    mine = lane.video(own)
    counts: dict[Any, list[int]] = {}
    for execution in being_made:
        counts.setdefault(lane.video(execution), [0, 0])[1] += 1
    for execution in before:
        counts.setdefault(lane.video(execution), [0, 0])[0] += 1
    titles = await _titles(db, list(counts))
    videos = tuple(
        VideoAhead(
            video_id=str(video),
            title=titles.get(video) or str(video),
            previews=queued + making,
            being_made=making,
            own=video == mine,
        )
        for video, (queued, making) in counts.items()
    )
    return PreviewPlace(running=len(being_made), ahead=len(before), videos=videos)


# ── The lane measured ─────────────────────────────────────────────

#: How far back the lane's waits and draw times are read: the previews that started within it.
MEASURED_WINDOW = timedelta(hours=1)
#: The p95 wait, as a multiple of the median draw time, past which the lane is too small for what is asked of it.
SIZING_WAIT_FACTOR = 2.0


def percentile(values: Sequence[float], share: float) -> float | None:
    """The *share* (0-1) percentile of *values*, by nearest rank; ``None`` for none."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(share * len(ordered))) - 1]


@dataclass(frozen=True)
class LaneMeasure:
    """The preview lane as the last :data:`MEASURED_WINDOW` measured it: its workers and slots (``None``: none said),
    the previews queued now, and the waits and draw times of the previews that started."""

    workers: int | None
    slots: int | None
    queued: int
    measured: int
    median_wait_s: float | None
    p95_wait_s: float | None
    median_draw_s: float | None

    @property
    def undersized(self) -> bool:
        """Whether the p95 wait passed :data:`SIZING_WAIT_FACTOR` times the median draw time."""
        if self.p95_wait_s is None or not self.median_draw_s:
            return False
        return self.p95_wait_s > SIZING_WAIT_FACTOR * self.median_draw_s

    def sizing(self) -> str:
        """What the deployment should do about the lane's size, read off its measures."""
        if self.measured == 0:
            return "no scene preview started in the last hour: nothing measured"
        if not self.undersized:
            return "sized for what is asked: the p95 wait stays within twice the median draw time"
        return (
            f"too small for what is asked: the p95 wait ({self.p95_wait_s:.0f} s) passed twice the median draw time "
            f"({self.median_draw_s:.0f} s). Add a worker that draws scene previews only (VIDEO_STUDIO_RENDER_CONCURRENCY=0), "
            "or raise VIDEO_STUDIO_PREVIEW_CONCURRENCY on a worker whose CPUs are idle more than half the time"
        )

    def as_dict(self) -> dict:
        return {
            "workers": self.workers,
            "slots": self.slots,
            "queued": self.queued,
            "measured_previews": self.measured,
            "median_wait_s": _rounded(self.median_wait_s),
            "p95_wait_s": _rounded(self.p95_wait_s),
            "median_draw_s": _rounded(self.median_draw_s),
            "sizing": self.sizing(),
        }


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def measure(
    timings: Sequence[tuple[datetime, datetime, datetime | None]], *, workers: Any = None, queued: int = 0
) -> LaneMeasure:
    """The lane's :class:`LaneMeasure` from its previews' ``(asked, started, finished)`` times and its *workers*
    (``preview_routing.LaneWorkers``): each one's wait in the queue, and the draw time of each that finished."""
    waits = [max(0.0, (_aware(started) - _aware(asked)).total_seconds()) for asked, started, _finished in timings]
    draws = [
        max(0.0, (_aware(finished) - _aware(started)).total_seconds())
        for _asked, started, finished in timings
        if finished is not None
    ]
    return LaneMeasure(
        workers=getattr(workers, "workers", None),
        slots=getattr(workers, "slots", None),
        queued=queued,
        measured=len(waits),
        median_wait_s=percentile(waits, 0.5),
        p95_wait_s=percentile(waits, 0.95),
        median_draw_s=percentile(draws, 0.5),
    )


async def recent_timings(db: Any, *, now: datetime | None = None) -> list[tuple[datetime, datetime, datetime | None]]:
    """The ``(asked, started, finished)`` times of the scene previews that started in the last :data:`MEASURED_WINDOW`,
    less those served from a kept preview (``kept_previews``: never queued in the lane)."""
    since = (now or datetime.now(UTC)) - MEASURED_WINDOW
    rows = (
        await db.execute(
            select(VideoRender.created_at, VideoRender.started_at, VideoRender.finished_at, VideoRender.measured).where(
                VideoRender.scene_id.is_not(None),
                VideoRender.started_at.is_not(None),
                VideoRender.started_at >= since,
            )
        )
    ).all()
    return [
        (asked, started, finished)
        for asked, started, finished, measured in rows
        if asked is not None and not (isinstance(measured, dict) and measured.get("kept_from"))
    ]


async def recent_p95_wait(db: Any, *, now: datetime | None = None) -> float | None:
    """The p95 wait in the queue of the scene previews that started in the last :data:`MEASURED_WINDOW`: what a queued
    preview's answer says of the lane's current load beside its own expected wait; ``None`` when none started."""
    waits = [
        max(0.0, (_aware(started) - _aware(asked)).total_seconds())
        for asked, started, _finished in await recent_timings(db, now=now)
    ]
    return percentile(waits, 0.95)


async def read_measure(db: Any, workers: Any = None, *, now: datetime | None = None) -> LaneMeasure:
    """The lane measured over the last :data:`MEASURED_WINDOW` (:func:`measure`): the scene previews that started in
    it (:func:`recent_timings`)."""
    timings = await recent_timings(db, now=now)
    lane = await read_lane(db)
    return measure(timings, workers=workers, queued=len(lane.pending))


async def draw_seconds(db: Any) -> tuple[float, int] | None:
    """The lane's median draw time and how many previews it is the median of: the last :data:`DRAW_SAMPLE` drawn,
    whenever (a preview served from a kept one never drew) - what a queued preview's wait is estimated with
    (:func:`expected_wait`); ``None`` before any was drawn."""
    rows = (
        await db.execute(
            select(VideoRender.started_at, VideoRender.finished_at, VideoRender.measured)
            .where(
                VideoRender.scene_id.is_not(None),
                VideoRender.status == JOB_READY,
                VideoRender.started_at.is_not(None),
                VideoRender.finished_at.is_not(None),
            )
            .order_by(VideoRender.finished_at.desc())
            .limit(2 * DRAW_SAMPLE)
        )
    ).all()
    draws = [
        max(0.0, (_aware(finished) - _aware(started)).total_seconds())
        for started, finished, measured in rows
        if not (isinstance(measured, dict) and measured.get("kept_from"))
    ][:DRAW_SAMPLE]
    return (median(draws), len(draws)) if draws else None


def expected_wait(ahead: int, running: int, slots: int | None, draw_s: float | None) -> float | None:
    """Seconds until a free slot of a lane of *slots* takes a queued preview with *ahead* previews dealt before it
    while *running* are being made, each drawn in the lane's median time *draw_s*: none with a slot free for
    it; else the previews being made, half drawn on average, then a round of the slots for every *slots* previews
    ahead past the free ones. ``None`` when the slots or the draw time are not known."""
    if not slots or not draw_s:
        return None
    free = max(0, slots - running)
    if ahead < free:
        return 0.0
    return draw_s / 2 + ((ahead - free) // slots) * draw_s


def _minutes(seconds: float) -> str:
    return f"{round(seconds)} s" if seconds < 120 else f"{round(seconds / 60)} min"


def wait_words(wait_s: float | None, draw: tuple[float, int] | None, p95_s: float | None = None) -> str:
    """``about 20 min before it starts at the lane's pace (a scene preview drawn in about 90 s, the median of the
    last 20; the previews started in the last hour waited 25 min at the 95th percentile)``: what a queued preview
    says of its wait (:func:`expected_wait` on :func:`draw_seconds`) and of the lane's load (*p95_s*,
    :func:`recent_p95_wait`); empty with a slot free for it, or when it is not known."""
    if wait_s is None or wait_s <= 0 or draw is None:
        return ""
    seconds, previews = draw
    of = f"the median of the last {previews}" if previews > 1 else "the last one drawn"
    load = f"; the previews started in the last hour waited {_minutes(p95_s)} at the 95th percentile" if p95_s else ""
    return (
        f"about {_minutes(wait_s)} before it starts at the lane's pace (a scene preview drawn in about "
        f"{_minutes(seconds)}, {of}{load})"
    )
