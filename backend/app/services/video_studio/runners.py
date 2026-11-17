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

"""What the worker runs for a recording, a clip and an AI track.

Each runner owns its sessions (short ones: a browser recording or a provider
poll lasts minutes and must not pin a pooled connection), reports progress on
its row, and answers a :class:`JobOutcome` the adapter maps onto the engine:
``done`` (the row says ready, failed or cancelled), ``retry`` (transient, the
engine backs off), ``hold`` (the provider rate limited a request it never
started: waited out without spending an attempt), ``wait`` (a take
short of its fps beside other jobs, back in the recording lane) or
``failed`` (the engine dead-letters it).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.config import settings
from app.core.database import async_session
from app.core.utils import format_exception_message
from app.llm.identity import bind_llm_caller
from app.models.browser_profile import BrowserProfile
from app.models.video_studio import (
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_PENDING,
    JOB_READY,
    JOB_RUNNING,
    JOB_TERMINAL_STATUSES,
    VideoClip,
    VideoTake,
)
from app.services.video_studio import (
    assets,
    canvas_alone,
    capture_rate,
    cpu_share,
    encoder,
    jobs,
    pacing_checkpoint,
    scroll_pacing,
    take_repair,
    take_window,
)

if TYPE_CHECKING:
    from app.services.orchestration_engine import LeaseExpiry
    from app.services.video_studio.recording_lane import Slot

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobOutcome:
    # done | retry | hold | failed | lease_lost (a render the engine took back) | again | handover (its worker
    # stops) | given_back (a scene preview gave its lent slot back) | wait (a take back in its lane)
    kind: str
    error: str | None = None
    #: A ``hold``: the seconds the provider asked to wait, when it said.
    wait: float | None = None
    #: A ``hold`` for a provider that cools for its quota: waited out up to ``provider_cooling.MAX_HOLD``.
    quota: bool = False
    #: A ``hold`` for a provider busy (``provider_waits``): its exact *wait*, waited out up to the same bound.
    busy: bool = False
    #: A render the platform failed (``render_retry``): its cause - ``chromium``, ``memory``, ...
    cause: str | None = None
    #: An ``again``: the record its execution keeps of the one run again it had (``render_retry.INFRA_RETRY_KEY``).
    again: dict | None = None
    #: A ``wait``: a take short of its fps beside other jobs, back in the recording lane - the wait its
    #: execution keeps (``recording_lane.WAIT_KEY``), due again in *wait* seconds.
    lane: dict | None = None


def _now() -> datetime:
    return datetime.now(UTC)


# ── Recording ─────────────────────────────────────────────────────────────


async def run_take(take_id: str, *, cancel_event: asyncio.Event | None = None, lane: Slot | None = None) -> JobOutcome:
    """Record the take *take_id* in the slot the recording lane granted it (*lane*, ``recording_lane.Slot``).

    Short of its fps beside other jobs on its worker (``capture.WorkerBusyError``), it records nothing and
    answers ``wait``: its row queued again, saying what it waits for, its execution back in its place in the lane.
    """
    from app.services.browser_profiles import signin
    from app.services.browser_profiles.browser import TargetPolicy, TargetRefusedError, target_policy
    from app.services.browser_profiles.profiles import resolved_snapshot, save_storage_state
    from app.services.video_studio import capture, insert_floor, recording_lane, take_state
    from app.services.video_studio.spec import parse_take_script, stored_take_script

    async with async_session() as db:
        queued = await db.get(VideoTake, uuid.UUID(take_id))
        repairs = take_repair.repair_source(queued.capture) if queued is not None else None
        open_ = queued is not None and queued.status not in JOB_TERMINAL_STATUSES
        resume = take_repair.resume_point(queued.capture) if open_ and repairs is None else None
    if repairs is not None:
        # A finished take repaired: no browser, no capture - its passes run again on the CPU.
        return await run_repair(take_id, cancel_event=cancel_event)
    if resume is not None:
        # Its capture done and its keyframes stored before its worker stopped: finished from them, not recorded again.
        finished = await _resumed_take(take_id, resume, cancel_event=cancel_event)
        if finished is not None:
            return finished
    async with async_session() as db:
        take = await db.get(VideoTake, uuid.UUID(take_id))
        if take is None or take.status in JOB_TERMINAL_STATUSES:
            return JobOutcome("done")
        profile = await db.get(BrowserProfile, take.profile_id) if take.profile_id else None
        snap = await resolved_snapshot(profile) if profile is not None else None
        script = parse_take_script(take.script)
        user_id = take.user_id
        take_name = take.name
        # Made for a screen insert (record_browser_demo's target_render): held to the insert capture floor below.
        marked = insert_floor.mark_of(getattr(take, "capture", None))
        # Recorded once more after a take refused only for spans its worker's quota starved (#11097).
        queued_capture = getattr(take, "capture", None)
        starved_before = queued_capture.get(STARVED_RETRY_KEY) if isinstance(queued_capture, dict) else None
        # The steps the person's recent takes with this profile found: a step that misses its element here
        # says where one of them found it.
        earlier = await take_state.load_earlier_finds(db, take_id=take.id, user_id=user_id, profile_id=take.profile_id)
        take.status = JOB_RUNNING
        take.started_at = take.progress_at = _now()
        take.progress = 2
        take.stage = "Starting the browser"
        await db.commit()
    start_url = script.start_url or (snap.start_url if snap else None)
    if not start_url:
        await jobs.mark("take", take_id, status=JOB_FAILED, error="The recording has no start URL.", finished_at=_now())
        return JobOutcome("done")
    policy = (
        snap.policy if snap else target_policy(start_url=None, login_url=None, allowed_hosts=None) or TargetPolicy()
    )

    async def progress(value: int, stage: str) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError
        await jobs.mark("take", take_id, progress=value, stage=stage[:200])

    workdir = tempfile.mkdtemp(prefix="xtm-take-")
    resumable = False
    try:
        try:
            result = await capture.record_take(
                script,
                workdir=workdir,
                profile=snap,
                start_url=start_url,
                policy=policy,
                max_seconds=float(settings.video_studio_max_take_seconds),
                progress=progress,
                warm_up=True,
                earlier=earlier,
                lane=lane,
            )
        except capture.WorkerBusyError as exc:
            # Short of its fps beside other jobs: nothing recorded, queued again in its place in the lane.
            if lane is None:
                raise
            wait = lane.waiting(exc.plan, exc.wanted, _now(), relaunch=getattr(exc, "relaunch", False))
            queued = await jobs.mark_if_open(
                "take", take_id, status=JOB_PENDING, started_at=None, progress_at=None, progress=0, stage=wait.stage()
            )
            if not queued:
                return JobOutcome("done")
            return JobOutcome("wait", wait.stage(), wait=recording_lane.POLL_SECONDS, lane=wait.as_dict())
        except capture.ProfileNotAuthenticatedError as exc:
            if snap is not None:
                await signin.store_probe(snap.id, exc.probe, message=f"The recording stopped before step 1: {exc}")
            status = getattr(exc.probe, "http_status", None)
            if status is not None:
                # The application did not answer: a deploy or a restart outlasts the probe's own retries. Queued
                # again for the adapter's wait, never signed in again: its session was not judged.
                queued = await jobs.mark_if_open(
                    "take",
                    take_id,
                    status=JOB_PENDING,
                    started_at=None,
                    progress_at=None,
                    progress=0,
                    stage=f"Waiting for the application to answer (HTTP {status})",
                )
                return JobOutcome("hold", str(exc)[:2000]) if queued else JobOutcome("done")
            await jobs.mark("take", take_id, status=JOB_FAILED, error=str(exc)[:2000], finished_at=_now(), stage=None)
            return JobOutcome("done")
        except capture.CredentialRefusedError as exc:
            # What the step would have typed is a credential: the stored script keeps none of it.
            stored = stored_take_script(script)
            stored["steps"][exc.step]["text"] = ""
            await jobs.mark(
                "take", take_id, status=JOB_FAILED, error=str(exc)[:2000], finished_at=_now(), stage=None, script=stored
            )
            return JobOutcome("done")
        except capture.StepFailedError as exc:
            # The failed take keeps the page it opened on, a contact sheet ending on the frame where the step failed
            # and its action log up to there (how each node step was located).
            sheet = await _failure_sheet(exc, workdir, user_id=user_id, name=take_name)
            await jobs.mark(
                "take",
                take_id,
                status=JOB_FAILED,
                error=str(exc)[:2000],
                finished_at=_now(),
                stage=None,
                # What it opened on, and what drew its canvases and what they copied up to the failure.
                capture={**({"opened": exc.opened} if exc.opened else {}), **exc.capture} or None,
                contact_sheet_file_id=sheet,
                events=list(exc.events) or None,
            )
            return JobOutcome("done")
        except (capture.TakeError, TargetRefusedError) as exc:
            await jobs.mark("take", take_id, status=JOB_FAILED, error=str(exc)[:2000], finished_at=_now(), stage=None)
            return JobOutcome("done")
        if await jobs.is_cancelled("take", take_id):
            return JobOutcome("done")
        # Recorded at another window, pixel ratio or rate when its fps_fallback took one.
        played = result.script or script

        async def keep_for_resume(path: str) -> Any:
            # Its keyframes stored before its passes and its encode: a worker's stop from here on leaves the next
            # worker what to finish it from (``_resumed_take``), never a take to record again.
            nonlocal resumable
            stored = await _store_keyframes(path, base=_slug(take_name or "recording"), user_id=user_id)
            point = {"notes": list(result.notes or []), "layout": result.layout, "fps_fallback": played.fps_fallback}
            resumable = await _resume_point_written(take_id, stored.id, point)
            return stored

        kept = await _kept_take(
            take_id,
            user_id=user_id,
            workdir=workdir,
            frames=result.frames,
            events=result.events,
            alone=result.alone,
            painted=result.painted,
            recorded=result.capture,
            duration=result.duration,
            pixel_ratio=result.pixel_ratio,
            viewport=result.viewport,
            fps=played.fps,
            cursor=result.cursor,
            notes=result.notes,
            progress=progress,
            on_packed=keep_for_resume,
            checkpoint=True,
        )
        captured, notes, info, video = kept.captured, kept.notes, kept.info, kept.video
        # On GPU GL under fps_fallback: refuse, motion still synthesised from keyframes is refused with its rate,
        # never kept as a take that steps: its stepping stretches and its paced transitions (events as kept).
        if refused := capture_rate.refusal(captured, played.fps_fallback, result.events):
            await assets.delete_files(kept.file_ids())
            # Refused only where the worker's quota starved it: the worker's fault, not the page's. Recorded once more
            # in its place in the lane, the next claim's worker recording it, before it is refused (#11097).
            if starved_before is None and capture_rate.starved_only(captured, played.fps_fallback, result.events):
                again = {
                    **insert_floor.kept(marked),
                    STARVED_RETRY_KEY: {"worker": lane.worker if lane is not None else None},
                }
                requeued = await jobs.mark_if_open(
                    "take",
                    take_id,
                    status=JOB_PENDING,
                    started_at=None,
                    progress_at=None,
                    progress=0,
                    stage=STARVED_RETRY_STAGE,
                    capture=again,
                )
                if not requeued:
                    return JobOutcome("done")
                return JobOutcome("wait", STARVED_RETRY_STAGE, wait=recording_lane.POLL_SECONDS, lane=None)
            if starved_before is not None:
                refused = f"{refused} {STARVED_RETRY_WORDS}"
            # Its capture and events kept: the refusal's reader says the same capture line a ready take says.
            await jobs.mark(
                "take",
                take_id,
                status=JOB_FAILED,
                # Its step notes after the refusal, as a ready take says them.
                error=capture_rate.error_lines([refused, *notes]),
                finished_at=_now(),
                stage=None,
                capture=captured or None,
                events=list(result.events or []) or None,
            )
            return JobOutcome("done")
        # Made for a screen insert and painted under the floor an insert source needs: recorded once more in the
        # recording lane, by the worker that claims it then; under it again, failed saying both rates and the size
        # that would sustain it (``insert_floor``, #10079).
        floor = insert_floor.judge(
            marked,
            captured_frames=len(result.frames),
            duration=info.duration,
            viewport=result.viewport,
            pixel_ratio=result.pixel_ratio,
            worker=lane.worker if lane is not None else None,
            measured=insert_floor.measured_of(captured),
        )
        if floor is not None and floor.kind == insert_floor.AGAIN and marked is not None and lane is not None:
            await assets.delete_files(kept.file_ids())
            queued = await jobs.mark_if_open(
                "take",
                take_id,
                status=JOB_PENDING,
                started_at=None,
                progress_at=None,
                progress=0,
                stage=floor.stage(),
                capture=insert_floor.retry_capture(marked, floor),
            )
            if not queued:
                return JobOutcome("done")
            # Back in its place in the lane, no attempt spent, waiting for nothing: the next claim records it.
            return JobOutcome("wait", floor.stage(), wait=recording_lane.POLL_SECONDS, lane=None)
        if floor is not None and floor.kind in (insert_floor.AGAIN, insert_floor.FAILED):
            # Under the floor on its second take (or with no lane to go back to): failed, never ready.
            failed = replace(floor, kind=insert_floor.FAILED)
            await assets.delete_files(kept.file_ids())
            await jobs.mark(
                "take",
                take_id,
                status=JOB_FAILED,
                error="\n".join([failed.words(), *notes])[:2000],
                finished_at=_now(),
                stage=None,
                capture={**(captured or {}), **insert_floor.kept(marked)} or None,
                events=list(result.events or []) or None,
            )
            return JobOutcome("done")
        if floor is not None and floor.kind == insert_floor.PASSED_AGAIN:
            notes = [floor.words(), *notes]
        async with async_session() as db:
            take = await db.get(VideoTake, uuid.UUID(take_id))
            if take is None or take.status == JOB_CANCELLED:
                await assets.delete_files(kept.file_ids())
                return JobOutcome("done")
            take.status = JOB_READY
            take.progress = 100
            take.stage = None
            take.error = "\n".join(notes) or None
            take.events = result.events
            # What it was recorded for kept beside its capture: a repair or a re-record reads it.
            take.capture = {**captured, **insert_floor.kept(marked)} if marked else captured
            recorded = (captured.get("pixel_ratio") or {}).get("recorded")
            if recorded is not None and recorded != script.pixel_ratio:
                # Recorded at the ratio the worker sustains: its script says the ratio its frames have.
                take.script = {**(take.script or {}), "pixel_ratio": int(recorded) if float(recorded).is_integer() else recorded}
            if played.viewport != script.viewport or played.fps != script.fps:
                # Its fps_fallback's window or rate: every reader of its size or its pace quotes its script.
                take.script = {**(take.script or {}), "viewport": played.viewport.model_dump(), "fps": played.fps}
            take.cursor = result.cursor
            take.duration_ms = int(info.duration * 1000)
            take.viewport_width, take.viewport_height = result.viewport
            take.pixel_ratio = result.pixel_ratio
            take.layout = result.layout
            take.fps = 60
            take.captured_frames = len(result.frames)
            take.file_id = video.id
            take.poster_file_id = kept.poster.id
            take.contact_sheet_file_id = kept.sheet.id if kept.sheet else None
            take.size_bytes = video.size
            take.finished_at = _now()
            # The session the take ends with replaces the saved one only once it opens the application
            # (its last steps may have signed out).
            if result.storage_state and take.profile_id and result.probe is not None and result.probe.authenticated:
                profile = await db.get(BrowserProfile, take.profile_id)
                if profile is not None:
                    save_storage_state(profile, result.storage_state, probe=result.probe.to_dict())
            await db.commit()
        # Its pacing checkpoint served its purpose: the take is saved.
        await assets.delete_files(kept.pacing_chunks)
        return JobOutcome("done")
    except asyncio.CancelledError:
        if (cancel_event is None or not cancel_event.is_set()) and _worker_stopping():
            # Torn down by its worker's stop (a rollout, a node drain), not by the person: queued again, recorded
            # again from its script by the next worker (finished from its keyframes once they are stored); the adapter
            # hands its execution back, its attempt unspent.
            await asyncio.shield(_queued_after_stop(take_id, TAKE_RESUME_STAGE if resumable else TAKE_STOPPED_STAGE))
            raise
        await asyncio.shield(_ended_on_cancel(take_id, resumable=resumable, cancel_event=cancel_event))
        return JobOutcome("done")
    except encoder.EncoderUnavailableError as exc:
        await jobs.mark("take", take_id, status=JOB_FAILED, error=str(exc), finished_at=_now(), stage=None)
        return JobOutcome("done")
    except Exception as exc:
        logger.warning("Video take %s failed: %s", take_id, format_exception_message(exc), exc_info=True)
        return JobOutcome("retry", f"The recording failed: {format_exception_message(exc)}"[:2000])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


#: What a take its worker's stop tore down says while it waits for the next worker (``_queued_after_stop``).
TAKE_STOPPED_STAGE = "Its worker was stopped: the next worker records it again from its script"
#: The same, for a take stopped after its keyframes were stored: finished from them, never recorded again.
TAKE_RESUME_STAGE = "Its worker was stopped: the next worker finishes it from its keyframes"
#: What a take finished from its keyframes by the next worker says while it runs (``_resumed_take``).
TAKE_RESUMED_STAGE = "Finishing it from its keyframes (its worker was stopped)"
#: Where a take recorded once more after a refusal only its worker's quota starved names it (#11097): ``{worker}``.
STARVED_RETRY_KEY = "starved_retry"
#: What such a take says while it waits for the next claim to record it.
STARVED_RETRY_STAGE = "Recording again: its first take was starved by its worker's CPU quota, not by the page"
#: What its refusal adds when the second take is refused too.
STARVED_RETRY_WORDS = "Recorded twice: its first take was refused as starved by its worker's CPU quota too."
#: What the notes of a take finished from its keyframes say: the relaunch and the stage it resumed at (#9812).
TAKE_RESUMED_NOTE = (
    "Relaunched after its worker was stopped: resumed at its passes and its encode from the keyframes its capture "
    "stored, its steps not recorded again."
)


#: What a take its run was cut on says when nobody cancelled it and its worker was not stopping: its time ran out
#: or its execution was taken from it (a worker stopped earlier, a lease lost) - never "Cancelled.".
TAKE_CUT_ERROR = (
    "Not saved: its run was stopped before the end (its time ran out or its worker was stopped by a rollout or a "
    "drain), and nobody cancelled it."
)


async def _ended_on_cancel(take_id: str, *, resumable: bool = False, cancel_event: asyncio.Event | None = None) -> None:
    """End a take whose run was cancelled while its worker was not stopping.

    Cancelled only when a cancel asked for it (its *cancel_event* set by the cancel watcher, or ``jobs.cancel`` wrote
    ``cancelled`` and who asked on the row first);
    any other cut (the execution's deadline, the engine taking it back after a stopped worker) fails it, saying so and
    how to get it back, so a take is never "Cancelled." without a cancel (#9780)."""
    async with async_session() as db:
        take = await db.get(VideoTake, uuid.UUID(take_id))
        if take is None:
            return
        asked = take.status == JOB_CANCELLED or (cancel_event is not None and cancel_event.is_set())
    if asked:
        await jobs.mark_cancelled("take", take_id)
        return
    stage = "after its capture, while its passes or its encode ran" if resumable else "during its steps"
    again = f" record_browser_demo(from_recording_id='{take_id}') records it again from its script."
    await jobs.mark_if_open(
        "take",
        take_id,
        status=JOB_FAILED,
        error=f"{TAKE_CUT_ERROR} It stopped {stage}.{again}"[:2000],
        finished_at=_now(),
        stage=None,
    )


def _worker_stopping() -> bool:
    """Whether this worker was asked to stop (its stop signal, or its drain): a take torn down then was not cancelled."""
    from app.services.orchestration_reconciler import worker_stopping

    try:
        return worker_stopping()
    except Exception:  # unknown: read as the person's cancel, as before
        return False


async def _queued_after_stop(take_id: str, stage: str = TAKE_STOPPED_STAGE) -> None:
    """A take its worker's stop tore down, queued again: never cancelled, never failed, saying what it waits for.

    One the next worker finishes from its keyframes (``TAKE_RESUME_STAGE``) keeps its progress: what it did is not
    done again, so its progress never goes backwards across relaunches (#10030)."""
    reset = {} if stage == TAKE_RESUME_STAGE else {"progress": 0}
    try:
        await jobs.mark_if_open(
            "take", take_id, status=JOB_PENDING, started_at=None, progress_at=None, stage=stage, **reset
        )
    except Exception as exc:  # its execution is still handed back: the next run starts it again
        logger.warning(
            "Video take %s not queued again at its worker's stop: %s", take_id, format_exception_message(exc)
        )


async def _resume_point_written(take_id: str, keyframes_id: object, point: dict) -> bool:
    """Name on a take still open its stored keyframes and what finishing it from them needs (``capture.keyframes``,
    ``capture.resume``, ``take_repair.resume_point``); whether it was written."""
    try:
        async with async_session() as db:
            take = await db.get(VideoTake, uuid.UUID(take_id))
            if take is None or take.status in JOB_TERMINAL_STATUSES:
                return False
            take.capture = {
                **(take.capture or {}),
                take_repair.CAPTURE_KEY: str(keyframes_id),
                take_repair.RESUME_KEY: point,
            }
            await db.commit()
    except Exception as exc:  # recorded again from its script at a stop, as before
        logger.warning("Video take %s: its resume point not kept: %s", take_id, format_exception_message(exc))
        return False
    return True


async def _resume_point_dropped(take_id: str) -> None:
    """Forget a take's resume point (its keyframes unreadable): the next run records it again from its script."""
    async with async_session() as db:
        take = await db.get(VideoTake, uuid.UUID(take_id))
        if take is None:
            return
        kept = take_repair.keyframes_file(take.capture)
        rest = {
            k: v for k, v in (take.capture or {}).items() if k not in (take_repair.CAPTURE_KEY, take_repair.RESUME_KEY)
        }
        take.capture = rest or None
        await db.commit()
    if kept:
        await assets.delete_files([kept])


#: :func:`_resume_point_dropped` for the take adapter's relaunch after a deadline (#9885).
resume_point_dropped = _resume_point_dropped


async def _resumed_take(take_id: str, resume: dict, *, cancel_event: asyncio.Event | None = None) -> JobOutcome | None:
    """Finish the take *take_id* from the keyframes its stopped worker stored (*resume*, ``take_repair.resume_point``):
    no browser, no capture - its passes and its encode run again on the CPU, as a repair does. ``None``: its keyframes
    could not be read, its resume point dropped, the take to record again from its script."""
    from app.services.video_studio import take_state
    from app.services.video_studio.spec import parse_take_script

    async with async_session() as db:
        take = await db.get(VideoTake, uuid.UUID(take_id))
        if take is None or take.status in JOB_TERMINAL_STATUSES:
            return JobOutcome("done")
        user_id, take_name = take.user_id, take.name
        script = parse_take_script(take.script)
        # What its stopped workers paced already (#10030), bound to the keyframes it is finished from.
        pacing_found = pacing_checkpoint.pacing_point(take.capture, keyframes=resume["keyframes"])
        take.status = JOB_RUNNING
        take.started_at = take.progress_at = _now()
        # Never backwards across relaunches (#10030).
        take.progress = max(take.progress or 0, 80)
        take.stage = TAKE_RESUMED_STAGE
        await db.commit()

    async def progress(value: int, stage: str) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError
        await jobs.mark("take", take_id, progress=value, stage=stage[:200])

    workdir = tempfile.mkdtemp(prefix="xtm-resume-")
    try:
        zip_path = str(Path(workdir) / f"resume-{take_repair.KEYFRAMES_NAME}")
        stored = await assets.download_file(resume["keyframes"], zip_path)
        try:
            source = await asyncio.to_thread(take_repair.unpack_keyframes, zip_path, workdir) if stored else None
        except take_repair.KeyframesError as exc:
            logger.info("Keyframes of take %s not read, recorded again from its script: %s", take_id, exc)
            source = None
        if source is None:
            await _resume_point_dropped(take_id)
            await jobs.mark_if_open("take", take_id, status=JOB_PENDING, started_at=None, progress_at=None, progress=0)
            return None
        events = source.events
        pacing_done, pacing_chunks = await _pacing_restored(
            take_id,
            pacing_found,
            workdir=workdir,
            fps=source.fps,
            pixel_ratio=source.pixel_ratio,
            frames=len(source.frames),
        )
        kept = await _kept_take(
            take_id,
            user_id=user_id,
            workdir=workdir,
            frames=source.frames,
            events=events,
            alone=source.alone,
            painted=source.painted,
            recorded=source.capture,
            duration=source.duration,
            pixel_ratio=source.pixel_ratio,
            viewport=source.viewport,
            fps=source.fps,
            cursor=source.cursor,
            # Its notes say the relaunch and the stage it resumed at (#9812).
            notes=[*list(resume.get("notes") or []), TAKE_RESUMED_NOTE],
            progress=progress,
            stored_keyframes=stored,
            checkpoint=True,
            pacing_done=pacing_done,
            pacing_chunks=pacing_chunks,
        )
        if refused := capture_rate.refusal(kept.captured, resume.get("fps_fallback") or script.fps_fallback, events):
            # Its keyframes kept with its capture: what the refusal says is read off them, as a recorded take's.
            await assets.delete_files(
                [kept.video.id, kept.poster.id, kept.sheet.id if kept.sheet else None, *kept.pacing_chunks]
            )
            await jobs.mark(
                "take",
                take_id,
                status=JOB_FAILED,
                error=capture_rate.error_lines([refused, *kept.notes]),
                finished_at=_now(),
                stage=None,
                capture=kept.captured or None,
                events=list(events or []) or None,
            )
            return JobOutcome("done")
        async with async_session() as db:
            take = await db.get(VideoTake, uuid.UUID(take_id))
            if take is None or take.status == JOB_CANCELLED:
                await assets.delete_files(kept.file_ids())
                return JobOutcome("done")
            take.status = JOB_READY
            take.progress = 100
            take.stage = None
            take.error = "\n".join(kept.notes) or None
            take.events = events
            # The build of this worker, which finished it after its recorder's relaunch, beside the one that
            # recorded it (#11407).
            take.capture = {**kept.captured, take_state.RESUMED_BUILD_KEY: take_state.build_record()}
            ratio = source.pixel_ratio
            if ratio != script.pixel_ratio:
                take.script = {**(take.script or {}), "pixel_ratio": int(ratio) if float(ratio).is_integer() else ratio}
            if (script.viewport.width, script.viewport.height) != source.viewport or script.fps != source.fps:
                width, height = source.viewport
                take.script = {**(take.script or {}), "viewport": {"width": width, "height": height}, "fps": source.fps}
            take.cursor = source.cursor
            take.duration_ms = int(kept.info.duration * 1000)
            take.viewport_width, take.viewport_height = source.viewport
            take.pixel_ratio = ratio
            take.layout = resume.get("layout")
            take.fps = 60
            take.captured_frames = len(source.frames)
            take.file_id = kept.video.id
            take.poster_file_id = kept.poster.id
            take.contact_sheet_file_id = kept.sheet.id if kept.sheet else None
            take.size_bytes = kept.video.size
            take.finished_at = _now()
            await db.commit()
        await assets.delete_files(kept.pacing_chunks)
        logger.info("Video take %s finished from its keyframes after its worker stopped (%s)", take_id, take_name)
        return JobOutcome("done")
    except asyncio.CancelledError:
        if (cancel_event is None or not cancel_event.is_set()) and _worker_stopping():
            # Stopped again: its keyframes are still stored, the next worker finishes it from them.
            await asyncio.shield(_queued_after_stop(take_id, TAKE_RESUME_STAGE))
            raise
        await asyncio.shield(_ended_on_cancel(take_id, resumable=True, cancel_event=cancel_event))
        return JobOutcome("done")
    except encoder.EncoderUnavailableError as exc:
        await jobs.mark("take", take_id, status=JOB_FAILED, error=str(exc), finished_at=_now(), stage=None)
        return JobOutcome("done")
    except Exception as exc:
        logger.warning(
            "Video take %s not finished from its keyframes: %s", take_id, format_exception_message(exc), exc_info=True
        )
        return JobOutcome("retry", f"The recording failed: {format_exception_message(exc)}"[:2000])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@dataclass
class _Kept:
    """What :func:`_kept_take` made of a take's frames: its capture as kept, its notes, its MP4 probed and its files."""

    captured: dict
    notes: list[str]
    info: Any
    video: Any
    poster: Any
    sheet: Any
    keyframes: Any
    #: The chunks of its pacing checkpoint (``pacing_checkpoint``): deleted once the take is saved or dropped.
    pacing_chunks: list = field(default_factory=list)

    def file_ids(self) -> list:
        return [
            self.video.id,
            self.poster.id,
            self.sheet.id if self.sheet else None,
            self.keyframes.id,
            *self.pacing_chunks,
        ]


async def _kept_take(
    take_id: str,
    *,
    user_id: object,
    workdir: str,
    frames: list[tuple[float, str]],
    events: list[dict],
    alone: list[canvas_alone.AloneFrame],
    painted: dict[str, float],
    recorded: dict,
    duration: float,
    pixel_ratio: float,
    viewport: tuple[int, int],
    fps: int,
    cursor: list,
    notes: list[str],
    progress: Any,
    stored_keyframes: Any = None,
    on_packed: Any = None,
    checkpoint: bool = False,
    pacing_done: dict[str, dict] | None = None,
    pacing_chunks: list | None = None,
) -> _Kept:
    """A take's keyframes *frames* (in *workdir*) made into what it keeps, on the CPU: what it was made from packed
    first (``take_repair.pack_keyframes``), its canvases composited from their copies *alone*, its
    scrolls and transitions paced at *fps*, its capture measured, its MP4 encoded, its poster,
    contact sheet and keyframes stored. *events* and *recorded* are rewritten in place as the take keeps them. Shared
    by a recording (:func:`run_take`) and a repair (:func:`run_repair`).

    *stored_keyframes*: the keyframes file already stored for these frames (a take finished from them by the next
    worker, :func:`_resumed_take`), kept as it is. *on_packed*: awaited with the archive's path once packed, before
    any pass; the stored file it answers is kept as the take's keyframes (a recording stores them there, so that a
    worker's stop during its passes or its encode leaves the next worker what to finish it from).

    *checkpoint*: its pacing pass checkpointed every ``pacing_checkpoint.CHUNK_SECONDS`` once its keyframes are stored
    (#10030), *pacing_done* the steps its stopped workers paced already and *pacing_chunks* the files they are in."""
    from app.services.video_studio import capture, insert_floor

    notes = list(notes)
    # What it was made from, before any pass rewrote its events: what a repair re-runs the passes on.
    keyframes_path = str(Path(workdir) / take_repair.KEYFRAMES_NAME)
    if stored_keyframes is None:
        await asyncio.to_thread(
            take_repair.pack_keyframes,
            keyframes_path,
            workdir=workdir,
            frames=frames,
            events=copy.deepcopy(events),
            duration=duration,
            pixel_ratio=pixel_ratio,
            viewport=viewport,
            fps=fps,
            cursor=cursor,
            painted=painted,
            capture=copy.deepcopy(recorded),
            alone=alone,
        )
        if on_packed is not None:
            stored_keyframes = await on_packed(keyframes_path)
    # On software GL and on a GPU: a canvas transition the screencast fell short on, composited from the canvas
    # copied alone - what it is paced, measured and encoded from.
    # The copies its page skipped, every encoder behind, said on the transition or the step they fell in (#10970).
    recorded_alone = recorded.get("alone") if isinstance(recorded.get("alone"), dict) else None
    skips = canvas_alone.kept_skips(recorded_alone)
    composited = await asyncio.to_thread(
        canvas_alone.composite,
        frames,
        alone,
        events,
        fps=fps,
        pixel_ratio=pixel_ratio,
        workdir=workdir,
        offsets=painted,
        skips=skips,
    )
    if recorded_alone is not None:
        recorded_alone["runs"] = composited.runs
        # The instants are placed now: the take keeps where they fell, not each one (its keyframes keep them).
        recorded_alone.pop("skips", None)
        # Kept on every composited take, [] when no copy was skipped outside a run: the record's shape does not
        # depend on the count (#10970, retest 6fde5a86 of take 809edd99 found it absent on a zero-skip take).
        recorded_alone["skipped_outside_runs"] = composited.skipped_outside
    drawn_on = "GPU GL" if (recorded.get("gl") or {}).get("gpu") else "software GL"
    notes.extend(canvas_alone.notes(composited.runs, drawn_on))
    # Its scrolls and transitions drawn again at its rate from the frames the browser painted: a few
    # a second at pixel ratio 2. A scroll that outran the page's paints is said in its notes.
    chunks = list(pacing_chunks or [])
    resuming = f" (resumed from {len(pacing_done)} step(s) paced before)" if pacing_done else ""
    await progress(83, f"Pacing its scrolls and transitions at {fps} fps{resuming}")
    pending = pacing_checkpoint.Pending()
    keeper = None
    if checkpoint and stored_keyframes is not None:
        keeper = asyncio.create_task(
            _pacing_kept(
                take_id,
                pending,
                chunks,
                keyframes_id=stored_keyframes.id,
                keyframe_names=[name for _t, name in frames],
                workdir=workdir,
                fps=fps,
                pixel_ratio=pixel_ratio,
                frames=len(frames),
                user_id=user_id,
            )
        )
    try:
        paced = await asyncio.to_thread(
            scroll_pacing.pace,
            composited.frames,
            events,
            workdir=workdir,
            fps=fps,
            pixel_ratio=pixel_ratio,
            painted={**painted, **composited.painted},
            done=pacing_done,
            on_step=pending.add if keeper is not None else None,
            # A transition over a canvas copied alone follows its copy's page-limited classification (#10970 retest).
            canvas_runs=composited.runs,
        )
    finally:
        if keeper is not None:
            keeper.cancel()
            await asyncio.gather(keeper, return_exceptions=True)
    if keeper is not None:
        # Its last steps stored too: a stop at its measure or its encode does not pace it again.
        await _pacing_chunk_stored(
            take_id,
            pending.take(),
            chunks,
            keyframes_id=stored_keyframes.id,
            keyframe_names=[name for _t, name in frames],
            workdir=workdir,
            fps=fps,
            pixel_ratio=pixel_ratio,
            frames=len(frames),
            user_id=user_id,
        )
    notes.extend(paced.notes)
    if paced.resumed_at is not None:
        said = pacing_checkpoint.resumed_note(paced.resumed_at, len(paced.reused))
        logger.info("Video take %s: %s", take_id, said)
        notes.append(said)
    # A frame of another state spliced into a pause - the page's chrome drawn at another place for a few frames,
    # then back (``static_flicker``) - dropped, the frame before held; the stage that drew it is kept.
    before_flashes = list(paced.frames)
    paced.frames, flashes = await asyncio.to_thread(capture_rate.held_flashes, paced.frames, events, workdir, cursor)
    notes.append(capture_rate.flash_words(flashes))
    # Each frame kept or dropped, with its file name and the test that dropped it: what a window of the take reads.
    frame_listing = take_window.frame_listing(before_flashes, paced.frames, flashes)
    # How fast it captured what its page painted, on the frames it keeps - each canvas on its own,
    # its motion mostly synthesised from keyframes: said at its save, not at a render.
    await progress(83, "Measuring its capture rate and its canvases' transitions")
    captured = await asyncio.to_thread(
        capture_rate.measure,
        paced.frames,
        events,
        duration=duration,
        fps=fps,
        workdir=workdir,
        recorded=recorded,
        viewport_width=viewport[0],
        cursor=cursor,
        painted=composited.frames,
    )
    # Always said, ``[]`` for none: a retest tells a take with no flash from one recorded before the check.
    captured["held_flashes"] = flashes
    # The rate its browser painted at while its page changed, on the frames as painted: what the insert capture floor
    # holds a screen insert's source to - a still page held exactly, never counted against it (``insert_floor``, #10087);
    # the seconds its page asked to move told from those it rested (``painting`` on its events, #10101).
    captured[insert_floor.INSERT_RATE_KEY] = await asyncio.to_thread(
        insert_floor.motion_rate, frames, workdir=workdir, duration=duration, events=events
    )
    # Each span kept at the page's own rate said page-limited, each one its worker's quota starved said so (#10970).
    notes = [*notes, *capture_rate.motion_notes(captured), *capture_rate.page_limited_notes(captured, events)]
    await progress(84, "Encoding the recording")
    mezzanine = str(Path(workdir) / "take.mp4")

    async def encoding(done: int, total: int) -> None:
        share = done / total
        await progress(84 + int(8 * share), f"Encoding the recording ({round(share * 100)} %, {done:,} of {total:,} frames)")

    # Captured in real time at the worker's priority; its encode takes the CPU a whole render leaves.
    await encoder.frames_to_mezzanine(
        paced.frames,
        end_time=duration,
        out_path=mezzanine,
        fps=60,
        crf=14,
        preset="medium",
        workdir=workdir,
        nice=cpu_share.PREVIEW_NICE,
        on_progress=encoding,
    )
    info = await encoder.probe(mezzanine)
    await progress(93, "Saving the recording")
    poster_path = str(Path(workdir) / "poster.jpg")
    await encoder.extract_frame(
        mezzanine,
        at=min(max(info.duration * 0.4, 0.0), max(info.duration - 0.1, 0.0)),
        out_path=poster_path,
        width=1280,
    )
    sheet_path = str(Path(workdir) / "contact-sheet.png")
    capture.contact_sheet(workdir, capture.sheet_frames(frames, events, duration), out_path=sheet_path)
    async with async_session() as db:
        take = await db.get(VideoTake, uuid.UUID(take_id))
        name = (take.name if take else "recording") or "recording"
    base = _slug(name)
    video = await assets.store_path(
        mezzanine, filename=f"{base}.mp4", user_id=user_id, source=assets.SOURCE_TAKE, content_type="video/mp4"
    )
    poster = await assets.store_path(poster_path, filename=f"{base}-poster.jpg", user_id=user_id, source=assets.SOURCE_TAKE)
    sheet = None
    if Path(sheet_path).exists():
        sheet = await assets.store_path(
            sheet_path, filename=f"{base}-contact-sheet.png", user_id=user_id, source=assets.SOURCE_TAKE
        )
    kept_frames = stored_keyframes
    if kept_frames is None:
        kept_frames = await _store_keyframes(keyframes_path, base=base, user_id=user_id)
    captured[take_repair.CAPTURE_KEY] = str(kept_frames.id)
    if listed := await _store_frame_list(frame_listing, base=base, user_id=user_id):
        captured[take_window.FRAME_LIST_KEY] = listed
    return _Kept(captured, notes, info, video, poster, sheet, kept_frames, chunks)


async def _pacing_kept(take_id: str, pending: pacing_checkpoint.Pending, chunks: list, **where: Any) -> None:
    """While a take's pacing pass runs, store the steps it paced as a chunk every ``CHUNK_SECONDS`` (#10030)."""
    while True:
        await asyncio.sleep(pacing_checkpoint.CHUNK_SECONDS)
        await _pacing_chunk_stored(take_id, pending.take(), chunks, **where)


async def _pacing_chunk_stored(
    take_id: str,
    steps: dict[str, dict],
    chunks: list,
    *,
    keyframes_id: object,
    keyframe_names: list[str],
    workdir: str,
    fps: int,
    pixel_ratio: float,
    frames: int,
    user_id: object,
) -> None:
    """*steps* packed with the frames they drew, stored as a take's file and named on the take (``capture.pacing``),
    *chunks* growing with it. Best effort: a checkpoint not kept paces those steps again at the next relaunch."""
    if not steps:
        return
    path = str(Path(workdir) / f"pacing-{len(chunks):03d}.zip")
    try:
        await asyncio.to_thread(pacing_checkpoint.pack_chunk, path, steps, workdir=workdir, keyframes=keyframe_names)
        stored = await assets.store_path(
            path,
            filename=f"pacing-{take_id}-{len(chunks):03d}.zip",
            user_id=user_id,
            source=assets.SOURCE_TAKE,
            content_type="application/zip",
        )
        chunks.append(stored.id)
        point = pacing_checkpoint.point(
            keyframes=keyframes_id, fps=fps, pixel_ratio=pixel_ratio, frames=frames, chunks=chunks
        )
        async with async_session() as db:
            take = await db.get(VideoTake, uuid.UUID(take_id))
            if take is None or take.status in JOB_TERMINAL_STATUSES:
                return
            take.capture = {**(take.capture or {}), pacing_checkpoint.CAPTURE_KEY: point}
            await db.commit()
    except Exception as exc:
        logger.warning("Video take %s: a pacing checkpoint not kept: %s", take_id, format_exception_message(exc))
    finally:
        Path(path).unlink(missing_ok=True)


async def _pacing_restored(
    take_id: str, found: dict | None, *, workdir: str, fps: int, pixel_ratio: float, frames: int
) -> tuple[dict[str, dict] | None, list]:
    """The steps the chunks *found* name (``capture.pacing``) hold, their frames written to *workdir*, and the chunks
    read; ``(None, [])`` when the take has none for these keyframes, this rate and this ratio."""
    if not pacing_checkpoint.matches(found, fps=fps, pixel_ratio=pixel_ratio, frames=frames):
        return None, []
    done: dict[str, dict] = {}
    chunks: list = []
    for n, chunk_id in enumerate(found["chunks"]):
        path = str(Path(workdir) / f"restored-pacing-{n:03d}.zip")
        try:
            stored = await assets.download_file(chunk_id, path)
            steps = await asyncio.to_thread(pacing_checkpoint.unpack_chunk, path, workdir) if stored else {}
        except Exception as exc:
            logger.info("Video take %s: pacing chunk %s not read: %s", take_id, chunk_id, format_exception_message(exc))
            steps = {}
        finally:
            Path(path).unlink(missing_ok=True)
        if not steps:
            # A chunk lost: the steps after it are paced again, never reused out of order.
            break
        done.update(steps)
        chunks.append(chunk_id)
    return (done or None), chunks


async def _store_frame_list(listing: dict, *, base: str, user_id: object) -> str | None:
    """A take's frame list (``take_window.frame_listing``) stored as a file it owns; ``None`` when not stored."""
    try:
        stored = await assets.store_bytes(
            json.dumps(listing, separators=(",", ":")).encode(),
            filename=f"{base}-frames.json",
            user_id=user_id,
            source=assets.SOURCE_TAKE,
            content_type="application/json",
        )
    except Exception as exc:  # the take is kept without it: a window then says its frames were not listed
        logger.info("Frame list of a take not stored: %s", format_exception_message(exc))
        return None
    return str(stored.id)


async def _store_keyframes(path: str, *, base: str, user_id: object) -> Any:
    """The keyframes archive at *path* stored as a take's file (``take_repair.KEYFRAMES_NAME``)."""
    return await assets.store_path(
        path,
        filename=f"{base}-{take_repair.KEYFRAMES_NAME}",
        user_id=user_id,
        source=assets.SOURCE_TAKE,
        content_type="application/zip",
    )


async def run_repair(take_id: str, *, cancel_event: asyncio.Event | None = None) -> JobOutcome:
    """Make the take *take_id* (queued by ``get_video_recording(repair=true)``, ``capture.repair_of``) by repairing a
    finished take (``take_repair``): no browser, no recording, on the CPU like any take. From its keyframes when
    it kept them (exact), else from its MP4 (its painted frames read back, its off-scale runs dropped, the frame before
    holding); then today's passes run again (:func:`_kept_take`). The take repaired is kept; the new one says which
    stretches it repaired and what it could not (``capture.repair``: the residual, to record again)."""
    async with async_session() as db:
        take = await db.get(VideoTake, uuid.UUID(take_id))
        if take is None or take.status in JOB_TERMINAL_STATUSES:
            return JobOutcome("done")
        source_id = take_repair.repair_source(take.capture)
        source = await db.get(VideoTake, uuid.UUID(source_id)) if source_id else None
        user_id = take.user_id
        take.status = JOB_RUNNING
        take.started_at = take.progress_at = _now()
        take.progress = 2
        take.stage = "Reading the recording it repairs"
        await db.commit()
    if source is None or source.status != JOB_READY or source.file_id is None:
        await jobs.mark(
            "take",
            take_id,
            status=JOB_FAILED,
            error=f"The recording `{source_id}` it repairs is gone or not ready: record it again.",
            finished_at=_now(),
            stage=None,
        )
        return JobOutcome("done")

    async def progress(value: int, stage: str) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError
        await jobs.mark("take", take_id, progress=value, stage=stage[:200])

    workdir = tempfile.mkdtemp(prefix="xtm-repair-")
    try:
        route = take_repair.FROM_MP4
        faults: list[tuple[float, float]] = []
        kept_id = take_repair.keyframes_file(source.capture)
        unpacked = None
        if kept_id:
            zip_path = str(Path(workdir) / f"source-{take_repair.KEYFRAMES_NAME}")
            if await assets.download_file(kept_id, zip_path) is not None:
                try:
                    unpacked = await asyncio.to_thread(take_repair.unpack_keyframes, zip_path, workdir)
                except take_repair.KeyframesError as exc:
                    logger.info("Keyframes of take %s not read, repaired from its MP4: %s", source.id, exc)
        if unpacked is not None:
            route = take_repair.FROM_KEYFRAMES
            # Its off-scale runs dropped as the MP4 route drops them: the frame before holds.
            faults = take_repair.fault_spans(source.capture)
            frames = take_repair.drop_faults(unpacked.frames, faults)
            events, alone = unpacked.events, unpacked.alone
            painted, recorded, cursor = unpacked.painted, unpacked.capture, unpacked.cursor
            duration, pixel_ratio, viewport, fps = unpacked.duration, unpacked.pixel_ratio, unpacked.viewport, unpacked.fps
        else:
            mp4 = str(Path(workdir) / "source.mp4")
            if await assets.download_file(source.file_id, mp4) is None:
                await jobs.mark(
                    "take",
                    take_id,
                    status=JOB_FAILED,
                    error=f"The MP4 of recording `{source.id}` is gone: record it again.",
                    finished_at=_now(),
                    stage=None,
                )
                return JobOutcome("done")
            await progress(10, "Reading the frames its page painted off its MP4")
            info = await encoder.probe(mp4)
            try:
                frames = await take_repair.painted_from_mp4(
                    mp4,
                    workdir,
                    fps=info.fps or 60.0,
                    size=(info.width, info.height),
                    events=list(source.events or []),
                    capture=source.capture,
                )
            except encoder.EncoderUnavailableError:
                raise
            except encoder.EncoderError as exc:
                # One short reason, never ffmpeg's log, and no second attempt that fails the same way.
                logger.warning("Frames of take %s not read off its MP4: %s", source.id, exc)
                await jobs.mark(
                    "take",
                    take_id,
                    status=JOB_FAILED,
                    error=take_repair.extraction_failure(exc),
                    finished_at=_now(),
                    stage=None,
                )
                return JobOutcome("done")
            if not frames:
                await jobs.mark(
                    "take",
                    take_id,
                    status=JOB_FAILED,
                    error=f"No frame of recording `{source.id}` could be read off its MP4: record it again.",
                    finished_at=_now(),
                    stage=None,
                )
                return JobOutcome("done")
            route = take_repair.FROM_MP4
            events = take_repair.raw_events(list(source.events or []))
            alone, painted, recorded = [], {}, take_repair.recorded_summary(source.capture)
            cursor = list(source.cursor or [])
            duration, pixel_ratio = info.duration, float(source.pixel_ratio or 1.0)
            viewport = (source.viewport_width or info.width, source.viewport_height or info.height)
            fps = int((source.script or {}).get("fps") or 30)
            faults = take_repair.fault_spans(source.capture)
        await progress(80, f"Repairing it from its {'keyframes' if route == take_repair.FROM_KEYFRAMES else 'MP4'}")
        kept = await _kept_take(
            take_id,
            user_id=user_id,
            workdir=workdir,
            frames=frames,
            events=events,
            alone=alone,
            painted=painted,
            recorded=recorded,
            duration=duration,
            pixel_ratio=pixel_ratio,
            viewport=viewport,
            fps=fps,
            cursor=cursor,
            notes=[],
            progress=progress,
        )
        # A page settling on a step that holds it is nothing to record again: never a residual.
        # Keyframes an earlier repair read off an MP4 are no exact source: said, at any depth of the chain.
        origin = take_repair.repair_origin(route, source.capture)
        # Read at the rate the repaired take is kept at (60 fps), as the save check reads it (#10892).
        left = take_repair.residual(
            route,
            recorded=source.capture,
            notes=capture_rate.motion_notes(kept.captured, settling=False),
            origin=origin,
            events=events,
            fps=60,
        )
        kept.captured[take_repair.REPAIRED_KEY] = {
            "of": str(source.id),
            "from": route,
            "origin": origin,
            "spans": take_repair.repaired(events, faults),
            "residual": left,
        }
        async with async_session() as db:
            take = await db.get(VideoTake, uuid.UUID(take_id))
            if take is None or take.status == JOB_CANCELLED:
                await assets.delete_files(kept.file_ids())
                return JobOutcome("done")
            take.status = JOB_READY
            take.progress = 100
            take.stage = None
            take.error = "\n".join(kept.notes) or None
            take.events = events
            take.capture = kept.captured
            take.cursor = cursor
            take.duration_ms = int(kept.info.duration * 1000)
            take.viewport_width, take.viewport_height = viewport
            take.pixel_ratio = pixel_ratio
            take.layout = source.layout
            take.fps = 60
            # What its page painted, not the frames chosen off its MP4 (a hold is one of them there).
            take.captured_frames = take_repair.painted_count(
                route, recorded_frames=source.captured_frames, kept=len(frames)
            )
            take.file_id = kept.video.id
            take.poster_file_id = kept.poster.id
            take.contact_sheet_file_id = kept.sheet.id if kept.sheet else None
            take.size_bytes = kept.video.size
            take.finished_at = _now()
            await db.commit()
        return JobOutcome("done")
    except asyncio.CancelledError:
        await jobs.mark("take", take_id, status=JOB_CANCELLED, error="Cancelled.", finished_at=_now(), stage=None)
        return JobOutcome("done")
    except encoder.EncoderUnavailableError as exc:
        await jobs.mark("take", take_id, status=JOB_FAILED, error=str(exc), finished_at=_now(), stage=None)
        return JobOutcome("done")
    except Exception as exc:
        logger.warning("Video take repair %s failed: %s", take_id, format_exception_message(exc), exc_info=True)
        return JobOutcome("retry", f"The repair failed: {format_exception_message(exc)}"[:2000])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _failure_sheet(failed: Any, workdir: str, *, user_id: object, name: str | None) -> uuid.UUID | None:
    """The contact sheet of a take a step failed, stored (``capture.failure_sheet_frames``): its last frame the
    one where the step failed. ``None`` without a frame."""
    from app.services.video_studio import capture

    picks = capture.failure_sheet_frames(failed)
    if not picks:
        return None
    path = str(Path(workdir) / "contact-sheet.png")
    try:
        await asyncio.to_thread(capture.contact_sheet, workdir, picks, out_path=path)
        sheet = await assets.store_path(
            path, filename=f"{_slug(name or 'recording')}-contact-sheet.png", user_id=user_id, source=assets.SOURCE_TAKE
        )
    except Exception as exc:  # the failure is said without its frames
        logger.info("Contact sheet of a failed take not kept: %s", format_exception_message(exc))
        return None
    return sheet.id


def _slug(text: str) -> str:
    import re

    slug = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-").lower()
    return (slug or "video")[:80]


# ── AI clip ───────────────────────────────────────────────────────────────


async def _silenced(path: str, workdir: str) -> list[str]:
    """Drop, in place, the sound track of a shot asked silent; the notes saying what was done."""
    try:
        if not (await encoder.probe(path)).has_audio:
            return []
        silent = str(Path(workdir) / "clip-silent.mp4")
        await encoder.without_sound(path, silent)
    except encoder.EncoderError as exc:
        return [f"The shot was asked silent, but its sound track could not be removed: {exc}"]
    shutil.move(silent, path)
    return ["The shot was asked silent: the sound track the model made anyway was removed."]


async def _keep_frames(
    clip_id: str,
    path: str,
    duration: float,
    workdir: str,
    picture: Any,
    *,
    user_id: object,
    base: str,
    params: dict,
    label: str = "",
) -> dict:
    """The shot's first, middle and last frames stored as its files, and measured (``frame_qc``).

    Advisory: frames that cannot be cut leave the shot without them (logged),
    never failed. The frames a run that died had stored are deleted first.
    *params* gets them (``frame_qc.QC_KEY``), written on the row at once.
    Each file says what it is (``frame_qc.caption``), of the shot *label*.
    How the take was stabilised (``frame_qc.STABILISATION``, written
    there with the stabilised take) stays with them, and a stabilised take's
    first frame is compared with the part of its start picture it kept.
    """
    from app.services.video_studio import frame_qc, stabilise

    carried = {
        key: value
        for key, value in (params.get(frame_qc.QC_KEY) or {}).items()
        if key in frame_qc.CARRIED and isinstance(value, dict)
    }
    record = carried.get(frame_qc.STABILISATION)
    await assets.delete_files(frame_qc.cut_file_ids(params))
    params.pop(frame_qc.QC_KEY, None)
    stored: list = []
    try:
        cut = await frame_qc.cut_frames(path, duration, workdir)
        frames = {name: frame_qc.open_picture(Path(file).read_bytes()) for name, file in cut.items()}
        first, last = frames["first_frame_file_id"], frames["last_frame_file_id"]
        qc: dict[str, Any] = {"motion_ssim": frame_qc.ssim(first, last), **carried}
        qc[frame_qc.FRAME_CHANGE] = frame_qc.frame_change(qc)
        qc.update(await _camera_measured(clip_id, path, record, params))
        if picture is not None:
            start = frame_qc.open_picture(picture.data)
            if isinstance(record, dict) and record.get("applied") and record.get("crop"):
                start = stabilise.cropped(start, record["crop"])
            qc["reference_ssim"] = frame_qc.ssim(first, start)
        for name, file in cut.items():
            row = await assets.store_path(
                str(file),
                filename=f"{base}-{name.split('_')[0]}.jpg",
                user_id=user_id,
                source=assets.SOURCE_CLIP,
                description=frame_qc.caption(name, qc, shot=label or base, size=frames[name].size, duration=duration),
            )
            stored.append(row.id)
            qc[name] = str(row.id)
    except Exception as exc:
        logger.warning("Video clip %s: its frames were not kept: %s", clip_id, format_exception_message(exc))
        await assets.delete_files(stored)
        # The un-stabilised take and the logo check stay the take's: kept with a record of their own until frames are.
        if carried:
            params[frame_qc.QC_KEY] = carried
        return {}
    params[frame_qc.QC_KEY] = qc
    await jobs.mark("clip", clip_id, params=params)
    return qc


async def _camera_measured(clip_id: str, path: str, record: Any, params: dict) -> dict:
    """The take's camera motion measured on its background, as its frames keep it; empty when not measured.

    Read off its stabilisation record when it was stabilised, else measured
    for a shot held to a camera: given ``max_camera_motion`` or asked
    locked off. Advisory: a take whose camera cannot be followed is judged on
    its whole frame's change, as before, and says so in its failure.
    """
    from app.services.video_studio import content_drift, frame_qc, stabilise

    held = params.get(frame_qc.MAX_CAMERA_MOTION) is not None or content_drift.kept_camera(params) == content_drift.CAMERA_LOCKED
    try:
        motion, _why = await stabilise.camera_of(path, record if isinstance(record, dict) else None, measure=held)
    except Exception as exc:  # advisory, like the frames
        logger.warning("Video clip %s: its camera was not measured: %s", clip_id, format_exception_message(exc))
        return {}
    return {frame_qc.CAMERA_MOTION: round(motion, 4)} if motion is not None else {}


async def _reference_drifts(held: list[dict], path: str, duration: float, workdir: str) -> list:
    """The take at *path* read against the video's references *held* (``reference_drift.drifts_from_frames``):
    its first, middle and last frames cut again, each reference's picture read from its file."""
    from app.services.video_studio import clips, frame_qc, reference_drift

    folder = Path(workdir) / "consistency"
    folder.mkdir(parents=True, exist_ok=True)
    cut = await frame_qc.cut_frames(path, duration, str(folder))
    frames = reference_drift.frames_from_paths(cut)
    pictures: dict[str, Any] = {}
    for index, reference in enumerate(held):
        target = folder / f"reference-{index}"
        if await assets.download_file(reference["file_id"], str(target)) is None:
            raise clips.ClipError(f"the picture of reference `{reference.get('id')}` is gone")
        pictures[str(reference.get("id"))] = frame_qc.open_picture(target.read_bytes())
    return await asyncio.to_thread(reference_drift.drifts_from_frames, held, frames, pictures)


def _decoded_picture(data: bytes):
    import io

    from PIL import Image

    with Image.open(io.BytesIO(data)) as opened:
        opened.load()
        return opened.convert("RGBA")


async def _picture_file(file_id: object, workdir: str):
    """A picture a shot draws in (a screenshot), decoded; ``clips.ClipError`` when gone."""
    from app.services.video_studio import clips

    shot_path = Path(workdir) / f"picture-{file_id}"
    row = await assets.download_file(file_id, str(shot_path))
    if row is None:
        raise clips.ClipError("The screenshot drawn into the screen is gone: generate the shot again with another one.")
    picture = await clips.start_picture(shot_path.read_bytes(), row.content_type)
    return await asyncio.to_thread(_decoded_picture, picture.data)


async def _screenshot_of(screen, workdir: str):
    """The screenshot a screen replacement draws (``screens.py``), decoded; ``clips.ClipError`` when gone."""
    return await _picture_file(screen.file_id, workdir)


async def _screen_replaced(path: str, screen, image, stored: dict, workdir: str):
    """The real screenshot *image* drawn, in place, into the flat screen of the shot at *path* (``screens.replace``)."""
    from app.services.video_studio import screens

    out = str(Path(workdir) / "clip-screen.mp4")
    drawn = await screens.replace(path, out, image, screen, start_size=tuple(stored["start_size"]))
    shutil.move(out, path)
    return drawn


async def _regions_kept(path: str, still, boxes: list, workdir: str):
    """*still*'s *boxes* drawn back, in place, into every frame of the shot at *path* (``kept_regions.keep``)."""
    from app.services.video_studio import kept_regions

    out = str(Path(workdir) / "clip-kept.mp4")
    kept = await kept_regions.keep(path, out, still, boxes)
    shutil.move(out, path)
    return kept


async def _screens_cleaned(path: str, sightings: list, option: dict, screenshot, workdir: str):
    """The stray screens of the shot at *path* cleaned, in place, in every frame (``stray_screens.clean``)."""
    from app.services.video_studio import stray_screens

    out = str(Path(workdir) / "clip-cleaned.mp4")
    cleaned = await stray_screens.clean(path, out, sightings, option, screenshot)
    shutil.move(out, path)
    return cleaned


#: The params key logging what was drawn into the take after the provider made it: the screen, the kept
#: regions, the logo, the stabilisation, the cleaned screens, each with the file it made, so a run that dies after
#: a step resumes past it.
_TAKE_STEPS = "take_steps"


def _steps_done(params: dict, stored_file: object) -> set[str]:
    """The steps already drawn into the stored take *stored_file*: those logged up to the one that made it."""
    steps = [item for item in params.get(_TAKE_STEPS) or [] if isinstance(item, dict)]
    made = [index for index, item in enumerate(steps) if stored_file and item.get("file_id") == str(stored_file)]
    return {str(item.get("step")) for item in steps[: made[-1] + 1]} if made else set()


async def _logos_composited(
    path: str, asked: list, start: Any, params: dict, workdir: str, *, user_id: object, conversation_id: object
):
    """The shot's logo ingredients drawn back onto the take at *path*, in place (``logos.composite``).

    *start* is the start frame the take was asked from (read again when the
    run resumed a job, ``None``); a logo picture gone fails the shot
    (``clips.ClipError``), a start frame that cannot be read leaves the logos
    named in the prompt only, with a note.
    """
    from app.services.video_studio import clips, logo_integration, logos, screen_insert

    if start is None:
        try:
            start = await clips.start_frame(params, workdir, user_id=user_id, conversation_id=conversation_id)
        except Exception as exc:  # the prompt path still named the logo: it stays named only
            return logos.Composited(0, [], [f"The logo was named in the prompt only: its start frame could not be read ({exc})."])
    if start is None:
        return logos.Composited(0, [], ["The logo was named in the prompt only: the shot has no start frame to find it in."])
    pictures = []
    for index, item in enumerate(asked):
        picture = await _ingredient_picture(item.file_id, item.name, workdir, f"logo-{index}")
        pictures.append(logos.Logo(picture, item.name, item.quad))
    out = str(Path(workdir) / "clip-logos.mp4")
    done = await logos.composite(
        path,
        out,
        pictures,
        await asyncio.to_thread(screen_insert.decode, start.data),
        flat=logo_integration.flat_accepted(params),
    )
    if done.drawn:
        shutil.move(out, path)
    return done


async def _ingredient_picture(file_id: object, name: str, workdir: str, stem: str):
    """An ingredient's picture (a logo, its subject), decoded RGBA; ``clips.ClipError`` when it is gone."""
    from app.services.video_studio import clips

    target = Path(workdir) / stem
    row = await assets.download_file(file_id, str(target))
    if row is None:
        raise clips.ClipError(f"The picture of {name} is gone: generate the shot again with another one.")
    picture = await clips.start_picture(target.read_bytes(), row.content_type)
    return await asyncio.to_thread(_decoded_picture, picture.data)


async def _tracked_items(asked: list, workdir: str) -> list:
    """The tracked logos of a shot as ``tracked_logos.composite`` draws them: each picture with its subject's."""
    from app.services.video_studio import tracked_logos

    items = []
    for index, item in enumerate(asked):
        picture = await _ingredient_picture(item.file_id, item.name, workdir, f"logo-{index}")
        subject = await _ingredient_picture(item.subject.file_id, f"the subject of {item.name}", workdir, f"subject-{index}")
        items.append(tracked_logos.TrackedLogo(picture, item.name, tracked_logos.Subject(subject, item.subject.label)))
    return items


async def _native_logo_check(path: str, asked: list, workdir: str):
    """The native take at *path* checked for the logos it was sent as given (``tracked_logos.native_check``)."""
    from app.services.video_studio import tracked_logos

    marks = [(item.picture, item.label) for item in await _tracked_items(asked, workdir)]
    return await tracked_logos.native_check(path, marks)


async def _logos_tracked(
    path: str, asked: list, workdir: str, *, user_id: object, fell_back_from: str | None = None, flat: bool = False
):
    """The shot's logos drawn onto their subject wherever the take at *path* shows it, in place (``tracked_logos``).

    A picture gone fails the shot (``clips.ClipError``); the subject not
    found or its surface lost fail the take
    (``tracked_logos.IngredientLostError``); the detector giving no answer
    goes to the job's retry (``DetectionUnavailableError``).
    """
    from app.services.video_studio import tracked_logos

    items = await _tracked_items(asked, workdir)
    out = str(Path(workdir) / "clip-tracked.mp4")
    done = await tracked_logos.composite(path, out, items, tracked_logos.detector(user_id), flat=flat)
    done.fell_back_from = fell_back_from
    shutil.move(out, path)
    return done


async def _record_logo_check(composited: Any, params: dict, *, base: str, user_id: object) -> None:
    """The composited take's duplicate logo check kept with its frames (``frame_qc.LOGO_CHECK``).

    A duplicated logo's frame is stored as a file of the take (deleted with
    it), named by the record: every render playing the take fails its scene
    with it (``logos.duplicate_findings``). Storing the frame is best effort:
    the record stands without it.
    """
    from app.services.video_studio import frame_qc

    if composited.check is None:
        return
    record = dict(composited.check)
    if record.get("integration") is not None:
        record["integration"] = await _integration_kept(composited.integration, record["integration"], base=base, user_id=user_id)
    shown = composited.duplicate.frame_jpeg if composited.duplicate is not None else None
    if shown:
        try:
            row = await assets.store_bytes(
                shown,
                filename=f"{base}-logo-duplicated.jpg",
                user_id=user_id,
                source=assets.SOURCE_CLIP,
                content_type="image/jpeg",
                description=f"The frame where the AI shot shows its logo twice: {composited.duplicate.reason()}"[:1000],
            )
            record["frame_file_id"] = str(row.id)
        except Exception as exc:  # the record says it, with or without its frame
            logger.warning("The frame of a duplicated logo was not kept: %s", format_exception_message(exc))
    params[frame_qc.QC_KEY] = {**(params.get(frame_qc.QC_KEY) or {}), frame_qc.LOGO_CHECK: record}


async def _integration_kept(integration: Any, record: dict | None, *, base: str, user_id: object) -> dict | None:
    """*record* (``logo_integration.Integration.record``) with the frame a logo was found not integrated in stored as a
    file of the take (``frame_file_id``, deleted with it): every render playing it and the video's quality check name
    it. Storing the frame is best effort: the record stands without it."""
    if record is None or integration is None or integration.passed or not integration.frame_jpeg:
        return record
    try:
        row = await assets.store_bytes(
            integration.frame_jpeg,
            filename=f"{base}-logo-not-integrated.jpg",
            user_id=user_id,
            source=assets.SOURCE_CLIP,
            content_type="image/jpeg",
            description=f"The frame where the AI shot's logo reads as a flat sticker: {integration.reason()}"[:1000],
        )
    except Exception as exc:  # the record says it, with or without its frame
        logger.warning("The frame of a logo not integrated was not kept: %s", format_exception_message(exc))
        return record
    return {**record, "frame_file_id": str(row.id)}


async def _lost_logo_reason(error: Any, params: dict, *, base: str, user_id: object, what: str = "its logo") -> str:
    """A take's ``logo lost`` failure, the frame it was lost in stored with the take's frames and named.

    The tracked path's failures - the subject not found, its surface lost -
    carry their frame the same way (*what* names what was lost).
    Storing the frame is best effort: a take whose frame cannot be kept still
    fails, its reason said without it.
    """
    from app.services.video_studio import frame_qc

    reason = str(error)
    shown = getattr(error, "frame_jpeg", None)
    if not shown:
        return reason
    try:
        row = await assets.store_bytes(
            shown,
            filename=f"{base}-logo-lost.jpg",
            user_id=user_id,
            source=assets.SOURCE_CLIP,
            content_type="image/jpeg",
            description=f"The frame of the AI shot where {what} was lost: {error}"[:1000],
        )
    except Exception as exc:  # the failure stands without its frame
        logger.warning("The frame of a lost logo was not stored: %s", format_exception_message(exc))
        return reason
    params[frame_qc.QC_KEY] = {**(params.get(frame_qc.QC_KEY) or {}), frame_qc.LOST_FRAME: str(row.id)}
    return f"{reason} (frame: file `{row.id}`)"


def _crop_word(frame_path: str, box: tuple[float, float, float, float]) -> bytes:
    """The crop of a word at *box* (shares) out of the frame at *frame_path* (``text_drift.crop_word``)."""
    from app.services.video_studio import text_drift

    return text_drift.crop_word(frame_path, box)


async def _record_text_check(verdict: Any, params: dict, *, base: str, user_id: object) -> str | None:
    """The take's text check kept with its frames (``frame_qc.TEXT_CHECK``), a failed word's crop stored as a file.

    The crop is cut from the frame the word was read in, at the box the
    reading gave; storing it is best effort, the record stands without it.
    Answers the crop's file id, to name in the failure.
    """
    from app.services.video_studio import frame_qc

    record = verdict.record()
    crop_id: str | None = None
    failure = verdict.failure
    frame_path = verdict.frame_paths.get(failure.frame) if failure is not None else None
    if failure is not None and failure.box is not None and frame_path and Path(frame_path).exists():
        try:
            data = await asyncio.to_thread(_crop_word, frame_path, failure.box)
            row = await assets.store_bytes(
                data,
                filename=f"{base}-text-{failure.frame.split('_')[0]}.jpg",
                user_id=user_id,
                source=assets.SOURCE_CLIP,
                content_type="image/jpeg",
                description=(
                    f'The crop of the AI shot\'s {failure.frame.replace("_", " ")} where "{failure.expected}" of the '
                    f'start picture reads "{failure.seen}": the word the text check failed on.'
                )[:1000],
            )
            crop_id = str(row.id)
            record["failure"] = {**(record.get("failure") or {}), "crop_file_id": crop_id}
        except Exception as exc:  # the record says the word and the frame without its crop
            logger.warning("The crop of a misdrawn word was not stored: %s", format_exception_message(exc))
    # Each invented word is cropped too (dev:invented-text-check): the video's quality check names the crop.
    for index, item in enumerate(getattr(verdict, "invented", None) or []):
        frame_path = verdict.frame_paths.get(item.frame)
        if item.box is None or not frame_path or not Path(frame_path).exists():
            continue
        try:
            data = await asyncio.to_thread(_crop_word, frame_path, item.box)
            row = await assets.store_bytes(
                data,
                filename=f"{base}-invented-{item.frame.split('_')[0]}-{index}.jpg",
                user_id=user_id,
                source=assets.SOURCE_CLIP,
                content_type="image/jpeg",
                description=(
                    f'The crop of the AI shot\'s {item.frame.replace("_", " ")} reading "{item.word}": lettering the '
                    "take invented, no word of the start picture."
                )[:1000],
            )
            for kept in record.get("invented") or []:
                if kept.get("word") == item.word and kept.get("frame") == item.frame and not kept.get("crop_file_id"):
                    kept["crop_file_id"] = str(row.id)
                    break
        except Exception as exc:  # the record says the word and the frame without its crop
            logger.warning("The crop of an invented word was not stored: %s", format_exception_message(exc))
    # Each unreadable place the failure counts is cropped too (#10919): the author sees what to blank or keep.
    kept_places = record.get("places") or []
    for index, place in enumerate(getattr(verdict, "places", None) or []):
        frame_path = verdict.frame_paths.get(place.frame)
        if place.box is None or not frame_path or not Path(frame_path).exists() or index >= len(kept_places):
            continue
        try:
            data = await asyncio.to_thread(_crop_word, frame_path, place.box)
            row = await assets.store_bytes(
                data,
                filename=f"{base}-unreadable-{place.frame.split('_')[0]}-{index}.jpg",
                user_id=user_id,
                source=assets.SOURCE_CLIP,
                content_type="image/jpeg",
                description=(
                    f"The crop of the AI shot's {place.frame.replace('_', ' ')} at {place.where}: text drawn "
                    f"unreadable ({place.origin})."
                )[:1000],
            )
            kept_places[index]["crop_file_id"] = str(row.id)
        except Exception as exc:  # the record says the place and its box without its crop
            logger.warning("The crop of an unreadable place was not stored: %s", format_exception_message(exc))
    params[frame_qc.QC_KEY] = {**(params.get(frame_qc.QC_KEY) or {}), frame_qc.TEXT_CHECK: record}
    return crop_id


async def _insert_promotion_findings(user_id: Any, clip_id: str, file_id: str) -> list[str]:
    """What the pre-render lint of each video drawing a screen insert on the shot *clip_id* would refuse were its
    new take *file_id* to play (``insert_bezels.promotion_findings``): a regeneration in place failing it keeps
    its previous take playing, the findings in its failed attempt's reason (issue #10901). Never raises."""
    from app.models.user import User
    from app.services.video_studio import insert_bezels

    try:
        async with async_session() as db:
            user = await db.get(User, user_id)
            return await insert_bezels.promotion_findings(db, user, clip_id, file_id)
    except Exception as exc:
        logger.info(
            "Screen inserts on shot %s not linted before its promotion: %s", clip_id, format_exception_message(exc)
        )
        return []


async def _fall_back(clip_id: str, params: dict, reason: str, *, made: dict, label: str) -> JobOutcome | None:
    """Generate the shot again in place with its next fallback model, its take having failed *reason*.

    In one transaction on the locked row: the failed take ends as any failed
    attempt does (``clips.end_attempt``: its media on the row to look at, or
    beside the take a ready shot keeps), then ``clips.restart_clip`` asks the
    next model of ``fallback_models`` - the shot as it was asked, the chain of
    models tried kept (``FALLBACK_TRIED``) - so no one ever reads the shot as
    failed in between. ``None`` when there is no model left to try, or the
    next one cannot take the shot (said in the notes): the take then fails.
    *params* is not changed but for the chain and those notes.
    """
    import copy

    from app.models.user import User
    from app.services.video_studio import clips

    async with async_session() as db:
        clip = await db.get(VideoClip, uuid.UUID(clip_id), with_for_update=True)
        if clip is None or clip.status != JOB_RUNNING:
            return None
        tried = [
            *(params.get(clips.FALLBACK_TRIED) or []),
            {"model": clip.model, "provider": label, "reason": reason[:600]},
        ]
        following = clips.next_fallback({**params, clips.FALLBACK_TRIED: tried}, clip.model)
        user = await db.get(User, clip.user_id) if following is not None and clip.user_id else None
        if following is None or user is None:
            params[clips.FALLBACK_TRIED] = tried
            return None
        attempt = copy.deepcopy(params)
        for key, value in clips.end_attempt(attempt, status=JOB_FAILED, reason=reason, made=made).items():
            setattr(clip, key, value)
        clip.params = attempt
        clip.finished_at = _now()
        await db.flush()
        try:
            await clips.restart_clip(
                db,
                user,
                clip,
                owed_to=clip.owed_conversation_id,
                tried=tried,
                model=following["model"],
                provider_id=following.get("provider_id"),
            )
        except clips.ClipError as exc:
            await db.rollback()
            params[clips.FALLBACK_TRIED] = tried
            params.setdefault("notes", []).append(f"Not generated again with {following['model']}: {exc}")
            return None
        if not clip.stage:
            # A provider that cools says why the shot waits instead (``provider_cooling.hold``).
            clip.stage = f"Generating again with {following['model']}: the take of {tried[-1]['model']} failed its checks"[:200]
        await db.commit()
    await jobs.wake()
    return JobOutcome("done")


async def run_clip(clip_id: str, *, cancel_event: asyncio.Event | None = None) -> JobOutcome:
    """Make an AI shot - and never pay for it twice.

    The engine runs this again after any failure. So the upstream job id is
    stored the moment the provider created the job, and the video is stored
    the moment it is downloaded: a later run resumes that job, or takes the
    stored video and only does again what followed (the probe, the poster),
    instead of submitting a new paid generation.

    What the provider says of its job at every poll lands on the row:
    ``provider_state`` and, when it reports a percent, ``provider_eta_at``
    extrapolated from it - what ``get_video_jobs`` reports of a shot that
    went on in the background.

    Once made: a shot asked silent loses any sound track the
    provider still made, before it is stored; a shot made from a picture has
    its text checked against that picture (``text_drift``) and fails as
    "text drift" when its words were redrawn wrong; a shot whose prompt names
    subjects has them checked on its frames (``content_drift``) and
    fails as "content drift" when one is missing, of another kind, deformed
    or replaced. The take a failed shot generated again had made is deleted
    whichever way this generation ends (``clips.REPLACED_MEDIA``); a READY
    shot generated again keeps playing its take (``clips.KEPT_TAKE``):
    this attempt's video, poster and frames are kept beside it
    (``clips.ATTEMPT_MEDIA``) and replace it only once ready and checked - an
    attempt that fails, is cancelled or is given up on leaves the shot ready
    on that take, the attempt recorded beside it (``clips.end_attempt``),
    nothing paid asked again. A shot asked with reference images
    sends the same ones on every generation (``clips.reference_pictures``).
    A screen replacement (``screens.py``) is asked with its screen a flat
    colour, then has the real screenshot drawn into every frame before it is
    checked; its raw take is stored first and replaced once drawn, and kept on
    the row when the screen could not be followed ("screen lost").

    A provider that cools (``provider_cooling``) is not asked: the shot
    goes back to the queue until it cools (``hold``, no attempt spent, its row
    ``pending`` saying until when); one that answers 429 - or a quota - cools,
    for every shot asked of it, and this one goes back to the queue too. An
    account whose credits cannot pay for the shot (``clips.credits_for``)
    is not asked either: its model cools for its quota. A provider busy
    - its load, a 503, no answer, a 429 with no cooling known (Redis
    unreachable) - has the shot wait for it on its own schedule
    (``provider_waits``), for up to a day: never failed for it, never asked of
    another model (one model per round).
    """
    import time

    from app.llm import video_served
    from app.llm.video import VideoJobState, VideoProviderBusyError
    from app.services.video_studio import (
        clips,
        content_drift,
        frame_qc,
        insert_keep_out,
        kept_regions,
        logo_integration,
        logos,
        provider_cooling,
        provider_waits,
        screens,
        stabilise,
        stray_screens,
        text_drift,
        tracked_logos,
    )

    async with async_session() as db:
        clip = await db.get(VideoClip, uuid.UUID(clip_id))
        if clip is None or clip.status in JOB_TERMINAL_STATUSES:
            return JobOutcome("done")
        clip.status = JOB_RUNNING
        clip.progress = 3
        clip.stage = "Preparing the request"
        clip.progress_at = _now()
        if clip.provider_state in (provider_cooling.HELD_BY_PROVIDER, provider_waits.WAITING):
            clip.provider_state = clip.provider_eta_at = None
        await db.commit()
        user_id = clip.user_id
        conversation_id = clip.conversation_id
        shot_label = (clip.name or clip.prompt or "")[:80]
        await bind_llm_caller(db, user_id)
        params = dict(clip.params or {})
        # Named on the row until they are deleted: a run that dies first leaves them to the next one.
        replaced = list(params.get(clips.REPLACED_MEDIA) or [])
        keeps = clips.keeps_take(params)
        # The take that plays while this attempt is made, deleted once a new take replaces it.
        playing = [clip.file_id, clip.poster_file_id] if keeps else []
        # Its media columns: once a new take replaces it, it is kept to be put back (``clips.PREVIOUS_TAKES``).
        playing_media = {key: getattr(clip, key, None) for key in clips.TAKE_MEDIA} if keeps else None
        resume_job = clip.provider_job_id
        stored_file = clips.attempt_file(params) if keeps else clip.file_id
        provider_id = params.get("provider_id")
        label = clip.provider
        provider, model = None, clip.model
        unresolved: clips.ClipError | None = None
        try:
            # Re-resolved at run time: the person may have lost the provider since.
            provider, model, _config = await clips.resolve(db, user_id, model=clip.model, provider_id=provider_id)
            label = getattr(_config, "display_name", None) or getattr(_config, "provider", None) or label
        except clips.ClipError as exc:
            unresolved = exc
        # The art direction of the video the shot was made for, shaped for the model that makes it.
        authored_prompt = clip.prompt or ""
        negative_field = clips.takes_negative_prompt(params, provider, model)
        prompt, negative_prompt = clips.shot_prompts(
            params, authored_prompt, params.get("negative_prompt"), negative_field=negative_field
        )
        request = clips.build_request(
            model=model,
            prompt=prompt,
            aspect_ratio=params.get("aspect_ratio", "16:9"),
            duration_seconds=int(params.get("duration_seconds", 8)),
            resolution=params.get("resolution", "1080p"),
            negative_prompt=negative_prompt,
            sound=params.get("sound"),
        )
        # The ingredients named, a screen asked flat, a screen insert's rectangle kept clear, the
        # terms the last takes kept drawing against the negative prompt asked in the prompt itself.
        request.prompt, request.negative_prompt = clips.provider_prompts(params, request.prompt, request.negative_prompt)
        request.guidance = clips.insisted_guidance(params, provider, model)
        direction = clips.directed(params) or {}
        screen = screens.ScreenSpec.from_params(params[clips.SCREEN]) if isinstance(params.get(clips.SCREEN), dict) else None

    async def ended(status: str, reason: str, **outcome: object) -> JobOutcome:
        """The attempt ends without a new take: failed, or ready on the take it kept (``clips.end_attempt``)."""
        made = {key: outcome.pop(key) for key in clips.TAKE_MEDIA if key in outcome}
        params.pop(clips.REPLACED_MEDIA, None)
        changes = {**outcome, **clips.end_attempt(params, status=status, reason=reason, made=made)}
        await jobs.mark("clip", clip_id, finished_at=_now(), params=params, **changes)
        await assets.delete_files(replaced)
        return JobOutcome("done")

    async def failed(reason: str, **outcome: object) -> JobOutcome:
        return await ended(JOB_FAILED, reason, **outcome)

    async def take_failed(reason: str, made: dict) -> JobOutcome:
        """A made take failed a check of its frames: the shot's next fallback model makes it again, else it fails.

        Its video and frames are kept on the row (or beside the take a ready shot keeps), as any failed take's.
        """
        if params.get(clips.FALLBACK_MODELS):
            moved = await _fall_back(clip_id, params, reason, made=made, label=label)
            if moved is not None:
                return moved
        return await failed(reason, progress=100, stage=None, provider_eta_at=None, **made)

    async def held(cooling: provider_cooling.Cooling, *, ended: bool = False) -> JobOutcome:
        """Back to the queue until the provider cools: nothing asked, no attempt spent, nothing failed. *ended*: the job
        the provider had started ended for its state, never resumed."""
        await jobs.mark(
            "clip",
            clip_id,
            status=JOB_PENDING,
            **provider_cooling.held_changes(cooling),
            **({"provider_job_id": None} if ended else {}),
        )
        return JobOutcome(
            "hold",
            cooling.sentence(),
            wait=max(1.0, cooling.seconds_left()),
            quota=cooling.reason == provider_cooling.QUOTA,
        )

    async def busy(exc: VideoProviderBusyError) -> JobOutcome:
        """Back to the queue until the provider's next try (``provider_waits``): nothing failed, no attempt
        spent, never another model. A job the provider started and ended for its load is asked anew, never resumed."""
        now = _now()
        wait = provider_waits.next_wait(provider_waits.tries(params) + 1, exc.retry_after, clip_id)
        until = now + timedelta(seconds=wait)
        params[provider_waits.KEY] = provider_waits.waited(params, reason=str(exc), label=label, model=request.model, until=until, now=now)
        await jobs.mark(
            "clip",
            clip_id,
            status=JOB_PENDING,
            params=params,
            **provider_waits.row_changes(params[provider_waits.KEY], until),
            **({"provider_job_id": None} if exc.started else {}),
        )
        return JobOutcome("hold", provider_waits.line(params, until, now)[:2000], wait=wait, busy=True)

    async def keep_beside(media: dict, **changes: object) -> None:
        """What this attempt made, on the row: its columns, or beside the take it keeps (``clips.ATTEMPT_MEDIA``)."""
        if keeps:
            clips.keep_attempt_media(params, media)
            await jobs.mark("clip", clip_id, params=params, **changes)
        else:
            await jobs.mark("clip", clip_id, params=params, **media, **changes)

    if unresolved is not None and not stored_file:
        return await failed(str(unresolved))
    workdir = tempfile.mkdtemp(prefix="xtm-clip-")
    try:
        path = str(Path(workdir) / "clip.mp4")
        base = _slug(authored_prompt[:60])
        first = None
        if stored_file and await assets.download_file(stored_file, path) is None:
            stored_file = None
        if not stored_file:
            if not resume_job:
                cooling = await provider_cooling.cooling_of(provider_id, model=request.model)
                if cooling is not None:
                    return await held(cooling)
                # An account whose credits cannot pay for the shot is not asked: its model cools for its quota,
                # and the shot waits with the others - never sent to be refused, never asked of another model.
                credits = await clips.credits_for(provider, request)
                if credits is not None and credits.short():
                    cooling = await provider_cooling.note_refusal(
                        provider_id,
                        label=label,
                        model=request.model,
                        quota=True,
                        remaining=credits.remaining,
                        needed=credits.needed,
                        detail=credits.words(),
                    )
                    if cooling is not None:
                        return await held(cooling)
                    return await busy(VideoProviderBusyError(f"{label}: {credits.words()}", quota=True))
                # A job resumed upstream was asked with its frame already.
                try:
                    first = await clips.start_frame(params, workdir, user_id=user_id, conversation_id=conversation_id)
                    # The same pictures on every generation of the shot: its subjects stay the same.
                    request.reference_images = await clips.fit_reference_pictures(
                        params,
                        await clips.reference_pictures(params, workdir),
                        clips.reference_limits_of(provider, request.model),
                        model=request.model,
                    )
                except clips.ClipError as exc:
                    # Refused before the provider was asked (a picture gone, of a shape it refuses): no try.
                    clips.uncount_regeneration(params, authored_prompt)
                    return await failed(str(exc))
                if first is not None:
                    request.reference_image, request.reference_mime = first.data, first.mime
                    if insert_keep_out.start_as_reference(params, provider, request.model):
                        # The start picture again, as a picture to keep: its screen clear of what may stand in front.
                        request.reference_images = [*request.reference_images, (first.data, first.mime)]
                if first is not None and first.saved:
                    # Named before the provider is asked: a failed generation is made again from this very frame.
                    await jobs.mark("clip", clip_id, params=params)

            async def progress(value: int) -> None:
                if cancel_event is not None and cancel_event.is_set():
                    raise asyncio.CancelledError
                await jobs.mark("clip", clip_id, progress=max(5, min(95, value)))

            async def remember(job_id: str) -> None:
                await jobs.mark(
                    "clip", clip_id, provider_job_id=job_id[:255] or None, stage="Submitted to the provider"
                )
                # The provider took a request: its refusals, and the model's, are no longer in a row.
                await provider_cooling.clear(provider_id, model=request.model)
                await video_served.clear(provider_id, request.model)

            seen_upstream: list[float] = []

            async def tell(state: VideoJobState) -> None:
                if not seen_upstream:
                    seen_upstream.append(time.monotonic())
                eta = None
                if state.progress is not None and 5 <= state.progress < 100:
                    elapsed = time.monotonic() - seen_upstream[0]
                    eta = _now() + timedelta(seconds=elapsed * (100 - state.progress) / state.progress)
                await jobs.mark(
                    "clip",
                    clip_id,
                    stage="Generating at the provider",
                    provider_state=state.state[:40],
                    provider_eta_at=eta,
                )

            try:
                made = await clips.generate(
                    provider, request, progress=progress, on_job=remember, resume_job=resume_job, on_state=tell
                )
            except clips.ClipError as exc:
                if exc.not_served is not None:
                    # The provider answered 404 for the model: off its listing, refused before it is paid.
                    await video_served.note(provider_id, request.model, exc.not_served)
                if exc.unpaid:
                    # Refused on its arguments before a job existed: nothing made, so no try of its prompt.
                    clips.uncount_regeneration(params, authored_prompt)
                if params.get(clips.FALLBACK_MODELS):
                    # The provider failed the model: the next configured fallback makes the shot, else it fails.
                    moved = await _fall_back(clip_id, params, str(exc), made={}, label=label)
                    if moved is not None:
                        return moved
                return await failed(str(exc))
            except VideoProviderBusyError as exc:
                if provider_cooling.is_refusal(exc):
                    # The provider's quota or rate limit, shared by every shot asked of it: it cools,
                    # and this shot waits in the queue with the others - never a failure of its own.
                    cooling = await provider_cooling.note_refusal(
                        provider_id,
                        label=label,
                        model=request.model,
                        status_code=exc.status_code,
                        retry_after=exc.retry_after,
                        quota=exc.quota,
                        detail=str(exc),
                        remaining=exc.remaining,
                        needed=exc.needed,
                    )
                    if cooling is not None:
                        return await held(cooling, ended=exc.started)
                # Busy - its load, an outage, no answer, a 429 no cooling holds: the shot waits for this very
                # model on its own schedule, no attempt spent, nothing failed.
                return await busy(exc)
            Path(path).write_bytes(made.data)
            # Its waits for a busy provider are over: kept on the shot as a record.
            provider_waits.taken(params)
            # A new take: nothing drawn into it yet.
            params.pop(_TAKE_STEPS, None)
            asked = [
                note
                for note in (
                    clips.start_note(params),
                    clips.reference_note(params),
                    clips.direction_note(params, negative_field=negative_field),
                    clips.sound_note(params),
                    # The terms its last takes kept drawing, asked in its prompt itself.
                    clips.insisted_note_of(params, request.guidance, model),
                )
                if note
            ]
            params["notes"] = [*asked, *made.notes]
            if request.sound is False:
                params["notes"].extend(await _silenced(path, workdir))
            # Stored before anything that may fail after it: the paid video is never lost.
            video = await assets.store_path(
                path, filename=f"{base}.mp4", user_id=user_id, source=assets.SOURCE_CLIP, content_type="video/mp4"
            )
            stored_file = video.id
            if not keeps:
                # The failed take goes before the row stops naming it: a crash in between deletes it again.
                await assets.delete_files(replaced)
                replaced = []
                params.pop(clips.REPLACED_MEDIA, None)
            await keep_beside(
                {"file_id": stored_file},
                provider_job_id=made.job_id[:255] or None,
                stage="Saving the shot",
                provider_eta_at=None,
            )
        info = await encoder.probe(path)
        poster_path = str(Path(workdir) / "poster.jpg")
        poster_at = min(1.0, info.duration / 2)
        # What the poster is, so a second read in a turn answers with it.
        poster_caption = f'The poster of the AI shot "{shot_label or base}": its frame at {poster_at:.1f} s, 1280 wide.'
        await encoder.extract_frame(path, at=poster_at, out_path=poster_path, width=1280)
        poster = await assets.store_path(
            poster_path,
            filename=f"{base}-poster.jpg",
            user_id=user_id,
            source=assets.SOURCE_CLIP,
            description=poster_caption,
        )
        made_media = {
            "duration_ms": int(info.duration * 1000),
            "width": info.width,
            "height": info.height,
            "has_audio": info.has_audio,
            "file_id": stored_file,
            "poster_file_id": poster.id,
        }
        if keeps:
            # Named beside the kept take at once: a cancel or a dead letter records it with the attempt.
            await keep_beside(made_media)

        async def swap_take(
            step: str, notes: list[str], key: str | None = None, marker: str = "", *, keep: object = None
        ) -> None:
            """The video at *path*, drawn over in place, becomes the take; the one drawn over is deleted, but *keep*.

            The step is logged with the file it made (:data:`_TAKE_STEPS`) and,
            given a *key*, ``params[key][marker]`` names it, before the row is
            written: a run resumed on it never draws over it twice, nor redoes
            a step that came before it.
            """
            nonlocal stored_file
            params.setdefault("notes", []).extend(notes)
            raw = [made_media["file_id"], made_media["poster_file_id"]]
            video = await assets.store_path(
                path, filename=f"{base}.mp4", user_id=user_id, source=assets.SOURCE_CLIP, content_type="video/mp4"
            )
            await encoder.extract_frame(path, at=poster_at, out_path=poster_path, width=1280)
            poster = await assets.store_path(
                poster_path,
                filename=f"{base}-poster.jpg",
                user_id=user_id,
                source=assets.SOURCE_CLIP,
                description=poster_caption,
            )
            stored_file = video.id
            made_media.update(file_id=video.id, poster_file_id=poster.id)
            params[_TAKE_STEPS] = [*(params.get(_TAKE_STEPS) or []), {"step": step, "file_id": str(video.id)}]
            if key is not None:
                params[key] = {**params[key], marker: str(video.id)}
            await keep_beside(made_media)
            await assets.delete_files([file_id for file_id in raw if file_id != keep])

        steps_done = _steps_done(params, stored_file)
        # The screen the take shows, measured on the take as made, before a screenshot is drawn into it, of a
        # shot carrying a screen insert: a take that bends it fails - the next fallback model may keep it
        # flat. A shot carrying none has no surface to be flat.
        kept_surface = (params.get(frame_qc.QC_KEY) or {}).get(frame_qc.SURFACE)
        measured = isinstance(kept_surface, dict) and kept_surface.get("take_file_id") == str(stored_file)
        if not steps_done and not measured and clips.carries_insert(params):
            surface, unflat = await clips.take_surface(path, workdir, params)
            if surface is not None:
                params[frame_qc.QC_KEY] = {
                    **(params.get(frame_qc.QC_KEY) or {}),
                    frame_qc.SURFACE: {**surface, "take_file_id": str(stored_file)},
                }
            if unflat is not None:
                return await take_failed(unflat, made_media)
        if not steps_done and params.get("content_check", True) and authored_prompt.strip():
            # What stands in front of a screen insert's screen on every frame, matted before a screenshot is drawn
            # into it: the content check judges a negative-prompt term seen in front of the screen by it.
            await insert_keep_out.measure_take(path, info, params, take_file_id=str(stored_file))
        screenshot = None
        if screen is not None:
            try:
                screenshot = await _screenshot_of(screen, workdir)
                if "screen" not in steps_done and params[clips.SCREEN].get("replaced_file_id") != str(stored_file):
                    await jobs.mark("clip", clip_id, stage="Drawing the real screen into the shot")
                    drawn = await _screen_replaced(path, screen, screenshot, params[clips.SCREEN], workdir)
                else:
                    drawn = None
            except clips.ClipError as exc:
                return await failed(str(exc), progress=100, stage=None, provider_eta_at=None, **made_media)
            except screens.ScreenLostError as exc:
                # Kept on the row, like a drifted shot: the person sees what the model made of the screen - a lost
                # surface, the key left around it, a matte that flickers, content past the bezel alike.
                return await take_failed(str(exc), made_media)
            if drawn is not None:
                # What was drawn and measured travels with the take: the screenshot as fitted, its checks.
                params[clips.SCREEN] = {**params[clips.SCREEN], **drawn.record}
                await swap_take("screen", drawn.notes, clips.SCREEN, "replaced_file_id")
        regions = kept_regions.boxes_of(params)
        if (
            regions
            and "regions" not in steps_done
            and params[kept_regions.KEY].get("drawn_file_id") != str(stored_file)
        ):
            # The still's words, where the model redraws them: drawn back before any check reads the take.
            await jobs.mark("clip", clip_id, stage="Keeping the start picture's regions")
            try:
                still = first or await clips.start_frame(params, workdir, user_id=user_id, conversation_id=conversation_id)
                if still is None:
                    raise clips.ClipError("The shot has no start picture to keep regions from.")
                first = still
                image = await asyncio.to_thread(_decoded_picture, still.data)
                kept = await _regions_kept(path, image, regions, workdir)
            except clips.ClipError as exc:
                return await failed(str(exc), progress=100, stage=None, provider_eta_at=None, **made_media)
            except kept_regions.RegionLostError as exc:
                # The camera the regions could not follow is the take's: the next fallback model may hold it.
                return await take_failed(str(exc), made_media)
            await swap_take("regions", kept.notes, kept_regions.KEY, "drawn_file_id")
        logos_asked = clips.composited_logos(params)
        if logos_asked and "logos" not in steps_done:
            # A logo the model cannot take as a picture is drawn back with its own pixels.
            await jobs.mark("clip", clip_id, stage="Drawing the logo back onto the shot")
            try:
                composited = await _logos_composited(
                    path, logos_asked, first, params, workdir, user_id=user_id, conversation_id=conversation_id
                )
            except clips.ClipError as exc:
                return await failed(str(exc), progress=100, stage=None, provider_eta_at=None, **made_media)
            except logos.LogoLostError as exc:
                return await take_failed(await _lost_logo_reason(exc, params, base=base, user_id=user_id), made_media)
            # Any other error goes to the job's retry, which resumes from the take stored: a garbled logo is
            # never kept because drawing it back failed once.
            if composited.drawn:
                await _record_logo_check(composited, params, base=base, user_id=user_id)
                await swap_take("logos", composited.notes)
            else:
                params.setdefault("notes", []).extend(composited.notes)
        tracked_asked, fell_back_from = clips.tracked_logos(params), None
        sent_with_subject = clips.native_logos_with_subject(params)
        if sent_with_subject and "tracked" not in steps_done:
            # The model was sent the logo: its take is checked for the logo as given, and the tracked path
            # draws it onto its subject when the check fails - nothing generated again.
            await jobs.mark("clip", clip_id, stage="Checking the model's take for the logo")
            try:
                native = await _native_logo_check(path, sent_with_subject, workdir)
            except clips.ClipError as exc:
                return await failed(str(exc), progress=100, stage=None, provider_eta_at=None, **made_media)
            names = ", ".join(item.name for item in sent_with_subject)
            if native.passed:
                params.setdefault("notes", []).append(
                    f"The model's take shows {names} as given (found in {max(native.found.values())} of "
                    f"{native.sampled} frames sampled): nothing drawn onto the subject."
                )
            else:
                tracked_asked, fell_back_from = sent_with_subject, clips.PATH_NATIVE
                params[clips.INGREDIENTS_PATH] = clips.PATH_TRACKED
                params.setdefault("notes", []).append(
                    f"The model's take failed the logo check ({native.reason()}): {names} drawn onto the subject "
                    "instead, with the logo's own pixels (ingredients_path: tracked)."
                )
        if tracked_asked and "tracked" not in steps_done:
            # A logo on a subject the start frame does not show: found in the take, the logo drawn onto it.
            await jobs.mark("clip", clip_id, stage="Drawing the logo onto its subject")
            try:
                tracked = await _logos_tracked(
                    path,
                    tracked_asked,
                    workdir,
                    user_id=user_id,
                    fell_back_from=fell_back_from,
                    flat=logo_integration.flat_accepted(params),
                )
            except clips.ClipError as exc:
                return await failed(str(exc), progress=100, stage=None, provider_eta_at=None, **made_media)
            except tracked_logos.IngredientLostError as exc:
                reason = await _lost_logo_reason(exc, params, base=base, user_id=user_id, what="the logo's subject or surface")
                return await take_failed(reason, made_media)
            # The detector giving no answer, or any other error, goes to the job's retry, which resumes from the
            # take stored: a take is never kept without the logo because drawing it failed once.
            drawn_record = tracked.record()
            drawn_record["integration"] = await _integration_kept(
                tracked.integration, drawn_record.get("integration"), base=base, user_id=user_id
            )
            params[frame_qc.QC_KEY] = {**(params.get(frame_qc.QC_KEY) or {}), frame_qc.LOGO_TRACKED: drawn_record}
            await swap_take("tracked", tracked.notes)
        camera = None
        if (
            params.get(clips.STABILISE) is None
            and params.get(frame_qc.MAX_CAMERA_MOTION) is None
            and params.get("content_check", True)
        ):
            # Whether the prompt asks for a locked camera: the content check's own reading, made first and kept
            # for it - a shot skipping that check is stabilised only when asked or given a tolerance.
            camera = await content_drift.prompt_camera(
                user_id, prompt=authored_prompt, negative_prompt=params.get("negative_prompt"), params=params
            )
        if clips.wants_stabilising(params, camera) and "stabilise" not in steps_done:
            await jobs.mark("clip", clip_id, stage="Stabilising the camera")
            steadied = str(Path(workdir) / "clip-stable.mp4")
            # The screen drawn into it is never cropped into.
            shown = screens.start_quad(params.get(clips.SCREEN))
            try:
                result = await stabilise.stabilise(path, steadied, keep=[shown] if shown else None)
            except Exception as exc:  # advisory, like the frames: the take is then judged as the model made it
                logger.warning("Video clip %s was not stabilised: %s", clip_id, format_exception_message(exc))
                result = stabilise.Stabilised(False, reason=f"its frames could not be read ({format_exception_message(exc)[:160]})")
            record = result.as_params()
            if result.applied:
                unstabilised = made_media["file_id"]
                shutil.move(steadied, path)
                record[frame_qc.UNSTABILISED] = str(unstabilised)
                # With the swap's own write: the un-stabilised take is the take's from then on.
                params[frame_qc.QC_KEY] = {**(params.get(frame_qc.QC_KEY) or {}), frame_qc.STABILISATION: record}
                await swap_take("stabilise", [], keep=unstabilised)
            else:
                params[frame_qc.QC_KEY] = {**(params.get(frame_qc.QC_KEY) or {}), frame_qc.STABILISATION: record}
            if said := stabilise.note(record):
                params.setdefault("notes", []).append(said)
            await jobs.mark("clip", clip_id, params=params)
        started_from = bool(params.get("reference_file_id") or params.get("reference_video"))
        threshold = params.get(clips.MIN_REFERENCE_SIMILARITY)
        picture, unreadable = (first if started_from else None), None
        if picture is None and started_from and (params.get("text_check", True) or threshold is not None):
            try:
                picture = await clips.start_frame(params, workdir, user_id=user_id, conversation_id=conversation_id)
            except Exception as exc:  # each check below says what it could not do
                unreadable = exc
        if picture is not None and screenshot is not None:
            # The flat screen sent, with the screenshot drawn in: what the first frame is measured against.
            try:
                expected = await asyncio.to_thread(
                    screens.expected_start, picture.data, screenshot, screen, tuple(params[clips.SCREEN]["start_size"])
                )
            except Exception as exc:  # advisory, like the frames: the take is left unmeasured
                logger.warning("Video clip %s: its start was not redrawn: %s", clip_id, format_exception_message(exc))
                picture = None
            else:
                picture = clips.StartFrame(expected, "image/png", picture.file_id)
        await jobs.mark("clip", clip_id, stage="Checking the shot's frames")
        qc = await _keep_frames(
            clip_id, path, info.duration, workdir, picture, user_id=user_id, base=base, params=params, label=shot_label
        )
        # The thresholds this take passed under, recorded with its frames: the take is judged by them.
        applied: dict[str, float] = {}
        if threshold is not None:
            similarity = qc.get("reference_ssim")
            if similarity is None:
                params.setdefault("notes", []).append(
                    "The shot's first frame was not compared with its start picture: min_reference_similarity was not applied."
                )
            elif similarity < float(threshold):
                # Kept on the row, like a drift: the person sees what came out, generating it again replaces it.
                return await take_failed(frame_qc.drift_reason(similarity, float(threshold)), made_media)
            else:
                applied[clips.MIN_REFERENCE_SIMILARITY] = float(threshold)
        tolerance = params.get(frame_qc.MAX_CAMERA_MOTION)
        if tolerance is not None:
            # The camera a prompt cannot hold is measured on the frames, never read off one.
            motion = frame_qc.camera_motion(qc)
            if motion is None:
                params.setdefault("notes", []).append(
                    "The shot's camera motion was not measured: max_camera_motion was not applied."
                )
            elif motion > float(tolerance):
                steadied = (qc.get(frame_qc.STABILISATION) or {}) if qc else {}
                change = frame_qc.frame_change(qc) if frame_qc.CAMERA_MOTION in qc else None
                return await take_failed(
                    frame_qc.motion_reason(motion, float(tolerance), change=change) + frame_qc.stabilised_clause(steadied),
                    made_media,
                )
            else:
                applied[frame_qc.MAX_CAMERA_MOTION] = float(tolerance)
        # The video's references the shot is held to: a take whose frames drifted from one is failed, kept on
        # the row like a drift; a check that cannot read says so and lets the take through (the save reads it again).
        if held := clips.consistency_of(params):
            await jobs.mark("clip", clip_id, stage="Checking the shot against the video's references")
            try:
                drifts = await _reference_drifts(held, path, info.duration, workdir)
            except Exception as exc:  # advisory, like the frames
                params.setdefault("notes", []).append(
                    f"The shot was not checked against the video's references: {format_exception_message(exc)[:160]}"
                )
            else:
                if drifts:
                    from app.services.video_studio import reference_drift

                    # A reference seen in none of the take's frames is said, never a failure: the shot may
                    # frame the prop from an angle the reference does not show.
                    if reference_drift.failures(drifts):
                        return await take_failed(reference_drift.take_reason(drifts), made_media)
                    params.setdefault("notes", []).extend(drift.words() for drift in drifts)
        # A screen shot's screen is the screenshot itself: its text is never read against the start.
        if params.get("text_check", True) and started_from and screen is None:
            await jobs.mark("clip", clip_id, stage="Checking the text of the shot")
            if picture is None:
                params.setdefault("notes", []).append(f"The text of the shot was not checked: {unreadable}")
            else:

                def read_match(verdict: Any, measured: dict) -> None:
                    # Against the start picture's readable words alone: its soft flags count for nothing.
                    if verdict.texts and measured:
                        match = frame_qc.text_match(verdict.expected_text, verdict.texts.get("first_frame", ""))
                        if match is not None:
                            params[frame_qc.QC_KEY]["reference_text_match"] = match

                async def text_failure(verdict: Any, suffix: str = "") -> JobOutcome:
                    # The record goes with the take: the words checked, the ones left out, the word's crop.
                    crop = await _record_text_check(verdict, params, base=base, user_id=user_id)
                    named = f" (crop: file `{crop}`)" if crop else ""
                    return await take_failed(f"{verdict.reason}{suffix}{named}", made_media)

                # The screen a screen insert draws over is left unread: its lettering is covered at render time.
                covered = insert_keep_out.covered_box(params)
                verdict = await text_drift.check(
                    user_id, (picture.data, picture.mime), path, workdir, duration=info.duration, covered=covered
                )
                read_match(verdict, qc)
                if (
                    verdict.failed
                    and text_drift.reads_lettering(verdict)
                    and "lettering" not in _steps_done(params, stored_file)
                ):
                    # The start picture's lettering the model redrew: drawn back from the picture, the take read
                    # again - once - before it fails.
                    await jobs.mark("clip", clip_id, stage="Drawing the start picture's lettering back")
                    boxes = await text_drift.locate_lettering(user_id, (picture.data, picture.mime))
                    if not boxes:
                        return await text_failure(
                            verdict, " Its lettering was not drawn back: it could not be located on the start picture."
                        )
                    try:
                        image = await asyncio.to_thread(_decoded_picture, picture.data)
                        lettered = await _regions_kept(path, image, boxes, workdir)
                    except kept_regions.RegionLostError as exc:
                        return await text_failure(verdict, f" Its lettering was not drawn back: {exc}")
                    params[text_drift.LETTERING_KEY] = {"boxes": boxes}
                    await swap_take(
                        "lettering",
                        [
                            "The model redrew the start picture's lettering: it was drawn back from the start "
                            f"picture ({len(boxes)} region{'s' if len(boxes) > 1 else ''}) and the text read again.",
                            *lettered.notes,
                        ],
                        text_drift.LETTERING_KEY,
                        "drawn_file_id",
                    )
                    qc = await _keep_frames(
                        clip_id, path, info.duration, workdir, picture, user_id=user_id, base=base, params=params, label=shot_label
                    )
                    await jobs.mark("clip", clip_id, stage="Checking the text of the shot again")
                    verdict = await text_drift.check(
                        user_id, (picture.data, picture.mime), path, workdir, duration=info.duration, covered=covered
                    )
                    read_match(verdict, qc)
                    if verdict.failed:
                        return await text_failure(verdict, " This is after its lettering was drawn back from the start picture.")
                if verdict.failed:
                    # The video stays on the row: the person can see it, and generating it again replaces it.
                    return await text_failure(verdict)
                if verdict.checked:
                    await _record_text_check(verdict, params, base=base, user_id=user_id)
                for said in (
                    verdict.note,
                    verdict.covered_note,
                    insert_keep_out.key_advisory(params) if verdict.covered is not None else None,
                    verdict.new_text_note,
                    verdict.invented_note,
                ):
                    if said:
                        params.setdefault("notes", []).append(said)
        if params.get("content_check", True) and authored_prompt.strip():
            await jobs.mark("clip", clip_id, stage="Checking the shot against its prompt")
            cleanup = params.get(stray_screens.KEY) if isinstance(params.get(stray_screens.KEY), dict) else None
            # What the start picture already shows of the negative prompt: never a drift of the take's first frame
            # (issue #10053). Kept from the call that asked the shot, else read now on the picture in hand.
            opened_on = await content_drift.start_shown(
                user_id,
                params,
                params.get("negative_prompt"),
                clips.start_frame_file_id(params),
                (picture.data, picture.mime) if picture is not None else None,
            )

            start_digest = content_drift.start_content_digest(
                authored_prompt, params.get("negative_prompt"), clips.start_frame_file_id(params)
            )
            start_read = content_drift.kept_start_findings(params, start_digest)

            async def read_content():
                # What the author asked fails a shot that misses it; what the template avoids is only said.
                return await content_drift.check(
                    user_id,
                    prompt=authored_prompt,
                    negative_prompt=params.get("negative_prompt"),
                    video_path=path,
                    duration=info.duration,
                    workdir=workdir,
                    params=params,
                    avoided=list(direction.get("avoid") or []),
                    locate_screens=cleanup is not None,
                    keep_out=insert_keep_out.rectangle_of(params),
                    start_shown=opened_on,
                    # What the start picture itself failed of the subjects: a take's finding it shares is inherited.
                    start_findings=start_read,
                )

            async def drift_failed(reason: str) -> JobOutcome:
                # Which attempt the paid take was, and how many regenerations of its prompt remain.
                return await take_failed(f"{reason}{clips.attempt_clause(params, authored_prompt)}", made_media)

            content = await read_content()
            accept_minor = params.get(content_drift.ACCEPT_MINOR_KEY, True) is not False
            failing = content.failing(accept_minor=accept_minor)
            cleaned = cleanup.get("cleaned") if cleanup and cleanup.get("cleaned_file_id") == str(stored_file) else None
            if failing and cleanup is not None and cleaned is None and content.cleanable(accept_minor=accept_minor):
                # The screens the model drew against the negative prompt, cleaned rather than the take thrown away.
                await jobs.mark("clip", clip_id, stage="Cleaning the stray screens")
                try:
                    picture_in = (
                        await _picture_file(cleanup["file_id"], workdir)
                        if cleanup.get("mode") == stray_screens.SCREENSHOT
                        else None
                    )
                    done = await _screens_cleaned(path, content.sightings(), cleanup, picture_in, workdir)
                except clips.ClipError as exc:
                    reason = f"{content.reason(accept_minor=accept_minor)} The stray screens were not cleaned: {exc}."
                    return await failed(reason, progress=100, stage=None, provider_eta_at=None, **made_media)
                except stray_screens.CleanupError as exc:
                    return await drift_failed(f"{content.reason(accept_minor=accept_minor)} The stray screens were not cleaned: {exc}.")
                params[stray_screens.KEY] = {**cleanup, "cleaned": done.at_read_frames}
                await swap_take("cleaned", done.notes, stray_screens.KEY, "cleaned_file_id")
                cleaned = done.at_read_frames
                await _keep_frames(
                    clip_id, path, info.duration, workdir, picture, user_id=user_id, base=base, params=params, label=shot_label
                )
                # Read again: what is left on a cleaned screen is what the author chose; anything else still fails.
                await jobs.mark("clip", clip_id, stage="Checking the cleaned shot against its prompt")
                content = await read_content()
            if cleaned is not None:
                failing = content.failing_after(cleaned, accept_minor=accept_minor)
                on_cleaned = [f.what for f in content.failing(accept_minor=accept_minor) if f not in failing]
                if on_cleaned:
                    params.setdefault("notes", []).append(
                        f"The cleaned screens still read as: {'; '.join(dict.fromkeys(on_cleaned))} - kept: a cleaned "
                        "screen is blank (or shows the screenshot) by the shot's stray_screens choice."
                    )
            # A negative-prompt term seen only in front of a screen insert's screen: the take's measure judges it.
            judged = insert_keep_out.judge(failing, params)
            failing = judged.failing
            if (
                picture is not None
                and start_digest is not None
                and start_read is None
                and any(f.frame == content_drift.FRAMES[0] and not f.excluded for f in failing)
            ):
                # A subject finding on the first frame, the start picture never read for it (an older shot, a reading
                # that failed when it was asked): read it now, so a finding it shares is said inherited (issue #10895).
                start_read = await content_drift.read_start_content(
                    user_id,
                    (picture.data, picture.mime),
                    prompt=authored_prompt,
                    negative_prompt=params.get("negative_prompt"),
                    params=params,
                )
                content_drift.keep_start_findings(params, start_digest, start_read)
                content.start_findings = list(start_read or [])
            # What the negative prompt excludes that this take showed: two takes in a row, the next asks it harder.
            clips.record_excluded_shown(params, content_drift.excluded_shown(failing))
            if failing:
                # Kept on the row, like a text drift: the person sees what came out, generating it again replaces it.
                if cleaned is not None:
                    reason = f"{content.reason(accept_minor=accept_minor, findings=failing)} (after its stray screens were cleaned)"
                elif judged.accepted:
                    reason = content.reason(accept_minor=accept_minor, findings=failing)
                else:
                    reason = content.reason(accept_minor=accept_minor)
                return await drift_failed(f"{reason}{judged.clause}")
            for note in (
                content.note,
                content_drift.start_shown_note(opened_on),
                judged.note,
                # When the screen is first occluded and the trim that ends the shot before it (dev:screen-occlusion-check).
                insert_keep_out.occlusion_note(params),
                content.accepted_note(accept_minor=accept_minor),
                content.avoided_note(direction.get("template")),
            ):
                if note:
                    params.setdefault("notes", []).append(note)
        if keeps:
            # A regeneration in place: held to the pre-render lint of every video drawing a screen insert on the shot
            # before it replaces the take that plays (issue #10901); a shot under no insert reads nothing.
            insert_findings = await _insert_promotion_findings(user_id, clip_id, str(stored_file))
            if insert_findings:
                return await take_failed(f"screen insert: {'; '.join(insert_findings)}", made_media)
        measured = params.get(frame_qc.QC_KEY)
        if isinstance(measured, dict):
            measured[frame_qc.CHECKED_WITH] = applied
        # Ready and checked: the take it kept is kept to be put back (``restore_take``), the attempts that
        # failed beside it go now.
        replaced = [*replaced, *clips.retire_kept_take(params, playing, media=playing_media)]
        params.pop(clips.REPLACED_MEDIA, None)
        await jobs.mark(
            "clip",
            clip_id,
            status=JOB_READY,
            progress=100,
            stage=None,
            provider_eta_at=None,
            params=params,
            finished_at=_now(),
            **made_media,
        )
        await assets.delete_files(replaced)
        return JobOutcome("done")
    except asyncio.CancelledError:
        return await ended(JOB_CANCELLED, "Cancelled.")
    except Exception as exc:
        logger.warning("Video clip %s failed: %s", clip_id, format_exception_message(exc), exc_info=True)
        return JobOutcome("retry", f"The clip failed: {format_exception_message(exc)}"[:2000])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ── AI track ──────────────────────────────────────────────────────────────

#: Seconds over which a composition's progress estimate climbs (one request, no progress of its own).
_MUSIC_PACE_SECONDS = 45.0
_MUSIC_SUFFIXES = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/flac": ".flac"}


async def _extended(provider: Any, request: Any, take: dict, workdir: Path) -> tuple[str, Any]:
    """The kept *take* (``music.EXTENDED_FROM``) extended from its cut (``music.extend_take``)."""
    from app.services.video_studio import music

    take_path = str(workdir / "take")
    if await assets.download_file(take["file_id"], take_path) is None:
        raise music.MusicError("The take's file is gone: refit without extend composes the track again.")
    return await music.extend_take(provider, request, take_path, cut=float(take["cut"]), workdir=workdir)


async def _padded(take: dict, seconds: float, workdir: Path) -> tuple[str, Any]:
    """The kept *take* (``music.EXTENDED_FROM``, ``pad``) held to *seconds* with no model call (``music.pad_take``)."""
    from app.llm.music import MusicResult
    from app.services.video_studio import music

    take_path = str(workdir / "take")
    if await assets.download_file(take["file_id"], take_path) is None:
        raise music.MusicError("The take's file is gone: refit without pad composes the track again.")
    path, note = await music.pad_take(take_path, seconds=seconds, workdir=workdir)
    return path, MusicResult(b"", "audio/wav", notes=[note])


async def run_music(music_id: str, *, cancel_event: asyncio.Event | None = None) -> JobOutcome:
    import math
    import time

    from app.llm.music import MusicProviderBusyError
    from app.models.video_studio import VideoMusic
    from app.services.video_studio import music

    async with async_session() as db:
        row = await db.get(VideoMusic, uuid.UUID(music_id))
        if row is None or row.status in JOB_TERMINAL_STATUSES:
            return JobOutcome("done")
        params = dict(row.params or {})
        # The take an extended refit keeps: extended from its cut, ready on it again if no extension is made.
        extending = params.get(music.EXTENDED_FROM) if isinstance(params.get(music.EXTENDED_FROM), dict) else None
        # A padded refit: the take held to its length, no model asked.
        padding = bool(extending is not None and extending.get("pad"))
        row.status = JOB_RUNNING
        row.progress = 3
        row.stage = "Holding the take's last chord" if padding else "Composing with the music model"
        row.progress_at = _now()
        await db.commit()
        user_id = row.user_id
        await bind_llm_caller(db, user_id)
        # The file of a track composed again at a new length: deleted once this composition ends.
        replaced = list(params.pop(music.REPLACED_MEDIA, None) or [])
        try:
            # Re-resolved at run time: the person may have lost the provider since.
            if padding:
                provider, model = None, row.model
            elif extending is not None:
                provider, model, _config = await music.resolve_continuing(
                    db, user_id, model=row.model, provider_id=params.get("provider_id")
                )
            else:
                provider, model, _config = await music.resolve(
                    db, user_id, model=row.model, provider_id=params.get("provider_id")
                )
        except music.MusicError as exc:
            kept = music.kept_take(params, str(exc))
            for name, value in (kept or {"status": JOB_FAILED, "error": str(exc)[:2000]}).items():
                setattr(row, name, value)
            row.finished_at = _now()
            row.params = params
            await db.commit()
            await assets.delete_files(replaced)
            return JobOutcome("done")
        target = float(params.get("duration_seconds") or 60)
        request = music.build_request(
            model=model,
            prompt=row.prompt,
            # An extension composes from the take's cut to the length asked.
            duration_seconds=target - float(extending["cut"]) if extending is not None and not padding else target,
            instrumental=params.get("instrumental", True),
            negative_prompt=params.get("negative_prompt"),
        )
    workdir = tempfile.mkdtemp(prefix="xtm-music-")
    if padding:
        made_by = _padded(extending, target, Path(workdir))
    elif extending is not None:
        made_by = _extended(provider, request, extending, Path(workdir))
    else:
        made_by = music.generate(provider, request)
    composing = asyncio.create_task(made_by)

    async def ended_without(reason: str, **changes: Any) -> None:
        """The composition ended with no new take: an extension's row ready on the take it kept, else *changes*."""
        kept = music.kept_take(params, reason)
        if kept is not None:
            replaced[:] = [file_id for file_id in replaced if file_id != str(kept["file_id"])]
        await jobs.mark("music", music_id, finished_at=_now(), params=params, **(kept or changes))
        await assets.delete_files(replaced)

    try:
        started = time.monotonic()
        while not composing.done():
            await asyncio.wait({composing}, timeout=4.0)
            if cancel_event is not None and cancel_event.is_set():
                composing.cancel()
                raise asyncio.CancelledError
            if not composing.done():
                estimate = 5 + 85 * (1 - math.exp(-(time.monotonic() - started) / _MUSIC_PACE_SECONDS))
                await jobs.mark("music", music_id, progress=int(estimate))
        try:
            made = composing.result()
        except music.MusicError as exc:
            await ended_without(str(exc), status=JOB_FAILED, error=str(exc)[:2000])
            return JobOutcome("done")
        if extending is not None:
            path, made = made
            mime, suffix = "audio/wav", ".wav"
        else:
            mime = made.mime
            suffix = _MUSIC_SUFFIXES.get(mime.lower(), ".audio")
            path = str(Path(workdir) / f"music{suffix}")
            Path(path).write_bytes(made.audio)
        info = await encoder.probe(path)
        if info.duration <= 0:
            await ended_without(
                "The music model returned no playable audio.",
                status=JOB_FAILED,
                error="The music model returned no playable audio.",
            )
            return JobOutcome("done")
        base = _slug(request.prompt[:60])
        stored = await assets.store_path(
            path, filename=f"{base}{suffix if suffix != '.audio' else '.mp3'}", user_id=user_id, source=assets.SOURCE_MUSIC, content_type=mime
        )
        if params.pop(music.EXTENDED_FROM, None) is not None:
            # The extended take holds the kept take's head: the kept take's own file goes now.
            replaced.append(str(extending["file_id"]))
        params["notes"] = made.notes
        if made.description:
            params["description"] = made.description
        await jobs.mark(
            "music",
            music_id,
            status=JOB_READY,
            progress=100,
            stage=None,
            duration_ms=int(info.duration * 1000),
            file_id=stored.id,
            params=params,
            finished_at=_now(),
        )
        await assets.delete_files(replaced)
        return JobOutcome("done")
    except asyncio.CancelledError:
        composing.cancel()
        await ended_without("it was cancelled", status=JOB_CANCELLED, error="Cancelled.")
        return JobOutcome("done")
    except MusicProviderBusyError as exc:
        await jobs.mark("music", music_id, stage="Waiting to ask the music provider again")
        if exc.retry_after:
            # The wait the provider named: held for it, no attempt spent, as a rate-limited shot is.
            return JobOutcome("hold", str(exc)[:2000], wait=float(exc.retry_after))
        return JobOutcome("retry", str(exc)[:2000])
    except Exception as exc:
        logger.warning("AI track %s failed: %s", music_id, format_exception_message(exc), exc_info=True)
        return JobOutcome("retry", f"The music failed: {format_exception_message(exc)}"[:2000])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ── Dead letters ──────────────────────────────────────────────────────────

#: The error the reconciler writes on an execution past its deadline (``orchestration_reconciler``).
TIMED_OUT_ERROR = "Execution timed out"


async def dead_letter(
    kind: str, job_id: object, error: str | None, *, lease: LeaseExpiry | None = None, failure: dict | None = None
) -> None:
    """The engine gave up: the row says failed, with the last error and how far the job had gone.

    *lease* is the lease expiry that ended the last attempt: the row
    then says where the job was and when its worker was last heard from,
    never that it crashed, which nobody saw. *failure* is a render's
    platform cause (``VideoRender.failure``), written with it.
    """
    row = await jobs.load(kind, job_id)
    if row is None or row.status in JOB_TERMINAL_STATUSES:
        return
    stage = getattr(row, "stage", None)
    progress = getattr(row, "progress", 0)
    where = (f" at {progress}%" + (f" ({stage})" if stage else "")) if progress else ""
    if lease is not None and lease.why and kind == "take":
        # Taken back from a worker that went: what ended it named, a stop or a drain included, never a bare lease.
        why = f"{lease.why[:1].upper()}{lease.why[1:]}"
        message = f"{why}{where}, {lease.describe()}, with no attempt left: record it again."
    elif lease is not None:
        message = f"Lease expired{where}, {lease.describe()}, with no attempt left."
    else:
        message = (error or "The job did not finish.") + (f" It was{where}." if where else "")
    if kind == "render" and (error or "").startswith(TIMED_OUT_ERROR):
        # A render out of its time limit says what its relaunches cost it and what was given back (#11782).
        from app.services.video_studio.render_process import relaunch_cost_words

        if said := relaunch_cost_words(getattr(row, "relaunches", None)):
            message = f"{message} {said}"
    if kind == "clip":
        await _end_clip(row, JOB_FAILED, message)
        return
    if kind == "music":
        from app.services.video_studio import music

        # An extension given up on: its track plays the take it kept, never left without one.
        params = dict(getattr(row, "params", None) or {})
        if (kept := music.kept_take(params, message)) is not None:
            await jobs.mark(kind, job_id, finished_at=_now(), params=params, **kept)
            return
    await jobs.mark(
        kind,
        job_id,
        status=JOB_FAILED,
        error=message[:2000],
        finished_at=_now(),
        stage=None,
        **({"failure": failure} if failure is not None and kind == "render" else {}),
    )


async def _end_clip(row: Any, status: str, message: str) -> None:
    """End an AI shot the engine gave up on: failed, or ready on the take it kept; what it replaced deleted."""
    from app.services.video_studio import clips

    params = dict(row.params or {})
    replaced = list(params.pop(clips.REPLACED_MEDIA, None) or [])
    changes = {"stage": None, **clips.end_attempt(params, status=status, reason=message)}
    await jobs.mark(
        "clip",
        row.id,
        finished_at=_now(),
        params=params,
        provider_state=None,
        provider_eta_at=None,
        **changes,
    )
    await assets.delete_files(replaced)
