"""The recording lane on PostgreSQL, through the engine's claims.

A fixture worker captures a 60 fps take of 3200x1800 at 21 fps while two scene previews draw on it, and at 61 fps once
they ended. What is proven through the engine's own claim and deferral (``claim_due``, ``VideoTakeAdapter.execute``, the
reconciler's ``_apply_result``) and the runner (``run_take``, its browser recording and encode faked):

* the take short of its fps beside the previews is queued, not refused: its row says what it waits for and its place,
  its execution waits in its place in the lane; claimed while the previews still draw, it waits again without a
  warm-up; once they ended it measures again, records by itself, and the first line of its action log says the wait;
* the same fixture worker idle, measuring 20 fps, still refuses the take, naming the window sizes;
* a queued take the author cancels records nothing - nor one cancelled while it measures.

Every actor an ordinary user (rule 19). ``integration``-marked and skips when PostgreSQL is unreachable; the temporary
database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.orchestration import OrchestrationExecution
from app.models.user import User
from app.models.video_studio import JOB_CANCELLED, JOB_FAILED, JOB_PENDING, JOB_READY, VideoTake
from app.services import orchestration_engine as engine
from app.services.orchestration_adapters.video_studio import VideoTakeAdapter
from app.services.orchestration_reconciler import _apply_result
from app.services.video_studio import assets, capture, capture_rate, encoder, jobs, recording_lane, take_state, wake
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = [pytest.mark.integration]

START = "https://app.example.test/reports/7"
OWN_PIXELS = 3200 * 1800
SCRIPT = {
    "steps": [{"action": "wait", "seconds": 1}],
    "start_url": START,
    "viewport": {"width": 1600, "height": 900},
    "pixel_ratio": 2,
    "fps": 60,
}


class FixtureWorker:
    """Worker ``w1``: what it captures of a frame - *busy* fps at the take's own size while scene previews draw on it,
    *idle* fps once none does, a frame's cost going with its pixels -, and what its browser recorded and stored."""

    def __init__(self, *, busy: float, idle: float) -> None:
        self.busy, self.idle = busy, idle
        self.previews: list[uuid.UUID] = []
        self.warm_ups: list[tuple] = []
        self.recorded: list = []
        self.stored: list[str] = []
        self.during_warm_up = None
        #: The browser crashing once past its warm-up: the take's attempt fails for a cause of its own.
        self.crash_after_warm_up = False

    @asynccontextmanager
    async def warm_ups_of(self, _script):
        async def measure(window: tuple[int, int], ratio: float) -> float:
            self.warm_ups.append((window, ratio, bool(self.previews)))
            if self.during_warm_up is not None:
                await self.during_warm_up()
            pixels = round(window[0] * ratio) * round(window[1] * ratio)
            return min(120.0, (self.busy if self.previews else self.idle) * OWN_PIXELS / pixels)

        yield measure

    async def record_take(self, script, *, workdir, progress=None, lane=None, **_recording):
        """The recorder: its real warm-up (``capture.preflight``), then a second of frames as its browser painted them."""
        planned = await capture.preflight(script, progress=progress, lane=lane)
        if self.crash_after_warm_up:
            self.crash_after_warm_up = False
            raise RuntimeError("Target page, context or browser has been closed")
        self.recorded.append(planned)
        frames = []
        for index in range(31):
            name = f"frame-{index:05d}.jpg"
            Image.new("RGB", (64, 36), (10 + index * 7, 40, 90)).save(Path(workdir) / name, quality=80)
            frames.append((index / 30, name))
        event = {"step": 0, "action": "wait", "label": None, "t0": 0.05, "ta": 0.05, "t1": 1.0, "ok": True}
        return capture.CaptureResult(
            workdir=workdir,
            frames=frames,
            duration=1.05,
            events=[event],
            cursor=[[0.0, 800.0, 450.0]],
            viewport=planned.viewport,
            pixel_ratio=planned.ratio,
            frame_size=(64, 36),
            storage_state=None,
            signed_in=None,
            notes=list(planned.notes),
            capture={**planned.captured, "opened": {"asked": START, "carried": False, "url": START}},
            script=planned.script,
        )


@pytest.fixture
def worker(monkeypatch) -> FixtureWorker:
    fixture = FixtureWorker(busy=21.0, idle=61.0)
    monkeypatch.setattr(capture_rate, "_TABLE", {})
    monkeypatch.setattr(capture_rate, "warm_ups", fixture.warm_ups_of)
    monkeypatch.setattr(capture, "record_take", fixture.record_take)
    monkeypatch.setattr(recording_lane, "POLL_SECONDS", 0.0)
    monkeypatch.setattr(wake, "schedule", lambda *_args: None)

    async def mezzanine(_frames, *, out_path, **_options):
        Path(out_path).write_bytes(b"mp4")

    async def probe(_path):
        return SimpleNamespace(duration=1.05, width=3200, height=1800, has_audio=False)

    async def extract_frame(_path, *, out_path, **_options):
        Image.new("RGB", (64, 36)).save(out_path)

    async def store_path(path, *, filename, **_options):
        fixture.stored.append(filename)
        return SimpleNamespace(id=uuid.uuid4(), size=Path(path).stat().st_size)

    monkeypatch.setattr(encoder, "frames_to_mezzanine", mezzanine)
    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(encoder, "extract_frame", extract_frame)
    monkeypatch.setattr(assets, "store_path", store_path)
    return fixture


async def _take(db, *, fps: int = 60) -> VideoTake:
    owner = User(email=f"{uuid.uuid4().hex[:8]}@filigran.io", hashed_password="x", full_name="Samuel Hassine", role="user")
    db.add(owner)
    await db.flush()
    take = jobs.new_take(user_id=owner.id, profile_id=None, conversation_id=None, name="Graph tour", script={**SCRIPT, "fps": fps})
    db.add(take)
    await db.flush()
    await jobs.enqueue(db, "take", take)
    return take


async def _previews_on(db, fixture: FixtureWorker, count: int) -> None:
    """*count* scene previews drawing on worker w1: their executions running under its lease."""
    for _ in range(count):
        execution = await engine.create_execution(db, adapter=jobs.PREVIEW_ADAPTER, payload={"render_id": str(uuid.uuid4())})
        execution.state, execution.lease_owner, execution.attempt_count = "running", "w1", 1
        fixture.previews.append(execution.id)


async def _previews_end(db, fixture: FixtureWorker) -> None:
    for execution_id in fixture.previews:
        (await db.get(OrchestrationExecution, execution_id)).state = "completed"
    fixture.previews.clear()


async def _claim_and_run(scratch) -> OrchestrationExecution | None:
    """A free slot of w1's recording lane claims the next take and runs it, its result applied as the reconciler does."""
    async with scratch.sessions() as db:
        claimed = await engine.claim_due(db, adapter=jobs.TAKE_ADAPTER, worker_id="w1", limit=1)
        if not claimed:
            await db.rollback()
            return None
        [execution] = claimed
        execution_id = execution.id
        result = await VideoTakeAdapter().execute(SimpleNamespace(db=db, worker_id="w1", cancel_event=None), execution)
        await _apply_result(db, execution_id, "w1", result)
    async with scratch.sessions() as db:
        return await db.get(OrchestrationExecution, execution_id)


async def test_a_60_fps_take_beside_scene_previews_waits_in_the_lane_and_records_once_they_end(worker) -> None:
    async with scratch_database(
        "video_recording_lane", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            take = await _take(db)
            await _previews_on(db, worker, 2)
            await db.commit()

        # Measured in its slot beside the two previews: queued, never refused, nothing recorded.
        execution = await _claim_and_run(scratch)
        assert execution.state == "pending" and execution.adapter_state == recording_lane.WAITING_STATE
        assert execution.attempt_count == 0, "waiting in the lane spends no attempt"
        assert worker.warm_ups == [((1600, 900), 2.0, True)] and worker.recorded == [] and worker.stored == []
        async with scratch.sessions() as db:
            row = await db.get(VideoTake, take.id)
            assert row.status == JOB_PENDING and row.progress_at is None and row.started_at is None and row.error is None
            assert row.stage == (
                "Waiting in the recording lane: busy: 2 scene previews on its worker (21 fps measured beside 2 scene "
                "previews, under its 60 fps)"
            )
            [state] = await jobs.job_states("take", [row])
        # Its answer says its place and the jobs it waits behind, as a scene preview's does.
        assert state.status == jobs.STATE_QUEUED and state.queue.lane == "recording" and state.queue.position == 1
        assert jobs.describe(state).startswith(
            "waiting in the recording lane, place 1 (nothing queued ahead of it): busy: 2 scene previews on its worker; "
            "it measured 21 fps at 3200x1800 beside them, under its 60 fps, and starts by itself once its worker's "
            "load allows 60 fps (waiting "
        )

        # Claimed again while the previews draw: it waits again, no warm-up run.
        execution = await _claim_and_run(scratch)
        assert execution.state == "pending" and execution.adapter_state == recording_lane.WAITING_STATE
        assert len(worker.warm_ups) == 1 and worker.recorded == []

        # The previews end: the next claim measures again, idle, and the take records by itself.
        async with scratch.sessions() as db:
            await _previews_end(db, worker)
            await db.commit()
        execution = await _claim_and_run(scratch)
        assert execution.state == "completed"
        assert worker.warm_ups[1:] == [((1600, 900), 2.0, False)] and len(worker.recorded) == 1
        async with scratch.sessions() as db:
            row = await db.get(VideoTake, take.id)
        assert row.status == JOB_READY and row.file_id is not None and row.pixel_ratio == 2.0
        waited = row.capture["waited"]
        assert waited.startswith("waited ") and waited.endswith(
            " in the recording lane: 21 fps at 3200x1800 beside 2 scene previews on its worker, under its 60 fps; "
            "61 fps measured once its load allowed"
        )
        # The first line of its action log says the wait, and so do its notes.
        first_line = take_state.opened_words(row.capture["opened"], row.capture.get("fallback"), row.capture.get("waited"))
        assert first_line.startswith(f"opened on {START} - a new browser") and first_line.endswith(f"; {waited}")
        assert f"{waited[:1].upper()}{waited[1:]}." in (row.error or "")
        assert row.capture["preflight"]["beside"] == {"jobs": [], "said": ""}


async def test_a_take_that_measured_again_and_failed_for_another_cause_waits_for_nothing_on_its_retry(worker) -> None:
    async with scratch_database(
        "video_recording_retry", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            take = await _take(db)
            await _previews_on(db, worker, 2)
            await db.commit()
        await _claim_and_run(scratch)
        # The previews end, its warm-up passes, then its browser crashes: the engine runs it again later.
        async with scratch.sessions() as db:
            await _previews_end(db, worker)
            await db.commit()
        worker.crash_after_warm_up = True
        failed = await _claim_and_run(scratch)
        assert failed.state == "pending" and failed.adapter_state != recording_lane.WAITING_STATE
        assert recording_lane.wait_of(failed) is None
        # Previews draw again by its retry: it measures them, never held behind the wait it had.
        async with scratch.sessions() as db:
            await _previews_on(db, worker, 2)
            execution = await db.get(OrchestrationExecution, take.execution_id)
            execution.run_at = execution.created_at
            await db.commit()
        await _claim_and_run(scratch)
    assert [busy for _window, _ratio, busy in worker.warm_ups] == [True, False, True]


async def test_the_same_worker_idle_measuring_20_fps_still_refuses_naming_the_window_sizes(worker) -> None:
    worker.busy = worker.idle = 20.0
    async with scratch_database(
        "video_recording_idle", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            take = await _take(db)
            await db.commit()
        execution = await _claim_and_run(scratch)
        assert execution.state == "completed" and execution.adapter_state != recording_lane.WAITING_STATE
        async with scratch.sessions() as db:
            row = await db.get(VideoTake, take.id)
    assert row.status == JOB_FAILED and worker.recorded == [] and worker.stored == []
    assert row.error.startswith(
        "Refused before recording: this worker captures 3200x1800 frames at 20 fps (36 fps at pixel ratio 1.5), under "
        "the take's 60 fps, so its animations would step; the largest size measured sustaining 60 fps on this worker ("
    )
    assert " window at pixel ratio 1.5 (" in row.error and " painted, " in row.error
    assert " No other Video Studio job ran on this worker while it measured." in row.error
    # What the warm-ups measured: the screencast quality and the smaller sizes searched.
    assert " Its warm-ups captured JPEG screencast frames at quality 95; smaller sizes measured: " in row.error


async def test_a_queued_take_the_author_cancels_records_nothing(worker) -> None:
    async with scratch_database(
        "video_recording_cancel", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            take = await _take(db)
            await _previews_on(db, worker, 2)
            await db.commit()
        waiting = await _claim_and_run(scratch)
        assert waiting.adapter_state == recording_lane.WAITING_STATE

        # The author cancels it while it waits (cancel_video_job), then the previews end.
        async with scratch.sessions() as db:
            row = await db.get(VideoTake, take.id)
            assert await jobs.cancel(db, "take", row)
            await _previews_end(db, worker)
            await db.commit()
        assert await _claim_and_run(scratch) is None, "a cancelled take is never claimed again"
        async with scratch.sessions() as db:
            row = await db.get(VideoTake, take.id)
            execution = await db.get(OrchestrationExecution, waiting.id)
        assert execution.state == "cancelled"
        assert row.status == JOB_CANCELLED and row.file_id is None and row.poster_file_id is None
        assert row.captured_frames is None and row.events is None
        assert worker.recorded == [] and worker.stored == [] and len(worker.warm_ups) == 1


async def test_a_take_cancelled_while_it_measures_stays_cancelled_and_records_nothing(worker) -> None:
    async with scratch_database(
        "video_recording_cancel_measuring", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            take = await _take(db)
            await _previews_on(db, worker, 1)
            await db.commit()

        async def cancelled_meanwhile():
            async with scratch.sessions() as db:
                row = await db.get(VideoTake, take.id)
                await jobs.cancel(db, "take", row)
                await db.commit()

        worker.during_warm_up = cancelled_meanwhile
        await _claim_and_run(scratch)
        async with scratch.sessions() as db:
            row = await db.get(VideoTake, take.id)
            executions = (await db.execute(select(OrchestrationExecution).where(OrchestrationExecution.id == take.execution_id))).scalars().all()
    assert row.status == JOB_CANCELLED and row.error == "Cancelled." and row.file_id is None
    assert [execution.state for execution in executions] == ["cancelled"]
    assert worker.recorded == [] and worker.stored == []
