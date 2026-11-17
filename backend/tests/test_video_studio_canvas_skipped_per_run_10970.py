"""Skipped canvas copies are said on the transition or the step they fell in, not only take-wide (#10970).

A take said "N copies skipped, every encoder behind" as one total: a retest bar such as "no skipped-copies tail on
any wheel burst" could not be judged. The tap now hands over each skip's instant and canvas; the take keeps them per
run (``skipped``, ``skipped_windows``) and per step outside every run (``skipped_outside_runs``).
"""

from app.services.video_studio import canvas_alone, take_window

BOX = "10,20,800,600"
OTHER = "900,20,300,300"


def _copies(start: float, end: float, surface: str = BOX, fps: float = 30.0) -> list[canvas_alone.AloneFrame]:
    out, t, n = [], start, 0
    while t <= end + 1e-9:
        out.append(
            canvas_alone.AloneFrame(round(t, 4), surface, "canvas#graph", (10, 20, 800, 600), (), f"c{n}.jpg", n)
        )
        t += 1 / fps
        n += 1
    return out


EVENTS = [
    {"step": 1, "action": "goto", "ta": 0.0, "t0": 0.0, "t1": 1.0},
    {"step": 2, "action": "wheel", "ta": 1.0, "t0": 1.0, "t1": 3.0},
    {"step": 3, "action": "wait", "ta": 3.0, "t0": 3.0, "t1": 6.0},
]


def test_skips_inside_and_outside_runs_sum_to_the_total():
    runs = canvas_alone.alone_runs(_copies(1.2, 2.2), EVENTS)
    assert len(runs) == 1
    skips = [
        (1.5, BOX),  # inside the wheel burst
        (1.55, BOX),
        (2.4, BOX),  # the burst's tail, past its last copy within RUN_GAP: the burst's
        (1.6, OTHER),  # another canvas at the same time: no run of its own
        (0.5, BOX),  # the load, step 1
        (4.0, BOX),  # idle, step 3
        (4.1, BOX),
    ]
    inside, outside = canvas_alone.skips_by_run(runs, skips)
    assert inside == {0: [1.5, 1.55, 2.4]}
    assert outside == [0.5, 1.6, 4.0, 4.1]
    per_step = canvas_alone.skipped_outside(outside, EVENTS)
    assert per_step == [
        {"step": 1, "skipped": 1, "t": [0.5, 0.5]},
        {"step": 2, "skipped": 1, "t": [1.6, 1.6]},
        {"step": 3, "skipped": 2, "t": [4.0, 4.1]},
    ]
    assert sum(len(v) for v in inside.values()) + sum(e["skipped"] for e in per_step) == len(skips)
    assert canvas_alone._skip_windows(inside[0]) == [[1.5, 1.55], [2.4, 2.4]]


def test_composite_without_painted_frames_says_every_skip_outside(tmp_path):
    # No painted frame: composite answers before reading pixels, its skips all outside.
    found = canvas_alone.composite(
        [], _copies(1.2, 2.2), EVENTS, fps=30, pixel_ratio=1.0, workdir=str(tmp_path), skips=[(4.0, BOX)]
    )
    assert found.skipped_outside == [{"step": 3, "skipped": 1, "t": [4.0, 4.0]}]
    assert canvas_alone.composite([], [], EVENTS, fps=30, pixel_ratio=1.0, workdir=str(tmp_path)).skipped_outside == []


def test_the_notes_and_the_capture_line_say_where_they_fell():
    run = {
        "canvas": "canvas#graph",
        "t": [1.2, 2.2],
        "step": 2,
        "painted": 12,
        "fps": 29,
        "drawn": 31,
        "composited": True,
    }
    said = canvas_alone.notes([{**run, "skipped": 4, "skipped_windows": [[1.5, 1.6]]}], "GPU GL")[0]
    assert "copied at 29 fps of 31 drawn, 4 copies skipped (every encoder behind))" in said
    # Zero skipped: nothing extra, the run's record saying 0 and [] all the same.
    assert "skipped" not in canvas_alone.notes([run], "GPU GL")[0]
    assert "skipped" not in canvas_alone.notes([{**run, "skipped": 0, "skipped_windows": []}], "GPU GL")[0]
    # One copy: singular (retest c3a8b4a6 read "1 copies skipped").
    one = canvas_alone.notes([{**run, "skipped": 1, "skipped_windows": [[1.5, 1.5]]}], "GPU GL")[0]
    assert "copied at 29 fps of 31 drawn, 1 copy skipped (every encoder behind))" in one
    assert canvas_alone.copies_skipped(1) == "1 copy skipped" and canvas_alone.copies_skipped(4) == "4 copies skipped"
    alone = {
        "busy": 7,
        "runs": [{**run, "skipped": 3}],
        "skipped_outside_runs": [
            {"step": 1, "skipped": 1, "t": [0.5, 0.5]},
            {"step": 3, "skipped": 2, "t": [4.0, 4.1]},
        ],
    }
    line = canvas_alone.capture_words({"alone": alone})
    assert (
        "; 7 copies skipped, every encoder behind (3 in transitions, 3 outside (step 1: 1, step 3: 2), 1 not placed)"
        in line
    )
    # A take that kept no placement (recorded before, or failed): the total alone, as before.
    older = canvas_alone.capture_words({"alone": {"busy": 7, "runs": [run]}})
    assert older.endswith("; 7 copies skipped, every encoder behind")
    assert "skipped" not in canvas_alone.capture_words({"alone": {"runs": [run]}})


async def test_the_tap_keeps_each_skip_on_the_take_clock(tmp_path):
    class _Page:
        def __init__(self):
            self.calls = 0

        async def evaluate(self, *_args):
            self.calls += 1
            if self.calls == 1:
                return {"frames": [], "busy": 2, "skips": [[101500.0, BOX], [101550.0, BOX], ["bad", BOX]]}
            return {"frames": [], "busy": 1, "skips": [[104000.0, OTHER]]}

    tap = canvas_alone.Tap(_Page(), str(tmp_path), origin=100.0, max_copies=10)
    await tap.drain()
    kept = await tap.stop()
    # A navigation restarted the page's count (2 then 1): the instants handed over count all three.
    assert kept["busy"] == 3
    assert kept["skips"] == [[1.5, BOX], [1.55, BOX], [4.0, OTHER]]
    assert canvas_alone.kept_skips(kept) == [(1.5, BOX), (1.55, BOX), (4.0, OTHER)]
    assert canvas_alone.kept_skips(None) == []


def test_the_page_script_hands_the_skips_over():
    script = canvas_alone.tap_script(30, "(el) => el.id", encoders=3)
    assert "tap.skips.push([t, key]);" in script
    assert "cost: tap.cost, skips };" in script


def test_a_window_answers_the_skips_outside_runs_where_they_fell():
    capture = {"alone": {"busy": 2, "runs": [], "skipped_outside_runs": [{"step": 3, "skipped": 2, "t": [4.0, 4.1]}]}}
    assert take_window.window_records(capture, [], 3.5, 5.0)["skipped_outside_runs"] == [
        {"step": 3, "skipped": 2, "t": [4.0, 4.1]}
    ]
    assert take_window.window_records(capture, [], 0.0, 1.0)["skipped_outside_runs"] == []
    # The window's own count; the take-wide one under its own name whatever the window.
    assert take_window.window_records(capture, [], 3.5, 5.0)["skipped_total"] == 2
    assert take_window.window_records(capture, [], 0.0, 1.0)["skipped_total"] == 0
    assert take_window.window_records(capture, [], 0.0, 1.0)["skipped_total_take"] == 2


def _run(t0: float, t1: float, skipped: int) -> dict:
    windows = [[t0, t0]] if skipped else []
    return {
        "canvas": "canvas#graph",
        "t": [t0, t1],
        "step": 2,
        "composited": True,
        "skipped": skipped,
        "skipped_windows": windows,
    }


def _reconciles(alone: dict) -> None:
    """Per-run ``skipped`` plus ``skipped_outside_runs`` over the whole take make the take-wide total."""
    records = take_window.window_records({"alone": alone}, [], 0.0, 60.0)
    inside = sum(run["skipped"] for run in records["alone_runs"])
    outside = sum(entry["skipped"] for entry in records["skipped_outside_runs"])
    assert inside + outside == records["skipped_total"]


def test_a_zero_skip_take_says_both_keys_in_every_window():
    # Retest 6fde5a86 (take 809edd99): no copy skipped, skipped_outside_runs absent and no total to reconcile.
    alone = {"runs": [_run(0.9, 3.1, 0), _run(10.0, 12.0, 0)], "skipped_outside_runs": []}
    for start, end in ((0.0, 5.0), (5.0, 15.0), (15.0, 60.0), (0.0, 60.0)):
        records = take_window.window_records({"alone": alone}, [], start, end)
        assert records["skipped_outside_runs"] == []
        assert records["skipped_total"] == 0
    _reconciles(alone)
    # An older take that kept no placement, or none captured alone: the keys all the same.
    for capture in ({"alone": {"runs": []}}, {}, None):
        records = take_window.window_records(capture, [], 0.0, 60.0)
        assert records["skipped_outside_runs"] == [] and records["skipped_total"] == 0
        assert records["skipped_total_take"] == 0
    # The capture line stays silent for zero.
    assert "skipped" not in canvas_alone.capture_words({"alone": alone})


def test_a_one_skip_take_says_it_singular_and_reconciles():
    alone = {"busy": 1, "runs": [_run(1.0, 2.0, 1), _run(10.0, 12.0, 0)], "skipped_outside_runs": []}
    _reconciles(alone)
    assert take_window.window_records({"alone": alone}, [], 0.0, 60.0)["skipped_total"] == 1
    line = canvas_alone.capture_words({"alone": alone})
    assert "; 1 copy skipped, every encoder behind (1 in transitions, 0 outside)" in line


def test_a_k_skip_take_reconciles_inside_and_outside():
    alone = {
        "busy": 7,
        "runs": [_run(1.0, 2.0, 3), _run(10.0, 12.0, 1)],
        "skipped_outside_runs": [
            {"step": 1, "skipped": 1, "t": [0.5, 0.5]},
            {"step": 3, "skipped": 2, "t": [4.0, 4.1]},
        ],
    }
    _reconciles(alone)
    assert take_window.window_records({"alone": alone}, [], 0.0, 60.0)["skipped_total"] == 7
    line = canvas_alone.capture_words({"alone": alone})
    assert "; 7 copies skipped, every encoder behind (4 in transitions, 3 outside (step 1: 1, step 3: 2))" in line


def _adds_up(records: dict) -> int:
    inside = sum(run["skipped"] for run in records["alone_runs"])
    outside = sum(entry["skipped"] for entry in records["skipped_outside_runs"])
    assert inside + outside == records["skipped_total"]
    return records["skipped_total"]


def test_every_window_of_a_multi_run_take_adds_up_on_its_own():
    # Retest 5cfe9d18 (take c69f04f4): skipped_total said the take-wide 41 in every window, the runs there adding to
    # 0, 13, 28 and 13. Each window now counts what it lists; the take-wide figure is skipped_total_take.
    alone = {
        "busy": 44,
        "runs": [
            _run(0.5, 2.0, 0),
            _run(6.0, 9.0, 13),
            _run(20.0, 24.0, 15),
            _run(40.0, 44.0, 11),
            _run(70.0, 72.0, 2),
        ],
        "skipped_outside_runs": [{"step": 9, "skipped": 3, "t": [30.0, 30.2]}],
    }
    expected = {(0.0, 5.0): 0, (5.0, 15.0): 13, (15.0, 60.0): 29, (60.0, 83.2): 2, (0.0, 83.2): 44}
    for (start, end), total in expected.items():
        records = take_window.window_records({"alone": alone}, [], start, end)
        assert _adds_up(records) == total, (start, end)
        assert records["skipped_total_take"] == 44
        # Every run listed still carries its own count and instants.
        assert all("skipped" in run and "skipped_windows" in run for run in records["alone_runs"])


def test_a_window_cut_by_the_record_cap_still_counts_every_skip():
    runs = [_run(float(i), float(i) + 0.5, 1) for i in range(take_window.MOST_RECORDS + 5)]
    records = take_window.window_records({"alone": {"busy": len(runs), "runs": runs}}, [], 0.0, 1000.0)
    assert records["cut"] == {"alone_runs": 5}
    assert records["skipped_total"] == take_window.MOST_RECORDS + 5
