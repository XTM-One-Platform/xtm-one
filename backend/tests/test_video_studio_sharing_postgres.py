"""Sharing a video, against PostgreSQL, the REST routes and an anonymous client.

The pure pieces are ``test_video_studio_sharing.py``; this one proves what the
grants and the public link MEAN, every actor an ordinary user (rule 19: an
admin would short-circuit the visibility filter):

* the owner shares the video with a person (can view) and a group (can edit):
  the viewer watches and downloads it but cannot share or render it, the
  group's member edits its sharing, an outsider does not find it;
* an anonymous client watches the current render through the public link -
  never the spec, the owner or the grants - downloads it only once the link
  allows it, and loses it the moment the link is replaced, turned off, or the
  admin switch goes off; the assistant shares and turns the link off with the
  dialog's rules;
* the render list says what keeps each render, the nightly sweep deletes only
  a render past its rule, and a deleted current render hands over to the next.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours; the temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

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
from app.api.v1 import video_public, video_studio
from app.core.config import settings
from app.core.database import get_db
from app.models.audit_log import AuditLog
from app.models.file_attachment import FileAttachment
from app.models.group import Group, GroupMember
from app.models.user import User
from app.models.video_studio import JOB_FAILED, JOB_READY, VideoProject, VideoPublicLink, VideoRender
from app.services.builtin_tools.video_studio import build_video_studio_tools
from app.services.video_studio import assets, render_retention
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("video_studio_enabled")]

_TITLE = "Launch film"
_MP4 = b"\x00\x00\x00\x18ftypmp42-launch-film-frames"
_VTT = b"WEBVTT\n\n00:00.000 --> 00:02.000\nHello\n"


@asynccontextmanager
async def _client(sessions: async_sessionmaker, user: User | None) -> AsyncIterator[httpx.AsyncClient]:
    """Both routers on their own session per request; *user* signed in, or nobody."""
    api = FastAPI()
    api.include_router(video_studio.router, prefix="/api/v1/video-studio")
    api.include_router(video_public.router, prefix="/api/v1/public")

    async def _get_db():
        async with sessions() as db:
            yield db
            await db.commit()

    if user is not None:
        api.dependency_overrides[deps.get_current_user] = lambda: user
    api.dependency_overrides[get_db] = _get_db
    api.dependency_overrides[deps.require_ai_enabled] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        yield client


async def _tool(user: User, name: str, **arguments):
    _defs, executor = build_video_studio_tools(user_id=str(user.id))
    return await executor.execute_tool(ToolCall(id=name, name=name, arguments=arguments))


@pytest.fixture
def stubs(monkeypatch):
    """No Redis budgets, an object store holding the render's files, and file deletions recorded."""
    from app.services import file_storage as storage

    objects = {"launch.mp4": _MP4, "launch.vtt": _VTT, "launch.jpg": b"\xff\xd8poster"}
    deleted: list = []

    async def _no_budget(*_a, **_kw):
        return None

    async def _read_range(object_name, offset, length):
        return objects[object_name.rsplit("/", 1)[-1]][offset : offset + length]

    async def _delete_files(file_ids):
        deleted.extend(f for f in file_ids if f)

    async def _delete_object(name):
        deleted.append(name)

    async def _no_objects(*_args, **_kwargs):
        return []

    monkeypatch.setattr(video_public, "enforce_rate_limit", _no_budget)
    monkeypatch.setattr(storage.file_storage, "read_range", _read_range)
    monkeypatch.setattr(storage.file_storage, "delete", _delete_object)
    monkeypatch.setattr(storage.file_storage, "list_object_names", _no_objects)
    monkeypatch.setattr(assets, "delete_files", _delete_files)
    monkeypatch.setattr(settings, "video_public_links_enabled", True)
    return deleted


def _user(email: str, name: str) -> User:
    return User(email=email, hashed_password="x", full_name=name, role="user")


async def _ready_render(db, project: VideoProject, owner: User, *, minutes_ago: int, **columns) -> VideoRender:
    files = []
    folder = uuid.uuid4().hex
    for name, kind, size in (
        ("launch.mp4", "video/mp4", len(_MP4)),
        ("launch.jpg", "image/jpeg", 8),
        ("launch.vtt", "text/vtt", len(_VTT)),
    ):
        row = FileAttachment(
            user_id=owner.id, filename=name, content_type=kind, size=size, s3_object_name=f"renders/{folder}/{name}"
        )
        db.add(row)
        files.append(row)
    await db.flush()
    created = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    render = VideoRender(
        project_id=project.id,
        user_id=owner.id,
        revision=1,
        quality="high",
        status=JOB_READY,
        width=1920,
        height=1080,
        fps=60,
        duration_ms=39_800,
        size_bytes=len(_MP4),
        file_id=files[0].id,
        poster_file_id=files[1].id,
        captions_file_id=files[2].id,
        captions_burned_in=False,
        created_at=created,
        finished_at=created + timedelta(minutes=2),
        **columns,
    )
    db.add(render)
    await db.flush()
    return render


async def test_a_shared_video_opens_for_its_grants_and_its_public_link_for_anyone(stubs, monkeypatch) -> None:
    async with scratch_database(
        "video_sharing", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner = _user("samuel@filigran.io", "Samuel Hassine")
            viewer = _user("ada@filigran.io", "Ada Lovelace")
            editor = _user("grace@filigran.io", "Grace Hopper")
            outsider = _user("alan@filigran.io", "Alan Turing")
            db.add_all([owner, viewer, editor, outsider])
            await db.flush()
            marketing = Group(name="Marketing")
            db.add(marketing)
            await db.flush()
            db.add(GroupMember(group_id=marketing.id, user_id=editor.id))
            await db.commit()

        # The owner makes the video by hand, as the editor does.
        async with _client(scratch.sessions, owner) as client:
            created = await client.post(
                "/api/v1/video-studio/projects",
                json={"spec": {"title": _TITLE, "freestyle": True, "scenes": [{"type": "title", "title": "Launch"}]}},
            )
            assert created.status_code == 201, created.text
            video = created.json()
            assert (video["permission"], video["owner_name"], video["has_public_link"]) == (
                "owner",
                "Samuel Hassine",
                False,
            )
        video_id = video["id"]
        project_url = f"/api/v1/video-studio/projects/{video_id}"
        async with scratch.sessions() as db:
            project = await db.get(VideoProject, uuid.UUID(video_id))
            render = await _ready_render(db, project, owner, minutes_ago=5)
            project.latest_render_id = render.id
            await db.commit()
            render_id = render.id

        # An outsider does not find it; nobody but its managers can share it yet.
        async with _client(scratch.sessions, outsider) as client:
            assert (await client.get(project_url)).status_code == 404

        # The owner shares it: Ada can view, the Marketing group can edit.
        async with _client(scratch.sessions, owner) as client:
            shared = await client.patch(
                project_url,
                json={
                    "is_company_managed": False,
                    "shared_user_ids": [str(viewer.id)],
                    "shared_user_permissions": {str(viewer.id): "read_only"},
                    "shared_group_ids": [str(marketing.id)],
                    "shared_group_permissions": {str(marketing.id): "config"},
                    "access_restricted": False,
                },
            )
            assert shared.status_code == 200, shared.text
            body = shared.json()
            assert body["shared_user_permissions"] == {str(viewer.id): "read_only"}
            assert body["shared_group_permissions"] == {str(marketing.id): "config"}
            assert body["revision"] == video["revision"], "a sharing change never bumps the revision"
            # Publishing to the company is an admin's act.
            published = await client.patch(project_url, json={"is_company_managed": True})
            assert published.status_code == 403

        # Ada watches and downloads it, and can neither share nor render it.
        async with _client(scratch.sessions, viewer) as client:
            seen = (await client.get(project_url)).json()
            assert (seen["permission"], seen["can_manage"], seen["owner_name"]) == ("read_only", False, "Samuel Hassine")
            assert (await client.patch(project_url, json={"shared_user_ids": []})).status_code == 403
            assert (await client.post(f"{project_url}/renders", json={"quality": "draft"})).status_code == 403
            assert (await client.get(f"{project_url}/public-link")).status_code == 403
            assert (await client.patch(f"/api/v1/video-studio/renders/{render_id}", json={"keep": True})).status_code == 403
            token = await client.post(
                "/api/v1/video-studio/media-token",
                json={"kind": "render", "id": str(render_id), "asset": "video", "download": True},
            )
            assert token.status_code == 200, token.text
            saved = await client.get(token.json()["url"].replace("http://test", ""))
            assert saved.status_code == 200
            assert saved.content == _MP4
            assert "attachment" in saved.headers["content-disposition"]
            assert "Launch film - 1080p60.mp4" in saved.headers["content-disposition"]
            # A playing token keeps the storage name; a range is a 206.
            playing = await client.post(
                "/api/v1/video-studio/media-token", json={"kind": "render", "id": str(render_id), "asset": "video"}
            )
            ranged = await client.get(playing.json()["url"].replace("http://test", ""), headers={"Range": "bytes=0-3"})
            assert (ranged.status_code, ranged.content) == (206, _MP4[:4])
            assert "Launch film - 1080p60" not in ranged.headers.get("content-disposition", "")

        # Grace edits its sharing through the Marketing group.
        async with _client(scratch.sessions, editor) as client:
            seen = (await client.get(project_url)).json()
            assert (seen["permission"], seen["can_manage"]) == ("config", True)
            widened = await client.patch(
                project_url,
                json={
                    "shared_user_ids": [str(viewer.id), str(outsider.id)],
                    "shared_user_permissions": {str(viewer.id): "read_only", str(outsider.id): "read_only"},
                },
            )
            assert widened.status_code == 200, widened.text
        async with _client(scratch.sessions, outsider) as client:
            assert (await client.get(project_url)).json()["permission"] == "read_only"

        # The public link: off, then on from the Share dialog.
        link_url = f"{project_url}/public-link"
        async with _client(scratch.sessions, owner) as client:
            assert (await client.get(link_url)).json() == {
                "available": True,
                "enabled": False,
                "path": None,
                "url": None,
                "created_at": None,
                "created_by_name": None,
                "allow_download": False,
            }
            link = (await client.post(link_url, json={})).json()
            assert (link["enabled"], link["created_by_name"]) == (True, "Samuel Hassine")
            assert link["path"].startswith("/public/videos/")
            assert (await client.post(link_url, json={})).json()["path"] == link["path"]
            assert (await client.get(project_url)).json()["has_public_link"] is True
        token = link["path"].rsplit("/", 1)[1]
        async with scratch.sessions() as db:
            stored = (await db.execute(select(VideoPublicLink))).scalars().all()
            assert len(stored) == 1 and token not in (stored[0].token_hash, stored[0].token_enc)

        public = f"/api/v1/public/videos/{token}"
        async with _client(scratch.sessions, None) as anonymous:
            opened = await anonymous.get(public)
            assert opened.status_code == 200, opened.text
            assert (opened.headers["cache-control"], opened.headers["referrer-policy"]) == ("no-store", "no-referrer")
            body = opened.json()
            assert (body["title"], body["rendered"], body["download"], body["size_bytes"]) == (_TITLE, True, False, None)
            for private in ("user_id", "spec", "owner_name", "shared_user_ids", "permission", "can_manage", "id"):
                assert private not in body
            assert "Samuel Hassine" not in opened.text and "samuel@filigran.io" not in opened.text
            played = await anonymous.get(f"{public}/media/video", headers={"Range": "bytes=4-7"})
            assert (played.status_code, played.content) == (206, _MP4[4:8])
            assert played.headers["content-disposition"] == "inline"
            assert (await anonymous.get(f"{public}/media/captions")).content == _VTT
            # No download until the link offers it; never a file it does not serve.
            for path in (
                f"{public}/media/video?download=true",
                f"{public}/media/contact_sheet",
                f"/api/v1/public/videos/{'A' * 43}",
                f"/api/v1/public/videos/{'A' * 43}/media/video",
            ):
                assert (await anonymous.get(path)).status_code == 404, path
            assert (await anonymous.get(project_url)).status_code == 401

        async with _client(scratch.sessions, owner) as client:
            offered = await client.patch(link_url, json={"allow_download": True})
            assert offered.json()["allow_download"] is True
        async with _client(scratch.sessions, None) as anonymous:
            assert (await anonymous.get(public)).json()["download"] is True
            saved = await anonymous.get(f"{public}/media/video?download=true")
            assert saved.status_code == 200 and saved.content == _MP4
            assert "Launch film - 1080p60.mp4" in saved.headers["content-disposition"]
            assert (await anonymous.get(f"{public}/media/poster?download=true")).status_code == 404

        # Replacing the link kills the old URL at once; the download option survives.
        async with _client(scratch.sessions, owner) as client:
            rotated = (await client.post(link_url, json={"regenerate": True})).json()
        new_token = rotated["path"].rsplit("/", 1)[1]
        assert new_token != token and rotated["allow_download"] is True
        async with _client(scratch.sessions, None) as anonymous:
            assert (await anonymous.get(public)).status_code == 404
            assert (await anonymous.get(f"/api/v1/public/videos/{new_token}")).status_code == 200

        # The admin switch off: every link is a 404 and none can be created.
        monkeypatch.setattr(settings, "video_public_links_enabled", False)
        async with _client(scratch.sessions, None) as anonymous:
            assert (await anonymous.get(f"/api/v1/public/videos/{new_token}")).status_code == 404
        async with _client(scratch.sessions, owner) as client:
            assert (await client.get(link_url)).json()["available"] is False
            assert (await client.get(project_url)).json()["has_public_link"] is False
            assert (await client.post(link_url, json={"regenerate": True})).status_code == 403
        monkeypatch.setattr(settings, "video_public_links_enabled", True)

        # The assistant: a viewer's is refused, the owner's shares and turns the link off.
        refused = await _tool(viewer, "set_video_public_link", video_id=video_id, action="disable")
        assert refused.is_error and "but not share it" in refused.content
        shared = await _tool(owner, "share_video", users=["alan@filigran.io"], permission="config", video_id=video_id)
        assert not shared.is_error, shared.content
        assert "Alan Turing" in shared.content and "can edit" in shared.content
        unknown = await _tool(owner, "share_video", users=["nobody@example.com"], video_id=video_id)
        assert unknown.is_error and "Nothing was changed" in unknown.content
        disabled = await _tool(owner, "set_video_public_link", video_id=video_id, action="disable")
        assert not disabled.is_error, disabled.content
        async with _client(scratch.sessions, None) as anonymous:
            assert (await anonymous.get(f"/api/v1/public/videos/{new_token}")).status_code == 404
        described = await _tool(owner, "get_video", video_id=video_id)
        assert "Shared with" in described.content and "Ada Lovelace" in described.content

        async with scratch.sessions() as db:
            rows = (await db.execute(select(AuditLog).where(AuditLog.entity_id == video_id))).scalars().all()
            links = sorted((r.details or {}).get("public_link", "") for r in rows if (r.details or {}).get("public_link"))
            assert links == ["disabled", "download_enabled", "enabled", "regenerated"]
            assert sum(1 for r in rows if (r.details or {}).get("sharing_changed")) >= 6

        # Deleting the video takes its link with it.
        async with _client(scratch.sessions, owner) as client:
            assert (await client.delete(project_url)).status_code == 204
        async with scratch.sessions() as db:
            assert (await db.execute(select(VideoPublicLink))).scalars().all() == []


async def test_a_public_link_charges_every_answer_its_bytes_and_the_share_dialog_is_audited(stubs, monkeypatch) -> None:
    """A byte budget per address, suffix and multi-range answers, and the grant delta of a REST share."""
    from fastapi import HTTPException

    charged: list[tuple[str, int]] = []

    async def budget(_request, name, *, limit, window_seconds, cost=1, **_kwargs):
        charged.append((name, cost))
        if sum(c for n, c in charged if n == name) > limit:
            raise HTTPException(status_code=429, detail="Too many requests.")

    monkeypatch.setattr(video_public, "enforce_rate_limit", budget)
    monkeypatch.setattr(video_public, "PUBLIC_MEDIA_BYTES_PER_WINDOW", len(_MP4) + 8)
    async with scratch_database(
        "video_public_bytes", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner, viewer = _user("samuel@filigran.io", "Samuel Hassine"), _user("ada@filigran.io", "Ada Lovelace")
            db.add_all([owner, viewer])
            await db.flush()
            spec = {"title": _TITLE, "freestyle": True, "scenes": [{"type": "title", "title": "Launch"}]}
            project = VideoProject(user_id=owner.id, title=_TITLE, spec=spec)
            db.add(project)
            await db.flush()
            project.latest_render_id = (await _ready_render(db, project, owner, minutes_ago=5)).id
            await db.commit()
        project_url = f"/api/v1/video-studio/projects/{project.id}"
        async with _client(scratch.sessions, owner) as client:
            token = (await client.post(f"{project_url}/public-link", json={})).json()["path"].rsplit("/", 1)[1]
            shared = await client.patch(
                project_url,
                json={"shared_user_ids": [str(viewer.id)], "shared_user_permissions": {str(viewer.id): "read_only"}},
            )
            assert shared.status_code == 200, shared.text
            retiered = await client.patch(
                project_url,
                json={"shared_user_ids": [str(viewer.id)], "shared_user_permissions": {str(viewer.id): "config"}},
            )
            assert retiered.status_code == 200
            assert (await client.patch(project_url, json={"shared_user_ids": []})).status_code == 200
        media = f"/api/v1/public/videos/{token}/media/video"
        async with _client(scratch.sessions, None) as anonymous:
            # The last bytes asked from the end (a suffix range): a 206 of them, charged what it carries.
            tail = await anonymous.get(media, headers={"Range": "bytes=-4"})
            assert (tail.status_code, tail.content) == (206, _MP4[-4:])
            assert tail.headers["content-range"] == f"bytes {len(_MP4) - 4}-{len(_MP4) - 1}/{len(_MP4)}"
            # Several ranges at once are not served piecewise: the whole file, charged whole.
            whole = await anonymous.get(media, headers={"Range": "bytes=0-1,4-5"})
            assert (whole.status_code, whole.content) == (200, _MP4)
            # The budget is spent: the next answer is refused, not served in part.
            assert (await anonymous.get(media, headers={"Range": "bytes=0-7"})).status_code == 429
        assert [cost for name, cost in charged if name == "video_public_media_bytes"] == [4, len(_MP4), 8]
        async with scratch.sessions() as db:
            rows = (
                await db.execute(
                    select(AuditLog).where(AuditLog.entity_id == project.id).order_by(AuditLog.created_at)
                )
            ).scalars().all()
        deltas = [
            (row.details["added_users"], row.details["retiered_users"], row.details["removed_users"])
            for row in rows
            if row.summary == f"Changed who can open video {_TITLE}"
        ]
        assert deltas == [([str(viewer.id)], [], []), ([], [str(viewer.id)], []), ([], [], [str(viewer.id)])]


async def test_the_render_retention_keeps_the_newest_the_current_and_the_kept_renders(stubs) -> None:
    async with scratch_database(
        "video_render_retention", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner, editor = _user("samuel@filigran.io", "Samuel Hassine"), _user("grace@filigran.io", "Grace Hopper")
            db.add_all([owner, editor])
            await db.flush()
            project = VideoProject(
                user_id=owner.id,
                title=_TITLE,
                spec={"title": _TITLE, "freestyle": True, "scenes": [{"type": "title", "title": "Launch"}]},
                shared_user_ids=[str(editor.id)],
                shared_user_permissions={str(editor.id): "config"},
            )
            db.add(project)
            await db.flush()
            # Five recent ready renders, one superseded 45 days ago, one kept by hand, three failed minutes ago.
            ready = [await _ready_render(db, project, owner, minutes_ago=m) for m in (10, 20, 30, 40, 50)]
            aged = await _ready_render(db, project, owner, minutes_ago=60 * 24 * 45)
            kept = await _ready_render(db, project, owner, minutes_ago=60 * 24 * 60, keep=True)
            for minutes in (1, 2, 3):
                db.add(
                    VideoRender(
                        project_id=project.id,
                        user_id=owner.id,
                        revision=1,
                        quality="draft",
                        status=JOB_FAILED,
                        error="no voice",
                        created_at=datetime.now(UTC) - timedelta(minutes=minutes),
                    )
                )
            # The current render is an older one a manager picked.
            project.latest_render_id = ready[3].id
            await db.commit()
            project_id = project.id
            ids = {name: r.id for name, r in zip(("r1", "r2", "r3", "r4", "r5"), ready, strict=True)}
            ids["aged"], ids["kept"] = aged.id, kept.id
            aged_mp4 = aged.file_id

        # The render list says which is current, what keeps each render and until when.
        async with _client(scratch.sessions, owner) as client:
            listed = (await client.get(f"/api/v1/video-studio/projects/{project_id}/renders")).json()["items"]
            by_id = {item["id"]: item for item in listed}
            current = by_id[str(ids["r4"])]
            assert (current["is_current"], current["kept_by"], current["expires_at"]) == (True, "author", None)
            assert (by_id[str(ids["kept"])]["kept_by"], by_id[str(ids["kept"])]["expires_at"]) == ("author", None)
            assert (by_id[str(ids["r1"])]["kept_by"], by_id[str(ids["r1"])]["expires_at"]) == ("latest_ready", None)
            assert by_id[str(ids["r2"])]["kept_by"] == "retention" and by_id[str(ids["r2"])]["expires_at"] is not None
            assert by_id[str(ids["aged"])]["kept_by"] is None, "past its 30 days: the next sweep deletes it"
            assert (await client.get(f"/api/v1/video-studio/projects/{project_id}")).json()["render_retention"] == {
                "failed_cancelled_days": 1,
                "previews_days": 7,
                "superseded_ready_days": 30,
            }

        async with scratch.sessions() as db:
            assert (await render_retention.sweep(db)).renders == 1
        async with scratch.sessions() as db:
            left = (await db.execute(select(VideoRender).where(VideoRender.project_id == project_id))).scalars().all()
            survivors = {r.id for r in left if r.status == JOB_READY}
            # Only the superseded render past its 30 days went; the failed attempts are minutes old.
            assert survivors == {ids["r1"], ids["r2"], ids["r3"], ids["r4"], ids["r5"], ids["kept"]}
            assert sum(1 for r in left if r.status == JOB_FAILED) == 3
            assert (await db.execute(select(FileAttachment).where(FileAttachment.id == aged_mp4))).first() is None
        assert len(stubs) == 3, "the deleted render takes its MP4, poster and captions from the store"

        # A manager makes the newest one current; deleting it hands over to the next.
        async with _client(scratch.sessions, editor) as client:
            made = await client.patch(f"/api/v1/video-studio/renders/{ids['r1']}", json={"current": True})
            assert made.status_code == 200 and made.json()["is_current"] is True
            assert (await client.delete(f"/api/v1/video-studio/renders/{ids['r1']}")).status_code == 204
        async with scratch.sessions() as db:
            assert (await db.get(VideoProject, project_id)).latest_render_id == ids["r2"]
