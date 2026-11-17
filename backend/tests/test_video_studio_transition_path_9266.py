"""#9266: a GPU recording lane records one take at a time, and each canvas transition says which path captured it."""

from __future__ import annotations

from app.core.config import settings
from app.services.video_studio import capture_rate, jobs
from app.services.video_studio.capture_rate import NO_COPY, CanvasRun, LowSpan, canvas_words, low_words, screencast_why


def _run(alone: bool = False) -> CanvasRun:
    return CanvasRun("canvas.clickable", 10.0, 11.2, 22, 7, None, alone)


def test_take_concurrency_is_one_on_a_gpu_lane(monkeypatch):
    monkeypatch.setattr(settings, "video_studio_take_concurrency", 2)
    monkeypatch.setattr(settings, "video_studio_recording_gl", "gpu")
    assert capture_rate.take_concurrency() == 1
    assert jobs.slots(jobs.TAKE_ADAPTER) == 1


def test_take_concurrency_keeps_the_setting_elsewhere(monkeypatch):
    monkeypatch.setattr(settings, "video_studio_take_concurrency", 2)
    monkeypatch.setattr(settings, "video_studio_recording_gl", "auto")
    assert capture_rate.take_concurrency() == 2
    assert jobs.slots(jobs.TAKE_ADAPTER) == 2


def test_a_lane_that_records_none_stays_at_zero(monkeypatch):
    monkeypatch.setattr(settings, "video_studio_take_concurrency", 0)
    monkeypatch.setattr(settings, "video_studio_recording_gl", "gpu")
    assert capture_rate.take_concurrency() == 0


def test_screencast_why_names_the_overlapping_run():
    recorded = {"alone": {"runs": [{"t": [10.1, 11.0], "why": "copied no faster than painted", "composited": False}]}}
    assert screencast_why(_run(), recorded) == "copied no faster than painted"


def test_screencast_why_says_no_copy_when_no_run_covers_it():
    recorded = {"alone": {"runs": [{"t": [20.0, 21.0], "why": "painted at its rate"}]}}
    assert screencast_why(_run(), recorded) == NO_COPY


def test_screencast_why_is_silent_without_copies_or_when_captured_alone():
    assert screencast_why(_run(), None) is None
    assert screencast_why(_run(), {"alone": {"off": "worker-src refused"}}) is None
    assert screencast_why(_run(alone=True), {"alone": {"runs": []}}) is None


def test_canvas_words_say_the_path_and_rate():
    from dataclasses import replace

    left = replace(_run(), screencast="copied no faster than painted").as_dict()
    said = canvas_words([left, _run(alone=True).as_dict()])
    assert "at 17.5 fps, on the screencast (copied no faster than painted)" in said
    assert "captured alone" in said


def test_low_capture_rate_says_why_it_stayed_on_the_screencast():
    from dataclasses import replace

    low = replace(_run(), screencast=NO_COPY).low()
    assert low.alone == NO_COPY
    said = low_words({"low": [low.as_dict()]})
    assert said == [
        f"low capture rate at step 7 (canvas.clickable moved at 18 fps for 1.2 s; on the screencast, not captured alone: {NO_COPY})"
    ]


def test_low_span_words_unchanged_without_a_reason():
    assert LowSpan(1.0, 2.0, 12.0, 4).words() == "low capture rate at step 4 (12 fps for 1.0 s)"
