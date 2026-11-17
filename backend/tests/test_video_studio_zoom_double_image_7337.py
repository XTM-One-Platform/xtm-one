"""A canvas zoom's in-between frames are one keyframe moved, never two blended.

The pacer drew each frame between two keyframes of a canvas zoomed by the wheel as the keyframe before zoomed on,
cross-dissolved into the keyframe after zoomed back: wherever the zoom does not explain both to the pixel (labels a
graph draws at a fixed size, a re-layout between two paints), every node and label showed twice for the whole span -
the "blinks" of a software-GL take. What is proven here, on the frames of ``test_video_studio_canvas_zoom_6430``:

* :func:`scroll_pacing.double_image_score` reads a cross-dissolve of two keyframes as a double image and either keyframe
  alone as one picture;
* a zoom whose labels keep their size while the graph scales (what OpenCTI's graph draws) is paced into frames that
  never carry two keyframes' edges, each marked ``synthesis: warped``;
* a pair of keyframes whose zoom is not registered holds the nearer keyframe (``held_not_blended``), never a blend;
* a take paced by the old cross-dissolve gets the ``double_image`` save finding naming its step; one paced now does not.
"""

from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw

from app.services.video_studio import scroll_pacing
from tests.test_video_studio_canvas_zoom_6430 import ANCHOR, BOX, FPS, PAINT_FPS, RATIO, _scale, _share, _take, _world

#: Labels drawn next to nodes at a fixed size whatever the zoom, as a graph draws its labels: no single zoom maps one
#: keyframe's labels onto the next's, so a cross-dissolve shows both.
LABELS = 40


def _labelled(world: Image.Image):
    rng = np.random.default_rng(7337)
    width, height = world.size
    return [(float(rng.uniform(80, width - 300)), float(rng.uniform(80, height - 80)), f"T{1000 + i} Ingress {i}") for i in range(LABELS)]


def _with_labels(tmp_path, frames, labels):
    """Each keyframe of the take redrawn with fixed-size labels at the zoomed position of their node."""
    cx, cy = ANCHOR
    for t, name in frames:
        k = _scale(t)
        with Image.open(tmp_path / name) as opened:
            frame = opened.convert("RGB")
        draw = ImageDraw.Draw(frame)
        for x, y, text in labels:
            px, py = cx + (x - cx) * k + BOX[0], cy + (y - cy) * k + BOX[1]
            if BOX[1] + 10 < py < BOX[3] - 30 and BOX[0] < px < BOX[2] - 200:
                draw.text((px + 14, py - 6), text, fill=(245, 245, 245))
        frame.save(tmp_path / name, quality=95)


def _grey(tmp_path, name: str) -> np.ndarray:
    with Image.open(tmp_path / name) as opened:
        return np.asarray(opened.convert("L"), dtype=np.float32)[BOX[1] : BOX[3], BOX[0] : BOX[2]]


def test_the_score_tells_a_cross_dissolve_from_one_picture():
    rng = np.random.default_rng(1)
    one = np.zeros((300, 400), dtype=np.float32)
    other = np.zeros((300, 400), dtype=np.float32)
    for _ in range(60):
        x, y = rng.integers(10, 360), rng.integers(10, 270)
        one[y : y + 12, x : x + 30] = 220
        other[y + 7 : y + 19, x + 9 : x + 39] = 220
    assert scroll_pacing.double_image_score(one, one, other) < 0.2
    assert scroll_pacing.double_image_score(other, one, other) < 0.2
    blend = 0.5 * one + 0.5 * other
    assert scroll_pacing.double_image_score(blend, one, other) > scroll_pacing.DOUBLE_IMAGE_SHARE
    # Two pictures alike leave nothing to tell apart.
    assert scroll_pacing.double_image_score(one, one, one) == 0.0


def test_a_zoom_with_fixed_size_labels_never_draws_two_keyframes_at_once(tmp_path):
    world = _world()
    frames, events = _take(tmp_path, world)
    _with_labels(tmp_path, frames, _labelled(world))
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=FPS, pixel_ratio=RATIO)
    [scroll] = events[0]["scrolls"]
    assert scroll["paced"] and scroll["synthesis"] == scroll_pacing.ZOOM_SYNTHESIS, scroll
    times = [t for t, _name in frames]
    keyframes = {name for _t, name in frames}
    centre = ((BOX[2] - BOX[0] - 1) / 2, (BOX[3] - BOX[1] - 1) / 2)
    checked = 0
    for t, name in paced.frames:
        if not 0.05 < _share(t) < 0.95 or name in keyframes:
            continue
        k = max(i for i, when in enumerate(times) if when <= t)
        a, b = _grey(tmp_path, frames[k][1]), _grey(tmp_path, frames[k + 1][1])
        levels = [scroll_pacing.zoom_levels(a), scroll_pacing.zoom_levels(b)]
        still, _boxes = scroll_pacing.standing(levels)
        zoom = scroll_pacing.register_zoom(levels[0], levels[1], [ANCHOR, centre], still)
        assert zoom is not None
        u = (math.log(_scale(t)) - math.log(_scale(times[k]))) / (math.log(_scale(times[k + 1])) - math.log(_scale(times[k])))
        ahead = np.asarray(scroll_pacing._drawn_under(Image.fromarray(a.astype(np.uint8)), zoom.share(-u))[0], dtype=np.float32)
        back = np.asarray(scroll_pacing._drawn_under(Image.fromarray(b.astype(np.uint8)), zoom.share(1 - u))[0], dtype=np.float32)
        near, far = (back, ahead) if u >= 0.5 else (ahead, back)
        shown = _grey(tmp_path, name)
        inner = (slice(200, -200), slice(300, -300))
        to_near = float(np.abs(shown - near)[inner].mean())
        to_blend = float(np.abs(shown - 0.5 * (near + far))[inner].mean())
        # The frame is the nearer keyframe moved, not the two blended: the labels drawn once.
        assert to_near < to_blend, f"the frame at {t:.3f} s is a blend: {to_near:.2f} vs {to_blend:.2f}"
        assert scroll_pacing.double_image_score(shown, near, far) < scroll_pacing.DOUBLE_IMAGE_SHARE
        checked += 1
        if checked == 1:
            # The old cross-dissolve of the same two keyframes, each moved to the frame's instant, is what it catches.
            assert scroll_pacing.double_image_score(0.5 * (near + far), near, far) > scroll_pacing.DOUBLE_IMAGE_SHARE
        if checked >= 6:
            break
    assert checked >= 3


def test_the_frame_nearer_a_keyframe_is_drawn_from_it(tmp_path):
    before = Image.new("RGB", (400, 300), (10, 10, 10))
    ImageDraw.Draw(before).rectangle([100, 100, 160, 140], fill=(250, 250, 250))
    after = Image.new("RGB", (400, 300), (10, 10, 10))
    ImageDraw.Draw(after).rectangle([300, 40, 340, 60], fill=(250, 0, 0))
    zoom = scroll_pacing.Zoom(1.2, -40.0, -30.0)
    for u, red in ((0.3, False), (0.7, True)):
        path = tmp_path / f"z{u}.jpg"
        assert scroll_pacing._draw_zoom_frame(before, before, after, zoom, u, (0, 0), None, path)
        with Image.open(path) as opened:
            shown = np.asarray(opened.convert("RGB"), dtype=np.int16)
        reds = int(((shown[..., 0] > 150) & (shown[..., 1] < 80)).sum())
        whites = int((shown.min(axis=2) > 150).sum())
        # One keyframe or the other, never both: no faint copy of the other's shape.
        assert (reds > 0) == red and (whites > 0) == (not red), (u, reds, whites)


def test_a_pair_not_registered_holds_the_nearer_keyframe(tmp_path, monkeypatch):
    world = _world()
    frames, events = _take(tmp_path, world)
    calls = {"n": 0}
    real = scroll_pacing.register_zoom

    def flaky(*args, **kwargs):
        calls["n"] += 1
        return None if calls["n"] == 2 else real(*args, **kwargs)

    monkeypatch.setattr(scroll_pacing, "register_zoom", flaky)
    # Nor registered from the canvas's log (#8457): the pair stays unregistered, what this test is about.
    monkeypatch.setattr(scroll_pacing, "implied_zooms", lambda zooms, *_args: (list(zooms), 0))
    monkeypatch.setattr(scroll_pacing, "MAX_HELD", 1.0)
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=FPS, pixel_ratio=RATIO)
    [scroll] = events[0]["scrolls"]
    assert scroll["paced"] and scroll["held"] > 0 and scroll["held_not_blended"] is True
    keyframes = {name for _t, name in frames}
    times = {name: t for t, name in frames}
    for t, name in paced.frames:
        if name in keyframes and 0 < _share(t) < 1:
            # Whatever a frame holds, it is the keyframe nearest it among the two either side.
            assert abs(times[name] - t) <= 0.5 / PAINT_FPS + 1e-6, (t, name)
    assert "held, not blended" in scroll_pacing.scroll_steps_words(events)


def test_a_take_paced_by_the_old_cross_dissolve_is_named_and_a_new_one_is_not():
    old = [{"step": 3, "action": "scroll", "scrolls": [{"t": [12.23, 13.2], "scroller": "canvas", "paced": True, "fps": 30.0, "painted": 8.7}]}]
    assert scroll_pacing.blended_zooms(old) == [(3, 12.23, 13.2)]
    new = [{"step": 3, "action": "scroll", "scrolls": [{**old[0]["scrolls"][0], "synthesis": scroll_pacing.ZOOM_SYNTHESIS}]}]
    assert scroll_pacing.blended_zooms(new) == []
    # A window's scroll, or a zoom kept as painted, is no canvas zoom drawn between keyframes.
    window = [{"step": 1, "scrolls": [{"t": [0, 1], "scroller": "window", "paced": True}]}]
    kept = [{"step": 2, "scrolls": [{"t": [0, 1], "scroller": "canvas", "paced": False}]}]
    assert scroll_pacing.blended_zooms(window) == [] and scroll_pacing.blended_zooms(kept) == []


def test_the_finding_kind_is_declared():
    from typing import get_args

    from app.services.video_studio import quality

    assert "double_image" in get_args(quality.FindingKind)
    assert quality.QUALITY_REPORT_VERSION >= 34
    assert math.isfinite(scroll_pacing.DOUBLE_IMAGE_SHARE)
