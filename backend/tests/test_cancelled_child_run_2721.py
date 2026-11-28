"""A cancelled background task's child run ends ``cancelled``, never ``failed`` (#2721).

Cancelling a background task that is no longer needed is a designed pattern.
Its child run used to be written ``failed`` ("Background task cancelled."), so
the member verdict judged a healthy member failing and ``get_fleet_health``
raised ``member_failing``; no run-ended event was published either. Every path
that ends a child run because its task was cancelled now writes ``cancelled``
with an error naming who cancelled, and raises ``run.failed`` with
``reason: "cancelled"`` and ``cancelled: true``.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import assignment_engine as engine
from app.services import platform_events as pe
from app.services import sub_agent
from app.services.run_outcome import OUTCOME_OTHER, RunFact, member_verdict, run_outcome


def _child_run(**extra: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "agent_id": uuid.uuid4(),
        "assignment_id": None,
        "parent_run_id": uuid.uuid4(),
        "trigger_context": {},
        "user_id": uuid.uuid4(),
        "is_company_managed": False,
        "stop_reason": None,
        "status": "running",
        "error": None,
        "completed_at": None,
    }
    base.update(extra)
    return SimpleNamespace(**base)


def _task(run: SimpleNamespace | None, **extra: Any) -> SimpleNamespace:
    meta: dict[str, Any] = {"parent_run_id": str(uuid.uuid4())}
    if run is not None:
        meta["assignment_run_id"] = str(run.id)
    base = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "parent_agent_id": uuid.uuid4(),
        "conversation_id": None,
        "status": "running",
        "error": None,
        "completed_at": None,
        "started_at": datetime.now(UTC),
        "metadata_json": meta,
    }
    base.update(extra)
    return SimpleNamespace(**base)


class _Scalar:
    def __init__(self, row: Any) -> None:
        self._row = row

    def scalar_one_or_none(self) -> Any:
        return self._row


class _Session:
    """Answers the task read, then the child run read, of cancel_sub_agent_task."""

    def __init__(self, task: Any, run: Any) -> None:
        self._rows = [task, run]
        self.sync_session = object()
        self.commits = 0

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def execute(self, *_a: object, **_k: object) -> _Scalar:
        return _Scalar(self._rows.pop(0) if self._rows else None)

    async def commit(self) -> None:
        self.commits += 1


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Captures the after-commit run events instead of publishing them."""
    listeners: list[Any] = []
    published: list[tuple[Any, str, dict[str, Any]]] = []

    def _listen(_target: Any, name: str, fn: Any, **_kw: Any) -> None:
        assert name == "after_commit"
        listeners.append(fn)

    async def _publish(run: Any, name: str, *, reason: str, error: str | None = None) -> None:
        published.append((run, name, {"reason": reason, "error": error}))

    import sqlalchemy.event

    monkeypatch.setattr(sqlalchemy.event, "listen", _listen)
    monkeypatch.setattr(engine, "publish_run_event", _publish)
    return {"listeners": listeners, "published": published}


async def _commit(events: dict[str, Any]) -> None:
    for fn in list(events["listeners"]):
        fn(object())
    events["listeners"].clear()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def _cancel(task: Any, run: Any, **kwargs: Any) -> tuple[dict[str, Any] | None, _Session]:
    from app.services import orchestration_engine

    session = _Session(task, run)
    with (
        patch("app.core.database.async_session", lambda: session),
        patch.object(orchestration_engine, "get_execution_by_dedup_key", new=AsyncMock(return_value=None)),
        patch.object(sub_agent, "_update_task_delegation", new=AsyncMock()),
        patch.object(sub_agent, "publish_task_event", new=AsyncMock()),
    ):
        result = await sub_agent.cancel_sub_agent_task(str(task.id), str(task.user_id), **kwargs)
    return result, session


@pytest.mark.asyncio
async def test_the_agent_cancel_ends_the_child_run_cancelled_naming_the_parent_run(events: dict[str, Any]) -> None:
    run = _child_run()
    task = _task(run)
    parent_run = str(uuid.uuid4())

    result, session = await _cancel(task, run, spawned_by=str(task.parent_agent_id), cancelled_by_run_id=parent_run)

    assert result == {"task_id": str(task.id), "status": "cancelled", "was_running": True}
    assert session.commits == 1
    assert task.status == "cancelled" and run.status == "cancelled"
    assert run.error == task.error == f"Cancelled by parent run {parent_run}."
    assert run.completed_at is not None
    # The ending is published after the commit, as run.failed marked cancelled.
    assert events["published"] == []
    await _commit(events)
    [(snapshot, name, data)] = events["published"]
    assert name == "run.failed"
    assert data == {"reason": "cancelled", "error": f"Cancelled by parent run {parent_run}."}
    assert snapshot.id == run.id and snapshot.parent_run_id == run.parent_run_id


@pytest.mark.asyncio
async def test_the_agent_cancel_without_a_run_names_the_task_parent_run(events: dict[str, Any]) -> None:
    run = _child_run()
    task = _task(run)
    await _cancel(task, run, spawned_by=str(task.parent_agent_id))
    assert run.error == f"Cancelled by parent run {task.metadata_json['parent_run_id']}."


@pytest.mark.parametrize(
    ("is_admin", "owner", "expected"),
    [
        (False, True, sub_agent.CANCELLED_BY_USER_REASON),
        (True, False, sub_agent.CANCELLED_BY_ADMIN_REASON),
    ],
    ids=["user", "administrator"],
)
@pytest.mark.asyncio
async def test_a_person_cancel_ends_the_child_run_cancelled_naming_who(
    events: dict[str, Any], is_admin: bool, owner: bool, expected: str
) -> None:
    run = _child_run()
    task = _task(run)
    requester = task.user_id if owner else uuid.uuid4()
    from app.services import orchestration_engine

    session = _Session(task, run)
    with (
        patch("app.core.database.async_session", lambda: session),
        patch.object(orchestration_engine, "get_execution_by_dedup_key", new=AsyncMock(return_value=None)),
        patch.object(sub_agent, "_update_task_delegation", new=AsyncMock()),
        patch.object(sub_agent, "publish_task_event", new=AsyncMock()),
    ):
        await sub_agent.cancel_sub_agent_task(str(task.id), str(requester), is_admin=is_admin)
    assert run.status == "cancelled" and run.error == expected == task.error


@pytest.mark.asyncio
async def test_an_assignment_child_run_is_cancelled_and_left_to_the_engine_for_events(events: dict[str, Any]) -> None:
    run = _child_run(assignment_id=uuid.uuid4())
    task = _task(run)
    await _cancel(task, run)
    await _commit(events)
    assert run.status == "cancelled"
    assert events["published"] == [] and events["listeners"] == []


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
@pytest.mark.asyncio
async def test_a_child_run_already_ended_is_never_rewritten(events: dict[str, Any], status: str) -> None:
    run = _child_run(status=status, error="as it ended")
    task = _task(run)
    await _cancel(task, run)
    await _commit(events)
    assert run.status == status and run.error == "as it ended"
    assert events["published"] == []


def test_the_cancel_paths_never_write_failed_on_a_run() -> None:
    """Pin: no path that ends a child run because its task was cancelled writes ``failed``."""
    for fn in (
        sub_agent._cancel_child_run,
        sub_agent.cancel_sub_agent_task,
        sub_agent.cancel_sub_agent_tasks_for_scope,
        sub_agent.mark_sub_agent_tasks_cancelled_for_runs,
    ):
        source = inspect.getsource(fn)
        # The task's terminal-status guard may name "failed"; no write of it may.
        assert 'status = "failed"' not in source, fn.__name__
        assert 'status="failed"' not in source, fn.__name__
        assert "update_assignment_run" not in source, fn.__name__
        assert "Background task cancelled." not in source, fn.__name__
    assert 'child_run.status = "cancelled"' in inspect.getsource(sub_agent._cancel_child_run)


@pytest.mark.asyncio
async def test_the_emergency_stop_ends_the_child_runs_cancelled(events: dict[str, Any]) -> None:
    run = _child_run()
    task = _task(run, status="pending")

    class _Db:
        def __init__(self) -> None:
            self.sync_session = object()
            self.flushes = 0
            self.reads = 0

        async def execute(self, *_a: object, **_k: object) -> Any:
            self.reads += 1
            if self.reads == 1:
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [task]))
            if self.reads == 2:
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
            return _Scalar(run)

        async def flush(self) -> None:
            self.flushes += 1

    with patch.object(sub_agent, "_update_task_delegation", new=AsyncMock()):
        tasks, _executions = await sub_agent.cancel_sub_agent_tasks_for_scope(
            cast(Any, _Db()), user_id=None, error="Stopped."
        )
    assert tasks == 1
    assert task.status == "cancelled" and run.status == "cancelled" and run.error == "Stopped."


@pytest.mark.asyncio
async def test_a_parent_run_stop_ends_the_task_child_run_but_leaves_the_stopped_runs_to_the_caller(
    events: dict[str, Any],
) -> None:
    parent = str(uuid.uuid4())
    run = _child_run()
    child_task = _task(run)
    child_task.metadata_json["parent_run_id"] = parent
    stopped_run = _child_run()
    own_task = _task(stopped_run)

    reads: list[Any] = []

    class _Db:
        sync_session = object()

        async def execute(self, stmt: object, *_a: object, **_k: object) -> Any:
            reads.append(stmt)
            if len(reads) == 1:
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [child_task, own_task]))
            return _Scalar(run)

    with patch.object(sub_agent, "_update_task_delegation", new=AsyncMock()):
        count = await sub_agent.mark_sub_agent_tasks_cancelled_for_runs(
            cast(Any, _Db()), [parent, str(stopped_run.id)], reason=sub_agent.RUN_CANCELLED_TASK_REASON
        )
    assert count == 2
    assert run.status == "cancelled" and run.error == sub_agent.RUN_CANCELLED_TASK_REASON
    # The run the caller stops itself is not read or written here.
    assert stopped_run.status == "running"
    assert len(reads) == 2


def test_a_cancelled_run_stays_out_of_the_member_verdict() -> None:
    now = datetime.now(UTC)
    error = sub_agent.cancelled_by_run_reason(uuid.uuid4())
    assert run_outcome("cancelled", error) == OUTCOME_OTHER
    verdict = member_verdict(
        [RunFact(created_at=now, status="cancelled", error=error), RunFact(created_at=now, status="completed")],
        now=now,
    )
    assert verdict.failing is False and verdict.recent_failures == 0
    # A real failure still fails the member.
    failed = member_verdict([RunFact(created_at=now, status="failed", error="boom")], now=now)
    assert failed.failing is True and failed.recent_failures == 1


def test_the_run_failed_payload_says_cancelled() -> None:
    cancelled = pe.run_data(run_id=uuid.uuid4(), agent_id=uuid.uuid4(), reason="cancelled", error="Cancelled by user.")
    failed = pe.run_data(run_id=uuid.uuid4(), agent_id=uuid.uuid4(), reason="failed", error="boom")
    assert cancelled["cancelled"] is True and failed["cancelled"] is False
    assert "cancelled" in pe.PLATFORM_EVENTS["run.failed"]["filter_keys"]


@pytest.mark.asyncio
async def test_a_real_failure_still_raises_run_failed_not_cancelled(
    events: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.in_process_runs.untrack_in_process_run", lambda _rid: None)
    run = _child_run()
    db = MagicMock(execute=AsyncMock(return_value=_Scalar(run)), sync_session=object())
    await sub_agent.update_assignment_run(db, str(run.id), "failed", error="boom")
    await _commit(events)
    assert run.status == "failed"
    [(_snapshot, name, data)] = events["published"]
    assert name == "run.failed" and data == {"reason": "failed", "error": "boom"}
