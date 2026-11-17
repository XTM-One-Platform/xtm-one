"""A render relaunched by worker restarts or deploys keeps its attempts for real failures.

- a worker that said it was stopping, or that a deploy replaced, gives the attempt back and counts an
  infra relaunch apart (``payload.infra_relaunches``);
- a worker gone WITHOUT a stop request, with no out-of-memory sign, gives the attempt back on an adapter that
  keeps its progress across workers (``gives_back_silent_losses``: the render adapter), bounded by
  ``MAX_SILENT_LOSSES_GIVEN_BACK``; on any other adapter, or with an out-of-memory sign, it still spends it;
- ``get_video_jobs`` says the relaunches not counted;
- ``record_browser_demo`` asked twice the same way in one conversation records once (``same_take``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.services import orchestration_engine, orchestration_reconciler, worker_presence
from app.services.orchestration_adapters.video_studio import VideoPreviewAdapter, VideoRenderAdapter
from app.services.orchestration_engine import (
    INFRA_RELAUNCHES_KEY,
    LOSS_ROLLOUT_KILL,
    LOSS_SILENT,
    MAX_ROLLOUT_KILLS_GIVEN_BACK,
    MAX_SILENT_LOSSES_GIVEN_BACK,
    ROLLOUT_KILLS_KEY,
    SILENT_LOSSES_KEY,
    _take_back,
    infra_relaunches,
    rollout_kills,
)

_RENDER = VideoRenderAdapter.name


@pytest.fixture(autouse=True)
def _release(monkeypatch):
    monkeypatch.setattr(settings, "app_version", "1.0.1-new")
    monkeypatch.setattr(orchestration_engine, "head_of_queue", AsyncMock(return_value=datetime.now(UTC)))
    monkeypatch.setattr(orchestration_engine, "last_heard_at", lambda row: None)


def _silent(**fields) -> dict:
    """A worker of THIS release gone without a stop request, well under its memory limit."""
    return {
        "worker_id": "worker-x",
        "host": "pod-x",
        "boot_id": "another-boot",
        "state": "running",
        "version": "1.0.1-new",
        "memory": 1 << 30,
        "memory_limit": 4 << 30,
        **fields,
    }


def _row(*, adapter: str = _RENDER, attempt_count: int = 1, max_attempts: int = 3, payload=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        adapter=adapter,
        lease_owner="worker-x",
        lease_expires_at=datetime.now(UTC),
        adapter_state=None,
        updated_at=None,
        payload=payload or {},
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        state="running",
        run_at=None,
        last_error=None,
    )


def _given_back() -> object:
    return orchestration_reconciler.silent_loss_given_back_of([VideoRenderAdapter(), VideoPreviewAdapter()])


def test_only_the_whole_render_adapter_gives_back_silent_losses():
    assert VideoRenderAdapter.gives_back_silent_losses is True
    assert VideoPreviewAdapter.gives_back_silent_losses is True  # inherits: a preview resumes from its frames too
    from app.services.orchestration_adapters.video_studio import VideoTakeAdapter

    # A take resumes from its stored keyframes or records again from its script (#9812).
    assert VideoTakeAdapter.gives_back_silent_losses is True
    from app.services.orchestration_adapters.base import OrchestrationAdapter

    assert OrchestrationAdapter.gives_back_silent_losses is False


def test_out_of_memory_sign_reads_the_memory_share_and_never_a_stop():
    assert not worker_presence.out_of_memory_sign(_silent())
    assert worker_presence.out_of_memory_sign(_silent(memory=int(3.9 * (1 << 30))))
    assert not worker_presence.out_of_memory_sign(_silent(memory=int(3.9 * (1 << 30)), state="draining"))
    assert not worker_presence.out_of_memory_sign(None)


async def test_three_silent_worker_losses_leave_the_render_its_full_attempts():
    row = _row(attempt_count=1)
    for _ in range(3):
        dead = await _take_back(
            AsyncMock(),
            row,
            datetime.now(UTC),
            why="Its worker went",
            head=True,
            owner_record=_silent(),
            silent_loss_given_back=_given_back(),
        )
        assert dead is False and row.state == "pending"
        # The next claim counts an attempt again.
        row.attempt_count += 1
        row.state = "running"
    # Each loss gave its attempt back: the render stands at its first attempt, three relaunches counted apart.
    assert row.attempt_count == 1
    assert infra_relaunches(row) == 3


async def test_a_stop_on_its_word_is_counted_apart_too():
    row = _row(attempt_count=2)
    await _take_back(
        AsyncMock(),
        row,
        datetime.now(UTC),
        why="Its worker went",
        head=True,
        owner_record=_silent(state="draining"),
    )
    assert row.attempt_count == 1 and row.payload[INFRA_RELAUNCHES_KEY] == 1


async def test_a_silent_loss_with_an_out_of_memory_sign_still_spends_the_attempt():
    row = _row(attempt_count=3)
    dead = await _take_back(
        AsyncMock(),
        row,
        datetime.now(UTC),
        why="Its worker went",
        head=True,
        owner_record=_silent(memory=int(3.9 * (1 << 30))),
        silent_loss_given_back=_given_back(),
    )
    assert dead is True and row.state == "dead_letter" and infra_relaunches(row) == 0


async def test_another_adapter_still_spends_the_attempt_on_a_silent_loss():
    row = _row(adapter="sub_agent", attempt_count=2)
    await _take_back(
        AsyncMock(),
        row,
        datetime.now(UTC),
        why="Its worker went",
        head=True,
        owner_record=_silent(),
        silent_loss_given_back=_given_back(),
    )
    assert row.attempt_count == 2 and infra_relaunches(row) == 0


async def test_silent_losses_given_back_are_bounded():
    row = _row(
        attempt_count=3,
        payload={INFRA_RELAUNCHES_KEY: MAX_SILENT_LOSSES_GIVEN_BACK, SILENT_LOSSES_KEY: MAX_SILENT_LOSSES_GIVEN_BACK},
    )
    dead = await _take_back(
        AsyncMock(),
        row,
        datetime.now(UTC),
        why="Its worker went",
        head=True,
        owner_record=_silent(),
        silent_loss_given_back=_given_back(),
    )
    assert dead is True and row.attempt_count == 3


async def test_an_expired_lease_with_no_holder_record_is_unchanged():
    row = _row(attempt_count=3)
    dead = await _take_back(
        AsyncMock(), row, datetime.now(UTC), why="Lease expired", head=False, silent_loss_given_back=_given_back()
    )
    assert dead is True and infra_relaunches(row) == 0


def test_get_video_jobs_says_the_relaunches_not_counted():
    from app.services.video_studio import jobs

    lease = orchestration_engine.LeaseHeartbeat(
        silent_seconds=3,
        expires_in_seconds=170,
        attempt=1,
        max_attempts=3,
        lease_seconds=180,
        owner="w1",
        infra_relaunches=3,
    )
    state = SimpleNamespace(resume=None, no_lease_since=None, lease=lease)
    said = jobs.lease_clause(state)
    assert "3 relaunches after a worker restart or deploy not counted" in said
    assert lease.as_dict()["infra_relaunches"] == 3


def _older(**fields) -> dict:
    """A worker of an OLDER release gone without a stop request: the rollout's kill unless memory says otherwise."""
    return _silent(version="1.0.0-old", **fields)


def test_the_classifier_reads_an_older_release_under_its_memory_limit_as_a_rollout_kill():
    classify = _given_back()
    assert classify(_RENDER, _older()) == LOSS_ROLLOUT_KILL
    # The live release at high memory: a crash of the work, the attempt spent.
    assert classify(_RENDER, _silent(memory=int(3.9 * (1 << 30)))) is None
    # An older release at high memory: an out-of-memory kill, never a rollout's.
    assert classify(_RENDER, _older(memory=int(3.9 * (1 << 30)))) is None
    # The live release under its limit: a silent loss on its own, bounded count.
    assert classify(_RENDER, _silent()) == LOSS_SILENT
    assert classify("sub_agent", _older()) is None
    assert worker_presence.rollout_kill(_older())
    assert not worker_presence.rollout_kill(_silent())
    assert not worker_presence.rollout_kill(_older(state="draining"))
    assert "killed by the rollout that replaced its release (1.0.0-old -> 1.0.1-new" in worker_presence.stop_reason(
        _older()
    )


async def test_a_rollout_kill_after_many_handovers_keeps_the_render_attempt():
    # Nine builds rolled out: many handovers on a stop request already counted apart, then a SIGKILL by the rollout.
    row = _row(attempt_count=2, payload={INFRA_RELAUNCHES_KEY: 12, SILENT_LOSSES_KEY: MAX_SILENT_LOSSES_GIVEN_BACK})
    dead = await _take_back(
        AsyncMock(),
        row,
        datetime.now(UTC),
        why="Its worker went",
        head=True,
        owner_record=_older(),
        silent_loss_given_back=_given_back(),
    )
    assert dead is False and row.attempt_count == 1
    assert rollout_kills(row) == 1 and infra_relaunches(row) == 13
    assert row.payload[SILENT_LOSSES_KEY] == MAX_SILENT_LOSSES_GIVEN_BACK


async def test_handovers_never_use_up_the_bound_of_the_silent_losses():
    row = _row(attempt_count=2, payload={INFRA_RELAUNCHES_KEY: MAX_SILENT_LOSSES_GIVEN_BACK + 4})
    await _take_back(
        AsyncMock(),
        row,
        datetime.now(UTC),
        why="Its worker went",
        head=True,
        owner_record=_silent(),
        silent_loss_given_back=_given_back(),
    )
    assert row.attempt_count == 1 and row.payload[SILENT_LOSSES_KEY] == 1


async def test_rollout_kills_given_back_are_bounded():
    row = _row(attempt_count=3, payload={ROLLOUT_KILLS_KEY: MAX_ROLLOUT_KILLS_GIVEN_BACK})
    dead = await _take_back(
        AsyncMock(),
        row,
        datetime.now(UTC),
        why="Its worker went",
        head=True,
        owner_record=_older(),
        silent_loss_given_back=_given_back(),
    )
    assert dead is True and row.attempt_count == 3


async def test_a_render_crashing_on_the_live_build_still_fails_after_three_attempts():
    row = _row(attempt_count=1)
    dead = False
    for _ in range(3):
        dead = await _take_back(
            AsyncMock(),
            row,
            datetime.now(UTC),
            why="Its worker went",
            head=True,
            owner_record=_silent(memory=int(3.9 * (1 << 30))),
            silent_loss_given_back=_given_back(),
        )
        if dead:
            break
        row.attempt_count += 1
        row.state = "running"
    assert dead is True and row.state == "dead_letter" and infra_relaunches(row) == 0


async def test_a_classifier_answering_true_keeps_the_older_contract():
    row = _row(attempt_count=2)
    await _take_back(
        AsyncMock(),
        row,
        datetime.now(UTC),
        why="Its worker went",
        head=True,
        owner_record=_silent(),
        silent_loss_given_back=lambda name, record: True,
    )
    assert row.attempt_count == 1 and row.payload[SILENT_LOSSES_KEY] == 1


def test_get_video_jobs_names_the_relaunches_after_a_rollout_kill():
    from app.services.video_studio import jobs

    lease = orchestration_engine.LeaseHeartbeat(
        silent_seconds=3,
        expires_in_seconds=170,
        attempt=1,
        max_attempts=3,
        lease_seconds=180,
        owner="w1",
        infra_relaunches=5,
        rollout_kills=2,
    )
    said = jobs.lease_clause(SimpleNamespace(resume=None, no_lease_since=None, lease=lease))
    assert "2 relaunches after a rollout kill, not counted" in said
    assert lease.as_dict()["rollout_kills"] == 2


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


async def test_same_take_answers_the_identical_recording_and_not_another():
    from app.services.builtin_tools.video_studio import same_take

    submitted = {"steps": [{"action": "goto", "url": "/"}], "params": {"start_url": "https://example.test"}}
    first = SimpleNamespace(id=uuid.uuid4(), name="Demo", submitted_script=submitted, status="running")
    other = SimpleNamespace(id=uuid.uuid4(), name="Demo", submitted_script={"steps": []}, status="pending")
    session = SimpleNamespace(execute=AsyncMock(return_value=_Result([other, first])))

    found = await same_take(
        session, user_id=uuid.uuid4(), conversation_id=uuid.uuid4(), name="Demo", submitted=dict(submitted)
    )
    assert found is first

    session.execute = AsyncMock(return_value=_Result([other]))
    assert (
        await same_take(session, user_id=uuid.uuid4(), conversation_id=None, name="Demo", submitted=submitted) is None
    )


def test_same_take_looks_back_a_bounded_window():
    from app.services.builtin_tools import video_studio

    assert timedelta(seconds=video_studio.SAME_TAKE_WINDOW_SECONDS) <= timedelta(hours=1)
