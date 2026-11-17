"""Off-scale frames inside idle pauses of a recording.

On a worker drawing canvases on software GL, a take kept, inside pauses where its page did nothing, runs of frames
drawing the whole page at another scale - read by the quality check as stepping motion on a pause step. Here, on
synthetic frames: the recorder's screencast drops a frame painted at another geometry or JPEG size than most of the
take's (``capture._Screencast``) and says each run (``capture_rate.dropped_runs``); a stretch read as motion on a step
holding the page, between two frames alike, is a capture fault (``capture_rate.capture_fault``) - never stepping
motion - named in the take's notes and, where a scene shows it, by the ``off_scale_frames`` finding.
"""

from __future__ import annotations

import asyncio
import base64
import io
from pathlib import Path

import numpy as np
from PIL import Image

from app.services.video_studio import capture, capture_rate, timeline
from app.services.video_studio.quality import ContentFacts, content_findings
from app.services.video_studio.spec import authored_spec, parse_video_spec

FPS = 30
WIDTH, HEIGHT = 320, 180


def _page(*, scale: float = 1.0) -> np.ndarray:
    """A page with a top bar, a toolbar and a few panels; at *scale* the same page enlarged about its centre."""
    big = np.full((HEIGHT, WIDTH, 3), 236, dtype=np.uint8)
    big[0:16, :] = (24, 32, 64)  # the top bar
    big[HEIGHT - 20 : HEIGHT, 40:280] = (60, 60, 60)  # the bottom toolbar
    for k in range(5):
        x = 20 + 58 * k
        big[40:130, x : x + 40] = (40 + 40 * k, 120, 200 - 30 * k)
    if scale == 1.0:
        return big
    image = Image.fromarray(big).resize((round(WIDTH * scale), round(HEIGHT * scale)), Image.Resampling.BILINEAR)
    left, top = (image.width - WIDTH) // 2, (image.height - HEIGHT) // 2
    return np.asarray(image.crop((left, top, left + WIDTH, top + HEIGHT)))


def _frames(workdir: Path, shots: list[tuple[float, float]]) -> list[tuple[float, str]]:
    """``(t, scale)`` each written as a frame of the take."""
    out = []
    for index, (t, scale) in enumerate(shots):
        name = f"f{index:06d}.jpg"
        Image.fromarray(_page(scale=scale)).save(workdir / name, quality=92)
        out.append((round(t, 4), name))
    return out


def _events() -> list[dict]:
    return [
        {"step": 0, "action": "click", "label": "Layout", "t0": 0.0, "ta": 0.3, "t1": 0.5, "ok": True},
        {"step": 1, "action": "pause", "t0": 0.5, "ta": 0.5, "t1": 4.0, "ok": True},
    ]


#: The page at rest painted at 30 fps over the click; still, the screencast paints nothing for more than half a second,
#: then, inside the pause, a frame at the take's scale, two drawing the page 1.3x larger, one at the take's scale again.
REST = [(k / 30, 1.0) for k in range(15)]
PAUSE = [(1.0, 1.0), (1.15, 1.3), (1.3, 1.3), (1.45, 1.0)]


def test_two_off_scale_frames_in_a_pause_are_a_capture_fault_never_stepping_motion(tmp_path):
    frames = _frames(tmp_path, [*REST, *PAUSE])
    captured = capture_rate.measure(frames, _events(), duration=4.0, fps=FPS, workdir=str(tmp_path))

    assert captured["stepping"] == []
    [fault] = captured["off_scale"]
    assert (fault["step"], fault["action"], fault["count"]) == (1, "pause", 2)
    assert fault["frames"] == [frames[16][1], frames[17][1]]
    assert fault["t"] == [1.15, 1.45]
    [said] = capture_rate.fault_words(captured)
    assert said == "off-scale frames at step 1 (pause, 1.1-1.4 s, 0.3 s, 2 frame(s))"
    notes = capture_rate.motion_notes(captured)
    assert not any("stepping motion" in note.lower() for note in notes)
    assert notes[-1] == (
        "Capture fault, not a motion of the page - off-scale frames at step 1 (pause, 1.1-1.4 s, 0.3 s, 2 frame(s)): the "
        "frames about each run are alike, the page did not move. Record the take again."
    )


def test_the_same_change_on_a_moving_step_stays_stepping_motion(tmp_path):
    """A click, a scroll or a navigation moves the page: only a step holding it can carry a capture fault."""
    frames = _frames(tmp_path, [*REST, *PAUSE])
    events = _events()
    events[1]["action"] = "click"
    captured = capture_rate.measure(frames, events, duration=4.0, fps=FPS, workdir=str(tmp_path))
    assert captured["off_scale"] == []
    assert [stretch["step"] for stretch in captured["stepping"]] == [1]


def test_a_page_that_ends_elsewhere_is_no_capture_fault(tmp_path):
    """Frames about the stretch that differ (the page really changed during the wait) keep it stepping motion."""
    frames = _frames(tmp_path, [*REST, (1.0, 1.0), (1.15, 1.3), (1.3, 1.15), (1.45, 1.3)])
    captured = capture_rate.measure(frames, _events(), duration=4.0, fps=FPS, workdir=str(tmp_path))
    assert captured["off_scale"] == []
    assert [stretch["step"] for stretch in captured["stepping"]] == [1]


def _jpeg(width: int, height: int) -> str:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 200, 200)).save(out, format="JPEG", quality=80)
    return base64.b64encode(out.getvalue()).decode("ascii")


class _Cdp:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def on(self, _event: str, _handler) -> None:
        return None

    async def send(self, method: str, _params: dict | None = None) -> None:
        self.sent.append(method)


async def test_the_screencast_drops_a_frame_of_another_scale_or_size(tmp_path):
    screencast = capture._Screencast(_Cdp(), str(tmp_path), max_frames=100)
    screencast.t_start = 1000.0
    take = {"deviceWidth": 1600, "deviceHeight": 900, "pageScaleFactor": 1, "offsetTop": 0}
    shots = [
        (1000.1, take, (320, 180)),
        (1000.2, take, (320, 180)),
        (1000.3, {**take, "pageScaleFactor": 1.3}, (320, 180)),  # another declared scale
        (1000.4, take, (240, 180)),  # another size: a frame clipped to a canvas
        (1000.5, take, (320, 180)),
        (1000.6, take, (320, 180)),
    ]
    for stamp, metadata, size in shots:
        screencast._on_frame({"sessionId": 1, "metadata": {**metadata, "timestamp": stamp}, "data": _jpeg(*size)})
    await asyncio.sleep(0)
    await screencast.stop()

    assert [name for _t, name in screencast.frames] == ["f000000.jpg", "f000001.jpg", "f000004.jpg", "f000005.jpg"]
    assert screencast.frame_size == (320, 180)
    assert [round(t, 3) for t, _why in screencast.dropped] == [0.3, 0.4]
    assert screencast.dropped[0][1] == "pageScaleFactor 1.3 for 1"
    assert screencast.dropped[1][1] == "240x180 px for 320x180"
    # What a hold reads its canvases on is never a frame of another declared scale.
    assert screencast.latest is not None and screencast.latest[0] == screencast.frames[-1][0]

    events = [{"step": 0, "action": "click", "t0": 0.0}, {"step": 1, "action": "pause", "t0": 0.25}]
    runs = capture_rate.dropped_runs(screencast.dropped, events)
    assert runs == [{"t": [0.3, 0.4], "frames": 2, "step": 1, "why": "pageScaleFactor 1.3 for 1"}]
    assert capture_rate.dropped_words(runs) == (
        "Frames the browser painted at another size or scale were dropped, the frame before held in their place: "
        "step 1 (0.3-0.4 s, 2 frame(s): pageScaleFactor 1.3 for 1)."
    )
    assert capture_rate.dropped_words([]) == ""


TAKE = "00000000-0000-0000-0000-000000006907"
FAULT = {"t": [103.0, 103.4], "step": 24, "action": "pause", "frames": ["f003090.jpg", "f003093.jpg"], "count": 2}


def _findings(take: timeline.TakeMedia, **scene) -> list:
    authored = authored_spec(
        {"title": "Graph", "freestyle": True, "scenes": [{"id": "r", "type": "recording", "take_id": TAKE, **scene}]}
    )
    spec = parse_video_spec(authored)
    plan = timeline.estimate_plan(spec, {TAKE: take})
    facts = ContentFacts(takes={TAKE: take}, ready_takes={TAKE})
    return content_findings(spec, plan.document, facts, authored)


def _take() -> timeline.TakeMedia:
    events = [
        {"step": 23, "action": "hover", "t0": 100.0, "ta": 100.5, "t1": 101.0, "ok": True},
        {"step": 24, "action": "pause", "t0": 101.0, "ta": 101.0, "t1": 109.0, "ok": True},
    ]
    take = timeline.TakeMedia(TAKE, 110.0, 1600, 900, 1.5, 60, 480, events=events)
    take.off_scale = [FAULT]
    return take


def test_the_save_names_each_off_scale_run_a_scene_shows_as_a_capture_fault():
    found = _findings(_take())
    [fault] = [f for f in found if f.kind == "off_scale_frames"]
    assert fault.severity == "dense" and fault.scene_id == "r"
    assert (
        "plays off-scale frames at step 24 (pause, 103.0-103.4 s, 0.4 s, 2 frame(s): frames f003090.jpg, f003093.jpg) "
        "on scene r: a capture fault, not a motion of the page"
    ) in fault.message
    assert "record the take again" in fault.message
    assert not [f for f in found if f.kind == "stepping_motion"]


def test_a_scene_trimmed_past_the_run_has_no_capture_fault():
    assert not [f for f in _findings(_take(), trim_start=105.0) if f.kind == "off_scale_frames"]
