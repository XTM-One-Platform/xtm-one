"""The first action of a resumed browser session runs on its kept page, or says it could not (#9408).

A run resumed in another process whose first action was an evaluate was told "The next action opens it again first"
and then got SecurityError on localStorage: the page was not opened again (a failed goto, an error page) and the
script ran on an opaque document under that promise. The reopen now tries twice, needs an http(s) document, and on
failure the notice says what happened and where the action ran instead. The navigate-first behaviour of #9322 is
unchanged (test_browser_session_resume_9318.py).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import browser_tool

LAB = "http://10.42.3.17:3100"
PAGE = f"{LAB}/videos/e7"
KEY = "agent:assignment:run-9408"
STATE = {
    "cookies": [],
    "origins": [{"origin": LAB, "localStorage": [{"name": "copilot_token", "value": "lab-t0ken"}]}],
}
EXPRESSION = "location.href + ' ' + localStorage.length"


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


@pytest.fixture
def allow_all(monkeypatch):
    monkeypatch.setattr(browser_tool, "_url_check", lambda _session: AsyncMock(side_effect=lambda url: url))


class FakePage:
    """A page on about:blank (opaque origin: local storage throws) whose first gotos can fail or hit an error page."""

    def __init__(self, outcomes: list[str]) -> None:
        self.url = "about:blank"
        self.storage: dict[str, str] = {}
        self.gotos: list[str] = []
        self.outcomes = list(outcomes)  # per goto: "ok", "raise" or "error-page"; "ok" once exhausted
        self.on = MagicMock()
        self.set_default_timeout = MagicMock()
        self.set_default_navigation_timeout = MagicMock()
        self.is_closed = MagicMock(return_value=False)
        self.screenshot = AsyncMock(side_effect=lambda **_kw: self.url.encode())

    async def goto(self, url, wait_until=None):
        self.gotos.append(url)
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if outcome == "raise":
            raise RuntimeError(f"Page.goto: net::ERR_CONNECTION_RESET at {url}")
        self.url = "chrome-error://chromewebdata/" if outcome == "error-page" else url

    async def evaluate(self, script, items=None):
        if not self.url.startswith("http"):
            raise RuntimeError(
                "Page.evaluate: SecurityError: Failed to read the 'localStorage' property from 'Window': "
                "Access is denied for this document."
            )
        if items is not None:  # the platform's write-back
            written = 0
            for name, value in items:
                if name not in self.storage:
                    self.storage[name] = value
                    written += 1
            return written
        return f"{self.url} {len(self.storage)}"


class FakeLaunch:
    def __init__(self, outcomes: list[str]) -> None:
        self.page = FakePage(outcomes)
        self.context = SimpleNamespace(
            route=AsyncMock(),
            new_page=AsyncMock(return_value=self.page),
            new_cdp_session=AsyncMock(side_effect=RuntimeError("no devtools in this test")),
            storage_state=AsyncMock(return_value=STATE),
            close=AsyncMock(),
        )

        async def new_context(**kwargs):
            return self.context

        self.browser = SimpleNamespace(new_context=new_context, close=AsyncMock(), is_connected=lambda: True)
        self.chromium = SimpleNamespace(launch=AsyncMock(return_value=self.browser))

    def __call__(self):
        return self

    async def start(self):
        return self

    async def stop(self):
        return None


def _launch(monkeypatch, outcomes: list[str]) -> FakeLaunch:
    fake = FakeLaunch(outcomes)
    monkeypatch.setattr("playwright.async_api.async_playwright", fake)
    monkeypatch.setattr(browser_tool.RedirectGuard, "attach", AsyncMock())
    monkeypatch.setattr(browser_tool.RedirectGuard, "watch_main_frame", AsyncMock())
    return fake


async def _kept_by_another_process() -> None:
    signed_in = browser_tool.BrowserSession(
        pw=None, browser=None, context=SimpleNamespace(storage_state=AsyncMock(return_value=STATE)),
        page=SimpleNamespace(url=PAGE),
    )
    await browser_tool._remember_state(KEY, signed_in)
    # The run resumes in a new process: the shared copy only.
    browser_tool._kept_states.clear()
    browser_tool._kept_pages.clear()


async def test_the_first_evaluate_after_a_resume_runs_on_the_kept_page(redis, fresh_process, allow_all, monkeypatch):
    launch = _launch(monkeypatch, [])
    await _kept_by_another_process()

    result = await browser_tool.browser_evaluate(KEY, EXPRESSION)

    assert result == f"JavaScript result:\n{PAGE} 1"
    notice = browser_tool.take_notice(KEY) or ""
    assert f"It was on {PAGE}. The next action opens it again first" in notice
    assert launch.page.gotos[0] == PAGE


async def test_a_failed_reopen_is_tried_once_more_before_the_evaluate(redis, fresh_process, allow_all, monkeypatch):
    launch = _launch(monkeypatch, ["raise"])
    await _kept_by_another_process()

    result = await browser_tool.browser_evaluate(KEY, EXPRESSION)

    assert result == f"JavaScript result:\n{PAGE} 1"
    assert launch.page.gotos[:2] == [PAGE, PAGE]
    assert "could not be opened again" not in (browser_tool.take_notice(KEY) or "")


async def test_a_reopen_that_lands_on_an_error_page_is_tried_once_more(redis, fresh_process, allow_all, monkeypatch):
    launch = _launch(monkeypatch, ["error-page"])
    await _kept_by_another_process()

    result = await browser_tool.browser_evaluate(KEY, EXPRESSION)

    assert result == f"JavaScript result:\n{PAGE} 1"
    assert launch.page.gotos[:2] == [PAGE, PAGE]


async def test_a_page_that_cannot_be_opened_again_is_said_instead_of_promised(
    redis, fresh_process, allow_all, monkeypatch
):
    launch = _launch(monkeypatch, ["raise", "error-page"])
    await _kept_by_another_process()

    with pytest.raises(RuntimeError, match="SecurityError"):
        await browser_tool.browser_evaluate(KEY, EXPRESSION)

    assert launch.page.gotos == [PAGE, PAGE]
    notice = browser_tool.take_notice(KEY) or ""
    assert "opens it again first" not in notice
    assert f"It was on {PAGE}. It could not be opened again before this action (it ended on chrome-error" in notice
    assert "this action ran on chrome-error://chromewebdata/. Navigate to the page to go on." in notice


async def test_a_url_policy_refusal_is_said_instead_of_promised(redis, fresh_process, monkeypatch):
    launch = _launch(monkeypatch, [])
    await _kept_by_another_process()
    monkeypatch.setattr(
        browser_tool, "_url_check", lambda _session: AsyncMock(side_effect=ValueError("not a public address"))
    )

    png = await browser_tool.browser_screenshot(KEY)

    assert png == b"about:blank" and launch.page.gotos == []
    notice = browser_tool.take_notice(KEY) or ""
    assert "opens it again first" not in notice
    assert "It could not be opened again before this action (URL policy: not a public address)" in notice
    assert "this action ran on about:blank" in notice


async def test_a_navigate_first_keeps_its_notice(redis, fresh_process, allow_all, monkeypatch):
    launch = _launch(monkeypatch, [])
    await _kept_by_another_process()
    session = await browser_tool._get_or_create_session(KEY)
    session.redirects = SimpleNamespace(main_frame_refusals=0, refusal_since=lambda mark: None)
    monkeypatch.setattr(browser_tool, "_page_title", AsyncMock(return_value="Videos"))

    await browser_tool.browser_navigate(KEY, f"{LAB}/tools")

    assert PAGE not in launch.page.gotos
    notice = browser_tool.take_notice(KEY) or ""
    assert "This navigate went to the page it was given instead" in notice
    assert "could not be opened again" not in notice
