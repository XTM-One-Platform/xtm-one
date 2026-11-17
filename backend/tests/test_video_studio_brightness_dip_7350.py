"""A recording's highlight never dims the whole frame, and a dip is a finding.

Video 62aabdf7, scene s11 (a recording crossfaded in): its automatic highlight drew a 0.6 black shadow over the whole
stage - theme background, overlays and subtitles included -, the finished MP4 reading 27.1 -> 12.3 -> 25.3 in mean
luma over about 1.2 s right after the transition, and the quality check said nothing.

* the page draws a highlight's dim inside the recording's own picture, light enough to stay under the gate;
* ``brightness_dip`` finds a dim of the whole frame outside any transition, timed, with its three luma values;
* a fade through black, a crossfade, any declared transition, a cut, an author's spotlight and a card are left alone;
* the frozen-picture watch reads it on the same frames, and a resumed watch keeps the lumas it read;
* the finding holds another render of the same quality, like a frozen picture, and its scene's preview clears it.
"""

from __future__ import annotations

import asyncio
import math
import re

import numpy as np
import pytest

from app.services.video_studio import brightness_dip, compositor_page, encoder, frame_freeze, render_checks

RATE = 30.0


def _lumas(seconds: float, level: float = 27.0) -> list[float]:
    return [level] * round(seconds * RATE)


def _dipped(at: float, length: float, factor: float, total: float = 6.0, level: float = 27.0) -> list[float]:
    lumas = _lumas(total, level)
    for i in range(round(at * RATE), round((at + length) * RATE)):
        lumas[i] = level * factor
    return lumas


RECORDING = {"duration": 6.0, "scenes": [{"id": "s11", "type": "recording", "start": 0.0, "end": 6.0}]}


def test_the_reported_dip_is_found_with_its_time_its_scene_and_its_three_lumas():
    # 27.1 -> 12.3 -> 25.3: the frames of render 62aabdf7's s11, held about 0.6 s.
    lumas = _lumas(2.0, 27.1) + [20.0, 15.0] + [12.3] * 18 + [18.0, 22.0] + _lumas(3.0, 25.3)
    found = brightness_dip.findings(lumas, RATE, RECORDING)
    assert [(f.kind, f.scene_id, f.severity) for f in found] == [("brightness_dip", "s11", "warn")]
    dip = found[0]
    assert dip.data["before"] == 27.1 and dip.data["minimum"] == 12.3 and dip.data["after"] == 25.3
    assert dip.at < 2.0 < dip.data["minimum_at"] < dip.end <= 3.0
    assert "dims to 45% of its brightness" in dip.message and "mean luma 27.1 -> 12.3 -> 25.3" in dip.message
    assert re.search(r"\(0:0\d\.\d\d to 0:0\d\.\d\d\)", dip.message)


def test_a_synthetic_half_brightness_second_is_a_dip_and_a_tenth_darker_is_not():
    assert len(brightness_dip.findings(_dipped(2.0, 1.0, 0.5), RATE, RECORDING)) == 1
    assert brightness_dip.findings(_dipped(2.0, 1.0, 0.9), RATE, RECORDING) == []
    # The new highlight's dim on a full-bleed recording: 12 % darker at most, never a dip.
    assert brightness_dip.findings(_dipped(2.0, 1.0, 1 - 0.12), RATE, RECORDING) == []


def test_a_picture_that_stays_darker_is_a_new_picture_never_a_dip():
    lumas = _lumas(2.0) + _lumas(4.0, 10.0)
    assert brightness_dip.dips(lumas, RATE) == []


def test_a_dark_picture_and_frames_not_read_are_never_judged():
    assert brightness_dip.dips(_dipped(2.0, 0.5, 0.3, level=5.0), RATE) == []
    lumas = _dipped(2.0, 0.6, 0.4)
    for i in range(round(2.1 * RATE), round(2.3 * RATE)):
        lumas[i] = math.nan
    assert brightness_dip.dips(lumas, RATE) == []


@pytest.mark.parametrize(
    "document",
    [
        # A fade through black and a crossfade (the blended windows).
        {
            "duration": 6.0,
            "scenes": [
                {"id": "a", "type": "recording", "start": 0.0, "end": 2.5},
                {"id": "b", "type": "recording", "start": 1.8, "end": 6.0, "transition_in": {"type": "fade_black", "duration": 1.4}},
            ],
        },
        # Any declared transition (a zoom), and a plain cut from one picture to another.
        {
            "duration": 6.0,
            "scenes": [
                {"id": "a", "type": "clip", "start": 0.0, "end": 2.3},
                {"id": "b", "type": "clip", "start": 2.0, "end": 6.0, "transition_in": {"type": "zoom", "duration": 0.6}},
            ],
        },
        {"duration": 6.0, "scenes": [{"id": "a", "type": "clip", "start": 0.0, "end": 2.4}, {"id": "b", "type": "clip", "start": 2.4, "end": 6.0}]},
        # An author's spotlight overlay asks for the dim.
        {
            "duration": 6.0,
            "scenes": [{"id": "a", "type": "recording", "start": 0.0, "end": 6.0, "overlays": [{"type": "spotlight", "t0": 1.9, "t1": 3.2}]}],
        },
        # A statement's reveal: its own motion.
        {"duration": 6.0, "scenes": [{"id": "s", "type": "statement", "start": 0.0, "end": 6.0}]},
        # The video's own fade from black.
        {"duration": 6.0, "fade_in": 3.0, "scenes": [{"id": "a", "type": "clip", "start": 0.0, "end": 6.0}]},
    ],
)
def test_transitions_cuts_spotlights_and_cards_are_never_a_dip(document):
    assert brightness_dip.findings(_dipped(2.0, 1.0, 0.4), RATE, document) == []


def test_a_previews_dip_is_timed_in_its_own_mp4():
    document = {"duration": 20.0, "scenes": [{"id": "s11", "type": "recording", "start": 0.0, "end": 20.0}]}
    found = brightness_dip.findings(_dipped(2.0, 1.0, 0.4), RATE, document, offset=10.0)
    assert found[0].at == pytest.approx(1.5) and found[0].scene_id == "s11"


def test_the_watch_reads_the_dip_on_the_frames_it_decodes(monkeypatch):
    rng = np.random.default_rng(7350)
    base = [rng.integers(20, 40, size=(108, 192), dtype=np.uint8) for _ in range(150)]
    # Frames 60-89 (2.0 s to 3.0 s) at half their brightness: the whole frame dimmed, still moving.
    frames = [(f // 2 if 60 <= i < 90 else f).astype(np.uint8) for i, f in enumerate(base)]

    async def stream(_path, *, width, height, fps, start=0.0):
        for frame in frames:
            yield frame.tobytes()

    monkeypatch.setattr(encoder, "iter_gray_frames", stream)
    check = asyncio.run(frame_freeze.check_frames("final.mp4", document=RECORDING, width=1920, height=1080, fps=30))
    dips = [f for f in check.findings if f.kind == render_checks.BRIGHTNESS_DIP]
    assert [(f.scene_id, f.data["minimum_at"] >= 2.0) for f in dips] == [("s11", True)]
    assert check.summary["brightness_dips"] == 1


def test_a_resumed_watch_keeps_the_lumas_it_read():
    watch, holds, lumas = frame_freeze.FreezeWatch(RATE), frame_freeze.DesignedHolds(), [27.0, math.nan, 13.5]
    watch.restore(3, [], {})
    kept = frame_freeze._kept_state(watch, holds, [], lumas)
    assert kept["lumas"] == [27.0, None, 13.5]
    again: list[float] = []
    assert frame_freeze._restore(kept, frame_freeze.FreezeWatch(RATE), frame_freeze.DesignedHolds(), [], again) == 3
    assert again[0] == 27.0 and math.isnan(again[1]) and again[2] == 13.5
    # An older watch has no lumas: its frames are not read for a dip.
    old: list[float] = []
    frame_freeze._restore({"frame": 2}, frame_freeze.FreezeWatch(RATE), frame_freeze.DesignedHolds(), [], old)
    assert len(old) == 2 and all(math.isnan(v) for v in old)


def test_a_dip_holds_a_render_of_the_same_quality_and_fails_its_scene():
    finding = brightness_dip.findings(_dipped(2.0, 1.0, 0.4), RATE, RECORDING)[0].as_dict()
    checks = {"findings": [finding]}
    assert render_checks.open_findings(checks, same_quality=True) == [finding]
    assert render_checks.open_findings(checks, same_quality=False) == []
    assert not render_checks.scene_passed(checks, "s11")
    assert "preview the scene again" in render_checks.finding_fix(finding)


def test_the_page_draws_a_highlight_inside_the_recording_and_lightly():
    source = compositor_page.PAGE_JS
    dim = float(re.search(r"const HIGHLIGHT_DIM = ([0-9.]+);", source).group(1))
    # The darkest a full-bleed recording gets under its highlight stays above the gate's ratio.
    assert dim > 0 and 1 - dim > brightness_dip.DIP_RATIO + 0.1
    assert "highlightNodes.push(el('div', 'spot', vp))" in source
    assert "highlightNodes.push(el('div', 'spot', fx))" not in source
    # The highlight's own shadow uses the light dim; an author's spotlight overlay keeps its own, asked for.
    highlight = source[source.index("const active = (sc.highlights") : source.index("// The cursor and its clicks.")]
    assert "rgba(2,5,12,${HIGHLIGHT_DIM * a})" in highlight and "0.6 * a" not in highlight
