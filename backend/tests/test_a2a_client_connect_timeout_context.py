"""The context logged when a token mint times out on connect.

Refs #9869: a connect timeout that never reached the peer must name what the
process looked like (anyio thread limiter, event loop load), never raise.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from app.a2a.client import delegation
from app.core import executor as executor_module


def test_context_names_the_thread_limiter_and_the_loop() -> None:
    async def read() -> str:
        return delegation._connect_timeout_context()

    context = asyncio.run(read())

    assert "thread_limiter=" in context
    assert "/" in context.split("thread_limiter=", 1)[1]
    assert "loop_tasks=" in context
    assert "unknown" not in context


def test_context_outside_a_loop_degrades_without_raising() -> None:
    context = delegation._connect_timeout_context()

    assert "thread_limiter=unknown" in context
    assert "loop_tasks=unknown" in context


def test_context_names_the_default_executor_getaddrinfo_runs_on(monkeypatch) -> None:
    """A queued lookup on a saturated default executor shows as queued > 0."""
    pool = ThreadPoolExecutor(max_workers=1)
    release = Event()
    try:
        pool.submit(release.wait)
        pool.submit(lambda: None)
        monkeypatch.setattr(executor_module, "_executor", pool)

        context = delegation._connect_timeout_context()
    finally:
        release.set()
        pool.shutdown(wait=True)

    assert "default_executor=1/1 idle=0 queued=1" in context


def test_context_without_an_installed_executor_says_so(monkeypatch) -> None:
    monkeypatch.setattr(executor_module, "_executor", None)

    assert "default_executor=not_installed" in delegation._connect_timeout_context()
