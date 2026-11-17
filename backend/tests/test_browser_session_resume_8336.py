"""A browser session closed idle opens its kept page again before its first action.

The cleanup loop closes a session unused for ``SESSION_TIMEOUT_SECONDS`` and keeps its state in the process. The
next action re-created the session from that copy on about:blank: the page was opened again only for a state another
process kept. A run that waited past the idle timeout (a CI wait, a long consult) then ran its evaluate on about:blank
(SecurityError on local storage) or took a blank screenshot, while it believed it was still on its page.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import browser_tool

LAB = "http://10.42.3.17:3000"
KEY = "agent:assignment:run"
STATE = {
    "cookies": [],
    "origins": [{"origin": LAB, "localStorage": [{"name": "copilot_token", "value": "lab-t0ken"}]}],
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
    monkeypatch.setattr(browser_tool, "_ensure_cleanup_running", AsyncMock())


@pytest.fixture
def allow_all(monkeypatch):
    monkeypatch.setattr(browser_tool, "_url_check", lambda _session: AsyncMock(side_effect=lambda url: url))


class FakePage:
    """A page on about:blank whose local storage throws there, as Chromium's opaque origin does."""

    def __init__(self) -> None:
        self.url = "about:blank"
        self.storage: dict[str, str] = {}
        self.gotos: list[str] = []
        self.on = MagicMock()
        self.set_default_timeout = MagicMock()
        self.set_default_navigation_timeout = MagicMock()
        self.is_closed = MagicMock(return_value=False)
        self.screenshot = AsyncMock(side_effect=lambda **_kw: self.url.encode())

    async def goto(self, url, wait_until=None):
        self.gotos.append(url)
        self.url = url

    async def evaluate(self, script, items=None):
        if self.url == "about:blank":
            raise RuntimeError("SecurityError: Failed to read the 'localStorage' property from 'Window'")
        if items is not None:  # the platform's write-back
            written = 0
            for name, value in items:
                if name not in self.storage:
                    self.storage[name] = value
                    written += 1
            return written
        if "localStorage.length" in script:  # the platform's own read of the page's storage
            return [[k, v] for k, v in self.storage.items()]
        return self.storage.get("copilot_token")


class FakeLaunch:
    def __init__(self) -> None:
        self.page = FakePage()
        self.context = SimpleNamespace(
            route=AsyncMock(),
            new_page=AsyncMock(return_value=self.page),
            new_cdp_session=AsyncMock(side_effect=RuntimeError("no devtools in this test")),
            storage_state=AsyncMock(return_value=STATE),
            close=AsyncMock(),
        )

        async def new_context(**kwargs):
            return self.context

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


async def _closed_idle_in_this_process() -> None:
    """A signed-in session of this process, kept, then closed idle by the cleanup loop."""
    signed_in = browser_tool.BrowserSession(
        pw=SimpleNamespace(stop=AsyncMock()),
        browser=SimpleNamespace(is_connected=lambda: True, close=AsyncMock()),
        context=SimpleNamespace(storage_state=AsyncMock(return_value=STATE), close=AsyncMock()),
        page=SimpleNamespace(url=f"{LAB}/operations/work-items", is_closed=lambda: False),
    )
    browser_tool._sessions[KEY] = signed_in
    await browser_tool._remember_state(KEY, signed_in)
    await browser_tool._close_session(KEY)  # what _cleanup_loop does past SESSION_TIMEOUT_SECONDS
    assert KEY not in browser_tool._sessions and KEY in browser_tool._kept_states


async def test_the_first_evaluate_after_an_idle_close_runs_on_its_page_signed_in(
    redis, fresh_process, launch, allow_all, caplog
):
    await _closed_idle_in_this_process()

    with caplog.at_level(logging.WARNING, logger=browser_tool.logger.name):
        result = await browser_tool.browser_evaluate(KEY, "localStorage.getItem('copilot_token')")

    assert result == "JavaScript result:\nlab-t0ken"
    assert launch.page.gotos[0] == f"{LAB}/operations/work-items"
    assert "re-created from the state of this process (closed idle)" in caplog.text
    assert "opened again before its first action" in caplog.text
    notice = browser_tool.take_notice(KEY) or ""
    assert "The next action opens it again first, signed in." in notice
    assert "navigate to the page to go on" not in notice


async def test_the_first_screenshot_after_an_idle_close_is_not_blank(redis, fresh_process, launch, allow_all):
    await _closed_idle_in_this_process()

    png = await browser_tool.browser_screenshot(KEY)

    assert png == f"{LAB}/operations/work-items".encode()


async def test_a_browser_that_died_in_this_process_is_still_not_sent_back(redis, fresh_process, launch, allow_all):
    await _closed_idle_in_this_process()
    # A session is open for the key, but its browser died: the page may be what stopped it.
    browser_tool._sessions[KEY] = browser_tool.BrowserSession(
        pw=SimpleNamespace(stop=AsyncMock()),
        browser=SimpleNamespace(is_connected=lambda: False, close=AsyncMock()),
        context=SimpleNamespace(close=AsyncMock()),
        page=SimpleNamespace(url=f"{LAB}/operations/work-items", is_closed=lambda: True),
    )

    session = await browser_tool._get_or_create_session(KEY)

    assert session.resume_url is None
    assert "navigate to the page to go on" in (browser_tool.take_notice(KEY) or "")


async def test_a_key_with_nothing_kept_opens_empty(redis, fresh_process, launch, allow_all):
    session = await browser_tool._get_or_create_session(KEY)

    assert session.resume_url is None
    assert browser_tool.take_notice(KEY) is None
