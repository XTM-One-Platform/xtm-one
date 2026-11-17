"""A recording its worker's stop tears down during its encode is finished from its keyframes, never recorded again (#9309).

``runners.run_take`` stores a take's keyframes before its passes and its encode and names them on the take
(``capture.keyframes``, ``capture.resume``); a stop from there on queues it saying it will be finished from them, and
the next worker's ``run_take`` finishes it from them (``_resumed_take``): no browser, no capture, ready without a
new call. Keyframes that cannot be read send it back to its script.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.models.video_studio import JOB_PENDING, JOB_READY, VideoTake
from app.services import orchestration_reconciler
from app.services.video_studio import capture, jobs, runners, take_repair, take_state
from app.services.video_studio.spec import validate_take_script

USER_ID = uuid.uuid4()


def _take(**capture_) -> VideoTake:
    script = validate_take_script(
        {"start_url": "https://app.example.com/", "steps": [{"action": "wait", "seconds": 1}]}
    )
    return VideoTake(
        id=uuid.uuid4(),
        user_id=USER_ID,
        name="Dashboard tour",
        status=JOB_PENDING,
        script=script,
        capture=capture_ or None,
    )


class _Db:
    def __init__(self, take):
        self.take = take

    async def get(self, _model, _key):
        return self.take

    async def commit(self):
        return None


def _session_for(monkeypatch, take):
    @asynccontextmanager
    async def session():
        yield _Db(take)

    monkeypatch.setattr(runners, "async_session", session)


def test_resume_point_needs_its_keyframes_and_its_point():
    assert take_repair.resume_point(None) is None
    assert take_repair.resume_point({"resume": {"notes": []}}) is None
    assert take_repair.resume_point({"keyframes": "f-1"}) is None
    assert take_repair.resume_point({"keyframes": "f-1", "resume": {"notes": ["a"]}}) == {
        "notes": ["a"],
        "keyframes": "f-1",
    }
    # A finished take's measured fields never carry it into a repair from its MP4.
    assert take_repair.RESUME_KEY in take_repair.MEASURED_FIELDS


def test_the_keyframes_are_stored_and_named_on_the_take_before_its_encode(monkeypatch):
    take = _take()
    _session_for(monkeypatch, take)
    stored = SimpleNamespace(id=uuid.uuid4())
    seen: dict = {}

    async def store(path, **_kwargs):
        seen["path"] = path
        return stored

    monkeypatch.setattr(runners, "_store_keyframes", store)
    written = asyncio.run(runners._resume_point_written(str(take.id), stored.id, {"notes": ["n"], "layout": None}))
    assert written is True
    point = take_repair.resume_point(take.capture)
    assert point == {"notes": ["n"], "layout": None, "keyframes": str(stored.id)}


def test_a_take_stopped_at_its_encode_after_its_keyframes_were_stored_waits_to_be_finished_from_them(monkeypatch):
    take = _take()
    _session_for(monkeypatch, take)
    written: list[dict] = []

    async def earlier(*_args, **_kwargs):
        return {}

    result = SimpleNamespace(
        script=None,
        notes=[],
        layout=None,
        frames=[],
        events=[],
        alone=[],
        painted={},
        capture={},
        duration=1.0,
        pixel_ratio=2.0,
        viewport=(1280, 720),
        cursor=[],
    )

    async def record(*_args, **_kwargs):
        return result

    async def kept_take(_take_id, *, on_packed=None, progress=None, **_kwargs):
        await on_packed("keyframes.zip")
        await progress(90, "Encoding the recording (75 %, 2,150 of 2,883 frames)")
        raise asyncio.CancelledError

    async def store(_path, **_kwargs):
        return SimpleNamespace(id=uuid.uuid4())

    async def mark(_kind, _job_id, **changes):
        written.append(changes)

    async def mark_if_open(_kind, _job_id, **changes):
        written.append(changes)
        return True

    async def is_cancelled(*_args):
        return False

    monkeypatch.setattr(take_state, "load_earlier_finds", earlier)
    monkeypatch.setattr(capture, "record_take", record)
    monkeypatch.setattr(runners, "_kept_take", kept_take)
    monkeypatch.setattr(runners, "_store_keyframes", store)
    monkeypatch.setattr(jobs, "mark", mark)
    monkeypatch.setattr(jobs, "mark_if_open", mark_if_open)
    monkeypatch.setattr(jobs, "is_cancelled", is_cancelled)
    monkeypatch.setattr(orchestration_reconciler, "worker_stopping", lambda: True)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runners.run_take(str(take.id)))
    assert written[-1]["status"] == JOB_PENDING
    assert written[-1]["stage"] == runners.TAKE_RESUME_STAGE
    assert take_repair.resume_point(take.capture) is not None


def test_the_next_worker_finishes_a_stopped_take_from_its_keyframes_without_recording(monkeypatch):
    kept_id = str(uuid.uuid4())
    take = _take(
        keyframes=kept_id, resume={"notes": ["step 2 waited"], "layout": {"width": 1280}, "fps_fallback": "refuse"}
    )
    _session_for(monkeypatch, take)
    calls: dict = {}

    async def record(*_args, **_kwargs):  # never: no browser, no capture
        raise AssertionError("recorded again")

    async def download(file_id, path):
        calls["downloaded"] = file_id
        return SimpleNamespace(id=file_id)

    source = take_repair.Source(
        frames=[(0.0, "f0.jpg"), (0.5, "f1.jpg")],
        events=[],
        duration=1.0,
        pixel_ratio=2.0,
        viewport=(1280, 720),
        fps=30,
    )

    def unpack(_zip, _workdir):
        return source

    async def kept_take(_take_id, *, stored_keyframes=None, notes=None, **_kwargs):
        calls["stored_keyframes"] = stored_keyframes
        calls["notes"] = notes
        return runners._Kept(
            {"keyframes": kept_id},
            list(notes),
            SimpleNamespace(duration=1.0),
            SimpleNamespace(id=uuid.uuid4(), size=10),
            SimpleNamespace(id=uuid.uuid4()),
            None,
            stored_keyframes,
        )

    async def mark(*_args, **_kwargs):
        return None

    monkeypatch.setattr(capture, "record_take", record)
    monkeypatch.setattr(runners.assets, "download_file", download)
    monkeypatch.setattr(take_repair, "unpack_keyframes", unpack)
    monkeypatch.setattr(runners, "_kept_take", kept_take)
    monkeypatch.setattr(runners.capture_rate, "refusal", lambda *_a: None)
    monkeypatch.setattr(jobs, "mark", mark)
    outcome = asyncio.run(runners.run_take(str(take.id)))
    assert outcome.kind == "done"
    assert take.status == JOB_READY
    assert calls["downloaded"] == kept_id
    assert calls["stored_keyframes"].id == kept_id  # the stored keyframes kept as they are, not stored twice
    # Its notes keep its own and say the relaunch and the stage it resumed at (#9812).
    assert calls["notes"] == ["step 2 waited", runners.TAKE_RESUMED_NOTE]
    assert take.captured_frames == 2 and take.layout == {"width": 1280}
    assert take_repair.resume_point(take.capture) is None


def test_unreadable_keyframes_send_the_take_back_to_its_script(monkeypatch):
    take = _take(keyframes=str(uuid.uuid4()), resume={"notes": []})
    _session_for(monkeypatch, take)
    deleted: list = []

    async def download(_file_id, _path):
        return None

    async def delete_files(ids):
        deleted.extend(ids)

    async def mark_if_open(*_args, **_kwargs):
        return True

    monkeypatch.setattr(runners.assets, "download_file", download)
    monkeypatch.setattr(runners.assets, "delete_files", delete_files)
    monkeypatch.setattr(jobs, "mark_if_open", mark_if_open)
    outcome = asyncio.run(runners._resumed_take(str(take.id), take_repair.resume_point(take.capture)))
    assert outcome is None
    assert take_repair.resume_point(take.capture) is None
    assert len(deleted) == 1
