"""A render its worker left, against PostgreSQL.

* the regression: a worker killed mid-render (no hand-back, its render lease
  three minutes long) is found gone and its render taken back by the
  restarted worker's first cycle, first in the queue, and claimed again
  within 30 s of the kill - saying how the worker went;
* a preview waiting for its video's cap keeps its place: the jobs queued
  after it are claimed while it waits, never once its wait is over;
* a render a stopped worker left that no worker resumed within the rolling
  restart its deployment allows (10 min by default), while nothing held a
  slot of its lane, is failed naming the workers alive, never left
  ``running``; a preview in a lent render slot keeps it waiting;
* the regression of 21 Oct 2026: a render whose worker stopped at 50% waits
  for a worker started 5 min later, which takes it before the render queued
  behind it and resumes it from its kept segments, as the same job;
* a relaunch of the same revision and quality brings back the render no
  worker resumed: the same job on its own execution, first in the queue,
  its segments kept for it;
* a scene preview handed to another worker (``claim_for``) is that
  worker's alone to claim until its hand runs out, anyone's after.

``integration``-marked and skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.core.config import settings
from app.models.orchestration import OrchestrationExecution, OrchestrationOutboxEvent
from app.models.user import User
from app.models.video_studio import JOB_FAILED, JOB_PENDING, JOB_READY, JOB_RUNNING, VideoProject, VideoRender
from app.services import orchestration_engine as engine
from app.services import orchestration_reconciler as reconciler
from app.services import worker_presence
from app.services.orchestration_adapters import video_studio as adapters
from app.services.video_studio import jobs, projects, render_checks, render_preflight, render_process, render_segments
from app.services.video_studio.compositor import SEGMENTS_DIR, segment_name
from app.services.video_studio.render_process import RenderExit
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_RENDERS = "video_render"


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 20, 18, 0, tzinfo=UTC)

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _install(monkeypatch, clock: _Clock) -> None:
    class _ClockedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now if tz is None else clock.now.astimezone(tz)

    monkeypatch.setattr(engine, "datetime", _ClockedDatetime)


def _executions():
    return scratch_database(
        "workerrestart", tables=[OrchestrationExecution.__table__, OrchestrationOutboxEvent.__table__]
    )


async def _queue(sessions, clock: _Clock, *, ago: float = 0.0) -> uuid.UUID:
    async with sessions() as db:
        execution = await engine.create_execution(
            db,
            adapter=_RENDERS,
            payload={"render_id": str(uuid.uuid4())},
            run_at=clock.now - timedelta(seconds=ago),
            deadline_at=clock.now + timedelta(hours=4),
            max_attempts=3,
        )
        await db.commit()
        return execution.id


async def _claim(sessions, worker: str, limit: int = 1) -> list[uuid.UUID]:
    async with sessions() as db:
        rows = await engine.claim_due(db, adapter=_RENDERS, worker_id=worker, limit=limit, lease_seconds=jobs.RENDER_LEASE_SECONDS)
        ids = [row.id for row in rows]
        await db.commit()
    return ids


async def test_a_render_whose_worker_was_killed_resumes_within_30_seconds_of_its_restart(monkeypatch):
    clock = _Clock()
    _install(monkeypatch, clock)
    killed, restarted = "worker-0:1:aaaaaaaa", "worker-0:1:bbbbbbbb"
    async with _executions() as scratch:
        drawing = await _queue(scratch.sessions, clock, ago=120)
        assert await _claim(scratch.sessions, killed) == [drawing]
        # It draws for five minutes, its lease renewed.
        clock.advance(300)
        async with scratch.sessions() as db:
            await engine.extend_lease(db, execution_id=drawing, worker_id=killed, lease_seconds=jobs.RENDER_LEASE_SECONDS)
            await db.commit()
        # Queued behind it while it drew.
        behind = await _queue(scratch.sessions, clock, ago=30)
        # The worker is killed mid-render: no hand-back, a lease of three minutes nobody renews.
        killed_at = clock.now
        last_word = {
            "worker_id": killed,
            "host": "worker-7f9c",
            "boot_id": "another-boot",
            "version": settings.app_version,
            "state": "running",
            "at": killed_at.isoformat(),
            "memory": int(3.9 * (1 << 30)),
            "memory_limit": 4 << 30,
        }
        words: dict[str, dict] = {}

        async def records(worker_ids):
            return {worker_id: words.get(worker_id) for worker_id in worker_ids}

        monkeypatch.setattr(worker_presence, "records", records)
        # Its word still fresh: nothing is taken from it.
        words[killed] = last_word
        async with scratch.sessions() as db:
            assert await reconciler._reclaim_from_gone_workers(db, restarted, [_RENDERS], 10) == []
            await db.commit()
        async with scratch.sessions() as db:
            assert (await db.get(OrchestrationExecution, drawing)).lease_owner == killed
        # Its word runs out; the restarted worker's next cycle takes the render back - first in the queue.
        clock.advance(worker_presence.PRESENCE_TTL_SECONDS + settings.reconciler_interval_seconds)
        words[killed] = {**last_word, "lapsed": True}
        async with scratch.sessions() as db:
            assert await reconciler._reclaim_from_gone_workers(db, restarted, [_RENDERS], 10) == []
            await db.commit()
        # Taken before the render queued behind it, by the restarted worker's first free slot.
        assert await _claim(scratch.sessions, restarted) == [drawing]
        assert clock.now - killed_at < timedelta(seconds=30)
        async with scratch.sessions() as db:
            row = (await db.execute(select(OrchestrationExecution).where(OrchestrationExecution.id == drawing))).scalar_one()
            queued = await db.get(OrchestrationExecution, behind)
        # A fresh lease of its own, the attempt the dead worker spent counted, and why it was taken back.
        assert row.lease_owner == restarted and row.state == "running" and row.attempt_count == 2
        assert row.lease_expires_at == clock.now + timedelta(seconds=jobs.RENDER_LEASE_SECONDS)
        assert row.adapter_state == engine.OWNER_GONE_STATE and queued.state == "pending"
        assert "went without a stop request" in row.last_error and "out-of-memory killer" in row.last_error
        expiry = row.payload[engine.LEASE_EXPIRY_PAYLOAD_KEY]
        assert expiry["owner"] == killed and expiry["owner_record"]["memory_limit"] == 4 << 30


async def test_a_preview_waiting_its_turn_keeps_its_place(monkeypatch):
    clock = _Clock()
    _install(monkeypatch, clock)
    async with _executions() as scratch:
        first = await _queue(scratch.sessions, clock, ago=300)
        second = await _queue(scratch.sessions, clock, ago=240)
        third = await _queue(scratch.sessions, clock, ago=180)
        assert await _claim(scratch.sessions, "w1") == [first]
        async with scratch.sessions() as db:
            await engine.defer(
                db,
                execution_id=first,
                worker_id="w1",
                reason="Waiting for a preview of this video to end",
                delay_seconds=jobs.PREVIEW_WAIT_SECONDS,
                adapter_state=jobs.PREVIEW_WAITING_STATE,
                place="keep",
            )
            await db.commit()
        # While it waits, the next is taken; once its wait is over it is taken before the ones queued after it.
        assert await _claim(scratch.sessions, "w2") == [second]
        clock.advance(jobs.PREVIEW_WAIT_SECONDS + 1)
        assert await _claim(scratch.sessions, "w3") == [first]
        async with scratch.sessions() as db:
            row = await db.get(OrchestrationExecution, first)
            later = await db.get(OrchestrationExecution, third)
        assert engine.NOT_BEFORE_PAYLOAD_KEY not in row.payload and later.state == "pending"


async def test_a_preview_handed_to_a_worker_is_that_workers_alone_until_its_hand_runs_out(monkeypatch):
    """A preview its worker cannot draw on its pages is handed to the worker with room (``claim_for``)."""
    clock = _Clock()
    _install(monkeypatch, clock)
    async with _executions() as scratch:
        routed = await _queue(scratch.sessions, clock, ago=60)
        behind = await _queue(scratch.sessions, clock, ago=30)
        assert await _claim(scratch.sessions, "w1") == [routed]
        async with scratch.sessions() as db:
            await engine.defer(
                db,
                execution_id=routed,
                worker_id="w1",
                reason="Routed to worker w2",
                delay_seconds=0.0,
                adapter_state="preview_routed",
                place="keep",
                claim_for=("w2", 10.0),
            )
            await db.commit()
        # Any other worker takes what is queued behind it, never it; the worker it was handed to takes it, first.
        assert await _claim(scratch.sessions, "w3") == [behind]
        assert await _claim(scratch.sessions, "w2") == [routed]
        async with scratch.sessions() as db:
            row = await db.get(OrchestrationExecution, routed)
        assert engine.CLAIM_FOR_PAYLOAD_KEY not in row.payload and row.lease_owner == "w2"
        # Handed to a worker that never claims it: anyone takes it once the hand ran out.
        async with scratch.sessions() as db:
            await engine.defer(
                db,
                execution_id=routed,
                worker_id="w2",
                reason="Routed to worker w4",
                delay_seconds=0.0,
                adapter_state="preview_routed",
                place="keep",
                claim_for=("w4", 10.0),
            )
            await db.commit()
        assert await _claim(scratch.sessions, "w3") == []
        clock.advance(11)
        assert await _claim(scratch.sessions, "w3") == [routed]


async def test_a_render_no_worker_resumed_within_its_wait_is_failed_naming_the_workers(monkeypatch) -> None:
    now = datetime.now(UTC)
    monkeypatch.setattr(settings, "worker_termination_grace_seconds", 0)
    monkeypatch.setattr(settings, "worker_startup_seconds", 0)
    monkeypatch.setattr(worker_presence, "present", AsyncMock(return_value=[]))
    async with scratch_database(
        "unresumed", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner = User(email="samuel@filigran.io", hashed_password="x", full_name="Samuel", role="user")
            db.add(owner)
            await db.flush()
            video = VideoProject(user_id=owner.id, title="Film", spec={"title": "Film", "scenes": []})
            db.add(video)
            await db.flush()

            def handed(minutes: float) -> VideoRender:
                execution = OrchestrationExecution(
                    id=uuid.uuid4(),
                    adapter=_RENDERS,
                    state="pending",
                    adapter_state=jobs.HANDED_OVER_STATE,
                    payload={},
                    run_at=now - timedelta(minutes=minutes),
                    attempt_count=0,
                    max_attempts=3,
                    last_error="Handed to the next worker at 38%: its worker was asked to stop (SIGTERM).",
                    updated_at=now - timedelta(minutes=minutes),
                )
                db.add(execution)
                render = VideoRender(
                    project_id=video.id,
                    user_id=owner.id,
                    revision=1,
                    quality="high",
                    status=JOB_RUNNING,
                    progress=38,
                    progress_at=now - timedelta(minutes=minutes),
                    execution_id=execution.id,
                )
                db.add(render)
                return render

            # Gone 11 min: past the 10 min a rolling restart may take. Gone 4 min: past the old 3 min, still waiting.
            stale, fresh = handed(11), handed(4)
            await db.commit()
        assert await jobs.fail_unresumed_renders(now=now) == 1
        async with scratch.sessions() as db:
            failed = await db.get(VideoRender, stale.id)
            waiting = await db.get(VideoRender, fresh.id)
            execution = await db.get(OrchestrationExecution, failed.execution_id)
        assert failed.status == JOB_FAILED and waiting.status == JOB_RUNNING and execution.state == "cancelled"
        assert failed.error.startswith("No worker resumed it in 10 min after it went back to the queue at 38%")
        assert "No worker was running then." in failed.error and "as this same render" in failed.error
        # Marked, so a relaunch of the same revision and quality brings it back as the same job.
        assert execution.payload[jobs.UNRESUMED_KEY]["progress"] == 38 and execution.payload[jobs.UNRESUMED_KEY]["workers"] == []
        # A scene preview being made in a render slot lent to it (recorded on its execution): the
        # one waiting first in the queue is the next a render slot takes, and the check records its lane held.
        async with scratch.sessions() as db:
            db.add(
                OrchestrationExecution(
                    id=uuid.uuid4(),
                    adapter=jobs.PREVIEW_ADAPTER,
                    state="running",
                    payload={engine.LENT_SLOT_PAYLOAD_KEY: jobs.RENDER_ADAPTER},
                    run_at=now,
                    lease_owner="busy:1",
                    lease_expires_at=now + timedelta(minutes=3),
                )
            )
            await db.commit()
        assert await jobs.fail_unresumed_renders(now=now + timedelta(minutes=8)) == 0
        async with scratch.sessions() as db:
            held = await db.get(OrchestrationExecution, waiting.execution_id)
        assert held.state == "pending" and held.payload[jobs.LANE_HELD_KEY] == (now + timedelta(minutes=8)).isoformat()


# -- A render handed back waits for the rolling restart, resumes first, relaunches as the same job --


class _Store:
    """The object store the segments are kept in beside their render."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def upload_path(self, path, name, _content_type=None):
        self.objects[name] = Path(path).read_bytes()
        return name

    async def download_to_path(self, name, path, *, missing_ok=False):
        Path(path).write_bytes(self.objects[name])

    async def list_object_names(self, prefix, *, recursive=True):
        names = [name for name in self.objects if name.startswith(prefix)]
        if recursive:
            return names
        return sorted({prefix + name[len(prefix) :].split("/", 1)[0] + "/" for name in names})

    async def delete(self, name):
        self.objects.pop(name, None)


@pytest.fixture
def restarting(monkeypatch):
    """No wake, a deployment that says nothing of its restart (10 min), no worker alive, an object store."""
    from app.services import file_storage as storage_module

    store = _Store()
    monkeypatch.setattr(storage_module, "file_storage", store)
    monkeypatch.setattr(engine, "notify_orchestration_wake", AsyncMock())
    monkeypatch.setattr(worker_presence, "present", AsyncMock(return_value=[]))
    monkeypatch.setattr(settings, "worker_termination_grace_seconds", 0)
    monkeypatch.setattr(settings, "worker_startup_seconds", 0)
    return store


async def _drawn_by_a_stopped_worker(scratch, tmp_path: Path, *, progress: int) -> SimpleNamespace:
    """A whole render worker A drew to *progress*, two segments kept beside it, handed back on SIGTERM; then a render
    queued behind it a minute before."""
    async with scratch.sessions() as db:
        owner = User(email="samuel@filigran.io", hashed_password="x", full_name="Samuel", role="user")
        db.add(owner)
        await db.flush()
        video = VideoProject(user_id=owner.id, title="Film", spec={"title": "Film", "scenes": []})
        db.add(video)
        await db.flush()
        render = VideoRender(project_id=video.id, user_id=owner.id, revision=video.revision, quality="ultra", stage="Queued")
        db.add(render)
        await db.flush()
        await jobs.enqueue(db, "render", render)
        behind = await engine.create_execution(
            db,
            adapter=_RENDERS,
            payload={"render_id": str(uuid.uuid4())},
            run_at=datetime.now(UTC) - timedelta(minutes=1),
            deadline_at=datetime.now(UTC) + timedelta(hours=4),
            max_attempts=3,
        )
        await db.commit()
        world = SimpleNamespace(owner=owner.id, video=video.id, render=render.id, execution=render.execution_id, behind=behind.id)
    # Worker A takes it - the render queued behind it was not there yet when it did - and draws its segments.
    async with scratch.sessions() as db:
        await db.execute(
            update(OrchestrationExecution)
            .where(OrchestrationExecution.id == world.execution)
            .values(run_at=datetime.now(UTC) - timedelta(minutes=30))
        )
        await db.commit()
    assert await _claim(scratch.sessions, "worker-a") == [world.execution]
    segments = tmp_path / "worker-a" / SEGMENTS_DIR
    segments.mkdir(parents=True)
    for first, end in ((0, 600), (600, 1200)):
        piece = segments / segment_name(first, end)
        piece.write_bytes(f"{first}-{end}".encode())
        await render_segments.keep(str(world.render), "digest", piece)
    await jobs.mark("render", world.render, status=JOB_RUNNING, progress=progress)
    # SIGTERM: the render process is handed over, and its execution deferred as the adapter returns it.
    handed = await render_process.hand_over(str(world.render), RenderExit(kind="handed_over", progress=progress), tmp_path / "worker-a")
    assert handed.kind == "handover"
    async with scratch.sessions() as db:
        await engine.defer(
            db,
            execution_id=world.execution,
            worker_id="worker-a",
            reason=handed.error,
            delay_seconds=0.0,
            adapter_state=jobs.HANDED_OVER_STATE,
            payload_update=adapters.handed_over_by("worker-a"),
            place="head",
        )
        await db.commit()
    return world


async def _time_passes(scratch, world: SimpleNamespace, minutes: float) -> None:
    """*minutes* go by with no worker taking the render back: its hand-back and its last progress grow that old."""
    async with scratch.sessions() as db:
        await db.execute(
            update(OrchestrationExecution)
            .where(OrchestrationExecution.id == world.execution)
            .values(updated_at=OrchestrationExecution.updated_at - timedelta(minutes=minutes))
        )
        await db.execute(
            update(VideoRender)
            .where(VideoRender.id == world.render)
            .values(progress_at=VideoRender.progress_at - timedelta(minutes=minutes))
        )
        await db.commit()


async def test_a_render_whose_worker_stopped_at_half_resumes_on_a_worker_started_5_min_later_as_the_same_job(
    restarting, tmp_path
) -> None:
    async with scratch_database(
        "resumefive", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        world = await _drawn_by_a_stopped_worker(scratch, tmp_path, progress=50)
        # No worker for 5 min: worker A is gone, worker B not started yet.
        await _time_passes(scratch, world, 5)
        # Past the 3 min that failed it on 21 Oct 2026: it still waits for the rolling restart.
        assert await jobs.fail_unresumed_renders() == 0
        # Worker B starts: its first free render slot takes it, before the render queued behind it.
        assert await _claim(scratch.sessions, "worker-b") == [world.execution]
        # It resumes from the segments worker A kept, onto a disk of its own.
        restored = tmp_path / "worker-b" / SEGMENTS_DIR
        assert await render_segments.restore(str(world.render), "digest", restored) == 2
        assert (restored / segment_name(0, 600)).read_bytes() == b"0-600"
        # And finishes as the same job.
        await jobs.mark("render", world.render, status=JOB_READY, progress=100, finished_at=datetime.now(UTC))
        async with scratch.sessions() as db:
            await engine.complete(db, execution_id=world.execution, worker_id="worker-b")
            await db.commit()
        async with scratch.sessions() as db:
            renders = (await db.execute(select(VideoRender).where(VideoRender.project_id == world.video))).scalars().all()
            execution = await db.get(OrchestrationExecution, world.execution)
            behind = await db.get(OrchestrationExecution, world.behind)
        assert [row.id for row in renders] == [world.render] and renders[0].execution_id == world.execution
        assert renders[0].status == JOB_READY and behind.state == "pending"
        # The attempt worker A was stopped in was never counted; worker A's hand-back stamped its wait.
        assert execution.state == "completed" and execution.attempt_count == 1
        assert execution.payload[jobs.HANDED_OVER_KEY]["worker"] == "worker-a"
        assert execution.payload[jobs.HANDED_OVER_KEY]["resume_wait_seconds"] == 600


async def test_a_relaunch_brings_back_the_render_no_worker_resumed_as_the_same_job_first_in_the_queue(
    restarting, tmp_path, monkeypatch
) -> None:
    draining = {"worker_id": "worker-a", "host": "worker-old", "version": "1.4.2", "state": "draining", "signal": "SIGTERM"}
    monkeypatch.setattr(worker_presence, "present", AsyncMock(return_value=[draining]))
    async with scratch_database(
        "relaunch", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        world = await _drawn_by_a_stopped_worker(scratch, tmp_path, progress=73)
        await _time_passes(scratch, world, 11)
        assert await jobs.fail_unresumed_renders() == 1
        async with scratch.sessions() as db:
            failed = await db.get(VideoRender, world.render)
        assert failed.status == JOB_FAILED and "went back to the queue at 73%" in failed.error
        assert "Workers alive then: worker-old (version 1.4.2) was asked to stop (SIGTERM) and took no new job" in failed.error
        # Its segments stay for its relaunch.
        assert await render_segments.sweep() == 0 and len(restarting.objects) == 2
        # The relaunch of the same revision at the same quality.
        monkeypatch.setattr(render_checks, "gate", AsyncMock(return_value=[]))
        monkeypatch.setattr(render_preflight, "preflight", AsyncMock(return_value=None))
        monkeypatch.setattr(projects, "effective_spec", AsyncMock(return_value=SimpleNamespace(spec={"scenes": []})))
        monkeypatch.setattr("app.services.audit.log_audit", AsyncMock())
        async with scratch.sessions() as db:
            user = await db.get(User, world.owner)
            video = await db.get(VideoProject, world.video)
            started = await projects.start_render(db, video, user, quality="ultra")
            await db.commit()
        assert started.relaunched and not started.reused and started.render.id == world.render
        async with scratch.sessions() as db:
            renders = (await db.execute(select(VideoRender).where(VideoRender.project_id == world.video))).scalars().all()
            execution = await db.get(OrchestrationExecution, world.execution)
            behind = await db.get(OrchestrationExecution, world.behind)
        assert [row.id for row in renders] == [world.render] and renders[0].status == JOB_PENDING
        assert renders[0].execution_id == world.execution and renders[0].error is None
        # Its own execution, engaged afresh first in the queue, the mark of its failure gone.
        assert execution.state == "pending" and execution.adapter_state is None and execution.attempt_count == 0
        assert jobs.UNRESUMED_KEY not in execution.payload and execution.run_at < behind.run_at
        # The next free render slot takes it, and it resumes from its kept segments.
        assert await _claim(scratch.sessions, "worker-b") == [world.execution]
        assert await render_segments.restore(str(world.render), "digest", tmp_path / "worker-b" / SEGMENTS_DIR) == 2
