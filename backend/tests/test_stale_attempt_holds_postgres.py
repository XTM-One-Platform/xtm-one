"""A first attempt and its re-run, against a real PostgreSQL.

A first attempt now checks the run under its lock before any write, like a
re-run. Whichever of the two gets the row first decides: a re-run that
recorded its place first makes the first attempt a replaced one, which writes
nothing; a first attempt that checked first holds the row until its first
commit (a hold here), and the re-run's fence waits for it. A fake session
cannot exercise that lock, so each attempt holds a session of its own on one
scratch database (skipped without a reachable PostgreSQL whose role may
create databases).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - every table on the metadata
from app.models.agent import Agent
from app.models.assignment import AssignmentRun
from app.models.user import User
from app.services import ai_hold
from app.services.assignment_engine import REPLACED_ATTEMPT, _ended_elsewhere
from app.services.attempt_charges import attempt_fence, fence_attempt
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("holds", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _seed_run(sessions, *, status: str) -> uuid.UUID:
    async with sessions() as session:
        user = User(
            id=uuid.uuid4(),
            email=f"holds-{uuid.uuid4().hex[:8]}@test.local",
            full_name="Run Owner",
            hashed_password="x",
        )
        session.add(user)
        await session.flush()
        agent = Agent(id=uuid.uuid4(), user_id=user.id, name="Worker", slug=f"w-{uuid.uuid4().hex[:6]}")
        session.add(agent)
        await session.flush()
        run = AssignmentRun(
            id=uuid.uuid4(),
            agent_id=agent.id,
            user_id=user.id,
            status=status,
            trigger_context={"type": "schedule"},
        )
        session.add(run)
        await session.commit()
        return run.id


async def _row(sessions, run_id: uuid.UUID) -> AssignmentRun:
    async with sessions() as session:
        return await session.get(AssignmentRun, run_id)


@pytest.mark.asyncio
async def test_a_first_attempt_replaced_before_its_check_writes_nothing():
    async with _temp_database() as sessions:
        run_id = await _seed_run(sessions, status="running")
        async with sessions() as stale_db, sessions() as rerun_db:
            # Attempt 1 loaded the run, then stalled past its lease.
            stale_run = await stale_db.get(AssignmentRun, run_id)
            await stale_db.commit()

            rerun_run = await rerun_db.get(AssignmentRun, run_id)
            assert await fence_attempt(rerun_db, rerun_run, attempt=2) == (0, 2)

            # Attempt 1 reaches its check before a hold: replaced.
            assert (
                await _ended_elsewhere(stale_db, stale_run, place=(0, 1), starting=True)
                == REPLACED_ATTEMPT
            )
            await stale_db.rollback()

        row = await _row(sessions, run_id)
        assert row.status == "running"
        assert attempt_fence(row.trigger_context) == (0, 2)
        assert ai_hold.held_code_of(row.trigger_context) is None


@pytest.mark.asyncio
async def test_a_first_attempt_that_checked_first_holds_the_row_until_its_hold_commits():
    async with _temp_database() as sessions:
        run_id = await _seed_run(sessions, status="pending")
        async with sessions() as first_db, sessions() as rerun_db:
            first_run = await first_db.get(AssignmentRun, run_id)
            assert (
                await _ended_elsewhere(first_db, first_run, place=(0, 1), starting=True)
                is None
            )

            rerun_run = await rerun_db.get(AssignmentRun, run_id)
            fence = asyncio.create_task(fence_attempt(rerun_db, rerun_run, attempt=2))
            await asyncio.sleep(0.5)
            assert not fence.done(), "the row is locked until the first attempt commits"

            # The live first attempt holds, as it always did.
            first_run.trigger_context = ai_hold.with_hold_marker(
                first_run.trigger_context, "ai_disabled"
            )
            await first_db.commit()

            assert await fence == (0, 2)
            # The re-run then starts from the held row, marker and all.
            assert await _ended_elsewhere(rerun_db, rerun_run, place=(0, 2), starting=True) is None
            await rerun_db.commit()

        row = await _row(sessions, run_id)
        assert row.status == "pending"
        assert ai_hold.held_code_of(row.trigger_context) == "ai_disabled"
        assert attempt_fence(row.trigger_context) == (0, 2)
