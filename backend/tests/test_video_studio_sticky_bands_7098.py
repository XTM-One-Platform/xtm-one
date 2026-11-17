"""A paced scroll pins only a real sticky or fixed band, never paints one blank, and leaves the settled page as painted.

The scroll pacing held as painted any run of rows two neighbouring keyframes painted alike at the same screen rows.
Live takes showed two defects:

* false bands: a heading a slow paint had not moved yet, a table row, a whole scroller painted twice the same were held
  as "sticky bands" - drawn twice, cut in two, still painted after the scroll settled;
* white bands: a band held from a keyframe that repainted it blank drew a white strip across a dark page.

What is proven here, on synthetic keyframes:

* a band is pinned only when the content moved past it, it standing still, on two pairs of keyframes (a heading one
  lagging paint left in place is refused), never over 35% of the scroller (a whole scroller painted twice is refused),
  and, when the recorder read them, only on the page's own ``position: fixed`` / ``position: sticky`` element; every
  refused candidate is said under ``rejected`` with its reason;
* a page with one fixed 94 px bar over scrolling content answers exactly that one band, on its fixed element;
* a keyframe that repainted the band blank never paints it: the band is drawn from a keyframe that painted it;
* once the scroll settled, the frames are the browser's own keyframes, nothing composited over them, and a band the
  page repainted on settling is said (``band_repaint``).
"""

from __future__ import annotations

import numpy as np
from PIL import Image

from app.services.video_studio import scroll_pacing
from tests.test_video_studio_paced_bands_7033 import (
    FRAME,
    LOG_FROM,
    LOG_TO,
    PAINT_FPS,
    RATIO,
    S_END,
    SUB_CSS,
    TOP_BAR_CSS,
    _decoded,
    _list_take,
    _offset,
    _page,
    _sub_header,
    _view_list,
)

# ── pinned_bands and on_the_page, on grey keyframes ──────────────────────

HEIGHT, WIDTH, SHIFT = 600, 400, 40


def _content(rows: int = 3000) -> np.ndarray:
    y = np.arange(rows)[:, None]
    x = np.arange(WIDTH)[None, :]
    return ((((y // 6) * 37 + (x // 48) * 91 + (y // 90) * 13) % 180) + 40).astype(np.float32)


def _keyframes(count: int = 5) -> tuple[list[np.ndarray], list[float]]:
    page = _content()
    offsets = [float(k * SHIFT) for k in range(count)]
    return [page[int(o) : int(o) + HEIGHT].copy() for o in offsets], offsets


def _bar() -> np.ndarray:
    bar = np.full((60, WIDTH), 30.0, dtype=np.float32)
    bar[20:40, 20:300:30] = 220.0
    return bar


def test_a_band_standing_still_while_the_content_moves_is_pinned():
    moving, offsets = _keyframes()
    for grey in moving:
        grey[:60] = _bar()
    rejected: list = []
    bands = scroll_pacing.pinned_bands(moving, offsets, rejected)
    for mask in bands:
        assert scroll_pacing._band_runs(mask) == [(0, 60)]
    assert not rejected


def test_a_heading_one_lagging_paint_left_in_place_is_never_a_band():
    moving, offsets = _keyframes()
    # The paint of keyframe 2 lagged over a heading: its rows are keyframe 1's, the content around it moved.
    moving[2][200:260] = moving[1][200:260]
    rejected: list = []
    bands = scroll_pacing.pinned_bands(moving, offsets, rejected)
    assert not any(mask.any() for mask in bands)
    assert any("one pair" in why for _a, _b, why in rejected), rejected


def test_a_whole_scroller_painted_twice_alike_is_never_a_band():
    moving, offsets = _keyframes()
    moving[2] = moving[1].copy()
    rejected: list = []
    bands = scroll_pacing.pinned_bands(moving, offsets, rejected)
    assert not any(mask.any() for mask in bands)
    assert any("% of its scroller" in why for _a, _b, why in rejected), rejected


def test_a_band_is_kept_only_on_the_pages_fixed_or_sticky_element():
    moving, offsets = _keyframes()
    for grey in moving:
        grey[:60] = _bar()
    kwargs = {"ratio": 1.0, "top": 0, "left": 0, "right": WIDTH}
    kept = scroll_pacing.pinned_bands(moving, offsets)
    rejected: list = []
    scroll_pacing.on_the_page(kept, [(0.0, 0.0, 400.0, 58.0, "fixed")], rejected=rejected, **kwargs)
    assert all(mask[:60].all() for mask in kept) and not rejected
    dropped = scroll_pacing.pinned_bands(moving, offsets)
    scroll_pacing.on_the_page(dropped, [(0.0, 300.0, 400.0, 40.0, "sticky")], rejected=rejected, **kwargs)
    assert not any(mask.any() for mask in dropped)
    assert rejected and all("no fixed or sticky element" in why for _a, _b, why in rejected)
    unread = scroll_pacing.pinned_bands(moving, offsets)
    scroll_pacing.on_the_page(unread, None, rejected=[], **kwargs)
    assert all(mask[:60].all() for mask in unread)


def test_a_narrow_fixed_element_never_vouches_for_a_full_width_band():
    moving, offsets = _keyframes()
    for grey in moving:
        grey[:60] = _bar()
    kwargs = {"ratio": 1.0, "top": 0, "left": 0, "right": WIDTH}
    # A 40 px floating button over the bar's rows: the band is copied across the whole width, the button vouches for 10%.
    narrow = scroll_pacing.pinned_bands(moving, offsets)
    rejected: list = []
    scroll_pacing.on_the_page(narrow, [(340.0, 4.0, 40.0, 52.0, "fixed")], rejected=rejected, **kwargs)
    assert not any(mask.any() for mask in narrow)
    assert rejected and all("spans it" in why for _a, _b, why in rejected)
    # A full-width bar narrowed by a 15 px scrollbar still vouches for it (90% of the width).
    wide = scroll_pacing.pinned_bands(moving, offsets)
    rejected = []
    scroll_pacing.on_the_page(wide, [(0.0, 0.0, WIDTH - 15.0, 60.0, "fixed")], rejected=rejected, **kwargs)
    assert all(mask[:60].all() for mask in wide) and not rejected


def test_a_thin_fixed_bar_is_vouched_for_however_thin():
    moving, offsets = _keyframes()
    for grey in moving:
        grey[40:60] = 30.0
        grey[46:54, 20:300:30] = 220.0
    kwargs = {"ratio": 1.0, "top": 0, "left": 0, "right": WIDTH}
    bands = scroll_pacing.pinned_bands(moving, offsets)
    assert all(mask[40:60].all() for mask in bands)
    rejected: list = []
    scroll_pacing.on_the_page(bands, [(0.0, 40.0, float(WIDTH), 20.0, "fixed")], rejected=rejected, **kwargs)
    assert all(mask[40:60].all() for mask in bands) and not rejected


# ── pace, on screencast-like JPEG keyframes ──────────────────────────────

BAR_CSS = 94


def _view_bar(page: np.ndarray, offset_css: float, *, shade: int = 0) -> np.ndarray:
    """The page under a fixed 94 CSS px app bar; *shade*: the bar repainted a little lighter (a badge, a hover)."""
    top = round(offset_css * RATIO)
    frame = page[top : top + FRAME[1]].copy()
    bar = BAR_CSS * RATIO
    frame[:bar] = (20 + shade, 26 + shade, 44 + shade)
    for i in range(6):
        frame[60:120, 40 + i * 220 : 180 + i * 220] = (220, 224, 236)
    return frame


def _take(tmp_path, view, *, fixed=None):
    frames, t, k = [], 0.05, 0
    while t < 4.0:
        name = f"f{k:06d}.jpg"
        Image.fromarray(view(_offset(t), t)).save(tmp_path / name, quality=95)
        frames.append((round(t, 4), name))
        t += 1 / PAINT_FPS
        k += 1
    samples, s = [], LOG_FROM
    while s <= LOG_TO + 1e-9:
        samples.append([round(s, 4), _offset(s), 0, []])
        s += 1 / scroll_pacing.SCROLL_SAMPLE_HZ
    raw: dict = {"t": [LOG_FROM, LOG_TO], "samples": samples}
    if fixed is not None:
        raw["fixed"] = fixed
    return frames, [{"step": 0, "action": "scroll", "ok": True, "t0": 1.0, "t1": S_END, "scrolls": [raw]}]


def test_one_fixed_app_bar_over_scrolling_content_is_exactly_one_band(tmp_path):
    page = _page()
    # The bar is repainted lighter once the scroll settled: no keyframe-wide still band strips it.
    frames, events = _take(
        tmp_path,
        lambda offset, t: _view_bar(page, offset, shade=12 if t > 3.2 else 0),
        fixed=[[0, 0, 800, BAR_CSS, "fixed"], [0, 0, 800, BAR_CSS, "fixed"]],
    )
    scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=30, pixel_ratio=RATIO)
    [scroll] = events[0]["scrolls"]
    assert scroll["paced"], scroll
    assert len(scroll["pinned"]) == 1, scroll["pinned"]
    [[top, height]] = scroll["pinned"]
    assert top <= 2 and abs(height - BAR_CSS) <= 3, scroll["pinned"]
    assert scroll["pinned_on"][0][0] == "fixed", scroll["pinned_on"]


def test_a_keyframe_repainting_the_band_blank_never_paints_it_white(tmp_path):
    page = _page()
    frames, events = _list_take(tmp_path, page)
    bar, sub = TOP_BAR_CSS * RATIO, SUB_CSS * RATIO
    # Keyframe 8 (2.05 s, the sub-header pinned) repainted the sub-header blank white.
    blanked = _view_list(page, _offset(frames[8][0]))
    blanked[bar : bar + sub] = 255
    Image.fromarray(blanked).save(tmp_path / frames[8][1], quality=95)
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=30, pixel_ratio=RATIO)
    [scroll] = events[0]["scrolls"]
    assert scroll["paced"], scroll
    band = _sub_header().astype(np.int16)
    checked = 0
    for t, name in paced.frames:
        if not 1.0 + 340 / 450 <= t < S_END or not name.startswith("p"):
            continue
        shown = _decoded(tmp_path / name)
        held = shown[bar : bar + sub]
        assert np.abs(held - band).mean() < 4.0, f"the sub-header at {t:.3f} s"
        # Its title rows are never a white strip.
        assert held[30:62].mean() < 200, f"a white strip at {t:.3f} s"
        checked += 1
    assert checked > 20


def test_no_band_is_composited_once_the_scroll_settled(tmp_path):
    page = _page()

    def view(offset: float, t: float) -> np.ndarray:
        frame = _view_list(page, offset)
        if t > S_END:
            # Settled, the page repaints its sub-header (a hover, a count): a gradient, never a flat strip.
            bar, sub = TOP_BAR_CSS * RATIO, SUB_CSS * RATIO
            ramp = np.linspace(110, 190, FRAME[0]).astype(np.uint8)
            frame[bar : bar + sub] = ramp[None, :, None]
        return frame

    frames, events = _take(tmp_path, view)
    paced = scroll_pacing.pace(frames, events, workdir=str(tmp_path), fps=30, pixel_ratio=RATIO)
    [scroll] = events[0]["scrolls"]
    assert scroll["paced"], scroll
    keyframes = [(t, name) for t, name in frames]
    settled = [(t, name) for t, name in keyframes if t > S_END]
    first_settled = settled[0][0]
    checked = 0
    for t, name in paced.frames:
        if t < first_settled or not name.startswith("p"):
            continue
        # The keyframe painted at or before it, as the browser painted it.
        source = max((kt, kn) for kt, kn in keyframes if kt <= t + 1e-6)
        assert np.abs(_decoded(tmp_path / name) - _decoded(tmp_path / source[1])).mean() < 1.5, f"a band composited at {t:.3f} s"
        checked += 1
    assert checked >= 3
    # The band held during the scroll is unlike what the page painted once settled: the take says so.
    assert scroll.get("band_repaint"), scroll
    assert "band repaint" in scroll_pacing.scroll_steps_words(events)
