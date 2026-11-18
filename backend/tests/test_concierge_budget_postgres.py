"""Platform Concierge level 1 (#8254): the budget line against PostgreSQL.

The licence gate first, caps per cycle and per month, two concurrent
reservations at the cap, charge, release, close, stale cycles.

``integration``-marked; skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.core.config import settings
from app.models.health import ConciergeBudgetMonth, ConciergeCycle
from app.services import agentic_gate
from app.services.health import budget
from app.services.health.settings import load_concierge_settings
from tests.concierge_support import World, concierge_database, configure

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setattr(settings, "enabled_features", "CONCIERGE")

@pytest.fixture
def licensed(monkeypatch):
    async def _verdict(_db):
        return agentic_gate.AgenticVerdict(refusal=None)

    monkeypatch.setattr(agentic_gate, "agentic_verdict", _verdict)


# ── The budget ────────────────────────────────────────────────────────────


async def test_no_cycle_opens_while_the_concierge_is_off_or_unlicensed(monkeypatch) -> None:
    async with concierge_database() as sessions:
        world = await World().build(sessions)
        async with sessions() as db:
            with pytest.raises(budget.BudgetRefusedError) as off:
                await budget.open_cycle(db, specialist="experimenter", trigger="schedule")
            assert off.value.code == "concierge_disabled"
        await configure(sessions, world.admin, enabled=True)

        async def _unlicensed(_db):
            return agentic_gate.AgenticVerdict(refusal="license_required")

        monkeypatch.setattr(agentic_gate, "agentic_verdict", _unlicensed)
        async with sessions() as db:
            assert await budget.budget_refusal(db) == "license_required"
            with pytest.raises(budget.BudgetRefusedError) as unlicensed:
                await budget.open_cycle(db, specialist="experimenter", trigger="schedule")
            assert unlicensed.value.message == "Agentic AI is not licensed on this platform."
            assert (await db.execute(select(sa.func.count(ConciergeCycle.id)))).scalar_one() == 0


async def test_reserve_charge_release_and_close(licensed) -> None:
    async with concierge_database() as sessions:
        world = await World().build(sessions)
        caps = {"cycle_tokens": 100, "cycle_cost_usd": "1", "month_tokens": 150, "month_cost_usd": "10"}
        await configure(sessions, world.admin, enabled=True, budget=caps)
        async with sessions() as db:
            cycle = await budget.open_cycle(db, specialist="experimenter", trigger="signal")
            first = await budget.reserve(db, cycle, tokens=60, cost_usd=Decimal("0.2"))
            with pytest.raises(budget.BudgetRefusedError) as over:
                await budget.reserve(db, cycle, tokens=41, cost_usd=Decimal("0"))
            assert over.value.code == "cycle_budget_reached"
            await budget.charge(db, cycle, tokens=30, cost_usd=Decimal("0.1"), reservation=first)
            left = await budget.remaining(db, cycle)
            assert (left.cycle_tokens, left.cycle_cost_usd, left.month_tokens) == (70, Decimal("0.9"), 120)
            second = await budget.reserve(db, cycle, tokens=50, cost_usd=Decimal("0.5"))
            await budget.release(db, second)
            await budget.reserve(db, cycle, tokens=20, cost_usd=Decimal("0"))
            await budget.close_cycle(db, cycle, status="completed")
            await budget.close_cycle(db, cycle, status="failed")
            await db.commit()
            row = await db.get(ConciergeCycle, cycle)
            month = await db.get(ConciergeBudgetMonth, budget.current_month())
            assert (row.status, row.spent_tokens, row.reserved_tokens) == ("completed", 30, 0)
            assert (month.spent_tokens, month.reserved_tokens, month.spent_cost_usd) == (30, 0, Decimal("0.1"))
            with pytest.raises(budget.BudgetRefusedError) as closed:
                await budget.reserve(db, cycle, tokens=1, cost_usd=Decimal("0"))
            assert closed.value.code == "cycle_closed"
            # A late charge is still booked: spend is a fact.
            await budget.charge(db, cycle, tokens=200, cost_usd=Decimal("0"))
            await db.commit()
            assert await budget.budget_refusal(db) == "month_budget_reached"
            with pytest.raises(budget.BudgetRefusedError):
                await budget.open_cycle(db, specialist="memory_steward", trigger="schedule")


async def test_two_concurrent_reservations_never_pass_the_cap_together(licensed) -> None:
    async with concierge_database() as sessions:
        world = await World().build(sessions)
        caps = {"cycle_tokens": None, "cycle_cost_usd": None, "month_tokens": 100, "month_cost_usd": None}
        await configure(sessions, world.admin, enabled=True, budget=caps)
        async with sessions() as db:
            cycles = [await budget.open_cycle(db, specialist="experimenter", trigger="schedule") for _ in range(2)]
            await db.commit()

        async def _try(cycle_id) -> str:
            async with sessions() as db:
                try:
                    await budget.reserve(db, cycle_id, tokens=60, cost_usd=Decimal("0"))
                except budget.BudgetRefusedError as refused:
                    return refused.code
                await asyncio.sleep(0.2)
                await db.commit()
                return "reserved"

        outcomes = await asyncio.gather(*(_try(c) for c in cycles))
        assert sorted(outcomes) == ["month_budget_reached", "reserved"]
        async with sessions() as db:
            assert (await db.get(ConciergeBudgetMonth, budget.current_month())).reserved_tokens == 60


async def test_a_stale_cycle_is_closed_and_gives_its_reservation_back(licensed) -> None:
    async with concierge_database() as sessions:
        world = await World().build(sessions)
        await configure(sessions, world.admin, enabled=True)
        async with sessions() as db:
            stale = await budget.open_cycle(db, specialist="flow_lab", trigger="on_demand")
            fresh = await budget.open_cycle(db, specialist="flow_lab", trigger="on_demand")
            await budget.reserve(db, stale, tokens=10, cost_usd=Decimal("0"))
            (await db.get(ConciergeCycle, stale)).opened_at = datetime.now(UTC) - timedelta(days=2)
            await db.commit()
            assert await budget.close_stale_cycles(db, older_than=timedelta(days=1)) == 1
            await db.commit()
            assert (await db.get(ConciergeCycle, stale)).status == "failed"
            assert (await db.get(ConciergeCycle, fresh)).status == "open"
            assert (await db.get(ConciergeBudgetMonth, budget.current_month())).reserved_tokens == 0
            assert (await load_concierge_settings(db)).enabled is True


async def test_a_late_charge_after_close_keeps_the_other_cycles_reservations(licensed) -> None:
    async with concierge_database() as sessions:
        world = await World().build(sessions)
        caps = {"cycle_tokens": None, "cycle_cost_usd": None, "month_tokens": 100, "month_cost_usd": None}
        await configure(sessions, world.admin, enabled=True, budget=caps)
        async with sessions() as db:
            stale = await budget.open_cycle(db, specialist="experimenter", trigger="schedule")
            other = await budget.open_cycle(db, specialist="experimenter", trigger="schedule")
            held = await budget.reserve(db, stale, tokens=60, cost_usd=Decimal("0"))
            await budget.reserve(db, other, tokens=30, cost_usd=Decimal("0"))
            await budget.close_cycle(db, stale, status="failed", reason="worker lost")
            await budget.charge(db, stale, tokens=10, cost_usd=Decimal("0"), reservation=held)
            await budget.release(db, held)
            await db.commit()
            month = await db.get(ConciergeBudgetMonth, budget.current_month())
            assert (month.reserved_tokens, month.spent_tokens) == (30, 10)
            with pytest.raises(budget.BudgetRefusedError) as over:
                await budget.reserve(db, other, tokens=61, cost_usd=Decimal("0"))
            assert over.value.code == "month_budget_reached"


async def test_work_that_outlives_its_cycle_keeps_its_reservation_until_charged_once(licensed) -> None:
    import uuid

    async with concierge_database() as sessions:
        world = await World().build(sessions)
        caps = {"cycle_tokens": None, "cycle_cost_usd": None, "month_tokens": 100, "month_cost_usd": None}
        await configure(sessions, world.admin, enabled=True, budget=caps)
        async with sessions() as db:
            cycle = await budget.open_cycle(db, specialist="experimenter", trigger="signal")
            evaluation = ("evaluation", uuid.uuid4())
            await budget.reserve(db, cycle, tokens=50, cost_usd=Decimal("0"), hold=evaluation)
            await budget.reserve(db, cycle, tokens=20, cost_usd=Decimal("0"))
            await budget.close_cycle(db, cycle, status="completed")
            await db.commit()
            month = await db.get(ConciergeBudgetMonth, budget.current_month())
            # The evaluation still runs: its 50 stay set aside, the other 20 go back.
            assert (month.reserved_tokens, (await db.get(ConciergeCycle, cycle)).reserved_tokens) == (50, 50)
            other = await budget.open_cycle(db, specialist="experimenter", trigger="signal")
            with pytest.raises(budget.BudgetRefusedError):
                await budget.reserve(db, other, tokens=51, cost_usd=Decimal("0"))
            (held,) = await budget.held_reservations(db, "evaluation")
            assert (held.cycle_id, held.tokens, held.hold) == (cycle, 50, evaluation)
            assert await budget.charge(db, cycle, tokens=30, cost_usd=Decimal("0"), reservation=held) is True
            assert await budget.charge(db, cycle, tokens=30, cost_usd=Decimal("0"), reservation=held) is False
            await db.commit()
            await db.refresh(month)
            assert (month.reserved_tokens, month.spent_tokens) == (0, 30)
            assert await budget.held_reservations(db, "evaluation") == []


async def test_a_run_in_flight_reserves_without_asking_the_ai_gate(monkeypatch, licensed) -> None:
    async with concierge_database() as sessions:
        world = await World().build(sessions)
        await configure(sessions, world.admin, enabled=True)
        async with sessions() as db:
            cycle = await budget.open_cycle(db, specialist="experimenter", trigger="schedule")
            await db.commit()

        async def _ai_off(_db):
            raise AssertionError("a run in flight must not read the AI gate")

        monkeypatch.setattr(agentic_gate, "agentic_verdict", _ai_off)
        async with sessions() as db:
            reservation = await budget.reserve(db, cycle, tokens=10, cost_usd=Decimal("0.01"))
            assert reservation.tokens == 10
            assert (await budget.remaining(db, cycle)).refusal is None
