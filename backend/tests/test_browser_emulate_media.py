"""The browser tool's ``emulate_media`` action (#10675).

A rendered UX review needs the reduced-motion and dark states of a page: ``emulate_media`` sets
``prefers-reduced-motion`` and ``prefers-color-scheme`` through Playwright's ``page.emulate_media``, keeps them for
the session until ``reset`` like the viewport, and keeps them with the session's state, so a session re-created after
a resume opens with them. Nothing here launches a browser: a fake page records what the tool asked of it.
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


def _session(url: str = "https://lab.example/agents") -> browser_tool.BrowserSession:
    context = SimpleNamespace(storage_state=AsyncMock(return_value=STATE))
    page = SimpleNamespace(url=url, emulate_media=AsyncMock())
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


# ── The contract ────────────────────────────────────────────────────────────


def test_the_schema_offers_emulate_media_and_its_arguments():
    defs, _executor = build_browser_tool("agent:conv")
    schema = defs[0].input_schema
    assert "emulate_media" in schema["properties"]["action"]["enum"]
    assert "- emulate_media:" in defs[0].description
    assert schema["properties"]["reduced_motion"]["enum"] == ["reduce", "no-preference"]
    assert schema["properties"]["color_scheme"]["enum"] == ["light", "dark", "no-preference"]
    assert schema["properties"]["reset"]["type"] == "boolean"


# ── The action ──────────────────────────────────────────────────────────────


async def test_reduced_motion_is_emulated_on_the_page_and_kept_on_the_session(session):
    result = await _call({"action": "emulate_media", "reduced_motion": "reduce"})

    assert not result.is_error
    session.page.emulate_media.assert_awaited_once_with(reduced_motion="reduce")
    assert session.media == {"reduced_motion": "reduce"}
    assert "prefers-reduced-motion: reduce" in result.content


async def test_a_second_feature_adds_to_the_first(session):
    await _call({"action": "emulate_media", "reduced_motion": "reduce"})
    result = await _call({"action": "emulate_media", "color_scheme": "dark"})

    assert not result.is_error
    session.page.emulate_media.assert_awaited_with(color_scheme="dark")
    assert session.media == {"reduced_motion": "reduce", "color_scheme": "dark"}
    assert "prefers-reduced-motion: reduce, prefers-color-scheme: dark" in result.content


async def test_reset_stops_every_emulation(session):
    await _call({"action": "emulate_media", "reduced_motion": "reduce", "color_scheme": "dark"})
    result = await _call({"action": "emulate_media", "reset": True})

    assert not result.is_error
    session.page.emulate_media.assert_awaited_with(reduced_motion="null", color_scheme="null")
    assert session.media is None
    assert "reset" in result.content


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"reduced_motion": "less"},
        {"color_scheme": "sepia"},
        {"color_scheme": True},
        {"reduced_motion": "null"},
    ],
)
async def test_a_missing_or_unknown_value_is_refused_before_the_page(session, arguments):
    result = await _call({"action": "emulate_media", **arguments})

    assert result.is_error
    assert "emulate_media" in result.content
    session.page.emulate_media.assert_not_awaited()
    assert session.media is None


# ── Kept with the session's state ───────────────────────────────────────────


async def test_the_emulation_is_kept_with_the_shared_state(session):
    await _call({"action": "emulate_media", "reduced_motion": "reduce"})

    shared = await browser_tool._shared_state("agent:conv")  # the session key of build_browser_tool("agent:conv")
    assert shared is not None
    assert shared[1]["media"] == {"reduced_motion": "reduce"}


async def test_a_session_re_created_after_a_resume_opens_with_the_emulation(redis, fresh_process, launch):
    kept = _session()
    kept.media = {"reduced_motion": "reduce", "color_scheme": "dark"}
    await browser_tool._remember_state(KEY, kept)
    _forget_process()

    resumed = await browser_tool._launch_session(KEY)

    assert launch.context_kwargs["reduced_motion"] == "reduce"
    assert launch.context_kwargs["color_scheme"] == "dark"
    assert resumed.media == {"reduced_motion": "reduce", "color_scheme": "dark"}
    notice = browser_tool.take_notice(KEY) or ""
    assert "emulated media prefers-reduced-motion: reduce, prefers-color-scheme: dark restored" in notice


async def test_a_session_with_no_emulation_opens_with_the_browser_defaults(redis, fresh_process, launch):
    await browser_tool._remember_state(KEY, _session())
    _forget_process()

    resumed = await browser_tool._launch_session(KEY)

    assert "reduced_motion" not in launch.context_kwargs
    assert "color_scheme" not in launch.context_kwargs
    assert resumed.media is None


async def test_an_emulation_set_while_the_state_read_fails_is_restored_after_a_resume(
    redis, fresh_process, launch, monkeypatch
):
    await browser_tool._remember_state(KEY, _session())  # signed in, nothing emulated yet
    busy = _session()
    busy.context.storage_state = AsyncMock(side_effect=TimeoutError("page busy"))
    monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=busy))

    await browser_tool.browser_emulate_media(KEY, reduced_motion="reduce")
    _forget_process()
    resumed = await browser_tool._launch_session(KEY)

    assert launch.context_kwargs["reduced_motion"] == "reduce"
    assert launch.context_kwargs["storage_state"] == STATE
    assert resumed.media == {"reduced_motion": "reduce"}


async def test_a_reset_while_the_state_read_fails_is_not_restored_after_a_resume(
    redis, fresh_process, launch, monkeypatch
):
    emulating = _session()
    emulating.media = {"reduced_motion": "reduce"}
    await browser_tool._remember_state(KEY, emulating)
    emulating.context.storage_state = AsyncMock(side_effect=TimeoutError("page busy"))
    monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=emulating))

    await browser_tool.browser_emulate_media(KEY, reset=True)
    _forget_process()
    resumed = await browser_tool._launch_session(KEY)

    assert "reduced_motion" not in launch.context_kwargs
    assert resumed.media is None


async def test_a_kept_copy_with_an_unknown_value_opens_without_it(redis, fresh_process, launch, monkeypatch):
    kept = _session()
    kept.media = {"reduced_motion": "reduce", "color_scheme": "sepia"}
    await browser_tool._remember_state(KEY, kept)
    _forget_process()

    resumed = await browser_tool._launch_session(KEY)

    assert launch.context_kwargs["reduced_motion"] == "reduce"
    assert "color_scheme" not in launch.context_kwargs
    assert resumed.media == {"reduced_motion": "reduce"}
