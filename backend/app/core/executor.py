# SPDX-License-Identifier: Apache-2.0

"""Process-wide default ``ThreadPoolExecutor`` for the asyncio event loop.

Why this exists
---------------
asyncpg establishes every new database connection by resolving DNS through
``loop.getaddrinfo()``, which runs on asyncio's *default* ``ThreadPoolExecutor``.
That very same executor is shared by **every** ``asyncio.to_thread()`` /
``loop.run_in_executor(None, ...)`` call in the process — S3/MinIO file I/O
(``services/file_storage.py``), the synchronous Bedrock SDK
(``llm/bedrock_provider.py``, including one executor task *per streamed chunk*),
docx rendering, SMTP, RSS parsing, and more.

Python sizes the default executor at ``min(32, os.cpu_count() + 4)`` — roughly
6-8 threads on a typical container. Under the worker's high agentic concurrency
(``max_concurrent_runs + max_concurrent_sub_agent_tasks`` executions plus SAQ
jobs and document embedding), a burst of blocking ``to_thread`` work (above all
S3 operations) saturates that tiny pool. Once it is saturated, asyncpg's
``getaddrinfo`` can no longer obtain a thread within the connect timeout, so
**every new DB connection times out** — surfacing as the recurring
``Reconciler cycle error`` / ``TimeoutError`` that wedges the orchestration
reconciler until the pod is restarted.

The fix is to install a generously sized, named default executor at process
startup so DNS resolution and DB connection establishment are never starved by
application blocking work. Threads are created lazily and spend almost all of
their life blocked on I/O, so a large ceiling is cheap.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from app.core.config import settings

logger = logging.getLogger(__name__)

# Set at startup on the loop that will actually consume it (uvicorn's loop for
# the web app, the SAQ worker's loop for the worker). We track WHICH loop it was
# installed on so a repeat call on the same loop is a no-op, while a call from a
# *different* in-process loop (test re-use, hot reload, any newly created loop)
# installs a fresh executor as that loop's default instead of silently leaving
# it on Python's tiny built-in pool — see ``configure_default_executor``.
_executor: ThreadPoolExecutor | None = None
_executor_loop: asyncio.AbstractEventLoop | None = None
_install_lock = threading.Lock()


def _python_default_max_workers() -> int:
    """Python's built-in default executor size: ``min(32, cpu + 4)``."""
    return min(32, (os.cpu_count() or 1) + 4)


def resolve_default_thread_pool_size() -> int:
    """Resolve the effective default-executor size.

    ``DEFAULT_THREAD_POOL_MAX_WORKERS=0`` (the default) auto-derives a size that
    comfortably covers the worker's worst-case concurrent blocking work plus
    headroom for DNS resolution and miscellaneous ``to_thread`` calls. A
    positive override is honoured. The result is never smaller than Python's
    own default, so this can only ever *add* capacity — never regress an
    install that was implicitly relying on the built-in pool.
    """
    py_default = _python_default_max_workers()
    configured = settings.default_thread_pool_max_workers
    if configured and configured > 0:
        return max(int(configured), py_default)
    # Auto: every concurrent agentic execution / SAQ job / document embed may
    # hold one or more blocking threads at once; add a fixed headroom so DNS
    # resolution (new DB connections) and other to_thread work always have free
    # threads even at peak concurrency.
    auto = (
        settings.worker_concurrency
        + settings.max_concurrent_runs
        + settings.max_concurrent_sub_agent_tasks
        + settings.worker_document_processing_concurrency
        + _DNS_AND_MISC_HEADROOM
    )
    return max(auto, py_default)


# Headroom added to the auto-sized pool for DNS resolution (asyncpg connects)
# and incidental to_thread work (telemetry, email, docx, ...) that is not
# accounted for by the per-execution concurrency budget.
_DNS_AND_MISC_HEADROOM = 16


def configure_default_executor(thread_name_prefix: str) -> ThreadPoolExecutor:
    """Install a generously sized default executor on the running event loop.

    Must be called from inside the target event loop (web lifespan / worker
    startup) before any ``to_thread`` / DNS work runs, so our executor becomes
    *the* default the loop hands to asyncpg and ``asyncio.to_thread``.

    Idempotent **per loop**: a repeat call on the same running loop returns the
    already-installed executor. A call from a *different* loop installs a fresh
    executor as that loop's default. Keying on the loop (not merely "have we
    ever created one?") matters because the install must actually take effect on
    whatever loop is running — a new in-process loop (test re-use, hot reload)
    would otherwise be left on Python's tiny built-in pool, re-exposing the
    DNS/DB starvation this module exists to prevent. It also avoids
    handing back a stale executor: a closing loop shuts down its default
    executor, so a single shared instance cannot safely be reused across loops.
    """
    global _executor, _executor_loop
    loop = asyncio.get_running_loop()
    with _install_lock:
        if _executor is not None and _executor_loop is loop:
            return _executor
        max_workers = resolve_default_thread_pool_size()
        executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=thread_name_prefix
        )
        loop.set_default_executor(executor)
        _executor = executor
        _executor_loop = loop
        logger.info(
            "Installed default ThreadPoolExecutor (max_workers=%d, prefix=%r, "
            "python_default=%d) to keep DB connection establishment / DNS off "
            "the path of application to_thread work",
            max_workers,
            thread_name_prefix,
            _python_default_max_workers(),
        )
        return executor


def default_executor_state() -> str:
    """How loaded the installed default executor is, for a diagnostic line.

    ``loop.getaddrinfo`` runs on this executor, not on anyio's thread limiter,
    so a connect that times out before reaching its peer is explained here:
    ``queued`` above zero means every thread was busy and the lookup waited.
    Never raises.
    """
    with _install_lock:
        executor = _executor
    if executor is None:
        return "default_executor=not_installed"
    try:
        threads = len(executor._threads)
        idle = executor._idle_semaphore._value
        queued = executor._work_queue.qsize()
        return (
            f"default_executor={threads}/{executor._max_workers} "
            f"idle={idle} queued={queued}"
        )
    except Exception as exc:  # diagnostics only
        return f"default_executor=unknown ({type(exc).__name__})"


def shutdown_default_executor() -> None:
    """Best-effort shutdown of the installed executor (process teardown).

    Uses ``wait=False`` + ``cancel_futures=True`` so a thread still blocked on a
    slow/blackholed dependency (the very failure mode this module guards
    against) can never make shutdown itself hang.
    """
    global _executor, _executor_loop
    with _install_lock:
        executor = _executor
        _executor = None
        _executor_loop = None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)
