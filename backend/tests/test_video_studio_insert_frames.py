"""A screen insert's frames drawn once per worker and its pipeline's work done once. Synthetic frames only.

A 10 s scene with one screen insert took 12 minutes to preview at 1080p: every page redid the whole insert pipeline
for every frame - its bezel, its warp, its five-frame matte, its light -, and a page starting elsewhere, an attempt
run again or the next preview of the same revision did it all again.

* a frame is kept on the worker's disk with its audit, keyed by what its pixels are drawn from and never by when it
  shows, and read back by the next page or attempt - nothing composed, the same bytes, the same audits; one drawn
  without the pictures a failing audit shows is drawn again when they are wanted;
* a static insert's bezel is found once, on its first footage frame; a tracked one's on every frame;
* each footage frame's matte is read once, whatever frames show it, and a page starting mid-insert reads the frames
  behind it so it draws the frame a page drawing on would;
* the folders are held within their room and swept once unused;
* the frames read back price the render's own schedule, never what the next pre-flights read.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from app.core.config import settings
from app.services.video_studio import (
    compositor,
    insert_frames,
    render_history,
    render_preflight,
    screen_insert,
    screens,
)

W, H = 640, 360
QUAD = ((0.30, 0.22), (0.72, 0.22), (0.72, 0.62), (0.30, 0.62))
MONITOR = (int(0.30 * W), int(0.22 * H), int(0.72 * W), int(0.62 * H))


def _room(frame: int, *, green: bool = False) -> np.ndarray:
    """A room with a monitor (dark, or a green key), and a hand coming in over the monitor's left edge from frame 4."""
    picture = np.zeros((H, W, 3), dtype=np.uint8)
    picture[...] = np.linspace(40, 120, W, dtype=np.uint8)[None, :, None]
    x0, y0, x1, y1 = MONITOR
    picture[y0:y1, x0:x1] = (0, 177, 64) if green else (16, 20, 24)
    reach = max(0, frame - 4) * 12
    if reach:
        picture[y0 + 40 : y0 + 70, x0 - 40 : x0 - 40 + reach] = (210, 160, 130)
    return picture


def _insert(**fields) -> screen_insert.InsertSource:
    values = {"path": "insert.png", "kind": "image", "quad": QUAD, "label": "Scene s1: its screen insert 0"}
    values.update(fields)
    insert = screen_insert.InsertSource(**values)
    return dataclasses.replace(insert, box=tuple(screen_insert.grown_box(insert.quad, W / H)))


def _picture() -> Image.Image:
    rng = np.random.default_rng(3)
    return Image.fromarray(rng.integers(0, 255, (300, 400, 3), dtype=np.uint8), "RGB")


# ── Kept on the worker's disk ─────────────────────────────────────────────


def _ffmpeg_media(tmp_path: Path) -> tuple[Path, Path]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is missing")
    x0, y0, x1, y1 = MONITOR
    footage, source = tmp_path / "footage.mp4", tmp_path / "insert.mp4"
    for path, lavfi in (
        (footage, f"testsrc2=size={W}x{H}:rate=24,drawbox=x={x0}:y={y0}:w={x1 - x0}:h={y1 - y0}:color=0x101418@1:t=fill"),
        (source, "mandelbrot=size=320x180:rate=24"),
    ):
        subprocess.run(  # noqa: S603
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", lavfi, "-t", "1", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)],
            check=True,
            timeout=120,
        )
    return footage, source


class _Route:
    def __init__(self, url: str) -> None:
        self.request = SimpleNamespace(url=url)
        self.answer: dict = {}

    async def fulfill(self, **kwargs):
        self.answer = kwargs


async def _serve(server: compositor.FrameServer, key: str, frames: range) -> list[bytes]:
    bodies = []
    for n in frames:
        server.start_frame()
        route = _Route(f"{compositor.ORIGIN}/{key}/{n}.png?w={W}&h={H}&f={n}&n={n}")
        await server.handle(route)
        assert route.answer["status"] == 200
        bodies.append(route.answer["body"])
    return bodies


def test_a_page_or_an_attempt_after_the_first_reads_each_insert_frame_back_with_its_audit(tmp_path, monkeypatch):
    footage, source = _ffmpeg_media(tmp_path)
    key = "insert/s1.0"
    insert = _insert(
        path=str(source),
        kind="video",
        fps=24.0,
        footage=screen_insert.Footage(str(footage), "video", 24.0, "clip/s1"),
    )
    assets = compositor.CompositorAssets(sources={"clip/s1": compositor.FrameSource(str(footage), 24.0)}, inserts={key: insert})
    evidence = tmp_path / compositor.SEGMENTS_DIR / compositor.EVIDENCE_DIR
    first = compositor.FrameServer(assets, "<html></html>", max_open=3, evidence_dir=evidence)
    drawn = asyncio.run(_serve(first, key, range(12)))
    audits = first.take_audits(0, 12)
    assert (first.insert_drawn, first.insert_cached) == (12, 0) and set(audits[key]["frames"]) == {str(n) for n in range(12)}
    asyncio.run(first.close())

    # Another page, an attempt run again, the next preview: nothing composed, the frames and their audits as drawn.
    def composed(*_args, **_kwargs):
        raise AssertionError("an insert frame kept on the worker's disk is never composed again")

    monkeypatch.setattr(screen_insert, "compose", composed)
    second = compositor.FrameServer(assets, "<html></html>", max_open=3, evidence_dir=evidence)
    read = asyncio.run(_serve(second, key, range(12)))
    assert read == drawn and (second.insert_cached, second.insert_drawn) == (12, 0) and second.frame_reused
    assert second.take_audits(0, 12) == audits
    asyncio.run(second.close())


def test_a_frame_drawn_without_its_evidence_is_drawn_again_when_its_evidence_is_wanted(tmp_path):
    frames = insert_frames.Frames("drawing")
    failing = screen_insert.FrameAudit(covered=0.5)
    assert failing.failing
    proof = Image.new("RGB", (40, 30), (200, 0, 0))
    # Drawn uncaptured before a piece (no evidence written): served to a page that writes none, never to one that does.
    frames.put("3-3-640x360-1.0000", b"png", None, failing, {}, evidenced=False)
    assert frames.get("3-3-640x360-1.0000", evidence=False).audit["covered"] == 0.5
    assert frames.get("3-3-640x360-1.0000", evidence=True) is None
    # Drawn with it: read back with its pictures, which the frame server copies as its own evidence.
    frames.put("3-3-640x360-1.0000", b"png", b"glow", failing, {"proof": proof, "halo_crop": None}, evidenced=True)
    kept = frames.get("3-3-640x360-1.0000", evidence=True)
    assert kept.drawn == b"png" and kept.glow == b"glow" and set(kept.evidence) == {"proof"}
    server = compositor.FrameServer(compositor.CompositorAssets(), "<html></html>", evidence_dir=tmp_path / "evidence")
    asyncio.run(server._audited("insert/s1.0", 3, screen_insert.FrameAudit.from_dict(kept.audit), kept.evidence))
    [(_badness, frame, path)] = [server.evidence[mark] for mark in server.evidence]
    assert frame == 3 and Path(path).read_bytes() == kept.evidence["proof"].read_bytes()
    # A frame that passes is served to any page.
    frames.put("4-4-640x360-1.0000", b"png", None, screen_insert.FrameAudit(covered=0.0), {}, evidenced=False)
    assert frames.get("4-4-640x360-1.0000", evidence=True) is not None


def test_what_an_insert_is_drawn_by_keys_its_frames_never_when_it_shows():
    assets = compositor.CompositorAssets(origins={"insert.png": "file:a", "other.png": "file:b"})

    def key(insert: screen_insert.InsertSource) -> str:
        return insert_frames.folder_key(compositor.insert_drawn_by(insert, assets))

    base = _insert()
    # When it shows and what the findings call it: the segments' digest reads them, its pixels do not.
    placed = compositor._insert_inputs(base, assets, [{"t0": 1.0, "t1": 3.0}])
    elsewhere = compositor._insert_inputs(dataclasses.replace(base, label="Scene s9: its screen insert 0"), assets, [{"t0": 7.0, "t1": 9.0}])
    assert placed != elsewhere and key(dataclasses.replace(base, label="Scene s9: its screen insert 0")) == key(base)
    # Its source, quad, light, fit and key are what it is drawn by.
    for changed in (
        dataclasses.replace(base, path="other.png"),
        dataclasses.replace(base, quad=((0.31, 0.22), (0.72, 0.22), (0.72, 0.62), (0.30, 0.62))),
        dataclasses.replace(base, light=0.4),
        dataclasses.replace(base, fit="contain"),
        dataclasses.replace(base, key=screen_insert.KeySpec("green")),
    ):
        assert key(changed) != key(base)
    # A frame of it is its source frame, the footage frame under it, the host's size and a push's scale.
    names = {insert_frames.frame_name(*args) for args in ((3, 3, (W, H), 1.0), (3, 4, (W, H), 1.0), (4, 3, (W, H), 1.0), (3, 3, (1920, 1080), 1.0), (3, 3, (W, H), 1.2), (3, None, (W, H), 1.0))}
    assert len(names) == 6


# ── The pipeline's work, once ─────────────────────────────────────────────


def _counting(monkeypatch, owner, name: str) -> list:
    calls: list = []
    original = getattr(owner, name)

    def counted(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, counted)
    return calls


def test_a_static_inserts_bezel_is_found_once_and_a_tracked_ones_on_every_frame(monkeypatch):
    picture, first = _picture(), _room(0)
    locked = _counting(monkeypatch, screen_insert, "_locked")
    static = _insert()
    lock = screen_insert.static_lock(static, first, (W, H))
    surface = screen_insert.Surface.of(first, [(x * W, y * H) for x, y in QUAD])
    assert lock is not None and len(locked) == 1
    state = screen_insert.Matting()
    for n in range(6):
        layer = screen_insert.compose(static, picture, (W, H), n, footage=_room(n), surface=surface, lock=lock, state=state)
        assert layer.audit.refined == [[round(x / W, 5), round(y / H, 5)] for x, y in lock.found_px]
    assert len(locked) == 1
    # A tracked insert follows its quad frame by frame: its bezel is found on each, and it takes no lock.
    track = SimpleNamespace(quad_at=lambda _frame: list(QUAD), box_quad=lambda: list(static.box))
    tracked = dataclasses.replace(static, track=track)
    assert screen_insert.static_lock(tracked, first, (W, H)) is None
    state = screen_insert.Matting()
    for n in range(6):
        screen_insert.compose(tracked, picture, (W, H), n, footage=_room(n), surface=surface, lock=lock, state=state)
    assert len(locked) == 1 + 6


@pytest.mark.parametrize("keyed", [False, True], ids=["occlusion", "key"])
def test_each_footage_frames_matte_is_read_once_whatever_frames_show_it(monkeypatch, keyed):
    picture = _picture()
    insert = _insert(key=screen_insert.KeySpec("green") if keyed else None)
    if keyed:
        insert = screen_insert.keyed_on(insert, _room(0, green=True))
        # A keyed insert that is tracked keys frame by frame: its matte is read once a footage frame too.
        insert = dataclasses.replace(insert, track=SimpleNamespace(quad_at=lambda _frame: list(QUAD), box_quad=lambda: list(insert.box)))
    reads = _counting(monkeypatch, screen_insert.Surface, "front") if not keyed else None
    keys = _counting(monkeypatch, screens, "key_matte") if keyed else None
    first = _room(0, green=keyed)
    surface = screen_insert.Surface.of(first, [(x * W, y * H) for x, y in QUAD])
    lock = screen_insert.static_lock(insert, first, (W, H))
    state = screen_insert.Matting()
    # 30 output frames a second over 24 footage frames a second: footage frames 0 to 7, some shown twice.
    shown = [int(n * 24 / 30) for n in range(10)]
    last = None
    for footage in shown:
        if footage == last:
            continue
        last = footage
        ahead = [(footage + k, _room(footage + k, green=keyed)) for k in (1, 2)]
        screen_insert.compose(
            insert, picture, (W, H), footage, footage=_room(footage, green=keyed), surface=surface, lock=lock, state=state, footage_ahead=ahead
        )
    read = len(keys if keyed else reads)
    # Each footage frame shown and the two the last one reads ahead: once each.
    assert read == len(set(shown)) + 2, read


def test_a_page_starting_mid_insert_draws_the_frame_a_page_drawing_on_would():
    picture, first = _picture(), _room(0)
    insert = _insert()
    surface = screen_insert.Surface.of(first, [(x * W, y * H) for x, y in QUAD])
    lock = screen_insert.static_lock(insert, first, (W, H))

    def ahead(n: int) -> list:
        return [(n + 1, _room(n + 1)), (n + 2, _room(n + 2))]

    on = screen_insert.Matting()
    drawn = {}
    for n in range(9):
        layer = screen_insert.compose(insert, picture, (W, H), n, footage=_room(n), surface=surface, lock=lock, state=on, footage_ahead=ahead(n))
        drawn[n] = np.asarray(layer.warped.image)
    # A page starting at frame 6 - another page's piece, or after frames read back drawn - reads frames 4 and 5 too.
    fresh = screen_insert.Matting()
    window = [(4, _room(4)), (5, _room(5)), *ahead(6)]
    started = screen_insert.compose(insert, picture, (W, H), 6, footage=_room(6), surface=surface, lock=lock, state=fresh, footage_ahead=window)
    assert np.array_equal(np.asarray(started.warped.image), drawn[6])
    # Without them its matte's window is narrower: a hand coming in is matted otherwise.
    narrow = screen_insert.compose(
        insert, picture, (W, H), 6, footage=_room(6), surface=surface, lock=lock, state=screen_insert.Matting(), footage_ahead=ahead(6)
    )
    assert not np.array_equal(np.asarray(narrow.warped.image), drawn[6])


def test_a_page_reads_behind_only_what_it_has_not_read_and_never_before_the_insert_comes_in(tmp_path):
    insert = dataclasses.replace(_insert(), first_frame=10, footage=screen_insert.Footage("footage.mp4", "video", 24.0, "clip/s1"))
    server = compositor.FrameServer(compositor.CompositorAssets(), "<html></html>")
    asked: list[int] = []

    async def pixels(_found, frame, _size):
        asked.append(frame)
        return _room(frame)

    server._footage_pixels = pixels
    state = screen_insert.Matting()
    assert [n for n, _p in asyncio.run(server._footage_behind(insert, 11, (W, H), state))] == [10]
    state.raw[13] = np.zeros((2, 2), np.float32)
    assert [n for n, _p in asyncio.run(server._footage_behind(insert, 15, (W, H), state))] == [14]
    assert asked == [10, 14]


# ── Held to their room ────────────────────────────────────────────────────


def test_the_folders_are_held_within_their_room_the_least_recently_used_first_and_swept_once_unused(monkeypatch):
    monkeypatch.setattr(settings, "video_studio_insert_frames_mb", 1)
    now = time.time()
    for age, name in ((300, "oldest"), (200, "older"), (100, "newest")):
        folder = insert_frames.ROOT / name
        folder.mkdir(parents=True)
        (folder / "0-0-640x360-1.0000.png").write_bytes(b"x" * (400 << 10))
        os.utime(folder, (now - age, now - age))
    # 1.2 MiB in a room of 1 MiB: the least recently used goes - never the folder being written.
    assert insert_frames.bound(keep=insert_frames.ROOT / "oldest") == 1
    assert sorted(path.name for path in insert_frames.ROOT.iterdir()) == ["newest", "oldest"]
    # Unused past the sweep's hours: gone.
    os.utime(insert_frames.ROOT / "oldest", (now - insert_frames.KEPT_ON_DISK_SECONDS - 60,) * 2)
    assert insert_frames.sweep_disk() == 1 and [path.name for path in insert_frames.ROOT.iterdir()] == ["newest"]


def test_a_drawing_past_the_room_on_its_own_keeps_its_frames_and_takes_no_more(monkeypatch):
    """A long insert at 2160p fills the room alone: it is never removed while it is written, nor let grow past it."""
    monkeypatch.setattr(settings, "video_studio_insert_frames_mb", 1)
    monkeypatch.setattr(insert_frames, "_full", set())
    frames = insert_frames.Frames("long")
    audit = screen_insert.FrameAudit(covered=0.0)
    for n in range(3):
        frames.put(f"{n}-{n}-3840x2160-1.0000", b"x" * (400 << 10), None, audit, {}, evidenced=True)
    assert insert_frames.bound(keep=frames.folder) == 0 and frames.folder in insert_frames._full
    frames.put("3-3-3840x2160-1.0000", b"x" * (400 << 10), None, audit, {}, evidenced=True)
    assert frames.get("3-3-3840x2160-1.0000", evidence=False) is None
    assert frames.get("2-2-3840x2160-1.0000", evidence=False) is not None


def test_a_worker_given_no_room_keeps_no_frame(monkeypatch):
    monkeypatch.setattr(settings, "video_studio_insert_frames_mb", 0)
    frames = insert_frames.Frames("drawing")
    frames.put("0-0-640x360-1.0000", b"png", None, screen_insert.FrameAudit(covered=0.0), {}, evidenced=True)
    assert frames.get("0-0-640x360-1.0000", evidence=False) is None and not frames.folder.exists()


# ── What the next pre-flights read ────────────────────────────────────────


def test_frames_read_back_price_the_renders_schedule_never_the_next_preflights():
    tracker = compositor.DrawProgress()
    for _ in range(40):
        tracker.timed("insert", 2.0, 0, scene="clip+insert")
    for _ in range(60):
        tracker.timed("insert", 0.1, 0, scene="clip+insert", reused=True)
    tracker.insert_cached, tracker.insert_drawn = 60, 40
    # The schedule of this attempt prices a frame as it costs now ...
    assert tracker.kind_frames["insert"] == 100
    # ... the next pre-flights read what a drawn frame costs.
    assert tracker.kinds() == {"insert": [40, 80.0]} and tracker.scenes() == {"clip+insert": [40, 80.0]}
    assert tracker.insert_frames() == {"cached": 60, "drawn": 40, "share": 0.6}
    layout = SimpleNamespace(as_dict=lambda: {"pages": 2, "cpus": 2})
    measured = render_preflight.measurement(layout, frames=100, seconds=40.0, insert_frames=tracker.insert_frames(), scenes=tracker.scenes())
    assert measured["insert_frames"]["cached"] == 60 and render_history.read_back(measured)
    assert render_history.estimate_error({"model": render_preflight.ESTIMATE_MODEL, "raw_seconds": 100.0}, {**measured, "total_seconds": 50.0}) is None
    said = render_preflight.said_of(SimpleNamespace(preflight=None, measured=measured))
    assert "Its screen inserts: 60 frames read back as an earlier page, attempt or render of this worker drew them, 40 drawn." in said
    # A render that read none back is a measure like any.
    assert not render_history.read_back(render_preflight.measurement(layout, frames=100, seconds=40.0))
