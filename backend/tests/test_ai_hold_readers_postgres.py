"""What reads a hold, against real PostgreSQL.

``test_ai_hold_postgres.py`` covers the engine's ``defer`` step on the two
orchestration tables. The hold is also READ by queries over the domain tables,
and what matters there is the SQL itself - the JSONB ``?`` operator on the
execution payload, the JSON ``->`` accessor on a run's trigger context, the
states a correlated ``EXISTS`` accepts - so it is run here, on the full schema:

- the stale-evaluation sweep, which must treat a held evaluation as live for
  as long as its execution is in flight, including the moments it is claimed
  for a re-check and the moment its hold closes;
- the per-user fan-out, which must skip a user whose held run is the catch-up
  while keeping, for a run that was never held, the dedup window of ``main``;
- the linked-run marker of a held delegation, a best-effort write that must
  never cost the task its own marker.

Same scratch-database recipe as ``test_assignment_stale_reference_postgres.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.assignment import Assignment, AssignmentRun
from app.models.evaluation import Evaluation, EvaluationRun
from app.models.sub_agent_task import SubAgentTask
from app.services import ai_hold
from app.services import orchestration_engine as engine
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

REFUSAL = "license_required"


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "aiholdr",
        tables=ALL_TABLES,
        extensions=("vector",),
        bind_app_sessions=False,
    ) as db:
        yield db.sessions


async def _seed_user(db, label: str):
    from app.models.user import User

    user = User(
        id=uuid.uuid4(),
        email=f"aihold-{label}-{uuid.uuid4().hex[:8]}@test.local",
        full_name=f"AI Hold {label}",
        hashed_password="x",
    )
    db.add(user)
    await db.flush()
    return user


async def _seed_agent(db, owner):
    from app.models.agent import Agent

    agent = Agent(id=uuid.uuid4(), name=f"Held agent {uuid.uuid4().hex[:6]}", user_id=owner.id)
    db.add(agent)
    await db.flush()
    return agent


# -- The stale-evaluation sweep ----------------------------------------------


async def _seed_evaluation(db, agent, *, created_at: datetime) -> tuple[uuid.UUID, list[uuid.UUID]]:
    evaluation = Evaluation(id=uuid.uuid4(), name=f"Benchmark {uuid.uuid4().hex[:6]}")
    db.add(evaluation)
    await db.flush()
    runs = [
        EvaluationRun(
            id=uuid.uuid4(),
            evaluation_id=evaluation.id,
            agent_id=agent.id,
            model_id=model_id,
            status="pending",
            trigger_reason="schedule",
            created_at=created_at,
        )
        for model_id in ("model-a", "model-b")
    ]
    db.add_all(runs)
    await db.flush()
    return evaluation.id, [run.id for run in runs]


async def _evaluation_execution(sessions, evaluation_id: uuid.UUID) -> uuid.UUID:
    async with sessions() as db:
        execution = await engine.create_execution(
            db,
            adapter="evaluation",
            payload={"evaluation_id": str(evaluation_id), "trigger_reason": "schedule"},
            deadline_at=datetime.now(UTC) + timedelta(hours=1),
        )
        await db.commit()
        return execution.id


async def _claim(sessions, worker: str) -> list[uuid.UUID]:
    async with sessions() as db:
        rows = await engine.claim_due(db, adapter="evaluation", worker_id=worker, limit=10)
        ids = [row.id for row in rows]
        await db.commit()
    return ids


async def _hold(sessions, execution_id: uuid.UUID, worker: str) -> None:
    """What the reconciler applies for the adapter's ``hold_result``, due
    again at once so the next claim is the next re-check."""
    async with sessions() as db:
        await engine.defer(
            db,
            execution_id=execution_id,
            worker_id=worker,
            reason=ai_hold.held_message(REFUSAL),
            delay_seconds=0.0,
            adapter_state=ai_hold.held_adapter_state(REFUSAL),
        )
        await db.commit()


async def _statuses(sessions, run_ids: list[uuid.UUID]) -> set[str]:
    async with sessions() as db:
        rows = await db.execute(select(EvaluationRun.status).where(EvaluationRun.id.in_(run_ids)))
        return {status for (status,) in rows.all()}


# -- The per-user fan-out dedup ----------------------------------------------


@pytest.mark.asyncio
async def test_the_fanout_skips_a_held_catch_up_but_keeps_mains_window_for_the_rest(monkeypatch):
    """Daily fan-out, so a one-day dedup window. Runs three days old: a HELD
    one (the hold marker) and one that just resumed from its hold (running)
    are that user's catch-up; a paused (HITL) or delayed run that was never
    held does not block the user's run, exactly as on ``main``."""
    from app.services import agentic_gate, assignment_engine, autonomous

    monkeypatch.setattr(agentic_gate, "agentic_refusal", AsyncMock(return_value=None))
    trigger = AsyncMock(return_value="run")
    monkeypatch.setattr(assignment_engine, "trigger_assignment", trigger)

    now = datetime.now(UTC)
    old = now - timedelta(days=3)
    async with _temp_database() as sessions:
        async with sessions() as db:
            owner = await _seed_user(db, "owner")
            agent = await _seed_agent(db, owner)
            assignment = Assignment(
                id=uuid.uuid4(),
                agent_id=agent.id,
                name="Daily briefing",
                trigger_type="schedule",
                prompt="Brief me",
            )
            db.add(assignment)
            await db.flush()
            users = {
                label: await _seed_user(db, label)
                for label in ("held", "resumed", "paused", "delayed", "recent", "fresh")
            }
            fanout = {"type": "schedule_fanout", "cron": "0 8 * * *"}
            runs = [
                ("held", "pending", {**fanout, ai_hold.HOLD_MARKER_KEY: ai_hold.hold_marker(REFUSAL)}, old),
                ("resumed", "running", fanout, old),
                ("paused", "awaiting_human", fanout, old),
                ("delayed", "delayed", fanout, old),
                ("recent", "completed", fanout, now - timedelta(hours=1)),
            ]
            db.add_all(
                AssignmentRun(
                    id=uuid.uuid4(),
                    assignment_id=assignment.id,
                    agent_id=agent.id,
                    user_id=users[label].id,
                    status=status,
                    trigger_context=context,
                    created_at=created_at,
                )
                for label, status, context, created_at in runs
            )
            await db.commit()

        async with sessions() as db:
            fanned = await autonomous._fanout_assignment_to_users(
                db,
                SimpleNamespace(id=assignment.id, name=assignment.name),
                SimpleNamespace(name=agent.name),
                now,
                "0 8 * * *",
            )

    triggered = {call.kwargs["triggered_by_user_id"] for call in trigger.await_args_list}
    expected = {owner.id, users["paused"].id, users["delayed"].id, users["fresh"].id}
    assert triggered == expected, {
        label: user.id in triggered for label, user in {**users, "owner": owner}.items()
    }
    assert fanned == len(expected)


# -- The stale-assignment sweep ----------------------------------------------


@pytest.mark.asyncio
async def test_a_long_hold_never_crowds_the_stale_assignment_sweep_out_of_its_batch():
    """The sweep reads the 500 oldest active runs. A week-long hold keeps
    hundreds of old held runs active, each with a live execution the sweep
    would skip anyway: they must not take the batch, or a genuinely orphaned
    run behind them is never swept. A held run whose execution is gone is
    still an orphan, and still swept."""
    from app.models.orchestration import OrchestrationExecution
    from app.services.autonomous import _cleanup_stale_assignment_runs
    from app.services.orchestration_adapters.assignment import execution_dedup_key

    now = datetime.now(UTC)
    held_live_count = 510
    async with _temp_database() as sessions:
        async with sessions() as db:
            owner = await _seed_user(db, "owner")
            agent = await _seed_agent(db, owner)
            assignment = Assignment(
                id=uuid.uuid4(),
                agent_id=agent.id,
                name="Nightly triage",
                trigger_type="schedule",
                prompt="Triage",
            )
            db.add(assignment)
            await db.flush()
            marker = {ai_hold.HOLD_MARKER_KEY: ai_hold.hold_marker(REFUSAL)}

            def _run(created_at, context=None):
                return AssignmentRun(
                    id=uuid.uuid4(),
                    assignment_id=assignment.id,
                    agent_id=agent.id,
                    user_id=owner.id,
                    status="pending",
                    trigger_context=context,
                    created_at=created_at,
                )

            held_live = [_run(now - timedelta(days=7, minutes=i), marker) for i in range(held_live_count)]
            held_orphan = _run(now - timedelta(days=3), marker)
            stale = _run(now - timedelta(days=2))
            db.add_all([*held_live, held_orphan, stale])
            await db.flush()
            db.add_all(
                OrchestrationExecution(
                    id=uuid.uuid4(),
                    adapter="assignment",
                    state="pending",
                    payload={"assignment_run_id": str(run.id), "hold": {"reason": REFUSAL}},
                    run_at=now + timedelta(minutes=5),
                    dedup_key=execution_dedup_key(run.id),
                )
                for run in held_live
            )
            await db.commit()

        async with sessions() as db:
            cleaned = await _cleanup_stale_assignment_runs(db)

        async with sessions() as db:
            rows = dict(
                (
                    await db.execute(
                        select(AssignmentRun.id, AssignmentRun.status).where(
                            AssignmentRun.assignment_id == assignment.id
                        )
                    )
                ).all()
            )

    assert rows[stale.id] == "failed"
    assert rows[held_orphan.id] == "failed"
    assert {rows[run.id] for run in held_live} == {"pending"}
    assert cleaned == 2


# -- A held delegation's linked run ------------------------------------------


@pytest.mark.asyncio
async def test_a_linked_run_that_cannot_be_marked_never_costs_the_task_its_marker():
    """``_mark_run_held`` is best effort, on the session that is about to
    commit the task's own marker. When its read fails (here: the run table is
    locked past ``lock_timeout``), PostgreSQL aborts the whole transaction -
    swallowing the exception is not enough, and without a savepoint the
    task's marker is rolled back with it."""
    from app.services import sub_agent

    async with _temp_database() as sessions:
        async with sessions() as db:
            owner = await _seed_user(db, "owner")
            agent = await _seed_agent(db, owner)
            run = AssignmentRun(
                id=uuid.uuid4(),
                assignment_id=None,
                agent_id=agent.id,
                user_id=owner.id,
                status="pending",
            )
            task = SubAgentTask(
                id=uuid.uuid4(),
                user_id=owner.id,
                parent_agent_id=agent.id,
                target_agent_id=agent.id,
                task_description="Summarise the week's exposures",
                status="pending",
            )
            db.add_all([run, task])
            await db.commit()

        async with sessions() as blocker:
            await blocker.execute(text("LOCK TABLE assignment_runs IN ACCESS EXCLUSIVE MODE"))
            async with sessions() as db:
                await db.execute(text("SET LOCAL lock_timeout = '200ms'"))
                row = (
                    await db.execute(select(SubAgentTask).where(SubAgentTask.id == task.id))
                ).scalar_one()
                row.metadata_json = ai_hold.with_hold_marker(row.metadata_json, REFUSAL)
                await sub_agent._mark_run_held(db, str(run.id), REFUSAL)
                await db.commit()
            await blocker.rollback()

        async with sessions() as db:
            stored = (
                await db.execute(select(SubAgentTask.metadata_json).where(SubAgentTask.id == task.id))
            ).scalar_one()
    assert ai_hold.held_code_of(stored) == REFUSAL
