"""A canvas captured alone where the recording draws on the CPU, and motion mostly synthesised from keyframes.

On a worker without a GPU a graph's layout move was captured at 5 to 16 fps and each frame held to the next at the
take's rate: stepped. Here, on synthetic frames, a canvas transition the screencast painted at 8 fps is composited
from the canvas's own copies at 30 fps (``canvas_alone.composite``) - what the page draws over it kept, checked
against what the browser painted at rest -, and a transition left at 8 fps is a stretch of stepping motion
(``capture_rate.stepping``), the save finding ``stepping_motion`` naming its step.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.services.builtin_tools import video_studio as tool
from app.services.video_studio import canvas_alone, capture_rate, timeline
from app.services.video_studio.quality import ContentFacts, content_findings
from app.services.video_studio.spec import authored_spec, parse_video_spec

#: A 800x450 CSS px window at pixel ratio 2; the canvas stands at (100, 50), 500x300 CSS px, a legend drawn over it.
VIEWPORT_WIDTH, RATIO = 800, 2.0
CANVAS = (100.0, 50.0, 500.0, 300.0)
LEGEND = (480.0, 60.0, 100.0, 40.0)
SURFACE = "100,50,500,300"
BLOCK = (120, 90)


def _left(t: float) -> float:
    """Where the moving block stands in the canvas at *t* (CSS px from its left): a 1.5 s move from 1.0 s."""
    return 40 + 300 * min(max((t - 1.0) / 1.5, 0.0), 1.0)


def _device(v: float) -> int:
    return round(v * RATIO)


def _painted(t: float) -> np.ndarray:
    picture = np.full((900, 1600, 3), 12, dtype=np.uint8)
    x, y, w, h = CANVAS
    picture[_device(y) : _device(y + h), _device(x) : _device(x + w)] = 30
    left, top = x + _left(t), y + 120
    picture[_device(top) : _device(top + BLOCK[1]), _device(left) : _device(left + BLOCK[0])] = (220, 230, 240)
    lx, ly, lw, lh = LEGEND
    picture[_device(ly) : _device(ly + lh), _device(lx) : _device(lx + lw)] = (200, 60, 60)
    return picture


def _copy(t: float, *, blank: bool = False) -> np.ndarray:
    picture = np.full((300, 500, 3), 30, dtype=np.uint8)
    if not blank:
        left = round(_left(t))
        picture[120 : 120 + BLOCK[1], left : left + BLOCK[0]] = (220, 230, 240)
    return picture


def _frames(workdir: Path, times: list[float]) -> list[tuple[float, str]]:
    out = []
    for index, t in enumerate(times):
        name = f"f{index:05d}.jpg"
        Image.fromarray(_painted(t)).save(workdir / name, quality=92)
        out.append((round(t, 4), name))
    return out


def _copies(workdir: Path, times: list[float], *, blank: bool = False) -> list[canvas_alone.AloneFrame]:
    out = []
    for index, t in enumerate(times):
        name = f"c{index:06d}.jpg"
        Image.fromarray(_copy(t, blank=blank)).save(workdir / name, quality=92)
        out.append(canvas_alone.AloneFrame(round(t, 4), SURFACE, "canvas#graph", CANVAS, (LEGEND,), name))
    return out


def _events(**click) -> list[dict]:
    canvases = [["canvas#graph", list(CANVAS)]]
    return [
        {"step": 0, "action": "wait", "t0": 0.0, "ta": 0.0, "t1": 1.0, "ok": True, "canvases": canvases},
        {"step": 1, "action": "click", "label": "Layered layout", "t0": 1.0, "ta": 1.0, "t1": 2.7, "ok": True, "canvases": canvases, **click},
        {"step": 2, "action": "wait", "t0": 2.7, "ta": 2.7, "t1": 3.2, "ok": True, "canvases": canvases},
    ]


#: The screencast at rest before the move, at 8 fps through it, at rest after it; the canvas copied at 30 fps.
PAINTED = [0.3, 0.6, 0.9, *(1.0 + k / 8 for k in range(12)), 2.6, 2.8]
COPIED = [0.35, *(1.0 + k / 30 for k in range(46))]


def _read(workdir: Path, name: str) -> np.ndarray:
    with Image.open(workdir / name) as image:
        return np.asarray(image.convert("RGB"), dtype=np.int16)


def test_a_transition_painted_at_8_fps_is_composited_from_its_copies_at_30(tmp_path):
    frames = _frames(tmp_path, PAINTED)
    events = _events()
    done = canvas_alone.composite(
        frames, _copies(tmp_path, COPIED), events, fps=30, pixel_ratio=RATIO, workdir=str(tmp_path), offsets={n: 0.0 for _t, n in frames}
    )

    [run] = done.runs
    assert run["composited"] is True and run["step"] == 1 and run["canvas"] == "canvas#graph"
    # No copy skipped: said as 0 and [] all the same, the record keeps one shape (#10970, retest c3a8b4a6).
    assert (run["skipped"], run["skipped_windows"]) == (0, [])
    assert run["copies"] == 46 and run["fps"] == pytest.approx(30.0, abs=0.5) and run["painted"] == pytest.approx(8.0, abs=0.1)
    composited = [(t, name) for t, name in done.frames if name.startswith(capture_rate.COMPOSITED_PREFIX)]
    assert len(composited) == 46
    # The frames painted over the run's span stand behind its copies; those at rest about it are kept.
    assert [t for t, name in done.frames if name.startswith("f")] == [0.3, 0.6, 0.9, 2.6, 2.8]
    assert set(done.painted) == {name for _t, name in composited}

    name = next(name for t, name in composited if abs(t - 1.7) < 1e-3)
    picture = _read(tmp_path, name)
    # The legend the page draws over the canvas is kept from the frame painted behind it ...
    lx, ly, lw, lh = LEGEND
    assert np.abs(picture[_device(ly + lh / 2), _device(lx + lw / 2)] - (200, 60, 60)).max() < 16
    # ... the block stands where its copy at 1.7 s has it, not where the frame painted at 1.625 s did.
    top = _device(CANVAS[1] + 120 + BLOCK[1] / 2)
    assert np.abs(picture[top, _device(CANVAS[0] + _left(1.7) + BLOCK[0] - 5)] - (220, 230, 240)).max() < 16
    assert np.abs(picture[top, _device(CANVAS[0] + _left(1.625) + 5)] - 30).max() < 16
    # Outside the canvas, the page as painted.
    assert np.abs(picture[880, 20] - 12).max() < 8

    # Measured on the frames composited: the transition moved at the copies' rate, captured alone, and nothing steps.
    captured = capture_rate.measure(
        done.frames, events, duration=3.2, fps=30, workdir=str(tmp_path), viewport_width=VIEWPORT_WIDTH, painted=done.frames
    )
    [moved] = events[1]["canvas_motion"]
    assert moved["alone"] is True and moved["fps"] >= 25, moved
    assert capture_rate.canvas_words(events[1]["canvas_motion"]).endswith("captured alone")
    assert captured["stepping"] == [] and captured["low"] == []
    # The second wholly inside the move kept a frame of the take's rate each instant.
    assert captured["rates"][1] >= 25, captured["rates"]
    [note] = canvas_alone.notes(done.runs)
    assert note.startswith("Captured alone where the screencast fell short (software GL): canvas#graph at step 1 (1.5 s")


def test_the_same_transition_left_at_8_fps_is_stepping_motion_at_its_step(tmp_path):
    frames = _frames(tmp_path, PAINTED)
    events = _events()
    recorded = {"gl": {"gpu": False, "why": "Chromium found no GPU to draw WebGL on: webgl unavailable_software"}}
    captured = capture_rate.measure(
        frames, events, duration=3.2, fps=30, workdir=str(tmp_path), recorded=recorded, viewport_width=VIEWPORT_WIDTH
    )

    [stretch] = captured["stepping"]
    assert (stretch["step"], stretch["canvas"], stretch["fps"], stretch["gl"]) == (1, "canvas#graph", 30, "software")
    assert stretch["share"] > capture_rate.STEPPING_SHARE
    assert stretch["synthesised"] == stretch["frames"] - events[1]["canvas_motion"][0]["frames"]
    assert events[1]["synthesised"] == pytest.approx(stretch["share"], abs=0.01)
    seconds = stretch["t"][1] - stretch["t"][0]
    words = (
        f"stepping motion at step 1 (canvas#graph moved {seconds:.1f} s: {stretch['synthesised']} of its "
        f"{stretch['frames']} frames at 30 fps synthesised from keyframes)"
    )
    assert capture_rate.stepping_words(captured) == [words]
    # The notes say it once - its low stretch is the same one - and name a GPU worker.
    notes = capture_rate.motion_notes(captured)
    assert notes[0] == f"{words[:1].upper()}{words[1:]}: its motion will step."
    assert not any("low capture rate" in note for note in notes)
    assert "a worker whose Chromium draws on a GPU" in notes[-1]


def test_a_transition_the_screencast_painted_at_its_rate_keeps_its_frames(tmp_path):
    frames = _frames(tmp_path, [0.3, *(1.0 + k / 30 for k in range(46)), 2.6])
    done = canvas_alone.composite(frames, _copies(tmp_path, COPIED), _events(), fps=30, pixel_ratio=RATIO, workdir=str(tmp_path))
    [run] = done.runs
    assert (run["composited"], run["why"]) == (False, canvas_alone.PAINTED_AT_RATE)
    # A run kept as painted, with its why, says its zero skips too (#10970, retest c3a8b4a6).
    assert (run["skipped"], run["skipped_windows"]) == (0, [])
    assert done.frames == frames


def test_frames_handed_over_at_the_take_rate_that_moved_the_canvas_on_one_of_two_are_composited(tmp_path):
    """#9266: the screencast handed over 30 frames a second while the canvas changed on every other one (14 fps): the
    run counted as painted at its rate and kept stepping. Only the frames that moved the canvas count."""
    times = [0.3, *(1.0 + k / 30 for k in range(46)), 2.6]
    frames = []
    for index, t in enumerate(times):
        name = f"f{index:05d}.jpg"
        shown = t if t < 1.0 or t > 2.5 else 1.0 + (int((t - 1.0) * 15 + 1e-6)) / 15
        Image.fromarray(_painted(shown)).save(tmp_path / name, quality=92)
        frames.append((round(t, 4), name))
    done = canvas_alone.composite(frames, _copies(tmp_path, COPIED), _events(), fps=30, pixel_ratio=RATIO, workdir=str(tmp_path))
    [run] = done.runs
    assert run["painted"] == pytest.approx(30.0, abs=1.0) and run["moved"] < 20, run
    assert run["composited"] is True, run


def test_copies_unlike_what_the_browser_painted_are_never_composited(tmp_path):
    """A WebGL buffer read once presented copies the canvas blank: its last copy differs from the frame painted at rest."""
    frames = _frames(tmp_path, PAINTED)
    copies = _copies(tmp_path, COPIED, blank=True)
    done = canvas_alone.composite(frames, copies, _events(), fps=30, pixel_ratio=RATIO, workdir=str(tmp_path))
    [run] = done.runs
    assert run["composited"] is False and run["why"].startswith("its copies differ from what the browser painted ("), run
    assert done.frames == frames
    words = canvas_alone.capture_words({"alone": {"runs": done.runs}})
    assert words.startswith("canvas transitions captured alone: 0 composited, 1 kept as painted (its copies differ")


def test_a_first_frame_at_rest_not_yet_presented_does_not_refuse_the_run(tmp_path):
    """#9266: the first frame painted after a layout move still showed the canvas mid-way (the page's thread held back
    by its quota) and the run was refused at 14%; a later frame at rest matches its last copy, so it is composited and
    the run says which frame it was checked against."""
    times = [0.3, 0.6, 0.9, *(1.0 + k / 8 for k in range(12)), 2.6, 2.8]
    frames = []
    for index, t in enumerate(times):
        name = f"f{index:05d}.jpg"
        shown = 1.9 if abs(t - 2.6) < 1e-6 else t
        Image.fromarray(_painted(shown)).save(tmp_path / name, quality=92)
        frames.append((round(t, 4), name))
    done = canvas_alone.composite(frames, _copies(tmp_path, COPIED), _events(), fps=30, pixel_ratio=RATIO, workdir=str(tmp_path))
    [run] = done.runs
    assert run["composited"] is True, run
    after = [entry for entry in run["checked"] if entry["side"] == "after"]
    assert after and after[0]["frame"] == 2.8 and after[0]["share"] <= canvas_alone.MISMATCH_SHARE, run["checked"]


def test_a_refused_run_says_which_side_and_how_far_apart(tmp_path):
    frames = _frames(tmp_path, PAINTED)
    done = canvas_alone.composite(frames, _copies(tmp_path, COPIED, blank=True), _events(), fps=30, pixel_ratio=RATIO, workdir=str(tmp_path))
    [run] = done.runs
    assert "its last copy against the frame painted" in run["why"] and " s after it)" in run["why"], run
    assert {entry["side"] for entry in run["checked"]} >= {"after"}
    assert all(entry["lag"] >= 0 for entry in run["checked"]), run["checked"]


def test_a_run_with_nothing_painted_at_rest_about_it_is_never_composited(tmp_path):
    frames = _frames(tmp_path, [1.0 + k / 8 for k in range(12)])
    done = canvas_alone.composite(frames, _copies(tmp_path, COPIED[1:]), _events(), fps=30, pixel_ratio=RATIO, workdir=str(tmp_path))
    assert [(run["composited"], run["why"]) for run in done.runs] == [(False, canvas_alone.UNCHECKED)]
    assert done.frames == frames


def test_copies_made_while_a_step_scrolled_belong_to_no_run(tmp_path):
    """A scroll moves the canvas with its page: ``scroll_pacing`` draws those frames again, never a copy pasted."""
    copies = _copies(tmp_path, COPIED)
    assert canvas_alone.alone_runs(copies, _events(scrolls=[{"t": [0.9, 2.6], "samples": []}])) == []
    [run] = canvas_alone.alone_runs(copies, _events())
    assert (run.start, len(run.copies)) == (1.0, 46)
    # A canvas moved or resized starts another run; a single copy at rest is none.
    moved = [*copies[:20], *(canvas_alone.AloneFrame(c.t, c.surface, c.canvas, (101.5, 50.0, 500.0, 300.0), c.over, c.file) for c in copies[20:])]
    assert [len(r.copies) for r in canvas_alone.alone_runs(moved, _events())] == [19, 27]


def test_a_canvas_copied_at_rest_composites_nothing_and_both_backends_copy(tmp_path):
    frames = _frames(tmp_path, PAINTED)
    assert canvas_alone.composite(frames, [], _events(), fps=30, pixel_ratio=RATIO, workdir=str(tmp_path)).frames == frames
    # Software GL and GPU GL both copy their canvases alone; a take recorded before says nothing.
    assert capture_rate.GlBackend(capture_rate.SOFTWARE_GL_ARGS, gpu=False).alone is True
    assert capture_rate.GlBackend(capture_rate.GPU_GL_ARGS, gpu=True).alone is True
    assert canvas_alone.capture_words({}) == "" and canvas_alone.capture_words({"gl": {"gpu": True}}) == ""
    assert canvas_alone.capture_words({"alone": {"copies": 0}}).endswith("no transition needed it")


def _jpeg(picture: np.ndarray) -> str:
    out = io.BytesIO()
    Image.fromarray(picture).save(out, format="JPEG", quality=80)
    return base64.b64encode(out.getvalue()).decode("ascii")


class _Page:
    def __init__(self, payloads: list) -> None:
        self.payloads, self.evaluated = list(payloads), []

    async def evaluate(self, script: str):
        self.evaluated.append(script)
        return self.payloads.pop(0) if self.payloads else {"frames": [], "off": None}


async def test_the_recorder_drains_the_page_s_copies_on_the_screencast_s_clock(tmp_path):
    data = _jpeg(_copy(1.5))
    page = _Page(
        [
            {
                "frames": [
                    [1000_500.0, SURFACE, "canvas#graph", list(CANVAS), [list(LEGEND)], data],
                    [999_900.0, SURFACE, "canvas#graph", list(CANVAS), [], data],  # before the screencast started
                    [1000_600.0, SURFACE, "canvas#graph", [1, 2], [], data],  # no box
                    [1000_700.0, SURFACE, "canvas#graph", list(CANVAS), [], data],
                    [1000_800.0, SURFACE, "canvas#graph", list(CANVAS), [], data],  # past the most kept
                ],
                "off": None,
            },
            {"frames": [], "off": "the page refused its encoder: blocked by worker-src"},
        ]
    )
    tap = canvas_alone.Tap(page, str(tmp_path), origin=1000.0, max_copies=2)
    await tap.drain()
    assert [(f.t, f.file, f.clip, f.over) for f in tap.frames] == [
        (0.5, "c000000.jpg", CANVAS, (LEGEND,)),
        (0.7, "c000001.jpg", CANVAS, ()),
    ]
    assert (tmp_path / "c000001.jpg").read_bytes() == base64.b64decode(data)
    kept = await tap.stop()
    assert kept == {
        "ratio": canvas_alone.ALONE_RATIO,
        "quality": canvas_alone.ALONE_QUALITY,
        "copies": 2,
        "off": "the page refused its encoder: blocked by worker-src",
    }
    assert canvas_alone.capture_words({"alone": kept}) == "canvases not captured alone (the page refused its encoder: blocked by worker-src)"
    assert "its canvases not captured alone" in capture_rate.software_words({"gl": {"gpu": False}, "alone": kept})


def test_the_page_script_copies_at_the_take_s_rate_and_encodes_off_its_thread():
    script = canvas_alone.tap_script(60, "(el) => el.tagName.toLowerCase()")
    assert "const FPS = 60, RATIO = 1.0, QUALITY = 0.8" in script
    assert "URL.createObjectURL(new Blob([" in script and "new Worker(source)" in script and "convertToBlob" in script
    assert "__" not in script.replace("__xtm", "")


TAKE = "00000000-0000-0000-0000-000000006761"
STEPPING = {"t": [1.0, 2.5], "frames": 46, "synthesised": 34, "share": 0.739, "fps": 30, "step": 1, "canvas": "canvas#graph"}


def _take(*, software: bool) -> timeline.TakeMedia:
    events = [
        {"step": 0, "action": "click", "t0": 0.5, "ta": 1.0, "t1": 1.1, "ok": True, "box": {"x": 100, "y": 100, "w": 80, "h": 30}},
        {"step": 1, "action": "wait", "t0": 1.2, "ta": 1.2, "t1": 3.6, "ok": True},
        {"step": 2, "action": "wait", "t0": 3.6, "ta": 3.6, "t1": 7.2, "ok": True},
    ]
    gl = {"gl": "software"} if software else {}
    take = timeline.TakeMedia(TAKE, 8.0, 1600, 900, 1.5, 60, 480, events=events)
    take.stepping = [{**STEPPING, **gl}]
    take.low = [
        {"t": [1.0, 2.5], "fps": 8.0, "step": 1, "canvas": "canvas#graph", **gl},
        {"t": [4.0, 5.2], "fps": 12.0, "step": 2, **gl},
    ]
    return take


def _findings(take: timeline.TakeMedia, **scene) -> list:
    authored = authored_spec(
        {"title": "Graph", "freestyle": True, "scenes": [{"id": "r", "type": "recording", "take_id": TAKE, **scene}]}
    )
    spec = parse_video_spec(authored)
    plan = timeline.estimate_plan(spec, {TAKE: take})
    facts = ContentFacts(takes={TAKE: take}, ready_takes={TAKE})
    return content_findings(spec, plan.document, facts, authored)


@pytest.mark.parametrize("software", [True, False])
def test_a_scene_playing_stepping_motion_has_the_finding_naming_its_step(software):
    found = _findings(_take(software=software))
    [stepping] = [f for f in found if f.kind == "stepping_motion"]
    assert stepping.severity == "dense" and stepping.scene_id == "r"
    assert (
        "plays stepping motion at step 1 (canvas#graph moved 1.5 s: 34 of its 46 frames at 30 fps synthesised from "
        "keyframes) on scene r: more than half of those frames hold the keyframe before"
    ) in stepping.message
    assert ("a worker whose Chromium draws on a GPU" in stepping.message) is software
    # Its take kept no painted frames there: slowing the scene is not offered; a 60 fps take on software GL
    # is offered fps 30.
    assert "or slow the scene" not in stepping.message
    assert "slowing the scene would not smooth it: a slowed scene shows one painted frame at a time" in stepping.message
    assert ("or record it at fps 30 (fps_fallback: fps)" in stepping.message) is software
    # The stretch said stepping is never said low as well; the other low stretch still is.
    [low] = [f for f in found if f.kind == "take_capture_rate"]
    assert "low capture rate at step 2 (12 fps for 1.2 s" in low.message and "low capture rate at step 1" not in low.message


def test_a_scene_trimmed_past_the_stretch_has_no_stepping_finding():
    found = _findings(_take(software=True), trim_start=3.0)
    assert not [f for f in found if f.kind == "stepping_motion"]
    assert [f.kind for f in found if f.kind == "take_capture_rate"] == ["take_capture_rate"]


def test_a_stretch_whose_canvas_was_not_composited_says_why():
    said = capture_rate.stepping_words({"stepping": [{**STEPPING, "alone": canvas_alone.UNCHECKED}]})
    assert said == [
        "stepping motion at step 1 (canvas#graph moved 1.5 s: 34 of its 46 frames at 30 fps synthesised from keyframes; "
        f"not captured alone: {canvas_alone.UNCHECKED})"
    ]
    # A low stretch none of those covers is kept; one it covers is not.
    assert capture_rate.besides([{"t": [2.0, 3.0]}, {"t": [3.0, 4.0]}], [STEPPING]) == [{"t": [3.0, 4.0]}]


def test_record_browser_demo_says_which_gl_each_worker_shape_draws_with():
    description = {d.name: d for d in tool._definitions(["high"])}["record_browser_demo"].description
    assert "a GPU worker (its Chromium finds a GPU) draws them on it" in description
    assert "a CPU worker draws them on SwiftShader and also captures each canvas alone at reduced quality" in description
    assert "'stepping motion'" in description
