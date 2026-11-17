# SPDX-License-Identifier: Apache-2.0

"""What a worker process the platform starts for CPU work needs from its first moment.

Work that must never run on an event loop and must be stoppable - an SVG render
(``services/svg_raster.py``), Nova rule matching
(``services/nova_matching.py``), a large Teams Graph notification POST
(``channels/teams_graph_worker.py``) - runs in a ``ProcessPoolExecutor``
worker the platform kills when it overruns. Such a worker is imported by a
spawned interpreter, so this module stays stdlib only. Its address-space cap
(:func:`limit_address_space`) bounds what its work adds to what it maps once
started.
"""

from __future__ import annotations

import logging
import multiprocessing
import multiprocessing.connection
import os
import threading
from collections.abc import Callable
from concurrent.futures import Future, ProcessPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)


def exit_with_parent(thread_name: str) -> None:
    """End this worker the moment the process that started it is gone.

    A pool worker waits on its task queue, which never reports the parent's
    death: a parent killed outright (uvicorn's supervisor kills a worker whose
    event loop stopped answering, the OOM killer) would leave it behind for good.
    Call it from the pool's initializer; *thread_name* names the watching thread.
    """
    sentinel = getattr(multiprocessing.parent_process(), "sentinel", None)
    if sentinel is None:
        return

    def watch() -> None:
        try:
            ready = multiprocessing.connection.wait([sentinel])
        except Exception:  # cannot watch: the worker lives on as it always did
            return
        if ready:
            os._exit(0)

    threading.Thread(target=watch, name=thread_name, daemon=True).start()


def address_space_in_use() -> int | None:
    """The address space this process maps, in bytes (``/proc/self/statm``); ``None`` where it cannot be read."""
    try:
        with open("/proc/self/statm", encoding="ascii") as statm:
            pages = int(statm.read().split()[0])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


def limit_address_space(budget: int, what: str) -> int | None:
    """Cap this worker's address space at *budget* bytes past what it maps already; the cap set, ``None`` for none.

    Call it from the pool's initializer. A spawned worker has re-imported its
    parent's ``__main__`` by then, and what that maps grows with the host, not
    with the work: under a render process it is numpy, whose BLAS keeps a
    thread stack and a buffer for every CPU of the host (about 40 MiB each).
    The cap bounds what the work adds - an allocation past it fails in the
    worker - never what the worker started with. Where the OS lets no cap be
    set (Windows), or holds the process to a lower hard limit, that is said
    and kept; *what* names the worker in the log.
    """
    try:
        import resource

        cap = (address_space_in_use() or 0) + budget
        _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        if hard != resource.RLIM_INFINITY:
            cap = min(cap, hard)
        resource.setrlimit(resource.RLIMIT_AS, (cap, hard))
    except Exception as exc:  # Windows, or a limit the process may not set
        logger.debug("The %s runs without an address-space limit: %s", what, repr(exc)[:200])
        return None
    return cap


class ProcessWorker:
    """One spawned worker process, whose kill never races its spawn.

    The executor spawns its process inside the first submit, which a caller
    runs in a thread (:meth:`submit_start`) so the event loop never waits on a
    spawn, and it learns of the process only when that submit returns. A kill
    asked meanwhile is therefore done by that thread once the process is known,
    and a submit after a kill is refused: a caller cancelled mid-start never
    leaves a process behind, and never holds the event loop for the spawn.

    *initializer* (with *initargs*) runs first in the process, then *start*,
    its first task; *what* names the worker in errors and logs.
    """

    def __init__(
        self,
        initializer: Callable[..., None],
        start: Callable[[], Any],
        what: str,
        initargs: tuple[Any, ...] = (),
    ) -> None:
        self.executor = ProcessPoolExecutor(
            max_workers=1,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=initializer,
            initargs=initargs,
        )
        #: Its first task ran: jobs go straight to it.
        self.started = False
        self._start = start
        self._what = what
        self._state_lock = threading.Lock()
        self._spawning = False
        self._kill_asked = False

    def submit_start(self) -> Future:
        """Submit the first task, the one whose submit spawns the process: call it from a thread."""
        with self._state_lock:
            if self._kill_asked:
                raise RuntimeError(f"the {self._what} was discarded before it spawned")
            self._spawning = True
        try:
            return self.executor.submit(self._start)
        finally:
            with self._state_lock:
                self._spawning = False
                kill = self._kill_asked
            if kill:
                self._kill_now()

    def kill(self) -> None:
        """Kill the process and shut the executor down, never waiting on a spawn:
        one in flight is killed by its thread as soon as it is over."""
        with self._state_lock:
            self._kill_asked = True
            if self._spawning:
                return
        self._kill_now()

    def _kill_now(self) -> None:
        try:
            self.executor.kill_workers()
        except Exception:
            logger.debug("%s could not be killed", self._what, exc_info=True)
        self.executor.shutdown(wait=False, cancel_futures=True)
