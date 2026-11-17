"""A canvas drawn in bursts, its page held back by its container's quota, is copied at the take's rate (#10970).

The measurement of #10972 (take 001ef823, GPU GL on an L4, the worker held back 6.3 s by its quota) copied six
canvas transitions at 22 fps of 33 to 45 drawn. A page whose thread its quota stops draws a burst of frames at the
screen's rate, then nothing for 100 to 200 ms: a budget two copies deep is full after 67 ms of the gap and loses the
rest of its refill, so the next burst is copied at two copies and then at the burst's pace. The budget is deeper
now and a canvas starts with one copy in hand.
"""

import random

from app.services.video_studio import canvas_alone

NAME_JS = "(el) => el.id"


def _copied(times: list[float], fps: int, budget: float, fresh_ms: float | None = None) -> int:
    """The page script's budget, step for step: one copy in hand at the first frame, and again after *fresh_ms*
    without a frame."""
    held, filled, copied = 1.0, times[0], 0
    for at in times:
        if fresh_ms is not None and (at - filled) * 1000 > fresh_ms:
            held = 1.0
        else:
            held = min(budget, held + (at - filled) * fps)
        filled = at
        if held >= 1:
            held, copied = held - 1, copied + 1
    return copied


def _bursts(run_ms: float, period_ms: float, seed: int, seconds: float = 5.0, gap_jitter: float = 0.0) -> list[float]:
    """Frames at 60 Hz while the quota lets the page run (*run_ms*, jittered) of each *period_ms* (jittered by
    *gap_jitter*), none after."""
    rng = random.Random(seed)  # noqa: S311 - a seeded jitter for a test, no secret
    times: list[float] = []
    began = 0.0
    for _period in range(int(seconds * 1000 / period_ms)):
        running = run_ms * (1 + rng.uniform(-0.3, 0.3))
        k = 0
        while k * 1000 / 60 < running:
            times.append((began + k * 1000 / 60) / 1000)
            k += 1
        began += period_ms * (1 + rng.uniform(-gap_jitter, gap_jitter)) if gap_jitter else period_ms
    return times


def test_a_page_held_back_by_its_quota_drawing_33_to_45_fps_is_copied_at_28_or_more():
    # Quota periods of 200 and 300 ms reproduce the take: 34 to 40 fps drawn, copied at 20 to 25 two copies deep.
    for run_ms, period_ms in ((100, 200), (120, 200), (150, 300), (180, 300)):
        times = _bursts(run_ms, period_ms, seed=10970 + run_ms)
        seconds = times[-1] - times[0]
        drawn = len(times) / seconds
        assert drawn >= 33
        assert _copied(times, 30, 2.0) / seconds < 25, "two copies deep falls short here, as measured"
        assert _copied(times, 30, canvas_alone.COPY_BUDGET) / seconds >= 29


def test_a_wheel_zoom_whose_quota_gaps_run_to_half_a_second_is_copied_at_28_on_average():
    # Take e48150a0 (#10978 deployed, held back 3.8 s): wheel step 3 copied at 24 fps of 31 drawn six copies deep.
    for run_ms, period_ms in ((150, 300), (200, 400), (250, 500)):
        drawn, rates, six = [], [], []
        for seed in range(20):
            times = _bursts(run_ms, period_ms, seed=seed, seconds=2.6, gap_jitter=0.5)
            seconds = times[-1] - times[0]
            drawn.append(len(times) / seconds)
            rates.append(_copied(times, 30, canvas_alone.COPY_BUDGET, canvas_alone.COPY_FRESH_MS) / seconds)
            six.append(_copied(times, 30, 6.0) / seconds)
        assert sum(drawn) / len(drawn) >= 33
        assert sum(rates) / len(rates) >= 28
        assert min(rates) >= 25
        assert sum(rates) >= sum(six)


def test_the_deeper_budget_never_copies_above_the_take_s_rate_over_a_run():
    for rate in (40, 60):
        times = [k / rate for k in range(rate * 10)]
        copied = _copied(times, 30, canvas_alone.COPY_BUDGET, canvas_alone.COPY_FRESH_MS)
        assert copied <= 30 * (times[-1] - times[0]) + 1


def test_a_canvas_drawn_again_after_standing_still_starts_with_one_copy_in_hand():
    # Two seconds still, then a 60 Hz transition: its first 100 ms are copied at the rate, never in a burst.
    times = [0.0] + [2.0 + k / 60 for k in range(6)]
    assert _copied(times, 30, canvas_alone.COPY_BUDGET, canvas_alone.COPY_FRESH_MS) <= 1 + 1 + 3
    script = canvas_alone.tap_script(30, NAME_JS, encoders=3)
    assert f"FRESH_MS = {int(canvas_alone.COPY_FRESH_MS)};" in script
    assert "st.budget = t - st.filled > FRESH_MS ? 1 :" in script


def test_the_page_script_starts_a_canvas_with_one_copy_in_hand():
    script = canvas_alone.tap_script(30, NAME_JS, encoders=3)
    assert f"const BUDGET = {float(canvas_alone.COPY_BUDGET)!r};" in script
    assert "states.get(key) || { budget: 1, filled: t," in script
