"""A canvas wheel zoom on a GPU worker captured alone, a discrete cut said as such (issue #10897).

A wheel over a canvas zoomed it at 10 to 16 fps on the screencast, whatever the window, and was always drawn again from
keyframes: its copies alone were held during the scroll and left out of every run. Here: a wheel over a canvas keeps
its copies (``canvas_alone.alone_runs``, the recorder's scroll no longer holding them), a change with one picture
before and one after (an icon swapped) is a discrete cut neither synthesised nor refused, and the refusal names a smaller window only where the page's own paint stepped.
No browser, no database."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from app.services.video_studio import canvas_alone, capture, capture_rate, scroll_pacing, transition_pacing

SURFACE = "100,50,500,300"
CANVAS = (100.0, 50.0, 500.0, 300.0)


def _copies(times: list[float]) -> list[canvas_alone.AloneFrame]:
    return [
        canvas_alone.AloneFrame(round(t, 4), SURFACE, "canvas#graph", CANVAS, (), f"c{k:06d}.jpg")
        for k, t in enumerate(times)
    ]


def _wheel_events(surface: dict | None) -> list[dict]:
    scroll = {"t": [0.9, 2.6], "samples": [], **({"surface": surface} if surface is not None else {})}
    return [{"step": 1, "action": "scroll", "t0": 0.9, "t1": 2.6, "scrolls": [scroll]}]


COPIED = [1.0 + k / 30 for k in range(46)]


def test_a_wheel_over_a_canvas_keeps_its_copies_as_a_run():
    surface = {"kind": "canvas", "context": "webgl", "box": list(CANVAS), "over": [], "at": [300.0, 200.0], "ticks": []}
    [run] = canvas_alone.alone_runs(_copies(COPIED), _wheel_events(surface))
    assert len(run.copies) == 46


def test_a_scroll_of_the_window_still_leaves_its_copies_out():
    assert canvas_alone.alone_runs(_copies(COPIED), _wheel_events(None)) == []
    # A container scrolled (a surface that is not a canvas) leaves them out too.
    assert canvas_alone.alone_runs(_copies(COPIED), _wheel_events({"kind": "element"})) == []


def test_the_recorder_keeps_the_copies_only_for_a_canvas_wheel_on_the_gpu():
    source = Path(capture.__file__).read_text(encoding="utf-8")
    assert "recorder.gpu = bool(gl.gpu)" in source
    source = source[source.index("    async def _scroll(self, step: TakeStep)") :]
    # On software GL a canvas's copies are no faster than its paints: the wheel holds them, as a window scroll does.
    assert "canvas_alone.held(self.page) if surface is None or not self.gpu else contextlib.nullcontext()" in source


def _picture(level: int) -> np.ndarray:
    grey = np.full((60, 120), 30.0, dtype=np.float32)
    grey[20:40, 20 + level : 50 + level] = 220.0
    return grey


def test_an_icon_swap_with_nothing_between_is_a_discrete_cut():
    before, after = _picture(0), _picture(40)
    assert transition_pacing.discrete_cut([before, after])
    assert transition_pacing.discrete_cut([before, before, after, after])


def test_a_state_between_is_a_motion_not_a_cut():
    assert not transition_pacing.discrete_cut([_picture(0), _picture(20), _picture(40)])
    # The same picture before and after changed nothing.
    assert not transition_pacing.discrete_cut([_picture(0), _picture(0)])


def _cut_event(step: int) -> dict:
    entry = {
        "t": [5.0, 5.4],
        "paced": True,
        "painted": 10.0,
        "fps": 30,
        "held": 6,
        "held_not_blended": True,
        "discrete": True,
    }
    return {"step": step, "action": "click", "motion": [entry]}


def test_a_discrete_cut_is_neither_synthesised_nor_counted():
    event = _cut_event(3)
    assert scroll_pacing.synthesised_transitions([event], fps=30) == []
    assert not scroll_pacing.counts(event["motion"][0])
    assert "discrete, cut: one picture before, one after" in scroll_pacing.scroll_steps_words([event])
    capture_gpu = {"gl": {"gpu": True}, "fps": 30, "stepping": []}
    assert capture_rate.refusal(capture_gpu, "refuse", [event]) == ""


def _zoom(painted: float) -> dict:
    entry = {"t": [10.0, 11.6], "paced": True, "painted": painted, "fps": 30, "scroller": "canvas"}
    return {"step": 6, "action": "scroll", "scrolls": [entry]}


def test_a_refused_gpu_canvas_zoom_names_no_smaller_window():
    said = capture_rate.refusal({"gl": {"gpu": True}, "fps": 30, "stepping": []}, "refuse", [_zoom(14.0)])
    assert said.startswith("Refused after recording") and "smaller window" not in said
    assert "whatever the window" in said


def test_a_refused_page_transition_on_a_gpu_names_no_smaller_window_either():
    # #10950: a GPU lane's refusal never advises a smaller window, whatever stepped.
    click = {"step": 2, "action": "click", "motion": [{"t": [3.0, 3.4], "paced": True, "painted": 8.0, "fps": 30}]}
    said = capture_rate.refusal({"gl": {"gpu": True}, "fps": 30, "stepping": []}, "refuse", [click])
    assert said.startswith("Refused after recording") and "smaller window" not in said
