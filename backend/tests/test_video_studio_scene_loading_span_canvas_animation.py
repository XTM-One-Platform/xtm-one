"""A click that re-lays out a populated canvas on its own page is no page load (follow-up of issue #11377).

The wait after a click on a graph's layout button holds until the canvases stand still, so its ``settled`` time is the
end of the animation the scene exists to show. Read as "a click opened a page that settled at T", every such scene was
refused ``scene_loading_span``. Proven here on the stored action log alone:

* a click that kept its address and switched no tab opens no page: its wait's settle is no loading span, and the scene
  trimmed over the animation saves clean;
* the same click whose wait logged a loading signal (a progress indicator, an empty canvas) is still flagged on that
  span;
* a click that navigated to a page showing a spinner then an empty canvas is still flagged, as in #11381.
"""

from __future__ import annotations

from app.services.video_studio import page_settle, quality, spec, timeline

TAKE_ID = "00000000-0000-4000-8000-000000011378"
GRAPH = "https://app/graph/1"
BAR = "span.MuiCircularProgress-root[progressbar]"


def _relayout(loading: list | None = None) -> list[dict]:
    wait: dict = {
        "step": 2,
        "action": "wait",
        "t0": 1.7,
        "ta": 1.7,
        "t1": 3.6,
        "until_settled": True,
        "settled": {"at": 3.4, "after": 1.7, "seconds": 1.0},
    }
    if loading is not None:
        wait["loading"] = loading
    return [
        {"step": 0, "action": "goto", "t0": 0.0, "ta": 0.0, "t1": 1.0, "url": GRAPH},
        {
            "step": 1,
            "action": "click",
            "label": "Horizontal layout",
            "t0": 1.0,
            "ta": 1.5,
            "t1": 1.6,
            "url": GRAPH,
            "url_before": GRAPH,
        },
        wait,
        {"step": 3, "action": "pause", "t0": 3.6, "ta": 3.6, "t1": 8.0},
    ]


NAVIGATES = [
    {"step": 0, "action": "goto", "t0": 0.0, "ta": 0.0, "t1": 1.0, "url": "https://app/list"},
    {
        "step": 1,
        "action": "click",
        "t0": 1.0,
        "ta": 1.5,
        "t1": 1.6,
        "url": GRAPH,
        "url_before": "https://app/list",
    },
    {
        "step": 2,
        "action": "wait",
        "t0": 1.7,
        "ta": 1.7,
        "t1": 4.4,
        "until_settled": True,
        "settled": {"at": 4.2, "after": 2.5, "seconds": 2.0},
        "loading": [[1.7, 2.4, [BAR]], [2.4, 3.0, ["canvas (empty)"]]],
    },
    {"step": 3, "action": "pause", "t0": 4.4, "ta": 4.4, "t1": 8.0},
]


def _findings(events: list[dict], scene: dict | None = None) -> list:
    authored = spec.validate_video_spec(
        {
            "title": "Graph",
            "freestyle": True,
            "scenes": [
                {
                    "id": "demo",
                    "type": "recording",
                    "take_id": TAKE_ID,
                    "idle": {"mode": "off"},
                    "trim_start": 1.0,
                    **(scene or {}),
                }
            ],
        }
    )
    parsed = spec.parse_video_spec(authored)
    document = timeline.estimate_plan(parsed, {}, {}).document
    take = timeline.TakeMedia(TAKE_ID, 8.0, 1600, 900, 1.0, 60, 480, events=events, captured_frames=480)
    found = quality.content_findings(parsed, document, quality.ContentFacts(takes={TAKE_ID: take}), authored)
    return [f for f in found if f.kind == "scene_loading_span"]


def test_a_click_that_kept_its_address_opened_no_page():
    assert page_settle.opened_page(_relayout()[1]) is False
    assert page_settle.opened_page(NAVIGATES[1]) is True
    assert page_settle.opened_page({**_relayout()[1], "tab": True}) is True
    assert page_settle.opened_page({"action": "goto", "url": GRAPH}) is True
    assert page_settle.opened_page({**NAVIGATES[1], "ok": False}) is False


def test_a_canvas_relaid_out_with_no_loading_signal_is_no_loading_span():
    assert page_settle.loading_spans(_relayout()) == []
    assert _findings(_relayout()) == []


def test_a_layout_kept_in_the_address_query_is_no_page_load_either():
    events = _relayout()
    events[1] = {**events[1], "url": f"{GRAPH}?layout=radial"}
    assert page_settle.loading_spans(events) == []
    assert _findings(events) == []
    shown = _relayout([[1.7, 2.6, [BAR]]])
    shown[1] = {**shown[1], "url": f"{GRAPH}?layout=radial"}
    assert [(s.start, s.end) for s in page_settle.loading_spans(shown)] == [(1.5, 3.4), (1.7, 2.6)]


def test_the_same_click_whose_wait_showed_a_loading_signal_is_still_flagged_on_it():
    spans = page_settle.loading_spans(_relayout([[1.7, 2.6, [BAR]]]))
    assert [(s.start, s.end) for s in spans] == [(1.7, 2.6)]
    [finding] = _findings(_relayout([[1.7, 2.6, [BAR]]]))
    assert f"step 2 (wait) showed {BAR} (1.7-2.6 s)" in finding.message
    assert "opened a page" not in finding.message


def test_a_click_that_navigated_to_a_spinner_then_an_empty_canvas_is_still_flagged():
    [finding] = _findings(NAVIGATES)
    assert "step 1 (click) opened a page that settled at 4.2 s (1.5-4.2 s)" in finding.message
    assert finding.data["trim_start"] == 4.2


def test_a_wait_after_an_in_page_click_holds_until_settled_only_after_a_page_opened():
    wait = type("Step", (), {"action": "wait", "until_settled": None})()
    assert page_settle.until_settled(wait, _relayout()[:2]) is False
    assert page_settle.until_settled(wait, NAVIGATES[:2]) is True
