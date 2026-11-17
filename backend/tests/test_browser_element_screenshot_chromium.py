"""A ``screenshot`` of one element taller than the viewport paints the whole element, in a real Chromium.

An SPA root three times the viewport's height draws its cards only once they enter the view
(an ``IntersectionObserver``, as in-view effects and lazy lists do). The element capture used to
come back as background past the first screen (#9438); it is now taken with the page grown to the
element's height and the viewport restored after it. A viewport capture is unchanged.

Skipped where Playwright's Chromium is not installed; the ``Browser tool (Chromium)`` CI job
installs it and sets ``REQUIRE_CHROMIUM=1``, which turns the skip into a failure.
"""

from __future__ import annotations

import io
import os
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from app.services import browser_tool
from app.services.image_summary import looks_blank, summarize_image_bytes

pytestmark = pytest.mark.chromium

VIEWPORT = {"width": 1000, "height": 600}
CARD_BLUE = (0x00, 0x15, 0xA8)

# Twelve cards in a root of about 3 x 600 px; a card is painted blue only once it has been seen.
PAGE = (
    """<!doctype html><html><body style="margin: 0; background: #F2F2F3">
<div id="root" style="padding: 20px">
"""
    + "\n".join(
        '<div class="card" style="height: 130px; margin: 0 0 20px; background: #F2F2F3"></div>' for _ in range(12)
    )
    + """
</div>
<script>
const seen = new IntersectionObserver((entries) => {
  for (const entry of entries) if (entry.isIntersecting) entry.target.style.background = '#0015A8';
});
document.querySelectorAll('.card').forEach((card) => seen.observe(card));
</script></body></html>"""
)


@pytest.fixture
async def session(monkeypatch) -> AsyncIterator[browser_tool.BrowserSession]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:  # pragma: no cover - Playwright is a dev dependency
        pytest.skip("Playwright is not installed")
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch()
        except Exception as exc:
            if "Executable doesn't exist" not in str(exc) and "playwright install" not in str(exc).lower():
                raise
            if os.environ.get("REQUIRE_CHROMIUM") == "1":
                pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
            pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
        try:
            context = await browser.new_context(viewport=VIEWPORT)
            page = await context.new_page()
            await page.set_content(PAGE)
            held = browser_tool.BrowserSession(pw=None, browser=browser, context=context, page=page)
            monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=held))
            yield held
        finally:
            await browser.close()


def _blue_share_of_bottom_half(png: bytes) -> float:
    image = Image.open(io.BytesIO(png)).convert("RGB")
    width, height = image.size
    bottom = image.crop((0, height // 2, width, height))
    pixels = list(bottom.getdata())
    return sum(1 for pixel in pixels if pixel == CARD_BLUE) / len(pixels)


async def test_an_element_three_viewports_tall_is_captured_with_every_card_painted(session):
    root_height = await session.page.evaluate("document.getElementById('root').offsetHeight")
    assert root_height > 2 * VIEWPORT["height"]

    png = await browser_tool.browser_screenshot("test:element", selector="#root")

    image = Image.open(io.BytesIO(png))
    assert abs(image.size[1] - root_height) <= 1
    # The cards past the first screen are painted, not background.
    assert _blue_share_of_bottom_half(png) > 0.3
    assert not looks_blank(summarize_image_bytes(png))
    # The viewport is the one the run set, after the capture.
    assert session.page.viewport_size == VIEWPORT


async def test_a_viewport_capture_is_unchanged(session):
    png = await browser_tool.browser_screenshot("test:element")
    assert Image.open(io.BytesIO(png)).size == (VIEWPORT["width"], VIEWPORT["height"])
    assert session.page.viewport_size == VIEWPORT
