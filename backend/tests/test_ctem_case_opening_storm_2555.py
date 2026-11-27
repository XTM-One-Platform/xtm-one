# SPDX-License-Identifier: Apache-2.0

"""Turning CTEM case opening on with a large backlog keeps the platform up (#2555).

- A burst of case events retires the viewer views about once per window, never once per event.
- A drain pass never blocks on the vertical lock: a busy lock backs off and retries, bounded.
- The readiness probe reads the database through its own one-connection engine.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from app.services import ctem_case_opening as case_opening
from app.services import ctem_cases_view as view_of


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.incrs = 0

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: Any, ex: Any = None, nx: bool = False) -> bool:
        if nx and key in self.data:
            return False
        self.data[key] = str(value)
        return True

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)

    async def incr(self, key: str) -> int:
        self.incrs += 1
        self.data[key] = str(int(self.data.get(key) or 0) + 1)
        return int(self.data[key])


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    client = FakeRedis()

    async def _redis() -> FakeRedis:
        return client

    monkeypatch.setattr(view_of, "_redis", _redis)
    monkeypatch.setattr(view_of, "VIEW_BUMP_INTERVAL_SECONDS", 0.01)
    view_of._TRAILING_BUMPS.clear()
    return client


async def _settle() -> None:
    for task in list(view_of._TRAILING_BUMPS.values()):
        await task


async def test_a_burst_of_1000_case_events_bumps_the_generation_twice(redis: FakeRedis) -> None:
    for _ in range(1000):
        await view_of.forget_all_views("cyber")
    # The first event bumps at once; the other 999 mark the vertical pending, one trailing task.
    assert redis.incrs == 1
    assert len(view_of._TRAILING_BUMPS) == 1
    # The window ends (the fake never expires keys): the trailing bump takes the next one.
    redis.data.pop(view_of._bump_window_key("cyber"))
    await _settle()
    assert redis.incrs == 2
    assert view_of._bump_pending_key("cyber") not in redis.data


async def test_the_last_change_of_a_burst_is_never_lost(redis: FakeRedis) -> None:
    await view_of.forget_all_views("cyber")
    await view_of.forget_all_views("cyber")
    redis.data.pop(view_of._bump_window_key("cyber"))
    await _settle()
    assert int(redis.data[view_of._generation_key("cyber")]) == 2


async def test_a_trailing_bump_with_nothing_pending_bumps_nothing(redis: FakeRedis) -> None:
    await view_of.forget_all_views("cyber")
    await view_of.forget_all_views("cyber")
    # Another pod's trailing bump already retired the views and cleared the mark.
    redis.data.pop(view_of._bump_pending_key("cyber"))
    redis.data.pop(view_of._bump_window_key("cyber"))
    await _settle()
    assert redis.incrs == 1


async def test_each_vertical_has_its_own_window(redis: FakeRedis) -> None:
    await view_of.forget_all_views("cyber")
    await view_of.forget_all_views("exposure")
    assert redis.incrs == 2


async def _no_sleep(_seconds: float) -> None:
    return None


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None


async def test_a_busy_vertical_lock_backs_off_and_the_drain_goes_on(monkeypatch: pytest.MonkeyPatch) -> None:
    answers: list[dict[str, int] | None] = [None, None, {"opened": 3, "joined": 1, "skipped": 0}]
    answers.append({"opened": 0, "joined": 0, "skipped": 0})
    sleeps: list[float] = []

    async def drain_pass(_db: Any, _vertical: str) -> dict[str, int] | None:
        return answers.pop(0)

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(case_opening, "drain_pass", drain_pass)
    monkeypatch.setattr("app.services.ctem_case_opening.asyncio.sleep", sleep)
    totals = await case_opening.drain("cyber", session_factory=_Session)
    assert totals == {"passes": 2, "opened": 3, "joined": 1, "skipped": 0}
    # Two busy answers back off 0.5 then 1.0 s; the second pass waits the pass pause.
    assert sleeps == [0.5, 1.0, case_opening.DRAIN_PASS_PAUSE_SECONDS]


async def test_a_lock_busy_past_the_budget_stops_the_drain_with_a_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def busy(_db: Any, _vertical: str) -> None:
        return None

    monkeypatch.setattr(case_opening, "drain_pass", busy)
    monkeypatch.setattr("app.services.ctem_case_opening.asyncio.sleep", _no_sleep)
    monkeypatch.setattr(case_opening, "DRAIN_BUSY_BUDGET_SECONDS", 0.0)
    totals = await case_opening.drain("cyber", session_factory=_Session)
    assert totals == {"passes": 0, "opened": 0, "joined": 0, "skipped": 0}
    assert "vertical lock busy" in caplog.text


async def test_a_drain_pass_tries_the_lock_and_never_waits_on_it() -> None:
    statements: list[str] = []

    class Busy:
        async def scalar(self, statement: Any, params: Any = None) -> bool:
            statements.append(str(statement))
            return False

        async def rollback(self) -> None:
            statements.append("ROLLBACK")

    assert await case_opening.drain_pass(Busy(), "cyber") is None  # type: ignore[arg-type]
    assert "pg_try_advisory_xact_lock" in statements[0]
    assert statements[-1] == "ROLLBACK"
    assert "pg_advisory_xact_lock(" not in inspect.getsource(case_opening.drain_pass)


def test_the_readiness_probe_reads_the_database_on_its_own_one_connection_engine() -> None:
    from app import main
    from app.core import database

    source = inspect.getsource(main.health_ready)
    assert "health_engine().connect()" in source
    assert "engine.connect()" not in source.replace("health_engine().connect()", "")
    engine = database.health_engine()
    assert engine is database.health_engine()
    assert engine is not database.engine
    assert engine.pool.size() == 1
