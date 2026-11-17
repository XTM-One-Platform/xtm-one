"""A replaced scheduled attempt never writes its outcome over its run.

An attempt on a draining worker can finish after its run was ended without
it (a later attempt, a re-queue for the owner agent, the stale-run sweep).
Its late settle must not move the ended run back to ``pending`` or
``completed``: a run revived that way reads as a live holder, so the work item
claims it holds are never released.
"""

from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace

import pytest

from app.services import scheduled_work_item_runner as runner
from app.services.attempt_charges import QUOTA_ATTEMPT_FENCE_KEY


def _run(status: str = "running", fence: tuple[int, int] | None = None, run_id: uuid.UUID | None = None):
    context: dict = {"action_started_at": "x", "attempt": 1}
    if fence is not None:
        context[QUOTA_ATTEMPT_FENCE_KEY] = [fence[0], fence[1]]
    return SimpleNamespace(
        id=run_id or uuid.uuid4(),
        status=status,
        trigger_context=context,
        messages=None,
        actions_taken=None,
        error=None,
        result_summary=None,
        completed_at=None,
    )


def _item(run_id: uuid.UUID | None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        run_id=run_id,
        status="in_progress",
        owner_agent_id=uuid.uuid4(),
        outcome_code=None,
        result=None,
        claimed_by_agent_id=None,
        last_activity_at=None,
    )


class TestSupersededSettle:
    @pytest.mark.parametrize("status", ["completed", "failed", "skipped"])
    def test_an_ended_run_is_never_written_over(self, status):
        run = _run(status)
        assert runner.superseded_settle(run, _item(run.id), (0, 1)) == f"its run already ended {status}"

    def test_a_later_attempt_fenced_the_run(self):
        run = _run(fence=(0, 3))
        assert runner.superseded_settle(run, _item(run.id), (0, 2)) == "a later attempt of its run took it over"

    def test_a_later_pause_epoch_fenced_the_run(self):
        run = _run(fence=(1, 0))
        assert runner.superseded_settle(run, _item(run.id), (0, 2)) is not None

    def test_the_current_attempt_settles(self):
        run = _run(fence=(0, 2))
        assert runner.superseded_settle(run, _item(run.id), (0, 2)) is None

    def test_a_first_attempt_with_no_fence_settles(self):
        run = _run()
        assert runner.superseded_settle(run, _item(run.id), (0, 1)) is None

    def test_no_place_reads_only_the_status_and_the_item(self):
        run = _run(fence=(0, 5))
        assert runner.superseded_settle(run, _item(run.id), None) is None

    def test_an_item_a_fresh_run_owns_is_not_touched(self):
        run = _run()
        fresh = uuid.uuid4()
        assert runner.superseded_settle(run, _item(fresh), (0, 1)) == f"its work item is now run by {fresh}"

    def test_a_cancelled_run_still_settles_its_item_as_stopped(self):
        run = _run("cancelled")
        assert runner.superseded_settle(run, _item(run.id), (0, 1)) is None


class _Session:
    def __init__(self):
        self.committed = False
        self.rolled_back = False

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


@pytest.fixture
def session(monkeypatch):
    from app.core import database

    db = _Session()

    @contextlib.asynccontextmanager
    async def _session():
        yield db

    monkeypatch.setattr(database, "async_session", _session)
    return db


def _published(monkeypatch) -> list:
    calls: list = []

    async def _transition(*args, **kwargs):
        calls.append(("transition", args, kwargs))

    async def _deliver(item_id):
        calls.append(("deliver", item_id))

    monkeypatch.setattr(runner, "publish_transition", _transition)
    monkeypatch.setattr(runner.swi, "deliver_outcome", _deliver)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["retry", "done", "blocked"])
async def test_a_late_attempt_leaves_a_failed_run_failed(monkeypatch, session, kind):
    run = _run("failed")
    item = _item(run.id)
    item.status = "claimed"

    async def _locked(_db, _run_id):
        return run, item

    monkeypatch.setattr(runner, "_locked", _locked)
    calls = _published(monkeypatch)
    outcome = await runner._settle(run.id, runner._Executed(kind, "late"), place=(0, 1))
    assert outcome.kind == "ended"
    assert run.status == "failed" and item.status == "claimed"
    assert session.rolled_back and not session.committed
    assert calls == []


@pytest.mark.asyncio
async def test_a_replaced_attempt_writes_nothing(monkeypatch, session):
    run = _run(fence=(0, 2))
    item = _item(run.id)

    async def _locked(_db, _run_id):
        return run, item

    monkeypatch.setattr(runner, "_locked", _locked)
    _published(monkeypatch)
    outcome = await runner._settle(run.id, runner._Executed("retry", "late"), place=(0, 1))
    assert outcome.kind == "ended" and run.status == "running" and item.status == "in_progress"


@pytest.mark.asyncio
async def test_the_current_attempt_still_settles(monkeypatch, session):
    run = _run(fence=(0, 2))
    item = _item(run.id)

    async def _locked(_db, _run_id):
        return run, item

    monkeypatch.setattr(runner, "_locked", _locked)
    calls = _published(monkeypatch)
    outcome = await runner._settle(run.id, runner._Executed("done", "sent"), place=(0, 2))
    assert outcome.kind == "done" and run.status == "completed" and item.status == "done"
    assert session.committed and calls


def test_the_run_passes_its_place_to_the_settle():
    import inspect

    source = inspect.getsource(runner.execute_scheduled_run)
    assert "_settle(run_uuid, executed, place=place)" in source
