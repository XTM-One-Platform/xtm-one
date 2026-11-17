"""Tests for ``app.core.database.get_db`` cancellation resilience.

``get_db`` is the FastAPI request-scoped DB session dependency. Its ``finally``
block must always run ``session.close()`` to completion — even when the
surrounding ASGI task is being cancelled (e.g. by MCP server cleanup tearing
down its anyio ``TaskGroup``). Without the ``run_cleanup_to_completion`` guard
(which closes the session on a shielded child task), the asyncpg ROLLBACK that
SQLAlchemy issues inside ``session.close()`` observes the pending cancellation
on its first ``await`` and raises ``CancelledError``, surfacing as an unhandled
ASGI exception and returning a corrupt connection to the pool.
"""

import asyncio
import contextlib
from unittest.mock import AsyncMock, patch

import pytest

from app.core.database import get_db


def _mock_session(*, close_raises: Exception | None = None) -> AsyncMock:
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock(side_effect=close_raises)
    return session


@pytest.mark.asyncio
async def test_commits_and_closes_on_success():
    """Happy path: the session is committed once and closed once."""
    session = _mock_session()

    with patch("app.core.database.async_session", return_value=session):
        gen = get_db()
        yielded = await gen.__anext__()
        assert yielded is session
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    session.commit.assert_awaited_once()
    session.close.assert_awaited_once()
    session.rollback.assert_not_called()


@pytest.mark.asyncio
async def test_rolls_back_and_closes_on_exception():
    """A handler error rolls back, closes, and re-raises the original error."""
    session = _mock_session()

    with patch("app.core.database.async_session", return_value=session):
        gen = get_db()
        await gen.__anext__()
        with pytest.raises(RuntimeError, match="handler boom"):
            await gen.athrow(RuntimeError("handler boom"))

    session.rollback.assert_awaited_once()
    session.close.assert_awaited_once()
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_closes_session_under_pending_cancellation():
    """``session.close()`` must complete even with a pending task cancellation.

    This drives the generator's teardown while a ``cancel()`` is requested but
    undelivered — the exact condition under which the unguarded close raised
    ``CancelledError`` inside the asyncpg ROLLBACK in production.
    """
    close_calls: list[bool] = []

    async def _slow_close() -> None:
        # Real checkpoints: a pending, undelivered cancel would raise here
        # without the shielded cleanup, leaving the connection un-released.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        close_calls.append(True)

    session = _mock_session()
    session.close = _slow_close  # type: ignore[assignment]

    async def _runner() -> None:
        with patch("app.core.database.async_session", return_value=session):
            gen = get_db()
            await gen.__anext__()
            task = asyncio.current_task()
            assert task is not None
            task.cancel()  # requested + undelivered
            # Drive the generator to completion: commit → finally → shielded
            # close (which must finish despite the pending cancellation).
            with contextlib.suppress(StopAsyncIteration, asyncio.CancelledError):
                await gen.__anext__()

    task = asyncio.create_task(_runner())
    # ``await task`` (not ``asyncio.wait``) so an unexpected failure in
    # ``_runner`` surfaces instead of being silently swallowed. ``_runner``
    # suppresses the expected StopAsyncIteration/CancelledError, so it
    # completes normally here.
    await task

    assert close_calls == [True], (
        "session.close() must complete despite the pending cancellation — "
        "run_cleanup_to_completion protects the asyncpg ROLLBACK"
    )


@pytest.mark.asyncio
async def test_cancellation_thrown_into_generator_still_propagates():
    """A ``CancelledError`` thrown into the dependency during teardown must
    still propagate after the session is closed — closing the session must not
    swallow the cancellation that is unwinding the request.
    """
    session = _mock_session()

    with patch("app.core.database.async_session", return_value=session):
        gen = get_db()
        await gen.__anext__()
        with pytest.raises(asyncio.CancelledError):
            await gen.athrow(asyncio.CancelledError())

    session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_error_is_swallowed_and_does_not_mask_original():
    """A close() failure must be logged and suppressed, not re-raised.

    The exception the request was already propagating (here a cancellation)
    must reach the caller unchanged; a secondary close() error must not
    replace it or surface as an extra unhandled exception.
    """
    session = _mock_session(close_raises=RuntimeError("driver error during close"))

    with patch("app.core.database.async_session", return_value=session):
        gen = get_db()
        await gen.__anext__()
        with pytest.raises(asyncio.CancelledError):
            await gen.athrow(asyncio.CancelledError())

    session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_closed_exactly_once_no_double_close():
    """The session is created directly (not via ``async with``) so that
    ``close()`` runs exactly once — a second close from a context manager's
    ``__aexit__`` would re-trigger the cancellation failure this fix prevents.
    """
    session = _mock_session()

    with patch("app.core.database.async_session", return_value=session):
        gen = get_db()
        await gen.__anext__()
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    assert session.close.await_count == 1
