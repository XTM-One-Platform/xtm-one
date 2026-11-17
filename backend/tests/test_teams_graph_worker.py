"""The worker process that decides large Teams Graph notification POSTs.

A job runs in a spawned worker with an interpreter of its own; what it raises
is raised in the caller; a job past its timeout, a worker that dies and a
caller cancelled meanwhile each take the worker with them; and when no worker
can be had the job runs on the webhook thread pool, in this process, so the
answer never depends on where it ran. The route's own tests
(``test_teams_graph_client_state.py``) run every body in every mode.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing.connection
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from app.channels import teams_graph_worker
from app.channels.teams_graph_worker import run_decision
from tests import teams_graph_worker_helpers as helpers


@pytest.fixture(autouse=True)
def _fresh_worker():
    teams_graph_worker.shutdown_teams_graph_worker()
    yield
    teams_graph_worker.shutdown_teams_graph_worker()


async def test_a_job_runs_in_a_process_of_its_own():
    assert await run_decision(os.getpid) != os.getpid()


async def test_what_the_job_raises_is_raised_here():
    with pytest.raises(ValueError, match="not a batch"):
        await run_decision(helpers.refuse, "not a batch")


async def test_a_job_past_its_timeout_is_killed_and_run_on_the_thread_pool(monkeypatch, caplog):
    monkeypatch.setattr(teams_graph_worker, "DECISION_TIMEOUT_SECONDS", 1.0)
    assert await run_decision(os.getpid) != os.getpid()
    with caplog.at_level(logging.WARNING, logger=teams_graph_worker.__name__):
        assert await run_decision(helpers.slow_pid, 2.0) == os.getpid()
    assert "killed after 1s" in caplog.text
    assert teams_graph_worker._worker is None
    assert await run_decision(os.getpid) != os.getpid()


async def test_a_worker_that_dies_is_replaced_and_the_job_run_on_the_thread_pool(caplog):
    with caplog.at_level(logging.WARNING, logger=teams_graph_worker.__name__):
        assert await run_decision(helpers.pid_unless_in_a_worker) == os.getpid()
    assert "worker failed" in caplog.text
    assert await run_decision(os.getpid) != os.getpid()


async def test_a_worker_that_cannot_start_leaves_the_work_on_the_thread_pool_for_a_while(monkeypatch, caplog):
    real_start, real_timeout = teams_graph_worker._start_worker, teams_graph_worker.WORKER_START_TIMEOUT_SECONDS
    monkeypatch.setattr(teams_graph_worker, "WORKER_START_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(teams_graph_worker, "_start_worker", helpers.hanging_start)
    with caplog.at_level(logging.WARNING, logger=teams_graph_worker.__name__):
        assert await run_decision(os.getpid) == os.getpid()
    assert "unavailable (start failed" in caplog.text
    monkeypatch.setattr(teams_graph_worker, "_start_worker", real_start)
    monkeypatch.setattr(teams_graph_worker, "WORKER_START_TIMEOUT_SECONDS", real_timeout)
    # Within WORKER_RETRY_SECONDS no worker is tried again.
    assert await run_decision(os.getpid) == os.getpid()
    assert teams_graph_worker._worker is None
    # Not through monkeypatch: its undo would bring the cooldown back for the next tests.
    teams_graph_worker._retry_at = 0.0
    assert await run_decision(os.getpid) != os.getpid()


async def test_a_worker_that_cannot_be_created_leaves_the_work_on_the_thread_pool(monkeypatch, caplog):
    def _no_process():
        raise OSError("no process can be spawned here")

    monkeypatch.setattr(teams_graph_worker, "_get_worker", _no_process)
    with caplog.at_level(logging.WARNING, logger=teams_graph_worker.__name__):
        assert await run_decision(os.getpid) == os.getpid()
    assert "no process can be spawned here" in caplog.text


async def test_a_cancelled_caller_takes_the_worker_with_it():
    assert await run_decision(os.getpid) != os.getpid()
    job = asyncio.create_task(run_decision(helpers.slow_pid, 30.0))
    while teams_graph_worker._gate().locked() is False:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.2)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert teams_graph_worker._worker is None


class _HeldSpawns:
    """Every new worker, its spawn held inside its first submit until :meth:`release`."""

    def __init__(self, monkeypatch) -> None:
        self.workers: list = []
        self.processes: list = []
        self.spawning = threading.Event()
        self._released = threading.Event()
        real = teams_graph_worker.ProcessWorker

        def spy(*args, **kwargs):
            worker = real(*args, **kwargs)
            executor = worker.executor
            spawn = executor._spawn_process

            def held() -> None:
                self.spawning.set()
                self._released.wait(60)
                spawn()
                self.processes.extend(executor._processes.values())

            executor._spawn_process = held
            self.workers.append(worker)
            return worker

        monkeypatch.setattr(teams_graph_worker, "ProcessWorker", spy)

    def release(self) -> None:
        self._released.set()

    async def spawned(self) -> list:
        deadline = time.monotonic() + 60
        while not self.processes:
            assert time.monotonic() < deadline, "the worker never spawned its process"
            await asyncio.sleep(0.02)
        return self.processes


@pytest.fixture
def held_spawns(monkeypatch):
    spawns = _HeldSpawns(monkeypatch)
    yield spawns
    spawns.release()
    for process in spawns.processes:
        if process.is_alive():
            process.kill()


async def _gone(process, seconds: float = 30.0) -> bool:
    """Whether *process* exited within *seconds*, read from its sentinel.

    Never ``join`` / ``is_alive``: once the pool is killed, the executor's
    manager thread reaps the same child with its own ``join``, both threads
    sit in ``waitpid`` on one pid, and the one the kernel does not hand the
    status to gets ECHILD and reads the dead process as alive. The sentinel
    is ready once the child has exited, whichever thread reaps it.
    """
    ready = await asyncio.to_thread(multiprocessing.connection.wait, [process.sentinel], seconds)
    return bool(ready)


async def test_a_caller_cancelled_while_the_process_spawns_leaves_no_process(held_spawns, monkeypatch):
    """The first submit spawns the process in a thread: a kill asked before it
    returned would miss the process, which then came up and stayed."""
    job = asyncio.create_task(run_decision(os.getpid))
    assert await asyncio.to_thread(held_spawns.spawning.wait, 60), "the worker never began to spawn"
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert teams_graph_worker._worker is None
    held_spawns.release()
    [process] = await held_spawns.spawned()
    assert await _gone(process), "the process spawned for the cancelled caller was left running"
    assert held_spawns.workers[0].executor._shutdown_thread
    monkeypatch.undo()
    assert await run_decision(os.getpid) != os.getpid()


@pytest.mark.parametrize("stop", ["interrupt", "close"], ids=["interrupt", "close"])
async def test_a_start_ended_by_any_other_exception_leaves_no_process(held_spawns, stop):
    """Not only a cancellation: an interrupt, or the coroutine closed with its task, ends the start the same way."""
    start = teams_graph_worker._ready_worker()
    start.send(None)  # suspended while a thread spawns the process
    assert await asyncio.to_thread(held_spawns.spawning.wait, 60), "the worker never began to spawn"
    if stop == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            start.throw(KeyboardInterrupt())
    else:
        start.close()
    assert teams_graph_worker._worker is None
    held_spawns.release()
    [process] = await held_spawns.spawned()
    assert await _gone(process), "the process spawned for the ended start was left running"
    assert held_spawns.workers[0].executor._shutdown_thread


def test_the_worker_imports_neither_the_teams_channel_nor_the_web_stack():
    """What a fresh worker imports is what its start costs: its start task and
    both jobs - a resource parsed, a clientState decrypted - load no web stack."""
    probe = textwrap.dedent(
        """
        import json, sys
        from app.channels import teams_graph_worker
        from app.channels.teams_graph_verdict import decide_post, summarize_post
        from app.core.security import encrypt_value

        teams_graph_worker._start_worker()
        notification = {
            "changeType": "created",
            "resource": "teams('t')/channels('19:c@thread.tacv2')/messages('m')",
            "subscriptionId": "sub-1",
            "clientState": "state",
        }
        body = json.dumps({"value": [notification]}).encode()
        count, keys, _candidates, _ignored = summarize_post(body, None, None)
        record = json.dumps({"subscription_id": "sub-1", "client_state_enc": encrypt_value("state")})
        decision, _pending = decide_post(body, None, None, {keys[0]: record}, True)
        assert (count, len(decision.accepted)) == (1, 1), (count, decision)
        heavy = ("app.channels.teams", "app.integrations.providers.teams_bot", "fastapi", "starlette",
                 "sqlalchemy", "httpx")
        print(",".join(name for name in heavy if name in sys.modules))
        """
    )
    backend = Path(__file__).resolve().parents[1]
    result = subprocess.run(  # noqa: S603 - this interpreter, a fixed script
        [sys.executable, "-c", probe], cwd=backend, capture_output=True, text=True, timeout=120, check=True
    )
    assert result.stdout.strip() == ""
