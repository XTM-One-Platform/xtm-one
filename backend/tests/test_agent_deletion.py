"""The one agent deletion, shared by the REST route and the agent-builder tool.

What the deletion leaves in the database is pinned against PostgreSQL by
``test_agent_deletion_parity_postgres.py``; these pin its contract with its two
callers - a refusal, an agent already gone, a failure - and that both callers
go through it.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import app.api.v1.helpers as api_helpers
import app.core.database as database
from app.services import agent_deletion, agent_memory


class _Session:
    def __init__(self, agent=None):
        self.agent = agent
        self.log: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, statement, *_a, **_kw):
        self.log.append("serialize" if "pg_advisory_xact_lock" in str(statement) else "execute")

    async def get(self, _model, _pk, **kw):
        self.log.append(f"get populate_existing={kw.get('populate_existing')}")
        return self.agent

    async def commit(self):
        self.log.append("commit")

    async def rollback(self):
        self.log.append("rollback")


@pytest.fixture
def world(monkeypatch):
    sessions: list[_Session] = []

    def _session():
        sessions.append(_Session())
        return sessions[-1]

    clear = AsyncMock()
    monkeypatch.setattr(database, "async_session", _session)
    monkeypatch.setattr(agent_memory, "clear_deleted_agent_memory", clear)
    # No run left: one batch session, then the agent's own transaction.
    monkeypatch.setattr(agent_deletion, "_delete_run_batch", AsyncMock(return_value=_batch_of(0)))
    monkeypatch.setattr(agent_deletion, "_audit_run_purge", AsyncMock())
    return sessions, clear


def _batch_of(selected: int, deleted: int | None = None) -> agent_deletion._RunBatch:
    return agent_deletion._RunBatch(selected=selected, deleted=selected if deleted is None else deleted)


@pytest.mark.asyncio
async def test_a_refusal_is_returned_and_deletes_nothing(world, monkeypatch):
    sessions, clear = world
    rows = AsyncMock()
    monkeypatch.setattr(agent_deletion, "_delete_agent_rows", rows)

    async def _refuse(_db):
        return "Cannot delete built-in agents."

    assert await agent_deletion.delete_agent(uuid.uuid4(), authorize=_refuse) == "Cannot delete built-in agents."
    rows.assert_not_awaited()
    clear.assert_not_awaited()
    assert sessions[0].log == ["rollback"]


@pytest.mark.asyncio
async def test_an_agent_already_gone_once_the_lock_is_held_is_done(world):
    """A deletion that held the lock first removed it: nothing to write, memory still cleared."""
    sessions, clear = world
    agent_id = uuid.uuid4()

    async def _caller(_db):
        return object()

    assert await agent_deletion.delete_agent(agent_id, authorize=_caller) is None
    # Read after the lock, bypassing any copy the gate loaded before it.
    assert sessions[1].log == ["serialize", "get populate_existing=True", "commit"]
    clear.assert_awaited_once_with(str(agent_id))


@pytest.mark.asyncio
async def test_a_failed_deletion_raises_and_keeps_the_agent_memory(world, monkeypatch):
    sessions, clear = world
    monkeypatch.setattr(agent_deletion, "_delete_agent_rows", AsyncMock(side_effect=RuntimeError("boom")))

    async def _caller(_db):
        return object()

    with pytest.raises(RuntimeError):
        await agent_deletion.delete_agent(uuid.uuid4(), authorize=_caller)
    assert [s.log for s in sessions] == [["commit"], ["rollback"]]
    clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_every_attempt_authorizes_again_on_its_own_session(world, monkeypatch):
    sessions, _clear = world
    monkeypatch.setattr(api_helpers.asyncio, "sleep", AsyncMock())
    deadlock = Exception("deadlock detected")
    monkeypatch.setattr(agent_deletion, "_delete_agent_rows", AsyncMock(side_effect=[deadlock, None]))
    seen = []

    async def _caller(db):
        seen.append(db)
        return object()

    assert await agent_deletion.delete_agent(uuid.uuid4(), authorize=_caller) is None
    assert seen == sessions and len(sessions) == 3
    assert [s.log for s in sessions] == [["commit"], ["rollback"], ["commit"]]


@pytest.mark.asyncio
async def test_the_runs_go_in_committed_batches_before_the_agent(world, monkeypatch):
    sessions, clear = world
    order: list[str] = []
    full = agent_deletion.RUN_BATCH_SIZE

    async def _batch(_db, _agent_id):
        order.append("runs")
        return _batch_of([full, full, 3][order.count("runs") - 1])

    async def _rows(*_a, **_k):
        order.append("agent")

    monkeypatch.setattr(agent_deletion, "_delete_run_batch", _batch)
    monkeypatch.setattr(agent_deletion, "_delete_agent_rows", _rows)

    async def _caller(_db):
        return object()

    assert await agent_deletion.delete_agent(uuid.uuid4(), authorize=_caller) is None
    # A batch short of full is the last: the agent's transaction comes next.
    assert order == ["runs", "runs", "runs", "agent"]
    assert [s.log for s in sessions] == [["commit"]] * 4
    # Audited once, by the first batch, in its own transaction.
    agent_deletion._audit_run_purge.assert_awaited_once()
    assert agent_deletion._audit_run_purge.await_args.args[0] is sessions[0]
    clear.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_refusal_between_batches_stops_the_deletion(world, monkeypatch):
    sessions, clear = world
    monkeypatch.setattr(agent_deletion, "_delete_run_batch", AsyncMock(return_value=_batch_of(agent_deletion.RUN_BATCH_SIZE)))
    rows = AsyncMock()
    monkeypatch.setattr(agent_deletion, "_delete_agent_rows", rows)
    answers = iter([object(), "Agent not found."])

    async def _caller(_db):
        return next(answers)

    assert await agent_deletion.delete_agent(uuid.uuid4(), authorize=_caller) == "Agent not found."
    assert [s.log for s in sessions] == [["commit"], ["rollback"]]
    rows.assert_not_awaited()
    clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_batch_keeps_the_batches_committed_before_it(world, monkeypatch):
    """What a failure leaves is what the next deletion no longer has to delete."""
    sessions, clear = world
    monkeypatch.setattr(
        agent_deletion,
        "_delete_run_batch",
        AsyncMock(
            side_effect=[
                _batch_of(agent_deletion.RUN_BATCH_SIZE),
                RuntimeError("canceling statement due to statement timeout"),
            ]
        ),
    )
    rows = AsyncMock()
    monkeypatch.setattr(agent_deletion, "_delete_agent_rows", rows)

    async def _caller(_db):
        return object()

    with pytest.raises(RuntimeError):
        await agent_deletion.delete_agent(uuid.uuid4(), authorize=_caller)
    assert [s.log for s in sessions] == [["commit"], ["rollback"]]
    rows.assert_not_awaited()
    clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_batch_that_deleted_nothing_writes_no_audit_entry(world, monkeypatch):
    """A concurrent deletion took every run the first batch read: the batch that
    then deletes runs is the one that audits them."""
    sessions, _clear = world
    full = agent_deletion.RUN_BATCH_SIZE
    monkeypatch.setattr(
        agent_deletion, "_delete_run_batch", AsyncMock(side_effect=[_batch_of(full, deleted=0), _batch_of(3)])
    )
    monkeypatch.setattr(agent_deletion, "_delete_agent_rows", AsyncMock())

    async def _caller(_db):
        return object()

    assert await agent_deletion.delete_agent(uuid.uuid4(), authorize=_caller) is None
    agent_deletion._audit_run_purge.assert_awaited_once()
    assert agent_deletion._audit_run_purge.await_args.args[0] is sessions[1]


_BACKEND = Path(__file__).resolve().parents[1]

# The steps of an agent deletion: only ``services/agent_deletion.py`` takes them.
_STEPS = {
    "acquire_agent_delete_lock",
    "lock_tasks_before_deletion",
    "purge_agent_references",
    "cancel_executions_for_runs",
    "mark_sub_agent_tasks_cancelled_for_runs",
    "cancel_document_jobs_after_commit",
    "run_with_deadlock_retry",
    "clear_deleted_agent_memory",
    "log_audit",
}


def _function(path: str, name: str) -> ast.AST:
    tree = ast.parse((_BACKEND / path).read_text(encoding="utf-8"))
    [node] = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == name]
    return node


def _called(node: ast.AST) -> set[str]:
    return {
        getattr(call.func, "id", None) or getattr(call.func, "attr", None)
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
    }


@pytest.mark.parametrize(
    ("path", "function"),
    [("app/api/v1/agents.py", "delete_agent"), ("app/services/builtin_tools/agent_builder.py", "_delete_agent")],
    ids=["rest-route", "agent-builder-tool"],
)
def test_both_ways_to_delete_an_agent_go_through_the_shared_deletion(path, function):
    """A step added to one caller and not the other is the drift this module ended."""
    called = _called(_function(path, function))
    assert any(
        isinstance(call.func, ast.Attribute) and call.func.attr == "delete_agent"
        and getattr(call.func.value, "id", None) == "agent_deletion"
        for call in ast.walk(_function(path, function))
        if isinstance(call, ast.Call)
    ), f"{path}:{function} no longer deletes through agent_deletion.delete_agent"
    assert called & _STEPS == set(), f"{path}:{function} takes a deletion step itself: {sorted(called & _STEPS)}"
