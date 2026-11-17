"""One carrier per red on main, against a real PostgreSQL (``payload.main_red``).

Two items filed for the same red under two correlation keys and two owners
resolve to one carrier; another red is filed apart; ``get_work_item`` of a
second carrier names the live run holding the first. Marked ``integration``:
CI's integration job is the one with a PostgreSQL service.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import update

import app.models  # noqa: F401 - every table, for ALL_TABLES
from app.models.agent import Agent
from app.models.assignment import AssignmentRun
from app.models.user import User
from app.models.work_item import WorkItem
from app.services import work_item as wi_service
from app.services.company_managed_writes import person_writer
from app.services.work_item_main_red import same_red_text
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

WRITER = person_writer(is_admin=False)
RED = {"repo": "XTM-One-Platform/xtm-one", "check": "Frontend unit tests", "test": "src/office/addin.test.ts"}


@asynccontextmanager
async def _database() -> AsyncIterator:
    async with scratch_database("wimainred", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _member(sessions) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"member-{uuid.uuid4().hex[:8]}@test.local",
        full_name="Member",
        hashed_password="x",
    )
    async with sessions() as db:
        db.add(user)
        await db.commit()
    return user


async def _create(user: User, title: str, **kwargs) -> dict:
    return await wi_service.create_work_item(user_id=str(user.id), title=title, writer=WRITER, **kwargs)


async def test_two_keys_for_one_red_resolve_to_one_carrier():
    async with _database() as sessions:
        user = await _member(sessions)
        first = await _create(
            user, "Office add-in red", correlation_key="dev:main-red-xtm-one", payload={"main_red": RED}
        )
        second = await _create(
            user,
            "Fix the add-in test on main",
            correlation_key="dev:office-addin-red",
            owner_agent_id=str(uuid.uuid4()),
            payload={"main_red": {**RED, "repo": "xtm-one-platform/XTM-ONE", "sha": "9ee057f3fc80"}},
        )
        assert second["id"] == first["id"]
        assert second["deduplicated"] is True and second["dedupe_by"] == "main_red"
        assert first["payload"]["main_red"]["key"] == (
            "xtm-one-platform/xtm-one|frontend unit tests|test:src/office/addin.test.ts"
        )

        # Another red is another item.
        other = await _create(
            user,
            "Backend red",
            correlation_key="dev:main-red-xtm-one-backend",
            payload={"main_red": {**RED, "check": "Backend unit tests", "test": "tests/test_x.py"}},
        )
        assert other["id"] != first["id"] and "deduplicated" not in other

        # Once the carrier is finished, the next filing is a new carrier.
        await wi_service.complete_work_item(first["id"], user_id=str(user.id), writer=WRITER)
        again = await _create(user, "Office add-in red again", correlation_key="dev:x", payload={"main_red": RED})
        assert again["id"] != first["id"] and "deduplicated" not in again


async def test_a_second_carrier_is_told_of_the_live_holder():
    async with _database() as sessions:
        user = await _member(sessions)
        # A carrier filed before the key existed (two carriers on one red).
        first = await _create(user, "Red A", correlation_key="dev:red-a", payload={"main_red": RED})
        second = await _create(user, "Red B", correlation_key="dev:red-b")
        agent = Agent(id=uuid.uuid4(), user_id=user.id, name="Desk", slug=f"desk-{uuid.uuid4().hex[:6]}")
        live = AssignmentRun(id=uuid.uuid4(), agent_id=agent.id, user_id=user.id, status="running")
        async with sessions() as db:
            db.add(agent)
            await db.flush()
            db.add(live)
            await db.flush()
            await db.execute(
                update(WorkItem)
                .where(WorkItem.id == uuid.UUID(first["id"]))
                .values(status="in_progress", claimed_by_run_id=live.id)
            )
            await db.execute(
                update(WorkItem)
                .where(WorkItem.id == uuid.UUID(second["id"]))
                .values(payload={"main_red": first["payload"]["main_red"]})
            )
            await db.commit()

        read = await wi_service.get_work_item(second["id"], user_id=str(user.id))
        assert read is not None
        [carrier] = read["same_red"]
        assert carrier["id"] == first["id"]
        assert carrier["claimed_by_run"]["id"] == str(live.id) and carrier["claimed_by_run"]["alive"] is True
        text = same_red_text(read["same_red"], (str(uuid.uuid4()),))
        assert text and "HELD BY ANOTHER RUN" in text

        # An item on no red carries no same_red.
        plain = await _create(user, "Plain", correlation_key="dev:plain")
        assert "same_red" not in (await wi_service.get_work_item(plain["id"], user_id=str(user.id)))
