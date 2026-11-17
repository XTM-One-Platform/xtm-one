"""A sign-in written by ``evaluate`` or ``type`` is kept for a run resumed in another process.

The platform keeps the state after navigate, click, press and viewport. A
reviewer signed in on a lab preview with an ``evaluate`` script (the login API
fetched, the token written to ``localStorage``); the run was resumed in another
process before any navigate or click, and the session came back with the older,
signed-out state: "cookies restored (0), local storage of 0 site(s) restored".
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import browser_tool

LAB = "http://10.42.3.17:3000"
KEY = "agent:assignment:run"


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


SIGNED_OUT: dict = {"cookies": [], "origins": []}
SIGNED_IN: dict = {
    "cookies": [],
    "origins": [{"origin": LAB, "localStorage": [{"name": "copilot_token", "value": "lab-t0ken"}]}],
}


def _session(monkeypatch, page: SimpleNamespace) -> browser_tool.BrowserSession:
    """A session on the lab's /register page, signed out until its page acts."""
    states = [SIGNED_OUT]
    page.url = f"{LAB}/register"
    context = SimpleNamespace(storage_state=AsyncMock(side_effect=lambda: states[-1]))
    session = browser_tool.BrowserSession(
        pw=None, browser=None, context=context, page=page, viewport={"width": 1024, "height": 768}
    )
    session.signs_in = lambda: states.append(SIGNED_IN)  # type: ignore[attr-defined]
    monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=session))
    return session


async def _token_read_back() -> str | None:
    """The token a process that never saw the session reads back."""
    browser_tool._kept_states.clear()
    browser_tool._kept_pages.clear()
    shared = await browser_tool._shared_state(KEY)
    if shared is None:
        return None
    for origin in shared[0].get("origins") or []:
        for item in origin.get("localStorage") or []:
            if item.get("name") == "copilot_token":
                return item.get("value")
    return None


async def test_a_sign_in_written_by_evaluate_is_kept(redis, fresh_process, monkeypatch):
    page = SimpleNamespace()
    session = _session(monkeypatch, page)
    await browser_tool._remember_state(KEY, session)  # the last navigate: signed out

    async def script(_expression):
        session.signs_in()
        return {"ok": True}

    page.evaluate = script

    await browser_tool.browser_evaluate(KEY, "fetch('/api/v1/auth/login').then(...)")

    assert await _token_read_back() == "lab-t0ken"


async def test_a_sign_in_by_a_script_that_navigated_is_kept(redis, fresh_process, monkeypatch):
    page = SimpleNamespace()
    session = _session(monkeypatch, page)
    navigated = RuntimeError("Execution context was destroyed, most likely because of a navigation")

    async def script(_expression):
        session.signs_in()
        raise navigated

    page.evaluate = script
    monkeypatch.setattr(browser_tool, "_is_navigation_race", lambda exc: exc is navigated)
    monkeypatch.setattr(browser_tool, "_interrupted_script_message", AsyncMock(return_value="navigated"))

    with pytest.raises(browser_tool.PageNavigationError):
        await browser_tool.browser_evaluate(KEY, "form.submit()")

    assert await _token_read_back() == "lab-t0ken"


async def test_a_value_typed_into_a_page_that_stores_it_is_kept(redis, fresh_process, monkeypatch):
    page = SimpleNamespace()
    session = _session(monkeypatch, page)

    async def fill(_selector, _text):
        session.signs_in()

    page.fill = fill
    page.focus = AsyncMock()

    await browser_tool.browser_type(KEY, "#token", "lab-t0ken")

    assert await _token_read_back() == "lab-t0ken"
