"""A recording's scroll pacing on its edges. Synthetic keyframes and logs, made as ``test_..._6408`` makes them.

* an upward scroll (``to: top``) is read and paced as a downward one: the rows move the other way;
* a scroll container's static bands - a toolbar over it, a sidebar beside it - are kept as painted;
* ``register`` says nothing on a flat page or on two keyframes sharing too few rows, and the scroll is still paced from
  its log;
* a scroll faster than a screen between two paints holds the keyframe before it there and says the seconds that keep
  every two paints overlapping;
* ``moves`` plays a wheel that stalls through the instants it arrived;
* ``clock_lag`` finds a log running ahead of the paint, or further behind it than two frames;
* a mostly idle take recorded before its scrolls were logged plays at the frames it painted over its length.
"""

from __future__ import annotations

from itertools import pairwise

import numpy as np
import pytest

from app.services.video_studio import scroll_pacing
from app.services.video_studio.scroll_pacing import Scroll
from tests.test_video_studio_scroll_pacing_6408 import FRAME, HEADER_CSS, PAGE_CSS, RATIO, _decoded, _jpeg, _page, _view


def _take(tmp_path, page, offset, *, paint_fps: float, start: float, end: float, lag: float = 0.0, until: float = 4.0):
    """The keyframes and the step's event of a scroll whose offset at *t* is ``offset(t)`` (CSS px), logged at 60 Hz
    *lag* seconds behind the paint."""
    frames, t, k = [], 0.05, 0
    while t < until:
        name = f"f{k:06d}.jpg"
        _jpeg(_view(page, offset(t)), tmp_path / name)
        frames.append((round(t, 4), name))
        t += 1 / paint_fps
        k += 1
    samples, s = [], start - 0.1
    while s <= end + 0.2 + 1e-9:
        samples.append([round(s, 4), offset(s + lag), 0, []])
        s += 1 / scroll_pacing.SCROLL_SAMPLE_HZ
    event = {"step": 0, "action": "scroll", "ok": True, "t0": start, "t1": end, "scrolls": [{"t": [start - 0.1, end + 0.2], "samples": samples}]}
    return frames, [event]


def _unique_page() -> np.ndarray:
    """A 4000 CSS px page whose every band of rows is its own: 8 px blocks of random greys, which a JPEG keeps. The
    6408 page's marks come back every 30 rows, close enough for a short move to read 30 rows off."""
    rng = np.random.default_rng(11)
    blocks = rng.integers(40, 220, (PAGE_CSS * RATIO // 8, FRAME[0] // 8), dtype=np.uint8)
    grey = np.kron(blocks, np.ones((8, 8), dtype=np.uint8))
    return np.repeat(grey[:, :, None], 3, axis=2)


def test_an_upward_scroll_is_paced_as_a_downward_one(tmp_path):
    page = _unique_page()

    def up(t: float) -> float:
        # From 2400 px down the page back to 400 px - at rest mid-page either side, as the downward test is.
        return 2400.0 - min(max(t - 1.0, 0.0), 2.0) * 1000.0

    frames, events = _take(tmp_path, page, up, paint_fps=2.1, start=1.0, end=3.0)
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=30, pixel_ratio=RATIO)
    [scroll] = events[0]["scrolls"]
    assert scroll["paced"] and scroll["fps"] == 30.0 and scroll["scroller"] == "window"
    header = HEADER_CSS * RATIO
    during = [(t, name) for t, name in paced.frames if 1.0 <= t < 3.0]
    assert len(during) == 60
    for t, name in during[::7]:
        shown = _decoded(tmp_path, name)
        error = min(
            float(np.abs(shown[header:] - _view(page, up(t) + nudge / RATIO).astype(np.int16)[header:]).mean()) for nudge in (-1, 0, 1)
        )
        assert error < 4.0, f"the frame at {t:.3f} s"
    # Registered off the pixels, the rows moved up the page: a negative shift between two keyframes.
    a, b = (_view(page, offset).astype(np.float32).mean(axis=2)[header:] for offset in (1200.0, 1150.0))
    assert scroll_pacing.register(a, b, -50 * RATIO, search=48) == pytest.approx(-50 * RATIO, abs=1)


def test_a_scroll_container_s_static_bands_are_kept_as_painted():
    rng = np.random.default_rng(5)
    content = (rng.random((1200, 800)) * 200).astype(np.float32)
    greys = []
    for shift in (0, 40, 80, 120):
        grey = np.zeros((600, 1000), dtype=np.float32)
        grey[:60] = 30.0  # a toolbar over the container
        grey[:, :150] = 80.0  # a sidebar beside it
        grey[60:, 150:950] = content[shift : shift + 540]
        grey[:, 950:] = 50.0  # a scrollbar track standing still
        greys.append(grey)
    assert scroll_pacing.static_bands(greys, (0, 0, 1000, 600)) == (60, 0, 150, 50)
    # One keyframe only: nothing is told standing still.
    assert scroll_pacing.static_bands(greys[:1], (0, 0, 1000, 600)) == (0, 0, 0, 0)


def test_register_says_nothing_on_a_flat_page_or_on_keyframes_sharing_too_few_rows():
    flat = np.full((900, 800), 128.0, dtype=np.float32)
    assert scroll_pacing.register(flat, flat, 40.0, search=24) is None
    rng = np.random.default_rng(3)
    page = (rng.random((3000, 400)) * 255).astype(np.float32)
    a, b = page[:900], page[870:1770]
    # 870 of 900 rows apart: under MIN_OVERLAP of them shared.
    assert 900 * (1 - scroll_pacing.MIN_OVERLAP) < 870
    assert scroll_pacing.register(a, b, 870.0, search=24) is None
    assert scroll_pacing.register(a, page[40:940], 40.0, search=24) == pytest.approx(40.0, abs=1)


def test_a_flat_page_is_still_paced_from_its_log(tmp_path):
    flat = np.full((PAGE_CSS * RATIO, FRAME[0], 3), 200, dtype=np.uint8)
    frames, events = _take(tmp_path, flat, lambda t: min(max(t - 1.0, 0.0), 2.0) * 1000.0, paint_fps=2.1, start=1.0, end=3.0)
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=30, pixel_ratio=RATIO)
    [scroll] = events[0]["scrolls"]
    # Its keyframes are all alike: nothing moved on them, it keeps them, and the record says so.
    assert scroll["why"] == "nothing of it moved on its keyframes" and not scroll.get("paced")
    assert paced.frames == frames


def test_a_scroll_faster_than_a_screen_between_two_paints_holds_and_says_the_seconds(tmp_path):
    page = _page()

    def fast(t: float) -> float:
        return min(max(t - 1.0, 0.0), 1.0) * 3000.0

    frames, events = _take(tmp_path, page, fast, paint_fps=1.2, start=1.0, end=2.0)
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=30, pixel_ratio=RATIO)
    [scroll] = events[0]["scrolls"]
    assert scroll.get("outran"), scroll
    assert scroll["held"] > 0 and scroll["fps"] < 30.0
    assert scroll.get("seconds") and float(scroll["seconds"]) > 1.0
    assert any("seconds" in note for note in paced.notes)


def test_moves_plays_a_wheel_that_stalls_through_the_instants_it_arrived():
    # A wheel landing on each 100 px increment and stalling 100 ms on it, at 60 Hz.
    log = [(0.0, 0.0), (0.1, 0.0)]
    t = 0.1
    for step in range(1, 4):
        log.append((round(t + 1 / 60, 4), step * 100.0))
        for _ in range(6):
            t += 1 / 60
            log.append((round(t + 1 / 60, 4), step * 100.0))
        t += 1 / 60
    log.append((round(t + 0.5, 4), 300.0))
    kept = scroll_pacing.moves(log)
    offsets = [offset for _t, offset in kept]
    assert offsets == [0.0, 0.0, 100.0, 200.0, 300.0, 300.0]
    # Played through the instants it arrived, the scroll never stands still between two increments.
    scroll = Scroll((0.1, kept[-2][0]), kept)
    middles = [scroll.offset((a[0] + b[0]) / 2) for a, b in pairwise(kept[2:-1])]
    assert middles == [pytest.approx(150.0), pytest.approx(250.0)]
    assert scroll_pacing.moves(log[:2]) == log[:2]


@pytest.mark.parametrize("frames_off", [-3, 5])
def test_clock_lag_finds_a_log_ahead_of_the_paint_or_far_behind_it(frames_off):
    lag = frames_off / 60

    def offset(t: float) -> float:
        return min(max(t - 1.0, 0.0), 2.0) * 1000.0

    log = [(round(0.5 + n / 60, 4), offset(0.5 + n / 60 + lag)) for n in range(200)]
    scroll = Scroll((0.5, 3.8), log)
    # Keyframes either side of where it starts and stops: inside a steady stretch, every lag fits.
    times = [0.6, 1.08, 1.56, 2.04, 2.52, 3.0, 3.48]
    found = scroll_pacing.clock_lag(scroll, times, [offset(t) for t in times])
    assert found == pytest.approx(-lag, abs=0.004)


def test_a_mostly_idle_take_recorded_before_its_scrolls_were_logged_plays_at_what_it_painted():
    events = [{"step": 0, "action": "goto"}, {"step": 1, "action": "wait"}, {"step": 2, "action": "scroll"}, {"step": 3, "action": "wait"}]
    rate = scroll_pacing.take_rate(events, captured_frames=90, duration=45.0)
    assert (rate.fps, rate.scrolls, rate.steps, rate.step) == (2.0, True, True, 2)
    assert scroll_pacing.PACED_RATES == (30, 60) and scroll_pacing.DEFAULT_TAKE_FPS in scroll_pacing.PACED_RATES
