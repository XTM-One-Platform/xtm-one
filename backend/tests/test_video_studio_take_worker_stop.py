"""A recording its worker's stop tears down is handed back, never cancelled nor lost (#9309).

A rollout or a node drain stops the worker recording or encoding a take:
``runners.run_take`` queues the take again (its stage saying why) instead of
marking it cancelled, the take adapter hands its execution back first in the
lane with its attempt unspent, a take whose worker went is taken back on the
worker's last word (``resume_when_worker_gone``), and a take that ran out of
attempts names what ended it rather than a bare lease expiry.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.video_studio import JOB_CANCELLED, JOB_FAILED, JOB_PENDING, JOB_RUNNING, VideoTake
from app.services import orchestration_engine, orchestration_reconciler
from app.services.orchestration_adapters import video_studio as adapters
from app.services.orchestration_engine import LeaseExpiry
from app.services.video_studio import capture, jobs, runners, take_state
from app.services.video_studio.spec import validate_take_script

USER_ID = uuid.uuid4()


def _take() -> VideoTake:
    script = validate_take_script(
        {"start_url": "https://app.example.com/", "steps": [{"action": "wait", "seconds": 1}]}
    )
    return VideoTake(id=uuid.uuid4(), user_id=USER_ID, name="Dashboard tour", status=JOB_PENDING, script=script)


def _encoding_torn_down(monkeypatch, take: VideoTake, *, stopping: bool) -> list[dict]:
    """``run_take`` on *take*, torn down (``CancelledError``) at its encode; the rows' writes, in order."""
    written: list[dict] = []

    class _Db:
        async def get(self, _model, _key):
            return take

        async def commit(self):
            return None

    @asynccontextmanager
    async def session():
        yield _Db()

    async def earlier(*_args, **_kwargs):
        return {}

    async def record(*_args, progress=None, **_kwargs):
        # Its steps done, its encode at 75 %: the worker's stop cancels the task there.
        await progress(90, "Encoding the recording (75 %, 2,150 of 2,883 frames)")
        raise asyncio.CancelledError

    async def mark(_kind, _job_id, **changes):
        written.append(changes)

    async def mark_if_open(_kind, _job_id, **changes):
        written.append(changes)
        return True

    async def mark_cancelled(_kind, _job_id):
        written.append({"status": JOB_CANCELLED, "error": take.error or "Cancelled."})

    monkeypatch.setattr(runners, "async_session", session)
    monkeypatch.setattr(jobs, "mark_cancelled", mark_cancelled)
    monkeypatch.setattr(take_state, "load_earlier_finds", earlier)
    monkeypatch.setattr(capture, "record_take", record)
    monkeypatch.setattr(jobs, "mark", mark)
    monkeypatch.setattr(jobs, "mark_if_open", mark_if_open)
    monkeypatch.setattr(orchestration_reconciler, "worker_stopping", lambda: stopping)
    return written


def test_a_take_torn_down_at_its_encode_by_a_worker_stop_is_queued_again_not_cancelled(monkeypatch):
    take = _take()
    written = _encoding_torn_down(monkeypatch, take, stopping=True)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runners.run_take(str(take.id)))
    last = written[-1]
    assert (last["status"], last["progress"], last["started_at"]) == (JOB_PENDING, 0, None)
    assert last["stage"] == runners.TAKE_STOPPED_STAGE
    assert all(change.get("status") != JOB_CANCELLED for change in written)


def test_a_take_the_person_cancelled_is_still_cancelled(monkeypatch):
    take = _take()
    written = _encoding_torn_down(monkeypatch, take, stopping=False)
    cancel = asyncio.Event()
    cancel.set()
    outcome = asyncio.run(runners.run_take(str(take.id), cancel_event=cancel))
    assert outcome.kind == "done"
    assert written[-1]["status"] == JOB_CANCELLED


def test_a_take_cut_without_a_cancel_nor_a_worker_stop_fails_never_cancelled(monkeypatch):
    """Its deadline, or its execution taken back, cuts the run: the take fails naming the stop, never "Cancelled."
    (#9780)."""
    take = _take()
    written = _encoding_torn_down(monkeypatch, take, stopping=False)
    outcome = asyncio.run(runners.run_take(str(take.id)))
    assert outcome.kind == "done"
    assert written[-1]["status"] == JOB_FAILED and "nobody cancelled it" in written[-1]["error"]
    assert all(change.get("status") != JOB_CANCELLED for change in written)


def test_a_cancel_asked_while_the_worker_stops_is_the_persons(monkeypatch):
    take = _take()
    written = _encoding_torn_down(monkeypatch, take, stopping=True)
    cancel = asyncio.Event()
    cancel.set()
    outcome = asyncio.run(runners.run_take(str(take.id), cancel_event=cancel))
    assert outcome.kind == "done"
    assert written[-1]["status"] == JOB_CANCELLED


def test_the_take_adapter_takes_back_a_take_its_worker_went_with():
    adapter = adapters.VideoTakeAdapter()
    assert adapter.resume_when_worker_gone is True
    assert adapter.name in orchestration_reconciler.resumable_adapters([adapter])


def test_the_take_adapter_hands_its_execution_back_as_its_worker_stops(monkeypatch):
    handed: list[tuple] = []

    async def hand_back(execution_id, worker_id):
        handed.append((execution_id, worker_id))

    async def run(self, job_id, cancel_event, *, lane=None):
        raise asyncio.CancelledError

    async def commit():
        return None

    monkeypatch.setattr(adapters, "_hand_back_take", hand_back)
    monkeypatch.setattr(adapters.VideoTakeAdapter, "_run", run)
    stopping = asyncio.Event()
    stopping.set()
    ctx = SimpleNamespace(db=SimpleNamespace(commit=commit), worker_id="worker-1", stopping=stopping, cancel_event=None)
    execution = SimpleNamespace(id=uuid.uuid4(), payload={"take_id": str(uuid.uuid4())}, adapter_state=None)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(adapters.VideoTakeAdapter().execute(ctx, execution))
    assert handed == [(execution.id, "worker-1")]


def test_the_hand_back_defers_first_in_the_lane_its_attempt_unspent(monkeypatch):
    deferred: list[dict] = []

    class _Db:
        async def commit(self):
            return None

        async def get(self, _model, _key):
            return None

    @asynccontextmanager
    async def session():
        yield _Db()

    async def defer(_db, **kwargs):
        deferred.append(kwargs)

    async def wake():
        return None

    monkeypatch.setattr("app.core.database.async_session", session)
    monkeypatch.setattr(orchestration_engine, "defer", defer)
    monkeypatch.setattr(orchestration_engine, "notify_orchestration_wake", wake)
    execution_id = uuid.uuid4()
    asyncio.run(adapters._hand_back_take(execution_id, "worker-1"))
    (call,) = deferred
    assert call["execution_id"] == execution_id
    assert (call["delay_seconds"], call["place"], call["adapter_state"]) == (0.0, "head", adapters.HANDED_OVER_STATE)
    assert call["reason"] == adapters.TAKE_HANDOVER_REASON
    # ``defer`` on a leased execution gives back the attempt its claim counted.
    assert call.get("require_lease", True) is True


def test_a_take_handed_back_gets_a_whole_take_deadline_again():
    """A take stopped in its passes after most of its budget gets a whole take's time on the next worker (#9780)."""
    almost_spent = datetime.now(UTC) + timedelta(seconds=40)
    execution = SimpleNamespace(deadline_at=almost_spent)
    adapters.fresh_take_deadline(execution)
    assert execution.deadline_at >= jobs.take_deadline() - timedelta(seconds=5)
    later = jobs.take_deadline() + timedelta(hours=1)
    kept = SimpleNamespace(deadline_at=later)
    adapters.fresh_take_deadline(kept)
    assert kept.deadline_at == later
    adapters.fresh_take_deadline(None)
    unbounded = SimpleNamespace(deadline_at=None)
    adapters.fresh_take_deadline(unbounded)
    assert unbounded.deadline_at is None


def _paced_then_cut(monkeypatch, take: VideoTake, *, cancelled_first: bool) -> list[dict]:
    """``_ended_on_cancel`` on *take* (cut at its pacing, its worker not stopping); the rows' writes, in order."""
    written: list[dict] = []
    if cancelled_first:
        take.status, take.error = JOB_CANCELLED, "Cancelled by user@example.com."

    class _Db:
        async def get(self, _model, _key):
            return take

    @asynccontextmanager
    async def session():
        yield _Db()

    async def mark_if_open(_kind, _job_id, **changes):
        written.append({"if_open": True, **changes})
        return True

    async def mark_cancelled(_kind, _job_id):
        written.append({"status": JOB_CANCELLED, "error": take.error})

    monkeypatch.setattr(runners, "async_session", session)
    monkeypatch.setattr(jobs, "mark_if_open", mark_if_open)
    monkeypatch.setattr(jobs, "mark_cancelled", mark_cancelled)
    return written


def test_a_take_cut_at_its_pacing_without_a_cancel_fails_naming_the_stop_not_cancelled(monkeypatch):
    take = _take()
    take.status = JOB_RUNNING
    written = _paced_then_cut(monkeypatch, take, cancelled_first=False)
    asyncio.run(runners._ended_on_cancel(str(take.id), resumable=True))
    (last,) = written
    assert last["status"] == JOB_FAILED and last["if_open"] is True
    assert "Cancelled." not in last["error"]
    assert "rollout or a drain" in last["error"] and "nobody cancelled it" in last["error"]
    assert "while its passes or its encode ran" in last["error"]
    assert f"from_recording_id='{take.id}'" in last["error"]


def test_a_take_cancel_video_job_cancelled_keeps_who_cancelled_it(monkeypatch):
    take = _take()
    written = _paced_then_cut(monkeypatch, take, cancelled_first=True)
    asyncio.run(runners._ended_on_cancel(str(take.id), resumable=True))
    (last,) = written
    assert (last["status"], last["error"]) == (JOB_CANCELLED, "Cancelled by user@example.com.")


def test_a_resumed_take_cut_at_its_pacing_by_a_worker_stop_is_queued_again_from_its_keyframes(monkeypatch):
    """The next worker finishing a take from its keyframes is stopped at "Pacing its scrolls": queued again, never
    cancelled nor failed (#9780)."""
    take = _take()
    written: list[dict] = []

    class _Db:
        async def get(self, _model, _key):
            return take

        async def commit(self):
            return None

    @asynccontextmanager
    async def session():
        yield _Db()

    async def download(_file_id, path):
        return SimpleNamespace(id=uuid.uuid4(), path=path)

    source = SimpleNamespace(
        events=[],
        frames=[],
        alone=[],
        painted={},
        capture={},
        duration=1.0,
        pixel_ratio=1.0,
        viewport=(1280, 720),
        fps=60,
        cursor=[],
    )

    async def kept(_take_id, *, progress, **_kwargs):
        await progress(83, "Pacing its scrolls and transitions at 60 fps")
        raise asyncio.CancelledError

    async def mark(_kind, _job_id, **changes):
        written.append(changes)

    async def mark_if_open(_kind, _job_id, **changes):
        written.append(changes)
        return True

    monkeypatch.setattr(runners, "async_session", session)
    monkeypatch.setattr(runners.assets, "download_file", download)
    monkeypatch.setattr(runners.take_repair, "unpack_keyframes", lambda *_a: source)
    monkeypatch.setattr(runners, "_kept_take", kept)
    monkeypatch.setattr(jobs, "mark", mark)
    monkeypatch.setattr(jobs, "mark_if_open", mark_if_open)
    monkeypatch.setattr(orchestration_reconciler, "worker_stopping", lambda: True)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runners._resumed_take(str(take.id), {"keyframes": str(uuid.uuid4())}))
    assert written[-1]["status"] == JOB_PENDING and written[-1]["stage"] == runners.TAKE_RESUME_STAGE
    assert all(change.get("status") not in (JOB_CANCELLED, JOB_FAILED) for change in written)


def test_a_take_out_of_attempts_names_the_worker_stop_not_a_bare_lease(monkeypatch):
    marked: list[dict] = []
    row = SimpleNamespace(status=JOB_RUNNING, stage="Encoding the recording (75 %, 2,150 of 2,883 frames)", progress=89)

    async def load(_kind, _job_id):
        return row

    async def mark(_kind, _job_id, **changes):
        marked.append(changes)

    monkeypatch.setattr(jobs, "load", load)
    monkeypatch.setattr(jobs, "mark", mark)
    lease = LeaseExpiry(
        last_heartbeat_at=datetime.now(UTC),
        silent_seconds=60,
        owner="worker-1",
        attempt=2,
        why="its worker (version 1.2.3) was asked to stop (SIGTERM) and was killed before it handed it back",
    )
    asyncio.run(runners.dead_letter("take", uuid.uuid4(), None, lease=lease))
    error = marked[-1]["error"]
    assert error.startswith("Its worker (version 1.2.3) was asked to stop (SIGTERM)")
    assert "at 89% (Encoding the recording" in error
    assert error.endswith("with no attempt left: record it again.")
