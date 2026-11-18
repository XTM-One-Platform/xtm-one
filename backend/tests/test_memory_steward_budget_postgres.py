"""The Memory Steward's model call on the Concierge budget (#8258).

Level 8's cycle runner sets ``trigger_context["concierge_cycle_id"]`` on a
Concierge run (#8261); a delegated child run takes its root run's. The pass's
model call reserves on that cycle before it starts and charges what it spent;
a run with no key, or a malformed one, is in no cycle and charges nothing.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.assignment import AssignmentRun
from app.models.health import HealthCycle
from app.services.health import budget
from app.services.health import memory_steward as steward
from app.services.health import memory_steward_deps as deps
from app.services.health.budget import current_month
from app.services.health.memory_history import Lane
from tests.memory_steward_world import entry, make_world, world_database

pytestmark = pytest.mark.integration

_USAGE = {"input_tokens": 1200, "output_tokens": 300}


@pytest.mark.parametrize(
    ("trigger_context", "expected"),
    [
        ({"concierge_cycle_id": "6f1c2a52-3d1f-4b3a-9c51-0d6f8b1e2a77"}, uuid.UUID("6f1c2a52-3d1f-4b3a-9c51-0d6f8b1e2a77")),
        ({}, None),
        (None, None),
        ({"concierge_cycle_id": "not-a-uuid"}, None),
        ({"concierge_cycle_id": 42}, None),
        ({"concierge_cycle_id": None}, None),
        (["concierge_cycle_id"], None),
    ],
)
def test_cycle_id_of_reads_only_a_well_formed_key(trigger_context, expected) -> None:
    assert deps.cycle_id_of(trigger_context) == expected


class _Llm:
    def __init__(self, usage=_USAGE, error: BaseException | None = None) -> None:
        self.calls = 0
        self.usage = usage
        self.error = error

    async def chat(self, **_kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            content='{"duplicates": [], "contradictions": [], "time_bound": []}', usage=self.usage, stop_reason="end_turn"
        )


async def _run(world, session, trigger_context, *, parent=None) -> uuid.UUID:
    run = AssignmentRun(agent_id=world.agent, trigger_context=trigger_context, parent_run_id=parent)
    session.add(run)
    await session.commit()
    return run.id


async def test_a_run_and_its_delegated_child_name_their_cycle() -> None:
    async with world_database() as (db, session):
        world = await make_world(session, db.sessions)
        cycle = uuid.uuid4()
        root = await _run(world, session, {"concierge_cycle_id": str(cycle)})
        child = await _run(world, session, {"type": "delegation"}, parent=root)
        missing = await _run(world, session, {"type": "manual"})
        malformed = await _run(world, session, {"concierge_cycle_id": "cycle-1"})

        assert [await deps.run_cycle_id(session, run) for run in (root, child, missing, malformed, None)] == [
            cycle,
            cycle,
            None,
            None,
            None,
        ]


@pytest.mark.parametrize("key", ["present", "missing", "malformed"])
async def test_the_model_call_is_charged_on_the_runs_cycle_only(monkeypatch, key: str) -> None:
    async with world_database() as (db, session):
        world = await make_world(session, db.sessions)
        cycle = HealthCycle(specialist="memory_steward", trigger="schedule", month=current_month())
        session.add(cycle)
        await session.commit()
        context = {
            "present": {"concierge_cycle_id": str(cycle.id)},
            "missing": {"type": "manual"},
            "malformed": {"concierge_cycle_id": "not-a-uuid"},
        }[key]
        run = await _run(world, session, context)
        await entry(world, "Prefers answers in French", user=world.owner, days_old=2)
        llm = _Llm()

        async def resolved(*_args, **_kwargs):
            return llm, "test-model"

        async def switched_on(_db):
            return True

        monkeypatch.setattr("app.llm.router.get_llm_for_user", resolved)
        monkeypatch.setattr(budget, "health_active", switched_on)

        async with db.sessions() as work:
            lane = Lane(world.agent, world.owner)
            candidates, _cursor = await steward._load_candidates(work, lane, 0)
            await steward._ask_model(work, lane, candidates, cycle_id=await deps.run_cycle_id(work, run))
            # A pass failing after the call keeps its charge: spend is a fact.
            await work.rollback()

        async with db.sessions() as check:
            row = await check.get(HealthCycle, cycle.id)
        assert llm.calls == 1
        assert (row.spent_tokens, row.reserved_tokens, row.reserved_cost_usd) == (
            (1500, 0, Decimal(0)) if key == "present" else (0, 0, Decimal(0))
        )


async def _call_on_a_cycle(monkeypatch, llm: _Llm) -> tuple[HealthCycle, int, BaseException | None]:
    """One model call charged on a fresh open cycle; the cycle row after it,
    the tokens the call reserved and what it raised."""
    async with world_database() as (db, session):
        world = await make_world(session, db.sessions)
        cycle = HealthCycle(specialist="memory_steward", trigger="schedule", month=current_month())
        session.add(cycle)
        await session.commit()
        await entry(world, "Prefers answers in French", user=world.owner, days_old=2)
        reserved: list[int] = []
        reserve = deps.reserve_model_call

        async def recorded(db_, cycle_id, *, tokens):
            reserved.append(tokens)
            return await reserve(db_, cycle_id, tokens=tokens)

        async def resolved(*_args, **_kwargs):
            return llm, "test-model"

        async def switched_on(_db):
            return True

        monkeypatch.setattr("app.llm.router.get_llm_for_user", resolved)
        monkeypatch.setattr(budget, "health_active", switched_on)
        monkeypatch.setattr(deps, "reserve_model_call", recorded)
        async with db.sessions() as work:
            lane = Lane(world.agent, world.owner)
            candidates, _cursor = await steward._load_candidates(work, lane, 0)
            raised = None
            try:
                await steward._ask_model(work, lane, candidates, cycle_id=cycle.id)
            except BaseException as e:
                raised = e
            await work.rollback()
        async with db.sessions() as check:
            return await check.get(HealthCycle, cycle.id), reserved[0], raised


async def test_a_call_reporting_no_usage_is_charged_its_reservation(monkeypatch) -> None:
    row, reserved, raised = await _call_on_a_cycle(monkeypatch, _Llm(usage=None))
    assert raised is None
    assert reserved > 0
    assert (row.spent_tokens, row.reserved_tokens) == (reserved, 0)


async def test_a_cancelled_call_gives_its_reservation_back(monkeypatch) -> None:
    import asyncio

    row, _reserved, raised = await _call_on_a_cycle(monkeypatch, _Llm(error=asyncio.CancelledError()))
    assert isinstance(raised, asyncio.CancelledError)
    assert (row.spent_tokens, row.reserved_tokens) == (0, 0)


async def test_a_failed_call_gives_its_reservation_back(monkeypatch) -> None:
    row, _reserved, raised = await _call_on_a_cycle(monkeypatch, _Llm(error=RuntimeError("provider down")))
    assert raised is None
    assert (row.spent_tokens, row.reserved_tokens) == (0, 0)


async def test_the_pass_in_flight_never_reads_the_ai_gate(monkeypatch) -> None:
    """The AI gate decides whether a cycle opens, not whether a run in it may
    finish (``tests/test_ai_toggle.py``)."""
    async with world_database() as (_db, session):

        async def gate_closed(_db):
            return "ai_disabled"

        async def switched_on(_db):
            return True

        monkeypatch.setattr(budget, "_gate", gate_closed)
        monkeypatch.setattr(budget, "health_active", switched_on)
        await deps.ensure_budget(session, None)
