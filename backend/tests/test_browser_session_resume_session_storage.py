"""A run resumed in another process gets its page's session storage back with the session.

Playwright's storage state carries cookies and local storage only: a page state held in session storage (the chat
composer draft, the agent selected) was lost at every resume, while the notice said the session was restored. The
session storage of the page's origin is now kept with the kept page, shared Fernet-encrypted with it, written back
to the first page of that origin the re-created session opens, never over what the page holds, and named in the
notice ("session storage of N site(s) restored").
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import browser_tool

KEY = "agent:assignment:run"
LAB = "https://lab.example"
PAGE = f"{LAB}/chat"
STATE = {
    "cookies": [{"name": "sid", "value": "s3cret", "domain": "lab.example", "path": "/"}],
    "origins": [{"origin": LAB, "localStorage": [{"name": "token", "value": "t0ken"}]}],
}
DRAFT = [["copilot:chat-input-draft:new", "hello"], ["copilot:chat-input-draft:agent", "a1"]]


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
    """A page with a session storage and a local storage of its own, read and written by the tool's scripts."""

    def __init__(self, url: str, session_items: list[list[str]] | None = None) -> None:
        self.url = url
        self.session: dict[str, str] = dict(session_items or [])
        self.local: dict[str, str] = {}

    async def evaluate(self, script, arg=None):
        if script is browser_tool._READ_SESSION_STORAGE_JS:
            return [[k, v] for k, v in self.session.items()]
        if script is browser_tool._READ_LOCAL_STORAGE_JS:
            return [[k, v] for k, v in self.local.items()]
        target = (
            self.session
            if script is browser_tool._RESTORE_SESSION_STORAGE_JS
            else self.local
            if script is browser_tool._RESTORE_LOCAL_STORAGE_JS
            else None
        )
        if target is None:
            raise AssertionError("unexpected script")
        written = 0
        for name, value in arg:
            if name not in target:
                target[name] = value
                written += 1
        return written


def _session_on(page: FakePage) -> browser_tool.BrowserSession:
    context = SimpleNamespace(storage_state=AsyncMock(return_value=STATE))
    return browser_tool.BrowserSession(pw=None, browser=None, context=context, page=page)


def _resume_elsewhere() -> None:
    browser_tool._sessions.clear()
    browser_tool._kept_states.clear()
    browser_tool._kept_pages.clear()
    browser_tool._notices.clear()


async def test_the_session_storage_of_the_page_is_kept_and_shared_with_the_page(redis, fresh_process):
    await browser_tool._remember_state(KEY, _session_on(FakePage(PAGE, DRAFT)))

    assert browser_tool._kept_pages[KEY]["session_storage"] == {LAB: DRAFT}
    shared = await browser_tool._shared_entry(KEY)
    assert shared is not None and browser_tool._session_storage_of(shared[1]) == {
        LAB: [tuple(pair) for pair in DRAFT]
    }
    # Shared encrypted only: no value in clear in the store.
    assert all("hello" not in token for token in redis.data.values())


async def test_a_resumed_session_writes_the_session_storage_back_and_says_so(redis, fresh_process):
    await browser_tool._remember_state(KEY, _session_on(FakePage(PAGE, DRAFT)))
    _resume_elsewhere()
    shared = await browser_tool._shared_entry(KEY)
    assert shared is not None
    kept_state, kept_page = shared[0], shared[1]

    notice = browser_tool._restored_notice(KEY, kept_state, kept_page, from_process=False)
    assert "local storage of 1 site(s) restored, session storage of 1 site(s) restored" in notice

    page = FakePage(PAGE)
    session = _session_on(page)
    session.pending_storage = browser_tool._storage_by_origin(kept_state)
    session.pending_session_storage = browser_tool._session_storage_of(kept_page)
    page.goto = AsyncMock()

    await browser_tool._settle_resumed_page(KEY, session, PAGE)

    assert page.session == dict(DRAFT)
    assert session.pending_session_storage == {}


async def test_the_restore_never_overwrites_what_the_page_holds(fresh_process):
    page = FakePage(PAGE, [["copilot:chat-input-draft:new", "typed since"]])
    session = _session_on(page)
    session.pending_session_storage = {LAB: [("copilot:chat-input-draft:new", "hello"), ("x", "1")]}

    written = await browser_tool._restore_session_storage(session, LAB)

    assert written == 1
    assert page.session == {"copilot:chat-input-draft:new": "typed since", "x": "1"}


async def test_a_page_whose_session_storage_cannot_be_read_keeps_the_one_kept_before(redis, fresh_process):
    await browser_tool._remember_state(KEY, _session_on(FakePage(PAGE, DRAFT)))
    busy = FakePage(f"{LAB}/agents")

    async def unreadable(script, arg=None):
        if script is browser_tool._READ_SESSION_STORAGE_JS:
            raise RuntimeError("page busy")
        return []

    busy.evaluate = unreadable
    await browser_tool._remember_state(KEY, _session_on(busy))

    assert browser_tool._kept_pages[KEY]["session_storage"] == {LAB: DRAFT}


async def test_an_emptied_session_storage_is_kept_empty(redis, fresh_process):
    await browser_tool._remember_state(KEY, _session_on(FakePage(PAGE, DRAFT)))
    await browser_tool._remember_state(KEY, _session_on(FakePage(PAGE)))

    assert "session_storage" not in browser_tool._kept_pages[KEY]
    notice = browser_tool._restored_notice(KEY, STATE, browser_tool._kept_pages[KEY], from_process=True)
    assert "session storage" not in notice


def test_a_trimmed_page_keeps_the_session_storage_of_its_own_origin_only():
    page = {"url": PAGE, "session_storage": {LAB: DRAFT, "https://other.example": [["a", "b"]]}}

    assert browser_tool._trimmed_page(page)["session_storage"] == {LAB: DRAFT}
