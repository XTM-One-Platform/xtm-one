"""A recording frame drawn as two pictures at once is a finding of the render, wherever it was made (point 4).

The take-level checks read a take and the camera's keyframes; a blend made further down the pipeline (a retime,
a camera move, a hold) reached the audience with every gate green. ``double_frames`` reads the encoded render itself,
on the frames the frozen-picture watch decodes:

* a frame that is a mix of the two around it (a scroll step blended, a page swap blended) is a double image;
* a page that scrolls, holds, swaps on a cut or fades a small control is never one, nor a mix inside a transition;
* the finding names its time, its scene and the frame at its strongest blend, and holds another render of the same
  quality like a frozen picture;
* a scene preview's finding is timed in its own MP4, and a resumed watch keeps what it found.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
from scipy.ndimage import gaussian_filter

from app.services.video_studio import double_frames, encoder, frame_freeze, quality, render_checks

RATE = 30.0
H, W = 108, 192
RECORDING = {"duration": 4.0, "scenes": [{"id": "s3", "type": "recording", "start": 0.0, "end": 4.0}]}


def _page(seed: int, rows: int = H * 4) -> np.ndarray:
    """A page of text as the watch's grey copy reads it: lines of dark words on a light background."""
    rng = np.random.default_rng(seed)
    page = np.full((rows, W), 235.0)
    for y in range(4, rows - 6, 9):
        x = 4
        while x < W - 10:
            width = int(rng.integers(4, 18))
            page[y : y + 3, x : x + width] = 60
            x += width + int(rng.integers(3, 6))
    return page


PAGE, OTHER = _page(3), _page(9)


def _view(page: np.ndarray, scrolled: int) -> np.ndarray:
    return gaussian_filter(page[scrolled : scrolled + H], 0.6)


def _encoded(frame: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    return np.clip(frame + rng.normal(0.0, 1.5, frame.shape), 0, 255).astype(np.uint8)


def _take_frames(double_at: int | None = None, total: int = 120) -> list[np.ndarray]:
    """A take scrolling a page 3 px a frame for a second, held, then swapping page on a cut; *double_at*: the one
    frame drawn as a 50/50 mix of the frames around it - the known double frame."""
    rng = np.random.default_rng(7457)
    pictures = []
    for i in range(total):
        if i < 30:
            pictures.append(_view(PAGE, 3 * i))
        elif i < 70:
            pictures.append(_view(PAGE, 90))
        else:
            pictures.append(_view(OTHER, 0))
    if double_at is not None:
        pictures[double_at] = 0.5 * pictures[double_at - 1] + 0.5 * pictures[double_at + 1]
    return [_encoded(picture, rng) for picture in pictures]


def _watch(frames: list[np.ndarray], document: dict = RECORDING, **kwargs) -> frame_freeze.FrameCheck:
    async def stream(_path, *, width, height, fps, start=0.0):
        for frame in frames:
            yield frame.tobytes()

    original = encoder.iter_gray_frames
    encoder.iter_gray_frames = stream
    try:
        return asyncio.run(frame_freeze.check_frames("final.mp4", document=document, width=1920, height=1080, fps=30, **kwargs))
    finally:
        encoder.iter_gray_frames = original


def _doubles(check: frame_freeze.FrameCheck) -> list[render_checks.Finding]:
    return [f for f in check.findings if f.kind == render_checks.DOUBLE_IMAGE]


def test_a_render_with_a_known_double_frame_fails_the_check_naming_the_frame():
    found = _doubles(_watch(_take_frames(double_at=15)))
    assert len(found) == 1
    finding = found[0]
    assert finding.scene_id == "s3" and finding.severity == render_checks.WARN
    assert finding.data["frame"] == 15 and finding.data["frame_at"] == pytest.approx(0.5)
    assert finding.data["blend"] == pytest.approx(0.5, abs=0.05)
    assert finding.at == pytest.approx(0.5) and finding.end == pytest.approx(16 / RATE)
    assert "frame 15 at 0:00.50" in finding.message and "two pictures" in finding.message


def test_the_same_render_without_the_double_frame_passes():
    check = _watch(_take_frames())
    assert _doubles(check) == [] and "double_images" not in check.summary


def test_a_page_swap_blended_inside_the_scene_is_found_at_the_swap():
    found = _doubles(_watch(_take_frames(double_at=70)))
    assert [(f.data["frame"], f.scene_id) for f in found] == [(70, "s3")]


def test_the_summary_counts_the_double_images():
    assert _watch(_take_frames(double_at=15)).summary["double_images"] == 1


def test_a_scroll_a_hold_and_a_cut_are_never_a_mix_of_their_neighbours():
    rng = np.random.default_rng(1)
    for step in (2, 3, 6, 9):
        a, f, b = (_encoded(_view(PAGE, 50 + k * step), rng) for k in range(3))
        read = double_frames.read_blend(a, f, b)
        assert read is not None and not read.double, (step, read)
    hold = _encoded(_view(PAGE, 50), rng)
    assert not (double_frames.read_blend(hold, hold, hold) or double_frames.Blend(0, 1, None)).double
    cut = double_frames.read_blend(_encoded(_view(PAGE, 50), rng), _encoded(_view(PAGE, 50), rng), _encoded(_view(OTHER, 0), rng))
    assert cut is not None and not cut.double


def test_a_blend_at_any_mix_in_the_range_is_a_double_image():
    rng = np.random.default_rng(2)
    one, other = _view(PAGE, 40), _view(PAGE, 46)
    for mix in (0.3, 0.5, 0.7):
        read = double_frames.read_blend(_encoded(one, rng), _encoded((1 - mix) * one + mix * other, rng), _encoded(other, rng))
        assert read is not None and read.double and read.blend == pytest.approx(mix, abs=0.05)


def test_a_small_control_fading_is_never_judged():
    rng = np.random.default_rng(3)
    plain = _view(PAGE, 50)
    lit = plain.copy()
    lit[40:46, 60:70] = 120
    read = double_frames.read_blend(_encoded(plain, rng), _encoded(0.5 * plain + 0.5 * lit, rng), _encoded(lit, rng))
    assert read is None or not read.double


def test_only_recording_scenes_are_read_and_never_across_a_scene_change():
    document = {
        "duration": 4.0,
        "scenes": [
            {"id": "c", "type": "clip", "start": 0.0, "end": 1.0},
            {"id": "s3", "type": "recording", "start": 1.0, "end": 4.0},
        ],
    }
    # The mix at frame 15 sits in a clip: not a recording frame.
    assert _doubles(_watch(_take_frames(double_at=15), document)) == []


@pytest.mark.parametrize(
    "transition",
    [{"type": "crossfade", "duration": 0.5}, {"type": "fade_black", "duration": 0.5}, {"type": "zoom", "duration": 0.5}],
)
def test_a_mix_inside_a_transition_into_the_scene_is_never_a_finding(transition):
    document = {
        "duration": 4.0,
        "scenes": [
            {"id": "s2", "type": "recording", "start": 0.0, "end": 2.6},
            {"id": "s3", "type": "recording", "start": 2.2, "end": 4.0, "transition_in": transition},
        ],
    }
    hits = [[70, 0.5, 1.0]]
    assert double_frames.findings(hits, RATE, document) == []
    # Past the transition, the same frame is one.
    assert len(double_frames.findings([[100, 0.5, 1.0]], RATE, document)) == 1


def test_two_double_frames_side_by_side_are_one_finding():
    found = double_frames.findings([[15, 0.4, 0.9], [16, 0.5, 1.0], [40, 0.5, 1.0]], RATE, RECORDING)
    assert [(f.data["frames"], f.data["frame"]) for f in found] == [(2, 16), (1, 40)]


def test_the_message_agrees_with_the_number_of_frames():
    """One frame draws, several frames draw."""
    (one,) = double_frames.findings([[40, 0.5, 1.0]], RATE, RECORDING)
    assert one.message.startswith("1 frame of a recording draws two pictures at once")
    (three,) = double_frames.findings([[15, 0.4, 0.9], [16, 0.5, 1.0], [17, 0.6, 0.9]], RATE, RECORDING)
    assert three.data["frames"] == 3
    assert three.message.startswith("3 frames of a recording draw two pictures at once")


def test_a_previews_double_image_is_timed_in_its_own_mp4():
    document = {"duration": 20.0, "scenes": [{"id": "s3", "type": "recording", "start": 0.0, "end": 20.0}]}
    found = double_frames.findings([[15, 0.5, 1.0]], RATE, document, offset=10.0)
    assert found[0].at == pytest.approx(0.5) and found[0].data["frame"] == 315 and found[0].scene_id == "s3"


def test_a_resumed_watch_keeps_the_double_frames_it_found():
    watch, holds, doubles = frame_freeze.FreezeWatch(RATE), frame_freeze.DesignedHolds(), double_frames.DoubleWatch()
    doubles.hits = [[15, 0.5, 1.0], [40, 0.5, 1.0]]
    watch.restore(30, [], {})
    kept = frame_freeze._kept_state(watch, holds, [], [], doubles)
    assert kept["doubles"] == [[15, 0.5, 1.0]]
    again = double_frames.DoubleWatch()
    assert frame_freeze._restore(kept, frame_freeze.FreezeWatch(RATE), frame_freeze.DesignedHolds(), [], [], again) == 30
    assert again.hits == [[15, 0.5, 1.0]]
    # An older watch has none: nothing is made up.
    del kept["doubles"]
    fresh = double_frames.DoubleWatch()
    frame_freeze._restore(kept, frame_freeze.FreezeWatch(RATE), frame_freeze.DesignedHolds(), [], [], fresh)
    assert fresh.hits == []


def test_a_double_image_holds_a_render_of_the_same_quality_and_is_labelled():
    finding = double_frames.findings([[15, 0.5, 1.0]], RATE, RECORDING)[0].as_dict()
    checks = {"findings": [finding]}
    assert render_checks.open_findings(checks, same_quality=True) == [finding]
    assert render_checks.open_findings(checks, same_quality=False) == []
    assert "two pictures" in render_checks.finding_fix({"kind": render_checks.DOUBLE_IMAGE})


def test_the_quality_report_version_is_bumped():
    assert quality.QUALITY_REPORT_VERSION >= 39
