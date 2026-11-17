"""Video Studio renders stay inside the worker's resources.

A render draws its frames with ffmpeg decoders and x264 encoders next to the
platform's own worker: their threads follow the container's CPU quota, never
the node's core count; a scene's decoder is closed once its scene has ended
and a page never keeps more open than the timeline shows sources at once.
Nothing here launches ffmpeg or a browser (``test_video_studio_render_memory``
renders for real).
"""

import asyncio
import os
import sys

import pytest

from app.services.video_studio import compositor, encoder, resources


@pytest.fixture
def cgroup(monkeypatch, tmp_path):
    """Point the cgroup readers at files of the test's own, on a 32-CPU host."""
    paths = {
        "_CGROUP_V2_CPU_MAX": tmp_path / "cpu.max",
        "_CGROUP_V1_QUOTA": tmp_path / "cpu.cfs_quota_us",
        "_CGROUP_V1_PERIOD": tmp_path / "cpu.cfs_period_us",
        "_CGROUP_V2_MEMORY_MAX": tmp_path / "memory.max",
        "_CGROUP_V1_MEMORY_LIMIT": tmp_path / "memory.limit_in_bytes",
    }
    for name, path in paths.items():
        monkeypatch.setattr(resources, name, str(path))
    monkeypatch.setattr(resources.os, "process_cpu_count", lambda: 32)
    resources.cpu_budget.cache_clear()
    yield paths
    resources.cpu_budget.cache_clear()


def test_a_cgroup_v2_quota_bounds_the_cpu_budget(cgroup):
    cgroup["_CGROUP_V2_CPU_MAX"].write_text("150000 100000\n")
    assert resources.cpu_budget() == 2


def test_an_unlimited_cgroup_v2_quota_leaves_the_host_cpus(cgroup):
    cgroup["_CGROUP_V2_CPU_MAX"].write_text("max 100000\n")
    assert resources.cpu_budget() == 32


def test_a_cgroup_v1_quota_bounds_the_cpu_budget(cgroup):
    cgroup["_CGROUP_V1_QUOTA"].write_text("400000\n")
    cgroup["_CGROUP_V1_PERIOD"].write_text("100000\n")
    assert resources.cpu_budget() == 4


def test_an_unset_cgroup_v1_quota_leaves_the_host_cpus(cgroup):
    cgroup["_CGROUP_V1_QUOTA"].write_text("-1\n")
    cgroup["_CGROUP_V1_PERIOD"].write_text("100000\n")
    assert resources.cpu_budget() == 32


def test_without_a_cgroup_the_budget_is_the_host_cpus(cgroup):
    assert resources.cpu_budget() == 32


def test_a_quota_under_one_cpu_still_leaves_one(cgroup):
    cgroup["_CGROUP_V2_CPU_MAX"].write_text("20000 100000\n")
    assert resources.cpu_budget() == 1


def test_the_container_memory_limit_is_read_from_either_cgroup(cgroup):
    assert resources.memory_limit() is None
    cgroup["_CGROUP_V1_MEMORY_LIMIT"].write_text(str(9223372036854771712))
    assert resources.memory_limit() is None  # v1's "no limit"
    cgroup["_CGROUP_V1_MEMORY_LIMIT"].write_text(str(4 << 30))
    assert resources.memory_limit() == 4 << 30
    cgroup["_CGROUP_V2_MEMORY_MAX"].write_text("max\n")
    assert resources.memory_limit() is None
    cgroup["_CGROUP_V2_MEMORY_MAX"].write_text(f"{8 << 30}\n")
    assert resources.memory_limit() == 8 << 30


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_a_process_tree_and_its_memory_are_measured():
    pids = resources.process_tree(os.getpid())
    assert pids[0] == os.getpid()
    memory = resources.tree_memory(os.getpid())
    assert memory is not None and memory > 10 << 20
    assert resources.snapshot_tree(os.getpid())[os.getpid()] == resources.process_start(os.getpid())


def test_every_x264_encode_runs_on_the_cpu_budget(monkeypatch):
    monkeypatch.setattr(encoder, "cpu_budget", lambda: 3)
    args = encoder.x264_args(crf=16, preset="slow", fps=60)
    assert args[args.index("-threads") + 1] == "3"


async def test_a_frame_decoder_decodes_and_encodes_on_one_thread(monkeypatch):
    seen: list[tuple] = []

    async def spawn(*args, **_kwargs):
        seen.append(args)
        raise RuntimeError("no ffmpeg in a unit test")

    monkeypatch.setattr(encoder, "ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(encoder.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(RuntimeError):
        await encoder.FrameDecoder("clip.mp4", start=2.0).open()
    args = list(seen[0])
    input_at = args.index("-i")
    threads = [i for i, arg in enumerate(args) if arg == "-threads"]
    assert [args[i + 1] for i in threads] == ["1", "1"]
    assert threads[0] < input_at < threads[1]


# ── The timeline decides what is open ─────────────────────────────────────


def _scene(sid: str, kind: str, start: float, end: float, *, src: str | None = None, backdrop: str | None = None):
    scene = {"id": sid, "type": kind, "start": start, "end": end}
    if src:
        scene["src"] = src
    if backdrop:
        scene["backdrop"] = {"src": backdrop, "kind": "video"}
    return scene


def _document(*scenes: dict, fps: int = 10) -> dict:
    return {"fps": fps, "frames": int(scenes[-1]["end"] * fps), "scenes": list(scenes)}


def test_each_scene_is_released_from_the_first_frame_after_it_ended():
    doc = _document(
        _scene("t", "title", 0, 3),
        _scene("a", "clip", 2.2, 8, src="clip/a"),
        _scene("s", "statement", 7.2, 12, backdrop="backdrop/s"),
        _scene("b", "clip", 11.2, 20, src="clip/b"),
    )
    # The title shows no media; the last scene stays on screen to the end.
    assert compositor.release_schedule(doc) == [(81, ["clip/a"]), (121, ["backdrop/s"])]


def test_a_crossfade_needs_two_decoders_and_never_more():
    doc = _document(
        _scene("a", "clip", 0, 8, src="clip/a"),
        _scene("s", "statement", 7.2, 12, backdrop="backdrop/s"),
        _scene("b", "clip", 11.2, 20, src="clip/b"),
        _scene("c", "clip", 20, 30, src="clip/c"),
    )
    assert compositor.decoder_budget(doc) == 2


def test_three_sources_on_screen_at_once_raise_the_budget():
    doc = _document(
        _scene("a", "clip", 0, 5, src="clip/a"),
        _scene("b", "clip", 4.5, 5.5, src="clip/b"),
        _scene("c", "clip", 4.8, 9, src="clip/c"),
    )
    assert compositor.decoder_budget(doc) == 3


def test_the_scenes_being_drawn_are_named_in_timeline_order():
    doc = _document(
        _scene("a", "clip", 0, 8, src="clip/a"),
        _scene("s", "statement", 7.2, 12, backdrop="backdrop/s"),
        _scene("b", "clip", 11.2, 20, src="clip/b"),
    )
    assert compositor.scenes_at(doc, [75, 150]) == ["scene 1 (clip)", "scene 2 (statement)", "scene 3 (clip)"]
    assert compositor.scenes_at(doc, [400]) == ["scene 3 (clip)"]


class _FakeDecoder:
    opened: list[str] = []
    closed: list[str] = []

    def __init__(self, source: compositor.FrameSource, keep: tuple[int, int] | None = None) -> None:
        self.path = source.path
        _FakeDecoder.opened.append(source.path)

    async def frame(self, index: int) -> bytes:
        return b"\xff\xd8" + str(index).encode() + b"\xff\xd9"

    async def close(self) -> None:
        _FakeDecoder.closed.append(self.path)


class _Route:
    def __init__(self, path: str) -> None:
        self.request = type("Request", (), {"url": f"{compositor.ORIGIN}/{path}"})()
        self.status: int | None = None

    async def fulfill(self, *, status: int, **_kwargs) -> None:
        self.status = status

    async def abort(self, _reason: str) -> None:
        self.status = -1


@pytest.fixture
def server(monkeypatch, tmp_path):
    _FakeDecoder.opened, _FakeDecoder.closed = [], []
    monkeypatch.setattr(compositor, "_SceneDecoder", _FakeDecoder)
    still = tmp_path / "slide.png"
    still.write_bytes(b"png")
    assets = compositor.CompositorAssets(
        sources={f"clip/{name}": compositor.FrameSource(f"{name}.mp4", 24.0) for name in "abc"},
        images={"image/s": str(still)},
    )
    return compositor.FrameServer(assets, "<html></html>", max_open=2)


async def _ask(server: compositor.FrameServer, path: str) -> int | None:
    route = _Route(path)
    await server.handle(route)
    return route.status


async def test_a_scene_that_ended_has_its_decoder_closed_and_its_still_dropped(server):
    assert await _ask(server, "clip/a/0.jpg") == 200
    assert await _ask(server, "image/s") == 200
    await server.release(["clip/a", "image/s"])
    assert _FakeDecoder.closed == ["a.mp4"] and server.open_decoders == 0
    assert server._images == {}


async def test_a_page_never_keeps_more_decoders_open_than_its_budget(server):
    for name in "abc":
        assert await _ask(server, f"clip/{name}/0.jpg") == 200
    assert server.open_decoders == 2 and server.peak_open == 2
    # The one used least recently made room: it reopens when its scene asks again.
    assert _FakeDecoder.closed == ["a.mp4"]
    assert await _ask(server, "clip/a/1.jpg") == 200
    assert _FakeDecoder.opened == ["a.mp4", "b.mp4", "c.mp4", "a.mp4"]


async def test_a_decoder_is_never_closed_in_the_middle_of_a_frame(server, monkeypatch):
    started, finish = asyncio.Event(), asyncio.Event()

    async def slow_frame(self, index: int) -> bytes:
        started.set()
        await finish.wait()
        return b"\xff\xd8\xff\xd9"

    monkeypatch.setattr(_FakeDecoder, "frame", slow_frame)
    asking = asyncio.create_task(_ask(server, "clip/a/4.jpg"))
    await started.wait()
    releasing = asyncio.create_task(server.release(["clip/a"]))
    await asyncio.sleep(0.01)
    assert _FakeDecoder.closed == []
    finish.set()
    assert await asking == 200
    await releasing
    assert _FakeDecoder.closed == ["a.mp4"]
