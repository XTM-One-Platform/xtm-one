"""``delete_assignment(after_run=true)``: the deferred retirement of an assignment."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.services import assignment_retire
from app.services.assignment_engine import ACTIVE_RUN_STATUSES, QUEUED_RUN_STATUS
from app.services.assignment_pause import PAUSE_REASONS, SYSTEM_REASONS
from app.services.assignment_retire import (
    RETIRE_AFTER_RUN_REASON,
    detach_runs_clause,
    mark_retire_after_run,
    retiring_clause,
)


def test_retire_reason_is_a_known_human_pause_reason():
    assert RETIRE_AFTER_RUN_REASON in PAUSE_REASONS
    assert RETIRE_AFTER_RUN_REASON not in SYSTEM_REASONS


def test_mark_retire_after_run_pauses_and_names_the_runs():
    assignment = SimpleNamespace(id=uuid.uuid4(), name="One-off", enabled=True)
    runs = [uuid.uuid4()]
    actor = uuid.uuid4()

    details = mark_retire_after_run(
        assignment, actor_user_id=actor, actor_label="Sam", run_ids=runs
    )

    assert assignment.enabled is False
    assert assignment.paused_reason == RETIRE_AFTER_RUN_REASON
    assert assignment.paused_by_user_id == actor
    assert str(runs[0]) in details
    assert assignment.paused_details == details


def test_retiring_clause_waits_for_active_and_queued_runs():
    sql = str(
        retiring_clause().compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert f"'{RETIRE_AFTER_RUN_REASON}'" in sql
    assert "NOT (EXISTS" in sql
    for status in (*ACTIVE_RUN_STATUSES, QUEUED_RUN_STATUS):
        assert f"'{status}'" in sql


def test_detach_runs_clause_nulls_the_assignment_and_returns_the_runs():
    assignment_id = uuid.uuid4()
    sql = str(
        detach_runs_clause(assignment_id).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert sql.startswith("UPDATE assignment_runs SET assignment_id=NULL")
    assert "WHERE assignment_runs.assignment_id = " in sql
    assert "RETURNING assignment_runs.id" in sql


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _Session:
    """First execute: the candidate ids; then one locked re-read per id.

    A re-read that finds the row is followed by the detach of its runs, which
    answers *detached* (the run ids kept).
    """

    def __init__(self, ids, rereads, detached=()):
        self._results = [_Rows(ids)]
        for row in rereads:
            self._results.append(_Rows([row] if row else []))
            if row:
                self._results.append(_Rows(list(detached)))
        self.statements = []
        self.deleted = []
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, statement):
        self.statements.append(statement)
        return self._results.pop(0)

    async def delete(self, obj):
        # The runs are detached before the row goes, so the cascade keeps them.
        assert any(
            getattr(s, "is_update", False) for s in self.statements
        ), "the runs must be detached before the assignment is deleted"
        self.deleted.append(obj)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_retire_deletes_the_rows_still_retiring_and_skips_the_others(monkeypatch):
    from app.core import database
    from app.services import audit

    kept_id, gone_id = uuid.uuid4(), uuid.uuid4()
    gone = SimpleNamespace(
        id=gone_id,
        name="One-off",
        user_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        trigger_type="schedule",
        paused_by_label="Sam",
    )
    # ``kept`` was resumed (or got a new run) between the read and the lock.
    run_id = uuid.uuid4()
    session = _Session([kept_id, gone_id], [None, gone], detached=[run_id])
    audits = []

    async def _audit(_db, **kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(database, "async_session", lambda: session)
    monkeypatch.setattr(audit, "log_system_audit", _audit)

    deleted = await assignment_retire.retire_assignments_after_runs()

    assert deleted == 1
    assert session.deleted == [gone]
    assert session.commits == 1
    assert session.rollbacks == 1
    assert audits[0]["action"] == "delete"
    assert audits[0]["entity_id"] == gone_id
    assert audits[0]["details"]["reason"] == RETIRE_AFTER_RUN_REASON
    # The run that retired it outlives it: detached, then named on the audit row.
    detach = next(s for s in session.statements if getattr(s, "is_update", False))
    assert detach.compile().params["assignment_id"] is None
    assert audits[0]["details"]["runs_kept"] == 1
    assert audits[0]["details"]["run_ids"] == [str(run_id)]
