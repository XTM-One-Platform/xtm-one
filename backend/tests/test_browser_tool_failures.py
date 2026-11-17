"""The browser tool records its failed actions as failed calls.

The ``browser`` dispatcher catches every failure and answers with a sentence
(``Browser error: ...``, ``Browser action '...' failed: ...``,
``Unknown action: ...``) that ``is_error_shaped_result`` does not recognise, so
``CallableToolExecutor`` recorded each failed browser action as a success: the
trace, the ``tool_done`` event, the per-tool error counts and the failure brake
never saw one. The dispatcher now returns
them flagged ``is_error``, with the same sentence for the model.
"""

from __future__ import annotations

import pytest

from app.services.builtin_tools import build_browser_tool
from app.tools.base import ToolCall, ToolResult


async def _call(arguments: dict) -> ToolResult:
    _defs, executor = build_browser_tool("agent:conv")
    return await executor.execute_tool(ToolCall(id="call-1", name="browser", arguments=arguments))


def _raising(exc: Exception):
    async def _action(*_args, **_kwargs):
        raise exc

    return _action


@pytest.mark.parametrize(
    ("arguments", "action_function", "exc", "content"),
    [
        (
            {"action": "get_content"},
            "browser_get_content",
            RuntimeError("Failed to launch browser: Executable doesn't exist"),
            "Browser error: Failed to launch browser: Executable doesn't exist",
        ),
        (
            {"action": "snapshot"},
            "browser_snapshot",
            AttributeError("'Page' object has no attribute 'accessibility'"),
            "Browser action 'snapshot' failed: 'Page' object has no attribute 'accessibility'",
        ),
        (
            {"action": "click", "selector": "#missing"},
            "browser_click",
            TimeoutError("Timeout 10000ms exceeded."),
            "Browser action 'click' failed: Timeout 10000ms exceeded.",
        ),
    ],
)
async def test_a_failed_action_is_recorded_as_a_failed_call(monkeypatch, arguments, action_function, exc, content):
    monkeypatch.setattr(f"app.services.browser_tool.{action_function}", _raising(exc))

    result = await _call(arguments)

    assert result.content == content
    assert result.is_error is True
    assert result.tool_call_id == "call-1"


async def test_an_unknown_action_is_recorded_as_a_failed_call():
    result = await _call({"action": "pinch"})

    assert result.content.startswith("Unknown action: 'pinch'. Available: navigate,")
    assert result.is_error is True


async def test_a_successful_action_is_still_a_success(monkeypatch):
    async def _content(_session_key):
        return "URL: https://example.com/\nTitle: Example Domain\n\nExample Domain"

    monkeypatch.setattr("app.services.browser_tool.browser_get_content", _content)

    result = await _call({"action": "get_content"})

    assert result.content.startswith("URL: https://example.com/")
    assert result.is_error is False


async def test_an_argument_error_is_still_a_failed_call():
    result = await _call({"action": "navigate"})

    assert result.content == "Error: 'url' is required for the navigate action."
    assert result.is_error is True
