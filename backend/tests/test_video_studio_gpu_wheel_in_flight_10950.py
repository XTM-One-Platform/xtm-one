"""A canvas wheel zoom on a GPU worker: the turns are sent without awaiting the browser's answer to each, and no
refusal on a GPU lane advises a smaller window (retest of #10944, issue #10950). No browser, no database."""

from __future__ import annotations

from pathlib import Path

from app.services.video_studio import capture, capture_rate


def _source() -> str:
    source = Path(capture.__file__).read_text(encoding="utf-8")
    start = source.index("    async def _scrolled(")
    return source[start : source.index("    async def _canvas_appeared(", start)]


def test_gpu_wheel_turns_are_sent_ahead_bounded_and_awaited_before_the_settle():
    source = _source()
    assert "gpu_canvas = step.to is None and surface is not None and self.gpu" in source
    assert "if gpu_canvas:" in source
    assert "wheeling.append(asyncio.ensure_future(page.mouse.wheel(0, turn)))" in source
    assert "len(wheeling) >= _GPU_WHEELS_IN_FLIGHT" in source
    # Every turn answered before the zoom is read on as settled.
    assert source.index("await asyncio.gather(*wheeling") < source.index("await self._canvas_settled(")
    # Bounded: two turns a frame of the take (#10955), at most a few frames' turns ahead.
    assert 1 <= capture._GPU_WHEELS_IN_FLIGHT <= 4 * capture._GPU_TURNS_PER_FRAME


def test_gpu_canvas_wheel_turns_twice_a_frame_and_paint_reads_never_hold_a_turn():
    source = _source()
    # A turn lands in every presented frame of a ~60 Hz compositor at a 30 fps take (#10955).
    assert capture._GPU_TURNS_PER_FRAME >= 2
    assert "rate = int(self.script.fps) * (_GPU_TURNS_PER_FRAME if gpu_canvas else 1)" in source
    assert "tick = 1 / rate" in source
    assert "await asyncio.wait(pending[:1])" not in source
    assert capture.wheel_deltas(300, 1.0, 30 * capture._GPU_TURNS_PER_FRAME) == [5] * 60


def test_software_gl_still_awaits_each_turn():
    assert "elif step.to is None:\n                        await page.mouse.wheel(0, turn)" in _source()


def test_no_gpu_refusal_names_a_smaller_window():
    stepping = [{"t": [10.0, 11.6], "frames": 48, "synthesised": 25, "fps": 30, "step": 1}]
    click = {"step": 16, "action": "click", "motion": [{"t": [3.0, 3.4], "paced": True, "painted": 15.0, "fps": 30}]}
    for events in ([], [click]):
        said = capture_rate.refusal({"gl": {"gpu": True}, "fps": 30, "stepping": stepping}, "refuse", events)
        assert said.startswith("Refused after recording")
        assert "smaller window" not in said
        assert "whatever the window" in said
