"""The refusal's labels tell each cause apart under ``fps_fallback: refuse`` (#11021 retest).

Probe recording 8ecac4ad: step 16 (34.89-35.09 s), a canvas zoom whose copy counted the page's own frames (8 copied of
8 drawn, "copied no faster than painted"), was refused "unmeasured: the page's own frames were not counted" while the
capture line said "starved at 32.67-36.79 s (14.3 fps)". The label decision: ``page-limited`` where the copy counted
the page's frames and kept every one, ``starved`` where the quota held the worker back or the span overlaps a window
the capture was starved in, ``capture short`` where the capture kept fewer than the page drew, ``unmeasured`` only
where no count of the page's own frames exists. No browser, no database."""

from __future__ import annotations

from app.services.video_studio import canvas_alone, capture_rate, scroll_pacing

GPU = {"gl": {"gpu": True}, "fps": 30}
STARVED = {"painting": True, "starved": [[32.67, 36.79, 14.3]]}


def _zoom(**extra) -> dict:
    # Step 16 as the take keeps it: a canvas wheel zoom paced from keyframes, painted 15 fps, half synthesised.
    entry = {"t": [34.89, 35.09], "scroller": "canvas", "paced": True, "painted": 15.0, "fps": 30.0}
    return {"step": 16, "action": "scroll", "scrolls": [{**entry, **extra}]}


def _copy(**extra) -> dict:
    return {"t": [34.88, 35.1], "fps": 8.0, "drawn": 8.0, "why": canvas_alone.NO_FASTER, "composited": False, **extra}


# ── The paced scroll is classified by its canvas copy ───────────────────────────


def test_a_canvas_zoom_whose_copy_counted_the_pages_frames_is_page_limited_not_unmeasured():
    event = _zoom()
    scroll_pacing.mark_quota([event], [[0.0, 60.0, 0.0]], canvas_runs=[_copy()], fps=30)
    entry = event["scrolls"][0]
    assert entry["page_limited"] == 8.0 and entry["quota_held_back"] == 0.0
    [found] = scroll_pacing.synthesised_transitions([event], fps=30)
    assert found["cause"].startswith("page-limited")
    assert "unmeasured" not in found["cause"]


def test_a_canvas_zoom_held_at_the_pages_frames_is_kept_and_said_page_limited():
    event = _zoom(held=3, held_not_blended=True)
    scroll_pacing.mark_quota([event], [[0.0, 60.0, 0.0]], canvas_runs=[_copy()], fps=30)
    assert scroll_pacing.page_limited_kept(event["scrolls"][0])
    assert capture_rate.refusal({**GPU, "stepping": []}, "refuse", [event]) == ""
    [said] = capture_rate.page_limited_notes({"stepping": []}, [event])
    assert said.startswith("page-limited at step 16") and "none synthesised" in said


def test_the_copy_overlapping_the_span_most_decides_not_a_neighbour_grazing_its_edge():
    neighbour = {"t": [34.0, 34.9], "fps": 30.0, "drawn": 30.0, "why": canvas_alone.PAINTED_AT_RATE}
    assert scroll_pacing.canvas_limited([neighbour, _copy()], 34.89, 35.09, painted=15.0, fps=30) == (8.0, None)


# ── A span inside a starved window is starved ─────────────────────────────────


def test_step_16_inside_the_captures_starved_window_is_refused_as_starved():
    event = _zoom()
    scroll_pacing.mark_quota([event], None, canvas_runs=[_copy()], fps=30)
    capture = {**GPU, "stepping": [], "insert_rate": STARVED}
    said = capture_rate.refusal(capture, "refuse", [event])
    assert said.startswith("Refused after recording")
    assert "starved: it overlaps the capture's starved window 32.67-36.79 s" in said
    assert "unmeasured" not in said
    [note] = capture_rate.page_limited_notes(capture, [event])
    assert note.startswith("starved at step 16")


def test_a_starved_window_voids_a_page_limited_keep():
    event = _zoom(held=3, held_not_blended=True)
    scroll_pacing.mark_quota([event], [[0.0, 60.0, 0.0]], canvas_runs=[_copy()], fps=30)
    capture = {**GPU, "stepping": [], "insert_rate": STARVED}
    assert capture_rate.refusal(capture, "refuse", [event]).startswith("Refused after recording")
    assert not scroll_pacing.page_limited_kept(event["scrolls"][0])


def test_a_stretch_inside_a_starved_window_is_said_starved():
    stretch = {"t": [34.98, 35.17], "frames": 6, "synthesised": 3, "share": 0.5, "fps": 30, "step": 16}
    stretch |= {"canvas": "canvas#graph", "page_limited": 8.0, "quota_held_back": 0.0}
    capture = {**GPU, "stepping": [stretch], "insert_rate": STARVED}
    said = capture_rate.refusal(capture, "refuse", [])
    assert "starved: it overlaps the capture's starved window" in said


def test_the_starved_window_is_read_only_where_it_overlaps():
    assert scroll_pacing.starved_window([[32.67, 36.79, 14.3]], 34.89, 35.09) == [32.67, 36.79]
    assert scroll_pacing.starved_window([[32.67, 36.79, 14.3]], 14.97, 17.35) is None
    assert scroll_pacing.starved_window(None, 1.0, 2.0) is None


# ── Each label apart ─────────────────────────────────────────────────────────


def test_each_label_is_distinct():
    cause = scroll_pacing.refused_cause
    assert cause({"quota_held_back": 0.4}).startswith("starved: the worker's CPU quota")
    assert cause({"starved_window": [32.67, 36.79]}).startswith("starved: it overlaps")
    assert cause({"page_drew": 20.0}).startswith("capture short")
    assert cause({"page_limited": 8.0}).startswith("page-limited, quota unread")
    assert cause({"page_limited": 8.0, "quota_held_back": 0.0}).startswith("page-limited, paced between")
    assert cause({}) == "unmeasured: the page's own frames were not counted"


def test_a_span_with_no_page_frame_count_is_page_limited_at_its_screencast_rate():
    event = _zoom()
    scroll_pacing.mark_quota([event], [[0.0, 60.0, 0.0]], canvas_runs=[], fps=30)
    [found] = scroll_pacing.synthesised_transitions([event], fps=30)
    # The quota read and holding nothing back: the page's rate is the screencast's, never unmeasured (#11097).
    assert found["cause"].startswith("page-limited, screencast rate:")
    # Unread, it stays unmeasured.
    unread = _zoom()
    scroll_pacing.mark_quota([unread], None, canvas_runs=[], fps=30)
    [found] = scroll_pacing.synthesised_transitions([unread], fps=30)
    assert found["cause"] == "unmeasured: the page's own frames were not counted"


def test_a_capture_short_of_the_canvas_copy_is_said_capture_short():
    # Painted 5 fps and copied 4 of the 20 the page drew: more than one frame short over 0.2 s.
    event = _zoom(painted=5.0)
    scroll_pacing.mark_quota([event], [[0.0, 60.0, 0.0]], canvas_runs=[_copy(fps=4.0, drawn=20.0)], fps=30)
    [found] = scroll_pacing.synthesised_transitions([event], fps=30)
    assert found["cause"].startswith("capture short: the page drew 20 fps")
