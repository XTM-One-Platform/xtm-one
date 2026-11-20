"""A delegation's breaker write never holds the target row for the run.

After a failed send, the breaker counters used to be flushed by the calling
run's own session: the UPDATE took the target row lock inside the run's
transaction and kept it until the run committed. Every other delegation to
that partner that re-verified meanwhile claimed the row with ``SKIP LOCKED``,
gave up after the wait and was refused as ``verification_contended``.

The outcome is now committed in a short session of its own. A row another
session holds is written by a detached task once it is free, never by the
run's session, whose flush would otherwise take the lock for the rest of
the run. Same scratch-database recipe as ``test_a2a_client_verdict_contention_postgres.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.a2a.client import breaker, delegation, failures, registration
from app.models.a2a import OUTCOME_UNREACHABLE, A2AClientTarget
from tests.test_a2a_client_verdict_contention_postgres import _patch, _seed
from tests.test_a2a_hooks_postgres import _temp_database


async def _row_is_free(sm, target_id) -> bool:
    """Whether another session can lock the row right now (NOWAIT)."""
    from sqlalchemy.exc import DBAPIError

    async with sm() as other:
        try:
            await other.execute(
                select(A2AClientTarget.id).where(A2AClientTarget.id == target_id).with_for_update(nowait=True)
            )
        except DBAPIError:
            await other.rollback()
            return False
        await other.rollback()
        return True


async def _fail_once(caller, integration, target) -> None:
    """One transient failure concluded the way ``delegate`` concludes it."""
    breaker.record_failure(target, failures.TRANSIENT)
    await delegation._conclude(
        caller,
        integration,
        target,
        None,
        delegation.DelegationResult(outcome=OUTCOME_UNREACHABLE, detail="ConnectError"),
        submitted_at=datetime.now(UTC),
    )


@pytest.mark.integration
class TestBreakerWriteOutsideTheRun:
    @pytest.mark.asyncio
    async def test_a_failed_send_leaves_the_row_free_for_the_rest_of_the_run(self, monkeypatch):
        async with _temp_database() as sm:
            integration, target_id = await _seed(sm)
            _patch(monkeypatch, sm, [])
            async with sm() as caller:
                target = (
                    await caller.execute(select(A2AClientTarget).where(A2AClientTarget.id == target_id))
                ).scalar_one()
                await _fail_once(caller, integration, target)
                # The run goes on and flushes other work: nothing of the target.
                await caller.flush()
                assert not caller.dirty
                assert await _row_is_free(sm, target_id)
                # A second delegation to the same partner re-verifies at once.
                async with sm() as second:
                    other_target = (
                        await second.execute(select(A2AClientTarget).where(A2AClientTarget.id == target_id))
                    ).scalar_one()
                    await registration.reverify_before_delegating(integration, other_target)
                    await second.rollback()
                await caller.rollback()
            # Committed on its own: the run's rollback undid nothing.
            async with sm() as reader:
                row = (
                    await reader.execute(select(A2AClientTarget).where(A2AClientTarget.id == target_id))
                ).scalar_one()
                assert row.consecutive_transient_failures == 1
                assert row.last_failure_kind == failures.TRANSIENT.kind

    @pytest.mark.asyncio
    async def test_a_held_row_is_written_once_free_and_never_by_the_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(delegation, "_TARGET_OUTCOME_WAIT_SECONDS", 0.2)
        monkeypatch.setattr(delegation, "_TARGET_OUTCOME_POLL_SECONDS", 0.05)
        async with _temp_database() as sm:
            integration, target_id = await _seed(sm)
            _patch(monkeypatch, sm, [])
            async with sm() as holder, sm() as caller:
                # A verifier mid-write holds the row when the send concludes.
                await holder.execute(select(A2AClientTarget).where(A2AClientTarget.id == target_id).with_for_update())
                target = (
                    await caller.execute(select(A2AClientTarget).where(A2AClientTarget.id == target_id))
                ).scalar_one()
                await _fail_once(caller, integration, target)
                # Not left to the run: its next flush never waits for the
                # holder (the holder is still there), and never keeps the
                # lock for the rest of the run.
                await caller.flush()
                assert not caller.dirty
                assert len(delegation._BACKGROUND_TARGET_WRITES) == 1
                await holder.rollback()
                await delegation.drain_background_a2a()
                assert not delegation._BACKGROUND_TARGET_WRITES
                assert await _row_is_free(sm, target_id)
                await caller.rollback()
            async with sm() as reader:
                row = (
                    await reader.execute(select(A2AClientTarget).where(A2AClientTarget.id == target_id))
                ).scalar_one()
                assert row.consecutive_transient_failures == 1
                assert row.last_failure_kind == failures.TRANSIENT.kind
