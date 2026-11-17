"""The lab's ready-render seed run twice on PostgreSQL (#11713 retest): the second run finds the video again.

``.xtm-one/lab/seed_video_ready_render.py`` writes one video, its ready current render and two files (the committed
MP4 and poster) through the backend's own models; a lab restart runs it again on the same database. Here it runs
twice on a scratch database: one video, one render and two file rows after both, the second answer ``found_again``.
The file store is a stand-in (no S3 in this test); the rows are real.

``integration``-marked and skips when PostgreSQL is unreachable; the temporary database is ``tests/scratch_database.py``.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models.file_attachment import FileAttachment
from app.models.user import User
from app.models.video_studio import JOB_READY, VideoProject, VideoRender
from app.services.file_storage import file_storage
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

SEED = Path(__file__).resolve().parents[2] / ".xtm-one" / "lab" / "seed_video_ready_render.py"


def _seed_module():
    spec = importlib.util.spec_from_file_location("lab_seed_ready_render_pg", SEED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(not SEED.is_file(), reason="no .xtm-one next to the backend")
async def test_the_ready_render_seed_run_twice_writes_one_video_one_render_and_two_files(monkeypatch):
    uploaded: list[str] = []

    async def upload_path(path, object_name, content_type=None):
        assert Path(path).stat().st_size > 0
        uploaded.append(object_name)

    monkeypatch.setattr(file_storage, "upload_path", upload_path)
    module = _seed_module()
    async with scratch_database(
        "lab_ready_render", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner = User(email="lab@example.com", hashed_password="x", full_name="Lab Admin", role="admin")
            db.add(owner)
            await db.commit()
            owner_id = str(owner.id)

        first = await module.seed(owner_id)
        second = await module.seed(owner_id)

        assert first["found_again"] is False and second["found_again"] is True
        assert second["video"] == first["video"] and second["render"] == first["render"]
        assert len(uploaded) == 2
        async with scratch.sessions() as db:
            videos = await db.scalar(select(func.count()).select_from(VideoProject))
            renders = (await db.execute(select(VideoRender))).scalars().all()
            files = await db.scalar(select(func.count()).select_from(FileAttachment))
            video = await db.get(VideoProject, uuid.UUID(first["video"]))
        assert videos == 1 and files == 2 and len(renders) == 1
        render = renders[0]
        assert render.status == JOB_READY and render.file_id is not None and render.poster_file_id is not None
        assert str(video.latest_render_id) == first["render"] and video.title == module.VIDEO_TITLE
