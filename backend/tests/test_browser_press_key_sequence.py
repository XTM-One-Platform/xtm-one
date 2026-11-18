"""The browser press action walks a keyboard path in one call and says where the focus landed.

One tool call per key took one or two seconds each, so a path of 26 Tab stops could not be walked
inside an 8 s undo notice, and the focus read after a press often showed the previous element.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import browser_tool


@pytest.mark.parametrize(
    ("key", "keys"),
    [
        ("Enter", ["Enter"]),
        ("Shift+Tab", ["Shift+Tab"]),
        ("*", ["*"]),
        ("Tab*3 Enter", ["Tab", "Tab", "Tab", "Enter"]),
        ("  ArrowDown*2   Escape ", ["ArrowDown", "ArrowDown", "Escape"]),
        ("Shift+8*2", ["Shift+8", "Shift+8"]),
    ],
)
def test_a_key_sequence_is_read_with_its_repeat_counts(key, keys):
    assert browser_tool.parse_key_sequence(key) == keys


@pytest.mark.parametrize("key", ["", "   ", "Tab*0", "Tab*x", "Tab*-1", "Tab*101", "Tab*60 Enter*41"])
def test_an_unreadable_or_unbounded_sequence_is_refused(key):
    with pytest.raises(ValueError):
        browser_tool.parse_key_sequence(key)


def _session(focused: str) -> browser_tool.BrowserSession:
    page = SimpleNamespace(
        url="http://10.42.3.17:3000/chat",
        keyboard=SimpleNamespace(press=AsyncMock()),
        evaluate=AsyncMock(side_effect=lambda js: focused if js is browser_tool._FOCUSED_JS else True),
    )
    return browser_tool.BrowserSession(
        pw=None, browser=None, context=SimpleNamespace(), page=page, viewport={"width": 1024, "height": 800}
    )


@pytest.fixture
def session(monkeypatch):
    made = _session("button 'Undo'")
    monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=made))
    monkeypatch.setattr(browser_tool, "_reopen_resumed_page", AsyncMock())
    monkeypatch.setattr(browser_tool, "_remember_state", AsyncMock())
    return made


async def test_a_sequence_is_pressed_in_order_with_a_settle_after_each_key(session):
    result = await browser_tool.browser_press("agent:chat:c", "Tab*3 Enter")

    presses = [call.args[0] for call in session.page.keyboard.press.await_args_list]
    assert presses == ["Tab", "Tab", "Tab", "Enter"]
    settles = [c for c in session.page.evaluate.await_args_list if c.args[0] is browser_tool._SETTLE_JS]
    assert len(settles) == 4
    assert result == "Pressed 4 keys: Tab*3 Enter. Focus: button 'Undo'"


async def test_one_key_keeps_its_result_and_names_the_focus(session):
    assert await browser_tool.browser_press("agent:chat:c", "Enter") == "Pressed key: Enter. Focus: button 'Undo'"


async def test_a_refused_sequence_presses_nothing(session):
    with pytest.raises(ValueError):
        await browser_tool.browser_press("agent:chat:c", "Tab*500")
    session.page.keyboard.press.assert_not_awaited()
