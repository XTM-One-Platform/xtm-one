"""A recording's scroll turns the wheel on a clock, and a window's scroll is paced on what its keyframes painted.

A follow-up to the pacing of a take's scrolls at its rate from the keyframes the screencast painted:

* the wheel turned in fixed 22 ms sleeps, so a scroll lasted however long the turns' round trips took and landed
  unevenly (33 turns over about 1.5 s on a Windows host); it now turns on a 60 Hz clock over the step's ``seconds``
  (else the pace's speed), the turns it falls behind on caught up on the next tick;
* each keyframe's offset was read off its pixels and the log's clock lag fitted on them, while the screencast says the
  window's offset each frame painted (``scrollOffsetY``): the recorder keeps it, and a window's scroll is paced on it -
  the pixels are read only for a scroll container, or a keyframe that did not say.

Synthetic frames for the pacing (``test_video_studio_scroll_pacing_6408``'s page, header and 2.1 fps screencast); a
stand-in screencast session and wheel for the recorder.
"""

from __future__ import annotations

import base64
import io
import time
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from app.services.video_studio import capture, scroll_pacing
from app.services.video_studio.spec import parse_take_script, validate_take_script
from tests.test_video_studio_scroll_pacing_6408 import (
    END,
    HEADER_CSS,
    RATIO,
    START,
    _decoded,
    _offset,
    _page,
    _take,
    _view,
)


def _painted(frames: list[tuple[float, str]]) -> dict[str, float]:
    """The window's offset each keyframe painted, as the screencast says it: where the page stood at its stamp, on the
    device pixel the frame shows it at."""
    return {name: round(_offset(t) * RATIO) / RATIO for t, name in frames}


def test_a_window_scroll_is_paced_on_the_offsets_its_keyframes_painted(tmp_path, monkeypatch):
    page = _page()
    frames, events = _take(tmp_path, page)

    def unread(*_args, **_kwargs):
        raise AssertionError("a window's keyframes say what they painted: nothing is read off their pixels")

    monkeypatch.setattr(scroll_pacing, "register", unread)
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=30, pixel_ratio=RATIO, painted=_painted(frames))
    [scroll] = events[0]["scrolls"]
    assert scroll["paced"] and scroll["offsets"] == "painted" and scroll["fps"] == 30.0
    during = [(t, name) for t, name in paced.frames if START <= t < END]
    assert len(during) == 60
    header = HEADER_CSS * RATIO
    for t, name in during:
        shown = _decoded(tmp_path, name)
        # The page at its instant, to the device pixel (an offset of 66.7 CSS px is 133.3 device px).
        error = min(
            float(np.abs(shown[header:] - _view(page, _offset(t) + nudge / RATIO).astype(np.int16)[header:]).mean())
            for nudge in (-1, 0, 1)
        )
        assert error < 4.0, f"the frame at {t:.3f} s is the page there"


def test_a_keyframe_that_did_not_say_what_it_painted_has_its_offset_read_off_the_pixels(tmp_path):
    page = _page()
    frames, events = _take(tmp_path, page)
    painted = _painted(frames)
    painted.pop(frames[2][1])
    scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=30, pixel_ratio=RATIO, painted=painted)
    [scroll] = events[0]["scrolls"]
    assert scroll["paced"] and scroll["offsets"] == "registered"
    # A scroll container's keyframes say nothing of it: read off their pixels too.
    frames, events = _take(tmp_path, page)
    for sample in events[0]["scrolls"][0]["samples"]:
        sample[3] = [[1, sample[1], 0, [0, 0, 1600, 900]]]
        sample[1] = 0
    scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=30, pixel_ratio=RATIO, painted=_painted(frames))
    [scroll] = events[0]["scrolls"]
    assert scroll["scroller"] == "element" and scroll.get("offsets") in (None, "registered")


class _Session:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def on(self, _event: str, _handler) -> None:
        return None

    async def send(self, method: str, _params: dict | None = None) -> dict:
        self.sent.append(method)
        return {}


async def test_the_screencast_keeps_the_offset_each_frame_painted(tmp_path):
    buffer = io.BytesIO()
    Image.new("RGB", (16, 9), (40, 60, 80)).save(buffer, format="JPEG")
    data = base64.b64encode(buffer.getvalue()).decode()
    screencast = capture._Screencast(_Session(), str(tmp_path), max_frames=10)
    screencast.t_start = time.time()
    screencast._on_frame({"sessionId": 1, "data": data, "metadata": {"timestamp": time.time(), "scrollOffsetY": 1240.5}})
    screencast._on_frame({"sessionId": 2, "data": data, "metadata": {"timestamp": time.time()}})
    await screencast.stop()
    first, second = (name for _t, name in screencast.frames)
    assert screencast.offsets == {first: 1240.5} and second not in screencast.offsets


class _Mouse:
    def __init__(self) -> None:
        self.turns: list[tuple[float, float]] = []

    async def wheel(self, _x: float, y: float) -> None:
        self.turns.append((time.monotonic(), y))


def _recorder(mouse: _Mouse, pace: str = "natural", fps: int = 60):
    recorder = capture._Recorder.__new__(capture._Recorder)
    recorder.page = SimpleNamespace(mouse=mouse)
    recorder.script = SimpleNamespace(pace=pace, fps=fps)

    async def settled() -> None:
        return None

    recorder._wait_scroll_end = settled
    return recorder


async def test_the_wheel_turns_on_a_60_hz_clock_over_the_step_s_seconds():
    mouse = _Mouse()
    step = parse_take_script(validate_take_script({"steps": [{"action": "scroll", "by": 2000, "seconds": 2}]})).steps[0]
    await _recorder(mouse)._scrolled(step)
    assert sum(y for _t, y in mouse.turns) == 2000
    spread = mouse.turns[-1][0] - mouse.turns[0][0]
    # About 60 turns a second over 2 s, a steady pace: a coarse timer (Windows') catches up on fewer, larger turns.
    assert spread == pytest.approx(2.0, abs=0.15) and len(mouse.turns) >= 50
    first_half = sum(y for t, y in mouse.turns if t - mouse.turns[0][0] < spread / 2)
    assert first_half == pytest.approx(1000, abs=120)


def test_a_scroll_step_without_seconds_takes_its_pace_s_speed():
    assert capture.scroll_seconds(2000, "natural") == pytest.approx(2000 / 1500)
    assert capture.scroll_seconds(2000, "fast") < capture.scroll_seconds(2000, "natural") < capture.scroll_seconds(2000, "slow")
    assert capture.scroll_seconds(50, "slow") == 0.35 and capture.scroll_seconds(20000, "slow") == 4.0
