"""#10970: the capture line says why a canvas drawn was not copied.

Retest of #11296 (take 51415c30, L4): a wheel zoom drawn at 34.7 fps was copied at 25.2 fps with no copy skipped as
busy and 480 drawn, 470 encoded take-wide, so neither the queue nor the encoders' 'same' answers held the rate back.
The tap now counts the frames drawn while the canvas's budget was spent (over the take's rate) or while it was still,
and the copies the encoders found alike, and the capture line says them.
"""

from app.services.video_studio import canvas_alone


def test_the_tap_counts_why_a_frame_drawn_was_not_copied() -> None:
    script = canvas_alone.tap_script(30, "(el) => el.id")
    assert "spent: 0, idled: 0, alike: 0," in script
    assert "if (fresh && spent) tap.cost.spent++;" in script
    assert "else if (fresh && idle) tap.cost.idled++;" in script
    assert "if (data === 'same') { tap.cost.alike++;" in script
    assert {"spent", "idled", "alike"} <= set(canvas_alone.COST_KEYS)
    assert "__" not in script.replace("__xtm", "")


def test_the_capture_line_says_the_frames_not_copied() -> None:
    cost = {"page": 4.0, "copied": 2, "spent": 24, "idled": 3, "alike": 10}
    assert canvas_alone.cost_words(cost) == (
        "copies cost the page 2.0 ms (2 drawn, 0 encoded; not copied or kept: 24 frames over the rate, 3 frames still,"
        " 10 alike)"
    )


def test_a_gpu_canvas_budget_refills_above_the_take_rate() -> None:
    # Take 51415c30: a wheel zoom drawn at 34.7 fps was copied at 25.2 with the refill at exactly 30 a second. On a
    # GPU's encoders the refill has headroom; software GL (one encoder) keeps the take's rate.
    assert canvas_alone.copy_rate(30, 3) == 30 * canvas_alone.COPY_HEADROOM > 30
    assert canvas_alone.copy_rate(30, 1) == 30.0
    assert f"const RATE = {canvas_alone.copy_rate(30, 1)!r};" in canvas_alone.tap_script(30, "(el) => el.id")
    assert f"const RATE = {canvas_alone.copy_rate(30, 3)!r};" in canvas_alone.tap_script(
        30, "(el) => el.id", encoders=3
    )


def test_a_take_without_frames_not_copied_says_nothing_of_them() -> None:
    cost = {"page": 4.0, "copied": 2, "spent": 0, "idled": 0, "alike": 0}
    assert canvas_alone.cost_words(cost) == "copies cost the page 2.0 ms (2 drawn, 0 encoded)"
