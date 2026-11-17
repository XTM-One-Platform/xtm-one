"""A browser session of this process is not used once the run went on in another process (#9935).

A run handed to another worker and back found its session of this process still open on the page it had left: the
navigate the other process made in between answered the URL it reached (origin A), and the next evaluate here read
``location.href`` of the previous page (origin B). The session is now re-created from the newer shared copy.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import browser_tool

KEY = "agent:assignment:run"
ORIGIN_A = "http://10.42.3.17:3100"
ORIGIN_B = "http://10.42.3.18:3100"
STATE = {"cookies": [], "origins": []}


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
def process(monkeypatch):
    monkeypatch.setattr(browser_tool, "_sessions", {})
    monkeypatch.setattr(browser_tool, "_kept_states", {})
    monkeypatch.setattr(browser_tool, "_known_stamps", {})
    monkeypatch.setattr(browser_tool, "_kept_pages", {})
    monkeypatch.setattr(browser_tool, "_notices", {})
    monkeypatch.setattr(browser_tool, "_ensure_cleanup_running", AsyncMock())
    monkeypatch.setattr(browser_tool, "_PROCESS_ID", "this-process")
    relaunched = SimpleNamespace(name="relaunched")
    launch = AsyncMock(return_value=relaunched)
    monkeypatch.setattr(browser_tool, "_launch_session", launch)
    close = AsyncMock()
    monkeypatch.setattr(browser_tool, "_close_session", close)
    return SimpleNamespace(launch=launch, close=close, relaunched=relaunched)


def _open_session_on_b(kept_at: float) -> browser_tool.BrowserSession:
    """The session this process holds: on origin B, its state kept here at *kept_at*."""
    session = browser_tool.BrowserSession(
        pw=None,
        browser=SimpleNamespace(is_connected=lambda: True),
        context=SimpleNamespace(),
        page=SimpleNamespace(url=f"{ORIGIN_B}/flows/map?view=fleets&vis=mine", is_closed=MagicMock(return_value=False)),
        redirects=SimpleNamespace(main_frame_refusals=0, refusal_since=lambda mark: None),
    )
    browser_tool._sessions[KEY] = session
    browser_tool._kept_states[KEY] = (STATE, kept_at)
    browser_tool._kept_pages[KEY] = {"url": session.page.url}
    return session


async def _shared_by(monkeypatch, writer: str, kept_at: float, url: str) -> None:
    monkeypatch.setattr(browser_tool, "_PROCESS_ID", writer)
    await browser_tool._share_state(KEY, STATE, {"url": url}, kept_at=kept_at)
    monkeypatch.setattr(browser_tool, "_PROCESS_ID", "this-process")


async def test_a_session_left_behind_by_a_run_that_went_on_elsewhere_is_re_created(redis, process, monkeypatch):
    now = time.time()
    _open_session_on_b(kept_at=now - 60)
    await _shared_by(monkeypatch, "other-process", now - 5, f"{ORIGIN_A}/flows/map?view=fleets")

    session = await browser_tool._get_or_create_session(KEY)

    assert session is process.relaunched
    process.close.assert_awaited_once_with(KEY)
    process.launch.assert_awaited_once_with(KEY)


async def test_a_session_whose_copy_this_process_shared_last_is_kept(redis, process, monkeypatch):
    now = time.time()
    open_session = _open_session_on_b(kept_at=now - 5)
    await _shared_by(monkeypatch, "this-process", now - 5, f"{ORIGIN_B}/flows/map")

    assert await browser_tool._get_or_create_session(KEY) is open_session
    process.launch.assert_not_awaited()


async def test_a_copy_another_process_shared_before_this_one_kept_its_own_keeps_the_session(
    redis, process, monkeypatch
):
    now = time.time()
    open_session = _open_session_on_b(kept_at=now - 5)
    await _shared_by(monkeypatch, "other-process", now - 60, f"{ORIGIN_A}/flows/map")

    assert await browser_tool._get_or_create_session(KEY) is open_session
    process.launch.assert_not_awaited()


async def test_a_shared_copy_that_cannot_be_read_keeps_the_session(process, monkeypatch):
    async def unreachable():
        raise ConnectionError("redis down")

    monkeypatch.setattr("app.core.redis.redis_client", unreachable)
    open_session = _open_session_on_b(kept_at=time.time() - 5)

    assert await browser_tool._get_or_create_session(KEY) is open_session
    process.launch.assert_not_awaited()
