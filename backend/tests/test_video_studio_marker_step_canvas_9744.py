"""A marker plays nothing: canvas motion is never named by it, and a copy is not refused against a late frame (#9744).

On a GPU take, a graph's redraw after a wheel was logged at the ``marker`` step that followed it ("step 15: canvas
moved 0.9 s ... copies differ 6% ... frame painted 7.84 s after"): ``step_at`` named the marker, and the copy the
canvas stood at before was compared with a frame painted 7.84 s after it, across a redraw no copy saw. Here, on the
synthetic frames of #6761, a transition after a marker is named by the step still playing, and the before-side check
compares only a frame painted within ``REST_SECONDS`` of the copy.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from app.services.video_studio import canvas_alone, capture_rate
from tests.test_video_studio_canvas_alone_6761 import (
    CANVAS,
    COPIED,
    LEGEND,
    PAINTED,
    RATIO,
    SURFACE,
    _copies,
    _events,
    _frames,
)


def _marked() -> list[dict]:
    """A wait, a wheel at 0.4 s, a marker at 0.95 s: the move from 1.0 s plays under the wheel, the marker a point."""
    canvases = [["canvas#graph", list(CANVAS)]]
    return [
        {"step": 0, "action": "wait", "t0": 0.0, "ta": 0.0, "t1": 0.4, "ok": True, "canvases": canvases},
        {
            "step": 1,
            "action": "wheel",
            "label": "Zoom in",
            "t0": 0.4,
            "ta": 0.4,
            "t1": 0.9,
            "ok": True,
            "canvases": canvases,
        },
        {"step": 2, "action": "marker", "label": "focus", "t0": 0.95, "ta": 0.95, "t1": 0.95, "ok": True},
        {"step": 3, "action": "wait", "t0": 2.7, "ta": 2.7, "t1": 3.2, "ok": True, "canvases": canvases},
    ]


def test_step_at_passes_a_marker_and_names_the_step_still_playing():
    events = _marked()
    assert capture_rate.step_at(events, 1.2)["step"] == 1
    assert capture_rate.step_at(events, 0.95)["step"] == 1
    assert capture_rate.step_at(events, 2.8)["step"] == 3
    # Only markers before the first step: nothing was playing yet.
    assert capture_rate.step_at([{"step": 0, "action": "marker", "t0": 0.0}], 1.0) is None


def test_a_marker_does_not_end_the_page_loading():
    events = [
        {"step": 0, "action": "wait", "t0": 0.0},
        {"step": 1, "action": "marker", "label": "intro", "t0": 1.0},
        {"step": 2, "action": "click", "t0": 3.0},
    ]
    assert capture_rate.loading(events, 1.5) is True
    assert capture_rate.loading(events, 3.5) is False


def test_a_canvas_run_after_a_marker_is_named_by_the_step_that_moved_it(tmp_path):
    frames = _frames(tmp_path, PAINTED)
    done = canvas_alone.composite(
        frames, _copies(tmp_path, COPIED), _marked(), fps=30, pixel_ratio=RATIO, workdir=str(tmp_path)
    )
    [run] = done.runs
    assert run["step"] == 1, run
    [motion] = capture_rate.canvas_motion(frames, _marked(), workdir=str(tmp_path), viewport_width=800)
    assert motion.step == 1 and motion.label == "Zoom in", motion


def _blank_copy_before(workdir: Path, t: float) -> canvas_alone.AloneFrame:
    """A copy of the canvas at *t*, blank: what it held before a redraw no copy saw."""
    name = "before-000000.jpg"
    Image.fromarray(np.full((300, 500, 3), 30, dtype=np.uint8)).save(workdir / name, quality=92)
    return canvas_alone.AloneFrame(t, SURFACE, "canvas#graph", CANVAS, (LEGEND,), name)


def test_the_copy_before_is_not_compared_with_a_frame_painted_long_after_it(tmp_path, monkeypatch):
    frames = _frames(tmp_path, PAINTED)
    copies = [_blank_copy_before(tmp_path, 0.0), *_copies(tmp_path, COPIED[1:])]

    # Within REST_SECONDS (the frame at 1.0 s, 1.0 s after the copy): compared, and the blank copy refuses the run.
    near = canvas_alone.composite(frames, copies, _events(), fps=30, pixel_ratio=RATIO, workdir=str(tmp_path))
    [run] = near.runs
    assert run["composited"] is False and "the copy it stood at before" in run["why"], run

    # Past it: the page may have drawn since without a copy; that side is not compared, the after side decides.
    monkeypatch.setattr(canvas_alone, "REST_SECONDS", 0.5)
    late = canvas_alone.composite(frames, copies, _events(), fps=30, pixel_ratio=RATIO, workdir=str(tmp_path))
    [run] = late.runs
    assert run["composited"] is True, run
    assert {entry["side"] for entry in run["checked"]} == {"after"}, run["checked"]
