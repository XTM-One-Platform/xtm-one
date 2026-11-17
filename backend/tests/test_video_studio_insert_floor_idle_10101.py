"""The insert capture floor tells a page at rest from a starved capture (#10101).

Chromium's screencast hands over a frame only when the page paints: a dashboard held on hover then wait gives 0 to 3
frames a second, and its hover cards seconds apart are discrete changes, not a slow motion. A take whose page asked to
move (animation frames requested, a transition, a scroll) and changed, but whose frames were not delivered, still
fails, naming its starved seconds. Synthetic frames only: no browser, no database.
"""

from __future__ import annotations

import pytest

from app.services.video_studio import capture_rate, insert_floor, perceptual
from app.services.video_studio.perceptual import MIN_INSERT_PAINTED_FPS


def _marked(**extra):
    return insert_floor.mark_of({**insert_floor.mark(video_id="v", scene="s1", insert=0, quality="high"), **extra})


def _judge(measured, *, frames, duration, **extra):
    return insert_floor.judge(
        _marked(**extra),
        captured_frames=frames,
        duration=duration,
        viewport=(1600, 900),
        pixel_ratio=2,
        measured=measured,
    )


def _dashboard():
    """A static dashboard recorded with hover + waits (frames kept each second like recording e8f341ce): a 1 s
    load at 25 fps, then hover cards and tooltips a second or more apart, each repainted once or twice, the page at
    rest between them. Its page requested animation frames only while it loaded."""
    times = [k / 25 for k in range(25)]
    changed = [True] * 24
    t = 1.0
    for k in range(20):
        t += 0.8 if k % 3 else 2.5
        times += [t, t + 0.04]
        changed += [True, k % 2 == 0]
    events = [
        {"step": 0, "action": "wait", "t0": 0.0, "painting": [[0.0, 1.0]]},
        {"step": 1, "action": "hover", "t0": 1.0, "painting": []},
    ]
    return times, changed, events, t + 2.0


def test_a_static_dashboard_with_hover_and_waits_passes_the_floor():
    times, changed, events, duration = _dashboard()
    # Before #10101: changes about a second apart form one slow window, and the take is refused.
    before = insert_floor.rate_over(times, insert_floor.motion_windows(times, changed), duration)
    assert before["motion"] < MIN_INSERT_PAINTED_FPS
    measured = insert_floor.classify(times, changed, insert_floor.expected_spans(events), duration)
    assert measured["painting"] is True
    assert measured["starved"] == []
    assert measured["motion"] == pytest.approx(25.0, rel=0.05)
    assert measured["idle"] > 20
    assert _judge(measured, frames=len(times), duration=duration) is None
    assert perceptual.under_capture_floor(len(times), duration, measured) is None


def test_a_page_that_animates_but_is_delivered_under_the_floor_fails_naming_its_starved_seconds():
    # Held still 3 s, then a chart animates for 2 s (animation frames requested all along) delivered at 6 fps.
    times = [0.0, 1.0, 2.0, 3.0, *[3.0 + k / 6 for k in range(1, 13)], 6.0, 7.0]
    changed = [False, False, False, *[True] * 12, False, False]
    events = [
        {"step": 0, "action": "wait", "t0": 0.0, "painting": []},
        {"step": 1, "action": "click", "t0": 3.0, "painting": [[3.0, 5.0]]},
    ]
    measured = insert_floor.classify(times, changed, insert_floor.expected_spans(events), 7.5)
    assert measured["motion"] == pytest.approx(6.0, rel=0.1)
    assert measured["starved"] and measured["starved"][0][:2] == [pytest.approx(3.17, abs=0.01), 5.0]
    first = _judge(measured, frames=len(times), duration=7.5)
    assert first.kind == insert_floor.AGAIN
    failed = _judge(measured, frames=len(times), duration=7.5, **{insert_floor.RETRY_KEY: {"fps": first.fps}})
    assert failed.kind == insert_floor.FAILED
    words = failed.words()
    assert "starved at 3.2-5 s (6 fps)" in words
    assert "idle" in words
    # Starved: the size it would sustain is still advised.
    assert "pixel ratio" in words


def test_a_starved_window_needs_the_page_to_have_asked_for_frames():
    # The same 6 fps run with no paint requested is outside the expected spans: its frames 1/6 s apart are not a
    # dense run (DENSE_GAP), so the page is held - never refused for changes it did not ask to animate.
    times = [0.0, *[1.0 + k / 6 for k in range(12)], 4.0]
    changed = [True] * 12 + [False]
    events = [{"step": 0, "action": "hover", "t0": 0.0, "painting": []}]
    measured = insert_floor.classify(times, changed, insert_floor.expected_spans(events), 4.5)
    assert measured["motion"] is None and measured["starved"] == []
    assert _judge(measured, frames=len(times), duration=4.5) is None


def test_a_scroll_counts_as_expected_motion():
    times = [0.0, *[1.0 + k / 5 for k in range(10)], 4.0]
    changed = [True] * 10 + [False]
    events = [{"step": 0, "action": "scroll", "t0": 1.0, "painting": [], "scrolls": [{"t": [1.0, 3.0]}]}]
    measured = insert_floor.classify(times, changed, insert_floor.expected_spans(events), 4.5)
    assert measured["starved"] and measured["motion"] == pytest.approx(5.0, rel=0.1)


def test_a_failure_from_idleness_never_advises_a_softer_size():
    measured = {
        "whole": 3.0,
        "motion": 9.0,
        "seconds": 1.0,
        "windows": 1,
        "starved": [],
        "idle": 30.0,
        "painting": True,
    }
    judged = insert_floor.Judged(
        insert_floor.FAILED, 9.0, 8.0, (1600, 900), 2.0, motion_seconds=1.0, starved=(), idle=30.0
    )
    assert "pixel ratio" not in judged.sustained_words() and "window" not in judged.sustained_words()
    # A take measured before #10101 (no painting): no softer size either, it is recorded again on this build.
    legacy = insert_floor.Judged(insert_floor.FAILED, 6.0, 5.0, (1600, 900), 2.0, motion_seconds=2.0)
    assert "pixel ratio" not in legacy.sustained_words()
    assert insert_floor.measured_of({perceptual.INSERT_RATE_KEY: measured})["idle"] == 30.0


def test_events_without_painting_keep_the_10087_measure():
    assert insert_floor.expected_spans([{"step": 0, "action": "wait"}]) is None
    assert insert_floor.expected_spans(None) is None


def test_the_capture_line_says_idle_and_starved_seconds():
    capture = {
        "rates": [23, 11, 8, 1, 3, 12, 0, 0],
        "insert_rate": {"idle": 24.2, "starved": [[3.17, 5.0, 6.0]], "painting": True},
    }
    said = capture_rate.rates_words(capture)
    assert said.startswith("frames kept each second: 23 11 8 1 3 12 0 0")
    assert "idle 24 s: the page rested, frames held" in said
    assert "starved at 3.17-5 s (6 fps)" in said
    assert capture_rate.rates_words({"rates": [1, 2]}) == "frames kept each second: 1 2"
