"""Tests for the process-wide default ThreadPoolExecutor installer.

asyncpg resolves DNS for every new DB connection on asyncio's default
``ThreadPoolExecutor``, shared with every ``asyncio.to_thread`` call. Python's
default size (``min(32, cpu+4)``) is too small under the worker's agentic
concurrency, so a burst of blocking S3 work starves DB connection establishment
and wedges the reconciler. These tests pin the sizing + install contract.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

from app.core import executor as ex


def test_resolve_size_clamps_up_to_python_default(monkeypatch):
    # A configured value below Python's own default must be clamped UP — the
    # installer can only ever add capacity, never regress an install relying on
    # the built-in pool.
    monkeypatch.setattr(ex.settings, "default_thread_pool_max_workers", 1)
    assert ex.resolve_default_thread_pool_size() == ex._python_default_max_workers()


def test_resolve_size_honours_positive_override(monkeypatch):
    big = ex._python_default_max_workers() + 100
    monkeypatch.setattr(ex.settings, "default_thread_pool_max_workers", big)
    assert ex.resolve_default_thread_pool_size() == big


def test_resolve_size_auto_covers_worker_concurrency(monkeypatch):
    # 0 = auto: derive from the worker concurrency budget + DNS headroom.
    monkeypatch.setattr(ex.settings, "default_thread_pool_max_workers", 0)
    monkeypatch.setattr(ex.settings, "worker_concurrency", 20)
    monkeypatch.setattr(ex.settings, "max_concurrent_runs", 10)
    monkeypatch.setattr(ex.settings, "max_concurrent_sub_agent_tasks", 20)
    monkeypatch.setattr(ex.settings, "worker_document_processing_concurrency", 3)
    expected = max(
        20 + 10 + 20 + 3 + ex._DNS_AND_MISC_HEADROOM,
        ex._python_default_max_workers(),
    )
    assert ex.resolve_default_thread_pool_size() == expected
    # The auto size must comfortably exceed the worst-case concurrent execution
    # demand so DNS resolution always has a free thread.
    assert expected > 20 + 10 + 20 + 3


def test_configure_installs_named_executor_and_is_idempotent(monkeypatch):
    monkeypatch.setattr(ex.settings, "default_thread_pool_max_workers", 48)

    async def _run() -> None:
        # Start from a clean slate regardless of prior tests in this process.
        ex.shutdown_default_executor()
        e1 = ex.configure_default_executor("test-pool")
        assert isinstance(e1, ThreadPoolExecutor)
        assert e1._max_workers == 48
        assert e1._thread_name_prefix == "test-pool"
        # The running loop must now hand out OUR executor by default.
        loop = asyncio.get_running_loop()
        ran_on = await loop.run_in_executor(None, lambda: ex.threading.current_thread().name)
        assert ran_on.startswith("test-pool")
        # Second call is a no-op and returns the same instance.
        e2 = ex.configure_default_executor("test-pool")
        assert e2 is e1
        ex.shutdown_default_executor()

    asyncio.run(_run())


def test_configure_reinstalls_on_a_new_loop(monkeypatch):
    # A *different* in-process event loop (test re-use / hot reload / any newly
    # created loop) must ALSO get our executor as its default. The idempotency
    # guard keys on the running loop, not merely "have we ever created one?":
    # a closing loop shuts down its default executor, so each loop installs its
    # own. Without this, a second loop silently falls back to Python's small
    # built-in pool and re-exposes the DNS/DB starvation.
    monkeypatch.setattr(ex.settings, "default_thread_pool_max_workers", 0)

    async def _probe() -> str:
        ex.configure_default_executor("reuse-pool")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: ex.threading.current_thread().name
        )

    ex.shutdown_default_executor()  # clean slate regardless of prior tests
    # asyncio.run() builds (and on close tears down) a brand-new loop each call.
    name_loop1 = asyncio.run(_probe())
    name_loop2 = asyncio.run(_probe())
    ex.shutdown_default_executor()

    assert name_loop1.startswith("reuse-pool")
    # The crux: the SECOND, independent loop still ran its default-executor work
    # on our pool rather than Python's built-in one.
    assert name_loop2.startswith("reuse-pool")


def test_shutdown_is_safe_when_not_configured():
    # Calling shutdown without a prior install must never raise.
    ex.shutdown_default_executor()
    ex.shutdown_default_executor()
