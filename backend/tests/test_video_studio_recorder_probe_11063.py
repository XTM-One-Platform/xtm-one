"""A span drawn under the recorder's own probing is never said ``unmeasured`` (#11063, retest of #11048).

Probe recording c9d56974 (fps 30, ``fps_fallback: refuse``, GPU L4): step 16's transition 35.38-35.58 s, painted 15
fps and 50% synthesised, at the tail of the node's locate probe (probed 33.10-35.38 s) before the click, no canvas copy
over it, was refused "unmeasured: the page's own frames were not counted" while the worker's quota held it back. Such
a span is starved where the quota held the worker back over it, else page-limited, and one the take hides whole (its
frames held as the frame before the probe) is not judged as motion. The take's error keeps whole lines: a note cut at
2000 characters read "the page drew 8." for 8.8 fps. No browser, no database."""

from __future__ import annotations

from app.services.video_studio import capture_rate, scroll_pacing

GPU = {"gl": {"gpu": True}, "fps": 30}
PROBED = [33.10, 35.38]
SPAN = [35.38, 35.58]


def _step(**extra) -> dict:
    # Step 16 as the take keeps it: a paced transition over an svg, 15 fps painted, after the node's locate probe.
    entry = {"t": list(SPAN), "painted": 15.0, "fps": 30.0, "paced": True, "frames": 6, "held": 0}
    return {"step": 16, "action": "click", "probed": list(PROBED), "motion": [{**entry, **extra}]}


def test_a_probe_span_the_quota_held_back_is_starved_not_unmeasured():
    event = _step()
    scroll_pacing.mark_quota([event], [[33.0, 36.0, 3.0]], canvas_runs=[], fps=30)
    entry = event["motion"][0]
    assert entry["recorder_probe"] == [33.1, 35.48]
    assert entry["quota_held_back"] > 0
    [found] = scroll_pacing.synthesised_transitions([event], fps=30)
    assert found["cause"].startswith("starved: the worker's CPU quota held it back")
    said = capture_rate.refusal({**GPU, "stepping": []}, "refuse", [event])
    assert "unmeasured" not in said and "starved" in said


def test_a_probe_span_the_quota_left_alone_is_page_limited_and_kept():
    event = _step()
    scroll_pacing.mark_quota([event], [[0.0, 60.0, 0.0]], canvas_runs=[], fps=30)
    entry = event["motion"][0]
    assert entry["quota_held_back"] == 0.0 and scroll_pacing.probe_kept(entry)
    assert scroll_pacing.synthesised_transitions([event], fps=30) == []
    assert capture_rate.refusal({**GPU, "stepping": []}, "refuse", [event]) == ""
    [note] = capture_rate.page_limited_notes({"stepping": []}, [event])
    assert note.startswith("page-limited, recorder probe at step 16 (35.38-35.58 s): the page drew 15 fps")


def test_a_probe_span_whose_quota_was_not_read_is_page_limited_quota_unread():
    event = _step()
    scroll_pacing.mark_quota([event], None, canvas_runs=[], fps=30)
    [found] = scroll_pacing.synthesised_transitions([event], fps=30)
    assert found["cause"].startswith("page-limited, recorder probe, quota unread")


def test_a_span_hidden_whole_by_the_probe_is_not_judged_as_motion():
    event = _step(t=[33.5, 34.0])
    scroll_pacing.mark_quota([event], [[33.0, 36.0, 3.0]], canvas_runs=[], fps=30)
    assert event["motion"][0]["probe_hidden"] is True
    assert scroll_pacing.synthesised_transitions([event], fps=30) == []


def test_a_span_no_probe_overlaps_is_told_by_the_quota():
    # 0.4 s: a span under 0.3 s at its screencast rate is kept as page-limited advice (#11355).
    event = _step(t=[40.0, 40.4])
    scroll_pacing.mark_quota([event], [[33.0, 36.0, 3.0]], canvas_runs=[], fps=30)
    assert "recorder_probe" not in event["motion"][0]
    [found] = scroll_pacing.synthesised_transitions([event], fps=30)
    # No probe, no counted frame, the quota holding nothing back over it: page-limited at its screencast rate (#11097).
    assert found["cause"].startswith("page-limited, screencast rate:")


def test_a_span_a_copy_classified_keeps_its_classification():
    # #11048 clause 1: a counted copy decides before the probe does.
    event = _step(page_limited=30.0, held_not_blended=True)
    scroll_pacing.mark_quota([event], [[0.0, 60.0, 0.0]], canvas_runs=[], fps=30)
    assert "recorder_probe" not in event["motion"][0]
    assert scroll_pacing.page_limited_kept(event["motion"][0])


def test_a_stepping_stretch_under_the_probe_is_classified():
    stretch = capture_rate.Stepping(*SPAN, 6, 3, 30, 16, canvas=None)
    kept = stretch.as_dict()
    scroll_pacing.mark_probes([kept], [_step()], [[33.0, 36.0, 3.0]])
    assert kept["recorder_probe"] == [33.1, 35.48]
    assert scroll_pacing.refused_cause(kept).startswith("starved")
    [note] = capture_rate.page_limited_notes({"stepping": [kept]}, [])
    assert note.startswith("starved at step 16 (35.38-35.58 s): the page drew 15 fps")


def test_the_error_keeps_whole_lines():
    refused = "Refused after recording: " + "x" * 1900
    note = "page-limited at step 16 (36.69-36.84 s): the page drew 8.8 fps, every frame kept and held, none synthesised"
    said = capture_rate.error_lines([refused, note])
    assert said == refused and len(said) <= capture_rate.ERROR_CHARS
    short = capture_rate.error_lines(["Refused.", note])
    assert short.endswith("8.8 fps, every frame kept and held, none synthesised")


def test_a_first_line_too_long_is_cut_at_a_word():
    said = capture_rate.error_lines(["word " * 500], limit=100)
    assert len(said) <= 100 and not said.endswith("wor")
