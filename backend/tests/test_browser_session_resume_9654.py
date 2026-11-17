"""A sign-in a page writes just after a navigate is kept without another action (#9654).

A page that signs in from its URL (``/login#token=...``) writes its token to local storage from its own script, a
moment after the navigate returned and kept the state at DOMContentLoaded. A run handed back before any other
browser action resumed from that state: 0 sites restored, signed out. The navigate now keeps the state again once
the page settled, and the hand-back keeps every open session before the next worker can claim the run.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import assignment_run_resume, browser_tool

LAB = "http://10.42.3.17:3000"
KEY = "agent:assignment:run"
SIGNED_OUT = {"cookies": [], "origins": []}
SIGNED_IN = {"cookies": [], "origins": [{"origin": LAB, "localStorage": [{"name": "copilot_token", "value": "t0k"}]}]}


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
    monkeypatch.setattr(browser_tool, "_settle_keeps", set())
    monkeypatch.setattr(browser_tool, "_SETTLED_KEEP_SECONDS", 0.01)


class CallbackPage:
    """The sign-in callback page: the token lands once the app's script ran, after the navigate returned."""

    def __init__(self) -> None:
        self.url = f"{LAB}/login"
        self.token_written = False
        self.on = MagicMock()
        self.is_closed = MagicMock(return_value=False)


def _session(page: CallbackPage) -> browser_tool.BrowserSession:
    async def storage_state():
        return SIGNED_IN if page.token_written else SIGNED_OUT

    session = browser_tool.BrowserSession(
        pw=None,
        browser=SimpleNamespace(is_connected=lambda: True),
        context=SimpleNamespace(storage_state=storage_state),
        page=page,
    )
    browser_tool._sessions[KEY] = session
    return session


async def _shared_origins() -> int:
    entry = await browser_tool._shared_entry(KEY)
    assert entry is not None
    return len(entry[0].get("origins") or [])


async def _drain() -> None:
    for task in list(browser_tool._settle_keeps):
        await task


async def test_the_token_written_after_the_navigate_is_kept_once_the_page_settled(redis, fresh_process):
    page = CallbackPage()
    session = _session(page)
    await browser_tool._remember_state(KEY, session)
    assert await _shared_origins() == 0  # kept at DOMContentLoaded, before the app's script ran

    browser_tool._keep_after_settle(KEY, session)
    page.token_written = True  # the app writes its token from the #token= fragment
    await _drain()

    assert await _shared_origins() == 1


async def test_an_action_in_between_makes_the_deferred_keep_stand_down(redis, fresh_process, monkeypatch):
    page = CallbackPage()
    session = _session(page)
    remember = AsyncMock(return_value=None)
    monkeypatch.setattr(browser_tool, "_remember_state", remember)

    browser_tool._keep_after_settle(KEY, session)
    session.last_used += 1.0  # another action used the session: it kept the state itself
    await _drain()

    remember.assert_not_awaited()


async def test_a_closed_session_is_not_kept_again(redis, fresh_process, monkeypatch):
    page = CallbackPage()
    session = _session(page)
    remember = AsyncMock(return_value=None)
    monkeypatch.setattr(browser_tool, "_remember_state", remember)

    browser_tool._keep_after_settle(KEY, session)
    session.closed = True
    await _drain()

    remember.assert_not_awaited()


async def test_a_profile_session_schedules_no_keep(redis, fresh_process):
    page = CallbackPage()
    session = _session(page)
    session.profile = SimpleNamespace(name="lab")

    browser_tool._keep_after_settle(KEY, session)

    assert not browser_tool._settle_keeps


async def test_the_hand_back_keeps_the_open_sessions_first(monkeypatch):
    order: list[str] = []

    async def keep():
        order.append("keep")
        return 1

    monkeypatch.setattr(browser_tool, "keep_open_sessions", keep)
    checkpoint = SimpleNamespace(aclose=AsyncMock(side_effect=lambda flush: order.append("checkpoint")))

    class NoRun:
        async def __aenter__(self):
            return SimpleNamespace(
                execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))
            )

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("app.core.database.async_session", lambda: NoRun())
    monkeypatch.setattr("app.services.orchestration_reconciler.stop_signal", lambda: "SIGTERM")

    await assignment_run_resume.hand_back("00000000-0000-0000-0000-000000000001", checkpoint=checkpoint, attempt=1,
                                          place=None)

    assert order == ["keep", "checkpoint"]


async def test_a_failed_keep_never_stops_the_hand_back(monkeypatch):
    monkeypatch.setattr(browser_tool, "keep_open_sessions", AsyncMock(side_effect=RuntimeError("browser gone")))

    await assignment_run_resume._keep_browser_sessions("run")
