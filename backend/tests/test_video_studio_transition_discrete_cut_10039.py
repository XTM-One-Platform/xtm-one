"""A transition paced as a discrete change draws no frame that mixes two keyframes (#10039).

A 60 fps take reported a key press that toggled a graph selection and a details panel as "held, not blended", yet
its paced frames were 48 to 57 % mixes of the states before and after: only the place read as discrete (a label
line) was held while the panel and the selection rings kept the blend, and the scene preview's ``double_image`` check
flagged the frames. Now one discrete place makes the whole window steps, every frame one of its keyframes as
painted; a keyframe the screencast painted mid-fade is dropped and named; a plain fade keeps its blend.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from PIL import Image

from app.services.video_studio import double_frames, scroll_pacing, transition_pacing

VIEW = (1280, 720)


def _page(selected: bool, panel: bool) -> np.ndarray:
    """A graph page: nodes drawn as boxes with labels, a selection ring and its count in a toolbar when *selected*,
    a details panel of flat fill with lines of text over the right side when *panel*."""
    page = np.full((VIEW[1], VIEW[0], 3), 236, dtype=np.uint8)
    rng = np.random.default_rng(3)
    for row in range(4):
        for col in range(5):
            y, x = 80 + 120 * row, 60 + 160 * col
            page[y : y + 40, x : x + 90] = (90, 110, 150)
            for gx in range(x + 8, x + 80, 9):
                page[y + 46 : y + 46 + int(rng.integers(6, 12)), gx : gx + 3] = (30, 30, 40)
    page[650:700, :] = (248, 248, 250)
    label = 2 if selected else 1
    glyphs = np.random.default_rng(label)
    x = 80 + 9 * label
    while x < 600:
        top = int(glyphs.integers(656, 664))
        page[top : top + int(glyphs.integers(14, 26)), x : x + int(glyphs.integers(2, 5))] = (30, 30, 40)
        x += int(glyphs.integers(9, 16))
    if selected:
        for y, x in ((80, 60), (200, 220), (320, 380)):
            page[y - 6 : y - 3, x - 6 : x + 96] = (255, 140, 0)
            page[y + 43 : y + 46, x - 6 : x + 96] = (255, 140, 0)
            page[y - 6 : y + 46, x - 6 : x - 3] = (255, 140, 0)
            page[y - 6 : y + 46, x + 93 : x + 96] = (255, 140, 0)
    if panel:
        page[60:600, 900:1260] = (40, 52, 80)
        for y in range(90, 560, 30):
            page[y : y + 10, 930 : 930 + int(rng.integers(120, 300))] = (220, 224, 232)
    return page


class _Kept:
    """A frame writer that keeps every picture handed to it, as handed (no JPEG round trip)."""

    def __init__(self) -> None:
        self.pictures: list[tuple[str, Any]] = []

    def write(self, picture: Any, name: str) -> None:
        self.pictures.append((name, np.array(picture, copy=True)))

    def flush(self) -> None:
        return None


def _keyframes(tmp_path, pictures: list[np.ndarray], times: list[float]) -> scroll_pacing.Keyframes:
    frames = []
    for k, (picture, t) in enumerate(zip(pictures, times, strict=True)):
        name = f"f{k:06d}.png"
        Image.fromarray(picture).save(tmp_path / name)
        frames.append((t, name))
    return scroll_pacing.Keyframes(str(tmp_path), frames)


def test_every_frame_of_a_discrete_window_is_byte_equal_to_one_of_its_keyframes(tmp_path):
    before, after = _page(False, False), _page(True, True)
    times = [0.9, 1.5, 2.4]
    reader = _keyframes(tmp_path, [before, after, after], times)
    writer = _Kept()
    events = [{"step": 4, "action": "press", "ok": True, "motion": [[1.0, 1.3, "div.panel"]]}]
    drawn, _used = transition_pacing.pace_transitions(reader, times, events, fps=60, writer=writer)
    [window] = events[0]["motion"]
    assert window["paced"] and window["held_not_blended"]
    assert drawn and len(writer.pictures) == len(drawn)
    for _name, picture in writer.pictures:
        assert np.array_equal(picture, before) or np.array_equal(picture, after)
        read = double_frames.read_blend(before.mean(axis=2), picture.mean(axis=2), after.mean(axis=2))
        assert read is None or not read.double
    assert window["held"] == len(drawn) - 1
    assert np.array_equal(writer.pictures[-1][1], after)


def test_a_keyframe_painted_mid_fade_is_dropped_and_named(tmp_path):
    before, after = _page(False, False), _page(True, True)
    mid = ((before.astype(np.uint16) + after.astype(np.uint16)) // 2).astype(np.uint8)
    times = [0.9, 1.15, 1.5, 2.4]
    reader = _keyframes(tmp_path, [before, mid, after, after], times)
    writer = _Kept()
    events = [{"step": 5, "action": "press", "ok": True, "motion": [[1.0, 1.3, "div.panel"]]}]
    transition_pacing.pace_transitions(reader, times, events, fps=60, writer=writer)
    [window] = events[0]["motion"]
    assert window["held_not_blended"] and window["painted_blends"] == [1.15]
    for _name, picture in writer.pictures:
        assert np.array_equal(picture, before) or np.array_equal(picture, after)
    words = scroll_pacing.scroll_steps_words(events)
    assert "1 keyframe(s) painted mid-fade dropped (at 1.15 s)" in words


def test_a_plain_fade_keeps_its_blend_and_its_painted_keyframes(tmp_path):
    before = _page(False, False)
    after = before.copy()
    after[200:260, 300:600] = 255
    mid = ((before.astype(np.uint16) + after.astype(np.uint16)) // 2).astype(np.uint8)
    times = [0.9, 1.15, 1.5, 2.4]
    reader = _keyframes(tmp_path, [before, mid, after, after], times)
    writer = _Kept()
    events = [{"step": 6, "action": "hover", "ok": True, "motion": [[1.0, 1.3, "div.tip"]]}]
    transition_pacing.pace_transitions(reader, times, events, fps=60, writer=writer)
    [window] = events[0]["motion"]
    assert window["paced"] and not window.get("held_not_blended") and "painted_blends" not in window
    assert any(
        not np.array_equal(picture, before) and not np.array_equal(picture, after) for _name, picture in writer.pictures
    )
