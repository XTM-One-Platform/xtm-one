"""The first evaluate after a resume runs once the reopened page has rendered (#9793).

A run resumed in another process reopens its last page before its first action (#9408), at ``domcontentloaded``.
A single-page application has not mounted its root then: an evaluate reading ``document.querySelector('main')``
got null and failed with a TypeError, while a navigate then the same evaluate worked. The evaluate of a page the
resume has just opened again now waits (bounded, on the fake clock of #9739) for the page to render, and a script
error there says the page was just opened again. An evaluate of a session not resumed keeps its timing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services import browser_tool

KEY = "agent:assignment:run-9793"
PAGE = "http://10.42.3.17:3100/settings/integrations/oauth"
EXPRESSION = "document.querySelector('main').innerText.length"


class SpaPage:
    """A reopened SPA whose root mounts after *mount_after* render reads; the agent's script reads ``main``."""

    def __init__(self, mount_after: int) -> None:
        self.url = "about:blank"
        self.mount_after = mount_after
        self.render_reads = 0
        self.load_waits: list[str] = []
        self.scripts: list[str] = []

    @property
    def mounted(self) -> bool:
        return self.render_reads > self.mount_after

    async def goto(self, url, wait_until=None):
        self.url = url

    async def wait_for_load_state(self, state, timeout=None):
        self.load_waits.append(state)

    async def evaluate(self, script, *args):
        if script == browser_tool._RENDERED_JS:
            self.render_reads += 1
            return self.mounted
        self.scripts.append(script)
        if not self.mounted:
            raise RuntimeError("Page.evaluate: TypeError: Cannot read properties of null (reading 'innerText')")
        return 42


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


async def test_the_first_evaluate_after_a_resume_sees_the_rendered_page(session):
    page = SpaPage(mount_after=3)
    session(page, resumed=True)

    result = await browser_tool.browser_evaluate(KEY, EXPRESSION)

    assert result == "JavaScript result:\n42"
    assert page.load_waits == ["load", "networkidle"]
    assert page.render_reads == 4
    assert page.scripts == [EXPRESSION]  # the agent's script ran once, on the mounted page


async def test_a_reopened_page_never_rendered_says_to_run_the_script_again(session):
    page = SpaPage(mount_after=10_000)
    session(page, resumed=True)

    with pytest.raises(browser_tool.ReopenedPageScriptError) as raised:
        await browser_tool.browser_evaluate(KEY, EXPRESSION)

    message = str(raised.value)
    assert "TypeError" in message
    assert message.endswith(browser_tool._REOPENED_SCRIPT_NOTE)
    assert page.render_reads == 21  # one read at 0 s, then one after each 0.01 s pause up to the 0.2 s bound
    assert page.scripts == [EXPRESSION]


async def test_an_evaluate_of_a_session_not_resumed_runs_at_once(session):
    page = SpaPage(mount_after=0)
    page.url = PAGE
    session(page, resumed=False)

    with pytest.raises(RuntimeError, match="TypeError") as raised:
        await browser_tool.browser_evaluate(KEY, EXPRESSION)

    assert not isinstance(raised.value, browser_tool.ReopenedPageScriptError)
    assert page.load_waits == [] and page.render_reads == 0
