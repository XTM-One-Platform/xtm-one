"""Tests for ``app.core.cancellation`` cleanup guards.

Two complementary primitives are covered:

* ``uncancel_during_cleanup`` — the uncancel/recancel context manager used by
  the MCP client teardown. It is meant for the *already-delivered* case: the
  caller has caught ``CancelledError`` and re-raises after cleanup, so
  ``cancelling() > 0`` while the cleanup runs.

* ``run_cleanup_to_completion`` — runs a cleanup coroutine on a shielded child
  task. It guarantees completion even under a *requested-but-undelivered*
  cancellation, which is the production failure mode (the
  asyncpg ROLLBACK inside ``session.close()`` raising ``CancelledError``).
"""

import asyncio
import contextlib

import pytest

from app.core.cancellation import (
    run_cleanup_to_completion,
    uncancel_during_cleanup,
)

# ---------------------------------------------------------------------------
# uncancel_during_cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uncancel_noop_when_not_cancelled():
    """With no pending cancellation, the body runs and nothing is injected."""
    ran: list[bool] = []

    async with uncancel_during_cleanup():
        await asyncio.sleep(0)
        ran.append(True)

    task = asyncio.current_task()
    assert ran == [True]
    assert task is not None
    assert task.cancelling() == 0


@pytest.mark.asyncio
async def test_uncancel_restores_single_cancellation():
    """A delivered cancellation is lifted inside the block and restored after."""
    counts: list[int] = []

    async def _runner() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            # cancelling() > 0, but the cancel has been delivered already.
            async with uncancel_during_cleanup():
                counts.append(asyncio.current_task().cancelling())  # type: ignore[union-attr]
                await asyncio.sleep(0)  # completes — delivery was consumed
            counts.append(asyncio.current_task().cancelling())  # type: ignore[union-attr]
            raise

    task = asyncio.create_task(_runner())
    await asyncio.sleep(0)
    task.cancel()
    done, _ = await asyncio.wait([task])
    [completed] = done

    assert completed.cancelled()
    assert counts == [0, 1], "lifted to 0 inside the block, restored to 1 after"


@pytest.mark.asyncio
async def test_uncancel_restores_multiple_cancellations():
    """Multiple stacked cancellations are all restored after the block."""
    counts: list[int] = []

    async def _runner() -> None:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        task.cancel()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(0)  # deliver one; cancelling() stays 3
        async with uncancel_during_cleanup():
            counts.append(task.cancelling())  # lifted to 0
            await asyncio.sleep(0)
        counts.append(task.cancelling())  # restored to 3

    task = asyncio.create_task(_runner())
    done, _ = await asyncio.wait([task])
    [completed] = done

    assert completed.cancelled()
    assert counts == [0, 3], "all three lifted cancellations must be re-applied"


@pytest.mark.asyncio
async def test_uncancel_recancel_runs_even_when_body_raises():
    """If the body raises, the cancellation is still restored on the way out."""
    observed: list[int] = []

    async def _runner() -> None:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        try:
            async with uncancel_during_cleanup():
                raise RuntimeError("boom")  # no await — _must_cancel irrelevant
        except RuntimeError:
            observed.append(task.cancelling())
            raise

    task = asyncio.create_task(_runner())
    done, _ = await asyncio.wait([task])
    [completed] = done

    assert observed == [1], "cancellation restored before the RuntimeError unwinds"
    # A non-cancellation exception propagating synchronously leaves the task
    # failed (the restored cancel has no checkpoint to be delivered at).
    assert not completed.cancelled()
    assert isinstance(completed.exception(), RuntimeError)


# ---------------------------------------------------------------------------
# run_cleanup_to_completion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_cleanup_returns_value_when_not_cancelled():
    """Happy path: the cleanup runs and its return value is returned."""
    ran: list[bool] = []

    async def _cleanup() -> str:
        await asyncio.sleep(0)
        ran.append(True)
        return "done"

    result = await run_cleanup_to_completion(_cleanup())

    assert ran == [True]
    assert result == "done"


@pytest.mark.asyncio
async def test_run_cleanup_completes_under_undelivered_cancel():
    """The cleanup must finish even when a cancel is pending but undelivered.

    This is the precise scenario: a plain ``await`` would raise
    ``CancelledError`` on its first checkpoint, abandoning the cleanup.
    """
    finished: list[bool] = []

    async def _cleanup() -> None:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        finished.append(True)

    async def _runner() -> None:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()  # requested + undelivered
        await run_cleanup_to_completion(_cleanup())

    task = asyncio.create_task(_runner())
    # ``await task`` (not ``asyncio.wait``) so an unexpected failure in the
    # runner surfaces; the cancel is absorbed by run_cleanup_to_completion, so
    # the runner completes normally.
    await task

    assert finished == [True], "cleanup must run to completion despite the cancel"


@pytest.mark.asyncio
async def test_run_cleanup_completes_when_cancelled_while_waiting():
    """A cancellation delivered while waiting on the cleanup is absorbed."""
    finished: list[bool] = []

    async def _slow_cleanup() -> None:
        await asyncio.sleep(0.02)
        finished.append(True)

    async def _runner() -> None:
        await run_cleanup_to_completion(_slow_cleanup())

    task = asyncio.create_task(_runner())
    await asyncio.sleep(0)  # let the runner start awaiting the shielded cleanup
    task.cancel()
    # ``await task`` (not ``asyncio.wait``) so an unexpected failure surfaces;
    # the waiter cancellation is absorbed, so the runner completes normally.
    await task

    assert finished == [True], "cleanup must finish even though the waiter was cancelled"


@pytest.mark.asyncio
async def test_run_cleanup_propagates_cleanup_error():
    """A non-cancellation error from the cleanup propagates to the caller."""

    async def _cleanup() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("close failed")

    with pytest.raises(RuntimeError, match="close failed"):
        await run_cleanup_to_completion(_cleanup())


@pytest.mark.asyncio
async def test_run_cleanup_surfaces_inner_task_cancellation():
    """If the cleanup task *itself* is cancelled, the cancellation is surfaced.

    Distinct from a cancellation delivered to the *waiter* (absorbed): when the
    shielded child task is cancelled (e.g. event-loop shutdown), there is no
    way to complete the cleanup, so ``run_cleanup_to_completion`` re-raises
    rather than silently returning. This is the branch the ``get_db`` finally
    relies on to never hide a real cancellation.
    """

    async def _self_cancelling() -> None:
        # Running as the shielded child task, cancel ourselves so the child
        # ends up in the cancelled state.
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        await asyncio.sleep(0)  # deliver the cancellation to this task

    with pytest.raises(asyncio.CancelledError):
        await run_cleanup_to_completion(_self_cancelling())


@pytest.mark.asyncio
async def test_run_cleanup_drains_absorbed_cancellation_count():
    """An absorbed waiter cancellation must not leave the task's cancel count
    elevated: ``run_cleanup_to_completion`` calls ``uncancel()`` for each
    cancellation it swallows (asyncio 3.11+), so the count returns to 0 and the
    next ``await`` cannot spuriously re-raise.
    """

    async def _slow_cleanup() -> None:
        await asyncio.sleep(0.02)

    async def _runner() -> int:
        await run_cleanup_to_completion(_slow_cleanup())
        task = asyncio.current_task()
        assert task is not None
        return task.cancelling()

    task = asyncio.create_task(_runner())
    await asyncio.sleep(0)  # let the runner start awaiting the shielded cleanup
    task.cancel()
    count_after = await task

    assert count_after == 0, "absorbed cancellation must be drained via uncancel()"
