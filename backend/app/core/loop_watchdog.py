# SPDX-License-Identifier: Apache-2.0

"""Event-loop stall watchdog: turns silent hangs into actionable logs.

Why this exists
-----------------------------
Production pods repeatedly stopped answering ``GET /api/health`` for
80-120 s with a completely silent log stream, were killed by the kubelet,
and recovered — 15 confirmed occurrences with zero diagnostic output,
because a blocked event loop cannot log anything *while* it is blocked.

This watchdog produces evidence from inside the hang:

1. A **heartbeat task** on the monitored loop updates a monotonic timestamp
   every ``interval`` seconds. After every wake-up it also measures how late
   it ran; when the lag exceeds the stall threshold it logs a WARNING with
   the observed lag and a bounded summary of ``asyncio.all_tasks()`` stacks
   (the post-stall report).
2. A **sampler daemon thread** — immune to the loop being blocked — checks
   the heartbeat's freshness. When the heartbeat goes stale past the
   threshold it dumps the *current stack of the event-loop thread* via
   ``sys._current_frames()``, i.e. the exact frame that is blocking the
   loop, WHILE the stall is still in progress. Long stalls are re-dumped
   every ``redump_interval`` seconds.
3. On POSIX, ``faulthandler.register(SIGUSR1)`` is installed once so an
   operator can ``kill -USR1 <pid>`` a wedged process and get all thread
   stacks on stderr even if logging itself is stuck.

The watchdog is intentionally cheap: one timestamp write per interval on
the loop, and one comparison per interval on the sampler thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import functools
import logging
import signal
import sys
import threading
import time
import traceback

logger = logging.getLogger("app.loop_watchdog")

# Bound the post-stall asyncio task report so a pathological task count can
# never turn the diagnostic itself into a log bomb.
_MAX_TASKS_IN_REPORT = 25
_MAX_FRAMES_PER_TASK = 3


@functools.cache
def _register_sigusr1_dump() -> bool:
    """Register a SIGUSR1 all-thread stack dump (POSIX only).

    ``functools.cache`` on a zero-argument function makes this a one-shot
    latch: the body runs exactly once per process, every later call is a
    cached no-op. Returns whether the handler was installed (Windows has no
    ``SIGUSR1``, and restricted environments may refuse the registration —
    diagnostics only, never fail startup over it).
    """
    if not hasattr(signal, "SIGUSR1"):
        return False
    try:
        faulthandler.register(signal.SIGUSR1, all_threads=True, chain=True)
        logger.debug("SIGUSR1 stack-dump handler registered")
        return True
    except (ValueError, RuntimeError, OSError):
        logger.debug("Could not register SIGUSR1 stack dump", exc_info=True)
        return False


def _format_loop_thread_stack(thread_id: int) -> str:
    """Best-effort snapshot of the event-loop thread's current stack."""
    frame = sys._current_frames().get(thread_id)
    if frame is None:
        return "<event-loop thread stack unavailable>"
    return "".join(traceback.format_stack(frame))


def _summarize_tasks(loop: asyncio.AbstractEventLoop) -> str:
    """Bounded one-line-per-task summary of the loop's pending tasks."""
    try:
        tasks = [t for t in asyncio.all_tasks(loop) if not t.done()]
    except RuntimeError:
        return "<task enumeration unavailable>"
    lines: list[str] = [f"{len(tasks)} pending task(s)"]
    for task in tasks[:_MAX_TASKS_IN_REPORT]:
        frames = task.get_stack(limit=_MAX_FRAMES_PER_TASK)
        if frames:
            top = frames[-1]
            where = f"{top.f_code.co_filename}:{top.f_lineno} in {top.f_code.co_name}"
        else:
            where = "<no stack>"
        lines.append(f"  - {task.get_name()} @ {where}")
    if len(tasks) > _MAX_TASKS_IN_REPORT:
        lines.append(f"  ... and {len(tasks) - _MAX_TASKS_IN_REPORT} more")
    return "\n".join(lines)


class EventLoopWatchdog:
    """Detect and report event-loop stalls for one asyncio loop."""

    def __init__(
        self,
        *,
        threshold_seconds: float = 2.0,
        interval_seconds: float = 0.5,
        redump_interval_seconds: float = 30.0,
        name: str = "web",
    ) -> None:
        self.threshold = max(0.1, float(threshold_seconds))
        self.interval = max(0.05, float(interval_seconds))
        self.redump_interval = max(self.threshold, float(redump_interval_seconds))
        self.name = name
        self._last_beat = time.monotonic()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread_id: int | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._sampler_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # Sampler-side stall bookkeeping (only touched by the sampler thread).
        self._stall_started_at: float | None = None
        self._last_dump_at = 0.0

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the watchdog on the currently running event loop."""
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            return
        self._loop = asyncio.get_running_loop()
        self._loop_thread_id = threading.get_ident()
        self._last_beat = time.monotonic()
        self._stop_event.clear()
        self._heartbeat_task = asyncio.get_running_loop().create_task(
            self._heartbeat(), name=f"loop-watchdog-heartbeat-{self.name}"
        )
        self._sampler_thread = threading.Thread(
            target=self._sampler,
            name=f"loop-watchdog-{self.name}",
            daemon=True,
        )
        self._sampler_thread.start()
        _register_sigusr1_dump()
        logger.info(
            "Event-loop watchdog started (name=%s, threshold=%.1fs, interval=%.2fs)",
            self.name,
            self.threshold,
            self.interval,
        )

    def stop(self) -> None:
        """Stop the watchdog (lifespan teardown). Never raises.

        Bounded, not instantaneous: joining the sampler thread can wait up
        to ``interval * 4`` (2 s at the default 0.5 s interval) so a stop
        racing a sampler wake-up still ends with the thread reaped. The
        sampler wakes at least once per ``interval``, so in practice the
        join returns within one interval.
        """
        self._stop_event.set()
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is not None and not task.done():
            task.cancel()
        thread = self._sampler_thread
        self._sampler_thread = None
        if thread is not None and thread.is_alive():
            with contextlib.suppress(Exception):
                thread.join(timeout=self.interval * 4)

    # ── loop side: heartbeat + post-stall report ────────────────────────

    async def _heartbeat(self) -> None:
        while not self._stop_event.is_set():
            before = time.monotonic()
            self._last_beat = before
            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                return
            # How much later than requested did we actually wake up? That
            # excess is time during which THIS loop could not schedule
            # anything — including the /api/health short-circuit.
            lag = time.monotonic() - before - self.interval
            if lag >= self.threshold and self._loop is not None:
                # The diagnostic must never kill the heartbeat: a crashed
                # heartbeat task would silently disable the watchdog for the
                # rest of the process lifetime.
                try:
                    logger.warning(
                        "Event loop '%s' stalled for %.1fs (threshold %.1fs). %s",
                        self.name,
                        lag,
                        self.threshold,
                        _summarize_tasks(self._loop),
                    )
                except Exception:
                    logger.debug("Loop-lag report failed", exc_info=True)

    # ── sampler side: in-stall stack capture ────────────────────────────

    def _sampler(self) -> None:
        while not self._stop_event.wait(self.interval):
            staleness = time.monotonic() - self._last_beat
            if staleness < self.threshold + self.interval:
                # Healthy (the +interval grace absorbs scheduler jitter of a
                # busy-but-live loop). Close any open stall episode.
                if self._stall_started_at is not None:
                    logger.warning(
                        "Event loop '%s' recovered after a ~%.1fs stall",
                        self.name,
                        time.monotonic() - self._stall_started_at,
                    )
                    self._stall_started_at = None
                continue

            now = time.monotonic()
            first_dump = self._stall_started_at is None
            if first_dump:
                self._stall_started_at = self._last_beat
            if not first_dump and (now - self._last_dump_at) < self.redump_interval:
                continue
            self._last_dump_at = now

            stack = (
                _format_loop_thread_stack(self._loop_thread_id)
                if self._loop_thread_id is not None
                else "<loop thread unknown>"
            )
            logger.warning(
                "Event loop '%s' has been BLOCKED for %.1fs (no heartbeat). "
                "Current stack of the event-loop thread:\n%s",
                self.name,
                staleness,
                stack,
            )


_watchdogs: dict[str, EventLoopWatchdog] = {}


def start_loop_watchdog(name: str) -> EventLoopWatchdog | None:
    """Start (or return) the named watchdog on the running loop.

    Reads enablement + threshold from settings. Returns ``None`` when the
    watchdog is disabled. Must be called from inside the target event loop.
    """
    from app.core.config import settings

    if not settings.event_loop_watchdog_enabled:
        return None
    watchdog = _watchdogs.get(name)
    if watchdog is None:
        watchdog = EventLoopWatchdog(
            threshold_seconds=settings.event_loop_stall_threshold_seconds,
            name=name,
        )
        _watchdogs[name] = watchdog
    watchdog.start()
    return watchdog


def stop_loop_watchdog(name: str) -> None:
    """Stop the named watchdog if it is running."""
    watchdog = _watchdogs.pop(name, None)
    if watchdog is not None:
        watchdog.stop()
