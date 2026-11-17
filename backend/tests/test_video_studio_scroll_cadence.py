"""A scripted scroll moves the same distance every frame of its take, and a step whose scroll steps says so.

Found on a 1080p60 render of a recorded demo: a ``scroll`` step was a short burst of wheel turns the browser painted in
a few large steps, and on a 60 fps take the frames it painted alternated between a repeated frame and a jump of twice
the mean - a move-move-hold cadence at 10 to 30 Hz that reads as judder on every page. Here:

* the wheel turns once a frame of the take, each turn the same distance to the pixel (``capture.wheel_deltas``): 120
  turns of 10 px for 1200 px over 2 s at 60 fps, 60 of 20 px at 30; a scroll ``to: bottom`` moves the window the same
  way, never by the browser's own smooth scroll;
* a scroll painted at its take's rate but in a move-move-hold cadence is drawn again at that rate from keyframes spread
  over it: measured on the take's frames by phase correlation, its shift per frame has a stdev under 25 % of its mean,
  no frame repeated and none past twice the mean;
* each scroll keeps its cadence (``painted_fps``, ``max``, ``mean``, ``stdev``, ``stepping``), its step the least even
  one, and the action log says "stepping motion" for a step past :data:`scroll_pacing.STEPPING_SHARE`.
"""

from __future__ import annotations

import itertools
import statistics
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from app.services.video_studio import capture, scroll_pacing
from app.services.video_studio.spec import parse_take_script, validate_take_script

# ── The wheel's turns ────────────────────────────────────────────────────


@pytest.mark.parametrize(("fps", "count", "each"), [(60, 120, 10), (30, 60, 20)])
def test_a_1200_px_scroll_over_2_s_turns_once_a_frame_the_same_distance(fps, count, each):
    deltas = capture.wheel_deltas(1200, 2.0, fps)
    assert len(deltas) == count and set(deltas) == {each} and sum(deltas) == 1200
    upward = capture.wheel_deltas(-1000, 1.5, 60)
    # Not a whole number of pixels a frame: within one pixel of each other, every pixel sent, one way.
    assert len(upward) == 90 and sum(upward) == -1000 and max(upward) - min(upward) <= 1 and all(d < 0 for d in upward)
    assert capture.wheel_deltas(5, 0.0, 60) == [5]


class _Page:
    """A page the recorder scrolls: the wheel's turns, and the window's scroll for a step to the top or the bottom."""

    def __init__(self, scroll_y: float = 0.0, bottom: float = 1500.0) -> None:
        self.turns: list[tuple[float, float]] = []
        self.tops: list[float] = []
        self.scripts: list[str] = []
        self.scroll_y, self.bottom = scroll_y, bottom
        self.mouse = self

    async def wheel(self, _x: float, y: float) -> None:
        self.turns.append((time.monotonic(), y))

    async def evaluate(self, script: str, arg=None):
        self.scripts.append(script)
        if script == capture._WINDOW_SCROLL_RANGE_JS:
            return [self.scroll_y, self.bottom]
        if "scrollTo" in script:
            self.tops.append(float(arg))
        return None


def _recorder(page: _Page, fps: int):
    recorder = capture._Recorder.__new__(capture._Recorder)
    recorder.page = page
    recorder.script = SimpleNamespace(pace="natural", fps=fps)
    recorder.t_start = time.time()

    async def settled() -> None:
        return None

    recorder._wait_scroll_end = settled
    return recorder


def _step(**fields):
    return parse_take_script(validate_take_script({"steps": [{"action": "scroll", **fields}]})).steps[0]


@pytest.mark.parametrize(("fps", "count"), [(60, 120), (30, 60)])
async def test_the_recorder_turns_the_wheel_once_a_frame_of_the_take(fps, count):
    page = _Page()
    logged: dict = {}
    await _recorder(page, fps)._scrolled(_step(by=1200, seconds=2), None, logged)
    sent = [y for _t, y in page.turns]
    assert sum(sent) == 1200 and all(y > 0 for y in sent)
    # One turn a frame; a turn the clock fell behind on is caught up on the next, never sent ahead of its frame.
    assert 0.7 * count <= len(sent) <= count and min(sent) >= 1200 / count
    assert page.turns[-1][0] - page.turns[0][0] == pytest.approx(2.0 - 1 / fps, abs=0.15)
    turns = logged["wheel"]["turns"]
    assert turns[0] == [0.0, 0] and turns[-1][1] == 1200 and logged["wheel"]["pixels"] == 1200
    # Logged on the take's frame clock, never the host's: each turn on the frame it was due on, the last on the step's
    # last frame, however late a busy host sent it.
    assert all(t * fps == pytest.approx(round(t * fps), abs=0.01) for t, _sent in turns), turns
    assert turns[-1][0] == pytest.approx(round((count - 1) / fps, 4))


async def test_a_turn_a_busy_host_sends_late_is_logged_on_the_frame_it_was_due_on():
    """A host stalled 50 ms on one turn: the turns it caught up are logged on their frames, the log's pace exact."""
    page = _Page()
    stalled = {"done": False}
    wheel = page.wheel

    async def slow_wheel(x: float, y: float) -> None:
        await wheel(x, y)
        if len(page.turns) == 30 and not stalled["done"]:
            stalled["done"] = True
            time.sleep(0.05)  # the event loop held, as a loaded runner holds it

    page.wheel = slow_wheel  # type: ignore[method-assign]
    logged: dict = {}
    await _recorder(page, 60)._scrolled(_step(by=1200, seconds=2), None, logged)
    turns = logged["wheel"]["turns"]
    assert stalled["done"] and turns[-1] == [round(119 / 60, 4), 1200]
    # Every turn lies on the line from the first frame to the last: the share turned is the share of frames gone by.
    for t, sent in turns[1:]:
        assert sent == pytest.approx(1200 * (round(t * 60) + 1) / 120, abs=1), turns


class _Delegate:
    """A module seen through one attribute replaced: the rest of the module, and every other caller of it, untouched."""

    def __init__(self, module, **replaced) -> None:
        self._module, self._replaced = module, replaced

    def __getattr__(self, name: str):
        return self._replaced[name] if name in self._replaced else getattr(self._module, name)


@pytest.fixture
def frame_clock(monkeypatch):
    """The recorder's pacing clock frozen and driven by its own sleeps: every frame is due on time, whatever the load of
    the host running the test (a busy xdist worker held the loop 300 ms once and the real clock folded 19 frames into one
    catch-up step). The event loop keeps the real clock; only the recorder reads this one."""
    import asyncio

    now = [0.0]

    async def sleep(seconds: float) -> None:
        # A real sleep always takes some time: a wake-up on the frame boundary itself lands just past it.
        now[0] += max(seconds, 1e-6)
        await asyncio.sleep(0)

    monkeypatch.setattr(capture, "time", _Delegate(time, monotonic=lambda: now[0]))
    monkeypatch.setattr(capture, "asyncio", _Delegate(asyncio, sleep=sleep))
    return now


async def test_a_scroll_to_the_bottom_moves_the_window_a_frame_at_a_time(frame_clock):
    page = _Page(scroll_y=300.0, bottom=1500.0)
    await _recorder(page, 60)._scrolled(_step(to="bottom", seconds=1))
    # Never the browser's smooth scroll, which paints at its own pace in a few large steps; never the wheel.
    assert not page.turns and not any("smooth" in script for script in page.scripts)
    assert page.tops[-1] == 1500.0 and all(b > a for a, b in itertools.pairwise(page.tops))
    # On the take's frame clock: one move a frame, each the same distance - 1200 px over 60 frames.
    steps = [b - a for a, b in itertools.pairwise([300.0, *page.tops])]
    assert len(steps) == 60 and set(steps) == {20.0}
    up = _Page(scroll_y=900.0)
    await _recorder(up, 30)._scrolled(_step(to="top", seconds=0.5))
    assert up.tops[-1] == 0.0 and all(b < a for a, b in itertools.pairwise(up.tops))
    assert [a - b for a, b in itertools.pairwise([900.0, *up.tops])] == [60.0] * 15


async def test_a_scroll_to_the_bottom_a_held_loop_falls_behind_on_is_caught_up_on_the_next_frame(frame_clock):
    """A loop held about 300 ms on one move (a loaded host): the frames it missed are caught up in one move, never sent
    ahead of their frame, and the window still ends at the bottom on the step's last frame."""
    page = _Page(scroll_y=300.0, bottom=1500.0)
    evaluate = page.evaluate

    async def held(script: str, arg=None):
        found = await evaluate(script, arg)
        if len(page.tops) == 1 and "scrollTo" in script:
            frame_clock[0] += 18.5 / 60
        return found

    page.evaluate = held  # type: ignore[method-assign]
    await _recorder(page, 60)._scrolled(_step(to="bottom", seconds=1))
    steps = [b - a for a, b in itertools.pairwise([300.0, *page.tops])]
    # Held through 18.5 frames after the first move: the 18 frames gone by are caught up in the next move, no more.
    assert page.tops[-1] == 1500.0 and sum(steps) == 1200.0 and max(steps) == 20.0 * 18
    assert statistics.fmean(steps) > 20.0 and steps.count(20.0) == len(steps) - 1


# ── The cadence a take keeps ─────────────────────────────────────────────


def test_a_move_move_hold_cadence_is_stepping_and_a_steady_one_is_not():
    rest = [0.0] * 5
    steady = [*rest, *(10.0 * k for k in range(1, 121)), *([1200.0] * 5)]
    even = scroll_pacing.cadence(steady, painted=60.0)
    assert even == {"painted_fps": 60.0, "min": 10.0, "max": 10.0, "mean": 10.0, "stdev": 0.0, "stepping": False}
    assert scroll_pacing.even(even) and scroll_pacing.cadence_words(even) == ""
    # Moved, moved, held: 10, 20, 0 px a frame.
    held = [*rest, *(10.0 * (k - (k % 3 == 2)) for k in range(1, 121)), *([1200.0] * 5)]
    stepping = scroll_pacing.cadence(held, painted=58.3)
    assert stepping["stepping"] and stepping["min"] == 0.0 and stepping["max"] == 20.0 and stepping["mean"] == pytest.approx(10.0, abs=0.2)
    assert not scroll_pacing.even(stepping)
    assert scroll_pacing.cadence_words(stepping) == (
        f" (stepping motion: its scroll moved up to 20 px a frame, stdev {stepping['stdev']:g} px against a "
        f"{stepping['mean']:g} px mean, painted at 58.3 fps)"
    )
    assert scroll_pacing.cadence([0.0, 0.0, 4.0], painted=60.0) is None


# ── A scroll painted at its rate in a move-move-hold cadence ─────────────

#: A 800x450 window at pixel ratio 1 over a 3000 px page, a 40 px header fixed over it.
VIEW_W, VIEW_H, PAGE_H, HEADER = 800, 450, 3000, 40
#: The step: 1200 px over 2 s from 1.0 s, logged from 0.9 s to 3.2 s.
START, SECONDS, DISTANCE = 1.0, 2.0, 1200.0
LOG_FROM, LOG_TO = 0.9, 3.2


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


def _offset(t: float) -> float:
    return min(max((t - START) / SECONDS, 0.0), 1.0) * DISTANCE


def _stepping_take(tmp_path: Path, fps: int) -> tuple[list[tuple[float, str]], dict[str, float], list[dict]]:
    """A take whose screencast painted at its rate, but every third frame the one before again, then a jump of two."""
    page = _page()
    frames: list[tuple[float, str]] = []
    said: dict[str, float] = {}
    for k in range(round(4.0 * fps)):
        t = (k + 0.5) / fps
        shown = _offset((k - 1 + 0.5) / fps) if k % 3 == 2 else _offset(t)
        name = f"f{k:06d}.jpg"
        Image.fromarray(_view(page, shown)).save(tmp_path / name, quality=95)
        frames.append((round(t, 5), name))
        said[name] = round(shown)
    samples = [[round(s, 4), _offset(s), 0, []] for s in np.arange(LOG_FROM, LOG_TO + 1e-9, 1 / scroll_pacing.SCROLL_SAMPLE_HZ)]
    event = {"step": 1, "action": "scroll", "ok": True, "scrolls": [{"t": [LOG_FROM, LOG_TO], "samples": samples}]}
    return frames, said, [event]


def _grey(path: Path) -> np.ndarray:
    with Image.open(path) as opened:
        return np.asarray(opened.convert("L"), dtype=np.float64)


def phase_shift(a: np.ndarray, b: np.ndarray) -> float:
    """How many rows the content moved up from grey *a* to grey *b*, by phase correlation (to the row)."""
    window = np.hanning(a.shape[0])[:, None] * np.hanning(a.shape[1])[None, :]
    fa, fb = np.fft.fft2((a - a.mean()) * window), np.fft.fft2((b - b.mean()) * window)
    cross = fa * np.conj(fb)
    peak = np.unravel_index(np.argmax(np.abs(np.fft.ifft2(cross / (np.abs(cross) + 1e-9)))), a.shape)
    dy = int(peak[0])
    return float(dy - a.shape[0] if dy > a.shape[0] // 2 else dy)


def shifts(workdir: Path, frames: list[tuple[float, str]], crop: tuple[slice, slice]) -> list[float]:
    """The vertical shift of the content inside *crop* from each of *frames* to the next."""
    greys = [_grey(workdir / name)[crop] for _t, name in frames]
    return [phase_shift(a, b) for a, b in itertools.pairwise(greys)]


def assert_steady(moved: list[float]) -> None:
    """The acceptance bounds: a stdev under 25 % of the mean, no frame repeated, none past twice the mean."""
    mean = statistics.fmean(moved)
    assert mean > 0 and statistics.pstdev(moved) < 0.25 * mean, moved
    assert min(moved) > 0 and max(moved) <= 2 * mean, moved


@pytest.mark.parametrize("fps", [60, 30])
def test_a_scroll_painted_at_its_rate_in_a_move_move_hold_cadence_is_drawn_again_steady(tmp_path, fps):
    frames, said, events = _stepping_take(tmp_path, fps)
    crop = (slice(HEADER + 20, VIEW_H - 20), slice(100, 700))
    painted_moving = [(t, name) for t, name in frames if START + 0.1 < t < START + SECONDS - 0.1]
    # What the screencast painted: a repeated frame every third, then twice the mean.
    before = shifts(tmp_path, painted_moving, crop)
    assert min(before) == 0 and max(before) >= 1.9 * statistics.fmean(before)

    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=fps, pixel_ratio=1.0, painted=said)
    [scroll] = events[0]["scrolls"]
    assert scroll["painted"] >= scroll_pacing.PAINTED_ENOUGH * fps and scroll["paced"] and scroll["fps"] == float(fps), scroll
    assert scroll["painted_cadence"]["stepping"] and scroll["painted_cadence"]["min"] == 0.0
    steady = scroll["cadence"]
    assert not steady["stepping"] and steady["mean"] == pytest.approx(DISTANCE / SECONDS / fps, abs=0.5), steady
    assert steady["stdev"] < 0.25 * steady["mean"] and steady["max"] <= 2 * steady["mean"]
    # The step says it: its least even scroll, and nothing stepping in the action log.
    assert events[0]["scroll_cadence"] == steady and scroll_pacing.cadence_words(events[0]["scroll_cadence"]) == ""

    # Measured on the take's own frames: one a frame of its rate, the shift near-constant.
    moving = [(t, name) for t, name in paced.frames if START + 0.05 < t < START + SECONDS - 0.05]
    assert len(moving) >= 0.95 * fps * (SECONDS - 0.1) - 1
    moved = shifts(tmp_path, moving, crop)
    assert_steady(moved)
    assert statistics.fmean(moved) == pytest.approx(DISTANCE / SECONDS / fps, abs=0.5)


def _wheel_log(fps: int) -> dict:
    """The wheel's turns the recorder logs for the step (``capture``): one a frame of the take, on its clock."""
    sent = list(itertools.accumulate(capture.wheel_deltas(round(DISTANCE), SECONDS, fps)))
    return {"t0": START, "pixels": round(DISTANCE), "turns": [[0.0, 0], *([round(i / fps, 4), s] for i, s in enumerate(sent))]}


def _late(t: float, k: int, fps: int, kind: str, dropped: int) -> float:
    """Where a loaded host's frame *k* at *t* shows the page: a paint dropped (the frame before shown again), or every
    other frame painted a fifth of a frame late - jitter the painted cadence itself reads as even."""
    if kind == "dropped":
        return _offset(t - 1 / fps) if k == dropped else _offset(t)
    return _offset(t - 0.2 / fps) if k % 2 else _offset(t)


@pytest.mark.parametrize("kind", ["dropped", "jitter"])
@pytest.mark.parametrize("fps", [60, 30])
def test_a_scroll_the_wheel_turned_painted_at_its_rate_by_a_loaded_host_is_drawn_again_on_the_wheel_clock(tmp_path, fps, kind):
    """A loaded host drops a paint mid-scroll, or paints every other frame late: what it painted carries its load. The
    wheel turned on the take's frame clock, so the scroll is drawn again on it, the same distance every frame."""
    frames, said, events = _stepping_take(tmp_path, fps)
    page = _page()
    dropped = round((START + SECONDS / 2) * fps)
    for k, (t, name) in enumerate(frames):
        shown = _late(t, k, fps, kind, dropped)
        Image.fromarray(_view(page, shown)).save(tmp_path / name, quality=95)
        said[name] = round(shown)
    events[0]["scrolls"][0]["wheel"] = _wheel_log(fps)
    crop = (slice(HEADER + 20, VIEW_H - 20), slice(100, 700))
    painted_moving = [(t, name) for t, name in frames if START + 0.1 < t < START + SECONDS - 0.1]
    before = shifts(tmp_path, painted_moving, crop)
    assert max(before) - min(before) >= 0.2 * statistics.fmean(before), "what the host painted is uneven"

    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=fps, pixel_ratio=1.0, painted=said)
    [scroll] = events[0]["scrolls"]
    assert scroll["painted"] >= scroll_pacing.PAINTED_ENOUGH * fps and scroll["paced"] and scroll["fps"] == float(fps), scroll
    assert "held" not in scroll and not scroll["cadence"]["stepping"], scroll
    moving = [(t, name) for t, name in paced.frames if START + 0.1 < t < START + SECONDS - 0.1]
    assert len(moving) >= 0.95 * fps * (SECONDS - 0.2) - 1
    moved = shifts(tmp_path, moving, crop)
    assert_steady(moved)
    # On the wheel's clock: every frame the same distance, to the rounding of a pixel.
    assert max(moved) - min(moved) <= 2 and statistics.fmean(moved) == pytest.approx(DISTANCE / SECONDS / fps, rel=0.05), moved


def test_a_scroll_painted_at_its_rate_and_steady_keeps_its_frames_and_its_cadence(tmp_path):
    frames, said, events = _stepping_take(tmp_path, 60)
    page = _page()
    for t, name in frames:
        Image.fromarray(_view(page, _offset(t))).save(tmp_path / name, quality=95)
        said[name] = round(_offset(t))
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=60, pixel_ratio=1.0, painted=said)
    [scroll] = events[0]["scrolls"]
    assert paced.frames == frames and scroll["why"] == scroll_pacing.PAINTED_AT_RATE and not scroll["paced"]
    assert scroll["cadence"]["mean"] == pytest.approx(10.0, abs=0.5) and not scroll["cadence"]["stepping"]
    assert scroll_pacing.even(scroll["cadence"])


def test_the_action_log_says_a_step_whose_scroll_steps():
    from app.services.video_studio import jobs

    stepping = {"painted_fps": 58.3, "min": 0.0, "max": 21.0, "mean": 10.6, "stdev": 8.4, "stepping": True}
    take = SimpleNamespace(events=[{"step": 2, "action": "scroll", "t0": 1.0, "t1": 3.0, "scroll_cadence": stepping}])
    [line] = jobs.take_timeline(take)
    assert line["scroll_cadence"] == stepping
    assert scroll_pacing.cadence_words(line["scroll_cadence"]).startswith(" (stepping motion: its scroll moved up to 21 px a frame")
    # A take recorded before keeps reading: no cadence, nothing said.
    [old] = jobs.take_timeline(SimpleNamespace(events=[{"step": 0, "action": "scroll"}]))
    assert old["scroll_cadence"] is None and scroll_pacing.cadence_words(old["scroll_cadence"]) == ""
