"""``roi_ledger.figures`` against real PostgreSQL (issue #12077, slice L3).

The facts of a window read from the three sources (run records, closed cost
days, live traces of the open days) and summed by ``roi_figures.compute``.
``integration``-marked and skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.llm_trace import LLMTrace
from app.models.roi import RoiCostFact, RoiRunRecord
from app.services import roi_figures, roi_ledger
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


@asynccontextmanager
async def _session(prefix: str) -> AsyncIterator[AsyncSession]:
    async with (
        scratch_database(prefix, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        yield session


async def test_figures_reads_records_closed_days_and_live_traces_once_each() -> None:
    async with _session("roifig") as session:
        alice, bob, agent = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        closed_day = (_NOW - timedelta(days=3)).date()
        session.add_all(
            [
                RoiRunRecord(
                    agent_id=None,
                    user_id=alice,
                    attribution="user",
                    unit="threat_report_triaged",
                    units=2,
                    created_at=_NOW - timedelta(days=2),
                ),
                RoiCostFact(
                    day=closed_day,
                    user_id=alice,
                    attribution="user",
                    agent_id=agent,
                    cost_class="automation",
                    cost=Decimal("1.50"),
                    trace_count=3,
                    built_from="traces",
                ),
                # An open day (after the newest closed one): read live from the traces.
                LLMTrace(
                    id=uuid.uuid4(),
                    agent_id=agent,
                    user_id=bob,
                    attributed_user_id=bob,
                    attribution="user",
                    source="agent-chat",
                    name="t",
                    model="m",
                    started_at=_NOW - timedelta(hours=2),
                    total_cost=Decimal("0.25"),
                ),
            ]
        )
        await session.commit()

        org = await roi_ledger.figures(
            session, scope="organization", viewer=roi_figures.Viewer(user_id=None, organization=True), now=_NOW
        )
        assert org.totals.runs == 1
        assert org.totals.cost == pytest.approx(1.75)
        assert org.totals.value_likely > 0

        mine = await roi_ledger.figures(session, scope="mine", viewer=roi_figures.Viewer(user_id=bob), now=_NOW)
        assert (mine.totals.runs, mine.totals.cost) == (0, pytest.approx(0.25))
        assert [r.key for r in mine.by_source] == ["chat"]
