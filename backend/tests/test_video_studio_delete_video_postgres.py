"""``delete_video`` against PostgreSQL: the video goes for good, its media stays.

* the owner deletes a video: ``list_videos`` no longer lists it, ``get_video``
  answers "No video ... you can open", its renders and previews and every
  file they own are deleted, the recording it played stays in the library,
  and one audit entry says it was deleted through an agent;
* a video shared with someone, or with a public link, is refused without
  ``confirm`` (nothing deleted), and goes with it;
* a ``config`` grantee may edit the video but not delete it;
* the Studio's Delete runs the same service and writes the same audit entry.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours; the temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import video_studio
from app.core.config import settings
from app.core.database import get_db
from app.models.audit_log import AuditLog
from app.models.file_attachment import FileAttachment
from app.models.user import User
from app.models.video_studio import JOB_READY, VideoProject, VideoRender, VideoTake
from app.services.builtin_tools.video_studio import build_video_studio_tools
from app.services.video_studio import public_links
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("video_studio_enabled")]

_TITLE = "Retest film (test artifact, delete after)"


@pytest.fixture
def store(monkeypatch) -> dict[str, bytes]:
    from app.services import file_storage as storage

    objects: dict[str, bytes] = {}

    async def delete(name):
        objects.pop(name, None)

    async def list_object_names(prefix, recursive=True):
        return sorted(name for name in objects if name.startswith(prefix))

    monkeypatch.setattr(storage.file_storage, "delete", delete)
    monkeypatch.setattr(storage.file_storage, "list_object_names", list_object_names)
    monkeypatch.setattr(settings, "video_public_links_enabled", True)
    return objects


async def _tool(user: User, name: str, **arguments):
    _defs, executor = build_video_studio_tools(user_id=str(user.id))
    return await executor.execute_tool(ToolCall(id=name, name=name, arguments=arguments))


@asynccontextmanager
async def _client(sessions, user: User) -> AsyncIterator[httpx.AsyncClient]:
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


def _user(email: str) -> User:
    return User(email=email, hashed_password="x", full_name=email.split("@")[0].title(), role="user")


def _scratch(prefix: str):
    return scratch_database(prefix, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True)


async def _file(db, objects: dict, owner: User, name: str) -> FileAttachment:
    key = f"renders/{uuid.uuid4().hex}/{name}"
    objects[key] = b"x" * 10
    row = FileAttachment(user_id=owner.id, filename=name, content_type="application/octet-stream", size=10, s3_object_name=key)
    db.add(row)
    await db.flush()
    return row


async def _seed(scratch, objects: dict, owner: User, *others: User) -> tuple[uuid.UUID, uuid.UUID, set[str]]:
    """A video with a render and a scene preview, playing one recording: (video id, recording id, render keys)."""
    async with scratch.sessions() as db:
        db.add_all([owner, *others])
        await db.flush()
        take = VideoTake(user_id=owner.id, name="Library walkthrough", status=JOB_READY)
        db.add(take)
        await db.flush()
        spec = {"title": _TITLE, "freestyle": True, "scenes": [{"type": "recording", "take_id": str(take.id)}]}
        project = VideoProject(user_id=owner.id, title=_TITLE, spec=spec)
        db.add(project)
        await db.flush()
        keys: set[str] = set()
        for scene in (None, "s1"):
            mp4 = await _file(db, objects, owner, "render.mp4")
            keys.add(mp4.s3_object_name)
            db.add(
                VideoRender(
                    project_id=project.id,
                    user_id=owner.id,
                    revision=1,
                    quality="high",
                    status=JOB_READY,
                    scene_id=scene,
                    file_id=mp4.id,
                )
            )
        await db.commit()
        return project.id, take.id, keys


async def test_the_owner_deletes_the_video_its_renders_go_and_its_recording_stays(store) -> None:
    async with _scratch("video_delete_owner") as scratch:
        owner = _user("samuel@filigran.io")
        video_id, take_id, keys = await _seed(scratch, store, owner)

        answer = await _tool(owner, "delete_video", video_id=str(video_id))
        assert not answer.is_error, answer.content
        assert "for good" in answer.content and "1 render(s) and 1 scene preview(s)" in answer.content
        assert "no longer available" in answer.content

        listed = await _tool(owner, "list_videos")
        assert str(video_id) not in listed.content
        opened = await _tool(owner, "get_video", video_id=str(video_id))
        assert opened.is_error and f"No video `{video_id}` you can open" in opened.content
        assert not keys & set(store), "every file of the renders left the store"
        async with scratch.sessions() as db:
            assert (await db.execute(select(VideoRender))).scalars().all() == []
            assert await db.get(VideoTake, take_id) is not None, "the recording stays in the library"
            audits = (await db.execute(select(AuditLog).where(AuditLog.entity_id == str(video_id)))).scalars().all()
        assert [(a.action, a.entity_type) for a in audits] == [("delete", "video_project")]
        assert audits[0].summary == f"Deleted video {_TITLE} through an agent"
        assert audits[0].details["via_agent"] is True and audits[0].details["renders"] == 1


async def test_a_shared_video_is_refused_without_confirm_and_goes_with_it(store) -> None:
    async with _scratch("video_delete_shared") as scratch:
        owner, viewer = _user("samuel@filigran.io"), _user("ada@filigran.io")
        video_id, _take, keys = await _seed(scratch, store, owner, viewer)
        async with scratch.sessions() as db:
            project = await db.get(VideoProject, video_id)
            project.shared_user_ids = [str(viewer.id)]
            project.shared_user_permissions = {str(viewer.id): "read_only"}
            await public_links.enable_public_link(db, video_id, actor_id=owner.id)
            await db.commit()

        refused = await _tool(owner, "delete_video", video_id=str(video_id))
        assert refused.is_error and "Nothing was deleted" in refused.content
        assert "1 person(s) it is shared with" in refused.content and "anyone with its public link" in refused.content
        assert keys <= set(store)
        async with scratch.sessions() as db:
            assert await db.get(VideoProject, video_id) is not None

        # Someone the video is shared with cannot delete it, even with confirm.
        theirs = await _tool(viewer, "delete_video", video_id=str(video_id), confirm=True)
        assert theirs.is_error and "not yours to delete" in theirs.content

        confirmed = await _tool(owner, "delete_video", video_id=str(video_id), confirm=True)
        assert not confirmed.is_error, confirmed.content
        async with scratch.sessions() as db:
            assert await db.get(VideoProject, video_id) is None
            assert await public_links.load_public_link(db, video_id) is None


async def test_a_config_grantee_edits_but_does_not_delete(store) -> None:
    async with _scratch("video_delete_grantee") as scratch:
        owner, editor = _user("samuel@filigran.io"), _user("grace@filigran.io")
        video_id, _take, _keys = await _seed(scratch, store, owner, editor)
        async with scratch.sessions() as db:
            project = await db.get(VideoProject, video_id)
            project.shared_user_ids = [str(editor.id)]
            project.shared_user_permissions = {str(editor.id): "config"}
            await db.commit()

        refused = await _tool(editor, "delete_video", video_id=str(video_id), confirm=True)
        assert refused.is_error and "not yours to delete" in refused.content
        async with scratch.sessions() as db:
            assert await db.get(VideoProject, video_id) is not None


async def test_the_studio_delete_runs_the_same_service(store) -> None:
    async with _scratch("video_delete_studio") as scratch:
        owner = _user("samuel@filigran.io")
        video_id, take_id, keys = await _seed(scratch, store, owner)
        async with _client(scratch.sessions, owner) as client:
            assert (await client.delete(f"/api/v1/video-studio/projects/{video_id}")).status_code == 204
        assert not keys & set(store)
        async with scratch.sessions() as db:
            assert await db.get(VideoProject, video_id) is None
            assert await db.get(VideoTake, take_id) is not None
            audits = (await db.execute(select(AuditLog).where(AuditLog.entity_id == str(video_id)))).scalars().all()
        assert [a.summary for a in audits] == [f"Deleted video {_TITLE}"]
        assert "via_agent" not in audits[0].details
