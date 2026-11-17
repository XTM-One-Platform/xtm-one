"""The engine's ``defer`` step against real PostgreSQL.

A held execution goes round ``claim_due`` -> re-check -> ``defer`` for as long
as agentic AI is unavailable, with ``sweep_deadlines`` and
``reclaim_expired_leases`` running on every reconciler cycle beside it. What
matters is what the SQL does with the rows - ``FOR UPDATE SKIP LOCKED`` across
two reconcilers, the lease check under the row lock, the deadline the sweep
compares - so it is run here, not against a mock. The engine binds its own
clock, so a fake clock replays a week of cycles in seconds.

Only the two orchestration tables are created: the defer step touches nothing
else. Same scratch-database recipe as ``test_assignment_stale_reference_postgres.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.models.orchestration import OrchestrationExecution, OrchestrationOutboxEvent
from app.services import ai_hold
from app.services import orchestration_engine as engine
from tests.scratch_database import scratch_database

pytestmark = pytest.mark.integration

REFUSAL = "license_required"
BUDGET_SECONDS = 720.0
CLAIM_LAG_SECONDS = 1.5


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "aihold",
        tables=[OrchestrationExecution.__table__, OrchestrationOutboxEvent.__table__,],
        bind_app_sessions=False,
    ) as db:
        yield db.sessions


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _install(monkeypatch, clock: _Clock) -> None:
    class _ClockedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now if tz is None else clock.now.astimezone(tz)

    monkeypatch.setattr(engine, "datetime", _ClockedDatetime)


async def _create(sessions, clock: _Clock, *, count: int = 1) -> list[uuid.UUID]:
    ids = []
    async with sessions() as db:
        for _ in range(count):
            execution = await engine.create_execution(
                db,
                adapter="assignment",
                payload={"run_id": str(uuid.uuid4())},
                deadline_at=clock.now + timedelta(seconds=BUDGET_SECONDS),
            )
            ids.append(execution.id)
        await db.commit()
    return ids


async def _claim(sessions, worker: str, *, limit: int = 50) -> list[uuid.UUID]:
    async with sessions() as db:
        rows = await engine.claim_due(db, adapter="assignment", worker_id=worker, limit=limit)
        ids = [row.id for row in rows]
        await db.commit()
    return ids


async def _hold(sessions, execution_id: uuid.UUID, worker: str) -> None:
    async with sessions() as db:
        execution = await engine.get_execution(db, execution_id)
        result = ai_hold.hold_result(execution, REFUSAL)
        await engine.defer(
            db,
            execution_id=execution_id,
            worker_id=worker,
            reason=result.error,
            delay_seconds=result.backoff_seconds,
            adapter_state=result.adapter_state,
        )
        await db.commit()


async def _row(sessions, execution_id: uuid.UUID) -> OrchestrationExecution:
    async with sessions() as db:
        return (
            await db.execute(
                select(OrchestrationExecution).where(OrchestrationExecution.id == execution_id)
            )
        ).scalar_one()


async def _sweeps(sessions) -> list[uuid.UUID]:
    """The reconciler cycle's steps 1 + 2."""
    async with sessions() as db:
        dead = await engine.sweep_deadlines(db, limit=50)
        dead += await engine.reclaim_expired_leases(db, limit=50)
        ids = [row.id for row in dead]
        await db.commit()
    return ids


@pytest.mark.asyncio
async def test_two_reconcilers_claim_and_defer_disjoint_rows_and_count_no_attempt(monkeypatch):
    clock = _Clock()
    _install(monkeypatch, clock)
    async with _temp_database() as sessions:
        ids = set(await _create(sessions, clock, count=20))

        first, second = await asyncio.gather(
            _claim(sessions, "w1", limit=20), _claim(sessions, "w2", limit=20)
        )
        assert set(first).isdisjoint(second)
        assert set(first) | set(second) == ids

        # A worker cannot defer a row the other one holds.
        stolen = second[0] if second else first[0]
        thief = "w1" if second else "w2"
        with pytest.raises(engine.LeaseLostError):
            await _hold(sessions, stolen, thief)

        await asyncio.gather(
            *(_hold(sessions, i, "w1") for i in first),
            *(_hold(sessions, i, "w2") for i in second),
        )
        for execution_id in ids:
            row = await _row(sessions, execution_id)
            assert row.state == "pending" and row.lease_owner is None
            assert row.attempt_count == 0
            assert row.adapter_state == ai_hold.held_adapter_state(REFUSAL)
            assert engine.hold_record(row)["count"] == 1
            assert (row.deadline_at - row.run_at).total_seconds() == pytest.approx(
                BUDGET_SECONDS, abs=1
            )


@pytest.mark.asyncio
async def test_a_worker_whose_lease_lapsed_cannot_defer_and_nothing_is_stuck(monkeypatch):
    """Lost between the claim and the deferral: the lease lapses, the row is
    reclaimed and claimed by another reconciler, and the late deferral is
    refused without touching it. The next holder's deferral applies."""
    clock = _Clock()
    _install(monkeypatch, clock)
    async with _temp_database() as sessions:
        (execution_id,) = await _create(sessions, clock)
        assert await _claim(sessions, "w1") == [execution_id]

        clock.advance(settings.orchestration_lease_seconds + 1)
        assert await _sweeps(sessions) == []
        assert (await _row(sessions, execution_id)).state == "pending"
        assert await _claim(sessions, "w2") == [execution_id]

        with pytest.raises(engine.LeaseLostError):
            await _hold(sessions, execution_id, "w1")
        row = await _row(sessions, execution_id)
        assert row.state == "running" and row.lease_owner == "w2"
        assert engine.hold_record(row) is None

        await _hold(sessions, execution_id, "w2")
        row = await _row(sessions, execution_id)
        assert row.state == "pending" and row.lease_owner is None
        # w1's lost claim stays an attempt, w2's is given back.
        assert row.attempt_count == 1


@pytest.mark.asyncio
async def test_a_deferral_decides_on_the_row_as_it_is_now_not_as_it_was_loaded(monkeypatch):
    """The reconciler's own sequence: ``_do_execute`` loads the row into its
    session and commits (``expire_on_commit=False``), the adapter re-checks,
    then ``_apply_result`` defers on that SAME session. If the lease lapsed
    meanwhile and another reconciler claimed the row, the deferral must see
    the row as it is under the lock, not the copy loaded before the work -
    or it hands a live claim back to ``pending`` and the work runs twice."""
    clock = _Clock()
    _install(monkeypatch, clock)
    async with _temp_database() as sessions:
        (execution_id,) = await _create(sessions, clock)
        assert await _claim(sessions, "w1") == [execution_id]

        async with sessions() as w1:
            loaded = await engine.get_execution(w1, execution_id)
            await w1.commit()

            clock.advance(settings.orchestration_lease_seconds + 1)
            assert await _sweeps(sessions) == []
            assert await _claim(sessions, "w2") == [execution_id]

            result = ai_hold.hold_result(loaded, REFUSAL)
            with pytest.raises(engine.LeaseLostError):
                await engine.defer(
                    w1,
                    execution_id=execution_id,
                    worker_id="w1",
                    reason=result.error,
                    delay_seconds=result.backoff_seconds,
                    adapter_state=result.adapter_state,
                )
            await w1.rollback()

        row = await _row(sessions, execution_id)
        assert row.state == "running" and row.lease_owner == "w2"
        assert engine.hold_record(row) is None


@pytest.mark.asyncio
async def test_a_week_long_hold_is_never_dead_lettered_by_the_deadline_sweep(monkeypatch):
    clock = _Clock()
    _install(monkeypatch, clock)
    async with _temp_database() as sessions:
        (execution_id,) = await _create(sessions, clock)
        began = clock.now
        cycles = 0
        while True:
            assert await _sweeps(sessions) == [], "the sweep dead-lettered held work"
            assert await _claim(sessions, "w1") == [execution_id], (
                f"claim_due no longer reaches the held row after {cycles} cycles"
            )
            row = await _row(sessions, execution_id)
            if not ai_hold.hold_budget_left(row, now=clock.now):
                break
            await _hold(sessions, execution_id, "w1")
            row = await _row(sessions, execution_id)
            clock.advance((row.run_at - clock.now).total_seconds() + CLAIM_LAG_SECONDS)
            cycles += 1

        assert (clock.now - began).total_seconds() >= settings.ai_unavailable_hold_max_seconds
        assert cycles > 1000
        row = await _row(sessions, execution_id)
        assert row.state == "running"
        left = (row.deadline_at - clock.now).total_seconds()
        assert left >= BUDGET_SECONDS - CLAIM_LAG_SECONDS - 1


@pytest.mark.asyncio
async def test_blips_during_a_hold_never_dead_letter_it(monkeypatch):
    """A transient read retried through ``fail`` and a worker lost mid-check,
    three times each, around successful holds: the held row stays pending
    with its attempts intact."""
    clock = _Clock()
    _install(monkeypatch, clock)
    async with _temp_database() as sessions:
        (execution_id,) = await _create(sessions, clock)
        assert await _claim(sessions, "w1") == [execution_id]
        await _hold(sessions, execution_id, "w1")

        for blip in range(6):
            row = await _row(sessions, execution_id)
            clock.advance((row.run_at - clock.now).total_seconds() + CLAIM_LAG_SECONDS)
            assert await _claim(sessions, "w1") == [execution_id]
            if blip % 2:
                async with sessions() as db:
                    await engine.fail(
                        db,
                        execution_id=execution_id,
                        worker_id="w1",
                        error="not confirmed",
                        retryable=True,
                        backoff_seconds=30.0,
                    )
                    await db.commit()
            else:
                clock.advance(settings.orchestration_lease_seconds + 1)
                assert await _sweeps(sessions) == []
            row = await _row(sessions, execution_id)
            assert row.state == "pending", "a blip dead-lettered held work"
            clock.advance(max(0.0, (row.run_at - clock.now).total_seconds()) + 1)
            assert await _claim(sessions, "w1") == [execution_id]
            await _hold(sessions, execution_id, "w1")
            row = await _row(sessions, execution_id)
            assert row.state == "pending" and row.attempt_count == 0
