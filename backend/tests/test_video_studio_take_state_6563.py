"""A take starts from a clean page state, a step that misses its element names the page state it missed it in, and a
refused 60 fps take names a size that sustains it.

What is proven here without a browser (the real Chromium takes: ``test_video_studio_take_state_6563_chromium.py``):

* ``reset_state`` is on by default, takes its parts, and goes with a ``goto`` step only; ``fps_fallback`` refuses by
  default;
* a profile declares the storage keys its session lives in (``keep_storage_keys``), a list or a comma-separated text;
* a step is matched with an earlier take's by what it names, and its miss says the page's URL, title and candidates,
  and where an earlier take found it;
* the first line of a take's action log says what it opened on, and the contact sheet keeps the frame where a step
  failed;
* on a worker throttled to 50 fps at 2400x1350, the refusal of a 60 fps take names a size within 10 % of the largest
  that measured 60 fps - from this worker shape's table, measured where it does not say - and fps 30 at the requested
  size; ``fps_fallback`` records on either.
"""

from __future__ import annotations

import math
import re
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from PIL import Image

from app.api.v1.schemas.browser_profiles import BrowserProfileWrite
from app.models.video_studio import JOB_FAILED, JOB_READY, VideoTake
from app.services.browser_profiles import profiles
from app.services.builtin_tools import video_studio as tool
from app.services.video_studio import capture, capture_rate, spec, take_state
from app.tools.base import ToolCall

GRAPH = "https://app.example.test/reports/7/graph"
RADIAL = f"{GRAPH}?layoutMode=radial&layoutCentreId=19423125"


def _take_script(**fields) -> spec.TakeScript:
    return spec.parse_take_script(spec.validate_take_script({"steps": [{"action": "wait", "seconds": 1}], **fields}))


# ── The script ────────────────────────────────────────────────────────────


def test_a_take_resets_its_page_state_by_default_and_a_goto_may_reset_its_page():
    script = _take_script()
    assert script.reset_state is True and spec.page_reset(script.reset_state) == spec.PageReset()
    assert spec.PageReset() == spec.PageReset(url_query=True, local_storage=True, session_storage=True, indexed_db=False)
    assert script.fps_fallback == "refuse"

    parts = spec.page_reset(_take_script(reset_state={"local_storage": False}).reset_state)
    assert parts is not None and parts.local_storage is False and parts.session_storage is True
    assert spec.page_reset(False) is None and spec.page_reset(None) is None
    assert spec.page_reset(spec.PageReset(url_query=False, local_storage=False, session_storage=False)) is None

    goto = spec.validate_take_script(
        {"steps": [{"action": "goto", "url": GRAPH, "reset_state": {"indexed_db": True}}], "reset_state": False}
    )
    assert goto["reset_state"] is False and goto["steps"][0]["reset_state"]["indexed_db"] is True
    with pytest.raises(spec.VideoSpecError, match="reset_state goes with a goto"):
        spec.validate_take_script({"steps": [{"action": "click", "selector": "#a", "reset_state": True}]})
    with pytest.raises(spec.VideoSpecError, match="fps_fallback"):
        spec.validate_take_script({"steps": [{"action": "wait", "seconds": 1}], "fps_fallback": "lower"})


# ── The profile's session keys ────────────────────────────────────────────


def test_a_profile_declares_the_storage_keys_its_session_lives_in():
    cleaned = profiles.clean_login_config({"keep_storage_keys": "auth, msal.token\nauth", "signed_in_selector": "#app"})
    assert cleaned == {"keep_storage_keys": ["auth", "msal.token"], "signed_in_selector": "#app"}
    assert profiles.clean_login_config({"keep_storage_keys": ["auth", " "]}) == {"keep_storage_keys": ["auth"]}
    assert profiles.clean_login_config({"keep_storage_keys": []}) is None
    with pytest.raises(profiles.ProfileError, match="list of storage keys"):
        profiles.clean_login_config({"keep_storage_keys": {"auth": "1"}})
    with pytest.raises(profiles.ProfileError, match="at most 30 keys"):
        profiles.clean_login_config({"keep_storage_keys": [f"k{i}" for i in range(31)]})
    # The REST body takes the list beside the selectors.
    body = BrowserProfileWrite(name="Demo", start_url=GRAPH, login_config={"keep_storage_keys": ["auth"], "signed_in_selector": "#app"})
    assert profiles.clean_login_config(body.login_config) == {"keep_storage_keys": ["auth"], "signed_in_selector": "#app"}

    snap = profiles.ProfileSnapshot(id="p", name="Demo", start_url=GRAPH, login_url=None, login_config=cleaned)
    assert snap.keep_storage_keys == ("auth", "msal.token")
    assert profiles.ProfileSnapshot(id="p", name="Demo", start_url=GRAPH, login_url=None).keep_storage_keys == ()


# ── The same step on an earlier take ──────────────────────────────────────


def _step(**fields) -> spec.TakeStep:
    return spec.TakeStep.model_validate(fields)


def test_a_step_is_matched_with_an_earlier_take_s_by_what_it_names():
    assert take_state.step_key(_step(action="click", role="button", name="Layered layout")) == "name:layered layout"
    # A role's family matches the same name: the role is not part of what the step names.
    assert take_state.step_key(_step(action="click", role="menuitem", name="Layered Layout")) == "name:layered layout"
    assert take_state.step_key(_step(action="click", selector="#layered")) == "selector:#layered"
    assert take_state.step_key(_step(action="hover", text="Layered")) == "text:layered"
    assert take_state.step_key(_step(action="type", text="typed, not looked for")) is None
    assert take_state.step_key(_step(action="click", node={"label": "A"})) is None
    assert take_state.step_key(_step(action="wait", seconds=1)) is None

    steps = [
        {"action": "click", "role": "button", "name": "Layered layout"},
        {"action": "click", "role": "button", "name": "Lay out the graph around it"},
    ]
    newest = SimpleNamespace(
        id="t-new",
        name="Radial take",
        script={"steps": steps},
        events=[
            {"step": 0, "ok": True, "url_before": GRAPH, "url": GRAPH},
            {"step": 1, "ok": True, "url_before": GRAPH, "url": RADIAL},
        ],
    )
    older = SimpleNamespace(
        id="t-old",
        name="Older take",
        script={"steps": [*steps, {"action": "click", "selector": "#legend"}]},
        events=[{"step": 0, "ok": True, "url": "https://other/"}, {"step": 2, "ok": False, "url": GRAPH}],
    )
    unreadable = SimpleNamespace(id="t-bad", name="", script={"steps": "not a list"}, events=[{"step": 0}])
    found = take_state.earlier_finds([unreadable, newest, older])
    # The newest take that found it, at the URL its page was at when the step started; a skipped step found nothing.
    assert found["name:layered layout"] == take_state.EarlierFind("t-new", "Radial take", GRAPH)
    assert found["name:lay out the graph around it"].url == GRAPH
    assert "selector:#legend" not in found


def test_a_missed_step_says_the_page_state_it_missed_its_element_in():
    state = take_state.PageState(RADIAL, "Akira - Knowledge", "role button", 4)
    assert state.words() == f'The page was at {RADIAL} ("Akira - Knowledge"), showing 4 elements of role button'
    earlier = take_state.EarlierFind("t-1", "Radial take", GRAPH)
    assert take_state.missed_words(state, earlier) == (
        f'{state.words()}. It was found on take t-1 ("Radial take") at {GRAPH}; this take is at {RADIAL}: the page is '
        "in another state"
    )
    same = take_state.missed_words(take_state.PageState(GRAPH, "", "role button", 1), earlier)
    assert same.endswith(
        "found on take t-1 (\"Radial take\") at this same URL: the page is in another state (its storage, or the "
        "application's own data)"
    )
    assert take_state.PageState(GRAPH, "", "selector", 2, 0).words().endswith("where 2 elements match its selector (none of them shown)")
    assert take_state.PageState(GRAPH, "", "text", 0, 0).words().endswith("where nothing matches its text")
    assert state.as_dict() == {"url": RADIAL, "title": "Akira - Knowledge", "candidates": {"of": "role button", "count": 4}}
    assert take_state.missed_words(None, earlier) == ""


# ── What a take opened on ─────────────────────────────────────────────────


def test_the_action_log_s_first_line_says_what_the_take_opened_on():
    cleared = {
        "origin": "https://app.example.test",
        "local_storage": ["layoutMode", "graph.view.7", "a", "b", "c", "d", "e"],
        "session_storage": ["graphState"],
        "indexed_db": [],
        "kept": ["auth"],
    }
    opened = {"asked": GRAPH, "carried": True, "url": GRAPH, "reset": spec.PageReset().model_dump(), "cleared": cleared}
    assert take_state.opened_words(opened) == (
        f"opened on {GRAPH} - page state reset before step 0: cleared 7 localStorage keys (layoutMode, graph.view.7, "
        "a, b, c, d +1) and 1 sessionStorage key (graphState); kept auth (the profile's keep_storage_keys); cookies "
        "untouched; start_url opened as given"
    )
    assert take_state.added_query(GRAPH, RADIAL) == [["layoutMode", "radial"], ["layoutCentreId", "19423125"]]
    kept_query = take_state.opened_words({**opened, "url": RADIAL, "added_query": take_state.added_query(GRAPH, RADIAL)})
    assert kept_query.endswith(
        "; the page wrote layoutMode=radial, layoutCentreId=19423125 into its address itself (a state its storage reset "
        "does not reach)"
    )
    carried = take_state.opened_words({"asked": GRAPH, "carried": True, "url": RADIAL})
    assert "reset_state false: in the page state the profile's saved session carried from the takes before" in carried
    assert "a new browser" in take_state.opened_words({"asked": GRAPH, "carried": False, "url": GRAPH})
    fallback = "recorded at fps 30 at its own size (50 fps measured there) (fps_fallback fps: under 60 fps)"
    assert take_state.opened_words(opened, fallback).endswith(f"; {fallback}")
    assert take_state.opened_words(None) == "" and take_state.opened_words({"asked": GRAPH}) == ""


def _executor(monkeypatch, take: VideoTake):
    class _Session:
        async def get(self, model, key):
            if model is VideoTake:
                return take if str(key) == str(take.id) else None
            return SimpleNamespace(id=take.user_id, role="member")

    @asynccontextmanager
    async def session():
        yield _Session()

    monkeypatch.setattr(tool, "async_session", session)
    return tool.build_video_studio_tools(None, str(take.user_id))[1]


async def test_the_take_s_answer_opens_its_action_log_on_the_page_state_it_opened_in(monkeypatch):
    cleared = {"local_storage": ["layoutMode"], "session_storage": [], "indexed_db": [], "kept": ["auth"]}
    take = VideoTake(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="Layered layout",
        status=JOB_READY,
        progress=100,
        duration_ms=4000,
        viewport_width=1600,
        viewport_height=900,
        pixel_ratio=1.0,
        fps=60,
        captured_frames=120,
        events=[{"step": 0, "action": "click", "label": "Layered layout", "t0": 0.5, "ta": 1.0, "t1": 1.1, "url": GRAPH}],
        script={"steps": [{"action": "click", "role": "button", "name": "Layered layout"}], "pixel_ratio": 1},
        capture={"opened": {"asked": GRAPH, "carried": True, "url": GRAPH, "reset": {"url_query": True}, "cleared": cleared}},
    )
    answer = await _executor(monkeypatch, take).execute_tool(
        ToolCall(id="c1", name="get_video_recording", arguments={"recording_id": str(take.id), "include_contact_sheet": False})
    )
    log = answer.content.split("Action log (use at_step in overlays and highlights):\n", 1)[1].splitlines()
    # A take recorded before its build was kept says so last (#11407).
    assert log[0] == (
        f"  opened on {GRAPH} - page state reset before step 0: cleared 1 localStorage key (layoutMode); kept auth (the "
        "profile's keep_storage_keys); cookies untouched; start_url opened as given; build not recorded"
    )
    assert log[1].startswith("  step 0 click at 1.0 s - Layered layout")

    # A take a step failed says the page state it opened in too.
    take.status, take.error = JOB_FAILED, "Step 0 (click Layered layout) failed: Timeout 15000ms exceeded."
    failed = await _executor(monkeypatch, take).execute_tool(
        ToolCall(id="c2", name="get_video_recording", arguments={"recording_id": str(take.id), "include_contact_sheet": False})
    )
    assert not failed.is_error and f"\nIt opened on {GRAPH} - page state reset before step 0" in failed.content


# ── The contact sheet keeps the frame where a step failed ─────────────────


def test_the_contact_sheet_keeps_the_frame_where_a_step_failed():
    frames = [(t / 10, f"f{t:03d}.jpg") for t in range(80)]
    events = [
        {"step": 0, "action": "click", "label": "Lay out the graph around it", "t0": 0.5, "ta": 1.0, "t1": 1.2},
        {"step": 1, "action": "click", "label": "Layered layout", "t0": 1.5, "ta": 2.0, "t1": 2.0, "ok": False},
        {"step": 2, "action": "click", "label": "Legend", "t0": 2.5, "ta": 3.0, "t1": 3.2},
    ]
    picks = capture.sheet_frames(frames, events, 8.0)
    assert [label for _t, _name, label in picks] == [
        "step 0 click: Lay out the graph around it",
        "step 1 skipped: Layered layout",
        "step 2 click: Legend",
    ]
    # The frame painted at the instant the skipped step gave up.
    assert picks[1][:2] == (2.0, "f020.jpg")

    failed = capture.StepFailedError("Step 3 (click Layered layout) failed", step=3, at=6.04, label="Layered layout")
    failed.frames, failed.events = frames[:70], events
    *before, last = capture.failure_sheet_frames(failed)
    assert last == (6.04, "f060.jpg", "step 3 failed: Layered layout")
    assert before[0][2] == "step 0 click: Lay out the graph around it"
    assert capture.failure_sheet_frames(capture.StepFailedError("x", step=0, at=0.0, label="")) == []


def test_a_failed_take_s_contact_sheet_is_drawn_from_its_frames(tmp_path):
    for i in range(8):
        Image.new("RGB", (64, 36), (i * 30, 40, 90)).save(tmp_path / f"f{i}.jpg")
    failed = capture.StepFailedError("Step 1 failed", step=1, at=0.7, label="Layered layout")
    failed.frames = [(i / 10, f"f{i}.jpg") for i in range(8)]
    failed.events = [{"step": 0, "action": "click", "label": "Graph", "t0": 0.1, "ta": 0.2, "t1": 0.3}]
    sheet = tmp_path / "sheet.png"
    capture.contact_sheet(str(tmp_path), capture.failure_sheet_frames(failed), out_path=str(sheet))
    with Image.open(sheet) as drawn:
        assert drawn.width > 640


# ── A 60 fps take on a worker that captures 50 fps at 2400x1350 ───────────

#: The worker throttled to 50 fps at 2400x1350: a frame's cost goes with its pixels, at most 120 fps.
THROTTLED_PIXELS = 2400 * 1350
THROTTLED_FPS = 50.0


def _throttled(calls: list | None = None):
    async def measure(window: tuple[int, int], ratio: float) -> float:
        if calls is not None:
            calls.append((window, ratio))
        pixels = round(window[0] * ratio) * round(window[1] * ratio)
        return min(120.0, THROTTLED_FPS * THROTTLED_PIXELS / pixels)

    return measure


def _sixty(**fields) -> spec.TakeScript:
    return _take_script(viewport={"width": 1600, "height": 900}, pixel_ratio=1.5, fps=60, **fields)


@pytest.fixture(autouse=True)
def fresh_table(monkeypatch):
    monkeypatch.setattr(capture_rate, "_TABLE", {})


def _named_width(refusal: str) -> int:
    match = re.search(r"is a (\d+)x(\d+) window at pixel ratio 1\.5 \((\d+)x(\d+) painted, (\d+) fps measured\)", refusal)
    assert match, refusal
    return int(match[1])


async def test_a_refused_60_fps_take_names_the_largest_size_measured_sustaining_it_and_fps_30():
    calls: list = []
    planned = await capture_rate.choose(_sixty(), (1600, 900), 1.5, _throttled(calls), shape=(2, 4096))

    assert planned.refused is not None and planned.window is None
    assert planned.refused.startswith("Refused before recording: this worker captures 2400x1350 frames at 50 fps, under the take's 60 fps")
    # The largest window at pixel ratio 1.5 that measures 60 fps: 1460.6 CSS px wide.
    largest = 2400 * math.sqrt(THROTTLED_FPS / 60) / 1.5
    named = _named_width(planned.refused)
    assert 0.9 * largest <= named <= largest
    assert _throttled_fps(named) >= 60
    assert "on this worker (2 CPUs, 4.0 GiB)" in planned.refused
    assert "; or fps 30 at the requested size (50 fps measured there)" in planned.refused
    assert "fps_fallback 'size' or 'fps' records on it without asking" in planned.refused
    assert planned.measured["probes"], "the warm-ups the search measured are kept with the take"

    # Measured once, the table says it: the next refusal on this worker shape measures only its own size.
    again: list = []
    repeated = await capture_rate.choose(_sixty(), (1600, 900), 1.5, _throttled(again), shape=(2, 4096))
    assert _named_width(repeated.refused) == named and again == [((1600, 900), 1.5)]
    assert len(calls) > 1


def _throttled_fps(width: int) -> float:
    """What the throttled worker captures of a window *width* wide at pixel ratio 1.5."""
    height = round(width * 900 / 1600)
    return THROTTLED_FPS * THROTTLED_PIXELS / (round(width * 1.5) * round(height * 1.5))


async def test_the_table_of_a_worker_shape_forgets_what_it_measured_after_its_time(monkeypatch):
    await capture_rate.choose(_sixty(), (1600, 900), 1.5, _throttled(), shape=(2, 0))
    # Another worker shape reads none of it.
    other: list = []
    await capture_rate.choose(_sixty(), (1600, 900), 1.5, _throttled(other), shape=(8, 0))
    assert len(other) > 1
    monkeypatch.setattr(capture_rate, "TABLE_SECONDS", -1.0)
    calls: list = []
    await capture_rate.choose(_sixty(), (1600, 900), 1.5, _throttled(calls), shape=(2, 0))
    assert len(calls) > 1


async def test_fps_fallback_records_at_the_size_or_the_rate_it_names():
    sized = await capture_rate.choose(_sixty(fps_fallback="size"), (1600, 900), 1.5, _throttled(), shape=(2, 0))
    assert sized.refused is None and sized.window is not None and sized.pixel_ratio == 1.5
    assert 0.9 * 1460.6 <= sized.window[0] <= 1460.6 and sized.window[1] == round(sized.window[0] * 900 / 1600)
    assert sized.ratio == pytest.approx(1.5) and sized.fallback.startswith(f"recorded at a {sized.window[0]}x{sized.window[1]} window")
    assert "(fps_fallback size: 2400x1350 captured at 50 fps)" in sized.fallback
    assert sized.record()["fallback"] == sized.fallback

    slower = await capture_rate.choose(_sixty(fps_fallback="fps"), (1600, 900), 1.5, _throttled(), shape=(2, 0))
    assert slower.refused is None and slower.fps == 30 and slower.window is None and slower.pixel_ratio == 1.5
    assert slower.fallback == "recorded at fps 30 at its own size (50 fps measured there) (fps_fallback fps: under 60 fps)"


async def test_a_size_named_is_one_that_works_or_none_is():
    async def slow(_window, _ratio) -> float:
        return 20.0

    planned = await capture_rate.choose(_sixty(fps_fallback="size"), (1600, 900), 1.5, slow, shape=(1, 0))
    assert planned.refused is not None and planned.window is None
    assert "no window a recording takes sustains 60 fps on this worker (1 CPU), even at pixel ratio 1" in planned.refused
    assert "fps 30 at the requested size would not hold either (20 fps measured there)" in planned.refused
    assert "fps_fallback" not in planned.refused

    # A worker four times slower: no window at pixel ratio 1.5 holds 60 fps above the narrowest a recording takes
    # (854x480), one at pixel ratio 1 does - never a window under 480 px high.
    async def slower(window: tuple[int, int], ratio: float) -> float:
        return min(120.0, 15.0 * THROTTLED_PIXELS / (round(window[0] * ratio) * round(window[1] * ratio)))

    lowered = await capture_rate.choose(_sixty(), (1600, 900), 1.5, slower, shape=(3, 0))
    match = re.search(r"is a (\d+)x(\d+) window at pixel ratio 1 \(", lowered.refused)
    assert match, lowered.refused
    width, height = int(match[1]), int(match[2])
    assert 0.9 * 1200 <= width <= 1200 and height >= 480
    assert capture_rate.narrowest((1600, 900)) == 854

    # A search whose warm-ups cannot run still refuses a take measured too slow, never records it as asked.
    async def own_size_only(window, ratio) -> float:
        if window != (1600, 900):
            raise RuntimeError("the warm-up browser closed")
        return 50.0

    unmeasured = await capture_rate.choose(_sixty(fps_fallback="size"), (1600, 900), 1.5, own_size_only, shape=(5, 0))
    assert unmeasured.refused is not None and unmeasured.window is None
    assert "the smaller sizes could not be measured on this worker (5 CPUs): the warm-up browser closed" in unmeasured.refused
