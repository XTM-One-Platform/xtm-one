"""Test that _SafeIntegrationExecutor filters out LLM-hallucinated kwargs.

Regression test for the bug where `list_notion_block_children` crashed with:
  TypeError: build_tools.<locals>.list_notion_block_children() got an
  unexpected keyword argument 'max_depth'

The fix ensures that _SafeIntegrationExecutor.execute_tool() applies the
same _accepted_params filtering as the base CallableToolExecutor before
dispatching to the handler.
"""

from __future__ import annotations

import json

import pytest

from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_executor_with_tool(
    tool_name: str, has_var_keyword: bool = False
) -> _SafeIntegrationExecutor:
    """Build a _SafeIntegrationExecutor with a single registered handler."""

    if has_var_keyword:

        async def handler(block_id: str, **kwargs: object) -> str:
            return f"called with block_id={block_id!r}"

    else:

        async def handler(block_id: str) -> str:
            return f"called with block_id={block_id!r}"

    executor = _SafeIntegrationExecutor()
    executor.register(tool_name, handler)
    return executor


# ---------------------------------------------------------------------------
# Core regression: unexpected kwarg must be silently dropped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hallucinated_kwarg_is_silently_filtered():
    """LLM-hallucinated kwarg must not raise TypeError; result must succeed."""
    executor = _make_executor_with_tool("list_notion_block_children")

    result = await executor.execute_tool(
        ToolCall(
            id="call-1",
            name="list_notion_block_children",
            # 'max_depth' does not exist in the handler signature
            arguments={"block_id": "page-abc", "max_depth": 3},
        )
    )

    assert not result.is_error, f"Unexpected error: {result.content}"
    assert "page-abc" in result.content


@pytest.mark.asyncio
async def test_multiple_hallucinated_kwargs_are_filtered():
    """All unknown kwargs must be stripped even if several are hallucinated."""
    executor = _make_executor_with_tool("list_notion_block_children")

    result = await executor.execute_tool(
        ToolCall(
            id="call-2",
            name="list_notion_block_children",
            arguments={
                "block_id": "page-xyz",
                "max_depth": 5,
                "include_content": True,
                "format": "json",
            },
        )
    )

    assert not result.is_error, f"Unexpected error: {result.content}"
    assert "page-xyz" in result.content


@pytest.mark.asyncio
async def test_valid_kwargs_are_still_forwarded():
    """Declared parameters must still reach the handler after filtering."""
    executor = _make_executor_with_tool("list_notion_block_children")

    result = await executor.execute_tool(
        ToolCall(
            id="call-3",
            name="list_notion_block_children",
            arguments={"block_id": "page-123"},
        )
    )

    assert not result.is_error
    assert "page-123" in result.content


# ---------------------------------------------------------------------------
# Handler with **kwargs: no filtering should occur
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_with_var_keyword_passes_all_kwargs():
    """Handlers that accept **kwargs must receive all arguments unfiltered."""

    async def greedy_handler(block_id: str, **kwargs: object) -> dict:
        return {"block_id": block_id, "extra": kwargs}

    executor = _SafeIntegrationExecutor()
    executor.register("greedy_tool", greedy_handler)

    result = await executor.execute_tool(
        ToolCall(
            id="call-4",
            name="greedy_tool",
            arguments={"block_id": "blk-1", "extra_param": "value"},
        )
    )

    assert not result.is_error
    # The extra kwarg must pass through since the handler accepts **kwargs
    data = json.loads(result.content)
    assert data["block_id"] == "blk-1"
    assert data["extra"]["extra_param"] == "value"


# ---------------------------------------------------------------------------
# Non-dict arguments (defensive parsing)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_none_arguments_treated_as_empty():
    """None call.arguments must not crash; handler is called with no args."""

    async def no_arg_handler() -> str:
        return "ok"

    executor = _SafeIntegrationExecutor()
    executor.register("no_args_tool", no_arg_handler)

    call = ToolCall(id="call-5", name="no_args_tool", arguments=None)  # type: ignore[arg-type]
    result = await executor.execute_tool(call)

    assert not result.is_error
    assert result.content == "ok"


# ---------------------------------------------------------------------------
# Notion-specific regression (exact production scenario)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notion_executor_filters_max_depth():
    """Mirrors the exact production crash: 'max_depth' hallucinated by LLM."""
    executor = _make_executor_with_tool("list_notion_block_children")

    result = await executor.execute_tool(
        ToolCall(
            id="prod-call",
            name="list_notion_block_children",
            arguments={"block_id": "notion-page-id", "max_depth": 2},
        )
    )

    # Before the fix this raised:
    #   TypeError: list_notion_block_children() got an unexpected keyword
    #   argument 'max_depth'
    assert not result.is_error, f"Regression: {result.content}"


# ---------------------------------------------------------------------------
# TypeError safety-net: graceful error when filtering was bypassed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_type_error_returns_graceful_error_message():
    """When introspection fails and filtering is bypassed, a TypeError from the
    handler must be caught and returned as a structured, actionable ToolResult
    instead of propagating as a raw exception."""

    async def strict_handler(block_id: str) -> str:
        return f"ok:{block_id}"

    executor = _SafeIntegrationExecutor()
    executor.register("strict_tool", strict_handler)
    # Force accepted_params to None so filtering is skipped — simulates the
    # case where inspect.signature() failed at registration time.
    executor._accepted_params["strict_tool"] = None

    result = await executor.execute_tool(
        ToolCall(
            id="te-call",
            name="strict_tool",
            arguments={"block_id": "abc", "max_depth": 5},
        )
    )

    assert result.is_error
    # The error must name the tool so the LLM can self-correct.
    assert "strict_tool" in result.content
    # The message must guide the LLM to retry with correct parameters.
    assert "retry" in result.content.lower() or "parameter" in result.content.lower()
