"""Where what the best zoom leaves of a held canvas zoom pair sits: in one place, or all over the canvas.

Production takes of a force-directed graph zoomed by the wheel kept reading "held, not blended (keyframes not
registered: 2 pair(s) no zoom explains; the best zoom tried explains 22% of what tells them apart, 25% needed)" after
slices #9100 to #9205, each of which tuned the search without moving the share. The step now also says in how many
of the canvas's 16 parts what the best zoom leaves sits, so the next probe tells a change in one place (a card, a
label, what the cursor lit) from a picture changed all over (the graph's layout moving its nodes, a hold that is
right). What is proven here: a change in one corner is named in one part, two unrelated pictures in most of them,
an exact zoom in none, and the step words carry the count.
"""

from __future__ import annotations

import numpy as np

from app.services.video_studio import scroll_pacing

SHAPE = (240, 320)
POINT = (160.0, 120.0)


def _noise(seed: int) -> np.ndarray:
    from scipy.ndimage import gaussian_filter

    rng = np.random.default_rng(seed)
    return gaussian_filter(rng.uniform(0, 255, SHAPE), 2.0).astype(np.float32) * 3.0


def _zoomed(first: np.ndarray) -> tuple[np.ndarray, scroll_pacing.Zoom]:
    made = scroll_pacing.Zoom(1.1, (1 - 1.1) * POINT[0], (1 - 1.1) * POINT[1])
    drawn = scroll_pacing._zoomed(first, made, first.shape)
    assert drawn is not None
    out, rows, cols = drawn
    second = first.copy()
    second[rows, cols] = out
    return second, made


def test_a_change_in_one_corner_is_named_in_one_part():
    first = _noise(51)
    second, made = _zoomed(first)
    second[10:50, 10:70] = _noise(52)[10:50, 10:70] * 3.0 + 400.0
    weight = np.ones(SHAPE, dtype=np.float32)
    assert scroll_pacing._left_parts(first, second, weight, made) == 1


def test_two_unrelated_pictures_leave_most_parts():
    weight = np.ones(SHAPE, dtype=np.float32)
    parts = scroll_pacing._left_parts(_noise(53), _noise(54), weight, scroll_pacing.IDENTITY)
    assert parts >= 10, parts


def test_an_exact_zoom_leaves_nothing():
    first = _noise(55)
    second, made = _zoomed(first)
    weight = np.zeros(SHAPE, dtype=np.float32)
    assert scroll_pacing._left_parts(first, second, weight, made) == 0


def test_a_held_pair_reports_its_parts():
    a = [(1, _noise(56))]
    b = [(1, _noise(57))]
    still = [np.zeros(SHAPE, dtype=bool)]
    left: list[int] = []
    assert scroll_pacing.register_zoom(a, b, [POINT], still, None, None, None, left) is None
    assert len(left) == 1 and left[0] >= 10, left


def test_the_step_says_where_what_the_zoom_leaves_sits():
    entry = {
        "paced": True,
        "fps": 30.0,
        "scroller": "canvas",
        "held": 3,
        "held_not_blended": True,
        "held_why": {"unexplained": 2},
        "held_explained": 0.22,
        "held_left_parts": 3,
    }
    said = scroll_pacing._how(entry, 9.9)
    assert (
        "the best zoom tried explains 22% of what tells them apart, 25% needed; "
        "what it leaves sits in 3 of 16 parts of the canvas)" in said
    ), said
