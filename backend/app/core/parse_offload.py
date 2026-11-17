# SPDX-License-Identifier: Apache-2.0

"""Dedicated bounded thread pool for CPU-bound document parsing.

Why this exists
-----------------------------
Document text extraction (pypdf, python-docx, openpyxl, xlrd, python-pptx)
and HTML content extraction (trafilatura / BeautifulSoup) are pure-CPU,
pure-Python work that can take tens of seconds to minutes on large or
pathological files. Several web-process code paths executed that work
*directly on the uvicorn event loop*:

* chat / KB upload endpoints (``api/v1/chat.py``, ``api/v1/knowledge_bases.py``)
* per-turn LLM attachment resolution (``llm/file_resolver.py`` — re-parses the
  same PDF on EVERY turn of a conversation whose history references it)
* agent-loop file tools (``tools/base.py``)
* scheduled Drive KB syncs running in the web scheduler tick
  (``services/kb_sync.py``)

A single large PDF froze the event loop for 80-120 s: ``/api/health`` stopped
answering (the liveness short-circuit middleware never got scheduled), the
kubelet killed the pod, and every pooled Postgres connection was reset at
once — the recurring production hang.

The work is offloaded to a **dedicated** bounded executor rather than the
loop's default executor on purpose: the default executor also serves asyncpg
DNS resolution for every new DB connection (see ``core/executor.py``), and a burst of multi-minute parses must never starve DB connection
establishment. With its own small pool, parse bursts queue behind each other
while the loop, probes, and DB traffic stay responsive.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from app.core.config import settings

_parse_executor: ThreadPoolExecutor | None = None
_image_executor: ThreadPoolExecutor | None = None
_webhook_executor: ThreadPoolExecutor | None = None
_lock = threading.Lock()

THREAD_NAME_PREFIX = "xtmone-parse"
IMAGE_THREAD_NAME_PREFIX = "xtmone-image-prep"
WEBHOOK_THREAD_NAME_PREFIX = "xtmone-webhook"


def get_parse_executor() -> ThreadPoolExecutor:
    """Return the process-wide parse executor, creating it lazily."""
    global _parse_executor
    with _lock:
        if _parse_executor is None:
            _parse_executor = ThreadPoolExecutor(
                max_workers=max(1, settings.parse_thread_pool_max_workers),
                thread_name_prefix=THREAD_NAME_PREFIX,
            )
        return _parse_executor


async def run_parse[T](fn: Callable[..., T], /, *args) -> T:
    """Run a blocking CPU-bound parse function on the dedicated executor.

    Use this for any document / markup parsing that can be slow (PDF, DOCX,
    XLSX, PPTX, large HTML). Never run such work inline in an ``async def`` —
    it blocks the event loop for its full duration.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(get_parse_executor(), fn, *args)


def get_image_prep_executor() -> ThreadPoolExecutor:
    """Return the process-wide image preparation executor, creating it lazily."""
    global _image_executor
    with _lock:
        if _image_executor is None:
            _image_executor = ThreadPoolExecutor(
                max_workers=max(1, settings.parse_thread_pool_max_workers),
                thread_name_prefix=IMAGE_THREAD_NAME_PREFIX,
            )
        return _image_executor


async def run_image_prep[T](fn: Callable[..., T], /, *args) -> T:
    """Run the preparation of an image for an LLM request off the event loop.

    Its own pool, never the document parse pool: a request waits on its images
    before it can be sent, and a parse can hold every parse worker for minutes,
    so a chat turn would queue behind a knowledge-base ingestion. Preparation is
    bounded (one decode, one resize, one encode per image).
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(get_image_prep_executor(), fn, *args)


def get_webhook_executor() -> ThreadPoolExecutor:
    """Return the process-wide webhook body executor, creating it lazily."""
    global _webhook_executor
    with _lock:
        if _webhook_executor is None:
            _webhook_executor = ThreadPoolExecutor(
                max_workers=max(1, settings.parse_thread_pool_max_workers),
                thread_name_prefix=WEBHOOK_THREAD_NAME_PREFIX,
            )
        return _webhook_executor


async def run_webhook_parse[T](fn: Callable[..., T], /, *args) -> T:
    """Parse and decide a large inbound webhook body off the event loop.

    Its own pool, never the document parse pool: the sender waits on the
    answer (Microsoft Graph retries, then throttles, an endpoint that answers
    late), and a parse can hold every parse worker for minutes. Bounded like
    the others, so a flood of large bodies queues here instead of adding
    threads that compete with the loop for the GIL. Under the GIL a C-level
    step such as ``json.loads`` still holds the interpreter for its whole
    duration: what frees the loop is the Python-level work around it, and work
    that must not hold it at all runs in a worker process (the Teams Graph
    notification route comes here only when its worker cannot run,
    ``channels/teams_graph_worker.py``).
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(get_webhook_executor(), fn, *args)


def shutdown_offload_executors() -> None:
    """Best-effort teardown (lifespan/worker shutdown) of the parse, image
    preparation and webhook pools.

    ``wait=False`` + ``cancel_futures=True`` so a thread still crunching a
    pathological document can never make shutdown itself hang.
    """
    global _parse_executor, _image_executor, _webhook_executor
    with _lock:
        executors = (_parse_executor, _image_executor, _webhook_executor)
        _parse_executor = None
        _image_executor = None
        _webhook_executor = None
    for executor in executors:
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
