"""A render a rollout hands to a worker of the next build resumes from the frames it drew, never from frame 0 (#8819).

A segment records the digest of what its frames are drawn from (``compositor.WindowDigests``), and an attempt on
another worker takes back each segment whose recorded window matches (``render_segments.take_back``). The window held
the build's version (``settings.app_version``), which every rollout moves: after each deploy, no window matched and
the render was drawn again from frame 0. It now holds how the build draws (``compositor.drawing_release``: the
compositor page, the screen inserts' drawing and build versions, the Playwright release):

* the same drawing on a new build: every window keeps its digest, and the segments stored are taken back;
* the drawing changed (a page script, an insert drawing version): every window changes, nothing is joined.

Synthetic documents and a fake object store; the end-to-end draw needs Chromium and ffmpeg.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from app.core.config import settings
from app.services.video_studio import compositor, compositor_page, render_segments, screen_insert
from app.services.video_studio.spec import QUALITY_PRESETS

PRESET = QUALITY_PRESETS["high"]
SIZE = (1920, 1080)
RENDER = str(uuid.uuid4())


def _document() -> dict:
    return {
        "version": 1,
        "width": 1920,
        "height": 1080,
        "fps": 30,
        "duration": 8.0,
        "frames": 240,
        "theme": {"mode": "dark", "accent": "#00bcd4"},
        "captions": {"enabled": False},
        "watermark": "none",
        "scenes": [
            {"id": "s01", "type": "title", "title": "Exposure", "start": 0.0, "end": 4.0},
            {"id": "s02", "type": "title", "title": "Decision", "start": 4.0, "end": 8.0},
        ],
    }


@pytest.fixture(autouse=True)
def fresh_drawing_release():
    compositor.drawing_release.cache_clear()
    yield
    compositor.drawing_release.cache_clear()


def _window(first: int = 0, end: int = 60) -> str:
    return compositor.WindowDigests(_document(), PRESET, SIZE).of(first, end)


def test_a_new_build_that_draws_alike_keeps_every_window(monkeypatch):
    monkeypatch.setattr(settings, "app_version", "1.261105.0-dev.aaaaaaa")
    before = _window()
    monkeypatch.setattr(settings, "app_version", "1.261105.0-dev.bbbbbbb")
    compositor.drawing_release.cache_clear()
    assert _window() == before


def test_a_page_script_changed_changes_every_window(monkeypatch):
    before = _window()
    monkeypatch.setattr(compositor_page, "PAGE_JS", compositor_page.PAGE_JS + "\n// drawn otherwise")
    compositor.drawing_release.cache_clear()
    assert _window() != before


def test_an_insert_drawing_version_moved_changes_every_window(monkeypatch):
    before = _window()
    monkeypatch.setattr(screen_insert, "DRAWING_VERSION", screen_insert.DRAWING_VERSION + 1)
    compositor.drawing_release.cache_clear()
    assert _window() != before


class _Store:
    """The object store: ``name -> bytes``."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def list_object_names(self, prefix: str) -> list[str]:
        return sorted(name for name in self.objects if name.startswith(prefix))

    async def download(self, name: str) -> bytes:
        return self.objects[name]

    async def download_to_path(self, name: str, path: str, missing_ok: bool = False) -> None:
        with open(path, "wb") as target:
            target.write(self.objects[name])

    async def upload_path(self, path: str, name: str, content_type: str | None = None) -> None:
        with open(path, "rb") as source:
            self.objects[name] = source.read()

    async def delete(self, name: str) -> None:
        self.objects.pop(name, None)


@pytest.fixture
def store(monkeypatch):
    from app.services.file_storage import file_storage

    fake = _Store()
    for name in ("list_object_names", "download", "download_to_path", "upload_path", "delete"):
        monkeypatch.setattr(file_storage, name, getattr(fake, name))
    return fake


def test_the_next_build_takes_back_the_segments_the_render_stored(store, monkeypatch, tmp_path):
    # Drawn on build A: two segments stored under A's digest of the whole drawing, each with its window.
    monkeypatch.setattr(settings, "app_version", "1.261105.0-dev.aaaaaaa")
    old = render_segments.prefix_of(RENDER, "a" * 64)
    for first, end in ((0, 60), (60, 120)):
        name = old + compositor.segment_name(first, end)
        store.objects[name] = b"segment"
        store.objects[name[: -len(".mp4")] + compositor.DRAWN_SUFFIX] = json.dumps(
            {"revision": 3, "attempt": 1, "render": RENDER, "window": _window(first, end)}
        ).encode()
    # Build B takes the render over: another digest of the whole drawing, the same windows.
    monkeypatch.setattr(settings, "app_version", "1.261105.0-dev.bbbbbbb")
    compositor.drawing_release.cache_clear()
    windows = compositor.WindowDigests(_document(), PRESET, SIZE)
    runs = asyncio.run(render_segments.take_back(RENDER, "b" * 64, tmp_path, (0, 240), windows, set()))
    assert [run["frames"] for run in runs] == [[0, 120]]
    assert (tmp_path / compositor.segment_name(0, 60)).read_bytes() == b"segment"
    mine = render_segments.prefix_of(RENDER, "b" * 64)
    assert mine + compositor.segment_name(60, 120) in store.objects
