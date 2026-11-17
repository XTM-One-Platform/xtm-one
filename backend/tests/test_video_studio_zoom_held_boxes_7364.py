"""What stands over a zoomed canvas is held from the keyframe the canvas under it is drawn from.

Each frame between two keyframes of a canvas zoomed by the wheel is the NEARER keyframe moved by its share
of the zoom. What stands still over the canvas (a box the page draws over it, a label that never moved) was still held
as the keyframe BEFORE painted it, on every frame: on the frames drawn from the keyframe after, the canvas and the boxes
over it came from two keyframes, and a label crossing a held box's edge read split, the ones inside it offset - a
flicker of the labels on a few frames of the zoom. What is proven here:

* :func:`scroll_pacing._draw_zoom_frame` holds what stands still from the nearer keyframe: past half-way, the keyframe
  after, the one the canvas is drawn from;
* a zoom paced now says ``held_from: nearer``; a warped zoom holding boxes without it (paced by an older release) is named by
  :func:`scroll_pacing.misheld_zooms`, which the save says as ``double_image``; one paced now, or one holding nothing,
  is not.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from app.services.video_studio import scroll_pacing
from tests.test_video_studio_canvas_zoom_6430 import FPS, RATIO, _take, _world
from tests.test_video_studio_zoom_double_image_7337 import _labelled, _with_labels

#: A box held over the canvas (device px of the box): what the page draws over it.
HELD = (40, 40, 200, 80)


def _keyframe(colour: tuple[int, int, int], label_at: int) -> Image.Image:
    """A canvas box with a node and a label in the held box, the label where this keyframe painted it."""
    picture = Image.new("RGB", (400, 300), (10, 10, 10))
    draw = ImageDraw.Draw(picture)
    draw.rectangle([250, 150, 290, 190], fill=colour)
    draw.rectangle([label_at, 50, label_at + 60, 70], fill=(250, 250, 250))
    return picture


def _held_mask() -> Image.Image:
    mask = Image.new("L", (400, 300), 0)
    x, y, w, h = HELD
    ImageDraw.Draw(mask).rectangle([x, y, x + w - 1, y + h - 1], fill=255)
    return mask


def _white(shown: np.ndarray, x0: int, x1: int) -> int:
    return int((shown[50:71, x0:x1].min(axis=2) > 200).sum())


def test_a_held_box_comes_from_the_keyframe_the_canvas_is_drawn_from(tmp_path):
    # The label in the held box sits at x 60 on the keyframe before, at x 150 on the one after: two keyframes.
    before, after = _keyframe((250, 0, 0), 60), _keyframe((0, 0, 250), 150)
    zoom = scroll_pacing.Zoom(1.1, -27.0, -17.0)
    for u, near_after in ((0.3, False), (0.7, True)):
        path = tmp_path / f"z{u}.jpg"
        assert scroll_pacing._draw_zoom_frame(before, before, after, zoom, u, (0, 0), _held_mask(), path)
        with Image.open(path) as opened:
            shown = np.asarray(opened.convert("RGB"), dtype=np.int16)
        reds = int(((shown[..., 0] > 150) & (shown[..., 2] < 80)).sum())
        blues = int(((shown[..., 2] > 150) & (shown[..., 0] < 80)).sum())
        # The canvas: the nearer keyframe moved.
        assert (blues > 0) == near_after and (reds > 0) == (not near_after), (u, reds, blues)
        # The held box: the same keyframe's label, once - never the other keyframe's over the canvas of this one.
        at_before, at_after = _white(shown, 60, 121), _white(shown, 150, 211)
        if near_after:
            assert at_after > 600 and at_before == 0, (u, at_before, at_after)
        else:
            assert at_before > 600 and at_after == 0, (u, at_before, at_after)


def test_a_zoom_paced_now_says_where_it_held_its_boxes_from(tmp_path):
    world = _world()
    frames, events = _take(tmp_path, world)
    _with_labels(tmp_path, frames, _labelled(world))
    scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=FPS, pixel_ratio=RATIO)
    [scroll] = events[0]["scrolls"]
    assert scroll["paced"] and scroll["synthesis"] == scroll_pacing.ZOOM_SYNTHESIS, scroll
    # The fixture's toolbar stands over the canvas: held, and held from the nearer keyframe.
    assert scroll.get("overlays") or scroll.get("still_share"), scroll
    assert scroll["held_from"] == scroll_pacing.ZOOM_HELD_FROM
    assert scroll_pacing.misheld_zooms(events) == []
    # The same zoom as an older release paced it (no held_from): named, for the save to say and a repair to redraw.
    before = [{**events[0], "scrolls": [{key: value for key, value in scroll.items() if key != "held_from"}]}]
    [(step, start, end)] = scroll_pacing.misheld_zooms(before)
    assert step == 1 and end > start


def test_only_a_warped_zoom_holding_boxes_without_held_from_is_named():
    base = {"t": [12.3, 14.0], "scroller": "canvas", "paced": True, "fps": 30.0, "synthesis": scroll_pacing.ZOOM_SYNTHESIS}
    misheld = [{"step": 3, "action": "scroll", "scrolls": [{**base, "overlays": 8}]}]
    assert scroll_pacing.misheld_zooms(misheld) == [(3, 12.3, 14.0)]
    fixed = [{"step": 3, "scrolls": [{**base, "overlays": 8, "held_from": scroll_pacing.ZOOM_HELD_FROM}]}]
    nothing_held = [{"step": 3, "scrolls": [dict(base)]}]
    blended = [{"step": 3, "scrolls": [{key: value for key, value in base.items() if key != "synthesis"} | {"overlays": 8}]}]
    window = [{"step": 1, "scrolls": [{**base, "scroller": "window", "overlays": 8}]}]
    for events in (fixed, nothing_held, blended, window, None, ["not an event"]):
        assert scroll_pacing.misheld_zooms(events) == [], events
    # The old cross-dissolve stays blended_zooms' to name: one finding per take, never two.
    assert scroll_pacing.blended_zooms(blended) == [(3, 12.3, 14.0)]


def test_the_report_version_moved():
    from typing import get_args

    from app.services.video_studio import quality

    assert "double_image" in get_args(quality.FindingKind)
    assert quality.QUALITY_REPORT_VERSION >= 35
