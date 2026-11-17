"""A recording relaunched after its worker went is not failed on its first deadline (#9885).

- the worker that claims a relaunched take restarts its deadline to a whole take's from that claim
  (``VideoTakeAdapter.execute`` -> ``_restart_deadline``), the running task's timeout following it;
- a relaunched take dead-lettered on its deadline with an attempt left runs once more
  (``relaunch_after_deadline``): recorded again from its script when its worker held it (its resume stalled), first
  in the lane again with its resume kept when no worker took it; never a second time, never a first launch, never
  with no attempt left;
- ``sweep_deadlines`` names the worker that held a row it dead-letters.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import orchestration_engine
from app.services.orchestration_adapters import video_studio as adapters
from app.services.video_studio import jobs, runners

_TAKE = adapters.VideoTakeAdapter.name


def _execution(*, last_error: str, attempt_count: int = 1, payload: dict | None = None) -> SimpleNamespace:
    take_id = str(uuid.uuid4())
    base = {"take_id": take_id, adapters.HANDED_OVER_KEY: {"worker": "w-old", "version": "1"}}
    return SimpleNamespace(
        id=uuid.uuid4(),
        adapter=_TAKE,
        payload={**base, **(payload or {})},
        last_error=last_error,
        attempt_count=attempt_count,
        max_attempts=2,
        state="dead_letter",
        deadline_at=datetime.now(UTC) - timedelta(seconds=5),
    )


class _Session:
    async def __aenter__(self):
        return SimpleNamespace(commit=AsyncMock())

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def relaunch(monkeypatch):
    calls: dict = {"dropped": [], "marked": [], "relaunched": []}

    async def load(kind, job_id):
        return SimpleNamespace(status="running", progress=83, stage="Pacing its scrolls and transitions at 60 fps")

    async def mark_if_open(kind, job_id, **values):
        calls["marked"].append(values)
        return True

    async def dropped(take_id):
        calls["dropped"].append(take_id)

    async def relaunch_dead_lettered(db, **kwargs):
        calls["relaunched"].append(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(jobs, "load", load)
    monkeypatch.setattr(jobs, "mark_if_open", mark_if_open)
    monkeypatch.setattr(runners, "resume_point_dropped", dropped)
    monkeypatch.setattr(orchestration_engine, "relaunch_dead_lettered", relaunch_dead_lettered)
    monkeypatch.setattr(orchestration_engine, "notify_orchestration_wake", AsyncMock())
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session())
    return calls


async def test_a_resume_stalled_under_its_worker_is_recorded_again_from_its_script(relaunch):
    execution = _execution(last_error="Deadline exceeded at 2026-11-07T22:33:00+00:00 (held by worker-b).")
    assert await adapters.relaunch_after_deadline(execution) is True
    assert relaunch["dropped"] == [execution.payload["take_id"]]
    assert relaunch["marked"][0]["stage"] == adapters.TAKE_DEADLINE_RELAUNCH_STAGE
    (call,) = relaunch["relaunched"]
    record = call["payload_update"][adapters.DEADLINE_RELAUNCH_KEY]
    assert record["stalled"] is True and record["progress"] == 83
    # The reason names the stage it stalled at, the worker that held it and the attempts used.
    assert "held by worker-b" in call["reason"] and "Pacing its scrolls" in call["reason"]
    assert "1 of 2 attempts used" in call["reason"]
    assert call["deadline_at"] > datetime.now(UTC) + timedelta(minutes=1)


async def test_a_take_no_worker_took_in_time_keeps_its_resume(relaunch):
    execution = _execution(last_error="Deadline exceeded at 2026-11-07T22:33:00+00:00.")
    assert await adapters.relaunch_after_deadline(execution) is True
    assert relaunch["dropped"] == []
    assert relaunch["marked"][0]["stage"] == adapters.TAKE_DEADLINE_REQUEUED_STAGE


@pytest.mark.parametrize(
    ("last_error", "attempt_count", "payload"),
    [
        # Run once more already: never a second time.
        ("Deadline exceeded.", 1, {adapters.DEADLINE_RELAUNCH_KEY: {"at": "x"}}),
        # No attempt left.
        ("Deadline exceeded.", 2, None),
        # Not a deadline: the lease or a worker gone with no attempt left fails as before.
        ("Its worker is gone: no attempt left.", 1, None),
    ],
)
async def test_it_runs_once_more_only_on_a_deadline_with_an_attempt_left(relaunch, last_error, attempt_count, payload):
    execution = _execution(last_error=last_error, attempt_count=attempt_count, payload=payload)
    assert await adapters.relaunch_after_deadline(execution) is False
    assert relaunch["relaunched"] == []


async def test_a_first_launch_on_its_deadline_is_failed_as_before(relaunch):
    execution = _execution(last_error="Deadline exceeded.")
    execution.payload.pop(adapters.HANDED_OVER_KEY)
    assert await adapters.relaunch_after_deadline(execution) is False


async def test_the_claim_of_a_relaunched_take_restarts_its_deadline(monkeypatch):
    moved: list = []
    followed: list = []

    async def extend_deadline(db, *, execution_id, worker_id, deadline_at):
        moved.append((worker_id, deadline_at))
        return deadline_at

    monkeypatch.setattr(orchestration_engine, "extend_deadline", extend_deadline)
    monkeypatch.setattr(
        "app.services.orchestration_reconciler.follow_deadline", lambda eid, at: followed.append((eid, at))
    )
    execution = SimpleNamespace(id=uuid.uuid4(), deadline_at=datetime.now(UTC) + timedelta(seconds=30))
    ctx = SimpleNamespace(db=SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock()), worker_id="worker-b")
    await adapters._restart_deadline(ctx, execution)
    assert moved and moved[0][0] == "worker-b"
    assert execution.deadline_at == moved[0][1] > datetime.now(UTC) + timedelta(minutes=1)
    assert followed == [(execution.id, execution.deadline_at)]


async def test_a_deadline_not_restarted_leaves_the_take_running(monkeypatch):
    async def lost(db, **kwargs):
        raise orchestration_engine.LeaseLostError("not this worker's")

    monkeypatch.setattr(orchestration_engine, "extend_deadline", lost)
    before = datetime.now(UTC) + timedelta(seconds=30)
    execution = SimpleNamespace(id=uuid.uuid4(), deadline_at=before)
    ctx = SimpleNamespace(db=SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock()), worker_id="worker-b")
    await adapters._restart_deadline(ctx, execution)
    assert execution.deadline_at == before


async def test_sweep_deadlines_names_the_worker_that_held_the_row(monkeypatch):
    now = datetime.now(UTC)
    held = SimpleNamespace(
        state="running",
        deadline_at=now - timedelta(seconds=1),
        lease_owner="worker-b",
        lease_expires_at=now + timedelta(seconds=60),
        payload={},
        adapter_state=None,
    )
    queued = SimpleNamespace(
        state="pending",
        deadline_at=now - timedelta(seconds=1),
        lease_owner=None,
        lease_expires_at=None,
        payload={},
        adapter_state=None,
    )
    result = MagicMock()
    result.scalars.return_value.all.return_value = [held, queued]
    db = SimpleNamespace(execute=AsyncMock(return_value=result), flush=AsyncMock())
    monkeypatch.setattr(orchestration_engine, "hold_record", lambda row: None)
    monkeypatch.setattr(orchestration_engine, "hold_started_at", lambda row: None)
    dead = await orchestration_engine.sweep_deadlines(db, limit=10)
    assert dead == [held, queued]
    assert held.last_error.startswith("Deadline exceeded at ") and "(held by worker-b)" in held.last_error
    assert "held by" not in queued.last_error and held.lease_owner is None
