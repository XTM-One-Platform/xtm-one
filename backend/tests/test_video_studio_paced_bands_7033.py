"""A paced scroll keeps a sticky band as painted, and a paced canvas zoom never zooms what the page draws over it.

A recording's scrolls and canvas zooms are drawn again at its rate from a few keyframes a second. Two
artifacts reached rendered videos from that synthesis:

* a ``position: sticky`` band inside the scroller - an entity header pinned under the fixed top bar, a table's header
  row inside a scroll container - was translated with the rows under it between two keyframes, drawn twice or
  half-drawn over the first content row, then snapped back on the next keyframe: it blinked. ``static_bands`` keeps
  only what *every* keyframe paints alike at the scroller's edges; a band that first scrolls, then pins, or sits below
  another band, is not that;
* a box the page draws over a zoomed canvas (a toolbar, a legend) that changed on one keyframe (a hover, a count) was
  zoomed and cross-faded with the canvas: enlarged, doubled, cut at the frame's edges.

What is proven here, on frames made the way the screencast makes them (JPEG at its quality):

* a list under a fixed top bar and a sticky sub-header that scrolls 300 CSS px with the content, then pins, recorded
  at 4 fps and paced at 30 fps: every frame keeps the top bar, and the sub-header once pinned, as the keyframe pinning
  it painted them, and never draws the sub-header anywhere it was not painted; the content under it is the page at its
  instant; the take says the band it held;
* a canvas zoomed under a toolbar that changes on one keyframe: the toolbar is never scaled.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

from app.services.video_studio import scroll_pacing
from tests.test_video_studio_canvas_zoom_6430 import BOX, END, START, TOOLBAR, _take, _view, _world

RATIO = 2
VIEW = (800, 450)
FRAME = (VIEW[0] * RATIO, VIEW[1] * RATIO)
PAGE_CSS = 2400
TOP_BAR_CSS = 48
#: The sticky sub-header: in the page 300 CSS px below the top bar, 56 CSS px tall, pinned under the top bar.
SUB_AT_CSS, SUB_CSS = 300, 56
#: The scroll: at rest until 1.0 s, 900 CSS px at 450 px/s, at rest from 3.0 s; logged from 0.9 s to 3.2 s.
S_START, S_END, DISTANCE = 1.0, 3.0, 900.0
LOG_FROM, LOG_TO = 0.9, 3.2
PAINT_FPS = 4.0


def _offset(t: float) -> float:
    return min(max(t - S_START, 0.0), S_END - S_START) * DISTANCE / (S_END - S_START)


def _page() -> np.ndarray:
    """The page's content at pixel ratio 2: rows of text-like marks, every band of rows unlike its neighbours."""
    height, width = PAGE_CSS * RATIO, FRAME[0]
    y = np.arange(height)[:, None]
    x = np.arange(width)[None, :]
    marks = (((y // 6) * 37 + (x // 48) * 91 + (y // 90) * 13) % 180).astype(np.uint8) + 40
    return np.repeat(marks[:, :, None], 3, axis=2)


def _sub_header() -> np.ndarray:
    band = np.full((SUB_CSS * RATIO, FRAME[0], 3), (245, 246, 250), dtype=np.uint8)
    band[30:62, 60:700] = (40, 44, 62)
    for i in range(8):
        band[76:100, 60 + i * 180 : 200 + i * 180] = (90, 120, 200)
    return band


def _sub_row(offset_css: float) -> int:
    """The screen row (device px) the sub-header's top stands at: with the content, then pinned under the top bar."""
    return round(max(TOP_BAR_CSS + SUB_AT_CSS - offset_css, TOP_BAR_CSS) * RATIO)


def _view_list(page: np.ndarray, offset_css: float) -> np.ndarray:
    top = round(offset_css * RATIO)
    frame = page[top : top + FRAME[1]].copy()
    row = _sub_row(offset_css)
    band = _sub_header()
    frame[row : row + band.shape[0]] = band[: FRAME[1] - row]
    frame[: TOP_BAR_CSS * RATIO] = (24, 30, 48)
    frame[24:72, 40:600] = (230, 232, 240)
    return frame


def _decoded(path) -> np.ndarray:
    with Image.open(path) as opened:
        return np.asarray(opened.convert("RGB")).astype(np.int16)


def _list_take(tmp_path, page: np.ndarray) -> tuple[list[tuple[float, str]], list[dict]]:
    frames = []
    t, k = 0.05, 0
    while t < 4.0:
        name = f"f{k:06d}.jpg"
        Image.fromarray(_view_list(page, _offset(t))).save(tmp_path / name, quality=95)
        frames.append((round(t, 4), name))
        t += 1 / PAINT_FPS
        k += 1
    samples = []
    s = LOG_FROM
    while s <= LOG_TO + 1e-9:
        samples.append([round(s, 4), _offset(s), 0, []])
        s += 1 / scroll_pacing.SCROLL_SAMPLE_HZ
    event = {"step": 0, "action": "scroll", "ok": True, "t0": S_START, "t1": S_END, "scrolls": [{"t": [LOG_FROM, LOG_TO], "samples": samples}]}
    return frames, [event]


def test_a_sticky_sub_header_is_held_as_painted_and_never_translated_with_the_content(tmp_path):
    page = _page()
    frames, events = _list_take(tmp_path, page)
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=30, pixel_ratio=RATIO)
    [scroll] = events[0]["scrolls"]
    assert scroll["paced"] and scroll["fps"] == 30.0, scroll
    bar = TOP_BAR_CSS * RATIO
    sub = SUB_CSS * RATIO
    # The take says the band it held: the sub-header, pinned under the top bar.
    assert any(abs(top - TOP_BAR_CSS) <= 2 and abs(height - SUB_CSS) <= 4 for top, height in scroll["pinned"]), scroll["pinned"]
    band = _sub_header().astype(np.int16)
    during = [(t, name) for t, name in paced.frames if S_START <= t < S_END]
    assert len(during) == 60
    pinned_from = SUB_AT_CSS
    for t, name in during:
        shown = _decoded(tmp_path / name)
        offset = _offset(t)
        # The fixed top bar stands still on every frame.
        assert np.abs(shown[:bar] - _view_list(page, offset).astype(np.int16)[:bar]).mean() < 3.0, f"the top bar at {t:.3f} s"
        if offset >= pinned_from + 40:
            # Pinned: the sub-header under the top bar as painted, the content under it the page at its instant.
            assert np.abs(shown[bar : bar + sub] - band).mean() < 4.0, f"the sub-header at {t:.3f} s"
            expected = _view_list(page, offset).astype(np.int16)
            error = min(
                float(np.abs(shown[bar + sub :] - _view_list(page, offset + nudge / RATIO).astype(np.int16)[bar + sub :]).mean())
                for nudge in (-2, -1, 0, 1, 2)
            )
            assert error < 6.0, f"the content at {t:.3f} s: {error:.2f}"
            # Never a second copy of the band riding the content under it (the blink).
            below = shown[bar + sub :, :, :]
            rows_like_band = np.abs(below - band[40:41]).mean(axis=(1, 2)) < 4.0
            assert rows_like_band.sum() < 8, f"a copy of the sub-header at {t:.3f} s"
            assert expected.shape == shown.shape


def test_a_box_over_a_zoomed_canvas_is_never_zoomed_even_where_it_changed(tmp_path):
    world = _world()
    frames, events = _take(tmp_path, world)
    # The toolbar changes on one keyframe of the zoom (a hover lit one of its buttons): no longer alike on all.
    x0, y0, x1, y1 = TOOLBAR
    middle = next(name for t, name in frames if START + 0.4 < t < END - 0.4)
    with Image.open(tmp_path / middle) as opened:
        lit = np.asarray(opened.convert("RGB")).copy()
    lit[y0 + 24 : y0 + 54, x0 + 20 : x0 + 260] = (255, 200, 40)
    Image.fromarray(lit).save(tmp_path / middle, quality=95)
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=60, pixel_ratio=1.5)
    [scroll] = events[0]["scrolls"]
    assert scroll["paced"] and scroll["overlays"] == 1, scroll
    for t, name in [(t, name) for t, name in paced.frames if START <= t < END][::3]:
        shown = _decoded(tmp_path / name)
        # The toolbar as the NEARER keyframe painted it, the one the canvas under it is drawn from, to its
        # pixel: never scaled, never a blend of two scales.
        index = max(i for i, (when, _) in enumerate(frames) if when <= t)
        held = frames[index][1]
        if index + 1 < len(frames):
            (t_before, _), (t_after, after_name) = frames[index], frames[index + 1]
            if t_after > t_before and (t - t_before) / (t_after - t_before) >= 0.5:
                held = after_name
        nearer = _decoded(tmp_path / held)
        assert np.abs(shown[y0:y1, x0:x1] - nearer[y0:y1, x0:x1]).mean() < 2.0, f"the toolbar at {t:.3f} s"
        assert np.abs(shown[: BOX[1]] - _view(world, t).astype(np.int16)[: BOX[1]]).mean() < 3.0, f"the header at {t:.3f} s"
