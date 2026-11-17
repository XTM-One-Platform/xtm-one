"""Footage fills its scene instead of freezing on its last frame.

A scene lasts as long as its narration. When an AI shot, an animated image,
a statement's backdrop video or a recording is shorter, the time used to be a
frozen last frame, right before the next scene - in the editor and the render
alike. Now:

* a shot plays slower to fill the scene, down to ``SLOWEST_FILL`` of its
  speed - never one whose own sound is heard;
* a recording first gives back the pauses its idle compression sped up (the
  actions keep their real time; a ``cut`` pause stays a jump cut);
* only what is still missing holds the last frame, and the compositor keeps
  that frame drifting in until the scene ends;
* the quality check says when a scene still holds long, with the fix;
* the scenes' lengths, and so every start, transition, subtitle and the
  music, do not move.
"""

from __future__ import annotations

import asyncio
import itertools
import os

import pytest

from app.models.video_studio import JOB_READY, JOB_RUNNING
from app.services.video_studio import compositor_page, quality, timeline
from app.services.video_studio.spec import parse_video_spec, validate_video_spec

CLIP_ID = "00000000-0000-0000-0000-0000000000c1"
TAKE_ID = "00000000-0000-0000-0000-0000000000a1"
FILE_ID = "00000000-0000-0000-0000-0000000000f1"


def _spec(*scenes: dict) -> object:
    return parse_video_spec(validate_video_spec({"title": "Film", "freestyle": True, "scenes": list(scenes)}))


def _shot(duration: float = 4.0, *, has_audio: bool = False) -> timeline.ClipMedia:
    return timeline.ClipMedia(
        duration=duration, width=1280, height=720, fps=24.0, frame_count=int(duration * 24), has_audio=has_audio
    )


#: A click at 2-3 s, then 12 s where nothing happens, then typing at 15-17 s and a click right after.
_EVENTS = [
    {"step": 0, "action": "goto", "t0": 0.0, "ta": 0.0, "t1": 1.0, "url": "https://demo.example.com/"},
    {
        "step": 1,
        "action": "click",
        "t0": 2.0,
        "ta": 2.6,
        "t1": 3.0,
        "box": {"x": 1300, "y": 80, "w": 120, "h": 40},
        "point": {"x": 1360, "y": 100},
    },
    {"step": 2, "action": "type", "t0": 15.0, "ta": 15.2, "t1": 17.0, "box": {"x": 200, "y": 600, "w": 400, "h": 40}},
    {
        "step": 3,
        "action": "click",
        "t0": 17.2,
        "ta": 17.4,
        "t1": 17.6,
        "box": {"x": 700, "y": 600, "w": 120, "h": 40},
        "point": {"x": 760, "y": 620},
    },
]


def _take(duration: float = 20.0) -> timeline.TakeMedia:
    return timeline.TakeMedia(
        take_id=TAKE_ID,
        duration=duration,
        css_width=1600,
        css_height=900,
        pixel_ratio=1.0,
        fps=60,
        frame_count=int(duration * 60),
        events=list(_EVENTS),
        # The pointer travels across the page the whole time: every sample is placed by the time map.
        cursor=[(t / 2, 200.0 + 30.0 * t, 450.0) for t in range(int(duration * 2) + 1)],
    )


def _scene(document: dict, scene_id: str) -> dict:
    return next(scene for scene in document["scenes"] if scene["id"] == scene_id)


def _rate(points: list) -> float:
    """How fast the footage plays where it moves (its first segment)."""
    return (points[1][1] - points[0][1]) / (points[1][0] - points[0][0])


def _held(points: list) -> float:
    return quality.held_seconds({"time_map": points})


# ── The rule ──────────────────────────────────────────────────────────────


def test_footage_plays_slower_to_fill_its_scene_down_to_the_slowest_fill():
    assert timeline.filled_length(4.0, 1.0, 3.0) == 4.0  # long enough: its own length
    assert timeline.filled_length(4.0, 1.0, 5.0) == 5.0  # slowed to 0.8x: the scene is filled
    assert timeline.filled_length(4.0, 1.0, 10.0) == pytest.approx(4.0 / timeline.SLOWEST_FILL)
    # Slowed from the speed its author asked for.
    assert timeline.filled_length(4.0, 2.0, 10.0) == pytest.approx(2.0 / timeline.SLOWEST_FILL)
    assert timeline.filled_length(4.0, 1.0, 10.0, stretch=False) == 4.0


# ── An AI shot ────────────────────────────────────────────────────────────


def _clip_plan(narration_seconds: float, *, heard: bool) -> dict:
    spec = _spec(
        {"id": "s1", "type": "clip", "clip_id": CLIP_ID, "narration": "Every link lights up.", "audio": heard},
        {"id": "s2", "type": "statement", "lines": ["Next"], "duration": 3},
    )
    media = timeline.PlanMedia(
        clips={"s1": _shot(has_audio=heard)}, voices={"s1": timeline.VoiceMedia(duration=narration_seconds)}
    )
    return timeline.plan(spec, media, fps=30, theme={}).document


def test_an_ai_shot_under_a_longer_narration_plays_slower_before_holding():
    scene = _scene(_clip_plan(4.5, heard=False), "s1")
    points, length = scene["time_map"], scene["end"] - scene["start"]
    assert points[0] == [0.0, 0.0] and points[-1][1] == 4.0 and points[-1][0] == pytest.approx(length)
    # The narration needs about 5.3 s with its lead and tail: the 4 s shot fills all of it, nothing held.
    assert 1.0 > _rate(points) > timeline.SLOWEST_FILL and _held(points) == pytest.approx(0.0, abs=1e-3)
    long_scene = _scene(_clip_plan(9.0, heard=False), "s1")
    # 9 s is past what slowing fills: slowed to SLOWEST_FILL, the rest held.
    assert _rate(long_scene["time_map"]) == pytest.approx(timeline.SLOWEST_FILL, abs=1e-3)
    held = long_scene["end"] - long_scene["start"] - 4.0 / timeline.SLOWEST_FILL
    assert _held(long_scene["time_map"]) == pytest.approx(held, abs=1e-3)


def test_a_shot_whose_sound_is_heard_plays_at_its_speed_and_no_scene_moves():
    silent, heard = _clip_plan(9.0, heard=False), _clip_plan(9.0, heard=True)
    shot = _scene(heard, "s1")
    # Slowed, its own sound would be slowed too: it plays at its speed and holds.
    assert _rate(shot["time_map"]) == pytest.approx(1.0) and shot["time_map"][1][0] == 4.0
    # Filling changes what plays inside the scene, never the scenes' times.
    assert [(s["start"], s["end"]) for s in silent["scenes"]] == [(s["start"], s["end"]) for s in heard["scenes"]]
    assert silent["captions"]["cues"] == heard["captions"]["cues"]


def test_an_author_hold_is_kept_and_the_shot_fills_the_rest():
    spec = _spec(
        {"id": "s1", "type": "clip", "clip_id": CLIP_ID, "narration": "Long.", "audio": False, "hold_end": 1.0}
    )
    media = timeline.PlanMedia(clips={"s1": _shot()}, voices={"s1": timeline.VoiceMedia(duration=4.5)})
    scene = _scene(timeline.plan(spec, media, fps=30, theme={}).document, "s1")
    assert _held(scene["time_map"]) == pytest.approx(1.0, abs=1e-3)
    assert _rate(scene["time_map"]) < 1.0
    # The compositor is told it is the author's: a still beat, which the drift leaves alone.
    assert scene["hold_end"] == 1.0
    assert "hold_end" not in _scene(_clip_plan(9.0, heard=False), "s1")


def test_an_animated_image_and_a_backdrop_video_fill_their_scenes():
    animated = _spec({"id": "s1", "type": "image", "file_id": FILE_ID, "duration": 5, "animate": {}})
    scene = _scene(timeline.plan(animated, timeline.PlanMedia(clips={"s1": _shot()}), fps=30, theme={}).document, "s1")
    assert scene["time_map"] == [[0.0, 0.0], [5.0, 4.0], [5.0, 4.0]]
    statement = _spec({"id": "s", "type": "statement", "lines": ["x"], "duration": 5, "backdrop": {"clip_id": CLIP_ID}})
    media = timeline.PlanMedia(backdrops={"s": _shot()})
    backdrop = _scene(timeline.plan(statement, media, fps=30, theme={}).document, "s")["backdrop"]
    assert backdrop["time_map"] == [[0.0, 0.0], [5.0, 4.0], [5.0, 4.0]]


# ── A recording ───────────────────────────────────────────────────────────


def _time_map(fill_to: float, idle_mode: str = "compress", *, speed: float = 1.0, events=None) -> list:
    return timeline.build_time_map(
        duration=20.0,
        trim_start=0.0,
        trim_end=None,
        speed=speed,
        idle_mode=idle_mode,
        idle_threshold=1.2,
        idle_speed=6.0,
        events=_EVENTS if events is None else events,
        fill_to=fill_to,
    )


def _span(points: list, a: float, b: float) -> float:
    """The output seconds recording seconds *a* to *b* play in."""
    return timeline.map_inverse(points, b) - timeline.map_inverse(points, a)


def test_a_recording_slows_its_compressed_pauses_before_holding():
    rushed = _time_map(0.0)
    assert rushed[-1][0] < 12.0  # the 12 s pause (3.9-14.7 s, kept 0.25 s at each edge) sped up 6x
    relaxed = _time_map(13.0)
    assert relaxed[-1] == (pytest.approx(13.0), 20.0)
    # The actions keep their real time: the click still lasts one second.
    assert _span(relaxed, 2.0, 3.0) == pytest.approx(1.0, abs=0.01)
    # A pause the author compressed still reads compressed: never slower than RELAXED_PAUSE_SPEED, the rest
    # left to the hold, however long the narration.
    longest = _time_map(30.0)
    assert _span(longest, 4.15, 14.45) == pytest.approx(10.3 / timeline.RELAXED_PAUSE_SPEED, abs=0.01)
    assert longest[-1][0] < 15.0


def test_a_faster_recording_slows_its_pauses_at_its_own_speed():
    rushed, relaxed = _time_map(0.0, speed=2.0), _time_map(6.5, speed=2.0)
    assert rushed[-1][0] < 6.0 and relaxed[-1] == (pytest.approx(6.5), 20.0)
    assert _span(relaxed, 2.0, 3.0) == pytest.approx(0.5, abs=0.01)  # the click at the recording's speed
    longest = _time_map(30.0, speed=2.0)
    assert _span(longest, 4.15, 14.45) == pytest.approx(10.3 / (timeline.RELAXED_PAUSE_SPEED * 2.0), abs=0.01)


def test_a_cut_pause_and_a_skipped_step_stay_jump_cuts():
    assert _time_map(30.0, "cut") == _time_map(0.0, "cut")
    skipped = _time_map(60.0, events=[*_EVENTS, {"step": 4, "action": "click", "ok": False, "t0": 18.6, "t1": 19.8}])
    # The skipped step's wait (18.6-19.8 s) plays at CUT_SPEED whatever the narration needs.
    assert _span(skipped, 18.6, 19.8) == pytest.approx(1.2 / timeline.CUT_SPEED, abs=0.01)


def _recording(narration_seconds: float | None) -> dict:
    scene: dict = {"id": "r", "type": "recording", "take_id": TAKE_ID}
    voices = {}
    if narration_seconds is not None:
        scene["narration"] = "Watch the settings open."
        voices["r"] = timeline.VoiceMedia(duration=narration_seconds)
    media = timeline.PlanMedia(takes={TAKE_ID: _take()}, voices=voices)
    return _scene(timeline.plan(_spec(scene), media, fps=30, theme={}).document, "r")


def test_a_planned_recording_fills_its_narration_and_its_steps_follow():
    rushed, scene = _recording(None), _recording(12.5)
    points, length = [tuple(p) for p in scene["time_map"]], scene["end"] - scene["start"]
    assert points[-1][1] == 20.0 and _held(scene["time_map"]) == pytest.approx(0.0, abs=1e-3)
    assert points[-1][0] == pytest.approx(length) and length > rushed["end"] - rushed["start"]
    # Every step is drawn where the relaxed map puts it: the click after the pause comes later than when
    # the pause was rushed, the one before it does not move.
    clicks = scene["cursor"]["clicks"]
    assert [c[0] for c in clicks] == pytest.approx([timeline.map_inverse(points, ta) for ta in (2.6, 17.4)], abs=1e-3)
    assert clicks[0][0] == rushed["cursor"]["clicks"][0][0] and clicks[1][0] > rushed["cursor"]["clicks"][1][0]
    samples = scene["cursor"]["samples"]
    assert all(a[0] <= b[0] for a, b in itertools.pairwise(samples)) and samples[-1][0] <= length + 1e-3
    # The camera and the address bar never run past the scene, nor back in time.
    camera = [k[0] for k in scene["camera"]]
    assert camera == sorted(camera) and camera[-1] <= length + 1e-3
    assert all(0.0 <= u[0] <= length + 1e-3 for u in scene["urls"])


# ── What the quality check says ───────────────────────────────────────────


def _findings(spec, document, facts) -> list:
    return [f for f in quality.content_findings(spec, document, facts, None) if f.kind == "held_frame"]


def test_a_shot_still_holding_long_is_a_finding_with_its_fixes():
    spec = _spec({"id": "s1", "type": "clip", "clip_id": CLIP_ID, "narration": "Long.", "audio": False})
    media = timeline.PlanMedia(clips={"s1": _shot()}, voices={"s1": timeline.VoiceMedia(duration=9.0)})
    document = timeline.plan(spec, media, fps=30, theme={}).document
    ready = quality.ContentFacts(shots={CLIP_ID: quality.ShotFacts("Cat host", JOB_READY)})
    [finding] = _findings(spec, document, ready)
    assert finding.severity == "advisory" and finding.scene_id == "s1"
    # Every content finding says so to the editor, which words its severity for it and keeps no list of kinds.
    assert finding.as_dict()["group"] == "content"
    assert {f.group for f in quality.content_findings(spec, document, ready, None)} == {"content"}
    assert quality.QualityFinding(0, "s1", "clip", "", "overlap", "dense", "covers").group == "layout"
    assert 'its AI shot "Cat host" is shorter than its narration: played slowed to 70% of its speed' in finding.message
    assert f"reference_clip_id `{CLIP_ID}`" in finding.message and "duration_seconds" in finding.message
    # A shot still being made is planned at the length it was asked for: nothing to say yet.
    making = quality.ContentFacts(shots={CLIP_ID: quality.ShotFacts("Cat host", JOB_RUNNING)})
    assert _findings(spec, document, making) == []
    # A short hold is the drift's: no finding.
    short = timeline.PlanMedia(clips={"s1": _shot()}, voices={"s1": timeline.VoiceMedia(duration=5.5)})
    assert _findings(spec, timeline.plan(spec, short, fps=30, theme={}).document, ready) == []


def test_a_recording_still_holding_long_is_a_finding_once_it_is_recorded():
    spec = _spec({"id": "r", "type": "recording", "take_id": TAKE_ID, "narration": "Long."})
    media = timeline.PlanMedia(takes={TAKE_ID: _take()}, voices={"r": timeline.VoiceMedia(duration=24.0)})
    document = timeline.plan(spec, media, fps=30, theme={}).document
    assert _findings(spec, document, quality.ContentFacts()) == []
    [finding] = _findings(spec, document, quality.ContentFacts(ready_takes={TAKE_ID}))
    assert finding.message.startswith("its recording ends about")
    assert "record more of the flow" in finding.message


# ── The compositor ────────────────────────────────────────────────────────


def test_the_compositor_keeps_a_held_frame_drifting_to_the_end_of_its_scene():
    js = compositor_page.PAGE_JS
    for hook in ("function heldSpan(points, still)", "function heldDrift(span, t)", "driftShot(shot, held, t);"):
        assert hook in js
    # The author's own hold_end stays a still beat, on a clip and on a recording alike (``driftSpan``: their
    # ``extend_mode: zoom``).
    assert "heldSpan(sc.time_map, sc.hold_end)" in js and js.count("= driftSpan(sc)") + js.count("? driftSpan(sc)") == 2
    # The recording's camera zooms in with it, so its cursor and callouts follow.
    assert "const z = zoom * heldDrift(held, t);" in js
    assert ".shot { position: absolute; inset: 0; transform-origin: 50% 50%; }" in compositor_page.PAGE_CSS


@pytest.mark.chromium
def test_a_held_frame_drifts_in_a_real_compositor():
    from app.services.builtin_tools.html_render import font_face_css
    from app.services.video_studio.themes import build_theme

    scene = {
        "id": "s1",
        "type": "clip",
        "transition_in": {"type": "cut", "duration": 0},
        "overlays": [],
        "start": 0.0,
        "end": 8.0,
        "src": "clip/s1",
        "fit": "cover",
        "time_map": [[0, 0], [5, 4], [8, 4]],
        "src_fps": 24,
        "src_frames": 96,
        "src_w": 1280,
        "src_h": 720,
    }
    document = {
        "version": 1,
        "width": 1280,
        "height": 720,
        "fps": 30,
        "duration": 8.0,
        "frames": 240,
        "fade_in": 0.0,
        "fade_out": 0.0,
        "theme": build_theme(mode="dark", background_style="grid"),
        "captions": {"enabled": False, "style": "pill", "position": "bottom", "cues": []},
        "watermark": "none",
        "scenes": [scene],
    }

    # Frames 60 (2 s, moving), 150 (5 s, the hold starts), then 5.3, 6.5, 7.0, 7.5 and 7.97 s.
    frames = (60, 150, 160, 195, 210, 225, 239)
    authored = {**document, "scenes": [{**scene, "hold_end": 1.0}]}

    async def scales() -> list[list[str]]:
        from playwright.async_api import async_playwright

        html = compositor_page.page_html(font_face_css()).encode("utf-8")
        index = f"{compositor_page.ORIGIN}/index.html"

        async def handle(route) -> None:
            if route.request.url == index:
                await route.fulfill(status=200, body=html, content_type="text/html; charset=utf-8")
            else:
                await route.abort("blockedbyclient")

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1280, "height": 720})
                await page.route("**/*", handle)
                await page.goto(index)
                runs = []
                for doc in (document, authored):
                    await page.evaluate("doc => window.__xtm.load(doc)", doc)
                    seen = []
                    for frame in frames:
                        await page.evaluate("i => window.__xtm.render(i)", frame)
                        seen.append(await page.evaluate("() => document.querySelector('.shot').style.transform"))
                    runs.append(seen)
                return runs
            finally:
                await browser.close()

    try:
        planned, still = asyncio.run(scales())
    except Exception as exc:
        if "Executable doesn't exist" not in str(exc) and "playwright install" not in str(exc).lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")

    def scale(transform: str) -> float:
        return float(transform.removeprefix("scale(").removesuffix(")")) if transform else 1.0

    # Moving footage is never scaled; once held, the frame grows until the scene's last frame.
    assert planned[0] == "" and planned[1] == ""
    values = [scale(s) for s in planned[2:]]
    assert all(a < b for a, b in itertools.pairwise(values)) and values[0] > 1.0 and values[-1] <= 1.0 + 0.08
    # The author's last second is a still beat: it keeps the scale the drift reached, never back to 1.
    held = [scale(s) for s in still[2:]]
    assert held[0] < held[1] < held[2] == held[3] == held[4] == pytest.approx(1.04)
