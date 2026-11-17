"""A page's own fixed bar stands still on every paced frame, however its keyframes' pixels read.

A sticky band is held from the keyframe pinning it when the keyframes about a frame missed it. A take of an
inner scroll container under a fixed top bar, painted at about 16 fps, still blinked: on some keyframes the bar's rows
were merged with rows that stood still on that pair only, the merged run was refused (the bar's box covers less than
half of it), and those keyframes held no band at all. Their bar rows were then drawn as content, translated with the
scroll: the bar slid down and a second copy of it appeared under the first.

What is proven here:

* :func:`scroll_pacing.steady_bands` sets the rows a fixed element of the page covers at the same place before and
  after the scroll on every keyframe, once the pixels confirmed them on one; never for an element that moved, a narrow
  one, or rows no keyframe confirmed;
* a paced scroll under a fixed bar whose band three keyframes in a row missed keeps the bar in place on every frame
  drawn, with no copy of it under itself and no band blink; with the steady rows switched off the same take blinks
  (the regression this guards);
* a stretch of the page settling on a step that holds it is said ``settling (advisory)``, never as motion that will
  step, graded advisory whatever its rate, and is no residual of a repair.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.services.video_studio import capture_rate, scroll_pacing
from tests.test_video_studio_paced_bands_7033 import RATIO, S_END, S_START, _decoded, _offset, _page
from tests.test_video_studio_sticky_bands_7098 import BAR_CSS, WIDTH, _bar, _keyframes, _take, _view_bar

KWARGS = {"ratio": 1.0, "top": 0, "left": 0, "right": WIDTH}
STILL_BOX = (0.0, 0.0, float(WIDTH), 60.0, "fixed")


def _with_bar() -> tuple[list[np.ndarray], list[float]]:
    moving, offsets = _keyframes()
    for grey in moving:
        grey[:60] = _bar()
    return moving, offsets


def test_a_fixed_bar_the_pixels_missed_on_some_keyframes_is_set_on_every_one():
    moving, offsets = _with_bar()
    bands = scroll_pacing.pinned_bands(moving, offsets)
    # Keyframes 1 to 3 missed it (a run merged with rows still on one pair, refused whole).
    for j in (1, 2, 3):
        bands[j][:] = False
    found = scroll_pacing.steady_bands(bands, [STILL_BOX, STILL_BOX], **KWARGS)
    assert found == [(0, 60)]
    assert all(mask[:60].all() and not mask[60:].any() for mask in bands)


def test_an_element_that_moved_a_narrow_one_or_rows_no_keyframe_confirmed_are_never_steady():
    moving, offsets = _with_bar()
    base = scroll_pacing.pinned_bands(moving, offsets)
    for j in (1, 2, 3):
        base[j][:] = False

    def tried(fixed):
        bands = [mask.copy() for mask in base]
        return scroll_pacing.steady_bands(bands, fixed, **KWARGS), bands

    # Read once only (it moved between the two reads): not vouched for as standing still.
    moved = (0.0, 120.0, float(WIDTH), 60.0, "sticky")
    found, bands = tried([STILL_BOX, moved])
    assert found == [] and not any(bands[j].any() for j in (1, 2, 3))
    # A floating button over the bar's rows: too narrow to stand for the whole width.
    button = (340.0, 4.0, 40.0, 52.0, "fixed")
    assert tried([button, button])[0] == []
    # A sticky box over content no keyframe ever held still.
    content = (0.0, 300.0, float(WIDTH), 60.0, "sticky")
    assert tried([content, content])[0] == []
    # The page's boxes not read: nothing is set.
    assert tried(None)[0] == []


def _missing(pinned, drop: tuple[int, ...]):
    """:func:`scroll_pacing.pinned_bands` whose band keyframes *drop* missed."""

    def pinned_bands(moving, offsets, rejected=None):
        bands = pinned(moving, offsets, rejected)
        for j in drop:
            if j < len(bands):
                bands[j][:] = False
        return bands

    return pinned_bands


def _bar_frames(tmp_path, monkeypatch, *, steady: bool):
    page = _page()
    box = [0, 0, 800, BAR_CSS, "fixed"]
    # The bar repainted lighter once the scroll settled (a badge): no keyframe-wide still band strips it, the bar is a
    # band of the moving part as on a live page.
    frames, events = _take(tmp_path, lambda offset, t: _view_bar(page, offset, shade=12 if t > 3.2 else 0), fixed=[box, box])
    monkeypatch.setattr(scroll_pacing, "pinned_bands", _missing(scroll_pacing.pinned_bands, (3, 4, 5)))
    if not steady:
        monkeypatch.setattr(scroll_pacing, "steady_bands", lambda *args, **kwargs: [])
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=30, pixel_ratio=RATIO)
    [scroll] = events[0]["scrolls"]
    assert scroll["paced"], scroll
    return page, paced, scroll


def _bar_off(page, paced, tmp_path) -> list[float]:
    """The instants of the frames drawn during the scroll whose bar rows are not the bar as painted."""
    bar = BAR_CSS * RATIO
    off = []
    for t, name in paced.frames:
        if not S_START <= t < S_END or not name.startswith("p"):
            continue
        shown = _decoded(tmp_path / name)
        if np.abs(shown[:bar] - _view_bar(page, _offset(t)).astype(np.int16)[:bar]).mean() >= 3.0:
            off.append(t)
    return off


def test_a_fixed_bar_missed_on_three_keyframes_in_a_row_never_moves_nor_blinks(tmp_path, monkeypatch):
    page, paced, scroll = _bar_frames(tmp_path, monkeypatch, steady=True)
    assert _bar_off(page, paced, tmp_path) == []
    assert "band_blink" not in scroll, scroll.get("band_blink")
    [[top, height]] = scroll["pinned"]
    assert top <= 2 and abs(height - BAR_CSS) <= 3, scroll["pinned"]
    # Never a second copy of the bar under it: no row below the bar painted the bar's own flat colour across.
    bar = BAR_CSS * RATIO
    for t, name in paced.frames[:: 4]:
        below = _decoded(tmp_path / name)[bar:]
        like_bar = (np.abs(below - np.array([20, 26, 44])).max(axis=2) < 6).mean(axis=1) > 0.9
        assert like_bar.sum() < 4, f"a copy of the bar at {t:.3f} s"


def test_without_the_steady_rows_the_same_take_draws_the_bar_off_its_place(tmp_path, monkeypatch):
    page, paced, _scroll = _bar_frames(tmp_path, monkeypatch, steady=False)
    assert _bar_off(page, paced, tmp_path), "the take meant to blink did not: the regression test proves nothing"


# ── a page settling on a step that holds it ─────────────────────────────

SETTLING = {"t": [10.0, 12.0], "frames": 60, "synthesised": 52, "share": 0.867, "fps": 30, "step": 4, "settling": True}
MOVING = {"t": [3.0, 4.5], "frames": 45, "synthesised": 33, "share": 0.733, "fps": 30, "step": 1, "canvas": "canvas#graph"}


def test_a_settling_stretch_is_said_advisory_never_motion_that_will_step():
    notes = capture_rate.motion_notes({"stepping": [SETTLING]})
    assert notes == ["Settling (advisory) at step 4 (the page drew on 2.0 s on a step that holds it): nothing to record again."]
    assert not any("will step" in note for note in notes)
    both = capture_rate.motion_notes({"stepping": [MOVING, SETTLING]})
    assert both[0].startswith("Stepping motion at step 1") and both[0].endswith("its motion will step.")
    assert both[-1].startswith("Settling (advisory) at step 4")


def test_a_settling_stretch_is_no_residual_of_a_repair():
    assert capture_rate.motion_notes({"stepping": [SETTLING]}, settling=False) == []
    assert len(capture_rate.motion_notes({"stepping": [MOVING, SETTLING]}, settling=False)) == 1


@pytest.mark.parametrize("rate", [1.0, 0.25, 4.0])
def test_a_settling_stretch_is_graded_advisory_whatever_its_rate(rate):
    grade = capture_rate.stepping_grade(SETTLING, rate)
    assert grade is not None and grade.severity == "advisory" and grade.settling
