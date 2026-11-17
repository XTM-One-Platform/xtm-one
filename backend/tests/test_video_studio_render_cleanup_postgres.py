"""Cleaning up a video's renders and the render retention, against PostgreSQL.

The pure planning is ``test_video_studio_render_cleanup.py``; this one proves
the deletion: every actor an ordinary user (rule 19), the files in a fake
object store.

* ``delete_video_renders(select: all_but_latest_ready, keep_quality: high)``
  on the video of the issue leaves its revision 31 1080p render, deletes the
  rest with every file they own - MP4s, posters, captions, frames, finding
  frames, insert crops and the segments kept for a resume - frees exactly
  their bytes, and ``get_video`` then lists that one render with its size;
* a running render is refused by id and the rest go; the render the public
  link plays goes only with ``confirm``, the link then playing the 1080p;
* a dry run deletes nothing and answers the list the deletion answers;
* the nightly sweep deletes exactly the renders past their days (2, 8, 31
  days old), under the days an administrator sets;
* a scene's new preview replaces its previous ones;
* on a video shared to watch only, the Studio's cleanup deletes the caller's
  own render and refuses the others.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours; the temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

import json
import re
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
from app.core.database import get_db
from app.models.audit_log import AuditLog
from app.models.file_attachment import FileAttachment
from app.models.user import User
from app.models.video_studio import (
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_READY,
    JOB_RUNNING,
    JOB_TERMINAL_STATUSES,
    VideoProject,
    VideoRender,
)
from app.services.builtin_tools.video_studio import build_video_studio_tools
from app.services.video_studio import public_links, render_retention
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("video_studio_enabled")]

_TITLE = "Brand Film"
_SPEC = {"title": _TITLE, "freestyle": True, "scenes": [{"type": "title", "title": "Brand"}]}


@pytest.fixture
def store(monkeypatch) -> dict[str, bytes]:
    """The object store: what the renders' files and kept segments hold, deleted as the platform deletes them."""
    from app.services import file_storage as storage

    objects: dict[str, bytes] = {}

    async def delete(name):
        objects.pop(name, None)

    async def list_object_names(prefix, recursive=True):
        return sorted(name for name in objects if name.startswith(prefix))

    monkeypatch.setattr(storage.file_storage, "delete", delete)
    monkeypatch.setattr(storage.file_storage, "list_object_names", list_object_names)
    return objects


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
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        yield client


async def _tool(user: User, name: str, **arguments):
    _defs, executor = build_video_studio_tools(user_id=str(user.id))
    return await executor.execute_tool(ToolCall(id=name, name=name, arguments=arguments))


def _answer(content: str) -> dict:
    return json.loads(re.search(r"```json\n(.*)\n```", content, re.S).group(1))


def _user(email: str, *, role: str = "user") -> User:
    return User(email=email, hashed_password="x", full_name=email.split("@")[0].title(), role=role)


def _scratch(prefix: str):
    return scratch_database(prefix, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True)


class Film:
    """The video of the issue, stored: its renders by role and the bytes each one's files hold."""

    def __init__(self, db, objects: dict[str, bytes], owner: User) -> None:
        self.db, self.objects, self.owner = db, objects, owner
        self.bytes: dict[uuid.UUID, int] = {}
        self.keys: dict[uuid.UUID, set[str]] = {}

    async def file(self, name: str, size: int) -> FileAttachment:
        key = f"renders/{uuid.uuid4().hex}/{name}"
        self.objects[key] = b"x" * size
        row = FileAttachment(user_id=self.owner.id, filename=name, content_type="application/octet-stream", size=size, s3_object_name=key)
        self.db.add(row)
        await self.db.flush()
        return row

    async def render(
        self,
        project: VideoProject,
        *,
        days_ago: float,
        quality: str = "high",
        revision: int = 31,
        status: str = JOB_READY,
        scene_id: str | None = None,
        mp4: int = 2_000,
        user: User | None = None,
        **columns,
    ) -> VideoRender:
        created = datetime.now(UTC) - timedelta(days=days_ago)
        stored: list[FileAttachment] = []
        files: dict = {}
        if status in (JOB_READY, JOB_FAILED) and mp4:
            stored.append(video := await self.file("render.mp4", mp4))
            files["file_id"] = video.id
            if scene_id:
                frames = [await self.file(f"frame-{i}.jpg", 20) for i in range(3)]
                stored.extend(frames)
                files["frame_file_ids"] = [str(frame.id) for frame in frames]
            else:
                stored.append(poster := await self.file("poster.jpg", 100))
                stored.append(captions := await self.file("captions.vtt", 10))
                files.update(poster_file_id=poster.id, captions_file_id=captions.id)
        row = VideoRender(
            project_id=project.id,
            user_id=(user or self.owner).id,
            revision=revision,
            quality=quality,
            status=status,
            scene_id=scene_id,
            width=3840 if quality == "ultra" else 1920,
            height=2160 if quality == "ultra" else 1080,
            fps=60,
            created_at=created,
            finished_at=created + timedelta(minutes=5) if status in JOB_TERMINAL_STATUSES else None,
            **files,
            **columns,
        )
        self.db.add(row)
        await self.db.flush()
        segment = f"video-studio/render-segments/{row.id}/d1/seg-0000000-0000060.mp4"
        self.objects[segment] = b"segment"
        self.bytes[row.id] = sum(f.size for f in stored)
        self.keys[row.id] = {f.s3_object_name for f in stored} | {segment}
        return row

    async def issue(self, project: VideoProject) -> None:
        """A 4K final, two 1080p renders (revisions 28 and 31), a failed and a cancelled attempt, twenty previews."""
        self.ultra = await self.render(project, days_ago=10, quality="ultra", revision=26, mp4=4_000)
        self.rev28 = await self.render(project, days_ago=5, revision=28)
        finding = await self.file("finding.jpg", 30)
        crop = await self.file("crop.png", 40)
        self.failed = await self.render(
            project,
            days_ago=4,
            revision=29,
            status=JOB_FAILED,
            mp4=1_500,
            error="Deadline exceeded",
            checks={
                "findings": [{"kind": "frozen", "severity": "fail", "at": 1.0, "data": {"frame_file_id": str(finding.id)}}],
                "inserts": [{"key": "screen", "crops": [{"file_id": str(crop.id)}]}],
            },
        )
        self.bytes[self.failed.id] += finding.size + crop.size
        self.keys[self.failed.id] |= {finding.s3_object_name, crop.s3_object_name}
        self.cancelled = await self.render(project, days_ago=3, revision=30, status=JOB_CANCELLED)
        self.rev31 = await self.render(project, days_ago=2, revision=31, mp4=2_100)
        self.previews = [
            await self.render(project, days_ago=1 + i * 0.05, quality="standard", scene_id=f"s{i:02d}", mp4=300)
            for i in range(20)
        ]
        project.latest_render_id = self.rev31.id

    def freed_by(self, *renders: VideoRender) -> int:
        return sum(self.bytes[render.id] for render in renders)


async def _seed(scratch, objects, *users: User) -> tuple[VideoProject, Film]:
    async with scratch.sessions() as db:
        db.add_all(users)
        await db.flush()
        project = VideoProject(user_id=users[0].id, title=_TITLE, spec=_SPEC)
        db.add(project)
        await db.flush()
        film = Film(db, objects, users[0])
        await film.issue(project)
        await db.commit()
    return project, film


async def _left(scratch, project: VideoProject) -> list[VideoRender]:
    async with scratch.sessions() as db:
        return list((await db.execute(select(VideoRender).where(VideoRender.project_id == project.id))).scalars().all())


async def test_the_default_cleanup_leaves_the_newest_1080p_and_frees_every_file_of_the_rest(store) -> None:
    async with _scratch("video_render_cleanup") as scratch:
        owner = _user("samuel@filigran.io")
        project, film = await _seed(scratch, store, owner)
        gone = [film.ultra, film.rev28, film.failed, film.cancelled, *film.previews]

        answer = await _tool(owner, "delete_video_renders", video_id=str(project.id), select="all_but_latest_ready", keep_quality="high")
        assert not answer.is_error, answer.content
        data = _answer(answer.content)
        assert data["freed"] == {"renders": 24, "bytes": film.freed_by(*gone)}
        assert [r["render_id"] for r in data["renders"] if r["action"] == "keep"] == [str(film.rev31.id)]
        assert data["serves"]["current"]["render_id"] == str(film.rev31.id)
        assert data["serves"]["latest_ready"]["high"] == {
            "render_id": str(film.rev31.id),
            "quality": "high",
            "revision": 31,
            "size": "1920x1080",
            "size_bytes": film.bytes[film.rev31.id],
        }

        assert [r.id for r in await _left(scratch, project)] == [film.rev31.id]
        # Every object of the deleted renders left the store - frames, crops and kept segments included.
        assert set(store) == film.keys[film.rev31.id]
        async with scratch.sessions() as db:
            assert len((await db.execute(select(FileAttachment))).scalars().all()) == 3
            assert (await db.get(VideoProject, project.id)).latest_render_id == film.rev31.id
            audit = (await db.execute(select(AuditLog).where(AuditLog.entity_id == str(project.id)))).scalars().one()
        assert (audit.action, audit.details["renders"], audit.details["via"]) == ("delete", 24, "agent")
        assert audit.details["bytes_freed"] == film.freed_by(*gone) and "confirmed" not in audit.details

        described = await _tool(owner, "get_video", video_id=str(project.id))
        size = film.bytes[film.rev31.id]
        assert f"- render `{film.rev31.id}`: ready high rev 31" in described.content
        assert f"(size_bytes {size}), kept_by latest_ready - current" in described.content
        assert f"Storage: 1 render holds 0 MiB (size_bytes {size})" in described.content


async def test_a_running_render_is_refused_by_id_and_the_rest_go(store) -> None:
    async with _scratch("video_render_cleanup_running") as scratch:
        owner = _user("samuel@filigran.io")
        project, film = await _seed(scratch, store, owner)
        async with scratch.sessions() as db:
            running = await Film(db, store, owner).render(project, days_ago=0.01, revision=32, status=JOB_RUNNING)
            await db.commit()

        answer = await _tool(owner, "delete_video_renders", video_id=str(project.id), keep_quality="high")
        assert not answer.is_error, answer.content
        data = _answer(answer.content)
        refused = [r for r in data["renders"] if r["action"] == "refuse"]
        assert refused == [
            {
                "render_id": str(running.id),
                "quality": "high",
                "revision": 32,
                "status": JOB_RUNNING,
                "size_bytes": 0,
                "action": "refuse",
                "reason": "in_progress",
                "why": "still queued or running: cancel it first",
            }
        ]
        assert f"cancel_video_job(render_ids=['{running.id}'])" in answer.content
        assert data["freed"]["renders"] == 24
        assert {r.id for r in await _left(scratch, project)} == {film.rev31.id, running.id}


async def test_the_render_the_public_link_plays_goes_with_confirm_and_the_link_then_plays_the_1080p(store) -> None:
    async with _scratch("video_render_cleanup_link") as scratch:
        owner = _user("samuel@filigran.io")
        project, film = await _seed(scratch, store, owner)
        async with scratch.sessions() as db:
            (await db.get(VideoProject, project.id)).latest_render_id = film.ultra.id
            await public_links.enable_public_link(db, project.id, actor_id=owner.id)
            await db.commit()

        first = await _tool(owner, "delete_video_renders", video_id=str(project.id), keep_quality="high")
        data = _answer(first.content)
        assert [(r["render_id"], r["reason"]) for r in data["renders"] if r["action"] == "refuse"] == [
            (str(film.ultra.id), "public_link")
        ]
        assert f"the link then plays render `{film.rev31.id}` (high rev 31)" in first.content
        assert {r.id for r in await _left(scratch, project)} == {film.ultra.id, film.rev31.id}

        confirmed = await _tool(owner, "delete_video_renders", video_id=str(project.id), render_ids=[str(film.ultra.id)], confirm=True)
        assert not confirmed.is_error, confirmed.content
        assert (
            f"The video plays render `{film.rev31.id}` (high rev 31) (it was moved from the render deleted) - and so does "
            "its public link."
        ) in confirmed.content
        assert _answer(confirmed.content)["serves"]["current_changed"] is True
        async with scratch.sessions() as db:
            stored = await db.get(VideoProject, project.id)
            assert stored.latest_render_id == film.rev31.id
            assert (await public_links.current_render(db, stored)).id == film.rev31.id
            audits = (
                (await db.execute(select(AuditLog).where(AuditLog.entity_id == str(project.id)).order_by(AuditLog.created_at)))
                .scalars()
                .all()
            )
        last = audits[-1].details
        assert (last["confirmed"], last["confirmed_for"], last["current_render_id"]) == (True, ["public_link"], str(film.rev31.id))
        assert "past their protection (public_link)" in audits[-1].summary


async def test_a_dry_run_deletes_nothing_and_answers_the_list_the_deletion_answers(store) -> None:
    async with _scratch("video_render_cleanup_dry") as scratch:
        owner = _user("samuel@filigran.io")
        project, _film = await _seed(scratch, store, owner)
        objects = dict(store)

        dry = await _tool(owner, "delete_video_renders", video_id=str(project.id), keep_quality="high", dry_run=True)
        assert not dry.is_error and dry.content.startswith("Dry run - nothing was deleted: 24 render(s)")
        assert len(await _left(scratch, project)) == 25 and store == objects
        real = await _tool(owner, "delete_video_renders", video_id=str(project.id), keep_quality="high")
        assert _answer(dry.content)["renders"] == _answer(real.content)["renders"]
        assert _answer(dry.content)["would_free"] == _answer(real.content)["freed"]
        async with scratch.sessions() as db:
            audits = (await db.execute(select(AuditLog).where(AuditLog.entity_id == str(project.id)))).scalars().all()
        assert len(audits) == 1, "a dry run writes no audit row"


async def test_the_nightly_sweep_deletes_exactly_the_renders_past_the_days_an_administrator_set(store) -> None:
    async with _scratch("video_render_retention_days") as scratch:
        owner, admin = _user("samuel@filigran.io"), _user("root@filigran.io", role="admin")
        async with scratch.sessions() as db:
            db.add_all([owner, admin])
            await db.flush()
            project = VideoProject(user_id=owner.id, title=_TITLE, spec=_SPEC)
            db.add(project)
            await db.flush()
            film = Film(db, store, owner)
            newest = await film.render(project, days_ago=2)
            kept = {
                "high_8": await film.render(project, days_ago=8),
                "preview_2": await film.render(project, days_ago=2, quality="standard", scene_id="s1"),
                "ultra_31": await film.render(project, days_ago=31, quality="ultra"),
                "kept_31": await film.render(project, days_ago=31, keep=True),
            }
            aged = {
                "high_31": await film.render(project, days_ago=31),
                "failed_2": await film.render(project, days_ago=2, status=JOB_FAILED),
                "cancelled_8": await film.render(project, days_ago=8, status=JOB_CANCELLED),
            }
            old_previews = {
                "preview_8": await film.render(project, days_ago=8, quality="standard", scene_id="s2"),
                "preview_31": await film.render(project, days_ago=31, quality="standard", scene_id="s3"),
            }
            project.latest_render_id = newest.id
            await db.commit()

        async with _client(scratch.sessions, owner) as client:
            seen = (await client.get("/api/v1/video-studio/render-retention")).json()
            assert seen == {"failed_cancelled_days": 1, "previews_days": 7, "superseded_ready_days": 30, "can_edit": False}
            assert (await client.put("/api/v1/video-studio/render-retention", json={"previews_days": 0})).status_code == 403
        async with _client(scratch.sessions, admin) as client:
            saved = await client.put("/api/v1/video-studio/render-retention", json={"previews_days": 0})
            assert saved.status_code == 200 and saved.json()["previews_days"] == 0
            assert (await client.put("/api/v1/video-studio/render-retention", json={"previews_days": -1})).status_code == 422

        async with scratch.sessions() as db:
            swept = await render_retention.sweep(db)
        assert swept.renders == 3 and swept.freed[0].bytes == film.freed_by(*aged.values())
        assert {r.id for r in await _left(scratch, project)} == {newest.id, *(r.id for r in kept.values()), *(r.id for r in old_previews.values())}

        async with _client(scratch.sessions, admin) as client:
            assert (await client.put("/api/v1/video-studio/render-retention", json={"previews_days": 7})).status_code == 200
        async with scratch.sessions() as db:
            assert (await render_retention.sweep(db)).renders == 2
        async with scratch.sessions() as db:
            assert (await render_retention.sweep(db)).renders == 0, "the next night finds nothing more"
        assert {r.id for r in await _left(scratch, project)} == {newest.id, *(r.id for r in kept.values())}
        for render in [*aged.values(), *old_previews.values()]:
            assert not film.keys[render.id] & set(store), "a swept render's files and segments leave the store"
        async with scratch.sessions() as db:
            changes = (await db.execute(select(AuditLog).where(AuditLog.entity_type == "platform_settings"))).scalars().all()
        assert sorted(c.details["render_retention"]["previews_days"]["to"] for c in changes) == [0, 7]


async def test_a_scenes_new_preview_replaces_its_previous_ones(store) -> None:
    async with _scratch("video_render_preview_replaced") as scratch:
        owner = _user("samuel@filigran.io")
        async with scratch.sessions() as db:
            db.add(owner)
            await db.flush()
            project = VideoProject(user_id=owner.id, title=_TITLE, spec=_SPEC)
            db.add(project)
            await db.flush()
            film = Film(db, store, owner)
            older = await film.render(project, days_ago=0.2, quality="standard", scene_id="s1")
            failed = await film.render(project, days_ago=0.1, quality="draft", scene_id="s1", status=JOB_FAILED)
            pinned = await film.render(project, days_ago=0.3, quality="standard", scene_id="s1", keep=True)
            other = await film.render(project, days_ago=0.2, quality="standard", scene_id="s2")
            newer = await film.render(project, days_ago=0.01, quality="standard", scene_id="s1")
            await db.commit()
        async with scratch.sessions() as db:
            replaced = await render_retention.replaced_previews(db, await db.get(VideoRender, newer.id))
            await db.commit()
        await render_retention.purge(replaced)
        assert set(replaced.render_ids) == {str(older.id), str(failed.id)}
        assert {r.id for r in await _left(scratch, project)} == {newer.id, pinned.id, other.id}
        assert not (film.keys[older.id] | film.keys[failed.id]) & set(store)


async def test_on_a_video_shared_to_watch_only_the_studio_cleanup_deletes_only_the_callers_render(store) -> None:
    async with _scratch("video_render_cleanup_viewer") as scratch:
        owner, viewer, outsider = _user("samuel@filigran.io"), _user("ada@filigran.io"), _user("alan@filigran.io")
        project, film = await _seed(scratch, store, owner, viewer, outsider)
        async with scratch.sessions() as db:
            stored = await db.get(VideoProject, project.id)
            stored.shared_user_ids = [str(viewer.id)]
            stored.shared_user_permissions = {str(viewer.id): "read_only"}
            mine = await Film(db, store, owner).render(project, days_ago=1, revision=31, status=JOB_FAILED, user=viewer)
            await db.commit()

        url = f"/api/v1/video-studio/projects/{project.id}/renders/cleanup"
        async with _client(scratch.sessions, viewer) as client:
            preview = (await client.post(url, json={"select": "failed_and_cancelled", "dry_run": True})).json()
            assert preview["dry_run"] is True and preview["would_free"]["renders"] == 1
            done = await client.post(url, json={"select": "failed_and_cancelled"})
            assert done.status_code == 200, done.text
            actions = {r["render_id"]: (r["action"], r.get("reason")) for r in done.json()["renders"]}
            assert actions == {
                str(mine.id): ("delete", None),
                str(film.failed.id): ("refuse", "not_yours"),
                str(film.cancelled.id): ("refuse", "not_yours"),
            }
            assert (await client.post(url, json={"select": "all"})).status_code == 422
            assert (await client.post(url, json={"select": "previews", "render_ids": [str(mine.id)]})).status_code == 422
        async with _client(scratch.sessions, outsider) as client:
            assert (await client.post(url, json={})).status_code == 404
        left = {r.id for r in await _left(scratch, project)}
        assert mine.id not in left and {film.failed.id, film.cancelled.id} <= left
