"""A recording scene whose kept seconds play its page still loading is flagged at save (issue #11377).

The recorder's action log knows when a page loads: the click that opened it, the ``wait`` held until it settled
(``settled.at``) and every span a hold showed a progress indicator or an empty canvas (``loading``). A scene trimmed over
that stretch used to save, preview and render green. Proven here on the stored action log alone:

* :func:`page_settle.loading_spans` reads a click to its wait's settled time and every logged span;
* the save raises a blocking ``scene_loading_span`` naming the scene, the take, the seconds overlapped and the trim that
  removes it, and the pre-render lint reads the same finding;
* a scene trimmed past the settled time saves clean, an overlap under 0.3 s is tolerated, a take with no loading span
  is unchanged.
"""

from __future__ import annotations

from app.services.video_studio import page_settle, quality, spec, timeline

TAKE_ID = "00000000-0000-4000-8000-000000011377"
BAR = "span.MuiCircularProgress-root[progressbar]"
EVENTS = [
    {"step": 0, "action": "goto", "t0": 0.0, "ta": 0.0, "t1": 1.0, "url": "https://app/i"},
    {
        "step": 1,
        "action": "click",
        "label": "Start an investigation",
        "t0": 1.0,
        "ta": 1.5,
        "t1": 1.6,
        "url": "https://app/i/1",
        "url_before": "https://app/i",
    },
    {
        "step": 2,
        "action": "wait",
        "t0": 1.7,
        "ta": 1.7,
        "t1": 4.4,
        "until_settled": True,
        "settled": {"at": 4.2, "after": 2.5, "seconds": 2.0},
        "loading": [[1.7, 3.1, [BAR]]],
    },
    {"step": 3, "action": "pause", "t0": 4.4, "ta": 4.4, "t1": 8.0},
]
SETTLED = [
    {"step": 0, "action": "goto", "t0": 0.0, "ta": 0.0, "t1": 1.0, "url": "https://app/i"},
    {
        "step": 1,
        "action": "click",
        "t0": 1.0,
        "ta": 1.5,
        "t1": 1.6,
        "url": "https://app/i",
        "url_before": "https://app/i",
    },
    {"step": 2, "action": "pause", "t0": 1.7, "ta": 1.7, "t1": 8.0},
]


def _findings(scene: dict, events: list[dict] = EVENTS) -> list:
    authored = spec.validate_video_spec(
        {
            "title": "Investigation",
            "freestyle": True,
            "scenes": [{"id": "demo", "type": "recording", "take_id": TAKE_ID, "idle": {"mode": "off"}, **scene}],
        }
    )
    parsed = spec.parse_video_spec(authored)
    document = timeline.estimate_plan(parsed, {}, {}).document
    take = timeline.TakeMedia(TAKE_ID, 8.0, 1600, 900, 1.0, 60, 480, events=events, captured_frames=480)
    found = quality.content_findings(parsed, document, quality.ContentFacts(takes={TAKE_ID: take}), authored)
    return [f for f in found if f.kind == "scene_loading_span"]


def test_loading_spans_read_a_click_to_its_settled_time_and_every_logged_span():
    spans = page_settle.loading_spans(EVENTS)
    assert [(s.start, s.end) for s in spans] == [(1.5, 4.2), (1.7, 3.1)]
    assert spans[0].why == "step 1 (click) opened a page that settled at 4.2 s"
    assert spans[1].why == f"step 2 (wait) showed {BAR}"
    assert page_settle.loading_spans(SETTLED) == []


def test_a_wait_that_gave_up_loads_to_its_end():
    events = [*EVENTS[:2], {**EVENTS[2], "settled": {"at": None, "waited": 2.7, "seconds": 2.0}}]
    [span, _logged] = page_settle.loading_spans(events)
    assert (span.start, span.end) == (1.5, 4.4)
    assert span.why == "step 1 (click) opened a page not settled by 4.4 s"


def test_a_scene_trimmed_over_the_loading_span_is_flagged_with_the_overlap():
    [finding] = _findings({"trim_start": 1.0})
    assert finding.severity == "dense" and finding.scene_id == "demo"
    assert finding.message.startswith(f"take {TAKE_ID[:8]} plays its page still loading for 2.7 s on scene demo: ")
    assert "step 1 (click) opened a page that settled at 4.2 s (1.5-4.2 s)" in finding.message
    assert "move trim_start to 4.20 s (past the page settling)" in finding.message
    assert "cut the scene in two around it" in finding.message and "set idle mode cut" in finding.message


def test_the_same_scene_trimmed_after_the_settled_time_saves_clean():
    assert _findings({"trim_start": 4.3}) == []


def test_a_partial_overlap_under_a_third_of_a_second_is_tolerated():
    assert _findings({"trim_start": 4.0}) == []
    [finding] = _findings({"trim_start": 3.8})
    assert "for 0.4 s" in finding.message


def test_a_scene_with_no_loading_span_is_unchanged():
    assert _findings({}, SETTLED) == []


def test_idle_cut_compresses_the_wait_so_less_of_the_loading_plays():
    cut = _findings({"trim_start": 1.0, "idle": {"mode": "cut"}})
    off = _findings({"trim_start": 1.0})
    seconds = [f.data["seconds"] for f in cut]
    assert all(s < off[0].data["seconds"] for s in seconds)
