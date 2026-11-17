"""A box over a zoomed canvas that moves with the zoom is drawn with the canvas, never held as painted.

Every box the recorder reads over a canvas (``over``: an element above it, smaller than half of it) is held
as a keyframe painted it while the canvas around it is drawn again under the zoom. On a graph, many of those boxes are
the labels the page lays over its nodes: they move and scale with the zoom. Held, their inside reads unwarped in a
warped canvas, and a label crossing a box's edge reads split (``CVE-2025-26 399``), one inside it offset - and the take
said ``held_from: nearer``, so the save stayed silent. What is proven here:

* :func:`scroll_pacing.riding_boxes` names a box whose pixels the zoom carries, and not one standing still or changing
  in place (a toolbar a hover lights);
* a wheel zoom paced over a canvas with a label box riding it and a toolbar standing still draws the label box as the
  canvas at its instant on every frame, holds the toolbar, and says ``riding``, ``overlays`` and
  ``held_from: standing``;
* a warped zoom paced with ``held_from: nearer`` is named by :func:`scroll_pacing.misheld_zooms`, one paced now
  is not;
* a repair says when its keyframes were read off an MP4 by an earlier repair, at any depth of the chain.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from app.services.video_studio import scroll_pacing, take_repair
from tests.test_video_studio_canvas_zoom_6430 import (
    BOX,
    END,
    FPS,
    RATIO,
    START,
    TOOLBAR,
    TOOLBAR_CSS,
    _take,
    _view,
    _world,
)

#: A label the graph lays over a node, far enough from the cursor for the zoom to carry it: CSS px of the viewport,
#: and device px of the canvas's box.
LABEL_CSS = [1180.0, 640.0, 260.0, 80.0]
LABEL = (round(LABEL_CSS[0] * RATIO - BOX[0]), round(LABEL_CSS[1] * RATIO - BOX[1]), round(LABEL_CSS[2] * RATIO), round(LABEL_CSS[3] * RATIO))


def _labelled_world() -> Image.Image:
    """The graph with a label drawn into it where the recorder reads a box over the canvas: zoomed with the graph."""
    world = _world().copy()
    draw = ImageDraw.Draw(world)
    x, y, w, h = LABEL
    draw.rectangle([x, y, x + w, y + h], fill=(236, 238, 244))
    for i in range(9):
        draw.rectangle([x + 14 + i * 42, y + 30, x + 40 + i * 42, y + 78], fill=(30, 34, 52))
    return world


def _decoded(path) -> np.ndarray:
    with Image.open(path) as opened:
        return np.asarray(opened.convert("RGB")).astype(np.int16)


def test_riding_boxes_tells_a_box_the_zoom_carries_from_one_standing_still():
    rng = np.random.default_rng(7455)
    first = rng.uniform(0, 255, (300, 400)).astype(np.float32)
    zoom = scroll_pacing.Zoom(1.2, -40.0, -30.0)
    # The second keyframe: the first zoomed, except a toolbar that stands still and a button a hover lit in it.
    drawn = scroll_pacing._zoomed(first, zoom, first.shape)
    assert drawn is not None
    second = first.copy()
    out, rows, cols = drawn
    second[rows, cols] = out
    toolbar = (10.0, 10.0, 120.0, 30.0)
    second[10:40, 10:130] = first[10:40, 10:130]
    second[15:35, 20:60] = 250.0
    label = (220.0, 180.0, 100.0, 40.0)
    levels = [[(1, first)], [(1, second)]]
    assert scroll_pacing.riding_boxes(levels, [toolbar, label], [zoom]) == {1}
    # Not registered, or not moved: nothing rides.
    assert scroll_pacing.riding_boxes(levels, [toolbar, label], [None]) == set()
    assert scroll_pacing.riding_boxes(levels, [toolbar, label], [scroll_pacing.IDENTITY]) == set()


def test_a_label_box_riding_a_zoomed_canvas_is_drawn_with_it_and_the_toolbar_held(tmp_path):
    world = _labelled_world()
    frames, events = _take(tmp_path, world)
    events[0]["scrolls"][0]["surface"]["over"] = [TOOLBAR_CSS, LABEL_CSS]
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=FPS, pixel_ratio=RATIO)
    [scroll] = events[0]["scrolls"]
    assert scroll["paced"] and scroll["synthesis"] == scroll_pacing.ZOOM_SYNTHESIS, scroll
    assert scroll["riding"] == 1 and scroll["overlays"] == 1, scroll
    assert scroll["held_from"] == scroll_pacing.ZOOM_HELD_FROM == "standing"
    assert scroll_pacing.misheld_zooms(events) == []
    lx, ly, lw, lh = LABEL
    fx0, fy0 = lx + BOX[0], ly + BOX[1]
    x0, y0, x1, y1 = TOOLBAR
    during = [(t, name) for t, name in paced.frames if START <= t < END]
    assert during
    for t, name in during[::3]:
        shown = _decoded(tmp_path / name)
        expected = _view(world, t).astype(np.int16)
        # Where the label box was read: the canvas as zoomed at its instant, the label drawn once and whole - never the
        # keyframe's pixels held unwarped inside a warped canvas.
        region = (slice(fy0, fy0 + lh), slice(fx0, fx0 + lw))
        error = float(np.abs(shown[region] - expected[region]).mean())
        held = next(frame for when, frame in reversed(frames) if when <= t)
        stepped = float(np.abs(_decoded(tmp_path / held)[region] - expected[region]).mean())
        assert error < 4.0 and error <= stepped + 0.5, f"the label box at {t:.3f} s: {error:.2f} (held: {stepped:.2f})"
        # The toolbar stands still, held.
        assert np.abs(shown[y0:y1, x0:x1] - expected[y0:y1, x0:x1]).mean() < 3.0, f"the toolbar at {t:.3f} s"


def test_a_zoom_paced_by_7364_is_named_one_paced_now_is_not():
    base = {"t": [12.3, 14.0], "scroller": "canvas", "paced": True, "fps": 30.0, "synthesis": scroll_pacing.ZOOM_SYNTHESIS, "overlays": 8}
    by_7364 = [{"step": 3, "scrolls": [{**base, "held_from": "nearer"}]}]
    assert scroll_pacing.misheld_zooms(by_7364) == [(3, 12.3, 14.0)]
    now = [{"step": 3, "scrolls": [{**base, "overlays": 1, "riding": 7, "held_from": scroll_pacing.ZOOM_HELD_FROM}]}]
    assert scroll_pacing.misheld_zooms(now) == []


def test_a_repair_says_when_its_keyframes_came_off_an_mp4():
    mp4, keyframes = take_repair.FROM_MP4, take_repair.FROM_KEYFRAMES
    recorded = {"fps": 30}
    once = {take_repair.REPAIRED_KEY: {"of": "a", "from": mp4}}
    twice = {take_repair.REPAIRED_KEY: {"of": "b", "from": keyframes, "origin": mp4}}
    exact = {take_repair.REPAIRED_KEY: {"of": "c", "from": keyframes, "origin": keyframes}}
    assert take_repair.repair_origin(keyframes, recorded) == keyframes
    assert take_repair.repair_origin(keyframes, once) == mp4
    assert take_repair.repair_origin(keyframes, twice) == mp4
    assert take_repair.repair_origin(keyframes, exact) == keyframes
    assert take_repair.repair_origin(mp4, recorded) == mp4
    left = take_repair.residual(keyframes, recorded=recorded, notes=[], origin=mp4)
    assert len(left) == 1 and "earlier repair" in left[0]
    assert take_repair.residual(keyframes, recorded=recorded, notes=[], origin=keyframes) == []
    said = take_repair.repair_words({take_repair.REPAIRED_KEY: {"of": "d", "from": keyframes, "origin": mp4, "spans": [], "residual": left}})
    assert "read off an MP4 by an earlier repair (approximate)" in said
    assert "(exact)" in take_repair.repair_words({take_repair.REPAIRED_KEY: {"of": "e", "from": keyframes, "spans": []}})


def test_the_report_version_moved():
    from app.services.video_studio import quality

    assert quality.QUALITY_REPORT_VERSION >= 36
