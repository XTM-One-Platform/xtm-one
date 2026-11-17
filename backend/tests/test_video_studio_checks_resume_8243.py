"""A render relaunched on another worker at its checks goes on watching where it stood. Synthetic frames, a fake store.

A deploy replaces the worker and its disk: the frozen-picture watch's place
(``frame-watch.json``) was only in the working directory, so the next
attempt watched the render again from 0 s (seen restarting at 80 of 172 s and
110 of 172 s).

* after each window the place is stored beside the render, and an attempt on
  a fresh working directory takes it back and goes on from it;
* a store failing or hanging costs the watch nothing but that copy;
* nothing stored is a watch from the start; the place goes with the render's
  segments and is never taken for a segment.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest

from app.services.file_storage import FileNotFoundInStorageError
from app.services.video_studio import encoder, frame_freeze, render_segments

DOCUMENT = {"duration": 3.0, "scenes": [{"id": "a", "type": "clip", "start": 0.0, "end": 3.0}]}


def _moving(count: int) -> list[bytes]:
    rng = np.random.default_rng(1)
    picture = np.kron(rng.uniform(0, 255, (18, 32)), np.ones((6, 6))).astype(np.uint8)
    return [np.roll(picture, i, axis=1).tobytes() for i in range(count)]


class _Store:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail = False
        self.hang = False

    async def upload_path(self, path, name, _content_type=None):
        if self.hang:
            await asyncio.sleep(3600)
        if self.fail:
            raise OSError("store down")
        self.objects[name] = Path(path).read_bytes()
        return name

    async def download_to_path(self, name, path, *, missing_ok=False):
        if name not in self.objects:
            raise FileNotFoundInStorageError(name)
        Path(path).write_bytes(self.objects[name])

    async def list_object_names(self, prefix, *, recursive=True):
        return [name for name in self.objects if name.startswith(prefix)]

    async def delete(self, name):
        self.objects.pop(name, None)


@pytest.fixture
def store(monkeypatch):
    from app.services import file_storage as storage_module

    kept = _Store()
    monkeypatch.setattr(storage_module, "file_storage", kept)
    return kept


@pytest.fixture
def frames(monkeypatch):
    """90 moving frames at 30 fps, served from where the watch seeks; each decoder's start recorded."""
    served = _moving(90)
    starts: list[float] = []

    async def stream(_path, *, width, height, fps, start=0.0):
        starts.append(start)
        for index in range(round(start * fps), len(served)):
            yield served[index]

    monkeypatch.setattr(encoder, "iter_gray_frames", stream)
    monkeypatch.setattr(frame_freeze, "WINDOW_SECONDS", 1.0)
    return starts


class StoppedError(Exception):
    """The worker stopped by a deploy, mid-watch."""


async def _stopped_at_2_s(seconds: float, _total: float, _window) -> None:
    if seconds >= 2.0:
        raise StoppedError


def _checkpoint(workdir: Path, render_id: str = "r1") -> frame_freeze.Checkpoint:
    import functools

    return frame_freeze.Checkpoint(
        workdir / "frame-watch.json", "the drawing|30", functools.partial(render_segments.keep_check_place, render_id)
    )


def _watch(checkpoint: frame_freeze.Checkpoint, **options) -> frame_freeze.FrameCheck:
    return asyncio.run(
        frame_freeze.check_frames("final.mp4", document=DOCUMENT, width=1920, height=1080, fps=30, checkpoint=checkpoint, **options)
    )


def test_an_attempt_on_another_worker_takes_the_watch_place_back_and_goes_on_from_it(store, frames, tmp_path):
    worker_a = tmp_path / "worker-a"
    worker_a.mkdir()
    with pytest.raises(StoppedError):
        _watch(_checkpoint(worker_a), watched=_stopped_at_2_s)
    assert list(store.objects) == [render_segments.check_place_of("r1", "frame-watch.json")]

    # Another worker, a fresh disk: the place comes back from the store, the watch goes on at 2 s.
    worker_b = tmp_path / "worker-b"
    assert asyncio.run(render_segments.restore_check_place("r1", worker_b / "frame-watch.json")) is True
    resumed = _watch(_checkpoint(worker_b))
    # The run under way at 2 s is judged again from its first frame: never from 0 s.
    # Worker A's decoder opened at 0 s; worker B's opens at 2 s (a quarter of a frame early, ``frame_freeze._seek``).
    assert len(frames) == 2 and frames[0] == 0.0 and frames[1] == pytest.approx(2.0, abs=0.1)
    assert resumed.summary["resumed_at"] == pytest.approx(2.0, abs=0.1) and resumed.summary["frames"] == 90
    # A place already on the disk is never replaced by the store's.
    assert asyncio.run(render_segments.restore_check_place("r1", worker_b / "frame-watch.json")) is False


def test_a_place_of_other_frames_is_taken_back_but_never_read(store, frames, tmp_path):
    worker_a = tmp_path / "worker-a"
    worker_a.mkdir()
    with pytest.raises(StoppedError):
        _watch(_checkpoint(worker_a), watched=_stopped_at_2_s)
    worker_b = tmp_path / "worker-b"
    assert asyncio.run(render_segments.restore_check_place("r1", worker_b / "frame-watch.json")) is True
    other = frame_freeze.Checkpoint(worker_b / "frame-watch.json", "another drawing|30")
    check = _watch(other)
    assert "resumed_at" not in check.summary and frames[-1] == 0.0


@pytest.mark.parametrize("trouble", ["fail", "hang"])
def test_a_store_failing_or_hanging_costs_the_watch_nothing(store, frames, tmp_path, trouble, monkeypatch):
    # The judging deadline on a frozen clock: a loaded runner never leaves a window unjudged here, while the
    # hanging store is still cut after 0.2 s by the event loop's own clock.
    monkeypatch.setattr(frame_freeze, "_now", lambda: 0.0)
    setattr(store, trouble, True)
    check = _watch(_checkpoint(tmp_path), budget=0.2)
    assert check.summary["frames"] == 90 and "unjudged" not in check.summary
    assert store.objects == {} and (tmp_path / "frame-watch.json").is_file()


def test_nothing_stored_is_a_watch_from_the_start_and_the_place_goes_with_the_segments(store, tmp_path):
    assert asyncio.run(render_segments.restore_check_place("r2", tmp_path / "frame-watch.json")) is False
    assert not (tmp_path / "frame-watch.json").exists() and not list(tmp_path.iterdir())

    place = tmp_path / "frame-watch.json"
    place.write_text('{"key": "k", "frame": 30}', encoding="utf-8")
    asyncio.run(render_segments.keep_check_place("r2", place))
    name = render_segments.check_place_of("r2", "frame-watch.json")
    # Never taken for a segment of the render ...
    assert not render_segments._STORED_SEGMENT.match(name[len(render_segments.prefix_of("r2")) :])
    # ... and gone once the render ended.
    assert asyncio.run(render_segments.forget("r2")) == 1 and store.objects == {}
