"""A browser session resumed in another process opens its page again before its first action of any kind.

The platform settles a re-created session on its first navigate, but a run resumed after a rollout replays its earlier
tool results and believes it is still on its page: its first action is often an evaluate, a screenshot or a
get_content. That action ran on about:blank, where reading local storage raises SecurityError, and a second
navigate was needed.
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
    "cookies": [{"name": "sid", "value": "c00kie", "domain": "10.42.3.17", "path": "/"}],
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


async def _kept_by_another_process() -> None:
    signed_in = browser_tool.BrowserSession(
        pw=None, browser=None, context=SimpleNamespace(storage_state=AsyncMock(return_value=STATE)),
        page=SimpleNamespace(url=f"{LAB}/agents"),
    )
    await browser_tool._remember_state(KEY, signed_in)
    # The run resumes in a new process: the shared copy only.
    browser_tool._kept_states.clear()
    browser_tool._kept_pages.clear()


async def test_the_first_evaluate_of_a_resumed_session_runs_on_its_page_signed_in(
    redis, fresh_process, launch, allow_all, caplog
):
    await _kept_by_another_process()

    with caplog.at_level(logging.WARNING, logger=browser_tool.logger.name):
        result = await browser_tool.browser_evaluate(KEY, "localStorage.getItem('copilot_token')")

    assert result == "JavaScript result:\nlab-t0ken"
    assert launch.page.url == f"{LAB}/agents"
    assert launch.page.gotos[0] == f"{LAB}/agents"
    assert KEY in caplog.text and "opened again before its first action" in caplog.text
    notice = browser_tool.take_notice(KEY)
    assert notice is not None and "It was on http://10.42.3.17:3000/agents. The next action opens it again" in notice


async def test_the_first_screenshot_of_a_resumed_session_is_not_blank(redis, fresh_process, launch, allow_all):
    await _kept_by_another_process()

    png = await browser_tool.browser_screenshot(KEY)

    assert png == f"{LAB}/agents".encode()


async def test_the_page_is_opened_again_once_only(redis, fresh_process, launch, allow_all):
    await _kept_by_another_process()

    await browser_tool.browser_screenshot(KEY)
    # Opened again, then once more with the token written back.
    first = list(launch.page.gotos)
    await browser_tool.browser_screenshot(KEY)

    assert first == [f"{LAB}/agents", f"{LAB}/agents"]
    assert launch.page.gotos == first


async def test_an_explicit_navigate_goes_where_it_is_told(redis, fresh_process, launch, allow_all, monkeypatch):
    await _kept_by_another_process()
    session = await browser_tool._get_or_create_session(KEY)
    session.redirects = SimpleNamespace(main_frame_refusals=0, refusal_since=lambda mark: None)
    monkeypatch.setattr(browser_tool, "_page_title", AsyncMock(return_value="Tools"))

    await browser_tool.browser_navigate(KEY, f"{LAB}/tools")

    assert f"{LAB}/agents" not in launch.page.gotos
    assert launch.page.url == f"{LAB}/tools"
    assert launch.page.storage == {"copilot_token": "lab-t0ken"}


async def test_a_url_the_policy_refuses_is_not_opened_again(redis, fresh_process, launch, monkeypatch, caplog):
    await _kept_by_another_process()
    monkeypatch.setattr(
        browser_tool, "_url_check", lambda _session: AsyncMock(side_effect=ValueError("not a public address"))
    )

    with caplog.at_level(logging.WARNING, logger=browser_tool.logger.name):
        png = await browser_tool.browser_screenshot(KEY)

    assert launch.page.gotos == []
    assert png == b"about:blank"
    assert "not opened again (URL policy" in caplog.text


async def test_a_browser_that_stopped_in_this_process_is_not_sent_back_to_its_page(
    redis, fresh_process, launch, allow_all
):
    signed_in = browser_tool.BrowserSession(
        pw=None, browser=None, context=SimpleNamespace(storage_state=AsyncMock(return_value=STATE)),
        page=SimpleNamespace(url=f"{LAB}/agents"),
    )
    await browser_tool._remember_state(KEY, signed_in)  # this process's copy is the newest

    session = await browser_tool._launch_session(KEY)

    assert session.resume_url is None
    notice = browser_tool.take_notice(KEY) or ""
    assert "navigate to the page to go on" in notice
