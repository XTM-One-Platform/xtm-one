"""The Quality Analyst's signals and evidence checks, on PostgreSQL (#8259).

Pinned here:

* ``quality_signals`` reads the agent's window and the one before from its
  stored traces and rollups, its ratings, and its open findings; trace ids and
  rating comments only with content access, never from an opted-out user;
* ``verify_evidence`` keeps only the refs its rows confirm for the agent: its
  ratings and runs, its traces with content access, nothing of an opted-out
  user or of another agent.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.agent import Agent
from app.models.annotation import Annotation
from app.models.assignment import AssignmentRun
from app.models.llm_trace import LLMTrace
from app.models.user import User
from app.services.health.access import AgentAccess, Reader
from app.services.health.diagnosis import quality_signals, verify_evidence
from app.services.health.findings import Evidence, record_finding
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def _user(tag: str) -> User:
    return User(
        id=uuid.uuid4(),
        email=f"cd-{tag}-{uuid.uuid4().hex[:8]}@example.com",
        full_name=f"CD {tag}",
        hashed_password="x",
    )


def _trace(agent_id, user_id, at, *, status="ok", cause=None, cost=0.01, calls=2, errors=0) -> LLMTrace:
    return LLMTrace(
        agent_id=agent_id,
        user_id=user_id,
        source="assignment",
        name="run",
        started_at=at,
        status=status,
        failure_cause=cause,
        input_tokens=100,
        output_tokens=50,
        total_cost=cost,
        metadata_json={"rollup": {"tools": {"search_knowledge": {"calls": calls, "errors": errors}}}},
    )


def _reader(owner: User, opted_out: User) -> Reader:
    scope = SimpleNamespace(opted_out_user_ids=frozenset({opted_out.id}))
    return Reader(user=owner, principal=None, scope=scope)


async def _seed(sessions) -> SimpleNamespace:
    async with sessions() as db:
        owner, opted_out = _user("owner"), _user("opted-out")
        db.add_all([owner, opted_out])
        await db.flush()
        agent = Agent(id=uuid.uuid4(), name="Billing agent", user_id=owner.id, persona="You bill.")
        other = Agent(id=uuid.uuid4(), name="Other agent", user_id=owner.id, persona="x")
        db.add_all([agent, other])
        await db.flush()
        run = AssignmentRun(agent_id=agent.id, user_id=owner.id, status="completed")
        other_run = AssignmentRun(agent_id=other.id, user_id=owner.id, status="completed")
        db.add_all([run, other_run])
        await db.flush()

        day = timedelta(days=1)
        ok = _trace(agent.id, owner.id, NOW - day)
        failed = _trace(agent.id, owner.id, NOW - 2 * day, status="error", cause="tool_error", cost=0.03, calls=3, errors=2)
        failed_opted_out = _trace(agent.id, opted_out.id, NOW - 3 * day, status="error", cause="tool_error", cost=0.02)
        previous = _trace(agent.id, owner.id, NOW - 9 * day, cost=0.01)
        foreign = _trace(other.id, owner.id, NOW - day)
        db.add_all([ok, failed, failed_opted_out, previous, foreign])

        owner_rating = Annotation(
            target_type="assignment_run", target_id=run.id, user_id=owner.id, agent_id=agent.id,
            rating="negative", comment="It billed the wrong account.",
        )
        opted_out_rating = Annotation(
            target_type="assignment_run", target_id=run.id, user_id=opted_out.id, agent_id=agent.id,
            rating="negative", comment="Private remark.",
        )
        foreign_rating = Annotation(
            target_type="assignment_run", target_id=other_run.id, user_id=owner.id, agent_id=other.id,
            rating="negative",
        )
        db.add_all([owner_rating, opted_out_rating, foreign_rating])
        await db.flush()
        finding = await record_finding(
            db,
            target_type="agent",
            target_id=agent.id,
            pattern_kind="tool_errors",
            pattern_key="search_knowledge",
            hypothesis="The search tool fails on long queries.",
            suggested_experiment="",
            evidence=Evidence(counts={"errors": 2}),
            content_evidence=False,
            author_agent_id=None,
        )
        await db.commit()
        return SimpleNamespace(
            owner=owner, opted_out=opted_out, agent=agent, run=run, other_run=other_run,
            ok=ok, failed=failed, failed_opted_out=failed_opted_out, foreign=foreign,
            owner_rating=owner_rating, opted_out_rating=opted_out_rating, foreign_rating=foreign_rating,
            finding_id=finding.finding.id,
        )


@pytest.mark.asyncio
async def test_quality_signals_read_both_windows_ratings_and_open_findings():
    async with _temp_database("cd_signals") as sessions:
        w = await _seed(sessions)
        reader = _reader(w.owner, w.opted_out)
        async with sessions() as db:
            signals = await quality_signals(db, reader, AgentAccess(agent=w.agent, content=True), days=7, now=NOW)

    current = signals["current"]
    assert (current["runs"], current["failed"], current["failure_rate_pct"]) == (3, 2, 66.7)
    assert current["failure_causes"] == {"tool_error": 2}
    assert current["tools"] == [{"tool": "search_knowledge", "calls": 7, "errors": 2, "error_rate_pct": 28.6}]
    assert current["cost_per_run"] == 0.02
    assert (signals["previous"]["runs"], signals["previous"]["cost_per_run"]) == (1, 0.01)
    assert signals["drift_pct"]["cost_per_run"] == 100.0
    # The opted-out user's failed trace counts in the figures, never by its id.
    assert signals["failed_trace_ids"] == [str(w.failed.id)]

    feedback = signals["feedback"]
    assert [item["rating_id"] for item in feedback["latest_negative"]] == [str(w.owner_rating.id)]
    assert feedback["latest_negative"][0]["comment"] == "It billed the wrong account."
    assert signals["known_findings"] == [
        {"id": str(w.finding_id), "pattern_kind": "tool_errors", "pattern_key": "search_knowledge", "status": "open"}
    ]


@pytest.mark.asyncio
async def test_quality_signals_serve_no_content_without_content_access():
    async with _temp_database("cd_metrics") as sessions:
        w = await _seed(sessions)
        async with sessions() as db:
            signals = await quality_signals(
                db, _reader(w.owner, w.opted_out), AgentAccess(agent=w.agent, content=False), days=7, now=NOW
            )

    assert signals["content_readable"] is False
    assert "failed_trace_ids" not in signals
    assert [item["rating_id"] for item in signals["feedback"]["latest_negative"]] == [str(w.owner_rating.id)]
    assert "comment" not in signals["feedback"]["latest_negative"][0]


def _unstarted(agent_id, user_id, at, *, cause="engine_deadline_exceeded", status="failed") -> AssignmentRun:
    """A run its deadline stopped in the queue: no trace, never started (#1416)."""
    return AssignmentRun(
        agent_id=agent_id,
        user_id=user_id,
        status=status,
        error=f"Deadline exceeded at {at.isoformat()}.",
        failure_cause=cause,
        created_at=at,
        completed_at=at,
    )


@pytest.mark.asyncio
async def test_runs_failed_before_start_count_in_both_windows_and_once():
    async with _temp_database("cd_unstarted") as sessions:
        async with sessions() as db:
            owner, opted_out = _user("owner"), _user("opted-out")
            db.add_all([owner, opted_out])
            await db.flush()
            agent = Agent(id=uuid.uuid4(), name="Queued agent", user_id=owner.id, persona="x")
            db.add(agent)
            await db.flush()
            day = timedelta(days=1)
            # Failed without a start, but traced: counted once, as its trace.
            traced_run = _unstarted(agent.id, owner.id, NOW - 2 * day, cause="tool_error")
            unstarted = [
                _unstarted(agent.id, owner.id, NOW - day),
                _unstarted(agent.id, owner.id, NOW - day, cause="engine_never_claimed", status="cancelled"),
                _unstarted(agent.id, opted_out.id, NOW - 3 * day),
            ]
            started = AssignmentRun(
                agent_id=agent.id, user_id=owner.id, status="failed", created_at=NOW - day, started_at=NOW - day
            )
            previous = _unstarted(agent.id, owner.id, NOW - 9 * day, cause=None)
            db.add_all([traced_run, *unstarted, started, previous])
            await db.flush()
            failed_trace = _trace(agent.id, owner.id, NOW - 2 * day, status="error", cause="tool_error")
            failed_trace.assignment_run_id = traced_run.id
            db.add_all(
                [
                    _trace(agent.id, owner.id, NOW - day),
                    _trace(agent.id, owner.id, NOW - day),
                    failed_trace,
                    _trace(agent.id, owner.id, NOW - 9 * day),
                ]
            )
            await db.commit()
        async with sessions() as db:
            signals = await quality_signals(
                db, _reader(owner, opted_out), AgentAccess(agent=agent, content=True), days=7, now=NOW
            )
            metrics_only = await quality_signals(
                db, _reader(owner, opted_out), AgentAccess(agent=agent, content=False), days=7, now=NOW
            )

    current = signals["current"]
    # 2 traced ok + 1 traced failed + 3 failed before start; the started run without a trace is not one.
    assert (current["runs"], current["failed"], current["failure_rate_pct"]) == (6, 4, 66.7)
    assert current["failed_before_start"] == 3
    assert current["failed_before_start_causes"] == {"deadline_before_start": 3}
    previous_figures = signals["previous"]
    assert (previous_figures["runs"], previous_figures["failed"], previous_figures["failed_before_start"]) == (2, 1, 1)
    assert previous_figures["failure_rate_pct"] == 50.0
    assert signals["drift_pct"]["failure_rate"] == 33.4
    # Run ids beside the trace ids, never an opted-out user's, never without content access.
    assert sorted(signals["failed_before_start_run_ids"]) == sorted(str(r.id) for r in unstarted[:2])
    assert signals["failed_trace_ids"] == [str(failed_trace.id)]
    assert "failed_before_start_run_ids" not in metrics_only
    assert metrics_only["current"]["failed_before_start"] == 3


@pytest.mark.asyncio
async def test_verify_evidence_keeps_only_the_agent_s_own_refs():
    async with _temp_database("cd_evidence") as sessions:
        w = await _seed(sessions)
        reader = _reader(w.owner, w.opted_out)
        raw = {
            "rating_ids": [str(w.owner_rating.id), str(w.opted_out_rating.id), str(w.foreign_rating.id), "nope"],
            "run_ids": [str(w.run.id), str(w.other_run.id)],
            "trace_ids": [str(w.failed.id), str(w.failed_opted_out.id), str(w.foreign.id)],
            "counts": {"errors": 2, "flag": True, "note": "x"},
        }
        async with sessions() as db:
            with_content, dropped = await verify_evidence(db, reader, AgentAccess(agent=w.agent, content=True), raw)
            metrics_only, dropped_metrics = await verify_evidence(
                db, reader, AgentAccess(agent=w.agent, content=False), raw
            )

    assert with_content == Evidence(
        trace_ids=(str(w.failed.id),), rating_ids=(str(w.owner_rating.id),), run_ids=(str(w.run.id),), counts={"errors": 2}
    )
    # Eight well-formed refs asked ("nope" is not a ref), three kept.
    assert dropped == 5
    assert metrics_only.trace_ids == ()
    assert dropped_metrics == 6
