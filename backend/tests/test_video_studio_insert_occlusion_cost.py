"""The occlusion read's channel distance is the same values, read cheaper (xtm-one #9529).

The keyed static insert's first preview reads what stands in front of the surface on every frame: a 3 x 3 search of
per-pixel channel distances (``Surface._occluding``, and ``Surface._full`` on an unkeyed insert). numpy's
``max(axis=-1)`` over a length-3 channel axis is a strided reduction that cost about 40 % of that read on a profile of
a keyed static insert; ``_channel_max`` takes two element-wise maxima instead. Pinned here value for value, so the
cost cut never moves a gate.
"""

from __future__ import annotations

import numpy as np

from app.services.video_studio import screen_insert


def test_the_channel_maximum_is_numpys_reduction_value_for_value() -> None:
    rng = np.random.default_rng(7)
    pixels = (rng.random((97, 131, 3)) * 255.0).astype(np.float32)
    assert np.array_equal(screen_insert._channel_max(pixels), pixels.max(axis=-1))
    # A strided view of a difference, as the 3 x 3 search slices it.
    view = np.abs(pixels[1:, 2:] - pixels[:-1, :-2])
    assert np.array_equal(screen_insert._channel_max(view), view.max(axis=-1))
    assert screen_insert._channel_max(view).dtype == view.max(axis=-1).dtype


def test_the_channel_maximum_keeps_nan_where_numpy_does() -> None:
    pixels = np.array([[[1.0, np.nan, 3.0], [4.0, 5.0, 6.0]]], dtype=np.float32)
    got, want = screen_insert._channel_max(pixels), pixels.max(axis=-1)
    assert np.array_equal(np.isnan(got), np.isnan(want))
    assert got[0, 1] == want[0, 1]
