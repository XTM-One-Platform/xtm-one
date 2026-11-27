# SPDX-License-Identifier: Apache-2.0

"""A platform caught in a case-opened storm recovers on its own (#2570).

- Case opening is rate-limited per vertical: a burst opens at most the per-minute budget, the rest
  stays confirmed and opens on the next passes; a signal that joins an open case takes no token.
- A drain stops when the budget is spent; the paced pass of the autonomous tick opens the rest.
- A CTEM platform-event delivery past its assignment's queue limit is dropped and counted once,
  never queued past the limit; any other platform event keeps its persisted overflow.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services import ctem_case_opening as case_opening
from app.services import platform_events as pe

TESTS_CASE_OPENING_BUDGET = True


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, int | str] = {}

    async def get(self, key: str) -> Any:
        return self.data.get(key)

    async def incr(self, key: str) -> int:
        self.data[key] = int(self.data.get(key) or 0) + 1
        return int(self.data[key])

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def set(self, key: str, value: Any, ex: Any = None, nx: bool = False) -> bool:
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    client = FakeRedis()

    async def _redis() -> FakeRedis:
        return client

    monkeypatch.setattr(case_opening, "_redis", _redis)
    monkeypatch.setenv("CTEM_CASE_OPEN_PER_MINUTE", "10")
    return client


async def test_a_burst_of_1400_openings_takes_ten_tokens_a_minute(redis: FakeRedis) -> None:
    taken = [await case_opening.take_opening_token("cyber") for _ in range(1400)]
    assert taken.count(True) == 10
    assert await case_opening.budget_left("cyber") == 0
    # Another vertical has its own budget.
    assert await case_opening.take_opening_token("ot") is True


async def test_the_budget_is_per_minute(redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [600.0]
    import time

    monkeypatch.setattr(time, "time", lambda: clock[0])
    for _ in range(10):
        assert await case_opening.take_opening_token("cyber")
    assert not await case_opening.take_opening_token("cyber")
    clock[0] += 60
    assert await case_opening.take_opening_token("cyber")


async def test_zero_turns_the_limit_off(redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CTEM_CASE_OPEN_PER_MINUTE", "0")
    assert all([await case_opening.take_opening_token("cyber") for _ in range(50)])
    assert await case_opening.budget_left("cyber") is None


async def test_without_redis_the_budget_is_kept_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    async def down() -> Any:
        raise ConnectionError("redis down")

    monkeypatch.setattr(case_opening, "_redis", down)
    monkeypatch.setenv("CTEM_CASE_OPEN_PER_MINUTE", "3")
    case_opening._LOCAL_BUDGET.clear()
    assert [await case_opening.take_opening_token("cyber") for _ in range(5)] == [True, True, True, False, False]
    assert await case_opening.budget_left("cyber") == 0
    case_opening._LOCAL_BUDGET.clear()


async def test_a_drain_stops_when_the_budget_is_spent(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = [{"opened": 10, "joined": 2, "skipped": 0, "throttled": 1}, {"opened": 10, "joined": 0, "skipped": 0}]

    async def locked_pass(_vertical: str, _factory: Any) -> dict[str, int]:
        return answers.pop(0)

    monkeypatch.setattr(case_opening, "_locked_pass", locked_pass)
    totals = await case_opening.drain("cyber", session_factory=object())
    assert totals == {"passes": 1, "opened": 10, "joined": 2, "skipped": 0, "throttled": 1}
    assert len(answers) == 1


async def test_the_paced_pass_skips_the_database_when_the_budget_is_spent(redis: FakeRedis) -> None:
    for _ in range(10):
        await case_opening.take_opening_token("cyber")

    def no_session() -> Any:
        raise AssertionError("no session when the budget is spent")

    answer = await case_opening.paced_pass("cyber", session_factory=no_session)
    assert answer is not None and answer["throttled"] == 1


async def test_the_paced_pass_runs_once_a_minute(redis: FakeRedis) -> None:
    assert await case_opening.paced_pass_due() is True
    assert await case_opening.paced_pass_due() is False


async def test_open_waiting_stops_on_a_spent_budget(redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import ctem_case_loop as loop

    for _ in range(10):
        await case_opening.take_opening_token("cyber")

    class Result:
        def scalars(self) -> Result:
            return self

        def all(self) -> list[Any]:
            return [object(), object()]

    class Db:
        async def execute(self, _stmt: Any) -> Result:
            return Result()

    async def never(*_a: Any, **_k: Any) -> None:
        raise AssertionError("no opening past the budget")

    monkeypatch.setattr(loop, "open_from_signal", never)
    counts = {"opened": 0, "joined": 0, "skipped": 0}
    throttled = await loop.open_waiting(Db(), vertical="cyber", events=loop.PendingEvents(), now=None, counts=counts)  # type: ignore[arg-type]
    assert throttled is True and counts == {"opened": 0, "joined": 0, "skipped": 0}


def test_a_re_raised_delivery_past_the_queue_limit_is_dropped_any_other_persisted() -> None:
    assert pe.delivery_overflow("ctem_case.opened") == "drop_newest"
    assert pe.delivery_overflow("ctem_case.stalled") == "drop_newest"
    # Raised once: a lost delivery is never heard again (review of #2574).
    for once in (
        "ctem_case.sla_breached",
        "ctem_case.resolved",
        "ctem_case.reopened",
        "ctem_case.opening_changed",
        "ctem_signal.confirmed",
    ):
        assert pe.delivery_overflow(once) == "persist", once
    assert pe.delivery_overflow("work_item.note_added") == "persist"


def test_every_re_raised_event_is_a_known_platform_event() -> None:
    assert pe.RE_RAISED_EVENTS.issubset(pe.PLATFORM_EVENTS)
