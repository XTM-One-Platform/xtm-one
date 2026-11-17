"""One render per revision and quality, and scene previews queued per video, against PostgreSQL.

Every actor an ordinary user (rule 19: an admin would short-circuit the
visibility filter):

* relaunching a render of a revision in flight answers the same render - the
  route and ``render_video`` alike -, which the render list shows once and
  the audit records once; a render the engine took back from a lost lease is
  that same render; another quality still replaces it;
* the preview lane is dealt one preview per video in turn
  #(6295): a preview asked after another video's ten is queued - never
  refused - second, right after their first, the start answers its place,
  each of theirs after the first moves back one place, and the lane's slots
  claim them in that order (``claim_due(deal=...)``); past its cap a video's
  preview is queued too, the cap bounding how many run at once;
* a preview waits while its video runs its cap of previews, read from the
  executions the worker runs.

``integration``-marked and skips when PostgreSQL is unreachable; the
temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import video_studio
from app.core.config import settings
from app.core.database import get_db
from app.models.audit_log import AuditLog
from app.models.orchestration import OrchestrationExecution
from app.models.user import User
from app.models.video_studio import JOB_CANCELLED, JOB_PENDING, JOB_RUNNING, VideoProject, VideoRender
from app.services.builtin_tools import video_studio as tool
from app.services.video_studio import jobs
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("video_studio_enabled")]

_SPEC = {
    "title": "Launch film",
    "freestyle": True,
    "scenes": [{"id": "open", "type": "title", "title": "Launch"}, {"id": "close", "type": "title", "title": "Thanks"}],
}


@pytest.fixture(autouse=True)
def no_worker(monkeypatch):
    """No worker to wake, and no wait inside a tool call."""

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(jobs, "wake", nothing)
    monkeypatch.setattr(tool, "_wait_budget", lambda _default: 0.0)


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


async def _owner(db) -> User:
    owner = User(email="samuel@filigran.io", hashed_password="x", full_name="Samuel Hassine", role="user")
    db.add(owner)
    await db.flush()
    return owner


async def _project(db, owner: User, title: str = "Launch film") -> VideoProject:
    project = VideoProject(user_id=owner.id, title=title, spec={**_SPEC, "title": title}, scene_count=2)
    db.add(project)
    await db.flush()
    return project


async def _render_rows(db, project: VideoProject, *, previews: bool = False) -> list[VideoRender]:
    kind = VideoRender.scene_id.is_not(None) if previews else VideoRender.scene_id.is_(None)
    return list(
        (await db.execute(select(VideoRender).where(VideoRender.project_id == project.id, kind))).scalars().all()
    )


async def test_a_relaunch_on_a_revision_in_flight_answers_the_same_render() -> None:
    async with scratch_database(
        "video_reuse", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner = await _owner(db)
            project = await _project(db, owner)
            await db.commit()

        url = f"/api/v1/video-studio/projects/{project.id}/renders"
        async with _client(scratch.sessions, owner) as client:
            first = await client.post(url, json={"quality": "high"})
            again = await client.post(url, json={"quality": "high"})
            assert first.status_code == again.status_code == 202, again.text
            assert again.json()["id"] == first.json()["id"] and again.json()["reused"] is True
            assert first.json()["reused"] is False and again.json()["superseded_render_ids"] == []
            listed = (await client.get(url)).json()["items"]
        assert [item["id"] for item in listed] == [first.json()["id"]]

        async with scratch.sessions() as db:
            [row] = await _render_rows(db, project)
            assert row.status == JOB_PENDING
            triggers = (
                await db.execute(
                    select(func.count()).select_from(AuditLog).where(AuditLog.action == "trigger", AuditLog.entity_id == project.id)
                )
            ).scalar_one()
            assert triggers == 1


async def test_a_render_taken_back_from_a_lost_lease_is_the_one_a_relaunch_follows() -> None:
    async with scratch_database(
        "video_reuse_lease", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner = await _owner(db)
            project = await _project(db, owner)
            render = VideoRender(
                project_id=project.id, user_id=owner.id, revision=1, quality="high", status=JOB_RUNNING, progress=63
            )
            db.add(render)
            await db.flush()
            await jobs.enqueue(db, "render", render)
            # The engine reclaimed the execution of a worker that died: pending again, one attempt spent.
            execution = await db.get(OrchestrationExecution, render.execution_id)
            execution.state, execution.attempt_count, execution.lease_owner = "pending", 1, None
            render.stage = "Starting again: lease expired at 63%"
            await db.commit()

        url = f"/api/v1/video-studio/projects/{project.id}/renders"
        _defs, executor = tool.build_video_studio_tools(user_id=str(owner.id))
        answer = await executor.execute_tool(
            ToolCall(id="r", name="render_video", arguments={"video_id": str(project.id), "quality": "high"})
        )
        assert not answer.is_error, answer.content
        assert f"render `{render.id}`, queued or running, is the one followed" in answer.content
        async with _client(scratch.sessions, owner) as client:
            assert [item["id"] for item in (await client.get(url)).json()["items"]] == [str(render.id)]
            # Another quality is another render: it replaces this one, as before.
            draft = await client.post(url, json={"quality": "draft"})
            assert draft.json()["superseded_render_ids"] == [str(render.id)] and draft.json()["reused"] is False

        async with scratch.sessions() as db:
            assert (await db.get(VideoRender, render.id)).status == JOB_CANCELLED


async def _queued_previews(db, project: VideoProject, owner: User, count: int) -> list[VideoRender]:
    """*count* previews of the video's opening scene waiting in the queue (a preview of `close` replaces none)."""
    rows = []
    for index in range(count):
        row = VideoRender(
            project_id=project.id,
            user_id=owner.id,
            revision=1,
            quality="draft",
            status=JOB_PENDING,
            scene_id="open",
            created_at=datetime.now(UTC) - timedelta(minutes=count - index),
        )
        db.add(row)
        await db.flush()
        await jobs.enqueue(db, "render", row)
        rows.append(row)
    return rows


async def test_the_preview_lane_is_dealt_round_robin_by_video(monkeypatch) -> None:
    monkeypatch.setattr(settings, "video_studio_max_renders_per_user", 10)
    async with scratch_database(
        "video_preview_queue", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner = await _owner(db)
            edited = await _project(db, owner, "Brand film")
            other = await _project(db, owner, "Event loop")
            queued = await _queued_previews(db, other, owner, 10)
            await db.commit()
        async with scratch.sessions() as db:
            before = [(await jobs.queue_place("render", await db.get(VideoRender, r.id), db=db)).position for r in queued]
        assert before == list(range(1, 11))

        async with _client(scratch.sessions, owner) as client:
            started = await client.post(f"/api/v1/video-studio/projects/{edited.id}/scene-renders", json={"scene": 0})
            assert started.status_code == 202, started.text
            # Dealt into the first round: right after the other video's first preview, never behind its ten.
            assert started.json()["queue"]["position"] == 2 and started.json()["queue"]["ahead"] == 1
            assert started.json()["queue"]["lane"] == "preview"

        async with scratch.sessions() as db:
            [preview] = await _render_rows(db, edited, previews=True)
            # Its slots claim it so: the other video's first, then this one, then the other video's second.
            from app.services import orchestration_engine as engine
            from app.services.orchestration_adapters.video_studio import VideoPreviewAdapter

            claimed = await engine.claim_due(
                db, adapter=jobs.PREVIEW_ADAPTER, worker_id="w1", limit=3, deal=VideoPreviewAdapter().deal_claims
            )
            assert [row.id for row in claimed] == [queued[0].execution_id, preview.execution_id, queued[1].execution_id]
            await db.rollback()
        async with scratch.sessions() as db:
            [preview] = await _render_rows(db, edited, previews=True)
            # In the preview lane, like every other preview: never on a whole render's slot.
            adapters = (
                await db.execute(
                    select(OrchestrationExecution.adapter).where(
                        OrchestrationExecution.id.in_([preview.execution_id, *(row.execution_id for row in queued)])
                    )
                )
            ).scalars().all()
            assert set(adapters) == {jobs.PREVIEW_ADAPTER}
            # The other video's first keeps its place, each of the others moves back one: one turn in two each.
            after = [(await jobs.queue_place("render", await db.get(VideoRender, r.id), db=db)).position for r in queued]
            assert after == [1, *range(3, 12)]

        async with _client(scratch.sessions, owner) as client:
            # Past its cap the other video's next preview is queued too: the cap bounds how many run.
            eleventh = await client.post(f"/api/v1/video-studio/projects/{other.id}/scene-renders", json={"scene": 1})
            assert eleventh.status_code == 202, eleventh.text
        async with scratch.sessions() as db:
            assert len(await _render_rows(db, other, previews=True)) == 11


async def test_a_preview_waits_while_its_video_runs_its_cap_of_previews(monkeypatch) -> None:
    monkeypatch.setattr(settings, "video_studio_max_renders_per_user", 2)
    async with scratch_database(
        "video_preview_cap", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner = await _owner(db)
            edited = await _project(db, owner, "Brand film")
            other = await _project(db, owner, "Event loop")
            mine = await _queued_previews(db, edited, owner, 3)
            theirs = await _queued_previews(db, other, owner, 2)
            # Two of the video's previews and both of the other video's claimed by the worker.
            for row in (*mine[:2], *theirs):
                (await db.get(OrchestrationExecution, row.execution_id)).state = "running"
            whole = VideoRender(project_id=edited.id, user_id=owner.id, revision=1, quality="high", status=JOB_PENDING)
            db.add(whole)
            await db.flush()
            await jobs.enqueue(db, "render", whole)
            await db.commit()

        waits = await jobs.preview_waits(mine[2].id)
        assert waits == "Waiting for a preview of this video to end: 2 are being made (at most 2 at once)."
        # A running preview never counts itself, another video's never count, and a whole render never waits here.
        assert await jobs.preview_waits(mine[1].id) is None
        assert await jobs.preview_waits(whole.id) is None
        async with scratch.sessions() as db:
            (await db.get(OrchestrationExecution, mine[0].execution_id)).state = "completed"
            await db.commit()
        assert await jobs.preview_waits(mine[2].id) is None
