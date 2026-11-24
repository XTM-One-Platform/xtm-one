"""Run and background task deadlines count from the start, not from creation (#1433).

An assignment run or a background task is created with its execution budget
(timeout + grace) on its payload and a ``deadline_at`` that is only the
queue-wait bound. The first claim moves the deadline to ``start + budget`` and
records the queue wait; work never started before the queue bound is
dead-lettered as ``deadline_exceeded_before_start``, with no ``run.failed``.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.models.orchestration import OrchestrationExecution
from app.services import orchestration_engine as engine
from app.services import sub_agent as sub_agent_module
from app.services.assignment_engine import max_queue_wait_seconds
from app.services.failure_causes import BILLABLE_CANDIDATE_CAUSES, FailureCause
from app.services.orchestration_adapters import assignment as assignment_adapter


class _Result:
    def __init__(self, items=None, scalar=None):
        self._items = items or []
        self._scalar = scalar

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._items))

    def scalar_one_or_none(self):
        return self._scalar


class _Db:
    def __init__(self, *, items=None, scalar=None):
        self._items = items or []
        self._scalar = scalar
        self.commits = 0

    async def execute(self, *_args, **_kwargs):
        return _Result(items=self._items, scalar=self._scalar)

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1


class _Clock(datetime):
    """``datetime.now`` frozen at ``_Clock.at``."""

    at = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):
        return cls.at


def _queued(created: datetime, budget: float, *, queue_wait: float = 3600) -> OrchestrationExecution:
    deadline, extra = engine.queued_deadline(created, budget, max_queue_wait_seconds=queue_wait)
    return OrchestrationExecution(
        id=uuid.uuid4(),
        adapter="assignment",
        state="pending",
        adapter_state=None,
        payload={"run_id": str(uuid.uuid4()), **extra},
        run_at=created,
        deadline_at=deadline,
        lease_owner=None,
        lease_expires_at=None,
        attempt_count=0,
        max_attempts=3,
        last_error=None,
        dedup_key=None,
    )


def test_queued_deadline_is_the_queue_bound_with_the_budget_on_the_payload():
    created = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)
    deadline, extra = engine.queued_deadline(created, 720, max_queue_wait_seconds=1800)
    assert deadline == created + timedelta(seconds=1800)
    assert extra == {engine.START_BUDGET_PAYLOAD_KEY: 720.0}


def test_queued_deadline_disabled_timeout_sets_nothing():
    assert engine.queued_deadline(datetime.now(UTC), None) == (None, {})


def test_queued_deadline_without_queue_bound_has_no_deadline_until_the_start():
    deadline, extra = engine.queued_deadline(datetime.now(UTC), 720, max_queue_wait_seconds=0)
    assert deadline is None
    assert extra == {engine.START_BUDGET_PAYLOAD_KEY: 720.0}


@pytest.mark.asyncio
async def test_a_run_queued_900_s_gets_its_full_budget_from_its_start():
    """Timeout 600 s + grace, held 900 s in the queue, then claimed: the whole budget is left."""
    created = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)
    budget = 600 + 120
    row = _queued(created, budget)
    start = created + timedelta(seconds=900)
    _Clock.at = start
    with patch.object(engine, "datetime", _Clock):
        claimed = await engine.claim_due(_Db(items=[row]), adapter="assignment", worker_id="w1", limit=5)
    assert claimed == [row]
    assert row.deadline_at == start + timedelta(seconds=budget)
    assert engine.started_at(row) == start
    assert engine.queue_wait_seconds(row) == pytest.approx(900)
    # The runner and the wait tool read the execution's deadline: the whole 600 s timeout is
    # runnable (the grace stays held back for the wind-down), none of it spent in the queue.
    assert engine.runnable_seconds_left(row.deadline_at, now=start) == pytest.approx(600)


@pytest.mark.asyncio
async def test_a_reclaim_keeps_the_start_and_its_deadline():
    created = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)
    row = _queued(created, 720)
    _Clock.at = created + timedelta(seconds=60)
    with patch.object(engine, "datetime", _Clock):
        await engine.claim_due(_Db(items=[row]), adapter="assignment", worker_id="w1", limit=5)
    first_deadline = row.deadline_at
    # Handed back, claimed again later: the clock does not restart.
    row.state, row.lease_owner, row.lease_expires_at = "pending", None, None
    _Clock.at = created + timedelta(seconds=400)
    with patch.object(engine, "datetime", _Clock):
        await engine.claim_due(_Db(items=[row]), adapter="assignment", worker_id="w2", limit=5)
    assert row.deadline_at == first_deadline
    assert engine.started_at(row) == created + timedelta(seconds=60)


@pytest.mark.asyncio
async def test_work_without_a_start_budget_keeps_its_deadline_at_claim():
    created = datetime.now(UTC)
    deadline = created + timedelta(seconds=300)
    row = _queued(created, 720)
    row.payload = {"run_id": "r1"}
    row.deadline_at = deadline
    await engine.claim_due(_Db(items=[row]), adapter="assignment", worker_id="w1", limit=5)
    assert row.deadline_at == deadline
    assert engine.STARTED_AT_PAYLOAD_KEY not in row.payload


@pytest.mark.asyncio
async def test_unstarted_work_past_the_queue_bound_expires_before_start():
    created = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)
    row = _queued(created, 720, queue_wait=1800)
    _Clock.at = created + timedelta(seconds=1801)
    with patch.object(engine, "datetime", _Clock):
        dead = await engine.sweep_deadlines(_Db(items=[row]), limit=10)
    assert dead == [row]
    assert row.state == "dead_letter"
    assert engine.expired_before_start(row)
    assert engine.queue_wait_seconds(row) == pytest.approx(1801)
    assert "deadline_exceeded_before_start" in row.last_error


@pytest.mark.asyncio
async def test_started_work_past_its_budget_is_a_plain_deadline():
    created = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)
    row = _queued(created, 720)
    _Clock.at = created + timedelta(seconds=10)
    with patch.object(engine, "datetime", _Clock):
        await engine.claim_due(_Db(items=[row]), adapter="assignment", worker_id="w1", limit=5)
    _Clock.at = created + timedelta(seconds=10 + 721)
    with patch.object(engine, "datetime", _Clock):
        dead = await engine.sweep_deadlines(_Db(items=[row]), limit=10)
    assert dead == [row]
    assert not engine.expired_before_start(row)
    assert row.last_error.startswith("Deadline exceeded at")


@pytest.mark.asyncio
async def test_an_expired_before_start_run_ends_expired_not_started_without_run_failed():
    """Expired, not started: skipped (never failed), no model call, no run.failed, queue wait recorded."""
    run = SimpleNamespace(
        id=uuid.uuid4(),
        status="pending",
        error=None,
        trigger_context={"type": "schedule"},
        failure_cause=None,
        failure_determination=None,
        completed_at=None,
        stop_reason=None,
    )
    execution = SimpleNamespace(
        payload={
            "run_id": str(run.id),
            engine.START_BUDGET_PAYLOAD_KEY: 720.0,
            engine.EXPIRED_BEFORE_START_PAYLOAD_KEY: True,
            engine.QUEUE_WAIT_PAYLOAD_KEY: 1801.0,
        },
        state="dead_letter",
        attempt_count=0,
        max_attempts=3,
        deadline_at=None,
        last_error="x",
    )
    db = _Db(scalar=run)
    published = AsyncMock()
    with (
        patch("app.services.assignment_engine._publish_run_failed", published),
        patch("app.services.assignment_engine.execute_assignment_run", AsyncMock()) as body,
    ):
        await assignment_adapter.AssignmentAdapter().on_dead_letter(SimpleNamespace(db=db), execution)
    assert run.status == "skipped"
    assert run.stop_reason == assignment_adapter.EXPIRED_NOT_STARTED_STOP_REASON == "expired_not_started"
    assert run.failure_cause == FailureCause.DEADLINE_EXCEEDED_BEFORE_START.value
    assert run.trigger_context["queue"] == {"queue_wait_seconds": 1801.0, "started": False}
    assert "never started" in run.error
    published.assert_not_awaited()
    body.assert_not_awaited()
    assert db.commits == 1


def test_the_new_cause_is_a_capacity_cause():
    assert FailureCause.DEADLINE_EXCEEDED_BEFORE_START.value == "deadline_exceeded_before_start"
    assert FailureCause.DEADLINE_EXCEEDED_BEFORE_START in BILLABLE_CANDIDATE_CAUSES


def test_assignment_queue_bound_override():
    assert max_queue_wait_seconds(SimpleNamespace(trigger_config={"max_queue_wait_seconds": 900})) == 900.0
    assert max_queue_wait_seconds(SimpleNamespace(trigger_config={"max_queue_wait_seconds": 0})) == 0.0
    assert max_queue_wait_seconds(SimpleNamespace(trigger_config={"max_queue_wait_seconds": True})) is None
    assert max_queue_wait_seconds(SimpleNamespace(trigger_config={})) is None
    assert max_queue_wait_seconds(SimpleNamespace(trigger_config=None)) is None


def test_background_tasks_count_their_budget_from_their_start():
    """create_sub_agent_task puts the budget on the payload and the queue bound on deadline_at."""
    source = inspect.getsource(sub_agent_module.create_sub_agent_task)
    assert "queued_deadline(" in source
    assert "**start_budget" in source


@pytest.mark.asyncio
async def test_a_background_task_queued_past_its_budget_still_gets_it_at_start():
    created = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)
    row = _queued(created, 1800 + 120)
    row.adapter = "sub_agent"
    row.payload = {"task_id": str(uuid.uuid4()), engine.START_BUDGET_PAYLOAD_KEY: 1920.0}
    start = created + timedelta(seconds=2400)
    _Clock.at = start
    with patch.object(engine, "datetime", _Clock):
        await engine.claim_due(_Db(items=[row]), adapter="sub_agent", worker_id="w1", limit=5)
    assert row.deadline_at == start + timedelta(seconds=1920)
