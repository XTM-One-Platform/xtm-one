"""A recording no worker claims is said so, and is not failed on its deadline while it never started (#10662).

Three short takes stayed "place 1, 0 recordings being made now, nothing queued ahead" during a run of rollouts and
were dead-lettered on their deadline at 0%, never claimed. What is proven here:

* a queued take no running worker claims says so (``recording_lane.unserved_words``), in its line and its dict,
  apart from a lane that is only idle and from a lane refusing its GL (#8054);
* a take never claimed and caught by its deadline in the queue goes back first in the lane with a whole take's
  deadline, its attempts untouched, at most ``MAX_UNCLAIMED_REQUEUES`` times;
* past the bound it fails saying it never started and whether a worker served the lane;
* a take a worker claimed, held, measured or relaunched (#9815, #9885, #9889) is left to the rules it had.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import orchestration_engine, worker_presence
from app.services.orchestration_adapters import video_studio as adapters
from app.services.video_studio import jobs, recording_lane


def _present(monkeypatch, records):
    async def present(*, at_most: int = 256):
        return records

    monkeypatch.setattr(worker_presence, "present", present)


def test_a_lane_with_no_worker_running_says_so(monkeypatch):
    _present(monkeypatch, [])
    assert asyncio.run(recording_lane.unserved_words()) == "no worker is running now"
    draining = {"worker_id": "w-old", "state": worker_presence.STATE_DRAINING, "claims": [jobs.TAKE_ADAPTER]}
    _present(monkeypatch, [draining])
    assert asyncio.run(recording_lane.unserved_words()) == "no worker is running now (1 draining)"


def test_running_workers_that_claim_no_take_are_named_by_count(monkeypatch):
    other = {"worker_id": "w-1", "state": worker_presence.STATE_RUNNING, "claims": ["video_studio_render"]}
    _present(monkeypatch, [other, {**other, "worker_id": "w-2"}])
    assert asyncio.run(recording_lane.unserved_words()) == "none of the 2 running workers takes recordings"


def test_a_served_lane_or_an_unread_redis_says_nothing(monkeypatch):
    claiming = {"worker_id": "w-3", "state": worker_presence.STATE_RUNNING, "claims": [jobs.TAKE_ADAPTER]}
    _present(monkeypatch, [claiming])
    assert asyncio.run(recording_lane.unserved_words()) is None
    _present(monkeypatch, None)
    assert asyncio.run(recording_lane.unserved_words()) is None


def test_the_queue_answer_of_an_unserved_lane_says_it_and_how_it_ends():
    place = jobs.QueuePlace(
        running=0, ahead=0, per_worker=1, lane="recording", recordings=(), unserved="no worker is running now"
    )
    line = recording_lane.line(place)
    assert line.startswith(
        "waiting for a worker in the recording lane, place 1 (nothing queued ahead of it), but no worker takes "
        "recordings now: no worker is running now."
    )
    assert "instead of failing" in line
    assert place.as_dict()["recording_lane_unserved"] == "no worker is running now"
    idle = jobs.QueuePlace(running=0, ahead=0, per_worker=1, lane="recording", recordings=())
    assert "recording_lane_unserved" not in idle.as_dict()
    assert recording_lane.line(idle).startswith("waiting for a worker in the recording lane, place 1: 0 recordings")


def _execution(*, last_error: str = "Deadline exceeded at 2026-11-10T03:52:29+00:00.", attempt_count: int = 0, payload=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        adapter=jobs.TAKE_ADAPTER,
        payload={"take_id": str(uuid.uuid4()), **(payload or {})},
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
def requeue(monkeypatch):
    calls: dict = {"marked": [], "relaunched": []}

    async def load(kind, job_id):
        return SimpleNamespace(status="pending", progress=0, stage=None)

    async def mark_if_open(kind, job_id, **values):
        calls["marked"].append(values)
        return True

    async def relaunch_dead_lettered(db, **kwargs):
        calls["relaunched"].append(kwargs)
        return SimpleNamespace(**kwargs)

    async def unserved():
        return "no worker is running now"

    monkeypatch.setattr(jobs, "load", load)
    monkeypatch.setattr(jobs, "mark_if_open", mark_if_open)
    monkeypatch.setattr(recording_lane, "unserved_words", unserved)
    monkeypatch.setattr(orchestration_engine, "relaunch_dead_lettered", relaunch_dead_lettered)
    monkeypatch.setattr(orchestration_engine, "notify_orchestration_wake", AsyncMock())
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session())
    return calls


async def test_a_take_never_claimed_goes_back_first_in_the_lane_with_a_whole_take_s_time(requeue):
    execution = _execution()
    assert await adapters.requeue_unclaimed_after_deadline(execution) is True
    assert requeue["marked"][0]["stage"] == adapters.TAKE_UNCLAIMED_REQUEUED_STAGE
    (call,) = requeue["relaunched"]
    assert call["payload_update"] == {adapters.UNCLAIMED_REQUEUES_KEY: 1}
    assert call["deadline_at"] > datetime.now(UTC) + timedelta(minutes=1)
    assert "no worker took recordings: no worker is running now" in call["reason"]
    assert f"1 of {adapters.MAX_UNCLAIMED_REQUEUES}" in call["reason"]


async def test_past_the_bound_it_fails_saying_it_never_started(requeue):
    bound = adapters.MAX_UNCLAIMED_REQUEUES
    execution = _execution(payload={adapters.UNCLAIMED_REQUEUES_KEY: bound})
    assert await adapters.requeue_unclaimed_after_deadline(execution) is False
    assert requeue["relaunched"] == []
    assert execution.last_error.startswith("Deadline exceeded before it started: no worker took it")
    assert f"back in the lane {bound} times" in execution.last_error
    assert "no worker is running now" in execution.last_error


@pytest.mark.parametrize(
    ("last_error", "attempt_count", "payload"),
    [
        # Held by a worker when its deadline came: it was claimed.
        ("Deadline exceeded at 2026-11-10T03:52:29+00:00 (held by worker-b).", 0, None),
        # An attempt spent: a worker ran it (#9815 accounting).
        ("Deadline exceeded.", 1, None),
        # Waiting for its worker's load: a worker claimed and measured it.
        ("Deadline exceeded.", 0, {recording_lane.WAIT_KEY: {"since": "x"}}),
        # Relaunched after its worker went: the rule of #9885 / #9889.
        ("Deadline exceeded.", 0, {adapters.HANDED_OVER_KEY: {"worker": "w-old"}}),
        # Not a deadline.
        ("Cancelled.", 0, None),
    ],
)
async def test_a_take_a_worker_took_keeps_the_rules_it_had(requeue, last_error, attempt_count, payload):
    execution = _execution(last_error=last_error, attempt_count=attempt_count, payload=payload)
    before = execution.last_error
    assert await adapters.requeue_unclaimed_after_deadline(execution) is False
    assert requeue["relaunched"] == [] and execution.last_error == before
