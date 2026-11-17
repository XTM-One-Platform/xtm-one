"""Two attempts of one assignment run, against a real PostgreSQL.

The charge record on the run row is what fences an attempt's charge to that
attempt: taken off the row under the row lock before anything credits it. A
fake session cannot honestly exercise that lock, so here the attempt the engine
lost and the re-run that replaced it each hold a session of their own on one
scratch database (the recipe of ``tests/scratch_database.py``; skipped without
a reachable PostgreSQL whose role may create databases).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - every table on the metadata
from app.models.agent import Agent
from app.models.assignment import AssignmentRun
from app.models.user import User
from app.services.attempt_charges import (
    RecordedCharge,
    claim_charge_record,
    recorded_charge,
    release_charge_record,
    settle_lost_charge,
    take_lost_charge,
    with_recorded_charge,
)
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("charges", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _seed_run(sessions, *, status: str, trigger_context: dict) -> tuple[uuid.UUID, uuid.UUID]:
    async with sessions() as session:
        user = User(
            id=uuid.uuid4(),
            email=f"charges-{uuid.uuid4().hex[:8]}@test.local",
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
            trigger_context=trigger_context,
        )
        session.add(run)
        await session.commit()
        return run.id, user.id


async def _row_context(sessions, run_id: uuid.UUID) -> dict | None:
    async with sessions() as session:
        return (await session.get(AssignmentRun, run_id)).trigger_context


@pytest.mark.asyncio
async def test_a_stale_attempt_alive_cannot_credit_the_charge_its_rerun_took():
    async with _temp_database() as sessions:
        charged_user = uuid.uuid4()
        run_id, user_id = await _seed_run(
            sessions,
            status="running",
            trigger_context=with_recorded_charge(
                {"type": "schedule"}, charge_id="lost", user_id=charged_user, attempt=1
            ),
        )
        async with sessions() as stale_db, sessions() as rerun_db:
            # The attempt the engine lost is still running, on its own copy.
            stale_run = await stale_db.get(AssignmentRun, run_id)
            await stale_db.commit()

            rerun_run = await rerun_db.get(AssignmentRun, run_id)
            assert await take_lost_charge(rerun_db, rerun_run, attempt=2) == RecordedCharge(
                user_id=charged_user, charge_id="lost", attempt=1
            )
            # Credited: the record marked settling comes off.
            await settle_lost_charge(rerun_db, rerun_run, charge_id="lost")
            assert await claim_charge_record(
                rerun_db, rerun_run, charge_id="rerun", attempt=2, user_id=user_id, expected=None
            )

            # The stale attempt fails now. Its release waits for the row the
            # re-run holds until its "running" commit, then finds the re-run's
            # charge recorded, not its own.
            release = asyncio.create_task(
                release_charge_record(stale_db, stale_run, charge_id="lost")
            )
            await asyncio.sleep(0.5)
            assert not release.done(), "the row is locked until the re-run commits"
            await rerun_db.commit()
            assert await release is False
            await stale_db.commit()

        context = await _row_context(sessions, run_id)
        assert recorded_charge(context) == RecordedCharge(user_id=user_id, charge_id="rerun", attempt=2)
        assert context["type"] == "schedule"


@pytest.mark.asyncio
async def test_an_attempt_that_charges_after_its_replacement_recorded_nothing_overwrites():
    async with _temp_database() as sessions:
        run_id, user_id = await _seed_run(
            sessions, status="pending", trigger_context={"type": "schedule"}
        )
        async with sessions() as late_db, sessions() as rerun_db:
            late_run = await late_db.get(AssignmentRun, run_id)
            await late_db.commit()

            rerun_run = await rerun_db.get(AssignmentRun, run_id)
            assert await claim_charge_record(
                rerun_db, rerun_run, charge_id="rerun", attempt=2, user_id=user_id, expected=None
            )
            await rerun_db.commit()

            assert not await claim_charge_record(
                late_db, late_run, charge_id="late", attempt=1, user_id=user_id, expected=None
            )
            late_run.status = "running"
            await late_db.commit()

        assert recorded_charge(await _row_context(sessions, run_id)).charge_id == "rerun"


@pytest.mark.asyncio
async def test_the_dead_letter_credits_the_recorded_charge_once(monkeypatch):
    from app.services.orchestration_adapters.assignment import AssignmentAdapter
    from app.services.orchestration_adapters.base import AdapterContext

    async with _temp_database() as sessions:
        run_id, _user_id = await _seed_run(
            sessions,
            status="running",
            trigger_context=with_recorded_charge(
                {"type": "schedule"}, charge_id="owner", user_id=None, attempt=3
            ),
        )
        from app.services import quota

        credit = AsyncMock(return_value=quota.CHARGE_CREDIT_LANDED)
        monkeypatch.setattr("app.services.quota.credit_agentic_charge_once", credit)
        execution = SimpleNamespace(
            payload={"run_id": str(run_id)},
            last_error="Lease expired, last heartbeat 74 s ago, with no attempt left.",
            state="dead_letter",
            attempt_count=3,
            max_attempts=3,
            deadline_at=None,
        )

        for _ in range(2):
            async with sessions() as session:
                await AssignmentAdapter().on_dead_letter(
                    AdapterContext(db=session, worker_id="test"), execution
                )
                await session.commit()

        credit.assert_awaited_once()
        assert credit.await_args.args[1] is None
        async with sessions() as session:
            run = await session.get(AssignmentRun, run_id)
            assert run.status == "failed"
            assert recorded_charge(run.trigger_context) is None
        # The owner attempt, still alive, finds nothing of its own to credit.
        async with sessions() as owner_db:
            owner_run = await owner_db.get(AssignmentRun, run_id)
            assert await release_charge_record(owner_db, owner_run, charge_id="owner") is False
