"""A page that paints too slowly still scrolls smoothly in its recording.

The OpenAEV exposure overview repaints a heavy dashboard on every scroll offset, 3 to 12 frames a second. In a real
Chromium such a page hands over a frame whose ``scrollOffsetY`` already says the next offset while its pixels are
still the paint before (the pacing keeps every frame between the keyframes' offsets, never this): paced on what the frames
said, the scroll repeated 30 of its 78 frames while it moved, and the take still said 30 fps.

Synthetic frames made the way such a page paints - a paint every 0.31 s, 0.3 s behind the wheel, each followed by a
frame saying the next offset over the same pixels - and a log as late and as sparse:

* the scroll plays at 30 frames a second at the pace the wheel turned, every frame the page at its instant, the frames
  that said another offset read off their pixels;
* only where two keyframes are further apart than the window shows do frames hold (``outran``, the seconds its
  step takes).
"""

from __future__ import annotations

import itertools
import math

import numpy as np
import pytest
from PIL import Image

from app.services.video_studio import scroll_pacing

#: A 800x450 window at pixel ratio 1 over a 3000 px page, a 40 px header fixed over it.
VIEW_W, VIEW_H, PAGE_H, HEADER = 800, 450, 3000, 40
#: The scroll step: 600 px over 2.5 s from 1.0 s; the page paints every 0.31 s, 0.3 s behind it.
WHEEL, SECONDS, DISTANCE = 1.0, 2.5, 600.0
PAINT_EVERY, LATE = 0.31, 0.3


def _page() -> np.ndarray:
    y = np.arange(PAGE_H)[:, None]
    x = np.arange(VIEW_W)[None, :]
    marks = (((y // 6) * 37 + (x // 48) * 91 + (y // 90) * 13) % 180).astype(np.uint8) + 40
    return np.repeat(marks[:, :, None], 3, axis=2)


def _view(page: np.ndarray, offset: float) -> np.ndarray:
    top = min(max(round(offset), 0), PAGE_H - VIEW_H)
    frame = page[top : top + VIEW_H].copy()
    frame[:HEADER] = (24, 30, 48)
    return frame


def _painted_at(t: float, *, distance: float = DISTANCE, seconds: float = SECONDS) -> float:
    """Where the page painted itself at *t*: the wheel's motion, :data:`LATE` behind it."""
    return min(max((t - WHEEL - LATE) / seconds, 0.0), 1.0) * distance


def _take(tmp_path, page: np.ndarray, *, distance: float = DISTANCE, seconds: float = SECONDS, every: float = PAINT_EVERY):
    """The keyframes such a page hands over, what each says it painted, and the step's event with its late log."""
    frames: list[tuple[float, str]] = []
    said: dict[str, float] = {}

    def keep(t: float, shows: float, says: float) -> None:
        name = f"f{len(frames):06d}.jpg"
        Image.fromarray(_view(page, shows)).save(tmp_path / name, quality=95)
        frames.append((round(t, 4), name))
        said[name] = says

    keep(0.05, 0.0, 0.0)
    keep(0.6, 0.0, 0.0)
    t = WHEEL + LATE
    while t < WHEEL + LATE + seconds + every:
        shows = _painted_at(t, distance=distance, seconds=seconds)
        keep(t, shows, shows)
        nxt = _painted_at(t + every, distance=distance, seconds=seconds)
        if nxt != shows:
            # The compositor's next offset over the paint before: what the screencast hands over first.
            keep(t + 0.03, shows, nxt)
        t += every
    keep(t + 0.2, distance, distance)
    # The log is read on the page's main thread, blocked by the paint: a sample a paint, a paint late.
    samples = [[round(s, 4), _painted_at(s - LATE, distance=distance, seconds=seconds), 0, []] for s in np.arange(0.9, WHEEL + LATE + seconds + 0.8, every + 0.004)]
    # The wheel turned on its 60 Hz clock (``capture``): how far it had turned at each tick.
    turns = [[round(k / 60, 4), round(distance * min(k / 60 / seconds, 1.0))] for k in range(math.ceil(seconds * 60) + 1)]
    event = {
        "step": 2,
        "action": "scroll",
        "ok": True,
        "scrolls": [
            {
                "t": [0.9, round(WHEEL + LATE + seconds + 0.8, 3)],
                "samples": samples,
                "wheel": {"t0": WHEEL, "pixels": distance, "turns": turns},
            }
        ],
    }
    return frames, said, [event]


def _decoded(tmp_path, name: str) -> np.ndarray:
    with Image.open(tmp_path / name) as opened:
        return np.asarray(opened.convert("RGB")).astype(np.int16)


def _error(shown: np.ndarray, page: np.ndarray, t: float) -> float:
    """How far *shown* is from the page where it stood at *t*, under its header, to the pixel either side."""
    return min(float(np.abs(shown[HEADER:] - _view(page, _painted_at(t) + nudge).astype(np.int16)[HEADER:]).mean()) for nudge in (-1, 0, 1))


def test_a_page_painting_300_ms_late_scrolls_at_30_fps_at_the_wheel_s_pace(tmp_path):
    page = _page()
    frames, said, events = _take(tmp_path, page)
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=30, pixel_ratio=1.0, painted=said)
    [scroll] = events[0]["scrolls"]
    assert scroll["paced"] and scroll["fps"] == 30.0 and scroll["painted"] < 7 and scroll["offsets"] == "painted"
    # The frames that said the next offset over the paint before were read off their pixels.
    assert scroll["read"] >= 5
    assert scroll["late"] == pytest.approx(LATE, abs=0.02) and "held" not in scroll and "outran" not in scroll
    moving = [(t, name) for t, name in paced.frames if WHEEL + LATE < t < WHEEL + LATE + SECONDS]
    assert len(moving) >= 0.95 * 30 * SECONDS - 1
    previous = None
    for t, name in moving:
        shown = _decoded(tmp_path, name)
        # The page where it stood at that instant, to the pixel: never the keyframe before held, never past it.
        assert _error(shown, page, t) < 4.0, f"the frame at {t:.3f} s"
        assert np.abs(shown[:HEADER] - _view(page, 0)[:HEADER].astype(np.int16)).mean() < 4.0, "the header stands still"
        if previous is not None:
            assert np.abs(shown - previous).mean() > 1.0, f"the frame at {t:.3f} s repeats the one before"
        previous = shown
    rate = scroll_pacing.take_rate(events, captured_frames=len(frames), duration=5.0)
    assert (rate.fps, rate.steps, rate.seconds) == (30.0, False, None)
    assert rate.words() == f"scrolls at 30 fps ({rate.synthesised:.0f} frames a second synthesised between keyframes)"


def test_paced_on_what_the_frames_said_the_scroll_would_repeat_frames(tmp_path, monkeypatch):
    """The bug: each frame saying the next offset over the paint before, taken at its word."""
    page = _page()
    frames, said, events = _take(tmp_path, page)
    monkeypatch.setattr(scroll_pacing, "_checked", lambda offsets, *_args: (offsets, 0))
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=30, pixel_ratio=1.0, painted=said)
    moving = [(t, name) for t, name in paced.frames if WHEEL + LATE < t < WHEEL + LATE + SECONDS]
    wrong = [t for t, name in moving if _error(_decoded(tmp_path, name), page, t) >= 4.0]
    assert len(wrong) >= 5


def test_a_keyframe_painting_the_offset_of_the_one_before_is_the_paint_lagging_never_a_stop():
    assert scroll_pacing._anchors([0.0, 0.0, 0.0, 8.0, 80.0, 80.0, 160.0, 160.0, 160.0]) == [2, 3, 4, 6]


# ── Two keyframes further apart than the window shows ────────────────────


def _gapped(tmp_path):
    """1500 px over 1 s, the page painting every 0.5 s: 750 px between two keyframes, past the 410 px that move."""
    return _take(tmp_path, _page(), distance=1500.0, seconds=1.0, every=0.5)


def test_only_where_two_keyframes_do_not_meet_do_frames_hold_and_the_take_gives_the_seconds_its_step_needs(tmp_path):
    frames, said, events = _gapped(tmp_path)
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=30, pixel_ratio=1.0, painted=said)
    [scroll] = events[0]["scrolls"]
    # The frames that said the next offset read off their pixels: the paints 750 px apart are the real gap.
    assert scroll["paced"] and scroll["held"] >= 1 and scroll["read"] >= 1
    outran = scroll["outran"]
    assert (outran["moved"], outran["shows"]) == (750, VIEW_H - HEADER) and outran["between"] < 0.5 and scroll["seconds"] > 1.0
    [note] = paced.notes
    assert note.startswith("Step 2's scroll moved 750 px between two paints") and f"seconds: {scroll['seconds']:g}" in note


def test_a_wheel_step_keeps_when_and_how_far_its_wheel_turned():
    samples = [[0.1 * i, 40.0 * i, 0, []] for i in range(10)]
    steady = {"t0": 0.05, "pixels": 360, "turns": [[0.0, 0], *[[round(k / 60, 4), 9 * k] for k in range(1, 41)]]}
    [scroll] = scroll_pacing.read_scrolls({"t": [0.0, 0.9], "samples": samples, "wheel": steady})
    # On its clock the wheel turns along one line: its two ends are kept.
    assert scroll.wheel == ((0.05, 0.0), (0.05 + 0.6667, 1.0))
    assert scroll_pacing._compact(scroll)["wheel"] == [[0.05, 0.0], [0.7167, 1.0]]
    # A host too busy to turn it on time: the turns it sent late are where they were sent.
    late = {"t0": 0.0, "pixels": 300, "turns": [[0.0, 0], [0.1, 30], [0.2, 60], [0.5, 150], [0.6, 300]]}
    [scroll] = scroll_pacing.read_scrolls({"t": [0.0, 0.9], "samples": samples, "wheel": late})
    assert scroll.wheel == ((0.0, 0.0), (0.5, 0.5), (0.6, 1.0))
    assert scroll_pacing.read_scrolls({"t": [0.0, 0.9], "samples": samples})[0].wheel is None
    # Two scrollers moving one after the other share one wheel: no scroll of them takes its pace.
    turns = [[0.1 * i, i, 0, [[1, 40.0 * min(i, 4), 0, [0, 100, 800, 300]]]] for i in range(10)]
    for s in turns:
        s[1] = 40.0 * max(s[0] * 10 - 5, 0)
    split = scroll_pacing.read_scrolls({"t": [0.0, 0.9], "samples": turns, "wheel": steady})
    assert len(split) == 2 and all(scroll.wheel is None for scroll in split)


def test_the_steps_between_two_paced_frames_are_even(tmp_path):
    page = _page()
    frames, said, events = _take(tmp_path, page)
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=30, pixel_ratio=1.0, painted=said)
    moving = [t for t, _name in paced.frames if WHEEL + LATE + 0.05 < t < WHEEL + LATE + SECONDS - 0.05]
    steps = [_painted_at(b) - _painted_at(a) for a, b in itertools.pairwise(moving)]
    assert steps and all(abs(step - DISTANCE / SECONDS / 30) < 0.5 for step in steps), "8 px a frame"
