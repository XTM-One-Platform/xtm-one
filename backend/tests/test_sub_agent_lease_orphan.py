"""A background task's lease and its orphaned worker.

A sub-agent delegation runs under a lease sized for its longest tool call
(``SUB_AGENT_LEASE_SECONDS``), and when that lease is lost with no attempt
left while the worker may still be running, the task is left running, marked
orphaned, and keeps the ending its worker writes; the orphan sweep fails it
only once the worker's own time is over, naming what was last heard of it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import attempt_charges, sub_agent
from app.services.orchestration_adapters.sub_agent import SubAgentAdapter
from app.services.orchestration_engine import LEASE_EXPIRY_PAYLOAD_KEY
from app.services.orchestration_reconciler import _lease_seconds_of

TASK = str(uuid.uuid4())


# ---------------------------------------------------------------------------
# The lease
# ---------------------------------------------------------------------------


def test_a_background_task_runs_under_its_own_lease(monkeypatch):
    """Three times the longest tool call by default, read live, through the
    reconciler's adapter lease contract."""
    monkeypatch.setattr(sub_agent.settings, "sub_agent_lease_seconds", 900)
    assert SubAgentAdapter().lease_seconds == 900
    assert _lease_seconds_of(SubAgentAdapter()) == 900

    monkeypatch.setattr(sub_agent.settings, "sub_agent_lease_seconds", 1200)
    assert _lease_seconds_of(SubAgentAdapter()) == 1200, "read at each claim, not at import"


@pytest.mark.parametrize("value", [0, -1, None, "900"])
def test_a_lease_that_is_not_a_positive_number_keeps_the_engines(monkeypatch, value):
    monkeypatch.setattr(sub_agent.settings, "sub_agent_lease_seconds", value)
    assert SubAgentAdapter().lease_seconds is None
    assert _lease_seconds_of(SubAgentAdapter()) is None


def test_the_default_lease_covers_the_longest_wait_three_times():
    from app.core.config import Settings
    from app.tools.wait_policy import DEFAULT_MAXIMUM_WAIT_TIME

    assert Settings.model_fields["sub_agent_lease_seconds"].default >= 3 * DEFAULT_MAXIMUM_WAIT_TIME


# ---------------------------------------------------------------------------
# The dead letter of a lease that lapsed
# ---------------------------------------------------------------------------


def _task(status="running", *, started_at=None, metadata=None):
    return SimpleNamespace(
        id=uuid.UUID(TASK),
        status=status,
        error=None,
        completed_at=None,
        started_at=started_at,
        conversation_id=None,
        target_agent_id=uuid.uuid4(),
        metadata_json=metadata,
    )


def _session(task):
    return SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: task)),
        commit=AsyncMock(),
    )


def _lease_expired_execution(*, attempt=3, deadline_at=None, silent=61):
    now = datetime.now(UTC)
    heard = now - timedelta(seconds=silent)
    return SimpleNamespace(
        payload={
            "task_id": TASK,
            LEASE_EXPIRY_PAYLOAD_KEY: {
                "at": now.isoformat(),
                "last_heartbeat_at": heard.isoformat(),
                "silent_seconds": silent,
                "owner": "worker-a",
                "attempt": attempt,
            },
        },
        last_error=f"Lease expired, last heartbeat {silent} s ago, with no attempt left.",
        attempt_count=attempt,
        deadline_at=deadline_at,
    )


async def test_a_lost_lease_leaves_a_running_task_to_its_worker(monkeypatch):
    """The task stays running, marked orphaned with what the engine last heard;
    nothing is credited back and nobody is woken."""
    credit = AsyncMock(return_value=0)
    monkeypatch.setattr(attempt_charges, "credit_abandoned_task", credit)
    delegation = AsyncMock()
    monkeypatch.setattr(sub_agent, "_update_task_delegation", delegation)
    wake = AsyncMock()
    monkeypatch.setattr(sub_agent, "_schedule_parent_wake_if_needed", wake)
    deadline = datetime.now(UTC) + timedelta(minutes=10)
    task = _task("running", metadata={"parent_run_id": "r"})
    session = _session(task)

    await SubAgentAdapter().on_dead_letter(
        SimpleNamespace(db=session), _lease_expired_execution(attempt=3, deadline_at=deadline)
    )

    assert task.status == "running"
    assert task.error is None and task.completed_at is None
    record = sub_agent.orphan_record(task)
    assert record is not None
    assert record["attempt"] == 3 and record["owner"] == "worker-a" and record["silent_seconds"] == 61
    assert record["execution_error"].startswith("Lease expired")
    assert task.metadata_json["parent_run_id"] == "r", "the other metadata is kept"
    assert sub_agent.orphan_fail_after(record) == deadline + timedelta(
        seconds=sub_agent.settings.orchestration_grace_seconds
    )
    credit.assert_not_awaited()
    delegation.assert_not_awaited()
    wake.assert_not_awaited()
    session.commit.assert_awaited()


async def test_a_task_with_no_deadline_is_left_to_its_worker_for_the_job_timeout(monkeypatch):
    monkeypatch.setattr(attempt_charges, "credit_abandoned_task", AsyncMock(return_value=0))
    monkeypatch.setattr(sub_agent, "_update_task_delegation", AsyncMock())
    monkeypatch.setattr(sub_agent.settings, "worker_job_timeout", 600)
    monkeypatch.setattr(sub_agent.settings, "orchestration_grace_seconds", 120)
    started = datetime.now(UTC) - timedelta(minutes=5)
    task = _task("running", started_at=started)

    await SubAgentAdapter().on_dead_letter(SimpleNamespace(db=_session(task)), _lease_expired_execution())

    assert task.status == "running"
    assert sub_agent.orphan_fail_after(sub_agent.orphan_record(task)) == started + timedelta(seconds=720)


@pytest.mark.parametrize("status", ["pending", "failed"])
async def test_only_a_running_task_is_left_to_its_worker(monkeypatch, status):
    """A pending task has no worker; a failed one ended. Both settle as before."""
    credit = AsyncMock(return_value=0)
    monkeypatch.setattr(attempt_charges, "credit_abandoned_task", credit)
    monkeypatch.setattr(sub_agent, "_update_task_delegation", AsyncMock())
    task = _task(status)

    await SubAgentAdapter().on_dead_letter(SimpleNamespace(db=_session(task)), _lease_expired_execution())

    assert task.status == "failed"
    assert sub_agent.orphan_record(task) is None
    credit.assert_awaited_once()


async def test_a_deadline_blown_still_fails_the_task_at_once(monkeypatch):
    """No lease expiry on the execution: the engine knows the work is over."""
    credit = AsyncMock(return_value=0)
    monkeypatch.setattr(attempt_charges, "credit_abandoned_task", credit)
    monkeypatch.setattr(sub_agent, "_update_task_delegation", AsyncMock())
    task = _task("running")
    execution = SimpleNamespace(
        payload={"task_id": TASK}, last_error="Deadline exceeded.", attempt_count=1, deadline_at=None
    )

    await SubAgentAdapter().on_dead_letter(SimpleNamespace(db=_session(task)), execution)

    assert task.status == "failed" and task.error == "Deadline exceeded."
    assert sub_agent.orphan_record(task) is None
    credit.assert_awaited_once()


async def test_an_expiry_of_an_earlier_attempt_is_not_this_ones(monkeypatch):
    """The re-run that followed it ended another way: fail as before."""
    monkeypatch.setattr(attempt_charges, "credit_abandoned_task", AsyncMock(return_value=0))
    monkeypatch.setattr(sub_agent, "_update_task_delegation", AsyncMock())
    task = _task("running")
    execution = _lease_expired_execution(attempt=2)
    execution.attempt_count = 3

    await SubAgentAdapter().on_dead_letter(SimpleNamespace(db=_session(task)), execution)

    assert task.status == "failed"
    assert sub_agent.orphan_record(task) is None


# ---------------------------------------------------------------------------
# The orphan sweep
# ---------------------------------------------------------------------------


class _QueueResult:
    def __init__(self, *, scalars_all=None, rows=None):
        self._scalars_all = scalars_all
        self._rows = rows

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._scalars_all or []))

    def all(self):
        return list(self._rows or [])


class _QueueDB:
    def __init__(self, results):
        self._q = list(results)

    async def execute(self, *_args, **_kwargs):
        return self._q.pop(0)


def _orphaned(*, fail_after: datetime, attempt=3):
    heard = datetime.now(UTC) - timedelta(minutes=2)
    return _task(
        "running",
        started_at=datetime.now(UTC) - timedelta(minutes=20),
        metadata={
            sub_agent.ORPHANED_KEY: {
                "at": datetime.now(UTC).isoformat(),
                "fail_after": fail_after.isoformat(),
                "attempt": attempt,
                "owner": "worker-a",
                "last_heartbeat_at": heard.isoformat(),
                "silent_seconds": 61,
            }
        },
    )


async def _sweep(tasks, monkeypatch):
    credit = AsyncMock(return_value=0)
    monkeypatch.setattr(attempt_charges, "credit_abandoned_task", credit)
    db = _QueueDB(
        [
            _QueueResult(scalars_all=tasks),
            _QueueResult(rows=[(f"sub_agent_task:{t.id}", "dead_letter") for t in tasks]),
        ]
    )
    with patch.object(sub_agent, "_update_task_delegation", new=AsyncMock()):
        recovered = await sub_agent.recover_orphaned_sub_agent_tasks(db)
    return recovered, credit


async def test_the_sweep_waits_for_the_orphaned_worker(monkeypatch):
    """A dead-lettered execution whose task is orphaned and still within its
    worker's time is left alone: the worker writes the ending, and the caller
    reads it as completed."""
    task = _orphaned(fail_after=datetime.now(UTC) + timedelta(minutes=5))

    recovered, credit = await _sweep([task], monkeypatch)

    assert recovered == 0
    assert task.status == "running"
    credit.assert_not_awaited()


async def test_the_sweep_fails_the_orphaned_task_once_its_workers_time_is_over(monkeypatch):
    """Failed naming what was last heard of the attempt, and its charge credited back."""
    task = _orphaned(fail_after=datetime.now(UTC) - timedelta(seconds=1), attempt=3)

    recovered, credit = await _sweep([task], monkeypatch)

    assert recovered == 1
    assert task.status == "failed" and task.completed_at is not None
    assert "lost its lease on attempt 3" in task.error
    assert "last heartbeat 61 s before" in task.error
    assert "never reported an ending" in task.error
    credit.assert_awaited_once()
    assert credit.await_args.args == (TASK,)
    assert credit.await_args.kwargs["attempts"] == 3


async def test_a_dead_lettered_task_without_the_marker_is_failed_as_before(monkeypatch):
    task = _task("running", started_at=datetime.now(UTC), metadata={})

    recovered, credit = await _sweep([task], monkeypatch)

    assert recovered == 1
    assert task.status == "failed"
    assert task.error.startswith("Background task reconciled to failed")
    credit.assert_not_awaited()


async def test_an_unreadable_marker_never_holds_the_sweep_back(monkeypatch):
    task = _task("running", started_at=datetime.now(UTC), metadata={sub_agent.ORPHANED_KEY: {"fail_after": "soon"}})

    recovered, _credit = await _sweep([task], monkeypatch)

    assert recovered == 1
    assert task.status == "failed"


# ---------------------------------------------------------------------------
# The worker's ending stands
# ---------------------------------------------------------------------------


def test_an_orphaned_task_still_takes_its_workers_ending():
    """``_ended_elsewhere``: an orphaned, running, unreplaced task has no ending
    yet, so the worker that comes back writes its own."""
    task = _orphaned(fail_after=datetime.now(UTC) + timedelta(minutes=5), attempt=3)
    assert sub_agent._ended_elsewhere(task, (0, 3)) is None
