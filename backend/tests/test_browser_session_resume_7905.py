"""A plain browser session signed in on a lab preview comes back signed in in another process.

The platform shares a session's kept state through Redis, but a state over the
shared cap was dropped whole and silently, and a sign-in submitted with Enter
was never kept: the run resumed after a rollout opened the preview signed out.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import browser_tool

LAB = "http://10.42.3.17:3000"


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
    """Empty in-memory registries, as in a process that never saw the session."""
    monkeypatch.setattr(browser_tool, "_sessions", {})
    monkeypatch.setattr(browser_tool, "_kept_states", {})
    monkeypatch.setattr(browser_tool, "_known_stamps", {})
    monkeypatch.setattr(browser_tool, "_kept_pages", {})
    monkeypatch.setattr(browser_tool, "_notices", {})


def _lab_state(extra_origins: int = 0, size: int = 0) -> dict:
    origins = [{"origin": LAB, "localStorage": [{"name": "copilot_token", "value": "lab-t0ken"}]}]
    origins += [
        {"origin": f"https://site{i}.example", "localStorage": [{"name": "blob", "value": "x" * size}]}
        for i in range(extra_origins)
    ]
    return {"cookies": [{"name": "sid", "value": "c00kie", "domain": "10.42.3.17", "path": "/"}], "origins": origins}


def _session(state: dict, url: str = f"{LAB}/tools") -> browser_tool.BrowserSession:
    context = SimpleNamespace(storage_state=AsyncMock(return_value=state))
    page = SimpleNamespace(url=url, keyboard=SimpleNamespace(press=AsyncMock()))
    return browser_tool.BrowserSession(
        pw=None, browser=None, context=context, page=page, viewport={"width": 1024, "height": 800}
    )


async def test_a_lab_preview_sign_in_is_read_back_in_another_process(redis, fresh_process):
    await browser_tool._remember_state("agent:assignment:run", _session(_lab_state()))
    browser_tool._kept_states.clear()
    browser_tool._kept_pages.clear()

    shared = await browser_tool._shared_state("agent:assignment:run")

    assert shared is not None
    state, page = shared
    assert state["origins"][0]["origin"] == LAB
    assert state["origins"][0]["localStorage"][0]["value"] == "lab-t0ken"
    assert page == {"viewport": {"width": 1024, "height": 800}, "url": f"{LAB}/tools"}


async def test_an_oversized_state_is_shared_trimmed_to_the_page_origin(redis, fresh_process, monkeypatch):
    monkeypatch.setattr(browser_tool, "_MAX_SHARED_STATE_BYTES", 20_000)
    big = _lab_state(extra_origins=5, size=10_000)

    await browser_tool._remember_state("agent:assignment:run", _session(big))

    shared = await browser_tool._shared_state("agent:assignment:run")
    assert shared is not None, "an oversized state must never be dropped whole"
    state, page = shared
    assert [o["origin"] for o in state["origins"]] == [LAB]
    assert state["cookies"] == big["cookies"]
    assert page["url"] == f"{LAB}/tools"
    # This process keeps the whole state.
    assert browser_tool._kept_states["agent:assignment:run"][0] == big


async def test_a_state_too_large_even_trimmed_is_logged(redis, fresh_process, monkeypatch, caplog):
    monkeypatch.setattr(browser_tool, "_MAX_SHARED_STATE_BYTES", 10)

    with caplog.at_level(logging.WARNING, logger=browser_tool.logger.name):
        await browser_tool._remember_state("agent:assignment:run", _session(_lab_state()))

    assert redis.data == {}
    assert "agent:assignment:run" in caplog.text and "not shared" in caplog.text


async def test_a_redis_failure_is_logged_with_the_session_key(monkeypatch, fresh_process, caplog):
    async def refused():
        raise ConnectionError("refused")

    monkeypatch.setattr("app.core.redis.redis_client", refused)

    with caplog.at_level(logging.WARNING, logger=browser_tool.logger.name):
        await browser_tool._remember_state("agent:assignment:run", _session(_lab_state()))
        assert await browser_tool._shared_state("agent:assignment:run") is None

    assert "agent:assignment:run not shared" in caplog.text
    assert "agent:assignment:run not read back" in caplog.text


async def test_a_sign_in_submitted_with_enter_is_kept(redis, fresh_process, monkeypatch):
    session = _session(_lab_state())
    monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=session))

    await browser_tool.browser_press("agent:assignment:run", "Enter")

    session.page.keyboard.press.assert_awaited_once_with("Enter")
    shared = await browser_tool._shared_state("agent:assignment:run")
    assert shared is not None
    assert shared[0]["origins"][0]["localStorage"][0]["value"] == "lab-t0ken"


def test_the_origin_of_a_lab_preview_keeps_its_port():
    assert browser_tool._origin_of(f"{LAB}/tools?x=1") == LAB
    assert browser_tool._origin_of("about:blank") == ""
