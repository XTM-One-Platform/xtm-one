"""Assignment run retention (Operations > Executions).

Finished runs past the window are deleted with their cascade, a run still
working, waiting for a person, holding a claim or a lease, running a background
task or parenting a kept run is never selected, 0 keeps every run, the window
is the administrator's (else 90 days), and a backlog drains across ticks.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

import app.services.autonomous as autonomous
import app.services.coordination_retention as cr
import app.services.run_retention as rr
from app.core.config import Settings
from app.services.platform_insights.overview import _coordination_retention


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect())).lower()


class _Result:
    def __init__(self, rows=None, rowcount=0):
        self._rows = rows or []
        self.rowcount = rowcount

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, results):
        self._results = list(results)
        self.statements: list = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        self.statements.append(statement)
        return self._results.pop(0)

    async def commit(self):
        self.committed = True


# -- the window ---------------------------------------------------------------------


def test_the_environment_default_keeps_runs_90_days():
    assert Settings.model_fields["assignment_run_retention_days"].default == 90


def test_the_admin_window_wins_and_0_keeps_forever(monkeypatch):
    monkeypatch.setattr(cr.settings, "assignment_run_retention_days", 90)
    assert cr.coordination_retention_days(None)["assignment_run_days"] == 90
    assert cr.coordination_retention_days({"assignment_run_days": 30})["assignment_run_days"] == 30
    assert cr.coordination_retention_days({"assignment_run_days": 0})["assignment_run_days"] == 0
    assert cr.coordination_retention_days({"assignment_run_days": 3651})["assignment_run_days"] == 90


def test_the_overview_reports_run_retention_days(monkeypatch):
    monkeypatch.setattr(cr.settings, "assignment_run_retention_days", 90)
    assert _coordination_retention(SimpleNamespace(coordination_retention=None))["run_retention_days"] == 90
    ps = SimpleNamespace(coordination_retention={"assignment_run_days": 45})
    assert _coordination_retention(ps)["run_retention_days"] == 45


def test_a_settings_body_without_the_run_window_stays_valid():
    from app.api.v1.settings import CoordinationRetention

    body = CoordinationRetention(
        work_item_resolved_days=90,
        work_item_abandoned_days=14,
        shared_state_resolved_days=90,
        shared_state_archive_days=14,
        workset_resolved_days=7,
    )
    assert body.assignment_run_days is None
    with pytest.raises(ValueError):
        CoordinationRetention(**{**body.model_dump(), "assignment_run_days": 3651})


@pytest.mark.asyncio
async def test_a_save_without_the_run_window_keeps_the_stored_one(monkeypatch):
    import app.api.v1.settings as settings_api

    ps = SimpleNamespace(
        trace_retention_days=90,
        trace_storage_enabled=True,
        trace_store_payloads=True,
        audit_log_retention_days=90,
        coordination_retention={"assignment_run_days": 30},
    )
    monkeypatch.setattr("app.api.v1.platform_settings_helpers.get_platform_settings", AsyncMock(return_value=ps))
    monkeypatch.setattr(settings_api, "log_audit", AsyncMock())
    monkeypatch.setattr("app.services.trace_switches.publish_trace_switches", lambda *_a, **_k: None)
    monkeypatch.setattr(settings_api, "_build_observability_config_response", AsyncMock(return_value="ok"))
    db = SimpleNamespace(flush=AsyncMock(), commit=AsyncMock())
    body = settings_api.ObservabilityConfigUpdate(
        trace_storage_enabled=True,
        trace_retention_days=90,
        trace_store_payloads=True,
        coordination_retention=settings_api.CoordinationRetention(
            work_item_resolved_days=60,
            work_item_abandoned_days=14,
            shared_state_resolved_days=90,
            shared_state_archive_days=14,
            workset_resolved_days=7,
        ),
    )
    await settings_api.update_observability_config(body, request=None, user=SimpleNamespace(), db=db)
    assert ps.coordination_retention["assignment_run_days"] == 30
    assert ps.coordination_retention["work_item_resolved_days"] == 60


# -- the purge ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_0_window_deletes_nothing_and_opens_no_session(monkeypatch):
    def _no_session():
        raise AssertionError("a disabled retention opened a session")

    monkeypatch.setattr("app.core.database.async_session", _no_session)
    assert await rr.purge_expired_runs(older_than_days=0) == 0


@pytest.mark.asyncio
async def test_the_selection_keeps_live_held_and_parent_runs(monkeypatch):
    session = _Session([_Result(rows=[])])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    assert await rr.purge_expired_runs(older_than_days=90) == 0
    sql = _sql(session.statements[0])
    # Finished runs only: never pending, delayed, queued, running or awaiting_human.
    assert "assignment_runs.status in" in sql
    assert "coalesce(assignment_runs.completed_at, assignment_runs.created_at) <" in sql
    # A non-terminal work item claim, a shared-state lease, a live background task, a kept child.
    assert "work_items.claimed_by_run_id = assignment_runs.id" in sql
    assert "work_items.status not in" in sql
    assert "shared_states.held_by_run_id = assignment_runs.id" in sql
    assert "sub_agent_tasks.status in" in sql
    assert "parent_run_id = assignment_runs.id" in sql
    assert sql.count("not (exists") == 4
    assert "order by assignment_runs.created_at asc" in sql
    assert not session.committed


def test_the_terminal_statuses_exclude_every_live_one():
    from app.services.assignment_engine import ACTIVE_RUN_STATUSES, QUEUED_RUN_STATUS, TERMINAL_RUN_STATUSES

    assert not set(TERMINAL_RUN_STATUSES) & {*ACTIVE_RUN_STATUSES, QUEUED_RUN_STATUS}
    assert "awaiting_human" not in TERMINAL_RUN_STATUSES


@pytest.mark.asyncio
async def test_an_expired_run_is_deleted_with_its_cascade_and_rechecked(monkeypatch):
    run_ids = [uuid.uuid4(), uuid.uuid4()]
    session = _Session([_Result(rows=run_ids), _Result(rowcount=2)])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    lock = AsyncMock()
    files = AsyncMock(return_value=2)
    executions = AsyncMock(return_value=0)
    tasks = AsyncMock(return_value=0)
    monkeypatch.setattr("app.a2a.tasks.lock_tasks_before_deletion", lock)
    monkeypatch.setattr("app.services.file_cleanup.delete_file_attachments", files)
    monkeypatch.setattr("app.services.orchestration_engine.cancel_executions_for_runs", executions)
    monkeypatch.setattr("app.services.sub_agent.mark_sub_agent_tasks_cancelled_for_runs", tasks)

    assert await rr.purge_expired_runs(older_than_days=90, limit=10) == 2
    assert lock.await_args.kwargs == {"run_ids": run_ids}
    assert files.await_args.kwargs == {"assignment_run_id": run_ids}
    assert executions.await_args.args[1] == run_ids
    assert tasks.await_args.args[1] == run_ids
    delete_sql = _sql(session.statements[1])
    assert delete_sql.startswith("delete from assignment_runs")
    # The DELETE re-applies the window and the keep rules (TOCTOU-safe).
    assert "coalesce(assignment_runs.completed_at, assignment_runs.created_at) <" in delete_sql
    assert "shared_states.held_by_run_id" in delete_sql
    assert session.committed


@pytest.mark.asyncio
async def test_the_cascade_with_no_run_writes_nothing(monkeypatch):
    lock = AsyncMock()
    monkeypatch.setattr("app.a2a.tasks.lock_tasks_before_deletion", lock)
    db = SimpleNamespace(execute=AsyncMock())
    assert await rr.delete_runs_cascade(db, []) == 0
    lock.assert_not_awaited()
    db.execute.assert_not_awaited()


# -- the hourly drain -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_backlog_drains_across_ticks_and_each_pass_is_audited_once(monkeypatch):
    windows = {**cr.coordination_retention_days(None), "assignment_run_days": 90}
    monkeypatch.setattr(cr, "effective_coordination_retention", AsyncMock(return_value=windows))
    full = rr.RUN_RETENTION_BATCH
    # A backlog of 10,000 past the window: five full passes, then the rest, then none.
    purge = AsyncMock(side_effect=[full, full, full, full, full, 0])
    audit = AsyncMock()
    monkeypatch.setattr(rr, "purge_expired_runs", purge)
    monkeypatch.setattr(rr, "audit_run_retention_pass", audit)

    drained = 0
    passes = 0
    more = True
    while more:
        deleted, more = await autonomous._drain_run_retention()
        drained += deleted
        passes += 1
    assert drained == 10_000 and passes == 6
    assert purge.await_args.kwargs == {"older_than_days": 90}
    assert audit.await_count == 5
    assert audit.await_args.args == (full, 90)


@pytest.mark.asyncio
async def test_a_0_window_drains_nothing(monkeypatch):
    windows = {**cr.coordination_retention_days(None), "assignment_run_days": 0}
    monkeypatch.setattr(cr, "effective_coordination_retention", AsyncMock(return_value=windows))
    audit = AsyncMock()
    monkeypatch.setattr(rr, "audit_run_retention_pass", audit)
    assert await autonomous._drain_run_retention() == (0, False)
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_audit_entry_names_the_count_and_the_window(monkeypatch):
    admin = uuid.uuid4()

    class _AdminResult:
        def scalar_one_or_none(self):
            return admin

    session = SimpleNamespace(execute=AsyncMock(return_value=_AdminResult()), commit=AsyncMock())

    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("app.core.database.async_session", lambda: _Ctx())
    log = AsyncMock()
    monkeypatch.setattr("app.services.audit.log_system_audit", log)
    await rr.audit_run_retention_pass(1500, 90)
    kwargs = log.await_args.kwargs
    assert kwargs["anchor_user_id"] == admin
    assert kwargs["actor"] == rr.RUN_RETENTION_AUDIT_ACTOR
    assert kwargs["details"] == {"deleted": 1500, "retention_days": 90}
    session.commit.assert_awaited_once()

    log.reset_mock()
    await rr.audit_run_retention_pass(0, 90)
    log.assert_not_awaited()
