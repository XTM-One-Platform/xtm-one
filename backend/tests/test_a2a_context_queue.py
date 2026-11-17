"""Parallel messages in one A2A conversation are queued, not refused (#11864).

A partner orchestrator sent three messages at once under one ``contextId``;
the first ran and the other two were refused "the previous task has not
finished". The route now creates them marked queued, and the executor holds
each until the tasks submitted ahead of it in the context are finished.
"""

from __future__ import annotations

import uuid

import pytest

from app.a2a import contexts
from app.a2a import tasks as task_svc
from tests.a2a_server_stubs import FakeDb, run, task

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(_s):
        return None

    monkeypatch.setattr(task_svc.asyncio, "sleep", _instant)


def _ahead(monkeypatch, answers: list[bool]):
    seen: list = []

    async def _has_ahead(_db, t):
        seen.append(t)
        return answers.pop(0) if answers else False

    monkeypatch.setattr(contexts, "has_open_task_ahead", _has_ahead)
    return seen


class TestWaitForContextTurn:
    async def test_three_parallel_messages_all_run_in_turn(self, monkeypatch):
        """The acceptance: three concurrent messages to one agent in one
        context; each waits only while a task ahead of it is open, then runs."""
        _run = run()
        queue = [task(_run.id) for _ in range(3)]
        # First has nothing ahead; second waits one poll; third waits two.
        ahead = {queue[0].id: [], queue[1].id: [True], queue[2].id: [True, True]}

        async def _has_ahead(_db, t):
            remaining = ahead[t.id]
            return remaining.pop(0) if remaining else False

        monkeypatch.setattr(contexts, "has_open_task_ahead", _has_ahead)
        for t in queue:
            # One "pending" status read per poll it waits.
            db = FakeDb(*(["pending", None] * 2))
            assert await task_svc._wait_for_context_turn(db, t, _run.id) is True

    async def test_a_cancelled_queued_run_does_not_execute(self, monkeypatch):
        """Cancelled while waiting its turn: nothing is left to run."""
        _ahead(monkeypatch, [True])
        _run = run()
        db = FakeDb("cancelled")
        assert await task_svc._wait_for_context_turn(db, task(_run.id), _run.id) is False

    async def test_a_waiting_task_renews_its_lease(self, monkeypatch):
        """Each poll commits a heartbeat, so the reaper never takes a task
        that is only waiting its turn."""
        _ahead(monkeypatch, [True, True])
        _run = run()
        db = FakeDb("pending", None, "pending", None)
        assert await task_svc._wait_for_context_turn(db, task(_run.id), _run.id) is True
        assert db.commits == 2


class TestOnlyQueuedRunsWait:
    async def test_an_unqueued_run_never_reads_the_queue(self, monkeypatch):
        """A first message (or one on a fresh context) starts at once."""
        from tests.a2a_server_stubs import drive_executor

        seen = _ahead(monkeypatch, [True])
        captured = drive_executor(monkeypatch)
        await task_svc.execute_task_run(uuid.uuid4())
        assert seen == []
        assert "status" in captured

    async def test_a_queued_run_waits_before_executing(self, monkeypatch):
        from tests.a2a_server_stubs import drive_executor

        seen = _ahead(monkeypatch, [])
        captured = drive_executor(monkeypatch)
        captured["_run"].trigger_payload = {"message": "hi", "a2a_queued": True}
        await task_svc.execute_task_run(uuid.uuid4())
        assert seen == [captured["_task"]]
        assert "status" in captured


class TestOpenTasksAhead:
    async def test_a_task_without_context_has_nothing_ahead(self):
        t = task(None, context_id=None)
        assert await contexts.has_open_task_ahead(FakeDb(), t) is False

    async def test_open_task_count_counts_rows(self):
        ids = [uuid.uuid4() for _ in range(3)]
        ctx = type("Ctx", (), {"id": uuid.uuid4()})()
        assert await contexts.open_task_count(FakeDb(ids), ctx) == 3
