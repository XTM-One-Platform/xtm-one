"""ROI ledger attribution against real PostgreSQL (issue #12077, slice L1).

Who a value fact counts for: the run's acting identity, else the assignment's
owner, else the agent's owner; a work item's person; ``unknown`` when the run
and item are gone. ``integration``-marked and skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.agent import Agent
from app.models.assignment import AssignmentRun
from app.models.roi import RoiChatRecord, RoiRunRecord
from app.models.user import User
from app.models.work_item import WorkItem
from app.services import roi_chat_records, roi_ledger, roi_records
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_SCHEMA = {"type": "object", "x-xtm-roi": {"unit": "cve_assessed", "count": "cves"}}


@asynccontextmanager
async def _session(prefix: str) -> AsyncIterator[AsyncSession]:
    async with (
        scratch_database(prefix, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        yield session


async def _user(session: AsyncSession, name: str) -> User:
    user = User(id=uuid.uuid4(), email=f"{name}-{uuid.uuid4().hex[:8]}@test.local", full_name=name, hashed_password="x")
    session.add(user)
    await session.flush()
    return user


async def _run(session: AsyncSession, agent: Agent, user_id: uuid.UUID | None) -> AssignmentRun:
    start = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
    run = AssignmentRun(
        id=uuid.uuid4(),
        assignment_id=None,
        agent_id=agent.id,
        user_id=user_id,
        status="completed",
        started_at=start,
        completed_at=start + timedelta(seconds=60),
        result_summary=json.dumps({"cves": [{"id": "CVE-1"}]}),
    )
    session.add(run)
    await session.flush()
    return run


async def test_a_run_record_counts_for_the_acting_person_else_the_agent_owner() -> None:
    async with _session("roiledrun") as session:
        owner = await _user(session, "owner")
        colleague = await _user(session, "colleague")
        agent = Agent(id=uuid.uuid4(), name="Shared analyst", user_id=owner.id)
        session.add(agent)
        await session.flush()

        # A colleague using the owner's shared agent: the units count for the colleague.
        used = await _run(session, agent, colleague.id)
        row = await roi_records.record_run_units(session, run=used, output_schema=_SCHEMA)
        assert row is not None and (row.user_id, row.attribution) == (colleague.id, "assignment")

        # A run with no acting identity and no assignment: the agent's owner.
        unowned = await _run(session, agent, None)
        row = await roi_records.record_run_units(session, run=unowned, output_schema=_SCHEMA)
        assert row is not None and (row.user_id, row.attribution) == (owner.id, "agent_owner")
        await session.commit()


async def test_a_work_item_record_counts_for_the_person_the_item_works_for() -> None:
    async with _session("roiledwi") as session:
        owner = await _user(session, "owner")
        asker = await _user(session, "asker")
        agent = Agent(id=uuid.uuid4(), name="Desk", user_id=owner.id)
        session.add(agent)
        await session.flush()
        item = WorkItem(id=uuid.uuid4(), user_id=asker.id, owner_agent_id=agent.id, title="t", status="done")
        session.add(item)
        await session.flush()

        await roi_records.put_declaration(
            session, subject_type="agent", subject_id=agent.id, unit="cve_assessed", count_mode="per_work_item"
        )
        row = await roi_records.record_work_item_units(session, work_item_id=item.id, agent_id=agent.id)
        assert row is not None and (row.user_id, row.attribution) == (asker.id, "work_item")

        # An item that no longer exists names nobody.
        assert await roi_ledger.person_of_work_item(session, work_item_id=uuid.uuid4(), agent_id=agent.id) == (
            None,
            "unknown",
        )
        await session.commit()


async def test_the_backfill_stamps_older_records_once_and_orphans_as_unknown() -> None:
    async with _session("roiledbf") as session:
        owner = await _user(session, "owner")
        actor = await _user(session, "actor")
        actor_id = actor.id
        agent = Agent(id=uuid.uuid4(), name="Analyst", user_id=owner.id)
        session.add(agent)
        await session.flush()
        run = await _run(session, agent, actor.id)
        # Rows as written before #12077: no person, no attribution.
        session.add_all(
            [
                RoiRunRecord(id=uuid.uuid4(), run_id=run.id, agent_id=agent.id, unit="cve_assessed", units=1),
                RoiRunRecord(id=uuid.uuid4(), run_id=None, agent_id=agent.id, unit="cve_assessed", units=2),
            ]
        )
        await session.flush()
        await session.commit()

        dry = await roi_ledger.run_ledger_backfill(session, dry_run=True)
        assert (dry.run_records, dry.orphan_records) == (1, 1)
        assert all(r.attribution is None for r in (await session.execute(select(RoiRunRecord))).scalars())

        done = await roi_ledger.run_ledger_backfill(session)
        assert (done.run_records, done.orphan_records) == (1, 1)
        session.expire_all()
        stamped = {r.units: (r.user_id, r.attribution) for r in (await session.execute(select(RoiRunRecord))).scalars()}
        assert stamped == {1: (actor_id, "assignment"), 2: (None, "unknown")}

        # Idempotent: a second pass finds nothing left.
        again = await roi_ledger.run_ledger_backfill(session)
        assert (again.run_records, again.work_item_records, again.orphan_records, again.chat_records) == (0, 0, 0, 0)


async def test_a_chat_record_carries_its_fleet_written_or_backfilled() -> None:
    from app.models.chat import Conversation

    async with _session("roiledchat") as session:
        owner = await _user(session, "owner")
        agent = Agent(id=uuid.uuid4(), name="Writer", user_id=owner.id)
        session.add(agent)
        await session.flush()
        conversation = Conversation(id=uuid.uuid4(), user_id=owner.id, agent_id=agent.id)
        session.add(conversation)
        await session.flush()

        assert await roi_chat_records.record_deliverable(
            session,
            kind="presentation",
            deliverable_id=uuid.uuid4(),
            conversation_id=conversation.id,
            user_id=owner.id,
            title="Deck",
            size=10,
        )
        session.add(RoiChatRecord(id=uuid.uuid4(), record_key="report:old", kind="report", deliverable_id="old"))
        await session.flush()
        await session.commit()

        rows = {r.kind: r for r in (await session.execute(select(RoiChatRecord))).scalars()}
        # Written now: stamped at the first write (no home fleet here: NULL), not backfilled.
        assert rows["presentation"].fleet_backfilled is False and rows["presentation"].fleet_id is None
        assert rows["report"].fleet_backfilled is None

        done = await roi_ledger.run_ledger_backfill(session)
        assert done.chat_records == 1
        session.expire_all()
        old = (await session.execute(select(RoiChatRecord).where(RoiChatRecord.kind == "report"))).scalar_one()
        assert old.fleet_backfilled is True
