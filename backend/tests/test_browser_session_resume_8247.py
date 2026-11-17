"""A browser session re-created after a run came back to a process it had left starts from the newest state.

The kept state lives in this process (``_kept_states``) and, shared, in Redis.
A run handed back by process A, resumed in process B (where it signs
in), then handed to A again (still alive during a rolling deploy) was
re-created from A's older copy: Redis was never read while A had one. And A's
idle cleanup, closing the session the run left there, shared that stale copy
over the one B kept. Both came back signed out: "cookies restored (0), local
storage of 0 site(s) restored".
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import browser_tool

KEY = "agent:assignment:run"
LAB = "http://10.42.3.17:3000"

SIGNED_OUT = {"cookies": [], "origins": []}
SIGNED_IN = {
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
    monkeypatch.setattr(browser_tool, "_PROCESS_ID", "process-a")


class FakeLaunch:
    """``async_playwright()`` recording the context options of the launch."""

    def __init__(self) -> None:
        self.context_kwargs: dict = {}
        page = SimpleNamespace(
            url="about:blank",
            on=MagicMock(),
            set_default_timeout=MagicMock(),
            set_default_navigation_timeout=MagicMock(),
            is_closed=MagicMock(return_value=False),
        )
        context = SimpleNamespace(
            route=AsyncMock(),
            new_page=AsyncMock(return_value=page),
            new_cdp_session=AsyncMock(side_effect=RuntimeError("no devtools in this test")),
            close=AsyncMock(),
        )

        async def new_context(**kwargs):
            self.context_kwargs = kwargs
            return context

        browser = SimpleNamespace(new_context=new_context, close=AsyncMock(), is_connected=lambda: True)
        self.chromium = SimpleNamespace(launch=AsyncMock(return_value=browser))

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


def _session(state: dict, url: str = f"{LAB}/agents", last_used: float = 0.0) -> browser_tool.BrowserSession:
    context = SimpleNamespace(storage_state=AsyncMock(return_value=state), close=AsyncMock())
    page = SimpleNamespace(url=url, is_closed=lambda: False)
    browser = SimpleNamespace(is_connected=lambda: True, close=AsyncMock())
    session = browser_tool.BrowserSession(
        pw=SimpleNamespace(stop=AsyncMock()), browser=browser, context=context, page=page
    )
    session.last_used = last_used
    return session


async def _kept_in_process_b(monkeypatch, state: dict) -> None:
    """The run went on in process B, which kept and shared *state*."""
    kept_here = dict(browser_tool._kept_states), dict(browser_tool._kept_pages)
    monkeypatch.setattr(browser_tool, "_PROCESS_ID", "process-b")
    await browser_tool._remember_state(KEY, _session(state))
    monkeypatch.setattr(browser_tool, "_PROCESS_ID", "process-a")
    browser_tool._kept_states.clear()
    browser_tool._kept_states.update(kept_here[0])
    browser_tool._kept_pages.clear()
    browser_tool._kept_pages.update(kept_here[1])


async def test_a_run_back_in_a_process_it_left_starts_from_the_newer_shared_state(
    redis, fresh_process, launch, monkeypatch, caplog
):
    # Process A kept the session signed out, long ago.
    browser_tool._kept_states[KEY] = (SIGNED_OUT, 1.0)
    browser_tool._kept_pages[KEY] = {"url": f"{LAB}/login"}
    # The run went on in process B and signed in there.
    await _kept_in_process_b(monkeypatch, SIGNED_IN)

    with caplog.at_level(logging.WARNING, logger=browser_tool.logger.name):
        await browser_tool._launch_session(KEY)

    assert launch.context_kwargs["storage_state"] == SIGNED_IN
    notice = browser_tool.take_notice(KEY)
    assert notice is not None
    assert notice.startswith("Session re-created, cookies restored (1), local storage of 1 site(s) restored")
    assert "another server process" in notice
    assert KEY in caplog.text and "another process kept" in caplog.text


async def test_a_process_copy_newer_than_the_shared_one_is_kept(redis, fresh_process, launch, monkeypatch):
    await _kept_in_process_b(monkeypatch, SIGNED_OUT)
    browser_tool._kept_states[KEY] = (SIGNED_IN, 4_000_000_000.0)

    await browser_tool._launch_session(KEY)

    assert launch.context_kwargs["storage_state"] == SIGNED_IN


async def test_this_process_own_shared_copy_never_replaces_its_process_copy(redis, fresh_process, launch):
    await browser_tool._remember_state(KEY, _session(SIGNED_IN))

    await browser_tool._launch_session(KEY)

    assert launch.context_kwargs["storage_state"] == SIGNED_IN
    assert "another server process" not in (browser_tool.take_notice(KEY) or "")


async def test_an_idle_close_never_shares_over_a_newer_state_of_another_process(redis, fresh_process, monkeypatch):
    # The session the run left in process A, its last action long ago.
    browser_tool._sessions[KEY] = _session(SIGNED_OUT, last_used=1.0)
    await _kept_in_process_b(monkeypatch, SIGNED_IN)

    await browser_tool._close_session(KEY)

    shared = await browser_tool._shared_entry(KEY)
    assert shared is not None
    assert shared[0] == SIGNED_IN
    assert shared[2]["writer"] == "process-b"


async def test_an_idle_close_shares_when_its_state_is_the_newest(redis, fresh_process):
    browser_tool._sessions[KEY] = _session(SIGNED_IN, last_used=4_000_000_000.0)

    await browser_tool._close_session(KEY)

    shared = await browser_tool._shared_entry(KEY)
    assert shared is not None
    assert shared[0] == SIGNED_IN
    assert shared[2] == {"writer": "process-a", "kept_at": 4_000_000_000.0}
    assert browser_tool._kept_states[KEY] == (SIGNED_IN, 4_000_000_000.0)


async def test_a_state_shared_before_the_stamp_is_read_as_before(redis, fresh_process):
    from app.core.security import encrypt_value

    redis.data[f"browser:kept_state:{KEY}"] = encrypt_value(json.dumps({"state": SIGNED_IN, "url": f"{LAB}/a"}))

    shared = await browser_tool._shared_entry(KEY)

    assert shared == (SIGNED_IN, {"url": f"{LAB}/a"}, {})
    assert not browser_tool._newer_elsewhere(shared[2], 0.0)
