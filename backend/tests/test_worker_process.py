"""``core/worker_process.ProcessWorker``: a kill never races the spawn.

The executor spawns its process inside the first submit, which runs in a thread,
and learns of it only when that submit returns. A kill asked meanwhile is done by
that thread once the process is known, without the caller waiting on the spawn,
and a submit after a kill spawns nothing. The tasks are builtins or stdlib-only
helpers, which a spawned worker imports by name without this test module.
"""

from __future__ import annotations

import multiprocessing.connection
import os
import threading

import pytest

from app.core.worker_process import ProcessWorker
from tests import teams_graph_worker_helpers as helpers


class _HeldSpawn:
    """Keeps a worker's spawn inside its first submit until :meth:`release`."""

    def __init__(self, worker: ProcessWorker) -> None:
        self.spawning = threading.Event()
        self.processes: list = []
        self._released = threading.Event()
        executor = worker.executor
        spawn = executor._spawn_process

        def held() -> None:
            self.spawning.set()
            self._released.wait(60)
            spawn()
            self.processes.extend(executor._processes.values())

        executor._spawn_process = held

    def release(self) -> None:
        self._released.set()


def _gone(process, seconds: float = 30.0) -> bool:
    # The executor's own thread reaps the process too: ``is_alive()`` reads True
    # while that thread holds the exit status, the sentinel does not.
    return bool(multiprocessing.connection.wait([process.sentinel], seconds))


def test_a_kill_asked_while_the_process_spawns_is_done_once_it_is_known():
    worker = ProcessWorker(os.getpid, helpers.hanging_start, "test worker")
    held = _HeldSpawn(worker)
    submitting = threading.Thread(target=worker.submit_start)
    submitting.start()
    try:
        assert held.spawning.wait(60), "the worker never began to spawn"
        worker.kill()
        # The spawn is held until the kill returns: a kill that waited on it would find it done.
        assert not held.processes, "the kill waited on the spawn"
    finally:
        held.release()
        submitting.join(60)
    [process] = held.processes
    assert _gone(process), "the process spawned for a killed worker was left running"
    assert worker.executor._shutdown_thread


def test_a_submit_after_a_kill_spawns_nothing():
    worker = ProcessWorker(os.getpid, os.getpid, "test worker")
    worker.kill()
    with pytest.raises(RuntimeError, match="discarded before it spawned"):
        worker.submit_start()
    assert not worker.executor._processes
    assert worker.executor._shutdown_thread


def test_a_started_worker_is_killed_and_its_executor_shut_down():
    worker = ProcessWorker(os.getpid, os.getpid, "test worker")
    try:
        pid = worker.submit_start().result(60)
        [process] = worker.executor._processes.values()
        assert pid == process.pid != os.getpid()
    finally:
        worker.kill()
    assert _gone(process)
    assert worker.executor._shutdown_thread
