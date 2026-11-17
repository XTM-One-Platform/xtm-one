"""A box the page opens over a canvas during its wheel zoom is left out of the zoom's registration.

A production take of a force-directed graph zoomed by the wheel read "5 frame(s) held, not blended (... 3 pair(s) no
zoom explains; the best zoom tried explains 15% of what tells them apart, 25% needed)", and its frames showed a link's
tooltip open over the graph once the zoom brought a link under the cursor. The tooltip was not there as the wheel began,
so it was not among the boxes drawn over the canvas: its pixels told the keyframes apart where no zoom does. What is
proven here:

* the recorder reads the boxes over the canvas again once its zoom settled and keeps those that were not there
  (``surface["appeared"]``);
* :func:`scroll_pacing.appeared_still` marks them on every width, so a pair a zoom explains outside the tooltip is
  registered; without it the same pair is not;
* the step's words name them.
"""

from __future__ import annotations

import math

import numpy as np

from app.services.video_studio import capture, scroll_pacing

SHAPE = (240, 320)
POINT = (90.0, 70.0)
TIP = (200.0, 20.0, 110.0, 200.0)  # x, y, w, h (device px of the canvas's box)


def _noise(seed: int) -> np.ndarray:
    from scipy.ndimage import gaussian_filter

    rng = np.random.default_rng(seed)
    return gaussian_filter(rng.uniform(0, 255, SHAPE), 2.0).astype(np.float32) * 3.0


def _zoomed(grey: np.ndarray, zoom: scroll_pacing.Zoom) -> np.ndarray:
    drawn = scroll_pacing._zoomed(grey, zoom, grey.shape)
    assert drawn is not None
    out, rows, cols = drawn
    second = grey.copy()
    second[rows, cols] = out
    return second


def test_a_box_opened_during_the_zoom_is_left_out_of_its_registration():
    first = _noise(31)
    made = scroll_pacing.Zoom(1.1, (1 - 1.1) * POINT[0], (1 - 1.1) * POINT[1])
    second = _zoomed(first, made)
    # A tooltip opened over a third of the canvas on the second keyframe: flat, bright, unlike anything zoomed.
    x, y, w, h = (int(v) for v in TIP)
    second[y : y + h, x : x + w] = 900.0
    levels = [[(1, first)], [(1, second)]]
    still = [np.zeros(SHAPE, dtype=bool)]
    explained: list[float] = []
    assert scroll_pacing.register_zoom(levels[0], levels[1], [POINT], still, explained) is None
    assert explained and explained[0] < scroll_pacing.ZOOM_EXPLAINS
    marked = scroll_pacing.appeared_still(still, levels, [TIP])
    assert not still[0].any(), "the masks given are not changed"
    assert marked[0][y : y + h, x : x + w].all()
    zoom = scroll_pacing.register_zoom(levels[0], levels[1], [POINT], marked)
    assert zoom is not None and zoom != scroll_pacing.IDENTITY and math.isclose(zoom.scale, 1.1, rel_tol=1e-2), zoom


def test_the_box_is_marked_on_every_width_at_its_factor():
    levels = [[(4, np.zeros((60, 80))), (1, np.zeros(SHAPE))]]
    still = [np.zeros((60, 80), dtype=bool), np.zeros(SHAPE, dtype=bool)]
    marked = scroll_pacing.appeared_still(still, levels, [(40.0, 40.0, 40.0, 40.0)])
    assert marked[0][10:20, 10:20].all() and marked[0].sum() == 100
    assert marked[1][40:80, 40:80].all() and marked[1].sum() == 1600


def test_the_canvas_scroll_reads_the_boxes_that_appeared():
    surface = {
        "box": [0, 0, 320, 240],
        "at": [100, 100],
        "ticks": [[0, 0, None], [1, 100, None]],
        "over": [[0, 0, 40, 20]],
        "appeared": [[200, 20, 110, 200], "bad"],
    }
    scroll = scroll_pacing._canvas_scroll((0.0, 1.0), surface)
    assert scroll is not None and scroll.appeared == [(200.0, 20.0, 110.0, 200.0)]
    del surface["appeared"]
    assert scroll_pacing._canvas_scroll((0.0, 1.0), surface).appeared == []


def test_the_step_says_the_boxes_opened_during_the_zoom():
    entry = {"paced": True, "fps": 30.0, "scroller": "canvas", "appeared": 1}
    assert "1 box(es) opened over its canvas during the zoom held as painted" in scroll_pacing._how(entry, 10.3)
    del entry["appeared"]
    assert "opened over its canvas" not in scroll_pacing._how(entry, 10.3)


class _Page:
    def __init__(self, over: list) -> None:
        self.over = over

    async def evaluate(self, script: str, _arg=None):
        assert script == capture._CANVAS_AT_JS
        return {"context": "2d", "box": [0, 0, 320, 240], "over": self.over}


async def test_the_recorder_keeps_the_boxes_not_there_as_the_wheel_began():
    recorder = capture._Recorder.__new__(capture._Recorder)
    recorder.page = _Page([[0, 0, 40, 20], [1, 0.5, 40, 20], [200, 20, 110, 200]])
    surface = {"at": [100.0, 100.0], "over": [[0.0, 0.0, 40.0, 20.0]]}
    await recorder._canvas_appeared(surface)
    assert surface["appeared"] == [[200.0, 20.0, 110.0, 200.0]]
    # Nothing new: no key written.
    surface = {"at": [100.0, 100.0], "over": [[0.0, 0.0, 40.0, 20.0], [200.0, 20.0, 110.0, 200.0]]}
    await recorder._canvas_appeared(surface)
    assert "appeared" not in surface


def test_the_boxes_are_read_with_every_element_taking_the_pointer():
    # A take after #9141 kept no box "opened over its canvas" while its frames show a link's tooltip open after the
    # zoom: a graph's tooltip is pointer-events: none, which elementsFromPoint skips. The boxes are read with every
    # element but the canvases taking the pointer, the rule removed in the same task whatever happens.
    script = capture._CANVAS_AT_JS
    assert ":not(canvas) { pointer-events: auto !important; }" in script
    assert script.index("appendChild(every)") < script.index("elementsFromPoint(px, py)") < script.index("every.remove()")
    assert "finally" in script
