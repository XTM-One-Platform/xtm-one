"""Name a degraded CI runner before the step timeout does.

A GitHub-hosted runner is sometimes three to six times slower than usual. The
backend and bootstrap shards then spend 7 to 18 minutes collecting instead of
1.5, run out of their step's 25 minutes at about a quarter of the tests and
fail with a timeout and no FAILED line: the log looks like a code red and a
triage reads a megabyte of it to learn that nothing failed.

Collection time is the measure: it imports the same modules on every run, so
on a healthy runner it barely moves (84 to 102 s on the six backend shards of
a green main run, 62 to 74 s on the two bootstrap shards). The test step starts
``python -m tests.runner_speed`` in the background first. It waits for the
marker the suite writes when a process finishes collecting
(``pytest_collection_finish`` in ``tests/conftest.py``) and, once collection has
run past ``FACTOR`` times ``PYTEST_COLLECTION_MEDIAN_SECONDS``, prints one
``::error::runner degraded`` annotation: GitHub shows it in the job summary and
the failure blocks of the log name it. When the marker appears it prints the
measured time on a plain line. A green run never reaches the limit and shows no
annotation. Nothing here loosens a timeout or retries anything.

Unset (local runs, the sandbox checks), the watcher and the marker do nothing.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TextIO

MEDIAN_ENV = "PYTEST_COLLECTION_MEDIAN_SECONDS"
MARKER_ENV = "PYTEST_COLLECTED_MARKER"

#: Collection past this multiple of its median on green runs means the runner, not the code.
FACTOR = 3.0

#: How often the watcher looks for the marker, in seconds.
POLL_SECONDS = 1.0


def median_seconds(environ: Mapping[str, str] = os.environ) -> float | None:
    """The median collection time set by ``PYTEST_COLLECTION_MEDIAN_SECONDS``; ``None`` when unset, empty or not positive."""
    raw = (environ.get(MEDIAN_ENV) or "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        raise ValueError(f"{MEDIAN_ENV} must be a number of seconds, got {raw!r}") from None
    return seconds if seconds > 0 else None


def limit_seconds(median: float) -> float:
    """The collection time past which the runner is called degraded."""
    return median * FACTOR


def degraded_annotation(elapsed: float, median: float) -> str:
    """The one GitHub error annotation a degraded runner gets."""
    return (
        f"::error::runner degraded: collection {elapsed:.0f}s vs median {median:.0f}s "
        f"(limit {FACTOR:g}x): a step timeout that follows is the runner, not the code"
    )


def collected_line(elapsed: float, median: float) -> str:
    """The plain log line written when collection ends, red or green."""
    return f"[runner] collection {elapsed:.0f}s (median {median:.0f}s, limit {limit_seconds(median):.0f}s)"


def mark_collected(environ: Mapping[str, str] = os.environ) -> None:
    """Write the marker the watcher waits for; nothing when ``PYTEST_COLLECTED_MARKER`` is unset."""
    target = (environ.get(MARKER_ENV) or "").strip()
    if not target:
        return
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def watch(
    median: float,
    marker: Path,
    *,
    out: TextIO,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll: float = POLL_SECONDS,
) -> bool:
    """Wait for ``marker``; annotate once if collection runs past the limit. True when the runner was called degraded."""
    started = clock()
    limit = limit_seconds(median)
    degraded = False
    while not marker.exists():
        elapsed = clock() - started
        if not degraded and elapsed >= limit:
            print(degraded_annotation(elapsed, median), file=out, flush=True)
            degraded = True
        sleep(poll)
    print(collected_line(clock() - started, median), file=out, flush=True)
    return degraded


def main(environ: Mapping[str, str] = os.environ, out: TextIO = sys.stdout) -> int:
    """Entry point of ``python -m tests.runner_speed``: watch when both variables are set, else return at once."""
    median = median_seconds(environ)
    marker = (environ.get(MARKER_ENV) or "").strip()
    if median is None or not marker:
        return 0
    watch(median, Path(marker), out=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
