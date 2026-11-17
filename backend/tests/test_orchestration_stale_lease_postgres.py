"""A transition decides on the row as it is under the lock.

The reconciler's own sequence: ``_do_execute`` loads the execution into its
session and commits (sessions are ``expire_on_commit=False``, and the loaded
instance stays referenced), the adapter runs, then ``_apply_result`` calls
``complete`` / ``fail`` / ``await_event`` on that SAME session. If the lease
lapsed meanwhile and another reconciler claimed the row, the transition must
see the row as it is under the lock - not the copy loaded before the work -
or it completes, re-queues or pauses a claim that is still running, and the
work runs twice. The other transitions whose caller already holds the row owe
the same: an outbox handler reads the execution to route a resume before
``rearm_for_resume`` locks it, and ``cancel_execution``'s callers look it up
before cancelling it. Only a real database shows what the identity map hands
back, so it is run here against PostgreSQL. Only the two orchestration tables
are created; same scratch-database recipe as
``test_assignment_stale_reference_postgres.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.models.orchestration import OrchestrationExecution, OrchestrationOutboxEvent
from app.services import orchestration_engine as engine
from tests.scratch_database import scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("stalelease", tables=[OrchestrationExecution.__table__, OrchestrationOutboxEvent.__table__]) as db:
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


async def _create(sessions, clock: _Clock) -> uuid.UUID:
    async with sessions() as db:
        execution = await engine.create_execution(
            db,
            adapter="assignment",
            payload={"run_id": str(uuid.uuid4())},
            deadline_at=clock.now + timedelta(seconds=3600),
        )
        await db.commit()
        return execution.id


async def _claim(sessions, worker: str) -> list[uuid.UUID]:
    async with sessions() as db:
        rows = await engine.claim_due(db, adapter="assignment", worker_id=worker, limit=10)
        ids = [row.id for row in rows]
        await db.commit()
    return ids


async def _row(sessions, execution_id: uuid.UUID) -> OrchestrationExecution:
    async with sessions() as db:
        return (
            await db.execute(
                select(OrchestrationExecution).where(OrchestrationExecution.id == execution_id)
            )
        ).scalar_one()


async def _apply(db, transition: str, execution_id: uuid.UUID, worker: str) -> None:
    """What ``_apply_result`` calls for each adapter result."""
    if transition == "complete":
        await engine.complete(db, execution_id=execution_id, worker_id=worker)
    elif transition == "fail_retryable":
        await engine.fail(
            db, execution_id=execution_id, worker_id=worker, error="boom", retryable=True
        )
    elif transition == "fail_final":
        await engine.fail(
            db, execution_id=execution_id, worker_id=worker, error="boom", retryable=False
        )
    else:
        await engine.await_event(
            db, execution_id=execution_id, worker_id=worker, adapter_state="awaiting_human"
        )


async def _pause(sessions, execution_id: uuid.UUID, worker: str) -> None:
    async with sessions() as db:
        await engine.await_event(
            db, execution_id=execution_id, worker_id=worker, adapter_state="awaiting"
        )
        await db.commit()


TRANSITIONS = ["complete", "fail_retryable", "fail_final", "await_event"]


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", TRANSITIONS)
async def test_a_worker_whose_lease_lapsed_cannot_apply_its_result(monkeypatch, transition):
    clock = _Clock()
    _install(monkeypatch, clock)
    async with _temp_database() as sessions:
        execution_id = await _create(sessions, clock)
        assert await _claim(sessions, "w1") == [execution_id]

        async with sessions() as w1:
            loaded = await engine.get_execution(w1, execution_id)
            await w1.commit()

            # w1's heartbeat misses the lease; the row is reclaimed and w2
            # starts the same work.
            clock.advance(settings.orchestration_lease_seconds + 1)
            async with sessions() as sweeper:
                assert await engine.reclaim_expired_leases(sweeper, limit=10) == []
                await sweeper.commit()
            assert await _claim(sessions, "w2") == [execution_id]

            # w1's session still holds the copy it loaded before the work.
            assert loaded.lease_owner == "w1"
            with pytest.raises(engine.LeaseLostError):
                await _apply(w1, transition, execution_id, "w1")
            await w1.rollback()

        row = await _row(sessions, execution_id)
        assert row.state == "running"
        assert row.lease_owner == "w2"


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", TRANSITIONS)
async def test_the_lease_holder_still_applies_its_result_on_its_loaded_session(
    monkeypatch, transition
):
    """The normal path through the same loaded session is unchanged."""
    clock = _Clock()
    _install(monkeypatch, clock)
    async with _temp_database() as sessions:
        execution_id = await _create(sessions, clock)
        assert await _claim(sessions, "w1") == [execution_id]

        async with sessions() as w1:
            loaded = await engine.get_execution(w1, execution_id)
            await w1.commit()
            await _apply(w1, transition, execution_id, "w1")
            await w1.commit()
            # The transition changed the very instance the session held.
            assert loaded.lease_owner is None

        row = await _row(sessions, execution_id)
        expected = {
            "complete": "completed",
            "fail_retryable": "pending",
            "fail_final": "failed",
            "await_event": "running",
        }[transition]
        assert row.state == expected
        assert row.lease_owner is None


@pytest.mark.asyncio
async def test_a_resume_routed_before_a_cancel_does_not_revive_the_run(monkeypatch):
    """The outbox handler reads the execution to route the resume; a cancel that
    lands before ``rearm_for_resume`` locks the row stands."""
    clock = _Clock()
    _install(monkeypatch, clock)
    async with _temp_database() as sessions:
        execution_id = await _create(sessions, clock)
        assert await _claim(sessions, "w1") == [execution_id]
        await _pause(sessions, execution_id, "w1")

        async with sessions() as handler:
            routed = await engine.get_execution(handler, execution_id)
            async with sessions() as operator:
                await engine.cancel_execution(operator, execution_id=execution_id)
                await operator.commit()

            assert routed.state == "running"
            await engine.rearm_for_resume(handler, execution_id=execution_id)
            await handler.commit()

        assert (await _row(sessions, execution_id)).state == "cancelled"


@pytest.mark.asyncio
async def test_a_resume_routed_before_another_resume_and_a_claim_is_not_ready(monkeypatch):
    """Another resume re-arms the run and w2 claims it before this handler's
    ``rearm_for_resume`` locks the row: w2's lease is live, so re-queueing the
    work w2 is running would run it twice."""
    clock = _Clock()
    _install(monkeypatch, clock)
    async with _temp_database() as sessions:
        execution_id = await _create(sessions, clock)
        assert await _claim(sessions, "w1") == [execution_id]
        await _pause(sessions, execution_id, "w1")

        async with sessions() as handler:
            routed = await engine.get_execution(handler, execution_id)
            async with sessions() as other:
                await engine.rearm_for_resume(other, execution_id=execution_id)
                await other.commit()
            assert await _claim(sessions, "w2") == [execution_id]

            assert routed.lease_owner is None
            with pytest.raises(engine.ResumeNotReadyError):
                await engine.rearm_for_resume(handler, execution_id=execution_id)
            await handler.rollback()

        row = await _row(sessions, execution_id)
        assert row.state == "running"
        assert row.lease_owner == "w2"


@pytest.mark.asyncio
async def test_a_resume_still_rearms_the_paused_run_it_routed(monkeypatch):
    """The normal resume path through the same session is unchanged."""
    clock = _Clock()
    _install(monkeypatch, clock)
    async with _temp_database() as sessions:
        execution_id = await _create(sessions, clock)
        assert await _claim(sessions, "w1") == [execution_id]
        await _pause(sessions, execution_id, "w1")

        async with sessions() as handler:
            routed = await engine.get_execution(handler, execution_id)
            await engine.rearm_for_resume(handler, execution_id=execution_id)
            await handler.commit()
            assert routed.state == "pending"

        row = await _row(sessions, execution_id)
        assert row.state == "pending"
        assert row.adapter_state == "awaiting"


@pytest.mark.asyncio
async def test_a_cancel_looked_up_before_a_completion_leaves_the_row_completed(monkeypatch):
    """``cancel_execution``'s callers look the execution up first (autonomous
    stop, sub-agent cancel and delete); a row that completed meanwhile is left
    as it is, not overwritten as cancelled."""
    clock = _Clock()
    _install(monkeypatch, clock)
    async with _temp_database() as sessions:
        execution_id = await _create(sessions, clock)
        assert await _claim(sessions, "w1") == [execution_id]

        async with sessions() as caller:
            found = await engine.get_execution(caller, execution_id)
            async with sessions() as w1:
                await engine.complete(w1, execution_id=execution_id, worker_id="w1")
                await w1.commit()

            assert found.state == "running"
            result = await engine.cancel_execution(caller, execution_id=execution_id)
            await caller.commit()
            assert result.state == "completed"

        assert (await _row(sessions, execution_id)).state == "completed"


@pytest.mark.asyncio
async def test_a_cancel_still_cancels_the_live_run_it_looked_up(monkeypatch):
    """The normal cancel path through the same session is unchanged."""
    clock = _Clock()
    _install(monkeypatch, clock)
    async with _temp_database() as sessions:
        execution_id = await _create(sessions, clock)
        assert await _claim(sessions, "w1") == [execution_id]

        async with sessions() as caller:
            found = await engine.get_execution(caller, execution_id)
            await engine.cancel_execution(caller, execution_id=execution_id)
            await caller.commit()
            assert found.state == "cancelled"

        row = await _row(sessions, execution_id)
        assert row.state == "cancelled"
        assert row.lease_owner is None
