"""Deleting a run lowers its assignment's ``execution_count`` once, against PostgreSQL.

``DELETE /assignments/runs/{id}`` locks the run before it lowers the count, so
a second deletion of the same run, started before the first committed, waits
on the run, finds it gone and answers 404 instead of lowering the count again.
Taking the run before the assignment is also the order the agent deletion's
run batches take them in.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.v1 import assignments as assignments_api
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.user import User
from tests.scratch_database import ALL_TABLES, LOCK_WAITERS_IN_THIS_DATABASE, scratch_database

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_two_deletions_of_one_run_lower_the_count_once(monkeypatch):
    owner_id, agent_id, assignment_id, run_id = (uuid.uuid4() for _ in range(4))

    async with scratch_database("rundelete", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as scratch:
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
                    execution_count=5,
                )
            )
            await db.flush()
            db.add(AssignmentRun(id=run_id, agent_id=agent_id, assignment_id=assignment_id, user_id=owner_id, status="completed"))
            await db.commit()

        first_holds_the_run = asyncio.Event()
        release_first = asyncio.Event()

        async def _files(*_a, **_k):
            # The first deletion pauses here, after it locked the run.
            if not first_holds_the_run.is_set():
                first_holds_the_run.set()
                await release_first.wait()
            return 0

        monkeypatch.setattr("app.services.file_cleanup.delete_file_attachments", _files)

        async def _delete() -> str:
            async with scratch.sessions() as db:
                owner = await db.get(User, owner_id)
                try:
                    await assignments_api.delete_run(run_id=str(run_id), user=owner, db=db)
                except HTTPException as exc:
                    return str(exc.status_code)
            return "204"

        first = asyncio.create_task(_delete())
        await asyncio.wait_for(first_holds_the_run.wait(), timeout=10)
        second = asyncio.create_task(_delete())
        waiting = 0
        try:
            # Until the second deletion waits on a lock the first one holds.
            async with scratch.engine.connect() as conn:
                for _ in range(100):
                    waiting = (await conn.execute(LOCK_WAITERS_IN_THIS_DATABASE)).scalar()
                    await conn.rollback()
                    if waiting:
                        break
                    await asyncio.sleep(0.05)
        finally:
            release_first.set()
        outcomes = sorted(await asyncio.wait_for(asyncio.gather(first, second), timeout=30))

        async with scratch.sessions() as db:
            count = (await db.get(Assignment, assignment_id)).execution_count
            run_left = await db.get(AssignmentRun, run_id)
        assert waiting, "the second deletion never waited on the first"
        assert outcomes == ["204", "404"]
        assert run_left is None
        assert count == 4
