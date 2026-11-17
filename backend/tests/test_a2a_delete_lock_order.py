"""Every deletion that reaches an A2A task locks it before writing a run.

Deleting a run nulls its task's ``run_id`` and deleting an agent or a user
deletes their tasks, both inside the DELETE of the parent row - the run
first, the task second. The cancel, the claim, the terminal write and the
reaper take the task row first, so a deletion racing one of them on the same
live task deadlocked. Which transaction waits for which is asserted against
PostgreSQL in ``test_a2a_delete_lock_order_postgres.py``; these pin the order
of the statements each deletion route issues.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

import app.api.v1.helpers as api_helpers
import app.core.database as database
from app.a2a import tasks as task_svc
from app.api.v1 import agents as agents_module
from app.api.v1 import assignments as assignments_api
from app.api.v1 import users as users_module
from app.api.v1.schemas.assignment import BulkDeleteRunsRequest
from app.services import agent_deletion

_TASK_LOCK = "lock a2a_tasks"


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def _describe(stmt) -> str:
    sql = _sql(stmt)
    words = sql.split()
    if sql.startswith("SELECT a2a_tasks.id") and "FOR NO KEY UPDATE" in sql:
        return _TASK_LOCK
    if "pg_advisory_xact_lock" in sql:
        return "serialize"
    head = words[0].upper() if words else ""
    if head == "DELETE":
        return f"delete {words[2]}"
    if head == "UPDATE":
        return f"update {words[1]}"
    if head == "INSERT":
        return f"insert {words[2]}"
    return "read"


class _Scalars:
    def __init__(self, value):
        self._value = value

    def all(self):
        return list(self._value) if isinstance(self._value, list) else []

    def __iter__(self):
        return iter(self.all())


class _Result:
    def __init__(self, value):
        self._value = value
        self.rowcount = 0

    def scalar_one_or_none(self):
        return None if isinstance(self._value, list) else self._value

    def scalars(self):
        return _Scalars(self._value)

    def all(self):
        return list(self._value) if isinstance(self._value, list) else []


class _Recorder:
    """A session answering through *respond* and writing down what it was asked."""

    def __init__(self, respond=lambda _sql: None, got=None):
        self.log: list[str] = []
        self._respond = respond
        self._got = got
        self.info: dict = {}
        self.is_active = True

    async def execute(self, stmt, _params=None):
        self.log.append(_describe(stmt))
        return _Result(self._respond(_sql(stmt)))

    async def get(self, _model, _pk, **_kw):
        self.log.append("read")
        return self._got

    async def delete(self, obj):
        self.log.append(f"delete {type(obj).__name__}")

    def add(self, _obj):
        self.log.append("add")

    async def flush(self):
        self.log.append("flush")

    async def commit(self):
        self.log.append("commit")

    async def rollback(self):
        self.log.append("rollback")


class _Ctx:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_exc):
        return False


def _first_write(log: list[str]) -> int:
    return next(i for i, entry in enumerate(log) if entry.split()[0] in ("delete", "update", "insert"))


def _assert_tasks_locked_first(log: list[str]) -> None:
    assert _TASK_LOCK in log, f"the A2A tasks were never locked: {log}"
    assert log.index(_TASK_LOCK) < _first_write(log), f"a write came before the task lock: {log}"


def _assert_locked_again_right_before(log: list[str], write: str) -> None:
    """A deletion whose rows come from a filter or a cascade takes the task lock
    again in the statement before that write, so a task created since the first
    lock is held first too."""
    at = log.index(write)
    assert log[at - 1] == _TASK_LOCK, f"{write!r} is not preceded by the task lock: {log}"


def _stub_run_cleanup(monkeypatch, db: _Recorder) -> None:
    """The cleanup every run deletion does first, recorded as the writes it is."""

    async def _files(*_a, **_k):
        db.log.append("delete file_attachments")
        return 0

    async def _executions(*_a, **_k):
        db.log.append("update orchestration_executions")
        return 0

    async def _sub_agent_tasks(*_a, **_k):
        db.log.append("update sub_agent_tasks")
        return 0

    monkeypatch.setattr("app.services.file_cleanup.delete_file_attachments", _files)
    monkeypatch.setattr("app.services.orchestration_engine.cancel_executions_for_runs", _executions)
    monkeypatch.setattr("app.services.sub_agent.mark_sub_agent_tasks_cancelled_for_runs", _sub_agent_tasks)


async def _noop(*_a, **_k):
    return None


class TestTheLock:
    async def test_it_takes_the_tasks_in_key_order_with_the_stamp_s_own_mode(self):
        db = _Recorder()
        await task_svc.lock_tasks_before_deletion(db, run_ids=[uuid.uuid4()])
        assert db.log == [_TASK_LOCK]

        issued: list[str] = []

        class _Keep(_Recorder):
            async def execute(self, stmt, _params=None):
                issued.append(_sql(stmt))
                return _Result(None)

        await task_svc.lock_tasks_before_deletion(_Keep(), run_ids=[uuid.uuid4()])
        assert issued[0].endswith("ORDER BY a2a_tasks.id FOR NO KEY UPDATE"), issued[0]

    async def test_an_agent_deletion_covers_its_runs_exposures_and_contexts(self):
        issued: list[str] = []

        class _Keep(_Recorder):
            async def execute(self, stmt, _params=None):
                issued.append(_sql(stmt))
                return _Result(None)

        await task_svc.lock_tasks_before_deletion(_Keep(), agent_ids=[uuid.uuid4()])
        sql = issued[0]
        assert "assignment_runs.agent_id" in sql
        assert "a2a_exposures.agent_id" in sql
        assert "a2a_contexts.responsible_agent_id" in sql

    async def test_a_user_deletion_covers_its_runs_agents_and_run_as_exposures(self):
        issued: list[str] = []

        class _Keep(_Recorder):
            async def execute(self, stmt, _params=None):
                issued.append(_sql(stmt))
                return _Result(None)

        await task_svc.lock_tasks_before_deletion(_Keep(), user_id=uuid.uuid4())
        sql = issued[0]
        assert "assignment_runs.user_id" in sql
        assert "a2a_exposures.run_as_user_id" in sql
        assert "agents.user_id" in sql
        assert "a2a_contexts.responsible_agent_id" in sql

    async def test_nothing_to_lock_issues_nothing(self):
        db = _Recorder()
        await task_svc.lock_tasks_before_deletion(db, run_ids=[])
        await task_svc.lock_tasks_before_deletion(db)
        assert db.log == []


class TestEachDeletionRoute:
    async def test_deleting_one_run(self, monkeypatch):
        run = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), is_company_managed=False, assignment_id=None)

        def _respond(sql):
            if "WITH RECURSIVE" in sql:
                return [(run.id,)]
            if sql.startswith("SELECT assignment_runs"):
                return run
            return None

        db = _Recorder(_respond)
        _stub_run_cleanup(monkeypatch, db)
        await assignments_api.delete_run(run_id=str(run.id), user=SimpleNamespace(id=run.user_id, role="user"), db=db)
        _assert_tasks_locked_first(db.log)
        assert db.log.index(_TASK_LOCK) < db.log.index("delete SimpleNamespace")

    async def test_deleting_runs_in_bulk(self, monkeypatch):
        ids = [uuid.uuid4(), uuid.uuid4()]
        db = _Recorder(lambda sql: [SimpleNamespace(id=i) for i in ids] if sql.startswith("SELECT") else None)
        _stub_run_cleanup(monkeypatch, db)
        await assignments_api.bulk_delete_runs(
            BulkDeleteRunsRequest(run_ids=[str(i) for i in ids]),
            user=SimpleNamespace(id=uuid.uuid4(), role="admin"),
            db=db,
        )
        _assert_tasks_locked_first(db.log)
        assert db.log.index(_TASK_LOCK) < db.log.index("delete assignment_runs")

    async def test_purging_every_visible_run(self, monkeypatch):
        db = _Recorder(lambda sql: [(uuid.uuid4(),)] if sql.startswith("SELECT assignment_runs.id") else None)
        _stub_run_cleanup(monkeypatch, db)
        await assignments_api.purge_all_runs(user=SimpleNamespace(id=uuid.uuid4(), role="admin"), db=db)
        _assert_tasks_locked_first(db.log)
        _assert_locked_again_right_before(db.log, "delete assignment_runs")

    async def test_deleting_an_agent(self, monkeypatch):
        agent = SimpleNamespace(
            id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            name="Exposed",
            slug="exposed",
            icon="Bot",
            knowledge_base_ids=[],
            is_company_managed=False,
        )
        db = _Recorder(
            lambda sql: [uuid.uuid4()] if sql.startswith("SELECT assignment_runs.id") else None,
            got=agent,
        )
        _stub_run_cleanup(monkeypatch, db)

        async def _no_groups(*_a, **_k):
            return []

        async def _agent(*_a, **_k):
            return agent

        monkeypatch.setattr(agents_module, "get_user_group_ids", _no_groups)
        monkeypatch.setattr(agents_module, "get_user_agent", _agent)
        monkeypatch.setattr(agents_module, "assert_writable_agent", lambda *_a, **_k: None)
        monkeypatch.setattr(agent_deletion, "log_audit", _noop)
        monkeypatch.setattr(api_helpers, "purge_agent_references", _noop)
        monkeypatch.setattr(database, "async_session", lambda: _Ctx(db))
        monkeypatch.setattr("app.services.agent_memory.clear_deleted_agent_memory", _noop)

        await agents_module.delete_agent(
            agent_id=str(agent.id),
            request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={}),
            user=SimpleNamespace(id=uuid.uuid4(), is_admin=True),
            db=None,
            _ai_gate=None,
        )
        assert db.log.index("serialize") < db.log.index(_TASK_LOCK)
        _assert_tasks_locked_first(db.log)
        _assert_locked_again_right_before(db.log, "delete assignment_runs")
        _assert_locked_again_right_before(db.log, "delete SimpleNamespace")

    async def test_an_agent_run_batch_locks_the_tasks_of_its_own_runs(self, monkeypatch):
        """A batch of an agent's runs locks their tasks, never every run's.

        The agent-wide lock reaches the tasks of the whole remaining history, so
        each batch would grow with it; the agent's exposures and contexts are
        the agent transaction's, after the batches. The order stays task first,
        then the agent's row, then the runs before their executions, as a single
        run's deletion takes them.
        """
        statements: list[str] = []

        class _Sql(_Recorder):
            async def execute(self, stmt, _params=None):
                sql = _sql(stmt)
                statements.append(sql)
                result = await super().execute(stmt, _params)
                if sql.startswith("SELECT assignment_runs.id") and "FOR UPDATE" in sql:
                    self.log[-1] = "lock assignment_runs"
                return result

        db = _Sql(lambda sql: [uuid.uuid4(), uuid.uuid4()] if sql.startswith("SELECT assignment_runs.id") else None)
        _stub_run_cleanup(monkeypatch, db)

        assert (await agent_deletion._delete_run_batch(db, uuid.uuid4())).selected == 2
        _assert_tasks_locked_first(db.log)
        _assert_locked_again_right_before(db.log, "delete assignment_runs")
        locks = [
            i for i, sql in enumerate(statements) if sql.startswith("SELECT a2a_tasks.id") and "FOR NO KEY UPDATE" in sql
        ]
        assert all("agent_id" not in statements[i] and "a2a_exposures" not in statements[i] for i in locks), statements
        agent_row = next(i for i, sql in enumerate(statements) if "FROM agents" in sql and "FOR UPDATE" in sql)
        assert locks[0] < agent_row
        assert db.log.index("lock assignment_runs") < db.log.index("update orchestration_executions"), db.log

    async def test_deleting_a_user(self, monkeypatch):
        target = SimpleNamespace(id=uuid.uuid4(), email="leaver@example.com", full_name="Leaver")
        db = _Recorder(lambda sql: target if sql.startswith("SELECT users") else [])

        async def _reassign(*_a, **_k):
            # It updates every company-managed table, runs included.
            db.log.append("update assignment_runs")
            return {}

        monkeypatch.setattr(users_module, "reassign_company_managed_entities", _reassign)
        monkeypatch.setattr(users_module, "prune_user_grants", _noop)
        monkeypatch.setattr(users_module, "log_audit", _noop)
        monkeypatch.setattr(users_module, "_clear_deleted_user_memory", _noop)
        monkeypatch.setattr("app.services.quota.invalidate_membership_quota_cache", _noop)

        await users_module.delete_user(
            user_id=str(target.id),
            request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={}),
            admin=SimpleNamespace(id=uuid.uuid4(), email="admin-acting@example.com"),
            db=db,
        )
        _assert_tasks_locked_first(db.log)
        _assert_locked_again_right_before(db.log, "delete SimpleNamespace")
