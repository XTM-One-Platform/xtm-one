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

"""The worker process that decodes the files public chat widget visitors attach.

A widget visitor has no account, and an image, a PDF or an Office file can be
built to hold a decoder for minutes. Every such file is checked
(:func:`inspect_file`, ``widget_upload_inspection``) and every document read
(:func:`read_document`) in a spawned worker with an interpreter of its own
(``core/worker_process.py``, the pattern of the SVG rasterizer, Nova matching
and the Teams Graph worker), never on the parse executor the platform's own
work shares: one worker, one file at a time per event loop, killed past
:data:`READ_TIMEOUT_SECONDS`, with a caller cancelled meanwhile, or when a
read adds more than :data:`WORKER_ADDRESS_SPACE_BYTES` to it - where the OS
lets that cap be set (Linux); elsewhere the timeout and the kill still hold. Only a verdict
or the text crosses back.

It fails closed. A document the worker did not finish reading, or that no
worker could be started for, is refused (:class:`DocumentReadError`), never
read in this process: that would drop the bounds on the very file that
overran them.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
import weakref
from collections.abc import Callable
from typing import Any

from app.core.worker_process import ProcessWorker, exit_with_parent, limit_address_space

logger = logging.getLogger(__name__)

#: What reading one document may take (seconds, the read alone).
READ_TIMEOUT_SECONDS = 30.0
#: How long a document may wait for the worker before it is refused as busy.
QUEUE_TIMEOUT_SECONDS = 30.0
#: How long a fresh worker may take to start (spawn, imports), kept out of the
#: read's timeout.
WORKER_START_TIMEOUT_SECONDS = 60.0
#: After a worker could not be started, how long documents are refused before
#: a worker is tried again (seconds).
WORKER_RETRY_SECONDS = 60.0
#: What a read may add to the worker's address space, where the OS lets it be capped (``limit_address_space``).
WORKER_ADDRESS_SPACE_BYTES = 2 * 1024 * 1024 * 1024
#: The wait a busy reader asks a visitor for (seconds).
BUSY_RETRY_AFTER_SECONDS = 5

_WHAT = "widget document reader"

_worker: ProcessWorker | None = None
_worker_lock = threading.Lock()
_retry_at = 0.0
#: One document at a time per event loop, so the timeout measures the read and
#: never the queue in front of it.
_gates: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()


class DocumentReadError(Exception):
    """The document was not read: *retry_after* seconds when the reader was busy or could not start, else ``None``."""

    def __init__(self, reason: str, *, retry_after: int | None = None) -> None:
        super().__init__(reason)
        self.retry_after = retry_after


def _init_worker() -> None:
    """Worker initializer: the address-space cap and the watch on the parent."""
    limit_address_space(WORKER_ADDRESS_SPACE_BYTES, _WHAT)
    exit_with_parent("widget-document-parent-watch")


def _start_worker() -> None:
    """A fresh worker's first task: the decoders' imports, out of the read's timeout."""
    import PIL.Image  # noqa: F401

    from app.services import attachment_extract, chunking, widget_upload_inspection  # noqa: F401


def _inspect(filename: str, data: bytes) -> str | None:
    """``widget_upload_inspection.inspection_problem`` of the file, in the worker."""
    from app.services.widget_upload_inspection import inspection_problem

    return inspection_problem(filename, data)


def _read(data: bytes, filename: str, content_type: str, limit: int) -> tuple[str, int, int]:
    """``inline_attachment_text`` of the document, in the worker: its text cut at *limit*, and its lengths."""
    from app.services.attachment_extract import inline_attachment_text

    inline = inline_attachment_text(data, filename, content_type, False, limit)
    return inline.text, inline.total, inline.stripped_total


def _get_worker() -> ProcessWorker:
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = ProcessWorker(_init_worker, _start_worker, _WHAT)
        return _worker


def _discard_worker(worker: ProcessWorker) -> None:
    """Kill the worker (never waiting on its spawn) and let the next read start a new one."""
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


def _unavailable(reason: str) -> DocumentReadError:
    global _retry_at
    with _worker_lock:
        _retry_at = time.monotonic() + WORKER_RETRY_SECONDS
    logger.warning(
        "Widget document reader unavailable (%s): visitor documents are refused for the next %.0fs",
        reason,
        WORKER_RETRY_SECONDS,
    )
    return DocumentReadError("unavailable", retry_after=math.ceil(WORKER_RETRY_SECONDS))


async def _ready_worker() -> ProcessWorker:
    """The started worker or a new one; :class:`DocumentReadError` when none can be had now."""
    wait = _retry_at - time.monotonic()
    if wait > 0:
        raise DocumentReadError("unavailable", retry_after=math.ceil(wait))
    try:
        worker = _get_worker()
    except Exception as exc:  # no process can be spawned here
        raise _unavailable(repr(exc)[:200]) from None
    if worker.started:
        return worker
    try:
        # The first submit spawns the process: from a thread, not the loop's.
        started = await asyncio.to_thread(worker.submit_start)
        await asyncio.wait_for(asyncio.wrap_future(started), WORKER_START_TIMEOUT_SECONDS)
    except Exception as exc:  # a start that overran its bound, failed to spawn, or died starting
        _discard_worker(worker)
        raise _unavailable(f"start failed: {repr(exc)[:200]}") from None
    except BaseException:
        # A caller cancelled mid-start takes the half-started worker with it.
        _discard_worker(worker)
        raise
    worker.started = True
    return worker


async def _in_worker(worker: ProcessWorker, fn: Callable[..., Any], *args: Any) -> Any:
    """``fn(*args)`` in the worker; :class:`DocumentReadError` when it overran, died or failed.

    The worker is then discarded and the next read starts a fresh one. A caller
    cancelled meanwhile takes the worker with it.
    """
    loop = asyncio.get_running_loop()
    try:
        future = loop.run_in_executor(worker.executor, fn, *args)
    except Exception as exc:  # a pool that broke while idle (a killed worker) or was shut down
        _discard_worker(worker)
        raise DocumentReadError(f"worker unavailable: {repr(exc)[:200]}") from None
    try:
        return await asyncio.wait_for(future, READ_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning("Widget document reader killed after %.0fs on one document", READ_TIMEOUT_SECONDS)
        _discard_worker(worker)
        raise DocumentReadError("overran") from None
    except asyncio.CancelledError:
        # Nothing else would ever stop what the worker is busy with, and the
        # next read would queue behind it: kill it with its caller.
        _discard_worker(worker)
        raise
    except Exception as exc:  # the worker died (a crash, the memory cap) or the read raised
        logger.warning("Widget document reader failed on one document: %s", repr(exc)[:200])
        _discard_worker(worker)
        raise DocumentReadError("failed") from None


async def _run[T](fn: Callable[..., T], /, *args: Any) -> T:
    """``fn(*args)`` in the worker, one at a time per event loop; :class:`DocumentReadError` otherwise."""
    gate = _gate()
    try:
        async with asyncio.timeout(QUEUE_TIMEOUT_SECONDS):
            await gate.acquire()
    except TimeoutError:
        raise DocumentReadError("busy", retry_after=BUSY_RETRY_AFTER_SECONDS) from None
    try:
        return await _in_worker(await _ready_worker(), fn, *args)
    finally:
        gate.release()


async def inspect_file(filename: str, data: bytes) -> str | None:
    """What decoding finds wrong with a visitor's file (``widget_upload_inspection``), in the worker.

    ``None`` when nothing. Raises :class:`DocumentReadError`.
    """
    return await _run(_inspect, filename, data)


async def read_document(data: bytes, filename: str, content_type: str, limit: int) -> tuple[str, int, int]:
    """The text of a visitor's document cut at *limit*, its length and its stripped length.

    ``attachment_extract.inline_attachment_text`` - the chat upload message's
    rule - run in the worker. Raises :class:`DocumentReadError`.
    """
    return await _run(_read, data, filename, content_type, limit)


def shutdown_widget_document_reader() -> None:
    """Best-effort teardown (lifespan / worker shutdown): never waits on a read."""
    global _worker, _retry_at
    with _worker_lock:
        worker = _worker
        _worker = None
        _retry_at = 0.0
    if worker is not None:
        worker.kill()


__all__ = [
    "QUEUE_TIMEOUT_SECONDS",
    "READ_TIMEOUT_SECONDS",
    "WORKER_ADDRESS_SPACE_BYTES",
    "WORKER_RETRY_SECONDS",
    "WORKER_START_TIMEOUT_SECONDS",
    "DocumentReadError",
    "inspect_file",
    "read_document",
    "shutdown_widget_document_reader",
]
