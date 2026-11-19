"""Platform Health finding store on PostgreSQL (#8259): a pattern is recorded once
across cycles, a resolved one reopens, a dismissed one stays silent, and the
digest counts tell new findings from known ones."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models.agent import Agent
from app.models.health_finding import STATUS_DISMISSED, STATUS_OPEN, STATUS_RESOLVED, HealthFinding
from app.models.user import User
from app.services.health.findings import Evidence, FindingError, list_findings, record_finding, window_counts
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)


async def _agent(db) -> Agent:
    owner = User(
        id=uuid.uuid4(), email=f"cf-{uuid.uuid4().hex[:8]}@example.com", full_name="CF", hashed_password="x"
    )
    db.add(owner)
    await db.flush()
    agent = Agent(id=uuid.uuid4(), name="Billing agent", user_id=owner.id, persona="You bill.")
    db.add(agent)
    await db.flush()
    return agent


async def _record(db, agent: Agent, *, key: str, hypothesis: str, now: datetime, evidence: Evidence | None = None):
    return await record_finding(
        db,
        target_type="agent",
        target_id=agent.id,
        pattern_kind="tool_errors",
        pattern_key=key,
        hypothesis=hypothesis,
        suggested_experiment="Shorten the queries." if hypothesis == "first" else "",
        evidence=evidence or Evidence(),
        content_evidence=bool(evidence and evidence.trace_ids),
        author_agent_id=None,
        now=now,
    )


@pytest.mark.asyncio
async def test_a_pattern_is_recorded_once_and_a_resolved_one_reopens():
    async with _temp_database("cf_once") as sessions, sessions() as db:
        agent = await _agent(db)
        first = await _record(db, agent, key="search_knowledge", hypothesis="first", now=T0, evidence=Evidence(trace_ids=("t1",)))
        assert (first.new, first.reopened, first.finding.status) == (True, False, STATUS_OPEN)

        again = await _record(
            db, agent, key="  search_knowledge ", hypothesis="second", now=T0 + timedelta(days=7),
            evidence=Evidence(trace_ids=("t2",), counts={"errors": 3}),
        )
        assert (again.new, again.reopened) == (False, False)
        assert again.finding.id == first.finding.id
        assert again.finding.occurrences == 2
        assert again.finding.evidence == {"trace_ids": ["t2", "t1"], "rating_ids": [], "run_ids": [], "counts": {"errors": 3}}
        assert again.finding.hypothesis == "second"
        assert again.finding.suggested_experiment == "Shorten the queries."
        assert again.finding.content_evidence is True
        assert again.finding.reported_at == T0
        assert await db.scalar(select(func.count()).select_from(HealthFinding)) == 1

        again.finding.status = STATUS_RESOLVED
        await db.flush()
        recurred = await _record(db, agent, key="search_knowledge", hypothesis="third", now=T0 + timedelta(days=14))
        assert (recurred.new, recurred.reopened, recurred.finding.status) == (True, True, STATUS_OPEN)
        assert recurred.finding.reported_at == T0 + timedelta(days=14)
        assert recurred.finding.occurrences == 3


@pytest.mark.asyncio
async def test_a_dismissed_finding_stays_silent():
    async with _temp_database("cf_dismissed") as sessions, sessions() as db:
        agent = await _agent(db)
        first = await _record(db, agent, key="web_fetch", hypothesis="first", now=T0)
        first.finding.status = STATUS_DISMISSED
        await db.flush()
        again = await _record(db, agent, key="web_fetch", hypothesis="ignored", now=T0 + timedelta(days=7))
        assert (again.new, again.reopened, again.finding.status) == (False, False, STATUS_DISMISSED)
        assert again.finding.hypothesis == "first"
        assert again.finding.occurrences == 2


@pytest.mark.asyncio
async def test_window_counts_tell_new_from_known():
    async with _temp_database("cf_counts") as sessions, sessions() as db:
        agent, other = await _agent(db), await _agent(db)
        await _record(db, agent, key="a", hypothesis="first", now=T0)
        await _record(db, agent, key="a", hypothesis="again", now=T0 + timedelta(days=7))
        await _record(db, agent, key="b", hypothesis="first", now=T0 + timedelta(days=7))
        await _record(db, other, key="c", hypothesis="first", now=T0 + timedelta(days=8))
        week = {"since": T0 + timedelta(days=6), "until": T0 + timedelta(days=13)}
        assert await window_counts(db, **week) == {"new": 2, "known": 1}
        assert await window_counts(db, **week, target=("agent", agent.id)) == {"new": 1, "known": 1}
        assert await window_counts(db, since=T0 + timedelta(days=20)) == {"new": 0, "known": 0}


@pytest.mark.asyncio
async def test_list_findings_reads_the_targets_named_latest_seen_first():
    async with _temp_database("cf_list") as sessions, sessions() as db:
        agent, other = await _agent(db), await _agent(db)
        await _record(db, agent, key="a", hypothesis="first", now=T0)
        await _record(db, agent, key="b", hypothesis="first", now=T0 + timedelta(days=1))
        await _record(db, other, key="c", hypothesis="first", now=T0 + timedelta(days=2))
        listed = await list_findings(db, target_ids=[agent.id])
        assert [f.pattern_key for f in listed] == ["b", "a"]
        assert [f.pattern_key for f in await list_findings(db, target_ids=None, limit=2)] == ["c", "b"]
        assert await list_findings(db, target_ids=[]) == []
        assert await list_findings(db, target_ids=[agent.id], status=STATUS_DISMISSED) == []
        with pytest.raises(FindingError):
            await list_findings(db, target_ids=None, status="nope")
