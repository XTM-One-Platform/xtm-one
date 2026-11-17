"""A browser session resumed after a rollout keeps a sign-in token written after its last kept action.

An application signed in by a local storage token writes it when its login request answers, after the click (or the
Enter) that sent it has returned and kept the state. The read-only actions that followed (a screenshot, a
get_content) never kept it again, and nothing kept the open sessions when the process stopped: the run resumed in
another process from a state with 0 sites and came back signed out on the sign-in page.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import browser_tool

LAB = "http://10.42.3.17:3000"
KEY = "agent:assignment:run"
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


class SignInPage:
    """A lab page whose login request answers after the click returned: the token lands later."""

    def __init__(self) -> None:
        self.url = f"{LAB}/login"
        self.token_written = False
        self.on = MagicMock()
        self.is_closed = MagicMock(return_value=False)
        self.screenshot = AsyncMock(return_value=b"png")
        self.keyboard = SimpleNamespace(press=AsyncMock())

    async def click(self, selector):
        self.url = f"{LAB}/agents"

    async def wait_for_load_state(self, state):
        return None

    async def title(self):
        return "Agents"


def _signed_in_session(page: SignInPage) -> browser_tool.BrowserSession:
    async def storage_state():
        return SIGNED_IN if page.token_written else SIGNED_OUT

    session = browser_tool.BrowserSession(
        pw=None,
        browser=SimpleNamespace(is_connected=lambda: True),
        context=SimpleNamespace(storage_state=storage_state),
        page=page,
        redirects=SimpleNamespace(main_frame_refusals=0, refusal_since=lambda mark: None),
    )
    browser_tool._sessions[KEY] = session
    return session


async def _shared_origins() -> int:
    entry = await browser_tool._shared_entry(KEY)
    assert entry is not None
    return len(entry[0].get("origins") or [])


async def test_a_screenshot_after_the_sign_in_click_shares_the_token(redis, fresh_process, monkeypatch):
    page = SignInPage()
    _signed_in_session(page)
    monkeypatch.setattr(browser_tool, "_page_title", AsyncMock(return_value="Agents"))

    await browser_tool.browser_click(KEY, "button[type=submit]")
    assert await _shared_origins() == 0  # the login request has not answered yet

    page.token_written = True  # it answers while the agent looks at the page
    await browser_tool.browser_screenshot(KEY)

    assert await _shared_origins() == 1


@pytest.mark.parametrize("action", ["get_content", "console", "hover"])
async def test_every_read_only_action_keeps_the_state(redis, fresh_process, monkeypatch, action):
    page = SignInPage()
    page.url = f"{LAB}/agents"
    page.token_written = True
    page.hover = AsyncMock()
    _signed_in_session(page)
    monkeypatch.setattr(
        browser_tool, "_read_page", AsyncMock(return_value=({"url": page.url, "title": "Agents", "text": ""}, False))
    )
    page.evaluate = AsyncMock()

    if action == "get_content":
        await browser_tool.browser_get_content(KEY)
    elif action == "console":
        await browser_tool.browser_console(KEY)
    else:
        await browser_tool.browser_hover(KEY, "button")

    assert await _shared_origins() == 1


async def test_the_process_stop_keeps_the_token_written_since_the_last_action(redis, fresh_process, caplog):
    page = SignInPage()
    session = _signed_in_session(page)
    await browser_tool.browser_click(KEY, "button[type=submit]")
    assert await _shared_origins() == 0

    page.token_written = True  # the token lands, then the rollout stops the process before any other action
    with caplog.at_level(logging.WARNING, logger=browser_tool.logger.name):
        kept = await browser_tool.keep_open_sessions()

    assert kept == 1
    assert await _shared_origins() == 1
    assert f"Browser session {KEY} kept at the process stop: 1 cookie(s), local storage of 1 site(s)" in caplog.text
    assert not session.closed  # the stop keeps the state, it does not close the session


async def test_the_process_stop_never_overwrites_a_newer_copy_shared_elsewhere(redis, fresh_process, monkeypatch):
    page = SignInPage()
    page.url = f"{LAB}/agents"
    session = _signed_in_session(page)
    session.last_used = 1000.0
    # The run already went on in another process, which shared a newer state.
    monkeypatch.setattr(browser_tool, "_PROCESS_ID", "this-process")
    await browser_tool._share_state(KEY, SIGNED_IN, {"url": f"{LAB}/tools"}, kept_at=2000.0)
    monkeypatch.setattr(browser_tool, "_PROCESS_ID", "stopping-process")

    await browser_tool.keep_open_sessions()

    entry = await browser_tool._shared_entry(KEY)
    assert entry is not None
    assert entry[1]["url"] == f"{LAB}/tools"
    assert len(entry[0]["origins"]) == 1


async def test_a_session_with_a_profile_is_not_kept_at_the_process_stop(redis, fresh_process):
    page = SignInPage()
    page.url = f"{LAB}/agents"
    page.token_written = True
    session = _signed_in_session(page)
    session.profile = SimpleNamespace(name="lab")

    assert await browser_tool.keep_open_sessions() == 0
    assert await browser_tool._shared_entry(KEY) is None


async def test_the_resume_logs_what_it_starts_from(redis, fresh_process, monkeypatch, caplog):
    await browser_tool._share_state(KEY, SIGNED_OUT, {"url": f"{LAB}/login"}, kept_at=1234.0)
    fake_page = SimpleNamespace(
        url="about:blank", on=MagicMock(), set_default_timeout=MagicMock(), set_default_navigation_timeout=MagicMock()
    )
    context = SimpleNamespace(
        route=AsyncMock(), new_page=AsyncMock(return_value=fake_page),
        new_cdp_session=AsyncMock(side_effect=RuntimeError("no devtools")),
    )
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context))
    pw = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
    monkeypatch.setattr(
        "playwright.async_api.async_playwright", lambda: SimpleNamespace(start=AsyncMock(return_value=pw))
    )
    monkeypatch.setattr(browser_tool.RedirectGuard, "attach", AsyncMock())
    monkeypatch.setattr(browser_tool.RedirectGuard, "watch_main_frame", AsyncMock())

    with caplog.at_level(logging.WARNING, logger=browser_tool.logger.name):
        await browser_tool._launch_session(KEY)

    assert (
        f"Browser session {KEY} re-created from the state shared by another process: 0 cookie(s), "
        "local storage of 0 site(s)"
    ) in caplog.text
    assert "at 1234.0" in caplog.text
