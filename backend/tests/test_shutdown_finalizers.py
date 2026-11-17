"""Work a request owes before its process exits on a graceful shutdown.

uvicorn cancels the requests its bounded drain left running, then sends the
lifespan shutdown without awaiting them. A finalizer a cancelled request starts
must survive that request's cancellation, and the lifespan shutdown must wait,
bounded, for the cancelled requests and their finalizers before it tears the
process down. The background agent turns, which no drain waited for, get the
requests' grace and are then cancelled the way the drain cancels a request.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time

import pytest

from app.core import shutdown_finalizers
from app.core.shutdown_finalizers import (
    background_turns_in_flight,
    drain_shutdown_finalizers,
    start_shutdown_finalizer,
    track_background_turn,
    track_request_task,
    wait_for_shutdown_finalizer,
)
from app.core.utils import is_graceful_shutdown_cancellation
from app.main import GracefulShutdownDrainMiddleware

UVICORN_DRAIN_CANCEL_MSG = "Task cancelled, timeout graceful shutdown exceeded"


@pytest.fixture(autouse=True)
async def _clean_registries():
    yield
    for task in (
        *shutdown_finalizers._request_tasks,
        *shutdown_finalizers._finalizers,
        *shutdown_finalizers._background_turns,
    ):
        task.cancel()
    await asyncio.sleep(0)
    shutdown_finalizers._request_tasks.clear()
    shutdown_finalizers._finalizers.clear()
    shutdown_finalizers._background_turns.clear()


async def test_a_finalizer_outlives_the_cancellation_of_the_request_that_started_it():
    release = asyncio.Event()
    written: list[str] = []

    async def _write() -> None:
        await release.wait()
        written.append("reply")

    async def _request() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            start_shutdown_finalizer(_write(), name="reply")
            raise

    request = asyncio.create_task(_request())
    await asyncio.sleep(0)
    request.cancel(msg=UVICORN_DRAIN_CANCEL_MSG)
    with pytest.raises(asyncio.CancelledError):
        await request

    release.set()
    assert await drain_shutdown_finalizers(timeout=1) == 0
    assert written == ["reply"]


async def test_the_drain_waits_for_a_request_and_the_finalizer_it_starts_late():
    """A cancelled request still unwinding can start its finalizer after the
    drain began: the drain waits for that one too."""
    order: list[str] = []

    async def _write() -> None:
        await asyncio.sleep(0.02)
        order.append("finalizer")

    async def _request() -> None:
        with track_request_task():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.02)
                start_shutdown_finalizer(_write(), name="late")
                order.append("request")
                raise

    request = asyncio.create_task(_request())
    await asyncio.sleep(0)
    request.cancel(msg=UVICORN_DRAIN_CANCEL_MSG)

    assert await drain_shutdown_finalizers(timeout=1) == 0
    assert order == ["request", "finalizer"]
    assert request.cancelled()


async def test_the_drain_is_bounded(caplog: pytest.LogCaptureFixture):
    start_shutdown_finalizer(asyncio.Event().wait(), name="stuck")

    with caplog.at_level(logging.WARNING, logger="app.core.shutdown_finalizers"):
        assert await drain_shutdown_finalizers(timeout=0.05) == 1

    assert "still running after" in caplog.text


async def test_nothing_in_flight_returns_at_once():
    assert await drain_shutdown_finalizers(timeout=0) == 0


async def test_a_failing_finalizer_is_logged_never_raised(caplog: pytest.LogCaptureFixture):
    async def _boom() -> None:
        raise RuntimeError("database gone")

    with caplog.at_level(logging.WARNING, logger="app.core.shutdown_finalizers"):
        task = start_shutdown_finalizer(_boom(), name="boom")
        assert await drain_shutdown_finalizers(timeout=1) == 0

    assert task.exception() is None
    assert "Shutdown finalizer boom failed: database gone" in caplog.text


async def test_waiting_for_a_finalizer_never_cancels_it():
    release = asyncio.Event()
    task = start_shutdown_finalizer(release.wait(), name="slow")

    assert await wait_for_shutdown_finalizer(task, 0.02) is False
    assert not task.done()

    release.set()
    assert await wait_for_shutdown_finalizer(task, 1) is True


# ---------------------------------------------------------------------------
# Background agent turns
# ---------------------------------------------------------------------------


def _background_turn(seen: list[bool], *, runs_for: float | None = None) -> asyncio.Task:
    """A wake or a channel turn: records whether the drain cut it."""

    async def _turn() -> None:
        try:
            if runs_for is None:
                await asyncio.Event().wait()
            else:
                await asyncio.sleep(runs_for)
        except asyncio.CancelledError as cut:
            seen.append(is_graceful_shutdown_cancellation(cut))
            raise

    task = asyncio.create_task(_turn())
    track_background_turn(task)
    return task


async def test_a_background_turn_gets_its_grace_then_is_cut_as_the_drain_cuts_a_request(
    caplog: pytest.LogCaptureFixture,
):
    cut: list[bool] = []
    task = _background_turn(cut)
    await asyncio.sleep(0)
    assert background_turns_in_flight() == 1

    start = asyncio.get_running_loop().time()
    with caplog.at_level(logging.WARNING, logger="app.core.shutdown_finalizers"):
        assert (
            await drain_shutdown_finalizers(timeout=1, background_grace_until=time.monotonic() + 0.05)
            == 0
        )

    assert asyncio.get_running_loop().time() - start >= 0.05
    # Cut with uvicorn's drain message: run_agent_chat records its memory note
    # and the turn closes as interrupted, as a request the drain cuts does.
    assert cut == [True]
    assert task.cancelled()
    assert "cancelling 1 background agent turn(s)" in caplog.text
    assert background_turns_in_flight() == 0


async def test_a_background_turn_that_ends_within_its_grace_is_never_cut():
    cut: list[bool] = []
    task = _background_turn(cut, runs_for=0.02)

    assert await drain_shutdown_finalizers(timeout=1, background_grace_until=time.monotonic() + 5) == 0

    assert cut == []
    assert task.done() and not task.cancelled()


async def test_without_a_known_grace_a_background_turn_is_cut_at_once():
    cut: list[bool] = []
    _background_turn(cut)
    await asyncio.sleep(0)

    assert await drain_shutdown_finalizers(timeout=1) == 0

    assert cut == [True]


async def test_the_turns_spawned_outside_a_request_are_tracked(monkeypatch):
    from app.channels import base as channels_base
    from app.services import parent_wake

    monkeypatch.setattr(channels_base, "_get_channel_semaphore", lambda: None)
    release = asyncio.Event()

    channels_base.spawn_channel_task(release.wait(), name="channel-turn")
    parent_wake._spawn_wake(release.wait(), label="wake")

    assert background_turns_in_flight() == 2
    release.set()
    assert await drain_shutdown_finalizers(timeout=1) == 0
    assert background_turns_in_flight() == 0


# ---------------------------------------------------------------------------
# GracefulShutdownDrainMiddleware counts the requests in flight
# ---------------------------------------------------------------------------


async def _receive() -> dict:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


async def _send(_message: dict) -> None:
    return None


@pytest.mark.parametrize("scope_type", ["http", "websocket"])
async def test_the_middleware_counts_a_request_until_it_ends(scope_type):
    entered = asyncio.Event()
    leave = asyncio.Event()

    async def app(scope, receive, send):
        entered.set()
        await leave.wait()

    middleware = GracefulShutdownDrainMiddleware(app)
    task = asyncio.create_task(
        middleware({"type": scope_type, "path": "/x", "method": "GET", "headers": []}, _receive, _send)
    )
    await entered.wait()

    assert await drain_shutdown_finalizers(timeout=0.02) == 1

    leave.set()
    await task
    assert await drain_shutdown_finalizers(timeout=0) == 0


async def test_the_middleware_never_counts_the_lifespan():
    """The lifespan runs for the whole process: counting it would make its own
    shutdown wait for itself."""
    entered = asyncio.Event()

    async def app(scope, receive, send):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(GracefulShutdownDrainMiddleware(app)({"type": "lifespan"}, _receive, _send))
    await entered.wait()

    assert await drain_shutdown_finalizers(timeout=0) == 0
    task.cancel()


def test_the_lifespan_drains_before_it_closes_what_the_finalizers_write_to():
    from app import main

    source = inspect.getsource(main.lifespan)
    shutdown = source[source.index("yield  # HTTP server starts here") :]
    drain = shutdown.index("await drain_shutdown_finalizers(")

    assert drain < shutdown.index("close_pool()")
    assert drain < shutdown.index("close_saq_queue()")
    assert drain < shutdown.index("fail_orphaned_in_process_runs(")
