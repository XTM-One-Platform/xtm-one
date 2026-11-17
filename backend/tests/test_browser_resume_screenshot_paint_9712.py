"""A screenshot that is the first action after a resume captures the reopened page once it has painted (#9712).

A run resumed in another process reopens its last page before its first action (#8265, #9408). A single-page
application shows its loading screen for a while after ``domcontentloaded``: a screenshot taken at once was that
screen, one colour, flagged "may be blank" and wasted. The capture of a page the resume has just opened again now
waits for it to paint (bounded); still blank at the bound, the notice says so. A screenshot of a session that was
not resumed is taken at once, as before.

The wait is measured on a fake clock that only the paint loop's pauses advance (#9739): a capture and its PNG
summary take no fake time, so the outcome never depends on how loaded the runner is.
"""

from __future__ import annotations

import io
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from app.services import browser_tool

KEY = "agent:assignment:run-9712"
PAGE = "http://10.42.3.17:3100/"


def _png(painted: bool) -> bytes:
    image = Image.new("RGB", (200, 120), (0xF2, 0xF2, 0xF3))  # the loading screen's background
    if painted:
        for x in range(20, 180):
            for y in range(20, 60):
                image.putpixel((x, y), (0x00, 0x1B, 0xDA))  # a saturated header
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


BLANK = _png(False)
PAINTED = _png(True)


class SpaPage:
    """A reopened SPA: its first *blank_shots* captures show the loading screen, the next ones the rendered page."""

    def __init__(self, blank_shots: int) -> None:
        self.url = "about:blank"
        self.blank_shots = blank_shots
        self.shots = 0
        self.idle_waits: list[str] = []

    async def goto(self, url, wait_until=None):
        self.url = url

    async def wait_for_load_state(self, state, timeout=None):
        self.idle_waits.append(state)

    async def screenshot(self, full_page=False):
        self.shots += 1
        return BLANK if self.shots <= self.blank_shots else PAINTED


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr(browser_tool, "_notices", {})
    monkeypatch.setattr(browser_tool, "_url_check", lambda _session: AsyncMock(side_effect=lambda url: url))
    monkeypatch.setattr(browser_tool, "_remember_state", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_tool, "_REOPENED_PAINT_STEP_SECONDS", 0.01)
    monkeypatch.setattr(browser_tool, "_REOPENED_PAINT_SECONDS", 0.2)
    now = [0.0]

    async def fake_sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(browser_tool, "_paint_clock", lambda: now[0])
    monkeypatch.setattr(browser_tool, "_paint_sleep", fake_sleep)

    def install(page: SpaPage, *, resumed: bool) -> browser_tool.BrowserSession:
        made = browser_tool.BrowserSession(pw=None, browser=None, context=None, page=page)
        made.resume_url = PAGE if resumed else None
        monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=made))
        if resumed:
            browser_tool._notices[KEY] = f"Browser session re-created. It was on {PAGE}. {browser_tool._REOPEN_NOTE}."
        return made

    return install


async def test_the_first_screenshot_after_a_resume_waits_for_the_page_to_paint(session):
    page = SpaPage(blank_shots=2)
    session(page, resumed=True)

    png = await browser_tool.browser_screenshot(KEY)

    assert png == PAINTED
    assert page.shots == 3
    assert page.idle_waits == ["networkidle"]
    assert "had not painted" not in (browser_tool.take_notice(KEY) or "")


async def test_a_reopened_page_still_blank_at_the_bound_is_said_not_settled(session):
    page = SpaPage(blank_shots=10_000)
    session(page, resumed=True)

    png = await browser_tool.browser_screenshot(KEY)

    assert png == BLANK
    assert page.shots == 21  # one capture at 0 s, then one after each 0.01 s pause up to the 0.2 s bound
    notice = browser_tool.take_notice(KEY) or ""
    assert f"It was on {PAGE}." in notice
    assert "The page was opened again but had not painted after 0.2 s: capture again after a wait." in notice


async def test_a_screenshot_of_a_session_not_resumed_is_taken_at_once(session):
    page = SpaPage(blank_shots=1)
    page.url = PAGE
    session(page, resumed=False)

    png = await browser_tool.browser_screenshot(KEY)

    assert png == BLANK  # a blank page is captured as it is: no wait, no retry
    assert page.shots == 1 and page.idle_waits == []
    assert browser_tool.take_notice(KEY) is None
