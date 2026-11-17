"""A canvas moving while a take still lets its first page load (a graph's layout physics under the opening wait) is
the page settling, never stepping motion that refuses a GPU take under ``fps_fallback: refuse`` (#8457, slice 3).

The scripted motion - a click, a wheel zoom after the load - is still judged and still refuses."""

from __future__ import annotations

from app.services.video_studio import capture_rate

_OPENING = [
    {"step": 0, "action": "wait", "t0": 0.0, "t1": 3.0},
    {"step": 1, "action": "click", "t0": 3.0, "t1": 3.4},
    {"step": 2, "action": "wait", "t0": 3.4, "t1": 4.4},
    {"step": 3, "action": "wheel", "t0": 4.4, "t1": 5.4},
    {"step": 4, "action": "pause", "t0": 5.4, "t1": 7.4},
]


def test_loading_holds_while_every_step_so_far_only_waits():
    assert capture_rate.loading(_OPENING, 0.5)
    assert capture_rate.loading(_OPENING, 2.9)
    # After the click, a later wait is no longer the page loading: the script acted on it.
    assert not capture_rate.loading(_OPENING, 3.1)
    assert not capture_rate.loading(_OPENING, 3.8)
    assert not capture_rate.loading(_OPENING, 6.0)


def test_loading_before_the_first_step_and_without_steps():
    events = [{"step": 0, "action": "wait", "t0": 1.0, "t1": 3.0}]
    assert capture_rate.loading(events, 0.2)
    assert not capture_rate.loading([], 0.2)


def test_a_hover_first_is_not_loading():
    events = [{"step": 0, "action": "hover", "t0": 0.0, "t1": 2.0}]
    assert not capture_rate.loading(events, 1.0)


def _measure(monkeypatch, tmp_path, stretches):
    monkeypatch.setattr(capture_rate, "stepping", lambda *a, **k: list(stretches))
    monkeypatch.setattr(capture_rate, "capture_fault", lambda *a, **k: None)
    monkeypatch.setattr(capture_rate, "canvas_motion", lambda *a, **k: [])
    monkeypatch.setattr(capture_rate, "low_spans", lambda *a, **k: [])
    monkeypatch.setattr(capture_rate, "sparse_spans", lambda *a, **k: [])
    events = [dict(event) for event in _OPENING]
    frames = [(round(0.1 * i, 3), f"f{i:04d}.jpg") for i in range(74)]
    kept = capture_rate.measure(
        frames, events, duration=7.4, fps=30, workdir=str(tmp_path), recorded={"gl": {"gpu": True}}
    )
    return kept, events


def test_the_load_physics_of_a_graph_keep_a_gpu_take(monkeypatch, tmp_path):
    # The retest of build 3189dba: three canvases at step 0 painted at about 8 fps over 2.4-2.6 s.
    load = [
        capture_rate.Stepping(0.3, 2.7, 71, 52, 30, 0, canvas="canvas"),
        capture_rate.Stepping(0.2, 2.8, 77, 57, 30, 0, canvas="canvas#lasso-canvas"),
    ]
    kept, events = _measure(monkeypatch, tmp_path, load)
    assert all(stretch.get("settling") for stretch in kept["stepping"])
    # A settling stretch never marks its step synthesised: that is what refuses a render.
    assert "synthesised" not in events[0]
    assert capture_rate.refusal(kept, "refuse") == ""
    assert capture_rate.settling_words(kept)[0].startswith("settling (advisory) at step 0")


def test_the_scripted_motion_after_the_load_still_refuses(monkeypatch, tmp_path):
    load = capture_rate.Stepping(0.3, 2.7, 71, 52, 30, 0, canvas="canvas")
    layout = capture_rate.Stepping(3.1, 4.3, 37, 29, 30, 1, canvas="canvas")
    kept, events = _measure(monkeypatch, tmp_path, [load, layout])
    by_step = {stretch["step"]: stretch for stretch in kept["stepping"]}
    assert by_step[0].get("settling") and not by_step[1].get("settling")
    assert events[1]["synthesised"] == 0.78
    said = capture_rate.refusal(kept, "refuse")
    assert "canvas at step 1 painted at" in said and "step 0" not in said


def _measure_one(monkeypatch, tmp_path, stretch, events, frames):
    monkeypatch.setattr(capture_rate, "stepping", lambda *a, **k: [stretch])
    monkeypatch.setattr(capture_rate, "capture_fault", lambda *a, **k: None)
    monkeypatch.setattr(capture_rate, "canvas_motion", lambda *a, **k: [])
    monkeypatch.setattr(capture_rate, "low_spans", lambda *a, **k: [])
    monkeypatch.setattr(capture_rate, "sparse_spans", lambda *a, **k: [])
    shot = [(round(0.1 * i, 3), f"f{i:04d}.jpg") for i in range(frames)]
    return capture_rate.measure(
        shot, events, duration=frames / 10, fps=30, workdir=str(tmp_path), recorded={"gl": {"gpu": True}}
    )


def test_the_whole_page_drawing_before_the_first_step_keeps_a_gpu_take(monkeypatch, tmp_path):
    # The refusal of take e4e6367e (#9266): "the page at 0.3 s painted at 11.8 fps (13 of 21 frames synthesised)",
    # the page it opened on still drawing before step 0 started; an identical retake was kept.
    events = [{"step": 0, "action": "click", "t0": 1.2, "t1": 1.6}]
    kept = _measure_one(monkeypatch, tmp_path, capture_rate.Stepping(0.3, 1.0, 21, 13, 30, None), events, 20)
    assert kept["stepping"][0].get("settling")
    assert "synthesised" not in events[0]
    assert capture_rate.refusal(kept, "refuse") == ""


def test_the_whole_page_moving_on_a_click_still_refuses(monkeypatch, tmp_path):
    events = [{"step": 0, "action": "click", "t0": 1.2, "t1": 2.6}]
    kept = _measure_one(monkeypatch, tmp_path, capture_rate.Stepping(1.3, 2.4, 34, 26, 30, 0), events, 26)
    assert not kept["stepping"][0].get("settling")
    assert "the page at step 0 painted at" in capture_rate.refusal(kept, "refuse")
