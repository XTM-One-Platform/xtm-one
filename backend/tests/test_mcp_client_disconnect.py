"""Tests for MCPConnection.disconnect() RuntimeError suppression.

These tests verify that the disconnect() method gracefully suppresses all
known RuntimeError variants that arise from the MCP SDK's anyio-based async
generator lifecycle during connection teardown, so they don't pollute error
logs or surface as unhandled exceptions.
"""

import asyncio
from contextlib import AsyncExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.tools.mcp_client import MCPConnection, MCPServerRuntime


def _make_connection(name: str = "test-server") -> MCPConnection:
    """Build an MCPConnection in the "connected" state without actually connecting."""
    config = MCPServerRuntime(
        name=name,
        transport="streamable_http",
        url="http://localhost:9999/mcp",
    )
    conn = MCPConnection(config)
    conn._connected = True  # fake connected state
    # ``_close_exit_stack_best_effort`` has a ``_has_open_contexts``
    # short-circuit so calling ``aclose()`` on an
    # empty exit stack is a no-op.  Tests that mock the exit stack
    # need to declare that contexts are "open" so the helper actually
    # routes through the aclose() call we want to assert on.
    conn._has_open_contexts = True
    return conn


@pytest.mark.asyncio
async def test_disconnect_suppresses_cancel_scope_error():
    """RuntimeError with 'cancel scope' is suppressed (existing behaviour)."""
    conn = _make_connection()
    conn._exit_stack = AsyncMock(spec=AsyncExitStack)
    conn._exit_stack.aclose.side_effect = RuntimeError(
        "Attempted to exit cancel scope in a different task than it was entered in"
    )
    # Should not raise
    await conn.disconnect()
    assert conn._connected is False


@pytest.mark.asyncio
async def test_disconnect_suppresses_athrow_already_running():
    """RuntimeError 'athrow(): asynchronous generator is already running' is suppressed."""
    conn = _make_connection()
    conn._exit_stack = AsyncMock(spec=AsyncExitStack)
    conn._exit_stack.aclose.side_effect = RuntimeError(
        "athrow(): asynchronous generator is already running"
    )
    # Should not raise
    await conn.disconnect()
    assert conn._connected is False


@pytest.mark.asyncio
async def test_disconnect_suppresses_aclose_already_running():
    """RuntimeError 'aclose(): asynchronous generator is already running' is suppressed."""
    conn = _make_connection()
    conn._exit_stack = AsyncMock(spec=AsyncExitStack)
    conn._exit_stack.aclose.side_effect = RuntimeError(
        "aclose(): asynchronous generator is already running"
    )
    # Should not raise
    await conn.disconnect()
    assert conn._connected is False


@pytest.mark.asyncio
async def test_disconnect_suppresses_cancelled_error():
    """CancelledError during disconnect is suppressed."""
    conn = _make_connection()
    conn._exit_stack = AsyncMock(spec=AsyncExitStack)
    conn._exit_stack.aclose.side_effect = asyncio.CancelledError()
    # Should not raise
    await conn.disconnect()
    assert conn._connected is False


@pytest.mark.asyncio
async def test_disconnect_reraises_unexpected_runtime_error():
    """RuntimeError with an unrecognised message is re-raised."""
    conn = _make_connection()
    conn._exit_stack = AsyncMock(spec=AsyncExitStack)
    conn._exit_stack.aclose.side_effect = RuntimeError("some unexpected error")
    with pytest.raises(RuntimeError, match="some unexpected error"):
        await conn.disconnect()


@pytest.mark.asyncio
async def test_disconnect_skipped_when_not_connected():
    """disconnect() is a no-op when _connected is False."""
    conn = _make_connection()
    conn._connected = False
    conn._exit_stack = AsyncMock(spec=AsyncExitStack)
    await conn.disconnect()
    conn._exit_stack.aclose.assert_not_called()
    assert conn._connected is False


@pytest.mark.asyncio
async def test_disconnect_clears_connected_flag_on_success():
    """_connected is set to False after a clean disconnect."""
    conn = _make_connection()
    conn._exit_stack = AsyncMock(spec=AsyncExitStack)
    await conn.disconnect()
    assert conn._connected is False


@pytest.mark.asyncio
async def test_disconnect_cleans_up_stderr_capture_on_error():
    """_stderr_capture is stopped even when a suppressed error occurs."""
    conn = _make_connection()
    conn._exit_stack = AsyncMock(spec=AsyncExitStack)
    conn._exit_stack.aclose.side_effect = RuntimeError(
        "aclose(): asynchronous generator is already running"
    )

    mock_capture = SimpleNamespace(stopped=False)

    def _mark_stopped() -> None:
        mock_capture.stopped = True

    mock_capture.stop = _mark_stopped
    conn._stderr_capture = mock_capture  # type: ignore[assignment]

    await conn.disconnect()
    assert conn._connected is False
    assert mock_capture.stopped, "_stderr_capture.stop() must be called during cleanup"
    assert conn._stderr_capture is None


@pytest.mark.asyncio
async def test_disconnect_cleans_up_stderr_capture_when_runtime_error_propagates():
    """_stderr_capture must ALSO be stopped when ``disconnect()`` re-raises
    an unexpected ``RuntimeError`` — without this, every disconnect that
    hits a real error leaks the stderr reader thread / pipe FDs.

    The cleanup helper does ``self._stderr_capture.stop()``
    *after* ``_close_exit_stack_best_effort()`` returns, so the re-raise
    path skips it.  Wrapping in ``try/finally`` is the fix this test
    pins.
    """
    conn = _make_connection()
    conn._exit_stack = AsyncMock(spec=AsyncExitStack)
    conn._exit_stack.aclose.side_effect = RuntimeError(
        "some unexpected error not matching teardown noise"
    )

    mock_capture = SimpleNamespace(stopped=False)

    def _mark_stopped() -> None:
        mock_capture.stopped = True

    mock_capture.stop = _mark_stopped
    conn._stderr_capture = mock_capture  # type: ignore[assignment]

    # The unexpected RuntimeError MUST propagate (real bugs should
    # surface), but the stderr capture teardown MUST still run.
    with pytest.raises(RuntimeError, match="some unexpected error"):
        await conn.disconnect()

    assert conn._connected is False, (
        "post-disconnect invariant: _connected must be False even on re-raise"
    )
    assert mock_capture.stopped, (
        "stderr_capture.stop() must run via finally even when the cleanup "
        "helper re-raises — otherwise the stderr reader thread and pipe FDs "
        "leak on every disconnect that hits a real error"
    )
    assert conn._stderr_capture is None


@pytest.mark.asyncio
async def test_close_exit_stack_runs_aclose_when_task_already_cancelled():
    """``_close_exit_stack_best_effort`` must unwind the entered contexts
    even when the surrounding task already has a pending cancellation —
    i.e. when an outer ``except CancelledError:`` handler calls the
    helper, the ``cancelling()`` count is still non-zero and a plain
    ``await self._exit_stack.aclose()`` would observe the pending
    cancellation on its first checkpoint and bail without unwinding the
    entered contexts.  The uncancel/recancel dance restores the count
    to 0 for the duration of the cleanup, then re-injects the cancel so
    the outer caller still sees a cancelled task.

    This is the same code path exercised by the production
    ``connect()`` cancellation handler — the simpler scenario here
    just lets us assert the contract directly on the helper without
    routing through ``stdio_client`` mocking.
    """
    conn = _make_connection()
    aclose_calls: list[None] = []

    class _SlowAcloseStack:
        async def aclose(self) -> None:
            # An explicit checkpoint that a "pre-cancelled" outer task
            # would normally bail at — verifies the uncancel cleared
            # ``must_cancel`` before this yield.
            await asyncio.sleep(0)
            aclose_calls.append(None)

    conn._exit_stack = _SlowAcloseStack()  # type: ignore[assignment]

    cancel_observed: list[bool] = []

    async def _runner() -> None:
        # Simulate the production path: an outer ``Task.cancel()`` is
        # observed while the task is awaiting (here, sleep), the
        # CancelledError is caught, and the helper is invoked while
        # ``cancelling()`` is still > 0 — exactly the state
        # ``connect()`` is in when its ``session.initialize()`` is
        # cancelled by a parent task.
        try:
            await asyncio.sleep(60)  # cancelled externally below
        except asyncio.CancelledError:
            # ``cancelling()`` > 0 at this point; the helper must lift
            # it for the duration of aclose() and re-inject it in
            # finally so the next checkpoint still raises.
            await conn._close_exit_stack_best_effort("connecting to")
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                cancel_observed.append(True)
                raise

    task = asyncio.create_task(_runner())
    # Give the runner a chance to enter the sleep before we cancel.
    await asyncio.sleep(0)
    task.cancel()
    # ``asyncio.wait`` (rather than a bare ``await task``) satisfies the
    # static-analysis "statement has no effect" check while letting the
    # cancellation propagate to the runner; we then assert on the task's
    # terminal state and on the side-effect lists populated by the
    # runner's ``finally`` block.
    done, _pending = await asyncio.wait([task])
    [completed] = done
    assert completed.cancelled()

    assert aclose_calls == [None], (
        "aclose() must have completed despite the pending cancellation — "
        "this is the contract the uncancel/recancel dance protects."
    )
    assert cancel_observed == [True], (
        "The helper must re-trigger the cancellation in its ``finally`` "
        "so the outer task still observes that it was cancelled."
    )
