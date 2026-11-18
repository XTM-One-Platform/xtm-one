"""Flow shadows on the Concierge's budget, consent and switches, and locked decisions (#8260), on PostgreSQL.

On the shadow suite's fixtures (``test_health_flow_shadow_postgres``):

* every paired run is reserved on the Concierge's own budget line and
  charged when its pair settles; a spent budget stops the pairing, cursor
  kept; a shadow pairs a bounded number of runs;
* a live run whose consent no longer holds, or that its trigger did not
  start, is passed over;
* with the Concierge switched off, a held switch-over waits (then is given
  up at the deadline) while a regressing watch still rolls back;
* the tick re-reads and locks the assignment before deciding.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from app.models.assignment import Assignment, AssignmentRun
from app.models.flow import Flow
from app.models.flow_run import FlowRun
from app.models.flow_shadow import PAIR_STATUS_SKIPPED, SHADOW_STATUS_RUNNING, FlowShadow, FlowShadowPair
from app.models.health import HealthBudgetMonth, HealthCycle
from app.models.llm_trace import LLMTrace
from app.services.health import flow_shadow
from tests.test_health_flow_shadow_postgres import (
    FakeSandbox,
    _advance,
    _assignment_run,
    _database,
    _seed,
    _shadow,
    install_fake_sandbox,
)
from tests.test_health_flow_switch_over_postgres import _watching

pytestmark = pytest.mark.integration


@pytest.fixture
def sandbox(monkeypatch) -> FakeSandbox:
    return install_fake_sandbox(monkeypatch)


def _sized_run(seeded: dict, *, created_at: datetime, **fields) -> AssignmentRun:
    run = _assignment_run(seeded, created_at=created_at, **fields)
    run.total_input_tokens, run.total_output_tokens = 1_000, 500
    return run


async def test_each_paired_run_is_reserved_on_the_concierge_budget_and_charged_when_it_settles(sandbox):
    sandbox.run_status = "running"
    async with _database() as sessions:
        seeded = await _seed(sessions, sandbox)
        shadow_id = await _shadow(sessions, seeded)
        start = datetime.now(UTC) - timedelta(days=2)
        async with sessions() as db:
            db.add_all([_sized_run(seeded, created_at=start + timedelta(hours=i)) for i in range(2)])
            await db.commit()

        assert await _advance(sessions, shadow_id) == SHADOW_STATUS_RUNNING

        async with sessions() as db:
            shadow = await db.get(FlowShadow, shadow_id)
            cycle = await db.get(HealthCycle, shadow.cycle_id)
            # Sized on the live run each copies: 1 500 tokens apiece.
            assert (cycle.specialist, cycle.status, cycle.reserved_tokens) == ("flow_lab", "open", 3_000)
            assert (await db.get(HealthBudgetMonth, cycle.month)).reserved_tokens == 3_000
            for run in (await db.execute(select(FlowRun))).scalars():
                run.status, run.completed_at = "completed", datetime.now(UTC)
                db.add(
                    LLMTrace(
                        source="flow",
                        name="flow",
                        started_at=run.created_at,
                        metadata_json={"flow_run_id": str(run.id)},
                        input_tokens=700,
                        output_tokens=100,
                        total_cost=Decimal("0.02"),
                    )
                )
            await db.commit()

        assert await _advance(sessions, shadow_id) == SHADOW_STATUS_RUNNING

        async with sessions() as db:
            cycle = await db.get(HealthCycle, (await db.get(FlowShadow, shadow_id)).cycle_id)
            assert (cycle.reserved_tokens, cycle.spent_tokens, cycle.spent_cost_usd) == (0, 1_600, Decimal("0.04"))
            assert {p.reservation for p in (await db.execute(select(FlowShadowPair))).scalars()} == {None}


async def test_rotating_a_shadow_s_cycle_keeps_the_reservations_of_runs_still_going(sandbox):
    sandbox.run_status = "running"
    async with _database() as sessions:
        seeded = await _seed(sessions, sandbox)
        shadow_id = await _shadow(sessions, seeded)
        start = datetime.now(UTC) - timedelta(days=2)
        async with sessions() as db:
            db.add_all([_sized_run(seeded, created_at=start + timedelta(hours=i)) for i in range(2)])
            await db.commit()
        assert await _advance(sessions, shadow_id) == SHADOW_STATUS_RUNNING
        async with sessions() as db:
            first = await db.get(HealthCycle, (await db.get(FlowShadow, shadow_id)).cycle_id)
            first.opened_at = datetime.now(UTC) - flow_shadow.SHADOW_CYCLE_SPAN - timedelta(minutes=1)
            db.add(_sized_run(seeded, created_at=start + timedelta(hours=3)))
            await db.commit()
            first_id, month = first.id, first.month

        assert await _advance(sessions, shadow_id) == SHADOW_STATUS_RUNNING

        async with sessions() as db:
            first = await db.get(HealthCycle, first_id)
            assert first.status == "completed"
            # The two runs still going keep their 3 000 tokens past the rotation.
            assert (await db.get(HealthBudgetMonth, month)).reserved_tokens == 4_500
            for run in (await db.execute(select(FlowRun))).scalars():
                run.status, run.completed_at = "completed", datetime.now(UTC)
            await db.commit()

        assert await _advance(sessions, shadow_id) == SHADOW_STATUS_RUNNING

        async with sessions() as db:
            assert (await db.get(HealthBudgetMonth, month)).reserved_tokens == 0


async def test_a_spent_budget_stops_the_pairing_and_keeps_the_cursor(sandbox, monkeypatch):
    from app.services.health import budget
    from app.services.health.settings import HealthBudgetCaps

    real_settings = budget.load_concierge_settings

    async def tight(db):
        settings = await real_settings(db)
        return settings.model_copy(update={"budget": HealthBudgetCaps(month_tokens=1_000)})

    monkeypatch.setattr(budget, "load_concierge_settings", tight)
    async with _database() as sessions:
        seeded = await _seed(sessions, sandbox)
        shadow_id = await _shadow(sessions, seeded)
        async with sessions() as db:
            cursor = (await db.get(FlowShadow, shadow_id)).cursor_at
            db.add(_sized_run(seeded, created_at=datetime.now(UTC) - timedelta(hours=1)))
            await db.commit()

        assert await _advance(sessions, shadow_id) == SHADOW_STATUS_RUNNING

        assert sandbox.started == []
        async with sessions() as db:
            assert (await db.scalar(select(func.count()).select_from(FlowShadowPair))) == 0
            assert (await db.get(FlowShadow, shadow_id)).cursor_at == cursor


async def test_a_shadow_pairs_a_bounded_number_of_runs(sandbox):
    async with _database() as sessions:
        seeded = await _seed(sessions, sandbox)
        shadow_id = await _shadow(sessions, seeded)
        start = datetime.now(UTC) - timedelta(days=2)
        async with sessions() as db:
            db.add_all([_sized_run(seeded, created_at=start + timedelta(hours=i)) for i in range(3)])
            await db.commit()
        async with sessions() as db:
            shadow = await db.get(FlowShadow, shadow_id)
            assignment = await db.get(Assignment, seeded["assignment_id"])
            copy = await db.get(Flow, seeded["copy_id"])
            assert (
                await flow_shadow.pair_new_runs(db, shadow, assignment, copy, max_pairs=1, now=datetime.now(UTC)) == 1
            )
            await db.commit()
        assert len(sandbox.started) == 1
        async with sessions() as db:
            statuses = sorted(p.status for p in (await db.execute(select(FlowShadowPair))).scalars())
            assert statuses == ["pending", PAIR_STATUS_SKIPPED, PAIR_STATUS_SKIPPED]


async def test_runs_without_consent_or_not_started_by_the_trigger_are_passed_over(sandbox, monkeypatch):
    from app.services.health import consent

    async def opted_out(_db, *, agent_id=None, flow_id=None, user_id=None, user_ids=()):
        return user_id != opted_out_user

    monkeypatch.setattr(consent, "content_readable", opted_out)
    async with _database() as sessions:
        seeded = await _seed(sessions, sandbox)
        opted_out_user = seeded["owner_id"]
        shadow_id = await _shadow(sessions, seeded)
        start = datetime.now(UTC) - timedelta(days=2)
        async with sessions() as db:
            # An agent-builder ``trigger_assignment`` run is not the schedule firing.
            other = _sized_run(seeded, created_at=start, context_type="agent_tool")
            other.user_id = None
            db.add_all([_sized_run(seeded, created_at=start + timedelta(hours=1)), other])
            await db.commit()

        await _advance(sessions, shadow_id)

        assert sandbox.started == []
        async with sessions() as db:
            assert {p.status for p in (await db.execute(select(FlowShadowPair))).scalars()} == {PAIR_STATUS_SKIPPED}


async def test_with_the_concierge_off_a_held_switch_over_waits_then_is_given_up(sandbox, monkeypatch):
    from app.services.health import availability

    async def off(_db):
        return False

    async with _database() as sessions:
        seeded = await _seed(sessions, sandbox)
        now = datetime.now(UTC)
        shadow_id = await _shadow(sessions, seeded)
        async with sessions() as db:
            db.add_all([_assignment_run(seeded, created_at=now - timedelta(days=2, hours=-i)) for i in range(5)])
            await db.commit()
        # Paired while on; switched off before the take-over.
        await _advance(sessions, shadow_id)
        monkeypatch.setattr(availability, "concierge_active", off)
        async with sessions() as db:
            shadow = await db.get(FlowShadow, shadow_id)
            shadow.ends_at = now - timedelta(minutes=1)
            await db.commit()

        assert await _advance(sessions, shadow_id) == SHADOW_STATUS_RUNNING
        async with sessions() as db:
            assert (await db.scalar(select(func.count()).select_from(Flow))) == 1
            assert (await db.get(Assignment, seeded["assignment_id"])).enabled is True
            shadow = await db.get(FlowShadow, shadow_id)
            shadow.started_at = now - timedelta(days=31)
            await db.commit()

        assert await _advance(sessions, shadow_id) == "cancelled"
        async with sessions() as db:
            assert (await db.get(FlowShadow, shadow_id)).note == "Not switched over: Platform Health is switched off."
            assert (await db.get(Assignment, seeded["assignment_id"])).enabled is True


async def test_with_the_concierge_off_a_regressing_watch_still_rolls_back(sandbox, monkeypatch):
    from app.services.health import availability

    async def off(_db):
        return False

    monkeypatch.setattr(availability, "concierge_active", off)
    monkeypatch.setattr(availability, "concierge_enabled", lambda: False)
    async with _database() as sessions:
        seeded = await _seed(sessions, sandbox)
        shadow_id = await _watching(sessions, seeded, failed=3, completed=7)

        assert await _advance(sessions, shadow_id) == "rolled_back"


async def test_the_tick_decides_on_the_assignment_as_it_is_now_and_holds_it(sandbox):
    async with _database() as sessions:
        seeded = await _seed(sessions, sandbox)
        now = datetime.now(UTC)
        shadow_id = await _shadow(sessions, seeded, ends_at=now - timedelta(minutes=1))
        async with sessions() as db:
            db.add_all([_assignment_run(seeded, created_at=now - timedelta(days=2, hours=-i)) for i in range(5)])
            await db.commit()
        async with sessions() as tick:
            shadow = await tick.get(FlowShadow, shadow_id)
            assert (await tick.get(Assignment, seeded["assignment_id"])).enabled is True
            # The owner pauses it after the tick first read it.
            async with sessions() as owner:
                (await owner.get(Assignment, seeded["assignment_id"])).enabled = False
                await owner.commit()

            assert await flow_shadow.advance_shadow(tick, shadow) == "cancelled"

            async with sessions() as other:
                with pytest.raises(DBAPIError):
                    await other.execute(
                        select(Assignment).where(Assignment.id == seeded["assignment_id"]).with_for_update(nowait=True)
                    )
            await tick.commit()
        async with sessions() as db:
            assert (await db.scalar(select(func.count()).select_from(Flow))) == 1
            assert (
                await db.get(FlowShadow, shadow_id)
            ).note == "Not switched over: the assignment is no longer running."
