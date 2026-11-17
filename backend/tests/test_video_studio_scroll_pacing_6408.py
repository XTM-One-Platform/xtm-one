"""A recording's scrolls are drawn again at its frame rate from the frames the browser painted.

Chromium's screencast hands over a frame only when the page has painted and the frame before was taken: at 3200x1800
a take carried 2.1 frames a second (recording 38f88e75: 128 frames in 61.4 s), so a smooth scroll reached the insert as
2 positions a second. What is proven here, on frames made the way the screencast makes them - a 4000 px page under a
fixed header, cut at the offsets a linear scroll reaches at each paint, JPEG at the screencast's quality - and a scroll
log 20 ms off the paint clock:

* the 2000 px scroll over 2 s of a 1600x900 window at pixel ratio 2 plays at 30 frames a second: 60 frames, each
  the page about 33 px further down, no two the same, every one the page at its instant, the header standing still;
* a scroll painted at its rate keeps its frames, one that moved two scrollers is not drawn again, and what a take keeps
  of each scroll says why;
* a take reports its real frame rate - the lowest its scrolls play at, the frames it painted over its length for one
  recorded before - and one under 24 fps on a recording scene or a screen insert is a save finding.
"""

from __future__ import annotations

import itertools

import numpy as np
import pytest
from PIL import Image

from app.services.video_studio import quality, scroll_pacing, timeline
from app.services.video_studio.spec import TakeScript, parse_video_spec, validate_take_script, validate_video_spec

RATIO = 2
VIEW = (1600, 900)
FRAME = (VIEW[0] * RATIO, VIEW[1] * RATIO)
PAGE_CSS = 4000
HEADER_CSS = 60
#: The scroll: at rest until 1.0 s, 2000 CSS px at 1000 px/s, at rest from 3.0 s; logged from 0.9 s to 3.2 s.
START, END, DISTANCE = 1.0, 3.0, 2000.0
LOG_FROM, LOG_TO = 0.9, 3.2
#: The scroll log's clock runs this far behind the paint's - two frames of the page's main thread: it reads each offset
#: this much later than it was painted.
LAG = 2 / 60
#: The screencast's own pace at this size: a frame every ~0.48 s.
PAINT_FPS = 2.1


def _offset(t: float) -> float:
    """Where the page stands at *t* (CSS px)."""
    return min(max(t - START, 0.0), END - START) * DISTANCE / (END - START)


def _page() -> np.ndarray:
    """A 4000 CSS px page at pixel ratio 2: rows of text-like marks, every band of rows unlike its neighbours."""
    height, width = PAGE_CSS * RATIO, FRAME[0]
    y = np.arange(height)[:, None]
    x = np.arange(width)[None, :]
    marks = (((y // 6) * 37 + (x // 48) * 91 + (y // 90) * 13) % 180).astype(np.uint8) + 40
    page = np.repeat(marks[:, :, None], 3, axis=2)
    page[:, :, 2] = (page[:, :, 2].astype(np.int32) * 3 // 4 + 30).astype(np.uint8)
    return page


def _view(page: np.ndarray, offset_css: float) -> np.ndarray:
    """The frame the window shows at *offset_css*: the page scrolled, its fixed header over it."""
    top = min(max(round(offset_css * RATIO), 0), page.shape[0] - FRAME[1])
    frame = page[top : top + FRAME[1]].copy()
    frame[: HEADER_CSS * RATIO] = (24, 30, 48)
    frame[20:60, 40:600] = (230, 232, 240)
    return frame


def _jpeg(frame: np.ndarray, path) -> None:
    Image.fromarray(frame).save(path, quality=95)


def _take(tmp_path, page: np.ndarray, *, paint_fps: float = PAINT_FPS) -> tuple[list[tuple[float, str]], list[dict]]:
    """The keyframes the screencast hands over and the step's event with its scroll log, as the recorder keeps them."""
    frames = []
    t, k = 0.05, 0
    while t < 4.0:
        name = f"f{k:06d}.jpg"
        _jpeg(_view(page, _offset(t)), tmp_path / name)
        frames.append((round(t, 4), name))
        t += 1 / paint_fps
        k += 1
    samples = []
    s = LOG_FROM
    while s <= LOG_TO + 1e-9:
        samples.append([round(s, 4), _offset(s + LAG), 0, []])
        s += 1 / scroll_pacing.SCROLL_SAMPLE_HZ
    event = {"step": 0, "action": "scroll", "ok": True, "t0": START, "t1": END, "scrolls": [{"t": [LOG_FROM, LOG_TO], "samples": samples}]}
    return frames, [event]


def _decoded(tmp_path, name: str) -> np.ndarray:
    with Image.open(tmp_path / name) as opened:
        return np.asarray(opened.convert("RGB")).astype(np.int16)


def test_a_2000_px_scroll_over_2_s_at_3200x1800_plays_at_30_frames_a_second(tmp_path):
    page = _page()
    frames, events = _take(tmp_path, page)
    painted = [t for t, _name in frames if START <= t < END]
    assert len(painted) <= 5, "the screencast's own pace: about 2 frames a second"
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=30, pixel_ratio=RATIO)
    [scroll] = events[0]["scrolls"]
    assert scroll["paced"] and scroll["fps"] == 30.0 and scroll["painted"] < 3
    assert scroll["scroller"] == "window" and scroll["still"]["top"] == HEADER_CSS * RATIO
    during = [(t, name) for t, name in paced.frames if START <= t < END]
    assert len(during) == 60
    header = HEADER_CSS * RATIO
    previous = None
    for t, name in during:
        shown = _decoded(tmp_path, name)
        # The page at its instant, to the device pixel, cut from the keyframes either side - never the keyframe before,
        # held.
        error = min(
            float(np.abs(shown[header:] - _view(page, _offset(t) + nudge / RATIO).astype(np.int16)[header:]).mean())
            for nudge in (-1, 0, 1)
        )
        assert error < 4.0, f"the frame at {t:.3f} s"
        expected = _view(page, _offset(t)).astype(np.int16)
        assert np.abs(shown[:header] - expected[:header]).mean() < 4.0, "the header stands still"
        if previous is not None:
            assert np.abs(shown - previous).mean() > 1.0, f"the frame at {t:.3f} s repeats the one before"
        previous = shown
    steps = [_offset(b) - _offset(a) for (a, _x), (b, _y) in itertools.pairwise(during)]
    assert all(abs(step - DISTANCE / 60) < 1.0 for step in steps), "about 33 px a frame"
    # What a take keeps of it: its scroller, its log, how it was painted and how it plays.
    assert set(scroll) >= {"t", "scroller", "log", "painted", "fps", "paced", "frames"} and "samples" not in scroll
    rate = scroll_pacing.take_rate(events, captured_frames=len(frames), duration=4.0)
    assert (rate.fps, rate.scrolls, rate.paced, rate.steps) == (30.0, True, True, False)
    # What its frames a second are made of: those the browser painted, the rest synthesised between them.
    assert rate.words() == f"scrolls at 30 fps ({round(30 - scroll['painted'])} frames a second synthesised between keyframes)"
    # Its step says the rate it was painted at and the rate it plays at, and what it scrolled.
    assert (events[0]["painted_fps"], events[0]["fps"], events[0]["scrolled_element"]) == (scroll["painted"], 30.0, "window")


def test_a_scroll_painted_at_its_rate_keeps_its_frames_and_one_that_moved_two_scrollers_is_not_drawn_again(tmp_path):
    page = _page()
    frames, events = _take(tmp_path, page, paint_fps=32)
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=30, pixel_ratio=RATIO)
    assert paced.frames == frames and not paced.paced
    assert events[0]["scrolls"][0]["why"] == "painted at its rate" and events[0]["scrolls"][0]["fps"] >= 27
    frames, events = _take(tmp_path, page)
    for sample in events[0]["scrolls"][0]["samples"]:
        sample[3] = [[1, sample[1] / 2, 0, [0, 100, 800, 700]]]
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=30, pixel_ratio=RATIO)
    assert paced.frames == frames
    assert events[0]["scrolls"][0]["why"] == "two scrollers moved at once" and not events[0]["scrolls"][0]["paced"]


def test_a_scroll_container_is_found_by_what_moved():
    samples = [[0.1 * i, 0, 0, [[3, 40.0 * i, 0, [200, 80, 1200, 800]]]] for i in range(10)]
    [scroll] = scroll_pacing.read_scrolls({"t": [0.0, 0.9], "samples": samples, "names": {"3": "main#panel"}})
    assert scroll.scroller == "element" and scroll.box == (200, 80, 1200, 800) and scroll.unpaceable is None
    assert scroll.offset(0.45) == pytest.approx(180.0) and scroll.element == "main#panel"


def test_a_take_reports_its_real_frame_rate():
    # Recording 38f88e75: 128 frames in 61.4 s, a scroll step, recorded before its scrolls were logged.
    old = [{"step": 0, "action": "goto"}, {"step": 1, "action": "scroll"}]
    rate = scroll_pacing.take_rate(old, captured_frames=128, duration=61.4)
    assert (rate.fps, rate.scrolls, rate.steps) == (2.1, True, True)
    # A take that never scrolls steps nowhere: its paint rate is a fact.
    still = scroll_pacing.take_rate([{"step": 0, "action": "click"}], captured_frames=104, duration=14.8)
    assert (rate.steps, still.steps, still.words()) == (True, False, "7 fps painted")
    # A scroll that could not be drawn again plays at the rate it was painted.
    unpaced = [{"action": "scroll", "scrolls": [{"fps": 2.08, "paced": False, "why": "two scrollers moved at once"}]}]
    assert scroll_pacing.take_rate(unpaced, captured_frames=128, duration=61.4).steps


def test_a_recording_script_asks_its_rate():
    assert TakeScript.model_validate({"steps": [{"action": "wait", "seconds": 1}]}).fps == 30
    assert validate_take_script({"steps": [{"action": "wait", "seconds": 1}], "fps": 60})["fps"] == 60


TAKE = "38f88e75-0000-4000-8000-000000000001"


def _spec(with_insert: bool) -> tuple:
    scenes = [{"id": "demo", "type": "recording", "take_id": TAKE}]
    if with_insert:
        scenes = [
            {
                "id": "booth",
                "type": "clip",
                "clip_id": "00000000-0000-4000-8000-00000000c11b",
                "overlays": [{"type": "screen_insert", "source": {"take_id": TAKE}, "quad": [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]}],
            }
        ]
    authored = validate_video_spec({"title": "Brand film", "freestyle": True, "scenes": scenes})
    spec = parse_video_spec(authored)
    return spec, timeline.estimate_plan(spec, {}, {}).document, authored


@pytest.mark.parametrize("with_insert", [False, True])
def test_a_take_under_24_fps_on_a_recording_scene_or_a_screen_insert_is_a_save_finding(with_insert):
    spec, document, authored = _spec(with_insert)
    stepping = timeline.TakeMedia(TAKE, 61.4, 1600, 900, 2.0, 60, 3684, events=[{"step": 0, "action": "scroll"}], captured_frames=128)
    found = quality.content_findings(spec, document, quality.ContentFacts(takes={TAKE: stepping}), authored)
    [finding] = [f for f in found if f.kind == "take_frame_rate"]
    assert finding.severity == "dense" and finding.scene_id == ("booth" if with_insert else "demo")
    assert finding.message.startswith("take 38f88e75 carries 2.1 fps on step 0 (scroll): scrolls will step on ")
    # The fix it names is the recorder's: its scrolls paced, the worker's capture rate measured before step 1.
    assert finding.message.endswith(
        "record it again at 30 fps (record_browser_demo draws its scrolls again at its fps, and measures the worker's "
        "capture rate before step 1, recording at a pixel ratio it sustains)"
    )
    paced = [{"step": 0, "action": "scroll", "scrolls": [{"fps": 30.0, "paced": True}]}]
    smooth = timeline.TakeMedia(TAKE, 61.4, 1600, 900, 2.0, 60, 3684, events=paced, captured_frames=128)
    assert not [f for f in quality.content_findings(spec, document, quality.ContentFacts(takes={TAKE: smooth}), authored) if f.kind == "take_frame_rate"]


def test_the_insert_takes_a_spec_plays_are_gathered_for_the_check():
    spec, _document, _authored = _spec(True)
    assert quality._insert_takes(spec) == [TAKE]
    assert [entry[2] for entry in quality._shown_takes(spec)] == ["scene booth's screen insert 0"]
