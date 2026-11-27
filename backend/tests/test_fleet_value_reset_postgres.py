"""A fleet's value is measured from its last factory reset (#2580), against real PostgreSQL.

The reset stamps ``fleets.value_measured_since``; the fleet report, the records list and the
spend of its agents then start at the mark, nothing deleted; a fleet never reset reads as
before. ``integration``-marked and skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.agent import Agent
from app.models.fleet import Fleet, FleetMember
from app.models.roi import RoiRunRecord
from app.models.user import User
from app.services import fleet_value_marks, roi_records, roi_report, roi_spend
from app.services.workspaces import create_workspace
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _session(prefix: str) -> AsyncIterator[AsyncSession]:
    async with (
        scratch_database(prefix, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        yield session


async def _fleet(session: AsyncSession, slug: str) -> tuple[Fleet, Agent, Agent]:
    owner = User(id=uuid.uuid4(), email=f"{slug}-{uuid.uuid4().hex[:8]}@test.local", full_name=slug, hashed_password="x")
    session.add(owner)
    await session.flush()
    lead = Agent(id=uuid.uuid4(), name=f"{slug} lead", user_id=owner.id)
    desk = Agent(id=uuid.uuid4(), name=f"{slug} desk", user_id=owner.id)
    session.add_all([lead, desk])
    await session.flush()
    workspace = await create_workspace(session, user_id=owner.id, name=f"{slug} home")
    fleet = Fleet(user_id=owner.id, slug=slug, name=slug, workspace_id=workspace.id, orchestrator_agent_id=lead.id)
    session.add(fleet)
    await session.flush()
    session.add_all(
        [
            FleetMember(fleet_id=fleet.id, agent_id=lead.id, role="member"),
            FleetMember(fleet_id=fleet.id, agent_id=desk.id, role="member"),
        ]
    )
    await session.flush()
    return fleet, lead, desk


def _record(agent: Agent, fleet: Fleet, at: datetime) -> RoiRunRecord:
    return RoiRunRecord(
        id=uuid.uuid4(), agent_id=agent.id, fleet_id=fleet.id, unit="cve_assessed", units=1, created_at=at
    )


async def test_records_before_the_mark_leave_the_fleet_value_and_stay_stored() -> None:
    now = datetime.now(UTC)
    async with _session("fleetvalrec") as session:
        fleet, lead, desk = await _fleet(session, "reset")
        other, other_lead, _ = await _fleet(session, "never")
        session.add_all(
            [
                _record(lead, fleet, now - timedelta(days=9)),
                _record(desk, fleet, now - timedelta(days=5)),
                _record(lead, fleet, now - timedelta(days=2)),
                _record(other_lead, other, now - timedelta(days=3)),
            ]
        )
        await session.flush()
        since = now - timedelta(days=30)
        assert await roi_records.count_records(session, fleet_id=fleet.id, since=since) == 3

        fleet.value_measured_since = now - timedelta(hours=1)
        await session.flush()
        assert await roi_records.count_records(session, fleet_id=fleet.id, since=since) == 0
        # A fleet never reset (mark NULL) reads as before.
        assert await roi_records.count_records(session, fleet_id=other.id, since=since) == 1

        session.add(_record(desk, fleet, now))
        await session.flush()
        assert await roi_records.count_records(session, fleet_id=fleet.id, since=since) == 1
        # Nothing is deleted.
        stored = await session.scalar(select(func.count()).select_from(RoiRunRecord))
        assert stored == 5

        marks = await fleet_value_marks.load_marks(session)
        assert set(marks) == {fleet.id}
        assert await fleet_value_marks.agent_marks(session, [lead.id, desk.id, other_lead.id], marks) == {
            lead.id: marks[fleet.id],
            desk.id: marks[fleet.id],
        }


async def test_the_spend_of_a_reset_fleet_agents_is_read_from_the_mark(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    window = now - timedelta(days=30)
    reads: list[tuple[datetime | None, frozenset[uuid.UUID] | None]] = []

    async def spend(_db, *, since, now=None, agent_ids=None):  # noqa: ANN001, ANN202
        reads.append((since, None if agent_ids is None else frozenset(agent_ids)))
        ids = agent_ids if agent_ids is not None else []
        # The whole window spends 10 per agent; from the mark, 1.
        return {aid: (10.0 if since == window else 1.0) for aid in ids}

    monkeypatch.setattr(roi_spend, "agent_spend", spend)
    async with _session("fleetvalspend") as session:
        fleet, lead, desk = await _fleet(session, "spend")
        other, other_lead, _ = await _fleet(session, "kept")
        mark = now - timedelta(hours=2)
        fleet.value_measured_since = mark
        await session.flush()
        before = {lead.id: 10.0, desk.id: 10.0, other_lead.id: 10.0}
        clipped = await roi_report.clip_spend_to_marks(
            session, before, since=window, now=now, agent_ids={lead.id, desk.id, other_lead.id}
        )
        assert clipped == {lead.id: 1.0, desk.id: 1.0, other_lead.id: 10.0}
        assert reads == [(mark, frozenset({lead.id, desk.id}))]

        # A mark older than the window changes nothing.
        reads.clear()
        fleet.value_measured_since = window - timedelta(days=1)
        await session.flush()
        assert await roi_report.clip_spend_to_marks(session, before, since=window, now=now, agent_ids=None) == before
        assert reads == []
