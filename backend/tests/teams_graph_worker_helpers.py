"""Stand-ins for the Teams Graph notification worker's jobs (``test_teams_graph_worker.py``).

They run in the spawned worker process, which imports them by name: nothing
here may need more than the standard library.
"""

from __future__ import annotations

import multiprocessing
import os
import time


def slow_pid(seconds: float) -> int:
    """The process that ran the job, after *seconds*."""
    time.sleep(seconds)
    return os.getpid()


def pid_unless_in_a_worker(exit_code: int = 1) -> int:
    """Dies in a worker process; answers anywhere else (the thread fallback)."""
    if multiprocessing.parent_process() is not None:
        os._exit(exit_code)
    return os.getpid()


def refuse(message: str) -> None:
    raise ValueError(message)


def hanging_start() -> None:
    """A worker start that never finishes in time."""
    time.sleep(600)
