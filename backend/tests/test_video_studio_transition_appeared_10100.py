"""Content that appears in place of a spinner is held then cut at 60 fps, never blended (#10100).

Retest of #10042 on a 60 fps take: a side panel went from a small loading spinner to its text under a paced ``svg``
motion window; the pair failed ``discrete``'s balance (a spinner draws a few edges of its own, the text thousands), so
four frames showed the text rising over the spinner, and the scene preview's ``double_image`` read stayed green
because the spinner side had too few own edges to judge. Now such an appearance is held, and a forced blend of it on
a synthesised span is a preview finding; a box fading in and a block moving keep their blend.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from PIL import Image

from app.services.video_studio import double_frames, double_take, scroll_pacing, transition_pacing

VIEW = (1280, 720)


def _panel(spinner: bool, text: bool) -> np.ndarray:
    """A dark page with a side panel: a small spinner arc in its middle when *spinner*, lines of glyphs when *text*."""
    page = np.full((VIEW[1], VIEW[0], 3), (11, 15, 26), dtype=np.uint8)
    page[60:700, 900:1260] = (20, 30, 55)
    if spinner:
        yy, xx = np.mgrid[0 : VIEW[1], 0 : VIEW[0]]
        ring = np.hypot(yy - 300, xx - 1080)
        page[(ring > 14) & (ring < 18) & (xx > 1070)] = (120, 170, 230)
    if text:
        rng = np.random.default_rng(5)
        for y in range(90, 560, 22):
            x, stop = 930, 930 + int(rng.integers(150, 300))
            while x < stop:
                page[y : y + int(rng.integers(8, 12)), x : x + int(rng.integers(2, 4))] = (220, 224, 232)
                x += int(rng.integers(5, 9))
    return page


def _grey(picture: np.ndarray) -> np.ndarray:
    return picture.astype(np.float32).mean(axis=2)


class _Kept:
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


def test_text_in_place_of_a_spinner_appeared_and_a_fade_or_a_move_did_not():
    spinner, text = _grey(_panel(True, False)), _grey(_panel(False, True))
    assert not transition_pacing.discrete(spinner, text)
    assert transition_pacing.appeared(spinner, text)
    assert transition_pacing.appeared(text, spinner)
    assert transition_pacing.held_regions(spinner, text)
    # A plain box lit over the page: a fade.
    lit = spinner.copy()
    lit[200:260, 300:600] = 255.0
    assert not transition_pacing.appeared(spinner, lit)
    # A block of text moved sideways: both sides draw it, a move.
    moved, there = np.full((720, 1280), 30.0, np.float32), np.full((720, 1280), 30.0, np.float32)
    moved[100:320, 300:630] = text[80:300, 920:1250]
    there[100:320, 340:670] = text[80:300, 920:1250]
    assert not transition_pacing.appeared(moved, there)
    assert not transition_pacing.appeared(spinner, spinner)


def test_a_spinner_replaced_by_text_under_a_paced_svg_window_is_held_at_60_fps(tmp_path):
    before, after = _panel(True, False), _panel(False, True)
    times = [13.10, 13.30, 14.0]
    reader = _keyframes(tmp_path, [before, after, after], times)
    writer = _Kept()
    events = [
        {
            "step": 6,
            "action": "type",
            "ok": True,
            "motion": [[12.82, 13.04, "div.MuiPaper-root"], [13.147, 13.362, "svg"]],
        }
    ]
    transition_pacing.pace_transitions(reader, times, events, fps=60, writer=writer)
    svg = next(window for window in events[0]["motion"] if window.get("element") == "svg")
    assert svg["paced"] and svg["held_not_blended"]
    assert writer.pictures
    for _name, picture in writer.pictures:
        assert np.array_equal(picture, before) or np.array_equal(picture, after)
    assert "frame(s) held, not blended" in scroll_pacing.scroll_steps_words(events)


def test_a_forced_blend_of_a_spinner_and_its_text_is_a_preview_finding():
    small = (108, 192)

    def shrink(picture: np.ndarray) -> np.ndarray:
        return (
            np.asarray(Image.fromarray(picture).resize((small[1], small[0]), Image.BILINEAR))
            .astype(np.float32)
            .mean(axis=2)
        )

    spinner, text = shrink(_panel(True, False)), shrink(_panel(False, True))
    mix = 0.5 * spinner + 0.5 * text
    read = double_frames.read_blend(spinner, mix, text)
    assert read is not None and not read.double and read.one_sided
    run = [(n, n) for n in range(3)]
    hits = double_take.judge(run, {0: spinner, 1: mix, 2: text})
    assert [frame for frame, _take, _blend, _share in hits] == [1]
    # The render-wide watch is unchanged: it never judged a one-sided pair.
    plain = double_frames.DoubleWatch()
    for count, picture in enumerate((spinner, mix, text)):
        plain.feed(picture, count, {"type": "recording", "id": "s"})
    assert plain.hits == []
    # A frame equal to one neighbour is no blend.
    assert double_take.judge(run, {0: spinner, 1: spinner, 2: text}) == []
