"""A re-created browser session lands signed in on the first navigate.

After a rollout the session came back on about:blank, or on a document whose
local storage raised SecurityError, and a lab preview signed in by a local
storage token showed its sign-in page: two or three navigates were needed, and
an action run on about:blank overwrote the kept state with one naming no page.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


class FakePage:
    """A page whose navigations land where *landings* says, in turn; its local storage is a dict."""

    def __init__(self, landings: list[str], *, storage_errors: int = 0) -> None:
        self.url = "about:blank"
        self.landings = list(landings)
        self.storage: dict[str, str] = {}
        self.storage_errors = storage_errors
        self.gotos: list[str] = []

    async def goto(self, url, wait_until=None):
        self.gotos.append(url)
        self.url = self.landings.pop(0) if self.landings else url

    async def evaluate(self, script, items):
        if self.storage_errors:
            self.storage_errors -= 1
            raise RuntimeError("SecurityError: Access is denied for this document.")
        written = 0
        for name, value in items:
            if name not in self.storage:
                self.storage[name] = value
                written += 1
        return written


def _resumed(page: FakePage, state: dict = STATE) -> browser_tool.BrowserSession:
    session = browser_tool.BrowserSession(
        pw=None, browser=None, context=SimpleNamespace(storage_state=AsyncMock(return_value=state)), page=page
    )
    session.pending_storage = browser_tool._storage_by_origin(state)
    return session


async def test_missing_local_storage_is_written_back_and_the_page_opened_again(caplog):
    page = FakePage([f"{LAB}/tools"])
    page.url = f"{LAB}/login"
    session = _resumed(page)

    with caplog.at_level(logging.WARNING, logger=browser_tool.logger.name):
        await browser_tool._settle_resumed_page(KEY, session, f"{LAB}/tools")

    assert page.storage == {"copilot_token": "lab-t0ken"}
    assert page.gotos == [f"{LAB}/tools"]
    assert session.pending_storage == {}
    assert KEY in caplog.text and "written back" in caplog.text


async def test_a_page_that_kept_its_storage_is_not_opened_again():
    page = FakePage([])
    page.url = f"{LAB}/tools"
    page.storage["copilot_token"] = "lab-t0ken"
    session = _resumed(page)

    await browser_tool._settle_resumed_page(KEY, session, f"{LAB}/tools")

    assert page.gotos == []
    assert session.pending_storage == {}


async def test_a_navigate_left_on_about_blank_is_opened_again():
    page = FakePage([f"{LAB}/tools"])
    session = _resumed(page)

    await browser_tool._settle_resumed_page(KEY, session, f"{LAB}/tools")

    # Opened again from about:blank, then once more with the token written back.
    assert page.gotos == [f"{LAB}/tools", f"{LAB}/tools"]
    assert page.url == f"{LAB}/tools"
    assert page.storage == {"copilot_token": "lab-t0ken"}


async def test_a_storage_security_error_is_retried_then_written_back():
    page = FakePage([f"{LAB}/tools", f"{LAB}/tools"], storage_errors=1)
    page.url = f"{LAB}/tools"
    session = _resumed(page)

    await browser_tool._settle_resumed_page(KEY, session, f"{LAB}/tools")

    assert page.storage == {"copilot_token": "lab-t0ken"}
    assert len(page.gotos) == 2


async def test_the_reopens_are_bounded(caplog):
    page = FakePage(["about:blank"] * 10)
    session = _resumed(page)

    with caplog.at_level(logging.WARNING, logger=browser_tool.logger.name):
        await browser_tool._settle_resumed_page(KEY, session, f"{LAB}/tools")

    assert len(page.gotos) == browser_tool._RESUME_REOPENS
    assert "not opened again" in caplog.text


async def test_another_origin_is_left_alone():
    page = FakePage([])
    page.url = "https://docs.example/page"
    session = _resumed(page)

    await browser_tool._settle_resumed_page(KEY, session, "https://docs.example/page")

    assert page.gotos == [] and page.storage == {}
    assert LAB.lower() in session.pending_storage


async def test_an_action_on_about_blank_keeps_the_previous_state(redis, fresh_process):
    signed_in = browser_tool.BrowserSession(
        pw=None, browser=None, context=SimpleNamespace(storage_state=AsyncMock(return_value=STATE)),
        page=SimpleNamespace(url=f"{LAB}/tools"),
    )
    await browser_tool._remember_state(KEY, signed_in)
    blank = browser_tool.BrowserSession(
        pw=None, browser=None, context=SimpleNamespace(storage_state=AsyncMock(return_value={"cookies": [], "origins": []})),
        page=SimpleNamespace(url="about:blank"),
    )

    await browser_tool._remember_state(KEY, blank)

    blank.context.storage_state.assert_not_awaited()
    shared = await browser_tool._shared_state(KEY)
    assert shared is not None
    state, page = shared
    assert state == STATE
    assert page["url"] == f"{LAB}/tools"


async def test_the_first_navigate_of_a_resumed_session_settles_it(fresh_process, monkeypatch):
    page = FakePage([f"{LAB}/login", f"{LAB}/tools"])
    session = _resumed(page)
    session.redirects = SimpleNamespace(main_frame_refusals=0, refusal_since=lambda mark: None)
    session.console = SimpleNamespace(clear=lambda: None)
    monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=session))
    monkeypatch.setattr(browser_tool, "_url_check", lambda _session: AsyncMock(side_effect=lambda url: url))
    monkeypatch.setattr(browser_tool, "_page_title", AsyncMock(return_value="Tools"))
    monkeypatch.setattr(browser_tool, "_remember_state", AsyncMock())

    result = await browser_tool.browser_navigate(KEY, f"{LAB}/tools")

    assert result.startswith(f"Navigated to: {LAB}/tools")
    assert page.storage == {"copilot_token": "lab-t0ken"}
    assert page.gotos == [f"{LAB}/tools", f"{LAB}/tools"]


def test_the_kept_storage_is_indexed_by_origin():
    assert browser_tool._storage_by_origin(STATE) == {LAB: [("copilot_token", "lab-t0ken")]}
    assert browser_tool._storage_by_origin({"origins": [{"origin": LAB, "localStorage": []}]}) == {}
