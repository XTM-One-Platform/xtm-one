"""A page that removes its own local storage keys (a sign-out, a token cleared on a 401) is kept as read, and the
session re-created after a resume says which keys the page removed: the restore did not lose them, and the run
knows it resumes signed out."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import browser_tool

LAB = "http://10.42.3.17:3000"
KEY = "agent:assignment:run"
COOKIES = [{"name": "sid", "value": "c00kie", "domain": "10.42.3.17", "path": "/"}]


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
    def __init__(self, url: str, storage: dict[str, str]) -> None:
        self.url = url
        self.storage = dict(storage)

    async def evaluate(self, script, items=None):
        if script == browser_tool._READ_LOCAL_STORAGE_JS:
            return [[name, value] for name, value in self.storage.items()]
        return 0


def _session(page: FakePage) -> browser_tool.BrowserSession:
    def read():
        items = [{"name": n, "value": v} for n, v in page.storage.items()]
        return {"cookies": COOKIES, "origins": [{"origin": LAB, "localStorage": items}] if items else []}

    session = browser_tool.BrowserSession(
        pw=None, browser=None, context=SimpleNamespace(storage_state=AsyncMock(side_effect=read)), page=page
    )
    session.pending_storage = {}
    return session


async def _keep(url: str, storage: dict[str, str]) -> None:
    await browser_tool._remember_state(KEY, _session(FakePage(url, storage)))


SIGNED_IN = {"copilot_token": "lab-t0ken", "copilot-theme": "dark", "copilot-nav:expanded": "true"}
SIGNED_OUT = {"copilot-theme": "dark", "copilot-nav:expanded": "true"}


async def test_a_key_the_page_removed_is_named_in_the_shared_page(redis):
    await _keep(f"{LAB}/tools", SIGNED_IN)
    await _keep(f"{LAB}/login", SIGNED_OUT)

    state, page = await browser_tool._shared_state(KEY)

    names = {item["name"] for item in state["origins"][0]["localStorage"]}
    assert names == set(SIGNED_OUT)
    assert page["storage_removed"] == ["copilot_token"]
    assert "lab-t0ken" not in str(page)


async def test_the_removed_keys_stay_named_while_absent(redis):
    await _keep(f"{LAB}/tools", SIGNED_IN)
    await _keep(f"{LAB}/login", SIGNED_OUT)
    await _keep(f"{LAB}/login", SIGNED_OUT)

    _state, page = await browser_tool._shared_state(KEY)
    assert page["storage_removed"] == ["copilot_token"]


async def test_a_sign_in_again_clears_the_removed_keys(redis):
    await _keep(f"{LAB}/tools", SIGNED_IN)
    await _keep(f"{LAB}/login", SIGNED_OUT)
    await _keep(f"{LAB}/tools", SIGNED_IN)

    _state, page = await browser_tool._shared_state(KEY)
    assert "storage_removed" not in page


async def test_a_storage_emptied_whole_names_every_key(redis):
    await _keep(f"{LAB}/tools", {"copilot_token": "lab-t0ken"})
    await _keep(f"{LAB}/login", {})

    _state, page = await browser_tool._shared_state(KEY)
    assert page["storage_removed"] == ["copilot_token"]


async def test_nothing_removed_names_nothing(redis):
    await _keep(f"{LAB}/tools", SIGNED_IN)
    await _keep(f"{LAB}/agents", SIGNED_IN)

    _state, page = await browser_tool._shared_state(KEY)
    assert "storage_removed" not in page


def test_the_notice_names_the_removed_keys_and_does_not_promise_a_sign_in():
    state = {"cookies": COOKIES, "origins": [{"origin": LAB, "localStorage": [{"name": "copilot-theme", "value": "d"}]}]}
    page = {"url": f"{LAB}/login", "storage_removed": ["copilot_token"]}

    notice = browser_tool._restored_notice(KEY, state, page, True, reopens=True)

    assert "removed copilot_token" in notice
    assert "sign in again" in notice
    assert "signed in." not in notice


def test_the_notice_without_removed_keys_is_unchanged():
    state = {"cookies": COOKIES, "origins": []}

    notice = browser_tool._restored_notice(KEY, state, {"url": f"{LAB}/tools"}, True, reopens=True)

    assert "removed" not in notice
    assert notice.endswith("The next action opens it again first, signed in.")
