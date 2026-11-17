"""The Video Studio's libraries a page at a time, against PostgreSQL and the REST routes.

AI generation fills the libraries fast; every row must stay reachable, and the
search and the filters must cover the whole library, not the page the browser
holds. Every actor is an ordinary user (rule 19):

* the media - recordings and AI shots together, newest first - come a page at
  a time with their total, by kind, searched by every word of the name or the
  prompt; a failed shot is never listed as media; a video's own media are read
  by id whatever their age, and left out of a library search, like the
  expired ones when only the usable media are asked for;
* the videos come a page at a time with their total, the last changed first,
  searched by title or owner, narrowed to mine, shared with me or the company's;
  an outsider's video is never counted;
* the per-kind lists of recordings and shots are gone;
* the media no video uses - anyone's video, not only the ones the reader can
  open - and the media of a period are found on the whole library, and are
  deleted in bulk, by id or by the same filters, only ever the reader's own,
  the files with them, never one a video uses: those are kept and counted.

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
from sqlalchemy import insert, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import video_studio
from app.core.database import get_db
from app.models.audit_log import AuditLog
from app.models.file_attachment import FileAttachment
from app.models.user import User
from app.models.video_studio import JOB_FAILED, JOB_READY, VideoClip, VideoProject, VideoTake
from app.services import file_cleanup
from app.services.video_studio import library, retention
from app.services.video_studio.spec import ASSET_REFS_VERSION
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("video_studio_enabled")]

_BASE = "/api/v1/video-studio"


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


def _user(email: str, name: str) -> User:
    return User(email=email, hashed_password="x", full_name=name, role="user")


def _clip(owner: User, n: int, made: datetime, **columns) -> VideoClip:
    base = {
        "user_id": owner.id,
        "provider": "openai",
        "model": "sora-2",
        "prompt": f"A cow in a blue coat, shot {n}",
        "params": {},
        "status": JOB_READY,
        "created_at": made,
    }
    return VideoClip(**{**base, **columns})


async def _get(client: httpx.AsyncClient, path: str, **params) -> dict:
    answer = await client.get(f"{_BASE}{path}", params=params)
    assert answer.status_code == 200, answer.text
    return answer.json()


async def test_the_media_come_a_page_at_a_time_searched_and_filtered_on_the_server() -> None:
    async with scratch_database(
        "video_library_media", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        start = datetime(2026, 10, 1, tzinfo=UTC)
        async with scratch.sessions() as db:
            owner = _user("samuel@filigran.io", "Samuel Hassine")
            other = _user("ada@filigran.io", "Ada Lovelace")
            db.add_all([owner, other])
            await db.flush()
            # 60 AI shots and 5 recordings, a minute apart; a failed shot and someone else's shot besides.
            clips = [_clip(owner, n, start + timedelta(minutes=n)) for n in range(60)]
            clips[3].prompt = "A tabby cat on a glowing server rack"
            clips[10].expired_at = start
            takes = [
                VideoTake(
                    user_id=owner.id,
                    name=f"OpenCTI flow {n}",
                    status=JOB_READY,
                    script={},
                    created_at=start + timedelta(minutes=100 + n),
                )
                for n in range(5)
            ]
            failed = _clip(owner, 99, start + timedelta(minutes=500), status=JOB_FAILED)
            foreign = _clip(other, 98, start + timedelta(minutes=600))
            db.add_all([*clips, *takes, failed, foreign])
            await db.flush()
            oldest = clips[0]
            db.add(
                VideoProject(
                    user_id=owner.id,
                    title="Brand film",
                    spec={"title": "Brand film", "scenes": [{"type": "clip", "clip_id": str(oldest.id)}]},
                )
            )
            await db.commit()

        async with _client(scratch.sessions, owner) as client:
            # Newest first across both kinds: the recordings, then the shots; the failed and the foreign never.
            first = await _get(client, "/assets", limit=10)
            assert first["total"] == 65
            assert [item["kind"] for item in first["items"][:5]] == ["take"] * 5
            assert first["items"][0]["name"] == "OpenCTI flow 4"
            assert first["items"][5]["prompt"] == "A cow in a blue coat, shot 59"
            # The last page holds the oldest shot, with the video using it and no expiry.
            last = await _get(client, "/assets", limit=10, offset=60)
            assert len(last["items"]) == 5
            tail = last["items"][-1]
            assert tail["id"] == str(oldest.id)
            assert [use["title"] for use in tail["used_in"]] == ["Brand film"]
            assert tail["expires_at"] is None
            # By kind, and searched by every word, any case, the whole library.
            assert (await _get(client, "/assets", kind="take"))["total"] == 5
            assert (await _get(client, "/assets", kind="clip", limit=1))["total"] == 60
            found = await _get(client, "/assets", search="TABBY server")
            assert [item["prompt"] for item in found["items"]] == ["A tabby cat on a glowing server rack"]
            assert (await _get(client, "/assets", search="flow 3"))["total"] == 1
            # A video's own media by id, whatever their age; left out of a library search.
            own = await _get(client, "/assets", ids=f"{oldest.id},{takes[0].id}")
            assert {item["id"] for item in own["items"]} == {str(oldest.id), str(takes[0].id)}
            rest = await _get(client, "/assets", exclude=str(oldest.id), limit=100)
            assert rest["total"] == 64 and str(oldest.id) not in {item["id"] for item in rest["items"]}
            # The media a scene can still use: the expired shot is left out.
            assert (await _get(client, "/assets", available="true", limit=1))["total"] == 64
            # A bad id is refused, never read as "no filter".
            assert (await client.get(f"{_BASE}/assets", params={"ids": "not-a-uuid"})).status_code == 400
            # The per-kind lists are gone: GET /assets is the library.
            assert (await client.get(f"{_BASE}/clips")).status_code in (404, 405)
            assert (await client.get(f"{_BASE}/takes")).status_code in (404, 405)


async def test_the_videos_come_a_page_at_a_time_searched_and_filtered_on_the_server() -> None:
    async with scratch_database(
        "video_library_projects", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        start = datetime(2026, 10, 1, tzinfo=UTC)
        async with scratch.sessions() as db:
            owner = _user("samuel@filigran.io", "Samuel Hassine")
            colleague = _user("grace@filigran.io", "Grace Hopper")
            outsider = _user("alan@filigran.io", "Alan Turing")
            db.add_all([owner, colleague, outsider])
            await db.flush()

            def video(title: str, user: User | None, minutes: int, **columns) -> VideoProject:
                when = start + timedelta(minutes=minutes)
                return VideoProject(
                    user_id=user.id if user else None,
                    title=title,
                    spec={"title": title, "scenes": []},
                    created_at=when,
                    updated_at=when,
                    **columns,
                )

            db.add_all([video(f"Explainer {n}", owner, n) for n in range(30)])
            db.add_all(
                [
                    video("Growth 100%_plan", owner, 40),
                    video(
                        "Booth loop",
                        colleague,
                        100,
                        shared_user_ids=[str(owner.id)],
                        shared_user_permissions={str(owner.id): "read_only"},
                    ),
                    video("Company keynote", None, 200, is_company_managed=True),
                    # Shared by an account since deleted: its owner is NULL.
                    video(
                        "Orphan reel",
                        None,
                        250,
                        shared_user_ids=[str(owner.id)],
                        shared_user_permissions={str(owner.id): "read_only"},
                    ),
                    video("Secret teaser", outsider, 300),
                ]
            )
            await db.commit()

        async with _client(scratch.sessions, owner) as client:
            # The last changed first; the outsider's video is never counted.
            first = await _get(client, "/projects", limit=24)
            assert first["total"] == 34 and len(first["items"]) == 24
            assert [item["title"] for item in first["items"][:3]] == ["Orphan reel", "Company keynote", "Booth loop"]
            rest = await _get(client, "/projects", limit=24, offset=24)
            assert len(rest["items"]) == 10 and rest["items"][-1]["title"] == "Explainer 0"
            # The visibility filter and the search run on the whole library.
            assert (await _get(client, "/projects", visibility="mine"))["total"] == 31
            assert [i["title"] for i in (await _get(client, "/projects", visibility="shared"))["items"]] == [
                "Orphan reel",
                "Booth loop",
            ]
            # A search's % and _ are the characters, never wildcards.
            for typed in ("100%", "0%", "%_p"):
                assert [i["title"] for i in (await _get(client, "/projects", search=typed))["items"]] == [
                    "Growth 100%_plan"
                ]
            assert [i["title"] for i in (await _get(client, "/projects", visibility="company"))["items"]] == [
                "Company keynote"
            ]
            assert [i["title"] for i in (await _get(client, "/projects", search="explainer 17"))["items"]] == [
                "Explainer 17"
            ]
            assert [i["title"] for i in (await _get(client, "/projects", search="grace"))["items"]] == ["Booth loop"]
            assert (await _get(client, "/projects", search="teaser"))["total"] == 0
            # Each segment's count under the search, the numbers the control shows:
            # owned, shared (an orphan included) and company add up to All; the outsider never counts.
            counts = await _get(client, "/projects/segment-counts")
            assert counts == {"mine": 31, "shared": 2, "company": 1, "all": 34}
            for segment, value in counts.items():
                assert (await _get(client, "/projects", vis=segment, limit=1))["total"] == value, segment
            assert await _get(client, "/projects/segment-counts", search="explainer") == {
                "mine": 30,
                "shared": 0,
                "company": 0,
                "all": 30,
            }
            # A typo in the filter is refused, never read as "everything".
            assert (await client.get(f"{_BASE}/projects", params={"visibility": "min"})).status_code == 422


def _scene(kind: str, asset: VideoTake | VideoClip) -> dict:
    return {"type": "recording", "take_id": str(asset.id)} if kind == "take" else {"type": "clip", "clip_id": str(asset.id)}


def _video(owner: User, title: str, *scenes: dict) -> VideoProject:
    return VideoProject(user_id=owner.id, title=title, spec={"title": title, "scenes": list(scenes)})


async def test_the_media_no_video_uses_and_the_media_of_a_period_are_found_on_the_whole_library() -> None:
    async with scratch_database(
        "video_library_usage", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        start = datetime(2026, 10, 1, tzinfo=UTC)
        async with scratch.sessions() as db:
            owner = _user("samuel@filigran.io", "Samuel Hassine")
            colleague = _user("grace@filigran.io", "Grace Hopper")
            db.add_all([owner, colleague])
            await db.flush()
            # Six shots a day apart and a recording on day 10.
            clips = [_clip(owner, n, start + timedelta(days=n)) for n in range(6)]
            take = VideoTake(
                user_id=owner.id, name="OpenCTI flow", status=JOB_READY, script={}, created_at=start + timedelta(days=10)
            )
            db.add_all([*clips, take])
            await db.flush()
            # The owner's video uses shot 0 and the recording; a colleague's video the owner cannot open uses shot 1.
            db.add_all(
                [
                    _video(owner, "Brand film", _scene("clip", clips[0]), _scene("take", take)),
                    _video(colleague, "Booth loop", _scene("clip", clips[1])),
                ]
            )
            await db.commit()

        async with _client(scratch.sessions, owner) as client:
            unused = await _get(client, "/assets", usage="unused")
            assert unused["total"] == 4
            assert {item["id"] for item in unused["items"]} == {str(c.id) for c in clips[2:]}
            assert all(item["used_in"] == [] and item["in_use"] is False for item in unused["items"])
            # In a video: the colleague's counts, though the owner cannot open it and none is listed.
            used = await _get(client, "/assets", usage="used")
            assert {item["id"] for item in used["items"]} == {str(clips[0].id), str(clips[1].id), str(take.id)}
            booth = next(item for item in used["items"] if item["id"] == str(clips[1].id))
            assert booth["used_in"] == [] and booth["in_use"] is True
            # By kind and use together.
            assert (await _get(client, "/assets", usage="used", kind="take"))["total"] == 1
            # A period, both ends included.
            period = await _get(
                client,
                "/assets",
                date_from=(start + timedelta(days=2)).isoformat(),
                date_to=(start + timedelta(days=4)).isoformat(),
            )
            assert [item["id"] for item in period["items"]] == [str(clips[n].id) for n in (4, 3, 2)]
            assert (await _get(client, "/assets", date_from=(start + timedelta(days=5)).isoformat()))["total"] == 2
            # An instant without a zone reads as UTC.
            assert (await _get(client, "/assets", date_to="2026-10-01T00:00:00"))["total"] == 1
            # A typo in the filter is refused, never read as "everything".
            assert (await client.get(f"{_BASE}/assets", params={"usage": "unsed"})).status_code == 422


async def test_the_media_are_deleted_in_bulk_by_id_or_by_the_library_filters_only_ever_the_callers(monkeypatch) -> None:
    purged: list[list[str]] = []

    async def purge(keys):
        purged.append(list(keys))
        return len(purged[-1])

    monkeypatch.setattr(file_cleanup, "purge_storage_objects", purge)
    async with scratch_database(
        "video_library_bulk_delete", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        start = datetime(2026, 10, 1, tzinfo=UTC)
        async with scratch.sessions() as db:
            owner = _user("samuel@filigran.io", "Samuel Hassine")
            colleague = _user("grace@filigran.io", "Grace Hopper")
            db.add_all([owner, colleague])
            await db.flush()
            files = [
                FileAttachment(
                    user_id=owner.id, filename=f"shot-{n}.mp4", content_type="video/mp4", size=10, s3_object_name=f"media/{n}"
                )
                for n in range(3)
            ]
            db.add_all(files)
            await db.flush()
            # 150 unused shots (more than one batch), two with their files; a shot and a recording in a video.
            unused = [_clip(owner, n, start + timedelta(minutes=n)) for n in range(150)]
            unused[0].file_id, unused[0].poster_file_id = files[0].id, files[1].id
            kept = _clip(owner, 900, start + timedelta(days=1), file_id=files[2].id)
            take = VideoTake(user_id=owner.id, name="Flow", status=JOB_READY, script={}, created_at=start)
            failed = _clip(owner, 901, start, status=JOB_FAILED)
            foreign = _clip(colleague, 902, start)
            db.add_all([*unused, kept, take, failed, foreign])
            await db.flush()
            db.add(_video(owner, "Brand film", _scene("clip", kept), _scene("take", take)))
            await db.commit()

        async with _client(scratch.sessions, owner) as client:
            # Neither ids nor filters, a bad id, or too many ids: refused, nothing deleted.
            assert (await client.post(f"{_BASE}/assets/bulk-delete", json={})).status_code == 400
            bad = {"items": [{"kind": "clip", "id": "not-a-uuid"}]}
            assert (await client.post(f"{_BASE}/assets/bulk-delete", json=bad)).status_code == 422
            many = {"items": [{"kind": "clip", "id": str(unused[0].id)}] * 501}
            assert (await client.post(f"{_BASE}/assets/bulk-delete", json=many)).status_code == 422
            # By id: the caller's own listed media; the failed shot and a colleague's are skipped like unknown ids,
            # and the shot a video uses is kept and counted.
            named = {
                "items": [
                    {"kind": "clip", "id": str(unused[149].id)},
                    {"kind": "clip", "id": str(kept.id)},
                    {"kind": "clip", "id": str(failed.id)},
                    {"kind": "clip", "id": str(foreign.id)},
                    {"kind": "take", "id": str(unused[148].id)},
                ]
            }
            answer = await client.post(f"{_BASE}/assets/bulk-delete", json=named)
            assert answer.status_code == 200 and answer.json() == {"deleted": 1, "kept_in_use": 1}
            # Every medium no video uses, past one batch, the files with them; the ones in a video stay.
            unused_only = {"all_matching_filters": True, "usage": "unused"}
            answer = await client.post(f"{_BASE}/assets/bulk-delete", json=unused_only)
            assert answer.status_code == 200 and answer.json() == {"deleted": 149, "kept_in_use": 0}
            left = await _get(client, "/assets", limit=100)
            assert {item["id"] for item in left["items"]} == {str(kept.id), str(take.id)}
            # Every medium at all: the two a video uses are kept, whatever the filter asked.
            everything = {"all_matching_filters": True}
            assert (await client.post(f"{_BASE}/assets/bulk-delete", json=everything)).json() == {
                "deleted": 0,
                "kept_in_use": 2,
            }
            used_only = {"all_matching_filters": True, "usage": "used", "kind": "take"}
            assert (await client.post(f"{_BASE}/assets/bulk-delete", json=used_only)).json() == {
                "deleted": 0,
                "kept_in_use": 1,
            }
            assert (await _get(client, "/assets"))["total"] == 2

        async with scratch.sessions() as db:
            remaining = set((await db.execute(select(FileAttachment.s3_object_name))).scalars().all())
            assert remaining == {"media/2"}
            assert sorted(key for batch in purged for key in batch) == ["media/0", "media/1"]
            foreigners = (await db.execute(select(VideoClip.id).where(VideoClip.user_id == colleague.id))).scalars().all()
            assert foreigners == [foreign.id]
            assert (await db.get(VideoClip, failed.id)) is not None
            audits = (
                (await db.execute(select(AuditLog).where(AuditLog.entity_type == "video_media").order_by(AuditLog.created_at)))
                .scalars()
                .all()
            )
            assert [(a.details["ai_shots"], a.details["kept_in_use"], a.details["by_filters"]) for a in audits] == [
                (1, 1, False),
                (149, 0, True),
            ]
            assert {a.action for a in audits} == {"delete"}


async def _plan(db, query) -> str:
    """PostgreSQL's plan of *query*, its values inlined."""
    sql = str(query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    return "\n".join((await db.execute(text(f"EXPLAIN {sql}"))).scalars().all())


async def test_the_use_filter_reads_the_gin_index_of_the_videos_media_on_thousands_of_videos() -> None:
    """``usage`` asks each medium whether a video names it: ``EXISTS (... asset_refs ? '<kind>:<id>')`` once a
    row. On a library of thousands of videos PostgreSQL answers it off ``ix_video_projects_asset_refs_jsonb_gin``,
    never a scan of every video for every medium - and the videos a page's media are used in the same way."""
    async with scratch_database(
        "video_library_gin", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        start = datetime(2026, 10, 1, tzinfo=UTC)
        async with scratch.sessions() as db:
            owner = _user("samuel@filigran.io", "Samuel Hassine")
            db.add(owner)
            await db.flush()
            clips = [_clip(owner, n, start + timedelta(days=n)) for n in range(20)]
            db.add_all(clips)
            await db.flush()
            # 4000 videos naming media of their own, the first of them shot 0 - their index written with them, as a
            # replica writes it.
            rows = [
                {
                    "user_id": owner.id,
                    "title": f"Video {n}",
                    "spec": {"title": f"Video {n}", "scenes": []},
                    "asset_refs": [f"clip:{uuid.uuid4()}", f"take:{uuid.uuid4()}"],
                    "asset_refs_version": ASSET_REFS_VERSION,
                }
                for n in range(4000)
            ]
            rows[0]["asset_refs"] = [f"clip:{clips[0].id}"]
            await db.execute(insert(VideoProject.__table__), rows)
            await db.commit()
            index = "ix_video_projects_asset_refs_jsonb_gin"
            await db.execute(text("ANALYZE video_projects"))
            # The index as autovacuum keeps it: what a bulk write leaves in its pending list is priced into every
            # lookup (59 pages here: 82.6 against 4.3), so the planner may scan the videos until that list is flushed.
            await db.execute(text(f"SELECT gin_clean_pending_list('{index}'::regclass)"))

            for usage, expected in (("used", {clips[0].id}), ("unused", {clip.id for clip in clips[1:]})):
                query = select(VideoClip.id).where(*library._asset_filters(VideoClip, "clip", owner, library.AssetFilters(usage=usage)))
                assert set((await db.execute(query)).scalars().all()) == expected
                plan = await _plan(db, query)
                assert index in plan, plan
                assert "Seq Scan on video_projects" not in plan, plan
            keys = retention.media_keys([("clip", clips[0].id), ("clip", clips[1].id)])
            assert index in await _plan(db, select(VideoProject.id).where(retention.uses_media(keys)))
