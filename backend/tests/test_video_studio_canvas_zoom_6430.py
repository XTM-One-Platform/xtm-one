"""A scroll step that zooms a canvas or WebGL graph plays at its take's rate.

A wheel over a graph (OpenCTI's knowledge graph, a report graph) zooms its canvas and moves no
scroller, so the scroll was kept as "it did not move", the take judged on the frames it painted over its length (1.4
to 5 fps at pixel ratio 1.5 or 2) and refused at save ("scrolls will step"). What is proven here, on frames made the way
the screencast makes them - a graph in a canvas under a page header, a toolbar over it, zoomed 2x about the cursor
over 1.5 s and painted at 2.1 fps at 2400x1350, JPEG at the screencast's quality:

* the zoom, recorded at fps 60, plays at 60 frames a second: 90 frames, each the graph as zoomed at its instant (never
  the keyframe before, held), the toolbar over it and the header standing still - whether the canvas's log follows the
  wheel's delta (a WebGL canvas) or its 2D transform;
* a wheel over a canvas that zoomed nothing moves nothing: the take is no longer judged on its paint rate - but one
  logged before a canvas was looked for still is;
* over a canvas the recorder turns the wheel on the take's clock, each turn painted before the next;
* the take's answer says each scroll step's painted rate and whether it was drawn again from its keyframes.

A WebGL force graph in a real Chromium: ``test_video_studio_canvas_zoom_chromium``.
"""

from __future__ import annotations

import asyncio
import itertools
import math
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from app.models.video_studio import JOB_READY, VideoTake
from app.services.builtin_tools import video_studio as tool
from app.services.video_studio import capture, scroll_pacing
from app.services.video_studio.spec import parse_take_script, validate_take_script
from app.tools.base import ToolCall

RATIO = 1.5
FRAME = (2400, 1350)
#: The canvas under a 60 CSS px header: its client box in CSS px, and in device px of the frame.
BOX_CSS = (0.0, 60.0, 1600.0, 840.0)
BOX = (0, 90, 2400, 1350)
#: Where the wheel turns (CSS px of the viewport), and the point the graph zooms about (device px of the canvas).
CURSOR = (900.0, 480.0)
ANCHOR = (CURSOR[0] * RATIO - BOX[0], CURSOR[1] * RATIO - BOX[1])
#: A toolbar the page draws over the canvas (device px of the frame), and its client box as the recorder reads it.
TOOLBAR = (60, 120, 762, 201)
TOOLBAR_CSS = [40.0, 80.0, 468.0, 54.0]
#: The zoom: at rest until 1.0 s, 2x by 2.5 s; logged from 0.9 s to 2.9 s; the screencast's pace at this size.
START, END, ZOOM = 1.0, 2.5, 2.0
LOG_FROM, LOG_TO = 0.9, 2.9
PAINT_FPS = 2.1
FPS = 60
BY = -600


def _share(t: float) -> float:
    return min(max((t - START) / (END - START), 0.0), 1.0)


def _scale(t: float) -> float:
    """How far the graph is zoomed at *t*: a zoom per turn of the wheel, as a graph's wheel zooms."""
    return math.exp(math.log(ZOOM) * _share(t))


def _world() -> Image.Image:
    """The graph at rest, the canvas's size: nodes of every colour joined by links, on a dark ground."""
    rng = np.random.default_rng(6430)
    width, height = BOX[2] - BOX[0], BOX[3] - BOX[1]
    world = Image.new("RGB", (width, height), (12, 16, 28))
    draw = ImageDraw.Draw(world)
    nodes = [(float(rng.uniform(40, width - 40)), float(rng.uniform(40, height - 40))) for _ in range(150)]
    for _ in range(230):
        a, b = rng.integers(0, len(nodes), 2)
        draw.line([nodes[a], nodes[b]], fill=(70, 92, 124), width=3)
    for i, (x, y) in enumerate(nodes):
        r = 8 + (i * 7) % 12
        draw.ellipse([x - r, y - r, x + r, y + r], fill=tuple(int(v) for v in rng.integers(90, 250, 3)))
    return world.filter(ImageFilter.GaussianBlur(1.2))


def _view(world: Image.Image, t: float, *, still: bool = False) -> np.ndarray:
    """The frame the page shows at *t*: its header, the graph zoomed about the cursor in the canvas, the toolbar over."""
    k = 1.0 if still else _scale(t)
    cx, cy = ANCHOR
    canvas = world.transform(
        world.size, Image.Transform.AFFINE, (1 / k, 0, cx * (1 - 1 / k), 0, 1 / k, cy * (1 - 1 / k)), resample=Image.Resampling.BILINEAR
    )
    frame = Image.new("RGB", FRAME, (20, 24, 36))
    frame.paste(canvas, (BOX[0], BOX[1]))
    draw = ImageDraw.Draw(frame)
    draw.rectangle([30, 20, 600, 70], fill=(230, 232, 240))
    x0, y0, x1, y1 = TOOLBAR
    draw.rectangle([x0, y0, x1 - 1, y1 - 1], fill=(40, 44, 62))
    for i in range(13):
        draw.rectangle([x0 + 20 + i * 52, y0 + 24, x0 + 52 + i * 52, y0 + 54], fill=(205, 210, 222))
    return np.asarray(frame)


def _take(tmp_path, world: Image.Image, *, transform: bool = False, still: bool = False) -> tuple[list[tuple[float, str]], list[dict]]:
    """The keyframes the screencast hands over and the scroll step's event with its log, as the recorder keeps them:
    the window never moves, the canvas under the wheel logged with each turn of it."""
    frames = []
    t, k = 0.05, 0
    while t < 3.5:
        name = f"f{k:06d}.jpg"
        Image.fromarray(_view(world, t, still=still)).save(tmp_path / name, quality=95)
        frames.append((round(t, 4), name))
        t += 1 / PAINT_FPS
        k += 1
    samples, ticks = [], []
    s = LOG_FROM
    while s <= LOG_TO + 1e-9:
        samples.append([round(s, 4), 0, 0, []])
        scale = 1.0 if still else _scale(s)
        # A 2D canvas draws under its zoom, at the pixel ratio; a WebGL one has no transform to read.
        matrix = [scale * RATIO, 0, 0, scale * RATIO, 12.0, -30.0] if transform else None
        ticks.append([round(s, 4), round(BY * _share(s)), matrix])
        s += 1 / FPS
    surface = {
        "kind": "canvas",
        "context": "2d" if transform else "webgl2",
        "box": list(BOX_CSS),
        "over": [TOOLBAR_CSS],
        "at": list(CURSOR),
        "ticks": ticks,
    }
    event = {"step": 1, "action": "scroll", "ok": True, "t0": START, "t1": END, "scrolls": [{"t": [LOG_FROM, LOG_TO], "samples": samples, "surface": surface}]}
    return frames, [event]


def _decoded(tmp_path, name: str) -> np.ndarray:
    with Image.open(tmp_path / name) as opened:
        return np.asarray(opened.convert("RGB")).astype(np.int16)


def _graph(picture: np.ndarray) -> np.ndarray:
    """The canvas of a frame without the toolbar over it."""
    x0, y0, x1, y1 = BOX
    canvas = picture[y0:y1, x0:x1].astype(np.int16)
    mask = np.ones(canvas.shape[:2], dtype=bool)
    mask[TOOLBAR[1] - y0 - 8 : TOOLBAR[3] - y0 + 8, TOOLBAR[0] - x0 - 8 : TOOLBAR[2] - x0 + 8] = False
    return canvas[mask]


@pytest.mark.parametrize("transform", [False, True], ids=["webgl-wheel", "2d-transform"])
def test_a_canvas_zoomed_by_the_wheel_plays_at_the_take_s_60_fps(tmp_path, transform):
    world = _world()
    frames, events = _take(tmp_path, world, transform=transform)
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=FPS, pixel_ratio=RATIO)
    [scroll] = events[0]["scrolls"]
    assert scroll["scroller"] == "canvas" and scroll["paced"] and scroll["fps"] == 60.0, scroll
    assert scroll["painted"] < 3 and scroll["offsets"] == "registered" and "held" not in scroll
    assert scroll["progress"] == ("transform" if transform else "wheel")
    assert scroll["zoom"] == pytest.approx(ZOOM, abs=0.05) and scroll["still_share"] > 0
    assert "samples" not in scroll and "surface" not in scroll
    during = [(t, name) for t, name in paced.frames if START <= t < END]
    assert len(during) == 90
    x0, y0, x1, y1 = TOOLBAR
    for t, name in during[::3]:
        shown = _decoded(tmp_path, name)
        expected = _view(world, t)
        # The graph as zoomed at its instant, within the two keyframes' resampling (1 to 2.6 grey levels) - never the
        # keyframe before held while it zooms on (20 to 33 off).
        error = float(np.abs(_graph(shown) - _graph(expected)).mean())
        held = next(frame for when, frame in reversed(frames) if when <= t)
        stepped = float(np.abs(_graph(_decoded(tmp_path, held)) - _graph(expected)).mean())
        assert error < 3.0 and error <= stepped + 0.5, f"the frame at {t:.3f} s is the graph zoomed {_scale(t):.3f}x: {error:.2f}"
        assert stepped < 6.0 or error < 0.25 * stepped, f"the frame at {t:.3f} s holds its keyframe: {error:.2f} / {stepped:.2f}"
        # The toolbar over the canvas and the header above it stand still.
        assert np.abs(shown[y0:y1, x0:x1] - expected[y0:y1, x0:x1].astype(np.int16)).mean() < 3.0, f"the toolbar at {t:.3f} s"
        assert np.abs(shown[: BOX[1]] - expected[: BOX[1]].astype(np.int16)).mean() < 3.0, f"the header at {t:.3f} s"
    previous = None
    for t, name in during:
        with Image.open(tmp_path / name) as opened:
            shown = np.asarray(opened.convert("L").reduce(4), dtype=np.int16)
        if previous is not None and 0 < _share(t) < 1:
            assert np.abs(shown - previous).mean() > 0.3, f"the frame at {t:.3f} s repeats the one before"
        previous = shown
    rate = scroll_pacing.take_rate(events, captured_frames=len(frames), duration=3.5)
    assert (rate.fps, rate.scrolls, rate.paced, rate.steps) == (60.0, True, True, False)
    assert rate.words() == f"scrolls at 60 fps ({round(60 - scroll['painted'])} frames a second synthesised between keyframes)"


def test_a_wheel_over_a_canvas_that_zoomed_nothing_moves_nothing(tmp_path):
    world = _world()
    frames, events = _take(tmp_path, world, still=True)
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=FPS, pixel_ratio=RATIO)
    [scroll] = events[0]["scrolls"]
    assert paced.frames == frames and not scroll["paced"] and scroll["why"] == scroll_pacing.NOTHING_MOVED
    # The take scrolls nowhere: it is no longer judged on the 2.1 fps it painted.
    rate = scroll_pacing.take_rate(events, captured_frames=len(frames), duration=3.5)
    assert (rate.scrolls, rate.steps, rate.words()) == (False, False, "2.3 fps painted")
    # A scroll step that moved no scroller, logged before a canvas was looked for: it may have zoomed one.
    legacy = [{"step": 1, "action": "scroll", "scrolls": [{"painted": 2.1, "fps": 2.1, "paced": False, "why": scroll_pacing.DID_NOT_MOVE}]}]
    assert scroll_pacing.take_rate(legacy, captured_frames=8, duration=3.5).steps
    # A wheel over a page with no canvas that moved nothing moves nothing too.
    samples = [[0.1 * i, 0, 0, []] for i in range(10)]
    window = [{"step": 2, "action": "scroll", "scrolls": [{"t": [0.0, 0.9], "samples": samples, "surface": None}]}]
    scroll_pacing.pace(frames, window, workdir=str(tmp_path), fps=FPS, pixel_ratio=RATIO)
    assert window[0]["scrolls"][0]["why"] == scroll_pacing.NOTHING_MOVED
    assert not scroll_pacing.take_rate(window, captured_frames=8, duration=3.5).steps


def test_a_canvas_s_progress_follows_its_transform_or_the_wheel():
    ticks = [[0.1 * i, -40 * i, [1.5 * 1.1**i, 0, 0, 1.5 * 1.1**i, 5.0, 5.0]] for i in range(6)]
    log, progress = scroll_pacing.canvas_progress(ticks)
    assert progress == "transform" and log[3][1] - log[0][1] == pytest.approx(3 * math.log(1.1))
    # A 2D canvas that draws under a transform it resets reads the same one each turn: the wheel's delta is followed.
    flat = [[0.1 * i, -40 * i, [1.5, 0, 0, 1.5, 0, 0]] for i in range(6)]
    assert scroll_pacing.canvas_progress(flat) == ([(0.1 * i, -40.0 * i) for i in range(6)], "wheel")


def test_a_zoom_between_two_keyframes_is_read_off_their_pixels():
    world = _world()
    a = np.asarray(Image.fromarray(_view(world, 1.2)).convert("L"), dtype=np.float32)[BOX[1] : BOX[3], BOX[0] : BOX[2]]
    b = np.asarray(Image.fromarray(_view(world, 1.7)).convert("L"), dtype=np.float32)[BOX[1] : BOX[3], BOX[0] : BOX[2]]
    levels = [scroll_pacing.zoom_levels(a), scroll_pacing.zoom_levels(b)]
    still, _boxes = scroll_pacing.standing(levels)
    centre = ((a.shape[1] - 1) / 2, (a.shape[0] - 1) / 2)
    zoom = scroll_pacing.register_zoom(levels[0], levels[1], [ANCHOR, centre], still)
    assert zoom is not None and zoom.scale == pytest.approx(_scale(1.7) / _scale(1.2), abs=0.004)
    # The point it zooms about is the cursor: it does not move.
    fixed = (zoom.tx / (1 - zoom.scale), zoom.ty / (1 - zoom.scale))
    assert math.dist(fixed, ANCHOR) < 4.0
    assert zoom.share(1.0) == pytest.approx(zoom) and zoom.share(0.0) == scroll_pacing.IDENTITY
    assert scroll_pacing.register_zoom(levels[0], levels[0], [ANCHOR], still) == scroll_pacing.IDENTITY


# ── The recorder turns the wheel over a canvas on the take's clock ────────


class _Clock:
    """The recorder's clock, virtual: ``time.monotonic``/``time.time`` read it and ``asyncio.sleep`` advances
    it, so the wheel's turns are counted on the take's clock whatever the host's load - a busy xdist runner waking the
    loop late made the real clock catch up on fewer, larger turns (60 turns where 63 were the floor)."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return 1_700_000_000.0 + self.now

    async def sleep(self, seconds: float, result=None):
        # A real clock always moves on: a sleep's remainder rounded to nothing never leaves it where it was.
        self.now += max(1e-6, float(seconds))
        await _REAL_SLEEP(0)
        return result


_REAL_SLEEP = asyncio.sleep


class _Asyncio:
    """``asyncio`` as the recorder module sees it, its ``sleep`` on the virtual clock and everything else the real one."""

    def __init__(self, clock: _Clock) -> None:
        self.sleep = clock.sleep

    def __getattr__(self, name: str):
        return getattr(asyncio, name)


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    virtual = _Clock()
    monkeypatch.setattr(capture, "time", SimpleNamespace(monotonic=virtual.monotonic, time=virtual.time))
    monkeypatch.setattr(capture, "asyncio", _Asyncio(virtual))
    return virtual


class _CanvasPage:
    """A page with a canvas under the cursor that takes *paint* seconds of *clock* to paint the frame each turn of the
    wheel drew."""

    def __init__(self, paint: float, clock: _Clock) -> None:
        self.paint = paint
        self.clock = clock
        self.calls: list[tuple[str, float, float]] = []
        self.mouse = self

    async def wheel(self, _x: float, y: float) -> None:
        self.calls.append(("wheel", self.clock.monotonic(), y))

    async def evaluate(self, script: str, _arg=None):
        if script == capture._CANVAS_AT_JS:
            return {"context": "webgl2", "box": list(BOX_CSS), "over": [TOOLBAR_CSS]}
        if script == capture._CANVAS_TICK_JS:
            await self.clock.sleep(self.paint)
            self.calls.append(("painted", self.clock.monotonic(), 0))
            return [self.clock.time() * 1000, None]
        return None


def _recorder(page: _CanvasPage, fps: int):
    recorder = capture._Recorder.__new__(capture._Recorder)
    recorder.page = page
    recorder.script = SimpleNamespace(pace="natural", fps=fps)
    recorder.x, recorder.y = CURSOR
    recorder.scrolls = []
    recorder.t_start = page.clock.time()
    # Software GL, as on the CI runner: the wheel holds the canvas copies (#10897).
    recorder.gpu = False

    async def settled() -> None:
        return None

    recorder._wait_scroll_end = settled
    return recorder


@pytest.mark.parametrize(("fps", "paint"), [(60, 0.004), (30, 0.004), (60, 0.05)], ids=["60fps", "30fps", "slow-paint"])
async def test_over_a_canvas_the_wheel_turns_on_the_take_s_clock_each_turn_painted_before_the_next(fps, paint, clock):
    page = _CanvasPage(paint, clock)
    recorder = _recorder(page, fps)
    step = parse_take_script(validate_take_script({"steps": [{"action": "scroll", "by": BY, "seconds": 1.5}]})).steps[0]
    await recorder._scroll(step)
    [logged] = recorder.scrolls
    surface = logged["surface"]
    assert surface["box"] == list(BOX_CSS) and surface["at"] == list(CURSOR) and surface["context"] == "webgl2"
    assert surface["over"] == [TOOLBAR_CSS]
    kinds = [kind for kind, _t, _y in page.calls]
    # Every turn is painted before the next one is sent.
    assert all(b == "painted" for a, b in itertools.pairwise(kinds) if a == "wheel")
    turns = [(t, y) for kind, t, y in page.calls if kind == "wheel"]
    assert sum(y for _t, y in turns) == BY
    assert turns[-1][0] - turns[0][0] == pytest.approx(1.5, abs=0.15), "the wheel turned over the step's seconds"
    if paint < 1 / fps:
        # On the take's clock: about fps turns a second (a coarse timer catches up on fewer, larger turns).
        assert len(turns) >= 0.7 * fps * 1.5
        assert len(turns) <= fps * 1.5 + 2
    else:
        # A canvas painting slower than the clock: as many turns as it paints, the wheel's delta caught up on each.
        assert len(turns) <= 1.5 / paint + 2
    # Logged at rest, at each turn, then read on as the canvas settles.
    ticks = surface["ticks"]
    settled = [tick[0] for tick in ticks if tick[1] == BY]
    assert ticks[0][1] == 0 and len(ticks) == 1 + len(turns) + len(settled) - 1
    assert settled[-1] - settled[0] >= capture._CANVAS_SETTLE_SECONDS[0] - 0.05


async def test_a_wheel_over_no_canvas_turns_as_before(clock):
    page = _CanvasPage(0.0, clock)

    async def nothing(script: str, _arg=None):
        return None

    page.evaluate = nothing
    recorder = _recorder(page, 60)
    step = parse_take_script(validate_take_script({"steps": [{"action": "scroll", "by": 600, "seconds": 0.5}]})).steps[0]
    await recorder._scroll(step)
    [logged] = recorder.scrolls
    assert logged["surface"] is None
    assert [kind for kind, _t, _y in page.calls] == ["wheel"] * len(page.calls)


# ── The take's answer says each scroll step's rates ───────────────────────


def _events() -> list[dict]:
    return [
        {"step": 0, "action": "goto"},
        {"step": 1, "action": "scroll", "scrolls": [{"scroller": "canvas", "painted": 2.13, "fps": 60.0, "paced": True}]},
        {"step": 2, "action": "click", "scrolls": [{"scroller": "window", "painted": 31.0, "fps": 31.0, "paced": False, "why": "painted at its rate"}]},
        {"step": 3, "action": "scroll", "scrolls": [{"scroller": "element", "painted": 2.4, "fps": 2.4, "paced": False, "why": "two scrollers moved at once"}]},
        {"step": 4, "action": "scroll", "scrolls": [{"scroller": "window", "painted": 2.0, "fps": 2.0, "paced": False, "why": scroll_pacing.NOTHING_MOVED}]},
    ]


def test_the_scroll_steps_are_said_with_their_painted_rate():
    assert scroll_pacing.scroll_steps_words(_events()) == (
        "Scroll steps: step 1 (scroll) zoomed a canvas: painted at 2.1 fps, drawn again from its keyframes at 60 fps; "
        "step 2 (click) scrolled the window: painted at 31.0 fps, kept as painted, at its rate; "
        "step 3 (scroll) scrolled a scroll container: painted at 2.4 fps, not drawn again (two scrollers moved at once): "
        "it steps; step 4 (scroll) moved nothing."
    )
    assert scroll_pacing.scroll_steps_words([{"step": 0, "action": "click"}]) == ""


async def test_record_browser_demo_s_answer_names_each_scroll_step_s_painted_rate(monkeypatch):
    user_id = uuid.uuid4()
    take = VideoTake(
        id=uuid.uuid4(),
        user_id=user_id,
        name="Graph zoom",
        status=JOB_READY,
        progress=100,
        duration_ms=6000,
        viewport_width=1600,
        viewport_height=900,
        pixel_ratio=1.5,
        fps=60,
        captured_frames=13,
        events=_events()[:2],
        script={"steps": [{"action": "scroll", "by": BY}], "fps": 60, "pixel_ratio": 1.5},
    )

    class _Session:
        async def get(self, model, key):
            if model is VideoTake:
                return take if str(key) == str(take.id) else None
            return SimpleNamespace(id=user_id, role="member")

    @asynccontextmanager
    async def session():
        yield _Session()

    monkeypatch.setattr(tool, "async_session", session)
    _definitions, executor = tool.build_video_studio_tools(None, str(user_id))
    answer = await executor.execute_tool(
        ToolCall(id="c1", name="get_video_recording", arguments={"recording_id": str(take.id), "include_contact_sheet": False})
    )
    assert "scrolls at 60 fps (58 frames a second synthesised between keyframes)" in answer.content
    assert "Scroll steps: step 1 (scroll) zoomed a canvas: painted at 2.1 fps, drawn again from its keyframes at 60 fps." in answer.content
