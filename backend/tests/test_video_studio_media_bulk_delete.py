"""The media library's use and date filters and its bulk deletion, without a database.

What PostgreSQL itself answers - the filters over the whole library, the
deletion by id and by filters, the files purged - is
``test_video_studio_library_postgres.py``. Here:

* "used" is read row by row from the same index as ``in_use``: an ``EXISTS``
  over ``asset_refs ? '<kind>:<id>'``, every video whoever owns it;
* a period without a zone is read as UTC;
* a bulk deletion naming nothing, or a malformed id, is refused before
  anything is read.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.api import deps
from app.api.v1 import video_studio
from app.core.database import get_db
from app.models.user import User
from app.models.video_studio import VideoClip, VideoTake
from app.services.video_studio import library, retention

pytestmark = pytest.mark.usefixtures("video_studio_enabled")


def _sql(clause) -> str:
    """The PostgreSQL of *clause*, its whitespace one space wherever SQLAlchemy breaks or pads it."""
    return " ".join(str(clause.compile(dialect=postgresql.dialect())).split())


def test_a_medium_is_used_when_any_video_names_it_through_the_asset_index() -> None:
    sql = _sql(select(VideoClip.id).where(retention.named_by_a_video(VideoClip, "clip")))
    # Correlated to the row, the key grouped: ``?`` and ``||`` share a precedence in PostgreSQL.
    assert "WHERE EXISTS (SELECT video_projects.id FROM video_projects WHERE" in sql
    assert "CAST(video_projects.asset_refs AS JSONB) ? (%(param_1)s || CAST(video_clips.id AS TEXT))" in sql
    # Every video: no owner, sharing or visibility clause narrows it.
    assert "user_id" not in sql and "shared" not in sql


def _filtered(model: type[VideoTake] | type[VideoClip], label: str, filters: library.AssetFilters) -> str:
    user = User(email="samuel@filigran.io", hashed_password="x", full_name="Samuel", role="user")
    return _sql(select(model.id).where(*library._asset_filters(model, label, user, filters)))


def test_the_library_filters_on_use_and_on_when_the_media_were_made() -> None:
    made = datetime(2026, 10, 1, tzinfo=UTC)
    take_sql = _filtered(VideoTake, "take", library.AssetFilters(usage="unused", created_from=made, created_to=made))
    assert "NOT (EXISTS (SELECT video_projects.id" in take_sql
    assert "CAST(video_takes.id AS TEXT)" in take_sql
    assert "video_takes.created_at >= " in take_sql and "video_takes.created_at <= " in take_sql
    clip_sql = _filtered(VideoClip, "clip", library.AssetFilters(usage="used"))
    assert "EXISTS (SELECT video_projects.id" in clip_sql and "NOT (EXISTS" not in clip_sql
    assert "EXISTS" not in _filtered(VideoClip, "clip", library.AssetFilters())


def test_a_period_without_a_zone_is_read_as_utc() -> None:
    filters = library.AssetFilters(created_from=datetime(2026, 10, 1), created_to=datetime(2026, 10, 2, tzinfo=UTC))
    assert filters.created_from == datetime(2026, 10, 1, tzinfo=UTC)
    assert filters.created_to == datetime(2026, 10, 2, tzinfo=UTC)


class _NoDatabase:
    """A session no refused request may touch."""

    def __getattr__(self, name: str):
        raise AssertionError(f"the database was used ({name}) by a request refused before any read")


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    api = FastAPI()
    api.include_router(video_studio.router, prefix="/api/v1/video-studio")

    async def _db():
        yield _NoDatabase()

    api.dependency_overrides[deps.get_current_user] = lambda: User(
        email="samuel@filigran.io", hashed_password="x", full_name="Samuel", role="user"
    )
    api.dependency_overrides[get_db] = _db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as http:
        yield http


@pytest.mark.parametrize(
    ("body", "code"),
    [
        pytest.param({}, 400, id="nothing-named"),
        pytest.param({"items": []}, 400, id="no-items"),
        pytest.param({"items": [{"kind": "clip", "id": "not-a-uuid"}]}, 422, id="malformed-id"),
        pytest.param({"items": [{"kind": "music", "id": "00000000-0000-0000-0000-000000000001"}]}, 422, id="not-a-medium"),
        pytest.param({"all_matching_filters": True, "usage": "unsed"}, 422, id="usage-typo"),
        pytest.param({"all_matching_filters": True, "everything": True}, 422, id="unknown-field"),
    ],
)
async def test_a_bulk_deletion_naming_nothing_or_something_malformed_is_refused(client, body, code) -> None:
    answer = await client.post("/api/v1/video-studio/assets/bulk-delete", json=body)
    assert answer.status_code == code, answer.text
