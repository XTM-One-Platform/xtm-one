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


def test_the_installed_playwright_ships_the_api_the_snapshot_reads():
    """Tripwire for a Playwright bump: the call above must exist and take no required argument."""
    async_api = pytest.importorskip("playwright.async_api")

    method = getattr(async_api.Locator, "aria_snapshot", None)

    assert method is not None
    arguments = list(inspect.signature(method).parameters.values())[1:]
    assert all(argument.default is not inspect.Parameter.empty for argument in arguments)
