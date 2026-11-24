"""Platform Health level 8 (#8261) against a real PostgreSQL.

Pinned here: the fleet seed (five company-managed agents with their roles,
manual assignments, a company-managed fleet in a company-managed workspace;
idempotent; nothing while the flag is off), one open cycle per target under a
race (the partial unique index decides), the failure and cost signals firing
once per crossing and re-arming when they clear, the digest never reading a
failed run as completed, and the real routes' access rule - administrator,
owner, ``read_only`` and ``config`` grantees, group grant, revoked share,
company-managed target - on "Improve with Concierge" and the cycle listing.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import platform_health_cycles as routes
from app.core.database import get_db
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.fleet import Fleet, FleetMember
from app.models.group import Group, GroupMember
from app.models.health import HealthCycle
from app.models.health_cycle import HealthCycleRun, HealthSignalState
from app.models.llm_trace import LLMDailyStats, LLMTrace
from app.models.platform_settings import PlatformSettings
from app.models.user import User
from app.models.workspace import Workspace
from app.services import assignment_engine
from app.services.fleets import FLEET_HEALTH_CHECK_NAME
from app.services.health import budget, cycles, fleet_seed, signals
from app.services.health.settings import HealthSettings
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_REAL_TRIGGER = assignment_engine.trigger_assignment


@pytest.fixture
def concierge_on(monkeypatch):
    """The licence gate open, no run really started (the capability is built in)."""

    async def _no_gate(_db):
        return None

    monkeypatch.setattr(budget, "_gate", _no_gate)
    started: list[dict] = []

    async def _trigger(assignment, agent, payload=None, db=None, trigger_context=None, **_kw):
        run = AssignmentRun(
            assignment_id=assignment.id,
            agent_id=agent.id,
            user_id=assignment.user_id,
            status="running",
            trigger_payload=payload,
            trigger_context=trigger_context,
        )
        db.add(run)
        await db.commit()
        started.append({"payload": payload, "context": trigger_context, "run_id": run.id})
        return str(run.id)

    from app.services import assignment_engine

    monkeypatch.setattr(assignment_engine, "trigger_assignment", _trigger)
    return started


def _user(role: str = "user") -> User:
    return User(
        id=uuid.uuid4(),
        email=f"cc-{uuid.uuid4().hex[:8]}@example.com",
        full_name=f"Concierge {role}",
        hashed_password="x",
        role=role,
    )


async def _enable(db, **fields) -> None:
    db.add(PlatformSettings(health_settings=HealthSettings(enabled=True, **fields).model_dump(mode="json")))
    await db.commit()


@asynccontextmanager
async def _scratch(prefix: str):
    async with scratch_database(prefix, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as s:
        yield s.sessions


# ── Fleet seed ──────────────────────────────────────────────────────────────


async def _fleet_counts(db) -> tuple:
    agents = (await db.execute(select(Agent).where(Agent.health_role.is_not(None)))).scalars().all()
    assignments = await db.scalar(
        select(func.count()).select_from(Assignment).where(Assignment.agent_id.in_([a.id for a in agents] or [None]))
    )
    fleets = (await db.execute(select(Fleet))).scalars().all()
    members = await db.scalar(select(func.count()).select_from(FleetMember))
    workspaces = (await db.execute(select(Workspace))).scalars().all()
    return agents, assignments, fleets, members, workspaces


async def test_the_fleet_is_seeded_once_built_in(concierge_on):
    async with _scratch("cc_seed") as sessions, sessions() as db:
        db.add(_user("admin"))
        await db.commit()
        assert await fleet_seed.seed_health_fleet(db) == 8
        assert await fleet_seed.seed_health_fleet(db) == 0
        agents, assignments, fleets, members, workspaces = await _fleet_counts(db)

    assert sorted((a.health_role, a.is_company_managed, a.is_builtin) for a in agents) == [
        ("experimenter", True, True),
        ("flow_lab", True, True),
        ("memory_steward", True, True),
        ("openaev_checker", True, True),
        ("opencrq_checker", True, True),
        ("opencti_checker", True, True),
        ("orchestrator", True, True),
        ("quality_analyst", True, True),
    ]
    # The fleet's own assignments only: the health fleet gets no weekly fleet health check (#974).
    assert assignments == len(fleet_seed.FLEET_ASSIGNMENTS)
    assert [(f.is_company_managed, f.is_builtin, f.slug, f.orchestrator_agent_id) for f in fleets] == [
        (True, True, "platform-health", next(a.id for a in agents if a.health_role == "orchestrator"))
    ]
    assert members == 8
    assert [w.is_company_managed for w in workspaces] == [True]


async def test_an_upgrade_rewrites_a_hand_edit_and_keeps_the_rows(concierge_on):
    """A deployment seeded before #101 S1b: an edited persona, a disabled member, a
    disabled assignment, a non-built-in fleet. The next seed makes them built in, same
    ids, with the seeded definition back."""
    async with _scratch("cc_seed_edit") as sessions, sessions() as db:
        db.add(_user("admin"))
        await db.commit()
        await fleet_seed.seed_health_fleet(db)
        orchestrator = (await db.execute(select(Agent).where(Agent.health_role == "orchestrator"))).scalar_one()
        steward = (await db.execute(select(Agent).where(Agent.health_role == "memory_steward"))).scalar_one()
        seeded_persona = orchestrator.persona
        ids = (orchestrator.id, steward.id)
        orchestrator.persona = "Edited by an administrator."
        orchestrator.is_builtin = steward.is_builtin = False
        steward.enabled = False
        digest, _ = await fleet_seed.fleet_assignment(db, "digest")
        digest.enabled = False
        digest.prompt = "Edited."
        fleet = (await db.execute(select(Fleet))).scalar_one()
        fleet.is_builtin = False
        fleet.slug = "concierge"
        await db.commit()
        await fleet_seed.seed_health_fleet(db)
        for row in (orchestrator, steward, digest, fleet):
            await db.refresh(row)

    assert (orchestrator.id, steward.id) == ids
    assert orchestrator.persona == seeded_persona
    assert (orchestrator.is_builtin, steward.is_builtin, steward.enabled) == (True, True, True)
    assert (digest.enabled, digest.prompt) == (True, fleet_seed.ASSIGNMENT_BY_KEY["digest"].prompt)
    assert (fleet.is_builtin, fleet.slug) == (True, "platform-health")


async def test_a_reseed_keeps_an_emergency_stop_in_force(concierge_on):
    """An emergency stop outlives a restart: the seed rewrites the definition of a
    paused health assignment but never re-enables it, and keeps its stamp so the
    stop's resume still finds it."""
    from app.services.assignment_pause import PANIC_STOP_REASON

    async with _scratch("cc_seed_panic") as sessions, sessions() as db:
        db.add(_user("admin"))
        await db.commit()
        await fleet_seed.seed_health_fleet(db)
        digest, _ = await fleet_seed.fleet_assignment(db, "digest")
        digest.enabled = False
        digest.paused_reason = PANIC_STOP_REASON
        digest.prompt = "Edited."
        await db.commit()
        await fleet_seed.seed_health_fleet(db)
        await db.refresh(digest)

    assert (digest.enabled, digest.paused_reason) == (False, PANIC_STOP_REASON)
    assert digest.prompt == fleet_seed.ASSIGNMENT_BY_KEY["digest"].prompt


async def test_the_built_in_seeds_never_sweep_the_health_rows(concierge_on):
    """The built-in agent and fleet seeds delete a built-in row their lists do not
    name; the health fleet is seeded apart and must survive both."""
    from app.agents import fleet_seed as builtin_fleet_seed
    from app.agents.seed import seed_builtin_agents

    async with _scratch("cc_seed_sweep") as sessions, sessions() as db:
        db.add(_user("admin"))
        await db.commit()
        await fleet_seed.seed_health_fleet(db)
        await seed_builtin_agents(db, platform_mode="xtm_one")
        await builtin_fleet_seed.seed_builtin_fleets(db, platform_mode="xtm_one", fleets=[])
        agents, _, fleets, _, _ = await _fleet_counts(db)

    # The orchestrator and its seven members, the three module checkers (#101 S15) included.
    assert len(agents) == 8
    assert [f.slug for f in fleets if f.is_builtin and f.slug == "platform-health"] == ["platform-health"]


async def test_a_reseed_renames_an_assignment_still_under_its_former_name(concierge_on):
    """An assignment seeded as "Platform Health ..." is renamed in place (#101 S1): same row,
    schedule and enabled state kept, no second assignment, still found by its key."""
    async with _scratch("cc_seed_rename") as sessions, sessions() as db:
        db.add(_user("admin"))
        await db.commit()
        await fleet_seed.seed_health_fleet(db)
        spec = fleet_seed.ASSIGNMENT_BY_KEY["digest"]
        row = (await db.execute(select(Assignment).where(Assignment.name == spec.name))).scalar_one()
        row.name = spec.former_names[0]
        row.enabled = False
        row_id = row.id
        await db.commit()
        before = await db.scalar(select(func.count()).select_from(Assignment))
        found = await fleet_seed.fleet_assignment(db, "digest")
        assert found is not None and found[0].id == row_id
        await fleet_seed.seed_health_fleet(db)
        after = await db.scalar(select(func.count()).select_from(Assignment))
        await db.refresh(row)

    assert after == before
    # Built in: renamed in place and switched back on by the seed.
    assert (row.id, row.name, row.enabled) == (row_id, spec.name, True)
    assert all(not s.name.startswith("Concierge") for s in fleet_seed.FLEET_ASSIGNMENTS)


async def test_a_reseed_takes_the_non_consent_tool_groups_off_the_fleet_and_caps_its_loops(concierge_on):
    async with _scratch("cc_seed_groups") as sessions, sessions() as db:
        db.add(_user("admin"))
        await db.commit()
        await fleet_seed.seed_health_fleet(db)
        # As an earlier seed left them: trace and platform reads, no iteration cap.
        analyst = (await db.execute(select(Agent).where(Agent.health_role == "quality_analyst"))).scalar_one()
        analyst.builtin_tool_ids = ["observability", "platform_insights", *analyst.builtin_tool_ids]
        for assignment in (await db.execute(select(Assignment))).scalars():
            assignment.max_iterations = None
        await db.commit()
        await fleet_seed.seed_health_fleet(db)
        agents = (await db.execute(select(Agent).where(Agent.health_role.is_not(None)))).scalars().all()
        # The fleet's own loops; the weekly fleet health check (#11700) is not a Platform Health assignment.
        caps = {
            a.name: a.max_iterations
            for a in (await db.execute(select(Assignment))).scalars()
            if a.name != FLEET_HEALTH_CHECK_NAME
        }

    assert all(not set(a.builtin_tool_ids) & fleet_seed.NON_CONSENT_TOOL_GROUPS for a in agents)
    assert "health_diagnosis" in next(a for a in agents if a.health_role == "quality_analyst").builtin_tool_ids
    assert caps == {spec.name: spec.max_iterations for spec in fleet_seed.FLEET_ASSIGNMENTS}
    assert all(cap is not None for cap in caps.values())


async def test_a_cycle_run_has_no_user_and_acts_as_the_concierge_alone(concierge_on, monkeypatch):
    """The fleet's assignments keep their admin owner, but a cycle's run takes no
    user: company-managed, the Concierge principal with nobody behind it."""
    from unittest.mock import AsyncMock

    from app.api.v1 import helpers
    from app.services.health.identity import mark_health_run

    monkeypatch.setattr(assignment_engine, "trigger_assignment", _REAL_TRIGGER)
    monkeypatch.setattr(assignment_engine, "_refuse_if_ai_disabled", AsyncMock())
    monkeypatch.setattr(assignment_engine, "_schedule_run_execution", AsyncMock())
    async with _scratch("cc_identity") as sessions:
        admin, target = await _seeded_with_target(sessions)
        async with sessions() as db:
            started = await cycles.start_cycle(
                db,
                key="investigation",
                trigger="on_demand",
                target_type="agent",
                target_id=target.id,
                requested_by_user_id=admin.id,
            )
            run = await db.get(AssignmentRun, started.run_id)
            assignment = await db.get(Assignment, run.assignment_id)
            agent = await db.get(Agent, run.agent_id)

            @helpers.company_managed_run_scoped
            async def _marks():
                company_managed = helpers.mark_company_managed_run(assignment, run)
                principal = await mark_health_run(db, agent, run.user_id)
                return company_managed, principal, helpers.agent_tool_admin_bypass(admin)

            company_managed, principal, bypass = await _marks()
            outside = helpers.agent_tool_admin_bypass(admin)

    assert started.started and assignment.user_id == admin.id
    assert (run.user_id, run.triggered_by_user_id) == (None, None)
    assert company_managed is True
    assert (principal.role, principal.on_behalf_of_admin_id) == ("orchestrator", None)
    # No admin bypass inside the run, whoever owns the fleet; the admin's own tools keep it.
    assert (bypass, outside) == (False, True)
    # And the run acts with no user: never its agent's admin owner (tools, quota, variables).
    assert agent.user_id == admin.id
    assert assignment_engine._acting_identity(run.user_id, agent) is None


async def test_what_a_cycle_delegates_acts_with_no_user_either(concierge_on):
    """A task Platform Health delegates with nobody behind it is recorded under
    the fleet agent's owner (the row needs one) and marked: its run has no
    user, and the worker runs its turn with none. An administrator's own chat
    with the orchestrator delegates as that administrator, as before."""
    from app.models.sub_agent_task import SubAgentTask
    from app.services.health.identity import (
        HEALTH_ALONE_KEY,
        acting_as_health_principal,
        health_delegation_owner_id,
        health_principal_for,
    )
    from app.services.sub_agent import create_sub_agent_task

    async with _scratch("cc_delegate") as sessions:
        admin, target = await _seeded_with_target(sessions)
        async with sessions() as db:
            started = await cycles.start_cycle(
                db, key="investigation", trigger="on_demand", target_type="agent", target_id=target.id
            )
            run = await db.get(AssignmentRun, started.run_id)
            agents = {
                a.health_role: a
                for a in (await db.execute(select(Agent).where(Agent.health_role.is_not(None)))).scalars()
            }
            orchestrator, analyst = agents["orchestrator"], agents["quality_analyst"]
            tasks = []
            for who in (None, admin):
                with acting_as_health_principal(health_principal_for(orchestrator, who)):
                    owner = await health_delegation_owner_id(db, orchestrator) or (who.id if who else None)
                    tasks.append(
                        await create_sub_agent_task(
                            db,
                            user_id=str(owner),
                            parent_agent_id=str(orchestrator.id),
                            target_agent_id=str(analyst.id),
                            task_description="diagnose",
                            parent_run_id=str(run.id),
                        )
                    )
        async with sessions() as db:
            alone, asked = [await db.get(SubAgentTask, t.id) for t in tasks]
            child_users = [
                (await db.get(AssignmentRun, uuid.UUID(t.metadata_json["assignment_run_id"]))).user_id
                for t in (alone, asked)
            ]

    assert alone.user_id == admin.id and alone.metadata_json.get(HEALTH_ALONE_KEY) is True
    assert asked.user_id == admin.id and HEALTH_ALONE_KEY not in asked.metadata_json
    assert child_users == [None, admin.id]


async def test_the_fleet_is_seeded_on_every_deployment_and_once():
    async with _scratch("cc_seed_builtin") as sessions, sessions() as db:
        db.add(_user("admin"))
        await db.commit()
        assert await fleet_seed.seed_health_fleet(db) > 0
        assert await fleet_seed.seed_health_fleet(db) == 0


# ── One open cycle per target ───────────────────────────────────────────────


async def _seeded_with_target(sessions, **settings_fields):
    async with sessions() as db:
        admin = _user("admin")
        db.add(admin)
        await db.commit()
        await _enable(db, **settings_fields)
        await fleet_seed.seed_health_fleet(db)
        # Enrolled: Platform Health supervises opted-in targets only (consent.py).
        target = Agent(
            id=uuid.uuid4(), name="Invoice agent", user_id=admin.id, persona="You answer.", health_enrolled=True
        )
        db.add(target)
        await db.commit()
        return admin, target


async def test_two_racing_requests_open_one_cycle(concierge_on):
    async with _scratch("cc_race") as sessions:
        _admin, target = await _seeded_with_target(sessions)

        async def request():
            async with sessions() as db:
                return await cycles.start_cycle(
                    db, key="investigation", trigger="signal", target_type="agent", target_id=target.id
                )

        first, second = await asyncio.gather(request(), request())
        async with sessions() as db:
            open_cycles = (
                (await db.execute(select(HealthCycle).where(HealthCycle.target_id == target.id))).scalars().all()
            )

    results = sorted([first, second], key=lambda r: r.started, reverse=True)
    assert [r.started for r in results] == [True, False]
    assert results[1].refusal == "cycle_open"
    assert results[1].open_cycle_id == results[0].cycle_id
    assert [(c.id, c.status) for c in open_cycles] == [(results[0].cycle_id, "open")]
    assert len(concierge_on) == 1
    assert concierge_on[0]["context"]["concierge_cycle_id"] == str(results[0].cycle_id)
    assert concierge_on[0]["payload"]["target_id"] == str(target.id)


async def test_a_closed_cycle_frees_its_target_and_gets_a_truthful_digest(concierge_on):
    async with _scratch("cc_settle") as sessions:
        _admin, target = await _seeded_with_target(sessions)
        async with sessions() as db:
            started = await cycles.start_cycle(
                db, key="investigation", trigger="on_demand", target_type="agent", target_id=target.id
            )
            run = await db.get(AssignmentRun, started.run_id)
            run.status = "failed"
            await db.commit()
            assert await cycles.settle_cycles(db) == 1
            cycle = await db.get(HealthCycle, started.cycle_id)
            record = await db.get(HealthCycleRun, started.cycle_id)
            again = await cycles.start_cycle(
                db, key="investigation", trigger="on_demand", target_type="agent", target_id=target.id
            )

    assert cycle.status == "failed"
    assert (record.digest["status"], record.digest["skipped"]) == ("failed", [{"what": "run", "reason": "failed"}])
    assert again.started is True


def _trace(run_id, tokens: int, cost: str) -> LLMTrace:
    from decimal import Decimal

    return LLMTrace(
        assignment_run_id=run_id,
        source="assignment",
        name="run",
        started_at=datetime.now(UTC),
        total_tokens=tokens,
        total_cost=Decimal(cost),
    )


async def _delegated(db, started) -> AssignmentRun:
    child = AssignmentRun(
        agent_id=(await db.get(AssignmentRun, started.run_id)).agent_id,
        status="running",
        parent_run_id=started.run_id,
        trigger_context=await cycles.inherited_cycle_context(db, started.run_id),
    )
    db.add(child)
    await db.commit()
    return child


async def test_a_cycle_books_its_whole_run_tree_and_closes_once_every_run_ended(concierge_on):
    from decimal import Decimal

    from app.models.health import HealthBudgetMonth

    async with _scratch("cc_book") as sessions:
        _admin, target = await _seeded_with_target(sessions)
        async with sessions() as db:
            started = await cycles.start_cycle(
                db, key="investigation", trigger="on_demand", target_type="agent", target_id=target.id
            )
            child = await _delegated(db, started)
            (await db.get(AssignmentRun, started.run_id)).status = "completed"
            db.add(_trace(started.run_id, 1_000, "0.5"))
            await db.commit()
            # The orchestrator ended, its delegated specialist still works: booked, not closed.
            assert await cycles.settle_cycles(db) == 0
            cycle = await db.get(HealthCycle, started.cycle_id)
            first = (cycle.status, cycle.spent_tokens)
            child.status = "completed"
            db.add(_trace(child.id, 400, "0.1"))
            await db.commit()
            assert await cycles.settle_cycles(db) == 1
            await cycles.settle_cycles(db)
            await db.refresh(cycle)
            month = await db.get(HealthBudgetMonth, cycle.month)
            await db.refresh(month)

    assert first == ("open", 1_000)
    assert (cycle.status, cycle.spent_tokens, cycle.spent_cost_usd) == ("completed", 1_400, Decimal("0.6"))
    assert month.spent_tokens == 1_400


async def test_a_cycle_past_its_cap_stops_its_runs_and_closes_budget_reached(concierge_on, monkeypatch):
    from app.a2a.client import delegation

    monkeypatch.setattr(delegation, "spawn_run_cancel", lambda _ids: None)
    # The stopped run wrote no trace: booked at once rather than after the traces' grace.
    monkeypatch.setattr(cycles, "TRACE_SETTLE_DELAY", timedelta(0))
    async with _scratch("cc_cap") as sessions:
        _admin, target = await _seeded_with_target(sessions, budget={"cycle_tokens": 500})
        async with sessions() as db:
            started = await cycles.start_cycle(
                db, key="investigation", trigger="on_demand", target_type="agent", target_id=target.id
            )
            child = await _delegated(db, started)
            (await db.get(AssignmentRun, started.run_id)).status = "completed"
            db.add(_trace(started.run_id, 600, "0"))
            await db.commit()
            assert await cycles.settle_cycles(db) == 0
            await db.refresh(child)
            stopped = (child.status, child.error)
            assert await cycles.settle_cycles(db) == 1
            cycle = await db.get(HealthCycle, started.cycle_id)
            await db.refresh(cycle)

    assert stopped == ("cancelled", budget.REFUSAL_MESSAGES["cycle_budget_reached"])
    assert (cycle.status, cycle.spent_tokens) == ("budget_reached", 600)


async def test_a_target_outside_the_scope_starts_nothing(concierge_on):
    async with _scratch("cc_scope") as sessions:
        _admin, target = await _seeded_with_target(sessions, scope_mode="selected", scope_agent_ids=[])
        async with sessions() as db:
            result = await cycles.start_cycle(
                db, key="investigation", trigger="on_demand", target_type="agent", target_id=target.id
            )
            count = await db.scalar(select(func.count()).select_from(HealthCycle))
    assert (result.refusal, count, concierge_on) == ("not_in_scope", 0, [])


async def test_a_schedule_does_not_start_while_its_previous_cycle_is_open(concierge_on):
    from app.models.health_cycle import HealthScheduleMark

    now = datetime.now(UTC)
    async with _scratch("cc_schedule") as sessions:
        await _seeded_with_target(sessions)
        async with sessions() as db:
            for key in cycles.SCHEDULE_KEYS:
                db.add(HealthScheduleMark(schedule=key, fired_at=now - timedelta(days=8)))
            await db.commit()
            first = await cycles.run_due_schedules(db, now=now)
            second = await cycles.run_due_schedules(db, now=now + timedelta(days=8))
            refusals = {m.schedule: m.refusal for m in (await db.execute(select(HealthScheduleMark))).scalars()}
            for run in (await db.execute(select(AssignmentRun))).scalars():
                run.status = "completed"
            await db.commit()
            await cycles.settle_cycles(db)
            third = await cycles.run_due_schedules(db, now=now + timedelta(days=16))

    assert (first, second, third) == (4, 0, 4)
    assert refusals == {key: "cycle_open" for key in cycles.SCHEDULE_KEYS}
    assert len(concierge_on) == 8


async def test_a_delegated_run_inherits_its_parent_s_cycle(concierge_on):
    async with _scratch("cc_inherit") as sessions:
        _admin, target = await _seeded_with_target(sessions)
        async with sessions() as db:
            started = await cycles.start_cycle(
                db, key="investigation", trigger="on_demand", target_type="agent", target_id=target.id
            )
            plain = AssignmentRun(agent_id=target.id, status="completed", trigger_context={"type": "manual"})
            db.add(plain)
            await db.commit()
            inherited = await cycles.inherited_cycle_context(db, started.run_id)
            child = AssignmentRun(
                agent_id=target.id, status="pending", parent_run_id=started.run_id, trigger_context=inherited
            )
            db.add(child)
            await db.commit()
            grandchild = await cycles.inherited_cycle_context(db, child.id)
            none_for_plain = await cycles.inherited_cycle_context(db, plain.id)
            none_for_bad = await cycles.inherited_cycle_context(db, "not-a-uuid")

    expected = {"type": "delegation", "concierge_cycle_id": str(started.cycle_id)}
    assert (inherited, grandchild, none_for_plain, none_for_bad) == (expected, expected, None, None)


# ── Signals ─────────────────────────────────────────────────────────────────


async def _runs(db, assignment: Assignment, statuses: list[str], *, now: datetime) -> None:
    for index, status in enumerate(statuses):
        at = now - timedelta(minutes=index + 1)
        db.add(
            AssignmentRun(
                assignment_id=assignment.id,
                agent_id=assignment.agent_id,
                status=status,
                created_at=at,
                completed_at=at,
            )
        )
    await db.commit()


async def test_a_failure_spike_fires_once_and_rearms_when_it_clears(concierge_on):
    now = datetime.now(UTC)
    async with _scratch("cc_failure") as sessions:
        admin, target = await _seeded_with_target(sessions)
        async with sessions() as db:
            assignment = Assignment(
                agent_id=target.id, user_id=admin.id, name="Nightly", prompt="Go.", trigger_type="manual"
            )
            db.add(assignment)
            await db.commit()
            await _runs(db, assignment, ["failed", "failed"] + ["completed"] * 18, now=now)

            first = await signals.evaluate_signal(db, "failure_spike", now=now, since=now - timedelta(hours=1))
            crossings = await signals.record_readings(db, first, now=now)
            await db.commit()
            fired = await signals.fire_signals(db, crossings, now=now)
            second = await signals.evaluate_signal(db, "failure_spike", now=now, since=now - timedelta(hours=1))
            again = await signals.record_readings(db, second, now=now)
            await db.commit()

            await _runs(db, assignment, ["completed"] * 20, now=now + timedelta(minutes=30))
            later = now + timedelta(hours=1)
            cleared = await signals.record_readings(
                db, await signals.evaluate_signal(db, "failure_spike", now=later, since=now), now=later
            )
            await db.commit()
            state = (await db.execute(select(HealthSignalState))).scalar_one()

    assert [(r.target_type, r.target_id, r.crossed, r.value) for r in first] == [
        ("assignment", assignment.id, True, 10.0)
    ]
    assert (len(crossings), fired, again, cleared) == (1, 1, [], [])
    assert (state.active, state.kind, state.target_id) == (False, "failure_spike", assignment.id)
    assert len(concierge_on) == 1
    assert concierge_on[0]["payload"]["signal"]["kind"] == "failure_spike"


async def test_cost_drift_reads_the_daily_rollup(concierge_on):
    now = datetime.now(UTC)
    today = now.date()
    async with _scratch("cc_cost") as sessions:
        _admin, target = await _seeded_with_target(sessions)
        async with sessions() as db:
            for offset in range(1, 15):
                day: date = today - timedelta(days=offset)
                cost = 1.3 if offset <= 7 else 1.0
                db.add(
                    LLMDailyStats(
                        date=day, agent_id=target.id, model="m", source="assignment", trace_count=2, total_cost=2 * cost
                    )
                )
            await db.commit()
            readings = await signals.evaluate_signal(db, "cost_drift", now=now, since=now)

    assert [(r.target_id, r.crossed, r.value, r.baseline, r.detail["runs"]) for r in readings] == [
        (target.id, True, 1.3, 1.0, 14)
    ]


async def test_a_model_change_refused_for_a_passing_reason_fires_on_the_next_pass(concierge_on, monkeypatch):
    now = datetime.now(UTC)
    async with _scratch("cc_model") as sessions:
        _admin, target = await _seeded_with_target(sessions)
        async with sessions() as db:
            agent = await db.get(Agent, target.id)
            agent.llm_model = "model-a"
            await db.commit()
            sighting = await signals.record_readings(
                db, await signals.evaluate_signal(db, "model_change", now=now, since=now), now=now
            )
            agent.llm_model = "model-b"
            await db.commit()

            real_start = cycles.start_cycle

            async def refused(_db, **_kw):
                return cycles.CycleStart(refusal="month_budget_reached")

            monkeypatch.setattr(cycles, "start_cycle", refused)
            crossings = await signals.record_readings(
                db, await signals.evaluate_signal(db, "model_change", now=now, since=now), now=now
            )
            await db.commit()
            not_fired = await signals.fire_signals(db, crossings, now=now)
            monkeypatch.setattr(cycles, "start_cycle", real_start)
            again = await signals.record_readings(
                db, await signals.evaluate_signal(db, "model_change", now=now, since=now), now=now
            )
            await db.commit()
            fired = await signals.fire_signals(db, again, now=now)
            after = await signals.record_readings(
                db, await signals.evaluate_signal(db, "model_change", now=now, since=now), now=now
            )
            state = (
                await db.execute(select(HealthSignalState).where(HealthSignalState.target_id == target.id))
            ).scalar_one()

    assert sighting == []
    assert [(r.value, r.baseline) for r in crossings] == [("model-b", "model-a")]
    assert [(r.value, r.baseline) for r in again] == [("model-b", "model-a")]
    assert (not_fired, fired, after) == (0, 1, [])
    assert (state.last_value["value"], state.active, state.refusal) == ("model-b", False, None)
    assert len(concierge_on) == 1


async def test_the_concierge_s_sandbox_copies_raise_no_signal(concierge_on):
    now = datetime.now(UTC)
    async with _scratch("cc_sandbox") as sessions:
        admin, _target = await _seeded_with_target(sessions)
        async with sessions() as db:
            copy = Agent(
                id=uuid.uuid4(), name="Sandbox of Invoice agent", user_id=admin.id, persona="p", is_sandbox=True
            )
            db.add(copy)
            await db.commit()
            assignment = Assignment(
                agent_id=copy.id, user_id=admin.id, name="Copy", prompt="Go.", trigger_type="manual"
            )
            db.add(assignment)
            for offset in range(1, 15):
                db.add(
                    LLMDailyStats(
                        date=now.date() - timedelta(days=offset),
                        agent_id=copy.id,
                        model="m",
                        source="assignment",
                        trace_count=2,
                        total_cost=4.0 if offset <= 7 else 1.0,
                    )
                )
            await db.commit()
            await _runs(db, assignment, ["failed"] * 20, now=now)
            readings = {
                kind: await signals.evaluate_signal(db, kind, now=now, since=now - timedelta(hours=1))
                for kind in ("failure_spike", "cost_drift", "model_change")
            }
            on_demand = await cycles.start_cycle(
                db, key="investigation", trigger="on_demand", target_type="agent", target_id=copy.id
            )

    assert [r.target_id for rs in readings.values() for r in rs if r.target_id in (copy.id, assignment.id)] == []
    assert on_demand.refusal == "not_found"


async def test_a_failed_signal_evaluation_waits_for_its_cadence(concierge_on, monkeypatch):
    from app.core import database
    from app.models.health_cycle import HealthScheduleMark

    now = datetime.now(UTC)
    calls: list[str] = []

    async def broken(_db, kind, **_kw):
        calls.append(kind)
        raise RuntimeError("boom")

    async with _scratch("cc_signal_fail") as sessions:
        async with sessions() as db:
            await _enable(db)
        monkeypatch.setattr(database, "async_session", sessions)
        monkeypatch.setattr(signals, "evaluate_signal", broken)
        await signals.signals_tick(now=now)
        await signals.signals_tick(now=now + timedelta(minutes=1))
        async with sessions() as db:
            marks = {m.schedule for m in (await db.execute(select(HealthScheduleMark))).scalars()}

    assert calls == list(signals.SIGNAL_KINDS)
    assert marks == {"signal:feedback", "signal:failure", "signal:cost", "signal:model"}


# ── Routes: one access rule ─────────────────────────────────────────────────


@asynccontextmanager
async def _client(sessions, user: User) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1/health")

    async def _get_db():
        async with sessions() as db:
            yield db
            await db.commit()

    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _get_db
    # The AI gate has its own tests; here the budget's gate stands in for it (concierge_on).
    app.dependency_overrides[deps.require_agentic_ai] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def _access_matrix(sessions):
    async with sessions() as db:
        admin, owner, reader, configurer, grouped, revoked, stranger = (
            _user("admin"),
            _user(),
            _user(),
            _user(),
            _user(),
            _user(),
            _user(),
        )
        db.add_all([admin, owner, reader, configurer, grouped, revoked, stranger])
        await db.commit()
        await _enable(db)
        await fleet_seed.seed_health_fleet(db)
        group = Group(id=uuid.uuid4(), name=f"g-{uuid.uuid4().hex[:6]}")
        db.add(group)
        await db.flush()
        db.add(GroupMember(group_id=group.id, user_id=grouped.id))
        private = Agent(
            id=uuid.uuid4(),
            name="Private agent",
            user_id=owner.id,
            persona="p",
            shared_user_ids=[str(reader.id), str(configurer.id), str(revoked.id)],
            shared_user_permissions={
                str(reader.id): "read_only",
                str(configurer.id): "config",
                str(revoked.id): "config",
            },
            shared_group_ids=[str(group.id)],
            shared_group_permissions={str(group.id): "config"},
            health_enrolled=True,
        )
        company = Agent(
            id=uuid.uuid4(),
            name="Company agent",
            user_id=admin.id,
            persona="p",
            is_company_managed=True,
            health_enrolled=True,
        )
        db.add_all([private, company])
        await db.commit()
        # The share is revoked: the next request reads the row as it is now.
        private.shared_user_ids = [str(reader.id), str(configurer.id)]
        private.shared_user_permissions = {str(reader.id): "read_only", str(configurer.id): "config"}
        await db.commit()
        return (
            {
                "admin": admin,
                "owner": owner,
                "read_only": reader,
                "config": configurer,
                "group": grouped,
                "revoked": revoked,
                "stranger": stranger,
            },
            private,
            company,
        )


@pytest.mark.parametrize(
    ("who", "target", "expected"),
    [
        ("admin", "private", 202),
        ("owner", "private", 202),
        ("config", "private", 202),
        ("group", "private", 202),
        ("read_only", "private", 403),
        ("revoked", "private", 404),
        ("stranger", "private", 404),
        ("owner", "company", 403),
        ("admin", "company", 202),
    ],
)
async def test_improve_with_concierge_follows_the_manage_rule(concierge_on, who, target, expected):
    async with _scratch("cc_routes") as sessions:
        users, private, company = await _access_matrix(sessions)
        agent = private if target == "private" else company
        async with _client(sessions, users[who]) as client:
            response = await client.post(
                "/api/v1/health/cycles", json={"target_type": "agent", "target_id": str(agent.id)}
            )
            listing = await client.get(
                "/api/v1/health/cycles", params={"target_type": "agent", "target_id": str(agent.id)}
            )
    assert response.status_code == expected, response.text
    if expected == 202:
        assert [c["id"] for c in listing.json()] == [response.json()["cycle_id"]]
        assert listing.json()[0]["requested_by_me"] is True
    else:
        assert listing.status_code == 404
        assert len(concierge_on) == 0


@pytest.mark.parametrize(("who", "narrative"), [("owner", "quoted"), ("admin", "quoted"), ("config", None)])
async def test_the_digest_narrative_follows_the_chat_read_rule(concierge_on, who, narrative):
    """The narrative may quote conversations: a ``config`` grantee manages the agent, reads no chat."""
    async with _scratch("cc_routes_narrative") as sessions:
        users, private, _company = await _access_matrix(sessions)
        async with sessions() as db:
            cycle = HealthCycle(
                specialist="investigation",
                trigger="on_demand",
                target_type="agent",
                target_id=private.id,
                month=datetime.now(UTC).strftime("%Y-%m"),
                status="completed",
            )
            db.add(cycle)
            await db.flush()
            db.add(HealthCycleRun(cycle_id=cycle.id, digest={"status": "completed", "narrative": "quoted"}))
            await db.commit()
        async with _client(sessions, users[who]) as client:
            listing = await client.get(
                "/api/v1/health/cycles", params={"target_type": "agent", "target_id": str(private.id)}
            )
            one = await client.get(f"/api/v1/health/cycles/{cycle.id}")
    assert listing.json()[0]["digest"]["narrative"] == narrative
    assert one.json()["digest"]["narrative"] == narrative


async def test_a_non_admin_lists_nothing_without_a_target(concierge_on):
    async with _scratch("cc_routes_list") as sessions:
        users, _private, _company = await _access_matrix(sessions)
        async with _client(sessions, users["owner"]) as client:
            cycles_list = await client.get("/api/v1/health/cycles")
            signal_list = await client.get("/api/v1/health/signals")
        async with _client(sessions, users["admin"]) as client:
            admin_list = await client.get("/api/v1/health/cycles")
    assert (cycles_list.status_code, signal_list.status_code, admin_list.status_code) == (404, 404, 200)


async def test_a_signal_names_its_target_and_a_deleted_target_has_no_name(concierge_on):
    """The Signals tab shows the target's name (#9378 slice 6a, UX round 1 B1), never a raw id."""
    async with _scratch("cc_routes_signal_names") as sessions:
        users, private, _company = await _access_matrix(sessions)
        gone = uuid.uuid4()
        async with sessions() as db:
            for target_id in (private.id, gone):
                db.add(
                    HealthSignalState(
                        target_type="agent", target_id=target_id, kind="feedback_drop", active=True, last_value={}
                    )
                )
            await db.commit()
        async with _client(sessions, users["admin"]) as client:
            listing = await client.get("/api/v1/health/signals")
    assert listing.status_code == 200
    names = {row["target_id"]: row["target_name"] for row in listing.json()}
    assert names == {str(private.id): "Private agent", str(gone): None}


async def test_a_second_request_answers_409_with_the_open_cycle(concierge_on):
    async with _scratch("cc_routes_409") as sessions:
        users, private, _company = await _access_matrix(sessions)
        async with _client(sessions, users["owner"]) as client:
            body = {"target_type": "agent", "target_id": str(private.id)}
            first = await client.post("/api/v1/health/cycles", json=body)
            second = await client.post("/api/v1/health/cycles", json=body)
    assert (first.status_code, second.status_code) == (202, 409)
    assert second.json()["detail"] == {
        "code": "cycle_open",
        "message": "Platform Health is already working on this target.",
        "open_cycle_id": first.json()["cycle_id"],
    }


async def test_a_refusal_names_the_concierge_as_its_administrator_named_it(concierge_on):
    async with _scratch("cc_routes_409_named") as sessions:
        users, private, _company = await _access_matrix(sessions)
        async with sessions() as db:
            row = (await db.execute(select(PlatformSettings))).scalar_one()
            row.health_settings = {**row.health_settings, "name": "{Jeeves}"}
            await db.commit()
        async with _client(sessions, users["owner"]) as client:
            body = {"target_type": "agent", "target_id": str(private.id)}
            await client.post("/api/v1/health/cycles", json=body)
            second = await client.post("/api/v1/health/cycles", json=body)
    # A brace in the name is a brace: the message is never formatted with it.
    assert second.json()["detail"]["message"] == "{Jeeves} is already working on this target."


async def test_an_invalid_body_is_a_422(concierge_on):
    async with _scratch("cc_routes_422") as sessions:
        users, _private, _company = await _access_matrix(sessions)
        async with _client(sessions, users["owner"]) as client:
            bad_type = await client.post(
                "/api/v1/health/cycles", json={"target_type": "user", "target_id": str(uuid.uuid4())}
            )
            bad_id = await client.post("/api/v1/health/cycles", json={"target_type": "agent", "target_id": "x"})
            extra = await client.post(
                "/api/v1/health/cycles", json={"target_type": "agent", "target_id": str(uuid.uuid4()), "user_id": "x"}
            )
    assert (bad_type.status_code, bad_id.status_code, extra.status_code) == (422, 422, 422)


# ── Improvements inbox: level 3's read rule, row by row ─────────────────────


async def _with_proposal(sessions):
    from app.models.improvement_proposal import ImprovementProposal

    users, private, _company = await _access_matrix(sessions)
    async with sessions() as db:
        orchestrator = (await db.execute(select(Agent).where(Agent.slug == fleet_seed.ORCHESTRATOR_SLUG))).scalar_one()
        trace = LLMTrace(
            id=uuid.uuid4(), source="assignment", name="run", agent_id=private.id, started_at=datetime.now(UTC)
        )
        db.add(trace)
        proposal = ImprovementProposal(
            id=uuid.uuid4(),
            proposal_type="persona",
            target_type="agent",
            target_id=private.id,
            agent_id=private.id,
            status="pending_approval",
            dedup_key=uuid.uuid4().hex,
            base_content="p",
            proposed_content="p, shorter",
            summary="Shorter answers.",
            evidence_refs=[{"kind": "trace", "id": str(trace.id)}, {"kind": "trace", "id": str(uuid.uuid4())}],
            experiment_verdict="unmeasured",
            author_agent_id=orchestrator.id,
        )
        db.add(proposal)
        await db.commit()
    return users, private, proposal, trace


@pytest.mark.parametrize(
    ("who", "listed", "can_decide"),
    [
        ("owner", True, True),
        ("config", True, True),
        ("group", True, True),
        ("admin", True, False),  # no admin bypass on a private target (B11)
        ("read_only", False, None),
        ("revoked", False, None),
        ("stranger", False, None),
    ],
)
async def test_the_inbox_lists_a_proposal_to_who_may_read_it(concierge_on, who, listed, can_decide):
    async with _scratch("cc_inbox") as sessions:
        users, private, proposal, _trace = await _with_proposal(sessions)
        async with _client(sessions, users[who]) as client:
            by_agent = await client.get("/api/v1/health/improvements", params={"agent_id": str(private.id)})
            evidence = await client.get(f"/api/v1/health/improvements/{proposal.id}/evidence")
    items = by_agent.json()["items"]
    if listed:
        assert [(i["id"], i["can_decide"]) for i in items] == [(str(proposal.id), can_decide)]
        assert evidence.status_code == 200
    else:
        assert (items, by_agent.json()["total"], evidence.status_code) == ([], 0, 404)


async def test_evidence_links_only_what_the_viewer_may_open(concierge_on):
    async with _scratch("cc_evidence") as sessions:
        users, _private, proposal, trace = await _with_proposal(sessions)
        async with _client(sessions, users["owner"]) as client:
            owner = (await client.get(f"/api/v1/health/improvements/{proposal.id}/evidence")).json()
        async with _client(sessions, users["config"]) as client:
            configurer = (await client.get(f"/api/v1/health/improvements/{proposal.id}/evidence")).json()
    assert owner == [
        {"kind": "trace", "id": str(trace.id), "href": f"/observability/traces/{trace.id}", "hidden": False},
        {"kind": "trace", "id": None, "href": None, "hidden": True},
    ]
    # A config grantee manages the agent but does not read its chats (config_with_chats does).
    assert configurer == [
        {"kind": "trace", "id": None, "href": None, "hidden": True},
        {"kind": "trace", "id": None, "href": None, "hidden": True},
    ]


async def test_a_non_admin_names_a_target_to_list_proposals(concierge_on):
    async with _scratch("cc_inbox_scope") as sessions:
        users, _private, _proposal, _trace = await _with_proposal(sessions)
        async with _client(sessions, users["owner"]) as client:
            unscoped = await client.get("/api/v1/health/improvements")
        async with _client(sessions, users["admin"]) as client:
            everything = await client.get("/api/v1/health/improvements", params={"status": "pending_approval"})
    assert unscoped.status_code == 404
    assert everything.json()["total"] == 1


async def test_the_inbox_counts_and_pages_past_one_scan_batch(concierge_on, monkeypatch):
    from app.models.improvement_proposal import ImprovementProposal

    monkeypatch.setattr(routes, "_IMPROVEMENTS_SCAN", 2)
    async with _scratch("cc_inbox_pages") as sessions:
        users, private, proposal, _trace = await _with_proposal(sessions)
        async with sessions() as db:
            for index in range(4):
                db.add(
                    ImprovementProposal(
                        id=uuid.uuid4(),
                        proposal_type="persona",
                        target_type="agent",
                        target_id=private.id,
                        agent_id=private.id,
                        status="rejected",
                        dedup_key=uuid.uuid4().hex,
                        base_content="p",
                        proposed_content=f"p {index}",
                        author_agent_id=proposal.author_agent_id,
                        created_at=datetime.now(UTC) - timedelta(days=index + 1),
                    )
                )
            await db.commit()
        pages = {}
        for who in ("owner", "admin"):
            async with _client(sessions, users[who]) as client:
                pages[who] = (
                    await client.get(
                        "/api/v1/health/improvements",
                        params={"agent_id": str(private.id), "limit": 2, "offset": 4},
                    )
                ).json()

    assert [(p["total"], len(p["items"])) for p in pages.values()] == [(5, 1), (5, 1)]


async def test_the_inbox_searches_filters_and_sorts_for_the_data_table(concierge_on):
    """#9378 slice 6: repeated type and status, a summary search and a sort reach the query."""
    from app.models.improvement_proposal import ImprovementProposal

    async with _scratch("cc_inbox_table") as sessions:
        users, private, proposal, _trace = await _with_proposal(sessions)
        async with sessions() as db:
            for index, (kind, state, summary) in enumerate(
                [("persona", "kept", "Sharper tone"), ("persona", "rejected", "Shorter answers 100%")]
            ):
                db.add(
                    ImprovementProposal(
                        id=uuid.uuid4(),
                        proposal_type=kind,
                        target_type="agent",
                        target_id=private.id,
                        agent_id=private.id,
                        status=state,
                        summary=summary,
                        dedup_key=uuid.uuid4().hex,
                        base_content="p",
                        proposed_content=f"p {index}",
                        author_agent_id=proposal.author_agent_id,
                        created_at=datetime.now(UTC) - timedelta(days=index + 1),
                    )
                )
            await db.commit()
        scope = {"agent_id": str(private.id)}
        async with _client(sessions, users["admin"]) as client:
            both = (
                await client.get("/api/v1/health/improvements", params={**scope, "status": ["kept", "rejected"]})
            ).json()
            searched = (await client.get("/api/v1/health/improvements", params={**scope, "q": "tone"})).json()
            literal = (await client.get("/api/v1/health/improvements", params={**scope, "q": "100%"})).json()
            by_status = (
                await client.get("/api/v1/health/improvements", params={**scope, "sort": "status", "direction": "asc"})
            ).json()
            by_type = (
                await client.get("/api/v1/health/improvements", params={**scope, "proposal_type": ["persona", "skill"]})
            ).json()
            oldest = (
                await client.get("/api/v1/health/improvements", params={**scope, "sort": "date", "direction": "asc"})
            ).json()
            bad = await client.get("/api/v1/health/improvements", params={**scope, "sort": "summary"})

    assert both["total"] == 2 and {p["status"] for p in both["items"]} == {"kept", "rejected"}
    assert [p["summary"] for p in searched["items"]] == ["Sharper tone"]
    assert [p["summary"] for p in literal["items"]] == ["Shorter answers 100%"]
    assert [p["status"] for p in by_status["items"]] == ["kept", "pending_approval", "rejected"]
    assert by_type["total"] == 3
    assert oldest["items"][0]["summary"] == "Shorter answers 100%"
    assert bad.status_code == 422
