"""A whole render failed at a check stage keeps its frames 24 h for the next render of its revision. A fake store.

* failed by its checks after its drawing: its segments and its checks' place stay, a marker says until when;
* a render that passed, or that failed before storing a segment, keeps nothing past its end;
* the next render whose drawing digest, fps and size match takes them as its own and resumes from them;
* one drawn from something else ignores them and they are forgotten;
* the hourly sweep forgets them past their 24 h, naming the render and the digest;
* a store failing on keep is a log line, never a failed render.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.video_studio import JOB_FAILED, JOB_READY
from app.services.file_storage import FileNotFoundInStorageError
from app.services.video_studio import render_segments

NOW = datetime(2026, 11, 4, 7, 0, tzinfo=UTC)
DIGEST = "d" * 64
SEGMENTS = ("seg-0000000-0000300.mp4", "seg-0000300-0000600.mp4")


class _Store:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail = False

    async def upload_path(self, path, name, _content_type=None):
        if self.fail:
            raise OSError("store down")
        self.objects[name] = Path(path).read_bytes()
        return name

    async def upload_to(self, data, name, _content_type=None):
        if self.fail:
            raise OSError("store down")
        self.objects[name] = bytes(data)
        return name

    async def download(self, name):
        if name not in self.objects:
            raise FileNotFoundInStorageError(name)
        return self.objects[name]

    async def download_to_path(self, name, path, *, missing_ok=False):
        if name not in self.objects:
            raise FileNotFoundInStorageError(name)
        Path(path).write_bytes(self.objects[name])

    async def list_object_names(self, prefix, *, recursive=True):
        if self.fail:
            raise OSError("store down")
        return [name for name in self.objects if name.startswith(prefix)]

    async def delete(self, name):
        self.objects.pop(name, None)


@pytest.fixture
def store(monkeypatch):
    from app.services import file_storage as storage_module

    kept = _Store()
    monkeypatch.setattr(storage_module, "file_storage", kept)

    async def not_handed_over(_render_id):
        return False

    monkeypatch.setattr(render_segments, "handed_over", not_handed_over)
    return kept


def _drawn(store: _Store, render_id: str, digest: str = DIGEST) -> None:
    for name in SEGMENTS:
        store.objects[render_segments.prefix_of(render_id, digest) + name] = name.encode()
    store.objects[render_segments.check_place_of(render_id, "frame-watch.json")] = b'{"key": "k", "frame": 240}'


def _keep(render_id: str, *, now: datetime = NOW, digest: str = DIGEST) -> datetime | None:
    return asyncio.run(render_segments.keep_failed(render_id, digest, fps=30, size=(1920, 1080), now=now))


def test_a_render_failed_at_its_checks_keeps_its_frames_24_h(store):
    _drawn(store, "failed")
    until = _keep("failed")
    assert until == NOW + timedelta(seconds=render_segments.RENDER_FAILED_KEEP_SECONDS)
    assert render_segments.RENDER_FAILED_KEEP_SECONDS == 86400
    marker = asyncio.run(render_segments.kept_of("failed"))
    assert marker["digest"] == DIGEST and marker["until"] == until and marker["fps"] == 30
    # The attempt ending does not forget them while their time runs.
    assert asyncio.run(render_segments.forget_unless_kept("failed", NOW + timedelta(hours=1))) == 0
    assert len(store.objects) == 4
    # The record says so while they are kept, never after.
    row = SimpleNamespace(status=JOB_FAILED, drawing={render_segments.FRAMES_KEPT_UNTIL: until.isoformat()})
    assert render_segments.frames_kept_until(row, NOW) == until
    assert render_segments.kept_until_line(until).startswith("frames kept until 2026-11-05 07:00 UTC")
    assert render_segments.frames_kept_until(row, until + timedelta(seconds=1)) is None


def test_a_render_that_passed_or_stored_no_segment_keeps_nothing(store):
    # Passed: nothing kept, its attempt's end forgets what it stored, as before.
    _drawn(store, "passed")
    assert asyncio.run(render_segments.forget_unless_kept("passed", NOW)) == 3 and store.objects == {}
    ready = SimpleNamespace(status=JOB_READY, drawing={render_segments.FRAMES_KEPT_UNTIL: (NOW + timedelta(hours=2)).isoformat()})
    assert render_segments.frames_kept_until(ready, NOW) is None
    # Failed before drawing: no segment stored, no marker.
    assert _keep("early") is None and store.objects == {}


def test_the_next_render_of_the_same_drawing_resumes_from_the_kept_frames(store):
    _drawn(store, "failed")
    _keep("failed")
    resumed = asyncio.run(
        render_segments.adopt_kept("next", ["failed"], DIGEST, fps=30, size=(1920, 1080), now=NOW + timedelta(hours=3))
    )
    assert resumed == {"render": "failed", "digest": DIGEST, "segments": 2, "frames": [[0, 300], [300, 600]]}
    assert render_segments.resumed_seconds(resumed["frames"], 30) == 20.0
    # Its own now: what the compositor restores and the watch takes back; the failed render's are gone.
    assert sorted(store.objects) == sorted(
        [render_segments.prefix_of("next", DIGEST) + name for name in SEGMENTS]
        + [render_segments.check_place_of("next", "frame-watch.json")]
    )


def test_a_render_drawn_from_something_else_ignores_the_kept_frames_and_they_are_forgotten(store):
    _drawn(store, "failed")
    _keep("failed")
    other = "e" * 64
    assert asyncio.run(render_segments.adopt_kept("next", ["failed"], other, fps=30, size=(1920, 1080), now=NOW)) is None
    assert store.objects == {}
    # Same digest, another size: ignored too.
    _drawn(store, "failed2")
    _keep("failed2")
    assert asyncio.run(render_segments.adopt_kept("next", ["failed2"], DIGEST, fps=30, size=(3840, 2160), now=NOW)) is None
    assert store.objects == {}


def test_kept_frames_expire_after_24_h(store, caplog):
    _drawn(store, "failed")
    _keep("failed")
    assert asyncio.run(render_segments.expire_kept(["failed"], NOW + timedelta(hours=23))) == {"failed"}
    assert len(store.objects) == 4
    with caplog.at_level(logging.INFO, logger=render_segments.__name__):
        assert asyncio.run(render_segments.expire_kept(["failed"], NOW + timedelta(hours=24, seconds=1))) == set()
    assert store.objects == {}
    assert any("failed" in record.getMessage() and DIGEST in record.getMessage() and "expired" in record.getMessage() for record in caplog.records)
    # Past their time a later render finds nothing to resume from.
    _drawn(store, "late")
    _keep("late")
    late = NOW + timedelta(days=2)
    assert asyncio.run(render_segments.adopt_kept("next", ["late"], DIGEST, fps=30, size=(1920, 1080), now=late)) is None
    assert asyncio.run(render_segments.forget_unless_kept("late", late)) == 4 and store.objects == {}


def test_a_store_failing_on_keep_is_logged_never_raised(store, caplog):
    _drawn(store, "failed")
    store.fail = True
    with caplog.at_level(logging.INFO, logger=render_segments.__name__):
        assert _keep("failed") is None
    assert any("not kept after its failed checks" in record.getMessage() for record in caplog.records)
    store.fail = False
    assert asyncio.run(render_segments.kept_of("failed")) is None
