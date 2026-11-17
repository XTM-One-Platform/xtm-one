"""A sign-in page left "Still loading" by a navigate is not aborted by the next navigate (dev:ux-lab-browser-session-drop-11).

A lab opened from its sign-in link (``/login#token=``) on a slow dev server answers "Still loading": the document
arrived, its scripts have not stored the token yet. A second navigate called in the same turn ran as soon as the
first answered and left that document before it signed in: the session went on signed out, and the state kept at the
next graceful rollout stop resumed the run on ``/login`` with 0 cookies. The next navigate now waits for the loading
document first (bounded), keeps the signed-in state, then goes where it is told; a resume starts from that state.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.services import browser_tool

LAB = "http://10.0.1.125:3100"
SIGN_IN = f"{LAB}/login#token=lab-t0ken"
QUALITY = f"{LAB}/settings/automations/quality"
KEY = "agent:assignment:run"
SIGNED_OUT = {"cookies": [], "origins": []}
SIGNED_IN = {
    "cookies": [{"name": "sid", "value": "c00kie", "domain": "10.0.1.125", "path": "/"}],
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
    monkeypatch.setattr(browser_tool, "_RESUME_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(browser_tool, "_SETTLED_KEEP_SECONDS", 3600.0)

    async def allowed(url: str) -> str:
        return url

    monkeypatch.setattr(browser_tool, "validate_outbound_url_async", allowed)
    monkeypatch.setattr(browser_tool, "_page_title", AsyncMock(return_value="XTM One"))


class SlowLabPage:
    """A dev-server page: the sign-in document commits at once and loads (and stores its token) later."""

    def __init__(self) -> None:
        self.url = "about:blank"
        self.main_frame = SimpleNamespace(parent_frame=None)
        self.listeners: dict[str, list] = {}
        self.signed_in = False
        self.aborted_sign_in = False
        self.evaluate = AsyncMock(side_effect=self._evaluate)
        self.wait_for_load_state = AsyncMock()
        self.is_closed = MagicMock(return_value=False)

    def on(self, event: str, handler) -> None:
        self.listeners.setdefault(event, []).append(handler)

    async def _evaluate(self, expression, *_args):
        if expression == browser_tool._PAGE_HREF_JS:
            return self.url
        if expression == browser_tool._PAGE_URL_AND_TITLE_JS:
            return [self.url, "XTM One"]
        return {}

    def _commit(self, url: str) -> None:
        self.url = url
        for handler in self.listeners.get("framenavigated", []):
            handler(self.main_frame)

    def finish_sign_in(self) -> None:
        if self.url != SIGN_IN:  # left before its script ran: the token is never stored
            return
        self.signed_in = True
        self.url = f"{LAB}/"
        for handler in self.listeners.get("domcontentloaded", []):
            handler(self)

    async def goto(self, url, **_kwargs):
        if self.url == SIGN_IN and not self.signed_in:
            self.aborted_sign_in = True
        self._commit(url)
        if url == SIGN_IN:
            raise PlaywrightTimeoutError(f"Page.goto: Timeout {browser_tool.NAVIGATION_TIMEOUT_MS}ms exceeded.")
        for handler in self.listeners.get("domcontentloaded", []):
            handler(self)
        return SimpleNamespace(status=200)


def _open_session(page: SlowLabPage) -> browser_tool.BrowserSession:
    async def storage_state():
        return SIGNED_IN if page.signed_in else SIGNED_OUT

    session = browser_tool.BrowserSession(
        pw=None,
        browser=SimpleNamespace(is_connected=lambda: True),
        context=SimpleNamespace(storage_state=storage_state),
        page=page,
        redirects=SimpleNamespace(main_frame_refusals=0, refusal_since=lambda mark: None),
    )
    session.documents.watch(page)
    browser_tool._sessions[KEY] = session
    return session


async def test_the_next_navigate_waits_for_the_sign_in_page_and_a_graceful_resume_comes_back_signed_in(
    redis, fresh_process
):
    page = SlowLabPage()
    _open_session(page)

    first = await browser_tool.browser_navigate(KEY, SIGN_IN)
    assert "Still loading" in first

    # The second navigate of the same turn; the sign-in script finishes a moment after it was called.
    asyncio.get_running_loop().call_later(0.05, page.finish_sign_in)
    second = await browser_tool.browser_navigate(KEY, QUALITY)

    assert second.startswith(f"Navigated to: {QUALITY}")
    assert page.aborted_sign_in is False
    assert page.signed_in is True

    # A graceful SIGTERM: the stopping worker keeps the open session, another process resumes from Redis.
    assert await browser_tool.keep_open_sessions() == 1
    entry = await browser_tool._shared_entry(KEY)
    assert entry is not None
    state, kept_page, _stamp = entry
    assert len(state["cookies"]) > 0
    assert sum(len(o.get("localStorage") or []) for o in state["origins"]) > 0
    assert kept_page["url"] == QUALITY

    browser_tool._kept_states.clear()  # the resuming process holds no copy of its own
    browser_tool._kept_pages.clear()
    notice = browser_tool._restored_notice(KEY, state, kept_page, from_process=False, reopens=True)
    assert "cookies restored (1)" in notice
    assert "local storage of 1 site(s) restored" in notice
    assert f"It was on {QUALITY}" in notice


async def test_a_sign_in_page_that_never_loads_does_not_hold_the_next_navigate_past_its_bound(
    redis, fresh_process, monkeypatch
):
    monkeypatch.setattr(browser_tool, "NAVIGATION_TIMEOUT_MS", 50)
    page = SlowLabPage()
    session = _open_session(page)

    await browser_tool.browser_navigate(KEY, SIGN_IN)
    assert session.loading_mark is not None

    second = await browser_tool.browser_navigate(KEY, QUALITY)

    assert second.startswith(f"Navigated to: {QUALITY}")
    assert session.loading_mark is None
    assert page.aborted_sign_in is True  # left after the bound, as before


async def test_a_navigate_that_loaded_leaves_nothing_to_wait_for(redis, fresh_process):
    page = SlowLabPage()
    session = _open_session(page)

    await browser_tool.browser_navigate(KEY, QUALITY)

    assert session.loading_mark is None
