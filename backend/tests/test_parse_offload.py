"""Tests for the dedicated document-parse executor.

CPU-bound document parsing must run on its own bounded thread pool: never
on the event loop (it froze /api/health in production) and never on the
default executor (which serves asyncpg DNS resolution).
"""

from __future__ import annotations

import threading

import pytest

from app.core import parse_offload
from app.core.config import settings
from app.core.parse_offload import (
    IMAGE_THREAD_NAME_PREFIX,
    THREAD_NAME_PREFIX,
    WEBHOOK_THREAD_NAME_PREFIX,
    get_image_prep_executor,
    get_parse_executor,
    get_webhook_executor,
    run_image_prep,
    run_parse,
    run_webhook_parse,
    shutdown_offload_executors,
)
from app.services.chunking import extract_text_from_file_async


@pytest.fixture(autouse=True)
def _fresh_executor():
    """Isolate the module-level executor between tests."""
    shutdown_offload_executors()
    yield
    shutdown_offload_executors()


async def test_run_parse_executes_off_the_event_loop():
    loop_thread = threading.current_thread()
    worker_thread_name = await run_parse(lambda: threading.current_thread().name)
    assert worker_thread_name != loop_thread.name
    assert worker_thread_name.startswith(THREAD_NAME_PREFIX)


async def test_run_parse_uses_dedicated_pool_not_default_executor():
    """The parse pool must be distinct from the loop's default executor so
    a burst of slow parses can never starve asyncpg DNS resolution."""
    executor = get_parse_executor()
    assert executor._thread_name_prefix == THREAD_NAME_PREFIX
    assert executor._max_workers == max(1, settings.parse_thread_pool_max_workers)


async def test_run_parse_propagates_result_and_arguments():
    assert await run_parse(lambda a, b: a + b, 2, 3) == 5


async def test_run_parse_propagates_exceptions():
    def _boom():
        raise ValueError("bad document")

    with pytest.raises(ValueError, match="bad document"):
        await run_parse(_boom)


async def test_extract_text_async_matches_sync_result():
    text = await extract_text_from_file_async(b"hello\x00 world", "note.txt")
    assert text == "hello world"


async def test_extract_text_async_runs_on_parse_executor(monkeypatch):
    """The async wrapper must offload to the dedicated parse pool."""
    seen: dict[str, str] = {}

    def _spy(content: bytes, filename: str) -> str:
        seen["thread"] = threading.current_thread().name
        return "spied"

    from app.services import chunking

    monkeypatch.setattr(chunking, "extract_text_from_file", _spy)
    result = await chunking.extract_text_from_file_async(b"x", "a.pdf")
    assert result == "spied"
    assert seen["thread"].startswith(THREAD_NAME_PREFIX)


def test_shutdown_is_idempotent_and_recreates_lazily():
    executor_one = get_parse_executor()
    shutdown_offload_executors()
    shutdown_offload_executors()  # second call must be a no-op
    executor_two = get_parse_executor()
    assert executor_two is not executor_one
    assert parse_offload._parse_executor is executor_two


async def test_image_preparation_has_its_own_pool_torn_down_with_the_parse_pool():
    name = await run_image_prep(lambda: threading.current_thread().name)
    assert name.startswith(IMAGE_THREAD_NAME_PREFIX)
    image_pool = get_image_prep_executor()
    assert image_pool is not get_parse_executor()
    assert image_pool._max_workers == max(1, settings.parse_thread_pool_max_workers)

    shutdown_offload_executors()

    assert parse_offload._image_executor is None
    assert get_image_prep_executor() is not image_pool


async def test_webhook_bodies_have_their_own_pool_torn_down_with_the_parse_pool():
    """A sender waits on a webhook's answer: it never queues behind a document parse."""
    name = await run_webhook_parse(lambda: threading.current_thread().name)
    assert name.startswith(WEBHOOK_THREAD_NAME_PREFIX)
    webhook_pool = get_webhook_executor()
    assert webhook_pool is not get_parse_executor()
    assert webhook_pool is not get_image_prep_executor()
    assert webhook_pool._max_workers == max(1, settings.parse_thread_pool_max_workers)
    assert await run_webhook_parse(lambda a, b: a + b, 2, 3) == 5

    shutdown_offload_executors()

    assert parse_offload._webhook_executor is None
    assert get_webhook_executor() is not webhook_pool
