"""The first evaluate after a navigate runs on the document the navigate answered, or refuses (#10013).

A navigate answered "Navigated to: B" and the script run just after it read the page it had left: its relative
fetches went to the previous origin. The first ``evaluate`` after a navigate reads ``location.href``: a page still on
the page the navigate left is given a bounded time to get there, then the script is refused, never run there.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import browser_tool

KEY = "agent:assignment:run"
DARK = "https://dark.lab.example/agents"
LIGHT = "https://light.lab.example/login"


class LaggingPage:
    """A page whose document reaches the navigated URL only after *lag* reads of ``location.href``."""

    def __init__(self, start: str, lag: int = 0) -> None:
        self.url = start
        self.href = start
        self.lag = lag
        self.target: str | None = None
        self.scripts: list[str] = []
        self.time_origin = 0.0  # performance.timeOrigin: the document was created before any navigate of a test
        self.main_frame = SimpleNamespace(parent_frame=None)
        self.on = MagicMock()

    async def goto(self, url, wait_until=None):
        self.url = url  # committed: page.url names it at once
        self.target = url
        if self.lag <= 0:
            self.href = url
        return SimpleNamespace(ok=True)

    async def wait_for_load_state(self, state, timeout=None):
        return None

    async def evaluate(self, script, *args):
        if script == browser_tool._PAGE_HREF_JS:
            current = self.href
            if self.target is not None and self.lag > 0:
                self.lag -= 1
                if self.lag <= 0:
                    self.href = self.target
            return current
        if script == browser_tool._DOCUMENT_ORIGIN_JS:
            return self.time_origin
        self.scripts.append(script)
        return self.href


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(browser_tool, "_sessions", {})
    monkeypatch.setattr(browser_tool, "_notices", {})
    clock = FakeClock()
    monkeypatch.setattr(browser_tool, "_navigated_clock", clock)
    monkeypatch.setattr(browser_tool, "_navigated_sleep", clock.sleep)

    async def allow(url: str) -> str:
        return url

    monkeypatch.setattr(browser_tool, "validate_outbound_url_async", allow)
    monkeypatch.setattr(browser_tool, "_page_title", AsyncMock(return_value="Lab"))
    monkeypatch.setattr(browser_tool, "_remember_state", AsyncMock(return_value={}))
    monkeypatch.setattr(browser_tool, "_share_landed_page", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_tool, "_keep_after_settle", lambda *_a: None)
    # The answer of the navigate names the URL the browser committed, as page.url does before the document moves.
    monkeypatch.setattr(browser_tool, "_landed_url", AsyncMock(side_effect=lambda s: s.page.url))

    def install(page: LaggingPage) -> browser_tool.BrowserSession:
        session = browser_tool.BrowserSession(pw=None, browser=None, context=None, page=page)
        monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=session))
        return session

    return SimpleNamespace(install=install, clock=clock)


async def test_an_evaluate_waits_for_the_document_the_navigate_answered(env):
    page = LaggingPage(DARK, lag=3)
    env.install(page)

    answer = await browser_tool.browser_navigate(KEY, LIGHT)
    result = await browser_tool.browser_evaluate(KEY, "() => location.origin")

    assert answer.startswith(f"Navigated to: {LIGHT}\n")
    assert page.scripts == ["() => location.origin"]
    assert result.endswith(f"\n{LIGHT}")  # run on the light lab's document, never the dark one's


async def test_an_evaluate_on_a_page_still_on_the_previous_document_is_refused_unrun(env):
    page = LaggingPage(DARK, lag=10_000)
    env.install(page)

    await browser_tool.browser_navigate(KEY, LIGHT)
    with pytest.raises(browser_tool.StillOnPreviousPageError) as raised:
        await browser_tool.browser_evaluate(KEY, "() => fetch('/api/v1/agents')")

    assert page.scripts == []
    message = str(raised.value)
    assert f"still on {DARK}" in message and f"not on {LIGHT}" in message
    assert env.clock.now == pytest.approx(browser_tool._NAVIGATED_DOCUMENT_SECONDS)


async def test_the_check_runs_once_per_navigate(env):
    page = LaggingPage(DARK, lag=10_000)
    session = env.install(page)

    await browser_tool.browser_navigate(KEY, LIGHT)
    with pytest.raises(browser_tool.StillOnPreviousPageError):
        await browser_tool.browser_evaluate(KEY, "() => 1")

    assert session.navigated is None
    # The agent was told; not refused twice.
    assert (await browser_tool.browser_evaluate(KEY, "() => 2")).endswith(f"\n{DARK}")
    assert page.scripts == ["() => 2"]


async def test_a_page_that_routed_on_elsewhere_after_the_navigate_runs_the_script(env):
    page = LaggingPage(DARK)
    env.install(page)

    await browser_tool.browser_navigate(KEY, LIGHT)
    page.href = "https://light.lab.example/dashboard"  # the application's own routing after its sign-in

    result = await browser_tool.browser_evaluate(KEY, "() => location.href")
    assert result.endswith("\nhttps://light.lab.example/dashboard")
    assert env.clock.now == 0


async def test_navigating_between_two_origins_ten_times_always_evaluates_on_the_answered_one(env):
    page = LaggingPage(DARK)
    env.install(page)

    for turn in range(10):
        target = LIGHT if turn % 2 == 0 else DARK
        page.lag = turn % 3
        await browser_tool.browser_navigate(KEY, target)
        assert (await browser_tool.browser_evaluate(KEY, "() => location.href")).endswith(f"\n{target}")


LAB_DASHBOARDS = "https://lab.example/dashboards"


async def test_a_page_that_adds_its_default_query_after_the_navigate_runs_the_script(env):
    # The SPA writes its default filter with history.replaceState: the address the page left and the one it shows
    # now are both /dashboards?vis=all, but the document is the one the navigate to /dashboards answered.
    page = LaggingPage(f"{LAB_DASHBOARDS}?vis=all")
    env.install(page)

    await browser_tool.browser_navigate(KEY, LAB_DASHBOARDS)
    page.href = f"{LAB_DASHBOARDS}?vis=all"

    result = await browser_tool.browser_evaluate(KEY, "() => document.title")
    assert result.endswith(f"\n{LAB_DASHBOARDS}?vis=all")  # the fake page answers its address to any script
    assert page.scripts == ["() => document.title"]
    assert env.clock.now == 0


async def test_a_fragment_added_by_the_page_after_the_navigate_runs_the_script(env):
    page = LaggingPage(f"{LAB_DASHBOARDS}#top")
    env.install(page)

    await browser_tool.browser_navigate(KEY, f"{LAB_DASHBOARDS}/")
    page.href = f"{LAB_DASHBOARDS}#top"

    assert (await browser_tool.browser_evaluate(KEY, "() => 1")).endswith(f"\n{LAB_DASHBOARDS}#top")
    assert env.clock.now == 0


async def test_a_page_left_on_another_path_with_a_query_is_still_refused(env):
    left = "https://lab.example/agents?vis=all"
    page = LaggingPage(left, lag=10_000)
    env.install(page)

    await browser_tool.browser_navigate(KEY, LAB_DASHBOARDS)
    with pytest.raises(browser_tool.StillOnPreviousPageError) as raised:
        await browser_tool.browser_evaluate(KEY, "() => 1")

    assert page.scripts == []
    assert f"still on {left}" in str(raised.value)


def test_same_path_ignores_query_and_fragment_but_not_host_or_path():
    assert browser_tool._same_path(LAB_DASHBOARDS, f"{LAB_DASHBOARDS}/?vis=all#x")
    assert not browser_tool._same_path(LAB_DASHBOARDS, "https://lab.example/agents?vis=all")
    assert not browser_tool._same_path(LAB_DASHBOARDS, "https://other.example/dashboards")


LAB_ROOT = "https://lab.example/"
LAB_SIGN_IN = "https://lab.example/login#token=abc"


async def test_a_new_document_the_app_routed_back_to_the_address_it_left_runs_the_script(env):
    # The lab's sign-in URL: the navigate left /, the application signs in and routes the NEW document back to /.
    page = LaggingPage(LAB_ROOT)
    session = env.install(page)

    await browser_tool.browser_navigate(KEY, LAB_SIGN_IN)
    assert session.navigated_at is not None
    page.href = LAB_ROOT
    page.time_origin = session.navigated_at + 40  # created by the navigate

    result = await browser_tool.browser_evaluate(KEY, "() => location.pathname")
    assert page.scripts == ["() => location.pathname"]
    assert result.endswith(f"\n{LAB_ROOT}")
    assert env.clock.now == 0
    assert session.navigated is None and session.navigated_at is None


async def test_the_document_the_navigate_left_at_the_same_address_is_still_refused(env):
    page = LaggingPage(LAB_ROOT, lag=10_000)  # the navigate never replaced the document
    env.install(page)

    await browser_tool.browser_navigate(KEY, LAB_SIGN_IN)
    with pytest.raises(browser_tool.StillOnPreviousPageError):
        await browser_tool.browser_evaluate(KEY, "() => 1")

    assert page.scripts == []


async def test_an_unreadable_document_origin_keeps_the_refusal(env):
    page = LaggingPage(LAB_ROOT, lag=10_000)
    page.time_origin = "not a time"
    env.install(page)

    await browser_tool.browser_navigate(KEY, LAB_SIGN_IN)
    with pytest.raises(browser_tool.StillOnPreviousPageError):
        await browser_tool.browser_evaluate(KEY, "() => 1")


async def test_a_click_after_the_navigate_clears_the_check(env, monkeypatch):
    page = LaggingPage(DARK, lag=10_000)
    page.click = AsyncMock()
    session = env.install(page)

    await browser_tool.browser_navigate(KEY, LIGHT)
    assert session.navigated == (DARK, LIGHT)
    await browser_tool.browser_click(KEY, "a")

    assert session.navigated is None
