"""Bulk cancel of runs that have not started: service, endpoint, tool."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services import run_bulk_cancel
from app.services.run_bulk_cancel import (
    CANCELLABLE_RUN_STATUSES,
    BulkCancelFilter,
    BulkCancelFilterError,
    cancel_reason,
    cancel_waiting_runs,
    count_cancellable_runs,
)


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows))


class _Session:
    """Answers each ``execute`` / ``scalar`` with the next scripted value."""

    def __init__(self, answers):
        self._answers = list(answers)
        self.statements = []
        self.flushed = False

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self._answers.pop(0))

    async def scalar(self, statement):
        self.statements.append(statement)
        return self._answers.pop(0)

    async def flush(self):
        self.flushed = True


def _sql(statement) -> str:
    return str(statement.compile(compile_kwargs={"literal_binds": True}))


# --- the filter ---------------------------------------------------------------


def test_a_filter_on_status_alone_is_refused():
    with pytest.raises(BulkCancelFilterError, match="every waiting run"):
        BulkCancelFilter()


@pytest.mark.parametrize("status", ["running", "awaiting_human", "completed", "cancelled"])
def test_a_started_or_finished_status_is_refused(status):
    with pytest.raises(BulkCancelFilterError, match=status):
        BulkCancelFilter(agent_id=uuid.uuid4(), statuses=(status,))


def test_an_empty_status_list_is_refused():
    with pytest.raises(BulkCancelFilterError):
        BulkCancelFilter(agent_id=uuid.uuid4(), statuses=())


def test_the_default_statuses_are_the_waiting_ones():
    assert BulkCancelFilter(assignment_id=uuid.uuid4()).statuses == ("pending", "delayed", "queued")
    assert CANCELLABLE_RUN_STATUSES == ("pending", "delayed", "queued")


def test_the_reason_names_the_actor():
    assert cancel_reason("Ada Lovelace") == "Cancelled by Ada Lovelace"


# --- the service --------------------------------------------------------------


async def test_count_reads_the_filter_and_writes_nothing():
    assignment = uuid.uuid4()
    session = _Session([7])
    count = await count_cancellable_runs(
        session, BulkCancelFilter(assignment_id=assignment, statuses=("pending", "delayed"))
    )
    assert count == 7
    sql = _sql(session.statements[0])
    assert "count(" in sql.lower()
    assert assignment.hex in sql.replace("-", "")
    assert "'pending'" in sql and "'delayed'" in sql and "'queued'" not in sql
    assert not session.flushed


async def test_nothing_matching_cancels_nothing(monkeypatch):
    cancelled_executions = []

    async def _cancel(db, run_ids):
        cancelled_executions.append(list(run_ids))
        return 0

    from app.services import orchestration_engine

    monkeypatch.setattr(orchestration_engine, "cancel_executions_for_runs", _cancel)
    session = _Session([[]])
    assert await cancel_waiting_runs(session, BulkCancelFilter(agent_id=uuid.uuid4()), actor="Ada") == 0
    assert len(session.statements) == 1
    assert cancelled_executions == []


async def test_waiting_runs_end_cancelled_with_the_reason_and_their_executions_go(monkeypatch):
    first, second, raced = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    cancelled_executions = []

    async def _cancel(db, run_ids):
        cancelled_executions.append(list(run_ids))
        return len(cancelled_executions[-1])

    from app.services import orchestration_engine

    monkeypatch.setattr(orchestration_engine, "cancel_executions_for_runs", _cancel)
    before = datetime(2026, 11, 3, 8, 0)  # naive: read as UTC
    # The select finds three; the reconciler promoted ``raced`` to running in
    # between, so the guarded UPDATE returns two.
    session = _Session([[first, second, raced], [first, second]])
    count = await cancel_waiting_runs(
        session,
        BulkCancelFilter(assignment_id=uuid.uuid4(), statuses=("pending", "delayed"), created_before=before),
        actor="Ada Lovelace",
    )
    assert count == 2
    assert cancelled_executions == [[first, second]]
    assert session.flushed
    select_sql, update_sql = (_sql(s) for s in session.statements)
    assert "created_at <" in select_sql
    assert "2026-11-03 08:00:00" in select_sql
    assert update_sql.lower().startswith("update assignment_runs")
    assert "'cancelled'" in update_sql
    assert "Cancelled by Ada Lovelace" in update_sql
    # The UPDATE re-checks the status: a run that started is never overwritten.
    assert "status IN ('pending', 'delayed')" in update_sql


async def test_large_backlogs_are_written_in_bounded_batches(monkeypatch):
    from app.services import orchestration_engine

    async def _cancel(db, run_ids):
        return 0

    monkeypatch.setattr(orchestration_engine, "cancel_executions_for_runs", _cancel)
    monkeypatch.setattr(run_bulk_cancel, "_ID_CHUNK", 2)
    ids = [uuid.uuid4() for _ in range(5)]
    session = _Session([ids, ids[0:2], ids[2:4], ids[4:5]])
    assert await cancel_waiting_runs(session, BulkCancelFilter(agent_id=uuid.uuid4()), actor="Ada") == 5
    assert len(session.statements) == 4


# --- the endpoint -------------------------------------------------------------


def test_the_endpoint_is_admin_only_and_registered():
    import inspect

    from fastapi.routing import APIRoute

    from app.api.deps import require_admin
    from app.api.v1 import assignments

    route = next(
        r
        for r in assignments.router.routes
        if isinstance(r, APIRoute) and r.path.endswith("/runs/bulk-cancel")
    )
    assert route.methods == {"POST"}
    signature = inspect.signature(assignments.bulk_cancel_runs)
    assert signature.parameters["user"].default.dependency is require_admin


def test_the_request_rejects_a_started_status():
    from pydantic import ValidationError

    from app.api.v1.schemas.assignment import BulkCancelRunsRequest

    assert BulkCancelRunsRequest(assignment_id=str(uuid.uuid4())).status == ["pending", "delayed", "queued"]
    with pytest.raises(ValidationError):
        BulkCancelRunsRequest(assignment_id=str(uuid.uuid4()), status=["running"])


async def test_the_endpoint_refuses_a_filter_without_scope():
    from fastapi import HTTPException

    from app.api.v1 import assignments
    from app.api.v1.schemas.assignment import BulkCancelRunsRequest

    with pytest.raises(HTTPException) as raised:
        await assignments.bulk_cancel_runs(
            BulkCancelRunsRequest(), user=SimpleNamespace(full_name="A", email="a@x"), db=_Session([])
        )
    assert raised.value.status_code == 422


async def test_the_endpoint_dry_run_counts_and_cancels_nothing(monkeypatch):
    from app.api.v1 import assignments
    from app.api.v1.schemas.assignment import BulkCancelRunsRequest

    async def _must_not_cancel(*args, **kwargs):
        raise AssertionError("dry_run cancelled runs")

    monkeypatch.setattr(run_bulk_cancel, "cancel_waiting_runs", _must_not_cancel)
    response = await assignments.bulk_cancel_runs(
        BulkCancelRunsRequest(assignment_id=str(uuid.uuid4()), status=["pending", "delayed"], dry_run=True),
        user=SimpleNamespace(full_name="A", email="a@x"),
        db=_Session([12]),
    )
    assert (response.count, response.dry_run) == (12, True)


async def test_the_endpoint_cancels_audits_and_commits(monkeypatch):
    from app.api.v1 import assignments
    from app.api.v1.schemas.assignment import BulkCancelRunsRequest

    seen, audits = {}, []

    async def _cancel(db, filters, *, actor):
        seen.update(filters=filters, actor=actor)
        return 3

    async def _audit(db, **kwargs):
        audits.append(kwargs)

    class _Db:
        committed = False

        async def commit(self):
            self.committed = True

    monkeypatch.setattr(run_bulk_cancel, "cancel_waiting_runs", _cancel)
    monkeypatch.setattr(assignments, "log_audit", _audit)
    assignment = uuid.uuid4()
    db = _Db()
    response = await assignments.bulk_cancel_runs(
        BulkCancelRunsRequest(assignment_id=str(assignment), status=["pending", "delayed"]),
        user=SimpleNamespace(full_name="Ada Lovelace", email="ada@example.com"),
        db=db,
    )
    assert (response.count, response.dry_run) == (3, False)
    assert seen["actor"] == "Ada Lovelace"
    assert seen["filters"].assignment_id == assignment
    assert seen["filters"].statuses == ("pending", "delayed")
    assert audits[0]["action"] == "bulk_cancel_runs"
    assert audits[0]["details"]["cancelled_count"] == 3
    assert db.committed


# --- the agent tool -----------------------------------------------------------


class _ToolSession:
    def __init__(self):
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


@pytest.fixture
def tool(monkeypatch):
    from app.core import database
    from app.services import audit
    from app.services.builtin_tools import agent_builder
    from app.tools.base import ToolCall

    state = SimpleNamespace(caller=None, session=None, cancelled=None, audits=[])

    async def _caller(_db, uid):
        return state.caller

    async def _cancel(db, filters, *, actor):
        state.cancelled = (filters, actor)
        return 4

    async def _count(db, filters):
        return 9

    async def _audit(db, **kwargs):
        state.audits.append(kwargs)

    def _session():
        state.session = _ToolSession()
        return state.session

    monkeypatch.setattr(agent_builder, "_load_caller_user_for_builder", _caller)
    monkeypatch.setattr(database, "async_session", _session)
    monkeypatch.setattr(run_bulk_cancel, "cancel_waiting_runs", _cancel)
    monkeypatch.setattr(run_bulk_cancel, "count_cancellable_runs", _count)
    monkeypatch.setattr(audit, "log_audit", _audit)
    _defs, executor = agent_builder.build_agent_builder_tool(None, str(uuid.uuid4()))

    async def run(**arguments):
        result = await executor.execute_tool(ToolCall(id="1", name="cancel_runs", arguments=arguments))
        return result.content

    state.run = run
    return state


def _admin():
    return SimpleNamespace(
        id=uuid.uuid4(), is_admin=True, role="admin", full_name="Ada Lovelace", email="ada@example.com"
    )


def test_the_tool_is_offered_in_the_agent_builder_suite():
    from app.services.builtin_tools import build_agent_builder_tool

    definitions = {d.name: d for d in build_agent_builder_tool(None, "")[0]}
    schema = definitions["cancel_runs"].input_schema
    assert set(schema["properties"]) == {"agent_id", "assignment_id", "status", "created_before", "dry_run"}
    assert "ADMIN ONLY" in definitions["cancel_runs"].description


def test_the_tool_is_never_a_read_for_an_api_key_or_the_hitl_whitelist():
    from app.api.v1.mcp_xtm import XTM_MCP_READ_TOOLS
    from app.services.hitl_whitelist_default import SHIPPED_DEFAULT_WHITELIST

    assert "cancel_runs" not in XTM_MCP_READ_TOOLS
    assert "cancel_runs" not in SHIPPED_DEFAULT_WHITELIST


async def test_a_non_admin_is_refused(tool):
    tool.caller = SimpleNamespace(id=uuid.uuid4(), is_admin=False, role="user", full_name="U", email="u@x")
    content = await tool.run(assignment_id=str(uuid.uuid4()))
    assert "Only admins" in content
    assert tool.cancelled is None


async def test_the_tool_needs_a_scope(tool):
    tool.caller = _admin()
    content = await tool.run(status="queued")
    assert "every waiting run" in content
    assert tool.cancelled is None


async def test_the_tool_refuses_a_started_status(tool):
    tool.caller = _admin()
    content = await tool.run(agent_id=str(uuid.uuid4()), status="running")
    assert "running" in content and tool.cancelled is None


async def test_the_tool_rejects_a_bad_id_and_a_bad_date(tool):
    tool.caller = _admin()
    assert "agent_id must be a valid UUID" in await tool.run(agent_id="nope")
    assert "ISO-8601" in await tool.run(agent_id=str(uuid.uuid4()), created_before="yesterday")


async def test_dry_run_counts(tool):
    tool.caller = _admin()
    content = await tool.run(assignment_id=str(uuid.uuid4()), dry_run=True)
    assert content.startswith("9 run(s)")
    assert tool.cancelled is None


async def test_the_tool_cancels_audits_and_commits(tool):
    tool.caller = _admin()
    assignment = uuid.uuid4()
    content = await tool.run(
        assignment_id=str(assignment), status="pending, delayed", created_before="2026-11-03T08:00:00+00:00"
    )
    filters, actor = tool.cancelled
    assert filters.assignment_id == assignment
    assert filters.statuses == ("pending", "delayed")
    assert filters.created_before == datetime(2026, 11, 3, 8, 0, tzinfo=UTC)
    assert actor == "Ada Lovelace"
    assert "Cancelled 4 run(s)" in content and "Cancelled by Ada Lovelace" in content
    assert tool.audits[0]["action"] == "bulk_cancel_runs"
    assert tool.session.committed
