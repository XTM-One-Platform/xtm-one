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

"""The recording lane of the render queue: a take its worker's load cannot capture at its fps waits, never refused.

A take measures its worker's capture rate before step 1 (``capture_rate``). Beside scene
previews and a whole render on the same worker, that rate is the load's, not the
worker's: 60 fps takes measured 10 to 26 fps at 3200x1800 beside two previews and a
render on 8 CPUs and were refused four times in a row, the author left to poll by hand
until the worker was idle - and ``fps_fallback: size`` cannot answer a CPU other jobs
hold. So the take adapter (``VideoTakeAdapter``) is the recording lane of the render
queue, beside its render and preview lanes (``jobs.LANES``):

* **Measured in its slot, at the recording's priority.** The warm-up runs once the lane
  granted the take its slot, in the worker's process, at the priority the take's capture
  records at: a scene preview's process runs ``cpu_share.PREVIEW_NICE`` steps below it
  (``render_process.nice_for``), so a preview yields its CPU to the warm-up and to the
  take, never the reverse. What ran beside it on its worker - the scene previews, renders
  and other recordings being made, read before and after the warm-up (:func:`load_on`,
  :class:`Load`) - is kept with what it measured (``preflight.beside``). The takes of one
  worker measure one at a time (:func:`measuring`): a take waiting for its warm-up is
  never another's load, and two warm-ups never measure each other.
* **Queued, not refused.** A take short of its fps beside other jobs records nothing and
  goes back to the lane in its place (``defer(place="keep")``, :data:`WAITING_STATE`), its
  execution keeping the wait (:class:`Wait`, :data:`WAIT_KEY`): since when, the worker,
  the jobs it measured beside and the rate. Claimed again every :data:`POLL_SECONDS`, it
  reads its worker's load - a query, no browser: no lighter than the one it fell short
  beside, it waits again (:func:`held`); lighter, idle or on another worker, it measures
  again. Measured on an idle worker, the warm-up decides as it always did - its own size,
  pixel ratio 1.5, its ``fps_fallback``, or the refusal naming the sizes, which the
  worker itself falls short of, not its load.
* **Said.** While it waits, its row is queued: its stage, and ``jobs.queue_place`` its
  place, the recordings ahead by name, the jobs it waits behind and what it measured
  beside them (:func:`line`). Once recorded, its notes and the first line of its action
  log say the wait (:meth:`Wait.waited_words`).
* **Bounded.** Past :data:`MAX_WAIT` it measures once more and is decided as on an idle
  worker: a refusal then says the jobs beside it ("busy: 2 scene previews and 1 render on
  this worker") and offers the lane again (``capture_rate.decide``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select

from app.core.database import async_session
from app.core.utils import format_exception_message
from app.models.orchestration import OrchestrationExecution
from app.services.video_studio import cpu_share

logger = logging.getLogger(__name__)

#: The most characters of a refusing worker's reason a queued take says (:func:`refusing_workers`): the whole of a
#: GPU lane's refusal, its two probes and its container evidence, with room to spare.
REFUSED_CHARS = 4000
#: The ``adapter_state`` of a take waiting in the recording lane for its worker's load.
WAITING_STATE = "recording_waiting"
#: Where its execution keeps the wait (:class:`Wait`).
WAIT_KEY = "recording_wait"
#: How often a waiting take is claimed again to read its worker's load (a query; a warm-up only once it lightened).
POLL_SECONDS = 15.0
#: The longest a take waits in the lane: past it, measured once more and decided as on an idle worker.
MAX_WAIT = timedelta(hours=6)
#: How often a relaunched take short of its fps on an idle worker (:attr:`Wait.relaunch`) measures again.
RELAUNCH_POLL_SECONDS = 60.0
#: The longest a relaunched take waits for a worker that sustains its fps (:attr:`Wait.relaunch`): past it, its
#: pre-flight refuses it as it refuses a first launch (#9812).
RELAUNCH_MAX_WAIT = timedelta(minutes=30)
#: The order the jobs beside a take are said in.
_ORDER = (cpu_share.PREVIEW, cpu_share.RENDER, cpu_share.RECORDING)
#: The executions of this process's takes measuring, or waiting to (:func:`measuring`), and the lock they take turns on.
_MEASURING: set[str] = set()
_LOCK: dict[str, Any] = {}


def _kinds() -> dict[str, str]:
    """``{adapter: kind}`` of the jobs a take's capture shares its worker's CPUs with (``cpu_share``'s kinds)."""
    from app.services.video_studio.jobs import PREVIEW_ADAPTER, RENDER_ADAPTER, TAKE_ADAPTER

    return {PREVIEW_ADAPTER: cpu_share.PREVIEW, RENDER_ADAPTER: cpu_share.RENDER, TAKE_ADAPTER: cpu_share.RECORDING}


@dataclass(frozen=True)
class Beside:
    """A job running on a take's worker beside it: its kind (``cpu_share``'s) and its execution."""

    kind: str
    execution: str


@dataclass(frozen=True)
class Load:
    """What ran on a take's worker beside it (:func:`load_on`): its scene previews, renders and other recordings."""

    jobs: tuple[Beside, ...] = ()

    @property
    def idle(self) -> bool:
        return not self.jobs

    @property
    def weight(self) -> float:
        """What its jobs weigh against the take for the worker's CPUs, each at its priority (``cpu_share.weight``): a
        render or a recording 1024, a scene preview 110."""
        return sum(cpu_share.weight(cpu_share.nice_of(job.kind)) for job in self.jobs)

    def words(self) -> str:
        """``2 scene previews and 1 render``."""
        return cpu_share.counted(self.jobs)

    def busy_words(self, worker: str = "this worker") -> str:
        """``busy: 2 scene previews and 1 render on this worker``."""
        return f"busy: {self.words()} on {worker}"

    def union(self, other: Load | None) -> Load:
        """Its jobs and *other*'s, each once: what ran beside a warm-up read before and after it."""
        if other is None:
            return self
        seen = {job.execution for job in self.jobs}
        return _ordered([*self.jobs, *(job for job in other.jobs if job.execution not in seen)])

    def as_dict(self) -> dict:
        return {"jobs": [[job.kind, job.execution] for job in self.jobs], "said": self.words()}

    @classmethod
    def of(cls, value: Any) -> Load | None:
        if not isinstance(value, dict):
            return None
        jobs = [Beside(str(item[0]), str(item[1])) for item in value.get("jobs") or [] if isinstance(item, list | tuple) and len(item) == 2]
        return _ordered(jobs)


def _ordered(jobs: Sequence[Beside]) -> Load:
    rank = {kind: index for index, kind in enumerate(_ORDER)}
    return Load(tuple(sorted(jobs, key=lambda job: (rank.get(job.kind, len(rank)), job.execution))))


def seen(before: Load | None, after: Load | None) -> Load | None:
    """What ran beside a warm-up, read *before* and *after* it; ``None`` when neither read could say."""
    if before is None:
        return after
    return before.union(after)


async def load_on(db: Any, worker_id: str, *, excluding: object = None) -> Load:
    """What runs on worker *worker_id* now beside the take of execution *excluding*: the scene previews, renders and
    recordings it holds the lease of, less the takes of this process measuring or waiting to (:func:`measuring`)."""
    kinds = _kinds()
    rows = (
        await db.execute(
            select(OrchestrationExecution).where(
                OrchestrationExecution.adapter.in_(list(kinds)),
                OrchestrationExecution.state == "running",
                OrchestrationExecution.lease_owner == str(worker_id),
            )
        )
    ).scalars().all()
    skip = {str(excluding), *_MEASURING}
    return _ordered([Beside(kinds[row.adapter], str(row.id)) for row in rows if str(row.id) not in skip])


def _lock() -> asyncio.Lock:
    """The lock this process's warm-ups take turns on, made for the running event loop."""
    loop = asyncio.get_running_loop()
    if _LOCK.get("loop") is not loop:
        _LOCK.update(loop=loop, lock=asyncio.Lock())
    return _LOCK["lock"]


@contextlib.asynccontextmanager
async def measuring(execution_id: object) -> AsyncIterator[None]:
    """The warm-up of the take of *execution_id*, one take of this worker at a time: while it measures or waits to,
    no take's load counts it (:func:`load_on`)."""
    key = str(execution_id)
    _MEASURING.add(key)
    try:
        async with _lock():
            yield
    finally:
        _MEASURING.discard(key)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _span(wait: timedelta) -> str:
    from app.services.video_studio.jobs import span

    return span(wait)


def max_wait_words() -> str:
    """``6 h``: :data:`MAX_WAIT` as it is said."""
    hours = MAX_WAIT.total_seconds() / 3600
    return f"{hours:g} h" if hours >= 1 else _span(MAX_WAIT)


@dataclass(frozen=True)
class Wait:
    """A take waiting in the recording lane (:data:`WAIT_KEY`): since when, the worker it last measured on, what ran
    beside it there and what it measured - its painted *size* at *fps* frames a second, under its *wanted* - and the
    load its last claim read there (:attr:`load`)."""

    since: datetime
    worker: str
    beside: Load
    fps: float
    size: tuple[int, int]
    wanted: float
    #: The warm-ups it ran while it waited.
    measures: int = 1
    #: The load its worker had at its last claim; ``None``: the one it measured beside.
    load: Load | None = None
    #: A take relaunched after a worker went with it whose pre-flight fell short of its fps on its new worker,
    #: whatever ran beside it (``capture.RelaunchShortError``): it waits for a worker that sustains its fps, up to
    #: :data:`RELAUNCH_MAX_WAIT`, instead of being refused.
    relaunch: bool = False

    @property
    def now_load(self) -> Load:
        return self.load if self.load is not None else self.beside

    def waited(self, now: datetime) -> timedelta:
        return max(timedelta(0), now - self.since)

    def expired(self, now: datetime) -> bool:
        """Whether it waited :data:`MAX_WAIT` (:data:`RELAUNCH_MAX_WAIT` for a relaunch): measured once more, it is
        decided as on an idle worker."""
        return self.waited(now) >= (RELAUNCH_MAX_WAIT if self.relaunch else MAX_WAIT)

    def measured_words(self) -> str:
        """``21 fps at 3200x1800 beside 2 scene previews and 1 render, under its 60 fps``."""
        if self.relaunch:
            return (
                f"{self.fps:.0f} fps at {self.size[0]}x{self.size[1]} on the worker it was relaunched on after its "
                f"worker was stopped, under its {self.wanted:g} fps"
            )
        return (
            f"{self.fps:.0f} fps at {self.size[0]}x{self.size[1]} beside {self.beside.words()}, under its "
            f"{self.wanted:g} fps"
        )

    def stage(self) -> str:
        """What its row's stage says while it waits (at most 200 characters)."""
        if self.relaunch:
            return (
                f"Relaunched after its worker was stopped: waiting for a worker that sustains its {self.wanted:g} fps "
                f"({self.fps:.0f} fps measured at {self.size[0]}x{self.size[1]})"
            )[:200]
        return (
            f"Waiting in the recording lane: {self.now_load.busy_words('its worker')} ({self.fps:.0f} fps measured "
            f"beside {self.beside.words()}, under its {self.wanted:g} fps)"
        )[:200]

    def waited_words(self, now: datetime, fps: float | None = None) -> str:
        """``waited 4 min in the recording lane: 21 fps at 3200x1800 beside 2 scene previews and 1 render on its worker,
        under its 60 fps; 59 fps measured once its load allowed`` - what its action log's first line and its notes say
        once it recorded, *fps* what its last warm-up measured."""
        if self.relaunch:
            said = (
                f"relaunched after its worker was stopped, waited {_span(self.waited(now))} in the recording lane for a "
                f"worker that sustains its {self.wanted:g} fps: {self.measured_words()}"
            )
            return f"{said}; {fps:.0f} fps measured on the worker that recorded it" if fps is not None else said
        said = (
            f"waited {_span(self.waited(now))} in the recording lane: {self.fps:.0f} fps at {self.size[0]}x{self.size[1]} "
            f"beside {self.beside.words()} on its worker, under its {self.wanted:g} fps"
        )
        if fps is not None:
            said += f"; {fps:.0f} fps measured once its load allowed"
        return said

    def refused_words(self, now: datetime) -> str:
        """What a refusal measured after it waited adds: `` It waited 4 min in the recording lane first (...).``"""
        return f" It waited {_span(self.waited(now))} in the recording lane first ({self.measured_words()})."

    def as_dict(self) -> dict:
        return {
            "since": self.since.isoformat(),
            "worker": self.worker,
            "beside": self.beside.as_dict(),
            "fps": round(self.fps, 1),
            "size": list(self.size),
            "wanted": self.wanted,
            "measures": self.measures,
            **({"load": self.load.as_dict()} if self.load is not None else {}),
            **({"relaunch": True} if self.relaunch else {}),
        }

    @classmethod
    def of(cls, value: Any) -> Wait | None:
        if not isinstance(value, dict):
            return None
        try:
            since = _aware(datetime.fromisoformat(str(value["since"])))
            size = value.get("size") or (0, 0)
            return cls(
                since=since,
                worker=str(value.get("worker") or ""),
                beside=Load.of(value.get("beside")) or Load(),
                fps=float(value.get("fps") or 0.0),
                size=(int(size[0]), int(size[1])),
                wanted=float(value.get("wanted") or 0.0),
                measures=int(value.get("measures") or 1),
                load=Load.of(value.get("load")),
                relaunch=value.get("relaunch") is True,
            )
        except (KeyError, TypeError, ValueError, IndexError):
            return None


def wait_of(execution: Any) -> Wait | None:
    """The wait the execution of a take keeps (:data:`WAIT_KEY`) while it waits in the lane (:data:`WAITING_STATE`): a
    take that measured again since - its hold ended, ``VideoTakeAdapter`` - waits for nothing."""
    if getattr(execution, "adapter_state", None) != WAITING_STATE:
        return None
    return Wait.of((getattr(execution, "payload", None) or {}).get(WAIT_KEY))


def held(wait: Wait | None, worker_id: str, load: Load, now: datetime) -> bool:
    """Whether the take waiting (*wait*) waits again on worker *worker_id* without measuring: the worker it measured
    on, whose load (*load*) is no lighter than the one it fell short beside (:attr:`Load.weight`), within
    :data:`MAX_WAIT`. On another worker, an idle one or a lighter load, it measures again."""
    return (
        wait is not None
        and wait.worker == str(worker_id)
        and not load.idle
        and load.weight >= wait.beside.weight
        and not wait.expired(now)
    )


@dataclass(frozen=True)
class Slot:
    """The slot the recording lane granted a take on worker *worker*: what its warm-up reads beside it, whether it may
    wait again (within :data:`MAX_WAIT`), and the wait it comes from, if any (``capture.preflight``)."""

    worker: str
    execution_id: object
    waited: Wait | None = None
    may_wait: bool = True
    #: Relaunched after a worker went with its take (``VideoTakeAdapter``, ``relaunched_take``): a pre-flight short
    #: of its fps waits for a worker that sustains it (:attr:`Wait.relaunch`) while it :attr:`may_wait`.
    relaunched: bool = False

    async def beside(self) -> Load | None:
        """What runs on its worker beside it now (:func:`load_on`, on a session of its own); ``None`` when unread."""
        try:
            async with async_session() as db:
                return await load_on(db, self.worker, excluding=self.execution_id)
        except Exception as exc:  # unread: the warm-up decides as it did before the lane
            logger.info("Recording lane: the load of worker %s was not read: %s", self.worker, format_exception_message(exc))
            return None

    def measuring(self) -> contextlib.AbstractAsyncContextManager[None]:
        return measuring(self.execution_id)

    def waiting(self, plan: Any, wanted: float, now: datetime, *, relaunch: bool = False) -> Wait:
        """The wait of its take, short of its *wanted* fps beside other jobs (*plan*: ``capture_rate.CapturePlan.waits``),
        or, *relaunch*, short of it on the worker it was relaunched on (:attr:`Wait.relaunch`)."""
        measured = plan.measured or {}
        size = measured.get("size") or (0, 0)
        return Wait(
            since=self.waited.since if self.waited is not None else now,
            worker=str(self.worker),
            beside=plan.beside if plan.beside is not None else Load(),
            fps=float(measured.get("fps") or 0.0),
            size=(int(size[0]), int(size[1])),
            wanted=float(wanted),
            measures=self.waited.measures + 1 if self.waited is not None else 1,
            relaunch=relaunch or (self.waited is not None and self.waited.relaunch),
        )


def again(wait: Wait, load: Load) -> Wait:
    """*wait* once a claim read its worker's *load* and waits again (:func:`held`)."""
    return replace(wait, load=load)


# ── Its place in the lane ─────────────────────────────────────────────────


@dataclass(frozen=True)
class LanePlace:
    """Where a queued take stands in the recording lane: the recordings being made, those a free slot takes before
    it by name, and what it waits for on its worker (:class:`Wait`) when it does."""

    running: int
    ahead: tuple[str, ...]
    wait: Wait | None = None


def _queued_order(execution: Any) -> tuple:
    run_at = execution.run_at or datetime.min.replace(tzinfo=UTC)
    return (_aware(run_at), _aware(execution.created_at or run_at))


async def place_of(db: Any, own: Any) -> LanePlace:
    """Where the queued take of execution *own* stands: the recordings being made, the takes due before its own - the
    order a free slot takes them in - by name, and its wait."""
    from app.models.video_studio import VideoTake
    from app.services.video_studio.jobs import TAKE_ADAPTER

    of_lane = OrchestrationExecution.adapter == TAKE_ADAPTER
    count = select(func.count()).select_from(OrchestrationExecution)
    running = (await db.execute(count.where(of_lane, OrchestrationExecution.state == "running"))).scalar_one()
    before = (
        await db.execute(
            select(OrchestrationExecution).where(
                of_lane,
                OrchestrationExecution.state == "pending",
                OrchestrationExecution.id != own.id,
                or_(
                    OrchestrationExecution.run_at < own.run_at,
                    and_(OrchestrationExecution.run_at == own.run_at, OrchestrationExecution.created_at < own.created_at),
                ),
            )
        )
    ).scalars().all()
    before = sorted(before, key=_queued_order)
    names: dict = {}
    if before:
        takes = (await db.execute(select(VideoTake).where(VideoTake.execution_id.in_([e.id for e in before])))).scalars().all()
        names = {take.execution_id: take.name for take in takes}
    ahead = tuple(names.get(execution.id) or "a recording" for execution in before)
    return LanePlace(int(running), ahead, wait_of(own))


async def refusing_workers() -> tuple[str, ...]:
    """When no running worker claims the recording lane: each worker whose recording lane refuses recordings
    on its GL, as ``worker-7f9c (version 1.4.2) refuses recordings: <why>`` (``capture_rate.lane_gl`` in its presence
    record); empty while a worker claims takes, or when Redis cannot be read."""
    from app.services import worker_presence
    from app.services.video_studio.jobs import TAKE_ADAPTER

    found = await worker_presence.present()
    if not found:
        return ()
    running = [record for record in found if record.get("state") == worker_presence.STATE_RUNNING]
    if any(worker_presence.claims(record, TAKE_ADAPTER) for record in running):
        return ()
    said: list[str] = []
    for record in running:
        gl = record.get("recording_gl")
        if isinstance(gl, dict) and gl.get("refused"):
            # Whole up to REFUSED_CHARS: the container evidence ends the reason and a 400 cap cut it.
            said.append(f"{worker_presence.name_of(record)} refuses recordings: {str(gl['refused'])[:REFUSED_CHARS]}")
    return tuple(said)


async def unserved_words() -> str | None:
    """When no running worker claims the recording lane, in words: ``no worker is running now`` (a rollout between
    two releases, the recording workers scaled to 0) or ``none of the 3 running workers takes recordings``; ``None``
    while one does, or when Redis cannot be read. A lane read as idle and a lane no worker serves read apart."""
    from app.services import worker_presence
    from app.services.video_studio.jobs import TAKE_ADAPTER

    found = await worker_presence.present()
    if found is None:
        return None
    running = [record for record in found if record.get("state") == worker_presence.STATE_RUNNING]
    if any(worker_presence.claims(record, TAKE_ADAPTER) for record in running):
        return None
    if not running:
        draining = len(found)
        return "no worker is running now" + (f" ({draining} draining)" if draining else "")
    count = len(running)
    return f"none of the {count} running worker{'s' if count != 1 else ''} takes recordings"


def ahead_words(names: Sequence[str]) -> str:
    """``nothing queued ahead of it``, ``1 recording ahead ("Login demo")``."""
    if not names:
        return "nothing queued ahead of it"
    shown = ", ".join(f'"{name}"' for name in names[:5]) + (f" +{len(names) - 5}" if len(names) > 5 else "")
    return f"{len(names)} recording{'s' if len(names) != 1 else ''} ahead ({shown})"


def line(place: Any, now: datetime | None = None) -> str:
    """What a queued take says of its place in the recording lane (``jobs.describe``, *place* a ``jobs.QueuePlace``):
    its place and the recordings ahead, and - waiting for its worker's load - what it measured beside which jobs and
    that it starts by itself once their load allows its fps."""
    now = now or datetime.now(UTC)
    ahead = ahead_words(place.recordings or ())
    wait = place.wait
    refusing = getattr(place, "refusing", None)
    if wait is None and refusing:
        # No worker claims takes, and those of the lane refuse them on their GL: it would wait until its deadline.
        return (
            f"waiting for a worker in the recording lane, place {place.position} ({ahead}), but no worker takes "
            f"recordings now: {'; '.join(refusing)}. It fails on its deadline unless a recording lane draws on a GPU"
        )
    unserved = getattr(place, "unserved", None)
    if wait is None and unserved:
        # No worker claims takes at all: it reads apart from a lane that is only idle, and waits for one to be back.
        return (
            f"waiting for a worker in the recording lane, place {place.position} ({ahead}), but no worker takes "
            f"recordings now: {unserved}. It starts once a recording worker is back; not taken by its deadline, it goes "
            "back first in the lane instead of failing, a bounded number of times"
        )
    if wait is None:
        running = place.running
        return (
            f"waiting for a worker in the recording lane, place {place.position}: {running} "
            f"recording{'s' if running != 1 else ''} being made now, {ahead} ({place.slots_words()})"
        )
    load = wait.now_load
    measured = (
        f"it measured {wait.fps:.0f} fps at {wait.size[0]}x{wait.size[1]} beside them, under its {wait.wanted:g} fps"
        if load == wait.beside
        else f"it measured {wait.measured_words()}"
    )
    return (
        f"waiting in the recording lane, place {place.position} ({ahead}): {load.busy_words('its worker')}; {measured}, "
        f"and starts by itself once its worker's load allows {wait.wanted:g} fps (waiting {_span(wait.waited(now))} so "
        f"far, at most {max_wait_words()}; nothing was refused)"
    )
