"""A click transition or a canvas wheel zoom is never refused as unmeasured once the take read its worker's quota (#11097).

Shapes of the #11067 retest (GPU L4, fps 30, ``fps_fallback: refuse``): step 1, a click-triggered svg transition at
5.10-5.30 s painted 15 fps with no counted page frames and no recorder probe; step 8, a wheel zoom on
``canvas.clickable`` at 16.79-19.16 s painted 27 fps inside a composited copy run of it (16.30-18.65 s, 66 copies at
27.7 fps, the page drawing 40.1). No browser, no database."""

from __future__ import annotations

from app.services.video_studio import capture_rate, runners, scroll_pacing

GPU = {"gl": {"gpu": True}, "fps": 30}


def _click() -> dict:
    # Six frames paced to 30 fps from keyframes, blended: a motion, not a discrete change cut in one repaint.
    return {"t": [5.10, 5.30], "paced": True, "painted": 15.0, "fps": 30.0, "frames": 6}


def _zoom() -> dict:
    return {
        "t": [16.79, 19.16],
        "paced": True,
        "painted": 27.0,
        "fps": 30.0,
        "scroller": "canvas",
        "held": 2,
        "held_not_blended": True,
    }


COPY_RUN = {
    "canvas": "canvas.clickable",
    "t": [16.30, 18.65],
    "copies": 66,
    "fps": 27.7,
    "painted": 27.7,
    "drawn": 40.1,
    "composited": True,
}


# ── (a) a click transition with no counted page frames ─────────────────────


def test_a_click_transition_the_quota_held_back_just_before_is_starved():
    events = [{"step": 1, "motion": [_click()]}]
    scroll_pacing.mark_quota(events, [[4.70, 5.05, 0.3]], canvas_runs=[], fps=30)
    entry = events[0]["motion"][0]
    assert entry["quota_held_back"] > 0
    assert scroll_pacing.refused_cause(entry).startswith("starved: the worker's CPU quota held it back")


def test_a_click_transition_the_quota_held_nothing_back_is_page_limited_at_the_screencast_rate():
    events = [{"step": 1, "motion": [_click()]}]
    scroll_pacing.mark_quota(events, [[0.0, 30.0, 0.0]], canvas_runs=[], fps=30)
    entry = events[0]["motion"][0]
    assert entry["quota_held_back"] == 0.0 and entry["screencast_rate"] == 15.0
    cause = scroll_pacing.refused_cause(entry)
    assert cause.startswith("page-limited, screencast rate: the page delivered 15 fps")
    assert "unmeasured" not in cause


def test_a_take_that_did_not_read_its_quota_still_says_unmeasured():
    events = [{"step": 1, "motion": [_click()]}]
    scroll_pacing.mark_quota(events, None, canvas_runs=[], fps=30)
    assert scroll_pacing.refused_cause(events[0]["motion"][0]).startswith("unmeasured")


def test_a_stepping_stretch_with_no_counted_frames_is_told_by_the_quota_too():
    stretch = {"t": [5.10, 5.30], "frames": 6, "synthesised": 3, "fps": 30}
    scroll_pacing.mark_unclassified([stretch], [[5.0, 5.2, 0.2]])
    assert scroll_pacing.refused_cause(stretch).startswith("starved")
    calm = {"t": [5.10, 5.30], "frames": 6, "synthesised": 3, "fps": 30}
    scroll_pacing.mark_unclassified([calm], [])
    assert calm["screencast_rate"] == 15.0
    assert capture_rate._classified(calm) and capture_rate._classified(stretch)


def test_a_span_already_classified_keeps_its_label():
    entry = {**_click(), "page_drew": 30.0}
    scroll_pacing.mark_unclassified([entry], [[5.0, 5.3, 0.5]])
    assert "quota_held_back" not in entry and "screencast_rate" not in entry


# ── (b) a canvas wheel zoom inside a composited copy run ───────────────────


def test_a_canvas_zoom_inside_a_composited_copy_run_follows_its_counts():
    events = [{"step": 8, "scrolls": [_zoom()]}]
    scroll_pacing.mark_quota(events, [[0.0, 30.0, 0.0]], canvas_runs=[COPY_RUN], fps=30)
    entry = events[0]["scrolls"][0]
    assert entry["page_limited"] == 30.0 and entry["quota_held_back"] == 0.0
    assert scroll_pacing.page_limited_kept(entry)
    assert not scroll_pacing.synthesised_transitions(events, fps=30)


def test_a_composited_run_still_tells_nothing_of_a_screencast_window():
    # #11048's behaviour kept: a stretch of the screencast over a composited run is not classified by it.
    assert scroll_pacing.canvas_limited([COPY_RUN], 16.79, 18.0, painted=27.0, fps=30) == (None, None)
    assert scroll_pacing.canvas_limited([COPY_RUN], 16.79, 18.0, painted=27.0, fps=30, composited=True) == (30.0, None)


# ── (c) a take refused only for starved spans is recorded once more ────────


def test_a_take_refused_only_for_starved_spans_is_recorded_again():
    events = [{"step": 1, "motion": [_click()]}]
    scroll_pacing.mark_quota(events, [[4.70, 5.05, 0.3]], canvas_runs=[], fps=30)
    assert capture_rate.refusal(GPU, "refuse", events).startswith("Refused after recording")
    assert "starved: the worker's CPU quota held it back" in capture_rate.refusal(GPU, "refuse", events)
    assert capture_rate.starved_only(GPU, "refuse", events)


def test_a_take_refused_for_the_page_is_not_recorded_again():
    # 0.4 s: a span under 0.3 s at its screencast rate is kept as page-limited advice (#11355).
    events = [{"step": 1, "motion": [{**_click(), "t": [5.10, 5.50]}]}]
    scroll_pacing.mark_quota(events, [[0.0, 30.0, 0.0]], canvas_runs=[], fps=30)
    assert capture_rate.refusal(GPU, "refuse", events)
    assert not capture_rate.starved_only(GPU, "refuse", events)
    assert not capture_rate.starved_only(GPU, "fps", events)


def test_the_refusal_words_keep_their_shape():
    events = [{"step": 1, "motion": [_click()]}]
    said = capture_rate.refusal(GPU, "refuse", events)
    assert "step 1 at 5.10-5.30 s painted at 15 fps (50% of its frames synthesised; unmeasured: the page's own" in said
    assert said.endswith("(a page drawn on the GPU paints at its own rate whatever the window).")


def test_the_retry_says_why_it_records_again():
    assert "CPU quota" in runners.STARVED_RETRY_STAGE and len(runners.STARVED_RETRY_STAGE) <= 200
    assert runners.STARVED_RETRY_WORDS.startswith("Recorded twice")
