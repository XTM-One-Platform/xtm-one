"""get_video_recording(window=...) answers a take's raw capture records over a window of its seconds (#9590).

The ghosting of a static toolbar across a canvas cut (take 9262b803, 30.7-31.7 s) could not be traced: the summary lines
count the canvas copies captured alone, the paced scrolls and zooms, the transitions and the frames dropped, and no
tool read the records themselves. ``take_window.window_records`` keeps those whose span meets the window, as stored.
"""

from __future__ import annotations

import json

from app.services.video_studio import take_window

CAPTURE = {
    "alone": {
        "runs": [
            {"t": [5.0, 6.2], "step": 6, "canvas": "canvas.clickable", "composited": True},
            {
                "t": [30.89, 31.5],
                "step": 15,
                "canvas": "#relation-canvas",
                "composited": False,
                "why": "copy unlike paint",
            },
        ]
    },
    "off_scale_dropped": [
        {"t": 31.2, "frames": 3, "step": 15, "why": "deviceWidth 1794 for 1920"},
        {"t": 50.0, "frames": 1},
    ],
}
EVENTS = [
    {
        "step": 13,
        "action": "wheel",
        "ta": 26.2,
        "t1": 27.0,
        "scrolls": [{"t": [26.2, 31.0], "scroller": "canvas", "riding": 9, "log": [[0, 1]] * 100}],
    },
    {"step": 14, "action": "pause", "ta": 29.47, "t1": 30.89},
    {
        "step": 15,
        "action": "click",
        "label": "focus",
        "ta": 30.89,
        "t1": 33.0,
        "motion": [{"t": [30.9, 31.6], "paced": False}],
    },
    {"step": 20, "action": "click", "ta": 45.0, "t1": 46.0, "scrolls": [{"t": [45.0, 45.5], "scroller": "window"}]},
]


def test_window_is_parsed_and_refused_when_it_is_not_start_end():
    assert take_window.parse_window("30.5-32") == (30.5, 32.0)
    assert take_window.parse_window("30.5..32") == (30.5, 32.0)
    assert "not START-END" in take_window.parse_window("thirty")
    assert "ends before it starts" in take_window.parse_window("32-30")


def test_records_meeting_the_window_are_kept_as_stored_and_the_others_left_out():
    found = take_window.window_records(CAPTURE, EVENTS, 30.7, 31.7)
    assert [s["step"] for s in found["steps"]] == [14, 15]
    assert [r["step"] for r in found["alone_runs"]] == [15]
    assert found["alone_runs"][0]["why"] == "copy unlike paint"
    # A zoom started at step 13 still riding into the window is kept, its long log said by its length.
    assert found["scrolls"] == [{"step": 13, "t": [26.2, 31.0], "scroller": "canvas", "riding": 9, "log": "100 items"}]
    assert found["motion"] == [{"step": 15, "t": [30.9, 31.6], "paced": False}]
    assert [d["t"] for d in found["off_scale_dropped"]] == [31.2]
    assert "cut" not in found
    json.dumps(found)


def test_a_take_without_records_answers_empty_lists():
    found = take_window.window_records(None, None, 0, 10)
    assert found == {
        "window": [0, 10],
        "steps": [],
        "alone_runs": [],
        "scrolls": [],
        "motion": [],
        "off_scale_dropped": [],
        "held_flashes": [],
        # Said on every take, [] and 0 when none (#10970).
        "skipped_outside_runs": [],
        "skipped_total": 0,
        "skipped_total_take": 0,
    }


def test_a_window_over_many_records_is_cut_and_the_rest_counted():
    capture = {"alone": {"runs": [{"t": [i, i + 0.5]} for i in range(take_window.MOST_RECORDS + 5)]}}
    found = take_window.window_records(capture, [], 0, 1000)
    assert len(found["alone_runs"]) == take_window.MOST_RECORDS
    assert found["cut"] == {"alone_runs": 5}


# ── The take's frame list (item dev:recording-window-keyframe-list) ─────────────────────


def test_frame_listing_names_each_frame_kept_or_dropped_with_the_test_that_dropped_it():
    before = [
        (6.40, "f000100.jpg"),
        (6.45, "a000003.jpg"),
        (6.50, "f000101.jpg"),
        (6.55, "z000004.jpg"),
        (6.60, "f000102.jpg"),
    ]
    kept = [before[0], before[2], before[4]]
    flashes = [
        {"t": [6.45, 6.5], "frames": 1, "test": "gate replay", "speed": 6.0},
        {"t": [6.55, 6.6], "frames": 1},
    ]
    listing = take_window.frame_listing(before, kept, flashes)
    assert listing["version"] == take_window.FRAME_LIST_VERSION
    assert listing["frames"] == [
        [6.4, "f000100.jpg", None],
        [6.45, "a000003.jpg", "gate replay"],
        [6.5, "f000101.jpg", None],
        [6.55, "z000004.jpg", "hold flash"],
        [6.6, "f000102.jpg", None],
    ]
    json.dumps(listing)


def test_window_frames_say_time_name_stage_and_why_a_frame_is_gone():
    listing = {
        "frames": [
            [6.3, "f000099.jpg", None],
            [6.4, "f000100.jpg", None],
            [6.45, "a000003.jpg", "gate replay"],
            [6.5, "p000007.jpg", None],
            [6.9, "f000110.jpg", None],
        ]
    }
    frames, cut = take_window.window_frames(listing, 6.4, 6.8)
    assert cut == 0
    assert frames == [
        {"t": 6.4, "name": "f000100.jpg", "stage": "screencast", "kept": True},
        {"t": 6.45, "name": "a000003.jpg", "stage": "canvas composite", "kept": False, "why": "gate replay"},
        {"t": 6.5, "name": "p000007.jpg", "stage": "scroll pacing", "kept": True},
    ]


def test_window_frames_over_the_most_are_cut_and_the_rest_counted():
    listing = {"frames": [[i / 100, f"f{i:06d}.jpg", None] for i in range(take_window.MOST_FRAMES + 7)]}
    frames, cut = take_window.window_frames(listing, 0, 1000)
    assert len(frames) == take_window.MOST_FRAMES
    assert cut == 7


def test_frame_list_file_is_read_from_the_capture_and_owned_with_the_take():
    assert take_window.frame_list_file({take_window.FRAME_LIST_KEY: "abc"}) == "abc"
    assert take_window.frame_list_file(None) is None
    assert take_window.frame_list_file({}) is None
    from app.services.video_studio.take_repair import MEASURED_FIELDS

    # A repair measures again and stores its own list: the source's is never carried over.
    assert take_window.FRAME_LIST_KEY in MEASURED_FIELDS
