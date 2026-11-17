"""A background task failed at the resume cap releases its claims at once.

Before: the task was failed and its work items stayed ``in_progress``, held by
a dead holder, until the next scheduler tick ran ``release_ended_run_claims``.
Now the failing path releases the claims of the task and of its run in the
same pass, through the same sweep scoped to those holders.
"""

import inspect
import uuid

import pytest
from sqlalchemy.dialects import postgresql

import app.services.work_item as wi_service
from app.services import sub_agent
from app.services.orchestration_adapters import sub_agent as sub_agent_adapter


class _Result:
    def __init__(self, rows=None, rowcount=0):
        self._rows = rows or []
        self.rowcount = rowcount

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
        if not self._results:
            raise AssertionError("unexpected extra execute() call")
        return self._results.pop(0)

    async def commit(self):
        self.committed = True


def _compiled(statement):
    return statement.compile(dialect=postgresql.dialect())


@pytest.mark.asyncio
async def test_a_scoped_release_reads_only_the_named_holders(monkeypatch):
    task_id, run_id, item_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    session = _Session([
        _Result(rows=[(item_id, task_id, None)]),  # held by the failed task
        _Result(rows=[]),  # not an assignment run
        _Result(rows=[(task_id, "failed", None)]),  # the background task, failed
        _Result(rowcount=1),  # the release
    ])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    assert await wi_service.release_ended_run_claims(run_ids=[task_id, run_id, None, "not-a-uuid"]) == 1

    select = _compiled(session.statements[0])
    sql = str(select).lower()
    assert "work_items.claimed_by_run_id in" in sql
    scoped = [v for v in select.params.values() if isinstance(v, (list, tuple)) and task_id in v]
    assert scoped and set(scoped[0]) == {task_id, run_id}
    note = _compiled(session.statements[3]).params["claim_notes"][0]["text"]
    assert note.startswith(f"released: run {task_id} ended failed at")


@pytest.mark.asyncio
async def test_a_scoped_release_with_no_readable_holder_reads_nothing(monkeypatch):
    session = _Session([])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    assert await wi_service.release_ended_run_claims(run_ids=[None, "x"]) == 0
    assert session.statements == []


@pytest.mark.asyncio
async def test_the_unscoped_sweep_is_unchanged(monkeypatch):
    session = _Session([_Result(rows=[])])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    assert await wi_service.release_ended_run_claims() == 0
    assert "claimed_by_run_id in" not in str(_compiled(session.statements[0])).lower()


@pytest.mark.asyncio
async def test_release_failed_task_claims_names_the_task_and_its_run(monkeypatch):
    seen = {}

    async def fake_release(*, limit=500, run_ids=None):
        seen["run_ids"] = list(run_ids)
        return 2

    monkeypatch.setattr(wi_service, "release_ended_run_claims", fake_release)
    task_id, run_id = uuid.uuid4(), uuid.uuid4()
    assert await sub_agent.release_failed_task_claims(task_id, str(run_id)) == 2
    assert seen["run_ids"] == [task_id, str(run_id)]


@pytest.mark.asyncio
async def test_release_failed_task_claims_never_raises(monkeypatch):
    async def broken(**_kwargs):
        raise RuntimeError("db gone")

    monkeypatch.setattr(wi_service, "release_ended_run_claims", broken)
    assert await sub_agent.release_failed_task_claims(uuid.uuid4()) == 0


def test_the_refused_resume_releases_the_claims_after_the_failure_is_committed():
    source = inspect.getsource(sub_agent._execute_sub_agent_task)
    refused = source.index("if refused_resume is not None:")
    branch = source[refused : source.index("return SubAgentTaskOutcome(status=\"failed\", error=refused_resume)", refused)]
    assert branch.index("await db.commit()") < branch.index("await release_failed_task_claims(")


def test_the_dead_letter_releases_the_claims_after_the_failure_is_committed():
    source = inspect.getsource(sub_agent_adapter.SubAgentAdapter.on_dead_letter)
    release = source.index("await release_failed_task_claims(task_id, assignment_run_id)")
    assert source.rindex("await ctx.db.commit()", 0, release) < release
    # Last: after the parent wake, which commits on its own branch.
    assert source.index("_schedule_parent_wake_if_needed(") < release
    # After the run row is failed too, so both holders read as ended.
    assert source.index('run.status = "failed"') < release
