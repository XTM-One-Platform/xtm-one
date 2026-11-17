"""A task does not go terminal while work it delegated is still running.

Spec 010 User Story 1. The executor's own run finishing is a different event
from the task finishing: an agent may hand work to a peer or to a copy of
itself, that work outlives the run, and the partner holds only the task.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.a2a import protocol as proto
from app.a2a import tasks as task_svc
from tests.a2a_server_stubs import FakeDb, Result, agent, run, task

pytestmark = pytest.mark.asyncio


class TestStateWhileDelegatedWorkRuns:
    async def test_a_completed_run_with_live_children_reports_working(self, monkeypatch):
        """FR-001. The run is done; the work is not, so neither is the task."""
        _run = run(status="completed")
        _task = task(_run.id)

        async def _live(*_a, **_k):
            return True

        monkeypatch.setattr(task_svc, "live_delegated_work", _live)
        state = await task_svc.resolve_state(FakeDb(_run), _task)
        assert state is proto.A2AState.WORKING

    async def test_the_runs_own_state_stands_once_no_children_remain(self, monkeypatch):
        _run = run(status="completed")

        async def _none(*_a, **_k):
            return False

        monkeypatch.setattr(task_svc, "live_delegated_work", _none)
        state = await task_svc.resolve_state(FakeDb(_run), task(_run.id))
        assert state is proto.A2AState.COMPLETED

    async def test_a_cancel_request_still_wins_over_live_children(self, monkeypatch):
        """A cancelled task must not be dragged back to working by its own
        children — FR-004 stops them, and the partner was told cancelled."""
        _run = run(status="running")
        _task = task(_run.id, cancel_requested_at=datetime.now(UTC))

        async def _live(*_a, **_k):
            return True

        monkeypatch.setattr(task_svc, "live_delegated_work", _live)
        assert await task_svc.resolve_state(FakeDb(_run), _task) is proto.A2AState.CANCELED

    async def test_a_failed_run_with_live_children_still_reports_working(self, monkeypatch):
        """The parent failing does not retract work already handed off; the
        task stays open until that work stops, then reports the failure."""
        _run = run(status="failed")

        async def _live(*_a, **_k):
            return True

        monkeypatch.setattr(task_svc, "live_delegated_work", _live)
        assert (
            await task_svc.resolve_state(FakeDb(_run), task(_run.id))
            is proto.A2AState.WORKING
        )

    async def test_no_run_needs_no_child_query(self, monkeypatch):
        """A task with no run cannot have delegated anything; asking would be a
        query per poll for a guaranteed answer."""
        called = {"n": 0}

        async def _count(*_a, **_k):
            called["n"] += 1
            return False

        monkeypatch.setattr(task_svc, "live_delegated_work", _count)
        assert await task_svc.resolve_state(FakeDb(), task()) is proto.A2AState.SUBMITTED
        assert called["n"] == 0


class TestTheChildLookup:
    async def test_it_matches_the_indexed_json_path(self):
        """The linkage `mark_sub_agent_tasks_cancelled_for_runs` already uses,
        and the one the migration indexes. Matching a different path would read
        as correct and return nothing."""
        import inspect

        src = inspect.getsource(task_svc.live_delegated_work)
        assert "parent_run_id" in src
        assert "pending" in src and "running" in src

    async def test_it_reports_false_for_a_run_with_no_children(self):
        assert await task_svc.live_delegated_work(FakeDb(Result([])), uuid.uuid4()) is False

    async def test_it_reports_true_when_one_is_live(self):
        assert (
            await task_svc.live_delegated_work(FakeDb(Result([object()])), uuid.uuid4())
            is True
        )


class TestNotReportedAbandonedWhileWorking:
    async def test_the_reaper_skips_a_task_whose_children_are_live(self, monkeypatch):
        """FR-002's obligation: a parked task must not be reported abandoned.
        Its lease goes stale because nothing in-process is heartbeating for
        work running on a worker, so the reaper asks instead."""
        _run = run(status="running")
        _task = task(
            _run.id,
            lease_heartbeat_at=datetime.now(UTC) - timedelta(days=1),
        )

        async def _live(*_a, **_k):
            return True

        monkeypatch.setattr(task_svc, "live_delegated_work", _live)
        reaped = await task_svc.reap_stale_tasks(FakeDb(Result([_task]), Result([_run])))
        assert reaped == 0
        assert _task.reaped is False

    async def test_a_genuinely_abandoned_task_is_still_reaped(self, monkeypatch):
        _run = run(status="running")
        _task = task(
            _run.id, lease_heartbeat_at=datetime.now(UTC) - timedelta(days=1)
        )

        async def _none(*_a, **_k):
            return False

        monkeypatch.setattr(task_svc, "live_delegated_work", _none)
        reaped = await task_svc.reap_stale_tasks(FakeDb(Result([_task]), Result([_run])))
        assert reaped == 1


class TestTheParkedCeiling:
    async def test_a_task_past_its_lifetime_is_driven_terminal(self, monkeypatch):
        """FR-005. Live children hold a task open, so without a total bound a
        chain that never finishes leaves it pollable forever."""
        from app.core.config import settings

        _run = run(status="running")
        _task = task(
            _run.id,
            lease_heartbeat_at=datetime.now(UTC),
            created_at=datetime.now(UTC)
            - timedelta(seconds=settings.a2a_task_max_lifetime_seconds + 60),
        )

        async def _live(*_a, **_k):
            return True

        monkeypatch.setattr(task_svc, "live_delegated_work", _live)
        assert await task_svc.reap_stale_tasks(FakeDb(Result([_task]), Result([_run]))) == 1

    async def test_a_parked_task_whose_run_finished_is_still_bounded(
        self, monkeypatch
    ):
        """The case the bound exists for. The run finished — marking the task
        reaped — while the work it delegated runs on, so the lease disjunct,
        which that same flag excludes, can never see it again. Left out, the
        partner polls ``working`` forever: ``resolve_state`` folds the live
        child in whatever the run says."""
        from app.core.config import settings

        _run = run(status="completed")
        _task = task(
            _run.id,
            reaped=True,
            lease_heartbeat_at=None,
            created_at=datetime.now(UTC)
            - timedelta(seconds=settings.a2a_task_max_lifetime_seconds + 60),
        )
        stopped: list = []

        async def _live(*_a, **_k):
            # Alive until the sweep stops it, which is what makes the task
            # terminal — there is nothing on the run left to drive.
            return not stopped

        async def _cancel(_db, run_ids):
            stopped.extend(run_ids)

        monkeypatch.setattr(task_svc, "live_delegated_work", _live)
        monkeypatch.setattr(
            "app.services.sub_agent.mark_sub_agent_tasks_cancelled_for_runs", _cancel
        )
        reaped: list = []
        assert (
            await task_svc.reap_stale_tasks(
                FakeDb(Result([_task]), Result([_run])), reaped_out=reaped
            )
            == 1
        )
        assert stopped == [_run.id], "the work holding it open has to stop"
        assert reaped == [_task], "and the partner has to be told"

    async def test_the_selection_reaches_an_already_reaped_parked_task(self):
        """``reaped`` guards abandonment, not the total bound: a normally
        finalised run sets it, and gating the whole SELECT on it is what made
        the bound unreachable for the one case it was added for."""
        import inspect

        src = inspect.getsource(task_svc.reap_stale_tasks)
        select_body = src[src.index("select(A2ATask)") : src.index("if not rows")]
        assert "live_child" in select_body
        # Not the outermost predicate any more.
        assert not select_body.index("A2ATask.run_id.isnot(None)") > select_body.index(
            "A2ATask.reaped.is_(False)"
        )

    def test_its_reason_reaches_the_partner(self):
        """An unlisted reason becomes a generic line the partner cannot tell
        from any other failure."""
        assert task_svc.LIFETIME_ERROR_TEXT.startswith(
            task_svc._PARTNER_VISIBLE_ERROR_PREFIXES
        )


class TestDelegatedOutputReachesTheTask:
    def _agent(self):
        return agent(output_format="text")

    async def test_each_finished_child_becomes_an_artifact(self):
        """FR-003. The partner holds one task id and no way to address a child."""
        _run = run(status="completed", result_summary="parent answer")
        child = type(
            "Child",
            (),
            {
                "id": uuid.uuid4(),
                "status": "completed",
                "result": "child answer",
                "target_agent_id": uuid.uuid4(),
                "error": None,
            },
        )()
        arts = await task_svc.build_artifacts(
            FakeDb(_run, Result([child]), "Peer"), task(_run.id), self._agent(), "1.0"
        )
        assert [a["name"] for a in arts] == ["result", "delegated-result"]
        assert arts[1]["parts"][0] == {"text": "child answer"}

    async def test_a_child_is_attributed_to_the_agent_that_ran_it(self):
        _run = run(status="completed", result_summary="parent answer")
        target = uuid.uuid4()
        child = type(
            "Child",
            (),
            {
                "id": uuid.uuid4(),
                "status": "completed",
                "result": "child answer",
                "target_agent_id": target,
                "error": None,
            },
        )()
        arts = await task_svc.build_artifacts(
            FakeDb(_run, Result([child]), "Peer Specialist"),
            task(_run.id),
            self._agent(),
            "1.0",
        )
        assert arts[1]["metadata"]["respondingAgent"] == {
            "id": str(target),
            "name": "Peer Specialist",
        }

    async def test_an_unfinished_child_contributes_nothing(self):
        _run = run(status="completed", result_summary="parent answer")
        child = type(
            "Child",
            (),
            {
                "id": uuid.uuid4(),
                "status": "running",
                "result": None,
                "target_agent_id": uuid.uuid4(),
                "error": None,
            },
        )()
        arts = await task_svc.build_artifacts(
            FakeDb(_run, Result([child])), task(_run.id), self._agent(), "1.0"
        )
        assert [a["name"] for a in arts] == ["result"]


class TestCancellationReachesTheChildren:
    def test_the_cancel_path_cascades(self):
        """FR-004. Without this a task reports cancelled while its work runs on
        — which is the same untruth as reporting completed."""
        import inspect

        assert "cascade_cancel" in inspect.getsource(task_svc.request_cancel)
        src = inspect.getsource(task_svc.cascade_cancel)
        assert "mark_sub_agent_tasks_cancelled_for_runs" in src
