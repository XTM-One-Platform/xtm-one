"""completed_from returns exactly the items completed since T, newest first (#8558).

Against a real PostgreSQL (``tests/scratch_database.py``), as a non-admin: the
mapper stamps ``completed_at`` when an item reaches done, clears it when the
item is reopened, and ``list_work_items(status="done,cancelled",
completed_from=T, sort="completed_desc")`` keeps the item completed at T,
drops the one completed a microsecond before, and orders by completion.
Marked ``integration``: CI's integration job is the one with a PostgreSQL
service.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - every table, for ALL_TABLES
from app.models.user import User
from app.models.work_item import WorkItem
from app.services import work_item as wi_service
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

T = datetime(2026, 11, 4, 22, 0, tzinfo=UTC)


def _user() -> User:
    return User(id=uuid.uuid4(), email=f"m-{uuid.uuid4().hex[:8]}@test.local", full_name="M", hashed_password="x")


def _item(user: User, title: str, status: str, completed_at: datetime | None) -> WorkItem:
    return WorkItem(
        id=uuid.uuid4(),
        user_id=user.id,
        title=title,
        status=status,
        priority="medium",
        source="agent",
        completed_at=completed_at,
        created_at=T - timedelta(days=1),
    )


@pytest.mark.asyncio
async def test_completed_from_keeps_the_boundary_and_sorts_by_completion():
    async with scratch_database("wicomp", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        user = _user()
        before = _item(user, "before", "done", T - timedelta(microseconds=1))
        at = _item(user, "at", "done", T)
        later = _item(user, "later", "cancelled", T + timedelta(minutes=30))
        still_open = _item(user, "open", "open", None)
        async with scratch.sessions() as db:
            db.add(user)
            await db.flush()
            db.add_all([before, at, later, still_open])
            await db.commit()

        listed = await wi_service.list_work_items(
            user_id=str(user.id), status="done,cancelled", completed_from=T, sort="completed_desc"
        )
        assert [i["title"] for i in listed] == ["later", "at"]
        assert listed[1]["completed_at"] == T.isoformat()
        assert await wi_service.count_work_items(user_id=str(user.id), status="done,cancelled", completed_from=T) == 2
        bounded = await wi_service.list_work_items(
            user_id=str(user.id), completed_from=T, completed_to=T + timedelta(minutes=30)
        )
        assert [i["title"] for i in bounded] == ["at"]


@pytest.mark.asyncio
async def test_the_mapper_stamps_and_clears_completed_at():
    async with scratch_database("wicomp", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        user = _user()
        item = _item(user, "work", "in_progress", None)
        async with scratch.sessions() as db:
            db.add(user)
            await db.flush()
            db.add(item)
            await db.commit()
        async with scratch.sessions() as db:
            row = (await db.execute(select(WorkItem).where(WorkItem.id == item.id))).scalar_one()
            row.status = "done"
            await db.commit()
            assert row.completed_at is not None
            row.status = "open"
            await db.commit()
            assert row.completed_at is None
