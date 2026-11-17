"""The frame-capture probe tried again, a render run again on a lighter layout after its
memory ceiling, and the platform's run again of a dead-lettered render against PostgreSQL.

* the probe gives a Chromium starting cold a second, longer try, probes a worker settled on screenshots again in the
  background once that is stale, and is cancelled - its Chromium closed - when the worker stops;
* a whole render the memory ceiling stopped runs again on half the pages its first run laid out, on the worker's own
  CPUs, and says so;
* ``render_retry.requeue`` on PostgreSQL: the dead letter back to pending, its attempts back and its payload merged
  with the record of the run again, the render's row reopened - never a second time; a render cancelled while its
  execution is requeued keeps its cancel, and its execution is cancelled too.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.services.video_studio import compositor, render_preflight, render_retry, resources

# ── The capture probe ─────────────────────────────────────────────────────


@pytest.fixture
def fresh_capture(monkeypatch):
    monkeypatch.setattr(compositor, "_CAPTURE", [])
    monkeypatch.setattr(compositor, "_CAPTURE_PROBE", [])
    monkeypatch.delenv(compositor.FRAME_CAPTURE_ENV, raising=False)
    monkeypatch.setattr(settings, "video_studio_frame_capture", "begin_frame")


async def test_a_chromium_starting_cold_is_given_a_second_longer_try(fresh_capture, monkeypatch):
    waits: list[float] = []

    async def cold_then_up(wait):
        waits.append(wait)
        if len(waits) == 1:
            raise TimeoutError
        return 3.4

    monkeypatch.setattr(compositor, "_probe_begun_frame", cold_then_up)
    found = await compositor.worker_capture()
    assert waits == list(compositor.CAPTURE_PROBE_TRIES) == [2.0, 5.0]
    assert (found.mode, found.reason) == ("begin_frame", "a begun frame in 3.40 s (none within 2 s before)")


async def test_a_worker_on_screenshots_is_probed_again_in_the_background_once_stale(fresh_capture, monkeypatch):
    async def begins(_wait):
        return 0.2

    monkeypatch.setattr(compositor, "_probe_begun_frame", begins)
    settled = compositor.FrameCapture("begin_frame", "screenshot", "no begun frame within 2 s, then 5 s")
    compositor._CAPTURE[:] = [settled]
    # Fresh: kept, nothing probed.
    assert (await compositor.worker_capture()) is settled and compositor._CAPTURE_PROBE == []
    # Stale: the render asking draws with what is known, the probe runs beside it and settles the next one.
    stale = compositor.FrameCapture(
        "begin_frame", "screenshot", "demoted", demoted=True, settled_at=time.monotonic() - compositor.CAPTURE_REPROBE_SECONDS - 1
    )
    compositor._CAPTURE[:] = [stale]
    assert (await compositor.worker_capture()) is stale
    [probe] = compositor._CAPTURE_PROBE
    await probe
    assert (await compositor.worker_capture()).mode == "begin_frame"


async def test_the_probe_still_running_is_cancelled_when_the_worker_stops(fresh_capture, monkeypatch):
    closed = asyncio.Event()

    async def slow(_wait):
        try:
            await asyncio.sleep(60)
        finally:
            closed.set()

    monkeypatch.setattr(compositor, "_probe_begun_frame", slow)
    compositor.start_capture_probe()
    await asyncio.sleep(0)
    await compositor.stop_capture_probe()
    assert closed.is_set() and compositor._CAPTURE_PROBE == [] and compositor.known_capture() is None


# ── A lighter layout after the memory ceiling ─────────────────────────────


def test_a_render_the_memory_ceiling_stopped_runs_again_on_half_its_pages():
    assert render_retry.lighter_pages({"cause": render_retry.MEMORY, "pages": 8}) == 4
    assert render_retry.lighter_pages({"cause": render_retry.MEMORY, "pages": 3}) == 1
    # Its pages not known (its process stopped before laying them out): one.
    assert render_retry.lighter_pages({"cause": render_retry.MEMORY}) == 1
    # Any other cause, or a first run: laid out as the worker reads.
    assert render_retry.lighter_pages({"cause": render_retry.CHROMIUM, "pages": 8}) is None
    assert render_retry.lighter_pages(None) is None


def test_the_run_again_lays_out_those_pages_on_the_worker_s_own_cpus(monkeypatch):
    monkeypatch.setenv(render_preflight.RETRY_PAGES_ENV, "4")
    monkeypatch.setattr(resources, "cpu_budget", lambda: 8)
    monkeypatch.setattr(settings, "video_studio_render_pages", 0)
    pages = render_preflight.retry_pages()
    drawn = render_preflight.layout((3840, 2160), x264_preset="medium", ceiling=None, pages=pages, cpus=resources.cpu_budget())
    assert (drawn.pages, drawn.wanted, drawn.cpus) == (4, 4, 8)
    assert render_preflight.retry_pages_note() == (
        "Drawn on 4 pages at most: its first run passed its memory ceiling, so it ran again on a lighter layout."
    )
    monkeypatch.delenv(render_preflight.RETRY_PAGES_ENV)
    assert render_preflight.retry_pages() is None and render_preflight.retry_pages_note() is None


# ── render_retry.requeue against PostgreSQL ──────────────────────────────


async def _dead_letter(scratch):
    """A user's video at revision 1, its whole render running, the render's execution dead-lettered."""
    from sqlalchemy import update

    from app.models.orchestration import OrchestrationExecution
    from app.models.user import User
    from app.models.video_studio import JOB_RUNNING, VideoProject, VideoRender
    from app.services.video_studio import jobs

    async with scratch.sessions() as db:
        owner = User(email="maker@example.com", hashed_password="x", full_name="Video Maker", role="user")
        db.add(owner)
        await db.flush()
        project = VideoProject(user_id=owner.id, title="Launch film", spec={"title": "Launch film"}, scene_count=1, revision=1)
        db.add(project)
        await db.flush()
        render = VideoRender(project_id=project.id, user_id=owner.id, revision=1, quality="ultra", status=JOB_RUNNING, progress=61)
        db.add(render)
        await db.flush()
        await jobs.enqueue(db, "render", render)
        await db.execute(
            update(OrchestrationExecution)
            .where(OrchestrationExecution.id == render.execution_id)
            .values(
                state="dead_letter",
                attempt_count=3,
                last_error="memory ceiling",
                payload={"render_id": str(render.id), "kept": "from its enqueue"},
                deadline_at=datetime.now(UTC) + timedelta(hours=6),
            )
        )
        await db.commit()
        execution = await db.get(OrchestrationExecution, render.execution_id)
        return render.id, execution


@pytest.mark.integration
async def test_requeue_runs_a_dead_lettered_render_again_once_with_its_payload_merged(monkeypatch):
    from app.models.orchestration import OrchestrationExecution
    from app.models.video_studio import JOB_PENDING, VideoRender
    from app.services.video_studio import jobs
    from tests.scratch_database import ALL_TABLES, scratch_database

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(jobs, "wake", nothing)
    async with scratch_database("video_requeue", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as scratch:
        render_id, execution = await _dead_letter(scratch)
        assert await render_retry.requeue(execution, render_retry.MEMORY, "it passed its 24 GiB memory ceiling")
        async with scratch.sessions() as db:
            requeued = await db.get(OrchestrationExecution, execution.id)
            row = await db.get(VideoRender, render_id)
        assert (requeued.state, requeued.attempt_count, requeued.lease_owner) == ("pending", 0, None)
        record = requeued.payload[render_retry.INFRA_RETRY_KEY]
        assert requeued.payload["render_id"] == str(render_id) and requeued.payload["kept"] == "from its enqueue"
        assert (record["cause"], record["revision"]) == (render_retry.MEMORY, 1)
        assert requeued.last_error.startswith("Running again once on revision 1: it ran out of memory")
        assert (row.status, row.progress) == (JOB_PENDING, 0) and row.stage.startswith("Running again once on revision 1")
        # Never a second time.
        assert not await render_retry.requeue(requeued, render_retry.MEMORY, "again")


@pytest.mark.integration
async def test_a_render_cancelled_while_its_execution_is_requeued_keeps_its_cancel(monkeypatch):
    from sqlalchemy import update

    from app.models.orchestration import OrchestrationExecution
    from app.models.video_studio import JOB_CANCELLED, VideoRender
    from app.services import orchestration_engine as engine
    from app.services.video_studio import jobs
    from tests.scratch_database import ALL_TABLES, scratch_database

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(jobs, "wake", nothing)
    async with scratch_database("video_requeue_race", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as scratch:
        render_id, execution = await _dead_letter(scratch)
        requeue_execution = engine.requeue_execution

        async def requeued_then_cancelled(db, **kwargs):
            found = await requeue_execution(db, **kwargs)
            # The person cancels the render in the same instant, on another session.
            async with scratch.sessions() as other:
                await other.execute(update(VideoRender).where(VideoRender.id == render_id).values(status=JOB_CANCELLED, error="Cancelled."))
                await other.commit()
            return found

        monkeypatch.setattr(engine, "requeue_execution", requeued_then_cancelled)
        assert await render_retry.requeue(execution, render_retry.CHROMIUM, "Target closed")
        async with scratch.sessions() as db:
            row = await db.get(VideoRender, render_id)
            state = (await db.get(OrchestrationExecution, execution.id)).state
        assert (row.status, row.error, state) == (JOB_CANCELLED, "Cancelled.", "cancelled")
        assert uuid.UUID(str(render_id))
