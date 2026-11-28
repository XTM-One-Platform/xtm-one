# SPDX-License-Identifier: Apache-2.0

"""#2774: the CTEM passes run off the scheduler tick, each within a deadline.

* the scheduler tick runs no CTEM pass any more; ``main.py`` starts their own task;
* a pass past its deadline and grace is cancelled, the next pass still runs;
* a cycle runs under one Redis lock, so one replica at a time;
* the overrun warning names the step that ran long, never '3a-ter' for a later pass.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any

import pytest

from app.services import autonomous
from app.services import ctem_background_passes as passes


def test_the_tick_runs_no_ctem_pass() -> None:
    source = inspect.getsource(autonomous._run_autonomous_tick_inner)
    for name in (
        "expire_acceptances",
        "drain_new",
        "drain_within",
        "paced_pass",
        "ctem_case_loop.sweep",
        "ctem_cases_refresh",
        "ctem_attack_surface",
        "ctem_evidence_health",
        "ctem_inventory_staleness",
        "ctem_ticketing",
    ):
        assert name not in source, name


def test_main_starts_and_stops_the_ctem_task() -> None:
    from app import main

    source = inspect.getsource(main)
    assert "run_loop as run_ctem_passes" in source
    assert "_ctem_passes_task.cancel()" in source


def test_every_pass_of_the_former_segment_is_listed_in_order() -> None:
    assert [name for name, _ in passes.PASSES] == [
        "case expiry",
        "new-signal recheck",
        "signal inbox",
        "paced case opening",
        "case sweep",
        "cases recompute",
        "attack surface",
        "evidence health",
        "inventory evaluation",
        "ticketing sync",
    ]


async def test_a_slow_pass_is_cancelled_and_the_next_one_runs(monkeypatch: Any, caplog: Any) -> None:
    monkeypatch.setattr(passes, "PASS_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr(passes, "PASS_GRACE_SECONDS", 0.05)
    ran: list[str] = []

    async def stuck(_deadline: float) -> None:
        await asyncio.sleep(60)

    async def quick(deadline: float) -> None:
        assert deadline > time.monotonic()
        ran.append("quick")

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger=passes.logger.name):
        took = await passes.run_passes((("stuck", stuck), ("quick", quick)))
    assert time.monotonic() - started < 1.0
    assert ran == ["quick"]
    assert set(took) == {"stuck", "quick"}
    assert any("CTEM pass 'stuck' cancelled past its" in r.getMessage() for r in caplog.records)


async def test_a_failing_pass_never_stops_the_cycle(caplog: Any) -> None:
    ran: list[str] = []

    async def broken(_deadline: float) -> None:
        raise RuntimeError("OpenCRQ down")

    async def after(_deadline: float) -> None:
        ran.append("after")

    with caplog.at_level(logging.ERROR, logger=passes.logger.name):
        await passes.run_passes((("broken", broken), ("after", after)))
    assert ran == ["after"]
    assert any("CTEM pass 'broken' error" in r.getMessage() for r in caplog.records)


class _Redis:
    def __init__(self, held: bool = False) -> None:
        self.held = held
        self.released: list[str] = []

    async def set(self, key: str, token: str, *, nx: bool, ex: int) -> bool:
        assert key == passes.LOCK_KEY and nx and ex == passes.LOCK_SECONDS
        if self.held:
            return False
        self.held = True
        return True

    async def eval(self, _script: str, _n: int, key: str, token: str) -> int:
        self.released.append(key)
        self.held = False
        return 1


@pytest.mark.parametrize("held", [False, True])
async def test_one_replica_runs_a_cycle_at_a_time(monkeypatch: Any, held: bool) -> None:
    from app.core import redis as redis_module

    redis = _Redis(held=held)

    async def client() -> _Redis:
        return redis

    monkeypatch.setattr(redis_module, "redis_client", client)
    ran: list[bool] = []

    async def run_passes(*_a: Any, **_kw: Any) -> dict[str, float]:
        ran.append(True)
        return {}

    monkeypatch.setattr(passes, "run_passes", run_passes)
    assert await passes.run_cycle() is (not held)
    assert ran == ([] if held else [True])
    assert redis.released == ([] if held else [passes.LOCK_KEY])


async def test_the_tick_stays_in_its_window_with_a_slow_ctem_backlog(monkeypatch: Any) -> None:
    # The tick no longer awaits a CTEM pass: a recorder that takes minutes per row cannot reach it.
    from app.services import ctem_signal_inbox

    async def slow_drain(*_a: Any, **_kw: Any) -> dict[str, int]:
        await asyncio.sleep(3600)
        return {}

    monkeypatch.setattr(ctem_signal_inbox, "drain_within", slow_drain)
    assert "drain_within" not in inspect.getsource(autonomous._run_autonomous_tick_inner)


def test_the_overrun_warning_names_the_step_that_ran_long(monkeypatch: Any, caplog: Any) -> None:
    clock = {"now": 0.0}
    monkeypatch.setattr(autonomous.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(autonomous, "_schedule_trouble", None)
    tick = autonomous._TickClock()
    monkeypatch.setattr(autonomous, "_tick_clock", tick)
    tick.mark("3a-ter")
    clock["now"] += 1.0
    tick.mark("3a-quater blocked references")
    clock["now"] += 90.0
    tick.mark("3b")
    with caplog.at_level(logging.WARNING, logger=autonomous.logger.name):
        autonomous._warn_if_tick_overran()
    line = next(r.getMessage() for r in caplog.records if "overran" in r.getMessage())
    assert "slowest step '3a-quater blocked references'" in line
    assert "'3a-ter'" not in line


def test_the_tick_marks_each_pass_after_the_releases() -> None:
    source = inspect.getsource(autonomous._run_autonomous_tick_inner)
    for step in (
        "3a-quater assignment retirement",
        "3a-quater decision expiry",
        "3a-quater legacy claim release",
        "3a-quater dependency release",
        "3a-quater blocked references",
        "3a-quater notifications",
        "3a-quater timers",
        "3a-quater event retries",
    ):
        assert f'_tick_clock.mark("{step}")' in source, step
