"""``tests.paced_scroll`` reads a move off a rounded scroll record the way the pacer drew it."""

from __future__ import annotations

import pytest

from tests import paced_scroll

#: The record of CI run 41608166796 (Browser tool (Chromium) 1/3): ``late`` rounded to 0.131 from about 0.1313.
RECORD = {
    "paced": True,
    "late": 0.131,
    "wheel": [[0.9354, 0.0], [0.9354, 0.025], [2.2354, 1.0]],
    "log": [[0.9235, 0.0], [1.0086, 50.0], [2.5068, 2000.0], [2.8945, 2000.0]],
}


def test_a_frame_the_rounded_lag_puts_just_past_the_wheels_first_step_did_not_move():
    # On the rounded lag alone the wheel's 50 px step falls between the frames at 31/30 s and 32/30 s; on the pacer's
    # own lag, a third of a millisecond later, it falls after 32/30 s: both frames show the page at rest.
    assert paced_scroll.shown_at(RECORD, 32 / 30) - paced_scroll.shown_at(RECORD, 31 / 30) == pytest.approx(50.4, abs=0.1)
    assert paced_scroll.moved(RECORD, 31 / 30, 32 / 30) == 0.0


def test_a_move_every_allowed_lag_agrees_on_is_still_a_move():
    # Mid-scroll the wheel turns about 50 px a frame at 30 fps: no lag within the record's rounding hides it.
    assert paced_scroll.moved(RECORD, 1.5, 1.5 + 1 / 30) == pytest.approx(50.0, abs=0.5)


def test_a_scroll_not_turned_by_the_wheel_moves_on_its_log():
    record = {"paced": False, "log": [[0.0, 0.0], [1.0, 100.0]]}
    assert paced_scroll.moved(record, 0.25, 0.5) == pytest.approx(25.0)
