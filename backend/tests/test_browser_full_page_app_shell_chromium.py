"""A ``full_page`` screenshot of an app shell paints its fixed top bar once, at the top, in a real Chromium.

An app with a fixed top bar and sidebar scrolls inside its own container (``height: 100vh; overflow: auto``).
A stitched ``full_page`` capture came back one viewport tall for such a page, or, on a page whose document
also scrolls, with the fixed bar painted again over the content further down (#10196). The capture is now taken
with the viewport grown to what the scroll container holds, so the bar is drawn once and every card shows.

Skipped where Playwright's Chromium is not installed; the ``Browser tool (Chromium)`` CI job installs it and
sets ``REQUIRE_CHROMIUM=1``, which turns the skip into a failure.
"""

from __future__ import annotations

import io
import os
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from app.services import browser_tool

pytestmark = pytest.mark.chromium

VIEWPORT = {"width": 1000, "height": 600}
BAR_RED = (0xC8, 0x10, 0x10)
CARD_BLUE = (0x00, 0x15, 0xA8)
BAR_HEIGHT = 48
CARDS = 12

PAGE = (
    """<!doctype html><html><body style="margin: 0; background: #F2F2F3">
<header style="position: fixed; top: 0; left: 0; right: 0; height: 48px; background: #C81010; z-index: 2"></header>
<main id="scroller" style="position: absolute; top: 48px; left: 0; right: 0; bottom: 0; overflow-y: auto">
"""
    + "\n".join('<div style="height: 130px; margin: 20px; background: #0015A8"></div>' for _ in range(CARDS))
    + """
</main></body></html>"""
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


def _rows_with(image: Image.Image, colour: tuple[int, int, int], x: int) -> list[int]:
    return [y for y in range(image.size[1]) if image.getpixel((x, y)) == colour]


async def test_a_full_page_capture_of_an_app_shell_shows_every_card_and_the_bar_once(session):
    content = await session.page.evaluate("document.getElementById('scroller').scrollHeight")
    assert content > 2 * VIEWPORT["height"]

    png = await browser_tool.browser_screenshot("test:shell", full_page=True)

    image = Image.open(io.BytesIO(png)).convert("RGB")
    assert image.size[1] >= BAR_HEIGHT + content - 1
    # The fixed bar is drawn once, at the top: no red row past it.
    assert max(_rows_with(image, BAR_RED, 500)) < BAR_HEIGHT
    # The last card, at the bottom of the scroll container, is in the capture.
    assert max(_rows_with(image, CARD_BLUE, 500)) > image.size[1] - 200
    # The viewport is the one the run set, after the capture.
    assert session.page.viewport_size == VIEWPORT


async def test_a_full_page_capture_of_a_page_that_fits_is_one_viewport(session):
    await session.page.set_content('<!doctype html><html><body style="margin: 0"><p>short</p></body></html>')
    png = await browser_tool.browser_screenshot("test:shell", full_page=True)
    assert Image.open(io.BytesIO(png)).size == (VIEWPORT["width"], VIEWPORT["height"])
    assert session.page.viewport_size == VIEWPORT
