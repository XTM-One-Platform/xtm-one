"""A canvas is copied at each frame the page draws while its copy budget holds one (#10961).

A grid of fixed 1/fps slots dropped the second of two frames drawn within one slot and had none in the next: a GPU
canvas painting at 33 fps was copied 22 times a second (take e5a71484, 429 copies drawn, 422 encoded, no encoder
behind). The page script now paces copies with a budget refilled at the take's rate, at most
``canvas_alone.COPY_BUDGET`` ahead.
"""

import random

from app.services.video_studio import canvas_alone

NAME_JS = "(el) => el.id"


def test_the_page_script_paces_copies_with_a_budget_not_fixed_slots():
    script = canvas_alone.tap_script(30, NAME_JS, encoders=3)
    assert f"const BUDGET = {float(canvas_alone.COPY_BUDGET)!r};" in script
    assert "Math.min(BUDGET, st.budget + (t - st.filled) * RATE / 1000)" in script
    assert f"const RATE = {canvas_alone.copy_rate(30, 3)!r};" in script
    assert "st.budget -= 1" in script
    assert "Math.floor(t * FPS / 1000)" not in script and "st.slot" not in script
    assert "__" not in script.replace("__xtm", "")


def _copied(times: list[float], fps: int, *, budget: float | None) -> int:
    """How many of the frames drawn at *times* (seconds) the page copies: by fixed 1/fps slots (*budget* None), as
    before, or by the page script's budget (its arithmetic, step for step)."""
    copied, slot, held, filled = 0, -1, 1.0, times[0]
    for t in times:
        if budget is None:
            now = int(t * fps)
            if now != slot:
                slot, copied = now, copied + 1
            continue
        held, filled = min(budget, held + (t - filled) * fps), t
        if held >= 1:
            held, copied = held - 1, copied + 1
    return copied


def _drawn(low_ms: float, high_ms: float, seed: int) -> list[float]:
    rng = random.Random(seed)  # noqa: S311 - a seeded jitter for a test, no secret
    times, t = [], 0.0
    while t < 5.0:
        t += rng.uniform(low_ms, high_ms) / 1000
        times.append(t)
    return times


def test_a_page_drawing_near_the_take_s_rate_with_jitter_is_copied_closer_to_it():
    # Frames 10 to 50 ms apart (about 33 fps): fixed slots lose those that share one.
    times = _drawn(10, 50, 10959)
    seconds = times[-1] - times[0]
    slots = _copied(times, 30, budget=None) / seconds
    paced = _copied(times, 30, budget=canvas_alone.COPY_BUDGET) / seconds
    assert slots < 27 and paced >= slots + 2 and paced <= 30.5


def test_the_budget_never_copies_above_the_take_s_rate_on_average():
    times = [k / 60 for k in range(600)]
    paced = _copied(times, 30, budget=canvas_alone.COPY_BUDGET)
    assert paced <= 30 * (times[-1] - times[0]) + canvas_alone.COPY_BUDGET
