"""A background task killed again and again by one rollout is resumed every time (#12386).

- a worker gone without a word whose release is older than the newest release running now is the rollout's kill,
  whoever reclaims it: a pod of the outgoing release, or one of the new release that came up after the kill;
- a task taken back from such a kill counts on neither the streak of losses without a word nor the graceful
  streak: N > 3 kills in a row with no progress still resume it;
- three losses without a word on a stable build (the newest release itself) still end at the cap;
- a worker of the outgoing release claims no resumable execution while one of the newest release runs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.services import orchestration_engine, worker_presence
from app.services.background_checkpoint import (
    MAX_GRACEFUL_RESUMES,
    MAX_GRACEFUL_STREAK,
    MAX_RESUMES,
    RESUME_CAP_REACHED,
    ROLLOUT_KILL_KEY,
    graceful_streak,
    handed_back,
    next_graceful_streak,
    note_resume,
    record_resume,
    resume_counts,
    resume_refused,
)
from app.services.orchestration_adapters.sub_agent import _killed_by_rollout, _taken_back_on_its_word
from app.services.orchestration_engine import _take_back

OLD, NEW = "1.0.0-old", "1.0.1-new"
NOW = datetime.now(UTC)


def _record(**fields) -> dict:
    return {
        "worker_id": "worker-old",
        "host": "pod-old",
        "boot_id": "another-boot",
        "state": "running",
        "lapsed": True,
        "version": OLD,
        "memory": int(2.1 * (1 << 30)),
        "memory_limit": 16 << 30,
        "at": (NOW - timedelta(seconds=30)).isoformat(),
        "process_started_at": (NOW - timedelta(hours=2)).isoformat(),
        **fields,
    }


def _running(version: str, started: datetime, **fields) -> dict:
    return {
        "worker_id": f"w-{version}-{started.timestamp()}",
        "state": "running",
        "version": version,
        "process_started_at": started.isoformat(),
        **fields,
    }


@pytest.fixture(autouse=True)
def _release(monkeypatch):
    monkeypatch.setattr(orchestration_engine, "head_of_queue", AsyncMock(return_value=NOW))
    monkeypatch.setattr(orchestration_engine, "last_heard_at", lambda row: None)


# 1. The newest release, read from the workers there now.


def test_the_newest_release_is_the_one_whose_process_came_up_last(monkeypatch):
    monkeypatch.setattr(settings, "app_version", OLD)
    monkeypatch.setattr(worker_presence, "_PROCESS_STARTED_AT", NOW - timedelta(hours=2))
    found = [_running(OLD, NOW - timedelta(hours=3)), _running(NEW, NOW - timedelta(minutes=1))]
    assert worker_presence.newest_release(found) == NEW
    assert worker_presence.newest_release([]) == OLD, "alone, this worker's own release"
    # A record of a release that never said when it started is older than any that did.
    assert worker_presence.newest_release([{"state": "running", "version": NEW}]) == OLD


@pytest.mark.parametrize("reclaimer", [OLD, NEW])
def test_a_kill_of_an_older_release_is_the_rollouts_whoever_reclaims_it(monkeypatch, reclaimer):
    """The bug: replaced_by_deploy alone reads it against the reclaimer's own release and start."""
    monkeypatch.setattr(settings, "app_version", reclaimer)
    # A reclaimer of the outgoing release, or a new pod that came up after the kill: both miss it today.
    monkeypatch.setattr(worker_presence, "_PROCESS_STARTED_AT", NOW)
    record = _record()
    assert not worker_presence.replaced_by_deploy(record)
    assert worker_presence.killed_by_rollout(record, NEW)


@pytest.mark.parametrize(
    "fields",
    [
        {"version": NEW},  # the newest release itself: a crash on a stable build
        {"memory": int(15.5 * (1 << 30))},  # at its memory limit: the out-of-memory killer
        {"memory": None},  # no telemetry: nothing rules the out-of-memory killer out
        {"state": "draining"},  # said so: already a stop on its word
        {"version": None},
    ],
)
def test_a_crash_or_an_oom_is_never_a_rollout_kill(fields):
    assert not worker_presence.killed_by_rollout(_record(**fields), NEW)


def test_no_newest_release_known_reads_no_rollout_kill():
    assert not worker_presence.killed_by_rollout(_record(), None)


async def test_gone_marks_a_rollout_kill_against_the_newest_release(monkeypatch):
    monkeypatch.setattr(settings, "app_version", OLD)
    monkeypatch.setattr(worker_presence, "_PROCESS_STARTED_AT", NOW)
    killed, crashed = _record(), _record(worker_id="worker-new-crashed", version=NEW)
    monkeypatch.setattr(
        worker_presence, "records", AsyncMock(return_value={"worker-old": killed, "worker-new-crashed": crashed})
    )
    monkeypatch.setattr(worker_presence, "present", AsyncMock(return_value=[_running(NEW, NOW + timedelta(seconds=1))]))

    found = await worker_presence.gone(["worker-old", "worker-new-crashed"], me="worker-reclaimer")

    assert found["worker-old"][worker_presence.REPLACED_BY_DEPLOY] is True
    assert worker_presence.REPLACED_BY_DEPLOY not in found["worker-new-crashed"]


# 2. The take-back and the resume bound.


def _row(*, attempt_count: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        adapter="sub_agent",
        lease_owner="worker-old",
        lease_expires_at=NOW,
        adapter_state=None,
        updated_at=None,
        payload={},
        attempt_count=attempt_count,
        max_attempts=21,
        state="running",
        run_at=None,
        last_error=None,
    )


async def test_a_task_killed_more_than_three_times_by_a_rollout_is_resumed_every_time():
    metadata: dict = {}
    kills = MAX_RESUMES + 4
    for attempt in range(1, kills + 1):
        row = _row(attempt_count=attempt)
        record = {**_record(), worker_presence.REPLACED_BY_DEPLOY: True}
        dead = await _take_back(AsyncMock(), row, NOW, why="Its worker went", head=True, owner_record=record)
        assert dead is False and row.state == "pending", "the engine gives the attempt back"
        assert _taken_back_on_its_word(row) and _killed_by_rollout(row)
        # No progress at all between the kills: the task was killed before its first call each time.
        assert (
            resume_refused(
                metadata,
                attempt=row.attempt_count + 1,
                taken_back_on_its_word=True,
                completed_calls=0,
                killed_by_rollout=True,
            )
            is None
        ), f"kill {attempt} of one rollout is resumed"
        metadata = record_resume(
            metadata,
            attempt=row.attempt_count + 1,
            cause="killed by the rollout",
            replayed_calls=0,
            resumed_from=f"attempt-{attempt}",
            graceful=True,
            completed_calls=0,
            rollout_kill=True,
        )
    assert len(metadata["resumes"]) == kills


def test_rollout_kills_never_feed_the_graceful_streak_of_a_later_hand_back():
    """Review of #12392: several rollout kills with no progress, then a graceful hand-back of the same task."""
    task = SimpleNamespace(id=uuid.uuid4(), metadata_json={})
    replay = SimpleNamespace(replayed_calls=0, turn_id="attempt-1", completed_calls=0)
    for attempt in range(2, 2 + MAX_GRACEFUL_STREAK):
        assert (
            resume_refused(
                task.metadata_json,
                attempt=attempt,
                taken_back_on_its_word=True,
                completed_calls=0,
                killed_by_rollout=True,
            )
            is None
        )
        task.metadata_json = note_resume(
            task, replay=replay, attempt=attempt, taken_back_on_its_word=True, killed_by_rollout=True
        )
    assert all(r[ROLLOUT_KILL_KEY] is True for r in task.metadata_json["resumes"])
    assert graceful_streak(task.metadata_json) == 0, "rollout kills are outside the graceful streak"
    assert resume_counts(task.metadata_json)[1] == MAX_GRACEFUL_STREAK, "they still count on the graceful total"

    # Then the worker running the task is asked to stop (SIGTERM) and hands it back: a graceful resume.
    attempt = 2 + MAX_GRACEFUL_STREAK
    task.metadata_json = handed_back(task.metadata_json, attempt=attempt, signal="SIGTERM")
    assert next_graceful_streak(task.metadata_json, completed_calls=0) == 1
    assert resume_refused(task.metadata_json, attempt=attempt, completed_calls=0) is None
    task.metadata_json = note_resume(task, replay=replay, attempt=attempt)
    assert ROLLOUT_KILL_KEY not in task.metadata_json["resumes"][-1], "a hand-back keeps its streak"
    assert graceful_streak(task.metadata_json) == 1


def test_the_graceful_total_still_bounds_rollout_kills():
    metadata: dict = {}
    for attempt in range(MAX_GRACEFUL_RESUMES):
        metadata = record_resume(
            metadata,
            attempt=attempt,
            cause="killed",
            replayed_calls=0,
            resumed_from=None,
            graceful=True,
            completed_calls=0,
        )
    reason = resume_refused(metadata, taken_back_on_its_word=True, killed_by_rollout=True, completed_calls=0)
    assert reason is not None and reason.startswith(RESUME_CAP_REACHED)


def test_three_losses_without_a_word_on_a_stable_build_still_end_at_the_cap():
    metadata: dict = {}
    for attempt in range(2, 2 + MAX_RESUMES):
        assert resume_refused(metadata, attempt=attempt, completed_calls=0) is None
        metadata = record_resume(
            metadata, attempt=attempt, cause="lost", replayed_calls=0, resumed_from=None, completed_calls=0
        )
    reason = resume_refused(metadata, attempt=2 + MAX_RESUMES, completed_calls=0)
    assert reason is not None and reason.startswith(RESUME_CAP_REACHED)
    assert "were not a rollout's" in reason, "the cap names the cause: crash, not rollout"


async def test_a_crash_on_the_newest_release_is_not_given_the_rollout_bound():
    row = _row(attempt_count=2)
    dead = await _take_back(AsyncMock(), row, NOW, why="Its worker went", head=True, owner_record=_record(version=NEW))
    assert dead is False and not _killed_by_rollout(row) and not _taken_back_on_its_word(row)


# 3. A worker of the outgoing release claims no resumable work.


def test_an_outgoing_worker_leaves_resumable_work_to_the_incoming_release(monkeypatch):
    monkeypatch.setattr(settings, "app_version", OLD)
    monkeypatch.setattr(worker_presence, "_PROCESS_STARTED_AT", NOW - timedelta(hours=2))
    incoming = _running(NEW, NOW - timedelta(seconds=10), claims=["sub_agent"])
    assert worker_presence.outgoing([incoming], "sub_agent")
    assert not worker_presence.outgoing([{**incoming, "claims": ["video_render"]}], "sub_agent"), (
        "no incoming worker claims it: this one keeps claiming, nothing waits forever"
    )
    assert not worker_presence.outgoing([{**incoming, "state": "draining"}], "sub_agent")
    assert not worker_presence.outgoing([], "sub_agent")


def test_a_worker_of_the_newest_release_is_never_outgoing(monkeypatch):
    monkeypatch.setattr(settings, "app_version", NEW)
    monkeypatch.setattr(worker_presence, "_PROCESS_STARTED_AT", NOW - timedelta(seconds=10))
    assert not worker_presence.outgoing([_running(OLD, NOW - timedelta(hours=2), claims=["sub_agent"])], "sub_agent")
