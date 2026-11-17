"""The time band the ultra render test holds the pre-flight to, without drawing a frame.

``test_video_studio_ultra_render_chromium`` runs only in the ``Video Studio ultra render`` workflow; its band is
checked here on every CI run: a runner twice as fast as the default rate passes, one 1.4 times slower passes, a
render over 1.5 times the estimate fails, and one under a third of it fails.
"""

from __future__ import annotations

import pytest

from app.services.video_studio import render_preflight
from tests.test_video_studio_ultra_render_chromium import FASTEST_SHARE, RUNNER_VARIATION, time_band

SAID = 1135.09  # what the pre-flight said for the fixture on 31 Oct 2026, from DEFAULT_PAGE_RATE


def _within(took: float) -> bool:
    fastest, slowest = time_band(SAID)
    return fastest <= took <= slowest


@pytest.mark.parametrize(
    ("took", "passes"),
    [
        (SAID / 2, True),  # a runner twice as fast as the default rate
        (614.04, True),  # a red CI attempt
        (591.0, True),  # the red of the 31 Oct 2026 nightly
        (SAID, True),
        (SAID * 1.4, True),  # a runner 1.4 times slower
        (SAID * 1.51, False),  # the pre-flight under-promised
        (SAID / 3.1, False),  # a model more pessimistic than the correction absorbs
    ],
)
def test_the_band_lets_a_faster_runner_pass_and_holds_the_slow_side(took, passes):
    assert _within(took) is passes


def test_the_fast_side_is_the_runner_variation_over_the_correction_floor():
    floor = render_preflight.CORRECTION_BOUNDS[0]
    assert pytest.approx(1.5) == RUNNER_VARIATION
    assert pytest.approx(floor / RUNNER_VARIATION) == FASTEST_SHARE
    band = time_band(300.0)
    assert band == pytest.approx((100.0, 450.0))
