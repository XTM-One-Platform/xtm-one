"""``type`` on a controlled input the agent never clicked leaves the page's state holding the text.

A tag input (the shared ``TagInput`` of xtm-one) keeps its draft in framework state: the input renders
``value={draft}`` and an ``onFocus`` effect resets the draft one frame after the field gets the focus.
``type`` used to fill such a field without giving it the focus first: the fill focused it, the text
reached the DOM, then the effect reset the state, so the ``__reactProps`` value read ``""`` and the next
``press Enter`` added no chip (UX run e435af58 on xtm-one #10406). ``type`` now focuses the field, lets it
render, fills, reads the framework value back, types again with real key events when it differs, and
names a value that still differs.

The fixture is a minimal page that renders like React: the element carries ``__reactProps$<id>`` with the
``value`` prop it was rendered with, and only the page's state (written by its ``input`` handler)
decides what is rendered. No network: the page is written with ``set_content``.

Skipped where Playwright's Chromium is not installed; the ``Browser tool (Chromium)`` CI job installs it
and sets ``REQUIRE_CHROMIUM=1``, which turns the skip into a failure.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest

from app.services import browser_tool

pytestmark = pytest.mark.chromium

KEY = "test:type-controlled"

# A controlled input as React renders it: state -> props -> DOM. The focus effect resets the draft
# one frame later, as a tag input that opens its editor on focus does. Enter adds a chip from the
# value the keydown handler reads in the props, as TagInput's onKeyDown does.
PAGE = """<!doctype html><html><body>
<input id="tag" />
<input id="short" />
<ul id="chips"></ul>
<script>
function controlled(el, opts) {
  const propsKey = '__reactProps$t3st';
  let state = '';
  const render = () => {
    el[propsKey] = {value: state};
    if (el.value !== state) el.value = state;
  };
  el.addEventListener('input', () => {
    const next = opts.accept ? opts.accept(el.value) : el.value;
    state = next;
    render();
  });
  if (opts.resetOnFocus) {
    el.addEventListener('focus', () => requestAnimationFrame(() => { state = ''; render(); }));
  }
  el.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return;
    const value = el[propsKey].value;
    if (!value) return;
    const li = document.createElement('li');
    li.textContent = value;
    document.getElementById('chips').appendChild(li);
    state = '';
    render();
  });
  render();
}
controlled(document.getElementById('tag'), {resetOnFocus: true});
// A field that keeps four characters at most, whatever is typed.
controlled(document.getElementById('short'), {accept: (v) => v.slice(0, 4)});
</script></body></html>"""


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
            context = await browser.new_context(viewport={"width": 800, "height": 600})
            page = await context.new_page()
            await page.set_content(PAGE)
            held = browser_tool.BrowserSession(pw=None, browser=browser, context=context, page=page)
            monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=held))
            monkeypatch.setattr(browser_tool, "_remember_state", AsyncMock())
            yield held
        finally:
            await browser.close()


async def _props_value(session, selector: str) -> str:
    return await session.page.eval_on_selector(selector, "(el) => el['__reactProps$t3st'].value")


async def test_type_without_a_click_leaves_the_controlled_value_and_enter_adds_the_chip(session):
    result = await browser_tool.browser_type(KEY, "#tag", "abc")

    assert await _props_value(session, "#tag") == "abc"
    assert "does not hold" not in result

    await browser_tool.browser_press(KEY, "Enter")
    chips = await session.page.eval_on_selector_all("#chips li", "(items) => items.map((li) => li.textContent)")
    assert chips == ["abc"]


async def test_a_field_that_keeps_less_than_typed_is_named_in_the_result(session):
    result = await browser_tool.browser_type(KEY, "#short", "abcdefgh")

    assert await _props_value(session, "#short") == "abcd"
    assert "The field does not hold the text: it reads 'abcd'" in result
    assert "after a second try with real key events" in result


async def test_a_plain_input_is_typed_once(session):
    await session.page.set_content('<input id="plain" />')

    result = await browser_tool.browser_type(KEY, "#plain", "hello")

    assert result == "Typed into '#plain': hello"
    assert await session.page.input_value("#plain") == "hello"
