"""Tests for the run -> background-task cancellation cascade.

Background tasks (``sub_agent_tasks``) live in their own table, decoupled from
``AssignmentRun``. Deleting a run must also cancel the background tasks spawned
under it (matched via the run id stored on the task's ``metadata_json``), so a
deleted run never strands a task on ``pending`` / ``running``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import sub_agent


class _ScalarsResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._items))


class _FakeDB:
    """Minimal fake session: every ``execute`` returns the same preset rows."""

    def __init__(self, items):
        self._items = items
        self.executes = 0

    async def execute(self, *_args, **_kwargs):
        self.executes += 1
        return _ScalarsResult(self._items)


def _task(run_id: str, *, status: str, link_key: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        error=None,
        completed_at=None,
        metadata_json={link_key: run_id},
    )


@pytest.mark.asyncio
async def test_cascade_cancels_linked_pending_and_running_tasks():
    run_id = str(uuid.uuid4())
    pending = _task(run_id, status="pending", link_key="assignment_run_id")
    running = _task(run_id, status="running", link_key="parent_run_id")
    db = _FakeDB([pending, running])

    with patch.object(sub_agent, "_update_task_delegation", new=AsyncMock()):
        cancelled = await sub_agent.mark_sub_agent_tasks_cancelled_for_runs(
            db, [run_id]
        )

    assert cancelled == 2
    assert pending.status == "cancelled"
    assert running.status == "cancelled"
    assert pending.metadata_json["delivery_suppressed"] is True
    assert running.metadata_json["delivery_suppressed"] is True
    assert pending.completed_at is not None
    assert running.completed_at is not None


@pytest.mark.asyncio
async def test_a_deletion_says_so_and_another_cause_says_its_own():
    run_id = str(uuid.uuid4())
    deleted = _task(run_id, status="running", link_key="assignment_run_id")
    stopped = _task(run_id, status="running", link_key="assignment_run_id")
    audit = AsyncMock()

    with patch.object(sub_agent, "_update_task_delegation", new=audit):
        await sub_agent.mark_sub_agent_tasks_cancelled_for_runs(_FakeDB([deleted]), [run_id])
        await sub_agent.mark_sub_agent_tasks_cancelled_for_runs(
            _FakeDB([stopped]), [run_id], reason="Cancelled because the partner cancelled the task."
        )

    assert deleted.error == sub_agent.RUN_DELETED_TASK_REASON
    assert stopped.error == "Cancelled because the partner cancelled the task."
    assert [call.kwargs["error"] for call in audit.await_args_list] == [
        "Run deleted.",
        "Cancelled because the partner cancelled the task.",
    ]


@pytest.mark.asyncio
async def test_cascade_empty_run_ids_is_noop():
    db = _FakeDB([])
    assert await sub_agent.mark_sub_agent_tasks_cancelled_for_runs(db, []) == 0
    # None ids are filtered out -> still a no-op, no DB hit.
    assert await sub_agent.mark_sub_agent_tasks_cancelled_for_runs(db, [None]) == 0
    assert db.executes == 0


@pytest.mark.asyncio
async def test_cascade_batches_and_dedups_across_queries(monkeypatch):
    # A task can link to two deleted runs (its own assignment_run_id and its
    # orchestrator parent_run_id). Shrinking the batch forces those ids into
    # separate queries that each return the task; dedup-by-id must cancel it
    # once, not once per batch. Guards the bind-parameter-limit batching.
    monkeypatch.setattr(sub_agent, "_RUN_ID_QUERY_CHUNK", 1)
    run_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    task = _task(run_ids[0], status="running", link_key="assignment_run_id")
    db = _FakeDB([task])  # the same task is returned for every batch query

    with patch.object(sub_agent, "_update_task_delegation", new=AsyncMock()):
        cancelled = await sub_agent.mark_sub_agent_tasks_cancelled_for_runs(
            db, run_ids
        )

    assert cancelled == 1  # deduped across the two per-batch queries
    assert task.status == "cancelled"
    assert db.executes == 2  # one matching query per batch


def test_already_cancelled_only_preserves_cancelled_status():
    # The worker's terminal-write guard (_execute_sub_agent_task) must preserve a
    # KILLED task (status == "cancelled") and refuse to overwrite it with
    # completed/failed. It must NOT preserve a chat-aborted *running* task: those
    # are left to finish and complete normally (only their delivery is
    # suppressed), so the predicate keys strictly on the cancelled status.
    assert sub_agent._already_cancelled(SimpleNamespace(status="cancelled")) is True
    assert sub_agent._already_cancelled(SimpleNamespace(status="running")) is False
    assert sub_agent._already_cancelled(SimpleNamespace(status="completed")) is False
    assert sub_agent._already_cancelled(SimpleNamespace(status="failed")) is False
    assert sub_agent._already_cancelled(None) is False


# ── Orphaned-task recovery sweep ──────────────────────────────────────────────


class _QueueResult:
    def __init__(self, *, scalars_all=None, rows=None):
        self._scalars_all = scalars_all
        self._rows = rows

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._scalars_all or []))

    def all(self):
        return list(self._rows or [])


class _QueueDB:
    """Fake session returning pre-seeded results in call order."""

    def __init__(self, results):
        self._q = list(results)
        self.executes = 0
        self.statements: list = []

    async def execute(self, statement=None, *_args, **_kwargs):
        self.executes += 1
        self.statements.append(statement)
        return self._q.pop(0)


def _orphan_task(*, status, created_at):
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        error=None,
        completed_at=None,
        created_at=created_at,
        metadata_json={},  # no linked run -> _fail_orphaned_task returns early
    )


@pytest.mark.asyncio
async def test_recover_orphaned_sub_agent_tasks():
    """A task is failed only when its execution is failure-terminal, or it has no
    execution row and is older than the grace period. Live (pending/running) and
    completed executions are never touched."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    grace = sub_agent._ORPHAN_TASK_GRACE_SECONDS

    dead = _orphan_task(status="running", created_at=now)  # exec dead_letter -> fail
    none_old = _orphan_task(
        status="pending", created_at=now - timedelta(seconds=grace + 60)
    )  # no exec, aged -> fail
    live = _orphan_task(status="running", created_at=now)  # exec running -> skip
    none_young = _orphan_task(status="pending", created_at=now)  # no exec, young -> skip
    done = _orphan_task(status="running", created_at=now)  # exec completed -> skip

    candidates = [dead, none_old, live, none_young, done]
    state_rows = [
        (f"sub_agent_task:{dead.id}", "dead_letter"),
        (f"sub_agent_task:{live.id}", "running"),
        (f"sub_agent_task:{done.id}", "completed"),
        # none_old / none_young have no execution row -> absent from the map
    ]
    db = _QueueDB([
        _QueueResult(scalars_all=candidates),
        _QueueResult(rows=state_rows),
    ])

    with patch.object(sub_agent, "_update_task_delegation", new=AsyncMock()):
        recovered = await sub_agent.recover_orphaned_sub_agent_tasks(db)

    assert recovered == 2
    assert dead.status == "failed" and dead.completed_at is not None
    assert none_old.status == "failed" and none_old.completed_at is not None
    assert live.status == "running"  # engine still owns it
    assert none_young.status == "pending"  # too young to be sure it's orphaned
    assert done.status == "running"  # completed execution is never overwritten
    # Exactly two queries: candidates + execution-state map (no linked-run reads).
    assert db.executes == 2


@pytest.mark.asyncio
async def test_recover_orphaned_sub_agent_tasks_empty_is_noop():
    db = _QueueDB([_QueueResult(scalars_all=[])])
    assert await sub_agent.recover_orphaned_sub_agent_tasks(db) == 0
    assert db.executes == 1


# ── A task its completed execution never settled ──────────────────────────


async def _sweep_completed(tasks, moved_at):
    """Run the sweep on *tasks*, each one's execution ``completed`` and last moved at *moved_at*."""
    exec_ids = {t.id: uuid.uuid4() for t in tasks}
    rows = [(f"sub_agent_task:{t.id}", "completed", exec_ids[t.id], moved_at) for t in tasks]
    db = _QueueDB([_QueueResult(scalars_all=tasks), _QueueResult(rows=rows)])
    delegation = AsyncMock()
    with patch.object(sub_agent, "_update_task_delegation", new=delegation):
        recovered = await sub_agent.recover_orphaned_sub_agent_tasks(db)
    return recovered, exec_ids, delegation


@pytest.mark.asyncio
async def test_a_task_its_completed_execution_never_settled_is_failed_naming_the_execution():
    """The 31 Oct zombies: the execution completed, the task still reads
    ``running`` with no result. Past the quiet threshold the sweep fails it, the
    reason naming the execution and the run."""
    from datetime import UTC, datetime, timedelta

    old = datetime.now(UTC) - timedelta(seconds=sub_agent._completed_quiet_seconds() + 60)
    task = _orphan_task(status="running", created_at=old)
    task.result = None
    # Not a UUID: the linked-run read is skipped, the reason still names it.
    task.metadata_json = {"assignment_run_id": "run-31oct"}

    with patch("app.services.tracing.background_run.record_lost_background_run"):
        recovered, exec_ids, _delegation = await _sweep_completed([task], old)

    assert recovered == 1
    assert task.status == "failed" and task.completed_at is not None
    assert "completed without the task writing an ending" in task.error
    assert str(exec_ids[task.id]) in task.error
    assert "run run-31oct" in task.error


@pytest.mark.asyncio
async def test_a_task_its_completed_execution_never_settled_keeps_its_result_as_completed():
    """The body wrote the answer and lost only the status: it ends ``completed``."""
    from datetime import UTC, datetime, timedelta

    old = datetime.now(UTC) - timedelta(seconds=sub_agent._completed_quiet_seconds() + 60)
    task = _orphan_task(status="running", created_at=old)
    task.result = "The answer."

    recovered, _ids, delegation = await _sweep_completed([task], old)

    assert recovered == 1
    assert task.status == "completed" and task.completed_at is not None
    assert task.result == "The answer." and task.error is None
    assert delegation.await_args.args[2] == "completed"


@pytest.mark.asyncio
async def test_a_task_whose_execution_just_completed_is_left_to_its_body():
    """Within the quiet threshold the body may still be writing the ending."""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    task = _orphan_task(status="running", created_at=now)
    task.result = None

    recovered, _ids, delegation = await _sweep_completed([task], now)

    assert recovered == 0
    assert task.status == "running"
    delegation.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_dead_lettered_execution_reason_names_the_execution_and_the_run():
    from datetime import UTC, datetime

    task = _orphan_task(status="running", created_at=datetime.now(UTC))
    task.metadata_json = {"assignment_run_id": "run-abc"}
    exec_id = uuid.uuid4()
    db = _QueueDB([
        _QueueResult(scalars_all=[task]),
        _QueueResult(rows=[(f"sub_agent_task:{task.id}", "dead_letter", exec_id, None)]),
    ])

    with (
        patch.object(sub_agent, "_update_task_delegation", new=AsyncMock()),
        patch("app.services.tracing.background_run.record_lost_background_run"),
    ):
        assert await sub_agent.recover_orphaned_sub_agent_tasks(db) == 1

    assert task.status == "failed"
    assert f"execution {exec_id} (dead_letter), run run-abc" in task.error


def test_a_listed_running_task_names_its_run_and_its_execution():
    """``list_background_tasks`` (the tool) shows whether a running task still
    has a live execution: a zombie reads as one."""
    live = sub_agent.task_liveness_suffix(
        {
            "assignment_run_id": "1234567890ab",
            "execution_state": "running",
            "last_progress_at": "2026-11-02T22:00:00+00:00",
        }
    )
    assert live == " | run 12345678 | execution running, last progress 2026-11-02T22:00:00+00:00"
    zombie = sub_agent.task_liveness_suffix({"execution_state": "completed"})
    assert "run none" in zombie and "no live execution" in zombie
    assert sub_agent.task_liveness_suffix({"status": "completed"}) == ""


@pytest.mark.asyncio
async def test_execution_liveness_reads_only_open_tasks_and_marks_a_missing_row():
    from datetime import UTC, datetime

    running = _orphan_task(status="running", created_at=None)
    lost = _orphan_task(status="pending", created_at=None)
    done = _orphan_task(status="completed", created_at=None)
    exec_id = uuid.uuid4()
    moved = datetime(2026, 11, 2, 22, 0, tzinfo=UTC)
    db = _QueueDB([_QueueResult(rows=[(f"sub_agent_task:{running.id}", "running", exec_id, moved)])])

    found = await sub_agent._execution_liveness(db, [running, lost, done])

    assert found[str(running.id)] == {
        "execution_state": "running",
        "execution_id": str(exec_id),
        "last_progress_at": moved.isoformat(),
    }
    assert found[str(lost.id)]["execution_state"] == "none"
    assert str(done.id) not in found
    assert db.executes == 1


def _sql(query) -> str:
    from sqlalchemy.dialects import postgresql

    return str(query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_the_no_live_execution_filter_keeps_open_tasks_without_a_live_execution():
    """Operations > Background Tasks filters the zombies: the rule of
    ``task_liveness_suffix``, an open task with no pending / running execution."""
    base = dict(requester_id=str(uuid.uuid4()), is_admin=False, scope="mine", status=None, agent_id=None)
    plain = _sql(sub_agent._background_tasks_query(**base))
    assert "orchestration_executions" not in plain

    sql = _sql(sub_agent._background_tasks_query(**base, liveness="no_live_execution"))
    assert "NOT (EXISTS (SELECT orchestration_executions.id" in sql
    assert "sub_agent_tasks.status IN ('pending', 'running')" in sql
    assert "orchestration_executions.state IN ('pending', 'running')" in sql
    assert "'sub_agent_task:' || CAST(sub_agent_tasks.id AS VARCHAR)" in sql


@pytest.mark.asyncio
async def test_the_list_sorts_by_last_progress_on_the_time_the_updated_cell_shows() -> None:
    db = _QueueDB([_QueueResult(scalars_all=[]), _QueueResult(scalars_all=[])])
    await sub_agent.list_background_tasks(db, requester_id=str(uuid.uuid4()), sort="last_progress")
    await sub_agent.list_background_tasks(db, requester_id=str(uuid.uuid4()))
    by_progress, by_creation = (_sql(q) for q in db.statements)
    assert "LEFT OUTER JOIN orchestration_executions AS task_execution" in by_progress
    assert "ORDER BY coalesce(CASE WHEN (sub_agent_tasks.status IN ('pending', 'running'))" in by_progress
    assert "sub_agent_tasks.created_at) DESC, sub_agent_tasks.id DESC" in by_progress
    assert "orchestration_executions" not in by_creation
    assert "ORDER BY sub_agent_tasks.created_at DESC" in by_creation


@pytest.mark.asyncio
async def test_the_no_live_execution_filter_and_the_last_progress_sort_combine():
    """``?liveness=no_live_execution&sort=last_progress`` crashed the page in
    production: the sort's join on ``orchestration_executions``
    auto-correlated the filter's ``NOT EXISTS`` on the same table, which then
    had no FROM clause left. Each side now reads its own copy of the table."""
    db = _QueueDB([_QueueResult(scalars_all=[])])
    await sub_agent.list_background_tasks(
        db,
        requester_id=str(uuid.uuid4()),
        liveness="no_live_execution",
        sort="last_progress",
    )
    (statement,) = db.statements
    sql = _sql(statement)
    exists = sql[sql.index("NOT (EXISTS (") :]
    exists = exists[: exists.index("))") + 2]
    assert "FROM orchestration_executions \nWHERE" in exists
    assert "sub_agent_task:' || CAST(sub_agent_tasks.id AS VARCHAR)" in exists
    assert "LEFT OUTER JOIN orchestration_executions AS task_execution" in sql
    assert "sub_agent_tasks.status IN ('pending', 'running')" in sql
    assert "sub_agent_tasks.created_at) DESC, sub_agent_tasks.id DESC" in sql
