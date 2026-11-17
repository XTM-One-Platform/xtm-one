"""``emulate_media`` in a real Chromium: matchMedia and a motion-safe animation answer the emulated state (#10675).

Tailwind's ``motion-safe:animate-spin`` spins only under ``(prefers-reduced-motion: no-preference)``: with
``reduced_motion=reduce`` the element's ``animationName`` is ``none``, and ``reset`` gives the spin back. The
emulation survives a new document loaded in the page, as the viewport does.

Skipped where Playwright's Chromium is not installed; the ``Browser tool (Chromium)`` CI job
installs it and sets ``REQUIRE_CHROMIUM=1``, which turns the skip into a failure.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest

from app.services import browser_tool

pytestmark = pytest.mark.chromium

KEY = "agent:conv"
# The rule Tailwind emits for ``motion-safe:animate-spin``.
PAGE = """<!doctype html><html><head><style>
@keyframes spin { to { transform: rotate(360deg) } }
@media (prefers-reduced-motion: no-preference) { .motion-safe\\:animate-spin { animation: spin 1s linear infinite } }
</style></head><body><div id="spinner" class="motion-safe:animate-spin">o</div></body></html>"""
STATE_JS = """() => ({
  reduce: matchMedia('(prefers-reduced-motion: reduce)').matches,
  dark: matchMedia('(prefers-color-scheme: dark)').matches,
  animation: getComputedStyle(document.getElementById('spinner')).animationName,
})"""


@pytest.fixture
async def session(monkeypatch) -> AsyncIterator[browser_tool.BrowserSession]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:  # pragma: no cover - Playwright is a dev dependency
        pytest.skip("Playwright is not installed")
    monkeypatch.setattr(browser_tool, "_kept_states", {})
    monkeypatch.setattr(browser_tool, "_kept_pages", {})
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
            context = await browser.new_context()
            page = await context.new_page()
            await page.set_content(PAGE)
            held = browser_tool.BrowserSession(pw=None, browser=browser, context=context, page=page)
            monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=held))
            yield held
        finally:
            await browser.close()


async def test_a_page_spins_by_default(session):
    state = await session.page.evaluate(STATE_JS)

    assert state == {"reduce": False, "dark": False, "animation": "spin"}


async def test_reduced_motion_stops_a_motion_safe_animation_until_reset(session):
    await browser_tool.browser_emulate_media(KEY, reduced_motion="reduce")
    assert await session.page.evaluate(STATE_JS) == {"reduce": True, "dark": False, "animation": "none"}

    await browser_tool.browser_emulate_media(KEY, reset=True)
    assert await session.page.evaluate(STATE_JS) == {"reduce": False, "dark": False, "animation": "spin"}


async def test_the_emulation_survives_a_new_document(session):
    await browser_tool.browser_emulate_media(KEY, reduced_motion="reduce", color_scheme="dark")

    await session.page.set_content(PAGE)

    assert await session.page.evaluate(STATE_JS) == {"reduce": True, "dark": True, "animation": "none"}
