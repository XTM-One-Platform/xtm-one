"""A step's transition whose keyframes differ by a discrete change is held, never blended (#9496).

A click that swapped a toolbar's label was drawn again at 30 fps as the mix of its two painted keyframes: one to
three frames showed both labels at once, doubled and offset, and every render of the take carried the double image.
Now a pair of keyframes that each draw structure of their own where they differ is held then cut; a tooltip fading in
over the page keeps its blend (``test_video_studio_recorder_motion_6432``).
"""

from __future__ import annotations

import numpy as np
from PIL import Image

from app.services.video_studio import double_frames, scroll_pacing, transition_pacing

VIEW = (1280, 720)
BAR = (640, 700)


def _toolbar(label: int) -> np.ndarray:
    """A plain page with a bottom toolbar carrying a label: strokes standing for its glyphs, unlike for each *label*."""
    page = np.full((VIEW[1], VIEW[0], 3), 236, dtype=np.uint8)
    page[BAR[0] : BAR[1], :] = (248, 248, 250)
    rng = np.random.default_rng(label)
    x = 80 + 9 * label
    while x < 900:
        width, top = int(rng.integers(2, 5)), int(rng.integers(650, 662))
        page[top : top + int(rng.integers(14, 28)), x : x + width] = (30, 30, 40)
        x += int(rng.integers(9, 16))
    return page


def _take(tmp_path):
    before, after = _toolbar(1), _toolbar(2)
    for k, picture in enumerate((before, after, after)):
        Image.fromarray(picture).save(tmp_path / f"f{k:06d}.jpg", quality=95)
    frames = [(0.9, "f000000.jpg"), (1.5, "f000001.jpg"), (2.4, "f000002.jpg")]
    events = [{"step": 16, "action": "click", "ok": True, "motion": [[1.0, 1.3, "div.MuiPaper-root"]]}]
    return frames, events, before, after


def _grey(path) -> np.ndarray:
    with Image.open(path) as opened:
        return np.asarray(opened.convert("RGB")).astype(np.float32).mean(axis=2)


def test_two_labels_are_a_discrete_change_and_a_fade_onto_a_plain_box_is_not():
    a, b = _toolbar(1).mean(axis=2), _toolbar(2).mean(axis=2)
    assert transition_pacing.discrete(a, b)
    lit = a.copy()
    lit[200:260, 300:600] = 255.0
    assert not transition_pacing.discrete(a, lit)
    assert not transition_pacing.discrete(a, a)


def test_a_tooltip_fading_in_over_rows_of_text_keeps_its_blend():
    # The real-Chromium hover take (test_video_studio_recorder_motion_chromium): a light box with a dark label fading in
    # over rows of light text draws edges of its own on both sides, but paints what changes in its own fill.
    page = np.full((900, 1600, 3), (11, 15, 26), dtype=np.uint8)
    rng = np.random.default_rng(7)
    for i in range(20):
        y = 134 + 40 * i
        page[y : y + 39, 220:] = (40 + (i * 37) % 120, 60 + (i * 53) % 100, 80 + (i * 29) % 90)
        x = 244
        while x < 1500:
            top = y + int(rng.integers(8, 14))
            page[top : top + int(rng.integers(10, 20)), x : x + int(rng.integers(2, 5))] = (244, 246, 250)
            x += int(rng.integers(8, 15))
    shown = page.copy()
    shown[144:194, 244:544] = (244, 246, 250)
    x = 264
    while x < 520:
        top = int(rng.integers(156, 162))
        shown[top : top + int(rng.integers(10, 18)), x : x + int(rng.integers(2, 4))] = (11, 15, 26)
        x += int(rng.integers(8, 13))
    hidden, half = page.astype(np.float32).mean(axis=2), shown.astype(np.float32).mean(axis=2)
    assert not transition_pacing.discrete(hidden, half)
    assert not transition_pacing.discrete(hidden, (hidden + half) / 2)


def _toolbar_and_panel(label: int, panel: bool) -> np.ndarray:
    """The toolbar of *label*, and a details panel of flat fill with a few lines open over the page's right side."""
    page = _toolbar(label)
    if panel:
        page[60:600, 900:1260] = (40, 52, 80)
        for y in (90, 130, 170):
            page[y : y + 10, 930:1100] = (220, 224, 232)
    return page


def test_a_counter_swapped_while_a_panel_opens_holds_the_counter_and_blends_the_panel():
    # Retest of #9499 on take 1af9a15e step 16: the click on a graph node swapped the toolbar's counter and opened the
    # details panel at once; read as one change, the panel's fill outweighed the counter and the pair blended.
    a = _toolbar_and_panel(1, False).astype(np.float32).mean(axis=2)
    b = _toolbar_and_panel(2, True).astype(np.float32).mean(axis=2)
    assert not transition_pacing.discrete(a, b)
    places = transition_pacing.held_regions(a, b)
    assert places
    for rows, cols in places:
        # The counter's line is held; the panel is not, so it keeps its blend.
        assert rows.start >= BAR[0] - 60 and cols.start < 900
    assert transition_pacing.held_regions(a, a) == []
    lit = a.copy()
    lit[200:260, 300:600] = 255.0
    assert transition_pacing.held_regions(a, lit) == []


def test_a_counter_swapped_while_a_panel_opens_never_mixes_the_counter(tmp_path):
    before, after = _toolbar_and_panel(1, False), _toolbar_and_panel(2, True)
    for k, picture in enumerate((before, after, after)):
        Image.fromarray(picture).save(tmp_path / f"f{k:06d}.jpg", quality=95)
    frames = [(0.9, "f000000.jpg"), (1.5, "f000001.jpg"), (2.4, "f000002.jpg")]
    events = [{"step": 16, "action": "click", "ok": True, "motion": [[1.0, 1.3, "div.MuiPaper-root"]]}]
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=30, pixel_ratio=1)
    [window] = events[0]["motion"]
    assert window["paced"] and window["held_not_blended"]
    during = [(t, name) for t, name in paced.frames if 1.0 <= t <= 1.3]
    one, other = before.astype(np.float32).mean(axis=2), after.astype(np.float32).mean(axis=2)
    for _t, name in during:
        frame = _grey(tmp_path / name)
        # The counter and the panel cut together (#10039): never the panel half drawn over the page.
        assert min(float(np.abs(frame - one).mean()), float(np.abs(frame - other).mean())) < 1.0
        read = double_frames.read_blend(one, frame, other)
        assert read is None or not read.double


def test_a_label_swapped_by_a_click_is_held_then_cut_never_blended(tmp_path):
    frames, events, before, after = _take(tmp_path)
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=30, pixel_ratio=1)
    [window] = events[0]["motion"]
    assert window["paced"] and window["fps"] == 30.0
    during = [(t, name) for t, name in paced.frames if 1.0 <= t <= 1.3]
    assert len(during) == 10
    assert window["held_not_blended"] and window["held"] == len(during) - 1
    one, other = before.astype(np.float32).mean(axis=2), after.astype(np.float32).mean(axis=2)
    for _t, name in during:
        frame = _grey(tmp_path / name)
        # Each frame one label or the other, as painted: never a mix of the two.
        assert min(float(np.abs(frame - one).mean()), float(np.abs(frame - other).mean())) < 1.0
        read = double_frames.read_blend(one, frame, other)
        assert read is None or not read.double
    # The last frame of the window is the later keyframe, cut in where the change ends.
    assert float(np.abs(_grey(tmp_path / during[-1][1]) - other).mean()) < 1.0
    words = scroll_pacing.scroll_steps_words(events)
    # A discrete change with nothing between its two pictures is a cut (#10897): said, counted in no rate.
    assert "step 16 (click) moved div.MuiPaper-root: painted at 0.0 fps, discrete, cut: one picture before" in words
    assert f"({len(during) - 1} frame(s) held, not blended)" in words
    assert window["discrete"] and scroll_pacing.synthesised_transitions(events, fps=30) == []
