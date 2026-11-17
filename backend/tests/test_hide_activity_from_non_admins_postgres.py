"""The option that hides a company-managed agent's or fleet's activity from non-admins (#11624).

Off by default: a non-admin still sees the company-managed runs, traces, work
items and shared state exactly as before. On (on the agent, or on its home
fleet): a non-admin sees none of them, the owner and an administrator see them
all, and the rows filed in the fleet's workspace leave the non-admin's tiers.
Read against a scratch PostgreSQL, through the four single sources of truth.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.api.v1.helpers import run_visible_filter
from app.models.agent import Agent
from app.models.assignment import AssignmentRun
from app.models.fleet import Fleet, FleetMember
from app.models.llm_trace import LLMTrace
from app.models.shared_state import SharedState
from app.models.user import User
from app.models.work_item import WorkItem
from app.models.workspace import Workspace
from app.services import shared_state as shared_state_service
from app.services import work_item as work_item_service
from app.services.tracing.read_store import trace_visible_query
from app.services.workspaces import create_workspace
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("hideact", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _user(session, name: str, *, role: str = "user") -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{name}-{uuid.uuid4().hex[:8]}@test.local",
        full_name=name,
        hashed_password="x",
        role=role,
    )
    session.add(user)
    await session.flush()
    return user


async def _world(session) -> tuple[User, User, User, Agent, Fleet, Workspace]:
    """An owner's company-managed agent and fleet, with one row of each kind."""
    owner = await _user(session, "owner")
    reader = await _user(session, "reader")
    admin = await _user(session, "admin", role="admin")
    workspace = await create_workspace(session, user_id=owner.id, name="Fleet home")
    solo = Agent(id=uuid.uuid4(), name="solo", user_id=owner.id, is_company_managed=True)
    member = Agent(id=uuid.uuid4(), name="member", user_id=owner.id, is_company_managed=True)
    session.add_all([solo, member])
    await session.flush()
    fleet = Fleet(
        user_id=owner.id, slug="ops", name="Ops", workspace_id=workspace.id, is_company_managed=True
    )
    session.add(fleet)
    await session.flush()
    session.add(FleetMember(fleet_id=fleet.id, agent_id=member.id, role="member"))
    now = datetime.now(UTC)
    for agent in (solo, member):
        session.add(
            AssignmentRun(
                id=uuid.uuid4(),
                agent_id=agent.id,
                user_id=owner.id,
                status="completed",
                is_company_managed=True,
                started_at=now,
                trigger_payload={},
            )
        )
        session.add(
            LLMTrace(
                id=uuid.uuid4(),
                agent_id=agent.id,
                user_id=owner.id,
                source="assignment",
                name="run",
                started_at=now,
            )
        )
        session.add(
            WorkItem(title=f"{agent.name} item", is_company_managed=True, owner_agent_id=agent.id)
        )
        session.add(
            SharedState(key=f"{agent.name}:state", is_company_managed=True, owner_agent_id=agent.id)
        )
    # Filed in the fleet's workspace, by nobody's agent in particular.
    session.add(WorkItem(title="filed", workspace_id=workspace.id, user_id=owner.id))
    session.add(SharedState(key="filed", workspace_id=workspace.id, user_id=owner.id))
    await session.commit()
    return owner, reader, admin, solo, fleet, workspace


async def _seen(session, user: User, workspace_ids) -> dict[str, int]:
    runs = await session.scalar(
        select(func.count()).select_from(AssignmentRun).where(run_visible_filter(user.id))
    )
    if user.is_admin:
        runs = await session.scalar(select(func.count()).select_from(AssignmentRun))
    traces = await session.scalar(
        select(func.count()).select_from(trace_visible_query(user, []).subquery())
    )
    items = await session.scalar(
        select(func.count())
        .select_from(WorkItem)
        .where(work_item_service.work_item_visible_clause(user.id, workspace_ids))
    )
    states = await session.scalar(
        select(func.count())
        .select_from(SharedState)
        .where(shared_state_service.shared_state_visible_clause(user.id, workspace_ids))
    )
    return {"runs": runs, "traces": traces, "items": items, "states": states}


EVERYTHING = {"runs": 2, "traces": 2, "items": 3, "states": 3}


@pytest.mark.asyncio
async def test_flag_off_by_default_changes_nothing():
    async with _temp_database() as sessions, sessions() as session:
        owner, reader, admin, solo, fleet, workspace = await _world(session)
        assert solo.hide_activity_from_non_admins is False
        assert fleet.hide_activity_from_non_admins is False
        for user in (reader, owner, admin):
            assert await _seen(session, user, [workspace.id]) == EVERYTHING


@pytest.mark.asyncio
async def test_agent_flag_hides_its_activity_from_a_non_admin_only():
    async with _temp_database() as sessions, sessions() as session:
        owner, reader, admin, solo, _fleet, workspace = await _world(session)
        await session.execute(
            update(Agent).where(Agent.id == solo.id).values(hide_activity_from_non_admins=True)
        )
        await session.commit()
        assert await _seen(session, reader, [workspace.id]) == {
            "runs": 1,
            "traces": 1,
            "items": 2,
            "states": 2,
        }
        assert await _seen(session, owner, [workspace.id]) == EVERYTHING
        assert await _seen(session, admin, [workspace.id]) == EVERYTHING


@pytest.mark.asyncio
async def test_fleet_flag_hides_members_and_its_workspace():
    async with _temp_database() as sessions, sessions() as session:
        owner, reader, admin, _solo, fleet, workspace = await _world(session)
        await session.execute(
            update(Fleet).where(Fleet.id == fleet.id).values(hide_activity_from_non_admins=True)
        )
        await session.commit()
        # Only the solo agent's activity is left: the member's and the filed rows go.
        assert await _seen(session, reader, [workspace.id]) == {
            "runs": 1,
            "traces": 1,
            "items": 1,
            "states": 1,
        }
        assert await _seen(session, owner, [workspace.id]) == EVERYTHING
        assert await _seen(session, admin, [workspace.id]) == EVERYTHING


@pytest.mark.asyncio
async def test_own_rows_are_never_hidden():
    async with _temp_database() as sessions, sessions() as session:
        _owner, reader, _admin, solo, _fleet, workspace = await _world(session)
        session.add(
            AssignmentRun(
                id=uuid.uuid4(),
                agent_id=solo.id,
                user_id=reader.id,
                status="completed",
                is_company_managed=True,
                started_at=datetime.now(UTC),
                trigger_payload={},
            )
        )
        await session.execute(
            update(Agent).where(Agent.id == solo.id).values(hide_activity_from_non_admins=True)
        )
        await session.commit()
        seen = await _seen(session, reader, [workspace.id])
        # The member's company run, plus the reader's own run of the hidden agent.
        assert seen["runs"] == 2
