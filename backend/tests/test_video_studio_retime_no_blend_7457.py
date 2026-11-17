"""A recording scene played under speed 1 never draws two take frames cross-dissolved.

On a fixture take, no browser, no database:

* a page of text lines scrolling 13 px between painted instants, and a navigation that swaps the page, played at speed
  0.62 and 0.88: every output frame the compositor draws is one painted frame (``compositor_page.nearestPainted``,
  run in node when it is installed, mirrored here otherwise) - no frame scores as a double image;
* the same frames drawn as before the fix (the painted instants on either side cross-dissolved, ``blendPair``) score as
  a double image on the scroll and ghost the page before over the page after: the check sees the bug;
* a scene at speed 1 plans no painted instants: it draws ``Math.round(t * src_fps)`` exactly as before.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import numpy as np
import pytest

from app.services.video_studio import compositor_page, quality, screen_insert, timeline
from app.services.video_studio.scroll_pacing import DOUBLE_IMAGE_SHARE, double_image_score
from app.services.video_studio.spec import authored_spec, parse_video_spec

TAKE = "00000000-0000-0000-0000-000000007457"
OUT_FPS = 60
W, H = 320, 200
LINE = 13
#: Painted instants of the take: a scroll of half a line per painted frame from 0 to 1 s, a navigation at 1.5 s.
PAINTED = [round(n * 0.1, 2) for n in range(11)] + [1.4, 1.6, 1.8, 2.0]
NAVIGATION = 1.5


def _page(offset: int, *, other: bool = False) -> np.ndarray:
    """A page of dark text lines on white, scrolled *offset* px; *other* a different page (a dashboard of boxes)."""
    page = np.full((H, W), 245.0)
    if other:
        for top in range(10, H - 30, 46):
            page[top : top + 30, 20:140] = 60.0
            page[top : top + 30, 180:300] = 90.0
        return page
    for n in range(offset // LINE, (offset + H) // LINE + 1):
        y = n * LINE - offset
        if 0 <= y < H - 3:
            page[y : y + 3, 16 : W - 16 - (n * 37) % 120] = 30.0
    return page


def _painted_frame(instant: float) -> np.ndarray:
    """The take's picture at painted *instant*: the scroll one line per painted frame, then the other page."""
    if instant >= NAVIGATION:
        return _page(0, other=True)
    return _page(round(min(instant, 1.0) * 10) * 6)


def _nearest(times: list[float], t: float) -> float:
    """``compositor_page.nearestPainted``, mirrored: the painted instant nearest *t*, the earlier on a tie."""
    i, j = 0, len(times) - 1
    while j - i > 1:
        m = (i + j) >> 1
        if times[m] <= t:
            i = m
        else:
            j = m
    return times[j] if abs(times[j] - t) < abs(t - times[i]) else times[i]


def _blend_pair(times: list[float], t: float) -> tuple[float, float, float]:
    """``blendPair`` as it stood earlier: the painted instants on either side of *t* and the later one's share."""
    i = max(k for k in range(len(times)) if times[k] <= t)
    j = min(i + 1, len(times) - 1)
    p0, p1 = times[i], times[j]
    return p0, p1, (t - p0) / (p1 - p0) if p1 > p0 else 0.0


def _plan(speed: float) -> dict:
    take = timeline.TakeMedia(TAKE, 2.2, 1600, 1000, 1.0, 30, 66, sparse=[[0.0, 2.0, PAINTED]])
    raw = {
        "title": "Retime",
        "freestyle": True,
        "scenes": [{"id": "r", "type": "recording", "take_id": TAKE, "speed": speed, "fit": "off", "camera": {"mode": "auto"}}],
    }
    return timeline.estimate_plan(parse_video_spec(authored_spec(raw)), {TAKE: take}).document["scenes"][0]


def _map_time(time_map: list[list[float]], t: float) -> float:
    xs, ys = zip(*time_map, strict=True)
    return float(np.interp(t, xs, ys))


def _sources(planned: dict) -> list[float]:
    """The source second of every output frame of the planned scene at :data:`OUT_FPS`, inside the painted stretch."""
    length = float(planned["end"]) - float(planned["start"])
    sources = [_map_time(planned["time_map"], n / OUT_FPS) for n in range(int(length * OUT_FPS))]
    return [s for s in sources if PAINTED[0] <= s < PAINTED[-1]]


def _doubled(frame: np.ndarray, source: float) -> float:
    """The double image score of *frame* against the two painted frames on either side of *source*."""
    p0, p1, _w = _blend_pair(PAINTED, source)
    if p1 <= p0:
        return 0.0
    return double_image_score(frame, _painted_frame(p0), _painted_frame(p1))


@pytest.mark.parametrize("speed", [0.62, 0.88])
def test_no_output_frame_of_a_slowed_scene_is_two_take_frames(speed):
    planned = _plan(speed)
    assert planned["painted"] == [[0.0, 2.0, PAINTED]] and "blend" not in planned
    sources = _sources(planned)
    assert len(sources) > 60
    scores = [_doubled(_painted_frame(_nearest(PAINTED, s)), s) for s in sources]
    assert max(scores) < DOUBLE_IMAGE_SHARE


@pytest.mark.parametrize("speed", [0.62, 0.88])
def test_the_blend_before_the_fix_draws_the_scroll_twice_and_ghosts_the_page_swap(speed):
    """The check sees the bug: the same frames cross-dissolved as ``blendPair`` drew them score as a double image."""
    sources = _sources(_plan(speed))
    doubled, ghosted = [], []
    for s in sources:
        p0, p1, w = _blend_pair(PAINTED, s)
        if not 0.25 <= w <= 0.75:
            continue
        frame = _painted_frame(p0) * (1 - w) + _painted_frame(p1) * w
        if _doubled(frame, s) >= DOUBLE_IMAGE_SHARE:
            (ghosted if p0 < NAVIGATION <= p1 else doubled).append(s)
    assert doubled, "a scroll step blended draws every line twice"
    assert ghosted, "a page swap blended ghosts the page before over the page after"


def test_a_scene_at_speed_1_plans_no_painted_instants():
    planned = _plan(1.0)
    assert "painted" not in planned and "blend" not in planned


def test_the_drawing_and_report_versions_move():
    assert screen_insert.DRAWING_VERSION >= 18
    assert quality.QUALITY_REPORT_VERSION >= 37


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_compositor_picks_the_nearest_painted_instant(tmp_path):
    """``nearestPainted`` itself, run in node on the fixture's instants, agrees with the mirror at every frame."""
    js = compositor_page.PAGE_JS
    found = re.search(r"function nearestPainted\(spans, t\) \{.*?\n\}", js, re.S)
    assert found, "compositor_page defines nearestPainted"
    sources = _sources(_plan(0.62))
    script = tmp_path / "nearest.js"
    script.write_text(
        found.group(0)
        + f"\nconst spans = {json.dumps([[0.0, 2.0, PAINTED]])};\n"
        + f"console.log(JSON.stringify({json.dumps(sources)}.map((t) => nearestPainted(spans, t))));\n",
        encoding="utf-8",
    )
    node = shutil.which("node") or "node"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60, check=False)  # noqa: S603
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [_nearest(PAINTED, s) for s in sources]
