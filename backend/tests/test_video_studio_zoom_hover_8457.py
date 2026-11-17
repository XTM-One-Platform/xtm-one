"""A canvas wheel zoom whose only unexplained change sits under the cursor is registered without it.

Production takes of a force-directed graph zoomed by the wheel kept reading "1 frame(s) held, not blended (keyframes
not registered: 1 pair(s) no zoom explains; the best zoom tried explains 21% of what tells them apart, 25% needed)"
after #9181, with a relationship card open over the graph on the step's frames: the cursor stood still while the zoom
brought another link under it, so the page lit a different link there, a change no zoom explains.
:func:`scroll_pacing.register_zoom` now judges such a pair again on the coarsest width without :data:`HOVER_PX` about
the cursor. What is proven here: a zoom whose pixels change under the cursor is registered and counted; without a
cursor the same pair is held; two unrelated pictures stay held with a cursor; the step words name the pairs.
"""

from __future__ import annotations

import math

import numpy as np

from app.services.video_studio import scroll_pacing

SHAPE = (240, 320)
POINT = (160.0, 120.0)
CURSOR = (150.0, 110.0)


def _noise(seed: int) -> np.ndarray:
    from scipy.ndimage import gaussian_filter

    rng = np.random.default_rng(seed)
    return gaussian_filter(rng.uniform(0, 255, SHAPE), 2.0).astype(np.float32) * 3.0


def _pair() -> tuple[np.ndarray, np.ndarray]:
    first = _noise(41)
    made = scroll_pacing.Zoom(1.1, (1 - 1.1) * POINT[0], (1 - 1.1) * POINT[1])
    drawn = scroll_pacing._zoomed(first, made, first.shape)
    assert drawn is not None
    out, rows, cols = drawn
    second = first.copy()
    second[rows, cols] = out
    # What the pointer lights: a large change about the cursor, unrelated to the zoom.
    yy, xx = np.ogrid[: SHAPE[0], : SHAPE[1]]
    lit = np.hypot(xx - CURSOR[0], yy - CURSOR[1]) <= 90
    second[lit] = _noise(42)[lit] * 4.0 + 600.0
    return first, second


def test_a_zoom_changed_only_under_the_cursor_is_registered_without_it():
    first, second = _pair()
    levels = [[(1, picture)] for picture in (first, second)]
    still = [np.zeros(SHAPE, dtype=bool)]
    explained: list[float] = []
    # Without the cursor, the change it lit outweighs the zoom: held.
    assert scroll_pacing.register_zoom(levels[0], levels[1], [POINT], still, explained) is None
    assert explained and explained[0] < scroll_pacing.ZOOM_EXPLAINS
    hovered: list[int] = []
    zoom = scroll_pacing.register_zoom(levels[0], levels[1], [POINT], still, None, CURSOR, hovered)
    assert zoom is not None and zoom != scroll_pacing.IDENTITY
    assert math.isclose(zoom.scale, 1.1, rel_tol=2e-2), zoom
    assert hovered == [1]


def test_two_unrelated_pictures_stay_held_with_a_cursor():
    a = [(1, _noise(43))]
    b = [(1, _noise(44))]
    still = [np.zeros(SHAPE, dtype=bool)]
    explained: list[float] = []
    hovered: list[int] = []
    assert scroll_pacing.register_zoom(a, b, [POINT], still, explained, CURSOR, hovered) is None
    assert hovered == []
    assert len(explained) == 1 and explained[0] < scroll_pacing.ZOOM_EXPLAINS


def test_the_step_words_name_the_pairs_registered_without_what_the_cursor_lit():
    entry = {"paced": True, "fps": 30.0, "scroller": "canvas", "hovered": 1}
    said = scroll_pacing._how(entry, 11.3)
    assert "1 pair(s) of keyframes registered without what the cursor lit" in said, said
