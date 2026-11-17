"""A canvas wheel zoom keeps its boxes and its frames when its pixels alone do not tell.

A single wheel zoom of a WebGL graph read "20 box(es) over its canvas held as painted" and "5 frame(s) held, not
blended (keyframes not registered)": with one zoom in the take no other step could carry its labels, and a pair of
keyframes its pixels did not register held the nearer one although the canvas's own log said how far it zoomed. What
is proven here:

* :func:`scroll_pacing.riding_boxes` names a box no single pair tells but that the zoom draws closer than standing
  still over the whole zoom, and still not a toolbar that changes in place;
* :func:`scroll_pacing.implied_zooms` gives a pair its pixels did not register the zoom the canvas's log implies,
  scaled by what the registered pairs zoomed per unit of the wheel, and leaves it unregistered when that zoom draws it
  no closer than standing still;
* the step's words say how many pairs were registered from the log.
"""

from __future__ import annotations

import math

import numpy as np

from app.services.video_studio import scroll_pacing

SHAPE = (240, 320)
CENTRE = (160.0, 120.0)


def _noise(seed: int) -> np.ndarray:
    from scipy.ndimage import gaussian_filter

    rng = np.random.default_rng(seed)
    return gaussian_filter(rng.uniform(0, 255, SHAPE), 2.0).astype(np.float32) * 3.0


def _about(scale: float, point: tuple[float, float] = CENTRE) -> scroll_pacing.Zoom:
    return scroll_pacing.Zoom(scale, (1 - scale) * point[0], (1 - scale) * point[1])


def _zoomed(grey: np.ndarray, zoom: scroll_pacing.Zoom) -> np.ndarray:
    drawn = scroll_pacing._zoomed(grey, zoom, grey.shape)
    assert drawn is not None
    out, rows, cols = drawn
    second = grey.copy()
    second[rows, cols] = out
    return second


def test_a_box_no_single_pair_tells_rides_when_the_whole_zoom_draws_it_closer():
    first = _noise(8457)
    zoom = _about(1.15)
    second = _zoomed(first, zoom)
    # A label the page moved a frame late: on the second keyframe, part zoomed and part as it stood.
    label = (200.0, 150.0, 80.0, 40.0)
    rows, cols = slice(150, 190), slice(200, 280)
    second[rows, cols] = 0.6 * second[rows, cols] + 0.4 * first[rows, cols]
    # A toolbar that stands still, a button a hover lit in it.
    toolbar = (10.0, 10.0, 120.0, 30.0)
    second[10:40, 10:130] = first[10:40, 10:130]
    second[15:35, 20:60] = 250.0
    levels = [[(1, first)], [(1, second)]]
    assert scroll_pacing.riding_boxes(levels, [toolbar, label], [zoom]) == {1}
    # Not moved, or not registered: nothing rides.
    assert scroll_pacing.riding_boxes(levels, [toolbar, label], [scroll_pacing.IDENTITY]) == set()
    assert scroll_pacing.riding_boxes(levels, [toolbar, label], [None]) == set()


def test_a_pair_its_pixels_did_not_register_takes_the_zoom_the_wheel_log_implies():
    first = _noise(1)
    one, two = _about(1.1), _about(1.21)
    second = _zoomed(first, one)
    third = _zoomed(second, two)
    levels = [[(1, picture)] for picture in (first, second, third)]
    still = [np.zeros(SHAPE, dtype=bool)]
    # The wheel's delta: 100 for the first pair, 200 for the second (ln 1.21 = 2 ln 1.1).
    scroll = scroll_pacing.Scroll(span=(0.0, 2.0), log=[(0.0, 0.0), (1.0, 100.0), (2.0, 300.0)], scroller="canvas", progress="wheel")
    filled, count = scroll_pacing.implied_zooms([one, None], scroll, [0.0, 1.0, 2.0], levels, still)
    assert count == 1
    implied = filled[1]
    assert implied is not None and math.isclose(implied.scale, 1.21, rel_tol=1e-3), implied
    assert math.isclose(implied.tx, two.tx, abs_tol=0.5) and math.isclose(implied.ty, two.ty, abs_tol=0.5), implied
    # A transform log is the ln of the scale itself.
    by_transform = scroll_pacing.Scroll(
        span=(0.0, 2.0), log=[(0.0, 0.0), (1.0, math.log(1.1)), (2.0, math.log(1.1 * 1.21))], scroller="canvas", progress="transform"
    )
    filled, count = scroll_pacing.implied_zooms([one, None], by_transform, [0.0, 1.0, 2.0], levels, still)
    assert count == 1 and math.isclose(filled[1].scale, 1.21, rel_tol=1e-3)


def test_a_pair_the_implied_zoom_draws_no_closer_stays_unregistered():
    first = _noise(2)
    one = _about(1.1)
    second = _zoomed(first, one)
    # The third keyframe is another picture: no zoom explains it.
    levels = [[(1, picture)] for picture in (first, second, _noise(3))]
    still = [np.zeros(SHAPE, dtype=bool)]
    scroll = scroll_pacing.Scroll(span=(0.0, 2.0), log=[(0.0, 0.0), (1.0, 100.0), (2.0, 200.0)], scroller="canvas", progress="wheel")
    filled, count = scroll_pacing.implied_zooms([one, None], scroll, [0.0, 1.0, 2.0], levels, still)
    assert count == 0 and filled == [one, None]
    # Nothing registered to scale the wheel by, or nothing to fill: unchanged.
    assert scroll_pacing.implied_zooms([None, None], scroll, [0.0, 1.0, 2.0], levels, still) == ([None, None], 0)
    assert scroll_pacing.implied_zooms([one, one], scroll, [0.0, 1.0, 2.0], levels, still) == ([one, one], 0)


def test_the_step_says_the_pairs_registered_from_the_log():
    entry = {"paced": True, "fps": 30.0, "scroller": "canvas", "riding": 20, "implied": 3}
    said = scroll_pacing._how(entry, 12.0)
    assert "3 pair(s) of keyframes registered from the canvas's log" in said
    assert "held as painted" not in said and "held, not blended" not in said
