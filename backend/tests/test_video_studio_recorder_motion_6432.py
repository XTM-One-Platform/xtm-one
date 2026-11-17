"""A take's inner scroll panels, slow-painting pages and transitions play at its frame rate.

What the scroll pacing left at the browser's own paint rate, on synthetic frames made the way the screencast makes them:

* a scroll container watched from the start was refused as "found once it had begun to scroll" whenever the page's
  clock ran a round trip ahead of the recorder's - a slow page's first read always does: an inner panel's scroll
  stayed at 2 to 3 fps. It is judged on the log's own clock now, and named as the page names it;
* a panel under the wheel scrolled to its end, then the page: "two scrollers moved at once", the step left as
  painted (3.2 to 11.6 fps on a heavy dashboard). Each is paced over the span it moved in;
* a page painting every 300 ms, scrolled 600 px over 2.5 s at 1600x900: 30 fps, no two frames alike, the take saying
  how many a second were synthesised and the quality check silent;
* a scroll that outran the page's paints holds frames, plays at the share that moved, and the take's notes say the
  seconds its step takes for every two paints to overlap - the lint naming the step;
* a hover's tooltip fading in between two keyframes is drawn again at 30 fps, each frame the blend at its instant;
* each step says the rates it was painted at and plays at and the element it scrolled, and the take's answer says
  them; a locator miss says what the page was filtered by.
"""

from __future__ import annotations

import itertools

import numpy as np
import pytest
from PIL import Image

from app.services.video_studio import capture, quality, scroll_pacing, timeline, transition_pacing
from app.services.video_studio.spec import parse_video_spec, validate_video_spec

VIEW = (1600, 900)
HEADER = 60


def _page(height: int, width: int = VIEW[0], seed: int = 3) -> np.ndarray:
    """A page *height* CSS px tall at pixel ratio 1: rows of marks, every band of rows unlike its neighbours."""
    y = np.arange(height)[:, None]
    x = np.arange(width)[None, :]
    marks = (((y // 6) * 37 + (x // 48) * 91 + (y // 90) * (13 + seed)) % 180).astype(np.uint8) + 40
    page = np.repeat(marks[:, :, None], 3, axis=2)
    page[:, :, 2] = (page[:, :, 2].astype(np.int32) * 3 // 4 + 30).astype(np.uint8)
    return page


def _jpeg(frame: np.ndarray, path) -> None:
    Image.fromarray(frame).save(path, quality=95)


def _decoded(tmp_path, name: str) -> np.ndarray:
    with Image.open(tmp_path / name) as opened:
        return np.asarray(opened.convert("RGB")).astype(np.int16)


def _samples(offset, start: float, end: float, *, panel=None) -> list[list]:
    """The scroll log at 60 Hz from *start* to *end*: the window at ``offset(t)`` and, given, a panel ``(id, box,
    offset)`` - as ``capture._Recorder.scroll_log`` reads them."""
    out, t = [], start
    while t <= end + 1e-9:
        entries = [[panel[0], panel[2](t), 0, list(panel[1])]] if panel else []
        out.append([round(t, 4), offset(t), 0, entries])
        t += 1 / scroll_pacing.SCROLL_SAMPLE_HZ
    return out


# ── The scroller read on the log's own clock ──────────────────────────────


def test_a_panel_watched_from_the_first_sample_is_paceable_whatever_the_page_s_clock_says():
    # The log's first sample came 0.2 s after the recorder's own clock opened the span: a slow page's first read.
    panel = (4, (240, 80, 1100, 760), lambda t: max(0.0, min(t - 1.0, 1.0)) * 900)
    samples = _samples(lambda _t: 0.0, 0.9, 2.3, panel=panel)
    [scroll] = scroll_pacing.read_scrolls({"t": [0.7, 2.3], "samples": samples, "names": {"4": "main#panel.content"}})
    assert scroll.scroller == "element" and scroll.unpaceable is None and scroll.element == "main#panel.content"
    # One first seen once it scrolled (a scroll event found it) is not.
    late = [[s[0], s[1], s[2], s[3] if s[0] >= 1.3 else []] for s in samples]
    [scroll] = scroll_pacing.read_scrolls({"t": [0.7, 2.3], "samples": late})
    assert scroll.unpaceable == "its scroll container was found once it had begun to scroll"


def test_a_panel_scrolled_to_its_end_then_the_page_is_two_scrolls_each_paceable():
    panel = (2, (240, 80, 1100, 760), lambda t: max(0.0, min(t - 1.0, 0.5)) * 600)
    window = lambda t: max(0.0, min(t - 1.8, 1.0)) * 900  # noqa: E731
    scrolls = scroll_pacing.read_scrolls({"t": [0.9, 3.0], "samples": _samples(window, 0.9, 3.0, panel=panel), "names": {"2": "div#list"}})
    assert [(s.scroller, s.element, s.unpaceable) for s in scrolls] == [("element", "div#list", None), ("window", None, None)]
    assert scrolls[0].span[1] == scrolls[1].span[0] and 1.5 < scrolls[0].span[1] < 1.8
    # Moving together, they are one scroll, not paced.
    together = lambda t: max(0.0, min(t - 1.0, 1.0)) * 900  # noqa: E731
    [scroll] = scroll_pacing.read_scrolls({"t": [0.9, 3.0], "samples": _samples(together, 0.9, 3.0, panel=panel)})
    assert scroll.unpaceable == "two scrollers moved at once"


# ── A page that paints slowly ─────────────────────────────────────────────


def _slow_take(tmp_path, *, distance: float, seconds: float, paint_every: float, height: int = 3000):
    """The keyframes a page painting every *paint_every* s hands over while the window scrolls *distance* CSS px over
    *seconds* from 1.0 s (its fixed header over it), the offsets they painted, and the step's event."""
    page = _page(height)

    def offset(t: float) -> float:
        return min(max(t - 1.0, 0.0), seconds) * distance / seconds

    def view(at: float) -> np.ndarray:
        top = round(offset(at))
        frame = page[top : top + VIEW[1]].copy()
        frame[:HEADER] = (24, 30, 48)
        return frame

    frames, painted, t, k = [], {}, 0.05, 0
    while t < 1.0 + seconds + 1.0:
        name = f"f{k:06d}.jpg"
        _jpeg(view(t), tmp_path / name)
        frames.append((round(t, 4), name))
        painted[name] = float(round(offset(t)))
        t += paint_every
        k += 1
    event = {
        "step": 2,
        "action": "scroll",
        "ok": True,
        "scrolls": [{"t": [0.9, 1.0 + seconds + 0.3], "samples": _samples(offset, 0.9, 1.0 + seconds + 0.3)}],
    }
    return frames, painted, [event], offset


def test_a_page_painting_every_300_ms_scrolled_600_px_over_2_5_s_plays_at_30_fps(tmp_path):
    frames, painted, events, _offset = _slow_take(tmp_path, distance=600, seconds=2.5, paint_every=0.3)
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=30, pixel_ratio=1, painted=painted)
    [scroll] = events[0]["scrolls"]
    assert scroll["paced"] and scroll["fps"] == 30.0 and scroll["painted"] < 3.5 and "held" not in scroll
    during = [(t, name) for t, name in paced.frames if 1.0 <= t < 3.5]
    assert len(during) == 75
    previous = None
    for _t, name in during:
        shown = _decoded(tmp_path, name)
        if previous is not None:
            assert np.abs(shown[HEADER:] - previous[HEADER:]).mean() > 1.0, "no two frames alike while it scrolls"
        previous = shown
    rate = scroll_pacing.take_rate(events, captured_frames=len(frames), duration=frames[-1][0])
    assert not rate.steps and rate.words() == "scrolls at 30 fps (27 frames a second synthesised between keyframes)"
    assert (events[0]["painted_fps"], events[0]["fps"], events[0]["scrolled_element"]) == (scroll["painted"], 30.0, "window")
    assert paced.notes == []


@pytest.fixture
def outran(tmp_path):
    # 4000 px in 1.5 s, a paint every 0.5 s: about 1330 px between two paints, the scroller showing 840.
    frames, painted, events, _offset = _slow_take(tmp_path, distance=4000, seconds=1.5, paint_every=0.5, height=6000)
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=30, pixel_ratio=1, painted=painted)
    return frames, events, paced


def test_a_scroll_that_outran_its_paints_plays_at_the_share_that_moved_and_names_its_seconds(outran):
    frames, events, paced = outran
    [scroll] = events[0]["scrolls"]
    assert scroll["paced"] and scroll["held"] > 0 and scroll["fps"] < 30.0
    assert scroll["outran"]["moved"] > scroll["outran"]["shows"] == VIEW[1] - HEADER
    # Slower by how far it outran: every two paints then share 40% of its rows.
    assert scroll["seconds"] >= 1.5 * scroll["outran"]["moved"] / (scroll_pacing.OVERLAPPING * scroll["outran"]["shows"]) - 0.5
    [note] = paced.notes
    assert note.startswith(f"Step 2's scroll moved {scroll['outran']['moved']} px between two paints 0.50 s apart")
    assert note.endswith(f"Record it again with seconds: {scroll['seconds']:g} on step 2 - slower, every two paints overlapping.")
    rate = scroll_pacing.take_rate(events, captured_frames=len(frames), duration=frames[-1][0])
    if rate.steps:
        assert rate.where() == "step 2 (scroll)"
        assert rate.remedy() == f"record it again with seconds: {scroll['seconds']:g} on step 2 (its scroll outran the page's paints)"


# ── A hover's transition ──────────────────────────────────────────────────


def _tooltip_take(tmp_path):
    """A hover at 1.0 s fading a tooltip in over 0.3 s: one keyframe before it (0.9 s), the next once it is in
    (1.5 s) - the screencast's 2 a second."""
    base = _page(VIEW[1], seed=5)
    shown = base.copy()
    shown[400:470, 700:1000] = (250, 250, 255)
    _jpeg(base, tmp_path / "f000000.jpg")
    _jpeg(shown, tmp_path / "f000001.jpg")
    _jpeg(shown, tmp_path / "f000002.jpg")
    frames = [(0.9, "f000000.jpg"), (1.5, "f000001.jpg"), (2.4, "f000002.jpg")]
    event = {"step": 1, "action": "hover", "ok": True, "motion": [[1.0, 1.3, "div#tip.tooltip[tooltip]"], [1.05, 1.2, "button#save"]]}
    return frames, [event], base, shown


def test_a_tooltip_fading_in_between_two_keyframes_is_drawn_again_at_30_fps(tmp_path):
    frames, events, base, shown = _tooltip_take(tmp_path)
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=30, pixel_ratio=1)
    [window] = events[0]["motion"]
    assert window["paced"] and window["fps"] == 30.0 and window["element"] == "div#tip.tooltip[tooltip]"
    during = [(t, name) for t, name in paced.frames if 1.0 <= t <= 1.3]
    assert len(during) == 10
    # Each frame the blend at its instant: the tooltip a little brighter every frame, whole at the window's end.
    levels = [float(_decoded(tmp_path, name)[400:470, 700:1000].mean()) for _t, name in during]
    assert all(b > a for a, b in itertools.pairwise(levels))
    assert levels[0] == pytest.approx(float(base[400:470, 700:1000].mean()), abs=3)
    assert levels[-1] == pytest.approx(float(shown[400:470, 700:1000].mean()), abs=3)
    assert (events[0]["painted_fps"], events[0]["fps"]) == (0.0, 30.0) and "scrolled_element" not in events[0]
    rate = scroll_pacing.take_rate(events, captured_frames=len(frames), duration=3.0)
    assert (rate.transitions, rate.steps) == (True, False)
    assert rate.words() == "transitions at 30 fps (30 frames a second synthesised between keyframes)"


def test_a_transition_nothing_painted_counts_in_no_rate(tmp_path):
    base = _page(VIEW[1], seed=5)
    for k in range(3):
        _jpeg(base, tmp_path / f"f{k:06d}.jpg")
    frames = [(0.9, "f000000.jpg"), (1.5, "f000001.jpg"), (2.4, "f000002.jpg")]
    events = [{"step": 1, "action": "click", "motion": [[1.0, 1.2, "span.ripple"]]}]
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=30, pixel_ratio=1)
    assert paced.frames == frames and events[0]["motion"][0]["why"] == scroll_pacing.NOTHING_CHANGED
    rate = scroll_pacing.take_rate(events, captured_frames=3, duration=3.0)
    assert (rate.transitions, rate.steps, rate.words()) == (False, False, "1 fps painted")


def test_the_windows_of_a_step_merge_where_they_meet():
    raw = [[1.0, 1.3, "div#tip"], [1.1, 1.2, "button"], [2.0, 2.2, ""], [3.0, 2.9, "x"]]
    assert transition_pacing.windows(raw) == [(1.0, 1.3, ["div#tip", "button"]), (2.0, 2.2, [])]


# ── The take's words ──────────────────────────────────────────────────────

TAKE = "38f88e75-0000-4000-8000-000000000001"


def test_a_hover_take_under_24_fps_is_a_save_finding_naming_its_step():
    authored = validate_video_spec({"title": "Brand film", "freestyle": True, "scenes": [{"id": "demo", "type": "recording", "take_id": TAKE}]})
    spec = parse_video_spec(authored)
    document = timeline.estimate_plan(spec, {}, {}).document
    unpaced = [{"step": 3, "action": "hover", "motion": [{"t": [1.0, 1.3], "painted": 3.3, "fps": 3.3, "paced": False, "why": "it could not be drawn"}]}]
    take = timeline.TakeMedia(TAKE, 14.8, 1600, 900, 2.0, 60, 888, events=unpaced, captured_frames=21)
    found = quality.content_findings(spec, document, quality.ContentFacts(takes={TAKE: take}), authored)
    [finding] = [f for f in found if f.kind == "take_frame_rate"]
    assert finding.message == (
        "take 38f88e75 carries 3.3 fps on step 3 (hover): transitions will step on scene demo; step 3 was not drawn again "
        "at its rate (it could not be drawn): record it again"
    )
    # Its clicks and its hovers with nothing moving step nowhere (the take of hovers, 1.0 to 1.4 fps painted).
    still = [{"step": 3, "action": "hover", "scrolled": [1.0, 1.2], "scrolls": [{"t": [1.0, 1.2], "fps": 1.2, "why": scroll_pacing.DID_NOT_MOVE}]}]
    take = timeline.TakeMedia(TAKE, 14.8, 1600, 900, 2.0, 60, 888, events=still, captured_frames=18)
    assert not [f for f in quality.content_findings(spec, document, quality.ContentFacts(takes={TAKE: take}), authored) if f.kind == "take_frame_rate"]


def test_the_take_s_answer_says_each_step_s_rates_and_what_it_scrolled():
    events = [
        {"step": 0, "action": "goto"},
        {"step": 1, "action": "scroll", "scrolls": [{"scroller": "element", "element": "main#panel", "painted": 2.1, "fps": 30.0, "paced": True}]},
        {"step": 2, "action": "hover", "motion": [{"element": "div#tip", "painted": 0.0, "fps": 30.0, "paced": True}]},
        {"step": 3, "action": "click", "motion": [{"element": "span.ripple", "painted": 0.0, "fps": 0.0, "why": scroll_pacing.NOTHING_CHANGED}]},
    ]
    assert scroll_pacing.scroll_steps_words(events) == (
        "Scroll steps: step 1 (scroll) scrolled main#panel: painted at 2.1 fps, drawn again from its keyframes at 30 fps. "
        "Transition steps: step 2 (hover) moved div#tip: painted at 0.0 fps, drawn again from its keyframes at 30 fps."
    )


def test_a_locator_miss_says_what_the_page_was_filtered_by():
    held = {"fields": [["Search", "malware"], ["", "APT"]], "query": [["searchTerm", "malware"], ["sortBy", "name"]]}
    assert capture.filters_words(held) == (
        'its field "Search" "malware", a field "APT"; its address searchTerm=malware, sortBy=name'
    )
    assert capture.filters_words({}) == "" and capture.filters_words({"fields": [["Search", ""]], "query": []}) == ""
