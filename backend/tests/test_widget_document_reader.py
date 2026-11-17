"""The worker process that reads widget visitors' documents.

A read runs in a spawned worker with an interpreter of its own; a read past its
timeout, a worker that dies or fails and a caller cancelled meanwhile each take
the worker with them; and when no worker can be had the document is refused -
never read in this process, where nothing bounds it.
"""

from __future__ import annotations

import asyncio
import functools
import io
import logging
import os
import sys
import time

import pytest

from app.services import widget_document_reader as reader
from app.services.widget_document_reader import DocumentReadError, inspect_file, read_document


@pytest.fixture(autouse=True)
def _fresh_worker():
    reader.shutdown_widget_document_reader()
    yield
    reader.shutdown_widget_document_reader()


async def test_a_read_runs_in_a_process_of_its_own():
    assert await reader._run(os.getpid) != os.getpid()


async def test_a_document_is_read_as_the_chat_upload_message_reads_it():
    assert await read_document(b"Q3 revenue grew 12%.", "notes.txt", "text/plain", 100) == (
        "Q3 revenue grew 12%.",
        20,
        20,
    )
    text, total, _stripped = await read_document(b"hello world " * 10, "long.txt", "text/plain", 20)
    assert (text, total) == ("hello world hello wo", 120)


async def test_a_file_is_checked_in_the_worker():
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buffer, "PNG")
    assert await inspect_file("scan.png", buffer.getvalue()) is None
    assert await inspect_file("notes.docx", b"PK\x03\x04" + b"\x00" * 16) == "The file is not a valid DOCX document."


async def test_a_read_past_its_timeout_is_killed_and_the_document_refused(monkeypatch, caplog):
    monkeypatch.setattr(reader, "READ_TIMEOUT_SECONDS", 1.0)
    await reader._run(os.getpid)
    with caplog.at_level(logging.WARNING, logger=reader.__name__), pytest.raises(DocumentReadError) as refused:
        await reader._run(time.sleep, 5)
    # Refused for good: the same file would overrun again.
    assert refused.value.retry_after is None
    assert "killed after 1s" in caplog.text and reader._worker is None
    assert await reader._run(os.getpid) != os.getpid()


async def test_a_worker_that_dies_is_replaced_and_the_document_refused():
    with pytest.raises(DocumentReadError) as refused:
        await reader._run(os._exit, 1)
    assert refused.value.retry_after is None and reader._worker is None
    assert await reader._run(os.getpid) != os.getpid()


async def test_a_read_that_raises_refuses_the_document_and_replaces_the_worker():
    with pytest.raises(DocumentReadError):
        await reader._run(int, "not a number")
    assert reader._worker is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the address-space cap is RLIMIT_AS")
async def test_a_read_past_the_memory_cap_refuses_the_document(monkeypatch):
    with pytest.raises(DocumentReadError) as refused:
        await reader._run(bytearray, reader.WORKER_ADDRESS_SPACE_BYTES + 1)
    assert refused.value.retry_after is None and reader._worker is None


async def test_a_cancelled_caller_takes_the_worker_with_it():
    await reader._run(os.getpid)
    job = asyncio.create_task(reader._run(time.sleep, 30))
    while not reader._gate().locked():
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.2)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert reader._worker is None


async def test_a_worker_that_cannot_start_refuses_documents_for_a_while(monkeypatch, caplog):
    monkeypatch.setattr(reader, "WORKER_START_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(reader, "_start_worker", functools.partial(time.sleep, 5))
    with caplog.at_level(logging.WARNING, logger=reader.__name__), pytest.raises(DocumentReadError) as refused:
        await reader._run(os.getpid)
    assert "unavailable (start failed" in caplog.text
    assert refused.value.retry_after == reader.WORKER_RETRY_SECONDS
    monkeypatch.undo()
    # Within WORKER_RETRY_SECONDS no worker is tried again, and nothing is read here instead.
    with pytest.raises(DocumentReadError) as still:
        await reader._run(os.getpid)
    assert 0 < still.value.retry_after <= reader.WORKER_RETRY_SECONDS and reader._worker is None
    # Not through monkeypatch: its undo would bring the cooldown back for the next tests.
    reader._retry_at = 0.0
    assert await reader._run(os.getpid) != os.getpid()


async def test_a_document_that_waits_too_long_for_the_worker_is_refused_as_busy(monkeypatch):
    monkeypatch.setattr(reader, "QUEUE_TIMEOUT_SECONDS", 0.05)
    async with reader._gate():
        with pytest.raises(DocumentReadError) as refused:
            await reader._run(os.getpid)
    assert refused.value.retry_after == reader.BUSY_RETRY_AFTER_SECONDS
