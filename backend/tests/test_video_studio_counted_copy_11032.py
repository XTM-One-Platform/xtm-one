"""A span overlapped by a counted canvas copy is never said ``unmeasured`` (#11032 retest).

Probe recording 565499c4 (fps 30, ``fps_fallback: refuse``, GPU L4): step 11 (21.35-21.61 s), a click on the graph's
hierarchical layout, painted 26.9 fps with 8 frames held then cut, was refused "unmeasured: the page's own frames were
not counted" while the canvas copy run 20.73-22.38 s (starting before the step, ending after it) counted 49 copies at
29.2 fps and the page drew its canvas at the display's rate. The copy run overlapping the span decides: every frame
the take's rate can keep kept is the page's own frames (page-limited), fewer a capture short of it. No browser, no
database."""

from __future__ import annotations

from app.services.video_studio import canvas_alone, capture_rate, scroll_pacing

GPU = {"gl": {"gpu": True}, "fps": 30}
STEP_11 = (21.35, 21.61)


def _run(**extra) -> dict:
    # The canvas copy run of step 10 that step 11's click falls inside: 49 copies at 29.2 fps, the page drawing 44.4.
    run = {"t": [20.73, 22.38], "copies": 49, "fps": 29.2, "painted": 44.4, "drawn": 44.4, "composited": False}
    return {**run, "why": canvas_alone.NO_FASTER, "step": 10, **extra}


def _click(**extra) -> dict:
    # Step 11 as the take keeps it: a paced canvas transition, 26.9 fps painted, 8 held then cut.
    entry = {"t": list(STEP_11), "painted": 26.9, "fps": 30.0, "paced": True, "frames": 8, "held": 8}
    entry |= {"held_not_blended": True, "scroller": "canvas"}
    return {"step": 11, "action": "click", "motion": [{**entry, **extra}]}


def test_a_copy_run_spanning_the_step_classifies_it():
    assert scroll_pacing.canvas_limited([_run()], *STEP_11, painted=26.9, fps=30) == (30.0, None)


def test_step_11_is_never_said_unmeasured_and_is_kept_at_the_pages_frames():
    event = _click(page_limited=scroll_pacing.canvas_limited([_run()], *STEP_11, painted=26.9, fps=30)[0])
    scroll_pacing.mark_quota([event], [[0.0, 60.0, 0.0]], canvas_runs=[_run()], fps=30)
    entry = event["motion"][0]
    assert entry["page_limited"] == 30.0 and entry["quota_held_back"] == 0.0
    assert scroll_pacing.page_limited_kept(entry)
    assert scroll_pacing.synthesised_transitions([event], fps=30) == []
    said = capture_rate.refusal({**GPU, "stepping": []}, "refuse", [event])
    assert "unmeasured" not in said
    [note] = capture_rate.page_limited_notes({"stepping": []}, [event])
    assert note.startswith("page-limited at step 11")


def test_a_paced_scroll_inside_the_copy_run_is_classified_by_it():
    event = {
        "step": 11,
        "scrolls": [{"t": list(STEP_11), "scroller": "canvas", "paced": True, "painted": 26.9, "fps": 30.0}],
    }
    scroll_pacing.mark_quota([event], [[0.0, 60.0, 0.0]], canvas_runs=[_run()], fps=30)
    assert event["scrolls"][0]["page_limited"] == 30.0


def test_a_capture_short_inside_the_copy_run_is_said_capture_short_not_unmeasured():
    run = _run(fps=10.0, painted=10.0)
    assert scroll_pacing.canvas_limited([run], *STEP_11, painted=10.0, fps=30) == (None, 44.4)
    event = {
        "step": 11,
        "scrolls": [{"t": list(STEP_11), "scroller": "canvas", "paced": True, "painted": 10.0, "fps": 30.0}],
    }
    scroll_pacing.mark_quota([event], [[0.0, 60.0, 0.0]], canvas_runs=[run], fps=30)
    [found] = scroll_pacing.synthesised_transitions([event], fps=30)
    assert found["cause"].startswith("capture short: the page drew 44.4 fps")


def test_a_canvas_stretch_inside_the_copy_run_follows_it():
    stretch = capture_rate.Stepping(*STEP_11, 8, 1, 30, 11, canvas="canvas#graph")
    kept = stretch.as_dict()
    capture_rate._mark_page_limited(kept, stretch, [_run()], None, [[0.0, 60.0, 0.0]])
    assert kept["page_limited"] == 30.0 and scroll_pacing.page_limited_kept(kept)


def test_the_stretch_reads_the_run_overlapping_it_most():
    stretch = capture_rate.Stepping(*STEP_11, 8, 4, 30, 11, canvas="canvas#graph")
    grazing = {"t": [20.0, 21.36], "fps": 4.0, "drawn": 20.0}
    kept = stretch.as_dict()
    capture_rate._mark_page_limited(kept, stretch, [grazing, _run()], None, [])
    assert kept["page_limited"] == 30.0 and "page_drew" not in kept


def test_a_starved_window_still_wins_over_the_copy_run():
    event = _click()
    scroll_pacing.mark_quota([event], [[0.0, 60.0, 0.0]], canvas_runs=[_run()], fps=30)
    event["motion"][0]["page_limited"] = 30.0
    capture = {**GPU, "stepping": [], "insert_rate": {"painting": True, "starved": [[21.0, 22.0, 14.0]]}}
    said = capture_rate.refusal(capture, "refuse", [event])
    assert "starved: it overlaps the capture's starved window 21-22 s" in said


def test_a_span_no_copy_run_overlaps_is_told_by_the_quota_not_by_a_distant_run():
    # Step 1's shape: the nearest counted copy ran at 2.2 s, nowhere over 4.74-4.94 s.
    run = _run(t=[2.0, 2.4])
    assert scroll_pacing.canvas_limited([run], 4.74, 4.94, painted=10.0, fps=30) == (None, None)
    event = {
        "step": 1,
        "scrolls": [{"t": [4.74, 4.94], "scroller": "canvas", "paced": True, "painted": 10.0, "fps": 30.0}],
    }
    scroll_pacing.mark_quota([event], [[0.0, 60.0, 0.0]], canvas_runs=[run], fps=30)
    [found] = scroll_pacing.synthesised_transitions([event], fps=30)
    # No count of the page's frames over it, the quota holding nothing back: page-limited at the rate the screencast
    # delivered it, never unmeasured (#11097); the distant run tells nothing of it.
    assert "page_limited" not in event["scrolls"][0]
    assert found["cause"].startswith("page-limited, screencast rate: the page delivered 10 fps")


def test_the_pages_rate_is_capped_at_the_takes():
    assert scroll_pacing.page_drawn_rate(44.4, 30) == 30.0
    assert scroll_pacing.page_drawn_rate(8.0, 30) == 8.0
