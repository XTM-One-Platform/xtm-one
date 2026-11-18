"""The agent's browser shows the native UI a review needs (#126).

Playwright launches headless Chromium with ``--hide-scrollbars``: a screenshot painted no scrollbar, so an
overflowing box could not be reviewed. A native ``title`` tooltip is drawn by the browser outside the page, so
no screenshot shows it: ``hover`` now says it, with whether the element's text is cut.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import browser_tool

KEY = "agent:assignment:run-126"


@pytest.fixture
def fresh_process(monkeypatch):
    monkeypatch.setattr(browser_tool, "_sessions", {})
    monkeypatch.setattr(browser_tool, "_kept_states", {})
    monkeypatch.setattr(browser_tool, "_known_stamps", {})
    monkeypatch.setattr(browser_tool, "_kept_pages", {})
    monkeypatch.setattr(browser_tool, "_notices", {})


async def test_the_launch_keeps_classic_scrollbars(fresh_process, monkeypatch):
    fake_page = SimpleNamespace(
        url="about:blank", on=MagicMock(), set_default_timeout=MagicMock(), set_default_navigation_timeout=MagicMock()
    )
    context = SimpleNamespace(
        route=AsyncMock(),
        new_page=AsyncMock(return_value=fake_page),
        new_cdp_session=AsyncMock(side_effect=RuntimeError("no devtools")),
    )
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context))
    launch = AsyncMock(return_value=browser)
    pw = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
    monkeypatch.setattr(
        "playwright.async_api.async_playwright", lambda: SimpleNamespace(start=AsyncMock(return_value=pw))
    )
    monkeypatch.setattr(browser_tool, "_shared_entry", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_tool.RedirectGuard, "attach", AsyncMock())
    monkeypatch.setattr(browser_tool.RedirectGuard, "watch_main_frame", AsyncMock())

    await browser_tool._launch_session(KEY)

    kwargs = launch.await_args.kwargs
    assert kwargs["headless"] is True
    assert "--hide-scrollbars" in kwargs["ignore_default_args"]
    assert "--hide-scrollbars" not in kwargs["args"]


class TooltipPage:
    def __init__(self, found) -> None:
        self.url = "http://10.42.3.17:3000/agents"
        self.found = found
        self.mouse = SimpleNamespace(move=AsyncMock())
        self.hover = AsyncMock()
        self.evaluated: list[str] = []

    def locator(self, selector):
        page = self

        async def evaluate(script, timeout=None):
            page.evaluated.append(script)
            if script is browser_tool._HOVER_POINT_JS:
                return {"x": 40, "y": 12}
            if isinstance(page.found, Exception):
                raise page.found
            return page.found

        return SimpleNamespace(first=SimpleNamespace(evaluate=evaluate))


def _session(page) -> None:
    browser_tool._sessions[KEY] = browser_tool.BrowserSession(
        pw=None,
        browser=SimpleNamespace(is_connected=lambda: True),
        context=SimpleNamespace(),
        page=page,
        redirects=SimpleNamespace(main_frame_refusals=0, refusal_since=lambda mark: None),
    )


@pytest.fixture
def no_state(monkeypatch):
    monkeypatch.setattr(browser_tool, "_remember_state", AsyncMock())
    monkeypatch.setattr(browser_tool, "_reopen_resumed_page", AsyncMock())


async def test_hover_quotes_the_native_tooltip_and_the_cut(fresh_process, no_state):
    page = TooltipPage({"text": "Threat actor: APT-29 (Cozy Bear), full name", "cut": True})
    _session(page)

    answer = await browser_tool.browser_hover(KEY, "td.name span")

    assert answer.startswith("Hovering 'td.name span' at (40, 12) on http://10.42.3.17:3000/agents.")
    assert 'Native tooltip (title): "Threat actor: APT-29 (Cozy Bear), full name"' in answer
    assert "no screenshot shows it" in answer
    assert "text is cut" in answer
    page.mouse.move.assert_awaited_once_with(40.0, 12.0)


async def test_hover_without_a_title_says_nothing_more(fresh_process, no_state):
    page = TooltipPage({"text": "", "cut": False})
    _session(page)

    answer = await browser_tool.browser_hover(KEY, "button")

    assert answer == "Hovering 'button' at (40, 12) on http://10.42.3.17:3000/agents."


async def test_a_long_tooltip_is_cut_and_an_uncut_element_is_not_flagged(fresh_process, no_state):
    page = TooltipPage({"text": "x" * (browser_tool.MAX_TOOLTIP_CHARS + 50), "cut": False})
    _session(page)

    answer = await browser_tool.browser_hover(KEY, "span")

    assert '"' + "x" * browser_tool.MAX_TOOLTIP_CHARS + '..."' in answer
    assert "text is cut" not in answer


async def test_a_failed_tooltip_read_keeps_the_hover_answer(fresh_process, no_state):
    page = TooltipPage(RuntimeError("detached"))
    _session(page)

    answer = await browser_tool.browser_hover(KEY, "span")

    assert answer == "Hovering 'span' at (40, 12) on http://10.42.3.17:3000/agents."
