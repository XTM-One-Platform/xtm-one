"""Retroactive ROI records (issue #10006, slice S1) against real PostgreSQL.

``integration``-marked and skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.agent import Agent
from app.models.assignment import AssignmentRun
from app.models.roi import RoiRunRecord
from app.models.user import User
from app.services import roi_backfill, roi_records
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_START = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


@asynccontextmanager
async def _session(prefix: str) -> AsyncIterator[AsyncSession]:
    async with (
        scratch_database(prefix, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        yield session


async def _agent_with_runs(session: AsyncSession, completed: int = 10, failed: int = 2):
    user = User(id=uuid.uuid4(), email=f"bf-{uuid.uuid4().hex[:8]}@test.local", full_name="B", hashed_password="x")
    session.add(user)
    await session.flush()
    agent = Agent(id=uuid.uuid4(), name="Developer desk", user_id=user.id)
    session.add(agent)
    await session.flush()
    runs = []
    for i in range(completed + failed):
        start = _START + timedelta(days=i)
        run = AssignmentRun(
            id=uuid.uuid4(),
            assignment_id=None,
            agent_id=agent.id,
            user_id=user.id,
            status="completed" if i < completed else "failed",
            started_at=start,
            completed_at=start + timedelta(seconds=60),
            result_summary=json.dumps({"prs": [{"n": 1}, {"n": 2}]}),
        )
        session.add(run)
        runs.append(run)
    await session.flush()
    return user, agent, runs


async def _records(session: AsyncSession, agent_id: uuid.UUID) -> list[RoiRunRecord]:
    return list((await session.execute(select(RoiRunRecord).where(RoiRunRecord.agent_id == agent_id))).scalars().all())


async def _declare(
    session, agent_id, *, unit="threat_report_triaged", mode=roi_records.COUNT_PER_RUN, path=None, per=1
):
    await roi_records.put_declaration(
        session,
        subject_type=roi_records.SUBJECT_AGENT,
        subject_id=agent_id,
        unit=unit,
        count_mode=mode,
        count_path=path,
        units_per_count=per,
    )


async def test_a_declaration_counts_every_past_completed_run_once():
    async with _session("roibf1") as session:
        _, agent, runs = await _agent_with_runs(session)
        await _declare(session, agent.id)

        preview = await roi_backfill.backfill_subject(
            session, subject_type=roi_records.SUBJECT_AGENT, subject_id=agent.id, dry_run=True
        )
        assert (preview.runs_scanned, preview.records_created, preview.units_added) == (10, 10, 10)
        # Nothing written by the preview.
        assert await _records(session, agent.id) == []

        now = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)
        done = await roi_backfill.backfill_subject(
            session, subject_type=roi_records.SUBJECT_AGENT, subject_id=agent.id, now=now
        )
        assert done.records_created == 10
        # No trace of these runs is kept: their cost reads 0, counted as not retained.
        assert done.cost_not_retained == 10
        rows = await _records(session, agent.id)
        assert len(rows) == 10
        assert {r.backfilled_at for r in rows} == {now}
        # Each record sits at its run's completion, so the windows place it in its own period.
        by_run = {r.id: r for r in runs}
        for row in rows:
            assert row.created_at == by_run[row.run_id].completed_at
            assert row.outcome is None

        again = await roi_backfill.backfill_subject(
            session, subject_type=roi_records.SUBJECT_AGENT, subject_id=agent.id
        )
        assert (again.records_created, again.records_recounted) == (0, 0)
        count = (
            await session.execute(
                select(func.count()).select_from(RoiRunRecord).where(RoiRunRecord.agent_id == agent.id)
            )
        ).scalar()
        assert count == 10


async def test_a_changed_declaration_recounts_and_a_reviewed_record_keeps_its_review():
    async with _session("roibf2") as session:
        user, agent, _ = await _agent_with_runs(session, completed=3, failed=0)
        await _declare(session, agent.id)
        await roi_backfill.backfill_subject(session, subject_type=roi_records.SUBJECT_AGENT, subject_id=agent.id)
        agent_id, user_id = agent.id, user.id
        reviewed = (await _records(session, agent_id))[0]
        reviewed_id = reviewed.id
        reviewed.outcome = "modified"
        reviewed.review_min = 7.0
        reviewed.reviewed_by_user_id = user_id
        await session.flush()

        # The desk now counts each PR of its structured output.
        await _declare(session, agent.id, mode=roi_records.COUNT_PER_OUTPUT_ITEM, path="prs")
        result = await roi_backfill.backfill_subject(
            session, subject_type=roi_records.SUBJECT_AGENT, subject_id=agent.id
        )
        assert (result.records_created, result.records_recounted, result.units_added) == (0, 3, 3)
        session.expire_all()
        rows = await _records(session, agent_id)
        assert sorted(r.units for r in rows) == [2, 2, 2]
        kept = next(r for r in rows if r.id == reviewed_id)
        assert (kept.outcome, kept.review_min, kept.reviewed_by_user_id) == ("modified", 7.0, user_id)


async def test_an_agent_without_a_declaration_gets_nothing():
    async with _session("roibf3") as session:
        _, agent, _ = await _agent_with_runs(session, completed=2, failed=0)
        result = await roi_backfill.backfill_subject(
            session, subject_type=roi_records.SUBJECT_AGENT, subject_id=agent.id
        )
        assert (result.runs_scanned, result.records_created) == (2, 0)
        assert await _records(session, agent.id) == []
