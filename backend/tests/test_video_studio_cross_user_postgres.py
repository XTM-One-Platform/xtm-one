"""Another person's browser profile, MFA challenge and media, against PostgreSQL and the REST routes.

Every actor is an ordinary user (rule 19: an admin short-circuits the
visibility filter):

* a profile is invisible to someone it is not shared with - read, change,
  sign-in, session check, MFA screenshot - and its MFA challenge can be
  neither read nor answered by them, while its owner can;
* a media token is minted for a recording, an AI shot, an AI track or a
  render only to someone who may read it: its maker, or a person who can open
  a video using it - never a stranger;
* the per-person limit of renders in flight holds against two starts racing
  on two videos.

``integration``-marked and skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import browser_profiles, video_studio
from app.core.config import settings
from app.core.database import get_db
from app.models.browser_profile import BrowserProfile
from app.models.file_attachment import FileAttachment
from app.models.user import User
from app.models.video_studio import (
    JOB_PENDING,
    JOB_READY,
    VideoClip,
    VideoMusic,
    VideoProject,
    VideoRender,
    VideoTake,
)
from app.services.browser_profiles import signin
from app.services.video_studio import jobs, projects
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("video_studio_enabled")]


def _scratch(name: str):
    return scratch_database(name, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True)


def _user(email: str) -> User:
    return User(id=uuid.uuid4(), email=email, hashed_password="x", full_name=email.split("@")[0], role="user")


@asynccontextmanager
async def _client(sessions: async_sessionmaker, user: User) -> AsyncIterator[httpx.AsyncClient]:
    api = FastAPI()
    api.include_router(video_studio.router, prefix="/api/v1/video-studio")
    api.include_router(browser_profiles.router, prefix="/api/v1/browser-profiles")

    async def _get_db():
        async with sessions() as db:
            yield db
            await db.commit()

    api.dependency_overrides[deps.get_current_user] = lambda: user
    api.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        yield client


async def test_another_persons_profile_and_its_mfa_challenge_are_out_of_reach(monkeypatch) -> None:
    async with _scratch("video_cross_user_profiles") as scratch:
        async with scratch.sessions() as db:
            owner, stranger = _user("ada@filigran.io"), _user("alan@filigran.io")
            db.add_all([owner, stranger])
            await db.flush()
            profile = BrowserProfile(user_id=owner.id, name="Demo tenant", start_url="https://demo.example.com/")
            db.add(profile)
            await db.commit()

        challenge = str(uuid.uuid4())

        async def read_state(cid: str):
            if cid != challenge:
                return None
            return signin.SignInOutcome(status="mfa_required", message="Code?", challenge_id=cid, profile_id=str(profile.id))

        async def submit_code(cid: str, code: str, **_kwargs):
            return signin.SignInOutcome(status="active", message="Signed in.", challenge_id=cid, profile_id=str(profile.id))

        monkeypatch.setattr(signin, "read_state", read_state)
        monkeypatch.setattr(signin, "submit_code", submit_code)
        base = f"/api/v1/browser-profiles/{profile.id}"
        body = {"name": "Mine now", "start_url": "https://demo.example.com/"}
        async with _client(scratch.sessions, stranger) as client:
            assert (await client.get(base)).status_code == 404
            assert (await client.put(base, json=body)).status_code == 404
            assert (await client.delete(base)).status_code == 404
            assert (await client.post(f"{base}/sign-in")).status_code == 404
            assert (await client.post(f"{base}/check")).status_code == 404
            assert (await client.get(f"{base}/mfa-screenshot")).status_code == 404
            assert (await client.get(f"/api/v1/browser-profiles/mfa/{challenge}")).status_code == 404
            refused = await client.post(f"/api/v1/browser-profiles/mfa/{challenge}", json={"code": "123456"})
            assert refused.status_code == 404
            assert all(item["id"] != str(profile.id) for item in (await client.get("/api/v1/browser-profiles")).json()["items"])
        async with _client(scratch.sessions, owner) as client:
            assert (await client.get(f"/api/v1/browser-profiles/mfa/{challenge}")).json()["status"] == "mfa_required"
            answered = await client.post(f"/api/v1/browser-profiles/mfa/{challenge}", json={"code": "123456"})
            assert answered.json()["status"] == "active"
        async with scratch.sessions() as db:
            assert (await db.get(BrowserProfile, profile.id)).name == "Demo tenant"


async def test_a_media_token_is_minted_only_for_who_may_read_the_medium() -> None:
    async with _scratch("video_cross_user_media") as scratch:
        async with scratch.sessions() as db:
            maker, reader, stranger = _user("ada@filigran.io"), _user("grace@filigran.io"), _user("alan@filigran.io")
            db.add_all([maker, reader, stranger])
            await db.flush()
            files = []
            for n, kind in enumerate(("video/mp4", "video/mp4", "audio/mpeg", "video/mp4")):
                row = FileAttachment(
                    user_id=maker.id, filename=f"f{n}", content_type=kind, size=10, s3_object_name=f"media/{uuid.uuid4()}"
                )
                db.add(row)
                files.append(row)
            await db.flush()
            take = VideoTake(user_id=maker.id, name="Flow", status=JOB_READY, script={}, file_id=files[0].id)
            clip = VideoClip(
                user_id=maker.id, provider="gemini", model="veo", prompt="A cow", params={}, status=JOB_READY, file_id=files[1].id
            )
            track = VideoMusic(
                user_id=maker.id, provider="eleven", model="m", prompt="Calm", params={}, status=JOB_READY, file_id=files[2].id
            )
            db.add_all([take, clip, track])
            await db.flush()
            video = VideoProject(
                user_id=maker.id,
                title="Shared film",
                spec={
                    "title": "Shared film",
                    "music": {"music_id": str(track.id)},
                    "scenes": [{"type": "recording", "take_id": str(take.id)}, {"type": "clip", "clip_id": str(clip.id)}],
                },
                shared_user_ids=[str(reader.id)],
                shared_user_permissions={str(reader.id): "read_only"},
            )
            db.add(video)
            await db.flush()
            render = VideoRender(
                project_id=video.id, user_id=maker.id, revision=1, quality="high", status=JOB_READY, file_id=files[3].id
            )
            db.add(render)
            await db.commit()

        asks = [
            {"kind": "take", "id": str(take.id)},
            {"kind": "clip", "id": str(clip.id)},
            {"kind": "music", "id": str(track.id), "asset": "audio"},
            {"kind": "render", "id": str(render.id)},
        ]
        for person, allowed in ((maker, True), (reader, True), (stranger, False)):
            async with _client(scratch.sessions, person) as client:
                for ask in asks:
                    answer = await client.post("/api/v1/video-studio/media-token", json=ask)
                    assert answer.status_code == (200 if allowed else 404), (person.email, ask, answer.text)
                    if allowed:
                        assert "/api/v1/video-studio/media/" in answer.json()["url"]


async def test_the_per_person_render_limit_holds_against_two_starts_on_two_videos(monkeypatch) -> None:
    """The count of renders in flight was read, then the render inserted, with nothing between two starts."""
    monkeypatch.setattr(settings, "video_studio_max_renders_per_user", 1)

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(jobs, "wake", nothing)
    spec = {"title": "Film", "freestyle": True, "scenes": [{"type": "title", "title": "Hi"}]}
    async with _scratch("video_cross_user_limit") as scratch:
        async with scratch.sessions() as db:
            owner = _user("ada@filigran.io")
            db.add(owner)
            await db.flush()
            first = VideoProject(user_id=owner.id, title="First", spec=spec, scene_count=1)
            second = VideoProject(user_id=owner.id, title="Second", spec=spec, scene_count=1)
            db.add_all([first, second])
            await db.commit()

        async def start(project_id: uuid.UUID) -> bool:
            async with scratch.sessions() as db:
                video = await db.get(VideoProject, project_id)
                user = await db.get(User, owner.id)
                try:
                    await projects.start_render(db, video, user, quality="draft")
                except projects.RenderLimitError:
                    await db.rollback()
                    return False
                await asyncio.sleep(0.2)  # hold the transaction open, as a slower start would
                await db.commit()
                return True

        outcomes = await asyncio.gather(start(first.id), start(second.id))
        assert sorted(outcomes) == [False, True]
        async with scratch.sessions() as db:
            pending = (await db.execute(select(VideoRender.id).where(VideoRender.status == JOB_PENDING))).all()
        assert len(pending) == 1
