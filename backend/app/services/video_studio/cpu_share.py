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

"""The CPUs a whole render keeps beside the preview lane and the recordings.

An ultra render on 8 pages of 8 CPUs drew at 0.3 frames a second, 0.30x its pre-flight: its pages got 3.7 of its
worker's 8 CPUs while 2 scene previews and a recording ran beside it, and 12 more previews waited. Every job ran at
the worker's priority, and the kernel shares a busy container's CPUs by runnable thread: a render's 8 pages and their
encoders weighed no more than the previews' pages and encoders and the recording's browser and encoder beside them.

* **Priority.** A scene preview's process and everything it starts run :data:`PREVIEW_NICE` steps below the worker's
  priority (``render_process.nice_for``), and so does a recording's encode (``runners.run_take``): they take the CPU
  the render's pages leave (:func:`shares`). A recording's capture keeps the worker's priority: it follows a live page
  in real time, and a capture starved of CPU drops the frames it records.
* **Reservation.** A whole render - or scene range - reserves its pages' CPUs on its worker
  (``render_process.cpus_reserved``), and the worker's preview slots shrink to what the rest holds
  (``render_process.preview_slots``, the preview adapter's ``slots_held_back``): no preview starts on the CPUs a render
  draws on; it goes to a worker with room, or waits there.
* **The share, said.** The status line says the CPUs the render's pages got beside the jobs sharing their worker
  (``render_pace.PaceWatch.line``), and the render's measure keeps it (``render_pace.PaceWatch.contention``): its time
  corrects the next estimates at the share it got (:func:`idle_equivalent`).
* **Priced.** The pre-flight prices a render against its worker's committed load - the previews and recordings
  running or queued, at their measured CPU (:func:`committed_load`) - and says the shared estimate beside the idle one
  (:class:`Shared`, :func:`shared_seconds`).
* **Who slows it.** ``get_video_jobs`` on a render lists the jobs on its worker with their owner and video, and which
  the caller may cancel (:func:`competitors`, :func:`competitors_words`) - a job the caller may not read by its kind
  alone, "of another user": never its owner, id, scene or video.
* **Every job class.** The jobs of the reconciler's shared pool - assignment runs, sub-agent tasks, flows,
  evaluations - run in the worker's own process, beside a render at its priority: the pre-flight prices them too
  (:func:`committed_load`, named by :func:`job_kind`), each at the CPU its worker measured beside its renders and
  previews over the last minute (:class:`WorkerCpu`, published with its presence), and at the render lane its worker
  gives (``cpu_lanes``: a cgroup weight, :func:`grouped_shares`, or a raised priority).
"""

from __future__ import annotations

import logging
import math
import statistics
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: How many steps below the worker's CPU priority a scene preview's process and a recording's encode run: a
#: thread there weighs 110 against the 1024 of a render page's (the kernel's ``sched_prio_to_weight``).
PREVIEW_NICE = 10
#: What each kind of job beside a render is called.
PREVIEW = "scene preview"
RECORDING = "recording"
RENDER = "render"
#: The CPUs a recording is counted for: its browser's page and its compositor, and its encode once captured. It runs in
#: the worker's own process, where its CPU is not told apart from the worker's: never measured, counted so.
RECORDING_CPUS = 2.0
#: How long a scene preview and a recording run, until this platform finished some (:func:`committed_load`).
DEFAULT_PREVIEW_SECONDS = 90.0
DEFAULT_RECORDING_SECONDS = 120.0
#: The finished jobs of a kind whose time and CPU are read, newest first.
MEASURED_JOBS = 20
#: The least share of the worker's CPU a render's time is set back to an idle worker's by (:func:`idle_equivalent`): a
#: render that got less was held back by more than the share it got, which its pace explains (``render_pace``).
MIN_SHARE = 0.25
#: The CPUs a job of the worker's own process (an assignment run) is counted for where its worker measured
#: none (a worker of an earlier release, a container without cgroup statistics).
IN_PROCESS_CPUS = 0.25
#: How long a job of the worker's own process is priced as running beside a render: all of it - such jobs come and go,
#: what is measured is the CPU they use together.
LASTS_THE_RENDER = math.inf
#: The span :class:`WorkerCpu` measures a worker's CPU over.
CPU_WINDOW_SECONDS = 60.0


def job_kind(adapter: str) -> str:
    """What a job of the orchestration *adapter* is called beside a render: ``assignment job``."""
    return f"{str(adapter).replace('_', ' ')} job"


def nice_of(kind: str) -> int:
    """The niceness the CPU-bound work of a job of *kind* runs at (:data:`PREVIEW_NICE` for a scene preview)."""
    return PREVIEW_NICE if kind == PREVIEW else 0


def weight(nice: int) -> float:
    """What a thread at *nice* weighs against the others when the CPUs are busy: 1024 at 0, a quarter less a step."""
    return 1024.0 / 1.25 ** int(nice)


def shares(demands: list[tuple[float, int]], cpus: float) -> list[float]:
    """The CPUs each of *demands* - ``(the CPUs it uses alone, its niceness)`` - gets of *cpus*.

    As the kernel shares them: each job is as many runnable threads as the CPUs it uses alone, each thread on one CPU at
    most, weighed by its niceness (:func:`weight`). Busy CPUs go to the heaviest threads first; under its demand, a job
    gets all it asks.
    """
    wanted = [max(0.0, float(demand)) for demand, _nice in demands]
    if cpus <= 0:
        return [0.0 for _ in wanted]
    if sum(wanted) <= cpus:
        return wanted
    weights = [weight(nice) for _demand, nice in demands]
    low, high = 0.0, 1.0 / min(w for w, demand in zip(weights, wanted, strict=True) if demand > 0)
    for _ in range(80):
        middle = (low + high) / 2
        if sum(demand * min(1.0, middle * w) for demand, w in zip(wanted, weights, strict=True)) > cpus:
            high = middle
        else:
            low = middle
    return [demand * min(1.0, low * w) for demand, w in zip(wanted, weights, strict=True)]


def grouped_shares(first: float, weight: int, rest: list[tuple[float, int]], cpus: float) -> list[float]:
    """The CPUs a cgroup of *weight* running a demand of *first* CPUs gets of *cpus*, beside its sibling of the default
    weight (``cpu_lanes.DEFAULT_WEIGHT``) running *rest* (``cpu_lanes``): ``[its CPUs, each of rest's]``.

    As the kernel splits a busy CPU between sibling cgroups: by their weights, a cgroup under its share giving the
    rest to the other; within the sibling, by thread and priority (:func:`shares`)."""
    from app.services.video_studio.cpu_lanes import DEFAULT_WEIGHT

    first = max(0.0, float(first))
    demand = sum(max(0.0, float(value)) for value, _nice in rest)
    if cpus <= 0:
        return [0.0, *[0.0 for _ in rest]]
    if first + demand <= cpus:
        return [first, *[max(0.0, float(value)) for value, _nice in rest]]
    fair = cpus * weight / (weight + DEFAULT_WEIGHT)
    got = first if first <= fair else (cpus - demand if demand <= cpus - fair else fair)
    return [got, *shares(rest, cpus - got)]


def _shares_beside(render_cpus: float, jobs: list[Job], cpus: float, lane: Any) -> list[float]:
    """``[the render's CPUs, each of jobs']`` on *cpus* at the render *lane* its worker gives (``cpu_lanes.Lane``):
    its cgroup leaf's weight, its raised priority, or the worker's own."""
    from app.services.video_studio.cpu_lanes import CGROUP, PRIORITY

    rest = [(job.cpus, job.nice) for job in jobs]
    kind = getattr(lane, "kind", None)
    if kind == CGROUP:
        return grouped_shares(render_cpus, int(lane.weight), rest, cpus)
    return shares([(render_cpus, int(lane.nice) if kind == PRIORITY else 0), *rest], cpus)


@dataclass(frozen=True)
class Job:
    """A job committed on a render's worker: running there now, or queued for its lane."""

    kind: str
    #: The CPUs it uses alone, and whether that was measured on the last jobs of its kind.
    cpus: float
    #: How long it still runs alone.
    seconds: float
    running: bool = True
    job: str = ""
    measured: bool = False

    @property
    def nice(self) -> int:
        return nice_of(self.kind)

    @property
    def lasting(self) -> bool:
        """A job of the worker's own process, priced as running beside the whole render (:data:`LASTS_THE_RENDER`)."""
        return not math.isfinite(self.seconds)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "cpus": round(self.cpus, 2),
            "seconds": None if self.lasting else round(self.seconds),
            "running": self.running,
            "job": self.job,
            "measured": self.measured,
            **({"lasting": True} if self.lasting else {}),
        }

    @classmethod
    def of(cls, value: Any) -> Job | None:
        if not isinstance(value, dict) or not value.get("kind"):
            return None
        try:
            return cls(
                str(value["kind"]),
                float(value.get("cpus") or 0.0),
                LASTS_THE_RENDER if value.get("lasting") else float(value.get("seconds") or 0.0),
                bool(value.get("running", True)),
                str(value.get("job") or ""),
                bool(value.get("measured")),
            )
        except (TypeError, ValueError):
            return None


def shared_seconds(
    idle_seconds: float,
    *,
    cpus: int,
    render_cpus: float,
    jobs: list[Job] | tuple[Job, ...],
    slots: dict[str, int],
    lane: Any = None,
) -> tuple[float, float]:
    """``(seconds, share)``: how long a render taking *idle_seconds* on *render_cpus* of an idle worker of *cpus* CPUs
    takes beside *jobs*, and the share of its pace it keeps on average.

    Each job runs its seconds at its CPUs, slower by the share it gets (:func:`shares`, at its priority; the render at
    its worker's render *lane*); a queued one starts once a slot of its kind frees - *slots*, ``{kind: slots}``:
    a preview in the slots the render leaves, a recording in the worker's -, and a kind *slots* does not name starts at
    once. A job of the worker's own process runs the whole render (:data:`LASTS_THE_RENDER`).
    """
    if idle_seconds <= 0 or render_cpus <= 0:
        return max(0.0, idle_seconds), 1.0
    running: list[list] = [[job, job.seconds] for job in jobs if job.running and job.seconds > 0]
    queued = [job for job in jobs if not job.running and job.seconds > 0]
    left, elapsed = 1.0, 0.0
    for _ in range(4 * len(jobs) + 4):
        for job in list(queued):
            limit = slots.get(job.kind)
            if limit is None or sum(1 for other, _left in running if other.kind == job.kind) < limit:
                running.append([job, job.seconds])
                queued.remove(job)
        if not running:
            break
        got = _shares_beside(render_cpus, [job for job, _left in running], cpus, lane)
        pace = got[0] / render_cpus
        paces = [share / job.cpus if job.cpus > 0 else 1.0 for share, (job, _left) in zip(got[1:], running, strict=True)]
        ends = [remaining / job_pace for (_job, remaining), job_pace in zip(running, paces, strict=True) if job_pace > 0]
        step = min([left * idle_seconds / pace if pace > 0 else math.inf, *ends])
        if not math.isfinite(step):
            break
        elapsed += step
        left -= step * pace / idle_seconds
        for item, job_pace in zip(running, paces, strict=True):
            item[1] -= step * job_pace
        running = [item for item in running if item[1] > 1e-6]
        if left <= 1e-9:
            return elapsed, idle_seconds / elapsed
    elapsed += max(0.0, left) * idle_seconds
    return elapsed, (idle_seconds / elapsed if elapsed > 0 else 1.0)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'s' if count != 1 else ''}"


def _joined(items: list[str]) -> str:
    return ", ".join(items[:-1]) + f" and {items[-1]}" if len(items) > 1 else (items[0] if items else "")


def minutes_words(seconds: float) -> str:
    """``41 min``, ``6 h 12 min``: a duration as an answer says it, to the minute above (``render_preflight._duration``,
    the ETA's ``render_pace.pace_line``)."""
    minutes = max(1, math.ceil(max(0.0, seconds) / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def counted(jobs: list[Job] | tuple[Job, ...]) -> str:
    """``3 scene previews and 1 recording``: *jobs* by kind."""
    kinds: dict[str, int] = {}
    for job in jobs:
        kinds[job.kind] = kinds.get(job.kind, 0) + 1
    return _joined([_plural(count, kind) for kind, count in kinds.items()])


@dataclass(frozen=True)
class Shared:
    """What a pre-flight priced its render against: its worker's committed load, and what the render takes beside
    it against on an idle worker."""

    idle_seconds: float
    seconds: float
    #: The share of its idle pace it keeps on average beside them.
    share: float
    cpus: int
    jobs: tuple[Job, ...] = ()
    #: The preview slots the render leaves on its worker (``render_process.preview_slots``), of the worker's.
    preview_slots: int = 0
    preview_capacity: int = 0
    #: The render lane its worker gives (``cpu_lanes.Lane.as_dict``); ``None`` where its worker did not say.
    lane: dict | None = None

    def headline(self) -> str:
        """``about 31 min on a worker shared with 3 scene previews (about 28 min on an idle worker)``."""
        if not self.jobs:
            return f"about {minutes_words(self.idle_seconds)} on an idle worker"
        return (
            f"about {minutes_words(self.seconds)} on a worker shared with {counted(self.jobs)} (about "
            f"{minutes_words(self.idle_seconds)} on an idle worker)"
        )

    def words(self) -> str:
        """What the load is made of, and what the render keeps of its worker beside it."""
        if not self.jobs:
            return ""
        parts = []
        for kind in dict.fromkeys(job.kind for job in self.jobs):
            same = [job for job in self.jobs if job.kind == kind]
            running = sum(1 for job in same if job.running)
            queued = len(same) - running
            state = _joined([text for text in (f"{running} running" if running else "", f"{queued} queued" if queued else "") if text])
            each = statistics.median(job.cpus for job in same)
            if all(job.lasting for job in same):
                basis = (
                    "what its worker's own process used beside its renders and previews over the last minute"
                    if all(job.measured for job in same)
                    else "counted so, its worker measuring none"
                )
                parts.append(f"{_plural(len(same), kind)} ({state}, about {each:.2f} CPUs each: {basis})")
                continue
            basis = (
                f"measured on its kind's last {MEASURED_JOBS} at most"
                if all(job.measured for job in same)
                else ("in the worker's own process, never measured apart" if kind == RECORDING else "unmeasured")
            )
            parts.append(f"{_plural(len(same), kind)} ({state}, about {each:.1f} CPUs each, {basis})")
        said = f"Its worker's committed load: {_joined(parts)}."
        if any(job.kind == PREVIEW for job in self.jobs):
            said += (
                f" Scene previews draw at background priority, and only in the preview slots a render leaves "
                f"({self.preview_slots} of {self.preview_capacity} beside this one)"
            )
            said += "; a recording records at the render's priority, in real time." if any(job.kind == RECORDING for job in self.jobs) else "."
        elif any(job.kind == RECORDING for job in self.jobs):
            said += " A recording records at the render's priority, in real time."
        if any(job.lasting for job in self.jobs):
            said += (
                " New jobs of that kind go to a worker drawing no render first, and run beside it on what its pages "
                "leave when none takes them."
            )
        from app.services.video_studio.cpu_lanes import Lane

        lane = Lane.of(self.lane)
        if lane is not None:
            said += f" On this worker {lane.words()}."
        said += f" Its pages keep about {self.share:.0%} of their idle pace on average beside them."
        return said

    def as_dict(self) -> dict:
        return {
            "idle_seconds": round(self.idle_seconds),
            "seconds": round(self.seconds),
            "share": round(self.share, 3),
            "cpus": self.cpus,
            "jobs": [job.as_dict() for job in self.jobs],
            "preview_slots": self.preview_slots,
            "preview_capacity": self.preview_capacity,
            **({"lane": dict(self.lane)} if self.lane else {}),
        }

    @classmethod
    def of(cls, value: Any) -> Shared | None:
        if not isinstance(value, dict) or not value.get("idle_seconds"):
            return None
        try:
            return cls(
                float(value["idle_seconds"]),
                float(value.get("seconds") or value["idle_seconds"]),
                float(value.get("share") or 1.0),
                int(value.get("cpus") or 0),
                tuple(job for job in (Job.of(item) for item in value.get("jobs") or []) if job is not None),
                int(value.get("preview_slots") or 0),
                int(value.get("preview_capacity") or 0),
                dict(value["lane"]) if isinstance(value.get("lane"), dict) else None,
            )
        except (TypeError, ValueError):
            return None


def priced(
    idle_seconds: float, *, cpus: int, pages: int, jobs: list[Job], recording_slots: int, lane: Any = None
) -> Shared:
    """A render taking *idle_seconds* on *pages* pages of a worker of *cpus* CPUs, priced beside *jobs*: its
    preview slots shrunk to what its pages leave (``render_process.preview_slots_beside``), its pages at the render
    *lane* its worker gives (``cpu_lanes.Lane``; the worker's own priority where it did not say)."""
    from app.services.video_studio.render_process import preview_slots_beside

    capacity, left = preview_slots_beside(pages, cpus)
    seconds, share = shared_seconds(
        idle_seconds,
        cpus=cpus,
        render_cpus=cpus,
        jobs=jobs,
        slots={PREVIEW: left, RECORDING: recording_slots},
        lane=lane,
    )
    return Shared(
        idle_seconds, seconds, share, cpus, tuple(jobs), left, capacity, lane.as_dict() if lane is not None else None
    )


# -- The committed load a pre-flight reads -----------------------------------------------------


def _median(values: list[float], default: float) -> float:
    kept = [value for value in values if value > 0 and math.isfinite(value)]
    return statistics.median(kept) if kept else default


def _seconds_between(started: Any, finished: Any) -> float:
    if not isinstance(started, datetime) or not isinstance(finished, datetime):
        return 0.0
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=UTC)
    return (finished - started).total_seconds()


def measured_cpus(measured: Any) -> float | None:
    """The CPUs a render or preview used on average (``measured["cpu_seconds"]`` over its ``total_seconds``)."""
    if not isinstance(measured, dict):
        return None
    try:
        cpu, total = float(measured.get("cpu_seconds") or 0.0), float(measured.get("total_seconds") or 0.0)
    except (TypeError, ValueError):
        return None
    return cpu / total if cpu > 0 and total > 0 else None


async def _typical(db: Any) -> dict[str, tuple[float, float | None]]:
    """``{kind: (seconds, CPUs or None)}`` of the last :data:`MEASURED_JOBS` scene previews and recordings finished."""
    from sqlalchemy import select

    from app.models.video_studio import JOB_READY, VideoRender, VideoTake

    previews = (
        await db.execute(
            select(VideoRender.started_at, VideoRender.finished_at, VideoRender.measured)
            .where(VideoRender.scene_id.is_not(None), VideoRender.status == JOB_READY)
            .order_by(VideoRender.finished_at.desc().nulls_last())
            .limit(MEASURED_JOBS)
        )
    ).all()
    takes = (
        await db.execute(
            select(VideoTake.started_at, VideoTake.finished_at)
            .where(VideoTake.status == JOB_READY)
            .order_by(VideoTake.finished_at.desc().nulls_last())
            .limit(MEASURED_JOBS)
        )
    ).all()
    cpus = [value for _started, _finished, measured in previews if (value := measured_cpus(measured)) is not None]
    return {
        PREVIEW: (
            _median([_seconds_between(started, finished) for started, finished, _measured in previews], DEFAULT_PREVIEW_SECONDS),
            _median(cpus, 0.0) or None,
        ),
        RECORDING: (_median([_seconds_between(started, finished) for started, finished in takes], DEFAULT_RECORDING_SECONDS), None),
    }


async def _render_workers() -> dict[str, dict] | None:
    """The workers that draw whole renders now and their presence records (``worker_presence``); ``None`` unknown."""
    from app.services import worker_presence
    from app.services.video_studio.jobs import RENDER_ADAPTER

    found = await worker_presence.present()
    if found is None:
        return None
    return {
        str(record["worker_id"]): record
        for record in found
        if record.get("worker_id")
        and record.get("state") == worker_presence.STATE_RUNNING
        and worker_presence.claims(record, RENDER_ADAPTER)
    }


def _beside_cpus(record: dict | None, jobs: int, recordings: int) -> float | None:
    """The CPUs each of the *jobs* of its own process the worker of *record* runs used over the last minute:
    what its container used beside its renders' and previews' processes (:class:`WorkerCpu`), less its *recordings*
    counted at :data:`RECORDING_CPUS`, over them; ``None`` where it measured none."""
    measured = record.get("cpu") if isinstance(record, dict) else None
    if not isinstance(measured, dict) or jobs <= 0:
        return None
    try:
        rest = float(measured.get("cpus") or 0.0) - float(measured.get("video_cpus") or 0.0)
    except (TypeError, ValueError):
        return None
    return max(0.0, rest - recordings * RECORDING_CPUS) / jobs


async def committed_load(db: Any, *, preview_cpus: float, now: datetime | None = None) -> list[Job]:
    """The jobs a render queued now shares its worker with: the scene previews and recordings that worker runs -
    the render worker running the fewest jobs, whole renders counted -, and its share of those queued, each at
    the CPUs and the time the last finished ones of its kind took (*preview_cpus* for a preview until one measured its
    CPU, :data:`RECORDING_CPUS` for a recording), less what a running one already ran; and the jobs of its own process
    it runs (assignment runs, sub-agent tasks, flows - named by :func:`job_kind`), each at what it measured them
    to use (:func:`_beside_cpus`, :data:`IN_PROCESS_CPUS` unmeasured) for the whole render. Where the workers that draw
    renders are not known (``worker_presence``), the workers running jobs stand for them: one worker's load, never every
    worker's at once. Read under a savepoint: what cannot be read is no load."""
    from sqlalchemy import func, select

    from app.models.orchestration import OrchestrationExecution
    from app.models.video_studio import VideoRender, VideoTake
    from app.services.video_studio import figure_stills
    from app.services.video_studio.jobs import (
        CLIP_ADAPTER,
        MUSIC_ADAPTER,
        PREVIEW_ADAPTER,
        RENDER_ADAPTER,
        TAKE_ADAPTER,
    )

    # AI shots and tracks wait on their provider; a figure still is drawn in a process of its own, like a preview.
    not_in_process = (PREVIEW_ADAPTER, TAKE_ADAPTER, RENDER_ADAPTER, CLIP_ADAPTER, MUSIC_ADAPTER, figure_stills.ADAPTER)
    now = now or datetime.now(UTC)
    try:
        async with db.begin_nested():
            typical = await _typical(db)
            rows = (
                await db.execute(
                    select(
                        OrchestrationExecution.adapter,
                        OrchestrationExecution.state,
                        OrchestrationExecution.lease_owner,
                        OrchestrationExecution.payload,
                    ).where(
                        OrchestrationExecution.adapter.in_((PREVIEW_ADAPTER, TAKE_ADAPTER, RENDER_ADAPTER)),
                        OrchestrationExecution.state.in_(("running", "pending")),
                    )
                )
            ).all()
            workers = await _render_workers()
            owners = (
                sorted(workers)
                if workers
                else sorted({str(owner) for _adapter, state, owner, _payload in rows if state == "running" and owner})
            )
            busy = {owner: sum(1 for _a, state, held, _p in rows if state == "running" and str(held) == owner) for owner in owners}
            target = min(owners, key=lambda owner: (busy[owner], owner)) if owners else None
            here = [
                (adapter, payload or {})
                for adapter, state, owner, payload in rows
                if adapter != RENDER_ADAPTER and state == "running" and str(owner) == target
            ]
            started = await _started_at(db, here, VideoRender, VideoTake)
            own: list[tuple[str, int]] = []
            if target is not None:
                counted_rows = await db.execute(
                    select(OrchestrationExecution.adapter, func.count())
                    .where(
                        OrchestrationExecution.lease_owner == target,
                        OrchestrationExecution.state == "running",
                        OrchestrationExecution.adapter.not_in(not_in_process),
                    )
                    .group_by(OrchestrationExecution.adapter)
                    .order_by(OrchestrationExecution.adapter)
                )
                own = [(str(adapter), int(count)) for adapter, count in counted_rows.all()]
    except Exception as exc:
        logger.info("Video render committed load not read: %s", format_exception_message(exc))
        return []
    spread = max(1, len(owners))
    found: list[Job] = []
    for adapter, payload in here:
        kind = PREVIEW if adapter == PREVIEW_ADAPTER else RECORDING
        seconds, cpus = typical[kind]
        job = str(payload.get("render_id") or payload.get("take_id") or "")
        ran = _seconds_between(started.get(job), now)
        found.append(_job(kind, cpus, preview_cpus, max(seconds * 0.1, seconds - ran), running=True, job=job))
    recordings = sum(1 for adapter, _payload in here if adapter == TAKE_ADAPTER)
    each = _beside_cpus((workers or {}).get(target or ""), sum(count for _adapter, count in own), recordings)
    for adapter, count in own:
        found.extend(
            Job(job_kind(adapter), IN_PROCESS_CPUS if each is None else each, LASTS_THE_RENDER, measured=each is not None)
            for _ in range(count)
        )
    for adapter, kind in ((PREVIEW_ADAPTER, PREVIEW), (TAKE_ADAPTER, RECORDING)):
        waiting = sum(1 for listed, state, _owner, _payload in rows if listed == adapter and state == "pending")
        seconds, cpus = typical[kind]
        found.extend(_job(kind, cpus, preview_cpus, seconds, running=False) for _ in range(math.ceil(waiting / spread)))
    return found


def _job(kind: str, measured: float | None, preview_cpus: float, seconds: float, *, running: bool, job: str = "") -> Job:
    if kind == RECORDING:
        return Job(kind, RECORDING_CPUS, seconds, running, job)
    return Job(kind, measured or preview_cpus, seconds, running, job, measured=measured is not None)


async def _started_at(db: Any, here: list[tuple[str, dict]], renders: Any, takes: Any) -> dict[str, datetime]:
    """``{job id: when it started}`` of the running jobs *here*."""
    from sqlalchemy import select

    found: dict[str, datetime] = {}
    for model, key in ((renders, "render_id"), (takes, "take_id")):
        ids = [parsed for _adapter, payload in here if (parsed := _uuid(payload.get(key))) is not None]
        if ids:
            rows = (await db.execute(select(model.id, model.started_at).where(model.id.in_(ids)))).all()
            found.update({str(job_id): when for job_id, when in rows if when is not None})
    return found


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


def _video_trees() -> dict[int, float]:
    """``{pid: CPU seconds}`` of each render and scene preview process this worker runs, everything it started
    included (``resources.tree_cpu_seconds``)."""
    from app.services.video_studio import render_process, resources

    found: dict[int, float] = {}
    for pid in render_process.attempt_pids():
        seconds = resources.tree_cpu_seconds(pid)
        if seconds is not None:
            found[pid] = seconds
    return found


def _container_seconds() -> float | None:
    from app.services.video_studio import resources

    stat = resources.cpu_stat()
    return stat.usage_seconds if stat is not None else None


class WorkerCpu:
    """What a worker's CPU went to over the last :data:`CPU_WINDOW_SECONDS`: all its container used, and of it
    what its renders' and scene previews' processes used - the rest is its own process's jobs' (assignment runs,
    sub-agent tasks, recordings), which no process of their own tells apart. Sampled each time its presence is
    written (the render adapter's ``presence``): what :func:`committed_load` prices those jobs at."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        container: Callable[[], float | None] = _container_seconds,
        trees: Callable[[], dict[int, float]] = _video_trees,
    ) -> None:
        self._clock, self._container, self._trees = clock, container, trees
        self._last: tuple[float, float, dict[int, float]] | None = None
        #: ``(seconds, the container's CPU seconds, the renders' and previews')`` of each span sampled.
        self._spans: deque = deque()

    def sample(self) -> dict | None:
        """``{cpus, video_cpus, seconds}`` over the spans of the last minute; ``None`` before two samples, or where the
        container's CPU cannot be read. A render that started within a span counts all its CPU in it."""
        now, used, trees = self._clock(), self._container(), self._trees()
        if used is None:
            return None
        last, self._last = self._last, (now, used, trees)
        if last is not None and now > last[0]:
            spent = max(0.0, used - last[1])
            video = sum(max(0.0, seconds - last[2].get(pid, 0.0)) for pid, seconds in trees.items())
            self._spans.append((now - last[0], spent, min(spent, video)))
        while len(self._spans) > 1 and sum(span for span, _spent, _video in self._spans) - self._spans[0][0] >= CPU_WINDOW_SECONDS:
            self._spans.popleft()
        seconds = sum(span for span, _spent, _video in self._spans)
        if seconds <= 0:
            return None
        return {
            "cpus": round(sum(spent for _span, spent, _video in self._spans) / seconds, 3),
            "video_cpus": round(sum(video for _span, _spent, video in self._spans) / seconds, 3),
            "seconds": round(seconds, 1),
        }


#: This worker's (the render adapter's ``presence``).
WORKER_CPU = WorkerCpu()


# -- What a render's time says once drawn ------------------------------------------------------


def idle_equivalent(took: float, contention: Any) -> float:
    """*took* - a render's whole time - set back to an idle worker's: the part of it it shared its worker
    (``measured["contention"]["overlap"]``; the whole of it on a record older than that) times the share of its
    worker's CPU its pages got (``cpu_share``, :data:`MIN_SHARE` at least), the rest as it is. A job beside it for
    seconds of an hour's render sets back seconds, never the hour. What the next estimates' correction reads
    (``render_history.estimate_error``)."""
    if not isinstance(contention, dict) or not contention.get("shared"):
        return took
    try:
        share = float(contention.get("cpu_share") or 0.0)
        overlap = float(contention["overlap"]) if contention.get("overlap") is not None else 1.0
    except (TypeError, ValueError):
        return took
    if share <= 0:
        return took
    overlap = min(1.0, max(0.0, overlap))
    return took * (1.0 - overlap * (1.0 - max(MIN_SHARE, min(1.0, share))))


#: The share of a render's time its pages may wait for a CPU and still not be short of one (``waited``).
WAITED_SHARE = 0.1
#: The CPUs a render's container leaves unused past which a render whose pages did not wait was held back elsewhere.
UNUSED_CPUS = 1.0


def _cpu_words(contention: dict) -> str:
    """``; the rest of its container used 0.35 CPUs and 3.6 went unused; its pages waited for a CPU 2% of the time``:
    what its container's CPU went to besides it, and how long it waited for one; empty unmeasured."""
    said = ""
    if contention.get("others_cpus") is not None:
        said += f"; the rest of its container used {float(contention['others_cpus']):.2f} CPUs"
        if float(contention.get("unused_cpus") or 0.0) >= 0.5:
            said += f" and {float(contention['unused_cpus']):.1f} went unused"
    if contention.get("waited") is not None:
        who = "its pages" if contention.get("waited_own") else "a task of its container"
        said += f"; {who} waited for a CPU {float(contention['waited']):.0%} of the time"
    return said


def contention_words(contention: Any, took: float, expected: Any) -> str:
    """``Its pages got 7.4 of the worker's 8 CPUs on average (92% of the CPU used), beside 2 scene previews; the rest of
    its container used 0.60 CPUs: about 29 min on an idle worker, what the next estimates are corrected by, against the
    28 min its idle estimate said.`` - and, where its pages left CPUs unused without waiting for one, that the
    jobs beside it did not hold it back."""
    if not isinstance(contention, dict) or contention.get("cpus_used") is None:
        return ""
    said = f"Its pages got {float(contention['cpus_used']):.1f} of the worker's {contention.get('cpus')} CPUs on average"
    if contention.get("cpu_share") is not None:
        said += f" ({float(contention['cpu_share']):.0%} of the CPU used)"
    beside = contention.get("neighbours") or {}
    if isinstance(beside, dict) and beside:
        said += ", beside " + _joined([_plural(int(count), str(kind)) for kind, count in beside.items()])
    said += _cpu_words(contention)
    if not contention.get("shared") or took <= 0:
        said += ", its worker its own."
    else:
        idle = idle_equivalent(took, contention)
        said += f": about {minutes_words(idle)} on an idle worker, what the next estimates are corrected by"
        idle_said = expected.get("idle_seconds") if isinstance(expected, dict) else None
        if idle_said:
            said += f", against the {minutes_words(float(idle_said))} its idle estimate said"
        said += "."
    if (
        float(contention.get("unused_cpus") or 0.0) >= UNUSED_CPUS
        and contention.get("waited") is not None
        and float(contention["waited"]) < WAITED_SHARE
    ):
        said += (
            " Its pages left CPUs unused without waiting for one: the jobs beside them did not hold them back, what "
            "they drew did."
        )
    return said


# -- The jobs slowing a render, for get_video_jobs ---------------------------------------------

#: The payload key of each job kind's execution, what it is called and the ``cancel_video_job`` argument naming it.
_KINDS = {
    "render_id": ("render", "render_ids"),
    "take_id": (RECORDING, "recording_ids"),
    "clip_id": ("AI shot", "clip_ids"),
    "music_id": ("AI track", "music_ids"),
}


#: Who started a job the caller may not read: never their name, their e-mail, the job's id, scene or video.
ANOTHER_USER = "another user"


@dataclass(frozen=True)
class Competitor:
    """A job running on a render's worker beside it: what it is, who started it, its video when the caller may
    open it, and whether the caller may cancel it - or, a job the caller may not read (``jobs.may_read_job``),
    its kind alone (:attr:`anonymous`)."""

    kind: str
    job: str = ""
    argument: str = ""
    #: Who started it; ``None`` for a job the caller may not read.
    owner: str | None = None
    #: Its video's title - or a recording's, a shot's or a track's name - when the caller may read it.
    video: str | None = None
    scene: str = ""
    yours: bool = False

    @property
    def nice(self) -> int:
        return nice_of(self.kind)

    @property
    def anonymous(self) -> bool:
        return self.owner is None

    def as_dict(self) -> dict:
        if self.anonymous:
            return {"kind": self.kind, "owner": ANOTHER_USER, "may_cancel": False, "background_priority": self.nice > 0}
        return {
            "kind": self.kind,
            "id": self.job,
            "owner": self.owner,
            "video": self.video,
            "scene": self.scene or None,
            "may_cancel": self.yours,
            "background_priority": self.nice > 0,
        }

    def words(self) -> str:
        """``scene preview `1a2b3c4d` of scene s03 of "Launch film", started by Alice Martin - yours to cancel:
        cancel_video_job(render_ids=['...'])``; ``a recording of another user`` for one the caller may not read."""
        if self.anonymous:
            return f"{'an' if self.kind[:1] in 'aeiou' else 'a'} {self.kind} of {ANOTHER_USER}"
        said = f"{self.kind} `{self.job[:8]}`"
        if self.scene:
            said += f" of scene {self.scene}"
        if self.kind in (PREVIEW, RENDER):
            said += f' of "{self.video}"' if self.video else " of a video you cannot open"
        elif self.video:
            said += f' "{self.video}"'
        said += f", started by {self.owner}"
        if self.yours:
            return f"{said} - yours to cancel: cancel_video_job({self.argument}=['{self.job}'])"
        return f"{said} - only {self.owner} can cancel it"


async def competitors(db: Any, render: Any, user: Any) -> list[Competitor]:
    """The jobs running on the worker of the whole render *render* beside it, as *user* is told them: who
    started each, its video and whether *user* may cancel it (``jobs.may_cancel``) only for a job *user* may read
    (``jobs.may_read_job``); any other its kind alone (:attr:`Competitor.anonymous`). The jobs, their owners
    and their videos are read in one query each. Empty for a render no worker runs."""
    from sqlalchemy import select

    from app.models.orchestration import OrchestrationExecution
    from app.models.user import User
    from app.models.video_studio import VideoClip, VideoMusic, VideoProject, VideoRender, VideoTake
    from app.services.video_studio import jobs

    if getattr(render, "scene_id", None) or getattr(render, "execution_id", None) is None:
        return []
    own = await db.get(OrchestrationExecution, render.execution_id)
    if own is None or own.state != "running" or not own.lease_owner:
        return []
    rows = (
        await db.execute(
            select(OrchestrationExecution.id, OrchestrationExecution.payload).where(
                OrchestrationExecution.lease_owner == own.lease_owner,
                OrchestrationExecution.state == "running",
                OrchestrationExecution.id != own.id,
            )
        )
    ).all()
    models = {"render_id": VideoRender, "take_id": VideoTake, "clip_id": VideoClip, "music_id": VideoMusic}
    labels = {"render_id": "render", "take_id": "take", "clip_id": "clip", "music_id": "music"}
    wanted: list[tuple[str, uuid.UUID]] = []
    for _execution_id, payload in rows:
        payload = payload if isinstance(payload, dict) else {}
        key = next((name for name in _KINDS if payload.get(name)), None)
        job_id = _uuid(payload.get(key)) if key else None
        if key is not None and job_id is not None and job_id != render.id:
            wanted.append((key, job_id))
    loaded: dict[tuple[str, uuid.UUID], Any] = {}
    for key, model in models.items():
        ids = sorted({job_id for listed, job_id in wanted if listed == key}, key=str)
        if ids:
            found_rows = (await db.execute(select(model).where(model.id.in_(ids)))).scalars().all()
            loaded.update({(key, row.id): row for row in found_rows})
    shown: list[tuple[str, Any, bool]] = []
    for key, job_id in wanted:
        row = loaded.get((key, job_id))
        if row is not None:
            shown.append((key, row, await jobs.may_read_job(db, labels[key], row, user)))
    people = {row.user_id for _key, row, readable in shown if readable and row.user_id}
    projects = {row.project_id for key, row, readable in shown if readable and key == "render_id"}
    names: dict[Any, str] = {}
    if people:
        named = await db.execute(select(User.id, User.full_name, User.email).where(User.id.in_(sorted(people, key=str))))
        names = {person_id: full_name or email or "someone" for person_id, full_name, email in named.all()}
    titles: dict[Any, str | None] = {}
    if projects:
        titled = await db.execute(
            select(VideoProject.id, VideoProject.title).where(VideoProject.id.in_(sorted(projects, key=str)))
        )
        titles = dict(titled.all())
    found: list[Competitor] = []
    for key, row, readable in shown:
        kind, argument = _KINDS[key]
        scene = str(getattr(row, "scene_id", None) or "")
        if key == "render_id":
            kind = PREVIEW if scene else RENDER
        if not readable:
            found.append(Competitor(kind))
            continue
        owner = names.get(row.user_id, "someone") if row.user_id else "nobody (the platform)"
        video = titles.get(row.project_id) if key == "render_id" else getattr(row, "name", None)
        found.append(Competitor(kind, str(row.id), argument, owner, video, scene, jobs.may_cancel(row, user)))
    return found


def competitors_words(found: list[Competitor]) -> str:
    """What ``get_video_jobs`` says of the jobs sharing a render's worker (:func:`competitors`): each with its
    owner and video and whether the caller may cancel it - the user's to decide -, one the caller may not read by its
    kind alone."""
    if not found:
        return ""
    said = f"Sharing its worker now: {'; '.join(item.words() for item in found)}."
    if any(item.nice for item in found):
        said += " Scene previews draw at background priority, on the CPU the render's pages leave."
    if any(item.kind == RECORDING for item in found):
        said += " A recording records at the render's priority, in real time."
    if any(item.yours for item in found):
        said += " Cancelling one of the user's own gives the render its CPUs back: ask them first."
    return said
