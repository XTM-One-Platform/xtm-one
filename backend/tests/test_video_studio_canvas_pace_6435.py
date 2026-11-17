"""The Video Studio harness gaps of the OpenCTI graph demo, on fixtures: no browser, no database.

* a pointer step acts on a point or a node of a canvas: validated, estimated,
  logged with the box the camera frames, a missing node naming the closest
  labels;
* a take's capture: its stretches animated under 20 fps found and said
  (``low capture rate at step N``), never a keystroke's repaint nor a scroll;
  the warm-up's decision; the sparse stretches a slowed scene blends;
* a dynamic pace by construction: its presets under the author's choices and
  over the template's, a recording trimmed after its last step and sped up to
  its narration, the pace advisory and its still screen;
* ``frame.fill`` drawn edge to edge, left open by a template's frame lock;
* a scene's own narration speed and the voice's style presets.

The Chromium half (a canvas clicked, right-clicked, dragged and wheeled; an
animation captured) is ``test_video_studio_canvas_6435_chromium.py``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from app.services.video_studio import (
    capture,
    capture_rate,
    pace,
    pointer,
    recording_fit,
    take_preflight,
    templates,
    timeline,
    voice,
)
from app.services.video_studio.quality import ContentFacts, content_findings
from app.services.video_studio.spec import (
    VideoSpecError,
    authored_spec,
    parse_take_script,
    parse_video_spec,
    validate_take_script,
)

TAKE = "00000000-0000-0000-0000-000000006435"


# ── 1. A pointer step on a canvas ─────────────────────────────────────────


def test_pointer_steps_take_a_point_a_node_a_release_and_keys():
    script = parse_take_script(
        validate_take_script(
            {
                "steps": [
                    {"action": "click", "node": {"label": "Copper Lantern"}},
                    {"action": "right_click", "node": {"label": "Copper Lantern"}},
                    {"action": "click", "node": {"label": "APT-X"}, "modifiers": ["Control"]},
                    {"action": "drag", "at": {"x": 0.2, "y": 0.2, "in": "canvas"}, "to": {"x": 0.6, "y": 0.6, "in": "canvas"}},
                    {"action": "drag", "node": {"label": "A"}, "to": {"node": {"label": "B"}}},
                    {"action": "wheel", "by": -480, "at": {"x": 800, "y": 450}},
                    {"action": "hover", "at": {"x": 10, "y": 10}, "hover_ms": 600},
                    {"action": "scroll", "to": "bottom"},
                ]
            }
        )
    )
    click, right, ctrl, drag, node_drag, wheel, hover, scroll = script.steps
    assert click.on_canvas and click.node.label == "Copper Lantern"
    assert ctrl.modifiers == ["Control"]
    assert drag.at.within == "canvas" and drag.to.x == 0.6
    assert node_drag.to.node.label == "B"
    assert wheel.by == -480 and hover.hover_ms == 600 and scroll.to == "bottom"
    # Stored with the alias it was written with: a script read back validates again.
    stored = validate_take_script(script.model_dump(mode="json", exclude_none=True))
    assert stored["steps"][3]["at"] == {"x": 0.2, "y": 0.2, "in": "canvas"}
    assert capture.drives_pointer(click) and capture.drives_pointer(right) and not capture.drives_pointer(scroll)
    assert capture.step_label(click) == "Copper Lantern" and capture.step_label(drag) == "(0.2, 0.2) in canvas"


@pytest.mark.parametrize(
    ("step", "words"),
    [
        ({"action": "drag", "node": {"label": "A"}}, "drag needs where it releases"),
        ({"action": "type", "text": "x", "at": {"x": 1, "y": 1}}, "at and node are the target of a pointer action"),
        ({"action": "click", "at": {"x": 1700, "y": 10}}, "lies outside the 1600x900 window"),
        ({"action": "wheel"}, "wheel needs by"),
        ({"action": "scroll", "to": {"x": 1, "y": 1}}, "a scroll goes to top or bottom"),
        ({"action": "click", "selector": "#a", "node": {"label": "A"}}, "acts on one target"),
        ({"action": "right_click"}, "a point (at) or a node"),
        ({"action": "press", "key": "p", "modifiers": ["Control"]}, "hover_ms and modifiers go with a pointer action"),
    ],
)
def test_a_pointer_step_without_its_target_or_off_the_window_is_refused(step, words):
    with pytest.raises(VideoSpecError) as refused:
        validate_take_script({"steps": [step]})
    assert words in str(refused.value)


def test_a_point_on_an_element_reads_shares_up_to_one_and_pixels_past_it():
    assert pointer.placed_in(0.5, 100.0, 1200.0) == 700.0
    assert pointer.placed_in(1.0, 100.0, 1200.0) == 1300.0
    assert pointer.placed_in(40.0, 100.0, 1200.0) == 140.0
    # The camera frames a point as a box around it, kept inside the window.
    assert pointer.point_box(5.0, 890.0, (1600, 900)) == {"x": 0.0, "y": 844.0, "width": 56.0, "height": 56.0}


def test_a_missing_node_names_the_three_closest_labels():
    near = pointer.closest_labels("Copper Lanter", ["APT-X", "Copper Lantern", "Lantern Hub", "Kettle", "Amber"])
    assert [entry["name"] for entry in near][:2] == ["Copper Lantern", "Lantern Hub"] and len(near) == 3
    # Nothing alike: the graph's labels all the same, three of them.
    assert [entry["name"] for entry in pointer.closest_labels("Z", ["A", "B", "C", "D", "E"])] == ["A", "B", "C"]
    assert capture.names_line(near[:1]) == '"Copper Lantern" (node)'


def test_the_estimate_counts_a_pointer_steps_rest_drag_wheel_and_menu():
    script = parse_take_script(
        validate_take_script(
            {
                "settle_ms": 0,
                "steps": [
                    {"action": "click", "selector": "#plain"},
                    {"action": "click", "node": {"label": "B"}},
                    {"action": "drag", "node": {"label": "A"}, "to": {"node": {"label": "B"}}, "seconds": 1.5},
                    {"action": "wheel", "by": -600, "seconds": 2.0},
                    {"action": "right_click", "node": {"label": "C"}},
                ],
            }
        )
    )
    plain, node, drag, wheel, right = (take_preflight.step_seconds(step, script) for step in script.steps)
    assert node == pytest.approx(plain + pointer.HOVER_MS / 1000)
    assert drag == pytest.approx(plain + pointer.HOVER_MS / 1000 + 1.5)
    assert wheel == pytest.approx(plain + 2.0)
    assert right == pytest.approx(plain + pointer.HOVER_MS / 1000 + take_preflight.MENU_SECONDS)


def _clicked_node_take() -> timeline.TakeMedia:
    """A take whose step 1 clicks node B of a graph (its box as the recorder logs it) at 4 s."""
    events = [
        {"step": 0, "action": "wait", "t0": 0.0, "ta": 0.0, "t1": 2.0, "ok": True},
        {
            "step": 1,
            "action": "click",
            "t0": 3.2,
            "ta": 4.0,
            "t1": 4.1,
            "ok": True,
            "box": {"x": 1176.0, "y": 276.0, "w": 48.0, "h": 48.0},
            "point": {"x": 1200.0, "y": 300.0},
            "target": {"node": "B"},
        },
        {"step": 2, "action": "wait", "t0": 4.2, "ta": 4.2, "t1": 8.0, "ok": True},
    ]
    return timeline.TakeMedia(TAKE, 9.0, 1600, 900, 2.0, 60, 540, events=events, cursor=[(0.0, 960.0, 558.0), (4.0, 1200.0, 300.0)])


def test_the_camera_zooms_toward_the_node_clicked_at_its_step():
    take = _clicked_node_take()
    time_map = timeline.build_time_map(
        duration=take.duration, trim_start=0.0, trim_end=None, speed=1.0, idle_mode="off", idle_threshold=1.2,
        idle_speed=6.0, events=take.events,
    )
    for action in ("click", "right_click", "drag"):
        take.events[1]["action"] = action
        camera = timeline.plan_camera(
            mode="auto", max_zoom=1.8, zoom_on=["click"], keyframes=[], media=take, time_map=time_map, trim_start=0.0,
            out_duration=time_map[-1][0],
        )
        zoomed = [frame for frame in camera if frame[3] > 1.2]
        assert zoomed, f"{action}: the camera never zoomed"
        t, cx, cy, _zoom = zoomed[0][:4]
        assert t <= 4.0 + 0.5 and abs(cx - 1200 / 1600) < 0.12 and abs(cy - 300 / 900) < 0.2, (action, zoomed[0])
    # A right-click and a drag ripple where they press, as a click does.
    _samples, clicks = timeline.plan_cursor(take, time_map, time_map[-1][0])
    assert clicks and clicks[0][1] == pytest.approx(0.75)


def test_a_wheel_keeps_the_camera_wide_while_its_canvas_zooms():
    take = _clicked_node_take()
    take.events[1].update(action="wheel")
    time_map = timeline.build_time_map(
        duration=take.duration, trim_start=0.0, trim_end=None, speed=1.0, idle_mode="off", idle_threshold=1.2,
        idle_speed=6.0, events=take.events,
    )
    camera = timeline.plan_camera(
        mode="auto", max_zoom=1.8, zoom_on=["click", "hover"], keyframes=[], media=take, time_map=time_map,
        trim_start=0.0, out_duration=time_map[-1][0],
    )
    assert all(frame[3] == 1.0 for frame in camera)


# ── 2. A take's capture ───────────────────────────────────────────────────


def _frames(workdir: Path, times: list[float], *, moving: bool) -> list[tuple[float, str]]:
    """JPEG frames at *times*: a large block crossing the frame (an animation), or one pixel row changing (a caret)."""
    out = []
    for index, t in enumerate(times):
        picture = np.full((450, 800, 3), 12, dtype=np.uint8)
        if moving:
            left = (index * 37) % 600
            picture[100:350, left : left + 200] = (220, 230, 240)
        else:
            picture[200, 100 + index % 3] = 255
        name = f"f{index:05d}.jpg"
        Image.fromarray(picture).save(workdir / name, quality=92)
        out.append((round(t, 4), name))
    return out


EVENTS = [
    {"step": 0, "action": "wait", "t0": 0.0, "ta": 0.0, "t1": 1.0, "ok": True},
    {"step": 1, "action": "click", "label": "Layered layout", "t0": 1.0, "ta": 1.1, "t1": 1.2, "ok": True},
    {"step": 2, "action": "wait", "t0": 4.0, "ta": 4.0, "t1": 5.0, "ok": True},
]


def test_an_animation_captured_at_12_fps_is_said_at_its_step(tmp_path):
    times = [1.1 + i / 12 for i in range(29)]
    frames = _frames(tmp_path, times, moving=True)
    captured = capture_rate.measure(frames, [dict(e) for e in EVENTS], duration=5.0, fps=30, workdir=str(tmp_path))

    [low] = captured["low"]
    assert low["step"] == 1 and low["fps"] == pytest.approx(12.0, abs=0.5)
    assert capture_rate.low_words(captured) == ["low capture rate at step 1 (12 fps for 2.3 s)"]
    assert captured["rates"][1:4] == [11, 12, 4] or sum(captured["rates"]) == len(frames)
    # The stretch keeps its painted instants, from the take video's first frame: what a slowed scene blends.
    [[start, end, instants]] = captured["sparse"]
    assert start == 0.0 and end == pytest.approx(times[-1] - times[0], abs=1e-3) and len(instants) == len(frames)


def test_a_keystroke_repaint_or_a_scroll_is_never_a_low_capture_rate(tmp_path):
    times = [1.1 + i / 12 for i in range(30)]
    typed = _frames(tmp_path, times, moving=False)
    assert capture_rate.low_spans(typed, [dict(e) for e in EVENTS], str(tmp_path)) == []
    scrolled = [dict(e) for e in EVENTS]
    scrolled[1] = {**scrolled[1], "action": "scroll", "scrolls": [{"t": [1.0, 4.0], "samples": []}]}
    moving = _frames(tmp_path, times, moving=True)
    assert capture_rate.low_spans(moving, scrolled, str(tmp_path)) == []


def test_each_step_logs_its_rate_while_its_page_painted():
    events = [dict(e) for e in EVENTS]
    times = [1.1 + i / 30 for i in range(60)] + [4.5]
    capture_rate.step_rates(events, times)
    assert "capture_fps" not in events[0] and events[1]["capture_fps"] == pytest.approx(30.0, abs=0.5)
    assert capture_rate.per_second(times, 5.0) == [0, 27, 30, 3, 1]


def _script(pixel_ratio: float = 2, fps: int = 30) -> SimpleNamespace:
    return SimpleNamespace(
        pixel_ratio=pixel_ratio, fps=fps, fps_fallback="refuse", browser_zoom=None, viewport=SimpleNamespace(width=1600, height=900)
    )


def test_the_warm_up_keeps_lowers_or_refuses():
    kept = capture_rate.decide(_script(), (1600, 900), 2.0, 31.0, None)
    assert kept.pixel_ratio == 2 and kept.note is None and kept.refused is None

    lowered = capture_rate.decide(_script(), (1600, 900), 2.0, 17.0, 31.0)
    assert lowered.pixel_ratio == 1.5 and lowered.ratio == 1.5 and lowered.refused is None
    assert "captured 3200x1800 frames at 17 fps" in lowered.note and "pixel ratio 1.5 (2400x1350, 31 fps" in lowered.note
    assert lowered.record()["pixel_ratio"] == {"asked": 2.0, "recorded": 1.5}

    # The window that would is the one measured (test_video_studio_take_state_6563.py): none found, none named.
    refused = capture_rate.decide(_script(), (1600, 900), 2.0, 4.7, 9.0, shape=(4, 0))
    assert refused.refused.startswith("Refused before recording: this worker captures 3200x1800 frames at 5 fps")
    assert "no window a recording takes sustains 30 fps on this worker (4 CPUs), even at pixel ratio 1" in refused.refused

    # A browser zoom lays the page out narrower at a higher ratio: lowered, it keeps its zoom.
    zoomed = capture_rate.decide(_script(), (842, 474), 3.8, 17.0, 31.0)
    assert zoomed.ratio == pytest.approx(1.5 * 1.9)


def test_a_slowed_scene_plans_the_painted_instants_of_a_sparse_stretch_never_a_blend():
    take = timeline.TakeMedia(TAKE, 6.0, 1600, 900, 2.0, 60, 360, sparse=[[1.0, 2.5, [1.0, 1.2, 1.4, 2.5]]])
    for speed, slowed in ((0.5, True), (1.0, False)):
        authored = authored_spec(
            {"title": "Pick", "freestyle": True, "scenes": [{"id": "r", "type": "recording", "take_id": TAKE, "speed": speed, "fit": "off"}]}
        )
        planned = timeline.estimate_plan(parse_video_spec(authored), {TAKE: take}).document["scenes"][0]
        # The compositor picks the nearest painted instant; nothing is planned to cross-dissolve.
        assert "blend" not in planned
        assert ("painted" in planned) is slowed
        if slowed:
            assert planned["painted"] == take.sparse


# ── 3. A dynamic pace ─────────────────────────────────────────────────────


def _slow_take(take_id: str) -> timeline.TakeMedia:
    """A click, a 6 s wait for the page, a click, then the 1.2 s end hold the recorder kept."""
    events = [
        {"step": 0, "action": "click", "t0": 0.5, "ta": 1.0, "t1": 1.1, "ok": True, "box": {"x": 100, "y": 100, "w": 80, "h": 30}},
        {"step": 1, "action": "wait", "t0": 1.2, "ta": 1.2, "t1": 7.2, "ok": True},
        {"step": 2, "action": "click", "t0": 7.3, "ta": 7.8, "t1": 7.9, "ok": True, "box": {"x": 300, "y": 200, "w": 80, "h": 30}},
    ]
    return timeline.TakeMedia(take_id, 10.0, 1600, 900, 2.0, 60, 600, events=events)


def _demo(pace_name: str | None) -> tuple[dict, dict[str, timeline.TakeMedia]]:
    takes = {f"00000000-0000-0000-0000-00000000643{i}": _slow_take(f"00000000-0000-0000-0000-00000000643{i}") for i in range(5)}
    scenes = [
        {"id": f"s{i}", "type": "recording", "take_id": take_id, "narration": "Open the graph and lay it out by category."}
        for i, take_id in enumerate(takes)
    ]
    raw = {"title": "Pace", "freestyle": True, "scenes": scenes, **({"pace": pace_name} if pace_name else {})}
    return templates.apply_template(authored_spec(raw), None).spec, takes


def _saved(pace_name: str | None) -> tuple[timeline.TimelinePlan, list[recording_fit.Fit], object]:
    effective, takes = _demo(pace_name)
    spec = parse_video_spec(effective)
    plan = timeline.estimate_plan(spec, takes)
    found = recording_fit.fits(spec, plan.document, takes, plan.narration_spans)
    fitted = parse_video_spec(recording_fit.apply(effective, found))
    return timeline.estimate_plan(fitted, takes), found, fitted


def test_a_dynamic_demo_plays_a_fifth_shorter_than_the_defaults_and_reads_well():
    calm, calm_fits, _spec = _saved(None)
    dynamic, fits, spec = _saved("dynamic")

    assert calm_fits == []
    assert dynamic.duration <= 0.8 * calm.duration, (dynamic.duration, calm.duration)
    assert all(fit.speed <= pace.FASTEST_FIT_SPEED for fit in fits)
    effective, takes = _demo("dynamic")
    report = pace.report(dynamic.document, takes, dynamic.narration_spans, spec)
    assert report.flagged() == [] and report.summary().startswith("Pace: ")
    first = effective["scenes"][0]
    assert first["idle"]["mode"] == "cut" and first["camera"]["pace"] == "dynamic" and first["end_hold_ms"] == 300


def _busy_take(seconds: float) -> timeline.TakeMedia:
    """A take acting all along: a click every second for *seconds*."""
    events = [
        {"step": i, "action": "click", "t0": i + 0.1, "ta": i + 0.5, "t1": i + 0.6, "ok": True, "box": {"x": 100, "y": 100, "w": 80, "h": 30}}
        for i in range(int(seconds))
    ]
    return timeline.TakeMedia(TAKE, seconds + 1.2, 1600, 900, 2.0, 60, int((seconds + 1.2) * 60), events=events)


@pytest.mark.parametrize(("seconds", "clamped"), [(7.0, False), (12.0, True)])
def test_a_dynamic_video_speeds_a_recording_longer_than_its_narration_up_to_1_25(seconds, clamped):
    take = _busy_take(seconds)
    raw = {
        "title": "Fit",
        "freestyle": True,
        "pace": "dynamic",
        "scenes": [
            {
                "id": "demo",
                "type": "recording",
                "take_id": TAKE,
                "narration": "Open the graph, then lay it out by category in one click.",
            }
        ],
    }
    effective = templates.apply_template(authored_spec(raw), None).spec
    spec = parse_video_spec(effective)
    plan = timeline.estimate_plan(spec, {TAKE: take})
    [fit] = recording_fit.fits(spec, plan.document, {TAKE: take}, plan.narration_spans)
    assert 1.0 < fit.speed <= pace.FASTEST_FIT_SPEED and (fit.speed == pace.FASTEST_FIT_SPEED) is clamped
    words = fit.words()
    assert words.startswith(f"scene `demo`: speed set to {fit.speed:g} automatically")
    assert ("the fastest a fit plays in a dynamic video" in words) is clamped
    if not clamped:
        replanned = timeline.estimate_plan(parse_video_spec(recording_fit.apply(effective, [fit])), {TAKE: take})
        assert replanned.duration < plan.duration
    # A calm video keeps the recording's real time.
    calm = parse_video_spec(templates.apply_template(authored_spec({**raw, "pace": "calm"}), None).spec)
    calm_plan = timeline.estimate_plan(calm, {TAKE: take})
    assert recording_fit.fits(calm, calm_plan.document, {TAKE: take}, calm_plan.narration_spans) == []


def test_the_pace_presets_fill_only_what_the_author_and_the_template_leave():
    authored = authored_spec(
        {
            "title": "Pace",
            "pace": "dynamic",
            "default_transition": {"type": "crossfade"},
            "scenes": [
                {"id": "r", "type": "recording", "take_id": TAKE, "idle": {"mode": "off"}, "camera": {"move_seconds": 1.0}},
                {"id": "s", "type": "statement", "lines": ["Fast."], "reveal": "together", "transition": {"type": "auto"}},
            ],
        }
    )
    style = templates.VideoTemplateStyle.model_validate(
        {
            "defaults": {"default_transition": {"type": "auto", "pace": "calm"}},
            "scene_defaults": {"recording": {"idle": {"mode": "compress"}, "camera": {"pace": "calm"}}},
        }
    )
    effective = templates.apply_template(authored, templates.VideoTemplate(id="t", name="T", kind="video", style=style)).spec
    recording, statement = effective["scenes"]
    assert effective["default_transition"]["type"] == "crossfade"
    assert recording["idle"]["mode"] == "off" and recording["camera"] == {**recording["camera"], "pace": "dynamic", "move_seconds": 1.0}
    assert statement["reveal"] == "together" and statement["transition"] == {"type": "auto", "pace": "dynamic"}
    # A template presetting the pace brings it to a spec that names none.
    led = templates.VideoTemplateStyle.model_validate({"defaults": {"pace": "dynamic"}})
    bare = authored_spec({"title": "Pace", "scenes": [{"id": "r", "type": "recording", "take_id": TAKE}]})
    dressed = templates.apply_template(bare, templates.VideoTemplate(id="t", name="T", kind="video", style=led)).spec
    assert dressed["pace"] == "dynamic" and dressed["scenes"][0]["idle"]["mode"] == "cut"


def test_end_hold_ms_cuts_the_hold_the_recorder_kept_after_the_last_step():
    take = _slow_take(TAKE)
    scene = SimpleNamespace(trim_start=0.0, trim_end=None, end_hold_ms=300)
    assert timeline.kept_end(take, scene) == pytest.approx(8.2)
    assert timeline.kept_end(take, SimpleNamespace(trim_start=0.0, trim_end=6.0, end_hold_ms=300)) == 6.0
    assert timeline.kept_end(take, SimpleNamespace(trim_start=0.0, trim_end=None, end_hold_ms=None)) is None


def test_the_pace_advisory_lists_the_still_screen_of_a_scene_waiting_10_s_under_its_voice():
    events = [
        {"step": 0, "action": "click", "t0": 0.2, "ta": 0.6, "t1": 0.7, "ok": True},
        {"step": 1, "action": "wait", "t0": 0.8, "ta": 0.8, "t1": 10.8, "ok": True},
        {"step": 2, "action": "click", "t0": 10.9, "ta": 11.2, "t1": 11.3, "ok": True},
    ]
    take = timeline.TakeMedia(TAKE, 12.5, 1600, 900, 2.0, 60, 750, events=events)
    words = " ".join(["word"] * 30)
    authored = authored_spec(
        {
            "title": "Still",
            "freestyle": True,
            "scenes": [{"id": "idle", "type": "recording", "take_id": TAKE, "idle": {"mode": "off"}, "narration": words}],
        }
    )
    spec = parse_video_spec(authored)
    plan = timeline.estimate_plan(spec, {TAKE: take})
    report = pace.report(plan.document, {TAKE: take}, plan.narration_spans, spec)

    [scene] = report.scenes
    assert scene.longest_still == pytest.approx(10.0 - 0.9 - 0.3, abs=0.5) and scene.still >= scene.longest_still
    assert "still screen while the voice speaks: `idle` " in report.summary()
    [(scene_id, said)] = report.flagged()
    assert scene_id == "idle" and "its screen stays still" in said
    # Said by the quality check as a pace advisory, on that scene.
    facts = ContentFacts(takes={TAKE: take}, ready_takes={TAKE}, narration=dict(plan.narration_spans))
    found = [f for f in content_findings(spec, plan.document, facts, authored) if f.kind == "pace"]
    assert [(f.scene_id, f.severity) for f in found] == [("idle", "advisory")]


def test_a_take_captured_too_slowly_is_said_where_a_video_shows_that_stretch():
    take = _slow_take(TAKE)
    take.low = [{"t": [7.8, 9.1], "fps": 9.4, "step": 2}]
    authored = authored_spec({"title": "Low", "freestyle": True, "scenes": [{"id": "r", "type": "recording", "take_id": TAKE}]})
    spec = parse_video_spec(authored)
    plan = timeline.estimate_plan(spec, {TAKE: take})
    facts = ContentFacts(takes={TAKE: take}, ready_takes={TAKE})
    [found] = [f for f in content_findings(spec, plan.document, facts, authored) if f.kind == "take_capture_rate"]
    assert "low capture rate at step 2 (9 fps for 1.3 s)" in found.message and found.severity == "dense"
    # Trimmed away, it is not shown: nothing said.
    trimmed = authored_spec(
        {"title": "Low", "freestyle": True, "scenes": [{"id": "r", "type": "recording", "take_id": TAKE, "trim_end": 7.0}]}
    )
    spec = parse_video_spec(trimmed)
    plan = timeline.estimate_plan(spec, {TAKE: take})
    assert not [f for f in content_findings(spec, plan.document, facts, trimmed) if f.kind == "take_capture_rate"]


# ── 4. A recording drawn edge to edge ─────────────────────────────────────


def test_frame_fill_draws_the_window_edge_to_edge_and_a_lock_leaves_it_open():
    authored = authored_spec(
        {"title": "Fill", "scenes": [{"id": "r", "type": "recording", "take_id": TAKE, "frame": {"fill": True}}]}
    )
    style = templates.VideoTemplateStyle.model_validate(
        {
            "scene_defaults": {"recording": {"frame": {"style": "window", "url_bar": True, "padding": 0.055, "shadow": True}}},
            "locked": ["scenes.recording.frame"],
        }
    )
    applied = templates.apply_template(authored, templates.VideoTemplate(id="t", name="T", kind="video", style=style))
    assert applied.spec["scenes"][0]["frame"]["fill"] is True and applied.notes == []
    drawn = timeline.drawn_frame(parse_video_spec(applied.spec).scenes[0].frame)
    assert drawn == {**drawn, "style": "window", "url_bar": False, "padding": 0.0, "radius": 0, "shadow": False, "fill": True}
    # A template locking it off names it in its locked frame.
    off = templates.VideoTemplateStyle.model_validate(
        {"scene_defaults": {"recording": {"frame": {"style": "window", "fill": False}}}, "locked": ["scenes.recording.frame"]}
    )
    turned_off = templates.apply_template(authored, templates.VideoTemplate(id="t", name="T", kind="video", style=off))
    assert turned_off.spec["scenes"][0]["frame"]["fill"] is False and turned_off.notes


def test_the_filigran_product_video_template_is_calm_and_leaves_fill_open():
    from app.services.template_container_seed import BUILTIN_TEMPLATE_CONTAINERS

    [filigran] = [c for c in BUILTIN_TEMPLATE_CONTAINERS if c["slug"] == "filigran-video"]
    rules = filigran["extra_metadata"]["video"]
    # A demo told as a product manager tells it: the dynamic pace stays a video's choice.
    assert rules["defaults"]["pace"] == "calm"
    assert "scenes.recording.frame" in rules["locked"] and "fill" not in rules["scene_defaults"]["recording"]["frame"]
    assert templates.validate_style(rules)[1] == []


# ── 5. The voice ──────────────────────────────────────────────────────────


def test_a_scene_says_its_narration_at_its_own_speed():
    authored = authored_spec(
        {
            "title": "Voice",
            "freestyle": True,
            "voiceover": {"speed": 1.0},
            "scenes": [
                {"id": "a", "type": "title", "title": "A", "narration": " ".join(["word"] * 26)},
                {"id": "b", "type": "title", "title": "B", "narration": " ".join(["word"] * 26), "narration_speed": 1.3},
            ],
        }
    )
    spec = parse_video_spec(authored)
    plan = timeline.estimate_plan(spec, {})
    (a0, a1), (b0, b1) = plan.narration_spans["a"], plan.narration_spans["b"]
    assert (b1 - b0) < (a1 - a0) / 1.25
    plain, quick = voice.lines_of(spec)
    assert plain.speed is None and quick.speed == 1.3
    assert voice.line_speed(quick, spec.voiceover) == 1.3 and voice.line_speed(plain, spec.voiceover) == 1.0


def test_a_style_preset_is_sent_as_its_delivery_and_a_written_style_as_written():
    assert voice.delivery("energetic") == voice.STYLE_PRESETS["energetic"]
    assert voice.delivery(" Calm ") == voice.STYLE_PRESETS["calm"]
    assert voice.delivery("Warm and slow.") == "Warm and slow." and voice.delivery(None) is None
