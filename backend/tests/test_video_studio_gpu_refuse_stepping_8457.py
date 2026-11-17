"""A take recorded on GPU GL under ``fps_fallback: refuse`` whose motion still steps is refused with its rate."""

from __future__ import annotations

from app.services.video_studio import capture_rate

_STEPPING = {
    "t": [10.0, 11.2],
    "frames": 37,
    "synthesised": 29,
    "share": 0.784,
    "fps": 30,
    "step": 3,
    "canvas": "canvas#graph",
}


def _capture(*stretches: dict, gpu: bool | None = True) -> dict:
    return {"gl": {"gpu": gpu}, "fps": 30, "stepping": list(stretches)}


def test_a_gpu_take_whose_canvas_steps_is_refused_with_its_painted_rate():
    said = capture_rate.refusal(_capture(_STEPPING), "refuse")
    assert said.startswith("Refused after recording: this GPU worker did not capture the take's motion at 30 fps")
    assert "canvas#graph at step 3 painted at 6.7 fps over 1.2 s" in said
    assert "29 of its 37 frames at 30 fps synthesised" in said
    assert "fps_fallback: fps" in said


def test_another_fallback_or_software_gl_keeps_the_take():
    assert capture_rate.refusal(_capture(_STEPPING), "fps") == ""
    assert capture_rate.refusal(_capture(_STEPPING), "size") == ""
    assert capture_rate.refusal(_capture(_STEPPING, gpu=False), "refuse") == ""
    assert capture_rate.refusal(_capture(_STEPPING, gpu=None), "refuse") == ""
    assert capture_rate.refusal(None, "refuse") == ""


def test_settling_and_brief_dense_enough_stretches_keep_the_take():
    settling = {**_STEPPING, "settling": True}
    assert capture_rate.refusal(_capture(settling), "refuse") == ""
    # 0.5 s painted at 16 fps, a keyframe held 0.1 s: read as motion (advisory), not refused.
    brief = {"t": [5.0, 5.5], "frames": 16, "synthesised": 8, "fps": 30, "step": 2, "hold": 0.1}
    assert capture_rate.refusal(_capture(brief), "refuse") == ""


def test_the_whole_page_stretch_is_named_by_its_step_or_its_time():
    page = {"t": [0.0, 2.0], "frames": 61, "synthesised": 48, "fps": 30, "step": None}
    said = capture_rate.refusal(_capture(page), "refuse")
    assert "the page at 0.0 s painted at 6.5 fps over 2.0 s" in said
