"""The scheduler tick lock is a window marker.

The autonomous scheduler's Redis lock (``copilot:scheduler_tick_lock``) used
to be released in a ``finally`` block right after the tick completed
(typically <1s), so it only prevented *overlapping* ticks: every uvicorn
worker on every pod still ran the full tick once per its own 60s cycle
(N workers x M pods ticks per minute fleet-wide).

The fix holds the lock for its full 55s TTL — the key doubles as a "tick
already ran in this window" marker — restoring the "at most one tick per
~60s platform-wide" contract.  These tests pin the new semantics:

* WINDOW-1 — a worker that finds the key already set skips the tick.
* WINDOW-2 — once the key expires, the next worker to attempt runs the tick.
* WINDOW-3 — the lock is NEVER explicitly released after a successful tick
  (no ``delete``, no ``eval`` compare-and-delete): expiry is the only exit.
* WINDOW-4 — a tick that raises keeps the window marker (the failed window
  is not retried by a sibling worker) and the loop survives to the next
  cycle with the error logged.
* WINDOW-5 — the lock TTL stays strictly below the loop sleep interval, so
  a worker's next attempt always lands after its own key expired (a TTL >=
  the interval would silently skip every other window).
"""

from __future__ import annotations

import asyncio
import inspect
import re
from typing import Any

import pytest

from app import main

_LOCK_KEY = "copilot:scheduler_tick_lock"


class _FakeRedis:
    """Minimal async Redis stand-in that records every mutating call.

    TTL expiry is simulated manually via ``expire_now()`` so tests control
    the clock instead of sleeping.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.set_calls: list[dict[str, Any]] = []
        self.delete_calls: list[tuple[str, ...]] = []
        self.eval_calls: list[tuple[Any, ...]] = []

    async def set(
        self,
        key: str,
        value: str,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool | None:
        self.set_calls.append({"key": key, "value": value, "nx": nx, "ex": ex})
        if nx and key in self.store:
            return None  # redis-py returns None when SET NX loses
        self.store[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        self.delete_calls.append(keys)
        removed = 0
        for key in keys:
            if self.store.pop(key, None) is not None:
                removed += 1
        return removed

    async def eval(self, *args: Any, **kwargs: Any) -> int:
        self.eval_calls.append(args)
        return 0

    def expire_now(self) -> None:
        """Simulate the TTL elapsing."""
        self.store.pop(_LOCK_KEY, None)


def _wire_loop(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: _FakeRedis,
    max_cycles: int,
    tick_error: Exception | None = None,
    on_cycle_end: Any = None,
) -> dict[str, int]:
    """Patch the loop's collaborators and stop it after ``max_cycles``.

    Returns a counter dict with the number of tick executions.  The loop is
    exited by raising ``asyncio.CancelledError`` from the patched sleep —
    the loop's ``except Exception`` deliberately does not catch it, exactly
    like a real shutdown cancellation.
    """
    counters = {"ticks": 0, "sleeps": 0}

    async def _fake_redis_client() -> _FakeRedis:
        return fake_redis

    async def _fake_tick() -> int:
        counters["ticks"] += 1
        if tick_error is not None:
            raise tick_error
        return 0

    async def _fake_sleep(_seconds: float) -> None:
        counters["sleeps"] += 1
        if on_cycle_end is not None:
            on_cycle_end(counters["sleeps"])
        if counters["sleeps"] >= max_cycles:
            raise asyncio.CancelledError

    monkeypatch.setattr("app.core.redis.redis_client", _fake_redis_client)
    monkeypatch.setattr(
        "app.services.autonomous.run_autonomous_tick", _fake_tick
    )
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    return counters


async def test_second_attempt_within_window_skips_tick(monkeypatch):
    """WINDOW-1: while the key is alive, further acquire attempts must NOT
    run the tick — the key means "a tick already ran in this window"."""
    fake = _FakeRedis()
    counters = _wire_loop(monkeypatch, fake, max_cycles=3)

    with pytest.raises(asyncio.CancelledError):
        await main._autonomous_loop()

    # 3 loop cycles, but the key never expired: only the first cycle ticks.
    assert counters["sleeps"] == 3
    assert counters["ticks"] == 1
    # Every acquire attempt used SET NX with the 55s window TTL.
    assert all(c["nx"] is True and c["ex"] == 55 for c in fake.set_calls)
    assert all(c["key"] == _LOCK_KEY for c in fake.set_calls)


async def test_first_attempt_after_expiry_runs_tick(monkeypatch):
    """WINDOW-2: once the TTL elapses, the next worker to attempt wins the
    new window and runs the tick."""
    fake = _FakeRedis()

    def _expire_after_second_cycle(cycle: int) -> None:
        if cycle == 2:
            fake.expire_now()

    counters = _wire_loop(
        monkeypatch, fake, max_cycles=3, on_cycle_end=_expire_after_second_cycle
    )

    with pytest.raises(asyncio.CancelledError):
        await main._autonomous_loop()

    # Cycle 1 ticks, cycle 2 skips (key alive), key expires, cycle 3 ticks.
    assert counters["ticks"] == 2


async def test_lock_is_never_released_after_tick(monkeypatch):
    """WINDOW-3: after a successful tick the key must still be set and no
    explicit release (``delete`` or compare-and-delete ``eval``) may happen —
    TTL expiry is the only way the window closes."""
    fake = _FakeRedis()
    _wire_loop(monkeypatch, fake, max_cycles=1)

    with pytest.raises(asyncio.CancelledError):
        await main._autonomous_loop()

    assert _LOCK_KEY in fake.store, "window marker must survive the tick"
    assert fake.delete_calls == []
    assert fake.eval_calls == []


async def test_tick_exception_keeps_window_marker_and_loop_survives(
    monkeypatch, caplog
):
    """WINDOW-4: a tick that raises keeps the window marker (the failed
    window is deliberately not retried by another worker), the error is
    logged, and the loop continues to the next cycle."""
    fake = _FakeRedis()
    counters = _wire_loop(
        monkeypatch,
        fake,
        max_cycles=2,
        tick_error=RuntimeError("tick exploded"),
    )

    with (
        caplog.at_level("ERROR", logger="app.main"),
        pytest.raises(asyncio.CancelledError),
    ):
        await main._autonomous_loop()

    # The first tick raised; the loop logged, slept, attempted the second
    # window (key still held -> skip) and reached the second sleep.
    assert counters["ticks"] == 1
    assert counters["sleeps"] == 2
    assert _LOCK_KEY in fake.store, (
        "a crashed tick must keep the window marker so siblings do not "
        "retry (and multiply) the failure within the same window"
    )
    assert fake.delete_calls == []
    assert any(
        "Assignment scheduler error" in r.getMessage() for r in caplog.records
    )


def test_ttl_is_strictly_below_loop_sleep_interval():
    """WINDOW-5: the 55s TTL must stay strictly below the 60s sleep so each
    worker's next attempt lands after its own key expired.  A TTL >= the
    interval would make every worker find a live key on every attempt and
    silently skip windows."""
    src = inspect.getsource(main._autonomous_loop)

    ttl_match = re.search(r"ex=(\d+)", src)
    sleep_match = re.search(r"asyncio\.sleep\((\d+)\)", src)
    assert ttl_match is not None, "SET must carry an ex= TTL"
    assert sleep_match is not None, "loop must end with asyncio.sleep(<int>)"

    ttl = int(ttl_match.group(1))
    interval = int(sleep_match.group(1))
    assert ttl < interval, (
        f"lock TTL ({ttl}s) must be strictly below the loop interval "
        f"({interval}s) or workers would skip every other window"
    )
