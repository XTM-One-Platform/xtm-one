"""The recorder reads every fixed or sticky element of the page where it paints, or says it could not.

``capture._FIXED_BOXES_JS`` walks the document rather than hit-testing a grid: a 10 px fixed bar standing between two
grid rows is listed, so the scroll pacing never drops the real band it paints. Each element is hit-tested on up to five
rows across the slice of it in view (cut to the viewport and, for a sticky one, to its clipping ancestors) and recorded
as the slice that paints: a covered or clipped-away box is not listed, a slice away from its middle is. The point's
stack is read with ``pointer-events`` lifted by a constructed stylesheet, so an overlay that lets clicks through still
covers what lies under it, and the page's own styles are left as they were. More elements or boxes than the walk reads
answers ``null``: the bands stay unchecked, never checked on a partial list.
"""

from __future__ import annotations

import os

import pytest

from app.services.video_studio import capture

pytestmark = pytest.mark.chromium

HEIGHT = 600

PAGE = f"""<!doctype html><html><body style="margin:0;height:3000px">
<div id="thin" style="position:fixed;left:0;right:0;top:{HEIGHT / 24 - 5}px;height:10px;background:#123;z-index:10"></div>
<div style="position:sticky;top:0;height:12px;background:#456"></div>
<div style="position:fixed;display:none;top:200px;height:30px;width:100%">hidden</div>
<div style="position:fixed;top:2000px;height:30px;width:100%">below the fold</div>
</body></html>"""


async def _evaluate(html: str, after: str | None = None):
    """``_FIXED_BOXES_JS`` on *html*, and *after* evaluated once it ran (``(boxes, after)`` then)."""
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page(viewport={"width": 900, "height": HEIGHT})
                await page.set_content(html)
                boxes = await page.evaluate(capture._FIXED_BOXES_JS)
                if after is None:
                    return boxes
                return boxes, await page.evaluate(after)
            finally:
                await browser.close()
    except Exception as exc:
        text = str(exc)
        if "Executable doesn't exist" not in text and "playwright install" not in text.lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")


async def test_a_thin_fixed_bar_between_grid_rows_is_listed_and_hidden_or_off_view_ones_are_not():
    boxes = await _evaluate(PAGE)
    assert boxes is not None
    thin = [box for box in boxes if box[4] == "fixed" and abs(box[3] - 10) < 0.5]
    assert len(thin) == 1 and abs(thin[0][1] - (HEIGHT / 24 - 5)) < 0.5, boxes
    assert any(box[4] == "sticky" for box in boxes), boxes
    assert len(boxes) == 2, boxes


async def test_a_fixed_or_sticky_box_that_does_not_paint_there_is_never_listed():
    page = """<!doctype html><html><body style="margin:0;height:3000px">
<div id="covered" style="position:fixed;left:0;right:0;top:100px;height:30px;background:#123"></div>
<div style="position:absolute;left:0;right:0;top:90px;height:60px;background:#fff;z-index:5"></div>
<div style="position:relative;overflow:hidden;height:20px;margin-top:300px">
  <div style="height:200px"></div>
  <div id="clipped" style="position:sticky;top:0;height:25px;background:#456"></div>
</div>
<div id="through" style="position:fixed;left:0;right:0;top:560px;height:12px;pointer-events:none;background:#789"></div>
</body></html>"""
    boxes = await _evaluate(page)
    assert boxes is not None
    # The covered and the clipped ones paint nothing there; the pointer-events:none bar, probed, paints on top.
    assert sorted(round(box[1]) for box in boxes) == [560], boxes


async def test_a_bar_clipped_to_a_slice_away_from_its_middle_is_listed_as_the_slice_that_paints():
    page = """<!doctype html><html><body style="margin:0;height:3000px">
<div style="position:fixed;left:0;right:0;top:-30px;height:60px;background:#123"></div>
<div style="position:relative;overflow:hidden;height:20px;margin-top:300px">
  <div style="position:sticky;top:0;height:100px;background:#456"></div>
</div>
</body></html>"""
    boxes = await _evaluate(page)
    assert boxes is not None and len(boxes) == 2, boxes
    half_off, clipped = sorted(boxes, key=lambda box: box[1])
    assert half_off[4] == "fixed" and abs(half_off[1]) < 0.5 and abs(half_off[3] - 30) < 0.5, boxes
    assert clipped[4] == "sticky" and abs(clipped[1] - 300) < 0.5 and abs(clipped[3] - 20) < 0.5, boxes


async def test_a_pointer_events_none_bar_is_hit_tested_and_its_style_left_as_it_was():
    style = "position:fixed;left:0;right:0;top:0;height:40px;pointer-events:none;background:#123"
    page = f"""<!doctype html><html><body style="margin:0;height:3000px">
<div id="ghost" style="{style}"></div>
<div id="modal" style="position:fixed;left:0;right:0;top:0;height:60px;background:#fff;z-index:9"></div>
</body></html>"""
    boxes, inline = await _evaluate(page, "() => document.getElementById('ghost').getAttribute('style')")
    # Covered by the modal, the pointer-events:none bar is not listed; the modal is.
    assert boxes is not None and len(boxes) == 1 and abs(boxes[0][3] - 60) < 0.5, boxes
    assert inline == style


async def test_an_opaque_layer_hit_testing_passes_through_still_covers_the_bar_under_it():
    page = """<!doctype html><html><body style="margin:0;height:3000px">
<div style="position:fixed;left:0;right:0;top:0;height:60px;background:#123"></div>
<div style="position:fixed;inset:0;pointer-events:none;background:#000;z-index:9"></div>
</body></html>"""
    boxes = await _evaluate(page)
    # The overlay lets clicks through but paints over the bar: only the overlay is listed.
    assert boxes is not None and len(boxes) == 1 and abs(boxes[0][3] - HEIGHT) < 0.5, boxes


async def test_a_transparent_wrapper_over_a_bar_does_not_hide_it():
    page = """<!doctype html><html><body style="margin:0;height:3000px">
<div style="position:fixed;left:0;right:0;top:0;height:60px;background:#123"></div>
<div style="position:fixed;inset:0;pointer-events:none;z-index:9"></div>
</body></html>"""
    boxes = await _evaluate(page)
    # A portal root painting nothing passes: the bar is listed, the wrapper (painting nothing of its own) is not.
    assert boxes is not None and len(boxes) == 1, boxes
    assert abs(boxes[0][1]) < 0.5 and abs(boxes[0][3] - 60) < 0.5, boxes


async def test_more_fixed_boxes_than_it_lists_answers_unknown_never_a_partial_list():
    many = "".join(
        f'<div style="position:fixed;top:{i * 5}px;left:0;width:100%;height:4px;background:#123"></div>'
        for i in range(capture._FIXED_BOX_LIMIT + 1)
    )
    assert await _evaluate(f"<!doctype html><html><body>{many}</body></html>") is None
