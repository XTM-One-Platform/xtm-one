"""A canvas zoom in a stretch the screencast barely painted is never drawn from keyframes outside it (#9744 retest).

On a GPU take, a wheel then a marker then a long pause were painted at 0 to 1 frame a second for 7 s: the zoom over
that stretch was drawn from the keyframe painted 8 s before it and the one painted 13 s after it, and the frames it
drew carried those moments into the stretch - a cursor on a node clicked later, a tooltip of a button clicked before,
the toolbar moved - which ``static_flicker`` blocked. What is proven here:

* :func:`scroll_pacing.zoom_sources` keeps the keyframe before and after a zoom only within ``ZOOM_KEYFRAME_REACH`` of
  its span, and says how many it left out;
* :func:`scroll_pacing._pace_zoom` over a 7 s paint gap draws no frame (the take holds the last frame painted, as the
  encoder does between two keyframes) and its log names the stretch: ``why`` and ``outside``;
* a zoom whose neighbours are near keeps them (the paced zoom of #6430 is unchanged).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

from app.services.video_studio import scroll_pacing
from tests.test_video_studio_canvas_zoom_6430 import BOX_CSS, FPS, RATIO, TOOLBAR_CSS, _take, _world


def test_zoom_sources_leave_out_a_keyframe_painted_far_from_the_span():
    times = [0.0, 8.0, 21.0]
    sources, left_out = scroll_pacing.zoom_sources(times, (1.0, 7.0))
    assert sources == [] and left_out == 2
    # Within the reach on both sides: both kept, as before.
    sources, left_out = scroll_pacing.zoom_sources([0.6, 1.5, 7.4], (1.0, 7.0))
    assert sources == [0, 1, 2] and left_out == 0
    # A keyframe inside is always kept; only the far neighbour goes.
    sources, left_out = scroll_pacing.zoom_sources([0.0, 3.0, 7.2], (1.0, 7.0))
    assert sources == [1, 2] and left_out == 1


def _gap_take(tmp_path: Path) -> tuple[list[tuple[float, str]], list[float]]:
    """Two keyframes 9 s apart, each a different picture: the page before the pause and a later moment."""
    frames = []
    for k, (t, shade) in enumerate([(0.0, 40), (9.0, 200)]):
        name = f"g{k:06d}.jpg"
        Image.fromarray(np.full((1350, 2400, 3), shade, dtype=np.uint8)).save(tmp_path / name, quality=92)
        frames.append((t, name))
    return frames, [t for t, _name in frames]


def test_a_pause_with_a_seven_second_paint_gap_holds_and_never_draws_from_keyframes_outside(tmp_path):
    frames, times = _gap_take(tmp_path)
    scroll = scroll_pacing.Scroll(
        span=(1.0, 8.0),
        log=[(1.0, 0.0), (1.5, 100.0), (8.0, 100.0)],
        scroller="canvas",
        progress="wheel",
        box=tuple(BOX_CSS),
        over=[tuple(TOOLBAR_CSS)],
    )
    reader = scroll_pacing.Keyframes(str(tmp_path), frames)
    with ThreadPoolExecutor(max_workers=2) as saver:
        kept, drawn, used = scroll_pacing._pace_zoom(
            scroll, reader, times, fps=FPS, ratio=RATIO, out_dir=Path(tmp_path), serial=0, saver=saver
        )
    # No frame drawn: the take keeps its keyframes, the mezzanine holds the frame painted at 0.0 s through the pause.
    assert drawn == [] and used == set(), kept
    assert kept["paced"] is False
    assert kept["why"] == scroll_pacing.OUTSIDE_STRETCH
    assert kept["outside"] == 2


def test_a_zoom_with_near_keyframes_is_still_paced(tmp_path):
    frames, events = _take(tmp_path, _world())
    [scroll] = scroll_pacing.read_scrolls(events[0]["scrolls"][0])
    times = [t for t, _name in frames]
    reader = scroll_pacing.Keyframes(str(tmp_path), frames)
    with ThreadPoolExecutor(max_workers=2) as saver:
        kept, drawn, _used = scroll_pacing._pace_zoom(
            scroll, reader, times, fps=FPS, ratio=RATIO, out_dir=Path(tmp_path), serial=0, saver=saver
        )
    assert drawn and kept["paced"], kept
    assert "outside" not in kept
    # Every frame drawn is a frame drawn for the zoom or a keyframe of its own reach, never one painted far from it.
    names = {name for _t, name in frames}
    lo, hi = scroll.span
    reach = scroll_pacing.ZOOM_KEYFRAME_REACH
    for _t, name in drawn:
        if name in names:
            painted = next(t for t, n in frames if n == name)
            assert lo - reach <= painted <= hi + reach, (name, painted)
