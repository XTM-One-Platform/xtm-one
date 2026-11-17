"""A new render of a video replaces its renders in progress, against PostgreSQL.

Every actor an ordinary user (rule 19: an admin would short-circuit the
visibility filter):

* starting a render while one of the video waits and another runs cancels
  both - their executions too - and each records the render that replaced
  it; a finished render of the video and a render of another video are left
  alone, and the audit row names the replaced renders;
* the limit of renders in progress per person counts the replaced ones out;
* ``render_video`` replaces the render the editor started and says so, and
  ``get_video_jobs`` on the replaced render names the one to follow - or says
  it no longer exists once it was deleted;
* two starts racing on one video leave one render of it in progress;
* a replaced render's conversation is owed the new render when the same
  person started it, never across people; a failure written as it is
  replaced, or its final save, leave it replaced;
* making a render current while the render retention runs never loses it,
  and the routes refuse to make an unfinished render current and cancel a
  running one they delete.

``integration``-marked and skips when PostgreSQL is unreachable; the
temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import video_studio
from app.core.config import settings
from app.core.database import get_db
from app.models.audit_log import AuditLog
from app.models.orchestration import OrchestrationExecution
from app.models.user import User
from app.models.video_studio import (
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_PENDING,
    JOB_READY,
    JOB_RUNNING,
    VideoProject,
    VideoRender,
)
from app.services.builtin_tools import video_studio as tool
from app.services.video_studio import jobs, projects, render_checks
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("video_studio_enabled")]

_SPEC = {"title": "Launch film", "freestyle": True, "scenes": [{"type": "title", "title": "Launch"}]}


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


async def _seed(db) -> tuple[User, VideoProject]:
    owner = User(email="samuel@filigran.io", hashed_password="x", full_name="Samuel Hassine", role="user")
    db.add(owner)
    await db.flush()
    project = await _project(db, owner)
    return owner, project


async def _project(db, owner: User) -> VideoProject:
    project = VideoProject(user_id=owner.id, title="Launch film", spec=_SPEC, scene_count=1)
    db.add(project)
    await db.flush()
    return project


async def _render(db, project: VideoProject, owner: User, status: str, *, minutes_ago: int = 1) -> VideoRender:
    """A render as the worker left it, with its execution when it has not finished."""
    render = VideoRender(
        project_id=project.id,
        user_id=owner.id,
        revision=1,
        quality="high",
        status=status,
        progress=38 if status == JOB_RUNNING else 0,
        created_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
    )
    db.add(render)
    await db.flush()
    if status in (JOB_PENDING, JOB_RUNNING):
        await jobs.enqueue(db, "render", render)
    return render


async def _execution_state(db, render: VideoRender) -> str:
    return (
        await db.execute(select(OrchestrationExecution.state).where(OrchestrationExecution.id == render.execution_id))
    ).scalar_one()


async def test_a_new_render_cancels_the_video_s_renders_in_progress() -> None:
    async with scratch_database(
        "video_supersede", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner, project = await _seed(db)
            other = await _project(db, owner)
            running = await _render(db, project, owner, JOB_RUNNING, minutes_ago=20)
            queued = await _render(db, project, owner, JOB_PENDING, minutes_ago=8)
            finished = await _render(db, project, owner, JOB_READY, minutes_ago=120)
            elsewhere = await _render(db, other, owner, JOB_RUNNING, minutes_ago=5)
            await db.commit()

        url = f"/api/v1/video-studio/projects/{project.id}/renders"
        async with _client(scratch.sessions, owner) as client:
            # Another quality than theirs: the same one again follows the render in flight.
            started = await client.post(url, json={"quality": "draft"})
            assert started.status_code == 202, started.text
            body = started.json()
            assert body["status"] == JOB_PENDING
            assert body["superseded_render_ids"] == [str(running.id), str(queued.id)]
            listed = {r["id"]: r for r in (await client.get(url)).json()["items"]}
        new_id = uuid.UUID(body["id"])

        async with scratch.sessions() as db:
            for replaced in (running, queued):
                row = await db.get(VideoRender, replaced.id)
                assert row.status == JOB_CANCELLED
                assert row.superseded_by_id == new_id
                assert row.error == projects.superseded_reason(new_id)
                assert await _execution_state(db, row) == "cancelled"
                assert listed[str(row.id)]["superseded_by_id"] == str(new_id)
            assert (await db.get(VideoRender, finished.id)).status == JOB_READY
            kept = await db.get(VideoRender, elsewhere.id)
            assert (kept.status, kept.superseded_by_id) == (JOB_RUNNING, None)
            assert await _execution_state(db, kept) == "pending"
            new = await db.get(VideoRender, new_id)
            assert await _execution_state(db, new) == "pending"
            assert listed[str(new_id)]["superseded_by_id"] is None
            audit = (
                await db.execute(select(AuditLog).where(AuditLog.action == "trigger", AuditLog.entity_id == project.id))
            ).scalar_one()
            assert audit.details["superseded_render_ids"] == [str(running.id), str(queued.id)]


async def test_the_render_limit_counts_the_replaced_renders_out(monkeypatch) -> None:
    monkeypatch.setattr(settings, "video_studio_max_renders_per_user", 3)
    async with scratch_database(
        "video_supersede_limit", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner, project = await _seed(db)
            for _ in range(2):
                await _render(db, await _project(db, owner), owner, JOB_RUNNING)
            await _render(db, project, owner, JOB_RUNNING)
            await db.commit()

        url = f"/api/v1/video-studio/projects/{project.id}/renders"
        async with _client(scratch.sessions, owner) as client:
            # Three in progress, one of them this video's: the new one replaces it.
            assert (await client.post(url, json={"quality": "draft"})).status_code == 202
            async with scratch.sessions() as db:
                await _render(db, await _project(db, owner), owner, JOB_RUNNING)
                await db.commit()
            # Another quality, so a new render (the same one again follows the draft in flight).
            refused = await client.post(url, json={"quality": "high"})
            assert refused.status_code == 429
            assert refused.json()["detail"] == (
                "You already have 3 renders queued or running (Launch film): "
                "wait for one to finish, or cancel one from its video's renders."
            )

        async with scratch.sessions() as db:
            in_progress = (
                await db.execute(
                    select(VideoRender.id).where(
                        VideoRender.project_id == project.id, VideoRender.status.in_((JOB_PENDING, JOB_RUNNING))
                    )
                )
            ).all()
            assert len(in_progress) == 1, "a refused start replaces nothing"


async def test_render_video_replaces_the_render_in_progress_and_points_to_it() -> None:
    async with scratch_database(
        "video_supersede_tool", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner, project = await _seed(db)
            await db.commit()

        async with _client(scratch.sessions, owner) as client:
            first = (await client.post(f"/api/v1/video-studio/projects/{project.id}/renders", json={"quality": "draft"}))
            first_id = first.json()["id"]

        _defs, executor = tool.build_video_studio_tools(user_id=str(owner.id))
        answer = await executor.execute_tool(
            ToolCall(id="r", name="render_video", arguments={"video_id": str(project.id), "quality": "high"})
        )
        assert not answer.is_error, answer.content
        assert f"replaced the render of the video still in progress (`{first_id}`), now cancelled" in answer.content
        new_id = answer.content.split("Render `")[1].split("`")[0]
        assert new_id != first_id

        polled = await executor.execute_tool(
            ToolCall(id="p", name="get_video_jobs", arguments={"render_ids": [first_id]})
        )
        assert f"Render `{first_id}` was cancelled: a newer render of the same video (`{new_id}`) replaced it" in (
            polled.content
        )
        assert f"get_video_jobs(render_ids=['{new_id}'])" in polled.content


async def test_two_starts_racing_on_one_video_leave_one_render_in_progress() -> None:
    async with scratch_database(
        "video_supersede_race", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner, project = await _seed(db)
            await db.commit()

        async def start(quality: str) -> projects.StartedRender:
            async with scratch.sessions() as db:
                video = await db.get(VideoProject, project.id)
                user = await db.get(User, owner.id)
                started = await projects.start_render(db, video, user, quality=quality)
                await db.commit()
                return started

        first, second = await asyncio.gather(start("draft"), start("high"))

        async with scratch.sessions() as db:
            rows = (await db.execute(select(VideoRender).where(VideoRender.project_id == project.id))).scalars().all()
        in_progress = [r for r in rows if r.status == JOB_PENDING]
        replaced = [r for r in rows if r.status == JOB_CANCELLED]
        assert len(in_progress) == 1 and len(replaced) == 1
        assert replaced[0].superseded_by_id == in_progress[0].id
        assert sorted([len(first.superseded), len(second.superseded)]) == [0, 1]


def _scratch(name: str):
    return scratch_database(name, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True)


async def test_a_replaced_render_hands_its_conversation_to_the_new_one_never_across_people() -> None:
    """A render whose turn ended, replaced from the editor, would end silently."""
    async with _scratch("video_supersede_owed") as scratch:
        conversation, other_conversation = uuid.uuid4(), uuid.uuid4()
        async with scratch.sessions() as db:
            owner, project = await _seed(db)
            colleague = User(email="ada@filigran.io", hashed_password="x", full_name="Ada Lovelace", role="user")
            db.add(colleague)
            await db.flush()
            first = await _render(db, project, owner, JOB_RUNNING)
            await jobs.enqueue(db, "render", first, generation=1, owed_to=conversation)
            await db.commit()

        async def start(user_id: uuid.UUID, quality: str, owed_to: uuid.UUID | None = None) -> VideoRender:
            async with scratch.sessions() as db:
                video, user = await db.get(VideoProject, project.id), await db.get(User, user_id)
                started = await projects.start_render(db, video, user, quality=quality, owed_to=owed_to)
                await db.commit()
                return started.render

        # Each start asks another quality: the same one again follows the render in flight, never replaces it.
        # The same person replaces it from the editor: the new render is owed to the conversation.
        second = await start(owner.id, "draft")
        async with scratch.sessions() as db:
            assert (await db.get(VideoRender, second.id)).owed_conversation_id == conversation
            assert (await db.get(VideoRender, first.id)).owed_conversation_id is None
        # A render started in a turn of its own keeps that conversation.
        third = await start(owner.id, "high", owed_to=other_conversation)
        async with scratch.sessions() as db:
            assert (await db.get(VideoRender, third.id)).owed_conversation_id == other_conversation
        # Another person replaces it: the wake would run as them - nothing is handed over.
        fourth = await start(colleague.id, "draft")
        async with scratch.sessions() as db:
            assert (await db.get(VideoRender, fourth.id)).owed_conversation_id is None


async def test_a_failure_or_a_final_save_never_overwrites_a_replaced_render(monkeypatch) -> None:
    """A replaced render ends cancelled, its reason kept, the files its save stored deleted."""
    from app.services.video_studio import assets, render

    deleted: list = []

    async def delete_files(files):
        deleted.extend(files)

    monkeypatch.setattr(assets, "delete_files", delete_files)
    async with _scratch("video_supersede_terminal") as scratch:
        async with scratch.sessions() as db:
            owner, project = await _seed(db)
            replaced = await _render(db, project, owner, JOB_RUNNING)
            await db.commit()
        async with scratch.sessions() as db:
            video, user = await db.get(VideoProject, project.id), await db.get(User, owner.id)
            started = await projects.start_render(db, video, user, quality="draft")
            await db.commit()
        reason = projects.superseded_reason(started.render.id)
        # The worker meets an encoder error at that moment: the failure is not written over the replacement.
        assert not await jobs.mark_if_open("render", replaced.id, status="failed", error="ffmpeg is gone")
        # Its final save finds it cancelled: the stored files go, the row stays replaced.
        stored = [uuid.uuid4(), uuid.uuid4()]
        assert not await render.finish_made(str(replaced.id), stored, {"file_id": stored[0]}, whole=True, checks=render_checks.Checks())
        assert deleted == stored
        # The worker stopped between frames: the reason survives, the end is stamped.
        await jobs.mark_cancelled("render", replaced.id)
        async with scratch.sessions() as db:
            row = await db.get(VideoRender, replaced.id)
            assert (row.status, row.error, row.file_id) == (JOB_CANCELLED, reason, None)
            assert row.superseded_by_id == started.render.id and row.finished_at is not None
            assert (await db.get(VideoProject, project.id)).latest_render_id is None
        # A render still running is ended by the same writes.
        assert await jobs.mark_if_open("render", started.render.id, status="failed", error="ffmpeg is gone")


async def test_a_made_render_ends_ready_or_failed_on_its_checks() -> None:
    """The final save keeps the render checks' outcome: a failed render is a draft to look at."""
    from app.services.video_studio import render

    async with _scratch("video_supersede_checks") as scratch:
        async with scratch.sessions() as db:
            owner, project = await _seed(db)
            refused, passed = (await _render(db, project, owner, JOB_RUNNING), await _render(db, project, owner, JOB_RUNNING))
            await db.commit()
        stutter = render_checks.Finding(render_checks.STUTTER, 4.0, 5.0, "s1", "the voice stutters")
        failing = render_checks.Checks(findings=[stutter])
        drawn = {"s1": "digest-1"}
        assert await render.finish_made(str(refused.id), [], {}, whole=True, checks=failing, checks_failed=True, scenes_drawn=drawn)
        async with scratch.sessions() as db:
            row, video = await db.get(VideoRender, refused.id), await db.get(VideoProject, project.id)
            assert row.status == JOB_FAILED and "stutters" in row.error and row.checks["findings"]
            assert video.latest_render_id is None and not video.scene_previews
        assert await render.finish_made(str(passed.id), [], {}, whole=True, checks=render_checks.Checks(), scenes_drawn=drawn)
        async with scratch.sessions() as db:
            row, video = await db.get(VideoRender, passed.id), await db.get(VideoProject, project.id)
            assert row.status == JOB_READY and row.error is None
            assert video.latest_render_id == passed.id and "s1" in video.scene_previews


async def test_an_older_revisions_render_is_asked_about_then_hands_its_segments_over(monkeypatch) -> None:
    """The editor asks before cancelling the render of an older revision; replaced, it keeps its segments for
    the new render while that one is to draw, and they go once it ended."""
    from app.services.file_storage import file_storage
    from app.services.video_studio import render_segments

    async with _scratch("video_supersede_older") as scratch:
        async with scratch.sessions() as db:
            owner, project = await _seed(db)
            older = await _render(db, project, owner, JOB_RUNNING)
            project.revision = 2
            await db.commit()

        url = f"/api/v1/video-studio/projects/{project.id}/renders"
        async with _client(scratch.sessions, owner) as client:
            asked = await client.post(url, json={"quality": "high"})
            assert asked.status_code == 409, asked.text
            detail = asked.json()["detail"]
            assert detail["code"] == "render_replace" and detail["revision"] == 2
            assert detail["renders"] == [{"id": str(older.id), "revision": 1, "quality": "high", "progress": 38}]
            async with scratch.sessions() as db:
                assert (await db.get(VideoRender, older.id)).status == JOB_RUNNING, "a question cancels nothing"
            started = await client.post(url, json={"quality": "high", "replace": True})
            assert started.status_code == 202, started.text
            body = started.json()
        assert body["superseded_render_ids"] == [str(older.id)]
        assert "replaced the render of revision 1 at 38% (high)" in body["superseded_line"]
        new_id = uuid.UUID(body["id"])

        folder = render_segments.prefix_of(str(older.id))
        stored = {f"{folder}d/seg-0000000-0000060.mp4"}

        async def listed(prefix: str, *, recursive: bool = True) -> list[str]:
            if not recursive:
                return [folder] if stored else []
            return sorted(name for name in stored if name.startswith(prefix))

        async def delete(name: str) -> None:
            stored.discard(name)

        monkeypatch.setattr(file_storage, "list_object_names", listed)
        monkeypatch.setattr(file_storage, "delete", delete)
        # Replaced by a render still to draw at its quality: its segments stay, the sweep keeps them.
        assert await render_segments.handed_over(str(older.id))
        assert await render_segments.sweep() == 0 and stored
        # The new render ended: they go.
        async with scratch.sessions() as db:
            (await db.get(VideoRender, new_id)).status = JOB_READY
            await db.commit()
        assert not await render_segments.handed_over(str(older.id))
        assert await render_segments.sweep() == 1 and not stored


async def test_a_replaced_render_whose_newer_one_was_deleted_says_so() -> None:
    async with _scratch("video_supersede_dangling") as scratch:
        async with scratch.sessions() as db:
            owner, project = await _seed(db)
            replaced = await _render(db, project, owner, JOB_RUNNING)
            await db.commit()
        async with scratch.sessions() as db:
            video, user = await db.get(VideoProject, project.id), await db.get(User, owner.id)
            newer = (await projects.start_render(db, video, user, quality="draft")).render
            await db.commit()
            await db.delete(await db.get(VideoRender, newer.id))
            await db.commit()
        _defs, executor = tool.build_video_studio_tools(user_id=str(owner.id))
        polled = await executor.execute_tool(
            ToolCall(id="p", name="get_video_jobs", arguments={"render_ids": [str(replaced.id)]})
        )
        assert f"(`{newer.id}`) replaced it, and that render no longer exists" in polled.content
        assert f"get_video_jobs(render_ids=['{newer.id}'])" not in polled.content


async def test_making_a_render_current_while_the_retention_runs_never_loses_it() -> None:
    """A sweep skips a video being changed, and judges it again once the change committed."""
    from app.services.video_studio import render_retention

    old = datetime.now(UTC) - timedelta(days=60)
    async with _scratch("video_retention_current") as scratch:
        async with scratch.sessions() as db:
            owner, project = await _seed(db)
            renders = []
            for minutes, status in ((0, JOB_READY), (1, JOB_READY), (2, JOB_FAILED)):
                row = VideoRender(
                    project_id=project.id,
                    user_id=owner.id,
                    revision=1,
                    quality="high",
                    status=status,
                    file_id=uuid.uuid4() if status == JOB_READY else None,
                    created_at=old - timedelta(minutes=minutes),
                    finished_at=old - timedelta(minutes=minutes),
                )
                db.add(row)
                renders.append(row)
            await db.flush()
            playing, chosen, failed = renders
            project.latest_render_id = playing.id
            await db.commit()

        # A manager makes the older, superseded render current: the video's row is held until the change commits.
        async with scratch.sessions() as editor:
            row = await editor.get(VideoRender, chosen.id)
            assert await render_retention.lock_render(editor, row)
            (await editor.get(VideoProject, project.id)).latest_render_id = chosen.id
            await editor.flush()
            async with scratch.sessions() as sweeper:
                assert (await render_retention.sweep(sweeper)).renders == 0  # the video is held: skipped, not waited on
            await editor.commit()
        async with scratch.sessions() as sweeper:
            # Committed since: the render now current stays, so does the newest ready one; the failed attempt goes.
            assert (await render_retention.sweep(sweeper)).renders == 1
        async with scratch.sessions() as db:
            assert await db.get(VideoRender, chosen.id) is not None
            assert await db.get(VideoRender, playing.id) is not None
            assert await db.get(VideoRender, failed.id) is None
            assert (await db.get(VideoProject, project.id)).latest_render_id == chosen.id


async def test_the_routes_refuse_an_unfinished_current_render_and_cancel_a_running_one_they_delete(monkeypatch) -> None:
    from app.services.video_studio import assets

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(assets, "delete_files", nothing)
    async with _scratch("video_render_routes") as scratch:
        async with scratch.sessions() as db:
            owner, project = await _seed(db)
            running = await _render(db, project, owner, JOB_RUNNING)
            await db.commit()
        async with _client(scratch.sessions, owner) as client:
            refused = await client.patch(f"/api/v1/video-studio/renders/{running.id}", json={"current": True})
            assert refused.status_code == 409
            assert refused.json()["detail"] == "Only a finished render can be the one the video plays."
            assert (await client.delete(f"/api/v1/video-studio/renders/{running.id}")).status_code == 204
            assert (await client.patch(f"/api/v1/video-studio/renders/{running.id}", json={"current": True})).status_code == 404
        async with scratch.sessions() as db:
            assert await db.get(VideoRender, running.id) is None
            assert await _execution_state(db, running) == "cancelled"
