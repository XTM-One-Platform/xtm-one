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

"""A render in a process of its own: one crash fails one render, never the worker.

The worker's ``video_render`` adapter does not draw a video in its own process.
:func:`run_isolated` starts ``python -m app.services.video_studio.render_process
<render id>``, which runs ``render.run_render`` unchanged, and supervises it:

* **A memory ceiling of its own.** Every second the supervisor measures the
  render process and everything it started - Chromium, the ffmpeg encoders and
  decoders (``resources.tree_memory``). Past :func:`memory_ceiling` it stops the
  whole tree and fails the render with its peak memory and the scene it was
  drawing, below the container's limit: the kernel never kills the worker and
  the assignments, embeddings and AI shots running beside the render. A scene
  preview, which runs beside the whole render on a lane of its own,
  has a ceiling of its own (:func:`preview_memory_ceiling`), sized to the
  screen inserts its window shows; stopped there, it runs again at
  once on one page. A whole render never takes the preview reserve
  (:func:`preview_reserve`), a hard floor of 8 GiB, and its
  pages are laid out at launch in the room the worker has then
  (:func:`launch_room`): a preview starts beside it at once on its pages
  (:func:`admit_preview`); one whose pages do not fit what is left goes to a
  worker whose free memory holds them, or waits for them here
  (``preview_routing``) - never drawn on one page for want of memory. Should
  the container's memory run out all the same, the kernel stops a preview's
  processes first, a render's next, the worker's own last
  (:func:`stopped_before_the_worker`).
* **Its CPUs.** A whole render reserves its pages' CPUs
  (:func:`cpus_reserved`): the worker's preview slots shrink to what the
  rest holds (:func:`preview_slots`, one at least), and a preview's process
  starts at background priority (:func:`nice_for`, ``cpu_share``), taking
  the CPU the render's pages leave.
* **Its death, seen at once.** The process reports its stages, its progress
  and its outcome on its standard output, one JSON object a line (its logs go
  to standard error). A process that ends without an outcome crashed: the
  supervisor says how - exit code or signal - with its peak memory and where it
  was, the same second, never through a lease expiring minutes later.
* **A stall, resumed in place.** A render whose progress stands still
  for ``VIDEO_STUDIO_RENDER_STALL_SECONDS``, at any stage, is stopped and
  started again in the same working directory, under the same execution and
  its lease renewed: the segments of frames it had finished are kept
  (``compositor.kept_segments``) and only the rest is drawn. Each stall is
  recorded on the render (``VideoRender.stalls``: where it stood, how long it
  stood still, the frames kept), said by ``get_video_jobs``. Past
  :data:`MAX_STALL_RESUMES` it is failed as stalled. A frame loop between two
  progress reports, and an encode while it runs, are movement too: the
  render process says so (``heartbeat``), so a 2160p stage that draws
  for minutes is never taken for a stall.
* **Attempts that resume.** A crash runs the render again at the same
  quality, and a lease the engine took back runs it again too (the execution
  allows three attempts, ``jobs.enqueue``). Each render has one working
  directory on the worker (:func:`workdir_of`), kept when its attempt ends in
  a way that runs it again (:data:`_RUNS_AGAIN`), so the next attempt on this
  worker keeps what the last one finished - the compositor's segments, a
  figure's frames and its video (``compositor.same_drawing``) - and draws
  only the rest; an attempt on another worker takes the segments kept beside
  the render in the store (``render_segments``). A newer attempt of a
  render stops the one before it on this worker, which lost its execution
  and shares its directory, before it starts; :func:`sweep_workdirs` removes
  the directories no attempt will resume. The memory ceiling fails a render
  at once: the same render would end the same way.
* **A worker that stops hands its render over.** A deploy or a
  restart stops the worker (``AdapterContext.stopping``): the render process
  is stopped and the execution goes back to the queue with its attempt
  unspent (:func:`hand_over`), where the next worker takes it at once and
  resumes it - never a lease left to expire, never a render failed for it.

The worker keeps the execution's lease from its own event loop, which draws
nothing and reads ``/proc`` in a thread, and the render process renews it too
from a thread of its own while the render moves - its frames, followed or
watched, and its reports say it moves (``lease.FrameLease``)
- so a slow render that still moves is never taken for a dead one. A
render's lease lasts ``jobs.RENDER_LEASE_SECONDS`` (180 s). A render whose
lease is gone stops (``lease_lost``) and leaves its row to whoever has the
execution now; one the engine dead-lettered for a lease that lapsed while it
still moved runs again from its finished segments
(:func:`resume_after_lost_lease`), never "no attempt left". A cancel asks the process to stop between
frames (SIGTERM) and kills it past a grace period. The process dies with the
worker (``PR_SET_PDEATHSIG``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import shutil
import signal
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO

from app.core.config import settings
from app.core.utils import format_exception_message, safe_uuid
from app.models.video_studio import JOB_CANCELLED, JOB_FAILED, JOB_PENDING, JOB_TERMINAL_STATUSES
from app.services.orchestration_engine import LeaseExpiry, LeaseLostError
from app.services.video_studio import (
    compositor,
    cpu_lanes,
    figure_cache,
    insert_frames,
    jobs,
    render_cache,
    render_preflight,
    render_ranges,
    render_retry,
    render_segments,
    resources,
)
from app.services.video_studio.lease import EXECUTION_ENV, LEASE_ENV, OWNER_ENV, renew
from app.services.video_studio.runners import JobOutcome

logger = logging.getLogger(__name__)

#: The worker's process id, handed to the render process to check it still has it.
PARENT_ENV = "XTM_RENDER_PARENT_PID"
#: The kernel's out-of-memory score adjustment a render process sets itself, and everything it starts inherits:
#: should the container's memory run out, the kernel stops a preview's processes first, a whole render's
#: next, and the worker's own (0) last.
OOM_SCORE_ENV = "XTM_RENDER_OOM_SCORE_ADJ"
RENDER_OOM_SCORE_ADJ = 500
PREVIEW_OOM_SCORE_ADJ = 1000
_OOM_SCORE_ADJ_PATH = Path("/proc/self/oom_score_adj")
#: What a render process runs with, unless the worker's own environment says otherwise: two glibc heaps.
#: Each thread composing a screen insert kept a heap of its own, never given back: one 2160p page held 4.8 GiB at its
#: peak with the default heaps and 4.1 GiB with two, drawing as fast (``test_video_studio_page_peak_chromium``).
RENDER_ENVIRONMENT = {"MALLOC_ARENA_MAX": "2"}
_MIB = 1 << 20
#: The ceiling when the container sets no memory limit.
_DEFAULT_CEILING = 4096 * _MIB
#: The share of the container's memory limit a render may use: the rest is the worker's own.
_CEILING_SHARE = 0.6
_POLL_SECONDS = 1.0
#: How long a cancelled render has to stop between frames before it is killed.
_CANCEL_GRACE_SECONDS = 20.0
#: How long a render handed over has to close its open segments at the frames reached and store them (SIGUSR1,
#: ``compositor.hand_over_now``) before it is killed: inside ``DRAIN_MARGIN_SECONDS`` of the worker's grace.
HANDOVER_CLOSE_SECONDS = 15.0
_PR_SET_PDEATHSIG = 1
#: How many times a stalled render is resumed in place, its finished frames kept, before it fails.
MAX_STALL_RESUMES = 2
#: The stalls a render keeps on its row, the newest last.
_STALLS_KEPT = 10
#: The worker changes a render keeps on its row, the newest last.
_RELAUNCHES_KEPT = 10
#: How long a relaunched render may take from its relaunch to drawing again (issue #8958): the media gathered and the
#: narrations placed from their caches, its frames kept. Each relaunch says how long it took; past this, it says so.
RESUME_TO_DRAWING_BOUND_SECONDS = 300
#: The most one relaunch gives back to the render's deadline for the time its worker's stop cost it (#11782): from
#: its last progress before the stop to its next worker, an hour at most - a render left longer was not handed on.
RELAUNCH_CREDIT_MOST_SECONDS = 3600
#: Where the working directory of each render is made on this worker (:func:`workdir_of`).
WORKDIR_ROOT = Path(tempfile.gettempdir())
_WORKDIR_PREFIX = "xtm-render-"
#: What an attempt ends with that runs the render again: its working directory is kept for that attempt,
#: and its finished segments stay beside it in the store (``render_segments``) - a render the platform failed
#: running again once included (``again``), a scene preview that gave its lent slot back, and a whole
#: render whose own slot was asked back (``gave_back``).
_RUNS_AGAIN = ("lease_lost", "retry", "handover", "again", "given_back", "gave_back")


class _Leaving:
    """Set once the worker stops, the slot the render runs in is taken back, or its draining worker hands it on:
    what ``supervise`` stops on."""

    def __init__(self, *events: asyncio.Event | None) -> None:
        self.events = [event for event in events if event is not None]

    def is_set(self) -> bool:
        return any(event.is_set() for event in self.events)


#: A whole render this far through (its progress, %) is waited for by its stopping worker, up to
#: :func:`drain_seconds`: a deploy never hands over a render nearly done. One under it goes to a running worker at once.
DRAIN_PAST_PROGRESS = 50
#: What a stopping worker keeps of its termination grace after that wait: its render processes stopped, their
#: executions handed back, the worker's own teardown.
DRAIN_MARGIN_SECONDS = 30
#: How long a draining worker keeps drawing a render under :data:`DRAIN_PAST_PROGRESS` for a worker of the incoming
#: release to come up before any running worker takes it (:func:`incoming_wait_seconds`).
INCOMING_WAIT_SECONDS = 600
#: How often the stop's wait looks at the renders it waits for.
_DRAIN_POLL_SECONDS = 1.0
#: A working directory no attempt runs in is removed once untouched this long: longer than a render may run, an
#: ultra one's 8 h included.
_WORKDIR_KEPT_SECONDS = 9 * 3600


@dataclass
class _Attempt:
    """One attempt of a render on this worker: a newer attempt of the same render stops it."""

    superseded: asyncio.Event = field(default_factory=asyncio.Event)
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    #: The memory ceiling its process is held to, once read: what a scene preview starts beside.
    ceiling: int = 0
    #: What it holds of its worker once its process laid out its pages: their budget
    #: (``render_preflight.memory_budget``), never more than :attr:`ceiling`; ``0`` until then, its ceiling held.
    holds: int = 0
    #: The pages its process laid out: a whole render reserves their CPUs on its worker; ``0`` until then.
    pages: int = 0
    #: A scene preview's: its memory sits in the preview reserve, never in a render's room.
    preview: bool = False
    #: Its process's last progress (%), and the event that hands it over while its worker drains.
    progress: int = 0
    handing: asyncio.Event = field(default_factory=asyncio.Event)
    #: Its process while it runs: what its worker's CPU beside its renders and previews is measured without.
    pid: int = 0

    @property
    def held(self) -> int:
        """What it holds of its worker: its pages' budget once known, its ceiling until then."""
        return min(self.holds, self.ceiling) if self.holds and self.ceiling else self.holds or self.ceiling


#: The attempt of each render this worker runs, by render id.
_ATTEMPTS: dict[str, _Attempt] = {}


def workdir_of(render_id: str) -> Path:
    """The working directory of *render_id* on this worker: the same for each of its attempts."""
    return WORKDIR_ROOT / f"{_WORKDIR_PREFIX}{render_id}"


#: What the worker keeps for its own jobs below its container's limit beside an ultra render's own ceiling.
WORKER_RESERVE = 4 << 30
#: The least memory a worker that draws scene previews keeps out of every render's reach: the largest
#: preview's worst case on its two pages (7.5 GiB measured: 1080p, three video inserts and a picture). A hard floor:
#: ``VIDEO_STUDIO_PREVIEW_RESERVE_MB`` raises it, never lowers it, and no ceiling - an ultra one, one set by hand -
#: ever reaches into it.
PREVIEW_RESERVE_FLOOR = 8 << 30


def draws_previews() -> bool:
    """Whether this worker draws scene previews: ``VIDEO_STUDIO_PREVIEW_CONCURRENCY`` above 0."""
    return settings.video_studio_preview_concurrency > 0


def preview_reserve() -> int:
    """The memory of this worker no render ever takes: its scene previews'.

    :data:`PREVIEW_RESERVE_FLOOR` at least, ``VIDEO_STUDIO_PREVIEW_RESERVE_MB`` when that is more; none on a worker
    that draws no preview (``VIDEO_STUDIO_PREVIEW_CONCURRENCY=0``), whose memory is all its renders'.
    """
    if not draws_previews():
        return 0
    return max(PREVIEW_RESERVE_FLOOR, max(0, settings.video_studio_preview_reserve_mb) * _MIB)


def kept_from_renders() -> int:
    """What an ultra render leaves of its worker: its own jobs' or its previews', the larger."""
    return max(WORKER_RESERVE, preview_reserve())


def ultra_pages_need() -> int:
    """What an ultra render's pages need at their measured peak (``render_preflight.page_peak``): the pages
    the worker's CPUs want (``render_preflight.layout``, ``VIDEO_STUDIO_RENDER_PAGES``) at 2160p, each with the screen
    inserts its peak was measured with, its encoder on its share of the CPUs."""
    from app.services.video_studio.spec import ASPECT_RATIOS, QUALITY_PRESETS, output_size

    preset = QUALITY_PRESETS["ultra"]
    size = max((output_size(ratio, preset.short_side) for ratio in ASPECT_RATIOS), key=lambda s: s[0] * s[1])
    layout = render_preflight.layout(size, x264_preset=preset.x264_preset, ceiling=None)
    peak = render_preflight.page_peak(size, preset.x264_preset)
    return render_preflight.memory_budget(
        size, pages=layout.wanted, x264_preset=preset.x264_preset, inserts=peak.inserts if peak else None, cpus=layout.cpus
    )


def memory_ceiling(quality: str | None = None) -> int:
    """The memory a render process and everything it starts may use, in bytes.

    Never past the container's memory limit - the host's without one - less the preview reserve
    (:func:`preview_reserve`, a hard floor): a render is admitted only where what is left covers it, and a
    scene preview always finds that reserve beside it (:func:`admit_preview`). An ultra render may use
    ``VIDEO_STUDIO_ULTRA_RENDER_MEMORY_MB`` (16 GiB) at least, and more where the container has it: what the
    pages its CPUs want need at their measured peak (:func:`ultra_pages_need`) - never past that limit less
    :data:`WORKER_RESERVE` or the reserve, whichever is larger (:func:`kept_from_renders`), and never less than the
    other renders' ceiling. A host whose memory is unknown keeps the configured value. A worker smaller than its
    reserve holds no render at all: its renders are refused, the memory to give it said.
    """
    configured = settings.video_studio_render_memory_mb
    limit = resources.memory_limit()
    shared = int(limit * _CEILING_SHARE) if limit else _DEFAULT_CEILING
    ceiling = configured * _MIB if configured > 0 else shared
    bound = limit or resources.host_memory()
    if bound:
        ceiling = min(ceiling, max(0, bound - preview_reserve()))
    ultra = settings.video_studio_ultra_render_memory_mb * _MIB
    if quality != "ultra" or ultra <= 0:
        return ceiling
    if bound:
        ultra = min(max(ultra, ultra_pages_need()), max(0, bound - kept_from_renders()))
    return max(ceiling, ultra)


def renders_held(*, excluding: str | None = None) -> int:
    """What the whole renders and scene ranges this worker runs hold, *excluding* one of them."""
    return sum(
        attempt.held
        for key, attempt in _ATTEMPTS.items()
        if not attempt.preview and key != excluding and key not in _ADMITTED
    )


def launch_room(quality: str | None, *, excluding: str | None = None) -> int:
    """The memory a whole render - or a scene range - starting now on this worker may use: its pages are
    laid out in it at launch (``render_preflight.launch_ceiling``), never from what a pre-flight read earlier.

    Its quality's ceiling (:func:`memory_ceiling`), never more than the worker's memory less the preview reserve,
    less what its other renders hold (:func:`renders_held`) and what its previews hold past the reserve: the
    reserve is never borrowed, however the worker's runs come and go.
    """
    ceiling = memory_ceiling(quality)
    bound = resources.memory_limit() or resources.host_memory()
    if not bound:
        return ceiling
    reserve = preview_reserve()
    over = max(0, previews_held() - reserve)
    return max(0, min(ceiling, bound - reserve - renders_held(excluding=excluding) - over))


#: How much more than its estimate (``render_preflight.memory_needed``) a scene preview may hold before it is
#: stopped: the decoders of the clips and figures it shows, beside its pages.
PREVIEW_HEADROOM = 2.0


def _preview_drawing(
    inserts: render_preflight.InsertLoad | None, quality: str | None, pages: int | None
) -> tuple[int, int]:
    """``(estimate, budget)`` of a scene preview at its *quality* of the largest frame on *pages* pages (its own by
    default, ``render_preflight.preview_pages``), each with its screen *inserts*: what it is estimated to hold, and
    the ceiling its pages are laid out under (``render_preflight.memory_budget``)."""
    from app.services.video_studio.spec import ASPECT_RATIOS, PREVIEW_QUALITIES, QUALITY_PRESETS, output_size

    preset = QUALITY_PRESETS[quality if quality in PREVIEW_QUALITIES else "draft"]
    size = max((output_size(ratio, preset.short_side) for ratio in ASPECT_RATIOS), key=lambda s: s[0] * s[1])
    wanted = render_preflight.layout(
        size, x264_preset=preset.x264_preset, ceiling=None, pages=pages or render_preflight.preview_pages()
    ).wanted
    return (
        render_preflight.memory_needed(size, pages=wanted, x264_preset=preset.x264_preset, inserts=inserts),
        render_preflight.memory_budget(size, pages=wanted, x264_preset=preset.x264_preset, inserts=inserts),
    )


def preview_memory_ceiling(
    inserts: render_preflight.InsertLoad | None = None, *, quality: str | None = None, pages: int | None = None
) -> int:
    """The memory a scene preview's process and everything it starts may use, in bytes.

    ``VIDEO_STUDIO_PREVIEW_MEMORY_MB``, else :data:`PREVIEW_HEADROOM` times
    what a preview at its *quality* (a 720p draft, or 1080p) of the
    largest frame is estimated to hold on the pages a preview draws on
    (``render_preflight.preview_pages``; *pages* when given: one, once its
    ceiling stopped it on more), each with the screen *inserts* its
    window shows at once at most (:func:`preview_inserts`). Never more
    than the preview reserve unless the budget its pages are laid out
    under alone is more: a preview always starts on its pages in a free
    reserve, beside any render. Never more than the worker's memory.
    """
    configured = settings.video_studio_preview_memory_mb
    if configured > 0:
        return configured * _MIB
    needed, budget = _preview_drawing(inserts, quality, pages)
    ceiling = math.ceil(needed * PREVIEW_HEADROOM)
    if reserve := preview_reserve():
        ceiling = min(ceiling, max(budget, reserve))
    bound = resources.memory_limit() or resources.host_memory()
    return min(ceiling, bound) if bound else ceiling


@dataclass(frozen=True)
class PreviewSize:
    """What a scene preview may hold: its ceiling on its pages, and on one page once that ceiling
    stopped it."""

    #: Its ceiling on the pages a preview draws on (:func:`preview_memory_ceiling`): what its admission holds.
    ceiling: int
    #: Its ceiling on one page, its screen inserts composed one after the other: drawn again so after its own
    #: ceiling stopped it on its pages, never for want of its worker's memory.
    one_page: int
    #: The pages its :attr:`ceiling` is sized for.
    pages: int = render_preflight.PREVIEW_PAGES
    #: Whether its window shows screen inserts.
    inserts: bool = False


def preview_size(inserts: render_preflight.InsertLoad | None = None, *, quality: str | None = None) -> PreviewSize:
    """The :class:`PreviewSize` of a scene preview at *quality* whose window shows *inserts*."""
    return PreviewSize(
        ceiling=preview_memory_ceiling(inserts, quality=quality),
        one_page=preview_memory_ceiling(inserts, quality=quality, pages=1),
        pages=render_preflight.preview_pages(),
        inserts=bool(inserts),
    )


async def preview_inserts(row: object | None) -> render_preflight.InsertLoad | None:
    """The screen inserts the window of the scene preview of *row* shows at once at most; ``None`` for none.

    Read off its video's spec before anything is planned
    (``render_preflight.InsertLoad.of_spec``), so the supervisor, the
    admission and the render process size the preview alike. A row that is
    not a preview, or whose video cannot be read, has none: the preview's
    ceiling of a scene without inserts, as before.
    """
    scene_id = getattr(row, "scene_id", None)
    if not scene_id:
        return None
    from app.core.database import async_session
    from app.models.video_studio import VideoProject
    from app.services.video_studio.spec import parse_video_spec

    try:
        async with async_session() as db:
            project = await db.get(VideoProject, row.project_id)
        if project is None:
            return None
        return render_preflight.InsertLoad.of_spec(parse_video_spec(project.spec or {}), str(scene_id))
    except Exception as exc:  # the ceiling of a scene without inserts then: never what stops a preview
        logger.info("Video preview %s: its screen inserts were not read: %s", getattr(row, "id", "?"), format_exception_message(exc))
        return None


async def preview_size_of(render_id: str) -> PreviewSize:
    """The :class:`PreviewSize` of the scene preview *render_id*: sized to the screen inserts of its window
    and to its quality."""
    row = await _render_of(render_id)
    return preview_size(await preview_inserts(row), quality=getattr(row, "quality", None))


@dataclass(frozen=True)
class PreviewAdmission:
    """How a scene preview starts on this worker: always on its pages."""

    #: The memory ceiling its process is held to, and the memory it holds of its worker's meanwhile.
    ceiling: int
    #: The pages it draws on.
    pages: int = render_preflight.PREVIEW_PAGES
    #: Where it came to be drawn, when it was not at once on the worker that first took it: routed there from
    #: another worker, or after waiting for its pages (``preview_routing``); its answer says it.
    note: str | None = None


#: How each scene preview this worker admitted starts, by render id, until it ends.
_ADMITTED: dict[str, PreviewAdmission] = {}


def _preview_holds() -> list[int]:
    """What each scene preview this worker runs holds: its ceiling, from its admission on."""
    ids = {key for key, attempt in _ATTEMPTS.items() if attempt.preview} | set(_ADMITTED)
    return [max(_ATTEMPTS[r].held if r in _ATTEMPTS else 0, _ADMITTED[r].ceiling if r in _ADMITTED else 0) for r in ids]


def previews_held() -> int:
    """What the scene previews this worker runs hold: each one's ceiling, from its admission on."""
    return sum(_preview_holds())


def holders() -> str:
    """What holds this worker's memory, by kind: ``1 render (11.6 GiB) and 1 scene preview (7.0 GiB)``; ``nothing``.

    A preview that waits for its pages says it (:func:`admit_preview`): the memory its worker's renders hold is
    not the memory its previews hold, and only the first is kept from the previews by the reserve."""
    renders = [attempt.held for key, attempt in _ATTEMPTS.items() if not attempt.preview and key not in _ADMITTED]
    previews = _preview_holds()
    parts = [
        f"{len(held)} {noun}{'s' if len(held) != 1 else ''} ({resources.format_bytes(sum(held))})"
        for noun, held in (("render", renders), ("scene preview", previews))
        if held
    ]
    return " and ".join(parts) or "nothing"


def reserve_advice(size: PreviewSize) -> render_preflight.Infra | None:
    """The memory a worker needs to start as many previews of *size* at once as it runs, beside any render;
    ``None`` when its preview reserve already holds them.

    The reserve's floor (:data:`PREVIEW_RESERVE_FLOOR`) is one preview's worst case, and a worker runs
    ``VIDEO_STUDIO_PREVIEW_CONCURRENCY`` of them: beside a render that took the rest of its memory, the others wait
    for the first to end, and with no other worker that draws previews they have nowhere to go. A preview that
    waited for its pages says what to give (``render_preflight.record_preview_infra``), once: a reserve that holds
    as many previews at their worst case (the floor, or *size* when larger) as the worker runs.
    """
    reserve = preview_reserve()
    slots = max(1, settings.video_studio_preview_concurrency)
    if not reserve or size.ceiling <= 0 or reserve >= slots * size.ceiling:
        return None
    fits = reserve // size.ceiling
    needed = math.ceil(slots * max(size.ceiling, PREVIEW_RESERVE_FLOOR) / (1 << 30)) << 30
    more = needed - reserve
    return render_preflight.Infra(
        kind="preview_reserve",
        key=f"preview_reserve:{reserve // _MIB}:{slots}:{needed // _MIB}",
        text=(
            f"Scene previews wait for memory beside a render: this worker's preview reserve keeps {_gib(reserve)} out "
            f"of every render's reach, which holds {fits or 'no'} preview{'s' if fits > 1 else ''} of "
            f"{_gib(size.ceiling)} at a time, and it runs {slots} at once. To start them all at once, set "
            f"VIDEO_STUDIO_PREVIEW_RESERVE_MB to {needed // _MIB} and give the worker's container {_gib(more)} more "
            "memory, or run a second worker that draws scene previews only (VIDEO_STUDIO_RENDER_CONCURRENCY=0). "
            "The video needs no change."
        ),
    )


def memory_held() -> int:
    """What the runs of this worker hold: each render its pages' budget once laid out, its ceiling until then,
    each scene preview its ceiling from its admission."""
    return renders_held() + previews_held()


def previews_running() -> int:
    """How many scene previews this worker admitted and runs now."""
    return len({key for key, attempt in _ATTEMPTS.items() if attempt.preview} | set(_ADMITTED))


def admission_of(render_id: str) -> PreviewAdmission | None:
    """How the scene preview *render_id* was admitted on this worker (:func:`admit_preview`); ``None`` if it was not."""
    return _ADMITTED.get(render_id)


def room_for_previews() -> int | None:
    """What this worker's memory leaves a scene preview starting now: its memory less what its runs hold
    (:func:`memory_held`); ``None`` with neither a container limit nor a host memory read: unbounded."""
    bound = resources.memory_limit() or resources.host_memory()
    return max(0, bound - memory_held()) if bound else None


def admit_preview(render_id: str, size: PreviewSize | None = None, *, note: str | None = None) -> str | None:
    """Hold this worker's memory for the scene preview *render_id* on its pages; why it cannot start
    here now, else ``None``.

    A preview starts in what the runs of this worker hold (:func:`memory_held`) leave of the container's limit -
    the host's memory without one - on its pages, never fewer (a 1080p scene with a screen insert drew one
    frame a second on one page beside a 4K render). A whole render never takes the preview reserve
    (:func:`preview_reserve`, a hard floor), and a preview's ceiling stays within it (:func:`preview_memory_ceiling`),
    so one preview always starts beside any render; one more starts once what is left holds its pages - else
    the preview lane sends it to a worker whose free memory does, or waits for its pages here
    (``preview_routing``), *note* saying which once it starts. A preview alone always starts. Checked and held in
    one step, with nothing awaited in between, so two previews claimed together never both take the last room;
    :func:`release_preview` gives it back, and :func:`admission_of` tells its supervisor how it starts.
    """
    size = size or preview_size()
    held = memory_held()
    left = room_for_previews()
    if left is None or not held or left >= size.ceiling:
        _ADMITTED[render_id] = PreviewAdmission(size.ceiling, pages=size.pages, note=note)
        return None
    bound = resources.memory_limit() or resources.host_memory()
    return (
        f"its {size.pages} page{'s' if size.pages != 1 else ''} need {resources.format_bytes(size.ceiling)}, and its "
        f"worker holds {resources.format_bytes(held)} of its {resources.format_bytes(bound)} for {holders()} "
        f"({resources.format_bytes(left)} left)"
    )


def release_preview(render_id: str) -> None:
    """Give back the memory :func:`admit_preview` held for *render_id*: its run ended."""
    _ADMITTED.pop(render_id, None)


def memory_to_give(needed: int, quality: str | None = None) -> str:
    """How the worker gives a render *needed* bytes (:func:`memory_ceiling`): what a render short of memory says.

    A render short of memory is given memory - never a lower quality or a shorter video.
    """
    budget = math.ceil(needed / _MIB / 100) * 100
    if quality == "ultra" and settings.video_studio_ultra_render_memory_mb > 0:
        kept = kept_from_renders()
        return (
            f"set VIDEO_STUDIO_ULTRA_RENDER_MEMORY_MB to {budget} or more and give the worker's container "
            f"{_container_for(needed, kept)} GiB or more ({_gib(kept)} of it stay with the worker's own jobs and its "
            "scene previews)"
        )
    reserve = preview_reserve()
    beside = _container_for(needed, reserve)
    container = max(math.ceil(needed / _CEILING_SHARE / (1 << 30)), beside)
    kept = f" (its scene previews keep {_gib(reserve)} of it)" if reserve else ""
    return (
        f"set VIDEO_STUDIO_RENDER_MEMORY_MB to {budget} or more on a container of {beside} GiB or more{kept}, or give "
        f"the worker's container {container} GiB or more ({round(_CEILING_SHARE * 100)}% of it goes to a render)"
    )


def _container_for(needed: int, kept: int) -> int:
    """The fewest GiB of a container whose memory, less the *kept* bytes no render takes, still holds *needed* bytes."""
    return max(1, math.ceil((needed + kept) / (1 << 30)))


def _gib(value: int) -> str:
    return f"{value / (1 << 30):.3g} GiB"


async def _render_of(render_id: str) -> object | None:
    """The row of *render_id*, which its ceiling depends on (its quality, its lane); ``None`` when it
    cannot be read."""
    try:
        return await jobs.load("render", render_id)
    except Exception as exc:  # the other renders' ceiling then: never what stops a render
        logger.info("Video render %s: its row was not read: %s", render_id, format_exception_message(exc))
        return None


def ceiling_of(row: object | None, inserts: render_preflight.InsertLoad | None = None, *, key: str | None = None) -> int:
    """The memory ceiling of the render of *row*: a scene preview's, sized to its window's screen *inserts*
    (:func:`preview_inserts`) and its quality, else the room its quality has on this worker now
    (:func:`launch_room`), *key* its own run, left out of what the others hold."""
    if getattr(row, "scene_id", None):
        return preview_memory_ceiling(inserts, quality=getattr(row, "quality", None))
    return launch_room(getattr(row, "quality", None), excluding=key)


def no_room() -> str:
    """Why a whole render - or a range - cannot start on this worker at all: its memory, less the preview
    reserve and what its other runs hold, leaves nothing; never started unbounded."""
    bound = resources.memory_limit() or resources.host_memory()
    return (
        f"This worker's {resources.format_bytes(bound)} of memory, less the {_gib(preview_reserve())} its scene "
        f"previews keep and the {resources.format_bytes(renders_held())} its other renders hold, leaves no room for a "
        "render. The video and its quality need no change: give the worker's container more memory, or draw whole "
        "renders on a worker that draws no previews (VIDEO_STUDIO_PREVIEW_CONCURRENCY=0) beside one that draws them "
        "(VIDEO_STUDIO_RENDER_CONCURRENCY=0)."
    )


def _holding(attempt: _Attempt) -> Callable[[dict], None]:
    """What the supervisor does with the pages a whole render's process laid out (``render_preflight.layout_event``):
    the render holds their budget of its worker from then on, never more than its ceiling."""

    def laid_out(event: dict) -> None:
        with contextlib.suppress(TypeError, ValueError):
            attempt.holds = max(0, int(event.get("budget") or 0))
        with contextlib.suppress(TypeError, ValueError):
            attempt.pages = max(0, int(event.get("pages") or 0))

    return laid_out


#: The preview slots a worker keeps beside a whole render whatever its pages reserve: a scene preview never
#: waits for a whole render - it draws at background priority (:func:`nice_for`), on the CPU the render leaves.
PREVIEW_SLOTS_BESIDE_A_RENDER = 1


def nice_for(preview: bool) -> int:
    """The niceness a render process starts at: a scene preview's ``cpu_share.PREVIEW_NICE``, so that it and
    everything it starts take the CPU a whole render's pages leave; a whole render's, the worker's own - then put in
    its worker's render lane (``cpu_lanes.place``)."""
    from app.services.video_studio.cpu_share import PREVIEW_NICE

    return PREVIEW_NICE if preview else 0


def preview_cpus() -> int:
    """The CPUs a scene preview's pages are counted for: its pages (``render_preflight.preview_pages``), each
    ``render_preflight.PAGE_CPUS``."""
    return render_preflight.preview_pages() * render_preflight.PAGE_CPUS


def _wanted_pages(cpus: int) -> int:
    """The pages a whole render asks on a worker of *cpus* CPUs before it laid them out (``render_preflight.layout``)."""
    configured = int(settings.video_studio_render_pages)
    return max(1, min(configured if configured > 0 else render_preflight.auto_pages(cpus), cpus))


def attempt_pids() -> list[int]:
    """The processes of the renders, scene ranges and scene previews this worker runs now."""
    return [attempt.pid for attempt in _ATTEMPTS.values() if attempt.pid and not attempt.ended.is_set()]


def cpus_reserved() -> int:
    """The CPUs the whole renders and scene ranges this worker runs reserve: each its pages' - those it asks
    until its process laid them out -, ``render_preflight.PAGE_CPUS`` a page, the worker's CPUs at most."""
    cpus = resources.cpu_budget()
    pages = sum(
        attempt.pages or _wanted_pages(cpus)
        for key, attempt in _ATTEMPTS.items()
        if not attempt.preview and key not in _ADMITTED
    )
    return min(cpus, pages * render_preflight.PAGE_CPUS)


def preview_slots_beside(pages: int, cpus: int) -> tuple[int, int]:
    """``(the worker's preview slots, those a whole render on *pages* pages of its *cpus* CPUs leaves)``: as many
    previews as the CPUs its pages leave hold (:func:`preview_cpus`), :data:`PREVIEW_SLOTS_BESIDE_A_RENDER` at least,
    the worker's slots at most; none on a worker that draws no preview."""
    capacity = max(0, int(settings.video_studio_preview_concurrency)) if draws_previews() else 0
    if pages <= 0:
        return capacity, capacity
    left = max(0, int(cpus) - min(int(cpus), int(pages) * render_preflight.PAGE_CPUS)) // max(1, preview_cpus())
    return capacity, min(capacity, max(PREVIEW_SLOTS_BESIDE_A_RENDER, left))


def _own_preview_slots() -> int:
    """This worker's preview slots shrunk to what the CPUs its whole renders reserve (:func:`cpus_reserved`) leave
    (:func:`preview_slots_beside`), none added."""
    reserved = cpus_reserved()
    capacity, left = preview_slots_beside(reserved // render_preflight.PAGE_CPUS, resources.cpu_budget())
    return left if reserved else capacity


def preview_slots() -> int:
    """The scene previews this worker runs at once now: its preview slots, shrunk to what the CPUs its whole renders
    reserve leave, plus those it adds while previews are queued (:func:`preview_slots_added`)."""
    return _own_preview_slots() + preview_slots_added()


def preview_slots_held_back() -> int:
    """The preview slots this worker holds back now for the CPUs its whole renders reserve: the preview
    adapter's ``slots_held_back``, what the reconciler claims none into."""
    capacity = max(0, int(settings.video_studio_preview_concurrency)) if draws_previews() else 0
    return max(0, capacity - _own_preview_slots())


#: The scene previews queued in the preview lane at this worker's last reconcile tick
#: (``preview_lane.queued_count``, read by the preview adapter's ``periodic``): what the slots it adds are sized to.
_QUEUED: list[int] = [0]


def note_queued(count: int) -> None:
    """Keep the scene previews queued in the preview lane now: the next :func:`preview_slots_added` reads it."""
    _QUEUED[0] = max(0, int(count))


def preview_slots_most() -> int:
    """The most scene previews this worker runs at once while previews are queued:
    ``VIDEO_STUDIO_PREVIEW_CONCURRENCY_MAX``, held to what the CPUs its whole renders leave hold (each preview its
    :func:`preview_cpus`), never below its own slots; its own slots when the setting is ``0`` (static) or no more
    than them, and none on a worker that draws no preview."""
    if not draws_previews():
        return 0
    own = max(0, int(settings.video_studio_preview_concurrency))
    most = int(settings.video_studio_preview_concurrency_max)
    if most <= own:
        return own
    held = max(0, resources.cpu_budget() - cpus_reserved()) // max(1, preview_cpus())
    return max(own, min(most, held))


def adds_preview_slots() -> bool:
    """Whether this worker may add preview slots while previews are queued: its
    ``VIDEO_STUDIO_PREVIEW_CONCURRENCY_MAX`` above its own slots, on a worker that draws previews."""
    return draws_previews() and int(settings.video_studio_preview_concurrency_max) > int(
        settings.video_studio_preview_concurrency
    )


def preview_slots_added() -> int:
    """The preview slots this worker adds now above its own: the preview adapter's ``slots_added``. One for
    each preview queued beyond what its slots run - the previews it runs plus those queued in the lane at its last
    reconcile tick (:func:`note_queued`) -, :func:`preview_slots_most` at most; none once the queue is drawn."""
    if not adds_preview_slots():
        return 0
    own = max(0, int(settings.video_studio_preview_concurrency))
    room = preview_slots_most() - own
    if room <= 0:
        return 0
    return max(0, min(room, previews_running() + _QUEUED[0] - own))


@dataclass
class RenderExit:
    """How a render process ended, and what the supervisor saw of it."""

    #: ``done`` / ``retry`` / ``failed`` / ``lease_lost`` (its own outcome), ``crashed``, ``memory``,
    #: ``stalled``, ``cancelled``, ``superseded`` (a newer attempt of the render took it over),
    #: ``handed_over`` (the worker stops: the next one resumes it), ``gave_back`` (its slot asked back for a
    #: render that held one before it).
    kind: str = "crashed"
    error: str | None = None
    returncode: int | None = None
    peak_memory: int | None = None
    progress: int = 0
    stage: str | None = None
    drawing: list[str] = field(default_factory=list)
    #: A scene preview's process: held to the preview's ceiling, which its own setting raises.
    preview: bool = False
    #: The container's memory at the last sample before it ended, and its limit.
    container_memory: int | None = None
    container_limit: int | None = None
    #: Whether the kernel's out-of-memory killer killed a process of the container while it ran.
    oom_killed: bool = False
    #: It drew on one page (``render_preflight.ONE_PAGE_ENV``): a preview's ceiling stopped it once already.
    one_page: bool = False
    #: The platform's cause its own outcome named (``render_retry.cause_of_exception``): ``chromium``.
    cause: str | None = None

    def ending(self) -> str:
        """How the process ended: its signal or exit code - the out-of-memory killer's, when it was."""
        code = self.returncode
        if code is None:
            return "ended"
        if code < 0:
            try:
                name = signal.Signals(-code).name
            except ValueError:
                name = "an unknown signal"
            if self.oom_killed and -code == getattr(signal, "SIGKILL", 9):
                return f"was killed by the kernel's out-of-memory killer (signal {-code}, {name})"
            return f"was killed by signal {-code} ({name})"
        if code == 0:
            return "exited without a result"
        return f"exited with code {code}"

    def container(self) -> str:
        """``; the container used 3.9 GiB of its 4.0 GiB limit then`` - its memory as the process ended; empty unknown."""
        if self.container_memory is None:
            return ""
        used = resources.format_bytes(self.container_memory)
        if not self.container_limit:
            return f"; the container used {used} then"
        share = round(100 * self.container_memory / self.container_limit)
        return f"; the container used {used} of its {resources.format_bytes(self.container_limit)} limit ({share}%) then"

    def where(self) -> str:
        """``at 34% (Drawing frames (29%)), drawing scene 7 (clip) and scene 8 (statement)``."""
        text = f"at {self.progress}%"
        if self.stage:
            text += f" ({self.stage})"
        if self.drawing:
            scenes = self.drawing
            text += ", drawing " + (scenes[0] if len(scenes) == 1 else ", ".join(scenes[:-1]) + " and " + scenes[-1])
        return text

    def describe(self, *, ceiling: int | None, stall_seconds: float) -> str:
        peak = resources.format_bytes(self.peak_memory)
        if self.kind == "memory" and self.preview and self.one_page:
            return (
                f"The preview used {peak}, past its {resources.format_bytes(ceiling)} memory ceiling, {self.where()}, "
                "drawn on one page after its ceiling stopped it on more: it was stopped before it could take the worker "
                "down. The scene needs no change: raise VIDEO_STUDIO_PREVIEW_MEMORY_MB, the ceiling of a scene preview."
            )
        if self.kind == "memory" and self.preview:
            return (
                f"The preview used {peak}, past its {resources.format_bytes(ceiling)} memory ceiling, {self.where()}: "
                "it was stopped before it could take the worker down."
            )
        if self.kind == "memory":
            return (
                f"The render used {peak}, past its {resources.format_bytes(ceiling)} memory ceiling, "
                f"{self.where()}: it was stopped before it could take the worker down. The video and its quality "
                "need no change: the worker needs more memory than that - raise VIDEO_STUDIO_RENDER_MEMORY_MB "
                "(VIDEO_STUDIO_ULTRA_RENDER_MEMORY_MB for a 4K render), or the container's memory limit "
                f"({round(_CEILING_SHARE * 100)}% of it goes to a render, all but "
                f"{_gib(kept_from_renders())} to a 4K one)."
            )
        if self.kind == "stalled":
            return (
                f"The render stalled {self.where()}: no progress for {int(stall_seconds)} s, so it was stopped "
                f"(its memory peaked at {peak})."
            )
        return f"The render process {self.ending()} {self.where()} (its memory peaked at {peak}{self.container()})."


# ── The supervisor (the worker's side) ────────────────────────────────────


def _kill(process: asyncio.subprocess.Process, members: dict[int, int | None]) -> None:
    """SIGKILL the render process, its process group while it lives, and the processes it was seen to start.

    A member is killed only while it is still the process that was seen (its
    start time unchanged): a number the system gave to a new process meanwhile
    is never touched. Chromium leads a process group of its own, which is why
    the members are killed one by one and not only the group.
    """
    if os.name != "posix":
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        return
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
    for pid, started in members.items():
        if started is not None and resources.process_start(pid) != started:
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


def _stops(stopping: asyncio.Event | None, attempt: _Attempt | None) -> bool:
    """Whether *attempt*'s process is handed over now: its worker stops, or drains and hands it on at once."""
    return (stopping is not None and stopping.is_set()) or (attempt is not None and attempt.handing.is_set())


def _sample(pid: int) -> tuple[dict[int, int | None], int | None]:
    """The render's process tree and its memory: ``/proc`` reads, one ``smaps_rollup`` per process."""
    members = resources.snapshot_tree(pid)
    return members, resources.tree_memory(pid, list(members))


async def supervise(
    command: list[str],
    *,
    memory_limit: int | None,
    stall_seconds: float,
    cancel_event: asyncio.Event | None = None,
    superseded: asyncio.Event | None = None,
    stopping: asyncio.Event | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    poll_seconds: float = _POLL_SECONDS,
    cancel_grace_seconds: float = _CANCEL_GRACE_SECONDS,
    handover_close_seconds: float = HANDOVER_CLOSE_SECONDS,
    on_layout: Callable[[dict], None] | None = None,
    attempt: _Attempt | None = None,
    give_back: asyncio.Event | None = None,
    lane: bool = False,
) -> RenderExit:
    """Run a render process to its end under *memory_limit* and the stall interval; how it ended.

    *superseded* set, the process is killed at once: a newer attempt of the render has its execution.
    *stopping* set, it is killed too, ``handed_over``: the worker stops, and the next one resumes it - and so
    when its *attempt* is handed over while the worker drains (``_Attempt.handing``), the attempt told each
    progress its process reports. *give_back* set, it is killed, ``gave_back``: its slot goes to a render that held
    one before it. The container's memory is sampled with the render's, and its out-of-memory kills counted
    before and after: a SIGKILL the kernel sent for memory is said as such, with the memory at the time.
    *on_layout* receives the pages the process laid out (``render_preflight.layout_event``). With *lane* (a
    whole render or a scene range) the process goes to its worker's render lane before it starts its pages
    (``cpu_lanes.place``).
    """
    kills_before = resources.oom_kills()
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        start_new_session=os.name == "posix",
    )
    if lane:
        cpu_lanes.place(process.pid)
    if attempt is not None:
        attempt.pid = process.pid
    seen = RenderExit()
    outcome: dict = {}
    moved = time.monotonic()
    members: dict[int, int | None] = {}

    async def read() -> None:
        nonlocal moved
        if process.stdout is None:
            return
        while line := await process.stdout.readline():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "outcome":
                outcome.update(event)
            elif kind == "layout":
                if on_layout is not None:
                    on_layout(event)
            elif kind in ("progress", "stage", "heartbeat"):
                # A heartbeat: a frame loop between two progress reports, moving all the same.
                moved = time.monotonic()
                if kind == "progress":
                    with contextlib.suppress(TypeError, ValueError):
                        seen.progress = int(event.get("progress"))
                    if attempt is not None:
                        attempt.progress = seen.progress
                    seen.stage = str(event.get("stage") or "")[:200] or seen.stage
                    drawing = event.get("drawing")
                    seen.drawing = [str(scene) for scene in drawing][:6] if isinstance(drawing, list) else []

    reader = asyncio.create_task(read())
    exited = asyncio.create_task(process.wait())
    stopped: str | None = None
    try:
        while not exited.done():
            await asyncio.wait({exited}, timeout=poll_seconds)
            if exited.done():
                break
            # In a thread: reading a busy Chromium's ``smaps_rollup`` waits on
            # its memory map, and this loop is the one renewing the lease.
            sampled, memory = await asyncio.to_thread(_sample, process.pid)
            # Kept across samples: one taken as the render dies finds its
            # children already reparented, and must not forget them.
            members.update({pid: started for pid, started in sampled.items() if started is not None})
            seen.container_memory = resources.memory_usage()
            if memory is not None:
                seen.peak_memory = max(seen.peak_memory or 0, memory)
                if memory_limit and memory > memory_limit:
                    stopped = "memory"
            if stopped is None and stall_seconds > 0 and time.monotonic() - moved > stall_seconds:
                stopped = "stalled"
            if stopped is None and cancel_event is not None and cancel_event.is_set():
                stopped = "cancelled"
            if stopped is None and superseded is not None and superseded.is_set():
                stopped = "superseded"
            if stopped is None and _stops(stopping, attempt):
                stopped = "handed_over"
            if stopped is None and give_back is not None and give_back.is_set():
                stopped = "gave_back"
            if stopped is not None:
                break
        if stopped == "cancelled":
            with contextlib.suppress(ProcessLookupError):
                process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(asyncio.shield(exited), cancel_grace_seconds)
            except TimeoutError:
                _kill(process, resources.snapshot_tree(process.pid) or members)
        elif stopped == "handed_over" and hasattr(signal, "SIGUSR1"):
            # Its open segments closed at the frames reached and stored first: the next attempt resumes there,
            # never at the start of a segment each page had half drawn (#8958, 331 frames lost on 6 Nov 2026).
            with contextlib.suppress(ProcessLookupError):
                process.send_signal(signal.SIGUSR1)
            try:
                await asyncio.wait_for(asyncio.shield(exited), handover_close_seconds)
            except TimeoutError:
                _kill(process, resources.snapshot_tree(process.pid) or members)
        elif stopped is not None:
            _kill(process, resources.snapshot_tree(process.pid) or members)
        await exited
        # The lines written just before it exited.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(reader), 5.0)
    except BaseException:
        alive = resources.snapshot_tree(process.pid) if process.returncode is None else {}
        _kill(process, alive or members)
        raise
    finally:
        reader.cancel()
        # What it started and left behind (a crash leaves its Chromium and ffmpeg orphaned).
        _kill(process, {pid: started for pid, started in members.items() if pid != process.pid})
    seen.returncode = process.returncode
    kills_after = resources.oom_kills()
    seen.oom_killed = kills_before is not None and kills_after is not None and kills_after > kills_before
    seen.container_limit = resources.memory_limit()
    if stopped is not None:
        seen.kind = stopped
    elif outcome.get("kind") in _OWN_OUTCOMES:
        seen.kind = str(outcome["kind"])
        seen.error = outcome.get("error")
        seen.cause = outcome.get("cause") if isinstance(outcome.get("cause"), str) else None
    return seen


#: What the render process says of its own end: the job outcome, as it said it.
_OWN_OUTCOMES = ("done", "retry", "failed", "lease_lost")


#: The command-line flag of a render process drawing one scene range: ``--range <part> <digest>``.
_RANGE_FLAG = "--range"
#: Between a render's id and its range's place in the working directory of a range (:func:`range_workdir`).
_RANGE_SEPARATOR = "-range-"


def _command(render_id: str, workdir: str, *, part: int | None = None, digest: str | None = None) -> tuple[list[str], str]:
    import app

    backend = str(Path(app.__file__).resolve().parents[1])
    command = [sys.executable, "-m", "app.services.video_studio.render_process", render_id, workdir]
    if part is not None and digest:
        command += [_RANGE_FLAG, str(part), digest]
    return command, backend


def process_env(*, preview: bool) -> dict[str, str]:
    """The environment of a render process: the worker's - :data:`RENDER_ENVIRONMENT` where it sets none of them
    -, its process id, and its out-of-memory score."""
    adjustment = PREVIEW_OOM_SCORE_ADJ if preview else RENDER_OOM_SCORE_ADJ
    return {**RENDER_ENVIRONMENT, **os.environ, PARENT_ENV: str(os.getpid()), OOM_SCORE_ENV: str(adjustment)}


def range_workdir(render_id: str, part: int) -> Path:
    """The working directory of one scene range of *render_id* on this worker."""
    return workdir_of(f"{render_id}{_RANGE_SEPARATOR}{part}")


async def sweep_workdirs() -> None:
    """Remove the working directories on this worker no attempt will resume, never one an attempt runs in.

    A directory is kept between the attempts of its render; it goes once its
    render ended (or is gone), or once nothing touched it for
    :data:`_WORKDIR_KEPT_SECONDS` - the next attempt ran on another worker.
    Diagnostics: a sweep that fails is said, never what decides a render.
    """
    from sqlalchemy import select

    from app.core.database import async_session
    from app.models.video_studio import VideoRender

    now = time.time()
    stale: list[Path] = []
    by_render: dict[uuid.UUID, list[Path]] = {}
    try:
        for path in WORKDIR_ROOT.glob(f"{_WORKDIR_PREFIX}*"):
            name = path.name.removeprefix(_WORKDIR_PREFIX)
            if name in _ATTEMPTS or not path.is_dir():
                continue
            if now - path.stat().st_mtime > _WORKDIR_KEPT_SECONDS:
                stale.append(path)
            elif (render_id := safe_uuid(name.split(_RANGE_SEPARATOR, 1)[0])) is not None:
                # A scene range's directory is its render's: it goes once the render ended.
                by_render.setdefault(render_id, []).append(path)
        if by_render:
            async with async_session() as db:
                rows = await db.execute(select(VideoRender.id, VideoRender.status).where(VideoRender.id.in_(list(by_render))))
                status = dict(rows.all())
            stale += [
                path
                for render_id, paths in by_render.items()
                if render_id not in status or status[render_id] in JOB_TERMINAL_STATUSES
                for path in paths
            ]
        for path in stale:
            await asyncio.to_thread(shutil.rmtree, path, True)
            logger.info("Video render working directory %s removed: no attempt will resume it", path.name)
        # The figure drawings no render of this worker used for hours, and its render cache's files.
        if removed := await asyncio.to_thread(figure_cache.sweep_disk):
            logger.info("Video render figure drawings removed from this worker: %d unused for hours", removed)
        if removed := await asyncio.to_thread(render_cache.sweep_disk):
            logger.info("Video render cache files removed from this worker: %d unused for a day", removed)
        # The screen-insert frames no render of this worker read for hours, the rest held within their room.
        if removed := await asyncio.to_thread(insert_frames.sweep_disk):
            logger.info("Video render insert drawings removed from this worker: %d unused for hours", removed)
        await asyncio.to_thread(insert_frames.bound)
    except Exception as exc:
        logger.warning("Video render working directories were not swept: %s", format_exception_message(exc))


def _kept_work(workdir: Path) -> str:
    """What a former attempt left in *workdir* that this one keeps, in words; empty for nothing."""
    counts = {
        "frame": compositor.frames_kept(workdir),
        "figure frame": sum(1 for _ in workdir.glob("figure-*/f*.jpg")),
    }
    return " and ".join(f"{count} {noun}{'' if count == 1 else 's'}" for noun, count in counts.items() if count)


async def run_isolated(
    render_id: str,
    *,
    cancel_event: asyncio.Event | None = None,
    last_attempt: bool = True,
    lease: tuple[uuid.UUID, str] | None = None,
    attempt: tuple[int, int] | None = None,
    stopping: asyncio.Event | None = None,
    resumed: str | None = None,
    retried: dict | None = None,
    give_back: asyncio.Event | None = None,
) -> JobOutcome:
    """Run one attempt of a render in its own process, and settle the render row on a crash, a stall or the ceiling.

    The worker makes the render's working directory (:func:`workdir_of`): a
    process stopped by the supervisor cannot clean up after itself, and its
    media and chunks would stay in the worker's ``/tmp``. It is removed once
    the attempt ended the render, and kept when the attempt runs it again
    (:data:`_RUNS_AGAIN`): the next attempt on this worker resumes
    there, and an attempt on another worker from the segments kept beside the
    render in the store (``render_segments``), which go once it ended.
    *lease* (the execution and the worker holding it) is handed to the
    process, which renews it from the frames it draws; *attempt* is ``(this
    attempt, the attempts the execution allows)``. *stopping* set - the
    worker stops, a deploy or a restart - the process is stopped and the
    attempt ends ``handover`` (:func:`hand_over`): the execution goes back
    to the queue with its attempt unspent. *resumed* is what the
    render resumes after, when a stopped worker left it: ``after a
    deploy (1.4.1 -> 1.4.2)``, ``after its worker restarted``, or how a
    worker gone went - said on its row as it starts again. *retried* is the
    run again its execution already had after the platform failed it
    (``render_retry.record_of``): ``None``, a failure of the platform
    on its last attempt runs it again once (``again``). *give_back* set - the
    slot a scene preview runs in was lent by the render lane, which takes it
    back for a render a stopped worker left - it stops as at a stop and
    ends ``given_back``, its frames kept, to resume first in its own lane; a
    whole render whose own slot is asked back for a render that held one
    before it ends ``gave_back`` (:func:`give_slot_back`).
    """
    this = _Attempt()
    earlier = _ATTEMPTS.get(render_id)
    _ATTEMPTS[render_id] = this
    workdir = workdir_of(render_id)
    outcome: JobOutcome | None = None
    try:
        if earlier is not None:
            # The engine gave the execution to this attempt: the one before lost it, and shares the directory.
            logger.warning("Video render %s: its earlier attempt on this worker lost its execution and is stopped", render_id)
            earlier.superseded.set()
            await earlier.ended.wait()
        await sweep_workdirs()
        # The worker has the render from here, seconds before its process reports
        # anything: it is no longer waiting for a worker. Diagnostics, like
        # the process's own reports: it never costs the render an attempt.
        # Read before the heartbeat below moves it: how long ago the render last moved, on the worker that stopped.
        lost_since = await _last_moved(render_id) if resumed else None
        try:
            await jobs.heartbeat("render", render_id, "Starting the render process")
        except Exception as exc:
            logger.warning("Video render %s: its start was not recorded: %s", render_id, format_exception_message(exc))
        kept = _kept_work(workdir) if workdir.is_dir() else ""
        workdir.mkdir(parents=True, exist_ok=True)
        if resumed:
            # The time its worker's stop cost it is given back to its deadline (#11782), then the relaunch is counted
            # on its row with it, so a reader tells a relaunch from progress and a timeout says what relaunches cost.
            lost, given = await give_relaunch_time_back(render_id, lease, since=lost_since)
            await record_relaunch(
                render_id,
                resumed=resumed,
                attempt=attempt,
                frames_kept=compositor.frames_kept(workdir),
                lost_seconds=lost,
                given_back_seconds=given,
            )
        if kept or resumed:
            said = f"Resuming {resumed}" if resumed else "Resuming"
            said += f" (attempt {attempt[0]} of {attempt[1]})" if attempt else ""
            said += f": {kept} kept" if kept else ""
            logger.warning("Video render %s: %s.", render_id, said, extra={"render_id": render_id})
            try:
                await jobs.mark_if_open("render", render_id, stage=said[:200], progress_at=datetime.now(UTC))
            except Exception as exc:
                logger.warning("Video render %s: its resume was not recorded: %s", render_id, format_exception_message(exc))
        outcome = await _supervised(
            render_id,
            workdir,
            this,
            cancel_event=cancel_event,
            last_attempt=last_attempt,
            lease=lease,
            attempt=attempt,
            stopping=stopping,
            retried=retried,
            give_back=give_back,
        )
        return outcome
    finally:
        if outcome is not None and outcome.kind not in _RUNS_AGAIN:
            shutil.rmtree(workdir, ignore_errors=True)
            # Replaced by the render of a newer revision still to draw: its segments are that render's to take.
            # Failed by its checks after its drawing: kept for the next render of the revision, until the sweep.
            await render_segments.forget_unless_kept(render_id)
            try:
                await render_segments.forget_handed_to(render_id)
            except Exception as exc:  # the hourly sweep deletes them
                logger.info("Video render %s: the segments handed to it not deleted: %s", render_id, format_exception_message(exc))
            # Its scene ranges and what they were drawn from: the render ended.
            await render_ranges.forget(render_id)
        this.ended.set()
        if _ATTEMPTS.get(render_id) is this:
            del _ATTEMPTS[render_id]


async def run_range_isolated(
    render_id: str,
    part: int,
    digest: str,
    *,
    cancel_event: asyncio.Event | None = None,
    last_attempt: bool = True,
    lease: tuple[uuid.UUID, str] | None = None,
    stopping: asyncio.Event | None = None,
    give_back: asyncio.Event | None = None,
) -> JobOutcome:
    """Draw one scene range of a render on this worker, in a process of its own: a range execution's attempt.

    The same supervision as a render's (:func:`supervise`): its memory ceiling, its stall window - a stall resumed
    in place, its finished segments kept, :data:`MAX_STALL_RESUMES` times -, its lease renewed from the process, a
    worker that stops handing it over. What ends it writes the range's row only, never the render's: a range that
    cannot be drawn here (its memory, a stall past its resumes, a crash on its last attempt) is ``failed``, and the
    render draws what it lacks (``render_ranges.draw_all``).
    """
    key = f"{render_id}{_RANGE_SEPARATOR}{part}"
    this = _Attempt()
    earlier = _ATTEMPTS.get(key)
    _ATTEMPTS[key] = this
    workdir = range_workdir(render_id, part)
    outcome: JobOutcome | None = None
    try:
        if earlier is not None:
            earlier.superseded.set()
            await earlier.ended.wait()
        workdir.mkdir(parents=True, exist_ok=True)
        command, backend = _command(render_id, str(workdir), part=part, digest=digest)
        ceiling = ceiling_of(await _render_of(render_id), key=key)
        this.ceiling = ceiling
        if ceiling <= 0:
            # Left to the render, which draws what its ranges lack (``render_ranges.draw_all``).
            row = await render_ranges.find(render_id, digest, part)
            if row is not None:
                await render_ranges.fail_open(row.id, f"Range {part + 1}: {no_room()} The render draws it.")
            outcome = JobOutcome("failed", no_room())
            return outcome
        stall = float(max(0, settings.video_studio_render_stall_seconds))
        env = process_env(preview=False)
        # Its pages laid out in the room it has now, never in what a pre-flight read earlier.
        env[render_preflight.ROOM_ENV] = str(ceiling)
        # Its frames taken as this worker's probe settled.
        env[compositor.FRAME_CAPTURE_ENV] = (await compositor.worker_capture()).mode
        if lease is not None:
            env[EXECUTION_ENV], env[OWNER_ENV] = str(lease[0]), lease[1]
            env[LEASE_ENV] = str(jobs.RENDER_LEASE_SECONDS)
        resumes = 0
        while True:
            ended = await supervise(
                command,
                memory_limit=ceiling,
                stall_seconds=stall,
                cancel_event=cancel_event,
                superseded=this.superseded,
                stopping=stopping,
                cwd=backend,
                env=env,
                on_layout=_holding(this),
                attempt=this,
                give_back=give_back,
                lane=True,
            )
            if ended.kind != "stalled" or resumes >= MAX_STALL_RESUMES or (cancel_event is not None and cancel_event.is_set()):
                break
            if lease is not None:
                try:
                    await renew(*lease, lease_seconds=jobs.RENDER_LEASE_SECONDS)
                except LeaseLostError as exc:
                    outcome = JobOutcome("lease_lost", f"The range's lease is gone: {exc}"[:2000])
                    return outcome
            resumes += 1
            logger.warning("Video render %s: range %d stalled, resumed in place (%d of %d)", render_id, part, resumes, MAX_STALL_RESUMES)
        logger.info(
            "Video render %s: the process of range %d ended (%s, %s), memory peak %s",
            render_id,
            part,
            ended.kind,
            ended.ending(),
            resources.format_bytes(ended.peak_memory),
            extra={"render_id": render_id, "render_range": part, "render_exit": ended.kind},
        )
        outcome = await _settle_range(render_id, part, digest, ended, last_attempt=last_attempt, ceiling=ceiling, stall=stall)
        return outcome
    finally:
        if outcome is not None and outcome.kind not in _RUNS_AGAIN:
            shutil.rmtree(workdir, ignore_errors=True)
        this.ended.set()
        if _ATTEMPTS.get(key) is this:
            del _ATTEMPTS[key]


async def _settle_range(
    render_id: str, part: int, digest: str, ended: RenderExit, *, last_attempt: bool, ceiling: int | None, stall: float
) -> JobOutcome:
    """The job outcome of a range process that ended; its row ``failed`` when it cannot be drawn here."""
    if ended.kind in _OWN_OUTCOMES:
        return JobOutcome(ended.kind, ended.error)
    if ended.kind == "superseded":
        return JobOutcome("lease_lost", "A newer attempt of the range has its execution on this worker.")
    if ended.kind == "handed_over":
        return JobOutcome("handover", f"Range {part + 1} handed to the next worker: this one stopped.")
    if ended.kind == "gave_back":
        return JobOutcome(
            "gave_back",
            f"Range {part + 1} gave its slot back at {ended.progress}% to a render that held one before it, its worker "
            "having stopped: it resumes when a slot frees, from its finished segments.",
        )
    if ended.kind == "cancelled":
        return JobOutcome("done")
    message = ended.describe(ceiling=ceiling, stall_seconds=stall)
    if ended.kind == "crashed" and not last_attempt:
        return JobOutcome("retry", message)
    row = await render_ranges.find(render_id, digest, part)
    if row is not None:
        await render_ranges.fail_open(row.id, f"Range {part + 1}: {message} The render draws it.")
    return JobOutcome("failed", message)


async def _supervised(
    render_id: str,
    workdir: Path,
    this: _Attempt,
    *,
    cancel_event: asyncio.Event | None,
    last_attempt: bool,
    lease: tuple[uuid.UUID, str] | None,
    attempt: tuple[int, int] | None,
    stopping: asyncio.Event | None = None,
    retried: dict | None = None,
    give_back: asyncio.Event | None = None,
) -> JobOutcome:
    """The render process of one attempt run to its end in *workdir*, resumed in place after a stall.

    A scene preview stopped at its memory ceiling runs again at once, in the
    same directory, on one page (``render_preflight.ONE_PAGE_ENV``): its insert
    layers then composed one after the other, never side by side. A whole
    render's room is read as it launches (:func:`launch_room`), and once
    its process laid out its pages it holds their budget of the worker. Its
    frames are taken as this worker's probe settled (``compositor.worker_capture``);
    begun frames that fail it - its Chromium, a crash, a stall - make
    this worker take screenshots from then on (``compositor.demote_capture``).
    """
    command, backend = _command(render_id, str(workdir))
    row = await _render_of(render_id)
    preview = bool(getattr(row, "scene_id", None))
    this.preview = preview
    # A preview and everything it starts take the CPU a whole render's pages leave.
    command = resources.niced(command, nice_for(preview))
    # A preview starts as its admission said: its ceiling, on its pages, and where it came to be drawn.
    admitted = admission_of(render_id) if preview else None
    inserts = await preview_inserts(row) if preview else None
    ceiling = admitted.ceiling if admitted is not None else ceiling_of(row, inserts, key=render_id)
    this.ceiling = ceiling
    if not preview and ceiling <= 0:
        message = no_room()
        logger.warning("Video render %s failed: %s", render_id, message)
        if not await jobs.mark_if_open("render", render_id, status=JOB_FAILED, error=message, finished_at=datetime.now(UTC), stage=None):
            return JobOutcome("done")
        return JobOutcome("failed", message)
    stall = float(max(0, settings.video_studio_render_stall_seconds))
    logger.info(
        "Video render %s: starting its process (memory ceiling %s, stalled after %s)",
        render_id,
        resources.format_bytes(ceiling),
        f"{int(stall)} s" if stall else "never",
        extra={"render_id": render_id, "memory_ceiling_bytes": ceiling},
    )
    env = process_env(preview=preview)
    # Its pages laid out in the room it has now, never in what a pre-flight read earlier.
    env[render_preflight.ROOM_ENV] = str(ceiling)
    # Run again after its memory ceiling stopped it: on fewer pages, never the layout that failed.
    lighter = None if preview else render_retry.lighter_pages(retried)
    if lighter is not None:
        env[render_preflight.RETRY_PAGES_ENV] = str(lighter)
    capture = env[compositor.FRAME_CAPTURE_ENV] = (await compositor.worker_capture()).mode
    if lease is not None:
        env[EXECUTION_ENV], env[OWNER_ENV] = str(lease[0]), lease[1]
        env[LEASE_ENV] = str(jobs.RENDER_LEASE_SECONDS)
    if admitted is not None and admitted.note:
        env[render_preflight.ADMISSION_NOTE_ENV] = admitted.note
        logger.info("Video render %s: %s", render_id, admitted.note, extra={"render_id": render_id})
    started = time.monotonic()
    resumes = 0
    leaving = _Leaving(stopping, give_back, this.handing)
    while True:
        if this.superseded.is_set():
            ended = RenderExit(kind="superseded")
            break
        if leaving.is_set():
            ended = RenderExit(kind="handed_over", progress=this.progress)
            break
        ended = await supervise(
            command,
            memory_limit=ceiling,
            stall_seconds=stall,
            cancel_event=cancel_event,
            superseded=this.superseded,
            stopping=leaving,
            cwd=backend,
            env=env,
            on_layout=None if preview else _holding(this),
            attempt=this,
            lane=not preview,
        )
        ended.preview = preview
        ended.one_page = render_preflight.ONE_PAGE_ENV in env
        if capture == "begin_frame" and (ended.cause == render_retry.CHROMIUM or ended.kind in ("crashed", "stalled")):
            # Begun frames failed a render here: this worker, and this render's next run, take screenshots.
            compositor.demote_capture(ended.error or f"a render {ended.kind} {ended.where()}")
            capture = env[compositor.FRAME_CAPTURE_ENV] = "screenshot"
        if ended.kind == "memory" and preview and not ended.one_page:
            env[render_preflight.ONE_PAGE_ENV] = "1"
            env[render_preflight.ONE_PAGE_NOTE_ENV] = (
                "Drawn on one page" + (", its screen inserts composed one after the other" if inserts else "")
                + f": on {render_preflight.preview_pages()} pages it passed its {resources.format_bytes(ceiling)} memory "
                "ceiling."
            )
            said = f"Drawing again on one page: {ended.describe(ceiling=ceiling, stall_seconds=stall)}"
            logger.warning("Video render %s: %s", render_id, said, extra={"render_id": render_id, "render_exit": "memory"})
            if lease is not None:
                try:
                    await renew(*lease, lease_seconds=jobs.RENDER_LEASE_SECONDS)
                except LeaseLostError as exc:
                    return JobOutcome("lease_lost", f"The render's lease is gone: {exc}"[:2000])
            try:
                await jobs.mark_if_open("render", render_id, stage=said[:200], progress_at=datetime.now(UTC))
            except Exception as exc:  # diagnostics: the preview runs again whatever its row says
                logger.warning("Video render %s: its one-page run was not recorded: %s", render_id, format_exception_message(exc))
            continue
        if ended.kind != "stalled" or resumes >= MAX_STALL_RESUMES:
            break
        if cancel_event is not None and cancel_event.is_set():
            break
        # Resumed in place: the same execution, its lease taken again, the frames finished kept.
        if lease is not None:
            try:
                await renew(*lease, lease_seconds=jobs.RENDER_LEASE_SECONDS)
            except LeaseLostError as exc:
                return JobOutcome("lease_lost", f"The render's lease is gone: {exc}"[:2000])
        resumes += 1
        kept = compositor.frames_kept(workdir)
        await record_stall(render_id, ended, stall_seconds=stall, frames_kept=kept, resumed=True)
        logger.warning(
            "Video render %s: %s Resumed in place (%d of %d), %d frames kept.",
            render_id,
            ended.describe(ceiling=ceiling, stall_seconds=stall),
            resumes,
            MAX_STALL_RESUMES,
            kept,
            extra={"render_id": render_id, "render_stall": resumes, "frames_kept": kept},
        )
    logger.info(
        "Video render %s: its process ended (%s, %s) after %.0f s, memory peak %s",
        render_id,
        ended.kind,
        ended.ending(),
        time.monotonic() - started,
        resources.format_bytes(ended.peak_memory),
        extra={"render_id": render_id, "render_exit": ended.kind, "peak_memory_bytes": ended.peak_memory},
    )
    if ended.kind == "superseded":
        return JobOutcome("lease_lost", "A newer attempt of the render has its execution on this worker.")
    if ended.kind == "handed_over":
        stopped = stopping is not None and stopping.is_set()
        given_back = give_back is not None and give_back.is_set() and not stopped
        if given_back and not preview:
            # A whole render's own slot, asked back for a render that held one before it.
            return await give_slot_back(render_id, ended, workdir)
        return await hand_over(render_id, ended, workdir, given_back=given_back, early=this.handing.is_set())
    if ended.kind == "stalled":
        await record_stall(render_id, ended, stall_seconds=stall, frames_kept=compositor.frames_kept(workdir), resumed=False)
    return await settle(
        render_id,
        ended,
        last_attempt=last_attempt,
        ceiling=ceiling,
        stall_seconds=stall,
        resumes=resumes,
        attempt=attempt,
        again=retried is None,
        retried=retried,
        pages=None if preview else (this.pages or None),
    )


async def hand_over(
    render_id: str, ended: RenderExit, workdir: Path, *, given_back: bool = False, early: bool = False
) -> JobOutcome:
    """The worker stops with *render_id* in flight: ``handover``, its attempt unspent, its frames kept.

    A deploy or a restart is no failure of the render: the adapter hands the
    execution back to the queue (``AdapterResult.defer``), first, where the
    next worker takes it at once and resumes from the segments it finished -
    kept in *workdir* and beside the render in the store (``render_segments``)
    -, saying whether it resumed after a deploy or a restart. With
    *given_back*, the worker goes on: the scene preview gave the slot it was
    lent back to a render a stopped worker left, ``given_back``, and
    resumes first in its own lane. *early*: handed over as its worker began to
    drain, under :data:`DRAIN_PAST_PROGRESS` with a running worker to take it,
    never waited for.
    """
    from app.services.orchestration_reconciler import stop_signal

    kept = compositor.frames_kept(workdir)
    if given_back:
        said = (
            f"Gave its slot back at {ended.progress}% to a whole render a stopped worker left (the render lane lent it "
            f"that slot); it resumes first in the preview lane from its {kept} finished frames"
        )
    else:
        when = (
            f"under {DRAIN_PAST_PROGRESS}% as its worker was asked to stop ({stop_signal()}), to a worker running now"
            if early
            else f"its worker was asked to stop ({stop_signal()}) before the render ended"
        )
        said = f"Handed to the next worker at {ended.progress}%: {when}; it resumes first in the queue from its {kept} finished frames"
    logger.warning("Video render %s: %s.", render_id, said, extra={"render_id": render_id, "frames_kept": kept})
    try:
        await jobs.mark_if_open("render", render_id, stage=said[:200], progress_at=datetime.now(UTC))
    except Exception as exc:  # diagnostics: the execution is handed back whatever the row says
        logger.warning("Video render %s: its handover was not recorded: %s", render_id, format_exception_message(exc))
    return JobOutcome("given_back" if given_back else "handover", f"{said}."[:2000])


async def give_slot_back(render_id: str, ended: RenderExit, workdir: Path) -> JobOutcome:
    """*render_id*'s slot was asked back for a render that held one before it and lost it to a stopping worker:
    ``gave_back``, its attempt unspent, its frames kept - it waits in its place in the queue for the next slot."""
    kept = compositor.frames_kept(workdir)
    said = (
        f"Gave its slot back at {ended.progress}% to a render that held one before it, whose worker stopped; it resumes "
        f"in its place in the queue when a slot frees, from its {kept} finished frames"
    )
    logger.warning("Video render %s: %s.", render_id, said, extra={"render_id": render_id, "frames_kept": kept})
    try:
        await jobs.mark_if_open("render", render_id, stage=said[:200], progress_at=datetime.now(UTC))
    except Exception as exc:  # diagnostics: the execution goes back to the queue whatever the row says
        logger.warning("Video render %s: its slot given back was not recorded: %s", render_id, format_exception_message(exc))
    return JobOutcome("gave_back", f"{said}."[:2000])


def drain_seconds() -> float:
    """How long a stopping worker waits, from the stop, for the whole renders it draws past :data:`DRAIN_PAST_PROGRESS`:
    ``VIDEO_STUDIO_RENDER_DRAIN_SECONDS``, never past what its deployment lets a stop take - its termination
    grace (``WORKER_TERMINATION_GRACE_SECONDS``, Kubernetes' default 30 s when not given) less
    :data:`DRAIN_MARGIN_SECONDS` for the hand-over after it."""
    asked = max(0, int(settings.video_studio_render_drain_seconds))
    grace = settings.worker_termination_grace_seconds or jobs.DEFAULT_TERMINATION_GRACE_SECONDS
    return float(max(0, min(asked, grace - DRAIN_MARGIN_SECONDS)))


def _renders_drawing() -> list[tuple[str, _Attempt]]:
    """The whole renders and scene ranges this worker draws, by key: a scene preview is never waited for."""
    return [(key, attempt) for key, attempt in _ATTEMPTS.items() if not attempt.preview]


def incoming_wait_seconds() -> float:
    """How long, from the stop, a draining worker keeps drawing a render under :data:`DRAIN_PAST_PROGRESS` for a worker
    of the incoming release to come up: :data:`INCOMING_WAIT_SECONDS`, never past :func:`drain_seconds`. Past
    it - a plain restart or a scale-down, where no other release ever comes - any running worker takes it."""
    return float(min(INCOMING_WAIT_SECONDS, drain_seconds()))


async def hand_over_early() -> list[str]:
    """While its worker drains: the renders under :data:`DRAIN_PAST_PROGRESS` handed over at once when a running
    worker of the incoming release claims renders - it takes each in a slot it kept for it -, the rest left to
    draw for the stop to wait for (:func:`finish_before_stop`). A running worker of this worker's own release is
    stopped by the same rolling update next: it takes them only once :func:`incoming_wait_seconds` passed from the
    stop with no incoming worker. The keys handed over."""
    early = [(key, attempt) for key, attempt in _renders_drawing() if attempt.progress < DRAIN_PAST_PROGRESS and not attempt.handing.is_set()]
    if not early:
        return []
    from app.services import worker_presence
    from app.services.orchestration_reconciler import stop_asked_at

    began = stop_asked_at()
    waited_enough = began is None or time.monotonic() - began >= incoming_wait_seconds()
    try:
        takers = await worker_presence.count(claiming=jobs.RENDER_ADAPTER, incoming=True)
        which = "of the incoming release"
        if not takers and waited_enough:
            takers = await worker_presence.count(claiming=jobs.RENDER_ADAPTER)
            which = "running (none of another release came up)"
    except Exception as exc:  # unknown: they keep drawing, for the stop to hand them over
        logger.debug("Workers running not read: %s", format_exception_message(exc))
        return []
    if not takers:
        return []
    for key, attempt in early:
        logger.info("Video render %s: at %d%% as its worker drains, handed to one of the %d workers %s", key, attempt.progress, takers, which)
        attempt.handing.set()
    return [key for key, _attempt in early]


async def finish_before_stop() -> None:
    """Before the worker stops: wait for the whole renders it draws past :data:`DRAIN_PAST_PROGRESS` to end,
    up to :func:`drain_seconds` from the stop - a deploy never hands over a render nearly done, which would wait for a
    slot elsewhere. What still draws then is handed over by the stop, as before."""
    from app.services.orchestration_reconciler import stop_asked_at

    began = stop_asked_at() or time.monotonic()
    deadline = began + drain_seconds()
    said = False
    while True:
        waited = [(key, attempt.progress) for key, attempt in _renders_drawing() if attempt.progress >= DRAIN_PAST_PROGRESS]
        left = deadline - time.monotonic()
        if not waited or left <= 0:
            break
        if not said:
            said = True
            logger.warning(
                "Worker stop: waiting up to %.0f s for %s past %d%% to end before handing it over (%s)",
                left,
                "a render" if len(waited) == 1 else f"{len(waited)} renders",
                DRAIN_PAST_PROGRESS,
                ", ".join(f"{key} at {progress}%" for key, progress in waited),
            )
        await asyncio.sleep(min(_DRAIN_POLL_SECONDS, left))
    if said:
        ended = [key for key, _progress in waited] if waited else []
        logger.warning(
            "Worker stop: %s after %.0f s",
            f"still drawing {', '.join(ended)}: handed over" if ended else "the renders it waited for ended",
            time.monotonic() - began,
        )


#: How many times a render the engine dead-lettered for a lease that lapsed while it moved runs again.
MAX_LEASE_RESUMES = 3
#: The execution payload key that counts them.
LEASE_RESUMES_KEY = "lease_resumes"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def resume_after_lost_lease(execution, expiry: LeaseExpiry) -> bool:
    """Run again a render whose last attempt lost a lease while the render still moved; whether it did.

    "No attempt left" is for a render that stopped moving. One whose progress
    was reported within the stall window before the engine took its lease back
    was drawing: its execution is queued again (``requeue_execution``: its
    attempts back, its deadline kept) and the next attempt resumes from its
    finished segments, on this worker's disk or from the store
    (``render_segments``). Bounded: :data:`MAX_LEASE_RESUMES` times, within the
    render's deadline.
    """
    from app.core.database import async_session
    from app.services import orchestration_engine as engine

    payload = execution.payload or {}
    render_id = payload.get("render_id")
    resumes = int(payload.get(LEASE_RESUMES_KEY) or 0)
    now = datetime.now(UTC)
    if not render_id or resumes >= MAX_LEASE_RESUMES:
        return False
    if execution.deadline_at is not None and _aware(execution.deadline_at) <= now:
        return False
    row = await jobs.load("render", render_id)
    moved_at = getattr(row, "progress_at", None)
    if row is None or row.status in JOB_TERMINAL_STATUSES or moved_at is None:
        return False
    taken_back = expiry.last_heartbeat_at + timedelta(seconds=expiry.silent_seconds)
    window = float(settings.video_studio_render_stall_seconds) or float(jobs.RENDER_LEASE_SECONDS)
    if (taken_back - _aware(moved_at)).total_seconds() > window:
        return False
    said = (
        f"Resuming after a lost lease at {row.progress}% ({resumes + 1} of {MAX_LEASE_RESUMES}): "
        f"its lease lapsed ({expiry.describe()}) while it still moved, its finished frames kept"
    )
    async with async_session() as db:
        requeued = await engine.requeue_execution(db, execution_id=execution.id)
        if requeued is None:
            return False
        requeued.payload = {**(requeued.payload or {}), LEASE_RESUMES_KEY: resumes + 1}
        requeued.last_error = said[:2000]
        await db.commit()
    if not await jobs.mark_if_open("render", render_id, status=JOB_PENDING, stage=said[:200], progress_at=now):
        # Cancelled or replaced meanwhile: it keeps the end its cancel wrote, and never runs again.
        async with async_session() as db:
            await engine.cancel_execution(db, execution_id=execution.id)
            await db.commit()
        return True
    logger.warning("Video render %s: %s.", render_id, said, extra={"render_id": render_id, "lease_resumes": resumes + 1})
    await jobs.wake()
    return True


async def record_stall(
    render_id: str, ended: RenderExit, *, stall_seconds: float, frames_kept: int, resumed: bool
) -> dict:
    """Add a stall to the render's row (``VideoRender.stalls``); resumed, its stage says it starts again."""
    entry = {
        "at": datetime.now(UTC).isoformat(),
        "progress": ended.progress,
        "stage": ended.stage,
        "drawing": ended.drawing[:3],
        "seconds": int(stall_seconds),
        "frames_kept": frames_kept,
        "resumed": resumed,
    }
    try:
        row = await jobs.load("render", render_id)
        if row is None or row.status in JOB_TERMINAL_STATUSES:
            return entry
        changes: dict = {"stalls": [*(getattr(row, "stalls", None) or []), entry][-_STALLS_KEPT:]}
        if resumed:
            changes["stage"] = f"Resuming after a stall at {ended.progress}%: {frames_kept} frames kept"[:200]
            changes["progress_at"] = datetime.now(UTC)
        # Never over a render cancelled meanwhile: it keeps the end its cancel wrote.
        await jobs.mark_if_open("render", render_id, **changes)
    except Exception as exc:  # diagnostics: never what decides the render
        logger.warning("Video render %s: its stall was not recorded: %s", render_id, format_exception_message(exc))
    return entry


def stall_line(stalls: list | None) -> str:
    """What ``get_video_jobs`` says of a render's stalls: each with where it stood and what it kept; ``""`` for none."""
    entries = [entry for entry in stalls or [] if isinstance(entry, dict)]
    if not entries:
        return ""

    def said(entry: dict) -> str:
        where = f"at {entry.get('progress') or 0}%" + (f" ({entry['stage']})" if entry.get("stage") else "")
        outcome = (
            f"resumed in place, {entry.get('frames_kept') or 0} frames kept"
            if entry.get("resumed")
            else "stopped: no resume left"
        )
        return f"{where}, no progress for {entry.get('seconds') or 0} s, {outcome}"

    count = len(entries)
    return f"Stalled {count} time{'s' if count > 1 else ''}: " + "; ".join(said(e) for e in entries[-3:]) + "."


async def _last_moved(render_id: str) -> datetime | None:
    """When the render last moved (``progress_at``), read before its new attempt reports anything; ``None`` unknown."""
    try:
        row = await jobs.load("render", render_id)
    except Exception:  # unknown: nothing given back
        return None
    moved = getattr(row, "progress_at", None) if row is not None else None
    return _aware(moved) if isinstance(moved, datetime) else None


async def give_relaunch_time_back(
    render_id: str, lease: tuple[uuid.UUID, str] | None, *, since: datetime | None
) -> tuple[int, int]:
    """Give the render's deadline back the time its worker's stop cost it (#11782): ``(seconds lost, seconds given
    back)``.

    Lost: from its last progress before the stop (*since*) to now, when its next attempt starts - the grace of the
    stopping worker, the wait for the next one, the relaunch itself - :data:`RELAUNCH_CREDIT_MOST_SECONDS` at most.
    Given back to its execution's deadline (``orchestration_engine.give_deadline_back``), never past one more
    ``render_preflight.wall_seconds`` after the deadline it was queued with: a render handed on by every deploy never
    runs forever. The running task's timeout follows the row at its next heartbeat
    (``orchestration_reconciler._follow_deadline``). Best effort: ``(lost, 0)`` when the deadline could not be
    moved, the render then ends at the deadline it had.
    """
    if since is None:
        return 0, 0
    lost = int(min(max(0.0, (datetime.now(UTC) - since).total_seconds()), float(RELAUNCH_CREDIT_MOST_SECONDS)))
    if lost <= 0 or lease is None:
        return lost, 0
    from app.core.database import async_session
    from app.services import orchestration_engine as engine

    execution_id, owner = lease
    try:
        row = await jobs.load("render", render_id)
        most = float(render_preflight.wall_seconds(getattr(row, "quality", None)))
        async with async_session() as db:
            _deadline, given = await engine.give_deadline_back(
                db, execution_id=execution_id, worker_id=owner, seconds=lost, most_seconds=most
            )
            await db.commit()
    except Exception as exc:  # the render keeps the deadline it had
        logger.info(
            "Video render %s: its relaunch time was not given back: %s", render_id, format_exception_message(exc)
        )
        return lost, 0
    if given > 0:
        logger.info(
            "Video render %s: %d s its worker's stop cost it given back to its deadline",
            render_id,
            int(given),
            extra={"render_id": render_id, "relaunch_lost_seconds": lost, "given_back_seconds": int(given)},
        )
    return lost, int(given)


def relaunch_cost(relaunches: list[object] | None) -> tuple[int, int]:
    """What a render's relaunches cost it and what was given back to its deadline, in seconds, summed over the
    entries its row keeps (``lost_seconds``, ``given_back_seconds``)."""
    lost = given = 0
    for entry in relaunches or []:
        if not isinstance(entry, dict):
            continue
        for key, total in (("lost_seconds", "lost"), ("given_back_seconds", "given")):
            value = entry.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                if total == "lost":
                    lost += value
                else:
                    given += value
    return lost, given


def relaunch_cost_words(relaunches: list[object] | None) -> str:
    """What a timed-out render's error adds of its relaunches (#11782): ``""`` for a render never relaunched."""
    entries = [entry for entry in relaunches or [] if isinstance(entry, dict)]
    if not entries:
        return ""
    lost, given = relaunch_cost(entries)
    count = len(entries)
    said = f"It was relaunched {count} time{'s' if count > 1 else ''} after its worker stopped"
    if lost:
        said += f", which cost it about {_minutes(lost)}"
        said += f", {_minutes(given)} of it given back to its time limit" if given else ", none of it given back"
    return said + "."


def _minutes(seconds: int) -> str:
    return f"{seconds} s" if seconds < 90 else f"{round(seconds / 60)} min"


async def record_relaunch(
    render_id: str,
    *,
    resumed: str,
    attempt: tuple[int, int] | None,
    frames_kept: int,
    lost_seconds: int = 0,
    given_back_seconds: int = 0,
) -> dict | None:
    """Add a worker change to the render's row (``VideoRender.relaunches``): the attempt, what it resumed after
    and where it stood, the time the stop cost it and what was given back to its deadline
    (:func:`give_relaunch_time_back`); :func:`record_resumed_from` adds the frame its drawing resumes from. ``None``
    on a render ended."""
    try:
        row = await jobs.load("render", render_id)
        if row is None or row.status in JOB_TERMINAL_STATUSES:
            return None
        entry = {
            "at": datetime.now(UTC).isoformat(),
            "attempt": attempt[0] if attempt else None,
            "attempts": attempt[1] if attempt else None,
            "after": resumed[:200],
            "progress": int(getattr(row, "progress", 0) or 0),
            "frames_kept": frames_kept,
            # The release that takes it: more relaunches than releases says a deploy handed it twice.
            "version": settings.app_version,
        }
        if lost_seconds > 0:
            entry["lost_seconds"] = int(lost_seconds)
            entry["given_back_seconds"] = int(given_back_seconds)
        relaunches = [*(getattr(row, "relaunches", None) or []), entry][-_RELAUNCHES_KEPT:]
        # Never over a render cancelled meanwhile: it keeps the end its cancel wrote.
        await jobs.mark_if_open("render", render_id, relaunches=relaunches)
        return entry
    except Exception as exc:  # diagnostics: never what decides the render
        logger.warning("Video render %s: its relaunch was not recorded: %s", render_id, format_exception_message(exc))
        return None


async def record_resumed_from(render_id: str, frame: int) -> bool:
    """Say on the render's newest relaunch the frame its drawing resumed from: the frames kept from the
    attempts before, on this worker's disk or from the store. Once per relaunch; whether it was written."""
    try:
        row = await jobs.load("render", render_id)
        relaunches = [dict(entry) for entry in getattr(row, "relaunches", None) or [] if isinstance(entry, dict)]
        if row is None or row.status in JOB_TERMINAL_STATUSES or not relaunches:
            return False
        if relaunches[-1].get("frames_resumed_from") is not None:
            return False
        relaunches[-1]["frames_resumed_from"] = max(0, int(frame))
        if (took := _seconds_since(relaunches[-1].get("at"))) is not None:
            # How long the relaunch took to draw again: what a rollout faster than this costs the render.
            relaunches[-1]["drawing_after_seconds"] = took
            if took > RESUME_TO_DRAWING_BOUND_SECONDS:
                logger.warning(
                    "Video render %s: drawing again %d s after its relaunch, past the %d s bound",
                    render_id,
                    took,
                    RESUME_TO_DRAWING_BOUND_SECONDS,
                    extra={"render_id": render_id, "drawing_after_seconds": took},
                )
        return await jobs.mark_if_open("render", render_id, relaunches=relaunches)
    except Exception as exc:  # diagnostics: never what decides the render
        logger.warning("Video render %s: the frame it resumed from was not recorded: %s", render_id, format_exception_message(exc))
        return False


def _seconds_since(at: object) -> int | None:
    """Whole seconds from the ISO time *at* to now, never below 0; ``None`` for no time or one unreadable."""
    if not isinstance(at, str):
        return None
    try:
        since = datetime.fromisoformat(at)
    except ValueError:
        return None
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    return max(0, int((datetime.now(UTC) - since).total_seconds()))


#: The progress past which a render has drawn and encoded every frame: its checks, the inserts' read-back at 94 %.
READBACK_PROGRESS = 89


def relaunch_line(relaunches: list | None) -> str:
    """What ``get_video_jobs`` says of a render's worker changes: each with its attempt, what it resumed after,
    where it stood and the frame it resumed from; ``""`` for none."""
    entries = [entry for entry in relaunches or [] if isinstance(entry, dict)]
    if not entries:
        return ""

    def said(index: int, entry: dict) -> str:
        attempt = entry.get("attempt")
        of = f" of {entry['attempts']}" if entry.get("attempts") else ""
        text = f"attempt {attempt}{of}" if attempt else ""
        before = entries[index - 1].get("attempt") if index > 0 else None
        if isinstance(attempt, int) and isinstance(before, int) and attempt < before:
            # Never a count gone wrong: a lost lease while it still moved gives its attempts back.
            text += " (its attempts given back: its lease lapsed while it still moved)"
        text = (text + " " if text else "") + f"at {entry.get('progress') or 0}%"
        if entry.get("after"):
            text += f" {entry['after']}"
        kept = entry.get("frames_kept")
        if isinstance(kept, int) and kept > 0:
            text += f", {kept:,} frame{'' if kept == 1 else 's'} kept on its worker"
        resumed_from = entry.get("frames_resumed_from")
        if resumed_from is not None:
            text += f", resumed from frame {int(resumed_from):,}"
            took = entry.get("drawing_after_seconds")
            if isinstance(took, int) and not isinstance(took, bool):
                text += f" {took:,} s after its relaunch"
                if took > RESUME_TO_DRAWING_BOUND_SECONDS:
                    text += f" (past its {RESUME_TO_DRAWING_BOUND_SECONDS // 60} min bound)"
        elif int(entry.get("progress") or 0) >= READBACK_PROGRESS:
            # Past the draw: its frames are kept whole, and the read-back goes on from the place it kept.
            text += ", its frames drawn, its checks going on from where they stood"
        else:
            # Handed on again before its drawing resumed: what a rollout faster than its resume costs it.
            text += ", handed on before its drawing resumed" if entry is not entries[-1] else ""
        return text

    count = len(entries)
    versions = {str(entry["version"]) for entry in entries if entry.get("version")}
    # More relaunches than releases taking them: a deploy handed it over more than once.
    over = f" (over {len(versions)} build{'s' if len(versions) > 1 else ''})" if versions and count > len(versions) else ""
    return (
        f"Relaunched on another worker {count} time{'s' if count > 1 else ''}{over}: "
        + "; ".join(said(i, entries[i]) for i in range(max(0, count - 3), count))
        + "."
    )


async def settle(
    render_id: str,
    ended: RenderExit,
    *,
    last_attempt: bool,
    ceiling: int | None,
    stall_seconds: float,
    resumes: int = 0,
    attempt: tuple[int, int] | None = None,
    again: bool = False,
    retried: dict | None = None,
    pages: int | None = None,
) -> JobOutcome:
    """The job outcome of an ended render process; the render row says why when the process could not.

    *attempt* is ``(this attempt, the attempts the execution allows)``, said when the last one crashed. A failure
    of the platform (``render_retry``: its Chromium on the last attempt, the memory ceiling, a crash on the
    last attempt, a stall past its resumes) runs the render again once with *again* - ``again``, its row pending -
    while its video is at the revision it drew; else it fails, its row saying the platform's cause and the run
    again it had (*retried*). The *pages* a whole render's process laid out go with a run again after the memory
    ceiling, which draws on fewer (``render_retry.lighter_pages``).
    """
    if ended.kind in _OWN_OUTCOMES:
        if ended.kind == "retry" and ended.cause and last_attempt:
            return await _platform_failed(render_id, ended, ended.cause, again=again, retried=retried, attempt=attempt)
        return JobOutcome(ended.kind, ended.error, cause=ended.cause)
    row = await jobs.load("render", render_id)
    if row is None or row.status in JOB_TERMINAL_STATUSES:
        # It finished (or was cancelled) before its process went down: nothing to correct.
        return JobOutcome("done")
    now = datetime.now(UTC)
    # Each write ends the render only while it is still in progress: one replaced meanwhile stays replaced.
    if ended.kind == "cancelled":
        await jobs.mark_if_open("render", render_id, status=JOB_CANCELLED, error="Cancelled.", finished_at=now, stage=None)
        return JobOutcome("done")
    message = ended.describe(ceiling=ceiling, stall_seconds=stall_seconds)
    cause = render_retry.cause_of_exit(ended.kind, oom_killed=ended.oom_killed)
    if cause is not None and again and (ended.kind != "crashed" or last_attempt):
        record = await render_retry.run_again(render_id, cause, message, pages=pages if cause == render_retry.MEMORY else None)
        if record is not None:
            return JobOutcome("again", message, cause=cause, again=record)
    if ended.kind == "crashed" and not last_attempt:
        logger.warning("Video render %s: %s It runs again, resuming where it stopped.", render_id, message)
        if not await jobs.mark_if_open(
            "render",
            render_id,
            status=JOB_PENDING,
            progress=0,
            stage=f"Starting again: {message}"[:200],
            progress_at=now,
        ):
            return JobOutcome("done")
        return JobOutcome("retry", message)
    if ended.kind == "crashed":
        message += f" This was its last attempt ({attempt[0]} of {attempt[1]})." if attempt else " This was its last attempt."
    if ended.kind == "stalled" and resumes:
        message += f" It had been resumed in place {resumes} time{'s' if resumes > 1 else ''} after earlier stalls."
    logger.warning("Video render %s failed: %s", render_id, message)
    failure = {"failure": render_retry.failure_record(cause, retried=retried)} if cause else {}
    if not await jobs.mark_if_open(
        "render", render_id, status=JOB_FAILED, error=message[:2000], finished_at=now, stage=None, **failure
    ):
        return JobOutcome("done")
    return JobOutcome("failed", message, cause=cause)


async def _platform_failed(
    render_id: str,
    ended: RenderExit,
    cause: str,
    *,
    again: bool,
    retried: dict | None,
    attempt: tuple[int, int] | None,
) -> JobOutcome:
    """A render whose own last attempt the platform failed (*cause*): run again once (*again*), else failed,
    its row saying the cause and where it was - never left to a dead letter that says neither."""
    message = f"{(ended.error or 'The render failed.').rstrip('.')} {ended.where()}."
    if again:
        record = await render_retry.run_again(render_id, cause, message)
        if record is not None:
            return JobOutcome("again", message, cause=cause, again=record)
    message += f" This was its last attempt ({attempt[0]} of {attempt[1]})." if attempt else " This was its last attempt."
    logger.warning("Video render %s failed: %s", render_id, message)
    if not await jobs.mark_if_open(
        "render",
        render_id,
        status=JOB_FAILED,
        error=message[:2000],
        finished_at=datetime.now(UTC),
        stage=None,
        failure=render_retry.failure_record(cause, retried=retried),
    ):
        return JobOutcome("done")
    return JobOutcome("failed", message, cause=cause)


# ── The render process ────────────────────────────────────────────────────


def status_channel() -> TextIO:
    """Keep standard output for the supervisor's events; anything else written there goes to standard error."""
    channel = os.dup(1)
    os.dup2(2, 1)
    return os.fdopen(channel, "w", buffering=1, encoding="utf-8")


def die_with_the_worker() -> None:
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)
    except (OSError, AttributeError):
        return
    expected = os.environ.get(PARENT_ENV)
    if expected and expected.isdigit() and os.getppid() != int(expected):
        # The worker went before the request took effect.
        os._exit(1)


def stopped_before_the_worker() -> None:
    """Raise this process's out-of-memory score to :data:`OOM_SCORE_ENV`, before it starts anything.

    Chromium and ffmpeg inherit it: should the container's memory run out, the
    kernel stops them before the worker. Raising one's own score needs no
    privilege; where it cannot be written, nothing changes.
    """
    value = os.environ.get(OOM_SCORE_ENV, "")
    if not sys.platform.startswith("linux") or not value.lstrip("-").isdigit():
        return
    with contextlib.suppress(OSError):
        _OOM_SCORE_ADJ_PATH.write_text(value)


async def _run(render_id: str, channel: TextIO, workdir: Path | None, scene_range: tuple[int, str] | None = None) -> None:
    import app.models  # noqa: F401
    from app.core.executor import configure_default_executor
    from app.llm.identity import load_service_identity
    from app.services.licence_verdict import accept_licensed_verdicts
    from app.services.security_options import refresh_security_options_cache
    from app.services.video_studio.lease import FrameLease
    from app.services.video_studio.render import run_render

    configure_default_executor("xtmone-render")
    accept_licensed_verdicts()
    try:
        await refresh_security_options_cache()
    except Exception as exc:
        logger.warning(
            "Render %s: the security options could not be read: %s", render_id, format_exception_message(exc)
        )
    # What a gateway call with nobody bound signs as (``app.llm.identity``): only the platform's bootstrap sets it.
    await load_service_identity()
    cancel = asyncio.Event()
    with contextlib.suppress(NotImplementedError, AttributeError):
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, cancel.set)
    with contextlib.suppress(NotImplementedError, AttributeError):
        # Handed over by its worker: each page closes its segment at the frame reached (``supervise``).
        from app.services.video_studio.compositor import hand_over_now

        asyncio.get_running_loop().add_signal_handler(signal.SIGUSR1, hand_over_now)

    lease = FrameLease.from_environment()
    if lease is not None:
        # Renewed from a thread of its own while the render moves: never from a frame, never the worker.
        lease.start()

    def report(event: dict) -> None:
        if lease is not None:
            lease.moved()
        channel.write(json.dumps(event, default=str) + "\n")

    try:
        if scene_range is not None and workdir is not None:
            # One scene range of the render, drawn from what the render published.
            outcome = await render_ranges.run_range(
                render_id,
                scene_range[0],
                scene_range[1],
                cancel_event=cancel,
                report=report,
                workdir=workdir,
                on_frame=lease.frame_drawn if lease is not None else None,
            )
        else:
            outcome = await run_render(
                render_id,
                cancel_event=cancel,
                report=report,
                workdir=workdir,
                on_frame=lease.frame_drawn if lease is not None else None,
            )
    except Exception as exc:
        logger.warning("Video render %s failed: %s", render_id, format_exception_message(exc), exc_info=True)
        outcome = JobOutcome(
            "retry",
            f"The render failed: {format_exception_message(exc)}"[:2000],
            cause=render_retry.cause_of_exception(exc),
        )
    finally:
        if lease is not None:
            await lease.close()
    if lease is not None:
        logger.info(
            "Video render %s: its lease renewed %d times from %d frames (%d failures)",
            render_id,
            lease.renewals,
            lease.frames,
            lease.failures,
        )
    report({"type": "outcome", "kind": outcome.kind, "error": outcome.error, "cause": outcome.cause})
    from app.core.database import engine

    await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    scene_range = None
    if len(args) == 5 and args[2] == _RANGE_FLAG and args[3].isdigit():
        scene_range, args = (int(args[3]), args[4]), args[:2]
    if len(args) not in (1, 2):
        sys.stderr.write(
            "usage: python -m app.services.video_studio.render_process RENDER_ID [WORKDIR [--range PART DIGEST]]\n"
        )
        return 2
    channel = status_channel()
    die_with_the_worker()
    stopped_before_the_worker()
    from app.core.logging import setup_logging

    setup_logging(debug=settings.debug, log_level=settings.log_level, log_format=settings.log_format)
    asyncio.run(_run(args[0], channel, Path(args[1]) if len(args) == 2 else None, scene_range))
    channel.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
