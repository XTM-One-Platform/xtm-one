"""A run that ends cancels the background tasks it spawned that never started.

Nobody is left to collect such a task: started later, its result would have
no consumer (harness gap orphaned-background-task-on-parent-end).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import sub_agent


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _Db:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []
        self.commits = 0

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.rows)

    async def commit(self):
        self.commits += 1


def _task(status="pending"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        error=None,
        completed_at=None,
        started_at=None,
        metadata_json={"parent_run_id": "r1"},
    )


@pytest.mark.asyncio
async def test_unstarted_children_are_cancelled_with_their_cause():
    task = _task()
    db = _Db([task])
    execution = SimpleNamespace(id=uuid.uuid4(), state="pending")
    with (
        patch(
            "app.services.orchestration_engine.get_execution_by_dedup_key",
            AsyncMock(return_value=execution),
        ) as lookup,
        patch("app.services.orchestration_engine.cancel_execution", AsyncMock()) as cancel,
        patch.object(sub_agent, "_update_task_delegation", AsyncMock()) as delegation,
        patch.object(sub_agent, "_record_no_turn_ledger") as ledger,
    ):
        ids = await sub_agent.cancel_unstarted_tasks_of_ended_run(db, "r1")
    assert ids == [str(task.id)]
    assert task.status == "cancelled"
    assert task.error == sub_agent.PARENT_ENDED_TASK_REASON
    assert task.metadata_json["delivery_suppressed"] is True
    assert task.completed_at is not None
    lookup.assert_awaited_once()
    cancel.assert_awaited_once_with(db, execution_id=execution.id)
    delegation.assert_awaited_once_with(db, str(task.id), "cancelled", error=sub_agent.PARENT_ENDED_TASK_REASON)
    ledger.assert_called_once_with(task)
    assert db.commits == 1
    # Only pending, never-started children of this run are selected.
    sql = str(db.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "started_at IS NULL" in sql
    assert "'pending'" in sql
    assert "parent_run_id" in sql


@pytest.mark.asyncio
async def test_no_unstarted_child_writes_nothing():
    db = _Db([])
    ids = await sub_agent.cancel_unstarted_tasks_of_ended_run(db, "r1")
    assert ids == []
    assert db.commits == 0
