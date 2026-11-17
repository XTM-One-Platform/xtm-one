"""A figure scene drawn once per key, across renders and previews. A fake figure page, a fake store.

The brand film of 20 Oct 2026: the previews of `s04` and `s06` each redrew
the 589 frames of `s05`'s figure for the 0.6 s their transitions show of it.

* the key is what the frames are drawn from - never the page's document,
  which gives the deck and its slide new ids on every read: the same figure
  for the same scene timing is the same key, a longer scene, another
  narration timing or another set of readable pictures another one;
* a render's frames are offered to every render of the same key on the
  worker: one stopped half-way leaves them to the next, which draws the rest;
* the video encoded whole is played as it is by the next render - on this
  worker, or downloaded from beside the video on another - with no page
  opened; it is never kept when a picture the person may read was missing;
* a scene preview draws a neighbour's figure only for the frames its window
  shows, and never keeps that part as the whole;
* the drawings go with the video, past :data:`figure_cache.KEPT_PER_VIDEO`
  the oldest stored, and a folder unused for hours leaves the worker.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import presentation_export
from app.services.file_storage import FileNotFoundInStorageError
from app.services.video_studio import encoder, figure_cache, figures, render
from app.services.video_studio.speech_timing import SpokenWord

PICTURE_ID = "00000000-0000-0000-0000-0000000061f2"
VIDEO_ID = "00000000-0000-0000-0000-0000000061a0"
SCENE = SimpleNamespace(id="s05-platform", reveal="even", build="sequence", camera="follow", dwell=0.8, motion="drift")
LENGTH = 3.0
FPS = 10


def _source() -> figures.FigureSource:
    block = {
        "type": "architecture",
        "nodes": [{"id": "a", "label": "OpenCTI", "row": 1, "col": 1}, {"id": "b", "label": "OpenAEV", "row": 1, "col": 2}],
        "edges": [{"source": "a", "target": "b"}],
    }
    return figures.source_from_spec(SimpleNamespace(block_spec=block))


def _document(source: figures.FigureSource) -> dict:
    return figures.figure_document(source, scheme="dark", palette={"accent": "#00bcd4"})


class _Page:
    """The figure page: two steps, moving over the whole scene; what it screenshots, and where it stops."""

    def __init__(self, world: SimpleNamespace) -> None:
        self.world = world

    async def evaluate(self, script, arg=None):
        if "describe" in script:
            return {"steps": [{"labels": ["OpenCTI"]}, {"labels": ["OpenAEV"]}]}
        if "plan" in script:
            return {"active": [[0.0, arg["length"]]]}
        return None

    async def screenshot(self, *, path, **_kw):
        if self.world.stop_at is not None and len(self.world.shot) == self.world.stop_at:
            raise RuntimeError("Target page, context or browser has been closed")
        Path(path).write_bytes(b"jpeg")
        self.world.shot.append(Path(path).name.removesuffix(".part"))


class _Store:
    """The object store: what was uploaded, by name, with when."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, datetime]] = {}

    async def upload_path(self, path, name, _content_type=None):
        self.objects[name] = (Path(path).read_bytes(), datetime.now(UTC))
        return name

    async def download_to_path(self, name, path, *, missing_ok=False):
        if name not in self.objects:
            raise FileNotFoundInStorageError(name)
        Path(path).write_bytes(self.objects[name][0])

    async def list_objects_modified(self, prefix):
        return [(name, at) for name, (_data, at) in self.objects.items() if name.startswith(prefix)]

    async def list_object_names(self, prefix, *, recursive=True):
        names = [name for name in self.objects if name.startswith(prefix)]
        if recursive:
            return names
        return sorted({prefix + name[len(prefix) :].split("/", 1)[0] + "/" for name in names})

    async def delete(self, name):
        self.objects.pop(name, None)


@pytest.fixture
def world(monkeypatch):
    from app.services import file_storage as storage_module

    world = SimpleNamespace(shot=[], opened=0, encoded=[], encode_waits=0, stop_at=None, store=_Store(), missing=set(), asks=[])

    @asynccontextmanager
    async def open_page(_url, **kw):
        world.opened += 1
        for file_id in world.asks:
            route = SimpleNamespace(request=SimpleNamespace(url=f"https://render.local/__figure__/asset/{file_id}"), fulfill=_fulfil)
            await kw["answer"](route)
        yield _Page(world)

    async def frames_to_mezzanine(frames, *, out_path, on_wait=None, **_kw):
        world.encoded.append([name for _t, name in frames])
        for _ in range(world.encode_waits):
            await on_wait()
        Path(out_path).write_bytes(b"mp4:" + ",".join(name for _t, name in frames).encode())

    async def probe(_path):
        return SimpleNamespace(width=1920, height=1080)

    async def picture(_source, _user, file_id):
        return None if file_id in world.missing else (b"png", "image/png")

    monkeypatch.setattr(presentation_export, "open_render_page", open_page)
    monkeypatch.setattr(encoder, "frames_to_mezzanine", frames_to_mezzanine)
    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(figures, "_picture", picture)
    monkeypatch.setattr(storage_module, "file_storage", world.store)
    return world


def _draw(
    workdir: Path,
    *,
    length: float = LENGTH,
    span=None,
    video_id: str | None = VIDEO_ID,
    readable=frozenset(),
    on_frame=None,
    drawing: figures.Drawing | None = None,
):
    source = _source()
    return asyncio.run(
        figures.draw(
            source,
            _document(source),
            user=None,
            scene=SCENE,
            length=length,
            words=None,
            fps=FPS,
            viewport=(1920, 1080),
            scale=1,
            workdir=workdir,
            video_id=video_id,
            readable=readable,
            span=span,
            on_frame=on_frame,
            drawing=drawing,
        )
    )


def _key(**changes) -> str:
    source = _source()
    options = {
        "scene": SCENE,
        "length": LENGTH,
        "words": None,
        "safe": dict(figures.NO_SAFE_AREA),
        "viewport": (1920, 1080),
        "scale": 1,
        "fps": FPS,
        **changes,
    }
    return figures.render_key(source, _document(source), **options)


def test_the_key_is_what_the_frames_are_drawn_from_never_the_pages_document():
    source = _source()
    assert _document(source) != _document(source)  # a new deck id and slide id on every read
    assert _key() == _key()
    words = [SpokenWord(0.4, 0.9, "opencti")]
    for changed in (_key(length=3.5), _key(words=words), _key(readable=frozenset({PICTURE_ID})), _key(scale=2), _key(fps=30)):
        assert changed != _key()


def test_a_render_stopped_half_way_leaves_its_frames_to_the_next_render_of_the_figure(world, tmp_path):
    world.stop_at = 12
    with pytest.raises(figures.FigureError, match="browser has been closed"):
        _draw(tmp_path / "preview-s06")
    assert len(world.shot) == 12 and world.encoded == []
    # Another render (another working directory): it takes the 12 frames and draws the 18 others.
    world.stop_at, world.shot = None, []
    path, clip = _draw(tmp_path / "preview-s05")
    assert world.shot == [f"f{index:05d}.jpg" for index in range(12, 30)]
    assert world.encoded[-1] == [f"f{index:05d}.jpg" for index in range(30)] and clip.frame_count == 30
    assert path == str((tmp_path / "preview-s05" / "figure-s05-platform" / "figure.mp4").resolve())


def test_what_each_draw_drew_and_took_is_recorded_for_the_reuse_index(world, tmp_path):
    # Stopped half-way, then drawn by another render: 12 frames taken, 18 drawn.
    world.stop_at = 12
    with pytest.raises(figures.FigureError):
        _draw(tmp_path / "stopped")
    world.stop_at, world.shot = None, []
    resumed = figures.Drawing()
    _draw(tmp_path / "resumed", drawing=resumed)
    assert (resumed.key, resumed.drawn, resumed.reused) == (_key(), 18, 12)
    # The next render plays the video kept whole: every frame of the scene served, none drawn.
    played = figures.Drawing()
    _draw(tmp_path / "played", drawing=played)
    assert (played.key, played.drawn, played.reused) == (_key(), 0, round(LENGTH * FPS))
    # A neighbour's window: the frames its span shows.
    part = figures.Drawing()
    _draw(tmp_path / "neighbour", span=(2.4, 3.0), drawing=part)
    assert (part.drawn, part.reused) == (0, round(0.6 * FPS))


def test_the_encode_of_a_figure_keeps_the_lease_while_it_draws_no_frame(world, tmp_path):
    # At 2160p on a 4-vCPU worker the encode of 589 frames outlasted the 60 s lease: each wait it reports
    # is the render's work too.
    world.encode_waits = 4
    told = []
    _draw(tmp_path / "render", on_frame=lambda: told.append(len(world.encoded)))
    assert told.count(0) == 30 and told.count(1) == 4


def test_a_figure_drawn_whole_is_played_as_it_is_by_the_next_render_with_no_page_opened(world, tmp_path):
    _draw(tmp_path / "first")
    assert world.opened == 1 and len(world.shot) == 30
    key = _key()
    assert (figure_cache.ROOT / key / figure_cache.VIDEO).is_file()
    # Its frames leave the worker's folder once the video is kept: the video is what the next render plays.
    assert not list((figure_cache.ROOT / key).glob("f*.jpg"))
    assert figure_cache.object_name(VIDEO_ID, key) in world.store.objects
    world.shot = []
    path, clip = _draw(tmp_path / "second")
    assert world.opened == 1 and world.shot == [] and len(world.encoded) == 1
    assert path == str(figure_cache.ROOT / key / figure_cache.VIDEO) and clip.duration == LENGTH


def test_another_worker_downloads_the_figure_kept_beside_the_video(world, tmp_path, monkeypatch):
    _draw(tmp_path / "worker-a")
    monkeypatch.setattr(figure_cache, "ROOT", tmp_path / "worker-b-figures")
    path, _clip = _draw(tmp_path / "worker-b")
    assert world.opened == 1 and Path(path).read_bytes().startswith(b"mp4:f00000.jpg")
    # A video kept nowhere but on this worker is drawn again on another.
    monkeypatch.setattr(figure_cache, "ROOT", tmp_path / "worker-c-figures")
    _draw(tmp_path / "worker-c", length=3.5, video_id=None)
    monkeypatch.setattr(figure_cache, "ROOT", tmp_path / "worker-d-figures")
    _draw(tmp_path / "worker-d", length=3.5, video_id=None)
    assert world.opened == 3


def test_a_figure_missing_a_picture_the_person_may_read_is_never_kept(world, tmp_path):
    source = _source()
    with_picture = figures.FigureSource(source.kind, source.slide, None, frozenset({PICTURE_ID}))
    world.asks = [PICTURE_ID]

    def draw(workdir: Path, readable: frozenset[str]):
        return asyncio.run(
            figures.draw(
                with_picture,
                _document(with_picture),
                user=None,
                scene=SCENE,
                length=LENGTH,
                words=None,
                fps=FPS,
                viewport=(1920, 1080),
                scale=1,
                workdir=workdir,
                video_id=VIDEO_ID,
                readable=readable,
            )
        )

    # The person may read it, and it could not be read (a storage hiccup): drawn without it, never kept.
    world.missing = {PICTURE_ID}
    draw(tmp_path / "storage-hiccup", frozenset({PICTURE_ID}))
    assert not world.store.objects
    # A picture the person may not read is drawn without it every time: what is drawn is kept.
    draw(tmp_path / "not-theirs", frozenset())
    assert len(world.store.objects) == 1
    world.missing = set()
    draw(tmp_path / "read", frozenset({PICTURE_ID}))
    assert len(world.store.objects) == 2


async def _fulfil(**_kw):
    return None


def test_a_neighbours_figure_is_drawn_for_the_frames_its_window_shows_only(world, tmp_path):
    # The preview of the scene after it: the figure's last 0.6 s, under the transition.
    path, clip = _draw(tmp_path / "preview-s06", span=(2.4, 3.0))
    assert world.shot == [f"f{index:05d}.jpg" for index in range(24, 30)]
    assert world.encoded[-1] == world.shot and path.endswith("part.mp4") and clip.frame_count == 30
    # Never kept as the whole: the scene's own preview draws the rest, taking those six frames.
    assert not world.store.objects
    world.shot = []
    _draw(tmp_path / "preview-s05")
    assert world.shot == [f"f{index:05d}.jpg" for index in range(24)] and len(world.store.objects) == 1
    # The next neighbour plays the whole video: nothing drawn, no page opened.
    world.shot, opened = [], world.opened
    path, _clip = _draw(tmp_path / "preview-s04", span=(0.0, 0.6))
    assert world.shot == [] and world.opened == opened and path.endswith(figure_cache.VIDEO)


def test_the_frames_a_span_shows_start_with_the_frame_on_screen_at_its_start():
    times = figures.frame_times([[0.3, 1.2]], 3.0, 10)
    shown = figures.frames_in(times, (2.0, 3.0))
    # Nothing moves past 1.3 s: the frame held from there is the one on screen at 2.0 s.
    assert [times[k] for k in shown] == [times[-1]]
    assert figures.frames_in(times, (0.0, 0.25)) == [0, 1]


def test_a_preview_draws_a_neighbours_figure_only_under_its_transition():
    # Scene s05 placed from 10 to 30 s; the preview of s06 draws from 29.4 s on; of s04 up to 10.6 s.
    at = {"start": 10.0, "end": 30.0}
    assert render.figure_span(at, (29.4, 40.0), 30) == (19.3333, 20.0)
    assert render.figure_span(at, (4.0, 10.6), 30) == (0.0, 0.6667)
    # Its own preview draws it whole, and a whole render has no window.
    assert render.figure_span(at, (10.0, 30.0), 30) is None and render.figure_span(at, None, 30) is None


def test_the_drawings_go_with_the_video_and_past_the_count_the_oldest_stored(world, monkeypatch):
    store = world.store
    now = datetime.now(UTC)
    for index in range(figure_cache.KEPT_PER_VIDEO + 3):
        store.objects[figure_cache.object_name(VIDEO_ID, f"k{index:03d}")] = (b"mp4", now - timedelta(minutes=100 - index))
    other = figure_cache.object_name(str(uuid.uuid4()), "kept")
    store.objects[other] = (b"mp4", now)
    asyncio.run(figure_cache._bound(VIDEO_ID))
    mine = sorted(name for name in store.objects if VIDEO_ID in name)
    assert len(mine) == figure_cache.KEPT_PER_VIDEO and mine[0].endswith("k003.mp4") and other in store.objects
    assert asyncio.run(figure_cache.evict(VIDEO_ID)) == figure_cache.KEPT_PER_VIDEO
    assert list(store.objects) == [other]


def test_the_store_sweep_deletes_the_drawings_of_videos_gone(world, monkeypatch):
    from app.core import database

    alive, gone = uuid.uuid4(), uuid.uuid4()
    world.store.objects[figure_cache.object_name(str(alive), "a")] = (b"mp4", datetime.now(UTC))
    world.store.objects[figure_cache.object_name(str(gone), "b")] = (b"mp4", datetime.now(UTC))

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def execute(self, statement):
            asked = {value for value in statement.compile().params.values() for value in (value if isinstance(value, list) else [value])}
            return SimpleNamespace(scalars=lambda: iter([alive] if alive in asked else []))

    monkeypatch.setattr(database, "async_session", _Session)
    assert asyncio.run(figure_cache.sweep_store()) == 1
    assert list(world.store.objects) == [figure_cache.object_name(str(alive), "a")]


def test_a_folder_unused_for_hours_leaves_the_worker():
    used = figure_cache.folder("used")
    stale = figure_cache.folder("stale")
    old = time.time() - figure_cache.KEPT_ON_DISK_SECONDS - 60
    os.utime(stale, (old, old))
    assert figure_cache.sweep_disk() == 1 and used.is_dir() and not stale.exists()
