"""A recording lost to a worker rollout keeps its attempts and waits for a worker that sustains its fps (#9812).

- a take whose worker was killed by a rollout during its encode (no stop word, no out-of-memory sign) is taken back
  without spending an attempt, as a render is (``VideoTakeAdapter.gives_back_silent_losses``): two losses in a run of
  deploys never exhaust its ``max_attempts=2``;
- a relaunched take (handed back by its stopping worker, or given its attempt back) whose capture pre-flight falls
  short of its fps on its new worker waits in the recording lane (``recording_lane.Wait.relaunch``) instead of being
  refused, up to ``RELAUNCH_MAX_WAIT``; a first launch is refused as before.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.services import orchestration_engine, orchestration_reconciler
from app.services.orchestration_adapters import video_studio as adapters
from app.services.orchestration_engine import _take_back, infra_relaunches
from app.services.video_studio import capture, capture_rate, recording_lane
from app.services.video_studio.capture_rate import CapturePlan
from app.services.video_studio.spec import parse_take_script, validate_take_script

_TAKE = adapters.VideoTakeAdapter.name


@pytest.fixture(autouse=True)
def _release(monkeypatch):
    monkeypatch.setattr(settings, "app_version", "1.0.1-new")
    monkeypatch.setattr(orchestration_engine, "head_of_queue", AsyncMock(return_value=datetime.now(UTC)))
    monkeypatch.setattr(orchestration_engine, "last_heard_at", lambda row: None)


def _rollout_killed() -> dict:
    """A worker of the release before, gone without a stop word, well under its memory limit."""
    return {
        "worker_id": "worker-x",
        "host": "pod-x",
        "boot_id": "another-boot",
        "state": "running",
        "version": "1.0.0-old",
        "memory": 1 << 30,
        "memory_limit": 4 << 30,
    }


def _row(*, attempt_count: int = 1, payload=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        adapter=_TAKE,
        lease_owner="worker-x",
        lease_expires_at=datetime.now(UTC),
        adapter_state=None,
        updated_at=None,
        payload=payload or {"take_id": str(uuid.uuid4())},
        attempt_count=attempt_count,
        max_attempts=2,
        state="running",
        run_at=None,
        last_error=None,
    )


async def test_two_rollout_kills_during_the_encode_leave_the_take_its_attempts():
    row = _row(attempt_count=1)
    given_back = orchestration_reconciler.silent_loss_given_back_of([adapters.VideoTakeAdapter()])
    for _ in range(2):
        dead = await _take_back(
            AsyncMock(),
            row,
            datetime.now(UTC),
            why="Its worker went",
            head=True,
            owner_record=_rollout_killed(),
            silent_loss_given_back=given_back,
        )
        assert dead is False and row.state == "pending"
        row.attempt_count += 1
        row.state = "running"
    # Neither loss spent an attempt: the take stands at its first, two relaunches counted apart.
    assert row.attempt_count == 1
    assert infra_relaunches(row) == 2
    assert adapters.relaunched_take(row) is True


def test_a_first_launch_is_not_a_relaunch():
    assert adapters.relaunched_take(_row()) is False
    assert adapters.relaunched_take(None) is False
    handed = _row(payload={"take_id": "t", adapters.HANDED_OVER_KEY: {"worker": "w", "version": "1"}})
    assert adapters.relaunched_take(handed) is True


def _script():
    return parse_take_script(
        validate_take_script(
            {"start_url": "https://app.example.com/", "fps": 60, "steps": [{"action": "wait", "seconds": 1}]}
        )
    )


def _refusing(monkeypatch):
    async def plan(script, viewport, ratio, *, beside=None, may_wait=False):
        return CapturePlan(
            ratio, 2.0, 2.0, {"fps": 28.0, "size": [2880, 1620]}, refused="28 fps at 2880x1620, under its 60 fps."
        )

    monkeypatch.setattr(capture_rate, "plan", plan)


def _slot(*, relaunched: bool, waited=None, may_wait: bool = True) -> recording_lane.Slot:
    return recording_lane.Slot("worker-2", uuid.uuid4(), waited=waited, may_wait=may_wait, relaunched=relaunched)


def test_a_relaunch_short_of_its_fps_waits_in_the_lane_instead_of_failing(monkeypatch):
    _refusing(monkeypatch)
    lane = _slot(relaunched=True)
    with pytest.raises(capture.WorkerBusyError) as raised:
        asyncio.run(capture.preflight(_script(), lane=lane))
    assert raised.value.relaunch is True
    wait = lane.waiting(raised.value.plan, raised.value.wanted, datetime.now(UTC), relaunch=raised.value.relaunch)
    assert wait.relaunch is True
    assert "Relaunched after its worker was stopped" in wait.stage()
    # Kept on its execution and read back the same, measured again on its next claim (never held without a load).
    again = recording_lane.Wait.of(wait.as_dict())
    assert again is not None and again.relaunch is True
    assert not recording_lane.held(again, "worker-2", recording_lane.Load(), datetime.now(UTC))
    result = adapters.VideoTakeAdapter._waits(wait.stage(), wait.as_dict())
    assert result.backoff_seconds == recording_lane.RELAUNCH_POLL_SECONDS


def test_a_first_launch_short_of_its_fps_is_refused_as_before(monkeypatch):
    _refusing(monkeypatch)
    with pytest.raises(capture.TakeError, match="under its 60 fps"):
        asyncio.run(capture.preflight(_script(), lane=_slot(relaunched=False)))


def test_a_relaunch_that_waited_its_longest_is_refused(monkeypatch):
    _refusing(monkeypatch)
    since = datetime.now(UTC) - recording_lane.RELAUNCH_MAX_WAIT - timedelta(minutes=1)
    waited = recording_lane.Wait(
        since=since,
        worker="worker-2",
        beside=recording_lane.Load(),
        fps=28.0,
        size=(2880, 1620),
        wanted=60.0,
        relaunch=True,
    )
    assert waited.expired(datetime.now(UTC))
    lane = _slot(relaunched=True, waited=waited, may_wait=not waited.expired(datetime.now(UTC)))
    with pytest.raises(capture.TakeError, match="relaunched on after its worker was stopped"):
        asyncio.run(capture.preflight(_script(), lane=lane))
