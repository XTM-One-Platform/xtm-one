"""A canvas wheel zoom on a GPU worker kept under ``fps_fallback: refuse`` (retest of #10907, issue #10943).

The recorder awaited each wheel turn's painted frame before sending the next, so on a GPU the page drew the zoom at
10 to 15 fps; a zoom with no frame painted at rest after it was refused unchecked; a 0.2 s svg swap held then cut was
said synthesised; and a refused wheel zoom read as the page's paint still advised a smaller window. No browser, no
database."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from app.services.video_studio import canvas_alone, capture, capture_rate, scroll_pacing

SURFACE = "100,50,500,300"
CANVAS = (100.0, 50.0, 500.0, 300.0)


def _graph(tmp: Path, name: str, shift: int) -> None:
    picture = np.full((540, 960, 3), 240, dtype=np.uint8)
    picture[80 + shift : 160 + shift, 200:400] = 20
    Image.fromarray(picture).save(tmp / name)


def _copy(tmp: Path, k: int, t: float, shift: int) -> canvas_alone.AloneFrame:
    picture = np.full((300, 500, 3), 240, dtype=np.uint8)
    picture[30 + shift : 110 + shift, 100:300] = 20
    name = f"c{k:06d}.jpg"
    Image.fromarray(picture).save(tmp / name)
    return canvas_alone.AloneFrame(round(t, 4), SURFACE, "canvas#graph", CANVAS, (), name)


def test_a_zoom_with_no_frame_at_rest_soon_after_is_checked_against_the_next_frame(tmp_path):
    # The V100S case: the zoom settled at 2.5 s, the screencast painted again only at 4.0 s (past REST_SECONDS), and
    # nothing was copied before the run: once "no frame painted with the canvas at rest", now checked and composited.
    copies = [_copy(tmp_path, k, 1.0 + k / 30, 20) for k in range(46)]
    _graph(tmp_path, "f000001.jpg", 0)
    _graph(tmp_path, "f000002.jpg", 20)
    frames = [(0.5, "f000001.jpg"), (4.0, "f000002.jpg")]
    run = canvas_alone.AloneRun(tuple(copies))
    checked: list[dict] = []
    assert canvas_alone.check(run, copies, frames, workdir=str(tmp_path), ratio=1.0, checked=checked) is None
    assert checked and checked[0]["side"] == "after"


def test_a_late_frame_that_differs_still_refuses_the_copies(tmp_path):
    copies = [_copy(tmp_path, k, 1.0 + k / 30, 20) for k in range(46)]
    _graph(tmp_path, "f000002.jpg", 120)
    frames = [(4.0, "f000002.jpg")]
    run = canvas_alone.AloneRun(tuple(copies))
    why = canvas_alone.check(run, copies, frames, workdir=str(tmp_path), ratio=1.0) or ""
    assert "differ from what the browser painted" in why


def test_a_zoom_with_nothing_painted_after_it_stays_unchecked(tmp_path):
    copies = [_copy(tmp_path, k, 1.0 + k / 30, 20) for k in range(46)]
    _graph(tmp_path, "f000001.jpg", 20)
    run = canvas_alone.AloneRun(tuple(copies))
    frames = [(copies[30].t + 0.01, "f000001.jpg")]
    assert canvas_alone.check(run, copies, frames, workdir=str(tmp_path), ratio=1.0) == canvas_alone.UNCHECKED


def _swap(seconds: float, *, canvas: bool = False) -> dict:
    entry = {
        "t": [5.0, 5.0 + seconds],
        "paced": True,
        "painted": 5.0,
        "fps": 30,
        "held": 6,
        "held_not_blended": True,
        **({"scroller": "canvas"} if canvas else {}),
    }
    return {"step": 1, "action": "click", "motion": [entry]}


def test_a_brief_svg_swap_held_then_cut_is_not_synthesised():
    assert scroll_pacing.synthesised_transitions([_swap(0.2)], fps=30) == []
    assert capture_rate.refusal({"gl": {"gpu": True}, "fps": 30, "stepping": []}, "refuse", [_swap(0.2)]) == ""


def test_a_longer_held_transition_and_a_canvas_zoom_are_still_said():
    assert scroll_pacing.synthesised_transitions([_swap(0.6)], fps=30)
    assert scroll_pacing.synthesised_transitions([_swap(0.2, canvas=True)], fps=30)


def test_a_wheel_zoom_read_as_the_page_names_no_smaller_window():
    stepping = [{"t": [10.0, 11.6], "frames": 48, "synthesised": 25, "fps": 30, "step": 3}]
    wheel = {"step": 3, "action": "scroll", "scrolls": [{"t": [10.0, 11.6], "surface": {"kind": "canvas"}}]}
    said = capture_rate.refusal({"gl": {"gpu": True}, "fps": 30, "stepping": stepping}, "refuse", [wheel])
    assert said.startswith("Refused after recording") and "smaller window" not in said
    # Not a wheel step either: no GPU refusal names a smaller window since #10950.
    said = capture_rate.refusal({"gl": {"gpu": True}, "fps": 30, "stepping": stepping}, "refuse", [])
    assert said.startswith("Refused after recording") and "smaller window" not in said


def test_gpu_wheel_ticks_are_kept_in_paint_order_without_failed_reads():
    read = [[2.1, 40, None], RuntimeError("navigating"), [2.0, 20, None], [2.2, 60, None]]
    assert [tick[1] for tick in capture.gpu_ticks(read)] == [20, 40, 60]


def test_the_recorder_reads_wheel_paints_beside_the_turns_on_a_gpu():
    source = Path(capture.__file__).read_text(encoding="utf-8")
    source = source[source.index("    async def _scrolled(") :]
    assert "if surface is not None and self.gpu:" in source
    assert "asyncio.ensure_future(self._painted_tick(sent))" in source
