"""The first navigate of a re-created session ends on the URL it was given (#9576).

After a resume, the local storage written back to the re-created page can carry the state an application routes
from: after its load the page moved itself back to the URL it had kept (a debounced ``history.replaceState``), and
the navigate answered the requested URL while the next ``evaluate`` read the previous query string. The first
navigate of a re-created session watches the page past its load and opens the requested URL once more when the
page left it; a navigate of a session that was not re-created is unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import browser_tool

KEY = "agent:assignment:run"
KEPT = "https://lab.example/analytics?period=30d&ba_q=agent+1"
REQUESTED = "https://lab.example/analytics?breakdown=agents&ba_f_status=active"


class RoutingPage:
    """A page whose application routes back to *kept* after the load, *times* times."""

    def __init__(self, kept: str | None, times: int = 1) -> None:
        self.url = "about:blank"
        self.href = "about:blank"
        self.kept = kept
        self.times = times
        self.gotos: list[str] = []
        self.main_frame = SimpleNamespace(parent_frame=None)
        self.on = MagicMock()

    async def goto(self, url, wait_until=None):
        self.gotos.append(url)
        self.url = url
        self.href = url

    async def wait_for_load_state(self, state, timeout=None):
        if self.kept and self.times > 0:
            self.times -= 1
            self.href = self.kept  # history.replaceState: page.url follows in Playwright too
            self.url = self.kept

    async def evaluate(self, script, *args):
        return self.href


@pytest.fixture
def navigate_env(monkeypatch):
    monkeypatch.setattr(browser_tool, "_sessions", {})
    monkeypatch.setattr(browser_tool, "_notices", {})
    monkeypatch.setattr(browser_tool, "_RESUME_SETTLE_SECONDS", 0)

    async def allow(url: str) -> str:
        return url

    monkeypatch.setattr(browser_tool, "validate_outbound_url_async", allow)
    monkeypatch.setattr(browser_tool, "_page_title", AsyncMock(return_value="Analytics"))
    monkeypatch.setattr(browser_tool, "_remember_state", AsyncMock(return_value={}))

    def install(page: RoutingPage, *, resume_url: str | None) -> browser_tool.BrowserSession:
        session = browser_tool.BrowserSession(pw=None, browser=None, context=None, page=page)
        session.resume_url = resume_url
        monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=session))
        return session

    return install


async def test_a_navigate_after_a_resume_ends_on_the_requested_url_when_the_page_routes_back(navigate_env):
    page = RoutingPage(kept=KEPT)
    navigate_env(page, resume_url=KEPT)

    result = await browser_tool.browser_navigate(KEY, REQUESTED)

    assert result.startswith(f"Navigated to: {REQUESTED}\n")
    assert "not the requested" not in result
    assert page.href == REQUESTED
    assert page.gotos == [REQUESTED, REQUESTED]


async def test_a_page_that_keeps_routing_back_is_named_by_the_answer(navigate_env):
    page = RoutingPage(kept=KEPT, times=5)
    navigate_env(page, resume_url=KEPT)

    result = await browser_tool.browser_navigate(KEY, REQUESTED)

    # Opened again once only, and the answer says where scripts run instead of claiming the requested URL.
    assert page.gotos == [REQUESTED, REQUESTED]
    assert result.startswith(f"Navigated to: {KEPT}\n")
    assert f"not the requested {REQUESTED}" in result


async def test_a_navigate_of_a_session_not_recreated_is_opened_once(navigate_env):
    page = RoutingPage(kept=None)
    navigate_env(page, resume_url=None)

    result = await browser_tool.browser_navigate(KEY, REQUESTED)

    assert result.startswith(f"Navigated to: {REQUESTED}\n")
    assert page.gotos == [REQUESTED]


async def test_a_recreated_page_that_stays_put_is_opened_once(navigate_env):
    page = RoutingPage(kept=None)
    navigate_env(page, resume_url=KEPT)

    result = await browser_tool.browser_navigate(KEY, REQUESTED)

    assert result.startswith(f"Navigated to: {REQUESTED}\n")
    assert page.gotos == [REQUESTED]
