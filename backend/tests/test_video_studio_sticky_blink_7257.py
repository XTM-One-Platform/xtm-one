"""A sticky header on a paced scroll never blinks, and a wait step's settling page is never stepping motion.

* a band the keyframes about a pair pin, which one of the pair missed (a hover, a repaint) and which never rode the
  content between them, is held as painted on every frame drawn between them, never translated with the content;
* the self-check names the frames where a held band moved and came back within a few frames (a blink), and says
  nothing of a band that changed and stayed changed;
* a stretch of the whole page changing on a ``wait`` step (charts drawing, skeletons, counters) is kept ``settling``:
  graded advisory whatever its rate, never dense, so a render never waits for it; the same stretch on a click stays
  stepping motion.
"""

from __future__ import annotations

import numpy as np

from app.services.video_studio import capture_rate, scroll_pacing


def _bands(count: int, rows: int, run: tuple[int, int], missed: set[int]) -> list[np.ndarray]:
    found = []
    for j in range(count):
        mask = np.zeros(rows, dtype=bool)
        if j not in missed:
            mask[run[0] : run[1]] = True
        found.append(mask)
    return found


def test_a_band_one_keyframe_of_the_pair_missed_is_held_from_the_one_pinning_it():
    # Zoom 112% over a scroll container: the header's run (rows 40-120) pinned on keyframes 0, 1, 3, 4, missed on 2.
    bands = _bands(5, 600, (40, 120), missed={2})
    offsets = [0.0, 60.0, 120.0, 180.0, 240.0]
    excluded = {j: bands[j].copy() for j in range(5)}
    for k in (1, 2):
        held = scroll_pacing._held_bands(bands, {}, k, offsets[k] + 30.0, offsets, excluded)
        # Drawn from keyframe 1, the nearest before that pinned it: the header in place on every frame between.
        assert held == [(40, 120, 1)], held


def test_a_band_both_keyframes_pin_is_held_once():
    bands = _bands(4, 300, (0, 30), missed=set())
    held = scroll_pacing._held_bands(bands, {}, 1, 50.0, [0.0, 40.0, 80.0, 120.0], {j: bands[j].copy() for j in range(4)})
    assert held == [(0, 30, 1)]


def test_a_band_no_keyframe_after_the_pair_pins_is_not_held():
    # Let go for good (the header scrolled away with its section): nothing pins it after k, it rides the content.
    bands = _bands(4, 300, (0, 30), missed={2, 3})
    held = scroll_pacing._held_bands(bands, {}, 1, 50.0, [0.0, 40.0, 80.0, 120.0], {j: bands[j].copy() for j in range(4)})
    assert held == []


def test_the_self_check_names_a_band_that_moved_and_came_back():
    rng = np.random.default_rng(7)
    header = (rng.random((20, 100)) * 200).astype(np.float32)
    pushed = np.roll(header, -8, axis=0)
    strips = [header, header, pushed, header, header]
    times = [1.0, 1.033, 1.067, 1.1, 1.133]
    assert scroll_pacing.band_blinks(strips, times) == [1.067]
    # Changed and stayed changed (a hover lit it): no blink.
    assert scroll_pacing.band_blinks([header, header, pushed, pushed, pushed, pushed, pushed], [*times, 1.167, 1.2]) == []
    assert scroll_pacing.band_blinks([header] * 5, times) == []


def test_the_answer_says_a_band_blink_with_its_frames():
    entry = {"paced": True, "fps": 30.0, "pinned": [[0, 64]], "band_blink": [24.2, 24.233]}
    said = scroll_pacing._how(entry, 12.0)
    assert "band blink" in said and "24.20 s" in said and "2 frame(s)" in said


def _stretch(**extra) -> dict:
    # 2.0 s at 30 fps, 50 of 61 frames synthesised: dense as motion.
    return {"t": [2.4, 4.4], "frames": 61, "synthesised": 50, "fps": 30, "step": 3, **extra}


def test_a_settling_stretch_is_advisory_whatever_its_rate():
    assert capture_rate.stepping_grade(_stretch()).severity == "dense"
    grade = capture_rate.stepping_grade(_stretch(settling=True))
    assert grade.severity == "advisory" and grade.settling


def test_measure_keeps_a_wait_step_s_changing_page_as_settling(tmp_path, monkeypatch):
    found = capture_rate.Stepping(2.4, 4.4, 61, 50, 30, 3)
    monkeypatch.setattr(capture_rate, "stepping", lambda *a, **k: [found])
    monkeypatch.setattr(capture_rate, "capture_fault", lambda *a, **k: None)
    monkeypatch.setattr(capture_rate, "canvas_motion", lambda *a, **k: [])
    monkeypatch.setattr(capture_rate, "low_spans", lambda *a, **k: [])
    monkeypatch.setattr(capture_rate, "sparse_spans", lambda *a, **k: [])
    frames = [(round(0.1 * i, 3), f"f{i:04d}.jpg") for i in range(60)]
    for action, settling in (("wait", True), ("click", False)):
        events = [{"step": 3, "action": action, "t0": 2.0, "t1": 5.0}]
        kept = capture_rate.measure(frames, events, duration=6.0, fps=30, workdir=str(tmp_path))
        [stretch] = kept["stepping"]
        assert bool(stretch.get("settling")) is settling
        # A settling page never marks its step synthesised: that is what refuses a render.
        assert ("synthesised" in events[0]) is not settling
