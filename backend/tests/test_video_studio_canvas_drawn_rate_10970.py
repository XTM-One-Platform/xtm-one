"""A canvas run says the rate its page drew it at beside the rate it was copied at (#10970).

The retest of #10962 (take 185d7597, GPU GL on an L4) copied four wheel zooms at 22 to 24 fps while the screencast
painted them at 34 to 42: whether the copy loop fell short or the page drew its canvas no faster could not be told.
The page script now counts the frames it drew each canvas in and every copy carries the count.
"""

import random

from app.services.video_studio import canvas_alone

NAME_JS = "(el) => el.id"


def _copy(t: float, drawn: int | None) -> canvas_alone.AloneFrame:
    return canvas_alone.AloneFrame(t, "0,0,800,600", "canvas#graph", (0.0, 0.0, 800.0, 600.0), (), f"c{t}.jpg", drawn)


def test_the_page_script_counts_the_frames_a_canvas_was_drawn_in_and_sends_it_with_each_copy():
    script = canvas_alone.tap_script(30, NAME_JS, encoders=3)
    assert "if (fresh) st.frames++;" in script
    assert "clip.map(round), st.over, st.frames]);" in script


def test_a_run_says_the_rate_its_canvas_was_drawn_at():
    copies = [_copy(k / 22, 2 * k) for k in range(23)]
    run = canvas_alone.AloneRun(tuple(copies))
    assert round(run.fps) == 22
    assert round(run.drawn_fps or 0) == 44


def test_a_run_kept_before_the_count_says_no_drawn_rate():
    run = canvas_alone.AloneRun(tuple(_copy(k / 30, None) for k in range(10)))
    assert run.drawn_fps is None


def test_the_notes_say_the_drawn_rate_where_it_was_counted():
    run = {"canvas": "canvas#graph", "t": [1.0, 2.0], "step": 3, "painted": 34, "fps": 22, "drawn": 23.2}
    said = canvas_alone.notes([{**run, "composited": True}], gl="GPU GL")
    assert said == [
        "Captured alone where the screencast fell short (GPU GL): canvas#graph at step 3 (1.0 s, painted at 34 fps, copied at 22 fps of 23 drawn)."
    ]
    old = {k: v for k, v in run.items() if k != "drawn"}
    assert "drawn" not in canvas_alone.notes([{**old, "composited": True}], gl="GPU GL")[0]


class _Page:
    def __init__(self, answers):
        self.answers = list(answers)

    async def evaluate(self, _script, *_args):
        return self.answers.pop(0) if self.answers else None


async def test_a_drain_keeps_the_count_and_reads_a_tap_that_predates_it(tmp_path):
    data = "AAAA"
    frames = [
        [1000.0, "0,0,800,600", "canvas#graph", [0, 0, 800, 600], [], 7, data],
        [1033.0, "0,0,800,600", "canvas#graph", [0, 0, 800, 600], [], data],
    ]
    tap = canvas_alone.Tap(_Page([{"frames": frames}]), str(tmp_path), origin=0.0, max_copies=10)
    await tap.drain()
    assert [copy.drawn for copy in tap.frames] == [7, None]


def test_the_copy_budget_copies_a_page_drawing_at_40_fps_at_28_or_more():
    # The page script's budget, step for step, under a 40 fps paint with jitter: at least 28 copies a second.
    rng = random.Random(10970)  # noqa: S311 - a seeded jitter for a test, no secret
    times, t = [], 0.0
    while t < 5.0:
        t += rng.uniform(0.5, 1.5) / 40
        times.append(t)
    held, filled, copied = 1.0, times[0], 0
    for at in times:
        held, filled = min(canvas_alone.COPY_BUDGET, held + (at - filled) * 30), at
        if held >= 1:
            held, copied = held - 1, copied + 1
    assert copied / (times[-1] - times[0]) >= 28
