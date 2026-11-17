"""A span the page itself drew under the take's rate kept at its own frames under ``fps_fallback: refuse`` (#10970).

Sam's decision (option A, 11 Nov 2026): under ``refuse`` a span the page drew under 30 fps, every frame it drew kept
and held (never blended, no frame synthesised), with no CPU quota hold-back over it, is kept and said page-limited in
the take's notes; a span the worker's quota starved is still refused. No browser, no database."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.video_studio import canvas_alone, capture, capture_rate, scroll_pacing, transition_pacing

GPU = {"gl": {"gpu": True}, "fps": 30}


def _page_frames(start: float, end: float, rate: float) -> list[float]:
    n = round((end - start) * rate)
    return [round(start + k / rate, 4) for k in range(n)]


# ── The page's own rate ─────────────────────────────────────────────────────


def test_the_page_drawing_under_the_rate_with_every_frame_kept_is_page_limited():
    page = _page_frames(4.9, 5.3, 10)
    assert transition_pacing.page_limited_fps(page, 4.96, 5.17, painted=10.0, fps=30) is not None


def test_a_capture_short_of_what_the_page_drew_is_not_page_limited():
    page = _page_frames(4.9, 5.3, 30)
    assert scroll_pacing.page_limited_fps(page, 4.96, 5.17, painted=10.0, fps=30) is None
    # The page drew 20, the screencast kept 10: the capture fell short.
    assert scroll_pacing.page_limited_fps(_page_frames(4.9, 5.3, 20), 4.96, 5.17, painted=10.0, fps=30) is None


def test_a_take_recorded_before_the_page_counted_its_frames_says_nothing():
    assert scroll_pacing.page_limited_fps(None, 4.96, 5.17, painted=10.0, fps=30) is None
    assert scroll_pacing.page_frame_times([{"step": 1}]) is None
    assert scroll_pacing.page_frame_times([{"step": 1, "page_frames": [2.0, 1.0]}, {"step": 2}]) == [1.0, 2.0]


def test_the_motion_watch_counts_the_pages_own_frames():
    source = capture._MOTION_WATCH_JS
    assert "frames() { return frames.splice(0, frames.length); }" in source
    assert "frameUntil(ms + AFTER);" in source


# ── The quota's hold-back span by span ───────────────────────────────────────


def test_the_hold_back_is_counted_over_a_span_in_proportion():
    held = [[32.92, 36.87, 4.2]]
    assert scroll_pacing.held_back_over(held, 34.98, 35.17) > 0
    assert scroll_pacing.held_back_over(held, 4.96, 5.17) == 0
    assert scroll_pacing.held_back_over(None, 4.96, 5.17) is None


def test_the_tap_keeps_the_hold_back_span_by_span(monkeypatch):
    readings = iter([1.0, 1.5, 1.5, 2.0])
    monkeypatch.setattr(canvas_alone, "_cpu_stat", lambda: SimpleNamespace(throttled_seconds=next(readings)))
    clock = iter([100.25, 100.5, 100.75, 101.0])
    monkeypatch.setattr(canvas_alone.time, "time", lambda: next(clock))
    tap = canvas_alone.Tap.__new__(canvas_alone.Tap)
    tap.origin = 100.0
    tap.held_back = []
    tap._held_last = (0.0, 1.0)
    for _ in range(4):
        tap.sample_held_back()
    # Held back 0.5 s between 0.25 and 0.5, then 0.5 s between 0.75 and 1.0: two spans, never joined.
    assert tap.held_back == [[0.25, 0.5, 0.5], [0.75, 1.0, 0.5]]


# ── The refusal ─────────────────────────────────────────────────────────────


def _click(page_limited: float | None, held_back: float | None) -> dict:
    # Longer than one repaint (``scroll_pacing.REPAINT_SECONDS``): a motion, never a discrete change cut.
    entry = {"t": [4.9, 5.5], "paced": True, "painted": 10.0, "fps": 30.0, "held": 6, "held_not_blended": True}
    if page_limited is not None:
        entry["page_limited"] = page_limited
    if held_back is not None:
        entry["quota_held_back"] = held_back
    return {"step": 1, "action": "click", "motion": [entry]}


def test_a_page_limited_transition_is_neither_synthesised_nor_refused():
    event = _click(10.0, 0.0)
    assert scroll_pacing.synthesised_transitions([event], fps=30) == []
    assert capture_rate.refusal({**GPU, "stepping": []}, "refuse", [event]) == ""
    [said] = capture_rate.page_limited_notes({"stepping": []}, [event])
    assert said.startswith("page-limited at step 1") and "none synthesised" in said


def test_a_starved_transition_is_still_refused():
    event = _click(10.0, 0.4)
    assert scroll_pacing.synthesised_transitions([event], fps=30)
    said = capture_rate.refusal({**GPU, "stepping": []}, "refuse", [event])
    assert said.startswith("Refused after recording")
    assert capture_rate.page_limited_notes({"stepping": []}, [event])[0].startswith("starved at step 1")


def test_a_transition_whose_hold_back_was_not_measured_is_still_refused():
    event = _click(10.0, None)
    assert capture_rate.refusal({**GPU, "stepping": []}, "refuse", [event]).startswith("Refused after recording")


def _canvas_stretch(**extra) -> dict:
    stretch = {"t": [34.98, 35.17], "frames": 6, "synthesised": 3, "share": 0.5, "fps": 30, "step": 16}
    return {**stretch, "canvas": "canvas#graph", **extra}


def test_a_page_limited_canvas_stretch_is_kept_and_a_starved_one_refused():
    kept = _canvas_stretch(page_limited=8.0, quota_held_back=0.0)
    assert capture_rate.refusal({**GPU, "stepping": [kept]}, "refuse", []) == ""
    starved = _canvas_stretch(page_limited=8.0, quota_held_back=0.2)
    assert capture_rate.refusal({**GPU, "stepping": [starved]}, "refuse", []).startswith("Refused after recording")


def test_another_fallback_is_untouched():
    assert capture_rate.refusal({**GPU, "stepping": [_canvas_stretch()]}, "fps", []) == ""


def test_a_canvas_copied_at_the_rate_the_page_drew_it_is_marked_page_limited():
    stretch = capture_rate.Stepping(34.98, 35.17, 6, 3, 30, 16, canvas="canvas#graph")
    kept = stretch.as_dict()
    run = {"t": [34.98, 35.17], "fps": 8.0, "drawn": 8.0}
    capture_rate._mark_page_limited(kept, stretch, [run], None, [[32.92, 36.87, 4.2]])
    assert kept["page_limited"] == 8.0 and kept["quota_held_back"] > 0
    # The page drew 20, the copies kept 8: the copy loop fell short, never page-limited.
    kept = stretch.as_dict()
    capture_rate._mark_page_limited(kept, stretch, [{**run, "drawn": 20.0}], None, [])
    assert "page_limited" not in kept


# ── Retest of take 92c8c265: the screencast span follows its canvas copy ─────────


def test_a_screencast_stretch_over_a_canvas_kept_as_painted_is_page_limited():
    # Step 1 of take 92c8c265: the screencast stretch 4.99-5.19 s (the whole page, no canvas named) painted 10 fps,
    # 67% synthesised; its canvas copy kept as painted, 11 fps copied of 17 drawn, no hold-back there.
    stretch = capture_rate.Stepping(4.99, 5.19, 6, 4, 30, 1)
    kept = stretch.as_dict()
    run = {"t": [4.98, 5.2], "fps": 11.0, "drawn": 17.0, "why": canvas_alone.NO_FASTER, "composited": False}
    page = _page_frames(4.5, 5.5, 60)  # the page's frame loop at the display's rate: no proof alone
    capture_rate._mark_page_limited(kept, stretch, [run], page, [[32.92, 36.87, 4.2]])
    assert kept["page_limited"] == 17.0 and kept["quota_held_back"] == 0
    assert capture_rate.refusal({**GPU, "stepping": [kept]}, "refuse", []) == ""
    [said] = capture_rate.page_limited_notes({"stepping": [kept]}, [])
    assert said.startswith("page-limited at step 1")


def test_step_16_copied_at_the_rate_drawn_is_page_limited():
    stretch = capture_rate.Stepping(34.16, 34.37, 6, 3, 30, 16)
    kept = stretch.as_dict()
    run = {"t": [34.15, 34.38], "fps": 8.0, "drawn": 8.0, "why": canvas_alone.NO_FASTER}
    capture_rate._mark_page_limited(kept, stretch, [run], None, [[0.0, 1.0, 0.0]])
    assert scroll_pacing.page_limited_kept(kept)


def test_a_screencast_stretch_whose_capture_fell_short_of_the_canvas_is_refused_as_such():
    stretch = capture_rate.Stepping(4.99, 5.99, 30, 20, 30, 1, canvas="canvas#graph")
    kept = stretch.as_dict()
    run = {"t": [4.99, 5.99], "fps": 6.0, "drawn": 20.0}
    capture_rate._mark_page_limited(kept, stretch, [run], None, [])
    assert "page_limited" not in kept and kept["page_drew"] == 20.0
    said = capture_rate.refusal({**GPU, "stepping": [kept]}, "refuse", [])
    assert said.startswith("Refused after recording") and "capture short: the page drew 20 fps" in said
    assert capture_rate.page_limited_notes({"stepping": [kept]}, [])[0].startswith("capture short at step 1")


def test_a_transition_over_a_canvas_kept_as_painted_is_page_limited():
    runs = [{"t": [4.9, 5.5], "fps": 10.0, "drawn": 10.0, "why": canvas_alone.NO_FASTER}]
    assert scroll_pacing.canvas_limited(runs, 4.9, 5.5, painted=10.0, fps=30) == (10.0, None)
    assert scroll_pacing.canvas_limited(runs, 6.0, 7.0, painted=10.0, fps=30) == (None, None)
    # A canvas the page drew at the rate kept at 10: a capture short of it (#11032 retest), never unmeasured; one
    # composited from its copies: no classification from it.
    assert scroll_pacing.canvas_limited([{**runs[0], "drawn": 30.0}], 4.9, 5.5, painted=10.0, fps=30) == (None, 30.0)
    assert scroll_pacing.canvas_limited([{**runs[0], "composited": True}], 4.9, 5.5, painted=10, fps=30) == (None, None)
    short = [{"t": [4.9, 5.9], "fps": 5.0, "drawn": 20.0}]
    assert scroll_pacing.canvas_limited(short, 4.9, 5.9, painted=5.0, fps=30) == (None, 20.0)


def test_each_refused_cause_is_said_apart():
    starved = _click(10.0, 0.4)
    unmeasured = _click(10.0, None)
    unread = _click(None, None)
    found = scroll_pacing.synthesised_transitions([starved, unmeasured, unread], fps=30)
    causes = [item["cause"] for item in found]
    assert causes[0].startswith("starved") and causes[1].startswith("page-limited, quota unread: the page drew 10 fps")
    assert causes[2] == "unmeasured: the page's own frames were not counted"
    words = scroll_pacing.synthesised_words(found)
    assert "starved: the worker's CPU quota held it back 0.40 s" in words
    assert capture_rate.page_limited_notes({"stepping": []}, [unmeasured])[0].startswith(
        "page-limited, quota unread at step 1"
    )


def test_the_quota_is_marked_on_each_page_limited_transition():
    event = _click(10.0, None)
    scroll_pacing.mark_quota([event], [[4.9, 5.5, 0.0]])
    assert event["motion"][0]["quota_held_back"] == 0.0
    assert scroll_pacing.page_limited_kept(event["motion"][0])
