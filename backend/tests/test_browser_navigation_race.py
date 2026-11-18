"""The browser tool survives a page that navigates under its scripts.

Playwright answers ``Execution context was destroyed, most likely because of a
navigation`` when the page navigates, or a SPA reloads its frame, while a
script runs in it - and it answers BEFORE its client has processed the commit
of the new document: ``page.url`` still names the old page and
``wait_for_load_state("domcontentloaded")`` answers from the old document's
state (probed on Playwright 1.60 / headless Chromium). The double below keeps
that order: the race error first, the commit and DOMContentLoaded after it.

The tool's own reads (the page text, the title) change nothing, so they are
read once more on the new document, and only when the outbound URL policy
allows its URL. An agent's script is never run again: the error cannot tell a
script the page navigated under from one that navigated itself or had already
acted, and running a submit twice is worse than asking. A click timeout says
how far the click got - performed, maybe performed, or not performed and what
the selector matches - on which page.
"""

from __future__ import annotations

import asyncio
import functools
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.services import browser_tool
from app.services.builtin_tools import build_browser_tool
from app.tools.base import ToolCall, ToolResult

OLD_URL = "https://aev.example.com/admin/assets"
NEW_URL = "https://aev.example.com/admin/assets/endpoints"
INTERNAL_URL = "http://metadata.internal.example/latest/meta-data/"


def _navigation_race(api: str = "Page.evaluate") -> PlaywrightError:
    return PlaywrightError(f"{api}: Execution context was destroyed, most likely because of a navigation.")


@dataclass
class Race:
    """A script the page's navigation interrupts: the error, then the commit of *to*."""

    to: str = NEW_URL
    loaded: bool = True
    api: str = "Page.evaluate"


HANG = object()  # a script that never returns (a page stuck in a loop)
#: The action bound as shipped, before a test lowers it: a wait that ends sooner was bounded by the lowered one, never
#: by this nor the hang's minute - a wall clock a loaded runner's scheduler can stall for seconds still says so.
_SHIPPED_BOUND = browser_tool.ACTION_TIMEOUT_MS / 1000


class FakePage:
    """The slice of a Playwright page the tool drives, with its navigation events."""

    def __init__(self, *, evaluate=(), title=("Endpoints",), locator_count: int = 0, click=None):
        self.url = OLD_URL
        self.main_frame = SimpleNamespace(parent_frame=None)
        self.listeners: dict[str, list] = {}
        self.evaluate = AsyncMock(side_effect=self._steps(evaluate))
        self.title = AsyncMock(side_effect=self._steps(title))
        self.click = AsyncMock(side_effect=click)
        self.wait_for_load_state = AsyncMock()
        self.locator = MagicMock(return_value=SimpleNamespace(count=AsyncMock(return_value=locator_count)))

    def on(self, event: str, handler) -> None:
        self.listeners.setdefault(event, []).append(handler)

    def commit(self, url: str, *, loaded: bool = True) -> None:
        self.url = url
        for handler in self.listeners.get("framenavigated", []):
            handler(self.main_frame)
        if loaded:
            for handler in self.listeners.get("domcontentloaded", []):
                handler(self)

    def _steps(self, steps):
        queue = list(steps)

        async def step(*_args, **_kwargs):
            item = queue.pop(0)
            if isinstance(item, Race):
                asyncio.get_running_loop().call_soon(functools.partial(self.commit, item.to, loaded=item.loaded))
                raise _navigation_race(item.api)
            if item is HANG:
                await asyncio.sleep(60)
            if isinstance(item, BaseException):
                raise item
            return item

        return step


@pytest.fixture
def allowed_urls(monkeypatch):
    """The outbound URL policy, without DNS: every URL but ``INTERNAL_URL`` is allowed."""
    checked: list[str] = []

    async def validate(url: str) -> str:
        checked.append(url)
        if url == INTERNAL_URL:
            raise ValueError("URL targets a private or non-routable address: 169.254.169.254")
        return url

    monkeypatch.setattr(browser_tool, "validate_outbound_url_async", validate)
    return checked


@pytest.fixture
def install_page(monkeypatch, allowed_urls):
    def install(**kwargs) -> FakePage:
        page = FakePage(**kwargs)
        session = browser_tool.BrowserSession(pw=None, browser=None, context=None, page=page)
        session.documents.watch(page)
        monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=session))
        # The settle a navigate waits reads location.href through page.evaluate, whose answers these fakes queue for
        # the reads under test; it runs against a real page in test_browser_navigate_settles_chromium.py.
        monkeypatch.setattr(browser_tool, "_settle_navigation", AsyncMock(return_value=True))
        return page

    return install


def _content(url: str = NEW_URL, text: str = "  Endpoints\nweb-01  ", title: str = "Endpoints") -> dict:
    return {"url": url, "title": title, "text": text}


async def _call(arguments: dict) -> ToolResult:
    _defs, executor = build_browser_tool("agent:conv")
    return await executor.execute_tool(ToolCall(id="call-1", name="browser", arguments=arguments))


# ── evaluate: the agent's script runs once ──────────────────────────────────


async def test_an_interrupted_script_is_not_run_again_and_the_new_page_is_named(install_page):
    page = install_page(evaluate=[Race(), 42])

    result = await _call({"action": "evaluate", "expression": "() => rows.length"})

    assert result.is_error is True
    assert result.content == (
        f"Browser action 'evaluate' failed: the page navigated before the script returned (now at {NEW_URL}), "
        "so its result was lost. The script was not run again: it may have acted before the page changed "
        "(a click, a submit, a request). Run it again only if it just reads the page."
    )
    page.evaluate.assert_awaited_once_with("() => rows.length")
    page.wait_for_load_state.assert_not_awaited()


async def test_a_script_that_submits_a_form_is_not_submitted_twice(install_page):
    submit = "async () => { document.forms[0].submit(); await new Promise(r => setTimeout(r, 3000)) }"
    page = install_page(evaluate=[Race(to=OLD_URL + "?saved=1"), None])

    result = await _call({"action": "evaluate", "expression": submit})

    assert "The script was not run again" in result.content
    assert f"now at {OLD_URL}?saved=1" in result.content
    page.evaluate.assert_awaited_once_with(submit)


async def test_an_evaluate_without_a_navigation_is_unchanged(install_page, allowed_urls):
    page = install_page(evaluate=[42])

    result = await _call({"action": "evaluate", "expression": "() => rows.length"})

    assert result.content == "JavaScript result:\n42"
    assert result.is_error is False
    page.evaluate.assert_awaited_once()
    assert allowed_urls == []


async def test_a_new_page_that_never_loads_is_waited_for_a_bounded_time(install_page, monkeypatch):
    monkeypatch.setattr(browser_tool, "ACTION_TIMEOUT_MS", 50)
    install_page(evaluate=[Race(loaded=False)])

    started = asyncio.get_running_loop().time()
    result = await _call({"action": "evaluate", "expression": "() => rows.length"})

    assert asyncio.get_running_loop().time() - started < _SHIPPED_BOUND
    assert f"(at {NEW_URL}, not loaded after 0.05 s)" in result.content
    assert "The script was not run again" in result.content


async def test_a_script_interrupted_by_a_move_to_a_blocked_url_says_so(install_page):
    page = install_page(evaluate=[Race(to=INTERNAL_URL), 42])

    result = await _call({"action": "evaluate", "expression": "() => document.body.innerText"})

    assert result.is_error is True
    assert result.content == (
        f"Browser action 'evaluate' failed: the page navigated before the script returned, to {INTERNAL_URL}, "
        "which the browser's URL policy does not allow (URL targets a private or non-routable address: "
        "169.254.169.254). The script was not run again."
    )
    page.evaluate.assert_awaited_once()


@pytest.mark.parametrize(
    "script_error",
    [
        PlaywrightError(
            "Page.evaluate: TypeError: Cannot read properties of undefined (reading 'rows')\n"
            "    at eval (eval at evaluate (:291:30), <anonymous>:1:14)"
        ),
        PlaywrightError("Page.evaluate: Error: Execution context was destroyed"),
        RuntimeError("Execution context was destroyed, most likely because of a navigation."),
    ],
    ids=["type-error", "script-quoting-the-words", "not-a-playwright-error"],
)
async def test_an_error_that_is_not_the_navigation_race_is_unchanged(install_page, script_error):
    page = install_page(evaluate=[script_error, 42])

    result = await _call({"action": "evaluate", "expression": "() => data.rows"})

    assert result.is_error is True
    assert result.content.endswith(str(script_error))
    page.evaluate.assert_awaited_once()


# ── get_content: the tool's own read, once more on the new document ─────────


async def test_get_content_reads_the_new_document_after_a_navigation(install_page, allowed_urls):
    page = install_page(evaluate=[Race(), _content()])

    result = await _call({"action": "get_content"})

    assert result.is_error is False
    assert result.content == (
        "[The page navigated while it was read: this is the new document.]\n"
        f"URL: {NEW_URL}\nTitle: Endpoints\n\nEndpoints\nweb-01"
    )
    assert page.evaluate.await_count == 2
    assert allowed_urls == [NEW_URL, NEW_URL]


async def test_get_content_reads_text_title_and_url_in_one_script(install_page):
    page = install_page(evaluate=[_content(url=OLD_URL, title="Assets", text="web-01")])

    result = await _call({"action": "get_content"})

    assert result.content == f"URL: {OLD_URL}\nTitle: Assets\n\nweb-01"
    page.evaluate.assert_awaited_once_with(browser_tool._PAGE_CONTENT_JS)
    page.title.assert_not_awaited()


async def test_get_content_does_not_read_a_new_document_the_url_policy_blocks(install_page):
    page = install_page(evaluate=[Race(to=INTERNAL_URL), _content(url=INTERNAL_URL)])

    result = await _call({"action": "get_content"})

    assert result.is_error is True
    assert result.content == (
        f"Browser action 'get_content' failed: the page navigated while it was read, to {INTERNAL_URL}, which "
        "the browser's URL policy does not allow (URL targets a private or non-routable address: "
        "169.254.169.254); nothing was read from the new document."
    )
    page.evaluate.assert_awaited_once()


async def test_get_content_drops_a_read_that_landed_on_a_blocked_url(install_page):
    # The page moved on again between the policy check and the read: the read's own URL decides.
    page = install_page(evaluate=[Race(to=NEW_URL), _content(url=INTERNAL_URL, text="secret")])

    result = await _call({"action": "get_content"})

    assert result.is_error is True
    assert "secret" not in result.content
    assert f"to {INTERNAL_URL}, which the browser's URL policy does not allow" in result.content
    assert page.evaluate.await_count == 2


async def test_get_content_does_not_read_a_new_document_that_has_not_loaded(install_page, monkeypatch):
    monkeypatch.setattr(browser_tool, "ACTION_TIMEOUT_MS", 50)
    page = install_page(evaluate=[Race(loaded=False), _content()])

    result = await _call({"action": "get_content"})

    assert result.is_error is True
    assert result.content == (
        "Browser action 'get_content' failed: the page navigated while it was read, and the new document "
        f"had not loaded after 0.05 s (page now at {NEW_URL}); nothing was read from it. Wait for the page "
        "to settle, then read it again."
    )
    page.evaluate.assert_awaited_once()


async def test_a_read_of_the_new_document_is_bounded(install_page, monkeypatch):
    monkeypatch.setattr(browser_tool, "ACTION_TIMEOUT_MS", 50)
    install_page(evaluate=[Race(), HANG])

    started = asyncio.get_running_loop().time()
    result = await _call({"action": "get_content"})

    assert asyncio.get_running_loop().time() - started < _SHIPPED_BOUND
    assert result.is_error is True
    assert result.content == (
        f"Browser action 'get_content' failed: the page navigated to {NEW_URL} while it was read, and "
        "reading the new document did not finish within 0.05 s."
    )


async def test_a_page_that_keeps_navigating_is_named_instead_of_the_raw_error(install_page):
    page = install_page(evaluate=[Race(), Race(to=NEW_URL + "#again"), _content()])

    result = await _call({"action": "get_content"})

    assert result.is_error is True
    assert result.content == (
        "Browser action 'get_content' failed: the page kept navigating: it navigated while it was read, "
        f"and again while the new document was read (page now at {NEW_URL}). Wait for the page to settle, "
        "then read it again."
    )
    assert "Execution context was destroyed" not in result.content
    assert page.evaluate.await_count == 2


# ── the title read after navigate / click ────────────────────────────────────


@pytest.mark.parametrize(
    ("reread", "expected_title"),
    [
        ([NEW_URL, "Endpoints"], "Endpoints"),
        (Race(to=NEW_URL + "#again", api="Page.evaluate"), "(title unavailable: the page is still navigating)"),
        (
            PlaywrightError("Page.evaluate: Target page, context or browser has been closed"),
            "(title unavailable: the page navigated and its new title could not be read)",
        ),
    ],
    ids=["title-read-again", "title-still-navigating", "title-read-failed"],
)
async def test_a_title_read_racing_the_navigation_does_not_fail_the_click(install_page, reread, expected_title):
    page = install_page(title=[Race(api="Page.title")], evaluate=[reread])

    result = await _call({"action": "click", "selector": "#save"})

    assert result.is_error is False
    assert result.content.startswith(f"Clicked '#save'. Current page: {NEW_URL}")
    assert result.content.endswith(expected_title)
    page.click.assert_awaited_once_with("#save")


async def test_a_title_is_not_read_on_a_new_document_that_has_not_loaded(install_page, monkeypatch):
    monkeypatch.setattr(browser_tool, "ACTION_TIMEOUT_MS", 50)
    page = install_page(title=[Race(loaded=False, api="Page.title")], evaluate=[[NEW_URL, "Endpoints"]])

    result = await _call({"action": "click", "selector": "#save"})

    assert result.is_error is False
    assert result.content.endswith("(title unavailable: the page is still navigating)")
    page.evaluate.assert_not_awaited()


async def test_a_title_is_not_read_on_a_url_the_policy_blocks(install_page):
    page = install_page(title=[Race(to=INTERNAL_URL, api="Page.title")], evaluate=[[INTERNAL_URL, "Secret"]])

    result = await _call({"action": "click", "selector": "#save"})

    assert result.is_error is False
    assert result.content.endswith("(title unavailable: the page moved to a URL the browser's URL policy does not allow)")
    page.evaluate.assert_not_awaited()


async def test_a_title_read_on_navigate_is_read_again_on_the_redirected_page(install_page):
    page = install_page(title=[Race(api="Page.title")], evaluate=[[NEW_URL, "Endpoints"]])
    page.goto = AsyncMock()

    result = await browser_tool.browser_navigate("agent:conv", OLD_URL)

    assert result.startswith(f"Navigated to: {NEW_URL}\nTitle: Endpoints\nNote: not the requested {OLD_URL} ")
    assert page.evaluate.await_args_list[0].args == (browser_tool._PAGE_URL_AND_TITLE_JS,)


SPA_A = "https://lab.example.com/assignments/a"
SPA_B = "https://lab.example.com/assignments/b"


async def test_navigate_answers_where_the_document_says_it_is_not_the_committed_url(install_page):
    """A SPA that routes back after the load: the browser committed B, the document sits on A (issue #9233)."""
    page = install_page(title=["Assignment"], evaluate=[SPA_A, SPA_A])
    page.goto = AsyncMock(side_effect=lambda url, **_kw: page.commit(url))

    result = await browser_tool.browser_navigate("agent:conv", SPA_B)

    assert result.startswith(f"Navigated to: {SPA_A}\nTitle: Assignment\n")
    assert result.endswith(f"Note: not the requested {SPA_B} (a redirect or the application's own routing); scripts run on {SPA_A}.")
    page.wait_for_load_state.assert_awaited_once_with("load", timeout=browser_tool.ACTION_TIMEOUT_MS)


async def test_navigate_reads_the_document_again_after_its_load_when_it_lagged(install_page):
    """The first read still saw the previous route; after the load event the document is on the target."""
    page = install_page(title=["Assignment"], evaluate=[SPA_A, SPA_B])
    page.goto = AsyncMock(side_effect=lambda url, **_kw: page.commit(url))

    result = await browser_tool.browser_navigate("agent:conv", SPA_B)

    assert result == f"Navigated to: {SPA_B}\nTitle: Assignment"


@pytest.mark.parametrize("tries", range(10))
async def test_navigate_between_two_spa_routes_lands_on_the_target(install_page, tries):
    page = install_page(title=["A", "B"], evaluate=[SPA_A, SPA_B])
    page.goto = AsyncMock(side_effect=lambda url, **_kw: page.commit(url))

    first = await browser_tool.browser_navigate("agent:conv", SPA_A)
    second = await browser_tool.browser_navigate("agent:conv", SPA_B)

    assert first == f"Navigated to: {SPA_A}\nTitle: A"
    assert second == f"Navigated to: {SPA_B}\nTitle: B"
    page.wait_for_load_state.assert_not_awaited()


async def test_a_navigate_whose_document_cannot_be_read_answers_the_committed_url(install_page):
    page = install_page(title=["B"], evaluate=[PlaywrightError("Target closed")])
    page.goto = AsyncMock(side_effect=lambda url, **_kw: page.commit(url))

    assert await browser_tool.browser_navigate("agent:conv", SPA_B) == f"Navigated to: {SPA_B}\nTitle: B"


def _same_document_goto(page):
    """``page.goto`` the way Chromium answers a URL change made inside the open document: no response, no new load."""

    async def goto(url, **_kw):
        page.url = url
        return None

    return goto


@pytest.mark.parametrize(
    "target",
    ["https://lab.example.com/fleets/b?view=about", "https://lab.example.com/fleets/a?view=members"],
    ids=["another-path", "another-query"],
)
async def test_a_navigate_that_loaded_no_new_document_reloads_it(install_page, target):
    """A SPA route change with no new document is reloaded before the answer, so the DOM is the target's (#9635)."""
    page = install_page(title=["B"], evaluate=[target])
    page.url = "https://lab.example.com/fleets/a?view=about"
    page.goto = AsyncMock(side_effect=_same_document_goto(page))
    page.reload = AsyncMock(side_effect=lambda **_kw: page.commit(page.url))

    result = await browser_tool.browser_navigate("agent:conv", target)

    assert result == f"Navigated to: {target}\nTitle: B"
    page.reload.assert_awaited_once_with(wait_until="domcontentloaded")


async def test_a_fragment_only_navigate_is_reloaded_and_named(install_page):
    """A signed-in ``/login#token=`` URL opened on a page at ``/login`` is read on a fresh load (#10458)."""
    target = "https://lab.example.com/login#token=abc"
    page = install_page(title=["A"], evaluate=[target])
    page.url = "https://lab.example.com/login"
    page.goto = AsyncMock(side_effect=_same_document_goto(page))
    page.reload = AsyncMock(side_effect=lambda **_kw: page.commit(page.url))

    result = await browser_tool.browser_navigate("agent:conv", target)

    assert result == (
        f"Navigated to: {target}\nTitle: A\n"
        "Note: only the fragment differed, so the page was loaded again to read it as a first visit does."
    )
    page.reload.assert_awaited_once_with(wait_until="domcontentloaded")
    # The page's own scripts read the fragment after DOMContentLoaded: answered once it went quiet (#10464).
    bound_ms = browser_tool._FRAGMENT_SETTLE_SECONDS * 1000
    assert page.wait_for_load_state.await_args_list[:2] == [
        call("load", timeout=bound_ms),
        call("networkidle", timeout=bound_ms),
    ]


async def test_a_fragment_reload_that_never_goes_quiet_is_answered_at_the_bound(install_page):
    target = "https://lab.example.com/login#token=abc"
    page = install_page(title=["A"], evaluate=[target])
    page.url = "https://lab.example.com/login"
    page.goto = AsyncMock(side_effect=_same_document_goto(page))
    page.reload = AsyncMock(side_effect=lambda **_kw: page.commit(page.url))
    page.wait_for_load_state = AsyncMock(side_effect=[None, PlaywrightError("Timeout 5000ms exceeded.")])

    result = await browser_tool.browser_navigate("agent:conv", target)

    assert result.startswith(f"Navigated to: {target}\nTitle: A\n")
    assert page.wait_for_load_state.await_count == 2


async def test_a_route_change_reload_does_not_wait_for_the_network(install_page):
    target = "https://lab.example.com/fleets/b"
    page = install_page(title=["B"], evaluate=[target])
    page.url = "https://lab.example.com/fleets/a"
    page.goto = AsyncMock(side_effect=_same_document_goto(page))
    page.reload = AsyncMock(side_effect=lambda **_kw: page.commit(page.url))

    assert await browser_tool.browser_navigate("agent:conv", target) == f"Navigated to: {target}\nTitle: B"
    page.wait_for_load_state.assert_not_awaited()


async def test_a_navigate_whose_reload_fails_says_the_page_may_be_the_previous_route(install_page):
    target = "https://lab.example.com/fleets/b"
    page = install_page(title=["A"], evaluate=[target])
    page.url = "https://lab.example.com/fleets/a"
    page.goto = AsyncMock(side_effect=_same_document_goto(page))
    page.reload = AsyncMock(side_effect=PlaywrightError("Target closed"))

    result = await browser_tool.browser_navigate("agent:conv", target)

    assert "no new load" in result
    assert result.endswith("the page may still show the previous route.")


def test_a_trailing_slash_or_an_empty_fragment_is_not_a_different_page():
    assert browser_tool._landed_elsewhere("https://x.example/a", "https://x.example/a/") == ""
    assert browser_tool._landed_elsewhere("https://x.example/a", "https://x.example/a#") == ""
    assert browser_tool._landed_elsewhere("https://x.example/a", "https://x.example/b").startswith("\nNote:")


def test_the_kept_page_names_where_the_navigate_landed(monkeypatch):
    session = browser_tool.BrowserSession(pw=None, browser=None, context=None, page=SimpleNamespace(url=SPA_B))
    monkeypatch.setitem(browser_tool._kept_pages, "agent:keep", {"url": SPA_A, "viewport": {"width": 1, "height": 1}})

    browser_tool._keep_landed_page("agent:keep", session, SPA_B)

    assert browser_tool._kept_pages["agent:keep"] == {"url": SPA_B, "viewport": {"width": 1, "height": 1}}


def _navigation_timeout() -> PlaywrightTimeoutError:
    return PlaywrightTimeoutError(f"Page.goto: Timeout {browser_tool.NAVIGATION_TIMEOUT_MS}ms exceeded.")


async def test_a_navigate_whose_document_arrived_but_is_still_loading_is_not_an_error(install_page):
    page = install_page()

    async def goto(url, **_kwargs):
        page.commit(url, loaded=False)  # the dev server answered; its modules keep loading past the timeout
        raise _navigation_timeout()

    page.goto = AsyncMock(side_effect=goto)

    result = await _call({"action": "navigate", "url": NEW_URL})

    assert result.is_error is False
    assert result.content.startswith(f"Navigated to: {NEW_URL}\nStill loading:")
    assert "Do not navigate again" in result.content
    page.goto.assert_awaited_once()


async def test_a_navigate_timeout_before_any_document_arrived_stays_an_error(install_page):
    page = install_page()
    page.goto = AsyncMock(side_effect=_navigation_timeout())

    result = await _call({"action": "navigate", "url": NEW_URL})

    assert result.is_error is True
    assert "Timeout" in result.content
    assert "Still loading" not in result.content


async def test_a_navigate_timeout_argument_bounds_the_wait_for_the_page(install_page):
    page = install_page(title=["B"], evaluate=[NEW_URL])
    seen: dict = {}

    async def goto(url, **kwargs):
        seen.update(kwargs)
        raise _navigation_timeout()

    page.goto = AsyncMock(side_effect=goto)

    await _call({"action": "navigate", "url": NEW_URL, "timeout": 90})

    assert seen["timeout"] == 90_000


async def test_a_navigate_without_timeout_keeps_the_default_wait(install_page):
    page = install_page()
    seen: dict = {}

    async def goto(url, **kwargs):
        seen.update(kwargs)
        raise _navigation_timeout()

    page.goto = AsyncMock(side_effect=goto)

    await _call({"action": "navigate", "url": NEW_URL})

    assert "timeout" not in seen


async def test_a_still_loading_answer_names_the_asked_wait(install_page):
    page = install_page()

    async def goto(url, **_kwargs):
        page.commit(url, loaded=False)
        raise _navigation_timeout()

    page.goto = AsyncMock(side_effect=goto)

    result = await _call({"action": "navigate", "url": NEW_URL, "timeout": 75})

    assert "within 75 s" in result.content


def test_the_navigation_timeout_is_clamped():
    assert browser_tool.navigation_timeout(None) == browser_tool.NAVIGATION_TIMEOUT_MS / 1000
    assert browser_tool.navigation_timeout(0) == 1.0
    assert browser_tool.navigation_timeout(10_000) == browser_tool.MAX_NAVIGATION_TIMEOUT_SECONDS
    assert browser_tool.navigation_timeout("x") == browser_tool.NAVIGATION_TIMEOUT_MS / 1000
    assert browser_tool.navigation_timeout(float("nan")) == browser_tool.NAVIGATION_TIMEOUT_MS / 1000


# ── a click timeout says how far the click got ───────────────────────────────


def _click_timeout(log: str, timeout_ms: int = 10_000) -> PlaywrightTimeoutError:
    return PlaywrightTimeoutError(f"Page.click: Timeout {timeout_ms}ms exceeded.\nCall log:\n{log}")


_WAITING = '  - waiting for locator("#save")\n'
_RESOLVED = _WAITING + '    - locator resolved to <button id="save">Save</button>\n  - attempting click action\n'


@pytest.mark.parametrize(("timeout_ms", "waited"), [(10_000, "10 s"), (1_500, "1.5 s")], ids=["10s", "1.5s"])
async def test_a_click_timeout_says_the_selector_matched_nothing_on_the_page(install_page, timeout_ms, waited):
    selector = '[data-qa="modassets"]'
    log = f"  - waiting for locator({selector!r})\n"
    page = install_page(click=_click_timeout(log, timeout_ms), locator_count=0)

    result = await _call({"action": "click", "selector": selector})

    assert result.is_error is True
    assert result.content == (
        f"Browser action 'click' failed: no element matches '{selector}' on {OLD_URL} (waited {waited})."
    )
    page.locator.assert_called_once_with(selector)


async def test_a_click_timeout_with_matches_leaves_the_cause_to_playwrights_log(install_page):
    timeout = _click_timeout(
        _RESOLVED + "    2 x waiting for element to be visible, enabled and stable\n      - element is not enabled\n"
    )
    install_page(click=timeout, locator_count=2)

    result = await _call({"action": "click", "selector": "#save"})

    assert result.is_error is True
    assert result.content == (
        f"Browser action 'click' failed: 2 element(s) match '#save' on {OLD_URL}, but the click was not "
        f"performed within 10 s; Playwright's log says what it was waiting for. {timeout}"
    )


async def test_a_click_that_happened_before_the_timeout_is_not_offered_again(install_page):
    timeout = _click_timeout(
        _RESOLVED
        + "    - performing click action\n    - click action done\n    - waiting for scheduled navigations to finish\n"
    )
    page = install_page(click=timeout, locator_count=0)

    result = await _call({"action": "click", "selector": "#save"})

    assert result.is_error is True
    assert result.content == (
        "Browser action 'click' failed: the click on '#save' happened, but the navigation it started had "
        f"not finished after 10 s (page at {OLD_URL}). Check the page before clicking again."
    )
    page.locator.assert_not_called()


async def test_a_click_cut_off_while_performed_may_have_happened(install_page):
    timeout = _click_timeout(_RESOLVED + "    - performing click action\n")
    page = install_page(click=timeout, locator_count=1)

    result = await _call({"action": "click", "selector": "#save"})

    assert result.content == (
        "Browser action 'click' failed: the time ran out while the click on '#save' was being performed on "
        f"{OLD_URL}, so it may have happened. Check the page before clicking again. {timeout}"
    )
    page.locator.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        PlaywrightTimeoutError("Page.click: Timeout 10000ms exceeded."),
        RuntimeError("Timeout 10000ms exceeded.\nCall log:\n  - waiting for locator('#save')\n"),
        PlaywrightError("Page.click: Target page, context or browser has been closed"),
    ],
    ids=["timeout-without-call-log", "not-a-playwright-timeout", "not-a-timeout"],
)
async def test_a_click_error_the_tool_cannot_read_is_unchanged(install_page, error):
    page = install_page(click=error)

    result = await _call({"action": "click", "selector": "#save"})

    assert result.is_error is True
    assert result.content.endswith(str(error))
    page.locator.assert_not_called()


async def test_a_click_timeout_keeps_playwrights_words_when_the_selector_cannot_be_counted(install_page):
    timeout = _click_timeout(_WAITING)
    page = install_page(click=timeout)
    page.locator.return_value.count.side_effect = PlaywrightError("Locator.count: Target page has been closed")

    result = await _call({"action": "click", "selector": "#save"})

    assert result.content == f"Browser action 'click' failed: {timeout}"


# ── the document tracker ─────────────────────────────────────────────────────


async def test_the_tracker_waits_for_a_document_committed_after_the_mark():
    page = FakePage()
    tracker = browser_tool.DocumentTracker()
    tracker.watch(page)
    page.commit(OLD_URL)
    mark = tracker.committed

    # A late DOMContentLoaded of the document already there is not the new one.
    for handler in page.listeners["domcontentloaded"]:
        handler(page)
    assert await tracker.loaded_after(mark, 0.01) is False

    # A child frame's commit is not the page's.
    for handler in page.listeners["framenavigated"]:
        handler(SimpleNamespace(parent_frame=page.main_frame))
    assert await tracker.loaded_after(mark, 0.01) is False

    asyncio.get_running_loop().call_soon(page.commit, NEW_URL)
    assert await tracker.loaded_after(mark, 1) is True
