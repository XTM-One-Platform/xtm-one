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

"""Where a scene preview runs: on its pages, on a worker whose free memory holds them.

A 24 GiB worker drawing a 4K render on 3 pages left its preview lane one page:
a 7 s scene with a screen insert drew a frame a second - 4 min for its 210
frames -, three times in one sweep. A scene preview now starts on its pages
only (``render_process.admit_preview``), and the worker that claimed one its
memory does not hold on them:

* **routes it** to the worker whose free memory holds them - a preview slot
  free, the most memory free first -, read from what every worker that draws
  previews publishes here (:func:`publish`, at most every
  :data:`PUBLISH_EVERY_SECONDS`, a record living :data:`ROOM_TTL_SECONDS`).
  The execution is handed to that worker (``orchestration_engine.defer``'s
  ``claim_for``) for :data:`ROUTE_HOLD_SECONDS` - any worker after, so one that
  went meanwhile never strands it -, and never tried on more than
  :data:`MAX_ROUTES` workers;
* **else waits for its pages** where it is, in its place in the queue, no
  attempt spent, until a run of that worker ends.

Its answer says which (:func:`admission_note`): the first note of the preview.
Redis unreachable, no other worker is known: a preview waits for its pages on
the worker that took it.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from app.core.config import settings
from app.core.utils import format_exception_message
from app.services.video_studio import resources

logger = logging.getLogger(__name__)

_ROOM_KEY = "xtm:video-studio:preview-room:{}"
#: How often a worker that draws previews says what it has free, and how long its word lasts.
PUBLISH_EVERY_SECONDS = 2.0
ROOM_TTL_SECONDS = 15
#: How long a preview handed to a worker is that worker's alone to claim.
ROUTE_HOLD_SECONDS = 10.0
#: The most workers a preview is tried on before it waits for its pages where it is.
MAX_ROUTES = 2
#: How often a preview waiting for its pages is claimed again: what frees them is another preview ending.
WAIT_POLL_SECONDS = 5.0
#: Where a preview's execution records the route that handed it to a worker: ``{from, to, why, tried}``.
ROUTE_KEY = "preview_route"
#: The ``adapter_state`` of a preview handed to another worker.
ROUTED_STATE = "preview_routed"

_published: dict[str, float] = {}


@dataclass(frozen=True)
class Room:
    """What one worker has free for a scene preview starting now."""

    worker: str
    #: The memory its runs leave (``render_process.room_for_previews``); ``None``: unbounded.
    free: int | None
    #: Its preview slots not taken.
    slots: int
    bound: int | None = None
    held: int = 0
    #: Its preview slots in all (``VIDEO_STUDIO_PREVIEW_CONCURRENCY``); ``None`` from a worker of an earlier release.
    capacity: int | None = None
    #: Of them, those held back for the CPUs its whole renders reserve (``render_process.preview_slots_held_back``).
    held_back: int = 0
    #: Of them, those added above ``VIDEO_STUDIO_PREVIEW_CONCURRENCY`` while previews are queued
    #: (``render_process.preview_slots_added``).
    added: int = 0

    def holds(self, needed: int) -> bool:
        """Whether a preview whose pages need *needed* bytes starts there now."""
        return self.slots > 0 and (self.free is None or self.free >= needed)


def here(worker_id: str) -> Room:
    """What this worker has free for a scene preview now."""
    from app.services.video_studio import render_process

    return Room(
        worker=worker_id,
        free=render_process.room_for_previews(),
        # Its slots shrunk to what the CPUs its whole renders reserve leave.
        slots=max(0, render_process.preview_slots() - render_process.previews_running()),
        bound=resources.memory_limit() or resources.host_memory(),
        held=render_process.memory_held(),
        # Its slots in all, those it adds while previews are queued included.
        capacity=max(0, int(settings.video_studio_preview_concurrency)) + render_process.preview_slots_added(),
        held_back=render_process.preview_slots_held_back(),
        added=render_process.preview_slots_added(),
    )


async def publish(worker_id: str, *, force: bool = False) -> None:
    """Say what this worker has free for a scene preview (:func:`here`): at most every
    :data:`PUBLISH_EVERY_SECONDS` unless *force* (a preview just started or ended here)."""
    from app.core.redis import redis_client
    from app.services.video_studio import render_process

    if not render_process.draws_previews():
        return
    now = time.monotonic()
    if not force and now - _published.get(worker_id, -1e9) < PUBLISH_EVERY_SECONDS:
        return
    _published[worker_id] = now
    record = {**asdict(here(worker_id)), "at": datetime.now(UTC).isoformat()}
    try:
        await (await redis_client()).set(_ROOM_KEY.format(worker_id), json.dumps(record), ex=ROOM_TTL_SECONDS)
    except Exception as exc:  # unread: no preview is routed here, each waits where it is
        logger.debug("Preview room not published: %s", format_exception_message(exc))


def _room(value: object) -> Room | None:
    try:
        record = json.loads(value) if value else None
    except (TypeError, ValueError):
        return None
    if not isinstance(record, dict) or not record.get("worker"):
        return None
    try:
        free = record.get("free")
        return Room(
            worker=str(record["worker"]),
            free=None if free is None else int(free),
            slots=int(record.get("slots") or 0),
            bound=int(record["bound"]) if record.get("bound") is not None else None,
            held=int(record.get("held") or 0),
            capacity=int(record["capacity"]) if record.get("capacity") is not None else None,
            held_back=int(record.get("held_back") or 0),
            added=int(record.get("added") or 0),
        )
    except (TypeError, ValueError):
        return None


async def rooms(*, me: str | None = None, at_most: int = 64) -> list[Room]:
    """What the workers that draw previews said they have free - *me* left out, every one without it (how many
    workers the preview lane has) -; none when Redis cannot be read."""
    from app.core.redis import redis_client

    try:
        redis = await redis_client()
        keys = []
        async for key in redis.scan_iter(match=_ROOM_KEY.format("*"), count=100):
            keys.append(key)
            if len(keys) >= at_most:
                break
        said = await redis.mget(keys) if keys else []
    except Exception as exc:
        logger.debug("Preview rooms not read: %s", format_exception_message(exc))
        return []
    return [room for value in said if (room := _room(value)) is not None and (me is None or room.worker != me)]


@dataclass(frozen=True)
class LaneWorkers:
    """The workers the preview lane has: how many draw previews, and how many previews they make at a time."""

    workers: int
    slots: int
    #: Of :attr:`slots`, those held back now for the CPUs a whole render's pages reserve.
    held_back: int = 0
    #: Of :attr:`slots`, those added now while previews are queued.
    added: int = 0

    def words(self) -> str:
        """``1 worker draws previews, 2 at a time``; ``..., 2 at a time (1 held back while a whole render draws on its
        CPUs)``; ``..., 4 at a time (2 added while previews are queued)``."""
        many = self.workers != 1
        said = f"{self.workers} worker{'s' if many else ''} draw{'' if many else 's'} previews, {self.slots} at a time"
        notes = []
        if self.added:
            notes.append(f"{self.added} added while previews are queued")
        if self.held_back:
            notes.append(f"{self.held_back} held back while a whole render draws on {'their' if many else 'its'} CPUs")
        if notes:
            said += f" ({'; '.join(notes)})"
        return said


#: How long a read of the preview lane's workers is kept: every queue line - ``get_video_jobs``, the REST
#: ``queue`` field, the studio's polling - and every preview start's lane advice share one scan of the rooms, which
#: the workers publish every :data:`PUBLISH_EVERY_SECONDS`.
LANE_WORKERS_SECONDS = PUBLISH_EVERY_SECONDS
#: ``(when, what was read)`` of the last read (:func:`lane_workers`).
_lane_read: list[tuple[float, LaneWorkers | None]] = []


async def lane_workers() -> LaneWorkers | None:
    """The workers that said what they have free for a preview within :data:`ROOM_TTL_SECONDS`, and their preview slots
    (a worker of an earlier release counted at this process's ``VIDEO_STUDIO_PREVIEW_CONCURRENCY``); ``None`` when
    none did or Redis cannot be read - what the queue line says instead of a worker's slots alone. Read at most once
    every :data:`LANE_WORKERS_SECONDS`: a route decision reads :func:`rooms` itself, fresh."""
    now = time.monotonic()
    if _lane_read and now - _lane_read[0][0] < LANE_WORKERS_SECONDS:
        return _lane_read[0][1]
    found = await rooms()
    each = max(1, int(settings.video_studio_preview_concurrency))
    workers = (
        LaneWorkers(
            len(found),
            sum(room.capacity if room.capacity is not None else each for room in found),
            sum(room.held_back for room in found),
            sum(room.added for room in found),
        )
        if found
        else None
    )
    _lane_read[:] = [(now, workers)]
    return workers


def forget_lane_workers() -> None:
    """Forget the last read of the preview lane's workers (:func:`lane_workers`): the next one reads the rooms."""
    _lane_read.clear()


def best(found: Iterable[Room], needed: int, *, tried: Iterable[str] = ()) -> Room | None:
    """The worker of *found* a preview whose pages need *needed* bytes goes to: one not *tried* yet that
    :meth:`Room.holds` it, the most memory free first; ``None`` when none does."""
    left_out = set(tried)
    fitting = [room for room in found if room.worker not in left_out and room.holds(needed)]
    return max(fitting, key=lambda room: (room.free is None, room.free or 0)) if fitting else None


def route_of(payload: dict | None, worker_id: str) -> dict | None:
    """The route that handed the preview of *payload* to *worker_id* (:data:`ROUTE_KEY`); ``None`` when none did."""
    route = (payload or {}).get(ROUTE_KEY)
    return route if isinstance(route, dict) and route.get("to") == worker_id else None


def routed_text(*, worker: str, there: Room, why: str) -> str:
    """What a preview handed to another worker says while it is (its stage)."""
    free = "memory to spare" if there.free is None else f"{resources.format_bytes(there.free)} free"
    return f"Routed to worker {there.worker}, which has {free}: on worker {worker} {why}."


def waiting_text(*, worker: str, why: str) -> str:
    """What a preview waiting for its pages says while it does (its stage): that no other worker has room first, as a
    stage keeps 200 characters and *why* names what holds the memory."""
    return (
        f"Waiting for its pages on worker {worker} (no other worker has them free with a preview slot): {why}. It "
        "starts on its pages as soon as a run there ends."
    )


def admission_note(route: dict | None, *, worker: str, waited: float, why: str | None, pages: int) -> str | None:
    """What a preview's answer says of where it was drawn: routed from another worker, after waiting for its
    pages, or both; ``None`` for one drawn at once on the worker that took it."""
    on = f"on its {pages} page{'s' if pages != 1 else ''} on worker {worker}"
    parts = []
    if route is not None:
        parts.append(f"routed there from worker {route.get('from')}, where {route.get('why')}")
    if waited > 0 and why:
        parts.append(f"after waiting {round(waited)} s for its pages, as {why}")
    return f"Drawn {on}, " + "; ".join(parts) + "." if parts else None
