"""A run resumed in another process after a navigate lands where that navigate went (#9318).

The shared kept state names the page the session was on; a run resumed in another backend process reopens it
before its first action. It was shared only when the state read after a navigate succeeded: a page busy, or the
kept local storage of its origin not written back yet, left the shared copy naming the page of the PREVIOUS
navigate, and the evaluate after the resume ran there, the navigate lost. A navigate that is itself the first
action after a resume goes where it is told, and its notice says so instead of "opens it again first".
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import browser_tool

KEY = "agent:assignment:run"
PAGE_A = "https://lab.example/flows/map"
PAGE_B = "https://lab.example/agents"
OTHER_ORIGIN_B = "https://lab2.example/login"
STATE = {
    "cookies": [{"name": "sid", "value": "s3cret", "domain": "lab.example", "path": "/"}],
    "origins": [{"origin": "https://lab.example", "localStorage": [{"name": "token", "value": "t0ken"}]}],
}


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        self.data[key] = value
        return True

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, key):
        self.data.pop(key, None)
        return 1


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()

    async def client():
        return fake

    monkeypatch.setattr("app.core.redis.redis_client", client)
    return fake


@pytest.fixture
def fresh_process(monkeypatch):
    monkeypatch.setattr(browser_tool, "_sessions", {})
    monkeypatch.setattr(browser_tool, "_kept_states", {})
    monkeypatch.setattr(browser_tool, "_known_stamps", {})
    monkeypatch.setattr(browser_tool, "_kept_pages", {})
    monkeypatch.setattr(browser_tool, "_notices", {})


class FakeLaunch:
    """``async_playwright()`` opening one page on about:blank."""

    def __init__(self) -> None:
        page = SimpleNamespace(
            url="about:blank",
            on=MagicMock(),
            set_default_timeout=MagicMock(),
            set_default_navigation_timeout=MagicMock(),
            is_closed=MagicMock(return_value=False),
        )
        context = SimpleNamespace(
            route=AsyncMock(),
            new_page=AsyncMock(return_value=page),
            new_cdp_session=AsyncMock(side_effect=RuntimeError("no devtools in this test")),
            close=AsyncMock(),
        )

        async def new_context(**_kwargs):
            return context

        self.browser = SimpleNamespace(new_context=new_context, close=AsyncMock(), is_connected=lambda: True)
        self.chromium = SimpleNamespace(launch=AsyncMock(return_value=self.browser))

    def __call__(self):
        return self

    async def start(self):
        return self

    async def stop(self):
        return None


@pytest.fixture
def launch(monkeypatch):
    fake = FakeLaunch()
    monkeypatch.setattr("playwright.async_api.async_playwright", fake)
    monkeypatch.setattr(browser_tool.RedirectGuard, "attach", AsyncMock())
    monkeypatch.setattr(browser_tool.RedirectGuard, "watch_main_frame", AsyncMock())
    return fake


def _session_on(url: str) -> browser_tool.BrowserSession:
    context = SimpleNamespace(storage_state=AsyncMock(return_value=STATE))
    return browser_tool.BrowserSession(pw=None, browser=None, context=context, page=SimpleNamespace(url=url))


def _resume_elsewhere() -> None:
    """The run is handed to another process: nothing in memory there, the shared copy only."""
    browser_tool._sessions.clear()
    browser_tool._kept_states.clear()
    browser_tool._kept_pages.clear()
    browser_tool._notices.clear()


@pytest.mark.parametrize("target", [PAGE_B, OTHER_ORIGIN_B])
async def test_a_navigate_whose_state_read_failed_is_still_the_page_a_resume_reopens(
    redis, fresh_process, launch, target
):
    await browser_tool._remember_state(KEY, _session_on(PAGE_A))
    # The navigate to the target landed, but its state read shared nothing (page busy).
    navigated = _session_on(target)
    browser_tool._keep_landed_page(KEY, navigated, target)
    await browser_tool._share_landed_page(KEY, navigated, target)
    _resume_elsewhere()

    session = await browser_tool._launch_session(KEY)

    assert session.resume_url == target
    notice = browser_tool.take_notice(KEY)
    assert notice is not None and f"It was on {target}." in notice
    # The sign-in kept before the navigate travels with it.
    assert session.pending_storage == {"https://lab.example": [("token", "t0ken")]}


async def test_nothing_is_shared_for_a_landed_page_without_a_kept_state(redis, fresh_process):
    await browser_tool._share_landed_page(KEY, _session_on(PAGE_B), PAGE_B)

    assert redis.data == {}


class NavPage:
    """A re-created page: ``goto`` commits where it is told."""

    def __init__(self) -> None:
        self.url = "about:blank"
        self.main_frame = SimpleNamespace(parent_frame=None)
        self.on = MagicMock()

    async def goto(self, url, wait_until=None):
        self.url = url


@pytest.mark.parametrize("target", [PAGE_B, OTHER_ORIGIN_B])
async def test_a_navigate_first_after_a_resume_goes_where_it_is_told_and_says_so(fresh_process, monkeypatch, target):
    async def allow(url: str) -> str:
        return url

    monkeypatch.setattr(browser_tool, "validate_outbound_url_async", allow)
    session = browser_tool.BrowserSession(pw=None, browser=None, context=None, page=NavPage())
    session.resume_url = PAGE_A
    browser_tool._notices[KEY] = browser_tool._restored_notice(KEY, STATE, {"url": PAGE_A}, False, reopens=True)
    monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=session))
    monkeypatch.setattr(browser_tool, "_page_title", AsyncMock(return_value="B"))
    monkeypatch.setattr(browser_tool, "_landed_url", AsyncMock(side_effect=lambda s: s.page.url))
    monkeypatch.setattr(browser_tool, "_remember_state", AsyncMock(return_value=None))

    result = await browser_tool.browser_navigate(KEY, target)

    assert result.startswith(f"Navigated to: {target}")
    assert session.page.url == target
    assert session.resume_url is None
    notice = browser_tool.take_notice(KEY)
    assert notice is not None
    assert "opens it again first" not in notice
    assert "This navigate went to the page it was given instead" in notice
