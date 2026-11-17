"""A frame drawn within a canvas zoom is scored against the keyframes moved to its instant, not as they stand.

``test_video_studio_canvas_alone_6761_chromium`` scored each frame the pacer drew within a wheel zoom against the two
painted keyframes about it, unmoved. When the throttled screencast painted nothing for two seconds over the wheel, one
pair of keyframes held most of the zoom, and any single picture of the graph at a scale between theirs - the browser's
own, or the pacer's - draws edges of each in separate regions: near the anchor it lines up with both, far from it with
neither. ``double_image_score`` read that as a double image (run 41572162908: 0.60, against ``DOUBLE_IMAGE_SHARE``
0.5). What is proven here, offline, on the synthetic graph of ``test_video_studio_canvas_zoom_6430`` with fixed-size
labels (``test_video_studio_zoom_double_image_7337``):

* the cause: a picture of the graph rendered at the true intermediate zoom scores past ``DOUBLE_IMAGE_SHARE`` against
  the unmoved keyframes of a 2x zoom held by one gap - the score, not the pacer, misreads it;
* :func:`one_picture` moves both keyframes to the frame's instant (the share of the registered zoom that best draws it
  from the nearer one) and tells the nearer keyframe moved from the cross-dissolve of the two: every frame
  ``_draw_zoom_frame`` draws over such a gap reads as one picture;
* the check still catches what it guards: a cross-dissolve of the two keyframes moved to the same instant reads as a
  blend.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.services.video_studio import scroll_pacing
from tests.test_video_studio_canvas_zoom_6430 import ANCHOR, BOX, _world
from tests.test_video_studio_zoom_double_image_7337 import _labelled

#: The instants tried between two keyframes when reading where a frame stands: a fortieth of their zoom apart.
STEPS = 40


def one_picture(frame: np.ndarray, one: np.ndarray, other: np.ndarray, anchors: Sequence[tuple[float, float]]) -> tuple[float, float] | None:
    """How far the frame (grey, the canvas's box) is from the nearer keyframe moved to its instant, and from the
    cross-dissolve of the two moved there, in mean grey levels over what both moved keyframes paint. ``None`` when the
    two keyframes did not zoom or their zoom is not registered (no instant to move them to)."""
    levels = [scroll_pacing.zoom_levels(one), scroll_pacing.zoom_levels(other)]
    still, _boxes = scroll_pacing.standing(levels)
    zoom = scroll_pacing.register_zoom(levels[0], levels[1], anchors, still)
    if zoom is None or zoom == scroll_pacing.IDENTITY:
        return None
    before = Image.fromarray(np.clip(one, 0, 255).astype(np.uint8))
    after = Image.fromarray(np.clip(other, 0, 255).astype(np.uint8))
    shown = np.asarray(frame, dtype=np.float32)
    best: tuple[float, float] | None = None
    for n in range(1, STEPS):
        v = n / STEPS
        ahead, ahead_rows, ahead_columns = scroll_pacing._drawn_under(before, zoom.share(-v))
        back, back_rows, back_columns = scroll_pacing._drawn_under(after, zoom.share(1 - v))
        both = np.minimum.outer(np.minimum(ahead_rows, back_rows), np.minimum(ahead_columns, back_columns)) >= 0.5
        if not both.any():
            continue
        a, b = np.asarray(ahead, dtype=np.float32), np.asarray(back, dtype=np.float32)
        near = a if v < 0.5 else b
        to_near = float(np.abs(shown - near)[both].mean())
        if best is None or to_near < best[0]:
            best = (to_near, float(np.abs(shown - 0.5 * (a + b))[both].mean()))
    return best


def _canvas(world: Image.Image, labels, k: float) -> Image.Image:
    """The graph zoomed *k* about the cursor, its labels drawn at a fixed size next to their nodes (a graph's labels)."""
    cx, cy = ANCHOR
    canvas = world.transform(
        world.size, Image.Transform.AFFINE, (1 / k, 0, cx * (1 - 1 / k), 0, 1 / k, cy * (1 - 1 / k)), resample=Image.Resampling.BILINEAR
    )
    draw = ImageDraw.Draw(canvas)
    for x, y, text in labels:
        draw.text((cx + (x - cx) * k + 14, cy + (y - cy) * k - 6), text, fill=(245, 245, 245))
    return canvas


def _grey(picture: Image.Image) -> np.ndarray:
    return np.asarray(picture.convert("L"), dtype=np.float32)


@pytest.fixture(scope="module")
def gap():
    """A 2x wheel zoom the screencast painted only before and after: one pair of keyframes holds all of it."""
    world = _world()
    labels = _labelled(world)
    scale = 2.0
    before, after = _canvas(world, labels, 1.0), _canvas(world, labels, scale)
    centre = ((BOX[2] - BOX[0] - 1) / 2, (BOX[3] - BOX[1] - 1) / 2)
    levels = [scroll_pacing.zoom_levels(_grey(before)), scroll_pacing.zoom_levels(_grey(after))]
    still, _boxes = scroll_pacing.standing(levels)
    zoom = scroll_pacing.register_zoom(levels[0], levels[1], [ANCHOR, centre], still)
    assert zoom is not None and zoom != scroll_pacing.IDENTITY
    return world, labels, scale, before, after, zoom, [ANCHOR, centre]


def test_one_picture_of_the_zoom_misreads_as_a_double_image_against_the_unmoved_keyframes(gap):
    world, labels, scale, before, after, _zoom, _anchors = gap
    truth = _grey(_canvas(world, labels, math.exp(0.6 * math.log(scale))))
    # The browser's own picture 60 % of the way: one picture, which the old check scored as two.
    assert scroll_pacing.double_image_score(truth, _grey(before), _grey(after)) >= scroll_pacing.DOUBLE_IMAGE_SHARE


@pytest.mark.parametrize("u", [0.1, 0.3, 0.45, 0.55, 0.7, 0.9])
def test_a_frame_drawn_over_a_long_gap_reads_as_one_picture(gap, tmp_path, u):
    *_, before, after, zoom, anchors = gap
    path = tmp_path / f"z{u}.jpg"
    assert scroll_pacing._draw_zoom_frame(before, before, after, zoom, u, (0, 0), None, path)
    with Image.open(path) as opened:
        shown = _grey(opened)
    read = one_picture(shown, _grey(before), _grey(after), anchors)
    assert read is not None
    to_near, to_blend = read
    assert to_near < to_blend, f"the frame at u={u} reads as a blend: {to_near:.2f} vs {to_blend:.2f}"


@pytest.mark.parametrize("u", [0.3, 0.6])
def test_a_cross_dissolve_of_the_two_moved_keyframes_still_reads_as_a_blend(gap, u):
    *_, before, after, zoom, anchors = gap
    ahead = _grey(scroll_pacing._drawn_under(before, zoom.share(-u))[0])
    back = _grey(scroll_pacing._drawn_under(after, zoom.share(1 - u))[0])
    read = one_picture(0.5 * (ahead + back), _grey(before), _grey(after), anchors)
    assert read is not None
    to_near, to_blend = read
    assert to_blend < to_near, f"a cross-dissolve at u={u} reads as one picture: {to_near:.2f} vs {to_blend:.2f}"


def test_keyframes_that_did_not_zoom_leave_nothing_to_move():
    world = _world()
    flat = _grey(world)
    assert one_picture(flat, flat, flat, [ANCHOR]) is None
