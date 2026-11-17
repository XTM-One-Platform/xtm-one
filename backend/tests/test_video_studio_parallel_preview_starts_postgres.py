"""The scene previews of one video asked in one parallel round all start (#8545).

Eight ``render_video_scene`` calls on one video in one round: three answered the raw ``QueryCanceledError`` of
``pg_advisory_xact_lock``. Every start took the video-wide render-start lock and the person's, then computed what
the preview is made from under them, so the previews of a sweep ran one after another and the later ones waited past
the statement timeout. What is proven, against PostgreSQL (the locks are PostgreSQL's), every actor an ordinary user:

* ten preview starts of ten scenes of one video, each on its own session, run side by side: every one answers a
  queued render, ten rows, ten jobs;
* a preview start never waits on a start of another scene, nor on a whole render's start holding the video's lock;
* a wait for its own scene's lock that runs out raises :class:`projects.RenderStartBusyError`, in plain words,
  never the SQL; nothing is queued.

``integration``-marked and skips when PostgreSQL is unreachable (``tests/scratch_database.py``).
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select, text

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.user import User
from app.models.video_studio import JOB_PENDING, VideoProject, VideoRender
from app.services.video_studio import jobs, kept_previews, projects, render_checks
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = [pytest.mark.integration]

SCENES = [{"id": f"s{n}", "type": "title", "title": f"Scene {n}"} for n in range(10)]
SPEC = {"title": "Sweep", "freestyle": True, "scenes": SCENES}


@pytest.fixture
def stand_ins(monkeypatch):
    """The lint, its advisories and what a preview is made from given; each one slow, as a real sweep's are."""

    async def lint(_db, _project, _user, *, quality, scene_id=None):
        applied = await projects.effective_spec(_db, _project, _user)
        return applied.spec, [], []

    async def nothing_ahead(*_args, **_kwargs):
        return []

    async def made_from(*_args, **_kwargs):
        # What a preview is made from takes a while to read (digests, estimate, voices): read side by side.
        await asyncio.sleep(0.3)
        return None

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(projects, "_lint", lint)
    monkeypatch.setattr(render_checks, "soft_ahead", nothing_ahead)
    monkeypatch.setattr(kept_previews, "inputs_or_none", made_from)
    monkeypatch.setattr(jobs, "wake", nothing)


async def _setup(scratch) -> dict:
    async with scratch.sessions() as db:
        owner = User(email="maker@example.com", hashed_password="x", full_name="Video Maker", role="user")
        db.add(owner)
        await db.flush()
        project = VideoProject(user_id=owner.id, title="Sweep", spec=SPEC, scene_count=len(SCENES), revision=1)
        db.add(project)
        await db.commit()
        return {"owner": owner.id, "project": project.id}


async def _start(scratch, made: dict, scene: str) -> VideoRender:
    async with scratch.sessions() as db:
        project = await db.get(VideoProject, made["project"])
        owner = await db.get(User, made["owner"])
        started = await projects.start_scene_render(db, project, owner, scene=scene)
        await db.commit()
        return started.render


async def test_ten_parallel_preview_starts_of_one_video_all_answer_a_render(stand_ins) -> None:
    async with scratch_database("video_parallel_previews", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as scratch:
        made = await _setup(scratch)
        renders = await asyncio.wait_for(
            asyncio.gather(*(_start(scratch, made, scene["id"]) for scene in SCENES)), timeout=20
        )
        assert sorted(render.scene_id for render in renders) == sorted(scene["id"] for scene in SCENES)
        async with scratch.sessions() as db:
            rows = (await db.execute(select(VideoRender).where(VideoRender.project_id == made["project"]))).scalars().all()
            assert len(rows) == 10
            assert all(row.status == JOB_PENDING and row.execution_id is not None for row in rows)


async def test_a_preview_start_never_waits_on_another_scene_nor_on_a_whole_render_start(stand_ins) -> None:
    async with scratch_database("video_preview_lock_scope", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as scratch:
        made = await _setup(scratch)
        async with scratch.sessions() as holder:
            project = await holder.get(VideoProject, made["project"])
            owner = await holder.get(User, made["owner"])
            # A whole render's start and a preview of scene s0, both holding their locks, uncommitted.
            await projects._lock_render_starts(holder, project, owner)
            await projects._lock_scene_preview_start(holder, project, "s0")
            render = await asyncio.wait_for(_start(scratch, made, "s1"), timeout=10)
            assert render.scene_id == "s1"
            await holder.rollback()


async def test_a_lock_wait_that_runs_out_is_said_plainly_and_queues_nothing(stand_ins) -> None:
    async with scratch_database("video_preview_lock_busy", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as scratch:
        made = await _setup(scratch)
        async with scratch.sessions() as holder:
            project = await holder.get(VideoProject, made["project"])
            await projects._lock_scene_preview_start(holder, project, "s3")
            async with scratch.sessions() as db:
                await db.execute(text("SET LOCAL lock_timeout = '300ms'"))
                video = await db.get(VideoProject, made["project"])
                owner = await db.get(User, made["owner"])
                with pytest.raises(projects.RenderStartBusyError) as raised:
                    await projects.start_scene_render(db, video, owner, scene="s3")
                said = str(raised.value)
                assert "nothing was queued" in said and "pg_advisory" not in said and "SQL" not in said
                await db.rollback()
            await holder.rollback()
        async with scratch.sessions() as db:
            count = (await db.execute(select(func.count()).select_from(VideoRender))).scalar_one()
            assert count == 0
