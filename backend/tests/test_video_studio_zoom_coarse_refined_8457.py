"""A canvas wheel zoom the coarse search misses by a little is refined on the coarsest width before it is held.

Production takes of a force-directed graph zoomed by the wheel kept reading "1 frame(s) held, not blended (keyframes
not registered: 1 pair(s) no zoom explains; the best zoom tried explains 23% of what tells them apart, 25% needed)":
one pair a take, just under the bar. :func:`scroll_pacing.register_zoom` judges the coarse bests of its searches on the
coarsest width when the fine widths miss (a graph keeps its lines and labels at the screen's size), but those bests
only scale about the fixed points searched, a step apart. What is proven here: each coarse best is refined on the
coarsest width (scale and pan) before it is judged, so a zoom about a point no search tried is registered; two
unrelated pictures stay held, their share under the bar.
"""

from __future__ import annotations

import math

import numpy as np

from app.services.video_studio import scroll_pacing

SHAPE = (240, 320)
POINT = (90.0, 70.0)
FAR = [(300.0, 220.0)]


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


def test_a_zoom_about_a_point_no_search_tried_is_registered_once_refined(monkeypatch):
    # The canvas zoomed about POINT; the cursor stood across the box and the grid is one point (its centre): no
    # search tries POINT.
    first = _noise(31)
    made = scroll_pacing.Zoom(1.1, (1 - 1.1) * POINT[0], (1 - 1.1) * POINT[1])
    second = _zoomed(first, made)
    levels = [[(1, picture)] for picture in (first, second)]
    still = [np.zeros(SHAPE, dtype=bool)]
    monkeypatch.setattr(scroll_pacing, "ZOOM_GRID", 1)
    zoom = scroll_pacing.register_zoom(levels[0], levels[1], FAR, still)
    assert zoom is not None and zoom != scroll_pacing.IDENTITY
    assert math.isclose(zoom.scale, 1.1, rel_tol=1e-2), zoom
    # Without the refining, the same pair is held: the coarse bests alone miss the bar.
    monkeypatch.setattr(scroll_pacing, "_refined", lambda _a, _b, _w, zoom, _f, _s: zoom)
    explained: list[float] = []
    assert scroll_pacing.register_zoom(levels[0], levels[1], FAR, still, explained) is None
    assert len(explained) == 1 and explained[0] < scroll_pacing.ZOOM_EXPLAINS


def test_two_unrelated_pictures_stay_held():
    a = [(1, _noise(34)), (1, _noise(35))]
    b = [(1, _noise(36)), (1, _noise(37))]
    still = [np.zeros(SHAPE, dtype=bool), np.zeros(SHAPE, dtype=bool)]
    explained: list[float] = []
    assert scroll_pacing.register_zoom(a, b, [POINT], still, explained) is None
    assert len(explained) == 1 and explained[0] < scroll_pacing.ZOOM_EXPLAINS
