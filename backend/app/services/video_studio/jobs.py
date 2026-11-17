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

"""Video Studio jobs: recordings, renders, clips and AI tracks as durable orchestration executions.

A job row (``VideoTake`` / ``VideoRender`` / ``VideoClip`` / ``VideoMusic``) is created with an
``orchestration_executions`` row in the same transaction (``enqueue``); the
worker's reconciler claims it and the adapters of
``services/orchestration_adapters/video_studio.py`` run it under a lease, with
retries for what is transient, a deadline, and an operator cancel that stops a
render between frames. A chat turn never holds the work: a tool enqueues, then
waits (:func:`wait_for`) at most until its own deadline and says how to check
on what is still running.

What a caller is told of a job is read from the job's row and nowhere
else: :func:`job_state` turns it into one of four states - ``queued``,
``running``, ``ready``, ``failed`` - with its percent, its stage, the worker's
last progress write (``progress_at``, stamped by :func:`mark` on every
progress write) and, on failure, the reason. A job the worker has written a
heartbeat for is never ``queued``.

A job a chat turn starts is owed to its conversation
(``owed_conversation_id``): a wait that tells the agent how it ended clears it
(:func:`mark_told`), and the worker resumes a conversation still owed a job
that ended (``services/video_studio/wake.py``).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import async_session
from app.llm.video import STATE_RATE_LIMITED, STATE_UNAVAILABLE
from app.models.orchestration import OrchestrationExecution
from app.models.video_studio import (
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_PENDING,
    JOB_READY,
    JOB_RUNNING,
    JOB_TERMINAL_STATUSES,
    VideoClip,
    VideoMusic,
    VideoRender,
    VideoTake,
)
from app.services.orchestration_engine import OWNER_GONE_STATE, LeaseHeartbeat, lease_heartbeat
from app.services.video_studio import frame_qc, provider_waits, retention
from app.services.video_studio.clips import (
    end_attempt,
    failed_regeneration,
    fallback_state,
    keeps_take,
    regeneration_reason,
    regeneration_state,
    start_frame_file_id,
    take_params,
)
from app.services.video_studio.provider_cooling import HELD_BY_PROVIDER, held_line

TAKE_ADAPTER = "video_take"
RENDER_ADAPTER = "video_render"
#: The preview lane: a scene preview runs on slots of its own, beside the whole renders, never on theirs.
PREVIEW_ADAPTER = "video_preview"
#: The adapters a render runs on (a scene preview queued before the preview lane existed runs on the render lane).
RENDER_ADAPTERS = (RENDER_ADAPTER, PREVIEW_ADAPTER)
#: What ``queue.lane`` calls each lane of the render queue: the recordings are its third (``recording_lane``).
LANES = {RENDER_ADAPTER: "render", PREVIEW_ADAPTER: "preview", TAKE_ADAPTER: "recording"}
#: The lease a render's execution is held under: three times the engine's, renewed every third of it.
#: A render works for minutes at a stretch, and a lease that lapses throws away the frames of a whole attempt.
RENDER_LEASE_SECONDS = 180
CLIP_ADAPTER = "video_clip"
MUSIC_ADAPTER = "video_music"
_MODELS = {"take": VideoTake, "render": VideoRender, "clip": VideoClip, "music": VideoMusic}
_ADAPTERS = {"take": TAKE_ADAPTER, "render": RENDER_ADAPTER, "clip": CLIP_ADAPTER, "music": MUSIC_ADAPTER}
#: The setting that says how many jobs of an adapter one worker runs at a time.
_CONCURRENCY = {
    TAKE_ADAPTER: "video_studio_take_concurrency",
    RENDER_ADAPTER: "video_studio_render_concurrency",
    PREVIEW_ADAPTER: "video_studio_preview_concurrency",
    CLIP_ADAPTER: "video_studio_clip_concurrency",
    MUSIC_ADAPTER: "video_studio_clip_concurrency",
}
#: What a caller calls each kind (the ``get_video_jobs`` arguments), and the noun of one and of several.
KIND_LABELS = {"take": "recording", "render": "render", "clip": "clip", "music": "music"}
#: The ``get_video_jobs`` argument that names the jobs of each kind.
JOB_ID_ARGUMENTS = {"take": "recording_ids", "render": "render_ids", "clip": "clip_ids", "music": "music_ids"}
_NOUNS = {
    "take": ("recording", "recordings"),
    "render": ("render", "renders"),
    "clip": ("AI shot", "AI shots"),
    "music": ("AI track", "AI tracks"),
}
#: What the preview lane's renders are called.
_PREVIEW_NOUNS = ("scene preview", "scene previews")

#: The four states a caller acts on.
STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_READY = "ready"
STATE_FAILED = "failed"


def adapter_of(kind: str, row: Any) -> str:
    """The adapter that runs the job *row* of *kind*: a scene preview's is the preview lane's."""
    if kind == "render" and getattr(row, "scene_id", None):
        return PREVIEW_ADAPTER
    return _ADAPTERS[kind]


def slots(adapter: str) -> int:
    """How many jobs of *adapter* one worker runs at a time: one recording on a recording lane
    (``capture_rate.take_concurrency``)."""
    if adapter == TAKE_ADAPTER:
        from app.services.video_studio.capture_rate import take_concurrency

        return max(1, take_concurrency())
    return max(1, int(getattr(settings, _CONCURRENCY[adapter])))


def take_deadline(start: datetime | None = None) -> datetime:
    """A take's deadline from *start* (now by default): its longest recording, then 15 min for its passes and encode."""
    return _deadline("take", start=start)


def _deadline(kind: str, *, start: datetime | None = None, quality: str | None = None) -> datetime:
    if kind == "take":
        seconds = settings.video_studio_max_take_seconds + 900
    elif kind == "render":
        from app.services.video_studio.render_preflight import wall_seconds

        # An ultra render may run 8 h in segments a new attempt resumes from, any other 4 h.
        seconds = wall_seconds(quality)
    else:
        seconds = 1800
    now = datetime.now(UTC)
    return max(now, start or now) + timedelta(seconds=seconds)


async def enqueue(
    db: AsyncSession,
    kind: str,
    row: VideoTake | VideoRender | VideoClip | VideoMusic,
    *,
    generation: int = 0,
    owed_to: uuid.UUID | None = None,
    not_before: datetime | None = None,
    payload: dict | None = None,
) -> None:
    """Create the execution of a flushed job *row*; the caller commits, then calls :func:`wake`.

    *payload* adds to its execution's payload what its claim reads (a take's ``worker_class``).

    *generation* > 0 runs a row again (an AI shot generated again in place):
    the execution of each generation is its own, the earlier one done.
    *owed_to* is the conversation of the chat turn that starts it, told how it
    ends; a job started anywhere else is owed to nobody, an earlier
    generation's conversation included. A scene preview goes to the
    preview lane (:func:`adapter_of`): its own slots, so it never
    waits for a whole render, nor a whole render for it - and its slots claim
    one preview per video in turn (``preview_lane.deal_claims``). It has the deadline
    of its kind: previews queue up to the renders' own limit,
    and one waiting behind the others must not time out in the queue.
    *not_before* is when a job may start at the earliest (an AI shot queued
    on a provider that cools): its deadline counts from then.
    """
    from app.services.orchestration_engine import create_execution

    row.owed_conversation_id = owed_to
    suffix = f":{generation}" if generation else ""
    adapter = adapter_of(kind, row)
    execution = await create_execution(
        db,
        adapter=adapter,
        payload={**(payload or {}), f"{kind}_id": str(row.id)},
        run_at=not_before,
        deadline_at=_deadline(kind, start=not_before, quality=getattr(row, "quality", None)),
        dedup_key=f"{adapter}:{row.id}{suffix}",
        # A recording runs again once at most. A render runs three times
        # at most: a crash or a lease the engine took back runs it again,
        # resuming where it stopped (``render_process.run_isolated``).
        max_attempts=2 if kind == "take" else 3,
    )
    row.execution_id = execution.id


async def due_now(db: AsyncSession, row: Any) -> None:
    """Make the queued execution of *row* due at once: a job held behind a grace it no longer needs (a preview whose
    serve from a kept one did not finish). The caller commits, then calls :func:`wake`."""
    if getattr(row, "execution_id", None) is None:
        return
    await db.execute(
        update(OrchestrationExecution)
        .where(
            OrchestrationExecution.id == row.execution_id,
            OrchestrationExecution.state == "pending",
            OrchestrationExecution.lease_owner.is_(None),
        )
        .values(run_at=datetime.now(UTC))
    )


async def wake() -> None:
    from app.services.orchestration_engine import notify_orchestration_wake

    await notify_orchestration_wake()


async def load(kind: str, job_id: object) -> Any:
    async with async_session() as db:
        return await db.get(_MODELS[kind], uuid.UUID(str(job_id)))


#: Why :func:`wait_for_jobs` returned.
WAIT_ALL_DONE = "every job is finished"
WAIT_ONE_DONE = "a job finished"
WAIT_BUDGET = "the wait budget ran out"
#: How often a wait reads the rows again.
_WAIT_POLL_SECONDS = 2.0


async def _read(kind: str, ids: list[uuid.UUID]) -> list[Any]:
    """The rows of *ids*, in that order, read on a session of their own: what the worker last committed."""
    model = _MODELS[kind]
    async with async_session() as db:
        rows = (await db.execute(select(model).where(model.id.in_(ids)))).scalars().all()
    order = {jid: i for i, jid in enumerate(ids)}
    return sorted(rows, key=lambda r: order.get(r.id, 0))


async def wait_for_jobs(
    groups: Mapping[str, list[str]], *, timeout: float, told_to: uuid.UUID | None = None
) -> tuple[dict[str, list[Any]], str]:
    """``(rows by kind, why it returned)`` for jobs of several kinds, in one wait of at most *timeout* seconds.

    Returns as soon as a job that was still running at the first read has
    finished (:data:`WAIT_ONE_DONE`) - the caller has something to act on -,
    at once when every job already is (:data:`WAIT_ALL_DONE`), else with the
    rows as they are when the time is up (:data:`WAIT_BUDGET`). Every read is
    a fresh session: nothing the worker committed is missed.

    *told_to* is the conversation the caller reports the rows to: the jobs
    that ended are no longer owed to it (:func:`mark_told`).
    """
    deadline = time.monotonic() + max(0.0, timeout)
    ids = {kind: [uuid.UUID(str(j)) for j in job_ids] for kind, job_ids in groups.items() if job_ids}
    open_at_start: set[uuid.UUID] | None = None
    while True:
        rows = {kind: await _read(kind, kind_ids) for kind, kind_ids in ids.items()}
        everything = [row for kind_rows in rows.values() for row in kind_rows]
        running = {row.id for row in everything if row.status not in JOB_TERMINAL_STATUSES}
        why = None
        if not running:
            why = WAIT_ALL_DONE
        elif open_at_start is not None and open_at_start - running:
            why = WAIT_ONE_DONE
        elif time.monotonic() >= deadline:
            why = WAIT_BUDGET
        if open_at_start is None:
            open_at_start = running
        if why is not None:
            for kind, kind_rows in rows.items():
                await mark_told(kind, kind_rows, told_to)
            return rows, why
        await asyncio.sleep(min(_WAIT_POLL_SECONDS, max(0.01, deadline - time.monotonic())))


async def wait_for(kind: str, job_ids: list[str], *, timeout: float, told_to: uuid.UUID | None = None) -> list[Any]:
    """The rows of one kind, once one of them finished (all of them, for one job), or as they are at *timeout*."""
    rows, _why = await wait_for_jobs({kind: job_ids}, timeout=timeout, told_to=told_to)
    return rows.get(kind, [])


async def mark_told(kind: str, rows: list[Any], conversation_id: uuid.UUID | None) -> None:
    """The jobs of *rows* that ended are no longer owed to *conversation_id*: the caller tells it how.

    Only a job owed to that very conversation: a job another conversation
    started, read here, is still owed to its own.
    """
    if conversation_id is None:
        return
    ended = [
        row.id
        for row in rows
        if row.status in JOB_TERMINAL_STATUSES and getattr(row, "owed_conversation_id", None) == conversation_id
    ]
    if not ended:
        return
    model = _MODELS[kind]
    async with async_session() as db:
        await db.execute(
            update(model)
            .where(model.id.in_(ended), model.owed_conversation_id == conversation_id)
            .values(owed_conversation_id=None)
        )
        await db.commit()
    for row in rows:
        if row.id in ended:
            row.owed_conversation_id = None


async def owed_jobs_in_flight(conversation_id: uuid.UUID) -> bool:
    """Whether a job owed to *conversation_id* is still queued or running."""
    async with async_session() as db:
        for model in _MODELS.values():
            found = await db.execute(
                select(model.id)
                .where(model.owed_conversation_id == conversation_id, model.status.in_((JOB_PENDING, JOB_RUNNING)))
                .limit(1)
            )
            if found.first() is not None:
                return True
    return False


async def owed_jobs_pending(conversation_id: uuid.UUID) -> list[tuple[str, Any, str | None]]:
    """The jobs owed to *conversation_id* still queued or running, oldest first: ``(kind, row, name)``.

    What the conversation waits on: the worker resumes it once they end. *name*
    is what the person calls the job - the title of a render's video, the name
    of a recording, an AI shot or a track - or ``None``.
    """
    from app.models.video_studio import VideoProject

    found: list[tuple[str, Any, str | None]] = []
    async with async_session() as db:
        for kind, model in _MODELS.items():
            pending = (model.owed_conversation_id == conversation_id, model.status.in_((JOB_PENDING, JOB_RUNNING)))
            if kind == "render":
                rows = await db.execute(
                    select(model, VideoProject.title)
                    .outerjoin(VideoProject, VideoProject.id == model.project_id)
                    .where(*pending)
                )
                found.extend((kind, row, title) for row, title in rows.all())
            else:
                rows = (await db.execute(select(model).where(*pending))).scalars().all()
                found.extend((kind, row, getattr(row, "name", None)) for row in rows)
    return sorted(found, key=lambda item: item[1].created_at)


async def conversations_owed_ended_jobs(
    *, ended_before: datetime, ended_after: datetime, limit: int
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """``(conversation, user)`` still owed a job that ended ready or failed between the two moments.

    The conversations whose wake was lost: it runs in the worker that ran the
    job, after a grace, and a worker stopped meanwhile takes it along.
    """
    found: dict[uuid.UUID, uuid.UUID] = {}
    async with async_session() as db:
        for model in _MODELS.values():
            rows = await db.execute(
                select(model.owed_conversation_id, model.user_id)
                .where(
                    model.owed_conversation_id.is_not(None),
                    model.user_id.is_not(None),
                    model.status.in_((JOB_READY, JOB_FAILED)),
                    model.finished_at <= ended_before,
                    model.finished_at > ended_after,
                )
                .limit(limit)
            )
            for conversation_id, user_id in rows.all():
                found.setdefault(conversation_id, user_id)
    return list(found.items())[:limit]


async def claim_owed_jobs(conversation_id: uuid.UUID) -> dict[str, list[tuple[str, datetime]]]:
    """Take the jobs owed to *conversation_id* that ended ready or failed: ``{kind: [(id, created_at)]}``.

    One conditional update per kind, so two wakes racing never both claim a
    job. A cancelled job is nobody's news: :func:`cancel` already cleared it.
    """
    claimed: dict[str, list[tuple[str, datetime]]] = {}
    async with async_session() as db:
        for kind, model in _MODELS.items():
            result = await db.execute(
                update(model)
                .where(model.owed_conversation_id == conversation_id, model.status.in_((JOB_READY, JOB_FAILED)))
                .values(owed_conversation_id=None)
                .returning(model.id, model.created_at)
            )
            taken = [(str(job_id), created_at) for job_id, created_at in result.all()]
            if taken:
                claimed[kind] = taken
        await db.commit()
    return claimed


def max_renders_per_user() -> int:
    """Renders one person may have queued or running at once (``VIDEO_STUDIO_MAX_RENDERS_PER_USER``)."""
    return max(1, int(settings.video_studio_max_renders_per_user))


def max_scene_previews_per_video() -> int:
    """Scene previews of one video run at once: the renders' own limit, a bound on concurrency only.

    Every preview asked is queued (``projects.start_scene_render``): one in
    flight per scene, which a sweep of every scene asks in one batch. Past this
    many running for the video, the next one waits its turn in the preview
    lane - never claimed for while the video runs its cap, one preview per
    video in turn (``preview_lane.deal_claims``; :func:`preview_waits`
    when two workers claim at once) -, never refused.
    """
    return max_renders_per_user()


#: How long a scene preview waits before it is claimed again while its video runs its cap of previews.
PREVIEW_WAIT_SECONDS = 15.0
#: The ``adapter_state`` of a scene preview waiting for one of its video's previews to end.
PREVIEW_WAITING_STATE = "preview_waiting"
#: The ``adapter_state`` of a render its stopping worker handed back to the queue.
HANDED_OVER_STATE = "worker_stopped"
#: Where the execution of a render handed back records the worker that stopped and how long the render
#: waits for the next one (:func:`waits_for`).
HANDED_OVER_KEY = "handed_over"
#: The renders a worker that stopped had in flight, waiting for the next one: first in the queue
#: (handed back, or taken back from a worker gone), counted with the renders being made - their place is the one
#: they had - never ahead of the jobs queued behind them.
RESUMING_STATES = (HANDED_OVER_STATE, OWNER_GONE_STATE)
#: The ``adapter_state`` of a scene preview that gave the render slot it was lent back to a render a stopped worker
#: left: first in its own lane, its finished segments kept.
SLOT_GIVEN_BACK_STATE = "slot_given_back"
#: The ``adapter_state`` of a whole render that gave its own slot back to one of those: queued again in its
#: place.
SLOT_GIVEN_STATE = "own_slot_given_back"
#: The least a resumed render waits for a worker before it is failed, whatever its deployment says.
MIN_RESUME_WAIT = timedelta(minutes=3)
#: What a worker's deployment gives a rolling restart when it says nothing (:func:`resume_wait`): Kubernetes'
#: default termination grace, then what a new worker pod may take to start - 10 min together.
DEFAULT_TERMINATION_GRACE_SECONDS = 30
DEFAULT_WORKER_STARTUP_SECONDS = 570
#: Where the execution of a resumed render records when its lane was last seen full (written by
#: :func:`fail_unresumed_renders`): its wait counts from then once the lane frees, never from its hand-over.
LANE_HELD_KEY = "lane_held_at"
#: Where the execution of a render no worker resumed records it: a relaunch of the same revision and quality
#: reads it to bring that render back as the same job.
UNRESUMED_KEY = "unresumed"
#: How long a render no worker resumed may be brought back by a relaunch, its kept segments with it.
RELAUNCH_KEEP = timedelta(days=1)


def resume_wait() -> timedelta:
    """How long a render a stopped worker left waits for the next worker before it fails.

    As long as this worker's deployment lets a rolling restart take: its pod's
    termination grace, then a new pod's start (``WORKER_TERMINATION_GRACE_SECONDS``
    and ``WORKER_STARTUP_SECONDS``, each at its default when not given - 10 min
    for both), never less than :data:`MIN_RESUME_WAIT`.
    """
    grace = settings.worker_termination_grace_seconds or DEFAULT_TERMINATION_GRACE_SECONDS
    startup = settings.worker_startup_seconds or DEFAULT_WORKER_STARTUP_SECONDS
    return max(MIN_RESUME_WAIT, timedelta(seconds=grace + startup))


def waits_for(execution: Any) -> timedelta:
    """How long the resumed render of *execution* waits for a worker: what the worker that handed it back
    stamped - :func:`resume_wait` read from that worker's own deployment -, else what this process reads."""
    handed = (getattr(execution, "payload", None) or {}).get(HANDED_OVER_KEY)
    stamped = handed.get("resume_wait_seconds") if isinstance(handed, dict) else None
    if isinstance(stamped, int | float) and not isinstance(stamped, bool) and stamped > 0:
        return max(MIN_RESUME_WAIT, timedelta(seconds=float(stamped)))
    return resume_wait()


def span(wait: timedelta) -> str:
    """``10 min``, ``9 min 30 s`` or ``45 s``: a wait as it is said."""
    minutes, seconds = divmod(max(0, int(wait.total_seconds())), 60)
    if not minutes:
        return f"{seconds} s"
    return f"{minutes} min" + (f" {seconds} s" if seconds else "")


async def preview_waits(render_id: object) -> str | None:
    """Why the scene preview *render_id* must wait before it starts, or ``None`` when it may start.

    It waits while :func:`max_scene_previews_per_video` other previews of its
    video are running: read from the executions the worker runs, never from
    the rows' status (a claimed preview is running before its process writes
    it). A whole render, or a preview whose row is gone, never waits here.
    """
    from app.core.utils import safe_uuid

    if (uid := safe_uuid(str(render_id))) is None:
        return None
    async with async_session() as db:
        row = await db.get(VideoRender, uid)
        if row is None or not row.scene_id:
            return None
        running = (
            await db.execute(
                select(func.count())
                .select_from(OrchestrationExecution)
                .join(VideoRender, VideoRender.execution_id == OrchestrationExecution.id)
                .where(
                    OrchestrationExecution.adapter.in_(RENDER_ADAPTERS),
                    OrchestrationExecution.state == "running",
                    VideoRender.project_id == row.project_id,
                    VideoRender.scene_id.is_not(None),
                    VideoRender.id != row.id,
                )
            )
        ).scalar_one()
    cap = max_scene_previews_per_video()
    if int(running) < cap:
        return None
    return f"Waiting for a preview of this video to end: {int(running)} are being made (at most {cap} at once)."


async def fail_unresumed_renders(*, now: datetime | None = None, limit: int = 50) -> int:
    """Fail the renders a stopped worker left that no worker resumed within their wait; how many.

    A render handed back by its stopping worker, or taken back from a worker
    gone, is first in its lane's queue: the lane's next free slot takes it. It
    waits as long as its deployment lets a rolling restart take
    (:func:`waits_for`) without moving, and for as long as a job holds a slot
    of its lane anywhere (:func:`holds_slot_of`: a render, or a scene preview
    in a render slot lent to it - one on the preview lane's own slots never
    holds one): the slot that frees takes it. Each check that finds its
    lane held records it (:data:`LANE_HELD_KEY`), and its wait counts from the
    last of those or its last move, so the wait it states once its lane frees
    is the wait it gets. Past it, no worker will: it is failed, saying where it
    stood, why it went back and the workers alive then with why none took it
    (:func:`unresumed_reason`), instead of reading ``running`` for ever. Its
    execution is cancelled with it and marked (:data:`UNRESUMED_KEY`), so a
    relaunch of the same revision and quality brings it back as the same job
    (``projects.start_render``). Run by the platform's scheduler tick, which
    runs whether or not a worker does.
    """
    from app.services.orchestration_engine import cancel_execution

    now = now or datetime.now(UTC)
    of_renders = OrchestrationExecution.adapter.in_(RENDER_ADAPTERS)
    async with async_session() as db:
        running = await _running_renders(db)
        rows = (
            await db.execute(
                select(VideoRender, OrchestrationExecution)
                .join(OrchestrationExecution, OrchestrationExecution.id == VideoRender.execution_id)
                .where(
                    of_renders,
                    OrchestrationExecution.state == "pending",
                    OrchestrationExecution.adapter_state.in_(RESUMING_STATES),
                    VideoRender.status.in_((JOB_PENDING, JOB_RUNNING)),
                )
                .limit(limit)
            )
        ).all()
        due: list[tuple[VideoRender, OrchestrationExecution, Resume]] = []
        for render, execution in rows:
            held, _previews = lane_counts(execution.adapter, running)
            if held:
                await _mark_lane_held(db, execution, now)
                continue
            resume = resume_of(execution, render)
            if resume is not None and resume.grace_seconds(now) == 0:
                due.append((render, execution, resume))
        if not due:
            await db.commit()
            return 0
        workers = await workers_seen(running)
        failed = 0
        for render, execution, resume in due:
            reason = unresumed_reason(render, resume, workers)
            ended = await db.execute(
                update(VideoRender)
                .where(VideoRender.id == render.id, VideoRender.status.in_((JOB_PENDING, JOB_RUNNING)))
                .values(status=JOB_FAILED, error=reason[:2000], finished_at=now, stage=None)
            )
            if ended.rowcount:
                cancelled = await cancel_execution(db, execution_id=execution.id)
                if cancelled is not None:
                    left = {"at": now.isoformat(), "progress": render.progress or 0, "workers": workers}
                    cancelled.payload = {**(cancelled.payload or {}), UNRESUMED_KEY: left}
                failed += 1
        await db.commit()
    return failed


#: How long a render row reads queued or running, with no progress, while nothing runs it, before it is failed
#: (:func:`fail_orphaned_renders`).
ORPHAN_STALL = timedelta(minutes=15)
#: How long a render's execution may read ``running`` with no lease before it is taken back
#: (:func:`reclaim_leaseless_renders`): one render lease period, the longest its holder goes without a write.
LEASELESS_STALL = timedelta(seconds=RENDER_LEASE_SECONDS)


def no_lease_since(execution: Any) -> datetime | None:
    """Since when a render's *execution* reads ``running`` with no lease at all (its last write), else ``None``.

    A render execution never pauses lease-less (no ``await_event``): such a row has no holder.
    """
    if execution is None or execution.state != "running" or execution.lease_expires_at is not None:
        return None
    if getattr(execution, "adapter", None) not in RENDER_ADAPTERS:
        return None
    return _aware(execution.updated_at) or datetime.now(UTC)


async def reclaim_leaseless_renders(*, now: datetime | None = None, limit: int = 50) -> int:
    """Take back the render executions left ``running`` with no lease for :data:`LEASELESS_STALL`; how many.

    A relaunch that lost its lease record leaves the execution ``running`` with no holder: no lease expires, no
    worker is named gone, so nothing relaunched or failed it (6 Nov 2026: 94 %, attempt 3 of 3, its frozen-picture
    watch frozen). Each goes back first in its lane with attempts left, its kept frames and check places with it,
    or is dead-lettered with none: the render adapter's dead-letter runs it again once on the platform's account,
    else fails it with the harness reason (``orchestration_engine.NO_LEASE_WHY``). Run by the scheduler tick.
    """
    from app.services import orchestration_engine as engine
    from app.services.orchestration_adapters import register_builtin_adapters
    from app.services.orchestration_reconciler import attempt_bound_of, run_dead_letter

    register_builtin_adapters()
    now = now or datetime.now(UTC)
    async with async_session() as db:
        taken = await engine.reclaim_leaseless(
            db,
            adapters=RENDER_ADAPTERS,
            quiet_before=now - LEASELESS_STALL,
            limit=limit,
            bound_of=attempt_bound_of(),
        )
        await db.commit()
    for execution in taken:
        if execution.state == "dead_letter":
            await run_dead_letter(execution, worker_id="web-backstop")
    if taken:
        await wake()
    return len(taken)


def orphan_reason(render: Any, execution: Any, stalled: timedelta) -> str:
    """Why a render nothing runs any more is failed: where it stood, what became of its execution, how long it sat."""
    if execution is None:
        what = "its job is gone"
    elif execution.state == "running":
        what = "no worker holds its lease any more"
    else:
        what = f"its job ended ({execution.state})"
    stage = f" ({render.stage})" if getattr(render, "stage", None) else ""
    return (
        f"Stalled: no progress for {span(stalled)} at {render.progress or 0}%{stage} and {what}. "
        "Render this revision again: nothing is in flight for it."
    )


async def fail_orphaned_renders(*, now: datetime | None = None, limit: int = 50) -> int:
    """Fail the renders still read queued or running that nothing runs any more; how many.

    A render row says ``running`` until its worker writes how it ended. When
    its execution is gone, has ended, or is still ``running`` with a lease no
    worker renewed, and the row has not moved for :data:`ORPHAN_STALL`, no
    worker will write that end: it is failed with the reason
    (:func:`orphan_reason`), so ``render_video`` on its revision starts a
    fresh render instead of answering this one as in flight. A render handed
    back to the queue is :func:`fail_unresumed_renders`' and never read here.
    Run by the platform's scheduler tick.
    """
    from app.services.orchestration_engine import cancel_execution

    now = now or datetime.now(UTC)
    quiet_since = now - ORPHAN_STALL
    async with async_session() as db:
        rows = (
            await db.execute(
                select(VideoRender, OrchestrationExecution)
                .outerjoin(OrchestrationExecution, OrchestrationExecution.id == VideoRender.execution_id)
                .where(
                    VideoRender.status.in_((JOB_PENDING, JOB_RUNNING)),
                    VideoRender.execution_id.is_not(None),
                    func.coalesce(VideoRender.progress_at, VideoRender.updated_at) < quiet_since,
                    or_(
                        OrchestrationExecution.id.is_(None),
                        OrchestrationExecution.state.in_(("completed", "failed", "dead_letter", "cancelled")),
                        and_(
                            OrchestrationExecution.state == "running",
                            or_(
                                OrchestrationExecution.lease_expires_at.is_(None),
                                OrchestrationExecution.lease_expires_at < quiet_since,
                            ),
                        ),
                    ),
                )
                .limit(limit)
            )
        ).all()
        failed = 0
        for render, execution in rows:
            moved = _aware(render.progress_at) or _aware(render.updated_at) or quiet_since
            reason = orphan_reason(render, execution, now - moved)
            ended = await db.execute(
                update(VideoRender)
                .where(VideoRender.id == render.id, VideoRender.status.in_((JOB_PENDING, JOB_RUNNING)))
                .values(status=JOB_FAILED, error=reason[:2000], finished_at=now, stage=None)
            )
            if ended.rowcount:
                if execution is not None and execution.state == "running":
                    await cancel_execution(db, execution_id=execution.id)
                failed += 1
        await db.commit()
    return failed


def holds_slot_of(lane: str, adapter: str, lent_slot: str | None = None) -> bool:
    """Whether a running execution of *adapter* - in a slot the *lent_slot* adapter lent it, if any - holds a slot
    of *lane*, the lane a resumed job of that adapter waits in.

    The render lane is held by its renders and by the scene previews running in a slot it lent
    (``orchestration_engine.LENT_SLOT_PAYLOAD_KEY``), never by a preview on the preview lane's own slots: those are
    counted apart (:func:`lane_counts`). The preview lane by every preview: a resumed one takes a free slot of
    either.
    """
    if lane == PREVIEW_ADAPTER:
        return adapter == PREVIEW_ADAPTER
    return (lent_slot or adapter) == lane


def lane_counts(lane: str, running: list[tuple]) -> tuple[int, int]:
    """``(the jobs holding a slot of lane *lane*, the scene previews running on a lane of their own)`` among
    *running* - ``(adapter, lease owner, lent slot)`` of the renders and previews being made (:func:`holds_slot_of`)."""
    held = sum(1 for adapter, _owner, slot, *_ in running if holds_slot_of(lane, adapter, slot))
    apart = sum(1 for adapter, _owner, slot, *_ in running if adapter == PREVIEW_ADAPTER and not holds_slot_of(lane, adapter, slot))
    return held, apart


async def _running_renders(db: AsyncSession) -> list[tuple[str, str | None, str | None]]:
    """``(adapter, lease owner, lent slot)`` of every render and scene preview running now."""
    from app.services.orchestration_engine import LENT_SLOT_PAYLOAD_KEY

    rows = await db.execute(
        select(
            OrchestrationExecution.adapter,
            OrchestrationExecution.lease_owner,
            OrchestrationExecution.payload[LENT_SLOT_PAYLOAD_KEY].astext,
        ).where(OrchestrationExecution.adapter.in_(RENDER_ADAPTERS), OrchestrationExecution.state == "running")
    )
    return [(str(adapter), owner, slot) for adapter, owner, slot in rows.all()]


async def _mark_lane_held(db: AsyncSession, execution: Any, now: datetime) -> None:
    """Record on the resumed render's *execution* that its lane was held at *now* (:data:`LANE_HELD_KEY`).

    Its ``updated_at`` - when it went back to the queue - is kept, and the write lands only on the execution as it
    was read: still waiting, untouched since.
    """
    await db.execute(
        update(OrchestrationExecution)
        .where(
            OrchestrationExecution.id == execution.id,
            OrchestrationExecution.state == "pending",
            OrchestrationExecution.updated_at == execution.updated_at,
        )
        .values(payload={**(execution.payload or {}), LANE_HELD_KEY: now.isoformat()}, updated_at=OrchestrationExecution.updated_at)
        .execution_options(synchronize_session=False)
    )


async def workers_seen(running: list[tuple]) -> list[str] | None:
    """Each worker alive now and why it does not take a render waiting for one; ``None`` when unknown.

    Read from what the workers say of themselves (``worker_presence.present``)
    and from *running* - ``(adapter, lease owner, ...)`` of the renders and
    previews being made: one asked to stop takes no new job, one drawing says
    what, and one running with nothing in flight should have taken it.
    """
    from app.services import worker_presence

    present = await worker_presence.present()
    if present is None:
        return None
    held: dict[str, list[str]] = {}
    for adapter, owner, *_ in running:
        if owner:
            held.setdefault(str(owner), []).append(adapter)
    said = []
    for record in present:
        name = worker_presence.name_of(record)
        drawing = held.get(str(record.get("worker_id") or ""), [])
        if record.get("state") == worker_presence.STATE_DRAINING:
            said.append(f"{name} was asked to stop ({record.get('signal') or 'a stop request'}) and took no new job")
        elif drawing:
            renders = sum(1 for adapter in drawing if adapter == RENDER_ADAPTER)
            previews = len(drawing) - renders
            parts = [_count(renders, "render") if renders else "", _preview_count(previews) if previews else ""]
            said.append(f"{name} was drawing {' and '.join(part for part in parts if part)}")
        elif not worker_presence.claims(record, RENDER_ADAPTER):
            said.append(f"{name} draws no whole render (VIDEO_STUDIO_RENDER_CONCURRENCY=0)")
        else:
            said.append(f"{name} was running with no render in flight, yet did not take it: see that worker's logs")
    return said


def _preview_count(number: int) -> str:
    return f"{number} {_PREVIEW_NOUNS[0] if number == 1 else _PREVIEW_NOUNS[1]}"


def unresumed_reason(render: Any, resume: Resume, workers: list[str] | None) -> str:
    """Why *render* is failed, no worker having resumed it: where it stood, why it went back, how long it
    waited, and the workers alive then (:func:`workers_seen`) with why none took it."""
    why = (resume.reason or "its worker stopped").rstrip(".")
    said = (
        f"No worker resumed it in {span(resume.wait)} after it went back to the queue at {render.progress or 0}% "
        f"({why[:1].lower()}{why[1:]}), while nothing held a slot of its lane."
    )
    if workers is None:
        said += " The workers alive then could not be read."
    elif not workers:
        said += " No worker was running then."
    else:
        said += f" Workers alive then: {'; '.join(workers)}."
    if getattr(render, "scene_id", None):
        return f"{said} Preview the scene again once a worker runs."
    return (
        f"{said} Render it again at the same quality once a worker runs: it resumes from the segments it kept, as this "
        "same render."
    )


def relaunchable() -> Any:
    """Whether a render - joined to its execution - is a whole render no worker resumed that a relaunch may still bring
    back: failed within :data:`RELAUNCH_KEEP`, its execution marked :data:`UNRESUMED_KEY`. Its kept segments
    and scene ranges stay for it (``render_segments.sweep``, ``render_ranges.sweep``)."""
    return and_(
        VideoRender.status == JOB_FAILED,
        VideoRender.scene_id.is_(None),
        VideoRender.finished_at > datetime.now(UTC) - RELAUNCH_KEEP,
        OrchestrationExecution.state == "cancelled",
        OrchestrationExecution.payload.has_key(UNRESUMED_KEY),
    )


async def enqueue_again(db: AsyncSession, row: VideoRender, *, owed_to: uuid.UUID | None = None) -> None:
    """Queue a whole render no worker resumed again, as the same job; the caller commits, then calls :func:`wake`.

    Its own execution is engaged afresh (``reset_execution``: its id and its
    dedup key kept, its attempts given back) first in its lane - before every render
    queued, the place it had - and the worker that takes it resumes from the
    segments kept beside it (``render_segments``). A render whose execution is
    gone gets a new one, queued like any other.
    """
    from app.services.orchestration_engine import head_of_queue, reset_execution

    row.owed_conversation_id = owed_to
    execution = None
    if row.execution_id is not None:
        execution = await reset_execution(
            db,
            execution_id=row.execution_id,
            payload={"render_id": str(row.id)},
            deadline_at=_deadline("render", quality=row.quality),
            run_at=await head_of_queue(db, adapter_of("render", row), excluding=row.execution_id),
        )
    if execution is None:
        await enqueue(db, "render", row, owed_to=owed_to)


async def renders_in_flight(db: AsyncSession, user_id: object) -> list[tuple[VideoRender, str]]:
    """The whole renders *user_id* asked for that are queued or running, oldest first, each with its video's title."""
    from app.models.video_studio import VideoProject

    rows = await db.execute(
        select(VideoRender, VideoProject.title)
        .outerjoin(VideoProject, VideoProject.id == VideoRender.project_id)
        .where(
            VideoRender.user_id == user_id,
            VideoRender.status.in_((JOB_PENDING, JOB_RUNNING)),
            # A scene preview is a short job of its own: it never takes a whole render's place.
            VideoRender.scene_id.is_(None),
        )
        .order_by(VideoRender.created_at)
    )
    return [(render, title or "a deleted video") for render, title in rows.all()]


async def jobs_in_flight(user_id: object) -> dict[str, list[str]]:
    """Every job of *user_id* still queued or running, by kind: their recordings, shots, tracks and the renders they asked for."""
    found: dict[str, list[str]] = {}
    async with async_session() as db:
        for kind, model in _MODELS.items():
            ids = (
                await db.execute(
                    select(model.id)
                    .where(model.user_id == user_id, model.status.in_((JOB_PENDING, JOB_RUNNING)))
                    .order_by(model.created_at)
                )
            ).scalars().all()
            if ids:
                found[kind] = [str(job_id) for job_id in ids]
    return found


async def may_read_job(db: AsyncSession, kind: str, row: Any, user: Any) -> bool:
    """Whether *user* may read the job *row* of *kind*: a render through its video's visibility, the rest by their owner.

    The rule ``get_video_jobs`` reports a job under, and a ``wait(until=...)`` watches it under.
    """
    if kind == "render":
        from app.services.video_studio.projects import load_visible_project

        return await load_visible_project(db, str(row.project_id), user) is not None
    return row.user_id == user.id


@dataclass(frozen=True)
class JobIds:
    """The ids a ``get_video_jobs`` call names, resolved by :func:`resolve_job_ids`."""

    #: ``{kind: [full id, ...]}``, every kind of :data:`JOB_ID_ARGUMENTS` present.
    ids: dict[str, list[str]]
    #: ``{id prefix as given: full id}`` for each prefix taken for one job.
    resolved: dict[str, str]
    #: The values given that are no id, or a prefix starting no job the caller may read.
    not_found: list[str]
    #: The refusal of a prefix starting several jobs, ``None`` when none does.
    ambiguous: str | None = None


async def resolve_job_ids(db: AsyncSession, asked: Mapping[str, list[Any]], user: Any) -> JobIds:
    """The ids of *asked* (``{kind: [value, ...]}``) as full job ids *user* may name.

    A full UUID is kept as it is (the wait reports it not found when it names nothing). An id prefix
    (8+ hexadecimal characters, ``builtin_tools.id_prefix``) is looked up among the jobs of its kind
    *user* may read (:func:`may_read_job`'s rule: a render through its video's visibility, the rest
    by their owner): one match stands for its full id, several are refused naming them, none is
    not found. Any other value is not found too, never dropped in silence.
    """
    from app.core.utils import safe_uuid
    from app.services.builtin_tools.id_prefix import ambiguous_text, id_prefix, ids_by_prefix

    ids: dict[str, list[str]] = {kind: [] for kind in asked}
    resolved: dict[str, str] = {}
    not_found: list[str] = []
    for kind, values in asked.items():
        model = _MODELS[kind]
        for value in values:
            if (parsed := safe_uuid(str(value).strip())) is not None:
                ids[kind].append(str(parsed))
                continue
            prefix = id_prefix(value)
            if prefix is None:
                not_found.append(str(value))
                continue
            candidates = await ids_by_prefix(
                db, model, prefix, await _readable_clauses(db, kind, user), label=_JOB_LABELS.get(kind)
            )
            if len(candidates) > 1:
                noun = {"take": "recordings", "clip": "AI shots", "music": "AI tracks", "render": "renders"}[kind]
                return JobIds(ids, resolved, not_found, ambiguous_text(str(value), noun, candidates))
            if not candidates:
                not_found.append(str(value))
                continue
            full = str(candidates[0][0])
            resolved[str(value).strip()] = full
            ids[kind].append(full)
    return JobIds(ids, resolved, not_found)


#: The column an ambiguous prefix names a job of each kind by.
_JOB_LABELS: dict[str, Any] = {"take": VideoTake.name, "clip": VideoClip.name, "music": VideoMusic.name}


async def _readable_clauses(db: AsyncSession, kind: str, user: Any) -> list[Any]:
    """The clauses keeping the jobs of *kind* that *user* may read (:func:`may_read_job`)."""
    if kind != "render":
        return [_MODELS[kind].user_id == user.id]
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.models.video_studio import VideoProject

    groups = await get_user_group_ids(db, user.id)
    visible = user_visible_filter(VideoProject, user.id, groups, is_admin=user.role == "admin")
    return [VideoRender.project_id.in_(select(VideoProject.id).where(visible))]


def may_cancel(row: Any, user: Any) -> bool:
    """Whether *user* may cancel the job *row*: only who started a job cancels it (``cancel_video_job``, and what
    ``get_video_jobs`` says of the jobs sharing a render's worker)."""
    return row.user_id is not None and row.user_id == user.id


async def watched_jobs(job_ids: list[str], user_id: object) -> dict[str, tuple[str, Any, str | None]]:
    """The jobs among *job_ids* that *user_id* may read, ``{id as given: (kind, row, name)}``.

    What a ``wait(until=...)`` naming a recording, an AI shot, an AI track or
    a render watches, read under :func:`may_read_job`. *name* is what the
    person calls the job - the title of a render's video (with its scene for a
    scene preview), the name of anything else - or ``None``. Any other id is
    left out.
    """
    from app.core.utils import safe_uuid
    from app.models.user import User
    from app.models.video_studio import VideoProject

    uid = safe_uuid(str(user_id)) if user_id else None
    given = {parsed: jid for jid in job_ids if (parsed := safe_uuid(str(jid))) is not None}
    if uid is None or not given:
        return {}
    found: dict[str, tuple[str, Any, str | None]] = {}
    async with async_session() as db:
        user = await db.get(User, uid)
        if user is None:
            return {}
        for kind, model in _MODELS.items():
            rest = [parsed for parsed, jid in given.items() if jid not in found]
            if not rest:
                break
            if kind == "render":
                rows = await db.execute(
                    select(model, VideoProject.title)
                    .outerjoin(VideoProject, VideoProject.id == model.project_id)
                    .where(model.id.in_(rest))
                )
                named = [
                    (row, f"{title} - scene {row.scene_id}" if title and row.scene_id else title)
                    for row, title in rows.all()
                ]
            else:
                rows = (await db.execute(select(model).where(model.id.in_(rest)))).scalars().all()
                named = [(row, getattr(row, "name", None)) for row in rows]
            for row, name in named:
                if await may_read_job(db, kind, row, user):
                    found[given[row.id]] = (kind, row, name)
    return found


def tool_wait_budget(default: float = 240.0) -> float:
    """Seconds a tool may wait on a job: until shortly before the loop would cancel the call."""
    from app.tools.tool_events import current_tool_deadline

    deadline = current_tool_deadline()
    if deadline is None:
        return default
    return max(0.0, min(default, deadline - time.monotonic() - 8.0))


def clip_inline_wait_budget() -> tuple[float, bool]:
    """``(seconds, capped)``: how long ``generate_video_clip`` waits for its shot inside the call.

    ``VIDEO_STUDIO_CLIP_INLINE_WAIT_SECONDS``, the same for every call of a
    batch, measured from the call's start; less only when the call's own
    deadline is nearer (``capped``), which the answer then says.
    """
    configured = float(max(0, settings.video_studio_clip_inline_wait_seconds))
    budget = tool_wait_budget(configured)
    return budget, budget < configured


def cancelled_by(user: Any, how: str) -> str:
    """The reason a job cancelled on a person's ask keeps: who asked and how (``Cancelled by a@b.c with cancel_video_job.``),
    so "Cancelled." is never read as a worker's stop (#9780)."""
    who = getattr(user, "email", None) or getattr(user, "id", None) or "its owner"
    return f"Cancelled by {who} with {how}."


async def cancel(db: AsyncSession, kind: str, row: Any, *, reason: str = "Cancelled.") -> bool:
    """Cancel a job that has not finished, saying why (*reason*); ``False`` when it already had.

    A ready AI shot cancelled while generated again stays ready on the take it kept, the attempt
    recorded beside it.
    """
    from app.services.orchestration_engine import cancel_execution

    if row.status in JOB_TERMINAL_STATUSES:
        return False
    if kind == "clip" and keeps_take(row.params):
        params = dict(row.params or {})
        for key, value in end_attempt(params, status=JOB_CANCELLED, reason=reason).items():
            setattr(row, key, value)
        row.params = params
        row.finished_at = datetime.now(UTC)
        row.provider_state = row.provider_eta_at = None
    else:
        row.status = JOB_CANCELLED
        row.error = reason
    row.owed_conversation_id = None
    if row.execution_id:
        await cancel_execution(db, execution_id=row.execution_id)
    return True


async def mark_cancelled(kind: str, job_id: object) -> None:
    """End a job its worker stopped on a cancel, keeping the reason :func:`cancel` wrote, if it wrote one."""
    async with async_session() as db:
        row = await db.get(_MODELS[kind], uuid.UUID(str(job_id)))
        if row is None:
            return
        if row.status != JOB_CANCELLED or not row.error:
            row.error = "Cancelled."
        row.status = JOB_CANCELLED
        row.finished_at = datetime.now(UTC)
        row.stage = None
        await db.commit()


async def mark(kind: str, job_id: object, **changes: Any) -> None:
    """Write *changes* on a job row, on a session of its own (progress, stage, outcome).

    A write of the progress or the stage is the worker's heartbeat: it stamps
    ``progress_at`` too unless *changes* set it.
    """
    if ("progress" in changes or "stage" in changes) and "progress_at" not in changes:
        changes["progress_at"] = datetime.now(UTC)
    async with async_session() as db:
        row = await db.get(_MODELS[kind], uuid.UUID(str(job_id)))
        if row is None:
            return
        for key, value in changes.items():
            setattr(row, key, value)
        await db.commit()


async def mark_if_open(kind: str, job_id: object, **changes: Any) -> bool:
    """Write *changes* on a job only while it is still queued or running; whether it was.

    One conditional ``UPDATE``: a job cancelled meanwhile - a render replaced
    by a newer one, which then records ``superseded_by_id`` - keeps the end and
    the reason :func:`cancel` wrote, never a failure written over them.
    """
    model = _MODELS[kind]
    async with async_session() as db:
        result = await db.execute(
            update(model)
            .where(model.id == uuid.UUID(str(job_id)), model.status.in_((JOB_PENDING, JOB_RUNNING)))
            .values(**changes)
        )
        await db.commit()
    return bool(result.rowcount)


async def heartbeat(kind: str, job_id: object, stage: str) -> None:
    """Say the worker has the job, before its own progress starts, on a job that has not finished.

    *stage* is written on a job no worker had yet; one starting again keeps
    the stage that says why ("Starting again: ...").
    """
    async with async_session() as db:
        row = await db.get(_MODELS[kind], uuid.UUID(str(job_id)))
        if row is None or row.status in JOB_TERMINAL_STATUSES:
            return
        if row.progress_at is None:
            row.stage = stage[:200]
        row.progress_at = datetime.now(UTC)
        await db.commit()


async def is_cancelled(kind: str, job_id: object) -> bool:
    async with async_session() as db:
        row = await db.get(_MODELS[kind], uuid.UUID(str(job_id)))
        return row is None or row.status == JOB_CANCELLED


# ── What a caller is told of a job ────────────────────────────────


@dataclass(frozen=True)
class QueuePlace:
    """Where a queued job stands: the jobs of its lane being made, those the worker takes before it, a worker's slots."""

    running: int
    ahead: int
    per_worker: int
    #: The lane of the render queue it waits in (:data:`LANES`): ``render``, ``preview`` or ``recording``;
    #: ``None`` for the other kinds, one lane each.
    lane: str | None = None
    #: A queued scene preview: the previews it waits behind - being made, and dealt before it - by video
    #: (``preview_lane.VideoAhead``); ``None`` for any other job.
    videos: tuple | None = None
    #: A queued scene preview: the workers that draw previews and their slots
    #: (``preview_routing.LaneWorkers``); ``None`` for any other job, or when no worker said it.
    workers: Any = None
    #: A queued recording: the recordings queued ahead of it, by name; ``None`` for any other job.
    recordings: tuple[str, ...] | None = None
    #: A queued recording waiting for its worker's load (``recording_lane.Wait``): what it measured beside which
    #: jobs, since when; ``None`` otherwise.
    wait: Any = None
    #: A queued recording no running worker claims (``recording_lane.refusing_workers``): each worker whose
    #: recording lane refuses recordings on its GL, and why; ``None`` otherwise.
    refusing: tuple[str, ...] | None = None
    #: A queued recording no running worker claims at all, none refusing on its GL either
    #: (``recording_lane.unserved_words``): why in words; ``None`` otherwise.
    unserved: str | None = None
    #: A queued scene preview (``preview_lane.expected_wait``): the seconds it is expected to wait at the lane's
    #: pace, and the lane's median draw time it is estimated with and of how many previews
    #: (``preview_lane.draw_seconds``); ``None`` when not known.
    expected_wait_s: float | None = None
    draw: tuple[float, int] | None = None
    #: A queued scene preview (``preview_lane.recent_p95_wait``): the p95 wait in the queue of the previews
    #: that started in the last hour, the lane's current load; ``None`` when none did.
    p95_wait_s: float | None = None
    #: A queued whole render (``render_queue.RenderWait``): the renders it waits behind, how long each has
    #: left, and when it starts; ``None`` for any other job.
    render_wait: Any = None

    @property
    def position(self) -> int:
        """Its place in the queue, from 1: the next job a free slot takes is 1."""
        return self.ahead + 1

    def slots_words(self) -> str:
        """``1 worker draws previews, 2 at a time``, or ``a worker makes 2 at a time`` when no worker said it."""
        return self.workers.words() if self.workers is not None else f"a worker makes {self.per_worker} at a time"

    def wait_words(self) -> str:
        """``; about 20 min before it starts at the lane's pace (...)`` for a queued scene preview, ``; the render
        being made ends in about 18 min (...) - this one starts in about 18 min`` for a queued whole render, else
        empty."""
        from app.services.video_studio.preview_lane import wait_words

        if self.render_wait is not None:
            said = self.render_wait.words()
        else:
            said = wait_words(self.expected_wait_s, self.draw, self.p95_wait_s) if self.videos is not None else ""
        return f"; {said}" if said else ""

    def expected_start_at(self, now: datetime | None = None) -> datetime | None:
        """When a queued scene preview is expected to start: *now* plus its expected wait; ``None`` when the
        wait is not known."""
        if self.expected_wait_s is None:
            return None
        return (now or datetime.now(UTC)) + timedelta(seconds=self.expected_wait_s)

    def as_dict(self) -> dict:
        data = {"position": self.position, "ahead": self.ahead, "running": self.running, "per_worker": self.per_worker}
        if self.lane is not None:
            data["lane"] = self.lane
        if self.videos is not None:
            data["videos_ahead"] = [video.as_dict() for video in self.videos]
        if self.workers is not None:
            data["preview_workers"] = self.workers.workers
            data["preview_slots"] = self.workers.slots
            # Of them, those its workers add while previews are queued.
            data["preview_slots_added"] = int(getattr(self.workers, "added", 0) or 0)
        if self.recordings is not None:
            data["recordings_ahead"] = list(self.recordings)
        if self.wait is not None:
            data["waits_for_load"] = self.wait.as_dict()
        if self.refusing:
            data["recording_lane_refused"] = list(self.refusing)
        if self.unserved:
            data["recording_lane_unserved"] = self.unserved
        if self.videos is not None:
            data["expected_wait_seconds"] = round(self.expected_wait_s) if self.expected_wait_s is not None else None
            data["median_draw_seconds"] = round(self.draw[0], 1) if self.draw is not None else None
            start = self.expected_start_at()
            data["expected_start_at"] = start.isoformat() if start is not None else None
            data["p95_wait_seconds"] = round(self.p95_wait_s) if self.p95_wait_s is not None else None
        if self.render_wait is not None:
            data.update(self.render_wait.as_dict())
        return data


@dataclass(frozen=True)
class Resume:
    """A render waiting for the next worker after its own stopped: how, since when, and why."""

    #: ``handed_over`` (its worker stopped and handed it back) or ``worker_gone`` (taken back from a worker gone).
    how: str
    #: When it went back to the queue.
    since: datetime
    #: Why, in words: the execution's last error (the stop, or how the worker went).
    reason: str | None = None
    #: When it last moved: its hand-over, or the progress its render recorded after it.
    moved: datetime | None = None
    #: The jobs running now on a slot of its lane (:func:`holds_slot_of`): while one does, it waits for that
    #: slot and is never failed.
    lane_running: int = 0
    #: How long it waits for a worker without moving before it fails (:func:`waits_for`).
    wait: timedelta = MIN_RESUME_WAIT
    #: The scene previews running on a lane of their own: counted apart, they hold no slot it waits for.
    previews_running: int = 0
    #: When a check last found its lane held (:data:`LANE_HELD_KEY`): its wait counts from then too.
    lane_held_at: datetime | None = None

    def waited_seconds(self, now: datetime) -> int:
        return max(0, int((now - self.since).total_seconds()))

    def grace_seconds(self, now: datetime) -> int | None:
        """How long before :func:`fail_unresumed_renders` fails it: its :attr:`wait` after it last moved or its lane
        was last held, whichever is later; ``None`` while a job holds a slot of its lane - the slot that frees takes
        it first."""
        if self.lane_running:
            return None
        moved = max(self.since, self.moved or self.since, self.lane_held_at or self.since)
        return max(0, int((self.wait - (now - moved)).total_seconds()))

    def deadline_clause(self, now: datetime) -> str:
        """``it fails if no worker resumes it in 540 s``, or, while its lane's slots are held, that it waits."""
        left = self.grace_seconds(now)
        one = self.previews_running == 1
        apart = (
            f" ({_preview_count(self.previews_running)} on {'its' if one else 'their'} own lane "
            f"{'holds' if one else 'hold'} none of it)"
            if self.previews_running
            else ""
        )
        if left is None:
            holders = f"{self.lane_running} job{'s' if self.lane_running != 1 else ''}"
            return (
                f"it waits while {holders} {'hold' if self.lane_running != 1 else 'holds'} a slot of its lane{apart}, "
                f"the next slot that frees its own; once none does, it fails if no worker resumes it within "
                f"{span(self.wait)}"
            )
        if left == 0:
            return (
                f"nothing holds a slot of its lane{apart} and it has not moved for {span(self.wait)}: it fails at the "
                "next check unless a worker resumes it"
            )
        return f"it fails if no worker resumes it in {left} s{apart}"

    def as_dict(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        return {
            "how": self.how,
            "since": self.since.isoformat(),
            "waiting_seconds": self.waited_seconds(now),
            "wait_seconds": int(self.wait.total_seconds()),
            "fails_in_seconds": self.grace_seconds(now),
            "lane_running": self.lane_running,
            "previews_running": self.previews_running,
            "lane_held_at": self.lane_held_at.isoformat() if self.lane_held_at else None,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class JobState:
    """One job as the caller acts on it: read from its row by :func:`job_state`, never from anything else."""

    #: ``take`` / ``render`` / ``clip`` / ``music``; :data:`KIND_LABELS` is what the caller reads.
    kind: str
    id: str
    #: :data:`STATE_QUEUED`, :data:`STATE_RUNNING`, :data:`STATE_READY` or :data:`STATE_FAILED`.
    status: str
    percent: int
    stage: str | None
    #: The worker's last progress write; ``None`` before it wrote one.
    last_progress_at: datetime | None
    #: Why it failed (the provider's or the worker's words); ``None`` otherwise.
    reason: str | None = None
    #: What the provider last said of an AI shot's upstream job.
    provider_state: str | None = None
    #: Seconds until the provider should be done, from the progress it reports.
    estimated_remaining_seconds: int | None = None
    queue: QueuePlace | None = None
    #: Why a queued job waits for something other than a worker: its provider cooling.
    held: str | None = None
    #: A failed regeneration of a ready AI shot: its previous take still plays wherever it is used.
    previous_take_plays: bool = False
    #: How a running job's execution lease stands: a worker that stopped renewing it shows before it expires.
    lease: LeaseHeartbeat | None = None
    #: A render a stopped worker had in flight, waiting for the next one: first in the queue.
    resume: Resume | None = None
    #: A job whose execution reads ``running`` with no lease at all: since its last write (:func:`reclaim_leaseless_renders`).
    no_lease_since: datetime | None = None

    def as_dict(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        data: dict[str, Any] = {
            "kind": KIND_LABELS[self.kind],
            "id": self.id,
            "status": self.status,
            "percent": self.percent,
            "stage": self.stage,
            "last_progress_at": self.last_progress_at.isoformat() if self.last_progress_at else None,
            "last_progress_seconds_ago": (
                max(0, int((now - self.last_progress_at).total_seconds())) if self.last_progress_at else None
            ),
            "reason": self.reason,
        }
        if self.provider_state is not None:
            data["provider_state"] = self.provider_state
            data["estimated_remaining_seconds"] = self.estimated_remaining_seconds
        if self.queue is not None:
            data["queue"] = self.queue.as_dict()
        if self.held is not None:
            data["held"] = self.held
        if self.previous_take_plays:
            data["previous_take_plays"] = True
        if self.lease is not None:
            data["lease"] = self.lease.as_dict()
        if self.resume is not None:
            data["resume"] = self.resume.as_dict(now)
        if self.no_lease_since is not None:
            data["stalled"] = f"no lease since {self.no_lease_since.isoformat()}"
        return data


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def job_state(kind: str, row: Any, *, now: datetime | None = None, queue: QueuePlace | None = None) -> JobState:
    """What *row* (a take, render, clip or AI track) says of its job; *queue* is where it stands when queued,
    *lease* how its execution's lease stands while it runs (:func:`job_states`).

    ``running`` as soon as the worker wrote a heartbeat (``progress_at``),
    whatever the row's status: a render starting its second attempt is
    pending again, and it is not waiting for a worker. An AI shot held in the
    queue while its provider cools is ``queued``, ``held`` saying until when;
    a ready shot whose regeneration failed is ``failed`` - that job
    did - with its previous take still playing (``previous_take_plays``).
    """
    now = now or datetime.now(UTC)
    heartbeat_at = _aware(getattr(row, "progress_at", None))
    common = {"kind": kind, "id": str(row.id), "last_progress_at": heartbeat_at}
    if row.status == JOB_READY:
        failed = failed_regeneration(row) if kind == "clip" else None
        if failed is not None:
            said = regeneration_reason(failed)
            return JobState(
                status=STATE_FAILED,
                percent=100,
                stage=None,
                reason=f"{said[:1].upper()}{said[1:]}.",
                previous_take_plays=True,
                **common,
            )
        return JobState(status=STATE_READY, percent=100, stage=None, **common)
    if row.status in JOB_TERMINAL_STATUSES:
        reason = row.error or ("Cancelled." if row.status == JOB_CANCELLED else "No reason was given.")
        return JobState(status=STATE_FAILED, percent=row.progress or 0, stage=None, reason=reason, **common)
    if row.status == JOB_RUNNING or heartbeat_at is not None:
        eta_at = _aware(getattr(row, "provider_eta_at", None))
        return JobState(
            status=STATE_RUNNING,
            percent=row.progress or 0,
            stage=getattr(row, "stage", None) or "Started",
            provider_state=getattr(row, "provider_state", None),
            estimated_remaining_seconds=max(0, int((eta_at - now).total_seconds())) if eta_at else None,
            **common,
        )
    held = None
    if getattr(row, "provider_state", None) == HELD_BY_PROVIDER:
        held = held_line(getattr(row, "stage", None), _aware(getattr(row, "provider_eta_at", None)), now)
    elif getattr(row, "provider_state", None) == provider_waits.WAITING:
        # Its provider busy: when it is asked next and how many times it was turned away.
        held = provider_waits.line(getattr(row, "params", None), _aware(getattr(row, "provider_eta_at", None)), now)
    return JobState(
        status=STATE_QUEUED,
        percent=0,
        stage=getattr(row, "stage", None) or "Queued",
        queue=queue,
        held=held,
        **common,
    )


async def queue_place(kind: str, row: Any, *, db: AsyncSession | None = None) -> QueuePlace:
    """Where a queued job stands: what a caller needs to tell a long queue from a stuck worker.

    Read from the executions the worker claims, never from the job
    rows: ``running`` counts the executions of the job's adapter - for a
    render, its lane - being run, ``ahead`` the pending ones due
    before its own - the order a free slot takes them in, a job queued again
    after a lost lease or a retry included. A job waiting its turn keeps its
    place (``defer(place="keep")``), and the renders a stopped worker had in
    flight, resumed first, are counted as being made (:data:`RESUMING_STATES`).
    On every other lane nothing queued after a job ever goes before it.
    The preview lane is dealt as its slots claim it, one preview per
    video in turn (``preview_lane.place_of``): a queued preview's place
    is its place in that deal, and ``videos`` names the videos whose previews
    it waits behind, those being made included. A queued recording
    (``recording_lane.place_of``) names the recordings ahead of it, and what it
    waits for when its worker's load could not capture it at its fps. A job
    whose execution is gone waits behind every pending one of the lane its row
    says. Read on *db* when given (the start that queued it, before it commits),
    else on a session of its own.
    """
    resuming = OrchestrationExecution.adapter_state.in_(RESUMING_STATES)
    waiting = or_(OrchestrationExecution.adapter_state.is_(None), OrchestrationExecution.adapter_state.not_in(RESUMING_STATES))

    async def read(session: AsyncSession) -> tuple[str, int, int, dict]:
        execution_id = getattr(row, "execution_id", None)
        own = await session.get(OrchestrationExecution, execution_id) if execution_id else None
        adapter = getattr(own, "adapter", None)
        if adapter not in _CONCURRENCY:
            adapter = adapter_of(kind, row)
        if own is not None and adapter == PREVIEW_ADAPTER:
            from app.services.video_studio import preview_lane, preview_routing

            lane_workers = await preview_routing.lane_workers()
            place = await preview_lane.place_of(session, own, lane_workers)
            # Its wait at the lane's pace, said as it is queued: its place, the slots, the median draw time;
            # and the lane's load, the p95 wait of the previews started in the last hour.
            draw = await preview_lane.draw_seconds(session)
            wait = preview_lane.expected_wait(
                place.ahead, place.running, preview_lane.open_slots(lane_workers), draw[0] if draw else None
            )
            p95 = await preview_lane.recent_p95_wait(session)
            return (
                adapter,
                place.running,
                place.ahead,
                {"videos": place.videos, "expected_wait_s": wait, "draw": draw, "p95_wait_s": p95},
            )
        if own is not None and adapter == TAKE_ADAPTER:
            from app.services.video_studio import recording_lane

            taken = await recording_lane.place_of(session, own)
            refusing = await recording_lane.refusing_workers() if not taken.running else ()
            # No worker claims takes and none refuses on its GL: a rollout between releases, or none deployed.
            unserved = await recording_lane.unserved_words() if not taken.running and not refusing else None
            return (
                adapter,
                taken.running,
                len(taken.ahead),
                {"recordings": taken.ahead, "wait": taken.wait, "refusing": refusing or None, "unserved": unserved},
            )
        of_adapter = OrchestrationExecution.adapter == adapter
        queued_ahead = [of_adapter, OrchestrationExecution.state == "pending", waiting]
        if own is not None:
            queued_ahead += [
                OrchestrationExecution.id != own.id,
                or_(
                    OrchestrationExecution.run_at < own.run_at,
                    and_(
                        OrchestrationExecution.run_at == own.run_at,
                        OrchestrationExecution.created_at < own.created_at,
                    ),
                ),
            ]
        count = select(func.count()).select_from(OrchestrationExecution)
        being_made = or_(
            OrchestrationExecution.state == "running", and_(OrchestrationExecution.state == "pending", resuming)
        )
        running = (await session.execute(count.where(of_adapter, being_made))).scalar_one()
        ahead = (await session.execute(count.where(*queued_ahead))).scalar_one()
        if adapter == RENDER_ADAPTER and running:
            from app.services.video_studio import render_queue

            # When it starts: the renders being made end, then those queued before it are drawn.
            wait = await render_queue.read(session, and_(of_adapter, being_made), queued_ahead, now=datetime.now(UTC))
            return adapter, int(running), int(ahead), {"render_wait": wait}
        return adapter, int(running), int(ahead), {}

    if db is not None:
        adapter, running, ahead, extra = await read(db)
    else:
        async with async_session() as session:
            adapter, running, ahead, extra = await read(session)
    workers = None
    if adapter == PREVIEW_ADAPTER:
        from app.services.video_studio import preview_routing

        # How many workers draw previews: one seen is a lane of one worker's slots, whatever a deployment declares.
        workers = await preview_routing.lane_workers()
    return QueuePlace(
        running=running, ahead=ahead, per_worker=slots(adapter), lane=LANES.get(adapter), workers=workers, **extra
    )


async def _executions(execution_ids: list[uuid.UUID]) -> dict[uuid.UUID, OrchestrationExecution]:
    """The executions of *execution_ids*, read in one query."""
    if not execution_ids:
        return {}
    async with async_session() as db:
        rows = (
            await db.execute(select(OrchestrationExecution).where(OrchestrationExecution.id.in_(execution_ids)))
        ).scalars().all()
    return {row.id: row for row in rows}


def _lane_held_at(execution: Any) -> datetime | None:
    """When a check last found the lane of a resumed render's *execution* held (:data:`LANE_HELD_KEY`), if one did."""
    recorded = (getattr(execution, "payload", None) or {}).get(LANE_HELD_KEY)
    if not isinstance(recorded, str):
        return None
    try:
        return _aware(datetime.fromisoformat(recorded))
    except ValueError:
        return None


def resume_of(execution: Any, row: Any = None, *, lane_running: int = 0, previews_running: int = 0) -> Resume | None:
    """The :class:`Resume` of a render's *execution* waiting for the next worker (:data:`RESUMING_STATES`), else
    ``None``: when it last moved (its *row*'s progress after the hand-over), the jobs holding its lane's slots and the
    previews running apart (:func:`lane_counts`), when its lane was last held, and how long it waits
    (:func:`waits_for`)."""
    if execution is None or execution.state != "pending" or execution.adapter_state not in RESUMING_STATES:
        return None
    how = "handed_over" if execution.adapter_state == HANDED_OVER_STATE else "worker_gone"
    since = _aware(execution.updated_at) or datetime.now(UTC)
    progressed = _aware(getattr(row, "progress_at", None))
    moved = max(since, progressed) if progressed is not None else since
    return Resume(
        how=how,
        since=since,
        reason=execution.last_error,
        moved=moved,
        lane_running=int(lane_running),
        wait=waits_for(execution),
        previews_running=int(previews_running),
        lane_held_at=_lane_held_at(execution),
    )


async def job_states(kind: str, rows: list[Any]) -> list[JobState]:
    """The :class:`JobState` of each row: its queue place when it is queued, its lease while it runs, and what it
    waits for when a stopped worker left it."""
    now = datetime.now(UTC)
    states = []
    for row in rows:
        state = job_state(kind, row, now=now)
        if state.status == STATE_QUEUED:
            state = job_state(kind, row, now=now, queue=await queue_place(kind, row))
        states.append(state)
    running = [
        row.execution_id
        for row, state in zip(rows, states, strict=True)
        if state.status == STATE_RUNNING and getattr(row, "execution_id", None)
    ]
    executions = await _executions(running)
    waiting = {e.adapter for e in executions.values() if resume_of(e) is not None}
    held = await _lanes_held(waiting)
    found = []
    for row, state in zip(rows, states, strict=True):
        execution = executions.get(getattr(row, "execution_id", None))
        if execution is not None:
            lease = lease_heartbeat(execution, now)
            lane, apart = held.get(execution.adapter, (0, 0))
            resume = resume_of(execution, row, lane_running=lane, previews_running=apart) if lease is None else None
            state = replace(state, lease=lease, resume=resume, no_lease_since=no_lease_since(execution))
        found.append(state)
    return found


async def _lanes_held(adapters: set[str]) -> dict[str, tuple[int, int]]:
    """``adapter -> (the jobs holding a slot of its lane now, the previews running apart)`` (:func:`lane_counts`),
    for *adapters*; one query."""
    if not adapters:
        return {}
    async with async_session() as db:
        running = await _running_renders(db)
    return {adapter: lane_counts(adapter, running) for adapter in adapters}


def lease_clause(state: JobState, now: datetime | None = None) -> str:
    """Its lease and its heartbeat: ``, attempt 2 of 3; its lease (worker w1) was renewed 12 s ago and
    expires in 168 s``, a warning when a renewal was missed, or what a render a stopped worker left waits for.

    Empty for a job with neither: queued, or done.
    """
    if state.resume is not None:
        resume = state.resume
        now = now or datetime.now(UTC)
        how = "handed back by its stopping worker" if resume.how == "handed_over" else "taken back from a worker gone"
        cause = f" ({resume.reason.rstrip('.')})" if resume.reason else ""
        return (
            f"; {how} {resume.waited_seconds(now)} s ago{cause}, first in the queue for the next worker - "
            f"{resume.deadline_clause(now)}"
        )
    if state.no_lease_since is not None:
        return (
            f"; stalled: no lease since {state.no_lease_since.isoformat()} - no worker holds it, it is taken back "
            f"(run again, or failed with no attempt left) once quiet {span(LEASELESS_STALL)}"
        )
    lease = state.lease
    if lease is None:
        return ""
    said = f", attempt {lease.attempt} of {lease.max_attempts}" if lease.attempt > 1 else ""
    relaunched = getattr(lease, "infra_relaunches", 0) or 0
    if relaunched:
        # Relaunches for a worker lost outside the work (a deploy, a restart) are counted apart: no attempt spent.
        said += f", {relaunched} relaunch{'es' if relaunched > 1 else ''} after a worker restart or deploy not counted"
        killed = getattr(lease, "rollout_kills", 0) or 0
        if killed:
            said += f" ({killed} relaunch{'es' if killed > 1 else ''} after a rollout kill, not counted)"
    holder = f" (worker {lease.owner})" if lease.owner else ""
    said += f"; its lease{holder} was renewed {lease.silent_seconds} s ago and expires in {lease.expires_in_seconds} s"
    if lease.late:
        then = (
            "it runs again by itself if the lease expires"
            if lease.attempt < lease.max_attempts
            else "it fails if the lease expires: this is its last attempt"
        )
        said += f": a renewal was missed, its worker may have stalled, and {then}"
    return said


def noun(kind: str, row: Any = None) -> str:
    """What one job of *kind* is called (``AI shot``, ``render``); a render of one scene is a ``scene preview``."""
    if kind == "render" and getattr(row, "scene_id", None):
        return _PREVIEW_NOUNS[0]
    return _NOUNS[kind][0]


def _count(number: int, kind: str) -> str:
    one, several = _NOUNS[kind]
    return f"{number} {one if number == 1 else several}"


def count_phrase(counts: Mapping[str, int]) -> str:
    """``2 renders and 1 AI shot``: how many jobs of each kind, in the words a caller reads."""
    parts = [_count(number, kind) for kind, number in counts.items() if number]
    if len(parts) < 2:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def describe(state: JobState) -> str:
    """Where a job that has not finished is, as a clause: what a background answer and a poll say."""
    if state.status == STATE_QUEUED:
        if state.held is not None:
            return f"{state.held} - it starts by itself then, nothing failed"
        if state.queue is None:
            return "waiting for a worker"
        lane = state.queue.lane
        if state.queue.videos is not None:
            from app.services.video_studio.preview_lane import ahead_words

            ahead = ahead_words(state.queue.videos) or "next for a free slot of the preview lane"
            return f"waiting for a worker: {ahead} ({state.queue.slots_words()}){state.queue.wait_words()}"
        if state.queue.recordings is not None:
            from app.services.video_studio import recording_lane

            return recording_lane.line(state.queue)
        one, several = _PREVIEW_NOUNS if lane == "preview" else _NOUNS[state.kind]
        running = state.queue.running
        return (
            f"waiting for a worker: {running} {one if running == 1 else several} being made now, "
            f"{state.queue.ahead} queued ahead of it{f' in the {lane} lane' if lane else ''} "
            f"({state.queue.slots_words()}){state.queue.wait_words()}"
        )
    provider = state.provider_state
    if provider is None:
        return f"{state.stage} ({state.percent}%)" if state.stage else f"{state.percent}% done"
    if provider == "queued":
        return "the provider has it queued"
    if provider in (STATE_RATE_LIMITED, STATE_UNAVAILABLE):
        cause = "rate limited by the provider" if provider == STATE_RATE_LIMITED else "the provider is not answering"
        return f"{cause}: asked again after a back-off"
    if provider in ("completed", "done"):
        return "made by the provider, being saved"
    left = (
        f", about {state.estimated_remaining_seconds} s left" if state.estimated_remaining_seconds is not None else ""
    )
    return f"the provider is generating it ({provider}, {state.percent}%{left})"


def serialize_take(take: VideoTake, *, in_use: bool = False) -> dict:
    """A recording; *in_use*: a video uses it (``retention.expires_at``)."""
    return {
        "id": str(take.id),
        "user_id": str(take.user_id) if take.user_id else None,
        "profile_id": str(take.profile_id) if take.profile_id else None,
        "name": take.name,
        "status": take.status,
        "progress": take.progress,
        "stage": take.stage,
        "error": take.error,
        "duration_ms": take.duration_ms,
        "viewport": [take.viewport_width, take.viewport_height] if take.viewport_width else None,
        "pixel_ratio": take.pixel_ratio,
        "fps": take.fps,
        "captured_frames": take.captured_frames,
        "size_bytes": take.size_bytes,
        "file_id": str(take.file_id) if take.file_id else None,
        "poster_file_id": str(take.poster_file_id) if take.poster_file_id else None,
        "contact_sheet_file_id": str(take.contact_sheet_file_id) if take.contact_sheet_file_id else None,
        "steps": len((take.script or {}).get("steps") or []),
        "truncated_at_step": truncated_at_step(take),
        # The worker class it asked for (``record_browser_demo(worker_class=...)``), and the one that recorded it.
        "worker_class": ((take.submitted_script or {}).get("params") or {}).get("worker_class")
        if isinstance(take.submitted_script, dict)
        else None,
        "worker": (take.capture or {}).get("worker") if isinstance(take.capture, dict) else None,
        # The build that recorded it and, after a relaunch, the one that finished it (#11407); None before them.
        "build": (take.capture or {}).get("build") if isinstance(take.capture, dict) else None,
        "resumed_build": (take.capture or {}).get("resumed_build") if isinstance(take.capture, dict) else None,
        "created_at": take.created_at.isoformat() if take.created_at else None,
        "finished_at": take.finished_at.isoformat() if take.finished_at else None,
        **retention.retention_fields(take, in_use=in_use),
    }


def truncated_at_step(take: VideoTake) -> int | None:
    """The step a ready recording's time ran out at (``capture.record_take``): every step it played logs one
    event, so its action log stops before its script's end. ``None`` for a recording that played every step."""
    if take.status != JOB_READY:
        return None
    logged = len(take.events or [])
    return logged if logged < len((take.script or {}).get("steps") or []) else None


def take_timeline(take: VideoTake) -> list[dict]:
    """The action log as an agent reads it to place overlays: step, action, label, seconds."""
    lines = []
    for event in take.events or []:
        lines.append(
            {
                "step": event.get("step"),
                "action": event.get("action"),
                "label": event.get("label"),
                "at": round(float(event.get("ta", event.get("t0") or 0.0)), 2),
                "ends": round(float(event.get("t1") or 0.0), 2),
                "ok": event.get("ok", True),
                "has_element": bool(event.get("box")),
                "url": event.get("url"),
                # Its frames a second while its page painted (``capture_rate.step_rates``).
                "capture_fps": event.get("capture_fps"),
                # Each canvas transition it started and the rate it was captured at (``capture_rate.canvas_motion``).
                "canvas_motion": event.get("canvas_motion"),
                # The share of its motion's frames synthesised from keyframes, where it steps (``capture_rate.stepping``).
                "synthesised": event.get("synthesised"),
                # How evenly its scroll moved a frame of the take to the next, and whether it steps (``scroll_pacing.cadence``).
                "scroll_cadence": event.get("scroll_cadence"),
                # A wait held until its page settled: when it did (``page_settle``).
                "settled": event.get("settled"),
                # How a node step found its node and what its right-click opened (``located_words``).
                "located": located_words(event),
            }
        )
    return lines


def located_words(event: dict) -> str:
    """How a node step's node was found, said in the action log: the stage that resolved it and the
    point (CSS px of the window), and the menu its right-click opened, by its first title; empty for any other step."""
    located = event.get("located")
    if not isinstance(located, dict):
        return ""
    point = located.get("point") if isinstance(located.get("point"), dict) else event.get("point")
    said = f"node located by {located.get('by') or 'its locator'}"
    if isinstance(point, dict) and isinstance(point.get("x"), int | float) and isinstance(point.get("y"), int | float):
        said += f" at ({point['x']:.0f}, {point['y']:.0f})"
    menu = event.get("menu")
    if isinstance(menu, dict):
        titles = menu.get("titles") or []
        checked = event.get("menu_check") if isinstance(event.get("menu_check"), dict) else {}
        headed = f" headed {str(titles[0])[:60]!r}" if titles else ""
        verdict = "" if not checked else ", the node's" if checked.get("node") else ", not the node's"
        said += f"; its menu{headed}, {len(menu.get('items') or [])} entries{verdict}"
    elif event.get("action") == "right_click":
        said += "; no menu opened"
    return said


def serialize_clip(clip: VideoClip, *, in_use: bool = False) -> dict:
    """An AI shot; *in_use*: a video uses it (``retention.expires_at``)."""
    return {
        "id": str(clip.id),
        "user_id": str(clip.user_id) if clip.user_id else None,
        "name": clip.name,
        "provider": clip.provider,
        "model": clip.model,
        "prompt": clip.prompt,
        "params": clip.params or {},
        "status": clip.status,
        "progress": clip.progress,
        "error": clip.error,
        "duration_ms": clip.duration_ms,
        "width": clip.width,
        "height": clip.height,
        "has_audio": clip.has_audio,
        "file_id": str(clip.file_id) if clip.file_id else None,
        "poster_file_id": str(clip.poster_file_id) if clip.poster_file_id else None,
        #: What it opened on: the frame kept from its video, else its start image.
        "start_frame_file_id": start_frame_file_id(clip.params),
        #: The first, middle and last frames of the take that plays and what they measure.
        **frame_qc.summary(take_params(clip.params)),
        #: A ready shot generated again: being made, or failed, its previous take playing meanwhile.
        "regeneration": regeneration_state(clip),
        #: The models it falls back to when a take fails its checks, those that failed and which made the take.
        "fallback": fallback_state(clip),
        #: Waiting in the queue while its provider cools or is busy: when it is asked.
        "waiting_for_provider_until": (
            clip.provider_eta_at.isoformat()
            if clip.provider_state in (HELD_BY_PROVIDER, provider_waits.WAITING) and clip.provider_eta_at is not None
            else None
        ),
        "created_at": clip.created_at.isoformat() if clip.created_at else None,
        "finished_at": clip.finished_at.isoformat() if clip.finished_at else None,
        **retention.retention_fields(clip, in_use=in_use),
    }


def new_take(
    *,
    user_id: object,
    profile_id: object,
    conversation_id: object,
    name: str,
    script: dict,
    submitted_script: dict | None = None,
) -> VideoTake:
    return VideoTake(
        user_id=user_id,
        profile_id=profile_id,
        conversation_id=conversation_id,
        name=name[:200],
        status=JOB_PENDING,
        script=script,
        submitted_script=submitted_script,
        stage="Queued",
    )
