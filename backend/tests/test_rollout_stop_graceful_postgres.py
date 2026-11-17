"""The deadline hold repair writes no word for a leased row's holder, on a real database.

``OrchestrationExecution.updated_at`` has an ``onupdate``: any UPDATE of the row
that does not set the column stamps it with the time of the flush. On a leased
``running`` row that column is when its holder was last heard from
(``orchestration_engine.last_heard_at``), which the take-back of workers gone
compares with the holder's last presence word. The hold repair of
``sweep_deadlines`` must leave it as the holder wrote it, through the flush;
only PostgreSQL shows what the UPDATE carries.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.orchestration import OrchestrationExecution, OrchestrationOutboxEvent
from app.services import orchestration_engine as engine
from tests.scratch_database import scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    tables = [OrchestrationExecution.__table__, OrchestrationOutboxEvent.__table__]
    async with scratch_database("rolloutstop", tables=tables) as db:
        yield db.sessions


def _held(now: datetime) -> dict:
    return {
        engine.HOLD_PAYLOAD_KEY: {
            "since": (now - timedelta(minutes=1)).isoformat(),
            "count": 1,
            "reason": "held:ai_disabled",
            "budget_seconds": 600.0,
        }
    }


async def test_the_hold_repair_keeps_a_leased_rows_updated_at_through_the_flush():
    now = datetime.now(UTC)
    heard = now - timedelta(seconds=40)
    async with _temp_database() as sessions:
        async with sessions() as db:
            leased = OrchestrationExecution(
                adapter="sub_agent", state="running", payload=_held(now), lease_owner="worker-old",
                lease_expires_at=now + timedelta(seconds=50), deadline_at=now - timedelta(minutes=5),
                updated_at=heard,
            )
            unleased = OrchestrationExecution(
                adapter="sub_agent", state="pending", payload=_held(now), deadline_at=now - timedelta(minutes=5),
                updated_at=heard,
            )
            db.add_all([leased, unleased])
            await db.commit()
            ids = (leased.id, unleased.id)

        async with sessions() as db:
            assert await engine.sweep_deadlines(db, limit=10) == []
            await db.commit()

        async with sessions() as db:
            rows = {row.id: row for row in (await db.execute(select(OrchestrationExecution))).scalars().all()}

    kept, stamped = rows[ids[0]], rows[ids[1]]
    assert kept.deadline_at > now and stamped.deadline_at > now
    assert kept.updated_at == heard, "the reclaimer's repair read as a renewal by the gone holder"
    assert stamped.updated_at > heard
    word = {"at": (now - timedelta(seconds=20)).isoformat()}
    assert not engine._renewed_since_its_word(kept, word, datetime.now(UTC))


async def test_a_gone_workers_row_past_its_deadline_is_left_to_the_deadline_sweep():
    # The take-back of workers gone runs first in the cycle; a row past its hard deadline still dead-letters on
    # its deadline, with no lease-expiry record attached.
    now = datetime.now(UTC)
    async with _temp_database() as sessions:
        async with sessions() as db:
            overdue = OrchestrationExecution(
                adapter="sub_agent", state="running", payload={}, lease_owner="worker-old",
                lease_expires_at=now - timedelta(seconds=10), deadline_at=now - timedelta(minutes=1),
            )
            on_time = OrchestrationExecution(
                adapter="sub_agent", state="running", payload={}, lease_owner="worker-old",
                lease_expires_at=now - timedelta(seconds=10), deadline_at=now + timedelta(minutes=30),
                attempt_count=1, max_attempts=4,
            )
            db.add_all([overdue, on_time])
            await db.commit()
            ids = (overdue.id, on_time.id)

        gone = {"worker-old": {"state": "stopped", "lapsed": True}}
        async with sessions() as db:
            await engine.reclaim_from_gone_owners(db, gone, adapters=["sub_agent"], limit=10)
            dead = await engine.sweep_deadlines(db, limit=10)
            await db.commit()

        async with sessions() as db:
            rows = {row.id: row for row in (await db.execute(select(OrchestrationExecution))).scalars().all()}

    assert [row.id for row in dead] == [ids[0]]
    assert rows[ids[0]].state == "dead_letter" and rows[ids[0]].last_error.startswith("Deadline exceeded")
    assert rows[ids[1]].state == "pending" and rows[ids[1]].lease_owner is None
