"""Wait cycles refused at the write and broken by the sweep, on a real PostgreSQL (#606).

As a non-admin: A blocked on B, then B blocked on A is refused with the cycle
named; a three-item cycle likewise; a fix item waiting on pull request
owner/name#12 and a review item about #12 waiting on the fix: the second write
is refused; a cycle forced in the database is released by one sweep pass, with
one note and exactly one ``work_item.wait_cycle`` event; a second pass does
nothing. Marked ``integration``: CI's integration job has the PostgreSQL service.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

import app.models  # noqa: F401 - every table, for ALL_TABLES
from app.models.user import User
from app.models.work_item import WorkItem
from app.services import platform_events
from app.services import work_item as wi_service
from app.services import work_item_wait_cycles as cycles
from app.services.company_managed_writes import person_writer
from tests.scratch_database import ALL_TABLES, scratch_database
from tests.work_item_owner import add_owner, owner_of

pytestmark = pytest.mark.integration

WRITER = person_writer(is_admin=False)
PR = {"repo": "owner/name", "kind": "pull_request", "number": 12, "release_on": "merged_or_closed"}


@asynccontextmanager
async def _database() -> AsyncIterator:
    async with scratch_database("wicycles", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _member(sessions) -> User:
    user = User(id=uuid.uuid4(), email=f"m-{uuid.uuid4().hex[:8]}@test.local", full_name="M", hashed_password="x")
    async with sessions() as db:
        db.add(user)
        await db.commit()
    await add_owner(sessions, user.id)
    return user


async def _create(user: User, title: str, **kwargs) -> dict:
    kwargs.setdefault("owner_agent_id", owner_of(user.id))
    return await wi_service.create_work_item(user_id=str(user.id), title=title, writer=WRITER, **kwargs)


async def _update(user: User, item_id: str, **kwargs) -> dict:
    return await wi_service.update_work_item(item_id, user_id=str(user.id), writer=WRITER, **kwargs)


async def test_two_and_three_item_cycles_are_refused_with_the_cycle_named():
    async with _database() as sessions:
        user = await _member(sessions)
        b = await _create(user, "b")
        a = await _create(user, "a", blocked_on=[b["id"]])
        with pytest.raises(ValueError, match="closes a cycle") as refused:
            await _update(user, b["id"], blocked_on=[a["id"]])
        assert a["id"] in str(refused.value) and "review item blocks on the fix item" in str(refused.value)
        assert (await wi_service.get_work_item(b["id"], user_id=str(user.id)))["status"] == "open"

        c = await _create(user, "c", blocked_on=[a["id"]])
        with pytest.raises(ValueError, match="closes a cycle"):
            await _update(user, b["id"], blocked_on=[c["id"]])


async def test_a_review_gating_the_fix_pull_request_cannot_wait_on_the_fix():
    async with _database() as sessions:
        user = await _member(sessions)
        fix = await _create(user, "fix", blocked_on_refs=[PR])
        assert fix["status"] == "blocked"
        with pytest.raises(ValueError, match="owner/name#12"):
            await _create(user, "UX review: owner/name#12", correlation_key="ux:name-12", blocked_on=[fix["id"]])

        # The other order: the review first, then the fix closing the loop.
        review = await _create(user, "UX review: owner/name#12 again", correlation_key="ux:name-12")
        other_fix = await _create(user, "other fix")
        await _update(user, review["id"], blocked_on=[other_fix["id"]])
        with pytest.raises(ValueError, match="closes a cycle"):
            await _update(user, other_fix["id"], blocked_on_refs=[PR])

        # The recommended model passes: the review waits on the fix item, nothing waits on the merge.
        await _create(user, "UX review of the fix", correlation_key="ux:name-13", blocked_on=[other_fix["id"]])


async def test_a_cycle_forced_in_the_database_is_released_once_by_the_sweep(monkeypatch):
    published: list[tuple[str, dict]] = []

    async def publish(name, data, *, event_id=None):
        published.append((name, dict(data)))
        return event_id

    monkeypatch.setattr(platform_events, "publish", publish)
    async with _database() as sessions:
        user = await _member(sessions)
        a = await _create(user, "a")
        b = await _create(user, "b", blocked_on=[a["id"]])
        now = datetime.now(UTC)
        async with sessions() as db:
            # Written around the check, as a row older than it or a race would be.
            await db.execute(
                update(WorkItem)
                .where(WorkItem.id == uuid.UUID(a["id"]))
                .values(status="blocked", blocked_on=[b["id"]], last_activity_at=now - timedelta(minutes=5))
            )
            await db.execute(
                update(WorkItem)
                .where(WorkItem.id == uuid.UUID(b["id"]))
                .values(last_activity_at=now - timedelta(hours=6))
            )
            await db.commit()

        assert await cycles.release_wait_cycles() == 1
        released = await wi_service.get_work_item(a["id"], user_id=str(user.id))
        assert released["status"] == "open" and released["blocked_on"] == []
        assert released["claim_notes"][-1]["text"].startswith("released from a wait cycle:")
        events = [data for name, data in published if name == "work_item.wait_cycle"]
        assert len(events) == 1
        assert events[0]["cycle"]["items"] == sorted([a["id"], b["id"]])
        assert events[0]["cycle"]["released"] == a["id"]

        assert await cycles.release_wait_cycles() == 0
        assert len([1 for name, _ in published if name == "work_item.wait_cycle"]) == 1
