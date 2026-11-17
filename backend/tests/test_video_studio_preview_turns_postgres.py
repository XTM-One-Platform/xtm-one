"""The preview lane takes one preview per video in turn when its slots claim, against PostgreSQL.

Six previews of one video sat at places 3 to 15 behind another video's batch for 30 minutes, the queue line
saying "0 ahead" while two previews of other videos ran: the lane was dealt when a preview was queued, and a
preview that then waited was skipped by a claim that took whatever was due next. What is proven, through the
engine's own claim (``claim_due(deal=...)``, ``VideoPreviewAdapter.deal_claims``) on PostgreSQL:

* two videos queuing six previews each - one after the other - are claimed alternating, one slot at a time,
  whichever was asked first: the action log reads A, B, A, B...;
* a preview waiting for one worker's memory (``defer(place="keep", not_for=...)``) is held from that worker
  only: the next free slot of another worker takes it at once, in its video's turn;
* a queued preview's line names the video ahead and its remaining previews: "1 preview of video X ahead".

Every actor an ordinary user (rule 19). ``integration``-marked and skips when PostgreSQL is unreachable; the
temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.core.config import settings
from app.models.orchestration import OrchestrationExecution
from app.models.user import User
from app.models.video_studio import JOB_PENDING, VideoProject, VideoRender
from app.services import orchestration_engine as engine
from app.services.orchestration_adapters.video_studio import VideoPreviewAdapter
from app.services.video_studio import jobs
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = [pytest.mark.integration]


async def _owner(db) -> User:
    owner = User(email="samuel@filigran.io", hashed_password="x", full_name="Samuel Hassine", role="user")
    db.add(owner)
    await db.flush()
    return owner


async def _video(db, owner: User, title: str) -> VideoProject:
    project = VideoProject(user_id=owner.id, title=title, spec={"title": title, "freestyle": True, "scenes": []}, scene_count=6)
    db.add(project)
    await db.flush()
    return project


async def _previews(db, project: VideoProject, owner: User, count: int) -> list[VideoRender]:
    """*count* scene previews of *project* queued as a sweep queues them: one per scene, one after the other."""
    rows = []
    for index in range(count):
        row = VideoRender(
            project_id=project.id, user_id=owner.id, revision=1, quality="draft", status=JOB_PENDING, scene_id=f"s{index}"
        )
        db.add(row)
        await db.flush()
        await jobs.enqueue(db, "render", row)
        rows.append(row)
    return rows


async def test_two_videos_queuing_six_previews_each_alternate_in_the_action_log(monkeypatch) -> None:
    monkeypatch.setattr(settings, "video_studio_max_renders_per_user", 10)
    async with scratch_database(
        "video_preview_turns", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner = await _owner(db)
            first, second = await _video(db, owner, "Brand film"), await _video(db, owner, "Event loop")
            await _previews(db, first, owner, 6)
            await _previews(db, second, owner, 6)
            await db.commit()
        titles = {first.id: "A", second.id: "B"}
        log: list[str] = []
        started = datetime.now(UTC)
        for turn in range(12):
            async with scratch.sessions() as db:
                [claimed] = await engine.claim_due(
                    db, adapter=jobs.PREVIEW_ADAPTER, worker_id="w1", limit=1, deal=VideoPreviewAdapter().deal_claims
                )
                render = (await db.execute(select(VideoRender).where(VideoRender.execution_id == claimed.id))).scalar_one()
                log.append(titles[render.project_id])
                # Drawn, then done: the slot frees for the next claim.
                render.started_at = started + timedelta(seconds=turn)
                (await db.get(OrchestrationExecution, claimed.id)).state = "completed"
                await db.commit()
        assert log == ["A", "B"] * 6


async def test_a_preview_waiting_for_one_workers_memory_is_taken_in_its_turn_by_another_worker(monkeypatch) -> None:
    monkeypatch.setattr(settings, "video_studio_max_renders_per_user", 10)
    deal = VideoPreviewAdapter().deal_claims
    async with scratch_database(
        "video_preview_not_for", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner = await _owner(db)
            first, second = await _video(db, owner, "Brand film"), await _video(db, owner, "Event loop")
            await _previews(db, first, owner, 3)
            theirs = await _previews(db, second, owner, 3)
            await db.commit()
        async with scratch.sessions() as db:
            await engine.claim_due(db, adapter=jobs.PREVIEW_ADAPTER, worker_id="w1", limit=1, deal=deal)
            [waiting] = await engine.claim_due(db, adapter=jobs.PREVIEW_ADAPTER, worker_id="w1", limit=1, deal=deal)
            assert waiting.id == theirs[0].execution_id
            # Worker w1 cannot hold its pages: it waits for them there, in its place, held from w1 only.
            await engine.defer(
                db,
                execution_id=waiting.id,
                worker_id="w1",
                reason="Waiting for its pages on worker w1",
                delay_seconds=5.0,
                adapter_state="preview_memory_waiting",
                place="keep",
                not_for="w1",
            )
            await db.commit()
        async with scratch.sessions() as db:
            # w1 claims something else meanwhile, never the preview it waits for.
            [other] = await engine.claim_due(db, adapter=jobs.PREVIEW_ADAPTER, worker_id="w1", limit=1, deal=deal)
            assert other.id != waiting.id
            await db.rollback()
        async with scratch.sessions() as db:
            # The next free slot of another worker takes it at once: its video's turn, not the other video's next.
            [taken] = await engine.claim_due(db, adapter=jobs.PREVIEW_ADAPTER, worker_id="w2", limit=1, deal=deal)
            assert taken.id == waiting.id and engine.NOT_FOR_PAYLOAD_KEY not in (taken.payload or {})
            await db.rollback()


async def test_a_queued_previews_line_names_the_video_ahead_and_its_remaining_previews() -> None:
    async with scratch_database(
        "video_preview_line", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner = await _owner(db)
            ahead, mine = await _video(db, owner, "X"), await _video(db, owner, "Y")
            await _previews(db, ahead, owner, 1)
            [queued] = await _previews(db, mine, owner, 1)
            await db.commit()
        async with scratch.sessions() as db:
            row = await db.get(VideoRender, queued.id)
            place = await jobs.queue_place("render", row, db=db)
        assert (place.ahead, place.position, place.lane) == (1, 2, "preview")
        assert [(video["title"], video["previews"], video["own"]) for video in place.as_dict()["videos_ahead"]] == [("X", 1, False)]
        line = jobs.describe(jobs.job_state("render", row, queue=place))
        assert line == "waiting for a worker: 1 preview of video X ahead (a worker makes 2 at a time)"
