"""A take's capture findings are judged over the seconds of the take a scene plays (issue #10113).

A screen insert whose source is a recording comes in on its ``trim_start`` and plays the take for its
``duration``, then holds its last frame: a stretch the take captured too slowly before that start, or after
that end, is never on screen and never refuses the video. One inside the window still does. A recording scene
already judged its own trim; it is pinned here with a ``trim_start`` too.
"""

from __future__ import annotations

import pytest

from app.services.video_studio import quality, timeline
from app.services.video_studio.spec import parse_video_spec, validate_video_spec

TAKE = "91be7803-0000-4000-8000-000000010113"
QUAD = [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]


def _take(low: list[float]) -> timeline.TakeMedia:
    events = [
        {"step": 0, "action": "wait", "t0": 0.0, "ta": 0.0, "t1": 10.0, "ok": True},
        {"step": 1, "action": "click", "t0": 10.0, "ta": 10.2, "t1": 10.3, "ok": True},
        {"step": 2, "action": "wait", "t0": 10.3, "ta": 10.3, "t1": 19.5, "ok": True},
    ]
    take = timeline.TakeMedia(TAKE, 20.0, 1600, 900, 2.0, 60, 1200, events=events)
    take.low = [{"t": low, "fps": 9.0}]
    return take


def _insert_spec(trim_start: float) -> tuple:
    insert = {
        "type": "screen_insert",
        "source": {"take_id": TAKE},
        "quad": QUAD,
        "duration": 12.0,
        "trim_start": trim_start,
    }
    scenes = [{"id": "noise", "type": "clip", "clip_id": "00000000-0000-4000-8000-00000000c11b", "overlays": [insert]}]
    authored = validate_video_spec({"title": "Insert", "freestyle": True, "scenes": scenes})
    spec = parse_video_spec(authored)
    return spec, timeline.estimate_plan(spec, {}, {}).document, authored


def _recording_spec(take: timeline.TakeMedia, trim_start: float) -> tuple:
    scenes = [{"id": "demo", "type": "recording", "take_id": TAKE, "trim_start": trim_start}]
    authored = validate_video_spec({"title": "Recording", "freestyle": True, "scenes": scenes})
    spec = parse_video_spec(authored)
    return spec, timeline.estimate_plan(spec, {TAKE: take}).document, authored


def _capture_rate(spec, document, authored, take) -> list:
    facts = quality.ContentFacts(takes={TAKE: take}, ready_takes={TAKE})
    return [f for f in quality.content_findings(spec, document, facts, authored) if f.kind == "take_capture_rate"]


@pytest.mark.parametrize(("low", "said"), [([0.3, 1.7], False), ([6.0, 7.4], True), ([16.5, 17.9], False)])
def test_a_screen_insert_is_judged_over_the_seconds_of_the_take_it_plays(low, said):
    # Trimmed to 4.0 s for 12 s: it plays 4.0-16.0 s of the take, then holds its last frame.
    spec, document, authored = _insert_spec(4.0)
    found = _capture_rate(spec, document, authored, _take(low))
    assert bool(found) is said
    if said:
        [finding] = found
        assert finding.severity == "dense" and finding.scene_id == "noise"


def test_an_untrimmed_screen_insert_still_shows_the_take_from_its_start():
    spec, document, authored = _insert_spec(0.0)
    [finding] = _capture_rate(spec, document, authored, _take([0.3, 1.7]))
    assert finding.severity == "dense"


def test_the_window_an_insert_plays_is_read_from_its_identity():
    spec, _document, _authored = _insert_spec(4.0)
    [(_scene, _take_id, _where, identity)] = quality._shown_takes(spec)
    assert quality._insert_window(spec, identity) == (4.0, 16.0)
    assert quality._insert_window(spec, "insert/missing.0|take:x") is None


@pytest.mark.parametrize(("low", "said"), [([0.3, 1.7], False), ([6.0, 7.4], True)])
def test_a_recording_scene_trimmed_at_its_start_is_judged_over_what_it_keeps(low, said):
    take = _take(low)
    spec, document, authored = _recording_spec(take, 4.0)
    assert bool(_capture_rate(spec, document, authored, take)) is said
