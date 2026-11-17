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

"""Why a whole render's frames draw slower than its pre-flight said.

The 23 Oct 2026 ultra render of a video drew at 1.3 to 3.0 frames a second where its previous render - the same
scenes and inserts, on the same 8 pages of 8 CPUs - drew at 7.23: 2 h 42 min projected for the 28 min its
pre-flight said, and nothing said why. :class:`PaceWatch` follows a whole render's frames against its pre-flight:

* **Slow.** The frames drawn over the last minute against what the pre-flight priced *those* frames at - each at
  its scene type's cost (``Preflight.scene_raw_seconds``, corrected), on the pre-flight's pages, with its average
  frame's overhead (:class:`Priced`): a clip with a screen insert priced at twice a title never reads slow for
  drawing slower than a title.
* **Under half for 2 minutes** (:data:`SLOW_SHARE`, :data:`SLOW_SECONDS`): the status line names the cause
  (:func:`short_words`) and ``get_video_jobs`` says it whole (:func:`slow_words`): each scene type's cost a frame
  so far against its expected one, the share of its screen-insert frames read from the worker's cache, the other
  jobs on its worker (:func:`neighbours_of`) and the CPUs its pages got (``resources.tree_cpu_seconds``).
* **Under a third for 5 minutes** (:data:`CHECK_SHARE`, :data:`CHECK_SECONDS`): the worker's CPU throttling
  (``resources.cpu_stat``) and the render lanes' contention (:func:`lanes_of`) are checked once, and said in the
  stage text from then on.
* **Learned per scene type.** The render's measure keeps the breakdown (``VideoRender.measured["pace"]``,
  :meth:`PaceWatch.record`): its time is no estimate's error (:func:`explained`), and a render drawn on a shared
  worker prices the next pre-flights of its video with the scene types it found over their cost only
  (:func:`learned_scenes`): the others are priced as if it had not drawn them.
* **Its share, said.** The jobs beside it are read all along: beside any, the status line says the CPUs its
  pages get (:func:`share_words`), and its measure keeps those it got over the whole run (:meth:`PaceWatch.contention`),
  what its time corrects the next estimates by (``cpu_share.idle_equivalent``).
"""

from __future__ import annotations

import logging
import math
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.utils import format_exception_message
from app.services.video_studio import cpu_share, resources

logger = logging.getLogger(__name__)

#: A pace under this share of the pre-flight's, for :data:`SLOW_SECONDS`, is explained in the status line.
SLOW_SHARE = 0.5
SLOW_SECONDS = 120.0
#: A pace under this share, for :data:`CHECK_SECONDS`, checks the worker's throttling and the lanes once.
CHECK_SHARE = 1 / 3
CHECK_SECONDS = 300.0
#: The frames the pace is read over, as the status line's frames a second (``page_schedule.RATE_WINDOW_SECONDS``).
WINDOW_SECONDS = 60.0
#: The fewest seconds a window spans before its pace counts: the pages' start says nothing of their pace.
MIN_WINDOW_SECONDS = 30.0
#: A scene type is named when a frame of it took a page this many times its expected cost, and this many times what
#: the other scene types took against theirs: a worker that slows every frame alike is the worker, not a scene type.
OVER_COST = 2.0
#: The fewest frames a scene type drew before its cost a frame is set against its price.
MIN_FRAMES = 30
#: The share of the CPU the worker used that its pages got, under which the worker is shared.
SHARED_SHARE = 0.75
#: The share of its CPU quota's periods the worker was held back in, past which it is throttled.
THROTTLED_SHARE = 0.1
#: How often the other jobs on the worker are read again while the render is slow, and its CPU sampled.
NEIGHBOURS_EVERY_SECONDS = 60.0
CPU_EVERY_SECONDS = 10.0
#: The stage text's length (``VideoRender.stage``): the status line and the cause it names fit in it.
STAGE_ROOM = 200

#: What each job kind running beside a render is called (``jobs.enqueue``'s payload keys).
_NOUNS = {"render": "render", "music": "AI track", "clip": "AI shot", "take": "recording"}


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 and math.isfinite(number) else None


@dataclass(frozen=True)
class Priced:
    """What a render's pre-flight (``Preflight.as_dict``) priced its frames at: a page's seconds for a drawn frame of
    each scene type, corrected (:attr:`scene_seconds`) and before (:attr:`raw`), the overhead its average frame adds
    to them (its pages' efficiency, each segment's encoder), its pages and a frame's average on a page."""

    pages: int
    frame_seconds: float
    scene_seconds: dict
    raw: dict
    overhead: float = 1.0

    @classmethod
    def of(cls, expected: Any) -> Priced | None:
        if not isinstance(expected, dict):
            return None
        frame = _positive(expected.get("frame_seconds"))
        try:
            pages = int(expected.get("pages") or 0)
        except (TypeError, ValueError):
            return None
        if frame is None or pages <= 0:
            return None
        correction = _positive(expected.get("correction")) or 1.0
        raw = {
            str(name): value
            for name, cost in (expected.get("scene_raw_seconds") or {}).items()
            if (value := _positive(cost))
        }
        counts = expected.get("scenes") or {}
        per_frame = {
            str(name): value
            for name, cost in (expected.get("scene_seconds") or {}).items()
            if (value := _positive(cost))
        }
        total = sum(int(counts.get(name) or 0) for name in per_frame)
        mean = sum(int(counts.get(name) or 0) * cost for name, cost in per_frame.items()) / total if total else 0.0
        return cls(
            pages,
            frame,
            {name: cost * correction for name, cost in raw.items()},
            raw,
            frame / mean if mean > 0 else 1.0,
        )

    def wall_seconds(self, drawn: dict[str, int], frames: int, samples: int) -> float:
        """The seconds the pre-flight's pages take for *drawn* (``{scene type: drawn frames}``) - *frames* frames of
        the video, each of *samples* drawn frames, at its average frame's for a scene type it did not price."""
        samples = max(1, samples)
        seconds, counted = 0.0, 0
        for name, count in drawn.items():
            if count <= 0:
                continue
            counted += count
            cost = self.scene_seconds.get(name)
            seconds += count * (cost * self.overhead if cost else self.frame_seconds / samples)
        seconds += max(0, frames - counted // samples) * self.frame_seconds
        return seconds / self.pages


@dataclass(frozen=True)
class Neighbour:
    """A job running on the same worker as a render: what it is, its id and where it stands."""

    kind: str
    job: str
    stage: str = ""
    scene: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "job": self.job, "stage": self.stage, "scene": self.scene}

    @classmethod
    def of(cls, value: Any) -> Neighbour | None:
        if not isinstance(value, dict) or not value.get("kind"):
            return None
        return cls(
            str(value["kind"]),
            str(value.get("job") or ""),
            str(value.get("stage") or ""),
            str(value.get("scene") or ""),
        )

    def words(self) -> str:
        """``scene preview `1a2b3c4d` of scene s03``, ``render `1a2b3c4d` (Hearing and watching the render)``."""
        said = f"{self.kind} `{self.job[:8]}`" if self.job else self.kind
        if self.scene:
            said += f" of scene {self.scene}"
        return said + (f" ({self.stage})" if self.stage else "")


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'s' if count != 1 else ''}"


def neighbours_count(neighbours: list[Neighbour]) -> str:
    """``2 scene previews and 1 render``: the jobs beside a render, counted by kind."""
    counted: dict[str, int] = {}
    for item in neighbours:
        counted[item.kind] = counted.get(item.kind, 0) + 1
    return _joined([_plural(count, kind) for kind, count in counted.items()])


def _joined(items: list[str]) -> str:
    return ", ".join(items[:-1]) + f" and {items[-1]}" if len(items) > 1 else (items[0] if items else "")


async def neighbours_of(db: Any, owner: str, own_execution: object = None) -> list[Neighbour]:
    """The jobs the worker *owner* runs now beside the execution *own_execution*: its scene previews, its
    other renders at the stage they are at (their checks, their music), its AI tracks and shots, its recordings, and
    any other job it holds a lease on, by adapter."""
    from sqlalchemy import select

    from app.models.orchestration import OrchestrationExecution
    from app.models.video_studio import VideoRender
    from app.services.video_studio.jobs import PREVIEW_ADAPTER, RENDER_ADAPTERS

    rows = (
        await db.execute(
            select(OrchestrationExecution.id, OrchestrationExecution.adapter, OrchestrationExecution.payload).where(
                OrchestrationExecution.lease_owner == owner, OrchestrationExecution.state == "running"
            )
        )
    ).all()
    found: list[tuple[str, str, str]] = []
    renders: list[str] = []
    for execution_id, adapter, payload in rows:
        if own_execution is not None and str(execution_id) == str(own_execution):
            continue
        payload = payload if isinstance(payload, dict) else {}
        kind = next((name for name in _NOUNS if payload.get(f"{name}_id")), "")
        job = str(payload.get(f"{kind}_id") or "") if kind else ""
        if adapter in RENDER_ADAPTERS and job:
            renders.append(job)
        found.append((str(adapter), kind, job))
    stages: dict[str, tuple[str | None, str | None]] = {}
    if renders:
        import uuid

        ids = []
        for job in renders:
            try:
                ids.append(uuid.UUID(job))
            except ValueError:
                continue
        if ids:
            read = await db.execute(
                select(VideoRender.id, VideoRender.scene_id, VideoRender.stage).where(VideoRender.id.in_(ids))
            )
            stages = {str(render_id): (scene_id, stage) for render_id, scene_id, stage in read.all()}
    neighbours = []
    for adapter, kind, job in found:
        if kind == "render":
            scene_id, stage = stages.get(job, (None, None))
            preview = bool(scene_id) or adapter == PREVIEW_ADAPTER
            neighbours.append(
                Neighbour(
                    "scene preview" if preview else "render",
                    job,
                    "" if preview else str(stage or ""),
                    str(scene_id or ""),
                )
            )
        elif kind:
            neighbours.append(Neighbour(_NOUNS[kind], job))
        else:
            neighbours.append(Neighbour(cpu_share.job_kind(adapter), ""))
    return neighbours


async def lanes_of(db: Any, owner: str) -> dict[str, dict]:
    """``{lane: {here, slots, running, waiting}}`` of the render lanes: the jobs the worker *owner* runs on
    each against the slots it has, those every worker runs and those waiting for one."""
    from sqlalchemy import select

    from app.models.orchestration import OrchestrationExecution
    from app.services.video_studio import jobs

    rows = (
        await db.execute(
            select(
                OrchestrationExecution.adapter, OrchestrationExecution.state, OrchestrationExecution.lease_owner
            ).where(
                OrchestrationExecution.adapter.in_(jobs.RENDER_ADAPTERS),
                OrchestrationExecution.state.in_(("running", "pending")),
            )
        )
    ).all()
    lanes = {
        jobs.LANES[adapter]: {"here": 0, "slots": jobs.slots(adapter), "running": 0, "waiting": 0}
        for adapter in jobs.RENDER_ADAPTERS
    }
    for adapter, state, lease_owner in rows:
        lane = lanes.get(jobs.LANES.get(adapter, ""))
        if lane is None:
            continue
        if state == "pending":
            lane["waiting"] += 1
            continue
        lane["running"] += 1
        lane["here"] += lease_owner == owner
    return lanes


def _lease() -> tuple[str, str] | None:
    """``(the worker's id, the render's execution)`` its supervisor handed this render process (``lease``)."""
    from app.services.video_studio.lease import EXECUTION_ENV, OWNER_ENV

    owner = os.environ.get(OWNER_ENV, "").strip()
    return (owner, os.environ.get(EXECUTION_ENV, "").strip()) if owner else None


async def _read_neighbours() -> list[Neighbour] | None:
    from app.core.database import async_session

    lease = _lease()
    if lease is None:
        return None
    async with async_session() as db:
        return await neighbours_of(db, lease[0], lease[1] or None)


async def _read_lanes() -> dict | None:
    from app.core.database import async_session

    lease = _lease()
    if lease is None:
        return None
    async with async_session() as db:
        return await lanes_of(db, lease[0])


def lanes_words(lanes: dict) -> str:
    """``its render lane runs 1 of its 1 slot on this worker (2 on all workers, none waiting), its preview lane 2 of
    2 (3 waiting)``."""
    said = []
    for lane, held in lanes.items():
        slots = int(held.get("slots") or 0)
        waiting = int(held.get("waiting") or 0)
        said.append(
            f"its {lane} lane runs {int(held.get('here') or 0)} of its {_plural(slots, 'slot')} on this worker "
            f"({int(held.get('running') or 0)} on all workers, {waiting or 'none'} waiting)"
        )
    return _joined(said)


def _lanes_short(lanes: dict) -> str:
    return ", ".join(
        f"{lane} lane {int(held.get('here') or 0)}/{int(held.get('slots') or 0)}"
        + (f" +{int(held['waiting'])} waiting" if held.get("waiting") else "")
        for lane, held in lanes.items()
    )


def _duration(seconds: float) -> str:
    """``2 min 3 s``, ``45 s``, ``1 h 5 min``."""
    seconds = max(0, round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    if minutes:
        return f"{minutes} min {seconds} s" if seconds else f"{minutes} min"
    return f"{seconds} s"


class PaceWatch:
    """Follows a whole render's frames against its pre-flight's prices (the module's text): what the status
    line, ``get_video_jobs`` and the render's measure say of a pace under half the pre-flight's."""

    def __init__(
        self,
        expected: Any,
        *,
        pages: int,
        cpus: int,
        clock: Callable[[], float] = time.monotonic,
        cpu: Callable[[], float | None] | None = None,
        stat: Callable[[], resources.CpuStat | None] | None = None,
        neighbours: Callable[[], Awaitable[list[Neighbour] | None]] | None = None,
        lanes: Callable[[], Awaitable[dict | None]] | None = None,
        pressure: Callable[[], tuple[float, bool] | None] | None = None,
    ) -> None:
        self.priced = Priced.of(expected)
        self.pages, self.cpus = max(1, int(pages)), max(1, int(cpus))
        self._clock = clock
        self._cpu = cpu if cpu is not None else (lambda: resources.tree_cpu_seconds(os.getpid()))
        self._stat = stat if stat is not None else resources.cpu_stat
        self._neighbours = neighbours if neighbours is not None else _read_neighbours
        self._lanes = lanes if lanes is not None else _read_lanes
        #: Its cgroup's CPU pressure (``cpu_lanes.own_pressure`` from the render): none read unless given.
        self._pressure = pressure if pressure is not None else (lambda: None)
        #: ``(when, {scene type: frames drawn}, frames done)`` over the last :data:`WINDOW_SECONDS`.
        self._window: deque = deque()
        #: ``(when, the render's CPU seconds, the worker's CpuStat, its cgroup's CPU pressure)`` every
        #: :data:`CPU_EVERY_SECONDS` - the pressure ``(seconds waited, whether its cgroup is the render lane's)``.
        self._cpu_samples: deque = deque(maxlen=int(8 * 3600 / CPU_EVERY_SECONDS))
        self.ratio: float | None = None
        self.per_second: float | None = None
        self.worst: float | None = None
        self._slow_since: float | None = None
        self._third_since: float | None = None
        self._slow_before = 0.0
        self._neighbours_at: float | None = None
        self.neighbours: list[Neighbour] = []
        self._seen: dict[str, Neighbour] = {}
        self.cpus_used: float | None = None
        self.cpu_share: float | None = None
        #: The CPUs its pages got over the last :data:`WINDOW_SECONDS`, and their share of what the worker used.
        self.recent_cpus: float | None = None
        self.recent_share: float | None = None
        #: The most jobs of each kind seen beside it at once over the whole run (:meth:`contention`).
        self._most: dict[str, int] = {}
        #: The seconds a job was seen beside it: from each read that saw one to the next read.
        self._beside_seconds = 0.0
        self.checked: dict | None = None
        self._tracker: Any = None

    @property
    def slow(self) -> bool:
        """Whether the pace is under :data:`SLOW_SHARE` of the pre-flight's, and has been for :data:`SLOW_SECONDS`."""
        return self._slow_since is not None and self._clock() - self._slow_since >= SLOW_SECONDS

    @property
    def slow_seconds(self) -> float:
        """How long the pace was under half the pre-flight's, in spans of :data:`SLOW_SECONDS` at least."""
        return self._slow_before + (
            self._clock() - self._slow_since if self.slow and self._slow_since is not None else 0.0
        )

    async def observe(self, tracker: Any) -> None:
        """Read *tracker* (``compositor.DrawProgress``) once more: its pace against the pre-flight's, and while it is
        slow what says why. Never raises: what a status line says is never a reason to stop drawing."""
        try:
            await self._observe(tracker)
        except Exception as exc:
            logger.info("Video render pace not explained: %s", format_exception_message(exc))

    async def _observe(self, tracker: Any) -> None:
        now = self._clock()
        self._tracker = tracker
        if not self._cpu_samples or now - self._cpu_samples[-1][0] >= CPU_EVERY_SECONDS:
            self._cpu_samples.append((now, self._cpu(), self._stat(), self._pressure()))
        self._recent_over(now)
        # The jobs beside it, read all along: the status line says the share its pages got beside them.
        if self._neighbours_at is None or now - self._neighbours_at >= NEIGHBOURS_EVERY_SECONDS:
            if self._neighbours_at is not None and self.neighbours:
                self._beside_seconds += now - self._neighbours_at
            self._neighbours_at = now
            found = await self._neighbours()
            if found is not None:
                self.neighbours = list(found)
                for kind in dict.fromkeys(item.kind for item in found):
                    self._most[kind] = max(self._most.get(kind, 0), sum(1 for item in found if item.kind == kind))
        if self.priced is None:
            return
        self._window.append((now, dict(tracker.scene_frames), int(tracker.done)))
        while len(self._window) > 2 and self._window[1][0] <= now - WINDOW_SECONDS:
            self._window.popleft()
        (began, first, done_first), (_now, last, done_last) = self._window[0], self._window[-1]
        span = now - began
        if span < MIN_WINDOW_SECONDS:
            return
        drawn = {name: count - first.get(name, 0) for name, count in last.items()}
        priced = self.priced.wall_seconds(drawn, done_last - done_first, int(getattr(tracker, "samples", 1) or 1))
        if done_last <= done_first and priced <= 0:
            self.ratio = 0.0
        else:
            self.ratio = priced / span
        self.per_second = (done_last - done_first) / span
        self._follow(now)
        if not self.slow:
            return
        self.worst = self.ratio if self.worst is None else min(self.worst, self.ratio)
        self._seen.update({item.job or f"{item.kind}:{index}": item for index, item in enumerate(self.neighbours)})
        self._cpu_over(self._slow_since or now, now)
        if self.checked is None and self._third_since is not None and now - self._third_since >= CHECK_SECONDS:
            await self._check(self._third_since, now)

    def _follow(self, now: float) -> None:
        """Start or end the spans under half and under a third of the pre-flight's pace at *now*."""
        ratio = self.ratio if self.ratio is not None else 1.0
        if ratio < SLOW_SHARE:
            self._slow_since = now if self._slow_since is None else self._slow_since
        elif self._slow_since is not None:
            if now - self._slow_since >= SLOW_SECONDS:
                self._slow_before += now - self._slow_since
            self._slow_since = None
        if ratio < CHECK_SHARE:
            self._third_since = now if self._third_since is None else self._third_since
        else:
            self._third_since = None

    def _samples_over(self, since: float, now: float) -> tuple[tuple, tuple] | None:
        """The first and last CPU samples over ``[since, now]``, the one just before *since* included."""
        inside = [sample for sample in self._cpu_samples if sample[0] >= since - CPU_EVERY_SECONDS]
        if len(inside) < 2 or inside[-1][0] - inside[0][0] <= 0:
            return None
        return inside[0], inside[-1]

    @staticmethod
    def _cpu_between(first: tuple, last: tuple) -> tuple[float, float | None] | None:
        """``(the CPUs the render's pages got, their share of what the worker used)`` between two CPU samples."""
        (began, cpu_first, stat_first, *_), (ended, cpu_last, stat_last, *_) = first, last
        if cpu_first is None or cpu_last is None or ended <= began:
            return None
        used = max(0.0, cpu_last - cpu_first)
        share = None
        if (
            stat_first is not None
            and stat_last is not None
            and stat_first.usage_seconds is not None
            and stat_last.usage_seconds is not None
        ):
            total = stat_last.usage_seconds - stat_first.usage_seconds
            share = min(1.0, used / total) if total > 0 else None
        return used / (ended - began), share

    def _cpu_over(self, since: float, now: float) -> None:
        bounds = self._samples_over(since, now)
        found = self._cpu_between(*bounds) if bounds is not None else None
        if found is not None:
            self.cpus_used, self.cpu_share = found

    def _recent_over(self, now: float) -> None:
        bounds = self._samples_over(now - WINDOW_SECONDS, now)
        found = self._cpu_between(*bounds) if bounds is not None else None
        if found is not None:
            self.recent_cpus, self.recent_share = found

    def contention(self) -> dict | None:
        """What the render's measure keeps of the CPUs its pages got over the whole run (``measured
        ["contention"]``): those CPUs of its worker's, their share of what the worker used, the most jobs of each kind
        seen beside it at once, and whether it shared its worker - a job beside it, or its pages under
        :data:`SHARED_SHARE` of the CPU used - and for how much of the run (``overlap``: the share of it a job
        was seen beside it; the whole run when only its share says so). Where its container's CPU was read:
        what the rest of the container used (``others_cpus``) and the CPUs nobody in it used (``unused_cpus``); where
        its cgroup's CPU pressure was, the share of the run a task of it waited for a CPU (``waited``) and whether that
        cgroup was the render lane's own (``waited_own``). ``None`` where its CPU could not be read."""
        if len(self._cpu_samples) < 2:
            return None
        first, last = self._cpu_samples[0], self._cpu_samples[-1]
        found = self._cpu_between(first, last)
        if found is None:
            return None
        cpus_used, share = found
        run = last[0] - first[0]
        beside = self._beside_seconds
        if self.neighbours and self._neighbours_at is not None:
            beside += max(0.0, self._clock() - self._neighbours_at)
        overlap = min(1.0, beside / run) if self._most and run > 0 else 1.0
        record = {
            "cpus_used": round(cpus_used, 2),
            "cpus": self.cpus,
            "cpu_share": round(share, 3) if share is not None else None,
            "neighbours": dict(self._most),
            "shared": bool(self._most) or (share is not None and share < SHARED_SHARE),
            "overlap": round(overlap, 3),
            "seconds": round(run),
        }
        stats = (first[2], last[2])
        if run > 0 and all(stat is not None and stat.usage_seconds is not None for stat in stats):
            container = max(0.0, (stats[1].usage_seconds - stats[0].usage_seconds) / run)
            record["others_cpus"] = round(max(0.0, container - cpus_used), 2)
            record["unused_cpus"] = round(max(0.0, self.cpus - max(container, cpus_used)), 2)
        pressures = (first[3] if len(first) > 3 else None, last[3] if len(last) > 3 else None)
        if run > 0 and pressures[0] is not None and pressures[1] is not None:
            record["waited"] = round(min(1.0, max(0.0, (pressures[1][0] - pressures[0][0]) / run)), 3)
            record["waited_own"] = bool(pressures[1][1])
        return record

    async def _check(self, since: float, now: float) -> None:
        """Once: the worker's CPU throttling over the span under a third of the pace, and the render lanes."""
        checked: dict = {"after_seconds": round(now - since)}
        bounds = self._samples_over(since, now)
        stats = (bounds[0][2], bounds[1][2]) if bounds is not None else (None, None)
        if stats[0] is not None and stats[1] is not None:
            periods = stats[1].periods - stats[0].periods
            checked["periods"] = periods
            checked["throttled_share"] = (
                round((stats[1].throttled - stats[0].throttled) / periods, 3) if periods > 0 else 0.0
            )
            checked["throttled_seconds"] = round(max(0.0, stats[1].throttled_seconds - stats[0].throttled_seconds), 1)
            checked["over_seconds"] = round(bounds[1][0] - bounds[0][0])
        try:
            lanes = await self._lanes()
        except Exception as exc:
            logger.info("Video render lanes not read: %s", format_exception_message(exc))
            lanes = None
        if lanes is not None:
            checked["lanes"] = lanes
        self.checked = checked

    def _scenes(self) -> tuple[dict[str, list], list[str], float | None]:
        """``({scene type: [frames, seconds a frame took a page, its expected seconds]}, the scene types over their
        cost - the costliest first -, what the others took against their cost)``, of the frames drawn so far."""
        timed = self._tracker.scenes() if self._tracker is not None else {}
        rows: dict[str, list] = {}
        for name, (frames, seconds) in timed.items():
            expected = self.priced.raw.get(name) if self.priced is not None else None
            if frames >= MIN_FRAMES and expected:
                rows[name] = [int(frames), round(float(seconds) / frames, 3), round(expected, 3)]
        over = []
        for name, (_frames, took, expected) in rows.items():
            others = [(count, spent, cost) for other, (count, spent, cost) in rows.items() if other != name]
            priced = sum(count * cost for count, _spent, cost in others)
            alike = sum(count * spent for count, spent, _cost in others) / priced if others and priced > 0 else None
            # Alone, a scene type over its cost on a shared worker may be the worker's share: it is not named.
            beyond = (not self._shared()) if alike is None else took / expected >= OVER_COST * alike
            if took >= OVER_COST * expected and beyond:
                over.append(name)
        over.sort(key=lambda name: -(rows[name][1] - rows[name][2]) * rows[name][0])
        rest = [row for name, row in rows.items() if name not in over]
        cost = sum(frames * expected for frames, _took, expected in rest)
        alike = sum(frames * took for frames, took, _expected in rest) / cost if cost > 0 else None
        return rows, over, alike

    def _shared(self) -> bool:
        """Whether the worker's CPU was another's while the render was slow: jobs beside it, its pages under
        :data:`SHARED_SHARE` of what the worker used, or its quota holding it back."""
        if self.neighbours:
            return True
        if self.cpu_share is not None and self.cpu_share < SHARED_SHARE:
            return True
        return bool(self.checked and float(self.checked.get("throttled_share") or 0.0) >= THROTTLED_SHARE)

    def breakdown(self) -> dict | None:
        """What explains its pace once it was under half the pre-flight's for :data:`SLOW_SECONDS` - its
        ``preflight["slow"]`` while it draws, its ``measured["pace"]`` once drawn -; ``None`` while it never was."""
        seconds = self.slow_seconds
        if seconds <= 0 or self.priced is None:
            return None
        rows, over, alike = self._scenes()
        inserts = self._tracker.insert_frames() if self._tracker is not None else None
        expected_per_second = self.per_second / self.ratio if self.per_second and self.ratio else None
        return {
            "now": self.slow,
            "ratio": round(self.ratio, 2) if self.ratio is not None else None,
            "worst": round(self.worst, 2) if self.worst is not None else None,
            "slow_seconds": round(seconds),
            "per_second": round(self.per_second, 2) if self.per_second is not None else None,
            "expected_per_second": round(expected_per_second, 2) if expected_per_second else None,
            "pages": self.pages,
            "priced_pages": self.priced.pages,
            "cpus": self.cpus,
            "scenes": rows,
            "over": over,
            "alike": round(alike, 2) if alike is not None else None,
            "inserts": inserts,
            "neighbours": [item.as_dict() for item in (self.neighbours if self.slow else list(self._seen.values()))],
            "cpus_used": round(self.cpus_used, 2) if self.cpus_used is not None else None,
            "cpu_share": round(self.cpu_share, 3) if self.cpu_share is not None else None,
            "shared": self._shared(),
            "checked": self.checked,
        }

    def record(self) -> dict | None:
        """What the render's measure keeps (``measured["pace"]``): :meth:`breakdown` with every job seen beside it."""
        found = self.breakdown()
        if found is None:
            return None
        found["neighbours"] = [item.as_dict() for item in self._seen.values()]
        found["shared"] = bool(found["neighbours"]) or found["shared"]
        found["now"] = False
        return found

    def line(self, status: str) -> str:
        """*status* - the status line - with the cause of its pace while it is slow (:func:`short_words`), else, beside
        other jobs, the CPUs its pages get (:func:`share_words`)."""
        if not self.slow:
            if not self.neighbours or self.recent_cpus is None:
                return status
            return f"{status} - {share_words(self.recent_cpus, self.cpus, self.recent_share, self.neighbours)}"[:STAGE_ROOM]
        found = self.breakdown()
        if found is None:
            return status
        return f"{status} - {short_words(found, STAGE_ROOM - len(status) - 3)}"


def share_words(cpus_used: float, cpus: int, share: float | None, neighbours: list[Neighbour]) -> str:
    """``its pages get 7.6 of 8 CPUs (95% of the CPU used) beside 2 scene previews (previews at background priority)``:
    what a render's status line says of the worker it shares."""
    said = f"its pages get {cpus_used:.1f} of {cpus} CPUs"
    if share is not None:
        said += f" ({share:.0%} of the CPU used)"
    said += f" beside {neighbours_count(neighbours)}"
    if any(cpu_share.nice_of(item.kind) for item in neighbours):
        said += " (previews at background priority)"
    return said


def _neighbours(record: dict) -> list[Neighbour]:
    return [item for item in (Neighbour.of(value) for value in record.get("neighbours") or []) if item is not None]


def _scene_words(name: str) -> str:
    from app.services.video_studio.render_preflight import scene_words

    return scene_words(name)


def _checked_short(checked: dict) -> str:
    said = []
    if "throttled_share" in checked:
        said.append(f"throttled {float(checked['throttled_share']):.0%}")
    else:
        said.append("throttling unread")
    if checked.get("lanes"):
        said.append(_lanes_short(checked["lanes"]))
    return "checked: " + ", ".join(said)


def short_words(record: dict, room: int = STAGE_ROOM) -> str:
    """``0.25x the pre-flight's pace: figure 2.90 s a frame (expected 0.32 s); 2 scene previews on its worker; its
    pages got 3.1 of 8 CPUs``: the causes of :meth:`PaceWatch.breakdown`, the weightiest first, as many as *room*
    characters hold."""
    ratio = record.get("ratio")
    said = f"{float(ratio):.2f}x the pre-flight's pace" if ratio is not None else "slower than the pre-flight's pace"
    items = []
    scenes = record.get("scenes") or {}
    for name in (record.get("over") or [])[:2]:
        _frames, took, expected = scenes[name]
        items.append(f"{name} {took:.2f} s a frame (expected {expected:.2f} s)")
    if record.get("checked"):
        items.append(_checked_short(record["checked"]))
    if int(record.get("pages") or 0) < int(record.get("priced_pages") or 0):
        items.append(f"{record['pages']} of the {record['priced_pages']} pages it was priced on")
    if neighbours := _neighbours(record):
        items.append(f"{neighbours_count(neighbours)} on its worker")
    if record.get("cpus_used") is not None:
        items.append(f"its pages got {float(record['cpus_used']):.1f} of {record.get('cpus')} CPUs")
    inserts = record.get("inserts")
    if isinstance(inserts, dict) and (int(inserts.get("cached") or 0) + int(inserts.get("drawn") or 0)):
        items.append(f"insert frames {float(inserts.get('share') or 0.0):.0%} from the cache")
    if not record.get("over") and record.get("alike"):
        items.append(f"every scene type x{float(record['alike']):.1f} its cost")
    shown = []
    for item in items:
        if len(said) + len(": ") + len("; ".join([*shown, item])) > room:
            break
        shown.append(item)
    return f"{said}: {'; '.join(shown)}" if shown else said


def slow_words(record: Any, *, finished: bool = False) -> str:
    """What ``get_video_jobs`` and the render's answer say of a pace under half its pre-flight's: every cause
    of :meth:`PaceWatch.breakdown`, and once drawn what the next pre-flights learn from it."""
    if not isinstance(record, dict) or not record.get("slow_seconds"):
        return ""
    now = bool(record.get("now")) and not finished
    worst = record.get("ratio") if now else record.get("worst")
    measure = ""
    if worst is not None:
        measure = f" ({float(worst):.2f}x {'over the last minute' if now else 'at worst'}"
        if now and record.get("per_second") is not None and record.get("expected_per_second"):
            measure += (
                f", {float(record['per_second']):.1f} frames/s against the {float(record['expected_per_second']):.1f} "
                "it priced the frames drawn at"
            )
        measure += ")"
    said = f"Its frames {'have drawn' if now else 'drew'} under half the pre-flight's pace for {_duration(float(record['slow_seconds']))}{measure}: "
    causes = []
    scenes = record.get("scenes") or {}
    over = [name for name in record.get("over") or [] if name in scenes]
    for name in over:
        frames, took, expected = scenes[name]
        causes.append(
            f"{_scene_words(name)} {took:.2f} s a frame on a page against {expected:.2f} s expected (x{took / expected:.2f}, {int(frames):,} frames)"
        )
    if record.get("alike"):
        rest = "every other scene type" if over else "every scene type"
        causes.append(f"{rest} at x{float(record['alike']):.2f} its expected cost")
    if not scenes:
        causes.append("no scene type drew enough frames yet to set against its price")
    if int(record.get("pages") or 0) < int(record.get("priced_pages") or 0):
        causes.append(f"it draws on {record['pages']} pages, the pre-flight priced it on {record['priced_pages']}")
    inserts = record.get("inserts")
    if isinstance(inserts, dict) and (total := int(inserts.get("cached") or 0) + int(inserts.get("drawn") or 0)):
        causes.append(
            f"{float(inserts.get('share') or 0.0):.0%} of its screen-insert frames read from the worker's cache "
            f"({int(inserts.get('cached') or 0):,} of {total:,}), the rest drawn"
        )
    neighbours = _neighbours(record)
    shares = "shares" if now else "shared"
    if neighbours:
        causes.append(
            f"it {shares} its worker with {neighbours_count(neighbours)} ({_joined([item.words() for item in neighbours])})"
        )
    else:
        causes.append("no other job ran on its worker")
    if record.get("cpus_used") is not None:
        share = (
            f", {float(record['cpu_share']):.0%} of the CPU the worker used"
            if record.get("cpu_share") is not None
            else ""
        )
        causes.append(
            f"its pages got {float(record['cpus_used']):.1f} of the worker's {record.get('cpus')} CPUs{share}"
        )
    said += "; ".join(causes) + "."
    checked = record.get("checked")
    if isinstance(checked, dict):
        said += " " + checked_words(checked)
    if finished:
        if over:
            said += (
                f" The next pre-flights price {_joined([_scene_words(name) for name in over])} at what this render "
                "measured"
            )
        else:
            said += " The next pre-flights learn its scene types from what it measured"
        said += ", and hold none of its time against their estimates: its pace is explained per scene type and by its worker."
        if record.get("shared"):
            said += (
                " It was drawn on a shared worker: the next pre-flights of this video price its other scene types as if "
                "it had not drawn them."
            )
    return said


def checked_words(checked: dict) -> str:
    """``Checked once after 5 min under a third of the pre-flight's pace: the worker's CPU quota held it back in 41% of
    its periods (2 min 3 s of the last 5 min); its render lane runs ...``."""
    said = f"Checked once after {_duration(float(checked.get('after_seconds') or 0))} under a third of the pre-flight's pace: "
    if "throttled_share" in checked:
        share = float(checked["throttled_share"])
        if not int(checked.get("periods") or 0):
            said += "the worker has no CPU quota to hold it back"
        elif share > 0:
            said += (
                f"the worker's CPU quota held it back in {share:.0%} of its periods "
                f"({_duration(float(checked.get('throttled_seconds') or 0))} of the last {_duration(float(checked.get('over_seconds') or 0))})"
            )
        else:
            said += "the worker's CPU quota never held it back"
    else:
        said += "the worker's CPU throttling could not be read (no cgroup CPU statistics)"
    if checked.get("lanes"):
        said += f"; {lanes_words(checked['lanes'])}"
    return said + "."


def explained(measured: Any) -> bool:
    """Whether a render's measure says its pace was under half its pre-flight's (``measured["pace"]``): its
    time is no error of its estimate - its scene types are learned one by one, its worker's share is not the estimate's."""
    pace = measured.get("pace") if isinstance(measured, dict) else None
    return isinstance(pace, dict) and bool(pace.get("slow_seconds"))


def learned_scenes(measured: Any, names: Any) -> set[str]:
    """The scene types of *names* a render's measure prices the next pre-flights of its video with: all of
    them, but on a shared worker (``measured["pace"]["shared"]``) only those it found over their cost - the others'
    measures are the worker's share as much as their own cost."""
    names = {str(name) for name in names or ()}
    pace = measured.get("pace") if isinstance(measured, dict) else None
    if not isinstance(pace, dict) or not pace.get("shared"):
        return names
    return names & {str(name) for name in pace.get("over") or []}


# -- The ETA a render's measured pace gives (moved from render_preflight) --------------

#: The share of the frames a render draws before the pace they measured replaces its pre-flight's estimate.
PACE_SHARE = 0.1


@dataclass(frozen=True)
class Pace:
    """What a render's frames measured once :data:`PACE_SHARE` of those it draws were drawn
    (``compositor.DrawProgress.pace``): the frames drawn of all, its frames a second over the last minute, and the
    seconds its pages take for the rest at what each kind costs by then - the status line's ETA."""

    drawn: int
    frames: int
    per_second: float | None
    seconds_left: float

    def as_dict(self, expected: dict, now: datetime) -> dict:
        """Kept on the render's ``preflight`` (:func:`paced`): its frames' end and the render's - its checks after them,
        as *expected* said - as times, read later than they were written."""
        after = max(0.0, float(expected.get("check_seconds") or 0.0))
        frames_at = now + timedelta(seconds=max(0.0, self.seconds_left))
        return {
            "drawn": int(self.drawn),
            "frames": int(self.frames),
            "per_second": round(self.per_second, 2) if self.per_second else None,
            "measured_at": now.isoformat(),
            "frames_at": frames_at.isoformat(),
            "after_seconds": round(after),
            "at": (frames_at + timedelta(seconds=after)).isoformat(),
        }


def paced(expected: Any, pace: Pace, *, now: datetime | None = None) -> dict:
    """*expected* - a whole render's ``preflight``, or nothing - with *pace* kept as its ``pace``: what replaces
    its pre-flight's estimate in the render card and in ``get_video_jobs`` (:func:`eta_of`)."""
    kept = dict(expected) if isinstance(expected, dict) else {}
    kept["pace"] = pace.as_dict(kept, now or datetime.now(UTC))
    return kept


def unpaced(expected: Any) -> Any:
    """*expected* without the pace an earlier attempt of its render measured, nor what explained it
    (``slow``): each attempt measures its own."""
    if isinstance(expected, dict) and ("pace" in expected or "slow" in expected):
        return {key: value for key, value in expected.items() if key not in ("pace", "slow")}
    return expected


def _instant(value: Any) -> datetime | None:
    """*value* - a ``datetime`` or its ISO text - in UTC; ``None`` when it is neither."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def eta_of(render: Any) -> dict | None:
    """When an unfinished whole render should end: ``measured`` once :data:`PACE_SHARE` of its frames are drawn
    - the status line's ETA, its checks after it (:func:`paced`) -, else ``preflight`` - its start plus what its
    pre-flight expected, ``at`` ``None`` while no worker started it. ``None`` for a scene preview, a finished render,
    or one neither says anything of."""
    from app.models.video_studio import JOB_RUNNING, JOB_TERMINAL_STATUSES

    if getattr(render, "scene_id", None) or getattr(render, "status", None) in JOB_TERMINAL_STATUSES:
        return None
    expected = getattr(render, "preflight", None)
    expected = expected if isinstance(expected, dict) else {}
    said = expected.get("expected_seconds")
    pace = expected.get("pace")
    if isinstance(pace, dict) and _instant(pace.get("at")) is not None:
        return {
            "source": "measured",
            "at": pace["at"],
            "frames_at": pace.get("frames_at"),
            "drawn": pace.get("drawn"),
            "frames": pace.get("frames"),
            "per_second": pace.get("per_second"),
            "expected_seconds": said,
        }
    if said is None:
        return None
    started = _instant(getattr(render, "started_at", None)) if getattr(render, "status", None) == JOB_RUNNING else None
    at = (started + timedelta(seconds=float(said))).isoformat() if started is not None else None
    return {"source": "preflight", "at": at, "expected_seconds": int(said)}


def pace_line(eta: dict, started: Any) -> str:
    """``Measured on its first 846 of 8,462 frames (2.7 frames/s over the last minute): its frames end about 12:36 UTC
    and the render about 12:39 UTC with its checks, 1 h 19 min after it started - this replaces the pre-flight's
    estimate of about 32 min.``"""
    at, frames_at, began = _instant(eta.get("at")), _instant(eta.get("frames_at")), _instant(started)
    speed = f" ({float(eta['per_second']):.1f} frames/s over the last minute)" if eta.get("per_second") else ""
    said = f"Measured on its first {int(eta.get('drawn') or 0):,} of {int(eta.get('frames') or 0):,} frames{speed}: "
    if at is None:
        return ""
    if frames_at is not None and frames_at < at:
        said += f"its frames end about {frames_at:%H:%M} UTC and the render about {at:%H:%M} UTC with its checks"
    else:
        said += f"it ends about {at:%H:%M} UTC"
    if began is not None and at > began:
        said += f", {cpu_share.minutes_words((at - began).total_seconds())} after it started"
    if eta.get("expected_seconds"):
        said += f" - this replaces the pre-flight's estimate of about {cpu_share.minutes_words(float(eta['expected_seconds']))}"
    return said + "."
