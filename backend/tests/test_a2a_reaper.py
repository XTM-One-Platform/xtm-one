"""Stale-run reaper.

A run whose replica died mid-execution leaves its status at ``running`` with a
stale heartbeat, and the task stays pollable forever - which the specification
forbids outright ("100% of externally submitted tasks reach a terminal state").

Marked ``integration`` where it needs real Postgres for ``FOR UPDATE SKIP
LOCKED``; the projection logic itself is unit-tested here.
"""

import asyncio
import inspect
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.a2a import tasks as task_svc
from app.core.config import settings
from tests import a2a_server_stubs as stubs


class _Run:
    def __init__(self, status="running"):
        self.id = uuid.uuid4()
        self.status = status
        self.error = None


class _Task:
    def __init__(self, run_id, heartbeat, created_at=None):
        self.id = uuid.uuid4()
        self.run_id = run_id
        self.lease_heartbeat_at = heartbeat
        self.reaped = False
        # The reaper reads this for the total-lifetime bound, which is a
        # separate reason to reap from the stale lease these tests exercise.
        self.created_at = created_at or datetime.now(UTC)


class _Result:
    def __init__(self, v, scalars=None, rowcount=0):
        self._v = v
        self._scalars = scalars
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._v

    def scalars(self):
        outer = self

        class _S:
            def all(self):
                return outer._scalars or []

        return _S()


class _DB:
    """Serves the reaper's three statement kinds in the order it issues them.

    The runs are loaded in ONE batched ``IN`` query, not one per task, so a
    ``None`` in *runs* stands for "this task's run row is gone" — a real query
    simply returns fewer rows, never a null one.
    """

    def __init__(self, stale_tasks, runs):
        self._stale = stale_tasks
        self._runs = [r for r in runs if r is not None]
        self._calls = 0
        self.flushes = 0

    async def execute(self, _stmt):
        self._calls += 1
        if self._calls == 1:
            return _Result(None, scalars=self._stale)
        if self._calls == 2:
            return _Result(None, scalars=self._runs)
        # The status-guarded terminal UPDATE, one per still-active run.
        return _Result(None, rowcount=1)

    async def flush(self):
        self.flushes += 1


@pytest.fixture(autouse=True)
def _no_delegated_work(monkeypatch):
    stubs.silence_delegated_work(monkeypatch, task_svc)


class TestReaping:
    async def test_abandoned_run_is_driven_to_a_terminal_state(self):
        run = _Run("running")
        stale = datetime.now(UTC) - timedelta(
            seconds=settings.a2a_run_lease_seconds + 60
        )
        task = _Task(run.id, stale)
        count = await task_svc.reap_stale_tasks(_DB([task], [run]))
        assert count == 1
        assert run.status == "failed"
        assert "lease expired" in run.error
        # Stamped so the runs list shows the run as finished, not in flight.
        assert run.completed_at is not None
        assert task.reaped is True

    async def test_reaped_task_is_marked_so_it_is_not_reclaimed(self):
        run = _Run("running")
        task = _Task(run.id, datetime.now(UTC) - timedelta(days=1))
        await task_svc.reap_stale_tasks(_DB([task], [run]))
        assert task.reaped is True

    async def test_already_terminal_run_is_left_alone(self):
        """The run finished; only the heartbeat is stale. Overwriting a
        completed run's status would replace a real result with a failure."""
        run = _Run("completed")
        task = _Task(run.id, datetime.now(UTC) - timedelta(days=1))
        await task_svc.reap_stale_tasks(_DB([task], [run]))
        assert run.status == "completed"
        assert task.reaped is True

    async def test_nothing_stale_reaps_nothing(self):
        assert await task_svc.reap_stale_tasks(_DB([], [])) == 0

    async def test_missing_run_still_marks_the_task(self):
        task = _Task(uuid.uuid4(), datetime.now(UTC) - timedelta(days=1))
        assert await task_svc.reap_stale_tasks(_DB([task], [None])) == 1
        assert task.reaped is True


    async def test_what_it_reaped_is_handed_back_to_be_announced(self):
        """The sweep announces after its commit, and can only do that for
        tasks it is told about. Unhanded, a reaped task's terminal state
        reaches no callback."""
        run = _Run("running")
        task = _Task(run.id, datetime.now(UTC) - timedelta(days=1))
        handed: list = []
        assert await task_svc.reap_stale_tasks(
            _DB([task], [run]), reaped_out=handed
        ) == 1
        assert handed == [task]

    async def test_nothing_reaped_hands_back_nothing(self):
        handed: list = []
        await task_svc.reap_stale_tasks(_DB([], []), reaped_out=handed)
        assert handed == []

class TestReaperScheduling:
    """The reaper only enforces the lease if something actually runs it.

    The reap logic above was fully unit-tested while having NO production
    caller — an abandoned task stayed pollable forever despite the green
    tests. These tests pin the wiring (same style as
    test_startup_bootstrap_lock.py): startup must create the loop task and
    the lifespan must tear it down.
    """

    def test_startup_starts_the_reaper_loop(self):
        from app import main

        src = inspect.getsource(main._do_all_startup_work)
        assert "_a2a_reaper_task = asyncio.create_task(_a2a_reaper_loop())" in src, (
            "reap_stale_tasks has no caller unless _do_all_startup_work "
            "schedules _a2a_reaper_loop — abandoned external tasks would "
            "stay pollable forever (spec 006 SC-002)"
        )

    def test_lifespan_cancels_the_reaper_on_shutdown(self):
        from app import main

        src = inspect.getsource(main.lifespan)
        assert "_a2a_reaper_task.cancel()" in src

    async def test_loop_tick_reaps_and_commits(self, monkeypatch):
        """One tick = reap + commit. The commit matters: reap_stale_tasks
        only flushes, so a loop that skipped it would roll the reap back
        at session close and the sweep would be a silent no-op."""
        committed = asyncio.Event()
        calls = {"resume": 0, "reap": 0, "commit": 0}

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def commit(self):
                calls["commit"] += 1
                committed.set()

        async def fake_reap(db, **kwargs):
            calls["reap"] += 1
            return 2

        async def fake_resume(db):
            # The tick resumes the handed-back tasks first (#1657).
            calls["resume"] += 1
            return []

        monkeypatch.setattr("app.core.database.async_session", lambda: _Session())
        monkeypatch.setattr("app.a2a.tasks.reap_stale_tasks", fake_reap)
        monkeypatch.setattr("app.a2a.tasks.resume_handed_back_tasks", fake_resume)

        from app import main

        loop_task = asyncio.create_task(main._a2a_reaper_loop())
        try:
            await asyncio.wait_for(committed.wait(), timeout=5)
        finally:
            loop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop_task
        assert calls == {"resume": 1, "reap": 1, "commit": 1}


@pytest.mark.integration
class TestConcurrentReapers:
    """Two reapers on different replicas must take disjoint work.

    Needs real Postgres: ``FOR UPDATE SKIP LOCKED`` has no meaningful
    behaviour against a fake session, and the failure mode being guarded
    against - both reapers claiming the same row - only appears under real
    row locking.
    """

    # NOT YET WRITTEN, for the same reason as the dedup counterpart: the body
    # was an unconditional ``pytest.skip``, so the ``integration`` marker could
    # never reach an assertion and this stood in for coverage it never provided.
    @pytest.mark.skip(reason="unimplemented: needs the real Postgres harness")
    async def test_skip_locked_prevents_double_claim(self):
        raise AssertionError("unimplemented")
