"""A state read that leaves out the page's own origin never drops its kept sign-in token.

Playwright leaves out of ``storage_state()`` an origin it read empty or could not read. The page's own origin was taken
as read (so that a sign-out there is kept): one such read replaced the kept token with nothing, and the share handed the
run resumed after a rollout a state with 0 sites, signed out on the sign-in page.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import browser_tool

LAB = "http://10.42.3.17:3000"
KEY = "agent:assignment:run"
TOKEN = {"origin": LAB, "localStorage": [{"name": "copilot_token", "value": "lab-t0ken"}]}
SIGNED_IN = {"cookies": [], "origins": [TOKEN]}
ORIGIN_LEFT_OUT = {"cookies": [], "origins": []}


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


def _session(read: dict, page_storage) -> browser_tool.BrowserSession:
    """A session on the lab whose storage_state() answers *read* and whose page's localStorage answers *page_storage*
    (a list of pairs, or an exception to raise)."""
    if isinstance(page_storage, Exception):
        evaluate = AsyncMock(side_effect=page_storage)
    else:
        evaluate = AsyncMock(return_value=page_storage)
    page = SimpleNamespace(url=f"{LAB}/agents", evaluate=evaluate, is_closed=MagicMock(return_value=False))
    session = browser_tool.BrowserSession(
        pw=None,
        browser=SimpleNamespace(is_connected=lambda: True),
        context=SimpleNamespace(storage_state=AsyncMock(return_value=read)),
        page=page,
        redirects=SimpleNamespace(main_frame_refusals=0, refusal_since=lambda mark: None),
    )
    browser_tool._sessions[KEY] = session
    return session


async def _shared_tokens() -> list[str]:
    entry = await browser_tool._shared_entry(KEY)
    assert entry is not None
    return [
        item["value"]
        for o in entry[0].get("origins") or []
        if o.get("origin") == LAB
        for item in o.get("localStorage") or []
        if item.get("name") == "copilot_token"
    ]


async def test_a_read_missing_the_page_origin_keeps_the_token_the_page_holds(redis, fresh_process, caplog):
    session = _session(ORIGIN_LEFT_OUT, [["copilot_token", "lab-t0ken"]])

    with caplog.at_level(logging.WARNING, logger=browser_tool.logger.name):
        await browser_tool._remember_state(KEY, session)

    assert await _shared_tokens() == ["lab-t0ken"]
    assert f"Browser session state of {KEY}: local storage of {LAB} left out of the read, 1 item(s)" in caplog.text


async def test_a_read_missing_the_page_origin_at_the_process_stop_keeps_the_token(redis, fresh_process):
    _session(ORIGIN_LEFT_OUT, [["copilot_token", "lab-t0ken"]])

    assert await browser_tool.keep_open_sessions() == 1
    assert await _shared_tokens() == ["lab-t0ken"]


async def test_a_real_sign_out_on_the_page_stays_signed_out(redis, fresh_process):
    browser_tool._kept_states[KEY] = (SIGNED_IN, 1.0)
    session = _session(ORIGIN_LEFT_OUT, [])

    await browser_tool._remember_state(KEY, session)

    assert await _shared_tokens() == []


async def test_a_page_that_cannot_be_read_carries_the_kept_token_over(redis, fresh_process, caplog):
    browser_tool._kept_states[KEY] = (SIGNED_IN, 1.0)
    session = _session(ORIGIN_LEFT_OUT, RuntimeError("Execution context was destroyed"))

    with caplog.at_level(logging.WARNING, logger=browser_tool.logger.name):
        await browser_tool._remember_state(KEY, session)

    assert await _shared_tokens() == ["lab-t0ken"]
    assert f"local storage of {LAB} not read (" in caplog.text


async def test_a_read_holding_the_page_origin_is_taken_as_it_is(redis, fresh_process):
    session = _session(SIGNED_IN, [["copilot_token", "other"]])

    await browser_tool._remember_state(KEY, session)

    assert await _shared_tokens() == ["lab-t0ken"]
    # No direct localStorage read: the read holding the origin is taken as it is. The session storage read of
    # every kept action (#9561) is the only script the page runs.
    scripts = [call.args[0] for call in session.page.evaluate.await_args_list]
    assert browser_tool._READ_LOCAL_STORAGE_JS not in scripts
    assert scripts == [browser_tool._READ_SESSION_STORAGE_JS]
