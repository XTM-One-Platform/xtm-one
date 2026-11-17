"""Stepping motion graded as each scene draws it.

On synthetic takes, no browser:

* a 1.0 s canvas transition painted at 12 fps (49 of 61 frames at 60 fps synthesised), no keyframe held past 0.1 s,
  is an advisory at speed 1.0: the audience reads it as motion;
* a stretch holding one keyframe 0.4 s stays dense;
* at speed 0.75 a stretch steps whether or not the take kept its painted instants (``sparse``): the compositor shows
  one take frame per output frame, never two blended, and the finding never proposes slowing the scene;
* three recording scenes of one take, each showing a stepping stretch, are each said in one check;
* a 60 fps take drawn on software GL says, in its answer, that its canvas transitions will step and that fps 30 is the
  fallback.
"""

from __future__ import annotations

import pytest

from app.services.video_studio import capture_rate, timeline
from app.services.video_studio.quality import ContentFacts, content_findings
from app.services.video_studio.spec import authored_spec, parse_video_spec

pytestmark = pytest.mark.usefixtures("no_default_video_template")

TAKE = "00000000-0000-0000-0000-000000006882"
#: A 1.0 s transition at 60 fps: 61 frames, 12 of them painted, the rest synthesised from keyframes.
BRIEF = {"t": [1.0, 2.0], "frames": 61, "synthesised": 49, "fps": 60, "step": 1, "canvas": "canvas#graph", "hold": 0.1}


def _take(*stretches: dict, sparse: list | None = None) -> timeline.TakeMedia:
    events = [
        {"step": 0, "action": "click", "t0": 0.5, "ta": 0.9, "t1": 1.0, "ok": True, "box": {"x": 100, "y": 100, "w": 80, "h": 30}},
        {"step": 1, "action": "wait", "t0": 1.0, "ta": 1.0, "t1": 7.5, "ok": True},
    ]
    take = timeline.TakeMedia(TAKE, 8.0, 1600, 900, 1.5, 60, 480, events=events)
    take.stepping = [{**stretch, "share": round(stretch["synthesised"] / stretch["frames"], 3), "gl": "software"} for stretch in stretches]
    take.sparse = list(sparse or [])
    return take


def _stepping(take: timeline.TakeMedia, *scenes: dict) -> list:
    authored = authored_spec(
        {
            "title": "Graph",
            "freestyle": True,
            "scenes": [{"type": "recording", "take_id": TAKE, "idle": {"mode": "off"}, **scene} for scene in scenes],
        }
    )
    spec = parse_video_spec(authored)
    plan = timeline.estimate_plan(spec, {TAKE: take})
    facts = ContentFacts(takes={TAKE: take}, ready_takes={TAKE})
    return [f for f in content_findings(spec, plan.document, facts, authored) if f.kind == "stepping_motion"]


def test_a_brief_transition_painted_at_12_fps_is_an_advisory_at_speed_1():
    [found] = _stepping(_take(BRIEF), {"id": "r"})
    assert found.severity == "advisory" and found.scene_id == "r"
    assert "stepping motion at step 1 (canvas#graph moved 1.0 s: 49 of its 61 frames at 60 fps synthesised" in found.message
    assert "painted at 12 fps over 1.0 s on screen, a keyframe held 0.10 s at most" in found.message
    grade = capture_rate.stepping_grade(BRIEF)
    assert grade is not None and grade.severity == "advisory"


def test_a_keyframe_held_0_4_s_stays_dense():
    held = {**BRIEF, "hold": 0.4}
    [found] = _stepping(_take(held), {"id": "r"})
    assert found.severity == "dense"
    assert "more than half of those frames hold the keyframe before" in found.message
    assert "or record it at fps 30 (fps_fallback: fps)" in found.message
    # A stretch kept before its hold was measured is held for its mean painted interval: 1/12 s here.
    old = {key: value for key, value in BRIEF.items() if key != "hold"}
    assert capture_rate.stepping_grade(old).severity == "advisory"
    assert capture_rate.stepping_grade({**old, "t": [1.0, 3.0], "frames": 121, "synthesised": 97}).severity == "dense"


def test_slowing_a_scene_never_hides_its_stepping_nor_is_offered_as_a_fix():
    kept = [[0.9, 2.1, [round(0.9 + n / 12, 4) for n in range(15)]]]
    # Slowed, kept painted instants or not: one take frame per output frame, graded as drawn - 9 painted
    # frames a second of output - and slowing never offered.
    for take in (_take(BRIEF, sparse=kept), _take(BRIEF)):
        [found] = _stepping(take, {"id": "r", "speed": 0.75})
        assert found.severity == "dense"
        assert "or slow the scene" not in found.message
        assert "slowing the scene would not smooth it: a slowed scene shows one painted frame at a time" in found.message
    # At speed 1 over painted instants kept, a dense stretch no longer offers a slowdown that would blend it.
    [held] = _stepping(_take({**BRIEF, "hold": 0.4}, sparse=kept), {"id": "r"})
    assert held.severity == "dense" and "or slow the scene" not in held.message


def test_one_check_says_every_scene_showing_stepping_motion():
    stretches = [
        {**BRIEF, "t": [1.0, 1.6], "frames": 37, "synthesised": 30, "hold": 0.4, "step": 1},
        {**BRIEF, "t": [3.0, 3.6], "frames": 37, "synthesised": 30, "hold": 0.4, "step": 1},
        {**BRIEF, "t": [5.0, 5.6], "frames": 37, "synthesised": 30, "hold": 0.4, "step": 1},
    ]
    found = _stepping(
        _take(*stretches),
        {"id": "a", "trim_start": 0.5, "trim_end": 2.5},
        {"id": "b", "trim_start": 2.5, "trim_end": 4.5},
        {"id": "c", "trim_start": 4.5, "trim_end": 6.5},
    )
    assert [(f.scene_id, f.severity) for f in found] == [("a", "dense"), ("b", "dense"), ("c", "dense")]
    assert "moved 0.6 s" in found[1].message


def test_a_60_fps_take_on_software_gl_says_its_canvases_will_step_and_offers_fps_30():
    notice = capture_rate.software_gl_notice(60, {"gl": {"gpu": False}})
    assert notice.startswith("This deployment records canvases on software GL: 60 fps canvas transitions will step")
    assert "Record at fps 30 instead - or with fps_fallback: fps" in notice and "VIDEO_STUDIO_RECORDING_GL" in notice
    assert capture_rate.software_gl_notice(30, {"gl": {"gpu": False}}) == ""
    assert capture_rate.software_gl_notice(60, {"gl": {"gpu": True}}) == ""
    # A take recorded before its GL was kept says nothing.
    assert capture_rate.software_gl_notice(60, {}) == ""


def test_a_canvas_transition_keeps_its_longest_held_keyframe():
    run = capture_rate.CanvasRun("canvas#graph", 1.0, 2.0, 13, 1, hold=0.1)
    [stretch] = capture_rate.stepping([], [], [run], fps=60, workdir="")
    assert stretch.hold == 0.1 and stretch.as_dict()["hold"] == 0.1
    assert "hold" not in capture_rate.Stepping(1.0, 2.0, 61, 49, 60, 1).as_dict()
