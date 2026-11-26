# SPDX-License-Identifier: Apache-2.0

"""A burst of platform events never dispatches more than a bounded few at once (#2084)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services import ctem_case_opening, platform_events


@pytest.mark.parametrize(
    ("pool", "overflow", "expected"),
    [(10, 10, 2), (15, 10, 2), (5, 0, 1), (0, 0, 1), (40, 40, 4)],
)
def test_the_dispatch_limit_is_a_tenth_of_the_pool_between_one_and_four(monkeypatch, pool, overflow, expected):
    from app.core.config import settings

    monkeypatch.setattr(settings, "db_pool_size", pool)
    monkeypatch.setattr(settings, "db_max_overflow", overflow)
    assert platform_events.dispatch_limit() == expected


async def test_a_burst_of_500_events_dispatches_at_most_the_limit_at_once(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "db_pool_size", 10)
    monkeypatch.setattr(settings, "db_max_overflow", 10)
    platform_events._dispatch_gates.clear()
    running = 0
    peak = 0
    done = 0

    async def fake_dispatch_event(name, payload):
        nonlocal running, peak, done
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        running -= 1
        done += 1
        return []

    monkeypatch.setattr(platform_events, "dispatch_event", fake_dispatch_event)
    events = [SimpleNamespace(name="ctem_case.opened", data={"case_id": str(i)}) for i in range(500)]
    await asyncio.gather(*(platform_events.dispatch(e) for e in events))
    assert done == 500
    assert peak == platform_events.dispatch_limit() == 2


async def test_the_recompute_and_a_drain_pass_take_the_same_vertical_lock():
    statements: list[tuple[str, dict]] = []

    class FakeSession:
        async def execute(self, statement, params=None):
            statements.append((str(statement), dict(params or {})))

    await ctem_case_opening.hold_vertical_lock(FakeSession(), "cyber")
    sql, params = statements[0]
    assert "pg_advisory_xact_lock" in sql
    assert params == {"key": "ctem-case-drain:cyber"}


def test_the_recompute_write_phase_holds_the_vertical_lock():
    import inspect

    from app.services import ctem_cases_source

    source = inspect.getsource(ctem_cases_source.recompute_for_user)
    assert source.index("hold_vertical_lock(db, vertical)") < source.index("store.apply_recompute(")
    assert source.index("lock_timeout") < source.index("hold_vertical_lock(db, vertical)")
