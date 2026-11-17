# SPDX-License-Identifier: Apache-2.0

"""What a budget on CPU work is charged: the CPU time of what runs it, never load.

A budget that bounds what a pattern may cost - the regex time of a schema check
(``services/schema_guard.regex_stopwatch``: the agent output-schema checks,
argument coercion) or a Nova rule's match in its worker process
(``services/nova_matching.py``) - is charged the CPU time of what runs the
work. Wall-clock time also counts every moment it did not run: the thread or the
worker descheduled on a loaded host, a thread waiting to get the GIL back (the
``regex`` engine releases it while it matches), the event loop's other work. On
a busy runner those pauses spent budgets no pattern used (up to 518 ms of
a 1 s budget) and left the last constraints of an output unjudged.

A garbage collection is no pattern's work either, and one over a long-lived
worker's heap can take a sizeable share of a budget: :func:`thread_search_time`
leaves out the collections the thread ran (``gc.callbacks``).

On Linux a CPU clock is exact. On Windows a thread's CPU time advances by whole
scheduler ticks (15.6 ms): a search is charged a tick when one lands in it and
nothing otherwise, which adds up to the CPU time the searches used - never
charging the tick's share by the time a search lasted instead, which would
undercount every search shorter than a tick and let a pattern that backtracks
run far past its budget in searches of a few milliseconds.

Stdlib only: the Nova matching worker imports it.
"""

from __future__ import annotations

import gc
import os
import threading
import time
from collections.abc import Callable
from typing import Any

#: A clock read in seconds.
Clock = Callable[[], float]

_collections = threading.local()


def _count_collection(phase: str, _info: dict[str, Any]) -> None:
    # Called on the thread that runs the collection, before and after it.
    if phase == "start":
        _collections.started = time.thread_time()
        return
    started = getattr(_collections, "started", None)
    if started is not None:
        _collections.seconds = getattr(_collections, "seconds", 0.0) + time.thread_time() - started
        _collections.started = None


if _count_collection not in gc.callbacks:
    gc.callbacks.append(_count_collection)


def thread_search_time() -> float:
    """The calling thread's CPU time, the garbage collections it ran left out."""
    return time.thread_time() - getattr(_collections, "seconds", 0.0)


class CpuStopwatch:
    """The CPU time *cpu_clock* counted since it started, and the wall-clock time that passed.

    With no *cpu_clock*, or once it stops answering (the process it reads is
    gone), the wall-clock time is charged.
    """

    __slots__ = ("_cpu", "_cpu_started", "_wall", "_wall_started")

    def __init__(self, cpu_clock: Clock | None, *, wall_clock: Clock = time.perf_counter) -> None:
        self._cpu = cpu_clock
        self._wall = wall_clock
        self._wall_started = wall_clock()
        self._cpu_started = self._read_cpu()

    def _read_cpu(self) -> float | None:
        if self._cpu is None:
            return None
        try:
            return self._cpu()
        except OSError:
            self._cpu = None
            return None

    def elapsed(self) -> float:
        """Wall-clock time since it started."""
        return self._wall() - self._wall_started

    def charged(self) -> float:
        """CPU time since it started (wall-clock time without a CPU clock)."""
        cpu = self._read_cpu()
        if cpu is None or self._cpu_started is None:
            return max(0.0, self.elapsed())
        return max(0.0, cpu - self._cpu_started)


def process_cpu_clock(pid: int) -> Clock | None:
    """The CPU time of process *pid*, or ``None`` where this platform cannot read another process's.

    Linux publishes it in ``/proc/<pid>/stat`` (user and system time of all its
    threads, in clock ticks of 10 ms); the standard library has no reader of
    another process's CPU time on Windows or macOS. The clock raises
    ``OSError`` once the process is gone.
    """
    if pid <= 0 or not hasattr(os, "sysconf"):
        return None
    path = f"/proc/{pid}/stat"
    try:
        ticks_per_second = os.sysconf("SC_CLK_TCK")
        _stat_cpu_seconds(path, ticks_per_second)
    except (OSError, ValueError):
        return None
    return lambda: _stat_cpu_seconds(path, ticks_per_second)


def _stat_cpu_seconds(path: str, ticks_per_second: int) -> float:
    with open(path, "rb") as stat:
        # The command name, in parentheses, may itself hold spaces or parentheses.
        fields = stat.read().rsplit(b")", 1)[-1].split()
    try:
        return (int(fields[11]) + int(fields[12])) / ticks_per_second
    except (IndexError, ValueError) as exc:
        raise OSError(f"unreadable {path}") from exc
