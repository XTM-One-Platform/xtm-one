"""Recorder fidelity gate (issue #10867): a ``frame.fill`` recording drawn edge to edge, never fitted above the
captions' band, and a transition drawn from keyframes (over 30% synthesised, or held then cut) a blocking finding.
No browser, no database."""

from __future__ import annotations

from pathlib import Path

from app.services.video_studio import capture_rate, compositor_page, scroll_pacing


def _event(step: int, *, painted: float, held: int = 0, discrete: bool = False, canvas: bool = False) -> dict:
    entry = {"t": [10.0 + step, 10.6 + step], "paced": True, "painted": painted, "fps": 60}
    if canvas:
        entry["scroller"] = "canvas"
    if discrete:
        entry.update({"held": held, "held_not_blended": True})
    return {"step": step, "action": "wheel" if canvas else "click", ("scrolls" if canvas else "motion"): [entry]}


def test_a_wheel_zoom_painted_at_12_fps_on_a_60_fps_take_is_synthesised():
    [found] = scroll_pacing.synthesised_transitions([_event(8, painted=12.0, canvas=True)], fps=60)
    assert found["step"] == 8 and found["share"] == 0.8 and found["held"] == 0
    assert "80% of its frames synthesised" in scroll_pacing.synthesised_words([found])


def test_a_layout_click_held_then_cut_is_said_whatever_its_rate():
    [found] = scroll_pacing.synthesised_transitions([_event(16, painted=58.0, held=9, discrete=True)], fps=60)
    assert found["held"] == 9
    assert "9 held then cut" in scroll_pacing.synthesised_words([found])


def test_a_transition_captured_at_the_take_rate_is_not_said():
    assert scroll_pacing.synthesised_transitions([_event(3, painted=50.0, canvas=True)], fps=60) == []
    # 30% synthesised is the limit, not past it.
    assert scroll_pacing.synthesised_transitions([_event(3, painted=42.0, canvas=True)], fps=60) == []


def test_an_unpaced_transition_and_a_take_without_rate_are_not_said():
    event = _event(4, painted=5.0, canvas=True)
    event["scrolls"][0]["paced"] = False
    assert scroll_pacing.synthesised_transitions([event], fps=60) == []
    assert scroll_pacing.synthesised_transitions([_event(4, painted=5.0)], fps=0) == []


def test_a_brief_canvas_zoom_mostly_synthesised_on_a_gpu_take_is_refused():
    # 0.6 s at 60 fps, 36 frames, 28 synthesised: painted at 13 fps, a brief transition graded advisory.
    zoom = {"t": [11.0, 11.6], "frames": 36, "synthesised": 28, "fps": 60, "step": 8, "canvas": "canvas#graph"}
    assert capture_rate.stepping_grade(zoom).severity == "advisory"
    said = capture_rate.refusal({"gl": {"gpu": True}, "fps": 60, "stepping": [zoom]}, "refuse")
    assert "canvas#graph at step 8 painted at" in said and "28 of its 36 frames at 60 fps synthesised" in said
    # The same brief stretch of the page outside a canvas keeps its advisory grade.
    page = {k: v for k, v in zoom.items() if k != "canvas"}
    assert capture_rate.refusal({"gl": {"gpu": True}, "fps": 60, "stepping": [page]}, "refuse") == ""


def test_a_fill_recording_is_drawn_as_cover_before_the_window_branch():
    source = Path(compositor_page.__file__).read_text(encoding="utf-8")
    start = source.index("function buildRecording(layer, sc)")
    body = source[start : source.index("Object.assign(win.style", start)]
    fill, window = body.index("if (frame.fill)"), body.index("frame.style === 'window') {")
    assert fill < window
    branch = body[fill:window]
    # Cover: the page scaled to the larger ratio and centred, never inside the captions' reserve.
    assert "Math.max(W / sc.css_w, H / sc.css_h)" in branch and "room" not in branch
