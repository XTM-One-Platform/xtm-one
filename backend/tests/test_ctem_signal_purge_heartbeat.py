# SPDX-License-Identifier: Apache-2.0

"""#2652: a purge resync longer than the pause TTL keeps its job lock and intake pause; a dead job loses both."""

from __future__ import annotations

import asyncio
import time
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import ctem_signal_purge as purge

ADMIN = uuid.UUID("00000000-0000-4000-8000-000000000001")
#: One test second in real seconds: a 180 s resync runs 1.8 s, a 60 s TTL 0.6 s.
SCALE = 0.01


class _ExpiringRedis:
    """A Redis with key expiry on a scaled clock: set (nx, ex), get, exists, delete."""

    def __init__(self) -> None:
        self.data: dict[str, tuple[str, float | None]] = {}

    def _live(self, key: str) -> bool:
        item = self.data.get(key)
        if item is None:
            return False
        if item[1] is not None and time.monotonic() >= item[1]:
            del self.data[key]
            return False
        return True

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        if nx and self._live(key):
            return False
        self.data[key] = (value, None if ex is None else time.monotonic() + ex * SCALE)
        return True

    async def get(self, key: str) -> str | None:
        return self.data[key][0] if self._live(key) else None

    async def exists(self, key: str) -> int:
        return int(self._live(key))

    async def delete(self, key: str) -> int:
        return int(self.data.pop(key, None) is not None)


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> _ExpiringRedis:
    fake = _ExpiringRedis()

    async def _client() -> _ExpiringRedis:
        return fake

    async def _scaled_sleep(seconds: float) -> None:
        await asyncio.sleep(seconds * SCALE)

    monkeypatch.setattr(purge, "_redis", _client)
    monkeypatch.setattr(purge, "_sleep", _scaled_sleep)
    monkeypatch.setattr(purge, "PAUSE_TTL_SECONDS", 60)
    monkeypatch.setitem(purge._LOCAL, "paused", False)
    monkeypatch.setitem(purge._LOCAL, "status", None)
    return fake


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def commit(self) -> None:
        return None


def _patch_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _cases(sessions: Any, actor_id: Any, *, progress: Any) -> int:
        return 0

    async def _signals(sessions: Any, *, progress: Any) -> int:
        return 0

    async def _raised(db: Any) -> dict[str, int]:
        return {}

    async def _reraise(sessions: Any) -> dict[str, Any]:
        return {"recorded": 0}

    async def _noop(*args: Any, **kwargs: Any) -> None:
        return None

    from app.services import ctem_signal_resync

    monkeypatch.setattr(purge, "purge_cases", _cases)
    monkeypatch.setattr(purge, "purge_signals", _signals)
    monkeypatch.setattr(purge, "clear_raised_state", _raised)
    monkeypatch.setattr(purge, "_audit", _noop)
    monkeypatch.setattr(purge, "_announce", _noop)
    monkeypatch.setattr(purge, "_hold_recompute", _noop)
    monkeypatch.setattr(ctem_signal_resync, "reraise_held", _reraise)


@pytest.mark.asyncio
async def test_a_resync_three_ttls_long_stays_running_paused_and_alone(
    redis: _ExpiringRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_steps(monkeypatch)
    seen: list[tuple[str | None, bool, bool, bool]] = []

    async def _slow_source(**kw: Any) -> dict[str, Any]:
        # 180 test seconds, three TTLs: the state is read every 10 test seconds, as another pod would.
        for _ in range(18):
            await asyncio.sleep(10 * SCALE)
            await kw["on_read"]()
            status = await purge.read_status()
            seen.append(
                (
                    status and status["state"],
                    bool(await redis.exists(purge.PAUSE_KEY)),  # what every other pod reads
                    await purge._take_job(),  # a second purge
                    bool(await redis.exists(purge.JOB_LOCK_KEY)),
                )
            )
        await kw["progress"]("vulnerability", 18)
        return {"routes": {}}

    assert await purge._take_job() is True
    status = purge.new_status("long", SimpleNamespace(id=ADMIN), resync=True)
    done = await purge.run_job(status, ADMIN, sessions=_Session, resync=_slow_source)

    assert len(seen) == 18
    assert all(state == "running" for state, _, _, _ in seen)
    assert all(paused for _, paused, _, _ in seen), "intake reopened on another pod mid-resync"
    assert not any(second for _, _, second, _ in seen), "a second purge started mid-resync"
    assert all(lock for _, _, _, lock in seen)
    assert done["state"] == "done" and done["error"] is None
    progress = done["steps"]["resync"]["counts"]["progress"]
    assert progress["read"] == 18 and progress["routes_done"] == 1
    # Resumed and released at the end: intake runs again, the next purge may start.
    assert not await redis.exists(purge.PAUSE_KEY) and not await redis.exists(purge.JOB_LOCK_KEY)


@pytest.mark.asyncio
async def test_a_job_whose_process_died_reads_interrupted_within_one_ttl(redis: _ExpiringRedis) -> None:
    """No heartbeat runs (the process is gone): the lock and the pause end with their TTL."""
    assert await purge._take_job() is True
    await purge.pause_intake()
    status = purge.new_status("dead", SimpleNamespace(id=ADMIN), resync=True)
    status["step"] = "resync"
    status["steps"]["resync"]["state"] = "running"
    await purge._write_status(status)
    purge._LOCAL["paused"] = False  # another pod's view: only Redis says intake is paused

    assert (await purge.read_status())["state"] == "running"  # type: ignore[index]
    await asyncio.sleep(61 * SCALE)

    read = await purge.read_status()
    assert read is not None and read["state"] == "failed" and read["interrupted"] is True
    assert read["intake_paused"] is False
    assert await purge._take_job() is True


@pytest.mark.asyncio
async def test_the_heartbeat_stops_with_the_job(redis: _ExpiringRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_steps(monkeypatch)
    assert await purge._take_job() is True
    status = purge.new_status("short", SimpleNamespace(id=ADMIN), resync=False)
    await purge.run_job(status, ADMIN, sessions=_Session)
    await asyncio.sleep(25 * SCALE)  # past one heartbeat interval: nothing writes the keys back
    assert not await redis.exists(purge.JOB_LOCK_KEY) and not await redis.exists(purge.PAUSE_KEY)
    assert not [t for t in asyncio.all_tasks() if (t.get_name() or "").startswith("ctem-signal-purge-heartbeat")]


def test_the_route_count_matches_the_progress_reports_of_one_resync() -> None:
    from app.services import ctem_signal_resync as resync

    world = sum(1 for w in resync.WINDOWS if w.entity_type)
    # world routes, then pir, third_party, aikido, github, openaev_findings, then the three passes.
    assert world + 5 + 3 == resync.ROUTE_COUNT
