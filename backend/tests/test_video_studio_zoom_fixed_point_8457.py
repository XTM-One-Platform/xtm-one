"""A canvas wheel zoom registers a pair about the fixed point its other pairs share, and says why a pair stays held.

A production take of a force-directed graph zoomed by the wheel read "6 frame(s) held, not blended (keyframes not
registered)" with no word of why. :func:`scroll_pacing.register_zoom` searches a pair's zoom about the cursor or the
box's centre only; a graph library zooms about its own point, which the pairs it did register agree on. What is proven
here:

* :func:`scroll_pacing.implied_zooms` registers a pair again about the fixed point the registered pairs share before it
  falls back on the canvas's log, so a pair the log cannot scale (it did not move across it) is still registered;
* a pair left unregistered is given its reason, and the step's words name it.
"""

from __future__ import annotations

import math

import numpy as np

from app.services.video_studio import scroll_pacing

SHAPE = (240, 320)
POINT = (90.0, 70.0)


def _noise(seed: int) -> np.ndarray:
    from scipy.ndimage import gaussian_filter

    rng = np.random.default_rng(seed)
    return gaussian_filter(rng.uniform(0, 255, SHAPE), 2.0).astype(np.float32) * 3.0


def _about(scale: float, point: tuple[float, float] = POINT) -> scroll_pacing.Zoom:
    return scroll_pacing.Zoom(scale, (1 - scale) * point[0], (1 - scale) * point[1])


def _zoomed(grey: np.ndarray, zoom: scroll_pacing.Zoom) -> np.ndarray:
    drawn = scroll_pacing._zoomed(grey, zoom, grey.shape)
    assert drawn is not None
    out, rows, cols = drawn
    second = grey.copy()
    second[rows, cols] = out
    return second


def test_a_pair_is_registered_about_the_fixed_point_the_others_share():
    first = _noise(11)
    one, two = _about(1.1), _about(1.15)
    second = _zoomed(first, one)
    third = _zoomed(second, two)
    levels = [[(1, picture)] for picture in (first, second, third)]
    still = [np.zeros(SHAPE, dtype=bool)]
    # The log stood still across the second pair (an easing the wheel's delta does not tell): it cannot scale it.
    scroll = scroll_pacing.Scroll(span=(0.0, 2.0), log=[(0.0, 0.0), (1.0, 100.0), (2.0, 100.0)], scroller="canvas", progress="wheel")
    why: dict[int, str] = {}
    filled, count = scroll_pacing.implied_zooms([one, None], scroll, [0.0, 1.0, 2.0], levels, still, why)
    assert count == 1 and why == {}
    again = filled[1]
    assert again is not None and math.isclose(again.scale, 1.15, rel_tol=5e-3), again
    assert math.isclose(again.tx, two.tx, abs_tol=1.0) and math.isclose(again.ty, two.ty, abs_tol=1.0), again


def test_a_pair_left_unregistered_is_given_its_reason():
    first = _noise(12)
    one = _about(1.1)
    second = _zoomed(first, one)
    levels = [[(1, picture)] for picture in (first, second, _noise(13))]
    still = [np.zeros(SHAPE, dtype=bool)]
    moving = scroll_pacing.Scroll(span=(0.0, 2.0), log=[(0.0, 0.0), (1.0, 100.0), (2.0, 200.0)], scroller="canvas", progress="wheel")
    why: dict[int, str] = {}
    assert scroll_pacing.implied_zooms([one, None], moving, [0.0, 1.0, 2.0], levels, still, why) == ([one, None], 0)
    assert why == {1: "unexplained"}
    standing = scroll_pacing.Scroll(span=(0.0, 2.0), log=[(0.0, 0.0), (1.0, 100.0), (2.0, 100.0)], scroller="canvas", progress="wheel")
    why = {}
    scroll_pacing.implied_zooms([one, None], standing, [0.0, 1.0, 2.0], levels, still, why)
    assert why == {1: "log_still"}
    why = {}
    scroll_pacing.implied_zooms([None, None], moving, [0.0, 1.0, 2.0], levels, still, why)
    assert why == {0: "no_scale", 1: "no_scale"}
    # Without *why*, the answer is as before.
    assert scroll_pacing.implied_zooms([one, None], moving, [0.0, 1.0, 2.0], levels, still) == ([one, None], 0)


def test_the_step_says_why_its_frames_are_held():
    entry = {"paced": True, "fps": 30.0, "scroller": "canvas", "held": 6, "held_not_blended": True, "held_why": {"unexplained": 1, "uncovered": 2}}
    said = scroll_pacing._how(entry, 10.8)
    assert "6 frame(s) held, not blended (keyframes not registered: 2 frame(s) neither keyframe painted enough of, 1 pair(s) no zoom explains)" in said
    # A take recorded before the reasons were kept reads as it did.
    del entry["held_why"]
    assert "6 frame(s) held, not blended (keyframes not registered)" in scroll_pacing._how(entry, 10.8)


def test_a_pair_left_unexplained_says_how_near_the_bar_the_best_zoom_came():
    first = _noise(14)
    one = _about(1.1)
    second = _zoomed(first, one)
    # Two pictures no zoom relates: the best zoom tried explains next to nothing of what tells them apart.
    levels = [[(1, picture)] for picture in (first, second, _noise(15))]
    still = [np.zeros(SHAPE, dtype=bool)]
    moving = scroll_pacing.Scroll(span=(0.0, 2.0), log=[(0.0, 0.0), (1.0, 100.0), (2.0, 200.0)], scroller="canvas", progress="wheel")
    why: dict[int, str] = {}
    explained: dict[int, float] = {}
    assert scroll_pacing.implied_zooms([one, None], moving, [0.0, 1.0, 2.0], levels, still, why, explained) == ([one, None], 0)
    assert why == {1: "unexplained"} and set(explained) == {1}
    assert 0.0 <= explained[1] < scroll_pacing.ZOOM_EXPLAINS
    # A pair the fixed point registers is given no share.
    third = _zoomed(second, _about(1.15))
    levels = [[(1, picture)] for picture in (first, second, third)]
    standing = scroll_pacing.Scroll(span=(0.0, 2.0), log=[(0.0, 0.0), (1.0, 100.0), (2.0, 100.0)], scroller="canvas", progress="wheel")
    explained = {}
    _filled, count = scroll_pacing.implied_zooms([one, None], standing, [0.0, 1.0, 2.0], levels, still, None, explained)
    assert count == 1 and explained == {}


def test_a_pair_zoomed_about_a_point_far_from_its_anchors_is_registered_from_the_grid(monkeypatch):
    first = _noise(16)
    made = _about(1.1)
    second = _zoomed(first, made)
    levels = [[(1, picture)] for picture in (first, second)]
    still = [np.zeros(SHAPE, dtype=bool)]
    # The cursor stood across the box from the point the canvas zoomed about: no zoom about it explains the pair.
    far = [(300.0, 220.0)]
    monkeypatch.setattr(scroll_pacing, "ZOOM_GRID", 1)
    # The coarse bests left as found (not refined on the coarsest width, which registers it on its own).
    refined = scroll_pacing._refined
    monkeypatch.setattr(scroll_pacing, "_refined", lambda _a, _b, _w, zoom, _f, _s: zoom)
    explained: list[float] = []
    assert scroll_pacing.register_zoom(levels[0], levels[1], far, still, explained) is None
    assert len(explained) == 1 and explained[0] < scroll_pacing.ZOOM_EXPLAINS
    # Searched again from the grid of fixed points over the box, it is registered.
    monkeypatch.setattr(scroll_pacing, "ZOOM_GRID", 5)
    zoom = scroll_pacing.register_zoom(levels[0], levels[1], far, still)
    assert zoom is not None and math.isclose(zoom.scale, 1.1, rel_tol=1e-2), zoom
    monkeypatch.setattr(scroll_pacing, "_refined", refined)
    # A pair its anchors explain is not searched again: the same answer as before.
    assert scroll_pacing.register_zoom(levels[0], levels[1], [POINT], still) == scroll_pacing.register_zoom(
        levels[0], levels[1], [POINT], still
    )


def test_a_pair_the_coarse_width_explains_is_registered_though_the_fine_one_does_not():
    # A graph draws its lines and labels at the screen's size whatever its zoom: the fine width tells a zoom its nodes
    # follow apart from the keyframe it is drawn to, the coarse width does not. Here the fine pictures are unrelated.
    first = _noise(17)
    made = _about(1.1)
    second = _zoomed(first, made)
    a = [(1, first), (1, _noise(18))]
    b = [(1, second), (1, _noise(19))]
    still = [np.zeros(SHAPE, dtype=bool), np.zeros(SHAPE, dtype=bool)]
    zoom = scroll_pacing.register_zoom(a, b, [POINT], still)
    assert zoom is not None and zoom != scroll_pacing.IDENTITY
    # Two pictures unrelated on every width stay unregistered, their share under the bar.
    explained: list[float] = []
    c = [(1, _noise(20)), (1, _noise(21))]
    assert scroll_pacing.register_zoom(a, c, [POINT], still, explained) is None
    assert len(explained) == 1 and explained[0] < scroll_pacing.ZOOM_EXPLAINS


def test_the_step_says_how_near_the_bar_the_best_zoom_came():
    entry = {
        "paced": True,
        "fps": 30.0,
        "scroller": "canvas",
        "held": 3,
        "held_not_blended": True,
        "held_why": {"unexplained": 2},
        "held_explained": 0.183,
    }
    said = scroll_pacing._how(entry, 10.5)
    assert (
        "3 frame(s) held, not blended (keyframes not registered: 2 pair(s) no zoom explains; "
        "the best zoom tried explains 18% of what tells them apart, 25% needed)" in said
    ), said
