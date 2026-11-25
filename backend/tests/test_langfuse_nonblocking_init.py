"""Regression: ``get_langfuse_client`` must never block the asyncio event loop.

The Langfuse v4 SDK is synchronous; a slow/unreachable host previously froze the
worker's event loop on the in-loop ``client.api.projects.get()`` auth check,
stalling the orchestration reconciler and every concurrent agent loop (runs/tasks
stuck on ``pending``). The init now runs the blocking SDK calls in a worker
thread, bounded by a timeout, and is single-flighted. These tests pin that:

- while a slow init is in flight, other coroutines keep making progress, and the
  call gives up via its bounded ``wait_for`` instead of hanging;
- concurrent first-callers trigger the blocking init at most once.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.services import langfuse_tracing as lt


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    """Returns an enabled LangfuseConfig for the single ``select`` issued."""

    def __init__(self, config):
        self._config = config

    async def execute(self, *_args, **_kwargs):
        return _FakeResult(self._config)


def _enabled_config():
    return SimpleNamespace(
        enabled=True,
        public_key_enc="pk_enc",
        secret_key_enc="sk_enc",
        host="https://langfuse.invalid",
    )


@pytest.fixture(autouse=True)
def _isolate_langfuse_globals(monkeypatch):
    # Reset the module-level client cache / cooldown / single-flight lock so each
    # test starts from "never initialized", and stub decryption (the keys above
    # are not real Fernet tokens).
    monkeypatch.setattr(lt, "_langfuse_client", None)
    monkeypatch.setattr(lt, "_langfuse_enabled", None)
    monkeypatch.setattr(lt, "_langfuse_retry_after", 0)
    monkeypatch.setattr(lt, "_langfuse_init_lock", None)
    monkeypatch.setattr("app.core.security.decrypt_value", lambda _v: "decrypted")
    monkeypatch.setattr("app.core.security.decrypt_stored_value", lambda _v: "decrypted")
    yield


@pytest.mark.asyncio
async def test_slow_init_does_not_block_event_loop(monkeypatch):
    monkeypatch.setattr(lt, "_LANGFUSE_AUTH_TIMEOUT", 0)  # wait_for budget = 2s
    release = threading.Event()
    thread_done = threading.Event()

    def _slow_blocking(_pk, _sk, _host):
        # Blocks the *worker thread*, never the event loop. Released by the test
        # after the assertions so no thread lingers past the test. The 60s
        # ceiling is a safety net only: the assertion below is that the call
        # returned while this thread was STILL blocked, so the ceiling must stay
        # far above any event-loop stall a loaded CI runner can produce.
        try:
            release.wait(timeout=60)
        finally:
            thread_done.set()
        raise RuntimeError("unreachable host")

    monkeypatch.setattr(lt, "_blocking_init_and_auth", _slow_blocking)

    ticks = 0

    async def _ticker():
        nonlocal ticks
        for _ in range(40):
            await asyncio.sleep(0.05)
            ticks += 1

    ticker = asyncio.create_task(_ticker())
    try:
        client = await lt.get_langfuse_client(_FakeDB(_enabled_config()))
        # Captured BEFORE the release: the worker thread must still be blocked
        # when the call returns, i.e. the return came from the bounded
        # ``wait_for``, not from the thread finishing. This is the property
        # under test and it does not depend on wall-clock time: a previous
        # ``assert elapsed < 6`` flaked at 6.018s on a loaded runner (16
        # shards, -n 4) where the event loop itself stalled for seconds.
        returned_while_thread_blocked = not thread_done.is_set()
    finally:
        release.set()
        ticker.cancel()
        # Await the cancelled task so its cancellation is fully processed before
        # the test returns (avoids "Task was destroyed but it is pending").
        await asyncio.gather(ticker, return_exceptions=True)

    assert client is None  # gave up; did not hang
    assert returned_while_thread_blocked  # bounded by wait_for, not the thread block
    assert ticks > 0  # the loop kept running other coroutines during init
    assert lt._langfuse_retry_after > 0  # transient-failure cooldown armed


@pytest.mark.asyncio
async def test_concurrent_callers_initialize_at_most_once(monkeypatch):
    monkeypatch.setattr(lt, "_LANGFUSE_AUTH_TIMEOUT", 0)
    calls = 0

    def _failing(_pk, _sk, _host):
        nonlocal calls
        calls += 1
        raise RuntimeError("auth failed")

    monkeypatch.setattr(lt, "_blocking_init_and_auth", _failing)

    results = await asyncio.gather(
        *[lt.get_langfuse_client(_FakeDB(_enabled_config())) for _ in range(5)]
    )

    assert all(r is None for r in results)
    # Single-flight: the lock winner runs the init and arms the cooldown; the
    # rest observe the cooldown on re-check and return without a second attempt.
    assert calls == 1


@pytest.mark.asyncio
async def test_init_runs_on_the_langfuse_pool_not_the_default_executor(monkeypatch):
    """The default executor also serves the DNS lookups of every new database
    and HTTP connection: a Langfuse host that never answers must not hold any
    of its threads."""
    ran_on: list[str] = []

    def _record(_pk, _sk, _host):
        ran_on.append(threading.current_thread().name)
        raise RuntimeError("auth failed")

    monkeypatch.setattr(lt, "_blocking_init_and_auth", _record)

    assert await lt.get_langfuse_client(_FakeDB(_enabled_config())) is None
    assert len(ran_on) == 1 and ran_on[0].startswith("xtmone-langfuse")


@pytest.mark.asyncio
async def test_reset_wins_against_in_flight_init(monkeypatch):
    # A config save / disable (areset_langfuse_client) must win against a
    # concurrent slow init: the in-flight get_langfuse_client must not
    # repopulate the cache with an old-config client *after* the reset clears
    # it. Serializing both under the single-flight init lock guarantees this.
    monkeypatch.setattr(lt, "_LANGFUSE_AUTH_TIMEOUT", 5)
    gate = threading.Event()
    stale_client = object()  # stands in for the old-config client

    def _blocking(_pk, _sk, _host):
        gate.wait(timeout=10)  # hold the init (and the lock) open
        return stale_client

    monkeypatch.setattr(lt, "_blocking_init_and_auth", _blocking)

    init_task = asyncio.create_task(lt.get_langfuse_client(_FakeDB(_enabled_config())))
    # Let the init acquire the single-flight lock and block inside to_thread.
    for _ in range(50):
        await asyncio.sleep(0)
        if lt._get_init_lock().locked():
            break
    assert lt._get_init_lock().locked(), "init should be holding the init lock"

    reset_task = asyncio.create_task(lt.areset_langfuse_client())
    await asyncio.sleep(0)  # reset is now blocked waiting on the same lock

    gate.set()  # init finishes, sets the stale client, releases the lock
    await asyncio.gather(init_task, reset_task)

    # The reset acquired the lock only after the init released it, then cleared
    # the cache — so the stale (old-config) client did not survive the reset.
    assert lt._langfuse_client is None


@pytest.mark.asyncio
async def test_decryption_failure_returns_none_not_raises(monkeypatch):
    # A corrupted / rotated Fernet token must NOT bubble out of best-effort
    # tracing and 500 the unrelated chat/assignment request - it arms the
    # cooldown and returns None like any other init failure.
    def _boom(_value):
        raise ValueError("InvalidToken")

    monkeypatch.setattr("app.core.security.decrypt_value", _boom)

    client = await lt.get_langfuse_client(_FakeDB(_enabled_config()))

    assert client is None
    assert lt._langfuse_retry_after > 0
