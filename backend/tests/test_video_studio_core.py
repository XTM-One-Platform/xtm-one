"""Video Studio: the pure core - specs, the timeline planner, the audio
mix graph, media tokens and ranges, spec edits, profile secrets, URL policy.

Nothing here launches a browser or ffmpeg: those run in the worker, and their
end-to-end check needs Chromium and ffmpeg on the host.
"""

import asyncio
import base64
import itertools
import json
import math
import shutil
import subprocess
import uuid

import pytest

from app.models.browser_profile import SESSION_ACTIVE, SESSION_NONE, BrowserProfile
from app.services.browser_profiles import browser, profiles
from app.services.video_studio import compositor_page, encoder, media, projects, spec, timeline

TAKE_ID = "00000000-0000-0000-0000-000000000001"


def _spec(**overrides) -> spec.VideoSpec:
    raw = {
        "title": "Demo",
        "voiceover": {"enabled": False},
        "scenes": [
            {"type": "title", "title": "Hello", "duration": 3.0},
            {"type": "recording", "take_id": TAKE_ID},
            {"type": "outro", "title": "Bye", "duration": 3.0},
        ],
    }
    raw.update(overrides)
    return spec.parse_video_spec(spec.validate_video_spec(raw))


def _take(events: list[dict] | None = None, duration: float = 20.0) -> timeline.TakeMedia:
    return timeline.TakeMedia(
        take_id=TAKE_ID,
        duration=duration,
        css_width=1600,
        css_height=900,
        pixel_ratio=1.5,
        fps=60,
        frame_count=int(duration * 60),
        events=events or [],
        cursor=[(0.0, 800.0, 450.0), (duration, 800.0, 450.0)],
    )


_CLICK_EVENTS = [
    {"step": 0, "action": "goto", "t0": 0.0, "ta": 0.0, "t1": 1.0, "url": "https://demo.example.com/"},
    {
        "step": 1,
        "action": "click",
        "label": "Open settings",
        "t0": 2.0,
        "ta": 2.6,
        "t1": 3.0,
        "box": {"x": 1300, "y": 80, "w": 120, "h": 40},
        "point": {"x": 1360, "y": 100},
    },
    # 12 s with nothing on screen, then a second action.
    {
        "step": 2,
        "action": "type",
        "label": "Name it",
        "t0": 15.0,
        "ta": 15.2,
        "t1": 17.0,
        "box": {"x": 200, "y": 600, "w": 400, "h": 40},
        "point": {"x": 400, "y": 620},
    },
]


# ── Spec validation ───────────────────────────────────────────────────────


def test_scene_ids_are_assigned_and_unique():
    parsed = _spec()
    ids = [scene.id for scene in parsed.scenes]
    assert all(ids) and len(set(ids)) == len(ids)


def test_a_spec_problem_names_its_path():
    with pytest.raises(spec.VideoSpecError) as exc:
        spec.validate_video_spec({"title": "x", "scenes": [{"type": "recording"}]})
    assert any("scenes" in problem.path for problem in exc.value.problems)


def test_an_unknown_scene_type_is_refused():
    with pytest.raises(spec.VideoSpecError):
        spec.validate_video_spec({"title": "x", "scenes": [{"type": "hologram"}]})


def test_a_take_script_needs_steps_and_a_known_action():
    script = spec.validate_take_script(
        {"start_url": "https://demo.example.com", "steps": [{"action": "pause", "seconds": 1.0}]}
    )
    assert script["steps"][0]["action"] == "pause"
    with pytest.raises(spec.VideoSpecError):
        spec.validate_take_script({"start_url": "https://demo.example.com", "steps": []})
    with pytest.raises(spec.VideoSpecError):
        spec.validate_take_script({"start_url": "https://demo.example.com", "steps": [{"action": "teleport"}]})


def test_referenced_ids_collects_every_kind():
    raw = spec.validate_video_spec(
        {
            "title": "x",
            "scenes": [
                {"type": "recording", "take_id": TAKE_ID},
                {"type": "slide", "presentation_id": "11111111-1111-1111-1111-111111111111", "slide": 1},
            ],
        }
    )
    refs = spec.referenced_ids(raw)
    assert refs["take"] == {TAKE_ID}
    assert refs["presentation"] == {"11111111-1111-1111-1111-111111111111"}


@pytest.mark.parametrize(("ratio", "short_side"), [("16:9", 1080), ("9:16", 1080), ("1:1", 720), ("16:9", 2160)])
def test_output_sizes_are_even_and_keep_the_ratio(ratio, short_side):
    width, height = spec.output_size(ratio, short_side)
    assert width % 2 == 0 and height % 2 == 0
    assert min(width, height) == short_side
    a, b = (int(part) for part in ratio.split(":"))
    assert abs(width / height - a / b) < 0.01


def test_the_default_quality_is_1080p60():
    preset = spec.QUALITY_PRESETS["high"]
    assert (preset.short_side, preset.fps) == (1080, 60)
    assert spec.QUALITY_PRESETS["ultra"].short_side == 2160


# ── Time maps and idle compression ────────────────────────────────────────


def test_map_forward_and_inverse_are_consistent():
    points = [(0.0, 0.0), (2.0, 2.0), (3.0, 10.0), (5.0, 12.0)]
    for x in (0.0, 1.0, 2.5, 4.0, 5.0):
        assert timeline.map_inverse(points, timeline.map_forward(points, x)) == pytest.approx(x)


def test_idle_stretches_are_compressed_and_actions_keep_real_time():
    events = _CLICK_EVENTS
    plain = timeline.build_time_map(
        duration=20.0,
        trim_start=0.0,
        trim_end=None,
        speed=1.0,
        idle_mode="off",
        idle_threshold=1.5,
        idle_speed=4.0,
        events=events,
    )
    compressed = timeline.build_time_map(
        duration=20.0,
        trim_start=0.0,
        trim_end=None,
        speed=1.0,
        idle_mode="speed",
        idle_threshold=1.5,
        idle_speed=4.0,
        events=events,
    )
    assert plain[-1][0] == pytest.approx(20.0)
    assert compressed[-1][0] < 15.0
    # The click (2.0 -> 3.0 s of the recording) still lasts one second.
    t_a = timeline.map_inverse(compressed, 2.0)
    t_b = timeline.map_inverse(compressed, 3.0)
    assert t_b - t_a == pytest.approx(1.0, abs=0.01)


def test_trim_and_speed_shorten_the_scene():
    points = timeline.build_time_map(
        duration=20.0,
        trim_start=4.0,
        trim_end=14.0,
        speed=2.0,
        idle_mode="off",
        idle_threshold=1.5,
        idle_speed=4.0,
        events=[],
    )
    assert points[0] == (0.0, 4.0)
    assert points[-1][0] == pytest.approx(5.0)
    assert points[-1][1] == pytest.approx(14.0)


def test_a_hold_freezes_the_last_frame():
    points = timeline.with_hold([(0.0, 0.0), (4.0, 4.0)], 1.5)
    assert points[-1] == (5.5, 4.0)


# ── Planner ───────────────────────────────────────────────────────────────


def test_the_plan_is_frame_exact_and_ordered():
    parsed = _spec()
    result = timeline.plan(parsed, timeline.PlanMedia(takes={TAKE_ID: _take(_CLICK_EVENTS)}), fps=60, theme={})
    doc = result.document
    assert doc["frames"] == pytest.approx(result.duration * 60, abs=1)
    starts = [scene["start"] for scene in doc["scenes"]]
    assert starts == sorted(starts)
    assert doc["scenes"][0]["transition_in"]["type"] == "cut"


def test_the_auto_camera_zooms_on_clicked_elements():
    parsed = _spec()
    result = timeline.plan(parsed, timeline.PlanMedia(takes={TAKE_ID: _take(_CLICK_EVENTS)}), fps=60, theme={})
    recording = next(scene for scene in result.document["scenes"] if scene["type"] == "recording")
    zooms = [frame[3] for frame in recording["camera"]]
    assert max(zooms) > 1.1
    assert zooms[0] == pytest.approx(1.0)
    for frame in recording["camera"]:
        _, cx, cy, zoom = frame[:4]
        half_w, half_h = 0.5 / zoom, 0.5 / zoom
        assert half_w - 1e-6 <= cx <= 1 - half_w + 1e-6
        assert half_h - 1e-6 <= cy <= 1 - half_h + 1e-6
    assert recording["cursor"]["clicks"], "a click event draws a ripple"


def test_a_static_camera_never_zooms():
    parsed = _spec(
        scenes=[{"type": "recording", "take_id": TAKE_ID, "camera": {"mode": "static"}}],
    )
    result = timeline.plan(parsed, timeline.PlanMedia(takes={TAKE_ID: _take(_CLICK_EVENTS)}), fps=30, theme={})
    assert result.document["scenes"][0]["camera"] == [[0.0, 0.5, 0.5, 1.0]]


def _click(step: int, ta: float, x: float, y: float, w: float = 120.0, h: float = 36.0, **extra) -> dict:
    return {
        "step": step,
        "action": "click",
        "t0": ta - 0.5,
        "ta": ta,
        "t1": ta + 0.07,
        "box": {"x": x, "y": y, "w": w, "h": h},
        "point": {"x": x + w / 2, "y": y + h / 2},
        **extra,
    }


def _page_click(step: int, ta: float, x: float, y: float, w: float, h: float, before: str, after: str) -> dict:
    return _click(step, ta, x, y, w, h, url_before=f"https://demo.example.com{before}", url=f"https://demo.example.com{after}")


#: A real take of a short demo, 1600x900: open the threats, search, open one, its Knowledge tab.
_SHORT_DEMO = [
    _page_click(0, 1.33, 12.0, 144.2, 207.3, 38.3, "/dashboard", "/threats"),
    {
        "step": 1,
        "action": "type",
        "t0": 3.66,
        "ta": 4.06,
        "t1": 4.68,
        "box": {"x": 254.0, "y": 12.0, "w": 420.0, "h": 35.3},
        "point": {"x": 300.0, "y": 29.6},
        "url_before": "https://demo.example.com/threats",
        "url": "https://demo.example.com/threats",
    },
    _page_click(2, 7.3, 258.3, 246.2, 287.4, 46.7, "/threats", "/threat/emotet"),
    _page_click(3, 9.94, 347.0, 156.2, 96.9, 42.3, "/threat/emotet", "/threat/emotet"),
]

#: Clicks every 1.3 s around the page: more than any calm camera can follow.
_BUSY_EVENTS = [
    {"step": 0, "action": "goto", "t0": 0.0, "ta": 0.0, "t1": 1.5, "url": "https://demo.example.com/"},
    *(
        _click(i + 1, 2.0 + 1.3 * i, x, y)
        for i, (x, y) in enumerate(
            [(60, 120), (1300, 140), (700, 450), (100, 780), (1350, 760), (650, 200), (720, 260), (760, 320), (300, 500)]
        )
    ),
]


def _camera(events: list[dict], *, duration: float = 20.0, trim=(0.0, None), idle: str = "compress", **options):
    take = _take(events, duration=duration)
    time_map = timeline.build_time_map(
        duration=duration,
        trim_start=trim[0],
        trim_end=trim[1],
        speed=1.0,
        idle_mode=idle,
        idle_threshold=1.2,
        idle_speed=6.0,
        events=events,
    )
    options.setdefault("mode", "auto")
    options.setdefault("keyframes", [])
    return timeline.plan_camera(
        max_zoom=1.8,
        zoom_on=["click", "type", "select", "highlight"],
        media=take,
        time_map=time_map,
        trim_start=trim[0],
        out_duration=time_map[-1][0],
        **options,
    )


def _moves(frames: list[list[float]]) -> list[tuple[float, float, str]]:
    """``(start, end, kind)`` of every move of a camera: ``in``, ``out`` or ``pan``."""
    return [
        (a[0], b[0], "in" if b[3] > a[3] else "out" if b[3] < a[3] else "pan")
        for a, b in itertools.pairwise(frames)
        if a[1:4] != b[1:4]
    ]


def _deepest_zoom_between(frames: list[list[float]], start: float, end: float) -> float:
    """The deepest the camera can be between *start* and *end*: every keyframe in or around that span."""
    before = [f for f in frames if f[0] <= start][-1:] or frames[:1]
    inside = [f for f in frames if start < f[0] < end]
    after = [f for f in frames if f[0] >= end][:1]
    return max(f[3] for f in [*before, *inside, *after])


def _view_at(frames: list[list[float]], t: float) -> tuple[float, float, float]:
    """``(cx, cy, zoom)`` at *t*, as the compositor eases it (``camera()``: smootherstep, the pull-back of a move)."""
    if t <= frames[0][0]:
        return tuple(frames[0][1:4])
    if t >= frames[-1][0]:
        return tuple(frames[-1][1:4])
    a, b = next((a, b) for a, b in itertools.pairwise(frames) if a[0] <= t < b[0])
    u = (t - a[0]) / max(b[0] - a[0], 1e-6)
    p = u**3 * (u * (6 * u - 15) + 10)
    arc = b[4] if len(b) > 4 else 0.0
    zoom = math.exp(math.log(a[3]) + (math.log(b[3]) - math.log(a[3])) * p - arc * math.sin(math.pi * p))
    return a[1] + (b[1] - a[1]) * p, a[2] + (b[2] - a[2]) * p, zoom


def _shows(frames: list[list[float]], t: float, event: dict) -> bool:
    """Whether the camera at *t* shows the whole element of *event*, zoomed in (1600x900 page)."""
    cx, cy, zoom = _view_at(frames, t)
    box, half = event["box"], 0.5 / zoom
    left, top = box["x"] / 1600, box["y"] / 900
    right, bottom = left + box["w"] / 1600, top + box["h"] / 900
    return zoom > 1.2 and cx - half <= left and right <= cx + half and cy - half <= top and bottom <= cy + half


def _compositor_camera(tmp_path, frames: list[list[float]]) -> list[list[float]]:
    """``[t, cx, cy, zoom]`` every 1/60 s, drawn by the compositor's own ``camera()`` under node."""
    source = compositor_page.PAGE_JS
    start = source.index("const clamp = ")
    helpers = source[start : source.index("const el = ", start)]
    camera = source[source.index("function camera(") : source.index("function cursorAt(")]
    harness = """
const frames = JSON.parse(process.argv[2]);
const out = [];
for (let i = 0; i <= (frames[frames.length - 1][0] + 0.5) * 60; i++) out.push([i / 60, ...camera(frames, i / 60).slice(0, 3)]);
console.log(JSON.stringify(out));
"""
    script = tmp_path / "camera.js"
    script.write_text(helpers + camera + harness, encoding="utf-8")
    result = subprocess.run(  # noqa: S603
        [shutil.which("node") or "node", str(script), json.dumps(frames)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


#: Three steps far apart on one page, 2.5 s apart: the camera travels between them.
_SPREAD_EVENTS = [_click(0, 2.0, 60, 120), _click(1, 4.5, 1380, 760), _click(2, 7.0, 1350, 120)]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_compositor_glides_the_camera(tmp_path):
    for events in (_SHORT_DEMO, _SPREAD_EVENTS, _BUSY_EVENTS):
        frames = _camera(events)
        samples = _compositor_camera(tmp_path, frames)
        for t, cx, cy, zoom in samples[::7]:
            assert (cx, cy, zoom) == pytest.approx(_view_at(frames, t), abs=1e-4)
        # Per second, at the fastest frame: a calm zoom to 1.8 peaks at 1.875x its mean
        # (1.0 log-zoom/s), where the first planner's 0.4 s cubic zooms reached 4.4; a pan
        # across the whole page, pulling back on the way, at 1.2 view widths.
        zoom_speed = max(abs(math.log(b[3] / a[3])) * 60 for a, b in itertools.pairwise(samples))
        pan_speed = max(
            math.hypot(b[1] - a[1], b[2] - a[2]) * max(a[3], b[3]) * 60 for a, b in itertools.pairwise(samples)
        )
        assert zoom_speed < 1.1
        assert pan_speed < 1.3


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_long_move_pulls_back_on_the_way(tmp_path):
    frames = _camera(_SPREAD_EVENTS)
    far = next(b for a, b in itertools.pairwise(frames) if len(b) > 4)
    start = frames[frames.index(far) - 1]
    middle = [zoom for t, _, _, zoom in _compositor_camera(tmp_path, frames) if start[0] < t < far[0]]
    assert min(middle) < min(start[3], far[3]) - 0.1


def test_an_action_trimmed_away_never_moves_the_camera():
    # Before the fix, the steps before trim_start all landed on the scene's first
    # instant and opened it on a burst of 0.4 s zooms and pans.
    events = [*_CLICK_EVENTS[:2], _click(3, 5.0, 900, 500), _click(4, 6.2, 200, 700), _CLICK_EVENTS[2]]
    assert _camera(events, trim=(7.0, 14.0)) == [[0.0, 0.5, 0.5, 1.0]]


@pytest.mark.parametrize("pace", ["calm", "dynamic"])
@pytest.mark.parametrize("events", [_SHORT_DEMO, _SPREAD_EVENTS, _BUSY_EVENTS], ids=["short", "spread", "busy"])
def test_camera_moves_keep_their_length_and_rest_between_them(pace, events):
    timing = timeline.CAMERA_PACES[pace]
    moves = _moves(_camera(events, pace=pace))
    assert moves
    for start, end, kind in moves:
        floor = timing.pan * 0.7 if kind == "pan" else min(timing.zoom_in, timing.zoom_out) * 0.8
        assert end - start >= floor - 1e-3, (kind, start, end)
    for (_, end, kind), (start, _, next_kind) in itertools.pairwise(moves):
        assert start - end >= timing.settle - 1e-3, (kind, next_kind, end, start)


@pytest.mark.parametrize("scenes", ["alone", "carded"])
def test_a_short_demo_follows_its_steps(scenes):
    # A 10-second demo stayed static: the camera now opens wide, zooms in on the search,
    # backs out while the threat's page opens, and zooms in on its tab.
    recording = {"type": "recording", "take_id": TAKE_ID}
    parsed = _spec(scenes=[recording] if scenes == "alone" else [{"type": "title", "title": "Hi", "duration": 3.0}, recording])
    plan = timeline.plan(parsed, timeline.PlanMedia(takes={TAKE_ID: _take(_SHORT_DEMO, duration=13.4)}), fps=30, theme={})
    scene = next(s for s in plan.document["scenes"] if s["type"] == "recording")
    frames, time_map = scene["camera"], [tuple(p) for p in scene["time_map"]]
    at = {e["step"]: timeline.map_inverse(time_map, e["ta"]) for e in _SHORT_DEMO}
    kinds = [kind for _, _, kind in _moves(frames)]
    assert kinds.count("in") >= 2 and "out" in kinds
    assert _moves(frames)[0][0] >= max(timeline.CAMERA_PACES["calm"].establish, scene["transition_in"]["duration"])
    assert _shows(frames, at[1] + 0.3, _SHORT_DEMO[1]) and _shows(frames, at[3] + 0.4, _SHORT_DEMO[3])
    # Wide while the threat's page opens.
    assert any(_view_at(frames, t / 10)[2] == pytest.approx(1.0) for t in range(int(at[2] * 10), int(at[3] * 10)))


def test_the_camera_follows_each_step_across_the_page():
    frames = _camera(_SPREAD_EVENTS)
    for event in _SPREAD_EVENTS:
        assert _shows(frames, event["t1"], event), event["ta"]


def test_a_step_already_in_view_is_not_panned_to():
    events = [_click(0, 2.0, 700, 400), _click(1, 4.0, 760, 430)]
    assert [kind for _, _, kind in _moves(_camera(events))] == ["in", "out"]


def test_a_step_that_opens_another_page_shows_it_wide():
    opens = _page_click(1, 4.0, 700, 400, 120, 36, "/threats", "/threat/emotet")
    events = [_click(0, 2.5, 650, 380), opens, _page_click(2, 7.0, 300, 150, 120, 36, "/threat/emotet", "/threat/emotet")]
    frames = _camera(events, idle="off")
    kinds = [(kind, start) for start, _, kind in _moves(frames)]
    assert [kind for kind, _ in kinds] == ["in", "out", "in", "out"]
    # Back out right after the step, and no zoom in before the page it opened is there.
    assert kinds[1][1] == pytest.approx(opens["t1"] + timeline.PAGE_HOLD, abs=0.01)
    assert kinds[2][1] >= opens["t1"] + timeline.PAGE_HOLD


def test_the_camera_stays_wide_while_the_page_scrolls():
    # A click, the capture scrolls the next element into view, the next click.
    scrolled = [3.6, 4.2]
    events = [
        _CLICK_EVENTS[0],
        _click(1, 2.6, 700, 400),
        _click(2, 4.8, 720, 430, scrolled=scrolled),
    ]
    unaware = _camera([{k: v for k, v in e.items() if k != "scrolled"} for e in events], idle="off", pace="dynamic")
    assert _deepest_zoom_between(unaware, *scrolled) > 1.0
    assert _deepest_zoom_between(_camera(events, idle="off", pace="dynamic"), *scrolled) == 1.0


def test_a_scroll_into_view_plays_at_real_speed():
    events = [_CLICK_EVENTS[0], _click(1, 7.5, 700, 400, scrolled=[6.0, 6.8])]
    points = timeline.build_time_map(
        duration=10.0,
        trim_start=0.0,
        trim_end=None,
        speed=1.0,
        idle_mode="compress",
        idle_threshold=1.2,
        idle_speed=6.0,
        events=events,
    )
    assert timeline.map_inverse(points, 6.8) - timeline.map_inverse(points, 6.0) == pytest.approx(0.8, abs=0.01)


def test_the_camera_waits_for_the_page_load_and_the_transition_in():
    # Wide while the first page loads (the goto ends at 1 s), then in.
    assert _moves(_camera(_CLICK_EVENTS, idle="off"))[0][0] == pytest.approx(1.0)
    assert _moves(_camera(_CLICK_EVENTS, idle="off", start_after=1.5))[0][0] >= 1.5
    # The planner hands it the transition into the scene: the title's push.
    typing = [
        {
            "step": 0,
            "action": "type",
            "t0": 0.8,
            "ta": 1.2,
            "t1": 4.0,
            "box": {"x": 200, "y": 600, "w": 400, "h": 40},
            "point": {"x": 240, "y": 620},
        }
    ]
    # The scene opens wide: the page in full before the first zoom.
    assert _moves(_camera(typing))[0][0] == pytest.approx(timeline.CAMERA_PACES["calm"].establish)
    result = timeline.plan(_spec(), timeline.PlanMedia(takes={TAKE_ID: _take(typing)}), fps=60, theme={})
    recording = next(scene for scene in result.document["scenes"] if scene["type"] == "recording")
    assert _moves(recording["camera"])[0][0] >= recording["transition_in"]["duration"] > 0


def test_a_highlight_frames_its_whole_region():
    panel = {"x": 300, "y": 300, "w": 1000, "h": 400}
    highlight = {"step": 0, "action": "highlight", "t0": 1.5, "ta": 2.0, "t1": 4.0, "box": panel}
    zoom = max(frame[3] for frame in _camera([highlight]))
    assert 1.15 < zoom < 1.25  # the region fills 75% of the view
    # A click on an element that large is not worth a zoom.
    assert _camera([{**highlight, "action": "click", "t1": 2.07}]) == [[0.0, 0.5, 0.5, 1.0]]


def test_a_pace_and_a_move_length_time_the_camera():
    calm, dynamic = timeline.camera_pace("calm"), timeline.camera_pace("dynamic")
    assert dynamic.zoom_in < calm.zoom_in and dynamic.min_wide < calm.min_wide
    slow = timeline.camera_pace("calm", 2.6)
    scale = 2.6 / calm.zoom_in
    assert (slow.zoom_in, slow.zoom_out, slow.pan) == pytest.approx((2.6, calm.zoom_out * scale, calm.pan * scale))
    first_zoom = {
        seconds: next(end - start for start, end, kind in _moves(_camera(_CLICK_EVENTS, move_seconds=seconds)) if kind == "in")
        for seconds in (0.8, 2.0)
    }
    assert first_zoom[2.0] > first_zoom[0.8]


def test_manual_keyframes_keep_their_length_through_a_compressed_pause():
    keyframes = [{"t": 3.0, "x": 0.5, "y": 0.5, "zoom": 1.0}, {"t": 8.0, "x": 0.3, "y": 0.3, "zoom": 2.0}]
    events = [_CLICK_EVENTS[0], _click(1, 12.0, 700, 400)]
    squeezed = _camera(events, mode="manual", keyframes=keyframes)
    assert squeezed[1][0] - squeezed[0][0] >= timeline.CAMERA_PACES["calm"].zoom_in - 1e-3
    plain = _camera(events, mode="manual", keyframes=keyframes, idle="off")
    assert plain[1][0] - plain[0][0] == pytest.approx(5.0)


def test_the_camera_pace_is_part_of_the_spec():
    parsed = _spec(
        scenes=[{"type": "recording", "take_id": TAKE_ID, "camera": {"pace": "dynamic", "move_seconds": 1.1}}],
    )
    assert (parsed.scenes[0].camera.pace, parsed.scenes[0].camera.move_seconds) == ("dynamic", 1.1)
    assert _spec(scenes=[{"type": "recording", "take_id": TAKE_ID}]).scenes[0].camera.pace == "calm"
    for camera in ({"pace": "frantic"}, {"move_seconds": 0.1}):
        with pytest.raises(spec.VideoSpecError):
            _spec(scenes=[{"type": "recording", "take_id": TAKE_ID, "camera": camera}])


def test_an_overlay_anchors_to_its_step():
    parsed = _spec(
        scenes=[
            {
                "type": "recording",
                "take_id": TAKE_ID,
                "idle": {"mode": "off"},
                "overlays": [{"type": "callout", "text": "Here", "at_step": 2, "duration": 2.0}],
            }
        ],
    )
    result = timeline.plan(parsed, timeline.PlanMedia(takes={TAKE_ID: _take(_CLICK_EVENTS)}), fps=30, theme={})
    overlay = result.document["scenes"][0]["overlays"][0]
    # Step 2 acts at 15.2 s of the recording, shown 0.15 s before.
    assert overlay["t0"] == pytest.approx(15.05, abs=0.05)
    assert overlay["box"], "an overlay on a step points at its element"


def test_a_missing_step_is_a_note_not_a_crash():
    parsed = _spec(
        scenes=[
            {
                "type": "recording",
                "take_id": TAKE_ID,
                "overlays": [{"type": "callout", "text": "Here", "at_step": 9}],
            }
        ],
    )
    result = timeline.plan(parsed, timeline.PlanMedia(takes={TAKE_ID: _take(_CLICK_EVENTS)}), fps=30, theme={})
    assert any("step 9" in note for note in result.notes)


def test_narration_lengthens_a_scene_to_fit_its_voice():
    parsed = _spec(
        voiceover={"enabled": True},
        scenes=[{"type": "title", "title": "Hello", "duration": 2.0, "narration": "A narration line."}],
    )
    scene_id = parsed.scenes[0].id
    result = timeline.plan(
        parsed,
        timeline.PlanMedia(voices={scene_id: timeline.VoiceMedia(duration=7.5)}),
        fps=30,
        theme={},
    )
    assert result.duration > 7.5
    assert scene_id in result.narration_starts


def test_captions_follow_the_narration():
    parsed = _spec(
        captions={"enabled": True},
        scenes=[
            {
                "type": "title",
                "title": "Hello",
                "duration": 2.0,
                "narration": "First we open the settings. Then we name the new workspace and save it.",
            }
        ],
    )
    result = timeline.plan(parsed, timeline.PlanMedia(), fps=30, theme={})
    cues = result.document["captions"]["cues"]
    assert len(cues) >= 2
    assert all(t0 < t1 for t0, t1, _ in cues)
    assert all(cues[i][1] <= cues[i + 1][0] + 1e-6 for i in range(len(cues) - 1))
    vtt = timeline.vtt_from_cues(cues)
    assert vtt.startswith("WEBVTT")
    assert "-->" in vtt


def test_caption_phrases_stay_short():
    text = " ".join(["word"] * 120)
    assert all(len(phrase) <= timeline.CAPTION_MAX_CHARS for phrase in timeline.caption_phrases(text))


def test_a_missing_recording_fails_the_plan():
    with pytest.raises(ValueError, match="not available"):
        timeline.plan(_spec(), timeline.PlanMedia(), fps=30, theme={})


def test_the_estimate_needs_no_media():
    assert timeline.estimate_duration(_spec(), {}) > 6.0


# ── Audio mix graph ───────────────────────────────────────────────────────


def test_the_mix_takes_the_voice_stem_and_ducks_music_under_it():
    inputs, graph = encoder.build_mix_filter(
        duration=30.0,
        voice=encoder.VoiceTrack("stem.wav", 0.9),
        clips=[],
        music=encoder.MusicTrack("bed.wav", 0.5, 10.0, ((1.25, 4.0), (10.0, 14.0))),
    )
    assert inputs == ["stem.wav", "bed.wav"]
    # The stem is already placed and at the mix rate (``stitch``): never delayed nor resampled again.
    voice_chain = next(part for part in graph.split(";") if part.startswith("[0:a]"))
    assert voice_chain == "[0:a]pan=stereo|c0=c0|c1=c0,volume=0.900[voice]"
    assert "adelay" not in graph
    # The envelope is computed from the narration's place, not heard from it.
    assert "sidechaincompress" not in graph
    assert "volume='0.500*(1-0.6838*clip(" in graph and "eval=frame" in graph
    # The master is normalised in a second pass, from the measured mix.
    assert "loudnorm" not in graph
    assert graph.endswith("[mix]")


def test_a_bed_without_ducking_keeps_its_level():
    _inputs, graph = encoder.build_mix_filter(
        duration=20.0, voice=encoder.VoiceTrack("stem.wav"), clips=[], music=encoder.MusicTrack("bed.wav", 0.4, 0.0, ((2.0, 5.0),))
    )
    assert "volume=0.400" in graph and "eval=frame" not in graph


def test_a_silent_video_still_gets_an_audio_track():
    inputs, graph = encoder.build_mix_filter(duration=12.0, voice=None, clips=[], music=None)
    assert inputs == []
    assert graph.startswith("anullsrc")
    assert "duration=12.000" in graph


# ── Media tokens and ranges ───────────────────────────────────────────────


def test_a_media_token_is_bound_to_one_file():
    file_id, user_id = uuid.uuid4(), uuid.uuid4()
    token, expires = media.mint_media_token(file_id, user_id)
    claims = media.decode_media_token(token)
    assert (claims.file_id, claims.user_id) == (file_id, user_id)
    assert claims.expires == expires


def test_a_session_token_is_not_a_media_token():
    from app.core.security import create_access_token

    with pytest.raises(media.MediaTokenError):
        media.decode_media_token(create_access_token({"sub": str(uuid.uuid4())}))
    with pytest.raises(media.MediaTokenError):
        media.decode_media_token("not-a-token")


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("bytes=0-99", (0, 99)),
        ("bytes=100-", (100, 999)),
        ("bytes=-200", (800, 999)),
        ("bytes=900-5000", (900, 999)),
        ("items=0-1", None),
    ],
)
def test_range_parsing(header, expected):
    assert media.parse_range(header, 1000) == expected


def test_an_unsatisfiable_range_raises():
    with pytest.raises(ValueError):
        media.parse_range("bytes=2000-", 1000)


def test_an_open_range_is_capped():
    size = 50 * 1024 * 1024
    start, end = media.parse_range("bytes=0-", size)
    assert start == 0
    assert end - start + 1 == media.MAX_RANGE_BYTES


# ── Spec edits ────────────────────────────────────────────────────────────


def test_operations_edit_scenes_in_order():
    raw = spec.validate_video_spec(
        {
            "title": "x",
            "scenes": [
                {"id": "intro", "type": "title", "title": "Hi"},
                {"id": "end", "type": "outro", "title": "Bye"},
            ],
        }
    )
    edited, lines = projects.apply_operations(
        raw,
        [
            {"op": "update_scene", "scene": "intro", "fields": {"title": "Hello"}},
            {"op": "insert_scene", "index": 1, "value": {"id": "mid", "type": "title", "title": "Middle"}},
            {"op": "move_scene", "scene": "end", "index": 0},
            {"op": "update", "fields": {"title": "Renamed"}},
        ],
    )
    assert [scene["id"] for scene in edited["scenes"]] == ["end", "intro", "mid"]
    assert edited["scenes"][1]["title"] == "Hello"
    assert edited["title"] == "Renamed"
    assert len(lines) == 4
    assert raw["scenes"][0]["title"] == "Hi", "the input spec is not mutated"


def test_an_unknown_scene_or_op_is_refused():
    raw = spec.validate_video_spec({"title": "x", "scenes": [{"type": "title", "title": "Hi"}]})
    with pytest.raises(spec.VideoSpecError):
        projects.apply_operations(raw, [{"op": "remove_scene", "scene": "nope"}])
    with pytest.raises(spec.VideoSpecError):
        projects.apply_operations(raw, [{"op": "explode"}])
    with pytest.raises(spec.VideoSpecError):
        projects.apply_operations(raw, [{"op": "update_scene", "scene": 0, "fields": {"type": "outro"}}])


# ── Profile secrets ───────────────────────────────────────────────────────

# The RFC 6238 test key (the ASCII digits 1-9, 0, twice), in base32.
_SECRET = base64.b32encode(b"12345678901234567890").decode()


def test_credentials_are_encrypted_and_write_only():
    profile = BrowserProfile(name="Demo", start_url="https://demo.example.com")
    assert profiles.write_credentials(profile, username="ana@example.com", password=" p@ss ", totp_secret=_SECRET)
    assert profile.credentials_enc and "p@ss" not in profile.credentials_enc
    assert profiles.read_credentials(profile) == {
        "username": "ana@example.com",
        "password": " p@ss ",  # a password is kept byte for byte
        "totp_secret": _SECRET,
    }
    # Omitted = kept; "" = cleared; the same value = no change.
    assert profiles.write_credentials(profile, password="") is True
    assert "password" not in profiles.read_credentials(profile)
    assert profiles.write_credentials(profile, username="ana@example.com") is False


def test_a_bad_totp_secret_is_refused_on_write():
    profile = BrowserProfile(name="Demo", start_url="https://demo.example.com")
    with pytest.raises(profiles.ProfileError):
        profiles.write_credentials(profile, totp_secret="not a secret!")


def test_the_serialized_profile_never_carries_a_secret():
    profile = BrowserProfile(
        id=uuid.uuid4(),
        name="Demo",
        start_url="https://demo.example.com",
        allowed_hosts=[],
        keepalive_minutes=60,
    )
    profiles.write_credentials(profile, username="ana@example.com", password="hunter2", totp_secret=_SECRET)
    profiles.save_storage_state(
        profile,
        {"cookies": [{"name": "sid", "value": "secret-cookie"}], "origins": []},
        probe={"authenticated": True, "url": "https://demo.example.com/", "reason": "start_url renders the application"},
    )
    payload = profiles.serialize_profile(profile, can_manage=True)
    text = repr(payload)
    for secret in ("hunter2", _SECRET, "secret-cookie", "ana@example.com"):
        assert secret not in text
    assert payload["has_password"] and payload["has_totp"] and payload["has_username"]
    assert payload["username_hint"] == "a***@example.com"
    assert payload["session_status"] == SESSION_ACTIVE
    assert "credentials_enc" not in payload and "storage_state_enc" not in payload


def test_forgetting_the_session_clears_it():
    profile = BrowserProfile(name="Demo", start_url="https://demo.example.com")
    profiles.save_storage_state(profile, {"cookies": [], "origins": []})
    assert profiles.read_storage_state(profile) == {"cookies": [], "origins": []}
    profiles.save_storage_state(profile, None)
    assert profiles.read_storage_state(profile) is None
    assert profile.session_status == SESSION_NONE


def test_no_tool_takes_a_profile_credential():
    """A tool call's arguments stay in the conversation and its traces; credentials are typed on the page."""
    from app.services.builtin_tools.video_studio import build_video_studio_tools

    definitions, executor = build_video_studio_tools(user_id="")
    save = next(d for d in definitions if d.name == "save_browser_profile")
    assert not {"username", "password", "totp_secret"} & set(save.input_schema["properties"])
    assert "Browser profiles page" in save.description
    for definition in definitions:
        assert not {"password", "totp_secret"} & set(definition.input_schema.get("properties", {})), definition.name
    import inspect

    assert not {"username", "password", "totp_secret"} & set(inspect.signature(executor.handler("save_browser_profile")).parameters)


def test_a_typed_text_never_names_its_step_in_the_action_log():
    from app.services.video_studio.capture import step_label

    typed = spec.TakeStep(action="type", selector="#password", text="hunter2")
    assert step_label(typed) == "#password"
    assert step_label(spec.TakeStep(action="type", text="hunter2")) == "a field"
    assert step_label(spec.TakeStep(action="type", text="hunter2", label="Password")) == "Password"
    # A click's text is the element it targets: still its name.
    assert step_label(spec.TakeStep(action="click", text="Save")) == "Save"


@pytest.mark.parametrize(
    ("value", "masked"),
    [(None, None), ("ana@example.com", "a***@example.com"), ("admin", "ad***"), ("ab", "***")],
)
def test_usernames_are_masked(value, masked):
    assert profiles.mask_username(value) == masked


# ── URL policy ────────────────────────────────────────────────────────────


def test_hosts_are_normalised():
    assert browser.normalise_hosts(["https://OpenCTI.internal:8080/dash", "opencti.internal", " sso.lan. ", ""]) == [
        "opencti.internal",
        "sso.lan",
    ]


def test_private_targets_stay_off_by_default(monkeypatch):
    monkeypatch.setattr(browser, "security_option", lambda name: False)
    policy = browser.target_policy(
        start_url="http://10.0.0.5/", login_url=None, allowed_hosts=["10.0.0.5", "opencti.internal"]
    )
    assert policy.private_hosts == frozenset()
    with pytest.raises(browser.TargetRefusedError, match="private network"):
        asyncio.run(browser.check_target("http://10.0.0.5/", policy))


def test_a_declared_private_host_is_reached_once_the_option_is_on(monkeypatch):
    monkeypatch.setattr(browser, "security_option", lambda name: name == "browser_private_targets")
    policy = browser.target_policy(start_url="http://10.0.0.5/app", login_url=None, allowed_hosts=[])
    assert policy.admits_private("10.0.0.5")
    assert asyncio.run(browser.check_target("http://10.0.0.5/app", policy)) == "http://10.0.0.5/app"
    # Metadata and undeclared hosts stay refused.
    with pytest.raises(browser.TargetRefusedError):
        asyncio.run(browser.check_target("http://169.254.169.254/latest/meta-data", policy))
    with pytest.raises(browser.TargetRefusedError):
        asyncio.run(browser.check_target("http://10.0.0.6/", policy))


def test_the_route_filter_blocks_schemes_and_literal_internal_addresses():
    policy = browser.TargetPolicy(frozenset({"10.0.0.5"}))
    assert browser._blocked_by_route("file:///etc/passwd", policy)
    assert browser._blocked_by_route("http://127.0.0.1/", policy)
    assert browser._blocked_by_route("http://10.0.0.6/", policy)
    assert not browser._blocked_by_route("http://10.0.0.5/", policy)
    assert not browser._blocked_by_route("https://93.184.215.14/", policy)
    assert not browser._blocked_by_route("data:image/png;base64,AAAA", policy)


def test_the_route_filter_resolves_a_host_name_once_and_refuses_an_internal_one(monkeypatch):
    """A subresource named by host is held to the policy, not left to the redirect guard."""
    monkeypatch.setattr(browser, "security_option", lambda name: False)
    lookups: list[str] = []
    addresses = {"internal-service.corp": "10.0.0.7", "cdn.example.com": "93.184.215.14"}

    def fake_getaddrinfo(host, port, family=0, type=0, *args, **kwargs):
        lookups.append(host)
        return [(2, 1, 6, "", (addresses[host], port or 0))]

    import socket

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    route_filter = browser.RouteFilter(browser.target_policy(start_url=None, login_url=None, allowed_hosts=[]))

    async def run() -> list[bool]:
        return [
            await route_filter.admits("http://internal-service.corp/api"),
            await route_filter.admits("http://internal-service.corp/other"),
            await route_filter.admits("https://cdn.example.com/app.js"),
            await route_filter.admits("data:image/png;base64,AAAA"),
        ]

    assert asyncio.run(run()) == [False, False, True, True]
    assert lookups.count("internal-service.corp") == 1
