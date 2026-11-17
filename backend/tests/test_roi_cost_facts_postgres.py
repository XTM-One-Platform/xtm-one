"""ROI cost facts against real PostgreSQL (issue #12077, slice L2).

The AI cost of a day per person, attribution, agent, fleet and class: read live
from ``llm_traces``, closed once the day's rollup exists, closed from the rollup
when the traces are purged, closed again when the rollup's cost moves.
``integration``-marked and skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.llm_trace import LLMDailyStats, LLMTrace
from app.models.roi import RoiCostFact
from app.services import roi_ledger, trace_storage
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_DAY = date(2026, 11, 10)
_NOW = datetime(2026, 11, 12, 6, 0, tzinfo=UTC)


@asynccontextmanager
async def _session(prefix: str) -> AsyncIterator[AsyncSession]:
    async with (
        scratch_database(prefix, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        yield session


def _trace(source: str, cost: str, *, person: uuid.UUID | None, agent: uuid.UUID | None) -> LLMTrace:
    return LLMTrace(
        id=uuid.uuid4(),
        agent_id=agent,
        user_id=person,
        attributed_user_id=person,
        attribution="user" if person else "system",
        source=source,
        name="t",
        model="m",
        started_at=datetime.combine(_DAY, datetime.min.time(), tzinfo=UTC) + timedelta(hours=9),
        total_cost=Decimal(cost),
    )


async def _seed(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    alice, agent = uuid.uuid4(), uuid.uuid4()
    session.add_all(
        [
            _trace("assignment", "1.50", person=alice, agent=agent),
            _trace("assignment", "0.50", person=alice, agent=agent),
            _trace("agent-chat", "0.25", person=alice, agent=agent),
            _trace("general-chat", "0.10", person=alice, agent=None),
            _trace("memory", "0.05", person=None, agent=None),
        ]
    )
    await session.commit()
    return alice, agent


async def _facts(session: AsyncSession) -> list[RoiCostFact]:
    session.expire_all()
    return list((await session.execute(select(RoiCostFact).where(RoiCostFact.day == _DAY))).scalars())


async def test_the_live_read_groups_the_cost_by_person_agent_and_class() -> None:
    async with _session("roicfl") as session:
        alice, agent = await _seed(session)
        facts = await roi_ledger.cost_facts(session, first=_DAY, last=_DAY)
        by = {(f["user_id"], f["agent_id"], f["cost_class"]): (f["cost"], f["trace_count"]) for f in facts}
        assert by == {
            (alice, agent, "automation"): (Decimal("2.00"), 2),
            (alice, agent, "chat"): (Decimal("0.25"), 1),
            (alice, None, "chat"): (Decimal("0.10"), 1),
            (None, None, "platform"): (Decimal("0.05"), 1),
        }
        # The live read writes nothing.
        assert await _facts(session) == []


async def test_a_day_is_closed_once_its_rollup_exists_and_sums_to_it() -> None:
    async with _session("roicfc") as session:
        await _seed(session)
        # No rollup yet: nothing is due.
        assert (await roi_ledger.close_cost_days(session, now=_NOW)).rows == 0

        assert await trace_storage.aggregate_daily_stats(session, target_date=_DAY) > 0
        done = await roi_ledger.close_cost_days(session, now=_NOW)
        assert (done.from_traces, done.from_rollup, done.rows) == (1, 0, 4)
        facts = await _facts(session)
        assert sum(Decimal(f.cost) for f in facts) == Decimal("2.40")
        assert {f.built_from for f in facts} == {"traces"}

        # Closed: a second step changes nothing.
        again = await roi_ledger.close_cost_days(session, now=_NOW)
        assert (again.from_traces, again.from_rollup, again.rows) == (0, 0, 0)


async def test_a_purged_day_is_closed_from_its_rollup() -> None:
    async with _session("roicfr") as session:
        alice, agent = await _seed(session)
        assert await trace_storage.aggregate_daily_stats(session, target_date=_DAY) > 0
        await session.execute(delete(LLMTrace))
        await session.commit()

        done = await roi_ledger.close_cost_days(session, now=_NOW)
        assert (done.from_traces, done.from_rollup) == (0, 1)
        facts = await _facts(session)
        assert sum(Decimal(f.cost) for f in facts) == Decimal("2.40")
        assert {f.built_from for f in facts} == {"rollup"}
        automation = [f for f in facts if f.cost_class == "automation"]
        assert [(f.user_id, f.agent_id, Decimal(f.cost)) for f in automation] == [(alice, agent, Decimal("2.00"))]


async def test_a_repriced_day_is_closed_again() -> None:
    async with _session("roicfp") as session:
        await _seed(session)
        assert await trace_storage.aggregate_daily_stats(session, target_date=_DAY) > 0
        await roi_ledger.close_cost_days(session, now=_NOW)

        # A repricing moves the traces and the rollup together.
        await session.execute(update(LLMTrace).where(LLMTrace.source == "memory").values(total_cost=Decimal("0.45")))
        await session.execute(
            update(LLMDailyStats)
            .where(LLMDailyStats.date == _DAY, LLMDailyStats.source == "memory")
            .values(total_cost=Decimal("0.45"))
        )
        await session.commit()

        done = await roi_ledger.close_cost_days(session, now=_NOW)
        assert done.from_traces == 1
        facts = await _facts(session)
        assert sum(Decimal(f.cost) for f in facts) == Decimal("2.80")
        assert len(facts) == 4, "a day closed again holds one row per grain"
