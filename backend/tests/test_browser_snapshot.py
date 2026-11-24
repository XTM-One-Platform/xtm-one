"""The browser tool's ``snapshot`` action reads Playwright's ARIA snapshot.

``Page.accessibility`` was removed in Playwright 1.57, so on the pinned 1.60
every ``snapshot`` call raised ``AttributeError`` and the agent got a tool
error instead of the page outline. The action now reads
``Locator.aria_snapshot`` on the page body: the API every Playwright release
since 1.49 ships, the pinned 1.60 and 1.63 included (``Page.aria_snapshot``
only exists from 1.59).
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import browser_tool
from app.services.builtin_tools import build_browser_tool
from app.tools.base import ToolCall

OUTLINE = (
    '- heading "Example Domain" [level=1]\n'
    "- paragraph: This domain is for use in documentation examples.\n"
    '- link "Learn more":\n'
    "  - /url: https://iana.org/domains/example"
)


def _install_session(monkeypatch, body: object) -> SimpleNamespace:
    """A live session whose page has no ``accessibility`` attribute, like every Playwright >= 1.57.

    No profile and no release, as a session :func:`browser_tool._get_or_create_session` opens: the snapshot keeps
    its state after it runs, and a page with no http(s) URL leaves the kept state as it is."""
    page = SimpleNamespace(locator=MagicMock(return_value=body), url="about:blank")
    session = SimpleNamespace(lock=asyncio.Lock(), page=page, last_used=0.0, profile=None, release=None)
    monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=session))
    return session


async def test_snapshot_returns_the_aria_outline_of_the_page_body(monkeypatch):
    body = SimpleNamespace(aria_snapshot=AsyncMock(return_value=OUTLINE))
    session = _install_session(monkeypatch, body)

    result = await browser_tool.browser_snapshot("agent:conv")

    assert result == OUTLINE
    session.page.locator.assert_called_once_with("body")
    body.aria_snapshot.assert_awaited_once_with()
    assert not session.lock.locked()


@pytest.mark.parametrize("empty", ["", "\n  \n"])
async def test_a_page_with_nothing_to_outline_says_so(monkeypatch, empty):
    _install_session(monkeypatch, SimpleNamespace(aria_snapshot=AsyncMock(return_value=empty)))

    result = await browser_tool.browser_snapshot("agent:conv")

    assert result == "No accessibility tree available for this page."


async def test_a_playwright_without_the_aria_api_is_a_clear_error(monkeypatch):
    session = _install_session(monkeypatch, SimpleNamespace())

    with pytest.raises(RuntimeError, match=r"Locator\.aria_snapshot, Playwright 1\.49 or later"):
        await browser_tool.browser_snapshot("agent:conv")
    assert not session.lock.locked()


async def test_the_browser_tool_snapshot_action_returns_the_outline(monkeypatch):
    _install_session(monkeypatch, SimpleNamespace(aria_snapshot=AsyncMock(return_value=OUTLINE)))
    _defs, executor = build_browser_tool("agent:conv")

    result = await executor.execute_tool(ToolCall(id="call-1", name="browser", arguments={"action": "snapshot"}))

    assert result.content == OUTLINE
    assert result.is_error is False


async def test_the_browser_tool_names_the_missing_api_and_the_alternatives(monkeypatch):
    _install_session(monkeypatch, SimpleNamespace())
    _defs, executor = build_browser_tool("agent:conv")

    result = await executor.execute_tool(ToolCall(id="call-1", name="browser", arguments={"action": "snapshot"}))

    assert result.content.startswith("Browser error: The snapshot action needs Playwright's ARIA snapshot API")
    assert "Use get_content or screenshot instead." in result.content


async def test_a_selector_outlines_the_first_element_it_matches(monkeypatch):
    region = SimpleNamespace(aria_snapshot=AsyncMock(return_value=OUTLINE))
    matched = SimpleNamespace(first=region)
    session = _install_session(monkeypatch, matched)

    result = await browser_tool.browser_snapshot("agent:conv", "main")

    assert result == OUTLINE
    session.page.locator.assert_called_once_with("main")
    region.aria_snapshot.assert_awaited_once_with(timeout=browser_tool.SNAPSHOT_SELECTOR_TIMEOUT_MS)


async def test_a_selector_with_nothing_to_outline_names_it(monkeypatch):
    region = SimpleNamespace(aria_snapshot=AsyncMock(return_value=""))
    _install_session(monkeypatch, SimpleNamespace(first=region))

    result = await browser_tool.browser_snapshot("agent:conv", "[role=dialog]")

    assert result == "No accessibility tree available for '[role=dialog]' on this page."


async def test_the_browser_tool_passes_the_selector_to_the_snapshot(monkeypatch):
    region = SimpleNamespace(aria_snapshot=AsyncMock(return_value=OUTLINE))
    session = _install_session(monkeypatch, SimpleNamespace(first=region))
    _defs, executor = build_browser_tool("agent:conv")

    result = await executor.execute_tool(
        ToolCall(id="call-1", name="browser", arguments={"action": "snapshot", "selector": "nav"})
    )

    assert result.content == OUTLINE
    session.page.locator.assert_called_once_with("nav")


def test_a_short_outline_is_answered_whole():
    assert browser_tool.bound_snapshot(OUTLINE, scope="the page") == OUTLINE


def test_a_long_outline_is_cut_at_a_line_and_names_the_selector():
    line = '- link "Integration":\n'
    outline = line * 5_000  # about 110,000 characters, a whole catalog page

    result = browser_tool.bound_snapshot(outline, scope="the page", limit=1_000)

    head, _, notice = result.partition("\n\n[")
    assert len(head) <= 1_000
    assert head.endswith('"Integration":')
    assert f"of {len(outline):,} characters" in notice
    assert "pass selector" in notice


async def test_the_page_outline_is_bounded(monkeypatch):
    outline = '- link "Integration":\n' * 5_000
    _install_session(monkeypatch, SimpleNamespace(aria_snapshot=AsyncMock(return_value=outline)))

    result = await browser_tool.browser_snapshot("agent:conv")

    assert len(result) < browser_tool.MAX_PAGE_CONTENT_CHARS + 300
    assert "[Snapshot of the page truncated at" in result


def test_the_installed_playwright_ships_the_api_the_snapshot_reads():
    """Tripwire for a Playwright bump: the call above must exist and take no required argument."""
    async_api = pytest.importorskip("playwright.async_api")

    method = getattr(async_api.Locator, "aria_snapshot", None)

    assert method is not None
    arguments = list(inspect.signature(method).parameters.values())[1:]
    assert all(argument.default is not inspect.Parameter.empty for argument in arguments)
