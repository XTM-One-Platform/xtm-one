"""The browser tool's ``network`` action (#11230).

A reconnect retest needs a page cut from the network for a while, then given it back: ``network`` sets
``BrowserContext.set_offline``, keeps it on the session until ``offline=false`` and keeps it with the session's
state, so a session re-created after a resume opens offline too. Nothing here launches a browser: a fake context
records what the tool asked of it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import browser_tool
from app.services.builtin_tools import build_browser_tool
from app.tools.base import ToolCall, ToolResult

KEY = "agent:assignment:run"
STATE = {
    "cookies": [{"name": "sid", "value": "s3cret", "domain": "lab.example", "path": "/"}],
    "origins": [{"origin": "https://lab.example", "localStorage": [{"name": "token", "value": "t0ken"}]}],
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


def _session(url: str = "https://lab.example/chat") -> browser_tool.BrowserSession:
    context = SimpleNamespace(storage_state=AsyncMock(return_value=STATE), set_offline=AsyncMock())
    page = SimpleNamespace(url=url)
    return browser_tool.BrowserSession(pw=None, browser=None, context=context, page=page)


@pytest.fixture
def session(monkeypatch, redis, fresh_process):
    current = _session()
    monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=current))
    return current


async def _call(arguments: dict) -> ToolResult:
    _defs, executor = build_browser_tool("agent:conv")
    return await executor.execute_tool(ToolCall(id="call-1", name="browser", arguments=arguments))


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
        self.context = SimpleNamespace(
            route=AsyncMock(),
            new_page=AsyncMock(return_value=page),
            new_cdp_session=AsyncMock(side_effect=RuntimeError("no devtools in this test")),
            close=AsyncMock(),
        )

        async def new_context(**kwargs):
            self.context_kwargs = kwargs
            return self.context

        self.browser = SimpleNamespace(new_context=new_context, close=AsyncMock(), is_connected=lambda: True)
        self.chromium = SimpleNamespace(launch=AsyncMock(return_value=self.browser))

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


def _forget_process() -> None:
    """The worker hands the run back: the next process has the shared copy only."""
    for registry in (
        browser_tool._kept_states,
        browser_tool._kept_pages,
        browser_tool._known_stamps,
        browser_tool._sessions,
        browser_tool._notices,
    ):
        registry.clear()


def test_the_schema_offers_network_and_its_argument():
    defs, _executor = build_browser_tool("agent:conv")
    schema = defs[0].input_schema
    assert "network" in schema["properties"]["action"]["enum"]
    assert "- network:" in defs[0].description
    assert schema["properties"]["offline"]["type"] == "boolean"


async def test_offline_cuts_the_page_and_is_kept_on_the_session(session):
    result = await _call({"action": "network", "offline": True})

    assert not result.is_error
    session.context.set_offline.assert_awaited_once_with(True)
    assert session.offline is True
    assert "ERR_INTERNET_DISCONNECTED" in result.content


async def test_offline_false_gives_the_network_back(session):
    await _call({"action": "network", "offline": True})
    result = await _call({"action": "network", "offline": False})

    assert not result.is_error
    session.context.set_offline.assert_awaited_with(False)
    assert session.offline is False
    assert "online" in result.content


@pytest.mark.parametrize("arguments", [{}, {"offline": "yes"}, {"offline": 1}])
async def test_a_missing_or_wrong_value_is_refused_before_the_context(session, arguments):
    result = await _call({"action": "network", **arguments})

    assert result.is_error
    assert "network" in result.content
    session.context.set_offline.assert_not_awaited()
    assert session.offline is False


async def test_the_offline_state_is_kept_with_the_shared_state(session):
    await _call({"action": "network", "offline": True})

    shared = await browser_tool._shared_state("agent:conv")  # the session key of build_browser_tool("agent:conv")
    assert shared is not None
    assert shared[1]["offline"] is True


async def test_a_session_re_created_after_a_resume_opens_offline(redis, fresh_process, launch):
    kept = _session()
    kept.offline = True
    await browser_tool._remember_state(KEY, kept)
    _forget_process()

    resumed = await browser_tool._launch_session(KEY)

    assert launch.context_kwargs["offline"] is True
    assert resumed.offline is True
    assert "the page still offline" in (browser_tool.take_notice(KEY) or "")


async def test_a_session_never_cut_opens_online(redis, fresh_process, launch):
    await browser_tool._remember_state(KEY, _session())
    _forget_process()

    resumed = await browser_tool._launch_session(KEY)

    assert launch.context_kwargs["offline"] is False
    assert resumed.offline is False


async def test_offline_set_while_the_state_read_fails_is_restored_after_a_resume(
    redis, fresh_process, launch, monkeypatch
):
    await browser_tool._remember_state(KEY, _session())  # signed in, online
    busy = _session()
    busy.context.storage_state = AsyncMock(side_effect=TimeoutError("page busy"))
    monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=busy))

    await browser_tool.browser_network(KEY, True)
    _forget_process()
    resumed = await browser_tool._launch_session(KEY)

    assert launch.context_kwargs["offline"] is True
    assert launch.context_kwargs["storage_state"] == STATE
    assert resumed.offline is True


async def test_back_online_while_the_state_read_fails_is_not_restored_offline(
    redis, fresh_process, launch, monkeypatch
):
    cut = _session()
    cut.offline = True
    await browser_tool._remember_state(KEY, cut)
    cut.context.storage_state = AsyncMock(side_effect=TimeoutError("page busy"))
    monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=cut))

    await browser_tool.browser_network(KEY, False)
    _forget_process()
    resumed = await browser_tool._launch_session(KEY)

    assert launch.context_kwargs["offline"] is False
    assert resumed.offline is False
