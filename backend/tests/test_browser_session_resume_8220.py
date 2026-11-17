"""A resumed browser session never hands the next resume a state with 0 sites.

A run resumed twice could come back with "cookies restored (0), local storage of 0 site(s)
restored": one read of the context state that missed the lab origin (Playwright leaves out an origin it read empty
or could not read), or an action on a re-created page before its kept local storage had landed, replaced the kept
signed-in state and was shared with the other processes.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import browser_tool

LAB = "http://10.42.3.17:3000"
KEY = "agent:assignment:run"
TOKEN = {"origin": LAB, "localStorage": [{"name": "copilot_token", "value": "lab-t0ken"}]}
STATE = {"cookies": [{"name": "sid", "value": "c00kie", "domain": "10.42.3.17", "path": "/"}], "origins": [TOKEN]}
EMPTY = {"cookies": [], "origins": []}


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


@pytest.fixture(autouse=True)
def fresh_process(monkeypatch):
    monkeypatch.setattr(browser_tool, "_sessions", {})
    monkeypatch.setattr(browser_tool, "_kept_states", {})
    monkeypatch.setattr(browser_tool, "_known_stamps", {})
    monkeypatch.setattr(browser_tool, "_kept_pages", {})
    monkeypatch.setattr(browser_tool, "_notices", {})


class FakePage:
    """A page on *url* whose local storage is a dict; *denied*: reading it raises SecurityError."""

    def __init__(self, url: str, *, denied: bool = False) -> None:
        self.url = url
        self.storage: dict[str, str] = {}
        self.denied = denied

    async def evaluate(self, script, items=None):
        if self.denied:
            raise RuntimeError("SecurityError: Access is denied for this document.")
        if script == browser_tool._READ_LOCAL_STORAGE_JS:  # the direct read
            return [[name, value] for name, value in self.storage.items()]
        written = 0
        for name, value in items:
            if name not in self.storage:
                self.storage[name] = value
                written += 1
        return written


def _session(page, reads, *, pending: dict | None = None) -> browser_tool.BrowserSession:
    """A session whose context reads its state from *page* when *reads* is ``None``, else returns *reads*."""

    def read():
        if reads is not None:
            return reads
        items = [{"name": n, "value": v} for n, v in page.storage.items()]
        return {"cookies": STATE["cookies"], "origins": [{"origin": LAB, "localStorage": items}] if items else []}

    session = browser_tool.BrowserSession(
        pw=None, browser=None, context=SimpleNamespace(storage_state=AsyncMock(side_effect=read)), page=page
    )
    session.pending_storage = pending or {}
    return session


async def _signed_in() -> None:
    page = FakePage(f"{LAB}/tools")
    page.storage["copilot_token"] = "lab-t0ken"
    await browser_tool._remember_state(KEY, _session(page, None))


async def test_a_read_missing_another_origin_carries_its_kept_storage_over(redis, caplog):
    await _signed_in()
    elsewhere = _session(SimpleNamespace(url="https://docs.example/page"), EMPTY)

    with caplog.at_level(logging.WARNING, logger=browser_tool.logger.name):
        await browser_tool._remember_state(KEY, elsewhere)

    state, page = await browser_tool._shared_state(KEY)
    assert state["origins"] == [TOKEN]
    assert page["url"] == "https://docs.example/page"
    assert KEY in caplog.text and "carried over" in caplog.text


async def test_a_sign_out_on_the_page_origin_is_kept(redis):
    await _signed_in()

    await browser_tool._remember_state(KEY, _session(FakePage(f"{LAB}/login"), None))

    state, _page = await browser_tool._shared_state(KEY)
    assert state["origins"] == []


async def test_an_action_before_the_kept_storage_landed_writes_it_back_first(redis):
    await _signed_in()
    page = FakePage(f"{LAB}/login")  # a re-created session: the token handed to the context did not reach the page
    resumed = _session(page, None, pending=browser_tool._storage_by_origin(STATE))

    await browser_tool._remember_state(KEY, resumed)

    assert page.storage == {"copilot_token": "lab-t0ken"}
    assert resumed.pending_storage == {}
    state, _page = await browser_tool._shared_state(KEY)
    assert state["origins"] == [TOKEN]


async def test_a_page_whose_storage_cannot_be_written_keeps_the_previous_state(redis, caplog):
    await _signed_in()
    resumed = _session(FakePage(f"{LAB}/login", denied=True), EMPTY, pending=browser_tool._storage_by_origin(STATE))

    with caplog.at_level(logging.WARNING, logger=browser_tool.logger.name):
        await browser_tool._remember_state(KEY, resumed)

    resumed.context.storage_state.assert_not_awaited()
    state, page = await browser_tool._shared_state(KEY)
    assert state == STATE
    assert page["url"] == f"{LAB}/tools"
    assert KEY in caplog.text and "not written back" in caplog.text


async def test_two_resumes_in_a_row_keep_the_sign_in(redis):
    await _signed_in()
    for _resume in range(2):
        # Another process: only the shared copy is left.
        browser_tool._kept_states.clear()
        browser_tool._kept_pages.clear()
        kept, _page = await browser_tool._shared_state(KEY)
        browser_tool._kept_states[KEY] = (kept, 0.0)
        page = FakePage(f"{LAB}/login")
        await browser_tool._remember_state(KEY, _session(page, None, pending=browser_tool._storage_by_origin(kept)))

    state, _page = await browser_tool._shared_state(KEY)
    assert state["origins"] == [TOKEN]
