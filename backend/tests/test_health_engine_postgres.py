"""Platform Health engine on PostgreSQL (#101 S2, dev-docs/platform-health.md 1.2 and 3):
dedupe, auto-resolve after two passes, dismissal with expiry, daily score history,
nothing enrolled on upgrade (test (a)) and a day of passes metering nothing (test (d))."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models.agent import Agent
from app.models.agentic_execution import AgenticExecutionDaily
from app.models.fleet import FLEET_ROLE_MEMBER, Fleet, FleetMember
from app.models.health_check import STATUS_DISMISSED, STATUS_OPEN, STATUS_RESOLVED, HealthCheckFinding, HealthScoreDay
from app.models.user import User
from app.models.workspace import Workspace
from app.services.health import engine
from app.services.health.engine import CheckResult, dismiss_finding, enrolled_agent_ids, record_results, run_pass
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 19, 3, 0, tzinfo=UTC)


def _fail(failing: bool = True, *, agent: str = "a1", severity: str = "warning") -> CheckResult:
    return CheckResult(
        check_id="agents.tool_errors",
        module="agents",
        category="performance",
        severity=severity,
        failing=failing,
        subject={"agent_id": agent},
        evidence={"values": {"rate": 0.3}, "threshold": 0.1},
    )


async def _owner(db) -> User:
    user = User(id=uuid.uuid4(), email=f"he-{uuid.uuid4().hex[:8]}@example.com", full_name="HE", hashed_password="x")
    db.add(user)
    await db.flush()
    return user


@pytest.mark.asyncio
async def test_a_failure_is_one_row_resolved_after_two_passes_and_reopened_on_recurrence():
    async with _temp_database("he_dedupe") as sessions, sessions() as db:
        assert await record_results(db, [_fail()], now=T0) == {"opened": 1, "updated": 0, "resolved": 0}
        assert await record_results(db, [_fail()], now=T0 + timedelta(minutes=15)) == {
            "opened": 0,
            "updated": 1,
            "resolved": 0,
        }
        assert await db.scalar(select(func.count()).select_from(HealthCheckFinding)) == 1

        await record_results(db, [_fail(False)], now=T0 + timedelta(minutes=30))
        row = (await db.execute(select(HealthCheckFinding))).scalar_one()
        assert (row.status, row.pass_streak) == (STATUS_OPEN, 1)
        await record_results(db, [_fail(False)], now=T0 + timedelta(minutes=45))
        assert (row.status, row.resolved_at) == (STATUS_RESOLVED, T0 + timedelta(minutes=45))

        assert (await record_results(db, [_fail()], now=T0 + timedelta(hours=1)))["opened"] == 1
        assert (row.status, row.resolved_at, row.first_seen) == (STATUS_OPEN, None, T0)


@pytest.mark.asyncio
async def test_a_pending_failure_is_neither_scored_nor_open_and_a_pass_drops_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """#958: one failing read is a pending row the score and the open counts never see."""
    monkeypatch.setattr(engine, "OPEN_AFTER_FAILS", 2)
    async with _temp_database("he_pending") as sessions, sessions() as db:
        assert (await record_results(db, [_fail(severity="critical")], now=T0))["opened"] == 0
        assert (await engine.open_counts(db))["agents"]["critical"] == 0
        assert (await engine.module_scores(db))[0]["agents"] == 100
        await record_results(db, [_fail(False)], now=T0 + timedelta(minutes=15))
        assert await db.scalar(select(func.count()).select_from(HealthCheckFinding)) == 0
        await record_results(db, [_fail(severity="critical")], now=T0 + timedelta(minutes=30))
        assert (await record_results(db, [_fail(severity="critical")], now=T0 + timedelta(minutes=45)))["opened"] == 1
        row = (await db.execute(select(HealthCheckFinding))).scalar_one()
        assert (row.status, row.opened_at, row.alerted_at) == (STATUS_OPEN, T0 + timedelta(minutes=45), None)
        assert (await engine.module_scores(db))[0]["agents"] == 75


@pytest.mark.asyncio
async def test_a_dismissed_finding_stays_silent_until_its_expiry():
    async with _temp_database("he_dismiss") as sessions, sessions() as db:
        await record_results(db, [_fail()], now=T0)
        row = (await db.execute(select(HealthCheckFinding))).scalar_one()
        with pytest.raises(ValueError):
            await dismiss_finding(db, row.id, reason="  ", by="u", by_name="U", now=T0)
        await dismiss_finding(
            db, row.id, reason="Known, accepted", by="u", by_name="U", now=T0, until=T0 + timedelta(days=1)
        )
        await record_results(db, [_fail()], now=T0 + timedelta(hours=2))
        assert row.status == STATUS_DISMISSED
        assert (await record_results(db, [_fail()], now=T0 + timedelta(days=2)))["opened"] == 1
        assert (row.status, row.dismissal) == (STATUS_OPEN, None)


@pytest.mark.asyncio
async def test_the_pass_writes_one_score_row_per_module_and_day():
    async def check(db, now):
        return [_fail(severity="critical"), _fail(agent="a2", severity="warning")]

    probe = engine.Check(check_id="agents.tool_errors", module="agents", interval=timedelta(minutes=15), run=check)
    engine._LAST_RUN.clear()
    async with _temp_database("he_score") as sessions, sessions() as db:
        totals = await run_pass(db, now=T0, checks=[probe])
        assert totals["checks"] == 1 and totals["opened"] == 2
        rows = {r.module: r for r in (await db.execute(select(HealthScoreDay))).scalars()}
        assert set(rows) == set(engine.MODULES)
        assert (rows["agents"].score, rows["agents"].critical, rows["agents"].warning) == (67, 1, 1)
        assert rows["xtm_one"].score == 100
        scores, overall = await engine.module_scores(db)
        assert (scores["agents"], overall) == (67, 67)


@pytest.mark.asyncio
async def test_a_failing_check_keeps_the_results_of_the_checks_before_it():
    async def passing(db, now):
        return [_fail(severity="critical")]

    async def failing(db, now):
        await record_results(db, [_fail(agent="a2")], now=now)
        raise RuntimeError("check broke")

    first = engine.Check(check_id="agents.tool_errors", module="agents", interval=timedelta(minutes=15), run=passing)
    second = engine.Check(check_id="agents.broken", module="agents", interval=timedelta(minutes=15), run=failing)
    engine._LAST_RUN.clear()
    async with _temp_database("he_isolate") as sessions:
        async with sessions() as db:
            totals = await run_pass(db, now=T0, checks=[first, second])
        assert (totals["checks"], totals["opened"]) == (1, 1)
        async with sessions() as db:
            rows = (await db.execute(select(HealthCheckFinding))).scalars().all()
            assert [r.subject for r in rows] == [{"agent_id": "a1"}]
            day = await db.get(HealthScoreDay, (T0.date(), "agents"))
            assert (day.critical, day.score) == (1, 75)


@pytest.mark.asyncio
async def test_a_900_agent_upgrade_enrols_nothing_and_a_fleet_enrols_its_home_members():
    """Test (a): no agent is checked until it, or its fleet, is enrolled."""
    async with _temp_database("he_enrol") as sessions, sessions() as db:
        owner = await _owner(db)
        agents = [Agent(id=uuid.uuid4(), name=f"A{i}", user_id=owner.id, persona="p") for i in range(900)]
        db.add_all(agents)
        await db.flush()
        assert await enrolled_agent_ids(db) == frozenset()

        agents[0].health_enrolled = True
        workspace = Workspace(id=uuid.uuid4(), name="W", slug=f"w-{uuid.uuid4().hex[:8]}", user_id=owner.id)
        db.add(workspace)
        await db.flush()
        fleet = Fleet(
            id=uuid.uuid4(), name="F", slug=f"f-{uuid.uuid4().hex[:8]}", user_id=owner.id, workspace_id=workspace.id
        )
        db.add(fleet)
        await db.flush()
        db.add(FleetMember(fleet_id=fleet.id, agent_id=agents[1].id, role=FLEET_ROLE_MEMBER))
        await db.flush()
        assert await enrolled_agent_ids(db) == frozenset({agents[0].id})

        fleet.health_enrolled = True
        await db.flush()
        assert await enrolled_agent_ids(db) == frozenset({agents[0].id, agents[1].id})


@pytest.mark.asyncio
async def test_a_day_of_passes_meters_nothing(monkeypatch):
    """Test (d): 288 passes (one day at 5 minutes) change no quota counter and no execution count."""
    import app.services.quota as quota

    touched: list[str] = []
    for name in (
        "increment_agentic_usage",
        "check_agentic_quota",
        "increment_llm_proxy_usage",
        "increment_sandbox_usage",
    ):

        async def spy(*args, _name=name, **kwargs):
            touched.append(_name)

        monkeypatch.setattr(quota, name, spy)

    async def check(db, now):
        return [_fail(failing=now.minute % 10 == 0)]

    probe = engine.Check(check_id="agents.tool_errors", module="agents", interval=timedelta(minutes=5), run=check)
    engine._LAST_RUN.clear()
    async with _temp_database("he_metered") as sessions, sessions() as db:
        before = await db.scalar(select(func.count()).select_from(AgenticExecutionDaily))
        for step in range(288):
            await run_pass(db, now=T0 + timedelta(minutes=5 * step), checks=[probe])
        after = await db.scalar(select(func.count()).select_from(AgenticExecutionDaily))
    assert touched == []
    assert before == after == 0
