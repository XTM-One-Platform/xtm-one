"""A scene preview whose every frame is cached is served at once, before the previews queued ahead of it.

Previewing an unchanged figure scene took 12 minutes: it queued in its video's preview lane behind two insert
scenes' previews, although its last preview had drawn all 744 of its frames. What is proven, against PostgreSQL
(the engine's own claim, the rows a render finishes), every actor an ordinary user (rule 19):

* with two insert scenes' previews queued first, a preview of the figure scene made from the same inputs as its
  ready preview is served on the worker that asked within 30 s: ready, its answer saying "744 of 744 frames
  cached, encoded in N s", its MP4, frames and the files its checks show copied as its own, recorded as the
  scene's drawing - while both insert previews are still queued, never started;
* its job stands held behind the serve's grace: a claim of the lane now takes the insert previews, never it, and
  once run it ends at once on its finished row;
* a kept preview that cannot be read gives the preview back to the lane, its job due at once.

``integration``-marked and skips when PostgreSQL is unreachable (``tests/scratch_database.py``). The object store
and ffmpeg are stood in for: ``test_video_studio_kept_preview_6356.py`` encodes for real.
"""

from __future__ import annotations

import shutil
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.core.config import settings
from app.models.file_attachment import FileAttachment
from app.models.orchestration import OrchestrationExecution
from app.models.user import User
from app.models.video_studio import JOB_PENDING, JOB_READY, VideoProject, VideoRender
from app.services import orchestration_engine as engine
from app.services.orchestration_adapters.video_studio import VideoPreviewAdapter
from app.services.video_studio import assets, encoder, jobs, kept_previews, projects, render, render_answer
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = [pytest.mark.integration]

FLOW = {"type": "process_flow", "steps": [{"title": "Collect"}, {"title": "Enrich"}, {"title": "Act"}]}
SPEC = {
    "title": "Brand film",
    "freestyle": True,
    "scenes": [
        {"id": "s1", "type": "title", "title": "The platform at work"},
        {"id": "fig", "type": "figure", "block_spec": FLOW, "duration": 24, "perspective": "tilt"},
        {"id": "s3", "type": "title", "title": "In production"},
    ],
}
MADE_FROM = kept_previews.KeptInputs("made-from-the-same-inputs", {"fig": "drawing-of-fig"}, {"fig": "content-of-fig"})


async def _file(db, owner: User, name: str, description: str | None = None) -> FileAttachment:
    row = FileAttachment(
        user_id=owner.id, filename=name, content_type="image/jpeg", size=3, s3_object_name=f"kept/{name}", source="video_render",
        description=description,
    )
    db.add(row)
    await db.flush()
    return row


@pytest.fixture
def stand_ins(monkeypatch, tmp_path):
    """The object store and ffmpeg stood in for; the lint and what a preview is made from given."""
    from app.services import file_storage as storage_module

    async def lint(_db, _project, _user, *, quality, scene_id=None):
        applied = await projects.effective_spec(_db, _project, _user)
        return applied.spec, [], []

    async def made_from(*_args, **_kwargs):
        return MADE_FROM

    readable = {"ok": True}

    async def download(file_id, path):
        if not readable["ok"]:
            return None
        row = await assets.load_file(file_id)
        if row is None:
            return None
        with open(path, "wb") as fh:
            fh.write(b"kept")
        return row

    async def upload(*_args, **_kwargs):
        return None

    async def remux(*, source_path, out_path):
        shutil.copyfile(source_path, out_path)

    async def probe(_path):
        return encoder.MediaInfo(duration=24.8, width=1920, height=1080, fps=30.0, frame_count=744, has_audio=True, has_video=True)

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(settings, "video_studio_max_renders_per_user", 1)
    monkeypatch.setattr(projects, "_lint", lint)
    monkeypatch.setattr(kept_previews, "inputs_or_none", made_from)
    monkeypatch.setattr(assets, "download_file", download)
    monkeypatch.setattr(storage_module.file_storage, "upload_path", upload)
    monkeypatch.setattr(encoder, "remux", remux)
    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(jobs, "wake", nothing)
    return readable


async def _setup(scratch) -> dict:
    async with scratch.sessions() as db:
        owner = User(email="samuel@filigran.io", hashed_password="x", full_name="Samuel Hassine", role="user")
        db.add(owner)
        await db.flush()
        project = VideoProject(user_id=owner.id, title="Brand film", spec=SPEC, scene_count=3, revision=4)
        db.add(project)
        await db.flush()
        mp4 = await _file(db, owner, "brand-film-fig.mp4", "Preview of scene fig of the video Brand film")
        frames = [await _file(db, owner, f"brand-film-fig-{name}.jpg", f"The {name} settled frame") for name in ("first", "mid", "last")]
        proof = await _file(db, owner, "brand-film-fig-voice.jpg", "A finding's frame")
        kept = VideoRender(
            project_id=project.id,
            user_id=owner.id,
            revision=3,
            quality="standard",
            status=JOB_READY,
            scene_id="fig",
            file_id=mp4.id,
            frame_file_ids=[str(f.id) for f in frames],
            width=1920,
            height=1080,
            fps=30,
            duration_ms=24800,
            notes=["Scene fig previewed at standard quality (1920x1080, 30 fps)."],
            checks={
                "status": "findings",
                "findings": [
                    {"kind": "frozen", "severity": "warn", "at": 22.0, "scene_id": "fig", "message": "still 0.6 s", "data": {"frame_file_id": str(proof.id)}}
                ],
            },
            measured={"kept_as": MADE_FROM.digest, "frames_total": 744, "frames": 744, "seconds": 410.0},
            started_at=datetime.now(UTC) - timedelta(hours=1),
            finished_at=datetime.now(UTC) - timedelta(minutes=50),
        )
        db.add(kept)
        # Two insert scenes' previews, queued first in the video's lane: nothing of them is cached.
        inserts = []
        for scene_id in ("s1", "s3"):
            row = VideoRender(project_id=project.id, user_id=owner.id, revision=4, quality="standard", status=JOB_PENDING, scene_id=scene_id)
            db.add(row)
            await db.flush()
            await jobs.enqueue(db, "render", row)
            inserts.append(row)
        await db.commit()
        return {"owner": owner.id, "project": project.id, "kept": kept, "inserts": [row.id for row in inserts], "proof": proof.id}


async def test_a_fully_cached_preview_is_served_before_the_insert_previews_queued_ahead_of_it(stand_ins) -> None:
    async with scratch_database(
        "video_kept_preview", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        made = await _setup(scratch)
        began = time.monotonic()
        async with scratch.sessions() as db:
            project = await db.get(VideoProject, made["project"])
            owner = await db.get(User, made["owner"])
            started = await projects.start_scene_render(db, project, owner, scene="fig")
            await db.commit()
        await projects.run_started(started)
        took = time.monotonic() - began
        assert took < 30
        assert started.kept is not None and started.kept.render_id == str(made["kept"].id) and started.queue is None

        async with scratch.sessions() as db:
            served = await db.get(VideoRender, started.render.id)
            assert served.status == JOB_READY and served.scene_id == "fig"
            assert served.notes[0].startswith("744 of 744 frames cached, encoded in ")
            assert f"scene preview `{made['kept'].id}` drew them" in served.notes[0]
            assert served.measured["kept_as"] == MADE_FROM.digest and served.measured["frames_cached"] == 744
            # Its own files: the MP4, the three frames, the frame its checks name - none the kept preview's.
            kept_files = {str(made["kept"].file_id), *made["kept"].frame_file_ids, str(made["proof"])}
            own = [str(served.file_id), *served.frame_file_ids]
            [finding] = served.checks["findings"]
            own.append(finding["data"]["frame_file_id"])
            assert len(own) == 5 and not kept_files & set(own)
            for file_id in own:
                row = await db.get(FileAttachment, file_id)
                assert row is not None and row.user_id == made["owner"]
            assert (await db.get(FileAttachment, served.frame_file_ids[0])).description == "The first settled frame"
            # Recorded as the scene's drawing, like any preview.
            video = await db.get(VideoProject, made["project"])
            assert video.scene_previews["fig"]["render_id"] == str(served.id)
            assert video.scene_previews["fig"]["digest"] == "drawing-of-fig"
            # The insert previews queued ahead of it: still queued, never started.
            for insert_id in made["inserts"]:
                insert = await db.get(VideoRender, insert_id)
                assert insert.status == JOB_PENDING and insert.started_at is None
            # Its job is held behind the serve's grace: the lane's next claims are the insert previews.
            held = await db.get(OrchestrationExecution, served.execution_id)
            assert held.state == "pending" and held.run_at > datetime.now(UTC) + timedelta(seconds=kept_previews.SERVE_GRACE_SECONDS - 60)
        async with scratch.sessions() as db:
            claimed = await engine.claim_due(
                db, adapter=jobs.PREVIEW_ADAPTER, worker_id="w1", limit=5, deal=VideoPreviewAdapter().deal_claims
            )
            renders = (await db.execute(select(VideoRender).where(VideoRender.execution_id.in_([c.id for c in claimed])))).scalars().all()
            assert {row.scene_id for row in renders} <= {"s1", "s3"} and renders
            await db.rollback()
        # Its answer says it first.
        async with scratch.sessions() as db:
            served = await db.get(VideoRender, started.render.id)
            answer = render_answer.of_render(served, served.checks, facts=[])
            said = [fact["what"] for fact in answer.facts]
            assert any(text.startswith("744 of 744 frames cached, encoded in ") for text in said), said
        # Run once its grace is over, its job ends at once on the finished row: nothing is drawn again.
        outcome = await render.run_render(str(started.render.id))
        assert outcome.kind == "done"


async def test_a_kept_preview_that_cannot_be_read_gives_the_preview_back_to_the_lane(stand_ins) -> None:
    async with scratch_database(
        "video_kept_preview_gone", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        made = await _setup(scratch)
        stand_ins["ok"] = False
        async with scratch.sessions() as db:
            project = await db.get(VideoProject, made["project"])
            owner = await db.get(User, made["owner"])
            started = await projects.start_scene_render(db, project, owner, scene="fig")
            await db.commit()
        assert started.kept is not None
        assert not await kept_previews.serve(str(started.render.id), started.kept)
        async with scratch.sessions() as db:
            row = await db.get(VideoRender, started.render.id)
            assert row.status == JOB_PENDING and row.stage == "Queued" and row.file_id is None
            execution = await db.get(OrchestrationExecution, row.execution_id)
            assert execution.state == "pending" and execution.run_at <= datetime.now(UTC)
