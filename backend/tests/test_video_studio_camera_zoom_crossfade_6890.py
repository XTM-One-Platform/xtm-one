"""An automatic camera zoom out on a recording scene is never drawn as two framings of one page blended.

The bug: the camera's last back-out of a recording scene was planned against the whole scene, so a zoom out placed
at its end ran under the crossfade into the next scene - the moving zoomed-in framing blended with the next scene's
full framing of the same page, the title bar, tabs and toolbar drawn twice, offset, for the length of the dissolve.

On fixtures, no browser: a synthetic recording of a static page with a sharp title bar, planned as the render plans it
(:func:`timeline.estimate_plan`), its camera applied to the page the way the compositor applies it (one geometric
transform - scale and translate - of the frame shown, ``compositor_page.camera`` and the recording scene's ``cam``
transform), sampled at 60 fps over every frame of the move and of the crossfade out:

* the camera zooms in, then backs out, and is still before the transition out starts: every frame shows one title bar;
* the same scene planned without the transition out (the code before the fix) draws it twice - the check sees the bug;
* a recording painted under the output rate and played slowed shows the nearest painted frame (never
  two blended) under the camera transform, so a page that changes between them still shows one title bar;
* the quality check's ``doubled_ui`` finding (``doubled_ui.ghost_edges``) fires on a fixture frame blending two scaled
  copies of the page and stays silent on a clean geometric zoom, and refuses a render of a plan whose camera still
  moves under the crossfade (dense: ``render_checks.BLOCKING_SEVERITIES``).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from app.services.video_studio import doubled_ui, quality, screen_insert, templates, timeline
from app.services.video_studio.spec import authored_spec, parse_video_spec

TAKE = "00000000-0000-0000-0000-000000006890"
OUT_FPS = 60
#: The canvas the camera is applied on, small enough to sample every frame of a move.
W, H = 320, 180
BAR, PAGE, PANEL = 40.0, 240.0, 150.0
BAR_ROWS = 30
CROSSFADE = 0.5


def _take(*, fps: int = 60, sparse: list | None = None, seconds: float = 8.0, click_at: float = 5.5) -> timeline.TakeMedia:
    """A recording of one page: a click on a small element near its middle *click_at* seconds in, which the automatic
    camera moves in on; *sparse* the stretches it painted under its rate (``capture_rate.sparse_spans``)."""
    box = {"x": 700, "y": 400, "w": 200, "h": 100}
    event = {
        "step": 0,
        "action": "click",
        "t0": round(click_at - 0.4, 3),
        "ta": click_at,
        "t1": round(click_at + 0.1, 3),
        "ok": True,
        "box": box,
        "point": {"x": 800, "y": 450},
    }
    return timeline.TakeMedia(
        TAKE, seconds, 1600, 900, 1.0, fps, int(seconds * fps), events=[event], sparse=list(sparse or [])
    )


def _plan(take: timeline.TakeMedia, *, speed: float = 1.0) -> tuple[dict, dict, object]:
    """The recording scene with an automatic camera, crossfading into a second scene of the same page: the two
    planned scenes and the parsed first scene."""
    spec, plan = _planned(take, speed=speed)
    first, second = plan.document["scenes"]
    return first, second, spec.scenes[0]


def _planned(take: timeline.TakeMedia, *, speed: float = 1.0) -> tuple[object, object]:
    """The parsed spec of :func:`_plan`'s two scenes and its plan."""
    camera = {"mode": "auto", "max_zoom": 1.2, "pace": "calm"}
    raw = {
        "title": "Zoom out",
        "freestyle": True,
        "scenes": [
            {"id": "a", "type": "recording", "take_id": TAKE, "idle": {"mode": "off"}, "speed": speed, "camera": camera},
            {
                "id": "b",
                "type": "recording",
                "take_id": TAKE,
                "idle": {"mode": "off"},
                "camera": {"mode": "static"},
                "transition": {"type": "crossfade", "duration": CROSSFADE},
            },
        ],
    }
    spec = parse_video_spec(templates.apply_template(authored_spec(raw), None).spec)
    return spec, timeline.estimate_plan(spec, {TAKE: take})


# ── The compositor's camera, as compositor_page draws it ──────────────────


def _glide(p: float) -> float:
    return p * p * p * (p * (6 * p - 15) + 10)


def _camera(frames: list[list[float]], t: float) -> tuple[float, float, float]:
    """``compositor_page.camera``: the view ``(cx, cy, zoom)`` at *t*, eased, the zoom moving in log space."""
    if t <= frames[0][0]:
        return frames[0][1], frames[0][2], frames[0][3]
    if t >= frames[-1][0]:
        return frames[-1][1], frames[-1][2], frames[-1][3]
    i = max(k for k in range(len(frames)) if frames[k][0] <= t)
    a, b = frames[i], frames[i + 1]
    p = _glide((t - a[0]) / max(b[0] - a[0], 1e-6))
    arc = b[4] if len(b) > 4 else 0.0
    z = math.exp(math.log(a[3]) + (math.log(b[3]) - math.log(a[3])) * p - arc * math.sin(math.pi * p))
    return a[1] + (b[1] - a[1]) * p, a[2] + (b[2] - a[2]) * p, z


def _transform(view: tuple[float, float, float]) -> tuple[float, float, float]:
    """The recording scene's ``cam`` transform for *view*, on a footage filling the canvas: ``(tx, ty, z)``."""
    cx, cy, z = view
    fx, fy = cx * W, cy * H
    k = min(max((z - 1) / 0.5, 0.0), 1.0)
    tx = fx + (W / 2 - fx) * k - z * fx
    ty = fy + (H / 2 - fy) * k - z * fy
    if z * W >= W:
        tx = min(max(tx, W - z * W), 0.0)
    if z * H >= H:
        ty = min(max(ty, H - z * H), 0.0)
    return tx, ty, z


def _page(panel: float = 0.0) -> np.ndarray:
    """The recorded page: a sharp dark title bar over a light page, a side panel *panel* of the way open."""
    page = np.full((H, W), PAGE)
    page[:BAR_ROWS] = BAR
    if panel > 0:
        page[BAR_ROWS:, W - round(W * 0.3 * panel) :] = PANEL
    return page


def _shot(page: np.ndarray, view: tuple[float, float, float]) -> np.ndarray:
    """*page* drawn through the camera at *view*: one geometric transform of one picture, sampled bilinearly."""
    tx, ty, z = _transform(view)
    ys, xs = np.mgrid[0:H, 0:W].astype(float)
    sx, sy = np.clip((xs - tx) / z, 0, W - 1), np.clip((ys - ty) / z, 0, H - 1)
    x0, y0 = np.floor(sx).astype(int), np.floor(sy).astype(int)
    x1, y1 = np.minimum(x0 + 1, W - 1), np.minimum(y0 + 1, H - 1)
    fx, fy = sx - x0, sy - y0
    top = page[y0, x0] * (1 - fx) + page[y0, x1] * fx
    bottom = page[y1, x0] * (1 - fx) + page[y1, x1] * fx
    return top * (1 - fy) + bottom * fy


def _title_bars(frame: np.ndarray) -> int:
    """How many title bar edges (dark above, light below) the left of *frame* shows: 1 for one framing of the page,
    2 for two framings blended offset."""
    profile = frame[:, : W // 3].mean(axis=1)
    strong = np.diff(profile) > 0.2 * (PAGE - BAR)
    return int(np.count_nonzero(strong[1:] & ~strong[:-1]) + int(strong[0]))


def _map_time(time_map: list[list[float]], t: float) -> float:
    xs, ys = zip(*time_map, strict=True)
    return float(np.interp(t, xs, ys))


def _output_frames(start: float, end: float) -> list[float]:
    return [n / OUT_FPS for n in range(math.ceil(start * OUT_FPS), math.floor(end * OUT_FPS) + 1)]


def _frames_of_the_move_and_the_crossfade(first: dict, second: dict, camera: list[list[float]]) -> list[np.ndarray]:
    """Every output frame from the camera's first move to the end of the crossfade out, at 60 fps: the first scene
    through *camera*, blended over the crossfade with the second scene's wide framing of the same page."""
    page = _page()
    moves_from = camera[1][0] if len(camera) > 1 else 0.0
    fade_from, fade_until = float(second["start"]) - float(first["start"]), float(first["end"]) - float(first["start"])
    shots = []
    for t in _output_frames(moves_from, fade_until):
        shot = _shot(page, _camera(camera, t))
        if t >= fade_from:
            p = (t - fade_from) / max(fade_until - fade_from, 1e-6)
            shot = shot * (1 - p) + _shot(page, (0.5, 0.5, 1.0)) * p
        shots.append(shot)
    return shots


# ── 1. Zoom in, then back out before the transition out ──────────────────


def test_the_camera_zooms_in_then_is_back_out_before_the_crossfade_out():
    first, second, _scene = _plan(_take())
    camera = first["camera"]
    fade_from = float(second["start"]) - float(first["start"])

    assert second["transition_in"]["type"] == "crossfade"
    assert max(frame[3] for frame in camera) > 1.1, "the camera moves in on the click"
    assert camera[-1][3] == 1.0 and camera[-1][0] <= fade_from + 1e-3, "and is wide again before the crossfade"


def test_no_frame_of_the_zoom_in_the_zoom_out_or_the_crossfade_shows_two_title_bars():
    first, second, _scene = _plan(_take())
    shots = _frames_of_the_move_and_the_crossfade(first, second, first["camera"])

    assert len(shots) > OUT_FPS
    doubled = [n for n, shot in enumerate(shots) if _title_bars(shot) != 1]
    assert doubled == []


def test_the_camera_planned_into_the_crossfade_draws_the_page_twice():
    """The check sees the bug: the scene's camera planned against its whole length, as before the fix."""
    first, second, scene = _plan(_take())
    before_the_fix, _moves = timeline.recording_camera(scene, _take(), first["time_map"], 0.0)
    fade_from = float(second["start"]) - float(first["start"])
    assert before_the_fix[-1][0] > fade_from + 0.1, "the zoom out ends under the crossfade"

    shots = _frames_of_the_move_and_the_crossfade(first, second, before_the_fix)
    assert any(_title_bars(shot) == 2 for shot in shots)


@pytest.mark.parametrize(("length", "fade", "settled"), [(8.0, 0.5, 7.5), (8.0, 0.0, 8.0), (2.0, 1.5, 1.0)])
def test_the_camera_is_settled_before_the_transition_out_never_before_half_the_scene(length, fade, settled):
    assert timeline.camera_settled_before(length, fade) == pytest.approx(settled)


# ── 2. A recording painted under the output rate ─────────────────────────


def test_a_slowed_low_fps_recording_shows_its_nearest_painted_frame_under_one_camera_transform():
    painted = [round(n * 0.25, 2) for n in range(33)]  # 4 painted frames a second, the video at 20 fps
    take = _take(fps=20, sparse=[[0.0, 8.0, painted]])
    first, second, _scene = _plan(take, speed=0.8)
    camera, time_map, kept = first["camera"], first["time_map"], first.get("painted")
    fade_from = float(second["start"]) - float(first["start"])
    assert take.fps * 3 <= OUT_FPS and kept and "blend" not in first, "the painted instants are planned, no blend"
    assert max(frame[3] for frame in camera) > 1.1 and camera[-1][0] <= fade_from + 1e-3

    def nearest(source: float) -> float:
        # ``compositor_page.nearestPainted``: one painted instant, never two blended.
        return min(kept[0][2], key=lambda instant: (abs(instant - source), -instant))

    doubled = []
    for t in _output_frames(camera[1][0], fade_from):
        # The page changes between the painted frames: a side panel opening across the scene.
        shown = _page(nearest(_map_time(time_map, t)) / 8.0)
        if _title_bars(_shot(shown, _camera(camera, t))) != 1:
            doubled.append(round(t, 3))
    assert doubled == []


# ── 3. The quality check: a page drawn twice ─────────────────────────────


@pytest.mark.parametrize("zoom", [1.1, 1.2, 1.3])
@pytest.mark.parametrize("weight", [0.2, 0.5, 0.8])
def test_a_frame_blending_two_scaled_copies_of_the_page_is_a_doubled_ui(zoom, weight):
    page = doubled_ui.chrome_page()
    blended = doubled_ui.drawn(page, (0.5, 0.5, zoom)) * (1 - weight) + doubled_ui.drawn(page, (0.5, 0.5, 1.0)) * weight

    [ghost] = doubled_ui.ghost_edges(blended)
    # The zoomed copy's title bar edge sits first: the step between the two edges is its share of the blend.
    assert ghost.second - ghost.first >= 4 and ghost.weight == pytest.approx(1 - weight, abs=0.05)


@pytest.mark.parametrize("view", [(0.5, 0.5, 1.0), (0.5, 0.5, 1.05), (0.5, 0.5, 1.2), (0.3, 0.1, 1.2), (0.7, 0.9, 1.5)])
def test_a_clean_geometric_zoom_is_no_doubled_ui(view):
    assert doubled_ui.ghost_edges(doubled_ui.drawn(doubled_ui.chrome_page(), view)) == []


def test_the_doubled_ui_check_draws_the_camera_as_the_compositor_does():
    """The check's camera and transform are this file's model of ``compositor_page``: one geometric transform."""
    frames = [[0.0, 0.5, 0.5, 1.0], [1.0, 0.6, 0.4, 1.2, 0.05], [2.0, 0.5, 0.5, 1.0]]
    for t in (0.0, 0.3, 0.5, 1.0, 1.4, 2.5):
        assert doubled_ui.view_at(frames, t) == pytest.approx(_camera(frames, t))
        assert doubled_ui.placed(_camera(frames, t)) == pytest.approx(_transform(_camera(frames, t)))


def test_a_plan_whose_camera_is_still_before_the_crossfade_draws_no_doubled_ui():
    first, second, _scene = _plan(_take())
    assert doubled_ui.doubled_under_transition(first, second) == []


def test_a_plan_whose_camera_moves_under_the_crossfade_is_a_doubled_ui():
    first, second, scene = _plan(_take())
    first["camera"], _moves = timeline.recording_camera(scene, _take(), first["time_map"], 0.0)

    doubled = doubled_ui.doubled_under_transition(first, second)
    fade_from = float(second["start"]) - float(first["start"])
    assert doubled and all(fade_from - 1e-3 <= at <= float(first["end"]) - float(first["start"]) + 1e-3 for at in doubled)
    # A cut blends nothing.
    assert doubled_ui.doubled_under_transition(first, {**second, "transition_in": {"type": "cut", "duration": 0}}) == []


def test_the_quality_check_refuses_a_render_whose_page_is_drawn_twice():
    from app.services.video_studio.render_checks import BLOCKING_SEVERITIES

    spec, plan = _planned(_take())
    assert quality._doubled_pages(spec, plan.document["scenes"]) == []

    first, _second = plan.document["scenes"]
    first["camera"], _moves = timeline.recording_camera(spec.scenes[0], _take(), first["time_map"], 0.0)
    [(scene_id, words)] = quality._doubled_pages(spec, plan.document["scenes"])
    assert scene_id == "a" and "twice" in words and "scene b" in words
    findings = quality.content_findings(spec, plan.document, quality.ContentFacts(), None)
    [finding] = [f for f in findings if f.kind == "doubled_ui"]
    assert finding.severity in BLOCKING_SEVERITIES and finding.scene_id == "a"


def test_older_renders_are_drawn_again_and_older_quality_reports_read_again():
    assert screen_insert.DRAWING_VERSION >= 15 and quality.QUALITY_REPORT_VERSION >= 28
