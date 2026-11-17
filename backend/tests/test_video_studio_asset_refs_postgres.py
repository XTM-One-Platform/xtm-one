"""Which videos use a recording, an AI shot or an AI track, against PostgreSQL.

``video_projects.asset_refs`` indexes the media each spec names, so the
question is one indexed lookup, never a scan of up to 500 visible specs:

* every write of a project indexes it, whichever path wrote it; the migration
  indexes the existing videos exactly as the model does, and the media
  retention indexes a row an older replica left without it, keeping its
  ``updated_at``;
* a person who can open a video plays its media however many videos they see;
* a failed shot only another person's video uses is kept by the sweep and
  served to whoever can open that video; one no video uses is purged;
* ``GET /assets`` keeps a medium from expiring when any video uses it, and
  lists in ``used_in`` only the videos the caller can open.

Every actor is an ordinary user (rule 19). ``integration``-marked and skips
when PostgreSQL is unreachable, like its neighbours.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import video_studio
from app.core.database import get_db
from app.models.user import User
from app.models.video_studio import JOB_FAILED, JOB_READY, VideoClip, VideoMusic, VideoProject, VideoTake
from app.services.video_studio import projects, retention
from app.services.video_studio.spec import asset_refs
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("video_studio_enabled")]

_BASE = "/api/v1/video-studio"
_REVISION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "v5921arefs01_add_video_project_asset_refs.py"


def _load_revision():
    spec = importlib.util.spec_from_file_location("v5921arefs01", _REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scratch(name: str):
    return scratch_database(name, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True)


def _user(email: str) -> User:
    return User(id=uuid.uuid4(), email=email, hashed_password="x", full_name=email.split("@")[0], role="user")


def _clip(owner: User, **columns) -> VideoClip:
    return VideoClip(
        **{"user_id": owner.id, "provider": "gemini", "model": "veo-3", "prompt": "A cow", "params": {}, **columns}
    )


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


def _every_reference(take: uuid.UUID, clips: list[uuid.UUID], music: uuid.UUID, files: list[uuid.UUID]) -> dict:
    """A spec naming media at every place ``spec.referenced_ids`` reads, and some it ignores."""
    return {
        "title": "Every reference",
        "music": {"music_id": str(music)},
        "scenes": [
            {"type": "recording", "take_id": str(take)},
            {"type": "clip", "clip_id": str(clips[0]), "file_id": str(files[0])},
            {"type": "image", "file_id": str(files[1]), "animate": {"clip_id": str(clips[1])}},
            {"type": "statement", "lines": ["Hi"], "backdrop": {"clip_id": str(clips[2])}},
            {
                "type": "title",
                "overlays": [
                    {"type": "screen_insert", "source": {"take_id": str(take).upper(), "clip_id": str(clips[3])}},
                    {"type": "image", "file_id": str(files[2])},
                ],
            },
            # Not a recording scene: its take_id is not a reference, as in referenced_ids.
            {"type": "title", "take_id": str(uuid.uuid4())},
            {"type": "clip", "clip_id": "not-a-uuid"},
        ],
    }


async def test_every_write_indexes_the_video_and_the_migration_indexes_existing_ones_the_same_way() -> None:
    take, music = uuid.uuid4(), uuid.uuid4()
    clips = [uuid.uuid4() for _ in range(4)]
    files = [uuid.uuid4() for _ in range(3)]
    spec = _every_reference(take, clips, music, files)
    # The files it names are in the one media index too.
    expected = sorted(
        [f"take:{take}", f"music:{music}", *(f"clip:{clip}" for clip in clips), *(f"file:{f}" for f in files)],
    )
    # What the migration indexed: the recordings, AI shots and AI tracks.
    backfilled = [key for key in expected if not key.startswith("file:")]
    assert asset_refs(spec) == expected
    async with _scratch("video_asset_refs_index") as scratch:
        async with scratch.sessions() as db:
            owner = _user("samuel@filigran.io")
            db.add(owner)
            await db.flush()
            video = VideoProject(user_id=owner.id, title="Film", spec=spec)
            empty = VideoProject(user_id=owner.id, title="Empty", spec={"title": "Empty", "scenes": []})
            db.add_all([video, empty])
            await db.commit()
            # Inserted: indexed by the mapper event.
            assert (await db.get(VideoProject, video.id)).asset_refs == expected
            # Updated: indexed again.
            video.spec = {"title": "Film", "scenes": [{"type": "recording", "take_id": str(take)}]}
            await db.commit()
            stored = (await db.execute(select(VideoProject.asset_refs).where(VideoProject.id == video.id))).scalar_one()
            assert stored == [f"take:{take}"]
            # Rows an older replica wrote: the migration's backfill indexes them as its rule did.
            await db.execute(update(VideoProject).values(asset_refs=None, asset_refs_version=None, spec=VideoProject.spec))
            await db.execute(update(VideoProject).where(VideoProject.id == video.id).values(spec=spec, asset_refs=None))
            await db.commit()
        revision = _load_revision()
        async with scratch.engine.connect() as conn:
            indexed = await conn.run_sync(lambda sync: revision.backfill_in_batches(sync, batch_size=1))
            await conn.commit()
        assert indexed == 2
        async with scratch.sessions() as db:
            rows = dict((await db.execute(select(VideoProject.title, VideoProject.asset_refs))).all())
        assert rows == {"Film": backfilled, "Empty": []}
        # Indexed by an older rule: the media retention indexes them again as the model does now.
        async with scratch.sessions() as db:
            assert await retention.index_stale_refs(db) == 2
            await db.commit()
            rows = dict((await db.execute(select(VideoProject.title, VideoProject.asset_refs))).all())
        assert rows == {"Film": expected, "Empty": []}


async def test_the_sweep_indexes_a_row_left_unindexed_without_touching_when_it_changed() -> None:
    async with _scratch("video_asset_refs_heal") as scratch:
        changed = datetime(2026, 1, 2, tzinfo=UTC)
        async with scratch.sessions() as db:
            owner = _user("samuel@filigran.io")
            db.add(owner)
            await db.flush()
            failed = _clip(owner, status=JOB_FAILED, finished_at=changed, created_at=changed)
            db.add(failed)
            await db.flush()
            video = VideoProject(
                user_id=owner.id, title="Old", spec={"title": "Old", "scenes": [{"type": "clip", "clip_id": str(failed.id)}]}
            )
            db.add(video)
            await db.commit()
            await db.execute(
                update(VideoProject).where(VideoProject.id == video.id).values(asset_refs=None, updated_at=changed)
            )
            await db.commit()
        async with scratch.sessions() as db:
            await retention.sweep(db, now=changed + timedelta(days=2))
        async with scratch.sessions() as db:
            refs, updated_at = (
                await db.execute(select(VideoProject.asset_refs, VideoProject.updated_at).where(VideoProject.id == video.id))
            ).one()
            # Indexed before the purge read it, so the failed shot it uses is kept.
            assert refs == [f"clip:{failed.id}"] and updated_at == changed
            assert await db.get(VideoClip, failed.id) is not None


async def test_a_shared_video_shares_its_media_however_many_videos_the_reader_sees() -> None:
    """The access check read at most 500 visible videos, in no order."""
    async with _scratch("video_asset_refs_access") as scratch:
        async with scratch.sessions() as db:
            owner, reader, outsider = _user("ada@filigran.io"), _user("grace@filigran.io"), _user("alan@filigran.io")
            db.add_all([owner, reader, outsider])
            await db.flush()
            take = VideoTake(user_id=owner.id, name="Flow", status=JOB_READY, script={})
            track = VideoMusic(user_id=owner.id, provider="eleven", model="m", prompt="Calm", params={}, status=JOB_READY)
            db.add_all([take, track])
            await db.flush()
            old = datetime(2026, 1, 1, tzinfo=UTC)
            db.add(
                VideoProject(
                    user_id=owner.id,
                    title="Shared film",
                    spec={
                        "title": "Shared film",
                        "music": {"music_id": str(track.id)},
                        "scenes": [{"type": "recording", "take_id": str(take.id)}],
                    },
                    shared_user_ids=[str(reader.id)],
                    shared_user_permissions={str(reader.id): "read_only"},
                    created_at=old,
                    updated_at=old,
                )
            )
            # 501 newer company videos the reader sees too, none using the take.
            recent = datetime(2026, 6, 1, tzinfo=UTC)
            db.add_all(
                [
                    VideoProject(
                        user_id=None,
                        title=f"Company {n}",
                        spec={"title": f"Company {n}", "scenes": []},
                        is_company_managed=True,
                        created_at=recent,
                        updated_at=recent + timedelta(minutes=n),
                    )
                    for n in range(501)
                ]
            )
            await db.commit()
        async with scratch.sessions() as db:
            assert await projects.can_read_project_asset(db, "take", take, reader)
            assert await projects.can_read_project_asset(db, "music", track, reader)
            assert not await projects.can_read_project_asset(db, "take", take, outsider)


async def test_a_failed_shot_another_persons_video_uses_is_kept_and_served_to_who_can_open_it() -> None:
    """Listing and purging read the same source of truth, with no cap and no per-caller view."""
    async with _scratch("video_asset_refs_failed") as scratch:
        long_ago = datetime(2026, 1, 1, tzinfo=UTC)
        async with scratch.sessions() as db:
            maker, colleague, stranger = _user("ada@filigran.io"), _user("grace@filigran.io"), _user("alan@filigran.io")
            db.add_all([maker, colleague, stranger])
            await db.flush()
            used = _clip(maker, status=JOB_FAILED, error="Refused", finished_at=long_ago, created_at=long_ago)
            unused = _clip(maker, status=JOB_FAILED, error="Refused", finished_at=long_ago, created_at=long_ago)
            db.add_all([used, unused])
            await db.flush()
            db.add(
                VideoProject(
                    user_id=maker.id,
                    title="Team film",
                    spec={"title": "Team film", "scenes": [{"type": "clip", "clip_id": str(used.id)}]},
                    shared_user_ids=[str(colleague.id)],
                    shared_user_permissions={str(colleague.id): "config"},
                    created_at=long_ago,
                    updated_at=long_ago,
                )
            )
            await db.commit()
        async with scratch.sessions() as db:
            counts = await retention.sweep(db, now=long_ago + timedelta(days=1))
        assert counts["purged"] == 1
        async with scratch.sessions() as db:
            assert await db.get(VideoClip, unused.id) is None
            assert await db.get(VideoClip, used.id) is not None
        async with _client(scratch.sessions, colleague) as client:
            shot = await client.get(f"{_BASE}/clips/{used.id}")
            assert shot.status_code == 200, shot.text
            assert shot.json()["status"] == JOB_FAILED
        async with _client(scratch.sessions, stranger) as client:
            assert (await client.get(f"{_BASE}/clips/{used.id}")).status_code == 404


async def test_the_library_says_in_use_for_any_video_and_lists_only_the_videos_the_caller_can_open() -> None:
    async with _scratch("video_asset_refs_library") as scratch:
        async with scratch.sessions() as db:
            maker, other = _user("ada@filigran.io"), _user("alan@filigran.io")
            db.add_all([maker, other])
            await db.flush()
            mine_only = _clip(maker, status=JOB_READY, prompt="Mine")
            elsewhere = _clip(maker, status=JOB_READY, prompt="Elsewhere")
            unused = _clip(maker, status=JOB_READY, prompt="Unused")
            db.add_all([mine_only, elsewhere, unused])
            await db.flush()
            db.add_all(
                [
                    VideoProject(
                        user_id=maker.id,
                        title="My film",
                        spec={"title": "My film", "scenes": [{"type": "clip", "clip_id": str(mine_only.id)}]},
                    ),
                    # Someone else's video using the maker's shot (made before the shot was unshared, say).
                    VideoProject(
                        user_id=other.id,
                        title="Their film",
                        spec={"title": "Their film", "scenes": [{"type": "clip", "clip_id": str(elsewhere.id)}]},
                    ),
                ]
            )
            await db.commit()
        async with _client(scratch.sessions, maker) as client:
            answer = await client.get(f"{_BASE}/assets", params={"kind": "clip"})
            assert answer.status_code == 200, answer.text
            items = {item["prompt"]: item for item in answer.json()["items"]}
        assert [use["title"] for use in items["Mine"]["used_in"]] == ["My film"]
        assert items["Mine"]["expires_at"] is None
        # Kept from expiring because a video uses it, without naming a video the maker cannot open.
        assert items["Elsewhere"]["used_in"] == [] and items["Elsewhere"]["expires_at"] is None
        assert items["Unused"]["used_in"] == [] and items["Unused"]["expires_at"] is not None
