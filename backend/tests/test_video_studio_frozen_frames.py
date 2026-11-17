"""Frozen pictures, and the footage that no longer freezes. Synthetic frames only.

* a known frozen run is found with its frames, its time in the MP4 and its
  scene; one inside a card (title, closing card, statement) or an author's
  ``hold_end`` beat is not; one across a card keeps only its part outside it;
* the page's own drift on a held frame, even on a soft picture and
  at its slowest, is never a frozen run;
* the planner: footage shorter than its narration plays its ``extend_mode``
  - ``zoom`` by default (slowed to fill its scene, then drifting: the
  planner leaves it unmarked), ``loop`` its last second crossfaded, ``hold``
  its frame, neither slowed - on clips, animated images and recordings
  (which never loop);
* the page's drift switch and loop arithmetic, under node;
* the frame server keeps a loop's frames, so a loop never decodes backward.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import shutil
import subprocess

import numpy as np
import pytest

from app.services.video_studio import compositor, compositor_page, encoder, frame_freeze, timeline
from app.services.video_studio.spec import VideoSpecError, parse_video_spec, validate_video_spec

FPS = 30.0
H, W = 108, 192


def _texture(seed: int = 0, soft: bool = False) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = rng.uniform(0, 255, (H // 6, W // 6))
    picture = np.kron(base, np.ones((6, 6)))
    if soft:
        from scipy.ndimage import gaussian_filter

        picture = gaussian_filter(picture, 6)
    return picture


def _moving(count: int, start: int = 0) -> list[np.ndarray]:
    picture = _texture(1)
    return [np.roll(picture, start + i, axis=1).astype(np.uint8) for i in range(count)]


def _zoomed(picture: np.ndarray, scale: float) -> np.ndarray:
    from scipy.ndimage import map_coordinates

    ys, xs = np.mgrid[0:H, 0:W].astype(np.float64)
    cy, cx = (H - 1) / 2, (W - 1) / 2
    return map_coordinates(picture, [cy + (ys - cy) / scale, cx + (xs - cx) / scale], order=1).astype(np.uint8)


def test_a_known_frozen_run_is_found():
    frozen = _moving(1, 30)[0]
    frames = _moving(30) + [frozen] * 24 + _moving(15, 60)
    assert frame_freeze.frozen_runs(frames, FPS) == [(30, 54)]
    # Half a second or less is not frozen.
    assert frame_freeze.frozen_runs(_moving(10) + [frozen] * 15 + _moving(5, 40), FPS) == []


def _run_page(tmp_path, body: str, *, between: tuple[str, str], also: tuple[str, str] | None = None) -> dict:
    """*body* run under node after the page's helpers and the functions *between* (and *also*) its two markers."""
    source = compositor_page.PAGE_JS
    start = source.index("const clamp = ")
    script = source[start : source.index("const el = ", start)]
    script += source[source.index("function mapTime(") : source.index("function camera(")]
    for first, last in (between, also) if also else (between,):
        script += source[source.index(first) : source.index(last)]
    path = tmp_path / "page.js"
    path.write_text(script + body, encoding="utf-8")
    result = subprocess.run(  # noqa: S603
        [shutil.which("node") or "node", str(path)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_pages_drift_on_a_held_frame_is_never_frozen_even_on_a_soft_picture(tmp_path):
    # The scale the page itself draws (`heldDrift`) over a 2 s, a 4 s and a 10 s hold - the last its slowest pace.
    holds = (2, 4, 10)
    scales = _run_page(
        tmp_path,
        f"""
const out = {{}};
for (const hold of {json.dumps(list(holds))}) {{
  const span = heldSpan([[0, 0], [1, 1], [1 + hold, 1]], 0);
  out[hold] = Array.from({{ length: Math.round(hold * {FPS}) }}, (_, i) => heldDrift(span, 1 + i / {FPS}));
}}
console.log(JSON.stringify(out));
""",
        between=("const HELD_DRIFT", "function driftShot("),
    )
    for hold in holds:
        for picture in (_texture(2), _texture(3, soft=True)):
            frames = [_zoomed(picture, scale) for scale in scales[str(hold)]]
            # Eased in from rest: only its first frames may sit still, never past half a second.
            assert frame_freeze.frozen_runs(frames, FPS) == [], hold


DOCUMENT = {
    "scenes": [
        {"id": "open", "type": "title", "start": 0.0, "end": 3.0},
        {"id": "shot", "type": "clip", "start": 2.6, "end": 8.0},
        {"id": "claim", "type": "statement", "start": 7.6, "end": 11.0},
    ]
}


def test_a_frozen_run_is_timed_in_its_scene_and_cards_are_left_alone():
    findings = frame_freeze.findings_of_runs([(int(4.0 * FPS), int(5.5 * FPS))], FPS, DOCUMENT)
    assert [(f.kind, f.scene_id, f.at, f.end, f.severity) for f in findings] == [("frozen", "shot", 4.0, 5.5, "warn")]
    assert "frozen for 1.5 s (0:04.00 to 0:05.50) in a clip" in findings[0].message
    # Still inside a statement or a title: its design.
    assert frame_freeze.findings_of_runs([(int(8.0 * FPS), int(10.5 * FPS))], FPS, DOCUMENT) == []
    assert frame_freeze.findings_of_runs([(0, int(2.5 * FPS))], FPS, DOCUMENT) == []
    # Across a card: only what lies outside it, when over half a second.
    across = frame_freeze.findings_of_runs([(int(6.5 * FPS), int(9.0 * FPS))], FPS, DOCUMENT)
    assert [(f.at, f.end) for f in across] == [(6.5, 7.6)]


def test_what_is_still_by_design_is_never_frozen():
    document = {
        "scenes": [
            {"id": "still", "type": "image", "motion": "none", "start": 0.0, "end": 4.0},
            {"id": "moving", "type": "image", "motion": "ken_burns", "start": 4.0, "end": 8.0},
            {"id": "fig", "type": "clip", "figure": True, "start": 8.0, "end": 14.0},
            # A recording played over 6 s, then its last frame held 3 s (extend_mode hold).
            {
                "id": "demo",
                "type": "recording",
                "start": 14.0,
                "end": 23.0,
                "time_map": [[0.0, 0.0], [2.0, 2.0], [6.0, 9.0], [9.0, 9.0]],
            },
            # A clip whose author asked a 2 s still beat at its end (``hold_end``).
            {"id": "beat", "type": "clip", "hold_end": 2.0, "start": 23.0, "end": 30.0},
        ]
    }
    runs = [
        (int(t0 * FPS), int(t1 * FPS))
        for t0, t1 in ((0.5, 3.5), (5.0, 7.0), (9.0, 13.0), (15.0, 19.5), (20.5, 22.5), (26.0, 30.0))
    ]
    found = frame_freeze.findings_of_runs(runs, FPS, document)
    # An image asked still, a figure resting between its steps, a product at rest in its recording and the
    # author's still beat are design; a picture moving by design that froze, a recording's held end and a
    # clip frozen before its beat are not.
    assert [(f.scene_id, f.at, f.end) for f in found] == [
        ("moving", 5.0, 7.0),
        ("demo", 20.5, 22.5),
        ("beat", 26.0, 28.0),
    ]


def test_a_previews_findings_are_timed_in_its_own_mp4():
    findings = frame_freeze.findings_of_runs([(0, int(1.0 * FPS))], FPS, DOCUMENT, offset=4.0)
    assert findings[0].at == 0.0 and findings[0].scene_id == "shot"


def test_the_check_streams_the_frames(monkeypatch):
    frames = _moving(20) + [_moving(1, 99)[0]] * 30

    async def stream(_path, *, width, height, fps, start=0.0):
        assert (width, height, fps, start) == (192, 108, 30.0, 0.0)
        for frame in frames:
            yield frame.tobytes()

    monkeypatch.setattr(encoder, "iter_gray_frames", stream)
    document = {"scenes": [{"id": "shot", "type": "clip", "start": 0.0, "end": 5.0}]}
    check = asyncio.run(frame_freeze.check_frames("final.mp4", document=document, width=1280, height=720, fps=60))
    assert check.status == "findings" and check.summary == {"frames": 50, "fps": 30.0}
    assert check.findings[0].at == pytest.approx(20 / FPS)


def test_the_watch_says_how_far_it_is_every_window_of_video(monkeypatch):
    # A render's check that stalls is told from a long one by the stretch it last watched, and by the
    # window it reads, named with its scenes as it starts, and the end of the watch is said too.
    async def stream(_path, *, width, height, fps, start=0.0):
        for frame in _moving(75):  # 2.5 s at 30 frames a second
            yield frame.tobytes()

    told: list[tuple[float, float, str | None]] = []

    async def watched(seconds: float, total: float, window) -> None:
        told.append((seconds, total, window.where() if window is not None else None))

    monkeypatch.setattr(encoder, "iter_gray_frames", stream)
    monkeypatch.setattr(frame_freeze, "WINDOW_SECONDS", 1.0)
    document = {"duration": 9.0, "scenes": [{"id": "shot", "type": "clip", "start": 0.0, "end": 9.0}]}
    asyncio.run(
        frame_freeze.check_frames("final.mp4", document=document, width=1280, height=720, fps=60, duration=2.5, watched=watched)
    )
    assert told == [
        (0.0, 2.5, "0:00.00 to 0:01.00, scene `shot` (clip)"),
        (1.0, 2.5, "0:01.00 to 0:02.00, scene `shot` (clip)"),
        (2.0, 2.5, "0:02.00 to 0:02.50, scene `shot` (clip)"),
        (2.5, 2.5, None),
    ]
    # Without the MP4's length, the plan's.
    told.clear()
    asyncio.run(frame_freeze.check_frames("final.mp4", document=document, width=1280, height=720, fps=60, watched=watched))
    assert told[0][:2] == (0.0, 9.0)


# ── The planner ───────────────────────────────────────────────────────────

CLIP = "00000000-0000-0000-0000-00000000c1c1"
TAKE = "00000000-0000-0000-0000-00000000a1a1"
LONG = "This narration is far longer than the four seconds of footage the clip scene shows on screen today."


def _spec(scene: dict) -> object:
    return parse_video_spec(validate_video_spec({"title": "Extend", "freestyle": True, "scenes": [scene]}))


def _clip_media() -> timeline.PlanMedia:
    media = timeline.PlanMedia()
    media.clips["c"] = timeline.ClipMedia(duration=4.0, width=1280, height=720, fps=24.0, frame_count=96)
    return media


def _planned_clip(**extra) -> dict:
    scene = {"id": "c", "type": "clip", "clip_id": CLIP, "narration": LONG, **extra}
    return timeline.plan(_spec(scene), _clip_media(), fps=30, theme={}).document


def test_a_clip_shorter_than_its_narration_fills_its_scene_by_default():
    planned = _planned_clip()["scenes"][0]
    # ``zoom``: slowed to fill the scene as far as it may, the page drifting in on what still holds.
    assert "extend" not in planned
    played, source = planned["time_map"][1]
    assert source == 4.0 and played == pytest.approx(4.0 / timeline.SLOWEST_FILL, abs=1e-3)
    assert planned["time_map"][-1][0] > played


def test_a_clip_can_loop_its_last_second_or_hold_its_frame_at_its_own_speed():
    looped = _planned_clip(extend_mode="loop", trim_end=3.5)
    planned = looped["scenes"][0]
    assert planned["time_map"][1] == [3.5, 3.5]
    assert planned["extend"] == {
        "mode": "loop",
        "from": 3.5,
        "to": planned["end"],
        "period": 1.0,
        "fade": 0.3,
        "src_from": 2.5,
        "speed": 1.0,
    }
    assert timeline.loop_frames(looped) == {"clip/c": (60, 84)}
    # The author's still beat keeps the frame the loop reached.
    beat = _planned_clip(extend_mode="loop", trim_end=3.5, hold_end=1.5)["scenes"][0]
    assert beat["extend"]["to"] == pytest.approx(beat["end"] - 1.5) and beat["hold_end"] == 1.5
    held = _planned_clip(extend_mode="hold")["scenes"][0]
    assert held["extend"] == {"mode": "hold"} and held["time_map"][1] == [4.0, 4.0]


def test_a_clip_long_enough_plays_no_extension():
    media = _clip_media()
    media.clips["c"] = timeline.ClipMedia(duration=30.0, width=1280, height=720, fps=24.0, frame_count=720)
    planned = timeline.plan(_spec({"id": "c", "type": "clip", "clip_id": CLIP, "narration": LONG}), media, fps=30, theme={})
    assert "extend" not in planned.document["scenes"][0]


def test_a_recording_fills_and_drifts_or_holds_and_never_loops():
    with pytest.raises(VideoSpecError):
        validate_video_spec(
            {"title": "x", "freestyle": True, "scenes": [{"type": "recording", "take_id": TAKE, "extend_mode": "loop"}]}
        )
    recording = parse_video_spec(
        validate_video_spec({"title": "x", "freestyle": True, "scenes": [{"type": "recording", "take_id": TAKE}]})
    ).scenes[0]
    assert recording.extend_mode == "zoom"
    assert timeline.extension("zoom", played=2.0, length=6.0, src_from=0.0, src_to=0.0) is None
    assert timeline.extension("hold", played=2.0, length=6.0, src_from=0.0, src_to=0.0) == {"mode": "hold"}
    # Footage that lasts its scene needs no extension at all.
    assert timeline.extension("hold", played=6.0, length=6.0, src_from=0.0, src_to=0.0) is None


def test_a_clip_too_short_to_loop_drifts_like_zoom():
    assert timeline.extension("loop", played=0.2, length=3.0, src_from=0.0, src_to=0.2) is None


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_page_drifts_only_by_default_and_crossfades_each_loop(tmp_path):
    out = _run_page(
        tmp_path,
        """
const time_map = [[0, 0], [3.5, 3.5], [10, 3.5]];
const spans = [{ time_map }, { time_map, extend: { mode: 'hold' } }, { time_map, hold_end: 2 }].map(driftSpan);
const z = [3.0, 3.5, 4.0, 6.0, 8.0, 10.0, 12.0].map(t => heldDrift(spans[0], t));
const loop = { mode: 'loop', from: 3.5, to: 9.0, period: 1.0, fade: 0.3, src_from: 2.5, speed: 1.0 };
const sc = { time_map };
const l = [3.2, 3.35, 3.5, 3.8, 3.92, 4.05, 9.0, 9.6].map(t => loopTimes(loop, sc, t));
console.log(JSON.stringify({ spans, z, l }));
""",
        between=("const HELD_DRIFT", "function driftShot("),
        also=("function driftSpan(", "function frameSource("),
    )
    # ``zoom`` (unmarked) drifts over the held end; a hold does not; the author's beat ends the drift early.
    assert out["spans"] == [[3.5, 10], None, [3.5, 8]]
    z = out["z"]
    assert z[0] == 1 and z[1] == 1 and z[-1] == z[-2] > 1
    # Every second of the hold moves: never a still stretch, never back.
    assert all(b > a for a, b in itertools.pairwise(z[1:-1]))
    under, over, alpha = out["l"][0]
    # From 3.2 s the first loop fades in over the clip's own end.
    assert under == pytest.approx(3.2) and over == pytest.approx(2.5) and alpha == pytest.approx(0.0)
    assert out["l"][1][2] == pytest.approx(0.5)
    # The loop runs the last second again; the next starts 0.3 s before it ends (every 0.7 s), crossfading.
    assert out["l"][3][1] == pytest.approx(3.1) and out["l"][3][2] == 1
    under, over, alpha = out["l"][4]
    assert over == pytest.approx(2.52) and under == pytest.approx(3.22) and alpha == pytest.approx(0.02 / 0.3)
    under, over, alpha = out["l"][5]
    assert over == pytest.approx(2.65) and under == pytest.approx(3.35) and alpha == pytest.approx(0.5)
    # Past ``to`` (the author's still beat) the loop keeps the frame it reached.
    assert out["l"][7] == out["l"][6]


class _Decoder:
    opened: list[float] = []

    def __init__(self, path, *, start=0.0, **_kwargs):
        self.start = start
        _Decoder.opened.append(start)

    async def open(self):
        return None

    async def frames(self):
        index = round(self.start * 24)
        while index < 96:
            yield f"frame-{index}".encode()
            index += 1

    async def close(self):
        return None


def test_the_frame_server_keeps_a_loops_frames(monkeypatch):
    monkeypatch.setattr(encoder, "FrameDecoder", _Decoder)
    _Decoder.opened = []
    decoder = compositor._SceneDecoder(compositor.FrameSource("clip.mp4", 24.0), keep=(60, 84))

    async def play():
        got = [await decoder.frame(i) for i in range(0, 85)]
        # The loop: back to 60, and both layers of the crossfade, again and again.
        for i in range(60, 85):
            got.append(await decoder.frame(i))
            got.append(await decoder.frame(min(84, i + 17)))
        return got

    got = asyncio.run(play())
    assert got[60] == b"frame-60" and got[-2] == b"frame-84"
    assert _Decoder.opened == [0.0]
