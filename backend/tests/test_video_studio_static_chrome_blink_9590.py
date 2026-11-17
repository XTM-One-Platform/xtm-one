"""A single blink over a still page is a failing ``static_flicker`` finding (issue #9590, retest of #9960).

A 60 fps take paused on an OpenCTI graph, then a node click located by its highlight: the node's hover dim (the other
nodes faded, its edges lit) was drawn for three take frames and gone, a quarter second before the real hover. The
preview showed it for two of its 30 fps frames. Every gate was green: the watch saw the excursion, but a finding needed
three flicker frames, and a blink is one excursion. Now:

* the frames of that preview (``tests/data/video_studio/hover_dim_blink_9590.png``: its frames at 3.63 s to 3.80 s,
  the graph region, cut from contact sheet d4c63a63 of preview bfeeaf29) are one failing finding;
* a synthetic blink of two and of three frames over a still page is one, a frame shifted once is none;
* a change that moves on, fades in and stays, or a blink while the page moves under it is none;
* the take's gate replay holds the picture before a blink, so the take carries none, and lists the frames dropped.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.services.video_studio import capture_rate, render_checks, static_chrome

DATA = Path(__file__).parent / "data" / "video_studio" / "hover_dim_blink_9590.png"
RECORDING = {"duration": 2.0, "scenes": [{"id": "graph", "type": "recording", "start": 0.0, "end": 2.0}]}
SCENE = RECORDING["scenes"][0]


def _preview_frames() -> list[np.ndarray]:
    """The preview's frames at 3.63, 3.67, 3.70, 3.73, 3.77 and 3.80 s, stacked in the fixture (70 px each)."""
    strip = np.asarray(Image.open(DATA).convert("L"), dtype=np.float32)
    return [strip[k * 70 : (k + 1) * 70] for k in range(strip.shape[0] // 70)]


def _findings(frames: list[np.ndarray]) -> list[render_checks.Finding]:
    watch = static_chrome.ChromeWatch()
    for count, frame in enumerate(frames):
        watch.feed(frame, count, SCENE)
    return static_chrome.findings(watch.hits, 30.0, RECORDING)


def _page(shift: int = 0, *, dimmed: bool = False, x: int = 0) -> np.ndarray:
    """A graph page: nodes on a dark ground; *dimmed* fades every node, as a hover dim does."""
    rng = np.random.default_rng(9960)
    ground = rng.uniform(20, 60, (108, 192 * 2)).astype(np.float32)
    frame = ground[:, x : x + 192].copy()
    for k in range(24):
        cx, cy = 12 + (k % 8) * 22 + shift, 14 + (k // 8) * 30
        frame[cy : cy + 12, cx : cx + 12] = 90.0 if dimmed else 230.0
    return frame


def test_the_hover_dim_blink_of_the_preview_is_one_failing_finding():
    frames = _preview_frames()
    assert len(frames) == 6
    lead = [frames[0]] * 6
    found = _findings([*lead, *frames, *[frames[-1]] * 6])
    assert len(found) == 1, [f.message for f in found]
    finding = found[0]
    assert finding.kind == render_checks.STATIC_FLICKER and finding.severity == render_checks.FAIL
    # The blink sits on the two frames after 3.67 s: watch frames 8 and 9 of this feed.
    assert 7 / 30 <= finding.at <= 9 / 30 and finding.data["frames"] >= static_chrome.BLINK_FRAMES


@pytest.mark.parametrize("away", [2, 3])
def test_a_blink_of_two_or_three_frames_over_a_still_page_is_a_finding(away):
    still = _page()
    frames = [still] * 10 + [_page(dimmed=True)] * away + [still] * 10
    found = _findings(frames)
    assert len(found) == 1
    assert found[0].data["frames"] == away


def test_one_frame_away_is_no_blink():
    still = _page()
    assert _findings([still] * 10 + [_page(dimmed=True)] + [still] * 10) == []


def test_a_change_that_stays_or_a_page_that_moves_on_is_no_blink():
    assert _findings([_page()] * 10 + [_page(dimmed=True)] * 20) == []
    assert _findings([_page(x=4 * k) for k in range(30)]) == []


def test_a_two_frame_excursion_while_the_page_moves_is_no_blink():
    # The ground scrolls under the nodes: the page the excursion comes back to is not the one it left.
    frames = [_page(x=6 * k, dimmed=k in (10, 11)) for k in range(25)]
    assert _findings(frames) == []


def test_a_kept_watch_from_an_older_release_reads_its_hits_without_the_blink_flag():
    watch = static_chrome.ChromeWatch()
    watch.restore([[3, 4, 0.1, 0.1, 0.2, 0.2], [4, 4, 0.1, 0.1, 0.2, 0.2, 1]])
    assert watch.hits == [[3, 4, 0.1, 0.1, 0.2, 0.2, 0], [4, 4, 0.1, 0.1, 0.2, 0.2, 1]]
    assert static_chrome.is_run(watch.hits)
    assert not static_chrome.is_run(watch.hits[:1])


def _jpeg(path: Path, frame: np.ndarray) -> None:
    Image.fromarray(np.clip(frame, 0, 255).astype(np.uint8)).convert("RGB").save(path, format="JPEG", quality=95)


def test_the_take_holds_the_frame_before_a_blink_and_lists_it(tmp_path: Path) -> None:
    # A 60 fps take: a pause, then a click; three take frames of the hover dim a quarter second before the click,
    # and no probe span logged for them. The gate replay reads them as the render does and holds the frame before.
    frames = []
    for k in range(120):
        dimmed = k in (60, 61, 62)
        name = f"{'a' if dimmed else 'f'}{k:06d}.jpg"
        _jpeg(tmp_path / name, _page(dimmed=dimmed))
        frames.append((round(k / 60, 6), name))
    events = [{"step": 15, "action": "pause", "t0": 0.0}, {"step": 16, "action": "click", "t0": 0.9}]
    kept, flashes = capture_rate.held_flashes(frames, events, str(tmp_path))
    assert all(not name.startswith("a") for _t, name in kept)
    assert flashes and sum(f["frames"] for f in flashes) == 3
