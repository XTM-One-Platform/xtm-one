"""Tests for the event-loop stall watchdog.

The watchdog must turn a blocked event loop — the silent production hang
that repeatedly failed the /api/health liveness probe — into actionable
WARNING logs: an in-stall stack dump of the loop thread (from the sampler
thread) and a post-stall lag report (from the heartbeat task).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

from app.core import loop_watchdog
from app.core.config import settings
from app.core.loop_watchdog import (
    EventLoopWatchdog,
    start_loop_watchdog,
    stop_loop_watchdog,
)
from tests.conftest import reap_loop_watchdogs

WATCHDOG_LOGGER = "app.loop_watchdog"


def _block_the_loop_until_dumped(dumped: threading.Event) -> None:
    """Synchronous blocking work on the event-loop thread, until the sampler dumped it.

    The sampler may catch the loop thread at any instruction of this frame
    (the sleep, or the check of *dumped*), so the dump is asserted on this
    function's name, the frame present on the loop thread's stack for the
    whole stall, never on the callee it happens to be in at that instant.
    """
    deadline = time.monotonic() + 10.0
    while not dumped.is_set() and time.monotonic() < deadline:
        time.sleep(0.05)


async def test_blocked_loop_produces_stack_dump_and_lag_report(caplog):
    """Blocking the loop beyond the threshold must log the blocking stack
    while the stall is in progress AND a lag report once the loop resumes."""
    watchdog = EventLoopWatchdog(
        threshold_seconds=0.3,
        interval_seconds=0.05,
        name="test",
    )
    blocking_frame = _block_the_loop_until_dumped.__name__
    dumped = threading.Event()

    class _DumpSeen(logging.Handler):
        """Sets *dumped* from the sampler thread, so the loop thread never reads caplog while blocked."""

        def emit(self, record: logging.LogRecord) -> None:
            message = record.getMessage()
            if "has been BLOCKED" in message and f"'{watchdog.name}'" in message and blocking_frame in message:
                dumped.set()

    seen = _DumpSeen(level=logging.WARNING)
    watchdog_logger = logging.getLogger(WATCHDOG_LOGGER)
    watchdog_logger.addHandler(seen)
    with caplog.at_level(logging.WARNING, logger=WATCHDOG_LOGGER):
        watchdog.start()
        try:
            # Let the heartbeat task run at least once.
            await asyncio.sleep(0.15)
            # Simulate the root-cause behaviour: synchronous CPU/blocking
            # work executed directly on the event loop. It lasts until the
            # sampler thread has dumped it, however late that thread wakes,
            # then past the lag threshold with room to spare.
            _block_the_loop_until_dumped(dumped)
            time.sleep(0.3)
            # Give the heartbeat + sampler time to report after the stall.
            await asyncio.sleep(0.3)
        finally:
            watchdog.stop()
            watchdog_logger.removeHandler(seen)

    # Scope to THIS watchdog's loop name: every instance shares the one
    # "app.loop_watchdog" logger, so an instance leaked by another test could
    # otherwise satisfy these existence assertions and make the test pass for
    # the wrong reason — the mirror image of the false failure that the same
    # sharing causes in test_healthy_loop_stays_silent below.
    messages = [
        m
        for m in (r.getMessage() for r in caplog.records)
        if f"'{watchdog.name}'" in m
    ]
    blocked = [m for m in messages if "has been BLOCKED" in m]
    assert blocked, f"expected in-stall stack dump, got: {messages}"
    # The dump must contain the event-loop thread's actual blocking frame.
    assert any(blocking_frame in m for m in blocked), blocked

    assert any(
        "stalled for" in m for m in messages
    ), f"expected post-stall lag report, got: {messages}"


async def test_healthy_loop_stays_silent(caplog):
    """A responsive loop must produce zero watchdog warnings."""
    # The threshold is deliberately far larger than the ~0.3s body: the
    # sampler cries "BLOCKED" once the heartbeat is `threshold + interval`
    # stale, so a tight threshold turns ordinary CI scheduling jitter into a
    # failure. The short interval keeps the sampler running several times
    # during the body, which is what the assertion below actually exercises.
    watchdog = EventLoopWatchdog(
        threshold_seconds=5.0,
        interval_seconds=0.05,
        name="quiet",
    )
    with caplog.at_level(logging.WARNING, logger=WATCHDOG_LOGGER):
        watchdog.start()
        try:
            for _ in range(10):
                await asyncio.sleep(0.03)
        finally:
            watchdog.stop()

    # Scope the assertion to THIS watchdog's loop name. Every watchdog logs
    # to the one shared "app.loop_watchdog" logger, so a watchdog leaked by
    # another test would otherwise fail this one from the outside — see
    # ``reap_loop_watchdogs`` in tests/conftest.py, which stops the leak at
    # its source. This filter is the second line of defence.
    ours = [
        r
        for r in caplog.records
        if r.name == WATCHDOG_LOGGER and f"'{watchdog.name}'" in r.getMessage()
    ]
    assert ours == []


async def test_stop_cancels_heartbeat_and_joins_sampler():
    watchdog = EventLoopWatchdog(
        threshold_seconds=1.0, interval_seconds=0.05, name="stoppable"
    )
    watchdog.start()
    heartbeat = watchdog._heartbeat_task
    sampler = watchdog._sampler_thread
    assert heartbeat is not None and not heartbeat.done()
    assert sampler is not None and sampler.is_alive()

    watchdog.stop()
    await asyncio.sleep(0.1)

    assert heartbeat.cancelled() or heartbeat.done()
    sampler.join(timeout=1.0)
    assert not sampler.is_alive()


async def test_start_loop_watchdog_respects_disable_flag(monkeypatch):
    monkeypatch.setattr(settings, "event_loop_watchdog_enabled", False)
    assert start_loop_watchdog("disabled-test") is None


async def test_start_loop_watchdog_registry_roundtrip(monkeypatch):
    monkeypatch.setattr(settings, "event_loop_watchdog_enabled", True)
    monkeypatch.setattr(settings, "event_loop_stall_threshold_seconds", 5.0)
    watchdog = start_loop_watchdog("registry-test")
    try:
        assert watchdog is not None
        assert watchdog.threshold == 5.0
        # Second call returns the same (already running) instance.
        assert start_loop_watchdog("registry-test") is watchdog
    finally:
        stop_loop_watchdog("registry-test")


# ── test-isolation guarantee ────────────────────────────────────────────────


async def test_leaked_watchdog_is_reaped_after_the_test(monkeypatch):
    """A watchdog a test forgets to stop must not outlive that test.

    Regression guard for the CI flake where ``app.worker.startup()`` — run
    without its matching ``shutdown()`` — left the "worker" sampler thread
    running for the rest of the session, which then reported a phantom
    ``has been BLOCKED`` stall (its loop was gone, so the heartbeat could
    never refresh) into an unrelated later test's ``caplog``.

    This exercises ``reap_loop_watchdogs`` directly rather than relying on
    test ordering, which pytest-randomly makes non-deterministic.
    """
    monkeypatch.setattr(settings, "event_loop_watchdog_enabled", True)
    watchdog = start_loop_watchdog("leak-probe")
    assert watchdog is not None
    sampler = watchdog._sampler_thread
    assert sampler is not None and sampler.is_alive()

    assert "leak-probe" in reap_loop_watchdogs()

    assert "leak-probe" not in loop_watchdog._watchdogs
    sampler.join(timeout=2.0)
    assert not sampler.is_alive(), (
        "the sampler thread is a daemon thread that survives its event loop; "
        "leaving it running lets it report phantom stalls into later tests"
    )


def test_watchdog_reaper_is_autouse(request):
    """The reaper only protects the suite if it runs for *every* test.

    Without ``autouse``, the leak silently comes back and reappears as a
    random-ordering flake in whichever test happens to be running when the
    orphaned sampler's re-dump timer fires. This test never requests the
    fixture, so finding it active is proof that it applies everywhere.
    """
    assert "_no_leaked_loop_watchdogs" in request.fixturenames
