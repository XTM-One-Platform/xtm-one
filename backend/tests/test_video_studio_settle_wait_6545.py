"""A recording's wait holds until its page settled, and a hold on a page still loading is said.

What is proven here without a browser (the real Chromium takes: ``test_video_studio_settle_wait_6545_chromium``):

* ``until_settled`` goes with a ``wait`` only, and is on by default after a click that opened another page or switched
  a tab;
* :class:`page_settle.PageWatch` settles a page once no progress indicator shows and its DOM and canvases stood still
  0.5 s - twice its paint interval for a canvas painting slower -, and keeps the spans it showed loading, an empty
  canvas included;
* a ``pause`` or a ``highlight`` is flagged for every second it showed loading, a ``wait`` for the loading it ends on;
* the take's answer says each such hold and when a wait settled, and the save raises ``take_loading_hold`` on a
  recording scene keeping it - never on one trimmed past it.
"""

from __future__ import annotations

import io
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from app.models.video_studio import JOB_READY, VideoTake
from app.services.builtin_tools import video_studio as tool
from app.services.video_studio import page_settle, quality, spec, timeline
from app.services.video_studio.page_settle import PageRead, PageWatch
from app.tools.base import ToolCall

BAR = "span.MuiLinearProgress-root.MuiLinearProgress-indeterminate[progressbar]"
GRAPH = ("canvas#graph", (100.0, 200.0, 800.0, 500.0))


def _step(**fields) -> spec.TakeStep:
    return spec.TakeStep.model_validate(fields)


# ── The step ──────────────────────────────────────────────────────────


def test_until_settled_goes_with_a_wait_only():
    assert _step(action="wait", seconds=6, until_settled=True).until_settled is True
    with pytest.raises(ValueError, match="until_settled goes with a wait"):
        _step(action="pause", seconds=2, until_settled=True)


def test_a_wait_holds_until_settled_by_default_after_a_click_that_navigated_or_switched_a_tab():
    wait = _step(action="wait", seconds=6)
    tab = {"step": 0, "action": "click", "url": "https://app/e/1", "url_before": "https://app/e/1", "tab": True}
    navigated = {"step": 0, "action": "click", "url": "https://app/e/1/knowledge", "url_before": "https://app/e/1"}
    stayed = {"step": 0, "action": "click", "url": "https://app/e/1", "url_before": "https://app/e/1"}
    marker = {"step": 1, "action": "marker"}
    assert page_settle.until_settled(wait, [tab]) and page_settle.until_settled(wait, [navigated])
    assert page_settle.until_settled(wait, [tab, marker])
    assert not page_settle.until_settled(wait, [stayed])
    assert not page_settle.until_settled(wait, [{**tab, "action": "hover"}])
    assert not page_settle.until_settled(wait, [{**tab, "ok": False}])
    assert not page_settle.until_settled(wait, [])
    assert not page_settle.until_settled(_step(action="wait", seconds=6, until_settled=False), [tab])
    assert page_settle.until_settled(_step(action="wait", seconds=6, until_settled=True), [stayed])
    assert not page_settle.until_settled(_step(action="pause", seconds=2), [tab])


def test_the_watch_s_answer_is_read_in_seconds_of_the_take():
    raw = {"t": 12_500.0, "changed": 11_000.0, "busy": [BAR], "canvases": [["canvas#graph", [100, 200, 800, 500]], "bad"]}
    read = page_settle.page_read(raw, origin=10.0, at=9.9)
    assert read == PageRead(t=2.5, changed=1.0, busy=(BAR,), canvases=(GRAPH,))
    assert page_settle.page_read(None, origin=10.0, at=3.0) == PageRead(t=3.0, changed=3.0)


# ── Settled or not ────────────────────────────────────────────────────


def _graph(offset: int) -> np.ndarray:
    """A gray canvas of nodes, all moved *offset* px."""
    picture = np.full((250, 400), 20, dtype=np.int16)
    for i in range(8):
        x, y = 30 + i * 45 + offset, 60 + (i % 3) * 50
        picture[y : y + 16, x : x + 16] = 200
    return picture


def test_a_page_settles_once_its_progress_bar_cleared_and_its_dom_stood_still():
    watch = PageWatch(began=1.0)
    for t in (1.0, 1.2, 1.4, 6.2):
        watch.observe(PageRead(t=t, changed=1.0, busy=(BAR,)))
        assert watch.settled_at(t) is None
    # The bar gave way to the panel at 6.3 s: the DOM changed, then stood still.
    watch.observe(PageRead(t=6.4, changed=6.3))
    assert watch.settled_at(6.4) is None
    watch.observe(PageRead(t=6.8, changed=6.3))
    assert watch.settled_at(6.8) == pytest.approx(6.3)
    watch.close(6.8)
    assert watch.loading == [[1.0, 6.4, [BAR]]]
    assert watch.logged(until_settled=True, settled=6.3, seconds=2.0, ended=6.8) == {
        "loading": [[1.0, 6.4, [BAR]]],
        "until_settled": True,
        "settled": {"at": 6.3, "after": 5.3, "seconds": 2.0},
    }


def test_a_page_already_still_is_settled_from_the_hold_s_start():
    watch = PageWatch(began=4.0)
    watch.observe(PageRead(t=4.0, changed=2.0))
    assert watch.settled_at(4.0) == 4.0
    assert watch.logged(until_settled=False, settled=None, seconds=2.0, ended=6.0) == {}


def test_a_canvas_is_settled_once_its_pixels_stop_changing_even_repainted():
    key = page_settle.canvas_key(*GRAPH)
    watch = PageWatch(began=0.0)
    for i, t in enumerate((0.0, 0.2, 0.4, 0.6, 0.8)):
        watch.observe(PageRead(t=t, changed=0.0, canvases=(GRAPH,)), {key: _graph(i * 3)}, frame_t=t - 0.05, gap=0.05)
        assert watch.settled_at(t) is None
    # It stopped at 0.75 s and is painted again, the same, on every frame.
    for t in (1.0, 1.2, 1.4):
        watch.observe(PageRead(t=t, changed=0.0, canvases=(GRAPH,)), {key: _graph(12)}, frame_t=t - 0.05, gap=0.05)
    assert watch.settled_at(1.2) is None
    assert watch.settled_at(1.4) == pytest.approx(0.75)
    assert watch.loading == []


def test_a_canvas_painting_slowly_must_stand_still_twice_its_paint_interval():
    key = page_settle.canvas_key(*GRAPH)
    watch = PageWatch(began=0.0)
    # Painted every 0.48 s (2.1 frames a second): the frame read stays the same between two paints.
    frames = [(0.0, 0), (0.48, 3), (0.96, 6), (1.44, 9)]
    t = 0.0
    while t <= 2.6:
        painted, offset = max((f for f in frames if f[0] <= t), key=lambda f: f[0])
        gap = 0.48 if painted else 0.0
        watch.observe(PageRead(t=t, changed=0.0, canvases=(GRAPH,)), {key: _graph(offset)}, frame_t=painted, gap=gap)
        if t < 1.44 + 2 * 0.48 - 1e-6:
            # Half a second without a new paint is no stop when the page paints every 0.48 s.
            assert watch.settled_at(t) is None, t
        t = round(t + 0.2, 2)
    assert watch.still_needed() == pytest.approx(0.96)
    assert watch.settled_at(2.6) == pytest.approx(1.44)


def test_a_canvas_painting_nothing_is_a_page_loading():
    key = page_settle.canvas_key(*GRAPH)
    blank = np.full((250, 400), 20, dtype=np.int16)
    watch = PageWatch(began=2.0)
    for t in (2.0, 2.2, 2.4):
        watch.observe(PageRead(t=t, changed=1.0, canvases=(GRAPH,)), {key: blank}, frame_t=t, gap=0.05)
    watch.observe(PageRead(t=2.6, changed=1.0, canvases=(GRAPH,)), {key: _graph(0)}, frame_t=2.55, gap=0.05)
    watch.close(3.0)
    assert watch.loading == [[2.0, 2.6, ["canvas#graph (empty)"]]]
    assert watch.moving(2.7) == ["a canvas still repainting"]


def test_the_frame_s_canvases_are_read_off_its_pixels():
    frame = np.full((900, 1600, 3), 30, dtype=np.uint8)
    frame[300:600, 400:900] = (250, 250, 250)
    buffer = io.BytesIO()
    Image.fromarray(frame).save(buffer, "JPEG", quality=95)
    canvases = [("canvas#full", (400.0, 300.0, 500.0, 300.0)), ("canvas#blank", (1000.0, 300.0, 400.0, 300.0))]
    crops = page_settle.canvas_crops(buffer.getvalue(), 1600, canvases)
    full, blank = (crops[page_settle.canvas_key(*canvas)] for canvas in canvases)
    # Read at half its size (800 px wide): the boxes in CSS px land on the same pixels.
    assert full.shape == (150, 250) and blank.shape == (150, 200)
    assert page_settle.is_empty(full) and page_settle.is_empty(blank)
    assert page_settle.changed_share(full, full) == 0.0
    assert not page_settle.is_empty(_graph(0))


# ── The holds flagged ─────────────────────────────────────────────────


def test_a_pause_is_flagged_for_every_second_it_showed_loading_and_a_wait_for_what_it_ends_on():
    pause = {"step": 5, "action": "pause", "t0": 12.0, "t1": 15.0, "loading": [[12.0, 13.1, [BAR]], [13.5, 14.5, ["canvas#graph (empty)"]]]}
    hold = page_settle.loading_hold(pause)
    assert hold == page_settle.Hold(5, "pause", 2.1, (12.0, 14.5), (BAR, "canvas#graph (empty)"))
    assert page_settle.hold_words(hold) == (
        f"step 5 (pause) holds on a loading page for 2.1 s (12.0-14.5 s: {BAR}, canvas#graph (empty))"
    )
    # A wait that loaded, then showed its page loaded, did its job; one ending on a page loading did not.
    loaded = {"step": 2, "action": "wait", "t0": 3.0, "t1": 9.004, "loading": [[3.0, 6.0, [BAR]]]}
    ending = {**loaded, "loading": [[3.0, 4.0, [BAR]], [7.5, 9.0, [BAR]]]}
    assert page_settle.loading_hold(loaded) is None
    assert page_settle.loading_hold(ending) == page_settle.Hold(2, "wait", 1.5, (7.5, 9.0), (BAR,))
    # One that gave up before its page settled says so, however little of it the last read saw.
    gave_up = {
        **loaded,
        "loading": [[9.0, 9.0, [BAR]]],
        "until_settled": True,
        "settled": {"at": None, "waited": 6.0, "seconds": 2.0},
    }
    assert page_settle.loading_hold(gave_up).gave_up
    assert "its wait gave up before the page settled" in page_settle.hold_words(page_settle.loading_hold(gave_up))
    # A blip under a read's interval, a step that is no hold, a skipped one: nothing.
    assert page_settle.loading_hold({**pause, "loading": [[12.0, 12.1, [BAR]]]}) is None
    assert page_settle.loading_hold({**pause, "action": "click"}) is None
    assert page_settle.loading_hold({**pause, "ok": False}) is None


def test_the_action_log_says_when_a_wait_settled():
    assert page_settle.settled_words({"at": 19.7, "after": 7.4, "seconds": 6}) == (
        " (until settled: settled at 19.7 s, 7.4 s in, past its 6 s)"
    )
    assert page_settle.settled_words({"at": 14.0, "after": 1.7, "seconds": 6}) == " (until settled: settled at 14.0 s, 1.7 s in)"
    assert page_settle.settled_words({"at": None, "waited": 16.0, "seconds": 6}) == (
        " (until settled: not settled when it gave up after 16.0 s)"
    )
    assert page_settle.settled_words(None) == ""


def test_a_snapshot_says_how_long_its_page_took_to_settle():
    assert page_settle.snapshot_words(7.4, 8.0, []).startswith("It settled 7.4 s after it opened")
    assert page_settle.snapshot_words(None, 20.0, [BAR]).startswith(f"It had not settled 20 s after it opened ({BAR})")


# ── Where it is said ──────────────────────────────────────────────────

TAKE_ID = "00000000-0000-4000-8000-000000006545"
EVENTS = [
    {"step": 0, "action": "goto", "t0": 0.0, "ta": 0.0, "t1": 1.0, "url": "https://app/e/1"},
    {"step": 1, "action": "click", "t0": 1.0, "ta": 1.5, "t1": 1.6, "tab": True, "url": "https://app/e/1", "url_before": "https://app/e/1"},
    {
        "step": 2,
        "action": "wait",
        "t0": 1.7,
        "ta": 1.7,
        "t1": 9.6,
        "until_settled": True,
        "settled": {"at": 9.1, "after": 7.4, "seconds": 6.0},
        "loading": [[1.7, 9.1, [BAR]]],
    },
    {"step": 3, "action": "click", "t0": 9.6, "ta": 10.0, "t1": 10.1, "url": "https://app/e/1", "url_before": "https://app/e/1"},
    {"step": 4, "action": "pause", "t0": 10.2, "ta": 10.2, "t1": 13.2, "loading": [[10.2, 12.3, [BAR]]]},
]


def _media() -> timeline.TakeMedia:
    return timeline.TakeMedia(TAKE_ID, 14.0, 1600, 900, 1.0, 60, 840, events=EVENTS, captured_frames=600)


def _findings(scene: dict) -> list:
    authored = spec.validate_video_spec({"title": "Entity", "freestyle": True, "scenes": [{"id": "demo", "type": "recording", "take_id": TAKE_ID, **scene}]})
    parsed = spec.parse_video_spec(authored)
    document = timeline.estimate_plan(parsed, {}, {}).document
    found = quality.content_findings(parsed, document, quality.ContentFacts(takes={TAKE_ID: _media()}), authored)
    return [f for f in found if f.kind == "take_loading_hold"]


def test_the_save_advises_on_a_recording_scene_holding_on_a_page_still_loading():
    [finding] = _findings({})
    assert finding.severity == "advisory" and finding.scene_id == "demo"
    assert finding.message.startswith(
        f"take {TAKE_ID[:8]} shows its page still loading on scene demo: step 4 (pause) holds on a loading page for 2.1 s"
    )
    assert page_settle.REMEDY in finding.message and finding.message.endswith("or trim the scene past it")
    # A scene trimmed past the hold shows none of it.
    assert _findings({"trim_start": 13.0}) == []


async def test_the_take_s_answer_flags_its_holds_and_says_when_a_wait_settled(monkeypatch):
    user_id = uuid.uuid4()
    take = VideoTake(
        id=uuid.UUID(TAKE_ID),
        user_id=user_id,
        name="Knowledge tab",
        status=JOB_READY,
        progress=100,
        duration_ms=14000,
        viewport_width=1600,
        viewport_height=900,
        pixel_ratio=1.0,
        fps=60,
        captured_frames=600,
        events=EVENTS,
        script={"steps": [{"action": "wait", "seconds": 6}], "pixel_ratio": 1},
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
        ToolCall(id="c1", name="get_video_recording", arguments={"recording_id": TAKE_ID, "include_contact_sheet": False})
    )
    assert "  step 2 wait at 1.7 s (until settled: settled at 9.1 s, 7.4 s in, past its 6 s)" in answer.content
    assert (
        f"Holds on a loading page: step 4 (pause) holds on a loading page for 2.1 s (10.2-12.3 s: {BAR}). "
        f"Before placing it in a video, {page_settle.REMEDY}."
    ) in answer.content
