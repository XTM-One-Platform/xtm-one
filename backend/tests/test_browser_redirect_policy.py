"""Every redirect hop of the browser tool's Chromium is held to the outbound URL policy.

Playwright runs a route handler for the first URL of a request only, so the
route filter never saw a redirect: one ``302`` after a validated navigate
reached an address the policy refuses. The session's :class:`RedirectGuard`
sits in Chromium's own request interception (CDP ``Fetch``), where a hop is
paused before it is sent: a first URL is continued at once and left to the
route filter, a hop is sent only when the navigate's check allows it.

These tests drive the guard with a double of the CDP session; the same
behaviour against a real headless Chromium and loopback servers is
``test_browser_redirect_chromium.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Error as PlaywrightError

from app.services import browser_tool

MAIN_FRAME = "A7912D6B61074CCCD3D62DEF14241935"
PUBLIC_URL = "https://news.example.com/story"
HOP_URL = "http://169.254.169.254/latest/meta-data/"
REFUSAL = "URL targets a private or non-routable address: 169.254.169.254"


class FakeCDPSession:
    """The slice of a Playwright CDP session the guard drives."""

    def __init__(self, *, failing: tuple[str, ...] = ()) -> None:
        self.sent: list[tuple[str, dict | None]] = []
        self.handlers: dict[str, object] = {}
        self.detached = False
        self._failing = failing

    def on(self, event: str, handler) -> None:
        self.handlers[event] = handler

    async def send(self, method: str, params: dict | None = None) -> dict:
        self.sent.append((method, params))
        if method in self._failing:
            raise PlaywrightError(f"Protocol error ({method}): Target closed")
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"targetId": MAIN_FRAME, "type": "page"}}
        return {}

    async def detach(self) -> None:
        self.detached = True

    def releases(self) -> list[tuple[str, dict | None]]:
        return [(m, p) for m, p in self.sent if m in ("Fetch.continueRequest", "Fetch.failRequest")]


@pytest.fixture
def policy(monkeypatch):
    """The outbound URL policy, without DNS: every URL but ``HOP_URL`` is allowed."""
    checked: list[str] = []

    async def validate(url: str) -> str:
        checked.append(url)
        if url == HOP_URL:
            raise ValueError(REFUSAL)
        return url

    monkeypatch.setattr(browser_tool, "validate_outbound_url_async", validate)
    return checked


async def _attached(cdp: FakeCDPSession) -> browser_tool.RedirectGuard:
    guard = browser_tool.RedirectGuard()
    await guard.attach(SimpleNamespace(new_browser_cdp_session=AsyncMock(return_value=cdp)))
    await guard.watch_main_frame(SimpleNamespace(new_cdp_session=AsyncMock(return_value=cdp)), page=object())
    cdp.sent.clear()
    return guard


def _emit(
    cdp: FakeCDPSession,
    url: str,
    *,
    request_id: str = "interception-job-7.1",
    redirected: bool = True,
    frame: str = MAIN_FRAME,
    resource_type: str = "Document",
) -> None:
    """Chromium pauses one request."""
    event = {
        "requestId": request_id,
        "request": {"url": url, "method": "GET", "headers": {}},
        "frameId": frame,
        "resourceType": resource_type,
        "networkId": "64AA7200F1E2CA0FD038256552717FA6",
    }
    if redirected:
        event["redirectedRequestId"] = "interception-job-7.0"
    cdp.handlers["Fetch.requestPaused"](event)


def _guard_of(cdp: FakeCDPSession) -> browser_tool.RedirectGuard:
    """The guard whose pause handler *cdp* holds."""
    return cdp.handlers["Fetch.requestPaused"].__self__


async def _pause(cdp: FakeCDPSession, url: str, **kwargs) -> tuple[str, dict | None]:
    """Chromium pauses one request; await the guard's decision on it and return how it was released.

    The decision task itself is awaited, never a wall-clock wait: a busy test
    worker makes the test slower, not red.
    """
    guard = _guard_of(cdp)
    before = set(guard._decisions)
    _emit(cdp, url, **kwargs)
    (decision,) = guard._decisions - before
    await decision
    return cdp.releases()[-1]


async def _released(cdp: FakeCDPSession, count: int) -> list[tuple[str, dict | None]]:
    """Await every decision the guard still has in flight, then the *count* releases they sent."""
    await asyncio.gather(*_guard_of(cdp)._decisions)
    assert len(cdp.releases()) == count
    return cdp.releases()


# ── the guard ────────────────────────────────────────────────────────────────


async def test_the_guard_pauses_every_request_of_the_browser_before_it_is_sent():
    cdp = FakeCDPSession()
    browser = SimpleNamespace(new_browser_cdp_session=AsyncMock(return_value=cdp))

    await browser_tool.RedirectGuard().attach(browser)

    browser.new_browser_cdp_session.assert_awaited_once_with()
    assert cdp.sent == [("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})]
    assert "Fetch.requestPaused" in cdp.handlers


async def test_the_main_frame_is_read_off_the_page_target_and_the_page_session_released():
    cdp = FakeCDPSession()
    context = SimpleNamespace(new_cdp_session=AsyncMock(return_value=cdp))
    page = object()

    guard = browser_tool.RedirectGuard()
    await guard.watch_main_frame(context, page)

    context.new_cdp_session.assert_awaited_once_with(page)
    assert cdp.sent == [("Target.getTargetInfo", None)]
    assert cdp.detached is True
    assert guard._main_frame_id == MAIN_FRAME


async def test_a_first_url_is_continued_without_a_check(policy):
    cdp = FakeCDPSession()
    await _attached(cdp)

    release = await _pause(cdp, "http://10.0.0.5/admin", redirected=False)

    # The route filter decides a first URL, as before: the guard adds nothing to it.
    assert release == ("Fetch.continueRequest", {"requestId": "interception-job-7.1"})
    assert policy == []


async def test_a_hop_the_policy_allows_is_sent(policy):
    cdp = FakeCDPSession()
    guard = await _attached(cdp)

    release = await _pause(cdp, PUBLIC_URL)

    assert release == ("Fetch.continueRequest", {"requestId": "interception-job-7.1"})
    assert policy == [PUBLIC_URL]
    assert guard.main_frame_refusals == 0


async def test_a_hop_the_policy_refuses_is_never_sent_and_is_named(policy):
    cdp = FakeCDPSession()
    guard = await _attached(cdp)

    release = await _pause(cdp, HOP_URL)

    assert release == ("Fetch.failRequest", {"requestId": "interception-job-7.1", "errorReason": "Aborted"})
    assert ("Fetch.continueRequest", {"requestId": "interception-job-7.1"}) not in cdp.sent
    refusal = guard.refusal_since(0)
    assert refusal == browser_tool.RedirectRefusal(HOP_URL, REFUSAL)
    assert refusal.describe() == f"Navigation blocked by URL policy: redirected to {HOP_URL}: {REFUSAL}"


@pytest.mark.parametrize(
    ("frame", "resource_type"),
    [("C4523CFFE0E0B7E1B7E6A27D3E1F2C41", "Document"), (MAIN_FRAME, "Image"), (MAIN_FRAME, "XHR")],
    ids=["iframe", "image", "xhr"],
)
async def test_a_refused_hop_that_is_not_the_page_navigation_is_refused_without_naming_it(
    policy, frame, resource_type
):
    cdp = FakeCDPSession()
    guard = await _attached(cdp)

    release = await _pause(cdp, HOP_URL, frame=frame, resource_type=resource_type)

    assert release[0] == "Fetch.failRequest"
    # Only the page's own navigation is reported back to the action that caused it.
    assert guard.main_frame_refusals == 0
    assert guard.refusal_since(0) is None


async def test_a_hop_whose_check_does_not_answer_in_time_is_refused(monkeypatch):
    monkeypatch.setattr(browser_tool, "ACTION_TIMEOUT_MS", 50)

    async def hang(url: str) -> str:
        await asyncio.sleep(60)
        return url

    monkeypatch.setattr(browser_tool, "validate_outbound_url_async", hang)
    cdp = FakeCDPSession()
    guard = await _attached(cdp)

    release = await _pause(cdp, PUBLIC_URL)

    assert release[0] == "Fetch.failRequest"
    assert guard.refusal_since(0).reason == "its address could not be checked within 0.05 s"
    guard.close()


async def test_at_most_a_bounded_number_of_hops_are_checked_at_a_time(monkeypatch):
    answer = asyncio.Event()
    full = asyncio.Event()
    in_flight: list[str] = []
    most = 0

    async def slow_resolver(url: str) -> str:
        nonlocal most
        in_flight.append(url)
        most = max(most, len(in_flight))
        if len(in_flight) == browser_tool.MAX_CONCURRENT_HOP_CHECKS:
            full.set()
        await answer.wait()
        in_flight.remove(url)
        return url

    monkeypatch.setattr(browser_tool, "validate_outbound_url_async", slow_resolver)
    cdp = FakeCDPSession()
    await _attached(cdp)

    for number in range(10):
        _emit(cdp, f"{PUBLIC_URL}?hop={number}", request_id=f"interception-job-{number}.1")
    await full.wait()
    for _ in range(10):
        await asyncio.sleep(0)  # every other hop has had its turn at the semaphore

    # A page decides how many hops it makes, not how many resolver threads run.
    assert len(in_flight) == browser_tool.MAX_CONCURRENT_HOP_CHECKS
    assert cdp.releases() == []
    answer.set()
    releases = await _released(cdp, 10)
    assert {method for method, _ in releases} == {"Fetch.continueRequest"}
    assert most == browser_tool.MAX_CONCURRENT_HOP_CHECKS


async def test_a_check_keeps_its_place_until_its_resolver_returns(monkeypatch):
    monkeypatch.setattr(browser_tool, "ACTION_TIMEOUT_MS", 50)
    monkeypatch.setattr(browser_tool, "MAX_CONCURRENT_HOP_CHECKS", 1)
    resolver_returns = asyncio.Event()
    stalled = f"{PUBLIC_URL}?stalled"

    async def resolver(url: str) -> str:
        if url == stalled:
            await resolver_returns.wait()
        return url

    monkeypatch.setattr(browser_tool, "validate_outbound_url_async", resolver)
    cdp = FakeCDPSession()
    guard = await _attached(cdp)

    first = await _pause(cdp, stalled, request_id="interception-job-1.1")
    # Its resolver still runs: the only place stays taken, so the next hop is refused in time.
    second = await _pause(cdp, PUBLIC_URL, request_id="interception-job-2.1")
    stalled_checks = set(guard._checks)
    resolver_returns.set()
    # Its place is freed by the check's own done callback, which runs before this wait returns.
    await asyncio.wait(stalled_checks)
    third = await _pause(cdp, PUBLIC_URL, request_id="interception-job-3.1")

    assert first == ("Fetch.failRequest", {"requestId": "interception-job-1.1", "errorReason": "Aborted"})
    assert second == ("Fetch.failRequest", {"requestId": "interception-job-2.1", "errorReason": "Aborted"})
    assert third == ("Fetch.continueRequest", {"requestId": "interception-job-3.1"})
    assert [refusal.reason for _, refusal in guard._recent] == ["its address could not be checked within 0.05 s"] * 2
    assert guard._checks == set()


async def test_a_hop_whose_check_fails_is_refused(monkeypatch):
    async def broken(url: str) -> str:
        raise OSError("[Errno 24] Too many open files")

    monkeypatch.setattr(browser_tool, "validate_outbound_url_async", broken)
    cdp = FakeCDPSession()
    guard = await _attached(cdp)

    release = await _pause(cdp, PUBLIC_URL)

    assert release[0] == "Fetch.failRequest"
    assert guard.refusal_since(0).reason == "its address could not be checked ([Errno 24] Too many open files)"


@pytest.mark.parametrize("method", ["Fetch.continueRequest", "Fetch.failRequest"])
async def test_a_request_gone_with_its_page_is_left_alone(policy, method):
    cdp = FakeCDPSession(failing=(method,))
    guard = await _attached(cdp)

    release = await _pause(cdp, PUBLIC_URL if method == "Fetch.continueRequest" else HOP_URL)
    await asyncio.sleep(0)

    assert release[0] == method  # the error is swallowed: nothing is left to release
    # A refusal that stopped nothing is not reported: the action failed for another reason.
    assert guard.refusal_since(0) is None


async def test_an_action_is_told_only_the_refusals_after_its_mark(policy):
    cdp = FakeCDPSession()
    guard = await _attached(cdp)
    await _pause(cdp, HOP_URL)
    mark = guard.main_frame_refusals

    assert guard.refusal_since(mark) is None
    await _pause(cdp, HOP_URL)
    assert guard.main_frame_refusals == mark + 1
    assert guard.refusal_since(mark) == browser_tool.RedirectRefusal(HOP_URL, REFUSAL)


async def test_only_the_latest_refusals_are_kept(policy):
    cdp = FakeCDPSession()
    guard = await _attached(cdp)
    for _ in range(20):
        await _pause(cdp, HOP_URL)

    assert guard.main_frame_refusals == 20
    assert len(guard._recent) == 8
    assert guard.refusal_since(0) == browser_tool.RedirectRefusal(HOP_URL, REFUSAL)


async def test_closing_the_guard_cancels_the_checks_in_flight(monkeypatch):
    started = asyncio.Event()

    async def hang(url: str) -> str:
        started.set()
        await asyncio.sleep(60)
        return url

    monkeypatch.setattr(browser_tool, "validate_outbound_url_async", hang)
    cdp = FakeCDPSession()
    guard = await _attached(cdp)
    cdp.handlers["Fetch.requestPaused"](
        {"requestId": "interception-job-1.1", "redirectedRequestId": "interception-job-1.0", "request": {"url": PUBLIC_URL}}
    )
    await started.wait()
    (decision,) = guard._decisions
    (check,) = guard._checks

    guard.close()
    await asyncio.wait({decision, check}, timeout=2)

    assert decision.cancelled()
    assert check.cancelled()
    assert guard._decisions == set()
    assert guard._checks == set()
    assert cdp.releases() == []


# ── the actions that report a refused hop ────────────────────────────────────


def _session_with(page) -> browser_tool.BrowserSession:
    session = browser_tool.BrowserSession(pw=None, browser=None, context=None, page=page)
    session.documents.watch(page)
    return session


def _page(**kwargs):
    page = SimpleNamespace(
        url="https://news.example.com/",
        on=MagicMock(),
        goto=AsyncMock(),
        click=AsyncMock(),
        wait_for_load_state=AsyncMock(),
        title=AsyncMock(return_value="News"),
    )
    for name, value in kwargs.items():
        setattr(page, name, value)
    return page


def _refuse_during(session: browser_tool.BrowserSession, error: BaseException | None = None):
    """An action whose navigation the guard stops at a refused hop."""

    async def act(*_args, **_kwargs):
        session.redirects.main_frame_refusals += 1
        session.redirects._recent.append(
            (session.redirects.main_frame_refusals, browser_tool.RedirectRefusal(HOP_URL, REFUSAL))
        )
        if error is not None:
            raise error

    return act


async def test_a_navigate_stopped_at_a_refused_hop_is_refused_like_a_direct_navigate(monkeypatch, policy):
    page = _page()
    session = _session_with(page)
    page.goto = AsyncMock(side_effect=_refuse_during(session, PlaywrightError(f"Page.goto: net::ERR_ABORTED at {PUBLIC_URL}")))
    monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=session))

    result = await browser_tool.browser_navigate("agent:conv", PUBLIC_URL)

    assert result == f"Navigation blocked by URL policy: redirected to {HOP_URL}: {REFUSAL}"
    direct = await browser_tool.browser_navigate("agent:conv", HOP_URL)
    assert direct == f"Navigation blocked by URL policy: {REFUSAL}"
    page.goto.assert_awaited_once_with(PUBLIC_URL, wait_until="domcontentloaded")
    page.title.assert_not_awaited()


async def test_a_failed_navigate_without_a_refused_hop_fails_as_before(monkeypatch, policy):
    page = _page(goto=AsyncMock(side_effect=PlaywrightError(f"Page.goto: net::ERR_NAME_NOT_RESOLVED at {PUBLIC_URL}")))
    session = _session_with(page)
    monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=session))
    # A refusal from an earlier action is not this navigate's.
    session.redirects.main_frame_refusals = 1
    session.redirects._recent.append((1, browser_tool.RedirectRefusal(HOP_URL, REFUSAL)))

    with pytest.raises(PlaywrightError, match="ERR_NAME_NOT_RESOLVED"):
        await browser_tool.browser_navigate("agent:conv", PUBLIC_URL)


async def test_a_navigate_that_raises_something_else_is_not_read_as_a_refusal(monkeypatch, policy):
    page = _page()
    session = _session_with(page)
    page.goto = AsyncMock(side_effect=_refuse_during(session, RuntimeError("Target page, context or browser has been closed")))
    monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=session))

    with pytest.raises(RuntimeError, match="has been closed"):
        await browser_tool.browser_navigate("agent:conv", PUBLIC_URL)


async def test_a_navigate_is_unchanged_when_no_hop_is_refused(monkeypatch, policy):
    page = _page()
    session = _session_with(page)
    monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=session))

    result = await browser_tool.browser_navigate("agent:conv", PUBLIC_URL)

    assert result.startswith("Navigated to: https://news.example.com/\nTitle: News" + "\nNote: not the requested ")


async def test_a_click_stopped_at_a_refused_hop_says_why_the_page_did_not_change(monkeypatch):
    page = _page()
    session = _session_with(page)
    page.click = AsyncMock(side_effect=_refuse_during(session))
    monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=session))

    result = await browser_tool.browser_click("agent:conv", "#next")

    assert result == (
        "Clicked '#next'. Current page: https://news.example.com/ — News\n"
        f"Navigation blocked by URL policy: redirected to {HOP_URL}: {REFUSAL}"
    )


async def test_a_click_is_unchanged_when_no_hop_is_refused(monkeypatch):
    page = _page()
    session = _session_with(page)
    monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=session))

    result = await browser_tool.browser_click("agent:conv", "#next")

    assert result == "Clicked '#next'. Current page: https://news.example.com/ — News"


# ── the session carries the guard from its first request ────────────────────


class FakePlaywright:
    """``async_playwright()`` that records the order of the launch steps."""

    def __init__(self, *, fail_at: str | None = None) -> None:
        self.steps: list[str] = []
        self.fail_at = fail_at
        self.cdp = FakeCDPSession()
        page = _page()
        page.set_default_timeout = MagicMock()
        page.set_default_navigation_timeout = MagicMock()
        self.context = SimpleNamespace(
            route=self._step("route"),
            new_page=self._step("new_page", page),
            new_cdp_session=self._step("new_cdp_session", self.cdp),
            close=AsyncMock(),
        )
        self.browser = SimpleNamespace(
            new_browser_cdp_session=self._step("new_browser_cdp_session", self.cdp),
            new_context=self._step("new_context", self.context),
            close=self._step("browser.close"),
        )
        self.chromium = SimpleNamespace(launch=self._step("launch", self.browser))

    def _step(self, name: str, result=None):
        async def step(*_args, **_kwargs):
            self.steps.append(name)
            if name == self.fail_at:
                raise PlaywrightError(f"{name} failed")
            return result

        return step

    def __call__(self):
        return self

    async def start(self):
        return self

    async def stop(self):
        self.steps.append("pw.stop")


async def test_a_new_session_is_guarded_before_its_first_page_exists(monkeypatch):
    fake = FakePlaywright()
    monkeypatch.setattr("playwright.async_api.async_playwright", fake)
    monkeypatch.setattr(browser_tool, "_sessions", {})

    session = await browser_tool._launch_session("agent:conv")

    # The last DevTools session is the page's own, opened before any script runs (``BrowserSession.cdp``).
    assert fake.steps == [
        "launch",
        "new_browser_cdp_session",
        "new_context",
        "route",
        "new_page",
        "new_cdp_session",
        "new_cdp_session",
    ]
    assert fake.cdp.sent[0] == ("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
    assert session.redirects._main_frame_id == MAIN_FRAME
    assert browser_tool._sessions["agent:conv"] is session


@pytest.mark.parametrize("fail_at", ["new_browser_cdp_session", "new_cdp_session"])
async def test_a_browser_the_guard_cannot_hold_is_closed_instead_of_used(monkeypatch, fail_at):
    fake = FakePlaywright(fail_at=fail_at)
    monkeypatch.setattr("playwright.async_api.async_playwright", fake)
    monkeypatch.setattr(browser_tool, "_sessions", {})

    with pytest.raises(RuntimeError, match=f"Failed to launch browser: {fail_at} failed"):
        await browser_tool._launch_session("agent:conv")

    assert fake.steps[-2:] == ["browser.close", "pw.stop"]
    assert browser_tool._sessions == {}


async def test_closing_a_session_closes_its_guard(monkeypatch):
    session = _session_with(_page())
    session.context = SimpleNamespace(close=AsyncMock())
    session.browser = SimpleNamespace(close=AsyncMock())
    session.pw = SimpleNamespace(stop=AsyncMock())
    session.redirects.close = MagicMock()
    monkeypatch.setattr(browser_tool, "_sessions", {"agent:conv": session})

    await browser_tool._close_session("agent:conv")

    session.redirects.close.assert_called_once_with()
    session.browser.close.assert_awaited_once()
