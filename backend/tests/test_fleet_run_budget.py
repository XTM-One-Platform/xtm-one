"""The optional per-fleet run budget (#1526, ``services/fleet_run_budget``).

A fleet over its runs/hour or tokens/hour budget has its due runs held (left
out of the claim, never failed), its owner gets one notice, and the runs are
claimed again once usage is back under the budget. A fleet without a budget
is dealt exactly as before.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.services import fleet_run_budget as budget_mod

_NOW = datetime(2026, 11, 23, 13, 0, tzinfo=UTC)


def test_normalized_budget_keeps_positive_bounds_only():
    assert budget_mod.normalized_budget(None) == {}
    assert budget_mod.normalized_budget({}) == {}
    assert budget_mod.normalized_budget({"runs_per_hour": 0, "tokens_per_hour": True}) == {}
    assert budget_mod.normalized_budget({"runs_per_hour": 5, "other": 3}) == {"runs_per_hour": 5}


def test_over_budget_reads_either_bound():
    assert budget_mod.over_budget(None, runs=1000, tokens=10**9) is None
    assert budget_mod.over_budget({"runs_per_hour": 10}, runs=9, tokens=0) is None
    assert "10 runs" in (budget_mod.over_budget({"runs_per_hour": 10}, runs=10, tokens=0) or "")
    assert "tokens" in (budget_mod.over_budget({"tokens_per_hour": 500}, runs=1, tokens=500) or "")


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _Session:
    """Answers the reads of ``hold_over_budget`` in order: the runs, the budgeted fleets, the usage."""

    def __init__(self, answers):
        self._answers = list(answers)
        self.reads = 0

    async def execute(self, _statement):
        self.reads += 1
        return _Result(self._answers.pop(0))


class _Execution:
    def __init__(self):
        self.id = uuid.uuid4()
        self.run_id = uuid.uuid4()
        self.payload = {"run_id": str(self.run_id)}


@pytest.fixture
def notices(monkeypatch):
    sent: list[tuple] = []

    async def _notify(fleet_id, reason, *, held, now):
        sent.append((fleet_id, reason, held))
        return True

    monkeypatch.setattr(budget_mod, "notify_over_budget_once", _notify)
    return sent


@pytest.mark.asyncio
async def test_a_fleet_over_its_runs_budget_has_its_runs_held_and_its_owner_told(notices):
    agent_a, agent_b, parent = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    fleet_a = uuid.uuid4()
    a0, a1, b0, child = (_Execution() for _ in range(4))
    session = _Session(
        [
            [
                (a0.run_id, agent_a, None),
                (a1.run_id, agent_a, None),
                (b0.run_id, agent_b, None),
                (child.run_id, agent_a, parent),
            ],
            [(agent_a, fleet_a, {"runs_per_hour": 10})],
            [(fleet_a, 10, 0)],
        ]
    )
    kept = await budget_mod.hold_over_budget(session, [a0, a1, b0, child], now=_NOW)
    # A's own runs wait; B's run and A's delegated child go ahead, in order.
    assert [e.id for e in kept] == [b0.id, child.id]
    assert len(notices) == 1
    assert notices[0][0] == fleet_a and notices[0][2] == 2


@pytest.mark.asyncio
async def test_the_held_runs_are_claimed_again_under_budget(notices):
    agent_a, fleet_a = uuid.uuid4(), uuid.uuid4()
    a0, a1 = _Execution(), _Execution()
    session = _Session(
        [
            [(a0.run_id, agent_a, None), (a1.run_id, agent_a, None)],
            [(agent_a, fleet_a, {"runs_per_hour": 10})],
            [(fleet_a, 9, 0)],
        ]
    )
    kept = await budget_mod.hold_over_budget(session, [a0, a1], now=_NOW)
    assert kept == [a0, a1]
    assert notices == []


@pytest.mark.asyncio
async def test_tokens_budget_holds_too(notices):
    agent_a, fleet_a = uuid.uuid4(), uuid.uuid4()
    a0 = _Execution()
    session = _Session(
        [
            [(a0.run_id, agent_a, None)],
            [(agent_a, fleet_a, {"tokens_per_hour": 1000})],
            [(fleet_a, 1, 1500)],
        ]
    )
    assert await budget_mod.hold_over_budget(session, [a0], now=_NOW) == []
    assert len(notices) == 1


@pytest.mark.asyncio
async def test_no_budget_changes_nothing_and_reads_no_usage(notices):
    agent_a = uuid.uuid4()
    a0, a1 = _Execution(), _Execution()
    session = _Session([[(a0.run_id, agent_a, None), (a1.run_id, agent_a, None)], []])
    assert await budget_mod.hold_over_budget(session, [a0, a1], now=_NOW) == [a0, a1]
    assert session.reads == 2
    assert notices == []


@pytest.mark.asyncio
async def test_nothing_due_reads_nothing():
    session = _Session([])
    assert await budget_mod.hold_over_budget(session, [], now=_NOW) == []
    assert session.reads == 0


def test_notice_key_is_per_fleet_and_hour():
    fleet = uuid.uuid4()
    assert budget_mod.notice_key(fleet, _NOW) == f"{budget_mod.NOTICE_KEY_PREFIX}{fleet}:2026-11-23T13"
