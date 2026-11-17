"""No run escapes an agent deletion's cancellation, against a real PostgreSQL.

The deletion cancels the live executions of the agent's runs, read once, and
then deletes the runs. A run a trigger created between that read and the
delete was deleted with the rest while its execution stayed pending - the
orphan the cancellation exists to prevent (it holds a reconciler slot, and for
a delegation the fan-out counter). The deletion takes the agent's row lock
before reading the runs, and a run is inserted under a key-share lock on its
agent: from there on no run can be created for the agent until the deletion
ended, when the agent is gone.
"""

from __future__ import annotations

import datetime as dt
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import DBAPIError

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.a2a import tasks as a2a_tasks
from app.api.v1 import agents as agents_api
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.audit_log import AuditLog
from app.models.orchestration import OrchestrationExecution
from app.models.sub_agent_task import SubAgentTask
from app.models.user import User
from app.services import agent_deletion, agent_memory, orchestration_engine
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_no_run_is_created_for_an_agent_once_its_deletion_read_its_runs(monkeypatch):
    owner_id, agent_id, assignment_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    late_run = uuid.uuid4()
    attempts: list[str] = []

    async with scratch_database("agentrun", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as scratch:
        async with scratch.sessions() as db:
            db.add(User(id=owner_id, email=f"{owner_id}@example.com", hashed_password="x", full_name="Owner"))
            await db.flush()
            db.add(Agent(id=agent_id, user_id=owner_id, name="Doomed", slug=f"doomed-{agent_id.hex[:8]}", persona="p"))
            await db.flush()
            db.add(Assignment(id=assignment_id, agent_id=agent_id, user_id=owner_id, name="Nightly", trigger_type="manual", prompt="go"))
            await db.commit()

        cancel = orchestration_engine.cancel_executions_for_runs

        async def _a_trigger_fires_meanwhile(db, run_ids):
            """A run and its execution, created on another connection right after the runs were read."""
            try:
                async with scratch.engine.begin() as conn:
                    await conn.execute(text("SET LOCAL lock_timeout = '500ms'"))
                    await conn.execute(
                        insert(AssignmentRun).values(
                            id=late_run, agent_id=agent_id, assignment_id=assignment_id, user_id=owner_id, status="pending"
                        )
                    )
                    await conn.execute(
                        insert(OrchestrationExecution).values(
                            id=uuid.uuid4(),
                            adapter="assignment_run",
                            state="pending",
                            payload={"run_id": str(late_run)},
                            dedup_key=f"assignment_run:{late_run}",
                        )
                    )
                attempts.append("created")
            except DBAPIError as exc:
                attempts.append(getattr(exc.orig, "sqlstate", None) or type(exc.orig).__name__)
            return await cancel(db, run_ids)

        monkeypatch.setattr(orchestration_engine, "cancel_executions_for_runs", _a_trigger_fires_meanwhile)
        monkeypatch.setattr(agent_memory, "clear_deleted_agent_memory", AsyncMock())

        async with scratch.sessions() as db:
            owner = await db.get(User, owner_id)
            await agents_api.delete_agent(agent_id=str(agent_id), request=None, user=owner, db=db, _ai_gate=None)

        async with scratch.sessions() as db:
            live = (
                await db.execute(
                    select(OrchestrationExecution.dedup_key).where(OrchestrationExecution.state.in_(("pending", "running")))
                )
            ).scalars().all()
            assert await db.get(Agent, agent_id) is None

    # The trigger waited on the agent's row lock, and gave up: no run was
    # created for an agent being deleted, and no execution outlives its run.
    assert attempts == ["55P03"], attempts
    assert live == []


@pytest.mark.asyncio
async def test_a_long_history_goes_in_committed_batches_and_a_failure_keeps_them(monkeypatch):
    """An agent's runs go oldest first, a bounded batch per transaction.

    The second batch of the first deletion fails, as a statement timeout would:
    the first batch stays deleted, with its run's live execution and background
    task cancelled, and the next deletion resumes from the runs left.
    """
    owner_id, agent_id, assignment_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    started = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    run_ids = [uuid.uuid4() for _ in range(5)]
    oldest = run_ids[0]

    async with scratch_database("agentbatch", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as scratch:
        async with scratch.sessions() as db:
            db.add(User(id=owner_id, email=f"{owner_id}@example.com", hashed_password="x", full_name="Owner"))
            await db.flush()
            db.add(Agent(id=agent_id, user_id=owner_id, name="Guardian", slug=f"guardian-{agent_id.hex[:8]}", persona="p"))
            await db.flush()
            db.add(
                Assignment(
                    id=assignment_id,
                    agent_id=agent_id,
                    user_id=owner_id,
                    name="PRs",
                    trigger_type="webhook",
                    prompt="go",
                    execution_count=len(run_ids),
                )
            )
            await db.flush()
            db.add_all(
                AssignmentRun(
                    id=run_id,
                    agent_id=agent_id,
                    assignment_id=assignment_id,
                    user_id=owner_id,
                    status="running" if run_id == oldest else "completed",
                    created_at=started + dt.timedelta(minutes=i),
                )
                for i, run_id in enumerate(run_ids)
            )
            await db.flush()
            db.add_all(
                [
                    OrchestrationExecution(
                        id=uuid.uuid4(),
                        adapter="assignment_run",
                        state="running",
                        payload={"run_id": str(oldest)},
                        dedup_key=f"assignment_run:{oldest}",
                    ),
                    SubAgentTask(
                        id=uuid.uuid4(),
                        user_id=owner_id,
                        parent_agent_id=agent_id,
                        target_agent_id=agent_id,
                        task_description="look into it",
                        status="pending",
                        metadata_json={"parent_run_id": str(oldest)},
                    ),
                ]
            )
            await db.commit()

        monkeypatch.setattr(agent_deletion, "RUN_BATCH_SIZE", 2)
        monkeypatch.setattr(agent_memory, "clear_deleted_agent_memory", AsyncMock())
        batch = agent_deletion._delete_run_batch
        batches: list[int] = []

        async def _second_batch_times_out_once(db, aid):
            done = await batch(db, aid)
            batches.append(done.deleted)
            if len(batches) == 2:
                raise RuntimeError("canceling statement due to statement timeout")
            return done

        monkeypatch.setattr(agent_deletion, "_delete_run_batch", _second_batch_times_out_once)

        async def _delete() -> None:
            async with scratch.sessions() as db:
                owner = await db.get(User, owner_id)
                await agents_api.delete_agent(agent_id=str(agent_id), request=None, user=owner, db=db, _ai_gate=None)

        async def _left() -> tuple[list[uuid.UUID], list[str], list[str], bool]:
            async with scratch.sessions() as db:
                runs = (
                    await db.execute(
                        select(AssignmentRun.id).where(AssignmentRun.agent_id == agent_id).order_by(AssignmentRun.created_at)
                    )
                ).scalars().all()
                executions = (await db.execute(select(OrchestrationExecution.state))).scalars().all()
                tasks = (await db.execute(select(SubAgentTask.status))).scalars().all()
                return list(runs), list(executions), list(tasks), await db.get(Agent, agent_id) is not None

        async def _audited() -> list[str]:
            async with scratch.sessions() as db:
                return sorted((await db.execute(select(AuditLog.action))).scalars().all())

        with pytest.raises(HTTPException):
            await _delete()
        runs, executions, tasks, agent_left = await _left()
        assert batches == [2, 2]
        assert runs == run_ids[2:] and agent_left
        assert executions == ["cancelled"] and tasks == ["cancelled"]
        async with scratch.sessions() as db:
            # It counts the three runs it still has, not the five it had.
            assert (await db.get(Assignment, assignment_id)).execution_count == 3
        # The runs that went are audited, though the agent's deletion is not.
        assert await _audited() == ["purge_runs"]

        await _delete()
        runs, _executions, _tasks, agent_left = await _left()
        assert batches == [2, 2, 2, 1]
        assert runs == [] and not agent_left
        async with scratch.sessions() as db:
            assert (await db.execute(select(func.count()).select_from(AssignmentRun))).scalar() == 0
        assert await _audited() == ["delete", "purge_runs", "purge_runs"]


@pytest.mark.asyncio
async def test_a_run_another_deletion_took_first_is_not_uncounted_twice(monkeypatch):
    """The batch lowers ``execution_count`` by the runs it deleted.

    A single-run deletion takes no agent lock: between the batch's read of its
    runs and its lock on them, it removed one of them and lowered the count
    itself.
    """
    owner_id, agent_id, assignment_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    started = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    run_ids = [uuid.uuid4() for _ in range(4)]

    async with scratch_database("agentrace", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as scratch:
        async with scratch.sessions() as db:
            db.add(User(id=owner_id, email=f"{owner_id}@example.com", hashed_password="x", full_name="Owner"))
            await db.flush()
            db.add(Agent(id=agent_id, user_id=owner_id, name="Guardian", slug=f"guardian-{agent_id.hex[:8]}", persona="p"))
            await db.flush()
            db.add(
                Assignment(
                    id=assignment_id,
                    agent_id=agent_id,
                    user_id=owner_id,
                    name="PRs",
                    trigger_type="webhook",
                    prompt="go",
                    execution_count=len(run_ids),
                )
            )
            await db.flush()
            db.add_all(
                AssignmentRun(
                    id=run_id,
                    agent_id=agent_id,
                    assignment_id=assignment_id,
                    user_id=owner_id,
                    status="completed",
                    created_at=started + dt.timedelta(minutes=i),
                )
                for i, run_id in enumerate(run_ids)
            )
            await db.commit()

        monkeypatch.setattr(agent_deletion, "RUN_BATCH_SIZE", 2)
        monkeypatch.setattr(agent_memory, "clear_deleted_agent_memory", AsyncMock())
        lock_tasks = a2a_tasks.lock_tasks_before_deletion
        raced: list[uuid.UUID] = []

        async def _a_run_is_deleted_meanwhile(db, **kwargs):
            # Between the batch's read of its runs and its lock on them.
            if not raced and kwargs.get("run_ids"):
                ids = list(kwargs["run_ids"])
                raced.append(ids[0])
                async with scratch.engine.begin() as conn:
                    await conn.execute(text("DELETE FROM assignment_runs WHERE id = :id"), {"id": ids[0]})
                    await conn.execute(
                        text("UPDATE assignments SET execution_count = execution_count - 1 WHERE id = :id"),
                        {"id": assignment_id},
                    )
            return await lock_tasks(db, **kwargs)

        monkeypatch.setattr(a2a_tasks, "lock_tasks_before_deletion", _a_run_is_deleted_meanwhile)
        batch = agent_deletion._delete_run_batch
        calls: list[tuple[int, int]] = []

        async def _second_batch_fails(db, aid):
            done = await batch(db, aid)
            calls.append((done.selected, done.deleted))
            if len(calls) == 2:
                raise RuntimeError("canceling statement due to statement timeout")
            return done

        monkeypatch.setattr(agent_deletion, "_delete_run_batch", _second_batch_fails)

        with pytest.raises(HTTPException):
            async with scratch.sessions() as db:
                owner = await db.get(User, owner_id)
                await agents_api.delete_agent(agent_id=str(agent_id), request=None, user=owner, db=db, _ai_gate=None)

        async with scratch.sessions() as db:
            left = (
                await db.execute(select(AssignmentRun.id).where(AssignmentRun.agent_id == agent_id).order_by(AssignmentRun.created_at))
            ).scalars().all()
            count = (await db.get(Assignment, assignment_id)).execution_count
        assert raced == [run_ids[0]]
        # The first batch read 2 runs and deleted 1; a full selection, so the
        # next batch ran (and failed).
        assert calls == [(2, 1), (2, 2)]
        assert left == run_ids[2:]
        assert count == len(left)
