"""The editable PPTX of a ``positioning_map`` (#11601), in Chromium on the
real stylesheet: the axes are native shapes, every name, quadrant label, axis
title and end label one text box, each arrowhead one picture captured alone,
and every mark sits where the page drew it. Fictional vendors only."""

from __future__ import annotations

import pytest

from tests.test_presentation_export import _deck_stylesheet

# The block as `blocks/PositioningMap.tsx` renders it once its plot is
# measured (two items, the labels placed by the layout), in one column.
_MAP_SLIDE = """
<div class="deck-root">
  <div class="deck-static-slide" data-aspect="16:9" style="width:1920px;height:1080px;background:#0a0a0a">
    <div class="deck-slide" style="width:900px;height:900px;display:flex">
      <figure class="deck-positioning-map" data-size="md" data-axes="center" data-map-overlaps="0">
        <div class="deck-map-body">
          <div class="deck-map-frame">
            <div class="deck-map-y-title"><span>Openness</span></div>
            <div class="deck-map-plot">
              <div class="deck-map-quadrant" data-corner="top_right"><span class="deck-map-quadrant-label">Open platform</span></div>
              <div class="deck-map-axis" data-axis="x">
                <svg class="deck-map-arrow" data-direction="right" data-export-alone="true" viewBox="0 0 12 12"><path d="M1 1 L11 6 L1 11 Z"/></svg>
              </div>
              <div class="deck-map-axis" data-axis="y">
                <svg class="deck-map-arrow" data-direction="up" data-export-alone="true" viewBox="0 0 12 12"><path d="M1 11 L6 1 L11 11 Z"/></svg>
              </div>
              <span class="deck-map-end" data-axis="x" data-end="low">Single stage</span>
              <span class="deck-map-end" data-axis="x" data-end="high">Whole loop</span>
              <div class="deck-map-item" data-emphasis="highlight">
                <div class="deck-map-mark" style="--map-mark:60px;left:600px;top:80px"><span class="deck-map-dot"></span></div>
                <div class="deck-map-label" data-side="below" style="left:570px;top:116px"><span class="deck-map-name">Acme</span></div>
              </div>
              <div class="deck-map-item" data-emphasis="normal">
                <div class="deck-map-mark" style="--map-mark:40px;left:540px;top:150px"><span class="deck-map-dot"></span></div>
                <div class="deck-map-label" data-side="left" style="left:440px;top:140px"><span class="deck-map-name">Northwind</span></div>
              </div>
            </div>
            <div class="deck-map-x-title">Coverage</div>
          </div>
        </div>
      </figure>
    </div>
  </div>
</div>
"""


def test_a_positioning_map_reaches_the_editable_pptx_as_shapes_and_text():
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - the export dependency is always installed
        pytest.skip("playwright is not installed")

    from app.services.presentation_export import EXTRACT_PRIMITIVES_JS

    css = _deck_stylesheet()
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except PlaywrightError as exc:
            pytest.skip(f"no Chromium for Playwright here: {exc}")
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.set_content(_MAP_SLIDE)
        page.add_style_tag(content=css)
        before = page.screenshot()
        (slide,) = page.evaluate(EXTRACT_PRIMITIVES_JS)
        assert page.screenshot() == before, "the extraction changed what the page shows"
        items = slide["items"]
        texts = ["".join(run["text"] for run in item["runs"]) for item in items if item["kind"] == "text"]
        # Every word is an editable text box, written once.
        for word in ("Openness", "Open platform", "Single stage", "Whole loop", "Acme", "Northwind", "Coverage"):
            assert texts.count(word) == 1, (word, texts)
        # The y title is set on its side and read bottom to top: the angle
        # travels with its text box (review thread on #11604).
        (y_title,) = [item for item in items if item["kind"] == "text" and item["runs"][0]["text"] == "Openness"]
        assert y_title["rotate"] == 270
        assert all("rotate" not in item for item in items if item["kind"] == "text" and item is not y_title)
        rects = [item for item in items if item["kind"] == "rect"]
        # The two axes: a full-width 2px line and a full-height one, as shapes.
        assert any(item["h"] <= 2.5 and item["w"] > 300 and item["fill"] for item in rects)
        assert any(item["w"] <= 2.5 and item["h"] > 300 and item["fill"] for item in rects)
        # Each arrowhead is its own picture, captured alone.
        arrows = [item for item in items if item["kind"] == "image" and item["w"] <= 16 and item["h"] <= 16]
        assert len(arrows) >= 2
        browser.close()


def test_a_turned_text_box_is_laid_out_upright_and_turned_in_the_pptx():
    from pptx import Presentation
    from pptx.util import Emu

    from app.services.pptx_writer import _add_text

    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    run = {"text": "Openness", "color": [200, 200, 200], "size": 15}
    # The measured box of a vertical title: 20 px wide, 120 px tall, centred at (110, 260).
    item = {"kind": "text", "x": 100, "y": 200, "w": 20, "h": 120, "runs": [run], "lineHeight": 19, "rotate": 270}
    _add_text(slide, item, (1920, 1080, 1.0, 0.75), (10, 10, 10))
    (shape,) = slide.shapes
    assert shape.rotation == 270.0
    # Upright: as wide as the text runs long, about the same centre (the
    # writer's 6 px glyph slack widens a left-aligned box to the right).
    assert shape.height == Emu(20)
    assert shape.width >= Emu(120)
    assert shape.left == Emu(50)
    assert abs((shape.top + shape.height / 2) - 260) <= 1
