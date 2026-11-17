"""#8457, slice 4: what copying a canvas alone costs, kept and said on the take's capture line.

A GPU take of a graph canvas still copied at 10-14 fps with "copies skipped, every encoder behind" and "copies drawn
smaller to keep up": the page's thread, the encoders or the copies' size hold the rate back, and nothing said which.
The tap now counts the page thread's ms copying, the copies drawn and encoded, the encoders' ms and the bytes; the
take keeps the sums (``alone.cost``) and its capture line says them per copy.
"""

from __future__ import annotations

from app.services.video_studio import canvas_alone


def test_the_tap_counts_what_copying_costs():
    script = canvas_alone.tap_script(30, "(el) => 'c'", encoders=3)
    assert "page: 0, copied: 0, encoded: 0, encoding: 0, bytes: 0, signing: 0, scanning: 0, snapshotting: 0," in script
    assert "layers: 0, reused: 0, drawing: 0, frames: 0," in script
    assert "tap.cost.page += performance.now() - now;" in script
    assert "flight++; tap.cost.copied++;" in script
    assert "tap.cost.encoded++; tap.cost.encoding += Number(spent) || 0;" in script
    assert "performance.now() - began" in script
    assert "cost: tap.cost" in script
    assert "__" not in script.replace("__xtm", "")


class _Page:
    def __init__(self, answers):
        self.answers = list(answers)

    async def evaluate(self, *_args):
        return self.answers.pop(0) if self.answers else None


async def test_the_cost_is_summed_across_documents_and_kept(tmp_path):
    first = {"page": 20.0, "copied": 10, "encoded": 8, "encoding": 320.0, "bytes": 800_000}
    later = {"page": 42.0, "copied": 20, "encoded": 16, "encoding": 640.0, "bytes": 1_600_000}
    # A navigation: the next document counts from zero again.
    navigated = {"page": 4.0, "copied": 2, "encoded": 2, "encoding": 80.0, "bytes": 200_000}
    answers = [{"frames": [], "cost": cost} for cost in (first, later, navigated)]
    tap = canvas_alone.Tap(_Page(answers), str(tmp_path), origin=0.0, max_copies=10)
    for _ in range(3):
        await tap.drain()
    kept = await tap.stop()
    assert kept["cost"] == {
        "page": 46.0,
        "copied": 22.0,
        "encoded": 18.0,
        "encoding": 720.0,
        "bytes": 1_800_000.0,
        "signing": 0.0,
        "scanning": 0.0,
        "snapshotting": 0.0,
        "layers": 0.0,
        "reused": 0.0,
        "drawing": 0.0,
        "frames": 0.0,
        "gl": 0.0,
        "long": 0.0,
        "blocked": 0.0,
        "draining": 0.0,
        "spent": 0.0,
        "idled": 0.0,
        "alike": 0.0,
    }


async def test_a_take_that_copied_nothing_keeps_no_cost(tmp_path):
    tap = canvas_alone.Tap(_Page([{"frames": [], "cost": {"copied": 0}}]), str(tmp_path), origin=0.0, max_copies=10)
    await tap.drain()
    assert "cost" not in await tap.stop()


def test_the_capture_line_says_the_cost_per_copy():
    cost = {"page": 46.0, "copied": 20.0, "encoded": 16.0, "encoding": 640.0, "bytes": 16 * 200 * 1024}
    assert canvas_alone.cost_words(cost) == "copies cost the page 2.3 ms, the encoders 40 ms, 200 KB each (20 drawn, 16 encoded)"
    said = canvas_alone.capture_words({"alone": {"cost": cost, "lowered": 3, "runs": [{"composited": True}]}})
    assert said.endswith("; 3 copies drawn smaller to keep up; copies cost the page 2.3 ms, the encoders 40 ms, 200 KB each (20 drawn, 16 encoded)")


def test_no_cost_said_for_a_take_recorded_before_it_was_kept():
    assert canvas_alone.cost_words(None) == "" and canvas_alone.cost_words({"copied": 0}) == ""
    assert canvas_alone.cost_words({"page": 3.0, "copied": 2}) == "copies cost the page 1.5 ms (2 drawn, 0 encoded)"
    said = canvas_alone.capture_words({"alone": {"runs": [{"composited": True}]}})
    assert said == "canvas transitions captured alone: 1 composited"
