"""A render start answers the ready render that already covers the revision, on PostgreSQL.

The latest ready whole render, read locked by the route's start as a worker never touches it: the editor's render of
an unchanged revision answers it (``covered_from``) with nothing queued, gated or audited, and ``force`` queues a new
render. ``integration``-marked and skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import video_studio
from app.core.database import get_db
from app.models.audit_log import AuditLog
from app.models.user import User
from app.models.video_studio import JOB_PENDING, JOB_READY, VideoProject, VideoRender
from app.services.video_studio import jobs, lexicon_store, projects, quality, render, render_checks
from app.services.video_studio.spec import parse_video_spec
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("video_studio_enabled")]

_SPEC = {"title": "Launch film", "freestyle": True, "scenes": [{"type": "title", "title": "Launch"}]}


@pytest.fixture(autouse=True)
def no_worker(monkeypatch):
    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(jobs, "wake", nothing)


@asynccontextmanager
async def _client(sessions: async_sessionmaker, user: User) -> AsyncIterator[httpx.AsyncClient]:
    api = FastAPI()
    api.include_router(video_studio.router, prefix="/api/v1/video-studio")

    async def _get_db():
        async with sessions() as db:
            yield db
            await db.commit()

    api.dependency_overrides[deps.get_current_user] = lambda: user
    api.dependency_overrides[get_db] = _get_db
    api.dependency_overrides[deps.require_ai_enabled] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        yield client


async def test_the_ready_render_of_an_unchanged_revision_is_answered_and_force_renders_again() -> None:
    async with scratch_database(
        "video_covering", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner = User(email="samuel@filigran.io", hashed_password="x", full_name="Samuel Hassine", role="user")
            db.add(owner)
            await db.flush()
            project = VideoProject(user_id=owner.id, title="Launch film", spec=_SPEC, scene_count=1)
            db.add(project)
            await db.flush()
            # What a render of this revision recorded as it drew it (``render.drawing_of``).
            effective = (await projects.effective_spec(db, project, owner)).spec
            lexicon = await lexicon_store.for_voiceover(db, parse_video_spec(effective).voiceover)
            marks = await render_checks.digest_scenes(db, effective, lexicon)
            ready = VideoRender(
                project_id=project.id,
                user_id=owner.id,
                revision=project.revision,
                quality="high",
                status=JOB_READY,
                finished_at=datetime.now(UTC),
                checks={"status": "passed", "findings": [], "quality_report_version": quality.QUALITY_REPORT_VERSION},
                drawing=render.drawing_of(None, project.revision, marks, render_checks.sound_sources(effective)),
            )
            db.add(ready)
            await db.commit()

        url = f"/api/v1/video-studio/projects/{project.id}/renders"
        async with _client(scratch.sessions, owner) as client:
            covered = await client.post(url, json={"quality": "high"})
            assert covered.status_code == 202, covered.text
            body = covered.json()
            assert body["id"] == str(ready.id) and body["covered_from"] == project.revision and body["status"] == JOB_READY

            async with scratch.sessions() as db:
                renders = (await db.execute(select(func.count()).select_from(VideoRender))).scalar_one()
                audits = (await db.execute(select(func.count()).select_from(AuditLog).where(AuditLog.action == "trigger"))).scalar_one()
            assert (renders, audits) == (1, 0)

            forced = await client.post(url, json={"quality": "high", "force": True})
            assert forced.status_code == 202, forced.text
            again = forced.json()
            assert again["id"] != str(ready.id) and again["covered_from"] is None and again["status"] == JOB_PENDING
