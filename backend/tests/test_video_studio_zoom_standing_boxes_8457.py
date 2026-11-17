"""The boxes over a zoomed canvas that stand still are kept in place, and said apart from those held as painted.

A single wheel zoom of a WebGL graph read "20 box(es) over its canvas held as painted" once its pairs were all
registered (#8461): the page's elements over the canvas come nested (a legend, its rows, the icon and the count of
each) and a box too small to have an inside past its edge, or a row a hover lit inside a legend that stands still,
was never judged standing; and the words counted a toolbar kept in place with the boxes a zoom failed to carry.
What is proven here:

* :func:`scroll_pacing.standing` judges a box too small for an inside whole, and counts a box lying inside one that
  stands still whole as standing with it, even where it changed in place;
* a box inside one that does not stand still is judged on its own;
* :func:`scroll_pacing._how` says the boxes standing still apart from those held as painted, and an entry paced
  before the split still reads as it did.
"""

from __future__ import annotations

import numpy as np

from app.services.video_studio import scroll_pacing

SHAPE = (240, 320)


def _levels(*pictures: np.ndarray) -> list:
    return [[(1, picture)] for picture in pictures]


def _noise(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.uniform(0, 255, SHAPE).astype(np.float32)


def test_a_box_inside_a_standing_legend_stands_with_it_even_where_a_hover_lit_it():
    first = _noise(8457)
    second = _noise(8458)
    # A legend in the bottom left corner standing still; one of its rows lit by a hover on the second keyframe.
    legend = (10.0, 120.0, 100.0, 100.0)
    row = (14.0, 150.0, 50.0, 6.0)
    icon = (16.0, 152.0, 3.0, 3.0)
    second[120:220, 10:110] = first[120:220, 10:110]
    second[150:156, 14:64] += 40.0
    # A label over the moving canvas, nothing standing under it.
    label = (200.0, 60.0, 60.0, 14.0)
    _still, kept = scroll_pacing.standing(_levels(first, second), [legend, row, icon, label])
    assert legend in kept and row in kept and icon in kept
    assert label not in kept


def test_a_box_too_small_for_an_inside_is_judged_whole():
    first = _noise(1)
    second = _noise(2)
    # A count of 3 x 3 px standing still alone, over a canvas that changes everywhere else.
    count = (40.0, 40.0, 3.0, 3.0)
    second[40:43, 40:43] = first[40:43, 40:43]
    _still, kept = scroll_pacing.standing(_levels(first, second), [count])
    assert kept == [count]
    # The same box where the canvas moved under it: not standing.
    _still, kept = scroll_pacing.standing(_levels(first, _noise(3)), [count])
    assert kept == []


def test_a_box_inside_a_moving_one_is_judged_on_its_own():
    first = _noise(4)
    second = _noise(5)
    node_label = (100.0, 100.0, 80.0, 30.0)
    text = (104.0, 104.0, 40.0, 10.0)
    _still, kept = scroll_pacing.standing(_levels(first, second), [node_label, text])
    assert kept == []


def test_the_step_says_the_boxes_standing_still_apart_from_those_held():
    entry = {"paced": True, "fps": 30.0, "scroller": "canvas", "overlays": 20, "overlays_still": 20, "riding": 9}
    said = scroll_pacing._how(entry, 8.6)
    assert "20 box(es) standing still over its canvas kept in place" in said
    assert "held as painted" not in said
    mixed = scroll_pacing._how({**entry, "overlays_still": 18}, 8.6)
    assert "18 box(es) standing still over its canvas kept in place, 2 box(es) over its canvas held as painted" in mixed
    # Paced before the split: every held box still said as held.
    before = scroll_pacing._how({key: value for key, value in entry.items() if key != "overlays_still"}, 8.6)
    assert "20 box(es) over its canvas held as painted" in before
