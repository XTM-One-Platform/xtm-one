"""The insert capture floor held over the seconds a recording's page changes, not its whole length (#10087).

Chromium's screencast hands over a frame only when the page paints: a still console gives 0 to 1 a second, so a
perfectly smooth take reads 4 to 7 fps over its whole length. Synthetic frames only - no browser, no database.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.video_studio import insert_floor, perceptual
from app.services.video_studio.perceptual import MIN_INSERT_PAINTED_FPS


def _marked(**extra):
    return insert_floor.mark_of({**insert_floor.mark(video_id="v", scene="s1", insert=0, quality="ultra"), **extra})


def _console(motion_fps: float, *, still_seconds: float = 25.0, scroll_seconds: float = 2.0):
    """A static console held *still_seconds* (one paint a second), a scroll painted at *motion_fps* for
    *scroll_seconds*, then still again: (times, changed)."""
    times = [float(t) for t in range(int(still_seconds / 2))]
    changed = [False] * (len(times) - 1)
    start = times[-1] + 1.0
    step = 1.0 / motion_fps
    count = int(scroll_seconds * motion_fps)
    for k in range(count):
        times.append(start + k * step)
        changed.append(True)
    end = times[-1]
    for k in range(1, int(still_seconds / 2)):
        times.append(end + 1.0 + k)
        changed.append(False)
    return times, changed


def test_a_smooth_static_console_passes_the_floor_its_whole_take_rate_fails():
    times, changed = _console(28.0)
    duration = times[-1] + 0.5
    measured = insert_floor.rate_over(times, insert_floor.motion_windows(times, changed), duration)
    # Over its whole length it reads like the refused takes (about 3 frames a second) ...
    assert measured["whole"] < MIN_INSERT_PAINTED_FPS
    # ... while its page painted at the scroll's own rate whenever it changed.
    assert measured["motion"] == pytest.approx(28.0, rel=0.05)
    assert measured["windows"] == 1
    judged = insert_floor.judge(
        _marked(),
        captured_frames=len(times),
        duration=duration,
        viewport=(1600, 900),
        pixel_ratio=2,
        measured=measured,
    )
    assert judged is None
    assert perceptual.under_capture_floor(len(times), duration, measured) is None


def test_a_take_that_drops_frames_during_a_scroll_still_fails():
    times, changed = _console(6.0)
    duration = times[-1] + 0.5
    measured = insert_floor.rate_over(times, insert_floor.motion_windows(times, changed), duration)
    assert measured["motion"] == pytest.approx(6.0, rel=0.1)
    judged = insert_floor.judge(
        _marked(),
        captured_frames=len(times),
        duration=duration,
        viewport=(1600, 900),
        pixel_ratio=2,
        measured=measured,
    )
    assert judged.kind == insert_floor.AGAIN
    second = insert_floor.judge(
        _marked(**{insert_floor.RETRY_KEY: {"fps": judged.fps}}),
        captured_frames=len(times),
        duration=duration,
        viewport=(1600, 900),
        pixel_ratio=2,
        measured=measured,
    )
    assert second.kind == insert_floor.FAILED
    assert "over the" in second.words() and "s its page changed" in second.words()
    assert perceptual.under_capture_floor(len(times), duration, measured) == pytest.approx(measured["motion"])


def test_a_still_page_and_a_lone_change_are_fully_captured():
    times = [0.0, 1.0, 2.0, 3.0, 3.03, 4.0, 5.0]
    # A click redrawn at once (one changed frame), the rest held: no window long enough to judge.
    changed = [False, False, False, True, False, False]
    measured = insert_floor.rate_over(times, insert_floor.motion_windows(times, changed), 5.5)
    assert measured["motion"] is None and measured["windows"] == 0
    assert perceptual.still(measured)
    assert perceptual.insert_rate(len(times), 5.5, measured) is None
    assert (
        insert_floor.judge(
            _marked(), captured_frames=len(times), duration=5.5, viewport=(1600, 900), pixel_ratio=2, measured=measured
        )
        is None
    )


def test_the_still_frame_before_a_scroll_is_never_counted_against_it():
    # Held 3 s, then a scroll at 30 fps: the window starts at its first changed frame, not at the frame 3 s before.
    times = [0.0, 3.0, *[3.0 + k / 30 for k in range(1, 31)]]
    changed = [True] * (len(times) - 1)
    windows = insert_floor.motion_windows(times, changed)
    assert windows == [(1, len(times) - 1)]
    measured = insert_floor.rate_over(times, windows, 4.5)
    assert measured["motion"] == pytest.approx(30.0, rel=0.05)


def test_changes_more_than_a_second_apart_are_two_windows():
    times = [0.0, 0.1, 0.2, 0.3, 0.4, 3.0, 3.1, 3.2, 3.3]
    changed = [True, True, True, True, False, True, True, True]
    assert insert_floor.motion_windows(times, changed) == [(1, 4), (6, 8)]


def test_a_take_recorded_before_the_measure_keeps_its_whole_take_rate():
    assert perceptual.insert_rate(54, 10.0, None) == pytest.approx(5.4)
    assert insert_floor.measured_of({"fps": 60}) is None
    assert insert_floor.measured_of({perceptual.INSERT_RATE_KEY: {"whole": 3.0, "motion": 29.0}})["motion"] == 29.0
    judged = insert_floor.judge(_marked(), captured_frames=54, duration=10.0, viewport=(1600, 900), pixel_ratio=2)
    assert judged.kind == insert_floor.AGAIN and judged.motion_seconds is None


def test_motion_rate_reads_the_painted_frames(tmp_path):
    from PIL import Image

    frames = []
    # Two still frames a second apart, then a white bar sliding across at 25 fps.
    for k, t in enumerate([0.0, 1.0]):
        name = f"s{k}.jpg"
        Image.new("RGB", (160, 90), (10, 10, 10)).save(tmp_path / name)
        frames.append((t, name))
    for k in range(12):
        image = Image.new("RGB", (160, 90), (10, 10, 10))
        for x in range(k * 12, k * 12 + 24):
            for y in range(90):
                if x < 160:
                    image.putpixel((x, y), (250, 250, 250))
        name = f"m{k}.jpg"
        image.save(tmp_path / name)
        frames.append((1.04 + k * 0.04, name))
    measured = insert_floor.motion_rate(frames, workdir=str(tmp_path), duration=2.0)
    assert measured["windows"] == 1
    assert measured["motion"] == pytest.approx(25.0, rel=0.1)
    assert measured["whole"] == pytest.approx(7.0)


def test_the_lint_and_the_save_read_the_motion_rate():
    take = SimpleNamespace(
        captured_frames=40, duration=12.0, insert_rate={"whole": 3.3, "motion": 28.0, "seconds": 1.2}
    )
    assert perceptual.under_capture_floor(take.captured_frames, take.duration, take.insert_rate) is None
    assert perceptual.under_capture_floor(take.captured_frames, take.duration, None) == pytest.approx(40 / 12)
