"""Page chrome over a canvas (a toolbar, a Legend button) is never drawn with the canvas's zoom (#9590).

A box the page pins over a graph canvas is mostly see-through: between two keyframes of a wheel zoom the canvas moves
under it, and :func:`scroll_pacing.riding_boxes` read the box as riding the canvas. Drawn with the canvas, the toolbar
edge and the Legend button showed at a second offset on every frame drawn from a keyframe ("9 box(es) over its canvas
moving with it drawn with the canvas"), clean on the painted ones: a 15 Hz ghost of static chrome. What is proven here:

* the recorder says which boxes over the canvas the page still draws at the same place once the zoom settled
  (``surface["stayed"]``), and the take reads them back on the :class:`scroll_pacing.Scroll`;
* :func:`scroll_pacing._pace_zoom` holds such a box as painted whatever its pixels say, and still draws with the canvas
  a box that did not stay (a label the zoom moved).
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.services.video_studio import capture, scroll_pacing
from tests.test_video_studio_canvas_zoom_6430 import FPS, RATIO, _take, _world


def _surface(**extra) -> dict:
    surface = {"box": [0.0, 0.0, 800.0, 600.0], "at": [400.0, 300.0], "ticks": [[1.0, 0, None], [2.0, 120, None]]}
    surface.update(extra)
    return surface


def test_the_take_reads_back_the_boxes_that_stayed():
    scroll = scroll_pacing._canvas_scroll(
        (1.0, 2.0), _surface(over=[[10, 500, 80, 30], [300, 200, 60, 20]], stayed=[0])
    )
    assert scroll is not None and scroll.stayed == frozenset({0})
    # Not read on an older take, nor from anything but indices.
    assert scroll_pacing._canvas_scroll((1.0, 2.0), _surface(over=[[10, 500, 80, 30]])).stayed == frozenset()
    assert scroll_pacing._canvas_scroll(
        (1.0, 2.0), _surface(over=[[10, 500, 80, 30]], stayed=["0", True, 0])
    ).stayed == frozenset({0})


def test_the_recorder_says_which_boxes_stayed_once_the_zoom_settled():
    legend, label, tooltip = [10.0, 500.0, 80.0, 30.0], [300.0, 200.0, 60.0, 20.0], [420.0, 310.0, 90.0, 40.0]
    surface = _surface(over=[legend, label])

    class _Recorder:
        async def _canvas_at(self, _x, _y):
            # The Legend where it was, the label moved by the zoom, a tooltip opened.
            return {"over": [[11.0, 500.5, 80.0, 30.0], [340.0, 230.0, 72.0, 24.0], tooltip]}

    asyncio.run(capture._Recorder._canvas_appeared(_Recorder(), surface))  # type: ignore[arg-type]
    assert surface["stayed"] == [0]
    assert surface["appeared"] == [[340.0, 230.0, 72.0, 24.0], tooltip]


def _paced(tmp_path, monkeypatch, stayed: frozenset[int]) -> tuple[dict, dict]:
    frames, events = _take(tmp_path, _world())
    [scroll] = scroll_pacing.read_scrolls(events[0]["scrolls"][0])
    scroll.stayed = stayed
    # The worst case of the footage: the box's pixels read as riding the canvas, and never as standing still.
    monkeypatch.setattr(scroll_pacing, "riding_boxes", lambda _levels, over, _zooms: set(range(len(over))))
    real_standing = scroll_pacing.standing
    monkeypatch.setattr(scroll_pacing, "standing", lambda levels, over=(): (real_standing(levels, over)[0], []))
    seen: dict = {}
    with ThreadPoolExecutor(max_workers=2) as saver:
        kept, drawn, _used = scroll_pacing._pace_zoom(
            scroll,
            scroll_pacing.Keyframes(str(tmp_path), frames),
            [t for t, _name in frames],
            fps=FPS,
            ratio=RATIO,
            out_dir=Path(tmp_path),
            serial=0,
            saver=saver,
            seen=seen,
        )
    assert drawn and kept["paced"], kept
    return kept, seen


def test_a_box_that_stayed_is_held_as_painted_whatever_its_pixels_say(tmp_path, monkeypatch):
    kept, seen = _paced(tmp_path, monkeypatch, frozenset({0}))
    assert seen["standing"] == {0} and seen["riding"] == set()
    assert "riding" not in kept and kept.get("overlays") == 1, kept


def test_a_box_that_did_not_stay_is_still_drawn_with_the_canvas(tmp_path, monkeypatch):
    kept, seen = _paced(tmp_path, monkeypatch, frozenset())
    assert seen["riding"] == {0}
    assert kept.get("riding") == 1, kept
