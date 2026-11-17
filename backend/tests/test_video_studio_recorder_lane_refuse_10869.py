"""Recording lane (#10869 retest): on GPU GL under ``fps_fallback: refuse``, a take whose paced transitions were
over 30% drawn from keyframes, or held then cut, is refused - never kept ready while the save check finds it
``synthesised_transition``. No browser, no database."""

from __future__ import annotations

from pathlib import Path

from app.services.video_studio import capture_rate, runners


def _event(step: int, *, painted: float, held: int = 0, canvas: bool = True) -> dict:
    entry = {"t": [6.2 + step, 6.8 + step], "paced": True, "painted": painted, "fps": 60}
    if canvas:
        entry["scroller"] = "canvas"
    if held:
        entry.update({"held": held, "held_not_blended": True})
    return {"step": step, "action": "wheel" if canvas else "click", ("scrolls" if canvas else "motion"): [entry]}


def _gpu(fps: int = 60) -> dict:
    # No stepping stretch kept: what the probe take 644edfe3 showed, its transitions said only by the pacing pass.
    return {"gl": {"gpu": True}, "fps": fps, "stepping": []}


def test_a_gpu_take_whose_wheel_zoom_painted_at_15_fps_is_refused():
    said = capture_rate.refusal(_gpu(), "refuse", [_event(8, painted=15.2)])
    assert said.startswith("Refused after recording: this GPU worker did not capture the take's motion at 60 fps")
    assert "step 8 at" in said and "painted at 15.2 fps" in said and "75% of its frames synthesised" in said
    assert "fps_fallback: fps" in said


def test_a_layout_change_held_then_cut_is_refused_whatever_its_rate():
    said = capture_rate.refusal(_gpu(), "refuse", [_event(6, painted=58.0, held=5, canvas=False)])
    assert "step 6 at" in said and "5 held then cut" in said


def test_transitions_painted_at_the_take_rate_keep_the_take():
    assert capture_rate.refusal(_gpu(), "refuse", [_event(3, painted=55.0)]) == ""
    assert capture_rate.refusal(_gpu(), "refuse", None) == ""


def test_another_fallback_or_software_gl_keeps_the_take():
    starved = [_event(11, painted=6.7)]
    assert capture_rate.refusal(_gpu(), "fps", starved) == ""
    assert capture_rate.refusal({**_gpu(), "gl": {"gpu": False}}, "refuse", starved) == ""


def test_both_runners_pass_the_take_events_to_the_refusal():
    source = Path(runners.__file__).read_text(encoding="utf-8")
    assert "capture_rate.refusal(captured, played.fps_fallback, result.events)" in source
    assert 'capture_rate.refusal(kept.captured, resume.get("fps_fallback") or script.fps_fallback, events)' in source
