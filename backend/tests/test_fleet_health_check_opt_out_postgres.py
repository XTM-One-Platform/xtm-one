"""A deleted seeded fleet health check is not re-created at the next start (#114).

Against a scratch PostgreSQL: the boot backfill gives the orchestrator its
weekly check, a person deletes it, and the backfill run again in a fresh
session (what a backend restart does) creates nothing. A new fleet still gets
the check, and a fleet put back to the default gets it again.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.fleet import Fleet, FleetMember
from app.models.user import User
from app.services.fleets import (
    FLEET_HEALTH_CHECK_NAME,
    backfill_fleet_health_advisory,
    record_health_check_deleted,
    set_fleet_health_check,
)
from app.services.workspaces import create_workspace
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("fhcheck", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _fleet(session, owner: User, slug: str) -> tuple[Fleet, Agent]:
    workspace = await create_workspace(session, user_id=owner.id, name=f"{slug} home")
    orchestrator = Agent(id=uuid.uuid4(), name=f"{slug} lead", user_id=owner.id)
    session.add(orchestrator)
    await session.flush()
    fleet = Fleet(
        user_id=owner.id,
        slug=slug,
        name=slug,
        workspace_id=workspace.id,
        orchestrator_agent_id=orchestrator.id,
    )
    session.add(fleet)
    await session.flush()
    session.add(FleetMember(fleet_id=fleet.id, agent_id=orchestrator.id, role="member"))
    await session.commit()
    return fleet, orchestrator


async def _checks(session, agent_id: uuid.UUID) -> list[Assignment]:
    return list(
        (
            await session.execute(
                select(Assignment).where(Assignment.agent_id == agent_id, Assignment.name == FLEET_HEALTH_CHECK_NAME)
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_a_deleted_check_stays_deleted_across_a_restart_and_new_fleets_still_get_one():
    async with _temp_database() as sessions:
        async with sessions() as session:
            owner = User(
                id=uuid.uuid4(),
                email=f"o-{uuid.uuid4().hex[:8]}@test.local",
                full_name="owner",
                hashed_password="x",
            )
            session.add(owner)
            await session.commit()
            fleet, lead = await _fleet(session, owner, "ops")
            await backfill_fleet_health_advisory(session)
            (check,) = await _checks(session, lead.id)
            # A person deletes it, as DELETE /assignments/{id} does.
            await record_health_check_deleted(session, check, actor=str(owner.id))
            await session.delete(check)
            await session.commit()

        # The restart: the boot backfill runs again in a fresh session.
        async with sessions() as session:
            await backfill_fleet_health_advisory(session)
            assert await _checks(session, lead.id) == []
            stored = await session.get(Fleet, fleet.id)
            assert stored.health_check["mode"] == "off"
            # A fleet created after the fix still gets the seeded check.
            _other, other_lead = await _fleet(session, owner, "intel")
            await backfill_fleet_health_advisory(session)
            assert len(await _checks(session, other_lead.id)) == 1

        # A second consecutive start: still no seeded check on the fleet that opted out.
        async with sessions() as session:
            await backfill_fleet_health_advisory(session)
            assert await _checks(session, lead.id) == []
            assert (await session.get(Fleet, fleet.id)).health_check["mode"] == "off"
            assert len(await _checks(session, other_lead.id)) == 1

        # Back to the default: the seeded check comes back, and stays across a restart.
        async with sessions() as session:
            stored = await session.get(Fleet, fleet.id)
            await set_fleet_health_check(session, stored, "default", actor=str(owner.id))
            await session.commit()
        async with sessions() as session:
            await backfill_fleet_health_advisory(session)
            assert len(await _checks(session, lead.id)) == 1
            assert (await session.get(Fleet, fleet.id)).health_check is None
