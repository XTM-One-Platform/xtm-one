"""Platform Health agent checks on PostgreSQL (#101 S3, dev-docs/platform-health.md 4.1):
enrolled agents only, one result per subject, findings opened by the engine and
resolved once the cause is gone, nothing read with AI switched off, nothing metered."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.health_check import STATUS_OPEN, STATUS_RESOLVED, HealthCheckFinding
from app.models.user import User
from app.services.health import agent_checks, engine
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 19, 3, 0, tzinfo=UTC)
CHECKS = [c for c in engine.registered_checks() if c.module == "agents"]


@pytest.fixture(autouse=True)
def _ai_on(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.ai_state as ai_state

    async def enabled(db: AsyncSession) -> bool:
        return True

    monkeypatch.setattr(ai_state, "is_ai_enabled", enabled)
    engine._LAST_RUN.clear()


async def _world(
    db: AsyncSession, *, enrolled: bool = True, owner_active: bool = True
) -> tuple[User, Agent, Assignment]:
    owner = User(
        id=uuid.uuid4(),
        email=f"ac-{uuid.uuid4().hex[:8]}@example.com",
        full_name="AC",
        hashed_password="x",
        is_active=owner_active,
    )
    db.add(owner)
    await db.flush()
    agent = Agent(id=uuid.uuid4(), name="Target", user_id=owner.id, persona="p", health_enrolled=enrolled)
    db.add(agent)
    await db.flush()
    assignment = Assignment(
        id=uuid.uuid4(),
        agent_id=agent.id,
        user_id=owner.id,
        name="nightly",
        prompt="p",
        trigger_type="schedule",
        created_at=T0 - timedelta(days=30),
        last_triggered_at=T0 - timedelta(days=20),
    )
    db.add(assignment)
    await db.flush()
    for minutes in (30, 20, 10):
        db.add(
            AssignmentRun(
                assignment_id=assignment.id,
                agent_id=agent.id,
                user_id=owner.id,
                status="failed",
                error="tool refused",
                created_at=T0 - timedelta(minutes=minutes),
            )
        )
    await db.flush()
    return owner, agent, assignment


async def _open(db: AsyncSession) -> dict[str, HealthCheckFinding]:
    rows = (await db.execute(select(HealthCheckFinding))).scalars().all()
    return {r.check_id: r for r in rows if r.status == STATUS_OPEN}


def test_the_seven_agent_checks_are_registered_on_the_engine() -> None:
    assert {c.check_id for c in CHECKS} == {
        "agents.assignment_failing",
        "agents.schedule_silent",
        "agents.cost_spike",
        "agents.owner_disabled",
        "agents.failure_spike",
        "agents.feedback_drop",
        "agents.cost_drift",
    }
    assert {c.interval for c in CHECKS} == {agent_checks.AGENT_CHECK_INTERVAL}


@pytest.mark.asyncio
async def test_an_enrolled_agent_gets_one_finding_per_failing_check() -> None:
    async with _temp_database("hac_findings") as sessions, sessions() as db:
        _owner, agent, assignment = await _world(db, owner_active=False)
        totals = await engine.run_pass(db, now=T0, checks=CHECKS)
        assert totals["checks"] == 7
        found = await _open(db)
        assert set(found) == {"agents.assignment_failing", "agents.schedule_silent", "agents.owner_disabled"}
        assert found["agents.assignment_failing"].subject == {
            "agent_id": str(agent.id),
            "assignment_id": str(assignment.id),
        }
        assert found["agents.owner_disabled"].severity == "warning"
        assert found["agents.schedule_silent"].severity == "info"
        assert found["agents.assignment_failing"].doc_link == agent_checks.DOC_LINK


@pytest.mark.asyncio
async def test_a_not_enrolled_agent_is_never_read() -> None:
    async with _temp_database("hac_enrol") as sessions, sessions() as db:
        await _world(db, enrolled=False, owner_active=False)
        await engine.run_pass(db, now=T0, checks=CHECKS)
        assert await _open(db) == {}


@pytest.mark.asyncio
async def test_a_failing_assignment_resolves_after_two_passing_passes() -> None:
    async with _temp_database("hac_resolve") as sessions, sessions() as db:
        owner, agent, assignment = await _world(db)
        await engine.run_pass(db, now=T0, checks=CHECKS)
        row = (await _open(db))["agents.assignment_failing"]
        db.add(
            AssignmentRun(
                assignment_id=assignment.id, agent_id=agent.id, user_id=owner.id, status="completed", created_at=T0
            )
        )
        await db.flush()
        for step in (1, 2):
            engine._LAST_RUN.clear()
            await engine.run_pass(db, now=T0 + timedelta(minutes=15 * step), checks=CHECKS)
        await db.refresh(row)
        assert row.status == STATUS_RESOLVED


@pytest.mark.asyncio
async def test_pausing_the_assignment_resolves_its_findings() -> None:
    """The remedy the checks recommend (pause the assignment) takes it out of the read; its findings resolve."""
    async with _temp_database("hac_paused") as sessions, sessions() as db:
        _owner, _agent, assignment = await _world(db)
        await engine.run_pass(db, now=T0, checks=CHECKS)
        found = await _open(db)
        rows = [found["agents.assignment_failing"], found["agents.schedule_silent"]]
        assignment.enabled = False
        await db.flush()
        for step in (1, 2):
            engine._LAST_RUN.clear()
            await engine.run_pass(db, now=T0 + timedelta(minutes=15 * step), checks=CHECKS)
        for row in rows:
            await db.refresh(row)
            assert row.status == STATUS_RESOLVED


@pytest.mark.asyncio
async def test_unenrolling_the_agent_resolves_its_findings() -> None:
    async with _temp_database("hac_unenrol") as sessions, sessions() as db:
        _owner, agent, _assignment = await _world(db, owner_active=False)
        await engine.run_pass(db, now=T0, checks=CHECKS)
        rows = list((await _open(db)).values())
        assert len(rows) == 3
        agent.health_enrolled = False
        await db.flush()
        for step in (1, 2):
            engine._LAST_RUN.clear()
            await engine.run_pass(db, now=T0 + timedelta(minutes=15 * step), checks=CHECKS)
        for row in rows:
            await db.refresh(row)
            assert row.status == STATUS_RESOLVED


@pytest.mark.asyncio
async def test_a_cost_spike_is_reported_over_three_times_the_daily_average() -> None:
    async with _temp_database("hac_cost") as sessions, sessions() as db:
        owner, agent, assignment = await _world(db)
        db.add(
            AssignmentRun(
                assignment_id=assignment.id,
                agent_id=agent.id,
                user_id=owner.id,
                status="completed",
                total_input_tokens=400_000,
                total_output_tokens=10_000,
                created_at=T0 - timedelta(hours=2),
            )
        )
        await db.flush()
        await engine.run_pass(db, now=T0, checks=CHECKS)
        spike = (await _open(db))["agents.cost_spike"]
        assert spike.evidence["values"] == {"tokens_last_day": 410_000, "tokens_previous_7_days": 0}


async def _runs(
    db: AsyncSession, owner: User, agent: Agent, assignment: Assignment, *, failed: int, total: int
) -> None:
    for i in range(total):
        db.add(
            AssignmentRun(
                assignment_id=assignment.id,
                agent_id=agent.id,
                user_id=owner.id,
                status="failed" if i < failed else "completed",
                created_at=T0 - timedelta(hours=1, minutes=i),
            )
        )
    await db.flush()


@pytest.mark.asyncio
async def test_a_failure_spike_over_the_last_settled_runs_is_reported() -> None:
    """3 failed of the last 20 settled runs (15 %) crosses the 5 % default; the assignment is not failing (last 3 ok)."""
    async with _temp_database("hac_spike") as sessions, sessions() as db:
        owner, agent, assignment = await _world(db)
        # Newer than the three failures of the world: the last three judged runs succeed.
        for minutes in (3, 2, 1):
            db.add(
                AssignmentRun(
                    assignment_id=assignment.id,
                    agent_id=agent.id,
                    user_id=owner.id,
                    status="completed",
                    created_at=T0 - timedelta(minutes=minutes),
                )
            )
        await _runs(db, owner, agent, assignment, failed=0, total=17)
        await engine.run_pass(db, now=T0, checks=CHECKS)
        found = await _open(db)
        assert "agents.assignment_failing" not in found
        spike = found["agents.failure_spike"]
        assert spike.subject == {"agent_id": str(agent.id), "assignment_id": str(assignment.id)}
        assert spike.evidence["values"] == {"failure_rate_pct": 15.0, "runs": 20, "failed": 3}


@pytest.mark.asyncio
async def test_an_edited_failure_threshold_applies_on_the_next_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.health.settings import HealthSignalSettings

    async with _temp_database("hac_threshold") as sessions, sessions() as db:
        owner, agent, assignment = await _world(db)
        await _runs(db, owner, agent, assignment, failed=0, total=17)
        await engine.run_pass(db, now=T0, checks=CHECKS)
        row = (await _open(db))["agents.failure_spike"]

        async def relaxed(db: AsyncSession) -> HealthSignalSettings:
            return HealthSignalSettings(failure_rate_pct=50)

        monkeypatch.setattr(agent_checks, "_signal_settings", relaxed)
        for step in (1, 2):
            engine._LAST_RUN.clear()
            await engine.run_pass(db, now=T0 + timedelta(minutes=15 * step), checks=CHECKS)
        await db.refresh(row)
        assert row.status == STATUS_RESOLVED


@pytest.mark.asyncio
async def test_a_switched_off_signal_records_no_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.health.settings import HealthSignalSettings

    async def off(db: AsyncSession) -> HealthSignalSettings:
        return HealthSignalSettings(failure_enabled=False)

    monkeypatch.setattr(agent_checks, "_signal_settings", off)
    async with _temp_database("hac_signal_off") as sessions, sessions() as db:
        owner, agent, assignment = await _world(db)
        await _runs(db, owner, agent, assignment, failed=0, total=17)
        await engine.run_pass(db, now=T0, checks=CHECKS)
        assert "agents.failure_spike" not in await _open(db)


@pytest.mark.asyncio
async def test_with_ai_switched_off_no_agent_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.ai_state as ai_state

    async def disabled(db: Any) -> bool:
        return False

    monkeypatch.setattr(ai_state, "is_ai_enabled", disabled)
    async with _temp_database("hac_ai_off") as sessions, sessions() as db:
        await _world(db, owner_active=False)
        totals = await engine.run_pass(db, now=T0, checks=CHECKS)
        assert totals["checks"] == 7 and totals["opened"] == 0
        assert await _open(db) == {}


@pytest.mark.asyncio
async def test_with_ai_switched_off_an_open_finding_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.ai_state as ai_state

    async with _temp_database("hac_ai_keep") as sessions, sessions() as db:
        await _world(db, owner_active=False)
        await engine.run_pass(db, now=T0, checks=CHECKS)
        rows = list((await _open(db)).values())

        async def disabled(db: Any) -> bool:
            return False

        monkeypatch.setattr(ai_state, "is_ai_enabled", disabled)
        for step in (1, 2):
            engine._LAST_RUN.clear()
            await engine.run_pass(db, now=T0 + timedelta(minutes=15 * step), checks=CHECKS)
        for row in rows:
            await db.refresh(row)
            assert row.status == STATUS_OPEN
