"""A GPU page's copy queue holds a whole copy budget, so a burst after a quota gap is copied, not skipped (#10970).

The retest of #10988 (a10a3f48, take a103c4df, GPU GL on an L4) copied a wheel zoom at 25 fps of 32 drawn and said
51 copies skipped, every encoder behind: three encoders held 4 copies each (12) while the budget let the page spend
15 at once after a gap of its container's quota, so the end of each burst was dropped at the queue.
"""

import math
import random

from app.services.video_studio import canvas_alone

NAME_JS = "(el) => el.id"


def test_several_encoders_hold_a_whole_budget_and_the_copy_drawn_now():
    for encoders in (2, 3, 4, 8):
        depth = canvas_alone.flight_depth(encoders)
        assert depth >= math.ceil(canvas_alone.COPY_BUDGET) + 1
        assert depth >= canvas_alone.IN_FLIGHT * encoders


def test_one_encoder_keeps_its_own_depth():
    # Software GL: one encoder whose threads draw the page too, never asked to hold a burst.
    assert canvas_alone.flight_depth(1) == canvas_alone.IN_FLIGHT
    assert canvas_alone.flight_depth(0) == canvas_alone.IN_FLIGHT


def test_the_page_script_reads_the_depth():
    script = canvas_alone.tap_script(30, NAME_JS, encoders=3)
    assert f"ENCODERS = 3, FLIGHT = {canvas_alone.flight_depth(3)};" in script
    assert "__FLIGHT__" not in script
    assert f"FLIGHT = {canvas_alone.IN_FLIGHT};" in canvas_alone.tap_script(30, NAME_JS, encoders=1)


def _bursts(seed: int, seconds: float = 8.0) -> tuple[list[float], list[tuple[float, float]]]:
    """A page drawing at 60 fps in bursts of 250 to 450 ms between quota gaps of 300 to 500 ms (take a103c4df)."""
    rng = random.Random(seed)  # noqa: S311 - a seeded jitter for a test, no secret
    t, draws, gaps = 0.0, [], []
    while t < seconds:
        end = t + rng.uniform(0.25, 0.45)
        while t < end:
            t += 1 / 60
            draws.append(t)
        gap = rng.uniform(0.3, 0.5)
        gaps.append((t, t + gap))
        t += gap
    return draws, gaps


def _copied(
    draws: list[float], gaps: list[tuple[float, float]], depth: int, encoded_per_second: float
) -> tuple[int, int]:
    """The page script's budget and queue, step for step: copies made and copies skipped, every encoder behind. The
    encoders stand still through a gap, as the page does."""
    held, filled, copied, skipped, queue, free = 1.0, draws[0], 0, 0, [], 0.0
    for at in draws:
        queue = [done for done in queue if done > at]
        held, filled = min(canvas_alone.COPY_BUDGET, held + (at - filled) * 30), at
        if held < 1:
            continue
        if len(queue) >= depth:
            skipped += 1
            continue
        held, copied = held - 1, copied + 1
        done = max(at, free) + 1 / encoded_per_second
        while any(a <= done < b for a, b in gaps):
            done += 0.005
        free = done
        queue.append(done)
    return copied, skipped


def test_a_burst_after_a_quota_gap_is_queued_where_the_old_depth_skipped_it():
    # Encoders that keep up on average (three at about 19 copies a second each, #8457) but not with a burst.
    for seed in range(5):
        draws, gaps = _bursts(seed)
        old = _copied(draws, gaps, canvas_alone.IN_FLIGHT * 3, 45.0)
        new = _copied(draws, gaps, canvas_alone.flight_depth(3), 45.0)
        assert new[0] > old[0] and new[1] < old[1]


def test_the_deeper_queue_never_copies_above_the_take_s_rate():
    draws, gaps = _bursts(10970)
    copied, _ = _copied(draws, gaps, canvas_alone.flight_depth(3), 1000.0)
    assert copied <= 30 * (draws[-1] - draws[0]) + canvas_alone.COPY_BUDGET
