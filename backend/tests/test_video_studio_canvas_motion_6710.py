"""A recording's canvases measured on their own pixels, and the GL they were drawn with.

The OpenCTI graph's layout moves were captured at 5 to 16 fps under SwiftShader and played stepped, never said: the
whole frame's rate was judged, over stretches longer than a second. Here each canvas a step showed is measured on
its own pixels (``capture_rate.canvas_motion``), its transitions kept at their step in the action log and one under
20 fps said like a stretch of the whole frame, naming the GL it was drawn with; and the recording Chromium draws on a
GPU where Chromium finds one (``capture_rate.gl_backend``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.services.orchestration_adapters.video_studio import VideoTakeAdapter
from app.services.video_studio import capture_rate, timeline
from app.services.video_studio.quality import ContentFacts, content_findings
from app.services.video_studio.spec import authored_spec, parse_video_spec

#: A 800x450 CSS px window at pixel ratio 2; the canvas stands at (100, 50), 500x300 CSS px.
VIEWPORT_WIDTH = 800
CANVAS = ["canvas#graph", [100.0, 50.0, 500.0, 300.0]]


def _frames(workdir: Path, *, painted: float, moves_every: int, seconds: float, start: float = 1.1) -> list[tuple[float, str]]:
    """Frames painted *painted* times a second for *seconds* from *start*: a spinner outside the canvas changing on
    every one, a block inside the canvas moving on every *moves_every*-th (``0``: never)."""
    out = []
    for index in range(round(seconds * painted)):
        picture = np.full((900, 1600, 3), 12, dtype=np.uint8)
        picture[760:840, 1300 + (index % 4) * 20 : 1380 + (index % 4) * 20] = (200, 60, 60)
        step = index // moves_every if moves_every else 0
        left = 240 + (step * 40) % 760
        picture[300:420, left : left + 160] = (220, 230, 240)
        name = f"f{index:05d}.jpg"
        Image.fromarray(picture).save(workdir / name, quality=92)
        out.append((round(start + index / painted, 4), name))
    return out


def _events(**click) -> list[dict]:
    return [
        {"step": 0, "action": "wait", "t0": 0.0, "ta": 0.0, "t1": 1.0, "ok": True, "canvases": [CANVAS]},
        {"step": 1, "action": "click", "label": "Layered layout", "t0": 1.0, "ta": 1.05, "t1": 1.1, "ok": True, "canvases": [CANVAS], **click},
        {"step": 2, "action": "wait", "t0": 2.6, "ta": 2.6, "t1": 3.6, "ok": True, "canvases": [CANVAS]},
    ]


def test_a_canvas_stepping_at_10_fps_while_its_page_paints_at_60_is_said_at_its_step(tmp_path):
    """The page paints at 60 fps (a spinner), so the whole frame is never under the floor; its canvas moves on every
    sixth frame: a 10 fps transition, on the click's event and said low, naming the canvas and the software GL."""
    frames = _frames(tmp_path, painted=60, moves_every=6, seconds=1.2)
    events = _events()
    recorded = {"gl": {"gpu": False, "why": "Chromium found no GPU to draw WebGL on: webgl unavailable_software"}}
    captured = capture_rate.measure(
        frames, events, duration=3.6, fps=30, workdir=str(tmp_path), recorded=recorded, viewport_width=VIEWPORT_WIDTH
    )

    [run] = events[1]["canvas_motion"]
    assert run["canvas"] == "canvas#graph" and run["fps"] == pytest.approx(10.0, abs=0.6)
    assert run["frames"] >= capture_rate.CANVAS_FRAMES
    [low] = captured["low"]
    assert (low["step"], low["canvas"], low["gl"]) == (1, "canvas#graph", "software")
    seconds = run["t"][1] - run["t"][0]
    assert capture_rate.low_words(captured) == [
        f"low capture rate at step 1 (canvas#graph moved at {low['fps']:.0f} fps for {seconds:.1f} s, software GL)"
    ]
    assert "VIDEO_STUDIO_RECORDING_GL" in capture_rate.software_words(captured)
    assert capture_rate.canvas_words(events[1]["canvas_motion"]).startswith("canvas#graph moved 1.")


def test_a_canvas_moving_at_30_fps_is_logged_never_said_low(tmp_path):
    frames = _frames(tmp_path, painted=60, moves_every=2, seconds=1.2)
    events = _events()
    captured = capture_rate.measure(frames, events, duration=3.6, fps=30, workdir=str(tmp_path), viewport_width=VIEWPORT_WIDTH)
    [run] = events[1]["canvas_motion"]
    assert run["fps"] == pytest.approx(30.0, abs=1.5)
    assert captured["low"] == []


def test_a_canvas_repainted_under_the_moving_cursor_is_no_transition(tmp_path):
    """A hover moves what a canvas draws while the cursor glides over it: those frames are the pointer's."""
    frames = _frames(tmp_path, painted=60, moves_every=6, seconds=1.2)
    cursor = [[1.0 + i * 0.02, 100.0 + i * 4, 200.0] for i in range(70)]
    assert capture_rate.canvas_motion(frames, _events(), workdir=str(tmp_path), viewport_width=VIEWPORT_WIDTH, cursor=cursor) == []
    assert capture_rate.pointer_spans(cursor) == [(1.0, pytest.approx(1.0 + 69 * 0.02 + capture_rate.POINTER_TAIL))]
    # A cursor resting (the same point sampled) moves nothing.
    assert capture_rate.pointer_spans([[1.0, 50.0, 50.0], [1.1, 50.0, 50.0]]) == []


def test_a_scroll_a_short_move_or_a_step_with_no_canvas_is_no_transition(tmp_path):
    frames = _frames(tmp_path, painted=60, moves_every=6, seconds=1.2)
    # A scroll moves the canvas, a wheel zooms it: drawn again at the take's rate (scroll_pacing), judged there.
    scrolled = _events(scrolls=[{"t": [1.0, 2.5], "samples": []}])
    assert capture_rate.canvas_motion(frames, scrolled, workdir=str(tmp_path), viewport_width=VIEWPORT_WIDTH) == []
    # A CSS transition beside it (a button's ripple) leaves the canvas's own painted rate to measure.
    rippled = _events(motion=[{"t": [1.0, 1.4], "element": "button#relayout"}])
    [run] = capture_rate.canvas_motion(frames, rippled, workdir=str(tmp_path), viewport_width=VIEWPORT_WIDTH)
    assert run.fps == pytest.approx(10.0, abs=0.6)
    # No canvas logged on the step: nothing is cropped.
    bare = [{key: value for key, value in event.items() if key != "canvases"} for event in _events()]
    assert capture_rate.canvas_motion(frames, bare, workdir=str(tmp_path), viewport_width=VIEWPORT_WIDTH) == []
    # 0.3 s of motion: under CANVAS_SECONDS.
    short = _frames(tmp_path, painted=60, moves_every=2, seconds=0.3)
    assert capture_rate.canvas_motion(short, _events(), workdir=str(tmp_path), viewport_width=VIEWPORT_WIDTH) == []
    # Without the window's width (a direct call), no canvas is measured.
    assert capture_rate.canvas_motion(frames, _events(), workdir=str(tmp_path), viewport_width=0) == []


def test_a_canvas_stretch_the_whole_frame_already_says_is_said_once(tmp_path):
    """Only the canvas paints, at 8 fps for 1.2 s: the whole frame is under the floor there, and says it once."""
    out = []
    for index in range(10):
        picture = np.full((900, 1600, 3), 12, dtype=np.uint8)
        picture[300:420, 240 + index * 40 : 400 + index * 40] = (220, 230, 240)
        Image.fromarray(picture).save(tmp_path / f"g{index:03d}.jpg", quality=92)
        out.append((round(1.1 + index / 8, 4), f"g{index:03d}.jpg"))
    events = _events()
    captured = capture_rate.measure(out, events, duration=3.6, fps=30, workdir=str(tmp_path), viewport_width=VIEWPORT_WIDTH)
    [low] = captured["low"]
    assert "canvas" not in low and low["step"] == 1
    assert events[1]["canvas_motion"][0]["fps"] == pytest.approx(8.0, abs=0.6)


# ── The GL a recording draws with ─────────────────────────────────────────


@pytest.fixture
def fresh_backend(monkeypatch):
    monkeypatch.setattr(capture_rate, "_BACKEND", [])


@pytest.mark.parametrize(
    ("webgl", "gpu"), [("enabled", True), ("enabled_readback", True), ("unavailable_software", False), ("unavailable_off", False)]
)
def test_auto_draws_on_the_gpu_only_where_chromium_says_webgl_is_drawn_there(monkeypatch, fresh_backend, webgl, gpu):
    probed: list = []

    async def probe(args):
        probed.append(args)
        return {"webgl": webgl, "canvas_2d": webgl}

    monkeypatch.setattr(capture_rate, "_probe", probe)
    monkeypatch.setattr(capture_rate.settings, "video_studio_recording_gl", "auto")
    backend = asyncio.run(capture_rate.gl_backend())
    assert probed == [capture_rate.GPU_GL_ARGS]
    assert backend.gpu is gpu
    assert backend.args == (capture_rate.GPU_GL_ARGS if gpu else capture_rate.SOFTWARE_GL_ARGS)
    if not gpu:
        # The refusal names what the launch read, its renderer included.
        assert backend.why == f"Chromium found no GPU to draw WebGL on: webgl {webgl}, renderer unknown"
    # Decided once per process.
    assert asyncio.run(capture_rate.gl_backend()) is backend and len(probed) == 1


def test_software_never_looks_for_a_gpu_and_a_probe_that_fails_is_asked_again(monkeypatch, fresh_backend):
    async def probe(_args):
        raise RuntimeError("Executable doesn't exist")

    monkeypatch.setattr(capture_rate, "_probe", probe)
    monkeypatch.setattr(capture_rate.settings, "video_studio_recording_gl", "software")
    backend = asyncio.run(capture_rate.gl_backend())
    assert (backend.args, backend.gpu, backend.why) == (capture_rate.SOFTWARE_GL_ARGS, False, "VIDEO_STUDIO_RECORDING_GL=software")

    monkeypatch.setattr(capture_rate, "_BACKEND", [])
    monkeypatch.setattr(capture_rate.settings, "video_studio_recording_gl", "auto")
    failed = asyncio.run(capture_rate.gl_backend())
    assert failed.args == capture_rate.SOFTWARE_GL_ARGS and failed.why.startswith("the GPU could not be probed")
    assert capture_rate._BACKEND == []


def test_a_take_keeps_what_its_own_browser_drew_with():
    gpu = capture_rate.GlBackend(capture_rate.GPU_GL_ARGS, gpu=True)
    assert capture_rate.drawn_with(gpu, {"webgl": "enabled", "canvas_2d": "enabled"}) == {
        "gpu": True,
        "webgl": "enabled",
        "canvas_2d": "enabled",
    }
    lost = capture_rate.drawn_with(gpu, {"webgl": "unavailable_software", "canvas_2d": "unavailable_software"})
    assert lost["gpu"] is False and "off the GPU it was launched for" in lost["why"]
    assert capture_rate.drawn_with(gpu, None) == {"gpu": True}
    assert capture_rate.software_words({"gl": {"gpu": True}}) == "" and capture_rate.software_words({}) == ""


def test_the_gpu_flags_and_the_status_read_are_chromiums_own():
    """No renderer name is read: the feature status Chromium reports (``SystemInfo.getInfo``) decides."""
    assert "--use-angle=vulkan" in capture_rate.GPU_GL_ARGS and "--use-angle=swiftshader-webgl" in capture_rate.SOFTWARE_GL_ARGS

    class _Session:
        async def send(self, method):
            assert method == "SystemInfo.getInfo"
            return {"gpu": {"featureStatus": {"webgl": "enabled", "2d_canvas": "enabled_on"}}}

        async def detach(self):
            return None

    class _Browser:
        async def new_browser_cdp_session(self):
            return _Session()

    status = asyncio.run(capture_rate.gl_status(_Browser()))
    assert status == {"webgl": "enabled", "canvas_2d": "enabled_on"} and capture_rate.on_gpu(status)


@pytest.mark.parametrize(("concurrency", "claims"), [(0, False), (1, True), (2, True)])
def test_a_worker_with_no_take_slot_records_nothing(monkeypatch, concurrency, claims):
    """``VIDEO_STUDIO_TAKE_CONCURRENCY=0``: the recording lane kept to the workers that record (a GPU pool)."""
    monkeypatch.setattr("app.core.config.settings.video_studio_take_concurrency", concurrency)
    adapter = VideoTakeAdapter()
    assert adapter.claims_here is claims
    assert adapter.concurrency_limit == max(1, concurrency)


TAKE = "00000000-0000-0000-0000-000000006710"


@pytest.mark.parametrize("software", [True, False])
def test_a_low_canvas_transition_is_a_save_finding_naming_a_gpu_worker_when_drawn_on_software_gl(software):
    events = [
        {"step": 0, "action": "click", "t0": 0.5, "ta": 1.0, "t1": 1.1, "ok": True, "box": {"x": 100, "y": 100, "w": 80, "h": 30}},
        {"step": 1, "action": "wait", "t0": 1.2, "ta": 1.2, "t1": 7.2, "ok": True},
    ]
    take = timeline.TakeMedia(TAKE, 8.0, 1600, 900, 2.0, 60, 480, events=events)
    take.low = [
        {"t": [1.2, 2.0], "fps": 9.8, "step": 1, "canvas": "canvas#graph", **({"gl": "software"} if software else {})}
    ]
    authored = authored_spec({"title": "Graph", "freestyle": True, "scenes": [{"id": "r", "type": "recording", "take_id": TAKE}]})
    spec = parse_video_spec(authored)
    plan = timeline.estimate_plan(spec, {TAKE: take})
    facts = ContentFacts(takes={TAKE: take}, ready_takes={TAKE})
    [found] = [f for f in content_findings(spec, plan.document, facts, authored) if f.kind == "take_capture_rate"]
    gl = ", software GL" if software else ""
    assert f"low capture rate at step 1 (canvas#graph moved at 10 fps for 0.8 s{gl})" in found.message
    assert ("a worker whose Chromium draws on a GPU" in found.message) is software
    assert ("record it again (record_browser_demo measures" in found.message) is not software
    # Or repaired without recording again.
    assert "repair=true) makes a new take" in found.message
