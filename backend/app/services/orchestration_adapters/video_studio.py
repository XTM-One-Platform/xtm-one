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

"""Video Studio adapters: a recording, a render and an AI clip, run by the worker.

Each adapter has a dedicated concurrency budget (a render holds Chromium and
ffmpeg for minutes and must not starve assignments, nor they it) and the fast
cancel poll, so a cancel stops a render between frames. The runners own their
sessions; the adapter releases its own before handing over. ``retry`` maps to
a retryable failure the engine backs off. ``hold`` - a provider that rate
limited a request it never started - hands the execution back to wait
(``orchestration_engine.defer``: no attempt spent) for the wait the provider
named, else a short back-off, each jittered so a batch refused together
spreads out; past :data:`RATE_LIMIT_HOLD_SECONDS` the job fails saying so. A
shot held for a provider that cools for its quota (``provider_cooling``)
waits until ``provider_cooling.MAX_HOLD`` instead: a quota is no
failure of the shot. Once a job is done with - its run returned, or the
engine gave up on it - the conversation it is owed to is resumed when nobody
told it how the job ended
(``video_studio.wake``), and :class:`VideoStudioWakeSweepAdapter`
resumes one whose wake a stopped worker took along.
:class:`VideoFigureStillsAdapter` draws the editor's figure stills off the
save request (``figure_stills``). Scene previews run on a lane of
their own, :class:`VideoPreviewAdapter`, beside the whole renders.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.core.cancellation import run_to_completion
from app.core.utils import format_exception_message
from app.models.orchestration import OrchestrationExecution
from app.models.video_studio import JOB_PENDING, JOB_TERMINAL_STATUSES
from app.services.orchestration_adapters.base import AdapterContext, AdapterResult, OrchestrationAdapter
from app.services.video_studio import figure_stills, job_events, provider_cooling, render_ranges, render_retry, wake
from app.services.video_studio.jobs import (
    CLIP_ADAPTER,
    HANDED_OVER_KEY,
    HANDED_OVER_STATE,
    MUSIC_ADAPTER,
    PREVIEW_ADAPTER,
    RENDER_ADAPTER,
    RENDER_LEASE_SECONDS,
    RESUMING_STATES,
    SLOT_GIVEN_STATE,
    TAKE_ADAPTER,
    resume_wait,
)

if TYPE_CHECKING:
    from app.services.video_studio.recording_lane import Slot

logger = logging.getLogger(__name__)

__all__ = ["HANDED_OVER_KEY", "HANDED_OVER_STATE", "HANDOVER_REASON"]

#: What a render its stopping worker handed back to the queue says (``HANDED_OVER_STATE``).
HANDOVER_REASON = "Its worker was stopped: the next worker resumes it first."
#: What a scene preview that gave its lent render slot back says (``jobs.SLOT_GIVEN_BACK_STATE``).
SLOT_GIVEN_BACK_REASON = "It gave its lent render slot back to a render a stopped worker left: it resumes first."
#: The ``adapter_state`` of a scene preview waiting for its worker's memory to hold it beside the renders.
MEMORY_WAITING_STATE = "preview_memory_waiting"
#: Where the execution of a scene preview waiting for memory records since when, and why it began.
MEMORY_WAIT_KEY = "preview_memory_wait_since"
MEMORY_WHY_KEY = "preview_memory_wait_why"


def memory_waited(execution: OrchestrationExecution) -> tuple[float, str]:
    """``(seconds, since)``: how long the scene preview of *execution* has waited for memory, and since when.

    Counted from the first of its memory waits in a row: one that follows a run, or a wait of another kind (its
    video's previews, a stopped worker), starts at once.
    """
    now = datetime.now(UTC)
    recorded = (getattr(execution, "payload", None) or {}).get(MEMORY_WAIT_KEY)
    if getattr(execution, "adapter_state", None) == MEMORY_WAITING_STATE and isinstance(recorded, str):
        with contextlib.suppress(ValueError):
            since = datetime.fromisoformat(recorded)
            since = since if since.tzinfo else since.replace(tzinfo=UTC)
            return max(0.0, (now - since).total_seconds()), since.isoformat()
    return 0.0, now.isoformat()


def handed_over_by(worker_id: str) -> dict:
    """The execution payload a stopping worker hands a render back with: who it was, its version, the signal,
    and how long the render waits for the next worker - as long as this worker's deployment lets a rolling restart
    take (``jobs.resume_wait``)."""
    from app.core.config import settings
    from app.services.orchestration_reconciler import stop_signal

    return {
        HANDED_OVER_KEY: {
            "worker": worker_id,
            "version": settings.app_version,
            "signal": stop_signal(),
            "at": datetime.now(UTC).isoformat(),
            "resume_wait_seconds": int(resume_wait().total_seconds()),
        }
    }


#: How long a render a stopping worker hands back is held for a worker of the incoming release: only it
#: claims the render that long, any worker after - one that went meanwhile never strands it.
INCOMING_HOLD_SECONDS = 90.0


async def incoming_hand(worker_id: str) -> tuple[str, float] | None:
    """``(worker, seconds)``: the running worker of the incoming release a stopping worker hands a render to,
    ``None`` when none runs - a worker of the stopping one's own release is stopped by the same rolling update next.
    Never what keeps a render from being handed back."""
    from app.services import worker_presence

    try:
        taker = await worker_presence.incoming_worker(RENDER_ADAPTER, me=worker_id)
    except Exception as exc:
        logger.debug("Incoming worker not read: %s", format_exception_message(exc))
        return None
    return (taker, INCOMING_HOLD_SECONDS) if taker else None


def resumed_after(execution: OrchestrationExecution) -> str | None:
    """What a render resumes after, when a stopped worker left it; ``None`` for one no worker left.

    Handed back by its stopping worker: ``after a deploy (1.4.1 -> 1.4.2)``
    or ``after its worker restarted (version 1.4.2)``, from that worker's
    version. Taken back from a worker gone without a word: the same, then how
    it went - the signal and the memory at its last word
    (``worker_presence.stop_reason``).
    """
    from app.services import worker_presence
    from app.services.orchestration_engine import LEASE_EXPIRY_PAYLOAD_KEY

    payload = getattr(execution, "payload", None) or {}
    handed = payload.get(HANDED_OVER_KEY)
    if getattr(execution, "adapter_state", None) == HANDED_OVER_STATE and isinstance(handed, dict):
        return worker_presence.resumed_after(handed.get("version"))
    expiry = payload.get(LEASE_EXPIRY_PAYLOAD_KEY)
    # The expiry of the attempt before this one: the claim that runs it counted a new one.
    if not isinstance(expiry, dict) or expiry.get("attempt") != (getattr(execution, "attempt_count", 0) or 0) - 1:
        return None
    if not isinstance(expiry.get("why"), str):
        return None
    record = expiry.get("owner_record") if isinstance(expiry.get("owner_record"), dict) else {}
    why = expiry["why"]
    return f"{worker_presence.resumed_after(record.get('version'))}: {why[:1].lower()}{why[1:]}"

#: A rate-limited job the provider named no wait for is asked again after this long, doubling ...
RATE_LIMIT_FIRST_WAIT_SECONDS = 10.0
#: ... up to this: the limits that refuse a generation are counted per minute.
RATE_LIMIT_MAX_WAIT_SECONDS = 60.0
#: How long a job is held while its provider keeps refusing it as rate limited.
RATE_LIMIT_HOLD_SECONDS = 30 * 60
#: The ``adapter_state`` of a held job (never ``ai_hold``'s ``held:`` prefix: AI is available).
RATE_LIMITED_STATE = "rate_limited"


def rate_limit_base(deferrals: int, asked: float | None) -> float:
    """The wait the provider *asked* for, else a back-off doubling from :data:`RATE_LIMIT_FIRST_WAIT_SECONDS`
    up to :data:`RATE_LIMIT_MAX_WAIT_SECONDS`, after *deferrals* earlier ones."""
    if asked is not None and asked > 0:
        return asked
    return min(RATE_LIMIT_MAX_WAIT_SECONDS, RATE_LIMIT_FIRST_WAIT_SECONDS * 2 ** min(max(0, deferrals), 6))


def _spread(base: float) -> float:
    """*base* up to a quarter longer at random (a minute at most, for a provider that cools for hours), so the
    jobs of a batch refused in the same second are not refused together again."""
    return base + random.uniform(0.0, min(base * 0.25, RATE_LIMIT_MAX_WAIT_SECONDS))  # noqa: S311 - spreading retries


def rate_limit_wait(deferrals: int, asked: float | None) -> float:
    """Seconds until a rate-limited job is asked again, after *deferrals* earlier ones (:func:`rate_limit_base`, spread)."""
    return _spread(rate_limit_base(deferrals, asked))


#: A recording whose application did not answer (5xx or 429 past the probe's own retries) is tried again after
#: this long, doubling up to :data:`UNANSWERED_MAX_WAIT_SECONDS` ...
UNANSWERED_FIRST_WAIT_SECONDS = 30.0
UNANSWERED_MAX_WAIT_SECONDS = 300.0
#: ... for this long at most: a deploy or a restart, never an outage nobody is fixing.
UNANSWERED_HOLD_SECONDS = 30 * 60
#: The ``adapter_state`` of a recording waiting for its application to answer.
UNANSWERED_STATE = "application_unanswered"


def unanswered_wait(deferrals: int) -> float:
    """Seconds until a recording whose application did not answer is tried again, after *deferrals* earlier ones."""
    return min(UNANSWERED_MAX_WAIT_SECONDS, UNANSWERED_FIRST_WAIT_SECONDS * 2 ** min(max(0, deferrals), 6))


def _slot_given_back(outcome, worker_id: str) -> AdapterResult:
    """A render, or a range of one, whose slot went to one that held a slot before it: back in its place in the
    queue, its attempt unspent - due now on any other worker, never on *worker_id* while the render it gave its slot to
    is in its hand there (``orchestration_reconciler.GIVE_BACK_HOLD_SECONDS``)."""
    from app.services.orchestration_reconciler import GIVE_BACK_HOLD_SECONDS

    return AdapterResult.defer(
        outcome.error or "It gave its slot back to a render that held one before it.",
        delay_seconds=GIVE_BACK_HOLD_SECONDS,
        adapter_state=SLOT_GIVEN_STATE,
        place="keep",
        not_for=worker_id,
    )


def _ended(kind: str, job_id: object) -> None:
    """The worker is done with a job: wake the conversation it is owed to and raise its platform event.

    Both read the row and act only on a job that ended; a job still in flight (retried, handed back) is left alone.
    """
    wake.schedule(kind, job_id)
    job_events.schedule(kind, job_id)


def _result(outcome) -> AdapterResult:
    if outcome.kind == "retry":
        return AdapterResult.failed(outcome.error or "Video Studio job failed", retryable=True, backoff_seconds=30)
    if outcome.kind == "failed":
        return AdapterResult.failed(outcome.error or "Video Studio job failed", retryable=False)
    return AdapterResult.completed()


class _VideoStudioAdapter(OrchestrationAdapter):
    kind = ""

    @property
    def cancel_poll_seconds(self) -> float | None:
        return self._configured_cancel_poll()

    async def _run(self, job_id: str, cancel_event: asyncio.Event | None):  # pragma: no cover - overridden
        raise NotImplementedError

    async def execute(self, ctx: AdapterContext, execution: OrchestrationExecution) -> AdapterResult:
        job_id = (execution.payload or {}).get(f"{self.kind}_id")
        if not job_id:
            return AdapterResult.failed(f"{self.name} execution payload missing {self.kind}_id", retryable=False)
        await ctx.db.commit()
        outcome = await self._run(str(job_id), getattr(ctx, "cancel_event", None))
        result = await self._held(str(job_id), execution, outcome) if outcome.kind == "hold" else _result(outcome)
        _ended(self.kind, job_id)
        return result

    async def _held(self, job_id: str, execution: OrchestrationExecution, outcome) -> AdapterResult:
        """A job its provider rate limited: deferred for :func:`rate_limit_wait`, or failed once held too long.

        Too long is :data:`RATE_LIMIT_HOLD_SECONDS`, or ``provider_cooling.MAX_HOLD`` for a provider that cools
        for its quota: that one names its wait, and the shots of a batch wait it out together. The bound
        reads the wait itself, never the random spread added to it: a quota whose ``Retry-After`` is the whole
        day the cooling honours is waited out, not failed for its spread. A shot whose provider is busy
        (``outcome.busy``) waits the exact time its row says (``provider_waits``, spread already), up to the
        same day.
        """
        from app.services.orchestration_engine import hold_record, hold_started_at
        from app.services.video_studio.runners import dead_letter

        since = hold_started_at(execution)
        held = (datetime.now(UTC) - since).total_seconds() if since is not None else 0.0
        if outcome.busy and outcome.wait:
            base = wait = float(outcome.wait)
        else:
            base = rate_limit_base(int((hold_record(execution) or {}).get("count") or 0), outcome.wait)
            wait = _spread(base)
        reason = outcome.error or "The provider rate limited the request."
        limit = provider_cooling.MAX_HOLD.total_seconds() if outcome.quota or outcome.busy else RATE_LIMIT_HOLD_SECONDS
        if held + base > limit:
            longest = f"{limit / 3600:g} h" if limit >= 7200 else f"{round(limit / 60)} min"
            unavailable = "Busy" if outcome.busy else "Rate limited"
            reason = (
                f"{reason} {unavailable} for {round(held / 60)} min with {round(wait)} s more to wait: past the "
                f"{longest} a job waits for its provider. Generate it again later."
            )
            await dead_letter(self.kind, job_id, reason)
            return AdapterResult.failed(reason, retryable=False)
        return AdapterResult.defer(reason, delay_seconds=wait, adapter_state=RATE_LIMITED_STATE)

    async def on_dead_letter(self, ctx: AdapterContext, execution: OrchestrationExecution) -> None:
        from app.services.orchestration_engine import lease_expiry
        from app.services.video_studio.runners import dead_letter

        job_id = (execution.payload or {}).get(f"{self.kind}_id")
        await ctx.db.commit()
        await dead_letter(self.kind, job_id, execution.last_error, lease=lease_expiry(execution))
        if job_id:
            _ended(self.kind, job_id)


class VideoTakeAdapter(_VideoStudioAdapter):
    """The recording lane of the render queue (``recording_lane``): a take measures its worker's capture rate
    in its slot, and one short of its fps beside other jobs waits in its place - claimed again every
    ``recording_lane.POLL_SECONDS``, measured again only once its worker's load lightened -, never refused for them."""

    name = TAKE_ADAPTER
    kind = "take"
    # A take its worker went with (a rollout, a node drain) is taken back at once, first in the lane, and one
    # whose worker said it was stopping keeps its attempt (``orchestration_engine.stopped_on_its_word``).
    resume_when_worker_gone = True
    # A worker lost without a stop word (a rollout kill, a node lost; no out-of-memory sign) costs it no attempt
    # either: finished from its stored keyframes or recorded again from its script, a run of back-to-back deploys
    # never exhausts its two attempts (bounded by the engine, as a render's, #9812).
    gives_back_silent_losses = True

    @property
    def concurrency_limit(self) -> int | None:
        # A recording lane (``VIDEO_STUDIO_RECORDING_GL=gpu``) records one take at a time (``take_concurrency``).
        from app.services.video_studio.capture_rate import take_concurrency

        return max(1, take_concurrency())

    @property
    def claims_here(self) -> bool:
        # ``VIDEO_STUDIO_TAKE_CONCURRENCY=0``: a worker that records nothing, the recording lane kept to the
        # workers that do - a GPU pool, whose Chromium draws the takes' canvases on its GPU. A recording lane
        # (``VIDEO_STUDIO_RECORDING_GL=gpu``) claims none until its GPU is found, never one on SwiftShader.
        from app.services.video_studio.capture_rate import recording_refused, take_concurrency

        return take_concurrency() > 0 and recording_refused() is None

    def presence(self) -> dict:
        # A recording lane says its GL decision in its presence record: a queued take then names the workers
        # that refuse recordings and why, instead of waiting silently for one until its deadline. Every recording
        # worker says its class and GPU model (``worker_class``): what record_browser_demo(worker_class=...) asks.
        from app.services.video_studio.capture_rate import lane_gl, worker_record

        said = lane_gl()
        found: dict = {"recording_gl": said} if said is not None else {}
        if (own := worker_record()) is not None:
            found.update(recording_class=own["class"], recording_gpu=own["gpu"])
        return found

    async def deal_claims(self, db, due: list, worker_id: str) -> list:
        # A take asking for a worker class is claimed only by a worker of that class, in its place in the lane; one
        # asking for none by any recording worker, oldest first as before.
        from app.services.video_studio import worker_class
        from app.services.video_studio.capture_rate import worker_record

        own = worker_record()
        return worker_class.claimable(due, own["class"] if own else None)

    async def _run(self, job_id: str, cancel_event: asyncio.Event | None, *, lane: Slot | None = None):
        from app.services.video_studio.runners import run_take

        return await run_take(job_id, cancel_event=cancel_event, lane=lane)

    async def execute(self, ctx: AdapterContext, execution: OrchestrationExecution) -> AdapterResult:
        from app.services.orchestration_engine import end_hold
        from app.services.video_studio import jobs, recording_lane

        job_id = (execution.payload or {}).get("take_id")
        if not job_id:
            return AdapterResult.failed(f"{self.name} execution payload missing take_id", retryable=False)
        wait = recording_lane.wait_of(execution)
        now = datetime.now(UTC)
        if wait is not None:
            load = await recording_lane.load_on(ctx.db, ctx.worker_id, excluding=execution.id)
            if recording_lane.held(wait, ctx.worker_id, load, now):
                # Its worker's load no lighter than the one it fell short beside: it waits again, nothing measured.
                wait = recording_lane.again(wait, load)
                await ctx.db.commit()
                await jobs.mark_if_open("take", str(job_id), stage=wait.stage())
                return self._waits(wait.stage(), wait.as_dict())
            # It measures again: short again, it waits once more from the same start; passed, a retry waits for nothing.
            await end_hold(ctx.db, execution_id=execution.id, worker_id=ctx.worker_id, held_adapter_state=True)
        await ctx.db.commit()
        if relaunched_take(execution):
            # Relaunched after its worker went: its deadline bounded the wait for this worker, its run gets a whole
            # take's time again from this claim, never the rest its first run left (#9885).
            await _restart_deadline(ctx, execution)
        slot = recording_lane.Slot(
            ctx.worker_id,
            execution.id,
            waited=wait,
            may_wait=wait is None or not wait.expired(now),
            relaunched=relaunched_take(execution),
        )
        stopping = getattr(ctx, "stopping", None)
        try:
            outcome = await self._run(str(job_id), getattr(ctx, "cancel_event", None), lane=slot)
        except asyncio.CancelledError:
            # Torn down as its worker stops: handed back now, first in the lane, its attempt unspent.
            if stopping is not None and stopping.is_set():
                await run_to_completion(_hand_back_take(execution.id, ctx.worker_id))
            raise
        if outcome.kind == "wait":
            return self._waits(outcome.error or "Waiting in the recording lane.", outcome.lane)
        if outcome.kind == "hold":
            return await self._unanswered(str(job_id), execution, outcome)
        _ended(self.kind, job_id)
        return _result(outcome)

    async def _unanswered(self, job_id: str, execution: OrchestrationExecution, outcome) -> AdapterResult:
        """A take whose application did not answer before step 1 (``runners.run_take``): deferred, no attempt
        spent, for :func:`unanswered_wait`; failed once it waited :data:`UNANSWERED_HOLD_SECONDS`, saying so."""
        from app.services.orchestration_engine import hold_record, hold_started_at
        from app.services.video_studio.runners import dead_letter

        since = hold_started_at(execution)
        held = (datetime.now(UTC) - since).total_seconds() if since is not None else 0.0
        wait = unanswered_wait(int((hold_record(execution) or {}).get("count") or 0))
        reason = outcome.error or "The application did not answer."
        if held + wait > UNANSWERED_HOLD_SECONDS:
            reason = f"{reason}; it did not answer for {round(held / 60)} min, so the recording stopped: record it again once it does."
            await dead_letter(self.kind, job_id, reason)
            _ended(self.kind, job_id)
            return AdapterResult.failed(reason, retryable=False)
        return AdapterResult.defer(reason, delay_seconds=_spread(wait), adapter_state=UNANSWERED_STATE)

    async def on_dead_letter(self, ctx: AdapterContext, execution: OrchestrationExecution) -> None:
        # A relaunched take its deadline caught with an attempt left runs once more from its script, never failed.
        await ctx.db.commit()
        try:
            if await relaunch_after_deadline(execution):
                return
            # One no worker ever claimed (a rollout, no recording worker serving the lane) goes back first in the lane.
            if await requeue_unclaimed_after_deadline(execution):
                return
        except Exception as exc:  # failed as before, saying why
            logger.warning("Video take execution %s not relaunched: %s", execution.id, format_exception_message(exc))
        await super().on_dead_letter(ctx, execution)

    @staticmethod
    def _waits(reason: str, wait: dict | None) -> AdapterResult:
        """Back in its place in the recording lane, no attempt spent, claimed again in ``recording_lane.POLL_SECONDS``."""
        from app.services.video_studio import recording_lane

        # A relaunch short of its fps on its worker measures again less often: nothing beside it to lighten.
        relaunch = isinstance(wait, dict) and wait.get("relaunch") is True
        return AdapterResult.defer(
            reason,
            delay_seconds=recording_lane.RELAUNCH_POLL_SECONDS if relaunch else recording_lane.POLL_SECONDS,
            adapter_state=recording_lane.WAITING_STATE,
            payload_update={recording_lane.WAIT_KEY: wait},
            place="keep",
        )


def relaunched_take(execution: OrchestrationExecution | None) -> bool:
    """Whether a take's execution runs again after a worker went with it: handed back by its stopping worker
    (:data:`HANDED_OVER_KEY`) or given its attempt back for a worker lost (``orchestration_engine.infra_relaunches``).

    Its capture pre-flight short of its fps then waits in the recording lane for a worker that sustains it
    (``recording_lane.RELAUNCH_MAX_WAIT``) instead of refusing the take: a worker just booted by the rollout that
    stopped the first one often measures under its rate for its first minutes (#9812)."""
    from app.services.orchestration_engine import infra_relaunches

    if execution is None:
        return False
    payload = getattr(execution, "payload", None) or {}
    return isinstance(payload.get(HANDED_OVER_KEY), dict) or infra_relaunches(execution) > 0


def fresh_take_deadline(execution: OrchestrationExecution | None) -> None:
    """Give a take handed back by its stopping worker a whole take's deadline again (``jobs.take_deadline``).

    ``defer`` keeps the runnable time the take had left; a long take stopped in its passes or its encode had little,
    and the next worker's run from its keyframes was cut by that deadline and ended as if cancelled (#9780). The
    hand-back spends no attempt, so it spends no time either. Never moves a deadline in."""
    from app.services.video_studio.jobs import take_deadline

    if execution is None or execution.deadline_at is None:
        return
    fresh = take_deadline()
    if execution.deadline_at < fresh:
        execution.deadline_at = fresh


async def _restart_deadline(ctx: AdapterContext, execution: OrchestrationExecution) -> None:
    """Push a relaunched take's deadline out to a whole take's from now, as the worker that just claimed it; the task's
    own timeout follows at once. A failure leaves the deadline it had: logged, never raised."""
    from app.services.orchestration_engine import extend_deadline
    from app.services.orchestration_reconciler import follow_deadline
    from app.services.video_studio.jobs import take_deadline

    try:
        deadline_at = await extend_deadline(
            ctx.db, execution_id=execution.id, worker_id=ctx.worker_id, deadline_at=take_deadline()
        )
        await ctx.db.commit()
    except Exception as exc:
        logger.warning(
            "Video take execution %s: deadline not restarted: %s", execution.id, format_exception_message(exc)
        )
        with contextlib.suppress(Exception):
            await ctx.db.rollback()
        return
    if deadline_at is not None:
        execution.deadline_at = deadline_at
        follow_deadline(execution.id, deadline_at)


#: Where the execution of a take run once more after its deadline records it: never a second time.
DEADLINE_RELAUNCH_KEY = "deadline_relaunch"
#: What a relaunched take dead-lettered on its deadline says while it waits to be recorded again from its script.
TAKE_DEADLINE_RELAUNCH_STAGE = (
    "Its resume after a worker stop ran past its deadline: the next worker records it again from its script"
)
#: The same, for one no worker took before its deadline: queued again with a whole take's time, its resume kept.
TAKE_DEADLINE_REQUEUED_STAGE = (
    "No worker took it back before its deadline: first in the lane again with a whole take's time"
)


async def relaunch_after_deadline(execution: OrchestrationExecution) -> bool:
    """Run once more, recorded again from its script, a relaunched take dead-lettered on its deadline with an attempt
    left; whether it does (#9885).

    Only a take relaunched after its worker went (:func:`relaunched_take`), only on a deadline, only once
    (:data:`DEADLINE_RELAUNCH_KEY`), only with an attempt left: its execution goes back first in the lane on its next
    attempt (``orchestration_engine.relaunch_dead_lettered``: the attempts it used kept), with a whole take's deadline.
    Caught under a worker, its keyframes resume point is dropped so the next run records it again from its script
    instead of stalling on them again; caught in the queue, it keeps it."""
    from app.core.database import async_session
    from app.services.orchestration_engine import notify_orchestration_wake, relaunch_dead_lettered
    from app.services.video_studio import jobs
    from app.services.video_studio.jobs import take_deadline
    from app.services.video_studio.runners import resume_point_dropped

    payload = execution.payload or {}
    take_id = payload.get("take_id")
    if not take_id or payload.get(DEADLINE_RELAUNCH_KEY) is not None:
        return False
    if not (execution.last_error or "").startswith("Deadline exceeded") or not relaunched_take(execution):
        return False
    if int(execution.attempt_count or 0) >= int(execution.max_attempts or 0):
        return False
    take = await jobs.load("take", take_id)
    if take is None or take.status in JOB_TERMINAL_STATUSES:
        return False
    progress, stage = int(getattr(take, "progress", 0) or 0), getattr(take, "stage", None)
    first = (execution.last_error or "Deadline exceeded.").rstrip(".")
    said = f"{first}, at {progress}%" + (f" ({stage})" if stage else "")
    # Caught under a worker (``sweep_deadlines`` names the holder): its resume stalled there, so it is recorded again
    # from its script. Caught in the queue, no worker took it in time: it keeps what it resumes from.
    stalled = "(held by " in (execution.last_error or "")
    record = {
        "at": datetime.now(UTC).isoformat(),
        "reason": said[:500],
        "progress": progress,
        "stage": stage,
        "stalled": stalled,
    }
    if stalled:
        await resume_point_dropped(str(take_id))
    if not await jobs.mark_if_open(
        "take",
        str(take_id),
        status=JOB_PENDING,
        started_at=None,
        progress_at=None,
        progress=0,
        stage=TAKE_DEADLINE_RELAUNCH_STAGE if stalled else TAKE_DEADLINE_REQUEUED_STAGE,
    ):
        return False
    async with async_session() as db:
        relaunched = await relaunch_dead_lettered(
            db,
            execution_id=execution.id,
            deadline_at=take_deadline(),
            reason=f"{said}: {'recorded again from its script' if stalled else 'first in the lane again'} "
            f"({execution.attempt_count} of {execution.max_attempts} attempts used).",
            payload_update={DEADLINE_RELAUNCH_KEY: record},
        )
        await db.commit()
    if relaunched is None:
        return False
    logger.warning("Video take %s: %s; run once more (stalled under a worker: %s).", take_id, said, stalled)
    with contextlib.suppress(Exception):
        await notify_orchestration_wake()
    return True


#: Where the execution of a take no worker claimed before its deadline counts the times it went back to the lane.
UNCLAIMED_REQUEUES_KEY = "unclaimed_requeues"
#: The times a take no worker claimed goes back first in the lane on its deadline (each with a whole take's time):
#: past it, it fails saying it never started and whether a worker served the lane.
MAX_UNCLAIMED_REQUEUES = 3
#: What a take back in the lane after its deadline caught it unclaimed says while it waits.
TAKE_UNCLAIMED_REQUEUED_STAGE = "No worker took it before its deadline: first in the lane again with a whole take's time"


def never_claimed(execution: OrchestrationExecution) -> bool:
    """Whether a take's *execution* was caught by its deadline in the queue without any worker ever claiming it: no
    attempt spent, no worker holding it, no wait for a worker's load, not relaunched after a worker loss."""
    from app.services.video_studio import recording_lane

    payload = execution.payload or {}
    return (
        (execution.last_error or "").startswith("Deadline exceeded")
        and "(held by " not in (execution.last_error or "")
        and int(execution.attempt_count or 0) == 0
        and payload.get(recording_lane.WAIT_KEY) is None
        and not relaunched_take(execution)
    )


async def requeue_unclaimed_after_deadline(execution: OrchestrationExecution) -> bool:
    """Put a take no worker ever claimed (:func:`never_claimed`) back first in the lane with a whole take's deadline,
    at most :data:`MAX_UNCLAIMED_REQUEUES` times; whether it does. Its attempts are untouched (none was spent).

    Past the bound - or when it cannot go back - its last error is rewritten to say the stage: it never started, the
    times it went back, and whether a worker served the recording lane (``recording_lane.unserved_words``)."""
    from app.core.database import async_session
    from app.services.orchestration_engine import notify_orchestration_wake, relaunch_dead_lettered
    from app.services.video_studio import jobs, recording_lane
    from app.services.video_studio.jobs import take_deadline

    payload = execution.payload or {}
    take_id = payload.get("take_id")
    if not take_id or not never_claimed(execution):
        return False
    take = await jobs.load("take", take_id)
    if take is None or take.status in JOB_TERMINAL_STATUSES:
        return False
    count = payload.get(UNCLAIMED_REQUEUES_KEY)
    count = count if isinstance(count, int) and not isinstance(count, bool) else 0
    unserved = None
    with contextlib.suppress(Exception):
        unserved = await recording_lane.unserved_words()
    served = f"no worker took recordings: {unserved}" if unserved else "a recording worker was present but did not take it"
    if count >= MAX_UNCLAIMED_REQUEUES:
        execution.last_error = (
            f"Deadline exceeded before it started: no worker took it in the recording lane, back in the lane "
            f"{count} time{'s' if count != 1 else ''} ({served})."
        )[:2000]
        return False
    if not await jobs.mark_if_open(
        "take", str(take_id), status=JOB_PENDING, started_at=None, progress_at=None, progress=0,
        stage=TAKE_UNCLAIMED_REQUEUED_STAGE,
    ):
        return False
    async with async_session() as db:
        relaunched = await relaunch_dead_lettered(
            db,
            execution_id=execution.id,
            deadline_at=take_deadline(),
            reason=f"Deadline exceeded before any worker took it ({served}): first in the lane again "
            f"({count + 1} of {MAX_UNCLAIMED_REQUEUES}).",
            payload_update={UNCLAIMED_REQUEUES_KEY: count + 1},
        )
        await db.commit()
    if relaunched is None:
        return False
    logger.warning("Video take %s: no worker took it before its deadline (%s); back in the lane.", take_id, served)
    with contextlib.suppress(Exception):
        await notify_orchestration_wake()
    return True


#: What a take its stopping worker handed back to the queue says (``HANDED_OVER_STATE``).
TAKE_HANDOVER_REASON = "Its worker was stopped: the next worker records it again from its script."


async def _hand_back_take(execution_id, worker_id: str) -> None:
    """Give a take's execution back to the queue as its worker stops: first in the lane, due now, its attempt unspent.

    Any worker of the recording lane claims it: a lane is often one GPU worker, which a plain restart replaces with
    a worker of the same release. One killed before this write is taken back on its worker's last word instead
    (``resume_when_worker_gone``), its attempt given back the same way.
    """
    from app.core.database import async_session
    from app.services.orchestration_engine import LeaseLostError, defer, notify_orchestration_wake

    try:
        async with async_session() as db:
            await defer(
                db,
                execution_id=execution_id,
                worker_id=worker_id,
                reason=TAKE_HANDOVER_REASON,
                delay_seconds=0.0,
                adapter_state=HANDED_OVER_STATE,
                payload_update=handed_over_by(worker_id),
                place="head",
            )
            fresh_take_deadline(await db.get(OrchestrationExecution, execution_id))
            await db.commit()
    except LeaseLostError:
        return
    except Exception as exc:  # its worker's presence goes: the next worker takes it back at once
        logger.warning("Video take execution %s not handed back: %s", execution_id, format_exception_message(exc))
        return
    with contextlib.suppress(Exception):
        await notify_orchestration_wake()


class VideoRenderAdapter(_VideoStudioAdapter):
    """A render, in a process of its own: its crash fails it, never the worker.

    Its executions hold a lease of their own (``RENDER_LEASE_SECONDS``),
    and one the engine dead-lettered for a lease that lapsed while the render
    still moved runs again from its finished segments
    (``render_process.resume_after_lost_lease``) instead of failing.
    """

    name = RENDER_ADAPTER
    kind = "render"
    lease_seconds = RENDER_LEASE_SECONDS
    # Resumed from its finished segments, at once and first, when its worker goes.
    resume_when_worker_gone = True
    # Its slot kept from its first claim to its end: handed back by a stopping worker or taken back from a
    # worker gone, it resumes in a slot kept for it, or given back by the render that took one after it.
    keeps_slot_through = RESUMING_STATES
    # A worker lost without a stop request (no out-of-memory sign) costs it no attempt: its frames are kept and it
    # resumes from them, so a rolling deploy that loses nodes does not exhaust max_attempts (bounded by the engine).
    gives_back_silent_losses = True
    # Its slot, idle on a worker, takes scene previews while the preview lane's own are taken: a whole
    # render asked meanwhile waits for that one preview, a render a stopped worker left takes it back.
    lends_idle_slots_to = PREVIEW_ADAPTER
    takes_back_lent_slots_for = RESUMING_STATES

    async def on_dead_letter(self, ctx: AdapterContext, execution: OrchestrationExecution) -> None:
        from app.services.orchestration_engine import lease_expiry
        from app.services.video_studio import render_ranges
        from app.services.video_studio.render_process import resume_after_lost_lease

        if render_ranges.is_range(execution.payload):
            # A scene range given up on: the render draws what it lacks, the render's own row untouched.
            await ctx.db.commit()
            payload = execution.payload or {}
            row = await render_ranges.find(
                str(payload.get("render_id")), str(payload.get(render_ranges.DIGEST_KEY)), int(payload[render_ranges.RANGE_KEY])
            )
            if row is not None:
                await render_ranges.fail_open(row.id, execution.last_error or "Its execution was given up on.")
            return
        expiry = lease_expiry(execution)
        await ctx.db.commit()
        if expiry is not None and await resume_after_lost_lease(execution, expiry):
            return
        job_id = (execution.payload or {}).get("render_id")
        if expiry is not None and job_id:
            # Its worker's lease, not its video: run again once on its revision, else failed saying so.
            from app.services.video_studio.runners import dead_letter

            if await render_retry.requeue(execution, render_retry.LEASE, f"its lease lapsed ({expiry.describe()})"):
                return
            retried = render_retry.record_of(execution.payload)
            await dead_letter(
                self.kind,
                job_id,
                execution.last_error,
                lease=expiry,
                failure=render_retry.failure_record(render_retry.LEASE, retried=retried),
            )
            _ended(self.kind, job_id)
            return
        await super().on_dead_letter(ctx, execution)

    @property
    def concurrency_limit(self) -> int | None:
        from app.core.config import settings

        return max(1, settings.video_studio_render_concurrency)

    @property
    def claims_here(self) -> bool:
        # ``VIDEO_STUDIO_RENDER_CONCURRENCY=0``: a worker that draws scene previews only, no render nor range.
        from app.core.config import settings

        return settings.video_studio_render_concurrency > 0

    @property
    def cpus_reserved(self) -> int:
        # The CPUs its whole renders' pages reserve here: the shared pool goes to another worker first.
        from app.services.video_studio import render_process

        return render_process.cpus_reserved() if self.claims_here else 0

    def presence(self) -> dict:
        # What this worker's CPU went to over the last minute: what the pre-flight prices its other jobs at.
        from app.services.video_studio import cpu_share

        if not self.claims_here:
            return {}
        measured = cpu_share.WORKER_CPU.sample()
        return {"cpu": measured} if measured is not None else {}

    async def periodic(self, ctx: AdapterContext) -> None:
        # What this worker holds a render to, for the pre-flight of the next one: the web cannot read it.
        from app.services.video_studio import render_preflight, render_process

        if self.claims_here:
            await render_preflight.publish_worker()
        draining = getattr(ctx, "draining", None)
        if draining is not None and draining.is_set():
            # Asked to stop: a render under half its frames goes to a running worker now; those past half are
            # waited for by the stop.
            await render_process.hand_over_early()

    async def execute(self, ctx: AdapterContext, execution: OrchestrationExecution) -> AdapterResult:
        from app.services.orchestration_engine import LeaseLostError, end_hold, hold_record
        from app.services.video_studio import jobs
        from app.services.video_studio.render_process import run_isolated

        job_id = (execution.payload or {}).get("render_id")
        if not job_id:
            return AdapterResult.failed(f"{self.name} execution payload missing render_id", retryable=False)
        # Read before its hold ends: a render a stopped worker left says what it resumes after.
        resumed = resumed_after(execution)
        await ctx.db.commit()
        if render_ranges.is_range(execution.payload):
            return await self._range(ctx, execution, str(job_id))
        # A video runs its cap of scene previews at once; the next waits its turn, never refused, in its
        # place: never behind the previews queued after it.
        waiting = await jobs.preview_waits(job_id)
        if waiting is not None:
            await jobs.mark_if_open("render", job_id, stage=waiting[:200])
            return AdapterResult.defer(
                waiting,
                delay_seconds=jobs.PREVIEW_WAIT_SECONDS,
                adapter_state=jobs.PREVIEW_WAITING_STATE,
                place="keep",
            )
        if hold_record(execution) is not None:
            await end_hold(ctx.db, execution_id=execution.id, worker_id=ctx.worker_id, held_adapter_state=True)
            await ctx.db.commit()
        attempt = (execution.attempt_count or 1, execution.max_attempts or 1)
        stopping = getattr(ctx, "stopping", None)
        try:
            outcome = await run_isolated(
                str(job_id),
                cancel_event=getattr(ctx, "cancel_event", None),
                last_attempt=attempt[0] >= attempt[1],
                lease=(execution.id, ctx.worker_id),
                attempt=attempt,
                stopping=stopping,
                resumed=resumed,
                retried=render_retry.record_of(execution.payload),
                give_back=getattr(ctx, "give_back", None),
            )
        except asyncio.CancelledError:
            # Torn down as the worker stops, before the render process was handed over: hand it over now.
            if stopping is not None and stopping.is_set():
                await run_to_completion(_hand_back(execution.id, ctx.worker_id))
            raise
        if outcome.kind == "again":
            # The platform failed it, not its video: it runs again once, first in the queue, no attempt spent.
            return AdapterResult.defer(
                outcome.error or "The render runs again once.",
                delay_seconds=render_retry.RETRY_DELAY_SECONDS,
                adapter_state=render_retry.INFRA_RETRY_STATE,
                payload_update={render_retry.INFRA_RETRY_KEY: outcome.again},
                place="head",
            )
        if outcome.kind == "lease_lost":
            # The engine took the execution back: its row is the next
            # attempt's, or the dead letter's.
            logger.warning("Video render %s stopped: %s", job_id, outcome.error)
            raise LeaseLostError(outcome.error or f"Execution {execution.id} lease lost")
        if outcome.kind == "handover":
            # The worker stops: the next one takes it at once - first in the queue, a worker of the
            # incoming release first -, this attempt never counted.
            return AdapterResult.defer(
                outcome.error or HANDOVER_REASON,
                delay_seconds=0.0,
                adapter_state=HANDED_OVER_STATE,
                payload_update=handed_over_by(ctx.worker_id),
                place="head",
                claim_for=await incoming_hand(ctx.worker_id),
            )
        if outcome.kind == "given_back":
            # A scene preview whose lent render slot was taken back: first in its own lane, nothing spent.
            return AdapterResult.defer(
                outcome.error or SLOT_GIVEN_BACK_REASON,
                delay_seconds=0.0,
                adapter_state=jobs.SLOT_GIVEN_BACK_STATE,
                place="head",
            )
        if outcome.kind == "gave_back":
            return _slot_given_back(outcome, ctx.worker_id)
        _ended(self.kind, job_id)
        return _result(outcome)

    async def _range(self, ctx: AdapterContext, execution: OrchestrationExecution, render_id: str) -> AdapterResult:
        """One scene range of a render drawn on this worker: it ends the range, never the render."""
        from app.services.orchestration_engine import LeaseLostError
        from app.services.video_studio.render_process import run_range_isolated

        payload = execution.payload or {}
        attempt = (execution.attempt_count or 1, execution.max_attempts or 1)
        stopping = getattr(ctx, "stopping", None)
        try:
            outcome = await run_range_isolated(
                render_id,
                int(payload[render_ranges.RANGE_KEY]),
                str(payload.get(render_ranges.DIGEST_KEY) or ""),
                cancel_event=getattr(ctx, "cancel_event", None),
                last_attempt=attempt[0] >= attempt[1],
                lease=(execution.id, ctx.worker_id),
                stopping=stopping,
                give_back=getattr(ctx, "give_back", None),
            )
        except asyncio.CancelledError:
            if stopping is not None and stopping.is_set():
                await run_to_completion(_hand_back(execution.id, ctx.worker_id))
            raise
        if outcome.kind == "lease_lost":
            raise LeaseLostError(outcome.error or f"Execution {execution.id} lease lost")
        if outcome.kind == "handover":
            # First in the queue, like the render it is part of, to the incoming release first.
            return AdapterResult.defer(
                outcome.error or HANDOVER_REASON,
                delay_seconds=0.0,
                adapter_state=HANDED_OVER_STATE,
                payload_update=handed_over_by(ctx.worker_id),
                place="head",
                claim_for=await incoming_hand(ctx.worker_id),
            )
        if outcome.kind == "gave_back":
            return _slot_given_back(outcome, ctx.worker_id)
        return _result(outcome)


class VideoPreviewAdapter(VideoRenderAdapter):
    """The preview lane: scene previews seconds long, 720p30 or 1080p30, on slots of their own.

    A preview runs exactly as a render does - its own process, its lease, its
    resumes - beside the whole render, never on its slot: a whole render never
    waits for anyone's previews. A preview's process is held to the preview's
    memory ceiling, and starts on its pages only, in what its worker's runs
    leave of its memory (``render_process.admit_preview``). One its
    worker does not hold on them is handed to a worker whose free memory does
    (``preview_routing``: ``claim_for``, no attempt spent), else waits for its
    pages there, in its place, its stage saying which; its answer says where it
    was drawn and why.
    """

    name = PREVIEW_ADAPTER
    #: The render lane lends it its idle slots; it lends none, so takes none back.
    lends_idle_slots_to = None
    takes_back_lent_slots_for = ()
    #: Seconds long: a preview handed back takes the next free slot of its lane, first, and takes none back.
    keeps_slot_through = ()

    async def deal_claims(self, db, due: list, worker_id: str) -> list:
        # Its slots - and the render lane's lent to it - claim one preview per video in turn.
        from app.services.video_studio import preview_lane

        return await preview_lane.deal_claims(db, due, worker_id)

    @property
    def concurrency_limit(self) -> int | None:
        from app.core.config import settings

        return max(1, settings.video_studio_preview_concurrency)

    @property
    def slots_held_back(self) -> int:
        # The CPUs a whole render's pages reserve on this worker: its preview slots shrink to what the rest holds.
        from app.services.video_studio import render_process

        return render_process.preview_slots_held_back()

    @property
    def slots_added(self) -> int:
        # The slots it adds while previews are queued beyond its own: as many as its CPUs hold, at most
        # VIDEO_STUDIO_PREVIEW_CONCURRENCY_MAX; none when that is 0.
        from app.services.video_studio import render_process

        return render_process.preview_slots_added()

    @property
    def claims_here(self) -> bool:
        # ``VIDEO_STUDIO_PREVIEW_CONCURRENCY=0``: a worker that draws whole renders only, and keeps no reserve.
        from app.services.video_studio import render_process

        return render_process.draws_previews()

    async def periodic(self, ctx: AdapterContext) -> None:
        # What this worker has free for a preview, for the worker that cannot draw one on its pages.
        from app.services.video_studio import preview_lane, preview_routing, render_process

        if self.claims_here:
            if render_process.adds_preview_slots():
                # The lane's queue, re-read each tick: what the slots it adds are sized to.
                try:
                    render_process.note_queued(await preview_lane.queued_count(ctx.db))
                except Exception as exc:  # unread: the slots added stay as the last tick sized them
                    logger.debug("Preview queue not counted: %s", format_exception_message(exc))
            await preview_routing.publish(ctx.worker_id)

    async def execute(self, ctx: AdapterContext, execution: OrchestrationExecution) -> AdapterResult:
        from app.services.video_studio import jobs, preview_lane, preview_routing, render_preflight, render_process

        job_id = str((execution.payload or {}).get("render_id") or "")
        if not job_id:
            return await super().execute(ctx, execution)
        # Sized to the screen inserts of its scene's window, read before the admission holds it.
        size = await render_process.preview_size_of(job_id)
        payload = execution.payload or {}
        waited, since = memory_waited(execution)
        earlier_why = payload.get(MEMORY_WHY_KEY) if waited > 0 else None
        route = preview_routing.route_of(payload, ctx.worker_id)
        note = preview_routing.admission_note(route, worker=ctx.worker_id, waited=waited, why=earlier_why, pages=size.pages)
        why = render_process.admit_preview(job_id, size, note=note)
        if why is not None:
            await ctx.db.commit()
            tried = [*((route or {}).get("tried") or []), ctx.worker_id]
            there = None
            if len(tried) <= preview_routing.MAX_ROUTES:
                there = preview_routing.best(await preview_routing.rooms(me=ctx.worker_id), size.ceiling, tried=tried)
            if there is not None:
                said = preview_routing.routed_text(worker=ctx.worker_id, there=there, why=why)
                await jobs.mark_if_open("render", job_id, stage=said[:200])
                return AdapterResult.defer(
                    said,
                    delay_seconds=0.0,
                    adapter_state=preview_routing.ROUTED_STATE,
                    payload_update={
                        preview_routing.ROUTE_KEY: {"from": ctx.worker_id, "to": there.worker, "why": why, "tried": tried}
                    },
                    place="keep",
                    claim_for=(there.worker, preview_routing.ROUTE_HOLD_SECONDS),
                )
            said = preview_routing.waiting_text(worker=ctx.worker_id, why=why)
            await jobs.mark_if_open("render", job_id, stage=said[:200])
            # Waiting for its pages, in its place: never behind the previews queued after it, and never hidden
            # from another worker whose slot frees meanwhile - only this one waits before claiming it again.
            return AdapterResult.defer(
                said,
                delay_seconds=preview_routing.WAIT_POLL_SECONDS,
                adapter_state=MEMORY_WAITING_STATE,
                payload_update={MEMORY_WAIT_KEY: since, MEMORY_WHY_KEY: earlier_why or why},
                place="keep",
                not_for=ctx.worker_id,
            )
        if waited > 0 and (advice := render_process.reserve_advice(size)) is not None:
            # It waited for its pages beside a render: what the worker needs so its previews start at once.
            try:
                await render_preflight.record_preview_infra(ctx.db, job_id, advice)
            except Exception as exc:  # advice: never what decides the preview
                logger.warning("Video preview %s: its sizing advice was not recorded: %s", job_id, format_exception_message(exc))
        try:
            # It waited behind other videos' previews on a lane of one worker: what a second preview worker gives.
            lane = await preview_lane.lane_advice(ctx.db, execution, await preview_routing.lane_workers())
            if lane is not None:
                await render_preflight.record_preview_infra(ctx.db, job_id, lane)
        except Exception as exc:  # advice: never what decides the preview
            logger.warning("Video preview %s: its lane advice was not recorded: %s", job_id, format_exception_message(exc))
        await preview_routing.publish(ctx.worker_id, force=True)
        try:
            return await super().execute(ctx, execution)
        finally:
            render_process.release_preview(job_id)
            await preview_routing.publish(ctx.worker_id, force=True)


async def _hand_back(execution_id, worker_id: str) -> None:
    """Give a render's execution back to the queue as its worker stops: first, due now, its attempt unspent."""
    from app.core.config import settings
    from app.core.database import async_session
    from app.services.orchestration_engine import LeaseLostError, defer, notify_orchestration_wake
    from app.services.orchestration_reconciler import HANDBACK_OUTGOING_HOLD_SECONDS

    hand = await incoming_hand(worker_id)
    # No worker of the incoming release yet: kept from this release's workers, which the same rollout stops next.
    held_from = (str(settings.app_version), HANDBACK_OUTGOING_HOLD_SECONDS) if hand is None and settings.app_version else None
    try:
        async with async_session() as db:
            await defer(
                db,
                execution_id=execution_id,
                worker_id=worker_id,
                reason=HANDOVER_REASON,
                delay_seconds=0.0,
                adapter_state=HANDED_OVER_STATE,
                payload_update=handed_over_by(worker_id),
                place="head",
                claim_for=hand,
                **({"not_release": held_from} if held_from else {}),
            )
            await db.commit()
    except LeaseLostError:
        return
    except Exception as exc:  # its worker's presence goes: the next worker takes it back at once
        logger.warning("Video render execution %s not handed back: %s", execution_id, format_exception_message(exc))
        return
    with contextlib.suppress(Exception):
        await notify_orchestration_wake()


class VideoClipAdapter(_VideoStudioAdapter):
    name = CLIP_ADAPTER
    kind = "clip"

    @property
    def concurrency_limit(self) -> int | None:
        from app.core.config import settings

        return max(1, settings.video_studio_clip_concurrency)

    async def _run(self, job_id: str, cancel_event: asyncio.Event | None):
        from app.services.video_studio.runners import run_clip

        return await run_clip(job_id, cancel_event=cancel_event)


WAKE_SWEEP_ADAPTER = "video_studio_wake_sweep"


class VideoStudioWakeSweepAdapter(OrchestrationAdapter):
    """The worker's sweep of the Video Studio wakes a stopped worker took along.

    No execution is ever created for it: it exists for its periodic hook, at
    most every ``wake.SWEEP_INTERVAL_SECONDS`` per process, in a task of its
    own so the reconciler's cycle never waits on it.
    """

    name = WAKE_SWEEP_ADAPTER
    _sweep_task: asyncio.Task | None = None
    _last_sweep: float | None = None

    async def execute(self, ctx: AdapterContext, execution: OrchestrationExecution) -> AdapterResult:
        return AdapterResult.failed(f"{self.name} runs no executions", retryable=False)

    async def periodic(self, ctx: AdapterContext) -> None:
        if self._sweep_task is not None and not self._sweep_task.done():
            return
        now = time.monotonic()
        if self._last_sweep is not None and now - self._last_sweep < wake.SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep = now
        self._sweep_task = asyncio.create_task(self._sweep())

    async def _sweep(self) -> None:
        try:
            await wake.sweep_lost_wakes()
        except Exception as exc:
            logger.warning(
                "The Video Studio wake sweep failed: %s", format_exception_message(exc), exc_info=True
            )


class VideoFigureStillsAdapter(OrchestrationAdapter):
    """The editor's figure stills: drawn one job at a time, in a process held to the render's memory ceiling."""

    name = figure_stills.ADAPTER

    @property
    def concurrency_limit(self) -> int | None:
        return 1

    async def execute(self, ctx: AdapterContext, execution: OrchestrationExecution) -> AdapterResult:
        payload = dict(execution.payload or {})
        await ctx.db.commit()
        try:
            ended = await figure_stills.run_isolated(execution.id, cancel_event=getattr(ctx, "cancel_event", None))
        finally:
            await figure_stills.release(payload)
        if ended.kind == "done":
            return AdapterResult.completed()
        # Never retried: the next preview that finds a still missing asks for it again.
        return AdapterResult.failed(figure_stills.describe(ended), retryable=False)

    async def on_dead_letter(self, ctx: AdapterContext, execution: OrchestrationExecution) -> None:
        await figure_stills.release(dict(execution.payload or {}))


class VideoMusicAdapter(_VideoStudioAdapter):
    """An AI track: one composition request to a music model, the AI shots' budget."""

    name = MUSIC_ADAPTER
    kind = "music"

    @property
    def concurrency_limit(self) -> int | None:
        from app.core.config import settings

        return max(1, settings.video_studio_clip_concurrency)

    async def _run(self, job_id: str, cancel_event: asyncio.Event | None):
        from app.services.video_studio.runners import run_music

        return await run_music(job_id, cancel_event=cancel_event)
