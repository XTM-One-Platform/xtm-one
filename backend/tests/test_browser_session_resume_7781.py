"""A browser session re-created in another backend process keeps its sign-in and viewport.

A run resumed after a rollout runs in a new process: the in-memory session
registry and kept states are empty there. The kept state is shared,
encrypted, through Redis, so the next launch restores the cookies, the local
storage and the viewport instead of an empty page at about:blank.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import browser_tool


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttl: dict[str, int] = {}

    async def set(self, key, value, ex=None):
        self.data[key] = value
        self.ttl[key] = ex
        return True

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, key):
        self.data.pop(key, None)
        return 1


STATE = {
    "cookies": [{"name": "sid", "value": "s3cret", "domain": "lab.example", "path": "/"}],
    "origins": [{"origin": "https://lab.example", "localStorage": [{"name": "token", "value": "t0ken"}]}],
}


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
    monkeypatch.setattr(browser_tool, "_next_launch", {})


def _plain_session(url: str = "https://lab.example/flows/map", viewport=None) -> browser_tool.BrowserSession:
    context = SimpleNamespace(storage_state=AsyncMock(return_value=STATE))
    page = SimpleNamespace(url=url)
    return browser_tool.BrowserSession(pw=None, browser=None, context=context, page=page, viewport=viewport)


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


async def test_the_kept_state_is_shared_encrypted_with_its_ttl(redis, fresh_process):
    await browser_tool._remember_state("agent:assignment:run", _plain_session(viewport={"width": 1440, "height": 900}))

    (key, token), = redis.data.items()
    assert key == "browser:kept_state:agent:assignment:run"
    assert redis.ttl[key] == int(browser_tool.KEPT_STATE_SECONDS)
    # Credentials never sit in Redis in clear (invariant 2).
    assert "s3cret" not in token and "t0ken" not in token


async def test_a_session_relaunched_in_another_process_is_signed_in_at_its_viewport(redis, fresh_process, launch):
    await browser_tool._remember_state("agent:assignment:run", _plain_session(viewport={"width": 1440, "height": 900}))
    # The run resumes in a new process: nothing in memory, the shared copy only.
    browser_tool._kept_states.clear()
    browser_tool._kept_pages.clear()

    session = await browser_tool._launch_session("agent:assignment:run")

    assert launch.context_kwargs["storage_state"] == STATE
    assert launch.context_kwargs["viewport"] == {"width": 1440, "height": 900}
    assert session.viewport == {"width": 1440, "height": 900}
    notice = browser_tool.take_notice("agent:assignment:run")
    assert notice is not None
    assert notice.startswith("Session re-created, cookies restored (1), local storage of 1 site(s) restored")
    assert "another server process" in notice
    assert "viewport 1440 x 900 px restored" in notice
    assert "It was on https://lab.example/flows/map." in notice


async def test_a_session_with_nothing_kept_opens_empty_at_the_default_size(redis, fresh_process, launch):
    session = await browser_tool._launch_session("agent:assignment:other")

    assert launch.context_kwargs["storage_state"] is None
    assert launch.context_kwargs["viewport"] == browser_tool.DEFAULT_VIEWPORT
    assert session.viewport is None
    assert browser_tool.take_notice("agent:assignment:other") is None


async def test_closing_the_session_forgets_the_shared_state(redis, fresh_process):
    await browser_tool._remember_state("agent:conv", _plain_session())
    assert redis.data

    await browser_tool.browser_close("agent:conv")

    assert redis.data == {}
    assert "agent:conv" not in browser_tool._kept_states


async def test_a_profile_session_is_never_shared(redis, fresh_process):
    session = _plain_session()
    session.profile = SimpleNamespace(name="corp")

    await browser_tool._remember_state("agent:conv", session)

    assert redis.data == {}


async def test_redis_down_keeps_the_process_copy(monkeypatch, fresh_process):
    async def refused():
        raise ConnectionError("refused")

    monkeypatch.setattr("app.core.redis.redis_client", refused)

    await browser_tool._remember_state("agent:conv", _plain_session())

    assert browser_tool._kept_states["agent:conv"][0] == STATE
    assert await browser_tool._shared_state("agent:conv") is None


async def test_setting_the_viewport_keeps_it_for_the_next_session(redis, fresh_process, monkeypatch):
    session = _plain_session()
    session.page.set_viewport_size = AsyncMock()
    session.page.evaluate = AsyncMock(return_value=[1024, 768])  # the page reports the size at once (#9967)
    monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=session))

    await browser_tool.browser_set_viewport("agent:conv", 1024, 768)

    shared = await browser_tool._shared_state("agent:conv")
    assert shared is not None
    assert shared[1]["viewport"] == {"width": 1024, "height": 768}


async def test_a_viewport_set_while_the_state_read_fails_is_restored_after_a_resume(
    redis, fresh_process, launch, monkeypatch
):
    """#10599: the state read after the viewport timed out, the resumed session opened at 1280 x 720."""
    key = "agent:assignment:run"
    await browser_tool._remember_state(key, _plain_session())  # signed in, no viewport set yet
    session = _plain_session()
    session.context.storage_state = AsyncMock(side_effect=TimeoutError("page busy"))
    session.page.set_viewport_size = AsyncMock()
    session.page.evaluate = AsyncMock(return_value=[1728, 1080])
    monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=session))

    await browser_tool.browser_set_viewport(key, 1728, 1080)
    # The worker hands the run back: the next process has the shared copy only.
    browser_tool._kept_states.clear()
    browser_tool._kept_pages.clear()
    browser_tool._known_stamps.clear()
    browser_tool._sessions.clear()
    browser_tool._notices.clear()

    resumed = await browser_tool._launch_session(key)

    assert launch.context_kwargs["viewport"] == {"width": 1728, "height": 1080}
    assert launch.context_kwargs["storage_state"] == STATE
    assert resumed.viewport == {"width": 1728, "height": 1080}
    assert "viewport 1728 x 1080 px restored" in (browser_tool.take_notice(key) or "")


async def test_a_viewport_set_with_nothing_kept_shares_nothing(redis, fresh_process, monkeypatch):
    session = _plain_session(url="about:blank")
    session.page.set_viewport_size = AsyncMock()
    session.page.evaluate = AsyncMock(return_value=[1728, 1080])
    monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=session))

    await browser_tool.browser_set_viewport("agent:conv", 1728, 1080)

    assert redis.data == {}
    assert "agent:conv" not in browser_tool._kept_pages


def test_the_scale_is_bounded() -> None:
    assert browser_tool.viewport_scale(None) == 1
    assert browser_tool.viewport_scale(2) == 2.0
    for wrong in (0.5, 4, True, "2", float("nan")):
        with pytest.raises(ValueError):
            browser_tool.viewport_scale(wrong)


async def test_a_scale_change_recreates_the_session_signed_in_on_its_page(
    redis: FakeRedis, fresh_process: None, launch: FakeLaunch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """device_scale_factor is a context option: the viewport action re-creates the context at the new ratio."""
    key = "agent:conv"
    old = _plain_session(viewport={"width": 1280, "height": 720})
    old.redirects.close = MagicMock()  # type: ignore[method-assign]
    old.context = SimpleNamespace(storage_state=AsyncMock(return_value=STATE), close=AsyncMock())
    old.browser = SimpleNamespace(close=AsyncMock(), is_connected=lambda: True)
    old.pw = SimpleNamespace(stop=AsyncMock())
    browser_tool._sessions[key] = old
    await browser_tool._remember_state(key, old)  # signed in on its page
    monkeypatch.setattr(browser_tool, "_went_on_elsewhere", AsyncMock(return_value=False))
    monkeypatch.setattr(browser_tool, "_alive", lambda _s: True)
    monkeypatch.setattr(browser_tool, "_ensure_cleanup_running", AsyncMock())
    reopened: list[str] = []

    async def reopen(session_key: str, session: browser_tool.BrowserSession) -> bool:
        if session.resume_url:
            reopened.append(session.resume_url)
            session.resume_url = None
        return False

    monkeypatch.setattr(browser_tool, "_reopen_resumed_page", reopen)
    launch.context.new_page.return_value.set_viewport_size = AsyncMock()
    launch.context.new_page.return_value.evaluate = AsyncMock(return_value=[1440, 900])

    answer = await browser_tool.browser_set_viewport(key, 1440, 900, 2)

    assert launch.context_kwargs["device_scale_factor"] == 2.0
    assert launch.context_kwargs["viewport"] == {"width": 1440, "height": 900}
    assert launch.context_kwargs["storage_state"] == STATE
    assert reopened == ["https://lab.example/flows/map"]
    fresh = browser_tool._sessions[key]
    assert fresh is not old and fresh.scale == 2.0
    assert "at pixel ratio 2" in answer
    assert browser_tool._next_launch == {}


async def test_the_scale_is_restored_after_a_resume(redis: FakeRedis, fresh_process: None, launch: FakeLaunch) -> None:
    key = "agent:assignment:run"
    session = _plain_session(viewport={"width": 1440, "height": 900})
    session.scale = 2.0
    await browser_tool._remember_state(key, session)
    browser_tool._kept_states.clear()
    browser_tool._kept_pages.clear()

    resumed = await browser_tool._launch_session(key)

    assert launch.context_kwargs["device_scale_factor"] == 2.0
    assert resumed.scale == 2.0
    assert "pixel ratio 2 restored" in (browser_tool.take_notice(key) or "")


async def test_a_profile_session_refuses_a_scale_change(fresh_process: None, monkeypatch: pytest.MonkeyPatch) -> None:
    session = _plain_session()
    session.profile = SimpleNamespace(name="corp")
    monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=session))

    with pytest.raises(ValueError, match="browser profile"):
        await browser_tool.browser_set_viewport("agent:conv", 1440, 900, 2)


async def test_a_session_opens_at_ratio_one(redis: FakeRedis, fresh_process: None, launch: FakeLaunch) -> None:
    await browser_tool._launch_session("agent:assignment:other")

    assert launch.context_kwargs["device_scale_factor"] == 1
