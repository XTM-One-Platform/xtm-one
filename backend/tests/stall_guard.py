"""Bound every test: one that hangs fails its worker and leaves its stacks.

A test that never returns used to burn the rest of the CI step (25 minutes)
and fail the run with a step timeout that named nothing. With
``PYTEST_STALL_SECONDS`` set, each test runs under ``faulthandler``'s watchdog:
past the bound, the stack of every thread is written to
``$PYTEST_STALL_DIR/stall-<worker>.log`` and the worker exits. xdist reports
the test as crashed while running and starts a fresh worker, so the shard fails
in minutes and its stall report step prints every report that is not empty.

The watchdog is a C thread: it fires even when the stuck code holds the GIL.
The stacks go to a file, not to stderr, because pytest's fd capture owns fd 2
while a test runs.
"""

from __future__ import annotations

import faulthandler
import os
from pathlib import Path
from typing import IO

STALL_SECONDS_ENV = "PYTEST_STALL_SECONDS"
STALL_DIR_ENV = "PYTEST_STALL_DIR"


def stall_seconds(environ: os._Environ[str] | dict[str, str] = os.environ) -> float | None:
    """The bound set by ``PYTEST_STALL_SECONDS``; ``None`` when unset, empty or not positive."""
    raw = (environ.get(STALL_SECONDS_ENV) or "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        raise ValueError(f"{STALL_SECONDS_ENV} must be a number of seconds, got {raw!r}") from None
    return seconds if seconds > 0 else None


def open_report(worker: str, environ: os._Environ[str] | dict[str, str] = os.environ) -> IO[str]:
    """This worker's stall report, under ``PYTEST_STALL_DIR`` (default: the working directory).

    Kept open for the worker's life: the watchdog writes to its descriptor.
    """
    directory = Path(environ.get(STALL_DIR_ENV) or ".")
    directory.mkdir(parents=True, exist_ok=True)
    return open(directory / f"stall-{worker}.log", "w", encoding="utf-8")


def arm(seconds: float, report: IO[str], nodeid: str) -> None:
    """Start the watchdog for one test, naming it first in the report."""
    report.seek(0)
    report.truncate()
    report.write(f"Test still running after {seconds:g}s: {nodeid}\n")
    report.flush()
    faulthandler.dump_traceback_later(seconds, exit=True, file=report)


def disarm(report: IO[str]) -> None:
    """Stop the watchdog and empty the report: the test returned, so only a stall leaves one."""
    faulthandler.cancel_dump_traceback_later()
    report.seek(0)
    report.truncate()
    report.flush()
