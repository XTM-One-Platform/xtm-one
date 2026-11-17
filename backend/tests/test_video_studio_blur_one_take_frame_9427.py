"""A recording scene drawn under motion blur never averages two take frames into one double image (issue #9427).

At ``high`` and ``ultra`` with ``motion_blur: on`` each output frame is the average of ``motion_design.BLUR_SAMPLES``
sub-frames drawn over the shutter. A take that changes frame inside a shutter (a stepping capture, a frame held to
the next keyframe) used to show a different take frame on each sub-frame: the average was a mix of two pages, the
``double_image`` the full render reported on scenes whose preview (drawn without blur) was green. On a fixture take,
no browser, no database:

* the sub-frames of every output frame picked as before (the take frame at each sub-frame's own time) average into a
  frame ``double_frames.read_blend`` scores as a double image where the take steps: the check sees the bug;
* picked as the page now picks them (``compositor_page.takeTime``: the output frame's centre) every output frame is
  one take frame, and no frame scores as a double image;
* ``takeTime`` itself, run in node when it is installed, picks the centre for every sub-frame offset.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess

import numpy as np
import pytest

from app.services.video_studio import compositor_page, motion_design, screen_insert
from app.services.video_studio.double_frames import read_blend

OUT_FPS = 60
SRC_FPS = 30
W, H = 192, 108
LINE = 9
#: The take's keyframes: the capture painted every 3rd frame of its 30 fps (10 fps, stepping); each held to the next.
STEP = 3
#: The output frames drawn: two seconds of the scene from its first second.
FRAMES = range(OUT_FPS, 3 * OUT_FPS)


def _js_round(value: float) -> int:
    """``Math.round``: a half rounds up (Python's ``round`` rounds it to even)."""
    return math.floor(value + 0.5)


def _page(offset: int) -> np.ndarray:
    """A page of dark text lines on white, scrolled *offset* px."""
    page = np.full((H, W), 245.0, dtype=np.float32)
    for n in range(offset // LINE, (offset + H) // LINE + 1):
        y = n * LINE - offset
        if 0 <= y < H - 2:
            page[y : y + 2, 12 : W - 12 - (n * 29) % 80] = 30.0
    return page


def _take_frame(index: int) -> np.ndarray:
    """The take's frame *index*: the keyframe before it, the page scrolled 4 px per keyframe."""
    return _page(4 * (index // STEP))


def _old_time(frame: int, offset: float) -> float:
    """Before the fix: the take frame at the sub-frame's own time."""
    return (frame + offset) / OUT_FPS


def _new_time(frame: int, offset: float) -> float:
    """``compositor_page.takeTime`` mirrored: the output frame's centre (``S.frame = Math.round(index)``)."""
    return _js_round(frame + offset) / OUT_FPS


def _shown(t: float) -> np.ndarray:
    """``frameSource.show``: the take frame ``Math.round(t * src_fps)``."""
    return _take_frame(_js_round(t * SRC_FPS))


def _drawn(pick) -> list[np.ndarray]:
    offsets = motion_design.blur_offsets(motion_design.BLUR_SAMPLES)
    frames = []
    for frame in FRAMES:
        subs = [_shown(pick(frame, offset)) for offset in offsets]
        frames.append(np.mean(subs, axis=0))
    return frames


def _doubles(frames: list[np.ndarray]) -> int:
    count = 0
    for before, frame, after in zip(frames, frames[1:], frames[2:], strict=False):
        read = read_blend(before, frame, after)
        if read is not None and read.double:
            count += 1
    return count


def test_blur_is_on_with_several_offsets():
    offsets = motion_design.blur_offsets(motion_design.BLUR_SAMPLES)
    assert len(offsets) > 1 and any(offset < 0 for offset in offsets) and any(offset > 0 for offset in offsets)


def test_the_old_pick_averages_two_take_frames_into_a_double_image():
    assert _doubles(_drawn(_old_time)) > 0


def test_every_sub_frame_now_shows_the_take_frame_of_its_centre():
    frames = _drawn(_new_time)
    assert _doubles(frames) == 0
    for frame, drawn in zip(FRAMES, frames, strict=True):
        assert np.array_equal(drawn, _shown(frame / OUT_FPS))


def test_the_page_picks_the_take_frame_at_the_centre():
    js = compositor_page.PAGE_JS
    assert "function takeTime(sc, t)" in js
    assert "const ft = S.media ? t : takeTime(sc, t);" in js
    assert "frames.show(mapTime(sc.time_map, ft), mapRate(sc.time_map, ft));" in js


def test_inserts_pick_their_frame_at_the_centre_too():
    js = compositor_page.PAGE_JS
    assert "mapTime(ins.time_map, takeTime(sc, t))" in js
    assert "inserts(t, mapTime(sc.time_map, ft));" in js
    assert "inserts(t, mapTime(sc.time_map, t));" not in js


def test_the_drawing_version_moves():
    assert screen_insert.DRAWING_VERSION >= 23


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_take_time_in_node_matches_the_mirror(tmp_path):
    js = compositor_page.PAGE_JS
    found = re.search(r"function takeTime\(sc, t\) \{.*?\n\}", js, re.S)
    assert found, "compositor_page defines takeTime"
    start = 1.5
    offsets = motion_design.blur_offsets(motion_design.BLUR_SAMPLES)
    asked = [[frame, frame + offset] for frame in range(90, 130) for offset in [0.0, *offsets]]
    script = tmp_path / "take_time.js"
    script.write_text(
        f"const S = {{ fps: {OUT_FPS}, frame: 0 }};\n"
        + found.group(0)
        + f"\nconst sc = {{ start: {start} }};\n"
        + f"console.log(JSON.stringify({json.dumps(asked)}.map(([f, at]) => {{ S.frame = Math.round(at); "
        + "return takeTime(sc, at / S.fps - sc.start); })));\n",
        encoding="utf-8",
    )
    node = shutil.which("node") or "node"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60, check=False)  # noqa: S603
    assert result.returncode == 0, result.stderr
    got = json.loads(result.stdout)
    assert got == pytest.approx([frame / OUT_FPS - start for frame, _at in asked])
