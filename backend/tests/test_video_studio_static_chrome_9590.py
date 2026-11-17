"""Static page chrome drawn at two places in turn is a failing finding of the render (issue #9590).

A graph canvas laid out again under a toolbar and a "Legend" button: on about half the frames of the transition the
chrome was drawn shifted (a second copy of the button, a stray bar), every gate green. ``double_frames`` reads whole
frames as blends and never saw it. ``static_chrome`` reads every recording frame's tiles:

* chrome drawn shifted and back on alternate frames while the canvas moves is one ``static_flicker`` finding, a failure
  of the picture, naming its scene, its time window and the box it sits in;
* the canvas moving on under steady chrome, a page that scrolls, a panel that fades in and stays, a cut and a hold are
  never one;
* the finding refuses another render like any failing picture finding, and a resumed watch keeps what it found.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from app.services.video_studio import encoder, frame_freeze, render_checks, static_chrome

H, W = 108, 192
RECORDING = {"duration": 4.0, "scenes": [{"id": "graph", "type": "recording", "start": 0.0, "end": 4.0}]}
TITLE = {"duration": 4.0, "scenes": [{"id": "t", "type": "title", "start": 0.0, "end": 4.0}]}


def _texture(seed: int, shape: tuple[int, int]) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.uniform(40, 220, shape)


CANVAS = _texture(1, (H, W * 3))


def _chrome(frame: np.ndarray, shift: int = 0) -> np.ndarray:
    """A bottom toolbar and a "Legend" button over the canvas; *shift*: drawn that many px off, the capture fault."""
    frame[80 + shift : 100 + shift, 8 + shift : 64 + shift] = 245.0
    frame[84 + shift : 96 + shift, 14 + shift : 58 + shift] = 30.0  # the button's label
    frame[78 + shift : 80 + shift, 0:W] = 10.0  # the toolbar's top edge
    return frame


def _frame(i: int, *, shift: int = 0, moving: bool = True) -> np.ndarray:
    x = 4 * i if moving else 0
    frame = CANVAS[:, x : x + W].copy()
    return _chrome(frame, shift)


def _encoded(frames: list[np.ndarray], seed: int = 9590) -> list[np.ndarray]:
    rng = np.random.default_rng(seed)
    return [np.clip(f + rng.normal(0.0, 1.0, f.shape), 0, 255).astype(np.uint8) for f in frames]


def _ghosting(total: int = 90, window: tuple[int, int] = (30, 60)) -> list[np.ndarray]:
    """The canvas fans out over the whole take; inside *window* the chrome is drawn 5 px off on every other frame."""
    return _encoded([_frame(i, shift=5 if window[0] <= i < window[1] and i % 2 else 0) for i in range(total)])


def _watch(frames: list[np.ndarray], document: dict = RECORDING, **kwargs) -> frame_freeze.FrameCheck:
    async def stream(_path, *, width, height, fps, start=0.0):
        for frame in frames:
            yield frame.tobytes()

    original = encoder.iter_gray_frames
    encoder.iter_gray_frames = stream
    try:
        return asyncio.run(
            frame_freeze.check_frames("final.mp4", document=document, width=1920, height=1080, fps=30, **kwargs)
        )
    finally:
        encoder.iter_gray_frames = original


def _flickers(check: frame_freeze.FrameCheck) -> list[render_checks.Finding]:
    return [f for f in check.findings if f.kind == render_checks.STATIC_FLICKER]


def test_chrome_drawn_at_two_places_during_a_canvas_transition_is_one_failing_finding():
    check = _watch(_ghosting())
    found = _flickers(check)
    assert len(found) == 1, [f.message for f in check.findings]
    finding = found[0]
    assert finding.severity == render_checks.FAIL and finding.scene_id == "graph"
    assert 0.9 <= finding.at <= 1.1 and 1.9 <= finding.end <= 2.1
    x0, y0, x1, _y1 = finding.data["box"]
    # The box sits on the toolbar and the button, the bottom left of the frame.
    assert y0 >= 0.6 and x0 <= 0.1 and x1 <= 0.5, finding.data["box"]
    assert finding.data["frames"] >= 10
    assert check.summary["static_flickers"] == 1
    assert "static chrome" in finding.message


def test_the_same_transition_with_steady_chrome_passes():
    check = _watch(_encoded([_frame(i) for i in range(90)]))
    assert _flickers(check) == [] and "static_flickers" not in check.summary


def test_a_panel_that_fades_in_and_stays_is_never_a_flicker():
    frames = []
    for i in range(90):
        frame = _frame(i)
        alpha = min(1.0, max(0.0, (i - 30) / 10))
        frame[10:60, 120:180] = (1 - alpha) * frame[10:60, 120:180] + alpha * 250.0
        frames.append(frame)
    assert _flickers(_watch(_encoded(frames))) == []


def test_a_hold_and_a_cut_are_never_a_flicker():
    still = [_frame(0, moving=False) for _ in range(40)]
    other = [np.flipud(_frame(0, moving=False)) for _ in range(40)]
    assert _flickers(_watch(_encoded(still + other))) == []


def test_one_frame_shifted_once_is_not_a_run():
    frames = [_frame(i, shift=5 if i == 40 else 0) for i in range(90)]
    assert _flickers(_watch(_encoded(frames))) == []


def test_a_scene_that_is_no_recording_is_never_read():
    assert _flickers(_watch(_ghosting(), document=TITLE)) == []


def test_flipped_tiles_reads_an_excursion_and_its_return():
    a = _frame(0, moving=False)
    b = _frame(0, shift=5, moving=False)
    found = static_chrome.flipped_tiles([a, b, a])
    assert found is not None and found[0] == 1 and int(found[1].sum()) >= static_chrome.MIN_TILES
    # A picture that moves on never comes back: no flip.
    assert static_chrome.flipped_tiles([_frame(0), _frame(1), _frame(2)]) is None


def test_the_finding_refuses_another_render_like_a_failing_picture_finding():
    finding = _flickers(_watch(_ghosting()))[0]
    stored = {"findings": [finding.as_dict()]}
    assert render_checks.STATIC_FLICKER in render_checks.PICTURE_KINDS
    assert [f["kind"] for f in render_checks.open_findings(stored, same_quality=False)] == [
        render_checks.STATIC_FLICKER
    ]


def test_a_resumed_watch_keeps_the_flickers_found_before():
    watch = static_chrome.ChromeWatch()
    scene = RECORDING["scenes"][0]
    for count, frame in enumerate(_ghosting()):
        watch.feed(frame.astype(np.float32), count, scene)
    kept = watch.kept(50)
    assert kept and all(hit[0] < 50 for hit in kept)
    again = static_chrome.ChromeWatch()
    again.restore([list(hit) for hit in kept])
    assert again.hits == kept


@pytest.mark.parametrize("shift", [2, 4, 8])
def test_the_ghost_is_found_whatever_its_offset(shift):
    frames = _encoded([_frame(i, shift=shift if 30 <= i < 60 and i % 2 else 0) for i in range(90)])
    assert len(_flickers(_watch(frames))) == 1
