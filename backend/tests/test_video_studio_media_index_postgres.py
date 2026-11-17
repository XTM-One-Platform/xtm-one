"""No purge of a medium a video needs, against PostgreSQL and the REST routes.

The "in use" check before deleting a recording, a shot or a file read only
the media a video's scenes play. It reads the one media reference index now
(``video_projects.asset_refs``, ``retention.media_index``):

* a recording whose only use is a screen insert's source is refused to a
  single deletion, kept by a bulk one and never expired by the retention -
  in a video indexed before the index read insert sources too, once the
  tick has indexed it again;
* what an AI shot a video plays is made from - the recording a frame was cut
  from, the frame kept - is in use, and a shot generated again from another
  start makes its videos need the new one at once;
* what a video's ready render drew stays in use after the spec moved on,
  while the render is ready;
* a file a video needs - a shot's poster a screen insert shows - stays when
  the shot it came with is deleted.

Every actor is an ordinary user (rule 19). ``integration``-marked and skips
when PostgreSQL is unreachable, like its neighbours.
"""

from __future__ import annotations

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
from app.models.file_attachment import FileAttachment
from app.models.user import User
from app.models.video_studio import JOB_READY, VideoClip, VideoProject, VideoRender, VideoTake
from app.services import file_cleanup
from app.services.video_studio import assets, library, retention
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("video_studio_enabled")]

_BASE = "/api/v1/video-studio"
_QUAD = [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]
_LONG_AGO = datetime(2026, 1, 1, tzinfo=UTC)


def _scratch(name: str):
    return scratch_database(name, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True)


@asynccontextmanager
async def _client(sessions: async_sessionmaker, user: User) -> AsyncIterator[httpx.AsyncClient]:
    api = FastAPI()
    api.include_router(video_studio.router, prefix=_BASE)

    async def _get_db():
        async with sessions() as db:
            yield db
            await db.commit()

    api.dependency_overrides[deps.get_current_user] = lambda: user
    api.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        yield client


def _user(email: str) -> User:
    return User(email=email, hashed_password="x", full_name=email.split("@")[0], role="user")


def _take(owner: User, name: str) -> VideoTake:
    return VideoTake(
        user_id=owner.id, name=name, status=JOB_READY, script={}, created_at=_LONG_AGO, last_used_at=_LONG_AGO
    )


def _shot(owner: User, prompt: str, **columns) -> VideoClip:
    base = {"user_id": owner.id, "provider": "openai", "model": "sora-2", "prompt": prompt, "params": {}}
    return VideoClip(**{**base, "status": JOB_READY, "created_at": _LONG_AGO, "last_used_at": _LONG_AGO, **columns})


def _inserted(footage: VideoTake, take: VideoTake) -> dict:
    """A recording scene of *footage* whose screen insert shows *take*: the video's only use of *take*."""
    return {
        "id": "office",
        "type": "recording",
        "take_id": str(footage.id),
        "overlays": [{"type": "screen_insert", "source": {"take_id": str(take.id)}, "quad": _QUAD}],
    }


async def test_a_recording_only_a_screen_insert_shows_is_never_purged(monkeypatch) -> None:
    """The video's only use of the recording is a screen insert's source."""

    async def purge(keys):
        return len(list(keys))

    monkeypatch.setattr(file_cleanup, "purge_storage_objects", purge)
    monkeypatch.setattr(settings, "video_studio_media_retention_days", 1)
    monkeypatch.setattr(settings, "video_studio_unused_media_retention_days", 1)
    async with _scratch("video_media_index_insert") as scratch:
        async with scratch.sessions() as db:
            owner = _user("samuel@filigran.io")
            db.add(owner)
            await db.flush()
            footage, shown, unused = _take(owner, "Office"), _take(owner, "Dashboard tour"), _take(owner, "Old tour")
            db.add_all([footage, shown, unused])
            await db.flush()
            spec = {"title": "Launch", "scenes": [_inserted(footage, shown)]}
            db.add(VideoProject(user_id=owner.id, title="Launch", spec=spec))
            await db.commit()
        async with scratch.sessions() as db:
            refusal = await library.deletion_refusal(db, owner, "take", shown.id)
            assert refusal is not None and refusal["code"] == "media_in_use"
            assert [video["title"] for video in refusal["videos"]] == ["Launch"]
        async with _client(scratch.sessions, owner) as client:
            refused = await client.delete(f"{_BASE}/takes/{shown.id}")
            assert refused.status_code == 409 and refused.json()["detail"]["code"] == "media_in_use"
            named = {"items": [{"kind": "take", "id": str(shown.id)}]}
            answer = await client.post(f"{_BASE}/assets/bulk-delete", json=named)
            assert answer.json() == {"deleted": 0, "kept_in_use": 1}
            everything = {"all_matching_filters": True, "usage": "unused"}
            answer = await client.post(f"{_BASE}/assets/bulk-delete", json=everything)
            assert answer.json() == {"deleted": 1, "kept_in_use": 0}
        async with scratch.sessions() as db:
            counts = await retention.sweep(db, now=_LONG_AGO + timedelta(days=30))
        # The footage and the recording its screen insert shows: both in use, stamped, never expired.
        assert counts["expired"] == 0 and counts["in_use"] == 2
        async with scratch.sessions() as db:
            kept = await db.get(VideoTake, shown.id)
            assert kept is not None and kept.expired_at is None
            assert await db.get(VideoTake, unused.id) is None


async def test_a_video_an_older_rule_indexed_is_indexed_again_before_its_insert_source_goes() -> None:
    """The regression on stored data: a video indexed before the index read a screen insert's source.

    Its row kept the index of the scenes' own footage only, which every deletion
    read: nothing indexed it again while its spec stayed the same, so the
    recording its screen insert shows read as unused. The tick indexes it again
    (``index_stale_refs``) before any deletion reads it.
    """
    from sqlalchemy import update

    async with _scratch("video_media_index_stale") as scratch:
        async with scratch.sessions() as db:
            owner = _user("samuel@filigran.io")
            db.add(owner)
            await db.flush()
            footage, shown = _take(owner, "Office"), _take(owner, "Dashboard tour")
            db.add_all([footage, shown])
            await db.flush()
            spec = {"title": "Launch", "scenes": [_inserted(footage, shown)]}
            project = VideoProject(user_id=owner.id, title="Launch", spec=spec)
            db.add(project)
            await db.commit()
            # What the older rule stored: the scene's own footage, no version.
            await db.execute(
                update(VideoProject)
                .where(VideoProject.id == project.id)
                .values(asset_refs=[f"take:{footage.id}"], asset_refs_version=None)
            )
            await db.commit()
        async with scratch.sessions() as db:
            assert await library.deletion_refusal(db, owner, "take", shown.id) is None
            assert await retention.index_stale_refs(db) == 1
            await db.commit()
        async with _client(scratch.sessions, owner) as client:
            refused = await client.delete(f"{_BASE}/takes/{shown.id}")
            assert refused.status_code == 409 and refused.json()["detail"]["code"] == "media_in_use"
        async with scratch.sessions() as db:
            assert await retention.index_stale_refs(db) == 0


async def test_what_a_shot_is_made_from_and_what_a_ready_render_drew_stay_in_use() -> None:
    async with _scratch("video_media_index_sources") as scratch, scratch.sessions() as db:
        owner = _user("ada@filigran.io")
        db.add(owner)
        await db.flush()
        recording, other, drawn = _take(owner, "Flow"), _take(owner, "Other flow"), _take(owner, "Intro")
        frame = FileAttachment(user_id=owner.id, filename="frame.jpg", content_type="image/jpeg", size=1, s3_object_name="f")
        db.add_all([recording, other, drawn, frame])
        await db.flush()
        start = {"kind": "recording", "id": str(recording.id), "file_id": str(frame.id), "frame_file_id": str(frame.id)}
        shot = _shot(owner, "The flow, continued", params={"reference_video": start})
        db.add(shot)
        await db.flush()
        video = VideoProject(
            user_id=owner.id,
            title="Film",
            spec={"title": "Film", "scenes": [{"type": "clip", "clip_id": str(shot.id)}]},
        )
        db.add(video)
        await db.commit()
        # The shot is all the video names: what it was cut from is needed with it.
        assert sorted(await retention.media_in_use(db, [("take", recording.id), ("take", other.id)])) == [
            str(recording.id)
        ]
        assert await retention.files_in_use(db, [frame.id]) == {str(frame.id)}
        # Generated again from another recording: the video needs that one at once.
        shot.params = {"reference_video": {**start, "id": str(other.id)}}
        await db.commit()
        assert await retention.media_in_use(db, [("take", other.id)]) == {str(other.id)}

        # A ready render drew a scene the spec has since dropped: needed while the render is ready.
        render = VideoRender(
            project_id=video.id,
            user_id=owner.id,
            revision=1,
            quality="draft",
            status=JOB_READY,
            asset_refs=[f"take:{drawn.id}"],
        )
        db.add(render)
        await db.flush()
        retention.mark_for_reindex(video)
        await db.commit()
        assert await retention.media_in_use(db, [("take", drawn.id)]) == {str(drawn.id)}
        render.status = "failed"
        await db.flush()
        retention.mark_for_reindex(video)
        await db.commit()
        assert await retention.media_in_use(db, [("take", drawn.id)]) == set()
        stored = (await db.execute(select(VideoProject.asset_refs_version).where(VideoProject.id == video.id))).scalar_one()
        assert stored == 2


async def test_a_file_a_video_needs_stays_when_the_shot_it_came_with_goes(monkeypatch) -> None:
    removed: list[str] = []

    async def delete(name):
        removed.append(name)

    monkeypatch.setattr(assets.file_storage, "delete", delete)
    async with _scratch("video_media_index_files") as scratch:
        async with scratch.sessions() as db:
            owner = _user("grace@filigran.io")
            db.add(owner)
            await db.flush()
            video_file = FileAttachment(user_id=owner.id, filename="shot.mp4", content_type="video/mp4", size=1, s3_object_name="shot")
            poster = FileAttachment(user_id=owner.id, filename="poster.jpg", content_type="image/jpeg", size=1, s3_object_name="poster")
            db.add_all([video_file, poster])
            await db.flush()
            shot = _shot(owner, "A cow", file_id=video_file.id, poster_file_id=poster.id)
            db.add(shot)
            await db.flush()
            # Another video shows the shot's poster on a screen of its backdrop: a file only an overlay names.
            backdrop = FileAttachment(user_id=owner.id, filename="room.png", content_type="image/png", size=1, s3_object_name="room")
            db.add(backdrop)
            await db.flush()
            scene = {
                "type": "statement",
                "lines": ["Every alert"],
                "backdrop": {"file_id": str(backdrop.id)},
                "overlays": [{"type": "screen_insert", "source": {"file_id": str(poster.id)}, "quad": _QUAD}],
            }
            db.add(VideoProject(user_id=owner.id, title="Stills", spec={"title": "Stills", "scenes": [scene]}))
            await db.commit()
        async with scratch.sessions() as db:
            files = await library.remove_media(db, [("clip", await db.get(VideoClip, shot.id))])
            await db.commit()
        await assets.delete_files(files)
        async with scratch.sessions() as db:
            left = set((await db.execute(select(FileAttachment.s3_object_name))).scalars().all())
        assert left == {"poster", "room"} and removed == ["shot"]
