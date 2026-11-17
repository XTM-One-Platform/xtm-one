"""A brief motion the page itself delivered under the take's rate never refuses a whole take (#11355).

Shapes of recording 78c791eb (GPU L4, fps 30, ``fps_fallback: refuse``): a 0.2 s svg transition after a click the
screencast delivered at 10 fps, the worker's quota holding nothing back over it ("page-limited, screencast rate"),
and a 15 fps transition its worker's quota held back 1.22 s during a node-locate probe. The take was refused for
both: the first is page-limited advice now, the second still refuses it, as starved. No browser, no database."""

from __future__ import annotations

from app.services.video_studio import capture_rate, scroll_pacing

GPU = {"gl": {"gpu": True}, "fps": 30}


def _click(t: list[float], painted: float) -> dict:
    # Blended frames paced to 30 fps from keyframes: a motion, not a discrete change cut in one repaint.
    return {"t": t, "paced": True, "painted": painted, "fps": 30.0, "frames": 6}


def _page_limited() -> dict:
    return {"step": 3, "action": "click", "motion": [_click([12.40, 12.60], 10.0)]}


def _starved() -> dict:
    return {"step": 9, "action": "click", "motion": [_click([35.10, 35.30], 15.0)]}


HELD_BACK = [[0.0, 33.0, 0.0], [33.08, 37.25, 1.22], [37.25, 60.0, 0.0]]


def test_a_brief_transition_at_its_screencast_rate_is_kept_and_said_page_limited():
    events = [_page_limited()]
    scroll_pacing.mark_quota(events, HELD_BACK, canvas_runs=[], fps=30)
    entry = events[0]["motion"][0]
    assert entry["screencast_rate"] == 10.0 and entry["quota_held_back"] == 0.0
    assert scroll_pacing.page_limited_kept(entry)
    assert scroll_pacing.synthesised_transitions(events, fps=30) == []
    assert capture_rate.refusal({**GPU, "stepping": []}, "refuse", events) == ""
    [note] = capture_rate.page_limited_notes({"stepping": []}, events)
    assert note.startswith("page-limited at step 3 (12.40-12.60 s): the page drew 10 fps")


def test_the_take_is_refused_for_the_starved_span_alone_and_recorded_again():
    events = [_page_limited(), _starved()]
    scroll_pacing.mark_quota(events, HELD_BACK, canvas_runs=[], fps=30)
    said = capture_rate.refusal({**GPU, "stepping": []}, "refuse", events)
    assert "step 9" in said and "starved: the worker's CPU quota held it back" in said
    assert "step 3" not in said
    # Every span refused is the worker's: the take is recorded once more before it is refused (#11097).
    assert capture_rate.starved_only({**GPU, "stepping": []}, "refuse", events)


def test_a_longer_motion_at_its_screencast_rate_is_still_refused():
    events = [{"step": 3, "motion": [_click([12.40, 12.80], 10.0)]}]
    scroll_pacing.mark_quota(events, HELD_BACK, canvas_runs=[], fps=30)
    assert capture_rate.refusal({**GPU, "stepping": []}, "refuse", events)


def test_a_brief_motion_the_quota_starved_or_did_not_read_is_never_kept():
    starved = [_starved()]
    scroll_pacing.mark_quota(starved, HELD_BACK, canvas_runs=[], fps=30)
    assert not scroll_pacing.page_limited_kept(starved[0]["motion"][0])
    unread = [_page_limited()]
    scroll_pacing.mark_quota(unread, None, canvas_runs=[], fps=30)
    assert not scroll_pacing.page_limited_kept(unread[0]["motion"][0])
    windowed = {**_click([12.40, 12.60], 10.0), "screencast_rate": 10.0, "quota_held_back": 0.0}
    windowed["starved_window"] = [12.0, 13.0]
    assert not scroll_pacing.page_limited_kept(windowed)


def test_a_brief_canvas_zoom_paced_between_keyframes_is_not_kept_unless_held():
    zoom = {**_click([12.40, 12.60], 10.0), "scroller": "canvas", "screencast_rate": 10.0, "quota_held_back": 0.0}
    assert not scroll_pacing.page_limited_kept(zoom)
    assert scroll_pacing.page_limited_kept({**zoom, "held_not_blended": True})


def test_fps_fallback_other_than_refuse_is_unchanged():
    events = [_page_limited(), _starved()]
    scroll_pacing.mark_quota(events, HELD_BACK, canvas_runs=[], fps=30)
    assert capture_rate.refusal({**GPU, "stepping": []}, "fps", events) == ""
