"""Seeded health checks gated on a deleted fleet are repaired at boot (#1400).

Against a scratch PostgreSQL: a fleet absorbed (deleted) leaves its former
lead's weekly check gated on a missing id, ran or not. The boot backfill
re-points the check at the live fleet the agent leads, or deletes it (runs
kept) when the agent leads none, and cancels the open items those checks'
runs filed. A gate naming a fleet that does not exist is refused at save.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.fleet import Fleet, FleetMember
from app.models.user import User
from app.models.work_item import WorkItem
from app.services.fleets import (
    FLEET_HEALTH_CHECK_NAME,
    backfill_fleet_health_advisory,
    fleet_health_check_gate,
    gate_fleet_refusal,
)
from app.services.workspaces import create_workspace
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with scratch_database("fhorphan", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _owner(session: AsyncSession) -> User:
    owner = User(id=uuid.uuid4(), email=f"o-{uuid.uuid4().hex[:8]}@test.local", full_name="owner", hashed_password="x")
    session.add(owner)
    await session.commit()
    return owner


async def _fleet(session: AsyncSession, owner: User, slug: str, lead: Agent) -> Fleet:
    workspace = await create_workspace(session, user_id=owner.id, name=f"{slug} home")
    fleet = Fleet(user_id=owner.id, slug=slug, name=slug, workspace_id=workspace.id, orchestrator_agent_id=lead.id)
    session.add(fleet)
    await session.flush()
    session.add(FleetMember(fleet_id=fleet.id, agent_id=lead.id, role="member"))
    await session.commit()
    return fleet


async def _agent(session: AsyncSession, owner: User, name: str) -> Agent:
    agent = Agent(id=uuid.uuid4(), name=name, user_id=owner.id)
    session.add(agent)
    await session.commit()
    return agent


async def _orphaned_check(session: AsyncSession, agent: Agent, gone: uuid.UUID) -> tuple[Assignment, AssignmentRun]:
    """A check that ran on a fleet since deleted, with the run that filed items."""
    check = Assignment(
        agent_id=agent.id,
        user_id=agent.user_id,
        name=FLEET_HEALTH_CHECK_NAME,
        trigger_type="schedule",
        trigger_config={"cron": "0 7 * * 1"},
        prompt="old",
        activation_check=fleet_health_check_gate(gone),
        enabled=True,
    )
    session.add(check)
    await session.flush()
    run = AssignmentRun(
        id=uuid.uuid4(),
        assignment_id=check.id,
        agent_id=agent.id,
        user_id=agent.user_id,
        status="completed",
        started_at=datetime.now(UTC),
    )
    session.add(run)
    await session.commit()
    return check, run


async def _checks(session: AsyncSession, agent_id: uuid.UUID) -> list[Assignment]:
    rows = await session.execute(
        select(Assignment).where(Assignment.agent_id == agent_id, Assignment.name == FLEET_HEALTH_CHECK_NAME)
    )
    return list(rows.scalars().all())


@pytest.mark.asyncio
async def test_orphaned_checks_are_re_pointed_or_deleted_and_their_moot_items_cancelled() -> None:
    async with _temp_database() as sessions:
        async with sessions() as session:
            owner = await _owner(session)
            # A former desk lead that now leads the successor fleet, and one that leads nothing.
            successor_lead = await _agent(session, owner, "posture desk")
            idle_lead = await _agent(session, owner, "pir desk")
            successor = await _fleet(session, owner, "ctem-loop", successor_lead)
            gone_a, gone_b = uuid.uuid4(), uuid.uuid4()
            # The successor lead's check gates on its deleted desk fleet; the
            # backfill must not create a second one beside it.
            _check_a, run_a = await _orphaned_check(session, successor_lead, gone_a)
            check_b, run_b = await _orphaned_check(session, idle_lead, gone_b)
            keyed = WorkItem(user_id=owner.id, title="decide", correlation_key=f"fleet-health:{gone_b}:grant")
            keyed.needs_human = True
            ownerless = WorkItem(user_id=owner.id, title="grant a tool", origin_run_id=run_b.id)
            sibling = WorkItem(user_id=owner.id, title="posture finding", origin_run_id=run_a.id)
            unrelated = WorkItem(user_id=owner.id, title="keep me", correlation_key="fleet-health:other:x")
            session.add_all([keyed, ownerless, sibling, unrelated])
            await session.commit()
            ids = {"keyed": keyed.id, "ownerless": ownerless.id, "sibling": sibling.id, "unrelated": unrelated.id}

        async with sessions() as session:
            await backfill_fleet_health_advisory(session)

        async with sessions() as session:
            (kept,) = await _checks(session, successor_lead.id)
            assert (kept.activation_check or {})["arguments"]["fleet_id"] == str(successor.id)
            assert "could not read it" in kept.prompt
            assert await _checks(session, idle_lead.id) == []
            # The deleted check's run is kept as history.
            stored_run = await session.get(AssignmentRun, run_b.id)
            assert stored_run is not None and stored_run.assignment_id is None
            assert await session.get(Assignment, check_b.id) is None
            for name in ("keyed", "ownerless", "sibling"):
                item = await session.get(WorkItem, ids[name])
                assert item is not None and item.status == "cancelled", name
                assert "moot" in (item.result or "")
                assert item.needs_human is False
            other = await session.get(WorkItem, ids["unrelated"])
            assert other is not None and other.status != "cancelled"

        # A second start changes nothing.
        async with sessions() as session:
            await backfill_fleet_health_advisory(session)
            assert len(await _checks(session, successor_lead.id)) == 1
            assert await _checks(session, idle_lead.id) == []


@pytest.mark.asyncio
async def test_a_gate_on_a_fleet_that_does_not_exist_is_refused_at_save() -> None:
    async with _temp_database() as sessions, sessions() as session:
        owner = await _owner(session)
        lead = await _agent(session, owner, "lead")
        fleet = await _fleet(session, owner, "ops", lead)
        missing = uuid.uuid4()
        refusal = await gate_fleet_refusal(session, fleet_health_check_gate(missing))
        assert refusal is not None and str(missing) in refusal
        assert await gate_fleet_refusal(session, fleet_health_check_gate(fleet.id)) is None
        # A chained check is read too; a placeholder is left to the run.
        chained = {**fleet_health_check_gate(fleet.id), "also": [fleet_health_check_gate(missing)]}
        assert await gate_fleet_refusal(session, chained) is not None
        placeholder = {"tool": "get_fleet_health", "arguments": {"fleet_id": "{{payload.fleet_id}}"}}
        assert await gate_fleet_refusal(session, placeholder) is None
