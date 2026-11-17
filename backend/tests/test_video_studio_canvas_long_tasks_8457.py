"""#8457, slice 16: the capture line says how long the page's thread was held by long tasks.

Take 07a5505d (a graph on a 4-CPU GPU worker): "its own frames 1.3 ms drawing" while the canvas painted at 12 to
13 fps. What else holds the page's thread (its own long tasks, the recorder's) is now counted with the browser's
long task timing and said on the capture line.
"""

from app.services.video_studio import canvas_alone


def test_the_tap_counts_the_long_tasks_of_the_page() -> None:
    script = canvas_alone.tap_script(30, "(el) => el.id")
    assert "blocked: 0, long: 0," in script
    assert "observe({ type: 'longtask' })" in script
    assert "tap.cost.blocked += entry.duration; tap.cost.long++;" in script
    assert {"blocked", "long"} <= set(canvas_alone.COST_KEYS)
    assert "__" not in script.replace("__xtm", "")


def test_the_capture_line_says_the_long_tasks() -> None:
    cost = {"page": 15.7, "copied": 1, "frames": 1, "drawing": 1.3, "blocked": 4200.0, "long": 31}
    assert canvas_alone.cost_words(cost) == (
        "copies cost the page 15.7 ms, its own frames 1.3 ms drawing, its thread held 4.2 s by 31 long tasks"
        " (1 drawn, 0 encoded)"
    )
    one = canvas_alone.cost_words({"page": 1.0, "copied": 1, "blocked": 60.0, "long": 1})
    assert "its thread held 0.1 s by 1 long task (" in one


def test_the_tap_counts_apart_the_long_tasks_that_drained_the_copies() -> None:
    # Slice 17 (take aebc40c2: held 3.3 s by 42 long tasks, 22.8 s held back by the quota): a long task within which
    # the recorder drained the copies (their base64 handed over on the page's thread) is counted as draining too.
    script = canvas_alone.tap_script(30, "(el) => el.id")
    assert "blocked: 0, long: 0, draining: 0," in script
    assert "drains.push(performance.now());" in script
    assert "tap.cost.draining += entry.duration;" in script
    assert "draining" in canvas_alone.COST_KEYS
    cost = {"page": 17.0, "copied": 1, "blocked": 3300.0, "long": 42, "draining": 1200.0}
    assert canvas_alone.cost_words(cost) == (
        "copies cost the page 17.0 ms, its thread held 3.3 s by 42 long tasks (1.2 s of them handing copies over)"
        " (1 drawn, 0 encoded)"
    )
    assert canvas_alone.cost_words({**cost, "draining": 0.0}).endswith(
        "42 long tasks (none of them handing copies over) (1 drawn, 0 encoded)"
    )


def test_a_take_recorded_before_the_drains_were_counted_says_nothing_of_them() -> None:
    # Slice 18 (takes 53aba1b3 and d6c76ac8 said no drain part): a tap that counted the drains keeps ``draining``, 0
    # included, and says it; a take recorded before keeps none and says nothing, so the line tells the two apart.
    cost = {"page": 17.0, "copied": 1, "blocked": 2200.0, "long": 26}
    assert "handing copies over" not in canvas_alone.cost_words(cost)


def test_a_take_without_long_tasks_says_nothing_of_them() -> None:
    assert "long task" not in canvas_alone.cost_words({"page": 4.0, "copied": 2, "long": 0})
