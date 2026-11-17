# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""The worker process that decides large Teams Graph notification POSTs.

Parsing a 4 MiB POST and deciding its entries holds the GIL wherever it runs in
this process: ``json.loads`` and the garbage collection its allocations trigger
never let the event loop in, on a worker thread as inline. From
``GRAPH_NOTIFICATION_OFF_LOOP_BYTES`` the work runs in a spawned worker with an
interpreter of its own (``core/worker_process.py``, the pattern of the SVG
rasterizer and Nova matching): one worker, one job at a time per event loop,
killed past ``DECISION_TIMEOUT_SECONDS`` or with its caller, and only small
results cross back (the channel keys, then the decision). The worker imports
``teams_graph_verdict`` alone. A start that does not end with a started worker
(its caller cancelled while the process spawns, a failure, any other exception)
kills it through ``ProcessWorker``, which never races the spawn.

When no worker can be started, the work runs on the webhook thread pool
(``parse_offload.run_webhook_parse``) for ``WORKER_RETRY_SECONDS``; a job the
worker could not finish is decided there too. The functions are the same, so
the answer never depends on where they ran.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import weakref
from collections.abc import Callable
from concurrent.futures.process import BrokenProcessPool
from typing import Any

from app.core.parse_offload import run_webhook_parse
from app.core.worker_process import ProcessWorker, exit_with_parent

logger = logging.getLogger(__name__)

#: What one job may take (seconds): a 4 MiB POST is decided in well under one.
DECISION_TIMEOUT_SECONDS = 10.0
#: How long a fresh worker may take to start (spawn, imports), kept out of the
#: job's timeout.
WORKER_START_TIMEOUT_SECONDS = 30.0
#: After a worker could not be started, how long the work stays on the thread
#: pool before a worker is tried again (seconds).
WORKER_RETRY_SECONDS = 60.0

_WHAT = "Teams Graph notification worker"

_worker: ProcessWorker | None = None
_worker_lock = threading.Lock()
_retry_at = 0.0
#: What :func:`_in_worker` returns when the worker did not finish the job.
_FAILED = object()
#: One job at a time per event loop, so the timeout measures the job and never
#: the queue in front of it.
_gates: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()


def _init_worker() -> None:
    """Worker initializer: the watch on the parent."""
    exit_with_parent("teams-graph-parent-watch")


def _start_worker() -> None:
    """A fresh worker's first task: the imports every decision needs, out of the job's timeout."""
    from app.channels import teams_graph_verdict  # noqa: F401


def _get_worker() -> ProcessWorker:
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = ProcessWorker(_init_worker, _start_worker, _WHAT)
        return _worker


def _discard_worker(worker: ProcessWorker) -> None:
    """Kill the worker (never waiting on its spawn) and let the next job start a new one."""
    global _worker
    with _worker_lock:
        if _worker is worker:
            _worker = None
    worker.kill()


def _gate() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    gate = _gates.get(loop)
    if gate is None:
        gate = _gates[loop] = asyncio.Lock()
    return gate


def _stay_on_threads(reason: str) -> None:
    global _retry_at
    with _worker_lock:
        _retry_at = time.monotonic() + WORKER_RETRY_SECONDS
    logger.warning(
        "Teams Graph notification worker unavailable (%s): large POSTs are decided on the thread pool "
        "for the next %.0fs",
        reason,
        WORKER_RETRY_SECONDS,
    )


async def _ready_worker() -> ProcessWorker | None:
    """The started worker, a new one, or ``None`` when none can be had now."""
    if time.monotonic() < _retry_at:
        return None
    try:
        worker = _get_worker()
    except Exception as exc:  # no process can be spawned here
        _stay_on_threads(repr(exc)[:200])
        return None
    if worker.started:
        return worker
    try:
        # The first submit spawns the process: from a thread, not the loop's.
        started = await asyncio.to_thread(worker.submit_start)
        await asyncio.wait_for(asyncio.wrap_future(started), WORKER_START_TIMEOUT_SECONDS)
    except Exception as exc:  # a start that overran its bound, failed to spawn, or died starting
        _discard_worker(worker)
        _stay_on_threads(f"start failed: {repr(exc)[:200]}")
        return None
    except BaseException:
        # A caller cancelled mid-start (or a start ended any other way) takes
        # the half-started worker with it.
        _discard_worker(worker)
        raise
    worker.started = True
    return worker


async def _in_worker(worker: ProcessWorker, fn: Callable[..., Any], *args: Any) -> Any:
    """``fn(*args)`` in the worker, or ``_FAILED`` when it could not run, overran or died.

    The worker is then discarded and the next job starts a fresh one. A caller
    cancelled meanwhile takes the worker with it. What the job itself raises
    is raised here, as it would be in this process.
    """
    loop = asyncio.get_running_loop()
    try:
        future = loop.run_in_executor(worker.executor, fn, *args)
    except Exception as exc:  # a pool that broke while idle (a killed worker) or was shut down
        logger.warning("Teams Graph notification worker unavailable (%s)", repr(exc)[:200])
        _discard_worker(worker)
        return _FAILED
    try:
        return await asyncio.wait_for(future, DECISION_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning(
            "Teams Graph notification worker killed after %.0fs; the POST is decided on the thread pool",
            DECISION_TIMEOUT_SECONDS,
        )
        _discard_worker(worker)
        return _FAILED
    except asyncio.CancelledError:
        # Nothing else would ever stop what the worker is busy with, and the
        # next job would queue behind it: kill it with its caller.
        _discard_worker(worker)
        raise
    except (BrokenProcessPool, OSError) as exc:
        logger.warning(
            "Teams Graph notification worker failed (%s); the POST is decided on the thread pool", repr(exc)[:200]
        )
        _discard_worker(worker)
        return _FAILED


async def run_decision[T](fn: Callable[..., T], /, *args: Any) -> T:
    """``fn(*args)`` in the worker process, else on the webhook thread pool.

    *fn* is a function of ``teams_graph_verdict``, whose arguments and result
    cross the process boundary.
    """
    async with _gate():
        worker = await _ready_worker()
        if worker is not None:
            result = await _in_worker(worker, fn, *args)
            if result is not _FAILED:
                return result
    return await run_webhook_parse(fn, *args)


def shutdown_teams_graph_worker() -> None:
    """Best-effort teardown (lifespan / worker shutdown): never waits on a job."""
    global _worker, _retry_at
    with _worker_lock:
        worker = _worker
        _worker = None
        _retry_at = 0.0
    if worker is not None:
        worker.kill()


__all__ = [
    "DECISION_TIMEOUT_SECONDS",
    "WORKER_RETRY_SECONDS",
    "WORKER_START_TIMEOUT_SECONDS",
    "run_decision",
    "shutdown_teams_graph_worker",
]
